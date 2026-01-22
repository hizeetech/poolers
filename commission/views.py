from django.shortcuts import get_object_or_404
from django.http import JsonResponse
from django.contrib.auth.decorators import login_required
from .models import CommissionPeriod
from betting.models import User
from .services import calculate_weekly_agent_commission_data

@login_required
def get_commission_calculation(request):
    agent_id = request.GET.get('agent_id')
    period_id = request.GET.get('period_id')
    
    if not agent_id or not period_id:
        return JsonResponse({'error': 'Missing parameters'}, status=400)
    
    try:
        agent = User.objects.get(pk=agent_id, user_type__in=['agent', 'super_agent', 'master_agent'])
        period = CommissionPeriod.objects.get(pk=period_id)
        
        data = calculate_weekly_agent_commission_data(agent, period)
        if data:
            # Convert decimals to strings for JSON serialization
            json_data = {k: str(v) for k, v in data.items()}
            return JsonResponse(json_data)
        else:
             return JsonResponse({'error': 'Calculation failed (Check profile)'}, status=400)
            
    except (User.DoesNotExist, CommissionPeriod.DoesNotExist):
        return JsonResponse({'error': 'Invalid Agent or Period'}, status=404)
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)
