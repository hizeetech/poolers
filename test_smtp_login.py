"""Prove which SMTP credential Namecheap actually accepts, and patch .env safely.

    python test_smtp_login.py                     test the mailbox in .env
    python test_smtp_login.py --user other@x.y    test a different SMTP username
    python test_smtp_login.py --label "app pw"    tag the run in your own log
    python test_smtp_login.py --show              read-only: what .env holds now
    python test_smtp_login.py --apply             after a PASS, rewrite .env
    python test_smtp_login.py --user x@y --apply  ...also switch EMAIL_HOST_USER
    python test_smtp_login.py --apply --restart   ...and restart the 4 units
    python test_smtp_login.py --restart-only      only restart (no SMTP prompts)

Each test run makes exactly 2 authentication attempts: SMTP 465 SSL (what the
site actually sends through) and IMAP 993 SSL (read-only, proves whether the
CREDENTIAL is valid even when SMTP is refused). No messages are sent and no
mailbox is modified by the IMAP login.

The password is typed locally, never echoed, never written to disk, and is
piped to the remote process over stdin - so it never appears in argv, in `ps`,
in shell history or in any server-side log.

WRITE ORDER (learned the hard way): the rewritten text is verified IN MEMORY
first; the file is backed up and only then renamed into place; the file mode is
preserved; and a post-write check runs last. If the post-write check fails the
file has ALREADY been replaced, so the message says so and prints the restore
command - it never claims "unchanged".
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
ENV_KEYS = ['EMAIL_HOST', 'EMAIL_PORT', 'EMAIL_USE_SSL', 'EMAIL_USE_TLS',
            'EMAIL_HOST_USER', 'EMAIL_HOST_PASSWORD',
            'DEFAULT_FROM_EMAIL', 'SERVER_EMAIL']

PAYLOAD = '''\
import os, sys, ssl, smtplib, imaplib

PWD = sys.stdin.readline().rstrip('\\n').rstrip('\\r')
USERNAME = sys.argv[1] if len(sys.argv) > 1 else None
if not PWD:
    print('RESULT=ERROR empty password on stdin')
    sys.exit(1)

sys.path.insert(0, '/var/www/shop')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'poolbetting.settings')
import django
django.setup()
from django.conf import settings

host = settings.EMAIL_HOST
user = USERNAME or settings.EMAIL_HOST_USER
print('host=%s  username=%s  password_len=%d' % (host, user, len(PWD)))
print('')

results = {}

# --- SMTP (what the site actually sends through) ---
try:
    c = smtplib.SMTP_SSL(host, 465, timeout=25, context=ssl.create_default_context())
    try:
        c.login(user, PWD)
        print('  [OK]   SMTP 465 SSL   -> AUTHENTICATED')
        results['SMTP'] = 'OK'
    finally:
        try:
            c.quit()
        except Exception:
            try:
                c.close()
            except Exception:
                pass
except smtplib.SMTPAuthenticationError as exc:
    print('  [FAIL] SMTP 465 SSL   -> %s' % exc)
    results['SMTP'] = 'AUTH_FAIL'
except Exception as exc:
    print('  [ERR ] SMTP 465 SSL   -> %s: %s' % (type(exc).__name__, exc))
    results['SMTP'] = 'ERROR'

# --- IMAP (read-only discriminator: proves whether the CREDENTIAL is valid) ---
try:
    m = imaplib.IMAP4_SSL(host, 993, timeout=25)
    try:
        m.login(user, PWD)
        print('  [OK]   IMAP 993 SSL   -> AUTHENTICATED')
        results['IMAP'] = 'OK'
    finally:
        try:
            m.logout()
        except Exception:
            pass
except imaplib.IMAP4.error as exc:
    print('  [FAIL] IMAP 993 SSL   -> %s' % exc)
    results['IMAP'] = 'AUTH_FAIL'
except Exception as exc:
    print('  [ERR ] IMAP 993 SSL   -> %s: %s' % (type(exc).__name__, exc))
    results['IMAP'] = 'ERROR'

smtp_r = results.get('SMTP')
imap_r = results.get('IMAP')

print('')
print('RESULT=SMTP:%s  IMAP:%s' % (smtp_r, imap_r))
print('')
if smtp_r == 'OK':
    print('VERDICT: credential ACCEPTED for sending. Apply it and restart.')
elif imap_r == 'OK' and smtp_r != 'OK':
    print('VERDICT: the credential is VALID (IMAP accepted it) but SMTP is')
    print('         refused. That is a provider-side SMTP block/limit on this')
    print('         mailbox - exactly the bounce-penalty case. The password is')
    print('         NOT the problem, so do not touch .env yet.')
else:
    print('VERDICT: the credential itself is rejected on BOTH protocols, so this')
    print('         is the wrong password for this mailbox (or the mailbox has')
    print('         2FA enabled, which mandates an app-specific password).')
sys.exit(0 if smtp_r == 'OK' else 1)
'''


def describe(v):
    if v is None:
        return 'None'
    if v == '':
        return 'EMPTY STRING'
    return ('len=%d leading_space=%s trailing_space=%s dollar=%s hash=%s '
            'quote=%s backslash=%s') % (
        len(v), v[:1] == ' ', v[-1:] == ' ', '$' in v, '#' in v,
        ("'" in v) or ('"' in v), '\\' in v)


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
    """Resolve keys from raw .env text the same way python-dotenv would.

    python-dotenv needs a real path, so the text is staged in a local temp file.
    """
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


def show_state(ssh):
    """Read-only dump of what the live .env holds right now."""
    text, st = _sftp_read(ssh, REMOTE_ENV)
    vals, method = parse_env_text(text, ENV_KEYS)
    print('')
    print('=== %s  (read-only, parsed with %s) ===' % (REMOTE_ENV, method))
    print('  mtime=%s  mode=%o  size=%d bytes  sha256=%s' % (
        time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime(st.st_mtime)),
        st.st_mode & 0o777, st.st_size,
        hashlib.sha256(text.encode('utf-8')).hexdigest()[:16]))
    for key in ENV_KEYS:
        v = vals.get(key)
        if key == 'EMAIL_HOST_PASSWORD':
            print('  %-20s %s' % (key, describe(v)))
        else:
            print('  %-20s %r' % (key, v))
    for n, ln in enumerate(text.split('\n'), 1):
        if ln.strip().startswith('EMAIL_HOST_PASSWORD'):
            print('  (EMAIL_HOST_PASSWORD is defined on line %d)' % n)

    sftp = ssh.open_sftp()
    try:
        names = sorted(n for n in sftp.listdir(REMOTE_ROOT) if n.startswith('.env'))
    except Exception:
        names = []
    sftp.close()
    print('  .env* in place : %s' % names)
    if any(n.endswith('.tmp') for n in names):
        print('  WARNING: a leftover .tmp file exists - the rename did not complete.')
    return vals


def run(ssh, user, password):
    sftp = ssh.open_sftp()
    with sftp.open('/tmp/pb_test_login.py', 'w') as fh:
        fh.write(PAYLOAD)
    sftp.close()

    cmd = ('cd %s && PYTHONPATH=%s ./env/bin/python /tmp/pb_test_login.py %s 2>&1 '
           '| grep -v "^\\[V12" | grep -v "^INFO RUNTIME" | grep -v RuntimeWarning '
           '| grep -v "warnings.warn"'
           % (REMOTE_ROOT, REMOTE_ROOT, '"%s"' % user if user else ''))
    stdin, stdout, _ = ssh.exec_command(cmd, timeout=180)
    stdin.write(password + '\n')
    stdin.flush()
    out = stdout.read().decode('utf-8', 'replace')
    rc = stdout.channel.recv_exit_status()
    print(out.rstrip())
    return rc == 0


def apply_password(ssh, new_value, new_user=None):
    """Rewrite the EMAIL_HOST_* lines: verify, back up, rename, verify again."""
    data, st = _sftp_read(ssh, REMOTE_ENV)
    mode_before = st.st_mode & 0o777
    lines = data.split('\n')

    edits = [('EMAIL_HOST_PASSWORD', new_value)]
    if new_user:
        edits.insert(0, ('EMAIL_HOST_USER', new_user))

    print('')
    print('=== .env patch ===')
    print('  target        : %s  mode=%o  %d bytes  sha256=%s' % (
        REMOTE_ENV, mode_before, len(data.encode('utf-8')),
        hashlib.sha256(data.encode('utf-8')).hexdigest()[:16]))
    for key, value in edits:
        hits = [i for i, ln in enumerate(lines) if ln.strip().startswith(key + '=')]
        if len(hits) != 1:
            print('  ABORT: expected exactly 1 %s line, found %d - file NOT touched.'
                  % (key, len(hits)))
            return False
        idx = hits[0]
        print('  %-20s line %d  BEFORE %s' % (key, idx + 1,
                                              describe(lines[idx].split('=', 1)[1])))
        print('  %-20s          AFTER  %s' % (key, describe(value)))
        lines[idx] = key + '=' + quote_for_env(value)

    new_data = '\n'.join(lines)
    expected = dict(edits)

    # 1) verify the rewrite in memory, before the file is touched
    parsed, method = parse_env_text(new_data, list(expected))
    bad = [k for k, v in expected.items() if parsed.get(k) != v]
    if bad:
        print('  PRE-CHECK     : FAILED for %s (%s) - file NOT touched.' % (bad, method))
        return False
    print('  PRE-CHECK     : OK - the rewrite resolves correctly (%s)' % method)

    # 2) back up, then rename into place, preserving the file mode
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
    print('  BACKUP        : %s/.env  (%d bytes)' % (bdir, len(data.encode('utf-8'))))
    print('  WRITTEN       : renamed into place, mode preserved (%o)' % mode_before)

    # 3) post-write check - from here on the file HAS been replaced
    back, st2 = _sftp_read(ssh, REMOTE_ENV)
    print('  POST sha256   : %s  mode=%o' % (
        hashlib.sha256(back.encode('utf-8')).hexdigest()[:16], st2.st_mode & 0o777))
    parsed2, method2 = parse_env_text(back, list(expected))
    ok = True
    for key, value in expected.items():
        got = parsed2.get(key)
        same = got == value
        ok = ok and same
        if key == 'EMAIL_HOST_USER':
            print('  POST-CHECK    : %s resolves to %r  MATCH=%s' % (key, got, same))
        else:
            print('  POST-CHECK    : %s resolves to %s  MATCH=%s'
                  % (key, describe(got), same))

    if not ok:
        print('')
        print('  *** THE FILE WAS ALREADY REPLACED - the POST-CHECK failed (%s). ***'
              % method2)
        print('  Restore it with:')
        print('    cp %s/.env %s' % (bdir, REMOTE_ENV))
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
    do_apply = '--apply' in args
    do_restart = '--restart' in args
    do_show = '--show' in args
    restart_only = '--restart-only' in args
    user = None
    if '--user' in args:
        user = args[args.index('--user') + 1]
    label = args[args.index('--label') + 1] if '--label' in args else ''

    if restart_only:
        mode = 'RESTART ONLY (no SMTP test, no writes)'
    elif do_show:
        mode = 'READ-ONLY (.env inspection, nothing written)'
    elif do_apply:
        mode = 'TEST + WRITE .env' + (' + RESTART' if do_restart else '')
    else:
        mode = 'TEST ONLY (nothing written)'
    print('MODE = %s' % mode)
    if label:
        print('LABEL = %s' % label)

    ssh_pw = getpass.getpass('%s@%s password: ' % (USER, HOST))
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(HOST, username=USER, password=ssh_pw, timeout=30, banner_timeout=30)
    print('SSH connected to %s' % HOST)

    if restart_only:
        show_state(ssh)
        restart(ssh)
        print('')
        print('Next:  python verify_email_final.py send')
        ssh.close()
        return 0

    if do_show:
        show_state(ssh)
        ssh.close()
        return 0

    smtp_pw = getpass.getpass('SMTP password to verify (not echoed, not stored): ')
    if not smtp_pw:
        print('ABORT: empty password')
        ssh.close()
        return 2
    confirm = getpass.getpass('Type it again to rule out a typo: ')
    if confirm != smtp_pw:
        print('ABORT: the two entries differ - nothing was tested or written.')
        ssh.close()
        return 2

    ok = run(ssh, user, smtp_pw)

    if ok and do_apply:
        print('')
        print('Credential verified. Writing it to %s ...' % REMOTE_ENV)
        if apply_password(ssh, smtp_pw, new_user=user):
            print('')
            print('DONE and verified. The units must be restarted for the value to load.')
            if do_restart:
                restart(ssh)
            else:
                print('Restart with:  python test_smtp_login.py --restart-only')
        else:
            print('')
            print('The patch reported a problem - read the lines above carefully:')
            print('  * "file NOT touched"      -> .env is unchanged')
            print('  * "ALREADY REPLACED"      -> .env WAS rewritten; the restore')
            print('                               command is printed above')
    elif ok:
        print('')
        print('Next:  python test_smtp_login.py --apply')
        print('       then python test_smtp_login.py --restart-only')
        print('       then python verify_email_final.py send')

    ssh.close()
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
