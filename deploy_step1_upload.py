"""STEP 1 ALL-IN-ONE: Upload deploy files via paramiko (ONE password prompt only).
- Uploads 7 source files + deploy_manifest + verify_sha.sh
- Writes ALL manifest/scripts with explicit \n line endings (no \r\n bash corruption)
- Runs verify_sha.sh on remote in one exec channel
- Reports PASS/FAIL clearly.
Usage: python deploy_step1_upload.py
Pw prompt: enter stakenaija@172.236.9.63 password ONCE.
"""
import os, sys, hashlib, paramiko, getpass

DEPLOY_ROOT = r'c:\Users\HP\Desktop\poolbetting-main'
HOST = '172.236.9.63'
USER = 'stakenaija'
STAGE = '/tmp/notify_deploy'
REMOTE_DEPLOY_LOG = f'{STAGE}/step1_upload.log'

SRC_FILES = [
    (os.path.join(DEPLOY_ROOT, 'betting', 'models.py'),                                'models.py'),
    (os.path.join(DEPLOY_ROOT, 'betting', 'forms.py'),                                 'forms.py'),
    (os.path.join(DEPLOY_ROOT, 'betting', 'views.py'),                                 'views.py'),
    (os.path.join(DEPLOY_ROOT, 'betting', 'urls.py'),                                  'urls.py'),
    (os.path.join(DEPLOY_ROOT, 'betting', 'admin.py'),                                 'admin.py'),
    (os.path.join(DEPLOY_ROOT, 'betting', 'templates', 'betting', 'super_agent_remapping.html'), 'super_agent_remapping.html'),
    (os.path.join(DEPLOY_ROOT, 'betting', 'migrations', '0109_superagenttransferlog.py'), '0109_superagenttransferlog.py'),
]

VERIFY_SCRIPT = r'''#!/bin/bash
set -u
cd /tmp/notify_deploy || { echo "[FAIL] cd /tmp/notify_deploy"; exit 87; }
ALL_OK=1
PASS=0
FAIL=0
printf '%-66s  %-40s  %s\n' 'REMOTE-SHA' 'NAME' 'STATUS'
echo '----------------------------------------------------------------------------------------------------'
while IFS= read -r line || [ -n "$line" ]; do
  line="${line%$'\r'}"
  [ -z "$line" ] && continue
  EXPECTED=$(echo "$line" | awk '{print $1}')
  NAME=$(echo "$line" | awk '{print $2}')
  if [ ! -f "$NAME" ]; then
    printf '%-66s  %-40s  %s\n' "MISSING_FILE" "$NAME" "FAIL (not in stage dir)"
    ALL_OK=0; FAIL=$((FAIL+1))
    continue
  fi
  REMOTE=$(sha256sum "$NAME" | awk '{print $1}')
  if [ "$REMOTE" = "$EXPECTED" ]; then
    printf '%-66s  %-40s  %s\n' "$REMOTE" "$NAME" "OK"
    PASS=$((PASS+1))
  else
    printf '%-66s  %-40s  %s (expected=%s)\n' "$REMOTE" "$NAME" "MISMATCH" "$EXPECTED"
    ALL_OK=0; FAIL=$((FAIL+1))
  fi
done < deploy_manifest_latest.txt
echo '----------------------------------------------------------------------------------------------------'
echo "SHA verify: PASS=$PASS FAIL=$FAIL"
if [ $ALL_OK -eq 1 ] && [ $FAIL -eq 0 ]; then
  echo "[SHA GATE PASS $PASS/$PASS] Ready for STEP 2 SSH gates."
  exit 0
else
  echo "[SHA GATE FAIL $FAIL/$((PASS+FAIL)) ] Files corrupted in transit. RE-RUN STEP 1 SCP."
  exit 88
fi
'''

def sha256_of(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1024*1024), b''):
            h.update(chunk)
    return h.hexdigest()

def main():
    print(f"=== STEP 1 Deploy upload via paramiko (ONE auth) to {USER}@{HOST} ===")
    # Local py_compile pass 1a FIRST, fail fast local
    print("--- [GATE 1a] LOCAL py_compile RC=0 all .py files ---")
    import subprocess
    for (src, dst) in SRC_FILES:
        if src.endswith('.py'):
            r = subprocess.run([sys.executable, '-m', 'py_compile', src], capture_output=True, text=True, cwd=DEPLOY_ROOT)
            if r.returncode != 0:
                print(f"  FAIL  {dst}: {r.stderr}"); sys.exit(11)
            print(f"  PASS  {dst}")

    # Local shas 1b
    print("\n--- [GATE 1b] LOCAL SHA256 manifest ---")
    manifest_lines = []
    for (src, dst) in SRC_FILES:
        s = sha256_of(src)
        print(f"  {dst:40s}  {s}")
        manifest_lines.append(f"{s}  {dst}\n")
    manifest_bytes = ''.join(manifest_lines).encode('ascii')

    # Connect ONCE
    pw = getpass.getpass(f"\nEnter SSH password for {USER}@{HOST} (will prompt ONLY THIS ONE TIME): ")
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        ssh.connect(HOST, username=USER, password=pw, timeout=30, allow_agent=False, look_for_keys=False)
    except Exception as e:
        print(f"SSH connect FAIL: {e}"); sys.exit(12)
    print("Connected OK.")
    sftp = ssh.open_sftp()

    # mkdir stage
    try:
        sftp.mkdir(STAGE)
    except OSError:
        pass  # already exists

    log_lines = []
    def log(msg):
        print(msg); log_lines.append(msg + "\n")

    # Upload all source files 1c (BINARY mode preserve bytes exact)
    log("\n--- [GATE 1c] SFTP upload source files (BINARY mode, exact bytes) ---")
    for (src, dst) in SRC_FILES:
        remote = f"{STAGE}/{dst}"
        sftp.put(src, remote)
        size = sftp.stat(remote).st_size
        assert os.path.getsize(src) == size, f"size mismatch src={os.path.getsize(src)} remote={size} for {dst}"
        log(f"  UPLOAD OK  {dst} ({size} bytes)")

    # Upload manifest (EXPLICIT unix line endings - no CR)
    with sftp.file(f"{STAGE}/deploy_manifest_latest.txt", 'wb') as f:
        f.write(manifest_bytes)
    log("  UPLOAD deploy_manifest_latest.txt (unix LFs)")

    # Upload verify_sha.sh (EXPLICIT unix line endings)
    verify_bytes = VERIFY_SCRIPT.replace('\r\n', '\n').encode('ascii')
    with sftp.file(f"{STAGE}/verify_sha.sh", 'wb') as f:
        f.write(verify_bytes)
    # chmod +x
    sftp.chmod(f"{STAGE}/verify_sha.sh", 0o755)
    log("  UPLOAD verify_sha.sh (unix LFs, chmod +x)")

    # Write log so far to remote for audit trail
    with sftp.file(REMOTE_DEPLOY_LOG, 'wb') as f:
        f.write(''.join(log_lines).replace('\r\n', '\n').encode('utf-8'))

    # Run verification in one exec channel
    log("\n--- [GATE 1d] REMOTE SHA256 VERIFY via uploaded script ---")
    stdin, stdout, stderr = ssh.exec_command(f"bash {STAGE}/verify_sha.sh")
    out = stdout.read().decode('utf-8', errors='replace')
    err = stderr.read().decode('utf-8', errors='replace')
    rc = stdout.channel.recv_exit_status()
    print(out)
    if err.strip():
        print(f"[verify_stderr]\n{err}")

    # Append to deploy log remote
    with sftp.file(REMOTE_DEPLOY_LOG, 'ab') as f:
        f.write(('\n\n=== verify_sha.sh rc=' + str(rc) + ' stdout ===\n' + out + '\n=== stderr ===\n' + err + '\n').encode('utf-8'))

    sftp.close(); ssh.close()

    if rc == 0:
        print("\n🎉 STEP 1/2 COMPLETE — ALL SHA MATCH, CODEFILES STAGED.")
        print(f"Full log on remote: {REMOTE_DEPLOY_LOG}")
        print("Now SWITCH TO YOUR LIVE SSH TERMINAL (stakenaija@172.236.9.63) and paste STEP 2 bash zero-impact gate deploy script.")
    else:
        print(f"\n❌ STEP 1 FAIL RC={rc} — verify report above. Re-run this script.")
        sys.exit(rc)

if __name__ == '__main__':
    main()
