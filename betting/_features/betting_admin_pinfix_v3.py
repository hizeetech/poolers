import sys
import os
import re
import time
import functools as _ftg
import traceback


_ADMIN_PINFIX_V33_OPTA_VERSION_ = "3.3-OPTION-A-GOLD"
_ADMIN_PINFIX_V33_OPTA_TAG_ = "__V33_OPTA_PATCHED__"


def _is_plaintext_pin_not_hash(v: str) -> bool:
    if not isinstance(v, str):
        return False
    s = v.strip()
    if not s:
        return False
    if len(s) > 16:
        return False
    if s.startswith("pbkdf2_") or s.startswith("argon2") or s.startswith("bcrypt"):
        return False
    if s.startswith("$"):
        return False
    if re.search(r"[^0-9]", s):
        return False
    if len(s) < 4 or len(s) > 6:
        return False
    return True


def install_admin_pinfix_v3(force: bool = False, logger=None) -> bool:
    try:
        import django
        from django.apps import apps as djapps
        from django.contrib.auth import get_user_model
    except Exception as e:
        if logger:
            logger(f"[betting_admin_pinfix_v3.3] django import FAIL: {e}")
        return False

    try:
        if not djapps.apps.ready:
            if logger:
                logger("[betting_admin_pinfix_v3.3] apps NOT ready, skip install (apps.py retry next call)")
            return False
    except Exception:
        pass

    try:
        from betting import admin as betting_admin_mod
    except Exception as e:
        if logger:
            logger(f"[betting_admin_pinfix_v3.3] betting.admin import FAIL: {e}")
        return False

    Uv33 = get_user_model()

    try:
        admin_site_inst = getattr(betting_admin_mod, "betting_admin_site", None)
        if admin_site_inst is None:
            if logger:
                logger("[betting_admin_pinfix_v3.3] betting_admin_site NOT found on betting.admin module")
            return False
    except Exception as e:
        if logger:
            logger(f"[betting_admin_pinfix_v3.3] betting_admin_site lookup FAIL: {e}")
        return False

    try:
        registered_admin_obj = admin_site_inst._registry.get(Uv33)
        if registered_admin_obj is None:
            if logger:
                logger(f"[betting_admin_pinfix_v3.3] User model NOT registered on betting_admin_site (not ready yet)")
            return False
        V12_ADMIN_CLASS_REFERENCE_ = type(registered_admin_obj)
        if logger:
            logger(f"INFO ✅ v3.3 DYNAMICALLY found V12 admin class {V12_ADMIN_CLASS_REFERENCE_.__name__} via betting_admin_site._registry[User]! (No more ImportError function-local class.)")
    except Exception as e:
        if logger:
            logger(f"[betting_admin_pinfix_v3.3] dynamic V12 class lookup FAIL: {e}")
        return False

    if getattr(V12_ADMIN_CLASS_REFERENCE_, _ADMIN_PINFIX_V33_OPTA_TAG_, False) and not force:
        if logger:
            logger(f"[betting_admin_pinfix_v3.3] already patched idempotent skip (class tag {_ADMIN_PINFIX_V33_OPTA_TAG_} found).")
        return True

    save_target_name_used = None
    save_model_fast_ORIGINAL_V12_ = getattr(V12_ADMIN_CLASS_REFERENCE_, "save_model_fast", None)
    if save_model_fast_ORIGINAL_V12_ is None or not callable(save_model_fast_ORIGINAL_V12_):
        # FALLBACK: V12 class does not expose separate save_model_fast on class level
        # (rollback version uses standard Django ModelAdmin.save_model entrypoint).
        # Wrap save_model instead - 100% exists on every ModelAdmin regardless of V12 rewrite.
        save_model_BASE_ = getattr(V12_ADMIN_CLASS_REFERENCE_, "save_model", None)
        if save_model_BASE_ is None or not callable(save_model_BASE_):
            if logger:
                logger(f"[betting_admin_pinfix_v3.3] V12 class has NEITHER save_model_fast NOR save_model callable - cannot wrap!")
            return False
        save_target_name_used = "save_model"
        save_model_fast_ORIGINAL_V12_ = save_model_BASE_
        if logger:
            logger(f"INFO v3.3 V12 class no save_model_fast on class level -> FALLBACK wrap base ModelAdmin.save_model (always exists, POST hook works same).")
    else:
        save_target_name_used = "save_model_fast"
        if logger:
            logger(f"INFO v3.3 V12 class save_model_fast found -> use V12 fast path wrap.")

    @_ftg.wraps(save_model_fast_ORIGINAL_V12_)
    def _V33_OPTA_SAVE_MODEL_FAST_POST_HOOK_(admin_self_inst, request, obj_sm, form, change):
        save_model_fast_ORIGINAL_V12_(admin_self_inst, request, obj_sm, form, change)
        try:
            new_pin = None
            new_pw_raw = None
            try:
                cd_get = getattr(form, "cleaned_data", None)
                if isinstance(cd_get, dict):
                    new_pin_fld_v = cd_get.get("withdrawal_pin_new") if "withdrawal_pin_new" in cd_get else None
                    cfm_pin_fld_v = cd_get.get("withdrawal_pin_confirm") if "withdrawal_pin_confirm" in cd_get else None
                    if isinstance(new_pin_fld_v, str) and isinstance(cfm_pin_fld_v, str):
                        new_pin_fld_s = new_pin_fld_v.strip()
                        cfm_pin_fld_s = cfm_pin_fld_v.strip()
                        if new_pin_fld_s and cfm_pin_fld_s and new_pin_fld_s == cfm_pin_fld_s:
                            if _is_plaintext_pin_not_hash(new_pin_fld_s):
                                new_pin = new_pin_fld_s
                    pw_fld_v = None
                    if "password" in cd_get:
                        pw_fld_v = cd_get.get("password")
                    elif "password1" in cd_get:
                        pw_fld_v = cd_get.get("password1")
                    if isinstance(pw_fld_v, str) and pw_fld_v.strip():
                        new_pw_raw = pw_fld_v.strip()
            except Exception:
                pass

            if not new_pin:
                rf = (str(form.cleaned_data.get("withdrawal_pin") or "") or "").strip() if isinstance(getattr(form, "cleaned_data", None), dict) else ""
                if rf and _is_plaintext_pin_not_hash(rf):
                    new_pin = rf

            if isinstance(new_pw_raw, str) and new_pw_raw:
                try:
                    if callable(getattr(obj_sm, "set_password", None)):
                        obj_sm.set_password(new_pw_raw)
                        u_cols_exist_pw = {f.attname for f in Uv33._meta.get_fields() if getattr(f, "concrete", False) and not getattr(f, "many_to_many", False)}
                        cols_save_pw = [c for c in ["password", "failed_login_attempts", "is_locked"] if c in u_cols_exist_pw]
                        if cols_save_pw:
                            obj_sm.save(update_fields=cols_save_pw)
                except Exception as e2:
                    if logger:
                        logger(f"WARN v3.3 set_password post hook fail: {e2}")

            if new_pin and callable(getattr(obj_sm, "set_withdrawal_pin", None)):
                obj_sm.set_withdrawal_pin(new_pin)
                u_cols_exist = {f.attname for f in Uv33._meta.get_fields() if getattr(f, "concrete", False) and not getattr(f, "many_to_many", False)}
                cols_save = [c for c in ["withdrawal_pin", "withdrawal_pin_attempts", "withdrawal_locked", "withdrawal_locked_at"] if c in u_cols_exist]
                if cols_save:
                    if hasattr(obj_sm, "withdrawal_pin_attempts"):
                        try:
                            obj_sm.withdrawal_pin_attempts = 0
                        except Exception:
                            pass
                    if hasattr(obj_sm, "withdrawal_locked"):
                        try:
                            obj_sm.withdrawal_locked = False
                        except Exception:
                            pass
                    if hasattr(obj_sm, "withdrawal_locked_at"):
                        try:
                            obj_sm.withdrawal_locked_at = None
                        except Exception:
                            pass
                    obj_sm.save(update_fields=cols_save)
        except Exception as e_sm:
            if logger:
                logger(f"ERROR v3.3 save post hook pin hash FAIL: {e_sm}\n{traceback.format_exc()}")
            return

    setattr(V12_ADMIN_CLASS_REFERENCE_, save_target_name_used, _V33_OPTA_SAVE_MODEL_FAST_POST_HOOK_)
    if logger:
        logger(f"INFO OK v3.3 DIRECTLY WRAPPED V12 admin class {V12_ADMIN_CLASS_REFERENCE_.__name__}.{save_target_name_used} = post hook pin hash CASE A plaintext raw hash box only (NO WORKFLOW B new/confirm fields inject to fieldsets -> avoids FieldError 500).")

    setattr(V12_ADMIN_CLASS_REFERENCE_, _ADMIN_PINFIX_V33_OPTA_TAG_, True)

    if logger:
        logger(f"INFO OK install_admin_pinfix_v3.3 OPTION-A FINAL SUCCESS: V12 {save_target_name_used} wrap CASE A plaintext hash box auto-hash applied idempotent; NO AUCF runtime fields added; NO fieldsets injection -> ZERO FieldError 500 crash risk.")
    return True


if __name__ == "__main__":
    print(f"betting_admin_pinfix v{_ADMIN_PINFIX_V33_OPTA_VERSION_} (STANDALONE MODULE - LAZY LOAD VIA APPS.PY / MIDDLEWARE TRIGGER)")
    print("Call install_admin_pinfix_v3(force=True, logger=print) from apps.py or first request hook.")
    sys.exit(0)
