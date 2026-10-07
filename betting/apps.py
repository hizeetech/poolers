from django.apps import AppConfig

class BettingConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'betting'
    def ready(self):
        from . import admin_runtime_fixes  # noqa (registers correct admin class + fixes V12 closures / recursion)
