from celery import shared_task
from django.core.mail import EmailMessage
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils.html import strip_tags
from django.utils import timezone
from django.conf import settings
from django.db.models import Q
from django.db.models import Sum, Value, DecimalField
from django.db.models.functions import Coalesce
from django.core.cache import cache
from django.db import transaction as db_transaction
from decimal import Decimal
from datetime import datetime, timedelta
import io
import os
import base64
import shutil
import subprocess
from pathlib import Path
import pandas as pd
import requests

from .models import (
    Fixture,
    ScheduledFinanceReport,
    Transaction,
    User,
    Wallet,
    UserWithdrawal,
    WithdrawalReport,
    FinanceAuditLog,
    JournalEntry,
    PaymentGatewayEventLog,
    WithdrawalPinVerificationLog,
    FinanceSettlementBatch,
)
import logging

logger = logging.getLogger(__name__)

from notifications.services import create_notification, create_broadcast_notification


def _fmt_money(value):
    try:
        return f"₦{Decimal(value):,.2f}"
    except Exception:
        return f"₦{value}"


def _withdrawal_admin_recipients():
    configured = getattr(settings, 'WITHDRAWAL_ADMIN_EMAILS', None) or []
    configured = [e.strip() for e in configured if e and '@' in e]
    qs = (
        User.objects.filter(is_active=True)
        .filter(Q(is_superuser=True) | Q(user_type__in=['admin', 'finance', 'account_user']))
        .exclude(email__isnull=True)
        .exclude(email='')
        .values_list('email', flat=True)
    )
    all_emails = set(configured)
    all_emails.update([e.strip() for e in qs if e and '@' in e])
    return sorted(all_emails)

def _withdrawal_agent_recipients(withdrawal_user):
    try:
        u = withdrawal_user
        if not u:
            return []
        agent_user = None
        user_type = (getattr(u, 'user_type', '') or '').strip().lower()
        if user_type in ['agent', 'super_agent', 'master_agent']:
            agent_user = u
        else:
            agent_user = getattr(u, 'agent', None)
        email = (getattr(agent_user, 'email', '') or '').strip() if agent_user else ''
        if email and '@' in email:
            return [email]
    except Exception:
        pass
    return []


@shared_task(bind=True, autoretry_for=(Exception,), retry_backoff=True, retry_kwargs={'max_retries': 5})
def send_withdrawal_notification_emails(self, withdrawal_id, event):
    withdrawal = UserWithdrawal.objects.select_related('user', 'approved_rejected_by').get(id=withdrawal_id)

    try:
        from .models import SiteConfiguration
        site = SiteConfiguration.load()
        site_name = (getattr(site, 'site_name', '') or 'StakeNaija').strip() or 'StakeNaija'
    except Exception:
        site_name = 'StakeNaija'

    event_key = (event or '').strip().lower()
    if event_key not in ['requested', 'approved', 'completed', 'rejected']:
        return

    lock_key = f"withdrawal-email-lock:{withdrawal_id}:{event_key}"
    if not cache.add(lock_key, 1, timeout=300):
        return

    tx = (
        Transaction.objects.filter(related_withdrawal_request=withdrawal, transaction_type='withdrawal')
        .order_by('timestamp')
        .first()
    )
    reference = (
        getattr(tx, 'external_reference', None)
        or getattr(tx, 'paystack_reference', None)
        or (str(getattr(tx, 'id', '')) if tx else '')
        or str(withdrawal.id)
    )

    local_requested = timezone.localtime(withdrawal.request_time) if withdrawal.request_time else None
    local_processed = timezone.localtime(withdrawal.approved_rejected_time) if withdrawal.approved_rejected_time else None

    subject_map = {
        'requested': f"{site_name} • Withdrawal Request Submitted",
        'approved': f"{site_name} • Withdrawal Approved",
        'completed': f"{site_name} • Withdrawal Successful",
        'rejected': f"{site_name} • Withdrawal Rejected",
    }
    template_map = {
        'requested': 'betting/email/withdrawal_request.html',
        'approved': 'betting/email/withdrawal_success.html',
        'completed': 'betting/email/withdrawal_success.html',
        'rejected': 'betting/email/withdrawal_rejected.html',
    }
    user_field_map = {
        'requested': 'email_request_user_sent_at',
        'approved': 'email_approved_user_sent_at',
        'completed': 'email_completed_user_sent_at',
        'rejected': 'email_rejected_user_sent_at',
    }
    admin_field_map = {
        'requested': 'email_request_admin_sent_at',
        'approved': 'email_approved_admin_sent_at',
        'completed': 'email_completed_admin_sent_at',
        'rejected': 'email_rejected_admin_sent_at',
    }

    subject = subject_map[event_key]
    template_name = template_map[event_key]

    ctx_base = {
        'site_name': site_name,
        'user': withdrawal.user,
        'withdrawal': withdrawal,
        'amount_formatted': _fmt_money(withdrawal.amount),
        'requested_at': local_requested.strftime('%Y-%m-%d %H:%M:%S') if local_requested else '',
        'processed_at': local_processed.strftime('%Y-%m-%d %H:%M:%S') if local_processed else '',
        'reference': reference,
        'status': withdrawal.status,
        'event': event_key,
    }

    now = timezone.now()

    user_field = user_field_map[event_key]
    admin_field = admin_field_map[event_key]

    def save_report_entry(*, is_admin_copy, to_emails, cc_emails=None, bcc_emails=None, subject_text='', body_text='', body_html='', sent_at=None, error_text=''):
        try:
            u = withdrawal.user
            WithdrawalReport.objects.update_or_create(
                withdrawal=withdrawal,
                event=event_key,
                is_admin_copy=bool(is_admin_copy),
                defaults={
                    'user': u,
                    'username': (getattr(u, 'username', '') or getattr(u, 'email', '') or '').strip(),
                    'amount': withdrawal.amount,
                    'bank_name': withdrawal.bank_name,
                    'account_name': withdrawal.account_name,
                    'account_number': withdrawal.account_number,
                    'requested_at': withdrawal.request_time,
                    'updated_at': withdrawal.approved_rejected_time or now,
                    'transaction_reference': reference,
                    'withdrawal_status': withdrawal.status,
                    'email_subject': subject_text or '',
                    'email_to': ', '.join([e for e in (to_emails or []) if e])[:5000],
                    'email_cc': ', '.join([e for e in (cc_emails or []) if e])[:5000],
                    'email_bcc': ', '.join([e for e in (bcc_emails or []) if e])[:5000],
                    'email_body_text': body_text or '',
                    'email_body_html': body_html or '',
                    'email_sent_at': sent_at,
                    'email_error': error_text or '',
                }
            )
        except Exception:
            pass

    if getattr(withdrawal, user_field, None) is None:
        to_email = (withdrawal.user.email or '').strip()
        to_emails = [to_email] if (to_email and '@' in to_email) else []
        cc_emails = []
        agent_emails = _withdrawal_agent_recipients(withdrawal.user)
        for e in agent_emails:
            if e and e not in to_emails and e not in cc_emails:
                cc_emails.append(e)
        if not to_emails and cc_emails:
            to_emails = cc_emails
            cc_emails = []

        if to_emails:
            try:
                html = render_to_string(template_name, {**ctx_base, 'is_admin_copy': False})
                text = strip_tags(html) or f"{site_name}: Withdrawal update"
                msg = EmailMultiAlternatives(subject=subject, body=text, to=to_emails, cc=cc_emails)
                msg.attach_alternative(html, "text/html")
                msg.send(fail_silently=False)
                update_fields = {user_field: now, 'last_email_error': ''}
                if event_key in ['approved', 'completed']:
                    update_fields['email_success_user_sent_at'] = now
                UserWithdrawal.objects.filter(id=withdrawal.id).update(**update_fields)
                save_report_entry(
                    is_admin_copy=False,
                    to_emails=to_emails,
                    cc_emails=cc_emails,
                    subject_text=subject,
                    body_text=text,
                    body_html=html,
                    sent_at=now,
                    error_text='',
                )
            except Exception as e:
                UserWithdrawal.objects.filter(id=withdrawal.id).update(last_email_error=str(e)[:2000])
                save_report_entry(
                    is_admin_copy=False,
                    to_emails=to_emails,
                    cc_emails=cc_emails,
                    subject_text=subject,
                    body_text='',
                    body_html='',
                    sent_at=None,
                    error_text=str(e)[:2000],
                )
                raise

    if getattr(withdrawal, admin_field, None) is None:
        recipients = _withdrawal_admin_recipients()
        if not recipients:
            UserWithdrawal.objects.filter(id=withdrawal.id).update(
                last_email_error="No admin recipients configured for withdrawal notifications."
            )
            save_report_entry(
                is_admin_copy=True,
                to_emails=[],
                subject_text=subject,
                body_text='',
                body_html='',
                sent_at=None,
                error_text="No admin recipients configured for withdrawal notifications.",
            )
        else:
            try:
                html = render_to_string(template_name, {**ctx_base, 'is_admin_copy': True})
                text = strip_tags(html) or f"{site_name}: Withdrawal update"
                msg = EmailMultiAlternatives(subject=subject, body=text, to=recipients)
                msg.attach_alternative(html, "text/html")
                msg.send(fail_silently=False)
                update_fields = {admin_field: now, 'last_email_error': ''}
                if event_key in ['approved', 'completed']:
                    update_fields['email_success_admin_sent_at'] = now
                UserWithdrawal.objects.filter(id=withdrawal.id).update(**update_fields)
                save_report_entry(
                    is_admin_copy=True,
                    to_emails=recipients,
                    subject_text=subject,
                    body_text=text,
                    body_html=html,
                    sent_at=now,
                    error_text='',
                )
            except Exception as e:
                UserWithdrawal.objects.filter(id=withdrawal.id).update(last_email_error=str(e)[:2000])
                save_report_entry(
                    is_admin_copy=True,
                    to_emails=recipients,
                    subject_text=subject,
                    body_text='',
                    body_html='',
                    sent_at=None,
                    error_text=str(e)[:2000],
                )
                raise

@shared_task(bind=True, autoretry_for=(Exception,), retry_backoff=True, retry_kwargs={'max_retries': 3})
def backfill_withdrawal_notification_emails(self, withdrawal_ids):
    attempted = 0
    sent = 0
    skipped = 0
    failed = 0

    qs = UserWithdrawal.objects.filter(id__in=list(withdrawal_ids or [])).select_related('user').order_by('id')
    for w in qs.iterator():
        needed_events = []

        if w.email_request_admin_sent_at is None or w.email_request_user_sent_at is None:
            needed_events.append('requested')

        status_key = (w.status or '').strip().lower()
        if status_key in ('approved', 'completed'):
            status_field_map = {
                'approved': ('email_approved_admin_sent_at', 'email_approved_user_sent_at'),
                'completed': ('email_completed_admin_sent_at', 'email_completed_user_sent_at'),
            }
            admin_field, user_field = status_field_map[status_key]
            if getattr(w, admin_field, None) is None or getattr(w, user_field, None) is None:
                needed_events.append(status_key)
        elif status_key == 'rejected':
            if w.email_rejected_admin_sent_at is None or w.email_rejected_user_sent_at is None:
                needed_events.append('rejected')

        needed_events = list(dict.fromkeys(needed_events))
        if not needed_events:
            skipped += 1
            continue

        for ev in needed_events:
            attempted += 1
            try:
                send_withdrawal_notification_emails(w.id, ev)
                sent += 1
            except Exception:
                failed += 1

    return {
        'attempted': attempted,
        'sent': sent,
        'skipped': skipped,
        'failed': failed,
    }

@shared_task
def update_started_fixtures_status():
    """
    Periodically check for fixtures that have started and update their status/visibility.
    """
    # Get current time in the project's timezone (Africa/Lagos)
    local_now = timezone.localtime(timezone.now())
    
    # Find fixtures that are 'scheduled' and 'active' but start time has passed
    # We look for:
    # 1. Match date is in the past
    # 2. OR Match date is today AND match time is in the past or now
    started_fixtures = Fixture.objects.filter(
        is_active=True,
        status='scheduled'
    ).filter(
        Q(match_date__lt=local_now.date()) | 
        Q(match_date=local_now.date(), match_time__lte=local_now.time())
    )
    
    count = started_fixtures.count()
    if count > 0:
        # Update these fixtures:
        # 1. Set is_active=False (hides from public view)
        # 2. Set status='live' (indicates match has started)
        # Note: bulk update does not trigger signals, which is usually fine for this transition.
        updated_count = started_fixtures.update(is_active=False, status='live')
        logger.info(f"Updated {updated_count} fixtures to 'live' status and deactivated them.")
    else:
        logger.debug("No started fixtures found to update.")

@shared_task
def recalculate_tickets_for_fixture(fixture_id):
    """
    Background task to recalculate all tickets associated with a changed fixture.
    This prevents timeouts when saving results in the admin.
    """
    from .models import BetTicket, Selection  # Local import to avoid circular dependency
    try:
        # Get fixture - if it doesn't exist anymore, just return
        try:
            fixture = Fixture.objects.get(id=fixture_id)
        except Fixture.DoesNotExist:
            logger.warning(f"Fixture {fixture_id} not found during ticket recalculation task.")
            return

        try:
            serial = str(getattr(fixture, "serial_number", "") or "").strip()
            period_id = getattr(fixture, "betting_period_id", None)
            relink_q = Q(bet_ticket__status="pending")
            if period_id:
                relink_q &= (Q(betting_period_id=period_id) | Q(betting_period__isnull=True))
            if serial:
                relink_q &= Q(fixture_serial_number__iexact=serial)
            else:
                relink_q &= Q(
                    fixture_home_team__iexact=fixture.home_team,
                    fixture_away_team__iexact=fixture.away_team,
                    fixture_match_date=fixture.match_date,
                    fixture_match_time=fixture.match_time,
                )

            Selection.objects.filter(relink_q).exclude(fixture_id=fixture.id).update(
                fixture=fixture,
                fixture_serial_number=serial or "",
                fixture_home_team=fixture.home_team,
                fixture_away_team=fixture.away_team,
                fixture_match_date=fixture.match_date,
                fixture_match_time=fixture.match_time,
            )
        except Exception:
            pass

        tickets = BetTicket.objects.filter(status="pending", selections__fixture=fixture).distinct()
        count = tickets.count()
        logger.info(f"Starting recalculation for {count} tickets for fixture {fixture}")

        for ticket in tickets:
            try:
                # First, recalculate odds and potential winnings to handle void events
                ticket.recalculate_ticket()
                # Then, check if the ticket status should change (Won/Lost)
                ticket.check_and_update_status()
            except Exception as e:
                logger.error(f"Error updating ticket {ticket.id}: {e}")
        
        logger.info(f"Completed recalculation for {count} tickets for fixture {fixture}")
        
    except Exception as e:
        logger.error(f"Critical error in recalculate_tickets_for_fixture: {e}")


def _parse_recipients(raw):
    parts = [p.strip() for p in (raw or '').replace(';', ',').split(',')]
    return [p for p in parts if p and '@' in p]


def _report_range_for_frequency(freq, today):
    if freq == 'weekly':
        end_date = today - timedelta(days=1)
        start_date = end_date - timedelta(days=6)
        return start_date, end_date
    if freq == 'monthly':
        first_this_month = today.replace(day=1)
        end_prev = first_this_month - timedelta(days=1)
        start_prev = end_prev.replace(day=1)
        return start_prev, end_prev
    end_date = today - timedelta(days=1)
    start_date = end_date
    return start_date, end_date


def _next_run_at(freq, now):
    local_now = timezone.localtime(now)
    run_time = local_now.replace(hour=8, minute=5, second=0, microsecond=0)
    if run_time <= local_now:
        run_time = run_time + timedelta(days=1)
    if freq == 'weekly':
        run_time = run_time + timedelta(days=7)
    elif freq == 'monthly':
        first_next_month = (run_time.date().replace(day=1) + timedelta(days=32)).replace(day=1)
        run_time = timezone.make_aware(datetime.combine(first_next_month, run_time.time()))
    return run_time


def generate_finance_report_bytes(dataset, fmt, start_dt, end_dt):
    rows = []
    title = dataset or 'report'

    if dataset == 'deposits':
        qs = Transaction.objects.filter(transaction_type='deposit', timestamp__gte=start_dt, timestamp__lte=end_dt).select_related('user').order_by('-timestamp')
        for tx in qs[:100000]:
            rows.append({
                'time': tx.timestamp.isoformat(sep=' ', timespec='seconds'),
                'tx_id': str(tx.id),
                'user': tx.user.email or tx.user.username,
                'amount': str(tx.amount),
                'status': tx.status,
                'successful': 'yes' if tx.is_successful else 'no',
                'gateway': getattr(tx, 'payment_gateway', ''),
                'ref': tx.paystack_reference or tx.external_reference or '',
            })
        title = 'deposits'
    elif dataset == 'withdrawals':
        qs = UserWithdrawal.objects.filter(request_time__gte=start_dt, request_time__lte=end_dt).select_related('user', 'approved_rejected_by').order_by('-request_time')
        for w in qs[:100000]:
            rows.append({
                'time': w.request_time.isoformat(sep=' ', timespec='seconds'),
                'withdrawal_id': str(w.id),
                'user': w.user.email or w.user.username,
                'amount': str(w.amount),
                'status': w.status,
                'bank': w.bank_name,
                'account_number': w.account_number,
                'handled_by': getattr(getattr(w, 'approved_rejected_by', None), 'email', '') or '',
            })
        title = 'withdrawals'
    elif dataset == 'transactions':
        qs = Transaction.objects.filter(timestamp__gte=start_dt, timestamp__lte=end_dt).select_related('user', 'initiating_user').order_by('-timestamp')
        for tx in qs[:100000]:
            rows.append({
                'time': tx.timestamp.isoformat(sep=' ', timespec='seconds'),
                'tx_id': str(tx.id),
                'user': tx.user.email or tx.user.username,
                'type': tx.transaction_type,
                'amount': str(tx.amount),
                'status': tx.status,
                'successful': 'yes' if tx.is_successful else 'no',
                'gateway': getattr(tx, 'payment_gateway', ''),
                'initiator': getattr(getattr(tx, 'initiating_user', None), 'email', '') or '',
            })
        title = 'transactions'
    elif dataset == 'ledger':
        qs = FinanceAuditLog.objects.filter(created_at__gte=start_dt, created_at__lte=end_dt).select_related('actor', 'target_user', 'transaction', 'withdrawal').order_by('-created_at')
        for a in qs[:100000]:
            rows.append({
                'time': a.created_at.isoformat(sep=' ', timespec='seconds'),
                'action': a.action_type,
                'actor': getattr(getattr(a, 'actor', None), 'email', '') or '',
                'target_user': getattr(getattr(a, 'target_user', None), 'email', '') or '',
                'transaction_id': str(a.transaction_id) if a.transaction_id else '',
                'withdrawal_id': str(a.withdrawal_id) if a.withdrawal_id else '',
                'reason': a.reason,
            })
        title = 'ledger'
    elif dataset == 'journals':
        qs = JournalEntry.objects.filter(created_at__gte=start_dt, created_at__lte=end_dt).select_related('created_by').prefetch_related('lines__account').order_by('-created_at')
        for je in qs[:50000]:
            for line in list(getattr(je, 'lines', []).all())[:50]:
                rows.append({
                    'time': je.created_at.isoformat(sep=' ', timespec='seconds'),
                    'entry_date': je.entry_date.isoformat(),
                    'journal_id': str(je.id),
                    'memo': je.memo,
                    'created_by': getattr(getattr(je, 'created_by', None), 'email', '') or '',
                    'account': getattr(getattr(line, 'account', None), 'code', '') or '',
                    'account_name': getattr(getattr(line, 'account', None), 'name', '') or '',
                    'debit': str(line.debit),
                    'credit': str(line.credit),
                })
        title = 'journals'
    elif dataset == 'settlements':
        qs = FinanceSettlementBatch.objects.filter(created_at__gte=start_dt, created_at__lte=end_dt).select_related('created_by', 'approved_by').order_by('-created_at')
        for b in qs[:50000]:
            totals = b.items.aggregate(s=Coalesce(Sum('amount'), Value(0), output_field=DecimalField()))['s']
            rows.append({
                'time': b.created_at.isoformat(sep=' ', timespec='seconds'),
                'batch_id': str(b.id),
                'type': b.settlement_type,
                'status': b.status,
                'period_start': b.period_start.isoformat(),
                'period_end': b.period_end.isoformat(),
                'items_total': str(totals),
            })
        title = 'settlements'
    elif dataset == 'gateway_logs':
        qs = PaymentGatewayEventLog.objects.filter(created_at__gte=start_dt, created_at__lte=end_dt).select_related('transaction', 'user').order_by('-created_at')
        for g in qs[:100000]:
            rows.append({
                'time': g.created_at.isoformat(sep=' ', timespec='seconds'),
                'gateway': g.gateway,
                'event': g.event_type,
                'reference': g.reference,
                'success': 'yes' if g.success else 'no',
                'http_status': str(g.http_status or ''),
                'amount': str(g.amount or ''),
                'fee': str(g.fee_amount or ''),
                'user': getattr(getattr(g, 'user', None), 'email', '') or '',
                'tx_id': str(g.transaction_id or ''),
                'message': g.message,
            })
        title = 'gateway_logs'
    elif dataset == 'pin_logs':
        qs = WithdrawalPinVerificationLog.objects.filter(created_at__gte=start_dt, created_at__lte=end_dt).select_related('user').order_by('-created_at')
        for p in qs[:100000]:
            rows.append({
                'time': p.created_at.isoformat(sep=' ', timespec='seconds'),
                'user': p.user.email or p.user.username,
                'success': 'yes' if p.success else 'no',
                'ip': p.ip_address or '',
                'user_agent': p.user_agent or '',
            })
        title = 'pin_logs'
    else:
        raise ValueError("Unknown dataset")

    if fmt == 'csv':
        import csv
        output = io.StringIO()
        fieldnames = list(rows[0].keys()) if rows else []
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            writer.writerow(r)
        return output.getvalue().encode('utf-8'), title, 'text/csv', f"{title}.csv"

    if fmt == 'xlsx':
        output = io.BytesIO()
        df = pd.DataFrame(rows)
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            df.to_excel(writer, index=False, sheet_name=title[:31] or 'Sheet1')
        output.seek(0)
        return output.getvalue(), title, 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', f"{title}.xlsx"

    if fmt == 'pdf':
        try:
            from weasyprint import HTML
        except Exception as e:
            raise RuntimeError(f"PDF export unavailable: {e}")
        from html import escape as _html_escape
        cols = list(rows[0].keys()) if rows else []
        def esc(s):
            return _html_escape(str(s or ''), quote=True)
        head = ''.join([f"<th>{esc(c)}</th>" for c in cols])
        body = ''.join([
            "<tr>" + ''.join([f"<td>{esc(r.get(c))}</td>" for c in cols]) + "</tr>"
            for r in rows[:3000]
        ])
        html = f"""
        <html>
          <head>
            <meta charset="utf-8" />
            <style>
              body {{ font-family: Arial, sans-serif; font-size: 11px; }}
              h2 {{ margin: 0 0 8px 0; }}
              .meta {{ color: #666; margin-bottom: 12px; }}
              table {{ width: 100%; border-collapse: collapse; }}
              th, td {{ border: 1px solid #ddd; padding: 6px; vertical-align: top; }}
              th {{ background: #f3f5f7; text-align: left; }}
              tr:nth-child(even) td {{ background: #fafafa; }}
            </style>
          </head>
          <body>
            <h2>Finance Report: {esc(title)}</h2>
            <div class="meta">Range: {esc(start_dt.date().isoformat())} → {esc(end_dt.date().isoformat())}</div>
            <table>
              <thead><tr>{head}</tr></thead>
              <tbody>{body}</tbody>
            </table>
          </body>
        </html>
        """
        pdf_bytes = HTML(string=html).write_pdf()
        return pdf_bytes, title, 'application/pdf', f"{title}.pdf"

    raise ValueError("Unknown format")


@shared_task
def run_scheduled_finance_reports():
    now = timezone.now()
    today = timezone.localdate()
    due = ScheduledFinanceReport.objects.filter(is_active=True).filter(Q(next_run_at__lte=now) | Q(next_run_at__isnull=True)).order_by('next_run_at', 'id')[:200]
    ran = 0
    for r in due:
        recipients = _parse_recipients(r.recipients)
        if not recipients:
            r.last_status = 'skipped'
            r.last_error = 'No recipients'
            r.next_run_at = _next_run_at(r.frequency, now)
            r.last_run_at = now
            r.save(update_fields=['last_status', 'last_error', 'next_run_at', 'last_run_at', 'updated_at'])
            continue
        try:
            start_date, end_date = _report_range_for_frequency(r.frequency, today)
            start_dt = timezone.make_aware(datetime.combine(start_date, datetime.min.time()))
            end_dt = timezone.make_aware(datetime.combine(end_date, datetime.max.time()))
            content, title, mime, filename = generate_finance_report_bytes(r.dataset, r.report_format, start_dt, end_dt)
            subject = f"Finance Report: {r.name} ({start_date.isoformat()} → {end_date.isoformat()})"
            body = f"Attached: {title}.{r.report_format}"
            email = EmailMessage(subject=subject, body=body, to=recipients)
            email.attach(filename, content, mime)
            email.send(fail_silently=False)
            r.last_status = 'sent'
            r.last_error = ''
        except Exception as e:
            r.last_status = 'failed'
            r.last_error = str(e)[:255]
        r.last_run_at = now
        r.next_run_at = _next_run_at(r.frequency, now)
        r.save(update_fields=['last_status', 'last_error', 'last_run_at', 'next_run_at', 'updated_at'])
        ran += 1
    return ran


@shared_task(bind=True, autoretry_for=(Exception,), retry_backoff=True, retry_kwargs={'max_retries': 3})
def reconcile_recent_deposits(self, gateway="all", minutes=1440, limit=50, stuck_minutes=30, alert_cooldown_minutes=360):
    now = timezone.now()
    cutoff = now - timedelta(minutes=max(int(minutes or 1440), 1))
    gateway = (gateway or "all").strip().lower()
    stuck_cutoff = now - timedelta(minutes=max(int(stuck_minutes or 30), 1))
    alert_ttl = int(max(int(alert_cooldown_minutes or 360), 5) * 60)

    qs = Transaction.objects.filter(
        transaction_type="deposit",
        status__in=["pending", "failed"],
        is_successful=False,
        timestamp__gte=cutoff,
    ).exclude(external_reference__isnull=True).exclude(external_reference="")
    if gateway != "all":
        qs = qs.filter(payment_gateway=gateway)
    candidates = list(qs.order_by("timestamp")[: int(limit or 50)])
    credited = 0

    for tx in candidates:
        ref = (tx.external_reference or "").strip()
        gw = (getattr(tx, "payment_gateway", "") or "paystack").strip().lower()
        try:
            ok, amount_verified, payload, http_status, msg = _verify_deposit_gateway(gateway=gw, reference=ref)
        except Exception as e:
            PaymentGatewayEventLog.objects.create(
                gateway=gw,
                event_type="reconcile",
                reference=ref,
                transaction=tx,
                user=tx.user,
                amount=tx.amount,
                success=False,
                message=str(e),
                payload={},
            )
            if tx.timestamp and tx.timestamp <= stuck_cutoff:
                _maybe_alert_stuck_deposit(tx=tx, now=now, ttl_seconds=alert_ttl)
            continue

        PaymentGatewayEventLog.objects.create(
            gateway=gw,
            event_type="reconcile",
            reference=ref,
            transaction=tx,
            user=tx.user,
            amount=tx.amount,
            success=bool(ok),
            http_status=http_status,
            message=(msg or ""),
            payload=payload or {},
        )

        if not ok:
            if tx.timestamp and tx.timestamp <= stuck_cutoff:
                _maybe_alert_stuck_deposit(tx=tx, now=now, ttl_seconds=alert_ttl)
            continue

        try:
            amount_q = Decimal(str(amount_verified)).quantize(Decimal("0.01"))
        except Exception:
            continue

        with db_transaction.atomic():
            locked = Transaction.objects.select_for_update().select_related("user").get(pk=tx.pk)
            if locked.status == "completed" and locked.is_successful:
                continue
            if Decimal(str(locked.amount)).quantize(Decimal("0.01")) != amount_q:
                locked.status = "failed"
                locked.is_successful = False
                locked.description = f"Amount mismatch: Expected {locked.amount}, Got {amount_q}"
                locked.save(update_fields=["status", "is_successful", "description"])
                continue
            wallet, _ = Wallet.objects.select_for_update().get_or_create(user=locked.user, defaults={"balance": Decimal("0.00")})
            wallet.apply_delta(
                amount=amount_q,
                actor=None,
                transaction_obj=locked,
                reference=ref,
                reason=f"Deposit via {gw} (reconcile)",
                metadata={"gateway": gw, "source": "reconcile"},
            )
            locked.status = "completed"
            locked.is_successful = True
            locked.description = f"Online deposit via {gw} successful."
            locked.timestamp = now
            locked.save(update_fields=["status", "is_successful", "description", "timestamp"])
            credited += 1

    return credited


@shared_task(bind=True, autoretry_for=(Exception,), retry_backoff=True, retry_kwargs={'max_retries': 2})
def backup_database(self, validate_restore=True, retention_days=7):
    db = (getattr(settings, "DATABASES", None) or {}).get("default") or {}
    engine = (db.get("ENGINE") or "").strip().lower()
    if "postgresql" not in engine:
        raise RuntimeError("DB backup task supports PostgreSQL only.")

    db_name = (db.get("NAME") or "").strip()
    db_user = (db.get("USER") or "").strip()
    db_password = db.get("PASSWORD") or ""
    db_host = (db.get("HOST") or "localhost").strip() or "localhost"
    db_port = str(db.get("PORT") or "5432").strip() or "5432"

    pg_dump = (os.getenv("PG_DUMP_PATH") or "").strip() or (shutil.which("pg_dump") or "")
    if not pg_dump:
        raise RuntimeError("pg_dump not found. Set PG_DUMP_PATH or install PostgreSQL client tools.")

    out_dir = (os.getenv("DB_BACKUP_DIR") or "").strip()
    if out_dir:
        out_path = Path(out_dir)
    else:
        out_path = Path(getattr(settings, "BASE_DIR", Path.cwd())) / "db_backups"
    out_path.mkdir(parents=True, exist_ok=True)

    ts = timezone.now().strftime("%Y%m%d_%H%M%S")
    safe_db_name = (db_name or "database").replace(" ", "_")
    file_path = out_path / f"{safe_db_name}_{ts}.dump"

    env = os.environ.copy()
    env["PGPASSWORD"] = str(db_password)

    cmd = [
        pg_dump,
        "-Fc",
        "--no-owner",
        "--no-privileges",
        "-h",
        db_host,
        "-p",
        db_port,
        "-U",
        db_user,
        "-f",
        str(file_path),
        db_name,
    ]
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "pg_dump failed").strip()[:500])

    size_bytes = file_path.stat().st_size if file_path.exists() else 0
    if size_bytes <= 0:
        raise RuntimeError("Backup file is empty.")

    restore_check_ok = None
    if bool(validate_restore):
        pg_restore = (os.getenv("PG_RESTORE_PATH") or "").strip() or (shutil.which("pg_restore") or "")
        if pg_restore:
            proc2 = subprocess.run([pg_restore, "--list", str(file_path)], env=env, capture_output=True, text=True)
            restore_check_ok = proc2.returncode == 0
            if proc2.returncode != 0:
                raise RuntimeError((proc2.stderr or proc2.stdout or "pg_restore --list failed").strip()[:500])
        else:
            restore_check_ok = False

    retention = int(os.getenv("DB_BACKUP_RETENTION_DAYS") or retention_days or 7)
    cutoff = timezone.now() - timedelta(days=max(retention, 1))
    for p in out_path.glob("*.dump"):
        try:
            mtime = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.get_current_timezone())
            if mtime < cutoff:
                p.unlink(missing_ok=True)
        except Exception:
            continue

    return {
        "file": str(file_path),
        "size_bytes": int(size_bytes),
        "restore_check_ok": restore_check_ok,
    }


def _verify_deposit_gateway(*, gateway, reference):
    gateway = (gateway or "").strip().lower()
    reference = (reference or "").strip()
    if not reference:
        raise ValueError("Missing reference")

    if gateway == "paystack":
        secret = (getattr(settings, "PAYSTACK_SECRET_KEY", None) or "").strip()
        if not secret:
            raise RuntimeError("Missing PAYSTACK_SECRET_KEY")
        url = f"https://api.paystack.co/transaction/verify/{reference}"
        resp = requests.get(url, headers={"Authorization": f"Bearer {secret}"}, timeout=15)
        payload = resp.json()
        data = payload.get("data") or {}
        ok = bool(payload.get("status") and data.get("status") == "success")
        amount_verified = (Decimal(str(data.get("amount") or "0")) / Decimal("100")).quantize(Decimal("0.01"))
        msg = str(data.get("gateway_response") or data.get("message") or payload.get("message") or "")
        return ok, amount_verified, {"response": payload}, getattr(resp, "status_code", None), msg

    if gateway == "kora":
        secret_key = (os.getenv("KORA_SECRET_KEY") or os.getenv("KORAPAY_SECRET_KEY") or "").strip()
        base_url = os.getenv("KORA_BASE_URL") or os.getenv("KORAPAY_BASE_URL") or "https://api.korapay.com/merchant/api/v1"
        if base_url.rstrip("/").endswith("/merchant/api"):
            base_url = f"{base_url.rstrip('/')}/v1"
        if not secret_key:
            raise RuntimeError("Missing KORA_SECRET_KEY")
        url = f"{base_url.rstrip('/')}/charges/{reference}"
        resp = requests.get(url, headers={"Authorization": f"Bearer {secret_key}"}, timeout=15)
        payload = resp.json()
        ok = bool(payload.get("status") and (payload.get("data") or {}).get("status") == "success")
        amount_verified = Decimal(str((payload.get("data") or {}).get("amount") or "0"))
        msg = str(payload.get("message") or "")
        return ok, amount_verified, {"response": payload}, getattr(resp, "status_code", None), msg

    if gateway == "monnify":
        api_key = (os.getenv("MONNIFY_API_KEY") or "").strip()
        secret_key = (os.getenv("MONNIFY_SECRET_KEY") or "").strip()
        base_url = (os.getenv("MONNIFY_BASE_URL") or "").strip()
        if not base_url:
            raise RuntimeError("Missing MONNIFY_BASE_URL")
        if not api_key or not secret_key:
            raise RuntimeError("Missing MONNIFY credentials")

        auth_str = base64.b64encode(f"{api_key}:{secret_key}".encode()).decode()
        auth_url = f"{base_url.rstrip('/')}/api/v1/auth/login"
        auth_resp = requests.post(auth_url, headers={"Authorization": f"Basic {auth_str}"}, timeout=15)
        auth_payload = auth_resp.json() if auth_resp.content else {}
        if not bool(auth_payload.get("requestSuccessful")):
            msg = str(auth_payload.get("responseMessage") or "Authentication failed")
            return False, Decimal("0.00"), {"auth": auth_payload}, getattr(auth_resp, "status_code", None), msg

        token = ((auth_payload.get("responseBody") or {}).get("accessToken") or "").strip()
        if not token:
            return False, Decimal("0.00"), {"auth": auth_payload}, getattr(auth_resp, "status_code", None), "Missing access token"

        verify_url = f"{base_url.rstrip('/')}/api/v1/merchant/transactions/query?paymentReference={reference}"
        verify_resp = requests.get(verify_url, headers={"Authorization": f"Bearer {token}"}, timeout=15)
        verify_payload = verify_resp.json() if verify_resp.content else {}
        body = verify_payload.get("responseBody") or {}
        ok = bool(verify_payload.get("requestSuccessful") and body.get("paymentStatus") == "PAID")
        amount_verified = Decimal(str(body.get("amountPaid") or "0"))
        msg = str(verify_payload.get("responseMessage") or body.get("paymentStatus") or "")
        return ok, amount_verified, {"response": verify_payload}, getattr(verify_resp, "status_code", None), msg

    raise RuntimeError(f"Unsupported gateway: {gateway}")


def _deposit_admin_queryset():
    return User.objects.filter(is_active=True).filter(Q(is_superuser=True) | Q(user_type__in=["admin", "finance", "account_user"]))


def _maybe_alert_stuck_deposit(*, tx, now, ttl_seconds):
    lock_key = f"deposit_stuck_alert:{tx.pk}"
    if not cache.add(lock_key, 1, timeout=ttl_seconds):
        return

    ref = (tx.external_reference or tx.paystack_reference or str(tx.id) or "").strip()
    gateway = (getattr(tx, "payment_gateway", "") or "").strip().lower() or "paystack"
    title_user = "Deposit pending verification"
    msg_user = f"Your deposit is still pending confirmation. Reference: {ref}. If your payment was successful, it will be credited automatically once confirmed."
    try:
        create_notification(
            recipient=tx.user,
            notification_type="DEPOSIT_REMINDER",
            title=title_user,
            message=msg_user,
            data={"transaction_id": str(tx.id), "reference": ref, "gateway": gateway, "status": tx.status},
        )
    except Exception:
        pass

    admins = _deposit_admin_queryset()
    title_admin = "Pending deposit requires attention"
    msg_admin = f"A deposit has been pending beyond the configured threshold. User: {getattr(tx.user, 'email', '')}. Reference: {ref}. Gateway: {gateway}. Amount: {_fmt_money(tx.amount)}."
    try:
        create_broadcast_notification(
            queryset=admins,
            notification_type="SYSTEM_ANNOUNCEMENT",
            title=title_admin,
            message=msg_admin,
            data={"transaction_id": str(tx.id), "reference": ref, "gateway": gateway, "status": tx.status},
        )
    except Exception:
        pass
