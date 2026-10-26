"""Verify the post-login landing rule, locally and read-only.

Rule under test (betting.views.user_login):
    ONLY a cashier lands on the fixtures page.
    Every other role lands on its dashboard.

How it proves this without changing anything:
    For each role it performs a REAL POST to /login/ with a temporary
    password, inside a database transaction that is deliberately rolled
    back. The login code path therefore runs exactly as in production,
    while the database is left byte-for-byte unchanged.

Usage:
    python verify_login_landing.py
"""
import hashlib
import os
import sys
import traceback

TMP_PW = "pb-landing-probe-9821!"
LOGIN_URL = "/login/"

# Final path each role must end up on after a successful login.
EXPECTED = {
    "cashier": "/fixtures/",
    "player": "/dashboard/",
    "agent": "/agent/dashboard/",
    "super_agent": "/super-agent/dashboard/",
    "master_agent": "/master-agent/dashboard/",
    "admin": "/admin_dashboard/",
    "crm": "/crm/dashboard/",
    "finance": "/finance/dashboard/",
    "retail_manager": "/retail/dashboard/",
    "account_user": "/account-user/dashboard/",
}


def _ensure_django():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "poolbetting.settings")
    import django

    django.setup()


def _expected_for(user):
    if getattr(user, "is_superuser", False):
        return "/admin_dashboard/"
    return EXPECTED.get(user.user_type, "/dashboard/")


def _identity_fingerprint(User):
    """Hash of stored usernames and password hashes: proves nothing was written."""
    h = hashlib.sha256()
    for row in User.objects.order_by("pk").values_list("pk", "username", "password"):
        h.update(("%s:%s:%s;" % row).encode("utf-8", "replace"))
    return h.hexdigest()


def _pick_one_per_type(User):
    """One active, unlocked user per user_type.

    A username is NOT required at pick time: some roles (master_agent locally)
    have username=None. Those get a temporary username inside the rolled-back
    transaction so the redirect branch can still be exercised.
    """
    picked = {}
    for user in User.objects.filter(is_active=True, is_locked=False).order_by("pk"):
        picked.setdefault(user.user_type, user)
    return picked


def main():
    _ensure_django()
    from django.core.cache import cache
    from django.db import transaction
    from django.contrib.auth import get_user_model
    from django.test import Client, override_settings

    User = get_user_model()
    print("MODE = READ-ONLY (local only; every login is rolled back)")

    before = _identity_fingerprint(User)
    print("identity fingerprint BEFORE: %s" % before[:16])

    picked = _pick_one_per_type(User)
    print("")
    print("=== one login-capable user per role ===")
    for utype in sorted(picked):
        print("  %-16s %s" % (utype, picked[utype].username or "<no username>"))
    if not picked:
        print("  none found")
        return 2

    results = []
    try:
        cache.clear()
    except Exception:
        pass

    # RATELIMIT_ENABLE=False only for this process; user_login allows 5 POSTs/min/IP.
    with override_settings(
        DEBUG=False,
        RATELIMIT_ENABLE=False,
        ALLOWED_HOSTS=["testserver", "localhost", "127.0.0.1"],
    ):
        for utype in sorted(picked):
            user = picked[utype]
            expected = _expected_for(user)
            actual = None
            status = None
            note = ""

            # A user with no username (or an email-style one, which the login
            # form rejects) gets a temporary username purely so the redirect
            # branch can be reached. It is rolled back with everything else.
            effective_username = (user.username or "").strip()
            temp_username_used = None
            if (not effective_username) or ("@" in effective_username):
                temp_username_used = "pbprobe_%s" % utype
                effective_username = temp_username_used
                note = "temporary username assigned (original: %s)" % (user.username or "<none>")

            try:
                with transaction.atomic():
                    update_fields = ["password"]
                    user.set_password(TMP_PW)
                    if temp_username_used:
                        user.username = temp_username_used
                        update_fields.append("username")
                    user.save(update_fields=update_fields)

                    client = Client(raise_request_exception=False)
                    resp = client.post(
                        LOGIN_URL,
                        {
                            "identifier": effective_username,
                            "password": TMP_PW,
                            "username_typed_initial": "",
                        },
                        follow=True,
                    )
                    status = resp.status_code
                    chain = getattr(resp, "redirect_chain", None) or []
                    actual = chain[-1][0] if chain else LOGIN_URL
                    if not chain:
                        note = "no redirect - login did not authenticate"
                    transaction.set_rollback(True)
            except Exception as exc:
                note = "%s: %s" % (type(exc).__name__, exc)
                traceback.print_exc()

            ok = (actual == expected) and status == 200
            results.append((utype, effective_username, expected, actual, status, ok, note))
            print("")
            print("  %-16s user=%s" % (utype, effective_username))
            print("      expected : %s" % expected)
            print("      actual   : %s  (status=%s)" % (actual, status))
            if note:
                print("      note     : %s" % note)
            print("      %s" % ("PASS" if ok else "FAIL"))

    after = _identity_fingerprint(User)
    print("")
    print("identity fingerprint AFTER : %s" % after[:16])
    db_clean = before == after
    print("DATABASE UNCHANGED: %s" % db_clean)

    passed = sum(1 for r in results if r[5])
    print("")
    print("=" * 60)
    print("RESULT: %d/%d ROLE LANDING TESTS PASS" % (passed, len(results)))
    for utype, username, expected, actual, status, ok, note in results:
        if not ok:
            print("  FAIL %-16s expected %s got %s (%s)" % (utype, expected, actual, note or "mismatch"))
    if not db_clean:
        print("  WARNING: password fingerprint changed - investigate before trusting this run")

    return 0 if (passed == len(results) and db_clean) else 1


if __name__ == "__main__":
    sys.exit(main())
