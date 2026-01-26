from celery import shared_task
from django.utils import timezone
from django.db.models import Sum
from datetime import timedelta
from betting.models import BetTicket, AgentPayout
from .models import DailyMetricSnapshot
from .alerts import AlertService

@shared_task
def aggregate_daily_metrics(date_str=None):
    """
    Aggregates metrics for a given date and saves to DailyMetricSnapshot.
    date_str: 'YYYY-MM-DD' (defaults to yesterday if None)
    """
    if date_str:
        target_date = timezone.datetime.strptime(date_str, '%Y-%m-%d').date()
    else:
        # Default to yesterday to ensure full day's data
        target_date = timezone.now().date() - timedelta(days=1)
        
    start_of_day = timezone.datetime.combine(target_date, timezone.datetime.min.time())
    start_of_day = timezone.make_aware(start_of_day)
    
    end_of_day = timezone.datetime.combine(target_date, timezone.datetime.max.time())
    end_of_day = timezone.make_aware(end_of_day)
    
    # 1. Stake Volume
    tickets = BetTicket.objects.filter(placed_at__range=(start_of_day, end_of_day))
    total_stake = tickets.aggregate(total=Sum('stake_amount'))['total'] or 0
    
    # 2. Winnings Paid (Approximation: Tickets WON on this day)
    # Ideally, we should track actual payouts, but using won tickets is a standard GGR proxy
    won_tickets = BetTicket.objects.filter(status='won', last_updated__range=(start_of_day, end_of_day))
    total_winnings = won_tickets.aggregate(total=Sum('max_winning'))['total'] or 0
    
    # 3. GGR
    ggr = total_stake - total_winnings
    
    # 4. Commissions (AgentPayouts created/settled for this period? 
    # AgentPayouts are per betting_period. Let's approximate by looking at payouts created on this day 
    # OR sum commission from tickets if we tracked it per ticket. 
    # Since we don't have commission per ticket in model easily accessible without calculation, 
    # let's assume Net Profit = GGR for now, or subtract a flat % estimation if needed.
    # But wait, AgentPayout model exists. Let's use payouts created on this day as liability.)
    commissions = AgentPayout.objects.filter(created_at__range=(start_of_day, end_of_day)).aggregate(total=Sum('commission_amount'))['total'] or 0
    
    net_profit = ggr - commissions
    
    # 5. Operational Counts
    total_tickets_sold = tickets.count()
    active_users = tickets.values('user').distinct().count()
    retail_tickets = tickets.filter(user__user_type='cashier').count()
    online_tickets = tickets.filter(user__user_type='player').count()
    
    # Update or Create Snapshot
    snapshot, created = DailyMetricSnapshot.objects.update_or_create(
        date=target_date,
        defaults={
            'total_stake_volume': total_stake,
            'total_winnings_paid': total_winnings,
            'gross_gaming_revenue': ggr,
            'net_profit': net_profit,
            'total_tickets_sold': total_tickets_sold,
            'active_users_count': active_users,
            'online_tickets_count': online_tickets,
            'retail_tickets_count': retail_tickets,
        }
    )
    
    return f"Successfully aggregated metrics for {target_date}"

@shared_task(
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    max_retries=3,
    retry_kwargs={'max_retries': 3}
)
def run_risk_checks(self):
    """
    Periodically checks for risk alerts.
    """
    AlertService.check_and_send_alerts()
    return "Risk checks completed"
