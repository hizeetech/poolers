from django.contrib import admin
import os
import sys
from django import forms
from django.contrib.auth.admin import UserAdmin
from django.db.models import Q, IntegerField, Sum, Count, Value, DecimalField
from django.db.models.functions import Cast, Coalesce
from django.utils import timezone
from django.db import transaction as db_transaction
from django.db.utils import OperationalError, ProgrammingError
from django.contrib import messages
from decimal import Decimal
from django.urls import path, reverse 
from django.shortcuts import redirect, render, get_object_or_404
from django.utils.html import format_html 
from django_ckeditor_5.widgets import CKEditor5Widget
from django.core.mail import send_mail
from django.utils.crypto import get_random_string

ENABLE_CELERY_APPS = os.getenv("ENABLE_CELERY_APPS", "").strip().lower() in ("1", "true", "yes", "on")
FORCE_CELERY_ON_WINDOWS = os.getenv("FORCE_CELERY_ON_WINDOWS", "").strip().lower() in ("1", "true", "yes", "on")
CELERY_APPS_ENABLED = ENABLE_CELERY_APPS and (os.name != "nt" or FORCE_CELERY_ON_WINDOWS)
if CELERY_APPS_ENABLED:
    from django_celery_beat.models import PeriodicTask, IntervalSchedule, CrontabSchedule, SolarSchedule, ClockedSchedule
    from django_celery_beat.admin import PeriodicTaskAdmin, ClockedScheduleAdmin
    from django_celery_results.models import TaskResult, GroupResult
    from django_celery_results.admin import TaskResultAdmin, GroupResultAdmin
else:
    PeriodicTask = IntervalSchedule = CrontabSchedule = SolarSchedule = ClockedSchedule = None
    PeriodicTaskAdmin = ClockedScheduleAdmin = None
    TaskResult = GroupResult = None
    TaskResultAdmin = GroupResultAdmin = None

# Import your views for custom admin pages
from . import views 

# Import necessary forms from your forms.py
# Note: UserCreationForm and UserChangeForm are aliases for AdminUserCreationForm and AdminUserChangeForm
from .forms import (
    UserCreationForm, 
    UserChangeForm, 
    WithdrawalActionForm, 
    DeclareResultForm,
    FixtureForm,
    FixtureUploadForm,
    BettingPeriodForm,
)
from .services.email_policy import normalize_email_value
from django.core.files.storage import FileSystemStorage
from django.conf import settings
from datetime import datetime, time, timedelta
import math

from .models import (
    User, Wallet, WalletLedgerEntry, Transaction, BettingPeriod, Fixture, PopularPick, BetTicket,
    BonusRule, SystemSetting, AgentPayout, UserWithdrawal, ActivityLog, Result, Selection,
    SiteConfiguration, LoginAttempt, CreditRequest, CRMWalletApprovalRequest, Loan, CreditLog, ImpersonationLog,
    ProcessedWithdrawal, WebAuthnCredential, BiometricAuthLog, CarouselImage,
    PasswordResetRequest, State, FooterPage, FooterBadge,
    GlobalBettingSettings, AgentBettingLimitOverride, UserBettingLimitOverride, BettingLimitAuditLog,
    PaymentGatewayDeposit,
    CashierRegistrationRequest, PendingCashierRegistration, ApprovedNewCashier,
    RetailManagerMasterAgentMapping, RetailManagerSuperAgentMapping, RetailManagerAgentMapping,
    AgentTransferLog,
    AccountUnlockAppeal, AccountLockAuditLog,
    FinanceAuditLog, CRMActionLog, WithdrawalReport,
    CustomerComplaint, CustomerComplaintNote, DashboardTask,
    BulkMessageTemplate, BulkMessageCampaign, BulkMessageDelivery,
    CRMOpsAuditLog,
    OverdraftWallet, OverdraftWalletLedgerEntry, LoanRepayment, LoanAuditLog,
)
from . import signals
from .services.ticket_results import recalculate_tickets_for_fixture_sync


# --- Custom Admin Site Definition ---
class BettingAdminSite(admin.AdminSite):
    site_header = "PoolBetBetting Admin" # Corrected from "PoolBetting Admin" for consistency, but you can change back if intended
    site_title = "PoolBetting Admin Portal"
    index_title = "Welcome to PoolBetting Administration"

    def index(self, request, extra_context=None):
        extra_context = extra_context or {}
        try:
            pending_crm_wallet_approval_count = CreditRequest.objects.filter(
                status='pending',
                request_type__in=views.CRM_WALLET_APPROVAL_REQUEST_TYPES,
            ).count()
        except Exception:
            pending_crm_wallet_approval_count = 0

        try:
            from pending_registration.models import PendingAgentRegistration
            pending_agent_registration_count = PendingAgentRegistration.objects.filter(status='PENDING').count()
        except Exception:
            pending_agent_registration_count = 0

        extra_context.update({
            'pending_crm_wallet_approval_count': pending_crm_wallet_approval_count,
            'crm_wallet_approval_admin_url': reverse(f'{self.name}:betting_crmwalletapprovalrequest_changelist'),
            'crm_wallet_dashboard_url': reverse(f'{self.name}:dashboard'),
            'pending_agent_registration_count': pending_agent_registration_count,
            'pending_agent_registration_admin_url': reverse(f'{self.name}:pending_registration_pendingagentregistration_changelist'),
        })
        return super().index(request, extra_context)

    def admin_view(self, view, cacheable=False):
        from django.shortcuts import render
        from functools import wraps
        
        inner = super().admin_view(view, cacheable)

        @wraps(inner)
        def wrapper(request, *args, **kwargs):
            if request.user.is_authenticated and request.user.is_staff:
                # Restrict access to only 'admin' user_type or superusers
                # Agents, Cashiers, etc. have is_staff=True but should not access the Admin Panel
                if request.user.user_type != 'admin' and not request.user.is_superuser:
                    return render(request, 'betting/admin/admin_unauthorized.html')
            
            return inner(request, *args, **kwargs)
        
        return wrapper

    def get_urls(self):
        from django.urls import path, re_path

        urls = super().get_urls()

        custom_admin_pages = [
            path('dashboard/', self.admin_view(views.admin_dashboard), name='dashboard'),

            path('fixtures/', self.admin_view(views.manage_fixtures), name='manage_fixtures'),
            path('fixtures/add/', self.admin_view(views.add_fixture), name='add_fixture'),
            path('fixtures/edit/<int:fixture_id>/', self.admin_view(views.edit_fixture), name='edit_fixture'),
            path('fixtures/delete/<int:fixture_id>/', self.admin_view(views.delete_fixture), name='delete_fixture'),
            path('fixtures/declare-result/<int:fixture_id>/', self.admin_view(views.declare_result), name='declare_result'),

            path('users/', self.admin_view(views.manage_users), name='manage_users'),
            path('users/add/', self.admin_view(views.add_user), name='add_user'), # ADDED THIS LINE: Defines the URL for adding a user
            path('users/edit/<int:user_id>/', self.admin_view(views.edit_user), name='edit_user'),
            path('users/delete/<int:user_id>/', self.admin_view(views.delete_user), name='delete_user'),

            path('withdrawals/', self.admin_view(views.withdraw_request_list), name='withdraw_request_list'),
            path('withdrawals/<int:withdrawal_id>/action/', self.admin_view(views.approve_reject_withdrawal), name='approve_reject_withdrawal'),

            path('betting-periods/', self.admin_view(views.manage_betting_periods), name='manage_betting_periods'),
            path('betting-periods/add/', self.admin_view(views.add_betting_period), name='add_betting_period'),
            path('betting-periods/edit/<int:period_id>/', self.admin_view(views.edit_betting_period), name='edit_betting_period'),
            path('betting-periods/delete/<int:period_id>/', self.admin_view(views.delete_betting_period), name='delete_betting_period'),

            path('agent-payouts/', self.admin_view(views.manage_agent_payouts), name='manage_agent_payouts'),
            path('agent-payouts/settle/<int:payout_id>/', self.admin_view(views.mark_payout_settled), name='mark_payout_settled'),

            path('manual-wallet/', self.admin_view(views.admin_manual_wallet_manager), name='admin_manual_wallet_manager'),

            path('reports/ticket/', self.admin_view(views.admin_ticket_report), name='admin_ticket_report'), 
            path('reports/ticket/<uuid:ticket_id>/', self.admin_view(views.admin_ticket_details), name='admin_ticket_details'), 
            path('reports/ticket/void/<uuid:ticket_id>/', self.admin_view(views.admin_void_ticket_single), name='admin_void_ticket_single'), 
            path('reports/ticket/settle-won/<uuid:ticket_id>/', self.admin_view(views.admin_settle_won_ticket_single), name='admin_settle_won_ticket_single'), 
            path('reports/tickets-by-event/', self.admin_view(views.admin_tickets_by_event_report), name='admin_tickets_by_event_report'),
            path('ops/reconciliation/', self.admin_view(views.admin_reconciliation_dashboard), name='admin_reconciliation_dashboard'),
            path('ops/reconciled-credits/', self.admin_view(views.admin_reconciled_credits_dashboard), name='admin_reconciled_credits_dashboard'),
            path('ops/issued-overdraft/', self.admin_view(views.admin_issued_overdrafts), name='admin_issued_overdrafts'),
            path('ops/retail-manual-adjustments/', self.admin_view(views.admin_retail_manual_adjustments), name='admin_retail_manual_adjustments'),
            path('ops/loan-overdraft-center/', self.admin_view(views.admin_loan_overdraft_center), name='admin_loan_overdraft_center'),
            path('ops/ticket-transactions/', self.admin_view(views.admin_ticket_transactions), name='admin_ticket_transactions'),
            path('ops/ticket-transactions/backfill/', self.admin_view(views.admin_backfill_ticket_transactions), name='admin_backfill_ticket_transactions'),
            path(
                'ops/ticket-transactions/backfill-refund-reversal-adjustments/',
                self.admin_view(views.admin_backfill_ticket_refund_reversal_adjustments),
                name='admin_backfill_ticket_refund_reversal_adjustments',
            ),
            path('ops/celery-health/', self.admin_view(views.admin_celery_health), name='admin_celery_health'),
            path('reports/limits/rejections/', self.admin_view(views.admin_limit_rejections_report), name='admin_limit_rejections_report'),

            path('reports/wallet/', self.admin_view(views.admin_wallet_report), name='admin_wallet_report'),
            path('reports/sales-winnings/', self.admin_view(views.admin_sales_winnings_report), name='admin_sales_winnings_report'),
            path('reports/commission/', self.admin_view(views.admin_commission_report), name='admin_commission_report'),

            path('activity-logs/', self.admin_view(views.admin_activity_log), name='admin_activity_log'),
        ]
        
        return custom_admin_pages + urls

betting_admin_site = BettingAdminSite(name='betting_admin')


class LoginAttemptAdmin(admin.ModelAdmin):
    list_display = ('username_attempted', 'status', 'ip_address', 'timestamp', 'user')
    list_filter = ('status', 'timestamp')
    search_fields = ('username_attempted', 'ip_address', 'user__email')
    readonly_fields = ('timestamp',)

    def has_add_permission(self, request):
        return False


# --- Custom User Admin ---
class CustomUserAdmin(UserAdmin):
    # Use our custom forms for creation and editing in the Django admin
    add_form = UserCreationForm
    form = UserChangeForm

    list_display = (
        'email', 'username', 'first_name', 'last_name', 'other_name', 'state', 'user_type', 'is_staff', 'is_active',
        'is_locked', 'failed_login_attempts',
        'withdrawal_locked', 'withdrawal_attempts', 'withdrawal_pin_status',
        'get_phone_number', 'get_shop_address', 'get_master_agent', 'get_super_agent', 'agent',
        'cashier_prefix', 'date_joined', 'updated_at', 'last_login', 'get_last_impersonated', 'impersonate_button'
    )
    list_filter = (
        'user_type', 'is_active', 'is_staff', 'is_locked', 
        'withdrawal_locked', 'date_joined', 'last_login'
    )
    search_fields = (
        'email', 'username', 'first_name', 'last_name', 'other_name', 'phone_number'
    )
    ordering = (
        'email',
    )
    
    actions = ['unlock_accounts', 'impersonate_user_action', 'enable_withdrawals', 'disable_withdrawals', 'reset_withdrawal_attempts']

    def withdrawal_pin_status(self, obj):
        return "Set" if getattr(obj, 'withdrawal_pin', '') else "Not Set"
    withdrawal_pin_status.short_description = "Withdrawal PIN"

    def impersonate_user_action(self, request, queryset):
        # This is the bulk action
        if queryset.count() != 1:
            self.message_user(request, "Please select exactly one user to impersonate.", level=messages.WARNING)
            return
        
        if request.POST.get('confirmed') == 'yes':
            user = queryset.first()
            return redirect('betting:impersonate_user', user_id=user.pk)
        
        context = {
            'users': queryset,
            'title': 'Confirm Impersonation',
            'site_header': self.admin_site.site_header,
            'site_title': self.admin_site.site_title,
        }
        return render(request, 'betting/admin/impersonate_confirmation.html', context)
    impersonate_user_action.short_description = "Impersonate selected user"

    def impersonate_button(self, obj):
        # This is the inline button column
        if obj.is_superuser:
            return ""
        url = reverse('betting:impersonate_user', args=[obj.pk])
        msg = f"You are about to log in as {obj.email}. This action will be logged. Proceed?"
        return format_html(
            '<a class="button" href="{}" onclick="return confirm(\'{}\');" style="background-color: #f0ad4e; color: white; padding: 3px 8px; border-radius: 3px; text-decoration: none;">Login As User</a>', 
            url, msg
        )
    impersonate_button.short_description = "Impersonate"
    impersonate_button.allow_tags = True

    def get_last_impersonated(self, obj):
        last_log = ImpersonationLog.objects.filter(target_user=obj).order_by('-started_at').first()
        if last_log:
            return f"{last_log.started_at.strftime('%Y-%m-%d %H:%M')} ({last_log.admin_user.email})"
        return "-"
    get_last_impersonated.short_description = "Last Impersonated"

    def _resolve_pending_unlock_appeals(self, request, users, *, auto_comment):
        user_ids = [user.pk for user in users if getattr(user, 'pk', None)]
        if not user_ids:
            return 0

        prior_reason_map = {user.pk: (getattr(user, 'lock_reason', '') or '') for user in users if getattr(user, 'pk', None)}
        review_time = timezone.now()
        resolved_count = 0

        appeals = (
            AccountUnlockAppeal.objects
            .filter(locked_user_id__in=user_ids, status='pending')
            .select_related('locked_user', 'appealed_by')
        )

        for appeal in appeals:
            prior_reason = prior_reason_map.get(appeal.locked_user_id, '') or ''
            appeal.status = 'approved'
            appeal.admin_comment = auto_comment
            appeal.reviewed_at = review_time
            appeal.reviewed_by = request.user
            appeal.save(update_fields=['status', 'admin_comment', 'reviewed_at', 'reviewed_by'])

            AccountLockAuditLog.objects.create(
                locked_user=appeal.locked_user,
                appealed_by=appeal.appealed_by,
                reviewed_by=request.user,
                lock_reason=prior_reason,
                action='appeal_approved',
                remarks=appeal.appeal_reason,
            )
            try:
                views._notify_unlock_appeal_resolution(appeal, approved=True)
            except Exception:
                pass
            resolved_count += 1

        return resolved_count

    def unlock_accounts(self, request, queryset):
        targets = list(queryset)
        updated_count = queryset.update(
            is_locked=False,
            failed_login_attempts=0,
            last_failed_login=None,
            locked_at=None,
            lock_reason=None
        )
        resolved_appeals = self._resolve_pending_unlock_appeals(
            request,
            targets,
            auto_comment='Approved automatically because the account was unlocked from Django admin bulk action.',
        )
        
        # Log the unlock action for each user
        for user in targets:
            LoginAttempt.objects.create(
                user=user,
                username_attempted=user.email,
                ip_address=request.META.get('REMOTE_ADDR'),
                user_agent=request.META.get('HTTP_USER_AGENT', ''),
                status='unlocked'
            )
            AccountLockAuditLog.objects.create(
                locked_user=user,
                reviewed_by=request.user,
                lock_reason=user.lock_reason or '',
                action='unlocked',
                remarks='Account unlocked from Django admin bulk action.',
            )
            
        message = f"{updated_count} account(s) successfully unlocked."
        if resolved_appeals:
            message += f" {resolved_appeals} pending unlock appeal(s) marked approved."
        self.message_user(request, message)
    unlock_accounts.short_description = "Unlock selected accounts"

    fieldsets = (
        (None, {'fields': ('email', 'username', 'password')}),
        ('Personal info', {'fields': ('first_name', 'last_name', 'other_name', 'state', 'phone_number', 'shop_address', 'bank_account_name', 'kyc_status', 'vip_level', 'vip_manager')}),
        ('Permissions', {'fields': ('is_active', 'is_staff', 'is_superuser', 'can_manage_downline_wallets', 'groups', 'user_permissions', 'user_type', 'crm_role', 'finance_role')}),
        ('Hierarchy', {'fields': ('master_agent', 'super_agent', 'agent', 'cashier_prefix')}),
        ('Important dates', {'fields': ('last_login', 'date_joined')}),
        ('Security & Locking', {'fields': ('is_locked', 'failed_login_attempts', 'last_failed_login', 'locked_at', 'lock_reason')}),
        ('Withdrawal PIN', {'fields': ('withdrawal_locked', 'withdrawal_locked_at', 'withdrawal_attempts', 'withdrawal_pin_new', 'withdrawal_pin_confirm')}),
    )

    add_fieldsets = (
        (None, {
            'classes': ('wide',),
            'fields': (
                'email', 'username', 'password', 'password2',
                'first_name', 'last_name', 'other_name', 'state', 'phone_number', 'shop_address', 'bank_account_name',
                'kyc_status', 'vip_level', 'vip_manager',
                'user_type', 'crm_role', 'finance_role', 'is_active', 'is_staff', 'is_superuser', 'can_manage_downline_wallets', 
                'groups', 'user_permissions', 
                'master_agent', 'super_agent', 'agent', 'cashier_prefix'
            ),
        }),
    )
    
    readonly_fields = ('last_login', 'date_joined', 'withdrawal_locked_at') 

    def enable_withdrawals(self, request, queryset):
        updated = queryset.update(withdrawal_locked=False, withdrawal_attempts=0, withdrawal_locked_at=None)
        self.message_user(request, f"{updated} user(s) enabled for withdrawals.")
    enable_withdrawals.short_description = "Enable withdrawals"

    def disable_withdrawals(self, request, queryset):
        updated = queryset.update(withdrawal_locked=True, withdrawal_locked_at=timezone.now())
        self.message_user(request, f"{updated} user(s) disabled from withdrawals.")
    disable_withdrawals.short_description = "Disable withdrawals"

    def reset_withdrawal_attempts(self, request, queryset):
        updated = queryset.update(withdrawal_attempts=0)
        self.message_user(request, f"{updated} user(s) withdrawal attempts reset.")
    reset_withdrawal_attempts.short_description = "Reset withdrawal attempts"

    def get_phone_number(self, obj):
        if obj.user_type == 'cashier' and obj.agent:
            return obj.agent.phone_number
        return obj.phone_number
    get_phone_number.short_description = 'Phone Number'
    get_phone_number.admin_order_field = 'phone_number'

    def get_shop_address(self, obj):
        if obj.user_type == 'cashier' and obj.agent:
            return obj.agent.shop_address
        return obj.shop_address
    get_shop_address.short_description = 'Shop Address'
    get_shop_address.admin_order_field = 'shop_address'

    def get_master_agent(self, obj):
        if obj.user_type == 'cashier' and obj.agent:
            return obj.agent.master_agent
        return obj.master_agent
    get_master_agent.short_description = 'Master Agent'
    get_master_agent.admin_order_field = 'master_agent'

    def get_super_agent(self, obj):
        if obj.user_type == 'cashier' and obj.agent:
            return obj.agent.super_agent
        return obj.super_agent
    get_super_agent.short_description = 'Super Agent'
    get_super_agent.admin_order_field = 'super_agent' 

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        """
        Filter dropdowns for hierarchy fields to show only relevant user types.
        """
        if db_field.name == "master_agent":
            kwargs["queryset"] = User.objects.filter(user_type='master_agent')
        elif db_field.name == "super_agent":
            kwargs["queryset"] = User.objects.filter(user_type='super_agent')
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def get_form(self, request, obj=None, **kwargs):
        # Pass the request to the form for permission checks if needed in form's clean method
        # We wrap the form class to inject the request into __init__
        if obj: # Editing an existing object
            kwargs['form'] = self.form
        else: # Adding a new object
            kwargs['form'] = self.add_form
            
        FormClass = super().get_form(request, obj, **kwargs)
        
        class RequestForm(FormClass):
            def __init__(self, *args, **kwargs):
                kwargs['request'] = request
                super().__init__(*args, **kwargs)
                
        return RequestForm

    def save_model(self, request, obj, form, change):
        previous = None
        if change and obj.pk:
            previous = User.objects.filter(pk=obj.pk).only('is_locked', 'lock_reason').first()
        action_description = f"User '{obj.email}' {'updated' if change else 'created'}."
        views.log_admin_activity(request, action_description)

        # The password setting and user type related staff/superuser status
        # are now largely handled within the custom AdminUserCreationForm/AdminUserChangeForm save methods.
        # Call the form's save method explicitly if you need its custom logic to run.
        # Otherwise, super().save_model will call obj.save() and form.save() as needed.
        super().save_model(request, obj, form, change)

        if previous and previous.is_locked != obj.is_locked:
            if obj.is_locked:
                AccountLockAuditLog.objects.create(
                    locked_user=obj,
                    locked_by=request.user,
                    lock_reason=obj.lock_reason or '',
                    action='locked',
                    remarks='Account locked from Django admin user form.',
                )
            else:
                resolved_appeals = self._resolve_pending_unlock_appeals(
                    request,
                    [obj],
                    auto_comment='Approved automatically because the account was unlocked from the Django admin user form.',
                )
                AccountLockAuditLog.objects.create(
                    locked_user=obj,
                    reviewed_by=request.user,
                    lock_reason=previous.lock_reason or '',
                    action='unlocked',
                    remarks='Account unlocked from Django admin user form.',
                )
                if resolved_appeals:
                    self.message_user(
                        request,
                        f"{resolved_appeals} pending unlock appeal(s) marked approved for this account.",
                        level=messages.INFO,
                    )
        elif not change and obj.is_locked:
            AccountLockAuditLog.objects.create(
                locked_user=obj,
                locked_by=request.user,
                lock_reason=obj.lock_reason or '',
                action='locked',
                remarks='Locked account created from Django admin user form.',
            )

        if not change and getattr(obj, 'email', None) and getattr(obj, 'user_type', None) in ['retail_manager', 'finance', 'account_user', 'crm']:
            raw_password = None
            try:
                raw_password = form.cleaned_data.get('password')
            except Exception:
                raw_password = None
            if raw_password:
                login_url = request.build_absolute_uri('/login/')
                subject = 'Your StakeNaija Account Login Details'
                username_value = getattr(obj, 'username', '') or ''
                role_value = obj.get_user_type_display()
                host_value = request.get_host()
                def _esc(v):
                    return (v or '').replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

                email_html = _esc(obj.email)
                username_html = _esc(username_value or '-')
                password_html = _esc(raw_password)
                role_html = _esc(role_value)
                login_url_html = _esc(login_url)
                host_html = _esc(host_value)
                message = (
                    f"Hello,\n\n"
                    f"An account has been created for you on StakeNaija.\n\n"
                    f"Role: {role_value}\n"
                    f"Email: {obj.email}\n"
                    f"Username: {username_value or '-'}\n"
                    f"Password: {raw_password}\n\n"
                    f"You can log in using either your Email or Username with the Password above.\n\n"
                    f"Login: {login_url}\n\n"
                    f"If you did not request this account, please contact support.\n"
                    f"Host: {host_value}"
                )
                html_message = f"""
                <div style="background:#f6f9fc;padding:24px 0;">
                  <div style="max-width:720px;margin:0 auto;padding:0 12px;">
                    <div style="background:#0b3d2e;border-radius:14px;padding:16px 18px;margin-bottom:12px;">
                      <div style="font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif;color:#ffffff;font-size:18px;font-weight:800;line-height:1.25;">
                        StakeNaija
                      </div>
                      <div style="font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif;color:rgba(255,255,255,0.85);font-size:13px;margin-top:4px;">
                        Account Access Details
                      </div>
                    </div>

                    <div style="background:#ffffff;border:1px solid #e9edf2;border-radius:14px;box-shadow:0 10px 26px rgba(16,24,40,0.08);overflow:hidden;">
                      <div style="padding:18px 18px 0 18px;">
                        <div style="font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif;color:#101828;font-size:18px;font-weight:800;">
                          Your login details
                        </div>
                        <div style="font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif;color:#667085;font-size:13px;margin-top:6px;">
                          You can log in using either your Email or Username with the Password below.
                        </div>
                      </div>

                      <div style="padding:18px;">
                        <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="border-collapse:separate;border-spacing:0 10px;">
                          <tr>
                            <td style="background:#fbfcfe;border:1px solid #eef2f6;border-radius:12px;padding:12px;">
                              <div style="font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif;color:#667085;font-size:12px;margin-bottom:6px;">Role</div>
                              <div style="font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif;color:#101828;font-size:14px;font-weight:700;">{role_html}</div>
                            </td>
                          </tr>
                          <tr>
                            <td style="background:#fbfcfe;border:1px solid #eef2f6;border-radius:12px;padding:12px;">
                              <div style="font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif;color:#667085;font-size:12px;margin-bottom:6px;">Email</div>
                              <div style="font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,'Liberation Mono','Courier New',monospace;color:#101828;font-size:13px;font-weight:700;">{email_html}</div>
                            </td>
                          </tr>
                          <tr>
                            <td style="background:#fbfcfe;border:1px solid #eef2f6;border-radius:12px;padding:12px;">
                              <div style="font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif;color:#667085;font-size:12px;margin-bottom:6px;">Username</div>
                              <div style="font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,'Liberation Mono','Courier New',monospace;color:#101828;font-size:13px;font-weight:700;">{username_html}</div>
                            </td>
                          </tr>
                          <tr>
                            <td style="background:#fbfcfe;border:1px solid #eef2f6;border-radius:12px;padding:12px;">
                              <div style="font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif;color:#667085;font-size:12px;margin-bottom:6px;">Password</div>
                              <div style="font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,'Liberation Mono','Courier New',monospace;color:#101828;font-size:13px;font-weight:700;">{password_html}</div>
                            </td>
                          </tr>
                        </table>

                        <div style="margin-top:14px;border:1px dashed #d0d5dd;border-radius:12px;padding:12px;background:#ffffff;">
                          <div style="font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif;color:#667085;font-size:12px;margin-bottom:8px;">
                            Copy &amp; Paste
                          </div>
                          <div style="font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,'Liberation Mono','Courier New',monospace;color:#101828;font-size:13px;font-weight:700;white-space:pre-wrap;word-break:break-word;line-height:1.5;">
Email: {email_html}
Username: {username_html}
Password: {password_html}
                          </div>
                        </div>

                        <div style="margin-top:14px;">
                          <a href="{login_url_html}" style="display:inline-block;background:#0d6efd;color:#ffffff;text-decoration:none;font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif;font-weight:700;font-size:14px;padding:10px 14px;border-radius:10px;">
                            Login to StakeNaija
                          </a>
                        </div>

                        <div style="font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif;color:#667085;font-size:12px;margin-top:14px;">
                          If you did not request this account, please contact support immediately.
                        </div>
                      </div>
                    </div>

                    <div style="font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif;color:#98a2b3;font-size:12px;margin-top:12px;text-align:center;">
                      © {timezone.localdate().year} StakeNaija • {host_html}
                    </div>
                  </div>
                </div>
                """
                from_email = settings.DEFAULT_FROM_EMAIL or settings.EMAIL_HOST_USER
                try:
                    send_mail(
                        subject=subject,
                        message=message,
                        from_email=from_email,
                        recipient_list=[obj.email],
                        html_message=html_message,
                        fail_silently=False,
                    )
                    messages.success(request, f"Login details sent to {obj.email}.")
                except Exception as e:
                    messages.warning(request, f"User created but email sending failed: {e}")

        # Additional safeguards/messages if needed, but primary logic is in form's clean/save
        if obj.user_type == 'admin' and not obj.is_superuser:
            messages.warning(request, f"User {obj.email} is an 'admin' type but not a superuser. Please ensure 'is_superuser' is checked.")
        if obj.user_type in ['master_agent', 'super_agent', 'agent', 'cashier'] and not obj.is_staff:
            messages.warning(request, f"User {obj.email} is a '{obj.user_type}' but not marked as staff. Please ensure 'staff status' is checked.")


    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if request.user.is_superuser or request.user.user_type == 'admin':
            return qs
        
        if request.user.user_type == 'master_agent':
            return qs.filter(
                Q(master_agent=request.user) |
                Q(super_agent__master_agent=request.user) |
                Q(agent__super_agent__master_agent=request.user) |
                Q(agent__master_agent=request.user) | 
                Q(pk=request.user.pk)
            ).distinct()
        
        elif request.user.user_type == 'super_agent':
            return qs.filter(
                Q(super_agent=request.user) |
                Q(agent__super_agent=request.user) |
                Q(pk=request.user.pk)
            ).distinct()
        
        elif request.user.user_type == 'agent':
            return qs.filter(
                Q(agent=request.user) |
                Q(pk=request.user.pk)
            ).distinct()

        return qs.filter(pk=request.user.pk)

    def has_change_permission(self, request, obj=None):
        if request.user.is_superuser:
            return True
        
        if request.user.user_type == 'admin':
            if obj and obj.is_superuser and obj != request.user: 
                return False
            return True 

        if obj: 
            if request.user.user_type == 'master_agent':
                return (obj == request.user or 
                        (obj.user_type in ['super_agent', 'agent', 'cashier', 'player'] and (
                            obj.master_agent == request.user or
                            (obj.super_agent and obj.super_agent.master_agent == request.user) or
                            (obj.agent and obj.agent.master_agent == request.user) or # Simplified for direct agent under MA
                            (obj.agent and obj.agent.super_agent and obj.agent.super_agent.master_agent == request.user) # For player/cashier under agent under SA under MA
                        )))
            
            elif request.user.user_type == 'super_agent':
                return (obj == request.user or 
                        (obj.user_type in ['agent', 'cashier', 'player'] and (
                            obj.super_agent == request.user or
                            (obj.agent and obj.agent.super_agent == request.user)
                        )))
            
            elif request.user.user_type == 'agent':
                return (obj == request.user or 
                        (obj.user_type in ['cashier', 'player'] and obj.agent == request.user))
            
            return obj == request.user # Players/Cashiers can only edit their own profile in admin
        
        return request.user.is_superuser or request.user.user_type == 'admin' or request.user.user_type in ['master_agent', 'super_agent', 'agent']


    def has_delete_permission(self, request, obj=None):
        if request.user.is_superuser:
            return True

        if request.user.user_type == 'admin':
            if obj and obj.is_superuser and obj != request.user: 
                return False
            return True 

        if obj: 
            if request.user.user_type == 'master_agent':
                return (obj.user_type in ['super_agent', 'agent', 'cashier', 'player'] and (
                    obj.master_agent == request.user or
                    (obj.super_agent and obj.super_agent.master_agent == request.user) or
                    (obj.agent and obj.agent.super_agent and obj.agent.super_agent.master_agent == request.user) or
                    (obj.agent and obj.agent.master_agent == request.user)
                ))
            
            elif request.user.user_type == 'super_agent':
                return (obj.user_type in ['agent', 'cashier', 'player'] and (
                    obj.super_agent == request.user or
                    (obj.agent and obj.agent.super_agent == request.user)
                ))
            
            elif request.user.user_type == 'agent':
                return (obj.user_type in ['cashier', 'player'] and obj.user_type == 'cashier' and obj.agent == request.user)
            
            return False 
        
        return request.user.is_superuser or request.user.user_type == 'admin'


# --- Selection Inline for BetTicket Admin ---
class SelectionInline(admin.TabularInline):
    model = Selection
    extra = 0
    readonly_fields = ('fixture_display', 'bet_type', 'odd_selected', 'is_winning_selection')
    fields = ('fixture_display', 'bet_type', 'odd_selected', 'is_winning_selection')
    can_delete = False

    def fixture_display(self, obj):
        if getattr(obj, 'fixture_id', None):
            try:
                return f"{obj.fixture.home_team} vs {obj.fixture.away_team} ({obj.fixture.match_date})"
            except Exception:
                return str(obj.fixture)
        label = f"{getattr(obj, 'fixture_home_team', '')} vs {getattr(obj, 'fixture_away_team', '')}".strip()
        dt = getattr(obj, 'fixture_match_date', None)
        serial = getattr(obj, 'fixture_serial_number', '') or ''
        parts = [p for p in [label or 'Fixture', (str(dt) if dt else ''), (f"#{serial}" if serial else '')] if p]
        return " ".join(parts)
    fixture_display.short_description = 'Fixture'


# --- BetTicket Admin (Registered with custom site) ---
class TicketSelectionCountFilter(admin.SimpleListFilter):
    title = 'Single / Multiple / System'
    parameter_name = 'selection_type'

    def lookups(self, request, model_admin):
        return (
            ('single', 'Single'),
            ('multiple', 'Multiple'),
            ('system', 'System'),
        )

    def queryset(self, request, queryset):
        value = self.value()
        if value not in {'single', 'multiple', 'system'}:
            return queryset
        return queryset.filter(bet_type=value)


class BetTicketAdmin(admin.ModelAdmin):
    change_list_template = "betting/admin/betticket_change_list.html"
    list_display = (
        'ticket_id', 'user', 'selection_count', 'stake_amount', 'total_odd', 'potential_winning',
        'min_winning', 'max_winning', 'status', 'placed_at', 'deleted_by', 'deleted_at'
    )
    list_filter = ('status', TicketSelectionCountFilter, 'placed_at', 'user')
    search_fields = ('ticket_id', 'id__startswith', 'user__email__icontains')
    raw_id_fields = ('user', 'deleted_by')
    ordering = ('-placed_at',)
    inlines = [SelectionInline]
    readonly_fields = ('selections_snapshot_preview',)

    def get_queryset(self, request):
        return super().get_queryset(request).select_related('user', 'deleted_by').annotate(
            annotated_selection_count=Count('selections', distinct=True)
        )

    def selection_count(self, obj):
        return obj.original_selections_count or getattr(obj, 'annotated_selection_count', 0)

    selection_count.short_description = 'No. of Selections'
    selection_count.admin_order_field = 'original_selections_count'

    def selections_snapshot_preview(self, obj):
        snap = (getattr(obj, 'betting_limits_snapshot', None) or {}).get('selections_snapshot') or []
        if not snap:
            return ''
        lines = []
        for s in snap:
            home = s.get('home_team') or ''
            away = s.get('away_team') or ''
            bt = s.get('bet_type') or ''
            odd = s.get('odd_selected') or ''
            lines.append(f"{home} vs {away} | {bt} | {odd}")
        return "\n".join(lines)

    selections_snapshot_preview.short_description = 'Selections Snapshot'

    def save_model(self, request, obj, form, change):
        if change:
             # Check if status is changing to cancelled/deleted and deleted_by is not set
             if obj.status in ['cancelled', 'deleted'] and not obj.deleted_by:
                 obj.deleted_by = request.user
                 obj.deleted_at = timezone.now()
        super().save_model(request, obj, form, change)

    actions = ['recalculate_selected_tickets', 'void_selected_tickets', 'settle_won_selected_tickets']

    @admin.action(description='Recalculate selected pending tickets (refresh odds/status)')
    def recalculate_selected_tickets(self, request, queryset):
        updated = 0
        skipped = 0
        failed = 0

        qs = queryset.select_related('user').prefetch_related('selections__fixture')
        for ticket in qs:
            if ticket.status != 'pending':
                skipped += 1
                continue
            try:
                ticket.recalculate_ticket()
                ticket.check_and_update_status()
                updated += 1
            except Exception as e:
                failed += 1
                messages.error(request, f"Failed to recalculate ticket {ticket.ticket_id}: {e}")

        if updated:
            messages.success(request, f"Recalculated {updated} pending ticket(s).")
        if skipped:
            messages.info(request, f"Skipped {skipped} non-pending ticket(s).")
        if failed:
            messages.warning(request, f"Failed to recalculate {failed} ticket(s).")

    @admin.action(description='Void selected bet tickets and refund stake')
    def void_selected_tickets(self, request, queryset):
        tickets_voided = 0
        tickets_failed = 0
        changed_ticket_ids = []
        audit_ticket_codes = []
        locked_ticket_ids = list(queryset.values_list("id", flat=True))

        for ticket_id in locked_ticket_ids:
            try:
                with db_transaction.atomic():
                    ticket = BetTicket.objects.select_for_update().select_related("user").get(pk=ticket_id)
                    if ticket.status in ['won', 'lost', 'cashed_out', *BetTicket.VOIDED_STATUSES]:
                        messages.warning(request, f"Ticket {ticket.ticket_id} is already '{ticket.display_status_label}' and cannot be voided.")
                        tickets_failed += 1
                        continue

                    deleted_at = timezone.now()
                    refund_tx = Transaction.objects.create(
                        user=ticket.user,
                        initiating_user=request.user if getattr(request.user, "is_authenticated", False) else None,
                        target_user=ticket.user,
                        transaction_type='ticket_deletion_refund',
                        amount=ticket.stake_amount,
                        is_successful=True,
                        status='completed',
                        description=f"Admin bulk void: Stake refunded for ticket {ticket.ticket_id}",
                        related_bet_ticket=ticket,
                        timestamp=deleted_at,
                    )

                    user_wallet = Wallet.objects.select_for_update().get(user=ticket.user)
                    user_wallet.apply_delta(
                        amount=ticket.stake_amount,
                        actor=request.user if getattr(request.user, "is_authenticated", False) else None,
                        transaction_obj=refund_tx,
                        reference=str(ticket.ticket_id),
                        reason=refund_tx.description,
                        metadata={"ticket_id": ticket.ticket_id, "source": "admin_bulk_void"},
                    )

                    BetTicket.objects.filter(pk=ticket.pk).update(
                        status='deleted',
                        deleted_by=request.user,
                        deleted_at=deleted_at,
                        last_updated=deleted_at,
                    )

                    changed_ticket_ids.append(str(ticket.id))
                    audit_ticket_codes.append(ticket.ticket_id)
                    tickets_voided += 1
            except Exception as e:
                messages.error(request, f"Failed to void ticket #{ticket_id}: {e}")
                tickets_failed += 1

        if changed_ticket_ids:
            from commission.tasks import enqueue_refresh_weekly_commissions_for_ticket_ids
            from betting.signals import schedule_admin_betticket_refresh
            enqueue_refresh_weekly_commissions_for_ticket_ids(changed_ticket_ids)
            preview_codes = ", ".join(audit_ticket_codes[:10])
            suffix = " ..." if len(audit_ticket_codes) > 10 else ""
            views.log_admin_activity(
                request,
                f"Bulk voided {tickets_voided} bet ticket(s): {preview_codes}{suffix}",
                affected_object="BetTicket bulk void",
            )
            schedule_admin_betticket_refresh(
                {
                    "ticket_ids": audit_ticket_codes,
                    "action": "bulk_void",
                    "count": tickets_voided,
                }
            )

        if tickets_voided > 0:
            messages.success(request, f"Successfully voided {tickets_voided} bet tickets.")
        if tickets_failed > 0:
            messages.warning(request, f"Failed to void {tickets_failed} bet tickets.")

    @admin.action(description='Settle selected bet tickets as WON and payout winnings')
    def settle_won_selected_tickets(self, request, queryset):
        tickets_settled = 0
        tickets_failed = 0

        with db_transaction.atomic():
            for ticket in queryset:
                if ticket.status != 'pending':
                    messages.warning(request, f"Ticket {ticket.ticket_id} is already '{ticket.status}' and cannot be manually settled as won.")
                    tickets_failed += 1
                    continue

                try:
                    ticket.status = 'won'
                    ticket.save()

                    user_wallet = Wallet.objects.select_for_update().get(user=ticket.user)
                    winnings_amount = ticket.max_winning
                    tx = Transaction.objects.create(
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
                        transaction_obj=tx,
                        reference=str(ticket.ticket_id),
                        reason=tx.description,
                        metadata={"ticket_id": ticket.ticket_id, "source": "admin_action"},
                    )
                    tickets_settled += 1
                    views.log_admin_activity(request, f"Settled bet ticket {ticket.ticket_id} as WON and paid out winnings.") 
                except Exception as e:
                    messages.error(request, f"Failed to settle ticket {ticket.ticket_id}: {e}")
                    tickets_failed += 1

        if tickets_settled > 0:
            messages.success(request, f"Successfully settled {tickets_settled} bet tickets as WON.")
        if tickets_failed > 0:
            messages.warning(request, f"Failed to settle {tickets_failed} bet tickets as WON.")


# --- BettingPeriod Admin ---
class BettingPeriodAdmin(admin.ModelAdmin):
    form = BettingPeriodForm
    list_display = ('name', 'fixture_theme_preview', 'start_date', 'end_date', 'is_active')
    list_editable = ('is_active',)
    search_fields = ('name',)
    ordering = ('-start_date',)
    fields = ('name', 'start_date', 'end_date', 'fixture_theme_color', 'fixture_theme_preview', 'is_active')
    readonly_fields = ('fixture_theme_preview',)

    def fixture_theme_preview(self, obj):
        color = getattr(obj, 'resolved_fixture_theme_color', BettingPeriod.DEFAULT_FIXTURE_THEME_COLOR)
        text_color = getattr(obj, 'fixture_theme_text_color', '#ffffff')
        return format_html(
            '<span style="display:inline-flex;align-items:center;gap:0.5rem;">'
            '<span style="width:1rem;height:1rem;border-radius:999px;border:1px solid #d1d5db;display:inline-block;background:{};"></span>'
            '<code style="background:{};color:{};padding:0.15rem 0.45rem;border-radius:999px;">{}</code>'
            '</span>',
            color,
            color,
            text_color,
            color,
        )
    fixture_theme_preview.short_description = 'Fixture Color'

class PopularPickAdmin(admin.ModelAdmin):
    list_display = ('fixture', 'get_period', 'bet_type', 'get_odd', 'is_active', 'sort_order', 'created_at')
    list_filter = ('is_active', 'bet_type', 'fixture__betting_period')
    search_fields = ('fixture__home_team', 'fixture__away_team', 'fixture__serial_number')
    autocomplete_fields = ('fixture',)
    ordering = ('sort_order', '-created_at')

    def get_period(self, obj):
        return getattr(obj.fixture, 'betting_period', None)
    get_period.short_description = 'Betting Period'

    def get_odd(self, obj):
        return obj.odd_value
    get_odd.short_description = 'Odd'

# --- Fixture Admin ---
class FixtureAdmin(admin.ModelAdmin):
    form = FixtureForm
    list_display = (
        'home_team',
        'away_team',
        'match_date',
        'match_time',
        'draw_odd',
        'betting_period',
        'serial_number_display',
        'status',
        'is_active',
    )
    list_editable = ('match_date', 'match_time', 'draw_odd', 'is_active')
    list_filter = ('betting_period', 'status', 'is_active', 'match_date')
    search_fields = ('home_team', 'away_team', 'serial_number')
    
    def get_queryset(self, request):
        qs = super().get_queryset(request)
        # Only show fixtures from active betting periods
        qs = qs.filter(betting_period__is_active=True)
        return qs.annotate(
            serial_int=Cast('serial_number', IntegerField())
        ).order_by('serial_int')

    def serial_number_display(self, obj):
        return obj.serial_number
    serial_number_display.short_description = 'Serial Number'
    serial_number_display.admin_order_field = 'serial_int'

    class Media:
        js = ('js/admin_fixture_toggle.js',)

    change_list_template = "betting/admin/fixture_change_list.html"

    def get_urls(self):
        urls = super().get_urls()
        my_urls = [
            path('import-fixtures/', self.admin_site.admin_view(self.import_fixtures), name='import_fixtures'),
            path('import-fixtures/sample/', self.admin_site.admin_view(self.download_sample_template), name='download_sample_template'),
        ]
        return my_urls + urls

    def download_sample_template(self, request):
        import io
        import pandas as pd
        from django.http import HttpResponse
        
        # Create a DataFrame with sample data matching the required structure
        # Columns: Serial, Home, Ignored, Away, Draw Odd, Date, Time
        data = {
            'Serial Number': [1, 2, 3],
            'Home Team': ['Arsenal', 'Chelsea', 'Liverpool'],
            'Ignored (C)': ['', '', ''],
            'Away Team': ['Man Utd', 'Tottenham', 'Man City'],
            'Draw Odd': [3.50, 3.20, 3.10],
            'Match Date': ['01/02/26', '01/02/26', '01/02/26'],
            'Match Time': ['14:00', '16:00', '18:30']
        }
        df = pd.DataFrame(data)
        
        buffer = io.BytesIO()
        with pd.ExcelWriter(buffer, engine='openpyxl') as writer:
            df.to_excel(writer, index=False)
            
        buffer.seek(0)
        response = HttpResponse(buffer.read(), content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        response['Content-Disposition'] = 'attachment; filename=fixture_import_template.xlsx'
        return response

    def import_fixtures(self, request):
        if request.method == "POST":
            import pandas as pd
            form = FixtureUploadForm(request.POST, request.FILES)
            if form.is_valid():
                excel_file = request.FILES["excel_file"]
                betting_period = form.cleaned_data["betting_period"]
                
                try:
                    # Read Excel: Columns A, B, D, E, F, G -> Indices 0, 1, 3, 4, 5, 6
                    # Do not force dtype=str for all columns because Excel dates may come as datetime objects or serials.
                    # We'll parse dates/times explicitly below and always treat slash/dot/hyphen dates as day/month/year.
                    df = pd.read_excel(excel_file, usecols=[0, 1, 3, 4, 5, 6])
                    df.columns = ['serial_number', 'home_team', 'away_team', 'draw_odd', 'match_date', 'match_time']
                    
                    success_count = 0
                    updated_count = 0
                    skip_count = 0
                    errors = []
                    created_fixture_ids = []
                    updated_fixture_ids = []
                    
                    for index, row in df.iterrows():
                        try:
                            # Skip empty rows
                            if pd.isna(row['serial_number']) and pd.isna(row['home_team']):
                                continue
                                
                            # Validation
                            if pd.isna(row['serial_number']) or pd.isna(row['home_team']) or pd.isna(row['away_team']):
                                raise ValueError("Missing required fields (Serial, Home, Away)")
                            
                            # Skip header rows that might be interpreted as data
                            if str(row['serial_number']).strip().lower() in ['serial', 'serial number', 'serial_number']:
                                continue

                            serial = str(row['serial_number']).split('.')[0]
                            home = str(row['home_team']).strip()
                            away = str(row['away_team']).strip()
                            
                            def _parse_excel_date(v):
                                if v is None or pd.isna(v):
                                    raise ValueError("Date is missing")
                                if hasattr(v, 'date') and not isinstance(v, str):
                                    try:
                                        return v.date()
                                    except Exception:
                                        pass
                                s = str(v).strip()
                                if not s or s.lower() in ['nan', 'none', 'null']:
                                    raise ValueError("Date is missing")
                                if ' ' in s:
                                    s = s.split(' ')[0].strip()
                                if s.replace('.', '').replace('/', '').replace('-', '').isdigit():
                                    digits = s.replace('.', '').replace('/', '').replace('-', '')
                                    if len(digits) <= 6:
                                        try:
                                            serial_num = int(float(s))
                                            if serial_num > 0:
                                                parsed = (datetime(1899, 12, 30) + timedelta(days=serial_num)).date()
                                                if betting_period and (parsed < betting_period.start_date or parsed > betting_period.end_date):
                                                    raise ValueError(
                                                        f"Excel date serial {s} resolves to {parsed}, which is outside the selected betting period ({betting_period.start_date} to {betting_period.end_date}). "
                                                        f"Ensure the sheet date column is saved as text in dd/mm/yyyy."
                                                    )
                                                return parsed
                                        except ValueError:
                                            raise
                                        except Exception:
                                            pass

                                for fmt in ('%d/%m/%Y', '%d/%m/%y', '%d-%m-%Y', '%d-%m-%y', '%d.%m.%Y', '%d.%m.%y', '%Y-%m-%d'):
                                    try:
                                        parsed = datetime.strptime(s, fmt).date()
                                        break
                                    except ValueError:
                                        parsed = None
                                if parsed is None:
                                    try:
                                        parsed = pd.to_datetime(s, dayfirst=True, errors='raise').date()
                                    except Exception as e:
                                        raise ValueError(f"Invalid date format: {v} ({str(e)})")

                                if betting_period and (parsed < betting_period.start_date or parsed > betting_period.end_date):
                                    raise ValueError(f"Date {parsed} is outside the selected betting period ({betting_period.start_date} to {betting_period.end_date}).")
                                return parsed

                            match_date = _parse_excel_date(row['match_date'])
                                
                            # Parse Time
                            match_time = row['match_time']
                            try:
                                if pd.notna(match_time):
                                    # If it's already a time object (datetime.time)
                                    if isinstance(match_time, time): # Check exact type
                                        pass
                                    # If it's a datetime object (pd.Timestamp or datetime.datetime)
                                    elif hasattr(match_time, 'time'):
                                        match_time = match_time.time()
                                    # If it's a string, try parsing multiple formats
                                    elif isinstance(match_time, str):
                                        try:
                                            match_time = datetime.strptime(match_time, '%H:%M').time()
                                        except ValueError:
                                            # Try with seconds
                                            match_time = datetime.strptime(match_time, '%H:%M:%S').time()
                                    else:
                                        raise ValueError(f"Unknown time type: {type(match_time)}")
                                else:
                                    raise ValueError("Time is missing")
                            except Exception as e:
                                raise ValueError(f"Invalid time format: {match_time}")

                            existing_fixture = Fixture.objects.filter(serial_number=serial, betting_period=betting_period).first()
                            if existing_fixture:
                                existing_fixture.home_team = home
                                existing_fixture.away_team = away
                                existing_fixture.draw_odd = row['draw_odd'] if not pd.isna(row['draw_odd']) else None
                                existing_fixture.match_date = match_date
                                existing_fixture.match_time = match_time
                                if not existing_fixture.status:
                                    existing_fixture.status = 'scheduled'
                                existing_fixture.is_active = True
                                existing_fixture.save(update_fields=['home_team', 'away_team', 'draw_odd', 'match_date', 'match_time', 'status', 'is_active'])
                                updated_fixture_ids.append(existing_fixture.id)
                                updated_count += 1
                                continue
                                
                            # Check duplicates (Teams + Date + Time)
                            if Fixture.objects.filter(betting_period=betting_period, home_team__iexact=home, away_team__iexact=away, match_date=match_date, match_time=match_time).exists():
                                skip_count += 1
                                errors.append(f"Row {index + 2}: Duplicate Fixture {home} vs {away}")
                                continue

                            # Create Fixture
                            created_fixture = Fixture.objects.create(
                                betting_period=betting_period,
                                serial_number=serial,
                                home_team=home,
                                away_team=away,
                                draw_odd=row['draw_odd'] if not pd.isna(row['draw_odd']) else None,
                                match_date=match_date,
                                match_time=match_time,
                                status='scheduled',
                                is_active=True
                            )
                            created_fixture_ids.append(created_fixture.id)
                            success_count += 1
                            
                        except Exception as e:
                            errors.append(f"Row {index + 2}: {str(e)}")
                            
                    try:
                        if created_fixture_ids:
                            from django.db.models import Q
                            relinked = 0
                            for f in Fixture.objects.filter(id__in=created_fixture_ids).select_related('betting_period'):
                                serial = str(getattr(f, 'serial_number', '') or '').strip()
                                relink_q = Q(fixture__isnull=True, betting_period=f.betting_period)
                                if serial:
                                    relink_q &= (Q(fixture_serial_number__iexact=serial) | Q(fixture_home_team__iexact=f.home_team, fixture_away_team__iexact=f.away_team, fixture_match_date=f.match_date, fixture_match_time=f.match_time))
                                else:
                                    relink_q &= Q(fixture_home_team__iexact=f.home_team, fixture_away_team__iexact=f.away_team, fixture_match_date=f.match_date, fixture_match_time=f.match_time)

                                updated = Selection.objects.filter(relink_q).update(
                                    fixture=f,
                                    fixture_serial_number=serial or '',
                                    fixture_home_team=f.home_team,
                                    fixture_away_team=f.away_team,
                                    fixture_match_date=f.match_date,
                                    fixture_match_time=f.match_time,
                                )
                                relinked += int(updated or 0)

                            if relinked:
                                messages.info(request, f"Relinked {relinked} old ticket selections to newly uploaded fixtures.")
                    except Exception:
                        pass

                    messages.success(request, f"Upload Complete: {success_count} added, {updated_count} updated, {skip_count} skipped.")
                    if errors:
                        error_msg = " | ".join(errors[:10])
                        if len(errors) > 10:
                            error_msg += f" ... and {len(errors)-10} more."
                        messages.warning(request, f"Issues encountered: {error_msg}")
                        
                    return redirect('..')
                    
                except Exception as e:
                    messages.error(request, f"Critical Error processing file: {str(e)}")
                    
        else:
            form = FixtureUploadForm()
            
        context = {
            'form': form,
            'title': 'Upload Fixtures',
            'site_header': self.admin_site.site_header,
            'site_title': self.admin_site.site_title,
            'opts': self.model._meta,
        }
        return render(request, 'betting/admin/fixture_import.html', context)

    fieldsets = (
        (None, {
            'fields': ('betting_period', 'match_date', 'match_time', 'home_team', 'away_team', 'serial_number', 'status', 'is_active')
        }),
        ('Odds', {
            'fields': (
                ('active_home_win_odd', 'home_win_odd'),
                ('active_draw_odd', 'draw_odd'),
                ('active_away_win_odd', 'away_win_odd'),
                ('active_over_1_5_odd', 'over_1_5_odd'),
                ('active_under_1_5_odd', 'under_1_5_odd'),
                ('active_over_2_5_odd', 'over_2_5_odd'),
                ('active_under_2_5_odd', 'under_2_5_odd'),
                ('active_over_3_5_odd', 'over_3_5_odd'),
                ('active_under_3_5_odd', 'under_3_5_odd'),
                ('active_btts_yes_odd', 'btts_yes_odd'),
                ('active_btts_no_odd', 'btts_no_odd'),
                ('active_home_dnb_odd', 'home_dnb_odd'),
                ('active_away_dnb_odd', 'away_dnb_odd'),
            )
        }),
    )

# --- Result Admin ---
class ResultAdmin(admin.ModelAdmin):
    change_form_template = "betting/admin/result_change_form.html"
    list_display = (
        'serial_number_display',
        'home_team',
        'away_team',
        'match_date',
        'match_time',
        'home_score',
        'away_score',
        'status',
        'affected_tickets_count',
        'open_reprocess_page',
    )
    list_editable = ('home_score', 'away_score', 'status')
    list_filter = ('status', 'match_date', 'betting_period')
    search_fields = ('home_team', 'away_team', 'serial_number')
    
    def has_add_permission(self, request):
        return False

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        # Only show fixtures from active betting periods, similar to FixtureAdmin
        qs = qs.filter(betting_period__is_active=True)
        return qs.order_by('serial_number')

    def get_urls(self):
        urls = super().get_urls()
        opts = self.model._meta
        custom_urls = [
            path(
                '<path:object_id>/reprocess/',
                self.admin_site.admin_view(self.reprocess_affected_tickets_view),
                name=f'{opts.app_label}_{opts.model_name}_reprocess',
            ),
        ]
        return custom_urls + urls

    def save_model(self, request, obj, form, change):
        if obj.home_score is not None and obj.away_score is not None and obj.status in ('scheduled', 'live'):
            obj.status = 'finished'
        super().save_model(request, obj, form, change)

    def change_view(self, request, object_id, form_url='', extra_context=None):
        extra_context = extra_context or {}
        obj = self.get_object(request, object_id)
        if obj:
            opts = self.model._meta
            extra_context.update({
                'reprocess_url': reverse(
                    f'{self.admin_site.name}:{opts.app_label}_{opts.model_name}_reprocess',
                    args=[obj.pk],
                ),
                'affected_ticket_count': self._affected_tickets_queryset(obj).count(),
            })
        return super().change_view(request, object_id, form_url, extra_context=extra_context)

    def _affected_tickets_queryset(self, obj):
        return BetTicket.objects.filter(selections__fixture=obj).exclude(status__in=['deleted', 'cashed_out']).distinct()

    def affected_tickets_count(self, obj):
        return self._affected_tickets_queryset(obj).count()
    affected_tickets_count.short_description = 'Affected Tickets'

    def open_reprocess_page(self, obj):
        change_url = reverse(f'{self.admin_site.name}:{self.model._meta.app_label}_{self.model._meta.model_name}_change', args=[obj.pk])
        return format_html(
            '<a class="button" href="{}" style="padding:4px 8px; border-radius:4px; text-decoration:none;">Open / Reprocess</a>',
            change_url,
        )
    open_reprocess_page.short_description = 'Actions'

    @db_transaction.atomic
    def reprocess_affected_tickets_view(self, request, object_id):
        obj = get_object_or_404(Result, pk=object_id)
        change_url = reverse(
            f'{self.admin_site.name}:{self.model._meta.app_label}_{self.model._meta.model_name}_change',
            args=[obj.pk],
        )
        if request.method != 'POST':
            messages.warning(request, 'Use the Reprocess Affected Tickets button to continue.')
            return redirect(change_url)

        affected_count = self._affected_tickets_queryset(obj).count()
        result = recalculate_tickets_for_fixture_sync(obj.pk)
        if result and result.get("error"):
            self.message_user(
                request,
                (
                    "Unable to reprocess affected tickets because the correction would require reversing "
                    f"more settled winnings/refunds than the current wallet balance allows. Details: {result['error']}"
                ),
                level=messages.ERROR,
            )
            return redirect(change_url)
        self.message_user(
            request,
            f"Reprocessed {affected_count} affected ticket(s) for result {obj.serial_number}.",
            level=messages.SUCCESS,
        )
        return redirect(change_url)

    def serial_number_display(self, obj):
        return obj.serial_number
    serial_number_display.short_description = 'Serial Number'
    serial_number_display.admin_order_field = 'serial_number'


# --- Wallet Admin ---
class WalletAdmin(admin.ModelAdmin):
    list_display = ('user', 'balance', 'last_updated')
    search_fields = ('user__username', 'user__email')
    list_select_related = ('user',)
    readonly_fields = ('last_updated',)

class WalletLedgerEntryAdmin(admin.ModelAdmin):
    list_display = ("created_at", "user", "direction", "amount", "balance_before", "balance_after", "actor", "reference")
    list_filter = ("direction", "created_at")
    search_fields = ("user__email", "user__username", "actor__email", "reference", "reason")
    date_hierarchy = "created_at"
    list_select_related = ("user", "actor", "wallet", "transaction")

# --- Transaction Admin ---
class TransactionAdmin(admin.ModelAdmin):
    list_display = ('timestamp', 'user', 'transaction_type', 'payment_gateway_used', 'amount', 'status', 'is_successful')
    list_filter = ('transaction_type', 'status', 'is_successful', 'timestamp')
    search_fields = ('user__username', 'user__email', 'paystack_reference', 'external_reference', 'description', 'id')
    readonly_fields = ('timestamp',)
    date_hierarchy = 'timestamp'
    list_select_related = ('user',)

    def payment_gateway_used(self, obj):
        if obj.transaction_type != 'deposit':
            return ''
        if getattr(obj, 'payment_gateway', None):
            try:
                return obj.get_payment_gateway_display()
            except Exception:
                return obj.payment_gateway
        return ''
    payment_gateway_used.short_description = 'Payment Gateway'
    payment_gateway_used.admin_order_field = 'payment_gateway'

class PaymentGatewayDepositAdmin(admin.ModelAdmin):
    change_list_template = 'admin/betting/paymentgatewaydeposit/change_list.html'
    list_display = ('timestamp', 'user', 'payment_gateway', 'amount', 'status', 'is_successful', 'external_reference')
    list_filter = ('payment_gateway', 'status', 'is_successful', 'timestamp')
    search_fields = ('user__username', 'user__email', 'paystack_reference', 'external_reference', 'description', 'id')
    readonly_fields = (
        'id',
        'user',
        'initiating_user',
        'target_user',
        'transaction_type',
        'amount',
        'is_successful',
        'status',
        'description',
        'timestamp',
        'related_bet_ticket',
        'related_withdrawal_request',
        'related_payout',
        'payment_gateway',
        'paystack_reference',
        'external_reference',
    )
    fields = readonly_fields
    date_hierarchy = 'timestamp'
    list_select_related = (
        'user',
        'initiating_user',
        'target_user',
        'related_bet_ticket',
        'related_withdrawal_request',
        'related_payout',
    )

    def get_queryset(self, request):
        qs = super().get_queryset(request).select_related(
            'user',
            'initiating_user',
            'target_user',
            'related_bet_ticket',
            'related_withdrawal_request',
            'related_payout',
        )
        return qs.filter(transaction_type='deposit', payment_gateway__in=['monnify', 'paystack', 'kora'])

    def has_add_permission(self, request):
        return False

    def changelist_view(self, request, extra_context=None):
        extra_context = extra_context or {}
        tab = (request.GET.get('tab') or 'list').strip() or 'list'
        start_date = (request.GET.get('start_date') or '').strip()
        end_date = (request.GET.get('end_date') or '').strip()

        qs = self.get_queryset(request).filter(is_successful=True, status='completed')
        if start_date:
            qs = qs.filter(timestamp__date__gte=start_date)
        if end_date:
            qs = qs.filter(timestamp__date__lte=end_date)

        totals = list(
            qs.values('payment_gateway')
            .annotate(
                total_amount=Coalesce(Sum('amount'), Value(0), output_field=DecimalField()),
                total_count=Count('id'),
            )
            .order_by('payment_gateway')
        )
        for row in totals:
            label_map = {'paystack': 'Paystack', 'monnify': 'Monnify', 'kora': 'Kora'}
            row['payment_gateway_label'] = label_map.get(row['payment_gateway'], row['payment_gateway'])

        base_qd = request.GET.copy()
        try:
            base_qd.pop('tab', None)
        except Exception:
            pass
        base_query = base_qd.urlencode()

        extra_context.update(
            {
                'pgd_tab': tab,
                'pgd_start_date': start_date,
                'pgd_end_date': end_date,
                'pgd_gateway_totals': totals,
                'pgd_total_amount': qs.aggregate(v=Coalesce(Sum('amount'), Value(0), output_field=DecimalField()))['v'],
                'pgd_total_count': qs.count(),
                'pgd_base_query': base_query,
            }
        )
        return super().changelist_view(request, extra_context=extra_context)

class PendingCashierRegistrationAdmin(admin.ModelAdmin):
    list_display = ('created_at', 'agent', 'cashier_code', 'cashier_email', 'cashier_username', 'status', 'actions_buttons')
    list_filter = ('status', 'created_at')
    search_fields = ('agent__email', 'cashier_email', 'cashier_username', 'cashier_code')
    readonly_fields = ('created_at', 'reviewed_at', 'cashier_code', 'cashier_email', 'cashier_username', 'cashier_prefix', 'created_cashier', 'status')
    date_hierarchy = 'created_at'
    list_select_related = ('agent',)

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.filter(status='PENDING')

    def actions_buttons(self, obj):
        if obj.status == 'PENDING':
            return format_html(
                '<a class="btn btn-sm btn-success" href="{}">Approve</a>&nbsp;'
                '<a class="btn btn-sm btn-danger" href="{}">Reject</a>',
                f"approve/{obj.id}/",
                f"reject/{obj.id}/",
            )
        return obj.status
    actions_buttons.short_description = 'Actions'

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path('approve/<int:pk>/', self.admin_site.admin_view(self.approve_cashier), name='approve_cashier_request'),
            path('reject/<int:pk>/', self.admin_site.admin_view(self.reject_cashier), name='reject_cashier_request'),
        ]
        return custom_urls + urls

    def approve_cashier(self, request, pk):
        cashier_req = get_object_or_404(CashierRegistrationRequest, pk=pk)
        if cashier_req.status != 'PENDING':
            messages.warning(request, "This cashier registration is not pending.")
            return redirect(f'{self.admin_site.name}:betting_pendingcashierregistration_changelist')

        agent = cashier_req.agent
        if not agent:
            messages.error(request, "This request has no agent attached.")
            return redirect(f'{self.admin_site.name}:betting_pendingcashierregistration_changelist')

        raw_password = get_random_string(12)
        try:
            with db_transaction.atomic():
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

            messages.success(request, f"Cashier {cashier_req.cashier_email} approved and created successfully.")
        except Exception as e:
            messages.error(request, f"Error approving cashier: {e}")

        return redirect(f'{self.admin_site.name}:betting_pendingcashierregistration_changelist')

    def reject_cashier(self, request, pk):
        cashier_req = get_object_or_404(CashierRegistrationRequest, pk=pk)
        if cashier_req.status != 'PENDING':
            messages.warning(request, "This cashier registration is not pending.")
            return redirect(f'{self.admin_site.name}:betting_pendingcashierregistration_changelist')

        cashier_req.status = 'REJECTED'
        cashier_req.reviewed_at = timezone.now()
        cashier_req.admin_notes = 'Rejected by admin.'
        cashier_req.save(update_fields=['status', 'reviewed_at', 'admin_notes'])
        messages.success(request, "Cashier registration rejected.")
        return redirect(f'{self.admin_site.name}:betting_pendingcashierregistration_changelist')

class ApprovedNewCashierAdmin(admin.ModelAdmin):
    list_display = ('reviewed_at', 'agent', 'cashier_code', 'cashier_email', 'cashier_username', 'cashier_prefix')
    list_filter = ('reviewed_at', 'created_at')
    search_fields = ('agent__email', 'cashier_email', 'cashier_username', 'cashier_code')
    readonly_fields = [f.name for f in CashierRegistrationRequest._meta.fields]
    date_hierarchy = 'reviewed_at'
    list_select_related = ('agent',)

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.filter(status='APPROVED')

# --- UserWithdrawal Admin ---
class UserWithdrawalAdmin(admin.ModelAdmin):
    change_list_template = "betting/admin/userwithdrawal_change_list.html"

    def short_id(self, obj):
        return str(getattr(obj, 'id', '') or '')[:8]
    short_id.short_description = "ID"

    list_display = (
        'short_id',
        'user',
        'amount',
        'bank_name',
        'account_number',
        'account_name',
        'status',
        'request_time',
        'email_request_admin_sent_at',
        'email_request_user_sent_at',
        'email_approved_admin_sent_at',
        'email_approved_user_sent_at',
        'email_completed_admin_sent_at',
        'email_completed_user_sent_at',
    )
    list_editable = ('status',)
    list_filter = ('status', 'request_time', 'bank_name')
    search_fields = ('user__username', 'user__email', 'account_number', 'account_name')
    readonly_fields = (
        'request_time',
        'approved_rejected_time',
        'email_request_admin_sent_at',
        'email_request_user_sent_at',
        'email_approved_admin_sent_at',
        'email_approved_user_sent_at',
        'email_completed_admin_sent_at',
        'email_completed_user_sent_at',
        'email_success_admin_sent_at',
        'email_success_user_sent_at',
        'email_rejected_admin_sent_at',
        'email_rejected_user_sent_at',
        'last_email_error',
    )
    date_hierarchy = 'request_time'
    list_select_related = ('user',)
    actions = ['resend_emails_backfill_email_timestamps']

    def resend_emails_backfill_email_timestamps(self, request, queryset):
        try:
            from .tasks import backfill_withdrawal_notification_emails
        except Exception as exc:
            self.message_user(request, f"Could not queue resend/backfill task: {exc}", level=messages.ERROR)
            return

        withdrawal_ids = list(queryset.values_list('id', flat=True))
        if not withdrawal_ids:
            self.message_user(request, "No withdrawals selected.", level=messages.WARNING)
            return

        try:
            job = backfill_withdrawal_notification_emails.delay(withdrawal_ids)
            self.message_user(
                request,
                f"Resend/backfill queued for {len(withdrawal_ids)} withdrawal(s). Task ID: {job.id}",
                level=messages.INFO,
            )
        except Exception as exc:
            self.message_user(request, f"Failed to queue resend/backfill task: {exc}", level=messages.ERROR)
            return

    resend_emails_backfill_email_timestamps.short_description = "Resend emails / backfill email timestamps (missing only)"

class WithdrawalReportAdmin(admin.ModelAdmin):
    list_display = (
        'created_at',
        'username',
        'amount',
        'bank_name',
        'account_number',
        'transaction_reference',
        'event',
        'is_admin_copy',
        'withdrawal_status',
        'requested_at',
        'updated_at',
        'email_sent_at',
    )
    list_filter = ('event', 'is_admin_copy', 'withdrawal_status', 'bank_name', 'requested_at', 'email_sent_at')
    search_fields = (
        'username',
        'user__username',
        'user__email',
        'account_number',
        'account_name',
        'transaction_reference',
        'withdrawal__id',
    )
    readonly_fields = (
        'withdrawal',
        'user',
        'username',
        'amount',
        'bank_name',
        'account_name',
        'account_number',
        'requested_at',
        'updated_at',
        'transaction_reference',
        'withdrawal_status',
        'event',
        'is_admin_copy',
        'email_subject',
        'email_to',
        'email_cc',
        'email_bcc',
        'email_body_text',
        'email_body_html',
        'email_sent_at',
        'email_error',
        'created_at',
    )
    date_hierarchy = 'created_at'
    list_select_related = ('user', 'withdrawal')
    actions = ['backfill_reports_from_withdrawals']

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser

    def get_urls(self):
        urls = super().get_urls()
        my_urls = [
            path('backfill/', self.admin_site.admin_view(self.backfill_view), name='withdrawalreport_backfill'),
        ]
        return my_urls + urls

    def backfill_view(self, request):
        self.backfill_reports_from_withdrawals(request, None)
        return redirect('../')

    def backfill_reports_from_withdrawals(self, request, queryset):
        status_to_event = {
            'pending': 'requested',
            'approved': 'approved',
            'rejected': 'rejected',
            'completed': 'completed',
        }

        created = 0
        updated = 0
        total = 0

        withdrawals = UserWithdrawal.objects.select_related('user').all().order_by('request_time')
        for w in withdrawals:
            total += 1
            event_key = status_to_event.get((w.status or '').strip().lower(), 'requested')

            tx = (
                Transaction.objects.filter(related_withdrawal_request=w, transaction_type='withdrawal')
                .order_by('timestamp')
                .first()
            )
            reference = (
                getattr(tx, 'external_reference', None)
                or getattr(tx, 'paystack_reference', None)
                or (str(getattr(tx, 'id', '')) if tx else '')
                or str(w.id)
            )

            defaults = {
                'user': w.user,
                'username': (getattr(w.user, 'username', '') or getattr(w.user, 'email', '') or '').strip(),
                'amount': w.amount,
                'bank_name': w.bank_name,
                'account_name': w.account_name,
                'account_number': w.account_number,
                'requested_at': w.request_time,
                'updated_at': w.approved_rejected_time or w.request_time,
                'transaction_reference': reference,
                'withdrawal_status': w.status,
                'email_subject': '',
                'email_to': '',
                'email_cc': '',
                'email_bcc': '',
                'email_body_text': '',
                'email_body_html': '',
                'email_sent_at': None,
                'email_error': '',
            }

            obj, was_created = WithdrawalReport.objects.update_or_create(
                withdrawal=w,
                event=event_key,
                is_admin_copy=False,
                defaults=defaults,
            )
            if was_created:
                created += 1
            else:
                updated += 1

        self.message_user(request, f"Backfill complete: {created} created, {updated} updated (scanned {total}).")
    backfill_reports_from_withdrawals.short_description = "Backfill reports from all withdrawals"

# --- BonusRule Admin ---
class BonusRuleAdmin(admin.ModelAdmin):
    list_display = (
        'name',
        'is_active',
        'min_selections',
        'max_selections',
        'min_odd_per_selection',
        'bonus_percentage',
        'max_bonus_cap',
        'bonus_base',
        'allow_system_bets',
        'allow_accumulator_bets',
        'allow_single_bets',
    )
    list_filter = ('is_active', 'bonus_base', 'allow_system_bets', 'allow_accumulator_bets', 'allow_single_bets')
    search_fields = ('name',)
    ordering = ('min_selections', 'max_selections', 'min_odd_per_selection')

    def save_model(self, request, obj, form, change):
        obj.full_clean()
        super().save_model(request, obj, form, change)

class GlobalBettingSettingsAdmin(admin.ModelAdmin):
    list_display = ('is_active', 'betting_enabled', 'min_stake', 'max_stake', 'max_winning', 'updated_at')
    readonly_fields = ('created_at', 'updated_at', 'created_by', 'updated_by')

    def has_delete_permission(self, request, obj=None):
        return False

    def has_add_permission(self, request):
        try:
            return not GlobalBettingSettings.objects.exists()
        except (OperationalError, ProgrammingError):
            return True

    def save_model(self, request, obj, form, change):
        existing = GlobalBettingSettings.objects.filter(pk=obj.pk).first()
        obj.updated_by = request.user
        if not obj.created_by:
            obj.created_by = request.user
        obj.full_clean()
        super().save_model(request, obj, form, change)

        changed = {}
        if existing:
            for f in ['is_active', 'betting_enabled', 'min_stake', 'max_stake', 'max_winning', 'max_stake_by_ticket_type', 'max_winning_by_ticket_type', 'max_odds_per_ticket', 'max_selections_per_ticket', 'max_payout_per_day', 'max_payout_per_user_per_day']:
                old_val = getattr(existing, f)
                new_val = getattr(obj, f)
                if old_val != new_val:
                    changed[f] = {'old': str(old_val) if old_val is not None else None, 'new': str(new_val) if new_val is not None else None}
        else:
            changed['created'] = True

        BettingLimitAuditLog.objects.create(
            action_type='GLOBAL_UPDATE',
            actor=request.user,
            ip_address=(request.META.get('HTTP_X_FORWARDED_FOR', '').split(',')[0].strip() or request.META.get('REMOTE_ADDR')),
            message='Global betting settings updated.',
            data={'changed': changed}
        )

class AgentBettingLimitOverrideAdmin(admin.ModelAdmin):
    list_display = ('agent', 'is_active', 'custom_limits_enabled', 'min_stake', 'max_stake', 'max_winning', 'updated_at')
    list_filter = ('is_active', 'custom_limits_enabled')
    search_fields = ('agent__email', 'agent__username', 'agent__first_name', 'agent__last_name', 'agent__phone_number')
    autocomplete_fields = ('agent',)
    readonly_fields = ('created_at', 'updated_at', 'created_by', 'updated_by')
    actions = ('activate_overrides', 'deactivate_overrides', 'enable_custom_limits', 'disable_custom_limits')

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser

    def save_model(self, request, obj, form, change):
        existing = AgentBettingLimitOverride.objects.filter(pk=obj.pk).select_related('agent').first()
        obj.updated_by = request.user
        if not obj.created_by:
            obj.created_by = request.user
        obj.full_clean()
        super().save_model(request, obj, form, change)

        changed = {}
        if existing:
            for f in ['is_active', 'custom_limits_enabled', 'min_stake', 'max_stake', 'max_winning', 'max_stake_by_ticket_type', 'max_winning_by_ticket_type', 'max_odds_per_ticket', 'max_selections_per_ticket', 'max_payout_per_agent_per_day', 'max_payout_per_user_per_day']:
                old_val = getattr(existing, f)
                new_val = getattr(obj, f)
                if old_val != new_val:
                    changed[f] = {'old': str(old_val) if old_val is not None else None, 'new': str(new_val) if new_val is not None else None}
        else:
            changed['created'] = True

        BettingLimitAuditLog.objects.create(
            action_type='AGENT_UPDATE',
            actor=request.user,
            agent=obj.agent,
            ip_address=(request.META.get('HTTP_X_FORWARDED_FOR', '').split(',')[0].strip() or request.META.get('REMOTE_ADDR')),
            message='Agent betting limits override updated.',
            data={'changed': changed}
        )

    @admin.action(description='Activate selected overrides')
    def activate_overrides(self, request, queryset):
        updated = queryset.update(is_active=True)
        BettingLimitAuditLog.objects.create(
            action_type='AGENT_UPDATE',
            actor=request.user,
            ip_address=(request.META.get('HTTP_X_FORWARDED_FOR', '').split(',')[0].strip() or request.META.get('REMOTE_ADDR')),
            message='Bulk activate agent betting limit overrides.',
            data={'count': updated, 'ids': list(queryset.values_list('id', flat=True))}
        )
        self.message_user(request, f"Activated {updated} overrides.", level=messages.SUCCESS)

    @admin.action(description='Deactivate selected overrides')
    def deactivate_overrides(self, request, queryset):
        updated = queryset.update(is_active=False)
        BettingLimitAuditLog.objects.create(
            action_type='AGENT_UPDATE',
            actor=request.user,
            ip_address=(request.META.get('HTTP_X_FORWARDED_FOR', '').split(',')[0].strip() or request.META.get('REMOTE_ADDR')),
            message='Bulk deactivate agent betting limit overrides.',
            data={'count': updated, 'ids': list(queryset.values_list('id', flat=True))}
        )
        self.message_user(request, f"Deactivated {updated} overrides.", level=messages.SUCCESS)

    @admin.action(description='Enable custom limits on selected overrides')
    def enable_custom_limits(self, request, queryset):
        updated = queryset.update(custom_limits_enabled=True)
        BettingLimitAuditLog.objects.create(
            action_type='AGENT_UPDATE',
            actor=request.user,
            ip_address=(request.META.get('HTTP_X_FORWARDED_FOR', '').split(',')[0].strip() or request.META.get('REMOTE_ADDR')),
            message='Bulk enable custom limits for agent overrides.',
            data={'count': updated, 'ids': list(queryset.values_list('id', flat=True))}
        )
        self.message_user(request, f"Enabled custom limits for {updated} overrides.", level=messages.SUCCESS)

    @admin.action(description='Disable custom limits on selected overrides')
    def disable_custom_limits(self, request, queryset):
        updated = queryset.update(custom_limits_enabled=False)
        BettingLimitAuditLog.objects.create(
            action_type='AGENT_UPDATE',
            actor=request.user,
            ip_address=(request.META.get('HTTP_X_FORWARDED_FOR', '').split(',')[0].strip() or request.META.get('REMOTE_ADDR')),
            message='Bulk disable custom limits for agent overrides.',
            data={'count': updated, 'ids': list(queryset.values_list('id', flat=True))}
        )
        self.message_user(request, f"Disabled custom limits for {updated} overrides.", level=messages.SUCCESS)


class UserBettingLimitOverrideAdmin(admin.ModelAdmin):
    list_display = ('user', 'is_active', 'custom_limits_enabled', 'min_stake', 'max_stake', 'max_winning', 'updated_at')
    list_filter = ('is_active', 'custom_limits_enabled')
    search_fields = ('user__email', 'user__username', 'user__first_name', 'user__last_name', 'user__phone_number')
    autocomplete_fields = ('user',)
    readonly_fields = ('created_at', 'updated_at', 'created_by', 'updated_by')
    actions = ('activate_overrides', 'deactivate_overrides', 'enable_custom_limits', 'disable_custom_limits')

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser

    def save_model(self, request, obj, form, change):
        existing = UserBettingLimitOverride.objects.filter(pk=obj.pk).select_related('user').first()
        obj.updated_by = request.user
        if not obj.created_by:
            obj.created_by = request.user
        obj.full_clean()
        super().save_model(request, obj, form, change)

        changed = {}
        if existing:
            for f in ['is_active', 'custom_limits_enabled', 'min_stake', 'max_stake', 'max_winning', 'max_stake_by_ticket_type', 'max_winning_by_ticket_type', 'max_odds_per_ticket', 'max_selections_per_ticket', 'max_payout_per_user_per_day']:
                old_val = getattr(existing, f)
                new_val = getattr(obj, f)
                if old_val != new_val:
                    changed[f] = {'old': str(old_val) if old_val is not None else None, 'new': str(new_val) if new_val is not None else None}
        else:
            changed['created'] = True

        BettingLimitAuditLog.objects.create(
            action_type='USER_UPDATE',
            actor=request.user,
            affected_user=obj.user,
            ip_address=(request.META.get('HTTP_X_FORWARDED_FOR', '').split(',')[0].strip() or request.META.get('REMOTE_ADDR')),
            message='User betting limits override updated.',
            data={'changed': changed}
        )

    @admin.action(description='Activate selected overrides')
    def activate_overrides(self, request, queryset):
        updated = queryset.update(is_active=True)
        BettingLimitAuditLog.objects.create(
            action_type='USER_UPDATE',
            actor=request.user,
            ip_address=(request.META.get('HTTP_X_FORWARDED_FOR', '').split(',')[0].strip() or request.META.get('REMOTE_ADDR')),
            message='Bulk activate user betting limit overrides.',
            data={'count': updated, 'ids': list(queryset.values_list('id', flat=True))}
        )
        self.message_user(request, f"Activated {updated} overrides.", level=messages.SUCCESS)

    @admin.action(description='Deactivate selected overrides')
    def deactivate_overrides(self, request, queryset):
        updated = queryset.update(is_active=False)
        BettingLimitAuditLog.objects.create(
            action_type='USER_UPDATE',
            actor=request.user,
            ip_address=(request.META.get('HTTP_X_FORWARDED_FOR', '').split(',')[0].strip() or request.META.get('REMOTE_ADDR')),
            message='Bulk deactivate user betting limit overrides.',
            data={'count': updated, 'ids': list(queryset.values_list('id', flat=True))}
        )
        self.message_user(request, f"Deactivated {updated} overrides.", level=messages.SUCCESS)

    @admin.action(description='Enable custom limits on selected overrides')
    def enable_custom_limits(self, request, queryset):
        updated = queryset.update(custom_limits_enabled=True)
        BettingLimitAuditLog.objects.create(
            action_type='USER_UPDATE',
            actor=request.user,
            ip_address=(request.META.get('HTTP_X_FORWARDED_FOR', '').split(',')[0].strip() or request.META.get('REMOTE_ADDR')),
            message='Bulk enable custom limits for user overrides.',
            data={'count': updated, 'ids': list(queryset.values_list('id', flat=True))}
        )
        self.message_user(request, f"Enabled custom limits for {updated} overrides.", level=messages.SUCCESS)

    @admin.action(description='Disable custom limits on selected overrides')
    def disable_custom_limits(self, request, queryset):
        updated = queryset.update(custom_limits_enabled=False)
        BettingLimitAuditLog.objects.create(
            action_type='USER_UPDATE',
            actor=request.user,
            ip_address=(request.META.get('HTTP_X_FORWARDED_FOR', '').split(',')[0].strip() or request.META.get('REMOTE_ADDR')),
            message='Bulk disable custom limits for user overrides.',
            data={'count': updated, 'ids': list(queryset.values_list('id', flat=True))}
        )
        self.message_user(request, f"Disabled custom limits for {updated} overrides.", level=messages.SUCCESS)


class BettingLimitAuditLogAdmin(admin.ModelAdmin):
    list_display = ('created_at', 'action_type', 'actor', 'agent', 'affected_user', 'ticket')
    list_filter = ('action_type', 'created_at')
    search_fields = ('message', 'actor__email', 'agent__email', 'affected_user__email', 'ticket__ticket_id', 'ticket__id')
    readonly_fields = [f.name for f in BettingLimitAuditLog._meta.fields]
    date_hierarchy = 'created_at'

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser


class RetailManagerMasterAgentMappingAdmin(admin.ModelAdmin):
    list_display = ('retail_manager', 'master_agent', 'created_at')
    search_fields = ('retail_manager__email', 'retail_manager__username', 'master_agent__email', 'master_agent__username')
    list_filter = ('created_at',)
    autocomplete_fields = ('retail_manager', 'master_agent')
    date_hierarchy = 'created_at'


class RetailManagerSuperAgentMappingAdmin(admin.ModelAdmin):
    list_display = ('retail_manager', 'super_agent', 'created_at')
    search_fields = ('retail_manager__email', 'retail_manager__username', 'super_agent__email', 'super_agent__username')
    list_filter = ('created_at',)
    autocomplete_fields = ('retail_manager', 'super_agent')
    date_hierarchy = 'created_at'


class RetailManagerAgentMappingAdmin(admin.ModelAdmin):
    list_display = ('retail_manager', 'agent', 'created_at')
    search_fields = ('retail_manager__email', 'retail_manager__username', 'agent__email', 'agent__username')
    list_filter = ('created_at',)
    autocomplete_fields = ('retail_manager', 'agent')
    date_hierarchy = 'created_at'

class FinanceAuditLogAdmin(admin.ModelAdmin):
    list_display = ('created_at', 'action_type', 'actor', 'target_user', 'transaction', 'withdrawal', 'ip_address')
    list_filter = ('action_type', 'created_at')
    search_fields = ('actor__email', 'target_user__email', 'reason', 'notes', 'transaction__id', 'withdrawal__id')
    readonly_fields = [f.name for f in FinanceAuditLog._meta.fields]
    date_hierarchy = 'created_at'

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser


class CRMActionLogAdmin(admin.ModelAdmin):
    list_display = ('created_at', 'action_type', 'actor', 'target_user', 'reason')
    list_filter = ('action_type', 'created_at')
    search_fields = ('actor__email', 'target_user__email', 'reason', 'notes')
    readonly_fields = [f.name for f in CRMActionLog._meta.fields]
    date_hierarchy = 'created_at'

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser


class CustomerComplaintAdmin(admin.ModelAdmin):
    list_display = ('created_at', 'complaint_type', 'user', 'subject', 'status', 'priority', 'assigned_to')
    list_filter = ('complaint_type', 'status', 'priority', 'created_at', 'resolved_at')
    search_fields = ('user__email', 'user__username', 'subject', 'description', 'assigned_to__email', 'assigned_to__username')
    autocomplete_fields = ('user', 'assigned_to', 'created_by')
    list_select_related = ('user', 'assigned_to', 'created_by')
    date_hierarchy = 'created_at'


class CustomerComplaintNoteAdmin(admin.ModelAdmin):
    list_display = ('created_at', 'complaint', 'author', 'is_internal')
    list_filter = ('is_internal', 'created_at')
    search_fields = ('complaint__subject', 'author__email', 'author__username', 'note')
    autocomplete_fields = ('complaint', 'author')
    list_select_related = ('complaint', 'author')
    date_hierarchy = 'created_at'


class BulkMessageTemplateAdmin(admin.ModelAdmin):
    list_display = ('name', 'category', 'default_channel', 'is_active', 'created_by', 'created_at')
    list_filter = ('category', 'default_channel', 'is_active', 'created_at')
    search_fields = ('name', 'subject', 'message', 'created_by__email', 'created_by__username')
    autocomplete_fields = ('created_by',)
    list_select_related = ('created_by',)
    date_hierarchy = 'created_at'


class BulkMessageCampaignAdmin(admin.ModelAdmin):
    list_display = ('created_at', 'subject', 'channel', 'target_group', 'status', 'recipients_count', 'delivered_count', 'failed_count', 'created_by')
    list_filter = ('channel', 'target_group', 'status', 'recurring_pattern', 'created_at', 'sent_at')
    search_fields = ('subject', 'message', 'created_by__email', 'created_by__username')
    autocomplete_fields = ('template', 'created_by')
    list_select_related = ('template', 'created_by')
    date_hierarchy = 'created_at'


class BulkMessageDeliveryAdmin(admin.ModelAdmin):
    list_display = ('created_at', 'campaign', 'recipient', 'channel', 'status', 'sent_at')
    list_filter = ('channel', 'status', 'created_at', 'sent_at')
    search_fields = ('campaign__subject', 'recipient__email', 'recipient__username', 'error_message')
    autocomplete_fields = ('campaign', 'recipient')
    list_select_related = ('campaign', 'recipient')
    date_hierarchy = 'created_at'


class DashboardTaskAdmin(admin.ModelAdmin):
    list_display = ('created_at', 'title', 'assigned_to', 'audience_label', 'status', 'due_at', 'completed_at', 'created_by')
    list_filter = ('status', 'assigned_to__user_type', 'created_at', 'due_at', 'completed_at')
    search_fields = ('title', 'description', 'completion_report', 'assigned_to__email', 'assigned_to__username')
    autocomplete_fields = ('assigned_to', 'created_by')
    list_select_related = ('assigned_to', 'created_by')
    date_hierarchy = 'created_at'
    readonly_fields = ('created_at', 'updated_at', 'completed_at')

    def audience_label(self, obj):
        return obj.audience_label
    audience_label.short_description = 'Dashboard'

    def save_model(self, request, obj, form, change):
        if not obj.created_by_id:
            obj.created_by = request.user
        if obj.status != DashboardTask.STATUS.COMPLETED:
            obj.completed_at = None
        elif obj.status == DashboardTask.STATUS.COMPLETED and not obj.completed_at:
            obj.completed_at = timezone.now()
        super().save_model(request, obj, form, change)


class CRMOpsAuditLogAdmin(admin.ModelAdmin):
    list_display = ('created_at', 'module', 'action', 'actor', 'target_user', 'complaint', 'campaign', 'transaction', 'ip_address')
    list_filter = ('module', 'action', 'created_at')
    search_fields = ('actor__email', 'actor__username', 'target_user__email', 'target_user__username', 'complaint__subject', 'campaign__subject', 'ip_address')
    readonly_fields = [f.name for f in CRMOpsAuditLog._meta.fields]
    autocomplete_fields = ('actor', 'target_user', 'complaint', 'campaign', 'transaction')
    list_select_related = ('actor', 'target_user', 'complaint', 'campaign', 'transaction')
    date_hierarchy = 'created_at'

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser


class AgentTransferLogAdmin(admin.ModelAdmin):
    list_display = ('created_at', 'agent', 'old_super_agent', 'new_super_agent', 'transferred_by')
    list_filter = ('created_at', 'old_super_agent', 'new_super_agent', 'transferred_by')
    search_fields = (
        'agent__username', 'agent__email', 'agent__phone_number',
        'old_super_agent__username', 'old_super_agent__email',
        'new_super_agent__username', 'new_super_agent__email',
        'transferred_by__username', 'transferred_by__email',
        'remarks',
    )
    readonly_fields = [f.name for f in AgentTransferLog._meta.fields]
    date_hierarchy = 'created_at'

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser


class AccountUnlockAppealAdmin(admin.ModelAdmin):
    list_display = ('created_at', 'locked_user', 'appealed_by', 'status', 'reviewed_at', 'reviewed_by')
    list_filter = ('status', 'created_at', 'reviewed_at')
    search_fields = (
        'locked_user__username', 'locked_user__email',
        'appealed_by__username', 'appealed_by__email',
        'reviewed_by__username', 'reviewed_by__email',
        'appeal_reason', 'admin_comment',
    )
    readonly_fields = [f.name for f in AccountUnlockAppeal._meta.fields]
    date_hierarchy = 'created_at'
    list_select_related = ('locked_user', 'appealed_by', 'reviewed_by')

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser


class AccountLockAuditLogAdmin(admin.ModelAdmin):
    list_display = ('timestamp', 'locked_user', 'action', 'locked_by', 'appealed_by', 'reviewed_by')
    list_filter = ('action', 'timestamp')
    search_fields = (
        'locked_user__username', 'locked_user__email',
        'locked_by__username', 'locked_by__email',
        'appealed_by__username', 'appealed_by__email',
        'reviewed_by__username', 'reviewed_by__email',
        'lock_reason', 'remarks',
    )
    readonly_fields = [f.name for f in AccountLockAuditLog._meta.fields]
    date_hierarchy = 'timestamp'
    list_select_related = ('locked_user', 'locked_by', 'appealed_by', 'reviewed_by')

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser
# Register your models with the CUSTOM admin site
betting_admin_site.register(User, CustomUserAdmin)
betting_admin_site.register(Wallet, WalletAdmin)
betting_admin_site.register(WalletLedgerEntry, WalletLedgerEntryAdmin)
betting_admin_site.register(Transaction, TransactionAdmin)
betting_admin_site.register(PaymentGatewayDeposit, PaymentGatewayDepositAdmin)
betting_admin_site.register(PendingCashierRegistration, PendingCashierRegistrationAdmin)
betting_admin_site.register(ApprovedNewCashier, ApprovedNewCashierAdmin)
betting_admin_site.register(BettingPeriod, BettingPeriodAdmin)
betting_admin_site.register(PopularPick, PopularPickAdmin)
betting_admin_site.register(Fixture, FixtureAdmin)
betting_admin_site.register(Result, ResultAdmin)
betting_admin_site.register(BetTicket, BetTicketAdmin)
betting_admin_site.register(BonusRule, BonusRuleAdmin)
betting_admin_site.register(SystemSetting)
betting_admin_site.register(UserWithdrawal, UserWithdrawalAdmin)
betting_admin_site.register(WithdrawalReport, WithdrawalReportAdmin)
betting_admin_site.register(GlobalBettingSettings, GlobalBettingSettingsAdmin)
betting_admin_site.register(AgentBettingLimitOverride, AgentBettingLimitOverrideAdmin)
betting_admin_site.register(UserBettingLimitOverride, UserBettingLimitOverrideAdmin)
betting_admin_site.register(BettingLimitAuditLog, BettingLimitAuditLogAdmin)
betting_admin_site.register(RetailManagerMasterAgentMapping, RetailManagerMasterAgentMappingAdmin)
betting_admin_site.register(RetailManagerSuperAgentMapping, RetailManagerSuperAgentMappingAdmin)
betting_admin_site.register(RetailManagerAgentMapping, RetailManagerAgentMappingAdmin)
betting_admin_site.register(FinanceAuditLog, FinanceAuditLogAdmin)
betting_admin_site.register(CRMActionLog, CRMActionLogAdmin)
betting_admin_site.register(CustomerComplaint, CustomerComplaintAdmin)
betting_admin_site.register(CustomerComplaintNote, CustomerComplaintNoteAdmin)
betting_admin_site.register(DashboardTask, DashboardTaskAdmin)
betting_admin_site.register(BulkMessageTemplate, BulkMessageTemplateAdmin)
betting_admin_site.register(BulkMessageCampaign, BulkMessageCampaignAdmin)
betting_admin_site.register(BulkMessageDelivery, BulkMessageDeliveryAdmin)
betting_admin_site.register(CRMOpsAuditLog, CRMOpsAuditLogAdmin)
betting_admin_site.register(AgentTransferLog, AgentTransferLogAdmin)
betting_admin_site.register(AccountUnlockAppeal, AccountUnlockAppealAdmin)
betting_admin_site.register(AccountLockAuditLog, AccountLockAuditLogAdmin)

from void_requests.models import CashierVoidPermission, TicketVoidAuditLog, TicketVoidRequest
from void_requests.admin import CashierVoidPermissionAdmin, TicketVoidAuditLogAdmin, TicketVoidRequestAdmin

betting_admin_site.register(TicketVoidRequest, TicketVoidRequestAdmin)
betting_admin_site.register(TicketVoidAuditLog, TicketVoidAuditLogAdmin)
betting_admin_site.register(CashierVoidPermission, CashierVoidPermissionAdmin)

from risk.models import (
    RiskEngineSettings,
    FixtureRiskState,
    MarketRiskState,
    SelectionRiskState,
    FixtureLiabilitySnapshot,
    MarketLiabilitySnapshot,
    SelectionLiabilitySnapshot,
    AgentExposureSnapshot,
    UserExposureSnapshot,
    BettingPeriodLiabilitySnapshot,
    RiskAuditLog,
    SuspiciousActivityLog,
    SharpBettorProfile,
    DeviceFingerprint,
    SyndicateGroup,
    SyndicateMember,
    DuplicateTicketLog,
    ArbitrageAlert,
    IPWhitelistEntry,
    IPIntelligence,
)
from risk.admin import (
    RiskEngineSettingsAdmin,
    FixtureRiskStateAdmin,
    MarketRiskStateAdmin,
    SelectionRiskStateAdmin,
    FixtureLiabilitySnapshotAdmin,
    MarketLiabilitySnapshotAdmin,
    SelectionLiabilitySnapshotAdmin,
    AgentExposureSnapshotAdmin,
    UserExposureSnapshotAdmin,
    BettingPeriodLiabilitySnapshotAdmin,
    RiskAuditLogAdmin,
    SuspiciousActivityLogAdmin,
    SharpBettorProfileAdmin,
    DeviceFingerprintAdmin,
    SyndicateGroupAdmin,
    SyndicateMemberAdmin,
    DuplicateTicketLogAdmin,
    ArbitrageAlertAdmin,
    IPWhitelistEntryAdmin,
    IPIntelligenceAdmin,
)

betting_admin_site.register(RiskEngineSettings, RiskEngineSettingsAdmin)
betting_admin_site.register(FixtureRiskState, FixtureRiskStateAdmin)
betting_admin_site.register(MarketRiskState, MarketRiskStateAdmin)
betting_admin_site.register(SelectionRiskState, SelectionRiskStateAdmin)
betting_admin_site.register(FixtureLiabilitySnapshot, FixtureLiabilitySnapshotAdmin)
betting_admin_site.register(MarketLiabilitySnapshot, MarketLiabilitySnapshotAdmin)
betting_admin_site.register(SelectionLiabilitySnapshot, SelectionLiabilitySnapshotAdmin)
betting_admin_site.register(AgentExposureSnapshot, AgentExposureSnapshotAdmin)
betting_admin_site.register(UserExposureSnapshot, UserExposureSnapshotAdmin)
betting_admin_site.register(BettingPeriodLiabilitySnapshot, BettingPeriodLiabilitySnapshotAdmin)
betting_admin_site.register(RiskAuditLog, RiskAuditLogAdmin)
betting_admin_site.register(SuspiciousActivityLog, SuspiciousActivityLogAdmin)
betting_admin_site.register(SharpBettorProfile, SharpBettorProfileAdmin)
betting_admin_site.register(DeviceFingerprint, DeviceFingerprintAdmin)
betting_admin_site.register(SyndicateGroup, SyndicateGroupAdmin)
betting_admin_site.register(SyndicateMember, SyndicateMemberAdmin)
betting_admin_site.register(DuplicateTicketLog, DuplicateTicketLogAdmin)
betting_admin_site.register(ArbitrageAlert, ArbitrageAlertAdmin)
betting_admin_site.register(IPWhitelistEntry, IPWhitelistEntryAdmin)
betting_admin_site.register(IPIntelligence, IPIntelligenceAdmin)

from notifications.models import Notification, SystemAnnouncement, WebPushSubscription, NotificationCampaign
from notifications.admin import NotificationAdmin, SystemAnnouncementAdmin, WebPushSubscriptionAdmin, NotificationCampaignAdmin

betting_admin_site.register(Notification, NotificationAdmin)
betting_admin_site.register(SystemAnnouncement, SystemAnnouncementAdmin)
betting_admin_site.register(WebPushSubscription, WebPushSubscriptionAdmin)
betting_admin_site.register(NotificationCampaign, NotificationCampaignAdmin)

# --- Processed Withdrawal Admin (Audit) ---
class ProcessedWithdrawalAdmin(admin.ModelAdmin):
    list_display = (
        'id',
        'user',
        'amount',
        'balance_before_display',
        'balance_after_display',
        'approved_rejected_by_display',
        'approver_balance_before_display',
        'approver_balance_after_display',
        'status_badge',
        'approved_rejected_time_display',
    )
    list_filter = ('approved_rejected_time', 'approved_rejected_by', 'user')
    search_fields = ('user__email', 'user__username', 'id', 'processed_ip')
    readonly_fields = [field.name for field in UserWithdrawal._meta.fields] + ['balance_before', 'balance_after', 'approver_balance_before', 'approver_balance_after', 'processed_ip']
    date_hierarchy = 'approved_rejected_time'
    ordering = ('-approved_rejected_time',)
    change_list_template = 'betting/admin/processed_withdrawal_change_list.html'

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.filter(status__in=['approved', 'completed', 'rejected']).select_related('user', 'approved_rejected_by')

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
    
    def has_change_permission(self, request, obj=None):
        return False

    def status_badge(self, obj):
        color = 'green' if obj.status in ['approved', 'completed'] else 'gray'
        return format_html(
            '<span style="color: white; background-color: {}; padding: 3px 10px; border-radius: 5px;">{}</span>',
            color,
            obj.get_status_display()
        )
    status_badge.short_description = 'Status'

    def _ensure_cache(self, result_list):
        self._pw_cache = {
            'wallet_map': {},
            'approver_wallet_map': {},
            'activity_map': {},
        }

        ids = [obj.id for obj in result_list]
        if not ids:
            return

        user_ids = {obj.user_id for obj in result_list if obj.user_id}
        approver_ids = {obj.approved_rejected_by_id for obj in result_list if obj.approved_rejected_by_id}

        affected_objects = [f"Withdrawal: {i}" for i in ids]
        logs = (
            ActivityLog.objects
            .filter(affected_object__in=affected_objects, action_type='UPDATE')
            .select_related('user')
            .order_by('affected_object', '-timestamp')
        )
        seen = set()
        for log in logs:
            key = log.affected_object
            if key in seen:
                continue
            seen.add(key)
            self._pw_cache['activity_map'][key] = log
            if log.user_id:
                approver_ids.add(log.user_id)

        self._pw_cache['wallet_map'] = {
            row['user_id']: row['balance']
            for row in Wallet.objects.filter(user_id__in=list(user_ids)).values('user_id', 'balance')
        }
        self._pw_cache['approver_wallet_map'] = {
            row['user_id']: row['balance']
            for row in Wallet.objects.filter(user_id__in=list(approver_ids)).values('user_id', 'balance')
        }

    def changelist_view(self, request, extra_context=None):
        response = super().changelist_view(request, extra_context)
        try:
            cl = response.context_data['cl']
        except (AttributeError, KeyError):
            return response

        self._pw_cache = {}
        try:
            self._ensure_cache(cl.result_list)
        except Exception:
            self._pw_cache = {}

        try:
            qs = cl.queryset
        except Exception:
            return response

        total = qs.aggregate(Sum('amount'))['amount__sum'] or Decimal('0.00')
        extra_context = extra_context or {}
        extra_context['total_withdrawal_amount'] = total
        if hasattr(response, 'context_data'):
            response.context_data.update(extra_context)
        return response

    def _fmt_money(self, value):
        if value is None:
            return "-"
        try:
            return f"₦{Decimal(value):,.2f}"
        except Exception:
            return str(value)

    def _activity_for(self, obj):
        cache = getattr(self, '_pw_cache', {}) or {}
        return cache.get('activity_map', {}).get(f"Withdrawal: {obj.id}")

    def balance_before_display(self, obj):
        if obj.balance_before is not None:
            return self._fmt_money(obj.balance_before)
        cache = getattr(self, '_pw_cache', {}) or {}
        wallet_bal = cache.get('wallet_map', {}).get(obj.user_id)
        if wallet_bal is None:
            return "-"
        if obj.status == 'rejected':
            return self._fmt_money(wallet_bal)
        return self._fmt_money(wallet_bal + obj.amount)
    balance_before_display.short_description = 'Balance before'

    def balance_after_display(self, obj):
        if obj.balance_after is not None:
            return self._fmt_money(obj.balance_after)
        cache = getattr(self, '_pw_cache', {}) or {}
        wallet_bal = cache.get('wallet_map', {}).get(obj.user_id)
        if wallet_bal is None:
            return "-"
        if obj.status == 'rejected':
            return self._fmt_money(wallet_bal - obj.amount)
        return self._fmt_money(wallet_bal)
    balance_after_display.short_description = 'Balance after'

    def approved_rejected_by_display(self, obj):
        if obj.approved_rejected_by_id:
            return obj.approved_rejected_by
        log = self._activity_for(obj)
        return getattr(log, 'user', None) or "-"
    approved_rejected_by_display.short_description = 'Approved rejected by'

    def approver_balance_before_display(self, obj):
        if obj.approver_balance_before is not None:
            return self._fmt_money(obj.approver_balance_before)
        approver_id = obj.approved_rejected_by_id
        if not approver_id:
            log = self._activity_for(obj)
            approver_id = getattr(log, 'user_id', None)
        cache = getattr(self, '_pw_cache', {}) or {}
        bal = cache.get('approver_wallet_map', {}).get(approver_id)
        return self._fmt_money(bal) if bal is not None else "-"
    approver_balance_before_display.short_description = 'Approver balance before'

    def approver_balance_after_display(self, obj):
        if obj.approver_balance_after is not None:
            return self._fmt_money(obj.approver_balance_after)
        approver_id = obj.approved_rejected_by_id
        if not approver_id:
            log = self._activity_for(obj)
            approver_id = getattr(log, 'user_id', None)
        cache = getattr(self, '_pw_cache', {}) or {}
        bal = cache.get('approver_wallet_map', {}).get(approver_id)
        return self._fmt_money(bal) if bal is not None else "-"
    approver_balance_after_display.short_description = 'Approver balance after'

    def approved_rejected_time_display(self, obj):
        if obj.approved_rejected_time:
            return obj.approved_rejected_time
        log = self._activity_for(obj)
        if log and log.timestamp:
            return log.timestamp
        return obj.request_time
    approved_rejected_time_display.short_description = 'Approved rejected time'

betting_admin_site.register(ProcessedWithdrawal, ProcessedWithdrawalAdmin)
# Activity Log Admin
class ActivityLogAdmin(admin.ModelAdmin):
    list_display = ('timestamp', 'user', 'action_type_badge', 'amount_display', 'affected_object', 'ip_address', 'isp')
    list_filter = ('action_type', 'timestamp', 'user')
    search_fields = ('user__username', 'user__email', 'ip_address', 'isp', 'action', 'affected_object')
    readonly_fields = [field.name for field in ActivityLog._meta.fields] + ['amount_display']
    list_per_page = 50
    date_hierarchy = 'timestamp'
    list_select_related = ('user',)
    
    def has_add_permission(self, request):
        return False
        
    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser

    def amount_display(self, obj):
        """Extracts and displays amount from action description or affected object."""
        import re
        
        # Strategy 1: Regex search in action description
        # Looks for patterns like "Amount: 100", "Stake: 50", "Credit of 500"
        amount_patterns = [
            r'Amount:\s*([\d\.,]+)',
            r'Stake:\s*([\d\.,]+)',
            r'Credit\s*of\s*([\d\.,]+)',
            r'Debit\s*of\s*([\d\.,]+)',
            r'Transfer\s*([\d\.,]+)',
        ]
        
        for pattern in amount_patterns:
            match = re.search(pattern, obj.action, re.IGNORECASE)
            if match:
                return match.group(1)
        
        # Strategy 2: Try to find related transaction via affected_object string
        # If affected_object is "Transaction: <uuid>"
        if obj.affected_object and "Transaction" in obj.affected_object:
            try:
                # Extract UUID if present (simple split or regex)
                # Assuming format "Transaction: <uuid>"
                parts = obj.affected_object.split(':')
                if len(parts) > 1:
                    tx_id = parts[1].strip()
                    from .models import Transaction
                    tx = Transaction.objects.filter(id=tx_id).first()
                    if tx:
                        return f"{tx.amount} ({tx.get_transaction_type_display()})"
            except Exception:
                pass
                
        return "-"
    amount_display.short_description = "Amount"

    def action_type_badge(self, obj):
        from django.utils.html import format_html
        colors = {
            'CREATE': 'green',
            'UPDATE': 'orange',
            'DELETE': 'red',
            'LOGIN': 'blue',
            'LOGOUT': 'gray',
            'BET_PLACED': 'purple',
            'PAYOUT': 'gold',
        }
        action_type_value = obj.action_type or 'UNKNOWN'
        color = colors.get(action_type_value, 'black')
        return format_html(
            '<span style="color: white; background-color: {}; padding: 3px 10px; border-radius: 5px; font-weight: bold;">{}</span>',
            color,
            action_type_value
        )
    action_type_badge.short_description = 'Action'

betting_admin_site.register(ActivityLog, ActivityLogAdmin)

if PeriodicTask is not None:
    betting_admin_site.register(PeriodicTask, PeriodicTaskAdmin)
    betting_admin_site.register(IntervalSchedule)
    betting_admin_site.register(CrontabSchedule)
    betting_admin_site.register(SolarSchedule)
    betting_admin_site.register(ClockedSchedule, ClockedScheduleAdmin)
    betting_admin_site.register(TaskResult, TaskResultAdmin)
    betting_admin_site.register(GroupResult, GroupResultAdmin)

# Site Configuration Admin
class SiteConfigurationAdmin(admin.ModelAdmin):
    fieldsets = (
        ('General Settings', {
            'fields': ('site_name', 'logo', 'favicon', 'landing_page_background', 'show_ticket_status_on_landing', 'carousel_interval')
        }),
        ('Commission Settings', {
            'fields': ('commission_payment_source', 'account_user_commission_authority', 'require_commission_recall_approval')
        }),
        ('Ticket Void Settings', {
            'fields': ('enable_global_cashier_voiding',),
        }),
        ('CRM Operations', {
            'fields': ('crm_large_deposit_threshold', 'crm_failed_deposit_repeat_threshold'),
            'description': 'Thresholds used by CRM deposit monitoring and failed deposit flagging.',
        }),
        ('Loan / Overdraft Settings', {
            'fields': (
                'loan_min_ticket_count',
                'loan_min_deposit_amount',
                'loan_percentage',
                'loan_application_day',
                'loan_application_time',
                'loan_repayment_day',
                'loan_repayment_time',
            ),
            'description': 'Business rules that control overdraft qualification, opening window, and repayment deadline.',
        }),
        ('Bet Permission Settings', {
            'fields': ('allow_single_bet', 'allow_double_bet', 'allow_multiple_bet'),
            'description': 'Configure which types of bets are allowed based on the number of selections.'
        }),
        ('Navbar Customization', {
            'fields': ('navbar_text_type', 'navbar_gradient_start', 'navbar_gradient_end', 'navbar_link_hover_color')
        }),
    )

    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)
        # Set input_type to 'color' to render the native color picker
        form.base_fields['navbar_gradient_start'].widget.input_type = 'color'
        form.base_fields['navbar_gradient_end'].widget.input_type = 'color'
        form.base_fields['navbar_link_hover_color'].widget.input_type = 'color'
        return form

    def has_add_permission(self, request):
        if self.model.objects.exists():
            return False
        return super().has_add_permission(request)

    def has_delete_permission(self, request, obj=None):
        return False

class CarouselImageAdmin(admin.ModelAdmin):
    list_display = ('id', 'title', 'is_active', 'order', 'created_at')
    list_editable = ('is_active', 'order')
    list_filter = ('is_active',)

class PasswordResetRequestAdmin(admin.ModelAdmin):
    list_display = ('email', 'user', 'created_at', 'expires_at', 'email_sent', 'is_used', 'ip_address')
    list_filter = ('is_used', 'created_at')
    search_fields = ('email', 'user__email', 'token')
    readonly_fields = ('created_at', 'token', 'expires_at', 'user_agent', 'ip_address', 'email_sent', 'sent_at', 'send_error')

class StateAdmin(admin.ModelAdmin):
    list_display = ('state_name', 'abbreviation')
    search_fields = ('state_name', 'abbreviation')

betting_admin_site.register(CarouselImage, CarouselImageAdmin)
betting_admin_site.register(PasswordResetRequest, PasswordResetRequestAdmin)
betting_admin_site.register(State, StateAdmin)
betting_admin_site.register(SiteConfiguration, SiteConfigurationAdmin)


class FooterPageAdminForm(forms.ModelForm):
    class Meta:
        model = FooterPage
        fields = '__all__'
        widgets = {
            'content': CKEditor5Widget(config_name='default'),
        }


class FooterPageAdmin(admin.ModelAdmin):
    form = FooterPageAdminForm
    list_display = ('footer_label', 'slug', 'is_active', 'show_in_footer', 'order', 'updated_at')
    list_editable = ('is_active', 'show_in_footer', 'order')
    search_fields = ('footer_label', 'slug', 'title')
    prepopulated_fields = {'slug': ('footer_label',)}


class FooterBadgeAdmin(admin.ModelAdmin):
    list_display = ('id', 'alt_text', 'is_active', 'order', 'uploaded_at')
    list_editable = ('is_active', 'order')
    search_fields = ('alt_text', 'link_url')


betting_admin_site.register(FooterPage, FooterPageAdmin)
betting_admin_site.register(FooterBadge, FooterBadgeAdmin)

# --- Credit & Loan Admin ---

@admin.register(CreditRequest)
class CreditRequestAdmin(admin.ModelAdmin):
    list_display = ('requester', 'recipient', 'amount', 'request_type', 'status', 'created_at')
    list_filter = ('status', 'request_type', 'created_at')
    search_fields = ('requester__email', 'recipient__email', 'reason')
    readonly_fields = ('created_at', 'updated_at')


class CRMWalletApprovalRequestAdmin(admin.ModelAdmin):
    list_display = (
        'created_at',
        'requester',
        'recipient',
        'request_type',
        'amount',
        'status',
        'approved_by_display',
        'wallet_flow_display',
        'approval_actions',
    )
    list_filter = ('status', 'request_type', 'created_at')
    search_fields = ('requester__email', 'recipient__email', 'reason')
    readonly_fields = (
        'requester',
        'recipient',
        'amount',
        'reason',
        'request_type',
        'status',
        'approved_by_display',
        'wallet_flow_display',
        'created_at',
        'updated_at',
    )
    fields = readonly_fields

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .filter(request_type__in=views.CRM_WALLET_APPROVAL_REQUEST_TYPES)
            .select_related('requester', 'recipient')
        )

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser

    def _account_user_wallet_choices(self):
        account_users = list(
            User.objects.filter(is_active=True, user_type='account_user')
            .order_by('email')
        )
        balances = {
            wallet.user_id: wallet.balance
            for wallet in Wallet.objects.filter(user_id__in=[u.id for u in account_users])
        }
        for account_user in account_users:
            account_user.wallet_balance = balances.get(account_user.id, Decimal('0.00'))
        return account_users

    def _get_crm_approval_log(self, obj):
        cached = getattr(obj, '_crm_approval_log_cache', None)
        if cached is not None:
            return cached
        log = CRMActionLog.objects.filter(
            target_user=obj.requester,
            action_type__in=['WALLET_CREDITED', 'WALLET_DEBITED'],
            data__request_id=obj.id,
        ).order_by('-created_at').first()
        obj._crm_approval_log_cache = log
        return log

    def approved_by_display(self, obj):
        if obj.status != 'approved':
            return '-'
        log = self._get_crm_approval_log(obj)
        if not log:
            return '-'
        approved_by = log.data.get('approved_by') or getattr(log.actor, 'email', '') or '-'
        approved_by_role = log.data.get('approved_by_role') or '-'
        return format_html(
            '{}<div class="small" style="color:#6c757d;">{}</div>',
            approved_by,
            approved_by_role.replace('_', ' ').title(),
        )
    approved_by_display.short_description = 'Approved By'

    def wallet_flow_display(self, obj):
        if obj.status != 'approved':
            return '-'
        log = self._get_crm_approval_log(obj)
        if not log:
            return '-'

        if obj.request_type == 'crm_credit':
            funding_mode = log.data.get('funding_mode')
            funding_email = log.data.get('funding_account_user_email')
            if funding_mode == 'superadmin_override':
                return 'No wallet debit'
            if funding_mode == 'account_user_wallet' and funding_email:
                return format_html('Debited<div class="small" style="color:#6c757d;">{}</div>', funding_email)
            return 'Debited approver wallet'

        reimbursement_mode = log.data.get('reimbursement_mode')
        reimbursement_email = log.data.get('reimbursement_account_user_email')
        if reimbursement_mode == 'account_user_wallet' and reimbursement_email:
            return format_html('Reimbursed<div class="small" style="color:#6c757d;">{}</div>', reimbursement_email)
        return 'Reimbursed approver wallet'
    wallet_flow_display.short_description = 'Wallet Flow'

    def get_urls(self):
        opts = self.model._meta
        custom_urls = [
            path(
                '<int:request_id>/<str:action>/',
                self.admin_site.admin_view(self.process_request_view),
                name=f'{opts.app_label}_{opts.model_name}_process',
            ),
        ]
        return custom_urls + super().get_urls()

    def approval_actions(self, obj):
        if obj.status != 'pending':
            return '-'
        opts = self.model._meta
        approve_url = reverse(f'{self.admin_site.name}:{opts.app_label}_{opts.model_name}_process', args=[obj.pk, 'approve'])
        decline_url = reverse(f'{self.admin_site.name}:{opts.app_label}_{opts.model_name}_process', args=[obj.pk, 'decline'])
        return format_html(
            '<a class="button" href="{}" style="margin-right:6px; background:#198754; color:#fff; padding:4px 8px; border-radius:4px; text-decoration:none;">Approve</a>'
            '<a class="button" href="{}" style="background:#dc3545; color:#fff; padding:4px 8px; border-radius:4px; text-decoration:none;">Decline</a>',
            approve_url,
            decline_url,
        )
    approval_actions.short_description = 'Actions'

    @db_transaction.atomic
    def process_request_view(self, request, request_id, action):
        credit_req = get_object_or_404(
            CreditRequest.objects.select_related('requester', 'recipient'),
            id=request_id,
            request_type__in=views.CRM_WALLET_APPROVAL_REQUEST_TYPES,
        )
        opts = self.model._meta
        changelist_url = reverse(f'{self.admin_site.name}:{opts.app_label}_{opts.model_name}_changelist')
        action = (action or '').strip().lower()
        if action not in {'approve', 'decline'}:
            self.message_user(request, "Invalid request action.", level=messages.ERROR)
            return redirect(changelist_url)

        if request.method == 'POST':
            selected_account_user = None
            selected_account_user_id = (request.POST.get('account_user_wallet_user_id') or '').strip()
            if selected_account_user_id:
                selected_account_user = User.objects.filter(
                    id=selected_account_user_id,
                    is_active=True,
                    user_type='account_user',
                ).first()
            try:
                message_text, message_level = views.process_credit_request_decision(
                    actor=request.user,
                    credit_req=credit_req,
                    action=action,
                    account_user_wallet_user=selected_account_user,
                )
                self.message_user(request, message_text, level=message_level)
                return redirect(changelist_url)
            except views.CreditRequestProcessError as exc:
                self.message_user(request, str(exc), level=messages.ERROR)

        context = {
            **self.admin_site.each_context(request),
            'opts': opts,
            'title': f'Confirm {"approval" if action == "approve" else "decline"}',
            'request_obj': credit_req,
            'action': action,
            'action_label': 'Approve' if action == 'approve' else 'Decline',
            'changelist_url': changelist_url,
            'account_user_choices': self._account_user_wallet_choices(),
            'selected_account_user_id': (request.POST.get('account_user_wallet_user_id') or '').strip(),
        }
        return render(request, 'admin/betting/crm_wallet_approval_confirm.html', context)

@admin.register(Loan)
class LoanAdmin(admin.ModelAdmin):
    list_display = (
        'id',
        'borrower',
        'lender',
        'loan_type',
        'approval_level',
        'requested_amount',
        'amount',
        'outstanding_balance',
        'status',
        'due_date',
        'approved_by',
        'created_at',
    )
    list_filter = ('status', 'loan_type', 'approval_level', 'manual_assignment', 'account_locked_due_to_default', 'created_at')
    search_fields = ('borrower__email', 'borrower__username', 'lender__email', 'lender__username', 'rejection_reason', 'request_reason')
    readonly_fields = (
        'created_at',
        'approved_at',
        'rejected_at',
        'settled_at',
        'qualification_ticket_count',
        'qualification_deposit_volume',
        'workflow_snapshot',
    )

    def get_queryset(self, request):
        return super().get_queryset(request).select_related(
            'borrower', 'lender', 'approved_by', 'rejected_by', 'overdraft_wallet'
        )

class OverdraftWalletAdmin(admin.ModelAdmin):
    list_display = ('super_agent', 'total_funded', 'used_balance', 'current_balance', 'updated_at')
    search_fields = ('super_agent__email', 'super_agent__username')
    readonly_fields = ('created_at', 'updated_at', 'used_balance', 'remaining_balance')

    def get_queryset(self, request):
        return super().get_queryset(request).select_related('super_agent')

class OverdraftWalletLedgerEntryAdmin(admin.ModelAdmin):
    list_display = ('wallet', 'super_agent', 'direction', 'amount', 'balance_before', 'balance_after', 'reference', 'created_at')
    list_filter = ('direction', 'created_at')
    search_fields = ('super_agent__email', 'super_agent__username', 'reference', 'reason')
    readonly_fields = [field.name for field in OverdraftWalletLedgerEntry._meta.fields]

    def has_add_permission(self, request):
        return False

class LoanRepaymentAdmin(admin.ModelAdmin):
    list_display = ('loan', 'borrower', 'amount', 'source', 'recorded_by', 'created_at')
    list_filter = ('source', 'created_at')
    search_fields = ('borrower__email', 'borrower__username', 'loan__id', 'note')
    readonly_fields = [field.name for field in LoanRepayment._meta.fields]

    def has_add_permission(self, request):
        return False

class LoanAuditLogAdmin(admin.ModelAdmin):
    list_display = ('loan', 'borrower', 'performed_by', 'action', 'amount', 'created_at')
    list_filter = ('action', 'created_at')
    search_fields = ('borrower__email', 'borrower__username', 'loan__id', 'reason', 'ip_address')
    readonly_fields = [field.name for field in LoanAuditLog._meta.fields]

    def has_add_permission(self, request):
        return False

class CreditLogAdmin(admin.ModelAdmin):
    list_display = ('actor', 'target_user', 'action_type', 'amount', 'status', 'timestamp')
    list_filter = ('action_type', 'status', 'timestamp')
    search_fields = ('actor__email', 'target_user__email', 'reference_id')
    readonly_fields = ('timestamp',)

# Register these with custom admin site as well if needed
betting_admin_site.register(CreditRequest, CreditRequestAdmin)
betting_admin_site.register(CRMWalletApprovalRequest, CRMWalletApprovalRequestAdmin)
betting_admin_site.register(Loan, LoanAdmin)
betting_admin_site.register(OverdraftWallet, OverdraftWalletAdmin)
betting_admin_site.register(OverdraftWalletLedgerEntry, OverdraftWalletLedgerEntryAdmin)
betting_admin_site.register(LoanRepayment, LoanRepaymentAdmin)
betting_admin_site.register(LoanAuditLog, LoanAuditLogAdmin)
betting_admin_site.register(CreditLog, CreditLogAdmin)



@admin.register(WebAuthnCredential)
class WebAuthnCredentialAdmin(admin.ModelAdmin):
    list_display = ('user', 'device_name', 'created_at', 'last_used', 'sign_count')
    search_fields = ('user__email', 'device_name')
    readonly_fields = ('credential_id', 'public_key', 'created_at', 'last_used')
    def get_queryset(self, request):
        return super().get_queryset(request).select_related('user')
@admin.register(BiometricAuthLog)
class BiometricAuthLogAdmin(admin.ModelAdmin):
    list_display = ('user', 'action', 'status', 'ip_address', 'device_name', 'timestamp')
    list_filter = ('action', 'status', 'timestamp')
    search_fields = ('user__email', 'ip_address', 'device_name')
    readonly_fields = ('user', 'action', 'status', 'ip_address', 'device_name', 'timestamp')
    def has_add_permission(self, request):
        return False
betting_admin_site.register(LoginAttempt, LoginAttemptAdmin)
betting_admin_site.register(WebAuthnCredential, WebAuthnCredentialAdmin)

import pending_registration.admin
