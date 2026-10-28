"""Deploy the Paystack payout feature + admin fixes to live.

    python deploy_paystack_payout.py            DRY RUN (uploads nothing)
    python deploy_paystack_payout.py --apply    upload + verify SHA256

WHY THIS IS ZERO-IMPACT FOR USERS
    Uploading .py and template files does NOT affect the running site. Django
    (cached template loader) reads these from disk only on process start, and
    production runs without autoreload. shop-sync keeps serving the old code
    until it is restarted, so every user stays unaffected during this step.

    The deploy therefore has two separate halves:
      Step A (this script)  -> files land on disk        (zero user impact)
      Step B ([SSH LIVE])   -> migrate, then restart     (the impact window)

    Never restart before migrating: the new models.py declares columns that
    only exist once 0112 has been applied.

PAYOUT SAFETY
    PAYSTACK_PAYOUTS_ENABLED defaults to False in settings.py, so the payout
    action stays dormant after this deploy. No transfer can originate until
    someone deliberately sets the flag to True on live.

ORDERING
    New modules go up first; poolbetting/settings.py goes up last, so a partial
    upload can never leave the site pointing at a module that is not there.
"""
import getpass
import hashlib
import os
import subprocess
import sys
import time

DEPLOY_ROOT = r'c:\Users\HP\Desktop\poolbetting-main'
HOST = '172.236.9.63'
USER = 'stakenaija'
REMOTE_ROOT = '/var/www/shop'
SERVICE = 'shop-sync'
BACKUP_DIR = '/home/stakenaija/code_backup_%s'

# (local relative path, remote relative path) - remote root is /var/www/shop
NEW_FILES = [
    ('betting/services/paystack_payout.py',
     'betting/services/paystack_payout.py'),
    ('betting/migrations/0112_userwithdrawal_paystack_payout.py',
     'betting/migrations/0112_userwithdrawal_paystack_payout.py'),
    ('betting/templates/betting/admin/userwithdrawal_send_to_bank_confirm.html',
     'betting/templates/betting/admin/userwithdrawal_send_to_bank_confirm.html'),
    ('templates/admin/invalid_setup.html',
     'templates/admin/invalid_setup.html'),
]

CHANGED_FILES = [
    ('betting/models.py', 'betting/models.py'),
    ('betting/admin.py', 'betting/admin.py'),
    ('betting/templates/betting/admin/userwithdrawal_change_list.html',
     'betting/templates/betting/admin/userwithdrawal_change_list.html'),
    ('betting/templates/betting/admin/betticket_change_list.html',
     'betting/templates/betting/admin/betticket_change_list.html'),
]

# Uploaded last on purpose (see ORDERING above).
LAST_FILES = [
    ('poolbetting/settings.py', 'poolbetting/settings.py'),
]

ALL_FILES = NEW_FILES + CHANGED_FILES + LAST_FILES
NEW_REMOTE = {dst for _, dst in NEW_FILES}


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def remote_sha_map(ssh, rel_paths):
    """Return {relative_path: sha256} for the given paths on the server."""
    quoted = ' '.join("'%s'" % p for p in rel_paths)
    cmd = (
        "cd %s && for f in %s; do "
        "if [ -f \"$f\" ]; then sha256sum \"$f\"; else echo \"MISSING $f\"; fi; "
        "done" % (REMOTE_ROOT, quoted)
    )
    stdin, stdout, stderr = ssh.exec_command(cmd, timeout=120)
    out = stdout.read().decode('utf-8', errors='replace')
    err = stderr.read().decode('utf-8', errors='replace')
    if err.strip():
        print('  [stderr] %s' % err.strip())

    result = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == 'MISSING':
            result[parts[-1]] = None
        elif len(parts) >= 2:
            result[parts[-1]] = parts[0]
    return result


def main():
    args = sys.argv[1:]
    apply_mode = '--apply' in args

    try:
        import paramiko
    except ImportError:
        print('FATAL: paramiko not installed. Run: pip install paramiko')
        return 2

    print('=== Paystack payout deploy -> %s@%s:%s ===' % (USER, HOST, REMOTE_ROOT))
    print('    MODE = %s' % ('APPLY' if apply_mode else 'DRY RUN (nothing written)'))
    print('    %d files (%d new, %d changed)'
          % (len(ALL_FILES), len(NEW_FILES), len(CHANGED_FILES) + len(LAST_FILES)))

    missing = [src for src, _ in ALL_FILES
               if not os.path.isfile(os.path.join(DEPLOY_ROOT, src))]
    if missing:
        print('FATAL: missing local files:')
        for m in missing:
            print('   ', m)
        return 3

    print('\n--- [GATE 1] local syntax check ---')
    for src, _ in ALL_FILES:
        if src.endswith('.py'):
            r = subprocess.run(
                [sys.executable, '-m', 'py_compile', os.path.join(DEPLOY_ROOT, src)],
                capture_output=True, text=True, cwd=DEPLOY_ROOT)
            if r.returncode != 0:
                print('  FAIL %s\n%s' % (src, r.stderr))
                return 4
    print('  all .py files compile OK')

    local_sha = {dst: sha256_file(os.path.join(DEPLOY_ROOT, src))
                 for src, dst in ALL_FILES}

    pw = getpass.getpass('\nSSH password for %s@%s (prompted once): ' % (USER, HOST))
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        ssh.connect(HOST, username=USER, password=pw, timeout=30,
                    allow_agent=False, look_for_keys=False)
    except Exception as e:
        print('SSH connect FAIL: %s' % e)
        return 12
    print('Connected.')

    sftp = ssh.open_sftp()
    try:
        print('\n--- [GATE 2] live state ---')
        remote_sha = remote_sha_map(ssh, [dst for _, dst in ALL_FILES])

        to_upload = []
        for src, dst in ALL_FILES:
            rs = remote_sha.get(dst)
            ls = local_sha[dst]
            if rs is None:
                state = 'NEW' if dst in NEW_REMOTE else 'MISSING-ON-LIVE'
            elif rs == ls:
                state = 'UP-TO-DATE'
            else:
                state = 'CHANGED'
            print('  %-14s %s' % (state, dst))
            if state in ('NEW', 'CHANGED', 'MISSING-ON-LIVE'):
                to_upload.append((src, dst, state))

        print('')
        print('  files needing upload: %d of %d' % (len(to_upload), len(ALL_FILES)))

        if not apply_mode:
            print('')
            print('DRY RUN - nothing was written.')
            print('Confirm the list above, then re-run with --apply.')
            return 0

        if not to_upload:
            print('')
            print('Nothing to do - every live file already matches local.')
            return 0

        ts = time.strftime('%Y%m%d_%H%M%S')
        bdir = BACKUP_DIR % ts

        print('\n--- [GATE 3] backup live files ---')
        mk = ssh.exec_command('mkdir -p %s' % bdir)
        mk[1].channel.recv_exit_status()
        for _, dst, state in to_upload:
            if state == 'NEW':
                continue
            remote = '%s/%s' % (REMOTE_ROOT, dst)
            flat = dst.replace('/', '__')
            bk = '%s/%s.bak' % (bdir, flat)
            cmd = 'cp -p %s %s' % (remote, bk)
            stdin, stdout, stderr = ssh.exec_command(cmd, timeout=60)
            rc = stdout.channel.recv_exit_status()
            print('  %s %s' % ('BACKUP' if rc == 0 else 'SKIP  ', bk))

        print('\n--- [GATE 4] SFTP upload (binary, exact bytes) ---')
        for src, dst, state in to_upload:
            local = os.path.join(DEPLOY_ROOT, src)
            remote = '%s/%s' % (REMOTE_ROOT, dst)
            parent = os.path.dirname(remote)
            ssh.exec_command('mkdir -p %s' % parent)[1].channel.recv_exit_status()
            try:
                sftp.put(local, remote)
            except Exception as e:
                print('  FAIL upload %s: %s' % (dst, e))
                return 5
            size = sftp.stat(remote).st_size
            if os.path.getsize(local) != size:
                print('  FAIL size mismatch %s local=%d remote=%d'
                      % (dst, os.path.getsize(local), size))
                return 5
            print('  UPLOAD OK  %s (%d bytes)' % (dst, size))

        print('\n--- [GATE 5] remote SHA256 verify ---')
        verified = remote_sha_map(ssh, [dst for _, dst, _ in to_upload])
        bad = []
        for _, dst, _ in to_upload:
            if verified.get(dst) == local_sha[dst]:
                print('  OK       %s' % dst)
            else:
                bad.append(dst)
                print('  MISMATCH %s  local=%s remote=%s'
                      % (dst, local_sha[dst], verified.get(dst)))

        if bad:
            print('')
            print('FAILED: %d file(s) did not verify. Restore from %s' % (len(bad), bdir))
            return 6

        print('')
        print('SHA GATE PASS - all %d files uploaded and verified.' % len(to_upload))
        print('Backup: %s' % bdir)
        print('')
        print('=== STEP A COMPLETE - ZERO USER IMPACT ===')
        print('The live site is still running the OLD code. Nothing has changed for users.')
        print('')
        print('Next, in your live SSH session (Step B), using the project venv python:')
        print('  1. cd %s' % REMOTE_ROOT)
        print('  2. <venv>/bin/python manage.py migrate --plan   # expect ONLY betting.0112')
        print('  3. <venv>/bin/python manage.py migrate          # applies 0112 (additive)')
        print('  4. <venv>/bin/python manage.py check')
        print('  5. sudo systemctl restart %s                    # the impact window' % SERVICE)
        return 0
    finally:
        try:
            sftp.close()
        except Exception:
            pass
        try:
            ssh.close()
        except Exception:
            pass


if __name__ == '__main__':
    sys.exit(main())
