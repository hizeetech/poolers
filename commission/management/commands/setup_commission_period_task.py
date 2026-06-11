from django.core.management.base import BaseCommand

from commission.scheduling import (
    WEEKLY_COMMISSION_FINALIZATION_TASK_NAME,
    WEEKLY_COMMISSION_PERIOD_TASK_NAME,
    ensure_weekly_commission_finalization_periodic_task,
    ensure_weekly_commission_periodic_task,
)


class Command(BaseCommand):
    help = 'Setup periodic tasks for weekly commission period creation and finalization'

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

        final_schedule, final_schedule_created, final_task, final_task_created = ensure_weekly_commission_finalization_periodic_task()
        if final_schedule is None or final_task is None:
            return
        if final_schedule_created:
            self.stdout.write("Created Tuesday 00:00 finalization crontab schedule.")
        else:
            self.stdout.write("Verified Tuesday 00:00 finalization crontab schedule.")

        if final_task_created:
            self.stdout.write(self.style.SUCCESS(f'Successfully created task "{WEEKLY_COMMISSION_FINALIZATION_TASK_NAME}".'))
        else:
            self.stdout.write(self.style.SUCCESS(f'Successfully updated task "{WEEKLY_COMMISSION_FINALIZATION_TASK_NAME}".'))
