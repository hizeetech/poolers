from decimal import Decimal

from django.db import transaction

from betting.models import Transaction, Wallet, WalletLedgerEntry


RESULT_VOID_REFUND_TYPES = {"ticket_deletion_refund", "ticket_cancellation_refund"}


def _reversal_wallet_entry(refund_reversal_tx):
    return refund_reversal_tx.wallet_ledger_entries.order_by("created_at", "id").first()


def _reversed_refund_transaction(wallet_entry):
    if not wallet_entry:
        return None
    reversed_tx_id = str((wallet_entry.metadata or {}).get("reversed_tx_id") or "").strip()
    if not reversed_tx_id:
        return None
    return Transaction.objects.filter(pk=reversed_tx_id).first()


def _is_incorrect_void_refund_reversal(refund_reversal_tx, *, wallet_entry=None, reversed_tx=None):
    if refund_reversal_tx.transaction_type != "ticket_refund_reversal":
        return False
    if refund_reversal_tx.status != "completed" or not refund_reversal_tx.is_successful:
        return False

    if reversed_tx and reversed_tx.transaction_type in RESULT_VOID_REFUND_TYPES:
        return True

    description = (refund_reversal_tx.description or "").strip().lower()
    return (
        "result correction reversal of ticket_deletion_refund" in description
        or "result correction reversal of ticket_cancellation_refund" in description
    )


def _adjustment_exists_for_reversal(refund_reversal_tx):
    return WalletLedgerEntry.objects.filter(
        metadata__refund_reversal_adjustment_for=str(refund_reversal_tx.id)
    ).exists()


@transaction.atomic
def apply_refund_reversal_adjustment(refund_reversal_tx, *, actor=None):
    wallet_entry = _reversal_wallet_entry(refund_reversal_tx)
    reversed_tx = _reversed_refund_transaction(wallet_entry)
    if not _is_incorrect_void_refund_reversal(refund_reversal_tx, wallet_entry=wallet_entry, reversed_tx=reversed_tx):
        return None
    if _adjustment_exists_for_reversal(refund_reversal_tx):
        return None

    wallet, _ = Wallet.objects.select_for_update().get_or_create(
        user=refund_reversal_tx.user,
        defaults={"balance": Decimal("0.00")},
    )
    ticket = refund_reversal_tx.related_bet_ticket
    ticket_id = ""
    if ticket and getattr(ticket, "ticket_id", None):
        ticket_id = str(ticket.ticket_id)
    elif wallet_entry and (wallet_entry.reference or "").strip():
        ticket_id = wallet_entry.reference.strip()
    elif reversed_tx and getattr(reversed_tx.related_bet_ticket, "ticket_id", None):
        ticket_id = str(reversed_tx.related_bet_ticket.ticket_id)
        ticket = reversed_tx.related_bet_ticket

    description = (
        f"Adjustment for incorrect refund reversal after result correction on voided ticket {ticket_id or refund_reversal_tx.id}"
    )
    adjustment_tx = Transaction.objects.create(
        user=refund_reversal_tx.user,
        initiating_user=actor if getattr(actor, "is_authenticated", False) else None,
        target_user=refund_reversal_tx.user,
        transaction_type="ticket_deletion_refund",
        amount=refund_reversal_tx.amount,
        is_successful=True,
        status="completed",
        description=description,
        related_bet_ticket=ticket,
    )
    wallet.apply_delta(
        amount=refund_reversal_tx.amount,
        actor=actor if getattr(actor, "is_authenticated", False) else None,
        transaction_obj=adjustment_tx,
        reference=(ticket_id or str(refund_reversal_tx.id))[:120],
        reason=description,
        metadata={
            "ticket_id": ticket_id,
            "source": "ticket_void",
            "adjustment_type": "incorrect_result_refund_reversal_backfill",
            "refund_reversal_adjustment_for": str(refund_reversal_tx.id),
            "original_reversed_tx_id": str(reversed_tx.id) if reversed_tx else "",
        },
    )
    return adjustment_tx


def backfill_incorrect_refund_reversal_adjustments(*, actor=None, dry_run=False):
    summary = {
        "scanned": 0,
        "eligible": 0,
        "adjusted": 0,
        "already_adjusted": 0,
        "skipped": 0,
    }
    queryset = (
        Transaction.objects.select_related("user", "related_bet_ticket")
        .filter(transaction_type="ticket_refund_reversal")
        .order_by("timestamp", "id")
    )

    for refund_reversal_tx in queryset.iterator():
        summary["scanned"] += 1
        wallet_entry = _reversal_wallet_entry(refund_reversal_tx)
        reversed_tx = _reversed_refund_transaction(wallet_entry)
        if not _is_incorrect_void_refund_reversal(
            refund_reversal_tx,
            wallet_entry=wallet_entry,
            reversed_tx=reversed_tx,
        ):
            summary["skipped"] += 1
            continue

        summary["eligible"] += 1
        if _adjustment_exists_for_reversal(refund_reversal_tx):
            summary["already_adjusted"] += 1
            continue

        if dry_run:
            summary["adjusted"] += 1
            continue

        if apply_refund_reversal_adjustment(refund_reversal_tx, actor=actor):
            summary["adjusted"] += 1

    return summary
