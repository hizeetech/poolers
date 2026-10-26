"""Deploy the notifications_nav.js refreshCount guard to live.

    python deploy_notifications_nav.py            DRY RUN: compare, write nothing
    python deploy_notifications_nav.py --apply    upload + verify (no restart)

WHY ONLY THIS FILE: the drift report proved base.html's wallet poller and
notifications_nav.js's scheduleReconnect backoff are ALREADY live. The only
real delta is the 414-byte refreshCount guard:
    refreshCountInFlight      - no overlapping in-flight requests
    visibilityState === hidden - no polling from a background tab
    AbortController(8000)      - a hung request cannot pile up

Both live paths must be updated or the fix never reaches users:
  - .../notifications/static/notifications/...  (app source; keeps them in sync)
  - .../staticfiles/notifications/...           (what Nginx actually serves)

No restart: there is no STATICFILES_STORAGE hashing (so the URL is unchanged)
and no open_file_cache (so Nginx reads from disk per request).

Backups are written to /home/stakenaija/js_backup_<ts>/ - deliberately OUTSIDE
the static root, so they are not publicly downloadable.
"""
import os, sys, time, getpass, hashlib

try:
    import paramiko
except ImportError:
    print('paramiko missing.  Run:  pip install paramiko')
    sys.exit(1)

HOST = '172.236.9.63'
USER = 'stakenaija'
LOCAL = r'c:\Users\HP\Desktop\poolbetting-main\notifications\static\notifications\notifications_nav.js'
REMOTE_PATHS = [
    '/var/www/shop/notifications/static/notifications/notifications_nav.js',
    '/var/www/shop/staticfiles/notifications/notifications_nav.js',
]
# Leftovers from a previous guard build. Both sit INSIDE a public static root,
# so they are downloadable at /static/notifications/notifications_nav.js.bak.WSBACKOFF
# (7821 bytes each - an older copy of public JS, not a secret). Backed up, then removed.
STALE_PATHS = [
    '/var/www/shop/notifications/static/notifications/notifications_nav.js.bak.WSBACKOFF',
    '/var/www/shop/staticfiles/notifications/notifications_nav.js.bak.WSBACKOFF',
]
BACKUP_DIR = '/home/stakenaija/js_backup_%s'

# If these are absent the local file is not the guarded build - refuse to ship it.
MARKERS = [
    'refreshCountInFlight',
    'document.visibilityState === "hidden"',
    'AbortController',
    'scheduleReconnect',
]


def sha(b):
    return hashlib.sha256(b).hexdigest()


def read_remote(sftp, path):
    with sftp.open(path, 'rb') as fh:
        data = fh.read()
    st = sftp.stat(path)
    return data, st


def main():
    apply_mode = '--apply' in sys.argv[1:]
    print('MODE = %s' % ('APPLY' if apply_mode else 'DRY RUN (nothing written)'))

    if not os.path.isfile(LOCAL):
        print('ABORT: local file not found: %s' % LOCAL)
        return 2
    lb = open(LOCAL, 'rb').read()
    lsha = sha(lb)
    ltxt = lb.decode('utf-8')

    print('')
    print('=== local source of truth ===')
    print('  %s' % LOCAL)
    print('  %d bytes  sha256=%s' % (len(lb), lsha[:16]))
    missing = [m for m in MARKERS if m not in ltxt]
    for m in MARKERS:
        print('  %-38s %s' % (m, 'PRESENT' if m in ltxt else '*** MISSING ***'))
    if missing:
        print('')
        print('ABORT: the local file is missing %s - refusing to deploy an unguarded build.' % missing)
        return 2

    ssh_pw = getpass.getpass('%s@%s password: ' % (USER, HOST))
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(HOST, username=USER, password=ssh_pw, timeout=30, banner_timeout=30)
    print('SSH connected to %s' % HOST)
    sftp = ssh.open_sftp()

    plan = []
    print('')
    print('=== live state ===')
    for rp in REMOTE_PATHS:
        try:
            rb, st = read_remote(sftp, rp)
        except IOError as exc:
            print('  %s' % rp)
            print('      MISSING (%s)' % exc)
            plan.append((rp, None, None, 'missing'))
            continue
        same = sha(rb) == lsha
        guard = 'refreshCountInFlight' in rb.decode('utf-8', 'replace')
        print('  %s' % rp)
        print('      %d bytes  mode=%o  sha256=%s  mtime=%s' % (
            len(rb), st.st_mode & 0o777, sha(rb)[:16],
            time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime(st.st_mtime))))
        print('      guard present: %s   ALREADY UP TO DATE: %s' % (guard, same))
        plan.append((rp, st.st_mode & 0o777, rb, 'same' if same else 'update'))

    todo = [p for p in plan if p[3] != 'same']

    stale = []
    print('')
    print('=== stale public leftovers ===')
    for sp in STALE_PATHS:
        try:
            sb, sst = read_remote(sftp, sp)
        except IOError:
            print('  ABSENT   %s' % sp)
            continue
        print('  PRESENT  %s  (%d bytes, web-reachable)' % (sp, len(sb)))
        stale.append((sp, sb))

    print('')
    if not todo and not stale:
        print('Nothing to do - every live copy already matches the local file,')
        print('and no stale public leftovers remain.')
        sftp.close()
        ssh.close()
        return 0

    print('=== plan ===')
    for rp, mode, _rb, action in plan:
        print('  %-6s %s' % (action.upper(), rp))
    for sp, _sb in stale:
        print('  %-6s %s' % ('DELETE', sp))
    print('')
    print('  backups -> outside the static root, so they are NOT web-reachable')
    print('  restart -> none required (no hashed filenames, no open_file_cache)')

    if not apply_mode:
        print('')
        print('DRY RUN - nothing was written. Re-run with --apply to commit.')
        sftp.close()
        ssh.close()
        return 0

    ts = time.strftime('%Y%m%d_%H%M%S')
    bdir = BACKUP_DIR % ts
    ssh.exec_command('mkdir -p %s' % bdir)
    time.sleep(1)

    print('')
    print('=== APPLY ===')
    ok = True
    for rp, mode, rb, action in plan:
        if action == 'same':
            print('  SKIP    %s  (already identical)' % rp)
            continue
        name = os.path.basename(rp)
        if rb is not None:
            bpath = '%s/%s.%s' % (bdir, name, sha(rb)[:8])
            with sftp.open(bpath, 'wb') as fh:
                fh.write(rb)
            print('  BACKUP  %s  (%d bytes)' % (bpath, len(rb)))
        tmp = rp + '.pbnew'
        with sftp.open(tmp, 'wb') as fh:
            fh.write(lb)
        if mode is not None:
            sftp.chmod(tmp, mode)
        sftp.posix_rename(tmp, rp)
        print('  WRITTEN %s' % rp)

    print('')
    print('=== verify ===')
    for rp, _mode, _rb, action in plan:
        rb2, st2 = read_remote(sftp, rp)
        s2 = sha(rb2)
        match = s2 == lsha
        guard = 'refreshCountInFlight' in rb2.decode('utf-8', 'replace')
        ok = ok and match and guard
        print('  %s' % rp)
        print('      %d bytes  mode=%o  sha256=%s  MATCH=%s  guard=%s' % (
            len(rb2), st2.st_mode & 0o777, s2[:16], match, guard))

    if stale:
        print('')
        print('=== remove stale public leftovers ===')
        for sp, sb in stale:
            bpath = '%s/%s.%s' % (bdir, os.path.basename(sp), sha(sb)[:8])
            with sftp.open(bpath, 'wb') as fh:
                fh.write(sb)
            print('  BACKUP  %s  (%d bytes)' % (bpath, len(sb)))
            sftp.remove(sp)
            try:
                read_remote(sftp, sp)
                gone = False
            except IOError:
                gone = True
            ok = ok and gone
            print('  REMOVED %s  (gone from disk: %s)' % (sp, gone))

    sftp.close()
    ssh.close()
    print('')
    print('RESULT: %s' % ('ALL COPIES VERIFIED' if ok else '*** VERIFICATION FAILED ***'))
    if ok:
        print('')
        print('The guard is live. No restart was needed. Confirm in a browser with the')
        print('Network tab: /notifications/api/unread-count/ should fire every 15s while')
        print('the tab is visible, and not at all while it is hidden.')
        print('Backups: %s' % bdir)
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
