from django.db import models
from django.utils import timezone
from betting.models import User

class DailyMetricSnapshot(models.Model):
    """
    Stores aggregated metrics for a specific day.
    """
    date = models.DateField(unique=True, db_index=True)
    
    # Financials
    total_stake_volume = models.DecimalField(max_digits=15, decimal_places=2, default=0, db_index=True)
    total_winnings_paid = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    gross_gaming_revenue = models.DecimalField(max_digits=15, decimal_places=2, default=0) # Stake - Winnings
    net_profit = models.DecimalField(max_digits=15, decimal_places=2, default=0) # GGR - Commissions - Bonuses (simplified)
    
    # Operational
    total_tickets_sold = models.PositiveIntegerField(default=0)
    active_users_count = models.PositiveIntegerField(default=0)
    
    # Channel Split
    online_tickets_count = models.PositiveIntegerField(default=0)
    retail_tickets_count = models.PositiveIntegerField(default=0)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Metrics for {self.date}"

class Alert(models.Model):
    SEVERITY_CHOICES = (
        ('info', 'Info'),
        ('warning', 'Warning'),
        ('critical', 'Critical'),
    )
    
    title = models.CharField(max_length=255)
    message = models.TextField()
    severity = models.CharField(max_length=20, choices=SEVERITY_CHOICES, default='info')
    is_resolved = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    
    def __str__(self):
        return f"[{self.severity.upper()}] {self.title}"

class FraudAlert(models.Model):
    ALERT_TYPES = (
        ('multi_account', 'Multi-Account Detected'),
        ('bonus_abuse', 'Bonus Abuse Suspected'),
        ('suspicious_betting', 'Suspicious Betting Pattern'),
        ('high_value_bet', 'High Value Bet'),
        ('vpn_usage', 'VPN Usage Detected'),
        ('shared_device', 'Shared Device Detected'),
        ('payment_fraud', 'Payment Method Fraud'),
    )
    SEVERITY_LEVELS = (
        ('low', 'LOW'),
        ('medium', 'MEDIUM'),
        ('high', 'HIGH'),
        ('critical', 'CRITICAL'),
    )
    STATUS_CHOICES = (
        ('pending', 'Pending'),
        ('under_investigation', 'Under Investigation'),
        ('resolved_safe', 'Resolved (Safe)'),
        ('resolved_fraud', 'Resolved (Fraud)'),
        ('escalated', 'Escalated'),
    )

    alert_type = models.CharField(max_length=50, choices=ALERT_TYPES)
    severity = models.CharField(max_length=20, choices=SEVERITY_LEVELS, default='medium')
    description = models.TextField()
    detection_source = models.CharField(max_length=100, default='system')
    timestamp = models.DateTimeField(auto_now_add=True, db_index=True)
    status = models.CharField(max_length=30, choices=STATUS_CHOICES, default='pending', db_index=True)
    admin_notes = models.TextField(blank=True, null=True)
    
    related_ips = models.JSONField(default=list, blank=True)
    related_devices = models.JSONField(default=list, blank=True)
    
    affected_users = models.ManyToManyField(User, through='AlertAffectedUser', related_name='fraud_alerts')

    class Meta:
        ordering = ['-timestamp']
        verbose_name = "Fraud Alert"
        verbose_name_plural = "Fraud Alerts"

    def __str__(self):
        return f"{self.get_alert_type_display()} - {self.severity.upper()} ({self.status})"

class AlertAffectedUser(models.Model):
    alert = models.ForeignKey(FraudAlert, on_delete=models.CASCADE, related_name='affected_user_details')
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='alert_details')
    
    # Snapshot at detection
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    device_fingerprint = models.CharField(max_length=255, null=True, blank=True)
    risk_score = models.IntegerField(default=0)
    
    # Cached metrics for display
    last_activity_time = models.DateTimeField(null=True, blank=True)
    wallet_balance = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    total_deposits = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    total_withdrawals = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    total_bets_count = models.IntegerField(default=0)
    
    class Meta:
        unique_together = ('alert', 'user')
        verbose_name = "Alert Affected User"
        verbose_name_plural = "Alert Affected Users"

class InvestigationCase(models.Model):
    alert = models.OneToOneField(FraudAlert, on_delete=models.CASCADE, related_name='investigation_case')
    assigned_admin = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='assigned_cases')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    # Audit trail is handled by separate AdminActionLog or similar
    
    def __str__(self):
        return f"Case for Alert #{self.alert.id}"

class AdminActionLog(models.Model):
    alert = models.ForeignKey(FraudAlert, on_delete=models.CASCADE, related_name='action_logs')
    admin = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    action = models.CharField(max_length=100)
    notes = models.TextField(blank=True, null=True)
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-timestamp']
        verbose_name = "Admin Action Log"
        verbose_name_plural = "Admin Action Logs"

class UIPDashboardLink(DailyMetricSnapshot):
    class Meta:
        proxy = True
        verbose_name = "UIP Dashboard"
        verbose_name_plural = "Open UIP Dashboard"
