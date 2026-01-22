from django.db import models
from django.conf import settings
from django.core.validators import MinValueValidator, MaxValueValidator
from django.utils.translation import gettext_lazy as _
from decimal import Decimal
from betting.models import User as BettingUser

User = settings.AUTH_USER_MODEL

class CommissionPlan(models.Model):
    name = models.CharField(max_length=100, unique=True)
    description = models.TextField(blank=True)
    
    # GGR Settings
    ggr_percent = models.DecimalField(
        max_digits=5, decimal_places=2, default=0.00, 
        help_text="Percentage of GGR (Profit) to pay as commission.",
        validators=[MinValueValidator(0), MaxValueValidator(100)]
    )
    ggr_payment_day = models.CharField(
        max_length=15, 
        choices=[
            ('0', 'Monday'), ('1', 'Tuesday'), ('2', 'Wednesday'), 
            ('3', 'Thursday'), ('4', 'Friday'), ('5', 'Saturday'), ('6', 'Sunday')
        ],
        default='0',
        help_text="Day of the week to process GGR commissions"
    )
    
    # Hybrid Settings
    is_hybrid_active = models.BooleanField(default=False, help_text="Enable selection-based commission logic.")
    
    # Single Selection Override
    enable_single_selection_override = models.BooleanField(default=False)
    SINGLE_CALC_TYPE_CHOICES = (
        ('percentage_stake', 'Percentage of Stake'),
        ('percentage_ggr', 'Percentage of GGR'),
        ('fixed_value', 'Fixed Value'),
    )
    single_selection_calc_type = models.CharField(max_length=20, choices=SINGLE_CALC_TYPE_CHOICES, default='percentage_stake')
    single_selection_value = models.DecimalField(max_digits=10, decimal_places=2, default=0.00, help_text="Percentage or Fixed Amount")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.name

class HybridCommissionRule(models.Model):
    plan = models.ForeignKey(CommissionPlan, on_delete=models.CASCADE, related_name='hybrid_rules')
    min_selections = models.PositiveIntegerField()
    max_selections = models.PositiveIntegerField(null=True, blank=True, help_text="Leave blank for 'and above'")
    commission_percent = models.DecimalField(
        max_digits=5, decimal_places=2, 
        help_text="Percentage of Stake for this selection range",
        validators=[MinValueValidator(0), MaxValueValidator(100)]
    )

    class Meta:
        ordering = ['min_selections']
        unique_together = ('plan', 'min_selections')

    def __str__(self):
        range_str = f"{self.min_selections}"
        if self.max_selections:
            range_str += f"-{self.max_selections}"
        else:
            range_str += "+"
        return f"{self.plan.name}: {range_str} Sels -> {self.commission_percent}%"

class AgentCommissionProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='commission_profile')
    plan = models.ForeignKey(CommissionPlan, on_delete=models.PROTECT, related_name='assigned_agents')
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.user} - {self.plan}"

class CommissionPeriod(models.Model):
    PERIOD_TYPE_CHOICES = (
        ('weekly', 'Weekly'),
        ('monthly', 'Monthly'),
    )
    period_type = models.CharField(max_length=10, choices=PERIOD_TYPE_CHOICES)
    start_date = models.DateField()
    end_date = models.DateField()
    is_processed = models.BooleanField(default=False)
    processed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-start_date']
        unique_together = ('period_type', 'start_date', 'end_date')

    def __str__(self):
        return f"{self.get_period_type_display()} ({self.start_date} - {self.end_date})"

class WeeklyAgentCommission(models.Model):
    STATUS_CHOICES = (
        ('pending', 'Pending'),
        ('paid', 'Paid'),
    )
    agent = models.ForeignKey(User, on_delete=models.CASCADE, related_name='weekly_commissions')
    period = models.ForeignKey(CommissionPeriod, on_delete=models.CASCADE, related_name='agent_commissions')
    
    total_stake = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    total_winnings = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    ggr = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    
    commission_ggr_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    commission_hybrid_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    commission_total_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    is_marked_for_payment = models.BooleanField(default=False, verbose_name="Pay Now?")
    created_at = models.DateTimeField(auto_now_add=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    
    class Meta:
        unique_together = ('agent', 'period')
        verbose_name = "Weekly Agent Commission"
        verbose_name_plural = "Weekly Agent Commissions"

class MonthlyNetworkCommission(models.Model):
    STATUS_CHOICES = (
        ('pending', 'Pending'),
        ('paid', 'Paid'),
    )
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='monthly_commissions')
    period = models.ForeignKey(CommissionPeriod, on_delete=models.CASCADE, related_name='network_commissions')
    
    role = models.CharField(max_length=20) # 'super_agent' or 'master_agent'
    
    downline_stake = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    downline_winnings = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    downline_paid_commissions = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    ngr = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    
    commission_percent = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    commission_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='pending')
    created_at = models.DateTimeField(auto_now_add=True)
    paid_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = ('user', 'period')
        verbose_name = "Monthly Network Commission"
        verbose_name_plural = "Monthly Network Commissions"

class NetworkCommissionSettings(models.Model):
    ROLE_CHOICES = (('super_agent', 'Super Agent'), ('master_agent', 'Master Agent'))
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, unique=True)
    commission_percent = models.DecimalField(max_digits=5, decimal_places=2, default=0, validators=[MinValueValidator(0), MaxValueValidator(100)])
    payout_day_description = models.CharField(max_length=100, default="Last Friday") 

    def __str__(self):
        return f"{self.get_role_display()} Settings"

class RetailTransaction(BettingUser):
    class Meta:
        proxy = True
        verbose_name = "Retail Transaction"
        verbose_name_plural = "Retail Transactions"
