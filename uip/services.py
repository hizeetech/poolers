from django.db.models import Sum, Count, Q, F, FloatField, ExpressionWrapper
from django.db.models.functions import Cast
from django.utils import timezone
from django.core.cache import cache
from datetime import timedelta
import redis
from betting.models import BetTicket, User, Transaction, UserWithdrawal, Wallet, AgentPayout, LoginAttempt, Selection

class DashboardService:
    @staticmethod
    def get_redis_client():
        try:
            return redis.Redis(host='127.0.0.1', port=6379, db=0, socket_connect_timeout=1)
        except Exception:
            return None

    @staticmethod
    def get_data_version():
        r = DashboardService.get_redis_client()
        if r:
            try:
                v = r.get('uip_serial_freq_version')
                if v:
                    return int(v)
                r.set('uip_serial_freq_version', 1)
                return 1
            except Exception:
                pass
        return cache.get_or_set('uip_serial_freq_version', 1)

    @staticmethod
    def invalidate_data_version():
        r = DashboardService.get_redis_client()
        if r:
            try:
                return r.incr('uip_serial_freq_version')
            except Exception:
                pass
        try:
            return cache.incr('uip_serial_freq_version')
        except ValueError:
            cache.set('uip_serial_freq_version', 1)
            return 1

    @staticmethod
    def get_serial_number_frequency(start_date=None, end_date=None, scope='all', user_id=None, period_id=None):
        """
        Aggregates frequency of serial numbers 1-49 across all valid bets.
        Supports filtering by date range, scope (online/retail), specific user, and betting period.
        """
        # Create a unique cache key based on filters and data version
        version = DashboardService.get_data_version()
        cache_key = f'uip_serial_number_frequency_{version}_{start_date}_{end_date}_{scope}_{user_id}_{period_id}'
        cached_data = cache.get(cache_key)
        if cached_data:
            return cached_data

        # Base query: Filter valid tickets
        qs = Selection.objects.filter(
            bet_ticket__status__in=['pending', 'won', 'lost', 'cashed_out']
        )

        # Apply Filters
        if start_date:
            qs = qs.filter(bet_ticket__placed_at__date__gte=start_date)
        if end_date:
            qs = qs.filter(bet_ticket__placed_at__date__lte=end_date)
            
        if scope == 'online':
            qs = qs.filter(bet_ticket__user__user_type='player')
        elif scope == 'retail':
            qs = qs.filter(bet_ticket__user__user_type__in=['cashier', 'agent'])
            
        if user_id:
            qs = qs.filter(bet_ticket__user_id=user_id)
            
        if period_id:
            qs = qs.filter(fixture__betting_period_id=period_id)

        # Aggregate
        counts = qs.values('fixture__serial_number').annotate(
            count=Count('id')
        ).order_by('fixture__serial_number')
        
        # Convert to dictionary {serial_number: count}
        frequency_map = {}
        for entry in counts:
            sn = entry['fixture__serial_number']
            try:
                sn_int = int(sn)
                if 1 <= sn_int <= 49:
                    frequency_map[sn_int] = frequency_map.get(sn_int, 0) + entry['count']
            except (ValueError, TypeError):
                continue
                
        # Prepare lists for Chart.js
        labels = list(range(1, 50))
        data = [frequency_map.get(i, 0) for i in labels]
        
        result = {
            'labels': labels,
            'data': data,
            'last_updated': timezone.now().isoformat()
        }
        
        # Cache for 5 mins
        cache.set(cache_key, result, 300) 
        return result

    @staticmethod
    def get_live_metrics():
        # Cache key for live metrics (short duration: 60 seconds)
        cache_key = 'uip_live_metrics'
        cached_data = cache.get(cache_key)
        if cached_data:
            return cached_data

        today = timezone.now().date()
        start_of_day = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
        
        # 1. Daily Stake Volume (Exclude cancelled/deleted)
        tickets_today = BetTicket.objects.filter(
            placed_at__gte=start_of_day,
            status__in=['pending', 'won', 'lost', 'cashed_out']
        )
        total_stake = tickets_today.aggregate(total=Sum('stake_amount'))['total'] or 0
        
        # 2. Total Tickets Sold
        total_tickets = tickets_today.count()
        
        # 3. Total Winnings Paid (Approximation)
        won_tickets_today = BetTicket.objects.filter(status='won', last_updated__gte=start_of_day)
        total_winnings = won_tickets_today.aggregate(total=Sum('max_winning'))['total'] or 0
        
        # 4. GGR
        ggr = total_stake - total_winnings
        
        # 5. Active Users
        active_bettors_count = tickets_today.values('user').distinct().count()
        
        # 6. Online vs Retail Split
        retail_tickets = tickets_today.filter(user__user_type='cashier').count()
        online_tickets = tickets_today.filter(user__user_type='player').count()
        
        # 7. Recent Large Bets (Alerts)
        # Note: QuerySets are lazy, but slicing evaluates them. We need to serialize for cache.
        large_bets = list(tickets_today.filter(stake_amount__gte=5000).order_by('-stake_amount')[:5])
        
        data = {
            'date': today,
            'total_stake': total_stake,
            'total_tickets': total_tickets,
            'total_winnings': total_winnings,
            'ggr': ggr,
            'active_users': active_bettors_count,
            'retail_count': retail_tickets,
            'online_count': online_tickets,
            'large_bets': large_bets,
        }
        
        cache.set(cache_key, data, 60) # Cache for 1 minute
        return data

    @staticmethod
    def get_agent_leaderboard():
        cache_key = 'uip_agent_leaderboard'
        cached_data = cache.get(cache_key)
        if cached_data:
            return cached_data

        today = timezone.now().date()
        start_of_day = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
        
        top_agents = list(User.objects.filter(
            user_type='agent',
            agents_under__bet_tickets__placed_at__gte=start_of_day,
            agents_under__bet_tickets__status__in=['pending', 'won', 'lost', 'cashed_out']
        ).annotate(
            daily_sales=Sum('agents_under__bet_tickets__stake_amount', filter=Q(agents_under__bet_tickets__status__in=['pending', 'won', 'lost', 'cashed_out'])),
            ticket_count=Count('agents_under__bet_tickets', filter=Q(agents_under__bet_tickets__status__in=['pending', 'won', 'lost', 'cashed_out']))
        ).order_by('-daily_sales')[:10])
        
        cache.set(cache_key, top_agents, 300) # Cache for 5 minutes
        return top_agents

    @staticmethod
    def get_financial_metrics():
        cache_key = 'uip_financial_metrics'
        cached_data = cache.get(cache_key)
        if cached_data:
            return cached_data

        # Commission Liabilities
        pending_payouts = AgentPayout.objects.filter(status='pending').aggregate(total=Sum('commission_amount'))['total'] or 0
        
        # User Wallet Balances (Liability)
        total_wallet_balance = Wallet.objects.aggregate(total=Sum('balance'))['total'] or 0
        
        # Profit Margin Analysis (Current Month)
        today = timezone.now().date()
        start_of_month_date = today.replace(day=1)
        start_of_month = timezone.make_aware(timezone.datetime.combine(start_of_month_date, timezone.datetime.min.time()))
        
        monthly_tickets = BetTicket.objects.filter(
            placed_at__gte=start_of_month,
            status__in=['pending', 'won', 'lost', 'cashed_out']
        )
        monthly_stake = monthly_tickets.aggregate(total=Sum('stake_amount'))['total'] or 0
        monthly_winnings = BetTicket.objects.filter(status='won', last_updated__gte=start_of_month).aggregate(total=Sum('max_winning'))['total'] or 0
        
        monthly_ggr = monthly_stake - monthly_winnings
        margin_percent = (monthly_ggr / monthly_stake * 100) if monthly_stake > 0 else 0
        
        data = {
            'commission_liability': pending_payouts,
            'user_wallet_liability': total_wallet_balance,
            'monthly_ggr': monthly_ggr,
            'monthly_margin': margin_percent,
            'monthly_stake': monthly_stake
        }
        
        cache.set(cache_key, data, 300) # Cache for 5 minutes
        return data

    @staticmethod
    def get_analytics_metrics():
        cache_key = 'uip_analytics_metrics'
        cached_data = cache.get(cache_key)
        if cached_data:
            return cached_data

        today = timezone.now().date()
        
        # Convert dates to aware datetimes for filtering DateTimeFields
        start_of_week_date = today - timedelta(days=today.weekday())
        start_of_week = timezone.make_aware(timezone.datetime.combine(start_of_week_date, timezone.datetime.min.time()))
        
        start_of_month_date = today.replace(day=1)
        start_of_month = timezone.make_aware(timezone.datetime.combine(start_of_month_date, timezone.datetime.min.time()))
        
        # 1. Agent Performance (Weekly)
        top_agents_week = list(User.objects.filter(
            user_type='agent',
            agents_under__bet_tickets__placed_at__gte=start_of_week,
            agents_under__bet_tickets__status__in=['pending', 'won', 'lost', 'cashed_out']
        ).annotate(
            weekly_sales=Sum('agents_under__bet_tickets__stake_amount', filter=Q(agents_under__bet_tickets__status__in=['pending', 'won', 'lost', 'cashed_out'])),
            weekly_tickets=Count('agents_under__bet_tickets', filter=Q(agents_under__bet_tickets__status__in=['pending', 'won', 'lost', 'cashed_out']))
        ).order_by('-weekly_sales')[:10])
        
        # 2. User Acquisition (New users this month)
        new_users_month = User.objects.filter(date_joined__gte=start_of_month).count()
        
        # 3. Active Users (Month)
        active_users_month = BetTicket.objects.filter(
            placed_at__gte=start_of_month,
            status__in=['pending', 'won', 'lost', 'cashed_out']
        ).values('user').distinct().count()
        
        # 4. Ticket Status Distribution (Month)
        status_dist = list(BetTicket.objects.filter(placed_at__gte=start_of_month).values('status').annotate(count=Count('status')))
        
        data = {
            'top_agents_week': top_agents_week,
            'new_users_month': new_users_month,
            'active_users_month': active_users_month,
            'ticket_status_dist': status_dist
        }
        
        cache.set(cache_key, data, 600) # Cache for 10 minutes
        return data

    @staticmethod
    def get_risk_metrics():
        today = timezone.now().date()
        # Convert to aware datetime
        start_of_week_date = today - timedelta(days=today.weekday())
        start_of_week = timezone.make_aware(timezone.datetime.combine(start_of_week_date, timezone.datetime.min.time()))
        
        # 1. Multi-Account/IP Detection
        # Find IPs with more than 2 distinct users successfully logging in this week
        suspicious_ips = LoginAttempt.objects.filter(
            status='success', 
            timestamp__gte=start_of_week
        ).values('ip_address').annotate(
            user_count=Count('user', distinct=True)
        ).filter(user_count__gt=2).order_by('-user_count')
        
        # 2. Repeated Bonus Abuse (Users with > 3 bonuses this week)
        bonus_abusers = Transaction.objects.filter(
            transaction_type='bonus',
            timestamp__gte=start_of_week
        ).values('user__email').annotate(
            bonus_count=Count('id')
        ).filter(bonus_count__gt=3).order_by('-bonus_count')
        
        # 3. High Winning Rate Users (> 70% win rate with > 5 bets)
        # This is complex in Django ORM without subqueries or window functions, let's do a simpler approach
        # Find users with high total winnings this week
        high_winners = BetTicket.objects.filter(
            status='won',
            last_updated__gte=start_of_week
        ).values('user__email').annotate(
            total_won=Sum('max_winning'),
            win_count=Count('id')
        ).order_by('-total_won')[:10]
        
        # 4. Large Bet Alerts (Recent)
        large_bets = BetTicket.objects.filter(
            stake_amount__gte=10000, # Threshold
            placed_at__gte=start_of_week
        ).order_by('-placed_at')[:10]
        
        return {
            'suspicious_ips': suspicious_ips,
            'bonus_abusers': bonus_abusers,
            'high_winners': high_winners,
            'large_bets': large_bets
        }
