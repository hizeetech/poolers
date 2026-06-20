from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from betting.models import TicketTransactionLedger, Transaction, WalletLedgerEntry


ZERO = Decimal("0.00")
LEGACY_DIRECT_TRANSACTION_TYPES = {"bet_payout", "ticket_deletion_refund"}


def _quantize(value):
    return Decimal(str(value or "0.00")).quantize(Decimal("0.01"))


def _gateway_label(tx):
    gateway = (getattr(tx, "payment_gateway", "") or "").strip().lower()
    return {
        "monnify": "Monnify",
        "paystack": "Paystack",
        "kora": "Kora",
    }.get(gateway, gateway.title() or "Deposit")


def _reference_sequence(source_obj):
    source_id = getattr(source_obj, "id", None)
    if hasattr(source_id, "int"):
        return str(source_id.int % 1000000).zfill(6)
    try:
        return str(int(source_id)).zfill(6)
    except Exception:
        return "000000"


def build_ticket_transaction_reference(*, wallet_entry=None, tx=None, created_at=None):
    if tx:
        preserved_reference = (getattr(tx, "external_reference", "") or getattr(tx, "paystack_reference", "") or "").strip()
        if preserved_reference:
            return preserved_reference
    if wallet_entry:
        preserved_reference = (getattr(wallet_entry, "reference", "") or "").strip()
        if preserved_reference:
            return preserved_reference
    event_time = created_at or getattr(wallet_entry, "created_at", None) or getattr(tx, "timestamp", None) or timezone.now()
    source_obj = wallet_entry or tx
    return f"TXN-{event_time.strftime('%Y%m%d')}-{_reference_sequence(source_obj)}"


def classify_ticket_transaction(*, tx=None, metadata=None):
    metadata = metadata or {}
    source_hint = (metadata.get("source") or metadata.get("type") or "").strip().lower()
    raw_type = (getattr(tx, "transaction_type", "") or "").strip().lower()

    if raw_type == "bet_placement":
        return "Ticket Purchase", "Ticket Purchase"
    if raw_type in {"ticket_deletion_refund", "fixture_deletion_refund"}:
        return "Ticket Voided", "Ticket Void"
    if raw_type == "bet_payout":
        return "Winning Settlement", "Ticket Settlement"
    if raw_type == "deposit":
        return "Deposit", _gateway_label(tx)
    if raw_type == "withdrawal":
        return "Withdrawal", "Withdrawal"
    if raw_type == "withdrawal_refund":
        return "Withdrawal Refund", "Withdrawal"
    if raw_type == "commission_payout":
        return "Commission Credit", "Commission"
    if raw_type in {"commission_recall_debit", "commission_recall_credit"}:
        return "Commission Adjustment", "Commission"
    if raw_type == "wallet_transfer_in":
        return "Wallet Transfer In", "Wallet Transfer"
    if raw_type == "wallet_transfer_out":
        return "Wallet Transfer Out", "Wallet Transfer"
    if raw_type == "bonus":
        return "Bonus Credit", "Bonus"
    if raw_type == "account_user_credit":
        return "Admin Credit", "Admin Credit"
    if raw_type == "account_user_debit":
        return "Admin Debit", "Admin Debit"
    if raw_type == "bet_payout_reversal":
        return "Winning Reversal", "Ticket Settlement"
    if raw_type == "ticket_refund_reversal":
        return "Refund Reversal", "Ticket Void"

    if source_hint in {"gateway_deposit", "paystack", "monnify", "kora"}:
        return "Deposit", source_hint.replace("_", " ").title()
    if source_hint in {"withdraw_request"}:
        return "Withdrawal", "Withdrawal"
    if source_hint in {"loan_approval", "manual_overdraft"}:
        return "Overdraft Credit", "Overdraft"
    if source_hint in {"loan_manual_remit_clawback"}:
        return "Overdraft Debit", "Overdraft"
    if source_hint in {"loan_manual_remit", "result_backfill"}:
        return "Wallet Adjustment", source_hint.replace("_", " ").title()
    if source_hint in {"admin_bulk_void", "ticket_void"}:
        return "Ticket Voided", "Ticket Void"
    if source_hint in {"admin_action"}:
        return "Admin Credit", "Admin Credit"
    return "Wallet Adjustment", raw_type.replace("_", " ").title() or source_hint.replace("_", " ").title() or "Wallet"


def _ticket_for_event(*, wallet_entry=None, tx=None):
    if tx and getattr(tx, "related_bet_ticket_id", None):
        return tx.related_bet_ticket
    if wallet_entry and getattr(wallet_entry, "transaction", None) and getattr(wallet_entry.transaction, "related_bet_ticket_id", None):
        return wallet_entry.transaction.related_bet_ticket
    return None


def _created_by_for_event(*, wallet_entry=None, tx=None):
    if wallet_entry and getattr(wallet_entry, "actor_id", None):
        return wallet_entry.actor
    if tx and getattr(tx, "initiating_user_id", None):
        return tx.initiating_user
    return None


def _description_for_event(*, wallet_entry=None, tx=None):
    if tx and (tx.description or "").strip():
        return tx.description.strip()
    if wallet_entry and (wallet_entry.reason or "").strip():
        return wallet_entry.reason.strip()
    return ""


def _ledger_payload_for_wallet_entry(wallet_entry, *, balance_before=None, balance_after=None):
    tx = getattr(wallet_entry, "transaction", None)
    credit = wallet_entry.amount if wallet_entry.direction == "credit" else ZERO
    debit = wallet_entry.amount if wallet_entry.direction == "debit" else ZERO
    transaction_type, source = classify_ticket_transaction(tx=tx, metadata=wallet_entry.metadata or {})
    return {
        "user": wallet_entry.user,
        "ticket": _ticket_for_event(wallet_entry=wallet_entry, tx=tx),
        "transaction": tx,
        "wallet_ledger_entry": wallet_entry,
        "event_key": f"wallet-ledger:{wallet_entry.id}",
        "reference": build_ticket_transaction_reference(wallet_entry=wallet_entry, tx=tx, created_at=wallet_entry.created_at),
        "transaction_type": transaction_type,
        "source": source,
        "description": _description_for_event(wallet_entry=wallet_entry, tx=tx),
        "debit": _quantize(debit),
        "credit": _quantize(credit),
        "balance_before": _quantize(balance_before if balance_before is not None else wallet_entry.balance_before),
        "balance_after": _quantize(balance_after if balance_after is not None else wallet_entry.balance_after),
        "created_by": _created_by_for_event(wallet_entry=wallet_entry, tx=tx),
        "ip_address": (wallet_entry.metadata or {}).get("ip_address"),
        "metadata": wallet_entry.metadata or {},
        "created_at": wallet_entry.created_at,
    }


def _signed_amount_for_legacy_transaction(tx):
    if tx.transaction_type in {"bet_payout", "ticket_deletion_refund"}:
        return _quantize(tx.amount)
    return ZERO


def _ledger_payload_for_legacy_transaction(tx, *, balance_before, balance_after):
    transaction_type, source = classify_ticket_transaction(tx=tx, metadata={})
    signed_amount = _signed_amount_for_legacy_transaction(tx)
    credit = signed_amount if signed_amount > 0 else ZERO
    debit = abs(signed_amount) if signed_amount < 0 else ZERO
    return {
        "user": tx.user,
        "ticket": _ticket_for_event(tx=tx),
        "transaction": tx,
        "wallet_ledger_entry": None,
        "event_key": f"legacy-transaction:{tx.id}",
        "reference": build_ticket_transaction_reference(tx=tx, created_at=tx.timestamp),
        "transaction_type": transaction_type,
        "source": source,
        "description": _description_for_event(tx=tx),
        "debit": _quantize(debit),
        "credit": _quantize(credit),
        "balance_before": _quantize(balance_before),
        "balance_after": _quantize(balance_after),
        "created_by": _created_by_for_event(tx=tx),
        "ip_address": None,
        "metadata": {"legacy_backfill": True, "transaction_type": tx.transaction_type},
        "created_at": tx.timestamp,
    }


def upsert_ticket_transaction_ledger(payload):
    defaults = dict(payload)
    event_key = defaults.pop("event_key")
    ledger, _ = TicketTransactionLedger.objects.update_or_create(
        event_key=event_key,
        defaults=defaults,
    )
    return ledger


def sync_ticket_transaction_ledger_for_wallet_entry(wallet_entry):
    payload = _ledger_payload_for_wallet_entry(wallet_entry)
    return upsert_ticket_transaction_ledger(payload)


def legacy_transactions_without_wallet_ledger_queryset():
    return (
        Transaction.objects.select_related("user", "initiating_user", "related_bet_ticket")
        .filter(
            transaction_type__in=LEGACY_DIRECT_TRANSACTION_TYPES,
            status="completed",
            is_successful=True,
            wallet_ledger_entries__isnull=True,
        )
        .order_by("user_id", "timestamp", "id")
    )


def rebuild_ticket_transaction_ledger_for_user(user_id):
    wallet_entries = list(
        WalletLedgerEntry.objects.select_related(
            "user",
            "actor",
            "transaction",
            "transaction__initiating_user",
            "transaction__related_bet_ticket",
        )
        .filter(user_id=user_id)
        .order_by("created_at", "id")
    )
    legacy_transactions = list(legacy_transactions_without_wallet_ledger_queryset().filter(user_id=user_id))

    events = []
    for entry in wallet_entries:
        events.append(("wallet", entry.created_at, entry.id, entry))
    for tx in legacy_transactions:
        events.append(("legacy", tx.timestamp, str(tx.id), tx))
    events.sort(key=lambda item: (item[1], 0 if item[0] == "legacy" else 1, str(item[2])))

    running_balance = None
    for kind, _created_at, _ordering, obj in events:
        if kind == "wallet":
            payload = _ledger_payload_for_wallet_entry(obj)
            running_balance = payload["balance_after"]
        else:
            delta = _signed_amount_for_legacy_transaction(obj)
            balance_before = _quantize(running_balance if running_balance is not None else ZERO)
            balance_after = _quantize(balance_before + delta)
            payload = _ledger_payload_for_legacy_transaction(
                obj,
                balance_before=balance_before,
                balance_after=balance_after,
            )
            running_balance = balance_after
        upsert_ticket_transaction_ledger(payload)

    return len(events)


@transaction.atomic
def backfill_ticket_transaction_ledgers():
    user_ids = set(WalletLedgerEntry.objects.values_list("user_id", flat=True))
    user_ids.update(legacy_transactions_without_wallet_ledger_queryset().values_list("user_id", flat=True))
    processed_events = 0
    for user_id in sorted(user_ids):
        processed_events += rebuild_ticket_transaction_ledger_for_user(user_id)
    return processed_events
