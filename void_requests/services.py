from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from betting.models import BetTicket, SystemSetting
from notifications.services import create_notification

from .models import TicketVoidRequest, TicketVoidAuditLog
from datetime import timedelta


def compute_auto_void_at(*, requested_at):
    raw = SystemSetting.get_setting("VOID_REQUEST_TIMEOUT_MINUTES", "3")
    try:
        minutes = int(str(raw).strip())
    except Exception:
        minutes = 3
    if minutes < 1:
        minutes = 1
    return requested_at + timedelta(minutes=minutes)


@transaction.atomic
def create_void_request(*, ticket, cashier, reason=""):
    ticket = BetTicket.objects.select_for_update().select_related("user").get(pk=ticket.pk)
    if cashier.user_type != "cashier":
        raise ValueError("Only cashiers can request void.")
    if ticket.user_id != cashier.id:
        raise PermissionError("You can only request void for your own tickets.")
    if ticket.status != "pending":
        raise ValueError("Only pending tickets can be requested for void.")
    if ticket.status in ["cancelled", "deleted"]:
        raise ValueError("Ticket is already voided.")
    if TicketVoidRequest.objects.filter(ticket=ticket).exists():
        raise ValueError("Void request already exists for this ticket.")

    now = timezone.now()
    vr = TicketVoidRequest.objects.create(
        ticket=ticket,
        cashier=cashier,
        agent=getattr(cashier, "agent", None),
        requested_at=now,
        auto_void_at=compute_auto_void_at(requested_at=now),
        status=TicketVoidRequest.STATUS_PENDING,
        reason=(reason or "").strip(),
        is_processed=False,
    )
    TicketVoidAuditLog.objects.create(
        void_request=vr,
        ticket=ticket,
        cashier=cashier,
        agent=getattr(cashier, "agent", None),
        admin=None,
        action=TicketVoidAuditLog.ACTION_REQUEST_CREATED,
        old_status=ticket.status or "",
        new_status=ticket.status or "",
        amount_refunded=Decimal("0.00"),
    )
    return vr


@transaction.atomic
def reject_void_request(*, void_request_id, rejected_by, reason=""):
    vr = (
        TicketVoidRequest.objects.select_for_update(of=("self",))
        .select_related("ticket", "cashier", "agent")
        .get(pk=void_request_id)
    )
    if vr.is_processed:
        return vr
    now = timezone.now()
    vr.status = TicketVoidRequest.STATUS_REJECTED
    vr.is_processed = True
    vr.approved_by = rejected_by
    vr.approved_at = now
    if reason:
        vr.reason = (reason or "").strip()
    vr.save(update_fields=["status", "is_processed", "approved_by", "approved_at", "reason", "updated_at"])
    TicketVoidAuditLog.objects.create(
        void_request=vr,
        ticket=vr.ticket,
        cashier=vr.cashier,
        agent=vr.agent,
        admin=rejected_by,
        action=TicketVoidAuditLog.ACTION_REJECTED,
        old_status=vr.ticket.status or "",
        new_status=vr.ticket.status or "",
        amount_refunded=Decimal("0.00"),
    )
    return vr


@transaction.atomic
def approve_and_void_request(*, void_request_id, approved_by=None, is_auto=False):
    vr = (
        TicketVoidRequest.objects.select_for_update(of=("self",))
        .select_related("ticket", "cashier", "agent")
        .get(pk=void_request_id)
    )
    if vr.is_processed:
        return vr

    ticket = BetTicket.objects.select_for_update().get(pk=vr.ticket_id)
    old_status = ticket.status or ""
    if ticket.status in ["cancelled", "deleted"]:
        vr.status = TicketVoidRequest.STATUS_APPROVED if not is_auto else TicketVoidRequest.STATUS_AUTO_VOIDED
        vr.is_processed = True
        vr.approved_by = approved_by
        vr.approved_at = timezone.now()
        vr.save(update_fields=["status", "is_processed", "approved_by", "approved_at", "updated_at"])
        return vr
    if ticket.status != "pending":
        vr.status = TicketVoidRequest.STATUS_REJECTED
        vr.is_processed = True
        vr.approved_by = approved_by
        vr.approved_at = timezone.now()
        vr.save(update_fields=["status", "is_processed", "approved_by", "approved_at", "updated_at"])
        TicketVoidAuditLog.objects.create(
            void_request=vr,
            ticket=ticket,
            cashier=vr.cashier,
            agent=vr.agent,
            admin=approved_by,
            action=TicketVoidAuditLog.ACTION_REJECTED,
            old_status=old_status,
            new_status=ticket.status or "",
            amount_refunded=Decimal("0.00"),
        )
        return vr

    now = timezone.now()
    ticket.status = "deleted"
    ticket.deleted_by = approved_by
    ticket.deleted_at = now
    ticket.save()

    vr.status = TicketVoidRequest.STATUS_AUTO_VOIDED if is_auto else TicketVoidRequest.STATUS_APPROVED
    vr.is_processed = True
    vr.approved_by = approved_by
    vr.approved_at = now
    vr.save(update_fields=["status", "is_processed", "approved_by", "approved_at", "updated_at"])

    TicketVoidAuditLog.objects.create(
        void_request=vr,
        ticket=ticket,
        cashier=vr.cashier,
        agent=vr.agent,
        admin=approved_by,
        action=TicketVoidAuditLog.ACTION_AUTO_VOIDED if is_auto else TicketVoidAuditLog.ACTION_APPROVED,
        old_status=old_status,
        new_status=ticket.status or "",
        amount_refunded=Decimal("0.00"),
    )
    TicketVoidAuditLog.objects.create(
        void_request=vr,
        ticket=ticket,
        cashier=vr.cashier,
        agent=vr.agent,
        admin=approved_by,
        action=TicketVoidAuditLog.ACTION_REFUNDED,
        old_status=old_status,
        new_status=ticket.status or "",
        amount_refunded=ticket.stake_amount or Decimal("0.00"),
    )

    if vr.agent_id:
        create_notification(
            recipient=vr.agent,
            notification_type="VOID_REQUEST",
            title=f"Ticket {ticket.ticket_id} voided and refunded",
            message=f"Ticket {ticket.ticket_id} has been voided and refunded.",
            data={"ticket_id": ticket.ticket_id, "void_request_id": vr.id, "status": vr.status},
        )
    cashier_msg = (
        f"Your void request for Ticket {ticket.ticket_id} was automatically approved and refunded."
        if is_auto
        else f"Your void request for Ticket {ticket.ticket_id} has been approved and refunded."
    )
    create_notification(
        recipient=vr.cashier,
        notification_type="VOID_REQUEST",
        title=f"Void request processed ({ticket.ticket_id})",
        message=cashier_msg,
        data={"ticket_id": ticket.ticket_id, "void_request_id": vr.id, "status": vr.status},
    )
    return vr


def process_due_void_requests(*, limit=200):
    now = timezone.now()
    try:
        import sys

        is_test_run = any(a in ("test", "pytest") for a in sys.argv)
    except Exception:
        is_test_run = False
    due_ids = list(
        TicketVoidRequest.objects.filter(status=TicketVoidRequest.STATUS_PENDING, is_processed=False, auto_void_at__lte=now)
        .order_by("auto_void_at")
        .values_list("id", flat=True)[: int(limit or 200)]
    )
    processed = 0
    for vr_id in due_ids:
        try:
            approve_and_void_request(void_request_id=vr_id, approved_by=None, is_auto=True)
            processed += 1
        except Exception:
            if is_test_run:
                raise
            continue
    return processed
