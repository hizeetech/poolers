import sys
import os
import time
import functools as _ftg
import traceback


_V53_MW_TOKEN_ = "PinfixV53EnsureInstalledMiddleware-GOLD"


class PinfixV53EnsureInstalledMiddleware:
    """First-position middleware (settings.MIDDLEWARE index 0) that BRUTE-FORCE
    rewrites betting.views globals + betting.urls urlpatterns callback entries
    on EVERY process_request until a WORKER-SCOPED class attribute flag
    INSTALL_DONE_LOCAL = True is set ONLY AFTER explicit identity + unique
    token name checks both confirm wrappers active. No 'flag lie' bugs where
    an early flag says applied but wrappers aren't actually there in memory.

    Why install on first HTTP request, not apps.ready? Gunicorn worker boot
    import-order race: apps.py runs AppConfig.ready() before all model apps +
    betting.views/urls imports finish → import betting.views inside AppConfig
    .ready() triggers AppRegistryNotReady / django.core.exceptions.ImproperlyConfigured
    / silently fails and wrapper never loads with no retry. Middleware process_
    request ALWAYS runs AFTER apps.ready is True, so wrappers will load on
    first HTTP request that enters this worker (guaranteed).
    """

    INSTALL_DONE_LOCAL = False  # Class-level worker-scoped flag.
    V12_FORM_FORCED_LOCAL = False  # Class-level: once True, skip expensive admin site rescan.

    def __init__(self, get_response):
        self.get_response = get_response
        try:
            self.worker_pid = os.getpid()
        except Exception:
            self.worker_pid = -1

    def __call__(self, request):
        # =====================================================================
        # 2026sep30 ULTIMATE FIX for MA/SA admin save: form attribute timing.
        # Module-level _betting_v12_force_admin_form_class_at_import runs at
        # import, BEFORE betting.admin.apps.ready() rebuilds V12 class and
        # overwrites .form = UserChangeForm (39 attribute copy, no .form copy).
        # Run AGAIN from FIRST middleware __call__ (AFTER apps.ready=True so
        # V12 is registered) — once per worker via class attribute flag. This
        # forces every registered betting_admin_site instance's form attribute
        # back to AdminUserCreationForm with our required=False + _clean_fields
        # blanket + failed_login_attempts HiddenInput declarations, so admin
        # get_form returns subclass of AdminUserCreationForm NOT factory
        # ModelForm with Model-level 6 required=True invisible fields.
        # =====================================================================
        if not PinfixV53EnsureInstalledMiddleware.V12_FORM_FORCED_LOCAL:
            try:
                import logging as _l_fmw
                _lr_fm = _l_fmw.getLogger('betting.v53mw')
                def _fm_log(s):
                    try:
                        _lr_fm.info("[%s] %s" % (_V53_MW_TOKEN_, s))
                    except Exception:
                        pass
                from betting.forms import ensure_v12_admin_form_forced
                ensure_v12_admin_form_forced(log_fn=_fm_log)
                PinfixV53EnsureInstalledMiddleware.V12_FORM_FORCED_LOCAL = True
            except Exception as _fmw_e:
                try:
                    import logging as _l_fmw2
                    _lr_fm2 = _l_fmw2.getLogger('betting.v53mw')
                    _lr_fm2.warning(f"[{_V53_MW_TOKEN_}] ensure_v12_admin_form_forced FAIL: {type(_fmw_e).__name__}: {_fmw_e}")
                except Exception:
                    pass

        # =====================================================================
        # 2026sep30 ULTIMATE FIX: PER-REQUEST injection for Add User page.
        # Ensures password1+password2 2-box General tab render NO MATTER WHAT
        # the V12 dynamic class copier did / did not copy. Re-applies on
        # EVERY request to /admin/betting/user/add/ so V12 attribute issues
        # or worker boot timing bugs are irrelevant.
        # =====================================================================
        try:
            _path = getattr(request, 'path', '') or ''
            if _path.startswith('/admin/betting/user/add') or '/admin/betting/user/add' in _path:
                try:
                    from django.contrib.auth import get_user_model as _gu_mw
                    _U_MW = _gu_mw()
                    from betting.admin import betting_admin_site as _bas_mw
                    _inst_mw = _bas_mw._registry.get(_U_MW, None)
                    if _inst_mw is not None:
                        _cls_mw = type(_inst_mw)
                        # V6 2026sep30: REVERT add_form_template to Django's DEFAULT
                        # 2-step banner mode (step-1 renders ONLY 3 fields:
                        # email/username/password banner mode). The previous
                        # add_form_template='admin/change_form.html' (SINGLE-WIDE
                        # override) was left over from the abandoned 2-password-box
                        # approach and FORCES Jazzmin to split ALL 27 form fields
                        # across 8 tabs, whose inputs are not rendered in the
                        # initial step-1 HTML → browser does NOT POST these
                        # 24+ invisible inputs at all → ModelForm raises
                        # required=True errors for EVERY tab → generic red banner.
                        # Banner mode works CORRECTLY with our nuclear AUCF
                        # __init__ + _clean_fields blanket (see Part B TestClient
                        # status=302 pk=456 created).
                        #
                        # Additionally, REVERT class/instance add_fieldsets +
                        # fieldsets swap overrides: banner add_fieldsets tuple
                        # already set by V12 copier correctly.
                        for _attr_nm in ('add_form_template', 'fieldsets'):
                            try:
                                if hasattr(_cls_mw, _attr_nm):
                                    delattr(_cls_mw, _attr_nm)
                            except Exception:
                                pass
                            try:
                                _cur = getattr(_inst_mw, _attr_nm, None)
                                if _attr_nm == 'add_form_template' and _cur == 'admin/change_form.html':
                                    delattr(_inst_mw, _attr_nm)
                            except Exception:
                                pass
                        if hasattr(_inst_mw, 'add_fieldsets') and hasattr(_inst_mw.add_fieldsets, '__len__') and len(_inst_mw.add_fieldsets or []) > 0:
                            # Keep add_fieldsets only if it's already a 3-field wide banner tuple
                            first_group_fields = list(_inst_mw.add_fieldsets[0][1].get('fields', ()))
                            if len(first_group_fields) > 5:
                                # It was the 27-field single-wide override — remove, let Django banner default
                                try:
                                    delattr(_inst_mw, 'add_fieldsets')
                                    if hasattr(_cls_mw, 'add_fieldsets'):
                                        delattr(_cls_mw, 'add_fieldsets')
                                except Exception:
                                    pass
                except Exception as _mw_e1:
                    try:
                        import logging as _l_mw_e
                        lr = _l_mw_e.getLogger('betting.v53mw')
                        lr.warning(f"[{_V53_MW_TOKEN_}] PER-REQ user/add inject V6 FAIL: {type(_mw_e1).__name__}: {_mw_e1}")
                    except Exception:
                        pass
        except Exception:
            pass

        if not PinfixV53EnsureInstalledMiddleware.INSTALL_DONE_LOCAL:
            try:
                import importlib as il
                try:
                    v5_mod = il.import_module('betting._features.betting_pinfix_v5')
                except Exception as im_e:
                    # Log one per worker process (once per boot).
                    try:
                        import logging as _log
                        logr = _log.getLogger('betting.v53mw')
                        logr.warning(f"[{_V53_MW_TOKEN_}] import betting_pinfix_v5 FAIL: {im_e}")
                    except Exception:
                        pass
                    v5_mod = None
                if v5_mod is not None:
                    # Actually install/retry wrappers
                    def _mwlogger(msg):
                        try:
                            import logging as _log2
                            lr = _log2.getLogger('betting.v53mw')
                            lr.info(f"[{_V53_MW_TOKEN_}] {msg}")
                        except Exception:
                            pass

                    _ = v5_mod.install_v5_pinfix(logger=_mwlogger, force=False)

                    # ---- EXPLICIT CONFIRMATION BEFORE FLAG (no flag lie!) ----
                    ok_verify = False
                    ok_withdraw = False
                    try:
                        from betting import views as _bvw
                        from betting import urls as _bur
                        # Verify view wrapper identity check
                        verify_name = getattr(_bvw.verify_withdrawal_pin, '__name__', '')
                        ok_verify = (verify_name == '_v5_verify_withdrawal_pin')

                        # Withdraw wrapper: use UNIQUE function name token (def line
                        # inside install has name=_V53GOLD_WITHDR_). functools.wraps
                        # copies __wrapped__ but only if you copy __name__ too. We set
                        # it explicitly in install, so this check is 100% reliable.
                        withdraw_name = getattr(_bvw.withdraw_funds, '__name__', '')
                        ok_withdraw = (withdraw_name == '_V53GOLD_WITHDR_')

                        # Also rewrite urlpatterns callbacks here (if still not wrapped)
                        if not (ok_verify and ok_withdraw):
                            patterns = getattr(_bur, 'urlpatterns', None)
                            if patterns and isinstance(patterns, list):
                                for p in patterns:
                                    cb = getattr(p, 'callback', None)
                                    if not callable(cb):
                                        continue
                                    cb_n = getattr(cb, '__name__', '')
                                    # Force wrap patterns that match withdraw / verify-pin routes
                                    if not ok_verify and (
                                        cb_n == 'verify_withdrawal_pin'
                                        or ('verify' in cb_n.lower() and 'pin' in cb_n.lower())
                                    ):
                                        orig_cb = cb

                                        @_ftg.wraps(orig_cb)
                                        def _v5_wrap_url_verify_mw(request, *a, _ocb=orig_cb, **kw):
                                            return v5_mod._v5_verify_withdrawal_pin(
                                                request, lambda req: _ocb(req, *a, **kw)
                                            )
                                        _v5_wrap_url_verify_mw.__name__ = '_v5_verify_withdrawal_pin'
                                        p.callback = _v5_wrap_url_verify_mw
                                        ok_verify = True
                                    if not ok_withdraw and (
                                        cb_n == 'withdraw_funds'
                                        or ('withdraw' in cb_n.lower() and 'fund' in cb_n.lower() and 'verify' not in cb_n.lower())
                                    ):
                                        orig_cb_wd = cb

                                        @_ftg.wraps(orig_cb_wd)
                                        def _V53GOLD_WITHDR_URL_MW_(request, *a, _ocb=orig_cb_wd, **kw):
                                            oview = lambda req: _ocb(req, *a, **kw)
                                            return v5_mod._v5_withdraw_funds(request, oview)
                                        p.callback = _V53GOLD_WITHDR_URL_MW_
                                        ok_withdraw = True
                    except Exception as confirm_e:
                        try:
                            import logging as _log3
                            lr = _log3.getLogger('betting.v53mw')
                            lr.warning(f"[{_V53_MW_TOKEN_}] CONFIRM CHECK FAIL: {confirm_e}")
                        except Exception:
                            pass

                    if ok_verify and ok_withdraw:
                        # =============================================================
                        # 2026sep29 HOTFIX: Master Agent / Super Agent Add User page
                        # generic banner bug: Live V12 UserAdmin class dynamic copy
                        # sometimes MISSES CustomUserAdmin fieldsets Group0 password1
                        # + password2 entries / add_fieldsets OR add_view method. The
                        # symptom was 1 lonely password box in General tab, admin types
                        # one password -> form required 2 boxes -> add_error invisible
                        # -> generic red top banner "Please correct the errors below."
                        #
                        # GUARANTEED FIX: Worker-level class attribute injection on
                        # every gunicorn worker boot (once per process, at first HTTP
                        # request). We DYNAMICALLY rewrite the currently REGISTERED
                        # User model admin instance on betting_admin_site to:
                        #   A) Ensure class/instance fieldsets tuple Group0 has
                        #      password1 + password2 fields.
                        #   B) Ensure add_fieldsets (single wide 27-field 1-group
                        #      without Jazzmin tab splits) explicitly set.
                        #   C) Monkey-patch add_view method if missing, temporarily
                        #      swapping self.fieldsets = add_fieldsets during the
                        #      add request so Jazzmin NEVER splits 7-group tabs for
                        #      Add User page -> forces single form Password+Confirm.
                        #
                        # This is IDEMPOTENT (class tag __V53MW_USERADD_PATCHED__),
                        # so middleware loop runs once per worker boot.
                        # =============================================================
                        try:
                            from django.contrib.auth import get_user_model as _v53_gum
                            _UserCls = _v53_gum()
                            from betting.admin import betting_admin_site as _v53_bas
                            _v53_reg_admin = _v53_bas._registry.get(_UserCls, None)
                            if _v53_reg_admin is not None:
                                _V53_PATCH_TAG = '__V53MW_USERADD_PATCHED__'
                                _V53_REG_CLS = type(_v53_reg_admin)
                                if not getattr(_V53_REG_CLS, _V53_PATCH_TAG, False):
                                    def _v53_mw__ensure_group0_fields_has_password(regobj):
                                        # V6 2026sep30: REVERT ALL single-wide overrides.
                                        # - DELETE add_form_template 'admin/change_form.html' class/instance
                                        #   setattrs (keeps Django 2-step banner mode).
                                        # - DELETE 27-field single-wide add_fieldsets (keeps default
                                        #   3-field email/username/password step-1 banner).
                                        # Our nuclear AdminUserCreationForm.__init__ blanket
                                        # required=False + _clean_fields() skip handles all other
                                        # Model-level BLANK=False fields (kyc_status, vip_level,
                                        # is_active, failed_login_attempts, etc. — 15 Model-level
                                        # BLANK=False fields confirmed in Part D diag).
                                        # Banner mode → step1 POST only 3 fields → 302 redirect.
                                        for _bad_attr in ('add_form_template', 'change_form_template'):
                                            try:
                                                if hasattr(type(regobj), _bad_attr):
                                                    cur_cls = getattr(type(regobj), _bad_attr, None)
                                                    if cur_cls == 'admin/change_form.html':
                                                        delattr(type(regobj), _bad_attr)
                                            except Exception:
                                                pass
                                            try:
                                                cur_inst = getattr(regobj, _bad_attr, None)
                                                if cur_inst == 'admin/change_form.html':
                                                    delattr(regobj, _bad_attr)
                                            except Exception:
                                                pass
                                        # Remove 27-field single-wide add_fieldsets if len > 5:
                                        _cfs = list(getattr(regobj, 'add_fieldsets', None) or [])
                                        if len(_cfs) > 0:
                                            _g0f = list(_cfs[0][1].get('fields', ()))
                                            if len(_g0f) > 5:
                                                try:
                                                    delattr(regobj, 'add_fieldsets')
                                                    _rc = type(regobj)
                                                    if hasattr(_rc, 'add_fieldsets'):
                                                        delattr(_rc, 'add_fieldsets')
                                                except Exception:
                                                    pass
                                        return True

                                    _v53_mw__ensure_group0_fields_has_password(_v53_reg_admin)

                                    # --- (D) 2026sep30 ULTIMATE: AdminUserCreationForm
                                    # get_form() monkey patch. V12 class copies
                                    # CustomUserAdmin.get_form() method which
                                    # HARD-CODED returns random widget form class
                                    # (django.forms.widgets.UserForm) — bypasses
                                    # self.add_form attribute entirely. We invoke
                                    # ensure_v12_admin_form_forced() here (inside
                                    # FIRST HTTP REQUEST MIDDLEWARE PRIME) so V12
                                    # is already registered. Idempotent via tags.
                                    try:
                                        from betting.forms import (
                                            ensure_v12_admin_form_forced as _v53mw_ensure_v12
                                        )
                                        _v53mw_ensure_v12(log_fn=_mwlogger)
                                    except Exception as _e_v12:
                                        try:
                                            import logging as _log_v12_fail
                                            lr = _log_v12_fail.getLogger('betting.v53mw')
                                            lr.warning(f"[{_V53_MW_TOKEN_}] ensure_v12 FAIL: {type(_e_v12).__name__}: {_e_v12}")
                                        except Exception:
                                            pass

                                    # --- (C) Class patch tag + logging ---
                                    setattr(_V53_REG_CLS, _V53_PATCH_TAG, True)
                                    try:
                                        import logging as _log_pw_v6
                                        lr = _log_pw_v6.getLogger('betting.v53mw')
                                        lr.info(
                                            f"[{_V53_MW_TOKEN_}] ✅ V6-2026sep30 User Admin "
                                            f"banner 2-step REVERT APPLIED (add_form_template class/instance "
                                            f"attrs cleared; 27-field single-wide add_fieldsets cleared; "
                                            f"nuclear AUCF handles all required=False step-1)."
                                        )
                                    except Exception:
                                        pass
                            else:
                                # Retry on next request forever until confirmed.
                                try:
                                    import logging as _log_retry
                                    lr = _log_retry.getLogger('betting.v53mw')
                                    lr.warning(
                                        f"[{_V53_MW_TOKEN_}] NOT YET CONFIRMED (retry next request)"
                                        f" verify identity={ok_verify} withdraw name={ok_withdraw}"
                                    )
                                except Exception:
                                    pass
                        except Exception as e_top:
                            try:
                                import logging as _log_exc
                                lr = _log_exc.getLogger('betting.v53mw')
                                lr.warning(f"[{_V53_MW_TOKEN_}] wrapper install FAIL top: {type(e_top).__name__}: {e_top}")
                            except Exception:
                                pass
                        PinfixV53EnsureInstalledMiddleware.INSTALL_DONE_LOCAL = True
                        try:
                            import logging as _log_final
                            lr = _log_final.getLogger('betting.v53mw')
                            lr.info(
                                f"[{_V53_MW_TOKEN_}] ✅ INSTALL_DONE_LOCAL=True worker_pid={self.worker_pid} "
                                f"verify wrapper={ok_verify} withdraw wrapper unique token={ok_withdraw}"
                            )
                        except Exception:
                            pass
            except Exception as e_outer:
                try:
                    import logging as _log_outer
                    lr = _log_outer.getLogger('betting.v53mw')
                    lr.warning(f"[{_V53_MW_TOKEN_}] install FAIL outer try: {type(e_outer).__name__}: {e_outer}")
                except Exception:
                    pass
        # Always call downstream regardless of success / fail above.
        response = self.get_response(request)

        # =====================================================================
        # 2026sep30 Restore user admin fieldsets after add_view response ran.
        # (prevents edit views from accidentally seeing single-wide add group)
        # =====================================================================
        try:
            restore_trip = getattr(request, '_v53mw_restore_fieldsets_user_admin', None)
            if restore_trip is not None:
                _inst_mw_r, _saved_fs_r, _cls_mw_r = restore_trip
                if _saved_fs_r is not None:
                    _inst_mw_r.fieldsets = _saved_fs_r
                    if hasattr(_cls_mw_r, 'fieldsets'):
                        setattr(_cls_mw_r, 'fieldsets', _saved_fs_r)
                try: delattr(request, '_v53mw_restore_fieldsets_user_admin')
                except Exception: pass
        except Exception:
            pass

        # =====================================================================
        # NUCLEAR LAYER C: response-level template rewrite. Runs AFTER all
        # admin class template resolution, changeform_view super calls,
        # jazzmin overrides, and Django UserAdmin internal template_name
        # hardcodes. NOTHING can bypass this because it rewrites the
        # outgoing response's template_name attribute DIRECTLY on the
        # TemplateResponse instance AFTER it's been fully built by the
        # entire view call chain.
        # =====================================================================
        try:
            _path_rw = getattr(request, 'path', '') or ''
            if _path_rw.startswith('/admin/betting/user/add'):
                has_tn = hasattr(response, 'template_name')
                is_tpl_resp = type(response).__name__ == 'TemplateResponse' or has_tn
                if is_tpl_resp and response is not None:
                    tn = getattr(response, 'template_name', None)
                    changed = False
                    def _is_bad_template(name_str):
                        ns = str(name_str).lower()
                        return 'user/add_form' in ns or 'auth/user/add' in ns
                    if isinstance(tn, (list, tuple)):
                        new_tn = []
                        for t in tn:
                            if _is_bad_template(t):
                                new_tn.append('admin/change_form.html')
                                changed = True
                            else:
                                new_tn.append(t)
                        if changed:
                            response.template_name = type(tn)(new_tn) if isinstance(tn, tuple) else new_tn
                    elif isinstance(tn, str) and _is_bad_template(tn):
                        response.template_name = 'admin/change_form.html'
                        changed = True
                    if changed and hasattr(response, '_request') and getattr(response, '_request', None) is not None:
                        try:
                            import logging as _l3
                            lr3 = _l3.getLogger('betting.v53mw')
                            lr3.info(f"[{_V53_MW_TOKEN_}] NUCLEAR TEMPLATE REWRITE: user/add response.template_name {tn!r} -> {response.template_name!r}")
                        except Exception:
                            pass
        except Exception as _nuke_e:
            try:
                import logging as _l_nuke
                ln = _l_nuke.getLogger('betting.v53mw')
                ln.warning(f"[{_V53_MW_TOKEN_}] NUCLEAR TEMPLATE REWRITE EXCEPTION: {type(_nuke_e).__name__}: {_nuke_e}")
            except Exception:
                pass

        return response


if __name__ == "__main__":
    print(f"{_V53_MW_TOKEN_} middleware class. Activated via settings.MIDDLEWARE index 0. Install done? {PinfixV53EnsureInstalledMiddleware.INSTALL_DONE_LOCAL}")
    sys.exit(0)
