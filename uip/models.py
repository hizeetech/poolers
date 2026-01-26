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

class UIPDashboardLink(DailyMetricSnapshot):
    class Meta:
        proxy = True
        verbose_name = "UIP Dashboard"
        verbose_name_plural = "Open UIP Dashboard"
