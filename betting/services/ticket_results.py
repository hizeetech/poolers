import logging

from django.db import transaction
from django.db.models import Q

from betting.models import BetTicket, Fixture, Selection
from commission.sync_utils import refresh_weekly_commissions_for_ticket_ids_sync


logger = logging.getLogger(__name__)


def recalculate_tickets_for_fixture_sync(fixture_id):
    try:
        try:
            fixture = Fixture.objects.get(id=fixture_id)
        except Fixture.DoesNotExist:
            logger.warning("Fixture %s not found during ticket recalculation.", fixture_id)
            return

        try:
            serial = str(getattr(fixture, "serial_number", "") or "").strip()
            period_id = getattr(fixture, "betting_period_id", None)
            relink_q = Q(bet_ticket__status="pending")
            if period_id:
                relink_q &= (Q(betting_period_id=period_id) | Q(betting_period__isnull=True))
            if serial:
                relink_q &= Q(fixture_serial_number__iexact=serial)
            else:
                relink_q &= Q(
                    fixture_home_team__iexact=fixture.home_team,
                    fixture_away_team__iexact=fixture.away_team,
                    fixture_match_date=fixture.match_date,
                    fixture_match_time=fixture.match_time,
                )

            Selection.objects.filter(relink_q).exclude(fixture_id=fixture.id).update(
                fixture=fixture,
                fixture_serial_number=serial or "",
                fixture_home_team=fixture.home_team,
                fixture_away_team=fixture.away_team,
                fixture_match_date=fixture.match_date,
                fixture_match_time=fixture.match_time,
            )
        except Exception:
            pass

        tickets = list(
            BetTicket.objects.filter(selections__fixture=fixture)
            .exclude(status__in=[*BetTicket.VOIDED_STATUSES, "cashed_out"])
            .distinct()
        )
        count = len(tickets)
        affected_ticket_ids = [str(ticket.id) for ticket in tickets]
        logger.info("Starting recalculation for %s tickets for fixture %s", count, fixture)

        with transaction.atomic():
            for ticket in tickets:
                current_ticket = BetTicket.objects.get(pk=ticket.pk)
                if current_ticket.status == "pending":
                    current_ticket.recalculate_ticket()
                    current_ticket.check_and_update_status()
                else:
                    current_ticket.backfill_after_result_correction(reason=f"Fixture {fixture.id} result corrected")

        logger.info("Completed recalculation for %s tickets for fixture %s", count, fixture)
        if affected_ticket_ids:
            refresh_weekly_commissions_for_ticket_ids_sync(affected_ticket_ids)
        return {"error": None, "affected_ticket_ids": affected_ticket_ids, "processed": count}
    except Exception as exc:
        logger.error("Critical error in recalculate_tickets_for_fixture_sync: %s", exc)
        return {"error": str(exc), "affected_ticket_ids": locals().get("affected_ticket_ids", []), "processed": 0}
