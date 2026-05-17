from django.urls import path
from . import views

app_name = 'uip'

urlpatterns = [
    path('dashboard/', views.uip_dashboard, name='dashboard'),
    path('financials/', views.uip_financials, name='financials'),
    path('analytics/', views.uip_analytics, name='analytics'),
    path('risk/', views.uip_risk, name='risk'),
    path('leaderboards/', views.uip_leaderboards, name='leaderboards'),
    path('forecasting/', views.uip_forecasting, name='forecasting'),
    path('reports/', views.uip_reports, name='reports'),
    path('audit/', views.uip_audit, name='audit'),
    path('api/serial-analytics/', views.serial_number_analytics, name='serial_analytics'),
    path('export/financials/', views.export_financials, name='export_financials'),
    path('export/agents/', views.export_agents, name='export_agents'),
    path('export/audit/', views.export_audit, name='export_audit'),
    path('api/investigation/action/', views.investigation_user_action, name='investigation_action'),
    path('api/investigation/status/', views.update_fraud_alert_status, name='update_investigation_status'),
    path('api/investigation/note/', views.add_fraud_alert_note, name='add_investigation_note'),
    path('api/investigation/sync/', views.sync_investigation_alerts, name='sync_investigation'),
    path('export/investigation/<int:alert_id>/', views.export_investigation_report, name='export_investigation'),
]
