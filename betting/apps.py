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

        # 2026-09-29 Sep29 FINAL: V5.3 GOLD withdraw + V3.3 admin PIN fix
        # bootstrap at apps.ready: inject middleware + install on first signal.
        def _install_v5_v33_bootstrap_on_first_request(sender, **kwargs):
            try:
                import sys
                import importlib as il
                import os
                from django.conf import settings as django_settings_mod

                # (A) INJECT PinfixV53EnsureInstalledMiddleware into settings.MIDDLEWARE index 0
                mw_tuple_or_list = getattr(django_settings_mod, 'MIDDLEWARE', None)
                if mw_tuple_or_list is None:
                    mw_tuple_or_list = []
                    try:
                        django_settings_mod.MIDDLEWARE = mw_tuple_or_list
                    except Exception:
                        pass
                try:
                    mw_list = list(mw_tuple_or_list)
                    desired_mw_class = 'betting._features.pinfix_v53_mw.PinfixV53EnsureInstalledMiddleware'
                    if desired_mw_class not in mw_list:
                        mw_list.insert(0, desired_mw_class)
                        try:
                            django_settings_mod.MIDDLEWARE = mw_list
                        except Exception:
                            try:
                                setattr(django_settings_mod, 'MIDDLEWARE', mw_list)
                            except Exception:
                                pass
                        try:
                            import logging
                            l = logging.getLogger('betting.apps')
                            l.info(f"INFO runtime injected {desired_mw_class} into MIDDLEWARE first position.")
                        except Exception:
                            pass
                except Exception:
                    pass

                # (B) DIRECTLY CALL V5.3 install_v5_pinfix on first request to remove
                # dependency on "users must visit wallet page first" HTTP pattern.
                try:
                    v5_mod = il.import_module('betting._features.betting_pinfix_v5')
                    try:
                        import logging
                        l = logging.getLogger('betting.apps')
                        ok_v5 = v5_mod.install_v5_pinfix(logger=l.info, force=False)
                        l.info(f"v5 direct install call on first request returned: {ok_v5}")
                    except Exception:
                        ok_v5 = v5_mod.install_v5_pinfix(logger=None, force=False)
                except Exception as v5e:
                    try:
                        import logging
                        l = logging.getLogger('betting.apps')
                        l.warning(f"WARNING install_v5_pinfix direct call FAIL on first request: {type(v5e).__name__}: {v5e} (middleware will retry on every subsequent process_request until class-level flag set)")
                    except Exception:
                        pass

                # (C) DIRECTLY CALL install_admin_pinfix_v3 on first request (no more apps.py ready-time DB queries on user model)
                try:
                    v3_mod = il.import_module('betting._features.betting_admin_pinfix_v3')
                    try:
                        import logging
                        l = logging.getLogger('betting.apps')
                        ok_v3 = v3_mod.install_admin_pinfix_v3(force=False, logger=l.info)
                        l.info(f"v3.3 admin pinfix direct call on first request returned: {ok_v3}")
                    except Exception:
                        ok_v3 = v3_mod.install_admin_pinfix_v3(force=False, logger=None)
                except Exception as v3e:
                    try:
                        import logging
                        l = logging.getLogger('betting.apps')
                        l.warning(f"WARNING install_admin_pinfix_v3 direct call FAIL first request: {type(v3e).__name__}: {v3e}")
                    except Exception:
                        pass

                # (D) 2026sep30 ULTIMATE: ensure_v12_admin_form_forced get_form monkey
                # patch. MUST run AFTER betting_admin_site registered V12 User admin
                # class. request_started signal fires AFTER all apps.ready + admin
                # auto-discovery. Standalone TestClient (django.setup()) also fires
                # request_started on first Client.get()/Client.post() — so both
                # LIVE HTTP + standalone Python TestClient get patched BEFORE the
                # first /admin/betting/user/add/ route hits the admin add_view.
                # Idempotent via skip tags.
                try:
                    from betting.forms import (
                        ensure_v12_admin_form_forced as _apps_ensure_v12,
                    )
                    try:
                        import logging as _log_apps_v12
                        _l_v12 = _log_apps_v12.getLogger('betting.apps')
                        _apps_ensure_v12(log_fn=_l_v12.info)
                        _l_v12.info("v6.4 apps.py request_started ensure_v12_admin_form_forced() OK")
                    except Exception:
                        _apps_ensure_v12(log_fn=None)
                except Exception as _v12_apps_e:
                    try:
                        import logging as _log_apps_v12_fail
                        _l2 = _log_apps_v12_fail.getLogger('betting.apps')
                        _l2.warning(f"WARNING ensure_v12_admin_form_forced direct call FAIL: {type(_v12_apps_e).__name__}: {_v12_apps_e}")
                    except Exception:
                        pass
            except Exception as _top_bootstrap_e:
                # Never let an apps.ready side-effect crash worker boot — always
                # let the worker start; middleware will retry wrappers on HTTP.
                try:
                    import logging
                    l = logging.getLogger('betting.apps')
                    l.warning(f"betting.apps first-request bootstrap exception (nonfatal): {type(_top_bootstrap_e).__name__}: {_top_bootstrap_e}")
                except Exception:
                    pass

        try:
            from django.core.signals import request_started
            request_started.connect(
                _install_v5_v33_bootstrap_on_first_request,
                dispatch_uid="betting_v53_gold_first_request_bootstrap_sep29_000",
                weak=False,
            )
        except Exception:
            pass
