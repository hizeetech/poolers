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

from .models import User, Fixture, BettingPeriod, Wallet, UserWithdrawal, BetTicket, Transaction, BonusRule, SystemSetting

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
                Wallet.objects.create(user=user, balance=Decimal('0.00')) 

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
                            Wallet.objects.create(user=cashier_user, balance=Decimal('0.00')) 
                            if current_request_for_messages: # Use current_request_for_messages here
                                messages.info(current_request_for_messages, f"Cashier account created: {cashier_email}")
                        else:
                            if current_request_for_messages: # Use current_request_for_messages here
                                messages.warning(current_request_for_messages, f"Cashier account {cashier_email} already exists. Skipping creation.")
                
        return user

# --- Authentication Form ---
class LoginForm(AuthenticationForm): 
    # Removed explicit username field definition to let AuthenticationForm handle it
    # based on CustomUser.USERNAME_FIELD (which should be 'email')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Update the label and widgets for the 'username' field (which corresponds to email)
        self.fields['username'].label = "Email Address"
        self.fields['username'].widget = forms.EmailInput(
            attrs={'class': 'form-control form-control-lg rounded-pill px-4 py-2', 'autofocus': True, 'placeholder': 'Email'}
        )
        self.fields['password'].widget = forms.PasswordInput(
            attrs={'class': 'form-control form-control-lg rounded-pill px-4 py-2', 'placeholder': 'Password'}
        )


# --- Fixture Form ---
class FixtureForm(forms.ModelForm):
    betting_period = forms.ModelChoiceField(
        queryset=BettingPeriod.objects.filter(is_active=True).order_by('-start_date'),
        empty_label="Select Betting Period",
        required=True,
        widget=forms.Select(attrs={'class': 'form-select rounded-md'}) 
    )

    class Meta:
        model = Fixture
        fields = [
            'betting_period', 'serial_number', 'home_team', 'away_team', 'match_date', 'match_time',
            'home_win_odd', 'draw_odd', 'away_win_odd',
            'over_1_5_odd', 'under_1_5_odd',
            'over_2_5_odd', 'under_2_5_odd',
            'over_3_5_odd', 'under_3_5_odd',
            'btts_yes_odd', 'btts_no_odd',
            'home_dnb_odd', 'away_dnb_odd',
            'is_active',
        ]
        widgets = {
            'match_date': forms.DateInput(attrs={'type': 'date', 'class': 'form-control rounded-md'}),
            'match_time': forms.TimeInput(attrs={'type': 'time', 'class': 'form-control rounded-md'}),
            'serial_number': forms.NumberInput(attrs={'class': 'form-control rounded-md', 'placeholder': 'S/N'}),
            'home_team': forms.TextInput(attrs={'class': 'form-control rounded-md', 'placeholder': 'Home Team Name'}),
            'away_team': forms.TextInput(attrs={'class': 'form-control rounded-md', 'placeholder': 'Away Team Name'}),
            'is_active': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            
            # Odds Widgets
            'home_win_odd': forms.NumberInput(attrs={'class': 'form-control rounded-md', 'step': '0.01'}),
            'draw_odd': forms.NumberInput(attrs={'class': 'form-control rounded-md', 'step': '0.01'}),
            'away_win_odd': forms.NumberInput(attrs={'class': 'form-control rounded-md', 'step': '0.01'}),
            'over_1_5_odd': forms.NumberInput(attrs={'class': 'form-control rounded-md', 'step': '0.01'}),
            'under_1_5_odd': forms.NumberInput(attrs={'class': 'form-control rounded-md', 'step': '0.01'}),
            'over_2_5_odd': forms.NumberInput(attrs={'class': 'form-control rounded-md', 'step': '0.01'}),
            'under_2_5_odd': forms.NumberInput(attrs={'class': 'form-control rounded-md', 'step': '0.01'}),
            'over_3_5_odd': forms.NumberInput(attrs={'class': 'form-control rounded-md', 'step': '0.01'}),
            'under_3_5_odd': forms.NumberInput(attrs={'class': 'form-control rounded-md', 'step': '0.01'}),
            'btts_yes_odd': forms.NumberInput(attrs={'class': 'form-control rounded-md', 'step': '0.01'}),
            'btts_no_odd': forms.NumberInput(attrs={'class': 'form-control rounded-md', 'step': '0.01'}),
            'home_dnb_odd': forms.NumberInput(attrs={'class': 'form-control rounded-md', 'step': '0.01'}),
            'away_dnb_odd': forms.NumberInput(attrs={'class': 'form-control rounded-md', 'step': '0.01'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.fields['betting_period'].queryset.exists():
            self.fields['betting_period'].help_text = "No active betting periods found. Please create one first."
            self.fields['betting_period'].required = False

    def clean(self):
        cleaned_data = super().clean()
        match_date = cleaned_data.get('match_date')
        match_time = cleaned_data.get('match_time')

        if match_date and match_time:
            from django.utils import timezone as django_timezone
            match_datetime = django_timezone.make_aware(django_timezone.datetime.combine(match_date, match_time))
            if match_datetime < django_timezone.now():
                if self.instance and self.instance.status == 'pending' and not self.instance.pk: 
                    self.add_error('match_date', "Cannot create a new pending fixture with a past date/time.")
                    self.add_error('match_time', "Cannot create a new pending fixture with a past date/time.")
                elif not self.instance and match_datetime < django_timezone.now(): 
                     self.add_error('match_date', "Cannot create a new fixture with a past date/time.")
                     self.add_error('match_time', "Cannot create a new fixture with a past date/time.")
        
        return cleaned_data


# --- Declare Result Form ---
class DeclareResultForm(forms.ModelForm):
    result = forms.ChoiceField(
        choices=[
            ('', 'Select Result'),
            ('home_win', 'Home Win'),
            ('draw', 'Draw'),
            ('away_win', 'Away Win'),
            ('over_1_5', 'Over 1.5 Goals'),
            ('under_1_5', 'Under 1.5 Goals'),
            ('over_2_5', 'Over 2.5 Goals'),
            ('under_2_5', 'Under 2.5 Goals'),
            ('over_3_5', 'Over 3.5 Goals'),
            ('under_3_5', 'Under 3.5 Goals'),
            ('btts_yes', 'BTTS Yes'),
            ('btts_no', 'BTTS No'),
            ('home_dnb', 'Home Draw No Bet'),
            ('away_dnb', 'Away Draw No Bet'),
        ],
        required=False,
        widget=forms.Select(attrs={'class': 'form-select rounded-md'})
    )

    class Meta:
        model = Fixture
        fields = ['home_score', 'away_score', 'status']
        widgets = {
            'home_score': forms.NumberInput(attrs={'class': 'form-control rounded-md', 'min': '0'}),
            'away_score': forms.NumberInput(attrs={'class': 'form-control rounded-md', 'min': '0'}),
            'status': forms.Select(attrs={'class': 'form-select rounded-md'}),
        }
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['status'].choices = [
            ('finished', 'Finished'),
            ('settled', 'Settled'),
            ('cancelled', 'Cancelled'),
        ]
        self.fields['home_score'].required = False
        self.fields['away_score'].required = False


    def clean(self):
        cleaned_data = super().clean()
        home_score = cleaned_data.get('home_score')
        away_score = cleaned_data.get('away_score')
        status = cleaned_data.get('status')
        result = cleaned_data.get('result')

        
        if status == 'finished' or status == 'settled':
            if home_score is None or away_score is None:
                self.add_error(None, "Scores are required when status is 'Finished' or 'Settled'.")
            if not result:
                self.add_error(None, "Result is required when status is 'Finished' or 'Settled'.")
            if home_score is not None and home_score < 0:
                self.add_error('home_score', "Scores cannot be negative.")
            if away_score is not None and away_score < 0:
                self.add_error('away_score', "Scores cannot be negative.")
        
        if status == 'cancelled':
            cleaned_data['home_score'] = None
            cleaned_data['away_score'] = None
            cleaned_data['result'] = None 

        if result and (status == 'completed' or status == 'settled') and home_score is not None and away_score is not None:
            if result == 'home_win' and home_score <= away_score:
                self.add_error('result', "Home win requires home score to be greater than away score.")
            elif result == 'away_win' and away_score <= home_score:
                self.add_error('result', "Away win requires away score to be greater than home score.")
            elif result == 'draw' and home_score != away_score:
                self.add_error('result', "Draw requires scores to be equal.")
            elif result == 'over_1_5' and (home_score + away_score) <= 1:
                self.add_error('result', "Over 1.5 goals requires total score > 1.")
            elif result == 'under_1_5' and (home_score + away_score) > 1:
                self.add_error('result', "Under 1.5 goals requires total score <= 1.")
            elif result == 'over_2_5' and (home_score + away_score) <= 2:
                self.add_error('result', "Over 2.5 goals requires total score > 2.")
            elif result == 'under_2_5' and (home_score + away_score) > 2:
                self.add_error('result', "Under 2.5 goals requires total score <= 2.")
            elif result == 'over_3_5' and (home_score + away_score) <= 3:
                self.add_error('result', "Over 3.5 goals requires total score > 3.")
            elif result == 'under_3_5' and (home_score + away_score) > 3:
                self.add_error('result', "Under 3.5 goals requires total score <= 3.")
            elif result == 'btts_yes' and (home_score == 0 or away_score == 0):
                self.add_error('result', "BTTS Yes requires both teams to score.")
            elif result == 'btts_no' and (home_score > 0 and away_score > 0):
                self.add_error('result', "BTTS No requires at least one team not to score.")
            elif result == 'home_dnb' and home_score < away_score:
                self.add_error('result', "Home DNB is lost if away team wins.")
            elif result == 'away_dnb' and away_score < home_score:
                self.add_error('result', "Away DNB is lost if home team wins.")

        return cleaned_data


# --- Betting Period Form ---
class BettingPeriodForm(forms.ModelForm):
    class Meta:
        model = BettingPeriod
        fields = ['name', 'start_date', 'end_date', 'is_active']
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control rounded-md', 'placeholder': 'e.g., Week 1 Betting Period'}),
            'start_date': forms.DateInput(attrs={'type': 'date', 'class': 'form-control rounded-md'}),
            'end_date': forms.DateInput(attrs={'class': 'form-control rounded-md', 'type': 'date'}),
            'is_active': forms.CheckboxInput(attrs={'class': 'form-check-input'})
        }

    def clean(self):
        cleaned_data = super().clean()
        start_date = cleaned_data.get('start_date')
        end_date = cleaned_data.get('end_date')

        if start_date and end_date and start_date > end_date:
            self.add_error('end_date', "End date cannot be before start date.")
        
        # Check for overlapping periods
        if self.instance and self.instance.pk:
            conflicting_periods = BettingPeriod.objects.exclude(pk=self.instance.pk).filter(
                start_date__lte=end_date,
                end_date__gte=start_date
            )
        else:
            conflicting_periods = BettingPeriod.objects.filter(
                start_date__lte=end_date,
                end_date__gte=start_date
            )
        
        if conflicting_periods.exists():
            self.add_error(None, "Betting period overlaps with an existing period.")
        
        return cleaned_data


# --- Paystack Deposit Form ---
class InitiateDepositForm(forms.Form): # Renamed to InitiateDepositForm to match views.py import
    amount = forms.DecimalField(
        max_digits=10,
        decimal_places=2,
        min_value=Decimal('50.00'),
        widget=forms.NumberInput(attrs={'class': 'form-control form-control-lg rounded-pill px-4 py-2', 'placeholder': 'Amount (e.g., 5000.00)', 'step': '0.01'})
    )

    def clean_amount(self):
        amount = self.cleaned_data['amount']
        if amount <= 0:
            raise forms.ValidationError("Amount must be positive.")
        return amount

# --- Withdrawal Request Form ---
class WithdrawFundsForm(forms.ModelForm): # Renamed to WithdrawFundsForm to match views.py import
    class Meta:
        model = UserWithdrawal
        fields = ['amount', 'bank_name', 'account_name', 'account_number']
        widgets = {
            'amount': forms.NumberInput(attrs={'class': 'form-control rounded-md', 'placeholder': 'Amount to withdraw', 'step': '0.01'}),
            'bank_name': forms.TextInput(attrs={'class': 'form-control rounded-md', 'placeholder': 'e.g., Zenith Bank'}),
            'account_name': forms.TextInput(attrs={'class': 'form-control rounded-md', 'placeholder': 'Account Holder Name'}),
            'account_number': forms.TextInput(attrs={'class': 'form-control rounded-md', 'placeholder': 'Account Number'}),
        }

    def clean_amount(self):
        amount = self.cleaned_data['amount']
        if amount <= 0:
            raise forms.ValidationError("Amount must be positive.")
        return amount

    def clean_account_number(self):
        account_number = self.cleaned_data['account_number']
        if not account_number.isdigit() or len(account_number) != 10: 
            raise forms.ValidationError("Account number must be exactly 10 digits.")
        return account_number

    def clean(self):
        cleaned_data = super().clean()
        amount = cleaned_data.get('amount')
        
        # This clean method expects an instance to be present with a user and wallet
        # It's usually called when form is initialized with an instance (e.g., in views)
        if self.instance and hasattr(self.instance, 'user') and self.instance.user:
            if self.instance.user.wallet.balance < amount:
                self.add_error('amount', "Insufficient balance for withdrawal.")
        # If the form is used for a new withdrawal where user is passed separately,
        # balance check might need to be in the view or a custom __init__ method.
        
        return cleaned_data


# --- User Profile Form (for regular users to edit their own profile) ---
class ProfileEditForm(forms.ModelForm): 
    class Meta:
        model = CustomUser
        fields = ['first_name', 'last_name', 'phone_number', 'shop_address']
        widgets = {
            'first_name': forms.TextInput(attrs={'class': 'form-control rounded-md'}),
            'last_name': forms.TextInput(attrs={'class': 'form-control rounded-md'}),
            'phone_number': forms.TextInput(attrs={'class': 'form-control rounded-md'}),
            'shop_address': forms.TextInput(attrs={'class': 'form-control rounded-md'}),
        }
    

# --- Change Password Form ---
class PasswordChangeForm(AuthPasswordChangeForm): 
    old_password = forms.CharField(
        widget=forms.PasswordInput(attrs={'class': 'form-control rounded-md'}), 
        label="Old Password"
    )
    new_password1 = forms.CharField(
        widget=forms.PasswordInput(attrs={'class': 'form-control rounded-md'}), 
        label="New Password"
    )
    new_password2 = forms.CharField(
        widget=forms.PasswordInput(attrs={'class': 'form-control rounded-md'}), 
        label="Confirm New Password"
    )

    def clean(self):
        cleaned_data = super().clean()
        new_password1 = cleaned_data.get('new_password1')
        new_password2 = cleaned_data.get('new_password2')

        if new_password1 and new_password2 and new_password1 != new_password2:
            self.add_error('new_password2', "New passwords do not match.")
        return cleaned_data


# --- Wallet Transfer Form ---
class WalletTransferForm(forms.Form):
    recipient_identifier = forms.CharField(
        max_length=255,
        help_text="Email of the recipient user (e.g., agent, cashier, player in your downline).",
        widget=forms.TextInput(attrs={'class': 'form-control rounded-md', 'placeholder': 'Recipient Email'})
    )
    
    amount = forms.DecimalField(
        max_digits=10,
        decimal_places=2,
        min_value=Decimal('0.01'),
        widget=forms.NumberInput(attrs={'class': 'form-control rounded-md', 'placeholder': 'Amount to transfer', 'step': '0.01'})
    )
    
    TRANSACTION_CHOICES = [
        ('credit', 'Credit (Transfer from your wallet to recipient)'),
        ('debit', 'Debit (Transfer from recipient\'s wallet to yours)'),
    ]
    transaction_type = forms.ChoiceField(
        choices=TRANSACTION_CHOICES,
        widget=forms.Select(attrs={'class': 'form-select rounded-md'}) 
    )
    
    description = forms.CharField(
        max_length=255,
        required=False,
        widget=forms.TextInput(attrs={'class': 'form-control rounded-md', 'placeholder': 'Optional description for the transfer'})
    )

    def __init__(self, *args, **kwargs):
        self.sender_user = kwargs.pop('sender_user', None)
        super().__init__(*args, **kwargs)
        for field_name, field in self.fields.items():
            if isinstance(field.widget, (forms.TextInput, forms.NumberInput, forms.EmailInput, forms.PasswordInput, forms.Textarea, forms.Select)):
                if 'class' not in field.widget.attrs:
                    field.widget.attrs['class'] = 'form-control'

    def clean(self):
        cleaned_data = super().clean()
        amount = cleaned_data.get('amount')
        transaction_type = cleaned_data.get('transaction_type')
        recipient_identifier = cleaned_data.get('recipient_identifier')
        
        recipient_user = None
        if recipient_identifier:
            try:
                recipient_user = CustomUser.objects.get(email=recipient_identifier)
                cleaned_data['recipient_user_obj'] = recipient_user 
            except CustomUser.DoesNotExist:
                self.add_error('recipient_identifier', "Recipient user not found by email.")
                return cleaned_data

        if not self.sender_user:
            raise forms.ValidationError("Sender user not provided to the form initialization.")
        
        if recipient_user and self.sender_user.pk == recipient_user.pk:
            self.add_error('recipient_identifier', "Cannot transfer funds to your own account.")
            return cleaned_data

        # Permission/Hierarchy Check
        has_permission = True # Relaxed permission for now
        # if self.sender_user.is_superuser or self.sender_user.user_type == 'admin':
        #     has_permission = True
        # elif self.sender_user.user_type == 'master_agent':
        #     if recipient_user and recipient_user.user_type in ['super_agent', 'agent', 'cashier', 'player']:
        #         if recipient_user.master_agent == self.sender_user or \
        #            (recipient_user.super_agent and recipient_user.super_agent.master_agent == self.sender_user) or \
        #            (recipient_user.agent and (recipient_user.agent.master_agent == self.sender_user or \
        #                                       (recipient_user.agent.super_agent and recipient_user.agent.super_agent.master_agent == self.sender_user))):
        #             has_permission = True
        # elif self.sender_user.user_type == 'super_agent':
        #     if recipient_user and recipient_user.user_type in ['agent', 'cashier', 'player']:
        #         if recipient_user.super_agent == self.sender_user or \
        #            (recipient_user.agent and recipient_user.agent.super_agent == self.sender_user):
        #             has_permission = True
        # elif self.sender_user.user_type == 'agent':
        #     if recipient_user and recipient_user.user_type in ['cashier', 'player']:
        #         if recipient_user.agent == self.sender_user:
        #             has_permission = True
        
        if not has_permission:
            self.add_error('recipient_identifier', "You do not have permission to transfer funds to this recipient's user type or hierarchy.")
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

# --- Bet Ticket Form ---
class BetTicketForm(forms.Form): # Added BetTicketForm
    fixture_id = forms.IntegerField(
        widget=forms.HiddenInput() # Hidden, value set by JS from selected fixture
    )
    selected_outcome = forms.CharField(
        max_length=50,
        widget=forms.HiddenInput() # Hidden, value set by JS from selected odd type
    )
    stake_amount = forms.DecimalField(
        max_digits=10,
        decimal_places=2,
        min_value=Decimal('50.00'),
        widget=forms.NumberInput(attrs={'class': 'form-control form-control-lg rounded-pill px-4 py-2', 'placeholder': 'Stake Amount (e.g., 100.00)'})
    )

    def clean_fixture_id(self):
        fixture_id = self.cleaned_data['fixture_id']
        try:
            Fixture.objects.get(id=fixture_id)
        except Fixture.DoesNotExist:
            raise forms.ValidationError("Invalid fixture selected.")
        return fixture_id

    def clean_selected_outcome(self):
        selected_outcome = self.cleaned_data['selected_outcome']
        valid_outcomes = [
            'home_win', 'draw', 'away_win',
            'home_dnb', 'away_dnb',
            'over_1_5', 'under_1_5',
            'over_2_5', 'under_2_5',
            'over_3_5', 'under_3_5',
            'btts_yes', 'btts_no'
        ]
        if selected_outcome not in valid_outcomes:
            raise forms.ValidationError("Invalid betting outcome.")
        return selected_outcome

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
            ticket = BetTicket.objects.get(ticket_id=ticket_id)
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


    class Meta: 
        model = CustomUser
        fields = (
            'email', 'password', 'password2', 
            'first_name', 'last_name', 'phone_number', 'shop_address', 'user_type',
            'is_active', 'is_staff', 'is_superuser', 
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
                # Admins cannot create other admins or superusers directly
                self.fields['user_type'].choices = [c for c in self.fields['user_type'].choices if c[0] not in ['admin', 'superuser']]
            else: # Master agent, Super agent, Agent can only create their direct downline types
                if current_user.user_type == 'master_agent':
                    self.fields['user_type'].choices = [c for c in self.fields['user_type'].choices if c[0] in ['super_agent', 'agent', 'cashier', 'player']]
                    self.fields['master_agent'].initial = current_user.pk
                    self.fields['master_agent'].queryset = User.objects.filter(pk=current_user.pk)
                    self.fields['master_agent'].widget.attrs['readonly'] = 'readonly'
                    self.fields['master_agent'].widget.attrs['class'] += ' bg-light'

                    self.fields['super_agent'].queryset = User.objects.filter(master_agent=current_user, user_type='super_agent')
                    self.fields['agent'].queryset = User.objects.filter(Q(super_agent__master_agent=current_user) | Q(master_agent=current_user), user_type='agent')

                elif current_user.user_type == 'super_agent':
                    self.fields['user_type'].choices = [c for c in self.fields['user_type'].choices if c[0] in ['agent', 'cashier', 'player']]
                    self.fields['master_agent'].initial = current_user.master_agent.pk if current_user.master_agent else None
                    self.fields['master_agent'].queryset = User.objects.filter(pk=current_user.master_agent.pk) if current_user.master_agent else User.objects.none()
                    self.fields['master_agent'].widget.attrs['readonly'] = 'readonly'
                    self.fields['master_agent'].widget.attrs['class'] += ' bg-light'

                    self.fields['super_agent'].initial = current_user.pk
                    self.fields['super_agent'].queryset = User.objects.filter(pk=current_user.pk)
                    self.fields['super_agent'].widget.attrs['readonly'] = 'readonly'
                    self.fields['super_agent'].widget.attrs['class'] += ' bg-light'

                    self.fields['agent'].queryset = User.objects.filter(super_agent=current_user, user_type='agent')

                elif current_user.user_type == 'agent':
                    self.fields['user_type'].choices = [c for c in self.fields['user_type'].choices if c[0] in ['cashier', 'player']]
                    
                    self.fields['master_agent'].initial = current_user.master_agent.pk if current_user.master_agent else None
                    self.fields['master_agent'].queryset = User.objects.filter(pk=current_user.master_agent.pk) if current_user.master_agent else User.objects.none()
                    self.fields['master_agent'].widget.attrs['readonly'] = 'readonly'
                    self.fields['master_agent'].widget.attrs['class'] += ' bg-light'

                    self.fields['super_agent'].initial = current_user.super_agent.pk if current_user.super_agent else None
                    self.fields['super_agent'].queryset = User.objects.filter(pk=current_user.super_agent.pk) if current_user.super_agent else User.objects.none()
                    self.fields['super_agent'].widget.attrs['readonly'] = 'readonly'
                    self.fields['super_agent'].widget.attrs['class'] += ' bg-light'

                    self.fields['agent'].initial = current_user.pk
                    self.fields['agent'].queryset = User.objects.filter(pk=current_user.pk)
                    self.fields['agent'].widget.attrs['readonly'] = 'readonly'
                    self.fields['agent'].widget.attrs['class'] += ' bg-light'
                
                else: # Other non-staff types (e.g., player trying to access admin form, or no user)
                    self.fields['user_type'].choices = [('player', 'Player')] # Default to player if no specific hierarchy
                    # Make all hierarchy fields hidden and set to None
                    self.fields['master_agent'].widget = forms.HiddenInput()
                    self.fields['super_agent'].widget = forms.HiddenInput()
                    self.fields['agent'].widget = forms.HiddenInput()
                    self.fields['cashier_prefix'].widget = forms.HiddenInput()


    def clean_password2(self):
        # Override to avoid DjangoUserCreationForm looking for 'password1'
        password = self.cleaned_data.get('password')
        password2 = self.cleaned_data.get('password2')
        if password and password2 and password != password2:
            raise forms.ValidationError("Passwords don't match.")
        return password2

    def clean(self):
        cleaned_data = super().clean()
        password = cleaned_data.get('password')
        password2 = cleaned_data.get('password2')

        if password and password2 and password != password2:
            self.add_error('password2', "Passwords don't match.")
        if password and not password2: 
            self.add_error('password2', "This field is required.")
        if password2 and not password: 
            self.add_error('password', "This field is required.")
        
        user_type = cleaned_data.get('user_type')
        master_agent = cleaned_data.get('master_agent')
        super_agent = cleaned_data.get('super_agent')
        agent = cleaned_data.get('agent')
        cashier_prefix = cleaned_data.get('cashier_prefix')

        current_user = self.request.user if self.request and hasattr(self.request, 'user') else None

        # Hierarchy validation based on selected user_type
        if user_type == 'master_agent':
            if master_agent or super_agent or agent:
                self.add_error(None, "Master Agent cannot be assigned to another agent type.")
            if current_user and not current_user.is_superuser and current_user.user_type != 'admin':
                # Only Superusers or Admins can create Master Agents
                if current_user.user_type != 'master_agent': # Master agent can't create another top-level master agent
                    self.add_error('user_type', "Only Superusers or Admins can create Master Agents.")
                else:
                    self.add_error('user_type', "You cannot create another Master Agent directly under yourself.")
            
        elif user_type == 'super_agent':
            if agent:
                self.add_error(None, "Super Agent cannot be assigned directly to an Agent (only Master Agent).")
            # if not master_agent:
            #     self.add_error('master_agent', "Super Agent must be assigned to a Master Agent.")
            if master_agent and current_user and not current_user.is_superuser and current_user.user_type != 'admin':
                if current_user.user_type == 'master_agent' and master_agent != current_user:
                    self.add_error('master_agent', "You can only assign Super Agents under your own Master Agent account.")
                elif current_user.user_type == 'super_agent': # Super agent cannot create another super agent directly
                    self.add_error('user_type', "You cannot create another Super Agent directly.")
            
        elif user_type == 'agent':
            # if not (super_agent or master_agent):
            #     self.add_error(None, "Agent must be assigned to either a Super Agent or a Master Agent.")
            if super_agent and master_agent and super_agent.master_agent != master_agent:
                self.add_error(None, "If both Super Agent and Master Agent are selected, the Super Agent's Master Agent must match the selected Master Agent.")
            
            if current_user and not current_user.is_superuser and current_user.user_type != 'admin':
                if current_user.user_type == 'master_agent' and master_agent != current_user:
                    self.add_error('master_agent', "You can only assign Agents under your Master Agent account.")
                elif current_user.user_type == 'super_agent' and super_agent != current_user:
                    self.add_error('super_agent', "You can only assign Agents under your Super Agent account.")
                elif current_user.user_type == 'agent':
                    self.add_error(None, "Agents cannot create other agents directly.")

        elif user_type == 'cashier':
            if not (agent or super_agent or master_agent):
                self.add_error(None, "Cashier must be assigned to an Agent, Super Agent, or Master Agent.")
            if agent and (super_agent or master_agent):
                if (super_agent and agent.super_agent != super_agent) or \
                   (master_agent and agent.master_agent != master_agent):
                    self.add_error(None, "Assigned Agent's hierarchy must match selected Super Agent/Master Agent.")
            
            if current_user and not current_user.is_superuser and current_user.user_type != 'admin':
                if current_user.user_type == 'master_agent':
                    is_valid_parent = (master_agent == current_user) or \
                                      (super_agent and super_agent.master_agent == current_user) or \
                                      (agent and (agent.master_agent == current_user or (agent.super_agent and agent.super_agent.master_agent == current_user)))
                    if not is_valid_parent:
                        self.add_error(None, "You can only assign Cashiers within your Master Agent hierarchy.")
                elif current_user.user_type == 'super_agent':
                    is_valid_parent = (super_agent == current_user) or \
                                      (agent and agent.super_agent == current_user)
                    if not is_valid_parent:
                        self.add_error(None, "You can only assign Cashiers within your Super Agent hierarchy.")
                elif current_user.user_type == 'agent':
                    if agent and agent != current_user:
                        self.add_error('agent', "You can only assign Cashiers under your Agent account.")
                elif current_user.user_type == 'player': 
                    self.add_error(None, "Players cannot create cashiers.")

        # Cashier prefix validation
        if user_type == 'cashier' and not cashier_prefix:
            self.add_error('cashier_prefix', "Cashier Prefix is required for Cashier user type.")
        elif user_type != 'cashier' and cashier_prefix:
            self.add_error('cashier_prefix', "Cashier Prefix should only be set for Cashier user type.")

        return cleaned_data

    def save(self, commit=True):
        # Bypass DjangoUserCreationForm.save() entirely because it expects 'password1'
        # Call ModelForm.save directly
        user = forms.ModelForm.save(self, commit=False)
        user.set_password(self.cleaned_data["password"])

        user.user_type = self.cleaned_data['user_type']
        user.first_name = self.cleaned_data.get('first_name')
        user.last_name = self.cleaned_data.get('last_name')
        user.phone_number = self.cleaned_data.get('phone_number')
        user.shop_address = self.cleaned_data.get('shop_address')
        
        # Explicitly set hierarchy fields to None if they are not applicable or not provided
        user.master_agent = None
        user.super_agent = None
        user.agent = None

        if user.user_type == 'super_agent':
            user.master_agent = self.cleaned_data.get('master_agent')
        elif user.user_type == 'agent':
            user.master_agent = self.cleaned_data.get('master_agent')
            user.super_agent = self.cleaned_data.get('super_agent')
        elif user.user_type == 'cashier':
            user.master_agent = self.cleaned_data.get('master_agent')
            user.super_agent = self.cleaned_data.get('super_agent')
            user.agent = self.cleaned_data.get('agent')
        
        if user.user_type == 'cashier':
            user.cashier_prefix = self.cleaned_data.get('cashier_prefix')
        else:
            user.cashier_prefix = None 
            
        user.is_staff = user.user_type in ['admin', 'master_agent', 'super_agent', 'agent', 'cashier']
        user.is_superuser = user.user_type == 'admin'

        if commit:
            user.save()
            # Handle groups and user permissions if they are present in the form fields.
            if 'groups' in self.cleaned_data:
                user.groups.set(self.cleaned_data['groups'])
            if 'user_permissions' in self.cleaned_data:
                user.user_permissions.set(self.cleaned_data['user_permissions'])

            # Automatic cashier creation for new agents
            if user.user_type == 'agent':
                current_request = self.request 
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
                        Wallet.objects.create(user=cashier_user, balance=Decimal('0.00')) 
                        messages.info(current_request, f"Cashier account created by admin: {cashier_email}") if current_request else None
                    else:
                        messages.warning(current_request, f"Cashier account {cashier_email} already exists. Skipping creation.") if current_request else None

        return user


class AdminUserChangeForm(DjangoUserChangeForm):
    password = forms.CharField( 
        widget=forms.PasswordInput(attrs={'class': 'form-control rounded-md'}),
        required=False,
        help_text="Leave blank to keep current password. Enter new password here."
    )

    USER_TYPE_ADMIN_CHOICES = [
        ('player', 'Player'),
        ('cashier', 'Cashier'),
        ('agent', 'Agent'),
        ('super_agent', 'Super Agent'),
        ('master_agent', 'Master Agent'),
        ('admin', 'Admin'),
    ]
    user_type = forms.ChoiceField(choices=USER_TYPE_ADMIN_CHOICES,
                                  widget=forms.Select(attrs={'class': 'form-control'}))
    
    master_agent = forms.ModelChoiceField(queryset=User.objects.filter(user_type='master_agent'), 
                                          required=False, widget=forms.Select(attrs={'class': 'form-control'}))
    super_agent = forms.ModelChoiceField(queryset=User.objects.filter(user_type='super_agent'), 
                                         required=False, widget=forms.Select(attrs={'class': 'form-control'}))
    agent = forms.ModelChoiceField(queryset=User.objects.filter(user_type='agent'), 
                                   required=False, widget=forms.Select(attrs={'class': 'form-control'}))
    cashier_prefix = forms.CharField(max_length=10, required=False, 
                                     widget=forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Cashier Prefix (for cashiers)'}))

    class Meta: 
        model = CustomUser
        fields = (
            'email', 'password', 
            'first_name', 'last_name', 'phone_number', 'shop_address', 'user_type',
            'is_active', 'is_staff', 'is_superuser', 
            'groups', 'user_permissions', 
            'master_agent', 'super_agent', 'agent', 'cashier_prefix',
            'last_login', 'date_joined' 
        )
        read_only_fields = ('last_login', 'date_joined',)
        widgets = { 
            'email': forms.EmailInput(attrs={'class': 'form-control'}),
        }
    
    def __init__(self, *args, **kwargs):
        self.request = kwargs.pop('request', None) 
        super().__init__(*args, **kwargs)
        self.fields['password'].widget.attrs['placeholder'] = 'Leave blank to keep current password'
        
        instance = getattr(self, 'instance', None)
        current_user = self.request.user if self.request and hasattr(self.request, 'user') else None

        if current_user and not current_user.is_superuser:
            # Non-superuser admin cannot edit superusers
            if instance and instance.is_superuser:
                for field_name in self.fields:
                    self.fields[field_name].disabled = True
                self.add_error(None, "You do not have permission to edit superuser accounts.")
            
            # Filter user_type choices based on current user's role
            if current_user.user_type != 'admin':
                self.fields['user_type'].choices = [c for c in self.fields['user_type'].choices if c[0] not in ['admin', 'superuser']]
            elif current_user.user_type == 'admin' and not current_user.is_superuser:
                self.fields['user_type'].choices = [c for c in self.fields['user_type'].choices if c[0] != 'superuser']
                if instance and instance.user_type == 'superuser':
                     for field_name in self.fields: # Disable all fields if trying to edit superuser
                        self.fields[field_name].disabled = True
                     self.add_error(None, "You do not have permission to edit superuser accounts.")

        # Filter queryset for related fields based on current user's hierarchy and instance type
        if current_user and not current_user.is_superuser and current_user.user_type != 'admin':
            if current_user.user_type == 'master_agent':
                self.fields['master_agent'].queryset = User.objects.filter(pk=current_user.pk)
                self.fields['super_agent'].queryset = User.objects.filter(master_agent=current_user, user_type='super_agent')
                self.fields['agent'].queryset = User.objects.filter(
                    Q(super_agent__master_agent=current_user) | Q(master_agent=current_user),
                    user_type='agent'
                )
            elif current_user.user_type == 'super_agent':
                self.fields['master_agent'].queryset = User.objects.filter(pk=current_user.master_agent.pk) if current_user.master_agent else User.objects.none()
                self.fields['super_agent'].queryset = User.objects.filter(pk=current_user.pk)
                self.fields['agent'].queryset = User.objects.filter(super_agent=current_user, user_type='agent')
            elif current_user.user_type == 'agent':
                self.fields['master_agent'].queryset = User.objects.filter(pk=current_user.master_agent.pk) if current_user.master_agent else User.objects.none()
                self.fields['super_agent'].queryset = User.objects.filter(pk=current_user.super_agent.pk) if current_user.super_agent else User.objects.none()
                self.fields['agent'].queryset = User.objects.filter(pk=current_user.pk)
            
            # Make sure you cannot assign to yourself in the hierarchy fields
            if instance:
                self.fields['master_agent'].queryset = self.fields['master_agent'].queryset.exclude(pk=instance.pk)
                self.fields['super_agent'].queryset = self.fields['super_agent'].queryset.exclude(pk=instance.pk)
                self.fields['agent'].queryset = self.fields['agent'].queryset.exclude(pk=instance.pk)
        
        # Adjust read-only/initial fields based on instance's user_type for editing
        if instance:
            if instance.user_type == 'master_agent':
                # Master agents should not have master_agent, super_agent, agent assigned
                self.fields['master_agent'].required = False
                self.fields['super_agent'].required = False
                self.fields['agent'].required = False
            elif instance.user_type == 'super_agent':
                self.fields['super_agent'].required = False
                self.fields['agent'].required = False
                # Master agent field becomes required for super agent creation/edit
                self.fields['master_agent'].required = False 
            elif instance.user_type == 'agent':
                self.fields['agent'].required = False
                # Super agent or Master agent field becomes required for agent creation/edit
                self.fields['super_agent'].required = True 
            elif instance.user_type == 'cashier':
                self.fields['cashier_prefix'].required = True # Cashier prefix is required for cashiers
                # Agent, Super agent, or Master agent field becomes required for cashier creation/edit
                self.fields['agent'].required = True 
        

    def clean(self):
        cleaned_data = super().clean()
        
        instance = getattr(self, 'instance', None) 
        current_user = self.request.user if self.request and hasattr(self.request, 'user') else None

        # Permission check for editing superusers/admins
        if current_user and not current_user.is_superuser:
            if instance and instance.is_superuser:
                raise ValidationError("You do not have permission to edit superuser accounts.")
            if current_user.user_type != 'admin' and cleaned_data.get('user_type') in ['admin', 'superuser']:
                self.add_error('user_type', "You do not have permission to set user type to Admin or Superuser.")
            elif current_user.user_type == 'admin' and not current_user.is_superuser and cleaned_data.get('user_type') == 'superuser':
                self.add_error('user_type', "Admins (non-superuser) cannot set user type to Superuser.")


        user_type = cleaned_data.get('user_type')
        master_agent = cleaned_data.get('master_agent')
        super_agent = cleaned_data.get('super_agent')
        agent = cleaned_data.get('agent')
        cashier_prefix = cleaned_data.get('cashier_prefix')
        
        # Clear hierarchy fields if they are not applicable for the selected user_type
        if user_type == 'admin' or user_type == 'player':
            cleaned_data['master_agent'] = None
            cleaned_data['super_agent'] = None
            cleaned_data['agent'] = None
            cleaned_data['cashier_prefix'] = None
        elif user_type == 'master_agent':
            cleaned_data['super_agent'] = None
            cleaned_data['agent'] = None
            cleaned_data['cashier_prefix'] = None
        elif user_type == 'super_agent':
            cleaned_data['agent'] = None
            cleaned_data['cashier_prefix'] = None
            # if not master_agent:
            #     self.add_error('master_agent', "Super Agent must be assigned to a Master Agent.")
        elif user_type == 'agent':
            cleaned_data['cashier_prefix'] = None
            # if not (super_agent or master_agent):
            #     self.add_error(None, "Agent must be assigned to either a Super Agent or a Master Agent.")
            if super_agent and master_agent and super_agent.master_agent != master_agent:
                self.add_error(None, "If both Super Agent and Master Agent are selected, the Super Agent's Master Agent must match the selected Master Agent.")
        elif user_type == 'cashier':
            if not (agent or super_agent or master_agent):
                self.add_error(None, "Cashier must be assigned to an Agent, Super Agent, or Master Agent.")
            if agent and (super_agent or master_agent):
                if (super_agent and agent.super_agent != super_agent) or \
                   (master_agent and agent.master_agent != master_agent):
                    self.add_error(None, "Assigned Agent's hierarchy must match selected Super Agent/Master Agent.")
            
            if not cashier_prefix:
                self.add_error('cashier_prefix', "Cashier Prefix is required for Cashier user type.")

        # Ensure correct hierarchy relationship if provided
        if master_agent and super_agent and super_agent.master_agent != master_agent:
            self.add_error('super_agent', "Selected Super Agent must belong to the selected Master Agent.")
        if agent and super_agent and agent.super_agent != super_agent:
            self.add_error('agent', "Selected Agent must belong to the selected Super Agent.")
        if agent and master_agent and agent.master_agent != master_agent:
            self.add_error('agent', "Selected Agent's Master Agent must match the selected Master Agent.")
        
        # Prevent self-assignment for hierarchy fields in edit form
        if instance:
            if user_type != 'player' and (master_agent == instance or super_agent == instance or agent == instance):
                self.add_error(None, "A user cannot be their own Master Agent, Super Agent, or Agent.")
            
            # Further checks for non-superuser/non-admin editing permissions
            if current_user and not current_user.is_superuser and current_user.user_type != 'admin':
                if user_type == 'master_agent': # current_user is not superuser/admin, cannot edit master agent
                    self.add_error('user_type', "You do not have permission to change to Master Agent type.")
                elif user_type == 'super_agent' and current_user.user_type != 'master_agent' and current_user.user_type != 'super_agent':
                    self.add_error('user_type', "You do not have permission to change to Super Agent type.")
                elif user_type == 'agent' and current_user.user_type not in ['master_agent', 'super_agent', 'agent']:
                    self.add_error('user_type', "You do not have permission to change to Agent type.")
                elif user_type == 'cashier' and current_user.user_type not in ['master_agent', 'super_agent', 'agent', 'cashier']:
                     self.add_error('user_type', "You do not have permission to change to Cashier type.")

                # Enforce assignment within hierarchy for non-admin/superuser
                if current_user.user_type == 'master_agent':
                    if master_agent and master_agent != current_user:
                        self.add_error('master_agent', "You can only assign a Master Agent to yourself within your hierarchy.")
                    if super_agent and super_agent.master_agent != current_user:
                        self.add_error('super_agent', "Selected Super Agent is not under your Master Agent hierarchy.")
                    if agent and (agent.master_agent != current_user and (not agent.super_agent or agent.super_agent.master_agent != current_user)):
                        self.add_error('agent', "Selected Agent is not under your Master Agent hierarchy.")
                elif current_user.user_type == 'super_agent':
                    if master_agent and master_agent != current_user.master_agent:
                        self.add_error('master_agent', "Selected Master Agent must be your Master Agent.")
                    if super_agent and super_agent != current_user:
                        self.add_error('super_agent', "You can only assign a Super Agent to yourself within your hierarchy.")
                    if agent and agent.super_agent != current_user:
                        self.add_error('agent', "Selected Agent is not under your Super Agent hierarchy.")
                elif current_user.user_type == 'agent':
                    if master_agent and master_agent != current_user.master_agent:
                        self.add_error('master_agent', "Selected Master Agent must be your Master Agent.")
                    if super_agent and super_agent != current_user.super_agent:
                        self.add_error('super_agent', "Selected Super Agent must be your Super Agent.")
                    if agent and agent != current_user:
                        self.add_error('agent', "You can only assign an Agent to yourself within your hierarchy.")


        return cleaned_data

    def save(self, commit=True):
        user = super().save(commit=False)
        
        if 'password' in self.cleaned_data and self.cleaned_data['password']:
            user.set_password(self.cleaned_data['password'])

        user.user_type = self.cleaned_data['user_type']
        user.first_name = self.cleaned_data.get('first_name')
        user.last_name = self.cleaned_data.get('last_name')
        user.phone_number = self.cleaned_data.get('phone_number')
        user.shop_address = self.cleaned_data.get('shop_address')

        # Set hierarchy fields based on user_type chosen in the form
        # Ensure that if a hierarchy field is not selected or not applicable for the user_type, it's explicitly set to None
        master_agent_data = self.cleaned_data.get('master_agent')
        super_agent_data = self.cleaned_data.get('super_agent')
        agent_data = self.cleaned_data.get('agent')

        if user.user_type == 'master_agent':
            user.master_agent = None
            user.super_agent = None
            user.agent = None
        elif user.user_type == 'super_agent':
            user.master_agent = master_agent_data
            user.super_agent = None
            user.agent = None
        elif user.user_type == 'agent':
            user.master_agent = master_agent_data
            user.super_agent = super_agent_data
            user.agent = None 
        elif user.user_type == 'cashier':
            user.master_agent = master_agent_data
            user.super_agent = super_agent_data
            user.agent = agent_data
        else: # Player, Admin
            user.master_agent = None
            user.super_agent = None
            user.agent = None
        
        if user.user_type == 'cashier':
            user.cashier_prefix = self.cleaned_data.get('cashier_prefix')
        else:
            user.cashier_prefix = None 
            
        user.is_staff = user.user_type in ['admin', 'master_agent', 'super_agent', 'agent', 'cashier']
        user.is_superuser = user.user_type == 'admin'

        if commit:
            user.save()
            # Handle groups and user permissions if they are present in the form fields.
            if 'groups' in self.cleaned_data:
                user.groups.set(self.cleaned_data['groups'])
            if 'user_permissions' in self.cleaned_data:
                user.user_permissions.set(self.cleaned_data['user_permissions'])
        return user


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
