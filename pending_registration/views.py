from django.shortcuts import redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.contrib.auth.hashers import make_password
from django.conf import settings
from django.core.mail import send_mail
from django.template.loader import render_to_string
from django.utils.html import strip_tags
from django.urls import reverse
from .forms import AgentRegistrationForm
from betting.services.usernames import generate_cashier_email

def is_agent_creator(user):
    return user.is_authenticated and user.user_type in ['master_agent', 'super_agent', 'agent']

@login_required
def register_agent(request):
    if request.user.user_type == 'master_agent':
        fallback_url = reverse('betting:master_agent_dashboard')
    elif request.user.user_type == 'super_agent':
        fallback_url = reverse('betting:super_agent_dashboard')
    else:
        fallback_url = reverse('betting:agent_dashboard')

    if not is_agent_creator(request.user):
        messages.error(request, "You are not authorized to register an agent.")
        return redirect(request.META.get('HTTP_REFERER') or fallback_url)

    if request.method == 'POST':
        form = AgentRegistrationForm(request.POST)
        if form.is_valid():
            pending_agent = form.save(commit=False)
            raw_password = form.cleaned_data['password']
            pending_agent.password = make_password(raw_password)
            pending_agent.registered_by = request.user
            
            # Hierarchy Logic
            if request.user.user_type == 'master_agent':
                pending_agent.master_agent = request.user
            elif request.user.user_type == 'super_agent':
                pending_agent.super_agent = request.user
                pending_agent.master_agent = request.user.master_agent # Inherit master agent if applicable
            elif request.user.user_type == 'agent':
                pending_agent.super_agent = request.user.super_agent
                pending_agent.master_agent = request.user.master_agent or getattr(request.user.super_agent, 'master_agent', None)
            
            pending_agent.save()

            cashier_emails = [
                generate_cashier_email(pending_agent.email, "C1"),
                generate_cashier_email(pending_agent.email, "C2"),
            ]

            login_url = request.build_absolute_uri(reverse('betting:login'))
            html_message = render_to_string('pending_registration/email/agent_submitted.html', {
                'pending_agent': pending_agent,
                'cashier_emails': cashier_emails,
                'login_url': login_url,
                'password': raw_password,
            })
            send_mail(
                subject='Agent Registration Submitted',
                message=strip_tags(html_message),
                from_email=settings.DEFAULT_FROM_EMAIL or settings.EMAIL_HOST_USER,
                recipient_list=[pending_agent.email],
                html_message=html_message,
                fail_silently=True
            )

            messages.success(request, "Agent registration submitted for approval.")
            return redirect(request.META.get('HTTP_REFERER') or fallback_url)
        else:
            for field, errors in form.errors.items():
                for error in errors:
                    messages.error(request, f"{field}: {error}")
            return redirect(request.META.get('HTTP_REFERER') or fallback_url)
            
    return redirect(request.META.get('HTTP_REFERER') or fallback_url)
