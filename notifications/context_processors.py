from .models import Notification
from django.conf import settings


def notifications_context(request):
    user = getattr(request, "user", None)
    if not user or user.is_anonymous:
        return {
            "notifications_unread_count": 0,
            "vapid_public_key": getattr(settings, "VAPID_PUBLIC_KEY", "") or "",
            "deposit_reminder_alert": None,
        }

    unread = Notification.objects.filter(recipient=user, is_read=False).count()
    recent_system = list(Notification.objects.filter(recipient=user).order_by("-created_at")[:10])
    
    reminder = (
        Notification.objects.filter(recipient=user, is_read=False, notification_type="DEPOSIT_REMINDER")
        .order_by("-created_at")
        .values("id", "title", "message", "data")
        .first()
    )
    if reminder:
        data = reminder.get("data") or {}
        reminder["url"] = (data.get("url") or "/wallet/") if isinstance(data, dict) else "/wallet/"

    return {
        "notifications_unread_count": unread,
        "notifications_recent": recent_system,
        "vapid_public_key": getattr(settings, "VAPID_PUBLIC_KEY", "") or "",
        "deposit_reminder_alert": reminder,
    }
