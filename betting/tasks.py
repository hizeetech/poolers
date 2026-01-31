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
