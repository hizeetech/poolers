from django.shortcuts import redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.contrib.auth.hashers import make_password
from django.urls import reverse
from .forms import AgentRegistrationForm

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

            messages.success(request, "Agent registration submitted for approval. Login details will be sent after admin approval.")
            return redirect(request.META.get('HTTP_REFERER') or fallback_url)
        else:
            for field, errors in form.errors.items():
                for error in errors:
                    messages.error(request, f"{field}: {error}")
            return redirect(request.META.get('HTTP_REFERER') or fallback_url)
            
    return redirect(request.META.get('HTTP_REFERER') or fallback_url)
