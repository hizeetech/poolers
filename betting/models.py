from django.db import models
from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.core.exceptions import ValidationError
from django.utils import timezone
import uuid
from decimal import Decimal
from django.core.validators import MinValueValidator
import secrets
import string
from django.db.models import Q
from django.db.models.signals import pre_save, post_save
from django.dispatch import receiver
from django.db import transaction

class SiteConfiguration(models.Model):
    site_name = models.CharField(max_length=255, default="PoolBetting")
    logo = models.ImageField(upload_to='site_branding/', blank=True, null=True)
    favicon = models.ImageField(upload_to='site_branding/', blank=True, null=True, help_text="Upload a favicon (small icon) for the browser tab.")
    navbar_text_type = models.CharField(
        max_length=10, 
        choices=[('dark', 'Light Text (for Dark Backgrounds)'), ('light', 'Dark Text (for Light Backgrounds)')],
        default='light',
        help_text="Choose 'Light Text' if using a dark navbar color, and vice versa."
    )
    navbar_gradient_start = models.CharField(max_length=50, default="#ffffff", help_text="Gradient Start Color (Left/Logo side)")
    navbar_gradient_end = models.CharField(max_length=50, default="#f8f9fa", help_text="Gradient End Color (Right side)")
    navbar_link_hover_color = models.CharField(max_length=50, default="#007bff", help_text="Color of nav links on hover")
    
    def save(self, *args, **kwargs):
        self.pk = 1
        super(SiteConfiguration, self).save(*args, **kwargs)

    @classmethod
    def load(cls):
        obj, created = cls.objects.get_or_create(pk=1)
        return obj

    def __str__(self):
        return "Site Configuration"

    class Meta:
        verbose_name = "Site Configuration"
        verbose_name_plural = "Site Configuration"

class CustomUserManager(BaseUserManager):
    def create_user(self, email, password=None, **extra_fields):
        if not email:
            raise ValueError('The Email field must be set')
        email = self.normalize_email(email)
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)
        extra_fields.setdefault('is_active', True)
        extra_fields.setdefault('user_type', 'admin')

        if extra_fields.get('is_staff') is not True:
            raise ValueError('Superuser must have is_staff=True.')
        if extra_fields.get('is_superuser') is not True:
            raise ValueError('Superuser must have is_superuser=True.')

        return self.create_user(email, password, **extra_fields)

class User(AbstractBaseUser, PermissionsMixin):
    USER_TYPE_CHOICES = (
        ('player', 'Player'),
        ('cashier', 'Cashier'),
        ('agent', 'Agent'),
        ('super_agent', 'Super Agent'),
        ('master_agent', 'Master Agent'),
        ('account_user', 'Account User'),
        ('admin', 'Admin'),
    )

    email = models.EmailField(unique=True)
    first_name = models.CharField(max_length=100, blank=True, null=True)
    last_name = models.CharField(max_length=100, blank=True, null=True)
    phone_number = models.CharField(max_length=20, blank=True, null=True)
    shop_address = models.CharField(max_length=255, blank=True, null=True)
    
    user_type = models.CharField(max_length=20, choices=USER_TYPE_CHOICES, default='player')
    
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    is_superuser = models.BooleanField(default=False)

    master_agent = models.ForeignKey('self', on_delete=models.SET_NULL, null=True, blank=True, related_name='master_agents_under')
    super_agent = models.ForeignKey('self', on_delete=models.SET_NULL, null=True, blank=True, related_name='super_agents_under')
    agent = models.ForeignKey('self', on_delete=models.SET_NULL, null=True, blank=True, related_name='agents_under')

    cashier_prefix = models.CharField(max_length=10, blank=True, null=True) 

    # Security fields
    failed_login_attempts = models.IntegerField(default=0)
    last_failed_login = models.DateTimeField(null=True, blank=True)
    is_locked = models.BooleanField(default=False)
    locked_at = models.DateTimeField(null=True, blank=True)
    lock_reason = models.TextField(null=True, blank=True)

    date_joined = models.DateTimeField(default=timezone.now)

    objects = CustomUserManager()

    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = []

    class Meta:
        verbose_name = 'User'
        verbose_name_plural = 'Users'
        constraints = [
            models.UniqueConstraint(fields=['cashier_prefix'], condition=~Q(cashier_prefix__isnull=True) & ~Q(cashier_prefix=''), name='unique_cashier_prefix_if_not_null_or_empty')
        ]

    def __str__(self):
        return f"{self.first_name or ''} {self.last_name or ''}".strip() or self.email

    def get_full_name(self):
        return f"{self.first_name or ''} {self.last_name or ''}".strip() or self.email

    def get_short_name(self):
        return self.first_name or self.email

    @property
    def wallet(self):
        wallet, created = Wallet.objects.get_or_create(user=self)
        return wallet

    def clean(self):
        if self.user_type == 'admin':
            self.is_staff = True
            self.is_superuser = True
        elif self.user_type in ['master_agent', 'super_agent', 'agent', 'cashier']:
            self.is_staff = True 
            self.is_superuser = False
        else:
            self.is_staff = False
            self.is_superuser = False

        if self.user_type == 'cashier' and not self.cashier_prefix:
            raise ValidationError({'cashier_prefix': 'Cashier must have a cashier prefix.'})
        
        if self.cashier_prefix and self.user_type != 'cashier':
            raise ValidationError({'user_type': 'Cashier prefix can only be set for users with user type "Cashier".'})

        if self.user_type != 'cashier' and self.cashier_prefix:
            self.cashier_prefix = None

        super().clean()

class SystemSetting(models.Model):
    key = models.CharField(max_length=100, unique=True)
    value = models.TextField()
    description = models.TextField(blank=True, null=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.key}: {self.value}"

    @classmethod
    def get_setting(cls, key, default=None):
        try:
            return cls.objects.get(key=key).value
        except cls.DoesNotExist:
            return default

class Wallet(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='wallet')
    balance = models.DecimalField(max_digits=10, decimal_places=2, default=0.00, validators=[MinValueValidator(Decimal('0.00'))])
    last_updated = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Wallet for {self.user.email} (Balance: {self.balance})"

class Transaction(models.Model):
    TRANSACTION_TYPES = (
        ('deposit', 'Deposit'),
        ('withdrawal', 'Withdrawal'),
        ('bet_placement', 'Bet Placement'),
        ('bet_payout', 'Bet Payout'),
        ('commission_payout', 'Commission Payout'),
        ('wallet_transfer_out', 'Wallet Transfer Out'),
        ('wallet_transfer_in', 'Wallet Transfer In'),
        ('bonus', 'Bonus'),
        ('ticket_deletion_refund', 'Ticket Deletion Refund'),
        ('withdrawal_refund', 'Withdrawal Refund'),
    )
    STATUS_CHOICES = (
        ('pending', 'Pending'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
        ('reversed', 'Reversed'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='transactions')
    initiating_user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='initiated_transactions')
    target_user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='received_transactions')
    transaction_type = models.CharField(max_length=50, choices=TRANSACTION_TYPES)
    amount = models.DecimalField(max_digits=10, decimal_places=2, validators=[MinValueValidator(Decimal('0.01'))])
    is_successful = models.BooleanField(default=False)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    description = models.TextField(blank=True, null=True)
    timestamp = models.DateTimeField(auto_now_add=True)
    
    related_bet_ticket = models.ForeignKey('BetTicket', on_delete=models.SET_NULL, null=True, blank=True, related_name='transactions')
    related_withdrawal_request = models.ForeignKey('UserWithdrawal', on_delete=models.SET_NULL, null=True, blank=True, related_name='transactions')
    related_payout = models.ForeignKey('AgentPayout', on_delete=models.SET_NULL, null=True, blank=True, related_name='transactions')
    paystack_reference = models.CharField(max_length=100, blank=True, null=True, unique=True)

    class Meta:
        ordering = ['-timestamp']

    def __str__(self):
        return f"{self.transaction_type} - {self.user.email} - {self.amount} ({self.status})"

class BettingPeriod(models.Model):
    name = models.CharField(max_length=255, unique=True)
    start_date = models.DateField()
    end_date = models.DateField()
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['-start_date']

    def __str__(self):
        return self.name

class Fixture(models.Model):
    STATUS_CHOICES = (
        ('scheduled', 'Scheduled'),
        ('live', 'Live'),
        ('finished', 'Finished'),
        ('postponed', 'Postponed'),
        ('cancelled', 'Cancelled'),
        ('settled', 'Settled'),
    )
    betting_period = models.ForeignKey(BettingPeriod, on_delete=models.CASCADE, related_name='fixtures')
    serial_number = models.CharField(max_length=50)
    home_team = models.CharField(max_length=255)
    away_team = models.CharField(max_length=255)
    match_date = models.DateField()
    match_time = models.TimeField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='scheduled')
    home_score = models.IntegerField(null=True, blank=True)
    away_score = models.IntegerField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    # Odds Fields
    home_win_odd = models.DecimalField(max_digits=5, decimal_places=2, default=1.00)
    draw_odd = models.DecimalField(max_digits=5, decimal_places=2, default=1.00)
    away_win_odd = models.DecimalField(max_digits=5, decimal_places=2, default=1.00)
    over_1_5_odd = models.DecimalField(max_digits=5, decimal_places=2, default=1.00)
    under_1_5_odd = models.DecimalField(max_digits=5, decimal_places=2, default=1.00)
    over_2_5_odd = models.DecimalField(max_digits=5, decimal_places=2, default=1.00)
    under_2_5_odd = models.DecimalField(max_digits=5, decimal_places=2, default=1.00)
    over_3_5_odd = models.DecimalField(max_digits=5, decimal_places=2, default=1.00)
    under_3_5_odd = models.DecimalField(max_digits=5, decimal_places=2, default=1.00)
    btts_yes_odd = models.DecimalField(max_digits=5, decimal_places=2, default=1.00)
    btts_no_odd = models.DecimalField(max_digits=5, decimal_places=2, default=1.00)
    home_dnb_odd = models.DecimalField(max_digits=5, decimal_places=2, default=1.00)
    away_dnb_odd = models.DecimalField(max_digits=5, decimal_places=2, default=1.00)

    def __str__(self):
        return f"{self.home_team} vs {self.away_team}"

class Selection(models.Model):
    bet_ticket = models.ForeignKey('BetTicket', on_delete=models.CASCADE, related_name='selections')
    fixture = models.ForeignKey(Fixture, on_delete=models.CASCADE, related_name='selections')
    bet_type = models.CharField(max_length=50)
    odd_selected = models.DecimalField(max_digits=10, decimal_places=2)
    is_winning_selection = models.BooleanField(null=True, blank=True)

    def __str__(self):
        return f"{self.fixture} - {self.bet_type}"

class BetTicket(models.Model):
    STATUS_CHOICES = (
        ('pending', 'Pending'),
        ('won', 'Won'),
        ('lost', 'Lost'),
        ('cashed_out', 'Cashed Out'),
        ('deleted', 'Deleted'),
        ('cancelled', 'Cancelled'),
    )

    BET_TYPE_CHOICES = (
        ('single', 'Single'),
        ('multiple', 'Multiple'),
        ('system', 'System'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    ticket_id = models.CharField(max_length=8, unique=True, editable=False, null=True)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='bet_tickets')
    bet_type = models.CharField(max_length=20, choices=BET_TYPE_CHOICES, default='single')
    system_min_count = models.PositiveIntegerField(null=True, blank=True, help_text="Minimum selections required for system bet (k in k/n)")
    
    stake_amount = models.DecimalField(max_digits=10, decimal_places=2, validators=[MinValueValidator(Decimal('0.01'))], db_index=True)
    total_odd = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('1.00'))
    potential_winning = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    max_winning = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending', db_index=True)
    
    placed_at = models.DateTimeField(auto_now_add=True, db_index=True)
    last_updated = models.DateTimeField(auto_now=True, db_index=True)
    
    deleted_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='deleted_tickets')
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-placed_at']
        verbose_name_plural = "Bet Tickets"

    def __str__(self):
        return f"Ticket {self.id} by {self.user.email} - Stake: {self.stake_amount} - Status: {self.status}"

    def save(self, *args, **kwargs):
        if not self.ticket_id:
            while True:
                new_id = ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(6))
                if not BetTicket.objects.filter(ticket_id=new_id).exists():
                    self.ticket_id = new_id
                    break
        super().save(*args, **kwargs)

    def calculate_total_odd_and_potential_winning(self):
        calculated_odd = Decimal('1.00')
        for selection in self.selections.all(): 
            calculated_odd *= selection.odd_selected
        
        self.total_odd = calculated_odd.quantize(Decimal('0.01'))

        self.potential_winning = (self.stake_amount * self.total_odd).quantize(Decimal('0.01'))

        max_winning_setting = SystemSetting.objects.filter(key='max_winning_per_ticket').first()
        if max_winning_setting and max_winning_setting.value:
            try:
                max_winning_limit = Decimal(max_winning_setting.value)
                self.max_winning = min(self.potential_winning, max_winning_limit).quantize(Decimal('0.01'))
            except Exception:
                self.max_winning = self.potential_winning
        else:
            self.max_winning = self.potential_winning

    def get_min_potential_winning(self):
        if self.bet_type != 'system' or not self.system_min_count:
            return self.max_winning
        
        # System bet calculation
        from itertools import combinations
        all_selections = list(self.selections.all())
        if len(all_selections) < self.system_min_count:
            return Decimal('0.00')
            
        lines = list(combinations(all_selections, self.system_min_count))
        num_lines = len(lines)
        if num_lines == 0:
            return Decimal('0.00')
            
        stake_per_line = self.stake_amount / Decimal(num_lines)
        
        # Find min odd line
        min_line_odd = None
        
        for line in lines:
            line_odd = Decimal('1.00')
            for sel in line:
                line_odd *= sel.odd_selected
            
            if min_line_odd is None or line_odd < min_line_odd:
                min_line_odd = line_odd
                
        return (stake_per_line * min_line_odd).quantize(Decimal('0.01'))

    def has_computed_results(self):
        return self.selections.filter(fixture__status__in=['finished', 'settled']).exists()

    def check_and_update_status(self):
        if self.status == 'pending':
            all_fixtures_settled = True
            
            # First pass: Update all selections based on fixture status/score
            for selection in self.selections.all():
                fixture = selection.fixture
                
                # Check for cancelled fixture
                if fixture.status == 'cancelled':
                    self.status = 'cancelled'
                    self.save()
                    return # Exit immediately

                if fixture.status != 'settled':
                    all_fixtures_settled = False
                
                # Determine winning status if scores are available
                is_winning_selection = False
                total_goals = (fixture.home_score + fixture.away_score) if (fixture.home_score is not None and fixture.away_score is not None) else None

                # Logic to determine if selection is winning...
                # Note: This logic assumes scores are final if we are checking. 
                # Ideally we only check if fixture.status is 'settled' or 'finished'.
                # But to be 'instant', we might check even if 'live' or just 'finished' before 'settled'.
                # For safety, let's rely on scores being present.
                
                # Only update selection status if the fixture is finished or settled
                if fixture.status in ['finished', 'settled'] and fixture.home_score is not None and fixture.away_score is not None:
                     if selection.bet_type == 'home_win':
                         is_winning_selection = (fixture.home_score > fixture.away_score)
                     elif selection.bet_type == 'draw':
                         is_winning_selection = (fixture.home_score == fixture.away_score)
                     elif selection.bet_type == 'away_win':
                         is_winning_selection = (fixture.home_score < fixture.away_score)
                     elif selection.bet_type == 'over_1_5' and total_goals is not None:
                         is_winning_selection = (total_goals > Decimal('1.5'))
                     elif selection.bet_type == 'under_1_5' and total_goals is not None:
                         is_winning_selection = (total_goals <= Decimal('1.5'))
                     elif selection.bet_type == 'over_2_5' and total_goals is not None:
                         is_winning_selection = (total_goals > Decimal('2.5'))
                     elif selection.bet_type == 'under_2_5' and total_goals is not None:
                         is_winning_selection = (total_goals <= Decimal('2.5'))
                     elif selection.bet_type == 'over_3_5' and total_goals is not None:
                         is_winning_selection = (total_goals > Decimal('3.5'))
                     elif selection.bet_type == 'under_3_5' and total_goals is not None:
                         is_winning_selection = (total_goals <= Decimal('3.5'))
                     elif selection.bet_type == 'btts_yes':
                         is_winning_selection = (fixture.home_score > 0 and fixture.away_score > 0)
                     elif selection.bet_type == 'btts_no':
                         is_winning_selection = (fixture.home_score == 0 or fixture.away_score == 0)
                     elif selection.bet_type == 'home_dnb':
                         if fixture.home_score == fixture.away_score:
                             is_winning_selection = None
                         else:
                             is_winning_selection = (fixture.home_score > fixture.away_score)
                     elif selection.bet_type == 'away_dnb':
                         if fixture.home_score == fixture.away_score:
                             is_winning_selection = None
                         else:
                             is_winning_selection = (fixture.home_score < fixture.away_score)
                     
                     selection.is_winning_selection = is_winning_selection
                     selection.save()

            # Second pass: Determine ticket status
            if self.bet_type == 'system' and self.system_min_count:
                # System bet logic (simplified for brevity, keeping existing structure)
                # Only resolve if all fixtures are settled for system bets to avoid complexity
                if not all_fixtures_settled:
                    return

                from itertools import combinations
                winning_selections = [s for s in self.selections.all() if s.is_winning_selection]
                
                # ... (calculation logic same as before) ...
                # Re-implementing simplified version to ensure context match
                all_selections = list(self.selections.all())
                lines = list(combinations(all_selections, self.system_min_count))
                
                winning_amount = Decimal('0.00')
                lines_won_count = 0
                num_lines = len(lines)
                stake_per_line = self.stake_amount / Decimal(num_lines)

                for line in lines:
                    line_won = True
                    line_odd = Decimal('1.00')
                    for sel in line:
                        if sel.is_winning_selection is False:
                            line_won = False
                            break
                        elif sel.is_winning_selection is True:
                            line_odd *= sel.odd_selected
                        elif sel.is_winning_selection is None: # Void
                            line_odd *= Decimal('1.00')
                    
                    if line_won:
                        lines_won_count += 1
                        winning_amount += (stake_per_line * line_odd)
                
                if lines_won_count > 0:
                    self.status = 'won'
                    self.potential_winning = winning_amount.quantize(Decimal('0.01'))
                    self.max_winning = self.potential_winning
                else:
                    self.status = 'lost'
                    self.potential_winning = Decimal('0.00')

            else: # Single or Multiple
                ticket_won = True
                total_odd_settled = Decimal('1.00')
                any_selection_lost = False
                
                for selection in self.selections.all():
                    if selection.is_winning_selection is False:
                        ticket_won = False
                        any_selection_lost = True
                        break # One loss kills the ticket
                    elif selection.is_winning_selection is True:
                        total_odd_settled *= selection.odd_selected
                    elif selection.is_winning_selection is None: # Void
                        total_odd_settled *= Decimal('1.00')
                    # If None (not settled yet), we can't decide unless we found a loss
                
                if any_selection_lost:
                    self.status = 'lost'
                    self.potential_winning = Decimal('0.00')
                    self.save()
                    return # Instant update to lost

                if not all_fixtures_settled:
                    return # Still pending and no losses yet

                if ticket_won:
                    self.status = 'won'
                    self.potential_winning = (self.stake_amount * total_odd_settled).quantize(Decimal('0.01'))
                    self.max_winning = self.potential_winning
                else:
                    self.status = 'lost'
                    self.potential_winning = Decimal('0.00')

            self.save()

            # Post-save: Credit wallet if won
            if self.status == 'won':
                with transaction.atomic():
                    wallet = Wallet.objects.select_for_update().get(user=self.user)
                    wallet.balance += self.max_winning
                    wallet.save()
                    
                    Transaction.objects.create(
                        user=self.user,
                        initiating_user=None, # System
                        transaction_type='bet_payout',
                        amount=self.max_winning,
                        is_successful=True,
                        status='completed',
                        description=f"Winnings for ticket {self.ticket_id}",
                        related_bet_ticket=self,
                        timestamp=timezone.now()
                    )

class Result(Fixture):
    class Meta:
        proxy = True
        verbose_name = "Result"
        verbose_name_plural = "Results"


class BonusRule(models.Model):
    name = models.CharField(max_length=255)
    min_selections = models.IntegerField(default=1, validators=[MinValueValidator(1)])
    min_odd_per_selection = models.DecimalField(max_digits=5, decimal_places=2, default=1.01, validators=[MinValueValidator(Decimal('1.01'))])
    bonus_percentage = models.DecimalField(max_digits=5, decimal_places=2, default=0.00, validators=[MinValueValidator(Decimal('0.00'))])

    class Meta:
        verbose_name_plural = "Bonus Rules"
        unique_together = (('min_selections', 'min_odd_per_selection'),)

    def __str__(self):
        return f"Bonus: {self.name} ({self.bonus_percentage*100}% for {self.min_selections}+ selections)"

class AgentPayout(models.Model):
    STATUS_CHOICES = (
        ('pending', 'Pending'),
        ('settled', 'Settled'),
    )
    
    agent = models.ForeignKey(User, on_delete=models.CASCADE, limit_choices_to={'user_type__in': ['agent', 'super_agent', 'master_agent']}, related_name='agent_payouts')
    betting_period = models.ForeignKey(BettingPeriod, on_delete=models.CASCADE, related_name='payouts')
    
    total_turnover = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    total_winnings = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    ggr = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    commission_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0.00)
    commission_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    created_at = models.DateTimeField(auto_now_add=True)
    settled_at = models.DateTimeField(null=True, blank=True)
    settled_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='settled_agent_payouts')

    class Meta:
        verbose_name_plural = "Agent Payouts"
        unique_together = (('agent', 'betting_period'),)
        ordering = ['-created_at']

    def __str__(self):
        return f"Payout for {self.agent.email} - Period {self.betting_period.name} - ₦{self.commission_amount} ({self.status})"

class UserWithdrawal(models.Model):
    STATUS_CHOICES = (
        ('pending', 'Pending'),
        ('approved', 'Approved'),
        ('rejected', 'Rejected'),
        ('completed', 'Completed'),
    )
    
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='withdrawal_requests')
    amount = models.DecimalField(max_digits=10, decimal_places=2, validators=[MinValueValidator(Decimal('0.01'))])
    bank_name = models.CharField(max_length=255)
    account_name = models.CharField(max_length=255)
    account_number = models.CharField(max_length=50)
    request_time = models.DateTimeField(auto_now_add=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    
    approved_rejected_time = models.DateTimeField(null=True, blank=True)
    approved_rejected_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='handled_withdrawals')

    def __str__(self):
        return f"Withdrawal {self.id} - {self.user.email} - {self.amount}"

class ActivityLog(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='activity_logs')
    action = models.TextField()
    action_type = models.CharField(max_length=50, blank=True, null=True)
    timestamp = models.DateTimeField(auto_now_add=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(null=True, blank=True)
    path = models.CharField(max_length=255, null=True, blank=True)
    isp = models.CharField(max_length=255, null=True, blank=True)
    affected_object = models.CharField(max_length=255, null=True, blank=True)

    class Meta:
        ordering = ['-timestamp']

    def __str__(self):
        return f"{self.user.email} - {self.action}"

class LoginAttempt(models.Model):
    STATUS_CHOICES = (
        ('success', 'Success'),
        ('failed', 'Failed'),
        ('locked', 'Account Locked'),
        ('unlocked', 'Account Unlocked'),
    )
    
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='login_attempts')
    username_attempted = models.CharField(max_length=255, null=True, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES)
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-timestamp']
        verbose_name = 'Login Attempt'
        verbose_name_plural = 'Login Attempts'

    def __str__(self):
        return f"{self.username_attempted} - {self.status} - {self.timestamp}"

@receiver(pre_save, sender=BetTicket)
def refund_stake_on_void(sender, instance, **kwargs):
    if instance.pk:
        try:
            old_ticket = BetTicket.objects.get(pk=instance.pk)
            if old_ticket.status not in ['cancelled', 'deleted'] and instance.status in ['cancelled', 'deleted']:
                with transaction.atomic():
                    wallet = Wallet.objects.select_for_update().get(user=instance.user)
                    wallet.balance += instance.stake_amount
                    wallet.save()
                    
                    initiating_user = instance.deleted_by if instance.deleted_by else None
                    
                    Transaction.objects.create(
                        user=instance.user,
                        initiating_user=initiating_user,
                        transaction_type='ticket_deletion_refund',
                        amount=instance.stake_amount,
                        is_successful=True,
                        status='completed',
                        description=f"Refund for ticket {instance.ticket_id} (Status: {instance.status})",
                        related_bet_ticket=instance,
                        timestamp=timezone.now()
                    )
        except BetTicket.DoesNotExist:
            pass

@receiver(post_save, sender=Fixture)
@receiver(post_save, sender=Result)
def update_tickets_on_fixture_change(sender, instance, created, **kwargs):
    if not created:

        tickets = BetTicket.objects.filter(selections__fixture=instance).distinct()
        
        for ticket in tickets:
            ticket.check_and_update_status()

class CreditRequest(models.Model):
    STATUS_CHOICES = (
        ('pending', 'Pending'),
        ('approved', 'Approved'),
        ('declined', 'Declined'),
    )
    REQUEST_TYPE_CHOICES = (
        ('credit', 'Normal Credit'),
        ('loan', 'Loan'),
    )
    
    requester = models.ForeignKey(User, on_delete=models.CASCADE, related_name='sent_credit_requests')
    recipient = models.ForeignKey(User, on_delete=models.CASCADE, related_name='received_credit_requests')
    amount = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(Decimal('0.01'))])
    reason = models.TextField()
    request_type = models.CharField(max_length=20, choices=REQUEST_TYPE_CHOICES, default='credit')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    def __str__(self):
        return f"{self.requester} -> {self.recipient}: {self.amount} ({self.status})"

class Loan(models.Model):
    STATUS_CHOICES = (
        ('active', 'Active'),
        ('settled', 'Settled'),
        ('defaulted', 'Defaulted'),
    )
    
    borrower = models.ForeignKey(User, on_delete=models.CASCADE, related_name='loans_borrowed')
    lender = models.ForeignKey(User, on_delete=models.CASCADE, related_name='loans_lent')
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    outstanding_balance = models.DecimalField(max_digits=12, decimal_places=2)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='active')
    created_at = models.DateTimeField(auto_now_add=True)
    due_date = models.DateTimeField(null=True, blank=True)
    credit_request = models.OneToOneField(CreditRequest, on_delete=models.SET_NULL, null=True, blank=True, related_name='loan')
    
    def __str__(self):
        return f"Loan: {self.borrower} owes {self.lender} {self.outstanding_balance}"

class CreditLog(models.Model):
    actor = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name='credit_actions_performed')
    target_user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name='credit_actions_received')
    action_type = models.CharField(max_length=50) 
    amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    timestamp = models.DateTimeField(auto_now_add=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    reference_id = models.CharField(max_length=100, blank=True, null=True)
    status = models.CharField(max_length=20, blank=True, null=True)
    
    def __str__(self):
        return f"{self.timestamp} - {self.action_type}"

