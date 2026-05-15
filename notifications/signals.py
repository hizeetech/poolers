from django.db.models.signals import post_save
from django.db import transaction
from django.dispatch import receiver
from django.core.cache import cache

from .models import SystemAnnouncement, NotificationCampaign
from .tasks import broadcast_announcement, send_campaign
from .services import create_notification


@receiver(post_save, sender=SystemAnnouncement)
def _announce_on_create(sender, instance, created, **kwargs):
    if instance.is_active:
        transaction.on_commit(lambda: broadcast_announcement.delay(instance.id))


@receiver(post_save, sender=NotificationCampaign)
def _send_campaign_on_save(sender, instance, created, **kwargs):
    if instance.send_now and not instance.sent_at:
        transaction.on_commit(lambda: send_campaign.delay(instance.id))


@receiver(post_save, sender="betting.Transaction")
def _notify_on_transaction(sender, instance, created, **kwargs):
    if not created:
        return
    if not instance.is_successful or instance.status != "completed":
        return

    if instance.transaction_type == "deposit":
        create_notification(
            recipient=instance.user,
            notification_type="DEPOSIT_SUCCESS",
            title="Deposit successful",
            message=f"Your deposit of ₦{instance.amount:.2f} was successful.",
            data={"transaction_id": str(instance.id), "amount": str(instance.amount), "gateway": instance.payment_gateway},
        )
    elif instance.transaction_type == "bet_payout" and instance.related_bet_ticket_id:
        create_notification(
            recipient=instance.user,
            notification_type="TICKET_SETTLED",
            title="Ticket payout credited",
            message=f"Your winning of ₦{instance.amount:.2f} has been credited.",
            data={"transaction_id": str(instance.id), "ticket_id": str(instance.related_bet_ticket_id)},
        )


@receiver(post_save, sender="betting.BetTicket")
def _notify_on_ticket_settled(sender, instance, **kwargs):
    if instance.status not in ["won", "lost", "cancelled", "deleted", "cashed_out"]:
        return

    key = f"notif:ticket:settled:{instance.id}:{instance.status}"
    if cache.get(key):
        return
    cache.set(key, 1, timeout=86400)

    notification_type = "TICKET_SETTLED"
    if instance.status == "won":
        title = "Ticket won"
        msg = f"Ticket {instance.ticket_id} was settled as WON."
    elif instance.status == "lost":
        title = "Ticket lost"
        msg = f"Ticket {instance.ticket_id} was settled as LOST."
    elif instance.status in ["cancelled", "deleted"]:
        notification_type = "TICKET_VOIDED"
        title = "Ticket voided"
        msg = f"Ticket {instance.ticket_id} was voided."
    elif instance.status == "cashed_out":
        title = "Ticket cashed out"
        msg = f"Ticket {instance.ticket_id} was cashed out."
    else:
        title = "Ticket updated"
        msg = f"Ticket {instance.ticket_id} status: {instance.status.upper()}."

    create_notification(
        recipient=instance.user,
        notification_type=notification_type,
        title=title,
        message=msg,
        data={"ticket_id": str(instance.id), "ticket_code": str(instance.ticket_id), "status": instance.status},
    )
