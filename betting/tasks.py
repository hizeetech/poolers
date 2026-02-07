from celery import shared_task
from django.utils import timezone
from django.db.models import Q
from .models import Fixture
import logging

logger = logging.getLogger(__name__)

@shared_task
def update_started_fixtures_status():
    """
    Periodically check for fixtures that have started and update their status/visibility.
    """
    # Get current time in the project's timezone (Africa/Lagos)
    local_now = timezone.localtime(timezone.now())
    
    # Find fixtures that are 'scheduled' and 'active' but start time has passed
    # We look for:
    # 1. Match date is in the past
    # 2. OR Match date is today AND match time is in the past or now
    started_fixtures = Fixture.objects.filter(
        is_active=True,
        status='scheduled'
    ).filter(
        Q(match_date__lt=local_now.date()) | 
        Q(match_date=local_now.date(), match_time__lte=local_now.time())
    )
    
    count = started_fixtures.count()
    if count > 0:
        # Update these fixtures:
        # 1. Set is_active=False (hides from public view)
        # 2. Set status='live' (indicates match has started)
        # Note: bulk update does not trigger signals, which is usually fine for this transition.
        updated_count = started_fixtures.update(is_active=False, status='live')
        logger.info(f"Updated {updated_count} fixtures to 'live' status and deactivated them.")
    else:
        logger.debug("No started fixtures found to update.")

@shared_task
def recalculate_tickets_for_fixture(fixture_id):
    """
    Background task to recalculate all tickets associated with a changed fixture.
    This prevents timeouts when saving results in the admin.
    """
    from .models import BetTicket  # Local import to avoid circular dependency
    try:
        # Get fixture - if it doesn't exist anymore, just return
        try:
            fixture = Fixture.objects.get(id=fixture_id)
        except Fixture.DoesNotExist:
            logger.warning(f"Fixture {fixture_id} not found during ticket recalculation task.")
            return

        tickets = BetTicket.objects.filter(selections__fixture=fixture).distinct()
        count = tickets.count()
        logger.info(f"Starting recalculation for {count} tickets for fixture {fixture}")

        for ticket in tickets:
            try:
                # First, recalculate odds and potential winnings to handle void events
                ticket.recalculate_ticket()
                # Then, check if the ticket status should change (Won/Lost)
                ticket.check_and_update_status()
            except Exception as e:
                logger.error(f"Error updating ticket {ticket.id}: {e}")
        
        logger.info(f"Completed recalculation for {count} tickets for fixture {fixture}")
        
    except Exception as e:
        logger.error(f"Critical error in recalculate_tickets_for_fixture: {e}")
