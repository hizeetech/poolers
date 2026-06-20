import itertools
import math
import os
import re
import sys
import time
import threading
import traceback
import secrets
import smtplib
from urllib.parse import urlencode
from functools import wraps
from types import SimpleNamespace
from django.core.mail import send_mail
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib import messages
from django.conf import settings
from django.apps import apps
from django.db.models import Sum, Q, Case, When, F, DecimalField, Value, IntegerField, Count, OuterRef, Subquery, Max, Prefetch
from django.db.models.functions import Cast, Coalesce, TruncDate
from django.db import transaction as db_transaction
from django.db.utils import OperationalError, ProgrammingError
from django.utils import timezone
from datetime import timedelta, date, datetime
import logging
import requests # For Paystack API calls
import json
from django.http import JsonResponse, HttpResponse, Http404, HttpResponseForbidden, HttpResponseBadRequest, QueryDict
from django.views.decorators.csrf import csrf_exempt, ensure_csrf_cookie
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST
from django.utils.crypto import get_random_string
from django.template.loader import render_to_string
from django.utils.html import strip_tags
from decimal import Decimal, InvalidOperation # Import InvalidOperation
import uuid # For UUIDField
from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
from django.urls import reverse # Import reverse for dynamic URL lookup
from django.contrib.auth import authenticate, login, logout # Ensure these are imported
from django_ratelimit.decorators import ratelimit
from django.core.cache import cache
import hashlib
import hmac
import ipaddress

from risk.services import (
    is_suspended as risk_is_suspended,
    market_key_for_bet_type,
    selection_key_for_bet_type,
    auto_suspend_if_needed,
    compute_duplicate_ticket_signature,
    log_duplicate_ticket_if_needed,
    record_device_fingerprint,
    evaluate_ticket_risk,
    check_ip_intelligence,
)
from notifications.services import create_notification
from .services.loan_overdraft import (
    LoanOverdraftError,
    apply_repayment_and_credit_wallet,
    build_qualification_snapshot,
    can_user_transfer_from_wallet,
    create_manual_overdraft,
    build_wallet_overdraft_payload,
    enforce_due_loans,
    finance_overdue_rows,
    fund_overdraft_wallet,
    get_loan_settings,
    get_or_create_overdraft_wallet,
    get_user_outstanding_loans,
    get_user_outstanding_loan_amount,
    get_user_pending_credit_amount,
    loan_has_active_lock_override,
    loan_lock_override_details,
    notify_loan_event,
    override_unlock_loan_without_payment,
    relock_loan_after_override,
    reject_loan_request,
    remit_overdraft_pending_credit,
    submit_overdraft_request,
    approve_loan_request,
    user_has_outstanding_loan,
)

from .models import (
    User, Wallet, WalletLedgerEntry, Transaction, BettingPeriod, Fixture, PopularPick, Selection, BetTicket,
    BonusRule, SystemSetting, UserWithdrawal, WithdrawalReport, AgentPayout, ActivityLog,
    CreditRequest, Loan, CreditLog, ImpersonationLog, ProcessedWithdrawal,
    SiteConfiguration, CarouselImage, PasswordResetRequest, FooterPage, State,
    BettingLimitAuditLog, GlobalBettingSettings, AgentBettingLimitOverride,
    CashierRegistrationRequest, CRMActionLog, LoginAttempt,
    RetailManagerMasterAgentMapping, RetailManagerSuperAgentMapping, RetailManagerAgentMapping, RetailManagerDashboardNote,
    AgentTransferLog, AccountUnlockAppeal, AccountLockAuditLog,
    CustomerComplaint, CustomerComplaintNote, BulkMessageTemplate, BulkMessageCampaign, BulkMessageDelivery, CRMOpsAuditLog,
    FinanceAuditLog, WithdrawalPinVerificationLog, PaymentGatewayEventLog, FinanceTransactionReview,
    LedgerAccount, JournalEntry, JournalLine, FinanceSettlementBatch, FinanceSettlementItem,
    ScheduledFinanceReport, OverdraftWallet, LoanAuditLog, LoanRepayment, TicketTransactionLedger
)
from commission.models import CommissionPeriod, WeeklyAgentCommission, MonthlyNetworkCommission
from pending_registration.models import PendingAgentRegistration
from .forms import (
    UserRegistrationForm, LoginForm, PasswordChangeForm, ProfileEditForm, 
    InitiateDepositForm, WithdrawFundsForm, WalletTransferForm,
    BetTicketForm, CheckTicketStatusForm, DeclareResultForm,
    AdminUserCreationForm, AdminUserChangeForm, WithdrawalActionForm,
    FixtureForm, BettingPeriodForm,
    AccountUserSearchForm, AccountUserWalletActionForm, SuperAdminFundAccountUserForm,
    CreditRequestForm, LoanSettlementForm, AdminManualWalletForm, OverdraftRequestForm,
    LoanCenterDecisionForm, AdminOverdraftWalletFundingForm, LoanOverrideUnlockForm, LoanOverrideRelockForm,
    ForgotPasswordForm, ResetPasswordForm, WithdrawalPinCreateForm, WithdrawalPinResetForm,
    CRMUserProfileForm, CRMWithdrawalDecisionForm, CashierVoidPermissionForm, AgentMinStakeOverrideForm,
    RetailManagerDashboardNoteForm, AgentRemapForm, AccountUnlockAppealForm, AccountUnlockAppealReviewForm,
    CustomerComplaintForm, CustomerComplaintActionForm, CustomerComplaintNoteForm, BulkMessageTemplateForm,
    BulkMessageCampaignForm, CRMThresholdSettingsForm
)
from .services.email_policy import duplicate_email_details, normalize_email_value, resolve_user_from_identifier

# Setup logger for this app
logger = logging.getLogger('betting') # Use the 'betting' logger defined in settings.py


def _ratelimit_key_user_or_ip(group, request):
    user = getattr(request, "user", None)
    if getattr(user, "is_authenticated", False):
        return f"user:{user.pk}"
    ip = (request.META.get("HTTP_X_REAL_IP") or "").strip()
    if not ip:
        ip = (request.META.get("HTTP_X_FORWARDED_FOR") or "").split(",")[0].strip()
    if not ip:
        ip = (request.META.get("REMOTE_ADDR") or "").strip()
    return f"ip:{ip}" if ip else "ip:unknown"


def _get_deposit_notification_email():
    return (
        os.getenv('DEPOSIT_ADMIN_NOTIFICATION_EMAIL')
        or os.getenv('ADMIN_NOTIFICATION_EMAIL')
        or settings.DEFAULT_FROM_EMAIL
        or settings.EMAIL_HOST_USER
    )


def _notify_admin_deposit_success(user, transaction_record, amount, gateway):
    admin_email = _get_deposit_notification_email()
    from_email = settings.DEFAULT_FROM_EMAIL or settings.EMAIL_HOST_USER
    if not admin_email or not from_email:
        return

    identifier = user.email or user.username or f"user#{user.pk}"
    subject = f"Deposit Successful ({gateway.capitalize()}): ₦{amount:.2f}"
    message = (
        f"User: {identifier}\n"
        f"Amount: ₦{amount:.2f}\n"
        f"Gateway: {gateway}\n"
        f"Reference: {transaction_record.external_reference}\n"
        f"Time: {timezone.now().strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
    )
    send_mail(
        subject=subject,
        message=message,
        from_email=from_email,
        recipient_list=[admin_email],
        fail_silently=True
    )


def _quantize_amount(amount):
    try:
        return Decimal(str(amount)).quantize(Decimal("0.01"))
    except Exception:
        return None


def _complete_deposit_transaction(*, tx, amount, gateway, reference, source, payload=None, http_status=None, message=""):
    amount_q = _quantize_amount(amount)
    if amount_q is None or amount_q <= 0:
        raise ValueError("Invalid amount.")

    tx = Transaction.objects.select_for_update().select_related("user").get(pk=tx.pk)
    if tx.transaction_type != "deposit":
        raise ValueError("Not a deposit transaction.")

    if tx.status == "completed" and tx.is_successful:
        PaymentGatewayEventLog.objects.create(
            gateway=gateway,
            event_type=source,
            reference=reference or (tx.external_reference or tx.paystack_reference or str(tx.id)),
            transaction=tx,
            user=tx.user,
            amount=tx.amount,
            success=True,
            http_status=http_status,
            message=(message or "Already completed"),
            payload=(payload or {}),
        )
        return False

    if amount_q != _quantize_amount(tx.amount):
        tx.status = "failed"
        tx.is_successful = False
        tx.description = f"Amount mismatch: Expected {tx.amount}, Got {amount_q}"
        tx.save(update_fields=["status", "is_successful", "description"])
        PaymentGatewayEventLog.objects.create(
            gateway=gateway,
            event_type=source,
            reference=reference or (tx.external_reference or tx.paystack_reference or str(tx.id)),
            transaction=tx,
            user=tx.user,
            amount=tx.amount,
            success=False,
            http_status=http_status,
            message="Amount mismatch",
            payload=(payload or {}),
        )
        raise ValueError("Amount mismatch.")

    apply_repayment_and_credit_wallet(
        user=tx.user,
        amount=amount_q,
        source="gateway_deposit",
        actor=None,
        transaction_obj=tx,
        reference=reference or (tx.external_reference or tx.paystack_reference or str(tx.id)),
        reason=f"Deposit via {gateway} ({source})",
        metadata={"gateway": gateway, "source": source},
    )

    tx.status = "completed"
    tx.is_successful = True
    tx.description = f"Online deposit via {gateway} successful."
    tx.timestamp = timezone.now()
    tx.save(update_fields=["status", "is_successful", "description", "timestamp"])

    PaymentGatewayEventLog.objects.create(
        gateway=gateway,
        event_type=source,
        reference=reference or (tx.external_reference or tx.paystack_reference or str(tx.id)),
        transaction=tx,
        user=tx.user,
        amount=amount_q,
        success=True,
        http_status=http_status,
        message=(message or "Completed"),
        payload=(payload or {}),
    )
    return True


def _build_agent_total_wallet_map(agents_qs):
    agent_ids = list(agents_qs.values_list('id', flat=True))
    if not agent_ids:
        return {}

    agent_wallet_map = {
        row['user_id']: row['balance']
        for row in Wallet.objects.filter(user_id__in=agent_ids).values('user_id', 'balance')
    }
    cashier_totals = {
        row['user__agent_id']: row['total']
        for row in Wallet.objects.filter(user__user_type='cashier', user__agent_id__in=agent_ids)
        .values('user__agent_id')
        .annotate(total=Coalesce(Sum('balance'), Value(0), output_field=DecimalField()))
    }

    totals = {}
    for agent_id in agent_ids:
        totals[agent_id] = (agent_wallet_map.get(agent_id) or Decimal('0.00')) + (cashier_totals.get(agent_id) or Decimal('0.00'))
    return totals


def _build_super_agent_total_wallet_map(super_agents_qs):
    sa_ids = list(super_agents_qs.values_list('id', flat=True))
    if not sa_ids:
        return {}

    sa_wallet_map = {
        row['user_id']: row['balance']
        for row in Wallet.objects.filter(user_id__in=sa_ids).values('user_id', 'balance')
    }

    agent_totals = {
        row['super_agent_id']: row['total']
        for row in User.objects.filter(user_type='agent', super_agent_id__in=sa_ids)
        .values('super_agent_id')
        .annotate(total=Coalesce(Sum('wallet__balance'), Value(0), output_field=DecimalField()))
    }
    cashier_under_agents_totals = {
        row['agent__super_agent_id']: row['total']
        for row in User.objects.filter(user_type='cashier', agent__super_agent_id__in=sa_ids)
        .values('agent__super_agent_id')
        .annotate(total=Coalesce(Sum('wallet__balance'), Value(0), output_field=DecimalField()))
    }
    direct_cashier_totals = {
        row['super_agent_id']: row['total']
        for row in User.objects.filter(user_type='cashier', super_agent_id__in=sa_ids, agent__isnull=True)
        .values('super_agent_id')
        .annotate(total=Coalesce(Sum('wallet__balance'), Value(0), output_field=DecimalField()))
    }

    totals = {}
    for sa_id in sa_ids:
        totals[sa_id] = (
            (sa_wallet_map.get(sa_id) or Decimal('0.00'))
            + (agent_totals.get(sa_id) or Decimal('0.00'))
            + (cashier_under_agents_totals.get(sa_id) or Decimal('0.00'))
            + (direct_cashier_totals.get(sa_id) or Decimal('0.00'))
        )
    return totals


def _get_wallet_balance_map(user_ids):
    ids = [int(i) for i in user_ids if i]
    if not ids:
        return {}
    return {
        row["user_id"]: row["balance"]
        for row in Wallet.objects.filter(user_id__in=ids).values("user_id", "balance")
    }


# --- Helper Functions for User Permissions and Logging ---

def is_admin(user):
    return user.is_authenticated and user.user_type == 'admin'

def is_master_agent(user):
    return user.is_authenticated and user.user_type == 'master_agent'

def is_super_agent(user):
    return user.is_authenticated and user.user_type == 'super_agent'

def is_agent(user):
    return user.is_authenticated and user.user_type == 'agent'

from .utils import (
    get_client_ip,
    get_active_bonus_rules_cached,
    select_bonus_rule,
    compute_bonus_amount,
    get_effective_betting_limits_for_user,
    acquire_ticket_placement_lock,
    release_ticket_placement_lock,
    validate_ticket_against_limits,
    BettingLimitViolation,
    serialize_limits,
    system_bet_payout_projections,
    logout_user_from_all_active_sessions,
)
from .services.usernames import generate_cashier_email
from .services.usernames import create_agent_and_cashiers

def is_cashier(user):
    return user.is_authenticated and user.user_type == 'cashier'

def is_player(user):
    return user.is_authenticated and user.user_type == 'player'

def is_account_user(user):
    return user.is_authenticated and user.user_type == 'account_user'

def is_crm_user(user):
    return user.is_authenticated and (user.is_superuser or user.user_type in ['admin', 'crm'])


def user_passes_test_403(test_func):
    def decorator(view_func):
        @wraps(view_func)
        def _wrapped_view(request, *args, **kwargs):
            if request.user.is_authenticated and not test_func(request.user):
                return HttpResponse("Permission Denied", status=403)
            return view_func(request, *args, **kwargs)

        return _wrapped_view

    return decorator


def run_after_commit_in_background(callback):
    def _start():
        worker = threading.Thread(target=callback, daemon=True)
        worker.start()

    try:
        db_transaction.on_commit(_start)
    except Exception:
        _start()

def crm_can_approve_withdrawals(user):
    if not is_crm_user(user):
        return False
    if user.is_superuser or user.user_type == 'admin':
        return True
    return user.crm_role in ['ops', 'supervisor']

def crm_can_suspend_users(user):
    if not is_crm_user(user):
        return False
    if user.is_superuser or user.user_type == 'admin':
        return True
    return user.crm_role in ['compliance', 'supervisor']

def crm_can_approve_registrations(user):
    if not is_crm_user(user):
        return False
    if user.is_superuser or user.user_type == 'admin':
        return True
    return user.crm_role in ['ops', 'compliance', 'supervisor']

def crm_can_edit_profiles(user):
    if not is_crm_user(user):
        return False
    if user.is_superuser or user.user_type == 'admin':
        return True
    return user.crm_role in ['ops', 'compliance', 'supervisor']

def crm_can_manage_wallet(user):
    if not is_crm_user(user):
        return False
    if user.is_superuser or user.user_type == 'admin':
        return True
    return user.crm_role in ['compliance', 'supervisor']

def crm_can_freeze_withdrawals(user):
    if not is_crm_user(user):
        return False
    if user.is_superuser or user.user_type == 'admin':
        return True
    return user.crm_role in ['compliance', 'supervisor']

def crm_can_reset_password(user):
    if not is_crm_user(user):
        return False
    if user.is_superuser or user.user_type == 'admin':
        return True
    return user.crm_role in ['supervisor']

def crm_can_message(user):
    return is_crm_user(user)

def crm_can_send_direct_email(user):
    if not is_crm_user(user):
        return False
    if user.is_superuser or user.user_type == 'admin':
        return True
    return getattr(user, 'crm_role', '') in ['supervisor']

def crm_can_send_bulk_email(user):
    if not is_crm_user(user):
        return False
    if user.is_superuser or user.user_type == 'admin':
        return True
    return getattr(user, 'crm_role', '') in ['supervisor']

def crm_can_view_audit(user):
    if not is_crm_user(user):
        return False
    if user.is_superuser or user.user_type == 'admin':
        return True
    return user.crm_role in ['supervisor']


def crm_can_remap_agents(user):
    return bool(user.is_authenticated and (user.is_superuser or user.user_type in ['admin', 'crm']))


def _crm_agent_transfer_history_queryset(*, q='', start_dt=None, end_dt=None, old_super_agent_id='', new_super_agent_id=''):
    qs = AgentTransferLog.objects.select_related(
        'agent', 'old_super_agent', 'new_super_agent', 'transferred_by'
    ).order_by('-created_at')
    if start_dt:
        qs = qs.filter(created_at__gte=start_dt)
    if end_dt:
        qs = qs.filter(created_at__lte=end_dt)
    if old_super_agent_id.isdigit():
        qs = qs.filter(old_super_agent_id=int(old_super_agent_id))
    if new_super_agent_id.isdigit():
        qs = qs.filter(new_super_agent_id=int(new_super_agent_id))
    if q:
        qs = qs.filter(
            Q(agent__username__icontains=q) |
            Q(agent__email__icontains=q) |
            Q(agent__phone_number__icontains=q) |
            Q(agent__first_name__icontains=q) |
            Q(agent__last_name__icontains=q) |
            Q(agent__other_name__icontains=q) |
            Q(old_super_agent__username__icontains=q) |
            Q(old_super_agent__email__icontains=q) |
            Q(old_super_agent__first_name__icontains=q) |
            Q(old_super_agent__last_name__icontains=q) |
            Q(new_super_agent__username__icontains=q) |
            Q(new_super_agent__email__icontains=q) |
            Q(new_super_agent__first_name__icontains=q) |
            Q(new_super_agent__last_name__icontains=q) |
            Q(transferred_by__username__icontains=q) |
            Q(transferred_by__email__icontains=q)
        )
    return qs


def _export_simple_rows(*, rows, title, fmt):
    if fmt == 'csv':
        import io
        import csv
        output = io.StringIO()
        fieldnames = list(rows[0].keys()) if rows else []
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
        response = HttpResponse(output.getvalue(), content_type='text/csv')
        response['Content-Disposition'] = f'attachment; filename="{title}.csv"'
        return response

    if fmt == 'xlsx':
        import io
        import pandas as pd
        output = io.BytesIO()
        df = pd.DataFrame(rows)
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            df.to_excel(writer, index=False, sheet_name=(title[:31] or 'Sheet1'))
        output.seek(0)
        response = HttpResponse(
            output.getvalue(),
            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        )
        response['Content-Disposition'] = f'attachment; filename="{title}.xlsx"'
        return response

    if fmt == 'pdf':
        try:
            from weasyprint import HTML
        except Exception as e:
            return HttpResponseBadRequest(f"PDF export unavailable: {e}")
        from html import escape as _html_escape

        columns = list(rows[0].keys()) if rows else []

        def esc(value):
            return _html_escape(str(value or ''), quote=True)

        head = ''.join([f"<th>{esc(col)}</th>" for col in columns])
        body = ''.join(
            "<tr>" + ''.join([f"<td>{esc(row.get(col))}</td>" for col in columns]) + "</tr>"
            for row in rows[:3000]
        )
        html = f"""
        <html>
          <head>
            <meta charset="utf-8" />
            <style>
              body {{ font-family: Arial, sans-serif; font-size: 11px; }}
              h2 {{ margin: 0 0 8px 0; }}
              table {{ width: 100%; border-collapse: collapse; }}
              th, td {{ border: 1px solid #ddd; padding: 6px; vertical-align: top; }}
              th {{ background: #f3f5f7; text-align: left; }}
              tr:nth-child(even) td {{ background: #fafafa; }}
            </style>
          </head>
          <body>
            <h2>{esc(title.replace('_', ' ').title())}</h2>
            <table>
              <thead><tr>{head}</tr></thead>
              <tbody>{body}</tbody>
            </table>
          </body>
        </html>
        """
        pdf_bytes = HTML(string=html).write_pdf()
        response = HttpResponse(pdf_bytes, content_type='application/pdf')
        response['Content-Disposition'] = f'attachment; filename="{title}.pdf"'
        return response

    return HttpResponseBadRequest("Unknown format")
def can_manage_account_unlock_appeals(user):
    return bool(user.is_authenticated and (user.is_superuser or user.user_type in ['admin', 'crm']))


def _scoped_lock_subject_users_queryset(user):
    scoped_types = ['agent', 'super_agent', 'retail_manager', 'cashier']
    if not getattr(user, 'is_authenticated', False):
        return User.objects.none()
    if user.is_superuser or user.user_type in ['admin', 'crm']:
        return User.objects.filter(user_type__in=scoped_types).distinct()
    if user.user_type == 'super_agent':
        return User.objects.filter(
            Q(user_type='agent', super_agent=user) |
            Q(user_type='cashier', agent__super_agent=user)
        ).distinct()
    if user.user_type == 'retail_manager':
        super_agents = get_retail_manager_super_agents(user)
        agents = get_retail_manager_agents(user, super_agents_qs=super_agents)
        super_agent_ids = list(super_agents.values_list('id', flat=True))
        agent_ids = list(agents.values_list('id', flat=True))
        q = Q()
        if super_agent_ids:
            q |= Q(user_type='super_agent', id__in=super_agent_ids)
        if agent_ids:
            q |= Q(user_type='agent', id__in=agent_ids)
            q |= Q(user_type='cashier', agent_id__in=agent_ids)
        return User.objects.filter(q).distinct() if q else User.objects.none()
    return User.objects.none()


def _scoped_locked_accounts_queryset(user):
    latest_appeal_qs = AccountUnlockAppeal.objects.filter(locked_user_id=OuterRef('pk')).order_by('-created_at')
    return (
        _scoped_lock_subject_users_queryset(user)
        .filter(is_locked=True)
        .annotate(
            latest_appeal_status=Subquery(latest_appeal_qs.values('status')[:1]),
            latest_appeal_created_at=Subquery(latest_appeal_qs.values('created_at')[:1]),
        )
        .select_related('super_agent', 'agent', 'master_agent')
        .order_by('-locked_at', 'username', 'email')
    )


def _scoped_account_unlock_appeals_queryset(user):
    scoped_targets = _scoped_lock_subject_users_queryset(user)
    return (
        AccountUnlockAppeal.objects.select_related('locked_user', 'appealed_by', 'reviewed_by')
        .filter(locked_user__in=scoped_targets)
        .order_by('-created_at')
    )


def _apply_locked_accounts_filters(
    qs,
    *,
    query='',
    user_type='',
    status='',
    locked_by='',
    locked_start_dt=None,
    locked_end_dt=None,
    appeal_start_dt=None,
    appeal_end_dt=None,
):
    query = (query or '').strip()
    user_type = (user_type or '').strip()
    status = (status or '').strip().lower()
    locked_by = (locked_by or '').strip()

    if query:
        qs = qs.filter(
            Q(username__icontains=query) |
            Q(email__icontains=query) |
            Q(phone_number__icontains=query) |
            Q(first_name__icontains=query) |
            Q(last_name__icontains=query) |
            Q(other_name__icontains=query)
        )
    if user_type:
        qs = qs.filter(user_type=user_type)
    if locked_start_dt:
        qs = qs.filter(locked_at__gte=locked_start_dt)
    if locked_end_dt:
        qs = qs.filter(locked_at__lte=locked_end_dt)
    if appeal_start_dt:
        qs = qs.filter(latest_appeal_created_at__gte=appeal_start_dt)
    if appeal_end_dt:
        qs = qs.filter(latest_appeal_created_at__lte=appeal_end_dt)
    if status == 'appealed':
        qs = qs.filter(latest_appeal_status='pending')
    elif status == 'rejected':
        qs = qs.filter(latest_appeal_status='rejected')
    elif status == 'locked':
        qs = qs.exclude(latest_appeal_status__in=['pending', 'rejected'])
    if locked_by:
        lock_filters = (
            Q(locked_by__username__icontains=locked_by) |
            Q(locked_by__email__icontains=locked_by) |
            Q(lock_reason__icontains=locked_by) |
            Q(remarks__icontains=locked_by)
        )
        if locked_by.lower() in {'system', 'invalid credentials', 'invalid credential'}:
            lock_filters |= Q(locked_by__isnull=True)
        lock_ids = list(
            AccountLockAuditLog.objects.filter(action='locked')
            .filter(lock_filters)
            .values_list('locked_user_id', flat=True)
        )
        qs = qs.filter(id__in=lock_ids)
    return qs


def _apply_account_unlock_appeal_filters(
    qs,
    *,
    query='',
    user_type='',
    status='',
    locked_by='',
    locked_start_dt=None,
    locked_end_dt=None,
    appeal_start_dt=None,
    appeal_end_dt=None,
):
    query = (query or '').strip()
    user_type = (user_type or '').strip()
    status = (status or '').strip().lower()
    locked_by = (locked_by or '').strip()

    if query:
        qs = qs.filter(
            Q(locked_user__username__icontains=query) |
            Q(locked_user__email__icontains=query) |
            Q(locked_user__phone_number__icontains=query) |
            Q(locked_user__first_name__icontains=query) |
            Q(locked_user__last_name__icontains=query) |
            Q(appealed_by__username__icontains=query) |
            Q(appealed_by__email__icontains=query)
        )
    if user_type:
        qs = qs.filter(locked_user__user_type=user_type)
    if status:
        qs = qs.filter(status=status)
    if locked_start_dt:
        qs = qs.filter(locked_user__locked_at__gte=locked_start_dt)
    if locked_end_dt:
        qs = qs.filter(locked_user__locked_at__lte=locked_end_dt)
    if appeal_start_dt:
        qs = qs.filter(created_at__gte=appeal_start_dt)
    if appeal_end_dt:
        qs = qs.filter(created_at__lte=appeal_end_dt)
    if locked_by:
        lock_filters = (
            Q(locked_by__username__icontains=locked_by) |
            Q(locked_by__email__icontains=locked_by) |
            Q(lock_reason__icontains=locked_by) |
            Q(remarks__icontains=locked_by)
        )
        if locked_by.lower() in {'system', 'invalid credentials', 'invalid credential'}:
            lock_filters |= Q(locked_by__isnull=True)
        lock_ids = list(
            AccountLockAuditLog.objects.filter(action='locked')
            .filter(lock_filters)
            .values_list('locked_user_id', flat=True)
        )
        qs = qs.filter(locked_user_id__in=lock_ids)
    return qs


def _attach_locked_account_metadata(users):
    user_ids = [u.id for u in users if getattr(u, 'id', None)]
    if not user_ids:
        return users

    latest_appeals = (
        AccountUnlockAppeal.objects.filter(locked_user_id__in=user_ids)
        .select_related('appealed_by', 'reviewed_by')
        .order_by('locked_user_id', '-created_at')
    )
    latest_appeal_map = {}
    for appeal in latest_appeals:
        latest_appeal_map.setdefault(appeal.locked_user_id, appeal)

    latest_lock_logs = (
        AccountLockAuditLog.objects.filter(locked_user_id__in=user_ids, action='locked')
        .select_related('locked_by')
        .order_by('locked_user_id', '-timestamp')
    )
    latest_lock_map = {}
    for log in latest_lock_logs:
        latest_lock_map.setdefault(log.locked_user_id, log)

    for user in users:
        latest_appeal = latest_appeal_map.get(user.id)
        latest_lock = latest_lock_map.get(user.id)
        user.latest_unlock_appeal = latest_appeal
        user.locked_account_status_label = 'Locked'
        if latest_appeal:
            if latest_appeal.status == 'pending':
                user.locked_account_status_label = 'Appealed'
            elif latest_appeal.status == 'rejected':
                user.locked_account_status_label = 'Rejected'
        actor = getattr(latest_lock, 'locked_by', None)
        user.locked_by_display = (
            (getattr(actor, 'username', None) or getattr(actor, 'email', None))
            if actor else 'System'
        )
        user.lock_reason_display = (
            (getattr(latest_lock, 'lock_reason', None) or '').strip()
            or (getattr(user, 'lock_reason', None) or '').strip()
            or '-'
        )
        user.can_submit_unlock_appeal = not (latest_appeal and latest_appeal.status == 'pending')
    return users


def _safe_send_simple_email(subject, message, recipients):
    recipient_list = [email for email in recipients if email]
    if not recipient_list:
        return
    try:
        send_mail(
            subject=subject,
            message=message,
            from_email=settings.DEFAULT_FROM_EMAIL or settings.EMAIL_HOST_USER,
            recipient_list=recipient_list,
            fail_silently=True,
        )
    except Exception:
        pass


def _get_related_super_agent_for_locked_user(locked_user):
    if not locked_user:
        return None
    if locked_user.user_type == 'agent':
        return locked_user.super_agent
    if locked_user.user_type == 'cashier' and locked_user.agent_id:
        try:
            return locked_user.agent.super_agent
        except Exception:
            return None
    return None


def _get_related_retail_managers_for_locked_user(locked_user):
    if not locked_user:
        return User.objects.none()
    retail_manager_ids = set()
    if locked_user.user_type == 'super_agent':
        retail_manager_ids |= set(
            RetailManagerSuperAgentMapping.objects.filter(super_agent=locked_user).values_list('retail_manager_id', flat=True)
        )
    elif locked_user.user_type == 'agent':
        retail_manager_ids |= set(
            RetailManagerAgentMapping.objects.filter(agent=locked_user).values_list('retail_manager_id', flat=True)
        )
        if locked_user.super_agent_id:
            retail_manager_ids |= set(
                RetailManagerSuperAgentMapping.objects.filter(super_agent_id=locked_user.super_agent_id).values_list('retail_manager_id', flat=True)
            )
    elif locked_user.user_type == 'cashier' and locked_user.agent_id:
        retail_manager_ids |= set(
            RetailManagerAgentMapping.objects.filter(agent_id=locked_user.agent_id).values_list('retail_manager_id', flat=True)
        )
        try:
            if locked_user.agent and locked_user.agent.super_agent_id:
                retail_manager_ids |= set(
                    RetailManagerSuperAgentMapping.objects.filter(super_agent_id=locked_user.agent.super_agent_id).values_list('retail_manager_id', flat=True)
                )
        except Exception:
            pass
    if not retail_manager_ids:
        return User.objects.none()
    return User.objects.filter(id__in=list(retail_manager_ids), is_active=True)


def _notify_admins_of_unlock_appeal(appeal):
    review_url = reverse('betting:account_appeals_review')
    message = 'A new account unlock appeal has been submitted.'
    admin_qs = User.objects.filter(Q(is_superuser=True) | Q(user_type='admin'), is_active=True).distinct()
    for admin_user in admin_qs.iterator():
        create_notification(
            recipient=admin_user,
            notification_type='SYSTEM_ANNOUNCEMENT',
            title='New Account Unlock Appeal',
            message=message,
            data={
                'popup_category': 'message',
                'delivery_channel': 'in_app',
                'url': review_url,
            },
        )


def _notify_unlock_appeal_resolution(appeal, *, approved):
    locked_user = appeal.locked_user
    if not locked_user:
        return

    related_super_agent = _get_related_super_agent_for_locked_user(locked_user)
    related_retail_managers = list(_get_related_retail_managers_for_locked_user(locked_user))

    if approved:
        locked_title = 'Unlock Appeal Approved'
        locked_message = 'Your unlock appeal has been approved. Your account is now active.'
        applicant_title = 'Unlock Appeal Approved'
        applicant_message = 'Your unlock appeal has been approved. The account is now active.'
        manager_message = f"{locked_user.username or locked_user.email or locked_user.get_full_name()} unlock appeal has been approved."
        email_subject = 'Unlock Appeal Approved'
        email_message = locked_message
    else:
        locked_title = 'Unlock Appeal Rejected'
        locked_message = 'Your unlock appeal has been reviewed and declined. Please contact support for further clarification.'
        applicant_title = 'Unlock Appeal Rejected'
        applicant_message = 'Your unlock appeal has been reviewed and declined. Please contact support for further clarification.'
        manager_message = f"{locked_user.username or locked_user.email or locked_user.get_full_name()} unlock appeal has been rejected."
        email_subject = 'Unlock Appeal Rejected'
        email_message = locked_message

    recipient_pool = []
    if appeal.appealed_by_id:
        recipient_pool.append((appeal.appealed_by, applicant_title, applicant_message))
    recipient_pool.append((locked_user, locked_title, locked_message))
    if related_super_agent:
        recipient_pool.append((related_super_agent, locked_title, manager_message))
    for retail_manager in related_retail_managers:
        recipient_pool.append((retail_manager, locked_title, manager_message))

    seen_ids = set()
    email_targets = []
    for recipient, title, message in recipient_pool:
        if not recipient or recipient.id in seen_ids:
            continue
        seen_ids.add(recipient.id)
        create_notification(
            recipient=recipient,
            notification_type='SYSTEM_ANNOUNCEMENT',
            title=title,
            message=message,
            data={
                'popup_category': 'message',
                'delivery_channel': 'in_app',
                'url': reverse('betting:account_appeals_review') if can_manage_account_unlock_appeals(recipient) else reverse('betting:crm_dashboard') if recipient.user_type == 'crm' else reverse('betting:retail_dashboard') if recipient.user_type == 'retail_manager' else reverse('betting:super_agent_dashboard') if recipient.user_type == 'super_agent' else reverse('betting:user_dashboard'),
            },
        )
        email_targets.append(recipient.email)

    _safe_send_simple_email(email_subject, email_message, email_targets)


CRM_WALLET_APPROVAL_REQUEST_TYPES = ('crm_credit', 'crm_debit')


def get_default_wallet_request_approver():
    approver = User.objects.filter(is_active=True, user_type='account_user').order_by('id').first()
    if approver:
        return approver
    return User.objects.filter(is_active=True).filter(Q(is_superuser=True) | Q(user_type='admin')).order_by('-is_superuser', 'id').first()


class CreditRequestProcessError(Exception):
    pass


def get_credit_request_approver_role(user):
    if getattr(user, 'is_superuser', False):
        return 'superadmin'
    if getattr(user, 'user_type', '') == 'admin':
        return 'admin'
    if getattr(user, 'user_type', '') == 'account_user':
        return 'account_user'
    return getattr(user, 'user_type', '') or 'user'


def process_credit_request_decision(*, actor, credit_req, action, account_user_wallet_user=None):
    if credit_req.status != 'pending':
        raise CreditRequestProcessError("This request has already been processed.")

    if action == 'decline':
        credit_req.status = 'declined'
        credit_req.save(update_fields=['status', 'updated_at'])
        CreditLog.objects.create(
            actor=actor,
            target_user=credit_req.requester,
            action_type='request_declined',
            amount=credit_req.amount,
            status='declined',
            reference_id=str(credit_req.id)
        )
        return "Request declined.", messages.INFO

    if action != 'approve':
        raise CreditRequestProcessError("Invalid request action.")

    approver_role = get_credit_request_approver_role(actor)
    admin_actor = approver_role in {'superadmin', 'admin'}
    selected_account_user = None
    if account_user_wallet_user is not None:
        if getattr(account_user_wallet_user, 'user_type', '') != 'account_user' or not getattr(account_user_wallet_user, 'is_active', False):
            raise CreditRequestProcessError("Selected Account User is not available for wallet processing.")
        selected_account_user = account_user_wallet_user

    if credit_req.request_type == 'crm_credit':
        target_wallet, _ = Wallet.objects.select_for_update().get_or_create(user=credit_req.requester, defaults={'balance': Decimal('0.00')})
        funding_mode = 'approver_wallet'
        source_wallet_user = actor
        source_wallet = None
        tx_out = None

        if admin_actor:
            if selected_account_user is not None:
                source_wallet_user = selected_account_user
                source_wallet, _ = Wallet.objects.select_for_update().get_or_create(user=source_wallet_user, defaults={'balance': Decimal('0.00')})
                if source_wallet.balance < credit_req.amount:
                    raise CreditRequestProcessError("Selected Account User wallet has insufficient funds for this credit.")
                funding_mode = 'account_user_wallet'
                tx_out = Transaction.objects.create(
                    user=source_wallet_user,
                    initiating_user=actor,
                    target_user=credit_req.requester,
                    transaction_type='account_user_debit',
                    amount=credit_req.amount,
                    status='completed',
                    is_successful=True,
                    description=f"CRM credit approved by {actor.email} using Account User {source_wallet_user.email}"
                )
            elif approver_role == 'superadmin':
                funding_mode = 'superadmin_override'
            else:
                raise CreditRequestProcessError("Please choose an Account User wallet to debit for this CRM credit approval.")
        else:
            source_wallet, _ = Wallet.objects.select_for_update().get_or_create(user=actor, defaults={'balance': Decimal('0.00')})
            if source_wallet.balance < credit_req.amount:
                raise CreditRequestProcessError("Insufficient funds in your wallet to approve this credit.")
            tx_out = Transaction.objects.create(
                user=actor,
                transaction_type='wallet_transfer_out',
                amount=credit_req.amount,
                status='completed',
                is_successful=True,
                target_user=credit_req.requester,
                description=f"Approved CRM credit for {credit_req.requester.email}"
            )

        if tx_out is not None and source_wallet is not None:
            source_wallet.apply_delta(
                amount=-credit_req.amount,
                actor=actor,
                transaction_obj=tx_out,
                reference=str(credit_req.id),
                reason=tx_out.description,
                metadata={
                    "credit_request_id": credit_req.id,
                    "request_type": credit_req.request_type,
                    "approved_by_role": approver_role,
                    "funding_mode": funding_mode,
                    "funding_account_user_id": getattr(source_wallet_user, 'id', None) if funding_mode == 'account_user_wallet' else None,
                },
            )

        if funding_mode == 'superadmin_override':
            tx_in_type = 'manual_credit'
            tx_in_description = f"CRM credit approved by superadmin {actor.email} without wallet debit"
            tx_in_target_user = None
        elif funding_mode == 'account_user_wallet':
            tx_in_type = 'account_user_credit'
            tx_in_description = f"CRM credit approved by {actor.email} using Account User {source_wallet_user.email}"
            tx_in_target_user = source_wallet_user
        else:
            tx_in_type = 'wallet_transfer_in'
            tx_in_description = f"CRM credit approved by {actor.email} ({approver_role})"
            tx_in_target_user = actor

        tx_in = Transaction.objects.create(
            user=credit_req.requester,
            transaction_type=tx_in_type,
            amount=credit_req.amount,
            status='completed',
            is_successful=True,
            initiating_user=actor,
            target_user=tx_in_target_user,
            description=tx_in_description
        )
        credit_result = apply_repayment_and_credit_wallet(
            user=credit_req.requester,
            amount=credit_req.amount,
            source='crm_credit',
            actor=actor,
            transaction_obj=tx_in,
            reference=str(credit_req.id),
            reason=tx_in.description,
            metadata={
                "credit_request_id": credit_req.id,
                "request_type": credit_req.request_type,
                "approved_by_role": approver_role,
                "funding_mode": funding_mode,
                "funding_account_user_id": getattr(source_wallet_user, 'id', None) if funding_mode == 'account_user_wallet' else None,
            },
        )
        credit_req.status = 'approved'
        credit_req.save(update_fields=['status', 'updated_at'])
        CRMActionLog.objects.create(
            actor=actor,
            target_user=credit_req.requester,
            action_type='WALLET_CREDITED',
            reason=credit_req.reason,
            data={
                'amount': str(credit_req.amount),
                'request_id': credit_req.id,
                'approved_by': actor.email,
                'approved_by_role': approver_role,
                'funding_mode': funding_mode,
                'funding_account_user_email': getattr(source_wallet_user, 'email', '') if funding_mode == 'account_user_wallet' else '',
            },
        )
        CreditLog.objects.create(
            actor=actor,
            target_user=credit_req.requester,
            action_type='request_approved',
            amount=credit_req.amount,
            status='approved',
            reference_id=str(credit_req.id)
        )
        if funding_mode == 'superadmin_override':
            return (
                "CRM credit request approved by superadmin without debiting any wallet. "
                f"Wallet credit: ₦{credit_result.get('wallet_credit_amount') or Decimal('0.00')}. "
                f"Reserved new credit: ₦{credit_result.get('pending_credit_amount') or Decimal('0.00')}."
            ), messages.SUCCESS
        if funding_mode == 'account_user_wallet':
            return (
                f"CRM credit request approved. Debited Account User {source_wallet_user.email} and credited the target user. "
                f"Wallet credit: ₦{credit_result.get('wallet_credit_amount') or Decimal('0.00')}. "
                f"Reserved new credit: ₦{credit_result.get('pending_credit_amount') or Decimal('0.00')}."
            ), messages.SUCCESS
        return (
            "CRM credit request approved. Funds transferred. "
            f"Wallet credit: ₦{credit_result.get('wallet_credit_amount') or Decimal('0.00')}. "
            f"Reserved new credit: ₦{credit_result.get('pending_credit_amount') or Decimal('0.00')}."
        ), messages.SUCCESS

    if credit_req.request_type == 'crm_debit':
        target_wallet, _ = Wallet.objects.select_for_update().get_or_create(user=credit_req.requester, defaults={'balance': Decimal('0.00')})
        if target_wallet.balance < credit_req.amount:
            raise CreditRequestProcessError("Target user has insufficient funds for this debit.")

        reimbursement_mode = 'approver_wallet'
        reimbursement_user = actor
        reimbursement_wallet = None

        if admin_actor:
            if selected_account_user is None:
                raise CreditRequestProcessError("Please choose an Account User wallet to reimburse for this CRM debit approval.")
            reimbursement_user = selected_account_user
            reimbursement_wallet, _ = Wallet.objects.select_for_update().get_or_create(user=reimbursement_user, defaults={'balance': Decimal('0.00')})
            reimbursement_mode = 'account_user_wallet'
            tx_out = Transaction.objects.create(
                user=credit_req.requester,
                transaction_type='account_user_debit',
                amount=credit_req.amount,
                status='completed',
                is_successful=True,
                initiating_user=actor,
                target_user=reimbursement_user,
                description=f"CRM debit approved by {actor.email}; reimbursed to Account User {reimbursement_user.email}"
            )
            tx_in = Transaction.objects.create(
                user=reimbursement_user,
                transaction_type='account_user_credit',
                amount=credit_req.amount,
                status='completed',
                is_successful=True,
                initiating_user=actor,
                target_user=credit_req.requester,
                description=f"Reimbursed from CRM debit approved by {actor.email} for {credit_req.requester.email}"
            )
        else:
            reimbursement_wallet, _ = Wallet.objects.select_for_update().get_or_create(user=actor, defaults={'balance': Decimal('0.00')})
            tx_out = Transaction.objects.create(
                user=credit_req.requester,
                transaction_type='wallet_transfer_out',
                amount=credit_req.amount,
                status='completed',
                is_successful=True,
                initiating_user=actor,
                target_user=actor,
                description=f"CRM debit approved by {actor.email} ({approver_role})"
            )
            tx_in = Transaction.objects.create(
                user=actor,
                transaction_type='wallet_transfer_in',
                amount=credit_req.amount,
                status='completed',
                is_successful=True,
                initiating_user=actor,
                target_user=credit_req.requester,
                description=f"Received CRM debit value from {credit_req.requester.email}"
            )
        target_wallet.apply_delta(
            amount=-credit_req.amount,
            actor=actor,
            transaction_obj=tx_out,
            reference=str(credit_req.id),
            reason=tx_out.description,
            metadata={
                "credit_request_id": credit_req.id,
                "request_type": credit_req.request_type,
                "approved_by_role": approver_role,
                "reimbursement_mode": reimbursement_mode,
                "reimbursement_account_user_id": getattr(reimbursement_user, 'id', None) if reimbursement_mode == 'account_user_wallet' else None,
            },
        )
        reimbursement_wallet.apply_delta(
            amount=credit_req.amount,
            actor=actor,
            transaction_obj=tx_in,
            reference=str(credit_req.id),
            reason=tx_in.description,
            metadata={
                "credit_request_id": credit_req.id,
                "request_type": credit_req.request_type,
                "approved_by_role": approver_role,
                "reimbursement_mode": reimbursement_mode,
                "reimbursement_account_user_id": getattr(reimbursement_user, 'id', None) if reimbursement_mode == 'account_user_wallet' else None,
            },
        )
        credit_req.status = 'approved'
        credit_req.save(update_fields=['status', 'updated_at'])
        CRMActionLog.objects.create(
            actor=actor,
            target_user=credit_req.requester,
            action_type='WALLET_DEBITED',
            reason=credit_req.reason,
            data={
                'amount': str(credit_req.amount),
                'request_id': credit_req.id,
                'approved_by': actor.email,
                'approved_by_role': approver_role,
                'reimbursement_mode': reimbursement_mode,
                'reimbursement_account_user_email': getattr(reimbursement_user, 'email', '') if reimbursement_mode == 'account_user_wallet' else '',
            },
        )
        CreditLog.objects.create(
            actor=actor,
            target_user=credit_req.requester,
            action_type='request_approved',
            amount=credit_req.amount,
            status='approved',
            reference_id=str(credit_req.id)
        )
        if reimbursement_mode == 'account_user_wallet':
            return f"CRM debit request approved. Target user debited and Account User {reimbursement_user.email} reimbursed.", messages.SUCCESS
        return "CRM debit request approved. Funds moved to the approver wallet.", messages.SUCCESS

    lender_wallet = Wallet.objects.select_for_update().get(user=actor)
    borrower_wallet = Wallet.objects.select_for_update().get(user=credit_req.requester)

    if lender_wallet.balance < credit_req.amount:
        raise CreditRequestProcessError("Insufficient funds to approve this request.")

    if credit_req.request_type == 'loan':
        Loan.objects.create(
            borrower=credit_req.requester,
            lender=actor,
            amount=credit_req.amount,
            outstanding_balance=credit_req.amount,
            status='active',
            credit_request=credit_req,
            due_date=timezone.now() + timedelta(days=7)
        )

    tx_out = Transaction.objects.create(
        user=actor,
        transaction_type='wallet_transfer_out',
        amount=credit_req.amount,
        status='completed',
        is_successful=True,
        target_user=credit_req.requester,
        description=f"Approved {credit_req.request_type} request to {credit_req.requester.email}"
    )

    tx_in = Transaction.objects.create(
        user=credit_req.requester,
        transaction_type='wallet_transfer_in',
        amount=credit_req.amount,
        status='completed',
        is_successful=True,
        initiating_user=actor,
        description=f"Received {credit_req.request_type} from {actor.email}"
    )
    lender_wallet.apply_delta(
        amount=-credit_req.amount,
        actor=actor,
        transaction_obj=tx_out,
        reference=str(credit_req.id),
        reason=tx_out.description,
        metadata={"credit_request_id": credit_req.id, "request_type": credit_req.request_type},
    )
    borrower_wallet.apply_delta(
        amount=credit_req.amount,
        actor=actor,
        transaction_obj=tx_in,
        reference=str(credit_req.id),
        reason=tx_in.description,
        metadata={"credit_request_id": credit_req.id, "request_type": credit_req.request_type},
    )
    credit_req.status = 'approved'
    credit_req.save(update_fields=['status', 'updated_at'])
    CreditLog.objects.create(
        actor=actor,
        target_user=credit_req.requester,
        action_type='request_approved',
        amount=credit_req.amount,
        status='approved',
        reference_id=str(credit_req.id)
    )
    return "Request approved. Funds transferred.", messages.SUCCESS


def attach_wallet_balance_snapshots(transactions):
    tx_list = list(transactions)
    for tx in tx_list:
        tx.wallet_balance_before = None
        tx.wallet_balance_after = None

    tx_ids = [tx.id for tx in tx_list if getattr(tx, 'id', None)]
    if not tx_ids:
        return tx_list

    WalletLedgerEntry = apps.get_model('betting', 'WalletLedgerEntry')
    ledger_entries = (
        WalletLedgerEntry.objects
        .filter(transaction_id__in=tx_ids)
        .order_by('created_at', 'id')
    )

    ledger_by_key = {}
    ledger_by_tx = {}
    for entry in ledger_entries:
        key = (entry.transaction_id, entry.user_id)
        ledger_by_key.setdefault(key, entry)
        ledger_by_tx.setdefault(entry.transaction_id, entry)

    for tx in tx_list:
        entry = ledger_by_key.get((tx.id, tx.user_id)) or ledger_by_tx.get(tx.id)
        if entry:
            tx.wallet_balance_before = entry.balance_before
            tx.wallet_balance_after = entry.balance_after

    return tx_list

def is_retail_manager(user):
    return user.is_authenticated and user.user_type == 'retail_manager'

def retail_can_view(user):
    return is_retail_manager(user)

def is_finance_user(user):
    return user.is_authenticated and (user.is_superuser or user.user_type in ['admin', 'finance'])

def finance_can_approve_withdrawals(user):
    if not is_finance_user(user):
        return False
    if user.is_superuser or user.user_type == 'admin':
        return True
    return getattr(user, 'finance_role', '') in ['manager', 'withdrawal']

def finance_can_reverse_transactions(user):
    if not is_finance_user(user):
        return False
    if user.is_superuser or user.user_type == 'admin':
        return True
    return getattr(user, 'finance_role', '') in ['manager']

def finance_can_adjust_wallets(user):
    if not is_finance_user(user):
        return False
    if user.is_superuser or user.user_type == 'admin':
        return True
    return getattr(user, 'finance_role', '') in ['manager', 'accountant']

def finance_can_export(user):
    if not is_finance_user(user):
        return False
    if user.is_superuser or user.user_type == 'admin':
        return True
    return getattr(user, 'finance_role', '') in ['manager', 'accountant', 'auditor', 'settlement', 'withdrawal']

def finance_can_view_audit(user):
    if not is_finance_user(user):
        return False
    if user.is_superuser or user.user_type == 'admin':
        return True
    return getattr(user, 'finance_role', '') in ['manager', 'auditor']

def finance_can_verify_transactions(user):
    if not is_finance_user(user):
        return False
    if user.is_superuser or user.user_type == 'admin':
        return True
    return getattr(user, 'finance_role', '') in ['manager', 'accountant', 'auditor']

def finance_can_manage_settlements(user):
    if not is_finance_user(user):
        return False
    if user.is_superuser or user.user_type == 'admin':
        return True
    return getattr(user, 'finance_role', '') in ['manager', 'settlement']

def finance_can_manage_ledger(user):
    if not is_finance_user(user):
        return False
    if user.is_superuser or user.user_type == 'admin':
        return True
    return getattr(user, 'finance_role', '') in ['manager', 'accountant']

def finance_can_view_gateways(user):
    if not is_finance_user(user):
        return False
    if user.is_superuser or user.user_type == 'admin':
        return True
    return getattr(user, 'finance_role', '') in ['manager', 'accountant', 'auditor']

def finance_can_view_pin_logs(user):
    if not is_finance_user(user):
        return False
    if user.is_superuser or user.user_type == 'admin':
        return True
    return getattr(user, 'finance_role', '') in ['manager', 'withdrawal', 'auditor']

def get_retail_manager_master_agents(user):
    if not is_retail_manager(user):
        return User.objects.none()
    ma_ids = set(
        RetailManagerMasterAgentMapping.objects.filter(retail_manager=user).values_list('master_agent_id', flat=True)
    )
    derived_from_super = set(
        RetailManagerSuperAgentMapping.objects.filter(retail_manager=user)
        .exclude(super_agent__master_agent_id__isnull=True)
        .values_list('super_agent__master_agent_id', flat=True)
    )
    derived_from_agents = set(
        RetailManagerAgentMapping.objects.filter(retail_manager=user)
        .exclude(agent__master_agent_id__isnull=True)
        .values_list('agent__master_agent_id', flat=True)
    )
    ma_ids |= derived_from_super
    ma_ids |= derived_from_agents
    if not ma_ids:
        return User.objects.none()
    return User.objects.filter(id__in=list(ma_ids), user_type='master_agent')

def get_retail_manager_super_agents(user, *, master_agents_qs=None):
    if not is_retail_manager(user):
        return User.objects.none()
    direct_ids = list(
        RetailManagerSuperAgentMapping.objects.filter(retail_manager=user).values_list('super_agent_id', flat=True)
    )
    direct_qs = User.objects.filter(id__in=direct_ids, user_type='super_agent') if direct_ids else User.objects.none()

    derived_super_ids = list(
        RetailManagerAgentMapping.objects.filter(retail_manager=user)
        .exclude(agent__super_agent_id__isnull=True)
        .values_list('agent__super_agent_id', flat=True)
        .distinct()
    )
    derived_from_agents_qs = User.objects.filter(id__in=derived_super_ids, user_type='super_agent') if derived_super_ids else User.objects.none()

    return (direct_qs | derived_from_agents_qs).distinct()

def get_retail_manager_agents(user, *, master_agents_qs=None, super_agents_qs=None):
    if not is_retail_manager(user):
        return User.objects.none()
    direct_ids = list(
        RetailManagerAgentMapping.objects.filter(retail_manager=user).values_list('agent_id', flat=True)
    )
    direct_qs = User.objects.filter(id__in=direct_ids, user_type='agent') if direct_ids else User.objects.none()

    if master_agents_qs is None:
        master_agents_qs = get_retail_manager_master_agents(user)
    if super_agents_qs is None:
        super_agents_qs = get_retail_manager_super_agents(user, master_agents_qs=master_agents_qs)
    derived_qs = (
        User.objects.filter(user_type='agent', super_agent__in=super_agents_qs)
        if super_agents_qs is not None and super_agents_qs.exists()
        else User.objects.none()
    )
    return (direct_qs | derived_qs).distinct()

def get_retail_network_users_qs(user):
    if not is_retail_manager(user):
        return User.objects.none()
    mas = get_retail_manager_master_agents(user)
    sas = get_retail_manager_super_agents(user, master_agents_qs=mas)
    agents = get_retail_manager_agents(user, master_agents_qs=mas, super_agents_qs=sas)
    q = Q(id__in=list(mas.values_list('id', flat=True)))
    q |= Q(id__in=list(sas.values_list('id', flat=True)))
    q |= Q(id__in=list(agents.values_list('id', flat=True)))
    q |= Q(agent__in=agents) | Q(super_agent__in=sas)
    return User.objects.filter(q).distinct()


def can_view_overdraft_reporting(user):
    return bool(
        user.is_authenticated
        and (
            is_retail_manager(user)
            or is_crm_user(user)
            or is_finance_user(user)
            or is_account_user(user)
        )
    )


def _loan_reporting_scope_queryset(user):
    loans = Loan.objects.select_related(
        'borrower',
        'borrower__super_agent',
        'borrower__master_agent',
        'approved_by',
        'rejected_by',
        'lender',
        'credit_request',
        'credit_request__requester',
    ).order_by('-created_at', '-id')
    if is_retail_manager(user):
        return loans.filter(borrower__in=get_retail_manager_agents(user))
    return loans


def _loan_reporting_actor_label(user_obj):
    if not user_obj:
        return '-'
    if getattr(user_obj, 'is_superuser', False):
        return 'Super Admin'
    user_type = getattr(user_obj, 'user_type', '') or ''
    if user_type == 'admin':
        return 'Admin'
    if user_type == 'finance':
        return 'Finance User'
    if user_type == 'account_user':
        return 'Account User'
    if user_type == 'crm':
        return 'CRM User'
    return user_obj.username or user_obj.email or f'User #{user_obj.pk}'


def _loan_reporting_status_label(loan):
    if loan.status == 'pending':
        return 'Pending Approval'
    if loan.status == 'rejected':
        return 'Rejected'
    if loan.status == 'settled':
        return 'Fully Settled'
    if loan.status == 'defaulted' and (loan.account_locked_due_to_default or getattr(loan.borrower, 'is_locked', False)):
        return 'Locked'
    if loan.status == 'defaulted':
        return 'Defaulted'
    if loan.outstanding_balance > Decimal('0.00') and loan.due_date and loan.due_date < timezone.now():
        return 'Overdue'
    if loan.outstanding_balance > Decimal('0.00') and loan.repaid_amount > Decimal('0.00'):
        return 'Partially Repaid'
    if loan.status == 'active':
        return 'Approved'
    return loan.get_status_display()


def _loan_reporting_repayment_status(loan):
    if loan.status == 'pending':
        return 'Pending Approval'
    if loan.status == 'rejected':
        return 'Rejected'
    if loan.status == 'settled' or loan.outstanding_balance <= Decimal('0.00'):
        return 'Fully Settled'
    if loan.status == 'defaulted':
        return 'Defaulted'
    if loan.due_date and loan.due_date < timezone.now():
        return 'Overdue'
    if loan.repaid_amount > Decimal('0.00'):
        return 'Partially Repaid'
    return 'Outstanding'


def _loan_reporting_days_outstanding(loan, *, now=None):
    now = now or timezone.now()
    if not loan.created_at:
        return 0
    return max(0, (timezone.localtime(now).date() - timezone.localtime(loan.created_at).date()).days)


def _loan_reporting_days_overdue(loan, *, now=None):
    now = now or timezone.now()
    if not loan.due_date or loan.outstanding_balance <= Decimal('0.00') or loan.due_date >= now:
        return 0
    return max(0, (timezone.localtime(now).date() - timezone.localtime(loan.due_date).date()).days)


def _loan_reporting_parse_date_range(start_date_str, end_date_str):
    start_dt = None
    end_dt = None
    try:
        if start_date_str:
            start_dt = timezone.make_aware(datetime.strptime(start_date_str, '%Y-%m-%d'))
    except Exception:
        start_dt = None
    try:
        if end_date_str:
            end_raw = datetime.strptime(end_date_str, '%Y-%m-%d')
            end_dt = timezone.make_aware(datetime.combine(end_raw.date(), datetime.max.time()))
    except Exception:
        end_dt = None
    return start_dt, end_dt


def _loan_reporting_filters_from_request(request):
    return {
        'q': (request.GET.get('loan_q') or '').strip(),
        'start_date': (request.GET.get('start_date') or '').strip(),
        'end_date': (request.GET.get('end_date') or '').strip(),
        'retail_manager_id': (request.GET.get('loan_retail_manager') or '').strip(),
        'master_agent_id': (request.GET.get('loan_master_agent') or '').strip(),
        'super_agent_id': (request.GET.get('loan_super_agent') or '').strip(),
        'agent_identifier': (request.GET.get('loan_agent') or '').strip(),
        'status': (request.GET.get('loan_status') or '').strip(),
        'withdrawal_locked': (request.GET.get('loan_withdrawal_locked') or '').strip(),
        'account_locked': (request.GET.get('loan_account_locked') or '').strip(),
        'issued_by': (request.GET.get('loan_issued_by') or '').strip(),
        'outstanding_only': (request.GET.get('loan_outstanding_only') or '').strip(),
        'overdue_only': (request.GET.get('loan_overdue_only') or '').strip(),
    }


def _apply_loan_reporting_filters(loans, *, filters, viewer):
    q = filters.get('q') or ''
    start_dt, end_dt = _loan_reporting_parse_date_range(filters.get('start_date'), filters.get('end_date'))
    now = timezone.now()

    if start_dt:
        loans = loans.filter(created_at__gte=start_dt)
    if end_dt:
        loans = loans.filter(created_at__lte=end_dt)

    if q:
        loans = loans.filter(
            Q(borrower__username__icontains=q)
            | Q(borrower__email__icontains=q)
            | Q(borrower__phone_number__icontains=q)
            | Q(borrower__first_name__icontains=q)
            | Q(borrower__last_name__icontains=q)
            | Q(borrower__super_agent__username__icontains=q)
            | Q(borrower__super_agent__email__icontains=q)
            | Q(borrower__master_agent__username__icontains=q)
            | Q(borrower__master_agent__email__icontains=q)
        )

    if filters.get('retail_manager_id') and not is_retail_manager(viewer):
        retail_manager_id = filters['retail_manager_id']
        mapped_super_ids = RetailManagerSuperAgentMapping.objects.filter(
            retail_manager_id=retail_manager_id
        ).values_list('super_agent_id', flat=True)
        mapped_master_ids = RetailManagerMasterAgentMapping.objects.filter(
            retail_manager_id=retail_manager_id
        ).values_list('master_agent_id', flat=True)
        direct_agent_ids = RetailManagerAgentMapping.objects.filter(
            retail_manager_id=retail_manager_id
        ).values_list('agent_id', flat=True)
        loans = loans.filter(
            Q(borrower_id__in=direct_agent_ids)
            | Q(borrower__super_agent_id__in=mapped_super_ids)
            | Q(borrower__master_agent_id__in=mapped_master_ids)
        )

    if filters.get('master_agent_id'):
        loans = loans.filter(borrower__master_agent_id=filters['master_agent_id'])
    if filters.get('super_agent_id'):
        loans = loans.filter(borrower__super_agent_id=filters['super_agent_id'])
    if filters.get('agent_identifier'):
        agent_value = filters['agent_identifier']
        loans = loans.filter(
            Q(borrower__username__icontains=agent_value)
            | Q(borrower__email__icontains=agent_value)
            | Q(borrower__phone_number__icontains=agent_value)
        )

    status_filter = filters.get('status') or ''
    if status_filter == 'pending_approval':
        loans = loans.filter(status='pending')
    elif status_filter == 'approved':
        loans = loans.filter(status='active', repaid_amount=Decimal('0.00'))
    elif status_filter == 'rejected':
        loans = loans.filter(status='rejected')
    elif status_filter == 'partially_repaid':
        loans = loans.filter(
            status__in=['active', 'overdue', 'defaulted'],
            outstanding_balance__gt=Decimal('0.00'),
            repaid_amount__gt=Decimal('0.00'),
        )
    elif status_filter == 'fully_settled':
        loans = loans.filter(status='settled')
    elif status_filter == 'overdue':
        loans = loans.filter(
            status__in=['active', 'overdue', 'defaulted'],
            outstanding_balance__gt=Decimal('0.00'),
            due_date__lt=now,
        )
    elif status_filter == 'defaulted':
        loans = loans.filter(status='defaulted')
    elif status_filter == 'locked':
        loans = loans.filter(Q(account_locked_due_to_default=True) | Q(borrower__is_locked=True))

    if filters.get('withdrawal_locked') == 'yes':
        loans = loans.filter(Q(outstanding_balance__gt=Decimal('0.00')) | Q(borrower__withdrawal_locked=True))
    elif filters.get('withdrawal_locked') == 'no':
        loans = loans.filter(outstanding_balance__lte=Decimal('0.00'), borrower__withdrawal_locked=False)

    if filters.get('account_locked') == 'yes':
        loans = loans.filter(Q(account_locked_due_to_default=True) | Q(borrower__is_locked=True))
    elif filters.get('account_locked') == 'no':
        loans = loans.filter(account_locked_due_to_default=False, borrower__is_locked=False)

    issued_by = filters.get('issued_by') or ''
    if issued_by:
        loans = loans.filter(
            Q(approved_by__username__icontains=issued_by)
            | Q(approved_by__email__icontains=issued_by)
            | Q(lender__username__icontains=issued_by)
            | Q(lender__email__icontains=issued_by)
        )

    if filters.get('outstanding_only') in {'1', 'true', 'yes', 'on'}:
        loans = loans.filter(outstanding_balance__gt=Decimal('0.00'))

    if filters.get('overdue_only') in {'1', 'true', 'yes', 'on'}:
        loans = loans.filter(
            status__in=['active', 'overdue', 'defaulted'],
            outstanding_balance__gt=Decimal('0.00'),
            due_date__lt=now,
        )

    return loans


def _build_loan_reporting_retail_manager_map(loans):
    borrower_ids = set()
    super_agent_ids = set()
    master_agent_ids = set()
    for loan in loans:
        borrower_ids.add(loan.borrower_id)
        if getattr(loan.borrower, 'super_agent_id', None):
            super_agent_ids.add(loan.borrower.super_agent_id)
        if getattr(loan.borrower, 'master_agent_id', None):
            master_agent_ids.add(loan.borrower.master_agent_id)

    rm_by_agent = {}
    for row in RetailManagerAgentMapping.objects.filter(agent_id__in=borrower_ids).select_related('retail_manager'):
        rm_by_agent.setdefault(row.agent_id, row.retail_manager)

    rm_by_super = {}
    for row in RetailManagerSuperAgentMapping.objects.filter(super_agent_id__in=super_agent_ids).select_related('retail_manager'):
        rm_by_super.setdefault(row.super_agent_id, row.retail_manager)

    rm_by_master = {}
    for row in RetailManagerMasterAgentMapping.objects.filter(master_agent_id__in=master_agent_ids).select_related('retail_manager'):
        rm_by_master.setdefault(row.master_agent_id, row.retail_manager)

    mapping = {}
    for loan in loans:
        borrower = loan.borrower
        mapping[loan.id] = (
            rm_by_agent.get(borrower.id)
            or rm_by_super.get(getattr(borrower, 'super_agent_id', None))
            or rm_by_master.get(getattr(borrower, 'master_agent_id', None))
        )
    return mapping


def _loan_reporting_row_dict(loan, *, retail_manager_obj=None, include_retail_manager=False, now=None):
    now = now or timezone.now()
    borrower = loan.borrower
    super_agent = getattr(borrower, 'super_agent', None)
    master_agent = getattr(borrower, 'master_agent', None)
    issued_by_user = loan.approved_by or loan.lender
    withdrawal_locked = bool(loan.outstanding_balance > Decimal('0.00') or getattr(borrower, 'withdrawal_locked', False))
    account_locked = bool(getattr(borrower, 'is_locked', False) or loan.account_locked_due_to_default)
    issued_dt = timezone.localtime(loan.created_at) if loan.created_at else None
    due_dt = timezone.localtime(loan.due_date) if loan.due_date else None

    row = {
        'loan': loan,
        'detail_url': reverse('betting:overdraft_report_detail', args=[loan.id]),
        'agent_username': borrower.username or borrower.email or '',
        'agent_full_name': borrower.get_full_name() or borrower.email or borrower.username or '',
        'agent_phone_number': borrower.phone_number or '-',
        'agent_email': borrower.email or '',
        'super_agent': _loan_reporting_actor_label(super_agent) if super_agent else '-',
        'master_agent': _loan_reporting_actor_label(master_agent) if master_agent else '-',
        'amount_given': loan.amount or Decimal('0.00'),
        'outstanding_balance': loan.outstanding_balance or Decimal('0.00'),
        'qualified_amount': loan.qualified_amount or Decimal('0.00'),
        'status': _loan_reporting_status_label(loan),
        'withdrawal_locked': 'Yes' if withdrawal_locked else 'No',
        'account_locked': 'Yes' if account_locked else 'No',
        'date_issued': issued_dt.strftime('%Y-%m-%d') if issued_dt else '',
        'time_issued': issued_dt.strftime('%H:%M:%S') if issued_dt else '',
        'issued_by': _loan_reporting_actor_label(issued_by_user),
        'repayment_due_date': due_dt.strftime('%Y-%m-%d %H:%M:%S') if due_dt else '',
        'repayment_status': _loan_reporting_repayment_status(loan),
        'days_outstanding': _loan_reporting_days_outstanding(loan, now=now),
        'days_overdue': _loan_reporting_days_overdue(loan, now=now),
    }
    if include_retail_manager:
        row['retail_manager'] = _loan_reporting_actor_label(retail_manager_obj) if retail_manager_obj else '-'
    return row


def _build_loan_reporting_dataset(user, *, filters=None, include_retail_manager=False):
    filters = filters or {}
    now = timezone.now()
    filtered_loans = list(_apply_loan_reporting_filters(_loan_reporting_scope_queryset(user), filters=filters, viewer=user))
    retail_map = _build_loan_reporting_retail_manager_map(filtered_loans) if include_retail_manager else {}
    rows = [
        _loan_reporting_row_dict(
            loan,
            retail_manager_obj=retail_map.get(loan.id),
            include_retail_manager=include_retail_manager,
            now=now,
        )
        for loan in filtered_loans
    ]

    summary_base = Loan.objects.filter(id__in=[loan.id for loan in filtered_loans])
    summary = summary_base.aggregate(
        total_issued=Coalesce(Sum('amount'), Decimal('0.00')),
        total_outstanding=Coalesce(Sum('outstanding_balance'), Decimal('0.00')),
    )
    summary['total_settled'] = (
        summary_base.filter(status='settled').aggregate(total=Coalesce(Sum('amount'), Decimal('0.00')))['total']
        or Decimal('0.00')
    )
    summary['total_overdue'] = (
        summary_base.filter(
            status__in=['active', 'overdue', 'defaulted'],
            outstanding_balance__gt=Decimal('0.00'),
            due_date__lt=now,
        ).aggregate(total=Coalesce(Sum('outstanding_balance'), Decimal('0.00')))['total']
        or Decimal('0.00')
    )
    borrower_ids = list(summary_base.values_list('borrower_id', flat=True))
    summary['total_locked_accounts'] = User.objects.filter(id__in=borrower_ids, is_locked=True).count()
    summary['total_withdrawal_locked'] = (
        User.objects.filter(id__in=borrower_ids, withdrawal_locked=True).count()
        + summary_base.filter(outstanding_balance__gt=Decimal('0.00'))
        .exclude(borrower__withdrawal_locked=True)
        .values('borrower_id')
        .distinct()
        .count()
    )
    summary['total_records'] = len(rows)

    if is_retail_manager(user):
        retail_manager_options = [user]
        master_agent_options = list(get_retail_manager_master_agents(user).order_by('username', 'email')[:200])
        super_agent_options = list(get_retail_manager_super_agents(user).order_by('username', 'email')[:200])
        agent_options = list(get_retail_manager_agents(user).order_by('username', 'email')[:500])
    else:
        retail_manager_options = list(User.objects.filter(user_type='retail_manager').only('id', 'username', 'email').order_by('username', 'email')[:200])
        master_agent_options = list(User.objects.filter(user_type='master_agent').only('id', 'username', 'email').order_by('username', 'email')[:300])
        super_agent_options = list(User.objects.filter(user_type='super_agent').only('id', 'username', 'email').order_by('username', 'email')[:500])
        agent_options = list(User.objects.filter(user_type='agent').only('id', 'username', 'email').order_by('username', 'email')[:1000])

    return {
        'rows': rows,
        'summary': summary,
        'filters': filters,
        'retail_manager_options': retail_manager_options,
        'master_agent_options': master_agent_options,
        'super_agent_options': super_agent_options,
        'agent_options': agent_options,
        'status_choices': [
            ('pending_approval', 'Pending Approval'),
            ('approved', 'Approved'),
            ('rejected', 'Rejected'),
            ('partially_repaid', 'Partially Repaid'),
            ('fully_settled', 'Fully Settled'),
            ('overdue', 'Overdue'),
            ('defaulted', 'Defaulted'),
            ('locked', 'Locked'),
        ],
    }


def _loan_reporting_export_rows(rows, *, include_retail_manager=False):
    export_rows = []
    for row in rows:
        payload = {
            'agent_username': row['agent_username'],
            'agent_full_name': row['agent_full_name'],
            'agent_phone_number': row['agent_phone_number'],
            'agent_email': row['agent_email'],
            'super_agent': row['super_agent'],
            'master_agent': row['master_agent'],
            'amount_of_overdraft_given': str(row['amount_given']),
            'outstanding_balance': str(row['outstanding_balance']),
            'qualified_amount': str(row['qualified_amount']),
            'status': row['status'],
            'withdrawal_locked': row['withdrawal_locked'],
            'account_downline_locked': row['account_locked'],
            'date_issued': row['date_issued'],
            'time_issued': row['time_issued'],
            'issued_by': row['issued_by'],
            'repayment_due_date': row['repayment_due_date'],
            'repayment_status': row['repayment_status'],
            'days_outstanding': row['days_outstanding'],
            'days_overdue': row['days_overdue'],
        }
        if include_retail_manager:
            payload['retail_manager'] = row['retail_manager']
        export_rows.append(payload)
    return export_rows


def _loan_reporting_querystring(filters, extra=None):
    qd = QueryDict('', mutable=True)
    for key, value in (filters or {}).items():
        if value not in (None, '', False):
            qd[key] = str(value)
    for key, value in (extra or {}).items():
        if value not in (None, '', False):
            qd[key] = str(value)
    return qd.urlencode()


def _build_overdraft_reporting_dashboard_context(
    request,
    *,
    include_retail_manager=False,
    extra_params=None,
    page_param='loan_page',
    per_page=50,
):
    filters = _loan_reporting_filters_from_request(request)
    dataset = _build_loan_reporting_dataset(
        request.user,
        filters=filters,
        include_retail_manager=include_retail_manager,
    )
    page = Paginator(dataset['rows'], per_page).get_page(request.GET.get(page_param) or 1)
    return {
        'overdraft_reporting': dataset,
        'overdraft_reporting_page': page,
        'overdraft_reporting_filters': filters,
        'overdraft_reporting_querystring': _loan_reporting_querystring(filters, extra=extra_params or {}),
        'overdraft_reporting_reset_querystring': _loan_reporting_querystring({}, extra=extra_params or {}),
        'overdraft_reporting_page_param': page_param,
        'overdraft_reporting_extra_params': extra_params or {},
    }


def _build_overdraft_detail_context(viewer, loan):
    scoped_loan = _loan_reporting_scope_queryset(viewer).filter(id=loan.id).first()
    if not scoped_loan:
        raise Http404()

    retail_map = _build_loan_reporting_retail_manager_map([scoped_loan])
    requested_by = getattr(getattr(scoped_loan, 'credit_request', None), 'requester', None)
    audit_logs = list(scoped_loan.audit_logs.select_related('performed_by').order_by('-created_at')[:100])
    audit_summary = {
        'who_requested': {'actor': _loan_reporting_actor_label(requested_by) if requested_by else '-', 'timestamp': '', 'ip_address': ''},
        'who_approved': {'actor': _loan_reporting_actor_label(scoped_loan.approved_by) if scoped_loan.approved_by else '-', 'timestamp': '', 'ip_address': ''},
        'who_rejected': {'actor': _loan_reporting_actor_label(scoped_loan.rejected_by) if scoped_loan.rejected_by else '-', 'timestamp': '', 'ip_address': ''},
        'who_issued': {'actor': _loan_reporting_actor_label(scoped_loan.approved_by or scoped_loan.lender), 'timestamp': '', 'ip_address': ''},
        'who_modified': {'actor': '-', 'timestamp': '', 'ip_address': ''},
        'who_locked_account': {'actor': '-', 'timestamp': '', 'ip_address': ''},
        'who_unlocked_account': {'actor': '-', 'timestamp': '', 'ip_address': ''},
    }
    if scoped_loan.credit_request and scoped_loan.credit_request.created_at:
        audit_summary['who_requested']['timestamp'] = timezone.localtime(scoped_loan.credit_request.created_at).strftime('%Y-%m-%d %H:%M:%S')
    if scoped_loan.approved_at:
        audit_summary['who_approved']['timestamp'] = timezone.localtime(scoped_loan.approved_at).strftime('%Y-%m-%d %H:%M:%S')
        audit_summary['who_issued']['timestamp'] = timezone.localtime(scoped_loan.approved_at).strftime('%Y-%m-%d %H:%M:%S')
    elif scoped_loan.created_at:
        audit_summary['who_issued']['timestamp'] = timezone.localtime(scoped_loan.created_at).strftime('%Y-%m-%d %H:%M:%S')
    if scoped_loan.rejected_at:
        audit_summary['who_rejected']['timestamp'] = timezone.localtime(scoped_loan.rejected_at).strftime('%Y-%m-%d %H:%M:%S')
    for entry in audit_logs:
        entry_ts = timezone.localtime(entry.created_at).strftime('%Y-%m-%d %H:%M:%S') if entry.created_at else ''
        entry_ip = entry.ip_address or ''
        entry_actor = _loan_reporting_actor_label(entry.performed_by)
        if audit_summary['who_modified']['actor'] == '-' and entry.action == 'override':
            audit_summary['who_modified'] = {'actor': entry_actor, 'timestamp': entry_ts, 'ip_address': entry_ip}
        if audit_summary['who_locked_account']['actor'] == '-' and entry.action == 'account_locked':
            audit_summary['who_locked_account'] = {'actor': entry_actor, 'timestamp': entry_ts, 'ip_address': entry_ip}
        if audit_summary['who_unlocked_account']['actor'] == '-' and entry.action == 'account_unlocked':
            audit_summary['who_unlocked_account'] = {'actor': entry_actor, 'timestamp': entry_ts, 'ip_address': entry_ip}
        if audit_summary['who_issued']['ip_address'] == '' and entry.action in ['manual_assigned', 'loan_approved']:
            audit_summary['who_issued']['ip_address'] = entry_ip
        if audit_summary['who_approved']['ip_address'] == '' and entry.action == 'loan_approved':
            audit_summary['who_approved']['ip_address'] = entry_ip
        if audit_summary['who_rejected']['ip_address'] == '' and entry.action == 'loan_rejected':
            audit_summary['who_rejected']['ip_address'] = entry_ip
        if audit_summary['who_requested']['ip_address'] == '' and entry.action == 'requested':
            audit_summary['who_requested']['ip_address'] = entry_ip

    audit_requirement_rows = [
        {'label': 'Who Requested', **audit_summary['who_requested']},
        {'label': 'Who Approved', **audit_summary['who_approved']},
        {'label': 'Who Rejected', **audit_summary['who_rejected']},
        {'label': 'Who Issued', **audit_summary['who_issued']},
        {'label': 'Who Modified', **audit_summary['who_modified']},
        {'label': 'Who Locked Account', **audit_summary['who_locked_account']},
        {'label': 'Who Unlocked Account', **audit_summary['who_unlocked_account']},
    ]

    approval_history = []
    if requested_by:
        approval_history.append({
            'label': 'Who Requested',
            'actor': _loan_reporting_actor_label(requested_by),
            'timestamp': timezone.localtime(scoped_loan.credit_request.created_at).strftime('%Y-%m-%d %H:%M:%S') if scoped_loan.credit_request and scoped_loan.credit_request.created_at else '',
            'ip_address': '',
        })
    if scoped_loan.approved_by:
        approval_history.append({
            'label': 'Who Approved',
            'actor': _loan_reporting_actor_label(scoped_loan.approved_by),
            'timestamp': timezone.localtime(scoped_loan.approved_at).strftime('%Y-%m-%d %H:%M:%S') if scoped_loan.approved_at else '',
            'ip_address': '',
        })
    if scoped_loan.rejected_by:
        approval_history.append({
            'label': 'Who Rejected',
            'actor': _loan_reporting_actor_label(scoped_loan.rejected_by),
            'timestamp': timezone.localtime(scoped_loan.rejected_at).strftime('%Y-%m-%d %H:%M:%S') if scoped_loan.rejected_at else '',
            'ip_address': '',
        })

    return {
        'loan': scoped_loan,
        'borrower': scoped_loan.borrower,
        'retail_manager_label': _loan_reporting_actor_label(retail_map.get(scoped_loan.id)),
        'row': _loan_reporting_row_dict(
            scoped_loan,
            retail_manager_obj=retail_map.get(scoped_loan.id),
            include_retail_manager=True,
        ),
        'loan_history': list(Loan.objects.filter(borrower=scoped_loan.borrower).select_related('approved_by', 'rejected_by', 'lender').order_by('-created_at')[:20]),
        'repayment_history': list(scoped_loan.repayments.select_related('recorded_by', 'source_transaction').order_by('-created_at')[:30]),
        'deposit_history': list(Transaction.objects.filter(user=scoped_loan.borrower, transaction_type='deposit', status='completed', is_successful=True).order_by('-timestamp')[:30]),
        'approval_history': approval_history,
        'audit_logs': audit_logs,
        'audit_summary': audit_summary,
        'audit_requirement_rows': audit_requirement_rows,
        'qualified_amount_percentage': get_loan_settings().get('loan_percentage'),
    }


CRM_OPS_EXCLUDED_USER_TYPES = ['admin', 'crm', 'finance', 'account_user']


def _ops_scope_users_queryset(user):
    if is_retail_manager(user):
        return get_retail_network_users_qs(user)
    return User.objects.exclude(is_superuser=True)


def _ops_targetable_users_queryset(user):
    return _ops_scope_users_queryset(user).exclude(user_type__in=CRM_OPS_EXCLUDED_USER_TYPES)


def _retail_manager_scoped_user_ids(retail_manager_id):
    try:
        retail_user = User.objects.get(id=int(retail_manager_id), user_type='retail_manager')
    except Exception:
        return []
    return list(get_retail_network_users_qs(retail_user).values_list('id', flat=True))


def _retail_manager_scoped_agent_ids(retail_manager_id):
    try:
        retail_user = User.objects.get(id=int(retail_manager_id), user_type='retail_manager')
    except Exception:
        return []
    return list(get_retail_manager_agents(retail_user).values_list('id', flat=True))


def _dormant_scope_agents_queryset(user):
    base_qs = User.objects.filter(user_type='agent')
    if not getattr(user, 'is_authenticated', False):
        return base_qs.none()
    if user.is_superuser or user.user_type in ['admin', 'crm']:
        return base_qs
    if is_retail_manager(user):
        return get_retail_manager_agents(user)
    if user.user_type == 'master_agent':
        return base_qs.filter(Q(master_agent=user) | Q(super_agent__master_agent=user)).distinct()
    if user.user_type == 'super_agent':
        return base_qs.filter(super_agent=user)
    if user.user_type == 'agent':
        return base_qs.filter(id=user.id)
    return base_qs.none()


def _annotate_dormant_agent_queryset(qs):
    WalletLedgerEntry = apps.get_model('betting', 'WalletLedgerEntry')
    wallet_balance_subq = Wallet.objects.filter(user_id=OuterRef('pk')).values('balance')[:1]
    agent_last_bet_qs = (
        BetTicket.objects.exclude(status__in=['deleted', 'cancelled'])
        .filter(user_id=OuterRef('pk'))
        .order_by('-placed_at')
    )
    cashier_last_bet_qs = (
        BetTicket.objects.exclude(status__in=['deleted', 'cancelled'])
        .filter(user__agent_id=OuterRef('pk'), user__user_type='cashier')
        .order_by('-placed_at')
    )
    agent_last_transaction_qs = (
        Transaction.objects.filter(user_id=OuterRef('pk')).order_by('-timestamp')
    )
    cashier_last_transaction_qs = (
        Transaction.objects.filter(user__agent_id=OuterRef('pk'), user__user_type='cashier').order_by('-timestamp')
    )
    agent_last_deposit_qs = (
        Transaction.objects.filter(
            user_id=OuterRef('pk'),
            transaction_type='deposit',
            status='completed',
            is_successful=True,
        ).order_by('-timestamp')
    )
    cashier_last_deposit_qs = (
        Transaction.objects.filter(
            user__agent_id=OuterRef('pk'),
            user__user_type='cashier',
            transaction_type='deposit',
            status='completed',
            is_successful=True,
        ).order_by('-timestamp')
    )
    agent_last_wallet_activity_qs = (
        WalletLedgerEntry.objects.filter(user_id=OuterRef('pk')).order_by('-created_at')
    )
    cashier_last_wallet_activity_qs = (
        WalletLedgerEntry.objects.filter(user__agent_id=OuterRef('pk'), user__user_type='cashier').order_by('-created_at')
    )
    cashier_last_login_qs = (
        User.objects.filter(agent_id=OuterRef('pk'), user_type='cashier')
        .exclude(last_login__isnull=True)
        .order_by('-last_login')
    )
    return qs.select_related('super_agent', 'master_agent').annotate(
        wallet_balance_annotated=Coalesce(Subquery(wallet_balance_subq), Value(0), output_field=DecimalField()),
        cashiers_count=Count('agents_under', filter=Q(agents_under__user_type='cashier'), distinct=True),
        agent_last_bet_at=Subquery(agent_last_bet_qs.values('placed_at')[:1]),
        cashier_last_bet_at=Subquery(cashier_last_bet_qs.values('placed_at')[:1]),
        agent_last_transaction_at=Subquery(agent_last_transaction_qs.values('timestamp')[:1]),
        cashier_last_transaction_at=Subquery(cashier_last_transaction_qs.values('timestamp')[:1]),
        agent_last_deposit_at=Subquery(agent_last_deposit_qs.values('timestamp')[:1]),
        cashier_last_deposit_at=Subquery(cashier_last_deposit_qs.values('timestamp')[:1]),
        agent_last_wallet_activity_at=Subquery(agent_last_wallet_activity_qs.values('created_at')[:1]),
        cashier_last_wallet_activity_at=Subquery(cashier_last_wallet_activity_qs.values('created_at')[:1]),
        cashier_last_login=Subquery(cashier_last_login_qs.values('last_login')[:1]),
    )


def _annotate_user_engagement(qs):
    wallet_balance_subq = Wallet.objects.filter(user_id=OuterRef('pk')).values('balance')[:1]
    latest_bet_qs = (
        BetTicket.objects.exclude(status__in=['deleted', 'cancelled'])
        .filter(user_id=OuterRef('pk'))
        .order_by('-placed_at')
    )
    latest_deposit_qs = (
        Transaction.objects.filter(
            user_id=OuterRef('pk'),
            transaction_type='deposit',
            status='completed',
            is_successful=True,
        ).order_by('-timestamp')
    )
    bet_count_qs = (
        BetTicket.objects.exclude(status__in=['deleted', 'cancelled'])
        .filter(user_id=OuterRef('pk'))
        .values('user_id')
        .annotate(total=Count('id'))
        .values('total')[:1]
    )
    deposit_count_qs = (
        Transaction.objects.filter(
            user_id=OuterRef('pk'),
            transaction_type='deposit',
            status='completed',
            is_successful=True,
        ).values('user_id').annotate(total=Count('id')).values('total')[:1]
    )
    deposit_sum_qs = (
        Transaction.objects.filter(
            user_id=OuterRef('pk'),
            transaction_type='deposit',
            status='completed',
            is_successful=True,
        ).values('user_id').annotate(total=Sum('amount')).values('total')[:1]
    )
    return qs.select_related('wallet', 'agent', 'super_agent', 'master_agent').annotate(
        wallet_balance_annotated=Coalesce(Subquery(wallet_balance_subq), Value(0), output_field=DecimalField()),
        last_bet_at=Subquery(latest_bet_qs.values('placed_at')[:1]),
        last_deposit_at=Subquery(latest_deposit_qs.values('timestamp')[:1]),
        bets_count=Coalesce(Subquery(bet_count_qs), Value(0), output_field=IntegerField()),
        deposits_count=Coalesce(Subquery(deposit_count_qs), Value(0), output_field=IntegerField()),
        deposits_amount=Coalesce(Subquery(deposit_sum_qs), Value(0), output_field=DecimalField()),
    )


def _apply_ops_user_filters(
    qs,
    *,
    query='',
    user_type='',
    agent_id='',
    super_agent_id='',
    retail_manager_id='',
    status='',
):
    if query:
        qs = qs.filter(
            Q(username__icontains=query) |
            Q(email__icontains=query) |
            Q(first_name__icontains=query) |
            Q(last_name__icontains=query) |
            Q(other_name__icontains=query) |
            Q(phone_number__icontains=query)
        )
    if user_type:
        qs = qs.filter(user_type=user_type)
    if agent_id:
        try:
            agent_id_int = int(agent_id)
            qs = qs.filter(Q(id=agent_id_int) | Q(agent_id=agent_id_int))
        except Exception:
            pass
    if super_agent_id:
        try:
            super_agent_id_int = int(super_agent_id)
            qs = qs.filter(Q(id=super_agent_id_int) | Q(super_agent_id=super_agent_id_int))
        except Exception:
            pass
    if retail_manager_id:
        scoped_ids = _retail_manager_scoped_user_ids(retail_manager_id)
        qs = qs.filter(id__in=scoped_ids) if scoped_ids else qs.none()
    if status == 'active':
        qs = qs.filter(is_active=True)
    elif status == 'inactive':
        qs = qs.filter(is_active=False)
    elif status == 'locked':
        qs = qs.filter(is_locked=True)
    return qs


def _apply_dormant_agent_filters(
    qs,
    *,
    query='',
    user_type='',
    agent_id='',
    super_agent_id='',
    retail_manager_id='',
    status='',
):
    if user_type and user_type != 'agent':
        return qs.none()
    if query:
        qs = qs.filter(
            Q(username__icontains=query) |
            Q(email__icontains=query) |
            Q(first_name__icontains=query) |
            Q(last_name__icontains=query) |
            Q(other_name__icontains=query) |
            Q(phone_number__icontains=query)
        )
    if agent_id:
        try:
            qs = qs.filter(id=int(agent_id))
        except Exception:
            pass
    if super_agent_id:
        try:
            qs = qs.filter(super_agent_id=int(super_agent_id))
        except Exception:
            pass
    if retail_manager_id:
        scoped_agent_ids = _retail_manager_scoped_agent_ids(retail_manager_id)
        qs = qs.filter(id__in=scoped_agent_ids) if scoped_agent_ids else qs.none()
    if status == 'active':
        qs = qs.filter(is_active=True)
    elif status == 'inactive':
        qs = qs.filter(is_active=False)
    elif status == 'locked':
        qs = qs.filter(is_locked=True)
    return qs


def _resolve_dormant_reference_at(agent_obj):
    last_activity_at = getattr(agent_obj, 'last_activity_at', None)
    return last_activity_at or getattr(agent_obj, 'date_joined', None)


def _attach_dormant_agent_activity_fields(agent_obj, *, now=None):
    now = now or timezone.now()
    agent_activity_points = [
        getattr(agent_obj, 'last_login', None),
        getattr(agent_obj, 'agent_last_bet_at', None),
        getattr(agent_obj, 'agent_last_transaction_at', None),
        getattr(agent_obj, 'agent_last_wallet_activity_at', None),
    ]
    downline_activity_points = [
        getattr(agent_obj, 'cashier_last_login', None),
        getattr(agent_obj, 'cashier_last_bet_at', None),
        getattr(agent_obj, 'cashier_last_transaction_at', None),
        getattr(agent_obj, 'cashier_last_wallet_activity_at', None),
    ]
    agent_obj.last_agent_activity_at = max([value for value in agent_activity_points if value is not None], default=None)
    agent_obj.last_downline_activity_at = max([value for value in downline_activity_points if value is not None], default=None)
    activity_points = [agent_obj.last_agent_activity_at, agent_obj.last_downline_activity_at]
    agent_obj.last_activity_at = max([value for value in activity_points if value is not None], default=None)
    agent_obj.reference_activity_at = _resolve_dormant_reference_at(agent_obj)
    agent_obj.dormant_days = (now - agent_obj.reference_activity_at).days if agent_obj.reference_activity_at else None
    return agent_obj


def _bucket_match_for_dormancy(agent_obj, bucket, *, login_cutoff_7=None, login_cutoff_14=None, login_cutoff_30=None):
    reference_at = _resolve_dormant_reference_at(agent_obj)
    if bucket == 'login_7':
        return bool(reference_at and reference_at < login_cutoff_7)
    if bucket == 'login_14':
        return bool(reference_at and reference_at < login_cutoff_14)
    if bucket == 'login_30':
        return bool(reference_at and reference_at < login_cutoff_30)
    return False


def _build_dormant_center_dataset(
    scope_user,
    *,
    query='',
    user_type='',
    agent_id='',
    super_agent_id='',
    retail_manager_id='',
    status='',
    bucket='login_7',
    start_dt=None,
    end_dt=None,
):
    now = timezone.now()
    base_qs = _dormant_scope_agents_queryset(scope_user)
    base_qs = _apply_dormant_agent_filters(
        base_qs,
        query=query,
        user_type=user_type,
        agent_id=agent_id,
        super_agent_id=super_agent_id,
        retail_manager_id=retail_manager_id,
        status=status,
    )
    agents = list(
        _annotate_dormant_agent_queryset(base_qs).order_by('username', 'email')
    )
    login_cutoff_7 = now - timedelta(days=7)
    login_cutoff_14 = now - timedelta(days=14)
    login_cutoff_30 = now - timedelta(days=30)
    cards = {
        'login_7': 0,
        'login_14': 0,
        'login_30': 0,
    }
    bucket = bucket if bucket in cards else 'login_30'
    rows = []
    filtered_agents = []
    for agent_obj in agents:
        _attach_dormant_agent_activity_fields(agent_obj, now=now)
        # Dormancy is a current-state signal based on the latest known activity across
        # the agent and mapped cashiers. The dashboard-wide date range is for period
        # analytics and should not suppress dormant counts/rows when an agent last
        # acted before that window.
        filtered_agents.append(agent_obj)
        if _bucket_match_for_dormancy(agent_obj, 'login_7', login_cutoff_7=login_cutoff_7, login_cutoff_14=login_cutoff_14, login_cutoff_30=login_cutoff_30):
            cards['login_7'] += 1
        if _bucket_match_for_dormancy(agent_obj, 'login_14', login_cutoff_7=login_cutoff_7, login_cutoff_14=login_cutoff_14, login_cutoff_30=login_cutoff_30):
            cards['login_14'] += 1
        if _bucket_match_for_dormancy(agent_obj, 'login_30', login_cutoff_7=login_cutoff_7, login_cutoff_14=login_cutoff_14, login_cutoff_30=login_cutoff_30):
            cards['login_30'] += 1
        if _bucket_match_for_dormancy(agent_obj, bucket, login_cutoff_7=login_cutoff_7, login_cutoff_14=login_cutoff_14, login_cutoff_30=login_cutoff_30):
            rows.append(agent_obj)
    trend_chart = {
        'labels': ['7 Days', '14 Days', '30 Days'],
        'values': [cards['login_7'], cards['login_14'], cards['login_30']],
    }
    active_vs_dormant = {
        'labels': ['Active Agents', 'Dormant Agents'],
        'values': [max(len(filtered_agents) - cards['login_30'], 0), cards['login_30']],
    }
    by_super_agent_counts = {}
    for row in rows:
        label = getattr(getattr(row, 'super_agent', None), 'username', None) or getattr(getattr(row, 'super_agent', None), 'email', None) or 'Unassigned'
        by_super_agent_counts[label] = by_super_agent_counts.get(label, 0) + 1
    by_super_agent_chart = {
        'labels': list(by_super_agent_counts.keys()),
        'values': list(by_super_agent_counts.values()),
    }
    return {
        'cards': cards,
        'rows': rows,
        'filtered_agents': filtered_agents,
        'current_bucket_total': len(rows),
        'trend_chart': trend_chart,
        'active_vs_dormant_chart': active_vs_dormant,
        'by_super_agent_chart': by_super_agent_chart,
    }


def _build_dormant_center_data(
    scope_user,
    *,
    query='',
    user_type='',
    agent_id='',
    super_agent_id='',
    retail_manager_id='',
    status='',
    bucket='login_7',
    start_dt=None,
    end_dt=None,
):
    dataset = _build_dormant_center_dataset(
        scope_user,
        query=query,
        user_type=user_type,
        agent_id=agent_id,
        super_agent_id=super_agent_id,
        retail_manager_id=retail_manager_id,
        status=status,
        bucket=bucket,
        start_dt=start_dt,
        end_dt=end_dt,
    )
    return dataset['cards'], dataset['rows']


def _attach_dormant_agent_drilldown(rows):
    agent_rows = list(rows or [])
    agent_ids = [row.id for row in agent_rows if getattr(row, 'id', None)]
    if not agent_ids:
        return agent_rows
    cashier_qs = (
        _annotate_user_engagement(User.objects.filter(user_type='cashier', agent_id__in=agent_ids))
        .select_related('agent')
        .order_by('agent_id', 'username', 'email')
    )
    cashiers_by_agent = {}
    for cashier in cashier_qs:
        cashiers_by_agent.setdefault(cashier.agent_id, []).append(cashier)
    for row in agent_rows:
        row.mapped_cashiers_rows = cashiers_by_agent.get(row.id, [])
    return agent_rows


def _build_activation_center_data(
    scope_user,
    *,
    query='',
    user_type='',
    agent_id='',
    super_agent_id='',
    retail_manager_id='',
    status='',
    category='registered_never_deposited',
):
    cutoff = timezone.now() - timedelta(days=30)
    base_qs = _annotate_user_engagement(_ops_targetable_users_queryset(scope_user))
    base_qs = _apply_ops_user_filters(
        base_qs,
        query=query,
        user_type=user_type,
        agent_id=agent_id,
        super_agent_id=super_agent_id,
        retail_manager_id=retail_manager_id,
        status=status,
    )
    users = list(base_qs.order_by('-date_joined'))

    def _category_match(user_obj, cat):
        bets_count = int(getattr(user_obj, 'bets_count', 0) or 0)
        deposits_count = int(getattr(user_obj, 'deposits_count', 0) or 0)
        if cat == 'registered_never_deposited':
            return deposits_count == 0
        if cat == 'deposited_never_played':
            return deposits_count > 0 and bets_count == 0
        if cat == 'played_once_only':
            return bets_count == 1
        if cat == 'dormant_bettors':
            last_bet_at = getattr(user_obj, 'last_bet_at', None)
            return bets_count > 0 and ((last_bet_at is None) or (last_bet_at < cutoff))
        return True

    cards = {
        'registered_never_deposited': 0,
        'deposited_never_played': 0,
        'played_once_only': 0,
        'dormant_bettors': 0,
    }
    rows = []
    for user_obj in users:
        for cat in cards.keys():
            if _category_match(user_obj, cat):
                cards[cat] += 1
        if _category_match(user_obj, category):
            rows.append(user_obj)
    return cards, rows


def _complaint_scope_queryset(user):
    qs = CustomerComplaint.objects.select_related('user', 'assigned_to', 'created_by').prefetch_related('notes__author')
    if is_retail_manager(user):
        return qs.filter(user__in=get_retail_network_users_qs(user))
    return qs


def _apply_complaint_filters(qs, *, query='', complaint_type='', status='', priority='', start_dt=None, end_dt=None):
    if query:
        qs = qs.filter(
            Q(user__username__icontains=query) |
            Q(user__email__icontains=query) |
            Q(subject__icontains=query) |
            Q(description__icontains=query)
        )
    if complaint_type:
        qs = qs.filter(complaint_type=complaint_type)
    if status:
        qs = qs.filter(status=status)
    if priority:
        qs = qs.filter(priority=priority)
    if start_dt:
        qs = qs.filter(created_at__gte=start_dt)
    if end_dt:
        qs = qs.filter(created_at__lte=end_dt)
    return qs.order_by('-updated_at', '-created_at')


def _deposit_scope_queryset(user):
    qs = Transaction.objects.filter(transaction_type='deposit').select_related('user').order_by('-timestamp')
    if is_retail_manager(user):
        qs = qs.filter(user__in=get_retail_network_users_qs(user))
    return qs


def _build_deposit_monitoring_data(user, *, start_dt=None, end_dt=None, status='', gateway='', flag=''):
    site_config = SiteConfiguration.load()
    large_threshold = site_config.crm_large_deposit_threshold or Decimal('100000.00')
    repeat_threshold = int(site_config.crm_failed_deposit_repeat_threshold or 3)
    base_qs = _deposit_scope_queryset(user)
    if start_dt:
        base_qs = base_qs.filter(timestamp__gte=start_dt)
    if end_dt:
        base_qs = base_qs.filter(timestamp__lte=end_dt)
    if status:
        base_qs = base_qs.filter(status=status)
    if gateway:
        base_qs = base_qs.filter(payment_gateway=gateway)

    failed_user_count_map = {
        row['user_id']: int(row['attempts'] or 0)
        for row in (
            base_qs.filter(status='failed')
            .order_by()
            .values('user_id')
            .annotate(attempts=Count('id'))
        )
    }
    rows = []
    for tx in base_qs:
        attempts = failed_user_count_map.get(tx.user_id, 0)
        is_large = (tx.amount or Decimal('0.00')) >= large_threshold
        is_repeat_failed = attempts >= repeat_threshold and tx.status == 'failed'
        if flag == 'large' and not is_large:
            continue
        if flag == 'repeat_failed' and not is_repeat_failed:
            continue
        rows.append({
            'transaction': tx,
            'attempts': attempts,
            'is_large': is_large,
            'is_repeat_failed': is_repeat_failed,
        })

    today = timezone.localdate()
    today_start = timezone.make_aware(datetime.combine(today, datetime.min.time()))
    today_end = timezone.make_aware(datetime.combine(today, datetime.max.time()))
    today_qs = _deposit_scope_queryset(user).filter(timestamp__gte=today_start, timestamp__lte=today_end)
    cards = {
        'successful_today': today_qs.filter(status='completed', is_successful=True).count(),
        'pending': _deposit_scope_queryset(user).filter(status='pending').count(),
        'failed': _deposit_scope_queryset(user).filter(status='failed').count(),
        'large': _deposit_scope_queryset(user).filter(amount__gte=large_threshold).count(),
        'repeat_failed': sum(1 for count in failed_user_count_map.values() if count >= repeat_threshold),
        'large_threshold': large_threshold,
        'repeat_threshold': repeat_threshold,
    }
    return cards, rows


def _entity_descendant_user_ids(entity):
    entity_type = getattr(entity, 'user_type', '')
    if entity_type == 'retail_manager':
        return list(get_retail_network_users_qs(entity).values_list('id', flat=True))
    if entity_type == 'super_agent':
        agent_ids = list(User.objects.filter(super_agent=entity, user_type='agent').values_list('id', flat=True))
        return list(
            User.objects.filter(
                Q(id=entity.id) |
                Q(id__in=agent_ids) |
                Q(agent_id__in=agent_ids)
            ).values_list('id', flat=True)
        )
    if entity_type == 'agent':
        return list(User.objects.filter(Q(id=entity.id) | Q(agent=entity)).values_list('id', flat=True))
    return [entity.id]


def _build_agent_performance_rows(scope_user, *, entity_type='super_agent', start_dt=None, end_dt=None, query=''):
    if entity_type == 'retail_manager':
        entities_qs = User.objects.filter(user_type='retail_manager')
    elif entity_type == 'agent':
        if is_retail_manager(scope_user):
            entities_qs = get_retail_manager_agents(scope_user)
        else:
            entities_qs = User.objects.filter(user_type='agent')
    else:
        if is_retail_manager(scope_user):
            entities_qs = get_retail_manager_super_agents(scope_user)
        else:
            entities_qs = User.objects.filter(user_type='super_agent')
    if query:
        entities_qs = entities_qs.filter(Q(username__icontains=query) | Q(email__icontains=query) | Q(first_name__icontains=query) | Q(last_name__icontains=query))

    rows = []
    chart_user_ids = set()
    for entity in entities_qs.select_related('master_agent').order_by('username', 'email'):
        descendant_ids = _entity_descendant_user_ids(entity)
        if not descendant_ids:
            continue
        chart_user_ids.update(descendant_ids)
        tickets_qs = BetTicket.objects.exclude(status__in=['deleted', 'cancelled']).filter(user_id__in=descendant_ids)
        if start_dt:
            tickets_qs = tickets_qs.filter(placed_at__gte=start_dt)
        if end_dt:
            tickets_qs = tickets_qs.filter(placed_at__lte=end_dt)
        deposits_qs = Transaction.objects.filter(user_id__in=descendant_ids, transaction_type='deposit', status='completed', is_successful=True)
        withdrawals_qs = UserWithdrawal.objects.filter(user_id__in=descendant_ids)
        bonuses_qs = Transaction.objects.filter(user_id__in=descendant_ids, transaction_type='bonus', status='completed', is_successful=True)
        if start_dt:
            deposits_qs = deposits_qs.filter(timestamp__gte=start_dt)
            withdrawals_qs = withdrawals_qs.filter(request_time__gte=start_dt)
            bonuses_qs = bonuses_qs.filter(timestamp__gte=start_dt)
        if end_dt:
            deposits_qs = deposits_qs.filter(timestamp__lte=end_dt)
            withdrawals_qs = withdrawals_qs.filter(request_time__lte=end_dt)
            bonuses_qs = bonuses_qs.filter(timestamp__lte=end_dt)

        turnover = tickets_qs.aggregate(v=Coalesce(Sum('stake_amount'), Value(0), output_field=DecimalField()))['v'] or Decimal('0.00')
        payouts = tickets_qs.filter(status='won').aggregate(v=Coalesce(Sum('max_winning'), Value(0), output_field=DecimalField()))['v'] or Decimal('0.00')
        ggr = turnover - payouts
        bonus_cost = bonuses_qs.aggregate(v=Coalesce(Sum('amount'), Value(0), output_field=DecimalField()))['v'] or Decimal('0.00')
        net_ggr = ggr - bonus_cost
        commission_amount = Transaction.objects.filter(
            user=entity,
            transaction_type='commission_payout',
            status='completed',
            is_successful=True,
            timestamp__gte=start_dt if start_dt else datetime.min.replace(tzinfo=timezone.get_current_timezone()),
            timestamp__lte=end_dt if end_dt else timezone.now(),
        ).aggregate(v=Coalesce(Sum('amount'), Value(0), output_field=DecimalField()))['v'] or Decimal('0.00')
        active_users = tickets_qs.values('user_id').distinct().count()
        dormant_users = User.objects.filter(id__in=descendant_ids).filter(Q(last_login__lt=timezone.now() - timedelta(days=30)) | Q(last_login__isnull=True)).count()
        deposit_volume = deposits_qs.aggregate(v=Coalesce(Sum('amount'), Value(0), output_field=DecimalField()))['v'] or Decimal('0.00')
        withdrawal_volume = withdrawals_qs.aggregate(v=Coalesce(Sum('amount'), Value(0), output_field=DecimalField()))['v'] or Decimal('0.00')
        underlying_agents = User.objects.filter(id__in=descendant_ids, user_type='agent').count()
        cashiers_count = User.objects.filter(id__in=descendant_ids, user_type='cashier').count()
        active_bettors = active_users
        tickets_sold = tickets_qs.count()
        average_stake = (turnover / Decimal(tickets_sold)) if tickets_sold else Decimal('0.00')
        winning_percentage = (Decimal(tickets_qs.filter(status='won').count()) / Decimal(tickets_sold) * Decimal('100.00')) if tickets_sold else Decimal('0.00')
        rows.append({
            'entity': entity,
            'turnover': turnover,
            'ggr': ggr,
            'net_ggr': net_ggr,
            'commission': commission_amount,
            'active_users': active_users,
            'dormant_users': dormant_users,
            'deposit_volume': deposit_volume,
            'withdrawal_volume': withdrawal_volume,
            'cashiers_count': cashiers_count,
            'agents_count': underlying_agents,
            'active_bettors': active_bettors,
            'tickets_sold': tickets_sold,
            'average_stake': average_stake,
            'winning_percentage': winning_percentage.quantize(Decimal('0.01')) if tickets_sold else Decimal('0.00'),
        })
    rows.sort(key=lambda row: row['turnover'], reverse=True)

    chart_labels = []
    turnover_series = []
    ggr_series = []
    commission_series = []
    if chart_user_ids:
        daily_qs = BetTicket.objects.exclude(status__in=['deleted', 'cancelled']).filter(user_id__in=list(chart_user_ids))
        if start_dt:
            daily_qs = daily_qs.filter(placed_at__gte=start_dt)
        if end_dt:
            daily_qs = daily_qs.filter(placed_at__lte=end_dt)
        daily_rows = (
            daily_qs.annotate(day=TruncDate('placed_at'))
            .values('day')
            .annotate(
                turnover=Coalesce(Sum('stake_amount'), Value(0), output_field=DecimalField()),
                payouts=Coalesce(Sum(Case(When(status='won', then='max_winning'), default=Value(0), output_field=DecimalField())), Value(0), output_field=DecimalField()),
            ).order_by('day')
        )
        commission_daily = (
            Transaction.objects.filter(
                user_id__in=list(chart_user_ids),
                transaction_type='commission_payout',
                status='completed',
                is_successful=True,
            )
            .filter(timestamp__gte=start_dt if start_dt else datetime.min.replace(tzinfo=timezone.get_current_timezone()))
            .filter(timestamp__lte=end_dt if end_dt else timezone.now())
            .annotate(day=TruncDate('timestamp'))
            .values('day')
            .annotate(total=Coalesce(Sum('amount'), Value(0), output_field=DecimalField()))
            .order_by('day')
        )
        commission_map = {row['day']: row['total'] for row in commission_daily}
        for row in daily_rows:
            day = row['day']
            turnover_value = row['turnover'] or Decimal('0.00')
            ggr_value = turnover_value - (row['payouts'] or Decimal('0.00'))
            chart_labels.append(day.isoformat())
            turnover_series.append(float(turnover_value))
            ggr_series.append(float(ggr_value))
            commission_series.append(float(commission_map.get(day, Decimal('0.00'))))

    return rows, {
        'labels': chart_labels,
        'turnover': turnover_series,
        'ggr': ggr_series,
        'commission': commission_series,
    }


def _log_crm_ops_action(request, *, module, action, target_user=None, complaint=None, campaign=None, transaction_obj=None, metadata=None):
    CRMOpsAuditLog.objects.create(
        actor=request.user if getattr(request.user, 'is_authenticated', False) else None,
        module=module,
        action=action,
        target_user=target_user,
        complaint=complaint,
        campaign=campaign,
        transaction=transaction_obj,
        ip_address=get_client_ip(request),
        metadata=metadata or {},
    )


def _parse_dashboard_target_user_ids(request, *, field_name='target_user_ids'):
    raw_values = []
    raw_values.extend(request.POST.getlist(field_name))
    extra_single = (request.POST.get(field_name) or '').strip()
    if extra_single:
        raw_values.append(extra_single)
    legacy_single = (request.POST.get('target_user_id') or '').strip()
    if legacy_single:
        raw_values.append(legacy_single)
    raw_values.extend(request.POST.getlist('selected_user_ids'))

    parsed_ids = []
    for raw in raw_values:
        for bit in str(raw or '').split(','):
            bit = bit.strip()
            if bit.isdigit():
                parsed_ids.append(int(bit))
    return sorted(set(parsed_ids))


def _send_ops_message_to_users(
    request,
    *,
    allowed_users_qs,
    module,
    action='message_sent',
    redirect_url,
):
    if not crm_can_message(request.user):
        messages.error(request, 'Not allowed.')
        return redirect(redirect_url)

    target_user_ids = _parse_dashboard_target_user_ids(request)
    if not target_user_ids:
        messages.error(request, 'Select at least one user.')
        return redirect(redirect_url)

    title = (request.POST.get('msg_title') or '').strip() or 'Message'
    body = (request.POST.get('msg_body') or '').strip()
    channels = [channel.strip() for channel in request.POST.getlist('message_channels') if channel.strip()]
    if not channels:
        if request.POST.get('via_inapp') == '1':
            channels.append('in_app')
        if request.POST.get('via_email') == '1':
            channels.append('email')
        if request.POST.get('via_sms') == '1':
            channels.append('sms')
    channels = sorted(set(channels))

    if not body:
        messages.error(request, 'Message is required.')
        return redirect(redirect_url)
    if not channels:
        messages.error(request, 'Select at least one delivery channel.')
        return redirect(redirect_url)

    allowed_users = list(allowed_users_qs.filter(id__in=target_user_ids).distinct())
    if not allowed_users:
        messages.error(request, 'Selected users are outside your scope.')
        return redirect(redirect_url)

    delivered_recipients = 0
    for msg_target in allowed_users:
        sent = []
        errors = {}

        if 'in_app' in channels:
            try:
                create_notification(
                    recipient=msg_target,
                    notification_type='SYSTEM_ANNOUNCEMENT',
                    title=title,
                    message=body,
                    data={
                        'popup_category': 'message',
                        'delivery_channel': 'in_app',
                        'url': '/notifications/',
                    },
                )
                sent.append('in_app')
            except Exception as exc:
                errors['in_app'] = str(exc)

        if 'email' in channels:
            try:
                from django.core.mail import EmailMultiAlternatives
                from django.template.loader import render_to_string
                from django.utils.html import strip_tags

                if not msg_target.email:
                    raise ValueError("Target user has no email address.")

                html = render_to_string('betting/email/crm_message.html', {
                    'site_name': getattr(getattr(settings, 'SITE_NAME', None), 'strip', lambda: '')() or 'StakeNaija',
                    'title': title,
                    'body': body,
                    'user': msg_target,
                })
                text = strip_tags(html) or body
                email_message = EmailMultiAlternatives(
                    subject=title,
                    body=text,
                    from_email=settings.DEFAULT_FROM_EMAIL or settings.EMAIL_HOST_USER,
                    to=[msg_target.email],
                )
                email_message.attach_alternative(html, "text/html")
                email_message.send(fail_silently=False)
                create_notification(
                    recipient=msg_target,
                    notification_type='SYSTEM_ANNOUNCEMENT',
                    title=title,
                    message=body,
                    data={
                        'popup_category': 'message',
                        'delivery_channel': 'email',
                        'url': '/notifications/',
                    },
                )
                sent.append('email')
            except Exception as exc:
                errors['email'] = str(exc)

        sms_status = None
        if 'sms' in channels:
            try:
                from notifications.services import send_sms_ebulksms

                sms_status = send_sms_ebulksms(
                    msisdn=msg_target.phone_number or '',
                    message=body,
                    sender=getattr(settings, 'EBULKSMS_SENDER', None),
                )
                if sms_status.get('ok'):
                    sent.append('sms')
                else:
                    errors['sms'] = sms_status.get('error') or sms_status.get('status') or 'failed'
            except Exception as exc:
                sms_status = {'ok': False, 'error': str(exc)}
                errors['sms'] = str(exc)

        if sent:
            delivered_recipients += 1

        CRMActionLog.objects.create(
            actor=request.user,
            target_user=msg_target,
            action_type='MESSAGE_SENT',
            data={
                'channels': sent,
                'errors': errors,
                'sms': sms_status or {},
                'source': 'dashboard_ops',
                'module': module,
            },
        )
        _log_crm_ops_action(
            request,
            module=module,
            action=action,
            target_user=msg_target,
            metadata={
                'title': title,
                'channels': sent,
                'errors': errors,
            },
        )

    if delivered_recipients:
        messages.success(request, f'Message processed for {delivered_recipients} recipient(s).')
    else:
        messages.error(request, 'Message was not sent. Check channel configuration and recipient details.')
    return redirect(redirect_url)


def _resolve_bulk_campaign_users(campaign, *, acting_user=None):
    scope_user = acting_user or campaign.created_by
    scope_qs = _ops_targetable_users_queryset(scope_user or User())
    site_config = SiteConfiguration.load()
    if campaign.target_group == 'all_users':
        return scope_qs.filter(is_active=True)
    if campaign.target_group == 'all_agents':
        return scope_qs.filter(user_type='agent', is_active=True)
    if campaign.target_group == 'all_super_agents':
        return scope_qs.filter(user_type='super_agent', is_active=True)
    if campaign.target_group == 'all_retail_managers':
        return scope_qs.filter(user_type='retail_manager', is_active=True)
    if campaign.target_group == 'specific_agents':
        return scope_qs.filter(id__in=(campaign.target_agent_ids or []), user_type='agent', is_active=True)
    if campaign.target_group == 'custom_users':
        return scope_qs.filter(id__in=(campaign.target_user_ids or []), is_active=True)
    if campaign.target_group == 'dormant_users':
        return scope_qs.filter(Q(last_login__lt=timezone.now() - timedelta(days=30)) | Q(last_login__isnull=True), is_active=True)
    if campaign.target_group == 'high_value_users':
        ids = list(
            Transaction.objects.filter(
                user__in=scope_qs,
                transaction_type='deposit',
                status='completed',
                is_successful=True,
            ).values('user_id').annotate(total=Sum('amount')).filter(total__gte=site_config.crm_large_deposit_threshold).values_list('user_id', flat=True)
        )
        return scope_qs.filter(id__in=ids, is_active=True)
    if campaign.target_group == 'recent_registrations':
        return scope_qs.filter(date_joined__gte=timezone.now() - timedelta(days=7), is_active=True)
    if campaign.target_group == 'failed_deposit_users':
        ids = list(
            Transaction.objects.filter(
                user__in=scope_qs,
                transaction_type='deposit',
                status='failed',
            ).values('user_id').annotate(total=Count('id')).filter(total__gte=site_config.crm_failed_deposit_repeat_threshold).values_list('user_id', flat=True)
        )
        return scope_qs.filter(id__in=ids, is_active=True)
    if campaign.target_group == 'pending_withdrawal_users':
        ids = list(UserWithdrawal.objects.filter(user__in=scope_qs, status='pending').values_list('user_id', flat=True).distinct())
        return scope_qs.filter(id__in=ids, is_active=True)
    return scope_qs.none()


def _next_campaign_schedule(current_at, pattern):
    if not current_at:
        current_at = timezone.now()
    now = timezone.now()
    step = None
    if pattern == 'daily':
        step = timedelta(days=1)
    elif pattern == 'weekly':
        step = timedelta(days=7)
    elif pattern == 'monthly':
        step = timedelta(days=30)
    if step is None:
        return None

    next_run = current_at + step
    while next_run <= now:
        next_run += step
    return next_run


def send_bulk_message_campaign_now(campaign_id, *, acting_user=None):
    campaign = BulkMessageCampaign.objects.filter(id=campaign_id).select_related('created_by', 'template').first()
    if not campaign:
        return 0
    if campaign.channel == 'email':
        actor_for_policy = acting_user or campaign.created_by
        if actor_for_policy and not crm_can_send_bulk_email(actor_for_policy):
            campaign.status = 'failed'
            campaign.last_error = 'not_allowed'
            campaign.sent_at = timezone.now()
            campaign.save(update_fields=['status', 'last_error', 'sent_at', 'updated_at'])
            CRMOpsAuditLog.objects.create(
                actor=acting_user or campaign.created_by,
                module='bulk_messaging',
                action='campaign_blocked',
                campaign=campaign,
                metadata={'reason': 'email_not_allowed', 'channel': campaign.channel, 'target_group': campaign.target_group},
            )
            return 0
    recipients_qs = _resolve_bulk_campaign_users(campaign, acting_user=acting_user)
    recipients = list(recipients_qs.only('id', 'email', 'phone_number', 'username', 'first_name', 'last_name'))
    campaign.status = 'processing'
    campaign.last_error = ''
    campaign.save(update_fields=['status', 'last_error', 'updated_at'])

    delivered_count = 0
    failed_count = 0
    for recipient in recipients:
        delivery_status = 'failed'
        error_message = ''
        provider_response = {}
        sent_at = timezone.now()
        try:
            if campaign.channel == 'in_app':
                notification = create_notification(
                    recipient=recipient,
                    notification_type='SYSTEM_ANNOUNCEMENT',
                    title=campaign.subject or 'Notification',
                    message=campaign.message,
                    data={
                        'campaign_id': campaign.id,
                        'popup_category': 'message',
                        'delivery_channel': 'in_app',
                        'url': '/notifications/',
                    },
                )
                provider_response = {'notification_id': notification.id}
                delivery_status = 'sent'
            elif campaign.channel == 'email':
                from django.core.mail import EmailMultiAlternatives
                if not recipient.email:
                    raise ValueError('missing_email')
                html = render_to_string('betting/email/crm_message.html', {
                    'site_name': getattr(getattr(settings, 'SITE_NAME', None), 'strip', lambda: '')() or 'StakeNaija',
                    'title': campaign.subject or 'Notification',
                    'body': campaign.message,
                    'user': recipient,
                })
                text = strip_tags(html) or campaign.message
                message = EmailMultiAlternatives(
                    subject=campaign.subject or 'Notification',
                    body=text,
                    from_email=settings.DEFAULT_FROM_EMAIL or settings.EMAIL_HOST_USER,
                    to=[recipient.email],
                )
                message.attach_alternative(html, 'text/html')
                message.send(fail_silently=False)
                delivery_status = 'sent'
            elif campaign.channel == 'sms':
                from notifications.services import send_sms_ebulksms
                provider_response = send_sms_ebulksms(
                    msisdn=recipient.phone_number or '',
                    message=campaign.message,
                    sender=getattr(settings, 'EBULKSMS_SENDER', None),
                )
                if provider_response.get('ok'):
                    delivery_status = 'sent'
                else:
                    error_message = provider_response.get('error') or provider_response.get('status') or 'sms_failed'
            else:
                raise ValueError('unsupported_channel')
        except Exception as exc:
            error_message = str(exc)

        if delivery_status == 'sent':
            delivered_count += 1
        else:
            failed_count += 1

        BulkMessageDelivery.objects.create(
            campaign=campaign,
            recipient=recipient,
            channel=campaign.channel,
            status=delivery_status,
            error_message=error_message[:255],
            provider_response=provider_response or {},
            sent_at=sent_at if delivery_status == 'sent' else None,
        )

    campaign.recipients_count = len(recipients)
    campaign.delivered_count = delivered_count
    campaign.failed_count = failed_count
    campaign.opened_count = 0
    campaign.clicked_count = 0
    campaign.conversion_count = 0
    campaign.sent_at = timezone.now()
    if failed_count and delivered_count:
        campaign.status = 'partial'
    elif failed_count and not delivered_count:
        campaign.status = 'failed'
        campaign.last_error = 'All deliveries failed.'
    else:
        campaign.status = 'sent'
    next_run_at = _next_campaign_schedule(campaign.schedule_at or campaign.sent_at, campaign.recurring_pattern)
    if next_run_at:
        campaign.next_run_at = next_run_at
        campaign.schedule_at = next_run_at
        campaign.status = 'scheduled'
    campaign.save(
        update_fields=[
            'recipients_count', 'delivered_count', 'failed_count', 'opened_count', 'clicked_count',
            'conversion_count', 'sent_at', 'status', 'last_error', 'next_run_at', 'schedule_at', 'updated_at'
        ]
    )
    CRMOpsAuditLog.objects.create(
        actor=acting_user,
        module='bulk_messaging',
        action='campaign_sent',
        campaign=campaign,
        metadata={
            'scheduled_run': acting_user is None,
            'channel': campaign.channel,
            'target_group': campaign.target_group,
            'delivered_count': delivered_count,
            'failed_count': failed_count,
            'status': campaign.status,
            'recurring_pattern': campaign.recurring_pattern,
            'next_run_at': campaign.next_run_at.isoformat() if campaign.next_run_at else '',
        },
    )
    return delivered_count


def process_due_bulk_message_campaigns(*, acting_user=None, limit=20):
    due_campaigns = BulkMessageCampaign.objects.filter(status='scheduled', schedule_at__lte=timezone.now()).order_by('schedule_at')[:limit]
    processed = 0
    for campaign in due_campaigns:
        send_bulk_message_campaign_now(campaign.id, acting_user=acting_user)
        processed += 1
    return processed


def log_admin_activity(request, action_description, action_type='UPDATE', affected_object=None):
    """Logs administrative actions."""
    if request.user.is_authenticated and (request.user.is_superuser or request.user.user_type in ['admin', 'account_user']):
        ActivityLog.objects.create(
            user=request.user,
            action=action_description,
            action_type=action_type, # Default to UPDATE for generic admin actions
            ip_address=get_client_ip(request),
            user_agent=request.META.get('HTTP_USER_AGENT', 'Unknown'),
            path=request.path,
            affected_object=affected_object
        )

# --- General Authentication Views ---

def frontpage(request):
    carousel_images = CarouselImage.objects.filter(is_active=True)
    context = {
        'carousel_images': carousel_images,
    }
    return render(request, 'betting/frontpage.html', context)

def register_user(request):
    if request.method == 'POST':
        form = UserRegistrationForm(request.POST, request=request) # Pass request to form
        if form.is_valid():
            user = form.save(request=request) # Pass request to form's save method for messages
            user_type = form.cleaned_data.get('user_type')
            if user_type == 'agent' and user is None:
                messages.success(request, 'Registration submitted for approval. Login details will be sent after admin approval.')
                return redirect('betting:login')
            messages.success(request, 'Registration successful. Please log in.')
            return redirect('betting:login')
        else:
            # Handle field errors
            for field, errors in form.errors.items():
                for error in errors:
                    messages.error(request, f"{field.replace('_', ' ').title()}: {error}")
            # Handle non-field errors
            if form.non_field_errors():
                for error in form.non_field_errors():
                    messages.error(request, f"Error: {error}")
    else:
        form = UserRegistrationForm()
    return render(request, 'betting/register.html', {'form': form})


@never_cache
@ensure_csrf_cookie
@ratelimit(key='ip', rate='5/m', method='POST', block=False)
def user_login(request):
    logger.debug("Entering user_login view.")
    if request.method == 'POST':
        is_testing = False
        try:
            import sys
            is_testing = 'test' in sys.argv
        except Exception:
            is_testing = False
        if getattr(request, 'limited', False) and not is_testing:
            messages.error(request, 'Too many login attempts. Please wait and try again.')
            form = LoginForm()
            return render(request, 'betting/login.html', {'form': form})
        form = LoginForm(request=request, data=request.POST)
        
        if form.is_valid():
            logger.debug("LoginForm is valid.")
            user = form.get_user()
            logger.debug(f"Attempting to authenticate user: {getattr(user, 'email', None)}")
            if user is not None:
                logger.debug(f"Authentication successful for user: {user.email}. User ID: {user.id}")
                login(request, user)
                logger.debug(f"User {user.email} logged in. Redirecting...")
                messages.success(request, f'Welcome, {user.first_name or user.email}!')

                if user.user_type in ['player', 'cashier', ''] or not getattr(user, 'user_type', ''):
                    return redirect('betting:fixtures')
                return redirect('betting:user_dashboard')
            else: # This block is theoretically unreachable if form.is_valid() implies user is not None
                logger.debug("Authentication failed. User is None after form.is_valid().")
                messages.error(request, 'An unexpected authentication error occurred. Please try again.')
        else:
            logger.debug("LoginForm is NOT valid.")
            # Errors are handled by the form instance in the template
            pass
    else:
        logger.debug("GET request for login page.")
        form = LoginForm()
    return render(request, 'betting/login.html', {'form': form})


@ratelimit(key='ip', rate='5/m', method='POST', block=False)
@ratelimit(key='post:identifier', rate='5/h', method='POST', block=False)
def forgot_password(request):
    if request.method == 'POST':
        is_testing = False
        try:
            import sys
            is_testing = 'test' in sys.argv
        except Exception:
            is_testing = False
        if getattr(request, 'limited', False) and not is_testing:
            try:
                cache_key = "email_volume:forgot_password:limited"
                if cache.add(cache_key, 1, timeout=3600):
                    pass
                else:
                    cache.incr(cache_key)
            except Exception:
                pass
            return JsonResponse({'status': 'error', 'message': 'Too many requests. Please wait and try again.'})

        try:
            cache_key = "email_volume:forgot_password:requested"
            if cache.add(cache_key, 1, timeout=3600):
                pass
            else:
                cache.incr(cache_key)
        except Exception:
            pass

        form = ForgotPasswordForm(request.POST)
        if form.is_valid():
            identifier = form.cleaned_data['identifier']
            user, resolution_error = resolve_user_from_identifier(identifier)
            
            if not user:
                try:
                    cache_key = "email_volume:forgot_password:user_not_found"
                    if cache.add(cache_key, 1, timeout=3600):
                        pass
                    else:
                        cache.incr(cache_key)
                except Exception:
                    pass
                return JsonResponse({'status': 'error', 'message': resolution_error or 'User not found in our database.'})
            
            # Create Reset Request
            token = secrets.token_urlsafe(32)
            expires_at = timezone.now() + timedelta(hours=2)
            
            reset_request = PasswordResetRequest.objects.create(
                email=user.email,
                token=token,
                user=user,
                expires_at=expires_at,
                ip_address=get_client_ip(request),
                user_agent=request.META.get('HTTP_USER_AGENT', 'Unknown')
            )
            
            # Send Email
            reset_url = request.build_absolute_uri(
                reverse('betting:reset_password', kwargs={'token': token})
            )
            
            subject = "Password Reset Request - StakeNaija"
            message = f"Hello {user.first_name},\n\nYou requested to reset your password. Click the link below to set a new password:\n\n{reset_url}\n\nThis link expires in 2 hours.\n\nIf you didn't request this, please ignore this email."
            
            try:
                from_email = settings.DEFAULT_FROM_EMAIL or settings.EMAIL_HOST_USER or f"no-reply@{request.get_host().split(':')[0]}"

                use_console_backend = settings.DEBUG and (
                    not getattr(settings, 'EMAIL_HOST', None)
                    or not getattr(settings, 'EMAIL_HOST_USER', None)
                    or not getattr(settings, 'EMAIL_HOST_PASSWORD', None)
                    or not from_email
                )

                if use_console_backend:
                    from django.core.mail import get_connection
                    connection = get_connection('django.core.mail.backends.console.EmailBackend')
                else:
                    from django.core.mail import get_connection
                    connection = get_connection()

                send_mail(
                    subject,
                    message,
                    from_email,
                    [user.email],
                    fail_silently=False,
                    connection=connection,
                )
                reset_request.email_sent = True
                reset_request.sent_at = timezone.now()
                reset_request.send_error = None
                reset_request.save(update_fields=['email_sent', 'sent_at', 'send_error'])
                try:
                    cache_key = "email_volume:forgot_password:sent"
                    if cache.add(cache_key, 1, timeout=3600):
                        pass
                    else:
                        cache.incr(cache_key)
                except Exception:
                    pass
                return JsonResponse({'status': 'success', 'message': 'A reset link has been sent to your email.'})
            except smtplib.SMTPAuthenticationError as e:
                logger.exception(f"Email sending failed: {str(e)}")
                reset_request.email_sent = False
                reset_request.send_error = str(e)
                reset_request.save(update_fields=['email_sent', 'send_error'])
                try:
                    cache_key = "email_volume:forgot_password:smtp_auth_error"
                    if cache.add(cache_key, 1, timeout=3600):
                        pass
                    else:
                        cache.incr(cache_key)
                except Exception:
                    pass

                if settings.DEBUG:
                    from django.core.mail import get_connection
                    connection = get_connection('django.core.mail.backends.console.EmailBackend')
                    send_mail(
                        subject,
                        message,
                        settings.DEFAULT_FROM_EMAIL or f"no-reply@{request.get_host().split(':')[0]}",
                        [user.email],
                        fail_silently=True,
                        connection=connection,
                    )
                    return JsonResponse({'status': 'success', 'message': 'Email delivery is not configured on this server. A reset link was generated (check server console output).'})

                return JsonResponse({'status': 'error', 'message': 'Failed to send reset email. Please try again later.'})
            except Exception as e:
                logger.exception(f"Email sending failed: {str(e)}")
                reset_request.email_sent = False
                reset_request.send_error = str(e)
                reset_request.save(update_fields=['email_sent', 'send_error'])
                try:
                    cache_key = "email_volume:forgot_password:send_error"
                    if cache.add(cache_key, 1, timeout=3600):
                        pass
                    else:
                        cache.incr(cache_key)
                except Exception:
                    pass
                return JsonResponse({'status': 'error', 'message': 'Failed to send reset email. Please try again later.'})
    else:
        form = ForgotPasswordForm()
    return render(request, 'betting/forgot_password.html', {'form': form})


def check_email_usage(request):
    email = request.GET.get('email') or request.POST.get('email')
    exclude_user_id = request.GET.get('exclude_user_id') or request.POST.get('exclude_user_id')
    try:
        exclude_user_id = int(exclude_user_id) if exclude_user_id else None
    except Exception:
        exclude_user_id = None

    details = duplicate_email_details(email, exclude_user_id=exclude_user_id)
    return JsonResponse(details)


def reset_password(request, token):
    reset_request = get_object_or_404(PasswordResetRequest, token=token)
    
    if not reset_request.is_valid():
        messages.error(request, "This reset link has expired or already been used.")
        return redirect('betting:forgot_password')
        
    if request.method == 'POST':
        form = ResetPasswordForm(request.POST)
        if form.is_valid():
            user = reset_request.user
            user.set_password(form.cleaned_data['password'])
            user.save()
            logout_user_from_all_active_sessions(user)
            
            # Mark request as used
            reset_request.is_used = True
            reset_request.save()
            
            messages.success(request, "Password reset successful! You can now login with your new password.")
            return redirect('betting:login')
    else:
        form = ResetPasswordForm()
        
    return render(request, 'betting/reset_password.html', {'form': form, 'token': token})


@login_required
def user_logout(request):
    logout(request)
    messages.info(request, 'You have been logged out.')
    return redirect('betting:frontpage')

# --- Fixtures & Betting Views ---

def _get_fixtures_data(period_id=None):
    """
    Helper to fetch fixtures and the current betting period.
    Returns a tuple: (fixtures, current_betting_period)
    """
    current_betting_period = None
    fixtures = Fixture.objects.none()

    if period_id:
        current_betting_period = get_object_or_404(BettingPeriod, id=period_id)
        fixtures = Fixture.objects.filter(betting_period=current_betting_period).annotate(
            serial_int=Cast('serial_number', IntegerField())
        ).order_by('serial_int')
    else:
        # Get the latest open betting period
        current_betting_period = BettingPeriod.objects.filter(
            start_date__lte=timezone.now().date(),
            end_date__gte=timezone.now().date(),
            is_active=True
        ).order_by('-start_date').first()

        # Fallback: if no currently running period, get the next upcoming active period
        if not current_betting_period:
            current_betting_period = BettingPeriod.objects.filter(
                start_date__gt=timezone.now().date(),
                is_active=True
            ).order_by('start_date').first()
        
        # Fallback 2: if still no period, get the latest active period (could be past)
        if not current_betting_period:
            current_betting_period = BettingPeriod.objects.filter(
                is_active=True
            ).order_by('-start_date').first()

        if current_betting_period:
            fixtures = Fixture.objects.filter(betting_period=current_betting_period).annotate(
                serial_int=Cast('serial_number', IntegerField())
            ).order_by('serial_int')

    # Filter out fixtures that are not active or have invalid status
    if fixtures.exists():
        fixtures = fixtures.filter(is_active=True).exclude(status__in=['cancelled', 'finished', 'settled', 'postponed'])

        # Filter out fixtures that have already started (Date/Time check)
    # We compare against local time because match_date/time are typically stored as wall-clock time
    local_now = timezone.localtime(timezone.now())
    fixtures = fixtures.filter(
       Q(match_date__gt=local_now.date()) | 
       Q(match_date=local_now.date(), match_time__gt=local_now.time())
    )
        
    return fixtures, current_betting_period

def calculate_bonus_amount(potential_winning, stake_amount, selections, bet_type):
    odds = []
    for s in selections:
        if isinstance(s, dict):
            odds.append(s.get('odd', Decimal('0.00')))
        elif hasattr(s, 'odd_selected'):
            odds.append(s.odd_selected)
        else:
            odds.append(Decimal('0.00'))

    rule = select_bonus_rule(bet_type, len(selections), odds)
    if not rule:
        return None, Decimal('0.00'), Decimal('0.00'), Decimal('0.0000')

    pct = rule.get('pct', Decimal('0.0000'))
    base_amount = Decimal(str(potential_winning or 0))
    if rule.get('base') == 'net':
        base_amount = base_amount - Decimal(str(stake_amount or 0))
        if base_amount < 0:
            base_amount = Decimal('0.00')

    bonus_amount = compute_bonus_amount(base_amount, pct, rule.get('cap'))
    return rule, base_amount, bonus_amount, pct

def fixtures_view(request, period_id=None):
    fixtures, current_betting_period = _get_fixtures_data(period_id)

    all_periods = BettingPeriod.objects.all().order_by('-start_date')
    active_periods = BettingPeriod.objects.filter(is_active=True).order_by('-start_date')
    popular_picks = []
    if current_betting_period:
        local_now = timezone.localtime(timezone.now())
        picks_qs = PopularPick.objects.select_related('fixture', 'fixture__betting_period').filter(
            is_active=True,
            fixture__is_active=True,
            fixture__status='scheduled',
            fixture__betting_period=current_betting_period,
        ).filter(
            Q(fixture__match_date__gt=local_now.date()) |
            Q(fixture__match_date=local_now.date(), fixture__match_time__gt=local_now.time())
        )
        popular_picks = [p for p in picks_qs if p.odd_value is not None][:10]

    bonus_rules_data = []
    for r in get_active_bonus_rules_cached():
        bonus_rules_data.append({
            'id': r['id'],
            'min': r['min'],
            'max': r['max'],
            'min_odd': float(r['min_odd']),
            'pct': float(r['pct']),
            'cap': float(r['cap']) if r['cap'] is not None else None,
            'base': r['base'],
            'allow_system': r['allow_system'],
            'allow_acca': r['allow_acca'],
            'allow_single': r['allow_single'],
        })

    context = {
        'fixtures': fixtures,
        'current_betting_period': current_betting_period,
        'all_periods': all_periods,
        'active_periods': active_periods,
        'popular_picks': popular_picks,
        'bet_ticket_form': BetTicketForm(), # For placing single bets on fixture page
        'can_place_bet': is_cashier(request.user),
        'bonus_rules_json': json.dumps(bonus_rules_data),
    }
    return render(request, 'betting/fixtures.html', context)

def fixtures_list_partial(request, period_id=None):
    """
    Returns only the HTML for the fixtures list, used for AJAX polling.
    """
    fixtures, current_betting_period = _get_fixtures_data(period_id)
    
    context = {
        'fixtures': fixtures,
        'current_betting_period': current_betting_period,
    }
    return render(request, 'betting/includes/fixtures_list.html', context)

def popular_picks_json(request, period_id=None):
    _, current_betting_period = _get_fixtures_data(period_id)
    if not current_betting_period:
        return JsonResponse({'success': True, 'picks': []})

    local_now = timezone.localtime(timezone.now())
    picks_qs = PopularPick.objects.select_related('fixture', 'fixture__betting_period').filter(
        is_active=True,
        fixture__is_active=True,
        fixture__status='scheduled',
        fixture__betting_period=current_betting_period,
    ).filter(
        Q(fixture__match_date__gt=local_now.date()) |
        Q(fixture__match_date=local_now.date(), fixture__match_time__gt=local_now.time())
    ).order_by('sort_order', '-created_at')

    picks = []
    for p in picks_qs[:20]:
        odd = p.odd_value
        if odd is None:
            continue
        f = p.fixture
        picks.append({
            'fixture_id': f.id,
            'bet_type': p.bet_type,
            'odd': float(odd),
            'market_label': p.market_label,
            'selection_label': p.selection_label,
            'period_name': f.betting_period.name if f.betting_period_id else '',
            'home_team': f.home_team,
            'away_team': f.away_team,
            'match_date': f.match_date.strftime('%Y-%m-%d') if f.match_date else '',
            'match_time': f.match_time.strftime('%H:%M') if f.match_time else '',
        })
        if len(picks) >= 10:
            break

    return JsonResponse({'success': True, 'picks': picks})


def _can_access_commission_management(user):
    return bool(
        user
        and getattr(user, 'is_authenticated', False)
        and (getattr(user, 'is_superuser', False) or getattr(user, 'user_type', '') in ['admin', 'crm', 'account_user', 'retail_manager'])
    )


def _commission_allowed_agents_qs(user):
    if not user or not getattr(user, 'is_authenticated', False):
        return User.objects.none()
    if getattr(user, 'is_superuser', False) or getattr(user, 'user_type', '') in ['admin', 'crm', 'account_user']:
        return User.objects.filter(user_type='agent')
    if getattr(user, 'user_type', '') == 'retail_manager':
        mas = get_retail_manager_master_agents(user)
        sas = get_retail_manager_super_agents(user, master_agents_qs=mas)
        return get_retail_manager_agents(user, master_agents_qs=mas, super_agents_qs=sas)
    return User.objects.none()


@login_required
def commission_management(request):
    if not _can_access_commission_management(request.user):
        return HttpResponseForbidden("Not allowed.")

    from commission.models import (
        CommissionPlan,
        AgentCommissionProfile,
        CommissionProfileAssignmentLog,
        CommissionChangeRequest,
    )

    tab = (request.GET.get('tab') or 'assign').strip() or 'assign'
    preselect_agent_id = (request.GET.get('agent_id') or '').strip()
    q = (request.GET.get('q') or '').strip()
    profile_id = (request.GET.get('profile') or '').strip()
    status = (request.GET.get('status') or '').strip()
    date_from = (request.GET.get('from') or '').strip()
    date_to = (request.GET.get('to') or '').strip()

    allowed_agents = _commission_allowed_agents_qs(request.user)

    plans = CommissionPlan.objects.all().order_by('name')
    context = {
        'tab': tab,
        'q': q,
        'profile_id': profile_id,
        'status': status,
        'date_from': date_from,
        'date_to': date_to,
        'plans': plans,
        'is_super_admin': bool(request.user.is_superuser or request.user.user_type == 'admin'),
        'is_retail_manager': bool(request.user.user_type == 'retail_manager'),
    }
    if tab == 'assign' and preselect_agent_id:
        try:
            pre_u = allowed_agents.get(id=preselect_agent_id)
            context['preselected_agent'] = pre_u
        except Exception:
            context['preselected_agent'] = None

    if tab == 'profiles':
        context['profiles'] = plans
        return render(request, 'betting/commission_management.html', context)

    if tab == 'assigned':
        qs = AgentCommissionProfile.objects.select_related('user', 'plan', 'assigned_by').filter(user__in=allowed_agents)
        if q:
            qs = qs.filter(
                Q(user__username__icontains=q) |
                Q(user__email__icontains=q) |
                Q(user__first_name__icontains=q) |
                Q(user__last_name__icontains=q) |
                Q(user__phone_number__icontains=q)
            )
        if profile_id:
            qs = qs.filter(plan_id=profile_id)
        if date_from:
            qs = qs.filter(assigned_at__date__gte=date_from)
        if date_to:
            qs = qs.filter(assigned_at__date__lte=date_to)
        page = Paginator(qs.order_by('-assigned_at'), 30).get_page(request.GET.get('page'))
        context['page_obj'] = page
        context['rows'] = page.object_list
        return render(request, 'betting/commission_management.html', context)

    if tab == 'unassigned':
        qs = allowed_agents.filter(commission_profile__isnull=True)
        if q:
            qs = qs.filter(
                Q(username__icontains=q) |
                Q(email__icontains=q) |
                Q(first_name__icontains=q) |
                Q(last_name__icontains=q) |
                Q(phone_number__icontains=q)
            )
        page = Paginator(qs.order_by('-date_joined'), 30).get_page(request.GET.get('page'))
        context['page_obj'] = page
        context['rows'] = page.object_list
        return render(request, 'betting/commission_management.html', context)

    if tab == 'history':
        qs = CommissionProfileAssignmentLog.objects.select_related('agent', 'previous_profile', 'new_profile', 'assigned_by').filter(agent__in=allowed_agents)
        if q:
            qs = qs.filter(
                Q(agent__username__icontains=q) |
                Q(agent__email__icontains=q) |
                Q(assigned_by__username__icontains=q) |
                Q(assigned_by__email__icontains=q) |
                Q(assignment_reason__icontains=q) |
                Q(ip_address__icontains=q)
            )
        if profile_id:
            qs = qs.filter(new_profile_id=profile_id)
        if date_from:
            qs = qs.filter(created_at__date__gte=date_from)
        if date_to:
            qs = qs.filter(created_at__date__lte=date_to)
        page = Paginator(qs.order_by('-created_at'), 30).get_page(request.GET.get('page'))
        context['page_obj'] = page
        context['rows'] = page.object_list
        return render(request, 'betting/commission_management.html', context)

    if tab == 'requests':
        qs = CommissionChangeRequest.objects.select_related('agent', 'current_profile', 'requested_profile', 'requested_by', 'decided_by').filter(agent__in=allowed_agents)
        if status:
            qs = qs.filter(status=status)
        if q:
            qs = qs.filter(
                Q(agent__username__icontains=q) |
                Q(agent__email__icontains=q) |
                Q(requested_by__username__icontains=q) |
                Q(reason__icontains=q)
            )
        page = Paginator(qs.order_by('-created_at'), 30).get_page(request.GET.get('page'))
        context['page_obj'] = page
        context['rows'] = page.object_list
        return render(request, 'betting/commission_management.html', context)

    return render(request, 'betting/commission_management.html', context)


@login_required
def commission_management_agent_search(request):
    if not _can_access_commission_management(request.user):
        return JsonResponse({'results': []})

    term = (request.GET.get('q') or request.GET.get('term') or '').strip()
    state_id = (request.GET.get('state') or '').strip()
    agent_type = (request.GET.get('agent_type') or '').strip()

    qs = _commission_allowed_agents_qs(request.user).select_related('state')
    if state_id:
        qs = qs.filter(state_id=state_id)
    if agent_type:
        qs = qs.filter(user_type=agent_type)

    if term:
        qs = qs.filter(
            Q(username__icontains=term) |
            Q(email__icontains=term) |
            Q(first_name__icontains=term) |
            Q(last_name__icontains=term) |
            Q(phone_number__icontains=term) |
            Q(shop_address__icontains=term)
        )

    qs = qs.order_by('username')[:30]
    results = []
    for u in qs:
        results.append({
            'id': u.id,
            'text': f"{u.username} • {u.get_full_name() or u.email or ''}".strip(),
        })
    return JsonResponse({'results': results})


@login_required
@require_POST
def commission_management_assign_api(request):
    if not _can_access_commission_management(request.user):
        return JsonResponse({'success': False, 'message': 'Not allowed.'}, status=403)

    from commission.models import CommissionPlan
    from commission.services import CommissionProfileAssignmentService

    agent_id = (request.POST.get('agent_id') or '').strip()
    profile_id = (request.POST.get('profile_id') or '').strip()
    reason = (request.POST.get('reason') or '').strip()
    allow_override = (request.POST.get('allow_override') or '').strip().lower() in ['1', 'true', 'yes', 'on']

    try:
        agent = _commission_allowed_agents_qs(request.user).get(id=agent_id)
    except Exception:
        return JsonResponse({'success': False, 'message': 'Agent not found or not allowed.'}, status=404)

    try:
        plan = CommissionPlan.objects.get(id=profile_id)
    except Exception:
        return JsonResponse({'success': False, 'message': 'Commission profile not found.'}, status=404)

    ip = get_client_ip(request) if 'get_client_ip' in globals() else request.META.get('REMOTE_ADDR')
    device = (request.META.get('HTTP_USER_AGENT', '') or '')[:2000]
    ok, msg, profile = CommissionProfileAssignmentService.assign_profile(
        agent=agent,
        plan=plan,
        actor=request.user,
        reason=reason,
        ip_address=ip,
        device_info=device,
        allow_override=allow_override,
    )

    if not ok:
        return JsonResponse({'success': False, 'message': msg}, status=400)

    return JsonResponse({
        'success': True,
        'message': msg,
        'assigned_profile': getattr(getattr(profile, 'plan', None), 'name', ''),
        'assigned_at': profile.assigned_at.isoformat(sep=' ', timespec='seconds') if getattr(profile, 'assigned_at', None) else '',
    })


@login_required
@require_POST
def commission_management_change_request_api(request):
    if not _can_access_commission_management(request.user):
        return JsonResponse({'success': False, 'message': 'Not allowed.'}, status=403)

    from commission.models import CommissionPlan, AgentCommissionProfile, CommissionChangeRequest

    agent_id = (request.POST.get('agent_id') or '').strip()
    profile_id = (request.POST.get('profile_id') or '').strip()
    reason = (request.POST.get('reason') or '').strip()

    try:
        agent = _commission_allowed_agents_qs(request.user).get(id=agent_id)
    except Exception:
        return JsonResponse({'success': False, 'message': 'Agent not found or not allowed.'}, status=404)

    try:
        plan = CommissionPlan.objects.get(id=profile_id)
    except Exception:
        return JsonResponse({'success': False, 'message': 'Commission profile not found.'}, status=404)

    current = AgentCommissionProfile.objects.filter(user=agent).select_related('plan').first()
    req = CommissionChangeRequest.objects.create(
        agent=agent,
        requested_by=request.user,
        current_profile=getattr(current, 'plan', None),
        requested_profile=plan,
        reason=(reason or '')[:255],
        status='pending',
    )

    return JsonResponse({'success': True, 'message': 'Change request submitted.', 'id': req.id})


@login_required
@require_POST
def commission_management_change_request_decide_api(request, request_id):
    if not _can_access_commission_management(request.user):
        return JsonResponse({'success': False, 'message': 'Not allowed.'}, status=403)

    from commission.models import CommissionChangeRequest
    from commission.services import CommissionProfileAssignmentService

    is_super = bool(request.user.is_superuser or request.user.user_type == 'admin')
    if not is_super:
        return JsonResponse({'success': False, 'message': 'Only Super Admin can approve/reject.'}, status=403)

    action = (request.POST.get('action') or '').strip().lower()
    note = (request.POST.get('note') or '').strip()
    try:
        req = CommissionChangeRequest.objects.select_related('agent', 'current_profile', 'requested_profile').get(id=request_id)
    except Exception:
        return JsonResponse({'success': False, 'message': 'Request not found.'}, status=404)

    if req.status != 'pending':
        return JsonResponse({'success': False, 'message': 'Request already decided.'}, status=400)

    if action not in ['approve', 'reject']:
        return JsonResponse({'success': False, 'message': 'Invalid action.'}, status=400)

    if action == 'reject':
        req.status = 'rejected'
        req.decided_by = request.user
        req.decided_at = timezone.now()
        req.decision_note = (note or '')[:255]
        req.save(update_fields=['status', 'decided_by', 'decided_at', 'decision_note', 'updated_at'])
        return JsonResponse({'success': True, 'message': 'Request rejected.'})

    ip = get_client_ip(request) if 'get_client_ip' in globals() else request.META.get('REMOTE_ADDR')
    device = (request.META.get('HTTP_USER_AGENT', '') or '')[:2000]
    ok, msg, _ = CommissionProfileAssignmentService.assign_profile(
        agent=req.agent,
        plan=req.requested_profile,
        actor=request.user,
        reason=note or req.reason or 'Approved change request',
        ip_address=ip,
        device_info=device,
        allow_override=True,
    )
    if not ok:
        return JsonResponse({'success': False, 'message': msg}, status=400)

    req.status = 'approved'
    req.decided_by = request.user
    req.decided_at = timezone.now()
    req.decision_note = (note or '')[:255]
    req.save(update_fields=['status', 'decided_by', 'decided_at', 'decision_note', 'updated_at'])
    return JsonResponse({'success': True, 'message': 'Request approved and profile assigned.'})


@login_required
def commission_management_export(request):
    if not _can_access_commission_management(request.user):
        return HttpResponseForbidden("Not allowed.")

    from commission.models import AgentCommissionProfile, CommissionProfileAssignmentLog

    dataset = (request.GET.get('dataset') or '').strip().lower()
    fmt = (request.GET.get('format') or 'csv').strip().lower()

    allowed_agents = _commission_allowed_agents_qs(request.user)
    rows = []
    title = 'commission_management'

    if dataset == 'unassigned':
        title = 'unassigned_agents'
        qs = allowed_agents.filter(commission_profile__isnull=True).select_related('state').order_by('-date_joined')
        for u in qs[:100000]:
            rows.append({
                'username': u.username,
                'full_name': u.get_full_name(),
                'email': u.email,
                'phone': u.phone_number or '',
                'state': getattr(getattr(u, 'state', None), 'state_name', '') or '',
                'agent_type': u.user_type,
                'date_registered': u.date_joined.isoformat(sep=' ', timespec='seconds') if u.date_joined else '',
                'status': 'active' if u.is_active else 'inactive',
            })
    elif dataset == 'assigned':
        title = 'assigned_profiles'
        qs = AgentCommissionProfile.objects.select_related('user', 'plan', 'assigned_by').filter(user__in=allowed_agents).order_by('-assigned_at')
        for p in qs[:100000]:
            rows.append({
                'username': p.user.username,
                'full_name': p.user.get_full_name(),
                'email': p.user.email,
                'profile': p.plan.name,
                'assigned_at': p.assigned_at.isoformat(sep=' ', timespec='seconds') if p.assigned_at else '',
                'assigned_by': getattr(getattr(p, 'assigned_by', None), 'email', '') or '',
                'status': 'active' if p.is_active else 'inactive',
            })
    elif dataset == 'history':
        title = 'assignment_history'
        qs = CommissionProfileAssignmentLog.objects.select_related('agent', 'previous_profile', 'new_profile', 'assigned_by').filter(agent__in=allowed_agents).order_by('-created_at')
        for h in qs[:100000]:
            rows.append({
                'time': h.created_at.isoformat(sep=' ', timespec='seconds') if h.created_at else '',
                'agent': h.agent.email or h.agent.username,
                'previous_profile': getattr(getattr(h, 'previous_profile', None), 'name', '') or '',
                'new_profile': h.new_profile.name,
                'assigned_by': getattr(getattr(h, 'assigned_by', None), 'email', '') or '',
                'role': h.assigned_by_role,
                'ip': h.ip_address or '',
                'reason': h.assignment_reason,
                'override': 'yes' if h.is_override else 'no',
            })
    else:
        return HttpResponseBadRequest("Unknown dataset")

    if fmt == 'csv':
        import io
        import csv
        output = io.StringIO()
        fieldnames = list(rows[0].keys()) if rows else []
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            writer.writerow(r)
        resp = HttpResponse(output.getvalue(), content_type='text/csv')
        resp['Content-Disposition'] = f'attachment; filename="{title}.csv"'
        return resp

    if fmt == 'xlsx':
        import io
        import pandas as pd
        output = io.BytesIO()
        df = pd.DataFrame(rows)
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            df.to_excel(writer, index=False, sheet_name=(title[:31] or 'Sheet1'))
        output.seek(0)
        resp = HttpResponse(output.getvalue(), content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        resp['Content-Disposition'] = f'attachment; filename="{title}.xlsx"'
        return resp

    if fmt == 'pdf':
        try:
            from weasyprint import HTML
        except Exception as e:
            return HttpResponseBadRequest(f"PDF export unavailable: {e}")
        from html import escape as _html_escape
        cols = list(rows[0].keys()) if rows else []
        def esc(s):
            return _html_escape(str(s or ''), quote=True)
        head = ''.join([f"<th>{esc(c)}</th>" for c in cols])
        body = ''.join([
            "<tr>" + ''.join([f"<td>{esc(r.get(c))}</td>" for c in cols]) + "</tr>"
            for r in rows[:3000]
        ])
        html = f"""
        <html>
          <head>
            <meta charset="utf-8" />
            <style>
              body {{ font-family: Arial, sans-serif; font-size: 11px; }}
              h2 {{ margin: 0 0 8px 0; }}
              table {{ width: 100%; border-collapse: collapse; }}
              th, td {{ border: 1px solid #ddd; padding: 6px; vertical-align: top; }}
              th {{ background: #f3f5f7; text-align: left; }}
              tr:nth-child(even) td {{ background: #fafafa; }}
            </style>
          </head>
          <body>
            <h2>{esc(title.replace('_',' ').title())}</h2>
            <table>
              <thead><tr>{head}</tr></thead>
              <tbody>{body}</tbody>
            </table>
          </body>
        </html>
        """
        pdf_bytes = HTML(string=html).write_pdf()
        resp = HttpResponse(pdf_bytes, content_type='application/pdf')
        resp['Content-Disposition'] = f'attachment; filename="{title}.pdf"'
        return resp

    return HttpResponseBadRequest("Unknown format")


@login_required
@ratelimit(key='user', rate='10/m', method='POST', block=True)
@db_transaction.atomic
def place_bet(request):
    try:
        if request.method == 'POST':
            # Debug logging
            logger.info(f"Place Bet Request Keys: {list(request.POST.keys())}")

            if not is_cashier(request.user):
                if request.headers.get('x-requested-with') == 'XMLHttpRequest' or 'application/json' in request.META.get('HTTP_ACCEPT', ''):
                    return JsonResponse({'success': False, 'message': 'You are not authorized to place a bet'})
                messages.error(request, 'You are not authorized to place a bet')
                return redirect('betting:fixtures')

            # Check if this is the new JS-based bet placement
            if 'selections' in request.POST:
                try:
                    selections_data = json.loads(request.POST.get('selections'))
                    stake_amount_str = request.POST.get('stake_amount')
                    if not stake_amount_str:
                         return JsonResponse({'success': False, 'message': 'Stake amount is missing.'})
                    stake_amount_per_line = Decimal(stake_amount_str)
                    
                    is_system_bet = request.POST.get('is_system_bet') == 'true'
                    permutation_count = int(request.POST.get('permutation_count', 0))

                    if not selections_data:
                        return JsonResponse({'success': False, 'message': 'No bets selected.'})

                    # --- IDEMPOTENCY CHECK ---
                    # Create a unique hash for this bet request to prevent double submission
                    idempotency_key = None
                    try:
                        # Ensure stable sorting for JSON hash
                        idempotency_payload = f"bet-{request.user.id}-{json.dumps(selections_data, sort_keys=True)}-{stake_amount_str}-{is_system_bet}-{permutation_count}"
                        idempotency_key = hashlib.sha256(idempotency_payload.encode('utf-8')).hexdigest()
                        
                        if cache.get(idempotency_key):
                            logger.warning(f"Duplicate bet placement blocked for user {request.user.id}")
                            return JsonResponse({'success': False, 'message': 'Duplicate bet detected. Please wait a moment.'})
                        
                        # Lock for 30 seconds
                        cache.set(idempotency_key, True, timeout=30)
                    except Exception as e:
                        logger.error(f"Idempotency check error: {e}")
                        idempotency_key = None # Ensure it's None if check failed
                        # Continue if check fails, don't block user
                    # -------------------------

                    placement_lock_key = None

                    # Helper to clear lock on failure
                    def fail_response(message):
                        if idempotency_key:
                            cache.delete(idempotency_key)
                        if placement_lock_key:
                            release_ticket_placement_lock(placement_lock_key)
                        return JsonResponse({'success': False, 'message': message})

                    placement_lock_key = acquire_ticket_placement_lock(request.user.id)
                    if not placement_lock_key:
                        return fail_response('Too many requests. Please retry.')

                    # Basic Validation of selections
                    valid_selections = []
                    seen_fixtures = set()
                    for sel in selections_data:
                        # Support both camelCase (legacy/API) and snake_case (frontend) keys
                        fixture_id = sel.get('fixtureId') or sel.get('fixture_id')
                        if not fixture_id:
                            return fail_response('Missing fixture ID.')

                        fixture_key = str(fixture_id)
                        if fixture_key in seen_fixtures:
                            return fail_response('Duplicate event selected. Remove duplicate selections and try again.')
                        seen_fixtures.add(fixture_key)

                        try:
                            fixture = Fixture.objects.get(id=fixture_id)
                        except Fixture.DoesNotExist:
                            return fail_response('Fixture not found.')

                        # Validate fixture status
                        status = str(getattr(fixture, "status", "") or "").strip().lower()
                        closed_statuses = {'finished', 'settled', 'cancelled', 'postponed', 'abandoned', 'no_result'}
                        if status in closed_statuses:
                            return fail_response(f'Betting closed for {fixture.home_team} vs {fixture.away_team}')
                        
                        # Validate match time (Strict Date/Time Enforcement)
                        local_now = timezone.localtime(timezone.now())
                        if fixture.match_date < local_now.date() or (fixture.match_date == local_now.date() and fixture.match_time <= local_now.time()):
                             return fail_response(f'Betting closed for {fixture.home_team} vs {fixture.away_team} (Match Started)')

                        if not fixture.betting_period.is_active:
                                return fail_response(f'Betting period closed for {fixture.home_team} vs {fixture.away_team}')
                        
                        # Validate odds and outcome
                        outcome = sel.get('outcome') or sel.get('bet_type')
                        if not outcome:
                            return fail_response('Missing bet outcome.')

                        # Normalize outcome keys (frontend vs backend mismatch)
                        outcome_map = {
                            'over1_5': 'over_1_5',
                            'under1_5': 'under_1_5',
                            'over2_5': 'over_2_5',
                            'under2_5': 'under_2_5',
                            'over3_5': 'over_3_5',
                            'under3_5': 'under_3_5',
                        }
                        outcome = outcome_map.get(outcome, outcome)

                        # Verify odd matches server side to prevent tampering (simplified check)
                        # For now we assume the odd sent is correct or we re-fetch. 
                        # Better to re-fetch.
                        if outcome == 'home_win': odd = fixture.home_win_odd
                        elif outcome == 'draw': odd = fixture.draw_odd
                        elif outcome == 'away_win': odd = fixture.away_win_odd
                        elif outcome == 'home_dnb': odd = fixture.home_dnb_odd
                        elif outcome == 'away_dnb': odd = fixture.away_dnb_odd
                        elif outcome == 'over_1_5': odd = fixture.over_1_5_odd
                        elif outcome == 'under_1_5': odd = fixture.under_1_5_odd
                        elif outcome == 'over_2_5': odd = fixture.over_2_5_odd
                        elif outcome == 'under_2_5': odd = fixture.under_2_5_odd
                        elif outcome == 'over_3_5': odd = fixture.over_3_5_odd
                        elif outcome == 'under_3_5': odd = fixture.under_3_5_odd
                        elif outcome == 'btts_yes': odd = fixture.btts_yes_odd
                        elif outcome == 'btts_no': odd = fixture.btts_no_odd
                        else:
                            return fail_response(f'Invalid outcome for {fixture.home_team} vs {fixture.away_team}')

                        market_key = market_key_for_bet_type(outcome)
                        selection_key = selection_key_for_bet_type(outcome)
                        if risk_is_suspended(fixture.id, market_key, selection_key):
                            return fail_response('This event/market is temporarily suspended due to risk management.')

                        valid_selections.append({
                            'fixture': fixture,
                            'bet_type': outcome,
                            'odd': odd,
                            'market_key': market_key,
                            'selection_key': selection_key,
                        })

                    # --- Bet Permission Validation ---
                    config = SiteConfiguration.objects.first()
                    if config:
                        num_selections = len(valid_selections)
                        if num_selections == 1 and not config.allow_single_bet:
                            return fail_response('Single bets are currently disabled. Please add more selections.')
                        elif num_selections == 2 and not config.allow_double_bet:
                            return fail_response('Double bets are currently disabled. Please add more selections.')
                        elif num_selections >= 3 and not config.allow_multiple_bet:
                            return fail_response('Multiple bets are currently disabled.')
                    # ---------------------------------

                    # Calculate Total Stake and Combinations
                    
                    if is_system_bet and len(valid_selections) >= 3:
                        # System Bet Logic
                        if permutation_count < 2 or permutation_count > len(valid_selections):
                                return fail_response('Invalid permutation count.')

                        num_lines = math.comb(len(valid_selections), permutation_count)
                        total_stake = stake_amount_per_line * Decimal(num_lines)
                        
                        bet_type = 'system'
                        system_min_count = permutation_count
                    else:
                        # Single or Accumulator Logic (1 line)
                        num_lines = 1
                        total_stake = stake_amount_per_line
                        
                        bet_type = 'multiple' if len(valid_selections) > 1 else 'single'
                        system_min_count = None

                    # Calculate Potential Winning (Max Winning) for the single ticket
                    if bet_type == 'system':
                        k = int(system_min_count or 0)
                        odds_list = [s['odd'] for s in valid_selections]
                        projections = system_bet_payout_projections(odds_list, stake_amount_per_line, k)
                        potential_win = projections['max_potential_winning']
                        min_potential_win = projections['min_potential_winning']
                        total_ticket_odd = Decimal('0.00')
                        max_line_odd = projections['max_line_odd']
                    else:
                        total_ticket_odd = Decimal('1.00')
                        for sel in valid_selections:
                            total_ticket_odd *= sel['odd']
                        potential_win = (stake_amount_per_line * total_ticket_odd).quantize(Decimal('0.01'))
                        min_potential_win = potential_win
                        max_line_odd = total_ticket_odd.quantize(Decimal('0.01'))

                    odds_list = [s['odd'] for s in valid_selections]
                    rule_dict, bonus_base_amount, estimated_bonus_amount, applied_pct = calculate_bonus_amount(potential_win, total_stake, valid_selections, bet_type)
                    
                    # Calculate Min Winning with its own bonus
                    min_winning = min_potential_win
                    if rule_dict:
                        pct = rule_dict.get('pct', Decimal('0.0000'))
                        min_base = min_potential_win
                        if rule_dict.get('base') == 'net':
                            min_base = max(Decimal('0.00'), min_potential_win - total_stake)
                        
                        min_bonus = compute_bonus_amount(min_base, pct, rule_dict.get('cap'))
                        min_winning = (min_potential_win + min_bonus).quantize(Decimal('0.01'))

                    max_winning = (potential_win + estimated_bonus_amount).quantize(Decimal('0.01'))

                    SelectionLiabilitySnapshot = apps.get_model("risk", "SelectionLiabilitySnapshot")
                    MarketLiabilitySnapshot = apps.get_model("risk", "MarketLiabilitySnapshot")
                    FixtureLiabilitySnapshot = apps.get_model("risk", "FixtureLiabilitySnapshot")
                    ip_address = get_client_ip(request)
                    fingerprint_hash = request.headers.get("X-Device-Fingerprint") or request.POST.get("device_fingerprint") or ""

                    try:
                        record_device_fingerprint(
                            user=request.user,
                            fingerprint_hash=fingerprint_hash,
                            ip_address=ip_address,
                            user_agent=request.META.get("HTTP_USER_AGENT", ""),
                            timezone_name=request.POST.get("device_timezone", ""),
                            screen=request.POST.get("device_screen", ""),
                            platform=request.POST.get("device_platform", ""),
                            language=request.POST.get("device_language", ""),
                        )
                    except Exception:
                        pass

                    try:
                        ipintel = check_ip_intelligence(ip_address)
                        if ipintel.get("blocked"):
                            return fail_response("Betting is blocked from your current network. Please disable VPN/Proxy or contact support.")
                    except Exception:
                        pass

                    for s in valid_selections:
                        existing_selection = (
                            SelectionLiabilitySnapshot.objects.filter(
                                fixture_id=s["fixture"].id,
                                market_key=s["market_key"],
                                selection_key=s["selection_key"],
                            )
                            .values("total_potential_payout")
                            .first()
                        )
                        existing_market = (
                            MarketLiabilitySnapshot.objects.filter(
                                fixture_id=s["fixture"].id,
                                market_key=s["market_key"],
                            )
                            .values("total_potential_payout")
                            .first()
                        )
                        existing_fixture = (
                            FixtureLiabilitySnapshot.objects.filter(fixture_id=s["fixture"].id)
                            .values("total_potential_payout")
                            .first()
                        )

                        existing_selection_liability = Decimal(str((existing_selection or {}).get("total_potential_payout") or "0.00"))
                        existing_market_liability = Decimal(str((existing_market or {}).get("total_potential_payout") or "0.00"))
                        existing_fixture_liability = Decimal(str((existing_fixture or {}).get("total_potential_payout") or "0.00"))

                        projected_selection_liability = (existing_selection_liability + max_winning).quantize(Decimal("0.01"))
                        projected_market_liability = (existing_market_liability + max_winning).quantize(Decimal("0.01"))
                        projected_fixture_liability = (existing_fixture_liability + max_winning).quantize(Decimal("0.01"))
                        decision = auto_suspend_if_needed(
                            actor=request.user,
                            fixture_id=s["fixture"].id,
                            market_key=s["market_key"],
                            selection_key=s["selection_key"],
                            projected_selection_liability=projected_selection_liability,
                            projected_market_liability=projected_market_liability,
                            projected_fixture_liability=projected_fixture_liability,
                        )
                        if decision.suspended:
                            admin_user_ids = list(
                                User.objects.filter(Q(is_superuser=True) | Q(user_type__in=["admin", "account_user"]))
                                .values_list("id", flat=True)[:200]
                            )
                            alert_payload = {
                                "fixture_id": s["fixture"].id,
                                "market_key": s["market_key"],
                                "selection_key": s["selection_key"],
                                "projected_selection_liability": str(projected_selection_liability),
                                "projected_market_liability": str(projected_market_liability),
                                "projected_fixture_liability": str(projected_fixture_liability),
                            }

                            def _dispatch_suspension_notifications():
                                for admin_user in User.objects.filter(id__in=admin_user_ids).only("id"):
                                    try:
                                        create_notification(
                                            recipient=admin_user,
                                            notification_type="EVENT_SUSPENDED",
                                            title="Risk Auto-Suspension Triggered",
                                            message=f"Auto-suspended {decision.level} due to exposure. Fixture #{s['fixture'].id}.",
                                            data=alert_payload,
                                        )
                                    except Exception:
                                        continue

                            run_after_commit_in_background(_dispatch_suspension_notifications)
                            return fail_response('This event/market is temporarily suspended due to high exposure.')

                    duplicate_signature = compute_duplicate_ticket_signature(
                        user_id=request.user.id,
                        selections=selections_data,
                        stake_per_line=str(stake_amount_per_line),
                        is_system_bet=is_system_bet,
                        permutation_count=permutation_count,
                        fingerprint_hash=fingerprint_hash,
                        ip_address=ip_address,
                    )
                    def _resolve_agent_for_user(u):
                        if not u:
                            return None
                        if u.user_type in ['agent', 'super_agent', 'master_agent']:
                            return u
                        if getattr(u, 'agent', None):
                            return u.agent
                        if getattr(u, 'super_agent', None):
                            return u.super_agent
                        if getattr(u, 'master_agent', None):
                            return u.master_agent
                        return None

                    agent_obj = _resolve_agent_for_user(request.user)

                    try:
                        limits = validate_ticket_against_limits(
                            user=request.user,
                            ticket_type=bet_type,
                            selection_count=len(valid_selections),
                            total_stake=total_stake,
                            max_winning=max_winning,
                            ticket_odds=max_line_odd,
                        )
                    except BettingLimitViolation as e:
                        BettingLimitAuditLog.objects.create(
                            action_type='TICKET_REJECTED',
                            actor=request.user,
                            agent=agent_obj,
                            affected_user=request.user,
                            ip_address=get_client_ip(request),
                            message=e.message,
                            data={
                                'code': e.code,
                                'bet_type': bet_type,
                                'selection_count': len(valid_selections),
                                'system_min_count': system_min_count,
                                'stake_per_line': str(stake_amount_per_line),
                                'total_stake': str(total_stake),
                                'ticket_max_winning': str(max_winning),
                                'ticket_potential_winning': str(potential_win),
                                'ticket_odds': str(max_line_odd),
                                'limits': (e.data.get('limits') if isinstance(e.data, dict) else {}) or {},
                                'details': e.data,
                            }
                        )

                        try:
                            reject_key = f"betting_limits:rejects:u:{request.user.id}"
                            cnt = int(cache.get(reject_key) or 0) + 1
                            cache.set(reject_key, cnt, timeout=600)
                            if cnt >= 5:
                                ActivityLog.objects.create(
                                    user=request.user,
                                    action_type='RISK',
                                    action=f"Repeated ticket rejections due to limits. Count:{cnt} LastReason:{e.code}",
                                    affected_object=f"User: {request.user.id}",
                                    ip_address=get_client_ip(request),
                                    path=request.path
                                )
                        except Exception:
                            pass

                        return fail_response(e.message)

                    try:
                        user_wallet = Wallet.objects.select_for_update().get(user=request.user)
                    except Wallet.DoesNotExist:
                        return fail_response('User wallet not found.')

                    if user_wallet.balance < total_stake:
                        return fail_response(f'Insufficient balance. Required: ₦{total_stake:.2f}, Available: ₦{user_wallet.balance:.2f}')

                    # Create Single BetTicket
                    ip = get_client_ip(request)
                    bonus_rule_obj = BonusRule.objects.filter(id=rule_dict['id']).first() if rule_dict else None
                    limits_snapshot = serialize_limits(limits)
                    limits_snapshot.update({
                        'applied_ticket_type': bet_type,
                        'stake_per_line': str(stake_amount_per_line),
                        'total_stake': str(total_stake),
                        'ticket_odds': str(max_line_odd),
                    })
                    limits_snapshot['selections_snapshot'] = [
                        {
                            'fixture_id': s['fixture'].id,
                            'home_team': s['fixture'].home_team,
                            'away_team': s['fixture'].away_team,
                            'match_date': str(getattr(s['fixture'], 'match_date', '') or ''),
                            'match_time': str(getattr(s['fixture'], 'match_time', '') or ''),
                            'bet_type': s['bet_type'],
                            'odd_selected': str(s['odd']),
                        }
                        for s in valid_selections
                    ]
                    bet_ticket = BetTicket.objects.create(
                        user=request.user,
                        stake_amount=total_stake, # Total stake for the ticket
                        total_odd=total_ticket_odd,
                        potential_winning=potential_win,
                        min_winning=min_winning,
                        max_winning=max_winning,
                        status='pending',
                        bet_type=bet_type,
                        system_min_count=system_min_count,
                        original_selections_count=len(valid_selections),
                        placed_ip=ip,
                        bonus_rule=bonus_rule_obj,
                        bonus_percentage_applied=(rule_dict['pct'] if rule_dict else Decimal('0.0000')),
                        bonus_base=(rule_dict['base'] if rule_dict else 'gross'),
                        betting_limits_snapshot=limits_snapshot
                    )
                    
                    # Create Selections
                    for sel in valid_selections:
                        Selection.objects.create(
                            bet_ticket=bet_ticket,
                            fixture=sel['fixture'],
                            betting_period=sel['fixture'].betting_period,
                            fixture_serial_number=str(getattr(sel['fixture'], 'serial_number', '') or ''),
                            fixture_home_team=sel['fixture'].home_team,
                            fixture_away_team=sel['fixture'].away_team,
                            fixture_match_date=getattr(sel['fixture'], 'match_date', None),
                            fixture_match_time=getattr(sel['fixture'], 'match_time', None),
                            bet_type=sel['bet_type'],
                            odd_selected=sel['odd']
                        )

                    try:
                        log_duplicate_ticket_if_needed(
                            user=request.user,
                            ticket=bet_ticket,
                            signature=duplicate_signature,
                            ip_address=ip_address,
                            fingerprint_hash=fingerprint_hash,
                        )
                    except Exception:
                        pass

                    try:
                        risk_score = evaluate_ticket_risk(
                            user=request.user,
                            ticket=bet_ticket,
                            ip_address=ip_address,
                            fingerprint_hash=fingerprint_hash,
                            selections=valid_selections,
                            stake_amount=total_stake,
                        )
                        if risk_score >= 70:
                            admin_user_ids = list(
                                User.objects.filter(Q(is_superuser=True) | Q(user_type__in=["admin", "account_user"]))
                                .values_list("id", flat=True)[:200]
                            )
                            risk_alert_data = {
                                "user_id": request.user.id,
                                "ticket_id": bet_ticket.ticket_id,
                                "risk_score": risk_score,
                            }
                            risk_alert_message = (
                                f"User {request.user.email} triggered a risk score of {risk_score} on ticket {bet_ticket.ticket_id}."
                            )

                            def _dispatch_risk_alert_notifications():
                                for admin_user in User.objects.filter(id__in=admin_user_ids).only("id"):
                                    try:
                                        create_notification(
                                            recipient=admin_user,
                                            notification_type="RISK_ALERT",
                                            title="Risk Alert: Suspicious Betting",
                                            message=risk_alert_message,
                                            data=risk_alert_data,
                                        )
                                    except Exception:
                                        continue

                            run_after_commit_in_background(_dispatch_risk_alert_notifications)
                    except Exception:
                        pass

                    fixture_ids_for_refresh = {s["fixture"].id for s in valid_selections}
                    agent_id_for_refresh = getattr(agent_obj, "id", None)
                    period_id_for_refresh = None
                    if valid_selections and valid_selections[0].get("fixture"):
                        period_id_for_refresh = getattr(valid_selections[0]["fixture"], "betting_period_id", None)
                    user_id_for_refresh = request.user.id

                    def _risk_refresh_sync():
                        try:
                            from risk.services import (
                                update_agent_exposure_snapshot,
                                update_betting_period_liability_snapshot,
                                update_liability_snapshots_for_fixture,
                                update_user_exposure_snapshot,
                            )

                            for fid in fixture_ids_for_refresh:
                                try:
                                    update_liability_snapshots_for_fixture(fid)
                                except Exception:
                                    continue
                            if agent_id_for_refresh:
                                try:
                                    update_agent_exposure_snapshot(agent_id_for_refresh)
                                except Exception:
                                    pass
                            try:
                                update_user_exposure_snapshot(user_id_for_refresh)
                            except Exception:
                                pass
                            if period_id_for_refresh:
                                try:
                                    update_betting_period_liability_snapshot(period_id_for_refresh)
                                except Exception:
                                    pass
                        except Exception:
                            return

                    def _risk_workers_available():
                        cache_key = "risk:celery_workers_available"
                        cached = cache.get(cache_key)
                        if cached is not None:
                            return bool(cached)
                        ok = False
                        try:
                            from celery import current_app
                            insp = current_app.control.inspect(timeout=0.5)
                            res = insp.ping() if insp else None
                            ok = bool(res)
                        except Exception:
                            ok = False
                        cache.set(cache_key, ok, timeout=15)
                        return ok

                    def _schedule_risk_refresh():
                        is_test_run = any(arg in ("test", "pytest") for arg in (sys.argv or []))
                        if is_test_run or getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False) or getattr(settings, "CELERY_ALWAYS_EAGER", False):
                            _risk_refresh_sync()
                            return

                        if _risk_workers_available():
                            try:
                                from risk.tasks import (
                                    refresh_agent_exposure,
                                    refresh_betting_period_liability,
                                    refresh_fixture_liability,
                                    refresh_user_exposure,
                                )

                                for fid in fixture_ids_for_refresh:
                                    refresh_fixture_liability.delay(fid)
                                if agent_id_for_refresh:
                                    refresh_agent_exposure.delay(agent_id_for_refresh)
                                refresh_user_exposure.delay(user_id_for_refresh)
                                if period_id_for_refresh:
                                    refresh_betting_period_liability.delay(period_id_for_refresh)
                                return
                            except Exception:
                                pass

                        worker = threading.Thread(target=_risk_refresh_sync, daemon=True)
                        worker.start()

                    try:
                        db_transaction.on_commit(_schedule_risk_refresh)
                    except Exception:
                        pass

                    # Transaction Record (Summary)
                    tx = Transaction.objects.create(
                        user=request.user,
                        transaction_type='bet_placement',
                        amount=total_stake,
                        is_successful=True,
                        status='completed',
                        description=f"Placed bet. Type: {bet_type.title()}. Ticket ID: {bet_ticket.ticket_id}",
                        related_bet_ticket=bet_ticket,
                        timestamp=timezone.now()
                    )
                    user_wallet.apply_delta(
                        amount=-total_stake,
                        actor=request.user,
                        transaction_obj=tx,
                        reference=str(bet_ticket.ticket_id),
                        reason=tx.description,
                        metadata={"ticket_id": bet_ticket.ticket_id, "bet_type": bet_type},
                    )

                    # Refresh wallet to get the latest balance
                    user_wallet.refresh_from_db()

                    if bet_type == 'system' and bet_ticket.bonus_percentage_applied and bet_ticket.bonus_percentage_applied > 0:
                        try:
                            ip_users_key = f"sysbonus:ip:{ip}:users"
                            recent_users = cache.get(ip_users_key) or []
                            if request.user.id not in recent_users:
                                recent_users.append(request.user.id)
                            cache.set(ip_users_key, recent_users, timeout=600)

                            sig_payload = f"{bet_type}:{system_min_count}:{sorted([(str(s['fixture'].id), s['bet_type']) for s in valid_selections])}:{stake_amount_str}"
                            sig_hash = hashlib.sha256(sig_payload.encode('utf-8')).hexdigest()
                            sig_key = f"sysbonus:sig:{request.user.id}:{sig_hash}"
                            duplicate_sig = cache.get(sig_key) is not None
                            cache.set(sig_key, 1, timeout=120)

                            if len(recent_users) >= 4 or duplicate_sig:
                                ActivityLog.objects.create(
                                    user=request.user,
                                    action_type='RISK',
                                    action=f"System-bet bonus risk flag. UsersOnIP:{len(recent_users)} DuplicateSig:{duplicate_sig} BonusPct:{bet_ticket.bonus_percentage_applied} Ticket:{bet_ticket.ticket_id}",
                                    affected_object=f"BetTicket: {bet_ticket.ticket_id}",
                                    ip_address=ip,
                                    path=request.path
                                )
                        except Exception:
                            pass

                    if placement_lock_key:
                        release_ticket_placement_lock(placement_lock_key)

                    return JsonResponse({
                        'success': True, 
                        'message': f'Successfully placed bet!',
                        'ticket_id': bet_ticket.ticket_id,
                        'new_balance': str(user_wallet.balance)
                    })

                except json.JSONDecodeError:
                    return JsonResponse({'success': False, 'message': 'Invalid data format.'})
                except Exception as e:
                    logger.error(f"Error placing bet: {e}")
                    traceback.print_exc()
                    if idempotency_key:
                        cache.delete(idempotency_key)
                    if placement_lock_key:
                        release_ticket_placement_lock(placement_lock_key)
                    return JsonResponse({'success': False, 'message': f'An error occurred: {str(e)}'})

            # Fallback to old single bet form logic
            elif 'fixture_id' in request.POST:
                form = BetTicketForm(request.POST)
                if form.is_valid():
                    # Retrieve data from form
                    fixture_id = form.cleaned_data['fixture_id']
                    selected_outcome = form.cleaned_data['selected_outcome']
                    stake_amount = form.cleaned_data['stake_amount']

                    fixture = get_object_or_404(Fixture, id=fixture_id)

                    # Basic validation: ensure fixture is still open for betting
                    if fixture.status != 'scheduled':
                        messages.error(request, 'Betting is closed for this fixture.')
                        return redirect('betting:fixtures')
                    
                    # Ensure the betting period is active
                    if not fixture.betting_period.is_active or fixture.betting_period.end_date < timezone.now().date():
                        messages.error(request, 'The betting period for this fixture is closed.')
                        return redirect('betting:fixtures')

                    # Validate match time (Strict Date/Time Enforcement)
                    local_now = timezone.localtime(timezone.now())
                    if fixture.match_date < local_now.date() or (fixture.match_date == local_now.date() and fixture.match_time <= local_now.time()):
                        messages.error(request, 'Betting is closed for this fixture (Match Started).')
                        return redirect('betting:fixtures')

                    # Get the correct odd based on selected_outcome
                    if selected_outcome == 'home_win':
                        odd = fixture.home_win_odd 
                    elif selected_outcome == 'draw':
                        odd = fixture.draw_odd
                    elif selected_outcome == 'away_win':
                        odd = fixture.away_win_odd 
                    elif selected_outcome == 'home_dnb':
                        odd = fixture.home_dnb_odd
                    elif selected_outcome == 'away_dnb':
                        odd = fixture.away_dnb_odd
                    elif selected_outcome == 'over_1_5':
                        odd = fixture.over1_5_odd
                    elif selected_outcome == 'under_1_5':
                        odd = fixture.under1_5_odd
                    elif selected_outcome == 'over_2_5':
                        odd = fixture.over2_5_odd
                    elif selected_outcome == 'under_2_5':
                        odd = fixture.under2_5_odd
                    elif selected_outcome == 'over_3_5':
                        odd = fixture.over3_5_odd
                    elif selected_outcome == 'under_3_5':
                        odd = fixture.under3_5_odd
                    elif selected_outcome == 'btts_yes':
                        odd = fixture.btts_yes_odd
                    elif selected_outcome == 'btts_no':
                        odd = fixture.btts_no_odd
                    else:
                        messages.error(request, 'Invalid outcome selected.')
                        return redirect('betting:fixtures')

                    # Check if user has sufficient balance
                    user_wallet = get_object_or_404(Wallet.objects.select_for_update(), user=request.user)
                    if user_wallet.balance < stake_amount:
                        messages.error(request, 'Insufficient balance to place this bet.')
                        return redirect('betting:wallet')

                    # Calculate potential winning and max winning (assuming max_winning = potential_winning for simple bets)
                    potential_winning = stake_amount * odd
                    max_winning = potential_winning # For single bet, max_winning is same as potential_winning

                    placement_lock_key = acquire_ticket_placement_lock(request.user.id)
                    if not placement_lock_key:
                        messages.error(request, 'Too many requests. Please retry.')
                        return redirect('betting:fixtures')

                    try:
                        limits = validate_ticket_against_limits(
                            user=request.user,
                            ticket_type='single',
                            selection_count=1,
                            total_stake=stake_amount,
                            max_winning=max_winning,
                            ticket_odds=odd,
                        )
                    except BettingLimitViolation as e:
                        BettingLimitAuditLog.objects.create(
                            action_type='TICKET_REJECTED',
                            actor=request.user,
                            agent=(request.user if request.user.user_type in ['agent', 'super_agent', 'master_agent'] else (request.user.agent or request.user.super_agent or request.user.master_agent)),
                            affected_user=request.user,
                            ip_address=get_client_ip(request),
                            message=e.message,
                            data={'code': e.code, 'details': e.data}
                        )
                        release_ticket_placement_lock(placement_lock_key)
                        messages.error(request, e.message)
                        return redirect('betting:fixtures')

                    # Create BetTicket
                    limits_snapshot = serialize_limits(limits)
                    limits_snapshot.update({
                        'applied_ticket_type': 'single',
                        'stake_per_line': str(stake_amount),
                        'total_stake': str(stake_amount),
                        'ticket_odds': str(odd),
                    })
                    limits_snapshot['selections_snapshot'] = [
                        {
                            'fixture_id': fixture.id,
                            'home_team': fixture.home_team,
                            'away_team': fixture.away_team,
                            'match_date': str(getattr(fixture, 'match_date', '') or ''),
                            'match_time': str(getattr(fixture, 'match_time', '') or ''),
                            'bet_type': selected_outcome,
                            'odd_selected': str(odd),
                        }
                    ]
                    bet_ticket = BetTicket.objects.create(
                        user=request.user,
                        stake_amount=stake_amount,
                        total_odd=odd, # For a single bet, total_odd is just the odd of the selected outcome
                        potential_winning=potential_winning,
                        max_winning=max_winning,
                        status='pending',
                        betting_limits_snapshot=limits_snapshot
                    )
                    # Create Selection for the bet ticket
                    Selection.objects.create(
                        bet_ticket=bet_ticket,
                        fixture=fixture,
                        betting_period=fixture.betting_period,
                        fixture_serial_number=str(getattr(fixture, 'serial_number', '') or ''),
                        fixture_home_team=fixture.home_team,
                        fixture_away_team=fixture.away_team,
                        fixture_match_date=getattr(fixture, 'match_date', None),
                        fixture_match_time=getattr(fixture, 'match_time', None),
                        bet_type=selected_outcome, # Corrected field name
                        odd_selected=odd # Store the odd at the time of betting
                    )

                    # Record Transaction
                    tx = Transaction.objects.create(
                        user=request.user,
                        transaction_type='bet_placement',
                        amount=stake_amount,
                        is_successful=True,
                        status='completed',
                        description=f"Bet placed on {fixture.home_team} vs {fixture.away_team} for {selected_outcome}",
                        related_bet_ticket=bet_ticket,
                        timestamp=timezone.now()
                    )
                    user_wallet.apply_delta(
                        amount=-stake_amount,
                        actor=request.user,
                        transaction_obj=tx,
                        reference=str(bet_ticket.ticket_id),
                        reason=tx.description,
                        metadata={"ticket_id": bet_ticket.ticket_id, "bet_type": "single"},
                    )

                    release_ticket_placement_lock(placement_lock_key)
                    messages.success(request, f'Bet placed successfully! Your ticket ID is {bet_ticket.ticket_id}. Potential winning: ₦{potential_winning:.2f}')
                    return redirect('betting:fixtures') # Redirect back to fixtures with errors
                else:
                    for field, errors in form.errors.items():
                        for error in errors:
                            messages.error(request, f"{field.replace('_', ' ').title()}: {error}")
                    if form.non_field_errors():
                        for error in form.non_field_errors():
                            messages.error(request, f"Betting Error: {error}")
                    return redirect('betting:fixtures') # Redirect back to fixtures with errors
            
            else:
                # Unknown request format - likely AJAX missing data
                logger.warning(f"Invalid place_bet request params: {request.POST.keys()}")
                # If it looks like the new JS form (has 'is_system_bet' or 'stake_amount') OR if it's an AJAX request
                if 'is_system_bet' in request.POST or 'stake_amount' in request.POST or request.headers.get('x-requested-with') == 'XMLHttpRequest' or 'application/json' in request.META.get('HTTP_ACCEPT', ''):
                    return JsonResponse({'success': False, 'message': 'Invalid request parameters. Missing selections data.'})
                
                messages.error(request, 'Invalid request parameters.')
                return redirect('betting:fixtures')

        messages.error(request, 'Invalid request to place bet.')
        return redirect('betting:fixtures')
    except Exception as e:
        logger.error(f"Unexpected error in place_bet: {e}")
        traceback.print_exc()
        if 'is_system_bet' in request.POST or 'stake_amount' in request.POST or request.headers.get('x-requested-with') == 'XMLHttpRequest' or 'application/json' in request.META.get('HTTP_ACCEPT', ''):
             return JsonResponse({'success': False, 'message': f'An unexpected error occurred: {str(e)}'})
        messages.error(request, 'An unexpected error occurred. Please try again.')
        return redirect('betting:fixtures')

def check_ticket_status(request):
    ticket = None
    tickets = BetTicket.objects.none()
    # Default 60 mins if not set
    void_window_str = SystemSetting.get_setting('ticket_cancellation_window_minutes', '60')
    try:
        void_window = int(void_window_str)
    except (ValueError, TypeError):
        void_window = 60

    if request.method == 'POST':
        form = CheckTicketStatusForm(request.POST) 
        if form.is_valid():
            ticket = form.ticket 
        else:
            for field, errors in form.errors.items():
                for error in errors:
                    messages.error(request, f"{field.replace('_', ' ').title()}: {error}")
            if form.non_field_errors():
                for error in form.non_field_errors():
                    messages.error(request, f"Ticket Check Error: {error}")
    else:
        form = CheckTicketStatusForm()
    
    # Populate tickets based on user hierarchy
    if request.user.is_authenticated:
        if request.user.is_superuser or request.user.user_type == 'admin':
            tickets = BetTicket.objects.all().order_by('-placed_at')
        elif is_crm_user(request.user) or is_finance_user(request.user) or is_account_user(request.user):
            tickets = BetTicket.objects.all().order_by('-placed_at')
        elif request.user.user_type == 'master_agent':
            tickets = BetTicket.objects.filter(
                Q(user__master_agent=request.user) | 
                Q(user__super_agent__master_agent=request.user) |
                Q(user__agent__master_agent=request.user) |
                Q(user__agent__super_agent__master_agent=request.user)
            ).order_by('-placed_at')
        elif request.user.user_type == 'super_agent':
             tickets = BetTicket.objects.filter(
                Q(user__super_agent=request.user) |
                Q(user__agent__super_agent=request.user)
            ).order_by('-placed_at')
        elif request.user.user_type == 'agent':
            tickets = BetTicket.objects.filter(
                Q(user__agent=request.user)
            ).order_by('-placed_at')
        else: # Player/Cashier
            tickets = BetTicket.objects.filter(user=request.user).order_by('-placed_at')

    # Apply Filters
    ticket_id_query = request.GET.get('ticket_id')
    status_query = request.GET.get('status')
    bet_type_query = request.GET.get('bet_type')
    date_from = request.GET.get('date_from')
    date_to = request.GET.get('date_to')
    settled_date_from = request.GET.get('settled_date_from')
    settled_date_to = request.GET.get('settled_date_to')

    if ticket_id_query:
        ticket_id_query = (ticket_id_query or '').strip().upper()
        if request.user.is_authenticated:
            tickets = tickets.filter(ticket_id__icontains=ticket_id_query)
        else:
            if re.fullmatch(r"[A-Z0-9]{6,8}", ticket_id_query or ""):
                tickets = BetTicket.objects.filter(ticket_id__iexact=ticket_id_query).order_by('-placed_at')
                ticket = tickets.first()
                if not ticket:
                    messages.error(request, "Ticket not found.")
            else:
                messages.error(request, "Please enter a valid Ticket ID.")
            status_query = None
            bet_type_query = None
            date_from = None
            date_to = None
            settled_date_from = None
            settled_date_to = None
    
    if status_query and status_query != 'all':
        tickets = tickets.filter(status=status_query)
        
    if bet_type_query and bet_type_query != 'all':
        tickets = tickets.filter(bet_type=bet_type_query)
        
    if date_from:
        try:
            tickets = tickets.filter(placed_at__date__gte=date_from)
        except (ValueError, TypeError): pass
        
    if date_to:
        try:
            tickets = tickets.filter(placed_at__date__lte=date_to)
        except (ValueError, TypeError): pass

    if settled_date_from:
        try:
            # Filter by last_updated for settled tickets
            tickets = tickets.filter(last_updated__date__gte=settled_date_from).exclude(status='pending')
        except (ValueError, TypeError): pass
        
    if settled_date_to:
        try:
            tickets = tickets.filter(last_updated__date__lte=settled_date_to).exclude(status='pending')
        except (ValueError, TypeError): pass

    tickets_qs = tickets.select_related('user').prefetch_related('selections')
    tickets_paginator = Paginator(tickets_qs, 50)
    page_num = (request.GET.get('page') or '1').strip() or '1'
    try:
        tickets_page = tickets_paginator.page(page_num)
    except Exception:
        tickets_page = tickets_paginator.page(1)
    tickets_list = list(tickets_page.object_list)
    try:
        TicketVoidRequest = apps.get_model('void_requests', 'TicketVoidRequest')
        req_rows = TicketVoidRequest.objects.filter(ticket_id__in=[t.id for t in tickets_list]).values('ticket_id', 'status')
        req_map = {r['ticket_id']: r['status'] for r in req_rows}
        for t in tickets_list:
            t.void_request_status = req_map.get(t.id, '')
            t.has_void_request = bool(t.void_request_status)
    except Exception:
        for t in tickets_list:
            t.void_request_status = ''
            t.has_void_request = False

    cashier_can_request_void = False
    if request.user.is_authenticated and request.user.user_type == "cashier":
        try:
            from void_requests.services import can_cashier_request_void

            cashier_can_request_void = bool(can_cashier_request_void(request.user))
        except Exception:
            cashier_can_request_void = False

    # AJAX Polling Check
    if request.method == 'GET' and request.GET.get('action') == 'poll_tickets':
        return render(request, 'betting/partials/ticket_list_rows.html', {
            'tickets': tickets_list,
            'void_window': void_window,
            'now': timezone.now(),
            'cashier_can_request_void': cashier_can_request_void,
        })

    ticket_query = request.GET.copy()
    ticket_query.pop('page', None)
    ticket_query.pop('action', None)
    ticket_pagination_querystring = ticket_query.urlencode()

    context = {
        'form': form, 
        'ticket': ticket, 
        'tickets': tickets_list,
        'tickets_page': tickets_page,
        'ticket_page_numbers': list(
            tickets_paginator.get_elided_page_range(
                tickets_page.number,
                on_each_side=1,
                on_ends=1,
            )
        ),
        'ticket_pagination_querystring': ticket_pagination_querystring,
        'void_window': void_window,
        'now': timezone.now(),
        'cashier_can_request_void': cashier_can_request_void,
        'bet_type_choices': BetTicket.BET_TYPE_CHOICES,
        'status_choices': BetTicket.STATUS_CHOICES,
        'ticket_bonus_percent': (ticket.bonus_percentage_applied * Decimal('100')) if ticket else Decimal('0.00'),
        'ticket_estimated_bonus': (max(Decimal('0.00'), (ticket.max_winning - ticket.potential_winning)) if ticket and not ticket.bonus_is_final else (ticket.bonus_amount if ticket else Decimal('0.00'))),
        'ticket_selections_snapshot': (ticket.betting_limits_snapshot or {}).get('selections_snapshot', []) if ticket else [],
    }
    return render(request, 'betting/check_ticket.html', context)


@login_required
@db_transaction.atomic
def agent_void_ticket(request, ticket_id):
    ticket = get_object_or_404(BetTicket, ticket_id=ticket_id)
    void_window_str = SystemSetting.get_setting('ticket_cancellation_window_minutes', '60')
    try:
        void_window = int(void_window_str)
    except (ValueError, TypeError):
        void_window = 60
    
    # Permission Check
    can_void = False
    if request.user.is_superuser or request.user.user_type == 'admin':
        can_void = True
    elif request.user.user_type == 'agent':
        # Agent can only void tickets from their cashiers
        if ticket.user.user_type == 'cashier' and ticket.user.agent == request.user:
            # Check window
            time_diff = (timezone.now() - ticket.placed_at).total_seconds() / 60
            if time_diff <= void_window:
                can_void = True
            else:
                messages.error(request, "Cancellation window has expired for this ticket.")
                return redirect('betting:check_ticket_status')
        else:
             messages.error(request, "You can only void tickets placed by your cashiers.")
             return redirect('betting:check_ticket_status')
    
    if not can_void:
        messages.error(request, "You do not have permission to void this ticket.")
        return redirect('betting:check_ticket_status')

    if ticket.status in ['cancelled', 'deleted']:
        messages.error(request, "Ticket is already voided.")
        return redirect('betting:check_ticket_status')

    if ticket.status != 'pending':
         messages.error(request, "Only pending tickets can be voided.")
         return redirect('betting:check_ticket_status')

    try:
        TicketVoidRequest = apps.get_model('void_requests', 'TicketVoidRequest')
        vr = TicketVoidRequest.objects.filter(ticket=ticket, status='pending', is_processed=False).first()
        if vr:
            messages.error(request, "This ticket is already pending void approval.")
            return redirect('betting:check_ticket_status')
    except Exception:
        pass

    # Void Process
    ticket.status = 'cancelled'
    ticket.deleted_by = request.user
    ticket.deleted_at = timezone.now()
    ticket.save() # This triggers the pre_save signal to refund stake
    from commission.tasks import enqueue_refresh_weekly_commissions_for_ticket_ids
    enqueue_refresh_weekly_commissions_for_ticket_ids([str(ticket.id)])

    if request.user.is_superuser or request.user.user_type == 'admin':
        log_admin_activity(request, f"Voided ticket {ticket.ticket_id} for user {ticket.user.email}")
    
    messages.success(request, f"Ticket {ticket.ticket_id} voided and stake refunded successfully.")
    return redirect('betting:check_ticket_status')

# --- Wallet & Payments Views ---

@login_required
def wallet_view(request):
    wallet, created = Wallet.objects.get_or_create(user=request.user, defaults={'balance': Decimal('0.00')})
    if created:
        logger.info(f"Created missing wallet for user {request.user.email} in wallet_view.")
    transactions = Transaction.objects.filter(user=request.user).order_by('-timestamp')[:20] # Last 20 transactions
    pending_withdrawals = UserWithdrawal.objects.filter(user=request.user, status='pending').order_by('-request_time') # Corrected field name

    if request.user.maybe_auto_unlock_withdrawal():
        request.user.save(update_fields=['withdrawal_locked', 'withdrawal_attempts', 'withdrawal_locked_at'])
    if request.user.withdrawal_locked and not request.user.withdrawal_locked_at:
        request.user.withdrawal_locked_at = timezone.now()
        request.user.save(update_fields=['withdrawal_locked_at'])

    # Initialize forms based on user type
    wallet_transfer_form = None
    credit_request_form = None
    overdraft_request_form = None
    
    # Cashiers can only request credit, not transfer freely (unless logic changes)
    # Agents/Super/Master can transfer AND request credit
    if request.user.user_type == 'cashier':
        credit_request_form = CreditRequestForm(user=request.user)
    elif request.user.user_type in ['agent', 'super_agent', 'master_agent', 'account_user']:
        # Check permission for Master/Super Agents
        if request.user.user_type in ['master_agent', 'super_agent'] and not getattr(request.user, 'can_manage_downline_wallets', True):
            wallet_transfer_form = None
        else:
            wallet_transfer_form = WalletTransferForm(sender_user=request.user)
        if request.user.user_type in ['agent', 'super_agent']:
            overdraft_request_form = OverdraftRequestForm(user=request.user)
        else:
            credit_request_form = CreditRequestForm(user=request.user)

    # Active Loans
    active_loans = Loan.objects.filter(
        borrower=request.user,
        status__in=['active', 'overdue', 'defaulted'],
        outstanding_balance__gt=Decimal('0.00'),
    ).order_by('due_date', '-created_at')
    pending_loan_requests = Loan.objects.filter(borrower=request.user, status='pending').order_by('-created_at')
    qualification_snapshot = build_qualification_snapshot(request.user) if request.user.user_type in ['agent', 'super_agent'] else None
    outstanding_overdraft_amount = get_user_outstanding_loan_amount(request.user) if request.user.user_type in ['agent', 'super_agent'] else Decimal('0.00')
    pending_remittance_credit = get_user_pending_credit_amount(request.user) if request.user.user_type in ['agent', 'super_agent'] else Decimal('0.00')
    withdrawal_lock_expires_at = request.user.get_withdrawal_lock_expires_at()
    can_withdraw = request.user.user_type in ['agent', 'super_agent', 'master_agent', 'account_user', 'finance', 'admin', 'retail_manager', 'crm']
    if user_has_outstanding_loan(request.user):
        can_withdraw = False
    can_transfer_from_wallet = can_user_transfer_from_wallet(request.user)

    primary_outstanding_loan = active_loans.first()

    context = {
        'wallet': wallet,
        'transactions': transactions,
        'initiate_deposit_form': InitiateDepositForm(),
        'withdraw_funds_form': WithdrawFundsForm(user=request.user), # Pass user for validation
        'wallet_transfer_form': wallet_transfer_form,
        'credit_request_form': credit_request_form,
        'overdraft_request_form': overdraft_request_form,
        'active_loans': active_loans,
        'primary_outstanding_loan': primary_outstanding_loan,
        'pending_loan_requests': pending_loan_requests,
        'qualification_snapshot': qualification_snapshot,
        'outstanding_overdraft_amount': outstanding_overdraft_amount,
        'pending_remittance_credit': pending_remittance_credit,
        'loan_settlement_form': LoanSettlementForm(request=request),
        'pending_withdrawals': pending_withdrawals,
        'withdrawal_pin_is_set': request.user.withdrawal_pin_is_set,
        'withdrawal_locked': request.user.withdrawal_locked,
        'withdrawal_attempts': request.user.withdrawal_attempts,
        'withdrawal_lock_expires_at': withdrawal_lock_expires_at,
        'paystack_public_key': settings.PAYSTACK_PUBLIC_KEY,
        'min_operating_balance': Decimal('5000.00'),
        'can_withdraw_from_wallet': can_withdraw,
        'can_transfer_from_wallet': can_transfer_from_wallet,
    }
    return render(request, 'betting/wallet.html', context)


@login_required
def api_wallet_overdraft_status(request):
    return JsonResponse({'status': 'success', **build_wallet_overdraft_payload(request.user)})


@login_required
@require_POST
def remit_overdraft_pending_credit_view(request):
    if request.user.user_type not in ['agent', 'super_agent']:
        return JsonResponse({'status': 'error', 'message': 'Only agents and super agents can remit overdraft credits.'}, status=403)
    try:
        result = remit_overdraft_pending_credit(
            user=request.user,
            actor=request.user,
            ip_address=get_client_ip(request),
        )
        payload = build_wallet_overdraft_payload(request.user)
        return JsonResponse(
            {
                'status': 'success',
                'message': (
                    f"Overdraft remitted successfully. Loan repayment: ₦{result['repaid_amount']}. "
                    f"Wallet credit: ₦{result['wallet_credit_amount']}."
                ),
                **payload,
            }
        )
    except LoanOverdraftError as exc:
        return JsonResponse({'status': 'error', 'message': str(exc)}, status=400)


@login_required
def deposit_status(request, reference):
    reference = (reference or "").strip()
    if not reference:
        raise Http404("Missing reference")

    tx = (
        Transaction.objects.filter(transaction_type="deposit")
        .filter(Q(external_reference=reference) | Q(paystack_reference=reference))
        .select_related("user")
        .first()
    )
    if not tx:
        raise Http404("Deposit not found")

    is_admin_viewer = bool(request.user.is_superuser or request.user.user_type in ["admin", "finance", "account_user"])
    if not is_admin_viewer and tx.user_id != request.user.id:
        return HttpResponseForbidden("Not allowed.")

    logs = PaymentGatewayEventLog.objects.filter(transaction=tx).order_by("-created_at")[:200]

    context = {
        "tx": tx,
        "reference": reference,
        "gateway_logs": logs,
        "is_admin_viewer": is_admin_viewer,
    }
    return render(request, "betting/deposit_status.html", context)

@login_required
@ratelimit(key=_ratelimit_key_user_or_ip, rate="10/m", method="POST", block=True)
@db_transaction.atomic
def initiate_deposit(request):
    if request.method == 'POST':
        # Handle JSON Request (AJAX)
        if request.content_type == 'application/json' or request.headers.get('Content-Type', '').startswith('application/json'):
            try:
                data = json.loads(request.body)
                gateway = (data.get('gateway') or 'paystack').strip().lower()
                if gateway not in {'paystack', 'monnify', 'kora'}:
                    return JsonResponse({'status': 'error', 'message': 'Unsupported gateway.'}, status=400)

                amount = _quantize_amount(data.get('amount', 0))
                
                if not amount or amount <= 0:
                     return JsonResponse({'status': 'error', 'message': 'Invalid amount.'}, status=400)
                
                reference = str(uuid.uuid4())
                
                # Create a pending transaction record
                tx = Transaction.objects.create(
                    user=request.user,
                    transaction_type='deposit',
                    amount=amount,
                    status='pending',
                    description=f'Pending online deposit via {gateway.capitalize()}',
                    payment_gateway=gateway,
                    external_reference=reference, # Use external_reference for all gateways
                    timestamp=timezone.now()
                )
                PaymentGatewayEventLog.objects.create(
                    gateway=gateway,
                    event_type='init',
                    reference=reference,
                    transaction=tx,
                    user=request.user,
                    amount=amount,
                    success=True,
                    payload={'mode': 'json', 'email': request.user.email, 'amount': str(amount)},
                )
                
                # Logic for different gateways
                if gateway == 'paystack':
                    return JsonResponse({
                        'status': 'success',
                        'gateway': 'paystack',
                        'email': request.user.email,
                        'amount': int(amount * Decimal("100")), # Amount in kobo
                        'reference': reference,
                        'public_key': settings.PAYSTACK_PUBLIC_KEY
                    })
                elif gateway == 'monnify':
                    # Monnify initialization requires an access token
                    auth_url = f"{os.getenv('MONNIFY_BASE_URL')}/api/v1/auth/login"
                    auth_headers = {
                        "Authorization": f"Basic {os.getenv('MONNIFY_API_KEY')}:{os.getenv('MONNIFY_SECRET_KEY')}"
                    }
                    # We'll just return the keys for frontend SDK if possible, or handle server-side
                    # For simplicity in this example, we return data for the frontend Monnify SDK
                    return JsonResponse({
                        'status': 'success',
                        'gateway': 'monnify',
                        'email': request.user.email,
                        'amount': str(amount),
                        'reference': reference,
                        'apiKey': os.getenv('MONNIFY_API_KEY'),
                        'contractCode': os.getenv('MONNIFY_CONTRACT_CODE')
                    })
                elif gateway == 'kora':
                    public_key = os.getenv('KORA_PUBLIC_KEY') or os.getenv('KORAPAY_PUBLIC_KEY')
                    if not public_key:
                        return JsonResponse({'status': 'error', 'message': 'Kora is not configured (missing public key).'}, status=500)
                    return JsonResponse({
                        'status': 'success',
                        'gateway': 'kora',
                        'email': request.user.email,
                        'amount': str(amount),
                        'reference': reference,
                        'publicKey': public_key
                    })
                else:
                    return JsonResponse({'status': 'error', 'message': 'Unsupported gateway.'}, status=400)

            except Exception as e:
                logger.error(f"Error in initiate_deposit API: {str(e)}")
                return JsonResponse({'status': 'error', 'message': str(e)}, status=500)

        # Handle Form Request (Traditional Redirect)
        gateway = request.POST.get('gateway', 'paystack')
        form = InitiateDepositForm(request.POST)
        if form.is_valid():
            amount = form.cleaned_data['amount']
            reference = str(uuid.uuid4())

            # Create a pending transaction record
            tx = Transaction.objects.create(
                user=request.user,
                transaction_type='deposit',
                amount=amount,
                status='pending',
                description=f'Pending online deposit via {gateway.capitalize()}',
                payment_gateway=gateway,
                external_reference=reference,
                timestamp=timezone.now()
            )
            PaymentGatewayEventLog.objects.create(
                gateway=gateway,
                event_type='init',
                reference=reference,
                transaction=tx,
                user=request.user,
                amount=amount,
                success=True,
                payload={'mode': 'form', 'email': request.user.email, 'amount': str(amount)},
            )

            if gateway == 'paystack':
                paystack_secret_key = settings.PAYSTACK_SECRET_KEY
                url = "https://api.paystack.co/transaction/initialize"
                headers = {
                    "Authorization": f"Bearer {paystack_secret_key}",
                    "Content-Type": "application/json"
                }
                payload = {
                    "email": request.user.email,
                    "amount": int(amount * 100),
                    "reference": reference,
                    "callback_url": request.build_absolute_uri(reverse('betting:verify_deposit')),
                    "metadata": {
                        "user_id": str(request.user.id),
                        "user_email": request.user.email,
                        "gateway": "paystack"
                    }
                }
                try:
                    response = requests.post(url, headers=headers, data=json.dumps(payload), timeout=10)
                    response.raise_for_status()
                    response_data = response.json()
                    PaymentGatewayEventLog.objects.create(
                        gateway='paystack',
                        event_type='init',
                        reference=reference,
                        transaction=tx,
                        user=request.user,
                        amount=amount,
                        success=bool(response_data.get('status')),
                        http_status=getattr(response, 'status_code', None),
                        message=str(response_data.get('message') or ''),
                        payload={'request': payload, 'response': response_data},
                    )
                    if response_data['status']:
                        return redirect(response_data['data']['authorization_url'])
                    else:
                        messages.error(request, f"Paystack initialization failed: {response_data['message']}")
                except Exception as e:
                    PaymentGatewayEventLog.objects.create(
                        gateway='paystack',
                        event_type='init',
                        reference=reference,
                        transaction=tx,
                        user=request.user,
                        amount=amount,
                        success=False,
                        message=str(e),
                        payload={'request': payload},
                    )
                    messages.error(request, f"Error initiating Paystack payment: {e}")
            
            elif gateway == 'monnify':
                # Monnify Server-side initialization
                auth_url = f"{os.getenv('MONNIFY_BASE_URL')}/api/v1/auth/login"
                api_key = os.getenv('MONNIFY_API_KEY')
                secret_key = os.getenv('MONNIFY_SECRET_KEY')
                import base64
                auth_str = base64.b64encode(f"{api_key}:{secret_key}".encode()).decode()
                
                try:
                    auth_response = requests.post(auth_url, headers={"Authorization": f"Basic {auth_str}"}, timeout=10)
                    auth_data = auth_response.json()
                    if auth_data['requestSuccessful']:
                        token = auth_data['responseBody']['accessToken']
                        init_url = f"{os.getenv('MONNIFY_BASE_URL')}/api/v1/merchant/transactions/init-transaction"
                        init_payload = {
                            "amount": float(amount),
                            "customerName": f"{request.user.first_name} {request.user.last_name}",
                            "customerEmail": request.user.email,
                            "paymentReference": reference,
                            "paymentDescription": "Wallet Deposit",
                            "currencyCode": "NGN",
                            "contractCode": os.getenv('MONNIFY_CONTRACT_CODE'),
                            "redirectUrl": request.build_absolute_uri(reverse('betting:verify_monnify_deposit')),
                            "paymentMethods": ["CARD", "ACCOUNT_TRANSFER"]
                        }
                        init_response = requests.post(init_url, headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"}, data=json.dumps(init_payload), timeout=10)
                        init_data = init_response.json()
                        PaymentGatewayEventLog.objects.create(
                            gateway='monnify',
                            event_type='init',
                            reference=reference,
                            transaction=tx,
                            user=request.user,
                            amount=amount,
                            success=bool(init_data.get('requestSuccessful')),
                            http_status=getattr(init_response, 'status_code', None),
                            message=str(init_data.get('responseMessage') or ''),
                            payload={'request': init_payload, 'response': init_data},
                        )
                        if init_data['requestSuccessful']:
                            return redirect(init_data['responseBody']['checkoutUrl'])
                        else:
                            messages.error(request, f"Monnify initialization failed: {init_data['responseMessage']}")
                    else:
                        messages.error(request, "Monnify authentication failed.")
                except Exception as e:
                    PaymentGatewayEventLog.objects.create(
                        gateway='monnify',
                        event_type='init',
                        reference=reference,
                        transaction=tx,
                        user=request.user,
                        amount=amount,
                        success=False,
                        message=str(e),
                        payload={'request': {'paymentReference': reference}},
                    )
                    messages.error(request, f"Error initiating Monnify payment: {e}")

            elif gateway == 'kora':
                # Kora Server-side initialization
                base_url = os.getenv('KORA_BASE_URL') or os.getenv('KORAPAY_BASE_URL') or "https://api.korapay.com/merchant/api/v1"
                if base_url.rstrip('/').endswith('/merchant/api'):
                    base_url = f"{base_url.rstrip('/')}/v1"

                secret_key = os.getenv('KORA_SECRET_KEY') or os.getenv('KORAPAY_SECRET_KEY')
                if not secret_key:
                    messages.error(request, "Kora is not configured (missing KORA_SECRET_KEY).")
                    return redirect('betting:wallet')
                url = f"{base_url.rstrip('/')}/charges/initialize"
                headers = {
                    "Authorization": f"Bearer {secret_key}",
                    "Content-Type": "application/json"
                }
                payload = {
                    "amount": float(amount),
                    "currency": "NGN",
                    "reference": reference,
                    "notification_url": request.build_absolute_uri(reverse('betting:kora_webhook')),
                    "redirect_url": request.build_absolute_uri(reverse('betting:verify_kora_deposit')),
                    "customer": {
                        "name": f"{request.user.first_name} {request.user.last_name}",
                        "email": request.user.email
                    }
                }
                try:
                    response = requests.post(url, headers=headers, data=json.dumps(payload), timeout=10)
                    response.raise_for_status()
                    response_data = response.json()
                    PaymentGatewayEventLog.objects.create(
                        gateway='kora',
                        event_type='init',
                        reference=reference,
                        transaction=tx,
                        user=request.user,
                        amount=amount,
                        success=bool(response_data.get('status')),
                        http_status=getattr(response, 'status_code', None),
                        message=str(response_data.get('message') or ''),
                        payload={'request': payload, 'response': response_data},
                    )
                    if response_data['status']:
                        return redirect(response_data['data']['checkout_url'])
                    else:
                        messages.error(request, f"Kora initialization failed: {response_data['message']}")
                except Exception as e:
                    PaymentGatewayEventLog.objects.create(
                        gateway='kora',
                        event_type='init',
                        reference=reference,
                        transaction=tx,
                        user=request.user,
                        amount=amount,
                        success=False,
                        message=str(e),
                        payload={'request': payload},
                    )
                    messages.error(request, f"Error initiating Kora payment: {e}")

            # If we reach here, something failed
            Transaction.objects.filter(external_reference=reference).update(status='failed')
        else:
            for field, errors in form.errors.items():
                for error in errors:
                    messages.error(request, f"{field.replace('_', ' ').title()}: {error}")
    return redirect('betting:wallet')

@login_required
@ratelimit(key=_ratelimit_key_user_or_ip, rate="20/m", method="GET", block=True)
@db_transaction.atomic
def verify_monnify_deposit(request):
    reference = request.GET.get('paymentReference')
    if not reference:
        messages.error(request, "Monnify reference not found.")
        return redirect('betting:wallet')

    # Use select_for_update() on Transaction to prevent race conditions
    transaction_record = get_object_or_404(
        Transaction.objects.select_for_update(), 
        external_reference=reference, 
        user=request.user
    )
    
    if transaction_record.status == 'completed':
        messages.success(request, "This deposit has already been successfully verified.")
        return redirect('betting:wallet')
    
    # Monnify API verification
    api_key = os.getenv('MONNIFY_API_KEY')
    secret_key = os.getenv('MONNIFY_SECRET_KEY')
    base_url = os.getenv('MONNIFY_BASE_URL')
    
    import base64
    auth_str = base64.b64encode(f"{api_key}:{secret_key}".encode()).decode()
    
    try:
        # 1. Get Access Token
        auth_url = f"{base_url}/api/v1/auth/login"
        auth_response = requests.post(auth_url, headers={"Authorization": f"Basic {auth_str}"}, timeout=10)
        auth_data = auth_response.json()
        
        if auth_data.get('requestSuccessful'):
            token = auth_data['responseBody']['accessToken']
            
            # 2. Verify Transaction
            verify_url = f"{base_url}/api/v1/merchant/transactions/query?paymentReference={reference}"
            verify_response = requests.get(verify_url, headers={"Authorization": f"Bearer {token}"}, timeout=10)
            verify_data = verify_response.json()

            payment_status = str((verify_data.get('responseBody') or {}).get('paymentStatus') or '').strip().upper()
            if verify_data.get('requestSuccessful') and payment_status == 'PAID':
                amount_verified = Decimal(str(verify_data['responseBody']['amountPaid']))
                completed = _complete_deposit_transaction(
                    tx=transaction_record,
                    amount=amount_verified,
                    gateway="monnify",
                    reference=reference,
                    source="verify",
                    payload={"response": verify_data},
                    http_status=getattr(verify_response, "status_code", None),
                    message=str(verify_data.get("responseMessage") or ""),
                )
                if completed:
                    _notify_admin_deposit_success(request.user, transaction_record, amount_verified, "monnify")
                    messages.success(request, f"Deposit of ₦{amount_verified:.2f} successful! Your wallet has been credited.")
                else:
                    messages.success(request, "This deposit has already been successfully verified.")
            else:
                msg = verify_data.get('responseMessage', 'Payment not successful')
                PaymentGatewayEventLog.objects.create(
                    gateway='monnify',
                    event_type='verify',
                    reference=reference,
                    transaction=transaction_record,
                    user=request.user,
                    amount=transaction_record.amount,
                    success=False,
                    http_status=getattr(verify_response, 'status_code', None),
                    message=str(msg or '') or f"Status: {payment_status}",
                    payload={'response': verify_data},
                )
                if payment_status and payment_status not in {"FAILED", "CANCELLED", "EXPIRED"}:
                    messages.info(
                        request,
                        "Payment is not yet confirmed. If you already completed the payment, your wallet will be credited once Monnify confirms it."
                    )
                else:
                    messages.error(request, f"Monnify verification failed: {msg}")
        else:
            PaymentGatewayEventLog.objects.create(
                gateway='monnify',
                event_type='verify',
                reference=reference,
                transaction=transaction_record,
                user=request.user,
                amount=transaction_record.amount,
                success=False,
                http_status=getattr(auth_response, 'status_code', None),
                message=str(auth_data.get('responseMessage') or 'Authentication failed'),
                payload={'response': auth_data},
            )
            messages.error(request, "Monnify authentication failed during verification.")
            
    except Exception as e:
        logger.error(f"Monnify verification error: {str(e)}")
        messages.error(request, f"Error verifying Monnify payment: {str(e)}")

    return redirect('betting:wallet')

@login_required
@ratelimit(key=_ratelimit_key_user_or_ip, rate="20/m", method="GET", block=True)
@db_transaction.atomic
def verify_kora_deposit(request):
    reference = request.GET.get('reference')
    if not reference:
        messages.error(request, "Kora reference not found.")
        return redirect('betting:wallet')

    # Use select_for_update() on Transaction to prevent race conditions
    transaction_record = get_object_or_404(
        Transaction.objects.select_for_update(), 
        external_reference=reference, 
        user=request.user
    )
    
    if transaction_record.status == 'completed':
        messages.success(request, "This deposit has already been successfully verified.")
        return redirect('betting:wallet')
    
    # Kora API verification
    secret_key = os.getenv('KORA_SECRET_KEY') or os.getenv('KORAPAY_SECRET_KEY')
    base_url = os.getenv('KORA_BASE_URL') or os.getenv('KORAPAY_BASE_URL') or "https://api.korapay.com/merchant/api/v1"
    if base_url.rstrip('/').endswith('/merchant/api'):
        base_url = f"{base_url.rstrip('/')}/v1"
    if not secret_key:
        messages.error(request, "Kora is not configured (missing KORA_SECRET_KEY).")
        return redirect('betting:wallet')
    
    try:
        verify_url = f"{base_url.rstrip('/')}/charges/{reference}"
        headers = {"Authorization": f"Bearer {secret_key}"}
        
        response = requests.get(verify_url, headers=headers, timeout=10)
        response_data = response.json()
        
        data = response_data.get('data') or {}
        status = str(data.get('status') or '').strip().lower()
        if response_data.get('status') and status == 'success':
            amount_verified = Decimal(str(response_data['data']['amount']))
            completed = _complete_deposit_transaction(
                tx=transaction_record,
                amount=amount_verified,
                gateway="kora",
                reference=reference,
                source="verify",
                payload={"response": response_data},
                http_status=getattr(response, "status_code", None),
                message=str(response_data.get("message") or ""),
            )
            if completed:
                _notify_admin_deposit_success(request.user, transaction_record, amount_verified, "kora")
                messages.success(request, f"Deposit of ₦{amount_verified:.2f} successful! Your wallet has been credited.")
            else:
                messages.success(request, "This deposit has already been successfully verified.")
        else:
            msg = response_data.get('message', 'Payment not successful')
            PaymentGatewayEventLog.objects.create(
                gateway='kora',
                event_type='verify',
                reference=reference,
                transaction=transaction_record,
                user=request.user,
                amount=transaction_record.amount,
                success=False,
                http_status=getattr(response, 'status_code', None),
                message=str(msg or ''),
                payload={'response': response_data},
            )
            if status and status not in {"failed", "cancelled"}:
                messages.info(
                    request,
                    "Payment is not yet confirmed. If you already completed the payment, your wallet will be credited once Kora confirms it."
                )
            else:
                messages.error(request, f"Kora verification failed: {msg}")
            
    except Exception as e:
        logger.error(f"Kora verification error: {str(e)}")
        PaymentGatewayEventLog.objects.create(
            gateway='kora',
            event_type='verify',
            reference=reference,
            transaction=transaction_record,
            user=request.user,
            amount=transaction_record.amount,
            success=False,
            message=str(e),
            payload={},
        )
        messages.error(request, f"Error verifying Kora payment: {str(e)}")

    return redirect('betting:wallet')


@login_required
@ratelimit(key=_ratelimit_key_user_or_ip, rate="20/m", method="GET", block=True)
@db_transaction.atomic
def verify_deposit(request):
    reference = request.GET.get('trxref') or request.GET.get('reference')
    if not reference:
        messages.error(request, "Payment reference not found.")
        return redirect('betting:wallet')

    # Use select_for_update() on Transaction to prevent race conditions
    transaction_record = get_object_or_404(
        Transaction.objects.select_for_update(), 
        external_reference=reference, 
        user=request.user
    )

    if transaction_record.status == 'completed':
        messages.success(request, "This deposit has already been successfully verified.")
        return redirect('betting:wallet')
    
    paystack_secret_key = settings.PAYSTACK_SECRET_KEY
    url = f"https://api.paystack.co/transaction/verify/{reference}"
    headers = {
        "Authorization": f"Bearer {paystack_secret_key}",
        "Content-Type": "application/json"
    }

    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        response_data = response.json()

        data = response_data.get("data") or {}
        paystack_status = str(data.get('status') or '').strip().lower()
        if response_data.get('status') and paystack_status == 'success':
            amount_verified = (Decimal(str(data.get("amount") or "0")) / Decimal("100")).quantize(Decimal("0.01"))
            completed = _complete_deposit_transaction(
                tx=transaction_record,
                amount=amount_verified,
                gateway="paystack",
                reference=reference,
                source="verify",
                payload={"response": response_data},
                http_status=getattr(response, "status_code", None),
                message=str(data.get("gateway_response") or data.get("message") or ""),
            )
            if completed:
                _notify_admin_deposit_success(request.user, transaction_record, amount_verified, "paystack")
                messages.success(request, f"Deposit of ₦{amount_verified:.2f} successful! Your wallet has been credited.")
            else:
                messages.success(request, "This deposit has already been successfully verified.")
        else:
            msg = str(data.get('gateway_response') or data.get('message') or response_data.get('message') or 'Payment not successful')
            PaymentGatewayEventLog.objects.create(
                gateway='paystack',
                event_type='verify',
                reference=reference,
                transaction=transaction_record,
                user=request.user,
                amount=transaction_record.amount,
                success=False,
                http_status=getattr(response, 'status_code', None),
                message=msg,
                payload={'response': response_data},
            )
            if paystack_status and paystack_status not in {"failed", "abandoned", "reversed"}:
                messages.info(request, "Payment received, awaiting confirmation. Your wallet will be credited once confirmed.")
            else:
                messages.error(request, f"Payment verification failed: {msg}")
            
    except requests.exceptions.Timeout:
        PaymentGatewayEventLog.objects.create(
            gateway='paystack',
            event_type='verify',
            reference=reference,
            transaction=transaction_record,
            user=request.user,
            amount=transaction_record.amount,
            success=False,
            message="Paystack verification timed out.",
            payload={},
        )
        messages.error(request, "Paystack verification timed out. Please try again (your payment will be credited once confirmed).")
    except requests.exceptions.RequestException as e:
        PaymentGatewayEventLog.objects.create(
            gateway='paystack',
            event_type='verify',
            reference=reference,
            transaction=transaction_record,
            user=request.user,
            amount=transaction_record.amount,
            success=False,
            message=str(e),
            payload={},
        )
        messages.error(request, f"Error verifying payment with Paystack: {e}")
    except json.JSONDecodeError:
        PaymentGatewayEventLog.objects.create(
            gateway='paystack',
            event_type='verify',
            reference=reference,
            transaction=transaction_record,
            user=request.user,
            amount=transaction_record.amount,
            success=False,
            message="Invalid JSON response from Paystack during verification.",
            payload={},
        )
        messages.error(request, "Invalid response from Paystack during verification.")

    return redirect('betting:wallet')


def _request_ip(request):
    ip = (request.META.get("HTTP_X_REAL_IP") or "").strip()
    if not ip:
        ip = (request.META.get("HTTP_X_FORWARDED_FOR") or "").split(",")[0].strip()
    if not ip:
        ip = (request.META.get("REMOTE_ADDR") or "").strip()
    return ip


def _ip_allowed(ip, allowlist):
    if not allowlist:
        return True
    try:
        ip_obj = ipaddress.ip_address(ip)
    except Exception:
        return False
    for entry in allowlist:
        entry = (entry or "").strip()
        if not entry:
            continue
        try:
            if "/" in entry:
                if ip_obj in ipaddress.ip_network(entry, strict=False):
                    return True
            else:
                if ip_obj == ipaddress.ip_address(entry):
                    return True
        except Exception:
            continue
    return False


def _enforce_webhook_allowlist(request, gateway):
    gateway = (gateway or "").strip().lower()
    if gateway == "paystack":
        allowlist = list(getattr(settings, "PAYSTACK_WEBHOOK_IP_ALLOWLIST", []) or [])
    elif gateway == "kora":
        allowlist = list(getattr(settings, "KORA_WEBHOOK_IP_ALLOWLIST", []) or [])
    elif gateway == "monnify":
        allowlist = list(getattr(settings, "MONNIFY_WEBHOOK_IP_ALLOWLIST", []) or [])
    else:
        allowlist = []
    if not allowlist:
        return True
    return _ip_allowed(_request_ip(request), allowlist)


@csrf_exempt
@require_POST
@ratelimit(key="ip", rate="300/m", method="POST", block=True)
def paystack_webhook(request):
    if not _enforce_webhook_allowlist(request, "paystack"):
        return HttpResponseForbidden("Not allowed.")
    secret = (getattr(settings, "PAYSTACK_WEBHOOK_SECRET", None) or getattr(settings, "PAYSTACK_SECRET_KEY", None) or "").strip()
    if not secret:
        return HttpResponse(status=500)

    raw = request.body or b""
    signature = (request.META.get("HTTP_X_PAYSTACK_SIGNATURE") or "").strip()
    expected = hmac.new(secret.encode("utf-8"), raw, hashlib.sha512).hexdigest()
    if not signature or not hmac.compare_digest(signature, expected):
        return HttpResponse(status=400)

    try:
        payload = json.loads(raw.decode("utf-8") or "{}")
    except Exception:
        return HttpResponse(status=400)

    data = payload.get("data") or {}
    reference = (data.get("reference") or "").strip()
    amount_kobo = data.get("amount")
    status = (data.get("status") or "").strip().lower()

    tx = None
    try:
        tx = Transaction.objects.filter(transaction_type="deposit", external_reference=reference).first() if reference else None
    except Exception:
        tx = None

    with db_transaction.atomic():
        if not tx:
            PaymentGatewayEventLog.objects.create(
                gateway="paystack",
                event_type="webhook",
                reference=reference,
                transaction=None,
                user=None,
                amount=None,
                success=False,
                http_status=200,
                message="Transaction not found for reference.",
                payload=payload,
            )
            return HttpResponse(status=200)

        if status == "success":
            try:
                amount_verified = (Decimal(str(amount_kobo or "0")) / Decimal("100")).quantize(Decimal("0.01"))
                _complete_deposit_transaction(
                    tx=tx,
                    amount=amount_verified,
                    gateway="paystack",
                    reference=reference,
                    source="webhook",
                    payload=payload,
                    http_status=200,
                    message=str(payload.get("event") or ""),
                )
            except Exception as e:
                PaymentGatewayEventLog.objects.create(
                    gateway="paystack",
                    event_type="webhook",
                    reference=reference,
                    transaction=tx,
                    user=tx.user,
                    amount=tx.amount,
                    success=False,
                    http_status=200,
                    message=str(e),
                    payload=payload,
                )
        else:
            PaymentGatewayEventLog.objects.create(
                gateway="paystack",
                event_type="webhook",
                reference=reference,
                transaction=tx,
                user=tx.user,
                amount=tx.amount,
                success=False,
                http_status=200,
                message=f"Non-success status: {status}",
                payload=payload,
            )

    return HttpResponse(status=200)


@csrf_exempt
@require_POST
@ratelimit(key="ip", rate="300/m", method="POST", block=True)
def kora_webhook(request):
    if not _enforce_webhook_allowlist(request, "kora"):
        return HttpResponseForbidden("Not allowed.")
    secret = (os.getenv("KORA_SECRET_KEY") or os.getenv("KORAPAY_SECRET_KEY") or "").strip()
    if not secret:
        return HttpResponse(status=500)

    raw = request.body or b""
    try:
        payload = json.loads(raw.decode("utf-8") or "{}")
    except Exception:
        return HttpResponse(status=400)

    data = payload.get("data") or {}
    signature = (request.META.get("HTTP_X_KORAPAY_SIGNATURE") or "").strip()
    data_bytes = json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    expected = hmac.new(secret.encode("utf-8"), data_bytes, hashlib.sha256).hexdigest()
    if not signature or not hmac.compare_digest(signature, expected):
        return HttpResponse(status=400)

    reference = (data.get("reference") or data.get("transaction_reference") or "").strip()
    status = (data.get("status") or "").strip().lower()
    amount_raw = data.get("amount") or data.get("amount_paid") or data.get("amountPaid")

    tx = None
    try:
        tx = Transaction.objects.filter(transaction_type="deposit", external_reference=reference).first() if reference else None
    except Exception:
        tx = None

    with db_transaction.atomic():
        if not tx:
            PaymentGatewayEventLog.objects.create(
                gateway="kora",
                event_type="webhook",
                reference=reference,
                transaction=None,
                user=None,
                amount=None,
                success=False,
                http_status=200,
                message="Transaction not found for reference.",
                payload=payload,
            )
            return HttpResponse(status=200)

        if status == "success":
            try:
                amount_verified = Decimal(str(amount_raw or "0"))
                _complete_deposit_transaction(
                    tx=tx,
                    amount=amount_verified,
                    gateway="kora",
                    reference=reference,
                    source="webhook",
                    payload=payload,
                    http_status=200,
                    message=str(payload.get("event") or ""),
                )
            except Exception as e:
                PaymentGatewayEventLog.objects.create(
                    gateway="kora",
                    event_type="webhook",
                    reference=reference,
                    transaction=tx,
                    user=tx.user,
                    amount=tx.amount,
                    success=False,
                    http_status=200,
                    message=str(e),
                    payload=payload,
                )
        else:
            PaymentGatewayEventLog.objects.create(
                gateway="kora",
                event_type="webhook",
                reference=reference,
                transaction=tx,
                user=tx.user,
                amount=tx.amount,
                success=False,
                http_status=200,
                message=f"Non-success status: {status}",
                payload=payload,
            )

    return HttpResponse(status=200)


@csrf_exempt
@require_POST
@ratelimit(key="ip", rate="300/m", method="POST", block=True)
def monnify_webhook(request):
    if not _enforce_webhook_allowlist(request, "monnify"):
        return HttpResponseForbidden("Not allowed.")

    secret = (os.getenv("MONNIFY_SECRET_KEY") or "").strip()
    if not secret:
        return HttpResponse(status=500)

    raw = request.body or b""
    signature = (request.META.get("HTTP_MONNIFY_SIGNATURE") or "").strip()
    expected = hmac.new(secret.encode("utf-8"), raw, hashlib.sha512).hexdigest()
    if not signature or not hmac.compare_digest(signature, expected):
        return HttpResponse(status=400)

    try:
        payload = json.loads(raw.decode("utf-8") or "{}")
    except Exception:
        return HttpResponse(status=400)

    event_type = (payload.get("eventType") or "").strip()
    data = payload.get("eventData") or {}
    reference = (data.get("paymentReference") or "").strip()
    status = (data.get("paymentStatus") or "").strip().upper()
    amount_raw = data.get("amountPaid") or data.get("totalPayable")

    tx = None
    try:
        tx = Transaction.objects.filter(transaction_type="deposit", external_reference=reference).first() if reference else None
    except Exception:
        tx = None

    with db_transaction.atomic():
        if not tx:
            PaymentGatewayEventLog.objects.create(
                gateway="monnify",
                event_type="webhook",
                reference=reference,
                transaction=None,
                user=None,
                amount=None,
                success=False,
                http_status=200,
                message=f"Transaction not found for reference. ({event_type})",
                payload=payload,
            )
            return HttpResponse(status=200)

        if status == "PAID":
            try:
                amount_verified = Decimal(str(amount_raw or "0")).quantize(Decimal("0.01"))
                _complete_deposit_transaction(
                    tx=tx,
                    amount=amount_verified,
                    gateway="monnify",
                    reference=reference,
                    source="webhook",
                    payload=payload,
                    http_status=200,
                    message=event_type,
                )
            except Exception as e:
                PaymentGatewayEventLog.objects.create(
                    gateway="monnify",
                    event_type="webhook",
                    reference=reference,
                    transaction=tx,
                    user=tx.user,
                    amount=tx.amount,
                    success=False,
                    http_status=200,
                    message=str(e),
                    payload=payload,
                )
        else:
            PaymentGatewayEventLog.objects.create(
                gateway="monnify",
                event_type="webhook",
                reference=reference,
                transaction=tx,
                user=tx.user,
                amount=tx.amount,
                success=False,
                http_status=200,
                message=f"Non-PAID status: {status} ({event_type})",
                payload=payload,
            )

    return HttpResponse(status=200)


@login_required
@db_transaction.atomic
def withdraw_funds(request):
    expects_json = request.headers.get('Content-Type', '').startswith('application/json')
    allowed_user_types = {'master_agent', 'super_agent', 'agent', 'account_user', 'finance', 'admin', 'retail_manager', 'crm'}
    if request.user.user_type not in allowed_user_types:
        if expects_json:
            return JsonResponse({'status': 'error', 'message': 'You are not authorized to withdraw funds.'}, status=403)
        messages.error(request, "You are not authorized to withdraw funds.")
        return redirect('betting:wallet')

    user = User.objects.select_for_update().get(pk=request.user.pk)
    if user.maybe_auto_unlock_withdrawal():
        user.save(update_fields=['withdrawal_locked', 'withdrawal_attempts', 'withdrawal_locked_at'])
    if user.withdrawal_locked and not user.withdrawal_locked_at:
        user.withdrawal_locked_at = timezone.now()
        user.save(update_fields=['withdrawal_locked_at'])

    if user.withdrawal_locked:
        expires_at = user.get_withdrawal_lock_expires_at()
        retry_at = expires_at.isoformat() if expires_at else None
        if expects_json:
            return JsonResponse({'status': 'locked', 'message': 'Withdrawal access has been disabled. Retry after 24 hours or contact administrator.', 'retry_at': retry_at}, status=423)
        if expires_at:
            messages.error(request, f"Withdrawal access has been disabled. Retry after {expires_at.strftime('%Y-%m-%d %H:%M')} or contact administrator.")
        else:
            messages.error(request, "Withdrawal access has been disabled. Retry after 24 hours or contact administrator.")
        return redirect('betting:wallet')

    if not user.withdrawal_pin_is_set:
        if expects_json:
            return JsonResponse({'status': 'no_pin', 'message': 'Please create your Withdrawal PIN first.', 'redirect_url': reverse('betting:profile')}, status=400)
        messages.warning(request, "Please create your Withdrawal PIN first.")
        return redirect('betting:profile')

    # Check for active loans
    has_active_loans = Loan.objects.filter(borrower=user, status='active', outstanding_balance__gt=0).exists()
    if has_active_loans:
        if expects_json:
            return JsonResponse({'status': 'error', 'message': 'You cannot withdraw funds while you have an active unpaid loan.'}, status=400)
        messages.error(request, "You cannot withdraw funds while you have an active unpaid loan.")
        return redirect('betting:wallet')

    if request.method == 'POST':
        if request.headers.get('Content-Type', '').startswith('application/json'):
            try:
                payload = json.loads(request.body or '{}')
            except Exception:
                payload = {}
            form = WithdrawFundsForm(payload, user=user)
            expects_json = True
        else:
            form = WithdrawFundsForm(request.POST, user=user)
            expects_json = False

        if not _is_withdrawal_pin_verified_recent(request):
            if expects_json:
                return JsonResponse({'status': 'error', 'message': 'Withdrawal PIN verification required.'}, status=400)
            messages.error(request, "Withdrawal PIN verification required.")
            return redirect('betting:wallet')

        if form.is_valid():
            amount = form.cleaned_data['amount']
            bank_name = form.cleaned_data['bank_name']
            account_number = form.cleaned_data['account_number']

            user_wallet = get_object_or_404(Wallet.objects.select_for_update(), user=user)

            recent_cutoff = timezone.now() - timedelta(seconds=3)
            if UserWithdrawal.objects.filter(user=user, request_time__gte=recent_cutoff).exists():
                msg = "Please wait a moment before submitting another withdrawal request."
                if expects_json:
                    return JsonResponse({'status': 'error', 'code': 'rate_limited', 'message': msg}, status=429)
                messages.error(request, msg)
                return redirect('betting:wallet')

            min_operating = Decimal('5000.00')
            max_withdrawable = user_wallet.balance - min_operating
            if max_withdrawable <= 0 or amount > max_withdrawable:
                msg = "You must retain a minimum operating balance of ₦5,000 in your wallet before making withdrawals."
                payload = {
                    'status': 'error',
                    'code': 'min_operating_balance',
                    'message': msg,
                    'min_operating_balance': float(min_operating),
                    'max_withdrawable': float(max(max_withdrawable, Decimal('0.00'))),
                }
                if expects_json:
                    return JsonResponse(payload, status=400)
                messages.error(request, msg)
                return redirect('betting:wallet')

            if user_wallet.balance < amount:
                if expects_json:
                    return JsonResponse({'status': 'error', 'message': 'Insufficient balance for withdrawal.'}, status=400)
                messages.error(request, 'Insufficient balance for withdrawal.')
                return redirect('betting:wallet')

            balance_before = user_wallet.balance
            balance_after = balance_before - amount

            withdrawal = UserWithdrawal.objects.create(
                user=user,
                amount=amount,
                bank_name=bank_name,
                account_name=form.cleaned_data['account_name'], # Corrected to use cleaned_data
                account_number=account_number,
                balance_before=balance_before,
                balance_after=balance_after,
                status='pending' # Set to pending for admin approval
            )

            tx = Transaction.objects.create(
                user=user,
                initiating_user=user,
                target_user=user,
                transaction_type='withdrawal',
                amount=amount,
                is_successful=True,
                status='completed',
                description=f"Withdrawal request {withdrawal.id} created (deducted from wallet).",
                related_withdrawal_request=withdrawal,
                timestamp=timezone.now()
            )
            user_wallet.apply_delta(
                amount=-amount,
                actor=user,
                transaction_obj=tx,
                reference=str(withdrawal.id),
                reason=tx.description,
                metadata={"withdrawal_id": withdrawal.id, "source": "withdraw_request"},
            )
            _clear_withdrawal_pin_verified(request)
            if expects_json:
                return JsonResponse({'status': 'success', 'message': 'Withdrawal request submitted successfully.'})
            messages.success(request, 'Withdrawal request submitted successfully. It will be reviewed by an admin.')
        else:
            if expects_json:
                msg = "Invalid withdrawal request."
                code = "invalid"
                details = {}
                try:
                    if form.errors:
                        for k, v in form.errors.items():
                            details[k] = [str(e) for e in v]
                except Exception:
                    details = {}
                if form.non_field_errors():
                    msg = " ".join([str(e) for e in form.non_field_errors()])
                elif details.get('amount'):
                    msg = details['amount'][0] or msg
                elif details:
                    first_key = next(iter(details.keys()))
                    if details.get(first_key):
                        msg = details[first_key][0] or msg
                return JsonResponse({'status': 'error', 'code': code, 'message': msg, 'errors': details}, status=400)
            for field, errors in form.errors.items():
                for error in errors:
                    messages.error(request, f"{field.replace('_', ' ').title()}: {error}")
            if form.non_field_errors():
                for error in form.non_field_errors():
                    messages.error(request, f"Withdrawal Error: {error}")
    return redirect('betting:wallet')


def _set_withdrawal_pin_verified(request):
    request.session['withdrawal_pin_verified_at'] = timezone.now().timestamp()


def _clear_withdrawal_pin_verified(request):
    if 'withdrawal_pin_verified_at' in request.session:
        del request.session['withdrawal_pin_verified_at']


def _is_withdrawal_pin_verified_recent(request, max_age_seconds=300):
    ts = request.session.get('withdrawal_pin_verified_at')
    if not ts:
        return False
    try:
        ts_val = float(ts)
    except (TypeError, ValueError):
        return False
    return (timezone.now().timestamp() - ts_val) <= max_age_seconds


@login_required
@db_transaction.atomic
def verify_withdrawal_pin(request):
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'Invalid request.'}, status=400)

    if request.headers.get('Content-Type', '').startswith('application/json'):
        try:
            payload = json.loads(request.body or '{}')
        except Exception:
            payload = {}
        raw_pin = (payload.get('pin') or '').strip()
    else:
        raw_pin = (request.POST.get('pin') or '').strip()

    user = User.objects.select_for_update().get(pk=request.user.pk)

    allowed_user_types = {'master_agent', 'super_agent', 'agent', 'account_user', 'finance', 'admin', 'retail_manager', 'crm'}
    if user.user_type not in allowed_user_types:
        return JsonResponse({'status': 'error', 'message': 'You are not authorized to withdraw funds.'}, status=403)

    if user.maybe_auto_unlock_withdrawal():
        user.save(update_fields=['withdrawal_locked', 'withdrawal_attempts', 'withdrawal_locked_at'])

    if user.withdrawal_locked:
        expires_at = user.get_withdrawal_lock_expires_at()
        retry_at = expires_at.isoformat() if expires_at else None
        return JsonResponse(
            {
                'status': 'locked',
                'message': 'Withdrawal access has been disabled. Retry after 24 hours or contact administrator.',
                'retry_at': retry_at
            },
            status=423
        )

    if not user.withdrawal_pin_is_set:
        return JsonResponse({'status': 'no_pin', 'message': 'Please create your Withdrawal PIN first.', 'redirect_url': reverse('betting:profile')}, status=400)

    if not raw_pin:
        return JsonResponse({'status': 'error', 'message': 'Withdrawal PIN is required.'}, status=400)

    if user.check_withdrawal_pin(raw_pin):
        WithdrawalPinVerificationLog.objects.create(
            user=user,
            success=True,
            ip_address=get_client_ip(request),
            user_agent=(request.META.get('HTTP_USER_AGENT') or '')[:1000],
        )
        user.withdrawal_attempts = 0
        user.withdrawal_locked = False
        user.withdrawal_locked_at = None
        user.save(update_fields=['withdrawal_attempts', 'withdrawal_locked', 'withdrawal_locked_at'])
        _set_withdrawal_pin_verified(request)
        return JsonResponse({'status': 'success', 'message': 'PIN verified.'})

    WithdrawalPinVerificationLog.objects.create(
        user=user,
        success=False,
        ip_address=get_client_ip(request),
        user_agent=(request.META.get('HTTP_USER_AGENT') or '')[:1000],
    )
    user.withdrawal_attempts = (user.withdrawal_attempts or 0) + 1
    remaining = max(0, 3 - int(user.withdrawal_attempts))
    if user.withdrawal_attempts >= 3:
        user.withdrawal_locked = True
        user.withdrawal_locked_at = timezone.now()
    user.save(update_fields=['withdrawal_attempts', 'withdrawal_locked', 'withdrawal_locked_at'])

    if user.withdrawal_locked:
        expires_at = user.get_withdrawal_lock_expires_at()
        retry_at = expires_at.isoformat() if expires_at else None
        return JsonResponse(
            {
                'status': 'locked',
                'message': 'Withdrawal access has been disabled. Retry after 24 hours or contact administrator.',
                'retry_at': retry_at
            },
            status=423
        )
    return JsonResponse({'status': 'error', 'message': 'Invalid withdrawal PIN.', 'attempts_remaining': remaining}, status=400)


@login_required
@db_transaction.atomic
def wallet_transfer(request):
    # Check if user has permission to manage downline wallets
    if request.user.user_type in ['master_agent', 'super_agent'] and not getattr(request.user, 'can_manage_downline_wallets', True):
        CreditLog.objects.create(
            actor=request.user,
            action_type='wallet_transfer_denied',
            amount=Decimal('0.00')
        )
        messages.error(request, "You do not have permission to credit or debit downline wallets. Please contact the administrator.")
        return redirect('betting:wallet')

    if request.user.user_type == 'super_agent' and user_has_outstanding_loan(request.user):
        CreditLog.objects.create(
            actor=request.user,
            action_type='wallet_transfer_denied',
            amount=Decimal('0.00')
        )
        messages.error(request, "Outstanding overdraft must be cleared before wallet transfers are permitted.")
        return redirect('betting:wallet')

    if request.method == 'POST':
        form = WalletTransferForm(sender_user=request.user, data=request.POST) # Pass sender_user explicitly
        if form.is_valid():
            recipient = form.cleaned_data['recipient_user_obj'] # Get recipient object from form's clean method
            amount = form.cleaned_data['amount']
            transaction_type = form.cleaned_data['transaction_type']
            description = form.cleaned_data['description']

            sender_wallet = get_object_or_404(Wallet.objects.select_for_update(), user=request.user)
            recipient_wallet = get_object_or_404(Wallet.objects.select_for_update(), user=recipient)

            if transaction_type == 'credit':
                if sender_wallet.balance < amount:
                    messages.error(request, "Insufficient balance to complete this transfer.")
                    return redirect('betting:wallet')
                transfer_description_sender = f"Sent funds to {recipient.email}: {description}"
                transfer_description_recipient = f"Received funds from {request.user.email}: {description}"
                transaction_type_sender = 'wallet_transfer_out' # Corrected type
                transaction_type_recipient = 'wallet_transfer_in' # Corrected type
            elif transaction_type == 'debit':
                if recipient_wallet.balance < amount:
                    messages.error(request, "Recipient has insufficient balance for this debit.")
                    return redirect('betting:wallet')
                transfer_description_sender = f"Received funds from {recipient.email}: {description}"
                transfer_description_recipient = f"Sent funds to {request.user.email}: {description}"
                transaction_type_sender = 'wallet_transfer_in' # From sender's perspective
                transaction_type_recipient = 'wallet_transfer_out' # From recipient's perspective

            tx_sender = Transaction.objects.create(
                user=request.user,
                initiating_user=request.user,
                target_user=recipient,
                transaction_type=transaction_type_sender,
                amount=amount,
                is_successful=True,
                status='completed',
                description=transfer_description_sender,
                timestamp=timezone.now()
            )
            tx_recipient = Transaction.objects.create(
                user=recipient,
                initiating_user=request.user, # The one who initiated it
                target_user=recipient,
                transaction_type=transaction_type_recipient,
                amount=amount,
                is_successful=True,
                status='completed',
                description=transfer_description_recipient,
                timestamp=timezone.now()
            )
            if transaction_type == 'credit':
                sender_wallet.apply_delta(
                    amount=-amount,
                    actor=request.user,
                    transaction_obj=tx_sender,
                    reference=str(tx_sender.id),
                    reason=tx_sender.description,
                    metadata={"transfer": "out", "counterparty": recipient.id},
                )
                recipient_wallet.apply_delta(
                    amount=amount,
                    actor=request.user,
                    transaction_obj=tx_recipient,
                    reference=str(tx_sender.id),
                    reason=tx_recipient.description,
                    metadata={"transfer": "in", "counterparty": request.user.id},
                )
            elif transaction_type == 'debit':
                sender_wallet.apply_delta(
                    amount=amount,
                    actor=request.user,
                    transaction_obj=tx_sender,
                    reference=str(tx_sender.id),
                    reason=tx_sender.description,
                    metadata={"transfer": "in", "counterparty": recipient.id},
                )
                recipient_wallet.apply_delta(
                    amount=-amount,
                    actor=request.user,
                    transaction_obj=tx_recipient,
                    reference=str(tx_sender.id),
                    reason=tx_recipient.description,
                    metadata={"transfer": "out", "counterparty": request.user.id},
                )
            messages.success(request, f'Successfully completed transfer of ₦{amount:.2f} to/from {recipient.email}.')
        else:
            for field, errors in form.errors.items():
                for error in errors:
                    messages.error(request, f"{field.replace('_', ' ').title()}: {error}")
            if form.non_field_errors():
                for error in form.non_field_errors():
                    messages.error(request, f"Transfer Error: {error}")
    return redirect('betting:wallet')

@login_required
def submit_credit_request(request):
    if request.method == 'POST':
        form = CreditRequestForm(request.POST, user=request.user)
        if form.is_valid():
            pending_exists = CreditRequest.objects.filter(requester=request.user, status='pending').exists()
            if pending_exists:
                 messages.warning(request, "You already have a pending credit request.")
                 return redirect('betting:wallet')

            credit_request = form.save(commit=False)
            credit_request.requester = request.user
            credit_request.status = 'pending'
            credit_request.save()
            
            CreditLog.objects.create(
                actor=request.user,
                target_user=credit_request.recipient,
                action_type='request_created',
                amount=credit_request.amount,
                status='pending',
                reference_id=str(credit_request.id)
            )

            messages.success(request, "Credit request submitted successfully.")
            return redirect('betting:wallet')
        else:
             for field, errors in form.errors.items():
                for error in errors:
                    messages.error(request, f"{field}: {error}")
    return redirect('betting:wallet')


@login_required
@require_POST
def submit_overdraft_request_view(request):
    if request.user.user_type not in ['agent', 'super_agent']:
        messages.error(request, "Only agents and super agents can request overdraft.")
        return redirect('betting:wallet')

    form = OverdraftRequestForm(request.POST, user=request.user)
    if not form.is_valid():
        for field, errors in form.errors.items():
            for error in errors:
                messages.error(request, f"{field}: {error}")
        return redirect('betting:wallet')

    try:
        submit_overdraft_request(
            borrower=request.user,
            requested_amount=form.cleaned_data['requested_amount'],
            reason=form.cleaned_data.get('reason') or '',
            ip_address=get_client_ip(request),
        )
        messages.success(request, "Overdraft request submitted successfully.")
    except LoanOverdraftError as exc:
        messages.error(request, str(exc))
    return redirect('betting:wallet')


@login_required
@require_POST
def process_overdraft_request_view(request, loan_id):
    action = (request.POST.get('action') or '').strip().lower()
    reason = (request.POST.get('reason') or '').strip()
    redirect_to = (request.POST.get('return_to') or '').strip()

    try:
        if action == 'approve':
            approve_loan_request(actor=request.user, loan_id=loan_id, ip_address=get_client_ip(request))
            messages.success(request, "Overdraft request approved successfully.")
        elif action == 'reject':
            reject_loan_request(actor=request.user, loan_id=loan_id, reason=reason, ip_address=get_client_ip(request))
            messages.success(request, "Overdraft request rejected.")
        else:
            messages.error(request, "Invalid overdraft request action.")
    except LoanOverdraftError as exc:
        messages.error(request, str(exc))

    if redirect_to:
        return redirect(redirect_to)
    if request.user.user_type == 'super_agent':
        return redirect('betting:super_agent_dashboard')
    return redirect('betting:wallet')

@login_required
def manage_credit_requests(request):
    received_filter = Q(recipient=request.user)
    if request.user.is_superuser or request.user.user_type == 'admin':
        received_filter |= Q(request_type__in=CRM_WALLET_APPROVAL_REQUEST_TYPES)
    elif request.user.user_type == 'account_user':
        received_filter |= Q(request_type__in=CRM_WALLET_APPROVAL_REQUEST_TYPES, recipient__user_type='account_user')
    received_requests = CreditRequest.objects.filter(received_filter).select_related('requester', 'recipient').distinct().order_by('-created_at')
    sent_requests = CreditRequest.objects.filter(requester=request.user).order_by('-created_at')
    
    return render(request, 'betting/manage_credit_requests.html', {
        'received_requests': received_requests,
        'sent_requests': sent_requests
    })

@login_required
@db_transaction.atomic
def approve_credit_request(request, request_id):
    credit_req = get_object_or_404(CreditRequest.objects.select_related('requester', 'recipient'), id=request_id)
    can_process = credit_req.recipient_id == request.user.id
    if credit_req.request_type in CRM_WALLET_APPROVAL_REQUEST_TYPES:
        can_process = can_process or request.user.is_superuser or request.user.user_type in ['admin', 'account_user']
    if not can_process:
        raise Http404()
    
    if request.method == 'POST':
        action = request.POST.get('action')
        selected_account_user = None
        selected_account_user_id = (request.POST.get('account_user_wallet_user_id') or '').strip()
        if selected_account_user_id:
            selected_account_user = User.objects.filter(
                id=selected_account_user_id,
                is_active=True,
                user_type='account_user',
            ).first()
        try:
            message_text, message_level = process_credit_request_decision(
                actor=request.user,
                credit_req=credit_req,
                action=action,
                account_user_wallet_user=selected_account_user,
            )
            messages.add_message(request, message_level, message_text)
        except CreditRequestProcessError as exc:
            messages.error(request, str(exc))
            
    return redirect('betting:manage_credit_requests')

@login_required
@db_transaction.atomic
def settle_loan(request, loan_id):
    loan = get_object_or_404(Loan, id=loan_id, borrower=request.user)
    
    if request.method == 'POST':
        form = LoanSettlementForm(request.POST)
        if form.is_valid():
            method = form.cleaned_data['settlement_method']
            
            if method == 'wallet':
                borrower_wallet = Wallet.objects.select_for_update().get(user=request.user)
                lender_wallet = Wallet.objects.select_for_update().get(user=loan.lender)
                
                amount_to_pay = loan.outstanding_balance
                
                if borrower_wallet.balance < amount_to_pay:
                    messages.error(request, "Insufficient wallet balance.")
                    return redirect('betting:wallet')
                
                loan.outstanding_balance = Decimal('0.00')
                loan.status = 'settled'
                loan.save()
                
                tx_out = Transaction.objects.create(
                    user=request.user,
                    transaction_type='wallet_transfer_out',
                    amount=amount_to_pay,
                    status='completed',
                    is_successful=True,
                    target_user=loan.lender,
                    description=f"Loan repayment to {loan.lender.email}"
                )
                
                tx_in = Transaction.objects.create(
                    user=loan.lender,
                    transaction_type='wallet_transfer_in',
                    amount=amount_to_pay,
                    status='completed',
                    is_successful=True,
                    initiating_user=request.user,
                    description=f"Loan repayment received from {request.user.email}"
                )
                borrower_wallet.apply_delta(
                    amount=-amount_to_pay,
                    actor=request.user,
                    transaction_obj=tx_out,
                    reference=str(loan.id),
                    reason=tx_out.description,
                    metadata={"loan_id": loan.id, "settlement": "wallet"},
                )
                lender_wallet.apply_delta(
                    amount=amount_to_pay,
                    actor=request.user,
                    transaction_obj=tx_in,
                    reference=str(loan.id),
                    reason=tx_in.description,
                    metadata={"loan_id": loan.id, "settlement": "wallet"},
                )
                
                CreditLog.objects.create(
                    actor=request.user,
                    target_user=loan.lender,
                    action_type='loan_settled_wallet',
                    amount=amount_to_pay,
                    status='settled',
                    reference_id=str(loan.id)
                )
                
                messages.success(request, "Loan settled successfully via wallet.")
                
            elif method == 'deposit':
                messages.info(request, "Please deposit funds to your wallet to settle the loan.")
                return redirect('betting:wallet')
                
    return redirect('betting:wallet')


# --- User Profile Views ---

@login_required
def profile_view(request):
    if request.user.maybe_auto_unlock_withdrawal():
        request.user.save(update_fields=['withdrawal_locked', 'withdrawal_attempts', 'withdrawal_locked_at'])
    if request.user.withdrawal_locked and not request.user.withdrawal_locked_at:
        request.user.withdrawal_locked_at = timezone.now()
        request.user.save(update_fields=['withdrawal_locked_at'])
    user_wallet = get_object_or_404(Wallet, user=request.user)
    transactions = Transaction.objects.filter(user=request.user).order_by('-timestamp')[:10] # Last 10 transactions

    # Calculate total deposits and withdrawals in the view
    total_deposits = Transaction.objects.filter(
        user=request.user, 
        transaction_type='deposit', 
        is_successful=True
    ).aggregate(Sum('amount'))['amount__sum'] or Decimal('0.00')

    total_withdrawals = UserWithdrawal.objects.filter(
        user=request.user, 
        status='approved'
    ).aggregate(Sum('amount'))['amount__sum'] or Decimal('0.00')

    site_config = None
    try:
        site_config = SiteConfiguration.load()
    except Exception:
        site_config = None
    global_cashier_voiding_enabled = bool(getattr(site_config, "enable_global_cashier_voiding", False))

    cashier_void_permission_form = None
    agent_min_stake_form = None
    if request.user.user_type == "agent":
        if request.method == "POST" and "cashier_void_permissions_submit" in request.POST:
            cashier_void_permission_form = CashierVoidPermissionForm(request.POST, agent=request.user)
        else:
            cashier_void_permission_form = CashierVoidPermissionForm(agent=request.user)
            try:
                CashierVoidPermission = apps.get_model("void_requests", "CashierVoidPermission")
                allowed_ids = list(
                    CashierVoidPermission.objects.filter(agent=request.user, can_request_void=True).values_list("cashier_id", flat=True)
                )
                cashier_void_permission_form.initial = {"cashiers": allowed_ids}
            except Exception:
                pass
        try:
            override = AgentBettingLimitOverride.objects.filter(agent=request.user, is_active=True, custom_limits_enabled=True).first()
        except Exception:
            override = None
        if request.method == "POST" and "agent_min_stake_submit" in request.POST:
            agent_min_stake_form = AgentMinStakeOverrideForm(request.POST)
        else:
            agent_min_stake_form = AgentMinStakeOverrideForm(
                initial={"min_stake": getattr(override, "min_stake", None)}
            )


    if request.method == 'POST':
        profile_form = ProfileEditForm(request.POST, instance=request.user, request=request)
        password_form = PasswordChangeForm(request.user, request.POST)
        pin_create_form = WithdrawalPinCreateForm(request.POST)
        pin_reset_form = WithdrawalPinResetForm(request.POST, user=request.user)
        
        if 'profile_submit' in request.POST:
            if profile_form.is_valid():
                profile_form.save()
                messages.success(request, 'Your profile has been updated successfully.')
                return redirect('betting:profile')
            else:
                for field, errors in profile_form.errors.items():
                    for error in errors:
                        messages.error(request, f"Profile - {field.replace('_', ' ').title()}: {error}")
                if profile_form.non_field_errors():
                    for error in profile_form.non_field_errors():
                        messages.error(request, f"Profile Error: {error}")
        
        elif 'password_submit' in request.POST:
            if password_form.is_valid():
                password_form.save()
                logout_user_from_all_active_sessions(request.user)
                logout(request)
                messages.success(request, 'Your password has been changed successfully. Please log in again.')
                return redirect('betting:login') # Redirect to login after password change
            else:
                for field, errors in password_form.errors.items():
                    for error in errors:
                        messages.error(request, f"Password - {field.replace('_', ' ').title()}: {error}")
                if password_form.non_field_errors():
                    for error in password_form.non_field_errors():
                        messages.error(request, f"Password Error: {error}")
        elif 'withdrawal_pin_create_submit' in request.POST:
            if request.user.withdrawal_pin_is_set:
                messages.warning(request, "Withdrawal PIN is already set. Use Reset Withdrawal PIN instead.")
                return redirect('betting:profile')
            if pin_create_form.is_valid():
                request.user.set_withdrawal_pin(pin_create_form.cleaned_data['pin'])
                request.user.withdrawal_attempts = 0
                request.user.withdrawal_locked = False
                request.user.withdrawal_locked_at = None
                request.user.save(update_fields=['withdrawal_pin', 'withdrawal_attempts', 'withdrawal_locked', 'withdrawal_locked_at'])
                messages.success(request, "Withdrawal PIN created successfully.")
                return redirect('betting:profile')
            else:
                for error in pin_create_form.non_field_errors():
                    messages.error(request, error)
        elif 'withdrawal_pin_reset_submit' in request.POST:
            if pin_reset_form.is_valid():
                request.user.set_withdrawal_pin(pin_reset_form.cleaned_data['new_pin'])
                request.user.withdrawal_attempts = 0
                request.user.withdrawal_locked = False
                request.user.withdrawal_locked_at = None
                request.user.save(update_fields=['withdrawal_pin', 'withdrawal_attempts', 'withdrawal_locked', 'withdrawal_locked_at'])
                messages.success(request, "Withdrawal PIN updated successfully.")
                return redirect('betting:profile')
            else:
                for error in pin_reset_form.non_field_errors():
                    messages.error(request, error)
        elif "cashier_void_permissions_submit" in request.POST:
            if request.user.user_type != "agent":
                return HttpResponseForbidden("Not allowed.")
            if not cashier_void_permission_form:
                cashier_void_permission_form = CashierVoidPermissionForm(request.POST, agent=request.user)
            if cashier_void_permission_form.is_valid():
                selected_cashiers = set(cashier_void_permission_form.cleaned_data.get("cashiers").values_list("id", flat=True))
                scoped_cashiers = list(cashier_void_permission_form.fields["cashiers"].queryset.values_list("id", flat=True))

                try:
                    CashierVoidPermission = apps.get_model("void_requests", "CashierVoidPermission")
                    existing = {
                        row["cashier_id"]: row["can_request_void"]
                        for row in CashierVoidPermission.objects.filter(agent=request.user, cashier_id__in=scoped_cashiers).values(
                            "cashier_id", "can_request_void"
                        )
                    }
                    to_create = []
                    to_update_ids_true = []
                    to_update_ids_false = []
                    now_selected = selected_cashiers

                    for cid in scoped_cashiers:
                        desired = cid in now_selected
                        if cid not in existing:
                            to_create.append(CashierVoidPermission(agent=request.user, cashier_id=cid, can_request_void=desired))
                        elif bool(existing[cid]) != desired:
                            if desired:
                                to_update_ids_true.append(cid)
                            else:
                                to_update_ids_false.append(cid)

                    with db_transaction.atomic():
                        if to_create:
                            CashierVoidPermission.objects.bulk_create(to_create, ignore_conflicts=True)
                        if to_update_ids_true:
                            CashierVoidPermission.objects.filter(agent=request.user, cashier_id__in=to_update_ids_true).update(
                                can_request_void=True
                            )
                        if to_update_ids_false:
                            CashierVoidPermission.objects.filter(agent=request.user, cashier_id__in=to_update_ids_false).update(
                                can_request_void=False
                            )

                    if global_cashier_voiding_enabled:
                        messages.warning(request, "Global cashier voiding is enabled. Agent-level settings are currently ignored.")
                    messages.success(request, "Cashier void permissions updated.")
                    return redirect("betting:profile")
                except Exception:
                    messages.error(request, "Unable to update cashier void permissions.")
            else:
                for error in cashier_void_permission_form.non_field_errors():
                    messages.error(request, error)
        elif "agent_min_stake_submit" in request.POST:
            if request.user.user_type != "agent":
                return HttpResponseForbidden("Not allowed.")
            if not agent_min_stake_form:
                agent_min_stake_form = AgentMinStakeOverrideForm(request.POST)
            if agent_min_stake_form.is_valid():
                min_stake = agent_min_stake_form.cleaned_data.get("min_stake")
                with db_transaction.atomic():
                    override, created = AgentBettingLimitOverride.objects.select_for_update().get_or_create(
                        agent=request.user,
                        defaults={
                            "is_active": True,
                            "custom_limits_enabled": True,
                            "created_by": request.user,
                            "updated_by": request.user,
                        },
                    )
                    override.is_active = True
                    override.custom_limits_enabled = True
                    override.min_stake = min_stake
                    if created and not override.created_by_id:
                        override.created_by = request.user
                    override.updated_by = request.user
                    override.save()

                messages.success(request, "Minimum stake override updated.")
                return redirect("betting:profile")
            else:
                for error in agent_min_stake_form.non_field_errors():
                    messages.error(request, error)
    else:
        profile_form = ProfileEditForm(instance=request.user, request=request)
        password_form = PasswordChangeForm(request.user)
        pin_create_form = WithdrawalPinCreateForm()
        pin_reset_form = WithdrawalPinResetForm(user=request.user)

    context = {
        'profile_form': profile_form,
        'password_form': password_form,
        'pin_create_form': pin_create_form,
        'pin_reset_form': pin_reset_form,
        'withdrawal_lock_expires_at': request.user.get_withdrawal_lock_expires_at(),
        'wallet': user_wallet,
        'transactions': transactions,
        'total_deposits': total_deposits,       # Add to context
        'total_withdrawals': total_withdrawals, # Add to context
        'global_cashier_voiding_enabled': global_cashier_voiding_enabled,
        'cashier_void_permission_form': cashier_void_permission_form,
        'agent_min_stake_form': agent_min_stake_form,
    }
    return render(request, 'betting/profile.html', context)


@login_required
def change_password(request):
    if request.method == 'POST':
        form = PasswordChangeForm(request.user, request.POST)
        if form.is_valid():
            form.save()
            logout_user_from_all_active_sessions(request.user)
            logout(request)
            messages.success(request, 'Your password was successfully updated!')
            return redirect('betting:login') # Log out user after password change for security
        else:
            for field, errors in form.errors.items():
                for error in errors:
                    messages.error(request, f"{field.replace('_', ' ').title()}: {error}")
            if form.non_field_errors():
                for error in form.non_field_errors():
                    messages.error(request, f"Password Change Error: {error}")
    else:
        form = PasswordChangeForm(request.user)
    return render(request, 'betting/change_password.html', {'form': form})


@login_required
def user_dashboard(request):
    """
    Unified dashboard entry point. Redirects special roles to their dashboards,
    and renders the standard user dashboard for players.
    """
    user = request.user
    
    # Redirect special users to their specific dashboards
    if user.is_superuser or user.user_type == 'admin':
        return redirect('betting:admin_dashboard')
    elif user.user_type == 'master_agent':
        return redirect('betting:master_agent_dashboard')
    elif user.user_type == 'super_agent':
        return redirect('betting:super_agent_dashboard')
    elif user.user_type == 'account_user':
        return redirect('betting:account_user_dashboard')
    elif user.user_type == 'crm':
        return redirect('betting:crm_dashboard')
    elif user.user_type == 'retail_manager':
        return redirect('betting:retail_dashboard')
    elif user.user_type == 'finance':
        return redirect('betting:finance_dashboard')
    elif user.user_type == 'agent' or user.user_type == 'cashier':
        return redirect('betting:agent_dashboard')
        
    # Standard User / Player Dashboard Logic
    recent_tickets = BetTicket.objects.filter(user=user).order_by('-placed_at')[:10]
    active_bets_count = BetTicket.objects.filter(user=user, status='pending').count()
    
    # Calculate Total Winnings (only WON tickets)
    total_winnings = BetTicket.objects.filter(user=user, status='won').aggregate(Sum('max_winning'))['max_winning__sum'] or Decimal('0.00')

    context = {
        'recent_tickets': recent_tickets,
        'active_bets_count': active_bets_count,
        'total_winnings': total_winnings,
    }
    return render(request, 'betting/user_dashboard.html', context)


# --- Agent/Super Agent/Master Agent specific Views ---

@login_required
@user_passes_test(lambda u: u.user_type in ['agent', 'super_agent', 'master_agent', 'cashier'])
def agent_dashboard(request):
    user = request.user
    today = timezone.now().date()
    days_since_monday = today.weekday()
    if days_since_monday == 0:
        days_since_monday = 7
    last_monday = today - timedelta(days=days_since_monday)
    start_of_week = last_monday - timedelta(days=6)

    first_day_this_month = today.replace(day=1)
    last_day_last_month = first_day_this_month - timedelta(days=1)
    start_of_month = last_day_last_month.replace(day=1)
    start_date_str = request.GET.get('start_date') or ''
    end_date_str = request.GET.get('end_date') or ''
    locked_q = (request.GET.get('locked_q') or '').strip()
    locked_user_type = (request.GET.get('locked_user_type') or '').strip()
    locked_status = (request.GET.get('locked_status') or '').strip()
    locked_by = (request.GET.get('locked_by') or '').strip()
    locked_start_date = (request.GET.get('locked_start_date') or '').strip()
    locked_end_date = (request.GET.get('locked_end_date') or '').strip()
    locked_appeal_start_date = (request.GET.get('locked_appeal_start_date') or '').strip()
    locked_appeal_end_date = (request.GET.get('locked_appeal_end_date') or '').strip()
    dormant_q = (request.GET.get('dormant_q') or '').strip()
    dormant_bucket = (request.GET.get('dormant_bucket') or 'login_7').strip() or 'login_7'
    dormant_agent = (request.GET.get('dormant_agent') or '').strip()
    dormant_super_agent = (request.GET.get('dormant_super_agent') or '').strip()
    dormant_status = (request.GET.get('dormant_status') or '').strip()
    complaint_q = (request.GET.get('complaint_q') or '').strip()
    complaint_type = (request.GET.get('complaint_type') or '').strip()
    complaint_status = (request.GET.get('complaint_status') or '').strip()
    complaint_priority = (request.GET.get('complaint_priority') or '').strip()
    performance_entity = (request.GET.get('performance_entity') or 'super_agent').strip() or 'super_agent'
    performance_q = (request.GET.get('performance_q') or '').strip()
    start_date = None
    end_date = None
    try:
        if start_date_str:
            start_date = date.fromisoformat(start_date_str)
    except Exception:
        start_date = None
    try:
        if end_date_str:
            end_date = date.fromisoformat(end_date_str)
    except Exception:
        end_date = None
    if start_date and end_date and start_date > end_date:
        start_date, end_date = end_date, start_date

    def _parse_dashboard_bound(value, *, end=False):
        value = (value or '').strip()
        if not value:
            return None
        try:
            parsed = date.fromisoformat(value)
            return timezone.make_aware(datetime.combine(parsed, datetime.max.time() if end else datetime.min.time()))
        except Exception:
            return None

    locked_start_dt = _parse_dashboard_bound(locked_start_date, end=False)
    locked_end_dt = _parse_dashboard_bound(locked_end_date, end=True)
    locked_appeal_start_dt = _parse_dashboard_bound(locked_appeal_start_date, end=False)
    locked_appeal_end_dt = _parse_dashboard_bound(locked_appeal_end_date, end=True)

    direct_downline_rows = []
    master_downline_tree = []
    super_downline_tree = []
    direct_super_agents_qs = User.objects.none()
    direct_agents_qs = User.objects.none()
    if user.user_type == 'master_agent':
        direct_super_agents_qs = (
            User.objects.filter(user_type='super_agent', master_agent=user)
            .select_related('state')
            .order_by('email')
        )
        agents_qs = (
            User.objects.filter(user_type='agent', super_agent__in=direct_super_agents_qs)
            .select_related('state', 'super_agent')
            .order_by('email')
        )
        cashiers_qs = (
            User.objects.filter(user_type='cashier', agent__in=agents_qs)
            .select_related('state', 'agent')
            .order_by('email')
        )

        wallet_map = _get_wallet_balance_map(
            list(direct_super_agents_qs.values_list('id', flat=True))
            + list(agents_qs.values_list('id', flat=True))
            + list(cashiers_qs.values_list('id', flat=True))
        )

        cashiers_by_agent = {}
        for cashier in cashiers_qs:
            cashiers_by_agent.setdefault(cashier.agent_id, []).append(cashier)

        agent_totals = {}
        for ag in agents_qs:
            total = wallet_map.get(ag.id) or Decimal('0.00')
            for cashier in cashiers_by_agent.get(ag.id, []):
                total += wallet_map.get(cashier.id) or Decimal('0.00')
            agent_totals[ag.id] = total

        agents_by_sa = {}
        for ag in agents_qs:
            agents_by_sa.setdefault(ag.super_agent_id, []).append(ag)

        for sa in direct_super_agents_qs:
            sa_agents = agents_by_sa.get(sa.id, [])
            sa_total = wallet_map.get(sa.id) or Decimal('0.00')
            agent_rows = []
            for ag in sa_agents:
                cashier_rows = []
                for cashier in cashiers_by_agent.get(ag.id, []):
                    cashier_rows.append({
                        'user': cashier,
                        'balance': wallet_map.get(cashier.id) or Decimal('0.00'),
                    })
                agent_rows.append({
                    'user': ag,
                    'total_balance': agent_totals.get(ag.id) or Decimal('0.00'),
                    'cashiers': cashier_rows,
                })
                sa_total += agent_totals.get(ag.id) or Decimal('0.00')

            master_downline_tree.append({
                'user': sa,
                'total_balance': sa_total,
                'agents': agent_rows,
            })

    elif user.user_type == 'super_agent':
        direct_agents_qs = (
            User.objects.filter(user_type='agent', super_agent=user)
            .select_related('state')
            .order_by('email')
        )
        cashiers_qs = (
            User.objects.filter(user_type='cashier', agent__in=direct_agents_qs)
            .select_related('state', 'agent')
            .order_by('email')
        )

        wallet_map = _get_wallet_balance_map(
            list(direct_agents_qs.values_list('id', flat=True))
            + list(cashiers_qs.values_list('id', flat=True))
        )

        cashiers_by_agent = {}
        for cashier in cashiers_qs:
            cashiers_by_agent.setdefault(cashier.agent_id, []).append(cashier)

        for ag in direct_agents_qs:
            total = wallet_map.get(ag.id) or Decimal('0.00')
            cashier_rows = []
            for cashier in cashiers_by_agent.get(ag.id, []):
                bal = wallet_map.get(cashier.id) or Decimal('0.00')
                total += bal
                cashier_rows.append({'user': cashier, 'balance': bal})

            super_downline_tree.append({
                'user': ag,
                'total_balance': total,
                'cashiers': cashier_rows,
            })

    elif user.user_type == 'agent':
        direct_cashiers_qs = User.objects.filter(user_type='cashier', agent=user).select_related('state')
        for ca in direct_cashiers_qs:
            direct_downline_rows.append({'user': ca, 'aggregated_balance': getattr(getattr(ca, 'wallet', None), 'balance', Decimal('0.00'))})
        direct_downline_rows.sort(key=lambda r: (r['user'].email or '').lower())

    # Get downline users
    if user.user_type == 'master_agent':
        downline_users_qs = User.objects.filter(
            Q(master_agent=user) |
            Q(super_agent__master_agent=user) |
            Q(agent__super_agent__master_agent=user)
        )
    elif user.user_type == 'super_agent':
        downline_users_qs = User.objects.filter(
            Q(super_agent=user) |
            Q(agent__super_agent=user)
        )
    elif user.user_type == 'agent':
        downline_users_qs = User.objects.filter(agent=user)
    elif user.user_type == 'cashier':
        # Cashiers see their own tickets, but have no downline users
        downline_users_qs = User.objects.none()
    else:
        downline_users_qs = User.objects.none()

    total_downline_users = downline_users_qs.count()

    # Calculate GGR for downline (sum of GGR from their bet tickets)
    # GGR is Turnover - Winnings
    # We need to consider all bet tickets placed by downline users
    if user.user_type == 'cashier':
        downline_bet_tickets = BetTicket.objects.filter(user=user).exclude(status__in=['deleted', 'cancelled'])
    else:
        downline_bet_tickets = BetTicket.objects.filter(
            user__in=downline_users_qs
        ).exclude(status__in=['deleted', 'cancelled'])

    scope = request.GET.get('scope', 'all')
    if start_date and end_date:
        metrics_start = start_date
        metrics_end = end_date
        downline_bet_tickets = downline_bet_tickets.filter(
            placed_at__date__gte=metrics_start,
            placed_at__date__lte=metrics_end,
        )
        metrics_label = 'Custom'
        scope = 'custom'
    elif scope == 'week':
        try:
            from commission.models import CommissionPeriod
        except Exception:
            CommissionPeriod = None

        period = None
        if CommissionPeriod is not None:
            period = (
                CommissionPeriod.objects.filter(
                    period_type='weekly',
                    start_date=start_of_week,
                    end_date=last_monday,
                )
                .order_by('-start_date')
                .first()
            )

        metrics_start = period.start_date if period else start_of_week
        metrics_end = period.end_date if period else last_monday
        downline_bet_tickets = downline_bet_tickets.filter(
            placed_at__date__gte=metrics_start,
            placed_at__date__lte=metrics_end,
        )
        metrics_label = 'Weekly'
    elif scope == 'month':
        try:
            from commission.models import CommissionPeriod
        except Exception:
            CommissionPeriod = None

        period = None
        if CommissionPeriod is not None:
            period = (
                CommissionPeriod.objects.filter(
                    period_type='monthly',
                    start_date=start_of_month,
                    end_date=last_day_last_month,
                )
                .order_by('-start_date')
                .first()
            )

        metrics_start = period.start_date if period else start_of_month
        metrics_end = period.end_date if period else last_day_last_month
        downline_bet_tickets = downline_bet_tickets.filter(
            placed_at__date__gte=metrics_start,
            placed_at__date__lte=metrics_end,
        )
        metrics_label = 'Monthly'
    else:
        scope = 'all'
        metrics_label = 'All Time'
        metrics_start = None
        metrics_end = None

    # Aggregate total turnover and winnings for downline
    total_downline_turnover = downline_bet_tickets.aggregate(Sum('stake_amount'))['stake_amount__sum'] or Decimal('0.00')
    total_downline_winnings = downline_bet_tickets.filter(status='won').aggregate(Sum('max_winning'))['max_winning__sum'] or Decimal('0.00')
    
    downline_ggr = total_downline_turnover - total_downline_winnings

    # Commission calculations
    total_commission_paid = Decimal('0.00')
    pending_commission = Decimal('0.00')
    pending_commission_single = Decimal('0.00')
    pending_commission_multiple = Decimal('0.00')

    if user.user_type == 'agent':
        weekly_comms = WeeklyAgentCommission.objects.filter(agent=user).select_related('period')
        if metrics_start and metrics_end:
            weekly_comms = weekly_comms.filter(period__end_date__gte=metrics_start, period__start_date__lte=metrics_end)
        total_commission_paid = weekly_comms.aggregate(Sum('amount_paid'))['amount_paid__sum'] or Decimal('0.00')
        pending_total = weekly_comms.filter(status__in=['pending', 'approved', 'partially_paid']).aggregate(Sum('commission_total_amount'))['commission_total_amount__sum'] or Decimal('0.00')
        pending_paid = weekly_comms.filter(status__in=['pending', 'approved', 'partially_paid']).aggregate(Sum('amount_paid'))['amount_paid__sum'] or Decimal('0.00')
        pending_commission = max(Decimal('0.00'), pending_total - pending_paid)

        pending_rows = list(
            weekly_comms.filter(status__in=['pending', 'approved', 'partially_paid']).values(
                'commission_single_amount',
                'commission_multiple_amount',
                'commission_total_amount',
                'amount_paid',
            )
        )
        for r in pending_rows:
            single_amt = r.get('commission_single_amount') or Decimal('0.00')
            multiple_amt = r.get('commission_multiple_amount') or Decimal('0.00')
            total_amt = r.get('commission_total_amount') or (single_amt + multiple_amt) or Decimal('0.00')
            paid_amt = r.get('amount_paid') or Decimal('0.00')
            if total_amt > 0 and paid_amt > 0:
                single_paid_share = (paid_amt * (single_amt / total_amt))
                multiple_paid_share = (paid_amt * (multiple_amt / total_amt))
            else:
                single_paid_share = Decimal('0.00')
                multiple_paid_share = Decimal('0.00')

            pending_commission_single += max(Decimal('0.00'), single_amt - single_paid_share)
            pending_commission_multiple += max(Decimal('0.00'), multiple_amt - multiple_paid_share)
        if metrics_start and metrics_end and not weekly_comms.exists():
            try:
                from commission.services import calculate_weekly_agent_commission_data
            except Exception:
                calculate_weekly_agent_commission_data = None
            if calculate_weekly_agent_commission_data is not None:
                try:
                    from commission.models import CommissionPeriod as CommissionPeriodModel
                except Exception:
                    CommissionPeriodModel = None

                period_for_calc = None
                if CommissionPeriodModel is not None:
                    period_for_calc = CommissionPeriodModel.objects.filter(
                        period_type='weekly',
                        start_date=metrics_start,
                        end_date=metrics_end,
                    ).first()
                if period_for_calc is None:
                    class CommissionPeriodStub:
                        pass
                    period_for_calc = CommissionPeriodStub()
                    period_for_calc.start_date = metrics_start
                    period_for_calc.end_date = metrics_end
                calc = calculate_weekly_agent_commission_data(user, period_for_calc, include_breakdown=True) or {}
                pending_commission = (calc.get("commission_total_amount") or Decimal("0.00"))
                pending_commission_single = (calc.get("commission_single_amount") or Decimal("0.00"))
                pending_commission_multiple = (calc.get("commission_multiple_amount") or Decimal("0.00"))
    elif user.user_type in ['super_agent', 'master_agent']:
        monthly_comms = MonthlyNetworkCommission.objects.filter(user=user).select_related('period')
        if metrics_start and metrics_end:
            monthly_comms = monthly_comms.filter(period__end_date__gte=metrics_start, period__start_date__lte=metrics_end)
        total_commission_paid = monthly_comms.aggregate(Sum('amount_paid'))['amount_paid__sum'] or Decimal('0.00')
        pending_total = monthly_comms.filter(status__in=['pending', 'approved', 'partially_paid']).aggregate(Sum('commission_amount'))['commission_amount__sum'] or Decimal('0.00')
        pending_paid = monthly_comms.filter(status__in=['pending', 'approved', 'partially_paid']).aggregate(Sum('amount_paid'))['amount_paid__sum'] or Decimal('0.00')
        pending_commission = max(Decimal('0.00'), pending_total - pending_paid)

    # Top performing users/agents (example: based on GGR)
    # CORRECTED: Changed 'betticket__' to 'bet_tickets__'
    tickets_date_filter = None
    if metrics_start and metrics_end:
        tickets_date_filter = Q(bet_tickets__placed_at__date__gte=metrics_start, bet_tickets__placed_at__date__lte=metrics_end)

    stake_case = Case(
        When(bet_tickets__status__in=['won', 'lost', 'pending', 'cashed_out', 'cancelled'], then=F('bet_tickets__stake_amount')),
        default=Value(0),
        output_field=DecimalField()
    )
    win_case = Case(
        When(bet_tickets__status='won', then=F('bet_tickets__potential_winning')),
        default=Value(0),
        output_field=DecimalField()
    )

    if tickets_date_filter is not None:
        top_performers = downline_users_qs.annotate(
            user_total_stake=Sum(stake_case, filter=tickets_date_filter),
            user_total_winnings=Sum(win_case, filter=tickets_date_filter),
        ).annotate(
            user_ggr=F('user_total_stake') - F('user_total_winnings')
        ).order_by('-user_ggr')[:5]
    else:
        top_performers = downline_users_qs.annotate(
            user_total_stake=Sum(stake_case),
            user_total_winnings=Sum(win_case),
        ).annotate(
            user_ggr=F('user_total_stake') - F('user_total_winnings')
        ).order_by('-user_ggr')[:5] # Top 5 based on GGR

    # Recent activity from downline users
    recent_downline_transactions = Transaction.objects.filter(
        Q(user__in=downline_users_qs) | Q(initiating_user__in=downline_users_qs)
    )
    if metrics_start and metrics_end:
        recent_downline_transactions = recent_downline_transactions.filter(
            timestamp__date__gte=metrics_start,
            timestamp__date__lte=metrics_end,
        )
    recent_downline_transactions = recent_downline_transactions.order_by('-timestamp')[:10]

    sort_by = request.GET.get('sort_by') or 'placed_at'
    sort_dir = request.GET.get('sort_dir') or 'desc'
    sort_map = {
        'placed_at': 'placed_at',
        'stake': 'stake_amount',
        'potential': 'potential_winning',
        'max': 'max_winning',
        'status': 'status',
        'user': 'user__email',
    }
    sort_field = sort_map.get(sort_by, 'placed_at')
    order_expr = f"-{sort_field}" if sort_dir == 'desc' else sort_field

    locked_accounts_summary = {'locked_downlines': 0, 'pending_appeals': 0}
    locked_accounts_page = None
    locked_accounts_rows = []
    if user.user_type == 'super_agent':
        locked_accounts_summary = {
            'locked_downlines': _scoped_locked_accounts_queryset(user).count(),
            'pending_appeals': _scoped_account_unlock_appeals_queryset(user).filter(status='pending').count(),
        }
        locked_accounts_qs = _apply_locked_accounts_filters(
            _scoped_locked_accounts_queryset(user),
            query=locked_q,
            user_type=locked_user_type,
            status=locked_status,
            locked_by=locked_by,
            locked_start_dt=locked_start_dt,
            locked_end_dt=locked_end_dt,
            appeal_start_dt=locked_appeal_start_dt,
            appeal_end_dt=locked_appeal_end_dt,
        )
        locked_accounts_page = Paginator(locked_accounts_qs, 25).get_page(request.GET.get('locked_page') or 1)
        locked_accounts_rows = _attach_locked_account_metadata(list(locked_accounts_page.object_list))

    dormant_agents_summary = {'total': 0, 'login_7': 0, 'login_14': 0, 'login_30': 0}
    dormant_agents_page = None
    dormant_agents_rows = []
    if user.user_type in ['super_agent', 'master_agent']:
        dormant_dataset = _build_dormant_center_dataset(
            request.user,
            query=dormant_q,
            agent_id=dormant_agent,
            super_agent_id=(dormant_super_agent if user.user_type == 'master_agent' else ''),
            status=dormant_status,
            bucket=dormant_bucket,
            start_dt=(_parse_dashboard_bound(start_date_str, end=False) if start_date_str else None),
            end_dt=(_parse_dashboard_bound(end_date_str, end=True) if end_date_str else None),
        )
        dormant_agents_summary = {
            'total': dormant_dataset['current_bucket_total'],
            'login_7': dormant_dataset['cards']['login_7'],
            'login_14': dormant_dataset['cards']['login_14'],
            'login_30': dormant_dataset['cards']['login_30'],
        }
        dormant_agents_page = Paginator(dormant_dataset['rows'], 25).get_page(request.GET.get('dormant_page') or 1)
        dormant_agents_rows = _attach_dormant_agent_drilldown(list(dormant_agents_page.object_list))

    dashboard_loan = None
    if user.user_type in ['agent', 'super_agent']:
        dashboard_loan = get_user_outstanding_loans(user).first()

    overdraft_wallet_card = None
    if user.user_type == 'super_agent':
        try:
            overdraft_wallet_card = get_or_create_overdraft_wallet(user)
        except LoanOverdraftError:
            overdraft_wallet_card = None

    pending_overdraft_requests = []
    if user.user_type == 'super_agent':
        pending_overdraft_requests = list(
            Loan.objects.filter(
                lender=user,
                approval_level='super_agent',
                status='pending',
            ).select_related('borrower').order_by('created_at')[:10]
        )

    qualified_overdraft_agent_rows = []
    if user.user_type == 'super_agent':
        loan_settings = get_loan_settings()
        direct_agent_qs = User.objects.filter(
            user_type='agent',
            super_agent=user,
        ).order_by('first_name', 'last_name', 'username', 'email')
        for agent_row in direct_agent_qs:
            snapshot = build_qualification_snapshot(agent_row)
            meets_core_requirements = (
                snapshot.ticket_count >= loan_settings['min_ticket_count']
                and snapshot.deposit_total >= loan_settings['min_deposit_amount']
            )
            has_pending_request = Loan.objects.filter(borrower=agent_row, status='pending').exists()
            outstanding_amount = get_user_outstanding_loan_amount(agent_row)
            qualified_overdraft_agent_rows.append(
                SimpleNamespace(
                    agent=agent_row,
                    snapshot=snapshot,
                    is_qualified=meets_core_requirements,
                    amount_qualified=(snapshot.qualified_amount if meets_core_requirements else Decimal('0.00')),
                    outstanding_overdraft_amount=outstanding_amount,
                    has_pending_request=has_pending_request,
                    has_outstanding_loan=outstanding_amount > Decimal('0.00'),
                    request_window_open=(timezone.localtime(timezone.now()) >= snapshot.request_open_at),
                    can_submit_now=(snapshot.can_submit_now and not has_pending_request and outstanding_amount <= Decimal('0.00')),
                )
            )
        qualified_overdraft_agent_rows.sort(
            key=lambda row: (
                row.is_qualified,
                row.amount_qualified,
                row.snapshot.deposit_total,
                row.snapshot.ticket_count,
            ),
            reverse=True,
        )

    context = {
        'user': user,
        'downline_users': downline_users_qs, # Pass the QuerySet
        'direct_downline_rows': direct_downline_rows,
        'master_downline_tree': master_downline_tree,
        'super_downline_tree': super_downline_tree,
        'downline_bet_tickets': downline_bet_tickets.order_by(order_expr)[:50], # Pass the QuerySet, sliced
        'total_downline_users': total_downline_users,
        'total_downline_turnover': total_downline_turnover,
        'total_downline_stake': total_downline_turnover, # Alias for total stake
        'total_downline_winnings': total_downline_winnings,
        'downline_ggr': downline_ggr,
        'metrics_scope': scope,
        'metrics_label': metrics_label,
        'metrics_start': metrics_start,
        'metrics_end': metrics_end,
        'current_start_date': metrics_start.isoformat() if metrics_start else '',
        'current_end_date': metrics_end.isoformat() if metrics_end else '',
        'current_sort_by': sort_by,
        'current_sort_dir': sort_dir,
        'total_commission_paid': total_commission_paid,
        'pending_commission': pending_commission,
        'pending_commission_single': pending_commission_single,
        'pending_commission_multiple': pending_commission_multiple,
        'top_performers': top_performers,
        'recent_downline_transactions': recent_downline_transactions,
        'show_reports': True,
        'locked_accounts_summary': locked_accounts_summary,
        'locked_accounts_page': locked_accounts_page,
        'locked_accounts_rows': locked_accounts_rows,
        'locked_q': locked_q,
        'locked_user_type': locked_user_type,
        'locked_status': locked_status,
        'locked_by': locked_by,
        'locked_start_date': locked_start_date,
        'locked_end_date': locked_end_date,
        'locked_appeal_start_date': locked_appeal_start_date,
        'locked_appeal_end_date': locked_appeal_end_date,
        'dormant_agents_summary': dormant_agents_summary,
        'dormant_agents_page': dormant_agents_page,
        'dormant_agents_rows': dormant_agents_rows,
        'dormant_q': dormant_q,
        'dormant_bucket': dormant_bucket,
        'dormant_agent': dormant_agent,
        'dormant_super_agent': dormant_super_agent,
        'dormant_status': dormant_status,
        'dormant_super_agent_choices': list(direct_super_agents_qs),
        'dashboard_loan': dashboard_loan,
        'outstanding_overdraft_amount': get_user_outstanding_loan_amount(user) if user.user_type in ['agent', 'super_agent'] else Decimal('0.00'),
        'overdraft_wallet_card': overdraft_wallet_card,
        'pending_overdraft_requests': pending_overdraft_requests,
        'qualified_overdraft_agent_rows': qualified_overdraft_agent_rows,
        'ticket_transactions_widget': _ticket_transaction_widget_context(
            user,
            limit=8,
            date_from=metrics_start.isoformat() if metrics_start else '',
            date_to=metrics_end.isoformat() if metrics_end else '',
        ),
    }
    return render(request, 'betting/agent_dashboard.html', context)


def footer_page(request, slug):
    page = get_object_or_404(FooterPage, slug=slug, is_active=True)
    return render(request, 'betting/footer_page.html', {'page': page})


def betting_results_view(request):
    """
    View for displaying betting results filtered by BettingPeriod, Serial Number, and Date.
    """
    period_id = request.GET.get('period_id')
    serial_number = request.GET.get('serial_number')
    match_date = request.GET.get('match_date')
    
    # Get all betting periods (including past ones) for the dropdown, latest first
    betting_periods = BettingPeriod.objects.all().order_by('-start_date')
    
    # Default to the most recent period if none selected
    if not period_id and betting_periods.exists():
        selected_period = betting_periods.first()
        period_id = selected_period.id
    else:
        selected_period = get_object_or_404(BettingPeriod, id=period_id) if period_id else None

    fixtures = Fixture.objects.none()
    if selected_period:
        # Base queryset for the selected period
        fixtures = Fixture.objects.filter(
            betting_period=selected_period
        )
        
        # Apply Serial Number filter
        if serial_number:
            fixtures = fixtures.filter(serial_number=serial_number)
            
        # Apply Date filter
        if match_date:
            fixtures = fixtures.filter(match_date=match_date)
            
        fixtures = fixtures.order_by('serial_number')

    context = {
        'betting_periods': betting_periods,
        'selected_period': selected_period,
        'fixtures': fixtures,
        'page_title': 'Betting Results',
        'current_serial': serial_number,
        'current_date': match_date
    }
    return render(request, 'betting/results.html', context)


@login_required
@user_passes_test(is_master_agent)
def master_agent_dashboard(request):
    return agent_dashboard(request)


@login_required
@user_passes_test(is_super_agent)
def super_agent_dashboard(request):
    return agent_dashboard(request)


@login_required
@user_passes_test_403(lambda u: u.user_type in ['super_agent', 'master_agent'])
def downline_dormant_agents_export(request):
    fmt = (request.GET.get('format') or 'csv').strip().lower()
    dormant_q = (request.GET.get('dormant_q') or '').strip()
    dormant_bucket = (request.GET.get('dormant_bucket') or 'login_7').strip() or 'login_7'
    dormant_agent = (request.GET.get('dormant_agent') or '').strip()
    dormant_super_agent = (request.GET.get('dormant_super_agent') or '').strip()
    dormant_status = (request.GET.get('dormant_status') or '').strip()
    start_date = (request.GET.get('start_date') or '').strip()
    end_date = (request.GET.get('end_date') or '').strip()

    def _parse_dashboard_bound(value, *, end=False):
        value = (value or '').strip()
        if not value:
            return None
        try:
            parsed = date.fromisoformat(value)
            return timezone.make_aware(datetime.combine(parsed, datetime.max.time() if end else datetime.min.time()))
        except Exception:
            return None

    dataset = _build_dormant_center_dataset(
        request.user,
        query=dormant_q,
        agent_id=dormant_agent,
        super_agent_id=(dormant_super_agent if request.user.user_type == 'master_agent' else ''),
        status=dormant_status,
        bucket=dormant_bucket,
        start_dt=_parse_dashboard_bound(start_date, end=False),
        end_dt=_parse_dashboard_bound(end_date, end=True),
    )
    rows = []
    for user_obj in dataset['rows']:
        rows.append({
            'username': user_obj.username or user_obj.email or '',
            'agent_name': user_obj.get_full_name() or '',
            'super_agent': getattr(getattr(user_obj, 'super_agent', None), 'username', '') or getattr(getattr(user_obj, 'super_agent', None), 'email', '') or '',
            'last_agent_activity': user_obj.last_agent_activity_at.isoformat(sep=' ', timespec='seconds') if getattr(user_obj, 'last_agent_activity_at', None) else '',
            'last_downline_activity': user_obj.last_downline_activity_at.isoformat(sep=' ', timespec='seconds') if getattr(user_obj, 'last_downline_activity_at', None) else '',
            'last_agent_login': user_obj.last_login.isoformat(sep=' ', timespec='seconds') if user_obj.last_login else '',
            'last_agent_bet': user_obj.agent_last_bet_at.isoformat(sep=' ', timespec='seconds') if getattr(user_obj, 'agent_last_bet_at', None) else '',
            'last_deposit_date': user_obj.agent_last_deposit_at.isoformat(sep=' ', timespec='seconds') if getattr(user_obj, 'agent_last_deposit_at', None) else '',
            'cashiers': getattr(user_obj, 'cashiers_count', 0) or 0,
            'dormant_days': getattr(user_obj, 'dormant_days', '') if getattr(user_obj, 'dormant_days', None) is not None else '',
            'status': 'Locked' if user_obj.is_locked else ('Active' if user_obj.is_active else 'Inactive'),
        })
    title = 'master_agent_dormant_agents' if request.user.user_type == 'master_agent' else 'super_agent_dormant_agents'
    return _export_simple_rows(rows=rows, title=title, fmt=fmt)


@login_required
@user_passes_test(lambda u: u.user_type in ['agent', 'super_agent', 'master_agent', 'admin'])
def downline_users(request):
    user = request.user
    user_type_filter = request.GET.get('user_type', 'all')
    search_query = request.GET.get('q', '')

    queryset = User.objects.all()

    # Filter based on current user's hierarchy
    if user.user_type == 'master_agent':
        queryset = queryset.filter(
            Q(master_agent=user) |
            Q(super_agent__master_agent=user) |
            Q(agent__super_agent__master_agent=user)
        )
    elif user.user_type == 'super_agent':
        queryset = queryset.filter(
            Q(super_agent=user) |
            Q(agent__super_agent=user)
        )
    elif user.user_type == 'agent':
        queryset = queryset.filter(agent=user)
    else:
        queryset = User.objects.none() # Non-admin/agent types only see themselves

    # Apply user type filter from GET parameter
    if user_type_filter != 'all':
        queryset = queryset.filter(user_type=user_type_filter)

    # Apply search query
    if search_query:
        queryset = queryset.filter(
            Q(email__icontains=search_query) |
            Q(first_name__icontains=search_query) |
            Q(last_name__icontains=search_query) |
            Q(phone_number__icontains=search_query)
        )

    # Exclude the logged-in user from the list if they are in the downline
    # Unless the user is an admin viewing all users
    if not (user.is_superuser or user.user_type == 'admin'):
        queryset = queryset.exclude(pk=user.pk)

    paginator = Paginator(queryset.order_by('email'), 10) # 10 users per page
    page_number = request.GET.get('page')

    try:
        downline_users_paginated = paginator.page(page_number)
    except PageNotAnInteger:
        downline_users_paginated = paginator.page(1)
    except EmptyPage:
        downline_users_paginated = paginator.page(paginator.num_pages)

    context = {
        'downline_users': downline_users_paginated,
        'user_type_choices': User.USER_TYPE_CHOICES,
        'current_user_type_filter': user_type_filter,
        'current_search_query': search_query,
    }
    return render(request, 'betting/downline_users.html', context)


@login_required
@user_passes_test(lambda u: u.user_type in ['agent', 'super_agent', 'master_agent', 'admin'])
def downline_bets(request):
    user = request.user
    status_filter = request.GET.get('status', 'all')
    user_filter = request.GET.get('user', 'all')
    start_date_str = request.GET.get('start_date')
    end_date_str = request.GET.get('end_date')

    # Default to last 30 days if no dates provided
    end_date = date.fromisoformat(end_date_str) if end_date_str else timezone.now().date()
    start_date = date.fromisoformat(start_date_str) if start_date_str else end_date - timedelta(days=30)

    # Determine downline users based on logged-in user's hierarchy
    if user.user_type == 'admin' or user.is_superuser:
        downline_users_qs = User.objects.all() # Admins can see all users' bets
    elif user.user_type == 'master_agent':
        downline_users_qs = User.objects.filter(
            Q(master_agent=user) |
            Q(super_agent__master_agent=user) |
            Q(agent__super_agent__master_agent=user)
        )
    elif user.user_type == 'super_agent':
        downline_users_qs = User.objects.filter(
            Q(super_agent=user) |
            Q(agent__super_agent=user)
        )
    elif user.user_type == 'agent':
        downline_users_qs = User.objects.filter(agent=user)
    else:
        downline_users_qs = User.objects.none() # Should not happen with decorator, but for safety

    # Filter bet tickets by downline users
    bet_tickets = BetTicket.objects.filter(user__in=downline_users_qs)

    # Apply status filter
    if status_filter != 'all':
        bet_tickets = bet_tickets.filter(status=status_filter)

    # Apply specific user filter if provided
    if user_filter != 'all':
        try:
            user_id = int(user_filter)
            bet_tickets = bet_tickets.filter(user__id=user_id)
        except (ValueError, TypeError):
            pass # Invalid user ID, ignore filter

    # Apply date filters
    if start_date_str:
        try:
            start_date = date.fromisoformat(start_date_str)
            bet_tickets = bet_tickets.filter(placed_at__date__gte=start_date)
        except ValueError:
            messages.error(request, "Invalid start date format.")
    if end_date_str:
        try:
            end_date = date.fromisoformat(end_date_str)
            bet_tickets = bet_tickets.filter(placed_at__date__lte=end_date)
        except ValueError:
            messages.error(request, "Invalid end date format.")

    paginator = Paginator(bet_tickets.order_by('-placed_at'), 10) # 10 tickets per page
    page_number = request.GET.get('page')

    try:
        downline_bet_tickets_paginated = paginator.page(page_number)
    except PageNotAnInteger:
        downline_bet_tickets_paginated = paginator.page(1)
    except EmptyPage:
        downline_bet_tickets_paginated = paginator.page(paginator.num_pages)

    context = {
        'bet_tickets': downline_bet_tickets_paginated,
        'status_choices': [('all', 'All')] + list(BetTicket.STATUS_CHOICES),
        'current_status_filter': status_filter,
        'all_downline_users': downline_users_qs.order_by('email'), # For the user filter dropdown
        'current_user_filter': user_filter,
        'start_date_filter': start_date_str,
        'end_date_filter': end_date_str,
    }
    return render(request, 'betting/downline_bets.html', context)


@login_required
@user_passes_test(lambda u: u.user_type in ['agent', 'super_agent', 'master_agent', 'admin'])
def agent_wallet_report(request):
    user = request.user
    user_filter = request.GET.get('user', 'all')

    # Determine downline users whose wallets to report on
    if user.user_type == 'admin' or user.is_superuser:
        report_users_qs = User.objects.all() # Admins can see all
    elif user.user_type == 'master_agent':
        report_users_qs = User.objects.filter(
            Q(master_agent=user) |
            Q(super_agent__master_agent=user) |
            Q(agent__super_agent__master_agent=user)
        )
    elif user.user_type == 'super_agent':
        report_users_qs = User.objects.filter(
            Q(super_agent=user) |
            Q(agent__super_agent=user)
        )
    elif user.user_type == 'agent':
        report_users_qs = User.objects.filter(agent=user)
    else:
        report_users_qs = User.objects.none()

    # Filter wallets by the determined users
    wallets = Wallet.objects.filter(user__in=report_users_qs)

    # Apply specific user filter if provided in GET params
    if user_filter != 'all':
        try:
            filter_user_id = int(user_filter)
            wallets = wallets.filter(user__id=filter_user_id)
        except (ValueError, TypeError):
            pass # Invalid user ID, ignore filter

    context = {
        'report_title': 'Agent/Admin Wallet Report',
        'wallets': wallets.order_by('user__email'),
        'all_users': report_users_qs.order_by('email'), # For the filter dropdown
        'current_user_filter': user_filter,
    }
    return render(request, 'betting/reports/wallet_report.html', context)


@login_required
@user_passes_test(lambda u: u.is_superuser or u.user_type in ['admin', 'agent', 'super_agent', 'master_agent'])
def agent_sales_winnings_report(request):
    user = request.user
    user_filter = request.GET.get('user', 'all')
    start_date_str = request.GET.get('start_date')
    end_date_str = request.GET.get('end_date')

    # Default to last 30 days if no dates provided
    end_date = date.fromisoformat(end_date_str) if end_date_str else timezone.now().date()
    start_date = date.fromisoformat(start_date_str) if start_date_str else end_date - timedelta(days=30)

    # Determine relevant users based on hierarchy
    if user.user_type == 'admin' or user.is_superuser:
        relevant_users_qs = User.objects.all()
    elif user.user_type == 'master_agent':
        relevant_users_qs = User.objects.filter(
            Q(master_agent=user) |
            Q(super_agent__master_agent=user) |
            Q(agent__super_agent__master_agent=user)
        ).distinct()
    elif user.user_type == 'super_agent':
        relevant_users_qs = User.objects.filter(
            Q(super_agent=user) |
            Q(agent__super_agent=user)
        ).distinct()
    elif user.user_type == 'agent':
        relevant_users_qs = User.objects.filter(agent=user).distinct()
    else:
        relevant_users_qs = User.objects.none() # Should not be reached due to decorator

    # Apply specific user filter if provided
    if user_filter != 'all':
        try:
            filter_user_id = int(user_filter)
            relevant_users_qs = relevant_users_qs.filter(id=filter_user_id)
        except (ValueError, TypeError):
            pass # Invalid user ID, ignore filter

    # Aggregate sales and winnings for each relevant user within the date range
    report_data = []
    for u in relevant_users_qs.order_by('email'):
        bets_by_user = BetTicket.objects.filter(
            user=u,
            placed_at__date__gte=start_date,
            placed_at__date__lt=end_date + timedelta(days=1) # Include full end date
        ).exclude(status='deleted')

        total_stake = bets_by_user.aggregate(Sum('stake_amount'))['stake_amount__sum'] or Decimal('0.00')
        total_winnings = bets_by_user.filter(status='won').aggregate(Sum('potential_winning'))['potential_winning__sum'] or Decimal('0.00')
        
        net_result = total_stake - total_winnings

        if total_stake > Decimal('0.00') or total_winnings > Decimal('0.00'): # Only include users with activity
            report_data.append({
                'user': u,
                'total_stake': total_stake,
                'total_winnings': total_winnings,
                'net_result': net_result,
            })

    context = {
        'report_title': 'Agent/Admin Sales & Winnings Report',
        'report_data': report_data,
        'all_users': relevant_users_qs.order_by('email'),
        'current_user_filter': user_filter,
        'start_date_filter': start_date.isoformat(),
        'end_date_filter': end_date.isoformat(),
    }
    return render(request, 'betting/reports/sales_winnings_report.html', context)


@login_required
@user_passes_test(lambda u: u.is_superuser or u.user_type in ['admin', 'agent', 'super_agent', 'master_agent'])
def agent_commission_report(request):
    user = request.user
    agent_filter_id = request.GET.get('user', 'all')
    start_date_str = request.GET.get('start_date')
    end_date_str = request.GET.get('end_date')

    end_date = date.fromisoformat(end_date_str) if end_date_str else timezone.now().date()
    start_date = date.fromisoformat(start_date_str) if start_date_str else end_date - timedelta(days=30)

    # Determine which agents/users to fetch data for
    all_users = None
    if user.is_superuser or user.user_type == 'admin':
        all_users = User.objects.filter(user_type__in=['agent', 'super_agent', 'master_agent']).order_by('email')
        
    commission_data = []

    # Fetch Weekly Commissions
    weekly_qs = WeeklyAgentCommission.objects.filter(
        period__end_date__gte=start_date,
        period__start_date__lte=end_date
    ).select_related('agent', 'period')

    if user.is_superuser or user.user_type == 'admin':
        if agent_filter_id != 'all':
            weekly_qs = weekly_qs.filter(agent__id=agent_filter_id)
    else:
        weekly_qs = weekly_qs.filter(agent=user)

    for wc in weekly_qs:
        commission_data.append({
            'agent': wc.agent,
            'period': str(wc.period),
            'type': 'Weekly Agent Commission',
            'commission_amount': wc.commission_total_amount,
            'status': wc.status,
            'paid_at': wc.paid_at,
            'ggr': wc.ggr
        })

    # Fetch Monthly Network Commissions
    monthly_qs = MonthlyNetworkCommission.objects.filter(
        period__end_date__gte=start_date,
        period__start_date__lte=end_date
    ).select_related('user', 'period')

    if user.is_superuser or user.user_type == 'admin':
        if agent_filter_id != 'all':
            monthly_qs = monthly_qs.filter(user__id=agent_filter_id)
    else:
        monthly_qs = monthly_qs.filter(user=user)

    for mc in monthly_qs:
        commission_data.append({
            'agent': mc.user,
            'period': str(mc.period),
            'type': f"Monthly {mc.role.replace('_', ' ').title()} Commission",
            'commission_amount': mc.commission_amount,
            'status': mc.status,
            'paid_at': mc.paid_at,
            'ggr': mc.ngr # Use NGR as GGR equivalent
        })
    
    # Sort by date (descending) - tricky as period string might not sort well, but good enough
    commission_data.sort(key=lambda x: x['period'], reverse=True)

    # Calculate Summary
    total_paid = sum(c['commission_amount'] for c in commission_data if c['status'] == 'paid')
    outstanding = sum(c['commission_amount'] for c in commission_data if c['status'] == 'pending')
    total_ggr = sum(c['ggr'] for c in commission_data)
    
    payout_ratio = Decimal(0)
    total_comm = total_paid + outstanding
    if total_ggr > 0:
        payout_ratio = (total_comm / total_ggr * 100).quantize(Decimal('0.01'))

    context = {
        'report_title': 'Commission Report',
        'commission_data': commission_data,
        'all_users': all_users, 
        'current_user_filter': agent_filter_id,
        'start_date_filter': start_date.isoformat(),
        'end_date_filter': end_date.isoformat(),
        'summary': {
            'total_paid': total_paid,
            'outstanding': outstanding,
            'total_ggr': total_ggr,
            'payout_ratio': payout_ratio
        }
    }
    return render(request, 'betting/reports/commission_report.html', context)


@login_required
@user_passes_test(is_admin)
def admin_commission_financial_report(request):
    """
    Comprehensive financial report for Admin to track all commissions.
    """
    # Filter params
    period_type = request.GET.get('period_type', 'all')
    status = request.GET.get('status', 'all')
    start_date_str = request.GET.get('start_date')
    end_date_str = request.GET.get('end_date')
    user_search = request.GET.get('user_search', '')

    end_date = date.fromisoformat(end_date_str) if end_date_str else timezone.now().date()
    start_date = date.fromisoformat(start_date_str) if start_date_str else end_date - timedelta(days=30)

    weekly_qs = WeeklyAgentCommission.objects.filter(
        period__end_date__gte=start_date,
        period__end_date__lte=end_date
    ).select_related('agent', 'period')
    
    monthly_qs = MonthlyNetworkCommission.objects.filter(
        period__end_date__gte=start_date,
        period__end_date__lte=end_date
    ).select_related('user', 'period')

    # Apply filters
    if status != 'all':
        weekly_qs = weekly_qs.filter(status=status)
        monthly_qs = monthly_qs.filter(status=status)
    if user_search:
        weekly_qs = weekly_qs.filter(
            Q(agent__email__icontains=user_search) | 
            Q(agent__first_name__icontains=user_search) | 
            Q(agent__last_name__icontains=user_search)
        )
        monthly_qs = monthly_qs.filter(
            Q(user__email__icontains=user_search) | 
            Q(user__first_name__icontains=user_search) | 
            Q(user__last_name__icontains=user_search)
        )

    # Aggregates
    weekly_paid = weekly_qs.aggregate(total=Sum('amount_paid'))['total'] or Decimal('0.00')
    weekly_pending_total = weekly_qs.filter(status__in=['pending', 'approved', 'partially_paid']).aggregate(total=Sum('commission_total_amount'))['total'] or Decimal('0.00')
    weekly_pending_paid = weekly_qs.filter(status__in=['pending', 'approved', 'partially_paid']).aggregate(total=Sum('amount_paid'))['total'] or Decimal('0.00')
    weekly_stats = weekly_qs.aggregate(
        total_ggr=Sum('ggr'),
        total_stake=Sum('total_stake')
    )
    
    monthly_paid = monthly_qs.aggregate(total=Sum('amount_paid'))['total'] or Decimal('0.00')
    monthly_pending_total = monthly_qs.filter(status__in=['pending', 'approved', 'partially_paid']).aggregate(total=Sum('commission_amount'))['total'] or Decimal('0.00')
    monthly_pending_paid = monthly_qs.filter(status__in=['pending', 'approved', 'partially_paid']).aggregate(total=Sum('amount_paid'))['total'] or Decimal('0.00')
    monthly_stats = monthly_qs.aggregate(
        total_ngr=Sum('ngr')
    )
    
    def get_val(val): return val or Decimal('0.00')
    
    summary = {
        'total_weekly_paid': weekly_paid,
        'total_weekly_pending': max(Decimal('0.00'), weekly_pending_total - weekly_pending_paid),
        'total_weekly_ggr': get_val(weekly_stats['total_ggr']),
        'total_weekly_stake': get_val(weekly_stats['total_stake']),
        
        'total_monthly_paid': monthly_paid,
        'total_monthly_pending': max(Decimal('0.00'), monthly_pending_total - monthly_pending_paid),
        'total_monthly_ngr': get_val(monthly_stats['total_ngr']),
        
        'grand_total_paid': weekly_paid + monthly_paid,
        'grand_total_pending': max(Decimal('0.00'), weekly_pending_total - weekly_pending_paid) + max(Decimal('0.00'), monthly_pending_total - monthly_pending_paid),
    }

    # Prepare list for table
    commission_list = []
    if period_type in ['all', 'weekly']:
        for item in weekly_qs:
            commission_list.append({
                'type': 'Weekly (Agent)',
                'user': item.agent,
                'period': item.period,
                'amount': item.commission_total_amount,
                'status': item.status,
                'basis_amount': item.ggr, # Show GGR as basis
                'created_at': item.created_at
            })
            
    if period_type in ['all', 'monthly']:
        for item in monthly_qs:
            commission_list.append({
                'type': 'Monthly (Network)',
                'user': item.user,
                'period': item.period,
                'amount': item.commission_amount,
                'status': item.status,
                'basis_amount': item.ngr, # Show NGR as basis
                'created_at': item.created_at
            })
            
    # Sort by period start date desc
    commission_list.sort(key=lambda x: x['period'].start_date, reverse=True)
    
    # Pagination
    paginator = Paginator(commission_list, 20)
    page_number = request.GET.get('page')
    try:
        page_obj = paginator.page(page_number)
    except PageNotAnInteger:
        page_obj = paginator.page(1)
    except EmptyPage:
        page_obj = paginator.page(paginator.num_pages)

    context = {
        'summary': summary,
        'commissions': page_obj,
        'filter_params': request.GET,
        'start_date_filter': start_date.isoformat(),
        'end_date_filter': end_date.isoformat(),
    }
    return render(request, 'betting/reports/admin_commission_financial_report.html', context)


# --- Admin Dashboard & Management Views ---

@login_required
@user_passes_test(lambda u: u.is_superuser or u.user_type == 'admin')
def admin_dashboard(request):
    total_users = User.objects.count()
    total_players = User.objects.filter(user_type='player').count()
    total_cashiers = User.objects.filter(user_type='cashier').count()
    total_agents = User.objects.filter(user_type='agent').count()
    total_super_agents = User.objects.filter(user_type='super_agent').count()
    total_master_agents = User.objects.filter(user_type='master_agent').count()
    total_admins = User.objects.filter(user_type='admin').count()

    total_bets_placed = BetTicket.objects.count()
    total_stake_amount = BetTicket.objects.aggregate(Sum('stake_amount'))['stake_amount__sum'] or Decimal('0.00')
    total_potential_winning = BetTicket.objects.filter(status='won').aggregate(Sum('potential_winning'))['potential_winning__sum'] or Decimal('0.00')
    
    total_deposits = Transaction.objects.filter(transaction_type='deposit', is_successful=True).aggregate(Sum('amount'))['amount__sum'] or Decimal('0.00')
    total_withdrawals = UserWithdrawal.objects.filter(status='approved').aggregate(Sum('amount'))['amount__sum'] or Decimal('0.00')

    pending_bets = BetTicket.objects.filter(status='pending').count()
    won_bets = BetTicket.objects.filter(status='won').count()
    lost_bets = BetTicket.objects.filter(status='lost').count()
    deleted_bets = BetTicket.objects.filter(status='deleted').count() # Tickets marked as deleted/voided
    
    pending_registrations_count = PendingAgentRegistration.objects.filter(status='PENDING').count()

    try:
        global_limits = GlobalBettingSettings.load()
    except (OperationalError, ProgrammingError):
        global_limits = None
    active_agent_overrides = AgentBettingLimitOverride.objects.filter(is_active=True, custom_limits_enabled=True).count()
    today = timezone.localdate()
    rejected_tickets_today = BettingLimitAuditLog.objects.filter(action_type='TICKET_REJECTED', created_at__date=today).count()

    agents_with_custom_limits = (
        AgentBettingLimitOverride.objects
        .filter(is_active=True, custom_limits_enabled=True)
        .select_related('agent')
        .order_by('-updated_at')[:10]
    )

    top_exposure_agents = (
        BetTicket.objects
        .filter(status='pending', user__agent__isnull=False)
        .values('user__agent_id', 'user__agent__email', 'user__agent__username')
        .annotate(exposure=Coalesce(Sum('max_winning'), Value(0), output_field=DecimalField()))
        .order_by('-exposure')[:10]
    )

    platform_exposure_today = (
        BetTicket.objects
        .filter(placed_at__date=today, status__in=['pending', 'won'])
        .aggregate(total=Coalesce(Sum('max_winning'), Value(0), output_field=DecimalField()))['total']
    )

    platform_sales_today = (
        BetTicket.objects
        .filter(placed_at__date=today)
        .aggregate(total=Coalesce(Sum('stake_amount'), Value(0), output_field=DecimalField()))['total']
    )

    pending_crm_wallet_approvals = list(
        CreditRequest.objects
        .filter(status='pending', request_type__in=CRM_WALLET_APPROVAL_REQUEST_TYPES)
        .select_related('requester', 'recipient')
        .order_by('-created_at')[:10]
    )
    pending_crm_wallet_approval_count = len(pending_crm_wallet_approvals)

    selected_commission_period = None
    commission_period_id = (request.GET.get('commission_period_id') or '').strip()
    commission_periods = list(CommissionPeriod.objects.filter(period_type='weekly').order_by('-start_date')[:104])
    if commission_period_id:
        try:
            selected_commission_period = CommissionPeriod.objects.filter(period_type='weekly', id=int(commission_period_id)).first()
        except Exception:
            selected_commission_period = None
    if selected_commission_period is None and commission_periods:
        selected_commission_period = commission_periods[0]

    period_turnover = Decimal('0.00')
    period_winnings = Decimal('0.00')
    period_ggr = Decimal('0.00')
    period_commission_paid = Decimal('0.00')
    period_ngr = Decimal('0.00')
    if selected_commission_period:
        period_start_dt = timezone.make_aware(datetime.combine(selected_commission_period.start_date, datetime.min.time()))
        period_end_dt = timezone.make_aware(datetime.combine(selected_commission_period.end_date, datetime.max.time()))
        tickets_qs = (
            BetTicket.objects.exclude(status__in=['deleted', 'cancelled'])
            .filter(placed_at__gte=period_start_dt, placed_at__lte=period_end_dt)
        )
        period_turnover = tickets_qs.aggregate(total=Coalesce(Sum('stake_amount'), Value(0), output_field=DecimalField()))['total']
        period_winnings = (
            tickets_qs.filter(status='won')
            .aggregate(total=Coalesce(Sum('max_winning'), Value(0), output_field=DecimalField()))['total']
        )
        period_ggr = (period_turnover or Decimal('0.00')) - (period_winnings or Decimal('0.00'))
        period_commission_paid = (
            WeeklyAgentCommission.objects.filter(period=selected_commission_period)
            .aggregate(total=Coalesce(Sum('amount_paid'), Value(0), output_field=DecimalField()))['total']
        )
        period_ngr = (period_ggr or Decimal('0.00')) - (period_commission_paid or Decimal('0.00'))

    context = {
        'total_users': total_users,
        'total_players': total_players,
        'total_cashiers': total_cashiers,
        'total_agents': total_agents,
        'total_super_agents': total_super_agents,
        'total_master_agents': total_master_agents,
        'total_admins': total_admins,
        'total_bets_placed': total_bets_placed,
        'total_stake_amount': total_stake_amount,
        'total_potential_winning': total_potential_winning,
        'total_deposits': total_deposits,
        'total_withdrawals': total_withdrawals,
        'pending_bets': pending_bets,
        'won_bets': won_bets,
        'lost_bets': lost_bets,
        'deleted_bets': deleted_bets,
        'pending_registrations_count': pending_registrations_count,
        'global_limits': global_limits,
        'active_agent_overrides': active_agent_overrides,
        'rejected_tickets_today': rejected_tickets_today,
        'agents_with_custom_limits': agents_with_custom_limits,
        'top_exposure_agents': top_exposure_agents,
        'platform_exposure_today': platform_exposure_today,
        'platform_sales_today': platform_sales_today,
        'pending_crm_wallet_approvals': pending_crm_wallet_approvals,
        'pending_crm_wallet_approval_count': pending_crm_wallet_approval_count,
        'commission_periods': commission_periods,
        'selected_commission_period': selected_commission_period,
        'commission_period_id': str(getattr(selected_commission_period, 'id', '') or ''),
        'period_turnover': period_turnover,
        'period_winnings': period_winnings,
        'period_ggr': period_ggr,
        'period_commission_paid': period_commission_paid,
        'period_ngr': period_ngr,
        'ticket_transactions_widget': _ticket_transaction_widget_context(
            request.user,
            limit=12,
            full_url_name='betting_admin:admin_ticket_transactions',
        ),
    }
    return render(request, 'betting/admin/dashboard.html', context)


@login_required
@user_passes_test(lambda u: u.is_superuser or u.user_type == 'admin')
def manage_users(request):
    user_type_filter = request.GET.get('user_type', 'all')
    search_query = request.GET.get('q', '')

    users_queryset = User.objects.all().order_by('email') # Default ordering

    if user_type_filter != 'all':
        users_queryset = users_queryset.filter(user_type=user_type_filter)
    
    if search_query:
        users_queryset = users_queryset.filter(
            Q(email__icontains=search_query) |
            Q(first_name__icontains=search_query) |
            Q(last_name__icontains=search_query) |
            Q(phone_number__icontains=search_query)
        )

    # Exclude the currently logged-in user from the list to prevent self-deletion issues
    users_queryset = users_queryset.exclude(pk=request.user.pk)

    paginator = Paginator(users_queryset, 10) # Show 10 users per page
    page_number = request.GET.get('page')

    try:
        users = paginator.page(page_number)
    except PageNotAnInteger:
        users = paginator.page(1)
    except EmptyPage:
        users = paginator.page(paginator.num_pages)

    context = {
        'users': users,
        'user_type_choices': User.USER_TYPE_CHOICES, # Assuming this is accessible from the User model
        'current_user_type_filter': user_type_filter,
        'current_search_query': search_query,
    }
    return render(request, 'betting/admin/manage_users.html', context)


@login_required
@user_passes_test(lambda u: u.is_superuser or u.user_type == 'admin')
def add_user(request):
    if request.method == 'POST':
        form = AdminUserCreationForm(request.POST, request=request) # Pass request to form
        if form.is_valid():
            user = form.save()
            Wallet.objects.create(user=user, balance=0) # Create a wallet for the new user
            messages.success(request, f"User {user.email} added successfully.")
            log_admin_activity(request, f"Added new user: {user.email} ({user.get_user_type_display()})")
            return redirect('betting_admin:manage_users')
        else:
            for field, errors in form.errors.items():
                for error in errors:
                    messages.error(request, f"{field.replace('_', ' ').title()}: {error}")
            if form.non_field_errors():
                for error in form.non_field_errors():
                    messages.error(request, f"Add User Error: {error}")
    else:
        form = AdminUserCreationForm(request=request) # Pass request to form for initial display
    return render(request, 'betting/admin/add_user.html', {'form': form})


@login_required
@user_passes_test(lambda u: u.is_superuser or u.user_type == 'admin')
def edit_user(request, user_id):
    user_to_edit = get_object_or_404(User, id=user_id)

    # Prevent editing superusers if logged-in user is not a superuser themselves
    if not request.user.is_superuser and user_to_edit.is_superuser:
        messages.error(request, "You do not have permission to edit superuser accounts.")
        return redirect('betting_admin:manage_users')
    
    # Prevent editing self if it would lead to permission issues
    if request.user.pk == user_to_edit.pk and not request.user.is_superuser:
        messages.warning(request, "You are trying to edit your own account. Use the 'Profile' page for personal changes, or ensure you have superuser privileges for advanced changes.")
        # Optionally redirect to profile page for self-edits, or proceed with warning
        # return redirect('betting:profile')

    if request.method == 'POST':
        form = AdminUserChangeForm(request.POST, instance=user_to_edit, request=request) # Pass request to form
        if form.is_valid():
            user = form.save()
            messages.success(request, f"User {user.email} updated successfully.")
            log_admin_activity(request, f"Edited user: {user.email} ({user.get_user_type_display()})")
            return redirect('betting_admin:manage_users')
        else:
            for field, errors in form.errors.items():
                for error in errors:
                    messages.error(request, f"{field.replace('_', ' ').title()}: {error}")
            if form.non_field_errors():
                for error in form.non_field_errors():
                    messages.error(request, f"Edit User Error: {error}")
    else:
        form = AdminUserChangeForm(instance=user_to_edit, request=request) # Pass request to form for initial display
    return render(request, 'betting/admin/edit_user.html', {'form': form, 'user_to_edit': user_to_edit})


@login_required
@user_passes_test(lambda u: u.is_superuser or u.user_type == 'admin')
@db_transaction.atomic
def delete_user(request, user_id):
    user_to_delete = get_object_or_404(User, id=user_id)

    if user_to_delete == request.user:
        messages.error(request, "You cannot delete your own account.")
        return redirect('betting_admin:manage_users')

    # Prevent deleting superusers if logged-in user is not a superuser themselves
    if user_to_delete.is_superuser and not request.user.is_superuser:
        messages.error(request, "You do not have permission to delete superuser accounts.")
        return redirect('betting_admin:manage_users')
    
    # Only allow admin to delete other admins (not superusers)
    if user_to_delete.user_type == 'admin' and not request.user.is_superuser:
        messages.error(request, "Only a superuser can delete other admin accounts.")
        return redirect('betting_admin:manage_users')

    user_email = user_to_delete.email # Capture email before deletion
    
    if request.method == 'POST':
        try:
            # Transfer wallet balance of deleted user to admin's wallet (or specific system account)
            # Find or create an an admin wallet for receiving funds
            admin_user_wallet = Wallet.objects.select_for_update().get_or_create(user=request.user)[0]
            deleted_user_wallet = Wallet.objects.select_for_update().get(user=user_to_delete)

            if deleted_user_wallet.balance > Decimal('0.00'):
                # Record transaction for the transfer
                tx = Transaction.objects.create(
                    user=user_to_delete,
                    initiating_user=request.user,
                    target_user=request.user,
                    transaction_type='wallet_balance_transfer_on_deletion',
                    amount=deleted_user_wallet.balance,
                    is_successful=True,
                    status='completed',
                    description=f"Wallet balance of {user_email} transferred to admin upon deletion.",
                    timestamp=timezone.now()
                )
                admin_user_wallet.apply_delta(
                    amount=deleted_user_wallet.balance,
                    actor=request.user,
                    transaction_obj=tx,
                    reference=str(user_to_delete.id),
                    reason=tx.description,
                    metadata={"deleted_user_id": user_to_delete.id, "source": "user_deletion"},
                )
                messages.info(request, f"Wallet balance of {user_email} (₦{deleted_user_wallet.balance:.2f}) transferred to your wallet.")


            # Mark related bet tickets as 'deleted' (void) and refund their stake
            # This handles cases where user is deleted but their bets are still active
            related_bet_tickets = BetTicket.objects.filter(user=user_to_delete).exclude(status__in=['deleted', 'won', 'lost', 'cashed_out', 'cancelled'])
            for ticket in related_bet_tickets:
                ticket.status = 'deleted'
                ticket.deleted_by = request.user
                ticket.deleted_at = timezone.now()
                ticket.save()
                
                user_wallet = Wallet.objects.select_for_update().get(user=ticket.user)
                refund_tx = Transaction.objects.create(
                    user=ticket.user,
                    initiating_user=request.user,
                    target_user=request.user,
                    transaction_type='fixture_deletion_refund',
                    amount=ticket.stake_amount,
                    is_successful=True,
                    status='completed',
                    description=f"Refund for stake on ticket {ticket.id} due to fixture deletion: {fixture_name}",
                    related_bet_ticket=ticket,
                    timestamp=timezone.now()
                )
                user_wallet.apply_delta(
                    amount=ticket.stake_amount,
                    actor=request.user,
                    transaction_obj=refund_tx,
                    reference=str(ticket.ticket_id),
                    reason=refund_tx.description,
                    metadata={"ticket_id": ticket.ticket_id, "source": "user_deletion_refund"},
                )
                log_admin_activity(request, f"Refunded ticket {ticket.id} due to deletion of fixture {fixture_name}.")

            user_to_delete.delete()
            messages.success(request, f"User {user_email} and associated data (wallet, transactions, bets) deleted successfully.")
            log_admin_activity(request, f"Deleted user: {user_email}")
            return redirect('betting_admin:manage_users')
        except Exception as e:
            messages.error(request, f"An error occurred while deleting user {user_email}: {e}")
            log_admin_activity(request, f"Failed to delete user: {user_email} - Error: {e}")
            return redirect('betting_admin:manage_users')
    
    messages.error(request, "Invalid request for user deletion.")
    return redirect('betting_admin:manage_users')


@login_required
@user_passes_test(lambda u: u.is_superuser or u.user_type == 'admin')
def manage_fixtures(request):
    status_filter = request.GET.get('status', 'all')
    period_filter = request.GET.get('period', 'all')
    search_query = request.GET.get('q', '')

    fixtures_queryset = Fixture.objects.all()

    if status_filter != 'all':
        fixtures_queryset = fixtures_queryset.filter(status=status_filter)
    
    if period_filter != 'all':
        try:
            period_id = int(period_filter)
            fixtures_queryset = fixtures_queryset.filter(betting_period__id=period_id)
        except (ValueError, TypeError):
            pass # Invalid period ID

    if search_query:
        fixtures_queryset = fixtures_queryset.filter(
            Q(home_team__icontains=search_query) |
            Q(away_team__icontains=search_query)
        )

    paginator = Paginator(fixtures_queryset.order_by('-match_time'), 10) # 10 fixtures per page
    page_number = request.GET.get('page')

    try:
        fixtures = paginator.page(page_number)
    except PageNotAnInteger:
        fixtures = paginator.page(1)
    except EmptyPage:
        fixtures = paginator.page(paginator.num_pages)

    context = {
        'fixtures': fixtures,
        'fixture_status_choices': [('all', 'All')] + list(Fixture.STATUS_CHOICES),
        'current_status_filter': status_filter,
        'all_betting_periods': BettingPeriod.objects.all().order_by('-start_date'),
        'current_period_filter': period_filter,
        'current_search_query': search_query,
    }
    return render(request, 'betting/admin/manage_fixtures.html', context)


@login_required
@user_passes_test(lambda u: u.is_superuser or u.user_type == 'admin')
def add_fixture(request):
    if request.method == 'POST':
        form = FixtureForm(request.POST)
        if form.is_valid():
            fixture = form.save(commit=False)
            # Assuming 'created_by' field exists on Fixture model. If not, remove this line.
            # fixture.created_by = request.user 
            fixture.save()
            messages.success(request, f"Fixture {fixture.home_team} vs {fixture.away_team} added successfully.")
            log_admin_activity(request, f"Added new fixture: {fixture.home_team} vs {fixture.away_team}")
            return redirect('betting_admin:manage_fixtures')
        else:
            for field, errors in form.errors.items():
                for error in errors:
                    messages.error(request, f"{field.replace('_', ' ').title()}: {error}")
            if form.non_field_errors():
                for error in form.non_field_errors():
                    messages.error(request, f"Add Fixture Error: {error}")
    else:
        form = FixtureForm()
    return render(request, 'betting/admin/add_fixture.html', {'form': form})


@login_required
@user_passes_test(lambda u: u.is_superuser or u.user_type == 'admin')
def edit_fixture(request, fixture_id):
    fixture = get_object_or_404(Fixture, id=fixture_id)
    if request.method == 'POST':
        form = FixtureForm(request.POST, instance=fixture)
        if form.is_valid():
            fixture = form.save()
            messages.success(request, f"Fixture {fixture.home_team} vs {fixture.away_team} updated successfully.")
            log_admin_activity(request, f"Edited fixture: {fixture.home_team} vs {fixture.away_team}")
            return redirect('betting_admin:manage_fixtures')
        else:
            for field, errors in form.errors.items():
                for error in errors:
                    messages.error(request, f"{field.replace('_', ' ').title()}: {error}")
            if form.non_field_errors():
                for error in form.non_field_errors():
                    messages.error(request, f"Edit Fixture Error: {error}")
    else:
        form = FixtureForm(instance=fixture)
    return render(request, 'betting/admin/edit_fixture.html', {'form': form, 'fixture': fixture})


@login_required
@user_passes_test(lambda u: u.is_superuser or u.user_type == 'admin')
@db_transaction.atomic
def delete_fixture(request, fixture_id):
    fixture = get_object_or_404(Fixture, id=fixture_id)
    fixture_name = f"{fixture.home_team} vs {fixture.away_team}"

    if fixture.status != 'pending': # Assuming 'pending' means it's still open for betting. If your model uses 'open', replace 'pending'.
        messages.error(request, f"Fixture '{fixture_name}' cannot be deleted as its status is '{fixture.status}'. Only 'pending' fixtures can be deleted.")
        return redirect('betting_admin:manage_fixtures')

    if request.method == 'POST':
        try:
            # Mark associated pending bets as deleted and refund stake
            pending_bets = BetTicket.objects.filter(
                selections__fixture=fixture, # Selects tickets that have this fixture
                status='pending'
            ).distinct() # Use distinct to avoid duplicates if a ticket has multiple selections

            for ticket in pending_bets:
                ticket.status = 'deleted'
                ticket.deleted_by = request.user
                ticket.deleted_at = timezone.now()
                ticket.save()

                user_wallet = Wallet.objects.select_for_update().get(user=ticket.user)
                refund_tx = Transaction.objects.create(
                    user=ticket.user,
                    initiating_user=request.user,
                    target_user=request.user,
                    transaction_type='fixture_deletion_refund',
                    amount=ticket.stake_amount,
                    is_successful=True,
                    status='completed',
                    description=f"Refund for stake on ticket {ticket.id} due to fixture deletion: {fixture_name}",
                    related_bet_ticket=ticket,
                    timestamp=timezone.now()
                )
                user_wallet.apply_delta(
                    amount=ticket.stake_amount,
                    actor=request.user,
                    transaction_obj=refund_tx,
                    reference=str(ticket.ticket_id),
                    reason=refund_tx.description,
                    metadata={"ticket_id": ticket.ticket_id, "source": "fixture_deletion"},
                )
                log_admin_activity(request, f"Refunded ticket {ticket.id} due to deletion of fixture {fixture_name}.")

            fixture.delete()
            messages.success(request, f"Fixture '{fixture_name}' and associated pending bets (refunded) deleted successfully.")
            log_admin_activity(request, f"Deleted fixture: {fixture_name}")
            return redirect('betting_admin:manage_fixtures')
        except Exception as e:
            messages.error(request, f"An error occurred while deleting fixture '{fixture_name}': {e}")
            log_admin_activity(request, f"Failed to delete fixture: {fixture_name} - Error: {e}")
            return redirect('betting_admin:manage_fixtures')
    
    messages.error(request, "Invalid request for fixture deletion.")
    return redirect('betting_admin:manage_fixtures')


@login_required
@user_passes_test(lambda u: u.is_superuser or u.user_type == 'admin')
@db_transaction.atomic
def declare_result(request, fixture_id):
    fixture = get_object_or_404(Fixture, id=fixture_id)

    if fixture.status != 'pending': # Assuming fixtures are 'pending' before result declaration
        messages.warning(request, f"Fixture is already '{fixture.status}'. Result cannot be declared again.")
        return redirect('betting_admin:manage_fixtures')

    if request.method == 'POST':
        form = DeclareResultForm(request.POST, instance=fixture)
        if form.is_valid():
            # Get the result from the form.
            # IMPORTANT: Your Fixture model has a 'result' field with choices like 'home_win', 'draw', etc.
            # Your form's 'result' field correctly points to this.
            # The 'winning_outcome' variable in the previous code snippet for fixture was not a model field.
            # So, we should update the fixture.result directly.
            fixture.home_score = form.cleaned_data['home_score']
            fixture.away_score = form.cleaned_data['away_score']
            fixture.result = form.cleaned_data['result'] # Use the form's cleaned data for result
            fixture.status = form.cleaned_data['status'] 
            fixture.save()

            # Process all bet tickets related to this fixture
            # Use select_related/prefetch_related if fetching many to reduce queries
            bets_on_this_fixture = BetTicket.objects.filter(
                selections__fixture=fixture 
            ).distinct().select_for_update() 
            affected_ticket_ids = [str(pk) for pk in bets_on_this_fixture.values_list('id', flat=True)]

            for ticket in bets_on_this_fixture:
                # Find the specific selection for *this* fixture within *this* ticket
                selection_for_this_fixture = ticket.selections.filter(fixture=fixture).first()

                if not selection_for_this_fixture:
                    continue # Should not happen if query above is correct

                if ticket.status == 'pending': 
                    # Determine if the selection on this ticket for this fixture wins
                    is_selection_winning = False
                    # The following logic should mirror how results are actually determined
                    # based on fixture.home_score, fixture.away_score and fixture.result (which is now correctly set)

                    # Simplified: if the selection matches the declared fixture result, it's winning for that selection
                    if selection_for_this_fixture.bet_type == fixture.result:
                        is_selection_winning = True
                    # Handle DNB cases where a draw voids the selection
                    elif selection_for_this_fixture.bet_type == 'home_dnb' and fixture.result == 'draw':
                        is_selection_winning = None # Voided
                    elif selection_for_this_fixture.bet_type == 'away_dnb' and fixture.result == 'draw':
                        is_selection_winning = None # Voided
                    # Add more complex logic for Over/Under, BTTS if not covered by direct result match
                    # (Your Fixture clean method already sets fixture.result based on scores, so this simplifies things)
                    
                    selection_for_this_fixture.is_winning_selection = is_selection_winning
                    selection_for_this_fixture.save()

                    # Re-evaluate entire ticket status after updating this selection
                    # This logic should ideally be in a BetTicket method
                    all_selections_evaluated = True
                    ticket_still_winning = True
                    ticket_is_cancelled = False

                    for sel in ticket.selections.all():
                        if sel.is_winning_selection is None: # Found a voided selection
                            ticket_is_cancelled = True
                            break
                        if sel.is_winning_selection == False: # Found a losing selection
                            ticket_still_winning = False
                        
                        # Check if fixture related to this selection is settled/cancelled
                        if sel.fixture.status not in ['settled', 'cancelled']:
                            all_selections_evaluated = False
                            break # Not all fixtures on the ticket are settled yet

                    if all_selections_evaluated:
                        if ticket_is_cancelled:
                            ticket.status = 'cancelled'
                            # Refund stake for cancelled tickets
                            user_wallet = Wallet.objects.select_for_update().get(user=ticket.user)
                            refund_tx = Transaction.objects.create(
                                user=ticket.user,
                                initiating_user=request.user, 
                                target_user=ticket.user,
                                transaction_type='ticket_cancellation_refund',
                                amount=ticket.stake_amount,
                                is_successful=True,
                                status='completed',
                                description=f"Refund for cancelled bet ticket {ticket.id} (due to voided selection in fixture {fixture.home_team} vs {fixture.away_team})",
                                related_bet_ticket=ticket,
                                timestamp=timezone.now()
                            )
                            user_wallet.apply_delta(
                                amount=ticket.stake_amount,
                                actor=request.user,
                                transaction_obj=refund_tx,
                                reference=str(ticket.ticket_id),
                                reason=refund_tx.description,
                                metadata={"ticket_id": ticket.ticket_id, "source": "ticket_cancelled"},
                            )
                            messages.info(request, f"Ticket {ticket.id} is CANCELLED (stake refunded) due to fixture {fixture.home_team} vs {fixture.away_team} resulting in a void.")
                            log_admin_activity(request, f"Ticket {ticket.id} CANCELLED and refunded due to fixture {fixture.id} void result.")
                        elif ticket_still_winning:
                            ticket.status = 'won'
                            ticket.save()

                            user_wallet = Wallet.objects.select_for_update().get(user=ticket.user)
                            payout_tx = Transaction.objects.create(
                                user=ticket.user,
                                initiating_user=request.user, 
                                target_user=ticket.user,
                                transaction_type='bet_payout',
                                amount=ticket.max_winning,
                                is_successful=True,
                                status='completed',
                                description=f"Winnings for bet ticket {ticket.id} on {fixture.home_team} vs {fixture.away_team}",
                                related_bet_ticket=ticket,
                                timestamp=timezone.now()
                            )
                            user_wallet.apply_delta(
                                amount=ticket.max_winning,
                                actor=request.user,
                                transaction_obj=payout_tx,
                                reference=str(ticket.ticket_id),
                                reason=payout_tx.description,
                                metadata={"ticket_id": ticket.ticket_id, "source": "ticket_won"},
                            )
                            messages.success(request, f"Ticket {ticket.id} is WON! Winnings of ₦{ticket.max_winning:.2f} paid to {ticket.user.email}.")
                            log_admin_activity(request, f"Declared fixture {fixture.id} as WON for ticket {ticket.id} and paid out.")
                        else:
                            ticket.status = 'lost'
                            ticket.save()
                            messages.info(request, f"Ticket {ticket.id} is LOST.")
                            log_admin_activity(request, f"Declared fixture {fixture.id} as LOST for ticket {ticket.id}.")
                    
                    ticket.save() # Save the ticket status update

                elif ticket.status == 'deleted':
                    messages.info(request, f"Ticket {ticket.id} was previously deleted and will not be processed for result.")
                # No need for else (already processed) as per previous code, as it would be handled by the update logic above.


            messages.success(request, f"Result declared for {fixture.home_team} vs {fixture.away_team} as '{fixture.get_result_display()}'. All associated tickets processed.")
            log_admin_activity(request, f"Declared result for fixture {fixture.id} ({fixture.home_team} vs {fixture.away_team}) as {fixture.result}.")
            if affected_ticket_ids:
                from commission.tasks import enqueue_refresh_weekly_commissions_for_ticket_ids
                enqueue_refresh_weekly_commissions_for_ticket_ids(affected_ticket_ids)
            return redirect('betting_admin:manage_fixtures')
        else:
            for field, errors in form.errors.items():
                for error in errors:
                    messages.error(request, f"{field.replace('_', ' ').title()}: {error}")
            if form.non_field_errors():
                for error in form.non_field_errors():
                    messages.error(request, f"Declare Result Error: {error}")
    else:
        form = DeclareResultForm(instance=fixture)
    
    context = {
        'form': form,
        'fixture': fixture,
    }
    return render(request, 'betting/admin/declare_result.html', context)


@login_required
@user_passes_test(lambda u: u.is_superuser or u.user_type == 'admin')
def withdraw_request_list(request):
    status_filter = request.GET.get('status', 'all')
    search_query = request.GET.get('q', '')

    withdrawals_queryset = UserWithdrawal.objects.all().order_by('-request_time')

    if status_filter != 'all':
        withdrawals_queryset = withdrawals_queryset.filter(status=status_filter)
    
    if search_query:
        withdrawals_queryset = withdrawals_queryset.filter(
            Q(user__email__icontains=search_query) |
            Q(bank_name__icontains=search_query) |
            Q(account_number__icontains=search_query) |
            Q(id__startswith=search_query)
        )

    paginator = Paginator(withdrawals_queryset, 10) # 10 requests per page
    page_number = request.GET.get('page')

    try:
        withdrawals = paginator.page(page_number)
    except PageNotAnInteger:
        withdrawals = paginator.page(1)
    except EmptyPage:
        withdrawals = paginator.page(paginator.num_pages)

    context = {
        'withdrawals': withdrawals,
        'status_choices': [('all', 'All')] + list(UserWithdrawal.STATUS_CHOICES),
        'current_status_filter': status_filter,
        'current_search_query': search_query,
        'withdrawal_action_form': WithdrawalActionForm() # Form for approve/reject
    }
    return render(request, 'betting/admin/withdraw_request_list.html', context)


@login_required
@user_passes_test(lambda u: u.is_superuser or u.user_type == 'admin')
@db_transaction.atomic
def approve_reject_withdrawal(request, withdrawal_id):
    withdrawal_request = get_object_or_404(UserWithdrawal.objects.select_for_update(), id=withdrawal_id)

    if withdrawal_request.status != 'pending':
        messages.warning(request, f"This withdrawal request has already been {withdrawal_request.status}.")
        return redirect('betting_admin:withdraw_request_list')

    if request.method == 'POST':
        form = WithdrawalActionForm(request.POST)
        if form.is_valid():
            action = form.cleaned_data['action']
            reason = form.cleaned_data['reason']

            if action == 'approve':
                withdrawal_request.status = 'approved'
                
                # Capture Balance Snapshot for Audit
                try:
                    user_wallet = Wallet.objects.get(user=withdrawal_request.user)
                    # Since funds are deducted at request time (pending), current balance is the 'after' state relative to the request
                    withdrawal_request.balance_after = user_wallet.balance 
                    withdrawal_request.balance_before = user_wallet.balance + withdrawal_request.amount
                except Wallet.DoesNotExist:
                    pass

                withdrawal_request.processed_ip = request.META.get('REMOTE_ADDR')
                
                messages.success(request, f"Withdrawal request {withdrawal_id} approved. Funds should be disbursed.")
                log_admin_activity(request, f"Approved withdrawal request {withdrawal_id} for user {withdrawal_request.user.email}")
            elif action == 'reject':
                withdrawal_request.status = 'rejected'
                withdrawal_request._skip_signal_refund = True # Skip signal processing since we handle it manually here
                # Refund funds to user's wallet
                user_wallet = get_object_or_404(Wallet.objects.select_for_update(), user=withdrawal_request.user)

                # Record the refund transaction
                refund_tx = Transaction.objects.create(
                    user=withdrawal_request.user,
                    initiating_user=request.user, 
                    target_user=withdrawal_request.user,
                    transaction_type='withdrawal_refund',
                    amount=withdrawal_request.amount,
                    is_successful=True,
                    status='completed',
                    description=f"Refund for rejected withdrawal request {withdrawal_id}. Reason: {reason or 'No reason provided.'}",
                    timestamp=timezone.now()
                )
                user_wallet.apply_delta(
                    amount=withdrawal_request.amount,
                    actor=request.user,
                    transaction_obj=refund_tx,
                    reference=str(withdrawal_request.id),
                    reason=refund_tx.description,
                    metadata={"withdrawal_id": withdrawal_request.id, "source": "admin_reject"},
                )
                messages.info(request, f"Withdrawal request {withdrawal_id} rejected. Funds (₦{withdrawal_request.amount:.2f}) returned to {withdrawal_request.user.email}'s wallet. Reason: {reason or 'No reason provided.'}")
                log_admin_activity(request, f"Rejected withdrawal request {withdrawal_id} for user {withdrawal_request.user.email}. Reason: {reason}")
            
            withdrawal_request.approved_rejected_by = request.user # Corrected field name
            withdrawal_request.approved_rejected_time = timezone.now() # Corrected field name
            withdrawal_request.admin_notes = reason # Corrected field name
            withdrawal_request.save()

            return redirect('betting_admin:withdraw_request_list')
        else:
            for field, errors in form.errors.items():
                for error in errors:
                    messages.error(request, f"{field.replace('_', ' ').title()}: {error}")
            if form.non_field_errors():
                for error in form.non_field_errors():
                    messages.error(request, f"Withdrawal Action Error: {error}")
    
    messages.error(request, "Invalid request for withdrawal action.")
    return redirect('betting_admin:withdraw_request_list')


@login_required
@user_passes_test(lambda u: u.is_superuser or u.user_type == 'admin')
def manage_betting_periods(request):
    periods = BettingPeriod.objects.all().order_by('-start_date')
    paginator = Paginator(periods, 10) 
    page_number = request.GET.get('page')

    try:
        betting_periods = paginator.page(page_number)
    except PageNotAnInteger:
        betting_periods = paginator.page(1)
    except EmptyPage:
        betting_periods = paginator.page(paginator.num_pages)

    context = {
        'betting_periods': betting_periods
    }
    return render(request, 'betting/admin/manage_betting_periods.html', context)


@login_required
@user_passes_test(lambda u: u.is_superuser or u.user_type == 'admin')
def add_betting_period(request):
    if request.method == 'POST':
        form = BettingPeriodForm(request.POST)
        if form.is_valid():
            period = form.save()
            messages.success(request, f"Betting period '{period.name}' added successfully.")
            log_admin_activity(request, f"Added new betting period: {period.name}")
            return redirect('betting_admin:manage_betting_periods')
        else:
            for field, errors in form.errors.items():
                for error in errors:
                    messages.error(request, f"{field.replace('_', ' ').title()}: {error}")
            if form.non_field_errors():
                for error in form.non_field_errors():
                    messages.error(request, f"Add Betting Period Error: {error}")
    else:
        form = BettingPeriodForm()
    return render(request, 'betting/admin/add_betting_period.html', {'form': form})


@login_required
@user_passes_test(lambda u: u.is_superuser or u.user_type == 'admin')
def edit_betting_period(request, period_id):
    period = get_object_or_404(BettingPeriod, id=period_id)
    if request.method == 'POST':
        form = BettingPeriodForm(request.POST, instance=period)
        if form.is_valid():
            period = form.save()
            messages.success(request, f"Betting period '{period.name}' updated successfully.")
            log_admin_activity(request, f"Edited betting period: {period.name}")
            return redirect('betting_admin:manage_betting_periods')
        else:
            for field, errors in form.errors.items():
                for error in errors:
                    messages.error(request, f"{field.replace('_', ' ').title()}: {error}")
            if form.non_field_errors():
                for error in form.non_field_errors():
                    messages.error(request, f"Edit Betting Period Error: {error}")
    else:
        form = BettingPeriodForm(instance=period)
    return render(request, 'betting/admin/edit_betting_period.html', {'form': form, 'period': period})


@login_required
@user_passes_test(lambda u: u.is_superuser or u.user_type == 'admin')
@db_transaction.atomic
def delete_betting_period(request, period_id):
    period = get_object_or_404(BettingPeriod, id=period_id)
    period_name = period.name

    if Fixture.objects.filter(betting_period=period).exclude(status__in=['settled', 'cancelled', 'deleted']).exists(): # Only allow deletion if no unsettled or active fixtures
        messages.error(request, f"Cannot delete betting period '{period_name}'. There are active or unsettled fixtures associated with it.")
        return redirect('betting_admin:manage_betting_periods')
    
    if request.method == 'POST':
        try:
            period.delete()
            messages.success(request, f"Betting period '{period_name}' deleted successfully.")
            log_admin_activity(request, f"Deleted betting period: {period_name}")
            return redirect('betting_admin:manage_betting_periods')
        except Exception as e:
            messages.error(request, f"An error occurred while deleting betting period '{period_name}': {e}")
            log_admin_activity(request, f"Failed to delete betting period: {period_name} - Error: {e}")
            return redirect('betting_admin:manage_betting_periods')

    messages.error(request, "Invalid request for betting period deletion.")
    return redirect('betting_admin:manage_betting_periods')


@login_required
@user_passes_test(lambda u: u.is_superuser or u.user_type == 'admin')
def manage_agent_payouts(request):
    status_filter = request.GET.get('status', 'all')
    agent_filter = request.GET.get('agent', 'all')
    start_date_str = request.GET.get('start_date')
    end_date_str = request.GET.get('end_date')

    payouts_queryset = AgentPayout.objects.all()

    if status_filter != 'all':
        payouts_queryset = payouts_queryset.filter(status=status_filter)
    
    if agent_filter != 'all':
        try:
            agent_id = int(agent_filter)
            payouts_queryset = payouts_queryset.filter(agent__id=agent_id)
        except (ValueError, TypeError):
            pass

    # Corrected filter for payout_date (model has 'created_at' and 'settled_at' but not 'payout_date')
    # Assuming filtering should be by 'created_at' for the payout request creation date
    if start_date_str:
        try:
            start_date = date.fromisoformat(start_date_str)
            payouts_queryset = payouts_queryset.filter(created_at__date__gte=start_date)
        except ValueError:
            messages.error(request, "Invalid start date format.")
    if end_date_str:
        try:
            end_date = date.fromisoformat(end_date_str)
            payouts_queryset = payouts_queryset.filter(created_at__date__lte=end_date)
        except ValueError:
            messages.error(request, "Invalid end date format.")

    paginator = Paginator(payouts_queryset.order_by('-created_at'), 10) # Order by created_at
    page_number = request.GET.get('page')

    try:
        agent_payouts = paginator.page(page_number)
    except PageNotAnInteger:
        agent_payouts = paginator.page(1)
   
    except EmptyPage:
        agent_payouts = paginator.page(paginator.num_pages)

    context = {
        'agent_payouts': agent_payouts,
        'status_choices': [('all', 'All')] + list(AgentPayout.STATUS_CHOICES),
        'current_status_filter': status_filter,
        'all_agents': User.objects.filter(user_type__in=['agent', 'super_agent', 'master_agent']).order_by('email'),
        'current_agent_filter': agent_filter,
        'start_date_filter': start_date_str,
        'end_date_filter': end_date_str,
    }
    return render(request, 'betting/admin/manage_agent_payouts.html', context)


@login_required
@user_passes_test(lambda u: u.is_superuser or u.user_type == 'admin')
@db_transaction.atomic
def mark_payout_settled(request, payout_id):
    payout = get_object_or_404(AgentPayout, id=payout_id)

    if payout.status != 'pending':
        messages.warning(request, f"Payout {payout.id} is already '{payout.status}'.")
        return redirect('betting_admin:manage_agent_payouts')

    if request.method == 'POST':
        payout.status = 'settled'
        payout.settled_by = request.user # Corrected field name
        payout.settled_at = timezone.now() # Corrected field name
        payout.save()
        messages.success(request, f"Agent payout {payout.id} marked as settled.")
        log_admin_activity(request, f"Marked agent payout {payout.id} for {payout.agent.email} as settled.")
        return redirect('betting_admin:manage_agent_payouts')
    
    messages.error(request, "Invalid request for marking payout settled.")
    return redirect('betting_admin:manage_agent_payouts')


@login_required
@user_passes_test(lambda u: u.is_superuser or u.user_type == 'admin')
def admin_ticket_report(request):
    status_filter = request.GET.get('status', 'all')
    user_filter = request.GET.get('user', 'all')
    start_date_str = request.GET.get('start_date')
    end_date_str = request.GET.get('end_date')

    bet_tickets_queryset = BetTicket.objects.all()

    if status_filter != 'all':
        bet_tickets_queryset = bet_tickets_queryset.filter(status=status_filter)
    
    if user_filter != 'all':
        try:
            user_id = int(user_filter)
            bet_tickets_queryset = bet_tickets_queryset.filter(user__id=user_id)
        except (ValueError, TypeError):
            pass

    if start_date_str:
        try:
            start_date = date.fromisoformat(start_date_str)
            bet_tickets_queryset = bet_tickets_queryset.filter(placed_at__date__gte=start_date)
        except ValueError:
            messages.error(request, "Invalid start date format.")
    if end_date_str:
        try:
            end_date = date.fromisoformat(end_date_str)
            bet_tickets_queryset = bet_tickets_queryset.filter(placed_at__date__lte=end_date)
        except ValueError:
            messages.error(request, "Invalid end date format.")
    
    paginator = Paginator(bet_tickets_queryset.order_by('-placed_at'), 10)
    page_number = request.GET.get('page')

    try:
        bet_tickets = paginator.page(page_number)
    except PageNotAnInteger:
        bet_tickets = paginator.page(1)
    except EmptyPage:
        bet_tickets = paginator.page(paginator.num_pages)

    context = {
        'bet_tickets': bet_tickets,
        'status_choices': [('all', 'All')] + list(BetTicket.STATUS_CHOICES),
        'current_status_filter': status_filter,
        'all_users': User.objects.all().order_by('email'), # For user filter dropdown
        'current_user_filter': user_filter,
        'start_date_filter': start_date_str,
        'end_date_filter': end_date_str,
    }
    return render(request, 'betting/admin/ticket_report.html', context)


@login_required
@user_passes_test(lambda u: u.is_superuser or u.user_type == 'admin')
def admin_reconciliation_dashboard(request):
    gateway = (request.GET.get("gateway") or "all").strip().lower()
    if gateway not in {"all", "paystack", "kora", "monnify"}:
        gateway = "all"

    if request.method == "POST":
        action = (request.POST.get("action") or "").strip().lower()
        post_gateway = (request.POST.get("gateway") or gateway or "all").strip().lower()
        if post_gateway not in {"all", "paystack", "kora", "monnify"}:
            post_gateway = "all"

        if action == "reconcile_now":
            try:
                from betting.tasks import reconcile_recent_deposits
                result = reconcile_recent_deposits.delay(
                    gateway=post_gateway,
                    minutes=1440,
                    limit=200,
                    stuck_minutes=30,
                    alert_cooldown_minutes=360,
                )
                messages.success(request, f"Reconciliation started (gateway={post_gateway}). Task ID: {result.id}")
            except Exception as e:
                messages.error(request, f"Unable to start reconciliation via Celery: {e}")
            return redirect(f"{reverse('betting_admin:admin_reconciliation_dashboard')}?gateway={post_gateway}")

    deposits_qs = Transaction.objects.filter(transaction_type="deposit").select_related("user").order_by("timestamp")
    pending_qs = deposits_qs.filter(status="pending", is_successful=False)
    failed_qs = deposits_qs.filter(status="failed", is_successful=False)

    if gateway != "all":
        pending_qs = pending_qs.filter(payment_gateway=gateway)
        failed_qs = failed_qs.filter(payment_gateway=gateway)

    now = timezone.now()
    stuck_cutoff = now - timedelta(minutes=30)
    pending_total = pending_qs.count()
    failed_total = failed_qs.count()
    pending_stuck_total = pending_qs.filter(timestamp__lte=stuck_cutoff).count()
    oldest_pending = pending_qs.first()

    pending_rows = list(pending_qs.order_by("timestamp")[:200])
    failed_rows = list(failed_qs.order_by("-timestamp")[:200])

    last_webhook = dict(
        PaymentGatewayEventLog.objects.filter(event_type="webhook")
        .values("gateway")
        .annotate(last=Max("created_at"))
        .values_list("gateway", "last")
    )
    last_reconcile = dict(
        PaymentGatewayEventLog.objects.filter(event_type="reconcile")
        .values("gateway")
        .annotate(last=Max("created_at"))
        .values_list("gateway", "last")
    )

    context = {
        "gateway": gateway,
        "pending_total": pending_total,
        "failed_total": failed_total,
        "pending_stuck_total": pending_stuck_total,
        "oldest_pending": oldest_pending,
        "pending_rows": pending_rows,
        "failed_rows": failed_rows,
        "last_webhook": last_webhook,
        "last_reconcile": last_reconcile,
        "now": now,
    }
    return render(request, "betting/admin/reconciliation_dashboard.html", context)


def admin_reconciled_credits_dashboard(request):
    gateway = (request.GET.get("gateway") or "all").strip().lower()
    if gateway not in {"all", "paystack", "kora", "monnify"}:
        gateway = "all"

    q = (request.GET.get("q") or "").strip()
    start_date_str = (request.GET.get("start_date") or "").strip()
    end_date_str = (request.GET.get("end_date") or "").strip()

    def _parse_input_date(value):
        if not value:
            return None
        try:
            return datetime.strptime(value, "%Y-%m-%d").date()
        except Exception:
            return None

    start_date = _parse_input_date(start_date_str)
    end_date = _parse_input_date(end_date_str)
    start_dt = timezone.make_aware(datetime.combine(start_date, datetime.min.time())) if start_date else None
    end_dt = timezone.make_aware(datetime.combine(end_date, datetime.max.time())) if end_date else None

    credits_qs = (
        WalletLedgerEntry.objects.filter(direction="credit")
        .filter(Q(metadata__source="reconcile") | Q(reason__icontains="(reconcile)"))
        .select_related("user", "transaction", "wallet")
        .order_by("-created_at")
    )

    if gateway != "all":
        credits_qs = credits_qs.filter(
            Q(metadata__gateway=gateway) |
            Q(reason__icontains=f"Deposit via {gateway}")
        )

    if q:
        credits_qs = credits_qs.filter(
            Q(user__email__icontains=q) |
            Q(user__username__icontains=q) |
            Q(reference__icontains=q) |
            Q(reason__icontains=q) |
            Q(transaction__external_reference__icontains=q) |
            Q(transaction__paystack_reference__icontains=q)
        )

    if start_dt:
        credits_qs = credits_qs.filter(created_at__gte=start_dt)
    if end_dt:
        credits_qs = credits_qs.filter(created_at__lte=end_dt)

    summary = credits_qs.aggregate(
        total_count=Count("id"),
        total_amount=Coalesce(Sum("amount"), Decimal("0.00")),
    )
    credits_page = Paginator(credits_qs, 50).get_page(request.GET.get("page") or 1)

    context = {
        "gateway": gateway,
        "q": q,
        "start_date": start_date_str,
        "end_date": end_date_str,
        "summary": summary,
        "credits_page": credits_page,
    }
    return render(request, "betting/admin/reconciled_credits_dashboard.html", context)


@login_required
@user_passes_test(lambda u: u.is_superuser or u.user_type == 'admin')
def admin_loan_overdraft_center(request):
    tab = (request.GET.get("tab") or "pending").strip().lower()
    if tab not in {"pending", "approved", "rejected", "outstanding", "overdue", "locked", "settled"}:
        tab = "pending"

    export_format = (request.GET.get("format") or "").strip().lower()
    q = (request.GET.get("q") or "").strip()
    borrower_role = (request.GET.get("borrower_role") or "").strip().lower()
    loan_type = (request.GET.get("loan_type") or "").strip().lower()
    lock_state = (request.GET.get("lock_state") or "").strip().lower()
    page_number = request.GET.get("page") or 1

    redirect_params = {"tab": tab}
    if q:
        redirect_params["q"] = q
    if borrower_role:
        redirect_params["borrower_role"] = borrower_role
    if loan_type:
        redirect_params["loan_type"] = loan_type
    if lock_state:
        redirect_params["lock_state"] = lock_state
    if page_number:
        redirect_params["page"] = page_number

    if request.method == "POST":
        if "loan_decision_submit" in request.POST:
            decision_form = LoanCenterDecisionForm(request.POST)
            funding_form = AdminOverdraftWalletFundingForm()
            override_unlock_form = LoanOverrideUnlockForm()
            if decision_form.is_valid():
                loan_id = decision_form.cleaned_data["loan_id"]
                action = decision_form.cleaned_data["action"]
                reason = decision_form.cleaned_data.get("reason") or ""
                try:
                    if action == "approve":
                        approve_loan_request(actor=request.user, loan_id=loan_id, ip_address=get_client_ip(request))
                        log_admin_activity(
                            request,
                            f"Approved overdraft request #{loan_id} from loan center.",
                            action_type="LOAN_APPROVED",
                            affected_object=f"loan:{loan_id}",
                        )
                        messages.success(request, f"Loan request #{loan_id} approved successfully.")
                    else:
                        reject_loan_request(
                            actor=request.user,
                            loan_id=loan_id,
                            reason=reason,
                            ip_address=get_client_ip(request),
                        )
                        log_admin_activity(
                            request,
                            f"Rejected overdraft request #{loan_id} from loan center. Reason: {reason}",
                            action_type="LOAN_REJECTED",
                            affected_object=f"loan:{loan_id}",
                        )
                        messages.success(request, f"Loan request #{loan_id} rejected.")
                except LoanOverdraftError as exc:
                    messages.error(request, str(exc))
            else:
                for error in decision_form.non_field_errors():
                    messages.error(request, error)
        elif "fund_wallet_submit" in request.POST:
            funding_form = AdminOverdraftWalletFundingForm(request.POST)
            decision_form = LoanCenterDecisionForm()
            override_unlock_form = LoanOverrideUnlockForm()
            if funding_form.is_valid():
                super_agent = funding_form.cleaned_data["super_agent"]
                amount = funding_form.cleaned_data["amount"]
                reason = funding_form.cleaned_data.get("reason") or ""
                try:
                    wallet, before, after = fund_overdraft_wallet(
                        super_agent=super_agent,
                        amount=amount,
                        actor=request.user,
                        reason=reason,
                        ip_address=get_client_ip(request),
                    )
                    log_admin_activity(
                        request,
                        f"Funded overdraft wallet for {super_agent.username or super_agent.email} with ₦{amount}. "
                        f"Balance moved from ₦{before} to ₦{after}.",
                        action_type="OVERDRAFT_WALLET_FUNDED",
                        affected_object=f"overdraft_wallet:{wallet.id}",
                    )
                    messages.success(
                        request,
                        f"Overdraft wallet for {super_agent.username or super_agent.email} funded with ₦{amount}. "
                        f"New balance: ₦{after}."
                    )
                except LoanOverdraftError as exc:
                    messages.error(request, str(exc))
            else:
                for field_errors in funding_form.errors.values():
                    for error in field_errors:
                        messages.error(request, error)
        elif "override_unlock_submit" in request.POST:
            decision_form = LoanCenterDecisionForm()
            funding_form = AdminOverdraftWalletFundingForm()
            override_unlock_form = LoanOverrideUnlockForm(request.POST)
            if not request.user.is_superuser:
                messages.error(request, "Only Super Admin can override unlock overdue overdraft accounts without payment.")
            elif override_unlock_form.is_valid():
                loan_id = override_unlock_form.cleaned_data["loan_id"]
                reason = override_unlock_form.cleaned_data["reason"]
                try:
                    loan, unlocked_targets = override_unlock_loan_without_payment(
                        actor=request.user,
                        loan_id=loan_id,
                        reason=reason,
                        ip_address=get_client_ip(request),
                    )
                    unlocked_labels = ", ".join(
                        target.username or target.email or f"user#{target.id}"
                        for target in unlocked_targets
                    ) or "No currently locked targets"
                    log_admin_activity(
                        request,
                        f"Applied overdraft override unlock without payment for loan #{loan_id}. "
                        f"Unlocked: {unlocked_labels}. Reason: {reason}",
                        action_type="LOAN_OVERRIDE_UNLOCK",
                        affected_object=f"loan:{loan_id}",
                    )
                    messages.success(
                        request,
                        f"Override unlock applied for loan #{loan_id}. Outstanding balance remains unpaid."
                    )
                except LoanOverdraftError as exc:
                    messages.error(request, str(exc))
            else:
                for field_errors in override_unlock_form.errors.values():
                    for error in field_errors:
                        messages.error(request, error)
        elif "relock_submit" in request.POST:
            decision_form = LoanCenterDecisionForm()
            funding_form = AdminOverdraftWalletFundingForm()
            override_unlock_form = LoanOverrideUnlockForm()
            relock_form = LoanOverrideRelockForm(request.POST)
            if not request.user.is_superuser:
                messages.error(request, "Only Super Admin can re-lock overdue overdraft accounts after override unlock.")
            elif relock_form.is_valid():
                loan_id = relock_form.cleaned_data["loan_id"]
                reason = relock_form.cleaned_data["reason"]
                try:
                    loan, relocked_targets = relock_loan_after_override(
                        actor=request.user,
                        loan_id=loan_id,
                        reason=reason,
                        ip_address=get_client_ip(request),
                    )
                    relocked_labels = ", ".join(
                        target.username or target.email or f"user#{target.id}"
                        for target in relocked_targets
                    ) or "No targets"
                    log_admin_activity(
                        request,
                        f"Re-locked overdraft loan #{loan_id} after override unlock. "
                        f"Locked: {relocked_labels}. Reason: {reason}",
                        action_type="LOAN_OVERRIDE_RELOCK",
                        affected_object=f"loan:{loan_id}",
                    )
                    messages.success(
                        request,
                        f"Loan #{loan_id} has been re-locked and the override unlock has been reversed."
                    )
                except LoanOverdraftError as exc:
                    messages.error(request, str(exc))
            else:
                for field_errors in relock_form.errors.values():
                    for error in field_errors:
                        messages.error(request, error)
        elif "run_due_enforcement" in request.POST:
            decision_form = LoanCenterDecisionForm()
            funding_form = AdminOverdraftWalletFundingForm()
            override_unlock_form = LoanOverrideUnlockForm()
            processed_loans = enforce_due_loans()
            log_admin_activity(
                request,
                f"Executed overdue loan enforcement from loan center. Processed {processed_loans} loan(s).",
                action_type="LOAN_ENFORCEMENT_RUN",
                affected_object=f"processed:{processed_loans}",
            )
            messages.success(request, f"Due enforcement completed. Processed {processed_loans} overdue loan(s).")
        else:
            decision_form = LoanCenterDecisionForm()
            funding_form = AdminOverdraftWalletFundingForm()
            override_unlock_form = LoanOverrideUnlockForm()
        return redirect(f"{reverse('betting_admin:admin_loan_overdraft_center')}?{urlencode(redirect_params)}")

    decision_form = LoanCenterDecisionForm()
    funding_form = AdminOverdraftWalletFundingForm()
    override_unlock_form = LoanOverrideUnlockForm()

    loans = Loan.objects.select_related(
        "borrower",
        "lender",
        "approved_by",
        "rejected_by",
        "overdraft_wallet",
    ).order_by("-created_at")
    now = timezone.now()

    if tab == "pending":
        loans = loans.filter(status="pending")
    elif tab == "approved":
        loans = loans.filter(approved_at__isnull=False).exclude(status="rejected")
    elif tab == "rejected":
        loans = loans.filter(status="rejected")
    elif tab == "outstanding":
        loans = loans.filter(status__in=["active", "overdue", "defaulted"], outstanding_balance__gt=Decimal("0.00"))
    elif tab == "overdue":
        loans = loans.filter(status__in=["active", "overdue", "defaulted"], outstanding_balance__gt=Decimal("0.00"), due_date__lt=now)
    elif tab == "locked":
        loans = loans.filter(account_locked_due_to_default=True)
    elif tab == "settled":
        loans = loans.filter(status="settled")

    if q:
        loans = loans.filter(
            Q(borrower__email__icontains=q)
            | Q(borrower__username__icontains=q)
            | Q(lender__email__icontains=q)
            | Q(lender__username__icontains=q)
            | Q(request_reason__icontains=q)
            | Q(rejection_reason__icontains=q)
        )

    if borrower_role:
        loans = loans.filter(borrower__user_type=borrower_role)

    if loan_type:
        loans = loans.filter(loan_type=loan_type)

    if lock_state == "locked":
        loans = loans.filter(borrower__is_locked=True)
    elif lock_state == "unlocked":
        loans = loans.filter(borrower__is_locked=False)

    loan_ids_for_summary = list(loans.values_list("id", flat=True))
    summary_base = Loan.objects.filter(id__in=loan_ids_for_summary)
    summary = summary_base.aggregate(
        total_requested=Coalesce(Sum("requested_amount"), Decimal("0.00")),
        total_approved=Coalesce(Sum("amount"), Decimal("0.00")),
        total_outstanding=Coalesce(Sum("outstanding_balance"), Decimal("0.00")),
    )
    summary["total_records"] = len(loan_ids_for_summary)
    summary["overdue_balance"] = (
        summary_base.filter(outstanding_balance__gt=Decimal("0.00"), due_date__lt=now).aggregate(
            total=Coalesce(Sum("outstanding_balance"), Decimal("0.00"))
        )["total"]
        or Decimal("0.00")
    )
    summary["locked_count"] = User.objects.filter(
        id__in=summary_base.values_list("borrower_id", flat=True),
        is_locked=True,
    ).count()

    if export_format in {"csv", "xlsx", "pdf"}:
        rows = []
        for loan in loans:
            days_overdue = 0
            if loan.due_date and loan.due_date < now and loan.outstanding_balance > 0:
                days_overdue = (timezone.localtime(now).date() - timezone.localtime(loan.due_date).date()).days
            rows.append(
                {
                    "loan_id": loan.id,
                    "agent": loan.borrower.username or loan.borrower.email or "",
                    "super_agent": loan.lender.username or loan.lender.email or "",
                    "role": loan.borrower.get_user_type_display(),
                    "ticket_count": loan.qualification_ticket_count,
                    "deposit_volume": loan.qualification_deposit_volume,
                    "qualified_amount": loan.qualified_amount,
                    "requested_amount": loan.requested_amount,
                    "approved_amount": loan.amount,
                    "outstanding_balance": loan.outstanding_balance,
                    "due_date": timezone.localtime(loan.due_date).strftime("%Y-%m-%d %H:%M:%S") if loan.due_date else "",
                    "status": loan.get_status_display(),
                    "withdrawal_disabled": "Yes" if loan.outstanding_balance > 0 else "No",
                    "account_locked": "Yes" if getattr(loan.borrower, "is_locked", False) else "No",
                    "days_overdue": days_overdue,
                }
            )
        return _export_simple_rows(rows=rows, title=f"loan_overdraft_{tab}", fmt=export_format)

    loans_page = Paginator(loans, 50).get_page(request.GET.get("page") or 1)
    for loan in loans_page.object_list:
        override_meta = loan_lock_override_details(loan)
        loan.lock_override_active = override_meta["active"]
        loan.lock_override_reason = override_meta["reason"]
        loan.lock_override_by_label = override_meta["performed_by_label"]
        loan.lock_override_at = override_meta["performed_at"]
        loan.can_override_unlock = bool(
            request.user.is_superuser
            and loan.outstanding_balance > Decimal("0.00")
            and (
                loan.account_locked_due_to_default
                or getattr(loan.borrower, "is_locked", False)
            )
            and not loan.lock_override_active
        )
        loan.can_relock_after_override = bool(
            request.user.is_superuser
            and loan.outstanding_balance > Decimal("0.00")
            and loan.lock_override_active
        )
    tab_counts = {
        "pending": Loan.objects.filter(status="pending").count(),
        "approved": Loan.objects.filter(approved_at__isnull=False).exclude(status="rejected").count(),
        "rejected": Loan.objects.filter(status="rejected").count(),
        "outstanding": Loan.objects.filter(status__in=["active", "overdue", "defaulted"], outstanding_balance__gt=Decimal("0.00")).count(),
        "overdue": Loan.objects.filter(status__in=["active", "overdue", "defaulted"], outstanding_balance__gt=Decimal("0.00"), due_date__lt=now).count(),
        "locked": Loan.objects.filter(account_locked_due_to_default=True).count(),
        "settled": Loan.objects.filter(status="settled").count(),
    }

    overdraft_wallets = list(
        OverdraftWallet.objects.select_related("super_agent").order_by("-updated_at")[:10]
    )

    context = {
        "tab": tab,
        "q": q,
        "borrower_role": borrower_role,
        "loan_type": loan_type,
        "lock_state": lock_state,
        "tab_counts": tab_counts,
        "loans_page": loans_page,
        "now": now,
        "decision_form": decision_form,
        "funding_form": funding_form,
        "override_unlock_form": override_unlock_form,
        "overdraft_wallets": overdraft_wallets,
        "summary": summary,
        "is_superadmin": request.user.is_superuser,
        "borrower_role_choices": [
            ("agent", "Agent"),
            ("super_agent", "Super Agent"),
        ],
        "loan_type_choices": [
            ("agent_overdraft", "Agent Overdraft"),
            ("super_agent_overdraft", "Super Agent Overdraft"),
            ("manual_overdraft", "Manual Overdraft"),
        ],
    }
    return render(request, "betting/admin/loan_overdraft_center.html", context)


@login_required
@user_passes_test(lambda u: u.is_superuser or u.user_type == 'admin')
def admin_celery_health(request):
    beat_ok = False
    results_ok = False
    periodic_tasks = []
    task_results = []
    worker_ping = None
    inspect_counts = {"active": None, "reserved": None, "scheduled": None}
    inspect_error = None

    try:
        from django_celery_beat.models import PeriodicTask
        beat_ok = True
        periodic_tasks = list(
            PeriodicTask.objects.filter(task__in=["betting.tasks.reconcile_recent_deposits", "betting.tasks.backup_database"])
            .order_by("name")
        )
    except Exception:
        beat_ok = False

    try:
        from django_celery_results.models import TaskResult
        results_ok = True
        task_results = list(
            TaskResult.objects.filter(task_name__in=["betting.tasks.reconcile_recent_deposits", "betting.tasks.backup_database"])
            .order_by("-date_done")[:20]
        )
    except Exception:
        results_ok = False

    try:
        from celery import current_app
        insp = current_app.control.inspect(timeout=1)
        worker_ping = insp.ping()
        active = insp.active() or {}
        reserved = insp.reserved() or {}
        scheduled = insp.scheduled() or {}
        inspect_counts["active"] = sum(len(v or []) for v in active.values())
        inspect_counts["reserved"] = sum(len(v or []) for v in reserved.values())
        inspect_counts["scheduled"] = sum(len(v or []) for v in scheduled.values())
    except Exception as e:
        inspect_error = str(e)

    context = {
        "beat_ok": beat_ok,
        "results_ok": results_ok,
        "periodic_tasks": periodic_tasks,
        "task_results": task_results,
        "worker_ping": worker_ping,
        "inspect_counts": inspect_counts,
        "inspect_error": inspect_error,
    }
    return render(request, "betting/admin/celery_health.html", context)



@login_required
@user_passes_test(lambda u: u.is_superuser or u.user_type == 'admin')
def admin_tickets_by_event_report(request):
    betting_period_id = (request.GET.get("betting_period") or "").strip()
    fixture_id = (request.GET.get("fixture") or "").strip()

    periods = BettingPeriod.objects.all().order_by("-start_date")
    fixtures = Fixture.objects.none()
    selected_period = None
    selected_fixture = None
    tickets_qs = BetTicket.objects.none()

    if betting_period_id:
        try:
            selected_period = BettingPeriod.objects.get(pk=int(betting_period_id))
            fixtures = (
                Fixture.objects.filter(betting_period=selected_period)
                .order_by("serial_number", "match_date", "match_time", "home_team", "away_team")
            )
        except Exception:
            selected_period = None
            fixtures = Fixture.objects.none()

    if fixture_id and selected_period:
        try:
            selected_fixture = Fixture.objects.get(pk=int(fixture_id), betting_period=selected_period)
        except Exception:
            selected_fixture = None

    if selected_fixture:
        serial = str(getattr(selected_fixture, "serial_number", "") or "").strip()
        tickets_qs = (
            BetTicket.objects.filter(
                Q(selections__fixture_id=selected_fixture.id)
                | Q(
                    selections__fixture__isnull=True,
                    selections__betting_period_id=selected_fixture.betting_period_id,
                    selections__fixture_serial_number__iexact=serial,
                )
            )
            .select_related("user")
            .distinct()
            .order_by("-placed_at")
        )

    paginator = Paginator(tickets_qs, 50)
    page_number = request.GET.get("page")
    try:
        tickets = paginator.page(page_number)
    except PageNotAnInteger:
        tickets = paginator.page(1)
    except EmptyPage:
        tickets = paginator.page(paginator.num_pages)

    context = {
        "periods": periods,
        "fixtures": fixtures,
        "tickets": tickets,
        "selected_betting_period": str(getattr(selected_period, "id", "") or ""),
        "selected_fixture": str(getattr(selected_fixture, "id", "") or ""),
    }
    return render(request, "betting/admin/tickets_by_event_report.html", context)


@login_required
@user_passes_test(lambda u: u.is_superuser or u.user_type == 'admin')
def admin_limit_rejections_report(request):
    agent_query = (request.GET.get('agent') or '').strip()
    user_query = (request.GET.get('user') or '').strip()
    start_date_str = request.GET.get('start_date')
    end_date_str = request.GET.get('end_date')

    qs = (
        BettingLimitAuditLog.objects
        .filter(action_type='TICKET_REJECTED')
        .select_related('actor', 'agent', 'affected_user', 'ticket')
        .order_by('-created_at')
    )

    if agent_query:
        qs = qs.filter(
            Q(agent__email__icontains=agent_query)
            | Q(agent__username__icontains=agent_query)
            | Q(agent__first_name__icontains=agent_query)
            | Q(agent__last_name__icontains=agent_query)
            | Q(agent__phone_number__icontains=agent_query)
        )

    if user_query:
        qs = qs.filter(
            Q(affected_user__email__icontains=user_query)
            | Q(affected_user__username__icontains=user_query)
            | Q(affected_user__first_name__icontains=user_query)
            | Q(affected_user__last_name__icontains=user_query)
            | Q(affected_user__phone_number__icontains=user_query)
        )

    if start_date_str:
        try:
            start_date = date.fromisoformat(start_date_str)
            qs = qs.filter(created_at__date__gte=start_date)
        except ValueError:
            messages.error(request, "Invalid start date format.")

    if end_date_str:
        try:
            end_date = date.fromisoformat(end_date_str)
            qs = qs.filter(created_at__date__lte=end_date)
        except ValueError:
            messages.error(request, "Invalid end date format.")

    total_rejections = qs.count()

    paginator = Paginator(qs, 50)
    page_number = request.GET.get('page')
    try:
        logs = paginator.page(page_number)
    except PageNotAnInteger:
        logs = paginator.page(1)
    except EmptyPage:
        logs = paginator.page(paginator.num_pages)

    context = {
        'report_title': "Tickets Rejected Due To Limits",
        'logs': logs,
        'total_rejections': total_rejections,
        'agent_filter': agent_query,
        'user_filter': user_query,
        'start_date_filter': start_date_str,
        'end_date_filter': end_date_str,
    }
    return render(request, 'betting/admin/betting_limit_rejections_report.html', context)


@login_required
@user_passes_test(lambda u: u.is_superuser or u.user_type == 'admin')
def admin_ticket_details(request, ticket_id):
    ticket = get_object_or_404(BetTicket, id=ticket_id)
    return render(request, 'betting/admin/ticket_detail.html', {'bet_ticket': ticket})


@login_required
@user_passes_test(lambda u: u.is_superuser or u.user_type == 'admin')
@db_transaction.atomic
def admin_void_ticket_single(request, ticket_id):
    ticket = get_object_or_404(BetTicket, id=ticket_id)

    if request.method == 'POST':
        if ticket.status in ['won', 'lost', 'cashed_out', 'deleted', 'cancelled']:
            messages.warning(request, f"Ticket {ticket.ticket_id} is already '{ticket.status}' and cannot be voided/deleted.")
            return redirect('betting_admin:admin_ticket_report') 
        
        try:
            ticket.status = 'deleted'
            ticket.deleted_by = request.user
            ticket.deleted_at = timezone.now()
            ticket.save()
            from commission.tasks import enqueue_refresh_weekly_commissions_for_ticket_ids
            enqueue_refresh_weekly_commissions_for_ticket_ids([str(ticket.id)])

            user_wallet = Wallet.objects.select_for_update().get(user=ticket.user)
            refund_tx = Transaction.objects.create(
                user=ticket.user,
                initiating_user=request.user,
                target_user=ticket.user,
                transaction_type='ticket_deletion_refund',
                amount=ticket.stake_amount,
                is_successful=True,
                status='completed',
                description=f"Admin void: Stake refunded for ticket {ticket.ticket_id}",
                related_bet_ticket=ticket,
                timestamp=timezone.now()
            )
            user_wallet.apply_delta(
                amount=ticket.stake_amount,
                actor=request.user,
                transaction_obj=refund_tx,
                reference=str(ticket.ticket_id),
                reason=refund_tx.description,
                metadata={"ticket_id": ticket.ticket_id, "source": "admin_void"},
            )
            messages.success(request, f"Bet ticket {ticket.ticket_id} successfully voided/deleted and stake refunded to {ticket.user.email}.")
            log_admin_activity(request, f"Voided/Deleted bet ticket {ticket.ticket_id} and refunded stake.")
        except Exception as e:
            messages.error(request, f"Failed to void ticket {ticket.ticket_id}: {e}")
            log_admin_activity(request, f"Failed to void ticket {ticket.ticket_id}. Error: {e}")
        
    return redirect('betting_admin:admin_ticket_report') 

@login_required
@user_passes_test(lambda u: u.is_superuser or u.user_type == 'admin')
@db_transaction.atomic
def admin_settle_won_ticket_single(request, ticket_id):
    ticket = get_object_or_404(BetTicket, id=ticket_id)

    if request.method == 'POST':
        if ticket.status != 'pending':
            messages.warning(request, f"Ticket {ticket.ticket_id} is already '{ticket.status}' and cannot be manually settled as won.")
            return redirect('betting_admin:admin_ticket_report') 

        try:
            ticket.status = 'won'
            ticket.save()

            user_wallet = Wallet.objects.select_for_update().get(user=ticket.user)
            winnings_amount = ticket.max_winning
            payout_tx = Transaction.objects.create(
                user=ticket.user,
                initiating_user=request.user,
                target_user=ticket.user,
                transaction_type='bet_payout',
                amount=winnings_amount,
                is_successful=True,
                status='completed',
                description=f"Admin payout: Winnings for Bet Ticket {ticket.ticket_id}",
                related_bet_ticket=ticket,
                timestamp=timezone.now()
            )
            user_wallet.apply_delta(
                amount=winnings_amount,
                actor=request.user,
                transaction_obj=payout_tx,
                reference=str(ticket.ticket_id),
                reason=payout_tx.description,
                metadata={"ticket_id": ticket.ticket_id, "source": "admin_settle_won"},
            )
            messages.success(request, f"Bet ticket {ticket.ticket_id} settled as WON and winnings of ₦{winnings_amount:.2f} paid to {ticket.user.email}.")
            log_admin_activity(request, f"Settled bet ticket {ticket.ticket_id} as WON and paid out winnings.")
        except Exception as e:
            messages.error(request, f"Failed to settle ticket {ticket.ticket_id} as WON: {e}")
            log_admin_activity(request, f"Failed to settle ticket {ticket.ticket_id} as WON. Error: {e}")
        
    return redirect('betting_admin:admin_ticket_report') 


@login_required
@user_passes_test(lambda u: u.is_superuser or u.user_type == 'admin')
def admin_wallet_report(request):
    report_title = "Admin Wallet Report"
    user_filter = request.GET.get('user', 'all')

    wallets_queryset = Wallet.objects.all()
    selected_user = None

    if user_filter != 'all':
        try:
            filter_user_id = int(user_filter)
            wallets_queryset = wallets_queryset.filter(user__id=filter_user_id)
            selected_user = User.objects.filter(id=filter_user_id).first()
        except (ValueError, TypeError):
            pass 

    paginator = Paginator(wallets_queryset.order_by('user__email'), 10)
    page_number = request.GET.get('page')

    try:
        wallets = paginator.page(page_number)
    except PageNotAnInteger:
        wallets = paginator.page(1)
    except EmptyPage:
        wallets = paginator.page(paginator.num_pages)

    context = {
        'report_title': report_title,
        'wallets': wallets,
        'current_user_filter': user_filter,
        'selected_user': selected_user,
    }
    return render(request, 'betting/admin/wallet_report.html', context)


@login_required
@user_passes_test(lambda u: u.is_superuser or u.user_type == 'admin')
def admin_sales_winnings_report(request):
    report_title = "Admin Sales & Winnings Report"
    user_filter = request.GET.get('user', 'all')
    start_date_str = request.GET.get('start_date')
    end_date_str = request.GET.get('end_date')

    end_date = date.fromisoformat(end_date_str) if end_date_str else timezone.now().date()
    start_date = date.fromisoformat(start_date_str) if start_date_str else end_date - timedelta(days=30)

    users_qs = User.objects.all()

    if user_filter != 'all':
        try:
            filter_user_id = int(user_filter)
            users_qs = users_qs.filter(id=filter_user_id)
        except (ValueError, TypeError):
            pass 

    report_data = []
    for u in users_qs.order_by('email'):
        # CORRECTED: Changed 'betticket__' to 'bet_tickets__'
        bets_by_user = BetTicket.objects.filter(
            user=u,
            placed_at__date__gte=start_date,
            placed_at__date__lt=end_date + timedelta(days=1) 
        ).exclude(status='deleted')

        total_stake = bets_by_user.aggregate(Sum('stake_amount'))['stake_amount__sum'] or Decimal('0.00')
        total_winnings = bets_by_user.filter(status='won').aggregate(Sum('potential_winning'))['potential_winning__sum'] or Decimal('0.00')
        
        net_result = total_stake - total_winnings

        if total_stake > Decimal('0.00') or total_winnings > Decimal('0.00'): 
            report_data.append({
                'user': u,
                'total_stake': total_stake,
                'total_winnings': total_winnings,
                'ggr': net_result, # Changed net_result to ggr for consistency with definition
            })
    
    paginator = Paginator(report_data, 10) 
    page_number = request.GET.get('page')

    try:
        paginated_report_data = paginator.page(page_number)
    except PageNotAnInteger:
        paginated_report_data = paginator.page(1)
    except EmptyPage:
        paginated_report_data = paginator.page(paginator.num_pages)


    context = {
        'report_title': report_title,
        'report_data': paginated_report_data,
        'all_users': User.objects.all().order_by('email'), 
        'current_user_filter': user_filter,
        'start_date_filter': start_date.isoformat(),
        'end_date_filter': end_date.isoformat(),
    }
    return render(request, 'betting/admin/sales_winnings_report.html', context)


@login_required
@user_passes_test(lambda u: u.is_superuser or u.user_type == 'admin')
def admin_commission_report(request):
    from commission.models import WeeklyAgentCommission, MonthlyNetworkCommission
    
    report_title = "Admin Commission Report"
    agent_filter_id = request.GET.get('user', 'all')
    start_date_str = request.GET.get('start_date')
    end_date_str = request.GET.get('end_date')

    end_date = date.fromisoformat(end_date_str) if end_date_str else timezone.now().date()
    start_date = date.fromisoformat(start_date_str) if start_date_str else end_date - timedelta(days=30)

    # Fetch Commissions
    weekly_comms = WeeklyAgentCommission.objects.filter(
        period__end_date__gte=start_date,
        period__end_date__lte=end_date
    ).select_related('agent', 'period')
    
    monthly_comms = MonthlyNetworkCommission.objects.filter(
        period__end_date__gte=start_date,
        period__end_date__lte=end_date
    ).select_related('user', 'period')

    if agent_filter_id != 'all':
        try:
            user_id = int(agent_filter_id)
            weekly_comms = weekly_comms.filter(agent__id=user_id)
            monthly_comms = monthly_comms.filter(user__id=user_id)
        except ValueError:
            pass

    # Aggregate Data
    commission_data = []
    
    # Process Weekly
    for wc in weekly_comms:
        commission_data.append({
            'agent': wc.agent,
            'type': 'Weekly (Agent)',
            'period': str(wc.period),
            'ggr': wc.ggr,
            'commission_amount': wc.commission_total_amount,
            'status': wc.status,
            'paid_at': wc.paid_at
        })

    # Process Monthly
    for mc in monthly_comms:
        commission_data.append({
            'agent': mc.user,
            'type': f"Monthly ({mc.role.replace('_', ' ').title()})",
            'period': str(mc.period),
            'ggr': mc.ngr, # Use NGR as GGR equivalent for display
            'commission_amount': mc.commission_amount,
            'status': mc.status,
            'paid_at': mc.paid_at
        })

    # Summary Stats
    total_paid = sum(c['commission_amount'] for c in commission_data if c['status'] == 'paid')
    outstanding = sum(c['commission_amount'] for c in commission_data if c['status'] == 'pending')
    total_ggr = sum(c['ggr'] for c in commission_data)
    
    payout_ratio = Decimal(0)
    total_comm = total_paid + outstanding
    if total_ggr > 0:
        payout_ratio = (total_comm / total_ggr * 100).quantize(Decimal('0.01'))

    context = {
        'report_title': report_title,
        'commission_data': commission_data,
        'all_users': User.objects.filter(user_type__in=['agent', 'super_agent', 'master_agent']).order_by('email'),
        'current_user_filter': agent_filter_id,
        'start_date_filter': start_date.isoformat(),
        'end_date_filter': end_date.isoformat(),
        'summary': {
            'total_paid': total_paid,
            'outstanding': outstanding,
            'total_ggr': total_ggr,
            'payout_ratio': payout_ratio
        }
    }
    return render(request, 'betting/admin/commission_report.html', context)


@login_required
@user_passes_test(lambda u: u.is_superuser or u.user_type == 'admin')
def admin_activity_log(request):
    report_title = "Admin Activity Log"
    user_filter = request.GET.get('user', 'all')
    start_date_str = request.GET.get('start_date')
    end_date_str = request.GET.get('end_date')

    activity_logs_queryset = ActivityLog.objects.all().order_by('-timestamp')

    if user_filter != 'all':
        try:
            user_id = int(user_filter)
            activity_logs_queryset = activity_logs_queryset.filter(user__id=user_id)
        except (ValueError, TypeError):
            pass

    if start_date_str:
        try:
            start_date = date.fromisoformat(start_date_str)
            activity_logs_queryset = activity_logs_queryset.filter(timestamp__date__gte=start_date)
        except ValueError:
            messages.error(request, "Invalid start date format.")
    if end_date_str:
        try:
            end_date = date.fromisoformat(end_date_str)
            activity_logs_queryset = activity_logs_queryset.filter(timestamp__date__lte=end_date)
        except ValueError:
            messages.error(request, "Invalid end date format.")
    
    paginator = Paginator(activity_logs_queryset, 10)
    page_number = request.GET.get('page')

    try:
        activity_logs = paginator.page(page_number)
    except PageNotAnInteger:
        activity_logs = paginator.page(1)
    except EmptyPage:
        activity_logs = paginator.page(paginator.num_pages)

    context = {
        'report_title': report_title,
        'activity_logs': activity_logs,
        'all_users': User.objects.all().order_by('email'), 
        'current_user_filter': user_filter,
        'start_date_filter': start_date_str,
        'end_date_filter': end_date_str,
    }
    return render(request, 'betting/admin/activity_log.html', context)


@login_required
@user_passes_test(is_agent)
def agent_cashier_list(request):
    cashiers = User.objects.filter(agent=request.user, user_type='cashier').order_by('-date_joined')
    pending_requests = (
        CashierRegistrationRequest.objects
        .filter(agent=request.user, status='PENDING')
        .order_by('-created_at')
    )
    paginator = Paginator(cashiers, 10)
    page = request.GET.get('page')
    try:
        cashiers_page = paginator.page(page)
    except PageNotAnInteger:
        cashiers_page = paginator.page(1)
    except EmptyPage:
        cashiers_page = paginator.page(paginator.num_pages)

    context = {
        'cashiers': cashiers_page,
        'pending_cashier_requests': pending_requests,
    }
    return render(request, 'betting/agent/cashier_list.html', context)

@login_required
@user_passes_test(is_agent)
def agent_create_cashier(request):
    redirect_to = request.META.get('HTTP_REFERER') or reverse('betting:agent_cashier_list')
    if request.method == 'POST':
        try:
            first_name = request.POST.get('first_name')
            last_name = request.POST.get('last_name')
            other_name = request.POST.get('other_name')
            phone_number = request.POST.get('phone_number')

            if not first_name or not last_name or not other_name:
                messages.error(request, "First Name, Last Name, and Other Name are required.")
                return redirect(redirect_to)

            agent = request.user

            if not agent.cashier_prefix:
                import random
                while True:
                    prefix = str(random.randint(1000, 9999))
                    if not User.objects.filter(cashier_prefix=prefix).exists():
                        break
                agent.cashier_prefix = prefix
                agent.save(update_fields=['cashier_prefix'])

            base_prefix = agent.cashier_prefix

            existing_numbers = []
            for cashier in User.objects.filter(agent=agent, user_type='cashier').only('cashier_prefix'):
                cp = (cashier.cashier_prefix or '').strip()
                if not cp or '-' not in cp:
                    continue
                base, _, suffix = cp.partition('-')
                if base != base_prefix:
                    continue
                try:
                    existing_numbers.append(int(suffix))
                except Exception:
                    continue

            for req in CashierRegistrationRequest.objects.filter(agent=agent).exclude(status='REJECTED').only('cashier_code'):
                m = re.match(r'^C(\d+)$', (req.cashier_code or '').strip(), re.IGNORECASE)
                if m:
                    try:
                        existing_numbers.append(int(m.group(1)))
                    except Exception:
                        pass

            max_existing = max(existing_numbers) if existing_numbers else 2
            next_num = max_existing + 1

            cashier_code = f"C{next_num}"
            cashier_email = normalize_email_value(agent.email)

            root = None
            for c in User.objects.filter(agent=agent, user_type='cashier').exclude(username__isnull=True).only('username').order_by('date_joined'):
                uname = (c.username or '').strip()
                m = re.match(r'^(.*)C\d+$', uname, re.IGNORECASE)
                if m and m.group(1):
                    root = m.group(1)
                    break
            if not root:
                root = (agent.username or (agent.email.split('@')[0] if agent.email else 'Agent')).strip()
                root = re.sub(r'[^A-Za-z0-9]', '', root)[:30] or 'Agent'

            cashier_username = f"{root}{cashier_code}"
            if User.objects.filter(username__iexact=cashier_username).exists():
                counter = 1
                while True:
                    candidate = f"{root}{cashier_code}{counter}"
                    if not User.objects.filter(username__iexact=candidate).exists():
                        cashier_username = candidate
                        break
                    counter += 1

            cashier_prefix = f"{base_prefix}-{next_num:02d}"

            req = CashierRegistrationRequest.objects.create(
                agent=agent,
                first_name=first_name,
                last_name=last_name,
                other_name=other_name,
                phone_number=phone_number,
                cashier_code=cashier_code,
                cashier_email=cashier_email,
                cashier_username=cashier_username,
                cashier_prefix=cashier_prefix,
                status='PENDING'
            )

            try:
                admin_emails = list(
                    User.objects.filter(Q(is_superuser=True) | Q(user_type='admin'))
                    .exclude(email__isnull=True)
                    .exclude(email__exact='')
                    .values_list('email', flat=True)
                )
                admin_emails = sorted(set([e.strip() for e in admin_emails if e and e.strip()]))
                if admin_emails:
                    review_url = request.build_absolute_uri('/admin/betting/pendingcashierregistration/')
                    msg = (
                        "New cashier registration request submitted.\n\n"
                        f"Agent: {agent.get_full_name() or agent.email}\n"
                        f"Agent Email: {agent.email}\n"
                        f"Cashier Code: {cashier_code}\n"
                        f"Cashier Email: {cashier_email}\n"
                        f"Cashier Username: {cashier_username}\n"
                        f"Cashier Name: {first_name} {last_name} {other_name}\n"
                        f"Cashier Phone: {phone_number or '-'}\n"
                        f"Review: {review_url}\n"
                    )
                    send_mail(
                        subject=f"New Cashier Request ({cashier_code})",
                        message=msg,
                        from_email=settings.DEFAULT_FROM_EMAIL or settings.EMAIL_HOST_USER,
                        recipient_list=admin_emails,
                        fail_silently=True,
                    )
            except Exception:
                pass

            messages.success(request, f"Cashier registration {cashier_code} submitted for admin approval.")
        except Exception as e:
            messages.error(request, f"Error submitting cashier registration: {e}")
            logger.error(f"Error submitting cashier registration: {e}")
    
    return redirect(redirect_to)

@login_required
@user_passes_test(is_agent)
def agent_edit_cashier(request, cashier_id):
    if request.method == 'POST':
        cashier = get_object_or_404(User, id=cashier_id, agent=request.user, user_type='cashier')
        try:
            cashier.first_name = request.POST.get('first_name')
            cashier.last_name = request.POST.get('last_name')
            cashier.phone_number = request.POST.get('phone_number')
            # Email update is sensitive, usually requires verification, but for simplicity:
            new_email = request.POST.get('email')
            if new_email and new_email != cashier.email:
                cashier.email = normalize_email_value(new_email)
            
            cashier.save()
            messages.success(request, f"Cashier {cashier.email} updated successfully.")
        except Exception as e:
            messages.error(request, f"Error updating cashier: {e}")
    
    return redirect('betting:agent_cashier_list')

@login_required
@user_passes_test(is_agent)
def agent_delete_cashier(request, cashier_id):
    if request.method == 'POST':
        cashier = get_object_or_404(User, id=cashier_id, agent=request.user, user_type='cashier')
        try:
            # Check if cashier has active tickets or transactions?
            # For now, just delete. Standard Django behavior will cascade or set null based on model defs.
            # User model defines related_name='bet_tickets', on_delete=CASCADE usually for tickets.
            # But let's check safety.
            cashier.delete()
            messages.success(request, "Cashier deleted successfully.")
        except Exception as e:
            messages.error(request, f"Error deleting cashier: {e}")
    
    return redirect('betting:agent_cashier_list')

@login_required
@user_passes_test(is_agent)
@db_transaction.atomic
def agent_credit_cashier(request, cashier_id):
    # Check if user has permission to manage downline wallets
    if request.user.user_type in ['master_agent', 'super_agent'] and not getattr(request.user, 'can_manage_downline_wallets', True):
        CreditLog.objects.create(
            actor=request.user,
            action_type='credit_cashier_denied',
            amount=Decimal('0.00')
        )
        messages.error(request, "You do not have permission to credit or debit downline wallets. Please contact the administrator.")
        return redirect('betting:agent_cashier_list')

    if request.method == 'POST':
        cashier = get_object_or_404(User, id=cashier_id, agent=request.user, user_type='cashier')
        amount_str = request.POST.get('amount')
        
        try:
            amount = Decimal(amount_str)
            if amount <= 0:
                messages.error(request, "Amount must be positive.")
                return redirect('betting:agent_cashier_list')
            
            # Ensure wallets exist before locking
            _ = request.user.wallet
            _ = cashier.wallet

            agent_wallet = Wallet.objects.select_for_update().get(user=request.user)
            if agent_wallet.balance < amount:
                messages.error(request, "Insufficient funds in your wallet.")
                return redirect('betting:agent_cashier_list')
            
            cashier_wallet = Wallet.objects.select_for_update().get(user=cashier)
            
            # Create transactions
            tx_out = Transaction.objects.create(
                user=request.user,
                transaction_type='wallet_transfer_out',
                amount=amount,
                status='completed',
                is_successful=True,
                target_user=cashier,
                description=f"Transfer to cashier {cashier.email}"
            )
            
            tx_in = Transaction.objects.create(
                user=cashier,
                transaction_type='wallet_transfer_in',
                amount=amount,
                status='completed',
                is_successful=True,
                initiating_user=request.user,
                description=f"Received credit from agent {request.user.email}"
            )
            agent_wallet.apply_delta(
                amount=-amount,
                actor=request.user,
                transaction_obj=tx_out,
                reference=str(tx_out.id),
                reason=tx_out.description,
                metadata={"cashier_id": cashier.id, "source": "agent_credit_cashier"},
            )
            cashier_wallet.apply_delta(
                amount=amount,
                actor=request.user,
                transaction_obj=tx_in,
                reference=str(tx_out.id),
                reason=tx_in.description,
                metadata={"agent_id": request.user.id, "source": "agent_credit_cashier"},
            )
            
            messages.success(request, f"Successfully credited ₦{amount} to {cashier.email}.")
            
        except InvalidOperation:
            messages.error(request, "Invalid amount format.")
        except Exception as e:
            messages.error(request, f"Error processing credit: {e}")
            
    return redirect('betting:agent_cashier_list')


# --- API Endpoints (Placeholder implementations) ---

@csrf_exempt
def api_betting_periods(request):
    return JsonResponse({'status': 'success', 'message': 'API endpoint for betting periods (placeholder).'})

@csrf_exempt
def api_fixtures(request):
    return JsonResponse({'status': 'success', 'message': 'API endpoint for fixtures (placeholder).'})

@csrf_exempt
def api_place_bet(request):
    return JsonResponse({'status': 'success', 'message': 'API endpoint for place bet (placeholder).'})

@csrf_exempt
def api_check_ticket_status(request):
    return JsonResponse({'status': 'success', 'message': 'API endpoint for check ticket status (placeholder).'})

@login_required
def api_user_wallet(request):
    try:
        wallet = request.user.wallet
        return JsonResponse({'status': 'success', 'balance': float(wallet.balance)})
    except Exception as e:
        return JsonResponse({'status': 'error', 'message': str(e)})

@csrf_exempt
def api_initiate_deposit(request):
    raise Http404()

@csrf_exempt
def api_verify_deposit(request):
    raise Http404()

@login_required
def api_withdraw_funds(request):
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'Invalid request.'}, status=405)
    if not request.headers.get('Content-Type', '').startswith('application/json'):
        return JsonResponse({'status': 'error', 'message': 'Content-Type must be application/json.'}, status=415)
    return withdraw_funds(request)

@csrf_exempt
def api_wallet_transfer(request):
    return JsonResponse({'status': 'success', 'message': 'API endpoint for wallet transfer (placeholder).'})

@csrf_exempt
def api_user_profile(request):
    return JsonResponse({'status': 'success', 'message': 'API endpoint for user profile (placeholder).'})


TICKET_TRANSACTION_FILTER_TYPES = [
    "Admin Credit",
    "Admin Debit",
    "Bonus Credit",
    "Commission Adjustment",
    "Commission Credit",
    "Deposit",
    "Overdraft Credit",
    "Overdraft Debit",
    "Refund Reversal",
    "Ticket Purchase",
    "Ticket Voided",
    "Wallet Adjustment",
    "Wallet Transfer In",
    "Wallet Transfer Out",
    "Winning Reversal",
    "Winning Settlement",
    "Withdrawal",
    "Withdrawal Refund",
]
TICKET_TRANSACTION_FILTER_SOURCES = [
    "Admin Credit",
    "Admin Debit",
    "Bonus",
    "Commission",
    "Kora",
    "Monnify",
    "Overdraft",
    "Paystack",
    "Ticket Purchase",
    "Ticket Settlement",
    "Ticket Void",
    "Wallet Transfer",
    "Withdrawal",
]


def _default_ticket_transaction_filters():
    return {
        'date_from': '',
        'date_to': '',
        'username': '',
        'role': '',
        'cashier_id': '',
        'agent_id': '',
        'super_agent_id': '',
        'master_agent_id': '',
        'ticket_id': '',
        'reference_id': '',
        'transaction_type': '',
        'source': '',
        'order': 'desc',
    }


def can_view_ticket_transactions(user):
    allowed_types = {
        'cashier',
        'agent',
        'super_agent',
        'master_agent',
        'retail_manager',
        'crm',
        'finance',
        'account_user',
        'admin',
    }
    return bool(getattr(user, 'is_authenticated', False) and (user.is_superuser or user.user_type in allowed_types))


def _ticket_transaction_scope_users_queryset(user):
    if not getattr(user, 'is_authenticated', False):
        return User.objects.none()
    if user.is_superuser or user.user_type in {'admin', 'finance', 'account_user', 'crm'}:
        return User.objects.all()
    if user.user_type == 'cashier':
        return User.objects.filter(id=user.id)
    if user.user_type == 'agent':
        return User.objects.filter(Q(id=user.id) | Q(agent=user)).distinct()
    if user.user_type == 'super_agent':
        return User.objects.filter(
            Q(id=user.id) |
            Q(super_agent=user) |
            Q(agent__super_agent=user)
        ).distinct()
    if user.user_type == 'master_agent':
        return User.objects.filter(
            Q(id=user.id) |
            Q(master_agent=user) |
            Q(super_agent__master_agent=user) |
            Q(agent__master_agent=user) |
            Q(agent__super_agent__master_agent=user)
        ).distinct()
    if is_retail_manager(user):
        return (User.objects.filter(id=user.id) | get_retail_network_users_qs(user)).distinct()
    return User.objects.none()


def _parse_statement_date(raw_value, *, end_of_day=False):
    raw_value = (raw_value or '').strip()
    if not raw_value:
        return None
    try:
        parsed = date.fromisoformat(raw_value)
    except ValueError:
        return None
    time_value = datetime.max.time() if end_of_day else datetime.min.time()
    combined = datetime.combine(parsed, time_value)
    if timezone.is_naive(combined):
        return timezone.make_aware(combined, timezone.get_current_timezone())
    return combined


def _ticket_transaction_filtered_users(scope_users, filters):
    users = scope_users
    username_q = (filters.get('username') or '').strip()
    if username_q:
        users = users.filter(
            Q(username__icontains=username_q) |
            Q(email__icontains=username_q) |
            Q(first_name__icontains=username_q) |
            Q(last_name__icontains=username_q)
        )

    role = (filters.get('role') or '').strip()
    if role in {'cashier', 'agent', 'super_agent', 'master_agent', 'retail_manager'}:
        users = users.filter(user_type=role)

    cashier_id = (filters.get('cashier_id') or '').strip()
    if cashier_id.isdigit():
        users = users.filter(id=int(cashier_id), user_type='cashier')

    agent_id = (filters.get('agent_id') or '').strip()
    if agent_id.isdigit():
        agent_int = int(agent_id)
        users = users.filter(Q(id=agent_int, user_type='agent') | Q(agent_id=agent_int))

    super_agent_id = (filters.get('super_agent_id') or '').strip()
    if super_agent_id.isdigit():
        super_agent_int = int(super_agent_id)
        users = users.filter(
            Q(id=super_agent_int, user_type='super_agent') |
            Q(super_agent_id=super_agent_int) |
            Q(agent__super_agent_id=super_agent_int)
        )

    master_agent_id = (filters.get('master_agent_id') or '').strip()
    if master_agent_id.isdigit():
        master_agent_int = int(master_agent_id)
        users = users.filter(
            Q(id=master_agent_int, user_type='master_agent') |
            Q(master_agent_id=master_agent_int) |
            Q(super_agent__master_agent_id=master_agent_int) |
            Q(agent__master_agent_id=master_agent_int) |
            Q(agent__super_agent__master_agent_id=master_agent_int)
        )
    return users.distinct()


def _ticket_transaction_filtered_queryset(user, filters):
    scope_users = _ticket_transaction_scope_users_queryset(user)
    filtered_users = _ticket_transaction_filtered_users(scope_users, filters)
    queryset = TicketTransactionLedger.objects.select_related(
        'user',
        'ticket',
        'created_by',
        'transaction',
    ).filter(user__in=filtered_users)

    date_from = _parse_statement_date(filters.get('date_from'))
    date_to = _parse_statement_date(filters.get('date_to'), end_of_day=True)
    if date_from:
        queryset = queryset.filter(created_at__gte=date_from)
    if date_to:
        queryset = queryset.filter(created_at__lte=date_to)

    ticket_id = (filters.get('ticket_id') or '').strip()
    if ticket_id:
        queryset = queryset.filter(
            Q(ticket__ticket_id__icontains=ticket_id) |
            Q(reference__icontains=ticket_id) |
            Q(description__icontains=ticket_id)
        )

    reference_id = (filters.get('reference_id') or '').strip()
    if reference_id:
        queryset = queryset.filter(reference__icontains=reference_id)

    transaction_type = (filters.get('transaction_type') or '').strip()
    if transaction_type:
        queryset = queryset.filter(transaction_type=transaction_type)

    source = (filters.get('source') or '').strip()
    if source:
        queryset = queryset.filter(source=source)

    order = 'asc' if (filters.get('order') or '').strip().lower() == 'asc' else 'desc'
    ordering = ('created_at', 'id') if order == 'asc' else ('-created_at', '-id')
    return queryset.order_by(*ordering), filtered_users, date_from, date_to, order


def _aggregate_money(queryset, *, field):
    value = queryset.aggregate(total=Coalesce(Sum(field), Value(Decimal('0.00'), output_field=DecimalField(max_digits=12, decimal_places=2))))['total']
    return Decimal(str(value or '0.00')).quantize(Decimal('0.01'))


def _sum_user_balances(users_queryset):
    value = Wallet.objects.filter(user__in=users_queryset).aggregate(
        total=Coalesce(Sum('balance'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=12, decimal_places=2)))
    )['total']
    return Decimal(str(value or '0.00')).quantize(Decimal('0.01'))


def _opening_balance_for_users(users_queryset, start_dt):
    if start_dt:
        latest_before_subquery = TicketTransactionLedger.objects.filter(
            user_id=OuterRef('pk'),
            created_at__lt=start_dt,
        ).order_by('-created_at', '-id').values('balance_after')[:1]
        aggregated = users_queryset.annotate(
            opening_balance_amount=Coalesce(
                Subquery(latest_before_subquery, output_field=DecimalField(max_digits=12, decimal_places=2)),
                Value(Decimal('0.00'), output_field=DecimalField(max_digits=12, decimal_places=2)),
            )
        ).aggregate(
            total=Coalesce(Sum('opening_balance_amount'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=12, decimal_places=2)))
        )['total']
        return Decimal(str(aggregated or '0.00')).quantize(Decimal('0.01'))

    first_before_subquery = TicketTransactionLedger.objects.filter(
        user_id=OuterRef('pk')
    ).order_by('created_at', 'id').values('balance_before')[:1]
    aggregated = users_queryset.annotate(
        opening_balance_amount=Coalesce(
            Subquery(first_before_subquery, output_field=DecimalField(max_digits=12, decimal_places=2)),
            Value(Decimal('0.00'), output_field=DecimalField(max_digits=12, decimal_places=2)),
        )
    ).aggregate(
        total=Coalesce(Sum('opening_balance_amount'), Value(Decimal('0.00'), output_field=DecimalField(max_digits=12, decimal_places=2)))
    )['total']
    return Decimal(str(aggregated or '0.00')).quantize(Decimal('0.01'))


def _ticket_transaction_summary(queryset, filtered_users, start_dt):
    total_debits = _aggregate_money(queryset, field='debit')
    total_credits = _aggregate_money(queryset, field='credit')
    opening_balance = _opening_balance_for_users(filtered_users, start_dt)
    net_movement = (total_credits - total_debits).quantize(Decimal('0.01'))
    closing_balance = (opening_balance + net_movement).quantize(Decimal('0.01'))
    commission_credits = (
        _aggregate_money(queryset.filter(transaction_type='Commission Credit'), field='credit') +
        _aggregate_money(queryset.filter(transaction_type='Commission Adjustment'), field='credit')
    ).quantize(Decimal('0.01'))
    return {
        'opening_balance': opening_balance,
        'current_balance': _sum_user_balances(filtered_users),
        'closing_balance': closing_balance,
        'total_deposits': _aggregate_money(queryset.filter(transaction_type='Deposit'), field='credit'),
        'total_stakes': _aggregate_money(queryset.filter(transaction_type='Ticket Purchase'), field='debit'),
        'total_winnings': _aggregate_money(queryset.filter(transaction_type='Winning Settlement'), field='credit'),
        'total_voids_refunded': _aggregate_money(queryset.filter(transaction_type='Ticket Voided'), field='credit'),
        'total_withdrawals': _aggregate_money(queryset.filter(transaction_type='Withdrawal'), field='debit'),
        'commission_credits': commission_credits,
        'net_movement': net_movement,
        'total_debits': total_debits,
        'total_credits': total_credits,
    }


def _ticket_transaction_export_rows(queryset):
    rows = []
    for item in queryset:
        rows.append({
            'Date/Time': timezone.localtime(item.created_at).strftime('%Y-%m-%d %H:%M:%S') if item.created_at else '',
            'Username': item.user.username or item.user.email,
            'Reference ID': item.reference,
            'Ticket ID': getattr(getattr(item, 'ticket', None), 'ticket_id', '') or '',
            'Source': item.source,
            'Transaction Type': item.transaction_type,
            'Description': item.description,
            'Debit': f"{item.debit:.2f}",
            'Credit': f"{item.credit:.2f}",
            'Balance Before': f"{item.balance_before:.2f}",
            'Balance After': f"{item.balance_after:.2f}",
        })
    return rows


def _ticket_transaction_filter_metadata(request, filters, summary):
    return {
        'generated_at': timezone.localtime(timezone.now()).strftime('%Y-%m-%d %H:%M:%S'),
        'username': filters.get('username') or 'All',
        'date_from': filters.get('date_from') or 'Inception',
        'date_to': filters.get('date_to') or 'Now',
        'reference_id': filters.get('reference_id') or 'All',
        'ticket_id': filters.get('ticket_id') or 'All',
        'transaction_type': filters.get('transaction_type') or 'All',
        'source': filters.get('source') or 'All',
        'opening_balance': f"{summary['opening_balance']:.2f}",
        'closing_balance': f"{summary['closing_balance']:.2f}",
        'current_balance': f"{summary['current_balance']:.2f}",
    }


def _ticket_transaction_widget_context(user, *, limit=8, date_from='', date_to='', full_url_name='betting:ticket_transactions'):
    filters = _default_ticket_transaction_filters()
    filters['date_from'] = (date_from or '').strip()
    filters['date_to'] = (date_to or '').strip()
    queryset, filtered_users, start_dt, _end_dt, _order = _ticket_transaction_filtered_queryset(user, filters)
    summary = _ticket_transaction_summary(queryset, filtered_users, start_dt)
    recent_entries = list(queryset[:limit])
    query_params = {}
    if filters['date_from']:
        query_params['date_from'] = filters['date_from']
    if filters['date_to']:
        query_params['date_to'] = filters['date_to']
    open_full_url = reverse(full_url_name)
    if query_params:
        open_full_url = f"{open_full_url}?{urlencode(query_params)}"
    return {
        'summary': summary,
        'entries': recent_entries,
        'entry_count': queryset.count(),
        'open_full_url': open_full_url,
        'date_from': filters['date_from'],
        'date_to': filters['date_to'],
    }


def _ticket_transaction_request_filters(request):
    filters = _default_ticket_transaction_filters()
    filters.update({
        'date_from': (request.GET.get('date_from') or '').strip(),
        'date_to': (request.GET.get('date_to') or '').strip(),
        'username': (request.GET.get('username') or '').strip(),
        'role': (request.GET.get('role') or '').strip(),
        'cashier_id': (request.GET.get('cashier') or '').strip(),
        'agent_id': (request.GET.get('agent') or '').strip(),
        'super_agent_id': (request.GET.get('super_agent') or '').strip(),
        'master_agent_id': (request.GET.get('master_agent') or '').strip(),
        'ticket_id': (request.GET.get('ticket_id') or '').strip(),
        'reference_id': (request.GET.get('reference_id') or '').strip(),
        'transaction_type': (request.GET.get('transaction_type') or '').strip(),
        'source': (request.GET.get('source') or '').strip(),
        'order': (request.GET.get('order') or 'desc').strip(),
    })
    return filters


def _build_ticket_transactions_page_context(user, filters, *, page_number=1):
    queryset, filtered_users, start_dt, end_dt, order = _ticket_transaction_filtered_queryset(user, filters)
    summary = _ticket_transaction_summary(queryset, filtered_users, start_dt)
    page_obj = Paginator(queryset, 50).get_page(page_number)
    role_filter_users = _ticket_transaction_filtered_users(
        _ticket_transaction_scope_users_queryset(user),
        {
            'username': filters['username'],
            'role': filters.get('role', ''),
            'cashier_id': '',
            'agent_id': '',
            'super_agent_id': '',
            'master_agent_id': '',
        },
    )
    transaction_type_options = sorted(set(TICKET_TRANSACTION_FILTER_TYPES) | set(TicketTransactionLedger.objects.values_list('transaction_type', flat=True)))
    source_options = sorted(set(TICKET_TRANSACTION_FILTER_SOURCES) | set(TicketTransactionLedger.objects.values_list('source', flat=True)))
    context = {
        'page_obj': page_obj,
        'summary': summary,
        'filters': filters,
        'order': order,
        'date_from_dt': start_dt,
        'date_to_dt': end_dt,
        'cashier_options': list(role_filter_users.filter(user_type='cashier').order_by('username', 'email')[:300]),
        'agent_options': list(role_filter_users.filter(user_type='agent').order_by('username', 'email')[:300]),
        'super_agent_options': list(role_filter_users.filter(user_type='super_agent').order_by('username', 'email')[:300]),
        'master_agent_options': list(role_filter_users.filter(user_type='master_agent').order_by('username', 'email')[:300]),
        'transaction_type_options': [opt for opt in transaction_type_options if opt],
        'source_options': [opt for opt in source_options if opt],
        'role_options': [
            ('cashier', 'Cashier'),
            ('agent', 'Agent'),
            ('super_agent', 'Super Agent'),
            ('master_agent', 'Master Agent'),
            ('retail_manager', 'Retail Manager'),
        ],
        'can_filter_role': user.is_superuser or user.user_type == 'admin',
        'can_filter_cashier': user.user_type in {'agent', 'super_agent', 'master_agent', 'retail_manager', 'crm', 'finance', 'account_user', 'admin'} or user.is_superuser,
        'can_filter_agent': user.user_type in {'super_agent', 'master_agent', 'retail_manager', 'crm', 'finance', 'account_user', 'admin'} or user.is_superuser,
        'can_filter_super_agent': user.user_type in {'master_agent', 'retail_manager', 'crm', 'finance', 'account_user', 'admin'} or user.is_superuser,
        'can_filter_master_agent': user.user_type in {'retail_manager', 'crm', 'finance', 'account_user', 'admin'} or user.is_superuser,
    }
    return context, queryset, summary


def _export_ticket_transaction_statement(request, *, queryset, filters, summary, fmt):
    rows = _ticket_transaction_export_rows(queryset)
    meta = _ticket_transaction_filter_metadata(request, filters, summary)
    filename_base = f"ticket_transactions_{timezone.now().strftime('%Y%m%d_%H%M%S')}"

    if fmt == 'xlsx':
        import io
        import pandas as pd

        output = io.BytesIO()
        summary_rows = [{'Metric': key.replace('_', ' ').title(), 'Value': value} for key, value in meta.items()]
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            pd.DataFrame(summary_rows).to_excel(writer, sheet_name='Summary', index=False)
            pd.DataFrame(rows).to_excel(writer, sheet_name='Transactions', index=False)
        output.seek(0)
        response = HttpResponse(output.getvalue(), content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        response['Content-Disposition'] = f'attachment; filename="{filename_base}.xlsx"'
        return response

    if fmt == 'pdf':
        from weasyprint import HTML

        html = render_to_string(
            'betting/ticket_transactions_export_pdf.html',
            {
                'rows': rows,
                'meta': meta,
                'summary': summary,
                'title': 'Ticket Transactions Statement',
            },
            request=request,
        )
        pdf_bytes = HTML(string=html, base_url=request.build_absolute_uri('/')).write_pdf()
        response = HttpResponse(pdf_bytes, content_type='application/pdf')
        response['Content-Disposition'] = f'attachment; filename="{filename_base}.pdf"'
        return response

    if fmt == 'print':
        return render(
            request,
            'betting/ticket_transactions_print.html',
            {
                'rows': queryset,
                'summary': summary,
                'filters': filters,
                'meta': meta,
                'title': 'Ticket Transactions Statement',
            },
        )
    return HttpResponseBadRequest('Unsupported format.')


@login_required
@user_passes_test(can_view_ticket_transactions)
def ticket_transactions(request):
    filters = _ticket_transaction_request_filters(request)
    export_format = (request.GET.get('format') or '').strip().lower()
    context, queryset, summary = _build_ticket_transactions_page_context(
        request.user,
        filters,
        page_number=request.GET.get('page') or 1,
    )

    if export_format in {'xlsx', 'pdf', 'print'}:
        return _export_ticket_transaction_statement(
            request,
            queryset=queryset,
            filters=filters,
            summary=summary,
            fmt=export_format,
        )

    return render(request, 'betting/ticket_transactions.html', context)


@login_required
@user_passes_test(lambda u: u.is_superuser or u.user_type == 'admin')
def admin_ticket_transactions(request):
    request.current_app = 'betting_admin'
    filters = _ticket_transaction_request_filters(request)
    export_format = (request.GET.get('format') or '').strip().lower()
    context, queryset, summary = _build_ticket_transactions_page_context(
        request.user,
        filters,
        page_number=request.GET.get('page') or 1,
    )
    if export_format in {'xlsx', 'pdf', 'print'}:
        return _export_ticket_transaction_statement(
            request,
            queryset=queryset,
            filters=filters,
            summary=summary,
            fmt=export_format,
        )
    return render(
        request,
        'betting/admin/ticket_transactions.html',
        {
            **context,
            'standalone_url': reverse('betting:ticket_transactions'),
            'admin_reset_url': reverse('betting_admin:admin_ticket_transactions'),
        },
    )


@csrf_exempt
def api_change_password(request):
    return JsonResponse({'status': 'success', 'message': 'API endpoint for change password (placeholder).'})

@csrf_exempt
def api_user_transactions(request):
    return JsonResponse({'status': 'success', 'message': 'API endpoint for user transactions (placeholder).'})

@csrf_exempt
def api_agent_commissions(request):
    return JsonResponse({'status': 'success', 'message': 'API endpoint for agent commissions (placeholder).'})

@csrf_exempt
def api_agent_users(request):
    return JsonResponse({'status': 'success', 'message': 'API endpoint for agent users (placeholder).'})

@csrf_exempt
def api_cashier_transactions(request):
    return JsonResponse({'status': 'success', 'message': 'API endpoint for cashier transactions (placeholder).'})

@csrf_exempt
def api_bet_tickets(request):
    return JsonResponse({'status': 'success', 'message': 'API endpoint for bet tickets (placeholder).'})

@csrf_exempt
def api_void_ticket(request):
    return JsonResponse({'status': 'success', 'message': 'API endpoint for voiding a ticket (placeholder).'})

@csrf_exempt
def api_manage_users(request):
    return JsonResponse({'status': 'success', 'message': 'API endpoint for managing users (placeholder).'})

@csrf_exempt
def api_system_settings(request):
    return JsonResponse({'status': 'success', 'message': 'API endpoint for system settings (placeholder).'})


@login_required
def mark_downline_activity_notifications_read(request):
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'Invalid request.'}, status=400)

    if request.user.user_type not in ['agent', 'super_agent', 'master_agent']:
        return JsonResponse({'status': 'error', 'message': 'Not allowed.'}, status=403)

    request.user.downline_activity_last_seen_at = timezone.now()
    request.user.save(update_fields=['downline_activity_last_seen_at'])
    return JsonResponse({'status': 'success'})

# --- Account User Views ---

from commission.models import WeeklyAgentCommission, MonthlyNetworkCommission
from commission.services import pay_weekly_commission, pay_monthly_network_commission, pay_weekly_commission_amount, pay_monthly_network_commission_amount

@login_required
@user_passes_test(is_account_user)
def account_user_dashboard(request):
    search_form = AccountUserSearchForm()
    action_form = AccountUserWalletActionForm()
    found_user = None
    search_results = None
    activity_log = []
    start_date_str = (request.GET.get('start_date') or '').strip()
    end_date_str = (request.GET.get('end_date') or '').strip()
    commission_period_id_raw = (request.GET.get('commission_period') or '').strip()
    commission_search = (request.GET.get('commission_search') or '').strip()
    selected_top_period_id_raw = (request.GET.get('top_period') or '').strip()

    # --- NEW: Fetch Credit/Loan Data ---
    incoming_credit_request_filter = Q(recipient=request.user)
    incoming_credit_request_filter |= Q(
        request_type__in=CRM_WALLET_APPROVAL_REQUEST_TYPES,
        recipient__user_type='account_user',
    )
    all_incoming_credit_requests = CreditRequest.objects.filter(
        incoming_credit_request_filter,
        status='pending'
    ).select_related('requester', 'recipient').distinct().order_by('-created_at')
    crm_wallet_approval_requests = list(
        all_incoming_credit_requests.filter(request_type__in=CRM_WALLET_APPROVAL_REQUEST_TYPES)[:8]
    )
    requests_paginator = Paginator(all_incoming_credit_requests, 10)
    requests_page = request.GET.get('requests_page')
    try:
        incoming_credit_requests = requests_paginator.page(requests_page)
    except PageNotAnInteger:
        incoming_credit_requests = requests_paginator.page(1)
    except EmptyPage:
        incoming_credit_requests = requests_paginator.page(requests_paginator.num_pages)

    all_active_loans_given = Loan.objects.filter(
        lender=request.user, 
        status='active'
    ).order_by('-created_at')
    loans_paginator = Paginator(all_active_loans_given, 10)
    loans_page = request.GET.get('loans_page')
    try:
        active_loans_given = loans_paginator.page(loans_page)
    except PageNotAnInteger:
        active_loans_given = loans_paginator.page(1)
    except EmptyPage:
        active_loans_given = loans_paginator.page(loans_paginator.num_pages)
    # -----------------------------------

    recent_transactions = attach_wallet_balance_snapshots(Transaction.objects.filter(
        Q(initiating_user=request.user) | Q(user=request.user)
    ).order_by('-timestamp')[:20])

    today = timezone.localdate()
    account_kpis = {
        'deposits_today': Transaction.objects.filter(transaction_type='deposit', status='completed', is_successful=True, timestamp__date=today).aggregate(
            s=Coalesce(Sum('amount'), Value(0), output_field=DecimalField())
        )['s'],
        'withdrawals_pending': UserWithdrawal.objects.filter(status='pending').count(),
        'withdrawals_today': UserWithdrawal.objects.filter(request_time__date=today).aggregate(
            s=Coalesce(Sum('amount'), Value(0), output_field=DecimalField())
        )['s'],
        'failed_transactions_7d': Transaction.objects.filter(Q(status='failed') | Q(is_successful=False)).filter(timestamp__gte=timezone.now() - timedelta(days=7)).count(),
    }

    metrics_start_date = None
    metrics_end_date = None
    if start_date_str:
        try:
            metrics_start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date()
        except Exception:
            metrics_start_date = None
    if end_date_str:
        try:
            metrics_end_date = datetime.strptime(end_date_str, '%Y-%m-%d').date()
        except Exception:
            metrics_end_date = None
    if metrics_start_date and metrics_end_date and metrics_start_date > metrics_end_date:
        metrics_start_date, metrics_end_date = metrics_end_date, metrics_start_date
    if metrics_start_date is None and metrics_end_date is None:
        metrics_end_date = today
        metrics_start_date = today - timedelta(days=30)
        metrics_label = 'Last 30 days'
    else:
        metrics_start_date = metrics_start_date or metrics_end_date or today
        metrics_end_date = metrics_end_date or metrics_start_date or today
        metrics_label = 'Custom range'

    metrics_start_dt = timezone.make_aware(datetime.combine(metrics_start_date, datetime.min.time()))
    metrics_end_dt = timezone.make_aware(datetime.combine(metrics_end_date, datetime.max.time()))
    top_fixtures, top_period_options, selected_top_period_id = build_top_fixtures_by_betting_period(selected_top_period_id_raw)
    overdraft_reporting_context = _build_overdraft_reporting_dashboard_context(
        request,
        include_retail_manager=True,
        extra_params={'section': 'overdraft_center'},
        per_page=25,
    )

    platform_users_qs = User.objects.filter(is_superuser=False)
    kpi_cache_key = f"account_user:kpis:v2:{metrics_start_date.isoformat()}:{metrics_end_date.isoformat()}"
    chart_cache_key = f"account_user:charts:{metrics_start_date.isoformat()}:{metrics_end_date.isoformat()}"
    kpis = cache.get(kpi_cache_key)
    charts_data = cache.get(chart_cache_key)

    if kpis is None:
        total_registered_users = platform_users_qs.count()
        active_users_today = platform_users_qs.filter(last_login__date=today).count()
        new_registrations = platform_users_qs.filter(date_joined__date__gte=metrics_start_date, date_joined__date__lte=metrics_end_date).count()

        tickets_qs = BetTicket.objects.exclude(status__in=['deleted', 'cancelled']).filter(placed_at__gte=metrics_start_dt, placed_at__lte=metrics_end_dt)
        total_bets_placed = tickets_qs.count()
        total_stake_amount = tickets_qs.aggregate(v=Sum('stake_amount'))['v'] or Decimal('0.00')

        total_payouts = tickets_qs.filter(status='won').aggregate(v=Sum('max_winning'))['v'] or Decimal('0.00')
        ggr = total_stake_amount - total_payouts

        weekly_periods_in_range = CommissionPeriod.objects.filter(
            period_type='weekly',
            start_date__gte=metrics_start_date,
            end_date__lte=metrics_end_date,
        )
        total_paid_commission = (
            WeeklyAgentCommission.objects.filter(period__in=weekly_periods_in_range)
            .aggregate(v=Sum('amount_paid'))['v'] or Decimal('0.00')
        )
        ngr = ggr - total_paid_commission

        total_deposits = Transaction.objects.filter(
            transaction_type='deposit',
            status='completed',
            is_successful=True,
            timestamp__gte=metrics_start_dt,
            timestamp__lte=metrics_end_dt,
        ).aggregate(v=Sum('amount'))['v'] or Decimal('0.00')

        total_withdrawals = UserWithdrawal.objects.filter(
            status='approved',
            approved_rejected_time__gte=metrics_start_dt,
            approved_rejected_time__lte=metrics_end_dt,
        ).aggregate(v=Sum('amount'))['v'] or Decimal('0.00')

        pending_withdrawals_count = UserWithdrawal.objects.filter(status='pending').count()

        bettors_in_range = tickets_qs.values('user_id').distinct().count()
        conversion_rate = (Decimal(bettors_in_range) / Decimal(total_registered_users) * Decimal('100.00')) if total_registered_users else Decimal('0.00')
        average_bet_value = (total_stake_amount / Decimal(total_bets_placed)) if total_bets_placed else Decimal('0.00')

        kpis = {
            'total_registered_users': int(total_registered_users),
            'active_users_today': int(active_users_today),
            'new_registrations': int(new_registrations),
            'total_bets_placed': int(total_bets_placed),
            'total_stake_amount': str(total_stake_amount),
            'total_payouts': str(total_payouts),
            'ggr': str(ggr),
            'total_paid_commission': str(total_paid_commission),
            'ngr': str(ngr),
            'total_deposits': str(total_deposits),
            'total_withdrawals': str(total_withdrawals),
            'pending_withdrawals': int(pending_withdrawals_count),
            'conversion_rate': str(conversion_rate.quantize(Decimal('0.01'))),
            'average_bet_value': str(average_bet_value.quantize(Decimal('0.01'))),
        }
        cache.set(kpi_cache_key, kpis, 30)

    if charts_data is None:
        ticket_series = (
            BetTicket.objects.exclude(status__in=['deleted', 'cancelled'])
            .filter(placed_at__gte=metrics_start_dt, placed_at__lte=metrics_end_dt)
            .annotate(day=TruncDate('placed_at'))
            .values('day')
            .annotate(
                stake=Sum('stake_amount'),
                payouts=Sum(Case(When(status='won', then=F('max_winning')), default=Value(0), output_field=DecimalField())),
                bets=Count('id'),
            )
            .order_by('day')
        )

        registrations_series = (
            platform_users_qs.filter(date_joined__gte=metrics_start_dt, date_joined__lte=metrics_end_dt)
            .annotate(day=TruncDate('date_joined'))
            .values('day')
            .annotate(registrations=Count('id'))
            .order_by('day')
        )

        deposit_series = (
            Transaction.objects.filter(
                transaction_type='deposit',
                status='completed',
                is_successful=True,
                timestamp__gte=metrics_start_dt,
                timestamp__lte=metrics_end_dt,
            )
            .annotate(day=TruncDate('timestamp'))
            .values('day')
            .annotate(deposits=Sum('amount'))
            .order_by('day')
        )

        withdrawal_series = (
            UserWithdrawal.objects.filter(
                status='approved',
                approved_rejected_time__gte=metrics_start_dt,
                approved_rejected_time__lte=metrics_end_dt,
            )
            .annotate(day=TruncDate('approved_rejected_time'))
            .values('day')
            .annotate(withdrawals=Sum('amount'))
            .order_by('day')
        )

        selection_top = (
            Selection.objects.filter(bet_ticket__placed_at__gte=metrics_start_dt, bet_ticket__placed_at__lte=metrics_end_dt)
            .values('fixture_home_team', 'fixture_away_team')
            .annotate(picks=Count('id'))
            .order_by('-picks')[:5]
        )

        charts_data = {
            'ticket_series': [
                {
                    'day': r['day'].isoformat(),
                    'stake': str(r['stake'] or Decimal('0.00')),
                    'payouts': str(r['payouts'] or Decimal('0.00')),
                    'bets': int(r['bets'] or 0),
                }
                for r in ticket_series
            ],
            'registrations_series': [{'day': r['day'].isoformat(), 'registrations': int(r['registrations'] or 0)} for r in registrations_series],
            'deposit_series': [{'day': r['day'].isoformat(), 'deposits': str(r['deposits'] or Decimal('0.00'))} for r in deposit_series],
            'withdrawal_series': [{'day': r['day'].isoformat(), 'withdrawals': str(r['withdrawals'] or Decimal('0.00'))} for r in withdrawal_series],
            'top_fixtures': [
                {
                    'label': f"{(r.get('fixture_home_team') or '').strip()} vs {(r.get('fixture_away_team') or '').strip()}".strip() or 'Fixture',
                    'picks': int(r['picks'] or 0),
                }
                for r in selection_top
            ],
        }
        cache.set(chart_cache_key, charts_data, 60)

    charts_data = dict(charts_data or {})
    charts_data['top_fixtures'] = top_fixtures

    # Handle View User via GET
    if request.method == 'GET' and 'view_user_id' in request.GET:
        try:
            found_user = User.objects.exclude(is_superuser=True).exclude(user_type='account_user').get(id=request.GET.get('view_user_id'))
        except (User.DoesNotExist, ValueError):
            messages.error(request, "User not found or invalid ID.")

    if request.method == 'POST':
        if 'pay_adjusted' in request.POST:
            config = SiteConfiguration.load()
            if not config.account_user_commission_authority:
                messages.error(request, "Commission disbursement is disabled for Account Users in Site Configuration.")
                return redirect('betting:account_user_dashboard')

            comm_key = (request.POST.get('pay_adjusted') or '').strip()
            selected_period_raw = (request.POST.get('commission_period') or '').strip()
            selected_period_id = None
            if selected_period_raw:
                try:
                    selected_period_id = int(selected_period_raw)
                except Exception:
                    selected_period_id = None

            if not selected_period_id:
                messages.error(request, "Please select a commission period before paying.")
                return redirect('betting:account_user_dashboard')

            amount_raw = request.POST.get(f"adjusted_amount_{comm_key}")
            try:
                amount = Decimal(str(amount_raw))
            except Exception:
                messages.error(request, "Invalid adjusted commission amount.")
                return redirect('betting:account_user_dashboard')

            try:
                comm_type, comm_id = comm_key.split('_', 1)
                if comm_type == 'weekly':
                    comm = WeeklyAgentCommission.objects.get(id=comm_id)
                    if comm.period_id != selected_period_id:
                        raise InvalidOperation("Selected item does not match the selected commission period.")
                    success, msg = pay_weekly_commission_amount(comm, amount, actor=request.user)
                elif comm_type == 'monthly':
                    comm = MonthlyNetworkCommission.objects.get(id=comm_id)
                    if comm.period_id != selected_period_id:
                        raise InvalidOperation("Selected item does not match the selected commission period.")
                    success, msg = pay_monthly_network_commission_amount(comm, amount, actor=request.user)
                else:
                    success, msg = False, "Invalid type"
            except Exception as e:
                success, msg = False, str(e)

            if success:
                messages.success(request, msg)
            else:
                messages.error(request, msg)

            qd = QueryDict(mutable=True)
            qd['commission_period'] = str(selected_period_id)
            qd['section'] = 'commissions'
            commission_search_post = (request.POST.get('commission_search') or '').strip()
            if commission_search_post:
                qd['commission_search'] = commission_search_post
            return redirect(f"{reverse('betting:account_user_dashboard')}?{qd.urlencode()}")

        if 'pay_commissions' in request.POST:
            config = SiteConfiguration.load()
            if not config.account_user_commission_authority:
                messages.error(request, "Commission disbursement is disabled for Account Users in Site Configuration.")
                return redirect('betting:account_user_dashboard')

            selected_items = request.POST.getlist('selected_commissions')
            selected_period_raw = (request.POST.get('commission_period') or '').strip()
            selected_period_id = None
            if selected_period_raw:
                try:
                    selected_period_id = int(selected_period_raw)
                except Exception:
                    selected_period_id = None

            if not selected_period_id:
                messages.error(request, "Please select a commission period before paying.")
                return redirect('betting:account_user_dashboard')

            success_count = 0
            error_count = 0
            
            for item in selected_items:
                try:
                    comm_type, comm_id = item.split('_')
                    if comm_type == 'weekly':
                        comm = WeeklyAgentCommission.objects.get(id=comm_id)
                        if comm.period_id != selected_period_id:
                            raise InvalidOperation("Selected item does not match the selected commission period.")
                        success, msg = pay_weekly_commission(comm, actor=request.user)
                    elif comm_type == 'monthly':
                        comm = MonthlyNetworkCommission.objects.get(id=comm_id)
                        if comm.period_id != selected_period_id:
                            raise InvalidOperation("Selected item does not match the selected commission period.")
                        success, msg = pay_monthly_network_commission(comm, actor=request.user)
                    else:
                        success, msg = False, "Invalid type"
                    
                    if success:
                        success_count += 1
                    else:
                        error_count += 1
                        messages.error(request, f"Error paying {item}: {msg}")
                except Exception as e:
                    error_count += 1
                    messages.error(request, f"Error processing {item}: {str(e)}")
            
            if success_count > 0:
                messages.success(request, f"Successfully paid {success_count} commissions.")
            qd = QueryDict(mutable=True)
            qd['commission_period'] = str(selected_period_id)
            qd['section'] = 'commissions'
            commission_search_post = (request.POST.get('commission_search') or '').strip()
            if commission_search_post:
                qd['commission_search'] = commission_search_post
            return redirect(f"{reverse('betting:account_user_dashboard')}?{qd.urlencode()}")

        elif 'process_withdrawals' in request.POST:
            selected_withdrawals = request.POST.getlist('selected_withdrawals')
            action = request.POST.get('withdrawal_action')
            
            success_count = 0
            for w_id in selected_withdrawals:
                try:
                    with db_transaction.atomic():
                        withdrawal = UserWithdrawal.objects.select_for_update().get(id=w_id)
                        
                        if withdrawal.status == 'pending':
                            if action == 'mark_paid':
                                # 1. Capture Audit Data (Balance Snapshot)
                                try:
                                    user_wallet = Wallet.objects.select_for_update().get(user=withdrawal.user)
                                    withdrawal.balance_after = user_wallet.balance 
                                    withdrawal.balance_before = user_wallet.balance + withdrawal.amount
                                except Wallet.DoesNotExist:
                                    pass

                                withdrawal.status = 'completed'
                                withdrawal.approved_rejected_by = request.user
                                withdrawal.approved_rejected_time = timezone.now()
                                withdrawal.processed_ip = request.META.get('REMOTE_ADDR')
                                withdrawal.save()

                                # 2. Credit Account User (Processor) Wallet
                                # Assumption: Account User paid cash, system reimburses them.
                                processor_wallet = Wallet.objects.select_for_update().get(user=request.user)
                                
                                # Capture Approver Balances
                                withdrawal.approver_balance_before = processor_wallet.balance
                                tx = Transaction.objects.create(
                                    user=request.user,
                                    initiating_user=request.user,
                                    transaction_type='account_user_credit',
                                    amount=withdrawal.amount,
                                    status='completed',
                                    is_successful=True,
                                    description=f"Reimbursement for processing withdrawal {withdrawal.id} for {withdrawal.user.email}",
                                    timestamp=timezone.now()
                                )
                                before, after = processor_wallet.apply_delta(
                                    amount=withdrawal.amount,
                                    actor=request.user,
                                    transaction_obj=tx,
                                    reference=str(withdrawal.id),
                                    reason=tx.description,
                                    metadata={"withdrawal_id": withdrawal.id, "source": "withdrawal_reimbursement"},
                                )
                                withdrawal.approver_balance_before = before
                                withdrawal.approver_balance_after = after
                                withdrawal.save(update_fields=["approver_balance_before", "approver_balance_after"])

                                success_count += 1

                            elif action == 'reject':
                                withdrawal.status = 'rejected'
                                withdrawal.approved_rejected_by = request.user
                                withdrawal.approved_rejected_time = timezone.now()
                                withdrawal.processed_ip = request.META.get('REMOTE_ADDR')
                                withdrawal.save() # Signal handles refund
                                success_count += 1

                except UserWithdrawal.DoesNotExist:
                    pass
                except Exception as e:
                    messages.error(request, f"Error processing withdrawal {w_id}: {e}")
            
            if success_count > 0:
                messages.success(request, f"Successfully processed {success_count} withdrawals.")
            return redirect('betting:account_user_dashboard')

        elif 'search_user' in request.POST:
            search_form = AccountUserSearchForm(request.POST)
            if search_form.is_valid():
                search_term = (search_form.cleaned_data.get('search_term') or '').strip()

                base_qs = User.objects.filter(
                    Q(email__icontains=search_term) |
                    Q(phone_number__icontains=search_term) |
                    Q(username__icontains=search_term) |
                    Q(first_name__icontains=search_term) |
                    Q(last_name__icontains=search_term) |
                    Q(other_name__icontains=search_term)
                ).exclude(is_superuser=True).exclude(user_type='account_user')

                tokens = [t for t in re.split(r'\s+', search_term) if t]
                name_qs = User.objects.none()
                if len(tokens) > 1 and '@' not in search_term:
                    tokens_q = Q()
                    for t in tokens:
                        tokens_q &= (Q(first_name__icontains=t) | Q(last_name__icontains=t) | Q(other_name__icontains=t))
                    name_qs = User.objects.filter(tokens_q).exclude(is_superuser=True).exclude(user_type='account_user')

                users = (base_qs | name_qs)
                
                if search_term.isdigit():
                     # Prioritize exact ID match if search term is a digit (likely from autocomplete)
                     exact_match = User.objects.filter(id=int(search_term)).exclude(is_superuser=True).first()
                     if exact_match:
                         found_user = exact_match
                         users = User.objects.filter(pk=exact_match.pk)
                     else:
                         users = users | User.objects.filter(id=int(search_term)).exclude(is_superuser=True).exclude(user_type='account_user')

                users = users.distinct()
                
                if found_user:
                     pass # Already found via exact ID match
                elif users.count() == 1:
                    found_user = users.first()
                    messages.success(request, f"User found: {found_user.get_full_name()} ({found_user.email})")
                elif users.count() > 1:
                    search_results = users
                    messages.warning(request, "Multiple users found. Please select one.")
                else:
                    messages.error(request, "No user found.")
        
        elif 'perform_action' in request.POST:
            action_form = AccountUserWalletActionForm(request.POST)
            target_user_id = request.POST.get('target_user_id')
            if target_user_id:
                target_user = get_object_or_404(User, id=target_user_id)
                
                if target_user.is_superuser or target_user.user_type == 'account_user':
                    messages.error(request, "Operation not allowed on this user.")
                    return redirect('betting:account_user_dashboard')

                if action_form.is_valid():
                    action = action_form.cleaned_data['action']
                    amount = action_form.cleaned_data['amount']
                    description = action_form.cleaned_data['description']
                    
                    try:
                        with db_transaction.atomic():
                            account_wallet = Wallet.objects.select_for_update().get(user=request.user)
                            target_wallet = Wallet.objects.select_for_update().get(user=target_user)
                            
                            if action == 'credit':
                                if account_wallet.balance < amount:
                                    raise InvalidOperation("Insufficient funds in your account wallet.")
                                tx_type_account = 'account_user_debit'
                                tx_type_target = 'account_user_credit'

                            elif action == 'debit':
                                if target_wallet.balance < amount:
                                    raise InvalidOperation("User has insufficient funds.")
                                tx_type_account = 'account_user_credit'
                                tx_type_target = 'account_user_debit'
                            
                            tx_account = Transaction.objects.create(
                                user=request.user,
                                initiating_user=request.user,
                                transaction_type=tx_type_account,
                                amount=amount,
                                status='completed',
                                is_successful=True,
                                description=f"{action.title()} for user {target_user.email}: {description}"
                            )
                            
                            tx_target = Transaction.objects.create(
                                user=target_user,
                                initiating_user=request.user,
                                transaction_type=tx_type_target,
                                amount=amount,
                                status='completed',
                                is_successful=True,
                                description=f"{action.title()} by Account Manager: {description}"
                            )
                            
                            if action == 'credit':
                                account_wallet.apply_delta(
                                    amount=-amount,
                                    actor=request.user,
                                    transaction_obj=tx_account,
                                    reference=str(tx_account.id),
                                    reason=tx_account.description,
                                    metadata={"target_user_id": target_user.id, "source": "account_user_dashboard"},
                                )
                                credit_result = apply_repayment_and_credit_wallet(
                                    user=target_user,
                                    amount=amount,
                                    source='account_user_credit',
                                    actor=request.user,
                                    transaction_obj=tx_target,
                                    reference=str(tx_account.id),
                                    reason=tx_target.description,
                                    metadata={"account_user_id": request.user.id, "source": "account_user_dashboard"},
                                )
                            elif action == 'debit':
                                target_wallet.apply_delta(
                                    amount=-amount,
                                    actor=request.user,
                                    transaction_obj=tx_target,
                                    reference=str(tx_account.id),
                                    reason=tx_target.description,
                                    metadata={"account_user_id": request.user.id, "source": "account_user_dashboard"},
                                )
                                account_wallet.apply_delta(
                                    amount=amount,
                                    actor=request.user,
                                    transaction_obj=tx_account,
                                    reference=str(tx_account.id),
                                    reason=tx_account.description,
                                    metadata={"target_user_id": target_user.id, "source": "account_user_dashboard"},
                                )
                            
                            # Log to Admin Activity Log
                            log_admin_activity(
                                request, 
                                f"Account User Manual {action} of {amount} for {target_user.email}. Reason: {description}",
                                action_type=f"MANUAL_{action.upper()}",
                                affected_object=target_user.email
                            )

                            messages.success(request, f"Successfully {action}ed {amount} for {target_user.email}.")
                            if action == 'credit':
                                messages.info(
                                    request,
                                    f"Wallet credit: ₦{credit_result.get('wallet_credit_amount') or Decimal('0.00')}. "
                                    f"Reserved new credit: ₦{credit_result.get('pending_credit_amount') or Decimal('0.00')}."
                                )
                            found_user = None 
                            
                    except InvalidOperation as e:
                        messages.error(request, str(e))
                    except Exception as e:
                        messages.error(request, f"An error occurred: {str(e)}")
                        logger.error(f"Account User Action Error: {traceback.format_exc()}")
            else:
                 messages.error(request, "Target user not specified.")

    try:
        from commission.models import CommissionPeriod as CommissionPeriodModel, AgentCommissionProfile
        from commission.services import calculate_weekly_agent_commission

        recent_weekly_periods = list(CommissionPeriodModel.objects.filter(period_type='weekly').order_by('-start_date')[:12])
        for period in recent_weekly_periods:
            agent_ids = (
                BetTicket.objects.filter(
                    user__agent__isnull=False,
                    placed_at__date__gte=period.start_date,
                    placed_at__date__lte=period.end_date,
                )
                .exclude(status__in=['pending', 'cancelled', 'deleted'])
                .values_list('user__agent_id', flat=True)
                .distinct()
            )
            prof_agent_ids = AgentCommissionProfile.objects.filter(is_active=True, user_id__in=agent_ids).values_list('user_id', flat=True)
            for agent in User.objects.filter(id__in=prof_agent_ids):
                calculate_weekly_agent_commission(agent, period)
    except Exception:
        pass

    # Fetch Pending Commissions
    pending_weekly_base = WeeklyAgentCommission.objects.filter(status__in=['pending', 'approved', 'partially_paid']).select_related('agent', 'period').order_by('-period__start_date')
    pending_monthly_base = MonthlyNetworkCommission.objects.filter(status__in=['pending', 'approved', 'partially_paid']).select_related('user', 'period').order_by('-period__start_date')

    commission_period_options = []
    selected_commission_period_id = None
    try:
        from commission.models import CommissionPeriod as CommissionPeriodModel
        period_ids = set(pending_weekly_base.values_list('period_id', flat=True).distinct()) | set(pending_monthly_base.values_list('period_id', flat=True).distinct())
        if period_ids:
            commission_period_options = list(CommissionPeriodModel.objects.filter(id__in=period_ids).order_by('-start_date'))
        if commission_period_id_raw:
            try:
                selected_commission_period_id = int(commission_period_id_raw)
            except Exception:
                selected_commission_period_id = None
        if selected_commission_period_id is None and commission_period_options:
            selected_commission_period_id = commission_period_options[0].id
    except Exception:
        commission_period_options = []
        selected_commission_period_id = None

    pending_weekly = pending_weekly_base
    pending_monthly = pending_monthly_base

    if selected_commission_period_id:
        pending_weekly = pending_weekly.filter(period_id=selected_commission_period_id)
        pending_monthly = pending_monthly.filter(period_id=selected_commission_period_id)

    if commission_search:
        weekly_q = (
            Q(agent__email__icontains=commission_search) |
            Q(agent__username__icontains=commission_search) |
            Q(agent__phone_number__icontains=commission_search) |
            Q(agent__first_name__icontains=commission_search) |
            Q(agent__last_name__icontains=commission_search) |
            Q(agent__other_name__icontains=commission_search)
        )
        monthly_q = (
            Q(user__email__icontains=commission_search) |
            Q(user__username__icontains=commission_search) |
            Q(user__phone_number__icontains=commission_search) |
            Q(user__first_name__icontains=commission_search) |
            Q(user__last_name__icontains=commission_search) |
            Q(user__other_name__icontains=commission_search)
        )
        pending_weekly = pending_weekly.filter(weekly_q)
        pending_monthly = pending_monthly.filter(monthly_q)
    
    try:
        from commission.services import calculate_weekly_agent_commission_data, calculate_monthly_network_commission_data
    except Exception:
        calculate_weekly_agent_commission_data = None
        calculate_monthly_network_commission_data = None

    if calculate_weekly_agent_commission_data:
        for wc in pending_weekly:
            data = calculate_weekly_agent_commission_data(wc.agent, wc.period)
            if not data:
                continue
            data = dict(data)
            data.pop('is_live_period', None)
            changed = False
            for field, value in data.items():
                if getattr(wc, field) != value:
                    setattr(wc, field, value)
                    changed = True
            if changed:
                wc.save(update_fields=list(data.keys()))

    if calculate_monthly_network_commission_data:
        for mc in pending_monthly:
            data = calculate_monthly_network_commission_data(mc.user, mc.period)
            if not data:
                continue
            changed = False
            for field, value in data.items():
                if getattr(mc, field) != value:
                    setattr(mc, field, value)
                    changed = True
            if changed:
                mc.save(update_fields=list(data.keys()))

    pending_commissions = []
    for wc in pending_weekly:
        pending_commissions.append({
            'id_str': f"weekly_{wc.id}",
            'type': 'Weekly',
            'user': wc.agent,
            'period': wc.period,
            'amount': (wc.commission_total_amount or Decimal('0.00')) - (wc.amount_paid or Decimal('0.00')),
            'ggr_ngr': wc.ggr,
            'status': wc.status,
        })
    
    for mc in pending_monthly:
        pending_commissions.append({
            'id_str': f"monthly_{mc.id}",
            'type': f"Monthly ({mc.role.replace('_', ' ').title()})",
            'user': mc.user,
            'period': mc.period,
            'amount': (mc.commission_amount or Decimal('0.00')) - (mc.amount_paid or Decimal('0.00')),
            'ggr_ngr': mc.ngr,
            'status': mc.status,
        })

    # Fetch Pending Withdrawals
    all_pending_withdrawals = UserWithdrawal.objects.filter(status='pending').select_related('user').order_by('request_time')
    withdrawals_paginator = Paginator(all_pending_withdrawals, 10)
    withdrawals_page = request.GET.get('withdrawals_page')
    try:
        pending_withdrawals = withdrawals_paginator.page(withdrawals_page)
    except PageNotAnInteger:
        pending_withdrawals = withdrawals_paginator.page(1)
    except EmptyPage:
        pending_withdrawals = withdrawals_paginator.page(withdrawals_paginator.num_pages)
        
    # Paginate Pending Commissions (List)
    commissions_paginator = Paginator(pending_commissions, 10)
    commissions_page = request.GET.get('commissions_page')
    try:
        pending_commissions_page = commissions_paginator.page(commissions_page)
    except PageNotAnInteger:
        pending_commissions_page = commissions_paginator.page(1)
    except EmptyPage:
        pending_commissions_page = commissions_paginator.page(commissions_paginator.num_pages)

    # Construct Activity Log if user found
    if found_user:
        user_transactions = Transaction.objects.filter(user=found_user).order_by('-timestamp')
        user_bets = BetTicket.objects.filter(user=found_user).order_by('-placed_at')
        
        for t in user_transactions:
            activity_log.append({
                'timestamp': t.timestamp,
                'type': 'Transaction',
                'description': t.description or t.get_transaction_type_display(),
                'amount': t.amount,
                'status': t.status,
                'ref': t.external_reference or t.paystack_reference or str(t.id)[:8],
                'is_credit': t.transaction_type in ['deposit', 'bet_payout', 'commission_payout', 'wallet_transfer_in', 'bonus', 'withdrawal_refund', 'account_user_credit']
            })
            
        for b in user_bets:
             activity_log.append({
                'timestamp': b.placed_at,
                'type': 'Bet Placement',
                'description': f"{b.get_bet_type_display().title()} Bet ({b.selections.count()} selections)",
                'amount': b.stake_amount,
                'status': b.status,
                'ref': b.ticket_id,
                'is_credit': False # Bets are debits (stakes)
            })
            
        activity_log.sort(key=lambda x: x['timestamp'], reverse=True)

    # --- Bet Ticket Management ---
    ticket_search_query = request.GET.get('ticket_search', '').strip()
    ticket_status_filter = request.GET.get('ticket_status', '').strip()
    ticket_date_from = request.GET.get('ticket_date_from', '').strip()
    ticket_date_to = request.GET.get('ticket_date_to', '').strip()
    bet_q = (request.GET.get('bet_q') or ticket_search_query).strip()
    bet_status = (request.GET.get('bet_status') or ticket_status_filter).strip()
    bet_agent_id = (request.GET.get('bet_agent') or '').strip()

    all_tickets = BetTicket.objects.filter(
        placed_at__gte=metrics_start_dt,
        placed_at__lte=metrics_end_dt,
    )

    if ticket_date_from:
        try:
            date_from = datetime.strptime(ticket_date_from, '%Y-%m-%d')
            all_tickets = all_tickets.filter(placed_at__gte=timezone.make_aware(date_from))
        except ValueError:
            pass

    if ticket_date_to:
        try:
            date_to = datetime.strptime(ticket_date_to, '%Y-%m-%d')
            date_to = date_to.replace(hour=23, minute=59, second=59)
            all_tickets = all_tickets.filter(placed_at__lte=timezone.make_aware(date_to))
        except ValueError:
            pass

    tickets_page, ticket_agent_filter_options = build_dashboard_bets_page(
        all_tickets,
        bet_q=bet_q,
        bet_status=bet_status,
        bet_agent_id=bet_agent_id,
        page_number=(request.GET.get('bets_page') or request.GET.get('tickets_page') or 1),
    )

    # --- Wallets Management ---
    wallet_search = request.GET.get('wallet_search', '')
    all_wallets = Wallet.objects.select_related('user').all().order_by('-balance')
    
    if wallet_search:
        all_wallets = all_wallets.filter(
            Q(user__email__icontains=wallet_search) | 
            Q(user__first_name__icontains=wallet_search) | 
            Q(user__last_name__icontains=wallet_search)
        )

    wallets_paginator = Paginator(all_wallets, 20)
    wallets_page_num = request.GET.get('wallets_page')
    try:
        wallets_page = wallets_paginator.page(wallets_page_num)
    except PageNotAnInteger:
        wallets_page = wallets_paginator.page(1)
    except EmptyPage:
        wallets_page = wallets_paginator.page(wallets_paginator.num_pages)

    # --- Transactions Management ---
    txn_search = request.GET.get('txn_search', '')
    txn_type_filter = request.GET.get('txn_type_filter', '')
    all_transactions = Transaction.objects.select_related('user', 'initiating_user').all().order_by('-timestamp')

    if txn_search:
        all_transactions = all_transactions.filter(
            Q(transaction_id__icontains=txn_search) |
            Q(user__email__icontains=txn_search) |
            Q(user__first_name__icontains=txn_search) |
            Q(user__last_name__icontains=txn_search)
        )
    
    if txn_type_filter:
        all_transactions = all_transactions.filter(transaction_type=txn_type_filter)

    transactions_paginator = Paginator(all_transactions, 20)
    transactions_page_num = request.GET.get('transactions_page')
    try:
        transactions_page = transactions_paginator.page(transactions_page_num)
    except PageNotAnInteger:
        transactions_page = transactions_paginator.page(1)
    except EmptyPage:
        transactions_page = transactions_paginator.page(transactions_paginator.num_pages)

    # --- Processed Withdrawals Management ---
    pw_search = request.GET.get('pw_search', '')
    pw_status_filter = request.GET.get('pw_status_filter', '')
    all_processed_withdrawals = ProcessedWithdrawal.objects.filter(status__in=['approved', 'completed', 'rejected']).order_by('-approved_rejected_time')

    if pw_search:
        all_processed_withdrawals = all_processed_withdrawals.filter(
            Q(user__email__icontains=pw_search) |
            Q(user__first_name__icontains=pw_search) |
            Q(user__last_name__icontains=pw_search) |
            Q(bank_name__icontains=pw_search) |
            Q(account_number__icontains=pw_search)
        )
    
    if pw_status_filter:
        all_processed_withdrawals = all_processed_withdrawals.filter(status=pw_status_filter)

    pw_paginator = Paginator(all_processed_withdrawals, 20)
    pw_page_num = request.GET.get('processed_withdrawals_page')
    try:
        processed_withdrawals_page = pw_paginator.page(pw_page_num)
    except PageNotAnInteger:
        processed_withdrawals_page = pw_paginator.page(1)
    except EmptyPage:
        processed_withdrawals_page = pw_paginator.page(pw_paginator.num_pages)

    context = {
        'account_kpis': account_kpis,
        'kpis': kpis,
        'charts_data': charts_data,
        'metrics_label': metrics_label,
        'metrics_start': metrics_start_date.isoformat(),
        'metrics_end': metrics_end_date.isoformat(),
        'start_date': start_date_str,
        'end_date': end_date_str,
        'wallets_page': wallets_page,
        'wallet_search': wallet_search,
        'transactions_page': transactions_page,
        'txn_search': txn_search,
        'txn_type_filter': txn_type_filter,
        'processed_withdrawals_page': processed_withdrawals_page,
        'pw_search': pw_search,
        'pw_status_filter': pw_status_filter,
        'tickets_page': tickets_page,
        'bet_tickets_page': tickets_page,
        'bet_q': bet_q,
        'bet_status': bet_status,
        'bet_agent': bet_agent_id,
        'agent_filter_options': ticket_agent_filter_options,
        'ticket_search_query': ticket_search_query,
        'ticket_status_filter': ticket_status_filter,
        'ticket_date_from': ticket_date_from,
        'ticket_date_to': ticket_date_to,
        'incoming_credit_requests': incoming_credit_requests, # NEW
        'crm_wallet_approval_requests': crm_wallet_approval_requests,
        'active_loans_given': active_loans_given,         # NEW
        'pending_withdrawals': pending_withdrawals,
        'search_form': search_form,
        'action_form': action_form,
        'found_user': found_user,
        'search_results': search_results,
        'activity_log': activity_log,
        'recent_transactions': recent_transactions,
        'wallet': request.user.wallet,
        'pending_commissions': pending_commissions_page, # Paginated
        'commission_period_options': commission_period_options,
        'selected_commission_period_id': selected_commission_period_id,
        'commission_search': commission_search,
        'top_period_options': top_period_options,
        'selected_top_period_id': selected_top_period_id,
        **overdraft_reporting_context,
    }
    return render(request, 'betting/account_user_dashboard.html', context)


@login_required
@user_passes_test(is_account_user)
def account_user_activity_feed(request):
    limit = 20
    tickets = (
        BetTicket.objects.exclude(status__in=['deleted', 'cancelled'])
        .select_related('user')
        .order_by('-placed_at')[:10]
    )
    txs = (
        Transaction.objects.filter(status='completed', is_successful=True)
        .select_related('user')
        .order_by('-timestamp')[:10]
    )
    withdrawals = (
        UserWithdrawal.objects.select_related('user')
        .order_by('-request_time')[:10]
    )

    events = []
    for t in tickets:
        events.append({
            'ts': t.placed_at.isoformat() if getattr(t, 'placed_at', None) else '',
            'type': 'bet',
            'user': getattr(getattr(t, 'user', None), 'email', '') or getattr(getattr(t, 'user', None), 'username', '') or '-',
            'label': f"Bet placed ({t.ticket_id or ''})".strip(),
            'amount': str(getattr(t, 'stake_amount', Decimal('0.00'))),
            'status': t.status,
        })

    for tx in txs:
        events.append({
            'ts': tx.timestamp.isoformat() if getattr(tx, 'timestamp', None) else '',
            'type': 'transaction',
            'user': getattr(getattr(tx, 'user', None), 'email', '') or getattr(getattr(tx, 'user', None), 'username', '') or '-',
            'label': tx.transaction_type,
            'amount': str(getattr(tx, 'amount', Decimal('0.00'))),
            'status': tx.status,
        })

    for w in withdrawals:
        events.append({
            'ts': w.request_time.isoformat() if getattr(w, 'request_time', None) else '',
            'type': 'withdrawal',
            'user': getattr(getattr(w, 'user', None), 'email', '') or getattr(getattr(w, 'user', None), 'username', '') or '-',
            'label': 'Withdrawal request',
            'amount': str(getattr(w, 'amount', Decimal('0.00'))),
            'status': w.status,
        })

    events.sort(key=lambda e: e.get('ts') or '', reverse=True)
    return JsonResponse({'events': events[:limit]})


@login_required
def overdraft_report_detail(request, loan_id):
    if not can_view_overdraft_reporting(request.user):
        return HttpResponse("Permission Denied", status=403)
    context = _build_overdraft_detail_context(request.user, get_object_or_404(Loan, id=loan_id))
    context['back_url'] = (request.GET.get('next') or '').strip()
    return render(request, 'betting/overdraft_report_detail.html', context)


@login_required
@user_passes_test(is_account_user)
def account_user_export(request):
    dataset = (request.GET.get('dataset') or '').strip().lower()
    fmt = (request.GET.get('format') or 'csv').strip().lower()
    if dataset != 'overdrafts':
        return HttpResponseBadRequest("Unknown dataset")
    report_context = _build_overdraft_reporting_dashboard_context(
        request,
        include_retail_manager=True,
        extra_params={'section': 'overdraft_center'},
    )
    rows = _loan_reporting_export_rows(
        report_context['overdraft_reporting']['rows'],
        include_retail_manager=True,
    )
    return _export_simple_rows(rows=rows, title='account_user_overdraft_center', fmt=fmt)

def build_weekly_commission_dashboard_rows(agent_qs, selected_period_id_raw=''):
    commission_rows = []
    commission_period_options = []
    selected_commission_period_id = ''
    try:
        CommissionPeriod = apps.get_model('commission', 'CommissionPeriod')
        WeeklyAgentCommission = apps.get_model('commission', 'WeeklyAgentCommission')
        from commission.services import calculate_weekly_agent_commission_data

        period_qs = CommissionPeriod.objects.filter(period_type='weekly').order_by('-start_date')
        commission_period_options = list(period_qs[:200])

        selected_period = None
        if selected_period_id_raw:
            try:
                selected_period = CommissionPeriod.objects.filter(
                    id=int(selected_period_id_raw),
                    period_type='weekly',
                ).first()
            except Exception:
                selected_period = None

        if selected_period is None:
            selected_period = period_qs.first()

        selected_commission_period_id = str(selected_period.id) if selected_period else ''

        comm_qs = WeeklyAgentCommission.objects.filter(agent__in=agent_qs).select_related('agent', 'period')
        if selected_period:
            comm_qs = comm_qs.filter(period=selected_period)

        comm_map = {rec.agent_id: rec for rec in comm_qs}
        for ag in agent_qs.only('id', 'username', 'email', 'phone_number').order_by('username', 'email'):
            rec = comm_map.get(ag.id)
            calc = calculate_weekly_agent_commission_data(ag, selected_period) if selected_period else None
            calc_total = calc.get('commission_total_amount') if isinstance(calc, dict) else None
            if calc_total is None:
                calc_total = getattr(rec, 'commission_total_amount', None) if rec else None
            if calc_total is None:
                calc_total = Decimal('0.00')

            commission_rows.append({
                'agent_id': ag.id,
                'agent_username': (ag.username or '').strip() or (ag.email or '').strip() or '-',
                'agent_phone_number': (ag.phone_number or '').strip() or '-',
                'total': calc_total,
                'partially_paid': getattr(rec, 'amount_paid', Decimal('0.00')) if rec else Decimal('0.00'),
                'status': getattr(rec, 'status', 'pending') if rec else 'pending',
            })
    except Exception:
        commission_rows = []
        commission_period_options = []
        selected_commission_period_id = ''

    return commission_rows, commission_period_options, selected_commission_period_id

def build_dashboard_bets_page(base_qs, bet_q='', bet_status='', bet_agent_id='', page_number=1):
    bets_qs = (
        base_qs.exclude(status__in=['deleted'])
        .select_related('user', 'user__agent', 'user__super_agent', 'user__master_agent')
        .order_by('-placed_at')
    )

    if bet_q:
        bets_qs = bets_qs.filter(
            Q(ticket_id__icontains=bet_q) |
            Q(user__email__icontains=bet_q) |
            Q(user__username__icontains=bet_q) |
            Q(user__phone_number__icontains=bet_q)
        )
    if bet_status:
        bets_qs = bets_qs.filter(status=bet_status)
    if bet_agent_id:
        try:
            bets_qs = bets_qs.filter(user__agent_id=int(bet_agent_id))
        except Exception:
            pass

    paginator = Paginator(bets_qs, 50)
    try:
        bets_page = paginator.page(page_number or 1)
    except Exception:
        bets_page = paginator.page(1)

    agent_filter_options = list(
        User.objects.filter(user_type__in=['agent', 'super_agent', 'master_agent'])
        .only('id', 'email', 'username')
        .order_by('email')[:200]
    )
    return bets_page, agent_filter_options

def build_top_fixtures_by_betting_period(selected_period_id_raw=''):
    top_period_options = list(BettingPeriod.objects.order_by('-start_date')[:200])
    selected_top_period_id = ''
    selected_period = None

    if selected_period_id_raw:
        try:
            selected_period = BettingPeriod.objects.filter(id=int(selected_period_id_raw)).first()
        except Exception:
            selected_period = None

    if selected_period is None and top_period_options:
        selected_period = top_period_options[0]

    if selected_period:
        selected_top_period_id = str(selected_period.id)

    top_fixtures = []
    if selected_period:
        selection_top = (
            Selection.objects.filter(
                Q(betting_period=selected_period) |
                Q(betting_period__isnull=True, fixture__betting_period=selected_period)
            )
            .values('fixture_home_team', 'fixture_away_team')
            .annotate(picks=Count('id'))
            .order_by('-picks')[:5]
        )
        top_fixtures = [
            {
                'label': f"{(row.get('fixture_home_team') or '').strip()} vs {(row.get('fixture_away_team') or '').strip()}".strip() or 'Fixture',
                'picks': int(row['picks'] or 0),
            }
            for row in selection_top
        ]

    return top_fixtures, top_period_options, selected_top_period_id

@login_required
def agent_remapping(request):
    if not crm_can_remap_agents(request.user):
        return HttpResponse("Permission Denied", status=403)

    subtab = ((request.POST.get('subtab') if request.method == 'POST' else request.GET.get('subtab')) or 'remap').strip() or 'remap'
    q = (request.GET.get('q') or '').strip()
    history_q = (request.GET.get('history_q') or '').strip()
    current_super_agent_id = ((request.POST.get('current_super_agent') if request.method == 'POST' else request.GET.get('current_super_agent')) or '').strip()
    destination_super_agent_id = ((request.POST.get('destination_super_agent') if request.method == 'POST' else request.GET.get('destination_super_agent')) or '').strip()
    history_old_super_agent_id = (request.GET.get('history_old_super_agent') or '').strip()
    history_new_super_agent_id = (request.GET.get('history_new_super_agent') or '').strip()
    start_date_str = (request.GET.get('start_date') or '').strip()
    end_date_str = (request.GET.get('end_date') or '').strip()

    start_dt = None
    end_dt = None
    if start_date_str:
        try:
            start_dt = timezone.make_aware(datetime.combine(datetime.strptime(start_date_str, '%Y-%m-%d').date(), datetime.min.time()))
        except Exception:
            start_dt = None
    if end_date_str:
        try:
            end_dt = timezone.make_aware(datetime.combine(datetime.strptime(end_date_str, '%Y-%m-%d').date(), datetime.max.time()))
        except Exception:
            end_dt = None

    current_super_agents_qs = User.objects.filter(user_type='super_agent').select_related('master_agent').order_by('username', 'email')
    destination_super_agents_qs = current_super_agents_qs.filter(is_active=True)

    agent_rows_qs = User.objects.filter(user_type='agent').select_related('super_agent', 'master_agent', 'wallet').order_by('username', 'email')
    if current_super_agent_id.isdigit():
        agent_rows_qs = agent_rows_qs.filter(super_agent_id=int(current_super_agent_id))
    else:
        agent_rows_qs = agent_rows_qs.none()
    if q:
        agent_rows_qs = agent_rows_qs.filter(
            Q(username__icontains=q) |
            Q(email__icontains=q) |
            Q(phone_number__icontains=q) |
            Q(first_name__icontains=q) |
            Q(last_name__icontains=q) |
            Q(other_name__icontains=q)
        )
    remap_agent_rows = list(agent_rows_qs[:500])

    remap_form = AgentRemapForm(
        current_super_agent_qs=current_super_agents_qs,
        destination_super_agent_qs=destination_super_agents_qs,
        agent_queryset=agent_rows_qs,
        initial={
            'current_super_agent': current_super_agent_id or None,
            'destination_super_agent': destination_super_agent_id or None,
        },
    )

    if request.method == 'POST' and request.POST.get('action') == 'transfer_agents':
        remap_form = AgentRemapForm(
            request.POST,
            current_super_agent_qs=current_super_agents_qs,
            destination_super_agent_qs=destination_super_agents_qs,
            agent_queryset=agent_rows_qs,
        )
        subtab = 'remap'
        if remap_form.is_valid():
            current_super_agent = remap_form.cleaned_data['current_super_agent']
            destination_super_agent = remap_form.cleaned_data['destination_super_agent']
            remarks = remap_form.cleaned_data.get('remarks') or ''
            selected_agents = list(
                remap_form.cleaned_data['agents'].select_related('super_agent', 'master_agent').order_by('username', 'email')
            )
            try:
                with db_transaction.atomic():
                    for agent in selected_agents:
                        if agent.super_agent_id != current_super_agent.id:
                            raise ValueError('One or more selected agents no longer belong to the chosen Current Super Agent.')
                        if agent.super_agent_id == destination_super_agent.id:
                            raise ValueError('Agent already belongs to this Super Agent.')

                        old_super_agent = agent.super_agent
                        agent.super_agent = destination_super_agent
                        agent.master_agent = destination_super_agent.master_agent
                        agent.save()

                        AgentTransferLog.objects.create(
                            agent=agent,
                            old_super_agent=old_super_agent,
                            new_super_agent=destination_super_agent,
                            transferred_by=request.user,
                            remarks=remarks,
                        )

                        if old_super_agent:
                            create_notification(
                                recipient=old_super_agent,
                                notification_type='SYSTEM_ANNOUNCEMENT',
                                title='Agent Removed From Downline',
                                message=f"{agent.get_full_name() or agent.username or agent.email} has been removed from your downline and reassigned by Management.",
                                data={
                                    'popup_category': 'message',
                                    'delivery_channel': 'in_app',
                                    'url': reverse('betting:agent_remapping'),
                                },
                            )
                        create_notification(
                            recipient=destination_super_agent,
                            notification_type='SYSTEM_ANNOUNCEMENT',
                            title='New Agent Assigned',
                            message=f"{agent.get_full_name() or agent.username or agent.email} has been assigned to your downline by Management.",
                            data={
                                'popup_category': 'message',
                                'delivery_channel': 'in_app',
                                'url': reverse('betting:agent_remapping'),
                            },
                        )
                        create_notification(
                            recipient=agent,
                            notification_type='SYSTEM_ANNOUNCEMENT',
                            title='Super Agent Reassigned',
                            message='Your account has been reassigned to a new Super Agent. Your operations, wallet, tickets and commissions remain unaffected.',
                            data={
                                'popup_category': 'message',
                                'delivery_channel': 'in_app',
                                'url': reverse('betting:user_dashboard'),
                            },
                        )
                messages.success(request, f"{len(selected_agents)} agent(s) transferred successfully.")
                query = QueryDict(mutable=True)
                query['subtab'] = 'history'
                if history_q:
                    query['history_q'] = history_q
                if start_date_str:
                    query['start_date'] = start_date_str
                if end_date_str:
                    query['end_date'] = end_date_str
                return redirect(f"{reverse('betting:agent_remapping')}?{query.urlencode()}")
            except ValueError as exc:
                messages.error(request, str(exc))
            except Exception:
                messages.error(request, 'Agent transfer failed. Please try again.')

    history_qs = _crm_agent_transfer_history_queryset(
        q=history_q,
        start_dt=start_dt,
        end_dt=end_dt,
        old_super_agent_id=history_old_super_agent_id,
        new_super_agent_id=history_new_super_agent_id,
    )
    history_paginator = Paginator(history_qs, 50)
    history_page_number = request.GET.get('history_page') or 1
    try:
        history_page = history_paginator.page(history_page_number)
    except Exception:
        history_page = history_paginator.page(1)

    context = {
        'subtab': subtab,
        'q': q,
        'history_q': history_q,
        'start_date': start_date_str,
        'end_date': end_date_str,
        'current_super_agent': current_super_agent_id,
        'destination_super_agent': destination_super_agent_id,
        'history_old_super_agent': history_old_super_agent_id,
        'history_new_super_agent': history_new_super_agent_id,
        'current_super_agents': list(current_super_agents_qs[:300]),
        'destination_super_agents': list(destination_super_agents_qs[:300]),
        'remap_agent_rows': remap_agent_rows,
        'remap_form': remap_form,
        'selected_agent_ids': [str(v) for v in ((request.POST.getlist('agents') if request.method == 'POST' else []))],
        'history_page': history_page,
        'history_total_count': history_qs.count(),
    }
    return render(request, 'betting/agent_remapping.html', context)


@login_required
def agent_remapping_export(request):
    if not crm_can_remap_agents(request.user):
        return HttpResponse("Permission Denied", status=403)

    fmt = (request.GET.get('format') or 'xlsx').strip().lower()
    if fmt not in {'csv', 'xlsx', 'pdf'}:
        return HttpResponseBadRequest('Unknown format')

    history_q = (request.GET.get('history_q') or '').strip()
    history_old_super_agent_id = (request.GET.get('history_old_super_agent') or '').strip()
    history_new_super_agent_id = (request.GET.get('history_new_super_agent') or '').strip()
    start_date_str = (request.GET.get('start_date') or '').strip()
    end_date_str = (request.GET.get('end_date') or '').strip()

    start_dt = None
    end_dt = None
    if start_date_str:
        try:
            start_dt = timezone.make_aware(datetime.combine(datetime.strptime(start_date_str, '%Y-%m-%d').date(), datetime.min.time()))
        except Exception:
            start_dt = None
    if end_date_str:
        try:
            end_dt = timezone.make_aware(datetime.combine(datetime.strptime(end_date_str, '%Y-%m-%d').date(), datetime.max.time()))
        except Exception:
            end_dt = None

    qs = _crm_agent_transfer_history_queryset(
        q=history_q,
        start_dt=start_dt,
        end_dt=end_dt,
        old_super_agent_id=history_old_super_agent_id,
        new_super_agent_id=history_new_super_agent_id,
    )
    rows = []
    for item in qs[:100000]:
        rows.append({
            'date': item.created_at.isoformat(sep=' ', timespec='seconds') if item.created_at else '',
            'agent_username': getattr(item.agent, 'username', '') or '',
            'agent_name': getattr(item.agent, 'get_full_name', lambda: '')() or '',
            'agent_phone': getattr(item.agent, 'phone_number', '') or '',
            'old_super_agent': getattr(item.old_super_agent, 'username', '') or getattr(item.old_super_agent, 'email', '') or '',
            'new_super_agent': getattr(item.new_super_agent, 'username', '') or getattr(item.new_super_agent, 'email', '') or '',
            'transferred_by': getattr(item.transferred_by, 'username', '') or getattr(item.transferred_by, 'email', '') or '',
            'remarks': item.remarks or '',
        })
    return _export_simple_rows(rows=rows, title='agent_transfer_history', fmt=fmt)


@login_required
@require_POST
def submit_account_unlock_appeal(request, locked_user_id):
    if not can_manage_account_unlock_appeals(request.user) and request.user.user_type not in ['super_agent', 'retail_manager']:
        return JsonResponse({'ok': False, 'message': 'Permission Denied'}, status=403)

    locked_user = _scoped_locked_accounts_queryset(request.user).filter(id=locked_user_id).first()
    if not locked_user:
        return JsonResponse({'ok': False, 'message': 'Locked account not found.'}, status=404)

    if AccountUnlockAppeal.objects.filter(locked_user=locked_user, status='pending').exists():
        return JsonResponse({'ok': False, 'message': 'A pending appeal already exists for this account.'}, status=400)

    form = AccountUnlockAppealForm(request.POST)
    if not form.is_valid():
        error_message = next(iter(form.errors.get('appeal_reason', ['Unable to submit appeal.'])), 'Unable to submit appeal.')
        return JsonResponse({'ok': False, 'message': error_message}, status=400)

    appeal = AccountUnlockAppeal.objects.create(
        user=locked_user,
        locked_user=locked_user,
        appealed_by=request.user,
        appeal_reason=form.cleaned_data['appeal_reason'],
        status='pending',
    )
    AccountLockAuditLog.objects.create(
        locked_user=locked_user,
        appealed_by=request.user,
        lock_reason=(locked_user.lock_reason or '').strip(),
        action='appeal_submitted',
        remarks=form.cleaned_data['appeal_reason'],
    )
    _notify_admins_of_unlock_appeal(appeal)
    return JsonResponse({
        'ok': True,
        'message': 'Appeal submitted successfully. Your request has been forwarded to Admin for review.',
    })


@login_required
def account_appeals_review(request):
    if not can_manage_account_unlock_appeals(request.user):
        return HttpResponse("Permission Denied", status=403)

    def _parse_bound(value, *, end=False):
        value = (value or '').strip()
        if not value:
            return None
        try:
            parsed = datetime.strptime(value, '%Y-%m-%d').date()
            return timezone.make_aware(datetime.combine(parsed, datetime.max.time() if end else datetime.min.time()))
        except Exception:
            return None

    query = (request.GET.get('q') or '').strip()
    user_type = (request.GET.get('user_type') or '').strip()
    status = (request.GET.get('status') or '').strip()
    locked_by = (request.GET.get('locked_by') or '').strip()
    locked_start_date = (request.GET.get('locked_start_date') or '').strip()
    locked_end_date = (request.GET.get('locked_end_date') or '').strip()
    appeal_start_date = (request.GET.get('appeal_start_date') or '').strip()
    appeal_end_date = (request.GET.get('appeal_end_date') or '').strip()

    locked_start_dt = _parse_bound(locked_start_date, end=False)
    locked_end_dt = _parse_bound(locked_end_date, end=True)
    appeal_start_dt = _parse_bound(appeal_start_date, end=False)
    appeal_end_dt = _parse_bound(appeal_end_date, end=True)

    if request.method == 'POST' and request.POST.get('review_appeal') == '1':
        return_to = (request.POST.get('return_to') or reverse('betting:account_appeals_review')).strip()
        appeal = get_object_or_404(
            AccountUnlockAppeal.objects.select_related('locked_user', 'appealed_by'),
            id=request.POST.get('appeal_id'),
        )
        review_form = AccountUnlockAppealReviewForm(request.POST)
        if review_form.is_valid():
            if appeal.status != 'pending':
                messages.error(request, 'This appeal has already been reviewed.')
                return redirect(return_to)
            action = review_form.cleaned_data['action']
            admin_comment = review_form.cleaned_data['admin_comment']
            locked_user = appeal.locked_user
            review_time = timezone.now()
            prior_reason = ''
            if locked_user:
                prior_reason = (locked_user.lock_reason or '').strip()
            with db_transaction.atomic():
                if action == 'approve' and locked_user:
                    locked_user.is_locked = False
                    locked_user.failed_login_attempts = 0
                    locked_user.last_failed_login = None
                    locked_user.locked_at = None
                    locked_user.lock_reason = ''
                    locked_user.save(update_fields=['is_locked', 'failed_login_attempts', 'last_failed_login', 'locked_at', 'lock_reason'])
                    LoginAttempt.objects.create(
                        user=locked_user,
                        username_attempted=locked_user.email,
                        ip_address=request.META.get('REMOTE_ADDR'),
                        user_agent=request.META.get('HTTP_USER_AGENT', ''),
                        status='unlocked',
                    )
                    AccountLockAuditLog.objects.create(
                        locked_user=locked_user,
                        appealed_by=appeal.appealed_by,
                        reviewed_by=request.user,
                        lock_reason=prior_reason,
                        action='appeal_approved',
                        remarks=appeal.appeal_reason,
                    )
                    AccountLockAuditLog.objects.create(
                        locked_user=locked_user,
                        reviewed_by=request.user,
                        lock_reason=prior_reason,
                        action='unlocked',
                        remarks='Account unlocked after appeal approval.',
                    )
                    appeal.status = 'approved'
                    messages.success(request, 'Appeal approved and account unlocked successfully.')
                else:
                    AccountLockAuditLog.objects.create(
                        locked_user=locked_user,
                        appealed_by=appeal.appealed_by,
                        reviewed_by=request.user,
                        lock_reason=prior_reason,
                        action='appeal_rejected',
                        remarks=admin_comment or appeal.appeal_reason,
                    )
                    appeal.status = 'rejected'
                    messages.success(request, 'Appeal rejected successfully.')
                appeal.admin_comment = admin_comment
                appeal.reviewed_at = review_time
                appeal.reviewed_by = request.user
                appeal.save(update_fields=['status', 'admin_comment', 'reviewed_at', 'reviewed_by'])
            _notify_unlock_appeal_resolution(appeal, approved=(action == 'approve'))
        else:
            messages.error(request, next(iter(review_form.errors.get('admin_comment', ['Unable to review appeal.'])), 'Unable to review appeal.'))
        return redirect(return_to)

    locked_accounts_qs = _apply_locked_accounts_filters(
        _scoped_locked_accounts_queryset(request.user),
        query=query,
        user_type=user_type,
        status=status,
        locked_by=locked_by,
        locked_start_dt=locked_start_dt,
        locked_end_dt=locked_end_dt,
        appeal_start_dt=appeal_start_dt,
        appeal_end_dt=appeal_end_dt,
    )
    locked_accounts_paginator = Paginator(locked_accounts_qs, 50)
    locked_accounts_page = locked_accounts_paginator.get_page(request.GET.get('locked_page') or 1)
    locked_accounts_rows = _attach_locked_account_metadata(list(locked_accounts_page.object_list))

    appeals_qs = _apply_account_unlock_appeal_filters(
        _scoped_account_unlock_appeals_queryset(request.user),
        query=query,
        user_type=user_type,
        status=status if status in ['pending', 'approved', 'rejected'] else '',
        locked_by=locked_by,
        locked_start_dt=locked_start_dt,
        locked_end_dt=locked_end_dt,
        appeal_start_dt=appeal_start_dt,
        appeal_end_dt=appeal_end_dt,
    )
    appeals_paginator = Paginator(appeals_qs, 50)
    appeals_page = appeals_paginator.get_page(request.GET.get('appeals_page') or 1)

    summary = {
        'total_locked_accounts': _scoped_locked_accounts_queryset(request.user).count(),
        'pending_appeals': _scoped_account_unlock_appeals_queryset(request.user).filter(status='pending').count(),
        'approved_appeals': _scoped_account_unlock_appeals_queryset(request.user).filter(status='approved').count(),
        'rejected_appeals': _scoped_account_unlock_appeals_queryset(request.user).filter(status='rejected').count(),
    }

    context = {
        'summary': summary,
        'locked_accounts_page': locked_accounts_page,
        'locked_accounts_rows': locked_accounts_rows,
        'appeals_page': appeals_page,
        'query': query,
        'user_type': user_type,
        'status': status,
        'locked_by': locked_by,
        'locked_start_date': locked_start_date,
        'locked_end_date': locked_end_date,
        'appeal_start_date': appeal_start_date,
        'appeal_end_date': appeal_end_date,
        'current_full_path': request.get_full_path(),
        'user_type_choices': [
            ('super_agent', 'Super Agent'),
            ('agent', 'Agent'),
            ('cashier', 'Cashier'),
            ('retail_manager', 'Retail Manager'),
        ],
    }
    return render(request, 'betting/account_appeals_review.html', context)


@login_required
def locked_accounts_export(request):
    if not can_manage_account_unlock_appeals(request.user):
        return HttpResponse("Permission Denied", status=403)

    fmt = (request.GET.get('format') or 'xlsx').strip().lower()
    if fmt not in {'csv', 'xlsx', 'pdf'}:
        return HttpResponseBadRequest('Unknown format')

    def _parse_bound(value, *, end=False):
        value = (value or '').strip()
        if not value:
            return None
        try:
            parsed = datetime.strptime(value, '%Y-%m-%d').date()
            return timezone.make_aware(datetime.combine(parsed, datetime.max.time() if end else datetime.min.time()))
        except Exception:
            return None

    query = (request.GET.get('q') or '').strip()
    user_type = (request.GET.get('user_type') or '').strip()
    status = (request.GET.get('status') or '').strip()
    locked_by = (request.GET.get('locked_by') or '').strip()
    locked_start_dt = _parse_bound(request.GET.get('locked_start_date'), end=False)
    locked_end_dt = _parse_bound(request.GET.get('locked_end_date'), end=True)
    appeal_start_dt = _parse_bound(request.GET.get('appeal_start_date'), end=False)
    appeal_end_dt = _parse_bound(request.GET.get('appeal_end_date'), end=True)

    rows = []
    users = list(_apply_locked_accounts_filters(
        _scoped_locked_accounts_queryset(request.user),
        query=query,
        user_type=user_type,
        status=status,
        locked_by=locked_by,
        locked_start_dt=locked_start_dt,
        locked_end_dt=locked_end_dt,
        appeal_start_dt=appeal_start_dt,
        appeal_end_dt=appeal_end_dt,
    )[:100000])
    _attach_locked_account_metadata(users)
    for user in users:
        rows.append({
            'username': user.username or user.email or '',
            'full_name': user.get_full_name() or '',
            'user_type': user.get_user_type_display(),
            'locked_date': timezone.localtime(user.locked_at).strftime('%Y-%m-%d') if user.locked_at else '',
            'locked_time': timezone.localtime(user.locked_at).strftime('%I:%M %p') if user.locked_at else '',
            'locked_by': getattr(user, 'locked_by_display', '') or '',
            'reason': getattr(user, 'lock_reason_display', '') or '',
            'status': getattr(user, 'locked_account_status_label', 'Locked'),
        })
    return _export_simple_rows(rows=rows, title='locked_accounts', fmt=fmt)


@login_required
def account_unlock_appeals_export(request):
    if not can_manage_account_unlock_appeals(request.user):
        return HttpResponse("Permission Denied", status=403)

    fmt = (request.GET.get('format') or 'xlsx').strip().lower()
    if fmt not in {'csv', 'xlsx', 'pdf'}:
        return HttpResponseBadRequest('Unknown format')

    def _parse_bound(value, *, end=False):
        value = (value or '').strip()
        if not value:
            return None
        try:
            parsed = datetime.strptime(value, '%Y-%m-%d').date()
            return timezone.make_aware(datetime.combine(parsed, datetime.max.time() if end else datetime.min.time()))
        except Exception:
            return None

    query = (request.GET.get('q') or '').strip()
    user_type = (request.GET.get('user_type') or '').strip()
    status = (request.GET.get('status') or '').strip()
    locked_by = (request.GET.get('locked_by') or '').strip()
    locked_start_dt = _parse_bound(request.GET.get('locked_start_date'), end=False)
    locked_end_dt = _parse_bound(request.GET.get('locked_end_date'), end=True)
    appeal_start_dt = _parse_bound(request.GET.get('appeal_start_date'), end=False)
    appeal_end_dt = _parse_bound(request.GET.get('appeal_end_date'), end=True)

    rows = []
    appeals = _apply_account_unlock_appeal_filters(
        _scoped_account_unlock_appeals_queryset(request.user),
        query=query,
        user_type=user_type,
        status=status if status in ['pending', 'approved', 'rejected'] else '',
        locked_by=locked_by,
        locked_start_dt=locked_start_dt,
        locked_end_dt=locked_end_dt,
        appeal_start_dt=appeal_start_dt,
        appeal_end_dt=appeal_end_dt,
    )[:100000]
    for appeal in appeals:
        locked_user = appeal.locked_user
        rows.append({
            'date': appeal.created_at.isoformat(sep=' ', timespec='seconds') if appeal.created_at else '',
            'locked_user': getattr(locked_user, 'username', '') or getattr(locked_user, 'email', '') or '',
            'user_type': getattr(locked_user, 'get_user_type_display', lambda: '')() if locked_user else '',
            'appealed_by': getattr(appeal.appealed_by, 'username', '') or getattr(appeal.appealed_by, 'email', '') or '',
            'reason': appeal.appeal_reason or '',
            'status': appeal.get_status_display(),
            'admin_comment': appeal.admin_comment or '',
            'reviewed_by': getattr(appeal.reviewed_by, 'username', '') or getattr(appeal.reviewed_by, 'email', '') or '',
            'reviewed_at': appeal.reviewed_at.isoformat(sep=' ', timespec='seconds') if appeal.reviewed_at else '',
        })
    return _export_simple_rows(rows=rows, title='account_unlock_appeals', fmt=fmt)


@login_required
@user_passes_test_403(is_crm_user)
def crm_dashboard(request):
    tab_raw = (request.POST.get('tab') if request.method == 'POST' else request.GET.get('tab')) or 'overview'
    active_tab = (tab_raw or 'overview').strip() or 'overview'
    q = (request.GET.get('q') or '').strip()
    start_date_str = (request.GET.get('start_date') or '').strip()
    end_date_str = (request.GET.get('end_date') or '').strip()
    audit_query = (request.GET.get('audit_q') or '').strip()
    audit_action_type = (request.GET.get('audit_action_type') or '').strip()
    bet_q = (request.GET.get('bet_q') or '').strip()
    bet_status = (request.GET.get('bet_status') or '').strip()
    bet_agent_id = (request.GET.get('bet_agent') or '').strip()
    commission_agent_q = (request.GET.get('commission_agent') or '').strip()
    hierarchy_agent_q = (request.GET.get('hierarchy_agent') or '').strip()
    selected_top_period_id_raw = (request.GET.get('top_period') or '').strip()
    segment_key = (request.GET.get('segment') or '').strip()
    locked_q = (request.GET.get('locked_q') or '').strip()
    locked_user_type = (request.GET.get('locked_user_type') or '').strip()
    locked_status = (request.GET.get('locked_status') or '').strip()
    locked_by = (request.GET.get('locked_by') or '').strip()
    locked_start_date = (request.GET.get('locked_start_date') or '').strip()
    locked_end_date = (request.GET.get('locked_end_date') or '').strip()
    locked_appeal_start_date = (request.GET.get('locked_appeal_start_date') or '').strip()
    locked_appeal_end_date = (request.GET.get('locked_appeal_end_date') or '').strip()
    comm_msg_title = (request.POST.get('campaign_title') or '').strip()
    comm_msg_body = (request.POST.get('campaign_message') or '').strip()
    dormant_bucket = (request.GET.get('dormant_bucket') or 'login_7').strip() or 'login_7'
    dormant_agent = (request.GET.get('dormant_agent') or '').strip()
    dormant_super_agent = (request.GET.get('dormant_super_agent') or '').strip()
    dormant_retail_manager = (request.GET.get('dormant_retail_manager') or '').strip()
    dormant_status = (request.GET.get('dormant_status') or '').strip()
    complaint_q = (request.GET.get('complaint_q') or '').strip()
    complaint_type = (request.GET.get('complaint_type') or '').strip()
    complaint_status = (request.GET.get('complaint_status') or '').strip()
    complaint_priority = (request.GET.get('complaint_priority') or '').strip()
    deposit_status_filter = (request.GET.get('deposit_status_filter') or '').strip()
    deposit_gateway = (request.GET.get('deposit_gateway') or '').strip()
    deposit_flag = (request.GET.get('deposit_flag') or '').strip()
    performance_entity = (request.GET.get('performance_entity') or 'super_agent').strip() or 'super_agent'
    performance_q = (request.GET.get('performance_q') or '').strip()
    activation_category = (request.GET.get('activation_category') or 'registered_never_deposited').strip() or 'registered_never_deposited'
    activation_q = (request.GET.get('activation_q') or '').strip()
    activation_user_type = (request.GET.get('activation_user_type') or '').strip()
    activation_agent = (request.GET.get('activation_agent') or '').strip()
    activation_super_agent = (request.GET.get('activation_super_agent') or '').strip()
    activation_retail_manager = (request.GET.get('activation_retail_manager') or '').strip()
    activation_status = (request.GET.get('activation_status') or '').strip()
    campaign_channel_filter = (request.GET.get('campaign_channel') or '').strip()
    campaign_target_group_filter = (request.GET.get('campaign_target_group') or '').strip()

    start_dt = None
    end_dt = None
    if start_date_str:
        try:
            sd = datetime.strptime(start_date_str, '%Y-%m-%d').date()
            start_dt = timezone.make_aware(datetime.combine(sd, datetime.min.time()))
        except Exception:
            start_dt = None
    if end_date_str:
        try:
            ed = datetime.strptime(end_date_str, '%Y-%m-%d').date()
            end_dt = timezone.make_aware(datetime.combine(ed, datetime.max.time()))
        except Exception:
            end_dt = None

    def _parse_dashboard_bound(value, *, end=False):
        value = (value or '').strip()
        if not value:
            return None
        try:
            parsed = datetime.strptime(value, '%Y-%m-%d').date()
            return timezone.make_aware(datetime.combine(parsed, datetime.max.time() if end else datetime.min.time()))
        except Exception:
            return None

    locked_start_dt = _parse_dashboard_bound(locked_start_date, end=False)
    locked_end_dt = _parse_dashboard_bound(locked_end_date, end=True)
    locked_appeal_start_dt = _parse_dashboard_bound(locked_appeal_start_date, end=False)
    locked_appeal_end_dt = _parse_dashboard_bound(locked_appeal_end_date, end=True)

    today = timezone.now().date()
    metrics_start_date = None
    metrics_end_date = None
    if start_date_str:
        try:
            metrics_start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date()
        except Exception:
            metrics_start_date = None
    if end_date_str:
        try:
            metrics_end_date = datetime.strptime(end_date_str, '%Y-%m-%d').date()
        except Exception:
            metrics_end_date = None
    if metrics_start_date and metrics_end_date and metrics_start_date > metrics_end_date:
        metrics_start_date, metrics_end_date = metrics_end_date, metrics_start_date
    if metrics_start_date is None and metrics_end_date is None:
        metrics_end_date = today
        metrics_start_date = today - timedelta(days=30)
        metrics_label = 'Last 30 days'
    else:
        metrics_start_date = metrics_start_date or metrics_end_date or today
        metrics_end_date = metrics_end_date or metrics_start_date or today
        metrics_label = 'Custom range'

    metrics_start_dt = timezone.make_aware(datetime.combine(metrics_start_date, datetime.min.time()))
    metrics_end_dt = timezone.make_aware(datetime.combine(metrics_end_date, datetime.max.time()))
    top_fixtures, top_period_options, selected_top_period_id = build_top_fixtures_by_betting_period(selected_top_period_id_raw)
    complaint_form = CustomerComplaintForm(user_queryset=_ops_targetable_users_queryset(request.user))
    complaint_action_form = CustomerComplaintActionForm()
    complaint_note_form = CustomerComplaintNoteForm()
    complaint_action_target_id = None
    complaint_note_target_id = None
    bulk_template_form = BulkMessageTemplateForm()
    bulk_campaign_form = BulkMessageCampaignForm(
        agent_queryset=_ops_targetable_users_queryset(request.user).filter(user_type='agent'),
        template_queryset=BulkMessageTemplate.objects.filter(is_active=True),
        user_queryset=_ops_targetable_users_queryset(request.user),
    )
    threshold_form = CRMThresholdSettingsForm(instance=SiteConfiguration.load())
    crm_assignable_users = list(
        User.objects.filter(Q(user_type='crm') | Q(user_type='admin'))
        .only('id', 'email', 'username')
        .order_by('email')[:100]
    )

    if request.method == 'POST' and active_tab == 'communications':
        if not crm_can_message(request.user):
            messages.error(request, 'Not allowed.')
            return redirect(f"{reverse('betting:crm_dashboard')}?tab=communications")
        if request.POST.get('create_campaign') == '1':
            if not comm_msg_body:
                messages.error(request, 'Message is required.')
                return redirect(f"{reverse('betting:crm_dashboard')}?tab=communications")
            NotificationCampaign = apps.get_model('notifications', 'NotificationCampaign')
            obj = NotificationCampaign.objects.create(
                title=comm_msg_title or 'CRM Broadcast',
                message=comm_msg_body,
                notification_type='SYSTEM_ANNOUNCEMENT',
                send_to_all=True,
                created_by=request.user,
                send_now=True,
            )
            try:
                from notifications.tasks import send_campaign
                send_campaign(obj.id)
            except Exception as e:
                messages.error(request, f'Broadcast could not be sent immediately: {e}')
                return redirect(f"{reverse('betting:crm_dashboard')}?tab=communications")
            CRMActionLog.objects.create(
                actor=request.user,
                action_type='MESSAGE_SENT',
                reason=(comm_msg_title or 'CRM Broadcast'),
                notes=comm_msg_body,
                data={
                    'campaign_id': obj.id,
                    'send_to_all': True,
                    'notification_type': 'SYSTEM_ANNOUNCEMENT',
                },
            )
            messages.success(request, 'Broadcast sent.')
            return redirect(f"{reverse('betting:crm_dashboard')}?tab=communications")

    if request.method == 'POST' and request.POST.get('send_dashboard_message') == '1':
        return _send_ops_message_to_users(
            request,
            allowed_users_qs=_ops_targetable_users_queryset(request.user),
            module=(request.POST.get('module') or active_tab or 'bulk_messaging').strip() or 'bulk_messaging',
            redirect_url=f"{reverse('betting:crm_dashboard')}?tab={active_tab}",
        )

    if request.method == 'POST' and active_tab == 'complaints':
        complaint_redirect = f"{reverse('betting:crm_dashboard')}?tab=complaints"
        if request.POST.get('create_complaint') == '1':
            complaint_form = CustomerComplaintForm(request.POST, user_queryset=_ops_targetable_users_queryset(request.user))
            if complaint_form.is_valid():
                complaint = complaint_form.save(commit=False)
                complaint.created_by = request.user
                if not _ops_targetable_users_queryset(request.user).filter(id=complaint.user_id).exists():
                    messages.error(request, 'Selected user is outside your scope.')
                    return redirect(complaint_redirect)
                complaint.save()
                CustomerComplaintNote.objects.create(
                    complaint=complaint,
                    author=request.user,
                    note='Complaint created from CRM dashboard.',
                    is_internal=True,
                )
                _log_crm_ops_action(request, module='complaints', action='complaint_created', target_user=complaint.user, complaint=complaint)
                messages.success(request, 'Complaint created.')
                return redirect(complaint_redirect)
            messages.error(request, 'Unable to create complaint.')
        elif request.POST.get('update_complaint') == '1':
            complaint = get_object_or_404(_complaint_scope_queryset(request.user), id=request.POST.get('complaint_id'))
            action_form = CustomerComplaintActionForm(request.POST)
            if action_form.is_valid():
                old_status = complaint.status
                complaint.status = action_form.cleaned_data['status']
                complaint.priority = action_form.cleaned_data['priority']
                complaint.assigned_to = action_form.cleaned_data['assigned_to']
                if complaint.status in ['resolved', 'closed'] and not complaint.resolved_at:
                    complaint.resolved_at = timezone.now()
                elif complaint.status not in ['resolved', 'closed']:
                    complaint.resolved_at = None
                complaint.save(update_fields=['status', 'priority', 'assigned_to', 'resolved_at', 'updated_at'])
                admin_note = (action_form.cleaned_data.get('admin_note') or '').strip()
                if admin_note:
                    CustomerComplaintNote.objects.create(
                        complaint=complaint,
                        author=request.user,
                        note=admin_note,
                        is_internal=True,
                    )
                _log_crm_ops_action(
                    request,
                    module='complaints',
                    action='complaint_updated',
                    target_user=complaint.user,
                    complaint=complaint,
                    metadata={'from_status': old_status, 'to_status': complaint.status},
                )
                messages.success(request, 'Complaint updated.')
                return redirect(complaint_redirect)
            complaint_action_form = action_form
            complaint_action_target_id = complaint.id
            messages.error(request, 'Unable to update complaint.')
        elif request.POST.get('add_complaint_note') == '1':
            complaint = get_object_or_404(_complaint_scope_queryset(request.user), id=request.POST.get('complaint_id'))
            complaint_note_form = CustomerComplaintNoteForm(request.POST)
            if complaint_note_form.is_valid():
                note = complaint_note_form.save(commit=False)
                note.complaint = complaint
                note.author = request.user
                note.save()
                _log_crm_ops_action(request, module='complaints', action='complaint_note_added', target_user=complaint.user, complaint=complaint)
                messages.success(request, 'Complaint note added.')
                return redirect(complaint_redirect)
            complaint_note_target_id = complaint.id
            messages.error(request, 'Unable to add complaint note.')

    if request.method == 'POST' and active_tab == 'deposit_monitoring' and request.POST.get('escalate_deposit') == '1':
        deposit_redirect = f"{reverse('betting:crm_dashboard')}?tab=deposit_monitoring"
        tx = get_object_or_404(_deposit_scope_queryset(request.user), id=request.POST.get('transaction_id'))
        escalation_note = (request.POST.get('escalation_note') or '').strip()
        _log_crm_ops_action(
            request,
            module='deposit_monitoring',
            action='deposit_escalated',
            target_user=tx.user,
            transaction_obj=tx,
            metadata={
                'note': escalation_note,
                'status': tx.status,
                'gateway': getattr(tx, 'payment_gateway', ''),
                'amount': str(tx.amount or Decimal('0.00')),
            },
        )
        messages.success(request, 'Deposit issue escalated and logged.')
        return redirect(deposit_redirect)

    if request.method == 'POST' and active_tab == 'bulk_messaging':
        bulk_redirect = f"{reverse('betting:crm_dashboard')}?tab=bulk_messaging"
        if not crm_can_message(request.user):
            messages.error(request, 'Not allowed.')
            return redirect(bulk_redirect)
        if request.POST.get('save_bulk_template') == '1':
            bulk_template_form = BulkMessageTemplateForm(request.POST)
            if bulk_template_form.is_valid():
                template = bulk_template_form.save(commit=False)
                template.created_by = request.user
                template.save()
                _log_crm_ops_action(request, module='bulk_messaging', action='template_created', metadata={'template_id': template.id})
                messages.success(request, 'Template saved.')
                return redirect(bulk_redirect)
            messages.error(request, 'Unable to save template.')
        elif request.POST.get('save_bulk_campaign') == '1':
            bulk_campaign_form = BulkMessageCampaignForm(
                request.POST,
                agent_queryset=_ops_targetable_users_queryset(request.user).filter(user_type='agent'),
                template_queryset=BulkMessageTemplate.objects.filter(is_active=True),
                user_queryset=_ops_targetable_users_queryset(request.user),
            )
            if bulk_campaign_form.is_valid():
                campaign = bulk_campaign_form.save(commit=False)
                if campaign.channel == 'email' and not crm_can_send_bulk_email(request.user):
                    messages.error(request, 'Not allowed to send bulk email campaigns.')
                    _log_crm_ops_action(request, module='bulk_messaging', action='campaign_blocked', metadata={'reason': 'email_not_allowed', 'channel': campaign.channel, 'target_group': getattr(campaign, 'target_group', '')})
                    return redirect(bulk_redirect)
                campaign.created_by = request.user
                campaign.target_agent_ids = [agent.id for agent in bulk_campaign_form.cleaned_data.get('target_agent_ids') or []]
                campaign.target_user_ids = bulk_campaign_form.cleaned_data.get('target_user_ids_list') or []
                campaign.filter_snapshot = {
                    'channel': campaign.channel,
                    'target_group': campaign.target_group,
                }
                send_now_flag = bool(request.POST.get('send_now'))
                if send_now_flag or not campaign.schedule_at or campaign.schedule_at <= timezone.now():
                    campaign.status = 'processing'
                else:
                    campaign.status = 'scheduled'
                campaign.save()
                _log_crm_ops_action(request, module='bulk_messaging', action='campaign_created', campaign=campaign, metadata={'channel': campaign.channel, 'target_group': campaign.target_group})
                if campaign.status == 'processing':
                    delivered = send_bulk_message_campaign_now(campaign.id, acting_user=request.user)
                    messages.success(request, f'Campaign processed for {delivered} recipient(s).')
                else:
                    messages.success(request, 'Campaign scheduled.')
                return redirect(bulk_redirect)
            messages.error(request, 'Unable to save campaign.')
    if request.method == 'POST' and request.POST.get('update_crm_thresholds') == '1':
        threshold_redirect = f"{reverse('betting:crm_dashboard')}?tab=deposit_monitoring"
        threshold_form = CRMThresholdSettingsForm(request.POST, instance=SiteConfiguration.load())
        if threshold_form.is_valid():
            threshold_form.save()
            _log_crm_ops_action(request, module='deposit_monitoring', action='thresholds_updated')
            messages.success(request, 'Deposit monitoring thresholds updated.')
            return redirect(threshold_redirect)
        messages.error(request, 'Unable to update thresholds.')

    if active_tab == 'bulk_messaging' and crm_can_message(request.user):
        try:
            process_due_bulk_message_campaigns(acting_user=request.user, limit=10)
        except Exception:
            pass

    users = User.objects.none()
    if q:
        users = User.objects.filter(
            Q(email__icontains=q) |
            Q(phone_number__icontains=q) |
            Q(username__icontains=q) |
            Q(first_name__icontains=q) |
            Q(last_name__icontains=q) |
            Q(other_name__icontains=q)
        ).exclude(is_superuser=True).order_by('-updated_at')[:30]

    pending_withdrawals_qs = UserWithdrawal.objects.filter(status='pending').select_related('user').order_by('request_time')
    if start_dt:
        pending_withdrawals_qs = pending_withdrawals_qs.filter(request_time__gte=start_dt)
    if end_dt:
        pending_withdrawals_qs = pending_withdrawals_qs.filter(request_time__lte=end_dt)
    pending_withdrawals_tab_count = pending_withdrawals_qs.count()
    pending_withdrawals = pending_withdrawals_qs[:50]

    pending_cashier_qs = CashierRegistrationRequest.objects.filter(status='PENDING').select_related('agent').order_by('created_at')
    if start_dt:
        pending_cashier_qs = pending_cashier_qs.filter(created_at__gte=start_dt)
    if end_dt:
        pending_cashier_qs = pending_cashier_qs.filter(created_at__lte=end_dt)
    pending_cashiers_tab_count = pending_cashier_qs.count()
    pending_cashier_requests = pending_cashier_qs[:50]

    pending_agent_qs = PendingAgentRegistration.objects.filter(status='PENDING').order_by('created_at')
    if start_dt:
        pending_agent_qs = pending_agent_qs.filter(created_at__gte=start_dt)
    if end_dt:
        pending_agent_qs = pending_agent_qs.filter(created_at__lte=end_dt)
    pending_agents_tab_count = pending_agent_qs.count()
    pending_agent_regs = pending_agent_qs[:50]

    platform_users_qs = User.objects.filter(is_superuser=False)

    kpi_cache_key = f"crm:kpis:{metrics_start_date.isoformat()}:{metrics_end_date.isoformat()}"
    chart_cache_key = f"crm:charts:{metrics_start_date.isoformat()}:{metrics_end_date.isoformat()}"
    cached_kpis = cache.get(kpi_cache_key)
    cached_charts = cache.get(chart_cache_key)

    if cached_kpis is None:
        total_registered_users = platform_users_qs.count()
        active_users_today = platform_users_qs.filter(last_login__date=today).count()
        new_registrations = platform_users_qs.filter(date_joined__date__gte=metrics_start_date, date_joined__date__lte=metrics_end_date).count()

        tickets_qs = BetTicket.objects.exclude(status__in=['deleted', 'cancelled']).filter(placed_at__gte=metrics_start_dt, placed_at__lte=metrics_end_dt)
        total_bets_placed = tickets_qs.count()
        total_stake_amount = tickets_qs.aggregate(v=Sum('stake_amount'))['v'] or Decimal('0.00')

        total_payouts = tickets_qs.filter(status='won').aggregate(v=Sum('max_winning'))['v'] or Decimal('0.00')
        ggr = total_stake_amount - total_payouts

        bonus_cost = Transaction.objects.filter(
            transaction_type='bonus',
            status='completed',
            is_successful=True,
            timestamp__gte=metrics_start_dt,
            timestamp__lte=metrics_end_dt,
        ).aggregate(v=Sum('amount'))['v'] or Decimal('0.00')
        ngr = ggr - bonus_cost

        total_deposits = Transaction.objects.filter(
            transaction_type='deposit',
            status='completed',
            is_successful=True,
            timestamp__gte=metrics_start_dt,
            timestamp__lte=metrics_end_dt,
        ).aggregate(v=Sum('amount'))['v'] or Decimal('0.00')

        total_withdrawals = UserWithdrawal.objects.filter(
            status='approved',
            approved_rejected_time__gte=metrics_start_dt,
            approved_rejected_time__lte=metrics_end_dt,
        ).aggregate(v=Sum('amount'))['v'] or Decimal('0.00')

        pending_withdrawals_count = UserWithdrawal.objects.filter(status='pending').count()

        bettors_in_range = tickets_qs.values('user_id').distinct().count()
        conversion_rate = (Decimal(bettors_in_range) / Decimal(total_registered_users) * Decimal('100.00')) if total_registered_users else Decimal('0.00')
        average_bet_value = (total_stake_amount / Decimal(total_bets_placed)) if total_bets_placed else Decimal('0.00')
        platform_profit_loss = ngr

        cached_kpis = {
            'total_registered_users': int(total_registered_users),
            'active_users_today': int(active_users_today),
            'new_registrations': int(new_registrations),
            'total_bets_placed': int(total_bets_placed),
            'total_stake_amount': str(total_stake_amount),
            'total_payouts': str(total_payouts),
            'ggr': str(ggr),
            'ngr': str(ngr),
            'total_deposits': str(total_deposits),
            'total_withdrawals': str(total_withdrawals),
            'pending_withdrawals': int(pending_withdrawals_count),
            'conversion_rate': str(conversion_rate.quantize(Decimal('0.01'))),
            'average_bet_value': str(average_bet_value.quantize(Decimal('0.01'))),
            'platform_profit_loss': str(platform_profit_loss),
        }
        cache.set(kpi_cache_key, cached_kpis, 30)

    if cached_charts is None:
        ticket_series = (
            BetTicket.objects.exclude(status__in=['deleted', 'cancelled'])
            .filter(placed_at__gte=metrics_start_dt, placed_at__lte=metrics_end_dt)
            .annotate(day=TruncDate('placed_at'))
            .values('day')
            .annotate(
                stake=Sum('stake_amount'),
                payouts=Sum(Case(When(status='won', then=F('max_winning')), default=Value(0), output_field=DecimalField())),
                bets=Count('id'),
            )
            .order_by('day')
        )

        registrations_series = (
            platform_users_qs.filter(date_joined__gte=metrics_start_dt, date_joined__lte=metrics_end_dt)
            .annotate(day=TruncDate('date_joined'))
            .values('day')
            .annotate(registrations=Count('id'))
            .order_by('day')
        )

        deposit_series = (
            Transaction.objects.filter(
                transaction_type='deposit',
                status='completed',
                is_successful=True,
                timestamp__gte=metrics_start_dt,
                timestamp__lte=metrics_end_dt,
            )
            .annotate(day=TruncDate('timestamp'))
            .values('day')
            .annotate(deposits=Sum('amount'))
            .order_by('day')
        )

        withdrawal_series = (
            UserWithdrawal.objects.filter(
                status='approved',
                approved_rejected_time__gte=metrics_start_dt,
                approved_rejected_time__lte=metrics_end_dt,
            )
            .annotate(day=TruncDate('approved_rejected_time'))
            .values('day')
            .annotate(withdrawals=Sum('amount'))
            .order_by('day')
        )

        selection_top = (
            Selection.objects.filter(bet_ticket__placed_at__gte=metrics_start_dt, bet_ticket__placed_at__lte=metrics_end_dt)
            .values('fixture_home_team', 'fixture_away_team')
            .annotate(picks=Count('id'))
            .order_by('-picks')[:5]
        )

        cached_charts = {
            'ticket_series': [
                {
                    'day': r['day'].isoformat(),
                    'stake': str(r['stake'] or Decimal('0.00')),
                    'payouts': str(r['payouts'] or Decimal('0.00')),
                    'bets': int(r['bets'] or 0),
                }
                for r in ticket_series
            ],
            'registrations_series': [{'day': r['day'].isoformat(), 'registrations': int(r['registrations'] or 0)} for r in registrations_series],
            'deposit_series': [{'day': r['day'].isoformat(), 'deposits': str(r['deposits'] or Decimal('0.00'))} for r in deposit_series],
            'withdrawal_series': [{'day': r['day'].isoformat(), 'withdrawals': str(r['withdrawals'] or Decimal('0.00'))} for r in withdrawal_series],
            'top_fixtures': [
                {
                    'label': f"{(r.get('fixture_home_team') or '').strip()} vs {(r.get('fixture_away_team') or '').strip()}".strip() or 'Fixture',
                    'picks': int(r['picks'] or 0),
                }
                for r in selection_top
            ],
        }
        cache.set(chart_cache_key, cached_charts, 60)

    cached_charts = dict(cached_charts or {})
    cached_charts['top_fixtures'] = top_fixtures

    locked_accounts_summary = {
        'total_locked_accounts': _scoped_locked_accounts_queryset(request.user).count(),
        'pending_appeals': _scoped_account_unlock_appeals_queryset(request.user).filter(status='pending').count(),
        'approved_appeals': _scoped_account_unlock_appeals_queryset(request.user).filter(status='approved').count(),
        'rejected_appeals': _scoped_account_unlock_appeals_queryset(request.user).filter(status='rejected').count(),
    }
    locked_accounts_page = None
    locked_accounts_rows = []
    if active_tab == 'locked_accounts':
        locked_accounts_qs = _apply_locked_accounts_filters(
            _scoped_locked_accounts_queryset(request.user),
            query=locked_q,
            user_type=locked_user_type,
            status=locked_status,
            locked_by=locked_by,
            locked_start_dt=locked_start_dt,
            locked_end_dt=locked_end_dt,
            appeal_start_dt=locked_appeal_start_dt,
            appeal_end_dt=locked_appeal_end_dt,
        )
        locked_accounts_page = Paginator(locked_accounts_qs, 50).get_page(request.GET.get('locked_page') or 1)
        locked_accounts_rows = _attach_locked_account_metadata(list(locked_accounts_page.object_list))

    bet_tickets_page = None
    agent_filter_options = []
    if active_tab == 'bets':
        bet_tickets_page, agent_filter_options = build_dashboard_bets_page(
            BetTicket.objects.filter(placed_at__gte=metrics_start_dt, placed_at__lte=metrics_end_dt),
            bet_q=bet_q,
            bet_status=bet_status,
            bet_agent_id=bet_agent_id,
            page_number=(request.GET.get('bets_page') or 1),
        )

    segment_stats = None
    segment_users_page = None
    if active_tab == 'segments':
        seg_cache_key = f"crm:segments:{metrics_start_date.isoformat()}:{metrics_end_date.isoformat()}"
        segment_stats = cache.get(seg_cache_key)
        if segment_stats is None:
            last7 = today - timedelta(days=7)
            inactive_cutoff = today - timedelta(days=30)
            vip_count = platform_users_qs.exclude(vip_level='standard').count()
            new_users_count = platform_users_qs.filter(date_joined__date__gte=last7).count()
            inactive_users_count = platform_users_qs.filter(Q(last_login__date__lt=inactive_cutoff) | Q(last_login__isnull=True)).count()
            high_freq_count = (
                BetTicket.objects.exclude(status__in=['deleted', 'cancelled'])
                .filter(placed_at__gte=metrics_start_dt, placed_at__lte=metrics_end_dt)
                .values('user_id')
                .annotate(c=Count('id'))
                .filter(c__gte=20)
                .count()
            )
            bonus_hunters_count = (
                Transaction.objects.filter(transaction_type='bonus', status='completed', is_successful=True, timestamp__gte=metrics_start_dt, timestamp__lte=metrics_end_dt)
                .values('user_id')
                .annotate(c=Count('id'))
                .filter(c__gte=5)
                .count()
            )
            high_value_count = (
                Transaction.objects.filter(transaction_type='deposit', status='completed', is_successful=True, timestamp__gte=metrics_start_dt, timestamp__lte=metrics_end_dt)
                .values('user_id')
                .annotate(s=Sum('amount'))
                .filter(s__gte=Decimal('100000.00'))
                .count()
            )
            segment_stats = {
                'vip': vip_count,
                'new_users': new_users_count,
                'inactive': inactive_users_count,
                'high_frequency': high_freq_count,
                'bonus_hunters': bonus_hunters_count,
                'high_value': high_value_count,
            }
            cache.set(seg_cache_key, segment_stats, 60)

        seg_users_qs = platform_users_qs.order_by('-date_joined')
        if segment_key == 'vip':
            seg_users_qs = seg_users_qs.exclude(vip_level='standard')
        elif segment_key == 'new_users':
            seg_users_qs = seg_users_qs.filter(date_joined__date__gte=today - timedelta(days=7))
        elif segment_key == 'inactive':
            seg_users_qs = seg_users_qs.filter(Q(last_login__date__lt=today - timedelta(days=30)) | Q(last_login__isnull=True))
        elif segment_key == 'high_frequency':
            ids = list(
                BetTicket.objects.exclude(status__in=['deleted', 'cancelled'])
                .filter(placed_at__gte=metrics_start_dt, placed_at__lte=metrics_end_dt)
                .values('user_id')
                .annotate(c=Count('id'))
                .filter(c__gte=20)
                .values_list('user_id', flat=True)[:5000]
            )
            seg_users_qs = seg_users_qs.filter(id__in=ids)
        elif segment_key == 'bonus_hunters':
            ids = list(
                Transaction.objects.filter(transaction_type='bonus', status='completed', is_successful=True, timestamp__gte=metrics_start_dt, timestamp__lte=metrics_end_dt)
                .values('user_id')
                .annotate(c=Count('id'))
                .filter(c__gte=5)
                .values_list('user_id', flat=True)[:5000]
            )
            seg_users_qs = seg_users_qs.filter(id__in=ids)
        elif segment_key == 'high_value':
            ids = list(
                Transaction.objects.filter(transaction_type='deposit', status='completed', is_successful=True, timestamp__gte=metrics_start_dt, timestamp__lte=metrics_end_dt)
                .values('user_id')
                .annotate(s=Sum('amount'))
                .filter(s__gte=Decimal('100000.00'))
                .values_list('user_id', flat=True)[:5000]
            )
            seg_users_qs = seg_users_qs.filter(id__in=ids)
        else:
            segment_key = ''

        seg_paginator = Paginator(seg_users_qs.select_related('vip_manager', 'agent', 'super_agent', 'master_agent'), 50)
        seg_page_num = request.GET.get('segments_page') or 1
        try:
            segment_users_page = seg_paginator.page(seg_page_num)
        except Exception:
            segment_users_page = seg_paginator.page(1)

    recent_campaigns = []
    sms_balance = None
    if active_tab == 'communications':
        NotificationCampaign = apps.get_model('notifications', 'NotificationCampaign')
        recent_campaigns = list(NotificationCampaign.objects.select_related('created_by').order_by('-created_at')[:20])
        try:
            from notifications.services import get_ebulksms_balance
            sms_balance = get_ebulksms_balance()
        except Exception:
            sms_balance = None

    risk_logs_page = None
    risk_kind = (request.GET.get('risk_kind') or '').strip()
    if active_tab == 'risk':
        SuspiciousActivityLog = apps.get_model('risk', 'SuspiciousActivityLog')
        risk_qs = SuspiciousActivityLog.objects.select_related('user', 'ticket').order_by('-created_at')
        if start_dt:
            risk_qs = risk_qs.filter(created_at__gte=start_dt)
        if end_dt:
            risk_qs = risk_qs.filter(created_at__lte=end_dt)
        if risk_kind:
            risk_qs = risk_qs.filter(kind=risk_kind)
        risk_paginator = Paginator(risk_qs, 50)
        risk_page_num = request.GET.get('risk_page') or 1
        try:
            risk_logs_page = risk_paginator.page(risk_page_num)
        except Exception:
            risk_logs_page = risk_paginator.page(1)

    audit_logs = []
    if crm_can_view_audit(request.user):
        audit_qs = CRMActionLog.objects.select_related('actor', 'target_user', 'withdrawal', 'ticket', 'cashier_request', 'pending_agent_registration').order_by('-created_at')
        if start_dt:
            audit_qs = audit_qs.filter(created_at__gte=start_dt)
        if end_dt:
            audit_qs = audit_qs.filter(created_at__lte=end_dt)
        if audit_action_type:
            audit_qs = audit_qs.filter(action_type=audit_action_type)
        if audit_query:
            audit_qs = audit_qs.filter(
                Q(reason__icontains=audit_query) |
                Q(notes__icontains=audit_query) |
                Q(actor__email__icontains=audit_query) |
                Q(target_user__email__icontains=audit_query)
            )
        audit_logs = list(audit_qs[:100])

    retail_hierarchy = []
    hierarchy_search_results = []
    if active_tab == 'retail_hierarchy':
        last_bet_at_subq = Subquery(
            BetTicket.objects.filter(user__agent_id=OuterRef('id'))
            .exclude(status__in=['deleted', 'cancelled'])
            .order_by('-placed_at')
            .values('placed_at')[:1]
        )
        sas_qs = (
            User.objects.filter(user_type='super_agent')
            .exclude(master_agent_id__isnull=True)
            .select_related('state', 'master_agent')
            .order_by('email')
        )
        agents_qs = (
            User.objects.filter(user_type='agent')
            .select_related('state', 'master_agent', 'super_agent')
            .annotate(last_bet_at=last_bet_at_subq)
            .order_by('email')
        )
        if hierarchy_agent_q:
            agents_qs = agents_qs.filter(username__icontains=hierarchy_agent_q)
            hierarchy_search_results = list(agents_qs[:50])
            matched_sa_ids = set(
                agents_qs.exclude(super_agent_id__isnull=True).values_list('super_agent_id', flat=True)
            )
            matched_ma_ids = set(
                agents_qs.exclude(master_agent_id__isnull=True).values_list('master_agent_id', flat=True)
            )
            sas_qs = sas_qs.filter(Q(id__in=list(matched_sa_ids)) | Q(master_agent_id__in=list(matched_ma_ids)))
        ma_ids = set(sas_qs.values_list('master_agent_id', flat=True)) | set(
            agents_qs.exclude(master_agent_id__isnull=True).values_list('master_agent_id', flat=True)
        )
        mas_list = list(
            User.objects.filter(user_type='master_agent', id__in=list(ma_ids)).select_related('state').order_by('email')
        )
        sas_list = list(sas_qs)
        agents_list = list(agents_qs)

        agents_by_sa = {}
        for ag in agents_list:
            agents_by_sa.setdefault(getattr(ag, 'super_agent_id', None), []).append(ag)
        sas_by_ma = {}
        for sa in sas_list:
            sas_by_ma.setdefault(getattr(sa, 'master_agent_id', None), []).append(sa)
        direct_agents_by_ma = {}
        for ag in agents_list:
            if ag.super_agent_id is None and ag.master_agent_id is not None:
                direct_agents_by_ma.setdefault(ag.master_agent_id, []).append(ag)

        for ma in mas_list:
            node = {'master_agent': ma, 'super_agents': [], 'direct_agents': direct_agents_by_ma.get(ma.id, [])}
            for sa in sas_by_ma.get(ma.id, []):
                node['super_agents'].append({'super_agent': sa, 'agents': agents_by_sa.get(sa.id, [])})
            retail_hierarchy.append(node)

    commission_rows = []
    commission_period_options = []
    selected_commission_period_id = ''
    if active_tab == 'commissions':
        selected_commission_period_id = (request.GET.get('commission_period') or '').strip()
        crm_agents_qs = User.objects.filter(user_type='agent', is_superuser=False)
        if commission_agent_q:
            crm_agents_qs = crm_agents_qs.filter(
                Q(username__icontains=commission_agent_q) |
                Q(email__icontains=commission_agent_q)
            )
        commission_rows, commission_period_options, selected_commission_period_id = build_weekly_commission_dashboard_rows(
            crm_agents_qs,
            selected_commission_period_id,
        )

    recent_agent_transfers = []
    if active_tab == 'agent_management':
        recent_agent_transfers = list(
            AgentTransferLog.objects.select_related('agent', 'old_super_agent', 'new_super_agent', 'transferred_by').order_by('-created_at')[:5]
        )

    crm_filter_agents = list(_ops_targetable_users_queryset(request.user).filter(user_type='agent').only('id', 'username', 'email').order_by('username', 'email')[:200])
    crm_filter_super_agents = list(_ops_targetable_users_queryset(request.user).filter(user_type='super_agent').only('id', 'username', 'email').order_by('username', 'email')[:200])
    crm_filter_retail_managers = list(User.objects.filter(user_type='retail_manager').only('id', 'username', 'email').order_by('username', 'email')[:200])

    dormant_cards, dormant_rows = _build_dormant_center_data(
        request.user,
        query=q,
        agent_id=dormant_agent,
        super_agent_id=dormant_super_agent,
        retail_manager_id=dormant_retail_manager,
        status=dormant_status,
        bucket=dormant_bucket,
        start_dt=start_dt,
        end_dt=end_dt,
    )
    dormant_page = Paginator(dormant_rows, 50).get_page(request.GET.get('dormant_page') or 1) if active_tab == 'dormant_accounts' else None
    dormant_agents_tab_count = len(dormant_rows)

    complaint_stats = {
        'open': _complaint_scope_queryset(request.user).filter(status='open').count(),
        'pending': _complaint_scope_queryset(request.user).filter(status='pending').count(),
        'resolved': _complaint_scope_queryset(request.user).filter(status='resolved').count(),
        'escalated': _complaint_scope_queryset(request.user).filter(status='escalated').count(),
    }
    complaints_page = None
    if active_tab == 'complaints':
        complaints_qs = _apply_complaint_filters(
            _complaint_scope_queryset(request.user),
            query=complaint_q,
            complaint_type=complaint_type,
            status=complaint_status,
            priority=complaint_priority,
            start_dt=start_dt,
            end_dt=end_dt,
        )
        complaints_page = Paginator(complaints_qs, 25).get_page(request.GET.get('complaint_page') or 1)

    deposit_cards, deposit_rows = _build_deposit_monitoring_data(
        request.user,
        start_dt=start_dt,
        end_dt=end_dt,
        status=deposit_status_filter,
        gateway=deposit_gateway,
        flag=deposit_flag,
    )
    deposit_page = Paginator(deposit_rows, 50).get_page(request.GET.get('deposit_page') or 1) if active_tab == 'deposit_monitoring' else None

    performance_rows = []
    performance_page = None
    performance_chart = {'labels': [], 'turnover': [], 'ggr': [], 'commission': []}
    performance_top_super_agents = []
    performance_top_agents = []
    performance_top_retail_managers = []
    if active_tab == 'agent_performance':
        performance_rows, performance_chart = _build_agent_performance_rows(
            request.user,
            entity_type=performance_entity,
            start_dt=metrics_start_dt,
            end_dt=metrics_end_dt,
            query=performance_q,
        )
        performance_page = Paginator(performance_rows, 25).get_page(request.GET.get('performance_page') or 1)
        performance_top_super_agents = _build_agent_performance_rows(request.user, entity_type='super_agent', start_dt=metrics_start_dt, end_dt=metrics_end_dt, query='')[0][:10]
        performance_top_agents = _build_agent_performance_rows(request.user, entity_type='agent', start_dt=metrics_start_dt, end_dt=metrics_end_dt, query='')[0][:10]
        performance_top_retail_managers = _build_agent_performance_rows(request.user, entity_type='retail_manager', start_dt=metrics_start_dt, end_dt=metrics_end_dt, query='')[0][:10]

    activation_cards, activation_rows = _build_activation_center_data(
        request.user,
        query=activation_q,
        user_type=activation_user_type,
        agent_id=activation_agent,
        super_agent_id=activation_super_agent,
        retail_manager_id=activation_retail_manager,
        status=activation_status,
        category=activation_category,
    )
    activation_page = Paginator(activation_rows, 50).get_page(request.GET.get('activation_page') or 1) if active_tab == 'user_activation' else None

    bulk_campaigns_page = None
    bulk_templates = []
    bulk_recent_deliveries = []
    if active_tab == 'bulk_messaging':
        bulk_qs = BulkMessageCampaign.objects.select_related('created_by', 'template').order_by('-created_at')
        if campaign_channel_filter:
            bulk_qs = bulk_qs.filter(channel=campaign_channel_filter)
        if campaign_target_group_filter:
            bulk_qs = bulk_qs.filter(target_group=campaign_target_group_filter)
        bulk_campaigns_page = Paginator(bulk_qs, 25).get_page(request.GET.get('campaign_page') or 1)
        bulk_templates = list(BulkMessageTemplate.objects.order_by('category', 'name')[:50])
        bulk_recent_deliveries = list(BulkMessageDelivery.objects.select_related('campaign', 'recipient').order_by('-created_at')[:30])

    ops_audit_logs = list(
        CRMOpsAuditLog.objects.select_related('actor', 'target_user', 'complaint', 'campaign', 'transaction').order_by('-created_at')[:100]
    )
    overdraft_reporting_context = {}
    if active_tab == 'overdraft_center':
        overdraft_reporting_context = _build_overdraft_reporting_dashboard_context(
            request,
            include_retail_manager=True,
            extra_params={'tab': 'overdraft_center'},
        )

    context = {
        'active_tab': active_tab,
        'q': q,
        'start_date': start_date_str,
        'end_date': end_date_str,
        'metrics_label': metrics_label,
        'metrics_start': metrics_start_date.isoformat(),
        'metrics_end': metrics_end_date.isoformat(),
        'kpis': cached_kpis,
        'charts_data': cached_charts,
        'bet_q': bet_q,
        'bet_status': bet_status,
        'bet_agent': bet_agent_id,
        'bet_tickets_page': bet_tickets_page,
        'agent_filter_options': agent_filter_options,
        'segment_key': segment_key,
        'segment_stats': segment_stats,
        'segment_users_page': segment_users_page,
        'recent_campaigns': recent_campaigns,
        'sms_balance': sms_balance,
        'risk_kind': risk_kind,
        'risk_logs_page': risk_logs_page,
        'users': users,
        'pending_withdrawals': pending_withdrawals,
        'pending_cashier_requests': pending_cashier_requests,
        'pending_agent_regs': pending_agent_regs,
        'dormant_agents_tab_count': dormant_agents_tab_count,
        'pending_withdrawals_tab_count': pending_withdrawals_tab_count,
        'pending_cashiers_tab_count': pending_cashiers_tab_count,
        'pending_agents_tab_count': pending_agents_tab_count,
        'audit_logs': audit_logs,
        'audit_q': audit_query,
        'audit_action_type': audit_action_type,
        'audit_action_choices': getattr(CRMActionLog, 'ACTION_TYPES', ()),
        'can_approve_withdrawals': crm_can_approve_withdrawals(request.user),
        'can_suspend_users': crm_can_suspend_users(request.user),
        'can_approve_registrations': crm_can_approve_registrations(request.user),
        'can_edit_profiles': crm_can_edit_profiles(request.user),
        'can_view_audit': crm_can_view_audit(request.user),
        'can_remap_agents': crm_can_remap_agents(request.user),
        'can_message': crm_can_message(request.user),
        'retail_hierarchy': retail_hierarchy,
        'hierarchy_search_results': hierarchy_search_results,
        'hierarchy_agent_q': hierarchy_agent_q,
        'commission_rows': commission_rows,
        'commission_period_options': commission_period_options,
        'selected_commission_period_id': selected_commission_period_id,
        'commission_agent_q': commission_agent_q,
        'top_period_options': top_period_options,
        'selected_top_period_id': selected_top_period_id,
        'recent_agent_transfers': recent_agent_transfers,
        'locked_accounts_summary': locked_accounts_summary,
        'locked_accounts_page': locked_accounts_page,
        'locked_accounts_rows': locked_accounts_rows,
        'locked_q': locked_q,
        'locked_user_type': locked_user_type,
        'locked_status': locked_status,
        'locked_by': locked_by,
        'locked_start_date': locked_start_date,
        'locked_end_date': locked_end_date,
        'locked_appeal_start_date': locked_appeal_start_date,
        'locked_appeal_end_date': locked_appeal_end_date,
        'crm_filter_agents': crm_filter_agents,
        'crm_filter_super_agents': crm_filter_super_agents,
        'crm_filter_retail_managers': crm_filter_retail_managers,
        'dormant_bucket': dormant_bucket,
        'dormant_agent': dormant_agent,
        'dormant_super_agent': dormant_super_agent,
        'dormant_retail_manager': dormant_retail_manager,
        'dormant_status': dormant_status,
        'dormant_cards': dormant_cards,
        'dormant_page': dormant_page,
        'complaint_q': complaint_q,
        'complaint_type': complaint_type,
        'complaint_status': complaint_status,
        'complaint_priority': complaint_priority,
        'complaint_stats': complaint_stats,
        'complaints_page': complaints_page,
        'complaint_form': complaint_form,
        'complaint_action_form': complaint_action_form,
        'complaint_action_target_id': complaint_action_target_id,
        'complaint_note_form': complaint_note_form,
        'complaint_note_target_id': complaint_note_target_id,
        'deposit_status_filter': deposit_status_filter,
        'deposit_gateway': deposit_gateway,
        'deposit_flag': deposit_flag,
        'deposit_cards': deposit_cards,
        'deposit_page': deposit_page,
        'performance_entity': performance_entity,
        'performance_q': performance_q,
        'performance_page': performance_page,
        'performance_chart': performance_chart,
        'performance_top_super_agents': performance_top_super_agents,
        'performance_top_agents': performance_top_agents,
        'performance_top_retail_managers': performance_top_retail_managers,
        'activation_category': activation_category,
        'activation_q': activation_q,
        'activation_user_type': activation_user_type,
        'activation_agent': activation_agent,
        'activation_super_agent': activation_super_agent,
        'activation_retail_manager': activation_retail_manager,
        'activation_status': activation_status,
        'activation_cards': activation_cards,
        'activation_page': activation_page,
        'bulk_campaigns_page': bulk_campaigns_page,
        'bulk_templates': bulk_templates,
        'bulk_recent_deliveries': bulk_recent_deliveries,
        'bulk_template_form': bulk_template_form,
        'bulk_campaign_form': bulk_campaign_form,
        'threshold_form': threshold_form,
        'campaign_channel_filter': campaign_channel_filter,
        'campaign_target_group_filter': campaign_target_group_filter,
        'ops_audit_logs': ops_audit_logs,
        'complaint_type_choices': CustomerComplaint.COMPLAINT_TYPE_CHOICES,
        'complaint_status_choices': CustomerComplaint.STATUS_CHOICES,
        'complaint_priority_choices': CustomerComplaint.PRIORITY_CHOICES,
        'crm_assignable_users': crm_assignable_users,
        'bulk_channel_choices': BulkMessageCampaign.CHANNEL_CHOICES,
        'bulk_target_group_choices': BulkMessageCampaign.TARGET_GROUP_CHOICES,
        'bulk_recurring_choices': BulkMessageCampaign.RECURRING_CHOICES,
        'ticket_transactions_widget': _ticket_transaction_widget_context(
            request.user,
            limit=12,
            date_from=start_date_str,
            date_to=end_date_str,
        ),
        **overdraft_reporting_context,
    }
    return render(request, 'betting/crm_dashboard.html', context)

@login_required
@user_passes_test_403(is_crm_user)
def crm_activity_feed(request):
    limit = 20

    tickets = (
        BetTicket.objects.exclude(status__in=['deleted', 'cancelled'])
        .select_related('user')
        .order_by('-placed_at')[:10]
    )
    txs = (
        Transaction.objects.filter(status='completed', is_successful=True)
        .select_related('user')
        .order_by('-timestamp')[:10]
    )
    withdrawals = (
        UserWithdrawal.objects.select_related('user')
        .order_by('-request_time')[:10]
    )

    events = []
    for t in tickets:
        events.append({
            'ts': t.placed_at.isoformat() if getattr(t, 'placed_at', None) else '',
            'type': 'bet',
            'user': getattr(getattr(t, 'user', None), 'email', '') or getattr(getattr(t, 'user', None), 'username', '') or '-',
            'label': f"Bet placed ({t.ticket_id or ''})".strip(),
            'amount': str(getattr(t, 'stake_amount', Decimal('0.00'))),
            'status': t.status,
        })

    for tx in txs:
        events.append({
            'ts': tx.timestamp.isoformat() if getattr(tx, 'timestamp', None) else '',
            'type': 'transaction',
            'user': getattr(getattr(tx, 'user', None), 'email', '') or getattr(getattr(tx, 'user', None), 'username', '') or '-',
            'label': tx.transaction_type,
            'amount': str(getattr(tx, 'amount', Decimal('0.00'))),
            'status': tx.status,
        })

    for w in withdrawals:
        events.append({
            'ts': w.request_time.isoformat() if getattr(w, 'request_time', None) else '',
            'type': 'withdrawal',
            'user': getattr(getattr(w, 'user', None), 'email', '') or getattr(getattr(w, 'user', None), 'username', '') or '-',
            'label': 'Withdrawal request',
            'amount': str(getattr(w, 'amount', Decimal('0.00'))),
            'status': w.status,
        })

    events.sort(key=lambda e: e.get('ts') or '', reverse=True)
    return JsonResponse({'events': events[:limit]})

@login_required
@login_required
@user_passes_test_403(is_crm_user)
def crm_export(request):
    dataset = (request.GET.get('dataset') or '').strip().lower()
    fmt = (request.GET.get('format') or 'csv').strip().lower()
    q = (request.GET.get('q') or '').strip()
    start_date_str = (request.GET.get('start_date') or '').strip()
    end_date_str = (request.GET.get('end_date') or '').strip()
    dormant_bucket = (request.GET.get('dormant_bucket') or 'login_7').strip() or 'login_7'
    dormant_agent = (request.GET.get('dormant_agent') or '').strip()
    dormant_super_agent = (request.GET.get('dormant_super_agent') or '').strip()
    dormant_retail_manager = (request.GET.get('dormant_retail_manager') or '').strip()
    dormant_status = (request.GET.get('dormant_status') or '').strip()
    complaint_q = (request.GET.get('complaint_q') or '').strip()
    complaint_type = (request.GET.get('complaint_type') or '').strip()
    complaint_status = (request.GET.get('complaint_status') or '').strip()
    complaint_priority = (request.GET.get('complaint_priority') or '').strip()
    deposit_status_filter = (request.GET.get('deposit_status_filter') or '').strip()
    deposit_gateway = (request.GET.get('deposit_gateway') or '').strip()
    deposit_flag = (request.GET.get('deposit_flag') or '').strip()
    performance_entity = (request.GET.get('performance_entity') or 'super_agent').strip() or 'super_agent'
    performance_q = (request.GET.get('performance_q') or '').strip()
    activation_category = (request.GET.get('activation_category') or 'registered_never_deposited').strip() or 'registered_never_deposited'
    activation_q = (request.GET.get('activation_q') or '').strip()
    activation_user_type = (request.GET.get('activation_user_type') or '').strip()
    activation_agent = (request.GET.get('activation_agent') or '').strip()
    activation_super_agent = (request.GET.get('activation_super_agent') or '').strip()
    activation_retail_manager = (request.GET.get('activation_retail_manager') or '').strip()
    activation_status = (request.GET.get('activation_status') or '').strip()
    campaign_channel_filter = (request.GET.get('campaign_channel') or '').strip()
    campaign_target_group_filter = (request.GET.get('campaign_target_group') or '').strip()

    start_dt = None
    end_dt = None
    try:
        if start_date_str:
            start_dt = timezone.make_aware(datetime.strptime(start_date_str, '%Y-%m-%d'))
    except Exception:
        start_dt = None
    try:
        if end_date_str:
            end_raw = datetime.strptime(end_date_str, '%Y-%m-%d')
            end_dt = timezone.make_aware(datetime.combine(end_raw.date(), datetime.max.time()))
    except Exception:
        end_dt = None

    rows = []
    title = dataset or 'crm_export'

    if dataset == 'overdrafts':
        report_context = _build_overdraft_reporting_dashboard_context(
            request,
            include_retail_manager=True,
            extra_params={'tab': 'overdraft_center'},
        )
        rows = _loan_reporting_export_rows(
            report_context['overdraft_reporting']['rows'],
            include_retail_manager=True,
        )
        title = 'crm_overdraft_center'

    elif dataset == 'dormant_accounts':
        _, dormant_rows = _build_dormant_center_data(
            request.user,
            query=q,
            agent_id=dormant_agent,
            super_agent_id=dormant_super_agent,
            retail_manager_id=dormant_retail_manager,
            status=dormant_status,
            bucket=dormant_bucket,
            start_dt=start_dt,
            end_dt=end_dt,
        )
        for user_obj in dormant_rows:
            rows.append({
                'username': user_obj.username or user_obj.email or '',
                'full_name': user_obj.get_full_name() or '',
                'last_login': user_obj.last_login.isoformat(sep=' ', timespec='seconds') if user_obj.last_login else '',
                'last_bet': user_obj.last_bet_at.isoformat(sep=' ', timespec='seconds') if getattr(user_obj, 'last_bet_at', None) else '',
                'last_transaction': user_obj.last_transaction_at.isoformat(sep=' ', timespec='seconds') if getattr(user_obj, 'last_transaction_at', None) else '',
                'last_wallet_activity': user_obj.last_wallet_activity_at.isoformat(sep=' ', timespec='seconds') if getattr(user_obj, 'last_wallet_activity_at', None) else '',
                'last_activity': user_obj.last_activity_at.isoformat(sep=' ', timespec='seconds') if getattr(user_obj, 'last_activity_at', None) else '',
                'super_agent': getattr(getattr(user_obj, 'super_agent', None), 'username', '') or getattr(getattr(user_obj, 'super_agent', None), 'email', '') or '',
                'cashiers': getattr(user_obj, 'cashiers_count', 0) or 0,
                'dormant_days': getattr(user_obj, 'dormant_days', '') if getattr(user_obj, 'dormant_days', None) is not None else '',
                'wallet_balance': str(getattr(user_obj, 'wallet_balance_annotated', Decimal('0.00')) or Decimal('0.00')),
                'status': 'Locked' if user_obj.is_locked else ('Active' if user_obj.is_active else 'Inactive'),
            })
        title = 'crm_dormant_agents'

    elif dataset == 'complaints':
        complaints_qs = _apply_complaint_filters(
            _complaint_scope_queryset(request.user),
            query=complaint_q,
            complaint_type=complaint_type,
            status=complaint_status,
            priority=complaint_priority,
            start_dt=start_dt,
            end_dt=end_dt,
        )
        for complaint in complaints_qs[:5000]:
            rows.append({
                'created_at': complaint.created_at.isoformat(sep=' ', timespec='seconds') if complaint.created_at else '',
                'complaint_type': complaint.get_complaint_type_display(),
                'user': complaint.user.username or complaint.user.email or '',
                'subject': complaint.subject,
                'status': complaint.get_status_display(),
                'priority': complaint.get_priority_display(),
                'assigned_to': getattr(complaint.assigned_to, 'email', '') or getattr(complaint.assigned_to, 'username', '') or '',
                'resolved_at': complaint.resolved_at.isoformat(sep=' ', timespec='seconds') if complaint.resolved_at else '',
            })
        title = 'crm_complaints'

    elif dataset == 'deposit_monitoring':
        _, deposit_rows = _build_deposit_monitoring_data(
            request.user,
            start_dt=start_dt,
            end_dt=end_dt,
            status=deposit_status_filter,
            gateway=deposit_gateway,
            flag=deposit_flag,
        )
        for row in deposit_rows:
            tx = row['transaction']
            rows.append({
                'date': tx.timestamp.isoformat(sep=' ', timespec='seconds') if tx.timestamp else '',
                'user': tx.user.username or tx.user.email or '',
                'amount': str(tx.amount or Decimal('0.00')),
                'gateway': getattr(tx, 'get_payment_gateway_display', lambda: getattr(tx, 'payment_gateway', ''))(),
                'status': tx.status,
                'reference': tx.external_reference or tx.paystack_reference or str(tx.id),
                'attempts': row['attempts'],
                'large_deposit': 'yes' if row['is_large'] else 'no',
                'repeat_failed': 'yes' if row['is_repeat_failed'] else 'no',
            })
        title = 'crm_deposit_monitoring'

    elif dataset == 'agent_performance':
        performance_rows, _ = _build_agent_performance_rows(
            request.user,
            entity_type=performance_entity,
            start_dt=start_dt,
            end_dt=end_dt,
            query=performance_q,
        )
        for row in performance_rows:
            entity = row['entity']
            rows.append({
                'entity': entity.username or entity.email or '',
                'entity_type': entity.get_user_type_display(),
                'turnover': str(row['turnover']),
                'ggr': str(row['ggr']),
                'net_ggr': str(row['net_ggr']),
                'commission': str(row['commission']),
                'active_users': row['active_users'],
                'dormant_users': row['dormant_users'],
                'deposit_volume': str(row['deposit_volume']),
                'withdrawal_volume': str(row['withdrawal_volume']),
                'cashiers': row['cashiers_count'],
                'agents': row['agents_count'],
                'tickets_sold': row['tickets_sold'],
                'average_stake': str(row['average_stake']),
                'winning_percentage': str(row['winning_percentage']),
            })
        title = 'crm_agent_performance'

    elif dataset == 'user_activation':
        _, activation_rows = _build_activation_center_data(
            request.user,
            query=activation_q,
            user_type=activation_user_type,
            agent_id=activation_agent,
            super_agent_id=activation_super_agent,
            retail_manager_id=activation_retail_manager,
            status=activation_status,
            category=activation_category,
        )
        for user_obj in activation_rows:
            rows.append({
                'user': user_obj.username or user_obj.email or '',
                'registration_date': user_obj.date_joined.isoformat(sep=' ', timespec='seconds') if user_obj.date_joined else '',
                'last_login': user_obj.last_login.isoformat(sep=' ', timespec='seconds') if user_obj.last_login else '',
                'deposit_amount': str(getattr(user_obj, 'deposits_amount', Decimal('0.00')) or Decimal('0.00')),
                'bets_placed': int(getattr(user_obj, 'bets_count', 0) or 0),
                'status': 'Locked' if user_obj.is_locked else ('Active' if user_obj.is_active else 'Inactive'),
            })
        title = 'crm_user_activation'

    elif dataset == 'bulk_messaging':
        campaigns_qs = BulkMessageCampaign.objects.select_related('created_by', 'template').order_by('-created_at')
        if campaign_channel_filter:
            campaigns_qs = campaigns_qs.filter(channel=campaign_channel_filter)
        if campaign_target_group_filter:
            campaigns_qs = campaigns_qs.filter(target_group=campaign_target_group_filter)
        for campaign in campaigns_qs[:5000]:
            rows.append({
                'created_at': campaign.created_at.isoformat(sep=' ', timespec='seconds') if campaign.created_at else '',
                'subject': campaign.subject,
                'channel': campaign.get_channel_display(),
                'target_group': campaign.get_target_group_display(),
                'status': campaign.get_status_display(),
                'recipients': campaign.recipients_count,
                'delivered': campaign.delivered_count,
                'failed': campaign.failed_count,
                'opened': campaign.opened_count,
                'clicked': campaign.clicked_count,
                'scheduled_for': campaign.schedule_at.isoformat(sep=' ', timespec='seconds') if campaign.schedule_at else '',
                'sent_at': campaign.sent_at.isoformat(sep=' ', timespec='seconds') if campaign.sent_at else '',
            })
        title = 'crm_bulk_messaging'

    else:
        return HttpResponse("Unknown dataset.", status=400)

    return _export_simple_rows(rows=rows, title=title, fmt=fmt)


@user_passes_test_403(is_retail_manager)
def retail_activity_feed(request):
    limit = 30
    limit = 30
    network_users = get_retail_network_users_qs(request.user)

    tickets = (
        BetTicket.objects.exclude(status__in=['deleted', 'cancelled'])
        .filter(user__in=network_users)
        .select_related('user')
        .order_by('-placed_at')[:15]
    )
    txs = (
        Transaction.objects.filter(status='completed', is_successful=True)
        .filter(user__in=network_users)
        .select_related('user')
        .order_by('-timestamp')[:15]
    )
    withdrawals = (
        UserWithdrawal.objects.select_related('user')
        .filter(user__in=network_users)
        .order_by('-request_time')[:15]
    )

    events = []
    for t in tickets:
        events.append({
            'ts': t.placed_at.isoformat() if getattr(t, 'placed_at', None) else '',
            'type': 'bet',
            'user': getattr(getattr(t, 'user', None), 'email', '') or getattr(getattr(t, 'user', None), 'username', '') or '-',
            'label': f"Bet placed ({t.ticket_id or ''})".strip(),
            'amount': str(getattr(t, 'stake_amount', Decimal('0.00'))),
            'status': t.status,
        })

    for tx in txs:
        events.append({
            'ts': tx.timestamp.isoformat() if getattr(tx, 'timestamp', None) else '',
            'type': 'transaction',
            'user': getattr(getattr(tx, 'user', None), 'email', '') or getattr(getattr(tx, 'user', None), 'username', '') or '-',
            'label': tx.transaction_type,
            'amount': str(getattr(tx, 'amount', Decimal('0.00'))),
            'status': tx.status,
        })

    for w in withdrawals:
        events.append({
            'ts': w.request_time.isoformat() if getattr(w, 'request_time', None) else '',
            'type': 'withdrawal',
            'user': getattr(getattr(w, 'user', None), 'email', '') or getattr(getattr(w, 'user', None), 'username', '') or '-',
            'label': 'Withdrawal request',
            'amount': str(getattr(w, 'amount', Decimal('0.00'))),
            'status': w.status,
        })

    events.sort(key=lambda e: e.get('ts') or '', reverse=True)
    return JsonResponse({'events': events[:limit]})


@login_required
@user_passes_test_403(is_retail_manager)
def retail_dashboard(request):
    tab_raw = (request.POST.get('tab') if request.method == 'POST' else request.GET.get('tab')) or 'overview'
    active_tab = (tab_raw or 'overview').strip() or 'overview'
    start_date_str = (request.GET.get('start_date') or '').strip()
    end_date_str = (request.GET.get('end_date') or '').strip()
    bet_q = (request.GET.get('bet_q') or '').strip()
    bet_status = (request.GET.get('bet_status') or '').strip()
    bet_agent_id = (request.GET.get('bet_agent') or '').strip()
    tx_type = (request.GET.get('tx_type') or '').strip()
    q = (request.GET.get('q') or '').strip()
    shop_q = (request.GET.get('shop_q') or '').strip()
    shop_state = (request.GET.get('shop_state') or '').strip()
    shop_active = (request.GET.get('shop_active') or '').strip()
    player_q = (request.GET.get('player_q') or '').strip()
    player_status = (request.GET.get('player_status') or '').strip()
    player_kyc = (request.GET.get('player_kyc') or '').strip()
    commission_agent_q = (request.GET.get('commission_agent') or '').strip()
    locked_q = (request.GET.get('locked_q') or '').strip()
    locked_user_type = (request.GET.get('locked_user_type') or '').strip()
    locked_status = (request.GET.get('locked_status') or '').strip()
    locked_by = (request.GET.get('locked_by') or '').strip()
    locked_start_date = (request.GET.get('locked_start_date') or '').strip()
    locked_end_date = (request.GET.get('locked_end_date') or '').strip()
    locked_appeal_start_date = (request.GET.get('locked_appeal_start_date') or '').strip()
    locked_appeal_end_date = (request.GET.get('locked_appeal_end_date') or '').strip()
    dormant_q = (request.GET.get('dormant_q') or '').strip()
    dormant_bucket = (request.GET.get('dormant_bucket') or 'login_7').strip() or 'login_7'
    dormant_agent = (request.GET.get('dormant_agent') or '').strip()
    dormant_super_agent = (request.GET.get('dormant_super_agent') or '').strip()
    dormant_status = (request.GET.get('dormant_status') or '').strip()
    complaint_q = (request.GET.get('complaint_q') or '').strip()
    complaint_type = (request.GET.get('complaint_type') or '').strip()
    complaint_status = (request.GET.get('complaint_status') or '').strip()
    complaint_priority = (request.GET.get('complaint_priority') or '').strip()
    performance_entity = (request.GET.get('performance_entity') or 'super_agent').strip() or 'super_agent'
    performance_q = (request.GET.get('performance_q') or '').strip()

    start_dt = None
    end_dt = None
    try:
        if start_date_str:
            start_dt = timezone.make_aware(datetime.strptime(start_date_str, "%Y-%m-%d"))
    except Exception:
        start_dt = None
    try:
        if end_date_str:
            end_raw = datetime.strptime(end_date_str, "%Y-%m-%d")
            end_dt = timezone.make_aware(datetime.combine(end_raw.date(), datetime.max.time()))
    except Exception:
        end_dt = None

    def _parse_dashboard_bound(value, *, end=False):
        value = (value or '').strip()
        if not value:
            return None
        try:
            parsed = datetime.strptime(value, "%Y-%m-%d").date()
            return timezone.make_aware(datetime.combine(parsed, datetime.max.time() if end else datetime.min.time()))
        except Exception:
            return None

    locked_start_dt = _parse_dashboard_bound(locked_start_date, end=False)
    locked_end_dt = _parse_dashboard_bound(locked_end_date, end=True)
    locked_appeal_start_dt = _parse_dashboard_bound(locked_appeal_start_date, end=False)
    locked_appeal_end_dt = _parse_dashboard_bound(locked_appeal_end_date, end=True)

    today = timezone.localdate()
    metrics_start_date = (start_dt.date() if start_dt else (today - timedelta(days=30)))
    metrics_end_date = (end_dt.date() if end_dt else today)
    metrics_start_dt = timezone.make_aware(datetime.combine(metrics_start_date, datetime.min.time()))
    metrics_end_dt = timezone.make_aware(datetime.combine(metrics_end_date, datetime.max.time()))
    metrics_label = f"{metrics_start_date.isoformat()} → {metrics_end_date.isoformat()}"

    note_entry, _ = RetailManagerDashboardNote.objects.get_or_create(retail_manager=request.user)
    note_form = RetailManagerDashboardNoteForm(instance=note_entry)
    complaint_form = CustomerComplaintForm(user_queryset=_ops_targetable_users_queryset(request.user))
    complaint_action_form = CustomerComplaintActionForm()
    complaint_note_form = CustomerComplaintNoteForm()
    complaint_action_target_id = None
    complaint_note_target_id = None

    if request.method == 'POST' and active_tab == 'note' and request.POST.get('save_note') == '1':
        note_form = RetailManagerDashboardNoteForm(request.POST, instance=note_entry)
        if note_form.is_valid():
            saved_note = note_form.save(commit=False)
            saved_note.retail_manager = request.user
            saved_note.save()
            messages.success(request, 'Note saved successfully.')
            qd = QueryDict(mutable=True)
            qd['tab'] = 'note'
            if start_date_str:
                qd['start_date'] = start_date_str
            if end_date_str:
                qd['end_date'] = end_date_str
            return redirect(f"{reverse('betting:retail_dashboard')}?{qd.urlencode()}")
        messages.error(request, 'Unable to save note. Please review the editor content and try again.')

    if request.method == 'POST' and active_tab == 'complaints':
        retail_complaint_redirect = f"{reverse('betting:retail_dashboard')}?tab=complaints"
        if request.POST.get('create_complaint') == '1':
            complaint_form = CustomerComplaintForm(request.POST, user_queryset=_ops_targetable_users_queryset(request.user))
            if complaint_form.is_valid():
                complaint = complaint_form.save(commit=False)
                complaint.created_by = request.user
                if not _ops_targetable_users_queryset(request.user).filter(id=complaint.user_id).exists():
                    messages.error(request, 'Selected user is outside your network.')
                    return redirect(retail_complaint_redirect)
                complaint.save()
                CustomerComplaintNote.objects.create(
                    complaint=complaint,
                    author=request.user,
                    note='Complaint created from Retail Manager dashboard.',
                    is_internal=True,
                )
                _log_crm_ops_action(request, module='complaints', action='retail_complaint_created', target_user=complaint.user, complaint=complaint)
                messages.success(request, 'Complaint created.')
                return redirect(retail_complaint_redirect)
            messages.error(request, 'Unable to create complaint.')
        elif request.POST.get('update_complaint') == '1':
            complaint = get_object_or_404(_complaint_scope_queryset(request.user), id=request.POST.get('complaint_id'))
            action_form = CustomerComplaintActionForm(request.POST)
            if action_form.is_valid():
                complaint.status = action_form.cleaned_data['status']
                complaint.priority = action_form.cleaned_data['priority']
                admin_note = (action_form.cleaned_data.get('admin_note') or '').strip()
                if complaint.status in ['resolved', 'closed'] and not complaint.resolved_at:
                    complaint.resolved_at = timezone.now()
                complaint.save(update_fields=['status', 'priority', 'resolved_at', 'updated_at'])
                if admin_note:
                    CustomerComplaintNote.objects.create(
                        complaint=complaint,
                        author=request.user,
                        note=admin_note,
                        is_internal=True,
                    )
                _log_crm_ops_action(request, module='complaints', action='retail_complaint_updated', target_user=complaint.user, complaint=complaint)
                messages.success(request, 'Complaint updated.')
                return redirect(retail_complaint_redirect)
            complaint_action_form = action_form
            complaint_action_target_id = complaint.id
            messages.error(request, 'Unable to update complaint.')
        elif request.POST.get('add_complaint_note') == '1':
            complaint = get_object_or_404(_complaint_scope_queryset(request.user), id=request.POST.get('complaint_id'))
            complaint_note_form = CustomerComplaintNoteForm(request.POST)
            if complaint_note_form.is_valid():
                note = complaint_note_form.save(commit=False)
                note.complaint = complaint
                note.author = request.user
                note.save()
                _log_crm_ops_action(request, module='complaints', action='retail_complaint_note_added', target_user=complaint.user, complaint=complaint)
                messages.success(request, 'Complaint note added.')
                return redirect(retail_complaint_redirect)
            complaint_note_target_id = complaint.id
            messages.error(request, 'Unable to add complaint note.')

    master_agents = get_retail_manager_master_agents(request.user)
    super_agents = get_retail_manager_super_agents(request.user, master_agents_qs=master_agents)
    agents = get_retail_manager_agents(request.user, master_agents_qs=master_agents, super_agents_qs=super_agents)
    network_users = get_retail_network_users_qs(request.user)

    total_mapped_master_agents = master_agents.count()
    total_mapped_super_agents = super_agents.count()
    total_mapped_agents = agents.count()
    total_active_players = network_users.filter(user_type='player', is_active=True).count()
    online_users = (
        User.objects.filter(id__in=network_users.values_list('id', flat=True))
        .filter(downline_activity_last_seen_at__gte=timezone.now() - timedelta(minutes=5))
        .count()
    )

    tickets_today_qs = (
        BetTicket.objects.exclude(status__in=['deleted', 'cancelled'])
        .filter(user__in=network_users, placed_at__date=today)
    )
    total_bets_today = tickets_today_qs.count()
    total_stake_today = tickets_today_qs.aggregate(s=Coalesce(Sum('stake_amount'), Value(0), output_field=DecimalField()))['s']
    total_payouts_today = tickets_today_qs.filter(status='won').aggregate(s=Coalesce(Sum('max_winning'), Value(0), output_field=DecimalField()))['s']
    revenue_today = (total_stake_today or Decimal('0.00')) - (total_payouts_today or Decimal('0.00'))

    deposits_today = (
        Transaction.objects.filter(user__in=network_users, transaction_type='deposit', status='completed', is_successful=True, timestamp__date=today)
        .aggregate(s=Coalesce(Sum('amount'), Value(0), output_field=DecimalField()))['s']
    )
    withdrawals_today = (
        UserWithdrawal.objects.filter(user__in=network_users, request_time__date=today)
        .aggregate(s=Coalesce(Sum('amount'), Value(0), output_field=DecimalField()))['s']
    )
    pending_withdrawals_count = UserWithdrawal.objects.filter(user__in=network_users, status='pending').count()
    active_betting_shops = agents.filter(is_active=True).exclude(shop_address__isnull=True).exclude(shop_address__exact='').count()

    pending_withdrawals_tab_qs = (
        UserWithdrawal.objects.select_related('user')
        .filter(user__in=network_users, status='pending')
        .order_by('-request_time')
    )
    if start_dt:
        pending_withdrawals_tab_qs = pending_withdrawals_tab_qs.filter(request_time__gte=start_dt)
    if end_dt:
        pending_withdrawals_tab_qs = pending_withdrawals_tab_qs.filter(request_time__lte=end_dt)
    pending_withdrawals_tab_count = pending_withdrawals_tab_qs.count()
    pending_withdrawals_tab_page = Paginator(pending_withdrawals_tab_qs, 50).get_page(request.GET.get('pending_withdrawals_page') or 1) if active_tab == 'pending_withdrawals' else None

    pending_cashiers_tab_qs = (
        CashierRegistrationRequest.objects.select_related('agent')
        .filter(status='PENDING', agent__in=agents)
        .order_by('-created_at')
    )
    if start_dt:
        pending_cashiers_tab_qs = pending_cashiers_tab_qs.filter(created_at__gte=start_dt)
    if end_dt:
        pending_cashiers_tab_qs = pending_cashiers_tab_qs.filter(created_at__lte=end_dt)
    pending_cashiers_tab_count = pending_cashiers_tab_qs.count()
    pending_cashiers_tab_page = Paginator(pending_cashiers_tab_qs, 50).get_page(request.GET.get('pending_cashiers_page') or 1) if active_tab == 'pending_cashiers' else None

    pending_agents_tab_qs = (
        PendingAgentRegistration.objects.select_related('master_agent', 'super_agent', 'registered_by')
        .filter(status='PENDING', registered_by=request.user)
        .order_by('-created_at')
    )
    if start_dt:
        pending_agents_tab_qs = pending_agents_tab_qs.filter(created_at__gte=start_dt)
    if end_dt:
        pending_agents_tab_qs = pending_agents_tab_qs.filter(created_at__lte=end_dt)
    pending_agents_tab_count = pending_agents_tab_qs.count()
    pending_agents_tab_page = Paginator(pending_agents_tab_qs, 50).get_page(request.GET.get('pending_agents_page') or 1) if active_tab == 'pending_agents' else None

    tickets_range_qs = (
        BetTicket.objects.exclude(status__in=['deleted', 'cancelled'])
        .filter(user__in=network_users, placed_at__gte=metrics_start_dt, placed_at__lte=metrics_end_dt)
    )
    total_stake_amount = tickets_range_qs.aggregate(s=Coalesce(Sum('stake_amount'), Value(0), output_field=DecimalField()))['s']
    total_winning_payouts = tickets_range_qs.filter(status='won').aggregate(s=Coalesce(Sum('max_winning'), Value(0), output_field=DecimalField()))['s']
    total_revenue_generated = (total_stake_amount or Decimal('0.00')) - (total_winning_payouts or Decimal('0.00'))

    commission_earned = (
        Transaction.objects.filter(user__in=agents, transaction_type='commission_payout', status='completed', is_successful=True, timestamp__gte=metrics_start_dt, timestamp__lte=metrics_end_dt)
        .aggregate(s=Coalesce(Sum('amount'), Value(0), output_field=DecimalField()))['s']
    )

    chart_cache_key = f"retail:charts:{request.user.id}:{metrics_start_date.isoformat()}:{metrics_end_date.isoformat()}"
    charts_data = cache.get(chart_cache_key)
    if charts_data is None:
        day_count = (metrics_end_date - metrics_start_date).days + 1
        days = [metrics_start_date + timedelta(days=i) for i in range(max(0, day_count))]

        tickets_daily = (
            tickets_range_qs
            .values('placed_at__date')
            .annotate(
                bets=Count('id'),
                stake=Coalesce(Sum('stake_amount'), Value(0), output_field=DecimalField()),
                payouts=Coalesce(Sum(Case(When(status='won', then='max_winning'), default=Value(0), output_field=DecimalField())), Value(0), output_field=DecimalField()),
            )
        )
        tmap = {row['placed_at__date']: row for row in tickets_daily}

        tx_daily = (
            Transaction.objects.filter(user__in=network_users, status='completed', is_successful=True, timestamp__gte=metrics_start_dt, timestamp__lte=metrics_end_dt)
            .values('timestamp__date', 'transaction_type')
            .annotate(total=Coalesce(Sum('amount'), Value(0), output_field=DecimalField()))
        )
        dep_map = {}
        wdr_map = {}
        for row in tx_daily:
            d = row['timestamp__date']
            if row['transaction_type'] == 'deposit':
                dep_map[d] = dep_map.get(d, Decimal('0.00')) + (row['total'] or Decimal('0.00'))
            if row['transaction_type'] == 'withdrawal':
                wdr_map[d] = wdr_map.get(d, Decimal('0.00')) + (row['total'] or Decimal('0.00'))

        labels = [d.isoformat() for d in days]
        bets_series = []
        stake_series = []
        payouts_series = []
        deposits_series = []
        withdrawals_series = []
        for d in days:
            row = tmap.get(d)
            bets_series.append(int(row['bets']) if row else 0)
            stake_series.append(float(row['stake']) if row else 0.0)
            payouts_series.append(float(row['payouts']) if row else 0.0)
            deposits_series.append(float(dep_map.get(d, Decimal('0.00'))))
            withdrawals_series.append(float(wdr_map.get(d, Decimal('0.00'))))

        charts_data = {
            'labels': labels,
            'bets': bets_series,
            'stake': stake_series,
            'payouts': payouts_series,
            'deposits': deposits_series,
            'withdrawals': withdrawals_series,
        }
        cache.set(chart_cache_key, charts_data, 60)

    top_agents = []
    try:
        top_agents = list(
            tickets_range_qs.filter(user__agent__isnull=False)
            .values('user__agent_id', 'user__agent__email', 'user__agent__username')
            .annotate(stake=Coalesce(Sum('stake_amount'), Value(0), output_field=DecimalField()), bets=Count('id'))
            .order_by('-stake')[:10]
        )
    except Exception:
        top_agents = []

    overview_downline_rows = []
    if active_tab == 'overview':
        mapped_super_agents = list(
            super_agents.select_related('master_agent', 'wallet').order_by('email')
        )
        overview_agents = list(
            agents.filter(super_agent__in=super_agents)
            .select_related('super_agent', 'wallet')
            .prefetch_related(
                Prefetch(
                    'agents_under',
                    queryset=User.objects.filter(user_type='cashier').select_related('wallet').order_by('email'),
                    to_attr='overview_cashiers',
                )
            )
            .order_by('super_agent__email', 'email')
        )
        agents_by_super_agent = {}
        for ag in overview_agents:
            cashiers = list(getattr(ag, 'overview_cashiers', []))
            agent_balance = getattr(getattr(ag, 'wallet', None), 'balance', None) or Decimal('0.00')
            cashier_total = sum(
                (getattr(getattr(cashier, 'wallet', None), 'balance', None) or Decimal('0.00'))
                for cashier in cashiers
            )
            total_balance = agent_balance + cashier_total
            agents_by_super_agent.setdefault(ag.super_agent_id, []).append(
                {
                    'agent': ag,
                    'cashier_count': len(cashiers),
                    'agent_balance': agent_balance,
                    'cashier_total_balance': cashier_total,
                    'total_balance': total_balance,
                }
            )
        for sa in mapped_super_agents:
            agent_rows = agents_by_super_agent.get(sa.id, [])
            overview_downline_rows.append(
                {
                    'super_agent': sa,
                    'agents': agent_rows,
                    'agent_count': len(agent_rows),
                    'total_balance': sum((row['total_balance'] for row in agent_rows), Decimal('0.00')),
                }
            )

    locked_accounts_summary = {
        'locked_downlines': _scoped_locked_accounts_queryset(request.user).count(),
        'pending_appeals': _scoped_account_unlock_appeals_queryset(request.user).filter(status='pending').count(),
    }
    locked_accounts_page = None
    locked_accounts_rows = []
    if active_tab == 'locked_accounts':
        locked_accounts_qs = _apply_locked_accounts_filters(
            _scoped_locked_accounts_queryset(request.user),
            query=locked_q,
            user_type=locked_user_type,
            status=locked_status,
            locked_by=locked_by,
            locked_start_dt=locked_start_dt,
            locked_end_dt=locked_end_dt,
            appeal_start_dt=locked_appeal_start_dt,
            appeal_end_dt=locked_appeal_end_dt,
        )
        locked_accounts_page = Paginator(locked_accounts_qs, 50).get_page(request.GET.get('locked_page') or 1)
        locked_accounts_rows = _attach_locked_account_metadata(list(locked_accounts_page.object_list))

    hierarchy = []
    if active_tab == 'hierarchy':
        last_bet_at_subq = Subquery(
            BetTicket.objects.filter(user__agent_id=OuterRef('id'))
            .exclude(status__in=['deleted', 'cancelled'])
            .order_by('-placed_at')
            .values('placed_at')[:1]
        )
        mas_list = list(master_agents.select_related('state').order_by('email'))
        sas_list = list(super_agents.select_related('state', 'master_agent').order_by('email'))
        agents_list = list(
            agents.select_related('state', 'master_agent', 'super_agent')
            .annotate(last_bet_at=last_bet_at_subq)
            .order_by('email')
        )
        agents_by_sa = {}
        for ag in agents_list:
            agents_by_sa.setdefault(getattr(ag, 'super_agent_id', None), []).append(ag)
        sas_by_ma = {}
        for sa in sas_list:
            sas_by_ma.setdefault(getattr(sa, 'master_agent_id', None), []).append(sa)
        direct_agents_by_ma = {}
        for ag in agents_list:
            if ag.super_agent_id is None and ag.master_agent_id is not None:
                direct_agents_by_ma.setdefault(ag.master_agent_id, []).append(ag)
        for ma in mas_list:
            node = {'master_agent': ma, 'super_agents': [], 'direct_agents': direct_agents_by_ma.get(ma.id, [])}
            for sa in sas_by_ma.get(ma.id, []):
                node['super_agents'].append({'super_agent': sa, 'agents': agents_by_sa.get(sa.id, [])})
            hierarchy.append(node)

    bet_tickets_page = None
    agent_filter_options = []
    if active_tab == 'bets':
        bets_qs = tickets_range_qs.select_related('user', 'user__agent', 'user__super_agent', 'user__master_agent').order_by('-placed_at')
        if bet_q:
            bets_qs = bets_qs.filter(
                Q(ticket_id__icontains=bet_q) |
                Q(user__email__icontains=bet_q) |
                Q(user__username__icontains=bet_q) |
                Q(user__phone_number__icontains=bet_q)
            )
        if bet_status:
            bets_qs = bets_qs.filter(status=bet_status)
        if bet_agent_id:
            try:
                bets_qs = bets_qs.filter(user__agent_id=int(bet_agent_id))
            except Exception:
                pass
        bet_paginator = Paginator(bets_qs, 50)
        bet_page_num = request.GET.get('bets_page') or 1
        try:
            bet_tickets_page = bet_paginator.page(bet_page_num)
        except Exception:
            bet_tickets_page = bet_paginator.page(1)
        agent_filter_options = list(agents.only('id', 'email', 'username').order_by('email')[:200])

    tx_page = None
    withdrawals_page = None
    if active_tab == 'finance':
        tx_qs = (
            Transaction.objects.filter(user__in=network_users, status='completed', is_successful=True, timestamp__gte=metrics_start_dt, timestamp__lte=metrics_end_dt)
            .select_related('user', 'initiating_user')
            .order_by('-timestamp')
        )
        if tx_type:
            tx_qs = tx_qs.filter(transaction_type=tx_type)
        if q:
            tx_qs = tx_qs.filter(Q(user__email__icontains=q) | Q(user__username__icontains=q) | Q(user__phone_number__icontains=q))
        tx_p = Paginator(tx_qs, 50)
        try:
            tx_page = tx_p.page(request.GET.get('tx_page') or 1)
        except Exception:
            tx_page = tx_p.page(1)

        w_qs = (
            UserWithdrawal.objects.filter(user__in=network_users, request_time__gte=metrics_start_dt, request_time__lte=metrics_end_dt)
            .select_related('user')
            .order_by('-request_time')
        )
        if q:
            w_qs = w_qs.filter(Q(user__email__icontains=q) | Q(user__username__icontains=q) | Q(user__phone_number__icontains=q))
        w_p = Paginator(w_qs, 50)
        try:
            withdrawals_page = w_p.page(request.GET.get('w_page') or 1)
        except Exception:
            withdrawals_page = w_p.page(1)

    commission_rows = []
    commission_period_options = []
    selected_commission_period_id = ''
    commission_page = None
    if active_tab == 'commissions':
        selected_commission_period_id = (request.GET.get('commission_period') or '').strip()
        try:
            CommissionPeriod = apps.get_model('commission', 'CommissionPeriod')
            WeeklyAgentCommission = apps.get_model('commission', 'WeeklyAgentCommission')
            from commission.services import calculate_weekly_agent_commission_data

            period_qs = CommissionPeriod.objects.filter(period_type='weekly').order_by('-start_date')
            commission_period_options = list(period_qs[:200])

            selected_period = None
            if selected_commission_period_id:
                try:
                    selected_period = CommissionPeriod.objects.filter(id=int(selected_commission_period_id), period_type='weekly').first()
                except Exception:
                    selected_period = None

            if selected_period is None:
                selected_period = period_qs.first()
                selected_commission_period_id = str(selected_period.id) if selected_period else ''

            filtered_agents = agents
            if commission_agent_q:
                filtered_agents = filtered_agents.filter(
                    Q(username__icontains=commission_agent_q) |
                    Q(email__icontains=commission_agent_q)
                )

            comm_qs = WeeklyAgentCommission.objects.filter(agent__in=filtered_agents).select_related('agent', 'period')
            if selected_period:
                comm_qs = comm_qs.filter(period=selected_period)

            comm_map = {c.agent_id: c for c in comm_qs}
            commission_rows = []
            for ag in filtered_agents.only('id', 'username', 'email', 'phone_number').order_by('email'):
                rec = comm_map.get(ag.id)
                calc = calculate_weekly_agent_commission_data(ag, selected_period) if selected_period else None
                calc_total = None
                if isinstance(calc, dict):
                    calc_total = calc.get('commission_total_amount', None)
                if calc_total is None:
                    calc_total = getattr(rec, 'commission_total_amount', None) if rec else None
                if calc_total is None:
                    calc_total = Decimal('0.00')
                commission_rows.append(
                    {
                        'agent_id': ag.id,
                        'agent_username': (ag.username or '').strip() or (ag.email or '').strip() or '-',
                        'agent_phone_number': (ag.phone_number or '').strip() or '-',
                        'total': calc_total,
                        'partially_paid': getattr(rec, 'amount_paid', Decimal('0.00')) if rec else Decimal('0.00'),
                        'status': getattr(rec, 'status', 'pending') if rec else 'pending',
                    }
                )
            commission_page = Paginator(commission_rows, 50).get_page(request.GET.get('commission_page') or 1)
        except Exception:
            commission_rows = []
            commission_period_options = []
            selected_commission_period_id = ''
            commission_page = Paginator([], 50).get_page(1)

    risk_logs_page = None
    risk_kind = (request.GET.get('risk_kind') or '').strip()
    if active_tab == 'risk':
        SuspiciousActivityLog = apps.get_model('risk', 'SuspiciousActivityLog')
        risk_qs = SuspiciousActivityLog.objects.select_related('user', 'ticket').filter(user__in=network_users).order_by('-created_at')
        if start_dt:
            risk_qs = risk_qs.filter(created_at__gte=start_dt)
        if end_dt:
            risk_qs = risk_qs.filter(created_at__lte=end_dt)
        if risk_kind:
            risk_qs = risk_qs.filter(kind=risk_kind)
        risk_paginator = Paginator(risk_qs, 50)
        try:
            risk_logs_page = risk_paginator.page(request.GET.get('risk_page') or 1)
        except Exception:
            risk_logs_page = risk_paginator.page(1)

    shops_page = None
    if active_tab == 'shops':
        base_qs = agents.select_related('state', 'master_agent', 'super_agent').order_by('email')
        if shop_q:
            base_qs = base_qs.filter(
                Q(email__icontains=shop_q) |
                Q(username__icontains=shop_q) |
                Q(shop_address__icontains=shop_q)
            )
        if shop_state:
            try:
                base_qs = base_qs.filter(state_id=int(shop_state))
            except Exception:
                pass
        if shop_active in ['1', '0']:
            base_qs = base_qs.filter(is_active=(shop_active == '1'))

        ticket_scope = (
            BetTicket.objects.exclude(status__in=['deleted', 'cancelled'])
            .filter(placed_at__gte=metrics_start_dt, placed_at__lte=metrics_end_dt)
            .filter(Q(user_id=OuterRef('pk')) | Q(user__agent_id=OuterRef('pk')))
            .order_by()
        )
        stake_sub = ticket_scope.annotate(_=Value(1)).values('_').annotate(total=Sum('stake_amount')).values('total')[:1]
        bets_sub = ticket_scope.annotate(_=Value(1)).values('_').annotate(total=Count('id')).values('total')[:1]
        payout_sub = ticket_scope.filter(status='won').annotate(_=Value(1)).values('_').annotate(total=Sum('max_winning')).values('total')[:1]

        shops_qs = (
            base_qs.annotate(
                players_count=Count('agents_under', filter=Q(agents_under__user_type='player'), distinct=True),
                cashiers_count=Count('agents_under', filter=Q(agents_under__user_type='cashier'), distinct=True),
                bets_count=Coalesce(Subquery(bets_sub), Value(0), output_field=IntegerField()),
                stake_sum=Coalesce(Subquery(stake_sub), Value(0), output_field=DecimalField()),
                payout_sum=Coalesce(Subquery(payout_sub), Value(0), output_field=DecimalField()),
                revenue_sum=Coalesce(Subquery(stake_sub), Value(0), output_field=DecimalField()) - Coalesce(Subquery(payout_sub), Value(0), output_field=DecimalField()),
            )
        )
        shops_paginator = Paginator(shops_qs, 50)
        try:
            shops_page = shops_paginator.page(request.GET.get('shops_page') or 1)
        except Exception:
            shops_page = shops_paginator.page(1)

    players_page = None
    if active_tab == 'players':
        players_qs = (
            network_users.filter(user_type='player')
            .select_related('wallet', 'agent', 'super_agent', 'master_agent', 'state', 'vip_manager')
            .order_by('-date_joined')
        )
        if player_q:
            players_qs = players_qs.filter(
                Q(email__icontains=player_q) |
                Q(username__icontains=player_q) |
                Q(phone_number__icontains=player_q) |
                Q(first_name__icontains=player_q) |
                Q(last_name__icontains=player_q)
            )
        if player_status in ['1', '0']:
            players_qs = players_qs.filter(is_active=(player_status == '1'))
        if player_kyc:
            players_qs = players_qs.filter(kyc_status=player_kyc)

        players_paginator = Paginator(players_qs, 50)
        try:
            players_page = players_paginator.page(request.GET.get('players_page') or 1)
        except Exception:
            players_page = players_paginator.page(1)

    dormant_cards, dormant_rows = _build_dormant_center_data(
        request.user,
        query=dormant_q,
        agent_id=dormant_agent,
        super_agent_id=dormant_super_agent,
        retail_manager_id='',
        status=dormant_status,
        bucket=dormant_bucket,
        start_dt=start_dt,
        end_dt=end_dt,
    )
    dormant_page = Paginator(dormant_rows, 50).get_page(request.GET.get('dormant_page') or 1) if active_tab == 'dormant_accounts' else None

    complaint_stats = {
        'open': _complaint_scope_queryset(request.user).filter(status='open').count(),
        'pending': _complaint_scope_queryset(request.user).filter(status='pending').count(),
        'resolved': _complaint_scope_queryset(request.user).filter(status='resolved').count(),
        'escalated': _complaint_scope_queryset(request.user).filter(status='escalated').count(),
    }
    complaints_page = None
    if active_tab == 'complaints':
        complaints_qs = _apply_complaint_filters(
            _complaint_scope_queryset(request.user),
            query=complaint_q,
            complaint_type=complaint_type,
            status=complaint_status,
            priority=complaint_priority,
            start_dt=start_dt,
            end_dt=end_dt,
        )
        complaints_page = Paginator(complaints_qs, 25).get_page(request.GET.get('complaint_page') or 1)

    performance_rows = []
    performance_page = None
    performance_chart = {'labels': [], 'turnover': [], 'ggr': [], 'commission': []}
    performance_top_super_agents = []
    performance_top_agents = []
    if active_tab == 'agent_performance':
        performance_rows, performance_chart = _build_agent_performance_rows(
            request.user,
            entity_type=performance_entity if performance_entity in ['super_agent', 'agent'] else 'super_agent',
            start_dt=metrics_start_dt,
            end_dt=metrics_end_dt,
            query=performance_q,
        )
        performance_page = Paginator(performance_rows, 25).get_page(request.GET.get('performance_page') or 1)
        performance_top_super_agents = _build_agent_performance_rows(request.user, entity_type='super_agent', start_dt=metrics_start_dt, end_dt=metrics_end_dt, query='')[0][:10]
        performance_top_agents = _build_agent_performance_rows(request.user, entity_type='agent', start_dt=metrics_start_dt, end_dt=metrics_end_dt, query='')[0][:10]

    overdraft_reporting_context = {}
    if active_tab == 'overdraft_monitoring':
        overdraft_reporting_context = _build_overdraft_reporting_dashboard_context(
            request,
            include_retail_manager=False,
            extra_params={'tab': 'overdraft_monitoring'},
        )

    context = {
        'active_tab': active_tab,
        'start_date': start_date_str,
        'end_date': end_date_str,
        'metrics_label': metrics_label,
        'kpis': {
            'total_mapped_master_agents': total_mapped_master_agents,
            'total_mapped_super_agents': total_mapped_super_agents,
            'total_mapped_agents': total_mapped_agents,
            'total_active_players': total_active_players,
            'online_users': online_users,
            'total_bets_today': total_bets_today,
            'total_stake_today': total_stake_today,
            'total_payouts_today': total_payouts_today,
            'revenue_today': revenue_today,
            'total_stake_amount': total_stake_amount,
            'total_winning_payouts': total_winning_payouts,
            'total_revenue_generated': total_revenue_generated,
            'commission_earned': commission_earned,
            'deposits_today': deposits_today,
            'withdrawals_today': withdrawals_today,
            'pending_withdrawals_count': pending_withdrawals_count,
            'active_betting_shops': active_betting_shops,
        },
        'charts_data': charts_data,
        'top_agents': top_agents,
        'overview_downline_rows': overview_downline_rows,
        'hierarchy': hierarchy,
        'bet_q': bet_q,
        'bet_status': bet_status,
        'bet_agent': bet_agent_id,
        'bet_tickets_page': bet_tickets_page,
        'agent_filter_options': agent_filter_options,
        'tx_type': tx_type,
        'q': q,
        'tx_page': tx_page,
        'withdrawals_page': withdrawals_page,
        'commission_rows': commission_rows,
        'commission_page': commission_page,
        'commission_period_options': commission_period_options,
        'selected_commission_period_id': selected_commission_period_id,
        'commission_agent_q': commission_agent_q,
        'risk_kind': risk_kind,
        'risk_logs_page': risk_logs_page,
        'shop_q': shop_q,
        'shop_state': shop_state,
        'shop_active': shop_active,
        'shops_page': shops_page,
        'player_q': player_q,
        'player_status': player_status,
        'player_kyc': player_kyc,
        'players_page': players_page,
        'states': list(State.objects.all().order_by('state_name')),
        'mapped_super_agents': list(super_agents.only('id', 'email', 'username').order_by('email')[:200]),
        'note_form': note_form,
        'note_entry': note_entry,
        'locked_accounts_summary': locked_accounts_summary,
        'locked_accounts_page': locked_accounts_page,
        'locked_accounts_rows': locked_accounts_rows,
        'locked_q': locked_q,
        'locked_user_type': locked_user_type,
        'locked_status': locked_status,
        'locked_by': locked_by,
        'locked_start_date': locked_start_date,
        'locked_end_date': locked_end_date,
        'locked_appeal_start_date': locked_appeal_start_date,
        'locked_appeal_end_date': locked_appeal_end_date,
        'dormant_q': dormant_q,
        'dormant_bucket': dormant_bucket,
        'dormant_agent': dormant_agent,
        'dormant_super_agent': dormant_super_agent,
        'dormant_status': dormant_status,
        'dormant_cards': dormant_cards,
        'dormant_page': dormant_page,
        'dormant_agents_tab_count': len(dormant_rows),
        'pending_withdrawals_tab_count': pending_withdrawals_tab_count,
        'pending_cashiers_tab_count': pending_cashiers_tab_count,
        'pending_agents_tab_count': pending_agents_tab_count,
        'pending_withdrawals_tab_page': pending_withdrawals_tab_page,
        'pending_cashiers_tab_page': pending_cashiers_tab_page,
        'pending_agents_tab_page': pending_agents_tab_page,
        'complaint_q': complaint_q,
        'complaint_type': complaint_type,
        'complaint_status': complaint_status,
        'complaint_priority': complaint_priority,
        'complaint_stats': complaint_stats,
        'complaints_page': complaints_page,
        'complaint_form': complaint_form,
        'complaint_action_form': complaint_action_form,
        'complaint_action_target_id': complaint_action_target_id,
        'complaint_note_form': complaint_note_form,
        'complaint_note_target_id': complaint_note_target_id,
        'performance_entity': performance_entity,
        'performance_q': performance_q,
        'performance_page': performance_page,
        'performance_chart': performance_chart,
        'performance_top_super_agents': performance_top_super_agents,
        'performance_top_agents': performance_top_agents,
        'complaint_type_choices': CustomerComplaint.COMPLAINT_TYPE_CHOICES,
        'complaint_status_choices': CustomerComplaint.STATUS_CHOICES,
        'complaint_priority_choices': CustomerComplaint.PRIORITY_CHOICES,
        'ticket_transactions_widget': _ticket_transaction_widget_context(
            request.user,
            limit=12,
            date_from=start_date_str,
            date_to=end_date_str,
        ),
        **overdraft_reporting_context,
    }
    return render(request, 'betting/retail_dashboard.html', context)


@login_required
@user_passes_test_403(is_retail_manager)
def retail_player_detail(request, user_id):
    target = get_object_or_404(User, id=user_id, user_type='player')
    if not get_retail_network_users_qs(request.user).filter(id=target.id).exists():
        raise Http404()

    wallet = Wallet.objects.filter(user=target).first()
    tickets = BetTicket.objects.filter(user=target).order_by('-placed_at')[:50]
    withdrawals = UserWithdrawal.objects.filter(user=target).order_by('-request_time')[:30]
    deposits = Transaction.objects.filter(user=target, transaction_type='deposit').order_by('-timestamp')[:30]
    txs = Transaction.objects.filter(user=target).order_by('-timestamp')[:50]

    login_attempts = LoginAttempt.objects.filter(user=target, status='success').order_by('-timestamp')[:25]

    DeviceFingerprint = apps.get_model('risk', 'DeviceFingerprint')
    SuspiciousActivityLog = apps.get_model('risk', 'SuspiciousActivityLog')
    IPIntelligence = apps.get_model('risk', 'IPIntelligence')

    device_fingerprints = []
    suspicious_logs = []
    ip_intel = None
    last_ip = None
    try:
        device_fingerprints = list(DeviceFingerprint.objects.filter(user=target).order_by('-last_seen_at')[:25])
    except Exception:
        device_fingerprints = []
    try:
        suspicious_logs = list(SuspiciousActivityLog.objects.filter(user=target).select_related('ticket').order_by('-created_at')[:25])
    except Exception:
        suspicious_logs = []
    try:
        last_ip = (login_attempts[0].ip_address if login_attempts else None) or (device_fingerprints[0].ip_address if device_fingerprints else None)
    except Exception:
        last_ip = None
    if last_ip:
        try:
            ip_intel = IPIntelligence.objects.filter(ip_address=last_ip).first()
        except Exception:
            ip_intel = None

    context = {
        'target_user': target,
        'wallet': wallet,
        'tickets': tickets,
        'withdrawals': withdrawals,
        'deposits': deposits,
        'transactions': txs,
        'login_attempts': login_attempts,
        'device_fingerprints': device_fingerprints,
        'suspicious_logs': suspicious_logs,
        'ip_intel': ip_intel,
    }
    return render(request, 'betting/retail_player_detail.html', context)


@login_required
@user_passes_test_403(is_retail_manager)
def retail_export(request):
    dataset = (request.GET.get('dataset') or '').strip().lower()
    fmt = (request.GET.get('format') or 'csv').strip().lower()
    start_date_str = (request.GET.get('start_date') or '').strip()
    end_date_str = (request.GET.get('end_date') or '').strip()
    dormant_q = (request.GET.get('dormant_q') or '').strip()
    dormant_bucket = (request.GET.get('dormant_bucket') or 'login_7').strip() or 'login_7'
    dormant_agent = (request.GET.get('dormant_agent') or '').strip()
    dormant_super_agent = (request.GET.get('dormant_super_agent') or '').strip()
    dormant_status = (request.GET.get('dormant_status') or '').strip()
    complaint_q = (request.GET.get('complaint_q') or '').strip()
    complaint_type = (request.GET.get('complaint_type') or '').strip()
    complaint_status = (request.GET.get('complaint_status') or '').strip()
    complaint_priority = (request.GET.get('complaint_priority') or '').strip()
    performance_entity = (request.GET.get('performance_entity') or 'super_agent').strip() or 'super_agent'
    performance_q = (request.GET.get('performance_q') or '').strip()

    start_dt = None
    end_dt = None
    try:
        if start_date_str:
            start_dt = timezone.make_aware(datetime.strptime(start_date_str, "%Y-%m-%d"))
    except Exception:
        start_dt = None
    try:
        if end_date_str:
            end_raw = datetime.strptime(end_date_str, "%Y-%m-%d")
            end_dt = timezone.make_aware(datetime.combine(end_raw.date(), datetime.max.time()))
    except Exception:
        end_dt = None

    today = timezone.localdate()
    if not start_dt:
        start_dt = timezone.make_aware(datetime.combine(today - timedelta(days=30), datetime.min.time()))
    if not end_dt:
        end_dt = timezone.make_aware(datetime.combine(today, datetime.max.time()))

    master_agents = get_retail_manager_master_agents(request.user)
    super_agents = get_retail_manager_super_agents(request.user, master_agents_qs=master_agents)
    agents = get_retail_manager_agents(request.user, master_agents_qs=master_agents, super_agents_qs=super_agents)
    network_users = get_retail_network_users_qs(request.user)

    rows = []
    title = f"{dataset or 'report'}"

    if dataset == 'overdrafts':
        report_context = _build_overdraft_reporting_dashboard_context(
            request,
            include_retail_manager=False,
            extra_params={'tab': 'overdraft_monitoring'},
        )
        rows = _loan_reporting_export_rows(
            report_context['overdraft_reporting']['rows'],
            include_retail_manager=False,
        )
        title = 'retail_overdraft_monitoring'

    elif dataset == 'bets':
        qs = (
            BetTicket.objects.exclude(status__in=['deleted', 'cancelled'])
            .filter(user__in=network_users, placed_at__gte=start_dt, placed_at__lte=end_dt)
            .select_related('user')
            .order_by('-placed_at')
        )
        for t in qs[:50000]:
            rows.append({
                'time': t.placed_at.isoformat(sep=' ', timespec='seconds'),
                'ticket_id': t.ticket_id,
                'user': t.user.email or t.user.username,
                'stake': str(t.stake_amount),
                'status': t.status,
                'max_winning': str(t.max_winning),
            })
        title = "bets"

    elif dataset == 'transactions':
        qs = (
            Transaction.objects.filter(user__in=network_users, status='completed', is_successful=True)
            .filter(timestamp__gte=start_dt, timestamp__lte=end_dt)
            .select_related('user')
            .order_by('-timestamp')
        )
        for tx in qs[:50000]:
            rows.append({
                'time': tx.timestamp.isoformat(sep=' ', timespec='seconds'),
                'user': tx.user.email or tx.user.username,
                'type': tx.transaction_type,
                'amount': str(tx.amount),
                'status': tx.status,
                'gateway': getattr(tx, 'payment_gateway', ''),
            })
        title = "transactions"

    elif dataset == 'withdrawals':
        qs = (
            UserWithdrawal.objects.filter(user__in=network_users)
            .filter(request_time__gte=start_dt, request_time__lte=end_dt)
            .select_related('user')
            .order_by('-request_time')
        )
        for w in qs[:50000]:
            rows.append({
                'time': w.request_time.isoformat(sep=' ', timespec='seconds'),
                'user': w.user.email or w.user.username,
                'amount': str(w.amount),
                'status': w.status,
                'bank': getattr(w, 'bank_name', ''),
                'account': getattr(w, 'account_number', ''),
            })
        title = "withdrawals"

    elif dataset == 'commissions':
        qs = (
            Transaction.objects.filter(user__in=agents, transaction_type='commission_payout', status='completed', is_successful=True)
            .filter(timestamp__gte=start_dt, timestamp__lte=end_dt)
            .select_related('user')
            .order_by('-timestamp')
        )
        for tx in qs[:50000]:
            rows.append({
                'time': tx.timestamp.isoformat(sep=' ', timespec='seconds'),
                'agent': tx.user.email or tx.user.username,
                'amount': str(tx.amount),
                'status': tx.status,
            })
        title = "commissions"

    elif dataset == 'players':
        qs = (
            network_users.filter(user_type='player')
            .select_related('wallet', 'agent', 'super_agent', 'master_agent', 'state')
            .order_by('-date_joined')
        )
        for u in qs[:50000]:
            rows.append({
                'joined': u.date_joined.isoformat(sep=' ', timespec='seconds') if u.date_joined else '',
                'user': u.email or u.username,
                'phone': u.phone_number or '',
                'state': getattr(u.state, 'state_name', '') if u.state_id else '',
                'kyc': getattr(u, 'kyc_status', ''),
                'vip': getattr(u, 'vip_level', ''),
                'wallet_balance': str(getattr(getattr(u, 'wallet', None), 'balance', '') or ''),
                'agent': getattr(getattr(u, 'agent', None), 'email', '') if getattr(u, 'agent_id', None) else '',
            })
        title = "players"

    elif dataset == 'shops':
        qs = agents.select_related('state').order_by('email')
        for a in qs[:50000]:
            rows.append({
                'shop': a.email or a.username,
                'state': getattr(a.state, 'state_name', '') if a.state_id else '',
                'address': a.shop_address or '',
                'active': 'yes' if a.is_active else 'no',
            })
        title = "shops"

    elif dataset == 'dormant_accounts':
        _, dormant_rows = _build_dormant_center_data(
            request.user,
            query=dormant_q,
            agent_id=dormant_agent,
            super_agent_id=dormant_super_agent,
            retail_manager_id='',
            status=dormant_status,
            bucket=dormant_bucket,
            start_dt=start_dt,
            end_dt=end_dt,
        )
        for user_obj in dormant_rows:
            rows.append({
                'username': user_obj.username or user_obj.email or '',
                'full_name': user_obj.get_full_name() or '',
                'last_login': user_obj.last_login.isoformat(sep=' ', timespec='seconds') if user_obj.last_login else '',
                'last_bet': user_obj.last_bet_at.isoformat(sep=' ', timespec='seconds') if getattr(user_obj, 'last_bet_at', None) else '',
                'last_transaction': user_obj.last_transaction_at.isoformat(sep=' ', timespec='seconds') if getattr(user_obj, 'last_transaction_at', None) else '',
                'last_wallet_activity': user_obj.last_wallet_activity_at.isoformat(sep=' ', timespec='seconds') if getattr(user_obj, 'last_wallet_activity_at', None) else '',
                'last_activity': user_obj.last_activity_at.isoformat(sep=' ', timespec='seconds') if getattr(user_obj, 'last_activity_at', None) else '',
                'super_agent': getattr(getattr(user_obj, 'super_agent', None), 'username', '') or getattr(getattr(user_obj, 'super_agent', None), 'email', '') or '',
                'cashiers': getattr(user_obj, 'cashiers_count', 0) or 0,
                'dormant_days': getattr(user_obj, 'dormant_days', '') if getattr(user_obj, 'dormant_days', None) is not None else '',
                'wallet_balance': str(getattr(user_obj, 'wallet_balance_annotated', Decimal('0.00')) or Decimal('0.00')),
                'status': 'Locked' if user_obj.is_locked else ('Active' if user_obj.is_active else 'Inactive'),
            })
        title = "retail_dormant_agents"

    elif dataset == 'complaints':
        qs = _apply_complaint_filters(
            _complaint_scope_queryset(request.user),
            query=complaint_q,
            complaint_type=complaint_type,
            status=complaint_status,
            priority=complaint_priority,
            start_dt=start_dt,
            end_dt=end_dt,
        )
        for complaint in qs[:5000]:
            rows.append({
                'created_at': complaint.created_at.isoformat(sep=' ', timespec='seconds') if complaint.created_at else '',
                'complaint_type': complaint.get_complaint_type_display(),
                'user': complaint.user.username or complaint.user.email or '',
                'subject': complaint.subject,
                'status': complaint.get_status_display(),
                'priority': complaint.get_priority_display(),
                'assigned_to': getattr(complaint.assigned_to, 'email', '') or getattr(complaint.assigned_to, 'username', '') or '',
                'resolved_at': complaint.resolved_at.isoformat(sep=' ', timespec='seconds') if complaint.resolved_at else '',
            })
        title = "complaints"

    elif dataset == 'agent_performance':
        performance_rows, _ = _build_agent_performance_rows(
            request.user,
            entity_type=performance_entity if performance_entity in ['super_agent', 'agent'] else 'super_agent',
            start_dt=start_dt,
            end_dt=end_dt,
            query=performance_q,
        )
        for row in performance_rows:
            entity = row['entity']
            rows.append({
                'entity': entity.username or entity.email or '',
                'entity_type': entity.get_user_type_display(),
                'turnover': str(row['turnover']),
                'ggr': str(row['ggr']),
                'net_ggr': str(row['net_ggr']),
                'commission': str(row['commission']),
                'active_users': row['active_users'],
                'dormant_users': row['dormant_users'],
                'deposit_volume': str(row['deposit_volume']),
                'withdrawal_volume': str(row['withdrawal_volume']),
                'cashiers': row['cashiers_count'],
                'agents': row['agents_count'],
                'active_bettors': row['active_bettors'],
                'tickets_sold': row['tickets_sold'],
                'average_stake': str(row['average_stake']),
                'winning_percentage': str(row['winning_percentage']),
            })
        title = "agent_performance"

    else:
        return HttpResponse("Unknown dataset.", status=400)

    filename_base = f"retail_{title}_{timezone.now().strftime('%Y%m%d_%H%M%S')}"
    return _export_simple_rows(rows=rows, title=filename_base, fmt=fmt)


@login_required
def commission_recall_dashboard(request):
    if not (
        request.user.is_superuser
        or request.user.user_type in ['admin', 'account_user']
        or is_finance_user(request.user)
        or is_crm_user(request.user)
        or is_retail_manager(request.user)
    ):
        return HttpResponseForbidden("Not allowed.")

    from commission.models import WeeklyAgentCommission, MonthlyNetworkCommission, CommissionRecall, CommissionRecallLog
    from commission.services import recall_commission, decide_commission_recall
    from betting.utils import get_client_ip

    config = SiteConfiguration.load()
    can_recall = bool(
        request.user.is_superuser
        or request.user.user_type in ['admin']
        or (request.user.user_type == 'account_user' and config.account_user_commission_authority)
        or request.user.has_perm('commission.can_recall_commission')
    )
    can_approve = bool(
        request.user.is_superuser
        or request.user.user_type in ['admin']
        or request.user.has_perm('commission.can_approve_commission_recall')
    )

    tab = (request.GET.get('tab') or 'queue').strip() or 'queue'
    q = (request.GET.get('q') or '').strip()
    agent_type = (request.GET.get('agent_type') or '').strip()
    status_filter = (request.GET.get('status') or '').strip()
    reason_filter = (request.GET.get('reason') or '').strip()
    start_date = (request.GET.get('start_date') or '').strip()
    end_date = (request.GET.get('end_date') or '').strip()

    if request.method == 'POST':
        action = (request.POST.get('action') or '').strip()

        if action == 'recall':
            if not can_recall:
                messages.error(request, "Not allowed to recall commissions.")
                return redirect(f"{reverse('betting:commission_recall_dashboard')}?tab={tab}")

            commission_type = (request.POST.get('commission_type') or '').strip()
            commission_id = (request.POST.get('commission_id') or '').strip()
            amount_str = (request.POST.get('amount') or '').strip()
            reason = (request.POST.get('reason') or '').strip()
            other_reason_text = (request.POST.get('other_reason_text') or '').strip()
            notes = (request.POST.get('notes') or '').strip()

            if reason == 'other' and not other_reason_text:
                messages.error(request, "Other reason text is required.")
                return redirect(f"{reverse('betting:commission_recall_dashboard')}?tab={tab}")

            try:
                amount = Decimal(amount_str)
            except Exception:
                messages.error(request, "Invalid amount.")
                return redirect(f"{reverse('betting:commission_recall_dashboard')}?tab={tab}")

            try:
                commission_id_int = int(commission_id)
            except Exception:
                messages.error(request, "Invalid commission.")
                return redirect(f"{reverse('betting:commission_recall_dashboard')}?tab={tab}")

            require_approval = bool(config.require_commission_recall_approval and request.user.user_type == 'account_user' and not (request.user.is_superuser or request.user.user_type == 'admin'))
            ok, msg = recall_commission(
                commission_type=commission_type,
                commission_id=commission_id_int,
                amount=amount,
                reason=reason,
                notes=notes,
                actor=request.user,
                ip_address=get_client_ip(request),
                device_info=(request.META.get('HTTP_USER_AGENT') or '')[:255],
                require_approval=require_approval,
                other_reason_text=other_reason_text,
            )
            if ok:
                messages.success(request, msg)
            else:
                messages.error(request, msg)
            return redirect(f"{reverse('betting:commission_recall_dashboard')}?tab={tab}")

        if action == 'bulk_recall':
            if not can_recall:
                messages.error(request, "Not allowed to recall commissions.")
                return redirect(f"{reverse('betting:commission_recall_dashboard')}?tab={tab}")

            selected = request.POST.getlist('selected_items')
            reason = (request.POST.get('reason') or '').strip()
            other_reason_text = (request.POST.get('other_reason_text') or '').strip()
            notes = (request.POST.get('notes') or '').strip()
            if not selected:
                messages.warning(request, "No commissions selected.")
                return redirect(f"{reverse('betting:commission_recall_dashboard')}?tab={tab}")
            if reason == 'other' and not other_reason_text:
                messages.error(request, "Other reason text is required.")
                return redirect(f"{reverse('betting:commission_recall_dashboard')}?tab={tab}")

            require_approval = bool(config.require_commission_recall_approval and request.user.user_type == 'account_user' and not (request.user.is_superuser or request.user.user_type == 'admin'))
            ok_count = 0
            fail_count = 0
            for item in selected:
                try:
                    ctype, cid = item.split('_', 1)
                    cid_int = int(cid)
                except Exception:
                    fail_count += 1
                    continue

                if ctype == 'weekly':
                    rec = WeeklyAgentCommission.objects.filter(id=cid_int).first()
                    if not rec:
                        fail_count += 1
                        continue
                    amount = rec.amount_paid or Decimal('0.00')
                else:
                    rec = MonthlyNetworkCommission.objects.filter(id=cid_int).first()
                    if not rec:
                        fail_count += 1
                        continue
                    amount = rec.amount_paid or Decimal('0.00')

                if amount <= 0:
                    fail_count += 1
                    continue

                ok, _msg = recall_commission(
                    commission_type=ctype,
                    commission_id=cid_int,
                    amount=amount,
                    reason=reason,
                    notes=notes,
                    actor=request.user,
                    ip_address=get_client_ip(request),
                    device_info=(request.META.get('HTTP_USER_AGENT') or '')[:255],
                    require_approval=require_approval,
                    other_reason_text=other_reason_text,
                )
                if ok:
                    ok_count += 1
                else:
                    fail_count += 1

            if ok_count:
                messages.success(request, f"Recalled {ok_count} commission(s).")
            if fail_count:
                messages.warning(request, f"{fail_count} commission(s) could not be recalled.")
            return redirect(f"{reverse('betting:commission_recall_dashboard')}?tab={tab}")

        if action == 'decide':
            if not can_approve:
                messages.error(request, "Not allowed.")
                return redirect(f"{reverse('betting:commission_recall_dashboard')}?tab=requests")

            recall_id = (request.POST.get('recall_id') or '').strip()
            decision = (request.POST.get('decision') or '').strip()
            note = (request.POST.get('note') or '').strip()
            try:
                recall_id_int = int(recall_id)
            except Exception:
                messages.error(request, "Invalid recall request.")
                return redirect(f"{reverse('betting:commission_recall_dashboard')}?tab=requests")

            ok, msg = decide_commission_recall(recall_id=recall_id_int, actor=request.user, decision=decision, note=note)
            if ok:
                messages.success(request, msg)
            else:
                messages.error(request, msg)
            return redirect(f"{reverse('betting:commission_recall_dashboard')}?tab=requests")

    weekly_qs = WeeklyAgentCommission.objects.select_related('agent', 'period', 'paid_by').filter(amount_paid__gt=0)
    monthly_qs = MonthlyNetworkCommission.objects.select_related('user', 'period', 'paid_by').filter(amount_paid__gt=0)

    if status_filter:
        weekly_qs = weekly_qs.filter(status=status_filter)
        monthly_qs = monthly_qs.filter(status=status_filter)
    else:
        weekly_qs = weekly_qs.filter(status__in=['paid', 'partially_paid'])
        monthly_qs = monthly_qs.filter(status__in=['paid', 'partially_paid'])

    if agent_type:
        weekly_qs = weekly_qs.filter(agent__user_type=agent_type)
        monthly_qs = monthly_qs.filter(user__user_type=agent_type)

    if start_date:
        weekly_qs = weekly_qs.filter(paid_at__date__gte=start_date)
        monthly_qs = monthly_qs.filter(paid_at__date__gte=start_date)
    if end_date:
        weekly_qs = weekly_qs.filter(paid_at__date__lte=end_date)
        monthly_qs = monthly_qs.filter(paid_at__date__lte=end_date)

    if q:
        weekly_qs = weekly_qs.filter(Q(agent__email__icontains=q) | Q(agent__username__icontains=q))
        monthly_qs = monthly_qs.filter(Q(user__email__icontains=q) | Q(user__username__icontains=q))

    queue_rows = []
    for wc in weekly_qs.order_by('-paid_at')[:500]:
        queue_rows.append({
            'id_str': f"weekly_{wc.id}",
            'commission_type': 'weekly',
            'commission_id': wc.id,
            'user': wc.agent,
            'period': wc.period,
            'total_amount': wc.commission_total_amount,
            'amount_paid': wc.amount_paid,
            'paid_at': wc.paid_at,
            'paid_by': wc.paid_by,
            'status': wc.status,
        })

    for mc in monthly_qs.order_by('-paid_at')[:500]:
        queue_rows.append({
            'id_str': f"monthly_{mc.id}",
            'commission_type': 'monthly',
            'commission_id': mc.id,
            'user': mc.user,
            'period': mc.period,
            'total_amount': mc.commission_amount,
            'amount_paid': mc.amount_paid,
            'paid_at': mc.paid_at,
            'paid_by': mc.paid_by,
            'status': mc.status,
        })

    queue_rows.sort(key=lambda r: (r['paid_at'] or timezone.now()), reverse=True)

    logs_qs = CommissionRecallLog.objects.select_related('agent', 'recalled_by').all()
    if reason_filter:
        logs_qs = logs_qs.filter(recall_reason=reason_filter)
    if start_date:
        logs_qs = logs_qs.filter(recall_date__gte=start_date)
    if end_date:
        logs_qs = logs_qs.filter(recall_date__lte=end_date)
    if q:
        logs_qs = logs_qs.filter(Q(agent__email__icontains=q) | Q(agent__username__icontains=q) | Q(recalled_by__email__icontains=q))
    recall_logs = logs_qs.order_by('-created_at')[:500]

    requests_qs = CommissionRecall.objects.select_related('beneficiary', 'requested_by', 'decided_by', 'period').filter(status='pending_approval').order_by('-created_at')[:500]

    context = {
        'tab': tab,
        'q': q,
        'agent_type': agent_type,
        'status_filter': status_filter,
        'reason_filter': reason_filter,
        'start_date': start_date,
        'end_date': end_date,
        'can_recall': can_recall,
        'can_approve': can_approve,
        'require_recall_approval': bool(config.require_commission_recall_approval),
        'queue_rows': queue_rows,
        'recall_logs': recall_logs,
        'recall_requests': requests_qs,
        'recall_reasons': CommissionRecall.RECALL_REASON_CHOICES,
        'agent_type_choices': [
            ('agent', 'Agent'),
            ('super_agent', 'Super Agent'),
            ('master_agent', 'Master Agent'),
        ],
    }
    return render(request, 'betting/commission_recall.html', context)


@login_required
def commission_recall_export(request):
    if not (
        request.user.is_superuser
        or request.user.user_type in ['admin', 'account_user']
        or is_finance_user(request.user)
        or is_crm_user(request.user)
        or is_retail_manager(request.user)
    ):
        return HttpResponse("Not allowed.", status=403)

    from commission.models import WeeklyAgentCommission, MonthlyNetworkCommission, CommissionRecall, CommissionRecallLog

    dataset = (request.GET.get('dataset') or 'history').strip().lower()
    fmt = (request.GET.get('format') or 'csv').strip().lower()
    q = (request.GET.get('q') or '').strip()
    agent_type = (request.GET.get('agent_type') or '').strip()
    status_filter = (request.GET.get('status') or '').strip()
    reason_filter = (request.GET.get('reason') or '').strip()
    start_date = (request.GET.get('start_date') or '').strip()
    end_date = (request.GET.get('end_date') or '').strip()

    rows = []
    title = dataset
    today = timezone.localdate()
    if not start_date:
        start_date = (today - timedelta(days=30)).isoformat()
    if not end_date:
        end_date = today.isoformat()

    if dataset == 'queue':
        weekly_qs = WeeklyAgentCommission.objects.select_related('agent', 'period', 'paid_by').filter(amount_paid__gt=0)
        monthly_qs = MonthlyNetworkCommission.objects.select_related('user', 'period', 'paid_by').filter(amount_paid__gt=0)

        if status_filter:
            weekly_qs = weekly_qs.filter(status=status_filter)
            monthly_qs = monthly_qs.filter(status=status_filter)
        else:
            weekly_qs = weekly_qs.filter(status__in=['paid', 'partially_paid'])
            monthly_qs = monthly_qs.filter(status__in=['paid', 'partially_paid'])

        if agent_type:
            weekly_qs = weekly_qs.filter(agent__user_type=agent_type)
            monthly_qs = monthly_qs.filter(user__user_type=agent_type)

        if start_date:
            weekly_qs = weekly_qs.filter(paid_at__date__gte=start_date)
            monthly_qs = monthly_qs.filter(paid_at__date__gte=start_date)
        if end_date:
            weekly_qs = weekly_qs.filter(paid_at__date__lte=end_date)
            monthly_qs = monthly_qs.filter(paid_at__date__lte=end_date)

        if q:
            weekly_qs = weekly_qs.filter(Q(agent__email__icontains=q) | Q(agent__username__icontains=q))
            monthly_qs = monthly_qs.filter(Q(user__email__icontains=q) | Q(user__username__icontains=q))

        for wc in weekly_qs.order_by('-paid_at')[:200000]:
            rows.append({
                'type': 'weekly',
                'agent': wc.agent.email or wc.agent.username,
                'agent_type': wc.agent.user_type,
                'period_start': wc.period.start_date.isoformat(),
                'period_end': wc.period.end_date.isoformat(),
                'total_amount': str(wc.commission_total_amount or ''),
                'amount_paid': str(wc.amount_paid or ''),
                'paid_at': wc.paid_at.isoformat(sep=' ', timespec='seconds') if wc.paid_at else '',
                'paid_by': getattr(getattr(wc, 'paid_by', None), 'email', '') or '',
                'status': wc.status,
            })

        for mc in monthly_qs.order_by('-paid_at')[:200000]:
            rows.append({
                'type': 'monthly',
                'agent': mc.user.email or mc.user.username,
                'agent_type': mc.user.user_type,
                'period_start': mc.period.start_date.isoformat(),
                'period_end': mc.period.end_date.isoformat(),
                'total_amount': str(mc.commission_amount or ''),
                'amount_paid': str(mc.amount_paid or ''),
                'paid_at': mc.paid_at.isoformat(sep=' ', timespec='seconds') if mc.paid_at else '',
                'paid_by': getattr(getattr(mc, 'paid_by', None), 'email', '') or '',
                'status': mc.status,
            })

        title = 'paid_queue'

    elif dataset == 'history':
        logs_qs = CommissionRecallLog.objects.select_related('agent', 'recalled_by').all()
        if reason_filter:
            logs_qs = logs_qs.filter(recall_reason=reason_filter)
        if start_date:
            logs_qs = logs_qs.filter(recall_date__gte=start_date)
        if end_date:
            logs_qs = logs_qs.filter(recall_date__lte=end_date)
        if q:
            logs_qs = logs_qs.filter(Q(agent__email__icontains=q) | Q(agent__username__icontains=q) | Q(recalled_by__email__icontains=q))
        for log in logs_qs.order_by('-created_at')[:200000]:
            rows.append({
                'date': log.recall_date.isoformat(),
                'time': log.recall_time.strftime('%H:%M:%S'),
                'agent': log.agent.email or log.agent.username,
                'amount_recalled': str(log.amount_recalled or ''),
                'reason': log.recall_reason,
                'old_status': log.old_status,
                'new_status': log.new_status,
                'recalled_by': getattr(getattr(log, 'recalled_by', None), 'email', '') or '',
                'ip': log.ip_address or '',
            })
        title = 'recall_history'

    elif dataset == 'requests':
        qs = CommissionRecall.objects.select_related('beneficiary', 'requested_by', 'period').filter(status='pending_approval')
        for rr in qs.order_by('-created_at')[:200000]:
            rows.append({
                'created_at': rr.created_at.isoformat(sep=' ', timespec='seconds'),
                'agent': rr.beneficiary.email or rr.beneficiary.username,
                'period_start': rr.period.start_date.isoformat(),
                'period_end': rr.period.end_date.isoformat(),
                'amount': str(rr.amount_requested or ''),
                'reason': rr.recall_reason,
                'requested_by': getattr(getattr(rr, 'requested_by', None), 'email', '') or '',
            })
        title = 'recall_requests'

    else:
        return HttpResponse("Unknown dataset.", status=400)

    filename_base = f"commission_recall_{title}_{timezone.now().strftime('%Y%m%d_%H%M%S')}"

    if fmt == 'csv':
        import csv
        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = f'attachment; filename=\"{filename_base}.csv\"'
        fieldnames = list(rows[0].keys()) if rows else []
        writer = csv.DictWriter(response, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            writer.writerow(r)
        return response

    if fmt == 'xlsx':
        import io
        import pandas as pd
        output = io.BytesIO()
        df = pd.DataFrame(rows)
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            df.to_excel(writer, index=False, sheet_name=title[:31] or 'Sheet1')
        output.seek(0)
        response = HttpResponse(output.getvalue(), content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        response['Content-Disposition'] = f'attachment; filename=\"{filename_base}.xlsx\"'
        return response

    if fmt == 'pdf':
        from weasyprint import HTML
        def esc(s):
            return (str(s or "")
                    .replace("&", "&amp;")
                    .replace("<", "&lt;")
                    .replace(">", "&gt;")
                    .replace('"', "&quot;")
                    .replace("'", "&#39;"))
        cols = list(rows[0].keys()) if rows else []
        head = ''.join([f"<th>{esc(c)}</th>" for c in cols])
        body = ''.join([
            "<tr>" + ''.join([f"<td>{esc(r.get(c))}</td>" for c in cols]) + "</tr>"
            for r in rows[:2000]
        ])
        html = f"""
        <html>
          <head>
            <meta charset="utf-8" />
            <style>
              body {{ font-family: Arial, sans-serif; font-size: 11px; }}
              h2 {{ margin: 0 0 8px 0; }}
              .meta {{ color: #666; margin-bottom: 12px; }}
              table {{ width: 100%; border-collapse: collapse; }}
              th, td {{ border: 1px solid #ddd; padding: 6px; vertical-align: top; }}
              th {{ background: #f3f5f7; text-align: left; }}
              tr:nth-child(even) td {{ background: #fafafa; }}
            </style>
          </head>
          <body>
            <h2>Commission Recall: {esc(title)}</h2>
            <div class="meta">Range: {esc(start_date)} → {esc(end_date)}</div>
            <table>
              <thead><tr>{head}</tr></thead>
              <tbody>{body}</tbody>
            </table>
          </body>
        </html>
        """
        pdf_bytes = HTML(string=html, base_url=request.build_absolute_uri('/')).write_pdf()
        response = HttpResponse(pdf_bytes, content_type='application/pdf')
        response['Content-Disposition'] = f'attachment; filename=\"{filename_base}.pdf\"'
        return response

    return HttpResponse("Unknown format.", status=400)


@login_required
@user_passes_test(is_finance_user)
def finance_dashboard(request):
    tab_raw = (request.POST.get('tab') if request.method == 'POST' else request.GET.get('tab')) or 'overview'
    active_tab = (tab_raw or 'overview').strip() or 'overview'
    start_date_str = (request.GET.get('start_date') or '').strip()
    end_date_str = (request.GET.get('end_date') or '').strip()
    q = (request.GET.get('q') or '').strip()
    bet_q = (request.GET.get('bet_q') or '').strip()
    bet_status = (request.GET.get('bet_status') or '').strip()
    bet_agent_id = (request.GET.get('bet_agent') or '').strip()
    tx_type = (request.GET.get('tx_type') or '').strip()
    tx_status = (request.GET.get('tx_status') or '').strip()
    tx_gateway = (request.GET.get('tx_gateway') or '').strip()
    amount_min = (request.GET.get('amount_min') or '').strip()
    amount_max = (request.GET.get('amount_max') or '').strip()
    audit_q = (request.GET.get('audit_q') or '').strip()
    audit_action_type = (request.GET.get('audit_action_type') or '').strip()
    settlement_status = (request.GET.get('settlement_status') or '').strip()
    settlement_id = (request.GET.get('settlement_id') or '').strip()
    ledger_q = (request.GET.get('ledger_q') or '').strip()
    ledger_account = (request.GET.get('ledger_account') or '').strip()
    journal_id = (request.GET.get('journal_id') or '').strip()
    gateway_filter = (request.GET.get('gateway') or '').strip()
    pin_q = (request.GET.get('pin_q') or '').strip()
    pin_success = (request.GET.get('pin_success') or '').strip()
    recon_filter = (request.GET.get('recon') or '').strip()
    fraud_filter = (request.GET.get('fraud') or '').strip()
    selected_commission_period_id = (request.GET.get('commission_period') or '').strip()
    commission_agent_q = (request.GET.get('commission_agent') or '').strip()

    start_dt = None
    end_dt = None
    try:
        if start_date_str:
            start_dt = timezone.make_aware(datetime.strptime(start_date_str, "%Y-%m-%d"))
    except Exception:
        start_dt = None
    try:
        if end_date_str:
            end_raw = datetime.strptime(end_date_str, "%Y-%m-%d")
            end_dt = timezone.make_aware(datetime.combine(end_raw.date(), datetime.max.time()))
    except Exception:
        end_dt = None

    today = timezone.localdate()
    metrics_start_date = (start_dt.date() if start_dt else (today - timedelta(days=30)))
    metrics_end_date = (end_dt.date() if end_dt else today)
    metrics_start_dt = timezone.make_aware(datetime.combine(metrics_start_date, datetime.min.time()))
    metrics_end_dt = timezone.make_aware(datetime.combine(metrics_end_date, datetime.max.time()))
    metrics_label = f"{metrics_start_date.isoformat()} → {metrics_end_date.isoformat()}"

    if request.method == 'POST':
        if request.POST.get('withdrawal_action') == '1':
            if not finance_can_approve_withdrawals(request.user):
                messages.error(request, 'Not allowed.')
                return redirect(f"{reverse('betting:finance_dashboard')}?tab=withdrawals")
            wid = (request.POST.get('withdrawal_id') or '').strip()
            action = (request.POST.get('action') or '').strip()
            reason = (request.POST.get('reason') or '').strip()
            w = get_object_or_404(UserWithdrawal, id=wid)
            if w.status != 'pending':
                messages.warning(request, 'Withdrawal is not pending.')
                return redirect(f"{reverse('betting:finance_dashboard')}?tab=withdrawals")
            if action == 'approve':
                w.status = 'approved'
                w.approved_rejected_time = timezone.now()
                w.approved_rejected_by = request.user
                w.admin_notes = reason or ''
                w.save(update_fields=['status', 'approved_rejected_time', 'approved_rejected_by', 'admin_notes'])
                FinanceAuditLog.objects.create(
                    actor=request.user,
                    action_type='WITHDRAWAL_APPROVED',
                    target_user=w.user,
                    withdrawal=w,
                    ip_address=get_client_ip(request),
                    reason=reason,
                    data={'amount': str(w.amount)},
                )
                messages.success(request, 'Withdrawal approved.')
            elif action == 'reject':
                w._skip_signal_refund = True
                w.status = 'rejected'
                w.approved_rejected_time = timezone.now()
                w.approved_rejected_by = request.user
                w.admin_notes = reason or ''
                w.save(update_fields=['status', 'approved_rejected_time', 'approved_rejected_by', 'admin_notes'])
                with db_transaction.atomic():
                    wallet = Wallet.objects.select_for_update().get(user=w.user)
                    refund_tx = Transaction.objects.create(
                        user=w.user,
                        initiating_user=request.user,
                        target_user=w.user,
                        transaction_type='withdrawal_refund',
                        amount=w.amount,
                        is_successful=True,
                        status='completed',
                        description=f"Refund for rejected withdrawal request {w.id}",
                        related_withdrawal_request=w,
                        timestamp=timezone.now(),
                    )
                    wallet.apply_delta(
                        amount=w.amount,
                        actor=request.user,
                        transaction_obj=refund_tx,
                        reference=str(w.id),
                        reason=refund_tx.description,
                        metadata={"withdrawal_id": w.id, "source": "finance_reject"},
                    )
                FinanceAuditLog.objects.create(
                    actor=request.user,
                    action_type='WITHDRAWAL_REJECTED',
                    target_user=w.user,
                    withdrawal=w,
                    ip_address=get_client_ip(request),
                    reason=reason,
                    data={'amount': str(w.amount)},
                )
                messages.success(request, 'Withdrawal rejected and refunded.')
            return redirect(f"{reverse('betting:finance_dashboard')}?tab=withdrawals")

        if request.POST.get('bulk_withdrawal_approve') == '1':
            if not finance_can_approve_withdrawals(request.user):
                messages.error(request, 'Not allowed.')
                return redirect(f"{reverse('betting:finance_dashboard')}?tab=withdrawals")
            ids = request.POST.getlist('selected_withdrawals')
            updated = 0
            for wid in ids:
                w = UserWithdrawal.objects.filter(id=wid, status='pending').first()
                if not w:
                    continue
                w.status = 'approved'
                w.approved_rejected_time = timezone.now()
                w.approved_rejected_by = request.user
                w.save(update_fields=['status', 'approved_rejected_time', 'approved_rejected_by'])
                FinanceAuditLog.objects.create(
                    actor=request.user,
                    action_type='WITHDRAWAL_APPROVED',
                    target_user=w.user,
                    withdrawal=w,
                    ip_address=get_client_ip(request),
                    data={'amount': str(w.amount), 'bulk': True},
                )
                updated += 1
            messages.success(request, f"Approved {updated} withdrawals.")
            return redirect(f"{reverse('betting:finance_dashboard')}?tab=withdrawals")

        if request.POST.get('withdrawal_complete') == '1':
            if not finance_can_approve_withdrawals(request.user):
                messages.error(request, 'Not allowed.')
                return redirect(f"{reverse('betting:finance_dashboard')}?tab=withdrawals")
            wid = (request.POST.get('withdrawal_id') or '').strip()
            w = get_object_or_404(UserWithdrawal, id=wid)
            if w.status != 'approved':
                messages.error(request, 'Only approved withdrawals can be marked completed.')
                return redirect(f"{reverse('betting:finance_dashboard')}?tab=withdrawals")
            w.status = 'completed'
            if not w.approved_rejected_time:
                w.approved_rejected_time = timezone.now()
            if not w.approved_rejected_by:
                w.approved_rejected_by = request.user
            w.save(update_fields=['status', 'approved_rejected_time', 'approved_rejected_by'])
            FinanceAuditLog.objects.create(
                actor=request.user,
                action_type='WITHDRAWAL_COMPLETED',
                target_user=w.user,
                withdrawal=w,
                ip_address=get_client_ip(request),
                data={'amount': str(w.amount)},
            )
            messages.success(request, 'Withdrawal marked completed.')
            return redirect(f"{reverse('betting:finance_dashboard')}?tab=withdrawals")

        if request.POST.get('tx_review') == '1':
            if not finance_can_verify_transactions(request.user):
                messages.error(request, 'Not allowed.')
                return redirect(f"{reverse('betting:finance_dashboard')}?tab=transactions")
            tx_id = (request.POST.get('tx_id') or '').strip()
            status = (request.POST.get('review_status') or '').strip()
            notes = (request.POST.get('notes') or '').strip()
            if status not in ['verified', 'flagged', 'rejected']:
                messages.error(request, 'Invalid review status.')
                return redirect(f"{reverse('betting:finance_dashboard')}?tab=transactions")
            tx = get_object_or_404(Transaction, id=tx_id)
            FinanceTransactionReview.objects.create(transaction=tx, reviewer=request.user, status=status, notes=notes or '')
            FinanceAuditLog.objects.create(
                actor=request.user,
                action_type='TX_VERIFIED',
                target_user=tx.user,
                transaction=tx,
                ip_address=get_client_ip(request),
                reason=f"{status}: {notes}".strip(': ').strip(),
                data={'status': status},
            )
            messages.success(request, 'Transaction review saved.')
            return redirect(f"{reverse('betting:finance_dashboard')}?tab=transactions")

        if request.POST.get('deposit_complete') == '1':
            if not (finance_can_verify_transactions(request.user) and finance_can_adjust_wallets(request.user)):
                messages.error(request, 'Not allowed.')
                return redirect(f"{reverse('betting:finance_dashboard')}?tab=reconciliation")
            tx_id = (request.POST.get('tx_id') or '').strip()
            reason = (request.POST.get('reason') or '').strip()
            tx = get_object_or_404(Transaction, id=tx_id, transaction_type='deposit')
            if tx.status == 'completed' and tx.is_successful:
                messages.info(request, 'Deposit already completed.')
                return redirect(f"{reverse('betting:finance_dashboard')}?tab=reconciliation")
            with db_transaction.atomic():
                repayment_result = apply_repayment_and_credit_wallet(
                    user=tx.user,
                    amount=tx.amount,
                    source="gateway_deposit",
                    actor=request.user,
                    transaction_obj=tx,
                    reference=(tx.external_reference or tx.paystack_reference or str(tx.id)),
                    reason="Manual deposit completion",
                    metadata={"source": "finance_manual_complete", "reason": reason},
                )
                tx.status = 'completed'
                tx.is_successful = True
                tx.description = (reason or tx.description or '').strip()
                tx.timestamp = timezone.now()
                tx.save(update_fields=['status', 'is_successful', 'description', 'timestamp'])
                PaymentGatewayEventLog.objects.create(
                    gateway=getattr(tx, 'payment_gateway', '') or 'paystack',
                    event_type='reconcile',
                    reference=(tx.external_reference or tx.paystack_reference or str(tx.id)),
                    transaction=tx,
                    user=tx.user,
                    amount=tx.amount,
                    success=True,
                    message='Manual completion',
                    payload={'reason': reason},
                )
                FinanceAuditLog.objects.create(
                    actor=request.user,
                    action_type='DEPOSIT_MANUAL_COMPLETED',
                    target_user=tx.user,
                    transaction=tx,
                    ip_address=get_client_ip(request),
                    reason=reason,
                    data={'amount': str(tx.amount)},
                )
                cash = LedgerAccount.objects.filter(code='CASH_OPS', is_active=True).first()
                liab = LedgerAccount.objects.filter(code='LIAB_WALLET', is_active=True).first()
                if cash and liab:
                    je = JournalEntry.objects.create(
                        entry_date=timezone.localdate(),
                        memo=f"Manual deposit completion ({tx.external_reference or tx.id})",
                        created_by=request.user,
                        related_transaction=tx,
                    )
                    JournalLine.objects.create(entry=je, account=cash, debit=tx.amount, credit=Decimal('0.00'), related_user=tx.user)
                    JournalLine.objects.create(entry=je, account=liab, debit=Decimal('0.00'), credit=tx.amount, related_user=tx.user)
            repaid_amount = repayment_result.get("repaid_amount") or Decimal("0.00")
            wallet_credit_amount = repayment_result.get("wallet_credit_amount") or Decimal("0.00")
            pending_credit_amount = repayment_result.get("pending_credit_amount") or Decimal("0.00")
            messages.success(
                request,
                f"Deposit completed. Loan repayment: ₦{repaid_amount}. "
                f"Wallet credit: ₦{wallet_credit_amount}. Reserved new credit: ₦{pending_credit_amount}."
            )
            return redirect(f"{reverse('betting:finance_dashboard')}?tab=reconciliation")

        if request.POST.get('reverse_tx') == '1':
            if not finance_can_reverse_transactions(request.user):
                messages.error(request, 'Not allowed.')
                return redirect(f"{reverse('betting:finance_dashboard')}?tab=transactions")
            tx_id = (request.POST.get('tx_id') or '').strip()
            reason = (request.POST.get('reason') or '').strip()
            tx = get_object_or_404(Transaction, id=tx_id)
            if tx.status != 'completed' or not tx.is_successful:
                messages.error(request, 'Only completed successful transactions can be reversed.')
                return redirect(f"{reverse('betting:finance_dashboard')}?tab=transactions")
            if tx.transaction_type not in ['deposit', 'withdrawal', 'wallet_transfer_in', 'wallet_transfer_out', 'bonus', 'commission_payout', 'withdrawal_refund']:
                messages.error(request, 'This transaction type cannot be reversed from finance dashboard.')
                return redirect(f"{reverse('betting:finance_dashboard')}?tab=transactions")
            with db_transaction.atomic():
                wallet = Wallet.objects.select_for_update().get(user=tx.user)
                if tx.transaction_type in ['deposit', 'wallet_transfer_in', 'bonus', 'commission_payout', 'withdrawal_refund']:
                    if wallet.balance < tx.amount:
                        messages.error(request, 'Insufficient wallet balance to reverse this credit.')
                        return redirect(f"{reverse('betting:finance_dashboard')}?tab=transactions")
                    reversal_tx = Transaction.objects.create(
                        user=tx.user,
                        initiating_user=request.user,
                        target_user=tx.user,
                        transaction_type='wallet_transfer_out',
                        amount=tx.amount,
                        is_successful=True,
                        status='completed',
                        description=f"Reversal of {tx.transaction_type} {tx.id}. {reason}".strip(),
                        timestamp=timezone.now(),
                    )
                    wallet.apply_delta(
                        amount=-tx.amount,
                        actor=request.user,
                        transaction_obj=reversal_tx,
                        reference=str(tx.id),
                        reason=reversal_tx.description,
                        metadata={"reversed_tx_id": str(tx.id), "source": "finance_reverse"},
                    )
                elif tx.transaction_type in ['withdrawal', 'wallet_transfer_out']:
                    reversal_tx = Transaction.objects.create(
                        user=tx.user,
                        initiating_user=request.user,
                        target_user=tx.user,
                        transaction_type='wallet_transfer_in',
                        amount=tx.amount,
                        is_successful=True,
                        status='completed',
                        description=f"Reversal of {tx.transaction_type} {tx.id}. {reason}".strip(),
                        timestamp=timezone.now(),
                    )
                    wallet.apply_delta(
                        amount=tx.amount,
                        actor=request.user,
                        transaction_obj=reversal_tx,
                        reference=str(tx.id),
                        reason=reversal_tx.description,
                        metadata={"reversed_tx_id": str(tx.id), "source": "finance_reverse"},
                    )
                tx.status = 'reversed'
                tx.is_successful = False
                tx.save(update_fields=['status', 'is_successful'])
            FinanceAuditLog.objects.create(
                actor=request.user,
                action_type='TX_REVERSED',
                target_user=tx.user,
                transaction=tx,
                ip_address=get_client_ip(request),
                reason=reason,
                data={'tx_type': tx.transaction_type, 'amount': str(tx.amount)},
            )
            messages.success(request, 'Transaction reversed.')
            return redirect(f"{reverse('betting:finance_dashboard')}?tab=transactions")

        if request.POST.get('wallet_adjust') == '1':
            if not finance_can_adjust_wallets(request.user):
                messages.error(request, 'Not allowed.')
                return redirect(f"{reverse('betting:finance_dashboard')}?tab=wallets")
            user_ident = (request.POST.get('user_ident') or '').strip()
            direction = (request.POST.get('direction') or '').strip()
            reason = (request.POST.get('reason') or '').strip()
            amount_raw = (request.POST.get('amount') or '').strip()
            if direction not in ['credit', 'debit']:
                messages.error(request, 'Invalid direction.')
                return redirect(f"{reverse('betting:finance_dashboard')}?tab=wallets")
            try:
                amount = Decimal(amount_raw)
                if amount <= 0:
                    raise InvalidOperation()
            except Exception:
                messages.error(request, 'Invalid amount.')
                return redirect(f"{reverse('betting:finance_dashboard')}?tab=wallets")

            target_user = None
            resolution_error = ''
            if user_ident.isdigit():
                target_user = User.objects.filter(id=int(user_ident)).first()
            if not target_user:
                target_user, resolution_error = resolve_user_from_identifier(user_ident)
            if not target_user:
                messages.error(request, resolution_error or 'User not found.')
                return redirect(f"{reverse('betting:finance_dashboard')}?tab=wallets")

            with db_transaction.atomic():
                wallet = Wallet.objects.select_for_update().get(user=target_user)
                if direction == 'debit' and wallet.balance < amount:
                    messages.error(request, 'Insufficient wallet balance.')
                    return redirect(f"{reverse('betting:finance_dashboard')}?tab=wallets")
                if direction == 'credit':
                    tx_type = 'wallet_transfer_in'
                else:
                    tx_type = 'wallet_transfer_out'
                tx = Transaction.objects.create(
                    user=target_user,
                    initiating_user=request.user,
                    target_user=target_user,
                    transaction_type=tx_type,
                    amount=amount,
                    is_successful=True,
                    status='completed',
                    description=(f"Manual wallet {direction}: {reason}".strip() if reason else f"Manual wallet {direction}"),
                    timestamp=timezone.now(),
                )
                wallet.apply_delta(
                    amount=(amount if direction == "credit" else -amount),
                    actor=request.user,
                    transaction_obj=tx,
                    reference=str(tx.id),
                    reason=tx.description,
                    metadata={"direction": direction, "source": "finance_wallet_adjust"},
                )
                FinanceAuditLog.objects.create(
                    actor=request.user,
                    action_type='WALLET_ADJUSTED',
                    target_user=target_user,
                    transaction=tx,
                    ip_address=get_client_ip(request),
                    reason=reason,
                    data={'direction': direction, 'amount': str(amount)},
                )
                suspense = LedgerAccount.objects.filter(code='EQUITY_SUSPENSE', is_active=True).first()
                liab = LedgerAccount.objects.filter(code='LIAB_WALLET', is_active=True).first()
                if suspense and liab:
                    je = JournalEntry.objects.create(
                        entry_date=timezone.localdate(),
                        memo=f"Manual wallet {direction} ({target_user.email or target_user.username})",
                        created_by=request.user,
                        related_transaction=tx,
                    )
                    if direction == 'credit':
                        JournalLine.objects.create(entry=je, account=suspense, debit=amount, credit=Decimal('0.00'), related_user=target_user)
                        JournalLine.objects.create(entry=je, account=liab, debit=Decimal('0.00'), credit=amount, related_user=target_user)
                    else:
                        JournalLine.objects.create(entry=je, account=liab, debit=amount, credit=Decimal('0.00'), related_user=target_user)
                        JournalLine.objects.create(entry=je, account=suspense, debit=Decimal('0.00'), credit=amount, related_user=target_user)
            messages.success(request, 'Wallet adjusted.')
            return redirect(f"{reverse('betting:finance_dashboard')}?tab=wallets")

        if request.POST.get('create_settlement_batch') == '1':
            if not finance_can_manage_settlements(request.user):
                messages.error(request, 'Not allowed.')
                return redirect(f"{reverse('betting:finance_dashboard')}?tab=settlements")
            stype = (request.POST.get('settlement_type') or 'mixed_commission').strip()
            if stype not in ['weekly_commission', 'network_commission', 'mixed_commission', 'manual']:
                stype = 'mixed_commission'
            batch = FinanceSettlementBatch.objects.create(
                settlement_type=stype,
                period_start=metrics_start_date,
                period_end=metrics_end_date,
                status='draft',
                created_by=request.user,
            )
            total = Decimal('0.00')
            items = 0
            if stype in ['weekly_commission', 'mixed_commission']:
                wqs = WeeklyAgentCommission.objects.filter(
                    status__in=['pending', 'approved', 'partially_paid'],
                    period__start_date__lte=metrics_end_date,
                    period__end_date__gte=metrics_start_date,
                ).select_related('agent', 'period')
                for wc in wqs:
                    amt = (wc.commission_total_amount or Decimal('0.00')) - (wc.amount_paid or Decimal('0.00'))
                    if amt <= 0:
                        continue
                    FinanceSettlementItem.objects.create(batch=batch, beneficiary=wc.agent, amount=amt, weekly_commission=wc)
                    total += amt
                    items += 1
            if stype in ['network_commission', 'mixed_commission']:
                mqs = MonthlyNetworkCommission.objects.filter(
                    status__in=['pending', 'approved', 'partially_paid'],
                    period__start_date__lte=metrics_end_date,
                    period__end_date__gte=metrics_start_date,
                ).select_related('user', 'period')
                for mc in mqs:
                    amt = (mc.commission_amount or Decimal('0.00')) - (mc.amount_paid or Decimal('0.00'))
                    if amt <= 0:
                        continue
                    FinanceSettlementItem.objects.create(batch=batch, beneficiary=mc.user, amount=amt, monthly_commission=mc)
                    total += amt
                    items += 1
            FinanceAuditLog.objects.create(
                actor=request.user,
                action_type='SETTLEMENT_CREATED',
                ip_address=get_client_ip(request),
                reason=f"{stype}",
                data={'batch_id': str(batch.id), 'items': items, 'total': str(total), 'range': [metrics_start_date.isoformat(), metrics_end_date.isoformat()]},
            )
            messages.success(request, f"Settlement batch created ({items} items).")
            return redirect(f"{reverse('betting:finance_dashboard')}?tab=settlements&settlement_id={batch.id}")

        if request.POST.get('settlement_action') == '1':
            if not finance_can_manage_settlements(request.user):
                messages.error(request, 'Not allowed.')
                return redirect(f"{reverse('betting:finance_dashboard')}?tab=settlements")
            sid = (request.POST.get('settlement_id') or '').strip()
            action = (request.POST.get('action') or '').strip()
            batch = get_object_or_404(FinanceSettlementBatch, id=sid)
            if action == 'approve':
                if batch.status != 'draft':
                    messages.error(request, 'Only draft batches can be approved.')
                else:
                    batch.status = 'approved'
                    batch.approved_by = request.user
                    batch.approved_at = timezone.now()
                    batch.save(update_fields=['status', 'approved_by', 'approved_at'])
                    FinanceAuditLog.objects.create(
                        actor=request.user,
                        action_type='SETTLEMENT_APPROVED',
                        ip_address=get_client_ip(request),
                        reason=str(batch.id),
                        data={'batch_id': str(batch.id)},
                    )
                    messages.success(request, 'Settlement batch approved.')
            elif action == 'pay':
                if batch.status != 'approved':
                    messages.error(request, 'Only approved batches can be paid.')
                else:
                    paid_total = Decimal('0.00')
                    failed = 0
                    for item in batch.items.select_related('beneficiary', 'weekly_commission', 'monthly_commission').filter(status='pending'):
                        try:
                            with db_transaction.atomic():
                                wallet = Wallet.objects.select_for_update().get(user=item.beneficiary)
                                tx = Transaction.objects.create(
                                    user=item.beneficiary,
                                    initiating_user=request.user,
                                    target_user=item.beneficiary,
                                    transaction_type='commission_payout',
                                    amount=item.amount,
                                    is_successful=True,
                                    status='completed',
                                    description=f"Commission settlement {batch.id}",
                                    timestamp=timezone.now(),
                                )
                                wallet.apply_delta(
                                    amount=item.amount,
                                    actor=request.user,
                                    transaction_obj=tx,
                                    reference=str(batch.id),
                                    reason=tx.description,
                                    metadata={"settlement_batch_id": str(batch.id), "settlement_item_id": item.id},
                                )
                                if item.weekly_commission_id:
                                    wc = WeeklyAgentCommission.objects.select_for_update().get(id=item.weekly_commission_id)
                                    wc.amount_paid = (wc.amount_paid or Decimal('0.00')) + (item.amount or Decimal('0.00'))
                                    total_due = wc.commission_total_amount or Decimal('0.00')
                                    if wc.amount_paid >= total_due:
                                        wc.amount_paid = total_due
                                        wc.status = 'paid'
                                    else:
                                        wc.status = 'partially_paid'
                                    wc.paid_at = timezone.now()
                                    wc.paid_by = request.user
                                    wc.paid_source = 'system'
                                    wc.paid_from_user = request.user
                                    wc.save(update_fields=['amount_paid', 'status', 'paid_at', 'paid_by', 'paid_source', 'paid_from_user'])
                                if item.monthly_commission_id:
                                    mc = MonthlyNetworkCommission.objects.select_for_update().get(id=item.monthly_commission_id)
                                    mc.amount_paid = (mc.amount_paid or Decimal('0.00')) + (item.amount or Decimal('0.00'))
                                    total_due = mc.commission_amount or Decimal('0.00')
                                    if mc.amount_paid >= total_due:
                                        mc.amount_paid = total_due
                                        mc.status = 'paid'
                                    else:
                                        mc.status = 'partially_paid'
                                    mc.paid_at = timezone.now()
                                    mc.paid_by = request.user
                                    mc.paid_source = 'system'
                                    mc.paid_from_user = request.user
                                    mc.save(update_fields=['amount_paid', 'status', 'paid_at', 'paid_by', 'paid_source', 'paid_from_user'])
                                item.status = 'paid'
                                item.paid_at = timezone.now()
                                item.error_message = ''
                                item.save(update_fields=['status', 'paid_at', 'error_message'])
                                paid_total += item.amount
                        except Exception as e:
                            failed += 1
                            item.status = 'failed'
                            item.error_message = str(e)[:255]
                            item.save(update_fields=['status', 'error_message'])
                    comm_exp = LedgerAccount.objects.filter(code='EXP_COMM', is_active=True).first()
                    liab = LedgerAccount.objects.filter(code='LIAB_WALLET', is_active=True).first()
                    if paid_total > 0 and comm_exp and liab:
                        je = JournalEntry.objects.create(
                            entry_date=timezone.localdate(),
                            memo=f"Commission settlement batch {batch.id}",
                            created_by=request.user,
                        )
                        JournalLine.objects.create(entry=je, account=comm_exp, debit=paid_total, credit=Decimal('0.00'))
                        JournalLine.objects.create(entry=je, account=liab, debit=Decimal('0.00'), credit=paid_total)
                    if failed == 0:
                        batch.status = 'paid'
                        batch.paid_at = timezone.now()
                        batch.save(update_fields=['status', 'paid_at'])
                    FinanceAuditLog.objects.create(
                        actor=request.user,
                        action_type='SETTLEMENT_PAID',
                        ip_address=get_client_ip(request),
                        reason=str(batch.id),
                        data={'batch_id': str(batch.id), 'paid_total': str(paid_total), 'failed': failed},
                    )
                    messages.success(request, f"Settlement processed. Paid ₦{paid_total:.2f}. Failed: {failed}.")
            return redirect(f"{reverse('betting:finance_dashboard')}?tab=settlements&settlement_id={batch.id}")

        if request.POST.get('create_journal') == '1':
            if not finance_can_manage_ledger(request.user):
                messages.error(request, 'Not allowed.')
                return redirect(f"{reverse('betting:finance_dashboard')}?tab=ledger")
            entry_date_str = (request.POST.get('entry_date') or '').strip()
            memo = (request.POST.get('memo') or '').strip()
            a1 = (request.POST.get('account1') or '').strip()
            a2 = (request.POST.get('account2') or '').strip()
            d1 = (request.POST.get('debit1') or '').strip()
            c1 = (request.POST.get('credit1') or '').strip()
            d2 = (request.POST.get('debit2') or '').strip()
            c2 = (request.POST.get('credit2') or '').strip()
            try:
                entry_date = datetime.strptime(entry_date_str, "%Y-%m-%d").date()
            except Exception:
                messages.error(request, 'Invalid entry date.')
                return redirect(f"{reverse('betting:finance_dashboard')}?tab=ledger")
            try:
                debit1 = Decimal(d1 or '0')
                credit1 = Decimal(c1 or '0')
                debit2 = Decimal(d2 or '0')
                credit2 = Decimal(c2 or '0')
            except Exception:
                messages.error(request, 'Invalid debit/credit.')
                return redirect(f"{reverse('betting:finance_dashboard')}?tab=ledger")
            if (debit1 + debit2) <= 0 or (credit1 + credit2) <= 0 or (debit1 + debit2) != (credit1 + credit2):
                messages.error(request, 'Journal entry must be balanced (total debits = total credits).')
                return redirect(f"{reverse('betting:finance_dashboard')}?tab=ledger")
            acc1 = LedgerAccount.objects.filter(code=a1, is_active=True).first()
            acc2 = LedgerAccount.objects.filter(code=a2, is_active=True).first()
            if not acc1 or not acc2:
                messages.error(request, 'Invalid account code.')
                return redirect(f"{reverse('betting:finance_dashboard')}?tab=ledger")
            je = JournalEntry.objects.create(entry_date=entry_date, memo=memo, created_by=request.user)
            JournalLine.objects.create(entry=je, account=acc1, debit=debit1, credit=credit1)
            JournalLine.objects.create(entry=je, account=acc2, debit=debit2, credit=credit2)
            FinanceAuditLog.objects.create(
                actor=request.user,
                action_type='JOURNAL_CREATED',
                ip_address=get_client_ip(request),
                reason=memo,
                data={'journal_id': je.id},
            )
            messages.success(request, 'Journal entry created.')
            return redirect(f"{reverse('betting:finance_dashboard')}?tab=ledger&journal_id={je.id}")

        if request.POST.get('create_scheduled_report') == '1':
            if not finance_can_export(request.user):
                messages.error(request, 'Not allowed.')
                return redirect(f"{reverse('betting:finance_dashboard')}?tab=reports")
            name = (request.POST.get('name') or '').strip() or 'Scheduled Report'
            dataset = (request.POST.get('dataset') or '').strip()
            report_format = (request.POST.get('report_format') or 'csv').strip()
            frequency = (request.POST.get('frequency') or 'daily').strip()
            recipients = (request.POST.get('recipients') or '').strip()
            if dataset not in dict(getattr(ScheduledFinanceReport, 'DATASET_CHOICES', ())).keys():
                messages.error(request, 'Invalid dataset.')
                return redirect(f"{reverse('betting:finance_dashboard')}?tab=reports")
            if report_format not in ['csv', 'xlsx', 'pdf']:
                report_format = 'csv'
            if frequency not in ['daily', 'weekly', 'monthly']:
                frequency = 'daily'
            r = ScheduledFinanceReport.objects.create(
                name=name,
                dataset=dataset,
                report_format=report_format,
                frequency=frequency,
                recipients=recipients,
                is_active=True,
                next_run_at=timezone.now(),
                created_by=request.user,
            )
            messages.success(request, 'Scheduled report created.')
            return redirect(f"{reverse('betting:finance_dashboard')}?tab=reports")

        if request.POST.get('toggle_scheduled_report') == '1':
            if not finance_can_export(request.user):
                messages.error(request, 'Not allowed.')
                return redirect(f"{reverse('betting:finance_dashboard')}?tab=reports")
            rid = (request.POST.get('report_id') or '').strip()
            r = get_object_or_404(ScheduledFinanceReport, id=rid)
            r.is_active = not bool(r.is_active)
            if r.is_active and not r.next_run_at:
                r.next_run_at = timezone.now()
            r.save(update_fields=['is_active', 'next_run_at', 'updated_at'])
            messages.success(request, 'Schedule updated.')
            return redirect(f"{reverse('betting:finance_dashboard')}?tab=reports")

        if request.POST.get('run_scheduled_report') == '1':
            if not finance_can_export(request.user):
                messages.error(request, 'Not allowed.')
                return redirect(f"{reverse('betting:finance_dashboard')}?tab=reports")
            rid = (request.POST.get('report_id') or '').strip()
            r = get_object_or_404(ScheduledFinanceReport, id=rid)
            from django.core.mail import EmailMessage
            from .tasks import generate_finance_report_bytes

            def _parse_recipients(raw):
                parts = [p.strip() for p in (raw or '').replace(';', ',').split(',')]
                return [p for p in parts if p and '@' in p]

            recipients_list = _parse_recipients(r.recipients)
            if not recipients_list:
                messages.error(request, 'No recipients configured.')
                return redirect(f"{reverse('betting:finance_dashboard')}?tab=reports")

            end_date = timezone.localdate() - timedelta(days=1)
            if r.frequency == 'weekly':
                start_date = end_date - timedelta(days=6)
            elif r.frequency == 'monthly':
                first_this_month = timezone.localdate().replace(day=1)
                end_date = first_this_month - timedelta(days=1)
                start_date = end_date.replace(day=1)
            else:
                start_date = end_date
            start_dt = timezone.make_aware(datetime.combine(start_date, datetime.min.time()))
            end_dt = timezone.make_aware(datetime.combine(end_date, datetime.max.time()))

            try:
                content, title, mime, filename = generate_finance_report_bytes(r.dataset, r.report_format, start_dt, end_dt)
                subject = f"Finance Report: {r.name} ({start_date.isoformat()} → {end_date.isoformat()})"
                body = f"Attached: {title}.{r.report_format}"
                email = EmailMessage(subject=subject, body=body, to=recipients_list)
                email.attach(filename, content, mime)
                email.send(fail_silently=False)
                r.last_status = 'sent'
                r.last_error = ''
                FinanceAuditLog.objects.create(
                    actor=request.user,
                    action_type='SCHEDULED_REPORT_SENT',
                    ip_address=get_client_ip(request),
                    reason=r.name,
                    data={'dataset': r.dataset, 'format': r.report_format},
                )
                messages.success(request, 'Report sent.')
            except Exception as e:
                r.last_status = 'failed'
                r.last_error = str(e)[:255]
                messages.error(request, f"Failed to send: {e}")
            r.last_run_at = timezone.now()
            r.save(update_fields=['last_status', 'last_error', 'last_run_at', 'updated_at'])
            return redirect(f"{reverse('betting:finance_dashboard')}?tab=reports")

    deposits_today = Transaction.objects.filter(transaction_type='deposit', status='completed', is_successful=True, timestamp__date=today).aggregate(
        s=Coalesce(Sum('amount'), Value(0), output_field=DecimalField())
    )['s']
    withdrawals_today = UserWithdrawal.objects.filter(request_time__date=today).aggregate(
        s=Coalesce(Sum('amount'), Value(0), output_field=DecimalField())
    )['s']
    pending_withdrawals = UserWithdrawal.objects.filter(status='pending').order_by('-request_time')
    pending_withdrawals_count = pending_withdrawals.count()
    successful_withdrawals_today = UserWithdrawal.objects.filter(status__in=['approved', 'completed'], approved_rejected_time__date=today).count()
    failed_transactions = Transaction.objects.filter(Q(status='failed') | Q(is_successful=False)).filter(timestamp__gte=metrics_start_dt, timestamp__lte=metrics_end_dt).count()

    tickets_range = BetTicket.objects.exclude(status__in=['deleted', 'cancelled']).filter(placed_at__gte=metrics_start_dt, placed_at__lte=metrics_end_dt)
    total_stakes = tickets_range.aggregate(s=Coalesce(Sum('stake_amount'), Value(0), output_field=DecimalField()))['s']
    total_payouts = tickets_range.filter(status='won').aggregate(s=Coalesce(Sum('max_winning'), Value(0), output_field=DecimalField()))['s']
    ggr = (total_stakes or Decimal('0.00')) - (total_payouts or Decimal('0.00'))
    ngr = ggr
    profit_loss = ngr

    agent_commissions = Transaction.objects.filter(transaction_type='commission_payout', status='completed', is_successful=True, timestamp__gte=metrics_start_dt, timestamp__lte=metrics_end_dt).aggregate(
        s=Coalesce(Sum('amount'), Value(0), output_field=DecimalField())
    )['s']
    bonus_expenses = Transaction.objects.filter(transaction_type='bonus', status='completed', is_successful=True, timestamp__gte=metrics_start_dt, timestamp__lte=metrics_end_dt).aggregate(
        s=Coalesce(Sum('amount'), Value(0), output_field=DecimalField())
    )['s']
    payment_gateway_charges = PaymentGatewayEventLog.objects.filter(
        created_at__gte=metrics_start_dt,
        created_at__lte=metrics_end_dt,
    ).aggregate(s=Coalesce(Sum('fee_amount'), Value(0), output_field=DecimalField()))['s']
    current_wallet_liabilities = Wallet.objects.aggregate(s=Coalesce(Sum('balance'), Value(0), output_field=DecimalField()))['s']
    current_exposure_liabilities = BetTicket.objects.filter(status='pending').aggregate(
        s=Coalesce(Sum('max_winning'), Value(0), output_field=DecimalField())
    )['s']
    available_operational_balance = Wallet.objects.filter(user__user_type__in=['admin', 'account_user', 'finance']).aggregate(
        s=Coalesce(Sum('balance'), Value(0), output_field=DecimalField())
    )['s']

    charts_cache_key = f"finance:charts:{metrics_start_date.isoformat()}:{metrics_end_date.isoformat()}"
    charts_data = cache.get(charts_cache_key)
    if charts_data is None:
        day_count = (metrics_end_date - metrics_start_date).days + 1
        days = [metrics_start_date + timedelta(days=i) for i in range(max(0, day_count))]
        labels = [d.isoformat() for d in days]

        dep_daily = (
            Transaction.objects.filter(transaction_type='deposit', status='completed', is_successful=True, timestamp__gte=metrics_start_dt, timestamp__lte=metrics_end_dt)
            .values('timestamp__date')
            .annotate(total=Coalesce(Sum('amount'), Value(0), output_field=DecimalField()))
        )
        dep_map = {row['timestamp__date']: row['total'] for row in dep_daily}

        wdr_daily = (
            UserWithdrawal.objects.filter(request_time__gte=metrics_start_dt, request_time__lte=metrics_end_dt)
            .values('request_time__date')
            .annotate(total=Coalesce(Sum('amount'), Value(0), output_field=DecimalField()))
        )
        wdr_map = {row['request_time__date']: row['total'] for row in wdr_daily}

        ticket_daily = (
            tickets_range.values('placed_at__date')
            .annotate(
                stake=Coalesce(Sum('stake_amount'), Value(0), output_field=DecimalField()),
                payouts=Coalesce(Sum(Case(When(status='won', then='max_winning'), default=Value(0), output_field=DecimalField())), Value(0), output_field=DecimalField()),
            )
        )
        tmap = {row['placed_at__date']: row for row in ticket_daily}

        deposits_series = [float(dep_map.get(d, Decimal('0.00'))) for d in days]
        withdrawals_series = [float(wdr_map.get(d, Decimal('0.00'))) for d in days]
        stake_series = [float((tmap.get(d) or {}).get('stake', Decimal('0.00'))) for d in days]
        payouts_series = [float((tmap.get(d) or {}).get('payouts', Decimal('0.00'))) for d in days]
        profit_series = [float(Decimal(str(stake_series[i])) - Decimal(str(payouts_series[i]))) for i in range(len(days))]

        charts_data = {
            'labels': labels,
            'deposits': deposits_series,
            'withdrawals': withdrawals_series,
            'stakes': stake_series,
            'payouts': payouts_series,
            'profit': profit_series,
        }
        cache.set(charts_cache_key, charts_data, 60)

    tx_page = None
    bet_tickets_page = None
    agent_filter_options = []
    deposits_page = None
    withdrawals_page = None
    wallets_page = None
    commissions_page = None
    commission_rows = []
    commission_period_options = []
    bonuses_page = None
    audit_page = None
    settlements_page = None
    settlement_batch = None
    settlement_items = None
    ledger_page = None
    selected_journal_entry = None
    gateway_logs_page = None
    pin_logs_page = None
    recon_deposits_page = None
    recon_mismatch_events_page = None
    fraud_high_risk_fixtures = []
    fraud_suspicious_withdrawals = []
    fraud_large_withdrawals = []
    scheduled_reports = None

    if active_tab == 'bets':
        bet_tickets_page, agent_filter_options = build_dashboard_bets_page(
            BetTicket.objects.filter(placed_at__gte=metrics_start_dt, placed_at__lte=metrics_end_dt),
            bet_q=bet_q,
            bet_status=bet_status,
            bet_agent_id=bet_agent_id,
            page_number=(request.GET.get('bets_page') or 1),
        )

    if active_tab == 'transactions':
        tx_qs = Transaction.objects.select_related('user', 'initiating_user').order_by('-timestamp')
        if start_dt:
            tx_qs = tx_qs.filter(timestamp__gte=start_dt)
        if end_dt:
            tx_qs = tx_qs.filter(timestamp__lte=end_dt)
        if q:
            tx_qs = tx_qs.filter(Q(user__email__icontains=q) | Q(user__username__icontains=q) | Q(user__phone_number__icontains=q) | Q(id__icontains=q))
        if tx_type:
            tx_qs = tx_qs.filter(transaction_type=tx_type)
        if tx_status:
            tx_qs = tx_qs.filter(status=tx_status)
        if tx_gateway:
            tx_qs = tx_qs.filter(payment_gateway=tx_gateway)
        try:
            if amount_min:
                tx_qs = tx_qs.filter(amount__gte=Decimal(amount_min))
        except Exception:
            pass
        try:
            if amount_max:
                tx_qs = tx_qs.filter(amount__lte=Decimal(amount_max))
        except Exception:
            pass
        tx_p = Paginator(tx_qs, 50)
        try:
            tx_page = tx_p.page(request.GET.get('tx_page') or 1)
        except Exception:
            tx_page = tx_p.page(1)

    if active_tab == 'deposits':
        dep_qs = Transaction.objects.filter(transaction_type='deposit').select_related('user').order_by('-timestamp')
        if start_dt:
            dep_qs = dep_qs.filter(timestamp__gte=start_dt)
        if end_dt:
            dep_qs = dep_qs.filter(timestamp__lte=end_dt)
        if q:
            dep_qs = dep_qs.filter(Q(user__email__icontains=q) | Q(user__username__icontains=q) | Q(paystack_reference__icontains=q) | Q(external_reference__icontains=q))
        if tx_status:
            dep_qs = dep_qs.filter(status=tx_status)
        if tx_gateway:
            dep_qs = dep_qs.filter(payment_gateway=tx_gateway)
        dep_p = Paginator(dep_qs, 50)
        try:
            deposits_page = dep_p.page(request.GET.get('dep_page') or 1)
        except Exception:
            deposits_page = dep_p.page(1)

    if active_tab == 'withdrawals':
        w_qs = UserWithdrawal.objects.select_related('user', 'approved_rejected_by').order_by('-request_time')
        if start_dt:
            w_qs = w_qs.filter(request_time__gte=start_dt)
        if end_dt:
            w_qs = w_qs.filter(request_time__lte=end_dt)
        if q:
            w_qs = w_qs.filter(Q(user__email__icontains=q) | Q(user__username__icontains=q) | Q(account_number__icontains=q))
        if tx_status:
            w_qs = w_qs.filter(status=tx_status)
        w_p = Paginator(w_qs, 50)
        try:
            withdrawals_page = w_p.page(request.GET.get('w_page') or 1)
        except Exception:
            withdrawals_page = w_p.page(1)

    if active_tab == 'wallets':
        wq = Wallet.objects.select_related('user').order_by('-balance')
        if q:
            wq = wq.filter(Q(user__email__icontains=q) | Q(user__username__icontains=q))
        wp = Paginator(wq, 50)
        try:
            wallets_page = wp.page(request.GET.get('wallets_page') or 1)
        except Exception:
            wallets_page = wp.page(1)

    if active_tab == 'commissions':
        finance_agents_qs = User.objects.filter(user_type='agent', is_superuser=False)
        if commission_agent_q:
            finance_agents_qs = finance_agents_qs.filter(
                Q(username__icontains=commission_agent_q) |
                Q(email__icontains=commission_agent_q)
            )
        commission_rows, commission_period_options, selected_commission_period_id = build_weekly_commission_dashboard_rows(
            finance_agents_qs,
            selected_commission_period_id,
        )

    if active_tab == 'bonuses':
        bq = Transaction.objects.filter(transaction_type='bonus').select_related('user').order_by('-timestamp')
        if start_dt:
            bq = bq.filter(timestamp__gte=start_dt)
        if end_dt:
            bq = bq.filter(timestamp__lte=end_dt)
        bp = Paginator(bq, 50)
        try:
            bonuses_page = bp.page(request.GET.get('bonus_page') or 1)
        except Exception:
            bonuses_page = bp.page(1)

    if active_tab == 'audit' and finance_can_view_audit(request.user):
        aq = FinanceAuditLog.objects.select_related('actor', 'target_user', 'transaction', 'withdrawal').order_by('-created_at')
        if start_dt:
            aq = aq.filter(created_at__gte=start_dt)
        if end_dt:
            aq = aq.filter(created_at__lte=end_dt)
        if audit_action_type:
            aq = aq.filter(action_type=audit_action_type)
        if audit_q:
            aq = aq.filter(
                Q(actor__email__icontains=audit_q) |
                Q(target_user__email__icontains=audit_q) |
                Q(reason__icontains=audit_q) |
                Q(notes__icontains=audit_q)
            )
        ap = Paginator(aq, 50)
        try:
            audit_page = ap.page(request.GET.get('audit_page') or 1)
        except Exception:
            audit_page = ap.page(1)

    if active_tab == 'settlements':
        sq = FinanceSettlementBatch.objects.select_related('created_by', 'approved_by').order_by('-created_at')
        sq = sq.filter(period_start__lte=metrics_end_date, period_end__gte=metrics_start_date)
        if settlement_status:
            sq = sq.filter(status=settlement_status)
        sp = Paginator(sq, 25)
        try:
            settlements_page = sp.page(request.GET.get('settlements_page') or 1)
        except Exception:
            settlements_page = sp.page(1)
        if settlement_id:
            settlement_batch = FinanceSettlementBatch.objects.filter(id=settlement_id).select_related('created_by', 'approved_by').first()
            if settlement_batch:
                settlement_items = settlement_batch.items.select_related('beneficiary', 'weekly_commission', 'monthly_commission').order_by('-created_at')[:2000]

    if active_tab == 'ledger':
        lq = JournalEntry.objects.select_related('created_by').order_by('-entry_date', '-created_at')
        lq = lq.filter(entry_date__gte=metrics_start_date, entry_date__lte=metrics_end_date)
        if ledger_q:
            lq = lq.filter(Q(memo__icontains=ledger_q) | Q(id__icontains=ledger_q))
        lp = Paginator(lq, 50)
        try:
            ledger_page = lp.page(request.GET.get('ledger_page') or 1)
        except Exception:
            ledger_page = lp.page(1)
        if journal_id:
            try:
                selected_journal_entry = JournalEntry.objects.select_related('created_by').prefetch_related('lines__account').get(id=int(journal_id))
            except Exception:
                selected_journal_entry = None

    if active_tab == 'gateways' and finance_can_view_gateways(request.user):
        gq = PaymentGatewayEventLog.objects.select_related('transaction', 'user').order_by('-created_at')
        gq = gq.filter(created_at__gte=metrics_start_dt, created_at__lte=metrics_end_dt)
        if gateway_filter:
            gq = gq.filter(gateway=gateway_filter)
        gp = Paginator(gq, 50)
        try:
            gateway_logs_page = gp.page(request.GET.get('gateway_page') or 1)
        except Exception:
            gateway_logs_page = gp.page(1)

    if active_tab == 'pin_logs' and finance_can_view_pin_logs(request.user):
        pq = WithdrawalPinVerificationLog.objects.select_related('user').order_by('-created_at')
        pq = pq.filter(created_at__gte=metrics_start_dt, created_at__lte=metrics_end_dt)
        if pin_success in ['1', '0']:
            pq = pq.filter(success=(pin_success == '1'))
        if pin_q:
            pq = pq.filter(Q(user__email__icontains=pin_q) | Q(user__username__icontains=pin_q) | Q(ip_address__icontains=pin_q))
        pp = Paginator(pq, 50)
        try:
            pin_logs_page = pp.page(request.GET.get('pin_page') or 1)
        except Exception:
            pin_logs_page = pp.page(1)

    if active_tab == 'reconciliation':
        dep_q = Transaction.objects.filter(transaction_type='deposit').select_related('user').order_by('-timestamp')
        dep_q = dep_q.filter(timestamp__gte=metrics_start_dt, timestamp__lte=metrics_end_dt)
        if recon_filter == 'pending':
            dep_q = dep_q.filter(status='pending')
        elif recon_filter == 'failed':
            dep_q = dep_q.filter(status='failed')
        elif recon_filter == 'mismatch':
            dep_q = dep_q.filter(description__icontains='Amount mismatch')
        else:
            dep_q = dep_q.filter(Q(status__in=['pending', 'failed']) | Q(is_successful=False) | Q(description__icontains='Amount mismatch'))
        if q:
            dep_q = dep_q.filter(Q(user__email__icontains=q) | Q(user__username__icontains=q) | Q(external_reference__icontains=q) | Q(paystack_reference__icontains=q))
        dp = Paginator(dep_q, 50)
        try:
            recon_deposits_page = dp.page(request.GET.get('recon_page') or 1)
        except Exception:
            recon_deposits_page = dp.page(1)

        mis_q = PaymentGatewayEventLog.objects.select_related('transaction', 'user').order_by('-created_at')
        mis_q = mis_q.filter(
            event_type='verify',
            success=True,
            created_at__gte=metrics_start_dt,
            created_at__lte=metrics_end_dt,
        ).filter(
            transaction__transaction_type='deposit'
        ).exclude(
            transaction__status='completed',
            transaction__is_successful=True,
        )
        mp = Paginator(mis_q, 50)
        try:
            recon_mismatch_events_page = mp.page(request.GET.get('mis_page') or 1)
        except Exception:
            recon_mismatch_events_page = mp.page(1)

    if active_tab == 'fraud':
        try:
            FixtureLiabilitySnapshot = apps.get_model('risk', 'FixtureLiabilitySnapshot')
            RiskEngineSettings = apps.get_model('risk', 'RiskEngineSettings')
            settings_obj = RiskEngineSettings.load()
            threshold = int(getattr(settings_obj, 'risk_threshold_percent', 85) or 85)
            fraud_high_risk_fixtures = list(
                FixtureLiabilitySnapshot.objects.select_related('fixture')
                .filter(risk_score__gte=threshold)
                .order_by('-risk_score', '-updated_at')[:25]
            )
        except Exception:
            fraud_high_risk_fixtures = []

        suspicious = (
            UserWithdrawal.objects.filter(request_time__gte=metrics_start_dt, request_time__lte=metrics_end_dt)
            .values('bank_name', 'account_number')
            .annotate(
                cnt=Count('id'),
                total=Coalesce(Sum('amount'), Value(0), output_field=DecimalField()),
            )
            .filter(cnt__gte=2)
            .order_by('-cnt', '-total')[:50]
        )
        fraud_suspicious_withdrawals = list(suspicious)

        try:
            large_cutoff = Decimal('500000.00')
        except Exception:
            large_cutoff = Decimal('500000.00')
        fraud_large_withdrawals = list(
            UserWithdrawal.objects.select_related('user')
            .filter(request_time__gte=metrics_start_dt, request_time__lte=metrics_end_dt, amount__gte=large_cutoff)
            .order_by('-amount')[:50]
        )

    if active_tab == 'reports':
        scheduled_reports = ScheduledFinanceReport.objects.select_related('created_by').order_by('-created_at')[:200]

    overdraft_reporting_context = {}
    if active_tab == 'overdraft_center':
        overdraft_reporting_context = _build_overdraft_reporting_dashboard_context(
            request,
            include_retail_manager=True,
            extra_params={'tab': 'overdraft_center'},
        )

    recent_events = []
    if active_tab == 'overview':
        recent_txs = list(Transaction.objects.select_related('user').order_by('-timestamp')[:15])
        recent_withdrawals = list(UserWithdrawal.objects.select_related('user').order_by('-request_time')[:15])
        recent_bets = list(BetTicket.objects.select_related('user').order_by('-placed_at')[:15])
        for tx in recent_txs:
            recent_events.append({
                'ts': tx.timestamp.isoformat() if tx.timestamp else '',
                'type': 'transaction',
                'user': tx.user.email or tx.user.username,
                'label': tx.transaction_type,
                'amount': str(tx.amount),
                'status': tx.status,
            })
        for w in recent_withdrawals:
            recent_events.append({
                'ts': w.request_time.isoformat() if w.request_time else '',
                'type': 'withdrawal',
                'user': w.user.email or w.user.username,
                'label': 'Withdrawal request',
                'amount': str(w.amount),
                'status': w.status,
            })
        for b in recent_bets:
            recent_events.append({
                'ts': b.placed_at.isoformat() if b.placed_at else '',
                'type': 'bet',
                'user': b.user.email or b.user.username,
                'label': f"Bet placed ({b.ticket_id or ''})".strip(),
                'amount': str(b.stake_amount),
                'status': b.status,
            })
        recent_events.sort(key=lambda e: e.get('ts') or '', reverse=True)
        recent_events = recent_events[:30]

    context = {
        'active_tab': active_tab,
        'start_date': start_date_str,
        'end_date': end_date_str,
        'metrics_label': metrics_label,
        'q': q,
        'bet_q': bet_q,
        'bet_status': bet_status,
        'bet_agent': bet_agent_id,
        'tx_type': tx_type,
        'tx_status': tx_status,
        'tx_gateway': tx_gateway,
        'amount_min': amount_min,
        'amount_max': amount_max,
        'settlement_status': settlement_status,
        'settlement_id': settlement_id,
        'ledger_q': ledger_q,
        'ledger_account': ledger_account,
        'journal_id': journal_id,
        'gateway': gateway_filter,
        'pin_q': pin_q,
        'pin_success': pin_success,
        'recon': recon_filter,
        'fraud': fraud_filter,
        'kpis': {
            'deposits_today': deposits_today,
            'withdrawals_today': withdrawals_today,
            'pending_withdrawals': pending_withdrawals_count,
            'successful_withdrawals_today': successful_withdrawals_today,
            'failed_transactions': failed_transactions,
            'ggr': ggr,
            'ngr': ngr,
            'total_stakes': total_stakes,
            'total_payouts': total_payouts,
            'profit_loss': profit_loss,
            'agent_commissions': agent_commissions,
            'bonus_expenses': bonus_expenses,
            'payment_gateway_charges': payment_gateway_charges,
            'wallet_liabilities': current_wallet_liabilities,
            'exposure_liabilities': current_exposure_liabilities,
            'operational_balance': available_operational_balance,
        },
        'charts_data': charts_data,
        'initial_events_json': json.dumps(recent_events),
        'bet_tickets_page': bet_tickets_page,
        'agent_filter_options': agent_filter_options,
        'tx_page': tx_page,
        'deposits_page': deposits_page,
        'withdrawals_page': withdrawals_page,
        'wallets_page': wallets_page,
        'commissions_page': commissions_page,
        'commission_rows': commission_rows,
        'commission_period_options': commission_period_options,
        'selected_commission_period_id': selected_commission_period_id,
        'commission_agent_q': commission_agent_q,
        'bonuses_page': bonuses_page,
        'audit_page': audit_page,
        'settlements_page': settlements_page,
        'settlement_batch': settlement_batch,
        'settlement_items': settlement_items,
        'ledger_page': ledger_page,
        'selected_journal_entry': selected_journal_entry,
        'ledger_accounts': list(LedgerAccount.objects.filter(is_active=True).order_by('code').values('code', 'name')),
        'gateway_logs_page': gateway_logs_page,
        'pin_logs_page': pin_logs_page,
        'recon_deposits_page': recon_deposits_page,
        'recon_mismatch_events_page': recon_mismatch_events_page,
        'fraud_high_risk_fixtures': fraud_high_risk_fixtures,
        'fraud_suspicious_withdrawals': fraud_suspicious_withdrawals,
        'fraud_large_withdrawals': fraud_large_withdrawals,
        'scheduled_reports': scheduled_reports,
        'audit_q': audit_q,
        'audit_action_type': audit_action_type,
        'audit_action_choices': getattr(FinanceAuditLog, 'ACTION_TYPES', ()),
        'can_approve_withdrawals': finance_can_approve_withdrawals(request.user),
        'can_reverse_transactions': finance_can_reverse_transactions(request.user),
        'can_verify_transactions': finance_can_verify_transactions(request.user),
        'can_adjust_wallets': finance_can_adjust_wallets(request.user),
        'can_export': finance_can_export(request.user),
        'can_view_audit': finance_can_view_audit(request.user),
        'can_manage_settlements': finance_can_manage_settlements(request.user),
        'can_manage_ledger': finance_can_manage_ledger(request.user),
        'can_view_gateways': finance_can_view_gateways(request.user),
        'can_view_pin_logs': finance_can_view_pin_logs(request.user),
        'ticket_transactions_widget': _ticket_transaction_widget_context(
            request.user,
            limit=12,
            date_from=start_date_str,
            date_to=end_date_str,
        ),
        **overdraft_reporting_context,
    }
    return render(request, 'betting/finance_dashboard.html', context)


@login_required
@user_passes_test(is_finance_user)
def finance_export(request):
    if not finance_can_export(request.user):
        return HttpResponse("Not allowed.", status=403)
    dataset = (request.GET.get('dataset') or '').strip().lower()
    fmt = (request.GET.get('format') or 'csv').strip().lower()
    start_date_str = (request.GET.get('start_date') or '').strip()
    end_date_str = (request.GET.get('end_date') or '').strip()

    start_dt = None
    end_dt = None
    try:
        if start_date_str:
            start_dt = timezone.make_aware(datetime.strptime(start_date_str, "%Y-%m-%d"))
    except Exception:
        start_dt = None
    try:
        if end_date_str:
            end_raw = datetime.strptime(end_date_str, "%Y-%m-%d")
            end_dt = timezone.make_aware(datetime.combine(end_raw.date(), datetime.max.time()))
    except Exception:
        end_dt = None

    today = timezone.localdate()
    if not start_dt:
        start_dt = timezone.make_aware(datetime.combine(today - timedelta(days=30), datetime.min.time()))
    if not end_dt:
        end_dt = timezone.make_aware(datetime.combine(today, datetime.max.time()))

    rows = []
    title = dataset or 'report'

    if dataset == 'overdrafts':
        report_context = _build_overdraft_reporting_dashboard_context(
            request,
            include_retail_manager=True,
            extra_params={'tab': 'overdraft_center'},
        )
        rows = _loan_reporting_export_rows(
            report_context['overdraft_reporting']['rows'],
            include_retail_manager=True,
        )
        title = 'finance_overdraft_center'

    elif dataset == 'deposits':
        qs = Transaction.objects.filter(transaction_type='deposit').filter(timestamp__gte=start_dt, timestamp__lte=end_dt).select_related('user').order_by('-timestamp')
        for tx in qs[:100000]:
            rows.append({
                'time': tx.timestamp.isoformat(sep=' ', timespec='seconds'),
                'tx_id': str(tx.id),
                'user': tx.user.email or tx.user.username,
                'amount': str(tx.amount),
                'status': tx.status,
                'successful': 'yes' if tx.is_successful else 'no',
                'gateway': getattr(tx, 'payment_gateway', ''),
                'ref': tx.paystack_reference or tx.external_reference or '',
            })
        title = 'deposits'

    elif dataset == 'withdrawals':
        qs = UserWithdrawal.objects.filter(request_time__gte=start_dt, request_time__lte=end_dt).select_related('user', 'approved_rejected_by').order_by('-request_time')
        for w in qs[:100000]:
            rows.append({
                'time': w.request_time.isoformat(sep=' ', timespec='seconds'),
                'withdrawal_id': str(w.id),
                'user': w.user.email or w.user.username,
                'amount': str(w.amount),
                'status': w.status,
                'bank': w.bank_name,
                'account_number': w.account_number,
                'handled_by': getattr(getattr(w, 'approved_rejected_by', None), 'email', '') or '',
            })
        title = 'withdrawals'

    elif dataset == 'transactions':
        qs = Transaction.objects.filter(timestamp__gte=start_dt, timestamp__lte=end_dt).select_related('user', 'initiating_user').order_by('-timestamp')
        for tx in qs[:100000]:
            rows.append({
                'time': tx.timestamp.isoformat(sep=' ', timespec='seconds'),
                'tx_id': str(tx.id),
                'user': tx.user.email or tx.user.username,
                'type': tx.transaction_type,
                'amount': str(tx.amount),
                'status': tx.status,
                'successful': 'yes' if tx.is_successful else 'no',
                'gateway': getattr(tx, 'payment_gateway', ''),
                'initiator': getattr(getattr(tx, 'initiating_user', None), 'email', '') or '',
            })
        title = 'transactions'

    elif dataset == 'ledger':
        qs = FinanceAuditLog.objects.filter(created_at__gte=start_dt, created_at__lte=end_dt).select_related('actor', 'target_user', 'transaction', 'withdrawal').order_by('-created_at')
        for a in qs[:100000]:
            rows.append({
                'time': a.created_at.isoformat(sep=' ', timespec='seconds'),
                'action': a.action_type,
                'actor': getattr(getattr(a, 'actor', None), 'email', '') or '',
                'target_user': getattr(getattr(a, 'target_user', None), 'email', '') or '',
                'transaction_id': str(a.transaction_id) if a.transaction_id else '',
                'withdrawal_id': str(a.withdrawal_id) if a.withdrawal_id else '',
                'reason': a.reason,
            })
        title = 'ledger'

    elif dataset == 'journals':
        qs = JournalEntry.objects.filter(created_at__gte=start_dt, created_at__lte=end_dt).select_related('created_by').prefetch_related('lines__account').order_by('-created_at')
        for je in qs[:50000]:
            for line in list(getattr(je, 'lines', []).all())[:50]:
                rows.append({
                    'time': je.created_at.isoformat(sep=' ', timespec='seconds'),
                    'entry_date': je.entry_date.isoformat(),
                    'journal_id': str(je.id),
                    'memo': je.memo,
                    'created_by': getattr(getattr(je, 'created_by', None), 'email', '') or '',
                    'account': getattr(getattr(line, 'account', None), 'code', '') or '',
                    'account_name': getattr(getattr(line, 'account', None), 'name', '') or '',
                    'debit': str(line.debit),
                    'credit': str(line.credit),
                })
        title = 'journals'

    elif dataset == 'settlements':
        qs = FinanceSettlementBatch.objects.filter(created_at__gte=start_dt, created_at__lte=end_dt).select_related('created_by', 'approved_by').order_by('-created_at')
        for b in qs[:50000]:
            totals = b.items.aggregate(s=Coalesce(Sum('amount'), Value(0), output_field=DecimalField()))['s']
            rows.append({
                'time': b.created_at.isoformat(sep=' ', timespec='seconds'),
                'batch_id': str(b.id),
                'type': b.settlement_type,
                'status': b.status,
                'period_start': b.period_start.isoformat(),
                'period_end': b.period_end.isoformat(),
                'items_total': str(totals),
                'created_by': getattr(getattr(b, 'created_by', None), 'email', '') or '',
                'approved_by': getattr(getattr(b, 'approved_by', None), 'email', '') or '',
            })
        title = 'settlements'

    elif dataset == 'gateway_logs':
        qs = PaymentGatewayEventLog.objects.filter(created_at__gte=start_dt, created_at__lte=end_dt).select_related('transaction', 'user').order_by('-created_at')
        for g in qs[:100000]:
            rows.append({
                'time': g.created_at.isoformat(sep=' ', timespec='seconds'),
                'gateway': g.gateway,
                'event': g.event_type,
                'reference': g.reference,
                'success': 'yes' if g.success else 'no',
                'http_status': str(g.http_status or ''),
                'amount': str(g.amount or ''),
                'fee': str(g.fee_amount or ''),
                'user': getattr(getattr(g, 'user', None), 'email', '') or '',
                'tx_id': str(g.transaction_id or ''),
                'message': g.message,
            })
        title = 'gateway_logs'

    elif dataset == 'pin_logs':
        qs = WithdrawalPinVerificationLog.objects.filter(created_at__gte=start_dt, created_at__lte=end_dt).select_related('user').order_by('-created_at')
        for p in qs[:100000]:
            rows.append({
                'time': p.created_at.isoformat(sep=' ', timespec='seconds'),
                'user': p.user.email or p.user.username,
                'success': 'yes' if p.success else 'no',
                'ip': p.ip_address or '',
                'user_agent': p.user_agent or '',
            })
        title = 'pin_logs'

    else:
        return HttpResponse("Unknown dataset.", status=400)

    FinanceAuditLog.objects.create(
        actor=request.user,
        action_type='REPORT_EXPORTED',
        ip_address=get_client_ip(request),
        data={'dataset': title, 'format': fmt, 'range': [start_dt.date().isoformat(), end_dt.date().isoformat()]},
    )

    filename_base = f"finance_{title}_{timezone.now().strftime('%Y%m%d_%H%M%S')}"

    if fmt == 'csv':
        import csv
        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = f'attachment; filename="{filename_base}.csv"'
        fieldnames = list(rows[0].keys()) if rows else []
        writer = csv.DictWriter(response, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            writer.writerow(r)
        return response

    if fmt == 'xlsx':
        import io
        import pandas as pd
        output = io.BytesIO()
        df = pd.DataFrame(rows)
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            df.to_excel(writer, index=False, sheet_name=title[:31] or 'Sheet1')
        output.seek(0)
        response = HttpResponse(output.getvalue(), content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        response['Content-Disposition'] = f'attachment; filename="{filename_base}.xlsx"'
        return response

    if fmt == 'pdf':
        from weasyprint import HTML
        def esc(s):
            return (str(s or '')
                    .replace('&', '&amp;')
                    .replace('<', '&lt;')
                    .replace('>', '&gt;')
                    .replace('"', '&quot;')
                    .replace("'", '&#39;'))
        cols = list(rows[0].keys()) if rows else []
        head = ''.join([f"<th>{esc(c)}</th>" for c in cols])
        body = ''.join([
            "<tr>" + ''.join([f"<td>{esc(r.get(c))}</td>" for c in cols]) + "</tr>"
            for r in rows[:3000]
        ])
        html = f"""
        <html>
          <head>
            <meta charset="utf-8" />
            <style>
              body {{ font-family: Arial, sans-serif; font-size: 11px; }}
              h2 {{ margin: 0 0 8px 0; }}
              .meta {{ color: #666; margin-bottom: 12px; }}
              table {{ width: 100%; border-collapse: collapse; }}
              th, td {{ border: 1px solid #ddd; padding: 6px; vertical-align: top; }}
              th {{ background: #f3f5f7; text-align: left; }}
              tr:nth-child(even) td {{ background: #fafafa; }}
            </style>
          </head>
          <body>
            <h2>Finance Report: {esc(title)}</h2>
            <div class="meta">Range: {esc(start_dt.date().isoformat())} → {esc(end_dt.date().isoformat())}</div>
            <table>
              <thead><tr>{head}</tr></thead>
              <tbody>{body}</tbody>
            </table>
          </body>
        </html>
        """
        pdf_bytes = HTML(string=html, base_url=request.build_absolute_uri('/')).write_pdf()
        response = HttpResponse(pdf_bytes, content_type='application/pdf')
        response['Content-Disposition'] = f'attachment; filename="{filename_base}.pdf"'
        return response

    return HttpResponse("Unknown format.", status=400)

@login_required
@user_passes_test(is_crm_user)
def crm_user_detail(request, user_id):
    target = get_object_or_404(User, id=user_id)
    def _crm_allowed_targets_for_root(root_user):
        t = (getattr(root_user, 'user_type', '') or '').strip()
        if t == 'agent':
            return User.objects.filter(Q(id=root_user.id) | Q(agent=root_user, user_type__in=['cashier', 'player']))
        if t == 'super_agent':
            agent_ids = User.objects.filter(super_agent=root_user, user_type='agent').values_list('id', flat=True)
            return User.objects.filter(
                Q(id=root_user.id) |
                Q(id__in=list(agent_ids)) |
                Q(agent_id__in=list(agent_ids), user_type__in=['cashier', 'player'])
            )
        if t == 'master_agent':
            sa_ids = User.objects.filter(master_agent=root_user, user_type='super_agent').values_list('id', flat=True)
            ag_ids = User.objects.filter(Q(master_agent=root_user) | Q(super_agent_id__in=list(sa_ids))).filter(user_type='agent').values_list('id', flat=True)
            return User.objects.filter(
                Q(id=root_user.id) |
                Q(id__in=list(sa_ids)) |
                Q(id__in=list(ag_ids)) |
                Q(agent_id__in=list(ag_ids), user_type__in=['cashier', 'player'])
            ).distinct()
        return User.objects.filter(id=root_user.id)

    scoped_users_qs = _crm_allowed_targets_for_root(target)
    scoped_user_ids = list(scoped_users_qs.values_list('id', flat=True))
    downline_user_ids = [uid for uid in scoped_user_ids if uid != target.id]
    hierarchy_types = ['agent', 'super_agent', 'master_agent']
    ticket_scope_ids = downline_user_ids if target.user_type in hierarchy_types else [target.id]
    withdrawal_scope_ids = scoped_user_ids if target.user_type in hierarchy_types else [target.id]

    wallet = Wallet.objects.filter(user=target).first()
    txs = attach_wallet_balance_snapshots(
        Transaction.objects.filter(user=target).select_related('initiating_user').order_by('-timestamp')[:30]
    )
    deposits = Transaction.objects.filter(user=target, transaction_type='deposit').order_by('-timestamp')[:20]
    bonuses = Transaction.objects.filter(user=target, transaction_type='bonus').order_by('-timestamp')[:20]
    tickets = BetTicket.objects.filter(user_id__in=ticket_scope_ids).select_related('user').order_by('-placed_at')[:20]
    withdrawals = list(
        UserWithdrawal.objects.filter(user_id__in=withdrawal_scope_ids).select_related('user', 'approved_rejected_by').order_by('-request_time')[:20]
    )
    for withdrawal in withdrawals:
        withdrawal.entry_kind = 'withdrawal'
        actor = getattr(withdrawal, 'approved_rejected_by', None)
        withdrawal.actor_display = (
            getattr(actor, 'email', '') or getattr(actor, 'username', '') or '-'
        ) if actor else '-'

    debit_activity_logs = list(
        Transaction.objects.filter(
            user_id__in=withdrawal_scope_ids,
        ).filter(
            Q(transaction_type__in=['account_user_debit', 'manual_debit', 'commission_recall_debit']) |
            Q(transaction_type='wallet_transfer_out', description__icontains='CRM debit') |
            Q(transaction_type='wallet_transfer_out', description__icontains='withdrawal')
        )
        .select_related('user', 'initiating_user')
        .order_by('-timestamp')[:20]
    )
    for tx in debit_activity_logs:
        tx.entry_kind = 'wallet_debit'
        tx.request_time = tx.timestamp
        tx.actor_display = (
            getattr(getattr(tx, 'initiating_user', None), 'email', '')
            or getattr(getattr(tx, 'initiating_user', None), 'username', '')
            or '-'
        )
        tx.action_label = tx.get_transaction_type_display() if hasattr(tx, 'get_transaction_type_display') else tx.transaction_type
    if debit_activity_logs:
        withdrawals.extend(debit_activity_logs)
        withdrawals.sort(key=lambda item: getattr(item, 'request_time', None) or timezone.now(), reverse=True)
        withdrawals = withdrawals[:20]

    real_withdrawal_reports = list(
        WithdrawalReport.objects.filter(withdrawal__user_id__in=withdrawal_scope_ids).select_related('withdrawal', 'user').order_by('-created_at')[:50]
    )
    reported_withdrawal_ids = {r.withdrawal_id for r in real_withdrawal_reports}
    synthetic_withdrawal_reports = []
    for withdrawal in withdrawals:
        if getattr(withdrawal, 'entry_kind', 'withdrawal') == 'withdrawal':
            if withdrawal.id in reported_withdrawal_ids:
                continue
            synthetic_withdrawal_reports.append(
                SimpleNamespace(
                    requested_at=getattr(withdrawal, 'request_time', None),
                    updated_at=getattr(withdrawal, 'approved_rejected_time', None) or getattr(withdrawal, 'request_time', None),
                    user=getattr(withdrawal, 'user', None),
                    username=(getattr(getattr(withdrawal, 'user', None), 'email', '') or getattr(getattr(withdrawal, 'user', None), 'username', '') or '-'),
                    transaction_reference=f"WD-{withdrawal.id}",
                    withdrawal_status=getattr(withdrawal, 'status', ''),
                    event=getattr(withdrawal, 'status', 'requested'),
                    is_admin_copy=False,
                    email_sent_at=None,
                    email_error='',
                )
            )
        elif getattr(withdrawal, 'entry_kind', '') == 'wallet_debit':
            synthetic_withdrawal_reports.append(
                SimpleNamespace(
                    requested_at=getattr(withdrawal, 'timestamp', None),
                    updated_at=getattr(withdrawal, 'timestamp', None),
                    user=getattr(withdrawal, 'user', None),
                    username=(getattr(getattr(withdrawal, 'user', None), 'email', '') or getattr(getattr(withdrawal, 'user', None), 'username', '') or '-'),
                    transaction_reference=str(getattr(withdrawal, 'id', '')),
                    withdrawal_status='completed',
                    event='completed',
                    is_admin_copy=False,
                    email_sent_at=None,
                    email_error='',
                )
            )
    withdrawal_reports = sorted(
        real_withdrawal_reports + synthetic_withdrawal_reports,
        key=lambda item: getattr(item, 'updated_at', None) or getattr(item, 'requested_at', None) or timezone.now(),
        reverse=True,
    )[:50]

    profile_form = CRMUserProfileForm(instance=target, request=request)

    if request.method == 'POST':
        if 'save_profile' in request.POST:
            if not crm_can_edit_profiles(request.user):
                messages.error(request, 'Not allowed.')
                return redirect('betting:crm_user_detail', user_id=target.id)
            profile_form = CRMUserProfileForm(request.POST, instance=target, request=request)
            if profile_form.is_valid():
                before = {
                    'first_name': target.first_name,
                    'last_name': target.last_name,
                    'other_name': target.other_name,
                    'phone_number': target.phone_number,
                    'state_id': target.state_id,
                    'shop_address': target.shop_address,
                    'bank_account_name': target.bank_account_name,
                    'kyc_status': getattr(target, 'kyc_status', ''),
                    'vip_level': getattr(target, 'vip_level', ''),
                    'vip_manager_id': getattr(target, 'vip_manager_id', None),
                }
                updated_user = profile_form.save()
                after = {
                    'first_name': updated_user.first_name,
                    'last_name': updated_user.last_name,
                    'other_name': updated_user.other_name,
                    'phone_number': updated_user.phone_number,
                    'state_id': updated_user.state_id,
                    'shop_address': updated_user.shop_address,
                    'bank_account_name': updated_user.bank_account_name,
                    'kyc_status': getattr(updated_user, 'kyc_status', ''),
                    'vip_level': getattr(updated_user, 'vip_level', ''),
                    'vip_manager_id': getattr(updated_user, 'vip_manager_id', None),
                }
                CRMActionLog.objects.create(
                    actor=request.user,
                    target_user=updated_user,
                    action_type='PROFILE_EDITED' if before == after else 'VIP_UPDATED',
                    data={'before': before, 'after': after},
                )
                messages.success(request, 'Profile updated.')
                return redirect('betting:crm_user_detail', user_id=target.id)
            messages.error(request, 'Please correct the errors.')

        elif 'toggle_active' in request.POST:
            if not crm_can_suspend_users(request.user):
                messages.error(request, 'Not allowed.')
                return redirect('betting:crm_user_detail', user_id=target.id)
            reason = (request.POST.get('reason') or '').strip()
            make_active = request.POST.get('make_active') == '1'
            if make_active:
                target.is_active = True
                target.save(update_fields=['is_active'])
                CRMActionLog.objects.create(
                    actor=request.user,
                    target_user=target,
                    action_type='USER_UNSUSPENDED',
                    reason=reason,
                )
                messages.success(request, 'User re-activated.')
            else:
                target.is_active = False
                target.save(update_fields=['is_active'])
                CRMActionLog.objects.create(
                    actor=request.user,
                    target_user=target,
                    action_type='USER_SUSPENDED',
                    reason=reason,
                )
                messages.success(request, 'User suspended.')
            return redirect('betting:crm_user_detail', user_id=target.id)

        elif 'toggle_withdrawals' in request.POST:
            if not crm_can_freeze_withdrawals(request.user):
                messages.error(request, 'Not allowed.')
                return redirect('betting:crm_user_detail', user_id=target.id)
            freeze = request.POST.get('freeze') == '1'
            reason = (request.POST.get('reason') or '').strip()
            if freeze:
                target.withdrawal_locked = True
                target.withdrawal_locked_at = timezone.now()
                target.withdrawal_attempts = 0
                target.save(update_fields=['withdrawal_locked', 'withdrawal_locked_at', 'withdrawal_attempts'])
                CRMActionLog.objects.create(actor=request.user, target_user=target, action_type='WITHDRAWAL_FROZEN', reason=reason)
                messages.success(request, 'Withdrawals frozen.')
            else:
                target.withdrawal_locked = False
                target.withdrawal_locked_at = None
                target.withdrawal_attempts = 0
                target.save(update_fields=['withdrawal_locked', 'withdrawal_locked_at', 'withdrawal_attempts'])
                CRMActionLog.objects.create(actor=request.user, target_user=target, action_type='WITHDRAWAL_UNFROZEN', reason=reason)
                messages.success(request, 'Withdrawals unfrozen.')
            return redirect('betting:crm_user_detail', user_id=target.id)

        elif 'wallet_adjust' in request.POST:
            if not crm_can_manage_wallet(request.user):
                messages.error(request, 'Not allowed.')
                return redirect('betting:crm_user_detail', user_id=target.id)

            try:
                target_user_id = int(request.POST.get('target_user_id') or target.id)
            except Exception:
                target_user_id = target.id
            allowed_qs = _crm_allowed_targets_for_root(target).only('id')
            if not allowed_qs.filter(id=target_user_id).exists():
                messages.error(request, 'Not allowed to manage wallet for this user.')
                return redirect('betting:crm_user_detail', user_id=target.id)
            wallet_target = get_object_or_404(User, id=target_user_id)

            form = AdminManualWalletForm(request.POST)
            if not form.is_valid():
                messages.error(request, 'Invalid wallet action.')
                return redirect('betting:crm_user_detail', user_id=target.id)
            action = form.cleaned_data['action']
            amount = form.cleaned_data['amount']
            description = (form.cleaned_data.get('description') or '').strip()
            reason = (request.POST.get('reason') or '').strip()

            approver = get_default_wallet_request_approver()
            if not approver:
                messages.error(request, 'No active Account User or Admin is available to approve this wallet action.')
                return redirect('betting:crm_user_detail', user_id=target.id)

            request_type = 'crm_credit' if action == 'credit' else 'crm_debit'
            approval_reason = description or reason or f"CRM wallet {action} request"
            credit_request = CreditRequest.objects.create(
                requester=wallet_target,
                recipient=approver,
                amount=amount,
                reason=approval_reason,
                request_type=request_type,
                status='pending',
            )

            CreditLog.objects.create(
                actor=request.user,
                target_user=wallet_target,
                action_type=f'{request_type}_requested',
                amount=amount,
                status='pending',
                reference_id=str(credit_request.id)
            )
            CRMActionLog.objects.create(
                actor=request.user,
                target_user=wallet_target,
                action_type='WALLET_CREDIT_REQUESTED' if action == 'credit' else 'WALLET_DEBIT_REQUESTED',
                reason=reason,
                data={
                    'amount': str(amount),
                    'description': description,
                    'request_id': credit_request.id,
                    'approver_id': approver.id,
                    'approver_email': approver.email,
                },
            )
            messages.success(request, f"Wallet {action} request sent for Account User/Admin approval.")
            return redirect('betting:crm_user_detail', user_id=target.id)

        elif 'send_message' in request.POST:
            if not crm_can_message(request.user):
                messages.error(request, 'Not allowed.')
                return redirect('betting:crm_user_detail', user_id=target.id)
            try:
                target_user_id = int(request.POST.get('target_user_id') or target.id)
            except Exception:
                target_user_id = target.id
            allowed_qs = _crm_allowed_targets_for_root(target).only('id')
            if not allowed_qs.filter(id=target_user_id).exists():
                messages.error(request, 'Not allowed to message this user.')
                return redirect('betting:crm_user_detail', user_id=target.id)
            msg_target = get_object_or_404(User, id=target_user_id)

            title = (request.POST.get('msg_title') or '').strip() or 'Message'
            body = (request.POST.get('msg_body') or '').strip()
            via_inapp = request.POST.get('via_inapp') == '1'
            via_email = request.POST.get('via_email') == '1'
            via_sms = request.POST.get('via_sms') == '1'

            if not body:
                messages.error(request, 'Message is required.')
                return redirect('betting:crm_user_detail', user_id=target.id)
            if not (via_inapp or via_email or via_sms):
                messages.error(request, 'Select at least one channel (In-app, Email, or SMS).')
                return redirect('betting:crm_user_detail', user_id=target.id)

            sent = []
            errors = {}
            sms_status = None
            if via_inapp:
                try:
                    create_notification(
                        recipient=msg_target,
                        notification_type='SYSTEM_ANNOUNCEMENT',
                        title=title,
                        message=body,
                        data={
                            'popup_category': 'message',
                            'delivery_channel': 'in_app',
                            'url': '/notifications/',
                        },
                    )
                    sent.append('in_app')
                except Exception as e:
                    errors['in_app'] = str(e)
            if via_email:
                if not crm_can_send_direct_email(request.user):
                    errors['email'] = 'not_allowed'
                    messages.error(request, 'Not allowed to send email messages.')
                    CRMActionLog.objects.create(
                        actor=request.user,
                        target_user=msg_target,
                        action_type='MESSAGE_SENT',
                        data={'channels': sent, 'errors': errors, 'sms': sms_status or {}, 'blocked': 'email_not_allowed'},
                    )
                    return redirect('betting:crm_user_detail', user_id=target.id)
                try:
                    from django.core.mail import EmailMultiAlternatives
                    from django.template.loader import render_to_string
                    from django.utils.html import strip_tags
                    if not msg_target.email:
                        raise ValueError("Target user has no email address.")
                    html = render_to_string('betting/email/crm_message.html', {
                        'site_name': getattr(getattr(settings, 'SITE_NAME', None), 'strip', lambda: '')() or 'StakeNaija',
                        'title': title,
                        'body': body,
                        'user': msg_target,
                    })
                    text = strip_tags(html) or body
                    m = EmailMultiAlternatives(
                        subject=title,
                        body=text,
                        from_email=settings.DEFAULT_FROM_EMAIL or settings.EMAIL_HOST_USER,
                        to=[msg_target.email],
                    )
                    m.attach_alternative(html, "text/html")
                    m.send(fail_silently=False)
                    create_notification(
                        recipient=msg_target,
                        notification_type='SYSTEM_ANNOUNCEMENT',
                        title=title,
                        message=body,
                        data={
                            'popup_category': 'message',
                            'delivery_channel': 'email',
                            'url': '/notifications/',
                        },
                    )
                    sent.append('email')
                except Exception as e:
                    errors['email'] = str(e)
            if via_sms:
                try:
                    from notifications.services import send_sms_ebulksms
                    sms_status = send_sms_ebulksms(msisdn=msg_target.phone_number or '', message=body, sender=getattr(settings, 'EBULKSMS_SENDER', None))
                    if sms_status.get('ok'):
                        sent.append('sms')
                    else:
                        errors['sms'] = sms_status.get('error') or sms_status.get('status') or 'failed'
                except Exception as e:
                    sms_status = {'ok': False, 'error': str(e)}
                    errors['sms'] = str(e)

            CRMActionLog.objects.create(
                actor=request.user,
                target_user=msg_target,
                action_type='MESSAGE_SENT',
                data={'channels': sent, 'errors': errors, 'sms': sms_status or {}},
            )
            if sent:
                messages.success(request, f"Message sent via: {', '.join(sent)}.")
            else:
                messages.error(request, "Message was not sent. Check email/SMS configuration and recipient details.")
            return redirect('betting:crm_user_detail', user_id=target.id)

        elif 'reset_password' in request.POST:
            if not crm_can_reset_password(request.user):
                messages.error(request, 'Not allowed.')
                return redirect('betting:crm_user_detail', user_id=target.id)
            try:
                target_user_id = int(request.POST.get('target_user_id') or target.id)
            except Exception:
                target_user_id = target.id
            allowed_qs = _crm_allowed_targets_for_root(target).only('id')
            if not allowed_qs.filter(id=target_user_id).exists():
                messages.error(request, 'Not allowed to reset password for this user.')
                return redirect('betting:crm_user_detail', user_id=target.id)
            reset_target = get_object_or_404(User, id=target_user_id)
            agent_copy_to = None
            try:
                if reset_target.user_type in ['cashier', 'player'] and getattr(reset_target, 'agent_id', None):
                    agent_user = User.objects.filter(id=reset_target.agent_id).only('id', 'email', 'username', 'user_type').first()
                    if agent_user and agent_user.email:
                        agent_copy_to = agent_user.email
            except Exception:
                agent_copy_to = None

            to_emails = []
            cc_emails = []
            if reset_target.email:
                to_emails = [reset_target.email]
                if agent_copy_to and agent_copy_to.lower() != reset_target.email.lower():
                    cc_emails = [agent_copy_to]
            else:
                if agent_copy_to:
                    to_emails = [agent_copy_to]
                else:
                    messages.error(request, 'Target user has no email address and no agent email was found to notify.')
                    return redirect('betting:crm_user_detail', user_id=target.id)

            raw_password = get_random_string(12)
            reset_target.set_password(raw_password)
            reset_target.save(update_fields=['password'])
            logout_user_from_all_active_sessions(reset_target)

            login_url = request.build_absolute_uri(reverse('betting:login'))
            email_error = None
            try:
                from django.core.mail import EmailMultiAlternatives
                from django.template.loader import render_to_string
                from django.utils.html import strip_tags
                html = render_to_string('betting/email/password_reset.html', {
                    'site_name': getattr(getattr(settings, 'SITE_NAME', None), 'strip', lambda: '')() or 'StakeNaija',
                    'user': reset_target,
                    'raw_password': raw_password,
                    'login_url': login_url,
                    'agent_copy_to': agent_copy_to,
                })
                text = strip_tags(html) or f"Your password has been reset. New password: {raw_password}\nLogin: {login_url}"
                m = EmailMultiAlternatives(
                    subject='Password Reset',
                    body=text,
                    from_email=settings.DEFAULT_FROM_EMAIL or settings.EMAIL_HOST_USER,
                    to=to_emails,
                    cc=cc_emails,
                )
                m.attach_alternative(html, "text/html")
                m.send(fail_silently=False)
            except Exception as e:
                email_error = str(e)

            CRMActionLog.objects.create(
                actor=request.user,
                target_user=reset_target,
                action_type='PASSWORD_RESET',
                data={'email_error': email_error or '', 'to': to_emails, 'cc': cc_emails},
            )
            if email_error:
                messages.warning(request, f"Password reset succeeded, but email could not be sent: {email_error}")
            else:
                messages.success(request, 'Password reset and emailed to user.')
            return redirect('betting:crm_user_detail', user_id=target.id)

    login_attempts = LoginAttempt.objects.filter(user=target, status='success').order_by('-timestamp')[:20]
    DeviceFingerprint = apps.get_model('risk', 'DeviceFingerprint')
    SuspiciousActivityLog = apps.get_model('risk', 'SuspiciousActivityLog')
    IPIntelligence = apps.get_model('risk', 'IPIntelligence')

    device_fingerprints = []
    suspicious_logs = []
    ip_intel = None
    last_ip = None
    try:
        device_fingerprints = list(DeviceFingerprint.objects.filter(user=target).order_by('-last_seen_at')[:20])
    except Exception:
        device_fingerprints = []
    try:
        suspicious_logs = list(SuspiciousActivityLog.objects.filter(user=target).select_related('ticket').order_by('-created_at')[:20])
    except Exception:
        suspicious_logs = []
    try:
        last_ip = (login_attempts[0].ip_address if login_attempts else None) or (device_fingerprints[0].ip_address if device_fingerprints else None)
    except Exception:
        last_ip = None
    if last_ip:
        try:
            ip_intel = IPIntelligence.objects.filter(ip_address=last_ip).first()
        except Exception:
            ip_intel = None

    context = {
        'target_user': target,
        'default_action_target': target,
        'wallet': wallet,
        'transactions': txs,
        'deposits': deposits,
        'bonuses': bonuses,
        'tickets': tickets,
        'withdrawals': withdrawals,
        'withdrawal_reports': withdrawal_reports,
        'profile_form': profile_form,
        'can_approve_withdrawals': crm_can_approve_withdrawals(request.user),
        'can_suspend_users': crm_can_suspend_users(request.user),
        'can_approve_registrations': crm_can_approve_registrations(request.user),
        'can_edit_profiles': crm_can_edit_profiles(request.user),
        'can_manage_wallet': crm_can_manage_wallet(request.user),
        'can_freeze_withdrawals': crm_can_freeze_withdrawals(request.user),
        'can_reset_password': crm_can_reset_password(request.user),
        'can_message': crm_can_message(request.user),
        'login_attempts': login_attempts,
        'device_fingerprints': device_fingerprints,
        'suspicious_logs': suspicious_logs,
        'ip_intel': ip_intel,
    }
    return render(request, 'betting/crm_user_detail.html', context)


@login_required
@user_passes_test(is_crm_user)
def crm_user_downline_search(request, user_id):
    root = get_object_or_404(User, id=user_id)
    if not (crm_can_manage_wallet(request.user) or crm_can_reset_password(request.user) or crm_can_message(request.user)):
        return JsonResponse({'results': [], 'pagination': {'more': False}}, status=403)

    search_term = (request.GET.get('q', '') or '').strip()
    page = request.GET.get('page', 1)

    t = (getattr(root, 'user_type', '') or '').strip()
    qs = User.objects.filter(id=root.id)
    if t == 'agent':
        qs = User.objects.filter(Q(id=root.id) | Q(agent=root, user_type__in=['cashier', 'player']))
    elif t == 'super_agent':
        ag = User.objects.filter(super_agent=root, user_type='agent')
        qs = User.objects.filter(Q(id=root.id) | Q(id__in=ag.values('id')) | Q(agent_id__in=ag.values('id'), user_type__in=['cashier', 'player']))
    elif t == 'master_agent':
        sa = User.objects.filter(master_agent=root, user_type='super_agent')
        ag = User.objects.filter(Q(master_agent=root) | Q(super_agent__in=sa)).filter(user_type='agent')
        qs = User.objects.filter(
            Q(id=root.id) |
            Q(id__in=sa.values('id')) |
            Q(id__in=ag.values('id')) |
            Q(agent_id__in=ag.values('id'), user_type__in=['cashier', 'player'])
        ).distinct()

    if search_term:
        qs = qs.filter(
            Q(email__icontains=search_term) |
            Q(username__icontains=search_term) |
            Q(phone_number__icontains=search_term) |
            Q(first_name__icontains=search_term) |
            Q(last_name__icontains=search_term) |
            Q(other_name__icontains=search_term) |
            Q(cashier_prefix__icontains=search_term)
        )

    qs = qs.order_by('email')
    paginator = Paginator(qs, 20)
    try:
        users_page = paginator.page(page)
    except PageNotAnInteger:
        users_page = paginator.page(1)
    except EmptyPage:
        users_page = paginator.page(paginator.num_pages)

    id_list = [u.id for u in users_page]
    wallet_map = {row["user_id"]: row["balance"] for row in Wallet.objects.filter(user_id__in=id_list).values("user_id", "balance")}

    results = []
    for u in users_page:
        label = u.get_full_name() or u.email
        if u.username:
            label = f"{label} @{u.username}"
        text = f"{label} ({u.get_user_type_display()}) - {u.email}"
        if u.user_type == 'cashier' and u.cashier_prefix:
            text = f"{u.cashier_prefix} - {text}"
        results.append({'id': u.id, 'text': text, 'balance': float(wallet_map.get(u.id) or 0)})

    return JsonResponse({'results': results, 'pagination': {'more': users_page.has_next()}})

@login_required
@user_passes_test(is_crm_user)
@db_transaction.atomic
def crm_withdrawal_action(request, withdrawal_id):
    if not crm_can_approve_withdrawals(request.user):
        messages.error(request, 'Not allowed.')
        return redirect('betting:crm_dashboard')

    withdrawal_request = get_object_or_404(UserWithdrawal, id=withdrawal_id)
    if withdrawal_request.status != 'pending':
        messages.warning(request, f"This withdrawal request has already been {withdrawal_request.status}.")
        return redirect('betting:crm_dashboard')

    if request.method != 'POST':
        messages.error(request, 'Invalid request.')
        return redirect('betting:crm_dashboard')

    form = CRMWithdrawalDecisionForm(request.POST)
    if not form.is_valid():
        messages.error(request, 'Invalid form.')
        return redirect('betting:crm_dashboard')

    action = form.cleaned_data['action']
    reason = form.cleaned_data.get('reason') or ''
    notes = form.cleaned_data.get('notes') or ''

    if action == 'approve':
        withdrawal_request.status = 'approved'
        try:
            user_wallet = Wallet.objects.get(user=withdrawal_request.user)
            withdrawal_request.balance_after = user_wallet.balance
            withdrawal_request.balance_before = user_wallet.balance + withdrawal_request.amount
        except Wallet.DoesNotExist:
            pass
        withdrawal_request.processed_ip = request.META.get('REMOTE_ADDR')
        withdrawal_request.approved_rejected_by = request.user
        withdrawal_request.approved_rejected_time = timezone.now()
        withdrawal_request.admin_notes = reason
        withdrawal_request.save()

        CRMActionLog.objects.create(
            actor=request.user,
            target_user=withdrawal_request.user,
            action_type='WITHDRAWAL_APPROVED',
            reason=reason,
            notes=notes,
            withdrawal=withdrawal_request,
            data={'amount': str(withdrawal_request.amount)},
        )
        messages.success(request, f"Withdrawal {withdrawal_id} approved.")

    elif action == 'reject':
        withdrawal_request.status = 'rejected'
        withdrawal_request._skip_signal_refund = True
        withdrawal_request.processed_ip = request.META.get('REMOTE_ADDR')
        withdrawal_request.approved_rejected_by = request.user
        withdrawal_request.approved_rejected_time = timezone.now()
        withdrawal_request.admin_notes = reason

        user_wallet = get_object_or_404(Wallet.objects.select_for_update(), user=withdrawal_request.user)
        refund_tx = Transaction.objects.create(
            user=withdrawal_request.user,
            initiating_user=request.user,
            target_user=withdrawal_request.user,
            transaction_type='withdrawal_refund',
            amount=withdrawal_request.amount,
            is_successful=True,
            status='completed',
            description=f"Refund for rejected withdrawal request {withdrawal_id}. Reason: {reason or 'No reason provided.'}",
            timestamp=timezone.now()
        )
        user_wallet.apply_delta(
            amount=withdrawal_request.amount,
            actor=request.user,
            transaction_obj=refund_tx,
            reference=str(withdrawal_request.id),
            reason=refund_tx.description,
            metadata={"withdrawal_id": withdrawal_request.id, "source": "crm_reject"},
        )
        withdrawal_request.save()

        CRMActionLog.objects.create(
            actor=request.user,
            target_user=withdrawal_request.user,
            action_type='WITHDRAWAL_REJECTED',
            reason=reason,
            notes=notes,
            withdrawal=withdrawal_request,
            data={'amount': str(withdrawal_request.amount)},
        )
        messages.info(request, f"Withdrawal {withdrawal_id} rejected and refunded.")

    return redirect(request.META.get('HTTP_REFERER') or reverse('betting:crm_dashboard'))

@login_required
@user_passes_test(is_crm_user)
@db_transaction.atomic
def crm_cashier_registration_action(request, pk, action):
    if not crm_can_approve_registrations(request.user):
        messages.error(request, 'Not allowed.')
        return redirect('betting:crm_dashboard')

    cashier_req = get_object_or_404(CashierRegistrationRequest, pk=pk)
    if cashier_req.status != 'PENDING':
        messages.warning(request, 'This cashier registration is not pending.')
        return redirect('betting:crm_dashboard')

    if request.method != 'POST':
        messages.error(request, 'Invalid request.')
        return redirect('betting:crm_dashboard')

    if action == 'approve':
        agent = cashier_req.agent
        if not agent:
            messages.error(request, 'This request has no agent attached.')
            return redirect('betting:crm_dashboard')

        raw_password = get_random_string(12)
        cashier = User.objects.create_user(
            email=normalize_email_value(cashier_req.cashier_email) or normalize_email_value(agent.email),
            password=raw_password,
            username=cashier_req.cashier_username,
            first_name=cashier_req.first_name,
            last_name=cashier_req.last_name,
            other_name=cashier_req.other_name,
            phone_number=cashier_req.phone_number,
            state=agent.state,
            user_type='cashier',
            agent=agent,
            master_agent=agent.master_agent,
            super_agent=agent.super_agent,
            cashier_prefix=cashier_req.cashier_prefix,
            is_active=True
        )
        Wallet.objects.get_or_create(user=cashier)

        cashier_req.created_cashier = cashier
        cashier_req.status = 'APPROVED'
        cashier_req.reviewed_at = timezone.now()
        cashier_req.admin_notes = None
        cashier_req.save(update_fields=['created_cashier', 'status', 'reviewed_at', 'admin_notes'])

        login_url = request.build_absolute_uri('/login/')
        message = (
            f"A new cashier registration has been approved for your shop.\n\n"
            f"Cashier Code: {cashier_req.cashier_code}\n"
            f"Cashier Email: {cashier_req.cashier_email}\n"
            f"Cashier Username: {cashier_req.cashier_username}\n"
            f"Cashier Prefix: {cashier_req.cashier_prefix or ''}\n"
            f"Password: {raw_password}\n\n"
            f"Login: {login_url}\n"
        )
        send_mail(
            subject='Cashier Registration Approved',
            message=message,
            from_email=settings.DEFAULT_FROM_EMAIL or settings.EMAIL_HOST_USER,
            recipient_list=[agent.email],
            fail_silently=True
        )

        CRMActionLog.objects.create(
            actor=request.user,
            target_user=agent,
            action_type='CASHIER_REG_APPROVED',
            cashier_request=cashier_req,
            data={'cashier_id': cashier.id, 'cashier_email': cashier.email},
        )
        messages.success(request, f"Cashier {cashier_req.cashier_email} approved and created.")

    elif action == 'reject':
        cashier_req.status = 'REJECTED'
        cashier_req.reviewed_at = timezone.now()
        cashier_req.admin_notes = 'Rejected by CRM.'
        cashier_req.save(update_fields=['status', 'reviewed_at', 'admin_notes'])
        CRMActionLog.objects.create(
            actor=request.user,
            target_user=cashier_req.agent,
            action_type='CASHIER_REG_REJECTED',
            cashier_request=cashier_req,
        )
        messages.success(request, 'Cashier registration rejected.')

    return redirect(request.META.get('HTTP_REFERER') or reverse('betting:crm_dashboard'))

@login_required
@user_passes_test(is_crm_user)
@db_transaction.atomic
def crm_agent_registration_action(request, pk, action):
    if not crm_can_approve_registrations(request.user):
        messages.error(request, 'Not allowed.')
        return redirect('betting:crm_dashboard')

    pending_reg = get_object_or_404(PendingAgentRegistration, pk=pk)
    if pending_reg.status != 'PENDING':
        messages.warning(request, 'This agent registration is not pending.')
        return redirect('betting:crm_dashboard')

    if request.method != 'POST':
        messages.error(request, 'Invalid request.')
        return redirect('betting:crm_dashboard')

    if action == 'approve':
        raw_password = get_random_string(12)
        created_user = None
        cashier_accounts = []

        if pending_reg.user_type == 'agent':
            state_obj = None
            if pending_reg.state:
                state_obj = State.objects.filter(state_name__iexact=pending_reg.state).first()
                if not state_obj:
                    state_obj = State.objects.filter(abbreviation__iexact=pending_reg.state).first()
            if not state_obj:
                state_obj = State.objects.first()

            full_name = (pending_reg.full_name or '').strip()
            parts = full_name.split()
            first_name = parts[0] if parts else ''
            last_name = parts[1] if len(parts) > 1 else ''
            other_name = ' '.join(parts[2:]) if len(parts) > 2 else ''

            agent, cashiers, _ = create_agent_and_cashiers(
                User,
                email=pending_reg.email,
                password=raw_password,
                first_name=first_name,
                last_name=last_name,
                other_name=other_name or 'Agent',
                state=state_obj,
                master_agent=pending_reg.master_agent,
                super_agent=pending_reg.super_agent,
                phone_number=pending_reg.phone,
                shop_address=pending_reg.state,
            )
            created_user = agent
            cashier_accounts = list(cashiers or [])
        else:
            local = (pending_reg.email or '').split("@")[0]
            local = re.sub(r"[^A-Za-z0-9]", "", local)[:20] or "User"
            candidate = local[:1].upper() + local[1:].lower()
            suffix = 1
            while User.objects.filter(username__iexact=candidate).exists():
                candidate = f"{local}{suffix}"
                suffix += 1

            full_name = (pending_reg.full_name or '').strip()
            parts = full_name.split()
            first_name = parts[0] if parts else ''
            last_name = ' '.join(parts[1:]) if len(parts) > 1 else ''
            created_user = User.objects.create_user(
                email=pending_reg.email,
                password=raw_password,
                username=candidate,
                first_name=first_name,
                last_name=last_name,
                other_name='',
                phone_number=pending_reg.phone,
                state=None,
                shop_address=pending_reg.state,
                user_type=pending_reg.user_type,
                master_agent=pending_reg.master_agent,
                super_agent=pending_reg.super_agent,
                is_active=True
            )
            Wallet.objects.get_or_create(user=created_user, defaults={'balance': Decimal('0.00')})

        pending_reg.status = 'APPROVED'
        pending_reg.reviewed_at = timezone.now()
        pending_reg.save(update_fields=['status', 'reviewed_at'])

        login_url = request.build_absolute_uri('/login/')
        html_message = render_to_string('pending_registration/email/agent_approved.html', {
            'user': created_user,
            'cashier_accounts': cashier_accounts,
            'login_url': login_url,
            'password': raw_password,
        })
        try:
            send_mail(
                subject='Pool Betting Agent Registration Approved',
                message=strip_tags(html_message),
                from_email=settings.DEFAULT_FROM_EMAIL or settings.EMAIL_HOST_USER,
                recipient_list=[created_user.email],
                html_message=html_message,
                fail_silently=False
            )
        except Exception:
            pass

        CRMActionLog.objects.create(
            actor=request.user,
            target_user=created_user,
            action_type='AGENT_REG_APPROVED',
            pending_agent_registration=pending_reg,
            data={'created_user_id': created_user.id},
        )
        messages.success(request, f"Registration approved for {created_user.email}.")

    elif action == 'reject':
        reason = (request.POST.get('reason') or '').strip()
        pending_reg.status = 'REJECTED'
        pending_reg.admin_notes = reason
        pending_reg.reviewed_at = timezone.now()
        pending_reg.save(update_fields=['status', 'admin_notes', 'reviewed_at'])

        try:
            html_message = render_to_string('pending_registration/email/agent_rejected.html', {
                'pending_reg': pending_reg,
                'reason': reason
            })
            send_mail(
                subject='Pool Betting Agent Registration Rejected',
                message=strip_tags(html_message),
                from_email=settings.DEFAULT_FROM_EMAIL or settings.EMAIL_HOST_USER,
                recipient_list=[pending_reg.email],
                html_message=html_message,
                fail_silently=False
            )
        except Exception:
            pass

        CRMActionLog.objects.create(
            actor=request.user,
            action_type='AGENT_REG_REJECTED',
            pending_agent_registration=pending_reg,
            reason=reason,
        )
        messages.info(request, 'Registration rejected.')

    return redirect(request.META.get('HTTP_REFERER') or reverse('betting:crm_dashboard'))


@login_required
@user_passes_test(lambda u: u.is_superuser)
def super_admin_fund_account_user(request):
    if request.method == 'POST':
        form = SuperAdminFundAccountUserForm(request.POST)
        if form.is_valid():
            account_user = form.cleaned_data['account_user']
            action = form.cleaned_data['action']
            amount = form.cleaned_data['amount']
            description = form.cleaned_data['description']
            
            try:
                with db_transaction.atomic():
                    wallet = Wallet.objects.select_for_update().get(user=account_user)
                    
                    if action == 'credit':
                        tx_type = 'account_user_credit'
                    else: # debit
                        if wallet.balance < amount:
                            raise InvalidOperation("Account User has insufficient funds.")
                        tx_type = 'account_user_debit'

                    tx = Transaction.objects.create(
                        user=account_user,
                        initiating_user=request.user,
                        transaction_type=tx_type,
                        amount=amount,
                        status='completed',
                        is_successful=True,
                        description=f"Super Admin Action ({action}): {description}"
                    )
                    wallet.apply_delta(
                        amount=(amount if action == "credit" else -amount),
                        actor=request.user,
                        transaction_obj=tx,
                        reference=str(tx.id),
                        reason=tx.description,
                        metadata={"source": "super_admin_fund_account_user", "action": action},
                    )
                    
                    messages.success(request, f"Successfully {action}ed {amount} for {account_user.email}.")
                    return redirect('betting:super_admin_fund_account_user')
            except InvalidOperation as e:
                messages.error(request, str(e))
            except Exception as e:
                messages.error(request, f"Error: {str(e)}")
    else:
        form = SuperAdminFundAccountUserForm()
    
    recent_transactions = Transaction.objects.filter(
        user__user_type='account_user',
        initiating_user=request.user
    ).order_by('-timestamp')[:20]

    return render(request, 'betting/super_admin_fund_account_user.html', {
        'form': form,
        'recent_transactions': recent_transactions
    })


@login_required
def impersonate_user(request, user_id):
    # Permission check
    if not (request.user.is_superuser or request.user.has_perm('betting.can_impersonate_users')):
        messages.error(request, "You do not have permission to impersonate users.")
        # Try to redirect to betting_admin dashboard, else fallback
        try:
            return redirect('betting_admin:dashboard')
        except:
            return redirect('/')

    target_user = get_object_or_404(User, pk=user_id)

    # Security Checks
    if target_user.is_superuser:
        messages.error(request, "Cannot impersonate a superuser.")
        return redirect(request.META.get('HTTP_REFERER', '/'))
    
    if request.session.get('impersonation_active'):
        messages.error(request, "Nested impersonation is not allowed.")
        return redirect(request.META.get('HTTP_REFERER', '/'))

    # Create Log
    log = ImpersonationLog.objects.create(
        admin_user=request.user,
        target_user=target_user,
        ip_address=request.META.get('REMOTE_ADDR'),
        user_agent=request.META.get('HTTP_USER_AGENT', ''),
        started_at=timezone.now()
    )

    # Capture values to persist across session flush
    original_admin_id = request.user.pk
    impersonation_started_at = str(timezone.now())
    impersonated_user_id = target_user.pk
    log_id = log.pk

    # Switch User (without password)
    # We need to set the backend manually because we are logging in without authentication
    backend = 'betting.backends.EmailOrUsernameBackend'
    target_user.backend = backend
    login(request, target_user)

    # RESTORE session details after login flush
    request.session['original_admin_id'] = original_admin_id
    request.session['impersonation_started_at'] = impersonation_started_at
    request.session['impersonation_active'] = True
    request.session['impersonated_user_id'] = impersonated_user_id
    request.session['impersonation_log_id'] = log_id

    messages.success(request, f"Now impersonating {target_user.email}")
    
    # Redirect based on user type
    if target_user.user_type == 'account_user':
        return redirect('betting:account_user_dashboard')
    elif target_user.user_type == 'agent':
        return redirect('betting:agent_dashboard')
    elif target_user.user_type == 'master_agent':
        return redirect('betting:master_agent_dashboard')
    elif target_user.user_type == 'super_agent':
        return redirect('betting:super_agent_dashboard')
    else:
        return redirect('betting:user_dashboard')


@login_required
def stop_impersonation(request):
    if not request.session.get('impersonation_active'):
        return redirect('/')

    original_admin_id = request.session.get('original_admin_id')
    log_id = request.session.get('impersonation_log_id')

    if not original_admin_id:
        logout(request)
        return redirect('/admin/login/')

    try:
        original_user = User.objects.get(pk=original_admin_id)
        # Re-login as admin
        backend = 'betting.backends.EmailOrUsernameBackend'
        original_user.backend = backend
        login(request, original_user)
        
        # Update Log
        if log_id:
            try:
                log = ImpersonationLog.objects.get(pk=log_id)
                log.ended_at = timezone.now()
                log.duration = log.ended_at - log.started_at
                log.termination_reason = "Manual Exit"
                log.save()
            except ImpersonationLog.DoesNotExist:
                pass

        # Clear session keys
        keys_to_pop = ['impersonation_active', 'original_admin_id', 'impersonation_started_at', 'impersonation_log_id', 'impersonated_user_id']
        for key in keys_to_pop:
            request.session.pop(key, None)

        messages.info(request, "Impersonation ended. Welcome back.")
        return redirect('betting_admin:dashboard')

    except User.DoesNotExist:
        logout(request)
        return redirect('/admin/login/')

@login_required
def api_betting_limits(request):
    ticket_type = request.GET.get('ticket_type') or request.GET.get('bet_type')
    limits = get_effective_betting_limits_for_user(request.user, ticket_type=ticket_type)
    return JsonResponse({'success': True, 'limits': serialize_limits(limits)})

@login_required
def api_downline_search(request):
    """
    API endpoint for searching downline users for Wallet Transfer.
    Filters based on logged-in user's role.
    """
    search_term = request.GET.get('q', '')
    page = request.GET.get('page', 1)
    
    user = request.user
    queryset = User.objects.none()

    if user.user_type == 'agent':
        # Agents see their Cashiers and Players
        queryset = User.objects.filter(
            Q(agent=user) & 
            Q(user_type__in=['cashier', 'player'])
        )
    elif user.user_type == 'super_agent':
        # Super Agents see their direct Agents only
        queryset = User.objects.filter(
            super_agent=user,
            user_type='agent'
        )
    elif user.user_type == 'master_agent':
        # Master Agents see Super Agents or Agents (depending on hierarchy)
        # Check both direct and indirect (via Super Agent)
        queryset = User.objects.filter(
            Q(master_agent=user) |
            Q(super_agent__master_agent=user)
        ).filter(user_type__in=['super_agent', 'agent']).distinct()
        
    elif user.user_type == 'account_user':
        # Account Users can see Master Agents, Super Agents, Agents, Cashiers
        queryset = User.objects.filter(
            user_type__in=['master_agent', 'super_agent', 'agent', 'cashier']
        )
    
    # Apply search filter
    if search_term:
        queryset = queryset.filter(
            Q(email__icontains=search_term) | 
            Q(first_name__icontains=search_term) | 
            Q(last_name__icontains=search_term) |
            Q(cashier_prefix__icontains=search_term)
        )
        
    # Ordering
    queryset = queryset.order_by('email')
    
    # Pagination
    paginator = Paginator(queryset, 20)
    try:
        users_page = paginator.page(page)
    except PageNotAnInteger:
        users_page = paginator.page(1)
    except EmptyPage:
        users_page = paginator.page(paginator.num_pages)
        
    results = []
    id_list = [u.id for u in users_page]
    wallet_map = {
        row["user_id"]: row["balance"]
        for row in Wallet.objects.filter(user_id__in=id_list).values("user_id", "balance")
    }
    for u in users_page:
        role_label = u.get_user_type_display()
        name_str = u.get_full_name()
        if not name_str:
             name_str = u.first_name if u.first_name else ""
        
        display_name = name_str
        if u.user_type == 'cashier' and u.username:
            display_name = u.username

        display_text = f"{display_name} ({role_label}) - {u.email}"
        if u.user_type == 'cashier' and u.cashier_prefix:
            display_text = f"{u.cashier_prefix} - {display_text}"
            
        results.append({
            'id': u.id,
            'text': display_text,
            'balance': float(wallet_map.get(u.id) or 0),
        })
        
    return JsonResponse({
        'results': results,
        'pagination': {
            'more': users_page.has_next()
        }
    })


@login_required
def api_downline_wallet_balance(request):
    user = request.user
    try:
        target_id = int(request.GET.get("user_id", "0"))
    except (TypeError, ValueError):
        target_id = 0

    if not target_id:
        return JsonResponse({"success": False, "message": "Invalid user."}, status=400)

    qs = User.objects.none()
    if user.user_type == 'agent':
        qs = User.objects.filter(Q(agent=user) & Q(user_type__in=['cashier', 'player']))
    elif user.user_type == 'super_agent':
        qs = User.objects.filter(super_agent=user, user_type='agent')
    elif user.user_type == 'master_agent':
        qs = User.objects.filter(
            Q(master_agent=user) | Q(super_agent__master_agent=user)
        ).filter(user_type__in=['super_agent', 'agent']).distinct()
    elif user.user_type == 'account_user':
        qs = User.objects.filter(user_type__in=['master_agent', 'super_agent', 'agent', 'cashier'])

    if not qs.filter(id=target_id).exists():
        return JsonResponse({"success": False, "message": "Not authorized."}, status=403)

    balance = Wallet.objects.filter(user_id=target_id).values_list("balance", flat=True).first()
    balance = balance or Decimal("0.00")
    return JsonResponse({"success": True, "balance": float(balance)})


@login_required
@user_passes_test(lambda u: u.is_superuser or u.user_type in ['admin', 'account_user'])
def api_admin_user_search(request):
    """
    API endpoint for Admin to search ANY user (excluding superusers).
    Used for Manual Wallet Manager autocomplete.
    """
    search_term = request.GET.get('q', '')
    page = request.GET.get('page', 1)
    
    # Start with all users except superusers
    queryset = User.objects.exclude(is_superuser=True)
    
    # If account_user, exclude other account_users to match dashboard logic
    if request.user.user_type == 'account_user':
        queryset = queryset.exclude(user_type='account_user')
    
    # Apply search filter
    if search_term:
        qs_filter = (
            Q(email__icontains=search_term) |
            Q(phone_number__icontains=search_term) |
            Q(username__icontains=search_term) |
            Q(first_name__icontains=search_term) |
            Q(last_name__icontains=search_term) |
            Q(other_name__icontains=search_term)
        )
        queryset = queryset.filter(qs_filter)
        if search_term.isdigit():
            extra = User.objects.filter(id=int(search_term)).exclude(is_superuser=True)
            if request.user.user_type == 'account_user':
                extra = extra.exclude(user_type='account_user')
            queryset = (queryset | extra).distinct()
    
    # Pagination
    paginator = Paginator(queryset.order_by('email'), 20)
    
    try:
        users_page = paginator.page(page)
    except PageNotAnInteger:
        users_page = paginator.page(1)
    except EmptyPage:
        users_page = paginator.page(paginator.num_pages)

    results = []
    for u in users_page:
        text = f"{u.get_full_name()} ({u.email})"
        if u.username:
            text += f" @{u.username}"
        if u.phone_number:
            text += f" - {u.phone_number}"
        text += f" [{u.get_user_type_display()}]"
            
        results.append({
            'id': u.id,
            'text': text
        })

    return JsonResponse({
        'results': results,
        'pagination': {
            'more': users_page.has_next()
        }
    })



@login_required
def get_ticket_details_json(request):
    ticket_id = request.GET.get('ticket_id')
    mode = request.GET.get('mode', 'rebet') # 'rebet' or 'reprint'

    if not ticket_id:
        return JsonResponse({'success': False, 'message': 'Ticket ID is required'}, status=400)

    # Permission check: Cashier, Agent, Admin
    if not (request.user.user_type in ['cashier', 'agent', 'admin'] or request.user.is_superuser):
        return JsonResponse({'success': False, 'message': 'Unauthorized'}, status=403)

    try:
        ticket = BetTicket.objects.get(ticket_id=ticket_id)
    except BetTicket.DoesNotExist:
        return JsonResponse({'success': False, 'message': 'Ticket not found'}, status=404)

    selections_data = []
    seen_fixture_ids = set()
    for sel in ticket.selections.select_related('fixture', 'fixture__betting_period').all():
        fixture = sel.fixture
        if mode == 'rebet':
            fixture_key = str(getattr(fixture, 'id', '') or '')
            if fixture_key and fixture_key in seen_fixture_ids:
                continue
            if fixture_key:
                seen_fixture_ids.add(fixture_key)
        
        # Determine Odd Value
        odd_value = sel.odd_selected # Default to stored odd for reprint
        is_active = True
        
        if mode == 'rebet':
            # Use current odd from fixture
            current_odd = None
            bt = sel.bet_type
            
            # Map bet_type to fixture field
            if bt == 'home_win': current_odd = getattr(fixture, 'home_win_odd', None)
            elif bt == 'draw': current_odd = getattr(fixture, 'draw_odd', None)
            elif bt == 'away_win': current_odd = getattr(fixture, 'away_win_odd', None)
            elif bt == 'home_dnb': current_odd = getattr(fixture, 'home_dnb_odd', None)
            elif bt == 'away_dnb': current_odd = getattr(fixture, 'away_dnb_odd', None)
            elif bt == 'over_1_5': current_odd = getattr(fixture, 'over_1_5_odd', None)
            elif bt == 'under_1_5': current_odd = getattr(fixture, 'under_1_5_odd', None)
            elif bt == 'over_2_5': current_odd = getattr(fixture, 'over_2_5_odd', None)
            elif bt == 'under_2_5': current_odd = getattr(fixture, 'under_2_5_odd', None)
            elif bt == 'over_3_5': current_odd = getattr(fixture, 'over_3_5_odd', None)
            elif bt == 'under_3_5': current_odd = getattr(fixture, 'under_3_5_odd', None)
            elif bt == 'btts_yes': current_odd = getattr(fixture, 'btts_yes_odd', None)
            elif bt == 'btts_no': current_odd = getattr(fixture, 'btts_no_odd', None)
            
            if current_odd is not None:
                odd_value = current_odd
            
            # Check if fixture is bettable
            if fixture.status != 'scheduled' or not fixture.is_active:
                is_active = False
                
        selections_data.append({
            'fixture_id': str(fixture.id),
            'fixture_home_team': fixture.home_team,
            'fixture_away_team': fixture.away_team,
            'fixture_match_date': fixture.match_date.strftime('%Y-%m-%d'),
            'fixture_match_time': fixture.match_time.strftime('%H:%M'),
            'bet_type': sel.bet_type,
            'bet_type_display': sel.bet_type.replace('_', ' ').title(),
            'odd': float(odd_value) if odd_value else 1.0,
            'fixture_period_name': fixture.betting_period.name if fixture.betting_period else '',
            'is_active': is_active
        })

    is_voided = ticket.status in ['cancelled', 'deleted', 'voided']

    data = {
        'ticket_id': ticket.ticket_id,
        'placed_at': ticket.placed_at.strftime('%Y-%m-%d %H:%M'),
        'stake_amount': 0.0 if is_voided else float(ticket.stake_amount),
        'total_odd': 0.0 if is_voided else float(ticket.total_odd),
        'max_winning': 0.0 if is_voided else float(ticket.max_winning),
        'potential_winning': 0.0 if is_voided else float(ticket.potential_winning),
        'bonus_percentage_applied': 0.0 if is_voided else float(ticket.bonus_percentage_applied),
        'bonus_base': ticket.bonus_base,
        'bonus_base_amount': 0.0 if is_voided else float(ticket.bonus_base_amount),
        'bonus_amount': 0.0 if is_voided else float(ticket.bonus_amount),
        'bonus_is_final': False if is_voided else bool(ticket.bonus_is_final),
        'bonus_applied_at': None if is_voided or not ticket.bonus_applied_at else ticket.bonus_applied_at.strftime('%Y-%m-%d %H:%M:%S'),
        'original_selections_count': ticket.original_selections_count or ticket.selections.count(),
        'selections': selections_data,
        'status': ticket.get_status_display(),
        'status_code': ticket.status,
        'bet_type': ticket.bet_type,
        'system_min_count': ticket.system_min_count
    }
    
    return JsonResponse({'success': True, 'ticket': data})

@login_required
def log_ticket_reprint(request):
    if request.method != 'POST':
        return JsonResponse({'success': False, 'message': 'Invalid method'}, status=405)
        
    ticket_id = request.POST.get('ticket_id')
    if not ticket_id:
         return JsonResponse({'success': False, 'message': 'Ticket ID required'}, status=400)
         
    try:
        ticket = BetTicket.objects.get(ticket_id=ticket_id)
        
        # Log activity
        ActivityLog.objects.create(
            user=request.user,
            action_type='REPRINT',
            action=f"Reprinted ticket {ticket_id}",
            affected_object=f"BetTicket: {ticket_id}",
            ip_address=request.META.get('REMOTE_ADDR'),
            path=request.path
        )
        
        return JsonResponse({'success': True})
    except BetTicket.DoesNotExist:
        return JsonResponse({'success': False, 'message': 'Ticket not found'}, status=404)

# WebAuthn Views

import json
import base64
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django.contrib.auth.decorators import login_required
from django.contrib.auth import login
from .webauthn_utils import WebAuthnUtils
from .models import BiometricAuthLog
from fido2.utils import websafe_encode, websafe_decode
from django.core.cache import cache

class BytesEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, bytes):
            return websafe_encode(obj)
        return super().default(obj)

def _client_ip(request):
    xff = request.META.get('HTTP_X_FORWARDED_FOR')
    if xff:
        return xff.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR') or 'unknown'

def _rate_limited(key, limit=5, window=60):
    count = cache.get(key)
    if count is None:
        cache.set(key, 1, window)
        return False
    if count >= limit:
        return True
    try:
        cache.incr(key)
    except:
        cache.set(key, count + 1, window)
    return False

@login_required
@require_POST
def webauthn_register_begin(request):
    rp_id = request.get_host().split(':')[0]
    utils = WebAuthnUtils(rp_id=rp_id)
    try:
        rl_key = f"webauthn:reg:{_client_ip(request)}:{request.user.id}"
        if _rate_limited(rl_key):
            return JsonResponse({'status': 'error', 'message': 'Too many requests'}, status=429)
        options, state = utils.register_begin(request.user)
        request.session['webauthn_reg_state'] = state
        return JsonResponse(dict(options.public_key), encoder=BytesEncoder)
    except Exception as e:
        return JsonResponse({'status': 'error', 'message': str(e)}, status=400)

@never_cache
@ensure_csrf_cookie
@login_required
@user_passes_test(lambda u: u.is_superuser or u.user_type == 'admin')
def admin_manual_wallet_manager(request):
    search_form = AccountUserSearchForm()
    action_form = AdminManualWalletForm()
    found_user = None
    search_results = None

    if request.method == 'POST':
        if 'search_user' in request.POST:
            search_form = AccountUserSearchForm(request.POST)
            if search_form.is_valid():
                search_term = search_form.cleaned_data['search_term']
                # Search for any user except superuser
                users = User.objects.filter(
                    Q(email__icontains=search_term) | 
                    Q(phone_number__icontains=search_term) |
                    Q(first_name__icontains=search_term) |
                    Q(last_name__icontains=search_term)
                ).exclude(is_superuser=True)
                
                if search_term.isdigit():
                     # Prioritize exact ID match if search term is a digit (likely from autocomplete)
                     exact_match = User.objects.filter(id=int(search_term)).exclude(is_superuser=True).first()
                     if exact_match:
                         found_user = exact_match
                         users = User.objects.filter(pk=exact_match.pk)
                     else:
                         users = users | User.objects.filter(id=int(search_term)).exclude(is_superuser=True)
                
                if found_user:
                     pass # Already found via exact ID match
                elif users.count() == 1:
                    found_user = users.first()
                    messages.success(request, f"User found: {found_user.get_full_name()} ({found_user.email})")
                elif users.count() > 1:
                    search_results = users
                    messages.warning(request, "Multiple users found. Please select one.")
                else:
                    messages.error(request, "No user found.")

        elif 'perform_action' in request.POST:
            action_form = AdminManualWalletForm(request.POST)
            target_user_id = request.POST.get('target_user_id')
            if target_user_id:
                target_user = get_object_or_404(User, id=target_user_id)
                
                if target_user.is_superuser:
                    messages.error(request, "Operation not allowed on superusers.")
                    return redirect('betting_admin:admin_manual_wallet_manager')

                if action_form.is_valid():
                    action = action_form.cleaned_data['action']
                    amount = action_form.cleaned_data['amount']
                    description = action_form.cleaned_data['description']
                    is_overdraft_loan = action_form.cleaned_data.get('is_overdraft_loan')
                    
                    try:
                        with db_transaction.atomic():
                            target_wallet = Wallet.objects.select_for_update().get(user=target_user)

                            if action == 'credit' and is_overdraft_loan:
                                loan = create_manual_overdraft(
                                    actor=request.user,
                                    borrower=target_user,
                                    amount=amount,
                                    reason=description,
                                    ip_address=get_client_ip(request),
                                )
                                log_admin_activity(
                                    request,
                                    f"Manual overdraft assignment of {amount} for {target_user.email}. Reason: {description}",
                                    action_type="MANUAL_OVERDRAFT",
                                    affected_object=target_user.email,
                                )
                                messages.success(
                                    request,
                                    f"Manual overdraft of ₦{amount} assigned to {target_user.email}. "
                                    f"Loan #{loan.id} is now active and the wallet balance has been increased."
                                )
                                return redirect('betting_admin:admin_manual_wallet_manager')

                            if action == 'credit':
                                tx_type = 'manual_credit'
                            elif action == 'debit':
                                if target_wallet.balance < amount:
                                    raise InvalidOperation("User has insufficient funds.")
                                tx_type = 'manual_debit'
                            
                            tx = Transaction.objects.create(
                                user=target_user,
                                initiating_user=request.user,
                                transaction_type=tx_type,
                                amount=amount,
                                status='completed',
                                is_successful=True,
                                description=f"Admin Manual {action.title()}: {description}"
                            )
                            if action == "credit":
                                repayment_result = apply_repayment_and_credit_wallet(
                                    user=target_user,
                                    amount=amount,
                                    source="admin_credit",
                                    actor=request.user,
                                    transaction_obj=tx,
                                    reference=str(tx.id),
                                    reason=tx.description,
                                    metadata={"source": "admin_manual_wallet_manager", "action": action},
                                )
                            else:
                                target_wallet.apply_delta(
                                    amount=-amount,
                                    actor=request.user,
                                    transaction_obj=tx,
                                    reference=str(tx.id),
                                    reason=tx.description,
                                    metadata={"source": "admin_manual_wallet_manager", "action": action},
                                )
                            
                            log_admin_activity(
                                request, 
                                f"Manual {action} of {amount} for {target_user.email}. Reason: {description}",
                                action_type=f"MANUAL_{action.upper()}",
                                affected_object=target_user.email
                            )
                            if action == "credit":
                                repaid_amount = repayment_result.get("repaid_amount") or Decimal("0.00")
                                wallet_credit_amount = repayment_result.get("wallet_credit_amount") or Decimal("0.00")
                                pending_credit_amount = repayment_result.get("pending_credit_amount") or Decimal("0.00")
                                messages.success(
                                    request,
                                    f"Successfully credited ₦{amount} for {target_user.email}. "
                                    f"Loan repayment: ₦{repaid_amount}. Wallet credit: ₦{wallet_credit_amount}. "
                                    f"Reserved new credit: ₦{pending_credit_amount}."
                                )
                            else:
                                messages.success(request, f"Successfully debited ₦{amount} for {target_user.email}.")
                            return redirect('betting_admin:admin_manual_wallet_manager')
                            
                    except LoanOverdraftError as e:
                        messages.error(request, str(e))
                    except InvalidOperation as e:
                        messages.error(request, str(e))
                    except Exception as e:
                        messages.error(request, f"An error occurred: {str(e)}")
            else:
                 messages.error(request, "Target user not specified.")

    # Get recent manual transactions (Admin and Account User)
    recent_transactions = Transaction.objects.filter(
        transaction_type__in=['manual_credit', 'manual_debit', 'account_user_credit', 'account_user_debit']
    ).select_related('user', 'initiating_user').order_by('-timestamp')[:20]

    context = {
        'search_form': search_form,
        'action_form': action_form,
        'found_user': found_user,
        'search_results': search_results,
        'recent_transactions': recent_transactions,
    }
    return render(request, 'betting/admin/manual_wallet_manager.html', context)

@login_required
@require_POST
def webauthn_register_complete(request):
    rp_id = request.get_host().split(':')[0]
    utils = WebAuthnUtils(rp_id=rp_id)
    try:
        data = json.loads(request.body)
        state = request.session.get('webauthn_reg_state')
        if not state:
            return JsonResponse({'status': 'error', 'message': 'No registration state found'}, status=400)
            
        device_name = data.get('device_name', 'Unknown Device')
        
        if 'id' in data:
            data['id'] = websafe_decode(data['id'])
        if 'rawId' in data:
            data['rawId'] = websafe_decode(data['rawId'])
        if 'response' in data:
            resp = data['response']
            if 'clientDataJSON' in resp:
                resp['clientDataJSON'] = websafe_decode(resp['clientDataJSON'])
            if 'attestationObject' in resp:
                resp['attestationObject'] = websafe_decode(resp['attestationObject'])
                
        utils.register_complete(state, data, request.user, device_name)
        
        BiometricAuthLog.objects.create(
            user=request.user,
            action='register',
            status='success',
            ip_address=request.META.get('REMOTE_ADDR'),
            device_name=device_name
        )
        
        del request.session['webauthn_reg_state']
        return JsonResponse({'status': 'success'})
    except Exception as e:
        BiometricAuthLog.objects.create(
            user=request.user,
            action='register',
            status='failed',
            ip_address=request.META.get('REMOTE_ADDR')
        )
        return JsonResponse({'status': 'error', 'message': str(e)}, status=400)

@require_POST
def webauthn_login_begin(request):
    try:
        data = json.loads(request.body)
        identifier = (data.get('username') or '').strip()

        if not identifier and (data.get('email') or '').strip():
            return JsonResponse({'status': 'error', 'message': 'Use your username to sign in. Email login is not supported.'}, status=400)
        
        rp_id = request.get_host().split(':')[0]
        utils = WebAuthnUtils(rp_id=rp_id)
        
        user = None
        if identifier:
            rl_key = f"webauthn:auth:{_client_ip(request)}:{identifier.lower()}"
            if _rate_limited(rl_key):
                return JsonResponse({'status': 'error', 'message': 'Too many requests'}, status=429)

            user = User.objects.filter(username__iexact=identifier).first()
            if not user:
                 return JsonResponse({'status': 'error', 'message': 'No user was found with that username.'}, status=404)
                 
            if user.user_type not in ['cashier', 'agent', 'super_agent', 'master_agent', 'admin']:
                 return JsonResponse({'status': 'error', 'message': 'Biometric login not enabled for this role'}, status=403)
                 
            request.session['webauthn_auth_user_id'] = user.id
        else:
            # Usernameless flow
            pass

        options, state = utils.authenticate_begin(user)
        request.session['webauthn_auth_state'] = state
        
        return JsonResponse(dict(options.public_key), encoder=BytesEncoder)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return JsonResponse({'status': 'error', 'message': str(e)}, status=400)

@require_POST
def webauthn_login_complete(request):
    rp_id = request.get_host().split(':')[0]
    utils = WebAuthnUtils(rp_id=rp_id)
    try:
        data = json.loads(request.body)
        state = request.session.get('webauthn_auth_state')
        
        # NOTE: In usernameless flow, user_id might be None
        user_id = request.session.get('webauthn_auth_user_id')
        
        if not state:
             return JsonResponse({'status': 'error', 'message': 'No authentication state found'}, status=400)
             
        user = None
        if user_id:
            try:
                user = User.objects.get(id=user_id)
            except User.DoesNotExist:
                pass
        
        # Decoding handled by fido2 library or manually here if needed.
        # Note: python-fido2 server.authenticate_complete expects decoded bytes for ids
        # But we pass the raw JSON data to utils.authenticate_complete? 
        # Wait, utils.authenticate_complete expects 'response_data' which is usually the JSON.
        # Let's check utils.authenticate_complete implementation again.
        # It calls self.server.authenticate_complete(state, creds_data, response_data).
        # Fido2Server.authenticate_complete expects response_data to be a ClientData object or dict.
        # If it's a dict, it should be the structure returned by navigator.credentials.get() (JSONified).
        # We don't need to manually decode here if we are passing the JSON structure that fido2 expects.
        # However, the previous code was manually decoding. Let's keep it consistent or let utils handle it.
        # Actually, let's look at the previous code: it was decoding 'id', 'rawId', 'clientDataJSON' etc.
        # If we remove that, it might break if utils expects decoded data.
        # But wait, the standard JSON from webauthn is base64url encoded.
        # python-fido2 helpers usually handle this if you use their helpers.
        # But let's stick to the manual decoding if that's what was working (or supposed to work).
        
        if 'id' in data:
            data['id'] = websafe_decode(data['id'])
        if 'rawId' in data:
            data['rawId'] = websafe_decode(data['rawId'])
        if 'response' in data:
            resp = data['response']
            if 'clientDataJSON' in resp:
                resp['clientDataJSON'] = websafe_decode(resp['clientDataJSON'])
            if 'authenticatorData' in resp:
                resp['authenticatorData'] = websafe_decode(resp['authenticatorData'])
            if 'signature' in resp:
                resp['signature'] = websafe_decode(resp['signature'])
            if 'userHandle' in resp and resp['userHandle']:
                resp['userHandle'] = websafe_decode(resp['userHandle'])

        # Now call utils.authenticate_complete which supports user=None
        cred = utils.authenticate_complete(state, data, user)
        
        if cred:
            # If user was None, get it from cred
            if not user:
                user = cred.user
                
            login(request, user, backend='betting.backends.EmailOrUsernameBackend')
            
            BiometricAuthLog.objects.create(
                user=user,
                action='login',
                status='success',
                ip_address=request.META.get('REMOTE_ADDR'),
                device_name=cred.device_name
            )
            
            if 'webauthn_auth_state' in request.session:
                del request.session['webauthn_auth_state']
            if 'webauthn_auth_user_id' in request.session:
                del request.session['webauthn_auth_user_id']
                
            # Determine redirect URL based on user type (similar to login view)
            redirect_url = '/dashboard/'
            if user.user_type == 'agent':
                redirect_url = '/agent/dashboard/'
            elif user.user_type == 'master_agent':
                redirect_url = '/master-agent/dashboard/'
            elif user.user_type == 'super_agent':
                redirect_url = '/super-agent/dashboard/'
            elif user.user_type == 'account_user':
                redirect_url = '/account-user/dashboard/'
            elif user.user_type == 'admin':
                redirect_url = '/admin/'
                
            return JsonResponse({'status': 'success', 'redirect_url': redirect_url})
        else:
            raise ValueError("Authentication failed")

    except Exception as e:
        import traceback
        traceback.print_exc()
        # Try to log failure if we can identify the user
        target_user = None
        if 'user' in locals() and user:
            target_user = user
        elif request.session.get('webauthn_auth_user_id'):
             try:
                target_user = User.objects.get(id=request.session.get('webauthn_auth_user_id'))
             except:
                 pass
        
        if target_user:
             try:
                BiometricAuthLog.objects.create(
                    user=target_user,
                    action='login',
                    status='failed',
                    ip_address=request.META.get('REMOTE_ADDR'),
                    details=str(e)
                )
             except:
                 pass
                 
        return JsonResponse({'status': 'error', 'message': str(e)}, status=400)
