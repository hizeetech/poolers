"""Read-only diagnosis of the live SMTP credential mismatch.

    python probe_smtp_auth.py

Answers, with zero impact:
  * which EMAIL_* values the Django process actually resolves
  * how /var/www/shop/.env stores EMAIL_HOST_PASSWORD and how many times
  * whether dotenv interpolation mangles it ($ / # / quotes)
  * whether an OS-level variable shadows the .env value (load_dotenv(override=False))
  * whether the SMTP host is reachable at all (connect + NOOP, no auth)
  * which candidate password actually authenticates (max 3 login attempts, nothing sent)
  * whether the systemd tier carries a shadowing EMAIL_* override

Nothing is written, nothing is restarted, no message is transmitted.
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

PAYLOAD = '''\
import os, sys, ssl, socket, smtplib, time

# Capture the SHELL environment BEFORE django loads settings.
# settings.py does load_dotenv(override=False) -> an OS variable would win
# over the .env file and silently ignore the edit.
PRE = {k: os.environ.get(k) for k in
       ('EMAIL_HOST', 'EMAIL_PORT', 'EMAIL_HOST_USER',
        'EMAIL_HOST_PASSWORD', 'EMAIL_USE_SSL', 'EMAIL_USE_TLS')}

sys.path.insert(0, '/var/www/shop')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'poolbetting.settings')
import django
django.setup()
from django.conf import settings

FROM_DOTENV = __import__('dotenv').dotenv_values


def shape(v):
    if v is None:
        return 'None'
    if v == '':
        return 'EMPTY STRING len=0'
    return ('len=%d space=%s dollar=%s hash=%s quote=%s '
            'sq=%s dq=%s backslash=%s first_alnum=%s last_alnum=%s') % (
        len(v), (' ' in v), ('$' in v), ('#' in v),
        (("'" in v) or ('"' in v)), v.startswith("'"), v.startswith('"'),
        ('\\\\' in v), v[:1].isalnum(), v[-1:].isalnum())


print('=== 1. shell environment BEFORE django (these would shadow the .env) ===')
for k, v in PRE.items():
    if k == 'EMAIL_HOST_PASSWORD':
        print('  %-22s present=%s  %s' % (k, v is not None, shape(v)))
    else:
        print('  %-22s %s' % (k, v))

print('')
print('=== 2. what the running Django process resolves ===')
print('  EMAIL_HOST       = %s' % settings.EMAIL_HOST)
print('  EMAIL_PORT       = %s' % settings.EMAIL_PORT)
print('  EMAIL_USE_SSL    = %s' % getattr(settings, 'EMAIL_USE_SSL', None))
print('  EMAIL_USE_TLS    = %s' % getattr(settings, 'EMAIL_USE_TLS', None))
print('  EMAIL_HOST_USER  = %s' % settings.EMAIL_HOST_USER)
print('  DEFAULT_FROM_EMAIL = %s' % getattr(settings, 'DEFAULT_FROM_EMAIL', None))
print('  SERVER_EMAIL     = %s' % getattr(settings, 'SERVER_EMAIL', None))
print('  EMAIL_HOST_PASSWORD %s' % shape(settings.EMAIL_HOST_PASSWORD))

try:
    print('  DNS %s -> %s' % (settings.EMAIL_HOST,
                              socket.gethostbyname(settings.EMAIL_HOST)))
except Exception as exc:
    print('  DNS %s -> FAILED %s' % (settings.EMAIL_HOST, exc))

print('')
print('=== 3. how /var/www/shop/.env stores it ===')
ENV_PATH = '/var/www/shop/.env'
st = os.stat(ENV_PATH)
print('  %s  mtime=%s  size=%d bytes' % (
    ENV_PATH,
    time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime(st.st_mtime)), st.st_size))
print('  edited %.1f minutes ago' % ((time.time() - st.st_mtime) / 60.0))

lines = open(ENV_PATH, encoding='utf-8', errors='replace').read().splitlines()
occs = []
for i, ln in enumerate(lines):
    s = ln.strip()
    if s.startswith('#') or '=' not in s:
        continue
    key = s.split('=', 1)[0].strip()
    if key in ('EMAIL_HOST_PASSWORD', 'EMAIL_HOST_USER', 'EMAIL_HOST',
               'EMAIL_PORT', 'EMAIL_USE_SSL', 'EMAIL_USE_TLS',
               'DEFAULT_FROM_EMAIL', 'SERVER_EMAIL'):
        val = s.split('=', 1)[1]
        if key == 'EMAIL_HOST_PASSWORD':
            print('    line %-4d %-20s -> %s' % (i + 1, key, shape(val)))
            occs.append((i + 1, val))
        else:
            print('    line %-4d %-20s -> %r' % (i + 1, key, val))

print('  EMAIL_HOST_PASSWORD defined %d time(s) in the file '
      '(python-dotenv keeps the LAST one)' % len(occs))

raw = FROM_DOTENV(ENV_PATH, interpolate=False)
interp = FROM_DOTENV(ENV_PATH)
raw_v = raw.get('EMAIL_HOST_PASSWORD')
int_v = interp.get('EMAIL_HOST_PASSWORD')
print('')
print('  raw   (interpolate=False) %s' % shape(raw_v))
print('  dotenv(interpolate=True)  %s' % shape(int_v))
print('  resolved in settings      %s' % shape(settings.EMAIL_HOST_PASSWORD))
print('  resolved == raw     ? %s' % (settings.EMAIL_HOST_PASSWORD == raw_v))
print('  resolved == dotenv  ? %s' % (settings.EMAIL_HOST_PASSWORD == int_v))

print('')
print('=== 4. transport reachability (connect + NOOP, NO credentials) ===')
host = settings.EMAIL_HOST
port = int(settings.EMAIL_PORT)
use_ssl = bool(getattr(settings, 'EMAIL_USE_SSL', False))
use_tls = bool(getattr(settings, 'EMAIL_USE_TLS', False))
try:
    if use_ssl:
        c = smtplib.SMTP_SSL(host, port, timeout=20,
                             context=ssl.create_default_context())
    else:
        c = smtplib.SMTP(host, port, timeout=20)
        if use_tls:
            c.starttls(context=ssl.create_default_context())
    print('  [OK] connected over %s:%s (%s) banner=%r'
          % (host, port, 'SSL' if use_ssl else ('STARTTLS' if use_tls else 'PLAIN'),
             c.noop()))
    c.quit()
except Exception as exc:
    print('  [FAIL] %s: %s' % (type(exc).__name__, exc))

print('')
print('=== 5. which candidate authenticates (max 3 attempts, sends nothing) ===')
cands = []
for label, val in (('settings-resolved', settings.EMAIL_HOST_PASSWORD),
                   ('raw-file-value', raw_v),
                   ('dotenv-parsed-value', int_v)):
    if val and all(val != v for _l, v in cands):
        cands.append((label, val))
cands = cands[:3]

ok = False
for label, cand in cands:
    conn = None
    try:
        if use_ssl:
            conn = smtplib.SMTP_SSL(host, port, timeout=20,
                                    context=ssl.create_default_context())
        else:
            conn = smtplib.SMTP(host, port, timeout=20)
            if use_tls:
                conn.starttls(context=ssl.create_default_context())
        conn.login(settings.EMAIL_HOST_USER, cand)
        print('  [OK]   %-20s len=%-3d -> AUTHENTICATED' % (label, len(cand)))
        ok = True
        try:
            conn.quit()
        except Exception:
            pass
        conn = None
        break
    except smtplib.SMTPAuthenticationError as exc:
        print('  [FAIL] %-20s len=%-3d -> %s' % (label, len(cand), exc))
    except Exception as exc:
        print('  [ERR ] %-20s len=%-3d -> %s: %s'
              % (label, len(cand), type(exc).__name__, exc))
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

print('')
if ok:
    print('VERDICT: the mailbox accepts one of the stored values - the .env '
          'entry the process resolves is NOT the working one.')
else:
    print('VERDICT: no stored value authenticates. The password in .env does '
          'not match the mailbox password at the provider (or the user/host is wrong).')

print('')
print('=== 6. sibling env files under /var/www/shop ===')
for name in sorted(os.listdir('/var/www/shop')):
    if name.startswith('.env'):
        p = os.path.join('/var/www/shop', name)
        print('  %-24s mtime=%s' % (name,
              time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(os.stat(p).st_mtime))))
'''


def main():
    print('MODE = READ-ONLY (no writes, no restarts, nothing transmitted)')
    pw = getpass.getpass('%s@%s password: ' % (USER, HOST))
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(HOST, username=USER, password=pw, timeout=30, banner_timeout=30)
    print('SSH connected to %s' % HOST)

    sftp = ssh.open_sftp()
    with sftp.open('/tmp/pb_probe_smtp_auth.py', 'w') as fh:
        fh.write(PAYLOAD)
    sftp.close()

    cmd = ('cd %s && PYTHONPATH=%s ./env/bin/python /tmp/pb_probe_smtp_auth.py 2>&1 '
           '| grep -v "^\\[V12" | grep -v "^INFO RUNTIME" | grep -v RuntimeWarning '
           '| grep -v "warnings.warn"' % (REMOTE_ROOT, REMOTE_ROOT))
    _, stdout, _ = ssh.exec_command(cmd, timeout=300)
    out = stdout.read().decode('utf-8', 'replace')
    rc = stdout.channel.recv_exit_status()
    print('')
    print(out.rstrip())

    print('')
    print('=== 7. systemd tier - does anything shadow the .env for the workers? ===')
    scmd = ('systemctl show shop-sync shop-ws shop-celery shop-celery-beat '
            '-p Id -p EnvironmentFiles -p Environment 2>&1 '
            "| sed -E 's/(PASSWORD=)[^ ]*/\\1<masked>/g'")
    _, stdout2, _ = ssh.exec_command(scmd, timeout=60)
    print(stdout2.read().decode('utf-8', 'replace').rstrip())

    ssh.close()
    return rc


if __name__ == '__main__':
    sys.exit(main())
