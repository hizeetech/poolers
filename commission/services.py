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
)
from betting.models import Wallet, Transaction, BetTicket, SiteConfiguration
from django.db.models import Sum, Q
from decimal import Decimal
import logging
from django.contrib.auth import get_user_model
from django.core.mail import EmailMultiAlternatives

User = get_user_model()
logger = logging.getLogger(__name__)

def pay_weekly_commission(commission_record):
    if commission_record.status == 'paid':
        return False, "Already paid"
    
    if commission_record.commission_total_amount <= 0:
         commission_record.status = 'paid'
         commission_record.paid_at = timezone.now()
         commission_record.save()
         return True, "Marked as paid (Zero amount)"

    config = SiteConfiguration.load()
    account_user = None

    if config.commission_payment_source == 'account_wallet':
        account_user = User.objects.filter(user_type='account_user').first()
        if not account_user:
            return False, "No Account User found to fund commission."
        
        # Check balance (pre-check)
        payer_wallet, _ = Wallet.objects.get_or_create(user=account_user)
        if payer_wallet.balance < commission_record.commission_total_amount:
            return False, f"Insufficient funds in Account User wallet ({account_user.email})."

    with transaction.atomic():
        # Handle Payer Deduction
        if account_user and config.commission_payment_source == 'account_wallet':
            payer_wallet = Wallet.objects.select_for_update().get(user=account_user)
            if payer_wallet.balance < commission_record.commission_total_amount:
                # Should be caught by pre-check, but for safety in race conditions
                raise ValueError("Insufficient funds in Account User wallet during transaction.")
            
            payer_wallet.balance -= commission_record.commission_total_amount
            payer_wallet.save()

            Transaction.objects.create(
                user=account_user,
                transaction_type='account_user_debit',
                amount=commission_record.commission_total_amount,
                is_successful=True,
                status='completed',
                description=f"Weekly Commission Payout for {commission_record.agent.email} ({commission_record.period})"
            )

        # Handle Payee Credit
        wallet, _ = Wallet.objects.get_or_create(user=commission_record.agent)
        # Ensure balance is Decimal (handle float default edge case)
        if isinstance(wallet.balance, float):
            wallet.balance = Decimal(str(wallet.balance))
            
        wallet.balance += commission_record.commission_total_amount
        wallet.save()
        
        Transaction.objects.create(
            user=commission_record.agent,
            transaction_type='commission_payout',
            amount=commission_record.commission_total_amount,
            is_successful=True,
            status='completed',
            description=f"Weekly Commission for {commission_record.period}",
        )
        
        commission_record.status = 'paid'
        commission_record.paid_at = timezone.now()
        commission_record.save()
        
    return True, "Paid successfully"

def pay_monthly_network_commission(commission_record):
    if commission_record.status == 'paid':
        return False, "Already paid"

    if commission_record.commission_amount <= 0:
        commission_record.status = 'paid'
        commission_record.paid_at = timezone.now()
        commission_record.save()
        return True, "Marked as paid (Zero amount)"

    config = SiteConfiguration.load()
    account_user = None

    if config.commission_payment_source == 'account_wallet':
        account_user = User.objects.filter(user_type='account_user').first()
        if not account_user:
            return False, "No Account User found to fund commission."
        
        # Check balance (pre-check)
        payer_wallet, _ = Wallet.objects.get_or_create(user=account_user)
        if payer_wallet.balance < commission_record.commission_amount:
            return False, f"Insufficient funds in Account User wallet ({account_user.email})."

    with transaction.atomic():
        # Handle Payer Deduction
        if account_user and config.commission_payment_source == 'account_wallet':
            payer_wallet = Wallet.objects.select_for_update().get(user=account_user)
            if payer_wallet.balance < commission_record.commission_amount:
                 raise ValueError("Insufficient funds in Account User wallet during transaction.")
            
            payer_wallet.balance -= commission_record.commission_amount
            payer_wallet.save()

            Transaction.objects.create(
                user=account_user,
                transaction_type='account_user_debit',
                amount=commission_record.commission_amount,
                is_successful=True,
                status='completed',
                description=f"Monthly Network Commission Payout ({commission_record.role}) for {commission_record.user.email} ({commission_record.period})"
            )

        # Handle Payee Credit
        wallet, _ = Wallet.objects.get_or_create(user=commission_record.user)
        if isinstance(wallet.balance, float):
            wallet.balance = Decimal(str(wallet.balance))
            
        wallet.balance += commission_record.commission_amount
        wallet.save()
        
        Transaction.objects.create(
            user=commission_record.user,
            transaction_type='commission_payout',
            amount=commission_record.commission_amount,
            is_successful=True,
            status='completed',
            description=f"Monthly Network Commission ({commission_record.role}) for {commission_record.period}",
        )
        
        commission_record.status = 'paid'
        commission_record.paid_at = timezone.now()
        commission_record.save()
        
    return True, "Paid successfully"

def calculate_weekly_agent_commission_data(agent, period):
    try:
        profile = agent.commission_profile
        plan = profile.plan
    except AgentCommissionProfile.DoesNotExist:
        logger.warning(f"Agent {agent.email} has no commission profile.")
        return None

    # Find tickets: Cashiers under this agent
    tickets = BetTicket.objects.filter(
        user__agent=agent,
        placed_at__date__gte=period.start_date,
        placed_at__date__lte=period.end_date
    ).exclude(status__in=['pending', 'cancelled', 'deleted'])
    
    total_stake = (tickets.aggregate(Sum('stake_amount'))['stake_amount__sum'] or Decimal(0)).quantize(Decimal('0.01'))
    total_winnings = (tickets.filter(status='won').aggregate(Sum('max_winning'))['max_winning__sum'] or Decimal(0)).quantize(Decimal('0.01'))
    ggr = (total_stake - total_winnings).quantize(Decimal('0.01'))
    
    # GGR Commission
    ggr_comm = Decimal(0)
    if ggr > 0:
        ggr_comm = (ggr * plan.ggr_percent / 100).quantize(Decimal('0.01'))
    
    # Hybrid / Single Logic
    hybrid_comm = Decimal(0)
    
    if plan.is_hybrid_active or plan.enable_single_selection_override:
        # Optimisation: Fetch needed fields
        # selection count is Count('selections')
        from django.db.models import Count
        tickets_with_count = tickets.annotate(num_selections=Count('selections'))
        
        hybrid_rules = list(plan.hybrid_rules.all())
        
        for ticket in tickets_with_count:
            ticket_comm = Decimal(0)
            is_single = (ticket.num_selections == 1)
            
            # Single Override
            if is_single and plan.enable_single_selection_override:
                if plan.single_selection_calc_type == 'percentage_stake':
                    ticket_comm = (ticket.stake_amount * plan.single_selection_value / 100)
                elif plan.single_selection_calc_type == 'percentage_ggr':
                    # GGR of a single ticket = Stake - Winning
                    # If won, GGR is negative usually.
                    ticket_ggr = ticket.stake_amount - (ticket.max_winning if ticket.status == 'won' else 0)
                    if ticket_ggr > 0:
                        ticket_comm = (ticket_ggr * plan.single_selection_value / 100)
                elif plan.single_selection_calc_type == 'fixed_value':
                    ticket_comm = plan.single_selection_value
            
            # Hybrid (Multi-selection)
            elif plan.is_hybrid_active and not is_single:
                # Find matching rule
                for rule in hybrid_rules:
                    match = False
                    if rule.max_selections:
                        if rule.min_selections <= ticket.num_selections <= rule.max_selections:
                            match = True
                    else:
                        if ticket.num_selections >= rule.min_selections:
                            match = True
                    
                    if match:
                        ticket_comm = (ticket.stake_amount * rule.commission_percent / 100)
                        break
            
            hybrid_comm += ticket_comm

    hybrid_comm = hybrid_comm.quantize(Decimal('0.01'))
    
    total_comm = ggr_comm + hybrid_comm

    return {
        'total_stake': total_stake,
        'total_winnings': total_winnings,
        'ggr': ggr,
        'commission_ggr_amount': ggr_comm,
        'commission_hybrid_amount': hybrid_comm,
        'commission_total_amount': total_comm
    }

def calculate_weekly_agent_commission(agent, period):
    data = calculate_weekly_agent_commission_data(agent, period)
    if not data:
        return None
        
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
    if user.user_type == 'super_agent':
        tickets = BetTicket.objects.filter(
            user__super_agent=user,
            placed_at__date__gte=start_date,
            placed_at__date__lte=end_date
        ).exclude(status__in=['cancelled', 'deleted'])
    elif user.user_type == 'master_agent':
        tickets = BetTicket.objects.filter(
            user__master_agent=user,
            placed_at__date__gte=start_date,
            placed_at__date__lte=end_date
        ).exclude(status__in=['cancelled', 'deleted'])
    
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
        commissions = WeeklyAgentCommission.objects.filter(period=period, status='pending')
        count = 0
        for comm in commissions:
            success, msg = pay_weekly_commission(comm)
            if success:
                count += 1
        return count

    @staticmethod
    def process_monthly_payouts(period):
        commissions = MonthlyNetworkCommission.objects.filter(period=period, status='pending')
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
