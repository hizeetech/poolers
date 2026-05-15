from dataclasses import dataclass
from django.apps import apps
from django.utils import timezone

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer


@dataclass(frozen=True)
class NotificationPayload:
    id: int
    notification_type: str
    title: str
    message: str
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
            {"type": "notifications.broadcast", "payload": {"title": title, "message": message, "notification_type": notification_type}},
        )

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
        "created_at": notification.created_at.isoformat(),
        "is_read": bool(notification.is_read),
    }
    async_to_sync(channel_layer.group_send)(
        f"notifications_user_{recipient_id}",
        {"type": "notifications.push", "payload": payload},
    )
