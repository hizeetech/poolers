from django.apps import AppConfig


class BettingConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'betting'

    def ready(self):
        import betting.signals
