"""READ-ONLY probe of the systemd / .env email leftovers.

    python probe_celery_env.py

Dumps, and nothing else:
  1. every drop-in directory under /etc/systemd/system/*.service.d/
  2. `systemctl cat` for the 4 units (shows inline Environment= lines)
  3. `systemctl show -p Environment -p EnvironmentFiles` for the 4 units
  4. the raw contents of every .conf drop-in
  5. the WITHDRAWAL_ADMIN_EMAILS line in /var/www/shop/.env

No file is written, no unit is restarted. Secrets are masked in the output.
"""
import sys

try:
    import paramiko
except ImportError:
    print('paramiko missing.  Run:  pip install paramiko')
    sys.exit(1)
import getpass

HOST = '172.236.9.63'
USER = 'stakenaija'
UNITS = ['shop-sync', 'shop-ws', 'shop-celery', 'shop-celery-beat']

MASK = r"sed -E 's/(PASSWORD|SECRET|TOKEN|KEY)=[^ ]*/\1=<masked>/g'"

CMDS = [
    ('1. drop-in directories present',
     'ls -d /etc/systemd/system/*.service.d/ 2>/dev/null; '
     'echo "---"; ls -la /etc/systemd/system/*.service.d/ 2>/dev/null'),
    ('2. systemctl cat (inline Environment= included)',
     'systemctl cat %s 2>&1 | %s' % (' '.join(UNITS), MASK)),
    ('3. resolved Environment / EnvironmentFiles',
     'systemctl show %s -p Environment -p EnvironmentFiles 2>&1 | %s'
     % (' '.join(UNITS), MASK)),
    ('4. raw drop-in contents',
     'for f in /etc/systemd/system/*.service.d/*.conf; do '
     'echo "=== $f ==="; cat "$f"; echo; done 2>/dev/null | %s' % MASK),
    ('5. .env WITHDRAWAL_ADMIN_EMAILS',
     "grep -n 'WITHDRAWAL_ADMIN_EMAILS' /var/www/shop/.env; "
     "echo '--- occurrences:'; grep -c 'WITHDRAWAL_ADMIN_EMAILS' /var/www/shop/.env"),
]


def main():
    print('MODE = READ-ONLY (no writes, no restarts)')
    ssh_pw = getpass.getpass('%s@%s password: ' % (USER, HOST))
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(HOST, username=USER, password=ssh_pw, timeout=30, banner_timeout=30)
    print('SSH connected to %s' % HOST)

    for title, cmd in CMDS:
        print('')
        print('=== %s ===' % title)
        _in, out, err = ssh.exec_command(cmd, timeout=60)
        text = out.read().decode('utf-8', 'replace')
        errtext = err.read().decode('utf-8', 'replace')
        print(text.rstrip())
        if errtext.strip():
            print('  [stderr] %s' % errtext.strip())

    ssh.close()
    print('')
    print('Nothing was written. Paste this whole output back.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
