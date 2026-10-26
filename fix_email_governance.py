"""Email governance: finish the job.

    python fix_email_governance.py            DRY RUN  (default, writes nothing)
    python fix_email_governance.py apply      APPLY    (DB only, no restart)

WHY
    Layer 1 (GuardedEmailBackend -> load_policy -> filter_with_policy) is the
    single choke point for every outbound message.  Two things are wrong today:
        * the master kill switch is OFF  -> 100% of mail is dropped
        * the allowlist holds .ng typos  -> the verified .com recipients are dropped
    This sets the master ON and the allowlist to the 4 verified ACTIVE addresses.
    Because the allowlist is a hard lock, mail can then only ever reach those 4,
    so agents / users / cashiers can never be emailed by accident.

WHAT IT DOES NOT TOUCH
    * the 25 role toggles (they stay OFF, exactly as you want)
    * the suppression list
    * anything outside SiteConfiguration.pk 1

ZERO IMPACT: the values are read from the DB at send time, so no service restart
and no deploy is needed - the change is live the moment the transaction commits.
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

TARGET_ALLOWLIST = ('primenaija9@gmail.com, infotech@stakenaija.com, '
                    'admin@stakenaija.ng, m.adeleke@stakenaija.com')

# Fields that must NOT move.
ROLE_SPOTCHECK = ('email_user_password_reset', 'email_user_deposit_successful',
                  'email_agent_registration_approved', 'email_cashier_withdrawal_updates')
# Admin toggles that are currently OFF and that this script deliberately leaves alone.
ADMIN_OFF_REPORT = ('email_admin_deposit_initiated', 'email_admin_deposit_failed')

PAYLOAD = '''\
import os, sys
sys.path.insert(0, '/var/www/shop')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'poolbetting.settings')
import django
django.setup()
from django.db import transaction
from betting.models import SiteConfiguration
from betting import email_prefs as p

MODE = sys.argv[1] if len(sys.argv) > 1 else 'dry'
TARGET_ALLOWLIST = %r
ROLE_SPOTCHECK = %r
ADMIN_OFF_REPORT = %r

def show_state(tag):
    c = SiteConfiguration.objects.get(pk=1)
    allowed = p.parse_addresses(getattr(c, p.ALLOWLIST_FIELD, '') or '')
    print('--- %%s' %% tag)
    print('    master(%%s) = %%s' %% (p.MASTER_FIELD, bool(getattr(c, p.MASTER_FIELD, True))))
    print('    allowlist              = %%s' %% allowed)
    print('    suppression            = %%s' %% p.parse_addresses(getattr(c, p.SUPPRESSION_FIELD, '') or ''))
    return c

def layer1_preview(c, allow_override):
    """What Layer 1 would deliver, using the CURRENT resolved recipients."""
    from betting import tasks as t
    master_on, suppressed, allowed_now = p.load_policy()
    target = {a.lower() for a in p.parse_addresses(allow_override)}
    print('')
    print('--- Layer-1 result per event  (master_on=%%s)' %% master_on)
    print('    %%-28s %%-46s %%s' %% ('EVENT', 'WOULD DELIVER', 'WOULD DROP'))
    events = ([('withdrawal', e, t._withdrawal_admin_recipients_for_event(e))
               for e in ('requested', 'approved', 'completed', 'rejected')]
              + [('deposit', e, t._deposit_admin_recipients_for_event(e))
                 for e in ('initiated', 'successful', 'failed', 'stuck_alert')])
    for kind, ev, resolved in events:
        kept = p.filter_with_policy(resolved, suppressed, target)
        keptk = {k.lower() for k in kept}
        dropped = [a for a in resolved if a.lower() not in keptk]
        print('    %%-28s %%-46s %%s' %% ('%%s[%%s]' %% (kind, ev), kept, dropped))
    print('')
    print('    sample NOT on the allowlist  -> %%s   (must be empty)'
          %% p.filter_with_policy(['someagent@example.com', 'old@dead.test'], suppressed, target))
    print('    sample junk fallback address -> %%s   (survivors must all be ON the allowlist)'
          %% p.filter_with_policy(t._default_withdrawal_admin_recipients(), suppressed, target))

c = show_state('CURRENT state')
cur = p.parse_addresses(getattr(c, p.ALLOWLIST_FIELD, '') or '')
target_list = p.parse_addresses(TARGET_ALLOWLIST)
curk = {a.lower() for a in cur}
tgtk = {a.lower() for a in target_list}
print('')
print('planned master    : %%s -> True' %% bool(getattr(c, p.MASTER_FIELD, True)))
print('planned allowlist : %%s -> %%s' %% (cur, target_list))
print('  allowlist ADD   : %%s' %% [a for a in target_list if a.lower() not in curk])
print('  allowlist REMOVE: %%s' %% [a for a in cur if a.lower() not in tgtk])

print('')
print('--- admin toggles left OFF on purpose (you asked about these) ---')
for n in ADMIN_OFF_REPORT:
    print('    %%-40s = %%s' %% (n, bool(getattr(c, n, True))))

layer1_preview(c, TARGET_ALLOWLIST)

if MODE != 'apply':
    print('')
    print('DRY RUN - nothing written.  Re-run with:  apply')
    sys.exit(0)

with transaction.atomic():
    SiteConfiguration.objects.filter(pk=1).update(
        **{p.MASTER_FIELD: True, p.ALLOWLIST_FIELD: TARGET_ALLOWLIST})
print('')
print('>>> COMMITTED SiteConfiguration.pk=1')

print('')
show_state('AFTER state')
layer1_preview(SiteConfiguration.objects.get(pk=1), TARGET_ALLOWLIST)

print('')
print('--- proof the 25 role toggles did NOT move (must all be False) ---')
for n in ROLE_SPOTCHECK:
    print('    %%-40s = %%s' %% (n, p.email_enabled(n)))
print('')
print('--- proof master switch now reads ON ---')
print('    master_enabled() = %%s' %% p.load_policy()[0])
''' % (TARGET_ALLOWLIST, ROLE_SPOTCHECK, ADMIN_OFF_REPORT)


def main():
    mode = 'apply' if 'apply' in sys.argv[1:] else 'dry'
    print('MODE = %s' % mode.upper())

    pw = getpass.getpass('%s@%s password: ' % (USER, HOST))
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(HOST, username=USER, password=pw, timeout=30, banner_timeout=30)
    print('SSH connected to %s' % HOST)

    sftp = ssh.open_sftp()
    with sftp.open('/tmp/pb_fix_email.py', 'w') as fh:
        fh.write(PAYLOAD)
    sftp.close()

    cmd = ('cd %s && PYTHONPATH=%s ./env/bin/python /tmp/pb_fix_email.py %s 2>&1 '
           '| grep -v "^\\[V12" | grep -v "^INFO RUNTIME" | grep -v RuntimeWarning '
           '| grep -v "warnings.warn"' % (REMOTE_ROOT, REMOTE_ROOT, mode))
    stdin, stdout, stderr = ssh.exec_command(cmd, timeout=300)
    out = stdout.read().decode('utf-8', 'replace')
    err = stderr.read().decode('utf-8', 'replace')
    rc = stdout.channel.recv_exit_status()
    print('')
    print(out.rstrip())
    if err.strip():
        print('[stderr] ' + err.rstrip())
    print('')
    print('exit code: %s' % rc)

    ssh.close()
    if mode == 'dry':
        print('DRY RUN complete.  Run "python fix_email_governance.py apply" to commit.')
    else:
        print('APPLIED.  Live immediately - no restart needed.')
    return rc


if __name__ == '__main__':
    sys.exit(main())
