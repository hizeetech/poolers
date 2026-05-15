from celery import shared_task
from django.apps import apps
from django.db.models import Sum, Count, Value, DecimalField, Q
from django.db.models.functions import Coalesce
from django.utils import timezone
from decimal import Decimal
from datetime import timedelta

from .services import (
    update_liability_snapshots_for_fixture,
    update_agent_exposure_snapshot,
    update_user_exposure_snapshot,
    update_betting_period_liability_snapshot,
)


@shared_task
def refresh_fixture_liabilities():
    Fixture = apps.get_model("betting", "Fixture")
    fixture_ids = list(Fixture.objects.filter(status="scheduled").values_list("id", flat=True))
    for fid in fixture_ids:
        try:
            update_liability_snapshots_for_fixture(fid)
        except Exception:
            continue


@shared_task
def refresh_fixture_liability(fixture_id):
    try:
        update_liability_snapshots_for_fixture(int(fixture_id))
        return True
    except Exception:
        return False


@shared_task
def refresh_agent_exposures():
    User = apps.get_model("betting", "User")
    agent_ids = list(User.objects.filter(user_type__in=["agent", "super_agent", "master_agent"]).values_list("id", flat=True))
    for aid in agent_ids:
        try:
            update_agent_exposure_snapshot(aid)
        except Exception:
            continue


@shared_task
def refresh_agent_exposure(agent_id):
    try:
        update_agent_exposure_snapshot(int(agent_id))
        return True
    except Exception:
        return False


@shared_task
def refresh_user_exposure(user_id):
    try:
        update_user_exposure_snapshot(int(user_id))
        return True
    except Exception:
        return False


@shared_task
def refresh_betting_period_liability(period_id):
    try:
        update_betting_period_liability_snapshot(int(period_id))
        return True
    except Exception:
        return False


@shared_task
def compute_sharp_bettors(days=30, min_tickets=20):
    BetTicket = apps.get_model("betting", "BetTicket")
    User = apps.get_model("betting", "User")
    SharpBettorProfile = apps.get_model("risk", "SharpBettorProfile")

    since = timezone.now() - timedelta(days=int(days))
    qs = BetTicket.objects.filter(placed_at__gte=since).exclude(status__in=["deleted", "cancelled"])

    stats = (
        qs.values("user_id")
        .annotate(
            ticket_count=Count("id"),
            total_stake=Coalesce(Sum("stake_amount"), Value(0), output_field=DecimalField()),
            total_return=Coalesce(Sum("max_winning", filter=Q(status="won")), Value(0), output_field=DecimalField()),
            total_odds_sum=Coalesce(Sum("total_odd"), Value(0), output_field=DecimalField()),
            wins=Count("id", filter=Q(status="won")),
        )
        .filter(ticket_count__gte=int(min_tickets))
    )

    for row in stats.iterator():
        user = User.objects.filter(id=row["user_id"]).first()
        if not user:
            continue
        ticket_count = int(row["ticket_count"] or 0)
        total_stake = Decimal(str(row["total_stake"] or 0))
        total_return = Decimal(str(row["total_return"] or 0))
        wins = int(row["wins"] or 0)
        total_odds_sum = Decimal(str(row["total_odds_sum"] or 0))
        profit = (total_return - total_stake).quantize(Decimal("0.01"))

        win_rate = Decimal("0.00")
        if ticket_count > 0:
            win_rate = (Decimal(wins) / Decimal(ticket_count) * 100).quantize(Decimal("0.01"))

        roi = Decimal("0.0000")
        if total_stake > 0:
            roi = (profit / total_stake).quantize(Decimal("0.0000"))

        yield_percent = (roi * 100).quantize(Decimal("0.01"))
        avg_odds = Decimal("0.00")
        if ticket_count > 0:
            avg_odds = (total_odds_sum / Decimal(ticket_count)).quantize(Decimal("0.01"))

        profile, _ = SharpBettorProfile.objects.get_or_create(user=user)
        profile.ticket_count = ticket_count
        profile.total_stake = total_stake
        profile.total_profit = profit
        profile.win_rate = win_rate
        profile.roi = roi
        profile.yield_percent = yield_percent
        profile.avg_odds = avg_odds
        profile.last_calculated_at = timezone.now()

        profile.is_flagged = bool(roi > Decimal("0.20") and win_rate > Decimal("60.00"))
        profile.save()
