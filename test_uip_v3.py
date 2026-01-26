import os
import django
from django.conf import settings

# Configure Django settings
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'poolbetting.settings')
django.setup()

from django.test import RequestFactory
from django.contrib.auth import get_user_model
from uip.views import (
    uip_dashboard, uip_financials, uip_analytics, uip_risk, 
    uip_forecasting, uip_reports, uip_audit,
    export_financials, export_agents, export_audit
)
from uip.alerts import AlertService

User = get_user_model()

def test_views():
    factory = RequestFactory()
    
    # Create a superuser for testing access
    user, created = User.objects.get_or_create(email='testadmin@example.com', user_type='admin', is_superuser=True)
    
    views_to_test = [
        ('/uip/dashboard/', uip_dashboard, "Dashboard"),
        ('/uip/financials/', uip_financials, "Financials"),
        ('/uip/analytics/', uip_analytics, "Analytics"),
        ('/uip/risk/', uip_risk, "Risk"),
        ('/uip/forecasting/', uip_forecasting, "Forecasting"),
        ('/uip/reports/', uip_reports, "Reports Hub"),
        ('/uip/audit/', uip_audit, "Audit Log"),
    ]

    print("\n--- Testing UI Views ---")
    for url, view_func, name in views_to_test:
        request = factory.get(url)
        request.user = user
        try:
            response = view_func(request)
            if response.status_code == 200:
                print(f"✅ {name} View: OK")
            else:
                print(f"❌ {name} View: Failed with status {response.status_code}")
        except Exception as e:
            print(f"❌ {name} View: Error - {str(e)}")

    print("\n--- Testing Exports ---")
    export_views = [
        ('/uip/export/financials/', export_financials, "Export Financials"),
        ('/uip/export/agents/', export_agents, "Export Agents"),
        ('/uip/export/audit/', export_audit, "Export Audit"),
    ]
    
    for url, view_func, name in export_views:
        request = factory.get(url)
        request.user = user
        try:
            response = view_func(request)
            if response.status_code == 200:
                if response['Content-Type'] == 'text/csv':
                    print(f"✅ {name}: OK (CSV Generated)")
                else:
                    print(f"⚠️ {name}: OK but unexpected Content-Type: {response['Content-Type']}")
            else:
                print(f"❌ {name}: Failed with status {response.status_code}")
        except Exception as e:
            print(f"❌ {name}: Error - {str(e)}")

    print("\n--- Testing Alert Service ---")
    try:
        # Just run the check function, don't worry about email failure (it's silent)
        AlertService.check_and_send_alerts()
        print("✅ AlertService.check_and_send_alerts() ran successfully")
    except Exception as e:
        print(f"❌ AlertService Error: {str(e)}")

if __name__ == '__main__':
    test_views()
