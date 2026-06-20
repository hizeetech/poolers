from datetime import timedelta

from django.apps import apps
from django.utils import timezone

from commission.models import CommissionPeriod
from commission.services import calculate_weekly_agent_commission


def get_current_weekly_period_bounds(reference_date=None):
    today = reference_date or timezone.localdate()
    days_since_tuesday = (today.weekday() - 1) % 7
    start_date = today - timedelta(days=days_since_tuesday)
    end_date = start_date + timedelta(days=6)
    return start_date, end_date


def ensure_weekly_commission_period_for_date(reference_date=None):
    start_date, end_date = get_current_weekly_period_bounds(reference_date=reference_date)
    return CommissionPeriod.objects.get_or_create(
        period_type='weekly',
        start_date=start_date,
        end_date=end_date,
    )


def refresh_weekly_commissions_for_ticket_ids_sync(ticket_ids):
    if not ticket_ids:
        return {"period_ids": [], "agent_ids": [], "updated": 0}

    BetTicket = apps.get_model('betting', 'BetTicket')
    User = apps.get_model('betting', 'User')
    tickets = list(
        BetTicket.objects.filter(id__in=ticket_ids)
        .select_related('user__agent')
        .only('id', 'placed_at', 'user_id', 'user__agent_id')
    )
    if not tickets:
        return {"period_ids": [], "agent_ids": [], "updated": 0}

    period_cache = {}
    affected_pairs = set()

    for ticket in tickets:
        agent_id = getattr(getattr(ticket, 'user', None), 'agent_id', None)
        placed_at = getattr(ticket, 'placed_at', None)
        placed_date = placed_at.date() if placed_at else None
        if not agent_id or not placed_date:
            continue

        if placed_date not in period_cache:
            weekly_period, _ = ensure_weekly_commission_period_for_date(reference_date=placed_date)
            period_cache[placed_date] = weekly_period.id
        affected_pairs.add((period_cache[placed_date], agent_id))

    if not affected_pairs:
        return {"period_ids": [], "agent_ids": [], "updated": 0}

    period_ids = sorted({period_id for period_id, _ in affected_pairs})
    agent_ids = sorted({agent_id for _, agent_id in affected_pairs})
    period_map = CommissionPeriod.objects.in_bulk(period_ids)
    agent_map = User.objects.in_bulk(agent_ids)

    updated = 0
    for period_id, agent_id in sorted(affected_pairs):
        period = period_map.get(period_id)
        agent = agent_map.get(agent_id)
        if not period or not agent:
            continue
        calculate_weekly_agent_commission(agent, period)
        updated += 1

    return {"period_ids": period_ids, "agent_ids": agent_ids, "updated": updated}
