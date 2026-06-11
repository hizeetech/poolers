from django.apps import apps
from django.db.models.signals import post_migrate
from django.dispatch import receiver

from .scheduling import (
    ensure_weekly_commission_finalization_periodic_task,
    ensure_weekly_commission_periodic_task,
)


@receiver(post_migrate)
def ensure_commission_periodic_tasks(sender, **kwargs):
    if getattr(sender, "name", "") != "commission":
        return
    if not apps.is_installed("django_celery_beat"):
        return
    ensure_weekly_commission_periodic_task()
    ensure_weekly_commission_finalization_periodic_task()
