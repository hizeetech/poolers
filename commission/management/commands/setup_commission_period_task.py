from django.core.management.base import BaseCommand

from commission.scheduling import (
    WEEKLY_COMMISSION_PERIOD_TASK_NAME,
    ensure_weekly_commission_periodic_task,
)


class Command(BaseCommand):
    help = 'Setup periodic task to auto-create the weekly commission period every Wednesday'

    def handle(self, *args, **kwargs):
        schedule, schedule_created, task, task_created = ensure_weekly_commission_periodic_task()
        if schedule is None or task is None:
            self.stdout.write(self.style.WARNING("django_celery_beat is not installed, so no periodic task was created."))
            return
        if schedule_created:
            self.stdout.write("Created Wednesday 00:00 crontab schedule.")
        else:
            self.stdout.write("Verified Wednesday 00:00 crontab schedule.")

        if task_created:
            self.stdout.write(self.style.SUCCESS(f'Successfully created task "{WEEKLY_COMMISSION_PERIOD_TASK_NAME}".'))
        else:
            self.stdout.write(self.style.SUCCESS(f'Successfully updated task "{WEEKLY_COMMISSION_PERIOD_TASK_NAME}".'))
