from django.core.management.base import BaseCommand
from django_celery_beat.models import PeriodicTask, IntervalSchedule
import json

class Command(BaseCommand):
    help = 'Setup periodic tasks for fixture management'

    def handle(self, *args, **kwargs):
        # Create or get interval (e.g., every 5 minutes)
        schedule, created = IntervalSchedule.objects.get_or_create(
            every=5,
            period=IntervalSchedule.MINUTES,
        )
        if created:
            self.stdout.write(f"Created 5-minute interval schedule.")

        # Create the periodic task
        task_name = 'Update Started Fixtures Status'
        task, created = PeriodicTask.objects.update_or_create(
            name=task_name,
            defaults={
                'task': 'betting.tasks.update_started_fixtures_status',
                'interval': schedule,
                'enabled': True,
                'description': 'Periodically sets active fixtures to live/inactive when start time passes.'
            }
        )
        
        if created:
            self.stdout.write(self.style.SUCCESS(f'Successfully created task "{task_name}".'))
        else:
            self.stdout.write(self.style.SUCCESS(f'Successfully updated task "{task_name}".'))
