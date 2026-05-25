from django.shortcuts import redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.contrib.auth.hashers import make_password
from django.urls import reverse
from django.apps import apps
from .forms import AgentRegistrationForm

def is_agent_creator(user):
    return user.is_authenticated and user.user_type in ['master_agent', 'super_agent', 'agent', 'retail_manager']

@login_required
def register_agent(request):
    if request.user.user_type == 'master_agent':
        fallback_url = reverse('betting:master_agent_dashboard')
    elif request.user.user_type == 'super_agent':
        fallback_url = reverse('betting:super_agent_dashboard')
    elif request.user.user_type == 'retail_manager':
        fallback_url = reverse('betting:retail_dashboard')
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
            elif request.user.user_type == 'retail_manager':
                if pending_agent.user_type != 'agent':
                    messages.error(request, "Retail Managers can only register Agents.")
                    return redirect(request.META.get('HTTP_REFERER') or fallback_url)
                super_agent_id = (request.POST.get('super_agent_id') or '').strip()
                if not super_agent_id:
                    messages.error(request, "Select a Super Agent for this registration.")
                    return redirect(request.META.get('HTTP_REFERER') or fallback_url)
                try:
                    super_agent_id_int = int(super_agent_id)
                except Exception:
                    messages.error(request, "Invalid Super Agent selection.")
                    return redirect(request.META.get('HTTP_REFERER') or fallback_url)
                RetailManagerSuperAgentMapping = apps.get_model('betting', 'RetailManagerSuperAgentMapping')
                allowed_ids = RetailManagerSuperAgentMapping.objects.filter(retail_manager=request.user).values_list('super_agent_id', flat=True)
                User = apps.get_model('betting', 'User')
                sa = User.objects.filter(id__in=allowed_ids, id=super_agent_id_int, user_type='super_agent').select_related('master_agent').first()
                if not sa:
                    messages.error(request, "You are not allowed to register under that Super Agent.")
                    return redirect(request.META.get('HTTP_REFERER') or fallback_url)
                pending_agent.super_agent = sa
                pending_agent.master_agent = getattr(sa, 'master_agent', None)
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
