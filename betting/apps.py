from django.apps import AppConfig


class BettingConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'betting'

    def ready(self):
        from . import admin_runtime_fixes  # noqa (registers correct admin class + fixes V12 closures / recursion)

        # ---- GATE 3 IMPERSONATION SAFETY (Django signals) ----
        # Any real login, logout, or failed-login signal ALWAYS wipes
        # lingering impersonation_active session flags.  Covers cases the
        # middleware + view gate might miss (admin login, OAuth, logout
        # via url, custom views, etc.)
        from django.dispatch import receiver
        from django.contrib.auth.signals import (
            user_logged_in, user_logged_out, user_login_failed,
        )

        @receiver(user_logged_in)
        def _wipe_impersonation_on_login(sender, request=None, user=None, **kwargs):
            if request is None:
                return
            try:
                from betting.middleware import ImpersonationMiddleware
                ImpersonationMiddleware.wipe_impersonation_state(request.session)
            except Exception:
                for key in ('impersonation_active','original_admin_id','impersonation_started_at','impersonation_log_id','impersonated_user_id'):
                    try: request.session.pop(key, None)
                    except Exception: pass

        @receiver(user_logged_out)
        def _wipe_impersonation_on_logout(sender, request=None, user=None, **kwargs):
            if request is None:
                return
            try:
                from betting.middleware import ImpersonationMiddleware
                ImpersonationMiddleware.wipe_impersonation_state(request.session)
            except Exception:
                for key in ('impersonation_active','original_admin_id','impersonation_started_at','impersonation_log_id','impersonated_user_id'):
                    try: request.session.pop(key, None)
                    except Exception: pass

        @receiver(user_login_failed)
        def _wipe_impersonation_on_login_fail(sender, credentials=None, request=None, **kwargs):
            if request is None:
                return
            try:
                from betting.middleware import ImpersonationMiddleware
                ImpersonationMiddleware.wipe_impersonation_state(request.session)
            except Exception:
                for key in ('impersonation_active','original_admin_id','impersonation_started_at','impersonation_log_id','impersonated_user_id'):
                    try: request.session.pop(key, None)
                    except Exception: pass
