from django.core.management.base import BaseCommand
from django.utils import timezone
from commission.models import CommissionPeriod
from commission.services import CommissionCalculationService, CommissionPayoutService
from datetime import timedelta

class Command(BaseCommand):
    help = 'Process agent commissions (calculation and optional payout)'

    def add_arguments(self, parser):
        parser.add_argument(
            '--payout',
            action='store_true',
            help='Process payouts for calculated commissions',
        )

    def handle(self, *args, **options):
        self.stdout.write("Starting commission processing...")

        # 1. Identify and create/get relevant periods
        today = timezone.now().date()
        
        # Determine last completed week (assuming Monday-Sunday)
        # If today is Monday (0), last week ended yesterday.
        # If today is Tuesday (1), last week ended 2 days ago.
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
            self.stdout.write(f"Processing weekly period: {weekly_period}")
            try:
                CommissionCalculationService.calculate_weekly_commissions(weekly_period)
                self.stdout.write(self.style.SUCCESS(f"Successfully calculated commissions for {weekly_period}"))
            except Exception as e:
                self.stdout.write(self.style.ERROR(f"Error calculating weekly commissions: {str(e)}"))
        else:
            self.stdout.write(f"Weekly period {weekly_period} already processed.")

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
            self.stdout.write(f"Processing monthly period: {monthly_period}")
            try:
                CommissionCalculationService.calculate_monthly_commissions(monthly_period)
                self.stdout.write(self.style.SUCCESS(f"Successfully calculated network commissions for {monthly_period}"))
            except Exception as e:
                self.stdout.write(self.style.ERROR(f"Error calculating monthly commissions: {str(e)}"))
        else:
            self.stdout.write(f"Monthly period {monthly_period} already processed.")

        # 2. Process Payouts if requested
        if options['payout']:
            self.stdout.write("Processing payouts...")
            
            # Weekly Payouts
            count_weekly = CommissionPayoutService.process_weekly_payouts(weekly_period)
            self.stdout.write(f"Processed {count_weekly} weekly payouts.")
            
            # Monthly Payouts
            count_monthly = CommissionPayoutService.process_monthly_payouts(monthly_period)
            self.stdout.write(f"Processed {count_monthly} monthly payouts.")
            
        self.stdout.write(self.style.SUCCESS("Commission processing completed."))
