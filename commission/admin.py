from django.contrib import admin
from django.utils import timezone
from django.contrib import messages
from betting.admin import betting_admin_site
from .models import (
    CommissionPlan, HybridCommissionRule, AgentCommissionProfile,
    CommissionPeriod, WeeklyAgentCommission, MonthlyNetworkCommission,
    NetworkCommissionSettings, RetailTransaction
)
from .services import (
    calculate_weekly_agent_commission, calculate_monthly_network_commission,
    pay_weekly_commission, pay_monthly_network_commission
)
from betting.models import User, BetTicket
from django.db.models import Sum

class RetailTransactionAdmin(admin.ModelAdmin):
    change_list_template = 'admin/commission/retailtransaction/change_list.html'

    def changelist_view(self, request, extra_context=None):
        hierarchy_data = []
        
        # Filter Parameters
        start_date = request.GET.get('start_date')
        end_date = request.GET.get('end_date')
        search_query = request.GET.get('q')
        
        # Date Filters for Tickets
        ticket_filters = {}
        if start_date:
            ticket_filters['placed_at__date__gte'] = start_date
        if end_date:
            ticket_filters['placed_at__date__lte'] = end_date
            
        # User Search Filter (Find relevant Master Agents)
        ma_queryset = User.objects.filter(user_type='master_agent')
        
        if search_query:
            # Find users matching query
            found_users = User.objects.filter(
                models.Q(email__icontains=search_query) | 
                models.Q(first_name__icontains=search_query) | 
                models.Q(last_name__icontains=search_query)
            )
            
            ma_ids = set()
            for user in found_users:
                if user.user_type == 'master_agent':
                    ma_ids.add(user.id)
                elif user.user_type == 'super_agent' and user.master_agent:
                    ma_ids.add(user.master_agent.id)
                elif user.user_type == 'agent' and user.super_agent and user.super_agent.master_agent:
                    ma_ids.add(user.super_agent.master_agent.id)
                elif user.user_type == 'cashier' and user.agent and user.agent.super_agent and user.agent.super_agent.master_agent:
                    ma_ids.add(user.agent.super_agent.master_agent.id)
            
            ma_queryset = ma_queryset.filter(id__in=ma_ids)

        master_agents = ma_queryset
        
        for ma in master_agents:
            # MA Data
            # Tickets where user__agent__super_agent__master_agent = ma
            ma_tickets = BetTicket.objects.filter(user__agent__super_agent__master_agent=ma).exclude(status__in=['cancelled', 'deleted'])
            if ticket_filters:
                ma_tickets = ma_tickets.filter(**ticket_filters)

            ma_sales = ma_tickets.aggregate(s=Sum('stake_amount'))['s'] or 0
            ma_winnings = ma_tickets.filter(status='won').aggregate(s=Sum('max_winning'))['s'] or 0
            ma_ggr = ma_sales - ma_winnings
            
            ma_plan = getattr(ma.commission_profile, 'plan', None) if hasattr(ma, 'commission_profile') else None

            ma_node = {
                'user': ma,
                'plan': ma_plan,
                'sales': ma_sales,
                'winnings': ma_winnings,
                'ggr': ma_ggr,
                'super_agents': []
            }
            
            # Filter children only if searching? 
            # If I searched for a specific agent, I might want to see only that agent path?
            # For now, let's show the full tree under the matched MA. 
            # Optimization: If search_query exists, we could prune the tree.
            # But let's stick to filtering the ROOT nodes first as per plan.
            
            super_agents = User.objects.filter(user_type='super_agent', master_agent=ma)
            for sa in super_agents:
                # SA Data
                sa_tickets = BetTicket.objects.filter(user__agent__super_agent=sa).exclude(status__in=['cancelled', 'deleted'])
                if ticket_filters:
                    sa_tickets = sa_tickets.filter(**ticket_filters)

                sa_sales = sa_tickets.aggregate(s=Sum('stake_amount'))['s'] or 0
                sa_winnings = sa_tickets.filter(status='won').aggregate(s=Sum('max_winning'))['s'] or 0
                sa_ggr = sa_sales - sa_winnings
                
                sa_plan = getattr(sa.commission_profile, 'plan', None) if hasattr(sa, 'commission_profile') else None

                sa_node = {
                    'user': sa,
                    'plan': sa_plan,
                    'sales': sa_sales,
                    'winnings': sa_winnings,
                    'ggr': sa_ggr,
                    'agents': []
                }
                
                agents = User.objects.filter(user_type='agent', super_agent=sa)
                for ag in agents:
                    # Agent Data
                    ag_tickets = BetTicket.objects.filter(user__agent=ag).exclude(status__in=['cancelled', 'deleted'])
                    if ticket_filters:
                        ag_tickets = ag_tickets.filter(**ticket_filters)

                    ag_sales = ag_tickets.aggregate(s=Sum('stake_amount'))['s'] or 0
                    ag_winnings = ag_tickets.filter(status='won').aggregate(s=Sum('max_winning'))['s'] or 0
                    ag_ggr = ag_sales - ag_winnings

                    ag_plan = getattr(ag.commission_profile, 'plan', None) if hasattr(ag, 'commission_profile') else None

                    ag_node = {
                        'user': ag,
                        'plan': ag_plan,
                        'sales': ag_sales,
                        'winnings': ag_winnings,
                        'ggr': ag_ggr,
                        'cashiers': []
                    }
                    
                    cashiers = User.objects.filter(user_type='cashier', agent=ag)
                    for ca in cashiers:
                         # Cashier Data
                        ca_tickets = BetTicket.objects.filter(user=ca).exclude(status__in=['cancelled', 'deleted'])
                        if ticket_filters:
                            ca_tickets = ca_tickets.filter(**ticket_filters)

                        ca_sales = ca_tickets.aggregate(s=Sum('stake_amount'))['s'] or 0
                        ca_winnings = ca_tickets.filter(status='won').aggregate(s=Sum('max_winning'))['s'] or 0
                        ca_ggr = ca_sales - ca_winnings
                        
                        ca_node = {
                            'user': ca,
                            'sales': ca_sales,
                            'winnings': ca_winnings,
                            'ggr': ca_ggr
                        }
                        ag_node['cashiers'].append(ca_node)
                    
                    sa_node['agents'].append(ag_node)
                
                ma_node['super_agents'].append(sa_node)
                
                hierarchy_data.append(ma_node)
                
            context = {
                **betting_admin_site.each_context(request),
                'title': "Retail Transactions",
                'hierarchy_data': hierarchy_data,
                'start_date': start_date,
                'end_date': end_date,
                'search_query': search_query,
                'opts': self.model._meta,
            }
            
            return render(request, self.change_list_template, context)


class HybridCommissionRuleInline(admin.TabularInline):
    model = HybridCommissionRule
    extra = 1

class CommissionPlanAdmin(admin.ModelAdmin):
    list_display = ('name', 'ggr_percent', 'is_hybrid_active', 'enable_single_selection_override')
    inlines = [HybridCommissionRuleInline]
    search_fields = ('name',)
    fieldsets = (
        (None, {
            'fields': ('name', 'description')
        }),
        ('GGR Commission', {
            'fields': ('ggr_percent', 'ggr_payment_day'),
            'description': "Standard Profit Share Commission"
        }),
        ('Hybrid Commission', {
            'fields': ('is_hybrid_active',),
            'description': "Add rules below for selection-based commission"
        }),
        ('Single Selection Special', {
            'fields': ('enable_single_selection_override', 'single_selection_calc_type', 'single_selection_value'),
            'description': "Special rules for single bets"
        }),
    )

class AgentCommissionProfileAdmin(admin.ModelAdmin):
    list_display = ('user', 'plan', 'is_active')
    search_fields = ('user__email', 'user__first_name', 'user__last_name')
    list_filter = ('plan', 'is_active')
    autocomplete_fields = ['plan']
    # Note: autocomplete_fields=['user'] requires UserAdmin to have search_fields. 
    # If User is not registered in this admin site with search_fields, it might fail.
    # We'll use raw_id_fields as a fallback or if autocomplete is not set up.
    raw_id_fields = ('user',)

class NetworkCommissionSettingsAdmin(admin.ModelAdmin):
    list_display = ('role', 'commission_percent', 'payout_day_description')
    list_editable = ('commission_percent', 'payout_day_description')

class CommissionPeriodAdmin(admin.ModelAdmin):
    list_display = ('__str__', 'period_type', 'start_date', 'end_date', 'is_processed', 'processed_at')
    list_filter = ('period_type', 'is_processed')
    actions = ['process_period']

    def process_period(self, request, queryset):
        count = 0
        for period in queryset:
            if period.period_type == 'weekly':
                # Find all active agents with profiles
                profiles = AgentCommissionProfile.objects.filter(is_active=True)
                for profile in profiles:
                    calculate_weekly_agent_commission(profile.user, period)
                count += 1
            elif period.period_type == 'monthly':
                # Find all Super Agents and Master Agents
                super_agents = User.objects.filter(user_type='super_agent', is_active=True)
                master_agents = User.objects.filter(user_type='master_agent', is_active=True)
                
                # Calc Super Agents First
                for sa in super_agents:
                    calculate_monthly_network_commission(sa, period)
                
                # Calc Master Agents Second
                for ma in master_agents:
                    calculate_monthly_network_commission(ma, period)
                
                count += 1
            
            period.is_processed = True
            period.processed_at = timezone.now()
            period.save()
            
        self.message_user(request, f"{count} periods processed.")
    process_period.short_description = "Calculate Commissions for selected periods"

class WeeklyAgentCommissionAdmin(admin.ModelAdmin):
    list_display = ('agent', 'period', 'total_stake', 'ggr', 'commission_total_amount', 'status', 'is_marked_for_payment', 'paid_at')
    list_editable = ('is_marked_for_payment',)
    list_filter = ('status', 'period')
    search_fields = ('agent__email',)
    actions = ['pay_commissions']
    readonly_fields = ('created_at', 'paid_at', 'total_stake', 'total_winnings', 'ggr', 
                      'commission_ggr_amount', 'commission_hybrid_amount', 'commission_total_amount', 'status')

    class Media:
        js = ('commission/js/weekly_commission_admin.js',)

    def add_view(self, request, form_url='', extra_context=None):
        from django.template.response import TemplateResponse
        from django.shortcuts import redirect
        from .models import CommissionPeriod, AgentCommissionProfile
        from .services import calculate_weekly_agent_commission_data

        periods = CommissionPeriod.objects.filter(period_type='weekly').order_by('-start_date')
        selected_period_id = request.GET.get('period_id') or request.POST.get('period_id')
        
        agent_data = []
        if selected_period_id:
            try:
                period = CommissionPeriod.objects.get(id=selected_period_id)
                # Find all agents with active profiles
                profiles = AgentCommissionProfile.objects.filter(is_active=True).select_related('user')
                
                for profile in profiles:
                    agent = profile.user
                    
                    # Check existing
                    existing = WeeklyAgentCommission.objects.filter(agent=agent, period=period).first()
                    
                    if existing:
                        row = {
                            'agent': agent,
                            'plan': profile.plan.name,
                            'data': existing,
                            'status': existing.get_status_display(),
                            'is_paid': existing.status == 'paid'
                        }
                        # Show if paid or if amount > 0
                        if existing.status == 'paid' or existing.commission_total_amount > 0:
                            agent_data.append(row)
                    else:
                        # Calculate
                        data = calculate_weekly_agent_commission_data(agent, period)
                        if data and data['commission_total_amount'] > 0:
                            row = {
                                'agent': agent,
                                'plan': profile.plan.name,
                                'data': data,
                                'status': 'Pending (Calculated)',
                                'is_paid': False
                            }
                            agent_data.append(row)
                            
            except CommissionPeriod.DoesNotExist:
                pass

        if request.method == 'POST' and '_pay_selected' in request.POST:
            selected_ids = request.POST.getlist('selected_agents')
            if selected_ids and selected_period_id:
                period = CommissionPeriod.objects.get(id=selected_period_id)
                count = 0
                for agent_id in selected_ids:
                    try:
                        agent = User.objects.get(id=agent_id)
                        # Create or Get record (persist calculation)
                        record = calculate_weekly_agent_commission(agent, period)
                        if record:
                            success, msg = pay_weekly_commission(record)
                            if success:
                                count += 1
                    except User.DoesNotExist:
                        continue
                
                self.message_user(request, f"Paid {count} agents successfully.")
                return redirect(request.path + f"?period_id={selected_period_id}")

        context = {
            **self.admin_site.each_context(request),
            'opts': self.model._meta,
            'periods': periods,
            'selected_period_id': selected_period_id,
            'agent_data': agent_data,
            'title': 'Bulk Weekly Commission Payment',
        }
        return TemplateResponse(request, "admin/commission/weeklyagentcommission/bulk_add.html", context)

    def pay_commissions(self, request, queryset):
        success_count = 0
        for record in queryset:
            success, message = pay_weekly_commission(record)
            if success:
                success_count += 1
        self.message_user(request, f"{success_count} commissions paid successfully.")
    pay_commissions.short_description = "Pay selected commissions"

    def save_model(self, request, obj, form, change):
        # Handle bulk payment checkbox
        if obj.is_marked_for_payment:
            # Reset flag immediately so it gets saved as False during payment or calculation
            obj.is_marked_for_payment = False
            
            success, msg = pay_weekly_commission(obj)
            if success:
                messages.success(request, f"Payment for {obj.agent}: {msg}")
            else:
                messages.warning(request, f"Payment for {obj.agent}: {msg}")
        
        calculated_obj = calculate_weekly_agent_commission(obj.agent, obj.period)
        
        if calculated_obj:
            obj.pk = calculated_obj.pk
            obj.total_stake = calculated_obj.total_stake
            obj.total_winnings = calculated_obj.total_winnings
            obj.ggr = calculated_obj.ggr
            obj.commission_ggr_amount = calculated_obj.commission_ggr_amount
            obj.commission_hybrid_amount = calculated_obj.commission_hybrid_amount
            obj.commission_total_amount = calculated_obj.commission_total_amount
            # Preserve paid status if we just paid it, otherwise take calculated status (usually pending unless updated)
            # But wait, calculate_weekly_agent_commission reads from DB. 
            # If pay_weekly_commission saved to DB, calculate_weekly_agent_commission might return that status?
            # Actually, calculate_weekly_agent_commission does update_or_create. 
            # If status is not in defaults, it keeps DB value.
            # So obj.status = calculated_obj.status will be correct.
            obj.status = calculated_obj.status
            # calculated_obj is already saved to DB by the service function.

class MonthlyNetworkCommissionAdmin(admin.ModelAdmin):
    list_display = ('user', 'role', 'period', 'ngr', 'commission_amount', 'status', 'paid_at')
    list_filter = ('status', 'role', 'period')
    search_fields = ('user__email',)
    actions = ['pay_commissions']
    readonly_fields = ('created_at', 'paid_at')

    def add_view(self, request, form_url='', extra_context=None):
        from django.template.response import TemplateResponse
        from django.shortcuts import redirect
        from .models import CommissionPeriod
        from .services import calculate_monthly_network_commission_data, calculate_monthly_network_commission, pay_monthly_network_commission
        
        periods = CommissionPeriod.objects.filter(period_type='monthly').order_by('-start_date')
        selected_period_id = request.GET.get('period_id') or request.POST.get('period_id')
        
        user_data = []
        if selected_period_id:
            try:
                period = CommissionPeriod.objects.get(id=selected_period_id)
                # Find all Super Agents and Master Agents
                network_users = User.objects.filter(user_type__in=['super_agent', 'master_agent'], is_active=True).order_by('user_type', 'email')
                
                for user in network_users:
                    # Check existing
                    existing = MonthlyNetworkCommission.objects.filter(user=user, period=period).first()
                    
                    if existing:
                        row = {
                            'user': user,
                            'data': existing,
                            'status': existing.get_status_display(),
                            'is_paid': existing.status == 'paid'
                        }
                        # Show if paid or if amount > 0 (or just show all for networks usually?)
                        # Weekly logic: if existing.status == 'paid' or existing.commission_total_amount > 0:
                        if existing.status == 'paid' or existing.commission_amount > 0:
                            user_data.append(row)
                    else:
                        # Calculate
                        data = calculate_monthly_network_commission_data(user, period)
                        if data and data['commission_amount'] > 0:
                            row = {
                                'user': user,
                                'data': data,
                                'status': 'Pending (Calculated)',
                                'is_paid': False
                            }
                            user_data.append(row)
                            
            except CommissionPeriod.DoesNotExist:
                pass

        if request.method == 'POST' and '_pay_selected' in request.POST:
            selected_ids = request.POST.getlist('selected_users')
            if selected_ids and selected_period_id:
                period = CommissionPeriod.objects.get(id=selected_period_id)
                count = 0
                for user_id in selected_ids:
                    try:
                        user = User.objects.get(id=user_id)
                        # Create or Get record (persist calculation)
                        record = calculate_monthly_network_commission(user, period)
                        if record:
                            success, msg = pay_monthly_network_commission(record)
                            if success:
                                count += 1
                    except User.DoesNotExist:
                        continue
                
                self.message_user(request, f"Paid {count} network commissions successfully.")
                return redirect(request.path + f"?period_id={selected_period_id}")

        context = {
            **self.admin_site.each_context(request),
            'opts': self.model._meta,
            'periods': periods,
            'selected_period_id': selected_period_id,
            'user_data': user_data,
            'title': 'Bulk Monthly Network Commission Payment',
        }
        return TemplateResponse(request, "admin/commission/monthlynetworkcommission/bulk_add.html", context)

    def pay_commissions(self, request, queryset):
        success_count = 0
        for record in queryset:
            success, msg = pay_monthly_network_commission(record)
            if success:
                success_count += 1
        self.message_user(request, f"Paid {success_count} commissions.")
    pay_commissions.short_description = "Pay selected commissions to Wallet"

# Register models with the custom betting_admin_site
betting_admin_site.register(CommissionPlan, CommissionPlanAdmin)
betting_admin_site.register(AgentCommissionProfile, AgentCommissionProfileAdmin)
betting_admin_site.register(NetworkCommissionSettings, NetworkCommissionSettingsAdmin)
betting_admin_site.register(CommissionPeriod, CommissionPeriodAdmin)
betting_admin_site.register(WeeklyAgentCommission, WeeklyAgentCommissionAdmin)
betting_admin_site.register(MonthlyNetworkCommission, MonthlyNetworkCommissionAdmin)
betting_admin_site.register(RetailTransaction, RetailTransactionAdmin)
