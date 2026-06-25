from django import forms
from django.contrib.auth.forms import AuthenticationForm, PasswordChangeForm as AuthPasswordChangeForm, UserCreationForm as DjangoUserCreationForm, UserChangeForm as DjangoUserChangeForm
from django.core.exceptions import ValidationError
from django_ckeditor_5.widgets import CKEditor5Widget
from decimal import Decimal
import random
import string
import re
from django.db import transaction as db_transaction
from django.contrib.auth import get_user_model
from django.utils import timezone
from django.contrib import messages 
from django.db.models import Q 
from django.utils.html import format_html

import logging
from django.conf import settings
from django.core.mail import send_mail, get_connection
from django.contrib.auth.hashers import make_password

from .models import User, Fixture, BettingPeriod, Wallet, UserWithdrawal, BetTicket, Transaction, BonusRule, SystemSetting, LoginAttempt, CreditRequest, State, RetailManagerDashboardNote, DashboardTask, AgentTransferLog, AccountUnlockAppeal, AccountLockAuditLog, CustomerComplaint, CustomerComplaintNote, BulkMessageTemplate, BulkMessageCampaign, SiteConfiguration
from .services.usernames import create_agent_and_cashiers
from .services.email_policy import (
    duplicate_email_details,
    is_truthy,
    log_email_audit,
    normalize_email_value,
    resolve_user_from_identifier,
    sync_agent_cashier_emails,
)
from pending_registration.models import PendingAgentRegistration

# Get the custom User model dynamically
CustomUser = get_user_model()
logger = logging.getLogger(__name__)


class DuplicateEmailConfirmationMixin:
    def __init__(self, *args, **kwargs):
        self.request = kwargs.pop("request", getattr(self, "request", None))
        super().__init__(*args, **kwargs)
        self.fields.setdefault("confirm_duplicate_email", forms.CharField(required=False, widget=forms.HiddenInput()))
        self.fields.setdefault("sync_cashier_emails", forms.CharField(required=False, widget=forms.HiddenInput()))
        self.fields.setdefault("email_warning_exclude_id", forms.IntegerField(required=False, widget=forms.HiddenInput()))
        self.fields.setdefault("original_email", forms.CharField(required=False, widget=forms.HiddenInput()))
        self.fields.setdefault("has_linked_cashiers", forms.CharField(required=False, widget=forms.HiddenInput()))
        self._duplicate_email_details = {"exists": False, "matches": [], "count": 0}
        self._email_before_save = normalize_email_value(getattr(getattr(self, "instance", None), "email", ""))
        instance = getattr(self, "instance", None)
        if "email_warning_exclude_id" in self.fields and getattr(instance, "pk", None):
            self.fields["email_warning_exclude_id"].initial = instance.pk
        if "original_email" in self.fields:
            self.fields["original_email"].initial = self._email_before_save
        if "has_linked_cashiers" in self.fields:
            has_linked_cashiers = bool(
                getattr(instance, "pk", None)
                and getattr(instance, "user_type", "") == "agent"
                and User.objects.filter(agent=instance, user_type="cashier").exists()
            )
            self.fields["has_linked_cashiers"].initial = "1" if has_linked_cashiers else "0"

    def _apply_duplicate_email_validation(self, cleaned_data):
        email = normalize_email_value(cleaned_data.get("email"))
        if not email:
            return cleaned_data
        cleaned_data["email"] = email
        exclude_user_id = getattr(getattr(self, "instance", None), "pk", None) or cleaned_data.get("email_warning_exclude_id")
        details = duplicate_email_details(email, exclude_user_id=exclude_user_id)
        self._duplicate_email_details = details
        if details["exists"] and not is_truthy(cleaned_data.get("confirm_duplicate_email")):
            self.add_error("email", "This email is already assigned to another user. Confirm to continue.")
        return cleaned_data

    def _log_duplicate_email_change(self, user):
        if not getattr(user, "pk", None):
            return
        if not self._duplicate_email_details.get("exists"):
            return
        current_email = normalize_email_value(getattr(user, "email", ""))
        action_type = "DUPLICATE_EMAIL_UPDATED" if self._email_before_save and self._email_before_save != current_email else "DUPLICATE_EMAIL_ASSIGNED"
        actor = getattr(getattr(self, "request", None), "user", None)
        if actor and not getattr(actor, "is_authenticated", False):
            actor = None
        log_email_audit(
            action_type=action_type,
            target_user=user,
            email=current_email,
            performed_by=actor or user,
            metadata={"matches": self._duplicate_email_details.get("matches", [])},
        )

    def _maybe_sync_agent_cashiers(self, user):
        if getattr(user, "user_type", "") != "agent":
            return []
        current_email = normalize_email_value(getattr(user, "email", ""))
        if not self._email_before_save or self._email_before_save == current_email:
            return []
        if not is_truthy(self.cleaned_data.get("sync_cashier_emails")):
            return []
        actor = getattr(getattr(self, "request", None), "user", None)
        if actor and not getattr(actor, "is_authenticated", False):
            actor = None
        return sync_agent_cashier_emails(user, current_email, actor=actor or user)


class OddUpdateIndicatorNumberInput(forms.NumberInput):
    def __init__(self, *args, indicator_html="", **kwargs):
        self.indicator_html = indicator_html
        super().__init__(*args, **kwargs)

    def render(self, name, value, attrs=None, renderer=None):
        input_html = super().render(name, value, attrs=attrs, renderer=renderer)
        if not self.indicator_html:
            return input_html
        return format_html(
            '{}<div style="font-size: 11px; line-height: 1.1; margin-top: 2px; white-space: nowrap;">{}</div>',
            input_html,
            self.indicator_html,
        )

# --- User Registration Form (for Frontend Self-Registration) ---
class UserRegistrationForm(DuplicateEmailConfirmationMixin, forms.ModelForm):
    # Using 'password' and 'password2' for consistency with Django's auth forms
    password = forms.CharField(
        widget=forms.PasswordInput(attrs={'class': 'form-control form-control-lg rounded-pill px-4 py-2'}),
        label="Password"
    )
    password2 = forms.CharField( 
        widget=forms.PasswordInput(attrs={'class': 'form-control form-control-lg rounded-pill px-4 py-2'}),
        label="Confirm Password"
    )

    # Only allow 'player' and 'agent' for self-registration
    user_type = forms.ChoiceField(
        choices=[
            ('player', 'Player'),
            ('agent', 'Agent'),
        ],
        widget=forms.RadioSelect(attrs={'class': 'form-check-input'}), 
        label="Account Type"
    )

    class Meta:
        model = CustomUser
        # Exclude hierarchy fields from frontend registration as they are set by admin
        fields = ['first_name', 'last_name', 'other_name', 'email', 'state', 'phone_number', 'shop_address', 'user_type']
        labels = {
            'email': 'Email Address',
            'phone_number': 'Phone Number',
            'shop_address': 'Shop Address',
            'other_name': 'Other Name',
            'state': 'State',
        }
        widgets = {
            'email': forms.EmailInput(attrs={'class': 'form-control form-control-lg rounded-pill px-4 py-2', 'placeholder': 'Enter your email'}),
            'first_name': forms.TextInput(attrs={'class': 'form-control form-control-lg rounded-pill px-4 py-2', 'placeholder': 'Enter your first name'}),
            'last_name': forms.TextInput(attrs={'class': 'form-control form-control-lg rounded-pill px-4 py-2', 'placeholder': 'Enter your last name'}),
            'other_name': forms.TextInput(attrs={'class': 'form-control form-control-lg rounded-pill px-4 py-2', 'placeholder': 'Enter your other name'}),
            'state': forms.Select(attrs={'class': 'form-select form-select-lg'}),
            'phone_number': forms.TextInput(attrs={'class': 'form-control form-control-lg rounded-pill px-4 py-2', 'placeholder': 'e.g. +234XXXXXXXXXX'}),
            'shop_address': forms.TextInput(attrs={'class': 'form-control form-control-lg rounded-pill px-4 py-2', 'placeholder': 'Enter your shop address (Optional)'}),
        }

    def __init__(self, *args, **kwargs):
        # Pop the 'request' argument before calling the superclass's __init__
        self.request = kwargs.pop('request', None)
        super().__init__(*args, **kwargs)

    def clean_password2(self): 
        password = self.cleaned_data.get('password')
        password2 = self.cleaned_data.get('password2')

        if password and password2 and password != password2:
            raise ValidationError("Passwords don't match")
        return password2

    def clean(self):
        cleaned_data = super().clean()
        password = cleaned_data.get("password")
        password2 = cleaned_data.get("password2")
        email = cleaned_data.get("email")

        if password and not password2: 
            self.add_error('password2', "This field is required.")
        elif password and password2 and password != password2:
            self.add_error('password2', "Passwords must match.")

        user_type = cleaned_data.get('user_type')
        if user_type == 'agent' and not cleaned_data.get('phone_number'):
            self.add_error('phone_number', "Agents must provide a phone number.")
        if user_type == 'agent':
            if not cleaned_data.get('first_name'):
                self.add_error('first_name', "First Name is required.")
            if not cleaned_data.get('last_name'):
                self.add_error('last_name', "Last Name is required.")
            if not cleaned_data.get('other_name'):
                self.add_error('other_name', "Other Name is required.")
            if not cleaned_data.get('state'):
                self.add_error('state', "State is required.")

        return self._apply_duplicate_email_validation(cleaned_data)

    def save(self, commit=True, request=None):
        user_type = self.cleaned_data.get('user_type')
        password = self.cleaned_data["password"]

        if user_type == 'agent':
            state_obj = self.cleaned_data.get('state')
            full_name = f"{(self.cleaned_data.get('first_name') or '').strip()} {(self.cleaned_data.get('last_name') or '').strip()} {(self.cleaned_data.get('other_name') or '').strip()}".strip()
            PendingAgentRegistration.objects.create(
                full_name=full_name,
                email=self.cleaned_data['email'],
                phone=self.cleaned_data.get('phone_number') or "",
                state=getattr(state_obj, "state_name", None) or getattr(state_obj, "abbreviation", None) or "",
                user_type='agent',
                password=make_password(password),
                registered_by=None,
                status='PENDING',
            )
            return None

        with db_transaction.atomic():
            user = super().save(commit=False)
            user.set_password(password)
            user.user_type = user_type or 'player'
            user.is_staff = False
            user.is_superuser = False
            user.master_agent = None
            user.super_agent = None
            user.agent = None
            if commit:
                user.save()
                Wallet.objects.get_or_create(user=user, defaults={'balance': Decimal('0.00')})
                self._log_duplicate_email_change(user)
            return user


# --- Login Form ---
class LoginForm(AuthenticationForm):
    identifier = forms.CharField(
        label="Username",
        widget=forms.TextInput(attrs={'class': 'form-control form-control-lg rounded-3 px-4 py-2', 'placeholder': 'Enter your username'})
    )
    password = forms.CharField(
        widget=forms.PasswordInput(attrs={'class': 'form-control form-control-lg rounded-3 px-4 py-2', 'placeholder': 'Password'})
    )

    def __init__(self, request=None, *args, **kwargs):
        super().__init__(request=request, *args, **kwargs)
        if 'username' in self.fields:
            self.fields.pop('username')

    def clean(self):
        from django.contrib.auth import authenticate
        from django.contrib.auth import get_user_model

        identifier = self.cleaned_data.get('identifier')
        password = self.cleaned_data.get('password')
        
        request = self.request
        ip = request.META.get('REMOTE_ADDR')
        user_agent = request.META.get('HTTP_USER_AGENT', '')

        UserModel = get_user_model()

        if identifier and "@" in identifier:
            raise forms.ValidationError("Use your username to log in. Email login is not supported.")

        # Resolve User
        user = None
        if identifier:
            user = UserModel.objects.filter(username__iexact=identifier).first()
        
        # 1. Check if Account is Locked
        if user and user.is_locked:
            reason = (user.lock_reason or "").strip()
            if "overdraft/loan obligation" in reason.lower():
                from betting.services.loan_overdraft import reassess_borrower_overdraft_lock_state

                reassess_borrower_overdraft_lock_state(user)
                user.refresh_from_db()
            if user.is_locked:
                lock_message = (
                    (user.lock_reason or "").strip()
                    or "Your account has been locked due to multiple failed login attempts. Please contact support or administrator."
                )
                LoginAttempt.objects.create(
                    user=user,
                    username_attempted=identifier,
                    ip_address=ip,
                    user_agent=user_agent,
                    status='locked'
                )
                raise forms.ValidationError(lock_message)

        # 2. Attempt Authentication
        try:
            authenticated_user = authenticate(request, username=identifier, password=password)
            if not authenticated_user:
                raise forms.ValidationError("Invalid credentials.")

            self.confirm_login_allowed(authenticated_user)
            self.user_cache = authenticated_user
            cleaned_data = self.cleaned_data
            
            # --- SUCCESS CASE ---
            if self.user_cache:
                # Reset failed attempts on success
                if self.user_cache.failed_login_attempts > 0:
                    self.user_cache.failed_login_attempts = 0
                    self.user_cache.last_failed_login = None
                    self.user_cache.save(update_fields=['failed_login_attempts', 'last_failed_login'])
                
                # Log Success
                LoginAttempt.objects.create(
                    user=self.user_cache,
                    username_attempted=identifier,
                    ip_address=ip,
                    user_agent=user_agent,
                    status='success'
                )
            
            return cleaned_data

        except forms.ValidationError as e:
            # --- FAILURE CASE ---
            if user:
                # Increment failed attempts
                user.failed_login_attempts += 1
                user.last_failed_login = timezone.now()
                
                # Requirements: 
                # Max 3 attempts. 
                # On 4th failed attempt -> Lock.
                
                # If attempts became 4 or more
                if user.failed_login_attempts >= 4:
                    user.is_locked = True
                    user.locked_at = timezone.now()
                    user.lock_reason = "Exceeded maximum failed login attempts"
                    user.save()
                    AccountLockAuditLog.objects.create(
                        locked_user=user,
                        lock_reason=user.lock_reason or "",
                        action='locked',
                        remarks='Account locked after repeated failed login attempts.',
                    )
                    
                    LoginAttempt.objects.create(
                        user=user,
                        username_attempted=identifier,
                        ip_address=ip,
                        user_agent=user_agent,
                        status='locked'
                    )
                    
                    # Override the default error message with the Lock message
                    raise forms.ValidationError(
                        "Your account has been locked due to multiple failed login attempts. Please contact support or administrator."
                    )
                else:
                    user.save()
                    LoginAttempt.objects.create(
                        user=user,
                        username_attempted=identifier,
                        ip_address=ip,
                        user_agent=user_agent,
                        status='failed'
                    )
                    
                    attempts_remaining = 4 - user.failed_login_attempts
                    
                    # Raise error with attempts remaining
                    raise forms.ValidationError(
                        f"Invalid credentials. You have {attempts_remaining} attempts remaining."
                    )
            else:
                # User not found (but we log the attempt)
                LoginAttempt.objects.create(
                    user=None,
                    username_attempted=identifier,
                    ip_address=ip,
                    user_agent=user_agent,
                    status='failed'
                )
            
            # Re-raise the original error (or our custom one if we hadn't raised above)
            # But since we handle the user case above, this is mostly for non-existent users
            raise e



# --- Password Change Form ---
class PasswordChangeForm(AuthPasswordChangeForm):
    # Add custom styling if needed
    pass


# --- Profile Edit Form ---
class ProfileEditForm(DuplicateEmailConfirmationMixin, forms.ModelForm):
    class Meta:
        model = CustomUser
        fields = ['first_name', 'last_name', 'email', 'phone_number', 'shop_address']
        widgets = {
            'first_name': forms.TextInput(attrs={'class': 'form-control rounded-pill'}),
            'last_name': forms.TextInput(attrs={'class': 'form-control rounded-pill'}),
            'email': forms.EmailInput(attrs={'class': 'form-control rounded-pill'}),
            'phone_number': forms.TextInput(attrs={'class': 'form-control rounded-pill'}),
            'shop_address': forms.TextInput(attrs={'class': 'form-control rounded-pill'}),
        }

    def clean(self):
        cleaned_data = super().clean()
        return self._apply_duplicate_email_validation(cleaned_data)

    def save(self, commit=True):
        user = super().save(commit=commit)
        if commit:
            self._log_duplicate_email_change(user)
            self._maybe_sync_agent_cashiers(user)
        return user

class WithdrawalPinCreateForm(forms.Form):
    pin = forms.CharField(
        widget=forms.PasswordInput(attrs={'class': 'form-control rounded-pill', 'inputmode': 'numeric', 'autocomplete': 'new-password'}),
        label="Enter PIN"
    )
    pin_confirm = forms.CharField(
        widget=forms.PasswordInput(attrs={'class': 'form-control rounded-pill', 'inputmode': 'numeric', 'autocomplete': 'new-password'}),
        label="Confirm PIN"
    )

    def clean(self):
        cleaned = super().clean()
        pin = (cleaned.get('pin') or '').strip()
        pin_confirm = (cleaned.get('pin_confirm') or '').strip()

        if pin != pin_confirm:
            raise ValidationError("PINs do not match.")
        if not re.fullmatch(r"\d{4}|\d{6}", pin or ""):
            raise ValidationError("PIN must be 4-digit or 6-digit numeric.")
        cleaned['pin'] = pin
        cleaned['pin_confirm'] = pin_confirm
        return cleaned

class WithdrawalPinResetForm(forms.Form):
    current_pin = forms.CharField(
        widget=forms.PasswordInput(attrs={'class': 'form-control rounded-pill', 'inputmode': 'numeric', 'autocomplete': 'current-password'}),
        label="Current PIN"
    )
    new_pin = forms.CharField(
        widget=forms.PasswordInput(attrs={'class': 'form-control rounded-pill', 'inputmode': 'numeric', 'autocomplete': 'new-password'}),
        label="New PIN"
    )
    new_pin_confirm = forms.CharField(
        widget=forms.PasswordInput(attrs={'class': 'form-control rounded-pill', 'inputmode': 'numeric', 'autocomplete': 'new-password'}),
        label="Confirm New PIN"
    )

    def __init__(self, *args, **kwargs):
        self.user = kwargs.pop('user', None)
        super().__init__(*args, **kwargs)

    def clean(self):
        cleaned = super().clean()
        current_pin = (cleaned.get('current_pin') or '').strip()
        new_pin = (cleaned.get('new_pin') or '').strip()
        new_pin_confirm = (cleaned.get('new_pin_confirm') or '').strip()

        if not self.user:
            raise ValidationError("User context is required.")

        if not self.user.withdrawal_pin_is_set:
            raise ValidationError("Withdrawal PIN is not set.")

        if not self.user.check_withdrawal_pin(current_pin):
            raise ValidationError("Current PIN is incorrect.")

        if new_pin != new_pin_confirm:
            raise ValidationError("New PINs do not match.")

        if not re.fullmatch(r"\d{4}|\d{6}", new_pin or ""):
            raise ValidationError("PIN must be 4-digit or 6-digit numeric.")

        cleaned['current_pin'] = current_pin
        cleaned['new_pin'] = new_pin
        cleaned['new_pin_confirm'] = new_pin_confirm
        return cleaned


# --- Initiate Deposit Form (for Player) ---
class InitiateDepositForm(forms.Form):
    amount = forms.DecimalField(
        max_digits=12, 
        decimal_places=2, 
        min_value=100.00, # Minimum deposit amount
        widget=forms.NumberInput(attrs={'class': 'form-control rounded-pill', 'placeholder': 'Amount (Min: ₦100.00)'})
    )


# --- Withdraw Funds Form (for Player) ---
class WithdrawFundsForm(forms.Form):
    amount = forms.DecimalField(
        max_digits=12, 
        decimal_places=2, 
        min_value=500.00, # Minimum withdrawal amount
        widget=forms.NumberInput(attrs={'class': 'form-control rounded-pill', 'placeholder': 'Amount (Min: ₦500.00)'})
    )
    account_number = forms.CharField(
        max_length=20,
        widget=forms.TextInput(attrs={'class': 'form-control rounded-pill', 'placeholder': 'Bank Account Number'})
    )
    bank_name = forms.CharField(
        max_length=100,
        widget=forms.TextInput(attrs={'class': 'form-control rounded-pill', 'placeholder': 'Bank Name'})
    )
    account_name = forms.CharField( # Added Account Name
        max_length=100,
        widget=forms.TextInput(attrs={'class': 'form-control rounded-pill', 'placeholder': 'Account Name'})
    )

    def __init__(self, *args, **kwargs):
        self.user = kwargs.pop('user', None)
        super().__init__(*args, **kwargs)
        if self.user:
            self.fields['account_name'].initial = self.user.withdrawal_account_name
            self.fields['account_name'].widget.attrs['readonly'] = True

    def clean_account_name(self):
        if self.user:
            return self.user.withdrawal_account_name
        return (self.cleaned_data.get('account_name') or '').strip()

    def clean_amount(self):
        amount = self.cleaned_data['amount']
        if self.user:
            wallet = Wallet.objects.get(user=self.user)
            if wallet.balance < amount:
                raise forms.ValidationError(f"Insufficient funds. Your balance is ₦{wallet.balance}.")
        return amount

    def clean(self):
        cleaned = super().clean()
        return cleaned


# --- Betting Period Form (Admin) ---
class BettingPeriodForm(forms.ModelForm):
    class Meta:
        model = BettingPeriod
        fields = ['name', 'start_date', 'end_date', 'fixture_theme_color', 'is_active']
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. Week 1'}),
            'start_date': forms.DateInput(attrs={'type': 'date', 'class': 'form-control'}),
            'end_date': forms.DateInput(attrs={'type': 'date', 'class': 'form-control'}),
            'fixture_theme_color': forms.TextInput(attrs={'type': 'color', 'class': 'form-control form-control-color', 'title': 'Choose fixture header color'}),
            'is_active': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        }


# --- Fixture Form (Admin) ---
class FixtureForm(forms.ModelForm):
    # Activeness Checkboxes for Game Options
    active_home_win_odd = forms.BooleanField(required=False, label="Active", initial=True, widget=forms.CheckboxInput(attrs={'class': 'form-check-input'}))
    active_draw_odd = forms.BooleanField(required=False, label="Active", initial=True, widget=forms.CheckboxInput(attrs={'class': 'form-check-input'}))
    active_away_win_odd = forms.BooleanField(required=False, label="Active", initial=True, widget=forms.CheckboxInput(attrs={'class': 'form-check-input'}))
    
    active_over_1_5_odd = forms.BooleanField(required=False, label="Active", initial=True, widget=forms.CheckboxInput(attrs={'class': 'form-check-input'}))
    active_under_1_5_odd = forms.BooleanField(required=False, label="Active", initial=True, widget=forms.CheckboxInput(attrs={'class': 'form-check-input'}))
    
    active_over_2_5_odd = forms.BooleanField(required=False, label="Active", initial=True, widget=forms.CheckboxInput(attrs={'class': 'form-check-input'}))
    active_under_2_5_odd = forms.BooleanField(required=False, label="Active", initial=True, widget=forms.CheckboxInput(attrs={'class': 'form-check-input'}))
    
    active_over_3_5_odd = forms.BooleanField(required=False, label="Active", initial=True, widget=forms.CheckboxInput(attrs={'class': 'form-check-input'}))
    active_under_3_5_odd = forms.BooleanField(required=False, label="Active", initial=True, widget=forms.CheckboxInput(attrs={'class': 'form-check-input'}))
    
    active_btts_yes_odd = forms.BooleanField(required=False, label="Active", initial=True, widget=forms.CheckboxInput(attrs={'class': 'form-check-input'}))
    active_btts_no_odd = forms.BooleanField(required=False, label="Active", initial=True, widget=forms.CheckboxInput(attrs={'class': 'form-check-input'}))
    
    active_home_dnb_odd = forms.BooleanField(required=False, label="Active", initial=True, widget=forms.CheckboxInput(attrs={'class': 'form-check-input'}))
    active_away_dnb_odd = forms.BooleanField(required=False, label="Active", initial=True, widget=forms.CheckboxInput(attrs={'class': 'form-check-input'}))

    class Media:
        js = ('js/admin_fixture_toggle.js',)

    class Meta:
        model = Fixture
        fields = [
            'betting_period', 'match_date', 'match_time', 'home_team', 'away_team', 'serial_number', 'status', 'is_active',
            'home_win_odd', 'draw_odd', 'away_win_odd',
            'over_1_5_odd', 'under_1_5_odd',
            'over_2_5_odd', 'under_2_5_odd',
            'over_3_5_odd', 'under_3_5_odd',
            'btts_yes_odd', 'btts_no_odd',
            'home_dnb_odd', 'away_dnb_odd'
        ]
        widgets = {
            'betting_period': forms.Select(attrs={'class': 'form-control'}),
            'match_date': forms.DateInput(attrs={'type': 'date', 'class': 'form-control'}),
            'match_time': forms.TimeInput(attrs={'type': 'time', 'class': 'form-control'}),
            'home_team': forms.TextInput(attrs={'class': 'form-control'}),
            'away_team': forms.TextInput(attrs={'class': 'form-control'}),
            'serial_number': forms.TextInput(attrs={'class': 'form-control'}),
            'status': forms.Select(attrs={'class': 'form-control'}),
            'is_active': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            
            'home_win_odd': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01'}),
            'draw_odd': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01'}),
            'away_win_odd': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01'}),
            
            'over_1_5_odd': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01'}),
            'under_1_5_odd': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01'}),
            
            'over_2_5_odd': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01'}),
            'under_2_5_odd': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01'}),
            
            'over_3_5_odd': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01'}),
            'under_3_5_odd': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01'}),
            
            'btts_yes_odd': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01'}),
            'btts_no_odd': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01'}),
            
            'home_dnb_odd': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01'}),
            'away_dnb_odd': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        draw_indicator_html = ""
        if self.instance.pk:
            odds_fields = [
                'home_win_odd', 'draw_odd', 'away_win_odd',
                'over_1_5_odd', 'under_1_5_odd',
                'over_2_5_odd', 'under_2_5_odd',
                'over_3_5_odd', 'under_3_5_odd',
                'btts_yes_odd', 'btts_no_odd',
                'home_dnb_odd', 'away_dnb_odd'
            ]
            for field in odds_fields:
                active_field = f"active_{field}"
                if getattr(self.instance, field) is None:
                    self.fields[active_field].initial = False
                else:
                    self.fields[active_field].initial = True

            if self.instance.odds_updated_at:
                if self.instance.odds_update_direction == 'up':
                    draw_indicator_html = format_html(
                        '<span class="text-success fw-bold"><i class="fas fa-caret-up"></i> Odd Updated</span>'
                    )
                elif self.instance.odds_update_direction == 'down':
                    draw_indicator_html = format_html(
                        '<span class="text-danger fw-bold"><i class="fas fa-caret-down"></i> Odd Updated</span>'
                    )
                else:
                    draw_indicator_html = format_html(
                        '<span class="text-warning fw-bold">Odd Updated</span>'
                    )

        self.fields['draw_odd'].widget = OddUpdateIndicatorNumberInput(
            attrs={'class': 'form-control', 'step': '0.01'},
            indicator_html=draw_indicator_html,
        )

        self.order_fields([
            'betting_period', 'match_date', 'match_time', 'home_team', 'away_team', 'serial_number', 'status', 'is_active',
            'active_home_win_odd', 'home_win_odd',
            'active_draw_odd', 'draw_odd',
            'active_away_win_odd', 'away_win_odd',
            'active_over_1_5_odd', 'over_1_5_odd',
            'active_under_1_5_odd', 'under_1_5_odd',
            'active_over_2_5_odd', 'over_2_5_odd',
            'active_under_2_5_odd', 'under_2_5_odd',
            'active_over_3_5_odd', 'over_3_5_odd',
            'active_under_3_5_odd', 'under_3_5_odd',
            'active_btts_yes_odd', 'btts_yes_odd',
            'active_btts_no_odd', 'btts_no_odd',
            'active_home_dnb_odd', 'home_dnb_odd',
            'active_away_dnb_odd', 'away_dnb_odd',
        ])

    def clean(self):
        cleaned_data = super().clean()
        odds_fields = [
            'home_win_odd', 'draw_odd', 'away_win_odd',
            'over_1_5_odd', 'under_1_5_odd',
            'over_2_5_odd', 'under_2_5_odd',
            'over_3_5_odd', 'under_3_5_odd',
            'btts_yes_odd', 'btts_no_odd',
            'home_dnb_odd', 'away_dnb_odd'
        ]
        for field in odds_fields:
            active_field = f"active_{field}"
            if not cleaned_data.get(active_field):
                cleaned_data[field] = None
        return cleaned_data


# --- Declare Result Form (Admin) ---
class DeclareResultForm(forms.Form):
    RESULT_CHOICES = [
        ('home_win', 'Home Win'),
        ('draw', 'Draw'),
        ('away_win', 'Away Win'),
        ('postponed', 'Postponed'),
        ('cancelled', 'Cancelled'),
    ]
    result = forms.ChoiceField(
        choices=RESULT_CHOICES,
        widget=forms.RadioSelect(attrs={'class': 'form-check-input'})
    )
    home_score = forms.IntegerField(required=False, widget=forms.NumberInput(attrs={'class': 'form-control', 'placeholder': 'Home Score'}))
    away_score = forms.IntegerField(required=False, widget=forms.NumberInput(attrs={'class': 'form-control', 'placeholder': 'Away Score'}))


# --- Wallet Transfer Form (Internal Transfers) ---
class WalletTransferForm(forms.Form):
    recipient_identifier = forms.CharField(
        label="Recipient Identifier",
        help_text="Enter Recipient's Email, Phone Number, or Cashier Prefix (e.g., 1234-01)",
        widget=forms.TextInput(attrs={'class': 'form-control rounded-md', 'placeholder': 'Email, Phone, or Cashier Prefix'})
    )
    amount = forms.DecimalField(
        max_digits=12, 
        decimal_places=2, 
        min_value=10.00, 
        widget=forms.NumberInput(attrs={'class': 'form-control rounded-md', 'placeholder': 'Amount'})
    )
    
    # Hidden field to identify transaction type ('credit' or 'debit')
    # Default is 'credit' (Sender sends money to Recipient)
    # 'debit' would be Recipient taking money from Sender (usually restricted)
    transaction_type = forms.CharField(widget=forms.HiddenInput(), initial='credit') 
    description = forms.CharField(
        required=False, 
        widget=forms.Textarea(attrs={'class': 'form-control rounded-md', 'rows': 2, 'placeholder': 'Optional Note'})
    )

    def __init__(self, *args, **kwargs):
        self.sender_user = kwargs.pop('sender_user', None)
        super().__init__(*args, **kwargs)

    def clean(self):
        cleaned_data = super().clean()
        recipient_identifier = cleaned_data.get('recipient_identifier')
        amount = cleaned_data.get('amount')
        transaction_type = cleaned_data.get('transaction_type')

        if not recipient_identifier:
             # Let field validation handle required error
            return cleaned_data
            
        # 1. Resolve Recipient
        recipient_user = None
        
        # New: Try to find by ID (from Select2)
        if recipient_identifier.isdigit():
            try:
                recipient_user = User.objects.get(pk=int(recipient_identifier))
            except User.DoesNotExist:
                pass

        if not recipient_user:
            try:
                # Try to find by Email
                email_matches = list(User.objects.filter(email__iexact=recipient_identifier)[:2])
                if len(email_matches) == 1:
                    recipient_user = email_matches[0]
            except Exception:
                recipient_user = None
            if not recipient_user:
                try:
                    # Try to find by Phone Number
                    recipient_user = User.objects.get(phone_number=recipient_identifier)
                except User.DoesNotExist:
                     try:
                        # Try to find by Cashier Prefix (Exact match for the cashier's full prefix e.g., 1234-01)
                        recipient_user = User.objects.get(cashier_prefix=recipient_identifier)
                     except User.DoesNotExist:
                         pass

        if not recipient_user:
            self.add_error('recipient_identifier', "Recipient not found.")
            return cleaned_data # Stop further checks

        if recipient_user == self.sender_user:
            self.add_error('recipient_identifier', "You cannot transfer funds to yourself.")
            return cleaned_data

        # Store resolved user for view usage
        self.recipient_user = recipient_user
        cleaned_data['recipient_user_obj'] = recipient_user

        # 2. Permission Logic (Who can transfer to whom?)
        # Only implementing 'credit' (Transfer Out) logic for now based on requirements
        # 'debit' logic (e.g., Agent withdrawing from Cashier) can be added later if needed.
        
        has_permission = False

        if self.sender_user.user_type == 'account_user':
            # Account User can credit/debit Master Agents, Super Agents, Agents, Cashiers
            if recipient_user.user_type in ['master_agent', 'super_agent', 'agent', 'cashier']:
                has_permission = True

        elif self.sender_user.user_type == 'master_agent':
            # Master Agent: Super Agents or Agents (depending on hierarchy)
            # Do NOT display: Cashiers, Players
            if recipient_user.user_type in ['super_agent', 'agent']:
                # Check direct relationship or indirect (via Super Agent)
                is_direct = (recipient_user.master_agent == self.sender_user)
                # Ensure super_agent is not None before checking its master_agent
                is_indirect = (
                    recipient_user.super_agent is not None and 
                    recipient_user.super_agent.master_agent == self.sender_user
                )
                
                if is_direct or is_indirect:
                    has_permission = True
        
        elif self.sender_user.user_type == 'super_agent':
            # Super Agent: Directly mapped Agents only
            # Do NOT display: Cashiers, Players
             if recipient_user.user_type == 'agent':
                if recipient_user.super_agent == self.sender_user:
                    has_permission = True

        elif self.sender_user.user_type == 'agent':
            # Agent: Cashiers and Players under the agent
             if recipient_user.user_type in ['cashier', 'player']:
                 if recipient_user.agent == self.sender_user:
                     has_permission = True
        
        if not has_permission:
            self.add_error('recipient_identifier', "Selected recipient is not part of your authorized downline network.")
            return cleaned_data

        # Balance Check
        if amount is not None and recipient_user:
            treat_as_overdraft = (self.data.get('treat_as_overdraft') or '').strip().lower() in {'1', 'true', 'yes', 'on'}

            if transaction_type == 'credit':
                sender_wallet, _ = Wallet.objects.get_or_create(user=self.sender_user, defaults={'balance': Decimal('0.00')})
                overdraft_wallet_balance = Decimal('0.00')
                if (
                    treat_as_overdraft
                    and self.sender_user.user_type == 'super_agent'
                    and recipient_user.user_type == 'agent'
                ):
                    from .services.loan_overdraft import get_or_create_overdraft_wallet

                    overdraft_wallet_balance = get_or_create_overdraft_wallet(self.sender_user).current_balance
                if sender_wallet.balance < amount and overdraft_wallet_balance < amount:
                    self.add_error('amount', "Insufficient balance in your wallet to credit the recipient.")
            elif transaction_type == 'debit':
                recipient_wallet = Wallet.objects.get(user=recipient_user)
                if recipient_wallet.balance < amount:
                    self.add_error('amount', f"Recipient ({recipient_user.email}) has insufficient balance (₦{recipient_wallet.balance}) to debit ₦{amount}.")
        
        return cleaned_data


# --- Bet Ticket Form (for manual bet placement if needed, or validating inputs) ---
class BetTicketForm(forms.Form):
    stake_amount = forms.DecimalField(
        min_value=Decimal('50.00'), # Minimum stake
        max_digits=10,
        decimal_places=2,
        label="Stake Amount",
        widget=forms.NumberInput(attrs={'class': 'form-control', 'placeholder': 'Enter Stake'})
    )
    
    def clean_stake_amount(self):
        stake_amount = self.cleaned_data['stake_amount']
        if stake_amount <= 0:
            raise forms.ValidationError("Stake amount must be positive.")
        return stake_amount


# --- Check Ticket Status Form ---
class CheckTicketStatusForm(forms.Form): 
    ticket_id = forms.CharField( 
        label="Bet Ticket ID",
        max_length=8,
        help_text="Enter the 6-character Ticket ID.",
        widget=forms.TextInput(attrs={'class': 'form-control rounded-md', 'placeholder': 'e.g., A1B2C3'}),
    )

    def clean_ticket_id(self):
        ticket_id = self.cleaned_data['ticket_id']
        try:
            ticket = BetTicket.objects.prefetch_related('selections__fixture').get(ticket_id=ticket_id)
        except BetTicket.DoesNotExist:
            raise forms.ValidationError("Bet Ticket not found.")
        
        self.ticket = ticket 
        return ticket_id


# --- Admin User Forms (for Django Admin Site) ---
class AdminUserCreationForm(DuplicateEmailConfirmationMixin, DjangoUserCreationForm):
    # Explicitly define password fields to match clean method and avoid inheritance issues
    password = forms.CharField(
        widget=forms.PasswordInput(attrs={'class': 'form-control'}),
        label="Password",
        required=False
    )
    password2 = forms.CharField(
        widget=forms.PasswordInput(attrs={'class': 'form-control'}),
        label="Confirm Password",
        required=False
    )

    USER_TYPE_ADMIN_CHOICES = [
        ('player', 'Player'),
        ('cashier', 'Cashier'),
        ('agent', 'Agent'),
        ('super_agent', 'Super Agent'),
        ('master_agent', 'Master Agent'),
        ('retail_manager', 'Retail Manager'),
        ('finance', 'Finance'),
        ('account_user', 'Account User'),
        ('crm', 'CRM'),
        ('admin', 'Admin'), 
    ]
    user_type = forms.ChoiceField(choices=USER_TYPE_ADMIN_CHOICES, initial='player',
                                  widget=forms.Select(attrs={'class': 'form-control'}))
    
    master_agent = forms.ModelChoiceField(queryset=User.objects.filter(user_type='master_agent'), 
                                          required=False, widget=forms.Select(attrs={'class': 'form-control'}))
    super_agent = forms.ModelChoiceField(queryset=User.objects.filter(user_type='super_agent'), 
                                         required=False, widget=forms.Select(attrs={'class': 'form-control'}))
    agent = forms.ModelChoiceField(queryset=User.objects.filter(user_type='agent'), 
                                   required=False, widget=forms.Select(attrs={'class': 'form-control'}))
    cashier_prefix = forms.CharField(max_length=10, required=False, 
                                     widget=forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Cashier Prefix (for cashiers)'}))
    
    first_name = forms.CharField(max_length=100, required=False, widget=forms.TextInput(attrs={'class': 'form-control'}))
    last_name = forms.CharField(max_length=100, required=False, widget=forms.TextInput(attrs={'class': 'form-control'}))
    phone_number = forms.CharField(max_length=20, required=False, widget=forms.TextInput(attrs={'class': 'form-control'}))
    shop_address = forms.CharField(max_length=255, required=False, widget=forms.TextInput(attrs={'class': 'form-control'}))
    crm_role = forms.ChoiceField(
        choices=(('', '---------'),) + tuple(getattr(User, 'CRM_ROLE_CHOICES', ())),
        required=False,
        initial='',
        widget=forms.Select(attrs={'class': 'form-control'})
    )
    finance_role = forms.ChoiceField(
        choices=(('', '---------'),) + tuple(getattr(User, 'FINANCE_ROLE_CHOICES', ())),
        required=False,
        initial='',
        widget=forms.Select(attrs={'class': 'form-control'})
    )

    can_manage_downline_wallets = forms.BooleanField(
        required=False, 
        initial=True,
        label="Can Manage Downline Wallets",
        help_text="Designates whether this agent can credit/debit downline wallets.",
        widget=forms.CheckboxInput(attrs={'class': 'form-check-input'})
    )

    class Meta:
        model = CustomUser
        fields = (
            'email', 'username', 'password', 'password2',
            'first_name', 'last_name', 'other_name', 'state', 'phone_number', 'shop_address', 'user_type',
            'crm_role',
            'finance_role',
            'is_active', 'is_staff', 'is_superuser', 'can_manage_downline_wallets',
            'groups', 'user_permissions',
            'master_agent', 'super_agent', 'agent', 'cashier_prefix'
        )
        field_classes = {'username': forms.CharField}
        widgets = {
            'email': forms.EmailInput(attrs={'class': 'form-control'}),
            'username': forms.TextInput(attrs={'class': 'form-control'}),
            'password': forms.PasswordInput(attrs={'class': 'form-control'}),
            'password2': forms.PasswordInput(attrs={'class': 'form-control'}),
        }

    class Media:
        js = ('https://cdn.jsdelivr.net/npm/sweetalert2@11', 'betting/js/duplicate_email_warning.js')

    def __init__(self, *args, **kwargs):
        self.request = kwargs.pop('request', None)
        super().__init__(*args, **kwargs)

        if 'password1' in self.fields:
            del self.fields['password1']
        if 'crm_role' in self.fields:
            self.fields['crm_role'].initial = ''
        if 'finance_role' in self.fields:
            self.fields['finance_role'].initial = ''

    def clean_password2(self):
        password = self.cleaned_data.get("password")
        password2 = self.cleaned_data.get("password2")
        if password and password2 and password != password2:
            raise ValidationError("Passwords do not match.")
        return password2

    def clean(self):
        cleaned_data = super().clean()
        user_type = cleaned_data.get('user_type')
        password = cleaned_data.get('password')
        password2 = cleaned_data.get('password2')
        crm_role = (cleaned_data.get('crm_role') or '').strip()
        finance_role = (cleaned_data.get('finance_role') or '').strip()

        auto_password_roles = {'retail_manager', 'finance', 'account_user', 'crm'}
        if user_type in auto_password_roles and not password and not password2:
            from django.utils.crypto import get_random_string
            generated = get_random_string(12)
            cleaned_data['password'] = generated
            cleaned_data['password2'] = generated
            password = generated
            password2 = generated

        if user_type not in auto_password_roles and (not password or not password2):
            self.add_error('password', "Password is required.")
            self.add_error('password2', "Confirm Password is required.")

        if user_type != 'crm':
            cleaned_data['crm_role'] = ''
        else:
            cleaned_data['crm_role'] = crm_role or 'viewer'

        if user_type != 'finance':
            cleaned_data['finance_role'] = ''
        else:
            cleaned_data['finance_role'] = finance_role

        if user_type == 'super_agent':
            if not cleaned_data.get('master_agent'):
                self.add_error('master_agent', "Master Agent is required for Super Agent creation.")

        if user_type == 'agent':
            if not cleaned_data.get('first_name'):
                self.add_error('first_name', "First Name is required.")
            if not cleaned_data.get('last_name'):
                self.add_error('last_name', "Last Name is required.")
            if not cleaned_data.get('other_name'):
                self.add_error('other_name', "Other Name is required.")
            if not cleaned_data.get('state'):
                self.add_error('state', "State is required.")

        if user_type == 'cashier':
            if not cleaned_data.get('agent'):
                self.add_error('agent', "Agent is required for Cashier creation.")
            if not cleaned_data.get('cashier_prefix'):
                self.add_error('cashier_prefix', "Cashier Prefix is required for Cashier creation.")

        return self._apply_duplicate_email_validation(cleaned_data)

    def save(self, commit=True):
        user = forms.ModelForm.save(self, commit=False)
        user.set_password(self.cleaned_data["password"])

        user_type = self.cleaned_data.get('user_type')
        if user_type:
            user.user_type = user_type

        if user.user_type == 'master_agent':
            user.master_agent = None
            user.super_agent = None
            user.agent = None
        elif user.user_type == 'super_agent':
            user.master_agent = self.cleaned_data.get('master_agent')
            user.super_agent = None
            user.agent = None
        elif user.user_type == 'agent':
            user.master_agent = self.cleaned_data.get('master_agent')
            user.super_agent = self.cleaned_data.get('super_agent')
            user.agent = None
        elif user.user_type == 'cashier':
            user.master_agent = self.cleaned_data.get('master_agent')
            user.super_agent = self.cleaned_data.get('super_agent')
            user.agent = self.cleaned_data.get('agent')
            user.cashier_prefix = self.cleaned_data.get('cashier_prefix')
        else:
            user.master_agent = None
            user.super_agent = None
            user.agent = None
            user.cashier_prefix = None

        user.crm_role = self.cleaned_data.get('crm_role') or user.crm_role
        user.finance_role = self.cleaned_data.get('finance_role') or user.finance_role
        user.is_staff = user.user_type in ['admin', 'master_agent', 'super_agent', 'agent', 'cashier', 'account_user', 'crm', 'retail_manager', 'finance']
        user.is_superuser = user.user_type == 'admin'

        if commit:
            if user.user_type == 'agent':
                state = self.cleaned_data.get('state')
                if not state:
                    raise ValidationError("State is required for agent creation.")
                from django.db import IntegrityError
                from .services.usernames import (
                    generate_agent_username,
                    generate_cashier_usernames,
                )

                user.first_name = self.cleaned_data.get('first_name')
                user.last_name = self.cleaned_data.get('last_name')
                user.other_name = self.cleaned_data.get('other_name')
                user.state = state
                user.phone_number = self.cleaned_data.get('phone_number')
                user.shop_address = self.cleaned_data.get('shop_address')
                user.master_agent = self.cleaned_data.get('master_agent')
                user.super_agent = self.cleaned_data.get('super_agent')
                user.agent = None

                username_value, roots, base_root = generate_agent_username(
                    CustomUser,
                    state.abbreviation,
                    user.first_name or "",
                    user.last_name or "",
                    user.other_name or "",
                )
                user.username = username_value

                user.is_active = self.cleaned_data.get('is_active', True)
                user.is_staff = True
                user.is_superuser = False

                try:
                    user.save()
                except IntegrityError:
                    username_value, roots, base_root = generate_agent_username(
                        CustomUser,
                        state.abbreviation,
                        user.first_name or "",
                        user.last_name or "",
                        user.other_name or "",
                    )
                    user.username = username_value
                    user.save()

                Wallet.objects.get_or_create(user=user, defaults={'balance': Decimal('0.00')})

                cashier1_username, cashier2_username, _cashier_root = generate_cashier_usernames(
                    CustomUser,
                    preferred_root=user.username,
                    roots=roots,
                    base_root=base_root,
                )

                cashier1 = CustomUser.objects.create_user(
                    email=user.email,
                    password=self.cleaned_data.get("password"),
                    username=cashier1_username,
                    first_name=user.first_name,
                    last_name=user.last_name,
                    other_name=user.other_name,
                    state=user.state,
                    user_type='cashier',
                    agent=user,
                    master_agent=user.master_agent,
                    super_agent=user.super_agent,
                    is_active=True,
                    is_staff=True,
                    is_superuser=False,
                )
                cashier2 = CustomUser.objects.create_user(
                    email=user.email,
                    password=self.cleaned_data.get("password"),
                    username=cashier2_username,
                    first_name=user.first_name,
                    last_name=user.last_name,
                    other_name=user.other_name,
                    state=user.state,
                    user_type='cashier',
                    agent=user,
                    master_agent=user.master_agent,
                    super_agent=user.super_agent,
                    is_active=True,
                    is_staff=True,
                    is_superuser=False,
                )

                Wallet.objects.get_or_create(user=cashier1, defaults={'balance': Decimal('0.00')})
                Wallet.objects.get_or_create(user=cashier2, defaults={'balance': Decimal('0.00')})
                self._log_duplicate_email_change(user)

                return user

            user.save()
            if self.cleaned_data.get('groups'):
                user.groups.set(self.cleaned_data['groups'])
            if self.cleaned_data.get('user_permissions'):
                user.user_permissions.set(self.cleaned_data['user_permissions'])

            Wallet.objects.get_or_create(user=user, defaults={'balance': Decimal('0.00')})
            self._log_duplicate_email_change(user)

        return user


class ForgotPasswordForm(forms.Form):
    identifier = forms.CharField(
        label="Username or Email Address",
        widget=forms.TextInput(attrs={'class': 'form-control form-control-lg rounded-pill px-4 py-2', 'placeholder': 'Enter your username or email'})
    )

class ResetPasswordForm(forms.Form):
    password = forms.CharField(
        label="New Password",
        widget=forms.PasswordInput(attrs={'class': 'form-control form-control-lg rounded-pill px-4 py-2', 'placeholder': 'Enter new password'})
    )
    password_confirm = forms.CharField(
        label="Confirm New Password",
        widget=forms.PasswordInput(attrs={'class': 'form-control form-control-lg rounded-pill px-4 py-2', 'placeholder': 'Confirm new password'})
    )

    def clean(self):
        cleaned_data = super().clean()
        password = cleaned_data.get("password")
        password_confirm = cleaned_data.get("password_confirm")

        if password and password_confirm and password != password_confirm:
            raise ValidationError("Passwords do not match.")
        return cleaned_data
        
        # Remove 'password1' field if present (inherited from DjangoUserCreationForm)
        if 'password1' in self.fields:
            del self.fields['password1']
        
        # Adjust queryset based on the current user's hierarchy and permissions
        current_user = self.request.user if self.request and hasattr(self.request, 'user') else None

        if current_user and not current_user.is_superuser:
            # Non-superuser admins can create all types except superuser
            if current_user.user_type == 'admin':
                # Admins cannot create other admins, superusers, or account users directly
                self.fields['user_type'].choices = [c for c in self.fields['user_type'].choices if c[0] not in ['admin', 'superuser', 'account_user']]
            else: # Master agent, Super agent, Agent can only create their direct downline types
                if current_user.user_type == 'master_agent':
                    self.fields['user_type'].choices = [c for c in self.fields['user_type'].choices if c[0] in ['super_agent', 'agent', 'cashier', 'player']]
                    self.fields['master_agent'].initial = current_user.pk

    def clean_password2(self):
        password = self.cleaned_data.get("password")
        password2 = self.cleaned_data.get("password2")
        if password and password2 and password != password2:
            raise ValidationError("Passwords do not match.")
        return password2

    def clean(self):
        cleaned_data = super().clean()
        user_type = cleaned_data.get('user_type')
        if user_type == 'agent':
            if not cleaned_data.get('first_name'):
                self.add_error('first_name', "First Name is required.")
            if not cleaned_data.get('last_name'):
                self.add_error('last_name', "Last Name is required.")
            if not cleaned_data.get('other_name'):
                self.add_error('other_name', "Other Name is required.")
            if not cleaned_data.get('state'):
                self.add_error('state', "State is required.")
        return cleaned_data

    def save(self, commit=True):
        # We bypass DjangoUserCreationForm.save() because it expects 'password1'
        # and instead call ModelForm.save() directly.
        
        user = forms.ModelForm.save(self, commit=False)
        user.set_password(self.cleaned_data["password"])
        
        # Set attributes based on user_type
        user_type = self.cleaned_data.get('user_type')
        if user_type:
            user.user_type = user_type
        
        # Set hierarchy fields
        if user.user_type == 'master_agent':
            user.master_agent = None
            user.super_agent = None
            user.agent = None
        elif user.user_type == 'super_agent':
            user.master_agent = self.cleaned_data.get('master_agent')
            user.super_agent = None
            user.agent = None
        elif user.user_type == 'agent':
            user.master_agent = self.cleaned_data.get('master_agent')
            user.super_agent = self.cleaned_data.get('super_agent')
            user.agent = None
        elif user.user_type == 'cashier':
            user.master_agent = self.cleaned_data.get('master_agent')
            user.super_agent = self.cleaned_data.get('super_agent')
            user.agent = self.cleaned_data.get('agent')
            user.cashier_prefix = self.cleaned_data.get('cashier_prefix')
        else: # Player, Admin, Account User
            user.master_agent = None
            user.super_agent = None
            user.agent = None
            user.cashier_prefix = None
            
        # Set permissions
        # Account User needs is_staff=True to access admin, but is_superuser=False
        user.is_staff = user.user_type in ['admin', 'master_agent', 'super_agent', 'agent', 'cashier', 'account_user']
        user.is_superuser = user.user_type == 'admin'

        if commit:
            if user.user_type == 'agent':
                state = self.cleaned_data.get('state')
                if not state:
                    raise ValidationError("State is required for agent creation.")
                from django.db import IntegrityError
                from .services.usernames import (
                    generate_agent_username,
                    generate_cashier_usernames,
                )

                user.first_name = self.cleaned_data.get('first_name')
                user.last_name = self.cleaned_data.get('last_name')
                user.other_name = self.cleaned_data.get('other_name')
                user.state = state
                user.phone_number = self.cleaned_data.get('phone_number')
                user.shop_address = self.cleaned_data.get('shop_address')
                user.master_agent = self.cleaned_data.get('master_agent')
                user.super_agent = self.cleaned_data.get('super_agent')
                user.agent = None

                username_value, roots, base_root = generate_agent_username(
                    CustomUser,
                    state.abbreviation,
                    user.first_name or "",
                    user.last_name or "",
                    user.other_name or "",
                )
                user.username = username_value

                user.is_active = self.cleaned_data.get('is_active', True)
                user.is_staff = True
                user.is_superuser = False

                try:
                    user.save()
                except IntegrityError:
                    username_value, roots, base_root = generate_agent_username(
                        CustomUser,
                        state.abbreviation,
                        user.first_name or "",
                        user.last_name or "",
                        user.other_name or "",
                    )
                    user.username = username_value
                    user.save()

                if self.cleaned_data.get('groups'):
                    user.groups.set(self.cleaned_data['groups'])
                if self.cleaned_data.get('user_permissions'):
                    user.user_permissions.set(self.cleaned_data['user_permissions'])

                Wallet.objects.get_or_create(user=user, defaults={'balance': Decimal('0.00')})

                cashier1_username, cashier2_username, _ = generate_cashier_usernames(
                    CustomUser,
                    preferred_root=user.username,
                    roots=roots,
                    base_root=base_root,
                )

                cashier1 = CustomUser.objects.create_user(
                    email=user.email,
                    password=self.cleaned_data.get("password"),
                    username=cashier1_username,
                    first_name=user.first_name,
                    last_name=user.last_name,
                    other_name=user.other_name,
                    state=user.state,
                    user_type='cashier',
                    agent=user,
                    master_agent=user.master_agent,
                    super_agent=user.super_agent,
                    is_active=True,
                    is_staff=True,
                    is_superuser=False,
                )
                cashier2 = CustomUser.objects.create_user(
                    email=user.email,
                    password=self.cleaned_data.get("password"),
                    username=cashier2_username,
                    first_name=user.first_name,
                    last_name=user.last_name,
                    other_name=user.other_name,
                    state=user.state,
                    user_type='cashier',
                    agent=user,
                    master_agent=user.master_agent,
                    super_agent=user.super_agent,
                    is_active=True,
                    is_staff=True,
                    is_superuser=False,
                )

                Wallet.objects.get_or_create(user=cashier1, defaults={'balance': Decimal('0.00')})
                Wallet.objects.get_or_create(user=cashier2, defaults={'balance': Decimal('0.00')})
                self._log_duplicate_email_change(user)

                return user

            user.save()
            if self.cleaned_data.get('groups'):
                user.groups.set(self.cleaned_data['groups'])
            if self.cleaned_data.get('user_permissions'):
                user.user_permissions.set(self.cleaned_data['user_permissions'])

            Wallet.objects.get_or_create(user=user, defaults={'balance': Decimal('0.00')})
            self._log_duplicate_email_change(user)

        return user


class AdminUserChangeForm(DuplicateEmailConfirmationMixin, DjangoUserChangeForm):
    password = forms.CharField( 
        widget=forms.PasswordInput(attrs={'class': 'form-control rounded-md'}),
        required=False,
        help_text="Leave blank to keep current password. Enter new password here."
    )

    can_manage_downline_wallets = forms.BooleanField(
        required=False, 
        label="Can Manage Downline Wallets",
        help_text="Designates whether this agent can credit/debit downline wallets.",
        widget=forms.CheckboxInput(attrs={'class': 'form-check-input'})
    )

    withdrawal_pin_new = forms.CharField(
        widget=forms.PasswordInput(attrs={'class': 'form-control rounded-md', 'inputmode': 'numeric', 'autocomplete': 'new-password'}),
        required=False,
        label="New Withdrawal PIN"
    )
    withdrawal_pin_confirm = forms.CharField(
        widget=forms.PasswordInput(attrs={'class': 'form-control rounded-md', 'inputmode': 'numeric', 'autocomplete': 'new-password'}),
        required=False,
        label="Confirm Withdrawal PIN"
    )

    class Media:
        js = ('https://cdn.jsdelivr.net/npm/sweetalert2@11', 'betting/js/duplicate_email_warning.js')

    def __init__(self, *args, **kwargs):
        self.request = kwargs.pop('request', None)
        super().__init__(*args, **kwargs)

    class Meta(DjangoUserChangeForm.Meta):
        field_classes = {'username': forms.CharField}

    def clean(self):
        cleaned = super().clean()
        new_pin = (cleaned.get('withdrawal_pin_new') or '').strip()
        confirm_pin = (cleaned.get('withdrawal_pin_confirm') or '').strip()

        if new_pin or confirm_pin:
            if new_pin != confirm_pin:
                raise ValidationError("Withdrawal PINs do not match.")
            if not re.fullmatch(r"\d{4}|\d{6}", new_pin or ""):
                raise ValidationError("Withdrawal PIN must be 4-digit or 6-digit numeric.")
            cleaned['withdrawal_pin_new'] = new_pin
            cleaned['withdrawal_pin_confirm'] = confirm_pin

        return self._apply_duplicate_email_validation(cleaned)

    def save(self, commit=True):
        user = super().save(commit=False)
        password = self.cleaned_data.get('password')
        if password:
            user.set_password(password)
        else:
            # If password is not provided, restore the original password
            # because super().save() overwrites it with the empty string from the form
            if user.pk:
                try:
                    original_user = CustomUser.objects.get(pk=user.pk)
                    user.password = original_user.password
                except CustomUser.DoesNotExist:
                    pass
        new_pin = (self.cleaned_data.get('withdrawal_pin_new') or '').strip()
        if new_pin:
            user.set_withdrawal_pin(new_pin)
            user.withdrawal_attempts = 0
            user.withdrawal_locked = False
            user.withdrawal_locked_at = None
                    
        if commit:
            user.save()
            self._log_duplicate_email_change(user)
            self._maybe_sync_agent_cashiers(user)
        return user

class UserWithUsernameChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        full_name = f"{obj.first_name or ''} {obj.last_name or ''}".strip()
        user_type_display = obj.get_user_type_display()
        if full_name:
            return f"{full_name} ({obj.email}) - {user_type_display}"
        return f"{obj.email} - {user_type_display}"

class CreditRequestForm(forms.ModelForm):
    recipient = UserWithUsernameChoiceField(
        queryset=User.objects.none(),
        required=False,
        widget=forms.Select(attrs={'class': 'form-select rounded-pill'})
    )

    class Meta:
        model = CreditRequest
        fields = ['amount', 'reason', 'request_type', 'recipient']
        widgets = {
            'amount': forms.NumberInput(attrs={'class': 'form-control rounded-pill', 'placeholder': 'Amount'}),
            'reason': forms.Textarea(attrs={'class': 'form-control rounded-3', 'rows': 3, 'placeholder': 'Reason for request'}),
            'request_type': forms.Select(attrs={'class': 'form-select rounded-pill'}),
        }

    def __init__(self, *args, **kwargs):
        user = kwargs.pop('user', None)
        super().__init__(*args, **kwargs)
        self.fields['request_type'].choices = [
            ('credit', 'Normal Credit'),
            ('loan', 'Loan'),
        ]
        
        if user:
            # Hierarchy Logic: Restrict recipients to direct Upline only
            if user.user_type == 'cashier':
                # Cashier -> Agent (Direct Upline)
                if user.agent:
                    self.fields['recipient'].queryset = User.objects.filter(pk=user.agent.pk)
                    self.fields['recipient'].initial = user.agent
                else:
                    # Fallback if no Agent assigned (orphan cashier?)
                    self.fields['recipient'].queryset = User.objects.none()
                     
            elif user.user_type == 'agent':
                # Agent -> Super Agent (if exists) -> Master Agent (if exists) -> Account User
                qs = User.objects.none()
                if user.super_agent:
                    qs = User.objects.filter(pk=user.super_agent.pk)
                elif user.master_agent:
                    qs = User.objects.filter(pk=user.master_agent.pk)
                else:
                    # Direct to Account User if no intermediaries
                     qs = User.objects.filter(user_type='account_user')
                
                self.fields['recipient'].queryset = qs.distinct()
                if qs.exists() and qs.count() == 1:
                    self.fields['recipient'].initial = qs.first()
                
            elif user.user_type == 'super_agent':
                # Super Agent -> Master Agent (if exists) -> Account User
                qs = User.objects.none()
                if user.master_agent:
                    qs = User.objects.filter(pk=user.master_agent.pk)
                else:
                    qs = User.objects.filter(user_type='account_user')

                self.fields['recipient'].queryset = qs.distinct()
                if qs.exists() and qs.count() == 1:
                    self.fields['recipient'].initial = qs.first()

            elif user.user_type == 'master_agent':
                # Master Agent -> Account User
                self.fields['recipient'].queryset = User.objects.filter(user_type='account_user')
                if self.fields['recipient'].queryset.exists():
                     self.fields['recipient'].initial = self.fields['recipient'].queryset.first()

class LoanSettlementForm(forms.Form):
    SETTLEMENT_CHOICES = (
        ('wallet', 'Wallet Balance'),
        ('deposit', 'Online Deposit'),
    )
    settlement_method = forms.ChoiceField(
        choices=SETTLEMENT_CHOICES,
        widget=forms.RadioSelect(attrs={'class': 'form-check-input'})
    )

    def __init__(self, *args, **kwargs):
        self.request = kwargs.pop('request', None)
        super().__init__(*args, **kwargs)


class OverdraftRequestForm(forms.Form):
    requested_amount = forms.DecimalField(
        max_digits=12,
        decimal_places=2,
        min_value=Decimal("0.01"),
        widget=forms.NumberInput(attrs={"class": "form-control rounded-pill", "placeholder": "Requested amount"}),
    )
    reason = forms.CharField(
        required=False,
        widget=forms.Textarea(
            attrs={
                "class": "form-control rounded-3",
                "rows": 3,
                "placeholder": "Optional reason / note for this overdraft request",
            }
        ),
    )

    def __init__(self, *args, **kwargs):
        self.user = kwargs.pop("user", None)
        super().__init__(*args, **kwargs)

    def clean(self):
        cleaned_data = super().clean()
        user = self.user
        amount = cleaned_data.get("requested_amount")
        if not user:
            raise ValidationError("User context is required.")
        from .services.loan_overdraft import build_qualification_snapshot, user_has_outstanding_loan

        if user.user_type not in ["agent", "super_agent"]:
            raise ValidationError("Only agents and super agents can request overdraft.")
        if user_has_outstanding_loan(user):
            raise ValidationError("Outstanding overdraft must be cleared before a new request can be submitted.")
        snapshot = build_qualification_snapshot(user)
        self.snapshot = snapshot
        if not snapshot.can_submit_now:
            raise ValidationError(snapshot.blockers[0])
        if amount and amount > snapshot.qualified_amount:
            raise ValidationError("Requested amount cannot exceed the qualified loan amount.")
        return cleaned_data


class LoanCenterDecisionForm(forms.Form):
    action = forms.ChoiceField(
        choices=(("approve", "Approve"), ("reject", "Reject")),
        widget=forms.HiddenInput(),
    )
    loan_id = forms.IntegerField(widget=forms.HiddenInput())
    reason = forms.CharField(
        required=False,
        widget=forms.TextInput(
            attrs={
                "class": "form-control form-control-sm",
                "placeholder": "Reason for rejection",
            }
        ),
    )

    def clean(self):
        cleaned_data = super().clean()
        action = (cleaned_data.get("action") or "").strip().lower()
        reason = (cleaned_data.get("reason") or "").strip()
        if action not in {"approve", "reject"}:
            raise ValidationError("Invalid loan action.")
        if action == "reject" and not reason:
            raise ValidationError("Reason for rejection is required.")
        return cleaned_data


class AdminOverdraftWalletFundingForm(forms.Form):
    super_agent = forms.ModelChoiceField(
        queryset=User.objects.filter(user_type="super_agent", is_active=True).order_by("username", "email"),
        widget=forms.Select(attrs={"class": "form-select"}),
        empty_label="Select super agent",
    )
    amount = forms.DecimalField(
        max_digits=14,
        decimal_places=2,
        min_value=Decimal("0.01"),
        widget=forms.NumberInput(attrs={"class": "form-control", "placeholder": "Funding amount"}),
    )
    reason = forms.CharField(
        required=False,
        widget=forms.TextInput(
            attrs={
                "class": "form-control",
                "placeholder": "Optional funding note",
            }
        ),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["super_agent"].label_from_instance = (
            lambda user_obj: (getattr(user_obj, "username", "") or getattr(user_obj, "email", "") or "").strip()
        )


class LoanOverrideUnlockForm(forms.Form):
    loan_id = forms.IntegerField(widget=forms.HiddenInput())
    reason = forms.CharField(
        required=True,
        widget=forms.TextInput(
            attrs={
                "class": "form-control form-control-sm",
                "placeholder": "Reason for override unlock",
            }
        ),
    )

    def clean_reason(self):
        reason = (self.cleaned_data.get("reason") or "").strip()
        if not reason:
            raise ValidationError("Override unlock reason is required.")
        return reason


class LoanOverrideRelockForm(forms.Form):
    loan_id = forms.IntegerField(widget=forms.HiddenInput())
    reason = forms.CharField(
        required=True,
        widget=forms.TextInput(
            attrs={
                "class": "form-control form-control-sm",
                "placeholder": "Reason for re-lock",
            }
        ),
    )

    def clean_reason(self):
        reason = (self.cleaned_data.get("reason") or "").strip()
        if not reason:
            raise ValidationError("Re-lock reason is required.")
        return reason



# Aliases for compatibility with betting/admin.py
UserCreationForm = AdminUserCreationForm 
UserChangeForm = AdminUserChangeForm


# --- Withdrawal Action Form (for admin to approve/reject withdrawals) ---
class WithdrawalActionForm(forms.Form):
    action = forms.ChoiceField(
        choices=[('approve', 'Approve'), ('reject', 'Reject')],
        widget=forms.RadioSelect(attrs={'class': 'form-check-input'})
    )
    reason = forms.CharField( 
        required=False,
        widget=forms.Textarea(attrs={'rows': 3, 'class': 'form-control rounded-md'})
    )

    def clean(self):
        cleaned_data = super().clean()
        action = cleaned_data.get('action')
        reason = cleaned_data.get('reason')

        if action == 'reject' and not reason:
            # You had a 'pass' here. If you want to require a reason for rejection,
            # uncomment the line below. Otherwise, it's fine as is.
            # self.add_error('reason', "Reason is required for rejecting a withdrawal.")
            pass 

        return cleaned_data

class AccountUserSearchForm(forms.Form):
    search_term = forms.CharField(
        label="Search User",
        max_length=100,
        widget=forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Username, Email, Phone, or ID'})
    )

class AccountUserWalletActionForm(forms.Form):
    ACTION_CHOICES = (
        ('credit', 'Credit User'),
        ('debit', 'Debit User'),
    )
    action = forms.ChoiceField(choices=ACTION_CHOICES, widget=forms.RadioSelect(attrs={'class': 'form-check-input'}))
    amount = forms.DecimalField(max_digits=12, decimal_places=2, min_value=0.01, widget=forms.NumberInput(attrs={'class': 'form-control'}))
    description = forms.CharField(widget=forms.Textarea(attrs={'class': 'form-control', 'rows': 3}), required=False)

    def clean(self):
        cleaned_data = super().clean()
        amount = cleaned_data.get('amount')
        if amount and amount <= 0:
            raise ValidationError("Amount must be greater than zero.")
        return cleaned_data

class CRMUserProfileForm(DuplicateEmailConfirmationMixin, forms.ModelForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if 'vip_manager' in self.fields:
            self.fields['vip_manager'].queryset = User.objects.filter(user_type='crm').order_by('email')

    class Meta:
        model = User
        fields = (
            'first_name',
            'last_name',
            'other_name',
            'email',
            'phone_number',
            'state',
            'shop_address',
            'bank_account_name',
            'kyc_status',
            'vip_level',
            'vip_manager',
        )
        widgets = {
            'first_name': forms.TextInput(attrs={'class': 'form-control'}),
            'last_name': forms.TextInput(attrs={'class': 'form-control'}),
            'other_name': forms.TextInput(attrs={'class': 'form-control'}),
            'email': forms.EmailInput(attrs={'class': 'form-control'}),
            'phone_number': forms.TextInput(attrs={'class': 'form-control'}),
            'state': forms.Select(attrs={'class': 'form-control'}),
            'shop_address': forms.TextInput(attrs={'class': 'form-control'}),
            'bank_account_name': forms.TextInput(attrs={'class': 'form-control'}),
            'kyc_status': forms.Select(attrs={'class': 'form-control'}),
            'vip_level': forms.Select(attrs={'class': 'form-control'}),
            'vip_manager': forms.Select(attrs={'class': 'form-control'}),
        }

    def clean(self):
        cleaned_data = super().clean()
        return self._apply_duplicate_email_validation(cleaned_data)

    def save(self, commit=True):
        user = super().save(commit=commit)
        if commit:
            self._log_duplicate_email_change(user)
            self._maybe_sync_agent_cashiers(user)
        return user

class CRMWithdrawalDecisionForm(forms.Form):
    ACTION_CHOICES = (
        ('approve', 'Approve'),
        ('reject', 'Reject'),
    )
    action = forms.ChoiceField(choices=ACTION_CHOICES, widget=forms.Select(attrs={'class': 'form-control'}))
    reason = forms.CharField(required=False, max_length=255, widget=forms.TextInput(attrs={'class': 'form-control'}))
    notes = forms.CharField(required=False, widget=forms.Textarea(attrs={'class': 'form-control', 'rows': 3}))

    def clean(self):
        cleaned_data = super().clean()
        action = cleaned_data.get('action')
        reason = (cleaned_data.get('reason') or '').strip()
        if action == 'reject' and not reason:
            self.add_error('reason', 'Reason is required when rejecting a withdrawal.')
        return cleaned_data


class CustomerComplaintForm(forms.ModelForm):
    class Meta:
        model = CustomerComplaint
        fields = ('user', 'complaint_type', 'subject', 'description', 'priority')
        widgets = {
            'user': forms.Select(attrs={'class': 'form-select'}),
            'complaint_type': forms.Select(attrs={'class': 'form-select'}),
            'subject': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Complaint subject'}),
            'description': forms.Textarea(attrs={'class': 'form-control', 'rows': 4, 'placeholder': 'Describe the complaint'}),
            'priority': forms.Select(attrs={'class': 'form-select'}),
        }

    def __init__(self, *args, **kwargs):
        user_queryset = kwargs.pop('user_queryset', None)
        super().__init__(*args, **kwargs)
        self.fields['user'].queryset = (user_queryset or User.objects.none()).order_by('username', 'email')
        self.fields['user'].label_from_instance = lambda u: f"{u.username or u.email} ({u.get_user_type_display()})"


class CustomerComplaintActionForm(forms.Form):
    complaint_id = forms.IntegerField(widget=forms.HiddenInput())
    status = forms.ChoiceField(choices=CustomerComplaint.STATUS_CHOICES, widget=forms.Select(attrs={'class': 'form-select form-select-sm'}))
    priority = forms.ChoiceField(choices=CustomerComplaint.PRIORITY_CHOICES, widget=forms.Select(attrs={'class': 'form-select form-select-sm'}))
    assigned_to = forms.ModelChoiceField(
        queryset=User.objects.filter(Q(user_type='crm') | Q(user_type='admin')).order_by('email'),
        required=False,
        widget=forms.Select(attrs={'class': 'form-select form-select-sm'}),
    )
    admin_note = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={'class': 'form-control form-control-sm', 'rows': 2, 'placeholder': 'Internal update or resolution note'}),
    )

    def clean_assigned_to(self):
        assigned_to = self.cleaned_data.get('assigned_to')
        if assigned_to and not (assigned_to.is_superuser or assigned_to.user_type in ['crm', 'admin']):
            raise ValidationError('Only CRM/Admin users can be assigned.')
        return assigned_to


class CustomerComplaintNoteForm(forms.ModelForm):
    class Meta:
        model = CustomerComplaintNote
        fields = ('note', 'is_internal')
        widgets = {
            'note': forms.Textarea(attrs={'class': 'form-control form-control-sm', 'rows': 2, 'placeholder': 'Add internal note'}),
            'is_internal': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        }


class BulkMessageTemplateForm(forms.ModelForm):
    class Meta:
        model = BulkMessageTemplate
        fields = ('name', 'category', 'default_channel', 'subject', 'message', 'is_active')
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Template name'}),
            'category': forms.Select(attrs={'class': 'form-select'}),
            'default_channel': forms.Select(attrs={'class': 'form-select'}),
            'subject': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Subject'}),
            'message': forms.Textarea(attrs={'class': 'form-control', 'rows': 4, 'placeholder': 'Template message'}),
            'is_active': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        }


class BulkMessageCampaignForm(forms.ModelForm):
    target_agent_ids = forms.ModelMultipleChoiceField(
        queryset=User.objects.filter(user_type='agent').order_by('username', 'email'),
        required=False,
        widget=forms.SelectMultiple(attrs={'class': 'form-select'}),
    )
    target_users = forms.ModelMultipleChoiceField(
        queryset=User.objects.none(),
        required=False,
        widget=forms.SelectMultiple(attrs={'class': 'form-select'}),
    )
    target_user_ids = forms.CharField(required=False, widget=forms.HiddenInput())
    send_now = forms.BooleanField(required=False, widget=forms.CheckboxInput(attrs={'class': 'form-check-input'}))

    class Meta:
        model = BulkMessageCampaign
        fields = ('template', 'subject', 'message', 'channel', 'target_group', 'schedule_at', 'recurring_pattern')
        widgets = {
            'template': forms.Select(attrs={'class': 'form-select'}),
            'subject': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Subject'}),
            'message': forms.Textarea(attrs={'class': 'form-control', 'rows': 4, 'placeholder': 'Message'}),
            'channel': forms.Select(attrs={'class': 'form-select'}),
            'target_group': forms.Select(attrs={'class': 'form-select'}),
            'schedule_at': forms.DateTimeInput(attrs={'class': 'form-control', 'type': 'datetime-local'}),
            'recurring_pattern': forms.Select(attrs={'class': 'form-select'}),
        }

    def __init__(self, *args, **kwargs):
        agent_queryset = kwargs.pop('agent_queryset', None)
        template_queryset = kwargs.pop('template_queryset', None)
        user_queryset = kwargs.pop('user_queryset', None)
        super().__init__(*args, **kwargs)
        if agent_queryset is not None:
            self.fields['target_agent_ids'].queryset = agent_queryset.order_by('username', 'email')
        if template_queryset is not None:
            self.fields['template'].queryset = template_queryset.order_by('category', 'name')
        if user_queryset is not None:
            self.fields['target_users'].queryset = user_queryset.order_by('username', 'email')
        self.fields['message'].required = False
        self.fields['target_users'].label_from_instance = lambda u: f"{u.username or u.email} ({u.get_user_type_display()})"

    def clean(self):
        cleaned_data = super().clean()
        template = cleaned_data.get('template')
        target_group = cleaned_data.get('target_group')
        target_user_ids_raw = (cleaned_data.get('target_user_ids') or '').strip()
        target_user_ids = []
        if target_user_ids_raw:
            for bit in target_user_ids_raw.split(','):
                bit = bit.strip()
                if bit.isdigit():
                    target_user_ids.append(int(bit))
        target_user_ids.extend(user.id for user in (cleaned_data.get('target_users') or []))
        cleaned_data['target_user_ids_list'] = sorted(set(target_user_ids))

        if template:
            if not cleaned_data.get('subject'):
                cleaned_data['subject'] = template.subject
            if not cleaned_data.get('message'):
                cleaned_data['message'] = template.message
            if not cleaned_data.get('channel'):
                cleaned_data['channel'] = template.default_channel

        if target_group == 'specific_agents' and not cleaned_data.get('target_agent_ids'):
            self.add_error('target_agent_ids', 'Select at least one agent.')
        if target_group == 'custom_users' and not cleaned_data.get('target_user_ids_list'):
            self.add_error('target_users', 'Select at least one user for a custom audience.')
        if not cleaned_data.get('message'):
            self.add_error('message', 'Message is required.')
        return cleaned_data


class CRMThresholdSettingsForm(forms.ModelForm):
    class Meta:
        model = SiteConfiguration
        fields = ('crm_large_deposit_threshold', 'crm_failed_deposit_repeat_threshold')
        widgets = {
            'crm_large_deposit_threshold': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01'}),
            'crm_failed_deposit_repeat_threshold': forms.NumberInput(attrs={'class': 'form-control', 'min': '1'}),
        }


class RetailManagerDashboardNoteForm(forms.ModelForm):
    class Meta:
        model = RetailManagerDashboardNote
        fields = ('content',)
        widgets = {
            'content': CKEditor5Widget(
                attrs={'class': 'django_ckeditor_5'},
                config_name='default',
            ),
        }


class DashboardTaskReportForm(forms.Form):
    task_id = forms.IntegerField(widget=forms.HiddenInput())
    completion_report = forms.CharField(
        label='Completion Report',
        max_length=5000,
        widget=forms.Textarea(
            attrs={
                'class': 'form-control',
                'rows': 4,
                'placeholder': 'Enter the task completion report for admin review.',
            }
        ),
    )

    def __init__(self, *args, task_queryset=None, **kwargs):
        self.task_queryset = task_queryset
        super().__init__(*args, **kwargs)

    def clean(self):
        cleaned_data = super().clean()
        task_id = cleaned_data.get('task_id')
        report = (cleaned_data.get('completion_report') or '').strip()
        queryset = self.task_queryset if self.task_queryset is not None else DashboardTask.objects.all()
        task = queryset.filter(id=task_id).first()
        if task is None:
            raise ValidationError('The selected task is no longer available.')
        if task.status == DashboardTask.STATUS.COMPLETED:
            raise ValidationError('This task has already been completed.')
        if not report:
            raise ValidationError('A completion report is required.')
        cleaned_data['task'] = task
        cleaned_data['completion_report'] = report
        return cleaned_data


class AccountUnlockAppealForm(forms.ModelForm):
    class Meta:
        model = AccountUnlockAppeal
        fields = ('appeal_reason',)
        widgets = {
            'appeal_reason': forms.Textarea(
                attrs={
                    'class': 'form-control',
                    'rows': 4,
                    'placeholder': 'Please explain why this account should be unlocked.',
                }
            ),
        }

    def clean_appeal_reason(self):
        value = (self.cleaned_data.get('appeal_reason') or '').strip()
        if not value:
            raise ValidationError('Appeal reason is required.')
        return value


class AccountUnlockAppealReviewForm(forms.Form):
    ACTION_CHOICES = (
        ('approve', 'Approve Unlock'),
        ('reject', 'Reject Appeal'),
    )

    action = forms.ChoiceField(choices=ACTION_CHOICES)
    admin_comment = forms.CharField(
        required=False,
        widget=forms.Textarea(
            attrs={
                'class': 'form-control',
                'rows': 3,
                'placeholder': 'Enter comment for rejection or review notes.',
            }
        ),
    )

    def clean(self):
        cleaned_data = super().clean()
        action = cleaned_data.get('action')
        admin_comment = (cleaned_data.get('admin_comment') or '').strip()
        if action == 'reject' and not admin_comment:
            self.add_error('admin_comment', 'Admin comment is required when rejecting an appeal.')
        cleaned_data['admin_comment'] = admin_comment
        return cleaned_data


class AgentRemapForm(forms.Form):
    current_super_agent = forms.ModelChoiceField(
        queryset=User.objects.none(),
        label="Current Super Agent",
        widget=forms.Select(attrs={'class': 'form-select'}),
    )
    agents = forms.ModelMultipleChoiceField(
        queryset=User.objects.none(),
        label="Agents",
        required=False,
        widget=forms.CheckboxSelectMultiple,
    )
    destination_super_agent = forms.ModelChoiceField(
        queryset=User.objects.none(),
        label="Transfer To Super Agent",
        widget=forms.Select(attrs={'class': 'form-select'}),
    )
    remarks = forms.CharField(
        label="Reason for transfer",
        required=False,
        widget=forms.Textarea(attrs={'class': 'form-control', 'rows': 3, 'placeholder': 'Optional remarks'}),
    )

    def __init__(self, *args, **kwargs):
        current_super_agent_qs = kwargs.pop('current_super_agent_qs', None)
        destination_super_agent_qs = kwargs.pop('destination_super_agent_qs', None)
        agent_queryset = kwargs.pop('agent_queryset', None)
        super().__init__(*args, **kwargs)

        current_super_agent_qs = current_super_agent_qs or User.objects.filter(user_type='super_agent').order_by('username', 'email')
        destination_super_agent_qs = destination_super_agent_qs or User.objects.filter(user_type='super_agent', is_active=True).order_by('username', 'email')
        self.fields['current_super_agent'].queryset = current_super_agent_qs
        self.fields['destination_super_agent'].queryset = destination_super_agent_qs

        current_super_agent_id = ''
        if self.is_bound:
            current_super_agent_id = (self.data.get('current_super_agent') or '').strip()
        else:
            current_super_agent_id = str(self.initial.get('current_super_agent') or '')

        if agent_queryset is None:
            agent_queryset = User.objects.filter(user_type='agent').select_related('super_agent').order_by('username', 'email')
            if current_super_agent_id.isdigit():
                agent_queryset = agent_queryset.filter(super_agent_id=int(current_super_agent_id))
            else:
                agent_queryset = agent_queryset.none()
        self.fields['agents'].queryset = agent_queryset

        self.fields['current_super_agent'].label_from_instance = self._label_super_agent
        self.fields['destination_super_agent'].label_from_instance = self._label_super_agent
        self.fields['agents'].label_from_instance = self._label_agent

    @staticmethod
    def _label_super_agent(user):
        identifier = (user.username or '').strip() or (user.email or '').strip() or f"user#{user.pk}"
        name = (user.get_full_name() or '').strip()
        return f"{identifier} - {name}" if name and name != identifier else identifier

    @staticmethod
    def _label_agent(user):
        identifier = (user.username or '').strip() or (user.email or '').strip() or f"user#{user.pk}"
        name = (user.get_full_name() or '').strip()
        phone = (user.phone_number or '').strip()
        bits = [identifier]
        if name and name != identifier:
            bits.append(name)
        if phone:
            bits.append(phone)
        return " | ".join(bits)

    def clean_destination_super_agent(self):
        destination = self.cleaned_data.get('destination_super_agent')
        if destination and not destination.is_active:
            raise ValidationError("Cannot transfer to an inactive Super Agent.")
        return destination

    def clean(self):
        cleaned_data = super().clean()
        current_super_agent = cleaned_data.get('current_super_agent')
        destination_super_agent = cleaned_data.get('destination_super_agent')
        agents = cleaned_data.get('agents')

        if current_super_agent and destination_super_agent and current_super_agent.id == destination_super_agent.id:
            self.add_error('destination_super_agent', 'Agent already belongs to this Super Agent.')

        if current_super_agent and agents:
            invalid_ids = [agent.id for agent in agents if agent.super_agent_id != current_super_agent.id]
            if invalid_ids:
                self.add_error('agents', 'One or more selected agents no longer belong to the chosen Current Super Agent.')

        if not agents:
            self.add_error('agents', 'Select at least one agent to transfer.')

        return cleaned_data


class AgentTransferLogForm(forms.ModelForm):
    class Meta:
        model = AgentTransferLog
        fields = ('remarks',)
        widgets = {
            'remarks': forms.Textarea(attrs={'class': 'form-control', 'rows': 3, 'placeholder': 'Optional remarks'}),
        }

class AdminManualWalletForm(forms.Form):
    ACTION_CHOICES = (
        ('credit', 'Credit User'),
        ('debit', 'Debit User'),
    )
    action = forms.ChoiceField(choices=ACTION_CHOICES, widget=forms.RadioSelect(attrs={'class': 'form-check-input'}))
    amount = forms.DecimalField(max_digits=12, decimal_places=2, min_value=0.01, widget=forms.NumberInput(attrs={'class': 'form-control'}))
    description = forms.CharField(widget=forms.Textarea(attrs={'class': 'form-control', 'rows': 3}), required=False)
    is_overdraft_loan = forms.BooleanField(required=False, label="Overdraft / Loan")

    def clean(self):
        cleaned_data = super().clean()
        amount = cleaned_data.get('amount')
        action = cleaned_data.get('action')
        is_overdraft_loan = cleaned_data.get('is_overdraft_loan')
        if amount and amount <= 0:
            raise ValidationError("Amount must be greater than zero.")
        if is_overdraft_loan and action != 'credit':
            raise ValidationError("Overdraft / Loan can only be used with a credit action.")
        return cleaned_data

class FixtureUploadForm(forms.Form):
    betting_period = forms.ModelChoiceField(
        queryset=BettingPeriod.objects.filter(is_active=True),
        required=True,
        label="Select Betting Period",
        widget=forms.Select(attrs={'class': 'form-control'})
    )
    excel_file = forms.FileField(
        label="Upload Excel File",
        help_text="Upload .xlsx or .xls file containing fixtures. Columns: Serial, Home, Away, Draw Odd, Date, Time",
        widget=forms.FileInput(attrs={'class': 'form-control', 'accept': '.xlsx, .xls'})
    )

class SuperAdminFundAccountUserForm(forms.Form):
    account_user = forms.ModelChoiceField(
        queryset=User.objects.filter(user_type='account_user'),
        widget=forms.Select(attrs={'class': 'form-control'}),
        label="Select Account User"
    )
    ACTION_CHOICES = (
        ('credit', 'Credit Wallet'),
        ('debit', 'Debit Wallet'),
    )
    action = forms.ChoiceField(choices=ACTION_CHOICES, widget=forms.RadioSelect(attrs={'class': 'form-check-input'}))
    amount = forms.DecimalField(max_digits=12, decimal_places=2, min_value=0.01, widget=forms.NumberInput(attrs={'class': 'form-control'}))
    description = forms.CharField(widget=forms.Textarea(attrs={'class': 'form-control', 'rows': 3}), required=False)

    def clean(self):
        cleaned_data = super().clean()
        amount = cleaned_data.get('amount')
        if amount and amount <= 0:
            raise ValidationError("Amount must be greater than zero.")
        return cleaned_data


class CashierVoidPermissionForm(forms.Form):
    cashiers = forms.ModelMultipleChoiceField(
        queryset=CustomUser.objects.none(),
        required=False,
        widget=forms.CheckboxSelectMultiple,
    )

    def __init__(self, *args, agent=None, **kwargs):
        super().__init__(*args, **kwargs)
        field = self.fields["cashiers"]
        field.queryset = CustomUser.objects.filter(user_type="cashier", agent=agent).order_by("username", "email")
        field.label_from_instance = lambda u: f"{(u.username or u.email)} • {u.phone_number or '-'}"


class AgentMinStakeOverrideForm(forms.Form):
    min_stake = forms.DecimalField(
        required=False,
        min_value=Decimal("0.00"),
        max_digits=12,
        decimal_places=2,
        widget=forms.NumberInput(attrs={"class": "form-control", "placeholder": "e.g. 200"}),
    )
