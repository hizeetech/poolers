"""READ-ONLY drift report: local working tree vs the live server.

    python verify_frontend_drift.py

For every file below it compares the LOCAL sha256 against the LIVE sha256 and
prints MATCH or DIFFER, plus a short unified diff when they differ. Nothing is
written, nothing is restarted.

Why this exists: the local source of a JS guard and the COLLECTED copy under
staticfiles/ can disagree, and Nginx serves the collected copy. Templates are
also wrapped in cached.Loader in production, so a template edit needs a restart.
"""
import os, sys, difflib

try:
    import paramiko
except ImportError:
    print('paramiko missing.  Run:  pip install paramiko')
    sys.exit(1)
import getpass, hashlib

HOST = '172.236.9.63'
USER = 'stakenaija'
LOCAL_ROOT = r'c:\Users\HP\Desktop\poolbetting-main'
REMOTE_ROOT = '/var/www/shop'

PAIRS = [
    # templates
    'betting/templates/betting/base.html',
    'betting/templates/betting/fixtures.html',
    'betting/templates/betting/retail_dashboard.html',
    'betting/templates/betting/wallet.html',
    'betting/templates/betting/agent_dashboard.html',
    'betting/templates/betting/finance_dashboard.html',
    'betting/templates/betting/admin/userwithdrawal_change_list.html',
    'betting/templates/betting/admin/betticket_change_list.html',
    'uip/templates/uip/dashboard.html',
    # js: source and the collected copy Nginx serves
    'notifications/static/notifications/notifications_nav.js',
    'notifications/static/notifications/notifications_center.js',
    'staticfiles/notifications/notifications_nav.js',
    'staticfiles/notifications/notifications_center.js',
]

MAX_DIFF_LINES = 60


def sha(b):
    return hashlib.sha256(b).hexdigest()


def main():
    print('MODE = READ-ONLY (sha256 compare + diff, nothing written)')
    ssh_pw = getpass.getpass('%s@%s password: ' % (USER, HOST))
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(HOST, username=USER, password=ssh_pw, timeout=30, banner_timeout=30)
    print('SSH connected to %s' % HOST)
    sftp = ssh.open_sftp()

    match = differ = missing_local = missing_remote = 0

    for rel in PAIRS:
        lp = os.path.join(LOCAL_ROOT, rel.replace('/', os.sep))
        rp = '%s/%s' % (REMOTE_ROOT, rel)

        if not os.path.isfile(lp):
            print('')
            print('?? %s' % rel)
            print('   LOCAL MISSING')
            missing_local += 1
            continue
        lb = open(lp, 'rb').read()

        try:
            with sftp.open(rp, 'rb') as fh:
                rb = fh.read()
        except IOError as exc:
            print('')
            print('?? %s' % rel)
            print('   REMOTE MISSING (%s)' % exc)
            missing_remote += 1
            continue

        print('')
        if sha(lb) == sha(rb):
            match += 1
            print('== %s  MATCH  (%d bytes)' % (rel, len(lb)))
            continue

        differ += 1
        print('!= %s  DIFFER' % rel)
        print('   local : %d bytes  sha256=%s' % (len(lb), sha(lb)[:16]))
        print('   remote: %d bytes  sha256=%s' % (len(rb), sha(rb)[:16]))

        try:
            lt = lb.decode('utf-8').splitlines()
            rt = rb.decode('utf-8').splitlines()
        except UnicodeDecodeError:
            print('   (binary - no diff shown)')
            continue

        d = list(difflib.unified_diff(rt, lt, fromfile='LIVE', tofile='LOCAL',
                                      lineterm='', n=2))
        print('   --- diff (LIVE -> LOCAL), %d lines ---' % len(d))
        for line in d[:MAX_DIFF_LINES]:
            print('   %s' % line)
        if len(d) > MAX_DIFF_LINES:
            print('   ... (%d more diff lines)' % (len(d) - MAX_DIFF_LINES))

    sftp.close()
    ssh.close()
    print('')
    print('=== summary ===')
    print('  MATCH          : %d' % match)
    print('  DIFFER         : %d' % differ)
    print('  local missing  : %d' % missing_local)
    print('  remote missing : %d' % missing_remote)
    return 0


if __name__ == '__main__':
    sys.exit(main())
