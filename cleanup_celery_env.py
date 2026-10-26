"""De-duplicate WITHDRAWAL_ADMIN_EMAILS in the Celery systemd drop-ins.

    python cleanup_celery_env.py --show              read-only: current state
    python cleanup_celery_env.py                     DRY RUN: the exact diff
    python cleanup_celery_env.py --apply             commit + daemon-reload
    python cleanup_celery_env.py --apply --restart   ...also restart both units

WHY: WITHDRAWAL_ADMIN_EMAILS is defined in four places and two of them hold a
stale address ('admin@shop.stakenaija.ng') that is not a real mailbox. Only
/var/www/shop/.env matters, so the two systemd copies are removed and the two
wdremail.conf drop-ins are deleted.

NOT TOUCHED: /var/www/shop/.env (that would need a 24-worker shop-sync restart
for a value that is a fallback only), and /etc/systemd/system/shop-gunicorn.service.d/
(not one of the 4 live units, and its z_override.conf binds the same socket as
shop-sync).

The value is a FALLBACK: betting/tasks.py prefers the SiteConfiguration GUI
fields, so this whole change is zero-behaviour. A Celery restart is only needed
for the running processes to stop carrying the stale copy.

Write order: back up the whole drop-in directory -> edit with owner/mode
preserved -> delete the duplicate -> daemon-reload -> verify.
"""
import os, sys, time, getpass, hashlib

try:
    import paramiko
except ImportError:
    print('paramiko missing.  Run:  pip install paramiko')
    sys.exit(1)

HOST = '172.236.9.63'
USER = 'stakenaija'
DIRS = ['/etc/systemd/system/shop-celery.service.d',
        '/etc/systemd/system/shop-celery-beat.service.d']
EDIT = 'dbpool_fix.conf'
DUPES = ['wdremail.conf']
DROP_PREFIX = 'Environment=WITHDRAWAL_ADMIN_EMAILS='
UNITS = ['shop-celery', 'shop-celery-beat']
KEEP_WORTH_CHECKING = ['Environment=DB_HOST=', 'Environment=DB_PORT=', 'Environment=LANG=']


def read_text(ssh, path):
    sftp = ssh.open_sftp()
    try:
        with sftp.open(path, 'r') as fh:
            data = fh.read().decode('utf-8')
        st = sftp.stat(path)
    finally:
        sftp.close()
    return data, st


def run(ssh, cmd, timeout=120):
    _in, out, err = ssh.exec_command(cmd, timeout=timeout)
    o = out.read().decode('utf-8', 'replace')
    e = err.read().decode('utf-8', 'replace')
    return o, e


def survey(ssh):
    """Read-only. Return a dict describing what is on disk right now."""
    sftp = ssh.open_sftp()
    found = {'dupes_present': [], 'dupes_absent': [], 'edits': [], 'missing': []}
    for d in DIRS:
        f = '%s/%s' % (d, EDIT)
        try:
            st = sftp.stat(f)
            with sftp.open(f, 'r') as fh:
                text = fh.read().decode('utf-8')
            hits = [ln for ln in text.split('\n') if ln.strip().startswith(DROP_PREFIX)]
            keeps = [ln for ln in text.split('\n')
                     if any(ln.strip().startswith(k) for k in KEEP_WORTH_CHECKING)]
            found['edits'].append({'path': f, 'mode': st.st_mode & 0o777,
                                   'size': st.st_size, 'text': text,
                                   'hits': hits, 'keeps': keeps})
        except IOError:
            found['missing'].append(f)
        for dup in DUPES:
            df = '%s/%s' % (d, dup)
            try:
                sftp.stat(df)
                found['dupes_present'].append(df)
            except IOError:
                found['dupes_absent'].append(df)
    sftp.close()
    return found


def show(ssh, found):
    print('')
    print('=== current drop-in state (read-only) ===')
    for rec in found['edits']:
        print('  %s  mode=%o  %d bytes' % (rec['path'], rec['mode'], rec['size']))
        print('      sha256=%s' % hashlib.sha256(rec['text'].encode('utf-8')).hexdigest()[:16])
        for k in rec['keeps']:
            print('      KEEP   %s' % k)
        for h in rec['hits']:
            print('      STALE  %s' % h)
        if not rec['hits']:
            print('      (no WITHDRAWAL_ADMIN_EMAILS line - already clean)')
    for f in found['missing']:
        print('  %s  MISSING' % f)
    for f in found['dupes_present']:
        print('  %s  PRESENT (will be deleted)' % f)
    for f in found['dupes_absent']:
        print('  %s  already absent' % f)
    o, _e = run(ssh, 'systemctl show %s -p Environment' % ' '.join(UNITS))
    print('')
    print('=== what the units resolve to right now ===')
    print(o.rstrip())
    o, _e = run(ssh, "grep -n 'WITHDRAWAL_ADMIN_EMAILS' /var/www/shop/.env")
    print('')
    print('=== the surviving definition, /var/www/shop/.env ===')
    print(o.rstrip() or '  (none)')


def dry_run(ssh, found):
    print('')
    print('=== DRY RUN - exact planned change (nothing written) ===')
    n_edit = n_del = 0
    for rec in found['edits']:
        print('  EDIT    %s' % rec['path'])
        for h in rec['hits']:
            print('            - %s' % h)
            n_edit += 1
        if not rec['hits']:
            print('            (nothing to remove)')
        for rhs in rec['keeps']:
            print('            = %s  (kept)' % rhs)
    for f in found['dupes_present']:
        print('  DELETE  %s' % f)
        n_del += 1
    for f in found['dupes_absent']:
        print('  SKIP    %s  (already absent)' % f)
    print('')
    print('  TOTALS  : %d stale line(s) removed, %d duplicate file(s) deleted' % (n_edit, n_del))
    print('  THEN    : systemctl daemon-reload  +  systemctl show -p Environment')
    print('  UNTOUCHED: /var/www/shop/.env , /etc/systemd/system/shop-gunicorn.service.d/')
    if n_edit == 0 and n_del == 0:
        print('')
        print('  NOTE    : nothing to do - the drop-ins are already clean.')
    print('')
    print('DRY RUN - nothing was written. Re-run with --apply to commit.')
    return 0


REMOTE_SCRIPT = r'''#!/bin/bash
set -u
TS="$1"; MODE="$2"
BK="/home/stakenaija/sd_backup_$TS"
DIRS="/etc/systemd/system/shop-celery.service.d /etc/systemd/system/shop-celery-beat.service.d"
echo "BACKUP_DIR=$BK"
mkdir -p "$BK"
for d in $DIRS; do
  n=$(basename "$d")
  mkdir -p "$BK/$n"
  cp -a "$d"/. "$BK/$n"/ 2>/dev/null
done
echo "--- backup complete ---"
for d in $DIRS; do
  f="$d/dbpool_fix.conf"
  if [ -f "$f" ]; then
    own=$(stat -c '%u:%g' "$f"); mod=$(stat -c '%a' "$f")
    before=$(wc -c < "$f")
    grep -v '^Environment=WITHDRAWAL_ADMIN_EMAILS=' "$f" > "$f.pbtmp"
    chown "$own" "$f.pbtmp"; chmod "$mod" "$f.pbtmp"
    mv "$f.pbtmp" "$f"
    after=$(wc -c < "$f")
    echo "EDITED  $f  ($before -> $after bytes, owner=$own mode=$mod)"
  else
    echo "MISSING $f"
  fi
  if [ -f "$d/wdremail.conf" ]; then
    rm -f "$d/wdremail.conf" && echo "DELETED $d/wdremail.conf"
  else
    echo "ABSENT  $d/wdremail.conf"
  fi
done
echo "--- running: systemctl daemon-reload ---"
systemctl daemon-reload && echo "daemon-reload OK"
echo "=== resolved Environment after reload ==="
systemctl show shop-celery shop-celery-beat -p Environment
echo "=== dbpool_fix.conf after the edit ==="
for d in $DIRS; do echo "--- $d/dbpool_fix.conf"; cat "$d/dbpool_fix.conf"; done
if [ "$MODE" = "restart" ]; then
  echo "--- restarting shop-celery shop-celery-beat ---"
  systemctl restart shop-celery shop-celery-beat
  sleep 3
  systemctl is-active shop-celery shop-celery-beat
fi
echo "--- done ---"
'''


def apply(ssh, do_restart):
    ts = time.strftime('%Y%m%d_%H%M%S')
    sftp = ssh.open_sftp()
    try:
        with sftp.open('/tmp/pb_cleanup_celery.sh', 'w') as fh:
            fh.write(REMOTE_SCRIPT)
    finally:
        sftp.close()

    print('')
    print('=== APPLY ===')
    sudo_pw = getpass.getpass('sudo password for %s@%s: ' % (USER, HOST))
    mode = 'restart' if do_restart else 'norestart'
    cmd = 'sudo -S -p "" bash /tmp/pb_cleanup_celery.sh %s %s' % (ts, mode)
    stdin, stdout, _stderr = ssh.exec_command(cmd, timeout=300)
    stdin.write(sudo_pw + '\n')
    stdin.flush()
    print(stdout.read().decode('utf-8', 'replace').rstrip())
    return True


def main():
    args = sys.argv[1:]
    do_show = '--show' in args
    do_apply = '--apply' in args
    do_restart = '--restart' in args

    print('MODE = %s' % ('APPLY' + (' + RESTART' if do_restart else '')
                         if do_apply else ('SHOW (read-only)' if do_show
                                           else 'DRY RUN (nothing written)')))
    ssh_pw = getpass.getpass('%s@%s password: ' % (USER, HOST))
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(HOST, username=USER, password=ssh_pw, timeout=30, banner_timeout=30)
    print('SSH connected to %s' % HOST)

    found = survey(ssh)
    show(ssh, found)

    if do_apply:
        apply(ssh, do_restart)
        if not do_restart:
            print('')
            print('The running Celery processes still carry the old copy. Restart them')
            print('with:  python cleanup_celery_env.py --apply --restart')
            print('(or just:  python patch_env_keys.py --restart-only  for the 4 units)')
        ssh.close()
        return 0

    if do_show:
        ssh.close()
        print('')
        print('Nothing was written.')
        return 0

    rc = dry_run(ssh, found)
    ssh.close()
    return rc


if __name__ == '__main__':
    sys.exit(main())
