"""Final proof for the email governance stack.

    python verify_email_final.py            read-only assertions + PASS/FAIL tally
    python verify_email_final.py send       ...plus ONE real SMTP send to the 4
                                            verified addresses and one negative
                                            control (must be dropped, no SMTP)

The negative control never reaches SMTP - GuardedEmailBackend drops it at the
choke point - so only ONE real email is transmitted, to addresses you confirmed
active.
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

ALLOW = ['primenaija9@gmail.com', 'infotech@stakenaija.com',
         'admin@stakenaija.ng', 'm.adeleke@stakenaija.com']

PAYLOAD = '''\
import os, sys
sys.path.insert(0, '/var/www/shop')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'poolbetting.settings')
import django
django.setup()
from django.conf import settings
from betting.models import SiteConfiguration
from betting import email_prefs as p
from betting import tasks as t

SEND = %r
ALLOW = %r
ALLOWK = {a.lower() for a in ALLOW}

RESULTS = []
def check(name, cond, detail=''):
    RESULTS.append(bool(cond))
    print('  [%%s] %%-62s %%s' %% ('PASS' if cond else 'FAIL', name, detail))

c = SiteConfiguration.objects.get(pk=1)
master_on, suppressed, allowed_set = p.load_policy()

print('=== state ===')
print('  master_on      = %%s' %% master_on)
print('  allowlist      = %%s' %% sorted(allowed_set or []))
print('  suppression    = %%s  (moot while the allowlist is set)' %% sorted(suppressed))
print('  EMAIL_BACKEND  = %%s' %% getattr(settings, 'EMAIL_BACKEND', None))
print('')

print('=== 1. global gates ===')
check('master kill switch is ON', master_on is True)
check('allowlist holds exactly the 4 verified addresses',
      allowed_set == ALLOWK, sorted(allowed_set or []))
check('Layer-1 backend is the choke point',
      getattr(settings, 'EMAIL_BACKEND', '').endswith('GuardedEmailBackend'),
      getattr(settings, 'EMAIL_BACKEND', None))

print('')
print('=== 2. per-event delivery (all 8 admin events) ===')
EVENTS = ([('withdrawal', e, t._withdrawal_admin_recipients_for_event(e))
           for e in ('requested', 'approved', 'completed', 'rejected')]
          + [('deposit', e, t._deposit_admin_recipients_for_event(e))
             for e in ('initiated', 'successful', 'failed', 'stuck_alert')])
all_ok_subset = True
for kind, ev, resolved in EVENTS:
    kept = p.filter_with_policy(resolved, suppressed, allowed_set)
    keptk = {k.lower() for k in kept}
    outside = [a for a in kept if a.lower() not in ALLOWK]
    dropped = [a for a in resolved if a.lower() not in keptk]
    subset = not outside
    all_ok_subset = all_ok_subset and subset and len(kept) > 0
    print('    %%-22s deliver=%%d dropped=%%d %%s' %% ('%%s[%%s]' %% (kind, ev), len(kept), len(dropped), kept))
check('every event delivers at least one recipient', all(len(p.filter_with_policy(r, suppressed, allowed_set)) > 0
                                                        for _, _, r in EVENTS))
check('NO delivered recipient is outside the allowlist', all_ok_subset)

print('')
print('=== 3. junk / dead addresses cannot be reached ===')
check('off-allowlist address is dropped',
      p.filter_with_policy(['someagent@example.com', 'old@dead.test'], suppressed, allowed_set) == [])
fallback = t._default_withdrawal_admin_recipients()
survivors = p.filter_with_policy(fallback, suppressed, allowed_set)
leaked = [a for a in survivors if a.lower() not in ALLOWK]
print('    legacy fallback resolved to %%d addresses, %%d survive Layer 1' %% (len(fallback), len(survivors)))
print('    survivors: %%s' %% survivors)
check('no non-allowlisted address survives the legacy fallback', leaked == [], leaked)

print('')
print('=== 4. role muting is still in force (nothing new can bounce) ===')
from betting.email_prefs import ADMIN_TOGGLES, USER_TOGGLES, AGENT_TOGGLES, CASHIER_TOGGLES
admin_names = [n for n, _l in ADMIN_TOGGLES]
muted = [n for n, _l in (USER_TOGGLES + AGENT_TOGGLES + CASHIER_TOGGLES)]
muted_on = [n for n in muted if p.email_enabled(n)]
admin_off = [n for n in admin_names if not p.email_enabled(n)]
print('    %%d non-admin toggles, %%d still ON' %% (len(muted), len(muted_on)))
print('    %%d admin toggles, %%d still OFF' %% (len(admin_names), len(admin_off)))
check('every user/agent/cashier toggle is OFF', muted_on == [], muted_on)
check('every admin toggle is ON', admin_off == [], admin_off)

if SEND:
    print('')
    print('=== 5. REAL send proof (one live email) ===')
    from django.core.mail import send_mail
    n = send_mail('StakeNaija email governance test',
                  'Admin notification email is working. Recipients are limited to the '
                  '4 verified addresses by the Site Configuration allowlist.',
                  settings.DEFAULT_FROM_EMAIL, ALLOW, fail_silently=False)
    check('live send to the 4 verified addresses returned 1', n == 1, 'returned %%s' %% n)
    n2 = send_mail('StakeNaija governance negative control',
                   'This must never be transmitted.',
                   settings.DEFAULT_FROM_EMAIL, ['dead@proof.local'], fail_silently=False)
    check('live send to an off-allowlist address was dropped (returned 0)', n2 == 0, 'returned %%s' %% n2)
else:
    print('')
    print('=== 5. REAL send proof ===')
    print('  [SKIP] re-run with:  send')

ok = sum(1 for r in RESULTS if r)
print('')
print('%%d/%%d PROOF TESTS PASS' %% (ok, len(RESULTS)))
sys.exit(0 if ok == len(RESULTS) else 1)
''' % (('send' in sys.argv[1:]), ALLOW)


def main():
    send = 'send' in sys.argv[1:]
    print('MODE = %s' % ('READ-ONLY + 1 LIVE EMAIL' if send else 'READ-ONLY'))

    pw = getpass.getpass('%s@%s password: ' % (USER, HOST))
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(HOST, username=USER, password=pw, timeout=30, banner_timeout=30)
    print('SSH connected to %s' % HOST)

    sftp = ssh.open_sftp()
    with sftp.open('/tmp/pb_verify_final.py', 'w') as fh:
        fh.write(PAYLOAD)
    sftp.close()

    cmd = ('cd %s && PYTHONPATH=%s ./env/bin/python /tmp/pb_verify_final.py 2>&1 '
           '| grep -v "^\\[V12" | grep -v "^INFO RUNTIME" | grep -v RuntimeWarning '
           '| grep -v "warnings.warn"' % (REMOTE_ROOT, REMOTE_ROOT))
    stdin, stdout, stderr = ssh.exec_command(cmd, timeout=300)
    out = stdout.read().decode('utf-8', 'replace')
    err = stderr.read().decode('utf-8', 'replace')
    rc = stdout.channel.recv_exit_status()
    print('')
    print(out.rstrip())
    if err.strip():
        print('[stderr] ' + err.rstrip())

    ssh.close()
    return rc


if __name__ == '__main__':
    sys.exit(main())
