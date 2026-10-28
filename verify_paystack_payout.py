"""Verification for the Paystack payout (bank transfer) flow. READ-ONLY by default.

PART A - offline (always runs)
    Every Paystack HTTP call is faked, and every database write happens inside a
    transaction that is rolled back at the end. No transfer is sent, no row is
    changed. Proves: feature flag, bank-name matching, kobo conversion, the
    name-mismatch guard, idempotency, and the success / failure / OTP paths.

PART B - live (only with --live)
    Uses the real Paystack keys in .env (sk_test_... on the local server) to fetch
    the live bank list and check that bank-name matching works against real data.
    It calls NO transfer endpoint, so no money can move.

Usage:
    python verify_paystack_payout.py
    python verify_paystack_payout.py --live
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'poolbetting.settings')

import django  # noqa: E402

django.setup()

from decimal import Decimal  # noqa: E402

from django.conf import settings  # noqa: E402
from django.core.cache import cache  # noqa: E402
from django.db import transaction  # noqa: E402

from betting.models import ActivityLog, UserWithdrawal  # noqa: E402
from betting.services import paystack_payout as payout  # noqa: E402

LIVE = '--live' in sys.argv

PASS = []
FAIL = []
CALLS = []


def check(name, condition, detail=''):
    if condition:
        PASS.append(name)
        print('  PASS  %s' % name)
    else:
        FAIL.append(name)
        print('  FAIL  %s   %s' % (name, detail))


def section(title):
    print('')
    print('--- %s ---' % title)


# ---------------------------------------------------------------------------
# fake transport
# ---------------------------------------------------------------------------

FAKE_BANKS = [
    {'name': 'Access Bank', 'code': '044', 'slug': 'access-bank'},
    {'name': 'First Bank of Nigeria', 'code': '011', 'slug': 'first-bank-of-nigeria'},
    {'name': 'Guaranty Trust Bank', 'code': '058', 'slug': 'guaranty-trust-bank'},
    {'name': 'United Bank For Africa', 'code': '033', 'slug': 'united-bank-for-africa'},
    {'name': 'Zenith Bank', 'code': '057', 'slug': 'zenith-bank'},
]


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.ok = 200 <= status_code < 300

    def json(self):
        return self._payload


class FakeRequests:
    """Stands in for the `requests` module inside the payout service."""

    class RequestException(Exception):
        pass

    def __init__(self, mode='success'):
        self.mode = mode

    def request(self, method, url, **kwargs):
        path = url.replace(payout.API_BASE, '')
        body = json.loads(kwargs['data']) if kwargs.get('data') else None
        CALLS.append({'method': method, 'path': path, 'params': kwargs.get('params'), 'body': body})

        if path == '/bank':
            return FakeResponse({'status': True, 'data': FAKE_BANKS})

        if path == '/bank/resolve':
            return FakeResponse({'status': True, 'data': {
                'account_number': (kwargs.get('params') or {}).get('account_number'),
                'account_name': 'ADEBAYO TEST USER',
            }})

        if path == '/transferrecipient':
            return FakeResponse({'status': True, 'data': {'recipient_code': 'RCP_test_123'}})

        if path == '/transfer':
            if self.mode == 'otp':
                # Paystack's real OTP-gated shape is HTTP 200 with status=false
                # and data.status="otp". The verifier must mirror that exactly.
                return FakeResponse({
                    'status': False,
                    'message': 'Transfer requires OTP to continue',
                    'code': 'transfer_otp_required',
                    'data': {
                        'transfer_code': 'TRF_otp',
                        'status': 'otp',
                        'reference': body.get('reference'),
                    },
                })
            if self.mode == 'pending':
                return FakeResponse({'status': True, 'data': {
                    'transfer_code': 'TRF_pending', 'status': 'pending',
                }})
            if self.mode == 'rejected':
                return FakeResponse(
                    {'status': False, 'message': 'Insufficient balance', 'code': 'insufficient_balance'},
                    status_code=400,
                )
            return FakeResponse({'status': True, 'data': {
                'transfer_code': 'TRF_ok', 'status': 'success', 'reference': body.get('reference'),
            }})

        if path.startswith('/transfer/verify/'):
            return FakeResponse({'status': True, 'data': {
                'transfer_code': 'TRF_ok', 'status': 'success',
            }})

        return FakeResponse({'status': False, 'message': 'unexpected path %s' % path}, status_code=404)


class fake_transport:
    """Context manager swapping the module's `requests` for a fake."""

    def __init__(self, mode='success'):
        self.mode = mode
        self.original = None

    def __enter__(self):
        self.original = payout.requests
        payout.requests = FakeRequests(mode=self.mode)
        CALLS.clear()
        cache.clear()
        return self

    def __exit__(self, *exc):
        payout.requests = self.original
        cache.clear()
        return False


# ---------------------------------------------------------------------------
# part A
# ---------------------------------------------------------------------------

def run_offline(target):
    section('A1. pure helpers (no network, no database)')

    bank, score, _cands = payout.guess_bank('Zenith Bank', FAKE_BANKS)
    check('bank match: exact name', bank and bank['code'] == '057', 'got %r' % (bank,))

    bank, score, _cands = payout.guess_bank('GTBank', FAKE_BANKS)
    check('bank match: alias "GTBank" -> Guaranty Trust',
          bank and bank['code'] == '058', 'got %r score=%s' % (bank, score))

    bank, score, _cands = payout.guess_bank('zenith', FAKE_BANKS)
    check('bank match: lowercase short form "zenith"',
          bank and bank['code'] == '057', 'got %r score=%s' % (bank, score))

    bank, score, _cands = payout.guess_bank('UBA', FAKE_BANKS)
    check('bank match: alias "UBA" -> United Bank For Africa',
          bank and bank['code'] == '033', 'got %r score=%s' % (bank, score))

    bank, score, _cands = payout.guess_bank('Totally Made Up Bank Plc', FAKE_BANKS)
    check('bank match: nonsense name is NOT matched', bank is None, 'got %r' % (bank,))

    check('name equivalence: same name with different case/spacing',
          payout._names_equivalent('Adebayo  Test-User', 'ADEBAYO TEST USER') is True)
    check('name equivalence: genuinely different names rejected',
          payout._names_equivalent('Adebayo Test User', 'Chinedu Okonkwo') is False)

    section('A2. feature flag')

    original_flag = getattr(settings, 'PAYSTACK_PAYOUTS_ENABLED', None)
    settings.PAYSTACK_PAYOUTS_ENABLED = False
    check('flag off -> payouts_enabled() is False', payout.payouts_enabled() is False)
    try:
        payout.send_withdrawal_to_bank(target)
        check('flag off -> send refuses', False, 'no error raised')
    except payout.PaystackPayoutError as exc:
        check('flag off -> send refuses', 'switched off' in str(exc), str(exc))
    settings.PAYSTACK_PAYOUTS_ENABLED = True
    check('flag on -> payouts_enabled() is True', payout.payouts_enabled() is True)

    section('A3. amount conversion (naira -> kobo)')

    with fake_transport() as fake:
        payout.initiate_transfer(Decimal('1500.50'), 'RCP_x', 'test', 'REF-KOBO-1')
    body = CALLS[-1]['body']
    check('1500.50 naira becomes 150050 kobo',
          body and body['amount'] == 150050, 'got %r' % (body and body.get('amount'),))
    check('transfer uses source=balance', body.get('source') == 'balance')
    check('transfer carries the reference through',
          body.get('reference') == 'REF-KOBO-1', 'got %r' % body.get('reference'))

    with fake_transport():
        payout.initiate_transfer(Decimal('0.01'), 'RCP_x', 'test', 'REF-KOBO-2')
    check('0.01 naira becomes 1 kobo', CALLS[-1]['body']['amount'] == 1)

    try:
        with fake_transport():
            payout.initiate_transfer(Decimal('0'), 'RCP_x', 'test', 'REF-KOBO-3')
        check('zero amount is refused', False, 'no error raised')
    except payout.PaystackPayoutError:
        check('zero amount is refused', True)

    section('A4. preview is read-only')

    with fake_transport():
        preview = payout.preview_payout(target, banks=FAKE_BANKS)
    check('preview resolves the account name',
          preview['resolved_account_name'] == 'ADEBAYO TEST USER',
          'got %r' % preview['resolved_account_name'])
    check('preview reports a name match',
          preview['name_matches'] is True)
    check('preview writes nothing to Paystack (only /bank/resolve called)',
          [c['path'] for c in CALLS] == ['/bank/resolve'],
          'calls=%s' % [c['path'] for c in CALLS])
    check('preview never touches the transfer endpoint',
          all(c['path'] != '/transfer' for c in CALLS))
    # Paystack rejects the resolve request with HTTP 400 "bank_code is required"
    # if the bank code is sent under any other name. Pinning it here stops this
    # from silently regressing, because the fake transport accepts anything.
    resolve_params = [c.get('params') or {} for c in CALLS if c['path'] == '/bank/resolve']
    check('account resolve sends the parameter Paystack expects (bank_code, not bank)',
          bool(resolve_params)
          and all(p.get('bank_code') for p in resolve_params)
          and all('bank' not in p for p in resolve_params),
          'params=%s' % resolve_params)
    check('preview leaves payout_status empty',
          UserWithdrawal.objects.get(pk=target.pk).payout_status == '',
          'payout_status=%r' % UserWithdrawal.objects.get(pk=target.pk).payout_status)

    section('A5. successful send')

    with fake_transport() as fake:
        result = payout.send_withdrawal_to_bank(target, bank_code='058')

    after = UserWithdrawal.objects.get(pk=target.pk)
    check('success path reports ok', result['ok'] is True, result.get('message'))
    check('withdrawal marked completed', after.status == 'completed', 'status=%r' % after.status)
    check('payout_status is success', after.payout_status == 'success', 'payout_status=%r' % after.payout_status)
    check('paid_at is stamped', after.paid_at is not None)
    check('payout_reference was recorded', bool(after.payout_reference))
    check('reference format is WD-<id>-A<n>',
          after.payout_reference == 'WD-%s-A1' % target.pk, 'got %r' % after.payout_reference)
    check('recipient code was cached', after.payout_recipient_code == 'RCP_test_123')
    check('/transfer was called exactly once',
          [c['path'] for c in CALLS].count('/transfer') == 1,
          'calls=%s' % [c['path'] for c in CALLS])
    check('kobo amount matches the withdrawal amount',
          CALLS[-1]['body']['amount'] == int(Decimal(str(target.amount)) * 100))
    check('successful payout writes an audit row (no silent state change)',
          ActivityLog.objects.filter(
              affected_object='Withdrawal: %s' % target.pk,
              action__startswith='Paystack payout SUCCESS').exists())
    check('payout attempt also left a STARTED audit row',
          ActivityLog.objects.filter(
              affected_object='Withdrawal: %s' % target.pk,
              action__startswith='Paystack payout STARTED').exists())

    section('A6. idempotency - a second send must be impossible')

    try:
        with fake_transport():
            payout.send_withdrawal_to_bank(target, bank_code='058')
        check('second send is refused', False, 'no error raised')
    except payout.PaystackPayoutError as exc:
        check('second send is refused', True, str(exc))

    # Rewind to approved but keep the reference, to model an admin retrying an
    # ambiguous failure. The reference must not be allowed to silently re-send.
    UserWithdrawal.objects.filter(pk=target.pk).update(
        status='approved', payout_status='processing',
    )
    try:
        with fake_transport():
            payout.send_withdrawal_to_bank(target, bank_code='058')
        check('send while processing is refused', False, 'no error raised')
    except payout.PaystackPayoutError as exc:
        check('send while processing is refused', 'already in progress' in str(exc), str(exc))

    section('A7. failure holds for a human (no silent retry)')

    UserWithdrawal.objects.filter(pk=target.pk).update(
        status='approved', payout_status='', payout_reference=None,
        payout_attempts=0, payout_recipient_code='', payout_error='', paid_at=None,
    )
    target.refresh_from_db()

    with fake_transport(mode='rejected'):
        result = payout.send_withdrawal_to_bank(target, bank_code='058')

    after = UserWithdrawal.objects.get(pk=target.pk)
    check('rejected transfer reports not ok', result['ok'] is False)
    check('rejected transfer is recorded as failed',
          after.payout_status == 'failed', 'payout_status=%r' % after.payout_status)
    check('error message is stored for the admin', 'Insufficient balance' in after.payout_error,
          after.payout_error)
    check('withdrawal stays approved (not completed)',
          after.status == 'approved', 'status=%r' % after.status)
    check('reference is kept so it can be verified later', bool(after.payout_reference))
    check('nothing was marked paid', after.paid_at is None)

    section('A8. pending / OTP outcomes')

    UserWithdrawal.objects.filter(pk=target.pk).update(
        status='approved', payout_status='', payout_reference=None,
        payout_attempts=0, payout_recipient_code='', payout_error='', paid_at=None,
    )
    with fake_transport(mode='pending'):
        result = payout.send_withdrawal_to_bank(target, bank_code='058')
    after = UserWithdrawal.objects.get(pk=target.pk)
    check('pending transfer is left as processing',
          after.payout_status == 'processing', 'payout_status=%r' % after.payout_status)
    check('pending transfer is NOT marked completed',
          after.status == 'approved', 'status=%r' % after.status)
    check('pending message tells the admin to refresh later',
          'Refresh payout status' in result['message'], result['message'])

    UserWithdrawal.objects.filter(pk=target.pk).update(
        status='approved', payout_status='', payout_reference=None,
        payout_attempts=0, payout_recipient_code='', payout_error='',
    )
    with fake_transport(mode='otp'):
        result = payout.send_withdrawal_to_bank(target, bank_code='058')
    after = UserWithdrawal.objects.get(pk=target.pk)
    check('OTP outcome stays processing (not failed)',
          after.payout_status == 'processing', 'payout_status=%r' % after.payout_status)
    check('OTP outcome keeps the reference so it can be refreshed',
          bool(after.payout_reference), 'reference=%r' % after.payout_reference)
    check('OTP outcome is NOT marked completed',
          after.status == 'approved', 'status=%r' % after.status)
    check('OTP outcome is NOT marked paid', after.paid_at is None)
    check('OTP message tells the admin to complete the OTP then refresh',
          'otp' in result['message'].lower()
          and 'Refresh payout status' in result['message'], result['message'])
    check('OTP outcome writes an awaiting-OTP audit row',
          ActivityLog.objects.filter(
              affected_object='Withdrawal: %s' % target.pk,
              action__startswith='Paystack payout awaiting OTP').exists())

    # A refresh after the OTP has been entered must settle the payout.
    with fake_transport():
        refreshed = payout.refresh_payout_status(target)
    after = UserWithdrawal.objects.get(pk=target.pk)
    check('refresh after OTP marks the payout completed',
          after.status == 'completed' and after.payout_status == 'success',
          'status=%r payout_status=%r' % (after.status, after.payout_status))
    check('refresh after OTP stamps paid_at', after.paid_at is not None)
    check('refresh after OTP reports success', refreshed.get('status') == 'success',
          'refreshed=%r' % refreshed.get('status'))

    section('A9. name-mismatch guard')

    UserWithdrawal.objects.filter(pk=target.pk).update(
        status='approved', payout_status='', payout_reference=None,
        payout_attempts=0, payout_recipient_code='', payout_error='',
        account_name='SOMEONE ELSE ENTIRELY',
    )
    target.refresh_from_db()

    try:
        with fake_transport():
            payout.send_withdrawal_to_bank(target, bank_code='058')
        check('mismatched name blocks the payout', False, 'no error raised')
    except payout.PaystackPayoutError as exc:
        check('mismatched name blocks the payout', 'belongs to' in str(exc), str(exc))

    after = UserWithdrawal.objects.get(pk=target.pk)
    check('blocked payout did not create a transfer',
          not after.payout_reference and after.payout_status == '',
          'reference=%r payout_status=%r' % (after.payout_reference, after.payout_status))

    with fake_transport():
        result = payout.send_withdrawal_to_bank(
            target, bank_code='058', acknowledge_name_mismatch=True,
        )
    check('explicit acknowledgement allows the payout',
          result['ok'] is True, result.get('message'))

    section('A10. refresh re-queries instead of re-sending')

    with fake_transport():
        result = payout.refresh_payout_status(target)
    check('refresh reports success',
          result['ok'] is True and result['status'] == 'success', result.get('message'))
    check('refresh called verify, not a new transfer',
          any(c['path'].startswith('/transfer/verify/') for c in CALLS)
          and all(c['path'] != '/transfer' for c in CALLS),
          'calls=%s' % [c['path'] for c in CALLS])

    settings.PAYSTACK_PAYOUTS_ENABLED = original_flag


# ---------------------------------------------------------------------------
# part B
# ---------------------------------------------------------------------------

def run_live():
    section('B1. live Paystack read-only probe (test keys on local)')

    key = (getattr(settings, 'PAYSTACK_SECRET_KEY', '') or '')
    mode = 'TEST' if '_test_' in key else ('LIVE' if '_live_' in key else 'UNKNOWN')
    print('  key mode      : %s' % mode)
    if mode == 'LIVE':
        print('  *** This server is using LIVE Paystack keys. Only the bank list is fetched. ***')

    check('payouts flag is on for this run', payout.payouts_enabled() is True,
          'set PAYSTACK_PAYOUTS_ENABLED=True in .env')

    cache.delete(payout.BANK_CACHE_KEY)
    try:
        banks = payout.fetch_banks()
    except payout.PaystackPayoutError as exc:
        check('live bank list fetched', False, str(exc))
        return

    check('live bank list fetched', len(banks) > 20, 'got %d banks' % len(banks))

    samples = ['Zenith Bank', 'GTBank', 'First Bank', 'UBA', 'Access Bank']
    for name in samples:
        bank, score, _c = payout.guess_bank(name, banks)
        check('live match %-12s -> %s' % (name, (bank or {}).get('name', 'NO MATCH')),
              bank is not None, 'score=%.2f' % score)

    bank, score, _c = payout.guess_bank('Totally Made Up Bank Plc', banks)
    check('live match rejects a nonsense bank name', bank is None, 'got %r' % (bank,))


# ---------------------------------------------------------------------------

def main():
    print('MODE = READ-ONLY (offline HTTP is faked; the database is rolled back)')
    print('LIVE PROBE = %s' % ('ON' if LIVE else 'off (pass --live to enable)'))

    target = UserWithdrawal.objects.filter(status='approved').order_by('-pk').first()
    if target is None:
        target = UserWithdrawal.objects.order_by('-pk').first()
    if target is None:
        print('ABORT: no UserWithdrawal rows exist, so the flow cannot be exercised.')
        return 1

    print('')
    print('target withdrawal: id=%s user=%s amount=%s status=%s'
          % (target.pk, target.user_id, target.amount, target.status))

    try:
        with transaction.atomic():
            # Put the target into a known state INSIDE the transaction only.
            UserWithdrawal.objects.filter(pk=target.pk).update(
                status='approved',
                bank_name='GTBank',
                account_name='ADEBAYO TEST USER',
                account_number='0123456789',
                bank_code='',
                payout_status='',
                payout_reference=None,
                payout_recipient_code='',
                payout_error='',
                payout_attempts=0,
                paid_at=None,
            )
            target.refresh_from_db()

            run_offline(target)

            transaction.set_rollback(True)
        print('')
        print('database changes rolled back (nothing was saved)')
    except Exception as exc:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        FAIL.append('unexpected error: %s' % exc)
        print('')
        print('database changes rolled back after the error')

    # Confirm the rollback really happened.
    fresh = UserWithdrawal.objects.get(pk=target.pk)
    before = original_state
    section('A11. rollback proof')
    check('withdrawal row is unchanged after the run',
          (fresh.status, fresh.payout_status, fresh.payout_reference) == before,
          'now=%r was=%r' % ((fresh.status, fresh.payout_status, fresh.payout_reference), before))

    if LIVE:
        run_live()

    print('')
    print('=' * 60)
    total = len(PASS) + len(FAIL)
    if FAIL:
        print('RESULT: %d/%d PASS  (%d FAILED)' % (len(PASS), total, len(FAIL)))
        for name in FAIL:
            print('  FAILED: %s' % name)
    else:
        print('RESULT: %d/%d PASS - ALL PAYSTACK PAYOUT CHECKS PASS' % (len(PASS), total))
    return 0


if __name__ == '__main__':
    # Captured before anything runs, for the rollback proof.
    _probe = UserWithdrawal.objects.filter(status='approved').order_by('-pk').first() \
        or UserWithdrawal.objects.order_by('-pk').first()
    original_state = (
        (_probe.status, _probe.payout_status, _probe.payout_reference) if _probe else None
    )
    main()
