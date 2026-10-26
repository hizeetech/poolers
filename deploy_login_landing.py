"""Deploy the login landing rule to live.

    python deploy_login_landing.py                 DRY RUN (writes nothing)
    python deploy_login_landing.py --apply         swap file + restart shop-sync
    python deploy_login_landing.py --apply --no-restart

THE RULE BEING DEPLOYED
    ONLY a cashier lands on the fixtures page.
    Every other role lands on its dashboard.

THE GATE
    The script refuses to write unless the live file differs from the local
    file by EXACTLY the intended hunk:
      1. the live file must still contain the old rule, and must not already
         contain the new one;
      2. the old rule is swapped for the new one IN MEMORY, from the live text;
      3. the result must equal the local file, after normalising line endings
         (the local checkout is CRLF, live is LF).
    If step 3 fails, nothing is written and a bounded diff is printed, so an
    unrelated live edit can never be silently overwritten.

WHY A RESTART IS REQUIRED HERE (unlike the static JS deploy)
    gunicorn sync workers import betting/views.py once at boot. The new
    redirect is only picked up after shop-sync restarts.
"""
import difflib
import getpass
import hashlib
import os
import sys
import time

try:
    import paramiko
except ImportError:
    print('paramiko missing.  Run:  pip install paramiko')
    sys.exit(1)

HOST = '172.236.9.63'
USER = 'stakenaija'
LOCAL = r'c:\Users\HP\Desktop\poolbetting-main\betting\views.py'
REMOTE = '/var/www/shop/betting/views.py'
BACKUP_DIR = '/home/stakenaija/code_backup_%s'
SERVICE = 'shop-sync'

OLD_BLOCK = (
    "                if user.user_type in ['player', 'cashier', ''] or not getattr(user, 'user_type', ''):\n"
    "                    return redirect('betting:fixtures')\n"
    "                return redirect('betting:user_dashboard')\n"
)

NEW_BLOCK = (
    "                # Landing rule: ONLY cashiers go to the fixtures page. Every other\n"
    "                # role lands on its dashboard. betting:user_dashboard resolves the\n"
    "                # per-role dashboard and renders the player dashboard for players.\n"
    "                if user.user_type == 'cashier':\n"
    "                    return redirect('betting:fixtures')\n"
    "                return redirect('betting:user_dashboard')\n"
)

MAX_DIFF_LINES = 40


def sha(b):
    return hashlib.sha256(b).hexdigest()


def norm(text):
    """Line endings out of the way: compare and build in LF only."""
    return text.replace('\r\n', '\n').replace('\r', '\n')


def read_remote(sftp, path):
    with sftp.open(path, 'rb') as fh:
        data = fh.read()
    return data, sftp.stat(path)


def print_diff(live_norm, want_norm):
    diff = list(difflib.unified_diff(
        live_norm.splitlines(), want_norm.splitlines(),
        fromfile='LIVE + intended hunk', tofile='LOCAL', lineterm='', n=2))
    print('')
    print('  --- diff (first %d lines) ---' % MAX_DIFF_LINES)
    for line in diff[:MAX_DIFF_LINES]:
        print('  ' + line)
    if len(diff) > MAX_DIFF_LINES:
        print('  ... (%d more diff lines)' % (len(diff) - MAX_DIFF_LINES))


def restart(ssh):
    print('')
    print('=== restarting %s ===' % SERVICE)
    sudo_pw = getpass.getpass('sudo password for %s@%s: ' % (USER, HOST))
    cmd = (
        'sudo -S -p "" systemctl restart {svc}; '
        'echo "is-active: $(systemctl is-active {svc})"; '
        'main=$(systemctl show {svc} -p MainPID --value); '
        'echo "MainPID: $main"; '
        'if [ -n "$main" ] && [ "$main" != "0" ]; then '
        '  echo "workers: $(pgrep -P $main | wc -l)"; fi'
    ).format(svc=SERVICE)
    stdin, stdout, _ = ssh.exec_command(cmd, timeout=180)
    stdin.write(sudo_pw + '\n')
    stdin.flush()
    print(stdout.read().decode('utf-8', 'replace').rstrip())


def main():
    args = sys.argv[1:]
    apply_mode = '--apply' in args
    do_restart = '--no-restart' not in args
    print('MODE = %s' % ('APPLY' if apply_mode else 'DRY RUN (nothing written)'))

    if not os.path.isfile(LOCAL):
        print('ABORT: local file not found: %s' % LOCAL)
        return 2
    lb = open(LOCAL, 'rb').read()
    local_norm = norm(lb.decode('utf-8'))

    print('')
    print('=== local source of truth ===')
    print('  %s' % LOCAL)
    print('  %d bytes (CRLF=%d)  sha256=%s' % (len(lb), lb.count(b'\r\n'), sha(lb)[:16]))
    local_has_new = NEW_BLOCK in local_norm
    local_has_old = OLD_BLOCK in local_norm
    print('  old rule present : %s   (expected False)' % local_has_old)
    print('  new rule present : %s   (expected True)' % local_has_new)
    if not local_has_new:
        print('')
        print('ABORT: the local file does not contain the new rule - nothing to deploy.')
        return 2

    ssh_pw = getpass.getpass('%s@%s password: ' % (USER, HOST))
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(HOST, username=USER, password=ssh_pw, timeout=30, banner_timeout=30)
    print('SSH connected to %s' % HOST)
    sftp = ssh.open_sftp()

    try:
        rb, st = read_remote(sftp, REMOTE)
    except IOError as exc:
        print('ABORT: cannot read %s (%s)' % (REMOTE, exc))
        sftp.close()
        ssh.close()
        return 2

    mode = st.st_mode & 0o777
    remote_norm = norm(rb.decode('utf-8'))
    use_crlf = b'\r\n' in rb

    print('')
    print('=== live file ===')
    print('  %s' % REMOTE)
    print('  %d bytes (CRLF=%d)  mode=%o  mtime=%s' % (
        len(rb), rb.count(b'\r\n'), mode,
        time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime(st.st_mtime))))
    print('  sha256=%s' % sha(rb)[:32])

    live_has_old = OLD_BLOCK in remote_norm
    live_has_new = NEW_BLOCK in remote_norm
    print('  old rule present : %s   (must be True to deploy)' % live_has_old)
    print('  new rule present : %s   (must be False to deploy)' % live_has_new)

    if live_has_new:
        print('')
        print('Nothing to do - live already has the new rule.')
        sftp.close()
        ssh.close()
        return 0
    if not live_has_old:
        print('')
        print('ABORT: live does not contain the expected old rule. It may have been')
        print('edited since this script was written. Nothing was written.')
        sftp.close()
        ssh.close()
        return 2

    candidate_norm = remote_norm.replace(OLD_BLOCK, NEW_BLOCK, 1)
    if candidate_norm != local_norm:
        print('')
        print('ABORT: applying the intended hunk to live does NOT reproduce the local')
        print('file. That means live and local differ elsewhere too. Nothing was written.')
        print_diff(candidate_norm, local_norm)
        sftp.close()
        ssh.close()
        return 2

    print('')
    print('=== gate passed ===')
    print('  live + intended hunk == local file (line endings normalised).')
    print('  the only change is the 3-line landing rule.')
    print('')
    print('=== plan ===')
    print('  BACKUP  -> %s/' % (BACKUP_DIR % '<ts>'))
    print('  WRITE   -> %s (%d -> %d bytes)' % (
        REMOTE, len(rb), len(candidate_norm.encode('utf-8'))))
    if do_restart:
        print('  RESTART -> systemctl restart %s   (REQUIRED: workers cache views.py)' % SERVICE)
    else:
        print('  RESTART -> skipped (--no-restart): the change will NOT take effect yet')

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
    bpath = '%s/views.py.%s' % (bdir, sha(rb)[:8])
    with sftp.open(bpath, 'wb') as fh:
        fh.write(rb)
    print('  BACKUP  %s  (%d bytes)' % (bpath, len(rb)))

    payload = candidate_norm.replace('\n', '\r\n').encode('utf-8') if use_crlf else candidate_norm.encode('utf-8')
    tmp = REMOTE + '.pbnew'
    with sftp.open(tmp, 'wb') as fh:
        fh.write(payload)
    sftp.chmod(tmp, mode)
    sftp.posix_rename(tmp, REMOTE)
    print('  WRITTEN %s' % REMOTE)

    print('')
    print('=== verify file ===')
    rb2, st2 = read_remote(sftp, REMOTE)
    ok = norm(rb2.decode('utf-8')) == candidate_norm
    print('  %d bytes  mode=%o' % (len(rb2), st2.st_mode & 0o777))
    print('  sha256=%s' % sha(rb2)[:32])
    print('  new rule present : %s' % (NEW_BLOCK in norm(rb2.decode('utf-8'))))
    print('  CONTENT MATCHES INTENDED: %s' % ok)
    if not ok:
        print('')
        print('*** VERIFICATION FAILED - restore with:')
        print('      cp %s %s' % (bpath, REMOTE))
        sftp.close()
        ssh.close()
        return 1

    if do_restart:
        restart(ssh)
    else:
        print('')
        print('NOTE: no restart was performed, so the new rule is not live yet.')

    sftp.close()
    ssh.close()
    print('')
    print('RESULT: %s' % ('DEPLOYED AND RESTARTED' if do_restart else 'FILE SWAPPED (not live until restarted)'))
    if not do_restart:
        print('Run with --apply (without --no-restart) to restart %s.' % SERVICE)
    print('Backup: %s' % bdir)
    print('')
    print('Confirm with: python verify_login_landing.py  (local matrix) and a browser')
    print('login as a cashier (fixtures) and as any other role (its dashboard).')
    return 0


if __name__ == '__main__':
    sys.exit(main())
