from django.core.management.base import BaseCommand
from django_celery_beat.models import PeriodicTask, CrontabSchedule
import json

class Command(BaseCommand):
    help = 'Setup Celery Beat periodic tasks for UIP and Commissions'

    def handle(self, *args, **kwargs):
        self.stdout.write("Configuring Periodic Tasks...")

        # 1. Daily UIP Aggregation (12:05 AM Daily)
        schedule_daily, _ = CrontabSchedule.objects.get_or_create(
            minute='5',
            hour='0',
            day_of_week='*',
            day_of_month='*',
            month_of_year='*',
            timezone='Africa/Lagos'
        )
        
        PeriodicTask.objects.update_or_create(
            name='Daily UIP Aggregation',
            defaults={
                'crontab': schedule_daily,
                'task': 'uip.tasks.aggregate_daily_metrics',
                'args': json.dumps([]),
                'enabled': True
            }
        )
        self.stdout.write(self.style.SUCCESS('Confirmed task: Daily UIP Aggregation'))

        # 2. Hourly Risk Scan (Every Hour)
        schedule_hourly, _ = CrontabSchedule.objects.get_or_create(
            minute='0',
            hour='*',
            day_of_week='*',
            day_of_month='*',
            month_of_year='*',
            timezone='Africa/Lagos'
        )
        
        PeriodicTask.objects.update_or_create(
            name='Hourly Risk Scan',
            defaults={
                'crontab': schedule_hourly,
                'task': 'uip.tasks.run_risk_checks',
                'args': json.dumps([]),
                'enabled': True
            }
        )
        self.stdout.write(self.style.SUCCESS('Confirmed task: Hourly Risk Scan'))

        # 3. Weekly Agent Commission Processing (Monday 1:00 AM)
        schedule_weekly, _ = CrontabSchedule.objects.get_or_create(
            minute='0',
            hour='1',
            day_of_week='1', # Monday
            day_of_month='*',
            month_of_year='*',
            timezone='Africa/Lagos'
        )
        
        PeriodicTask.objects.update_or_create(
            name='Weekly Agent Commission Processing',
            defaults={
                'crontab': schedule_weekly,
                'task': 'commission.tasks.process_commissions',
                'args': json.dumps([]), # payout=False by default, can change if needed
                'enabled': True
            }
        )
        self.stdout.write(self.style.SUCCESS('Confirmed task: Weekly Agent Commission Processing'))

        self.stdout.write(self.style.SUCCESS('All periodic tasks configured successfully!'))
