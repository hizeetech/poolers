"""Read-only health check for shop-sync after the restart.

Answers one question: is the service actually serving requests, or did the
restart leave it up-but-not-working (the `workers: 0` we saw)?

Nothing is written and nothing is restarted. Read-only.

    python probe_shop_sync_health.py
"""
import getpass
import sys

try:
    import paramiko
except ImportError:
    print('paramiko missing.  Run:  pip install paramiko')
    sys.exit(1)

HOST = '172.236.9.63'
USER = 'stakenaija'
SERVICE = 'shop-sync'
SOCKET = '/var/www/shop/run/shop.sock'

REMOTE_SCRIPT = r'''
svc="%s"
sock="%s"

echo "=== 1. systemd state ==="
echo "ActiveState : $(systemctl show $svc -p ActiveState --value)"
echo "SubState    : $(systemctl show $svc -p SubState --value)"
echo "MainPID     : $(systemctl show $svc -p MainPID --value)"
echo "Tasks       : $(systemctl show $svc -p Tasks --value)"
echo "NRestarts   : $(systemctl show $svc -p NRestarts --value)"
echo "ActiveEnter : $(systemctl show $svc -p ActiveEnterTimestamp --value)"

main=$(systemctl show $svc -p MainPID --value)
echo ""
echo "=== 2. process tree under MainPID ($main) ==="
if [ -n "$main" ] && [ "$main" != "0" ]; then
  echo "-- direct children --"
  ps -o pid,ppid,etime,cmd --ppid "$main" 2>/dev/null | head -40
  echo "-- child count: $(ps -o pid= --ppid "$main" 2>/dev/null | wc -l)"
else
  echo "MainPID is empty or 0"
fi

echo ""
echo "=== 3. all gunicorn processes ==="
echo "count: $(pgrep -f gunicorn 2>/dev/null | wc -l)"
pgrep -af gunicorn 2>/dev/null | head -40

echo ""
echo "=== 4. listener / socket ==="
ls -l "$sock" 2>&1
echo "listening sockets:"
(ss -xlp 2>/dev/null || netstat -xlp 2>/dev/null) | grep -i shop | head -10

echo ""
echo "=== 5. HTTP smoke test over the unix socket ==="
if command -v curl >/dev/null 2>&1; then
  echo "GET /login/  -> $(curl -s -o /dev/null -w '%%{http_code}' --max-time 15 --unix-socket "$sock" http://localhost/login/ 2>&1)"
  echo "GET /        -> $(curl -s -o /dev/null -w '%%{http_code}' --max-time 15 --unix-socket "$sock" http://localhost/ 2>&1)"
  echo "GET /dashboard/ (no auth, expect 302) -> $(curl -s -o /dev/null -w '%%{http_code}' --max-time 15 --unix-socket "$sock" http://localhost/dashboard/ 2>&1)"
else
  echo "curl not installed; skipping"
fi

echo ""
echo "=== 6. last 20 journal lines ==="
sudo -n journalctl -u $svc -n 20 --no-pager 2>/dev/null || journalctl -u $svc -n 20 --no-pager 2>&1 | tail -25
''' % (SERVICE, SOCKET)


def main():
    pw = getpass.getpass('%s@%s password: ' % (USER, HOST))
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(HOST, username=USER, password=pw, timeout=30, banner_timeout=30)
    print('SSH connected to %s' % HOST)
    print('MODE = READ-ONLY (no writes, no restarts)')

    stdin, stdout, stderr = ssh.exec_command(REMOTE_SCRIPT, timeout=120)
    out = stdout.read().decode('utf-8', 'replace')
    err = stderr.read().decode('utf-8', 'replace')
    print(out)
    if err.strip():
        print('--- stderr ---')
        print(err)
    ssh.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
