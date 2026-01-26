from django.core.management.base import BaseCommand
from django_celery_beat.models import PeriodicTask, CrontabSchedule
import json

class Command(BaseCommand):
    help = 'Setup UIP periodic tasks'

    def handle(self, *args, **kwargs):
        # Create Schedule: Run at 00:30 AM every day
        schedule, created = CrontabSchedule.objects.get_or_create(
            hour=0,
            minute=30,
            day_of_week='*',
            month_of_year='*',
            timezone='Africa/Lagos'
        )
        
        # Create Periodic Task
        task, created = PeriodicTask.objects.get_or_create(
            name='Aggregate Daily Metrics',
            defaults={
                'crontab': schedule,
                'task': 'uip.tasks.aggregate_daily_metrics',
                'args': json.dumps([]),
            }
        )
        
        if created:
            self.stdout.write(self.style.SUCCESS('Successfully created periodic task "Aggregate Daily Metrics"'))
        else:
            self.stdout.write(self.style.SUCCESS('Periodic task "Aggregate Daily Metrics" already exists'))
