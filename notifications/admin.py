from django.contrib import admin

from .models import Notification, SystemAnnouncement, WebPushSubscription, NotificationCampaign


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ("created_at", "recipient", "notification_type", "title", "is_read")
    list_filter = ("notification_type", "is_read", "created_at")
    search_fields = ("recipient__email", "recipient__username", "title", "message")
    readonly_fields = [f.name for f in Notification._meta.fields]
    date_hierarchy = "created_at"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(SystemAnnouncement)
class SystemAnnouncementAdmin(admin.ModelAdmin):
    list_display = ("created_at", "title", "is_active", "starts_at", "ends_at", "created_by")
    list_filter = ("is_active", "created_at")
    search_fields = ("title", "message")

    def save_model(self, request, obj, form, change):
        if not obj.created_by:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)


@admin.register(WebPushSubscription)
class WebPushSubscriptionAdmin(admin.ModelAdmin):
    list_display = ("user", "created_at", "updated_at")
    search_fields = ("user__email", "user__username", "endpoint")
    readonly_fields = [f.name for f in WebPushSubscription._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(NotificationCampaign)
class NotificationCampaignAdmin(admin.ModelAdmin):
    list_display = ("created_at", "title", "notification_type", "send_to_all", "send_now", "sent_at", "created_by")
    list_filter = ("send_to_all", "sent_at", "created_at")
    search_fields = ("title", "message")
    readonly_fields = ("created_at", "updated_at", "sent_at", "created_by")

    def save_model(self, request, obj, form, change):
        if not obj.created_by:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)
