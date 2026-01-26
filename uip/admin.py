from django.contrib import admin
from django.shortcuts import redirect
from django.urls import reverse
from .models import DailyMetricSnapshot, Alert, UIPDashboardLink
from .tasks import aggregate_daily_metrics
from django.utils import timezone

@admin.register(DailyMetricSnapshot)
class DailyMetricSnapshotAdmin(admin.ModelAdmin):
    list_display = ('date', 'total_stake_volume', 'gross_gaming_revenue', 'net_profit', 'total_tickets_sold')
    list_filter = ('date',)
    date_hierarchy = 'date'
    actions = ['run_aggregation_now']
    
    def run_aggregation_now(self, request, queryset):
        # Trigger aggregation for the selected dates (if any) or just run for yesterday
        # Since this action applies to selected objects, we can re-calculate them.
        for snapshot in queryset:
            aggregate_daily_metrics.delay(date_str=str(snapshot.date))
        self.message_user(request, "Aggregation tasks queued for selected dates.")
    run_aggregation_now.short_description = "Re-calculate metrics for selected dates"

@admin.register(Alert)
class AlertAdmin(admin.ModelAdmin):
    list_display = ('title', 'severity', 'is_resolved', 'created_at')
    list_filter = ('severity', 'is_resolved')
    search_fields = ('title', 'message')

@admin.register(UIPDashboardLink)
class UIPDashboardLinkAdmin(admin.ModelAdmin):
    """
    Proxy admin to redirect to the custom UIP Dashboard.
    """
    def changelist_view(self, request, extra_context=None):
        return redirect('uip:dashboard')
    
    def has_add_permission(self, request):
        return False
        
    def has_change_permission(self, request, obj=None):
        return False
        
    def has_delete_permission(self, request, obj=None):
        return False

    def has_module_permission(self, request):
        return True

    def has_view_permission(self, request, obj=None):
        return True
