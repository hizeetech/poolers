"""
RUNTIME FIXES loaded via AppConfig.ready() EVERY worker startup.
Eliminates 3 bugs:
  1) V12 method-copy closure TypeError -> 9 methods replaced with source-code
     substitution of UserAdmin.METHOD(self, ...) -> _DJANGO_ORIG_USERADMIN_METHOD(self, ...)
     (The module-level _DJANGO_ORIG_USERADMIN_* saves in admin.py AFTER UserAdmin import
      capture originals BEFORE V12_OVERWRITE_FORCE patches Django UserAdmin class.)
  2) Django default django.contrib.admin.site has BARE UserAdmin for User model
     (no CustomUserAdmin fieldsets/form with virtual fields withdrawal_pin_new/confirm).
     -> unregister + re-register with closure-free CustomUserAdmin.
  3) Jazzmin requires ModelAdmin.form to be AdminUserChangeForm (not bare UserForm)
     for declared_fields to survive field stripping in modelform_factory.
"""
import sys as _srif_sys
import re as _srif_re
import inspect as _srif_inspect

from django.contrib.auth import get_user_model as _srif_gum
User = _srif_gum()

# Import AFTER apps installed
from betting import admin as _srif_admin_mod
from django.contrib.auth.admin import UserAdmin as _srif_DUA

# Always ensure the _DJANGO_ORIG_USERADMIN_* variables exist in admin module
# (in case admin.py import order was weird / not captured yet)
if not hasattr(_srif_admin_mod, '_DJANGO_ORIG_USERADMIN_get_readonly_fields'):
    _srif_admin_mod._DJANGO_ORIG_USERADMIN_get_readonly_fields = _srif_DUA.get_readonly_fields
    _srif_admin_mod._DJANGO_ORIG_USERADMIN_get_fields           = _srif_DUA.get_fields
    _srif_admin_mod._DJANGO_ORIG_USERADMIN_get_fieldsets        = _srif_DUA.get_fieldsets
    _srif_admin_mod._DJANGO_ORIG_USERADMIN_get_form             = _srif_DUA.get_form
    _srif_admin_mod._DJANGO_ORIG_USERADMIN_save_model           = _srif_DUA.save_model
    _srif_admin_mod._DJANGO_ORIG_USERADMIN_get_queryset         = _srif_DUA.get_queryset
    _srif_admin_mod._DJANGO_ORIG_USERADMIN_changeform_view      = _srif_DUA.changeform_view
    _srif_admin_mod._DJANGO_ORIG_USERADMIN_formfield_for_foreignkey = _srif_DUA.formfield_for_foreignkey
    _srif_admin_mod._DJANGO_ORIG_USERADMIN_get_urls             = _srif_DUA.get_urls
    _srif_admin_mod._DJANGO_ORIG_USERADMIN_add_view             = _srif_DUA.add_view
    _srif_admin_mod._DJANGO_ORIG_USERADMIN_change_view          = _srif_DUA.change_view
    _srif_admin_mod._DJANGO_ORIG_USERADMIN_changelist_view      = _srif_DUA.changelist_view
    _srif_admin_mod._DJANGO_ORIG_USERADMIN_delete_model         = _srif_DUA.delete_model
    _srif_admin_mod._DJANGO_ORIG_USERADMIN_delete_queryset      = _srif_DUA.delete_queryset
    _srif_admin_mod._DJANGO_ORIG_USERADMIN_get_search_results   = _srif_DUA.get_search_results
    _srif_admin_mod._DJANGO_ORIG_USERADMIN_get_ordering         = _srif_DUA.get_ordering
    _srif_admin_mod._DJANGO_ORIG_USERADMIN_get_list_display     = _srif_DUA.get_list_display
    _srif_admin_mod._DJANGO_ORIG_USERADMIN_get_list_filter      = _srif_DUA.get_list_filter
    _srif_admin_mod._DJANGO_ORIG_USERADMIN_has_add_permission   = _srif_DUA.has_add_permission
    _srif_admin_mod._DJANGO_ORIG_USERADMIN_has_change_permission= _srif_DUA.has_change_permission
    _srif_admin_mod._DJANGO_ORIG_USERADMIN_has_delete_permission= _srif_DUA.has_delete_permission
    _srif_admin_mod._DJANGO_ORIG_USERADMIN_has_view_permission  = _srif_DUA.has_view_permission
    _srif_admin_mod._DJANGO_ORIG_USERADMIN_get_actions          = _srif_DUA.get_actions
    _srif_admin_mod._DJANGO_ORIG_USERADMIN_message_user         = _srif_DUA.message_user
    _srif_admin_mod._DJANGO_ORIG_USERADMIN_lookup_allowed       = _srif_DUA.lookup_allowed

CUA = _srif_admin_mod.CustomUserAdmin

# --- 1a. Rewrite the 9 method source code bodies that use UserAdmin.X(self, calls)
# to use module-level saved _DJANGO_ORIG_USERADMIN_ references explicitly.
# This makes closure fully free of V12_OVERWRITE class-patching side effects.
_REWRITE_MAP = [
    ("_DJANGO_ORIG_USERADMIN_get_readonly_fields(self, ",
     "UserAdmin.get_readonly_fields(self, "),
    ("_DJANGO_ORIG_USERADMIN_get_fields(self, ",
     "UserAdmin.get_fields(self, "),
    ("_DJANGO_ORIG_USERADMIN_get_fieldsets(self, ",
     "UserAdmin.get_fieldsets(self, "),
    ("_DJANGO_ORIG_USERADMIN_get_form(self, ",
     "UserAdmin.get_form(self, "),
    ("_DJANGO_ORIG_USERADMIN_save_model(self, ",
     "UserAdmin.save_model(self, "),
    ("_DJANGO_ORIG_USERADMIN_get_queryset(self, ",
     "UserAdmin.get_queryset(self, "),
    ("_DJANGO_ORIG_USERADMIN_changeform_view(self, ",
     "UserAdmin.changeform_view(self, "),
    ("_DJANGO_ORIG_USERADMIN_formfield_for_foreignkey(self, ",
     "UserAdmin.formfield_for_foreignkey(self, "),
]
_METHOD_NAMES = (
    "get_readonly_fields","get_fields","changeform_view","formfield_for_foreignkey",
    "get_fieldsets","get_form","save_model","get_queryset"
)

_runtime_patch_n = 0
for mname in _METHOD_NAMES:
    fn = getattr(CUA, mname, None)
    if fn is None or not callable(fn): continue
    try:
        src = _srif_inspect.getsource(fn)
    except (OSError, TypeError):
        continue
    # Skip if already uses _DJANGO_ORIG (already patched, idempotent)
    if '_DJANGO_ORIG_USERADMIN_' in src:
        continue
    new_src = src
    for new_tok, old_tok in _REWRITE_MAP:
        # Replace old_tok (UserAdmin.xxx(self,) -> new _DJANGO_ORIG_xxx(self,)
        if old_tok in new_src:
            new_src = new_src.replace(old_tok, new_tok)
            _runtime_patch_n += 1
    # Also handle zero-arg super().XXX( -> same token replacement from SUPER_PATTERNS
    SUPER_PATS = [
        ("super().get_readonly_fields(", "_DJANGO_ORIG_USERADMIN_get_readonly_fields(self, "),
        ("super().get_fields(",          "_DJANGO_ORIG_USERADMIN_get_fields(self, "),
        ("super().changeform_view(",     "_DJANGO_ORIG_USERADMIN_changeform_view(self, "),
        ("super().formfield_for_foreignkey(","_DJANGO_ORIG_USERADMIN_formfield_for_foreignkey(self, "),
        ("super().get_fieldsets(",       "_DJANGO_ORIG_USERADMIN_get_fieldsets(self, "),
        ("super().get_form(",            "_DJANGO_ORIG_USERADMIN_get_form(self, "),
        ("super().save_model(",          "_DJANGO_ORIG_USERADMIN_save_model(self, "),
        ("super().get_queryset(",        "_DJANGO_ORIG_USERADMIN_get_queryset(self, "),
    ]
    for old, new in SUPER_PATS:
        if old in new_src and not new_src.strip().startswith('super().__init__'):
            new_src = new_src.replace(old, new)
            _runtime_patch_n += 1
    # Recompile & rebind the fixed method
    try:
        code = compile(new_src, f'<runtime_fix_CUA_{mname}>', 'exec')
        ns = {
            '_DJANGO_ORIG_USERADMIN_get_readonly_fields': _srif_admin_mod._DJANGO_ORIG_USERADMIN_get_readonly_fields,
            '_DJANGO_ORIG_USERADMIN_get_fields':           _srif_admin_mod._DJANGO_ORIG_USERADMIN_get_fields,
            '_DJANGO_ORIG_USERADMIN_get_fieldsets':        _srif_admin_mod._DJANGO_ORIG_USERADMIN_get_fieldsets,
            '_DJANGO_ORIG_USERADMIN_get_form':             _srif_admin_mod._DJANGO_ORIG_USERADMIN_get_form,
            '_DJANGO_ORIG_USERADMIN_save_model':           _srif_admin_mod._DJANGO_ORIG_USERADMIN_save_model,
            '_DJANGO_ORIG_USERADMIN_get_queryset':         _srif_admin_mod._DJANGO_ORIG_USERADMIN_get_queryset,
            '_DJANGO_ORIG_USERADMIN_changeform_view':      _srif_admin_mod._DJANGO_ORIG_USERADMIN_changeform_view,
            '_DJANGO_ORIG_USERADMIN_formfield_for_foreignkey': _srif_admin_mod._DJANGO_ORIG_USERADMIN_formfield_for_foreignkey,
            'UserAdmin': _srif_DUA,
            '__name__': f'__rt_cua_{mname}',
        }
        exec(code, ns)
        new_fn = ns.get(mname, None)
        if callable(new_fn):
            # Bind function to the class as unbound method
            setattr(CUA, mname, new_fn)
    except Exception as _srif_ex:
        # Ignore if recompile fails; static file patch from BLOCK 4 will handle it
        pass

# --- 1b. Ensure form attribute of the class explicitly uses AdminUserChangeForm
# (Jazzmin + Django admin rely on ModelAdmin.form for modelform_factory).
from betting.forms import AdminUserChangeForm as _srif_AUCF, AdminUserCreationForm as _srif_AUCRF
CUA.form = _srif_AUCF
CUA.add_form = _srif_AUCRF

# --- 2. Unregister bare UserAdmin from default django.contrib.admin.site
#    and re-register with our patched CustomUserAdmin which has:
#    fieldsets=7 groups (Withdrawal Security 6 fields tuple)
#    form=AdminUserChangeForm with declared_fields withdrawal_pin_new/confirm
#    get_readonly_fields/get_fields NO closures NO recursion
try:
    from django.contrib import admin as _srif_djadmin
    _srif_site = _srif_djadmin.site
    if User in getattr(_srif_site, '_registry', {}):
        _srif_site.unregister(User)
    _srif_site.register(User, CUA)
    _RUNTIME_REG_REREGISTER_OK = True
except Exception as _srif_ex:
    _RUNTIME_REG_REREGISTER_OK = False

# --- 3. Also unregister/re-register on betting_admin_site in case V12 was
# registered with broken closure copies.
try:
    from betting.admin import betting_admin_site as _srif_bas
    if User in getattr(_srif_bas, '_registry', {}):
        _srif_bas.unregister(User)
    _srif_bas.register(User, CUA)
    _RUNTIME_BETTING_SITE_OK = True
except Exception:
    _RUNTIME_BETTING_SITE_OK = False

# Diagnostics via import-time logging (only visible in import logs, harmless)
import logging as _srif_log
_logr = _srif_log.getLogger('betting.admin_runtime_fixes')
_logr.info(
    "RUNTIME ADMIN FIX APPLIED: runtime_src_patches=%s rereg_default=%s rereg_betting=%s form_is_AUCF=%s addform_is_AUCRF=%s",
    _runtime_patch_n, _RUNTIME_REG_REREGISTER_OK, _RUNTIME_BETTING_SITE_OK,
    getattr(CUA.form,'__name__',None) == 'AdminUserChangeForm',
    getattr(getattr(CUA,'add_form',None),'__name__',None) == 'AdminUserCreationForm',
)
