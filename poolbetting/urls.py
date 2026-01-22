from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static

# Import your custom admin site instance
from betting.admin import betting_admin_site

urlpatterns = [
    # IMPORTANT: Use your custom admin site's URLs here.
    # This single line handles all paths under 'admin/', including default models
    # and your custom admin management views (fixtures, users, tickets report, etc.).
    path('admin/', betting_admin_site.urls),

    # Include ALL other betting app URLs (non-admin paths like frontpage, wallet, etc.)
    # IMPORTANT: Add the 'namespace' argument here for your betting app.
    path('', include(('betting.urls', 'betting'), namespace='betting')),
    path('commission/', include('commission.urls')),
]

if settings.DEBUG:
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
