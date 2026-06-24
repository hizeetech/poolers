from django.db import models
from django.conf import settings
from django.core.validators import MinValueValidator, MaxValueValidator
from django.utils.translation import gettext_lazy as _
from decimal import Decimal
from betting.models import User as BettingUser
from django.utils import timezone

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
    assigned_at = models.DateTimeField(default=timezone.now, db_index=True)
    assigned_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='commission_profiles_assigned')
    assigned_by_role = models.CharField(max_length=30, blank=True, default='')
    last_changed_at = models.DateTimeField(default=timezone.now, db_index=True)
    last_changed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='commission_profiles_changed')
    last_change_reason = models.CharField(max_length=255, blank=True, default='')
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    updated_at = models.DateTimeField(default=timezone.now)

    def __str__(self):
        return f"{self.user} - {self.plan}"


class CommissionProfileAssignmentLog(models.Model):
    agent = models.ForeignKey(User, on_delete=models.CASCADE, related_name='commission_assignment_logs', limit_choices_to={'user_type': 'agent'})
    previous_profile = models.ForeignKey(CommissionPlan, on_delete=models.SET_NULL, null=True, blank=True, related_name='assignment_logs_previous')
    new_profile = models.ForeignKey(CommissionPlan, on_delete=models.PROTECT, related_name='assignment_logs_new')
    assigned_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='commission_assignment_logs_by')
    assigned_by_role = models.CharField(max_length=30, blank=True, default='')
    assignment_reason = models.CharField(max_length=255, blank=True, default='')
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    device_info = models.TextField(blank=True, default='')
    is_override = models.BooleanField(default=False, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ('-created_at',)

    def __str__(self):
        return f"{self.agent} • {self.previous_profile} → {self.new_profile}"


class CommissionOverrideLog(models.Model):
    agent = models.ForeignKey(User, on_delete=models.CASCADE, related_name='commission_override_logs', limit_choices_to={'user_type': 'agent'})
    old_profile = models.ForeignKey(CommissionPlan, on_delete=models.SET_NULL, null=True, blank=True, related_name='override_logs_old')
    new_profile = models.ForeignKey(CommissionPlan, on_delete=models.PROTECT, related_name='override_logs_new')
    admin_user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='commission_override_logs_by')
    reason = models.CharField(max_length=255, blank=True, default='')
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    device_info = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ('-created_at',)

    def __str__(self):
        return f"{self.agent} • {self.old_profile} → {self.new_profile}"


class CommissionChangeRequest(models.Model):
    STATUS_CHOICES = (
        ('pending', 'Pending'),
        ('approved', 'Approved'),
        ('rejected', 'Rejected'),
    )
    agent = models.ForeignKey(User, on_delete=models.CASCADE, related_name='commission_change_requests', limit_choices_to={'user_type': 'agent'})
    requested_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='commission_change_requests_by')
    current_profile = models.ForeignKey(CommissionPlan, on_delete=models.SET_NULL, null=True, blank=True, related_name='change_requests_current')
    requested_profile = models.ForeignKey(CommissionPlan, on_delete=models.PROTECT, related_name='change_requests_requested')
    reason = models.CharField(max_length=255, blank=True, default='')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending', db_index=True)
    decided_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='commission_change_requests_decided')
    decided_at = models.DateTimeField(null=True, blank=True, db_index=True)
    decision_note = models.CharField(max_length=255, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ('-created_at',)

    def __str__(self):
        return f"{self.agent} • {self.current_profile} → {self.requested_profile} ({self.status})"

class CommissionPeriod(models.Model):
    PERIOD_TYPE_CHOICES = (
        ('weekly', 'Weekly'),
        ('monthly', 'Monthly'),
    )
    period_type = models.CharField(max_length=10, choices=PERIOD_TYPE_CHOICES)
    start_date = models.DateField()
    end_date = models.DateField()
    is_active = models.BooleanField(default=True)
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
        ('approved', 'Approved'),
        ('partially_paid', 'Partially Paid'),
        ('paid', 'Paid'),
    )
    agent = models.ForeignKey(User, on_delete=models.CASCADE, related_name='weekly_commissions')
    period = models.ForeignKey(CommissionPeriod, on_delete=models.CASCADE, related_name='agent_commissions')
    
    total_stake = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    total_winnings = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    ggr = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    single_stake = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    single_winnings = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    single_ggr = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    multiple_stake = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    multiple_winnings = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    multiple_ggr = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    
    commission_ggr_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    commission_hybrid_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    commission_total_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)

    commission_single_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    commission_multiple_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    is_marked_for_payment = models.BooleanField(default=False, verbose_name="Pay Now?")
    created_at = models.DateTimeField(auto_now_add=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    amount_paid = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    paid_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='weekly_commissions_paid_by')
    paid_source = models.CharField(max_length=20, blank=True, default='')
    paid_from_user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='weekly_commissions_paid_from')
    
    class Meta:
        unique_together = ('agent', 'period')
        verbose_name = "Weekly Agent Commission"
        verbose_name_plural = "Weekly Agent Commissions"

class PaidWeeklyAgentCommission(WeeklyAgentCommission):
    class Meta:
        proxy = True
        verbose_name = "Commission Paid"
        verbose_name_plural = "Commission Paid"

class MonthlyNetworkCommission(models.Model):
    STATUS_CHOICES = (
        ('pending', 'Pending'),
        ('approved', 'Approved'),
        ('partially_paid', 'Partially Paid'),
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
    
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    created_at = models.DateTimeField(auto_now_add=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    amount_paid = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    paid_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='monthly_commissions_paid_by')
    paid_source = models.CharField(max_length=20, blank=True, default='')
    paid_from_user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='monthly_commissions_paid_from')

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


class CommissionRecall(models.Model):
    RECALL_REASON_CHOICES = (
        ('wrong_calculation', 'Wrong Calculation'),
        ('premature_payment', 'Premature Payment'),
        ('duplicate_payment', 'Duplicate Payment'),
        ('fraud_investigation', 'Fraud Investigation'),
        ('wrong_commission_profile', 'Wrong Commission Profile'),
        ('compliance_issue', 'Compliance Issue'),
        ('administrative_error', 'Administrative Error'),
        ('other', 'Other'),
    )

    STATUS_CHOICES = (
        ('executed', 'Executed'),
        ('pending_approval', 'Pending Approval'),
        ('rejected', 'Rejected'),
        ('failed', 'Failed'),
    )

    weekly_commission = models.ForeignKey(WeeklyAgentCommission, on_delete=models.CASCADE, null=True, blank=True, related_name='recall_requests')
    monthly_commission = models.ForeignKey(MonthlyNetworkCommission, on_delete=models.CASCADE, null=True, blank=True, related_name='recall_requests')
    beneficiary = models.ForeignKey(User, on_delete=models.CASCADE, related_name='commission_recalls')
    period = models.ForeignKey(CommissionPeriod, on_delete=models.CASCADE, related_name='commission_recalls')

    amount_requested = models.DecimalField(max_digits=12, decimal_places=2)
    recall_reason = models.CharField(max_length=50, choices=RECALL_REASON_CHOICES)
    other_reason_text = models.CharField(max_length=255, blank=True, default='')
    notes = models.TextField(blank=True, default='')

    requested_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='commission_recalls_requested')
    requested_by_role = models.CharField(max_length=30, blank=True, default='')
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    device_info = models.CharField(max_length=255, blank=True, default='')

    status = models.CharField(max_length=30, choices=STATUS_CHOICES, default='executed')
    executed_at = models.DateTimeField(null=True, blank=True)

    decided_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='commission_recalls_decided')
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.CharField(max_length=255, blank=True, default='')

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ('-created_at',)
        permissions = [
            ('can_recall_commission', 'Can recall commission'),
            ('can_approve_commission_recall', 'Can approve commission recall'),
        ]

    def __str__(self):
        return f"Recall {self.amount_requested} • {self.beneficiary} • {self.period} ({self.status})"


class CommissionRecallLog(models.Model):
    recall = models.ForeignKey(CommissionRecall, on_delete=models.PROTECT, related_name='logs')
    weekly_commission = models.ForeignKey(WeeklyAgentCommission, on_delete=models.SET_NULL, null=True, blank=True, related_name='recall_logs')
    monthly_commission = models.ForeignKey(MonthlyNetworkCommission, on_delete=models.SET_NULL, null=True, blank=True, related_name='recall_logs')

    agent = models.ForeignKey(User, on_delete=models.CASCADE, related_name='commission_recall_logs')
    amount_recalled = models.DecimalField(max_digits=12, decimal_places=2)
    recall_reason = models.CharField(max_length=50, choices=CommissionRecall.RECALL_REASON_CHOICES)
    notes = models.TextField(blank=True, default='')

    recalled_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='commission_recalls_executed')
    recalled_by_role = models.CharField(max_length=30, blank=True, default='')

    recall_date = models.DateField()
    recall_time = models.TimeField()
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    device_info = models.CharField(max_length=255, blank=True, default='')

    old_status = models.CharField(max_length=30, blank=True, default='')
    new_status = models.CharField(max_length=30, blank=True, default='')
    old_amount_paid = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    new_amount_paid = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    old_total_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    new_total_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ('-created_at',)

    def __str__(self):
        return f"RecallLog {self.amount_recalled} • {self.agent} • {self.recall_date}"


class CommissionRecallApproval(models.Model):
    STATUS_CHOICES = (
        ('approved', 'Approved'),
        ('rejected', 'Rejected'),
    )

    recall = models.OneToOneField(CommissionRecall, on_delete=models.CASCADE, related_name='approval')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES)
    decided_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='commission_recall_approvals')
    decided_at = models.DateTimeField(auto_now_add=True)
    note = models.CharField(max_length=255, blank=True, default='')

    class Meta:
        ordering = ('-decided_at',)

    def __str__(self):
        return f"RecallApproval {self.status} • {self.recall_id}"
