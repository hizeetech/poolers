import datetime
import json
import logging
import os
import traceback

from django.conf import settings
from django.http import JsonResponse, HttpResponseServerError, HttpResponseForbidden
from django.template import loader
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt


logger = logging.getLogger(__name__)


def _is_json_client(request):
    """Return True when request comes from AJAX / JSON client (not HTML browser).

    Used to return JSON responses instead of HTML 403/500 pages for XHR/fetch()
    clients (e.g. bet-slip "Place Bet" button) which otherwise misclassify HTML
    errors as "unexpected response (HTML)" and show a generic toast.
    """
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return True
    accept = request.headers.get("Accept", "")
    if "application/json" in accept.lower():
        return True
    content_type = request.headers.get("Content-Type", "")
    if "application/json" in content_type.lower():
        return True
    return False


def csrf_failure(request, reason=""):
    """Global CSRF_FAILURE_VIEW replacement.

    - For JSON/AJAX clients (bet-slip Place Bet button etc.): return JSON
      403 with {"success": False, "message": "..."} so frontend toasts the
      real error message instead of misclassifying the default Django HTML
      403 page as "Server Error: unexpected response (HTML)".
    - For regular browser HTML users: return the normal Django HTML 403
      page (unchanged UX for humans navigating the site).

    Register via settings.CSRF_FAILURE_VIEW = "poolbetting.views.csrf_failure".
    """
    msg = (
        "Session expired or security token invalid. Please refresh the page"
        " and try again."
    )
    if _is_json_client(request):
        return JsonResponse(
            {"success": False, "message": msg, "reason": str(reason or "")},
            status=403,
        )
    # Legacy HTML 403 for regular browser clients (same UX as Django default)
    try:
        template = loader.get_template("403csrf.html")
        body = template.render({"reason": reason, "message": msg}, request)
        return HttpResponseForbidden(body)
    except Exception:
        html = (
            "<!doctype html><html><head><title>403 Forbidden</title></head>"
            "<body><h1>403 Forbidden</h1><p>{msg}</p><p>Reason: {reason}</p>"
            "<p><a href='/'>Refresh / Go home</a></p></body></html>"
        ).format(msg=msg, reason=reason or "")
        return HttpResponseForbidden(html)


def json_error_server_middleware(get_response):
    """Lightweight middleware to wrap whole-site 500 errors for JSON clients.

    When any view raises an unhandled exception:
      * for AJAX/JSON clients -> return {"success": False, "message": "Server error"} as JSON
      * for browser HTML clients -> return normal Django 500 HTML page (unchanged)
    Also logs full traceback to django.request ERROR logger (now captured by
    LOGGING rotating file handler in settings.py -> logs/django_errors.log).

    Register via MIDDLEWARE in settings.py, near the top.
    """
    def middleware(request):
        try:
            response = get_response(request)
            return response
        except Exception as exc:
            tb = traceback.format_exc()
            try:
                logger.error(
                    "Unhandled Django exception on %s %s by %s: %s\n%s",
                    request.method,
                    request.get_full_path(),
                    getattr(request.user, "id", None),
                    str(exc),
                    tb,
                )
            except Exception:
                pass
            if _is_json_client(request):
                return JsonResponse(
                    {
                        "success": False,
                        "message": (
                            "Something went wrong on our end. Please try again"
                            " later or contact support."
                        ),
                    },
                    status=500,
                )
            # HTML client — fall through to Django default 500 handler
            raise
    return middleware
