from django.urls import path
from . import views

app_name = 'uip'

urlpatterns = [
    path('dashboard/', views.uip_dashboard, name='dashboard'),
    path('financials/', views.uip_financials, name='financials'),
    path('analytics/', views.uip_analytics, name='analytics'),
    path('risk/', views.uip_risk, name='risk'),
    path('forecasting/', views.uip_forecasting, name='forecasting'),
    path('reports/', views.uip_reports, name='reports'),
    path('audit/', views.uip_audit, name='audit'),
    path('export/financials/', views.export_financials, name='export_financials'),
    path('export/agents/', views.export_agents, name='export_agents'),
    path('export/audit/', views.export_audit, name='export_audit'),
]
