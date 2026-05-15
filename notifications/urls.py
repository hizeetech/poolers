from django.urls import path

from . import views

app_name = "notifications"

urlpatterns = [
    path("", views.notification_center, name="center"),
    path("api/list/", views.api_notifications_list, name="api_list"),
    path("api/unread-count/", views.api_unread_count, name="api_unread_count"),
    path("api/mark-read/", views.api_notifications_mark_read, name="api_mark_read"),
    path("api/mark-all-read/", views.api_notifications_mark_all_read, name="api_mark_all_read"),
    path("api/webpush/subscribe/", views.api_webpush_subscribe, name="api_webpush_subscribe"),
    path("api/webpush/unsubscribe/", views.api_webpush_unsubscribe, name="api_webpush_unsubscribe"),
]
