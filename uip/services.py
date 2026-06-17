from django.apps import apps
from django.db import transaction
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.db.models import Sum, Count, Q, F, FloatField, ExpressionWrapper, DecimalField, Value
from django.db.models.functions import Cast, Coalesce
from django.utils import timezone
from django.core.cache import cache
from datetime import timedelta
import redis
from betting.models import BetTicket, User, Transaction, UserWithdrawal, Wallet, AgentPayout, LoginAttempt, Selection
from .models import Alert, FraudAlert, AlertAffectedUser, InvestigationCase, AdminActionLog

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
    def get_recent_activity(limit=50):
        cache_key = 'uip_recent_activity'
        cached_data = cache.get(cache_key)
        if cached_data:
            return cached_data

        activity_list = []

        # 1. Recent Alerts
        alerts = Alert.objects.order_by('-created_at')[:limit]
        for alert in alerts:
            activity_list.append({
                'type': 'alert',
                'timestamp': alert.created_at,
                'title': alert.title,
                'message': alert.message,
                'level': alert.severity
            })

        # 2. Recent High Value Bets (> 5000)
        large_bets = BetTicket.objects.filter(stake_amount__gte=5000).order_by('-placed_at')[:limit]
        for bet in large_bets:
            activity_list.append({
                'type': 'bet_placed',
                'timestamp': bet.placed_at,
                'ticket_id': bet.ticket_id,
                'user': bet.user.email,
                'amount': float(bet.stake_amount)
            })

        # 3. Recent Transactions (Deposits/Withdrawals)
        transactions = Transaction.objects.filter(amount__gte=5000).order_by('-timestamp')[:limit]
        for tx in transactions:
             activity_list.append({
                'type': 'transaction',
                'timestamp': tx.timestamp,
                'desc': tx.get_transaction_type_display(),
                'user': tx.user.email,
                'amount': float(tx.amount)
            })

        # Sort combined list by timestamp desc
        activity_list.sort(key=lambda x: x['timestamp'], reverse=True)
        
        # Slice to limit
        final_list = activity_list[:limit]
        
        cache.set(cache_key, final_list, 10) # Cache for 10 seconds (short cache for near real-time)
        return final_list

    @staticmethod
    def get_live_metrics(timeframe='daily'):
        # Cache key for live metrics (short duration: 60 seconds)
        cache_key = f'uip_live_metrics_{timeframe}'
        cached_data = cache.get(cache_key)
        if cached_data:
            return cached_data

        now = timezone.now()
        today = now.date()
        
        if timeframe == 'weekly':
            # Start of week (Monday)
            start_date = now - timedelta(days=now.weekday())
            start_time = start_date.replace(hour=0, minute=0, second=0, microsecond=0)
        elif timeframe == 'monthly':
            # Start of month
            start_time = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        else:
            # Daily (default)
            start_time = now.replace(hour=0, minute=0, second=0, microsecond=0)
        
        # 1. Stake Volume (Exclude cancelled/deleted)
        tickets_period = BetTicket.objects.filter(
            placed_at__gte=start_time,
            status__in=['pending', 'won', 'lost', 'cashed_out']
        )
        total_stake = tickets_period.aggregate(total=Sum('stake_amount'))['total'] or 0
        
        # 2. Total Tickets Sold
        total_tickets = tickets_period.count()
        
        # 3. Total Winnings Paid (Approximation)
        won_tickets_period = BetTicket.objects.filter(status='won', last_updated__gte=start_time)
        total_winnings = won_tickets_period.aggregate(total=Sum('max_winning'))['total'] or 0
        
        # 4. GGR
        ggr = total_stake - total_winnings
        
        # 5. Active Users
        active_bettors_count = tickets_period.values('user').distinct().count()
        
        # 6. Online vs Retail Split
        retail_tickets = tickets_period.filter(user__user_type='cashier').count()
        online_tickets = tickets_period.filter(user__user_type='player').count()
        
        # 7. Recent Large Bets (Alerts)
        # Note: QuerySets are lazy, but slicing evaluates them. We need to serialize for cache.
        large_bets = list(tickets_period.filter(stake_amount__gte=5000).order_by('-stake_amount')[:5])
        
        data = {
            'date': today,
            'timeframe': timeframe,
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
    def get_agent_leaderboard(timeframe='daily'):
        cache_key = f'uip_agent_leaderboard_{timeframe}'
        cached_data = cache.get(cache_key)
        if cached_data:
            return cached_data

        now = timezone.now()
        
        if timeframe == 'weekly':
            start_date = now - timedelta(days=now.weekday())
            start_time = start_date.replace(hour=0, minute=0, second=0, microsecond=0)
        elif timeframe == 'monthly':
            start_time = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        else:
            start_time = now.replace(hour=0, minute=0, second=0, microsecond=0)
        
        top_agents = list(User.objects.filter(
            user_type='agent',
            agents_under__bet_tickets__placed_at__gte=start_time,
            agents_under__bet_tickets__status__in=['pending', 'won', 'lost', 'cashed_out']
        ).annotate(
            daily_sales=Sum('agents_under__bet_tickets__stake_amount', filter=Q(agents_under__bet_tickets__status__in=['pending', 'won', 'lost', 'cashed_out'])),
            ticket_count=Count('agents_under__bet_tickets', filter=Q(agents_under__bet_tickets__status__in=['pending', 'won', 'lost', 'cashed_out']))
        ).order_by('-daily_sales')[:10])
        
        cache.set(cache_key, top_agents, 300) # Cache for 5 minutes
        return top_agents

    @staticmethod
    def get_agent_leaderboards(start_time, end_time, limit=50):
        start_key = (start_time.isoformat() if hasattr(start_time, "isoformat") else str(start_time)).replace(":", "_")
        end_key = (end_time.isoformat() if hasattr(end_time, "isoformat") else str(end_time)).replace(":", "_")
        cache_key = f"uip_agent_leaderboards_v1_{start_key}_{end_key}_{limit}"
        cached_data = cache.get(cache_key)
        if cached_data:
            return cached_data

        ticket_statuses = ['pending', 'won', 'lost', 'cashed_out']
        money_field = DecimalField(max_digits=18, decimal_places=2)

        base = User.objects.filter(user_type='agent').annotate(
            total_turnover=Coalesce(
                Sum(
                    'agents_under__bet_tickets__stake_amount',
                    filter=Q(
                        agents_under__bet_tickets__placed_at__gte=start_time,
                        agents_under__bet_tickets__placed_at__lte=end_time,
                        agents_under__bet_tickets__status__in=ticket_statuses,
                    )
                ),
                Value(0),
                output_field=money_field,
            ),
            tickets_sold=Coalesce(
                Count(
                    'agents_under__bet_tickets',
                    filter=Q(
                        agents_under__bet_tickets__placed_at__gte=start_time,
                        agents_under__bet_tickets__placed_at__lte=end_time,
                        agents_under__bet_tickets__status__in=ticket_statuses,
                    ),
                    distinct=True,
                ),
                0
            ),
            winnings_paid=Coalesce(
                Sum(
                    'agents_under__bet_tickets__max_winning',
                    filter=Q(
                        agents_under__bet_tickets__last_updated__gte=start_time,
                        agents_under__bet_tickets__last_updated__lte=end_time,
                        agents_under__bet_tickets__status='won',
                    )
                ),
                Value(0),
                output_field=money_field,
            ),
            total_deposits=Coalesce(
                Sum(
                    'agents_under__transactions__amount',
                    filter=Q(
                        agents_under__transactions__transaction_type='deposit',
                        agents_under__transactions__is_successful=True,
                        agents_under__transactions__status='completed',
                        agents_under__transactions__timestamp__gte=start_time,
                        agents_under__transactions__timestamp__lte=end_time,
                    )
                ),
                Value(0),
                output_field=money_field,
            ),
        )

        top_deposits = list(base.order_by('-total_deposits')[:limit])
        top_turnover = list(base.order_by('-total_turnover')[:limit])

        leaderboard_profit = []
        for a in base.only('id', 'email')[:limit * 3]:
            turnover = float(getattr(a, 'total_turnover', 0) or 0)
            winnings = float(getattr(a, 'winnings_paid', 0) or 0)
            revenue = turnover - winnings
            margin = (revenue / turnover * 100.0) if turnover > 0 else 0.0
            leaderboard_profit.append({
                'agent': a,
                'turnover': turnover,
                'winnings': winnings,
                'revenue': revenue,
                'margin': margin,
                'tickets': int(getattr(a, 'tickets_sold', 0) or 0),
                'deposits': float(getattr(a, 'total_deposits', 0) or 0),
            })
        leaderboard_profit.sort(key=lambda x: x['margin'], reverse=True)
        top_margin = leaderboard_profit[:limit]

        data = {
            'top_deposits': top_deposits,
            'top_turnover': top_turnover,
            'top_margin': top_margin,
        }
        cache.set(cache_key, data, 120)
        return data

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
        cache_key = "uip_risk_metrics_v2"
        cached = cache.get(cache_key)
        if cached:
            return cached

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
        ).filter(user_count__gt=1).order_by('-user_count')
        
        # 2. Repeated Bonus Abuse (Users with > 3 bonuses this week)
        bonus_abusers = Transaction.objects.filter(
            transaction_type='bonus',
            timestamp__gte=start_of_week
        ).values('user', 'user__username', 'user__email').annotate(
            bonus_count=Count('id')
        ).filter(bonus_count__gt=3).order_by('-bonus_count')
        
        # 3. High Winning Rate Users (> 70% win rate with > 5 bets)
        # This is complex in Django ORM without subqueries or window functions, let's do a simpler approach
        # Find users with high total winnings this week
        high_winners = BetTicket.objects.filter(
            status='won',
            last_updated__gte=start_of_week
        ).values('user', 'user__username', 'user__email').annotate(
            total_won=Sum('max_winning'),
            win_count=Count('id')
        ).order_by('-total_won')[:10]
        
        # 4. Large Bet Alerts (Recent)
        large_bets = BetTicket.objects.filter(
            stake_amount__gte=10000, # Threshold
            placed_at__gte=start_of_week
        ).order_by('-placed_at')[:10]

        FixtureLiabilitySnapshot = apps.get_model("risk", "FixtureLiabilitySnapshot")
        FixtureRiskState = apps.get_model("risk", "FixtureRiskState")
        MarketRiskState = apps.get_model("risk", "MarketRiskState")
        SelectionRiskState = apps.get_model("risk", "SelectionRiskState")
        SuspiciousActivityLog = apps.get_model("risk", "SuspiciousActivityLog")
        SharpBettorProfile = apps.get_model("risk", "SharpBettorProfile")
        SyndicateGroup = apps.get_model("risk", "SyndicateGroup")

        top_fixtures = list(
            FixtureLiabilitySnapshot.objects.select_related("fixture")
            .order_by("-risk_score", "-total_potential_payout", "-updated_at")[:20]
        )
        suspended_fixtures = list(
            FixtureRiskState.objects.filter(is_suspended=True).select_related("fixture").order_by("-updated_at")[:20]
        )
        suspended_markets = list(
            MarketRiskState.objects.filter(is_suspended=True).select_related("fixture").order_by("-updated_at")[:20]
        )
        suspended_selections = list(
            SelectionRiskState.objects.filter(is_suspended=True).select_related("fixture").order_by("-updated_at")[:20]
        )
        suspicious_logs = list(
            SuspiciousActivityLog.objects.select_related("user", "ticket").order_by("-created_at")[:50]
        )
        sharp_bettors = list(
            SharpBettorProfile.objects.filter(is_flagged=True).select_related("user").order_by("-roi", "-win_rate")[:20]
        )
        syndicates = list(
            SyndicateGroup.objects.filter(is_active=True).order_by("-risk_score", "-updated_at")[:20]
        )
        
        data = {
            'suspicious_ips': suspicious_ips,
            'bonus_abusers': bonus_abusers,
            'high_winners': high_winners,
            'large_bets': large_bets,
            'top_fixtures': top_fixtures,
            'suspended_fixtures': suspended_fixtures,
            'suspended_markets': suspended_markets,
            'suspended_selections': suspended_selections,
            'suspicious_logs': suspicious_logs,
            'sharp_bettors': sharp_bettors,
            'syndicates': syndicates,
        }
        cache.set(cache_key, data, 10)
        return data

class FraudDetectionService:
    @staticmethod
    def create_fraud_alert(alert_type, description, severity, related_users, related_ips=None, related_devices=None):
        """
        Creates a FraudAlert and links affected users with a snapshot of their data.
        """
        with transaction.atomic():
            alert = FraudAlert.objects.create(
                alert_type=alert_type,
                description=description,
                severity=severity,
                related_ips=related_ips or [],
                related_devices=related_devices or [],
            )
            
            for user in related_users:
                # Calculate individual risk score for this user in the context of this alert
                risk_score = FraudDetectionService.calculate_user_risk_score(user, alert_type)
                
                # Snapshot data for investigation
                total_bets = BetTicket.objects.filter(user=user).count()
                total_deposits = Transaction.objects.filter(
                    user=user, transaction_type='deposit', status='completed'
                ).aggregate(total=Sum('amount'))['total'] or 0
                total_withdrawals = Transaction.objects.filter(
                    user=user, transaction_type='withdrawal', status='completed'
                ).aggregate(total=Sum('amount'))['total'] or 0
                
                last_activity = user.activity_logs.order_by('-timestamp').first()
                
                AlertAffectedUser.objects.create(
                    alert=alert,
                    user=user,
                    ip_address=last_activity.ip_address if last_activity else None,
                    device_fingerprint=last_activity.user_agent if last_activity else None, # Using user_agent as a proxy for fingerprint if not available
                    risk_score=risk_score,
                    last_activity_time=last_activity.timestamp if last_activity else None,
                    wallet_balance=user.wallet.balance,
                    total_deposits=total_deposits,
                    total_withdrawals=total_withdrawals,
                    total_bets_count=total_bets,
                )
            
            # Create an investigation case automatically
            InvestigationCase.objects.create(alert=alert)
            
            # Send Real-Time Notification via WebSockets
            try:
                channel_layer = get_channel_layer()
                async_to_sync(channel_layer.group_send)(
                    'uip_dashboard',
                    {
                        'type': 'dashboard_update',
                        'data': {
                            'type': 'fraud_alert',
                            'message': description,
                            'alert_id': alert.id
                        }
                    }
                )
            except Exception as e:
                print(f"Failed to send WebSocket alert: {e}")
            
            return alert

    @staticmethod
    def calculate_user_risk_score(user, alert_type):
        """
        Dynamic risk score calculation based on user behavior and alert type.
        """
        score = 0
        
        # Base score by alert type
        base_scores = {
            'multi_account': 50,
            'bonus_abuse': 40,
            'suspicious_betting': 60,
            'high_value_bet': 20,
            'vpn_usage': 30,
            'shared_device': 40,
            'payment_fraud': 80,
        }
        score += base_scores.get(alert_type, 10)
        
        # Multipliers / Additives
        # 1. VPN Usage check (if available in activity logs)
        if user.activity_logs.filter(isp__icontains='VPN').exists():
            score += 20
            
        # 2. High win rate check
        total_bets = BetTicket.objects.filter(user=user).count()
        if total_bets > 10:
            won_bets = BetTicket.objects.filter(user=user, status='won').count()
            win_rate = (won_bets / total_bets) * 100
            if win_rate > 70:
                score += 30
                
        # 3. Account age (New accounts are higher risk)
        days_since_joined = (timezone.now() - user.date_joined).days
        if days_since_joined < 7:
            score += 15
            
        return min(score, 100)

    @staticmethod
    def run_detection_cycle():
        """
        Runs various fraud detection algorithms and generates alerts.
        """
        # 1. Multi-Account Detection (Same IP)
        start_of_week = timezone.now() - timedelta(days=7)
        suspicious_ips = LoginAttempt.objects.filter(
            status='success', 
            timestamp__gte=start_of_week
        ).values('ip_address').annotate(
            user_count=Count('user', distinct=True)
        ).filter(user_count__gt=1)
        
        for item in suspicious_ips:
            ip = item['ip_address']
            users = User.objects.filter(login_attempts__ip_address=ip, login_attempts__status='success').distinct()
            
            # Avoid duplicate alerts for the same IP today
            if not FraudAlert.objects.filter(alert_type='multi_account', related_ips__contains=[ip], timestamp__date=timezone.now().date()).exists():
                FraudDetectionService.create_fraud_alert(
                    alert_type='multi_account',
                    description=f"Multiple accounts ({item['user_count']}) detected using the same IP address: {ip}",
                    severity='high',
                    related_users=users,
                    related_ips=[ip]
                )

        # 2. Bonus Abuse Detection
        bonus_abusers = Transaction.objects.filter(
            transaction_type='bonus',
            timestamp__gte=start_of_week
        ).values('user').annotate(
            bonus_count=Count('id')
        ).filter(bonus_count__gt=5)
        
        for item in bonus_abusers:
            user = User.objects.get(id=item['user'])
            if not FraudAlert.objects.filter(alert_type='bonus_abuse', affected_users=user, timestamp__date=timezone.now().date()).exists():
                FraudDetectionService.create_fraud_alert(
                    alert_type='bonus_abuse',
                    description=f"User has claimed {item['bonus_count']} bonuses in the last 7 days, exceeding the threshold.",
                    severity='medium',
                    related_users=[user]
                )
