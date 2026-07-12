import logging
from itertools import islice

from django.db import transaction
from django.db.models import Q

from betting.models import BetTicket, Fixture, Selection
from commission.sync_utils import refresh_weekly_commissions_for_ticket_ids_sync


logger = logging.getLogger(__name__)
RECALCULATION_BATCH_SIZE = 100


def _batched(values, size):
    iterator = iter(values)
    while True:
        batch = list(islice(iterator, size))
        if not batch:
            return
        yield batch


def recalculate_tickets_for_fixture_sync(fixture_id):
    affected_ticket_ids = []
    processed = 0
    failed = 0
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

        ticket_ids = list(
            BetTicket.objects.filter(selections__fixture=fixture)
            .exclude(status__in=[*BetTicket.VOIDED_STATUSES, "cashed_out"])
            .values_list("id", flat=True)
            .distinct()
        )
        count = len(ticket_ids)
        affected_ticket_ids = [str(ticket_id) for ticket_id in ticket_ids]
        logger.info("Starting recalculation for %s tickets for fixture %s", count, fixture)

        for ticket_batch in _batched(ticket_ids, RECALCULATION_BATCH_SIZE):
            for ticket_id in ticket_batch:
                try:
                    with transaction.atomic():
                        current_ticket = BetTicket.objects.get(pk=ticket_id)
                        if current_ticket.status == "pending":
                            current_ticket.recalculate_ticket()
                            current_ticket.check_and_update_status()
                        else:
                            current_ticket.backfill_after_result_correction(
                                reason=f"Fixture {fixture.id} result corrected"
                            )
                    processed += 1
                except Exception:
                    failed += 1
                    logger.exception(
                        "Failed to recalculate ticket %s for fixture %s",
                        ticket_id,
                        fixture_id,
                    )

        logger.info(
            "Completed recalculation for %s tickets for fixture %s (processed=%s failed=%s)",
            count,
            fixture,
            processed,
            failed,
        )
        if affected_ticket_ids:
            refresh_weekly_commissions_for_ticket_ids_sync(affected_ticket_ids)
        return {
            "error": None,
            "affected_ticket_ids": affected_ticket_ids,
            "processed": processed,
            "failed": failed,
        }
    except Exception as exc:
        logger.exception("Critical error in recalculate_tickets_for_fixture_sync: %s", exc)
        return {
            "error": str(exc),
            "affected_ticket_ids": affected_ticket_ids,
            "processed": processed,
            "failed": failed,
        }
