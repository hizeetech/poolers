"""Deploy the local WebSocket reconnect-backoff template edits to live.

    python deploy_ws_backoff.py                 DRY RUN (writes nothing)
    python deploy_ws_backoff.py --apply         swap files + restart shop-sync
    python deploy_ws_backoff.py --apply --no-restart

WHY A RESTART IS REQUIRED
    These are Django template edits. Production uses the cached template loader,
    so shop-sync must restart before users receive the new JS.

SAFETY GATE
    For each file, the script:
      1. reads the live file and the local file;
      2. applies ONLY the intended old->new replacements in memory;
      3. refuses to write unless "live + intended edits == local", after
         normalising line endings.

    That means an unrelated live edit cannot be silently overwritten.
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
    print("paramiko missing. Run: pip install paramiko")
    sys.exit(1)

HOST = "172.236.9.63"
USER = "stakenaija"
SERVICE = "shop-sync"
BACKUP_DIR = "/home/stakenaija/code_backup_%s"
MAX_DIFF_LINES = 60


FILES = [
    {
        "label": "finance dashboard",
        "local": r"c:\Users\HP\Desktop\poolbetting-main\betting\templates\betting\finance_dashboard.html",
        "remote": "/var/www/shop/betting/templates/betting/finance_dashboard.html",
        "markers": ["scheduleReconnect", "reconnectAttempts = 0"],
        "replacements": [
            (
                "    const wsUrl = `${protocol}://${window.location.host}/ws/notifications/`;\n"
                "    let socket;\n"
                "    const connect = () => {\n"
                "      socket = new WebSocket(wsUrl);\n"
                "      socket.onmessage = (ev) => {\n",
                "    const wsUrl = `${protocol}://${window.location.host}/ws/notifications/`;\n"
                "    let socket;\n"
                "    let reconnectTimer = null;\n"
                "    let reconnectAttempts = 0;\n"
                "\n"
                "    // Capped exponential backoff with jitter: a flat retry made every open\n"
                "    // dashboard reconnect on the same beat after a server restart.\n"
                "    const scheduleReconnect = () => {\n"
                "      if (reconnectTimer) return;\n"
                "      const base = Math.min(30000, 2000 * Math.pow(2, reconnectAttempts));\n"
                "      reconnectAttempts += 1;\n"
                "      reconnectTimer = window.setTimeout(() => {\n"
                "        reconnectTimer = null;\n"
                "        connect();\n"
                "      }, base + Math.floor(Math.random() * 1000));\n"
                "    };\n"
                "\n"
                "    const connect = () => {\n"
                "      socket = new WebSocket(wsUrl);\n"
                "      socket.onopen = () => {\n"
                "        reconnectAttempts = 0;\n"
                "      };\n"
                "      socket.onmessage = (ev) => {\n",
            ),
            (
                "      socket.onclose = () => setTimeout(connect, 2500);\n",
                "      socket.onclose = () => scheduleReconnect();\n",
            ),
        ],
    },
    {
        "label": "retail dashboard",
        "local": r"c:\Users\HP\Desktop\poolbetting-main\betting\templates\betting\retail_dashboard.html",
        "remote": "/var/www/shop/betting/templates/betting/retail_dashboard.html",
        "markers": ["scheduleReconnect", "reconnectAttempts = 0"],
        "replacements": [
            (
                "    const wsUrl = `${protocol}://${window.location.host}/ws/notifications/`;\n"
                "    let socket;\n"
                "\n"
                "    const connect = () => {\n"
                "      socket = new WebSocket(wsUrl);\n"
                "      socket.onmessage = (ev) => {\n",
                "    const wsUrl = `${protocol}://${window.location.host}/ws/notifications/`;\n"
                "    let socket;\n"
                "    let reconnectTimer = null;\n"
                "    let reconnectAttempts = 0;\n"
                "\n"
                "    // Capped exponential backoff with jitter: a flat retry made every open\n"
                "    // dashboard reconnect on the same beat after a server restart.\n"
                "    const scheduleReconnect = () => {\n"
                "      if (reconnectTimer) return;\n"
                "      const base = Math.min(30000, 2000 * Math.pow(2, reconnectAttempts));\n"
                "      reconnectAttempts += 1;\n"
                "      reconnectTimer = window.setTimeout(() => {\n"
                "        reconnectTimer = null;\n"
                "        connect();\n"
                "      }, base + Math.floor(Math.random() * 1000));\n"
                "    };\n"
                "\n"
                "    const connect = () => {\n"
                "      socket = new WebSocket(wsUrl);\n"
                "      socket.onopen = () => {\n"
                "        reconnectAttempts = 0;\n"
                "      };\n"
                "      socket.onmessage = (ev) => {\n",
            ),
            (
                "      socket.onclose = () => {\n"
                "        setTimeout(connect, 2500);\n"
                "      };\n",
                "      socket.onclose = () => {\n"
                "        scheduleReconnect();\n"
                "      };\n",
            ),
        ],
    },
    {
        "label": "admin betticket changelist",
        "local": r"c:\Users\HP\Desktop\poolbetting-main\betting\templates\betting\admin\betticket_change_list.html",
        "remote": "/var/www/shop/betting/templates/betting/admin/betticket_change_list.html",
        "markers": ["scheduleReconnect", "reconnectAttempts = 0"],
        "replacements": [
            (
                "    let socket = null;\n"
                "    let reconnectTimer = null;\n",
                "    let socket = null;\n"
                "    let reconnectTimer = null;\n"
                "    let reconnectAttempts = 0;\n",
            ),
            (
                "    function connectSocket() {\n"
                "        if (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) {\n"
                "            return;\n"
                "        }\n",
                "    // Capped exponential backoff with jitter: a flat 3s retry made every open\n"
                "    // admin list reconnect on the same beat after a server restart.\n"
                "    function scheduleReconnect() {\n"
                "        if (reconnectTimer) return;\n"
                "        const base = Math.min(30000, 2000 * Math.pow(2, reconnectAttempts));\n"
                "        reconnectAttempts += 1;\n"
                "        reconnectTimer = window.setTimeout(function () {\n"
                "            reconnectTimer = null;\n"
                "            connectSocket();\n"
                "        }, base + Math.floor(Math.random() * 1000));\n"
                "    }\n"
                "\n"
                "    function connectSocket() {\n"
                "        if (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) {\n"
                "            return;\n"
                "        }\n",
            ),
            (
                "        socket.addEventListener('open', function () {\n"
                "            body.dataset.liveRefreshTransport = 'websocket';\n"
                "            if (reconnectTimer) {\n",
                "        socket.addEventListener('open', function () {\n"
                "            body.dataset.liveRefreshTransport = 'websocket';\n"
                "            reconnectAttempts = 0;\n"
                "            if (reconnectTimer) {\n",
            ),
            (
                "        socket.addEventListener('close', function () {\n"
                "            body.dataset.liveRefreshTransport = 'disconnected';\n"
                "            reconnectTimer = window.setTimeout(connectSocket, 3000);\n"
                "        });\n",
                "        socket.addEventListener('close', function () {\n"
                "            body.dataset.liveRefreshTransport = 'disconnected';\n"
                "            scheduleReconnect();\n"
                "        });\n",
            ),
        ],
    },
    {
        "label": "admin userwithdrawal changelist",
        "local": r"c:\Users\HP\Desktop\poolbetting-main\betting\templates\betting\admin\userwithdrawal_change_list.html",
        "remote": "/var/www/shop/betting/templates/betting/admin/userwithdrawal_change_list.html",
        "markers": ["scheduleReconnect", "reconnectAttempts = 0"],
        "replacements": [
            (
                "    let socket = null;\n"
                "    let reconnectTimer = null;\n"
                "    let pollTimer = null;\n",
                "    let socket = null;\n"
                "    let reconnectTimer = null;\n"
                "    let reconnectAttempts = 0;\n"
                "    let pollTimer = null;\n",
            ),
            (
                "    function connectSocket() {\n"
                "        if (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) {\n"
                "            return;\n"
                "        }\n",
                "    // Capped exponential backoff with jitter: a flat 3s retry made every open\n"
                "    // admin list reconnect on the same beat after a server restart.\n"
                "    function scheduleReconnect() {\n"
                "        if (reconnectTimer) return;\n"
                "        const base = Math.min(30000, 2000 * Math.pow(2, reconnectAttempts));\n"
                "        reconnectAttempts += 1;\n"
                "        reconnectTimer = window.setTimeout(function () {\n"
                "            reconnectTimer = null;\n"
                "            connectSocket();\n"
                "        }, base + Math.floor(Math.random() * 1000));\n"
                "    }\n"
                "\n"
                "    function connectSocket() {\n"
                "        if (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) {\n"
                "            return;\n"
                "        }\n",
            ),
            (
                "        socket.addEventListener('open', function () {\n"
                "            body.dataset.liveRefreshTransport = 'websocket';\n"
                "            if (reconnectTimer) {\n",
                "        socket.addEventListener('open', function () {\n"
                "            body.dataset.liveRefreshTransport = 'websocket';\n"
                "            reconnectAttempts = 0;\n"
                "            if (reconnectTimer) {\n",
            ),
            (
                "        socket.addEventListener('close', function () {\n"
                "            body.dataset.liveRefreshTransport = 'disconnected';\n"
                "            reconnectTimer = window.setTimeout(connectSocket, 3000);\n"
                "        });\n",
                "        socket.addEventListener('close', function () {\n"
                "            body.dataset.liveRefreshTransport = 'disconnected';\n"
                "            scheduleReconnect();\n"
                "        });\n",
            ),
        ],
    },
    {
        "label": "uip dashboard",
        "local": r"c:\Users\HP\Desktop\poolbetting-main\uip\templates\uip\dashboard.html",
        "remote": "/var/www/shop/uip/templates/uip/dashboard.html",
        "markers": ["scheduleReconnect", "socket.onclose = onClose;"],
        "replacements": [
            (
                "        const protocol = window.location.protocol === 'https:' ? 'wss://' : 'ws://';\n"
                "        const socket = new WebSocket(protocol + window.location.host + '/ws/dashboard/');\n"
                "\n"
                "        socket.onopen = function(e) {\n"
                "            console.log('WebSocket Connected');\n"
                "            liveIndicator.classList.remove('text-muted', 'text-danger');\n"
                "            liveIndicator.classList.add('text-success');\n"
                "        };\n",
                "        const protocol = window.location.protocol === 'https:' ? 'wss://' : 'ws://';\n"
                "        let socket = null;\n"
                "        let reconnectTimer = null;\n"
                "        let reconnectAttempts = 0;\n"
                "\n"
                "        // Capped exponential backoff with jitter. This socket previously had no\n"
                "        // reconnect at all, so the live panel stayed dead after any restart.\n"
                "        const scheduleReconnect = function() {\n"
                "            if (reconnectTimer) return;\n"
                "            const base = Math.min(30000, 2000 * Math.pow(2, reconnectAttempts));\n"
                "            reconnectAttempts += 1;\n"
                "            reconnectTimer = window.setTimeout(function() {\n"
                "                reconnectTimer = null;\n"
                "                connect();\n"
                "            }, base + Math.floor(Math.random() * 1000));\n"
                "        };\n"
                "\n"
                "        function onOpen(e) {\n"
                "            reconnectAttempts = 0;\n"
                "            console.log('WebSocket Connected');\n"
                "            liveIndicator.classList.remove('text-muted', 'text-danger');\n"
                "            liveIndicator.classList.add('text-success');\n"
                "        }\n",
            ),
            (
                "        socket.onmessage = function(e) {\n",
                "        function onMessage(e) {\n",
            ),
            (
                "        };\n"
                "\n"
                "        socket.onclose = function(e) {\n"
                "            console.error('Chat socket closed unexpectedly');\n"
                "            liveIndicator.classList.remove('text-success');\n"
                "            liveIndicator.classList.add('text-danger');\n"
                "        };\n",
                "        }\n"
                "\n"
                "        function onClose(e) {\n"
                "            console.error('Chat socket closed unexpectedly');\n"
                "            liveIndicator.classList.remove('text-success');\n"
                "            liveIndicator.classList.add('text-danger');\n"
                "            scheduleReconnect();\n"
                "        }\n"
                "\n"
                "        const connect = function() {\n"
                "            if (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) {\n"
                "                return;\n"
                "            }\n"
                "            socket = new WebSocket(protocol + window.location.host + '/ws/dashboard/');\n"
                "            socket.onopen = onOpen;\n"
                "            socket.onmessage = onMessage;\n"
                "            socket.onclose = onClose;\n"
                "        };\n"
                "        connect();\n",
            ),
        ],
    },
]


def sha(data):
    return hashlib.sha256(data).hexdigest()


def norm(text):
    return text.replace("\r\n", "\n").replace("\r", "\n")


def read_remote(sftp, path):
    with sftp.open(path, "rb") as fh:
        data = fh.read()
    return data, sftp.stat(path)


def print_diff(from_text, to_text, from_name, to_name):
    diff = list(
        difflib.unified_diff(
            from_text.splitlines(),
            to_text.splitlines(),
            fromfile=from_name,
            tofile=to_name,
            lineterm="",
            n=2,
        )
    )
    print("  --- diff (first %d lines) ---" % MAX_DIFF_LINES)
    for line in diff[:MAX_DIFF_LINES]:
        print("  " + line)
    if len(diff) > MAX_DIFF_LINES:
        print("  ... (%d more diff lines)" % (len(diff) - MAX_DIFF_LINES))


def apply_replacements(text, replacements):
    out = text
    used = 0
    for old, new in replacements:
        if old not in out:
            return None, used
        out = out.replace(old, new, 1)
        used += 1
    return out, used


def restart(ssh):
    print("")
    print("=== restarting %s ===" % SERVICE)
    sudo_pw = getpass.getpass("sudo password for %s@%s: " % (USER, HOST))
    cmd = (
        'sudo -S -p "" systemctl restart {svc}; '
        'echo "is-active: $(systemctl is-active {svc})"; '
        'main=$(systemctl show {svc} -p MainPID --value); '
        'echo "MainPID: $main"; '
        'if [ -n "$main" ] && [ "$main" != "0" ]; then '
        '  echo "workers: $(ps -o pid= --ppid $main 2>/dev/null | wc -l)"; fi'
    ).format(svc=SERVICE)
    stdin, stdout, _ = ssh.exec_command(cmd, timeout=180)
    stdin.write(sudo_pw + "\n")
    stdin.flush()
    print(stdout.read().decode("utf-8", "replace").rstrip())


def main():
    args = sys.argv[1:]
    apply_mode = "--apply" in args
    do_restart = "--no-restart" not in args
    print("MODE = %s" % ("APPLY" if apply_mode else "DRY RUN (nothing written)"))

    local_info = []
    for item in FILES:
        if not os.path.isfile(item["local"]):
            print("ABORT: local file not found: %s" % item["local"])
            return 2
        lb = open(item["local"], "rb").read()
        ltxt = norm(lb.decode("utf-8"))
        missing = [m for m in item["markers"] if m not in ltxt]
        print("")
        print("=== local source of truth: %s ===" % item["label"])
        print("  %s" % item["local"])
        print("  %d bytes  sha256=%s" % (len(lb), sha(lb)[:16]))
        for marker in item["markers"]:
            print("  %-36s %s" % (marker, "PRESENT" if marker in ltxt else "MISSING"))
        if missing:
            print("ABORT: local file is missing %s" % missing)
            return 2
        local_info.append({"bytes": lb, "text": ltxt})

    ssh_pw = getpass.getpass("%s@%s password: " % (USER, HOST))
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(HOST, username=USER, password=ssh_pw, timeout=30, banner_timeout=30)
    print("SSH connected to %s" % HOST)
    sftp = ssh.open_sftp()

    plans = []
    try:
        for idx, item in enumerate(FILES):
            rb, st = read_remote(sftp, item["remote"])
            rtxt = norm(rb.decode("utf-8"))
            ltxt = local_info[idx]["text"]
            candidate = None
            replacement_count = 0

            if rtxt == ltxt:
                state = "UP_TO_DATE"
            else:
                candidate, replacement_count = apply_replacements(rtxt, item["replacements"])
                if candidate == ltxt:
                    state = "UPDATE"
                else:
                    state = "ABORT"

            plans.append(
                {
                    "item": item,
                    "local_bytes": local_info[idx]["bytes"],
                    "local_text": ltxt,
                    "remote_bytes": rb,
                    "remote_text": rtxt,
                    "remote_mode": st.st_mode & 0o777,
                    "remote_mtime": st.st_mtime,
                    "candidate": candidate,
                    "use_crlf": b"\r\n" in rb,
                    "state": state,
                    "replacement_count": replacement_count,
                }
            )

        print("")
        print("=== live state ===")
        for plan in plans:
            item = plan["item"]
            print("  %s" % item["remote"])
            print(
                "      %d bytes  mode=%o  sha256=%s  mtime=%s"
                % (
                    len(plan["remote_bytes"]),
                    plan["remote_mode"],
                    sha(plan["remote_bytes"])[:16],
                    time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(plan["remote_mtime"])),
                )
            )
            print(
                "      local match: %s   planned action: %s   replacements: %d"
                % (
                    plan["remote_text"] == plan["local_text"],
                    plan["state"],
                    plan["replacement_count"],
                )
            )

        bad = [p for p in plans if p["state"] == "ABORT"]
        if bad:
            print("")
            print("=== gate failure ===")
            for plan in bad:
                print("  ABORT %s" % plan["item"]["remote"])
                print("  applying the intended edits to live does NOT reproduce the local file.")
                print_diff(
                    plan["candidate"] or plan["remote_text"],
                    plan["local_text"],
                    "LIVE + intended edits",
                    "LOCAL",
                )
                print("")
            print("Nothing was written.")
            sftp.close()
            ssh.close()
            return 2

        updates = [p for p in plans if p["state"] == "UPDATE"]
        print("")
        print("=== plan ===")
        if updates:
            for plan in updates:
                print("  UPDATE %s" % plan["item"]["remote"])
            print("  BACKUP  -> %s/" % (BACKUP_DIR % "<ts>"))
            if do_restart:
                print("  RESTART -> systemctl restart %s" % SERVICE)
            else:
                print("  RESTART -> skipped (--no-restart): templates stay stale until restart")
        else:
            print("  Nothing to do - every live file already matches local.")

        if not apply_mode:
            print("")
            print("DRY RUN - nothing was written. Re-run with --apply to commit.")
            sftp.close()
            ssh.close()
            return 0

        if not updates:
            print("")
            print("Nothing to do - every live file already matches local.")
            sftp.close()
            ssh.close()
            return 0

        ts = time.strftime("%Y%m%d_%H%M%S")
        bdir = BACKUP_DIR % ts
        ssh.exec_command("mkdir -p %s" % bdir)
        time.sleep(1)

        print("")
        print("=== APPLY ===")
        for plan in updates:
            remote = plan["item"]["remote"]
            backup = "%s/%s.%s" % (bdir, os.path.basename(remote), sha(plan["remote_bytes"])[:8])
            with sftp.open(backup, "wb") as fh:
                fh.write(plan["remote_bytes"])
            print("  BACKUP  %s  (%d bytes)" % (backup, len(plan["remote_bytes"])))

            payload = (
                plan["local_text"].replace("\n", "\r\n").encode("utf-8")
                if plan["use_crlf"]
                else plan["local_text"].encode("utf-8")
            )
            tmp = remote + ".pbnew"
            with sftp.open(tmp, "wb") as fh:
                fh.write(payload)
            sftp.chmod(tmp, plan["remote_mode"])
            sftp.posix_rename(tmp, remote)
            print("  WRITTEN %s" % remote)

        print("")
        print("=== verify files ===")
        for plan in updates:
            rb2, st2 = read_remote(sftp, plan["item"]["remote"])
            ok = norm(rb2.decode("utf-8")) == plan["local_text"]
            print("  %s" % plan["item"]["remote"])
            print("      %d bytes  mode=%o  sha256=%s" % (len(rb2), st2.st_mode & 0o777, sha(rb2)[:16]))
            print("      CONTENT MATCHES LOCAL: %s" % ok)
            if not ok:
                print("")
                print("*** VERIFICATION FAILED - restore from %s" % bdir)
                sftp.close()
                ssh.close()
                return 1

        if do_restart:
            restart(ssh)
        else:
            print("")
            print("NOTE: no restart was performed, so users still have the old cached templates.")

        sftp.close()
        ssh.close()
        print("")
        print("RESULT: %s" % ("DEPLOYED AND RESTARTED" if do_restart else "FILES SWAPPED (not live until restarted)"))
        print("Backup: %s" % bdir)
        return 0
    except Exception:
        try:
            sftp.close()
        except Exception:
            pass
        try:
            ssh.close()
        except Exception:
            pass
        raise


if __name__ == "__main__":
    sys.exit(main())
