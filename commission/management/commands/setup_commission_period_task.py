from django.core.management.base import BaseCommand
from django_celery_beat.models import CrontabSchedule, PeriodicTask


class Command(BaseCommand):
    help = 'Setup periodic task to auto-create the weekly commission period every Wednesday'

    def handle(self, *args, **kwargs):
        schedule, created = CrontabSchedule.objects.get_or_create(
            minute='5',
            hour='0',
            day_of_week='wednesday',
            day_of_month='*',
            month_of_year='*',
        )
        if created:
            self.stdout.write("Created Wednesday 00:05 crontab schedule.")

        task_name = 'Ensure Weekly Commission Period'
        task, created = PeriodicTask.objects.update_or_create(
            name=task_name,
            defaults={
                'task': 'commission.tasks.ensure_weekly_commission_period',
                'crontab': schedule,
                'enabled': True,
                'queue': 'commission_queue',
                'description': 'Automatically creates the last completed Tuesday-to-Monday weekly commission period every Wednesday.',
            }
        )

        if created:
            self.stdout.write(self.style.SUCCESS(f'Successfully created task "{task_name}".'))
        else:
            self.stdout.write(self.style.SUCCESS(f'Successfully updated task "{task_name}".'))
