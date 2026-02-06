from django import forms
from django.contrib.auth.forms import AuthenticationForm, PasswordChangeForm as AuthPasswordChangeForm, UserCreationForm as DjangoUserCreationForm, UserChangeForm as DjangoUserChangeForm
from django.core.exceptions import ValidationError
from decimal import Decimal
import random
import string
from django.db import transaction as db_transaction
from django.contrib.auth import get_user_model
from django.utils import timezone
from django.contrib import messages 
from django.db.models import Q 

from .models import User, Fixture, BettingPeriod, Wallet, UserWithdrawal, BetTicket, Transaction, BonusRule, SystemSetting, LoginAttempt, CreditRequest

# Get the custom User model dynamically
CustomUser = get_user_model()

# --- User Registration Form (for Frontend Self-Registration) ---
class UserRegistrationForm(forms.ModelForm):
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
        fields = ['first_name', 'last_name', 'email', 'phone_number', 'shop_address', 'user_type']
        labels = {
            'email': 'Email Address',
            'phone_number': 'Phone Number',
            'shop_address': 'Shop Address',
        }
        widgets = {
            'email': forms.EmailInput(attrs={'class': 'form-control form-control-lg rounded-pill px-4 py-2', 'placeholder': 'Enter your email'}),
            'first_name': forms.TextInput(attrs={'class': 'form-control form-control-lg rounded-pill px-4 py-2', 'placeholder': 'Enter your first name'}),
            'last_name': forms.TextInput(attrs={'class': 'form-control form-control-lg rounded-pill px-4 py-2', 'placeholder': 'Enter your last name'}),
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

        if password and not password2: 
            self.add_error('password2', "This field is required.")
        elif password and password2 and password != password2:
            self.add_error('password2', "Passwords must match.")

        user_type = cleaned_data.get('user_type')
        if user_type == 'agent' and not cleaned_data.get('phone_number'):
            self.add_error('phone_number', "Agents must provide a phone number.")
        
        return cleaned_data

    def save(self, commit=True, request=None):
        with db_transaction.atomic():
            user = super().save(commit=False)
            user.set_password(self.cleaned_data["password"])

            user.master_agent = None
            user.super_agent = None
            user.agent = None
            user.is_staff = False 
            user.is_superuser = False 

            if user.user_type in ['master_agent', 'super_agent', 'agent', 'cashier', 'admin']: 
                user.is_staff = True
            
            if user.user_type == 'admin': 
                user.is_superuser = True
            
            if user.user_type == 'agent' and not user.cashier_prefix:
                while True:
                    random_xxxx = ''.join(random.choices(string.digits, k=4))
                    if not CustomUser.objects.filter(cashier_prefix=random_xxxx).exists():
                        user.cashier_prefix = random_xxxx
                        break
            elif user.user_type not in ['cashier', 'agent']: 
                user.cashier_prefix = None
            
            if user.user_type == 'player':
                user.is_staff = False

            if commit:
                user.save()
                # Wallet is created by post_save signal in signals.py
                Wallet.objects.get_or_create(user=user, defaults={'balance': Decimal('0.00')})

                if user.user_type == 'agent':
                    # Use self.request if available, otherwise fallback to the passed request
                    current_request_for_messages = self.request or request 
                    for i in range(1, 3): 
                        base_cashier_email = f"{user.cashier_prefix}-CSH-{i:02d}"
                        cashier_email = f"{base_cashier_email}@cashier.com"
                        cashier_prefix_for_cashier = f"{user.cashier_prefix}-{i:02d}"

                        if not CustomUser.objects.filter(email=cashier_email).exists():
                            cashier_user = CustomUser.objects.create_user(
                                email=cashier_email,
                                password=self.cleaned_data["password"], 
                                first_name=f"Cashier {i} ({user.first_name})",
                                last_name=f"{user.last_name}",
                                user_type='cashier',
                                agent=user, 
                                is_active=True,
                                is_staff=True, 
                                is_superuser=False,
                                cashier_prefix=cashier_prefix_for_cashier 
                            )
                            # Wallet is created by post_save signal
                            Wallet.objects.get_or_create(user=cashier_user, defaults={'balance': Decimal('0.00')})
                            if current_request_for_messages: # Use current_request_for_messages here
                                messages.info(current_request_for_messages, f"Cashier account created: {cashier_email}")
                        else:
                            if current_request_for_messages: # Use current_request_for_messages here
                                messages.warning(current_request_for_messages, f"Cashier account {cashier_email} already exists. Skipping creation.")
                
        return user


# --- Login Form ---
class LoginForm(AuthenticationForm):
    username = forms.CharField(widget=forms.TextInput(attrs={'class': 'form-control form-control-lg rounded-pill px-4 py-2', 'placeholder': 'Email Address'}))
    password = forms.CharField(widget=forms.PasswordInput(attrs={'class': 'form-control form-control-lg rounded-pill px-4 py-2', 'placeholder': 'Password'}))

    def clean(self):
        username = self.cleaned_data.get('username')
        password = self.cleaned_data.get('password')
        
        request = self.request
        ip = request.META.get('REMOTE_ADDR')
        user_agent = request.META.get('HTTP_USER_AGENT', '')

        # Resolve User
        user = None
        if username:
            try:
                user = CustomUser.objects.get(email__iexact=username)
            except CustomUser.DoesNotExist:
                pass
        
        # 1. Check if Account is Locked
        if user and user.is_locked:
            LoginAttempt.objects.create(
                user=user,
                username_attempted=username,
                ip_address=ip,
                user_agent=user_agent,
                status='locked'
            )
            raise forms.ValidationError(
                "Your account has been locked due to multiple failed login attempts. Please contact support or administrator."
            )

        # 2. Attempt Authentication via Super Class
        try:
            # super().clean() calls authenticate() internally. 
            # If successful, self.user_cache is set.
            # If failed, it raises ValidationError.
            cleaned_data = super().clean()
            
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
                    username_attempted=username,
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
                    
                    LoginAttempt.objects.create(
                        user=user,
                        username_attempted=username,
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
                        username_attempted=username,
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
                    username_attempted=username,
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
class ProfileEditForm(forms.ModelForm):
    class Meta:
        model = CustomUser
        fields = ['first_name', 'last_name', 'phone_number', 'shop_address']
        widgets = {
            'first_name': forms.TextInput(attrs={'class': 'form-control rounded-pill'}),
            'last_name': forms.TextInput(attrs={'class': 'form-control rounded-pill'}),
            'phone_number': forms.TextInput(attrs={'class': 'form-control rounded-pill'}),
            'shop_address': forms.TextInput(attrs={'class': 'form-control rounded-pill'}),
        }


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
            self.fields['account_name'].initial = self.user.get_full_name()
            self.fields['account_name'].widget.attrs['readonly'] = True

    def clean_amount(self):
        amount = self.cleaned_data['amount']
        if self.user:
            wallet = Wallet.objects.get(user=self.user)
            if wallet.balance < amount:
                raise forms.ValidationError(f"Insufficient funds. Your balance is ₦{wallet.balance}.")
        return amount


# --- Betting Period Form (Admin) ---
class BettingPeriodForm(forms.ModelForm):
    class Meta:
        model = BettingPeriod
        fields = ['name', 'start_date', 'end_date', 'is_active']
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. Week 1'}),
            'start_date': forms.DateInput(attrs={'type': 'date', 'class': 'form-control'}),
            'end_date': forms.DateInput(attrs={'type': 'date', 'class': 'form-control'}),
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
                recipient_user = User.objects.get(email__iexact=recipient_identifier)
            except User.DoesNotExist:
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

            if transaction_type == 'credit':
                sender_wallet = Wallet.objects.get(user=self.sender_user)
                if sender_wallet.balance < amount:
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
class AdminUserCreationForm(DjangoUserCreationForm):
    # Explicitly define password fields to match clean method and avoid inheritance issues
    password = forms.CharField(
        widget=forms.PasswordInput(attrs={'class': 'form-control'}),
        label="Password"
    )
    password2 = forms.CharField(
        widget=forms.PasswordInput(attrs={'class': 'form-control'}),
        label="Confirm Password"
    )

    USER_TYPE_ADMIN_CHOICES = [
        ('player', 'Player'),
        ('cashier', 'Cashier'),
        ('agent', 'Agent'),
        ('super_agent', 'Super Agent'),
        ('master_agent', 'Master Agent'),
        ('account_user', 'Account User'),
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
            'email', 'password', 'password2', 
            'first_name', 'last_name', 'phone_number', 'shop_address', 'user_type',
            'is_active', 'is_staff', 'is_superuser', 'can_manage_downline_wallets',
            'groups', 'user_permissions', 
            'master_agent', 'super_agent', 'agent', 'cashier_prefix'
        )
        widgets = { 
            'email': forms.EmailInput(attrs={'class': 'form-control'}),
            'password': forms.PasswordInput(attrs={'class': 'form-control'}),
            'password2': forms.PasswordInput(attrs={'class': 'form-control'}),
        }

    def __init__(self, *args, **kwargs):
        self.request = kwargs.pop('request', None)
        super().__init__(*args, **kwargs)
        
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
        if user.user_type == 'agent' and not user.cashier_prefix:
            while True:
                random_xxxx = ''.join(random.choices(string.digits, k=4))
                if not CustomUser.objects.filter(cashier_prefix=random_xxxx).exists():
                    user.cashier_prefix = random_xxxx
                    break

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
            user.save()
            if self.cleaned_data.get('groups'):
                user.groups.set(self.cleaned_data['groups'])
            if self.cleaned_data.get('user_permissions'):
                user.user_permissions.set(self.cleaned_data['user_permissions'])
                
            # Initialize wallet for new user (Signal handles it, but get_or_create is safe)
            Wallet.objects.get_or_create(user=user, defaults={'balance': Decimal('0.00')})

            # Auto-create cashiers for Agent
            if user.user_type == 'agent':
                current_request_for_messages = self.request
                for i in range(1, 3):
                    base_cashier_email = f"{user.cashier_prefix}-CSH-{i:02d}"
                    cashier_email = f"{base_cashier_email}@cashier.com"
                    cashier_prefix_for_cashier = f"{user.cashier_prefix}-{i:02d}"
                    
                    if not CustomUser.objects.filter(email=cashier_email).exists():
                        cashier_user = CustomUser.objects.create_user(
                            email=cashier_email,
                            password=self.cleaned_data["password"],
                            first_name=f"Cashier {i} ({user.first_name})",
                            last_name=f"{user.last_name}",
                            user_type='cashier',
                            agent=user,
                            master_agent=user.master_agent,
                            super_agent=user.super_agent,
                            is_active=True,
                            is_staff=True,
                            is_superuser=False,
                            cashier_prefix=cashier_prefix_for_cashier
                        )
                        # Wallet handled by signal
                        if current_request_for_messages:
                            messages.info(current_request_for_messages, f"Cashier account created: {cashier_email}")

        return user


class AdminUserChangeForm(DjangoUserChangeForm):
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

    def __init__(self, *args, **kwargs):
        self.request = kwargs.pop('request', None)
        super().__init__(*args, **kwargs)

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
                    
        if commit:
            user.save()
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
