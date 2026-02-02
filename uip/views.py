from django.shortcuts import render
from django.contrib.auth.decorators import login_required, user_passes_test
from django.core.paginator import Paginator
from django.utils import timezone
from django.http import JsonResponse
from betting.models import ActivityLog, BettingPeriod
from .services import DashboardService
from .forecasting import ForecastingService
from .reporting import ReportingService

def is_admin_or_executive(user):
    return user.is_authenticated and (user.is_superuser or user.user_type in ['admin', 'account_user'])

@login_required
@user_passes_test(is_admin_or_executive)
def uip_dashboard(request):
    metrics = DashboardService.get_live_metrics()
    leaderboard = DashboardService.get_agent_leaderboard()
    betting_periods = BettingPeriod.objects.filter(is_active=True).order_by('-start_date')[:10]
    context = {
        'metrics': metrics,
        'leaderboard': leaderboard,
        'betting_periods': betting_periods,
        'page_title': 'Unified Intelligence Platform'
    }
    return render(request, 'uip/dashboard.html', context)

@login_required
@user_passes_test(is_admin_or_executive)
def uip_financials(request):
    metrics = DashboardService.get_financial_metrics()
    context = {
        'metrics': metrics,
        'page_title': 'Financial Intelligence'
    }
    return render(request, 'uip/financials.html', context)

@login_required
@user_passes_test(is_admin_or_executive)
def uip_analytics(request):
    metrics = DashboardService.get_analytics_metrics()
    context = {
        'metrics': metrics,
        'page_title': 'Agent & User Analytics'
    }
    return render(request, 'uip/analytics.html', context)

@login_required
@user_passes_test(is_admin_or_executive)
def uip_risk(request):
    metrics = DashboardService.get_risk_metrics()
    context = {
        'metrics': metrics,
        'page_title': 'Risk & Fraud Monitoring'
    }
    return render(request, 'uip/risk.html', context)

@login_required
@user_passes_test(is_admin_or_executive)
def uip_forecasting(request):
    turnover_forecast = ForecastingService.predict_turnover()
    peak_periods = ForecastingService.identify_peak_periods()
    
    context = {
        'forecast': turnover_forecast,
        'peak_periods': peak_periods,
        'page_title': 'Forecasting & Projections'
    }
    return render(request, 'uip/forecasting.html', context)

@login_required
@user_passes_test(is_admin_or_executive)
def uip_reports(request):
    return render(request, 'uip/reports.html', {'page_title': 'Automated Reporting'})

@login_required
@user_passes_test(is_admin_or_executive)
def uip_audit(request):
    # Filtering
    logs = ActivityLog.objects.all().order_by('-timestamp')
    
    email = request.GET.get('email')
    if email:
        logs = logs.filter(user__email__icontains=email)
        
    action_type = request.GET.get('action_type')
    if action_type:
        logs = logs.filter(action_type=action_type)
        
    date_str = request.GET.get('date')
    if date_str:
        logs = logs.filter(timestamp__date=date_str)

    paginator = Paginator(logs, 50) # Show 50 contacts per page.
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)
    
    context = {
        'logs': page_obj,
        'page_title': 'Audit & Compliance Logs'
    }
    return render(request, 'uip/audit.html', context)

@login_required
@user_passes_test(is_admin_or_executive)
def serial_number_analytics(request):
    """
    API endpoint for real-time serial number frequency chart.
    """
    start_date = request.GET.get('start_date') or None
    end_date = request.GET.get('end_date') or None
    scope = request.GET.get('scope') or 'all'
    user_id = request.GET.get('user_id') or None
    period_id = request.GET.get('period_id') or None

    data = DashboardService.get_serial_number_frequency(
        start_date=start_date,
        end_date=end_date,
        scope=scope,
        user_id=user_id,
        period_id=period_id
    )
    return JsonResponse(data)

@login_required
@user_passes_test(is_admin_or_executive)
def export_financials(request):
    start_date = request.GET.get('start_date') or timezone.now().date()
    end_date = request.GET.get('end_date') or timezone.now().date()
    return ReportingService.export_financial_report(start_date, end_date)

@login_required
@user_passes_test(is_admin_or_executive)
def export_agents(request):
    start_date = request.GET.get('start_date') or timezone.now().date()
    end_date = request.GET.get('end_date') or timezone.now().date()
    return ReportingService.export_agent_performance_report(start_date, end_date)

@login_required
@user_passes_test(is_admin_or_executive)
def export_audit(request):
    start_date = request.GET.get('start_date') or timezone.now().date()
    end_date = request.GET.get('end_date') or timezone.now().date()
    return ReportingService.export_audit_log_report(start_date, end_date)
