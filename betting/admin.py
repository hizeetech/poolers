from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from django.db.models import Q, IntegerField, Sum
from django.db.models.functions import Cast
from django.utils import timezone
from django.db import transaction as db_transaction
from django.contrib import messages
from decimal import Decimal
from django.urls import path, reverse 
from django.shortcuts import redirect, render
from django.utils.html import format_html 

# Celery Beat and Results imports
from django_celery_beat.models import PeriodicTask, IntervalSchedule, CrontabSchedule, SolarSchedule, ClockedSchedule
from django_celery_beat.admin import PeriodicTaskAdmin, ClockedScheduleAdmin
from django_celery_results.models import TaskResult, GroupResult
from django_celery_results.admin import TaskResultAdmin, GroupResultAdmin

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
    FixtureUploadForm
)
import pandas as pd
from django.core.files.storage import FileSystemStorage
from django.conf import settings
import os
from datetime import datetime, time

from .models import (
    User, Wallet, Transaction, BettingPeriod, Fixture, BetTicket,
    BonusRule, SystemSetting, AgentPayout, UserWithdrawal, ActivityLog, Result, Selection,
    SiteConfiguration, LoginAttempt, CreditRequest, Loan, CreditLog, ImpersonationLog,
    ProcessedWithdrawal, WebAuthnCredential, BiometricAuthLog
)


# --- Custom Admin Site Definition ---
class BettingAdminSite(admin.AdminSite):
    site_header = "PoolBetBetting Admin" # Corrected from "PoolBetting Admin" for consistency, but you can change back if intended
    site_title = "PoolBetting Admin Portal"
    index_title = "Welcome to PoolBetting Administration"

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
        'email', 'first_name', 'last_name', 'user_type', 'is_staff', 'is_active',
        'is_locked', 'failed_login_attempts',
        'get_phone_number', 'get_shop_address', 'get_master_agent', 'get_super_agent', 'agent',
        'cashier_prefix', 'date_joined', 'updated_at', 'last_login', 'get_last_impersonated', 'impersonate_button'
    )
    list_filter = (
        'user_type', 'is_active', 'is_staff', 'is_locked', 
        'date_joined', 'last_login'
    )
    search_fields = (
        'email', 'first_name', 'last_name', 'phone_number'
    )
    ordering = (
        'email',
    )
    
    actions = ['unlock_accounts', 'impersonate_user_action']

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

    def unlock_accounts(self, request, queryset):
        updated_count = queryset.update(
            is_locked=False,
            failed_login_attempts=0,
            last_failed_login=None,
            locked_at=None,
            lock_reason=None
        )
        
        # Log the unlock action for each user
        for user in queryset:
            LoginAttempt.objects.create(
                user=user,
                username_attempted=user.email,
                ip_address=request.META.get('REMOTE_ADDR'),
                user_agent=request.META.get('HTTP_USER_AGENT', ''),
                status='unlocked'
            )
            
        self.message_user(request, f"{updated_count} account(s) successfully unlocked.")
    unlock_accounts.short_description = "Unlock selected accounts"

    fieldsets = (
        (None, {'fields': ('email', 'password')}), 
        ('Personal info', {'fields': ('first_name', 'last_name', 'phone_number', 'shop_address')}),
        ('Permissions', {'fields': ('is_active', 'is_staff', 'is_superuser', 'can_manage_downline_wallets', 'groups', 'user_permissions', 'user_type')}),
        ('Hierarchy', {'fields': ('master_agent', 'super_agent', 'agent', 'cashier_prefix')}),
        ('Important dates', {'fields': ('last_login', 'date_joined')}),
        ('Security & Locking', {'fields': ('is_locked', 'failed_login_attempts', 'last_failed_login', 'locked_at', 'lock_reason')}),
    )

    add_fieldsets = (
        (None, {
            'classes': ('wide',),
            'fields': (
                'email', 'password', 'password2', 
                'first_name', 'last_name', 'phone_number', 'shop_address',
                'user_type', 'is_active', 'is_staff', 'is_superuser', 'can_manage_downline_wallets', 
                'groups', 'user_permissions', 
                'master_agent', 'super_agent', 'agent', 'cashier_prefix'
            ),
        }),
    )
    
    readonly_fields = ('last_login', 'date_joined',) 

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
        action_description = f"User '{obj.email}' {'updated' if change else 'created'}."
        views.log_admin_activity(request, action_description)

        # The password setting and user type related staff/superuser status
        # are now largely handled within the custom AdminUserCreationForm/AdminUserChangeForm save methods.
        # Call the form's save method explicitly if you need its custom logic to run.
        # Otherwise, super().save_model will call obj.save() and form.save() as needed.
        super().save_model(request, obj, form, change)

        # Auto-create cashiers for Agent
        if not change and obj.user_type == 'agent':
            password = form.cleaned_data.get('password')
            if password:
                for i in range(1, 3):
                    base_cashier_email = f"{obj.cashier_prefix}-CSH-{i:02d}"
                    cashier_email = f"{base_cashier_email}@cashier.com"
                    cashier_prefix_for_cashier = f"{obj.cashier_prefix}-{i:02d}"
                    
                    if not User.objects.filter(email=cashier_email).exists():
                        User.objects.create_user(
                            email=cashier_email,
                            password=password,
                            first_name=f"Cashier {i} ({obj.first_name})",
                            last_name=f"{obj.last_name}",
                            user_type='cashier',
                            agent=obj,
                            master_agent=obj.master_agent,
                            super_agent=obj.super_agent,
                            is_active=True,
                            is_staff=True,
                            is_superuser=False,
                            cashier_prefix=cashier_prefix_for_cashier
                        )
                        messages.info(request, f"Cashier account created: {cashier_email}")

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
    readonly_fields = ('fixture', 'bet_type', 'odd_selected', 'is_winning_selection')
    can_delete = False


# --- BetTicket Admin (Registered with custom site) ---
class BetTicketAdmin(admin.ModelAdmin):
    list_display = (
        'ticket_id', 'user', 'stake_amount', 'total_odd', 'potential_winning',
        'max_winning', 'status', 'placed_at', 'deleted_by', 'deleted_at'
    )
    list_filter = ('status', 'placed_at', 'user')
    search_fields = ('ticket_id', 'id__startswith', 'user__email__icontains')
    raw_id_fields = ('user', 'deleted_by')
    ordering = ('-placed_at',)
    inlines = [SelectionInline]

    def save_model(self, request, obj, form, change):
        if change:
             # Check if status is changing to cancelled/deleted and deleted_by is not set
             if obj.status in ['cancelled', 'deleted'] and not obj.deleted_by:
                 obj.deleted_by = request.user
                 obj.deleted_at = timezone.now()
        super().save_model(request, obj, form, change)

    actions = ['void_selected_tickets', 'settle_won_selected_tickets']

    @admin.action(description='Void/Delete selected bet tickets and refund stake')
    def void_selected_tickets(self, request, queryset):
        tickets_voided = 0
        tickets_failed = 0
        
        with db_transaction.atomic():
            for ticket in queryset:
                if ticket.status in ['won', 'lost', 'cashed_out', 'deleted', 'cancelled']: 
                    messages.warning(request, f"Ticket {ticket.ticket_id} is already '{ticket.status}' and cannot be voided/deleted.")
                    tickets_failed += 1
                    continue
                
                try:
                    ticket.status = 'deleted'
                    ticket.deleted_by = request.user
                    ticket.deleted_at = timezone.now()
                    ticket.save() # Signal handles refund

                    tickets_voided += 1
                    views.log_admin_activity(request, f"Voided/Deleted bet ticket {ticket.ticket_id} and refunded stake.") 
                except Exception as e:
                    messages.error(request, f"Failed to void ticket {ticket.ticket_id}: {e}")
                    tickets_failed += 1

        if tickets_voided > 0:
            messages.success(request, f"Successfully voided/deleted {tickets_voided} bet tickets.")
        if tickets_failed > 0:
            messages.warning(request, f"Failed to void/delete {tickets_failed} bet tickets.")

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
                    user_wallet.balance += winnings_amount
                    user_wallet.save()

                    Transaction.objects.create(
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
    list_display = ('name', 'start_date', 'end_date', 'is_active')
    list_editable = ('is_active',)
    search_fields = ('name',)
    ordering = ('-start_date',)

# --- Fixture Admin ---
class FixtureAdmin(admin.ModelAdmin):
    form = FixtureForm
    list_display = ('home_team', 'away_team', 'match_date', 'match_time', 'betting_period', 'serial_number_display', 'status', 'is_active')
    list_editable = ('is_active',)
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
        js = ('js/admin_fixture_toggle.js?v=2',)

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
            form = FixtureUploadForm(request.POST, request.FILES)
            if form.is_valid():
                excel_file = request.FILES["excel_file"]
                betting_period = form.cleaned_data["betting_period"]
                
                try:
                    # Read Excel: Columns A, B, D, E, F, G -> Indices 0, 1, 3, 4, 5, 6
                    # Use dtype=str to prevent automatic date parsing by pandas/Excel engine
                    # This ensures we get the raw text (e.g. "07/02/2026") which we can parse correctly with dayfirst=True
                    df = pd.read_excel(excel_file, usecols=[0, 1, 3, 4, 5, 6], dtype=str)
                    df.columns = ['serial_number', 'home_team', 'away_team', 'draw_odd', 'match_date', 'match_time']
                    
                    success_count = 0
                    skip_count = 0
                    errors = []
                    
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
                            
                            # Parse Date using pandas to_datetime for robustness
                            match_date = row['match_date']
                            try:
                                if pd.notna(match_date):
                                    # If string is "2026-02-07 00:00:00" (from Excel conversion), slice it
                                    if isinstance(match_date, str) and ' ' in match_date:
                                        match_date = match_date.split(' ')[0]
                                        
                                    match_date = pd.to_datetime(match_date, dayfirst=True).date()
                                else:
                                    raise ValueError("Date is missing")
                            except Exception as e:
                                raise ValueError(f"Invalid date format: {match_date} ({str(e)})")
                                
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

                            # Check duplicates (Serial + BettingPeriod)
                            if Fixture.objects.filter(serial_number=serial, betting_period=betting_period).exists():
                                skip_count += 1
                                errors.append(f"Row {index + 2}: Duplicate Serial {serial}")
                                continue
                                
                            # Check duplicates (Teams + Date + Time)
                            if Fixture.objects.filter(home_team__iexact=home, away_team__iexact=away, match_date=match_date, match_time=match_time).exists():
                                skip_count += 1
                                errors.append(f"Row {index + 2}: Duplicate Fixture {home} vs {away}")
                                continue

                            # Create Fixture
                            Fixture.objects.create(
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
                            success_count += 1
                            
                        except Exception as e:
                            errors.append(f"Row {index + 2}: {str(e)}")
                            
                    messages.success(request, f"Upload Complete: {success_count} added, {skip_count} skipped.")
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
    list_display = ('serial_number_display', 'home_team', 'away_team', 'match_date', 'match_time', 'home_score', 'away_score', 'status')
    list_editable = ('home_score', 'away_score', 'status')
    list_filter = ('status', 'match_date', 'betting_period')
    search_fields = ('home_team', 'away_team', 'serial_number')
    
    def get_queryset(self, request):
        qs = super().get_queryset(request)
        # Only show fixtures from active betting periods, similar to FixtureAdmin
        qs = qs.filter(betting_period__is_active=True)
        return qs.order_by('serial_number')

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

# --- Transaction Admin ---
class TransactionAdmin(admin.ModelAdmin):
    list_display = ('timestamp', 'user', 'transaction_type', 'amount', 'status', 'is_successful')
    list_filter = ('transaction_type', 'status', 'is_successful', 'timestamp')
    search_fields = ('user__username', 'user__email', 'paystack_reference', 'description', 'id')
    readonly_fields = ('timestamp',)
    date_hierarchy = 'timestamp'
    list_select_related = ('user',)

# --- UserWithdrawal Admin ---
class UserWithdrawalAdmin(admin.ModelAdmin):
    list_display = ('user', 'amount', 'bank_name', 'account_number', 'account_name', 'status', 'request_time')
    list_editable = ('status',)
    list_filter = ('status', 'request_time', 'bank_name')
    search_fields = ('user__username', 'user__email', 'account_number', 'account_name')
    readonly_fields = ('request_time', 'approved_rejected_time')
    date_hierarchy = 'request_time'
    list_select_related = ('user',)

# Register your models with the CUSTOM admin site
betting_admin_site.register(User, CustomUserAdmin)
betting_admin_site.register(Wallet, WalletAdmin)
betting_admin_site.register(Transaction, TransactionAdmin)
betting_admin_site.register(BettingPeriod, BettingPeriodAdmin)
betting_admin_site.register(Fixture, FixtureAdmin)
betting_admin_site.register(Result, ResultAdmin)
betting_admin_site.register(BetTicket, BetTicketAdmin)
betting_admin_site.register(BonusRule)
betting_admin_site.register(SystemSetting)
betting_admin_site.register(UserWithdrawal, UserWithdrawalAdmin)

# --- Processed Withdrawal Admin (Audit) ---
class ProcessedWithdrawalAdmin(admin.ModelAdmin):
    list_display = ('id', 'user', 'amount', 'balance_before', 'balance_after', 'approved_rejected_by', 'approver_balance_before', 'approver_balance_after', 'status_badge', 'approved_rejected_time')
    list_filter = ('approved_rejected_time', 'approved_rejected_by', 'user')
    search_fields = ('user__email', 'user__username', 'id', 'processed_ip')
    readonly_fields = [field.name for field in UserWithdrawal._meta.fields] + ['balance_before', 'balance_after', 'approver_balance_before', 'approver_balance_after', 'processed_ip']
    date_hierarchy = 'approved_rejected_time'
    ordering = ('-approved_rejected_time',)
    change_list_template = 'betting/admin/processed_withdrawal_change_list.html'

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.filter(status__in=['approved', 'completed'])

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

    def changelist_view(self, request, extra_context=None):
        response = super().changelist_view(request, extra_context)
        try:
            qs = response.context_data['cl'].queryset
        except (AttributeError, KeyError):
            return response
            
        total = qs.aggregate(Sum('amount'))['amount__sum'] or Decimal('0.00')
        extra_context = extra_context or {}
        extra_context['total_withdrawal_amount'] = total
        
        if hasattr(response, 'context_data'):
            response.context_data.update(extra_context)
            
        return response

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

# Register Celery Beat models
betting_admin_site.register(PeriodicTask, PeriodicTaskAdmin)
betting_admin_site.register(IntervalSchedule)
betting_admin_site.register(CrontabSchedule)
betting_admin_site.register(SolarSchedule)
betting_admin_site.register(ClockedSchedule, ClockedScheduleAdmin)

# Register Celery Results models
betting_admin_site.register(TaskResult, TaskResultAdmin)
betting_admin_site.register(GroupResult, GroupResultAdmin)

# Site Configuration Admin
class SiteConfigurationAdmin(admin.ModelAdmin):
    fieldsets = (
        ('General Settings', {
            'fields': ('site_name', 'logo', 'favicon', 'landing_page_background')
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

betting_admin_site.register(SiteConfiguration, SiteConfigurationAdmin)

# --- Credit & Loan Admin ---

@admin.register(CreditRequest)
class CreditRequestAdmin(admin.ModelAdmin):
    list_display = ('requester', 'recipient', 'amount', 'request_type', 'status', 'created_at')
    list_filter = ('status', 'request_type', 'created_at')
    search_fields = ('requester__email', 'recipient__email', 'reason')
    readonly_fields = ('created_at', 'updated_at')

@admin.register(Loan)
class LoanAdmin(admin.ModelAdmin):
    list_display = ('borrower', 'lender', 'amount', 'outstanding_balance', 'status', 'created_at', 'due_date')
    list_filter = ('status', 'created_at')
    search_fields = ('borrower__email', 'lender__email')
    readonly_fields = ('created_at',)

class CreditLogAdmin(admin.ModelAdmin):
    list_display = ('actor', 'target_user', 'action_type', 'amount', 'status', 'timestamp')
    list_filter = ('action_type', 'status', 'timestamp')
    search_fields = ('actor__email', 'target_user__email', 'reference_id')
    readonly_fields = ('timestamp',)

# Register these with custom admin site as well if needed
betting_admin_site.register(CreditRequest, CreditRequestAdmin)
betting_admin_site.register(Loan, LoanAdmin)
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
