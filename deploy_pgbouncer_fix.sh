#!/usr/bin/env bash
# ============================================================================
# deploy_pgbouncer_fix.sh
# ----------------------------------------------------------------------------
# One-shot live-server deployment script for the PgBouncer transaction-mode
# InvalidCursorName ("cursor does not exist") hotfix.
#
# What it does (in order):
#   1. Git refresh (hard reset to origin/<branch>)
#   2. Clear stale bytecode (__pycache__)
#   3. Full SIGKILL restart of Gunicorn + Celery worker + Celery beat
#      (project memory: HUP/reload may keep old code in memory -> SIGKILL required)
#   4. 4-phase post-patch verification:
#        - sweep for remaining .iterator() in hot paths
#        - module import + SafeModelChoiceField subclass check on key forms
#        - atomic fixture.save() signal-chain test (rolled back)
#        - django_errors.log delta for any new InvalidCursorName entries
#   5. Prints a rollback command block on failure so ops can revert instantly.
#
# Usage (run on LIVE server as the deployment user):
#   chmod +x deploy_pgbouncer_fix.sh
#   GUNICORN_SERVICE=gunicorn \
#   CELERY_WORKER_SERVICE=celery-worker \
#   CELERY_BEAT_SERVICE=celery-beat \
#   GIT_BRANCH=main \
#   ./deploy_pgbouncer_fix.sh
#
# The env vars above default to the values shown if omitted.
# ============================================================================
set -euo pipefail

# ---- Configuration (override via env) -------------------------------------
APP_DIR="${APP_DIR:-/var/www/shop}"
GIT_BRANCH="${GIT_BRANCH:-main}"
GUNICORN_SERVICE="${GUNICORN_SERVICE:-shop-gunicorn}"
CELERY_WORKER_SERVICE="${CELERY_WORKER_SERVICE:-shop-celery}"
CELERY_BEAT_SERVICE="${CELERY_BEAT_SERVICE:-shop-celery-beat}"
GUNICORN_PIDFILE="${GUNICORN_PIDFILE:-/var/run/gunicorn.pid}"
ERRLOG="${ERRLOG:-$APP_DIR/logs/django_errors.log}"
VENV_ACTIVATE="${VENV_ACTIVATE:-}"                  # e.g. /var/www/shop/env/bin/activate ; leave empty if none
DJANGO_SETTINGS_MODULE="${DJANGO_SETTINGS_MODULE:-poolbetting.settings}"
# Two HTTP admin endpoints that previously 500'd under PgBouncer transaction mode.
# Keep them in sync with betting/admin.py + betting/urls.py.
VIEW_SMOKE_PATHS="${VIEW_SMOKE_PATHS:-/admin/ops/loan-overdraft-center/ /crm/dashboard/}"
export DJANGO_SETTINGS_MODULE

# ---- Helpers ---------------------------------------------------------------
log()   { printf "[deploy-pgbouncer-fix %s] %s\n" "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$*"; }
die()   { local m="$1"; log "FATAL: $m"; print_rollback_help "${GOOD_SHA:-}"; exit 1; }

print_rollback_help() {
    local good_sha="${1:-<run:  git rev-parse HEAD  before deploy>}"
    local pidfile="${GUNICORN_PIDFILE:-/var/run/gunicorn.pid}"
    cat <<EOF

============================ ROLLBACK =======================================
Pre-deploy known-good commit (captured at start of this run):
    GOOD_SHA=$good_sha

Copy-paste this block to rollback instantly on failure:
    cd $APP_DIR
    git reset --hard $good_sha
    find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
    pkill -9 -f 'celery worker' 2>/dev/null || true
    pkill -9 -f 'celery beat'   2>/dev/null || true
    if [ -f $pidfile ]; then kill -9 "\$(cat $pidfile)" 2>/dev/null || true; rm -f $pidfile; fi
    pkill -9 -f 'gunicorn' 2>/dev/null || true
    sudo systemctl start $GUNICORN_SERVICE $CELERY_WORKER_SERVICE $CELERY_BEAT_SERVICE
=============================================================================
EOF
}

trap 'echo; log "Deployment FAILED on line $LINENO (exit $?)"' ERR

# ---- Stage 0: enter app dir, optional venv, record git baseline -----------
log "Entering APP_DIR=$APP_DIR"
cd "$APP_DIR"

if [ -n "${VENV_ACTIVATE}" ] && [ -f "${VENV_ACTIVATE}" ]; then
    log "Activating venv at ${VENV_ACTIVATE}"
    # shellcheck disable=SC1090
    source "${VENV_ACTIVATE}"
fi

log "Verifying python+django imports (DJANGO_SETTINGS_MODULE=$DJANGO_SETTINGS_MODULE)..."
set +e
PY_STDERR=$(python - <<'PY' 2>&1
import sys, django
django.setup()
sys.stderr.write("DJANGO_OK\n")
PY
)
PY_RC=$?
set -e
if [ "$PY_RC" -ne 0 ] || ! printf '%s' "$PY_STDERR" | grep -q "DJANGO_OK"; then
    echo "$PY_STDERR" >&2
    die "Python/Django import check failed. stdout+stderr printed above. Tips: 1) set VENV_ACTIVATE=/path/to/venv/bin/activate  2) override DJANGO_SETTINGS_MODULE if poolbetting.settings is wrong for this host"
fi
log "Python/Django import check passed"

GOOD_SHA=$(git rev-parse HEAD)
log "Pre-deploy commit: $GOOD_SHA   (SAVE THIS FOR ROLLBACK IF NEEDED)"

# ---- Stage 1: git pull (hard reset to remote branch) ----------------------
log "git fetch --all  +  git reset --hard origin/$GIT_BRANCH"
git fetch --all --prune
git reset --hard "origin/$GIT_BRANCH"
log "Deployed commit: $(git rev-parse HEAD)"

# ---- Stage 2: wipe stale bytecode -----------------------------------------
log "Clearing __pycache__ bytecode under $APP_DIR"
find "$APP_DIR" -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true

# ---- Stage 3: FULL SIGKILL restart ----------------------------------------
log "Stopping Celery worker + beat (pkill -9)..."
pkill -9 -f 'celery worker' 2>/dev/null || true
pkill -9 -f 'celery beat'   2>/dev/null || true
sleep 1

log "Stopping Gunicorn (kill -9 of PID-file + pkill sweep)..."
if [ -f "$GUNICORN_PIDFILE" ]; then
    G_PID=$(cat "$GUNICORN_PIDFILE")
    if [ -n "$G_PID" ]; then
        kill -9 "$G_PID" 2>/dev/null || true
    fi
    rm -f "$GUNICORN_PIDFILE"
fi
pkill -9 -f 'gunicorn' 2>/dev/null || true
sleep 1

log "Starting Gunicorn service: $GUNICORN_SERVICE"
sudo systemctl start "$GUNICORN_SERVICE" \
    || log "WARNING: systemctl start $GUNICORN_SERVICE returned non-zero — is it systemd-managed? (if you start gunicorn manually, do it now then press enter)"

log "Starting Celery services: $CELERY_WORKER_SERVICE + $CELERY_BEAT_SERVICE"
sudo systemctl start "$CELERY_WORKER_SERVICE" 2>/dev/null \
    || log "WARNING: systemctl start $CELERY_WORKER_SERVICE failed (ignore if you start celery manually)"
sudo systemctl start "$CELERY_BEAT_SERVICE"   2>/dev/null \
    || log "WARNING: systemctl start $CELERY_BEAT_SERVICE failed (ignore if you start celery manually)"

sleep 6
log "Process snapshot:"
ps auxf | grep -E 'gunicorn|celery' | grep -v grep || true

# ---- Stage 4/1: .iterator() disk sweep ------------------------------------
log "[1/4] .iterator() disk sweep on hot-path modules"
HOT_FILES=(
    "betting/signals.py"
    "betting/views.py"
    "betting/tasks.py"
    "betting/utils.py"
    "betting/services/ticket_refund_reversal_adjustments.py"
)
HITS=""
for f in "${HOT_FILES[@]}"; do
    if [ -f "$f" ]; then
        FH=$(grep -n "\.iterator()" "$f" 2>/dev/null || true)
        if [ -n "$FH" ]; then
            HITS+="--- $f ---\n$FH\n"
        fi
    fi
done
if [ -n "$HITS" ]; then
    printf "FAIL — remaining .iterator() calls:\n%s\n" "$HITS"
    die ".iterator() still present after deploy (see above)"
fi
log "[1/4] PASS — no .iterator() in hot paths"

# ---- Stage 4/2: module import + SafeModelChoiceField check ----------------
log "[2/4] Module imports + SafeModelChoiceField form-field check"
python - <<'PY'
import sys, django
django.setup()
from betting import forms, signals, tasks, utils  # noqa: F401  (import smoke test)

CHECK = [
    ("AdminOverdraftWalletFundingForm.super_agent",        forms.AdminOverdraftWalletFundingForm.base_fields["super_agent"]),
    ("CustomerComplaintForm.user",                         forms.CustomerComplaintForm.base_fields["user"]),
    ("CustomerComplaintActionForm.assigned_to",            forms.CustomerComplaintActionForm.base_fields["assigned_to"]),
    ("BulkMessageCampaignForm.target_agent_ids",           forms.BulkMessageCampaignForm.base_fields["target_agent_ids"]),
    ("BulkMessageCampaignForm.target_users",               forms.BulkMessageCampaignForm.base_fields["target_users"]),
    ("AdminUserCreationForm.master_agent",                 forms.AdminUserCreationForm.base_fields["master_agent"]),
    ("AdminUserCreationForm.super_agent",                  forms.AdminUserCreationForm.base_fields["super_agent"]),
    ("AdminUserCreationForm.agent",                        forms.AdminUserCreationForm.base_fields["agent"]),
    ("CreditRequestForm.recipient",                        forms.CreditRequestForm.base_fields["recipient"]),
]
fail = 0
for name, field in CHECK:
    ok = isinstance(field, (forms.SafeModelChoiceField, forms.SafeModelMultipleChoiceField))
    status = "PASS" if ok else "FAIL"
    print(f"  {status}: {name} -> {type(field).__name__}")
    if not ok:
        fail += 1
# AdminUserChangeForm hierarchy fields — only fail if the field exists and is the OLD class
for fname in ("master_agent", "super_agent", "agent"):
    field = forms.AdminUserChangeForm.base_fields.get(fname)
    if field is None:
        print(f"  WARN: AdminUserChangeForm.{fname} not declared explicitly (auto-generated ModelChoiceField — consider adding SafeModelChoiceField override)")
    else:
        ok = isinstance(field, forms.SafeModelChoiceField)
        print(f"  {'PASS' if ok else 'FAIL'}: AdminUserChangeForm.{fname} -> {type(field).__name__}")
        if not ok:
            fail += 1
sys.exit(0 if fail == 0 else 2)
PY
case $? in
    0) log "[2/4] PASS" ;;
    *) die "[2/4] FAIL — one or more fields are still using the old Django ModelChoiceField" ;;
esac

# ---- Stage 4/3: atomic fixture.save() rollback test -----------------------
log "[3/4] Atomic fixture.save() signal-chain test (rolled back, no real writes)"
python - <<'PY'
import sys, traceback
import django; django.setup()
from django.db import transaction
from django.db.utils import OperationalError
from betting.models import Fixture

fx = Fixture.objects.order_by("-pk").first()
if fx is None:
    print("  SKIP — no Fixture rows exist to test with")
    sys.exit(0)
try:
    with transaction.atomic():
        fx.status = "LIVE" if fx.status != "LIVE" else "PENDING"
        fx.save()
        raise RuntimeError("__ROLLBACK_MARKER__")
except RuntimeError as e:
    if "__ROLLBACK_MARKER__" in str(e):
        print("  PASS — fixture.save() signal chain completed cleanly, rolled back")
        sys.exit(0)
    raise
except OperationalError as e:
    print("  FAIL — OperationalError during fixture.save():", e)
    traceback.print_exc()
    sys.exit(3)
PY
case $? in
    0) log "[3/4] PASS" ;;
    *) die "[3/4] FAIL — fixture.save() signal chain hit an OperationalError (InvalidCursorName)" ;;
esac

# ---- Stage 4/4: django_errors.log delta -----------------------------------
log "[4/4] django_errors.log delta — expect zero new InvalidCursorName"
if [ -f "$ERRLOG" ]; then
    BEFORE=$(wc -l < "$ERRLOG")
    log "  $ERRLOG lines before view smoke-test: $BEFORE"

    python - <<PY
import django; django.setup()
from django.test import Client
from django.contrib.auth import get_user_model
import os

PATHS = [p for p in os.environ.get("VIEW_SMOKE_PATHS", "").split() if p]
User = get_user_model()
admin = User.objects.filter(is_superuser=True).order_by("pk").first()
if admin is None:
    print("  SKIP views smoke-test — no superuser exists. Create one or do manual curl.")
else:
    c = Client()
    c.force_login(admin)
    for path in PATHS:
        try:
            r = c.get(path, follow=True)
            print(f"  GET {path} -> HTTP {r.status_code}")
        except Exception as exc:  # noqa: BLE001
            print(f"  GET {path} -> EXCEPTION: {exc!r}")
PY

    AFTER=$(wc -l < "$ERRLOG")
    NEW=$(( AFTER - BEFORE ))
    if [ "$NEW" -lt 0 ]; then NEW=0; fi
    log "  New lines appended to $ERRLOG: $NEW"
    if [ "$NEW" -gt 0 ]; then
        NEW_BAD=$(tail -n "$NEW" "$ERRLOG" | grep -c -E "InvalidCursorName|cursor.*does not exist" || true)
        if [ "${NEW_BAD:-0}" -gt 0 ]; then
            echo
            echo "--- new InvalidCursorName hits in $ERRLOG (last $NEW lines) ---"
            tail -n "$NEW" "$ERRLOG" | grep -n -E "InvalidCursorName|cursor.*does not exist" || true
            die "[4/4] FAIL — $NEW_BAD new InvalidCursorName line(s) in django_errors.log"
        fi
    fi
    log "[4/4] PASS — no new InvalidCursorName in django_errors.log"
else
    log "[4/4] SKIP — $ERRLOG does not exist (set ERRLOG=/path/to/django_errors.log)"
fi

# ---- Final summary --------------------------------------------------------
echo
echo "==========================================================================="
echo " DEPLOY SUCCEEDED"
echo "   App dir        : $APP_DIR"
echo "   Deployed commit: $(git rev-parse HEAD)"
echo "   Baseline commit (rollback target if needed): $GOOD_SHA"
echo "   Services (re)started: $GUNICORN_SERVICE, $CELERY_WORKER_SERVICE, $CELERY_BEAT_SERVICE"
echo " For instant rollback, run:"
echo "   cd $APP_DIR && git reset --hard $GOOD_SHA && $0 with systemd restart block"
echo "==========================================================================="
