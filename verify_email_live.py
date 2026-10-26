"""Re-run the two probes that failed in deploy_cashier_compact.py.

READ-ONLY.  No restart, no migrate, no writes to the app tree (only /tmp helper files).

  GATE 1  live ORM proof of the email governance (the PYTHONPATH bug is fixed)
  GATE 2  HOW the app gets its environment  (EnvironmentFile vs .env at import)
  GATE 3  the REAL environment of the restarted master processes (safe keys only)
  GATE 4  effective Django settings from a fresh interpreter
  GATE 5  templates are OLDER than the live process  (= cached loader is fresh)
          + HTTP probe straight at the shop-sync unix socket

Usage:  python verify_email_live.py
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
UNITS = ['shop-sync', 'shop-ws', 'stakenaija_backend', 'shop-celery', 'shop-celery-beat']
SOCK = '/var/www/shop/run/shop.sock'

# ---------- remote helper: ORM proof (sys.path inserted INSIDE the file) ----------
PROOF = '''\
import os, sys
sys.path.insert(0, '/var/www/shop')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'poolbetting.settings')
import django
django.setup()
from django.conf import settings
from django.db import connection
from betting.models import SiteConfiguration
from betting import email_prefs as p

c = SiteConfiguration.load()
on = sum(1 for n in p.ALL_TOGGLE_FIELDS if getattr(c, n, None))
print('toggles_total            = %d' % len(p.ALL_TOGGLE_FIELDS))
print('toggles_currently_on     = %d' % on)
print('master_enabled           = %s' % p.master_enabled())
print('suppression_list         = %s' % sorted(p.suppression_list()))
print('recipient_allowlist      = %s' % p.allowlist())
cur = connection.cursor()
cur.execute("SELECT count(*) FROM information_schema.columns "
            "WHERE table_name='betting_siteconfiguration' AND column_name LIKE 'email_%'")
print('email_columns_in_db      = %s' % cur.fetchone()[0])
print('filter_recipients_proof  = %s' % p.filter_recipients(['good@example.com', 'not-an-email', 'x@dead.test']))
print('--- effective settings seen by a freshly started process ---')
for k in ('EMAIL_BACKEND', 'EMAIL_HOST', 'EMAIL_PORT', 'EMAIL_USE_SSL', 'EMAIL_USE_TLS',
          'DEFAULT_FROM_EMAIL', 'ADMIN_NOTIFICATION_EMAIL'):
    print('%-24s = %s' % (k, getattr(settings, k, '<unset>')))
'''

# ---------- remote helper: dump the REAL env of each master process ----------
ENVDUMP = '''\
#!/bin/sh
for u in %s; do
  echo "--- $u"
  MP=$(systemctl show -p MainPID --value "$u")
  echo "    MainPID=$MP"
  if [ -n "$MP" ] && [ -r "/proc/$MP/environ" ]; then
    tr '\\0' '\\n' < "/proc/$MP/environ" \\
      | grep -E '^(EMAIL_HOST|EMAIL_PORT|EMAIL_USE_TLS|EMAIL_USE_SSL|DEFAULT_FROM_EMAIL|ADMIN_NOTIFICATION_EMAIL|DEBUG)=' \\
      || echo "    (no EMAIL_* in this process env -> settings.py load_dotenv reads them from .env at import)"
  else
    echo "    (process env unreadable)"
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
    for path, body in (('/tmp/pb_email_proof2.py', PROOF), ('/tmp/pb_env_dump.sh', ENVDUMP)):
        with sftp.open(path, 'w') as fh:
            fh.write(body)
    sftp.close()

    print('\n############ GATE 1: live ORM proof (email governance) ############')
    run(ssh, 'cd %s && PYTHONPATH=%s ./env/bin/python /tmp/pb_email_proof2.py 2>&1 '
             '| grep -v RuntimeWarning | grep -v "warnings.warn"' % (REMOTE_ROOT, REMOTE_ROOT))

    print('\n############ GATE 2: how the units get their environment ############')
    run(ssh, "for u in %s; do echo \"=== $u\"; systemctl cat $u 2>/dev/null "
             "| grep -E '^(Environment|EnvironmentFile|ExecStart|User|Group)=' "
             "| sed 's/EMAIL_HOST_PASSWORD=[^ ]*/EMAIL_HOST_PASSWORD=<hidden>/g' ; done" % ' '.join(UNITS))

    print('\n############ GATE 3: REAL env of the restarted masters (safe keys only) ############')
    sudo(ssh, pw, 'bash /tmp/pb_env_dump.sh', 'bash /tmp/pb_env_dump.sh')

    print('\n############ GATE 5: templates older than the live process + socket health ############')
    run(ssh, "T=$(systemctl show -p ActiveEnterTimestamp --value shop-sync); "
             "echo \"shop-sync started: $T\"; "
             "echo \"base.html     mtime: $(stat -c %%y %s/betting/templates/betting/base.html)\"; "
             "echo \"fixtures.html mtime: $(stat -c %%y %s/betting/templates/betting/fixtures.html)\"; "
             "echo '-- files NEWER than the process start (any line here = BAD):'; "
             "find %s/betting/templates/betting/base.html %s/betting/templates/betting/fixtures.html "
             "-newermt \"$T\" -print; "
             "echo '-- (empty above = cached loader already holds the new bytes)'"
             % (REMOTE_ROOT, REMOTE_ROOT, REMOTE_ROOT, REMOTE_ROOT))
    run(ssh, "curl -s -o /dev/null -w 'shop.sock /fixtures/ HTTP %%{http_code}\\n' "
             "--unix-socket %s -H 'Host: shop.stakenaija.ng' http://127.0.0.1/fixtures/" % SOCK)
    run(ssh, "curl -s -o /dev/null -w 'shop.sock / HTTP %%{http_code}\\n' "
             "--unix-socket %s -H 'Host: shop.stakenaija.ng' http://127.0.0.1/" % SOCK)

    ssh.close()
    print('\nDONE.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
