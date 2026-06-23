from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from decimal import Decimal, ROUND_DOWN

from django.conf import settings
from django.core.mail import send_mail
from django.db import transaction
from django.db.models import Q, Sum
from django.urls import reverse
from django.utils import timezone

from betting.models import (
    AccountLockAuditLog,
    AccountUnlockAppeal,
    BetTicket,
    Loan,
    LoanAuditLog,
    LoanPendingCredit,
    LoanRepayment,
    LoginAttempt,
    OverdraftWallet,
    SiteConfiguration,
    Transaction,
    User,
    Wallet,
)
from betting.utils import logout_user_from_all_active_sessions
from notifications.services import create_notification, send_sms_ebulksms


WEEKDAY_NAME_MAP = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}

LOAN_LOCK_REASON = (
    "Due to an unsettled overdraft/loan obligation, your account has been disabled. "
    "Please contact Customer Service or the Administrator for assistance."
)


class LoanOverdraftError(Exception):
    pass


@dataclass
class QualificationSnapshot:
    ticket_count: int
    deposit_total: Decimal
    qualified_amount: Decimal
    request_open_at: datetime
    due_at: datetime
    can_submit_now: bool
    blockers: list[str]


def quantize_money(value) -> Decimal:
    return Decimal(str(value or "0")).quantize(Decimal("0.01"))


def get_loan_settings():
    cfg = SiteConfiguration.load()
    return {
        "min_ticket_count": int(getattr(cfg, "loan_min_ticket_count", 50) or 50),
        "min_deposit_amount": quantize_money(getattr(cfg, "loan_min_deposit_amount", Decimal("50000.00"))),
        "loan_percentage": quantize_money(getattr(cfg, "loan_percentage", Decimal("50.00"))),
        "application_day": (getattr(cfg, "loan_application_day", "friday") or "friday").strip().lower(),
        "application_time": getattr(cfg, "loan_application_time", None) or time(16, 0),
        "repayment_day": (getattr(cfg, "loan_repayment_day", "saturday") or "saturday").strip().lower(),
        "repayment_time": getattr(cfg, "loan_repayment_time", None) or time(15, 0),
    }


def get_current_commission_window(reference_dt=None):
    local_now = timezone.localtime(reference_dt or timezone.now())
    start_local = (local_now - timedelta(days=(local_now.weekday() - WEEKDAY_NAME_MAP["tuesday"]) % 7)).replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )
    end_local = start_local + timedelta(days=7)
    return start_local, end_local


def _this_weeks_named_day(named_day: str, at_time: time, *, reference_dt=None):
    local_now = timezone.localtime(reference_dt or timezone.now())
    weekday = WEEKDAY_NAME_MAP.get((named_day or "").strip().lower(), WEEKDAY_NAME_MAP["friday"])
    delta = (weekday - local_now.weekday()) % 7
    candidate_date = (local_now + timedelta(days=delta)).date()
    return timezone.make_aware(datetime.combine(candidate_date, at_time), timezone.get_current_timezone())


def _next_due_datetime(reference_dt=None):
    settings_map = get_loan_settings()
    due_at = _this_weeks_named_day(settings_map["repayment_day"], settings_map["repayment_time"], reference_dt=reference_dt)
    return due_at


def _application_open_datetime(reference_dt=None):
    settings_map = get_loan_settings()
    return _this_weeks_named_day(settings_map["application_day"], settings_map["application_time"], reference_dt=reference_dt)


def get_loan_metric_user_ids(user: User):
    ids = {user.id}
    if user.user_type == "agent":
        ids.update(User.objects.filter(user_type="cashier", agent=user).values_list("id", flat=True))
    elif user.user_type == "super_agent":
        agent_ids = list(User.objects.filter(user_type="agent", super_agent=user).values_list("id", flat=True))
        ids.update(agent_ids)
        if agent_ids:
            ids.update(User.objects.filter(user_type="cashier", agent_id__in=agent_ids).values_list("id", flat=True))
        ids.update(User.objects.filter(user_type="cashier", super_agent=user, agent__isnull=True).values_list("id", flat=True))
    return list(ids)


def build_qualification_snapshot(user: User, reference_dt=None) -> QualificationSnapshot:
    settings_map = get_loan_settings()
    start_dt, _end_dt = get_current_commission_window(reference_dt=reference_dt)
    local_now = timezone.localtime(reference_dt or timezone.now())
    request_open_at = _application_open_datetime(reference_dt=reference_dt)
    due_at = _next_due_datetime(reference_dt=reference_dt)
    metric_user_ids = get_loan_metric_user_ids(user)

    ticket_count = BetTicket.objects.filter(
        user_id__in=metric_user_ids,
        placed_at__gte=start_dt,
    ).exclude(status__in=["deleted", "cancelled"]).count()

    direct_gateway_deposit_total = (
        Transaction.objects.filter(
            user_id__in=metric_user_ids,
            transaction_type="deposit",
            status="completed",
            is_successful=True,
            payment_gateway__in=["monnify", "paystack", "kora"],
            timestamp__gte=start_dt,
        )
        .exclude(loan_pending_credits__source="gateway_deposit")
        .aggregate(total=Sum("amount"))["total"]
        or Decimal("0.00")
    )
    direct_gateway_deposit_total = quantize_money(direct_gateway_deposit_total)
    released_gateway_excess_total = Decimal("0.00")
    released_gateway_excess_entries = LoanPendingCredit.objects.filter(
        borrower_id__in=metric_user_ids,
        source="gateway_deposit",
        processed_at__isnull=False,
    ).filter(
        Q(source_transaction__timestamp__gte=start_dt)
        | Q(source_transaction__isnull=True, created_at__gte=start_dt)
    )
    for entry in released_gateway_excess_entries:
        try:
            released_gateway_excess_total += Decimal(
                str((entry.metadata or {}).get("qualified_deposit_excess_amount", "0.00") or "0.00")
            )
        except Exception:
            continue
    released_gateway_excess_total = quantize_money(released_gateway_excess_total)
    deposit_total = quantize_money(direct_gateway_deposit_total + released_gateway_excess_total)
    qualified_amount = (
        (deposit_total * settings_map["loan_percentage"] / Decimal("100.00"))
        .quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    )

    blockers = []
    if ticket_count < settings_map["min_ticket_count"]:
        blockers.append(f"Minimum ticket count is {settings_map['min_ticket_count']}.")
    if deposit_total < settings_map["min_deposit_amount"]:
        blockers.append(f"Minimum qualified deposit volume is N{settings_map['min_deposit_amount']}.")
    if local_now < request_open_at:
        blockers.append("Loan requests open every Friday at 4:00 PM WAT.")

    return QualificationSnapshot(
        ticket_count=ticket_count,
        deposit_total=deposit_total,
        qualified_amount=qualified_amount,
        request_open_at=request_open_at,
        due_at=due_at,
        can_submit_now=(len(blockers) == 0),
        blockers=blockers,
    )


def get_user_outstanding_loans(user: User):
    return Loan.objects.filter(
        borrower=user,
        status__in=["active", "overdue", "defaulted"],
        outstanding_balance__gt=Decimal("0.00"),
    ).order_by("due_date", "created_at")


def get_user_outstanding_loan_amount(user: User) -> Decimal:
    total = get_user_outstanding_loans(user).aggregate(total=Sum("outstanding_balance"))["total"] or Decimal("0.00")
    return quantize_money(total)


def user_has_outstanding_loan(user: User) -> bool:
    return get_user_outstanding_loans(user).exists()


def get_overdraft_restriction_borrower(user: User) -> User | None:
    if not getattr(user, "is_authenticated", False):
        return None
    if getattr(user, "user_type", "") in {"agent", "super_agent"}:
        return user
    if getattr(user, "user_type", "") == "cashier":
        return getattr(user, "agent", None) or getattr(user, "super_agent", None)
    return None


def user_has_overdraft_access_restriction(user: User) -> bool:
    borrower = get_overdraft_restriction_borrower(user)
    return bool(borrower and user_has_outstanding_loan(borrower))


def user_has_overdraft_wallet_transfer_restriction(user: User) -> bool:
    borrower = get_overdraft_restriction_borrower(user)
    if not borrower:
        return False
    now = timezone.now()
    return (
        get_user_outstanding_loans(borrower)
        .filter(Q(status__in=["overdue", "defaulted"]) | Q(due_date__lt=now))
        .exists()
    )


def get_user_pending_credit_amount(user: User) -> Decimal:
    total = (
        LoanPendingCredit.objects.filter(borrower=user, remaining_amount__gt=Decimal("0.00")).aggregate(
            total=Sum("remaining_amount")
        )["total"]
        or Decimal("0.00")
    )
    return quantize_money(total)


def can_user_transfer_from_wallet(user: User) -> bool:
    return not user_has_overdraft_wallet_transfer_restriction(user)


def can_user_place_bet(user: User) -> bool:
    return getattr(user, "is_authenticated", False) and getattr(user, "user_type", "") == "cashier" and not user_has_overdraft_access_restriction(user)


def build_recent_wallet_transactions_payload(user: User, *, limit: int = 20):
    rows = []
    transactions = (
        Transaction.objects.filter(user=user)
        .prefetch_related("wallet_ledger_entries")
        .order_by("-timestamp")[:limit]
    )
    for tx in transactions:
        details_url = ""
        if tx.transaction_type == "deposit" and tx.external_reference:
            try:
                details_url = reverse("betting:deposit_status", args=[tx.external_reference])
            except Exception:
                details_url = ""
        wallet_entry = next(iter(sorted(tx.wallet_ledger_entries.all(), key=lambda entry: (entry.created_at, entry.id))), None)
        rows.append(
            {
                "id": tx.id,
                "timestamp_display": (
                    timezone.localtime(tx.timestamp).strftime("%b %d, %Y %H:%M")
                    if tx.timestamp
                    else "-"
                ),
                "transaction_type": tx.transaction_type,
                "transaction_type_display": tx.get_transaction_type_display(),
                "amount": f"{quantize_money(tx.amount):.2f}",
                "status": tx.status,
                "status_display": tx.get_status_display(),
                "description": tx.description or "",
                "details_url": details_url,
                "balance_before": (
                    f"{quantize_money(wallet_entry.balance_before):.2f}"
                    if wallet_entry else ""
                ),
                "balance_after": (
                    f"{quantize_money(wallet_entry.balance_after):.2f}"
                    if wallet_entry else ""
                ),
            }
        )
    return rows


def get_loan_wallet_resident_amount(loan: Loan) -> Decimal:
    snapshot = getattr(loan, "workflow_snapshot", {}) or {}
    try:
        resident_amount = Decimal(str(snapshot.get("wallet_resident_amount", "0.00") or "0.00"))
    except Exception:
        resident_amount = Decimal("0.00")
    return quantize_money(max(Decimal("0.00"), resident_amount))


def get_user_wallet_resident_loan_amount(user: User) -> Decimal:
    total = sum((get_loan_wallet_resident_amount(loan) for loan in get_user_outstanding_loans(user)), Decimal("0.00"))
    return quantize_money(total)


def build_wallet_overdraft_payload(user: User):
    wallet, _ = Wallet.objects.get_or_create(user=user, defaults={"balance": Decimal("0.00")})
    qualification_snapshot = build_qualification_snapshot(user) if user.user_type in ["agent", "super_agent"] else None
    active_loans = list(
        Loan.objects.filter(
        borrower=user,
        status__in=["active", "overdue", "defaulted"],
        outstanding_balance__gt=Decimal("0.00"),
    ).select_related("lender").order_by("due_date", "-created_at")
    )
    pending_loan_requests = Loan.objects.filter(borrower=user, status="pending").order_by("-created_at")
    primary_outstanding_loan = active_loans[0] if active_loans else None
    outstanding_overdraft_amount = get_user_outstanding_loan_amount(user) if user.user_type in ["agent", "super_agent"] else Decimal("0.00")
    pending_remittance_credit = get_user_pending_credit_amount(user) if user.user_type in ["agent", "super_agent"] else Decimal("0.00")
    recent_transactions = build_recent_wallet_transactions_payload(user)
    outstanding_loan_rows = [
        {
            "loan_id": loan.id,
            "lender_name": loan.lender.username or loan.lender.email,
            "outstanding_balance": f"{loan.outstanding_balance:.2f}",
            "due_date_display": (
                timezone.localtime(loan.due_date).strftime("%b %d, %Y %I:%M %p %Z")
                if loan.due_date
                else "-"
            ),
            "status": loan.status,
            "status_display": loan.get_status_display(),
        }
        for loan in active_loans
    ]

    repayment_status = "Cleared"
    repayment_badge = "success"
    if primary_outstanding_loan:
        if primary_outstanding_loan.status in ["overdue", "defaulted"]:
            repayment_status = "Overdue"
            repayment_badge = "danger"
        else:
            repayment_status = "Outstanding"
            repayment_badge = "warning"
    elif pending_loan_requests.exists():
        repayment_status = "Pending Approval"
        repayment_badge = "info"

    return {
        "wallet_balance": f"{wallet.balance:.2f}",
        "outstanding_overdraft_amount": f"{outstanding_overdraft_amount:.2f}",
        "pending_remittance_credit": f"{pending_remittance_credit:.2f}",
        "repayment_status": repayment_status,
        "repayment_badge": repayment_badge,
        "due_date_display": (
            timezone.localtime(primary_outstanding_loan.due_date).strftime("%A %I:%M %p %Z")
            if primary_outstanding_loan and primary_outstanding_loan.due_date
            else "-"
        ),
        "can_remit_overdraft": bool(primary_outstanding_loan and pending_remittance_credit > Decimal("0.00")),
        "has_outstanding_loan": bool(primary_outstanding_loan),
        "can_withdraw_from_wallet": not bool(primary_outstanding_loan),
        "can_transfer_from_wallet": can_user_transfer_from_wallet(user),
        "outstanding_loans": outstanding_loan_rows,
        "qualification_ticket_count": qualification_snapshot.ticket_count if qualification_snapshot else 0,
        "qualification_deposit_total": f"{qualification_snapshot.deposit_total:.2f}" if qualification_snapshot else "0.00",
        "qualification_qualified_amount": f"{qualification_snapshot.qualified_amount:.2f}" if qualification_snapshot else "0.00",
        "recent_transactions": recent_transactions,
    }


def get_primary_approver_for_request(user: User):
    if user.user_type == "agent":
        return getattr(user, "super_agent", None)
    if user.user_type == "super_agent":
        return (
            User.objects.filter(Q(is_superuser=True) | Q(user_type="admin"), is_active=True)
            .order_by("-is_superuser", "id")
            .first()
        )
    return None


def get_or_create_overdraft_wallet(super_agent: User):
    if not super_agent or super_agent.user_type != "super_agent":
        raise LoanOverdraftError("Overdraft wallet is available only for super agents.")
    wallet, _ = OverdraftWallet.objects.get_or_create(super_agent=super_agent)
    return wallet


def fund_overdraft_wallet(*, super_agent: User, amount, actor=None, reason="", ip_address=None):
    amount = quantize_money(amount)
    if amount <= 0:
        raise LoanOverdraftError("Funding amount must be greater than zero.")
    wallet = get_or_create_overdraft_wallet(super_agent)
    before, after = wallet.apply_delta(
        amount=amount,
        actor=actor,
        reference=f"fund:{super_agent.id}",
        reason=reason or f"Admin funding for {super_agent.username or super_agent.email}",
        metadata={"source": "admin_funding"},
    )
    return wallet, before, after


def notify_loan_event(*, user: User, title: str, message: str, notification_type="SYSTEM_ANNOUNCEMENT", email_subject=None, sms_message=None, data=None):
    create_notification(
        recipient=user,
        notification_type=notification_type,
        title=title,
        message=message,
        data=data or {},
    )
    from_email = getattr(settings, "DEFAULT_FROM_EMAIL", "") or getattr(settings, "EMAIL_HOST_USER", "")
    if user.email and from_email:
        try:
            send_mail(email_subject or title, message, from_email, [user.email], fail_silently=True)
        except Exception:
            pass
    if getattr(settings, "EBULKSMS_ENABLED", False) and getattr(user, "phone_number", ""):
        try:
            send_sms_ebulksms(msisdn=user.phone_number, message=sms_message or message)
        except Exception:
            pass


def create_loan_audit(*, loan: Loan, action: str, performed_by=None, amount=None, reason="", ip_address=None, metadata=None):
    return LoanAuditLog.objects.create(
        loan=loan,
        borrower=loan.borrower,
        performed_by=performed_by,
        action=action,
        amount=quantize_money(amount) if amount is not None else None,
        reason=reason or "",
        ip_address=ip_address,
        metadata=metadata or {},
    )


def submit_overdraft_request(*, borrower: User, requested_amount, reason="", ip_address=None):
    if borrower.user_type not in ["agent", "super_agent"]:
        raise LoanOverdraftError("Only agents and super agents can request overdraft.")
    if user_has_outstanding_loan(borrower):
        raise LoanOverdraftError("Outstanding overdraft must be cleared before a new request can be submitted.")
    pending_exists = Loan.objects.filter(borrower=borrower, status="pending").exists()
    if pending_exists:
        raise LoanOverdraftError("You already have a pending overdraft request.")

    requested_amount = quantize_money(requested_amount)
    if requested_amount <= 0:
        raise LoanOverdraftError("Requested amount must be greater than zero.")

    snapshot = build_qualification_snapshot(borrower)
    if not snapshot.can_submit_now:
        raise LoanOverdraftError(snapshot.blockers[0])
    if requested_amount > snapshot.qualified_amount:
        raise LoanOverdraftError("Requested amount cannot exceed the qualified loan amount.")

    approver = get_primary_approver_for_request(borrower)
    if not approver:
        raise LoanOverdraftError("No approver is configured for this loan request.")

    approval_level = "super_agent" if borrower.user_type == "agent" else "admin"
    loan_type = "agent_overdraft" if borrower.user_type == "agent" else "super_agent_overdraft"
    loan = Loan.objects.create(
        borrower=borrower,
        lender=approver,
        amount=Decimal("0.00"),
        requested_amount=requested_amount,
        qualified_amount=snapshot.qualified_amount,
        qualification_ticket_count=snapshot.ticket_count,
        qualification_deposit_volume=snapshot.deposit_total,
        outstanding_balance=Decimal("0.00"),
        status="pending",
        loan_type=loan_type,
        approval_level=approval_level,
        due_date=snapshot.due_at,
        request_reason=reason or "",
        workflow_snapshot={
            "request_open_at": snapshot.request_open_at.isoformat(),
            "due_at": snapshot.due_at.isoformat(),
            "blockers": snapshot.blockers,
        },
    )
    create_loan_audit(
        loan=loan,
        action="request_submitted",
        performed_by=borrower,
        amount=requested_amount,
        reason=reason,
        ip_address=ip_address,
        metadata={
            "qualified_amount": str(snapshot.qualified_amount),
            "ticket_count": snapshot.ticket_count,
            "deposit_total": str(snapshot.deposit_total),
        },
    )
    notify_loan_event(
        user=borrower,
        title="Loan Request Submitted",
        message="Your overdraft request has been submitted and is awaiting review.",
        notification_type="LOAN_REQUEST_SUBMITTED",
        data={"loan_id": loan.id},
    )
    notify_loan_event(
        user=approver,
        title="Loan Request Pending Review",
        message=f"{borrower.username or borrower.email} submitted an overdraft request of N{requested_amount} for your review.",
        notification_type="LOAN_REQUEST_PENDING_REVIEW",
        data={"loan_id": loan.id, "borrower_id": borrower.id},
    )
    return loan


def _approve_agent_loan(*, actor: User, loan: Loan, ip_address=None):
    funding_user = getattr(loan.borrower, "super_agent", None) or loan.lender
    if not funding_user or funding_user.id != actor.id:
        raise LoanOverdraftError("Only the mapped super agent can approve this overdraft request.")
    overdraft_wallet = get_or_create_overdraft_wallet(funding_user)
    overdraft_wallet = OverdraftWallet.objects.select_for_update().get(pk=overdraft_wallet.pk)
    amount = quantize_money(loan.requested_amount)
    if overdraft_wallet.current_balance < amount:
        raise LoanOverdraftError("Super agent overdraft wallet has insufficient balance.")

    borrower_wallet, _ = Wallet.objects.select_for_update().get_or_create(user=loan.borrower, defaults={"balance": Decimal("0.00")})
    tx_in = Transaction.objects.create(
        user=loan.borrower,
        initiating_user=actor,
        target_user=actor,
        transaction_type="wallet_transfer_in",
        amount=amount,
        is_successful=True,
        status="completed",
        description=f"Overdraft approved by {actor.username or actor.email}",
    )
    overdraft_wallet.apply_delta(
        amount=-amount,
        actor=actor,
        loan=loan,
        reference=f"loan:{loan.id}",
        reason=f"Approved overdraft for {loan.borrower.username or loan.borrower.email}",
        metadata={"source": "loan_approval"},
    )
    borrower_wallet.apply_delta(
        amount=amount,
        actor=actor,
        transaction_obj=tx_in,
        reference=str(loan.id),
        reason=tx_in.description,
        metadata={"source": "loan_approval", "loan_id": loan.id},
    )
    loan.overdraft_wallet = overdraft_wallet


def _approve_super_agent_loan(*, actor: User, loan: Loan):
    amount = quantize_money(loan.requested_amount)
    borrower_wallet, _ = Wallet.objects.select_for_update().get_or_create(user=loan.borrower, defaults={"balance": Decimal("0.00")})
    tx_in = Transaction.objects.create(
        user=loan.borrower,
        initiating_user=actor,
        target_user=loan.borrower,
        transaction_type="manual_credit",
        amount=amount,
        is_successful=True,
        status="completed",
        description=f"Admin overdraft approved by {actor.username or actor.email}",
    )
    borrower_wallet.apply_delta(
        amount=amount,
        actor=actor,
        transaction_obj=tx_in,
        reference=str(loan.id),
        reason=tx_in.description,
        metadata={"source": "loan_approval", "loan_id": loan.id, "company_funded": True},
    )


def approve_loan_request(*, actor: User, loan_id: int, ip_address=None):
    with transaction.atomic():
        loan = Loan.objects.select_for_update().select_related("borrower", "lender").get(id=loan_id)
        if loan.status != "pending":
            raise LoanOverdraftError("This loan request has already been processed.")

        if loan.approval_level == "super_agent":
            _approve_agent_loan(actor=actor, loan=loan, ip_address=ip_address)
        else:
            if not (actor.is_superuser or actor.user_type == "admin"):
                raise LoanOverdraftError("Only admin or superadmin can approve this loan request.")
            _approve_super_agent_loan(actor=actor, loan=loan)

        approved_amount = quantize_money(loan.requested_amount)
        loan.amount = approved_amount
        loan.outstanding_balance = approved_amount
        loan.status = "active"
        loan.approved_by = actor
        loan.approved_at = timezone.now()
        loan.save(
            update_fields=[
                "amount",
                "outstanding_balance",
                "status",
                "approved_by",
                "approved_at",
                "overdraft_wallet",
                "updated_at",
            ]
        )
        create_loan_audit(
            loan=loan,
            action="approved",
            performed_by=actor,
            amount=approved_amount,
            ip_address=ip_address,
            metadata={"approval_level": loan.approval_level},
        )
        notify_loan_event(
            user=loan.borrower,
            title="Loan Approved",
            message=f"Your overdraft request of N{approved_amount} has been approved.",
            notification_type="LOAN_APPROVED",
            data={"loan_id": loan.id},
        )
        return loan


def reject_loan_request(*, actor: User, loan_id: int, reason: str, ip_address=None):
    reason = (reason or "").strip()
    if not reason:
        raise LoanOverdraftError("Reason for rejection is required.")
    with transaction.atomic():
        loan = Loan.objects.select_for_update().select_related("borrower").get(id=loan_id)
        if loan.status != "pending":
            raise LoanOverdraftError("This loan request has already been processed.")
        if loan.approval_level == "super_agent" and actor.id != loan.lender_id:
            raise LoanOverdraftError("Only the mapped super agent can reject this request.")
        if loan.approval_level == "admin" and not (actor.is_superuser or actor.user_type == "admin"):
            raise LoanOverdraftError("Only admin or superadmin can reject this request.")
        loan.status = "rejected"
        loan.rejected_by = actor
        loan.rejected_at = timezone.now()
        loan.rejection_reason = reason
        loan.save(update_fields=["status", "rejected_by", "rejected_at", "rejection_reason", "updated_at"])
        create_loan_audit(
            loan=loan,
            action="rejected",
            performed_by=actor,
            amount=loan.requested_amount,
            reason=reason,
            ip_address=ip_address,
        )
        notify_loan_event(
            user=loan.borrower,
            title="Loan Rejected",
            message=reason,
            notification_type="LOAN_REJECTED",
            data={"loan_id": loan.id},
        )
        return loan


def create_manual_overdraft(*, actor: User, borrower: User, amount, reason="", ip_address=None):
    amount = quantize_money(amount)
    if amount <= 0:
        raise LoanOverdraftError("Amount must be greater than zero.")
    with transaction.atomic():
        approver = get_primary_approver_for_request(borrower) or actor
        loan = Loan.objects.create(
            borrower=borrower,
            lender=approver,
            amount=amount,
            requested_amount=amount,
            qualified_amount=amount,
            outstanding_balance=amount,
            status="active",
            loan_type="manual_overdraft",
            approval_level="admin",
            approved_by=actor,
            approved_at=timezone.now(),
            due_date=_next_due_datetime(),
            request_reason=reason or "Manual overdraft assignment",
            manual_assignment=True,
        )
        if borrower.user_type == "agent" and getattr(borrower, "super_agent", None):
            overdraft_wallet = get_or_create_overdraft_wallet(borrower.super_agent)
            overdraft_wallet = OverdraftWallet.objects.select_for_update().get(pk=overdraft_wallet.pk)
            if overdraft_wallet.current_balance >= amount:
                overdraft_wallet.apply_delta(
                    amount=-amount,
                    actor=actor,
                    loan=loan,
                    reference=f"loan:{loan.id}",
                    reason=f"Manual overdraft assignment for {borrower.username or borrower.email}",
                    metadata={"source": "manual_overdraft"},
                )
                loan.overdraft_wallet = overdraft_wallet
                loan.lender = borrower.super_agent
                loan.save(update_fields=["overdraft_wallet", "lender", "updated_at"])
        borrower_wallet, _ = Wallet.objects.select_for_update().get_or_create(user=borrower, defaults={"balance": Decimal("0.00")})
        tx = Transaction.objects.create(
            user=borrower,
            initiating_user=actor,
            target_user=borrower,
            transaction_type="manual_credit",
            amount=amount,
            is_successful=True,
            status="completed",
            description=f"Manual overdraft assignment: {reason}".strip(": "),
        )
        borrower_wallet.apply_delta(
            amount=amount,
            actor=actor,
            transaction_obj=tx,
            reference=str(loan.id),
            reason=tx.description,
            metadata={"source": "manual_overdraft", "loan_id": loan.id},
        )
        create_loan_audit(
            loan=loan,
            action="manual_assigned",
            performed_by=actor,
            amount=amount,
            reason=reason,
            ip_address=ip_address,
            metadata={"wallet_credited": True},
        )
        notify_loan_event(
            user=borrower,
            title="Manual Overdraft Assigned",
            message=f"An overdraft of N{amount} has been assigned to your account and added to your wallet balance.",
            notification_type="LOAN_MANUAL_ASSIGNED",
            data={"loan_id": loan.id},
        )
        return loan


def _loan_lock_targets(borrower: User):
    targets = [borrower]
    if borrower.user_type == "agent":
        targets.extend(User.objects.filter(user_type="cashier", agent=borrower))
    elif borrower.user_type == "super_agent":
        agents = list(User.objects.filter(user_type="agent", super_agent=borrower))
        targets.extend(agents)
        if agents:
            targets.extend(User.objects.filter(user_type="cashier", agent__in=agents))
        targets.extend(User.objects.filter(user_type="cashier", super_agent=borrower, agent__isnull=True))
    return targets


def get_borrower_network_wallet_balance(borrower: User) -> Decimal:
    target_ids = [target.id for target in _loan_lock_targets(borrower)]
    total = (
        Wallet.objects.filter(user_id__in=target_ids).aggregate(total=Sum("balance"))["total"]
        or Decimal("0.00")
    )
    return quantize_money(total)


def loan_has_active_lock_override(loan: Loan) -> bool:
    snapshot = getattr(loan, "workflow_snapshot", {}) or {}
    return bool(snapshot.get("lock_override_active"))


def loan_lock_override_details(loan: Loan) -> dict:
    snapshot = getattr(loan, "workflow_snapshot", {}) or {}
    return {
        "active": bool(snapshot.get("lock_override_active")),
        "reason": (snapshot.get("lock_override_reason") or "").strip(),
        "performed_at": snapshot.get("lock_override_at") or "",
        "performed_by_id": snapshot.get("lock_override_by_id"),
        "performed_by_label": snapshot.get("lock_override_by_label") or "",
    }


def _clear_loan_lock_override_state(loan: Loan, *, actor: User | None = None, reason: str = ""):
    snapshot = dict(getattr(loan, "workflow_snapshot", {}) or {})
    if not snapshot.get("lock_override_active"):
        return
    snapshot.update(
        {
            "lock_override_active": False,
            "lock_override_cleared_at": timezone.localtime(timezone.now()).strftime("%Y-%m-%d %H:%M:%S"),
            "lock_override_cleared_by_id": getattr(actor, "id", None),
            "lock_override_cleared_by_label": (
                (getattr(actor, "username", "") or getattr(actor, "email", "") or "").strip()
                or (f"user#{actor.id}" if getattr(actor, "id", None) else "")
            ),
            "lock_override_cleared_reason": (reason or "").strip(),
        }
    )
    loan.workflow_snapshot = snapshot
    loan.save(update_fields=["workflow_snapshot", "updated_at"])


@transaction.atomic
def override_unlock_loan_without_payment(*, actor: User, loan_id: int, reason: str, ip_address=None):
    if not getattr(actor, "is_authenticated", False) or not getattr(actor, "is_superuser", False):
        raise LoanOverdraftError("Only Super Admin can override unlock overdue overdraft accounts without payment.")

    loan = (
        Loan.objects.select_for_update()
        .select_related("borrower", "lender")
        .get(id=loan_id)
    )
    borrower = loan.borrower
    reason = (reason or "").strip()
    if not reason:
        raise LoanOverdraftError("Override unlock reason is required.")
    if loan.outstanding_balance <= Decimal("0.00"):
        raise LoanOverdraftError("This loan has no outstanding balance.")
    if loan.due_date and loan.due_date >= timezone.now() and not (loan.account_locked_due_to_default or getattr(borrower, "is_locked", False)):
        raise LoanOverdraftError("Override unlock is available only for overdue or currently locked loan accounts.")
    if loan_has_active_lock_override(loan):
        raise LoanOverdraftError("An override unlock is already active for this loan.")

    unlocked_targets = []
    skipped_targets = []
    override_remark = f"Overdraft lock override approved without repayment for loan #{loan.id}. Reason: {reason}"

    for target in _loan_lock_targets(borrower):
        if not getattr(target, "is_locked", False):
            skipped_targets.append(target.id)
            continue
        current_reason = (getattr(target, "lock_reason", "") or "").strip()
        if current_reason and "overdraft/loan obligation" not in current_reason.lower():
            skipped_targets.append(target.id)
            continue
        target.is_locked = False
        target.locked_at = None
        target.lock_reason = ""
        target.failed_login_attempts = 0
        target.last_failed_login = None
        target.save(update_fields=["is_locked", "locked_at", "lock_reason", "failed_login_attempts", "last_failed_login"])
        LoginAttempt.objects.create(
            user=target,
            username_attempted=target.email,
            ip_address=ip_address,
            status="unlocked",
        )
        AccountLockAuditLog.objects.create(
            locked_user=target,
            reviewed_by=actor,
            lock_reason=current_reason or LOAN_LOCK_REASON,
            action="unlocked",
            remarks=override_remark,
        )
        unlocked_targets.append(target)

    snapshot = dict(getattr(loan, "workflow_snapshot", {}) or {})
    snapshot.update(
        {
            "lock_override_active": True,
            "lock_override_reason": reason,
            "lock_override_at": timezone.localtime(timezone.now()).strftime("%Y-%m-%d %H:%M:%S"),
            "lock_override_by_id": actor.id,
            "lock_override_by_label": actor.username or actor.email or f"user#{actor.id}",
            "lock_override_unlock_count": len(unlocked_targets),
        }
    )
    loan.workflow_snapshot = snapshot
    loan.account_locked_due_to_default = False
    loan.save(update_fields=["workflow_snapshot", "account_locked_due_to_default", "updated_at"])

    audit_metadata = {
        "override_type": "unlock_without_payment",
        "override_active": True,
        "unlocked_target_ids": [target.id for target in unlocked_targets],
        "skipped_target_ids": skipped_targets,
        "preserved_outstanding_balance": f"{quantize_money(loan.outstanding_balance):.2f}",
    }
    create_loan_audit(
        loan=loan,
        action="override",
        performed_by=actor,
        amount=loan.outstanding_balance,
        reason=reason,
        ip_address=ip_address,
        metadata=audit_metadata,
    )
    create_loan_audit(
        loan=loan,
        action="account_unlocked",
        performed_by=actor,
        amount=loan.outstanding_balance,
        reason=override_remark,
        ip_address=ip_address,
        metadata=audit_metadata,
    )
    notify_loan_event(
        user=borrower,
        title="Account Unlocked By Override",
        message="Your account access has been restored by Super Admin override. The outstanding overdraft remains unpaid.",
        notification_type="LOAN_ACCOUNT_UNLOCKED",
        data={"loan_id": loan.id, "override_unlock": True},
    )
    return loan, unlocked_targets


def _auto_unlock_loan_targets(borrower: User):
    targets = _loan_lock_targets(borrower)
    unlocked_any = False
    for target in targets:
        if not getattr(target, "is_locked", False):
            continue
        reason = (getattr(target, "lock_reason", "") or "").strip()
        if "overdraft/loan obligation" not in reason.lower():
            continue
        target.is_locked = False
        target.locked_at = None
        target.lock_reason = ""
        target.failed_login_attempts = 0
        target.last_failed_login = None
        target.save(update_fields=["is_locked", "locked_at", "lock_reason", "failed_login_attempts", "last_failed_login"])
        unlocked_any = True
        AccountLockAuditLog.objects.create(
            locked_user=target,
            reviewed_by=borrower if getattr(borrower, "is_authenticated", False) else None,
            action="unlocked",
            remarks="Loan cleared automatically; account unlocked.",
        )
    if unlocked_any:
        settled_loans = list(
            Loan.objects.filter(
                borrower=borrower,
                status="settled",
                account_locked_due_to_default=True,
                account_unlocked_after_settlement=False,
            )
        )
        for loan in settled_loans:
            loan.account_unlocked_after_settlement = True
            loan.save(update_fields=["account_unlocked_after_settlement", "updated_at"])
            create_loan_audit(
                loan=loan,
                action="account_unlocked",
                performed_by=borrower if getattr(borrower, "is_authenticated", False) else None,
                amount=loan.repaid_amount,
                reason="Loan cleared automatically; account unlocked.",
            )
        notify_loan_event(
            user=borrower,
            title="Account Unlocked",
            message="Your overdraft has been cleared and account access has been restored.",
            notification_type="LOAN_ACCOUNT_UNLOCKED",
            data={"borrower_id": borrower.id, "settled_loan_ids": [loan.id for loan in settled_loans]},
        )


def _reassess_borrower_overdraft_lock_state(borrower: User):
    outstanding_loans = get_user_outstanding_loans(borrower)
    if not outstanding_loans.exists():
        _auto_unlock_loan_targets(borrower)
        return

    now = timezone.now()
    overdue_loans = outstanding_loans.filter(Q(status__in=["overdue", "defaulted"]) | Q(due_date__lt=now))
    if overdue_loans.exists():
        for overdue_loan in overdue_loans:
            if overdue_loan.status != "overdue":
                overdue_loan.status = "overdue"
                overdue_loan.save(update_fields=["status", "updated_at"])
            _lock_defaulting_borrower(loan=overdue_loan)
        return

    unlocked_any = False
    for target in _loan_lock_targets(borrower):
        if not getattr(target, "is_locked", False):
            continue
        reason = (getattr(target, "lock_reason", "") or "").strip()
        if "overdraft/loan obligation" not in reason.lower():
            continue
        target.is_locked = False
        target.locked_at = None
        target.lock_reason = ""
        target.failed_login_attempts = 0
        target.last_failed_login = None
        target.save(update_fields=["is_locked", "locked_at", "lock_reason", "failed_login_attempts", "last_failed_login"])
        unlocked_any = True
        AccountLockAuditLog.objects.create(
            locked_user=target,
            reviewed_by=borrower if getattr(borrower, "is_authenticated", False) else None,
            action="unlocked",
            remarks="Overdraft is not overdue; account unlocked.",
        )

    if unlocked_any:
        Loan.objects.filter(
            borrower=borrower,
            status__in=["active", "overdue"],
            outstanding_balance__gt=Decimal("0.00"),
        ).update(account_locked_due_to_default=False, updated_at=timezone.now())


def reassess_borrower_overdraft_lock_state(borrower: User):
    _reassess_borrower_overdraft_lock_state(borrower)


def _lock_defaulting_borrower(*, loan: Loan, actor: User | None = None, ip_address=None, remarks_override: str = ""):
    if loan.account_locked_due_to_default:
        return
    if loan_has_active_lock_override(loan):
        return
    for target in _loan_lock_targets(loan.borrower):
        if getattr(target, "is_locked", False):
            continue
        target.is_locked = True
        target.locked_at = timezone.now()
        target.lock_reason = LOAN_LOCK_REASON
        target.save(update_fields=["is_locked", "locked_at", "lock_reason"])
        logout_user_from_all_active_sessions(target)
        AccountLockAuditLog.objects.create(
            locked_user=target,
            locked_by=actor if getattr(actor, "is_authenticated", False) else None,
            action="locked",
            lock_reason=LOAN_LOCK_REASON,
            remarks=remarks_override or f"Locked due to overdue overdraft loan #{loan.id}.",
        )
    loan.account_locked_due_to_default = True
    loan.save(update_fields=["account_locked_due_to_default", "updated_at"])
    create_loan_audit(
        loan=loan,
        action="account_locked",
        performed_by=actor if getattr(actor, "is_authenticated", False) else None,
        amount=loan.outstanding_balance,
        reason=remarks_override or LOAN_LOCK_REASON,
        ip_address=ip_address,
    )
    notify_loan_event(
        user=loan.borrower,
        title="Account Disabled",
        message=LOAN_LOCK_REASON,
        notification_type="LOAN_ACCOUNT_LOCKED",
        data={"loan_id": loan.id},
    )


@transaction.atomic
def relock_loan_after_override(*, actor: User, loan_id: int, reason: str, ip_address=None):
    if not getattr(actor, "is_authenticated", False) or not getattr(actor, "is_superuser", False):
        raise LoanOverdraftError("Only Super Admin can re-lock overdue overdraft accounts after override unlock.")

    loan = (
        Loan.objects.select_for_update()
        .select_related("borrower", "lender")
        .get(id=loan_id)
    )
    reason = (reason or "").strip()
    if not reason:
        raise LoanOverdraftError("Re-lock reason is required.")
    if loan.outstanding_balance <= Decimal("0.00"):
        raise LoanOverdraftError("This loan has no outstanding balance.")
    is_past_due = bool(loan.due_date and loan.due_date < timezone.now())
    override_active = loan_has_active_lock_override(loan)
    targets = list(_loan_lock_targets(loan.borrower))
    is_currently_locked = bool(
        loan.account_locked_due_to_default
        and all(getattr(target, "is_locked", False) for target in targets)
    )
    if not is_past_due:
        raise LoanOverdraftError("Re-lock is available only for overdue overdrafts.")
    if not override_active and is_currently_locked:
        raise LoanOverdraftError("This overdue loan is already locked.")

    relock_remark = f"Re-locked after override unlock for loan #{loan.id}. Reason: {reason}"
    snapshot = dict(getattr(loan, "workflow_snapshot", {}) or {})
    previous_override_reason = (snapshot.get("lock_override_reason") or "").strip()
    snapshot.update(
        {
            "lock_override_active": False,
            "lock_relock_reason": reason,
            "lock_relock_at": timezone.localtime(timezone.now()).strftime("%Y-%m-%d %H:%M:%S"),
            "lock_relock_by_id": actor.id,
            "lock_relock_by_label": actor.username or actor.email or f"user#{actor.id}",
        }
    )
    loan.workflow_snapshot = snapshot
    loan.account_locked_due_to_default = False
    loan.status = "overdue"
    loan.save(update_fields=["workflow_snapshot", "account_locked_due_to_default", "status", "updated_at"])

    _lock_defaulting_borrower(
        loan=loan,
        actor=actor,
        ip_address=ip_address,
        remarks_override=relock_remark,
    )

    locked_targets = [
        target for target in _loan_lock_targets(loan.borrower)
        if getattr(target, "is_locked", False) and (getattr(target, "lock_reason", "") or "").strip() == LOAN_LOCK_REASON
    ]
    create_loan_audit(
        loan=loan,
        action="override",
        performed_by=actor,
        amount=loan.outstanding_balance,
        reason=reason,
        ip_address=ip_address,
        metadata={
            "override_type": "relock_after_override",
            "previous_override_reason": previous_override_reason,
            "relocked_target_ids": [target.id for target in locked_targets],
        },
    )
    return loan, locked_targets


def _apply_amount_to_outstanding_loans(*, user: User, amount, source: str, actor=None, transaction_obj=None, reference="", reason="", metadata=None):
    amount = quantize_money(amount)
    metadata = metadata or {}
    repaid_total = Decimal("0.00")
    remaining = amount
    repaid_loans = []

    loans = list(get_user_outstanding_loans(user).select_for_update())
    for loan in loans:
        if remaining <= 0:
            break
        pay_amount = min(remaining, quantize_money(loan.outstanding_balance))
        if pay_amount <= 0:
            continue

        loan.outstanding_balance = quantize_money(loan.outstanding_balance - pay_amount)
        loan.repaid_amount = quantize_money((loan.repaid_amount or Decimal("0.00")) + pay_amount)
        if loan.outstanding_balance <= 0:
            loan.outstanding_balance = Decimal("0.00")
            loan.status = "settled"
            loan.settled_at = timezone.now()
        elif loan.due_date and loan.due_date < timezone.now():
            loan.status = "overdue"
        loan.save(update_fields=["outstanding_balance", "repaid_amount", "status", "settled_at", "updated_at"])

        LoanRepayment.objects.create(
            loan=loan,
            borrower=user,
            amount=pay_amount,
            source=source,
            source_transaction=transaction_obj,
            recorded_by=actor if getattr(actor, "is_authenticated", False) else None,
            note=reason or "",
            metadata={**metadata, "reference": reference},
        )
        create_loan_audit(
            loan=loan,
            action="repayment_received",
            performed_by=actor if getattr(actor, "is_authenticated", False) else None,
            amount=pay_amount,
            reason=reason or f"Overdraft remittance from {source}",
            metadata={"source": source, "reference": reference},
        )
        if loan.overdraft_wallet_id:
            overdraft_wallet = OverdraftWallet.objects.select_for_update().get(pk=loan.overdraft_wallet_id)
            overdraft_wallet.apply_delta(
                amount=pay_amount,
                actor=actor if getattr(actor, "is_authenticated", False) else None,
                loan=loan,
                reference=reference or str(transaction_obj.id if transaction_obj else loan.id),
                reason=f"Repayment recovered for loan #{loan.id}",
                metadata={"source": source},
            )
        remaining = quantize_money(remaining - pay_amount)
        repaid_total = quantize_money(repaid_total + pay_amount)
        repaid_loans.append({"loan_id": loan.id, "amount": str(pay_amount), "settled": loan.status == "settled"})

        if loan.status == "settled":
            create_loan_audit(
                loan=loan,
                action="loan_cleared",
                performed_by=actor if getattr(actor, "is_authenticated", False) else None,
                amount=pay_amount,
                reason="Loan fully settled.",
            )
            notify_loan_event(
                user=user,
                title="Loan Cleared",
                message="Your overdraft has been fully settled.",
                notification_type="LOAN_CLEARED",
                data={"loan_id": loan.id},
            )

    return {
        "repaid_amount": repaid_total,
        "remaining_amount": remaining,
        "repaid_loans": repaid_loans,
    }


def _apply_amount_to_single_loan(*, loan: Loan, amount, source: str, actor=None, transaction_obj=None, reference="", reason="", metadata=None):
    amount = quantize_money(amount)
    metadata = metadata or {}
    if amount <= Decimal("0.00"):
        return {
            "repaid_amount": Decimal("0.00"),
            "remaining_amount": Decimal("0.00"),
            "repaid_loans": [],
        }

    pay_amount = min(amount, quantize_money(loan.outstanding_balance))
    if pay_amount <= Decimal("0.00"):
        return {
            "repaid_amount": Decimal("0.00"),
            "remaining_amount": amount,
            "repaid_loans": [],
        }

    loan.outstanding_balance = quantize_money(loan.outstanding_balance - pay_amount)
    loan.repaid_amount = quantize_money((loan.repaid_amount or Decimal("0.00")) + pay_amount)
    if loan.outstanding_balance <= Decimal("0.00"):
        loan.outstanding_balance = Decimal("0.00")
        loan.status = "settled"
        loan.settled_at = timezone.now()
    elif loan.due_date and loan.due_date < timezone.now():
        loan.status = "overdue"
    loan.save(update_fields=["outstanding_balance", "repaid_amount", "status", "settled_at", "updated_at"])

    LoanRepayment.objects.create(
        loan=loan,
        borrower=loan.borrower,
        amount=pay_amount,
        source=source,
        source_transaction=transaction_obj,
        recorded_by=actor if getattr(actor, "is_authenticated", False) else None,
        note=reason or "",
        metadata={**metadata, "reference": reference},
    )
    create_loan_audit(
        loan=loan,
        action="repayment_received",
        performed_by=actor if getattr(actor, "is_authenticated", False) else None,
        amount=pay_amount,
        reason=reason or f"Overdraft remittance from {source}",
        metadata={**metadata, "source": source, "reference": reference},
    )
    if loan.overdraft_wallet_id:
        overdraft_wallet = OverdraftWallet.objects.select_for_update().get(pk=loan.overdraft_wallet_id)
        overdraft_wallet.apply_delta(
            amount=pay_amount,
            actor=actor if getattr(actor, "is_authenticated", False) else None,
            loan=loan,
            reference=reference or str(transaction_obj.id if transaction_obj else loan.id),
            reason=f"Repayment recovered for loan #{loan.id}",
            metadata={"source": source},
        )

    repaid_loans = [{"loan_id": loan.id, "amount": str(pay_amount), "settled": loan.status == "settled"}]
    if loan.status == "settled":
        create_loan_audit(
            loan=loan,
            action="loan_cleared",
            performed_by=actor if getattr(actor, "is_authenticated", False) else None,
            amount=pay_amount,
            reason="Loan fully settled.",
            metadata={**metadata, "source": source, "reference": reference},
        )
        notify_loan_event(
            user=loan.borrower,
            title="Loan Cleared",
            message="Your overdraft has been fully settled.",
            notification_type="LOAN_CLEARED",
            data={"loan_id": loan.id},
        )

    return {
        "repaid_amount": pay_amount,
        "remaining_amount": quantize_money(amount - pay_amount),
        "repaid_loans": repaid_loans,
    }


def apply_repayment_and_credit_wallet(*, user: User, amount, source: str, actor=None, transaction_obj=None, reference="", reason="", metadata=None):
    amount = quantize_money(amount)
    if amount <= 0:
        return {"repaid_amount": Decimal("0.00"), "wallet_credit_amount": Decimal("0.00"), "pending_credit_amount": Decimal("0.00")}

    metadata = metadata or {}

    with transaction.atomic():
        wallet, _ = Wallet.objects.select_for_update().get_or_create(user=user, defaults={"balance": Decimal("0.00")})
        loans = list(get_user_outstanding_loans(user).select_for_update())
        if loans:
            pending_credit = LoanPendingCredit.objects.create(
                borrower=user,
                source=source,
                source_transaction=transaction_obj,
                amount=amount,
                remaining_amount=amount,
                recorded_by=actor if getattr(actor, "is_authenticated", False) else None,
                note=reason or "",
                metadata={
                    **metadata,
                    "reference": reference,
                    "qualified_deposit_excess_amount": "0.00",
                },
            )
            for loan in loans:
                create_loan_audit(
                    loan=loan,
                    action="credit_reserved",
                    performed_by=actor if getattr(actor, "is_authenticated", False) else None,
                    amount=amount,
                    reason=reason or f"Incoming credit reserved for manual overdraft remittance from {source}",
                    metadata={"source": source, "reference": reference, "pending_credit_id": pending_credit.id},
                )
            return {"repaid_amount": Decimal("0.00"), "wallet_credit_amount": Decimal("0.00"), "pending_credit_amount": amount}

        if amount > 0:
            wallet.apply_delta(
                amount=amount,
                actor=actor if getattr(actor, "is_authenticated", False) else None,
                transaction_obj=transaction_obj,
                reference=reference,
                reason=reason,
                metadata=metadata,
            )
    return {"repaid_amount": Decimal("0.00"), "wallet_credit_amount": amount, "pending_credit_amount": Decimal("0.00")}


def remit_overdraft_pending_credit(*, user: User, actor=None, ip_address=None):
    with transaction.atomic():
        wallet, _ = Wallet.objects.select_for_update().get_or_create(user=user, defaults={"balance": Decimal("0.00")})
        outstanding_loans = list(get_user_outstanding_loans(user).select_for_update())
        pending_entries = list(
            LoanPendingCredit.objects.select_for_update()
            .filter(borrower=user, remaining_amount__gt=Decimal("0.00"))
            .order_by("created_at", "id")
        )
        if not pending_entries:
            raise LoanOverdraftError("There is no pending credit available for overdraft remittance.")
        if not outstanding_loans:
            raise LoanOverdraftError("There is no outstanding overdraft to remit.")

        total_available = quantize_money(sum((entry.remaining_amount for entry in pending_entries), Decimal("0.00")))
        total_outstanding = quantize_money(sum((loan.outstanding_balance for loan in outstanding_loans), Decimal("0.00")))
        total_wallet_resident = quantize_money(sum((get_loan_wallet_resident_amount(loan) for loan in outstanding_loans), Decimal("0.00")))
        repayment_target_amount = quantize_money(min(total_available, total_outstanding))
        wallet_principal_clawback = quantize_money(min(wallet.balance, total_wallet_resident, repayment_target_amount))

        if wallet_principal_clawback > 0:
            wallet.apply_delta(
                amount=-wallet_principal_clawback,
                actor=actor if getattr(actor, "is_authenticated", False) else None,
                reference=f"loan-remit-principal:{user.id}",
                reason="Wallet-backed overdraft principal removed during remittance.",
                metadata={
                    "pending_credit_ids": [entry.id for entry in pending_entries],
                    "source": "loan_manual_remit_clawback",
                },
            )

        result = _apply_amount_to_outstanding_loans(
            user=user,
            amount=total_available,
            source="manual_settlement",
            actor=actor,
            reason="Manual overdraft remittance from reserved credit.",
            metadata={"pending_credit_ids": [entry.id for entry in pending_entries]},
        )
        wallet_credit_amount = quantize_money(result["remaining_amount"])

        if wallet_credit_amount > 0:
            wallet.apply_delta(
                amount=wallet_credit_amount,
                actor=actor if getattr(actor, "is_authenticated", False) else None,
                reference=f"loan-remit:{user.id}",
                reason="Excess reserved credit released to wallet after overdraft remittance.",
                metadata={"pending_credit_ids": [entry.id for entry in pending_entries], "source": "loan_manual_remit"},
            )

        remaining_repaid_to_allocate = quantize_money(result["repaid_amount"])
        for entry in pending_entries:
            entry_amount = quantize_money(entry.remaining_amount)
            repaid_from_entry = quantize_money(min(entry_amount, remaining_repaid_to_allocate))
            remaining_repaid_to_allocate = quantize_money(remaining_repaid_to_allocate - repaid_from_entry)
            excess_from_entry = quantize_money(entry_amount - repaid_from_entry)
            entry_metadata = dict(entry.metadata or {})
            if entry.source == "gateway_deposit":
                entry_metadata["qualified_deposit_excess_amount"] = str(excess_from_entry)
            else:
                entry_metadata["qualified_deposit_excess_amount"] = "0.00"
            entry.remaining_amount = Decimal("0.00")
            entry.processed_at = timezone.now()
            entry.metadata = entry_metadata
            entry.save(update_fields=["remaining_amount", "processed_at", "metadata", "updated_at"])

    repaid_total = quantize_money(result["repaid_amount"])
    if repaid_total > 0:
        notify_loan_event(
            user=user,
            title="Repayment Received",
            message=f"N{repaid_total} has been applied to your outstanding overdraft balance.",
            notification_type="LOAN_REPAYMENT_RECEIVED",
            data={"borrower_id": user.id, "repaid_amount": str(repaid_total), "loans": result["repaid_loans"]},
        )

    _reassess_borrower_overdraft_lock_state(user)

    return {
        "repaid_amount": repaid_total,
        "wallet_credit_amount": wallet_credit_amount,
        "pending_credit_amount": Decimal("0.00"),
    }


@transaction.atomic
def clear_overdraft_from_admin(*, actor: User, loan_id: int, ip_address=None):
    loan = (
        Loan.objects.select_for_update()
        .select_related("borrower", "lender")
        .get(id=loan_id)
    )
    if loan.outstanding_balance <= Decimal("0.00"):
        raise LoanOverdraftError("This overdraft is already cleared.")

    amount_cleared = quantize_money(loan.outstanding_balance)
    _clear_loan_lock_override_state(
        loan,
        actor=actor,
        reason=f"Cleared manually from loan center for loan #{loan.id}.",
    )
    loan.outstanding_balance = Decimal("0.00")
    loan.status = "settled"
    loan.settled_at = timezone.now()
    loan.save(update_fields=["outstanding_balance", "status", "settled_at", "updated_at"])
    create_loan_audit(
        loan=loan,
        action="loan_cleared",
        performed_by=actor if getattr(actor, "is_authenticated", False) else None,
        amount=amount_cleared,
        reason="Overdraft cleared manually from loan center.",
        ip_address=ip_address,
        metadata={"clear_method": "admin_clear", "loan_id": loan.id},
    )
    notify_loan_event(
        user=loan.borrower,
        title="Overdraft Cleared",
        message="Your overdraft has been cleared administratively and your outstanding balance is now zero.",
        notification_type="LOAN_CLEARED",
        data={"loan_id": loan.id, "clear_method": "admin_clear"},
    )
    _reassess_borrower_overdraft_lock_state(loan.borrower)
    return loan, amount_cleared


@transaction.atomic
def recall_overdraft_from_wallets(*, actor: User, loan_id: int, ip_address=None):
    loan = (
        Loan.objects.select_for_update()
        .select_related("borrower", "lender")
        .get(id=loan_id)
    )
    if loan.outstanding_balance <= Decimal("0.00"):
        raise LoanOverdraftError("This overdraft is already cleared.")

    borrower = loan.borrower
    recall_reference = f"loan-recall:{loan.id}"
    target_users = _loan_lock_targets(borrower)
    recalled_total = Decimal("0.00")
    recalled_from_users = []

    for target in target_users:
        wallet, _ = Wallet.objects.select_for_update().get_or_create(user=target, defaults={"balance": Decimal("0.00")})
        available = quantize_money(max(Decimal("0.00"), wallet.balance or Decimal("0.00")))
        remaining_needed = quantize_money(loan.outstanding_balance - recalled_total)
        if available <= Decimal("0.00") or remaining_needed <= Decimal("0.00"):
            continue
        debit_amount = quantize_money(min(available, remaining_needed))
        if debit_amount <= Decimal("0.00"):
            continue

        description = (
            f"Overdraft recall for loan #{loan.id} belonging to "
            f"{borrower.username or borrower.email}"
        )
        tx = Transaction.objects.create(
            user=target,
            initiating_user=actor if getattr(actor, "is_authenticated", False) else None,
            target_user=borrower,
            transaction_type="account_user_debit",
            amount=debit_amount,
            is_successful=True,
            status="completed",
            description=description,
        )
        wallet.apply_delta(
            amount=-debit_amount,
            actor=actor if getattr(actor, "is_authenticated", False) else None,
            transaction_obj=tx,
            reference=recall_reference,
            reason=description,
            metadata={
                "source": "loan_manual_recall",
                "loan_id": loan.id,
                "borrower_id": borrower.id,
            },
        )
        recalled_total = quantize_money(recalled_total + debit_amount)
        recalled_from_users.append(
            {
                "user_id": target.id,
                "label": target.username or target.email or f"user#{target.id}",
                "amount": str(debit_amount),
            }
        )

    if recalled_total <= Decimal("0.00"):
        raise LoanOverdraftError("There is no available wallet balance to recall against this overdraft.")

    _clear_loan_lock_override_state(
        loan,
        actor=actor,
        reason=f"Recall overdraft executed from loan center for loan #{loan.id}.",
    )
    result = _apply_amount_to_single_loan(
        loan=loan,
        amount=recalled_total,
        source="manual_settlement",
        actor=actor,
        reference=recall_reference,
        reason="Overdraft recalled from borrower/downline wallet balances.",
        metadata={"recalled_from_users": recalled_from_users, "admin_recall": True},
    )
    _reassess_borrower_overdraft_lock_state(borrower)
    return loan, {
        "recalled_amount": quantize_money(result["repaid_amount"]),
        "remaining_outstanding": quantize_money(loan.outstanding_balance),
        "fully_settled": loan.outstanding_balance <= Decimal("0.00"),
        "recalled_from_users": recalled_from_users,
    }


def enforce_due_loans(reference_dt=None):
    now = reference_dt or timezone.now()
    overdue_loans = Loan.objects.filter(
        status__in=["active", "overdue", "defaulted"],
        outstanding_balance__gt=Decimal("0.00"),
        due_date__lt=now,
    ).select_related("borrower")
    processed = 0
    for loan in overdue_loans:
        if loan.status != "overdue":
            loan.status = "overdue"
            loan.save(update_fields=["status", "updated_at"])
        _lock_defaulting_borrower(loan=loan)
        processed += 1
    return processed


def finance_overdue_rows():
    now = timezone.now()
    rows = []
    for loan in Loan.objects.filter(
        status__in=["active", "overdue", "defaulted"],
        outstanding_balance__gt=Decimal("0.00"),
    ).select_related("borrower", "lender").order_by("due_date", "-created_at"):
        days_overdue = 0
        if loan.due_date and loan.due_date < now:
            days_overdue = (timezone.localtime(now).date() - timezone.localtime(loan.due_date).date()).days
        rows.append(
            {
                "loan": loan,
                "username": loan.borrower.username or loan.borrower.email,
                "role": loan.borrower.get_user_type_display(),
                "outstanding_amount": loan.outstanding_balance,
                "due_date": loan.due_date,
                "withdrawal_disabled": user_has_outstanding_loan(loan.borrower),
                "account_locked": bool(getattr(loan.borrower, "is_locked", False)),
                "days_overdue": max(days_overdue, 0),
            }
        )
    return rows
