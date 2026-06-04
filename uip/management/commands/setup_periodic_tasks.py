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

        # 4. Risk: Refresh Fixture Liabilities (Every 5 Minutes)
        schedule_5m, _ = CrontabSchedule.objects.get_or_create(
            minute='*/5',
            hour='*',
            day_of_week='*',
            day_of_month='*',
            month_of_year='*',
            timezone='Africa/Lagos'
        )
        PeriodicTask.objects.update_or_create(
            name='Risk Refresh Fixture Liabilities',
            defaults={
                'crontab': schedule_5m,
                'task': 'risk.tasks.refresh_fixture_liabilities',
                'args': json.dumps([]),
                'enabled': True
            }
        )
        self.stdout.write(self.style.SUCCESS('Confirmed task: Risk Refresh Fixture Liabilities'))

        # 5. Risk: Refresh Agent Exposures (Every 10 Minutes)
        schedule_10m, _ = CrontabSchedule.objects.get_or_create(
            minute='*/10',
            hour='*',
            day_of_week='*',
            day_of_month='*',
            month_of_year='*',
            timezone='Africa/Lagos'
        )
        PeriodicTask.objects.update_or_create(
            name='Risk Refresh Agent Exposures',
            defaults={
                'crontab': schedule_10m,
                'task': 'risk.tasks.refresh_agent_exposures',
                'args': json.dumps([]),
                'enabled': True
            }
        )
        self.stdout.write(self.style.SUCCESS('Confirmed task: Risk Refresh Agent Exposures'))

        # 6. Risk: Compute Sharp Bettors (Daily 02:10 AM)
        schedule_sharp, _ = CrontabSchedule.objects.get_or_create(
            minute='10',
            hour='2',
            day_of_week='*',
            day_of_month='*',
            month_of_year='*',
            timezone='Africa/Lagos'
        )
        PeriodicTask.objects.update_or_create(
            name='Risk Compute Sharp Bettors',
            defaults={
                'crontab': schedule_sharp,
                'task': 'risk.tasks.compute_sharp_bettors',
                'args': json.dumps([]),
                'enabled': True
            }
        )
        self.stdout.write(self.style.SUCCESS('Confirmed task: Risk Compute Sharp Bettors'))

        # 7. Notifications: Deposit Reminders (Every Hour)
        PeriodicTask.objects.update_or_create(
            name='Notifications Deposit Reminders',
            defaults={
                'crontab': schedule_hourly,
                'task': 'notifications.tasks.send_deposit_reminders',
                'args': json.dumps([]),
                'enabled': True
            }
        )
        self.stdout.write(self.style.SUCCESS('Confirmed task: Notifications Deposit Reminders'))

        schedule_every_minute, _ = CrontabSchedule.objects.get_or_create(
            minute='*',
            hour='*',
            day_of_week='*',
            day_of_month='*',
            month_of_year='*',
            timezone='Africa/Lagos'
        )

        PeriodicTask.objects.update_or_create(
            name='Process Ticket Void Requests',
            defaults={
                'crontab': schedule_every_minute,
                'task': 'void_requests.tasks.process_void_requests',
                'args': json.dumps([]),
                'enabled': True
            }
        )
        self.stdout.write(self.style.SUCCESS('Confirmed task: Process Ticket Void Requests'))

        PeriodicTask.objects.update_or_create(
            name='Reconcile Pending Deposits',
            defaults={
                'crontab': schedule_10m,
                'task': 'betting.tasks.reconcile_recent_deposits',
                'args': json.dumps([]),
                'kwargs': json.dumps({'gateway': 'all', 'minutes': 1440, 'limit': 50}),
                'enabled': True
            }
        )
        self.stdout.write(self.style.SUCCESS('Confirmed task: Reconcile Pending Deposits'))

        self.stdout.write(self.style.SUCCESS('All periodic tasks configured successfully!'))
