import itertools
import math
import os
import re
import traceback
import secrets
import smtplib
from django.core.mail import send_mail
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib import messages
from django.conf import settings
from django.apps import apps
from django.db.models import Sum, Q, Case, When, F, DecimalField, Value, IntegerField
from django.db.models.functions import Cast, Coalesce
from django.db import transaction as db_transaction
from django.db.utils import OperationalError, ProgrammingError
from django.utils import timezone
from datetime import timedelta, date, datetime
import logging
import requests # For Paystack API calls
import json
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from decimal import Decimal, InvalidOperation # Import InvalidOperation
import uuid # For UUIDField
from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
from django.urls import reverse # Import reverse for dynamic URL lookup
from django.contrib.auth import authenticate, login, logout # Ensure these are imported
from django_ratelimit.decorators import ratelimit
from django.core.cache import cache
import hashlib

from risk.services import (
    is_suspended as risk_is_suspended,
    market_key_for_bet_type,
    selection_key_for_bet_type,
    auto_suspend_if_needed,
    compute_duplicate_ticket_signature,
    log_duplicate_ticket_if_needed,
    record_device_fingerprint,
    evaluate_ticket_risk,
    check_ip_intelligence,
)
from notifications.services import create_notification

from .models import (
    User, Wallet, Transaction, BettingPeriod, Fixture, Selection, BetTicket,
    BonusRule, SystemSetting, UserWithdrawal, AgentPayout, ActivityLog,
    CreditRequest, Loan, CreditLog, ImpersonationLog, ProcessedWithdrawal,
    SiteConfiguration, CarouselImage, PasswordResetRequest, FooterPage,
    BettingLimitAuditLog, GlobalBettingSettings, AgentBettingLimitOverride,
    CashierRegistrationRequest
)
from commission.models import WeeklyAgentCommission, MonthlyNetworkCommission
from pending_registration.models import PendingAgentRegistration
from .forms import (
    UserRegistrationForm, LoginForm, PasswordChangeForm, ProfileEditForm, 
    InitiateDepositForm, WithdrawFundsForm, WalletTransferForm,
    BetTicketForm, CheckTicketStatusForm, DeclareResultForm,
    AdminUserCreationForm, AdminUserChangeForm, WithdrawalActionForm,
    FixtureForm, BettingPeriodForm,
    AccountUserSearchForm, AccountUserWalletActionForm, SuperAdminFundAccountUserForm,
    CreditRequestForm, LoanSettlementForm, AdminManualWalletForm,
    ForgotPasswordForm, ResetPasswordForm, WithdrawalPinCreateForm, WithdrawalPinResetForm
)

# Setup logger for this app
logger = logging.getLogger('betting') # Use the 'betting' logger defined in settings.py


def _get_admin_notification_email():
    return os.getenv('ADMIN_NOTIFICATION_EMAIL') or settings.DEFAULT_FROM_EMAIL or settings.EMAIL_HOST_USER


def _notify_admin_deposit_success(user, transaction_record, amount, gateway):
    admin_email = _get_admin_notification_email()
    from_email = settings.DEFAULT_FROM_EMAIL or settings.EMAIL_HOST_USER
    if not admin_email or not from_email:
        return

    identifier = user.email or user.username or f"user#{user.pk}"
    subject = f"Deposit Successful ({gateway.capitalize()}): ₦{amount:.2f}"
    message = (
        f"User: {identifier}\n"
        f"Amount: ₦{amount:.2f}\n"
        f"Gateway: {gateway}\n"
        f"Reference: {transaction_record.external_reference}\n"
        f"Time: {timezone.now().strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
    )
    send_mail(
        subject=subject,
        message=message,
        from_email=from_email,
        recipient_list=[admin_email],
        fail_silently=True
    )


def _build_agent_total_wallet_map(agents_qs):
    agent_ids = list(agents_qs.values_list('id', flat=True))
    if not agent_ids:
        return {}

    agent_wallet_map = {
        row['user_id']: row['balance']
        for row in Wallet.objects.filter(user_id__in=agent_ids).values('user_id', 'balance')
    }
    cashier_totals = {
        row['user__agent_id']: row['total']
        for row in Wallet.objects.filter(user__user_type='cashier', user__agent_id__in=agent_ids)
        .values('user__agent_id')
        .annotate(total=Coalesce(Sum('balance'), Value(0), output_field=DecimalField()))
    }

    totals = {}
    for agent_id in agent_ids:
        totals[agent_id] = (agent_wallet_map.get(agent_id) or Decimal('0.00')) + (cashier_totals.get(agent_id) or Decimal('0.00'))
    return totals


def _build_super_agent_total_wallet_map(super_agents_qs):
    sa_ids = list(super_agents_qs.values_list('id', flat=True))
    if not sa_ids:
        return {}

    sa_wallet_map = {
        row['user_id']: row['balance']
        for row in Wallet.objects.filter(user_id__in=sa_ids).values('user_id', 'balance')
    }

    agent_totals = {
        row['super_agent_id']: row['total']
        for row in User.objects.filter(user_type='agent', super_agent_id__in=sa_ids)
        .values('super_agent_id')
        .annotate(total=Coalesce(Sum('wallet__balance'), Value(0), output_field=DecimalField()))
    }
    cashier_under_agents_totals = {
        row['agent__super_agent_id']: row['total']
        for row in User.objects.filter(user_type='cashier', agent__super_agent_id__in=sa_ids)
        .values('agent__super_agent_id')
        .annotate(total=Coalesce(Sum('wallet__balance'), Value(0), output_field=DecimalField()))
    }
    direct_cashier_totals = {
        row['super_agent_id']: row['total']
        for row in User.objects.filter(user_type='cashier', super_agent_id__in=sa_ids, agent__isnull=True)
        .values('super_agent_id')
        .annotate(total=Coalesce(Sum('wallet__balance'), Value(0), output_field=DecimalField()))
    }

    totals = {}
    for sa_id in sa_ids:
        totals[sa_id] = (
            (sa_wallet_map.get(sa_id) or Decimal('0.00'))
            + (agent_totals.get(sa_id) or Decimal('0.00'))
            + (cashier_under_agents_totals.get(sa_id) or Decimal('0.00'))
            + (direct_cashier_totals.get(sa_id) or Decimal('0.00'))
        )
    return totals


def _get_wallet_balance_map(user_ids):
    ids = [int(i) for i in user_ids if i]
    if not ids:
        return {}
    return {
        row["user_id"]: row["balance"]
        for row in Wallet.objects.filter(user_id__in=ids).values("user_id", "balance")
    }


# --- Helper Functions for User Permissions and Logging ---

def is_admin(user):
    return user.is_authenticated and user.user_type == 'admin'

def is_master_agent(user):
    return user.is_authenticated and user.user_type == 'master_agent'

def is_super_agent(user):
    return user.is_authenticated and user.user_type == 'super_agent'

def is_agent(user):
    return user.is_authenticated and user.user_type == 'agent'

from .utils import (
    get_client_ip,
    get_active_bonus_rules_cached,
    select_bonus_rule,
    compute_bonus_amount,
    get_effective_betting_limits_for_user,
    acquire_ticket_placement_lock,
    release_ticket_placement_lock,
    validate_ticket_against_limits,
    BettingLimitViolation,
    serialize_limits,
    system_bet_payout_projections,
)
from .services.usernames import generate_cashier_email

def is_cashier(user):
    return user.is_authenticated and user.user_type == 'cashier'

def is_player(user):
    return user.is_authenticated and user.user_type == 'player'

def is_account_user(user):
    return user.is_authenticated and user.user_type == 'account_user'


def log_admin_activity(request, action_description, action_type='UPDATE', affected_object=None):
    """Logs administrative actions."""
    if request.user.is_authenticated and (request.user.is_superuser or request.user.user_type in ['admin', 'account_user']):
        ActivityLog.objects.create(
            user=request.user,
            action=action_description,
            action_type=action_type, # Default to UPDATE for generic admin actions
            ip_address=get_client_ip(request),
            user_agent=request.META.get('HTTP_USER_AGENT', 'Unknown'),
            path=request.path,
            affected_object=affected_object
        )

# --- General Authentication Views ---

def frontpage(request):
    carousel_images = CarouselImage.objects.filter(is_active=True)
    context = {
        'carousel_images': carousel_images,
    }
    return render(request, 'betting/frontpage.html', context)

def register_user(request):
    if request.method == 'POST':
        form = UserRegistrationForm(request.POST, request=request) # Pass request to form
        if form.is_valid():
            user = form.save(request=request) # Pass request to form's save method for messages
            user_type = form.cleaned_data.get('user_type')
            if user_type == 'agent' and user is None:
                messages.success(request, 'Registration submitted for approval. Login details will be sent after admin approval.')
                return redirect('betting:login')
            messages.success(request, 'Registration successful. Please log in.')
            return redirect('betting:login')
        else:
            # Handle field errors
            for field, errors in form.errors.items():
                for error in errors:
                    messages.error(request, f"{field.replace('_', ' ').title()}: {error}")
            # Handle non-field errors
            if form.non_field_errors():
                for error in form.non_field_errors():
                    messages.error(request, f"Error: {error}")
    else:
        form = UserRegistrationForm()
    return render(request, 'betting/register.html', {'form': form})


@ratelimit(key='ip', rate='5/m', method='POST', block=True)
def user_login(request):
    logger.debug("Entering user_login view.")
    if request.method == 'POST':
        form = LoginForm(request=request, data=request.POST)
        
        if form.is_valid():
            logger.debug("LoginForm is valid.")
            user = form.get_user()
            logger.debug(f"Attempting to authenticate user: {getattr(user, 'email', None)}")
            if user is not None:
                logger.debug(f"Authentication successful for user: {user.email}. User ID: {user.id}")
                login(request, user)
                logger.debug(f"User {user.email} logged in. Redirecting...")
                messages.success(request, f'Welcome, {user.first_name or user.email}!')
                
                if user.is_superuser or user.user_type == 'admin':
                    return redirect('betting_admin:dashboard')
                elif user.user_type == 'master_agent':
                    return redirect('betting:master_agent_dashboard')
                elif user.user_type == 'super_agent':
                    return redirect('betting:super_agent_dashboard')
                elif user.user_type == 'account_user':
                    return redirect('betting:account_user_dashboard')
                elif user.user_type == 'agent':
                    return redirect('betting:agent_dashboard')
                elif user.user_type == 'cashier':
                    return redirect('betting:wallet')
                else: # Player or unassigned type
                    return redirect('betting:fixtures')
            else: # This block is theoretically unreachable if form.is_valid() implies user is not None
                logger.debug("Authentication failed. User is None after form.is_valid().")
                messages.error(request, 'An unexpected authentication error occurred. Please try again.')
        else:
            logger.debug("LoginForm is NOT valid.")
            # Errors are handled by the form instance in the template
            pass
    else:
        logger.debug("GET request for login page.")
        form = LoginForm()
    return render(request, 'betting/login.html', {'form': form})


def forgot_password(request):
    if request.method == 'POST':
        form = ForgotPasswordForm(request.POST)
        if form.is_valid():
            email = form.cleaned_data['email']
            user = User.objects.filter(email__iexact=email).first()
            
            if not user:
                # Return JSON for SweetAlert in the template
                return JsonResponse({'status': 'error', 'message': 'Email not found in our database.'})
            
            # Create Reset Request
            token = secrets.token_urlsafe(32)
            expires_at = timezone.now() + timedelta(hours=2)
            
            reset_request = PasswordResetRequest.objects.create(
                email=email,
                token=token,
                user=user,
                expires_at=expires_at,
                ip_address=get_client_ip(request),
                user_agent=request.META.get('HTTP_USER_AGENT', 'Unknown')
            )
            
            # Send Email
            reset_url = request.build_absolute_uri(
                reverse('betting:reset_password', kwargs={'token': token})
            )
            
            subject = "Password Reset Request - StakeNaija"
            message = f"Hello {user.first_name},\n\nYou requested to reset your password. Click the link below to set a new password:\n\n{reset_url}\n\nThis link expires in 2 hours.\n\nIf you didn't request this, please ignore this email."
            
            try:
                from_email = settings.DEFAULT_FROM_EMAIL or settings.EMAIL_HOST_USER or f"no-reply@{request.get_host().split(':')[0]}"

                use_console_backend = settings.DEBUG and (
                    not getattr(settings, 'EMAIL_HOST', None)
                    or not getattr(settings, 'EMAIL_HOST_USER', None)
                    or not getattr(settings, 'EMAIL_HOST_PASSWORD', None)
                    or not from_email
                )

                if use_console_backend:
                    from django.core.mail import get_connection
                    connection = get_connection('django.core.mail.backends.console.EmailBackend')
                else:
                    from django.core.mail import get_connection
                    connection = get_connection()

                send_mail(
                    subject,
                    message,
                    from_email,
                    [email],
                    fail_silently=False,
                    connection=connection,
                )
                reset_request.email_sent = True
                reset_request.sent_at = timezone.now()
                reset_request.send_error = None
                reset_request.save(update_fields=['email_sent', 'sent_at', 'send_error'])
                return JsonResponse({'status': 'success', 'message': 'A reset link has been sent to your email.'})
            except smtplib.SMTPAuthenticationError as e:
                logger.exception(f"Email sending failed: {str(e)}")
                reset_request.email_sent = False
                reset_request.send_error = str(e)
                reset_request.save(update_fields=['email_sent', 'send_error'])

                if settings.DEBUG:
                    from django.core.mail import get_connection
                    connection = get_connection('django.core.mail.backends.console.EmailBackend')
                    send_mail(
                        subject,
                        message,
                        settings.DEFAULT_FROM_EMAIL or f"no-reply@{request.get_host().split(':')[0]}",
                        [email],
                        fail_silently=True,
                        connection=connection,
                    )
                    return JsonResponse({'status': 'success', 'message': 'Email delivery is not configured on this server. A reset link was generated (check server console output).'})

                return JsonResponse({'status': 'error', 'message': 'Failed to send reset email. Please try again later.'})
            except Exception as e:
                logger.exception(f"Email sending failed: {str(e)}")
                reset_request.email_sent = False
                reset_request.send_error = str(e)
                reset_request.save(update_fields=['email_sent', 'send_error'])
                return JsonResponse({'status': 'error', 'message': 'Failed to send reset email. Please try again later.'})
    else:
        form = ForgotPasswordForm()
    return render(request, 'betting/forgot_password.html', {'form': form})


def reset_password(request, token):
    reset_request = get_object_or_404(PasswordResetRequest, token=token)
    
    if not reset_request.is_valid():
        messages.error(request, "This reset link has expired or already been used.")
        return redirect('betting:forgot_password')
        
    if request.method == 'POST':
        form = ResetPasswordForm(request.POST)
        if form.is_valid():
            user = reset_request.user
            user.set_password(form.cleaned_data['password'])
            user.save()
            
            # Mark request as used
            reset_request.is_used = True
            reset_request.save()
            
            messages.success(request, "Password reset successful! You can now login with your new password.")
            return redirect('betting:login')
    else:
        form = ResetPasswordForm()
        
    return render(request, 'betting/reset_password.html', {'form': form, 'token': token})


@login_required
def user_logout(request):
    logout(request)
    messages.info(request, 'You have been logged out.')
    return redirect('betting:frontpage')

# --- Fixtures & Betting Views ---

def _get_fixtures_data(period_id=None):
    """
    Helper to fetch fixtures and the current betting period.
    Returns a tuple: (fixtures, current_betting_period)
    """
    current_betting_period = None
    fixtures = Fixture.objects.none()

    if period_id:
        current_betting_period = get_object_or_404(BettingPeriod, id=period_id)
        fixtures = Fixture.objects.filter(betting_period=current_betting_period).annotate(
            serial_int=Cast('serial_number', IntegerField())
        ).order_by('serial_int')
    else:
        # Get the latest open betting period
        current_betting_period = BettingPeriod.objects.filter(
            start_date__lte=timezone.now().date(),
            end_date__gte=timezone.now().date(),
            is_active=True
        ).order_by('-start_date').first()

        # Fallback: if no currently running period, get the next upcoming active period
        if not current_betting_period:
            current_betting_period = BettingPeriod.objects.filter(
                start_date__gt=timezone.now().date(),
                is_active=True
            ).order_by('start_date').first()
        
        # Fallback 2: if still no period, get the latest active period (could be past)
        if not current_betting_period:
            current_betting_period = BettingPeriod.objects.filter(
                is_active=True
            ).order_by('-start_date').first()

        if current_betting_period:
            fixtures = Fixture.objects.filter(betting_period=current_betting_period).annotate(
                serial_int=Cast('serial_number', IntegerField())
            ).order_by('serial_int')

    # Filter out fixtures that are not active or have invalid status
    if fixtures.exists():
        fixtures = fixtures.filter(is_active=True).exclude(status__in=['cancelled', 'finished', 'settled', 'postponed'])

        # Filter out fixtures that have already started (Date/Time check)
    # We compare against local time because match_date/time are typically stored as wall-clock time
    local_now = timezone.localtime(timezone.now())
    fixtures = fixtures.filter(
       Q(match_date__gt=local_now.date()) | 
       Q(match_date=local_now.date(), match_time__gt=local_now.time())
    )
        
    return fixtures, current_betting_period

def calculate_bonus_amount(potential_winning, stake_amount, selections, bet_type):
    odds = []
    for s in selections:
        if isinstance(s, dict):
            odds.append(s.get('odd', Decimal('0.00')))
        elif hasattr(s, 'odd_selected'):
            odds.append(s.odd_selected)
        else:
            odds.append(Decimal('0.00'))

    rule = select_bonus_rule(bet_type, len(selections), odds)
    if not rule:
        return None, Decimal('0.00'), Decimal('0.00'), Decimal('0.0000')

    pct = rule.get('pct', Decimal('0.0000'))
    base_amount = Decimal(str(potential_winning or 0))
    if rule.get('base') == 'net':
        base_amount = base_amount - Decimal(str(stake_amount or 0))
        if base_amount < 0:
            base_amount = Decimal('0.00')

    bonus_amount = compute_bonus_amount(base_amount, pct, rule.get('cap'))
    return rule, base_amount, bonus_amount, pct

def fixtures_view(request, period_id=None):
    fixtures, current_betting_period = _get_fixtures_data(period_id)

    all_periods = BettingPeriod.objects.all().order_by('-start_date')
    active_periods = BettingPeriod.objects.filter(is_active=True).order_by('-start_date')

    bonus_rules_data = []
    for r in get_active_bonus_rules_cached():
        bonus_rules_data.append({
            'id': r['id'],
            'min': r['min'],
            'max': r['max'],
            'min_odd': float(r['min_odd']),
            'pct': float(r['pct']),
            'cap': float(r['cap']) if r['cap'] is not None else None,
            'base': r['base'],
            'allow_system': r['allow_system'],
            'allow_acca': r['allow_acca'],
            'allow_single': r['allow_single'],
        })

    context = {
        'fixtures': fixtures,
        'current_betting_period': current_betting_period,
        'all_periods': all_periods,
        'active_periods': active_periods,
        'bet_ticket_form': BetTicketForm(), # For placing single bets on fixture page
        'can_place_bet': is_cashier(request.user),
        'bonus_rules_json': json.dumps(bonus_rules_data),
    }
    return render(request, 'betting/fixtures.html', context)

def fixtures_list_partial(request, period_id=None):
    """
    Returns only the HTML for the fixtures list, used for AJAX polling.
    """
    fixtures, current_betting_period = _get_fixtures_data(period_id)
    
    context = {
        'fixtures': fixtures,
        'current_betting_period': current_betting_period,
    }
    return render(request, 'betting/includes/fixtures_list.html', context)


@login_required
@ratelimit(key='user', rate='10/m', method='POST', block=True)
@db_transaction.atomic
def place_bet(request):
    try:
        if request.method == 'POST':
            # Debug logging
            logger.info(f"Place Bet Request Keys: {list(request.POST.keys())}")

            if not is_cashier(request.user):
                if request.headers.get('x-requested-with') == 'XMLHttpRequest' or 'application/json' in request.META.get('HTTP_ACCEPT', ''):
                    return JsonResponse({'success': False, 'message': 'You are not authorized to place a bet'})
                messages.error(request, 'You are not authorized to place a bet')
                return redirect('betting:fixtures')

            # Check if this is the new JS-based bet placement
            if 'selections' in request.POST:
                try:
                    selections_data = json.loads(request.POST.get('selections'))
                    stake_amount_str = request.POST.get('stake_amount')
                    if not stake_amount_str:
                         return JsonResponse({'success': False, 'message': 'Stake amount is missing.'})
                    stake_amount_per_line = Decimal(stake_amount_str)
                    
                    is_system_bet = request.POST.get('is_system_bet') == 'true'
                    permutation_count = int(request.POST.get('permutation_count', 0))

                    if not selections_data:
                        return JsonResponse({'success': False, 'message': 'No bets selected.'})

                    # --- IDEMPOTENCY CHECK ---
                    # Create a unique hash for this bet request to prevent double submission
                    idempotency_key = None
                    try:
                        # Ensure stable sorting for JSON hash
                        idempotency_payload = f"bet-{request.user.id}-{json.dumps(selections_data, sort_keys=True)}-{stake_amount_str}-{is_system_bet}-{permutation_count}"
                        idempotency_key = hashlib.sha256(idempotency_payload.encode('utf-8')).hexdigest()
                        
                        if cache.get(idempotency_key):
                            logger.warning(f"Duplicate bet placement blocked for user {request.user.id}")
                            return JsonResponse({'success': False, 'message': 'Duplicate bet detected. Please wait a moment.'})
                        
                        # Lock for 30 seconds
                        cache.set(idempotency_key, True, timeout=30)
                    except Exception as e:
                        logger.error(f"Idempotency check error: {e}")
                        idempotency_key = None # Ensure it's None if check failed
                        # Continue if check fails, don't block user
                    # -------------------------

                    placement_lock_key = None

                    # Helper to clear lock on failure
                    def fail_response(message):
                        if idempotency_key:
                            cache.delete(idempotency_key)
                        if placement_lock_key:
                            release_ticket_placement_lock(placement_lock_key)
                        return JsonResponse({'success': False, 'message': message})

                    placement_lock_key = acquire_ticket_placement_lock(request.user.id)
                    if not placement_lock_key:
                        return fail_response('Too many requests. Please retry.')

                    # Basic Validation of selections
                    valid_selections = []
                    for sel in selections_data:
                        # Support both camelCase (legacy/API) and snake_case (frontend) keys
                        fixture_id = sel.get('fixtureId') or sel.get('fixture_id')
                        if not fixture_id:
                            return fail_response('Missing fixture ID.')

                        try:
                            fixture = Fixture.objects.get(id=fixture_id)
                        except Fixture.DoesNotExist:
                            return fail_response('Fixture not found.')

                        # Validate fixture status
                        if fixture.status != 'scheduled': # Assuming 'scheduled' is the status for open matches
                            return fail_response(f'Betting closed for {fixture.home_team} vs {fixture.away_team}')
                        
                        # Validate match time (Strict Date/Time Enforcement)
                        local_now = timezone.localtime(timezone.now())
                        if fixture.match_date < local_now.date() or (fixture.match_date == local_now.date() and fixture.match_time <= local_now.time()):
                             return fail_response(f'Betting closed for {fixture.home_team} vs {fixture.away_team} (Match Started)')

                        if not fixture.betting_period.is_active:
                                return fail_response(f'Betting period closed for {fixture.home_team} vs {fixture.away_team}')
                        
                        # Validate odds and outcome
                        outcome = sel.get('outcome') or sel.get('bet_type')
                        if not outcome:
                            return fail_response('Missing bet outcome.')

                        # Normalize outcome keys (frontend vs backend mismatch)
                        outcome_map = {
                            'over1_5': 'over_1_5',
                            'under1_5': 'under_1_5',
                            'over2_5': 'over_2_5',
                            'under2_5': 'under_2_5',
                            'over3_5': 'over_3_5',
                            'under3_5': 'under_3_5',
                        }
                        outcome = outcome_map.get(outcome, outcome)

                        # Verify odd matches server side to prevent tampering (simplified check)
                        # For now we assume the odd sent is correct or we re-fetch. 
                        # Better to re-fetch.
                        if outcome == 'home_win': odd = fixture.home_win_odd
                        elif outcome == 'draw': odd = fixture.draw_odd
                        elif outcome == 'away_win': odd = fixture.away_win_odd
                        elif outcome == 'home_dnb': odd = fixture.home_dnb_odd
                        elif outcome == 'away_dnb': odd = fixture.away_dnb_odd
                        elif outcome == 'over_1_5': odd = fixture.over_1_5_odd
                        elif outcome == 'under_1_5': odd = fixture.under_1_5_odd
                        elif outcome == 'over_2_5': odd = fixture.over_2_5_odd
                        elif outcome == 'under_2_5': odd = fixture.under_2_5_odd
                        elif outcome == 'over_3_5': odd = fixture.over_3_5_odd
                        elif outcome == 'under_3_5': odd = fixture.under_3_5_odd
                        elif outcome == 'btts_yes': odd = fixture.btts_yes_odd
                        elif outcome == 'btts_no': odd = fixture.btts_no_odd
                        else:
                            return fail_response(f'Invalid outcome for {fixture.home_team} vs {fixture.away_team}')

                        market_key = market_key_for_bet_type(outcome)
                        selection_key = selection_key_for_bet_type(outcome)
                        if risk_is_suspended(fixture.id, market_key, selection_key):
                            return fail_response('This event/market is temporarily suspended due to risk management.')

                        valid_selections.append({
                            'fixture': fixture,
                            'bet_type': outcome,
                            'odd': odd,
                            'market_key': market_key,
                            'selection_key': selection_key,
                        })

                    # --- Bet Permission Validation ---
                    config = SiteConfiguration.objects.first()
                    if config:
                        num_selections = len(valid_selections)
                        if num_selections == 1 and not config.allow_single_bet:
                            return fail_response('Single bets are currently disabled. Please add more selections.')
                        elif num_selections == 2 and not config.allow_double_bet:
                            return fail_response('Double bets are currently disabled. Please add more selections.')
                        elif num_selections >= 3 and not config.allow_multiple_bet:
                            return fail_response('Multiple bets are currently disabled.')
                    # ---------------------------------

                    # Calculate Total Stake and Combinations
                    
                    if is_system_bet and len(valid_selections) >= 3:
                        # System Bet Logic
                        if permutation_count < 2 or permutation_count > len(valid_selections):
                                return fail_response('Invalid permutation count.')

                        num_lines = math.comb(len(valid_selections), permutation_count)
                        total_stake = stake_amount_per_line * Decimal(num_lines)
                        
                        bet_type = 'system'
                        system_min_count = permutation_count
                    else:
                        # Single or Accumulator Logic (1 line)
                        num_lines = 1
                        total_stake = stake_amount_per_line
                        
                        bet_type = 'multiple' if len(valid_selections) > 1 else 'single'
                        system_min_count = None

                    # Calculate Potential Winning (Max Winning) for the single ticket
                    if bet_type == 'system':
                        k = int(system_min_count or 0)
                        odds_list = [s['odd'] for s in valid_selections]
                        projections = system_bet_payout_projections(odds_list, stake_amount_per_line, k)
                        potential_win = projections['max_potential_winning']
                        min_potential_win = projections['min_potential_winning']
                        total_ticket_odd = Decimal('0.00')
                        max_line_odd = projections['max_line_odd']
                    else:
                        total_ticket_odd = Decimal('1.00')
                        for sel in valid_selections:
                            total_ticket_odd *= sel['odd']
                        potential_win = (stake_amount_per_line * total_ticket_odd).quantize(Decimal('0.01'))
                        min_potential_win = potential_win
                        max_line_odd = total_ticket_odd.quantize(Decimal('0.01'))

                    odds_list = [s['odd'] for s in valid_selections]
                    rule_dict, bonus_base_amount, estimated_bonus_amount, applied_pct = calculate_bonus_amount(potential_win, total_stake, valid_selections, bet_type)
                    
                    # Calculate Min Winning with its own bonus
                    min_winning = min_potential_win
                    if rule_dict:
                        pct = rule_dict.get('pct', Decimal('0.0000'))
                        min_base = min_potential_win
                        if rule_dict.get('base') == 'net':
                            min_base = max(Decimal('0.00'), min_potential_win - total_stake)
                        
                        min_bonus = compute_bonus_amount(min_base, pct, rule_dict.get('cap'))
                        min_winning = (min_potential_win + min_bonus).quantize(Decimal('0.01'))

                    max_winning = (potential_win + estimated_bonus_amount).quantize(Decimal('0.01'))

                    SelectionLiabilitySnapshot = apps.get_model("risk", "SelectionLiabilitySnapshot")
                    MarketLiabilitySnapshot = apps.get_model("risk", "MarketLiabilitySnapshot")
                    FixtureLiabilitySnapshot = apps.get_model("risk", "FixtureLiabilitySnapshot")
                    ip_address = get_client_ip(request)
                    fingerprint_hash = request.headers.get("X-Device-Fingerprint") or request.POST.get("device_fingerprint") or ""

                    try:
                        record_device_fingerprint(
                            user=request.user,
                            fingerprint_hash=fingerprint_hash,
                            ip_address=ip_address,
                            user_agent=request.META.get("HTTP_USER_AGENT", ""),
                            timezone_name=request.POST.get("device_timezone", ""),
                            screen=request.POST.get("device_screen", ""),
                            platform=request.POST.get("device_platform", ""),
                            language=request.POST.get("device_language", ""),
                        )
                    except Exception:
                        pass

                    try:
                        ipintel = check_ip_intelligence(ip_address)
                        if ipintel.get("blocked"):
                            return fail_response("Betting is blocked from your current network. Please disable VPN/Proxy or contact support.")
                    except Exception:
                        pass

                    for s in valid_selections:
                        existing_selection = (
                            SelectionLiabilitySnapshot.objects.filter(
                                fixture_id=s["fixture"].id,
                                market_key=s["market_key"],
                                selection_key=s["selection_key"],
                            )
                            .values("total_potential_payout")
                            .first()
                        )
                        existing_market = (
                            MarketLiabilitySnapshot.objects.filter(
                                fixture_id=s["fixture"].id,
                                market_key=s["market_key"],
                            )
                            .values("total_potential_payout")
                            .first()
                        )
                        existing_fixture = (
                            FixtureLiabilitySnapshot.objects.filter(fixture_id=s["fixture"].id)
                            .values("total_potential_payout")
                            .first()
                        )

                        existing_selection_liability = Decimal(str((existing_selection or {}).get("total_potential_payout") or "0.00"))
                        existing_market_liability = Decimal(str((existing_market or {}).get("total_potential_payout") or "0.00"))
                        existing_fixture_liability = Decimal(str((existing_fixture or {}).get("total_potential_payout") or "0.00"))

                        projected_selection_liability = (existing_selection_liability + max_winning).quantize(Decimal("0.01"))
                        projected_market_liability = (existing_market_liability + max_winning).quantize(Decimal("0.01"))
                        projected_fixture_liability = (existing_fixture_liability + max_winning).quantize(Decimal("0.01"))
                        decision = auto_suspend_if_needed(
                            actor=request.user,
                            fixture_id=s["fixture"].id,
                            market_key=s["market_key"],
                            selection_key=s["selection_key"],
                            projected_selection_liability=projected_selection_liability,
                            projected_market_liability=projected_market_liability,
                            projected_fixture_liability=projected_fixture_liability,
                        )
                        if decision.suspended:
                            for admin_user in User.objects.filter(Q(is_superuser=True) | Q(user_type__in=["admin", "account_user"])).only("id")[:200]:
                                try:
                                    create_notification(
                                        recipient=admin_user,
                                        notification_type="EVENT_SUSPENDED",
                                        title="Risk Auto-Suspension Triggered",
                                        message=f"Auto-suspended {decision.level} due to exposure. Fixture #{s['fixture'].id}.",
                                        data={
                                            "fixture_id": s["fixture"].id,
                                            "market_key": s["market_key"],
                                            "selection_key": s["selection_key"],
                                            "projected_selection_liability": str(projected_selection_liability),
                                            "projected_market_liability": str(projected_market_liability),
                                            "projected_fixture_liability": str(projected_fixture_liability),
                                        },
                                    )
                                except Exception:
                                    continue
                            return fail_response('This event/market is temporarily suspended due to high exposure.')

                    duplicate_signature = compute_duplicate_ticket_signature(
                        user_id=request.user.id,
                        selections=selections_data,
                        stake_per_line=str(stake_amount_per_line),
                        is_system_bet=is_system_bet,
                        permutation_count=permutation_count,
                        fingerprint_hash=fingerprint_hash,
                        ip_address=ip_address,
                    )
                    def _resolve_agent_for_user(u):
                        if not u:
                            return None
                        if u.user_type in ['agent', 'super_agent', 'master_agent']:
                            return u
                        if getattr(u, 'agent', None):
                            return u.agent
                        if getattr(u, 'super_agent', None):
                            return u.super_agent
                        if getattr(u, 'master_agent', None):
                            return u.master_agent
                        return None

                    agent_obj = _resolve_agent_for_user(request.user)

                    try:
                        limits = validate_ticket_against_limits(
                            user=request.user,
                            ticket_type=bet_type,
                            selection_count=len(valid_selections),
                            total_stake=total_stake,
                            max_winning=max_winning,
                            ticket_odds=max_line_odd,
                        )
                    except BettingLimitViolation as e:
                        BettingLimitAuditLog.objects.create(
                            action_type='TICKET_REJECTED',
                            actor=request.user,
                            agent=agent_obj,
                            affected_user=request.user,
                            ip_address=get_client_ip(request),
                            message=e.message,
                            data={
                                'code': e.code,
                                'bet_type': bet_type,
                                'selection_count': len(valid_selections),
                                'system_min_count': system_min_count,
                                'stake_per_line': str(stake_amount_per_line),
                                'total_stake': str(total_stake),
                                'ticket_max_winning': str(max_winning),
                                'ticket_potential_winning': str(potential_win),
                                'ticket_odds': str(max_line_odd),
                                'limits': (e.data.get('limits') if isinstance(e.data, dict) else {}) or {},
                                'details': e.data,
                            }
                        )

                        try:
                            reject_key = f"betting_limits:rejects:u:{request.user.id}"
                            cnt = int(cache.get(reject_key) or 0) + 1
                            cache.set(reject_key, cnt, timeout=600)
                            if cnt >= 5:
                                ActivityLog.objects.create(
                                    user=request.user,
                                    action_type='RISK',
                                    action=f"Repeated ticket rejections due to limits. Count:{cnt} LastReason:{e.code}",
                                    affected_object=f"User: {request.user.id}",
                                    ip_address=get_client_ip(request),
                                    path=request.path
                                )
                        except Exception:
                            pass

                        return fail_response(e.message)

                    try:
                        user_wallet = Wallet.objects.select_for_update().get(user=request.user)
                    except Wallet.DoesNotExist:
                        return fail_response('User wallet not found.')

                    if user_wallet.balance < total_stake:
                        return fail_response(f'Insufficient balance. Required: ₦{total_stake:.2f}, Available: ₦{user_wallet.balance:.2f}')

                    user_wallet.balance -= total_stake
                    user_wallet.save()

                    # Create Single BetTicket
                    ip = get_client_ip(request)
                    bonus_rule_obj = BonusRule.objects.filter(id=rule_dict['id']).first() if rule_dict else None
                    limits_snapshot = serialize_limits(limits)
                    limits_snapshot.update({
                        'applied_ticket_type': bet_type,
                        'stake_per_line': str(stake_amount_per_line),
                        'total_stake': str(total_stake),
                        'ticket_odds': str(max_line_odd),
                    })
                    limits_snapshot['selections_snapshot'] = [
                        {
                            'fixture_id': s['fixture'].id,
                            'home_team': s['fixture'].home_team,
                            'away_team': s['fixture'].away_team,
                            'match_date': str(getattr(s['fixture'], 'match_date', '') or ''),
                            'match_time': str(getattr(s['fixture'], 'match_time', '') or ''),
                            'bet_type': s['bet_type'],
                            'odd_selected': str(s['odd']),
                        }
                        for s in valid_selections
                    ]
                    bet_ticket = BetTicket.objects.create(
                        user=request.user,
                        stake_amount=total_stake, # Total stake for the ticket
                        total_odd=total_ticket_odd,
                        potential_winning=potential_win,
                        min_winning=min_winning,
                        max_winning=max_winning,
                        status='pending',
                        bet_type=bet_type,
                        system_min_count=system_min_count,
                        original_selections_count=len(valid_selections),
                        placed_ip=ip,
                        bonus_rule=bonus_rule_obj,
                        bonus_percentage_applied=(rule_dict['pct'] if rule_dict else Decimal('0.0000')),
                        bonus_base=(rule_dict['base'] if rule_dict else 'gross'),
                        betting_limits_snapshot=limits_snapshot
                    )
                    
                    # Create Selections
                    for sel in valid_selections:
                        Selection.objects.create(
                            bet_ticket=bet_ticket,
                            fixture=sel['fixture'],
                            betting_period=sel['fixture'].betting_period,
                            fixture_serial_number=str(getattr(sel['fixture'], 'serial_number', '') or ''),
                            fixture_home_team=sel['fixture'].home_team,
                            fixture_away_team=sel['fixture'].away_team,
                            fixture_match_date=getattr(sel['fixture'], 'match_date', None),
                            fixture_match_time=getattr(sel['fixture'], 'match_time', None),
                            bet_type=sel['bet_type'],
                            odd_selected=sel['odd']
                        )

                    try:
                        log_duplicate_ticket_if_needed(
                            user=request.user,
                            ticket=bet_ticket,
                            signature=duplicate_signature,
                            ip_address=ip_address,
                            fingerprint_hash=fingerprint_hash,
                        )
                    except Exception:
                        pass

                    try:
                        risk_score = evaluate_ticket_risk(
                            user=request.user,
                            ticket=bet_ticket,
                            ip_address=ip_address,
                            fingerprint_hash=fingerprint_hash,
                            selections=valid_selections,
                            stake_amount=total_stake,
                        )
                        if risk_score >= 70:
                            for admin_user in User.objects.filter(Q(is_superuser=True) | Q(user_type__in=["admin", "account_user"])).only("id")[:200]:
                                try:
                                    create_notification(
                                        recipient=admin_user,
                                        notification_type="RISK_ALERT",
                                        title="Risk Alert: Suspicious Betting",
                                        message=f"User {request.user.email} triggered a risk score of {risk_score} on ticket {bet_ticket.ticket_id}.",
                                        data={"user_id": request.user.id, "ticket_id": bet_ticket.ticket_id, "risk_score": risk_score},
                                    )
                                except Exception:
                                    continue
                    except Exception:
                        pass

                    try:
                        from risk.tasks import (
                            refresh_fixture_liability,
                            refresh_agent_exposure,
                            refresh_user_exposure,
                            refresh_betting_period_liability,
                        )

                        for fid in {s["fixture"].id for s in valid_selections}:
                            refresh_fixture_liability.delay(fid)
                        if agent_obj:
                            refresh_agent_exposure.delay(agent_obj.id)
                        refresh_user_exposure.delay(request.user.id)
                        if valid_selections and valid_selections[0].get("fixture") and getattr(valid_selections[0]["fixture"], "betting_period_id", None):
                            refresh_betting_period_liability.delay(valid_selections[0]["fixture"].betting_period_id)
                    except Exception:
                        pass

                    # Transaction Record (Summary)
                    Transaction.objects.create(
                        user=request.user,
                        transaction_type='bet_placement',
                        amount=total_stake,
                        is_successful=True,
                        status='completed',
                        description=f"Placed bet. Type: {bet_type.title()}. Ticket ID: {bet_ticket.ticket_id}",
                        related_bet_ticket=bet_ticket,
                        timestamp=timezone.now()
                    )

                    # Refresh wallet to get the latest balance
                    user_wallet.refresh_from_db()

                    if bet_type == 'system' and bet_ticket.bonus_percentage_applied and bet_ticket.bonus_percentage_applied > 0:
                        try:
                            ip_users_key = f"sysbonus:ip:{ip}:users"
                            recent_users = cache.get(ip_users_key) or []
                            if request.user.id not in recent_users:
                                recent_users.append(request.user.id)
                            cache.set(ip_users_key, recent_users, timeout=600)

                            sig_payload = f"{bet_type}:{system_min_count}:{sorted([(str(s['fixture'].id), s['bet_type']) for s in valid_selections])}:{stake_amount_str}"
                            sig_hash = hashlib.sha256(sig_payload.encode('utf-8')).hexdigest()
                            sig_key = f"sysbonus:sig:{request.user.id}:{sig_hash}"
                            duplicate_sig = cache.get(sig_key) is not None
                            cache.set(sig_key, 1, timeout=120)

                            if len(recent_users) >= 4 or duplicate_sig:
                                ActivityLog.objects.create(
                                    user=request.user,
                                    action_type='RISK',
                                    action=f"System-bet bonus risk flag. UsersOnIP:{len(recent_users)} DuplicateSig:{duplicate_sig} BonusPct:{bet_ticket.bonus_percentage_applied} Ticket:{bet_ticket.ticket_id}",
                                    affected_object=f"BetTicket: {bet_ticket.ticket_id}",
                                    ip_address=ip,
                                    path=request.path
                                )
                        except Exception:
                            pass

                    if placement_lock_key:
                        release_ticket_placement_lock(placement_lock_key)

                    return JsonResponse({
                        'success': True, 
                        'message': f'Successfully placed bet!',
                        'ticket_id': bet_ticket.ticket_id,
                        'new_balance': str(user_wallet.balance)
                    })

                except json.JSONDecodeError:
                    return JsonResponse({'success': False, 'message': 'Invalid data format.'})
                except Exception as e:
                    logger.error(f"Error placing bet: {e}")
                    traceback.print_exc()
                    if idempotency_key:
                        cache.delete(idempotency_key)
                    if placement_lock_key:
                        release_ticket_placement_lock(placement_lock_key)
                    return JsonResponse({'success': False, 'message': f'An error occurred: {str(e)}'})

            # Fallback to old single bet form logic
            elif 'fixture_id' in request.POST:
                form = BetTicketForm(request.POST)
                if form.is_valid():
                    # Retrieve data from form
                    fixture_id = form.cleaned_data['fixture_id']
                    selected_outcome = form.cleaned_data['selected_outcome']
                    stake_amount = form.cleaned_data['stake_amount']

                    fixture = get_object_or_404(Fixture, id=fixture_id)

                    # Basic validation: ensure fixture is still open for betting
                    if fixture.status != 'scheduled':
                        messages.error(request, 'Betting is closed for this fixture.')
                        return redirect('betting:fixtures')
                    
                    # Ensure the betting period is active
                    if not fixture.betting_period.is_active or fixture.betting_period.end_date < timezone.now().date():
                        messages.error(request, 'The betting period for this fixture is closed.')
                        return redirect('betting:fixtures')

                    # Validate match time (Strict Date/Time Enforcement)
                    local_now = timezone.localtime(timezone.now())
                    if fixture.match_date < local_now.date() or (fixture.match_date == local_now.date() and fixture.match_time <= local_now.time()):
                        messages.error(request, 'Betting is closed for this fixture (Match Started).')
                        return redirect('betting:fixtures')

                    # Get the correct odd based on selected_outcome
                    if selected_outcome == 'home_win':
                        odd = fixture.home_win_odd 
                    elif selected_outcome == 'draw':
                        odd = fixture.draw_odd
                    elif selected_outcome == 'away_win':
                        odd = fixture.away_win_odd 
                    elif selected_outcome == 'home_dnb':
                        odd = fixture.home_dnb_odd
                    elif selected_outcome == 'away_dnb':
                        odd = fixture.away_dnb_odd
                    elif selected_outcome == 'over_1_5':
                        odd = fixture.over1_5_odd
                    elif selected_outcome == 'under_1_5':
                        odd = fixture.under1_5_odd
                    elif selected_outcome == 'over_2_5':
                        odd = fixture.over2_5_odd
                    elif selected_outcome == 'under_2_5':
                        odd = fixture.under2_5_odd
                    elif selected_outcome == 'over_3_5':
                        odd = fixture.over3_5_odd
                    elif selected_outcome == 'under_3_5':
                        odd = fixture.under3_5_odd
                    elif selected_outcome == 'btts_yes':
                        odd = fixture.btts_yes_odd
                    elif selected_outcome == 'btts_no':
                        odd = fixture.btts_no_odd
                    else:
                        messages.error(request, 'Invalid outcome selected.')
                        return redirect('betting:fixtures')

                    # Check if user has sufficient balance
                    user_wallet = get_object_or_404(Wallet.objects.select_for_update(), user=request.user)
                    if user_wallet.balance < stake_amount:
                        messages.error(request, 'Insufficient balance to place this bet.')
                        return redirect('betting:wallet')

                    # Calculate potential winning and max winning (assuming max_winning = potential_winning for simple bets)
                    potential_winning = stake_amount * odd
                    max_winning = potential_winning # For single bet, max_winning is same as potential_winning

                    placement_lock_key = acquire_ticket_placement_lock(request.user.id)
                    if not placement_lock_key:
                        messages.error(request, 'Too many requests. Please retry.')
                        return redirect('betting:fixtures')

                    try:
                        limits = validate_ticket_against_limits(
                            user=request.user,
                            ticket_type='single',
                            selection_count=1,
                            total_stake=stake_amount,
                            max_winning=max_winning,
                            ticket_odds=odd,
                        )
                    except BettingLimitViolation as e:
                        BettingLimitAuditLog.objects.create(
                            action_type='TICKET_REJECTED',
                            actor=request.user,
                            agent=(request.user if request.user.user_type in ['agent', 'super_agent', 'master_agent'] else (request.user.agent or request.user.super_agent or request.user.master_agent)),
                            affected_user=request.user,
                            ip_address=get_client_ip(request),
                            message=e.message,
                            data={'code': e.code, 'details': e.data}
                        )
                        release_ticket_placement_lock(placement_lock_key)
                        messages.error(request, e.message)
                        return redirect('betting:fixtures')

                    # Deduct stake from wallet
                    user_wallet.balance -= stake_amount
                    user_wallet.save()

                    # Create BetTicket
                    limits_snapshot = serialize_limits(limits)
                    limits_snapshot.update({
                        'applied_ticket_type': 'single',
                        'stake_per_line': str(stake_amount),
                        'total_stake': str(stake_amount),
                        'ticket_odds': str(odd),
                    })
                    limits_snapshot['selections_snapshot'] = [
                        {
                            'fixture_id': fixture.id,
                            'home_team': fixture.home_team,
                            'away_team': fixture.away_team,
                            'match_date': str(getattr(fixture, 'match_date', '') or ''),
                            'match_time': str(getattr(fixture, 'match_time', '') or ''),
                            'bet_type': selected_outcome,
                            'odd_selected': str(odd),
                        }
                    ]
                    bet_ticket = BetTicket.objects.create(
                        user=request.user,
                        stake_amount=stake_amount,
                        total_odd=odd, # For a single bet, total_odd is just the odd of the selected outcome
                        potential_winning=potential_winning,
                        max_winning=max_winning,
                        status='pending',
                        betting_limits_snapshot=limits_snapshot
                    )
                    # Create Selection for the bet ticket
                    Selection.objects.create(
                        bet_ticket=bet_ticket,
                        fixture=fixture,
                        betting_period=fixture.betting_period,
                        fixture_serial_number=str(getattr(fixture, 'serial_number', '') or ''),
                        fixture_home_team=fixture.home_team,
                        fixture_away_team=fixture.away_team,
                        fixture_match_date=getattr(fixture, 'match_date', None),
                        fixture_match_time=getattr(fixture, 'match_time', None),
                        bet_type=selected_outcome, # Corrected field name
                        odd_selected=odd # Store the odd at the time of betting
                    )

                    # Record Transaction
                    Transaction.objects.create(
                        user=request.user,
                        transaction_type='bet_placement',
                        amount=stake_amount,
                        is_successful=True,
                        status='completed',
                        description=f"Bet placed on {fixture.home_team} vs {fixture.away_team} for {selected_outcome}",
                        related_bet_ticket=bet_ticket,
                        timestamp=timezone.now()
                    )

                    release_ticket_placement_lock(placement_lock_key)
                    messages.success(request, f'Bet placed successfully! Your ticket ID is {bet_ticket.ticket_id}. Potential winning: ₦{potential_winning:.2f}')
                    return redirect('betting:fixtures') # Redirect back to fixtures with errors
                else:
                    for field, errors in form.errors.items():
                        for error in errors:
                            messages.error(request, f"{field.replace('_', ' ').title()}: {error}")
                    if form.non_field_errors():
                        for error in form.non_field_errors():
                            messages.error(request, f"Betting Error: {error}")
                    return redirect('betting:fixtures') # Redirect back to fixtures with errors
            
            else:
                # Unknown request format - likely AJAX missing data
                logger.warning(f"Invalid place_bet request params: {request.POST.keys()}")
                # If it looks like the new JS form (has 'is_system_bet' or 'stake_amount') OR if it's an AJAX request
                if 'is_system_bet' in request.POST or 'stake_amount' in request.POST or request.headers.get('x-requested-with') == 'XMLHttpRequest' or 'application/json' in request.META.get('HTTP_ACCEPT', ''):
                    return JsonResponse({'success': False, 'message': 'Invalid request parameters. Missing selections data.'})
                
                messages.error(request, 'Invalid request parameters.')
                return redirect('betting:fixtures')

        messages.error(request, 'Invalid request to place bet.')
        return redirect('betting:fixtures')
    except Exception as e:
        logger.error(f"Unexpected error in place_bet: {e}")
        traceback.print_exc()
        if 'is_system_bet' in request.POST or 'stake_amount' in request.POST or request.headers.get('x-requested-with') == 'XMLHttpRequest' or 'application/json' in request.META.get('HTTP_ACCEPT', ''):
             return JsonResponse({'success': False, 'message': f'An unexpected error occurred: {str(e)}'})
        messages.error(request, 'An unexpected error occurred. Please try again.')
        return redirect('betting:fixtures')

def check_ticket_status(request):
    ticket = None
    tickets = BetTicket.objects.none()
    # Default 60 mins if not set
    void_window_str = SystemSetting.get_setting('ticket_cancellation_window_minutes', '60')
    try:
        void_window = int(void_window_str)
    except (ValueError, TypeError):
        void_window = 60

    if request.method == 'POST':
        form = CheckTicketStatusForm(request.POST) 
        if form.is_valid():
            ticket = form.ticket 
        else:
            for field, errors in form.errors.items():
                for error in errors:
                    messages.error(request, f"{field.replace('_', ' ').title()}: {error}")
            if form.non_field_errors():
                for error in form.non_field_errors():
                    messages.error(request, f"Ticket Check Error: {error}")
    else:
        form = CheckTicketStatusForm()
    
    # Populate tickets based on user hierarchy
    if request.user.is_authenticated:
        if request.user.is_superuser or request.user.user_type == 'admin':
            tickets = BetTicket.objects.all().order_by('-placed_at')
        elif request.user.user_type == 'master_agent':
            tickets = BetTicket.objects.filter(
                Q(user__master_agent=request.user) | 
                Q(user__super_agent__master_agent=request.user) |
                Q(user__agent__master_agent=request.user) |
                Q(user__agent__super_agent__master_agent=request.user)
            ).order_by('-placed_at')
        elif request.user.user_type == 'super_agent':
             tickets = BetTicket.objects.filter(
                Q(user__super_agent=request.user) |
                Q(user__agent__super_agent=request.user)
            ).order_by('-placed_at')
        elif request.user.user_type == 'agent':
            tickets = BetTicket.objects.filter(
                Q(user__agent=request.user)
            ).order_by('-placed_at')
        else: # Player/Cashier
            tickets = BetTicket.objects.filter(user=request.user).order_by('-placed_at')

    # Apply Filters
    ticket_id_query = request.GET.get('ticket_id')
    status_query = request.GET.get('status')
    bet_type_query = request.GET.get('bet_type')
    date_from = request.GET.get('date_from')
    date_to = request.GET.get('date_to')
    settled_date_from = request.GET.get('settled_date_from')
    settled_date_to = request.GET.get('settled_date_to')

    if ticket_id_query:
        ticket_id_query = (ticket_id_query or '').strip().upper()
        if request.user.is_authenticated:
            tickets = tickets.filter(ticket_id__icontains=ticket_id_query)
        else:
            if re.fullmatch(r"[A-Z0-9]{6,8}", ticket_id_query or ""):
                tickets = BetTicket.objects.filter(ticket_id__iexact=ticket_id_query).order_by('-placed_at')
                ticket = tickets.first()
                if not ticket:
                    messages.error(request, "Ticket not found.")
            else:
                messages.error(request, "Please enter a valid Ticket ID.")
            status_query = None
            bet_type_query = None
            date_from = None
            date_to = None
            settled_date_from = None
            settled_date_to = None
    
    if status_query and status_query != 'all':
        tickets = tickets.filter(status=status_query)
        
    if bet_type_query and bet_type_query != 'all':
        tickets = tickets.filter(bet_type=bet_type_query)
        
    if date_from:
        try:
            tickets = tickets.filter(placed_at__date__gte=date_from)
        except (ValueError, TypeError): pass
        
    if date_to:
        try:
            tickets = tickets.filter(placed_at__date__lte=date_to)
        except (ValueError, TypeError): pass

    if settled_date_from:
        try:
            # Filter by last_updated for settled tickets
            tickets = tickets.filter(last_updated__date__gte=settled_date_from).exclude(status='pending')
        except (ValueError, TypeError): pass
        
    if settled_date_to:
        try:
            tickets = tickets.filter(last_updated__date__lte=settled_date_to).exclude(status='pending')
        except (ValueError, TypeError): pass

    # Apply limit after filtering
    tickets = tickets[:50]

    # AJAX Polling Check
    if request.method == 'GET' and request.GET.get('action') == 'poll_tickets':
        return render(request, 'betting/partials/ticket_list_rows.html', {
            'tickets': tickets,
            'void_window': void_window,
            'now': timezone.now()
        })

    context = {
        'form': form, 
        'ticket': ticket, 
        'tickets': tickets,
        'void_window': void_window,
        'now': timezone.now(),
        'bet_type_choices': BetTicket.BET_TYPE_CHOICES,
        'status_choices': BetTicket.STATUS_CHOICES,
        'ticket_bonus_percent': (ticket.bonus_percentage_applied * Decimal('100')) if ticket else Decimal('0.00'),
        'ticket_estimated_bonus': (max(Decimal('0.00'), (ticket.max_winning - ticket.potential_winning)) if ticket and not ticket.bonus_is_final else (ticket.bonus_amount if ticket else Decimal('0.00'))),
        'ticket_selections_snapshot': (ticket.betting_limits_snapshot or {}).get('selections_snapshot', []) if ticket else [],
    }
    return render(request, 'betting/check_ticket.html', context)


@login_required
@db_transaction.atomic
def agent_void_ticket(request, ticket_id):
    ticket = get_object_or_404(BetTicket, ticket_id=ticket_id)
    void_window_str = SystemSetting.get_setting('ticket_cancellation_window_minutes', '60')
    try:
        void_window = int(void_window_str)
    except (ValueError, TypeError):
        void_window = 60
    
    # Permission Check
    can_void = False
    if request.user.is_superuser or request.user.user_type == 'admin':
        can_void = True
    elif request.user.user_type == 'agent':
        # Agent can only void tickets from their cashiers
        if ticket.user.user_type == 'cashier' and ticket.user.agent == request.user:
            # Check window
            time_diff = (timezone.now() - ticket.placed_at).total_seconds() / 60
            if time_diff <= void_window:
                can_void = True
            else:
                messages.error(request, "Cancellation window has expired for this ticket.")
                return redirect('betting:check_ticket_status')
        else:
             messages.error(request, "You can only void tickets placed by your cashiers.")
             return redirect('betting:check_ticket_status')
    
    if not can_void:
        messages.error(request, "You do not have permission to void this ticket.")
        return redirect('betting:check_ticket_status')

    if ticket.status in ['cancelled', 'deleted']:
        messages.error(request, "Ticket is already voided.")
        return redirect('betting:check_ticket_status')

    if ticket.status != 'pending':
         messages.error(request, "Only pending tickets can be voided.")
         return redirect('betting:check_ticket_status')

    # Void Process
    ticket.status = 'cancelled'
    ticket.deleted_by = request.user
    ticket.deleted_at = timezone.now()
    ticket.save() # This triggers the pre_save signal to refund stake

    if request.user.is_superuser or request.user.user_type == 'admin':
        log_admin_activity(request, f"Voided ticket {ticket.ticket_id} for user {ticket.user.email}")
    
    messages.success(request, f"Ticket {ticket.ticket_id} voided and stake refunded successfully.")
    return redirect('betting:check_ticket_status')

# --- Wallet & Payments Views ---

@login_required
def wallet_view(request):
    wallet, created = Wallet.objects.get_or_create(user=request.user, defaults={'balance': Decimal('0.00')})
    if created:
        logger.info(f"Created missing wallet for user {request.user.email} in wallet_view.")
    transactions = Transaction.objects.filter(user=request.user).order_by('-timestamp')[:20] # Last 20 transactions
    pending_withdrawals = UserWithdrawal.objects.filter(user=request.user, status='pending').order_by('-request_time') # Corrected field name

    if request.user.maybe_auto_unlock_withdrawal():
        request.user.save(update_fields=['withdrawal_locked', 'withdrawal_attempts', 'withdrawal_locked_at'])
    if request.user.withdrawal_locked and not request.user.withdrawal_locked_at:
        request.user.withdrawal_locked_at = timezone.now()
        request.user.save(update_fields=['withdrawal_locked_at'])

    # Initialize forms based on user type
    wallet_transfer_form = None
    credit_request_form = None
    
    # Cashiers can only request credit, not transfer freely (unless logic changes)
    # Agents/Super/Master can transfer AND request credit
    if request.user.user_type == 'cashier':
        credit_request_form = CreditRequestForm(user=request.user)
    elif request.user.user_type in ['agent', 'super_agent', 'master_agent', 'account_user']:
                # Check permission for Master/Super Agents
                if request.user.user_type in ['master_agent', 'super_agent'] and not getattr(request.user, 'can_manage_downline_wallets', True):
                    wallet_transfer_form = None
                else:
                    wallet_transfer_form = WalletTransferForm(sender_user=request.user)
                credit_request_form = CreditRequestForm(user=request.user)

    # Active Loans
    active_loans = Loan.objects.filter(borrower=request.user, status='active')
    withdrawal_lock_expires_at = request.user.get_withdrawal_lock_expires_at()

    context = {
        'wallet': wallet,
        'transactions': transactions,
        'initiate_deposit_form': InitiateDepositForm(),
        'withdraw_funds_form': WithdrawFundsForm(user=request.user), # Pass user for validation
        'wallet_transfer_form': wallet_transfer_form,
        'credit_request_form': credit_request_form,
        'active_loans': active_loans,
        'loan_settlement_form': LoanSettlementForm(request=request),
        'pending_withdrawals': pending_withdrawals,
        'withdrawal_pin_is_set': request.user.withdrawal_pin_is_set,
        'withdrawal_locked': request.user.withdrawal_locked,
        'withdrawal_attempts': request.user.withdrawal_attempts,
        'withdrawal_lock_expires_at': withdrawal_lock_expires_at,
        'paystack_public_key': settings.PAYSTACK_PUBLIC_KEY,
    }
    return render(request, 'betting/wallet.html', context)

@login_required
@db_transaction.atomic
def initiate_deposit(request):
    if request.method == 'POST':
        # Handle JSON Request (AJAX)
        if request.content_type == 'application/json' or request.headers.get('Content-Type', '').startswith('application/json'):
            try:
                data = json.loads(request.body)
                amount = float(data.get('amount', 0))
                gateway = data.get('gateway', 'paystack') # Default to paystack
                
                if amount <= 0:
                     return JsonResponse({'status': 'error', 'message': 'Invalid amount.'}, status=400)
                
                reference = str(uuid.uuid4())
                
                # Create a pending transaction record
                Transaction.objects.create(
                    user=request.user,
                    transaction_type='deposit',
                    amount=amount,
                    status='pending',
                    description=f'Pending online deposit via {gateway.capitalize()}',
                    payment_gateway=gateway,
                    external_reference=reference, # Use external_reference for all gateways
                    timestamp=timezone.now()
                )
                
                # Logic for different gateways
                if gateway == 'paystack':
                    return JsonResponse({
                        'status': 'success',
                        'gateway': 'paystack',
                        'email': request.user.email,
                        'amount': int(amount * 100), # Amount in kobo
                        'reference': reference,
                        'public_key': settings.PAYSTACK_PUBLIC_KEY
                    })
                elif gateway == 'monnify':
                    # Monnify initialization requires an access token
                    auth_url = f"{os.getenv('MONNIFY_BASE_URL')}/api/v1/auth/login"
                    auth_headers = {
                        "Authorization": f"Basic {os.getenv('MONNIFY_API_KEY')}:{os.getenv('MONNIFY_SECRET_KEY')}"
                    }
                    # We'll just return the keys for frontend SDK if possible, or handle server-side
                    # For simplicity in this example, we return data for the frontend Monnify SDK
                    return JsonResponse({
                        'status': 'success',
                        'gateway': 'monnify',
                        'email': request.user.email,
                        'amount': float(amount),
                        'reference': reference,
                        'apiKey': os.getenv('MONNIFY_API_KEY'),
                        'contractCode': os.getenv('MONNIFY_CONTRACT_CODE')
                    })
                elif gateway == 'kora':
                    public_key = os.getenv('KORA_PUBLIC_KEY') or os.getenv('KORAPAY_PUBLIC_KEY')
                    if not public_key:
                        return JsonResponse({'status': 'error', 'message': 'Kora is not configured (missing public key).'}, status=500)
                    return JsonResponse({
                        'status': 'success',
                        'gateway': 'kora',
                        'email': request.user.email,
                        'amount': float(amount),
                        'reference': reference,
                        'publicKey': public_key
                    })
                else:
                    return JsonResponse({'status': 'error', 'message': 'Unsupported gateway.'}, status=400)

            except Exception as e:
                logger.error(f"Error in initiate_deposit API: {str(e)}")
                return JsonResponse({'status': 'error', 'message': str(e)}, status=500)

        # Handle Form Request (Traditional Redirect)
        gateway = request.POST.get('gateway', 'paystack')
        form = InitiateDepositForm(request.POST)
        if form.is_valid():
            amount = form.cleaned_data['amount']
            reference = str(uuid.uuid4())

            # Create a pending transaction record
            Transaction.objects.create(
                user=request.user,
                transaction_type='deposit',
                amount=amount,
                status='pending',
                description=f'Pending online deposit via {gateway.capitalize()}',
                payment_gateway=gateway,
                external_reference=reference,
                timestamp=timezone.now()
            )

            if gateway == 'paystack':
                paystack_secret_key = settings.PAYSTACK_SECRET_KEY
                url = "https://api.paystack.co/transaction/initialize"
                headers = {
                    "Authorization": f"Bearer {paystack_secret_key}",
                    "Content-Type": "application/json"
                }
                payload = {
                    "email": request.user.email,
                    "amount": int(amount * 100),
                    "reference": reference,
                    "callback_url": request.build_absolute_uri(reverse('betting:verify_deposit')),
                    "metadata": {
                        "user_id": str(request.user.id),
                        "user_email": request.user.email,
                        "gateway": "paystack"
                    }
                }
                try:
                    response = requests.post(url, headers=headers, data=json.dumps(payload), timeout=10)
                    response.raise_for_status()
                    response_data = response.json()
                    if response_data['status']:
                        return redirect(response_data['data']['authorization_url'])
                    else:
                        messages.error(request, f"Paystack initialization failed: {response_data['message']}")
                except Exception as e:
                    messages.error(request, f"Error initiating Paystack payment: {e}")
            
            elif gateway == 'monnify':
                # Monnify Server-side initialization
                auth_url = f"{os.getenv('MONNIFY_BASE_URL')}/api/v1/auth/login"
                api_key = os.getenv('MONNIFY_API_KEY')
                secret_key = os.getenv('MONNIFY_SECRET_KEY')
                import base64
                auth_str = base64.b64encode(f"{api_key}:{secret_key}".encode()).decode()
                
                try:
                    auth_response = requests.post(auth_url, headers={"Authorization": f"Basic {auth_str}"}, timeout=10)
                    auth_data = auth_response.json()
                    if auth_data['requestSuccessful']:
                        token = auth_data['responseBody']['accessToken']
                        init_url = f"{os.getenv('MONNIFY_BASE_URL')}/api/v1/merchant/transactions/init-transaction"
                        init_payload = {
                            "amount": float(amount),
                            "customerName": f"{request.user.first_name} {request.user.last_name}",
                            "customerEmail": request.user.email,
                            "paymentReference": reference,
                            "paymentDescription": "Wallet Deposit",
                            "currencyCode": "NGN",
                            "contractCode": os.getenv('MONNIFY_CONTRACT_CODE'),
                            "redirectUrl": request.build_absolute_uri(reverse('betting:verify_monnify_deposit')),
                            "paymentMethods": ["CARD", "ACCOUNT_TRANSFER"]
                        }
                        init_response = requests.post(init_url, headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"}, data=json.dumps(init_payload), timeout=10)
                        init_data = init_response.json()
                        if init_data['requestSuccessful']:
                            return redirect(init_data['responseBody']['checkoutUrl'])
                        else:
                            messages.error(request, f"Monnify initialization failed: {init_data['responseMessage']}")
                    else:
                        messages.error(request, "Monnify authentication failed.")
                except Exception as e:
                    messages.error(request, f"Error initiating Monnify payment: {e}")

            elif gateway == 'kora':
                # Kora Server-side initialization
                base_url = os.getenv('KORA_BASE_URL') or os.getenv('KORAPAY_BASE_URL') or "https://api.korapay.com/merchant/api/v1"
                if base_url.rstrip('/').endswith('/merchant/api'):
                    base_url = f"{base_url.rstrip('/')}/v1"

                secret_key = os.getenv('KORA_SECRET_KEY') or os.getenv('KORAPAY_SECRET_KEY')
                if not secret_key:
                    messages.error(request, "Kora is not configured (missing KORA_SECRET_KEY).")
                    return redirect('betting:wallet')
                url = f"{base_url.rstrip('/')}/charges/initialize"
                headers = {
                    "Authorization": f"Bearer {secret_key}",
                    "Content-Type": "application/json"
                }
                payload = {
                    "amount": float(amount),
                    "currency": "NGN",
                    "reference": reference,
                    "notification_url": request.build_absolute_uri(reverse('betting:verify_kora_deposit')),
                    "redirect_url": request.build_absolute_uri(reverse('betting:verify_kora_deposit')),
                    "customer": {
                        "name": f"{request.user.first_name} {request.user.last_name}",
                        "email": request.user.email
                    }
                }
                try:
                    response = requests.post(url, headers=headers, data=json.dumps(payload), timeout=10)
                    response.raise_for_status()
                    response_data = response.json()
                    if response_data['status']:
                        return redirect(response_data['data']['checkout_url'])
                    else:
                        messages.error(request, f"Kora initialization failed: {response_data['message']}")
                except Exception as e:
                    messages.error(request, f"Error initiating Kora payment: {e}")

            # If we reach here, something failed
            Transaction.objects.filter(external_reference=reference).update(status='failed')
        else:
            for field, errors in form.errors.items():
                for error in errors:
                    messages.error(request, f"{field.replace('_', ' ').title()}: {error}")
    return redirect('betting:wallet')

@login_required
@db_transaction.atomic
def verify_monnify_deposit(request):
    reference = request.GET.get('paymentReference')
    if not reference:
        messages.error(request, "Monnify reference not found.")
        return redirect('betting:wallet')

    # Use select_for_update() on Transaction to prevent race conditions
    transaction_record = get_object_or_404(
        Transaction.objects.select_for_update(), 
        external_reference=reference, 
        user=request.user
    )
    
    if transaction_record.status == 'completed':
        messages.success(request, "This deposit has already been successfully verified.")
        return redirect('betting:wallet')
    
    if transaction_record.status == 'failed':
        messages.error(request, "This deposit previously failed.")
        return redirect('betting:wallet')

    # Monnify API verification
    api_key = os.getenv('MONNIFY_API_KEY')
    secret_key = os.getenv('MONNIFY_SECRET_KEY')
    base_url = os.getenv('MONNIFY_BASE_URL')
    
    import base64
    auth_str = base64.b64encode(f"{api_key}:{secret_key}".encode()).decode()
    
    try:
        # 1. Get Access Token
        auth_url = f"{base_url}/api/v1/auth/login"
        auth_response = requests.post(auth_url, headers={"Authorization": f"Basic {auth_str}"}, timeout=10)
        auth_data = auth_response.json()
        
        if auth_data.get('requestSuccessful'):
            token = auth_data['responseBody']['accessToken']
            
            # 2. Verify Transaction
            verify_url = f"{base_url}/api/v1/merchant/transactions/query?paymentReference={reference}"
            verify_response = requests.get(verify_url, headers={"Authorization": f"Bearer {token}"}, timeout=10)
            verify_data = verify_response.json()
            
            if verify_data.get('requestSuccessful') and verify_data['responseBody']['paymentStatus'] == 'PAID':
                amount_verified = Decimal(str(verify_data['responseBody']['amountPaid']))
                
                # Check if amount matches to prevent tampering
                if amount_verified != transaction_record.amount:
                    transaction_record.status = 'failed'
                    transaction_record.description = f"Amount mismatch: Expected {transaction_record.amount}, Got {amount_verified}"
                    transaction_record.save()
                    messages.error(request, "Deposit verification failed: Amount mismatch.")
                    return redirect('betting:wallet')

                # Update wallet balance
                user_wallet = get_object_or_404(Wallet.objects.select_for_update(), user=request.user)
                user_wallet.balance += amount_verified
                user_wallet.save()

                # Update transaction record
                transaction_record.status = 'completed'
                transaction_record.is_successful = True
                transaction_record.description = "Online deposit via Monnify successful."
                transaction_record.timestamp = timezone.now()
                transaction_record.save()

                _notify_admin_deposit_success(request.user, transaction_record, amount_verified, "monnify")
                messages.success(request, f"Deposit of ₦{amount_verified:.2f} successful! Your wallet has been credited.")
            else:
                msg = verify_data.get('responseMessage', 'Payment not successful')
                messages.error(request, f"Monnify verification failed: {msg}")
        else:
            messages.error(request, "Monnify authentication failed during verification.")
            
    except Exception as e:
        logger.error(f"Monnify verification error: {str(e)}")
        messages.error(request, f"Error verifying Monnify payment: {str(e)}")

    return redirect('betting:wallet')

@login_required
@db_transaction.atomic
def verify_kora_deposit(request):
    reference = request.GET.get('reference')
    if not reference:
        messages.error(request, "Kora reference not found.")
        return redirect('betting:wallet')

    # Use select_for_update() on Transaction to prevent race conditions
    transaction_record = get_object_or_404(
        Transaction.objects.select_for_update(), 
        external_reference=reference, 
        user=request.user
    )
    
    if transaction_record.status == 'completed':
        messages.success(request, "This deposit has already been successfully verified.")
        return redirect('betting:wallet')
    
    if transaction_record.status == 'failed':
        messages.error(request, "This deposit previously failed.")
        return redirect('betting:wallet')

    # Kora API verification
    secret_key = os.getenv('KORA_SECRET_KEY') or os.getenv('KORAPAY_SECRET_KEY')
    base_url = os.getenv('KORA_BASE_URL') or os.getenv('KORAPAY_BASE_URL') or "https://api.korapay.com/merchant/api/v1"
    if base_url.rstrip('/').endswith('/merchant/api'):
        base_url = f"{base_url.rstrip('/')}/v1"
    if not secret_key:
        messages.error(request, "Kora is not configured (missing KORA_SECRET_KEY).")
        return redirect('betting:wallet')
    
    try:
        verify_url = f"{base_url.rstrip('/')}/charges/{reference}"
        headers = {"Authorization": f"Bearer {secret_key}"}
        
        response = requests.get(verify_url, headers=headers, timeout=10)
        response_data = response.json()
        
        if response_data.get('status') and response_data['data']['status'] == 'success':
            amount_verified = Decimal(str(response_data['data']['amount']))
            
            # Check if amount matches to prevent tampering
            if amount_verified != transaction_record.amount:
                transaction_record.status = 'failed'
                transaction_record.description = f"Amount mismatch: Expected {transaction_record.amount}, Got {amount_verified}"
                transaction_record.save()
                messages.error(request, "Deposit verification failed: Amount mismatch.")
                return redirect('betting:wallet')

            # Update wallet balance
            user_wallet = get_object_or_404(Wallet.objects.select_for_update(), user=request.user)
            user_wallet.balance += amount_verified
            user_wallet.save()

            # Update transaction record
            transaction_record.status = 'completed'
            transaction_record.is_successful = True
            transaction_record.description = "Online deposit via Kora successful."
            transaction_record.timestamp = timezone.now()
            transaction_record.save()

            _notify_admin_deposit_success(request.user, transaction_record, amount_verified, "kora")
            messages.success(request, f"Deposit of ₦{amount_verified:.2f} successful! Your wallet has been credited.")
        else:
            msg = response_data.get('message', 'Payment not successful')
            messages.error(request, f"Kora verification failed: {msg}")
            
    except Exception as e:
        logger.error(f"Kora verification error: {str(e)}")
        messages.error(request, f"Error verifying Kora payment: {str(e)}")

    return redirect('betting:wallet')


@login_required
@db_transaction.atomic
def verify_deposit(request):
    reference = request.GET.get('trxref') or request.GET.get('reference')
    if not reference:
        messages.error(request, "Payment reference not found.")
        return redirect('betting:wallet')

    # Use select_for_update() on Transaction to prevent race conditions
    transaction_record = get_object_or_404(
        Transaction.objects.select_for_update(), 
        external_reference=reference, 
        user=request.user
    )

    if transaction_record.status == 'completed':
        messages.success(request, "This deposit has already been successfully verified.")
        return redirect('betting:wallet')
    
    if transaction_record.status == 'failed':
        messages.error(request, "This deposit previously failed.")
        return redirect('betting:wallet')

    paystack_secret_key = settings.PAYSTACK_SECRET_KEY
    url = f"https://api.paystack.co/transaction/verify/{reference}"
    headers = {
        "Authorization": f"Bearer {paystack_secret_key}",
        "Content-Type": "application/json"
    }

    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        response_data = response.json()

        if response_data['status'] and response_data['data']['status'] == 'success':
            amount_verified = Decimal(response_data['data']['amount'] / 100) # Convert kobo to naira
            
            if amount_verified != transaction_record.amount:
                # Mismatch between initiated and verified amount
                messages.error(request, "Amount mismatch during verification. Please contact support.")
                transaction_record.status = 'failed'
                transaction_record.description = f"Amount mismatch: Expected {transaction_record.amount}, Got {amount_verified}"
                transaction_record.save()
                return redirect('betting:wallet')

            # Update wallet balance
            user_wallet = get_object_or_404(Wallet.objects.select_for_update(), user=request.user)
            user_wallet.balance += amount_verified
            user_wallet.save()

            # Update transaction record
            transaction_record.status = 'completed'
            transaction_record.is_successful = True
            transaction_record.description = "Online deposit via Paystack successful."
            transaction_record.timestamp = timezone.now() # Update timestamp to completion time
            transaction_record.save()

            _notify_admin_deposit_success(request.user, transaction_record, amount_verified, "paystack")
            messages.success(request, f"Deposit of ₦{amount_verified:.2f} successful! Your wallet has been credited.")
        else:
            messages.error(request, f"Payment verification failed: {response_data['data'].get('message', 'Unknown error')}")
            transaction_record.status = 'failed'
            transaction_record.description = f"Paystack verification failed: {response_data['data'].get('message', 'Unknown error')}"
            transaction_record.save()
            
    except requests.exceptions.Timeout:
        messages.error(request, "Paystack verification timed out. Please try again.")
        transaction_record.status = 'failed'
        transaction_record.description = "Paystack verification timed out."
        transaction_record.save()
    except requests.exceptions.RequestException as e:
        messages.error(request, f"Error verifying payment with Paystack: {e}")
        transaction_record.status = 'failed'
        transaction_record.description = f"Error verifying payment with Paystack: {e}"
        transaction_record.save()
    except json.JSONDecodeError:
        messages.error(request, "Invalid response from Paystack during verification.")
        transaction_record.status = 'failed'
        transaction_record.description = "Invalid JSON response from Paystack during verification."
        transaction_record.save()

    return redirect('betting:wallet')


@login_required
@db_transaction.atomic
def withdraw_funds(request):
    expects_json = request.headers.get('Content-Type', '').startswith('application/json')
    # Restrict withdrawal to specific roles
    if request.user.user_type not in ['master_agent', 'super_agent', 'agent']:
        if expects_json:
            return JsonResponse({'status': 'error', 'message': 'You are not authorized to withdraw funds.'}, status=403)
        messages.error(request, "You are not authorized to withdraw funds.")
        return redirect('betting:wallet')

    user = User.objects.select_for_update().get(pk=request.user.pk)
    if user.maybe_auto_unlock_withdrawal():
        user.save(update_fields=['withdrawal_locked', 'withdrawal_attempts', 'withdrawal_locked_at'])
    if user.withdrawal_locked and not user.withdrawal_locked_at:
        user.withdrawal_locked_at = timezone.now()
        user.save(update_fields=['withdrawal_locked_at'])

    if user.withdrawal_locked:
        expires_at = user.get_withdrawal_lock_expires_at()
        retry_at = expires_at.isoformat() if expires_at else None
        if expects_json:
            return JsonResponse({'status': 'locked', 'message': 'Withdrawal access has been disabled. Retry after 24 hours or contact administrator.', 'retry_at': retry_at}, status=423)
        if expires_at:
            messages.error(request, f"Withdrawal access has been disabled. Retry after {expires_at.strftime('%Y-%m-%d %H:%M')} or contact administrator.")
        else:
            messages.error(request, "Withdrawal access has been disabled. Retry after 24 hours or contact administrator.")
        return redirect('betting:wallet')

    if not user.withdrawal_pin_is_set:
        if expects_json:
            return JsonResponse({'status': 'no_pin', 'message': 'Please create your Withdrawal PIN first.', 'redirect_url': reverse('betting:profile')}, status=400)
        messages.warning(request, "Please create your Withdrawal PIN first.")
        return redirect('betting:profile')

    # Check for active loans
    has_active_loans = Loan.objects.filter(borrower=request.user, status='active', outstanding_balance__gt=0).exists()
    if has_active_loans:
        if expects_json:
            return JsonResponse({'status': 'error', 'message': 'You cannot withdraw funds while you have an active unpaid loan.'}, status=400)
        messages.error(request, "You cannot withdraw funds while you have an active unpaid loan.")
        return redirect('betting:wallet')

    if request.method == 'POST':
        if request.headers.get('Content-Type', '').startswith('application/json'):
            try:
                payload = json.loads(request.body or '{}')
            except Exception:
                payload = {}
            form = WithdrawFundsForm(payload, user=user)
            expects_json = True
        else:
            form = WithdrawFundsForm(request.POST, user=user)
            expects_json = False

        if not _is_withdrawal_pin_verified_recent(request):
            if expects_json:
                return JsonResponse({'status': 'error', 'message': 'Withdrawal PIN verification required.'}, status=400)
            messages.error(request, "Withdrawal PIN verification required.")
            return redirect('betting:wallet')

        if form.is_valid():
            amount = form.cleaned_data['amount']
            bank_name = form.cleaned_data['bank_name']
            account_number = form.cleaned_data['account_number']

            user_wallet = get_object_or_404(Wallet.objects.select_for_update(), user=request.user)

            if user_wallet.balance < amount:
                if expects_json:
                    return JsonResponse({'status': 'error', 'message': 'Insufficient balance for withdrawal.'}, status=400)
                messages.error(request, 'Insufficient balance for withdrawal.')
                return redirect('betting:wallet')

            balance_before = user_wallet.balance
            balance_after = balance_before - amount
            user_wallet.balance = balance_after
            user_wallet.save()

            withdrawal = UserWithdrawal.objects.create(
                user=request.user,
                amount=amount,
                bank_name=bank_name,
                account_name=form.cleaned_data['account_name'], # Corrected to use cleaned_data
                account_number=account_number,
                balance_before=balance_before,
                balance_after=balance_after,
                status='pending' # Set to pending for admin approval
            )

            Transaction.objects.create(
                user=request.user,
                initiating_user=request.user,
                target_user=request.user,
                transaction_type='withdrawal',
                amount=amount,
                is_successful=True,
                status='completed',
                description=f"Withdrawal request {withdrawal.id} created (deducted from wallet).",
                related_withdrawal_request=withdrawal,
                timestamp=timezone.now()
            )
            _clear_withdrawal_pin_verified(request)
            if expects_json:
                return JsonResponse({'status': 'success', 'message': 'Withdrawal request submitted successfully.'})
            messages.success(request, 'Withdrawal request submitted successfully. It will be reviewed by an admin.')
        else:
            if expects_json:
                msg = "Invalid withdrawal request."
                if form.non_field_errors():
                    msg = " ".join([str(e) for e in form.non_field_errors()])
                return JsonResponse({'status': 'error', 'message': msg}, status=400)
            for field, errors in form.errors.items():
                for error in errors:
                    messages.error(request, f"{field.replace('_', ' ').title()}: {error}")
            if form.non_field_errors():
                for error in form.non_field_errors():
                    messages.error(request, f"Withdrawal Error: {error}")
    return redirect('betting:wallet')


def _set_withdrawal_pin_verified(request):
    request.session['withdrawal_pin_verified_at'] = timezone.now().timestamp()


def _clear_withdrawal_pin_verified(request):
    if 'withdrawal_pin_verified_at' in request.session:
        del request.session['withdrawal_pin_verified_at']


def _is_withdrawal_pin_verified_recent(request, max_age_seconds=300):
    ts = request.session.get('withdrawal_pin_verified_at')
    if not ts:
        return False
    try:
        ts_val = float(ts)
    except (TypeError, ValueError):
        return False
    return (timezone.now().timestamp() - ts_val) <= max_age_seconds


@login_required
@db_transaction.atomic
def verify_withdrawal_pin(request):
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'Invalid request.'}, status=400)

    if request.headers.get('Content-Type', '').startswith('application/json'):
        try:
            payload = json.loads(request.body or '{}')
        except Exception:
            payload = {}
        raw_pin = (payload.get('pin') or '').strip()
    else:
        raw_pin = (request.POST.get('pin') or '').strip()

    user = User.objects.select_for_update().get(pk=request.user.pk)

    if user.user_type not in ['master_agent', 'super_agent', 'agent']:
        return JsonResponse({'status': 'error', 'message': 'You are not authorized to withdraw funds.'}, status=403)

    if user.maybe_auto_unlock_withdrawal():
        user.save(update_fields=['withdrawal_locked', 'withdrawal_attempts', 'withdrawal_locked_at'])

    if user.withdrawal_locked:
        expires_at = user.get_withdrawal_lock_expires_at()
        retry_at = expires_at.isoformat() if expires_at else None
        return JsonResponse(
            {
                'status': 'locked',
                'message': 'Withdrawal access has been disabled. Retry after 24 hours or contact administrator.',
                'retry_at': retry_at
            },
            status=423
        )

    if not user.withdrawal_pin_is_set:
        return JsonResponse({'status': 'no_pin', 'message': 'Please create your Withdrawal PIN first.', 'redirect_url': reverse('betting:profile')}, status=400)

    if not raw_pin:
        return JsonResponse({'status': 'error', 'message': 'Withdrawal PIN is required.'}, status=400)

    if user.check_withdrawal_pin(raw_pin):
        user.withdrawal_attempts = 0
        user.withdrawal_locked = False
        user.withdrawal_locked_at = None
        user.save(update_fields=['withdrawal_attempts', 'withdrawal_locked', 'withdrawal_locked_at'])
        _set_withdrawal_pin_verified(request)
        return JsonResponse({'status': 'success', 'message': 'PIN verified.'})

    user.withdrawal_attempts = (user.withdrawal_attempts or 0) + 1
    remaining = max(0, 3 - int(user.withdrawal_attempts))
    if user.withdrawal_attempts >= 3:
        user.withdrawal_locked = True
        user.withdrawal_locked_at = timezone.now()
    user.save(update_fields=['withdrawal_attempts', 'withdrawal_locked', 'withdrawal_locked_at'])

    if user.withdrawal_locked:
        expires_at = user.get_withdrawal_lock_expires_at()
        retry_at = expires_at.isoformat() if expires_at else None
        return JsonResponse(
            {
                'status': 'locked',
                'message': 'Withdrawal access has been disabled. Retry after 24 hours or contact administrator.',
                'retry_at': retry_at
            },
            status=423
        )
    return JsonResponse({'status': 'error', 'message': 'Invalid withdrawal PIN.', 'attempts_remaining': remaining}, status=400)


@login_required
@db_transaction.atomic
def wallet_transfer(request):
    # Check if user has permission to manage downline wallets
    if request.user.user_type in ['master_agent', 'super_agent'] and not getattr(request.user, 'can_manage_downline_wallets', True):
        CreditLog.objects.create(
            actor=request.user,
            action_type='wallet_transfer_denied',
            amount=Decimal('0.00')
        )
        messages.error(request, "You do not have permission to credit or debit downline wallets. Please contact the administrator.")
        return redirect('betting:wallet')

    if request.method == 'POST':
        form = WalletTransferForm(sender_user=request.user, data=request.POST) # Pass sender_user explicitly
        if form.is_valid():
            recipient = form.cleaned_data['recipient_user_obj'] # Get recipient object from form's clean method
            amount = form.cleaned_data['amount']
            transaction_type = form.cleaned_data['transaction_type']
            description = form.cleaned_data['description']

            sender_wallet = get_object_or_404(Wallet.objects.select_for_update(), user=request.user)
            recipient_wallet = get_object_or_404(Wallet.objects.select_for_update(), user=recipient)

            if transaction_type == 'credit':
                sender_wallet.balance -= amount
                recipient_wallet.balance += amount
                transfer_description_sender = f"Sent funds to {recipient.email}: {description}"
                transfer_description_recipient = f"Received funds from {request.user.email}: {description}"
                transaction_type_sender = 'wallet_transfer_out' # Corrected type
                transaction_type_recipient = 'wallet_transfer_in' # Corrected type
            elif transaction_type == 'debit':
                sender_wallet.balance += amount
                recipient_wallet.balance -= amount
                transfer_description_sender = f"Received funds from {recipient.email}: {description}"
                transfer_description_recipient = f"Sent funds to {request.user.email}: {description}"
                transaction_type_sender = 'wallet_transfer_in' # From sender's perspective
                transaction_type_recipient = 'wallet_transfer_out' # From recipient's perspective
            
            sender_wallet.save()
            recipient_wallet.save()

            Transaction.objects.create(
                user=request.user,
                initiating_user=request.user,
                target_user=recipient,
                transaction_type=transaction_type_sender,
                amount=amount,
                is_successful=True,
                status='completed',
                description=transfer_description_sender,
                timestamp=timezone.now()
            )
            Transaction.objects.create(
                user=recipient,
                initiating_user=request.user, # The one who initiated it
                target_user=recipient,
                transaction_type=transaction_type_recipient,
                amount=amount,
                is_successful=True,
                status='completed',
                description=transfer_description_recipient,
                timestamp=timezone.now()
            )
            messages.success(request, f'Successfully completed transfer of ₦{amount:.2f} to/from {recipient.email}.')
        else:
            for field, errors in form.errors.items():
                for error in errors:
                    messages.error(request, f"{field.replace('_', ' ').title()}: {error}")
            if form.non_field_errors():
                for error in form.non_field_errors():
                    messages.error(request, f"Transfer Error: {error}")
    return redirect('betting:wallet')

@login_required
def submit_credit_request(request):
    if request.method == 'POST':
        form = CreditRequestForm(request.POST, user=request.user)
        if form.is_valid():
            pending_exists = CreditRequest.objects.filter(requester=request.user, status='pending').exists()
            if pending_exists:
                 messages.warning(request, "You already have a pending credit request.")
                 return redirect('betting:wallet')

            credit_request = form.save(commit=False)
            credit_request.requester = request.user
            credit_request.status = 'pending'
            credit_request.save()
            
            CreditLog.objects.create(
                actor=request.user,
                target_user=credit_request.recipient,
                action_type='request_created',
                amount=credit_request.amount,
                status='pending',
                reference_id=str(credit_request.id)
            )

            messages.success(request, "Credit request submitted successfully.")
            return redirect('betting:wallet')
        else:
             for field, errors in form.errors.items():
                for error in errors:
                    messages.error(request, f"{field}: {error}")
    return redirect('betting:wallet')

@login_required
def manage_credit_requests(request):
    received_requests = CreditRequest.objects.filter(recipient=request.user).order_by('-created_at')
    sent_requests = CreditRequest.objects.filter(requester=request.user).order_by('-created_at')
    
    return render(request, 'betting/manage_credit_requests.html', {
        'received_requests': received_requests,
        'sent_requests': sent_requests
    })

@login_required
@db_transaction.atomic
def approve_credit_request(request, request_id):
    credit_req = get_object_or_404(CreditRequest, id=request_id, recipient=request.user)
    
    if request.method == 'POST':
        action = request.POST.get('action')
        
        if credit_req.status != 'pending':
            messages.error(request, "This request has already been processed.")
            return redirect('betting:manage_credit_requests')
            
        if action == 'decline':
            credit_req.status = 'declined'
            credit_req.save()
            CreditLog.objects.create(
                actor=request.user,
                target_user=credit_req.requester,
                action_type='request_declined',
                amount=credit_req.amount,
                status='declined',
                reference_id=str(credit_req.id)
            )
            messages.info(request, "Request declined.")
            
        elif action == 'approve':
            lender_wallet = Wallet.objects.select_for_update().get(user=request.user)
            borrower_wallet = Wallet.objects.select_for_update().get(user=credit_req.requester)
            
            if lender_wallet.balance < credit_req.amount:
                messages.error(request, "Insufficient funds to approve this request.")
                return redirect('betting:manage_credit_requests')
                
            lender_wallet.balance -= credit_req.amount
            borrower_wallet.balance += credit_req.amount
            
            lender_wallet.save()
            borrower_wallet.save()
            
            credit_req.status = 'approved'
            credit_req.save()
            
            if credit_req.request_type == 'loan':
                Loan.objects.create(
                    borrower=credit_req.requester,
                    lender=request.user,
                    amount=credit_req.amount,
                    outstanding_balance=credit_req.amount,
                    status='active',
                    credit_request=credit_req,
                    due_date=timezone.now() + timedelta(days=7)
                )
                
            Transaction.objects.create(
                user=request.user,
                transaction_type='wallet_transfer_out',
                amount=credit_req.amount,
                status='completed',
                is_successful=True,
                target_user=credit_req.requester,
                description=f"Approved {credit_req.request_type} request to {credit_req.requester.email}"
            )
            
            Transaction.objects.create(
                user=credit_req.requester,
                transaction_type='wallet_transfer_in',
                amount=credit_req.amount,
                status='completed',
                is_successful=True,
                initiating_user=request.user,
                description=f"Received {credit_req.request_type} from {request.user.email}"
            )
            
            CreditLog.objects.create(
                actor=request.user,
                target_user=credit_req.requester,
                action_type='request_approved',
                amount=credit_req.amount,
                status='approved',
                reference_id=str(credit_req.id)
            )
            
            messages.success(request, f"Request approved. Funds transferred.")
            
    return redirect('betting:manage_credit_requests')

@login_required
@db_transaction.atomic
def settle_loan(request, loan_id):
    loan = get_object_or_404(Loan, id=loan_id, borrower=request.user)
    
    if request.method == 'POST':
        form = LoanSettlementForm(request.POST)
        if form.is_valid():
            method = form.cleaned_data['settlement_method']
            
            if method == 'wallet':
                borrower_wallet = Wallet.objects.select_for_update().get(user=request.user)
                lender_wallet = Wallet.objects.select_for_update().get(user=loan.lender)
                
                amount_to_pay = loan.outstanding_balance
                
                if borrower_wallet.balance < amount_to_pay:
                    messages.error(request, "Insufficient wallet balance.")
                    return redirect('betting:wallet')
                
                borrower_wallet.balance -= amount_to_pay
                lender_wallet.balance += amount_to_pay
                
                borrower_wallet.save()
                lender_wallet.save()
                
                loan.outstanding_balance = Decimal('0.00')
                loan.status = 'settled'
                loan.save()
                
                Transaction.objects.create(
                    user=request.user,
                    transaction_type='wallet_transfer_out',
                    amount=amount_to_pay,
                    status='completed',
                    is_successful=True,
                    target_user=loan.lender,
                    description=f"Loan repayment to {loan.lender.email}"
                )
                
                Transaction.objects.create(
                    user=loan.lender,
                    transaction_type='wallet_transfer_in',
                    amount=amount_to_pay,
                    status='completed',
                    is_successful=True,
                    initiating_user=request.user,
                    description=f"Loan repayment received from {request.user.email}"
                )
                
                CreditLog.objects.create(
                    actor=request.user,
                    target_user=loan.lender,
                    action_type='loan_settled_wallet',
                    amount=amount_to_pay,
                    status='settled',
                    reference_id=str(loan.id)
                )
                
                messages.success(request, "Loan settled successfully via wallet.")
                
            elif method == 'deposit':
                messages.info(request, "Please deposit funds to your wallet to settle the loan.")
                return redirect('betting:wallet')
                
    return redirect('betting:wallet')


# --- User Profile Views ---

@login_required
def profile_view(request):
    if request.user.maybe_auto_unlock_withdrawal():
        request.user.save(update_fields=['withdrawal_locked', 'withdrawal_attempts', 'withdrawal_locked_at'])
    if request.user.withdrawal_locked and not request.user.withdrawal_locked_at:
        request.user.withdrawal_locked_at = timezone.now()
        request.user.save(update_fields=['withdrawal_locked_at'])
    user_wallet = get_object_or_404(Wallet, user=request.user)
    transactions = Transaction.objects.filter(user=request.user).order_by('-timestamp')[:10] # Last 10 transactions

    # Calculate total deposits and withdrawals in the view
    total_deposits = Transaction.objects.filter(
        user=request.user, 
        transaction_type='deposit', 
        is_successful=True
    ).aggregate(Sum('amount'))['amount__sum'] or Decimal('0.00')

    total_withdrawals = UserWithdrawal.objects.filter(
        user=request.user, 
        status='approved'
    ).aggregate(Sum('amount'))['amount__sum'] or Decimal('0.00')


    if request.method == 'POST':
        profile_form = ProfileEditForm(request.POST, instance=request.user)
        password_form = PasswordChangeForm(request.user, request.POST)
        pin_create_form = WithdrawalPinCreateForm(request.POST)
        pin_reset_form = WithdrawalPinResetForm(request.POST, user=request.user)
        
        if 'profile_submit' in request.POST:
            if profile_form.is_valid():
                profile_form.save()
                messages.success(request, 'Your profile has been updated successfully.')
                return redirect('betting:profile')
            else:
                for field, errors in profile_form.errors.items():
                    for error in errors:
                        messages.error(request, f"Profile - {field.replace('_', ' ').title()}: {error}")
                if profile_form.non_field_errors():
                    for error in profile_form.non_field_errors():
                        messages.error(request, f"Profile Error: {error}")
        
        elif 'password_submit' in request.POST:
            if password_form.is_valid():
                password_form.save()
                messages.success(request, 'Your password has been changed successfully. Please log in again.')
                return redirect('betting:login') # Redirect to login after password change
            else:
                for field, errors in password_form.errors.items():
                    for error in errors:
                        messages.error(request, f"Password - {field.replace('_', ' ').title()}: {error}")
                if password_form.non_field_errors():
                    for error in password_form.non_field_errors():
                        messages.error(request, f"Password Error: {error}")
        elif 'withdrawal_pin_create_submit' in request.POST:
            if request.user.withdrawal_pin_is_set:
                messages.warning(request, "Withdrawal PIN is already set. Use Reset Withdrawal PIN instead.")
                return redirect('betting:profile')
            if pin_create_form.is_valid():
                request.user.set_withdrawal_pin(pin_create_form.cleaned_data['pin'])
                request.user.withdrawal_attempts = 0
                request.user.withdrawal_locked = False
                request.user.withdrawal_locked_at = None
                request.user.save(update_fields=['withdrawal_pin', 'withdrawal_attempts', 'withdrawal_locked', 'withdrawal_locked_at'])
                messages.success(request, "Withdrawal PIN created successfully.")
                return redirect('betting:profile')
            else:
                for error in pin_create_form.non_field_errors():
                    messages.error(request, error)
        elif 'withdrawal_pin_reset_submit' in request.POST:
            if pin_reset_form.is_valid():
                request.user.set_withdrawal_pin(pin_reset_form.cleaned_data['new_pin'])
                request.user.withdrawal_attempts = 0
                request.user.withdrawal_locked = False
                request.user.withdrawal_locked_at = None
                request.user.save(update_fields=['withdrawal_pin', 'withdrawal_attempts', 'withdrawal_locked', 'withdrawal_locked_at'])
                messages.success(request, "Withdrawal PIN updated successfully.")
                return redirect('betting:profile')
            else:
                for error in pin_reset_form.non_field_errors():
                    messages.error(request, error)
    else:
        profile_form = ProfileEditForm(instance=request.user)
        password_form = PasswordChangeForm(request.user)
        pin_create_form = WithdrawalPinCreateForm()
        pin_reset_form = WithdrawalPinResetForm(user=request.user)

    context = {
        'profile_form': profile_form,
        'password_form': password_form,
        'pin_create_form': pin_create_form,
        'pin_reset_form': pin_reset_form,
        'withdrawal_lock_expires_at': request.user.get_withdrawal_lock_expires_at(),
        'wallet': user_wallet,
        'transactions': transactions,
        'total_deposits': total_deposits,       # Add to context
        'total_withdrawals': total_withdrawals, # Add to context
    }
    return render(request, 'betting/profile.html', context)


@login_required
def change_password(request):
    if request.method == 'POST':
        form = PasswordChangeForm(request.user, request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, 'Your password was successfully updated!')
            return redirect('betting:login') # Log out user after password change for security
        else:
            for field, errors in form.errors.items():
                for error in errors:
                    messages.error(request, f"{field.replace('_', ' ').title()}: {error}")
            if form.non_field_errors():
                for error in form.non_field_errors():
                    messages.error(request, f"Password Change Error: {error}")
    else:
        form = PasswordChangeForm(request.user)
    return render(request, 'betting/change_password.html', {'form': form})


@login_required
def user_dashboard(request):
    """
    Unified dashboard entry point. Redirects special roles to their dashboards,
    and renders the standard user dashboard for players.
    """
    user = request.user
    
    # Redirect special users to their specific dashboards
    if user.is_superuser or user.user_type == 'admin':
        return redirect('betting:admin_dashboard')
    elif user.user_type == 'master_agent':
        return redirect('betting:master_agent_dashboard')
    elif user.user_type == 'super_agent':
        return redirect('betting:super_agent_dashboard')
    elif user.user_type == 'account_user':
        return redirect('betting:account_user_dashboard')
    elif user.user_type == 'agent' or user.user_type == 'cashier':
        return redirect('betting:agent_dashboard')
        
    # Standard User / Player Dashboard Logic
    recent_tickets = BetTicket.objects.filter(user=user).order_by('-placed_at')[:10]
    active_bets_count = BetTicket.objects.filter(user=user, status='pending').count()
    
    # Calculate Total Winnings (only WON tickets)
    total_winnings = BetTicket.objects.filter(user=user, status='won').aggregate(Sum('max_winning'))['max_winning__sum'] or Decimal('0.00')

    context = {
        'recent_tickets': recent_tickets,
        'active_bets_count': active_bets_count,
        'total_winnings': total_winnings,
    }
    return render(request, 'betting/user_dashboard.html', context)


# --- Agent/Super Agent/Master Agent specific Views ---

@login_required
@user_passes_test(lambda u: u.user_type in ['agent', 'super_agent', 'master_agent', 'cashier'])
def agent_dashboard(request):
    user = request.user
    today = timezone.now().date()
    start_of_week = today - timedelta(days=today.weekday()) # Monday
    start_of_month = today.replace(day=1)
    start_date_str = request.GET.get('start_date') or ''
    end_date_str = request.GET.get('end_date') or ''
    start_date = None
    end_date = None
    try:
        if start_date_str:
            start_date = date.fromisoformat(start_date_str)
    except Exception:
        start_date = None
    try:
        if end_date_str:
            end_date = date.fromisoformat(end_date_str)
    except Exception:
        end_date = None
    if start_date and end_date and start_date > end_date:
        start_date, end_date = end_date, start_date

    direct_downline_rows = []
    master_downline_tree = []
    super_downline_tree = []
    if user.user_type == 'master_agent':
        direct_super_agents_qs = (
            User.objects.filter(user_type='super_agent', master_agent=user)
            .select_related('state')
            .order_by('email')
        )
        agents_qs = (
            User.objects.filter(user_type='agent', super_agent__in=direct_super_agents_qs)
            .select_related('state', 'super_agent')
            .order_by('email')
        )
        cashiers_qs = (
            User.objects.filter(user_type='cashier', agent__in=agents_qs)
            .select_related('state', 'agent')
            .order_by('email')
        )

        wallet_map = _get_wallet_balance_map(
            list(direct_super_agents_qs.values_list('id', flat=True))
            + list(agents_qs.values_list('id', flat=True))
            + list(cashiers_qs.values_list('id', flat=True))
        )

        cashiers_by_agent = {}
        for cashier in cashiers_qs:
            cashiers_by_agent.setdefault(cashier.agent_id, []).append(cashier)

        agent_totals = {}
        for ag in agents_qs:
            total = wallet_map.get(ag.id) or Decimal('0.00')
            for cashier in cashiers_by_agent.get(ag.id, []):
                total += wallet_map.get(cashier.id) or Decimal('0.00')
            agent_totals[ag.id] = total

        agents_by_sa = {}
        for ag in agents_qs:
            agents_by_sa.setdefault(ag.super_agent_id, []).append(ag)

        for sa in direct_super_agents_qs:
            sa_agents = agents_by_sa.get(sa.id, [])
            sa_total = wallet_map.get(sa.id) or Decimal('0.00')
            agent_rows = []
            for ag in sa_agents:
                cashier_rows = []
                for cashier in cashiers_by_agent.get(ag.id, []):
                    cashier_rows.append({
                        'user': cashier,
                        'balance': wallet_map.get(cashier.id) or Decimal('0.00'),
                    })
                agent_rows.append({
                    'user': ag,
                    'total_balance': agent_totals.get(ag.id) or Decimal('0.00'),
                    'cashiers': cashier_rows,
                })
                sa_total += agent_totals.get(ag.id) or Decimal('0.00')

            master_downline_tree.append({
                'user': sa,
                'total_balance': sa_total,
                'agents': agent_rows,
            })

    elif user.user_type == 'super_agent':
        direct_agents_qs = (
            User.objects.filter(user_type='agent', super_agent=user)
            .select_related('state')
            .order_by('email')
        )
        cashiers_qs = (
            User.objects.filter(user_type='cashier', agent__in=direct_agents_qs)
            .select_related('state', 'agent')
            .order_by('email')
        )

        wallet_map = _get_wallet_balance_map(
            list(direct_agents_qs.values_list('id', flat=True))
            + list(cashiers_qs.values_list('id', flat=True))
        )

        cashiers_by_agent = {}
        for cashier in cashiers_qs:
            cashiers_by_agent.setdefault(cashier.agent_id, []).append(cashier)

        for ag in direct_agents_qs:
            total = wallet_map.get(ag.id) or Decimal('0.00')
            cashier_rows = []
            for cashier in cashiers_by_agent.get(ag.id, []):
                bal = wallet_map.get(cashier.id) or Decimal('0.00')
                total += bal
                cashier_rows.append({'user': cashier, 'balance': bal})

            super_downline_tree.append({
                'user': ag,
                'total_balance': total,
                'cashiers': cashier_rows,
            })

    elif user.user_type == 'agent':
        direct_cashiers_qs = User.objects.filter(user_type='cashier', agent=user).select_related('state')
        for ca in direct_cashiers_qs:
            direct_downline_rows.append({'user': ca, 'aggregated_balance': getattr(getattr(ca, 'wallet', None), 'balance', Decimal('0.00'))})
        direct_downline_rows.sort(key=lambda r: (r['user'].email or '').lower())

    # Get downline users
    if user.user_type == 'master_agent':
        downline_users_qs = User.objects.filter(
            Q(master_agent=user) |
            Q(super_agent__master_agent=user) |
            Q(agent__super_agent__master_agent=user)
        )
    elif user.user_type == 'super_agent':
        downline_users_qs = User.objects.filter(
            Q(super_agent=user) |
            Q(agent__super_agent=user)
        )
    elif user.user_type == 'agent':
        downline_users_qs = User.objects.filter(agent=user)
    elif user.user_type == 'cashier':
        # Cashiers see their own tickets, but have no downline users
        downline_users_qs = User.objects.none()
    else:
        downline_users_qs = User.objects.none()

    total_downline_users = downline_users_qs.count()

    # Calculate GGR for downline (sum of GGR from their bet tickets)
    # GGR is Turnover - Winnings
    # We need to consider all bet tickets placed by downline users
    if user.user_type == 'cashier':
        downline_bet_tickets = BetTicket.objects.filter(user=user).exclude(status__in=['deleted', 'cancelled'])
    else:
        downline_bet_tickets = BetTicket.objects.filter(
            user__in=downline_users_qs
        ).exclude(status__in=['deleted', 'cancelled'])

    scope = request.GET.get('scope', 'all')
    if start_date and end_date:
        metrics_start = start_date
        metrics_end = end_date
        downline_bet_tickets = downline_bet_tickets.filter(
            placed_at__date__gte=metrics_start,
            placed_at__date__lte=metrics_end,
        )
        metrics_label = 'Custom'
        scope = 'custom'
    elif scope == 'week':
        try:
            from commission.models import CommissionPeriod
        except Exception:
            CommissionPeriod = None

        period = None
        if CommissionPeriod is not None:
            period = (
                CommissionPeriod.objects.filter(
                    period_type='weekly',
                    start_date__lte=today,
                    end_date__gte=today,
                )
                .order_by('-start_date')
                .first()
            )

        metrics_start = period.start_date if period else start_of_week
        metrics_end = period.end_date if period else today
        downline_bet_tickets = downline_bet_tickets.filter(
            placed_at__date__gte=metrics_start,
            placed_at__date__lte=metrics_end,
        )
        metrics_label = 'Weekly'
    elif scope == 'month':
        try:
            from commission.models import CommissionPeriod
        except Exception:
            CommissionPeriod = None

        period = None
        if CommissionPeriod is not None:
            period = (
                CommissionPeriod.objects.filter(
                    period_type='monthly',
                    start_date__lte=today,
                    end_date__gte=today,
                )
                .order_by('-start_date')
                .first()
            )

        metrics_start = period.start_date if period else start_of_month
        metrics_end = period.end_date if period else today
        downline_bet_tickets = downline_bet_tickets.filter(
            placed_at__date__gte=metrics_start,
            placed_at__date__lte=metrics_end,
        )
        metrics_label = 'Monthly'
    else:
        scope = 'all'
        metrics_label = 'All Time'
        metrics_start = None
        metrics_end = None

    # Aggregate total turnover and winnings for downline
    total_downline_turnover = downline_bet_tickets.aggregate(Sum('stake_amount'))['stake_amount__sum'] or Decimal('0.00')
    total_downline_winnings = downline_bet_tickets.filter(status='won').aggregate(Sum('max_winning'))['max_winning__sum'] or Decimal('0.00')
    
    downline_ggr = total_downline_turnover - total_downline_winnings

    # Commission calculations
    total_commission_paid = Decimal('0.00')
    pending_commission = Decimal('0.00')

    if user.user_type == 'agent':
        weekly_comms = WeeklyAgentCommission.objects.filter(agent=user)
        total_commission_paid = weekly_comms.filter(status='paid').aggregate(Sum('commission_total_amount'))['commission_total_amount__sum'] or Decimal('0.00')
        pending_commission = weekly_comms.filter(status='pending').aggregate(Sum('commission_total_amount'))['commission_total_amount__sum'] or Decimal('0.00')
    elif user.user_type in ['super_agent', 'master_agent']:
        monthly_comms = MonthlyNetworkCommission.objects.filter(user=user)
        total_commission_paid = monthly_comms.filter(status='paid').aggregate(Sum('commission_amount'))['commission_amount__sum'] or Decimal('0.00')
        pending_commission = monthly_comms.filter(status='pending').aggregate(Sum('commission_amount'))['commission_amount__sum'] or Decimal('0.00')

    # Top performing users/agents (example: based on GGR)
    # CORRECTED: Changed 'betticket__' to 'bet_tickets__'
    tickets_date_filter = None
    if metrics_start and metrics_end:
        tickets_date_filter = Q(bet_tickets__placed_at__date__gte=metrics_start, bet_tickets__placed_at__date__lte=metrics_end)

    stake_case = Case(
        When(bet_tickets__status__in=['won', 'lost', 'pending', 'cashed_out', 'cancelled'], then=F('bet_tickets__stake_amount')),
        default=Value(0),
        output_field=DecimalField()
    )
    win_case = Case(
        When(bet_tickets__status='won', then=F('bet_tickets__potential_winning')),
        default=Value(0),
        output_field=DecimalField()
    )

    if tickets_date_filter is not None:
        top_performers = downline_users_qs.annotate(
            user_total_stake=Sum(stake_case, filter=tickets_date_filter),
            user_total_winnings=Sum(win_case, filter=tickets_date_filter),
        ).annotate(
            user_ggr=F('user_total_stake') - F('user_total_winnings')
        ).order_by('-user_ggr')[:5]
    else:
        top_performers = downline_users_qs.annotate(
            user_total_stake=Sum(stake_case),
            user_total_winnings=Sum(win_case),
        ).annotate(
            user_ggr=F('user_total_stake') - F('user_total_winnings')
        ).order_by('-user_ggr')[:5] # Top 5 based on GGR

    # Recent activity from downline users
    recent_downline_transactions = Transaction.objects.filter(
        Q(user__in=downline_users_qs) | Q(initiating_user__in=downline_users_qs)
    )
    if metrics_start and metrics_end:
        recent_downline_transactions = recent_downline_transactions.filter(
            timestamp__date__gte=metrics_start,
            timestamp__date__lte=metrics_end,
        )
    recent_downline_transactions = recent_downline_transactions.order_by('-timestamp')[:10]

    sort_by = request.GET.get('sort_by') or 'placed_at'
    sort_dir = request.GET.get('sort_dir') or 'desc'
    sort_map = {
        'placed_at': 'placed_at',
        'stake': 'stake_amount',
        'potential': 'potential_winning',
        'max': 'max_winning',
        'status': 'status',
        'user': 'user__email',
    }
    sort_field = sort_map.get(sort_by, 'placed_at')
    order_expr = f"-{sort_field}" if sort_dir == 'desc' else sort_field

    context = {
        'user': user,
        'downline_users': downline_users_qs, # Pass the QuerySet
        'direct_downline_rows': direct_downline_rows,
        'master_downline_tree': master_downline_tree,
        'super_downline_tree': super_downline_tree,
        'downline_bet_tickets': downline_bet_tickets.order_by(order_expr)[:50], # Pass the QuerySet, sliced
        'total_downline_users': total_downline_users,
        'total_downline_turnover': total_downline_turnover,
        'total_downline_stake': total_downline_turnover, # Alias for total stake
        'total_downline_winnings': total_downline_winnings,
        'downline_ggr': downline_ggr,
        'metrics_scope': scope,
        'metrics_label': metrics_label,
        'metrics_start': metrics_start,
        'metrics_end': metrics_end,
        'current_start_date': metrics_start.isoformat() if metrics_start else '',
        'current_end_date': metrics_end.isoformat() if metrics_end else '',
        'current_sort_by': sort_by,
        'current_sort_dir': sort_dir,
        'total_commission_paid': total_commission_paid,
        'pending_commission': pending_commission,
        'top_performers': top_performers,
        'recent_downline_transactions': recent_downline_transactions,
        'show_reports': True,
    }
    return render(request, 'betting/agent_dashboard.html', context)


def footer_page(request, slug):
    page = get_object_or_404(FooterPage, slug=slug, is_active=True)
    return render(request, 'betting/footer_page.html', {'page': page})


def betting_results_view(request):
    """
    View for displaying betting results filtered by BettingPeriod, Serial Number, and Date.
    """
    period_id = request.GET.get('period_id')
    serial_number = request.GET.get('serial_number')
    match_date = request.GET.get('match_date')
    
    # Get all betting periods (including past ones) for the dropdown, latest first
    betting_periods = BettingPeriod.objects.all().order_by('-start_date')
    
    # Default to the most recent period if none selected
    if not period_id and betting_periods.exists():
        selected_period = betting_periods.first()
        period_id = selected_period.id
    else:
        selected_period = get_object_or_404(BettingPeriod, id=period_id) if period_id else None

    fixtures = Fixture.objects.none()
    if selected_period:
        # Base queryset for the selected period
        fixtures = Fixture.objects.filter(
            betting_period=selected_period
        )
        
        # Apply Serial Number filter
        if serial_number:
            fixtures = fixtures.filter(serial_number=serial_number)
            
        # Apply Date filter
        if match_date:
            fixtures = fixtures.filter(match_date=match_date)
            
        fixtures = fixtures.order_by('serial_number')

    context = {
        'betting_periods': betting_periods,
        'selected_period': selected_period,
        'fixtures': fixtures,
        'page_title': 'Betting Results',
        'current_serial': serial_number,
        'current_date': match_date
    }
    return render(request, 'betting/results.html', context)


@login_required
@user_passes_test(is_master_agent)
def master_agent_dashboard(request):
    return agent_dashboard(request)


@login_required
@user_passes_test(is_super_agent)
def super_agent_dashboard(request):
    return agent_dashboard(request)


@login_required
@user_passes_test(lambda u: u.user_type in ['agent', 'super_agent', 'master_agent', 'admin'])
def downline_users(request):
    user = request.user
    user_type_filter = request.GET.get('user_type', 'all')
    search_query = request.GET.get('q', '')

    queryset = User.objects.all()

    # Filter based on current user's hierarchy
    if user.user_type == 'master_agent':
        queryset = queryset.filter(
            Q(master_agent=user) |
            Q(super_agent__master_agent=user) |
            Q(agent__super_agent__master_agent=user)
        )
    elif user.user_type == 'super_agent':
        queryset = queryset.filter(
            Q(super_agent=user) |
            Q(agent__super_agent=user)
        )
    elif user.user_type == 'agent':
        queryset = queryset.filter(agent=user)
    else:
        queryset = User.objects.none() # Non-admin/agent types only see themselves

    # Apply user type filter from GET parameter
    if user_type_filter != 'all':
        queryset = queryset.filter(user_type=user_type_filter)

    # Apply search query
    if search_query:
        queryset = queryset.filter(
            Q(email__icontains=search_query) |
            Q(first_name__icontains=search_query) |
            Q(last_name__icontains=search_query) |
            Q(phone_number__icontains=search_query)
        )

    # Exclude the logged-in user from the list if they are in the downline
    # Unless the user is an admin viewing all users
    if not (user.is_superuser or user.user_type == 'admin'):
        queryset = queryset.exclude(pk=user.pk)

    paginator = Paginator(queryset.order_by('email'), 10) # 10 users per page
    page_number = request.GET.get('page')

    try:
        downline_users_paginated = paginator.page(page_number)
    except PageNotAnInteger:
        downline_users_paginated = paginator.page(1)
    except EmptyPage:
        downline_users_paginated = paginator.page(paginator.num_pages)

    context = {
        'downline_users': downline_users_paginated,
        'user_type_choices': User.USER_TYPE_CHOICES,
        'current_user_type_filter': user_type_filter,
        'current_search_query': search_query,
    }
    return render(request, 'betting/downline_users.html', context)


@login_required
@user_passes_test(lambda u: u.user_type in ['agent', 'super_agent', 'master_agent', 'admin'])
def downline_bets(request):
    user = request.user
    status_filter = request.GET.get('status', 'all')
    user_filter = request.GET.get('user', 'all')
    start_date_str = request.GET.get('start_date')
    end_date_str = request.GET.get('end_date')

    # Default to last 30 days if no dates provided
    end_date = date.fromisoformat(end_date_str) if end_date_str else timezone.now().date()
    start_date = date.fromisoformat(start_date_str) if start_date_str else end_date - timedelta(days=30)

    # Determine downline users based on logged-in user's hierarchy
    if user.user_type == 'admin' or user.is_superuser:
        downline_users_qs = User.objects.all() # Admins can see all users' bets
    elif user.user_type == 'master_agent':
        downline_users_qs = User.objects.filter(
            Q(master_agent=user) |
            Q(super_agent__master_agent=user) |
            Q(agent__super_agent__master_agent=user)
        )
    elif user.user_type == 'super_agent':
        downline_users_qs = User.objects.filter(
            Q(super_agent=user) |
            Q(agent__super_agent=user)
        )
    elif user.user_type == 'agent':
        downline_users_qs = User.objects.filter(agent=user)
    else:
        downline_users_qs = User.objects.none() # Should not happen with decorator, but for safety

    # Filter bet tickets by downline users
    bet_tickets = BetTicket.objects.filter(user__in=downline_users_qs)

    # Apply status filter
    if status_filter != 'all':
        bet_tickets = bet_tickets.filter(status=status_filter)

    # Apply specific user filter if provided
    if user_filter != 'all':
        try:
            user_id = int(user_filter)
            bet_tickets = bet_tickets.filter(user__id=user_id)
        except (ValueError, TypeError):
            pass # Invalid user ID, ignore filter

    # Apply date filters
    if start_date_str:
        try:
            start_date = date.fromisoformat(start_date_str)
            bet_tickets = bet_tickets.filter(placed_at__date__gte=start_date)
        except ValueError:
            messages.error(request, "Invalid start date format.")
    if end_date_str:
        try:
            end_date = date.fromisoformat(end_date_str)
            bet_tickets = bet_tickets.filter(placed_at__date__lte=end_date)
        except ValueError:
            messages.error(request, "Invalid end date format.")

    paginator = Paginator(bet_tickets.order_by('-placed_at'), 10) # 10 tickets per page
    page_number = request.GET.get('page')

    try:
        downline_bet_tickets_paginated = paginator.page(page_number)
    except PageNotAnInteger:
        downline_bet_tickets_paginated = paginator.page(1)
    except EmptyPage:
        downline_bet_tickets_paginated = paginator.page(paginator.num_pages)

    context = {
        'bet_tickets': downline_bet_tickets_paginated,
        'status_choices': [('all', 'All')] + list(BetTicket.STATUS_CHOICES),
        'current_status_filter': status_filter,
        'all_downline_users': downline_users_qs.order_by('email'), # For the user filter dropdown
        'current_user_filter': user_filter,
        'start_date_filter': start_date_str,
        'end_date_filter': end_date_str,
    }
    return render(request, 'betting/downline_bets.html', context)


@login_required
@user_passes_test(lambda u: u.user_type in ['agent', 'super_agent', 'master_agent', 'admin'])
def agent_wallet_report(request):
    user = request.user
    user_filter = request.GET.get('user', 'all')

    # Determine downline users whose wallets to report on
    if user.user_type == 'admin' or user.is_superuser:
        report_users_qs = User.objects.all() # Admins can see all
    elif user.user_type == 'master_agent':
        report_users_qs = User.objects.filter(
            Q(master_agent=user) |
            Q(super_agent__master_agent=user) |
            Q(agent__super_agent__master_agent=user)
        )
    elif user.user_type == 'super_agent':
        report_users_qs = User.objects.filter(
            Q(super_agent=user) |
            Q(agent__super_agent=user)
        )
    elif user.user_type == 'agent':
        report_users_qs = User.objects.filter(agent=user)
    else:
        report_users_qs = User.objects.none()

    # Filter wallets by the determined users
    wallets = Wallet.objects.filter(user__in=report_users_qs)

    # Apply specific user filter if provided in GET params
    if user_filter != 'all':
        try:
            filter_user_id = int(user_filter)
            wallets = wallets.filter(user__id=filter_user_id)
        except (ValueError, TypeError):
            pass # Invalid user ID, ignore filter

    context = {
        'report_title': 'Agent/Admin Wallet Report',
        'wallets': wallets.order_by('user__email'),
        'all_users': report_users_qs.order_by('email'), # For the filter dropdown
        'current_user_filter': user_filter,
    }
    return render(request, 'betting/reports/wallet_report.html', context)


@login_required
@user_passes_test(lambda u: u.is_superuser or u.user_type in ['admin', 'agent', 'super_agent', 'master_agent'])
def agent_sales_winnings_report(request):
    user = request.user
    user_filter = request.GET.get('user', 'all')
    start_date_str = request.GET.get('start_date')
    end_date_str = request.GET.get('end_date')

    # Default to last 30 days if no dates provided
    end_date = date.fromisoformat(end_date_str) if end_date_str else timezone.now().date()
    start_date = date.fromisoformat(start_date_str) if start_date_str else end_date - timedelta(days=30)

    # Determine relevant users based on hierarchy
    if user.user_type == 'admin' or user.is_superuser:
        relevant_users_qs = User.objects.all()
    elif user.user_type == 'master_agent':
        relevant_users_qs = User.objects.filter(
            Q(master_agent=user) |
            Q(super_agent__master_agent=user) |
            Q(agent__super_agent__master_agent=user)
        ).distinct()
    elif user.user_type == 'super_agent':
        relevant_users_qs = User.objects.filter(
            Q(super_agent=user) |
            Q(agent__super_agent=user)
        ).distinct()
    elif user.user_type == 'agent':
        relevant_users_qs = User.objects.filter(agent=user).distinct()
    else:
        relevant_users_qs = User.objects.none() # Should not be reached due to decorator

    # Apply specific user filter if provided
    if user_filter != 'all':
        try:
            filter_user_id = int(user_filter)
            relevant_users_qs = relevant_users_qs.filter(id=filter_user_id)
        except (ValueError, TypeError):
            pass # Invalid user ID, ignore filter

    # Aggregate sales and winnings for each relevant user within the date range
    report_data = []
    for u in relevant_users_qs.order_by('email'):
        bets_by_user = BetTicket.objects.filter(
            user=u,
            placed_at__date__gte=start_date,
            placed_at__date__lt=end_date + timedelta(days=1) # Include full end date
        ).exclude(status='deleted')

        total_stake = bets_by_user.aggregate(Sum('stake_amount'))['stake_amount__sum'] or Decimal('0.00')
        total_winnings = bets_by_user.filter(status='won').aggregate(Sum('potential_winning'))['potential_winning__sum'] or Decimal('0.00')
        
        net_result = total_stake - total_winnings

        if total_stake > Decimal('0.00') or total_winnings > Decimal('0.00'): # Only include users with activity
            report_data.append({
                'user': u,
                'total_stake': total_stake,
                'total_winnings': total_winnings,
                'net_result': net_result,
            })

    context = {
        'report_title': 'Agent/Admin Sales & Winnings Report',
        'report_data': report_data,
        'all_users': relevant_users_qs.order_by('email'),
        'current_user_filter': user_filter,
        'start_date_filter': start_date.isoformat(),
        'end_date_filter': end_date.isoformat(),
    }
    return render(request, 'betting/reports/sales_winnings_report.html', context)


@login_required
@user_passes_test(lambda u: u.is_superuser or u.user_type in ['admin', 'agent', 'super_agent', 'master_agent'])
def agent_commission_report(request):
    user = request.user
    agent_filter_id = request.GET.get('user', 'all')
    start_date_str = request.GET.get('start_date')
    end_date_str = request.GET.get('end_date')

    end_date = date.fromisoformat(end_date_str) if end_date_str else timezone.now().date()
    start_date = date.fromisoformat(start_date_str) if start_date_str else end_date - timedelta(days=30)

    # Determine which agents/users to fetch data for
    all_users = None
    if user.is_superuser or user.user_type == 'admin':
        all_users = User.objects.filter(user_type__in=['agent', 'super_agent', 'master_agent']).order_by('email')
        
    commission_data = []

    # Fetch Weekly Commissions
    weekly_qs = WeeklyAgentCommission.objects.filter(
        period__end_date__gte=start_date,
        period__start_date__lte=end_date
    ).select_related('agent', 'period')

    if user.is_superuser or user.user_type == 'admin':
        if agent_filter_id != 'all':
            weekly_qs = weekly_qs.filter(agent__id=agent_filter_id)
    else:
        weekly_qs = weekly_qs.filter(agent=user)

    for wc in weekly_qs:
        commission_data.append({
            'agent': wc.agent,
            'period': str(wc.period),
            'type': 'Weekly Agent Commission',
            'commission_amount': wc.commission_total_amount,
            'status': wc.status,
            'paid_at': wc.paid_at,
            'ggr': wc.ggr
        })

    # Fetch Monthly Network Commissions
    monthly_qs = MonthlyNetworkCommission.objects.filter(
        period__end_date__gte=start_date,
        period__start_date__lte=end_date
    ).select_related('user', 'period')

    if user.is_superuser or user.user_type == 'admin':
        if agent_filter_id != 'all':
            monthly_qs = monthly_qs.filter(user__id=agent_filter_id)
    else:
        monthly_qs = monthly_qs.filter(user=user)

    for mc in monthly_qs:
        commission_data.append({
            'agent': mc.user,
            'period': str(mc.period),
            'type': f"Monthly {mc.role.replace('_', ' ').title()} Commission",
            'commission_amount': mc.commission_amount,
            'status': mc.status,
            'paid_at': mc.paid_at,
            'ggr': mc.ngr # Use NGR as GGR equivalent
        })
    
    # Sort by date (descending) - tricky as period string might not sort well, but good enough
    commission_data.sort(key=lambda x: x['period'], reverse=True)

    # Calculate Summary
    total_paid = sum(c['commission_amount'] for c in commission_data if c['status'] == 'paid')
    outstanding = sum(c['commission_amount'] for c in commission_data if c['status'] == 'pending')
    total_ggr = sum(c['ggr'] for c in commission_data)
    
    payout_ratio = Decimal(0)
    total_comm = total_paid + outstanding
    if total_ggr > 0:
        payout_ratio = (total_comm / total_ggr * 100).quantize(Decimal('0.01'))

    context = {
        'report_title': 'Commission Report',
        'commission_data': commission_data,
        'all_users': all_users, 
        'current_user_filter': agent_filter_id,
        'start_date_filter': start_date.isoformat(),
        'end_date_filter': end_date.isoformat(),
        'summary': {
            'total_paid': total_paid,
            'outstanding': outstanding,
            'total_ggr': total_ggr,
            'payout_ratio': payout_ratio
        }
    }
    return render(request, 'betting/reports/commission_report.html', context)


@login_required
@user_passes_test(is_admin)
def admin_commission_financial_report(request):
    """
    Comprehensive financial report for Admin to track all commissions.
    """
    # Filter params
    period_type = request.GET.get('period_type', 'all')
    status = request.GET.get('status', 'all')
    start_date_str = request.GET.get('start_date')
    end_date_str = request.GET.get('end_date')
    user_search = request.GET.get('user_search', '')

    end_date = date.fromisoformat(end_date_str) if end_date_str else timezone.now().date()
    start_date = date.fromisoformat(start_date_str) if start_date_str else end_date - timedelta(days=30)

    weekly_qs = WeeklyAgentCommission.objects.filter(
        period__end_date__gte=start_date,
        period__end_date__lte=end_date
    ).select_related('agent', 'period')
    
    monthly_qs = MonthlyNetworkCommission.objects.filter(
        period__end_date__gte=start_date,
        period__end_date__lte=end_date
    ).select_related('user', 'period')

    # Apply filters
    if status != 'all':
        weekly_qs = weekly_qs.filter(status=status)
        monthly_qs = monthly_qs.filter(status=status)
    if user_search:
        weekly_qs = weekly_qs.filter(
            Q(agent__email__icontains=user_search) | 
            Q(agent__first_name__icontains=user_search) | 
            Q(agent__last_name__icontains=user_search)
        )
        monthly_qs = monthly_qs.filter(
            Q(user__email__icontains=user_search) | 
            Q(user__first_name__icontains=user_search) | 
            Q(user__last_name__icontains=user_search)
        )

    # Aggregates
    weekly_stats = weekly_qs.aggregate(
        total_paid=Sum('commission_total_amount', filter=Q(status='paid')),
        total_pending=Sum('commission_total_amount', filter=Q(status='pending')),
        total_ggr=Sum('ggr'),
        total_stake=Sum('total_stake')
    )
    
    monthly_stats = monthly_qs.aggregate(
        total_paid=Sum('commission_amount', filter=Q(status='paid')),
        total_pending=Sum('commission_amount', filter=Q(status='pending')),
        total_ngr=Sum('ngr')
    )
    
    def get_val(val): return val or Decimal('0.00')
    
    summary = {
        'total_weekly_paid': get_val(weekly_stats['total_paid']),
        'total_weekly_pending': get_val(weekly_stats['total_pending']),
        'total_weekly_ggr': get_val(weekly_stats['total_ggr']),
        'total_weekly_stake': get_val(weekly_stats['total_stake']),
        
        'total_monthly_paid': get_val(monthly_stats['total_paid']),
        'total_monthly_pending': get_val(monthly_stats['total_pending']),
        'total_monthly_ngr': get_val(monthly_stats['total_ngr']),
        
        'grand_total_paid': get_val(weekly_stats['total_paid']) + get_val(monthly_stats['total_paid']),
        'grand_total_pending': get_val(weekly_stats['total_pending']) + get_val(monthly_stats['total_pending']),
    }

    # Prepare list for table
    commission_list = []
    if period_type in ['all', 'weekly']:
        for item in weekly_qs:
            commission_list.append({
                'type': 'Weekly (Agent)',
                'user': item.agent,
                'period': item.period,
                'amount': item.commission_total_amount,
                'status': item.status,
                'basis_amount': item.ggr, # Show GGR as basis
                'created_at': item.created_at
            })
            
    if period_type in ['all', 'monthly']:
        for item in monthly_qs:
            commission_list.append({
                'type': 'Monthly (Network)',
                'user': item.user,
                'period': item.period,
                'amount': item.commission_amount,
                'status': item.status,
                'basis_amount': item.ngr, # Show NGR as basis
                'created_at': item.created_at
            })
            
    # Sort by period start date desc
    commission_list.sort(key=lambda x: x['period'].start_date, reverse=True)
    
    # Pagination
    paginator = Paginator(commission_list, 20)
    page_number = request.GET.get('page')
    try:
        page_obj = paginator.page(page_number)
    except PageNotAnInteger:
        page_obj = paginator.page(1)
    except EmptyPage:
        page_obj = paginator.page(paginator.num_pages)

    context = {
        'summary': summary,
        'commissions': page_obj,
        'filter_params': request.GET,
        'start_date_filter': start_date.isoformat(),
        'end_date_filter': end_date.isoformat(),
    }
    return render(request, 'betting/reports/admin_commission_financial_report.html', context)


# --- Admin Dashboard & Management Views ---

@login_required
@user_passes_test(lambda u: u.is_superuser or u.user_type == 'admin')
def admin_dashboard(request):
    total_users = User.objects.count()
    total_players = User.objects.filter(user_type='player').count()
    total_cashiers = User.objects.filter(user_type='cashier').count()
    total_agents = User.objects.filter(user_type='agent').count()
    total_super_agents = User.objects.filter(user_type='super_agent').count()
    total_master_agents = User.objects.filter(user_type='master_agent').count()
    total_admins = User.objects.filter(user_type='admin').count()

    total_bets_placed = BetTicket.objects.count()
    total_stake_amount = BetTicket.objects.aggregate(Sum('stake_amount'))['stake_amount__sum'] or Decimal('0.00')
    total_potential_winning = BetTicket.objects.filter(status='won').aggregate(Sum('potential_winning'))['potential_winning__sum'] or Decimal('0.00')
    
    total_deposits = Transaction.objects.filter(transaction_type='deposit', is_successful=True).aggregate(Sum('amount'))['amount__sum'] or Decimal('0.00')
    total_withdrawals = UserWithdrawal.objects.filter(status='approved').aggregate(Sum('amount'))['amount__sum'] or Decimal('0.00')

    pending_bets = BetTicket.objects.filter(status='pending').count()
    won_bets = BetTicket.objects.filter(status='won').count()
    lost_bets = BetTicket.objects.filter(status='lost').count()
    deleted_bets = BetTicket.objects.filter(status='deleted').count() # Tickets marked as deleted/voided
    
    pending_registrations_count = PendingAgentRegistration.objects.filter(status='PENDING').count()

    try:
        global_limits = GlobalBettingSettings.load()
    except (OperationalError, ProgrammingError):
        global_limits = None
    active_agent_overrides = AgentBettingLimitOverride.objects.filter(is_active=True, custom_limits_enabled=True).count()
    today = timezone.localdate()
    rejected_tickets_today = BettingLimitAuditLog.objects.filter(action_type='TICKET_REJECTED', created_at__date=today).count()

    agents_with_custom_limits = (
        AgentBettingLimitOverride.objects
        .filter(is_active=True, custom_limits_enabled=True)
        .select_related('agent')
        .order_by('-updated_at')[:10]
    )

    top_exposure_agents = (
        BetTicket.objects
        .filter(status='pending', user__agent__isnull=False)
        .values('user__agent_id', 'user__agent__email', 'user__agent__username')
        .annotate(exposure=Coalesce(Sum('max_winning'), Value(0), output_field=DecimalField()))
        .order_by('-exposure')[:10]
    )

    platform_exposure_today = (
        BetTicket.objects
        .filter(placed_at__date=today, status__in=['pending', 'won'])
        .aggregate(total=Coalesce(Sum('max_winning'), Value(0), output_field=DecimalField()))['total']
    )

    platform_sales_today = (
        BetTicket.objects
        .filter(placed_at__date=today)
        .aggregate(total=Coalesce(Sum('stake_amount'), Value(0), output_field=DecimalField()))['total']
    )

    context = {
        'total_users': total_users,
        'total_players': total_players,
        'total_cashiers': total_cashiers,
        'total_agents': total_agents,
        'total_super_agents': total_super_agents,
        'total_master_agents': total_master_agents,
        'total_admins': total_admins,
        'total_bets_placed': total_bets_placed,
        'total_stake_amount': total_stake_amount,
        'total_potential_winning': total_potential_winning,
        'total_deposits': total_deposits,
        'total_withdrawals': total_withdrawals,
        'pending_bets': pending_bets,
        'won_bets': won_bets,
        'lost_bets': lost_bets,
        'deleted_bets': deleted_bets,
        'pending_registrations_count': pending_registrations_count,
        'global_limits': global_limits,
        'active_agent_overrides': active_agent_overrides,
        'rejected_tickets_today': rejected_tickets_today,
        'agents_with_custom_limits': agents_with_custom_limits,
        'top_exposure_agents': top_exposure_agents,
        'platform_exposure_today': platform_exposure_today,
        'platform_sales_today': platform_sales_today,
    }
    return render(request, 'betting/admin/dashboard.html', context)


@login_required
@user_passes_test(lambda u: u.is_superuser or u.user_type == 'admin')
def manage_users(request):
    user_type_filter = request.GET.get('user_type', 'all')
    search_query = request.GET.get('q', '')

    users_queryset = User.objects.all().order_by('email') # Default ordering

    if user_type_filter != 'all':
        users_queryset = users_queryset.filter(user_type=user_type_filter)
    
    if search_query:
        users_queryset = users_queryset.filter(
            Q(email__icontains=search_query) |
            Q(first_name__icontains=search_query) |
            Q(last_name__icontains=search_query) |
            Q(phone_number__icontains=search_query)
        )

    # Exclude the currently logged-in user from the list to prevent self-deletion issues
    users_queryset = users_queryset.exclude(pk=request.user.pk)

    paginator = Paginator(users_queryset, 10) # Show 10 users per page
    page_number = request.GET.get('page')

    try:
        users = paginator.page(page_number)
    except PageNotAnInteger:
        users = paginator.page(1)
    except EmptyPage:
        users = paginator.page(paginator.num_pages)

    context = {
        'users': users,
        'user_type_choices': User.USER_TYPE_CHOICES, # Assuming this is accessible from the User model
        'current_user_type_filter': user_type_filter,
        'current_search_query': search_query,
    }
    return render(request, 'betting/admin/manage_users.html', context)


@login_required
@user_passes_test(lambda u: u.is_superuser or u.user_type == 'admin')
def add_user(request):
    if request.method == 'POST':
        form = AdminUserCreationForm(request.POST, request=request) # Pass request to form
        if form.is_valid():
            user = form.save()
            Wallet.objects.create(user=user, balance=0) # Create a wallet for the new user
            messages.success(request, f"User {user.email} added successfully.")
            log_admin_activity(request, f"Added new user: {user.email} ({user.get_user_type_display()})")
            return redirect('betting_admin:manage_users')
        else:
            for field, errors in form.errors.items():
                for error in errors:
                    messages.error(request, f"{field.replace('_', ' ').title()}: {error}")
            if form.non_field_errors():
                for error in form.non_field_errors():
                    messages.error(request, f"Add User Error: {error}")
    else:
        form = AdminUserCreationForm(request=request) # Pass request to form for initial display
    return render(request, 'betting/admin/add_user.html', {'form': form})


@login_required
@user_passes_test(lambda u: u.is_superuser or u.user_type == 'admin')
def edit_user(request, user_id):
    user_to_edit = get_object_or_404(User, id=user_id)

    # Prevent editing superusers if logged-in user is not a superuser themselves
    if not request.user.is_superuser and user_to_edit.is_superuser:
        messages.error(request, "You do not have permission to edit superuser accounts.")
        return redirect('betting_admin:manage_users')
    
    # Prevent editing self if it would lead to permission issues
    if request.user.pk == user_to_edit.pk and not request.user.is_superuser:
        messages.warning(request, "You are trying to edit your own account. Use the 'Profile' page for personal changes, or ensure you have superuser privileges for advanced changes.")
        # Optionally redirect to profile page for self-edits, or proceed with warning
        # return redirect('betting:profile')

    if request.method == 'POST':
        form = AdminUserChangeForm(request.POST, instance=user_to_edit, request=request) # Pass request to form
        if form.is_valid():
            user = form.save()
            messages.success(request, f"User {user.email} updated successfully.")
            log_admin_activity(request, f"Edited user: {user.email} ({user.get_user_type_display()})")
            return redirect('betting_admin:manage_users')
        else:
            for field, errors in form.errors.items():
                for error in errors:
                    messages.error(request, f"{field.replace('_', ' ').title()}: {error}")
            if form.non_field_errors():
                for error in form.non_field_errors():
                    messages.error(request, f"Edit User Error: {error}")
    else:
        form = AdminUserChangeForm(instance=user_to_edit, request=request) # Pass request to form for initial display
    return render(request, 'betting/admin/edit_user.html', {'form': form, 'user_to_edit': user_to_edit})


@login_required
@user_passes_test(lambda u: u.is_superuser or u.user_type == 'admin')
@db_transaction.atomic
def delete_user(request, user_id):
    user_to_delete = get_object_or_404(User, id=user_id)

    if user_to_delete == request.user:
        messages.error(request, "You cannot delete your own account.")
        return redirect('betting_admin:manage_users')

    # Prevent deleting superusers if logged-in user is not a superuser themselves
    if user_to_delete.is_superuser and not request.user.is_superuser:
        messages.error(request, "You do not have permission to delete superuser accounts.")
        return redirect('betting_admin:manage_users')
    
    # Only allow admin to delete other admins (not superusers)
    if user_to_delete.user_type == 'admin' and not request.user.is_superuser:
        messages.error(request, "Only a superuser can delete other admin accounts.")
        return redirect('betting_admin:manage_users')

    user_email = user_to_delete.email # Capture email before deletion
    
    if request.method == 'POST':
        try:
            # Transfer wallet balance of deleted user to admin's wallet (or specific system account)
            # Find or create an an admin wallet for receiving funds
            admin_user_wallet = Wallet.objects.get_or_create(user=request.user)[0]
            deleted_user_wallet = Wallet.objects.get(user=user_to_delete)

            if deleted_user_wallet.balance > Decimal('0.00'):
                admin_user_wallet.balance += deleted_user_wallet.balance
                admin_user_wallet.save()

                # Record transaction for the transfer
                Transaction.objects.create(
                    user=user_to_delete,
                    initiating_user=request.user,
                    target_user=request.user,
                    transaction_type='wallet_balance_transfer_on_deletion',
                    amount=deleted_user_wallet.balance,
                    is_successful=True,
                    status='completed',
                    description=f"Wallet balance of {user_email} transferred to admin upon deletion.",
                    timestamp=timezone.now()
                )
                messages.info(request, f"Wallet balance of {user_email} (₦{deleted_user_wallet.balance:.2f}) transferred to your wallet.")


            # Mark related bet tickets as 'deleted' (void) and refund their stake
            # This handles cases where user is deleted but their bets are still active
            related_bet_tickets = BetTicket.objects.filter(user=user_to_delete).exclude(status__in=['deleted', 'won', 'lost', 'cashed_out', 'cancelled'])
            for ticket in related_bet_tickets:
                ticket.status = 'deleted'
                ticket.deleted_by = request.user
                ticket.deleted_at = timezone.now()
                ticket.save()
                
                user_wallet = Wallet.objects.select_for_update().get(user=ticket.user)
                user_wallet.balance += ticket.stake_amount
                user_wallet.save()

                Transaction.objects.create(
                    user=ticket.user,
                    initiating_user=request.user,
                    target_user=request.user,
                    transaction_type='fixture_deletion_refund',
                    amount=ticket.stake_amount,
                    is_successful=True,
                    status='completed',
                    description=f"Refund for stake on ticket {ticket.id} due to fixture deletion: {fixture_name}",
                    related_bet_ticket=ticket,
                    timestamp=timezone.now()
                )
                log_admin_activity(request, f"Refunded ticket {ticket.id} due to deletion of fixture {fixture_name}.")

            user_to_delete.delete()
            messages.success(request, f"User {user_email} and associated data (wallet, transactions, bets) deleted successfully.")
            log_admin_activity(request, f"Deleted user: {user_email}")
            return redirect('betting_admin:manage_users')
        except Exception as e:
            messages.error(request, f"An error occurred while deleting user {user_email}: {e}")
            log_admin_activity(request, f"Failed to delete user: {user_email} - Error: {e}")
            return redirect('betting_admin:manage_users')
    
    messages.error(request, "Invalid request for user deletion.")
    return redirect('betting_admin:manage_users')


@login_required
@user_passes_test(lambda u: u.is_superuser or u.user_type == 'admin')
def manage_fixtures(request):
    status_filter = request.GET.get('status', 'all')
    period_filter = request.GET.get('period', 'all')
    search_query = request.GET.get('q', '')

    fixtures_queryset = Fixture.objects.all()

    if status_filter != 'all':
        fixtures_queryset = fixtures_queryset.filter(status=status_filter)
    
    if period_filter != 'all':
        try:
            period_id = int(period_filter)
            fixtures_queryset = fixtures_queryset.filter(betting_period__id=period_id)
        except (ValueError, TypeError):
            pass # Invalid period ID

    if search_query:
        fixtures_queryset = fixtures_queryset.filter(
            Q(home_team__icontains=search_query) |
            Q(away_team__icontains=search_query)
        )

    paginator = Paginator(fixtures_queryset.order_by('-match_time'), 10) # 10 fixtures per page
    page_number = request.GET.get('page')

    try:
        fixtures = paginator.page(page_number)
    except PageNotAnInteger:
        fixtures = paginator.page(1)
    except EmptyPage:
        fixtures = paginator.page(paginator.num_pages)

    context = {
        'fixtures': fixtures,
        'fixture_status_choices': [('all', 'All')] + list(Fixture.STATUS_CHOICES),
        'current_status_filter': status_filter,
        'all_betting_periods': BettingPeriod.objects.all().order_by('-start_date'),
        'current_period_filter': period_filter,
        'current_search_query': search_query,
    }
    return render(request, 'betting/admin/manage_fixtures.html', context)


@login_required
@user_passes_test(lambda u: u.is_superuser or u.user_type == 'admin')
def add_fixture(request):
    if request.method == 'POST':
        form = FixtureForm(request.POST)
        if form.is_valid():
            fixture = form.save(commit=False)
            # Assuming 'created_by' field exists on Fixture model. If not, remove this line.
            # fixture.created_by = request.user 
            fixture.save()
            messages.success(request, f"Fixture {fixture.home_team} vs {fixture.away_team} added successfully.")
            log_admin_activity(request, f"Added new fixture: {fixture.home_team} vs {fixture.away_team}")
            return redirect('betting_admin:manage_fixtures')
        else:
            for field, errors in form.errors.items():
                for error in errors:
                    messages.error(request, f"{field.replace('_', ' ').title()}: {error}")
            if form.non_field_errors():
                for error in form.non_field_errors():
                    messages.error(request, f"Add Fixture Error: {error}")
    else:
        form = FixtureForm()
    return render(request, 'betting/admin/add_fixture.html', {'form': form})


@login_required
@user_passes_test(lambda u: u.is_superuser or u.user_type == 'admin')
def edit_fixture(request, fixture_id):
    fixture = get_object_or_404(Fixture, id=fixture_id)
    if request.method == 'POST':
        form = FixtureForm(request.POST, instance=fixture)
        if form.is_valid():
            fixture = form.save()
            messages.success(request, f"Fixture {fixture.home_team} vs {fixture.away_team} updated successfully.")
            log_admin_activity(request, f"Edited fixture: {fixture.home_team} vs {fixture.away_team}")
            return redirect('betting_admin:manage_fixtures')
        else:
            for field, errors in form.errors.items():
                for error in errors:
                    messages.error(request, f"{field.replace('_', ' ').title()}: {error}")
            if form.non_field_errors():
                for error in form.non_field_errors():
                    messages.error(request, f"Edit Fixture Error: {error}")
    else:
        form = FixtureForm(instance=fixture)
    return render(request, 'betting/admin/edit_fixture.html', {'form': form, 'fixture': fixture})


@login_required
@user_passes_test(lambda u: u.is_superuser or u.user_type == 'admin')
@db_transaction.atomic
def delete_fixture(request, fixture_id):
    fixture = get_object_or_404(Fixture, id=fixture_id)
    fixture_name = f"{fixture.home_team} vs {fixture.away_team}"

    if fixture.status != 'pending': # Assuming 'pending' means it's still open for betting. If your model uses 'open', replace 'pending'.
        messages.error(request, f"Fixture '{fixture_name}' cannot be deleted as its status is '{fixture.status}'. Only 'pending' fixtures can be deleted.")
        return redirect('betting_admin:manage_fixtures')

    if request.method == 'POST':
        try:
            # Mark associated pending bets as deleted and refund stake
            pending_bets = BetTicket.objects.filter(
                selections__fixture=fixture, # Selects tickets that have this fixture
                status='pending'
            ).distinct() # Use distinct to avoid duplicates if a ticket has multiple selections

            for ticket in pending_bets:
                ticket.status = 'deleted'
                ticket.deleted_by = request.user
                ticket.deleted_at = timezone.now()
                ticket.save()

                user_wallet = Wallet.objects.select_for_update().get(user=ticket.user)
                user_wallet.balance += ticket.stake_amount
                user_wallet.save()

                Transaction.objects.create(
                    user=ticket.user,
                    initiating_user=request.user,
                    target_user=request.user,
                    transaction_type='fixture_deletion_refund',
                    amount=ticket.stake_amount,
                    is_successful=True,
                    status='completed',
                    description=f"Refund for stake on ticket {ticket.id} due to fixture deletion: {fixture_name}",
                    related_bet_ticket=ticket,
                    timestamp=timezone.now()
                )
                log_admin_activity(request, f"Refunded ticket {ticket.id} due to deletion of fixture {fixture_name}.")

            fixture.delete()
            messages.success(request, f"Fixture '{fixture_name}' and associated pending bets (refunded) deleted successfully.")
            log_admin_activity(request, f"Deleted fixture: {fixture_name}")
            return redirect('betting_admin:manage_fixtures')
        except Exception as e:
            messages.error(request, f"An error occurred while deleting fixture '{fixture_name}': {e}")
            log_admin_activity(request, f"Failed to delete fixture: {fixture_name} - Error: {e}")
            return redirect('betting_admin:manage_fixtures')
    
    messages.error(request, "Invalid request for fixture deletion.")
    return redirect('betting_admin:manage_fixtures')


@login_required
@user_passes_test(lambda u: u.is_superuser or u.user_type == 'admin')
@db_transaction.atomic
def declare_result(request, fixture_id):
    fixture = get_object_or_404(Fixture, id=fixture_id)

    if fixture.status != 'pending': # Assuming fixtures are 'pending' before result declaration
        messages.warning(request, f"Fixture is already '{fixture.status}'. Result cannot be declared again.")
        return redirect('betting_admin:manage_fixtures')

    if request.method == 'POST':
        form = DeclareResultForm(request.POST, instance=fixture)
        if form.is_valid():
            # Get the result from the form.
            # IMPORTANT: Your Fixture model has a 'result' field with choices like 'home_win', 'draw', etc.
            # Your form's 'result' field correctly points to this.
            # The 'winning_outcome' variable in the previous code snippet for fixture was not a model field.
            # So, we should update the fixture.result directly.
            fixture.home_score = form.cleaned_data['home_score']
            fixture.away_score = form.cleaned_data['away_score']
            fixture.result = form.cleaned_data['result'] # Use the form's cleaned data for result
            fixture.status = form.cleaned_data['status'] 
            fixture.save()

            # Process all bet tickets related to this fixture
            # Use select_related/prefetch_related if fetching many to reduce queries
            bets_on_this_fixture = BetTicket.objects.filter(
                selections__fixture=fixture 
            ).distinct().select_for_update() 

            for ticket in bets_on_this_fixture:
                # Find the specific selection for *this* fixture within *this* ticket
                selection_for_this_fixture = ticket.selections.filter(fixture=fixture).first()

                if not selection_for_this_fixture:
                    continue # Should not happen if query above is correct

                if ticket.status == 'pending': 
                    # Determine if the selection on this ticket for this fixture wins
                    is_selection_winning = False
                    # The following logic should mirror how results are actually determined
                    # based on fixture.home_score, fixture.away_score and fixture.result (which is now correctly set)

                    # Simplified: if the selection matches the declared fixture result, it's winning for that selection
                    if selection_for_this_fixture.bet_type == fixture.result:
                        is_selection_winning = True
                    # Handle DNB cases where a draw voids the selection
                    elif selection_for_this_fixture.bet_type == 'home_dnb' and fixture.result == 'draw':
                        is_selection_winning = None # Voided
                    elif selection_for_this_fixture.bet_type == 'away_dnb' and fixture.result == 'draw':
                        is_selection_winning = None # Voided
                    # Add more complex logic for Over/Under, BTTS if not covered by direct result match
                    # (Your Fixture clean method already sets fixture.result based on scores, so this simplifies things)
                    
                    selection_for_this_fixture.is_winning_selection = is_selection_winning
                    selection_for_this_fixture.save()

                    # Re-evaluate entire ticket status after updating this selection
                    # This logic should ideally be in a BetTicket method
                    all_selections_evaluated = True
                    ticket_still_winning = True
                    ticket_is_cancelled = False

                    for sel in ticket.selections.all():
                        if sel.is_winning_selection is None: # Found a voided selection
                            ticket_is_cancelled = True
                            break
                        if sel.is_winning_selection == False: # Found a losing selection
                            ticket_still_winning = False
                        
                        # Check if fixture related to this selection is settled/cancelled
                        if sel.fixture.status not in ['settled', 'cancelled']:
                            all_selections_evaluated = False
                            break # Not all fixtures on the ticket are settled yet

                    if all_selections_evaluated:
                        if ticket_is_cancelled:
                            ticket.status = 'cancelled'
                            # Refund stake for cancelled tickets
                            user_wallet = Wallet.objects.select_for_update().get(user=ticket.user)
                            user_wallet.balance += ticket.stake_amount
                            user_wallet.save()
                            Transaction.objects.create(
                                user=ticket.user,
                                initiating_user=request.user, 
                                target_user=ticket.user,
                                transaction_type='ticket_cancellation_refund',
                                amount=ticket.stake_amount,
                                is_successful=True,
                                status='completed',
                                description=f"Refund for cancelled bet ticket {ticket.id} (due to voided selection in fixture {fixture.home_team} vs {fixture.away_team})",
                                related_bet_ticket=ticket,
                                timestamp=timezone.now()
                            )
                            messages.info(request, f"Ticket {ticket.id} is CANCELLED (stake refunded) due to fixture {fixture.home_team} vs {fixture.away_team} resulting in a void.")
                            log_admin_activity(request, f"Ticket {ticket.id} CANCELLED and refunded due to fixture {fixture.id} void result.")
                        elif ticket_still_winning:
                            ticket.status = 'won'
                            ticket.save()

                            user_wallet = Wallet.objects.select_for_update().get(user=ticket.user)
                            user_wallet.balance += ticket.max_winning
                            user_wallet.save()

                            Transaction.objects.create(
                                user=ticket.user,
                                initiating_user=request.user, 
                                target_user=ticket.user,
                                transaction_type='bet_payout',
                                amount=ticket.max_winning,
                                is_successful=True,
                                status='completed',
                                description=f"Winnings for bet ticket {ticket.id} on {fixture.home_team} vs {fixture.away_team}",
                                related_bet_ticket=ticket,
                                timestamp=timezone.now()
                            )
                            messages.success(request, f"Ticket {ticket.id} is WON! Winnings of ₦{ticket.max_winning:.2f} paid to {ticket.user.email}.")
                            log_admin_activity(request, f"Declared fixture {fixture.id} as WON for ticket {ticket.id} and paid out.")
                        else:
                            ticket.status = 'lost'
                            ticket.save()
                            messages.info(request, f"Ticket {ticket.id} is LOST.")
                            log_admin_activity(request, f"Declared fixture {fixture.id} as LOST for ticket {ticket.id}.")
                    
                    ticket.save() # Save the ticket status update

                elif ticket.status == 'deleted':
                    messages.info(request, f"Ticket {ticket.id} was previously deleted and will not be processed for result.")
                # No need for else (already processed) as per previous code, as it would be handled by the update logic above.


            messages.success(request, f"Result declared for {fixture.home_team} vs {fixture.away_team} as '{fixture.get_result_display()}'. All associated tickets processed.")
            log_admin_activity(request, f"Declared result for fixture {fixture.id} ({fixture.home_team} vs {fixture.away_team}) as {fixture.result}.")
            return redirect('betting_admin:manage_fixtures')
        else:
            for field, errors in form.errors.items():
                for error in errors:
                    messages.error(request, f"{field.replace('_', ' ').title()}: {error}")
            if form.non_field_errors():
                for error in form.non_field_errors():
                    messages.error(request, f"Declare Result Error: {error}")
    else:
        form = DeclareResultForm(instance=fixture)
    
    context = {
        'form': form,
        'fixture': fixture,
    }
    return render(request, 'betting/admin/declare_result.html', context)


@login_required
@user_passes_test(lambda u: u.is_superuser or u.user_type == 'admin')
def withdraw_request_list(request):
    status_filter = request.GET.get('status', 'all')
    search_query = request.GET.get('q', '')

    withdrawals_queryset = UserWithdrawal.objects.all().order_by('-request_time')

    if status_filter != 'all':
        withdrawals_queryset = withdrawals_queryset.filter(status=status_filter)
    
    if search_query:
        withdrawals_queryset = withdrawals_queryset.filter(
            Q(user__email__icontains=search_query) |
            Q(bank_name__icontains=search_query) |
            Q(account_number__icontains=search_query) |
            Q(id__startswith=search_query)
        )

    paginator = Paginator(withdrawals_queryset, 10) # 10 requests per page
    page_number = request.GET.get('page')

    try:
        withdrawals = paginator.page(page_number)
    except PageNotAnInteger:
        withdrawals = paginator.page(1)
    except EmptyPage:
        withdrawals = paginator.page(paginator.num_pages)

    context = {
        'withdrawals': withdrawals,
        'status_choices': [('all', 'All')] + list(UserWithdrawal.STATUS_CHOICES),
        'current_status_filter': status_filter,
        'current_search_query': search_query,
        'withdrawal_action_form': WithdrawalActionForm() # Form for approve/reject
    }
    return render(request, 'betting/admin/withdraw_request_list.html', context)


@login_required
@user_passes_test(lambda u: u.is_superuser or u.user_type == 'admin')
@db_transaction.atomic
def approve_reject_withdrawal(request, withdrawal_id):
    withdrawal_request = get_object_or_404(UserWithdrawal, id=withdrawal_id)

    if withdrawal_request.status != 'pending':
        messages.warning(request, f"This withdrawal request has already been {withdrawal_request.status}.")
        return redirect('betting_admin:withdraw_request_list')

    if request.method == 'POST':
        form = WithdrawalActionForm(request.POST)
        if form.is_valid():
            action = form.cleaned_data['action']
            reason = form.cleaned_data['reason']

            if action == 'approve':
                withdrawal_request.status = 'approved'
                
                # Capture Balance Snapshot for Audit
                try:
                    user_wallet = Wallet.objects.get(user=withdrawal_request.user)
                    # Since funds are deducted at request time (pending), current balance is the 'after' state relative to the request
                    withdrawal_request.balance_after = user_wallet.balance 
                    withdrawal_request.balance_before = user_wallet.balance + withdrawal_request.amount
                except Wallet.DoesNotExist:
                    pass

                withdrawal_request.processed_ip = request.META.get('REMOTE_ADDR')
                
                messages.success(request, f"Withdrawal request {withdrawal_id} approved. Funds should be disbursed.")
                log_admin_activity(request, f"Approved withdrawal request {withdrawal_id} for user {withdrawal_request.user.email}")
            elif action == 'reject':
                withdrawal_request.status = 'rejected'
                withdrawal_request._skip_signal_refund = True # Skip signal processing since we handle it manually here
                # Refund funds to user's wallet
                user_wallet = get_object_or_404(Wallet.objects.select_for_update(), user=withdrawal_request.user)
                user_wallet.balance += withdrawal_request.amount
                user_wallet.save()

                # Record the refund transaction
                Transaction.objects.create(
                    user=withdrawal_request.user,
                    initiating_user=request.user, 
                    target_user=withdrawal_request.user,
                    transaction_type='withdrawal_refund',
                    amount=withdrawal_request.amount,
                    is_successful=True,
                    status='completed',
                    description=f"Refund for rejected withdrawal request {withdrawal_id}. Reason: {reason or 'No reason provided.'}",
                    timestamp=timezone.now()
                )
                messages.info(request, f"Withdrawal request {withdrawal_id} rejected. Funds (₦{withdrawal_request.amount:.2f}) returned to {withdrawal_request.user.email}'s wallet. Reason: {reason or 'No reason provided.'}")
                log_admin_activity(request, f"Rejected withdrawal request {withdrawal_id} for user {withdrawal_request.user.email}. Reason: {reason}")
            
            withdrawal_request.approved_rejected_by = request.user # Corrected field name
            withdrawal_request.approved_rejected_time = timezone.now() # Corrected field name
            withdrawal_request.admin_notes = reason # Corrected field name
            withdrawal_request.save()

            return redirect('betting_admin:withdraw_request_list')
        else:
            for field, errors in form.errors.items():
                for error in errors:
                    messages.error(request, f"{field.replace('_', ' ').title()}: {error}")
            if form.non_field_errors():
                for error in form.non_field_errors():
                    messages.error(request, f"Withdrawal Action Error: {error}")
    
    messages.error(request, "Invalid request for withdrawal action.")
    return redirect('betting_admin:withdraw_request_list')


@login_required
@user_passes_test(lambda u: u.is_superuser or u.user_type == 'admin')
def manage_betting_periods(request):
    periods = BettingPeriod.objects.all().order_by('-start_date')
    paginator = Paginator(periods, 10) 
    page_number = request.GET.get('page')

    try:
        betting_periods = paginator.page(page_number)
    except PageNotAnInteger:
        betting_periods = paginator.page(1)
    except EmptyPage:
        betting_periods = paginator.page(paginator.num_pages)

    context = {
        'betting_periods': betting_periods
    }
    return render(request, 'betting/admin/manage_betting_periods.html', context)


@login_required
@user_passes_test(lambda u: u.is_superuser or u.user_type == 'admin')
def add_betting_period(request):
    if request.method == 'POST':
        form = BettingPeriodForm(request.POST)
        if form.is_valid():
            period = form.save()
            messages.success(request, f"Betting period '{period.name}' added successfully.")
            log_admin_activity(request, f"Added new betting period: {period.name}")
            return redirect('betting_admin:manage_betting_periods')
        else:
            for field, errors in form.errors.items():
                for error in errors:
                    messages.error(request, f"{field.replace('_', ' ').title()}: {error}")
            if form.non_field_errors():
                for error in form.non_field_errors():
                    messages.error(request, f"Add Betting Period Error: {error}")
    else:
        form = BettingPeriodForm()
    return render(request, 'betting/admin/add_betting_period.html', {'form': form})


@login_required
@user_passes_test(lambda u: u.is_superuser or u.user_type == 'admin')
def edit_betting_period(request, period_id):
    period = get_object_or_404(BettingPeriod, id=period_id)
    if request.method == 'POST':
        form = BettingPeriodForm(request.POST, instance=period)
        if form.is_valid():
            period = form.save()
            messages.success(request, f"Betting period '{period.name}' updated successfully.")
            log_admin_activity(request, f"Edited betting period: {period.name}")
            return redirect('betting_admin:manage_betting_periods')
        else:
            for field, errors in form.errors.items():
                for error in errors:
                    messages.error(request, f"{field.replace('_', ' ').title()}: {error}")
            if form.non_field_errors():
                for error in form.non_field_errors():
                    messages.error(request, f"Edit Betting Period Error: {error}")
    else:
        form = BettingPeriodForm(instance=period)
    return render(request, 'betting/admin/edit_betting_period.html', {'form': form, 'period': period})


@login_required
@user_passes_test(lambda u: u.is_superuser or u.user_type == 'admin')
@db_transaction.atomic
def delete_betting_period(request, period_id):
    period = get_object_or_404(BettingPeriod, id=period_id)
    period_name = period.name

    if Fixture.objects.filter(betting_period=period).exclude(status__in=['settled', 'cancelled', 'deleted']).exists(): # Only allow deletion if no unsettled or active fixtures
        messages.error(request, f"Cannot delete betting period '{period_name}'. There are active or unsettled fixtures associated with it.")
        return redirect('betting_admin:manage_betting_periods')
    
    if request.method == 'POST':
        try:
            period.delete()
            messages.success(request, f"Betting period '{period_name}' deleted successfully.")
            log_admin_activity(request, f"Deleted betting period: {period_name}")
            return redirect('betting_admin:manage_betting_periods')
        except Exception as e:
            messages.error(request, f"An error occurred while deleting betting period '{period_name}': {e}")
            log_admin_activity(request, f"Failed to delete betting period: {period_name} - Error: {e}")
            return redirect('betting_admin:manage_betting_periods')

    messages.error(request, "Invalid request for betting period deletion.")
    return redirect('betting_admin:manage_betting_periods')


@login_required
@user_passes_test(lambda u: u.is_superuser or u.user_type == 'admin')
def manage_agent_payouts(request):
    status_filter = request.GET.get('status', 'all')
    agent_filter = request.GET.get('agent', 'all')
    start_date_str = request.GET.get('start_date')
    end_date_str = request.GET.get('end_date')

    payouts_queryset = AgentPayout.objects.all()

    if status_filter != 'all':
        payouts_queryset = payouts_queryset.filter(status=status_filter)
    
    if agent_filter != 'all':
        try:
            agent_id = int(agent_filter)
            payouts_queryset = payouts_queryset.filter(agent__id=agent_id)
        except (ValueError, TypeError):
            pass

    # Corrected filter for payout_date (model has 'created_at' and 'settled_at' but not 'payout_date')
    # Assuming filtering should be by 'created_at' for the payout request creation date
    if start_date_str:
        try:
            start_date = date.fromisoformat(start_date_str)
            payouts_queryset = payouts_queryset.filter(created_at__date__gte=start_date)
        except ValueError:
            messages.error(request, "Invalid start date format.")
    if end_date_str:
        try:
            end_date = date.fromisoformat(end_date_str)
            payouts_queryset = payouts_queryset.filter(created_at__date__lte=end_date)
        except ValueError:
            messages.error(request, "Invalid end date format.")

    paginator = Paginator(payouts_queryset.order_by('-created_at'), 10) # Order by created_at
    page_number = request.GET.get('page')

    try:
        agent_payouts = paginator.page(page_number)
    except PageNotAnInteger:
        agent_payouts = paginator.page(1)
   
    except EmptyPage:
        agent_payouts = paginator.page(paginator.num_pages)

    context = {
        'agent_payouts': agent_payouts,
        'status_choices': [('all', 'All')] + list(AgentPayout.STATUS_CHOICES),
        'current_status_filter': status_filter,
        'all_agents': User.objects.filter(user_type__in=['agent', 'super_agent', 'master_agent']).order_by('email'),
        'current_agent_filter': agent_filter,
        'start_date_filter': start_date_str,
        'end_date_filter': end_date_str,
    }
    return render(request, 'betting/admin/manage_agent_payouts.html', context)


@login_required
@user_passes_test(lambda u: u.is_superuser or u.user_type == 'admin')
@db_transaction.atomic
def mark_payout_settled(request, payout_id):
    payout = get_object_or_404(AgentPayout, id=payout_id)

    if payout.status != 'pending':
        messages.warning(request, f"Payout {payout.id} is already '{payout.status}'.")
        return redirect('betting_admin:manage_agent_payouts')

    if request.method == 'POST':
        payout.status = 'settled'
        payout.settled_by = request.user # Corrected field name
        payout.settled_at = timezone.now() # Corrected field name
        payout.save()
        messages.success(request, f"Agent payout {payout.id} marked as settled.")
        log_admin_activity(request, f"Marked agent payout {payout.id} for {payout.agent.email} as settled.")
        return redirect('betting_admin:manage_agent_payouts')
    
    messages.error(request, "Invalid request for marking payout settled.")
    return redirect('betting_admin:manage_agent_payouts')


@login_required
@user_passes_test(lambda u: u.is_superuser or u.user_type == 'admin')
def admin_ticket_report(request):
    status_filter = request.GET.get('status', 'all')
    user_filter = request.GET.get('user', 'all')
    start_date_str = request.GET.get('start_date')
    end_date_str = request.GET.get('end_date')

    bet_tickets_queryset = BetTicket.objects.all()

    if status_filter != 'all':
        bet_tickets_queryset = bet_tickets_queryset.filter(status=status_filter)
    
    if user_filter != 'all':
        try:
            user_id = int(user_filter)
            bet_tickets_queryset = bet_tickets_queryset.filter(user__id=user_id)
        except (ValueError, TypeError):
            pass

    if start_date_str:
        try:
            start_date = date.fromisoformat(start_date_str)
            bet_tickets_queryset = bet_tickets_queryset.filter(placed_at__date__gte=start_date)
        except ValueError:
            messages.error(request, "Invalid start date format.")
    if end_date_str:
        try:
            end_date = date.fromisoformat(end_date_str)
            bet_tickets_queryset = bet_tickets_queryset.filter(placed_at__date__lte=end_date)
        except ValueError:
            messages.error(request, "Invalid end date format.")
    
    paginator = Paginator(bet_tickets_queryset.order_by('-placed_at'), 10)
    page_number = request.GET.get('page')

    try:
        bet_tickets = paginator.page(page_number)
    except PageNotAnInteger:
        bet_tickets = paginator.page(1)
    except EmptyPage:
        bet_tickets = paginator.page(paginator.num_pages)

    context = {
        'bet_tickets': bet_tickets,
        'status_choices': [('all', 'All')] + list(BetTicket.STATUS_CHOICES),
        'current_status_filter': status_filter,
        'all_users': User.objects.all().order_by('email'), # For user filter dropdown
        'current_user_filter': user_filter,
        'start_date_filter': start_date_str,
        'end_date_filter': end_date_str,
    }
    return render(request, 'betting/admin/ticket_report.html', context)

@login_required
@user_passes_test(lambda u: u.is_superuser or u.user_type == 'admin')
def admin_limit_rejections_report(request):
    agent_query = (request.GET.get('agent') or '').strip()
    user_query = (request.GET.get('user') or '').strip()
    start_date_str = request.GET.get('start_date')
    end_date_str = request.GET.get('end_date')

    qs = (
        BettingLimitAuditLog.objects
        .filter(action_type='TICKET_REJECTED')
        .select_related('actor', 'agent', 'affected_user', 'ticket')
        .order_by('-created_at')
    )

    if agent_query:
        qs = qs.filter(
            Q(agent__email__icontains=agent_query)
            | Q(agent__username__icontains=agent_query)
            | Q(agent__first_name__icontains=agent_query)
            | Q(agent__last_name__icontains=agent_query)
            | Q(agent__phone_number__icontains=agent_query)
        )

    if user_query:
        qs = qs.filter(
            Q(affected_user__email__icontains=user_query)
            | Q(affected_user__username__icontains=user_query)
            | Q(affected_user__first_name__icontains=user_query)
            | Q(affected_user__last_name__icontains=user_query)
            | Q(affected_user__phone_number__icontains=user_query)
        )

    if start_date_str:
        try:
            start_date = date.fromisoformat(start_date_str)
            qs = qs.filter(created_at__date__gte=start_date)
        except ValueError:
            messages.error(request, "Invalid start date format.")

    if end_date_str:
        try:
            end_date = date.fromisoformat(end_date_str)
            qs = qs.filter(created_at__date__lte=end_date)
        except ValueError:
            messages.error(request, "Invalid end date format.")

    total_rejections = qs.count()

    paginator = Paginator(qs, 50)
    page_number = request.GET.get('page')
    try:
        logs = paginator.page(page_number)
    except PageNotAnInteger:
        logs = paginator.page(1)
    except EmptyPage:
        logs = paginator.page(paginator.num_pages)

    context = {
        'report_title': "Tickets Rejected Due To Limits",
        'logs': logs,
        'total_rejections': total_rejections,
        'agent_filter': agent_query,
        'user_filter': user_query,
        'start_date_filter': start_date_str,
        'end_date_filter': end_date_str,
    }
    return render(request, 'betting/admin/betting_limit_rejections_report.html', context)


@login_required
@user_passes_test(lambda u: u.is_superuser or u.user_type == 'admin')
def admin_ticket_details(request, ticket_id):
    ticket = get_object_or_404(BetTicket, id=ticket_id)
    return render(request, 'betting/admin/ticket_detail.html', {'bet_ticket': ticket})


@login_required
@user_passes_test(lambda u: u.is_superuser or u.user_type == 'admin')
@db_transaction.atomic
def admin_void_ticket_single(request, ticket_id):
    ticket = get_object_or_404(BetTicket, id=ticket_id)

    if request.method == 'POST':
        if ticket.status in ['won', 'lost', 'cashed_out', 'deleted', 'cancelled']:
            messages.warning(request, f"Ticket {ticket.ticket_id} is already '{ticket.status}' and cannot be voided/deleted.")
            return redirect('betting_admin:admin_ticket_report') 
        
        try:
            ticket.status = 'deleted'
            ticket.deleted_by = request.user
            ticket.deleted_at = timezone.now()
            ticket.save()

            user_wallet = Wallet.objects.select_for_update().get(user=ticket.user)
            user_wallet.balance += ticket.stake_amount
            user_wallet.save()

            Transaction.objects.create(
                user=ticket.user,
                initiating_user=request.user,
                target_user=ticket.user,
                transaction_type='ticket_deletion_refund',
                amount=ticket.stake_amount,
                is_successful=True,
                status='completed',
                description=f"Admin void: Stake refunded for ticket {ticket.ticket_id}",
                related_bet_ticket=ticket,
                timestamp=timezone.now()
            )
            messages.success(request, f"Bet ticket {ticket.ticket_id} successfully voided/deleted and stake refunded to {ticket.user.email}.")
            log_admin_activity(request, f"Voided/Deleted bet ticket {ticket.ticket_id} and refunded stake.")
        except Exception as e:
            messages.error(request, f"Failed to void ticket {ticket.ticket_id}: {e}")
            log_admin_activity(request, f"Failed to void ticket {ticket.ticket_id}. Error: {e}")
        
    return redirect('betting_admin:admin_ticket_report') 

@login_required
@user_passes_test(lambda u: u.is_superuser or u.user_type == 'admin')
@db_transaction.atomic
def admin_settle_won_ticket_single(request, ticket_id):
    ticket = get_object_or_404(BetTicket, id=ticket_id)

    if request.method == 'POST':
        if ticket.status != 'pending':
            messages.warning(request, f"Ticket {ticket.ticket_id} is already '{ticket.status}' and cannot be manually settled as won.")
            return redirect('betting_admin:admin_ticket_report') 

        try:
            ticket.status = 'won'
            ticket.save()

            user_wallet = Wallet.objects.select_for_update().get(user=ticket.user)
            winnings_amount = ticket.max_winning
            user_wallet.balance += winnings_amount
            user_wallet.save()

            Transaction.objects.create(
                user=ticket.user,
                initiating_user=request.user,
                target_user=ticket.user,
                transaction_type='bet_payout',
                amount=winnings_amount,
                is_successful=True,
                status='completed',
                description=f"Admin payout: Winnings for Bet Ticket {ticket.ticket_id}",
                related_bet_ticket=ticket,
                timestamp=timezone.now()
            )
            messages.success(request, f"Bet ticket {ticket.ticket_id} settled as WON and winnings of ₦{winnings_amount:.2f} paid to {ticket.user.email}.")
            log_admin_activity(request, f"Settled bet ticket {ticket.ticket_id} as WON and paid out winnings.")
        except Exception as e:
            messages.error(request, f"Failed to settle ticket {ticket.ticket_id} as WON: {e}")
            log_admin_activity(request, f"Failed to settle ticket {ticket.ticket_id} as WON. Error: {e}")
        
    return redirect('betting_admin:admin_ticket_report') 


@login_required
@user_passes_test(lambda u: u.is_superuser or u.user_type == 'admin')
def admin_wallet_report(request):
    report_title = "Admin Wallet Report"
    user_filter = request.GET.get('user', 'all')

    wallets_queryset = Wallet.objects.all()

    if user_filter != 'all':
        try:
            filter_user_id = int(user_filter)
            wallets_queryset = wallets_queryset.filter(user__id=filter_user_id)
        except (ValueError, TypeError):
            pass 

    paginator = Paginator(wallets_queryset.order_by('user__email'), 10)
    page_number = request.GET.get('page')

    try:
        wallets = paginator.page(page_number)
    except PageNotAnInteger:
        wallets = paginator.page(1)
    except EmptyPage:
        wallets = paginator.page(paginator.num_pages)

    context = {
        'report_title': report_title,
        'wallets': wallets,
        'all_users': User.objects.all().order_by('email'), 
        'current_user_filter': user_filter,
    }
    return render(request, 'betting/admin/wallet_report.html', context)


@login_required
@user_passes_test(lambda u: u.is_superuser or u.user_type == 'admin')
def admin_sales_winnings_report(request):
    report_title = "Admin Sales & Winnings Report"
    user_filter = request.GET.get('user', 'all')
    start_date_str = request.GET.get('start_date')
    end_date_str = request.GET.get('end_date')

    end_date = date.fromisoformat(end_date_str) if end_date_str else timezone.now().date()
    start_date = date.fromisoformat(start_date_str) if start_date_str else end_date - timedelta(days=30)

    users_qs = User.objects.all()

    if user_filter != 'all':
        try:
            filter_user_id = int(user_filter)
            users_qs = users_qs.filter(id=filter_user_id)
        except (ValueError, TypeError):
            pass 

    report_data = []
    for u in users_qs.order_by('email'):
        # CORRECTED: Changed 'betticket__' to 'bet_tickets__'
        bets_by_user = BetTicket.objects.filter(
            user=u,
            placed_at__date__gte=start_date,
            placed_at__date__lt=end_date + timedelta(days=1) 
        ).exclude(status='deleted')

        total_stake = bets_by_user.aggregate(Sum('stake_amount'))['stake_amount__sum'] or Decimal('0.00')
        total_winnings = bets_by_user.filter(status='won').aggregate(Sum('potential_winning'))['potential_winning__sum'] or Decimal('0.00')
        
        net_result = total_stake - total_winnings

        if total_stake > Decimal('0.00') or total_winnings > Decimal('0.00'): 
            report_data.append({
                'user': u,
                'total_stake': total_stake,
                'total_winnings': total_winnings,
                'ggr': net_result, # Changed net_result to ggr for consistency with definition
            })
    
    paginator = Paginator(report_data, 10) 
    page_number = request.GET.get('page')

    try:
        paginated_report_data = paginator.page(page_number)
    except PageNotAnInteger:
        paginated_report_data = paginator.page(1)
    except EmptyPage:
        paginated_report_data = paginator.page(paginator.num_pages)


    context = {
        'report_title': report_title,
        'report_data': paginated_report_data,
        'all_users': User.objects.all().order_by('email'), 
        'current_user_filter': user_filter,
        'start_date_filter': start_date.isoformat(),
        'end_date_filter': end_date.isoformat(),
    }
    return render(request, 'betting/admin/sales_winnings_report.html', context)


@login_required
@user_passes_test(lambda u: u.is_superuser or u.user_type == 'admin')
def admin_commission_report(request):
    from commission.models import WeeklyAgentCommission, MonthlyNetworkCommission
    
    report_title = "Admin Commission Report"
    agent_filter_id = request.GET.get('user', 'all')
    start_date_str = request.GET.get('start_date')
    end_date_str = request.GET.get('end_date')

    end_date = date.fromisoformat(end_date_str) if end_date_str else timezone.now().date()
    start_date = date.fromisoformat(start_date_str) if start_date_str else end_date - timedelta(days=30)

    # Fetch Commissions
    weekly_comms = WeeklyAgentCommission.objects.filter(
        period__end_date__gte=start_date,
        period__end_date__lte=end_date
    ).select_related('agent', 'period')
    
    monthly_comms = MonthlyNetworkCommission.objects.filter(
        period__end_date__gte=start_date,
        period__end_date__lte=end_date
    ).select_related('user', 'period')

    if agent_filter_id != 'all':
        try:
            user_id = int(agent_filter_id)
            weekly_comms = weekly_comms.filter(agent__id=user_id)
            monthly_comms = monthly_comms.filter(user__id=user_id)
        except ValueError:
            pass

    # Aggregate Data
    commission_data = []
    
    # Process Weekly
    for wc in weekly_comms:
        commission_data.append({
            'agent': wc.agent,
            'type': 'Weekly (Agent)',
            'period': str(wc.period),
            'ggr': wc.ggr,
            'commission_amount': wc.commission_total_amount,
            'status': wc.status,
            'paid_at': wc.paid_at
        })

    # Process Monthly
    for mc in monthly_comms:
        commission_data.append({
            'agent': mc.user,
            'type': f"Monthly ({mc.role.replace('_', ' ').title()})",
            'period': str(mc.period),
            'ggr': mc.ngr, # Use NGR as GGR equivalent for display
            'commission_amount': mc.commission_amount,
            'status': mc.status,
            'paid_at': mc.paid_at
        })

    # Summary Stats
    total_paid = sum(c['commission_amount'] for c in commission_data if c['status'] == 'paid')
    outstanding = sum(c['commission_amount'] for c in commission_data if c['status'] == 'pending')
    total_ggr = sum(c['ggr'] for c in commission_data)
    
    payout_ratio = Decimal(0)
    total_comm = total_paid + outstanding
    if total_ggr > 0:
        payout_ratio = (total_comm / total_ggr * 100).quantize(Decimal('0.01'))

    context = {
        'report_title': report_title,
        'commission_data': commission_data,
        'all_users': User.objects.filter(user_type__in=['agent', 'super_agent', 'master_agent']).order_by('email'),
        'current_user_filter': agent_filter_id,
        'start_date_filter': start_date.isoformat(),
        'end_date_filter': end_date.isoformat(),
        'summary': {
            'total_paid': total_paid,
            'outstanding': outstanding,
            'total_ggr': total_ggr,
            'payout_ratio': payout_ratio
        }
    }
    return render(request, 'betting/admin/commission_report.html', context)


@login_required
@user_passes_test(lambda u: u.is_superuser or u.user_type == 'admin')
def admin_activity_log(request):
    report_title = "Admin Activity Log"
    user_filter = request.GET.get('user', 'all')
    start_date_str = request.GET.get('start_date')
    end_date_str = request.GET.get('end_date')

    activity_logs_queryset = ActivityLog.objects.all().order_by('-timestamp')

    if user_filter != 'all':
        try:
            user_id = int(user_filter)
            activity_logs_queryset = activity_logs_queryset.filter(user__id=user_id)
        except (ValueError, TypeError):
            pass

    if start_date_str:
        try:
            start_date = date.fromisoformat(start_date_str)
            activity_logs_queryset = activity_logs_queryset.filter(timestamp__date__gte=start_date)
        except ValueError:
            messages.error(request, "Invalid start date format.")
    if end_date_str:
        try:
            end_date = date.fromisoformat(end_date_str)
            activity_logs_queryset = activity_logs_queryset.filter(timestamp__date__lte=end_date)
        except ValueError:
            messages.error(request, "Invalid end date format.")
    
    paginator = Paginator(activity_logs_queryset, 10)
    page_number = request.GET.get('page')

    try:
        activity_logs = paginator.page(page_number)
    except PageNotAnInteger:
        activity_logs = paginator.page(1)
    except EmptyPage:
        activity_logs = paginator.page(paginator.num_pages)

    context = {
        'report_title': report_title,
        'activity_logs': activity_logs,
        'all_users': User.objects.all().order_by('email'), 
        'current_user_filter': user_filter,
        'start_date_filter': start_date_str,
        'end_date_filter': end_date_str,
    }
    return render(request, 'betting/admin/activity_log.html', context)


@login_required
@user_passes_test(is_agent)
def agent_cashier_list(request):
    cashiers = User.objects.filter(agent=request.user, user_type='cashier').order_by('-date_joined')
    pending_requests = (
        CashierRegistrationRequest.objects
        .filter(agent=request.user, status='PENDING')
        .order_by('-created_at')
    )
    paginator = Paginator(cashiers, 10)
    page = request.GET.get('page')
    try:
        cashiers_page = paginator.page(page)
    except PageNotAnInteger:
        cashiers_page = paginator.page(1)
    except EmptyPage:
        cashiers_page = paginator.page(paginator.num_pages)

    context = {
        'cashiers': cashiers_page,
        'pending_cashier_requests': pending_requests,
    }
    return render(request, 'betting/agent/cashier_list.html', context)

@login_required
@user_passes_test(is_agent)
def agent_create_cashier(request):
    redirect_to = request.META.get('HTTP_REFERER') or reverse('betting:agent_cashier_list')
    if request.method == 'POST':
        try:
            first_name = request.POST.get('first_name')
            last_name = request.POST.get('last_name')
            other_name = request.POST.get('other_name')
            phone_number = request.POST.get('phone_number')

            if not first_name or not last_name or not other_name:
                messages.error(request, "First Name, Last Name, and Other Name are required.")
                return redirect(redirect_to)

            agent = request.user

            if not agent.cashier_prefix:
                import random
                while True:
                    prefix = str(random.randint(1000, 9999))
                    if not User.objects.filter(cashier_prefix=prefix).exists():
                        break
                agent.cashier_prefix = prefix
                agent.save(update_fields=['cashier_prefix'])

            base_prefix = agent.cashier_prefix

            existing_numbers = []
            for cashier in User.objects.filter(agent=agent, user_type='cashier').only('cashier_prefix'):
                cp = (cashier.cashier_prefix or '').strip()
                if not cp or '-' not in cp:
                    continue
                base, _, suffix = cp.partition('-')
                if base != base_prefix:
                    continue
                try:
                    existing_numbers.append(int(suffix))
                except Exception:
                    continue

            for req in CashierRegistrationRequest.objects.filter(agent=agent).exclude(status='REJECTED').only('cashier_code'):
                m = re.match(r'^C(\d+)$', (req.cashier_code or '').strip(), re.IGNORECASE)
                if m:
                    try:
                        existing_numbers.append(int(m.group(1)))
                    except Exception:
                        pass

            max_existing = max(existing_numbers) if existing_numbers else 2
            next_num = max_existing + 1

            cashier_code = f"C{next_num}"
            cashier_email = generate_cashier_email(agent.email, cashier_code)
            if User.objects.filter(email__iexact=cashier_email).exists():
                messages.error(request, f"{cashier_code} already exists for this agent.")
                return redirect(redirect_to)

            root = None
            for c in User.objects.filter(agent=agent, user_type='cashier').exclude(username__isnull=True).only('username').order_by('date_joined'):
                uname = (c.username or '').strip()
                m = re.match(r'^(.*)C\d+$', uname, re.IGNORECASE)
                if m and m.group(1):
                    root = m.group(1)
                    break
            if not root:
                root = (agent.username or (agent.email.split('@')[0] if agent.email else 'Agent')).strip()
                root = re.sub(r'[^A-Za-z0-9]', '', root)[:30] or 'Agent'

            cashier_username = f"{root}{cashier_code}"
            if User.objects.filter(username__iexact=cashier_username).exists():
                counter = 1
                while True:
                    candidate = f"{root}{cashier_code}{counter}"
                    if not User.objects.filter(username__iexact=candidate).exists():
                        cashier_username = candidate
                        break
                    counter += 1

            cashier_prefix = f"{base_prefix}-{next_num:02d}"

            CashierRegistrationRequest.objects.create(
                agent=agent,
                first_name=first_name,
                last_name=last_name,
                other_name=other_name,
                phone_number=phone_number,
                cashier_code=cashier_code,
                cashier_email=cashier_email,
                cashier_username=cashier_username,
                cashier_prefix=cashier_prefix,
                status='PENDING'
            )

            messages.success(request, f"Cashier registration {cashier_code} submitted for admin approval.")
        except Exception as e:
            messages.error(request, f"Error submitting cashier registration: {e}")
            logger.error(f"Error submitting cashier registration: {e}")
    
    return redirect(redirect_to)

@login_required
@user_passes_test(is_agent)
def agent_edit_cashier(request, cashier_id):
    if request.method == 'POST':
        cashier = get_object_or_404(User, id=cashier_id, agent=request.user, user_type='cashier')
        try:
            cashier.first_name = request.POST.get('first_name')
            cashier.last_name = request.POST.get('last_name')
            cashier.phone_number = request.POST.get('phone_number')
            # Email update is sensitive, usually requires verification, but for simplicity:
            new_email = request.POST.get('email')
            if new_email and new_email != cashier.email:
                if User.objects.filter(email=new_email).exclude(id=cashier.id).exists():
                     messages.error(request, "Email already in use by another user.")
                     return redirect('betting:agent_cashier_list')
                cashier.email = new_email
            
            cashier.save()
            messages.success(request, f"Cashier {cashier.email} updated successfully.")
        except Exception as e:
            messages.error(request, f"Error updating cashier: {e}")
    
    return redirect('betting:agent_cashier_list')

@login_required
@user_passes_test(is_agent)
def agent_delete_cashier(request, cashier_id):
    if request.method == 'POST':
        cashier = get_object_or_404(User, id=cashier_id, agent=request.user, user_type='cashier')
        try:
            # Check if cashier has active tickets or transactions?
            # For now, just delete. Standard Django behavior will cascade or set null based on model defs.
            # User model defines related_name='bet_tickets', on_delete=CASCADE usually for tickets.
            # But let's check safety.
            cashier.delete()
            messages.success(request, "Cashier deleted successfully.")
        except Exception as e:
            messages.error(request, f"Error deleting cashier: {e}")
    
    return redirect('betting:agent_cashier_list')

@login_required
@user_passes_test(is_agent)
@db_transaction.atomic
def agent_credit_cashier(request, cashier_id):
    # Check if user has permission to manage downline wallets
    if request.user.user_type in ['master_agent', 'super_agent'] and not getattr(request.user, 'can_manage_downline_wallets', True):
        CreditLog.objects.create(
            actor=request.user,
            action_type='credit_cashier_denied',
            amount=Decimal('0.00')
        )
        messages.error(request, "You do not have permission to credit or debit downline wallets. Please contact the administrator.")
        return redirect('betting:agent_cashier_list')

    if request.method == 'POST':
        cashier = get_object_or_404(User, id=cashier_id, agent=request.user, user_type='cashier')
        amount_str = request.POST.get('amount')
        
        try:
            amount = Decimal(amount_str)
            if amount <= 0:
                messages.error(request, "Amount must be positive.")
                return redirect('betting:agent_cashier_list')
            
            # Ensure wallets exist before locking
            _ = request.user.wallet
            _ = cashier.wallet

            agent_wallet = Wallet.objects.select_for_update().get(user=request.user)
            if agent_wallet.balance < amount:
                messages.error(request, "Insufficient funds in your wallet.")
                return redirect('betting:agent_cashier_list')
            
            cashier_wallet = Wallet.objects.select_for_update().get(user=cashier)
            
            # Perform transfer
            agent_wallet.balance -= amount
            agent_wallet.save()
            
            cashier_wallet.balance += amount
            cashier_wallet.save()
            
            # Create transactions
            Transaction.objects.create(
                user=request.user,
                transaction_type='wallet_transfer_out',
                amount=amount,
                status='completed',
                is_successful=True,
                target_user=cashier,
                description=f"Transfer to cashier {cashier.email}"
            )
            
            Transaction.objects.create(
                user=cashier,
                transaction_type='wallet_transfer_in',
                amount=amount,
                status='completed',
                is_successful=True,
                initiating_user=request.user,
                description=f"Received credit from agent {request.user.email}"
            )
            
            messages.success(request, f"Successfully credited ₦{amount} to {cashier.email}.")
            
        except InvalidOperation:
            messages.error(request, "Invalid amount format.")
        except Exception as e:
            messages.error(request, f"Error processing credit: {e}")
            
    return redirect('betting:agent_cashier_list')


# --- API Endpoints (Placeholder implementations) ---

@csrf_exempt
def api_betting_periods(request):
    return JsonResponse({'status': 'success', 'message': 'API endpoint for betting periods (placeholder).'})

@csrf_exempt
def api_fixtures(request):
    return JsonResponse({'status': 'success', 'message': 'API endpoint for fixtures (placeholder).'})

@csrf_exempt
def api_place_bet(request):
    return JsonResponse({'status': 'success', 'message': 'API endpoint for place bet (placeholder).'})

@csrf_exempt
def api_check_ticket_status(request):
    return JsonResponse({'status': 'success', 'message': 'API endpoint for check ticket status (placeholder).'})

@login_required
def api_user_wallet(request):
    try:
        wallet = request.user.wallet
        return JsonResponse({'status': 'success', 'balance': float(wallet.balance)})
    except Exception as e:
        return JsonResponse({'status': 'error', 'message': str(e)})

@csrf_exempt
def api_initiate_deposit(request):
    return JsonResponse({'status': 'success', 'message': 'API endpoint for initiate deposit (placeholder).'})

@csrf_exempt
def api_verify_deposit(request):
    return JsonResponse({'status': 'success', 'message': 'API endpoint for verify deposit (placeholder).'})

@csrf_exempt
def api_withdraw_funds(request):
    return JsonResponse({'status': 'success', 'message': 'API endpoint for withdraw funds (placeholder).'})

@csrf_exempt
def api_wallet_transfer(request):
    return JsonResponse({'status': 'success', 'message': 'API endpoint for wallet transfer (placeholder).'})

@csrf_exempt
def api_user_profile(request):
    return JsonResponse({'status': 'success', 'message': 'API endpoint for user profile (placeholder).'})

@csrf_exempt
def api_change_password(request):
    return JsonResponse({'status': 'success', 'message': 'API endpoint for change password (placeholder).'})

@csrf_exempt
def api_user_transactions(request):
    return JsonResponse({'status': 'success', 'message': 'API endpoint for user transactions (placeholder).'})

@csrf_exempt
def api_agent_commissions(request):
    return JsonResponse({'status': 'success', 'message': 'API endpoint for agent commissions (placeholder).'})

@csrf_exempt
def api_agent_users(request):
    return JsonResponse({'status': 'success', 'message': 'API endpoint for agent users (placeholder).'})

@csrf_exempt
def api_cashier_transactions(request):
    return JsonResponse({'status': 'success', 'message': 'API endpoint for cashier transactions (placeholder).'})

@csrf_exempt
def api_bet_tickets(request):
    return JsonResponse({'status': 'success', 'message': 'API endpoint for bet tickets (placeholder).'})

@csrf_exempt
def api_void_ticket(request):
    return JsonResponse({'status': 'success', 'message': 'API endpoint for voiding a ticket (placeholder).'})

@csrf_exempt
def api_manage_users(request):
    return JsonResponse({'status': 'success', 'message': 'API endpoint for managing users (placeholder).'})

@csrf_exempt
def api_system_settings(request):
    return JsonResponse({'status': 'success', 'message': 'API endpoint for system settings (placeholder).'})


@login_required
def mark_downline_activity_notifications_read(request):
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'Invalid request.'}, status=400)

    if request.user.user_type not in ['agent', 'super_agent', 'master_agent']:
        return JsonResponse({'status': 'error', 'message': 'Not allowed.'}, status=403)

    request.user.downline_activity_last_seen_at = timezone.now()
    request.user.save(update_fields=['downline_activity_last_seen_at'])
    return JsonResponse({'status': 'success'})

# --- Account User Views ---

from commission.models import WeeklyAgentCommission, MonthlyNetworkCommission
from commission.services import pay_weekly_commission, pay_monthly_network_commission

@login_required
@user_passes_test(is_account_user)
def account_user_dashboard(request):
    search_form = AccountUserSearchForm()
    action_form = AccountUserWalletActionForm()
    found_user = None
    search_results = None
    activity_log = []

    # --- NEW: Fetch Credit/Loan Data ---
    all_incoming_credit_requests = CreditRequest.objects.filter(
        recipient=request.user, 
        status='pending'
    ).order_by('-created_at')
    requests_paginator = Paginator(all_incoming_credit_requests, 10)
    requests_page = request.GET.get('requests_page')
    try:
        incoming_credit_requests = requests_paginator.page(requests_page)
    except PageNotAnInteger:
        incoming_credit_requests = requests_paginator.page(1)
    except EmptyPage:
        incoming_credit_requests = requests_paginator.page(requests_paginator.num_pages)

    all_active_loans_given = Loan.objects.filter(
        lender=request.user, 
        status='active'
    ).order_by('-created_at')
    loans_paginator = Paginator(all_active_loans_given, 10)
    loans_page = request.GET.get('loans_page')
    try:
        active_loans_given = loans_paginator.page(loans_page)
    except PageNotAnInteger:
        active_loans_given = loans_paginator.page(1)
    except EmptyPage:
        active_loans_given = loans_paginator.page(loans_paginator.num_pages)
    # -----------------------------------

    recent_transactions = Transaction.objects.filter(
        Q(initiating_user=request.user) | Q(user=request.user)
    ).order_by('-timestamp')[:20]

    # Handle View User via GET
    if request.method == 'GET' and 'view_user_id' in request.GET:
        try:
            found_user = User.objects.exclude(is_superuser=True).exclude(user_type='account_user').get(id=request.GET.get('view_user_id'))
        except (User.DoesNotExist, ValueError):
            messages.error(request, "User not found or invalid ID.")

    if request.method == 'POST':
        if 'pay_commissions' in request.POST:
            selected_items = request.POST.getlist('selected_commissions')
            success_count = 0
            error_count = 0
            
            for item in selected_items:
                try:
                    comm_type, comm_id = item.split('_')
                    if comm_type == 'weekly':
                        comm = WeeklyAgentCommission.objects.get(id=comm_id)
                        success, msg = pay_weekly_commission(comm)
                    elif comm_type == 'monthly':
                        comm = MonthlyNetworkCommission.objects.get(id=comm_id)
                        success, msg = pay_monthly_network_commission(comm)
                    else:
                        success, msg = False, "Invalid type"
                    
                    if success:
                        success_count += 1
                    else:
                        error_count += 1
                        messages.error(request, f"Error paying {item}: {msg}")
                except Exception as e:
                    error_count += 1
                    messages.error(request, f"Error processing {item}: {str(e)}")
            
            if success_count > 0:
                messages.success(request, f"Successfully paid {success_count} commissions.")
            return redirect('betting:account_user_dashboard')

        elif 'process_withdrawals' in request.POST:
            selected_withdrawals = request.POST.getlist('selected_withdrawals')
            action = request.POST.get('withdrawal_action')
            
            success_count = 0
            for w_id in selected_withdrawals:
                try:
                    with db_transaction.atomic():
                        withdrawal = UserWithdrawal.objects.select_for_update().get(id=w_id)
                        
                        if withdrawal.status == 'pending':
                            if action == 'mark_paid':
                                # 1. Capture Audit Data (Balance Snapshot)
                                try:
                                    user_wallet = Wallet.objects.select_for_update().get(user=withdrawal.user)
                                    withdrawal.balance_after = user_wallet.balance 
                                    withdrawal.balance_before = user_wallet.balance + withdrawal.amount
                                except Wallet.DoesNotExist:
                                    pass

                                withdrawal.status = 'completed'
                                withdrawal.approved_rejected_by = request.user
                                withdrawal.approved_rejected_time = timezone.now()
                                withdrawal.processed_ip = request.META.get('REMOTE_ADDR')
                                withdrawal.save()

                                # 2. Credit Account User (Processor) Wallet
                                # Assumption: Account User paid cash, system reimburses them.
                                processor_wallet = Wallet.objects.select_for_update().get(user=request.user)
                                
                                # Capture Approver Balances
                                withdrawal.approver_balance_before = processor_wallet.balance
                                processor_wallet.balance += withdrawal.amount
                                withdrawal.approver_balance_after = processor_wallet.balance
                                withdrawal.save()

                                processor_wallet.save()

                                Transaction.objects.create(
                                    user=request.user,
                                    initiating_user=request.user,
                                    transaction_type='account_user_credit',
                                    amount=withdrawal.amount,
                                    status='completed',
                                    is_successful=True,
                                    description=f"Reimbursement for processing withdrawal {withdrawal.id} for {withdrawal.user.email}",
                                    timestamp=timezone.now()
                                )

                                success_count += 1

                            elif action == 'reject':
                                withdrawal.status = 'rejected'
                                withdrawal.approved_rejected_by = request.user
                                withdrawal.approved_rejected_time = timezone.now()
                                withdrawal.processed_ip = request.META.get('REMOTE_ADDR')
                                withdrawal.save() # Signal handles refund
                                success_count += 1

                except UserWithdrawal.DoesNotExist:
                    pass
                except Exception as e:
                    messages.error(request, f"Error processing withdrawal {w_id}: {e}")
            
            if success_count > 0:
                messages.success(request, f"Successfully processed {success_count} withdrawals.")
            return redirect('betting:account_user_dashboard')

        elif 'search_user' in request.POST:
            search_form = AccountUserSearchForm(request.POST)
            if search_form.is_valid():
                search_term = (search_form.cleaned_data.get('search_term') or '').strip()

                base_qs = User.objects.filter(
                    Q(email__icontains=search_term) |
                    Q(phone_number__icontains=search_term) |
                    Q(username__icontains=search_term) |
                    Q(first_name__icontains=search_term) |
                    Q(last_name__icontains=search_term) |
                    Q(other_name__icontains=search_term)
                ).exclude(is_superuser=True).exclude(user_type='account_user')

                tokens = [t for t in re.split(r'\s+', search_term) if t]
                name_qs = User.objects.none()
                if len(tokens) > 1 and '@' not in search_term:
                    tokens_q = Q()
                    for t in tokens:
                        tokens_q &= (Q(first_name__icontains=t) | Q(last_name__icontains=t) | Q(other_name__icontains=t))
                    name_qs = User.objects.filter(tokens_q).exclude(is_superuser=True).exclude(user_type='account_user')

                users = (base_qs | name_qs)
                
                if search_term.isdigit():
                     # Prioritize exact ID match if search term is a digit (likely from autocomplete)
                     exact_match = User.objects.filter(id=int(search_term)).exclude(is_superuser=True).first()
                     if exact_match:
                         found_user = exact_match
                         users = User.objects.filter(pk=exact_match.pk)
                     else:
                         users = users | User.objects.filter(id=int(search_term)).exclude(is_superuser=True).exclude(user_type='account_user')

                users = users.distinct()
                
                if found_user:
                     pass # Already found via exact ID match
                elif users.count() == 1:
                    found_user = users.first()
                    messages.success(request, f"User found: {found_user.get_full_name()} ({found_user.email})")
                elif users.count() > 1:
                    search_results = users
                    messages.warning(request, "Multiple users found. Please select one.")
                else:
                    messages.error(request, "No user found.")
        
        elif 'perform_action' in request.POST:
            action_form = AccountUserWalletActionForm(request.POST)
            target_user_id = request.POST.get('target_user_id')
            if target_user_id:
                target_user = get_object_or_404(User, id=target_user_id)
                
                if target_user.is_superuser or target_user.user_type == 'account_user':
                    messages.error(request, "Operation not allowed on this user.")
                    return redirect('betting:account_user_dashboard')

                if action_form.is_valid():
                    action = action_form.cleaned_data['action']
                    amount = action_form.cleaned_data['amount']
                    description = action_form.cleaned_data['description']
                    
                    try:
                        with db_transaction.atomic():
                            account_wallet = Wallet.objects.select_for_update().get(user=request.user)
                            target_wallet = Wallet.objects.select_for_update().get(user=target_user)
                            
                            if action == 'credit':
                                if account_wallet.balance < amount:
                                    raise InvalidOperation("Insufficient funds in your account wallet.")
                                
                                account_wallet.balance -= amount
                                target_wallet.balance += amount
                                
                                tx_type_account = 'account_user_debit'
                                tx_type_target = 'account_user_credit'

                            elif action == 'debit':
                                if target_wallet.balance < amount:
                                    raise InvalidOperation("User has insufficient funds.")
                                
                                target_wallet.balance -= amount
                                account_wallet.balance += amount
                                
                                tx_type_account = 'account_user_credit'
                                tx_type_target = 'account_user_debit'

                            account_wallet.save()
                            target_wallet.save()
                            
                            Transaction.objects.create(
                                user=request.user,
                                initiating_user=request.user,
                                transaction_type=tx_type_account,
                                amount=amount,
                                status='completed',
                                is_successful=True,
                                description=f"{action.title()} for user {target_user.email}: {description}"
                            )
                            
                            Transaction.objects.create(
                                user=target_user,
                                initiating_user=request.user,
                                transaction_type=tx_type_target,
                                amount=amount,
                                status='completed',
                                is_successful=True,
                                description=f"{action.title()} by Account Manager: {description}"
                            )
                            
                            # Log to Admin Activity Log
                            log_admin_activity(
                                request, 
                                f"Account User Manual {action} of {amount} for {target_user.email}. Reason: {description}",
                                action_type=f"MANUAL_{action.upper()}",
                                affected_object=target_user.email
                            )

                            messages.success(request, f"Successfully {action}ed {amount} for {target_user.email}.")
                            found_user = None 
                            
                    except InvalidOperation as e:
                        messages.error(request, str(e))
                    except Exception as e:
                        messages.error(request, f"An error occurred: {str(e)}")
                        logger.error(f"Account User Action Error: {traceback.format_exc()}")
            else:
                 messages.error(request, "Target user not specified.")

    # Fetch Pending Commissions
    pending_weekly = WeeklyAgentCommission.objects.filter(status='pending').select_related('agent', 'period').order_by('period__start_date')
    pending_monthly = MonthlyNetworkCommission.objects.filter(status='pending').select_related('user', 'period').order_by('period__start_date')
    
    try:
        from commission.services import calculate_weekly_agent_commission_data, calculate_monthly_network_commission_data
    except Exception:
        calculate_weekly_agent_commission_data = None
        calculate_monthly_network_commission_data = None

    if calculate_weekly_agent_commission_data:
        for wc in pending_weekly:
            data = calculate_weekly_agent_commission_data(wc.agent, wc.period)
            if not data:
                continue
            changed = False
            for field, value in data.items():
                if getattr(wc, field) != value:
                    setattr(wc, field, value)
                    changed = True
            if changed:
                wc.save(update_fields=list(data.keys()))

    if calculate_monthly_network_commission_data:
        for mc in pending_monthly:
            data = calculate_monthly_network_commission_data(mc.user, mc.period)
            if not data:
                continue
            changed = False
            for field, value in data.items():
                if getattr(mc, field) != value:
                    setattr(mc, field, value)
                    changed = True
            if changed:
                mc.save(update_fields=list(data.keys()))

    pending_commissions = []
    for wc in pending_weekly:
        pending_commissions.append({
            'id_str': f"weekly_{wc.id}",
            'type': 'Weekly',
            'user': wc.agent,
            'period': wc.period,
            'amount': wc.commission_total_amount,
            'ggr_ngr': wc.ggr
        })
    
    for mc in pending_monthly:
        pending_commissions.append({
            'id_str': f"monthly_{mc.id}",
            'type': f"Monthly ({mc.role.replace('_', ' ').title()})",
            'user': mc.user,
            'period': mc.period,
            'amount': mc.commission_amount,
            'ggr_ngr': mc.ngr
        })

    # Fetch Pending Withdrawals
    all_pending_withdrawals = UserWithdrawal.objects.filter(status='pending').select_related('user').order_by('request_time')
    withdrawals_paginator = Paginator(all_pending_withdrawals, 10)
    withdrawals_page = request.GET.get('withdrawals_page')
    try:
        pending_withdrawals = withdrawals_paginator.page(withdrawals_page)
    except PageNotAnInteger:
        pending_withdrawals = withdrawals_paginator.page(1)
    except EmptyPage:
        pending_withdrawals = withdrawals_paginator.page(withdrawals_paginator.num_pages)
        
    # Paginate Pending Commissions (List)
    commissions_paginator = Paginator(pending_commissions, 10)
    commissions_page = request.GET.get('commissions_page')
    try:
        pending_commissions_page = commissions_paginator.page(commissions_page)
    except PageNotAnInteger:
        pending_commissions_page = commissions_paginator.page(1)
    except EmptyPage:
        pending_commissions_page = commissions_paginator.page(commissions_paginator.num_pages)

    # Construct Activity Log if user found
    if found_user:
        user_transactions = Transaction.objects.filter(user=found_user).order_by('-timestamp')
        user_bets = BetTicket.objects.filter(user=found_user).order_by('-placed_at')
        
        for t in user_transactions:
            activity_log.append({
                'timestamp': t.timestamp,
                'type': 'Transaction',
                'description': t.description or t.get_transaction_type_display(),
                'amount': t.amount,
                'status': t.status,
                'ref': t.external_reference or t.paystack_reference or str(t.id)[:8],
                'is_credit': t.transaction_type in ['deposit', 'bet_payout', 'commission_payout', 'wallet_transfer_in', 'bonus', 'withdrawal_refund', 'account_user_credit']
            })
            
        for b in user_bets:
             activity_log.append({
                'timestamp': b.placed_at,
                'type': 'Bet Placement',
                'description': f"{b.get_bet_type_display().title()} Bet ({b.selections.count()} selections)",
                'amount': b.stake_amount,
                'status': b.status,
                'ref': b.ticket_id,
                'is_credit': False # Bets are debits (stakes)
            })
            
        activity_log.sort(key=lambda x: x['timestamp'], reverse=True)

    # --- Bet Ticket Management (Admin-like View) ---
    ticket_search_query = request.GET.get('ticket_search', '').strip()
    ticket_status_filter = request.GET.get('ticket_status', '').strip()
    ticket_date_from = request.GET.get('ticket_date_from', '').strip()
    ticket_date_to = request.GET.get('ticket_date_to', '').strip()

    all_tickets = BetTicket.objects.all().select_related('user').order_by('-placed_at')

    if ticket_search_query:
        all_tickets = all_tickets.filter(
            Q(ticket_id__icontains=ticket_search_query) |
            Q(user__email__icontains=ticket_search_query) |
            Q(user__first_name__icontains=ticket_search_query) |
            Q(user__last_name__icontains=ticket_search_query)
        )
    
    if ticket_status_filter:
        all_tickets = all_tickets.filter(status=ticket_status_filter)

    if ticket_date_from:
        try:
            date_from = datetime.strptime(ticket_date_from, '%Y-%m-%d')
            all_tickets = all_tickets.filter(placed_at__gte=timezone.make_aware(date_from))
        except ValueError:
            pass 
            
    if ticket_date_to:
        try:
            date_to = datetime.strptime(ticket_date_to, '%Y-%m-%d')
            # Add 1 day to include the end date fully (end of day)
            date_to = date_to.replace(hour=23, minute=59, second=59)
            all_tickets = all_tickets.filter(placed_at__lte=timezone.make_aware(date_to))
        except ValueError:
            pass

    tickets_paginator = Paginator(all_tickets, 20) # 20 tickets per page
    tickets_page_num = request.GET.get('tickets_page')
    try:
        tickets_page = tickets_paginator.page(tickets_page_num)
    except PageNotAnInteger:
        tickets_page = tickets_paginator.page(1)
    except EmptyPage:
        tickets_page = tickets_paginator.page(tickets_paginator.num_pages)
    # -----------------------------------------------

    # --- Wallets Management ---
    wallet_search = request.GET.get('wallet_search', '')
    all_wallets = Wallet.objects.select_related('user').all().order_by('-balance')
    
    if wallet_search:
        all_wallets = all_wallets.filter(
            Q(user__email__icontains=wallet_search) | 
            Q(user__first_name__icontains=wallet_search) | 
            Q(user__last_name__icontains=wallet_search)
        )

    wallets_paginator = Paginator(all_wallets, 20)
    wallets_page_num = request.GET.get('wallets_page')
    try:
        wallets_page = wallets_paginator.page(wallets_page_num)
    except PageNotAnInteger:
        wallets_page = wallets_paginator.page(1)
    except EmptyPage:
        wallets_page = wallets_paginator.page(wallets_paginator.num_pages)

    # --- Transactions Management ---
    txn_search = request.GET.get('txn_search', '')
    txn_type_filter = request.GET.get('txn_type_filter', '')
    all_transactions = Transaction.objects.select_related('user', 'initiating_user').all().order_by('-timestamp')

    if txn_search:
        all_transactions = all_transactions.filter(
            Q(transaction_id__icontains=txn_search) |
            Q(user__email__icontains=txn_search) |
            Q(user__first_name__icontains=txn_search) |
            Q(user__last_name__icontains=txn_search)
        )
    
    if txn_type_filter:
        all_transactions = all_transactions.filter(transaction_type=txn_type_filter)

    transactions_paginator = Paginator(all_transactions, 20)
    transactions_page_num = request.GET.get('transactions_page')
    try:
        transactions_page = transactions_paginator.page(transactions_page_num)
    except PageNotAnInteger:
        transactions_page = transactions_paginator.page(1)
    except EmptyPage:
        transactions_page = transactions_paginator.page(transactions_paginator.num_pages)

    # --- Processed Withdrawals Management ---
    pw_search = request.GET.get('pw_search', '')
    pw_status_filter = request.GET.get('pw_status_filter', '')
    all_processed_withdrawals = ProcessedWithdrawal.objects.filter(status__in=['approved', 'completed', 'rejected']).order_by('-approved_rejected_time')

    if pw_search:
        all_processed_withdrawals = all_processed_withdrawals.filter(
            Q(user__email__icontains=pw_search) |
            Q(user__first_name__icontains=pw_search) |
            Q(user__last_name__icontains=pw_search) |
            Q(bank_name__icontains=pw_search) |
            Q(account_number__icontains=pw_search)
        )
    
    if pw_status_filter:
        all_processed_withdrawals = all_processed_withdrawals.filter(status=pw_status_filter)

    pw_paginator = Paginator(all_processed_withdrawals, 20)
    pw_page_num = request.GET.get('processed_withdrawals_page')
    try:
        processed_withdrawals_page = pw_paginator.page(pw_page_num)
    except PageNotAnInteger:
        processed_withdrawals_page = pw_paginator.page(1)
    except EmptyPage:
        processed_withdrawals_page = pw_paginator.page(pw_paginator.num_pages)

    context = {
        'wallets_page': wallets_page,
        'wallet_search': wallet_search,
        'transactions_page': transactions_page,
        'txn_search': txn_search,
        'txn_type_filter': txn_type_filter,
        'processed_withdrawals_page': processed_withdrawals_page,
        'pw_search': pw_search,
        'pw_status_filter': pw_status_filter,
        'tickets_page': tickets_page,
        'ticket_search_query': ticket_search_query,
        'ticket_status_filter': ticket_status_filter,
        'ticket_date_from': ticket_date_from,
        'ticket_date_to': ticket_date_to,
        'incoming_credit_requests': incoming_credit_requests, # NEW
        'active_loans_given': active_loans_given,         # NEW
        'pending_withdrawals': pending_withdrawals,
        'search_form': search_form,
        'action_form': action_form,
        'found_user': found_user,
        'search_results': search_results,
        'activity_log': activity_log,
        'recent_transactions': recent_transactions,
        'wallet': request.user.wallet,
        'pending_commissions': pending_commissions_page, # Paginated
    }
    return render(request, 'betting/account_user_dashboard.html', context)


@login_required
@user_passes_test(lambda u: u.is_superuser)
def super_admin_fund_account_user(request):
    if request.method == 'POST':
        form = SuperAdminFundAccountUserForm(request.POST)
        if form.is_valid():
            account_user = form.cleaned_data['account_user']
            action = form.cleaned_data['action']
            amount = form.cleaned_data['amount']
            description = form.cleaned_data['description']
            
            try:
                with db_transaction.atomic():
                    wallet = Wallet.objects.select_for_update().get(user=account_user)
                    
                    if action == 'credit':
                        wallet.balance += amount
                        tx_type = 'account_user_credit'
                    else: # debit
                        if wallet.balance < amount:
                            raise InvalidOperation("Account User has insufficient funds.")
                        wallet.balance -= amount
                        tx_type = 'account_user_debit'
                    
                    wallet.save()
                    
                    Transaction.objects.create(
                        user=account_user,
                        initiating_user=request.user,
                        transaction_type=tx_type,
                        amount=amount,
                        status='completed',
                        is_successful=True,
                        description=f"Super Admin Action ({action}): {description}"
                    )
                    
                    messages.success(request, f"Successfully {action}ed {amount} for {account_user.email}.")
                    return redirect('betting:super_admin_fund_account_user')
            except InvalidOperation as e:
                messages.error(request, str(e))
            except Exception as e:
                messages.error(request, f"Error: {str(e)}")
    else:
        form = SuperAdminFundAccountUserForm()
    
    recent_transactions = Transaction.objects.filter(
        user__user_type='account_user',
        initiating_user=request.user
    ).order_by('-timestamp')[:20]

    return render(request, 'betting/super_admin_fund_account_user.html', {
        'form': form,
        'recent_transactions': recent_transactions
    })


@login_required
def impersonate_user(request, user_id):
    # Permission check
    if not (request.user.is_superuser or request.user.has_perm('betting.can_impersonate_users')):
        messages.error(request, "You do not have permission to impersonate users.")
        # Try to redirect to betting_admin dashboard, else fallback
        try:
            return redirect('betting_admin:dashboard')
        except:
            return redirect('/')

    target_user = get_object_or_404(User, pk=user_id)

    # Security Checks
    if target_user.is_superuser:
        messages.error(request, "Cannot impersonate a superuser.")
        return redirect(request.META.get('HTTP_REFERER', '/'))
    
    if request.session.get('impersonation_active'):
        messages.error(request, "Nested impersonation is not allowed.")
        return redirect(request.META.get('HTTP_REFERER', '/'))

    # Create Log
    log = ImpersonationLog.objects.create(
        admin_user=request.user,
        target_user=target_user,
        ip_address=request.META.get('REMOTE_ADDR'),
        user_agent=request.META.get('HTTP_USER_AGENT', ''),
        started_at=timezone.now()
    )

    # Capture values to persist across session flush
    original_admin_id = request.user.pk
    impersonation_started_at = str(timezone.now())
    impersonated_user_id = target_user.pk
    log_id = log.pk

    # Switch User (without password)
    # We need to set the backend manually because we are logging in without authentication
    backend = 'django.contrib.auth.backends.ModelBackend' # Standard Django backend
    target_user.backend = backend
    login(request, target_user)

    # RESTORE session details after login flush
    request.session['original_admin_id'] = original_admin_id
    request.session['impersonation_started_at'] = impersonation_started_at
    request.session['impersonation_active'] = True
    request.session['impersonated_user_id'] = impersonated_user_id
    request.session['impersonation_log_id'] = log_id

    messages.success(request, f"Now impersonating {target_user.email}")
    
    # Redirect based on user type
    if target_user.user_type == 'account_user':
        return redirect('betting:account_user_dashboard')
    elif target_user.user_type == 'agent':
        return redirect('betting:agent_dashboard')
    elif target_user.user_type == 'master_agent':
        return redirect('betting:master_agent_dashboard')
    elif target_user.user_type == 'super_agent':
        return redirect('betting:super_agent_dashboard')
    else:
        return redirect('betting:user_dashboard')


@login_required
def stop_impersonation(request):
    if not request.session.get('impersonation_active'):
        return redirect('/')

    original_admin_id = request.session.get('original_admin_id')
    log_id = request.session.get('impersonation_log_id')

    if not original_admin_id:
        logout(request)
        return redirect('/admin/login/')

    try:
        original_user = User.objects.get(pk=original_admin_id)
        # Re-login as admin
        backend = 'django.contrib.auth.backends.ModelBackend'
        original_user.backend = backend
        login(request, original_user)
        
        # Update Log
        if log_id:
            try:
                log = ImpersonationLog.objects.get(pk=log_id)
                log.ended_at = timezone.now()
                log.duration = log.ended_at - log.started_at
                log.termination_reason = "Manual Exit"
                log.save()
            except ImpersonationLog.DoesNotExist:
                pass

        # Clear session keys
        keys_to_pop = ['impersonation_active', 'original_admin_id', 'impersonation_started_at', 'impersonation_log_id', 'impersonated_user_id']
        for key in keys_to_pop:
            request.session.pop(key, None)

        messages.info(request, "Impersonation ended. Welcome back.")
        return redirect('betting_admin:dashboard')

    except User.DoesNotExist:
        logout(request)
        return redirect('/admin/login/')

@login_required
def api_betting_limits(request):
    ticket_type = request.GET.get('ticket_type') or request.GET.get('bet_type')
    limits = get_effective_betting_limits_for_user(request.user, ticket_type=ticket_type)
    return JsonResponse({'success': True, 'limits': serialize_limits(limits)})

@login_required
def api_downline_search(request):
    """
    API endpoint for searching downline users for Wallet Transfer.
    Filters based on logged-in user's role.
    """
    search_term = request.GET.get('q', '')
    page = request.GET.get('page', 1)
    
    user = request.user
    queryset = User.objects.none()

    if user.user_type == 'agent':
        # Agents see their Cashiers and Players
        queryset = User.objects.filter(
            Q(agent=user) & 
            Q(user_type__in=['cashier', 'player'])
        )
    elif user.user_type == 'super_agent':
        # Super Agents see their direct Agents only
        queryset = User.objects.filter(
            super_agent=user,
            user_type='agent'
        )
    elif user.user_type == 'master_agent':
        # Master Agents see Super Agents or Agents (depending on hierarchy)
        # Check both direct and indirect (via Super Agent)
        queryset = User.objects.filter(
            Q(master_agent=user) |
            Q(super_agent__master_agent=user)
        ).filter(user_type__in=['super_agent', 'agent']).distinct()
        
    elif user.user_type == 'account_user':
        # Account Users can see Master Agents, Super Agents, Agents, Cashiers
        queryset = User.objects.filter(
            user_type__in=['master_agent', 'super_agent', 'agent', 'cashier']
        )
    
    # Apply search filter
    if search_term:
        queryset = queryset.filter(
            Q(email__icontains=search_term) | 
            Q(first_name__icontains=search_term) | 
            Q(last_name__icontains=search_term) |
            Q(cashier_prefix__icontains=search_term)
        )
        
    # Ordering
    queryset = queryset.order_by('email')
    
    # Pagination
    paginator = Paginator(queryset, 20)
    try:
        users_page = paginator.page(page)
    except PageNotAnInteger:
        users_page = paginator.page(1)
    except EmptyPage:
        users_page = paginator.page(paginator.num_pages)
        
    results = []
    id_list = [u.id for u in users_page]
    wallet_map = {
        row["user_id"]: row["balance"]
        for row in Wallet.objects.filter(user_id__in=id_list).values("user_id", "balance")
    }
    for u in users_page:
        role_label = u.get_user_type_display()
        name_str = u.get_full_name()
        if not name_str:
             name_str = u.first_name if u.first_name else ""
        
        display_name = name_str
        if u.user_type == 'cashier' and u.username:
            display_name = u.username

        display_text = f"{display_name} ({role_label}) - {u.email}"
        if u.user_type == 'cashier' and u.cashier_prefix:
            display_text = f"{u.cashier_prefix} - {display_text}"
            
        results.append({
            'id': u.id,
            'text': display_text,
            'balance': float(wallet_map.get(u.id) or 0),
        })
        
    return JsonResponse({
        'results': results,
        'pagination': {
            'more': users_page.has_next()
        }
    })


@login_required
def api_downline_wallet_balance(request):
    user = request.user
    try:
        target_id = int(request.GET.get("user_id", "0"))
    except (TypeError, ValueError):
        target_id = 0

    if not target_id:
        return JsonResponse({"success": False, "message": "Invalid user."}, status=400)

    qs = User.objects.none()
    if user.user_type == 'agent':
        qs = User.objects.filter(Q(agent=user) & Q(user_type__in=['cashier', 'player']))
    elif user.user_type == 'super_agent':
        qs = User.objects.filter(super_agent=user, user_type='agent')
    elif user.user_type == 'master_agent':
        qs = User.objects.filter(
            Q(master_agent=user) | Q(super_agent__master_agent=user)
        ).filter(user_type__in=['super_agent', 'agent']).distinct()
    elif user.user_type == 'account_user':
        qs = User.objects.filter(user_type__in=['master_agent', 'super_agent', 'agent', 'cashier'])

    if not qs.filter(id=target_id).exists():
        return JsonResponse({"success": False, "message": "Not authorized."}, status=403)

    balance = Wallet.objects.filter(user_id=target_id).values_list("balance", flat=True).first()
    balance = balance or Decimal("0.00")
    return JsonResponse({"success": True, "balance": float(balance)})


@login_required
@user_passes_test(lambda u: u.is_superuser or u.user_type in ['admin', 'account_user'])
def api_admin_user_search(request):
    """
    API endpoint for Admin to search ANY user (excluding superusers).
    Used for Manual Wallet Manager autocomplete.
    """
    search_term = request.GET.get('q', '')
    page = request.GET.get('page', 1)
    
    # Start with all users except superusers
    queryset = User.objects.exclude(is_superuser=True)
    
    # If account_user, exclude other account_users to match dashboard logic
    if request.user.user_type == 'account_user':
        queryset = queryset.exclude(user_type='account_user')
    
    # Apply search filter
    if search_term:
        qs_filter = (
            Q(email__icontains=search_term) |
            Q(phone_number__icontains=search_term) |
            Q(username__icontains=search_term) |
            Q(first_name__icontains=search_term) |
            Q(last_name__icontains=search_term) |
            Q(other_name__icontains=search_term)
        )
        queryset = queryset.filter(qs_filter)
        if search_term.isdigit():
            extra = User.objects.filter(id=int(search_term)).exclude(is_superuser=True)
            if request.user.user_type == 'account_user':
                extra = extra.exclude(user_type='account_user')
            queryset = (queryset | extra).distinct()
    
    # Pagination
    paginator = Paginator(queryset.order_by('email'), 20)
    
    try:
        users_page = paginator.page(page)
    except PageNotAnInteger:
        users_page = paginator.page(1)
    except EmptyPage:
        users_page = paginator.page(paginator.num_pages)

    results = []
    for u in users_page:
        text = f"{u.get_full_name()} ({u.email})"
        if u.username:
            text += f" @{u.username}"
        if u.phone_number:
            text += f" - {u.phone_number}"
        text += f" [{u.get_user_type_display()}]"
            
        results.append({
            'id': u.id,
            'text': text
        })

    return JsonResponse({
        'results': results,
        'pagination': {
            'more': users_page.has_next()
        }
    })



@login_required
def get_ticket_details_json(request):
    ticket_id = request.GET.get('ticket_id')
    mode = request.GET.get('mode', 'rebet') # 'rebet' or 'reprint'

    if not ticket_id:
        return JsonResponse({'success': False, 'message': 'Ticket ID is required'}, status=400)

    # Permission check: Cashier, Agent, Admin
    if not (request.user.user_type in ['cashier', 'agent', 'admin'] or request.user.is_superuser):
        return JsonResponse({'success': False, 'message': 'Unauthorized'}, status=403)

    try:
        ticket = BetTicket.objects.get(ticket_id=ticket_id)
    except BetTicket.DoesNotExist:
        return JsonResponse({'success': False, 'message': 'Ticket not found'}, status=404)

    selections_data = []
    for sel in ticket.selections.select_related('fixture', 'fixture__betting_period').all():
        fixture = sel.fixture
        
        # Determine Odd Value
        odd_value = sel.odd_selected # Default to stored odd for reprint
        is_active = True
        
        if mode == 'rebet':
            # Use current odd from fixture
            current_odd = None
            bt = sel.bet_type
            
            # Map bet_type to fixture field
            if bt == 'home_win': current_odd = getattr(fixture, 'home_win_odd', None)
            elif bt == 'draw': current_odd = getattr(fixture, 'draw_odd', None)
            elif bt == 'away_win': current_odd = getattr(fixture, 'away_win_odd', None)
            elif bt == 'home_dnb': current_odd = getattr(fixture, 'home_dnb_odd', None)
            elif bt == 'away_dnb': current_odd = getattr(fixture, 'away_dnb_odd', None)
            elif bt == 'over_1_5': current_odd = getattr(fixture, 'over_1_5_odd', None)
            elif bt == 'under_1_5': current_odd = getattr(fixture, 'under_1_5_odd', None)
            elif bt == 'over_2_5': current_odd = getattr(fixture, 'over_2_5_odd', None)
            elif bt == 'under_2_5': current_odd = getattr(fixture, 'under_2_5_odd', None)
            elif bt == 'over_3_5': current_odd = getattr(fixture, 'over_3_5_odd', None)
            elif bt == 'under_3_5': current_odd = getattr(fixture, 'under_3_5_odd', None)
            elif bt == 'btts_yes': current_odd = getattr(fixture, 'btts_yes_odd', None)
            elif bt == 'btts_no': current_odd = getattr(fixture, 'btts_no_odd', None)
            
            if current_odd is not None:
                odd_value = current_odd
            
            # Check if fixture is bettable
            if fixture.status != 'scheduled' or not fixture.is_active:
                is_active = False
                
        selections_data.append({
            'fixture_id': fixture.id,
            'fixture_home_team': fixture.home_team,
            'fixture_away_team': fixture.away_team,
            'fixture_match_date': fixture.match_date.strftime('%Y-%m-%d'),
            'fixture_match_time': fixture.match_time.strftime('%H:%M'),
            'bet_type': sel.bet_type,
            'bet_type_display': sel.bet_type.replace('_', ' ').title(),
            'odd': float(odd_value) if odd_value else 1.0,
            'fixture_period_name': fixture.betting_period.name if fixture.betting_period else '',
            'is_active': is_active
        })

    is_voided = ticket.status in ['cancelled', 'deleted', 'voided']

    data = {
        'ticket_id': ticket.ticket_id,
        'placed_at': ticket.placed_at.strftime('%Y-%m-%d %H:%M'),
        'stake_amount': 0.0 if is_voided else float(ticket.stake_amount),
        'total_odd': 0.0 if is_voided else float(ticket.total_odd),
        'max_winning': 0.0 if is_voided else float(ticket.max_winning),
        'potential_winning': 0.0 if is_voided else float(ticket.potential_winning),
        'bonus_percentage_applied': 0.0 if is_voided else float(ticket.bonus_percentage_applied),
        'bonus_base': ticket.bonus_base,
        'bonus_base_amount': 0.0 if is_voided else float(ticket.bonus_base_amount),
        'bonus_amount': 0.0 if is_voided else float(ticket.bonus_amount),
        'bonus_is_final': False if is_voided else bool(ticket.bonus_is_final),
        'bonus_applied_at': None if is_voided or not ticket.bonus_applied_at else ticket.bonus_applied_at.strftime('%Y-%m-%d %H:%M:%S'),
        'original_selections_count': ticket.original_selections_count or ticket.selections.count(),
        'selections': selections_data,
        'status': ticket.get_status_display(),
        'status_code': ticket.status,
        'bet_type': ticket.bet_type,
        'system_min_count': ticket.system_min_count
    }
    
    return JsonResponse({'success': True, 'ticket': data})

@login_required
def log_ticket_reprint(request):
    if request.method != 'POST':
        return JsonResponse({'success': False, 'message': 'Invalid method'}, status=405)
        
    ticket_id = request.POST.get('ticket_id')
    if not ticket_id:
         return JsonResponse({'success': False, 'message': 'Ticket ID required'}, status=400)
         
    try:
        ticket = BetTicket.objects.get(ticket_id=ticket_id)
        
        # Log activity
        ActivityLog.objects.create(
            user=request.user,
            action_type='REPRINT',
            action=f"Reprinted ticket {ticket_id}",
            affected_object=f"BetTicket: {ticket_id}",
            ip_address=request.META.get('REMOTE_ADDR'),
            path=request.path
        )
        
        return JsonResponse({'success': True})
    except BetTicket.DoesNotExist:
        return JsonResponse({'success': False, 'message': 'Ticket not found'}, status=404)

# WebAuthn Views

import json
import base64
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django.contrib.auth.decorators import login_required
from django.contrib.auth import login
from .webauthn_utils import WebAuthnUtils
from .models import BiometricAuthLog
from fido2.utils import websafe_encode, websafe_decode
from django.core.cache import cache

class BytesEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, bytes):
            return websafe_encode(obj)
        return super().default(obj)

def _client_ip(request):
    xff = request.META.get('HTTP_X_FORWARDED_FOR')
    if xff:
        return xff.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR') or 'unknown'

def _rate_limited(key, limit=5, window=60):
    count = cache.get(key)
    if count is None:
        cache.set(key, 1, window)
        return False
    if count >= limit:
        return True
    try:
        cache.incr(key)
    except:
        cache.set(key, count + 1, window)
    return False

@login_required
@require_POST
def webauthn_register_begin(request):
    rp_id = request.get_host().split(':')[0]
    utils = WebAuthnUtils(rp_id=rp_id)
    try:
        rl_key = f"webauthn:reg:{_client_ip(request)}:{request.user.id}"
        if _rate_limited(rl_key):
            return JsonResponse({'status': 'error', 'message': 'Too many requests'}, status=429)
        options, state = utils.register_begin(request.user)
        request.session['webauthn_reg_state'] = state
        return JsonResponse(dict(options.public_key), encoder=BytesEncoder)
    except Exception as e:
        return JsonResponse({'status': 'error', 'message': str(e)}, status=400)

@login_required
@user_passes_test(lambda u: u.is_superuser or u.user_type == 'admin')
def admin_manual_wallet_manager(request):
    search_form = AccountUserSearchForm()
    action_form = AdminManualWalletForm()
    found_user = None
    search_results = None

    if request.method == 'POST':
        if 'search_user' in request.POST:
            search_form = AccountUserSearchForm(request.POST)
            if search_form.is_valid():
                search_term = search_form.cleaned_data['search_term']
                # Search for any user except superuser
                users = User.objects.filter(
                    Q(email__icontains=search_term) | 
                    Q(phone_number__icontains=search_term) |
                    Q(first_name__icontains=search_term) |
                    Q(last_name__icontains=search_term)
                ).exclude(is_superuser=True)
                
                if search_term.isdigit():
                     # Prioritize exact ID match if search term is a digit (likely from autocomplete)
                     exact_match = User.objects.filter(id=int(search_term)).exclude(is_superuser=True).first()
                     if exact_match:
                         found_user = exact_match
                         users = User.objects.filter(pk=exact_match.pk)
                     else:
                         users = users | User.objects.filter(id=int(search_term)).exclude(is_superuser=True)
                
                if found_user:
                     pass # Already found via exact ID match
                elif users.count() == 1:
                    found_user = users.first()
                    messages.success(request, f"User found: {found_user.get_full_name()} ({found_user.email})")
                elif users.count() > 1:
                    search_results = users
                    messages.warning(request, "Multiple users found. Please select one.")
                else:
                    messages.error(request, "No user found.")

        elif 'perform_action' in request.POST:
            action_form = AdminManualWalletForm(request.POST)
            target_user_id = request.POST.get('target_user_id')
            if target_user_id:
                target_user = get_object_or_404(User, id=target_user_id)
                
                if target_user.is_superuser:
                    messages.error(request, "Operation not allowed on superusers.")
                    return redirect('betting_admin:admin_manual_wallet_manager')

                if action_form.is_valid():
                    action = action_form.cleaned_data['action']
                    amount = action_form.cleaned_data['amount']
                    description = action_form.cleaned_data['description']
                    
                    try:
                        with db_transaction.atomic():
                            target_wallet = Wallet.objects.select_for_update().get(user=target_user)
                            
                            if action == 'credit':
                                target_wallet.balance += amount
                                tx_type = 'manual_credit'
                            elif action == 'debit':
                                if target_wallet.balance < amount:
                                    raise InvalidOperation("User has insufficient funds.")
                                target_wallet.balance -= amount
                                tx_type = 'manual_debit'

                            target_wallet.save()
                            
                            Transaction.objects.create(
                                user=target_user,
                                initiating_user=request.user,
                                transaction_type=tx_type,
                                amount=amount,
                                status='completed',
                                is_successful=True,
                                description=f"Admin Manual {action.title()}: {description}"
                            )
                            
                            log_admin_activity(
                                request, 
                                f"Manual {action} of {amount} for {target_user.email}. Reason: {description}",
                                action_type=f"MANUAL_{action.upper()}",
                                affected_object=target_user.email
                            )
                            messages.success(request, f"Successfully {action}ed ₦{amount} for {target_user.email}.")
                            return redirect('betting_admin:admin_manual_wallet_manager')
                            
                    except InvalidOperation as e:
                        messages.error(request, str(e))
                    except Exception as e:
                        messages.error(request, f"An error occurred: {str(e)}")
            else:
                 messages.error(request, "Target user not specified.")

    # Get recent manual transactions (Admin and Account User)
    recent_transactions = Transaction.objects.filter(
        transaction_type__in=['manual_credit', 'manual_debit', 'account_user_credit', 'account_user_debit']
    ).select_related('user', 'initiating_user').order_by('-timestamp')[:20]

    context = {
        'search_form': search_form,
        'action_form': action_form,
        'found_user': found_user,
        'search_results': search_results,
        'recent_transactions': recent_transactions,
    }
    return render(request, 'betting/admin/manual_wallet_manager.html', context)

@login_required
@require_POST
def webauthn_register_complete(request):
    rp_id = request.get_host().split(':')[0]
    utils = WebAuthnUtils(rp_id=rp_id)
    try:
        data = json.loads(request.body)
        state = request.session.get('webauthn_reg_state')
        if not state:
            return JsonResponse({'status': 'error', 'message': 'No registration state found'}, status=400)
            
        device_name = data.get('device_name', 'Unknown Device')
        
        if 'id' in data:
            data['id'] = websafe_decode(data['id'])
        if 'rawId' in data:
            data['rawId'] = websafe_decode(data['rawId'])
        if 'response' in data:
            resp = data['response']
            if 'clientDataJSON' in resp:
                resp['clientDataJSON'] = websafe_decode(resp['clientDataJSON'])
            if 'attestationObject' in resp:
                resp['attestationObject'] = websafe_decode(resp['attestationObject'])
                
        utils.register_complete(state, data, request.user, device_name)
        
        BiometricAuthLog.objects.create(
            user=request.user,
            action='register',
            status='success',
            ip_address=request.META.get('REMOTE_ADDR'),
            device_name=device_name
        )
        
        del request.session['webauthn_reg_state']
        return JsonResponse({'status': 'success'})
    except Exception as e:
        BiometricAuthLog.objects.create(
            user=request.user,
            action='register',
            status='failed',
            ip_address=request.META.get('REMOTE_ADDR')
        )
        return JsonResponse({'status': 'error', 'message': str(e)}, status=400)

@require_POST
def webauthn_login_begin(request):
    try:
        data = json.loads(request.body)
        email = data.get('email') or data.get('username')
        
        rp_id = request.get_host().split(':')[0]
        utils = WebAuthnUtils(rp_id=rp_id)
        
        user = None
        if email:
            rl_key = f"webauthn:auth:{_client_ip(request)}:{email}"
            if _rate_limited(rl_key):
                return JsonResponse({'status': 'error', 'message': 'Too many requests'}, status=429)
                
            try:
                user = User.objects.get(email=email)
            except User.DoesNotExist:
                 return JsonResponse({'status': 'error', 'message': 'User not found'}, status=404)
                 
            if user.user_type not in ['cashier', 'agent', 'super_agent', 'master_agent', 'admin']:
                 return JsonResponse({'status': 'error', 'message': 'Biometric login not enabled for this role'}, status=403)
                 
            request.session['webauthn_auth_user_id'] = user.id
        else:
            # Usernameless flow
            pass

        options, state = utils.authenticate_begin(user)
        request.session['webauthn_auth_state'] = state
        
        return JsonResponse(dict(options.public_key), encoder=BytesEncoder)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return JsonResponse({'status': 'error', 'message': str(e)}, status=400)

@require_POST
def webauthn_login_complete(request):
    rp_id = request.get_host().split(':')[0]
    utils = WebAuthnUtils(rp_id=rp_id)
    try:
        data = json.loads(request.body)
        state = request.session.get('webauthn_auth_state')
        
        # NOTE: In usernameless flow, user_id might be None
        user_id = request.session.get('webauthn_auth_user_id')
        
        if not state:
             return JsonResponse({'status': 'error', 'message': 'No authentication state found'}, status=400)
             
        user = None
        if user_id:
            try:
                user = User.objects.get(id=user_id)
            except User.DoesNotExist:
                pass
        
        # Decoding handled by fido2 library or manually here if needed.
        # Note: python-fido2 server.authenticate_complete expects decoded bytes for ids
        # But we pass the raw JSON data to utils.authenticate_complete? 
        # Wait, utils.authenticate_complete expects 'response_data' which is usually the JSON.
        # Let's check utils.authenticate_complete implementation again.
        # It calls self.server.authenticate_complete(state, creds_data, response_data).
        # Fido2Server.authenticate_complete expects response_data to be a ClientData object or dict.
        # If it's a dict, it should be the structure returned by navigator.credentials.get() (JSONified).
        # We don't need to manually decode here if we are passing the JSON structure that fido2 expects.
        # However, the previous code was manually decoding. Let's keep it consistent or let utils handle it.
        # Actually, let's look at the previous code: it was decoding 'id', 'rawId', 'clientDataJSON' etc.
        # If we remove that, it might break if utils expects decoded data.
        # But wait, the standard JSON from webauthn is base64url encoded.
        # python-fido2 helpers usually handle this if you use their helpers.
        # But let's stick to the manual decoding if that's what was working (or supposed to work).
        
        if 'id' in data:
            data['id'] = websafe_decode(data['id'])
        if 'rawId' in data:
            data['rawId'] = websafe_decode(data['rawId'])
        if 'response' in data:
            resp = data['response']
            if 'clientDataJSON' in resp:
                resp['clientDataJSON'] = websafe_decode(resp['clientDataJSON'])
            if 'authenticatorData' in resp:
                resp['authenticatorData'] = websafe_decode(resp['authenticatorData'])
            if 'signature' in resp:
                resp['signature'] = websafe_decode(resp['signature'])
            if 'userHandle' in resp and resp['userHandle']:
                resp['userHandle'] = websafe_decode(resp['userHandle'])

        # Now call utils.authenticate_complete which supports user=None
        cred = utils.authenticate_complete(state, data, user)
        
        if cred:
            # If user was None, get it from cred
            if not user:
                user = cred.user
                
            login(request, user, backend='django.contrib.auth.backends.ModelBackend')
            
            BiometricAuthLog.objects.create(
                user=user,
                action='login',
                status='success',
                ip_address=request.META.get('REMOTE_ADDR'),
                device_name=cred.device_name
            )
            
            if 'webauthn_auth_state' in request.session:
                del request.session['webauthn_auth_state']
            if 'webauthn_auth_user_id' in request.session:
                del request.session['webauthn_auth_user_id']
                
            # Determine redirect URL based on user type (similar to login view)
            redirect_url = '/dashboard/'
            if user.user_type == 'agent':
                redirect_url = '/agent/dashboard/'
            elif user.user_type == 'master_agent':
                redirect_url = '/master-agent/dashboard/'
            elif user.user_type == 'super_agent':
                redirect_url = '/super-agent/dashboard/'
            elif user.user_type == 'account_user':
                redirect_url = '/account-user/dashboard/'
            elif user.user_type == 'admin':
                redirect_url = '/admin/'
                
            return JsonResponse({'status': 'success', 'redirect_url': redirect_url})
        else:
            raise ValueError("Authentication failed")

    except Exception as e:
        import traceback
        traceback.print_exc()
        # Try to log failure if we can identify the user
        target_user = None
        if 'user' in locals() and user:
            target_user = user
        elif request.session.get('webauthn_auth_user_id'):
             try:
                target_user = User.objects.get(id=request.session.get('webauthn_auth_user_id'))
             except:
                 pass
        
        if target_user:
             try:
                BiometricAuthLog.objects.create(
                    user=target_user,
                    action='login',
                    status='failed',
                    ip_address=request.META.get('REMOTE_ADDR'),
                    details=str(e)
                )
             except:
                 pass
                 
        return JsonResponse({'status': 'error', 'message': str(e)}, status=400)
