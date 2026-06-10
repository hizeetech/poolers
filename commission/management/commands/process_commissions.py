from django.core.management.base import BaseCommand
from commission.services import CommissionCalculationService, CommissionPayoutService
from commission.tasks import (
    ensure_monthly_commission_period_for_date,
    ensure_last_completed_weekly_commission_period_for_date,
)

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
        weekly_period, created = ensure_last_completed_weekly_commission_period_for_date()
        
        if not weekly_period.is_processed:
            self.stdout.write(f"Processing weekly period: {weekly_period}")
            try:
                CommissionCalculationService.calculate_weekly_commissions(weekly_period)
                self.stdout.write(self.style.SUCCESS(f"Successfully calculated commissions for {weekly_period}"))
            except Exception as e:
                self.stdout.write(self.style.ERROR(f"Error calculating weekly commissions: {str(e)}"))
        else:
            self.stdout.write(f"Weekly period {weekly_period} already processed.")

        monthly_period, created = ensure_monthly_commission_period_for_date()
        
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
