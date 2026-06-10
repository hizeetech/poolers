from django.apps import apps


WEEKLY_COMMISSION_PERIOD_TASK_NAME = "Ensure Weekly Commission Period"


def ensure_weekly_commission_periodic_task():
    if not apps.is_installed("django_celery_beat"):
        return None, False, None, False

    from django_celery_beat.models import CrontabSchedule, PeriodicTask

    schedule, schedule_created = CrontabSchedule.objects.get_or_create(
        minute="0",
        hour="0",
        day_of_week="wednesday",
        day_of_month="*",
        month_of_year="*",
    )
    task, task_created = PeriodicTask.objects.update_or_create(
        name=WEEKLY_COMMISSION_PERIOD_TASK_NAME,
        defaults={
            "task": "commission.tasks.ensure_weekly_commission_period",
            "crontab": schedule,
            "enabled": True,
            "queue": "commission_queue",
            "description": "Automatically creates the last completed Tuesday-to-Monday weekly commission period every Wednesday at 00:00.",
        },
    )
    return schedule, schedule_created, task, task_created
