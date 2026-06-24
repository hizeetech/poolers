from django.db import transaction
from django.utils import timezone
from .models import (
    WeeklyAgentCommission,
    MonthlyNetworkCommission,
    AgentCommissionProfile,
    CommissionPeriod,
    CommissionPlan,
    CommissionProfileAssignmentLog,
    CommissionOverrideLog,
    CommissionRecall,
    CommissionRecallLog,
    CommissionRecallApproval,
)
from betting.models import Wallet, Transaction, BetTicket, SiteConfiguration
from django.db.models import Sum, Q
from decimal import Decimal
import logging
from django.contrib.auth import get_user_model
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from notifications.services import create_notification

User = get_user_model()
logger = logging.getLogger(__name__)


def _commission_voided_statuses():
    voided_statuses = list(getattr(BetTicket, 'VOIDED_STATUSES', ()) or ())
    if 'voided' not in voided_statuses:
        voided_statuses.append('voided')
    return tuple(voided_statuses)


def _commission_excluded_ticket_statuses(*, is_live_period):
    excluded = list(_commission_voided_statuses())
    if not is_live_period and 'pending' not in excluded:
        excluded.append('pending')
    return excluded


def restore_historical_weekly_paid_commission_record(agent, period, *, calc_data=None):
    period_text = str(period)
    historical_tx = (
        Transaction.objects.filter(
            user=agent,
            transaction_type='commission_payout',
            status='completed',
            is_successful=True,
            amount__gt=Decimal('0.00'),
        )
        .filter(
            Q(description__icontains='weekly commission')
            & (
                Q(description__icontains=period_text)
                | (
                    Q(description__icontains=str(period.start_date))
                    & Q(description__icontains=str(period.end_date))
                )
            )
        )
        .order_by('-timestamp')
        .first()
    )
    if not historical_tx:
        return None

    data = dict(calc_data or {})
    data.pop('is_live_period', None)
    existing = WeeklyAgentCommission.objects.filter(agent=agent, period=period).first()
    total_amount = data.get('commission_total_amount')
    if total_amount is None and existing:
        total_amount = existing.commission_total_amount
    if total_amount is None:
        total_amount = historical_tx.amount or Decimal('0.00')

    defaults = {
        **data,
        'status': 'paid',
        'amount_paid': total_amount,
        'paid_at': historical_tx.timestamp,
        'paid_by': historical_tx.initiating_user,
        'paid_from_user': historical_tx.initiating_user,
    }
    if historical_tx.initiating_user:
        defaults['paid_source'] = 'account_wallet'

    record, _created = WeeklyAgentCommission.objects.update_or_create(
        agent=agent,
        period=period,
        defaults=defaults,
    )
    return record

def pay_weekly_commission(commission_record, actor=None):
    if commission_record.status == 'paid':
        return False, "Already paid"
    
    outstanding = (commission_record.commission_total_amount or Decimal('0.00')) - (commission_record.amount_paid or Decimal('0.00'))
    if outstanding <= 0:
        commission_record.status = 'paid'
        commission_record.paid_at = timezone.now()
        commission_record.amount_paid = commission_record.commission_total_amount or Decimal('0.00')
        commission_record.save(update_fields=['status', 'paid_at', 'amount_paid'])
        return True, "Marked as paid (No outstanding amount)"

    config = SiteConfiguration.load()
    account_user = None

    if config.commission_payment_source == 'account_wallet':
        if actor and getattr(actor, 'user_type', None) == 'account_user':
            account_user = actor
        else:
            account_user = User.objects.filter(user_type='account_user').first()
        if not account_user:
            return False, "No Account User found to fund commission."
        
        # Check balance (pre-check)
        payer_wallet, _ = Wallet.objects.get_or_create(user=account_user)
        if payer_wallet.balance < outstanding:
            return False, f"Insufficient funds in Account User wallet ({account_user.email})."

    with transaction.atomic():
        # Handle Payer Deduction
        if account_user and config.commission_payment_source == 'account_wallet':
            payer_wallet = Wallet.objects.select_for_update().get(user=account_user)
            if payer_wallet.balance < outstanding:
                # Should be caught by pre-check, but for safety in race conditions
                raise ValueError("Insufficient funds in Account User wallet during transaction.")

            payer_tx = Transaction.objects.create(
                user=account_user,
                initiating_user=actor if getattr(actor, "is_authenticated", False) else None,
                target_user=commission_record.agent,
                transaction_type='account_user_debit',
                amount=outstanding,
                is_successful=True,
                status='completed',
                description=f"Weekly Commission Payout for {commission_record.agent.email} ({commission_record.period})"
            )
            payer_wallet.apply_delta(
                amount=-outstanding,
                actor=actor,
                transaction_obj=payer_tx,
                reference=str(commission_record.pk),
                reason=payer_tx.description,
                metadata={"commission_id": commission_record.pk, "type": "weekly_commission"},
            )

        # Handle Payee Credit
        wallet, _ = Wallet.objects.select_for_update().get_or_create(user=commission_record.agent)
        payee_tx = Transaction.objects.create(
            user=commission_record.agent,
            initiating_user=actor if getattr(actor, "is_authenticated", False) else None,
            target_user=commission_record.agent,
            transaction_type='commission_payout',
            amount=outstanding,
            is_successful=True,
            status='completed',
            description=f"Weekly Commission for {commission_record.period}",
        )
        wallet.apply_delta(
            amount=outstanding,
            actor=actor,
            transaction_obj=payee_tx,
            reference=str(commission_record.pk),
            reason=payee_tx.description,
            metadata={"commission_id": commission_record.pk, "type": "weekly_commission"},
        )
        
        commission_record.amount_paid = (commission_record.amount_paid or Decimal('0.00')) + outstanding
        commission_record.status = 'paid'
        commission_record.amount_paid = commission_record.commission_total_amount or Decimal('0.00')
        commission_record.paid_at = timezone.now()
        if actor:
            commission_record.paid_by = actor
        commission_record.paid_source = (config.commission_payment_source or '').strip()
        if account_user:
            commission_record.paid_from_user = account_user
        elif actor:
            commission_record.paid_from_user = actor
        commission_record.save(update_fields=['status', 'paid_at', 'amount_paid', 'paid_by', 'paid_source', 'paid_from_user'])
        
    return True, "Paid successfully"

def pay_weekly_commission_amount(commission_record, amount, actor=None):
    if commission_record.status == 'paid':
        return False, "Already paid"

    outstanding = (commission_record.commission_total_amount or Decimal('0.00')) - (commission_record.amount_paid or Decimal('0.00'))
    if outstanding <= 0:
        commission_record.status = 'paid'
        commission_record.paid_at = timezone.now()
        commission_record.amount_paid = commission_record.commission_total_amount or Decimal('0.00')
        commission_record.save(update_fields=['status', 'paid_at', 'amount_paid'])
        return True, "Marked as paid (No outstanding amount)"

    try:
        amount = Decimal(str(amount))
    except Exception:
        return False, "Invalid amount"

    if amount <= 0:
        return False, "Amount must be greater than zero"

    pay_amount = amount if amount <= outstanding else outstanding

    config = SiteConfiguration.load()
    account_user = None

    if config.commission_payment_source == 'account_wallet':
        if actor and getattr(actor, 'user_type', None) == 'account_user':
            account_user = actor
        else:
            account_user = User.objects.filter(user_type='account_user').first()
        if not account_user:
            return False, "No Account User found to fund commission."

        payer_wallet, _ = Wallet.objects.get_or_create(user=account_user)
        if payer_wallet.balance < pay_amount:
            return False, f"Insufficient funds in Account User wallet ({account_user.email})."

    with transaction.atomic():
        if account_user and config.commission_payment_source == 'account_wallet':
            payer_wallet = Wallet.objects.select_for_update().get(user=account_user)
            if payer_wallet.balance < pay_amount:
                raise ValueError("Insufficient funds in Account User wallet during transaction.")

            payer_tx = Transaction.objects.create(
                user=account_user,
                initiating_user=actor if getattr(actor, "is_authenticated", False) else None,
                target_user=commission_record.agent,
                transaction_type='account_user_debit',
                amount=pay_amount,
                is_successful=True,
                status='completed',
                description=f"Adjusted Weekly Commission Payout for {commission_record.agent.email} ({commission_record.period})"
            )
            payer_wallet.apply_delta(
                amount=-pay_amount,
                actor=actor,
                transaction_obj=payer_tx,
                reference=str(commission_record.pk),
                reason=payer_tx.description,
                metadata={"commission_id": commission_record.pk, "type": "weekly_commission_adjusted"},
            )

        wallet, _ = Wallet.objects.select_for_update().get_or_create(user=commission_record.agent)
        payee_tx = Transaction.objects.create(
            user=commission_record.agent,
            initiating_user=actor if getattr(actor, "is_authenticated", False) else None,
            target_user=commission_record.agent,
            transaction_type='commission_payout',
            amount=pay_amount,
            is_successful=True,
            status='completed',
            description=f"Adjusted Weekly Commission for {commission_record.period}",
        )
        wallet.apply_delta(
            amount=pay_amount,
            actor=actor,
            transaction_obj=payee_tx,
            reference=str(commission_record.pk),
            reason=payee_tx.description,
            metadata={"commission_id": commission_record.pk, "type": "weekly_commission_adjusted"},
        )

        commission_record.amount_paid = (commission_record.amount_paid or Decimal('0.00')) + pay_amount
        commission_record.status = 'paid'
        commission_record.amount_paid = commission_record.commission_total_amount or Decimal('0.00')
        commission_record.paid_at = timezone.now()
        if actor:
            commission_record.paid_by = actor
        commission_record.paid_source = (config.commission_payment_source or '').strip()
        if account_user:
            commission_record.paid_from_user = account_user
        elif actor:
            commission_record.paid_from_user = actor
        commission_record.save(update_fields=['status', 'paid_at', 'amount_paid', 'paid_by', 'paid_source', 'paid_from_user'])

    if pay_amount != amount:
        return True, f"Paid ₦{pay_amount} (capped to outstanding)"
    return True, f"Paid ₦{pay_amount}"

def pay_monthly_network_commission(commission_record, actor=None):
    if commission_record.status == 'paid':
        return False, "Already paid"

    outstanding = (commission_record.commission_amount or Decimal('0.00')) - (commission_record.amount_paid or Decimal('0.00'))
    if outstanding <= 0:
        commission_record.status = 'paid'
        commission_record.paid_at = timezone.now()
        commission_record.amount_paid = commission_record.commission_amount or Decimal('0.00')
        commission_record.save(update_fields=['status', 'paid_at', 'amount_paid'])
        return True, "Marked as paid (No outstanding amount)"

    config = SiteConfiguration.load()
    account_user = None

    if config.commission_payment_source == 'account_wallet':
        if actor and getattr(actor, 'user_type', None) == 'account_user':
            account_user = actor
        else:
            account_user = User.objects.filter(user_type='account_user').first()
        if not account_user:
            return False, "No Account User found to fund commission."
        
        # Check balance (pre-check)
        payer_wallet, _ = Wallet.objects.get_or_create(user=account_user)
        if payer_wallet.balance < outstanding:
            return False, f"Insufficient funds in Account User wallet ({account_user.email})."

    with transaction.atomic():
        # Handle Payer Deduction
        if account_user and config.commission_payment_source == 'account_wallet':
            payer_wallet = Wallet.objects.select_for_update().get(user=account_user)
            if payer_wallet.balance < outstanding:
                 raise ValueError("Insufficient funds in Account User wallet during transaction.")

            payer_tx = Transaction.objects.create(
                user=account_user,
                initiating_user=actor if getattr(actor, "is_authenticated", False) else None,
                target_user=commission_record.user,
                transaction_type='account_user_debit',
                amount=outstanding,
                is_successful=True,
                status='completed',
                description=f"Monthly Network Commission Payout ({commission_record.role}) for {commission_record.user.email} ({commission_record.period})"
            )
            payer_wallet.apply_delta(
                amount=-outstanding,
                actor=actor,
                transaction_obj=payer_tx,
                reference=str(commission_record.pk),
                reason=payer_tx.description,
                metadata={"commission_id": commission_record.pk, "type": "monthly_network_commission", "role": commission_record.role},
            )

        # Handle Payee Credit
        wallet, _ = Wallet.objects.select_for_update().get_or_create(user=commission_record.user)
        payee_tx = Transaction.objects.create(
            user=commission_record.user,
            initiating_user=actor if getattr(actor, "is_authenticated", False) else None,
            target_user=commission_record.user,
            transaction_type='commission_payout',
            amount=outstanding,
            is_successful=True,
            status='completed',
            description=f"Monthly Network Commission ({commission_record.role}) for {commission_record.period}",
        )
        wallet.apply_delta(
            amount=outstanding,
            actor=actor,
            transaction_obj=payee_tx,
            reference=str(commission_record.pk),
            reason=payee_tx.description,
            metadata={"commission_id": commission_record.pk, "type": "monthly_network_commission", "role": commission_record.role},
        )
        
        commission_record.amount_paid = (commission_record.amount_paid or Decimal('0.00')) + outstanding
        if commission_record.amount_paid >= (commission_record.commission_amount or Decimal('0.00')):
            commission_record.amount_paid = commission_record.commission_amount or Decimal('0.00')
            commission_record.status = 'paid'
        else:
            commission_record.status = 'partially_paid'
        commission_record.paid_at = timezone.now()
        if actor:
            commission_record.paid_by = actor
        commission_record.paid_source = (config.commission_payment_source or '').strip()
        if account_user:
            commission_record.paid_from_user = account_user
        elif actor:
            commission_record.paid_from_user = actor
        commission_record.save(update_fields=['status', 'paid_at', 'amount_paid', 'paid_by', 'paid_source', 'paid_from_user'])
        
    return True, "Paid successfully"

def pay_monthly_network_commission_amount(commission_record, amount, actor=None):
    if commission_record.status == 'paid':
        return False, "Already paid"

    outstanding = (commission_record.commission_amount or Decimal('0.00')) - (commission_record.amount_paid or Decimal('0.00'))
    if outstanding <= 0:
        commission_record.status = 'paid'
        commission_record.paid_at = timezone.now()
        commission_record.amount_paid = commission_record.commission_amount or Decimal('0.00')
        commission_record.save(update_fields=['status', 'paid_at', 'amount_paid'])
        return True, "Marked as paid (No outstanding amount)"

    try:
        amount = Decimal(str(amount))
    except Exception:
        return False, "Invalid amount"

    if amount <= 0:
        return False, "Amount must be greater than zero"

    pay_amount = amount if amount <= outstanding else outstanding

    config = SiteConfiguration.load()
    account_user = None

    if config.commission_payment_source == 'account_wallet':
        if actor and getattr(actor, 'user_type', None) == 'account_user':
            account_user = actor
        else:
            account_user = User.objects.filter(user_type='account_user').first()
        if not account_user:
            return False, "No Account User found to fund commission."

        payer_wallet, _ = Wallet.objects.get_or_create(user=account_user)
        if payer_wallet.balance < pay_amount:
            return False, f"Insufficient funds in Account User wallet ({account_user.email})."

    with transaction.atomic():
        if account_user and config.commission_payment_source == 'account_wallet':
            payer_wallet = Wallet.objects.select_for_update().get(user=account_user)
            if payer_wallet.balance < pay_amount:
                raise ValueError("Insufficient funds in Account User wallet during transaction.")

            payer_tx = Transaction.objects.create(
                user=account_user,
                initiating_user=actor if getattr(actor, "is_authenticated", False) else None,
                target_user=commission_record.user,
                transaction_type='account_user_debit',
                amount=pay_amount,
                is_successful=True,
                status='completed',
                description=f"Adjusted Monthly Network Commission Payout ({commission_record.role}) for {commission_record.user.email} ({commission_record.period})"
            )
            payer_wallet.apply_delta(
                amount=-pay_amount,
                actor=actor,
                transaction_obj=payer_tx,
                reference=str(commission_record.pk),
                reason=payer_tx.description,
                metadata={"commission_id": commission_record.pk, "type": "monthly_network_commission_adjusted", "role": commission_record.role},
            )

        wallet, _ = Wallet.objects.select_for_update().get_or_create(user=commission_record.user)
        payee_tx = Transaction.objects.create(
            user=commission_record.user,
            initiating_user=actor if getattr(actor, "is_authenticated", False) else None,
            target_user=commission_record.user,
            transaction_type='commission_payout',
            amount=pay_amount,
            is_successful=True,
            status='completed',
            description=f"Adjusted Monthly Network Commission ({commission_record.role}) for {commission_record.period}",
        )
        wallet.apply_delta(
            amount=pay_amount,
            actor=actor,
            transaction_obj=payee_tx,
            reference=str(commission_record.pk),
            reason=payee_tx.description,
            metadata={"commission_id": commission_record.pk, "type": "monthly_network_commission_adjusted", "role": commission_record.role},
        )

        commission_record.amount_paid = (commission_record.amount_paid or Decimal('0.00')) + pay_amount
        if commission_record.amount_paid >= (commission_record.commission_amount or Decimal('0.00')):
            commission_record.amount_paid = commission_record.commission_amount or Decimal('0.00')
            commission_record.status = 'paid'
        else:
            commission_record.status = 'partially_paid'
        commission_record.paid_at = timezone.now()
        if actor:
            commission_record.paid_by = actor
        commission_record.paid_source = (config.commission_payment_source or '').strip()
        if account_user:
            commission_record.paid_from_user = account_user
        elif actor:
            commission_record.paid_from_user = actor
        commission_record.save(update_fields=['status', 'paid_at', 'amount_paid', 'paid_by', 'paid_source', 'paid_from_user'])

    if pay_amount != amount:
        return True, f"Paid ₦{pay_amount} (capped to outstanding)"
    return True, f"Paid ₦{pay_amount}"


def recall_commission(*, commission_type, commission_id, amount, reason, notes, actor, ip_address=None, device_info='', require_approval=False, other_reason_text='', recall_obj=None):
    now = timezone.now()
    amount = (amount or Decimal('0.00'))
    if amount <= 0:
        return False, "Recall amount must be greater than zero."

    if commission_type not in ['weekly', 'monthly']:
        return False, "Invalid commission type."

    if commission_type == 'weekly':
        commission_record = WeeklyAgentCommission.objects.select_related('agent', 'period', 'paid_from_user', 'paid_by').filter(id=commission_id).first()
        if not commission_record:
            return False, "Weekly commission not found."
        beneficiary = commission_record.agent
        period = commission_record.period
        total_amount = commission_record.commission_total_amount or Decimal('0.00')
        amount_paid = commission_record.amount_paid or Decimal('0.00')
        old_status = commission_record.status
    else:
        commission_record = MonthlyNetworkCommission.objects.select_related('user', 'period', 'paid_from_user', 'paid_by').filter(id=commission_id).first()
        if not commission_record:
            return False, "Monthly commission not found."
        beneficiary = commission_record.user
        period = commission_record.period
        total_amount = commission_record.commission_amount or Decimal('0.00')
        amount_paid = commission_record.amount_paid or Decimal('0.00')
        old_status = commission_record.status

    if amount_paid <= 0:
        return False, "Commission has no paid amount to recall."

    if amount > amount_paid:
        return False, f"Recall amount cannot exceed paid amount (₦{amount_paid})."

    recall = recall_obj
    if not recall:
        recall = CommissionRecall.objects.create(
            weekly_commission=commission_record if commission_type == 'weekly' else None,
            monthly_commission=commission_record if commission_type == 'monthly' else None,
            beneficiary=beneficiary,
            period=period,
            amount_requested=amount,
            recall_reason=reason,
            other_reason_text=(other_reason_text or '').strip(),
            notes=(notes or '').strip(),
            requested_by=actor,
            requested_by_role=getattr(actor, 'user_type', '') or '',
            ip_address=ip_address,
            device_info=(device_info or '')[:255],
            status='pending_approval' if require_approval and not (getattr(actor, 'is_superuser', False) or getattr(actor, 'user_type', '') in ['admin']) else 'executed',
            executed_at=None,
        )

    if not recall_obj and recall.status == 'pending_approval':
        try:
            admins = User.objects.filter(is_superuser=True, is_active=True)
            for admin_user in admins[:20]:
                create_notification(
                    recipient=admin_user,
                    notification_type='commission_recall',
                    title='Commission Recall Request',
                    message=f"{actor.username or actor.email} requested a commission recall of ₦{amount} for {beneficiary.username or beneficiary.email}.",
                    data={'recall_id': recall.id, 'commission_type': commission_type},
                )
        except Exception:
            pass
        return True, "Recall request created and pending approval."

    payer = None
    payer_source = (getattr(commission_record, 'paid_source', '') or '').strip()
    payer = getattr(commission_record, 'paid_from_user', None) or getattr(commission_record, 'paid_by', None)
    if not payer:
        payer = User.objects.filter(is_superuser=True, is_active=True).first()

    with transaction.atomic():
        agent_wallet = Wallet.objects.select_for_update().filter(user=beneficiary).first()
        if not agent_wallet:
            agent_wallet = Wallet.objects.create(user=beneficiary, balance=Decimal('0.00'))
            agent_wallet = Wallet.objects.select_for_update().get(user=beneficiary)
        if agent_wallet.balance < amount:
            recall.status = 'failed'
            recall.decision_note = 'Insufficient agent wallet balance for recall.'
            recall.save(update_fields=['status', 'decision_note'])
            return False, "Insufficient agent wallet balance to recall this amount."

        debit_tx = Transaction.objects.create(
            user=beneficiary,
            initiating_user=actor,
            target_user=payer,
            transaction_type='commission_recall_debit',
            amount=amount,
            is_successful=True,
            status='completed',
            description=f"Commission recall debit for {period} ({commission_type})",
            timestamp=now,
        )
        agent_wallet.apply_delta(
            amount=-amount,
            actor=actor,
            transaction_obj=debit_tx,
            reference=str(recall.id),
            reason=debit_tx.description,
            metadata={"recall_id": recall.id, "commission_type": commission_type, "payer_source": payer_source},
        )

        if payer:
            payer_wallet = Wallet.objects.select_for_update().filter(user=payer).first()
            if not payer_wallet:
                payer_wallet = Wallet.objects.create(user=payer, balance=Decimal('0.00'))
                payer_wallet = Wallet.objects.select_for_update().get(user=payer)
            credit_tx = Transaction.objects.create(
                user=payer,
                initiating_user=actor,
                target_user=beneficiary,
                transaction_type='commission_recall_credit',
                amount=amount,
                is_successful=True,
                status='completed',
                description=f"Commission recall credit from {beneficiary.username or beneficiary.email} ({period})",
                timestamp=now,
            )
            payer_wallet.apply_delta(
                amount=amount,
                actor=actor,
                transaction_obj=credit_tx,
                reference=str(recall.id),
                reason=credit_tx.description,
                metadata={"recall_id": recall.id, "commission_type": commission_type, "payer_source": payer_source},
            )

        new_amount_paid = amount_paid - amount
        if commission_type == 'weekly':
            WeeklyAgentCommission.objects.filter(id=commission_record.id).update(amount_paid=new_amount_paid)
        else:
            MonthlyNetworkCommission.objects.filter(id=commission_record.id).update(amount_paid=new_amount_paid)

        if new_amount_paid <= 0:
            new_status = 'pending'
        elif new_amount_paid >= total_amount:
            new_status = 'paid'
        else:
            new_status = 'partially_paid'

        if commission_type == 'weekly':
            WeeklyAgentCommission.objects.filter(id=commission_record.id).update(status=new_status)
        else:
            MonthlyNetworkCommission.objects.filter(id=commission_record.id).update(status=new_status)

        recall.executed_at = now
        recall.status = 'executed'
        recall.save(update_fields=['executed_at', 'status'])

        CommissionRecallLog.objects.create(
            recall=recall,
            weekly_commission=commission_record if commission_type == 'weekly' else None,
            monthly_commission=commission_record if commission_type == 'monthly' else None,
            agent=beneficiary,
            amount_recalled=amount,
            recall_reason=reason,
            notes=(notes or '').strip(),
            recalled_by=actor,
            recalled_by_role=getattr(actor, 'user_type', '') or '',
            recall_date=now.date(),
            recall_time=now.time(),
            ip_address=ip_address,
            device_info=(device_info or '')[:255],
            old_status=old_status,
            new_status=new_status,
            old_amount_paid=amount_paid,
            new_amount_paid=new_amount_paid,
            old_total_amount=total_amount,
            new_total_amount=total_amount,
        )

    try:
        create_notification(
            recipient=beneficiary,
            notification_type='commission_recall',
            title='Commission Recalled',
            message=f"₦{amount} was recalled for {period}. Reason: {reason.replace('_', ' ').title()}",
            data={'commission_type': commission_type, 'commission_id': commission_id, 'amount': str(amount)},
        )
    except Exception:
        pass

    try:
        create_notification(
            recipient=actor,
            notification_type='commission_recall',
            title='Commission Recall Executed',
            message=f"Recalled ₦{amount} from {beneficiary.username or beneficiary.email} for {period}.",
            data={'commission_type': commission_type, 'commission_id': commission_id, 'amount': str(amount), 'payer_source': payer_source},
        )
    except Exception:
        pass

    try:
        site_name = SiteConfiguration.load().site_name
        subject = "Commission Recalled"
        ctx = {
            'site_name': site_name,
            'beneficiary': beneficiary,
            'period': period,
            'amount_formatted': f"₦{amount:,.2f}",
            'reason_label': dict(CommissionRecall.RECALL_REASON_CHOICES).get(reason, reason),
            'status': 'PENDING',
            'notes': notes or '',
        }
        html = render_to_string('betting/email/commission_recalled.html', ctx)
        to_emails = []
        if getattr(beneficiary, 'email', None):
            to_emails.append(beneficiary.email)
        if getattr(actor, 'email', None) and actor.email not in to_emails:
            to_emails.append(actor.email)
        admin_email = User.objects.filter(is_superuser=True, is_active=True).values_list('email', flat=True).first()
        if admin_email and admin_email not in to_emails:
            to_emails.append(admin_email)
        if to_emails:
            msg = EmailMultiAlternatives(subject=subject, body=html, to=to_emails)
            msg.attach_alternative(html, "text/html")
            msg.send(fail_silently=True)
    except Exception:
        pass

    return True, "Commission recalled successfully"


def decide_commission_recall(*, recall_id, actor, decision, note=''):
    recall = CommissionRecall.objects.select_related('weekly_commission', 'monthly_commission', 'beneficiary', 'period', 'requested_by').filter(id=recall_id).first()
    if not recall:
        return False, "Recall request not found."

    if recall.status != 'pending_approval':
        return False, "Recall request is not pending approval."

    if decision not in ['approve', 'reject']:
        return False, "Invalid decision."

    if decision == 'reject':
        recall.status = 'rejected'
        recall.decided_by = actor
        recall.decided_at = timezone.now()
        recall.decision_note = (note or '').strip()
        recall.save(update_fields=['status', 'decided_by', 'decided_at', 'decision_note'])
        CommissionRecallApproval.objects.create(recall=recall, status='rejected', decided_by=actor, note=(note or '').strip())
        try:
            if recall.requested_by:
                create_notification(
                    recipient=recall.requested_by,
                    notification_type='commission_recall',
                    title='Commission Recall Rejected',
                    message=f"Recall request for ₦{recall.amount_requested} was rejected. {note}".strip(),
                    data={'recall_id': recall.id},
                )
        except Exception:
            pass
        return True, "Recall request rejected."

    commission_type = 'weekly' if recall.weekly_commission_id else 'monthly'
    commission_id = recall.weekly_commission_id or recall.monthly_commission_id
    ok, msg = recall_commission(
        commission_type=commission_type,
        commission_id=commission_id,
        amount=recall.amount_requested,
        reason=recall.recall_reason,
        notes=recall.notes,
        actor=actor,
        ip_address=recall.ip_address,
        device_info=recall.device_info,
        require_approval=False,
        other_reason_text=recall.other_reason_text,
        recall_obj=recall,
    )
    if not ok:
        recall.status = 'failed'
        recall.decided_by = actor
        recall.decided_at = timezone.now()
        recall.decision_note = (msg or '')[:255]
        recall.save(update_fields=['status', 'decided_by', 'decided_at', 'decision_note'])
        CommissionRecallApproval.objects.create(recall=recall, status='approved', decided_by=actor, note=(note or '').strip())
        return False, msg

    recall.decided_by = actor
    recall.decided_at = timezone.now()
    recall.decision_note = (note or '').strip()
    recall.save(update_fields=['decided_by', 'decided_at', 'decision_note'])
    CommissionRecallApproval.objects.create(recall=recall, status='approved', decided_by=actor, note=(note or '').strip())
    return True, "Recall request approved and executed."

def calculate_weekly_agent_commission_data(agent, period, include_breakdown=False):
    try:
        profile = agent.commission_profile
        plan = profile.plan
    except AgentCommissionProfile.DoesNotExist:
        logger.warning(f"Agent {agent.email} has no commission profile.")
        return None

    today = timezone.localdate()
    is_live_period = period.start_date <= today <= period.end_date

    # For the active weekly period, include newly placed/open tickets so the admin view updates live.
    excluded_statuses = _commission_excluded_ticket_statuses(is_live_period=is_live_period)

    # Find tickets: Cashiers under this agent
    tickets = BetTicket.objects.filter(
        user__agent=agent,
        placed_at__date__gte=period.start_date,
        placed_at__date__lte=period.end_date
    ).exclude(status__in=excluded_statuses)
    
    total_stake = (tickets.aggregate(Sum('stake_amount'))['stake_amount__sum'] or Decimal(0)).quantize(Decimal('0.01'))
    total_winnings = (tickets.filter(status='won').aggregate(Sum('max_winning'))['max_winning__sum'] or Decimal(0)).quantize(Decimal('0.01'))
    ggr = (total_stake - total_winnings).quantize(Decimal('0.01'))

    from django.db.models import Count, IntegerField
    from django.db.models.functions import Coalesce
    tickets_with_count = list(
        tickets.annotate(
            num_selections=Coalesce(
                "original_selections_count",
                Count("selections", distinct=True),
                output_field=IntegerField(),
            )
        )
    )

    single_stake = Decimal('0.00')
    single_winnings = Decimal('0.00')
    multiple_stake = Decimal('0.00')
    multiple_winnings = Decimal('0.00')

    hybrid_rules = list(plan.hybrid_rules.all().order_by("min_selections"))

    for ticket in tickets_with_count:
        stake = (ticket.stake_amount or Decimal('0.00'))
        winnings = (ticket.max_winning or Decimal('0.00')) if ticket.status == 'won' else Decimal('0.00')
        n = int(getattr(ticket, "num_selections", 0) or 0)
        if n <= 0:
            bt = (getattr(ticket, "bet_type", "") or "").strip().lower()
            n = 2 if bt in ("multiple", "system") else 1
        if n == 1:
            single_stake += stake
            single_winnings += winnings
        else:
            multiple_stake += stake
            multiple_winnings += winnings

    single_stake = single_stake.quantize(Decimal('0.01'))
    single_winnings = single_winnings.quantize(Decimal('0.01'))
    multiple_stake = multiple_stake.quantize(Decimal('0.01'))
    multiple_winnings = multiple_winnings.quantize(Decimal('0.01'))

    single_ggr = (single_stake - single_winnings).quantize(Decimal('0.01'))
    multiple_ggr = (multiple_stake - multiple_winnings).quantize(Decimal('0.01'))

    commission_single_amount = Decimal('0.00')
    if single_ggr > 0:
        single_pct = (plan.ggr_percent or Decimal('0.00'))
        if getattr(plan, "enable_single_selection_override", False):
            calc_type = (getattr(plan, "single_selection_calc_type", "") or "").strip().lower()
            val = (getattr(plan, "single_selection_value", None) or Decimal("0.00"))
            if calc_type == "percentage_ggr":
                single_pct = val
            elif calc_type == "percentage_stake":
                commission_single_amount = (single_stake * val / Decimal("100.00")).quantize(Decimal("0.01"))
                single_pct = None
            elif calc_type == "fixed_value":
                commission_single_amount = val.quantize(Decimal("0.01"))
                single_pct = None

        if single_pct is not None:
            commission_single_amount = (single_ggr * single_pct / Decimal('100.00')).quantize(Decimal('0.01'))

    commission_multiple_amount = Decimal('0.00')
    if hybrid_rules:
        commission_multiple_amount_raw = Decimal("0.00")

        def _match_hybrid_percent(selection_count: int) -> Decimal:
            pct = Decimal("0.00")
            for r in hybrid_rules:
                max_sel = getattr(r, "max_selections", None)
                if selection_count < int(getattr(r, "min_selections", 0) or 0):
                    continue
                if max_sel is not None and selection_count > int(max_sel):
                    continue
                pct = r.commission_percent or Decimal("0.00")
            return pct

        bucket_ggr_by_pct = {}
        for ticket in tickets_with_count:
            n = int(getattr(ticket, "num_selections", 0) or 0)
            if n <= 0:
                bt = (getattr(ticket, "bet_type", "") or "").strip().lower()
                n = 2 if bt in ("multiple", "system") else 1
            if n < 2:
                continue
            pct = _match_hybrid_percent(n)
            if pct <= 0:
                continue

            stake = (ticket.stake_amount or Decimal("0.00"))
            winnings = (ticket.max_winning or Decimal("0.00")) if ticket.status == "won" else Decimal("0.00")
            ticket_ggr = stake - winnings
            bucket_ggr_by_pct[pct] = bucket_ggr_by_pct.get(pct, Decimal("0.00")) + ticket_ggr

        for pct, bucket_ggr in bucket_ggr_by_pct.items():
            if bucket_ggr <= 0:
                continue
            commission_multiple_amount_raw += (bucket_ggr * pct / Decimal("100.00"))

        commission_multiple_amount = commission_multiple_amount_raw.quantize(Decimal("0.01"))
    elif getattr(plan, "is_hybrid_active", False) and multiple_ggr > 0 and (plan.ggr_percent or Decimal("0.00")) > 0:
        commission_multiple_amount = (multiple_ggr * (plan.ggr_percent or Decimal("0.00")) / Decimal("100.00")).quantize(Decimal("0.01"))

    commission_total_amount = (commission_single_amount + commission_multiple_amount).quantize(Decimal('0.01'))

    data = {
        'total_stake': total_stake,
        'total_winnings': total_winnings,
        'ggr': ggr,
        'single_stake': single_stake,
        'single_winnings': single_winnings,
        'single_ggr': single_ggr,
        'multiple_stake': multiple_stake,
        'multiple_winnings': multiple_winnings,
        'multiple_ggr': multiple_ggr,
        'commission_ggr_amount': commission_single_amount,
        'commission_hybrid_amount': commission_multiple_amount,
        'commission_single_amount': commission_single_amount,
        'commission_multiple_amount': commission_multiple_amount,
        'commission_total_amount': commission_total_amount,
        'is_live_period': is_live_period,
    }
    return data

def calculate_weekly_agent_commission(agent, period):
    existing = WeeklyAgentCommission.objects.filter(agent=agent, period=period).first()
    if existing and (existing.status == 'paid' or (existing.amount_paid or Decimal('0.00')) > 0):
        if existing.status != 'paid':
            existing.status = 'paid'
            existing.amount_paid = existing.commission_total_amount or Decimal('0.00')
            if not existing.paid_at:
                existing.paid_at = timezone.now()
            existing.save(update_fields=['status', 'amount_paid', 'paid_at'])
        return existing

    data = calculate_weekly_agent_commission_data(agent, period)
    historical_record = restore_historical_weekly_paid_commission_record(agent, period, calc_data=data)
    if historical_record:
        return historical_record
    if not data:
        return existing

    # This flag is only used by the admin live view and is not stored on the model.
    data.pop('is_live_period', None)

    record, created = WeeklyAgentCommission.objects.update_or_create(
        agent=agent,
        period=period,
        defaults=data
    )
    return record

def calculate_monthly_network_commission_data(user, period):
    from .models import NetworkCommissionSettings
    
    # Validate User Type
    if user.user_type not in ['super_agent', 'master_agent']:
        return None

    # Get Settings
    try:
        settings_obj = NetworkCommissionSettings.objects.get(role=user.user_type)
    except NetworkCommissionSettings.DoesNotExist:
        logger.warning(f"No NetworkCommissionSettings for role {user.user_type}")
        return None

    # Date Range
    start_date = period.start_date
    end_date = period.end_date

    # 1. Total Stake & Winnings (Downlines)
    # Tickets placed in this month
    excluded_statuses = _commission_excluded_ticket_statuses(is_live_period=False)
    if user.user_type == 'super_agent':
        tickets = BetTicket.objects.filter(
            Q(user=user) |
            Q(user__super_agent=user) |
            Q(user__agent__super_agent=user),
            placed_at__date__gte=start_date,
            placed_at__date__lte=end_date,
        ).exclude(status__in=excluded_statuses)
    elif user.user_type == 'master_agent':
        tickets = BetTicket.objects.filter(
            Q(user=user) |
            Q(user__master_agent=user) |
            Q(user__super_agent__master_agent=user) |
            Q(user__agent__super_agent__master_agent=user),
            placed_at__date__gte=start_date,
            placed_at__date__lte=end_date,
        ).exclude(status__in=excluded_statuses)
    
    downline_stake = tickets.aggregate(Sum('stake_amount'))['stake_amount__sum'] or Decimal(0)
    downline_winnings = tickets.filter(status='won').aggregate(Sum('max_winning'))['max_winning__sum'] or Decimal(0)

    # 2. Commissions Paid to Downlines
    downline_commissions = Decimal(0)

    # A. Agent Commissions (Weekly)
    # We sum WeeklyAgentCommission for periods ending in this month
    if user.user_type == 'super_agent':
        agent_comms = WeeklyAgentCommission.objects.filter(
            agent__super_agent=user,
            period__end_date__gte=start_date,
            period__end_date__lte=end_date
        )
    else: # master_agent
        agent_comms = WeeklyAgentCommission.objects.filter(
            agent__master_agent=user,
            period__end_date__gte=start_date,
            period__end_date__lte=end_date
        )
    
    downline_commissions += agent_comms.aggregate(Sum('commission_total_amount'))['commission_total_amount__sum'] or Decimal(0)

    # B. Super Agent Commissions (Only if user is Master Agent)
    if user.user_type == 'master_agent':
        # These are MonthlyNetworkCommission for Super Agents under this Master Agent
        # for the SAME period.
        # Note: This assumes Super Agent commissions have been calculated already.
        sa_comms = MonthlyNetworkCommission.objects.filter(
            user__master_agent=user,
            role='super_agent',
            period=period
        )
        downline_commissions += sa_comms.aggregate(Sum('commission_amount'))['commission_amount__sum'] or Decimal(0)

    # 3. NGR
    ngr = downline_stake - downline_winnings - downline_commissions

    # 4. Commission
    commission_amount = Decimal(0)
    if ngr > 0:
        commission_amount = (ngr * settings_obj.commission_percent / 100).quantize(Decimal('0.01'))

    return {
        'role': user.user_type,
        'downline_stake': downline_stake,
        'downline_winnings': downline_winnings,
        'downline_paid_commissions': downline_commissions,
        'ngr': ngr,
        'commission_percent': settings_obj.commission_percent,
        'commission_amount': commission_amount
    }

def calculate_monthly_network_commission(user, period):
    data = calculate_monthly_network_commission_data(user, period)
    if not data:
        return None
        
    record, created = MonthlyNetworkCommission.objects.update_or_create(
        user=user,
        period=period,
        defaults=data
    )
    return record


def _apply_recomputed_payment_status(record, *, total_amount_field):
    amount_paid = (getattr(record, 'amount_paid', None) or Decimal('0.00')).quantize(Decimal('0.01'))
    total_amount = (getattr(record, total_amount_field, None) or Decimal('0.00')).quantize(Decimal('0.01'))
    overpaid = amount_paid > total_amount

    if amount_paid <= Decimal('0.00'):
        return overpaid, False

    next_status = 'paid' if amount_paid >= total_amount else 'partially_paid'
    if record.status == next_status:
        return overpaid, False

    record.status = next_status
    return overpaid, True


@transaction.atomic
def recompute_saved_weekly_commission_record(record, *, persist=True):
    data = calculate_weekly_agent_commission_data(record.agent, record.period)
    if not data:
        return {'updated': False, 'overpaid': False}

    data.pop('is_live_period', None)
    dirty_fields = []
    for field, value in data.items():
        if getattr(record, field) != value:
            setattr(record, field, value)
            dirty_fields.append(field)

    overpaid, status_changed = _apply_recomputed_payment_status(
        record,
        total_amount_field='commission_total_amount',
    )
    if status_changed:
        dirty_fields.append('status')

    if dirty_fields and persist:
        record.save(update_fields=dirty_fields)

    return {'updated': bool(dirty_fields), 'overpaid': overpaid}


@transaction.atomic
def recompute_saved_monthly_commission_record(record, *, persist=True):
    data = calculate_monthly_network_commission_data(record.user, record.period)
    if not data:
        return {'updated': False, 'overpaid': False}

    dirty_fields = []
    for field, value in data.items():
        if getattr(record, field) != value:
            setattr(record, field, value)
            dirty_fields.append(field)

    overpaid, status_changed = _apply_recomputed_payment_status(
        record,
        total_amount_field='commission_amount',
    )
    if status_changed:
        dirty_fields.append('status')

    if dirty_fields and persist:
        record.save(update_fields=dirty_fields)

    return {'updated': bool(dirty_fields), 'overpaid': overpaid}


class CommissionCalculationService:
    @staticmethod
    def calculate_weekly_commissions(period):
        User = get_user_model()
        agents = User.objects.filter(user_type='agent', is_active=True)
        count = 0
        for agent in agents:
            try:
                calculate_weekly_agent_commission(agent, period)
                count += 1
            except Exception as e:
                logger.error(f"Failed to calculate commission for agent {agent.email}: {e}")
        
        # Mark period as processed only if we did something (or even if 0 agents, it is technically processed)
        period.is_processed = True
        period.processed_at = timezone.now()
        period.save()
        return count

    @staticmethod
    def calculate_monthly_commissions(period):
        User = get_user_model()
        # Process Super Agents first
        super_agents = User.objects.filter(user_type='super_agent', is_active=True)
        for sa in super_agents:
            try:
                calculate_monthly_network_commission(sa, period)
            except Exception as e:
                logger.error(f"Failed to calculate commission for super agent {sa.email}: {e}")
        
        # Then Master Agents (they might depend on Super Agents' data if we structured it that way, 
        # but the current logic sums payouts which are based on WeeklyAgentCommission, so order might not matter 
        # unless we subtract Super Agent commissions from Master Agent NGR - which we DO in line 230+)
        # So YES, Super Agents MUST be processed before Master Agents if Master Agent NGR depends on Super Agent payouts.
        # But wait, line 230 sums `MonthlyNetworkCommission`. So yes, Super Agents must be calculated first.
        
        master_agents = User.objects.filter(user_type='master_agent', is_active=True)
        for ma in master_agents:
            try:
                calculate_monthly_network_commission(ma, period)
            except Exception as e:
                logger.error(f"Failed to calculate commission for master agent {ma.email}: {e}")

        period.is_processed = True
        period.processed_at = timezone.now()
        period.save()
        return True

class CommissionPayoutService:
    @staticmethod
    def process_weekly_payouts(period):
        commissions = WeeklyAgentCommission.objects.filter(period=period, status__in=['pending', 'approved', 'partially_paid'])
        count = 0
        for comm in commissions:
            success, msg = pay_weekly_commission(comm)
            if success:
                count += 1
        return count

    @staticmethod
    def process_monthly_payouts(period):
        commissions = MonthlyNetworkCommission.objects.filter(period=period, status__in=['pending', 'approved', 'partially_paid'])
        count = 0
        for comm in commissions:
            success, msg = pay_monthly_network_commission(comm)
            if success:
                count += 1
        return count


class CommissionProfileAssignmentService:
    @staticmethod
    def _is_super_admin(user):
        if not user or not getattr(user, 'is_authenticated', False):
            return False
        return bool(getattr(user, 'is_superuser', False) or getattr(user, 'user_type', '') == 'admin')

    @staticmethod
    def _restriction_next_allowed(profile):
        try:
            from datetime import timedelta
            base = profile.last_changed_at or profile.assigned_at or timezone.now()
            return base + timedelta(days=30)
        except Exception:
            return None

    @staticmethod
    def assign_profile(*, agent, plan, actor, reason='', ip_address=None, device_info='', allow_override=False):
        if not agent or getattr(agent, 'user_type', None) != 'agent':
            return False, "Invalid agent.", None
        if not plan or not isinstance(plan, CommissionPlan):
            return False, "Invalid commission profile.", None

        now = timezone.now()
        actor_role = (getattr(actor, 'user_type', '') or '').strip()
        is_super = CommissionProfileAssignmentService._is_super_admin(actor)
        can_override = bool(allow_override and is_super)

        with transaction.atomic():
            existing = AgentCommissionProfile.objects.select_for_update().filter(user=agent).select_related('plan').first()
            prev_plan = existing.plan if existing else None

            if existing:
                if prev_plan and prev_plan.id == plan.id and existing.is_active:
                    return True, "No change (already assigned).", existing

                next_allowed = CommissionProfileAssignmentService._restriction_next_allowed(existing)
                if next_allowed and now < next_allowed and not can_override:
                    msg = (
                        "This agent's commission profile was recently modified. "
                        "Commission profiles can only be changed once every 30 days. "
                        "Please wait until the restriction period expires or contact the System Administrator."
                    )
                    return False, msg, existing

                existing.plan = plan
                existing.is_active = True
                existing.assigned_at = now
                existing.assigned_by = actor if actor and getattr(actor, 'is_authenticated', False) else None
                existing.assigned_by_role = actor_role
                existing.last_changed_at = now
                existing.last_changed_by = actor if actor and getattr(actor, 'is_authenticated', False) else None
                existing.last_change_reason = (reason or '')[:255]
                existing.updated_at = now
                existing.save()
                profile = existing
            else:
                profile = AgentCommissionProfile.objects.create(
                    user=agent,
                    plan=plan,
                    is_active=True,
                    assigned_at=now,
                    assigned_by=actor if actor and getattr(actor, 'is_authenticated', False) else None,
                    assigned_by_role=actor_role,
                    last_changed_at=now,
                    last_changed_by=actor if actor and getattr(actor, 'is_authenticated', False) else None,
                    last_change_reason=(reason or '')[:255],
                    updated_at=now,
                )

            CommissionProfileAssignmentLog.objects.create(
                agent=agent,
                previous_profile=prev_plan,
                new_profile=plan,
                assigned_by=actor if actor and getattr(actor, 'is_authenticated', False) else None,
                assigned_by_role=actor_role,
                assignment_reason=(reason or '')[:255],
                ip_address=ip_address,
                device_info=(device_info or '')[:2000],
                is_override=bool(can_override),
            )

            if can_override:
                CommissionOverrideLog.objects.create(
                    agent=agent,
                    old_profile=prev_plan,
                    new_profile=plan,
                    admin_user=actor if actor and getattr(actor, 'is_authenticated', False) else None,
                    reason=(reason or '')[:255],
                    ip_address=ip_address,
                    device_info=(device_info or '')[:2000],
                )

        try:
            from notifications.services import create_notification
            create_notification(
                recipient=agent,
                notification_type='SYSTEM_ANNOUNCEMENT',
                title='Commission profile updated',
                message=f"Your commission profile has been set to {plan.name}.",
            )
        except Exception:
            pass

        try:
            to_email = (getattr(agent, 'email', '') or '').strip()
            if to_email and '@' in to_email:
                try:
                    site = SiteConfiguration.load()
                    site_name = (getattr(site, 'site_name', '') or 'StakeNaija').strip() or 'StakeNaija'
                except Exception:
                    site_name = 'StakeNaija'
                subject = f"{site_name} • Commission Profile Assigned"
                text = f"Your commission profile has been set to {plan.name}."
                html = f"""
                <html><body style="font-family:Arial,sans-serif;">
                <div style="max-width:640px;margin:0 auto;padding:16px;">
                  <div style="background:#0b3d2e;color:#fff;padding:14px 16px;border-radius:12px;font-weight:800;">
                    {site_name}
                  </div>
                  <div style="margin-top:12px;background:#fff;border:1px solid #e9edf2;border-radius:12px;padding:16px;">
                    <div style="font-size:16px;font-weight:800;color:#101828;">Commission Profile Assigned</div>
                    <div style="margin-top:8px;color:#475467;font-size:13px;">Hello {agent.get_full_name() or agent.username or agent.email},</div>
                    <div style="margin-top:10px;color:#101828;font-size:14px;line-height:1.6;">
                      Your commission profile has been set to <b>{plan.name}</b>.
                    </div>
                  </div>
                </div>
                </body></html>
                """
                msg = EmailMultiAlternatives(subject=subject, body=text, to=[to_email])
                msg.attach_alternative(html, "text/html")
                msg.send(fail_silently=True)
        except Exception:
            pass

        return True, "Commission profile assigned.", profile
