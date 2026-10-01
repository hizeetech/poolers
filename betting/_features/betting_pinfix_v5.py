import sys
import os
import re
import time
import functools as _ftg
import traceback
import json
from decimal import Decimal


_V5_VERSION_ = "5.3-GOLD-REINSTALLED"
_V5_WRAPPER_UNIQUE_TOKEN_ = "_V53GOLD_WITHDR_"  # DO NOT CHANGE THIS - identity check
_V5_VERIFY_NAME_ = "_v5_verify_withdrawal_pin"


def _v5_is_withdrawal_pin_verified_recent(request, max_age_seconds=540):
    """9 minutes double-gate (wrapper AND inner view share same logic).
    First check session timestamp. If session missing, fallback check DB last
    successful WithdrawalPinVerificationLog rows < 9min old; on success write
    session for subsequent calls so no extra DB queries.
    NEVER CLEAR THE FLAG if it's set!"""
    try:
        from django.utils import timezone
        ts_session = request.session.get('withdrawal_pin_verified_at')
        if ts_session:
            try:
                ts_val = float(ts_session)
                age = float(timezone.now().timestamp()) - ts_val
                if 0 <= age <= max_age_seconds:
                    return True
            except (TypeError, ValueError):
                pass
    except Exception:
        pass
    try:
        from django.utils import timezone
        from betting.models import WithdrawalPinVerificationLog, User
        user = getattr(request, 'user', None)
        if user and getattr(user, 'is_authenticated', False) and getattr(user, 'pk', None):
            cutoff = timezone.now() - timezone.timedelta(seconds=max_age_seconds)
            ok = bool(WithdrawalPinVerificationLog.objects.filter(
                user=user, pin_correct=True, created_at__gte=cutoff
            ).order_by('-created_at').first())
            if ok:
                try:
                    request.session['withdrawal_pin_verified_at'] = timezone.now().timestamp()
                    request.session.modified = True
                    # DB-backed session: persist now so next call sees it
                    try:
                        request.session.save()
                    except Exception:
                        pass
                except Exception:
                    pass
                return True
    except Exception:
        pass
    return False


def _v5_make_request_json_post_like(request):
    """AJAX frontend sends Content-Type application/json bodies — but native
    Django forms require QueryDict-backed request.POST. Re-parse JSON body to
    new Mutable QueryDict and replace request.POST (and if body empty do nothing).
    Avoids 'This field is required' on every AJAX form submit."""
    content_type = getattr(request, 'headers', {}).get('Content-Type', '') or ''
    if not str(content_type).lower().startswith('application/json'):
        return request
    raw_body = getattr(request, 'body', None) or b''
    if not raw_body:
        return request
    try:
        payload = json.loads(raw_body.decode('utf-8', errors='replace') or '{}')
    except Exception:
        return request
    if not isinstance(payload, dict) or not payload:
        return request
    from django.http.request import QueryDict
    new_qd = QueryDict(mutable=True)
    for k, v in payload.items():
        if v is None:
            new_qd[k] = ''
        elif isinstance(v, (list, tuple, set)):
            for item in list(v):
                new_qd.appendlist(k, '' if item is None else str(item))
        else:
            new_qd[k] = str(v)
    try:
        new_qd._mutable = False
    except Exception:
        pass
    try:
        request.POST = new_qd
    except Exception:
        try:
            setattr(request, '_post', new_qd)
        except Exception:
            pass
    return request


def _v5_verify_withdrawal_pin(request, original_view):
    """V5 wrapper: narrow atomic user select_for_update + pin attempt counter,
    NO broad atomic wrapping full view (avoids deadlocks with other rows).
    Explicit session save OUTSIDE atomic so rollback of any other step does
    not lose verification flag. Uses JSON→QueryDict parser for AJAX payloads."""
    from django.http import JsonResponse
    from django.contrib.auth import get_user_model
    from django.utils import timezone
    from django.db import transaction as djdb_trans

    UserMod = get_user_model()
    is_admin_or_super = getattr(request.user, 'is_superuser', False) or getattr(request.user, 'user_type', None) in ('admin', 'finance')

    # NEW: BUTTON DISABLED EARLY RETURN (OUTSIDE narrow atomic!). If withdraw_button_disabled by admin, don't run pin attempt counters)
    try:
        from betting.views import _withdraw_button_disabled_by_admin_check
        if _withdraw_button_disabled_by_admin_check() and not is_admin_or_super:
            msg = 'Withdrawals are currently disabled by the admin. Please check back later or contact support.'
            return JsonResponse({'status': 'error', 'success': False, 'withdraw_button_disabled_by_admin': True, 'message': msg}, status=403)
    except Exception:
        pass

    # Parse JSON body to QueryDict (so request.POST['pin'] exists for forms)
    request = _v5_make_request_json_post_like(request)

    # Read pin: try cleaned_data? No — just parse POST or JSON body again
    pin_value = None
    try:
        if getattr(request, 'POST', None):
            pv = request.POST.get('pin', None)
            if pv is not None:
                pin_value = str(pv).strip()
        if pin_value is None and request.headers.get('Content-Type','').lower().startswith('application/json'):
            try:
                p = json.loads((getattr(request, 'body', None) or b'').decode('utf-8', errors='replace') or '{}')
                if isinstance(p, dict) and p.get('pin'):
                    pin_value = str(p.get('pin', '')).strip()
            except Exception:
                pass
    except Exception:
        pass

    # HARD LOCKDOWN & SMOOTH PAUSE moved to INSIDE narrow atomic (skip atomic
    # for early returns so no unnecessary transaction spans)
    try:
        from betting.views import _is_global_withdrawals_enabled, _withdrawals_in_smooth_pause_mode, _global_withdrawals_disabled_message, _global_withdrawals_delay_seconds
        if not _is_global_withdrawals_enabled() and not is_admin_or_super:
            return JsonResponse({'status': 'error', 'withdrawals_disabled': True, 'message': _global_withdrawals_disabled_message()}, status=423)
        smooth = _withdrawals_in_smooth_pause_mode()
        if smooth and not is_admin_or_super:
            delay = _global_withdrawals_delay_seconds()
            if int(delay or 0) > 0:
                time.sleep(int(delay))
            return JsonResponse({'status': 'error', 'withdrawals_disabled': True, 'smooth_pause': True, 'message': _global_withdrawals_disabled_message()}, status=423)
    except Exception:
        pass

    user_pk = getattr(request.user, 'pk', None)
    if not user_pk or not getattr(request.user, 'is_authenticated', False):
        return JsonResponse({'status': 'error', 'message': 'Authentication required.'}, status=401)

    # ============== BEGIN NARROW ATOMIC ONLY around user + LPV write ==============
    user_obj = None
    check_result = False
    locked_bool = False
    max_attempts_int = 5
    try:
        with djdb_trans.atomic():
            user_obj = UserMod.objects.select_for_update().get(pk=user_pk)
            # auto unlock expired
            if callable(getattr(user_obj, 'maybe_auto_unlock_withdrawal', None)):
                try:
                    if user_obj.maybe_auto_unlock_withdrawal():
                        try:
                            cols_save = []
                            real_cols = {f.attname for f in UserMod._meta.get_fields() if getattr(f, 'concrete', False) and not getattr(f, 'many_to_many', False)}
                            for c in ['withdrawal_locked','withdrawal_pin_attempts','withdrawal_locked_at']:
                                if c in real_cols: cols_save.append(c)
                            if cols_save: user_obj.save(update_fields=cols_save)
                        except Exception:
                            pass
                except Exception:
                    pass

            locked_bool = bool(getattr(user_obj, 'withdrawal_locked', False))
            if locked_bool:
                expires_at = None
                try:
                    expires_at = user_obj.get_withdrawal_lock_expires_at()
                except Exception:
                    expires_at = None
                retry_at = expires_at.isoformat() if expires_at else None
                # Commit LPV log for locked attempt
                try:
                    from betting.models import WithdrawalPinVerificationLog
                    WithdrawalPinVerificationLog.objects.create(
                        user=user_obj, pin_correct=False, pin_value_dummy='',
                        user_agent=str(getattr(request,'META',{}).get('HTTP_USER_AGENT','')[:180]),
                        ip_address=str(getattr(request,'META',{}).get('REMOTE_ADDR','')[:45]),
                    )
                except Exception:
                    pass
                # Exit atomic, then return error (no session write)
                pass
            else:
                if not pin_value:
                    pass
                else:
                    check_result = bool(user_obj.check_withdrawal_pin(pin_value)) if callable(getattr(user_obj,'check_withdrawal_pin',None)) else False
                    real_cols_v = {f.attname for f in UserMod._meta.get_fields() if getattr(f, 'concrete', False) and not getattr(f, 'many_to_many', False)}
                    attempts_col_exists = 'withdrawal_pin_attempts' in real_cols_v
                    locked_col_exists = 'withdrawal_locked' in real_cols_v
                    locked_at_col_exists = 'withdrawal_locked_at' in real_cols_v
                    incremented_attempts = 0
                    if attempts_col_exists:
                        try:
                            cur = int(user_obj.withdrawal_pin_attempts or 0)
                        except Exception:
                            cur = 0
                        incremented_attempts = cur + 1
                        user_obj.withdrawal_pin_attempts = incremented_attempts
                    if not check_result and attempts_col_exists:
                        if incremented_attempts >= max_attempts_int:
                            if locked_col_exists:
                                user_obj.withdrawal_locked = True
                            if locked_at_col_exists:
                                user_obj.withdrawal_locked_at = timezone.now()
                    if check_result and attempts_col_exists:
                        user_obj.withdrawal_pin_attempts = 0
                        if locked_col_exists:
                            user_obj.withdrawal_locked = False
                        if locked_at_col_exists:
                            user_obj.withdrawal_locked_at = None
                    # Save only real cols
                    save_cols = []
                    for c in ['withdrawal_pin_attempts','withdrawal_locked','withdrawal_locked_at']:
                        if c in real_cols_v: save_cols.append(c)
                    if save_cols:
                        user_obj.save(update_fields=save_cols)
                    # LPV log
                    try:
                        from betting.models import WithdrawalPinVerificationLog
                        WithdrawalPinVerificationLog.objects.create(
                            user=user_obj, pin_correct=check_result, pin_value_dummy='',
                            user_agent=str(getattr(request,'META',{}).get('HTTP_USER_AGENT','')[:180]),
                            ip_address=str(getattr(request,'META',{}).get('REMOTE_ADDR','')[:45]),
                        )
                    except Exception:
                        pass
    except UserMod.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'User not found.'}, status=404)
    except Exception as e_narr:
        # Traceback for debugging (comment out in prod if noisy)
        return JsonResponse({'status': 'error', 'message': f'Server error during PIN verify: {type(e_narr).__name__}'}, status=500)

    # ============== EXIT NARROW ATOMIC. Session write OUTSIDE atomic ==============
    if locked_bool:
        expires_at = None
        try:
            expires_at = user_obj.get_withdrawal_lock_expires_at() if user_obj else None
        except Exception:
            expires_at = None
        retry_at = expires_at.isoformat() if expires_at else None
        msg = f"Withdrawal access locked due to too many failed PIN attempts. Contact administrator or try after 24 hours.{(' Retry available after: '+str(retry_at)) if retry_at else ''}"
        return JsonResponse({'status': 'locked', 'message': msg, 'retry_at': retry_at, 'attempts': max_attempts_int}, status=423)
    if not pin_value:
        return JsonResponse({'status': 'error', 'message': 'Withdrawal PIN is required.'}, status=400)
    if not check_result:
        attempts_now = int(getattr(user_obj,'withdrawal_pin_attempts', 0) or 0) if user_obj else 1
        remaining = max(1, max_attempts_int - attempts_now)
        return JsonResponse({'status': 'error', 'message': f'Invalid withdrawal PIN. Attempt {attempts_now}/{max_attempts_int}. {remaining} attempt(s) remaining before lock.', 'attempts': attempts_now, 'max_attempts': max_attempts_int}, status=400)

    # CORRECT PIN path: set session verification OUTSIDE any atomic so no rollback
    try:
        from django.utils import timezone as tz_now_alias
        request.session['withdrawal_pin_verified_at'] = float(tz_now_alias.now().timestamp())
        request.session.modified = True
        try:
            request.session.save()
        except Exception:
            pass
    except Exception:
        pass
    return JsonResponse({'status': 'success', 'message': 'Withdrawal PIN verified successfully. Proceed to submit withdrawal.'})


def _v5_withdraw_funds(request, original_view):
    """V5 wrapper for withdraw_funds: JSON→QueryDict, double-gate (no clear after
    pass), passes control to original view for full form validation/ledger logic.
    GLOBAL LOCKDOWN & SMOOTH PAUSE handled BEFORE broad @atomic is entered via
    early return — original view decorator would otherwise run sleep/deadlock
    inside @transaction.atomic (idle in tx for delay seconds)."""
    from django.http import JsonResponse
    from django.contrib.auth import get_user_model

    is_admin_or_super = getattr(request.user, 'is_superuser', False) or getattr(request.user, 'user_type', None) in ('admin', 'finance')
    expects_json = (request.headers.get('Content-Type','') or '').lower().startswith('application/json')

    # NEW: BUTTON DISABLED EARLY RETURN BEFORE CALLING original_view (no broad atomic, no sleep, no deadlocks)
    try:
        from betting.views import _withdraw_button_disabled_by_admin_check
        if _withdraw_button_disabled_by_admin_check() and not is_admin_or_super:
            msg = 'Withdrawals are currently disabled by the admin. Please check back later or contact support.'
            return JsonResponse({'status': 'error', 'success': False, 'withdraw_button_disabled_by_admin': True, 'message': msg}, status=403)
    except Exception:
        pass

    # Parse JSON body to QueryDict BEFORE calling original view
    request = _v5_make_request_json_post_like(request)

    # DOUBLE-GATE CHECK (wrapper AND inner view both call _v5_is_withdrawal_pin_verified_recent)
    # CRITICAL: NEVER CLEAR the session flag inside wrapper! Inner view clears
    # it AFTER successful withdrawal only (via native _clear_withdrawal_pin_verified).
    if request.method == 'POST':
        if not _v5_is_withdrawal_pin_verified_recent(request):
            if expects_json:
                return JsonResponse({'status': 'error', 'message': 'Withdrawal PIN verification required.'}, status=400)

    # HARD LOCKDOWN & SMOOTH PAUSE (EARLY RETURN BEFORE NATIVE BROAD @ATOMIC)
    try:
        from betting.views import _is_global_withdrawals_enabled, _withdrawals_in_smooth_pause_mode, _global_withdrawals_disabled_message, _global_withdrawals_delay_seconds
        if not _is_global_withdrawals_enabled() and not is_admin_or_super:
            msg = _global_withdrawals_disabled_message()
            if expects_json:
                return JsonResponse({'status': 'error', 'withdrawals_disabled': True, 'message': msg}, status=423)
        smooth_cfg = _withdrawals_in_smooth_pause_mode()
        if smooth_cfg and not is_admin_or_super:
            delay = _global_withdrawals_delay_seconds()
            if int(delay or 0) > 0:
                time.sleep(int(delay or 0))
            msg = _global_withdrawals_disabled_message()
            if expects_json:
                return JsonResponse({'status': 'error', 'withdrawals_disabled': True, 'smooth_pause': True, 'message': msg}, status=423)
    except Exception:
        pass

    # Call original native view — it has full validation/ledger/debit logic already
    return original_view(request)


def install_v5_pinfix(logger=None, force: bool = False):
    """Install wrapper functions into betting.views globals + betting.urls urlpatterns entries.
    Constraint2 standalone: called after apps.ready (middlewares). Idempotent via identity check."""
    try:
        import django
        from django.apps import apps as djapps
        if not djapps.apps.ready:
            if logger: logger(f"[v5.install] apps not ready skip")
            return False
    except Exception:
        pass
    try:
        import importlib as il
        from betting import views as betting_views_module
        from betting import urls as betting_urls_module
    except Exception as im_e:
        if logger: logger(f"[v5.install] import betting views/urls FAIL: {im_e}")
        return False
    try:
        U_G = None
        try:
            from django.contrib.auth import get_user_model
            U_G = get_user_model()
        except Exception:
            pass

        # ============== WRAP verify_withdrawal_pin ==============
        orig_verify = getattr(betting_views_module, 'verify_withdrawal_pin', None)
        if not callable(orig_verify):
            if logger: logger(f"[v5.install] FAIL orig verify NOT callable")
            return False
        # Idempotent identity check
        if getattr(orig_verify, "__name__", "") == _V5_VERIFY_NAME_ and not force:
            pass
        else:
            @_ftg.wraps(orig_verify)
            def _wrap_verify_renamed_(request, *a, **kw):
                return _v5_verify_withdrawal_pin(request, lambda req: orig_verify(req, *a, **kw))
            _wrap_verify_renamed_.__name__ = _V5_VERIFY_NAME_
            setattr(betting_views_module, 'verify_withdrawal_pin', _wrap_verify_renamed_)

        # ============== WRAP withdraw_funds (unique token AVOID functools.wraps __name__ copy bug) ==============
        orig_withdraw = getattr(betting_views_module, 'withdraw_funds', None)
        if not callable(orig_withdraw):
            if logger: logger(f"[v5.install] FAIL orig withdraw NOT callable")
            return False

        @_ftg.wraps(orig_withdraw)
        def _V53GOLD_WITHDR_(request, *a, **kw):
            oview_lambda = lambda req: orig_withdraw(req, *a, **kw)
            return _v5_withdraw_funds(request, oview_lambda)
        _V53GOLD_WITHDR_.__name__ = _V5_WRAPPER_UNIQUE_TOKEN_

        already_ok_withdraw_name = (getattr(betting_views_module.withdraw_funds, "__name__", "") == _V5_WRAPPER_UNIQUE_TOKEN_)
        if (not already_ok_withdraw_name) or force:
            setattr(betting_views_module, 'withdraw_funds', _V53GOLD_WITHDR_)

        # ============== ALSO rewrite betting.urls urlpatterns entries matching index 28/29 (views callbacks) ==============
        try:
            patterns = getattr(betting_urls_module, 'urlpatterns', None)
            if patterns and isinstance(patterns, list):
                for idx, pat in enumerate(patterns):
                    callback = getattr(pat, 'callback', None)
                    if not callable(callback):
                        continue
                    cname = getattr(callback, '__name__', '')
                    # Rewrite verify
                    if ('verify' in cname.lower() and 'pin' in cname.lower()) or (cname and cname == 'verify_withdrawal_pin'):
                        if getattr(callback, '__name__', '') != _V5_VERIFY_NAME_:
                            @_ftg.wraps(callback)
                            def _wrap_url_verify(request, *a, _cb=callback, **kw):
                                return _v5_verify_withdrawal_pin(request, lambda req: _cb(req, *a, **kw))
                            _wrap_url_verify.__name__ = _V5_VERIFY_NAME_
                            pat.callback = _wrap_url_verify
                    # Rewrite withdraw
                    if (cname and cname == 'withdraw_funds') or ('withdraw' in cname.lower() and 'fund' in cname.lower() and 'verify' not in cname.lower()):
                        if getattr(callback, '__name__', '') != _V5_WRAPPER_UNIQUE_TOKEN_:
                            ocb = callback
                            @_ftg.wraps(ocb)
                            def _V53GOLD_WITHDR_URL_(request, *a, _ocb=ocb, **kw):
                                oview = lambda req: _ocb(req, *a, **kw)
                                return _v5_withdraw_funds(request, oview)
                            _V53GOLD_WITHDR_URL_.__name__ = _V5_WRAPPER_UNIQUE_TOKEN_
                            pat.callback = _V53GOLD_WITHDR_URL_
        except Exception as url_e:
            if logger: logger(f"[v5.install] urlpatterns rewrite FAIL (nonfatal): {url_e}")

        if logger:
            ok_v = (getattr(betting_views_module.verify_withdrawal_pin, '__name__', '') == _V5_VERIFY_NAME_)
            ok_w = (getattr(betting_views_module.withdraw_funds, '__name__', '') == _V5_WRAPPER_UNIQUE_TOKEN_)
            logger(f"[v5.install OK VERSION={_V5_VERSION_}] verify wrapper={ok_v} withdraw wrapper unique token={ok_w}")
        return True
    except Exception as e:
        if logger:
            logger(f"[v5.install] FATAL: {type(e).__name__}: {e}\n{traceback.format_exc()}")
        return False


if __name__ == "__main__":
    print(f"betting_pinfix_v5 module v{_V5_VERSION_} (standalone lazy, call install_v5_pinfix(logger=print) after apps.ready)")
    sys.exit(0)
