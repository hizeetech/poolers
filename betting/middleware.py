from django.utils import timezone
from django.shortcuts import redirect
from django.contrib import messages
from django.urls import reverse
from .models import ImpersonationLog

class ImpersonationMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Check if impersonation is active
        if request.session.get('impersonation_active'):
            # Check for timeout
            started_at_str = request.session.get('impersonation_started_at')
            if started_at_str:
                from datetime import datetime
                # Handle potential format differences if stored as string vs datetime
                # Django sessions serializer (JSON) stores datetime as string usually
                try:
                    # If it's a timestamp (float)
                    if isinstance(started_at_str, float):
                        started_at = timezone.datetime.fromtimestamp(started_at_str, tz=timezone.utc)
                    else:
                        started_at = timezone.datetime.fromisoformat(started_at_str)
                except:
                    # Fallback or current time if parsing fails (shouldn't happen if consistent)
                     started_at = timezone.now()

                # 30 minutes timeout
                if (timezone.now() - started_at).total_seconds() > 1800: # 30 * 60
                    # Timeout expired
                    return self.force_stop_impersonation(request, "Timeout")

            request.impersonation_active = True
            request.original_user_id = request.session.get('original_admin_id')
            # The user is already switched in auth middleware if we used login(), 
            # but we need to ensure the banner knows who is who.
            # Actually, standard logic is:
            # 1. Admin logs in as User.
            # 2. request.user IS User.
            # 3. Session has 'original_admin_id'.
            
            request.impersonated_user = request.user
            
        else:
            request.impersonation_active = False

        response = self.get_response(request)
        return response

    def force_stop_impersonation(self, request, reason):
        # Logic to stop impersonation if timeout
        # We need to call the view logic or replicate it here.
        # Ideally, redirect to the stop endpoint.
        from django.contrib.auth import login, get_user_model
        User = get_user_model()
        
        original_admin_id = request.session.get('original_admin_id')
        log_id = request.session.get('impersonation_log_id')
        
        if original_admin_id:
            try:
                original_user = User.objects.get(pk=original_admin_id)
                login(request, original_user) # Switch back
            except User.DoesNotExist:
                pass # Should not happen
        
        # Update log
        if log_id:
            try:
                log = ImpersonationLog.objects.get(pk=log_id)
                log.ended_at = timezone.now()
                log.duration = log.ended_at - log.started_at
                log.termination_reason = reason
                log.save()
            except ImpersonationLog.DoesNotExist:
                pass

        # Clear session
        keys_to_pop = ['impersonation_active', 'original_admin_id', 'impersonation_started_at', 'impersonation_log_id']
        for key in keys_to_pop:
            request.session.pop(key, None)
            
        messages.warning(request, f"Impersonation ended due to {reason}.")
        return redirect('betting_admin:dashboard')

class ThreadLocalMiddleware:
    import threading
    _thread_locals = threading.local()

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        self._thread_locals.request = request
        response = self.get_response(request)
        if hasattr(self._thread_locals, 'request'):
            del self._thread_locals.request
        return response

def get_current_request():
    return getattr(ThreadLocalMiddleware._thread_locals, 'request', None)

def get_current_user():
    request = get_current_request()
    if request:
        return getattr(request, 'user', None)
    return None

