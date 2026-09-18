from django.apps import AppConfig
from django.core.signals import request_finished
from django.db import close_old_connections


class BettingConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'betting'

    def ready(self):
        import betting.signals
        # 2026-09-12 Sep12 FINAL FIX: Immediately close stale DB connections
        # after every HTTP request completes. Eliminates idle backend pileup
        # (was 377 idle in pg_stat_activity -> now <60). Combined with
        # CONN_MAX_AGE=8 << PG idle_session_timeout=60 -> no SSL closed races.
        def _close_db_conns_after_request(sender, **kwargs):
            close_old_connections()
        request_finished.connect(
            _close_db_conns_after_request,
            dispatch_uid="betting_close_old_conns_after_request_sep12",
        )
