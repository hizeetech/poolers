from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib import messages
from django.contrib.auth.hashers import make_password
from .forms import AgentRegistrationForm

def is_agent_creator(user):
    return user.is_authenticated and user.user_type in ['master_agent', 'super_agent']

@login_required
@user_passes_test(is_agent_creator)
def register_agent(request):
    if request.method == 'POST':
        form = AgentRegistrationForm(request.POST)
        if form.is_valid():
            pending_agent = form.save(commit=False)
            pending_agent.password = make_password(form.cleaned_data['password'])
            pending_agent.registered_by = request.user
            
            # Hierarchy Logic
            if request.user.user_type == 'master_agent':
                pending_agent.master_agent = request.user
            elif request.user.user_type == 'super_agent':
                pending_agent.super_agent = request.user
                pending_agent.master_agent = request.user.master_agent # Inherit master agent if applicable
            
            pending_agent.save()
            messages.success(request, "Agent registration submitted for approval.")
            return redirect(request.META.get('HTTP_REFERER', '/'))
        else:
            for field, errors in form.errors.items():
                for error in errors:
                    messages.error(request, f"{field}: {error}")
            return redirect(request.META.get('HTTP_REFERER', '/'))
            
    return redirect('/')
