from django.conf import settings
from django.db import models
from django.utils import timezone


class TicketVoidRequest(models.Model):
    STATUS_PENDING = "pending"
    STATUS_APPROVED = "approved"
    STATUS_REJECTED = "rejected"
    STATUS_AUTO_VOIDED = "auto_voided"

    STATUS_CHOICES = (
        (STATUS_PENDING, "Pending"),
        (STATUS_APPROVED, "Approved"),
        (STATUS_REJECTED, "Rejected"),
        (STATUS_AUTO_VOIDED, "Auto Voided"),
    )

    ticket = models.OneToOneField("betting.BetTicket", on_delete=models.CASCADE, related_name="void_request")
    cashier = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="ticket_void_requests_as_cashier")
    agent = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="ticket_void_requests_as_agent")
    requested_at = models.DateTimeField(default=timezone.now, db_index=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING, db_index=True)
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="ticket_void_requests_approved")
    approved_at = models.DateTimeField(null=True, blank=True)
    auto_void_at = models.DateTimeField(null=True, blank=True, db_index=True)
    reason = models.TextField(blank=True, default="")
    is_processed = models.BooleanField(default=False, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created_at",)

    def __str__(self):
        return f"VoidRequest({self.ticket.ticket_id}) {self.status}"


class TicketVoidAuditLog(models.Model):
    ACTION_REQUEST_CREATED = "request_created"
    ACTION_APPROVED = "approved"
    ACTION_REJECTED = "rejected"
    ACTION_AUTO_VOIDED = "auto_voided"
    ACTION_REFUNDED = "refunded"

    ACTION_CHOICES = (
        (ACTION_REQUEST_CREATED, "Request Created"),
        (ACTION_APPROVED, "Approved"),
        (ACTION_REJECTED, "Rejected"),
        (ACTION_AUTO_VOIDED, "Auto Voided"),
        (ACTION_REFUNDED, "Refunded"),
    )

    void_request = models.ForeignKey(TicketVoidRequest, on_delete=models.CASCADE, related_name="audit_logs")
    ticket = models.ForeignKey("betting.BetTicket", on_delete=models.CASCADE, related_name="void_audit_logs")
    cashier = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="void_audit_logs_as_cashier")
    agent = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="void_audit_logs_as_agent")
    admin = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="void_audit_logs_as_admin")
    action = models.CharField(max_length=30, choices=ACTION_CHOICES, db_index=True)
    old_status = models.CharField(max_length=30, blank=True, default="")
    new_status = models.CharField(max_length=30, blank=True, default="")
    amount_refunded = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ("-created_at",)

    def __str__(self):
        return f"VoidAudit({self.ticket.ticket_id}) {self.action}"


class CashierVoidPermission(models.Model):
    agent = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="cashier_void_permissions_as_agent")
    cashier = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="cashier_void_permissions_as_cashier")
    can_request_void = models.BooleanField(default=False, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("agent", "cashier")
        ordering = ("-updated_at",)

    def __str__(self):
        return f"CashierVoidPermission({self.agent_id}->{self.cashier_id})={self.can_request_void}"
