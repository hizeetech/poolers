import json
import os

ENABLE_CELERY_APPS = os.getenv("ENABLE_CELERY_APPS", "").strip().lower() in ("1", "true", "yes", "on")
FORCE_CELERY_ON_WINDOWS = os.getenv("FORCE_CELERY_ON_WINDOWS", "").strip().lower() in ("1", "true", "yes", "on")
CELERY_APPS_ENABLED = ENABLE_CELERY_APPS and (os.name != "nt" or FORCE_CELERY_ON_WINDOWS)
if CELERY_APPS_ENABLED:
    from celery import shared_task
else:
    def shared_task(*args, **kwargs):
        def decorator(func):
            def delay(*dargs, **dkwargs):
                return func(*dargs, **dkwargs)
            func.delay = delay
            return func
        if args and callable(args[0]) and not kwargs:
            return decorator(args[0])
        return decorator

from django.apps import apps
from django.conf import settings
from django.core.cache import cache
from django.db.models import Q
from django.utils import timezone

from .services import create_broadcast_notification, create_notification


@shared_task
def broadcast_announcement(announcement_id):
    SystemAnnouncement = apps.get_model("notifications", "SystemAnnouncement")
    User = apps.get_model("betting", "User")

    ann = SystemAnnouncement.objects.filter(id=announcement_id, is_active=True).first()
    if not ann:
        return 0

    now = timezone.now()
    if ann.starts_at and ann.starts_at > now:
        return 0
    if ann.ends_at and ann.ends_at < now:
        return 0

    qs = User.objects.filter(is_active=True).only("id")
    return create_broadcast_notification(
        queryset=qs,
        notification_type="SYSTEM_ANNOUNCEMENT",
        title=ann.title,
        message=ann.message,
        data={"announcement_id": ann.id},
    )


@shared_task
def send_deposit_reminders():
    RiskEngineSettings = apps.get_model("risk", "RiskEngineSettings")
    Wallet = apps.get_model("betting", "Wallet")

    settings_obj = RiskEngineSettings.load()
    threshold = settings_obj.deposit_reminder_threshold
    if threshold is None:
        return 0

    today = timezone.localdate().isoformat()
    qs = (
        Wallet.objects.filter(balance__lte=threshold, user__is_active=True)
        .filter(user__user_type__in=['player', 'cashier', 'agent', ''])
        .select_related("user")
        .only("id", "balance", "user_id")
    )

    sent = 0
    for w in qs.iterator():
        key = f"notifications:deposit_reminder:{w.user_id}:{today}"
        if not cache.add(key, 1, timeout=86400):
            continue
        try:
            create_notification(
                recipient=w.user,
                notification_type="DEPOSIT_REMINDER",
                title="Low wallet balance",
                message=f"Your wallet balance is ₦{w.balance:.2f}. Deposit now to keep betting and avoid missing live fixtures.",
                data={"threshold": str(threshold), "balance": str(w.balance), "url": "/wallet/"},
            )
            sent += 1
        except Exception:
            continue

    return sent


@shared_task
def send_webpush_for_notification(notification_id):
    Notification = apps.get_model("notifications", "Notification")
    WebPushSubscription = apps.get_model("notifications", "WebPushSubscription")
    n = Notification.objects.filter(id=notification_id).select_related("recipient").first()
    if not n:
        return 0

    vapid_public = getattr(settings, "VAPID_PUBLIC_KEY", "") or ""
    vapid_private = getattr(settings, "VAPID_PRIVATE_KEY", "") or ""
    vapid_subject = getattr(settings, "VAPID_SUBJECT", "mailto:admin@example.com") or "mailto:admin@example.com"
    if not vapid_public or not vapid_private:
        return 0

    try:
        from pywebpush import webpush
    except Exception:
        return 0

    subs = WebPushSubscription.objects.filter(user=n.recipient).only("endpoint", "p256dh", "auth")
    payload = {
        "title": n.title,
        "body": n.message or "",
        "data": {"notification_id": n.id, "type": n.notification_type, "url": "/notifications/"},
    }
    headers = {"Urgency": "high"}

    sent = 0
    for s in subs.iterator():
        try:
            webpush(
                subscription_info={
                    "endpoint": s.endpoint,
                    "keys": {"p256dh": s.p256dh, "auth": s.auth},
                },
                data=json.dumps(payload),
                vapid_private_key=vapid_private,
                vapid_claims={"sub": vapid_subject},
                headers=headers,
            )
            sent += 1
        except Exception:
            continue
    return sent


@shared_task
def send_campaign(campaign_id):
    NotificationCampaign = apps.get_model("notifications", "NotificationCampaign")
    User = apps.get_model("betting", "User")

    campaign = NotificationCampaign.objects.filter(id=campaign_id).first()
    if not campaign or campaign.sent_at:
        return 0

    qs = User.objects.filter(is_active=True).only("id")
    if not campaign.send_to_all:
        q = Q()
        user_types = campaign.target_user_types or []
        user_ids = campaign.target_user_ids or []
        if user_types:
            q |= Q(user_type__in=user_types)
        if user_ids:
            q |= Q(id__in=user_ids)
        if q:
            qs = qs.filter(q)
        else:
            return 0

    created = create_broadcast_notification(
        queryset=qs,
        notification_type=campaign.notification_type or "SYSTEM_ANNOUNCEMENT",
        title=campaign.title,
        message=campaign.message,
        data={"campaign_id": campaign.id},
    )

    campaign.sent_at = timezone.now()
    campaign.send_now = False
    campaign.save(update_fields=["sent_at", "send_now", "updated_at"])
    return created
