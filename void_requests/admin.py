from django.contrib import admin
from django.contrib import messages

from .models import TicketVoidRequest, TicketVoidAuditLog
from .services import approve_and_void_request, reject_void_request


class TicketVoidRequestAdmin(admin.ModelAdmin):
    list_display = ("ticket", "cashier", "agent", "status", "requested_at", "auto_void_at", "approved_by", "approved_at", "is_processed")
    list_filter = ("status", "requested_at", "auto_void_at", "approved_at", "is_processed")
    search_fields = ("ticket__ticket_id", "cashier__email", "cashier__username", "agent__email", "agent__username")
    readonly_fields = ("requested_at", "auto_void_at", "approved_at", "created_at", "updated_at")
    actions = ("void_selected_tickets", "reject_selected_requests")

    @admin.action(description="Void Selected Tickets")
    def void_selected_tickets(self, request, queryset):
        ok = 0
        skipped = 0
        for vr in queryset:
            try:
                approve_and_void_request(void_request_id=vr.id, approved_by=request.user, is_auto=False)
                ok += 1
            except Exception as e:
                skipped += 1
                messages.error(request, f"Failed: {vr.ticket.ticket_id} ({e})")
        if ok:
            messages.success(request, f"Voided {ok} tickets.")
        if skipped:
            messages.warning(request, f"Skipped {skipped} requests.")

    @admin.action(description="Reject Selected Requests")
    def reject_selected_requests(self, request, queryset):
        ok = 0
        skipped = 0
        for vr in queryset:
            try:
                reject_void_request(void_request_id=vr.id, rejected_by=request.user)
                ok += 1
            except Exception as e:
                skipped += 1
                messages.error(request, f"Failed: {vr.ticket.ticket_id} ({e})")
        if ok:
            messages.success(request, f"Rejected {ok} requests.")
        if skipped:
            messages.warning(request, f"Skipped {skipped} requests.")


class TicketVoidAuditLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "action", "ticket", "cashier", "agent", "admin", "amount_refunded", "old_status", "new_status")
    list_filter = ("action", "created_at")
    search_fields = ("ticket__ticket_id", "cashier__email", "cashier__username", "agent__email", "agent__username", "admin__email", "admin__username")
    readonly_fields = [f.name for f in TicketVoidAuditLog._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
