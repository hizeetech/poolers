from celery import shared_task
from django.utils import timezone
from datetime import timedelta
from commission.models import CommissionPeriod
from commission.services import CommissionCalculationService, CommissionPayoutService
import logging

logger = logging.getLogger(__name__)

def get_last_completed_weekly_period_bounds(reference_date=None):
    today = reference_date or timezone.localdate()
    days_since_monday = today.weekday()
    if days_since_monday == 0:
        days_since_monday = 7
    end_date = today - timedelta(days=days_since_monday)
    start_date = end_date - timedelta(days=6)
    return start_date, end_date

def get_last_completed_monthly_period_bounds(reference_date=None):
    today = reference_date or timezone.localdate()
    first_day_this_month = today.replace(day=1)
    end_date = first_day_this_month - timedelta(days=1)
    start_date = end_date.replace(day=1)
    return start_date, end_date

def ensure_weekly_commission_period_for_date(reference_date=None):
    start_date, end_date = get_last_completed_weekly_period_bounds(reference_date=reference_date)
    return CommissionPeriod.objects.get_or_create(
        period_type='weekly',
        start_date=start_date,
        end_date=end_date,
    )

def ensure_monthly_commission_period_for_date(reference_date=None):
    start_date, end_date = get_last_completed_monthly_period_bounds(reference_date=reference_date)
    return CommissionPeriod.objects.get_or_create(
        period_type='monthly',
        start_date=start_date,
        end_date=end_date,
    )

@shared_task(
    name='commission.tasks.ensure_weekly_commission_period',
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=5,
    max_retries=5,
    retry_kwargs={'max_retries': 5}
)
def ensure_weekly_commission_period(self):
    weekly_period, created = ensure_weekly_commission_period_for_date()
    logger.info(
        "Weekly commission period ready: %s (created=%s)",
        weekly_period,
        created,
    )
    return {
        'period_id': weekly_period.id,
        'created': created,
        'start_date': weekly_period.start_date.isoformat(),
        'end_date': weekly_period.end_date.isoformat(),
    }

@shared_task(
    name='commission.tasks.process_commissions',
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=5, # Exponential backoff starting at 5s
    max_retries=5,
    retry_kwargs={'max_retries': 5}
)
def process_commissions(self, payout=False):
    """
    Process agent commissions (calculation and optional payout).
    This task can be scheduled to run periodically (e.g., every Tuesday).
    """
    logger.info("Starting commission processing task...")

    # 1. Identify and create/get relevant periods
    today = timezone.localdate()

    weekly_period, created = ensure_weekly_commission_period_for_date(reference_date=today)
    
    if not weekly_period.is_processed:
        logger.info(f"Processing weekly period: {weekly_period}")
        try:
            CommissionCalculationService.calculate_weekly_commissions(weekly_period)
            logger.info(f"Successfully calculated commissions for {weekly_period}")
        except Exception as e:
            logger.error(f"Error calculating weekly commissions: {str(e)}")
    else:
        logger.info(f"Weekly period {weekly_period} already processed.")

    monthly_period, created = ensure_monthly_commission_period_for_date(reference_date=today)
    
    if not monthly_period.is_processed:
        logger.info(f"Processing monthly period: {monthly_period}")
        try:
            CommissionCalculationService.calculate_monthly_commissions(monthly_period)
            logger.info(f"Successfully calculated network commissions for {monthly_period}")
        except Exception as e:
            logger.error(f"Error calculating monthly commissions: {str(e)}")
    else:
        logger.info(f"Monthly period {monthly_period} already processed.")

    # 2. Process Payouts if requested
    if payout:
        logger.info("Processing payouts...")
        
        # Weekly Payouts
        count_weekly = CommissionPayoutService.process_weekly_payouts(weekly_period)
        logger.info(f"Processed {count_weekly} weekly payouts.")
        
        # Monthly Payouts
        count_monthly = CommissionPayoutService.process_monthly_payouts(monthly_period)
        logger.info(f"Processed {count_monthly} monthly payouts.")
        
    logger.info("Commission processing task completed.")
    return "Commission processing completed."
