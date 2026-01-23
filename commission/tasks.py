from celery import shared_task
from django.utils import timezone
from datetime import timedelta
from commission.models import CommissionPeriod
from commission.services import CommissionCalculationService, CommissionPayoutService
import logging

logger = logging.getLogger(__name__)

@shared_task(name='commission.tasks.process_commissions')
def process_commissions(payout=False):
    """
    Process agent commissions (calculation and optional payout).
    This task can be scheduled to run periodically (e.g., every Monday).
    """
    logger.info("Starting commission processing task...")

    # 1. Identify and create/get relevant periods
    today = timezone.now().date()
    
    # Determine last completed week (assuming Monday-Sunday)
    days_since_sunday = (today.weekday() + 1) % 7
    last_sunday = today - timedelta(days=days_since_sunday)
    last_monday = last_sunday - timedelta(days=6)
    
    # Check if weekly period exists for last week, if not create it
    weekly_period, created = CommissionPeriod.objects.get_or_create(
        period_type='weekly',
        start_date=last_monday,
        end_date=last_sunday
    )
    
    if not weekly_period.is_processed:
        logger.info(f"Processing weekly period: {weekly_period}")
        try:
            CommissionCalculationService.calculate_weekly_commissions(weekly_period)
            logger.info(f"Successfully calculated commissions for {weekly_period}")
        except Exception as e:
            logger.error(f"Error calculating weekly commissions: {str(e)}")
    else:
        logger.info(f"Weekly period {weekly_period} already processed.")

    # Determine last completed month
    first_day_this_month = today.replace(day=1)
    last_day_last_month = first_day_this_month - timedelta(days=1)
    first_day_last_month = last_day_last_month.replace(day=1)
    
    # Check if monthly period exists for last month
    monthly_period, created = CommissionPeriod.objects.get_or_create(
        period_type='monthly',
        start_date=first_day_last_month,
        end_date=last_day_last_month
    )
    
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
