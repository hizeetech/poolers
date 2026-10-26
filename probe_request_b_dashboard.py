"""Read-only probe for the post-login first-render failure (Request B).

Usage:
    python probe_request_b_dashboard.py
    python probe_request_b_dashboard.py --list
    python probe_request_b_dashboard.py --user-type crm
    python probe_request_b_dashboard.py --username someuser

What it does:
1. Loads Django locally only (no network, no live server).
2. Selects candidate users by user_type (the real role discriminator).
3. force_login()s each candidate and replays the REAL post-login sequence:
       GET /dashboard/   -> user_dashboard redirects by role
   then re-requests the final resolved path to reproduce the "refresh fixes
   it" step.
4. Captures the server-side traceback via Django's got_request_exception
   signal, so a 500 is reported with its real cause rather than just a status.

Why /dashboard/ and not "/": user_login() sends every non-player/non-cashier
role to betting:user_dashboard, which is /dashboard/. The bare "/" is
frontpage(), the public landing page, and never reaches a dashboard.

Why user_type and not the `agent` field: on betting.User, `agent` is a
ForeignKey to self (the upline agent), NOT a boolean. Filtering it as a bool
silently matches nothing.

This is READ-ONLY. It never writes the database and never touches the live
server.
"""
import argparse
import os
import re
import sys
import traceback

# Default post-login landing for every non-player/non-cashier role.
DEFAULT_ENTRY = "/dashboard/"

# Non-agent / non-cashier roles: exactly the set user_dashboard() redirects to
# their own dashboard instead of the agent dashboard.
DEFAULT_ROLE_TYPES = ["crm", "finance", "retail_manager", "account_user", "admin"]

_CSRF_RE = re.compile(r'(name="csrfmiddlewaretoken"\s+value=")[^"]*(")|(csrfmiddlewaretoken=[^&\s"]+)')


def _normalize(body):
    """Blank out CSRF tokens so two renders can be compared meaningfully."""
    return _CSRF_RE.sub(r'\1<csrf>\2\3<csrf>', body)


def _ensure_django():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "poolbetting.settings")
    import django

    django.setup()


def _get_user_model():
    from django.contrib.auth import get_user_model

    return get_user_model()


def _describe(user):
    return "user_type=%s username=%s pk=%s active=%s staff=%s superuser=%s" % (
        getattr(user, "user_type", None),
        getattr(user, "username", None),
        getattr(user, "pk", None),
        getattr(user, "is_active", None),
        getattr(user, "is_staff", None),
        getattr(user, "is_superuser", None),
    )


def _print_counts(User):
    from django.db.models import Count

    print("")
    print("=== local user_type counts ===")
    total = User.objects.count()
    print("TOTAL %d" % total)
    for row in User.objects.values("user_type").annotate(n=Count("id")).order_by("-n"):
        print("  %-16s %d" % (row["user_type"], row["n"]))
    if total == 0:
        print("  (database is empty - seed a user or run this against live instead)")


def _pick_candidates(User, user_types, username, limit):
    if username:
        users = list(User.objects.filter(username=username)[:1])
        if not users:
            users = list(User.objects.filter(email=username)[:1])
        return users

    qs = User.objects.filter(is_active=True)
    if user_types:
        qs = qs.filter(user_type__in=user_types)
    return list(qs.order_by("pk")[:limit])


def _install_exception_recorder():
    """Attach the traceback of any 500 the test client swallows."""
    from django.core.signals import got_request_exception

    captured = []

    def _recorder(sender, request, **kwargs):
        captured.append(traceback.format_exc())

    got_request_exception.connect(_recorder, weak=False)
    return captured


def _summarize(response):
    templates = [t.name for t in getattr(response, "templates", []) if getattr(t, "name", None)]
    context_keys = []
    try:
        ctx = response.context
        if ctx:
            ctx = ctx[-1] if isinstance(ctx, list) else ctx
            if hasattr(ctx, "flatten"):
                context_keys = sorted(k for k in ctx.flatten().keys() if not str(k).startswith("_"))[:40]
    except Exception:
        context_keys = ["<unavailable>"]

    try:
        body = response.content.decode("utf-8", "replace")
    except Exception:
        body = "<binary>"
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    preview = " | ".join(lines[:4])[:400]

    exc = getattr(response, "exc_info", None)
    exc_line = "%s: %s" % (exc[0].__name__, exc[1]) if exc and exc[1] else None

    return {
        "status": getattr(response, "status_code", None),
        "chain": getattr(response, "redirect_chain", None),
        "templates": templates[:8],
        "context_keys": context_keys,
        "preview": preview,
        "exception": exc_line,
    }


def _print_response(label, response):
    data = _summarize(response)
    print("  --- %s ---" % label)
    print("    status        :", data["status"])
    print("    redirect_chain:", data["chain"])
    print("    templates     :", data["templates"] or "<none>")
    print("    context_keys  :", data["context_keys"] or "<none>")
    print("    exception     :", data["exception"] or "<none>")
    print("    body preview  :", data["preview"] or "<empty>")


def run_probe(username=None, path=None, user_types=None, limit=5):
    _ensure_django()
    from django.test import Client, override_settings

    User = _get_user_model()
    entry = path or DEFAULT_ENTRY
    print("MODE = READ-ONLY (local only, no live traffic)")
    print('ENTRY PATH = %s  then follow (real post-login sequence)' % entry)

    if user_types is None:
        user_types = []
    _print_counts(User)

    users = _pick_candidates(User, user_types, username, limit)
    print("")
    print("=== candidates ===")
    if not users:
        print("  none matched (user_types=%s username=%s)" % (user_types or "ALL", username))
        return 2
    for idx, user in enumerate(users, 1):
        print("  %d. %s" % (idx, _describe(user)))

    captured = _install_exception_recorder()
    rc = 0

    with override_settings(DEBUG=False, ALLOWED_HOSTS=["testserver", "localhost", "127.0.0.1"]):
        for idx, user in enumerate(users, 1):
            print("")
            print("##########################################################")
            print("PROBE %d -> %s" % (idx, _describe(user)))
            client = Client(raise_request_exception=False)
            client.force_login(user)

            captured.clear()
            try:
                first = client.get(entry, follow=True)
                _print_response("FIRST GET %s" % entry, first)
                if captured:
                    print("    >>> SERVER-SIDE TRACEBACK (first request):")
                    for block in captured:
                        for line in block.strip().splitlines()[-25:]:
                            print("      " + line)

                final_path = path or (first.redirect_chain[-1][0] if getattr(first, "redirect_chain", None) else entry)

                captured.clear()
                second = client.get(final_path, follow=True)
                _print_response("SECOND GET %s (the refresh)" % final_path, second)
                if captured:
                    print("    >>> SERVER-SIDE TRACEBACK (second request):")
                    for block in captured:
                        for line in block.strip().splitlines()[-25:]:
                            print("      " + line)

                fs = getattr(first, "status_code", None)
                ss = getattr(second, "status_code", None)
                if fs != ss:
                    print("  VERDICT: REPRODUCED - first=%s -> second=%s (refresh changed it)" % (fs, ss))
                elif fs == 500:
                    print("  VERDICT: both requests 500 - persistent, not a first-render-only bug")
                elif _normalize(first.content.decode("utf-8", "replace")) != _normalize(second.content.decode("utf-8", "replace")):
                    print("  VERDICT: same status (%s) but body differs between first and second" % fs)
                else:
                    print("  VERDICT: no first-vs-second difference for this user")
            except Exception as exc:
                rc = 1
                print("  UNCAUGHT EXCEPTION: %s: %s" % (type(exc).__name__, exc))
                traceback.print_exc()

    return rc


def main(argv):
    parser = argparse.ArgumentParser()
    parser.add_argument("--username", help="Specific username or email to probe")
    parser.add_argument("--path", default=None, help='Path to request (default: %s then follow)' % DEFAULT_ENTRY)
    parser.add_argument("--user-type", action="append", dest="user_types",
                        help="Restrict candidates to a user_type (repeatable). Default: %s" % ",".join(DEFAULT_ROLE_TYPES))
    parser.add_argument("--limit", type=int, default=5, help="Max candidates to probe")
    parser.add_argument("--list", action="store_true", help="Only print user_type counts and exit")
    args = parser.parse_args(argv)

    if args.list:
        _ensure_django()
        _print_counts(_get_user_model())
        return 0

    user_types = args.user_types if args.user_types else list(DEFAULT_ROLE_TYPES)
    return run_probe(username=args.username, path=args.path, user_types=user_types, limit=args.limit)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
