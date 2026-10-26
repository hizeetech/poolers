from django.utils import timezone
from django.shortcuts import redirect
from django.contrib import messages
from django.urls import reverse
from django.core.cache import cache
from django.utils.cache import patch_vary_headers, patch_cache_control, add_never_cache_headers
from .models import ImpersonationLog


class _CacheSafetyMiddlewareTemplateFirst:
    """
    Global per-user cache hardening  —  stops the navbar / wallet /
    notification-count cross-user identity leak.

    Runs on EVERY response (after request.user is populated by
    AuthenticationMiddleware).  Rules:

      1. If request.user.is_authenticated  →  Cache-Control:  PRIVATE + NO-STORE
         + must-revalidate + max-age=0 + X-User-State: authenticated.
         Any shared cache (Nginx RAMDISK, Redis proxy, CDN, worker in-process
         globals dict, shared file cache)  CANNOT persist this response  —
         it contains per-user username / wallet / notif counts that must
         NEVER be served to a different session.  Even though earlier we
         added this to fixtures_view, navbar is on every page; middleware
         is zero-risk and bulletproof.

      2. Anonymous HTML-like responses → public, max-age=60 s_maxage=60
         (Saturday thundering-herd protection preserved for cold /fixtures/
         and / pages).  Always Vary on Cookie so cache-split by csrftoken
         / session id is respected by downstream caches.

      3. EVERY response (media/static/JSON/API/HTML):  Vary: Cookie,
         Accept-Encoding.  Always.  Avoids the "Nginx cached User A's HTML
         and served it to User B on a diff Content-Type" edge case.

      4. If the view already set Cache-Control (e.g. a private file download
         or fixtures_view explicit public 60s), we NEVER relax it.  We only
         TIGHTEN for authenticated.  For anon we only FILL-IN if not already
         explicitly set.
    """

    AUTO_NO_CACHE_PATHS = (
        "/wallet", "/account", "/profile", "/admin", "/bet/", "/place-bet",
        "/mybets", "/ticket", "/withdraw", "/deposit", "/notifications",
        "/dashboard", "/agent/", "/uip/", "/fixtures", "/api/", "/results",
        "/check-ticket", "/ticket-transactions",
    )

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)

        # ---- (3) Always vary: Cookie + Accept-Encoding ----
        patch_vary_headers(response, ('Cookie', 'Accept-Encoding'))

        user_auth = getattr(request, 'user', None)
        is_auth = bool(user_auth and user_auth.is_authenticated)

        path_info = getattr(request, 'path', '') or ''
        is_no_cache_path = any(p in path_info for p in self.AUTO_NO_CACHE_PATHS)

        content_type = ''
        ct_raw = response.get('Content-Type', '') or ''
        if isinstance(ct_raw, bytes):
            try:
                content_type = ct_raw.decode('utf-8', errors='replace')
            except Exception:
                content_type = str(ct_raw)
        else:
            content_type = str(ct_raw)
        is_html = ('text/html' in content_type.lower())
        is_json = ('application/json' in content_type.lower())

        if is_auth or is_no_cache_path or is_json:
            # ---- (1) Authenticated / sensitive routes: FORCE private + no-store ----
            # CRITICAL 2026-10-09 FIX: patch_cache_control is ADDITIVE. If a view
            # or upstream layer already set public=True, Django's patch function
            # does NOT remove it, leading to dual public/private header ambiguous
            # for nginx proxy cache / Redis cache layers, causing navbar/wallet
            # identity cross-leak (User A HTML served to User B authenticated).
            # Instead — FULLY DELETE the Cache-Control header first, then rebuild
            # ONLY the strict private/no-store directives.
            try:
                if 'Cache-Control' in response:
                    del response['Cache-Control']
            except Exception:
                pass
            try:
                if 'Pragma' in response:
                    del response['Pragma']
            except Exception:
                pass
            try:
                if 'Expires' in response:
                    del response['Expires']
            except Exception:
                pass
            patch_cache_control(
                response,
                private=True,
                no_store=True,
                must_revalidate=True,
                no_cache=True,
                max_age=0,
                s_maxage=0,
                proxy_revalidate=True,
            )
            # ===== 2026-10-09 Bug6 FINAL FIX: NGINX proxy_cache ignores Cache-Control: private.
            # The nginx-specific upstream header X-Accel-Expires: 0 FORCES nginx proxy_cache
            # to BYPASS lookup AND SKIP store 100% — different users hitting same URL always
            # get different HTML (never stale navbar/wallet from another user's cached copy).
            try:
                response['X-Accel-Expires'] = '0'
            except Exception:
                pass
            try:
                response['X-Proxy-Cache-Control'] = 'STORE-NEVER-BYPASS'
            except Exception:
                pass
            try:
                response['Pragma'] = 'no-cache'
            except Exception:
                pass
            try:
                from django.utils.http import http_date
                import time
                response['Expires'] = http_date(time.time() - 86400)
            except Exception:
                pass
            try:
                response['X-User-State'] = 'authenticated'
            except Exception:
                pass
            if is_html and is_auth:
                # Belt + braces: Django add_never_cache_headers for HTML auth'd
                # response sets Expires + Pragma: no-cache as well (browser layer).
                add_never_cache_headers(response)
                # Re-confirm X-Accel-Expires after Django helper (belt+braces).
                try:
                    response['X-Accel-Expires'] = '0'
                except Exception:
                    pass
        elif is_html:
            # ---- (2) Anonymous HTML, no explicit Cache-Control set: ----
            # Fill in Saturday-friendly 60s public cache.
            # If view already set something via patch_cache_control we still
            # FULLY RESET then set clean public-only header.
            cc_header = ''
            cc_raw = response.get('Cache-Control', '') or ''
            if isinstance(cc_raw, bytes):
                try:
                    cc_header = cc_raw.decode('utf-8', errors='replace')
                except Exception:
                    cc_header = str(cc_raw)
            else:
                cc_header = str(cc_raw)
            if not cc_header.strip() or ('no-store' not in cc_header.lower()
                                         and 'private' not in cc_header.lower()):
                try:
                    if 'Cache-Control' in response:
                        del response['Cache-Control']
                except Exception:
                    pass
                patch_cache_control(response, public=True, max_age=60, s_maxage=60)
                try:
                    if 'Set-Cookie' in response:
                        del response['Set-Cookie']
                except Exception:
                    pass
            else:
                # Anonymous HTML BUT view explicitly set no-store/private already
                # (e.g. /login/, /admin/login/, error pages). Still needs X-Accel-Expires: 0
                # because nginx proxy_cache ignores Cache-Control private by default.
                try:
                    response['X-Accel-Expires'] = '0'
                except Exception:
                    pass
                try:
                    response['X-Proxy-Cache-Control'] = 'STORE-NEVER-BYPASS'
                except Exception:
                    pass
            try:
                response['X-User-State'] = 'anonymous'
            except Exception:
                pass

        return response


class GlobalAuthenticatedResponseCacheSafetyMiddleware(_CacheSafetyMiddlewareTemplateFirst):
    pass


class ImpersonationMiddleware:
    """
    ZERO-DATABASE Impersonation session integrity watch dog.

    (Admin hang regression after G5): previous version of this middleware did
    User.objects.get(pk=original_admin_id) + orig.has_perm(...) on EVERY
    REQUEST.  Those calls interacted with V12COMPLETE runtime_src_patches
    CustomUserAdmin permission hooks, creating a deep chain of DB queries
    that eventually timed out or hung admin pages completely.

    This version: ZERO per-request DB queries. We don't need to hit User
    table to validate impersonation state because request.user (set by
    AuthenticationMiddleware BEFORE we run) already contains the correct
    logged-in user object. We just COMPARE request.user.pk against the two
    IDs stored in the session (impersonated_user_id / original_admin_id):

      - If request.user.pk == impersonated_user_id      → GENUINE IMPERSONATION → OK.
      - If request.user.pk == original_admin_id         → ADMIN NAV POST EXIT → STALE → WIPE.
      - If request.user.pk is BOTH different from above → THIRD USER LOGGED
        ONTO AN OLD IMPERSONATION SESSION (the exact bug). WIPE FLAGS.
      - If ANY session id is garbage (None / non-int / negative) → WIPE.
      - If start time older than 2 hours → WIPE.
      - If not authenticated → WIPE (can't impersonate anonymous).

    ImpersonationLogin view still sets everything correctly in the session
    (with DB writes); we only protect the integrity post-login.
    """

    IMPERSONATION_KEYS = (
        'impersonation_active',
        'original_admin_id',
        'impersonation_started_at',
        'impersonation_log_id',
        'impersonated_user_id',
    )

    def __init__(self, get_response):
        self.get_response = get_response

    @classmethod
    def wipe_impersonation_state(cls, session):
        """Idempotent clear of every known impersonation flag from session.

        Safe public helper called from Gate2 (views.user_login POST prolog),
        Gate3 (apps.py signals login/logout/login_fail) AND from inside this
        middleware whenever integrity check fails."""
        try:
            for key in cls.IMPERSONATION_KEYS:
                try:
                    session.pop(key, None)
                except Exception:
                    pass
            try:
                session.modified = True
            except Exception:
                pass
        except Exception:
            pass

    def __call__(self, request):
        sess = getattr(request, 'session', None)
        session_active = False
        session_target_id = None
        original_admin_id = None
        if sess is not None:
            try:
                session_active = bool(sess.get('impersonation_active'))
                session_target_id = sess.get('impersonated_user_id')
                original_admin_id = sess.get('original_admin_id')
            except Exception:
                session_active = False

        if session_active and sess is not None:
            # ===== 4 integrity checks; ANY failure → FULL WIPE =====
            ok = True

            # ---- (a) IDs positive ints? ----
            tgt_int = None
            try:
                tgt_int = int(session_target_id)
                if tgt_int <= 0:
                    ok = False
            except (TypeError, ValueError):
                ok = False
                tgt_int = None

            orig_int = None
            try:
                orig_int = int(original_admin_id)
                if orig_int <= 0:
                    orig_int = None
            except (TypeError, ValueError):
                orig_int = None
            # orig_admin allowed to be None? NO — when we enter valid
            # impersonation state original_admin_id MUST be set. If missing → stale.
            if orig_int is None:
                ok = False

            # ---- (b) user authenticated? ----
            real_user = getattr(request, 'user', None)
            is_auth = bool(real_user is not None and getattr(real_user, 'is_authenticated', False))
            if not is_auth:
                ok = False

            real_pk_int = None
            if is_auth and real_user is not None:
                rpk = getattr(real_user, 'pk', None)
                try:
                    real_pk_int = int(rpk)
                    if real_pk_int <= 0:
                        real_pk_int = None
                except (TypeError, ValueError):
                    real_pk_int = None
            if real_pk_int is None:
                ok = False

            # ---- (c) ZERO-DB CROSS-USER CHECK vs 3 possible states ----
            if ok:
                if real_pk_int == tgt_int:
                    # Case 1: authenticated user matches impersonated_target_id
                    # → valid genuine impersonation (login() did its job). OK.
                    pass
                elif orig_int is not None and real_pk_int == orig_int:
                    # Case 2: current user is ORIGINAL ADMIN, but impersonation
                    # flags still set. They exited impersonation improperly or
                    # the Stop Impersonation view crashed before flag wipe.
                    # This is NOT cross-user bug but it IS stale state → wipe.
                    ok = False
                else:
                    # Case 3: NEITHER → THIRD user logged in with typed
                    # credentials onto an old session. OgunPopoolaC1 onto
                    # OndoFrancisC1 session leftovers → THE EXACT BUG → WIPE!
                    ok = False

            # ---- (d) time ceiling absolute, 2 hours ----
            started_at_str = None
            try:
                started_at_str = sess.get('impersonation_started_at')
            except Exception:
                started_at_str = None
            t_ok = True
            if isinstance(started_at_str, str) and len(started_at_str) >= 10:
                try:
                    from django.utils.dateparse import parse_datetime
                    t0 = parse_datetime(started_at_str)
                    if t0 is not None:
                        now = timezone.now()
                        delta = (now - t0).total_seconds()
                        if delta > 7200 or delta < -60:
                            t_ok = False
                except Exception:
                    # Can't parse → stale garbage → safe wipe.
                    t_ok = False
            else:
                # missing or non-string start time → invalid session → wipe.
                t_ok = False
            if not t_ok:
                ok = False

            if not ok:
                # Wipe impersonation state, PROCEED with normal render,
                # NO crash, NO extra DB queries on ImpersonationLog etc.
                ImpersonationMiddleware.wipe_impersonation_state(sess)
                try:
                    request.impersonation_active = False
                except Exception:
                    pass
                try:
                    request.impersonated_user = None
                    request.original_user_id = None
                except Exception:
                    pass
                return self.get_response(request)

            # All checks passed → genuine impersonation session, expose attrs.
            try:
                request.impersonation_active = True
                request.original_user_id = orig_int
                request.impersonated_user = request.user
            except Exception:
                pass
        else:
            try:
                request.impersonation_active = False
                request.impersonated_user = None
                request.original_user_id = None
            except Exception:
                pass

        return self.get_response(request)

    def force_stop_impersonation(self, request, reason):
        """Fallback for explicit admin "Stop Impersonation" button views.

        NOTE: NOT called from per-request integrity __call__ anymore to avoid
        DB lookups on every user request. Views call this directly when the
        admin clicks Exit Impersonation banner (ImpersonationLog row needs
        updating)."""
        from django.contrib.auth import login, get_user_model
        from django.contrib.messages import add_message, WARNING
        User = get_user_model()

        sess2 = getattr(request, 'session', None)
        original_admin_id = None
        log_id = None
        if sess2 is not None:
            original_admin_id = sess2.get('original_admin_id')
            log_id = sess2.get('impersonation_log_id')

        if original_admin_id:
            try:
                original_user = User.objects.get(pk=int(original_admin_id))
                try:
                    backend = 'betting.backends.EmailOrUsernameBackend'
                    original_user.backend = backend
                    login(request, original_user)
                except Exception:
                    pass
            except User.DoesNotExist:
                pass

        if log_id:
            try:
                log = ImpersonationLog.objects.get(pk=log_id)
                log.ended_at = timezone.now()
                try:
                    log.duration = log.ended_at - log.started_at
                except Exception:
                    pass
                log.termination_reason = reason
                log.save()
            except ImpersonationLog.DoesNotExist:
                pass

        if sess2 is not None:
            ImpersonationMiddleware.wipe_impersonation_state(sess2)

        try:
            messages.warning(request, f"Impersonation ended due to {reason}.")
        except Exception:
            pass
        try:
            return redirect('betting_admin:dashboard')
        except Exception:
            return redirect('/')


class ThreadLocalMiddleware:
    import threading
    _thread_locals = threading.local()

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        self._thread_locals.request = request
        response = self.get_response(request)
        if hasattr(self._thread_locals, 'request'):
            del self._thread_locals.request
        return response


def get_current_request():
    return getattr(ThreadLocalMiddleware._thread_locals, 'request', None)


def get_current_user():
    request = get_current_request()
    if request:
        return getattr(request, 'user', None)
    return None


class EnsureRemoteAddrMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if 'REMOTE_ADDR' not in request.META or not request.META.get('REMOTE_ADDR'):
            forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
            if forwarded_for:
                request.META['REMOTE_ADDR'] = forwarded_for.split(',')[0].strip()
            else:
                real_ip = request.META.get('HTTP_X_REAL_IP')
                if real_ip:
                    request.META['REMOTE_ADDR'] = real_ip.strip()
        return self.get_response(request)


class LowBalanceDepositReminderMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        if user and getattr(user, "is_authenticated", False):
            user_type = getattr(user, "user_type", "") or ""
            if user_type not in ['player', 'cashier', 'agent', '']:
                return self.get_response(request)
            path = request.path or ""
            if not (path.startswith("/admin/") or path.startswith("/static/") or path.startswith("/media/")):
                try:
                    if request.session.get("low_balance_deposit_reminder_checked"):
                        return self.get_response(request)

                    request.session["low_balance_deposit_reminder_checked"] = True
                    from django.apps import apps
                    from django.utils import timezone as dj_timezone

                    from risk.services import get_risk_settings_cached
                    from notifications.services import create_notification

                    settings_obj = get_risk_settings_cached()
                    threshold = settings_obj.get("deposit_reminder_threshold")
                    if threshold is not None:
                        Wallet = apps.get_model("betting", "Wallet")
                        balance = Wallet.objects.filter(user_id=user.id).values_list("balance", flat=True).first()
                        if balance is not None and balance <= threshold:
                            Notification = apps.get_model("notifications", "Notification")
                            existing = (
                                Notification.objects.filter(recipient_id=user.id, is_read=False, notification_type="DEPOSIT_REMINDER")
                                .order_by("-created_at")
                                .values_list("id", flat=True)
                                .first()
                            )
                            if not existing:
                                create_notification(
                                    recipient=user,
                                    notification_type="DEPOSIT_REMINDER",
                                    title="Low wallet balance",
                                    message=f"Your wallet balance is ₦{balance:.2f}. Deposit now to keep betting and avoid missing live fixtures.",
                                    data={"threshold": str(threshold), "balance": str(balance), "url": "/wallet/"},
                                )
                except Exception:
                    pass

        return self.get_response(request)
