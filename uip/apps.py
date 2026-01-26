from django.apps import AppConfig


class UipConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'uip'
    verbose_name = 'Unified Intelligence Platform'

    def ready(self):
        import uip.signals
