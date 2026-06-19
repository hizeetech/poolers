from django.conf import settings
from django.db import models


class Notification(models.Model):
    TYPE_CHOICES = (
        ("DEPOSIT_SUCCESS", "Deposit Successful"),
        ("WITHDRAWAL_APPROVED", "Withdrawal Approved"),
        ("WITHDRAWAL_REJECTED", "Withdrawal Rejected"),
        ("BONUS_AWARDED", "Bonus Awarded"),
        ("TICKET_SETTLED", "Ticket Settled"),
        ("EVENT_SUSPENDED", "Event Suspended"),
        ("EVENT_ABANDONED", "Event Abandoned"),
        ("FIXTURE_POSTPONED", "Fixture Postponed"),
        ("ODDS_CHANGED", "Odds Changed"),
        ("TICKET_VOIDED", "Ticket Voided"),
        ("SYSTEM_ANNOUNCEMENT", "System Announcement"),
        ("RISK_ALERT", "Risk Alert"),
        ("DEPOSIT_REMINDER", "Deposit Reminder"),
        ("LOAN_REQUEST_SUBMITTED", "Loan Request Submitted"),
        ("LOAN_REQUEST_PENDING_REVIEW", "Loan Request Pending Review"),
        ("LOAN_APPROVED", "Loan Approved"),
        ("LOAN_REJECTED", "Loan Rejected"),
        ("LOAN_REPAYMENT_RECEIVED", "Loan Repayment Received"),
        ("LOAN_ACCOUNT_LOCKED", "Loan Account Locked"),
        ("LOAN_ACCOUNT_UNLOCKED", "Loan Account Unlocked"),
        ("LOAN_CLEARED", "Loan Cleared"),
        ("LOAN_MANUAL_ASSIGNED", "Manual Loan Assigned"),
    )

    recipient = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notifications")
    notification_type = models.CharField(max_length=40, choices=TYPE_CHOICES, db_index=True)
    title = models.CharField(max_length=160)
    message = models.TextField(blank=True, default="")
    data = models.JSONField(blank=True, default=dict)

    is_read = models.BooleanField(default=False, db_index=True)
    read_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["recipient", "is_read", "created_at"])]

    def __str__(self):
        return f"Notification({self.recipient_id},{self.notification_type})"


class SystemAnnouncement(models.Model):
    title = models.CharField(max_length=160)
    message = models.TextField()
    is_active = models.BooleanField(default=True, db_index=True)
    starts_at = models.DateTimeField(null=True, blank=True, db_index=True)
    ends_at = models.DateTimeField(null=True, blank=True, db_index=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.title


class NotificationCampaign(models.Model):
    title = models.CharField(max_length=160)
    message = models.TextField()
    notification_type = models.CharField(max_length=40, default="SYSTEM_ANNOUNCEMENT")

    send_to_all = models.BooleanField(default=True)
    target_user_types = models.JSONField(blank=True, default=list)
    target_user_ids = models.JSONField(blank=True, default=list)

    send_now = models.BooleanField(default=False)
    sent_at = models.DateTimeField(null=True, blank=True, db_index=True)

    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.title


class WebPushSubscription(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="webpush_subscriptions")
    endpoint = models.TextField(unique=True)
    p256dh = models.CharField(max_length=255)
    auth = models.CharField(max_length=255)
    user_agent = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=["user", "created_at"])]

    def __str__(self):
        return f"WebPushSubscription({self.user_id})"
