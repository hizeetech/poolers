from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from django.db.models import Q
from django.utils import timezone
from django.db import transaction as db_transaction
from django.contrib import messages
from decimal import Decimal
from django.urls import path, reverse 
from django.shortcuts import redirect 

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
    DeclareResultForm # Removed AdminUserEditForm as it's consolidated
)

from .models import (
    User, Wallet, Transaction, BettingPeriod, Fixture, BetTicket,
    BonusRule, SystemSetting, AgentPayout, UserWithdrawal, ActivityLog, Result, Selection,
    SiteConfiguration, LoginAttempt, CreditRequest, Loan, CreditLog
)


# --- Custom Admin Site Definition ---
class BettingAdminSite(admin.AdminSite):
    site_header = "PoolBetBetting Admin" # Corrected from "PoolBetting Admin" for consistency, but you can change back if intended
    site_title = "PoolBetting Admin Portal"
    index_title = "Welcome to PoolBetting Administration"

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
        'cashier_prefix'
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
    
    actions = ['unlock_accounts']

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
        ('Permissions', {'fields': ('is_active', 'is_staff', 'is_superuser', 'groups', 'user_permissions', 'user_type')}),
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
                'user_type', 'is_active', 'is_staff', 'is_superuser', 
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

    def get_form(self, request, obj=None, **kwargs):
        # Pass the request to the form for permission checks if needed in form's clean method
        if obj: # Editing an existing object
            kwargs['form'] = self.form
            kwargs['form'].request = request # Pass request to the form instance
        else: # Adding a new object
            kwargs['form'] = self.add_form
            kwargs['form'].request = request # Pass request to the form instance
        return super().get_form(request, obj, **kwargs)

    def save_model(self, request, obj, form, change):
        action_description = f"User '{obj.email}' {'updated' if change else 'created'}."
        views.log_admin_activity(request, action_description)

        # The password setting and user type related staff/superuser status
        # are now largely handled within the custom AdminUserCreationForm/AdminUserChangeForm save methods.
        # Call the form's save method explicitly if you need its custom logic to run.
        # Otherwise, super().save_model will call obj.save() and form.save() as needed.
        super().save_model(request, obj, form, change)

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
    list_display = ('home_team', 'away_team', 'match_date', 'match_time', 'betting_period', 'serial_number', 'status', 'is_active')
    list_editable = ('is_active',)
    list_filter = ('betting_period', 'status', 'is_active', 'match_date')
    search_fields = ('home_team', 'away_team', 'serial_number')
    ordering = ('-match_date', 'match_time')

# --- Result Admin ---
class ResultAdmin(admin.ModelAdmin):
    list_display = ('home_team', 'away_team', 'match_date', 'home_score', 'away_score', 'status')
    list_editable = ('home_score', 'away_score', 'status')
    list_filter = ('status', 'match_date', 'betting_period')
    search_fields = ('home_team', 'away_team', 'serial_number')
    ordering = ('-match_date', 'match_time')


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
# Activity Log Admin
class ActivityLogAdmin(admin.ModelAdmin):
    list_display = ('timestamp', 'user', 'action_type_badge', 'affected_object', 'ip_address', 'isp', 'mac_address_display')
    list_filter = ('action_type', 'timestamp', 'user')
    search_fields = ('user__username', 'user__email', 'ip_address', 'isp', 'action', 'affected_object')
    readonly_fields = [field.name for field in ActivityLog._meta.fields]
    list_per_page = 50
    date_hierarchy = 'timestamp'
    list_select_related = ('user',)
    
    def has_add_permission(self, request):
        return False
        
    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser

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
        color = colors.get(obj.action_type, 'black')
        return format_html(
            '<span style="color: white; background-color: {}; padding: 3px 10px; border-radius: 5px; font-weight: bold;">{}</span>',
            color,
            obj.get_action_type_display()
        )
    action_type_badge.short_description = 'Action'

    def mac_address_display(self, obj):
        if obj.mac_address:
            return obj.mac_address
        return "Unavailable (External Network)"
    mac_address_display.short_description = "MAC Address"

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


betting_admin_site.register(LoginAttempt, LoginAttemptAdmin)
