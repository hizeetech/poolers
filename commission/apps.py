from django.apps import AppConfig


class CommissionConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'commission'
    verbose_name = 'Commission Plan'

    def ready(self):
        from . import signals  # noqa: F401
