"""READ-ONLY: how is /static/ actually served, and with what cache policy?

    python probe_static_serving.py

Answers three questions and writes nothing:
  1. which Nginx config serves /static/ and from which directory
  2. what cache headers /static/ carries (an 'expires' would hide a fix from
     users who already hold the old file)
  3. the on-disk state of notifications_nav.js in both candidate locations
"""
import sys, getpass

try:
    import paramiko
except ImportError:
    print('paramiko missing.  Run:  pip install paramiko')
    sys.exit(1)

HOST = '172.236.9.63'
USER = 'stakenaija'

JS = 'notifications/notifications_nav.js'
PATHS = ['/var/www/shop/staticfiles/' + JS,
         '/var/www/shop/notifications/static/' + JS]

CMDS = [
    ('1a. nginx config files', 'ls -la /etc/nginx/sites-enabled/ /etc/nginx/conf.d/ 2>/dev/null'),
    ('1b. the /static/ location block',
     "grep -rn -A 12 'location /static' /etc/nginx/sites-enabled/ /etc/nginx/conf.d/ /etc/nginx/nginx.conf 2>/dev/null | head -70"),
    ('1c. any other staticfiles reference',
     "grep -rn 'staticfiles\\|/static' /etc/nginx/sites-enabled/ /etc/nginx/conf.d/ 2>/dev/null | head -30"),
    ('2. cache / expires directives in scope',
     "grep -rn 'expires\\|Cache-Control\\|etag\\|open_file_cache' /etc/nginx/sites-enabled/ /etc/nginx/conf.d/ /etc/nginx/nginx.conf 2>/dev/null | head -30"),
    ('3a. on-disk candidates',
     'for p in %s; do if [ -f "$p" ]; then stat -c "%%n  size=%%s  mtime=%%y  mode=%%a" "$p"; '
     'sha256sum "$p"; else echo "$p  MISSING"; fi; done' % ' '.join(PATHS)),
    ('3b. what the app source dir resolves to',
     'ls -la /var/www/shop/notifications/static/notifications/ 2>/dev/null'),
    ('3c. collectstatic last-run hints',
     'ls -la /var/www/shop/staticfiles/notifications/ 2>/dev/null'),
]


def main():
    print('MODE = READ-ONLY (nothing written)')
    ssh_pw = getpass.getpass('%s@%s password: ' % (USER, HOST))
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(HOST, username=USER, password=ssh_pw, timeout=30, banner_timeout=30)
    print('SSH connected to %s' % HOST)

    for title, cmd in CMDS:
        print('')
        print('=== %s ===' % title)
        _in, out, err = ssh.exec_command(cmd, timeout=60)
        text = out.read().decode('utf-8', 'replace').rstrip()
        errtext = err.read().decode('utf-8', 'replace').strip()
        print(text if text else '  (no output)')
        if errtext:
            print('  [stderr] %s' % errtext)

    ssh.close()
    print('')
    print('Nothing was written. Paste this back.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
