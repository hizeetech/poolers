"""Paystack payout (bank transfer) service for approved withdrawals.

SAFETY MODEL
------------
* Feature-flagged. ``PAYSTACK_PAYOUTS_ENABLED`` defaults to False, so importing
  this module can never move money on its own.
* Human-triggered, one withdrawal at a time, from the Django admin. There is no
  bulk path and nothing is triggered automatically by a status change.
* Idempotent. Each attempt claims a unique ``payout_reference`` and saves it
  BEFORE calling Paystack, so an ambiguous network failure can be re-checked
  instead of blindly re-sent. Paystack also rejects duplicate references.
* Failure holds for a human. Nothing is auto-retried. The wallet ledger is never
  touched here: funds were already deducted when the user submitted the request,
  so a failed payout simply leaves the withdrawal in "approved" with
  ``payout_status="failed"`` for an admin to review.
* Bank codes are never hardcoded. They are read live from Paystack's bank list,
  so they cannot go stale. The free-text bank name users type is only used to
  pre-select a suggestion; the admin confirms the final choice.
"""
import difflib
import json
import logging
import re
from decimal import Decimal, ROUND_HALF_UP

import requests
from django.conf import settings
from django.core.cache import cache
from django.db import transaction as db_transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

API_BASE = 'https://api.paystack.co'
BANK_CACHE_KEY = 'pb:paystack:banks:NGN'
BANK_CACHE_SECONDS = 60 * 60 * 24

# User text -> the words that actually appear in Paystack's bank list. This only
# expands a search term; it never supplies a bank code.
_BANK_ALIASES = {
    'gtb': 'guaranty trust bank',
    'gt bank': 'guaranty trust bank',
    'gtbank': 'guaranty trust bank',
    'guaranty': 'guaranty trust bank',
    'firstbank': 'first bank of nigeria',
    'first bank': 'first bank of nigeria',
    'uba': 'united bank for africa',
    'united bank': 'united bank for africa',
    'fcmb': 'first city monument bank',
    'stanbic': 'stanbic ibtc bank',
    'stanbic ibtc': 'stanbic ibtc bank',
    'mtn momo': 'momopsp',
    'momo': 'momopsp',
    'opay': 'paycom',
    'palmpay': 'palm pay',
    'moniepoint': 'moniepoint mfb',
    'alat': 'wema bank',
    'kuda': 'kuda mfb',
    'heritage': 'heritage bank',
    'union': 'union bank of nigeria',
    'polaris': 'polaris bank',
    'keystone': 'keystone bank',
    'providus': 'providus bank',
    'sparkle': 'sparkle microfinance',
    'carbon': 'carbon',
    'fairmoney': 'fairmoney microfinance',
}

_DROP_WORDS = {
    'bank', 'plc', 'ltd', 'limited', 'nigeria', 'nig', 'mfb',
    'microfinance', 'the', 'of', 'and', '&',
}


class PaystackPayoutError(Exception):
    """Any payout problem that should be surfaced to the admin."""

    def __init__(self, message, *, code=None, payload=None):
        super().__init__(message)
        self.code = code
        self.payload = payload or {}


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------

def payouts_enabled():
    """True only when the operator has explicitly switched payouts on."""
    return bool(getattr(settings, 'PAYSTACK_PAYOUTS_ENABLED', False))


def _secret_key():
    key = (getattr(settings, 'PAYSTACK_SECRET_KEY', '') or '').strip()
    if not key:
        raise PaystackPayoutError('PAYSTACK_SECRET_KEY is not configured on this server.')
    return key


def _timeout():
    try:
        return int(getattr(settings, 'PAYSTACK_TIMEOUT_SECONDS', 20))
    except (TypeError, ValueError):
        return 20


# --------------------------------------------------------------------------
# transport
# --------------------------------------------------------------------------

def _request(method, path, *, params=None, payload=None):
    """Single choke point for every Paystack call. Returns the ``data`` member."""
    headers = {
        'Authorization': 'Bearer ' + _secret_key(),
        'Content-Type': 'application/json',
        'Accept': 'application/json',
    }
    try:
        response = requests.request(
            method,
            API_BASE + path,
            headers=headers,
            params=params,
            data=json.dumps(payload) if payload is not None else None,
            timeout=_timeout(),
        )
    except requests.RequestException as exc:
        # Deliberately vague about success: we cannot know whether Paystack
        # accepted the request. Callers must treat this as "unknown", not "failed".
        raise PaystackPayoutError('Could not reach Paystack: %s' % exc) from exc

    try:
        body = response.json()
    except ValueError:
        raise PaystackPayoutError(
            'Paystack returned a non-JSON response (HTTP %s).' % response.status_code
        )

    if not response.ok or not body.get('status'):
        message = body.get('message') or ('Paystack error (HTTP %s)' % response.status_code)
        code = body.get('code') or body.get('type')
        raise PaystackPayoutError(message, code=code, payload=body)

    return body.get('data')


# --------------------------------------------------------------------------
# bank list + matching
# --------------------------------------------------------------------------

def fetch_banks(currency='NGN', use_cache=True):
    """Live bank list from Paystack. Cached for a day to avoid repeat calls."""
    if use_cache:
        cached = cache.get(BANK_CACHE_KEY)
        if cached:
            return cached

    banks = []
    page = 1
    while page <= 10:
        chunk = _request(
            'GET', '/bank',
            params={'currency': currency, 'perPage': 100, 'page': page},
        ) or []
        if not chunk:
            break
        for item in chunk:
            code = str(item.get('code') or '').strip()
            name = (item.get('name') or '').strip()
            if code and name:
                banks.append({'name': name, 'code': code, 'slug': item.get('slug') or ''})
        if len(chunk) < 100:
            break
        page += 1

    if banks:
        cache.set(BANK_CACHE_KEY, banks, BANK_CACHE_SECONDS)
    return banks


def _normalise_bank_name(value):
    text = (value or '').lower()
    text = re.sub(r'[^a-z0-9\s]', ' ', text)
    tokens = [t for t in text.split() if t and t not in _DROP_WORDS]
    return ' '.join(tokens)


def guess_bank(bank_name, banks):
    """Suggest the best match for a free-text bank name.

    Returns ``(best, score, candidates)`` where ``best`` is a bank dict or None.
    The score is a 0.0-1.0 similarity so the caller can decide whether to ask the
    admin to confirm.
    """
    target = _normalise_bank_name(bank_name)
    if not target or not banks:
        return None, 0.0, []

    expanded = _BANK_ALIASES.get((bank_name or '').strip().lower())
    queries = [target]
    if expanded:
        queries.insert(0, _normalise_bank_name(expanded))

    scored = []
    for bank in banks:
        norm = _normalise_bank_name(bank['name'])
        best = 0.0
        for query in queries:
            if not query:
                continue
            if query == norm:
                best = 1.0
            elif norm.startswith(query) or query.startswith(norm):
                # "zenith" vs "zenith bank" after suffix stripping
                best = max(best, 0.95)
            elif query in norm or norm in query:
                best = max(best, 0.88)
            else:
                best = max(best, difflib.SequenceMatcher(None, query, norm).ratio())
        scored.append((best, bank))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    candidates = [bank for score, bank in scored if score >= 0.55][:8]
    top_score, top_bank = scored[0]
    if top_score < 0.72:
        return None, top_score, candidates
    return top_bank, top_score, candidates


def find_bank_by_code(banks, code):
    code = str(code or '').strip()
    for bank in banks:
        if bank['code'] == code:
            return bank
    return None


# --------------------------------------------------------------------------
# account + transfer primitives
# --------------------------------------------------------------------------

def resolve_account(account_number, bank_code):
    """Confirm an account exists and get the name the bank holds on file."""
    account_number = re.sub(r'\D', '', str(account_number or ''))
    if len(account_number) != 10:
        raise PaystackPayoutError('Account number must be exactly 10 digits.')
    if not str(bank_code or '').strip():
        raise PaystackPayoutError('A bank code is required to resolve the account.')

    data = _request(
        'GET', '/bank/resolve',
        # Paystack expects the parameter to be named "bank_code" here. Sending it
        # as "bank" is rejected with HTTP 400 "bank_code is required".
        params={'account_number': account_number, 'bank_code': str(bank_code).strip()},
    ) or {}
    return {
        'account_number': data.get('account_number') or account_number,
        'account_name': (data.get('account_name') or '').strip(),
    }


def create_recipient(account_name, account_number, bank_code, currency='NGN'):
    data = _request('POST', '/transferrecipient', payload={
        'type': 'nuban',
        'name': account_name,
        'account_number': re.sub(r'\D', '', str(account_number or '')),
        'bank_code': str(bank_code).strip(),
        'currency': currency,
    }) or {}
    code = (data.get('recipient_code') or '').strip()
    if not code:
        raise PaystackPayoutError('Paystack did not return a recipient code.')
    return code


def initiate_transfer(amount_naira, recipient_code, reason, reference):
    try:
        kobo = int(
            (Decimal(str(amount_naira)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
             * 100).to_integral_value()
        )
    except Exception as exc:
        raise PaystackPayoutError('Invalid payout amount: %s' % exc) from exc

    if kobo <= 0:
        raise PaystackPayoutError('Payout amount must be greater than zero.')

    return _request('POST', '/transfer', payload={
        'source': 'balance',
        'amount': kobo,
        'recipient': recipient_code,
        'reason': (reason or 'Withdrawal payout')[:100],
        'reference': reference,
        'currency': 'NGN',
    }) or {}


def verify_transfer(reference):
    return _request('GET', '/transfer/verify/' + str(reference)) or {}


# --------------------------------------------------------------------------
# guards + orchestration
# --------------------------------------------------------------------------

def _names_equivalent(left, right):
    def clean(value):
        return re.sub(r'[^a-z0-9]', '', (value or '').lower())
    a, b = clean(left), clean(right)
    if not a or not b:
        return False
    if a == b:
        return True
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.85


def _record_audit(withdrawal, message, actor=None):
    """Write the admin-visible audit row for a payout event.

    The bookkeeping above uses ``.update()`` on purpose: it keeps Django signals
    (and therefore customer emails) out of the money path. The side effect is
    that no ActivityLog row is created automatically, so it is written here
    instead - a payout must never be a silent state change.
    """
    try:
        from betting.models import ActivityLog
        ActivityLog.objects.create(
            user=actor or withdrawal.user,
            action_type='UPDATE',
            action=message,
            affected_object='Withdrawal: %s' % withdrawal.pk,
        )
    except Exception:
        logger.exception('Could not write payout audit row for withdrawal %s', withdrawal.pk)


def preview_payout(withdrawal, banks=None, bank_code=None):
    """Read-only. Works out what a payout would do, without sending anything.

    Returns a dict with the resolved bank, the name Paystack holds on file, and
    any blocking problem. Used by the admin confirmation page.
    """
    result = {
        'withdrawal': withdrawal,
        'blocking_reason': '',
        'bank': None,
        'bank_confidence': 0.0,
        'bank_candidates': [],
        'resolved_account_name': '',
        'name_matches': False,
        'requires_bank_choice': False,
    }

    if not payouts_enabled():
        result['blocking_reason'] = (
            'Paystack payouts are switched off. Set PAYSTACK_PAYOUTS_ENABLED=True to enable.'
        )
        return result

    if withdrawal.status != 'approved':
        result['blocking_reason'] = (
            'Only approved withdrawals can be sent to the bank '
            '(this one is "%s").' % (withdrawal.status or 'unknown')
        )
        return result

    if withdrawal.payout_status == 'success':
        result['blocking_reason'] = 'This withdrawal has already been paid successfully.'
        return result

    if not withdrawal.account_number or len(re.sub(r'\D', '', str(withdrawal.account_number))) != 10:
        result['blocking_reason'] = 'Account number must be exactly 10 digits.'
        return result

    if not withdrawal.amount or Decimal(withdrawal.amount) <= 0:
        result['blocking_reason'] = 'Withdrawal amount is not valid.'
        return result

    try:
        banks = banks if banks is not None else fetch_banks()
    except PaystackPayoutError as exc:
        result['blocking_reason'] = str(exc)
        return result

    if bank_code:
        chosen = find_bank_by_code(banks, bank_code)
        if not chosen:
            result['blocking_reason'] = 'Bank code %s is not in the Paystack bank list.' % bank_code
            result['bank_candidates'] = banks[:200]
            result['requires_bank_choice'] = True
            return result
        result['bank'] = chosen
        result['bank_confidence'] = 1.0
    else:
        guess, score, candidates = guess_bank(withdrawal.bank_name, banks)
        result['bank'] = guess
        result['bank_confidence'] = score
        result['bank_candidates'] = candidates
        if guess is None:
            result['requires_bank_choice'] = True
            result['blocking_reason'] = (
                'Could not confidently match "%s" to a Paystack bank. '
                'Please choose the correct bank below.' % (withdrawal.bank_name or '')
            )
            return result

    bank = result['bank']
    try:
        resolved = resolve_account(withdrawal.account_number, bank['code'])
    except PaystackPayoutError as exc:
        result['blocking_reason'] = 'Account could not be verified: %s' % exc
        return result

    result['resolved_account_name'] = resolved['account_name']
    result['name_matches'] = _names_equivalent(resolved['account_name'], withdrawal.account_name)
    return result


def _claim_reference(withdrawal):
    """Reserve a unique Paystack reference before any money can move.

    Saving first is what makes an ambiguous failure recoverable: on retry we
    verify this exact reference instead of sending a second transfer.
    """
    attempt = (withdrawal.payout_attempts or 0) + 1
    return 'WD-%s-A%s' % (withdrawal.id, attempt)


def send_withdrawal_to_bank(withdrawal, *, actor=None, bank_code=None,
                            acknowledge_name_mismatch=False):
    """Send one approved withdrawal to the bank through Paystack.

    Returns a dict describing what happened. Never raises for business failures;
    those are recorded on the withdrawal and reported back.
    """
    from betting.models import UserWithdrawal

    if not payouts_enabled():
        raise PaystackPayoutError(
            'Paystack payouts are switched off. Set PAYSTACK_PAYOUTS_ENABLED=True to enable.'
        )

    with db_transaction.atomic():
        locked = UserWithdrawal.objects.select_for_update().get(pk=withdrawal.pk)

        if locked.status != 'approved':
            raise PaystackPayoutError(
                'Only approved withdrawals can be sent to the bank (this one is "%s").'
                % (locked.status or 'unknown')
            )
        if locked.payout_status == 'success':
            raise PaystackPayoutError('This withdrawal has already been paid successfully.')
        if locked.payout_status == 'processing':
            raise PaystackPayoutError(
                'A transfer for this withdrawal is already in progress. '
                'Use "Refresh payout status" instead of sending it again.'
            )

        banks = fetch_banks()
        chosen_code = str(bank_code or locked.bank_code or '').strip()
        if not chosen_code:
            guess, _score, _candidates = guess_bank(locked.bank_name, banks)
            if guess is None:
                raise PaystackPayoutError(
                    'Bank "%s" could not be matched. Choose the bank explicitly before sending.'
                    % (locked.bank_name or '')
                )
            chosen_code = guess['code']
        chosen_bank = find_bank_by_code(banks, chosen_code)
        if not chosen_bank:
            raise PaystackPayoutError(
                'Bank code %s is not in the Paystack bank list.' % chosen_code
            )

        resolved = resolve_account(locked.account_number, chosen_bank['code'])
        name_matches = _names_equivalent(resolved['account_name'], locked.account_name)
        if not name_matches and not acknowledge_name_mismatch:
            raise PaystackPayoutError(
                'The bank says this account belongs to "%s", but the withdrawal says "%s". '
                'Re-run and tick the acknowledgement if this is correct.'
                % (resolved['account_name'], locked.account_name)
            )

        reference = _claim_reference(locked)
        locked.payout_reference = reference
        locked.payout_attempts = attempt = (locked.payout_attempts or 0) + 1
        locked.bank_code = chosen_bank['code']
        locked.payout_status = 'processing'
        locked.payout_error = ''
        locked.save(update_fields=[
            'payout_reference', 'payout_attempts', 'bank_code',
            'payout_status', 'payout_error',
        ])

    # Reference is now durably claimed. From here a failure can always be
    # re-checked against Paystack using this exact reference.
    _record_audit(
        locked,
        'Paystack payout STARTED for withdrawal %s (ref %s, bank %s (%s), account %s)'
        % (locked.pk, reference, chosen_bank['name'], chosen_bank['code'],
           locked.account_number),
        actor=actor,
    )
    try:
        recipient_code = locked.payout_recipient_code
        if not recipient_code:
            recipient_code = create_recipient(
                resolved['account_name'], locked.account_number, chosen_bank['code']
            )
            UserWithdrawal.objects.filter(pk=locked.pk).update(
                payout_recipient_code=recipient_code
            )
            locked.payout_recipient_code = recipient_code

        reason = 'StakeNaija withdrawal %s' % locked.id
        transfer = initiate_transfer(locked.amount, recipient_code, reason, reference)
    except PaystackPayoutError as exc:
        # Paystack gates some transfers behind an OTP. It answers HTTP 200 with
        # status=false and data.status="otp" -- which looks like an error here,
        # but the transfer HAS been created and paying it only needs the OTP
        # entered in the Paystack dashboard. Marking it "failed" stranded live
        # payouts that were really in flight, so route it through the normal
        # mapper: the row stays "processing" and a later "Refresh payout status"
        # picks up the real result.
        payload = exc.payload or {}
        otp_data = payload.get('data') or {}
        otp_requested = (
            str(otp_data.get('status') or '').strip().lower() == 'otp'
            or 'otp' in str(payload.get('message') or exc).lower()
        )
        if otp_requested:
            return _apply_transfer_result(
                locked,
                otp_data or {'status': 'otp'},
                actor=actor,
                reference=reference,
            )

        # Could be a definite rejection or an unknown outcome. Keep the
        # reference so an admin can verify before deciding to retry.
        UserWithdrawal.objects.filter(pk=locked.pk).update(
            payout_status='failed',
            payout_error=str(exc),
        )
        _record_audit(
            locked,
            'Paystack payout FAILED for withdrawal %s (ref %s, bank %s): %s'
            % (locked.pk, reference, chosen_bank['name'], exc),
            actor=actor,
        )
        logger.warning('Paystack payout failed for withdrawal %s: %s', locked.pk, exc)
        return {
            'ok': False,
            'status': 'failed',
            'reference': reference,
            'message': str(exc),
            'transfer': {},
        }

    return _apply_transfer_result(locked, transfer, actor=actor, reference=reference)


def refresh_payout_status(withdrawal, *, actor=None):
    """Re-query Paystack for the transfer already linked to this withdrawal."""
    from betting.models import UserWithdrawal

    locked = UserWithdrawal.objects.get(pk=withdrawal.pk)
    if not locked.payout_reference:
        raise PaystackPayoutError('This withdrawal has no Paystack transfer to check.')

    transfer = verify_transfer(locked.payout_reference)
    return _apply_transfer_result(
        locked, transfer, actor=actor, reference=locked.payout_reference, is_refresh=True
    )


def _apply_transfer_result(withdrawal, transfer, *, actor=None, reference=None,
                           is_refresh=False):
    """Map a Paystack transfer payload onto the withdrawal and report back."""
    from betting.models import UserWithdrawal

    raw_status = str((transfer or {}).get('status') or '').strip().lower()
    transfer_code = (transfer or {}).get('transfer_code') or ''
    failure_reason = (transfer or {}).get('reason') if raw_status == 'failed' else ''

    if raw_status == 'otp':
        message = (
            'Paystack is asking for a transfer OTP. Complete the OTP in the '
            'Paystack dashboard, then use "Refresh payout status" to confirm '
            'the final result.'
        )
        UserWithdrawal.objects.filter(pk=withdrawal.pk).update(
            payout_status='processing', payout_error=message
        )
        _record_audit(
            withdrawal,
            'Paystack payout awaiting OTP for withdrawal %s (ref %s)'
            % (withdrawal.pk, reference),
            actor=actor,
        )
        return {'ok': False, 'status': 'processing', 'reference': reference,
                'message': message, 'transfer': transfer or {}}

    if raw_status in ('success', 'successful'):
        update = {
            'payout_status': 'success',
            'payout_error': '',
            'paid_at': timezone.now(),
            'status': 'completed',
        }
        UserWithdrawal.objects.filter(pk=withdrawal.pk).update(**update)
        _record_audit(
            withdrawal,
            'Paystack payout SUCCESS for withdrawal %s (ref %s, transfer %s) - marked completed'
            % (withdrawal.pk, reference, transfer_code or '-'),
            actor=actor,
        )
        logger.info('Paystack payout success for withdrawal %s (%s)',
                    withdrawal.pk, reference)
        return {'ok': True, 'status': 'success', 'reference': reference,
                'message': 'Transfer sent successfully. Withdrawal marked completed.',
                'transfer': transfer or {}}

    if raw_status in ('failed', 'reversed'):
        message = failure_reason or (
            'Paystack reported the transfer as %s. Held for manual review.' % raw_status
        )
        UserWithdrawal.objects.filter(pk=withdrawal.pk).update(
            payout_status='reversed' if raw_status == 'reversed' else 'failed',
            payout_error=message,
        )
        _record_audit(
            withdrawal,
            'Paystack payout %s for withdrawal %s (ref %s): %s'
            % (raw_status.upper(), withdrawal.pk, reference, message),
            actor=actor,
        )
        logger.warning('Paystack payout %s for withdrawal %s: %s',
                       raw_status, withdrawal.pk, message)
        return {'ok': False, 'status': raw_status, 'reference': reference,
                'message': message, 'transfer': transfer or {}}

    # pending / processing / received / anything unknown: leave it as processing
    # and let a human re-check. No automatic retry.
    message = (
        'Paystack reports "%s" for reference %s. Use "Refresh payout status" later '
        'to confirm the final result.' % (raw_status or 'unknown', reference)
    )
    UserWithdrawal.objects.filter(pk=withdrawal.pk).update(
        payout_status='processing', payout_error=''
    )
    if is_refresh:
        _record_audit(
            withdrawal,
            'Paystack payout still %s for withdrawal %s (ref %s)'
            % (raw_status or 'unknown', withdrawal.pk, reference),
            actor=actor,
        )
    return {'ok': False, 'status': raw_status or 'unknown', 'reference': reference,
            'message': message, 'transfer': transfer or {}}
