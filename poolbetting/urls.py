from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static

# Import your custom admin site instance
from betting.admin import betting_admin_site
from notifications import views as notification_views

urlpatterns = [
    # IMPORTANT: Use your custom admin site's URLs here.
    # This single line handles all paths under 'admin/', including default models
    # and your custom admin management views (fixtures, users, tickets report, etc.).
    path('admin/', betting_admin_site.urls),

    # Include ALL other betting app URLs (non-admin paths like frontpage, wallet, etc.)
    # IMPORTANT: Add the 'namespace' argument here for your betting app.
    path('', include(('betting.urls', 'betting'), namespace='betting')),
    path('sw.js', notification_views.service_worker, name='service_worker'),
    path('notifications/', include(('notifications.urls', 'notifications'), namespace='notifications')),
    path('commission/', include('commission.urls')),
    path('uip/', include('uip.urls')),
    path('registration/', include('pending_registration.urls')),
    path("ckeditor5/", include("django_ckeditor_5.urls")),
]

if settings.DEBUG:
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

    # Enable error page preview in DEBUG mode
    from django.views.defaults import page_not_found, server_error
    urlpatterns += [
        path('404/', lambda request: page_not_found(request, Exception("Test 404"))),
        path('500/', server_error),
    ]
