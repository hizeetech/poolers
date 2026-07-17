from django.db import close_old_connections, models
from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.core.exceptions import ValidationError
from django.utils import timezone
import uuid
from decimal import Decimal
from django.core.validators import MinValueValidator, RegexValidator, FileExtensionValidator
import secrets
import string
import re
import hashlib
import math
import os
import sys
from datetime import timedelta, time
from django.contrib.auth.hashers import make_password, check_password
from django.db.models import Q
from django.db.models.signals import pre_save, post_save
from django.dispatch import receiver
from django.db import transaction
import threading
from django.conf import settings
from django.core.cache import cache
from django_ckeditor_5.fields import CKEditor5Field
from django.apps import apps


class SiteConfiguration(models.Model):
    site_name = models.CharField(max_length=255, default="StakeNaija")
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
    landing_page_background = models.ImageField(upload_to='site_branding/', blank=True, null=True, help_text="Background image for the landing page")
    show_ticket_status_on_landing = models.BooleanField(default=True, help_text="Show/Hide the 'Check Your Bet Ticket Status' section on the landing page.")
    carousel_interval = models.PositiveIntegerField(default=5000, help_text="Carousel sliding interval in milliseconds (e.g., 5000 for 5 seconds).")
    
    # Commission Settings
    account_user_commission_authority = models.BooleanField(
        default=False, 
        help_text='Enable or disable Account User commission authority.'
    )

    require_commission_recall_approval = models.BooleanField(
        default=False,
        help_text='If enabled, Account User commission recalls require Admin approval before reversal occurs.'
    )

    show_agent_pending_commission_card = models.BooleanField(
        default=True,
        help_text='Show or hide the Pending Commission card on the agent dashboard frontend.',
    )
    
    PAYMENT_SOURCE_CHOICES = [
        ('system', 'System Default (Super Admin Wallet)'), 
        ('account_wallet', 'Account User Wallet'), 
        ('manual', 'Manual Selection Per Payout')
    ]
    commission_payment_source = models.CharField(
        max_length=20, 
        choices=PAYMENT_SOURCE_CHOICES, 
        default='system', 
        help_text='Select the funding source for commission payments.'
    )

    # Bet Permission Settings
    allow_single_bet = models.BooleanField(
        default=True,
        help_text='Allow users to place single bets (1 selection).'
    )
    allow_double_bet = models.BooleanField(
        default=True,
        help_text='Allow users to place double bets (2 selections).'
    )
    allow_multiple_bet = models.BooleanField(
        default=True,
        help_text='Allow users to place multiple bets (3 or more selections).'
    )

    enable_global_cashier_voiding = models.BooleanField(
        default=False,
        verbose_name="Enable Ticket Voiding For All Cashiers",
        help_text="If enabled, all cashiers can submit ticket void requests. If disabled, agent-level cashier permissions apply.",
    )
    crm_large_deposit_threshold = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal('100000.00'),
        validators=[MinValueValidator(Decimal('0.00'))],
        help_text="Deposit amount at or above this threshold is treated as a large deposit for CRM monitoring.",
    )
    crm_failed_deposit_repeat_threshold = models.PositiveIntegerField(
        default=3,
        help_text="Minimum number of failed deposits before a user is flagged as a repeated failed depositor.",
    )
    loan_min_ticket_count = models.PositiveIntegerField(
        default=50,
        help_text="Minimum valid ticket count required to qualify for an overdraft request in the current commission window.",
    )
    loan_min_deposit_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal('50000.00'),
        validators=[MinValueValidator(Decimal('0.00'))],
        help_text="Minimum successful gateway deposit volume required to qualify for an overdraft request.",
    )
    loan_percentage = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal('50.00'),
        validators=[MinValueValidator(Decimal('0.00'))],
        help_text="Percentage of qualified deposits that can be requested as overdraft.",
    )
    loan_application_day = models.CharField(
        max_length=12,
        default='friday',
        help_text="Day of week when overdraft applications open.",
    )
    loan_application_time = models.TimeField(
        default=time(16, 0),
        help_text="Time when overdraft applications open.",
    )
    loan_repayment_day = models.CharField(
        max_length=12,
        default='saturday',
        help_text="Day of week when overdraft repayment is due.",
    )
    loan_repayment_time = models.TimeField(
        default=time(15, 0),
        help_text="Time when overdraft repayment is due.",
    )
    
    def save(self, *args, **kwargs):
        previous_values = None
        if self.pk:
            previous_values = (
                SiteConfiguration.objects.filter(pk=self.pk)
                .values("loan_repayment_day", "loan_repayment_time")
                .first()
            )
        self.pk = 1
        super(SiteConfiguration, self).save(*args, **kwargs)
        repayment_settings_changed = bool(
            previous_values
            and (
                previous_values.get("loan_repayment_day") != self.loan_repayment_day
                or previous_values.get("loan_repayment_time") != self.loan_repayment_time
            )
        )
        if repayment_settings_changed:
            from betting.services.loan_overdraft import sync_active_loan_due_dates_with_site_configuration

            transaction.on_commit(sync_active_loan_due_dates_with_site_configuration)

    def _safe_media_file_url(self, field_file):
        try:
            if field_file and getattr(field_file, "name", "") and field_file.storage.exists(field_file.name):
                return field_file.url
        except Exception:
            return ""
        return ""

    @property
    def safe_logo_url(self):
        return self._safe_media_file_url(self.logo) or f"{settings.STATIC_URL}betting/images/logo.png"

    @property
    def safe_favicon_url(self):
        return (
            self._safe_media_file_url(self.favicon)
            or self._safe_media_file_url(self.logo)
            or f"{settings.STATIC_URL}betting/images/logo.png"
        )

    @classmethod
    def load(cls):
        obj, created = cls.objects.get_or_create(pk=1)
        return obj

    def __str__(self):
        return "Site Configuration"

    class Meta:
        verbose_name = "Site Configuration"
        verbose_name_plural = "Site Configuration"

class CarouselImage(models.Model):
    image = models.ImageField(upload_to='carousel_images/')
    title = models.CharField(max_length=255, blank=True, null=True, help_text="Optional title for the image")
    description = models.TextField(blank=True, null=True, help_text="Optional description for the image")
    is_active = models.BooleanField(default=True)
    order = models.PositiveIntegerField(default=0, help_text="Order of display")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['order', '-created_at']
        verbose_name = "Carousel Image"
        verbose_name_plural = "Carousel Images"

    def __str__(self):
        return f"Carousel Image {self.id} - {self.title or 'No Title'}"


class FooterPage(models.Model):
    slug = models.SlugField(unique=True)
    footer_label = models.CharField(max_length=100)
    title = models.CharField(max_length=255)
    content = CKEditor5Field(blank=True, null=True, config_name='default')
    is_active = models.BooleanField(default=True)
    show_in_footer = models.BooleanField(default=True)
    order = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['order', 'footer_label']

    def __str__(self):
        return self.footer_label


class FooterBadge(models.Model):
    image = models.ImageField(upload_to='footer_badges/')
    alt_text = models.CharField(max_length=150, blank=True, default="")
    link_url = models.URLField(blank=True, default="")
    content_hash = models.CharField(max_length=64, blank=True, default="", db_index=True)
    is_active = models.BooleanField(default=True)
    order = models.PositiveIntegerField(default=0)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['order', 'id']

    def save(self, *args, **kwargs):
        if self.image and not self.content_hash:
            file_obj = getattr(self.image, "file", None)
            if file_obj is not None:
                h = hashlib.sha256()
                try:
                    for chunk in self.image.chunks():
                        h.update(chunk)
                    self.content_hash = h.hexdigest()
                finally:
                    try:
                        file_obj.seek(0)
                    except Exception:
                        pass
        super().save(*args, **kwargs)

    def __str__(self):
        return self.alt_text or f"Footer Badge {self.id}"

class PasswordResetRequest(models.Model):
    email = models.EmailField()
    token = models.CharField(max_length=100, unique=True)
    user = models.ForeignKey('User', on_delete=models.CASCADE, null=True, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(null=True, blank=True)
    email_sent = models.BooleanField(default=False)
    sent_at = models.DateTimeField(null=True, blank=True)
    send_error = models.TextField(null=True, blank=True)
    is_used = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()

    def is_valid(self):
        return not self.is_used and timezone.now() < self.expires_at

    def __str__(self):
        return f"Reset for {self.email} at {self.created_at}"

    class Meta:
        ordering = ['-created_at']
        verbose_name = "Password Reset Request"
        verbose_name_plural = "Password Reset Requests"

class State(models.Model):
    state_name = models.CharField(max_length=100, unique=True)
    abbreviation = models.CharField(max_length=20, unique=True)

    def __str__(self):
        return f"{self.state_name} ({self.abbreviation})"

    class Meta:
        ordering = ['state_name']
        verbose_name = "State"
        verbose_name_plural = "States"

class CustomUserManager(BaseUserManager):
    def _generate_unique_username(self, email=None, preferred=None):
        base = (preferred or "").strip()
        if not base and email:
            base = (email or "").split("@")[0].strip()
        base = re.sub(r"[^A-Za-z0-9]", "", base or "")[:40] or f"user{uuid.uuid4().hex[:8]}"
        candidate = base
        suffix = 1
        while self.model.objects.filter(username__iexact=candidate).exists():
            candidate = f"{base}{suffix}"
            suffix += 1
        return candidate

    def create_user(self, email, password=None, **extra_fields):
        if not email:
            raise ValueError('The Email field must be set')
        email = self.normalize_email(email)
        username = (extra_fields.get('username') or '').strip()
        if not username:
            extra_fields['username'] = self._generate_unique_username(email=email)
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email=None, password=None, username=None, **extra_fields):
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)
        extra_fields.setdefault('is_active', True)
        extra_fields.setdefault('user_type', 'admin')

        if extra_fields.get('is_staff') is not True:
            raise ValueError('Superuser must have is_staff=True.')
        if extra_fields.get('is_superuser') is not True:
            raise ValueError('Superuser must have is_superuser=True.')

        username = (username or extra_fields.get('username') or '').strip()
        if username:
            extra_fields['username'] = username
        elif email:
            extra_fields['username'] = self._generate_unique_username(email=email, preferred='admin')

        return self.create_user(email=email, password=password, **extra_fields)

class User(AbstractBaseUser, PermissionsMixin):
    USER_TYPE_CHOICES = (
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
    )
    CRM_ROLE_CHOICES = (
        ('viewer', 'Viewer'),
        ('ops', 'Ops'),
        ('compliance', 'Compliance'),
        ('supervisor', 'Supervisor'),
    )
    KYC_STATUS_CHOICES = (
        ('unverified', 'Unverified'),
        ('pending', 'Pending'),
        ('verified', 'Verified'),
        ('rejected', 'Rejected'),
    )
    VIP_LEVEL_CHOICES = (
        ('standard', 'Standard'),
        ('vip1', 'VIP 1'),
        ('vip2', 'VIP 2'),
        ('vip3', 'VIP 3'),
    )
    FINANCE_ROLE_CHOICES = (
        ('manager', 'Finance Manager'),
        ('accountant', 'Accountant'),
        ('auditor', 'Auditor'),
        ('settlement', 'Settlement Officer'),
        ('withdrawal', 'Withdrawal Officer'),
    )

    email = models.EmailField()
    username = models.CharField(max_length=50, unique=True, blank=True, null=True)
    first_name = models.CharField(max_length=100, blank=True, null=True)
    last_name = models.CharField(max_length=100, blank=True, null=True)
    other_name = models.CharField(max_length=100, blank=True, null=True)
    state = models.ForeignKey(State, on_delete=models.SET_NULL, null=True, blank=True, related_name='users')
    phone_number = models.CharField(max_length=20, blank=True, null=True)
    shop_address = models.CharField(max_length=255, blank=True, null=True)
    bank_account_name = models.CharField(max_length=100, blank=True, default="")
    downline_activity_last_seen_at = models.DateTimeField(null=True, blank=True)
    
    user_type = models.CharField(max_length=20, choices=USER_TYPE_CHOICES, default='player')
    crm_role = models.CharField(max_length=20, choices=CRM_ROLE_CHOICES, default='viewer', db_index=True)
    finance_role = models.CharField(max_length=20, choices=FINANCE_ROLE_CHOICES, blank=True, default='', db_index=True)
    kyc_status = models.CharField(max_length=20, choices=KYC_STATUS_CHOICES, default='unverified', db_index=True)
    vip_level = models.CharField(max_length=20, choices=VIP_LEVEL_CHOICES, default='standard', db_index=True)
    vip_manager = models.ForeignKey('self', on_delete=models.SET_NULL, null=True, blank=True, related_name='vip_customers', limit_choices_to={'user_type': 'crm'})
    
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

    withdrawal_pin = models.CharField(max_length=128, blank=True, default="")
    withdrawal_attempts = models.PositiveSmallIntegerField(default=0)
    withdrawal_locked = models.BooleanField(default=False)
    withdrawal_locked_at = models.DateTimeField(null=True, blank=True)

    date_joined = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    # Permission fields
    can_manage_downline_wallets = models.BooleanField(default=True, help_text="Designates whether this agent can credit/debit downline wallets.")

    objects = CustomUserManager()

    USERNAME_FIELD = 'username'
    REQUIRED_FIELDS = ['email']

    class Meta:
        verbose_name = 'User'
        verbose_name_plural = 'Users'
        constraints = [
            models.UniqueConstraint(fields=['cashier_prefix'], condition=~Q(cashier_prefix__isnull=True) & ~Q(cashier_prefix=''), name='unique_cashier_prefix_if_not_null_or_empty')
        ]
        permissions = [
            ("can_impersonate_users", "Can impersonate users"),
        ]

    def __str__(self):
        return f"{self.first_name or ''} {self.last_name or ''}".strip() or self.email

    def save(self, *args, **kwargs):
        if not (self.username or '').strip():
            self.username = self.__class__.objects._generate_unique_username(email=self.email)
        if self.email:
            self.email = self.__class__.objects.normalize_email(self.email)
        super().save(*args, **kwargs)

    def get_full_name(self):
        return f"{self.first_name or ''} {self.last_name or ''}".strip() or self.email

    def get_short_name(self):
        return self.first_name or self.email

    @property
    def wallet(self):
        wallet, created = Wallet.objects.get_or_create(user=self)
        return wallet

    @property
    def withdrawal_account_name(self):
        return (self.bank_account_name or '').strip() or self.get_full_name()

    @property
    def withdrawal_pin_is_set(self):
        return bool(self.withdrawal_pin)

    def set_withdrawal_pin(self, raw_pin):
        self.withdrawal_pin = make_password(raw_pin)

    def check_withdrawal_pin(self, raw_pin):
        if not self.withdrawal_pin:
            return False
        return check_password(raw_pin, self.withdrawal_pin)

    def get_withdrawal_lock_expires_at(self, cooldown_hours=24):
        if not self.withdrawal_locked_at:
            return None
        return self.withdrawal_locked_at + timedelta(hours=cooldown_hours)

    def maybe_auto_unlock_withdrawal(self, now=None, cooldown_hours=24):
        if not self.withdrawal_locked:
            return False
        if not self.withdrawal_locked_at:
            return False
        now = now or timezone.now()
        expires_at = self.get_withdrawal_lock_expires_at(cooldown_hours=cooldown_hours)
        if not expires_at:
            return False
        if now >= expires_at:
            self.withdrawal_locked = False
            self.withdrawal_attempts = 0
            self.withdrawal_locked_at = None
            return True
        return False

    def clean(self):
        if self.user_type == 'admin':
            self.is_staff = True
            self.is_superuser = True
        elif self.user_type in ['master_agent', 'super_agent', 'agent', 'cashier', 'crm', 'account_user', 'retail_manager', 'finance']:
            self.is_staff = True 
            self.is_superuser = False
        else:
            self.is_staff = False
            self.is_superuser = False

        if self.username is not None:
            normalized_username = self.username.strip()
            normalized_username = re.sub(r'[^A-Za-z0-9]', '', normalized_username)
            self.username = normalized_username or None

        if self.user_type in ['agent', 'cashier']:
            if not self.first_name or not self.last_name or not self.other_name:
                raise ValidationError({'first_name': 'First Name, Last Name, and Other Name are required.'})
            if self.user_type == 'agent':
                if not self.state:
                    raise ValidationError({'state': 'State is required for agents.'})
                if not self.username and self.pk:
                    raise ValidationError({'username': 'Username is required for agents.'})
            if self.user_type == 'cashier' and not self.username:
                raise ValidationError({'username': 'Username is required for cashiers.'})

        # Hierarchy Role Validation
        if self.master_agent:
            if self.master_agent.user_type != 'master_agent':
                 raise ValidationError({'master_agent': "Invalid role selected. Only Master Agents can be assigned to this field."})
            if self.pk and self.master_agent.pk == self.pk:
                 raise ValidationError({'master_agent': "You cannot assign yourself as your own Master Agent."})

        if self.super_agent:
            if self.super_agent.user_type != 'super_agent':
                 raise ValidationError({'super_agent': "Invalid role selected. Only Super Agents can be assigned to this field."})
            if self.pk and self.super_agent.pk == self.pk:
                 raise ValidationError({'super_agent': "You cannot assign yourself as your own Super Agent."})

        # Cross-role Integrity Check
        if self.super_agent and self.super_agent.master_agent and self.master_agent:
            if self.master_agent != self.super_agent.master_agent:
                raise ValidationError({
                    'master_agent': f"Hierarchy Mismatch: The selected Super Agent belongs to Master Agent '{self.super_agent.master_agent}', but you selected '{self.master_agent}'."
                })

        # Circular Hierarchy Check
        if self.master_agent and self.pk:
            upline = self.master_agent
            visited = {self.pk}
            while upline:
                if upline.pk in visited:
                    raise ValidationError({'master_agent': "Circular hierarchy assignment detected."})
                visited.add(upline.pk)
                upline = upline.master_agent
                if len(visited) > 10: break

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

class GlobalBettingSettings(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    is_active = models.BooleanField(default=True)
    betting_enabled = models.BooleanField(default=True)

    min_stake = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('100.00'), validators=[MinValueValidator(Decimal('0.00'))])
    max_stake = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('500000.00'), validators=[MinValueValidator(Decimal('0.00'))])
    max_winning = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('40000000.00'), validators=[MinValueValidator(Decimal('0.00'))])

    max_stake_by_ticket_type = models.JSONField(blank=True, default=dict)
    max_winning_by_ticket_type = models.JSONField(blank=True, default=dict)

    max_odds_per_ticket = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True, validators=[MinValueValidator(Decimal('0.00'))])
    max_selections_per_ticket = models.PositiveIntegerField(null=True, blank=True)

    max_payout_per_day = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True, validators=[MinValueValidator(Decimal('0.00'))])
    max_payout_per_user_per_day = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True, validators=[MinValueValidator(Decimal('0.00'))])

    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='created_global_betting_settings')
    updated_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='updated_global_betting_settings')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    def clean(self):
        errors = {}
        if self.min_stake is not None and self.max_stake is not None and self.max_stake < self.min_stake:
            errors['max_stake'] = "Maximum stake must be greater than or equal to minimum stake."
        if errors:
            raise ValidationError(errors)

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def __str__(self):
        return "Global Betting Settings"

class AgentBettingLimitOverride(models.Model):
    agent = models.OneToOneField(User, on_delete=models.CASCADE, related_name='betting_limit_override', limit_choices_to={'user_type__in': ['agent', 'super_agent', 'master_agent']})
    is_active = models.BooleanField(default=True)
    custom_limits_enabled = models.BooleanField(default=True)

    min_stake = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, validators=[MinValueValidator(Decimal('0.00'))])
    max_stake = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, validators=[MinValueValidator(Decimal('0.00'))])
    max_winning = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True, validators=[MinValueValidator(Decimal('0.00'))])
    max_odds_per_ticket = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True, validators=[MinValueValidator(Decimal('0.00'))])
    max_stake_by_ticket_type = models.JSONField(blank=True, default=dict)
    max_winning_by_ticket_type = models.JSONField(blank=True, default=dict)

    max_selections_per_ticket = models.PositiveIntegerField(null=True, blank=True)
    max_payout_per_agent_per_day = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True, validators=[MinValueValidator(Decimal('0.00'))])
    max_payout_per_user_per_day = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True, validators=[MinValueValidator(Decimal('0.00'))])


    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='created_agent_betting_overrides')
    updated_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='updated_agent_betting_overrides')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def clean(self):
        errors = {}
        if self.min_stake is not None and self.max_stake is not None and self.max_stake < self.min_stake:
            errors['max_stake'] = "Maximum stake must be greater than or equal to minimum stake."
        if errors:
            raise ValidationError(errors)

    def __str__(self):
        return f"Betting Limits Override: {self.agent.email}"


class UserBettingLimitOverride(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='user_betting_limit_override')
    is_active = models.BooleanField(default=True)
    custom_limits_enabled = models.BooleanField(default=True)

    min_stake = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, validators=[MinValueValidator(Decimal('0.00'))])
    max_stake = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, validators=[MinValueValidator(Decimal('0.00'))])
    max_winning = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True, validators=[MinValueValidator(Decimal('0.00'))])
    max_odds_per_ticket = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True, validators=[MinValueValidator(Decimal('0.00'))])
    max_stake_by_ticket_type = models.JSONField(blank=True, default=dict)
    max_winning_by_ticket_type = models.JSONField(blank=True, default=dict)

    max_selections_per_ticket = models.PositiveIntegerField(null=True, blank=True)
    max_payout_per_user_per_day = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True, validators=[MinValueValidator(Decimal('0.00'))])

    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='created_user_betting_overrides')
    updated_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='updated_user_betting_overrides')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def clean(self):
        errors = {}
        if self.min_stake is not None and self.max_stake is not None and self.max_stake < self.min_stake:
            errors['max_stake'] = "Maximum stake must be greater than or equal to minimum stake."
        if errors:
            raise ValidationError(errors)

    def __str__(self):
        return f"User Betting Limits Override: {self.user.email}"


class BettingLimitAuditLog(models.Model):
    ACTION_CHOICES = (
        ('GLOBAL_UPDATE', 'Global Update'),
        ('AGENT_UPDATE', 'Agent Update'),
        ('USER_UPDATE', 'User Update'),
        ('TICKET_REJECTED', 'Ticket Rejected'),
    )

    action_type = models.CharField(max_length=30, choices=ACTION_CHOICES)
    actor = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='betting_limit_audit_logs')
    ip_address = models.GenericIPAddressField(null=True, blank=True)

    agent = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='betting_limit_audit_agent', limit_choices_to={'user_type__in': ['agent', 'super_agent', 'master_agent']})
    affected_user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='betting_limit_audit_affected_user')
    ticket = models.ForeignKey('BetTicket', on_delete=models.SET_NULL, null=True, blank=True, related_name='betting_limit_audit_logs')

    message = models.TextField(blank=True, default='')
    data = models.JSONField(blank=True, default=dict)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.action_type} - {self.created_at}"

class Wallet(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='wallet')
    balance = models.DecimalField(max_digits=10, decimal_places=2, default=0.00, validators=[MinValueValidator(Decimal('0.00'))])
    last_updated = models.DateTimeField(auto_now=True)

    @staticmethod
    def _resident_loan_amount_from_snapshot(snapshot):
        try:
            value = (snapshot or {}).get("wallet_resident_amount", "0.00")
            amount = Decimal(str(value or "0.00")).quantize(Decimal("0.01"))
        except Exception:
            amount = Decimal("0.00")
        return max(Decimal("0.00"), amount)

    @staticmethod
    def _snapshot_with_resident_loan_amount(snapshot, amount):
        snapshot_data = dict(snapshot or {})
        snapshot_data["wallet_resident_amount"] = str(max(Decimal("0.00"), Decimal(str(amount))).quantize(Decimal("0.01")))
        return snapshot_data

    def _allocate_wallet_resident_loan_amount(self, *, loan_id, amount):
        Loan = apps.get_model("betting", "Loan")
        loan = (
            Loan.objects.select_for_update()
            .filter(pk=loan_id, borrower=self.user)
            .first()
        )
        if not loan:
            return
        current_amount = self._resident_loan_amount_from_snapshot(loan.workflow_snapshot)
        loan.workflow_snapshot = self._snapshot_with_resident_loan_amount(
            loan.workflow_snapshot,
            current_amount + Decimal(str(amount or "0.00")),
        )
        loan.save(update_fields=["workflow_snapshot", "updated_at"])

    def _consume_wallet_resident_loan_amount(self, *, balance_after):
        Loan = apps.get_model("betting", "Loan")
        active_loans = list(
            Loan.objects.select_for_update()
            .filter(
                borrower=self.user,
                status__in=["active", "overdue", "defaulted", "pending"],
            )
            .order_by("due_date", "created_at", "id")
        )
        if not active_loans:
            return

        total_resident_before = Decimal("0.00")
        for loan in active_loans:
            total_resident_before += self._resident_loan_amount_from_snapshot(loan.workflow_snapshot)
        total_resident_before = total_resident_before.quantize(Decimal("0.01"))
        reduction_needed = max(Decimal("0.00"), (total_resident_before - Decimal(str(balance_after or "0.00"))).quantize(Decimal("0.01")))
        if reduction_needed <= Decimal("0.00"):
            return

        remaining_reduction = reduction_needed
        for loan in active_loans:
            if remaining_reduction <= Decimal("0.00"):
                break
            current_amount = self._resident_loan_amount_from_snapshot(loan.workflow_snapshot)
            if current_amount <= Decimal("0.00"):
                continue
            consume_amount = min(current_amount, remaining_reduction)
            loan.workflow_snapshot = self._snapshot_with_resident_loan_amount(
                loan.workflow_snapshot,
                current_amount - consume_amount,
            )
            loan.save(update_fields=["workflow_snapshot", "updated_at"])
            remaining_reduction = (remaining_reduction - consume_amount).quantize(Decimal("0.01"))

    def apply_delta(self, *, amount, actor=None, transaction_obj=None, reference="", reason="", metadata=None, allow_negative=False):
        amount_q = Decimal(str(amount)).quantize(Decimal("0.01"))
        with transaction.atomic():
            locked = Wallet.objects.select_for_update().select_related("user").get(pk=self.pk)
            before = Decimal(str(locked.balance or Decimal("0.00"))).quantize(Decimal("0.01"))
            after = (before + amount_q).quantize(Decimal("0.01"))
            if after < Decimal("0.00") and not allow_negative:
                raise ValueError("Wallet balance cannot be negative.")
            locked.balance = after
            locked.save(update_fields=["balance", "last_updated"])

            WalletLedgerEntry = apps.get_model("betting", "WalletLedgerEntry")
            WalletLedgerEntry.objects.create(
                wallet=locked,
                user=locked.user,
                actor=actor if getattr(actor, "is_authenticated", False) else None,
                transaction=transaction_obj,
                direction="credit" if amount_q >= 0 else "debit",
                amount=abs(amount_q),
                balance_before=before,
                balance_after=after,
                reference=(reference or "")[:120],
                reason=(reason or "")[:255],
                metadata=metadata or {},
            )

            metadata_map = metadata or {}
            source = metadata_map.get("source")
            loan_id = metadata_map.get("loan_id")
            if amount_q > 0 and loan_id and source in {"loan_approval", "manual_overdraft"}:
                locked._allocate_wallet_resident_loan_amount(loan_id=loan_id, amount=amount_q)
            elif amount_q < 0:
                locked._consume_wallet_resident_loan_amount(balance_after=after)

            self.balance = locked.balance
            return before, after

    def __str__(self):
        return f"Wallet for {self.user.email} (Balance: {self.balance})"


class WalletLedgerEntry(models.Model):
    DIRECTION_CHOICES = (
        ("credit", "Credit"),
        ("debit", "Debit"),
    )

    wallet = models.ForeignKey("Wallet", on_delete=models.CASCADE, related_name="ledger_entries")
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="wallet_ledger_entries")
    actor = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="wallet_ledger_actions")
    transaction = models.ForeignKey("Transaction", on_delete=models.SET_NULL, null=True, blank=True, related_name="wallet_ledger_entries")
    direction = models.CharField(max_length=10, choices=DIRECTION_CHOICES, db_index=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    balance_before = models.DecimalField(max_digits=12, decimal_places=2)
    balance_after = models.DecimalField(max_digits=12, decimal_places=2)
    reference = models.CharField(max_length=120, blank=True, default="", db_index=True)
    reason = models.CharField(max_length=255, blank=True, default="")
    metadata = models.JSONField(blank=True, default=dict)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user", "created_at"], name="bet_wl_user_created_idx"),
            models.Index(fields=["wallet", "created_at"], name="bet_wl_wallet_created_idx"),
        ]

    def __str__(self):
        return f"WalletLedger({self.user_id}) {self.direction} ₦{self.amount}"

class Transaction(models.Model):
    TRANSACTION_TYPES = (
        ('deposit', 'Deposit'),
        ('withdrawal', 'Withdrawal'),
        ('bet_placement', 'Bet Placement'),
        ('bet_payout', 'Bet Payout'),
        ('commission_payout', 'Commission Payout'),
        ('commission_recall_debit', 'Commission Recall Debit'),
        ('commission_recall_credit', 'Commission Recall Credit'),
        ('wallet_transfer_out', 'Wallet Transfer Out'),
        ('wallet_transfer_in', 'Wallet Transfer In'),
        ('bonus', 'Bonus'),
        ('ticket_deletion_refund', 'Ticket Deletion Refund'),
        ('withdrawal_refund', 'Withdrawal Refund'),
        ('account_user_debit', 'Account User Debit'),
        ('account_user_credit', 'Account User Credit'),
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
    payment_gateway = models.CharField(max_length=20, choices=[('paystack', 'Paystack'), ('monnify', 'Monnify'), ('kora', 'Kora')], default='paystack')
    paystack_reference = models.CharField(max_length=100, blank=True, null=True, unique=True)
    external_reference = models.CharField(max_length=100, blank=True, null=True, unique=True)

    class Meta:
        ordering = ['-timestamp']

    def __str__(self):
        return f"{self.transaction_type} - {self.user.email} - {self.amount} ({self.status})"


class TicketTransactionLedger(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='ticket_transaction_ledgers')
    ticket = models.ForeignKey('BetTicket', on_delete=models.SET_NULL, null=True, blank=True, related_name='transaction_ledgers')
    transaction = models.ForeignKey('Transaction', on_delete=models.SET_NULL, null=True, blank=True, related_name='ticket_transaction_ledgers')
    wallet_ledger_entry = models.OneToOneField('WalletLedgerEntry', on_delete=models.SET_NULL, null=True, blank=True, related_name='ticket_transaction_ledger')
    event_key = models.CharField(max_length=120, unique=True)
    reference = models.CharField(max_length=120, blank=True, default='', db_index=True)
    transaction_type = models.CharField(max_length=80, db_index=True)
    source = models.CharField(max_length=80, db_index=True)
    description = models.TextField(blank=True, default='')
    debit = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    credit = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    balance_before = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    balance_after = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='ticket_transaction_ledgers_created')
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    metadata = models.JSONField(blank=True, default=dict)
    created_at = models.DateTimeField(db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at', '-id']
        indexes = [
            models.Index(fields=['user', 'created_at'], name='bet_ttl_user_created_idx'),
            models.Index(fields=['user', 'transaction_type'], name='bet_ttl_user_type_idx'),
            models.Index(fields=['user', 'source'], name='bet_ttl_user_source_idx'),
            models.Index(fields=['ticket', 'created_at'], name='bet_ttl_ticket_created_idx'),
            models.Index(fields=['transaction', 'created_at'], name='bet_ttl_tx_created_idx'),
        ]

    def __str__(self):
        return f"TicketTxnLedger({self.user_id}) {self.transaction_type} {self.reference}"


class PaymentGatewayDeposit(Transaction):
    class Meta:
        proxy = True
        verbose_name = "Payment Gateway Deposit"
        verbose_name_plural = "Payment Gateway Deposit"


class CashierRegistrationRequest(models.Model):
    STATUS_CHOICES = (
        ('PENDING', 'Pending'),
        ('APPROVED', 'Approved'),
        ('REJECTED', 'Rejected'),
    )

    agent = models.ForeignKey(User, on_delete=models.CASCADE, related_name='cashier_registration_requests', limit_choices_to={'user_type': 'agent'})

    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)
    other_name = models.CharField(max_length=100)
    phone_number = models.CharField(max_length=20, blank=True, null=True)

    cashier_code = models.CharField(max_length=10)
    cashier_email = models.EmailField()
    cashier_username = models.CharField(max_length=50)
    cashier_prefix = models.CharField(max_length=10, blank=True, null=True)

    created_cashier = models.OneToOneField(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='created_from_cashier_request')

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='PENDING')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    admin_notes = models.TextField(blank=True, null=True)

    class Meta:
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(fields=['agent', 'cashier_code'], name='unique_cashier_request_agent_code'),
        ]

    def __str__(self):
        return f"{self.agent.email} - {self.cashier_code} - {self.status}"


class PendingCashierRegistration(CashierRegistrationRequest):
    class Meta:
        proxy = True
        verbose_name = "Pending Cashier Registration"
        verbose_name_plural = "Pending Cashier Registration"


class ApprovedNewCashier(CashierRegistrationRequest):
    class Meta:
        proxy = True
        verbose_name = "Approved New Cashier"
        verbose_name_plural = "Approved New Cashier"

class BettingPeriod(models.Model):
    DEFAULT_FIXTURE_THEME_COLOR = "#0b4f3a"

    name = models.CharField(max_length=255, unique=True)
    start_date = models.DateField()
    end_date = models.DateField()
    is_active = models.BooleanField(default=True)
    fixture_theme_color = models.CharField(
        max_length=7,
        default=DEFAULT_FIXTURE_THEME_COLOR,
        validators=[
            RegexValidator(
                regex=r"^#[0-9A-Fa-f]{6}$",
                message="Enter a valid hex color in the format #RRGGBB.",
            )
        ],
        help_text="Hex color used for fixture page section headers for this betting period.",
    )

    class Meta:
        ordering = ['-start_date']

    def __str__(self):
        return self.name

    @property
    def resolved_fixture_theme_color(self):
        return self.fixture_theme_color or self.DEFAULT_FIXTURE_THEME_COLOR

    @property
    def fixture_theme_text_color(self):
        color = (self.resolved_fixture_theme_color or self.DEFAULT_FIXTURE_THEME_COLOR).lstrip("#")
        try:
            red = int(color[0:2], 16)
            green = int(color[2:4], 16)
            blue = int(color[4:6], 16)
        except (TypeError, ValueError):
            return "#ffffff"

        brightness = ((red * 299) + (green * 587) + (blue * 114)) / 1000
        return "#111827" if brightness >= 186 else "#ffffff"

class Fixture(models.Model):
    ODDS_UPDATE_DIRECTION_CHOICES = (
        ('up', 'Up'),
        ('down', 'Down'),
        ('mixed', 'Mixed'),
    )
    STATUS_CHOICES = (
        ('scheduled', 'Scheduled'),
        ('live', 'Live'),
        ('finished', 'Finished'),
        ('postponed', 'Postponed'),
        ('cancelled', 'Cancelled'),
        ('settled', 'Settled'),
        ('abandoned', 'Abandoned'),
        ('no_result', 'No Result'),
    )
    betting_period = models.ForeignKey(BettingPeriod, on_delete=models.CASCADE, related_name='fixtures')
    serial_number = models.PositiveIntegerField()
    home_team = models.CharField(max_length=255)
    away_team = models.CharField(max_length=255)
    match_date = models.DateField()
    match_time = models.TimeField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='scheduled')
    home_score = models.IntegerField(null=True, blank=True)
    away_score = models.IntegerField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    odds_updated_at = models.DateTimeField(null=True, blank=True, db_index=True)
    datetime_updated_at = models.DateTimeField(null=True, blank=True, db_index=True)
    odds_update_direction = models.CharField(max_length=10, choices=ODDS_UPDATE_DIRECTION_CHOICES, blank=True, default='')

    # Odds Fields
    home_win_odd = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    draw_odd = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    away_win_odd = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    home_or_draw_odd = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    either_team_win_odd = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    away_or_draw_odd = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    over_1_5_odd = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    under_1_5_odd = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    over_2_5_odd = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    under_2_5_odd = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    over_3_5_odd = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    under_3_5_odd = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    btts_yes_odd = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    btts_no_odd = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    home_dnb_odd = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    away_dnb_odd = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)

    def __str__(self):
        return f"{self.home_team} vs {self.away_team}"

class PopularPick(models.Model):
    BET_TYPE_CHOICES = (
        ('home_win', 'Home Win'),
        ('draw', 'Draw'),
        ('away_win', 'Away Win'),
        ('home_or_draw', 'Home Win or Draw (1X)'),
        ('either_team_win', 'Anybody Wins (12)'),
        ('away_or_draw', 'Away Win or Draw (X2)'),
        ('home_dnb', 'Home DNB'),
        ('away_dnb', 'Away DNB'),
        ('over_1_5', 'Over 1.5'),
        ('under_1_5', 'Under 1.5'),
        ('over_2_5', 'Over 2.5'),
        ('under_2_5', 'Under 2.5'),
        ('over_3_5', 'Over 3.5'),
        ('under_3_5', 'Under 3.5'),
        ('btts_yes', 'BTTS Yes'),
        ('btts_no', 'BTTS No'),
    )

    fixture = models.OneToOneField(Fixture, on_delete=models.CASCADE, related_name='popular_pick')
    bet_type = models.CharField(max_length=50, choices=BET_TYPE_CHOICES)
    is_active = models.BooleanField(default=True)
    sort_order = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ('sort_order', '-created_at')
        verbose_name = "Popular Pick"
        verbose_name_plural = "Popular Picks"

    def __str__(self):
        return f"{self.fixture} - {self.bet_type}"

    @property
    def odd_value(self):
        field_map = {
            'home_win': 'home_win_odd',
            'draw': 'draw_odd',
            'away_win': 'away_win_odd',
            'home_or_draw': 'home_or_draw_odd',
            'either_team_win': 'either_team_win_odd',
            'away_or_draw': 'away_or_draw_odd',
            'home_dnb': 'home_dnb_odd',
            'away_dnb': 'away_dnb_odd',
            'over_1_5': 'over_1_5_odd',
            'under_1_5': 'under_1_5_odd',
            'over_2_5': 'over_2_5_odd',
            'under_2_5': 'under_2_5_odd',
            'over_3_5': 'over_3_5_odd',
            'under_3_5': 'under_3_5_odd',
            'btts_yes': 'btts_yes_odd',
            'btts_no': 'btts_no_odd',
        }
        field_name = field_map.get(self.bet_type)
        if not field_name or not self.fixture_id:
            return None
        return getattr(self.fixture, field_name, None)

    @property
    def market_label(self):
        if self.bet_type in ('home_win', 'draw', 'away_win'):
            return '1x2'
        if self.bet_type in ('home_or_draw', 'either_team_win', 'away_or_draw'):
            return 'Double Chance'
        if self.bet_type in ('home_dnb', 'away_dnb'):
            return 'DNB'
        if self.bet_type.startswith('over_') or self.bet_type.startswith('under_'):
            return 'Goals'
        if self.bet_type.startswith('btts_'):
            return 'BTTS'
        return 'Pick'

    @property
    def selection_label(self):
        if self.bet_type == 'home_win':
            return '1'
        if self.bet_type == 'draw':
            return 'X'
        if self.bet_type == 'away_win':
            return '2'
        if self.bet_type == 'home_or_draw':
            return '1X'
        if self.bet_type == 'either_team_win':
            return '12'
        if self.bet_type == 'away_or_draw':
            return 'X2'
        if self.bet_type == 'home_dnb':
            return 'Home DNB'
        if self.bet_type == 'away_dnb':
            return 'Away DNB'
        if self.bet_type == 'btts_yes':
            return 'Yes'
        if self.bet_type == 'btts_no':
            return 'No'
        display_map = {
            'over_1_5': 'Over 1.5',
            'under_1_5': 'Under 1.5',
            'over_2_5': 'Over 2.5',
            'under_2_5': 'Under 2.5',
            'over_3_5': 'Over 3.5',
            'under_3_5': 'Under 3.5',
        }
        return display_map.get(self.bet_type, self.bet_type.replace('_', ' ').title())

class Selection(models.Model):
    bet_ticket = models.ForeignKey('BetTicket', on_delete=models.CASCADE, related_name='selections')
    fixture = models.ForeignKey(Fixture, on_delete=models.SET_NULL, null=True, blank=True, related_name='selections')
    betting_period = models.ForeignKey('BettingPeriod', on_delete=models.SET_NULL, null=True, blank=True, related_name='selections')
    fixture_serial_number = models.CharField(max_length=50, blank=True, default='', db_index=True)
    fixture_home_team = models.CharField(max_length=255, blank=True, default='')
    fixture_away_team = models.CharField(max_length=255, blank=True, default='')
    fixture_match_date = models.DateField(null=True, blank=True)
    fixture_match_time = models.TimeField(null=True, blank=True)
    bet_type = models.CharField(max_length=50)
    odd_selected = models.DecimalField(max_digits=10, decimal_places=2)
    is_winning_selection = models.BooleanField(null=True, blank=True)

    def get_bet_type_display(self):
        return self.bet_type.replace('_', ' ').title()

    def __str__(self):
        if self.fixture_id:
            return f"{self.fixture} - {self.bet_type}"
        label = f"{self.fixture_home_team} vs {self.fixture_away_team}".strip()
        return f"{label or 'Fixture'} - {self.bet_type}"

class BetTicket(models.Model):
    STATUS_CHOICES = (
        ('pending', 'Pending'),
        ('won', 'Won'),
        ('lost', 'Lost'),
        ('cashed_out', 'Cashed Out'),
        ('deleted', 'Deleted'),
        ('cancelled', 'Cancelled'),
    )
    VOIDED_STATUSES = ('deleted', 'cancelled')

    BET_TYPE_CHOICES = (
        ('single', 'Single'),
        ('multiple', 'Multiple'),
        ('system', 'System'),
    )
    BONUS_BASE_CHOICES = (
        ('gross', 'Gross Winnings (Total Return)'),
        ('net', 'Net Winnings (Return - Stake)'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    ticket_id = models.CharField(max_length=8, unique=True, editable=False, null=True)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='bet_tickets')
    bet_type = models.CharField(max_length=20, choices=BET_TYPE_CHOICES, default='single')
    system_min_count = models.PositiveIntegerField(null=True, blank=True, help_text="Minimum selections required for system bet (k in k/n)")
    original_selections_count = models.PositiveIntegerField(null=True, blank=True, db_index=True)
    placed_ip = models.GenericIPAddressField(null=True, blank=True)
    
    stake_amount = models.DecimalField(max_digits=10, decimal_places=2, validators=[MinValueValidator(Decimal('0.01'))], db_index=True)
    total_odd = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('1.00'))
    potential_winning = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    min_winning = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    max_winning = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending', db_index=True)

    bonus_rule = models.ForeignKey('BonusRule', on_delete=models.SET_NULL, null=True, blank=True, related_name='tickets')
    bonus_percentage_applied = models.DecimalField(max_digits=6, decimal_places=4, default=Decimal('0.0000'), validators=[MinValueValidator(Decimal('0.0000'))])
    bonus_base = models.CharField(max_length=10, choices=BONUS_BASE_CHOICES, default='gross')
    bonus_base_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'), validators=[MinValueValidator(Decimal('0.00'))])
    bonus_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'), validators=[MinValueValidator(Decimal('0.00'))])
    bonus_is_final = models.BooleanField(default=False)
    bonus_applied_at = models.DateTimeField(null=True, blank=True, db_index=True)

    payout_processed = models.BooleanField(default=False, db_index=True)

    betting_limits_snapshot = models.JSONField(blank=True, default=dict)
    
    placed_at = models.DateTimeField(auto_now_add=True, db_index=True)
    last_updated = models.DateTimeField(auto_now=True, db_index=True)
    
    deleted_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='deleted_tickets')
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-placed_at']
        verbose_name_plural = "Bet Tickets"

    def __str__(self):
        return f"Ticket {self.id} by {self.user.email} - Stake: {self.stake_amount} - Status: {self.status}"

    @classmethod
    def is_voided_status_value(cls, status):
        return (status or '').strip().lower() in cls.VOIDED_STATUSES

    @property
    def is_voided(self):
        return self.is_voided_status_value(self.status)

    @property
    def display_status_label(self):
        return 'Voided' if self.is_voided else self.get_status_display()

    RESULT_REFUND_TRANSACTION_TYPES = (
        'ticket_deletion_refund',
        'ticket_cancellation_refund',
        'fixture_deletion_refund',
    )

    def save(self, *args, **kwargs):
        if not self.ticket_id:
            while True:
                new_id = ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(6))
                if not BetTicket.objects.filter(ticket_id=new_id).exists():
                    self.ticket_id = new_id
                    break
        super().save(*args, **kwargs)

    def _snapshot_ticket_odds_value(self):
        snapshot = self.betting_limits_snapshot or {}
        raw_value = snapshot.get('ticket_odds')
        if raw_value in (None, ''):
            return None
        try:
            return Decimal(str(raw_value)).quantize(Decimal('0.01'))
        except Exception:
            return None

    def _selection_odds_for_display(self):
        odds = []
        selection_values = list(self.selections.values_list('odd_selected', flat=True))
        if selection_values:
            for odd in selection_values:
                try:
                    odds.append(Decimal(str(odd)))
                except Exception:
                    continue
            return odds

        snapshot = (self.betting_limits_snapshot or {}).get('selections_snapshot') or []
        for item in snapshot:
            raw_odd = item.get('odd_selected')
            if raw_odd in (None, ''):
                continue
            try:
                odds.append(Decimal(str(raw_odd)))
            except Exception:
                continue
        return odds

    def get_display_total_odd(self):
        try:
            stored_total = Decimal(str(self.total_odd or Decimal('0.00'))).quantize(Decimal('0.01'))
        except Exception:
            stored_total = Decimal('0.00')

        if stored_total > Decimal('0.00'):
            return stored_total

        snapshot_total = self._snapshot_ticket_odds_value()
        if snapshot_total and snapshot_total > Decimal('0.00'):
            return snapshot_total

        odds = self._selection_odds_for_display()
        if not odds:
            return stored_total

        if self.bet_type == 'system' and self.system_min_count:
            k = int(self.system_min_count or 0)
            if k <= 0 or len(odds) < k:
                return stored_total
            computed_total = Decimal('1.00')
            for odd in sorted(odds, reverse=True)[:k]:
                computed_total *= odd
            return computed_total.quantize(Decimal('0.01'))

        computed_total = Decimal('1.00')
        for odd in odds:
            computed_total *= odd
        return computed_total.quantize(Decimal('0.01'))

    def calculate_total_odd_and_potential_winning(self):
        calculated_odd = Decimal('1.00')
        for selection in self.selections.all(): 
            calculated_odd *= selection.odd_selected
        
        self.total_odd = calculated_odd.quantize(Decimal('0.01'))

        self.potential_winning = (self.stake_amount * self.total_odd).quantize(Decimal('0.01'))

        self.max_winning = self.potential_winning

        snapshot_limit = None
        try:
            snapshot_limit = self.betting_limits_snapshot.get('max_winning')
        except Exception:
            snapshot_limit = None

        if snapshot_limit is not None:
            try:
                self.max_winning = min(self.max_winning, Decimal(str(snapshot_limit))).quantize(Decimal('0.01'))
                return
            except Exception:
                pass

        max_winning_setting = SystemSetting.objects.filter(key='max_winning_per_ticket').first()
        if max_winning_setting and max_winning_setting.value:
            try:
                max_winning_limit = Decimal(max_winning_setting.value)
                self.max_winning = min(self.max_winning, max_winning_limit).quantize(Decimal('0.01'))
            except Exception:
                self.max_winning = self.potential_winning

    def get_min_potential_winning(self):
        if self.bet_type != 'system' or not self.system_min_count:
            return self.max_winning

        all_selections = list(self.selections.all())
        k = int(self.system_min_count or 0)
        n = len(all_selections)
        if n < k or k <= 0:
            return Decimal('0.00')

        try:
            num_lines = math.comb(n, k)
        except Exception:
            return Decimal('0.00')
        if not num_lines:
            return Decimal('0.00')

        stake_per_line = (self.stake_amount / Decimal(num_lines)).quantize(Decimal('0.01'))

        odds = []
        for sel in all_selections:
            o = sel.odd_selected
            if getattr(sel, 'is_winning_selection', None) is None:
                o = Decimal('1.00')
            odds.append(o)
        odds.sort()
        min_line_odd = Decimal('1.00')
        for o in odds[:k]:
            min_line_odd *= o

        return (stake_per_line * min_line_odd).quantize(Decimal('0.01'))

    def recalculate_ticket(self):
        """
        Recalculates ticket odds, winnings, and bonus based on valid (non-postponed) events.
        """
        if self.status != 'pending':
            return

        from django.apps import apps
        BonusRule = apps.get_model('betting', 'BonusRule')
        SystemSetting = apps.get_model('betting', 'SystemSetting')
        ActivityLog = apps.get_model('betting', 'ActivityLog')
        
        # Define void statuses
        void_statuses = ['postponed', 'cancelled', 'abandoned', 'no_result']
        
        # 1. Identify valid selections
        all_selections = list(self.selections.select_related('fixture').all())
        valid_selections = []
        void_selections = []
        
        for selection in all_selections:
            fixture_status = getattr(selection.fixture, 'status', None)
            if fixture_status in void_statuses:
                void_selections.append(selection)
            else:
                valid_selections.append(selection)
                
        num_void = len(void_selections)
        num_valid = len(valid_selections)
        
        # If all void, ticket is cancelled
        if num_valid == 0:
            if self.status != 'cancelled':
                old_status = self.status
                self.status = 'cancelled'
                self.potential_winning = self.stake_amount 
                self.max_winning = self.stake_amount
                self.total_odd = Decimal('1.00')
                self.bonus_base_amount = Decimal('0.00')
                self.bonus_amount = Decimal('0.00')
                self.bonus_is_final = True
                self.bonus_applied_at = None
                self.save(update_fields=['status', 'potential_winning', 'max_winning', 'total_odd', 'bonus_base_amount', 'bonus_amount', 'bonus_is_final', 'bonus_applied_at'])
                
                # Log cancellation
                ActivityLog.objects.create(
                    user=self.user,
                    action_type='UPDATE',
                    action=f"Ticket {self.ticket_id} voided (All events void)",
                    affected_object=f"BetTicket: {self.ticket_id}"
                )
            return

        # Capture old values for logging
        old_potential = self.potential_winning
        old_max = self.max_winning

        # 2. Recalculate based on Ticket Type
        if self.bet_type == 'system' and self.system_min_count:
            k = self.system_min_count
            n = len(all_selections)
            if k and n >= k:
                num_lines = math.comb(n, k)
                stake_per_line = (self.stake_amount / Decimal(num_lines)) if num_lines else Decimal('0.00')

                dp = [Decimal('0.00')] * (k + 1)
                dp[0] = Decimal('1.00')
                count = 0
                for sel in all_selections:
                    fixture_status = getattr(sel.fixture, 'status', None)
                    o = Decimal('1.00') if fixture_status in void_statuses else sel.odd_selected
                    count += 1
                    upper = min(k, count)
                    for j in range(upper, 0, -1):
                        dp[j] = dp[j] + (dp[j - 1] * o)

                self.potential_winning = (stake_per_line * dp[k]).quantize(Decimal('0.01'))
                self.total_odd = Decimal('0.00')
            else:
                self.potential_winning = Decimal('0.00')
                self.total_odd = Decimal('0.00')

        else: # Single or Multiple
            new_total_odd = Decimal('1.00')
            for sel in all_selections:
                fixture_status = getattr(sel.fixture, 'status', None)
                if fixture_status in void_statuses:
                    new_total_odd *= Decimal('1.00')
                else:
                    new_total_odd *= sel.odd_selected
            
            self.total_odd = new_total_odd.quantize(Decimal('0.01'))
            self.potential_winning = (self.stake_amount * self.total_odd).quantize(Decimal('0.01'))

        bonus_amount = Decimal('0.00')
        if self.bonus_rule_id and self.bonus_percentage_applied and self.bonus_percentage_applied > 0 and self.status == 'pending':
            rule = self.bonus_rule
            if rule:
                effective_count = self.original_selections_count or len(all_selections)
                effective_count = max(0, int(effective_count) - int(num_void))

                qualifies = effective_count >= rule.min_selections and (rule.max_selections is None or effective_count <= rule.max_selections)
                if qualifies:
                    base_amount = self.potential_winning
                    if (self.bonus_base or rule.bonus_base) == 'net':
                        base_amount = self.potential_winning - self.stake_amount
                        if base_amount < 0:
                            base_amount = Decimal('0.00')

                    bonus_amount = (base_amount * self.bonus_percentage_applied).quantize(Decimal('0.01'))
                    if rule.max_bonus_cap is not None:
                        bonus_amount = min(bonus_amount, rule.max_bonus_cap)
        
        # 4. Finalize Max Winning
        self.max_winning = self.potential_winning + bonus_amount
        
        # Calculate min_winning for system bets during recalculation
        if self.bet_type == 'system' and self.system_min_count:
            min_potential = self.get_min_potential_winning()
            min_bonus = Decimal('0.00')
            if self.bonus_rule_id and self.bonus_percentage_applied and self.bonus_percentage_applied > 0:
                rule = self.bonus_rule
                if rule:
                    min_base = min_potential
                    if (self.bonus_base or rule.bonus_base) == 'net':
                        min_base = max(Decimal('0.00'), min_potential - self.stake_amount)
                    min_bonus = (min_base * self.bonus_percentage_applied).quantize(Decimal('0.01'))
                    if rule.max_bonus_cap is not None:
                        min_bonus = min(min_bonus, rule.max_bonus_cap)
            self.min_winning = min_potential + min_bonus
        else:
            self.min_winning = self.max_winning
        
        snapshot_limit = None
        try:
            snapshot_limit = self.betting_limits_snapshot.get('max_winning')
        except Exception:
            snapshot_limit = None

        if snapshot_limit is not None:
            try:
                self.max_winning = min(self.max_winning, Decimal(str(snapshot_limit)))
            except Exception:
                pass
        else:
            max_winning_setting = SystemSetting.objects.filter(key='max_winning_per_ticket').first()
            if max_winning_setting and max_winning_setting.value:
                try:
                    limit = Decimal(max_winning_setting.value)
                    self.max_winning = min(self.max_winning, limit)
                except Exception:
                    pass
        
        self.save()
        
        # Log if values changed
        if old_potential != self.potential_winning or old_max != self.max_winning:
            ActivityLog.objects.create(
                user=self.user,
                action_type='RECALCULATION',
                action=f"Ticket {self.ticket_id} recalculated. Void: {num_void}. Potential: {old_potential}->{self.potential_winning}. Max: {old_max}->{self.max_winning}",
                affected_object=f"BetTicket: {self.ticket_id}"
            )

    def has_computed_results(self):
        return self.selections.filter(fixture__status__in=['finished', 'settled']).exists()

    def check_and_update_status(self):
        if self.status == 'pending':
            void_statuses = ['cancelled', 'postponed', 'abandoned', 'no_result']
            selections = list(self.selections.select_related('fixture').all())

            all_fixtures_settled = True
            to_update = []

            for selection in selections:
                fixture = selection.fixture
                new_value = selection.is_winning_selection
                if fixture is None:
                    all_fixtures_settled = False
                    continue

                if fixture.status in void_statuses:
                    new_value = None
                elif fixture.status not in ['settled', 'finished']:
                    all_fixtures_settled = False
                    continue
                elif fixture.home_score is None or fixture.away_score is None:
                    all_fixtures_settled = False
                    continue
                else:
                    total_goals = fixture.home_score + fixture.away_score
                    if selection.bet_type == 'home_win':
                        new_value = fixture.home_score > fixture.away_score
                    elif selection.bet_type == 'draw':
                        new_value = fixture.home_score == fixture.away_score
                    elif selection.bet_type == 'away_win':
                        new_value = fixture.home_score < fixture.away_score
                    elif selection.bet_type == 'home_or_draw':
                        new_value = fixture.home_score >= fixture.away_score
                    elif selection.bet_type == 'either_team_win':
                        new_value = fixture.home_score != fixture.away_score
                    elif selection.bet_type == 'away_or_draw':
                        new_value = fixture.home_score <= fixture.away_score
                    elif selection.bet_type == 'over_1_5':
                        new_value = total_goals > Decimal('1.5')
                    elif selection.bet_type == 'under_1_5':
                        new_value = total_goals <= Decimal('1.5')
                    elif selection.bet_type == 'over_2_5':
                        new_value = total_goals > Decimal('2.5')
                    elif selection.bet_type == 'under_2_5':
                        new_value = total_goals <= Decimal('2.5')
                    elif selection.bet_type == 'over_3_5':
                        new_value = total_goals > Decimal('3.5')
                    elif selection.bet_type == 'under_3_5':
                        new_value = total_goals <= Decimal('3.5')
                    elif selection.bet_type == 'btts_yes':
                        new_value = fixture.home_score > 0 and fixture.away_score > 0
                    elif selection.bet_type == 'btts_no':
                        new_value = fixture.home_score == 0 or fixture.away_score == 0
                    elif selection.bet_type == 'home_dnb':
                        new_value = None if fixture.home_score == fixture.away_score else (fixture.home_score > fixture.away_score)
                    elif selection.bet_type == 'away_dnb':
                        new_value = None if fixture.home_score == fixture.away_score else (fixture.home_score < fixture.away_score)
                    else:
                        new_value = False

                if selection.is_winning_selection != new_value:
                    selection.is_winning_selection = new_value
                    to_update.append(selection)

            if to_update:
                Selection.objects.bulk_update(to_update, ['is_winning_selection'])

            if self.bet_type == 'system' and self.system_min_count:
                if not all_fixtures_settled:
                    return

                n = len(selections)
                k = int(self.system_min_count or 0)
                if not k or n < k:
                    return

                num_lines = math.comb(n, k)
                stake_per_line = (self.stake_amount / Decimal(num_lines)) if num_lines else Decimal('0.00')

                void_count = 0
                odds = []
                for s in selections:
                    if s.is_winning_selection is False:
                        continue
                    if s.is_winning_selection is None:
                        void_count += 1
                        odds.append(Decimal('1.00'))
                    else:
                        odds.append(s.odd_selected)

                if len(odds) < k:
                    self.status = 'lost'
                    self.total_odd = Decimal('0.00')
                    self.potential_winning = Decimal('0.00')
                else:
                    dp = [Decimal('0.00')] * (k + 1)
                    dp[0] = Decimal('1.00')
                    count = 0
                    for o in odds:
                        count += 1
                        upper = min(k, count)
                        for j in range(upper, 0, -1):
                            dp[j] = dp[j] + (dp[j - 1] * o)

                    winning_amount = (stake_per_line * dp[k]).quantize(Decimal('0.01'))
                    if winning_amount > 0:
                        self.status = 'won'
                        self.total_odd = Decimal('0.00')
                        self.potential_winning = winning_amount
                    else:
                        self.status = 'lost'
                        self.total_odd = Decimal('0.00')
                        self.potential_winning = Decimal('0.00')

            else:
                for s in selections:
                    if s.is_winning_selection is False:
                        self.status = 'lost'
                        self.potential_winning = Decimal('0.00')
                        self.total_odd = Decimal('0.00')
                        self.max_winning = Decimal('0.00')
                        self.bonus_base_amount = Decimal('0.00')
                        self.bonus_amount = Decimal('0.00')
                        self.bonus_is_final = True
                        self.bonus_applied_at = None
                        self.save()
                        return

                if not all_fixtures_settled:
                    return

                total_odd_settled = Decimal('1.00')
                void_count = 0
                non_void_odds = []

                for s in selections:
                    if s.is_winning_selection is None:
                        void_count += 1
                        total_odd_settled *= Decimal('1.00')
                    else:
                        total_odd_settled *= s.odd_selected
                        non_void_odds.append(s.odd_selected)

                self.status = 'won'
                self.total_odd = total_odd_settled.quantize(Decimal('0.01'))
                self.potential_winning = (self.stake_amount * self.total_odd).quantize(Decimal('0.01'))

            old_bonus = self.bonus_amount
            bonus_amount = Decimal('0.00')
            bonus_base_amount = Decimal('0.00')

            if self.status == 'won' and self.status != 'cashed_out' and self.bonus_rule_id and self.bonus_percentage_applied and self.bonus_percentage_applied > 0:
                rule = self.bonus_rule
                if rule:
                    current_void_count = sum(1 for s in selections if s.is_winning_selection is None)
                    effective_count = self.original_selections_count or len(selections)
                    effective_count = max(0, int(effective_count) - int(current_void_count))

                    qualifies = effective_count >= rule.min_selections and (rule.max_selections is None or effective_count <= rule.max_selections)

                    odds_to_check = [s.odd_selected for s in selections if s.is_winning_selection is not None]
                    odds_ok = True if not odds_to_check else (min(odds_to_check) >= rule.min_odd_per_selection)

                    if qualifies and odds_ok:
                        bonus_base_amount = self.potential_winning
                        if (self.bonus_base or rule.bonus_base) == 'net':
                            bonus_base_amount = self.potential_winning - self.stake_amount
                            if bonus_base_amount < 0:
                                bonus_base_amount = Decimal('0.00')

                        bonus_amount = (bonus_base_amount * self.bonus_percentage_applied).quantize(Decimal('0.01'))
                        if rule.max_bonus_cap is not None:
                            bonus_amount = min(bonus_amount, rule.max_bonus_cap)

            self.bonus_base_amount = bonus_base_amount
            self.bonus_amount = bonus_amount
            self.bonus_is_final = True
            if bonus_amount > 0:
                if not self.bonus_applied_at:
                    self.bonus_applied_at = timezone.now()
            else:
                self.bonus_applied_at = None

            self.max_winning = (self.potential_winning + bonus_amount).quantize(Decimal('0.01'))

            snapshot_limit = None
            try:
                snapshot_limit = self.betting_limits_snapshot.get('max_winning')
            except Exception:
                snapshot_limit = None

            if snapshot_limit is not None:
                try:
                    self.max_winning = min(self.max_winning, Decimal(str(snapshot_limit)))
                except Exception:
                    pass
            else:
                max_winning_setting = SystemSetting.objects.filter(key='max_winning_per_ticket').first()
                if max_winning_setting and max_winning_setting.value:
                    try:
                        limit = Decimal(max_winning_setting.value)
                        self.max_winning = min(self.max_winning, limit)
                    except Exception:
                        pass

            self.save()

            if bonus_amount > 0 and (not old_bonus or old_bonus != bonus_amount):
                ActivityLog.objects.create(
                    user=self.user,
                    action_type='BONUS_APPLIED',
                    action=f"Bonus applied on ticket {self.ticket_id}. Base: {bonus_base_amount} Pct: {self.bonus_percentage_applied} Bonus: {bonus_amount} Final: {self.max_winning}",
                    affected_object=f"BetTicket: {self.ticket_id}",
                    ip_address=self.placed_ip
                )

            if self.status == 'won':
                with transaction.atomic():
                    locked_ticket = BetTicket.objects.select_for_update().get(pk=self.pk)
                    if locked_ticket.payout_processed or locked_ticket.status != 'won':
                        return
                    wallet = Wallet.objects.select_for_update().get(user=self.user)
                    payout_tx = Transaction.objects.create(
                        user=self.user,
                        initiating_user=None,
                        transaction_type='bet_payout',
                        amount=locked_ticket.max_winning,
                        is_successful=True,
                        status='completed',
                        description=f"Winnings for ticket {locked_ticket.ticket_id}",
                        related_bet_ticket=locked_ticket,
                        timestamp=timezone.now()
                    )
                    wallet.apply_delta(
                        amount=locked_ticket.max_winning,
                        actor=None,
                        transaction_obj=payout_tx,
                        reference=str(locked_ticket.ticket_id),
                        reason=payout_tx.description,
                        metadata={
                            "ticket_id": locked_ticket.ticket_id,
                            "source": "ticket_settlement",
                        },
                    )

                    locked_ticket.payout_processed = True
                    locked_ticket.save(update_fields=['payout_processed'])

    def _get_active_result_side_effect_transactions(self):
        return list(
            self.transactions.filter(
                status='completed',
                is_successful=True,
                transaction_type__in=('bet_payout',) + self.RESULT_REFUND_TRANSACTION_TYPES,
            ).order_by('timestamp', 'id')
        )

    def reverse_result_side_effects(self, *, actor=None, reason=''):
        active_transactions = self._get_active_result_side_effect_transactions()
        if not active_transactions:
            return {'reversed_total': Decimal('0.00'), 'transactions': []}

        total_reversal = sum((tx.amount for tx in active_transactions), Decimal('0.00')).quantize(Decimal('0.01'))
        wallet = Wallet.objects.select_for_update().get(user=self.user)

        reversed_ids = []
        for tx in active_transactions:
            reversal_type = 'bet_payout_reversal' if tx.transaction_type == 'bet_payout' else 'ticket_refund_reversal'
            reversal_tx = Transaction.objects.create(
                user=self.user,
                initiating_user=actor if getattr(actor, 'is_authenticated', False) else None,
                target_user=self.user,
                transaction_type=reversal_type,
                amount=tx.amount,
                is_successful=True,
                status='completed',
                description=f"Result correction reversal of {tx.transaction_type} for ticket {self.ticket_id}. {reason}".strip(),
                related_bet_ticket=self,
                timestamp=timezone.now()
            )
            wallet.apply_delta(
                amount=-tx.amount,
                actor=actor if getattr(actor, 'is_authenticated', False) else None,
                transaction_obj=reversal_tx,
                reference=str(self.ticket_id),
                reason=reversal_tx.description,
                metadata={
                    'ticket_id': self.ticket_id,
                    'reversed_tx_id': str(tx.id),
                    'source': 'result_backfill',
                },
                allow_negative=True,
            )
            tx.status = 'reversed'
            tx.is_successful = False
            tx.save(update_fields=['status', 'is_successful'])
            reversed_ids.append(str(tx.id))

        return {'reversed_total': total_reversal, 'transactions': reversed_ids}

    def backfill_after_result_correction(self, *, actor=None, reason=''):
        if self.is_voided_status_value(self.status) or self.status == 'cashed_out':
            return False

        old_status = self.status
        old_max_winning = Decimal(str(self.max_winning or Decimal('0.00'))).quantize(Decimal('0.01'))
        old_payout_processed = bool(self.payout_processed)

        with transaction.atomic():
            locked_ticket = BetTicket.objects.select_for_update().get(pk=self.pk)
            reversal_info = locked_ticket.reverse_result_side_effects(actor=actor, reason=reason)
            locked_ticket.status = 'pending'
            locked_ticket.payout_processed = False
            locked_ticket.bonus_base_amount = Decimal('0.00')
            locked_ticket.bonus_amount = Decimal('0.00')
            locked_ticket.bonus_is_final = False
            locked_ticket.bonus_applied_at = None
            locked_ticket.save(
                update_fields=[
                    'status',
                    'payout_processed',
                    'bonus_base_amount',
                    'bonus_amount',
                    'bonus_is_final',
                    'bonus_applied_at',
                    'last_updated',
                ]
            )

        self.refresh_from_db()
        self.recalculate_ticket()
        self.check_and_update_status()
        self.refresh_from_db()

        ActivityLog.objects.create(
            user=self.user,
            action_type='RESULT_BACKFILL',
            action=(
                f"Ticket {self.ticket_id} backfilled after result correction. "
                f"Status: {old_status} -> {self.status}. "
                f"Max winning: {old_max_winning} -> {self.max_winning}. "
                f"Prior payout processed: {old_payout_processed}. "
                f"Reversed total: {reversal_info['reversed_total']}."
            ),
            affected_object=f"BetTicket: {self.ticket_id}"
        )
        return True

class Result(Fixture):
    class Meta:
        proxy = True
        verbose_name = "Result"
        verbose_name_plural = "Results"


class BonusRule(models.Model):
    BONUS_BASE_CHOICES = (
        ('gross', 'Gross Winnings (Total Return)'),
        ('net', 'Net Winnings (Return - Stake)'),
    )

    name = models.CharField(max_length=255)
    is_active = models.BooleanField(default=True)

    min_selections = models.PositiveIntegerField(default=1, validators=[MinValueValidator(1)])
    max_selections = models.PositiveIntegerField(null=True, blank=True)

    min_odd_per_selection = models.DecimalField(max_digits=6, decimal_places=2, default=Decimal('1.01'), validators=[MinValueValidator(Decimal('1.01'))])
    bonus_percentage = models.DecimalField(max_digits=6, decimal_places=4, default=Decimal('0.0000'), validators=[MinValueValidator(Decimal('0.0000'))])
    max_bonus_cap = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, validators=[MinValueValidator(Decimal('0.00'))])
    bonus_base = models.CharField(max_length=10, choices=BONUS_BASE_CHOICES, default='gross')

    allow_system_bets = models.BooleanField(default=False)
    allow_accumulator_bets = models.BooleanField(default=True)
    allow_single_bets = models.BooleanField(default=True)

    class Meta:
        verbose_name_plural = "Bonus Rules"
        ordering = ['min_selections', 'max_selections', 'min_odd_per_selection']

    def clean(self):
        errors = {}

        if self.max_selections is not None and self.max_selections < self.min_selections:
            errors['max_selections'] = "Maximum selections must be greater than or equal to minimum selections."

        if not (self.allow_system_bets or self.allow_accumulator_bets or self.allow_single_bets):
            errors['allow_system_bets'] = "At least one bet type must be enabled."

        if errors:
            raise ValidationError(errors)

        def _overlaps(a_min, a_max, b_min, b_max):
            a_max_eff = a_max if a_max is not None else 10**9
            b_max_eff = b_max if b_max is not None else 10**9
            return not (a_max_eff < b_min or b_max_eff < a_min)

        qs = BonusRule.objects.filter(is_active=True).exclude(pk=self.pk)
        for other in qs:
            if not _overlaps(self.min_selections, self.max_selections, other.min_selections, other.max_selections):
                continue

            shares_system = self.allow_system_bets and other.allow_system_bets
            shares_acca = self.allow_accumulator_bets and other.allow_accumulator_bets
            shares_single = self.allow_single_bets and other.allow_single_bets

            if shares_system or shares_acca or shares_single:
                raise ValidationError("Overlapping active bonus ranges are not allowed for the same enabled bet type(s).")

    def __str__(self):
        max_part = f"{self.max_selections}" if self.max_selections is not None else "∞"
        pct = (self.bonus_percentage * Decimal('100')).quantize(Decimal('0.01'))
        return f"Bonus: {self.name} ({pct}% for {self.min_selections}-{max_part} selections)"

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
    request_time = models.DateTimeField(auto_now_add=True, db_index=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending', db_index=True)
    
    approved_rejected_time = models.DateTimeField(null=True, blank=True, db_index=True)
    approved_rejected_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='handled_withdrawals')
    admin_notes = models.TextField(blank=True, null=True)
    
    # Audit Fields
    balance_before = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, help_text="User balance before withdrawal deduction")
    balance_after = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, help_text="User balance after withdrawal deduction")
    
    # Approver Audit Fields (for Account Users/Admins)
    approver_balance_before = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, help_text="Approver's wallet balance before processing")
    approver_balance_after = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, help_text="Approver's wallet balance after processing")
    
    processed_ip = models.GenericIPAddressField(null=True, blank=True)
    email_request_user_sent_at = models.DateTimeField(null=True, blank=True, db_index=True)
    email_request_admin_sent_at = models.DateTimeField(null=True, blank=True, db_index=True)
    email_approved_user_sent_at = models.DateTimeField(null=True, blank=True, db_index=True)
    email_approved_admin_sent_at = models.DateTimeField(null=True, blank=True, db_index=True)
    email_completed_user_sent_at = models.DateTimeField(null=True, blank=True, db_index=True)
    email_completed_admin_sent_at = models.DateTimeField(null=True, blank=True, db_index=True)
    email_success_user_sent_at = models.DateTimeField(null=True, blank=True, db_index=True)
    email_success_admin_sent_at = models.DateTimeField(null=True, blank=True, db_index=True)
    email_rejected_user_sent_at = models.DateTimeField(null=True, blank=True, db_index=True)
    email_rejected_admin_sent_at = models.DateTimeField(null=True, blank=True, db_index=True)
    last_email_error = models.TextField(blank=True, default="")

    def __str__(self):
        return f"Withdrawal {self.id} - {self.user.email} - {self.amount}"

class WithdrawalReport(models.Model):
    EVENT_CHOICES = (
        ('requested', 'Requested'),
        ('approved', 'Approved'),
        ('rejected', 'Rejected'),
        ('completed', 'Completed'),
    )

    withdrawal = models.ForeignKey(UserWithdrawal, on_delete=models.CASCADE, related_name='report_entries')
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='withdrawal_reports')

    username = models.CharField(max_length=150, blank=True, default='')
    amount = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    bank_name = models.CharField(max_length=255, blank=True, default='')
    account_name = models.CharField(max_length=255, blank=True, default='')
    account_number = models.CharField(max_length=50, blank=True, default='')

    requested_at = models.DateTimeField(null=True, blank=True, db_index=True)
    updated_at = models.DateTimeField(null=True, blank=True, db_index=True)
    transaction_reference = models.CharField(max_length=120, blank=True, default='', db_index=True)
    withdrawal_status = models.CharField(max_length=20, blank=True, default='', db_index=True)

    event = models.CharField(max_length=20, choices=EVENT_CHOICES, db_index=True)
    is_admin_copy = models.BooleanField(default=False, db_index=True)

    email_subject = models.CharField(max_length=255, blank=True, default='')
    email_to = models.TextField(blank=True, default='')
    email_cc = models.TextField(blank=True, default='')
    email_bcc = models.TextField(blank=True, default='')
    email_body_text = models.TextField(blank=True, default='')
    email_body_html = models.TextField(blank=True, default='')
    email_sent_at = models.DateTimeField(null=True, blank=True, db_index=True)
    email_error = models.TextField(blank=True, default='')

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ('-created_at',)
        unique_together = ('withdrawal', 'event', 'is_admin_copy')
        verbose_name = "Withdrawal Report"
        verbose_name_plural = "Withdrawal Reports"

    def __str__(self):
        return f"{self.username or self.user_id or ''} • {self.event} • {self.withdrawal_status}"

class ProcessedWithdrawal(UserWithdrawal):
    class Meta:
        proxy = True
        verbose_name = "Processed Withdrawal"
        verbose_name_plural = "Processed Withdrawals"

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


class EmailAuditLog(models.Model):
    ACTION_TYPES = (
        ('DUPLICATE_EMAIL_ASSIGNED', 'Duplicate Email Assigned'),
        ('DUPLICATE_EMAIL_UPDATED', 'Duplicate Email Updated'),
        ('CASHIER_EMAIL_SYNCHRONIZED', 'Cashier Email Synchronized'),
        ('AGENT_CASHIER_EMAIL_SYNC_TRIGGERED', 'Agent Cashier Email Sync Triggered'),
    )

    target_user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='email_audit_logs')
    email = models.EmailField(blank=True, default='', db_index=True)
    action_type = models.CharField(max_length=50, choices=ACTION_TYPES, db_index=True)
    performed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='performed_email_audit_logs')
    metadata = models.JSONField(blank=True, default=dict)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.action_type} - {self.email or getattr(self.target_user, 'email', '')}"

class CRMActionLog(models.Model):
    ACTION_TYPES = (
        ('WITHDRAWAL_APPROVED', 'Withdrawal Approved'),
        ('WITHDRAWAL_REJECTED', 'Withdrawal Rejected'),
        ('USER_SUSPENDED', 'User Suspended'),
        ('USER_UNSUSPENDED', 'User Unsuspended'),
        ('PROFILE_EDITED', 'Profile Edited'),
        ('WITHDRAWAL_FROZEN', 'Withdrawals Frozen'),
        ('WITHDRAWAL_UNFROZEN', 'Withdrawals Unfrozen'),
        ('WALLET_CREDITED', 'Wallet Credited'),
        ('WALLET_DEBITED', 'Wallet Debited'),
        ('WALLET_CREDIT_REQUESTED', 'Wallet Credit Requested'),
        ('WALLET_DEBIT_REQUESTED', 'Wallet Debit Requested'),
        ('PASSWORD_RESET', 'Password Reset'),
        ('MESSAGE_SENT', 'Message Sent'),
        ('VIP_UPDATED', 'VIP/KYC Updated'),
        ('CASHIER_REG_APPROVED', 'Cashier Registration Approved'),
        ('CASHIER_REG_REJECTED', 'Cashier Registration Rejected'),
        ('AGENT_REG_APPROVED', 'Agent Registration Approved'),
        ('AGENT_REG_REJECTED', 'Agent Registration Rejected'),
    )

    actor = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='crm_actions')
    target_user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='crm_action_targets')
    action_type = models.CharField(max_length=50, choices=ACTION_TYPES, db_index=True)
    reason = models.CharField(max_length=255, blank=True, default='')
    notes = models.TextField(blank=True, default='')

    withdrawal = models.ForeignKey('UserWithdrawal', on_delete=models.SET_NULL, null=True, blank=True, related_name='crm_actions')
    ticket = models.ForeignKey('BetTicket', on_delete=models.SET_NULL, null=True, blank=True, related_name='crm_actions')
    cashier_request = models.ForeignKey('CashierRegistrationRequest', on_delete=models.SET_NULL, null=True, blank=True, related_name='crm_actions')
    pending_agent_registration = models.ForeignKey('pending_registration.PendingAgentRegistration', on_delete=models.SET_NULL, null=True, blank=True, related_name='crm_actions')

    data = models.JSONField(blank=True, default=dict)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        who = self.actor.email if self.actor else 'System'
        return f"{self.action_type} by {who}"

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


class RetailManagerMasterAgentMapping(models.Model):
    retail_manager = models.ForeignKey(User, on_delete=models.CASCADE, related_name='mapped_master_agents', limit_choices_to={'user_type': 'retail_manager'})
    master_agent = models.ForeignKey(User, on_delete=models.CASCADE, related_name='mapped_to_retail_managers', limit_choices_to={'user_type': 'master_agent'})
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        unique_together = (('retail_manager', 'master_agent'),)
        verbose_name = 'Retail Manager → Master Agent Mapping'
        verbose_name_plural = 'Retail Manager → Master Agent Mappings'

    def __str__(self):
        return f"{self.retail_manager.email} → {self.master_agent.email}"


class RetailManagerSuperAgentMapping(models.Model):
    retail_manager = models.ForeignKey(User, on_delete=models.CASCADE, related_name='mapped_super_agents', limit_choices_to={'user_type': 'retail_manager'})
    super_agent = models.ForeignKey(User, on_delete=models.CASCADE, related_name='mapped_to_retail_managers_as_super', limit_choices_to={'user_type': 'super_agent'})
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        unique_together = (('retail_manager', 'super_agent'),)
        verbose_name = 'Retail Manager → Super Agent Mapping'
        verbose_name_plural = 'Retail Manager → Super Agent Mappings'

    def __str__(self):
        return f"{self.retail_manager.email} → {self.super_agent.email}"


class RetailManagerAgentMapping(models.Model):
    retail_manager = models.ForeignKey(User, on_delete=models.CASCADE, related_name='mapped_agents', limit_choices_to={'user_type': 'retail_manager'})
    agent = models.ForeignKey(User, on_delete=models.CASCADE, related_name='mapped_to_retail_managers_as_agent', limit_choices_to={'user_type': 'agent'})
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        unique_together = (('retail_manager', 'agent'),)
        verbose_name = 'Retail Manager → Agent Mapping'
        verbose_name_plural = 'Retail Manager → Agent Mappings'

    def __str__(self):
        return f"{self.retail_manager.email} → {self.agent.email}"


class RetailManagerDashboardNote(models.Model):
    retail_manager = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name='retail_dashboard_note',
        limit_choices_to={'user_type': 'retail_manager'},
    )
    content = CKEditor5Field(blank=True, default='', config_name='default')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True, db_index=True)

    class Meta:
        ordering = ['-updated_at']
        verbose_name = 'Retail Manager Dashboard Note'
        verbose_name_plural = 'Retail Manager Dashboard Notes'

    def __str__(self):
        identifier = self.retail_manager.username or self.retail_manager.email or f"user#{self.retail_manager_id}"
        return f"Retail Note - {identifier}"


class DashboardTask(models.Model):
    class STATUS(models.TextChoices):
        ASSIGNED = "assigned", "Assigned"
        IN_PROGRESS = "in_progress", "In Progress"
        COMPLETED = "completed", "Completed"

    title = models.CharField(max_length=180)
    description = models.TextField()
    assigned_to = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="dashboard_tasks",
        limit_choices_to=Q(user_type="crm") | Q(user_type="retail_manager"),
    )
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_dashboard_tasks",
    )
    due_at = models.DateTimeField(null=True, blank=True, db_index=True)
    status = models.CharField(max_length=20, choices=STATUS.choices, default=STATUS.ASSIGNED, db_index=True)
    completion_report = models.TextField(blank=True, default="")
    completed_at = models.DateTimeField(null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True, db_index=True)

    class Meta:
        ordering = ["status", "due_at", "-created_at"]
        verbose_name = "Dashboard Task"
        verbose_name_plural = "Dashboard Tasks"

    def __str__(self):
        assignee = self.assigned_to.username or self.assigned_to.email or f"user#{self.assigned_to_id}"
        return f"{self.title} -> {assignee}"

    @property
    def audience_label(self):
        if getattr(self.assigned_to, "user_type", "") == "crm":
            return "CRM"
        if getattr(self.assigned_to, "user_type", "") == "retail_manager":
            return "Retail Manager"
        return "Dashboard"

    def mark_completed(self, *, report: str):
        self.completion_report = (report or "").strip()
        self.status = self.STATUS.COMPLETED
        self.completed_at = timezone.now()


class AgentTransferLog(models.Model):
    agent = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='agent_transfer_logs',
        limit_choices_to={'user_type': 'agent'},
    )
    old_super_agent = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='agent_transfer_logs_as_old_super_agent',
        limit_choices_to={'user_type': 'super_agent'},
    )
    new_super_agent = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='agent_transfer_logs_as_new_super_agent',
        limit_choices_to={'user_type': 'super_agent'},
    )
    transferred_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='agent_transfer_logs_created',
    )
    remarks = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Agent Transfer Log'
        verbose_name_plural = 'Agent Transfer Logs'

    def __str__(self):
        agent_label = getattr(self.agent, 'username', None) or getattr(self.agent, 'email', None) or f"agent#{self.agent_id}"
        return f"Agent Transfer - {agent_label}"


class AccountUnlockAppeal(models.Model):
    STATUS_CHOICES = (
        ('pending', 'Pending'),
        ('approved', 'Approved'),
        ('rejected', 'Rejected'),
    )

    user = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='account_unlock_appeals',
    )
    locked_user = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='account_unlock_appeals_received',
    )
    appealed_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='account_unlock_appeals_submitted',
    )
    appeal_reason = models.TextField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending', db_index=True)
    admin_comment = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    reviewed_at = models.DateTimeField(null=True, blank=True, db_index=True)
    reviewed_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='account_unlock_appeals_reviewed',
    )

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Account Unlock Appeal'
        verbose_name_plural = 'Account Unlock Appeals'

    def __str__(self):
        target = getattr(self.locked_user, 'username', None) or getattr(self.locked_user, 'email', None) or f"user#{self.locked_user_id}"
        return f"Unlock Appeal - {target}"


class AccountLockAuditLog(models.Model):
    ACTION_CHOICES = (
        ('locked', 'Locked'),
        ('appeal_submitted', 'Appeal Submitted'),
        ('appeal_approved', 'Appeal Approved'),
        ('appeal_rejected', 'Appeal Rejected'),
        ('unlocked', 'Unlocked'),
    )

    locked_user = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='account_lock_audit_logs',
    )
    locked_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='account_lock_events_created',
    )
    lock_reason = models.CharField(max_length=255, blank=True, default='')
    appealed_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='account_lock_appeals_logged',
    )
    reviewed_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='account_lock_reviews_logged',
    )
    action = models.CharField(max_length=30, choices=ACTION_CHOICES, db_index=True)
    timestamp = models.DateTimeField(auto_now_add=True, db_index=True)
    remarks = models.TextField(blank=True, default='')

    class Meta:
        ordering = ['-timestamp']
        verbose_name = 'Account Lock Audit Log'
        verbose_name_plural = 'Account Lock Audit Logs'

    def __str__(self):
        target = getattr(self.locked_user, 'username', None) or getattr(self.locked_user, 'email', None) or f"user#{self.locked_user_id}"
        return f"{self.get_action_display()} - {target}"


class CustomerComplaint(models.Model):
    COMPLAINT_TYPE_CHOICES = (
        ('ticket', 'Ticket Complaints'),
        ('wallet', 'Wallet Complaints'),
        ('withdrawal', 'Withdrawal Complaints'),
        ('deposit', 'Deposit Complaints'),
        ('commission', 'Commission Complaints'),
    )
    STATUS_CHOICES = (
        ('open', 'Open'),
        ('pending', 'Pending'),
        ('in_progress', 'In Progress'),
        ('resolved', 'Resolved'),
        ('escalated', 'Escalated'),
        ('closed', 'Closed'),
    )
    PRIORITY_CHOICES = (
        ('low', 'Low'),
        ('medium', 'Medium'),
        ('high', 'High'),
        ('critical', 'Critical'),
    )

    complaint_type = models.CharField(max_length=30, choices=COMPLAINT_TYPE_CHOICES, db_index=True)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='customer_complaints')
    subject = models.CharField(max_length=255)
    description = models.TextField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='open', db_index=True)
    priority = models.CharField(max_length=20, choices=PRIORITY_CHOICES, default='medium', db_index=True)
    assigned_to = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='assigned_customer_complaints',
        limit_choices_to=Q(user_type='crm') | Q(user_type='admin'),
    )
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='created_customer_complaints',
    )
    resolved_at = models.DateTimeField(null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True, db_index=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.get_complaint_type_display()} - {self.subject}"


class CustomerComplaintNote(models.Model):
    complaint = models.ForeignKey(CustomerComplaint, on_delete=models.CASCADE, related_name='notes')
    author = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='customer_complaint_notes')
    note = models.TextField()
    is_internal = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"Complaint Note #{self.id}"


class BulkMessageTemplate(models.Model):
    CATEGORY_CHOICES = (
        ('promotions', 'Promotions'),
        ('bonus_offers', 'Bonus Offers'),
        ('account_alerts', 'Account Alerts'),
        ('kyc_reminders', 'KYC Reminders'),
        ('deposit_reminders', 'Deposit Reminders'),
        ('dormancy_reminders', 'Dormancy Reminders'),
        ('commission_updates', 'Commission Updates'),
    )
    CHANNEL_CHOICES = (
        ('sms', 'SMS'),
        ('email', 'Email'),
        ('in_app', 'In-App Notification'),
    )

    name = models.CharField(max_length=120)
    category = models.CharField(max_length=30, choices=CATEGORY_CHOICES, db_index=True)
    subject = models.CharField(max_length=160, blank=True, default='')
    message = models.TextField()
    default_channel = models.CharField(max_length=20, choices=CHANNEL_CHOICES, default='in_app')
    is_active = models.BooleanField(default=True, db_index=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='bulk_message_templates_created')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['category', 'name']

    def __str__(self):
        return self.name


class BulkMessageCampaign(models.Model):
    CHANNEL_CHOICES = (
        ('sms', 'SMS'),
        ('email', 'Email'),
        ('in_app', 'In-App Notification'),
    )
    TARGET_GROUP_CHOICES = (
        ('all_users', 'All Users'),
        ('all_agents', 'All Agents'),
        ('all_super_agents', 'All Super Agents'),
        ('all_retail_managers', 'All Retail Managers'),
        ('specific_agents', 'Specific Agents'),
        ('dormant_users', 'Dormant Users'),
        ('high_value_users', 'High Value Users'),
        ('recent_registrations', 'Recent Registrations'),
        ('failed_deposit_users', 'Users With Failed Deposits'),
        ('pending_withdrawal_users', 'Users With Pending Withdrawals'),
        ('custom_users', 'Custom Users'),
    )
    STATUS_CHOICES = (
        ('draft', 'Draft'),
        ('scheduled', 'Scheduled'),
        ('processing', 'Processing'),
        ('sent', 'Sent'),
        ('partial', 'Partial'),
        ('failed', 'Failed'),
        ('cancelled', 'Cancelled'),
    )
    RECURRING_CHOICES = (
        ('none', 'One-off'),
        ('daily', 'Daily'),
        ('weekly', 'Weekly'),
        ('monthly', 'Monthly'),
    )

    template = models.ForeignKey(BulkMessageTemplate, on_delete=models.SET_NULL, null=True, blank=True, related_name='campaigns')
    subject = models.CharField(max_length=160, blank=True, default='')
    message = models.TextField()
    channel = models.CharField(max_length=20, choices=CHANNEL_CHOICES, db_index=True)
    target_group = models.CharField(max_length=40, choices=TARGET_GROUP_CHOICES, db_index=True)
    schedule_at = models.DateTimeField(null=True, blank=True, db_index=True)
    recurring_pattern = models.CharField(max_length=20, choices=RECURRING_CHOICES, default='none', db_index=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='draft', db_index=True)
    target_user_ids = models.JSONField(blank=True, default=list)
    target_agent_ids = models.JSONField(blank=True, default=list)
    filter_snapshot = models.JSONField(blank=True, default=dict)
    recipients_count = models.PositiveIntegerField(default=0)
    delivered_count = models.PositiveIntegerField(default=0)
    failed_count = models.PositiveIntegerField(default=0)
    opened_count = models.PositiveIntegerField(default=0)
    clicked_count = models.PositiveIntegerField(default=0)
    conversion_count = models.PositiveIntegerField(default=0)
    sent_at = models.DateTimeField(null=True, blank=True, db_index=True)
    next_run_at = models.DateTimeField(null=True, blank=True, db_index=True)
    last_error = models.TextField(blank=True, default='')
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='bulk_message_campaigns_created')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return self.subject or f"Campaign #{self.pk}"


class BulkMessageDelivery(models.Model):
    STATUS_CHOICES = (
        ('pending', 'Pending'),
        ('sent', 'Sent'),
        ('failed', 'Failed'),
        ('opened', 'Opened'),
        ('clicked', 'Clicked'),
        ('converted', 'Converted'),
    )

    campaign = models.ForeignKey(BulkMessageCampaign, on_delete=models.CASCADE, related_name='deliveries')
    recipient = models.ForeignKey(User, on_delete=models.CASCADE, related_name='bulk_message_deliveries')
    channel = models.CharField(max_length=20, choices=BulkMessageCampaign.CHANNEL_CHOICES, db_index=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending', db_index=True)
    error_message = models.CharField(max_length=255, blank=True, default='')
    provider_response = models.JSONField(blank=True, default=dict)
    sent_at = models.DateTimeField(null=True, blank=True, db_index=True)
    opened_at = models.DateTimeField(null=True, blank=True, db_index=True)
    clicked_at = models.DateTimeField(null=True, blank=True, db_index=True)
    converted_at = models.DateTimeField(null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.campaign_id}:{self.recipient_id}:{self.channel}"


class CRMOpsAuditLog(models.Model):
    MODULE_CHOICES = (
        ('dormant_accounts', 'Dormant Accounts'),
        ('complaints', 'Complaints'),
        ('deposit_monitoring', 'Deposit Monitoring'),
        ('agent_performance', 'Agent Performance'),
        ('user_activation', 'User Activation'),
        ('bulk_messaging', 'Bulk Messaging'),
        ('daily_reporting', 'Daily Reporting'),
    )

    actor = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='crm_ops_actions')
    module = models.CharField(max_length=40, choices=MODULE_CHOICES, db_index=True)
    action = models.CharField(max_length=80, db_index=True)
    target_user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='crm_ops_targets')
    complaint = models.ForeignKey('CustomerComplaint', on_delete=models.SET_NULL, null=True, blank=True, related_name='audit_logs')
    campaign = models.ForeignKey('BulkMessageCampaign', on_delete=models.SET_NULL, null=True, blank=True, related_name='audit_logs')
    transaction = models.ForeignKey('Transaction', on_delete=models.SET_NULL, null=True, blank=True, related_name='crm_ops_audit_logs')
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    metadata = models.JSONField(blank=True, default=dict)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        who = self.actor.email if self.actor else 'System'
        return f"{self.module}:{self.action} by {who}"


def crm_report_attachment_upload_to(instance, filename):
    report_id = getattr(instance, 'report_id', None) or 'unassigned'
    suffix = uuid.uuid4().hex[:12]
    safe_name = os.path.basename(filename or 'attachment')
    return f"crm_reports/{report_id}/{suffix}_{safe_name}"


def validate_crm_report_attachment_size(value):
    max_size = 20 * 1024 * 1024
    size = getattr(value, 'size', 0) or 0
    if size > max_size:
        raise ValidationError("Attachment size must not exceed 20 MB.")


class CRMDailyReport(models.Model):
    class STATUS(models.TextChoices):
        DRAFT = 'draft', 'Draft'
        SUBMITTED = 'submitted', 'Submitted'
        APPROVED = 'approved', 'Approved'
        REJECTED = 'rejected', 'Rejected'
        RETURNED = 'returned', 'Returned For Correction'

    staff = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='crm_daily_reports',
        limit_choices_to={'user_type': 'crm'},
    )
    report_date = models.DateField(db_index=True)
    branch_name = models.CharField(max_length=120, blank=True, default='', db_index=True)
    status = models.CharField(max_length=20, choices=STATUS.choices, default=STATUS.DRAFT, db_index=True)

    calls_made = models.PositiveIntegerField(default=0)
    calls_received = models.PositiveIntegerField(default=0)
    whatsapp_conversations = models.PositiveIntegerField(default=0)
    emails_sent = models.PositiveIntegerField(default=0)
    sms_sent = models.PositiveIntegerField(default=0)
    push_notifications_sent = models.PositiveIntegerField(default=0)
    social_media_responses = models.PositiveIntegerField(default=0)
    general_notes = models.TextField(blank=True, default='')

    complaints_received = models.PositiveIntegerField(default=0)
    complaints_resolved = models.PositiveIntegerField(default=0)
    pending_complaints = models.PositiveIntegerField(default=0)
    escalated_cases = models.PositiveIntegerField(default=0)
    reopened_cases = models.PositiveIntegerField(default=0)

    dormant_customers_contacted = models.PositiveIntegerField(default=0)
    active_customers_followed_up = models.PositiveIntegerField(default=0)
    vip_customers_contacted = models.PositiveIntegerField(default=0)
    welcome_calls = models.PositiveIntegerField(default=0)
    birthday_messages = models.PositiveIntegerField(default=0)
    loyalty_calls = models.PositiveIntegerField(default=0)
    engagement_remarks = models.TextField(blank=True, default='')

    dormant_customers_reactivated = models.PositiveIntegerField(default=0)
    returning_customers = models.PositiveIntegerField(default=0)
    customers_retained = models.PositiveIntegerField(default=0)
    high_risk_customers_identified = models.PositiveIntegerField(default=0)
    customers_lost = models.PositiveIntegerField(default=0)
    reason_for_loss = models.TextField(blank=True, default='')

    new_registrations = models.PositiveIntegerField(default=0)
    first_time_depositors = models.PositiveIntegerField(default=0)
    repeat_depositors = models.PositiveIntegerField(default=0)
    customers_assisted_to_deposit = models.PositiveIntegerField(default=0)
    customers_assisted_to_place_bets = models.PositiveIntegerField(default=0)
    total_deposits_influenced = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    estimated_revenue_influenced = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))

    agents_contacted = models.PositiveIntegerField(default=0)
    retail_shops_contacted = models.PositiveIntegerField(default=0)
    agent_complaints_resolved = models.PositiveIntegerField(default=0)
    training_conducted = models.PositiveIntegerField(default=0)
    support_visits = models.PositiveIntegerField(default=0)

    positive_feedback = models.TextField(blank=True, default='')
    negative_feedback = models.TextField(blank=True, default='')
    customer_suggestions = models.TextField(blank=True, default='')
    recommendations = CKEditor5Field(blank=True, default='', config_name='default')

    calls_achievement_rate = models.DecimalField(max_digits=6, decimal_places=2, default=Decimal('0.00'))
    complaint_resolution_rate = models.DecimalField(max_digits=6, decimal_places=2, default=Decimal('0.00'))
    customer_reactivation_rate = models.DecimalField(max_digits=6, decimal_places=2, default=Decimal('0.00'))
    campaign_conversion_rate = models.DecimalField(max_digits=6, decimal_places=2, default=Decimal('0.00'))
    customer_retention_rate = models.DecimalField(max_digits=6, decimal_places=2, default=Decimal('0.00'))
    overall_productivity_score = models.DecimalField(max_digits=6, decimal_places=2, default=Decimal('0.00'))

    submitted_at = models.DateTimeField(null=True, blank=True, db_index=True)
    reviewed_at = models.DateTimeField(null=True, blank=True, db_index=True)
    approved_at = models.DateTimeField(null=True, blank=True, db_index=True)
    rejected_at = models.DateTimeField(null=True, blank=True, db_index=True)
    returned_at = models.DateTimeField(null=True, blank=True, db_index=True)

    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='crm_daily_reports_created',
    )
    updated_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='crm_daily_reports_updated',
    )
    reviewed_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='crm_daily_reports_reviewed',
    )
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    browser_information = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True, db_index=True)
    last_modified_at = models.DateTimeField(auto_now=True, db_index=True)

    class Meta:
        ordering = ['-report_date', '-updated_at']
        constraints = [
            models.UniqueConstraint(fields=['staff', 'report_date'], name='uniq_crm_daily_report_staff_date'),
        ]
        verbose_name = 'CRM Daily Report'
        verbose_name_plural = 'CRM Daily Reports'

    def __str__(self):
        identifier = self.staff.username or self.staff.email or f"user#{self.staff_id}"
        return f"CRM Daily Report - {identifier} - {self.report_date}"

    @property
    def is_editable_by_staff(self):
        return self.status in {self.STATUS.DRAFT, self.STATUS.RETURNED}

    def recalculate_kpis(self, *, campaign_rows=None):
        def _pct(numerator, denominator):
            numerator_q = Decimal(str(numerator or 0))
            denominator_q = Decimal(str(denominator or 0))
            if denominator_q <= Decimal('0.00'):
                return Decimal('0.00')
            value = (numerator_q / denominator_q) * Decimal('100.00')
            return min(value.quantize(Decimal('0.01')), Decimal('100.00'))

        campaign_rows = list(campaign_rows if campaign_rows is not None else self.campaign_rows.all())
        campaign_audience = sum(int(getattr(row, 'audience_size', 0) or 0) for row in campaign_rows)
        campaign_conversions = sum(int(getattr(row, 'conversions', 0) or 0) for row in campaign_rows)

        engagement_actions = (
            self.calls_made
            + self.whatsapp_conversations
            + self.emails_sent
            + self.sms_sent
            + self.push_notifications_sent
            + self.social_media_responses
        )
        engagement_target = max(
            self.calls_made + self.calls_received + self.whatsapp_conversations + self.emails_sent + self.sms_sent + self.push_notifications_sent,
            1,
        )

        self.calls_achievement_rate = _pct(engagement_actions, engagement_target)
        self.complaint_resolution_rate = _pct(self.complaints_resolved, self.complaints_received)
        self.customer_reactivation_rate = _pct(self.dormant_customers_reactivated, self.dormant_customers_contacted)
        self.campaign_conversion_rate = _pct(campaign_conversions, campaign_audience)
        self.customer_retention_rate = _pct(self.customers_retained, self.customers_retained + self.customers_lost)

        weighted_total = (
            self.calls_achievement_rate
            + self.complaint_resolution_rate
            + self.customer_reactivation_rate
            + self.campaign_conversion_rate
            + self.customer_retention_rate
        )
        self.overall_productivity_score = (weighted_total / Decimal('5.00')).quantize(Decimal('0.01'))


class CRMComplaint(models.Model):
    report = models.ForeignKey(CRMDailyReport, on_delete=models.CASCADE, related_name='complaint_rows')
    customer_name = models.CharField(max_length=160)
    complaint = models.TextField()
    escalated_to = models.CharField(max_length=160, blank=True, default='')
    status = models.CharField(max_length=80, blank=True, default='Pending')
    remarks = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['id']
        verbose_name = 'CRM Daily Report Complaint'
        verbose_name_plural = 'CRM Daily Report Complaints'

    def __str__(self):
        return f"Complaint Row #{self.pk or 'new'}"


class CRMCampaignPerformance(models.Model):
    CHANNEL_CHOICES = (
        ('sms', 'SMS'),
        ('whatsapp', 'WhatsApp'),
        ('email', 'Email'),
        ('push_notification', 'Push Notification'),
    )

    report = models.ForeignKey(CRMDailyReport, on_delete=models.CASCADE, related_name='campaign_rows')
    campaign_name = models.CharField(max_length=180)
    campaign_type = models.CharField(max_length=120)
    channel = models.CharField(max_length=30, choices=CHANNEL_CHOICES)
    audience_size = models.PositiveIntegerField(default=0)
    responses = models.PositiveIntegerField(default=0)
    conversions = models.PositiveIntegerField(default=0)
    revenue_generated = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    remarks = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['id']
        verbose_name = 'CRM Campaign Performance'
        verbose_name_plural = 'CRM Campaign Performance Rows'

    def __str__(self):
        return self.campaign_name


class CRMChallenge(models.Model):
    report = models.ForeignKey(CRMDailyReport, on_delete=models.CASCADE, related_name='challenge_rows')
    challenge = models.CharField(max_length=255)
    impact = models.TextField(blank=True, default='')
    action_taken = models.TextField(blank=True, default='')
    current_status = models.CharField(max_length=120, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['id']
        verbose_name = 'CRM Challenge'
        verbose_name_plural = 'CRM Challenges'

    def __str__(self):
        return self.challenge


class CRMNextDayTask(models.Model):
    PRIORITY_CHOICES = (
        ('high', 'High'),
        ('medium', 'Medium'),
        ('low', 'Low'),
    )

    report = models.ForeignKey(CRMDailyReport, on_delete=models.CASCADE, related_name='next_day_tasks')
    task = models.CharField(max_length=255)
    priority = models.CharField(max_length=20, choices=PRIORITY_CHOICES, default='medium')
    expected_outcome = models.TextField(blank=True, default='')
    deadline = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['id']
        verbose_name = 'CRM Next Day Task'
        verbose_name_plural = 'CRM Next Day Tasks'

    def __str__(self):
        return self.task


class CRMAdminComment(models.Model):
    ACTION_CHOICES = (
        ('comment', 'Comment'),
        ('approved', 'Approved'),
        ('rejected', 'Rejected'),
        ('returned', 'Returned For Correction'),
    )

    report = models.ForeignKey(CRMDailyReport, on_delete=models.CASCADE, related_name='admin_comments')
    author = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='crm_daily_report_comments',
    )
    action = models.CharField(max_length=20, choices=ACTION_CHOICES, default='comment', db_index=True)
    comment = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'CRM Daily Report Admin Comment'
        verbose_name_plural = 'CRM Daily Report Admin Comments'

    def __str__(self):
        return f"{self.get_action_display()} - report #{self.report_id}"


class CRMReportAttachment(models.Model):
    report = models.ForeignKey(CRMDailyReport, on_delete=models.CASCADE, related_name='attachments')
    file = models.FileField(
        upload_to=crm_report_attachment_upload_to,
        validators=[
            validate_crm_report_attachment_size,
            FileExtensionValidator(
                allowed_extensions=['pdf', 'xlsx', 'xls', 'doc', 'docx', 'png', 'jpg', 'jpeg', 'gif', 'webp', 'mp3', 'wav', 'ogg', 'm4a', 'aac', 'webm']
            ),
        ],
    )
    uploaded_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='crm_report_attachments_uploaded',
    )
    original_name = models.CharField(max_length=255, blank=True, default='')
    file_size = models.PositiveIntegerField(default=0)
    mime_type = models.CharField(max_length=120, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'CRM Report Attachment'
        verbose_name_plural = 'CRM Report Attachments'

    def __str__(self):
        return self.original_name or f"Attachment #{self.pk}"

    def save(self, *args, **kwargs):
        if self.file:
            self.original_name = self.original_name or os.path.basename(self.file.name)
            self.file_size = getattr(self.file, 'size', self.file_size or 0) or 0
        super().save(*args, **kwargs)


def retail_report_attachment_upload_to(instance, filename):
    report_id = getattr(instance, 'report_id', None) or 'unassigned'
    suffix = uuid.uuid4().hex[:12]
    safe_name = os.path.basename(filename or 'attachment')
    return f"retail_reports/{report_id}/{suffix}_{safe_name}"


class RetailDailyReport(models.Model):
    class STATUS(models.TextChoices):
        DRAFT = 'draft', 'Draft'
        SUBMITTED = 'submitted', 'Submitted'
        APPROVED = 'approved', 'Approved'
        REJECTED = 'rejected', 'Rejected'
        RETURNED = 'returned', 'Returned For Correction'

    retail_manager = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='retail_daily_reports',
        limit_choices_to={'user_type': 'retail_manager'},
    )
    report_date = models.DateField(db_index=True)
    branch_name = models.CharField(max_length=120, blank=True, default='', db_index=True)
    status = models.CharField(max_length=20, choices=STATUS.choices, default=STATUS.DRAFT, db_index=True)

    shops_visited = models.PositiveIntegerField(default=0)
    agents_supported = models.PositiveIntegerField(default=0)
    cashiers_supported = models.PositiveIntegerField(default=0)
    support_calls_made = models.PositiveIntegerField(default=0)
    support_calls_received = models.PositiveIntegerField(default=0)
    whatsapp_followups = models.PositiveIntegerField(default=0)
    escalation_cases = models.PositiveIntegerField(default=0)
    general_notes = models.TextField(blank=True, default='')

    pending_withdrawals_reviewed = models.PositiveIntegerField(default=0)
    withdrawals_resolved = models.PositiveIntegerField(default=0)
    dormant_accounts_contacted = models.PositiveIntegerField(default=0)
    dormant_accounts_reactivated = models.PositiveIntegerField(default=0)
    agent_complaints_received = models.PositiveIntegerField(default=0)
    agent_complaints_resolved = models.PositiveIntegerField(default=0)
    shop_issues_identified = models.PositiveIntegerField(default=0)
    shop_issues_resolved = models.PositiveIntegerField(default=0)

    new_agents_onboarded = models.PositiveIntegerField(default=0)
    training_sessions_conducted = models.PositiveIntegerField(default=0)
    compliance_checks_completed = models.PositiveIntegerField(default=0)
    terminals_checked = models.PositiveIntegerField(default=0)
    terminals_fixed = models.PositiveIntegerField(default=0)
    stock_requests_handled = models.PositiveIntegerField(default=0)
    marketing_support_requests = models.PositiveIntegerField(default=0)
    field_visit_notes = models.TextField(blank=True, default='')

    total_stake_influenced = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    estimated_revenue_influenced = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    commissions_followed_up = models.PositiveIntegerField(default=0)
    fraud_cases_flagged = models.PositiveIntegerField(default=0)
    retention_actions_taken = models.PositiveIntegerField(default=0)
    customers_assisted_to_bet = models.PositiveIntegerField(default=0)
    high_value_players_contacted = models.PositiveIntegerField(default=0)
    inactive_shops_reactivated = models.PositiveIntegerField(default=0)

    positive_feedback = models.TextField(blank=True, default='')
    negative_feedback = models.TextField(blank=True, default='')
    recommendations = CKEditor5Field(blank=True, default='', config_name='default')

    support_resolution_rate = models.DecimalField(max_digits=6, decimal_places=2, default=Decimal('0.00'))
    reactivation_rate = models.DecimalField(max_digits=6, decimal_places=2, default=Decimal('0.00'))
    field_visit_completion_rate = models.DecimalField(max_digits=6, decimal_places=2, default=Decimal('0.00'))
    training_completion_rate = models.DecimalField(max_digits=6, decimal_places=2, default=Decimal('0.00'))
    overall_productivity_score = models.DecimalField(max_digits=6, decimal_places=2, default=Decimal('0.00'))

    submitted_at = models.DateTimeField(null=True, blank=True, db_index=True)
    reviewed_at = models.DateTimeField(null=True, blank=True, db_index=True)
    approved_at = models.DateTimeField(null=True, blank=True, db_index=True)
    rejected_at = models.DateTimeField(null=True, blank=True, db_index=True)
    returned_at = models.DateTimeField(null=True, blank=True, db_index=True)

    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='retail_daily_reports_created',
    )
    updated_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='retail_daily_reports_updated',
    )
    reviewed_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='retail_daily_reports_reviewed',
    )
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    browser_information = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True, db_index=True)
    last_modified_at = models.DateTimeField(auto_now=True, db_index=True)

    class Meta:
        ordering = ['-report_date', '-updated_at']
        constraints = [
            models.UniqueConstraint(fields=['retail_manager', 'report_date'], name='uniq_retail_daily_report_manager_date'),
        ]
        verbose_name = 'Retail Daily Report'
        verbose_name_plural = 'Retail Daily Reports'

    def __str__(self):
        identifier = self.retail_manager.username or self.retail_manager.email or f"user#{self.retail_manager_id}"
        return f"Retail Daily Report - {identifier} - {self.report_date}"

    @property
    def is_editable_by_manager(self):
        return self.status in {self.STATUS.DRAFT, self.STATUS.RETURNED}

    def recalculate_kpis(self, *, campaign_rows=None):
        def _pct(numerator, denominator):
            numerator_q = Decimal(str(numerator or 0))
            denominator_q = Decimal(str(denominator or 0))
            if denominator_q <= Decimal('0.00'):
                return Decimal('0.00')
            value = (numerator_q / denominator_q) * Decimal('100.00')
            return min(value.quantize(Decimal('0.01')), Decimal('100.00'))

        campaign_rows = list(campaign_rows if campaign_rows is not None else self.campaign_rows.all())
        campaign_targets = sum(int(getattr(row, 'target_count', 0) or 0) for row in campaign_rows)
        campaign_resolved = sum(int(getattr(row, 'conversions', 0) or 0) for row in campaign_rows)

        self.support_resolution_rate = _pct(
            self.withdrawals_resolved + self.agent_complaints_resolved + self.shop_issues_resolved,
            self.pending_withdrawals_reviewed + self.agent_complaints_received + self.shop_issues_identified,
        )
        self.reactivation_rate = _pct(
            self.dormant_accounts_reactivated + self.inactive_shops_reactivated,
            self.dormant_accounts_contacted + self.shops_visited,
        )
        self.field_visit_completion_rate = _pct(
            self.shops_visited + self.terminals_checked,
            self.shops_visited + self.terminals_checked + self.support_calls_made,
        )
        self.training_completion_rate = _pct(
            self.training_sessions_conducted + self.new_agents_onboarded,
            self.training_sessions_conducted + self.new_agents_onboarded + self.marketing_support_requests,
        )

        weighted_total = (
            self.support_resolution_rate
            + self.reactivation_rate
            + self.field_visit_completion_rate
            + self.training_completion_rate
            + _pct(campaign_resolved, campaign_targets)
        )
        self.overall_productivity_score = (weighted_total / Decimal('5.00')).quantize(Decimal('0.01'))


class RetailSupportActivity(models.Model):
    report = models.ForeignKey(RetailDailyReport, on_delete=models.CASCADE, related_name='support_rows')
    shop_or_agent = models.CharField(max_length=180)
    issue = models.TextField()
    escalated_to = models.CharField(max_length=160, blank=True, default='')
    status = models.CharField(max_length=80, blank=True, default='Open')
    remarks = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['id']
        verbose_name = 'Retail Support Activity'
        verbose_name_plural = 'Retail Support Activities'

    def __str__(self):
        return f"Retail Support Row #{self.pk or 'new'}"


class RetailCampaignPerformance(models.Model):
    CHANNEL_CHOICES = (
        ('sms', 'SMS'),
        ('whatsapp', 'WhatsApp'),
        ('email', 'Email'),
        ('field_visit', 'Field Visit'),
    )

    report = models.ForeignKey(RetailDailyReport, on_delete=models.CASCADE, related_name='campaign_rows')
    campaign_name = models.CharField(max_length=180)
    campaign_type = models.CharField(max_length=120)
    channel = models.CharField(max_length=30, choices=CHANNEL_CHOICES)
    target_count = models.PositiveIntegerField(default=0)
    responses = models.PositiveIntegerField(default=0)
    conversions = models.PositiveIntegerField(default=0)
    revenue_generated = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    remarks = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['id']
        verbose_name = 'Retail Campaign Performance'
        verbose_name_plural = 'Retail Campaign Performance Rows'

    def __str__(self):
        return self.campaign_name


class RetailChallenge(models.Model):
    report = models.ForeignKey(RetailDailyReport, on_delete=models.CASCADE, related_name='challenge_rows')
    challenge = models.CharField(max_length=255)
    impact = models.TextField(blank=True, default='')
    action_taken = models.TextField(blank=True, default='')
    current_status = models.CharField(max_length=120, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['id']
        verbose_name = 'Retail Challenge'
        verbose_name_plural = 'Retail Challenges'

    def __str__(self):
        return self.challenge


class RetailNextDayTask(models.Model):
    PRIORITY_CHOICES = (
        ('high', 'High'),
        ('medium', 'Medium'),
        ('low', 'Low'),
    )

    report = models.ForeignKey(RetailDailyReport, on_delete=models.CASCADE, related_name='next_day_tasks')
    task = models.CharField(max_length=255)
    priority = models.CharField(max_length=20, choices=PRIORITY_CHOICES, default='medium')
    expected_outcome = models.TextField(blank=True, default='')
    deadline = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['id']
        verbose_name = 'Retail Next Day Task'
        verbose_name_plural = 'Retail Next Day Tasks'

    def __str__(self):
        return self.task


class RetailAdminComment(models.Model):
    ACTION_CHOICES = (
        ('comment', 'Comment'),
        ('approved', 'Approved'),
        ('rejected', 'Rejected'),
        ('returned', 'Returned For Correction'),
    )

    report = models.ForeignKey(RetailDailyReport, on_delete=models.CASCADE, related_name='admin_comments')
    author = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='retail_daily_report_comments',
    )
    action = models.CharField(max_length=20, choices=ACTION_CHOICES, default='comment', db_index=True)
    comment = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Retail Daily Report Admin Comment'
        verbose_name_plural = 'Retail Daily Report Admin Comments'

    def __str__(self):
        return f"{self.get_action_display()} - report #{self.report_id}"


class RetailReportAttachment(models.Model):
    report = models.ForeignKey(RetailDailyReport, on_delete=models.CASCADE, related_name='attachments')
    file = models.FileField(
        upload_to=retail_report_attachment_upload_to,
        validators=[
            validate_crm_report_attachment_size,
            FileExtensionValidator(
                allowed_extensions=['pdf', 'xlsx', 'xls', 'doc', 'docx', 'png', 'jpg', 'jpeg', 'gif', 'webp', 'mp3', 'wav', 'ogg', 'm4a', 'aac', 'webm']
            ),
        ],
    )
    uploaded_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='retail_report_attachments_uploaded',
    )
    original_name = models.CharField(max_length=255, blank=True, default='')
    file_size = models.PositiveIntegerField(default=0)
    mime_type = models.CharField(max_length=120, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Retail Report Attachment'
        verbose_name_plural = 'Retail Report Attachments'

    def __str__(self):
        return self.original_name or f"Attachment #{self.pk}"

    def save(self, *args, **kwargs):
        if self.file:
            self.original_name = self.original_name or os.path.basename(self.file.name)
            self.file_size = getattr(self.file, 'size', self.file_size or 0) or 0
        super().save(*args, **kwargs)


class FinanceAuditLog(models.Model):
    ACTION_TYPES = (
        ('WITHDRAWAL_APPROVED', 'Withdrawal Approved'),
        ('WITHDRAWAL_REJECTED', 'Withdrawal Rejected'),
        ('WITHDRAWAL_COMPLETED', 'Withdrawal Completed'),
        ('TX_REVERSED', 'Transaction Reversed'),
        ('TX_VERIFIED', 'Transaction Verified'),
        ('WALLET_ADJUSTED', 'Wallet Adjusted'),
        ('REPORT_EXPORTED', 'Report Exported'),
        ('SCHEDULED_REPORT_SENT', 'Scheduled Report Sent'),
        ('DEPOSIT_MANUAL_COMPLETED', 'Deposit Manually Completed'),
        ('SETTLEMENT_CREATED', 'Settlement Created'),
        ('SETTLEMENT_APPROVED', 'Settlement Approved'),
        ('SETTLEMENT_PAID', 'Settlement Paid'),
        ('JOURNAL_CREATED', 'Journal Entry Created'),
    )

    actor = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='finance_actions')
    action_type = models.CharField(max_length=50, choices=ACTION_TYPES, db_index=True)
    target_user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='finance_action_targets')
    transaction = models.ForeignKey('Transaction', on_delete=models.SET_NULL, null=True, blank=True, related_name='finance_audit_logs')
    withdrawal = models.ForeignKey('UserWithdrawal', on_delete=models.SET_NULL, null=True, blank=True, related_name='finance_audit_logs')
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    reason = models.CharField(max_length=255, blank=True, default='')
    notes = models.TextField(blank=True, default='')
    data = models.JSONField(blank=True, default=dict)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        who = self.actor.email if self.actor else 'System'
        return f"{self.action_type} by {who}"


class WithdrawalPinVerificationLog(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='withdrawal_pin_verifications')
    success = models.BooleanField(default=False, db_index=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"PIN verify {self.user.email} ({'ok' if self.success else 'fail'})"


class PaymentGatewayEventLog(models.Model):
    GATEWAY_CHOICES = (
        ('paystack', 'Paystack'),
        ('monnify', 'Monnify'),
        ('kora', 'Korapay'),
    )
    EVENT_CHOICES = (
        ('init', 'Initialize'),
        ('verify', 'Verify'),
        ('webhook', 'Webhook'),
        ('reconcile', 'Reconcile'),
    )

    gateway = models.CharField(max_length=20, choices=GATEWAY_CHOICES, db_index=True)
    event_type = models.CharField(max_length=20, choices=EVENT_CHOICES, db_index=True)
    reference = models.CharField(max_length=120, blank=True, default='', db_index=True)
    transaction = models.ForeignKey('Transaction', on_delete=models.SET_NULL, null=True, blank=True, related_name='gateway_events')
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='gateway_events')
    amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    fee_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    success = models.BooleanField(default=False, db_index=True)
    http_status = models.IntegerField(null=True, blank=True)
    message = models.CharField(max_length=255, blank=True, default='')
    payload = models.JSONField(blank=True, default=dict)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.gateway}:{self.event_type}:{self.reference or '-'}"


class FinanceTransactionReview(models.Model):
    STATUS_CHOICES = (
        ('verified', 'Verified'),
        ('flagged', 'Flagged'),
        ('rejected', 'Rejected'),
    )
    transaction = models.ForeignKey('Transaction', on_delete=models.CASCADE, related_name='finance_reviews')
    reviewer = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='finance_transaction_reviews')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, db_index=True)
    notes = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.transaction_id} {self.status}"


class LedgerAccount(models.Model):
    ACCOUNT_TYPE_CHOICES = (
        ('asset', 'Asset'),
        ('liability', 'Liability'),
        ('equity', 'Equity'),
        ('revenue', 'Revenue'),
        ('expense', 'Expense'),
    )
    code = models.CharField(max_length=30, unique=True)
    name = models.CharField(max_length=140)
    account_type = models.CharField(max_length=12, choices=ACCOUNT_TYPE_CHOICES, db_index=True)
    is_active = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['code']

    def __str__(self):
        return f"{self.code} - {self.name}"


class JournalEntry(models.Model):
    entry_date = models.DateField(db_index=True)
    memo = models.CharField(max_length=255, blank=True, default='')
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='journal_entries_created')
    related_transaction = models.ForeignKey('Transaction', on_delete=models.SET_NULL, null=True, blank=True, related_name='journal_entries')
    related_withdrawal = models.ForeignKey('UserWithdrawal', on_delete=models.SET_NULL, null=True, blank=True, related_name='journal_entries')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-entry_date', '-created_at']

    def __str__(self):
        return f"JE {self.id} {self.entry_date}"


class JournalLine(models.Model):
    entry = models.ForeignKey(JournalEntry, on_delete=models.CASCADE, related_name='lines')
    account = models.ForeignKey(LedgerAccount, on_delete=models.PROTECT, related_name='journal_lines')
    related_user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='journal_lines')
    debit = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    credit = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))

    class Meta:
        ordering = ['id']

    def __str__(self):
        return f"{self.account.code} D{self.debit} C{self.credit}"


class FinanceSettlementBatch(models.Model):
    STATUS_CHOICES = (
        ('draft', 'Draft'),
        ('approved', 'Approved'),
        ('paid', 'Paid'),
        ('cancelled', 'Cancelled'),
    )
    TYPE_CHOICES = (
        ('weekly_commission', 'Weekly Commissions'),
        ('network_commission', 'Network Commissions'),
        ('mixed_commission', 'Mixed Commissions'),
        ('manual', 'Manual'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    settlement_type = models.CharField(max_length=30, choices=TYPE_CHOICES, db_index=True)
    period_start = models.DateField(db_index=True)
    period_end = models.DateField(db_index=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='draft', db_index=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='finance_settlement_batches_created')
    approved_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='finance_settlement_batches_approved')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    approved_at = models.DateTimeField(null=True, blank=True, db_index=True)
    paid_at = models.DateTimeField(null=True, blank=True, db_index=True)
    notes = models.TextField(blank=True, default='')

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"Settlement {self.id} ({self.status})"


class FinanceSettlementItem(models.Model):
    STATUS_CHOICES = (
        ('pending', 'Pending'),
        ('paid', 'Paid'),
        ('failed', 'Failed'),
    )
    batch = models.ForeignKey(FinanceSettlementBatch, on_delete=models.CASCADE, related_name='items')
    beneficiary = models.ForeignKey(User, on_delete=models.CASCADE, related_name='finance_settlement_items')
    amount = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(Decimal('0.01'))])
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending', db_index=True)
    weekly_commission = models.ForeignKey('commission.WeeklyAgentCommission', on_delete=models.SET_NULL, null=True, blank=True, related_name='finance_settlement_items')
    monthly_commission = models.ForeignKey('commission.MonthlyNetworkCommission', on_delete=models.SET_NULL, null=True, blank=True, related_name='finance_settlement_items')
    agent_payout = models.ForeignKey('AgentPayout', on_delete=models.SET_NULL, null=True, blank=True, related_name='finance_settlement_items')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    paid_at = models.DateTimeField(null=True, blank=True, db_index=True)
    error_message = models.CharField(max_length=255, blank=True, default='')

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.beneficiary.email} ₦{self.amount} ({self.status})"


class ScheduledFinanceReport(models.Model):
    DATASET_CHOICES = (
        ('transactions', 'Transactions'),
        ('deposits', 'Deposits'),
        ('withdrawals', 'Withdrawals'),
        ('ledger', 'Finance Audit Ledger'),
        ('journals', 'Journal Entries'),
        ('settlements', 'Settlements'),
        ('gateway_logs', 'Gateway Logs'),
        ('pin_logs', 'Withdrawal PIN Logs'),
    )
    FORMAT_CHOICES = (
        ('csv', 'CSV'),
        ('xlsx', 'Excel'),
        ('pdf', 'PDF'),
    )
    FREQUENCY_CHOICES = (
        ('daily', 'Daily'),
        ('weekly', 'Weekly'),
        ('monthly', 'Monthly'),
    )

    name = models.CharField(max_length=120)
    dataset = models.CharField(max_length=30, choices=DATASET_CHOICES, db_index=True)
    report_format = models.CharField(max_length=10, choices=FORMAT_CHOICES, default='csv')
    frequency = models.CharField(max_length=10, choices=FREQUENCY_CHOICES, default='daily', db_index=True)
    recipients = models.TextField(blank=True, default='')

    is_active = models.BooleanField(default=True, db_index=True)
    last_run_at = models.DateTimeField(null=True, blank=True, db_index=True)
    next_run_at = models.DateTimeField(null=True, blank=True, db_index=True)
    last_status = models.CharField(max_length=20, blank=True, default='')
    last_error = models.CharField(max_length=255, blank=True, default='')

    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='scheduled_finance_reports_created')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.name} ({self.dataset})"


@receiver(pre_save, sender=BetTicket)
def refund_stake_on_void(sender, instance, **kwargs):
    if instance.pk:
        try:
            old_ticket = BetTicket.objects.get(pk=instance.pk)
            if (
                not BetTicket.is_voided_status_value(old_ticket.status)
                and BetTicket.is_voided_status_value(instance.status)
            ):
                with transaction.atomic():
                    wallet = Wallet.objects.select_for_update().get(user=instance.user)
                    initiating_user = instance.deleted_by if instance.deleted_by else None

                    refund_tx = Transaction.objects.create(
                        user=instance.user,
                        initiating_user=initiating_user,
                        transaction_type='ticket_deletion_refund',
                        amount=instance.stake_amount,
                        is_successful=True,
                        status='completed',
                        description=f"Refund for voided ticket {instance.ticket_id}",
                        related_bet_ticket=instance,
                        timestamp=timezone.now()
                    )
                    wallet.apply_delta(
                        amount=instance.stake_amount,
                        actor=initiating_user if getattr(initiating_user, 'is_authenticated', False) else None,
                        transaction_obj=refund_tx,
                        reference=str(instance.ticket_id),
                        reason=refund_tx.description,
                        metadata={
                            "ticket_id": instance.ticket_id,
                            "source": "ticket_void",
                            "void_status": instance.status,
                        },
                    )
        except BetTicket.DoesNotExist:
            pass


@receiver(pre_save, sender=Fixture)
@receiver(pre_save, sender=Result)
def track_fixture_update_flags(sender, instance, **kwargs):
    if not getattr(instance, "pk", None):
        return

    old = (
        Fixture.objects.filter(pk=instance.pk)
        .only(
            "match_date",
            "match_time",
            "home_win_odd",
            "draw_odd",
            "away_win_odd",
            "home_or_draw_odd",
            "either_team_win_odd",
            "away_or_draw_odd",
            "over_1_5_odd",
            "under_1_5_odd",
            "over_2_5_odd",
            "under_2_5_odd",
            "over_3_5_odd",
            "under_3_5_odd",
            "btts_yes_odd",
            "btts_no_odd",
            "home_dnb_odd",
            "away_dnb_odd",
        )
        .first()
    )
    if old is None:
        return

    datetime_changed = old.match_date != instance.match_date or old.match_time != instance.match_time

    def _norm_decimal(v):
        if v is None:
            return None
        if v == "":
            return None
        try:
            return Decimal(str(v)).quantize(Decimal("0.01"))
        except Exception:
            return str(v)

    odds_changed = False
    increased = False
    decreased = False
    for field in (
        "home_win_odd",
        "draw_odd",
        "away_win_odd",
        "home_or_draw_odd",
        "either_team_win_odd",
        "away_or_draw_odd",
        "over_1_5_odd",
        "under_1_5_odd",
        "over_2_5_odd",
        "under_2_5_odd",
        "over_3_5_odd",
        "under_3_5_odd",
        "btts_yes_odd",
        "btts_no_odd",
        "home_dnb_odd",
        "away_dnb_odd",
    ):
        old_value = _norm_decimal(getattr(old, field, None))
        new_value = _norm_decimal(getattr(instance, field, None))
        if old_value != new_value:
            odds_changed = True
            if old_value is None or new_value is None:
                continue
            if isinstance(old_value, Decimal) and isinstance(new_value, Decimal):
                if new_value > old_value:
                    increased = True
                elif new_value < old_value:
                    decreased = True

    if not datetime_changed and not odds_changed:
        return

    now = timezone.now()
    if datetime_changed:
        instance.datetime_updated_at = now
    if odds_changed:
        instance.odds_updated_at = now
        if increased and decreased:
            instance.odds_update_direction = 'mixed'
        elif increased:
            instance.odds_update_direction = 'up'
        elif decreased:
            instance.odds_update_direction = 'down'
        else:
            instance.odds_update_direction = ''

@receiver(post_save, sender=Fixture)
@receiver(post_save, sender=Result)
def update_tickets_on_fixture_change(sender, instance, created, **kwargs):
    try:
        serial = str(getattr(instance, 'serial_number', '') or '').strip()
        period_id = getattr(instance, "betting_period_id", None)
        relink_q = Q(bet_ticket__status="pending")
        if period_id:
            relink_q &= (Q(betting_period_id=period_id) | Q(betting_period__isnull=True))
        if serial:
            relink_q &= (Q(fixture_serial_number__iexact=serial) | Q(fixture_home_team__iexact=instance.home_team, fixture_away_team__iexact=instance.away_team, fixture_match_date=instance.match_date, fixture_match_time=instance.match_time))
        else:
            relink_q &= Q(fixture_home_team__iexact=instance.home_team, fixture_away_team__iexact=instance.away_team, fixture_match_date=instance.match_date, fixture_match_time=instance.match_time)

        Selection.objects.filter(relink_q).exclude(fixture_id=instance.id).update(
            fixture=instance,
            fixture_serial_number=serial or '',
            fixture_home_team=instance.home_team,
            fixture_away_team=instance.away_team,
            fixture_match_date=instance.match_date,
            fixture_match_time=instance.match_time,
        )
    except Exception:
        pass

    status = str(getattr(instance, 'status', '') or '').strip().lower()
    scores_present = getattr(instance, 'home_score', None) is not None and getattr(instance, 'away_score', None) is not None
    should_recalculate = scores_present or status in {'finished', 'settled', 'cancelled', 'postponed', 'abandoned', 'no_result'}

    if should_recalculate:
        try:
            from .services.ticket_results import recalculate_tickets_for_fixture_sync
            ticket_count = BetTicket.objects.filter(selections__fixture=instance).distinct().count()
            is_test_run = any(arg in ('test', 'pytest') for arg in getattr(sys, 'argv', []) or [])

            def _celery_workers_available():
                cache_key = "betting:celery_workers_available"
                cached = cache.get(cache_key)
                if cached is not None:
                    return bool(cached)
                ok = False
                try:
                    from celery import current_app
                    insp = current_app.control.inspect(timeout=0.5)
                    res = insp.ping() if insp else None
                    ok = bool(res)
                except Exception:
                    ok = False
                cache.set(cache_key, ok, timeout=15)
                return ok

            def _run_in_background():
                close_old_connections()
                try:
                    recalculate_tickets_for_fixture_sync(instance.id)
                except Exception:
                    return
                finally:
                    close_old_connections()

            def _schedule_background_thread():
                t = threading.Thread(
                    target=_run_in_background,
                    name=f"fixture-recalc-{instance.id}",
                    daemon=True,
                )
                t.start()

            if is_test_run or getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False) or getattr(settings, "CELERY_ALWAYS_EAGER", False):
                recalculate_tickets_for_fixture_sync(instance.id)
                return

            use_celery = _celery_workers_available()
            if use_celery:
                try:
                    from .tasks import recalculate_tickets_for_fixture
                    transaction.on_commit(lambda: recalculate_tickets_for_fixture.delay(instance.id))
                    return
                except Exception:
                    pass

            transaction.on_commit(_schedule_background_thread)
        except Exception:
            try:
                recalculate_tickets_for_fixture_sync(instance.id)
            except Exception:
                pass

class CreditRequest(models.Model):
    STATUS_CHOICES = (
        ('pending', 'Pending'),
        ('approved', 'Approved'),
        ('declined', 'Declined'),
    )
    REQUEST_TYPE_CHOICES = (
        ('credit', 'Normal Credit'),
        ('loan', 'Loan'),
        ('crm_credit', 'CRM Credit Approval'),
        ('crm_debit', 'CRM Debit Approval'),
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


class CRMWalletApprovalRequest(CreditRequest):
    class Meta:
        proxy = True
        verbose_name = 'CRM Wallet Approval Request'
        verbose_name_plural = 'CRM Wallet Approval Requests'

class Loan(models.Model):
    STATUS_CHOICES = (
        ('pending', 'Pending'),
        ('active', 'Active'),
        ('rejected', 'Rejected'),
        ('overdue', 'Overdue'),
        ('settled', 'Settled'),
        ('defaulted', 'Defaulted'),
    )
    LOAN_TYPE_CHOICES = (
        ('agent_overdraft', 'Agent Overdraft'),
        ('super_agent_overdraft', 'Super Agent Overdraft'),
        ('manual_overdraft', 'Manual Overdraft'),
    )
    APPROVAL_LEVEL_CHOICES = (
        ('super_agent', 'Super Agent'),
        ('admin', 'Admin'),
    )
    
    borrower = models.ForeignKey(User, on_delete=models.CASCADE, related_name='loans_borrowed')
    lender = models.ForeignKey(User, on_delete=models.CASCADE, related_name='loans_lent')
    amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    requested_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    qualified_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    qualification_ticket_count = models.PositiveIntegerField(default=0)
    qualification_deposit_volume = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    outstanding_balance = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    repaid_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='active')
    loan_type = models.CharField(max_length=30, choices=LOAN_TYPE_CHOICES, default='agent_overdraft')
    approval_level = models.CharField(max_length=20, choices=APPROVAL_LEVEL_CHOICES, default='super_agent')
    approved_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='loans_approved')
    rejected_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='loans_rejected')
    approved_at = models.DateTimeField(null=True, blank=True)
    rejected_at = models.DateTimeField(null=True, blank=True)
    settled_at = models.DateTimeField(null=True, blank=True)
    rejection_reason = models.TextField(blank=True, default='')
    request_reason = models.TextField(blank=True, default='')
    manual_assignment = models.BooleanField(default=False)
    account_locked_due_to_default = models.BooleanField(default=False)
    account_unlocked_after_settlement = models.BooleanField(default=False)
    workflow_snapshot = models.JSONField(blank=True, default=dict)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    due_date = models.DateTimeField(null=True, blank=True)
    credit_request = models.OneToOneField(CreditRequest, on_delete=models.SET_NULL, null=True, blank=True, related_name='loan')
    overdraft_wallet = models.ForeignKey('OverdraftWallet', on_delete=models.SET_NULL, null=True, blank=True, related_name='funded_loans')
    
    def __str__(self):
        return f"Loan: {self.borrower} owes {self.lender} {self.outstanding_balance}"


class LoanPendingCredit(models.Model):
    SOURCE_CHOICES = (
        ('gateway_deposit', 'Gateway Deposit'),
        ('admin_credit', 'Admin Credit'),
        ('account_user_credit', 'Account User Credit'),
        ('crm_credit', 'CRM Credit'),
        ('finance_credit', 'Finance Credit'),
        ('reconcile_credit', 'Reconciled Credit'),
    )

    borrower = models.ForeignKey(User, on_delete=models.CASCADE, related_name='loan_pending_credits')
    source = models.CharField(max_length=30, choices=SOURCE_CHOICES)
    source_transaction = models.ForeignKey(
        Transaction,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='loan_pending_credits',
    )
    amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    remaining_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    recorded_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='loan_pending_credits_recorded',
    )
    note = models.CharField(max_length=255, blank=True, default='')
    metadata = models.JSONField(blank=True, default=dict)
    processed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Loan Pending Credit'
        verbose_name_plural = 'Loan Pending Credits'

    def __str__(self):
        return f"PendingCredit({self.borrower_id}) ₦{self.remaining_amount}"


class OverdraftWallet(models.Model):
    super_agent = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name='overdraft_wallet',
        limit_choices_to={'user_type': 'super_agent'},
    )
    total_funded = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    current_balance = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    updated_at = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['super_agent__username', 'super_agent__email']
        verbose_name = 'Overdraft Wallet'
        verbose_name_plural = 'Overdraft Wallets'

    def __str__(self):
        return f"Overdraft Wallet - {self.super_agent.username or self.super_agent.email}"

    @property
    def used_balance(self):
        return max(Decimal('0.00'), (self.total_funded or Decimal('0.00')) - (self.current_balance or Decimal('0.00')))

    @property
    def remaining_balance(self):
        return self.current_balance or Decimal('0.00')

    def apply_delta(self, *, amount, actor=None, loan=None, reference="", reason="", metadata=None):
        amount_q = Decimal(str(amount)).quantize(Decimal("0.01"))
        with transaction.atomic():
            locked = OverdraftWallet.objects.select_for_update().get(pk=self.pk)
            before = Decimal(str(locked.current_balance or Decimal("0.00"))).quantize(Decimal("0.01"))
            total_before = Decimal(str(locked.total_funded or Decimal("0.00"))).quantize(Decimal("0.01"))
            after = (before + amount_q).quantize(Decimal("0.01"))
            if after < Decimal("0.00"):
                raise ValueError("Overdraft wallet balance cannot be negative.")
            if amount_q > 0:
                locked.total_funded = (total_before + amount_q).quantize(Decimal("0.01"))
            locked.current_balance = after
            locked.save(update_fields=["current_balance", "total_funded", "updated_at"])

            OverdraftWalletLedgerEntry.objects.create(
                wallet=locked,
                super_agent=locked.super_agent,
                actor=actor if getattr(actor, "is_authenticated", False) else None,
                loan=loan,
                direction="credit" if amount_q >= 0 else "debit",
                amount=abs(amount_q),
                balance_before=before,
                balance_after=after,
                reference=(reference or "")[:120],
                reason=(reason or "")[:255],
                metadata=metadata or {},
            )

            self.current_balance = locked.current_balance
            self.total_funded = locked.total_funded
            return before, after


class OverdraftWalletLedgerEntry(models.Model):
    DIRECTION_CHOICES = (
        ("credit", "Credit"),
        ("debit", "Debit"),
    )

    wallet = models.ForeignKey(OverdraftWallet, on_delete=models.CASCADE, related_name='ledger_entries')
    super_agent = models.ForeignKey(User, on_delete=models.CASCADE, related_name='overdraft_wallet_ledger_entries')
    actor = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='overdraft_wallet_actions')
    loan = models.ForeignKey(Loan, on_delete=models.SET_NULL, null=True, blank=True, related_name='overdraft_wallet_entries')
    direction = models.CharField(max_length=10, choices=DIRECTION_CHOICES, db_index=True)
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    balance_before = models.DecimalField(max_digits=14, decimal_places=2)
    balance_after = models.DecimalField(max_digits=14, decimal_places=2)
    reference = models.CharField(max_length=120, blank=True, default='', db_index=True)
    reason = models.CharField(max_length=255, blank=True, default='')
    metadata = models.JSONField(blank=True, default=dict)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Overdraft Wallet Ledger Entry'
        verbose_name_plural = 'Overdraft Wallet Ledger Entries'

    def __str__(self):
        return f"OverdraftLedger({self.super_agent_id}) {self.direction} ₦{self.amount}"


class LoanRepayment(models.Model):
    SOURCE_CHOICES = (
        ('gateway_deposit', 'Gateway Deposit'),
        ('admin_credit', 'Admin Credit'),
        ('manual_settlement', 'Manual Settlement'),
        ('finance_credit', 'Finance Credit'),
        ('reconcile_credit', 'Reconciled Credit'),
    )

    loan = models.ForeignKey(Loan, on_delete=models.CASCADE, related_name='repayments')
    borrower = models.ForeignKey(User, on_delete=models.CASCADE, related_name='loan_repayments')
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    source = models.CharField(max_length=30, choices=SOURCE_CHOICES)
    source_transaction = models.ForeignKey(Transaction, on_delete=models.SET_NULL, null=True, blank=True, related_name='loan_repayments')
    recorded_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='loan_repayments_recorded')
    note = models.CharField(max_length=255, blank=True, default='')
    metadata = models.JSONField(blank=True, default=dict)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Loan Repayment'
        verbose_name_plural = 'Loan Repayments'

    def __str__(self):
        return f"Repayment ₦{self.amount} for loan #{self.loan_id}"


class LoanAuditLog(models.Model):
    ACTION_CHOICES = (
        ('request_submitted', 'Request Submitted'),
        ('approved', 'Approved'),
        ('rejected', 'Rejected'),
        ('manual_assigned', 'Manual Assigned'),
        ('credit_reserved', 'Credit Reserved'),
        ('repayment_received', 'Repayment Received'),
        ('auto_deduction', 'Automatic Deduction'),
        ('account_locked', 'Account Locked'),
        ('account_unlocked', 'Account Unlocked'),
        ('override', 'Override'),
        ('loan_cleared', 'Loan Cleared'),
    )

    loan = models.ForeignKey(Loan, on_delete=models.CASCADE, related_name='audit_logs')
    borrower = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='loan_audit_events')
    performed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='loan_audit_actions')
    action = models.CharField(max_length=30, choices=ACTION_CHOICES, db_index=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    reason = models.TextField(blank=True, default='')
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    metadata = models.JSONField(blank=True, default=dict)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Loan Audit Log'
        verbose_name_plural = 'Loan Audit Logs'

    def __str__(self):
        return f"{self.action} - loan #{self.loan_id}"

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

class ImpersonationLog(models.Model):
    admin_user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='impersonation_logs')
    target_user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='impersonated_logs')
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(null=True, blank=True)
    started_at = models.DateTimeField(auto_now_add=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    duration = models.DurationField(null=True, blank=True)
    reason = models.TextField(null=True, blank=True)
    termination_reason = models.CharField(max_length=50, null=True, blank=True)

    def __str__(self):
        return f"{self.admin_user} impersonated {self.target_user} at {self.started_at}"


class WebAuthnCredential(models.Model):
    user = models.ForeignKey('User', on_delete=models.CASCADE, related_name='webauthn_credentials')
    credential_id = models.BinaryField(unique=True)
    public_key = models.BinaryField()
    sign_count = models.IntegerField(default=0)
    device_name = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.device_name} ({self.user.email})"

class BiometricAuthLog(models.Model):
    ACTION_CHOICES = (
        ('register', 'Registration'),
        ('login', 'Login'),
        ('revoke', 'Revocation'),
    )
    STATUS_CHOICES = (
        ('success', 'Success'),
        ('failed', 'Failed'),
    )
    user = models.ForeignKey('User', on_delete=models.CASCADE, related_name='biometric_logs')
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    device_name = models.CharField(max_length=255, null=True, blank=True)
    action = models.CharField(max_length=20, choices=ACTION_CHOICES)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES)
    timestamp = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.user.email} - {self.action} - {self.status}"
