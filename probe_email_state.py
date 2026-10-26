"""READ-ONLY probe: why are only 12/37 email toggles on, and where do the
email addresses REALLY come from at runtime.

Nothing is written outside /tmp.  No restart, no migrate, no DB writes.

  GATE A  per-toggle truth table: is it a real model field, what is its DB value
  GATE B  where WITHDRAWAL_ADMIN_EMAILS / ADMIN_NOTIFICATION_EMAIL / ... win from
  GATE C  .env mtime vs service start time (is your edit already live?)

Usage:  python probe_email_state.py
"""
import os, sys, getpass

try:
    import paramiko
except ImportError:
    print('paramiko missing.  Run:  pip install paramiko')
    sys.exit(1)

HOST = '172.236.9.63'
USER = 'stakenaija'
REMOTE_ROOT = '/var/www/shop'
UNITS = ['shop-sync', 'shop-ws', 'shop-celery', 'shop-celery-beat']
ADDR_KEYS = ('WITHDRAWAL_ADMIN_EMAILS', 'ADMIN_NOTIFICATION_EMAIL',
             'DEPOSIT_ADMIN_NOTIFICATION_EMAIL', 'DEFAULT_FROM_EMAIL')

# ---------------- GATE A: toggles truth table ----------------
TOGGLES = '''\
import os, sys
sys.path.insert(0, '/var/www/shop')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'poolbetting.settings')
import django
django.setup()
from django.db import connection
from betting.models import SiteConfiguration
from betting import email_prefs as p

have = {f.name for f in SiteConfiguration._meta.get_fields()}
c = SiteConfiguration.load()

print('siteconfiguration rows     = %d  ids=%s'
      % (SiteConfiguration.objects.count(), list(SiteConfiguration.objects.values_list('id', flat=True))))
cur = connection.cursor()
cur.execute("SELECT count(*) FROM information_schema.columns "
            "WHERE table_name='betting_siteconfiguration' AND column_name LIKE 'email_%'")
print('email_* columns in the DB  = %s' % cur.fetchone()[0])

missing, on, off = [], [], []
print('')
print('%-46s %-6s %s' % ('TOGGLE', 'FIELD?', 'DB VALUE'))
for n in p.ALL_TOGGLE_FIELDS:
    inmodel = n in have
    val = getattr(c, n, None) if inmodel else None
    print('%-46s %-6s %s' % (n, 'yes' if inmodel else 'NO', val))
    if not inmodel:
        missing.append(n)
    elif val:
        on.append(n)
    else:
        off.append(n)

print('')
print('ALL_TOGGLE_FIELDS total    = %d' % len(p.ALL_TOGGLE_FIELDS))
print('registered name not a model field = %d  %s' % (len(missing), missing))
print('ON  (%2d) = %s' % (len(on), on))
print('OFF (%2d) = %s' % (len(off), off))
print('')
master_on, suppressed, allowed = p.load_policy()
print('load_policy() master_on    = %s' % master_on)
print('load_policy() suppressed   = %s' % sorted(suppressed))
print('load_policy() allowlist    = %s' % (allowed if allowed is None else sorted(allowed)))
print('filter_with_policy sample  = %s'
      % p.filter_with_policy(['good@example.com', 'not-an-email', 'x@dead.test'], suppressed, allowed))

print('')
print('--- SiteConfiguration notification recipient fields (admin GUI = authoritative) ---')
for f in ('deposit_initiated_notification_emails', 'deposit_successful_notification_emails',
          'deposit_failed_notification_emails', 'withdrawal_initiated_notification_emails',
          'withdrawal_approved_notification_emails', 'withdrawal_rejected_notification_emails',
          'stuck_deposit_alert_notification_emails'):
    print('%-46s = %r' % (f, getattr(c, f, '<FIELD MISSING FROM MODELS.PY>')))

print('')
print('--- WHO ACTUALLY RECEIVES the admin mail (resolved by betting/tasks.py) ---')
from betting import tasks as t
from django.conf import settings as _s
print('settings.WITHDRAWAL_ADMIN_EMAILS         = %s' % getattr(_s, 'WITHDRAWAL_ADMIN_EMAILS', None))
print('settings.ADMIN_NOTIFICATION_EMAIL        = %s' % getattr(_s, 'ADMIN_NOTIFICATION_EMAIL', None))
for ev in ('requested', 'approved', 'completed', 'rejected'):
    print('withdrawal[%-9s] -> %s' % (ev, t._withdrawal_admin_recipients_for_event(ev)))
for ev in ('initiated', 'successful', 'failed', 'stuck_alert'):
    print('deposit   [%-11s] -> %s' % (ev, t._deposit_admin_recipients_for_event(ev)))
print('legacy fallback (no GUI override)        = %s' % t._default_withdrawal_admin_recipients())
'''

# ---------------- GATE B/C: env resolution + mtimes ----------------
ENVDUMP = '''\
#!/bin/sh
KEYSPEC='^(WITHDRAWAL_ADMIN_EMAILS|ADMIN_NOTIFICATION_EMAIL|DEPOSIT_ADMIN_NOTIFICATION_EMAIL|DEFAULT_FROM_EMAIL|EMAIL_HOST|EMAIL_PORT|EMAIL_USE_TLS|EMAIL_USE_SSL|DEBUG)='
for u in %s; do
  echo "--- $u  (started: $(systemctl show -p ActiveEnterTimestamp --value "$u"))"
  MP=$(systemctl show -p MainPID --value "$u")
  if [ -n "$MP" ] && [ -r "/proc/$MP/environ" ]; then
    tr '\\0' '\\n' < "/proc/$MP/environ" | grep -E "$KEYSPEC" | sort \\
      || echo "    (none of those keys are in the process env)"
  else
    echo "    MainPID=$MP (environ unreadable)"
  fi
done
''' % ' '.join(UNITS)


def run(ssh, cmd, label=None, timeout=180):
    if label:
        print('\n$ %s' % label)
    stdin, stdout, stderr = ssh.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode('utf-8', 'replace')
    err = stderr.read().decode('utf-8', 'replace')
    rc = stdout.channel.recv_exit_status()
    if out.strip():
        print(out.rstrip())
    if err.strip():
        print('  [stderr] ' + err.rstrip().replace('\n', '\n  [stderr] '))
    return rc, out, err


def sudo(ssh, pw, inner, label):
    print('\n$ sudo %s' % label)
    stdin, stdout, stderr = ssh.exec_command('sudo -S -p "" %s' % inner, timeout=300)
    stdin.write(pw + '\n')
    stdin.flush()
    out = stdout.read().decode('utf-8', 'replace')
    err = stderr.read().decode('utf-8', 'replace')
    rc = stdout.channel.recv_exit_status()
    if out.strip():
        print(out.rstrip())
    if err.strip():
        print('  [sudo stderr] ' + err.rstrip().replace('\n', '\n  [sudo stderr] '))
    return rc


def main():
    pw = getpass.getpass('%s@%s password: ' % (USER, HOST))
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(HOST, username=USER, password=pw, timeout=30, banner_timeout=30)
    print('SSH connected to %s' % HOST)

    sftp = ssh.open_sftp()
    for path, body in (('/tmp/pb_toggles.py', TOGGLES), ('/tmp/pb_envdump2.sh', ENVDUMP)):
        with sftp.open(path, 'w') as fh:
            fh.write(body)
    sftp.close()

    print('\n############ GATE A: email toggle truth table ############')
    run(ssh, 'cd %s && PYTHONPATH=%s ./env/bin/python /tmp/pb_toggles.py 2>&1 '
             '| grep -v "^\\[V12" | grep -v "^INFO RUNTIME" | grep -v RuntimeWarning '
             '| grep -v "warnings.warn"' % (REMOTE_ROOT, REMOTE_ROOT))

    print('\n############ GATE B: unit definition — where env values come from ############')
    run(ssh, "for u in %s; do echo \"=== $u\"; systemctl cat $u 2>/dev/null "
             "| grep -nE '^(# |Environment|EnvironmentFile|ExecStart|User|Group)' "
             "| sed -E 's/(PASSWORD|SECRET)[^ :=]*=[^ ]*/\\1=<hidden>/g'; done" % ' '.join(UNITS))
    run(ssh, "for u in %s; do echo \"=== $u\"; systemctl cat $u 2>/dev/null "
             "| sed -E 's/(PASSWORD|SECRET|API_KEY)[A-Z_]*=[^ ]*/<redacted>/g'; done" % ' '.join(UNITS),
        'FULL unit files + drop-ins (secrets redacted)')
    run(ssh, "for d in /etc/systemd/system/shop-celery.service.d "
             "/etc/systemd/system/shop-celery-beat.service.d "
             "/etc/systemd/system/shop-sync.service.d "
             "/etc/systemd/system/shop-ws.service.d; do "
             "echo \"=== $d\"; ls -l \"$d\" 2>/dev/null || echo '(no such drop-in dir)'; done",
        'drop-in files on disk')

    print('\n############ GATE B2: the address keys inside the env FILES ############')
    run(ssh, 'grep -nE "^(%s)=" %s/.env' % ('|'.join(ADDR_KEYS), REMOTE_ROOT),
        '/var/www/shop/.env')
    sudo(ssh, pw, 'grep -nE "^(%s)=" /etc/shop-sync.env'
                  ' 2>/dev/null || echo "(no /etc/shop-sync.env or unreadable)"' % '|'.join(ADDR_KEYS),
         '/etc/shop-sync.env')

    print('\n############ GATE B3: what the RESTARTED processes really hold ############')
    sudo(ssh, pw, 'bash /tmp/pb_envdump2.sh', 'bash /tmp/pb_envdump2.sh')

    print('\n############ GATE C: .env mtime vs process start ############')
    run(ssh, 'for f in %s/.env /etc/shop-sync.env; do '
             '[ -f "$f" ] && echo "$f  mtime $(stat -c %%y $f)"; done; '
             'echo "-- files NEWER than shop-sync start (BAD = edit happened after restart):"; '
             'T=$(systemctl show -p ActiveEnterTimestamp --value shop-sync); '
             'echo "   shop-sync started: $T"; '
             'find %s/.env /etc/shop-sync.env -newermt "$T" -print 2>/dev/null; '
             'echo "-- (empty = your .env edit was already in place when the services restarted)"'
             % (REMOTE_ROOT, REMOTE_ROOT))

    ssh.close()
    print('\nDONE.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
