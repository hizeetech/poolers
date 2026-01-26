import logging
from django.shortcuts import redirect
from django.contrib import messages
from django.utils import timezone
from django.conf import settings
from django.contrib.auth import logout

logger = logging.getLogger(__name__)

class UIPSecurityMiddleware:
    """
    Middleware to enforce strict security policies for the Unified Intelligence Platform (UIP).
    Includes:
    1. Comprehensive IP Logging for all UIP access.
    2. Strict Session Timeout (30 minutes idle) for admin/staff/account users in UIP.
    """
    
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Only apply to UIP paths
        if request.path.startswith('/uip/'):
            # 1. IP Logging
            ip = self.get_client_ip(request)
            user = request.user
            
            if user.is_authenticated:
                # Log access (info level)
                # In a real production setup, this might go to a dedicated security log file
                logger.info(f"UIP Access: User {user.email} (ID: {user.id}) from IP {ip} accessing {request.path}")
            
            # 2. Strict Session Timeout
            # Check if user is authorized for UIP (Admin, Superuser, Account User)
            if user.is_authenticated and (user.is_staff or user.is_superuser or getattr(user, 'user_type', '') == 'account_user'):
                last_activity = request.session.get('uip_last_activity')
                now = timezone.now().timestamp()
                
                # Timeout duration: 30 minutes (1800 seconds)
                TIMEOUT_SECONDS = 1800
                
                if last_activity:
                    if now - last_activity > TIMEOUT_SECONDS:
                        # Clear the specific session key first
                        del request.session['uip_last_activity']
                        
                        # Logout the user
                        logout(request)
                        
                        # Redirect to login with message
                        messages.warning(request, "Your session has expired due to inactivity in the secure UIP area.")
                        return redirect('betting:login')
                
                # Update last activity timestamp
                request.session['uip_last_activity'] = now

        response = self.get_response(request)
        return response

    def get_client_ip(self, request):
        x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
        if x_forwarded_for:
            ip = x_forwarded_for.split(',')[0]
        else:
            ip = request.META.get('REMOTE_ADDR')
        return ip
