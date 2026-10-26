"""Gated editor for individual keys in the live /var/www/shop/.env.

    python patch_env_keys.py --show
    python patch_env_keys.py --set "DEFAULT_FROM_EMAIL=StakeNaija <x@y.com>"
    python patch_env_keys.py --set "SERVER_EMAIL=x@y.com" --apply --restart

DEFAULT IS A DRY RUN. It prints BEFORE/AFTER, validates the rewrite in memory
and shows exactly what would change - then stops. Nothing is written until you
add --apply.

Write order: verify in memory -> back up -> chmod to the original mode ->
atomic rename -> post-check. If the post-check fails the file HAS already been
replaced, so the message says so and prints the restore command.
"""
import os, sys, time, getpass, hashlib

try:
    import paramiko
except ImportError:
    print('paramiko missing.  Run:  pip install paramiko')
    sys.exit(1)

HOST = '172.236.9.63'
USER = 'stakenaija'
REMOTE_ENV = '/var/www/shop/.env'
REMOTE_ROOT = '/var/www/shop'
UNITS = ['shop-sync', 'shop-ws', 'shop-celery', 'shop-celery-beat']
SHOW_KEYS = ['EMAIL_HOST', 'EMAIL_PORT', 'EMAIL_USE_SSL', 'EMAIL_USE_TLS',
             'EMAIL_HOST_USER', 'EMAIL_HOST_PASSWORD',
             'DEFAULT_FROM_EMAIL', 'SERVER_EMAIL', 'WITHDRAWAL_ADMIN_EMAILS']
MASKED = {'EMAIL_HOST_PASSWORD'}


def describe(key, v):
    if v is None:
        return 'None (KEY ABSENT)'
    if v == '':
        return 'EMPTY STRING'
    if key in MASKED:
        return ('len=%d leading_space=%s trailing_space=%s dollar=%s hash=%s '
                'quote=%s backslash=%s') % (
            len(v), v[:1] == ' ', v[-1:] == ' ', '$' in v, '#' in v,
            ("'" in v) or ('"' in v), '\\' in v)
    return repr(v)


def quote_for_env(v):
    """Return the value exactly as it must appear after the '=' in .env."""
    if v.strip() != v or any(ch in v for ch in "#$"):
        if "'" not in v:
            return "'" + v + "'"
        if '"' not in v:
            return '"' + v + '"'
    return v


def _sftp_read(ssh, path):
    sftp = ssh.open_sftp()
    try:
        with sftp.open(path, 'r') as fh:
            data = fh.read().decode('utf-8')
        st = sftp.stat(path)
    finally:
        sftp.close()
    return data, st


def parse_env_text(text, keys):
    """Resolve keys from raw .env text the same way python-dotenv would."""
    import tempfile
    tmp = None
    try:
        from dotenv import dotenv_values
        fd, tmp = tempfile.mkstemp(suffix='.env')
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            fh.write(text)
        try:
            vals = dotenv_values(tmp, interpolate=False)
            method = 'python-dotenv, interpolate=False'
        except TypeError:
            vals = dotenv_values(tmp)
            method = 'python-dotenv, default interpolation'
        return {k: vals.get(k) for k in keys}, method
    except Exception as exc:
        out = dict.fromkeys(keys)
        for key in keys:
            for ln in text.splitlines():
                if ln.strip().startswith(key + '='):
                    v = ln.strip().split('=', 1)[1].strip()
                    if len(v) >= 2 and v[0] == v[-1] and v[0] in '\'"':
                        v = v[1:-1]
                    out[key] = v
        return out, 'manual parser (%s)' % exc
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except Exception:
                pass


def build_rewrite(text, sets):
    """Return (new_text, rows) or (None, error_message)."""
    lines = text.split('\n')
    rows = []
    for key, value in sets:
        hits = [i for i, ln in enumerate(lines) if ln.strip().startswith(key + '=')]
        if len(hits) != 1:
            return None, '%s is defined %d time(s) in the file - refusing to guess.' % (
                key, len(hits))
        idx = hits[0]
        before = lines[idx].split('=', 1)[1]
        rows.append((key, idx + 1, before, value))
        lines[idx] = key + '=' + quote_for_env(value)
    return '\n'.join(lines), rows


def show(ssh, keys):
    text, st = _sftp_read(ssh, REMOTE_ENV)
    vals, method = parse_env_text(text, keys)
    print('')
    print('=== %s  (read-only, parsed with %s) ===' % (REMOTE_ENV, method))
    print('  mtime=%s  mode=%o  size=%d bytes  sha256=%s' % (
        time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime(st.st_mtime)),
        st.st_mode & 0o777, st.st_size,
        hashlib.sha256(text.encode('utf-8')).hexdigest()[:16]))
    for key in keys:
        print('  %-22s %s' % (key, describe(key, vals.get(key))))
    sftp = ssh.open_sftp()
    try:
        names = sorted(n for n in sftp.listdir(REMOTE_ROOT) if n.startswith('.env'))
    except Exception:
        names = []
    sftp.close()
    print('  .env* in place : %s' % names)
    return vals, st


def build_plan(data, sets):
    """Print BEFORE/AFTER and the in-memory pre-check. Return new_data or None."""
    new_data, rows = build_rewrite(data, sets)
    print('')
    print('=== plan ===')
    if new_data is None:
        print('  ABORT: %s - file NOT touched.' % rows)
        return None
    for key, line_no, before, after in rows:
        print('  %-22s line %-4d BEFORE %s' % (key, line_no, describe(key, before)))
        print('  %-22s       AFTER  %s' % ('', describe(key, after)))

    expected = dict(sets)
    parsed, method = parse_env_text(new_data, list(expected))
    bad = [k for k, v in expected.items() if parsed.get(k) != v]
    if bad:
        print('  PRE-CHECK  : FAILED for %s (%s) - file NOT touched.' % (bad, method))
        return None
    print('  PRE-CHECK  : OK - the rewrite resolves correctly (%s)' % method)
    return new_data


def apply_sets(ssh, sets):
    data, st = _sftp_read(ssh, REMOTE_ENV)
    mode_before = st.st_mode & 0o777

    new_data = build_plan(data, sets)
    if new_data is None:
        return False

    if new_data == data:
        print('  NOTE       : the file already holds these values - no change needed.')
        return True

    ts = time.strftime('%Y%m%d_%H%M%S')
    bdir = '/home/%s/env_backup_%s' % (USER, ts)
    sftp = ssh.open_sftp()
    try:
        ssh.exec_command('mkdir -p %s' % bdir)
        time.sleep(1)
        with sftp.open('%s/.env' % bdir, 'w') as fh:
            fh.write(data)
        with sftp.open('%s.tmp' % REMOTE_ENV, 'w') as fh:
            fh.write(new_data)
        sftp.chmod('%s.tmp' % REMOTE_ENV, mode_before)
        sftp.posix_rename('%s.tmp' % REMOTE_ENV, REMOTE_ENV)
    finally:
        sftp.close()
    print('  BACKUP     : %s/.env  (%d bytes)' % (bdir, len(data.encode('utf-8'))))
    print('  WRITTEN    : renamed into place, mode preserved (%o)' % mode_before)

    expected = dict(sets)
    back, st2 = _sftp_read(ssh, REMOTE_ENV)
    print('  POST sha256: %s  mode=%o' % (
        hashlib.sha256(back.encode('utf-8')).hexdigest()[:16], st2.st_mode & 0o777))
    parsed2, method2 = parse_env_text(back, list(expected))
    ok = True
    for key, value in expected.items():
        got = parsed2.get(key)
        same = got == value
        ok = ok and same
        print('  POST-CHECK : %-22s -> %s  MATCH=%s' % (key, describe(key, got), same))
    if not ok:
        print('')
        print('  *** THE FILE WAS ALREADY REPLACED - the POST-CHECK failed (%s). ***'
              % method2)
        print('  Restore with:  cp %s/.env %s' % (bdir, REMOTE_ENV))
    return ok


def restart(ssh):
    print('')
    print('=== restarting the 4 units ===')
    sudo_pw = getpass.getpass('sudo password for %s@%s: ' % (USER, HOST))
    cmd = 'sudo -S -p "" systemctl restart %s; systemctl is-active %s' % (
        ' '.join(UNITS), ' '.join(UNITS))
    stdin, stdout, _ = ssh.exec_command(cmd, timeout=180)
    stdin.write(sudo_pw + '\n')
    stdin.flush()
    print(stdout.read().decode('utf-8', 'replace').rstrip())


def main():
    args = sys.argv[1:]
    sets = []
    i = 0
    while i < len(args):
        if args[i] == '--set':
            if i + 1 >= len(args) or '=' not in args[i + 1]:
                print('ABORT: --set needs KEY=VALUE')
                return 2
            k, v = args[i + 1].split('=', 1)
            sets.append((k.strip(), v))
            i += 2
            continue
        i += 1

    do_apply = '--apply' in args
    do_restart = '--restart' in args
    do_restart_only = '--restart-only' in args
    do_show = '--show' in args or not sets

    print('MODE = %s' % ('RESTART ONLY (no writes)' if do_restart_only
                         else ('SHOW (read-only)' if not sets and not do_apply
                               else ('APPLY' if do_apply else 'DRY RUN (nothing written)'))))
    ssh_pw = getpass.getpass('%s@%s password: ' % (USER, HOST))
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(HOST, username=USER, password=ssh_pw, timeout=30, banner_timeout=30)
    print('SSH connected to %s' % HOST)

    show_keys = list(SHOW_KEYS)
    for key, _v in sets:
        if key not in show_keys:
            show_keys.append(key)

    if do_restart_only:
        restart(ssh)
        ssh.close()
        return 0

    if do_show or do_apply:
        show(ssh, show_keys)

    if not sets:
        ssh.close()
        return 0

    if do_apply:
        ok = apply_sets(ssh, sets)
        if ok:
            show(ssh, show_keys)
            print('')
            print('The units must be restarted for the values to load.')
            if do_restart:
                restart(ssh)
            else:
                print('Restart with:  python patch_env_keys.py --restart-only')
        else:
            print('')
            print('Read the lines above: "file NOT touched" means .env is unchanged;')
            print('"ALREADY REPLACED" means it was rewritten and the restore command')
            print('was printed.')
        ssh.close()
        return 0 if ok else 1

    data, _st = _sftp_read(ssh, REMOTE_ENV)
    new_data = build_plan(data, sets)
    print('')
    if new_data is None:
        print('DRY RUN ended - nothing was written.')
        ssh.close()
        return 1
    if new_data == data:
        print('DRY RUN - the file already holds these values; --apply would be a no-op.')
    else:
        print('DRY RUN - nothing was written. Re-run with --apply to commit the change.')
    ssh.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
