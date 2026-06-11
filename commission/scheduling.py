from django.apps import apps


WEEKLY_COMMISSION_PERIOD_TASK_NAME = "Ensure Weekly Commission Period"
WEEKLY_COMMISSION_FINALIZATION_TASK_NAME = "Finalize Last Completed Weekly Commissions"


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
            "description": "Automatically creates the current Tuesday-to-Monday weekly commission period every Wednesday at 00:00.",
        },
    )
    return schedule, schedule_created, task, task_created


def ensure_weekly_commission_finalization_periodic_task():
    if not apps.is_installed("django_celery_beat"):
        return None, False, None, False

    from django_celery_beat.models import CrontabSchedule, PeriodicTask

    schedule, schedule_created = CrontabSchedule.objects.get_or_create(
        minute="0",
        hour="0",
        day_of_week="tuesday",
        day_of_month="*",
        month_of_year="*",
    )
    task, task_created = PeriodicTask.objects.update_or_create(
        name=WEEKLY_COMMISSION_FINALIZATION_TASK_NAME,
        defaults={
            "task": "commission.tasks.finalize_last_completed_weekly_commissions",
            "crontab": schedule,
            "enabled": True,
            "queue": "commission_queue",
            "description": "Finalizes the last completed Tuesday-to-Monday weekly commission period every Tuesday at 00:00, excluding pending tickets.",
        },
    )
    return schedule, schedule_created, task, task_created
