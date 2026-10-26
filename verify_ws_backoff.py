"""Verify the WebSocket reconnect-backoff edits, locally and read-only.

For each affected page it:
  1. logs in as the right role (force_login inside a rolled-back transaction,
     so the database is left unchanged);
  2. asserts the page renders 200;
  3. asserts the new capped-backoff reconnect is present;
  4. asserts the old flat retry is gone;
  5. balance-checks the <script> block that holds the change - the exact
     failure mode of wrapping handlers into functions would be a stray brace.

    python verify_ws_backoff.py
"""
import os
import sys

TARGETS = [
    # label, url name, user_type, must_have, must_not_have
    ('finance_dashboard', 'betting:finance_dashboard', 'finance',
     ['scheduleReconnect', 'reconnectAttempts = 0'], ['setTimeout(connect, 2500)']),
    ('retail_dashboard', 'betting:retail_dashboard', 'retail_manager',
     ['scheduleReconnect', 'reconnectAttempts = 0'], ['setTimeout(connect, 2500)']),
    ('admin betticket', 'betting_admin:betting_betticket_changelist', 'admin',
     ['scheduleReconnect', 'reconnectAttempts = 0'], ['setTimeout(connectSocket, 3000)']),
    ('admin userwithdrawal', 'betting_admin:betting_userwithdrawal_changelist', 'admin',
     ['scheduleReconnect', 'reconnectAttempts = 0'], ['setTimeout(connectSocket, 3000)']),
    ('uip dashboard', 'uip:dashboard', 'admin',
     ['scheduleReconnect', 'socket.onclose = onClose;'], []),
]

PAIRS = {')': '(', ']': '[', '}': '{'}
REGEX_PREV = set('(,=:[!&|?{};+-*%<>~^')


def js_balance(js):
    """Return (ok, message). Skips strings, template literals, comments and
    regex literals, then checks that brackets balance."""
    stack = []
    i, n = 0, len(js)
    prev = ''
    while i < n:
        c = js[i]
        nxt = js[i + 1] if i + 1 < n else ''
        if c in '"\'':
            quote = c
            i += 1
            while i < n:
                if js[i] == '\\':
                    i += 2
                    continue
                if js[i] == quote:
                    break
                i += 1
            i += 1
            prev = quote
            continue
        if c == '`':
            i += 1
            while i < n:
                if js[i] == '\\':
                    i += 2
                    continue
                if js[i] == '`':
                    break
                i += 1
            i += 1
            prev = '`'
            continue
        if c == '/' and nxt == '/':
            while i < n and js[i] != '\n':
                i += 1
            continue
        if c == '/' and nxt == '*':
            i += 2
            while i + 1 < n and not (js[i] == '*' and js[i + 1] == '/'):
                i += 1
            i += 2
            continue
        if c == '/' and (prev == '' or prev in REGEX_PREV):
            i += 1
            in_class = False
            while i < n:
                if js[i] == '\\':
                    i += 2
                    continue
                if js[i] == '[':
                    in_class = True
                elif js[i] == ']':
                    in_class = False
                elif js[i] == '/' and not in_class:
                    break
                elif js[i] == '\n':
                    break
                i += 1
            i += 1
            prev = '/'
            continue
        if c in '([{':
            stack.append((c, i))
        elif c in PAIRS:
            if not stack or stack[-1][0] != PAIRS[c]:
                line = js.count('\n', 0, i) + 1
                return False, 'unbalanced %r near line %d' % (c, line)
            stack.pop()
        if not c.isspace():
            prev = c
        i += 1
    if stack:
        c, pos = stack[-1]
        line = js.count('\n', 0, pos) + 1
        return False, '%d unclosed %r, last opened near line %d' % (len(stack), c, line)
    return True, 'balanced'


def script_blocks(html, marker):
    """Every <script> body that mentions the marker."""
    blocks = []
    lower = html.lower()
    pos = 0
    while True:
        start = lower.find('<script', pos)
        if start == -1:
            break
        open_end = html.find('>', start)
        close = lower.find('</script>', open_end)
        if open_end == -1 or close == -1:
            break
        body = html[open_end + 1:close]
        if marker in body:
            blocks.append(body)
        pos = close + 1
    return blocks


def main():
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'poolbetting.settings')
    import django
    django.setup()

    from django.contrib.auth import get_user_model
    from django.db import transaction
    from django.test import Client, override_settings
    from django.urls import reverse, NoReverseMatch

    User = get_user_model()
    print('MODE = READ-ONLY (local; every login is rolled back)')

    results = []
    with override_settings(DEBUG=True, ALLOWED_HOSTS=['testserver', 'localhost', '127.0.0.1']):
        for label, url_name, utype, must_have, must_not_have in TARGETS:
            notes = []
            ok = True
            try:
                url = reverse(url_name)
            except NoReverseMatch as exc:
                results.append((label, url_name, 'REVERSE FAILED', str(exc)))
                print('')
                print('  %-22s REVERSE FAILED: %s' % (label, exc))
                continue

            qs = User.objects.filter(is_active=True, is_locked=False, user_type=utype)
            if utype == 'admin':
                qs = qs.filter(is_superuser=True)
            user = qs.order_by('pk').first()
            if user is None:
                results.append((label, url, 'NO USER', 'no active %s user' % utype))
                print('')
                print('  %-22s NO USER for user_type=%s' % (label, utype))
                continue

            body = ''
            status = None
            try:
                with transaction.atomic():
                    client = Client(raise_request_exception=False)
                    client.force_login(user)
                    resp = client.get(url, follow=True)
                    status = resp.status_code
                    body = resp.content.decode('utf-8', 'replace')
                    transaction.set_rollback(True)
            except Exception as exc:
                notes.append('%s: %s' % (type(exc).__name__, exc))

            if status != 200:
                ok = False
                notes.append('status is %s, expected 200' % status)
            for token in must_have:
                if token not in body:
                    ok = False
                    notes.append('missing %r' % token)
            for token in must_not_have:
                if token in body:
                    ok = False
                    notes.append('old flat retry still present: %r' % token)

            blocks = script_blocks(body, 'scheduleReconnect')
            if not blocks:
                ok = False
                notes.append('no script block containing scheduleReconnect')
            for idx, blk in enumerate(blocks):
                good, msg = js_balance(blk)
                if not good:
                    ok = False
                    notes.append('script block %d: %s' % (idx + 1, msg))

            results.append((label, url, '%d blocks' % len(blocks), '; '.join(notes) or 'ok'))
            print('')
            print('  %-22s %s' % (label, url))
            print('      user     : %s (user_type=%s)' % (user.username or '<none>', utype))
            print('      status   : %s' % status)
            print('      blocks   : %d containing scheduleReconnect' % len(blocks))
            for idx, blk in enumerate(blocks):
                good, msg = js_balance(blk)
                print('      balance  : block %d %s' % (idx + 1, msg))
            print('      notes    : %s' % ('; '.join(notes) if notes else 'ok'))
            print('      %s' % ('PASS' if ok else 'FAIL'))

    passed = sum(1 for r in results if r[3] == 'ok')
    print('')
    print('=' * 60)
    print('RESULT: %d/%d WS BACKOFF CHECKS PASS' % (passed, len(results)))
    for label, url, extra, note in results:
        if note != 'ok':
            print('  FAIL %-22s %s' % (label, note))
    return 0 if passed == len(results) else 1


if __name__ == '__main__':
    sys.exit(main())
