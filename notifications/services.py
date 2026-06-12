from dataclasses import dataclass
from django.apps import apps
from django.utils import timezone

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
import requests
from django.conf import settings
from django.utils import timezone


@dataclass(frozen=True)
class NotificationPayload:
    id: int
    notification_type: str
    title: str
    message: str
    data: dict
    created_at: str
    is_read: bool


def create_notification(*, recipient, notification_type, title, message="", data=None):
    Notification = apps.get_model("notifications", "Notification")
    obj = Notification.objects.create(
        recipient=recipient,
        notification_type=notification_type,
        title=title,
        message=message or "",
        data=data or {},
    )
    _broadcast_to_user(recipient_id=recipient.id, notification=obj)
    try:
        from .tasks import send_webpush_for_notification

        send_webpush_for_notification.delay(obj.id)
    except Exception:
        pass
    return obj


def create_broadcast_notification(*, queryset, notification_type, title, message="", data=None, batch_size=1000):
    Notification = apps.get_model("notifications", "Notification")
    channel_layer = get_channel_layer()

    buffer = []
    created = 0
    now = timezone.now()
    for user in queryset.iterator():
        buffer.append(
            Notification(
                recipient=user,
                notification_type=notification_type,
                title=title,
                message=message or "",
                data=data or {},
                created_at=now,
            )
        )
        if len(buffer) >= batch_size:
            Notification.objects.bulk_create(buffer)
            created += len(buffer)
            buffer = []

    if buffer:
        Notification.objects.bulk_create(buffer)
        created += len(buffer)

    if channel_layer:
        async_to_sync(channel_layer.group_send)(
            "notifications_broadcast",
            {
                "type": "notifications.broadcast",
                "payload": {
                    "title": title,
                    "message": message,
                    "notification_type": notification_type,
                    "data": data or {},
                },
            },
        )

    return created


def create_targeted_notifications(*, queryset, notification_type, title, message="", data=None, batch_size=500):
    Notification = apps.get_model("notifications", "Notification")
    channel_layer = get_channel_layer()

    buffer = []
    created = 0
    now = timezone.now()
    for user in queryset.iterator():
        buffer.append(
            Notification(
                recipient_id=user.id,
                notification_type=notification_type,
                title=title,
                message=message or "",
                data=data or {},
                created_at=now,
            )
        )
        if len(buffer) >= batch_size:
            created_objs = Notification.objects.bulk_create(buffer, batch_size=batch_size)
            created += len(created_objs)
            if channel_layer:
                for n in created_objs:
                    _broadcast_to_user(recipient_id=n.recipient_id, notification=n)
            buffer = []

    if buffer:
        created_objs = Notification.objects.bulk_create(buffer, batch_size=batch_size)
        created += len(created_objs)
        if channel_layer:
            for n in created_objs:
                _broadcast_to_user(recipient_id=n.recipient_id, notification=n)

    return created


def _broadcast_to_user(*, recipient_id, notification):
    channel_layer = get_channel_layer()
    if not channel_layer:
        return

    payload = {
        "id": notification.id,
        "notification_type": notification.notification_type,
        "title": notification.title,
        "message": notification.message,
        "data": notification.data or {},
        "created_at": notification.created_at.isoformat(),
        "is_read": bool(notification.is_read),
    }
    async_to_sync(channel_layer.group_send)(
        f"notifications_user_{recipient_id}",
        {"type": "notifications.push", "payload": payload},
    )


def send_sms_ebulksms(*, msisdn, message, sender=None):
    enabled = bool(getattr(settings, "EBULKSMS_ENABLED", False))
    if not enabled:
        return {"ok": False, "error": "disabled"}
    username = (getattr(settings, "EBULKSMS_USERNAME", None) or "").strip()
    apikey = (getattr(settings, "EBULKSMS_API_KEY", None) or "").strip()
    sender = (sender or getattr(settings, "EBULKSMS_SENDER", None) or "").strip()
    msisdn = (msisdn or "").strip()
    message = (message or "").strip()
    if not username or not apikey:
        return {"ok": False, "error": "missing_credentials"}
    if not sender:
        return {"ok": False, "error": "missing_sender"}
    if not msisdn:
        return {"ok": False, "error": "missing_recipient"}
    if not message:
        return {"ok": False, "error": "missing_message"}

    url = "https://api.ebulksms.com/sendsms.json"
    payload = {
        "SMS": {
            "auth": {"username": username, "apikey": apikey},
            "message": {"sender": sender[:14], "messagetext": message[:612], "flash": "0"},
            "recipients": {"gsm": [{"msidn": msisdn, "msgid": str(int(timezone.now().timestamp()))}]},
            "dndsender": "0",
        }
    }
    try:
        res = requests.post(url, json=payload, headers={"Content-Type": "application/json"}, timeout=20)
        data = res.json() if res.content else {}
        status = (((data or {}).get("response") or {}).get("status") or "").upper()
        ok = (res.status_code == 200) and (status == "SUCCESS")
        return {"ok": ok, "status_code": res.status_code, "status": status, "raw": data}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def get_ebulksms_balance():
    enabled = bool(getattr(settings, "EBULKSMS_ENABLED", False))
    if not enabled:
        return {"ok": False, "error": "disabled"}
    username = (getattr(settings, "EBULKSMS_USERNAME", None) or "").strip()
    apikey = (getattr(settings, "EBULKSMS_API_KEY", None) or "").strip()
    if not username or not apikey:
        return {"ok": False, "error": "missing_credentials"}
    url = f"https://api.ebulksms.com/balance/{username}/{apikey}"
    try:
        res = requests.get(url, timeout=20)
        if res.status_code != 200:
            return {"ok": False, "status_code": res.status_code, "text": res.text}
        return {"ok": True, "status_code": res.status_code, "text": (res.text or "").strip()}
    except Exception as e:
        return {"ok": False, "error": str(e)}
