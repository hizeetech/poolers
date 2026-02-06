import os
import django
from django.test import RequestFactory
from django.urls import reverse
from django.contrib.auth import get_user_model

# Setup Django environment
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'poolbetting.settings')
django.setup()

User = get_user_model()
from betting.views import admin_manual_wallet_manager
from betting.admin import betting_admin_site

def test_manual_wallet_manager_url_and_template():
    print("Starting test...")

    # 1. Verify URL resolving
    try:
        url = reverse('betting_admin:admin_manual_wallet_manager')
        print(f"URL 'betting_admin:admin_manual_wallet_manager' resolves to: {url}")
    except Exception as e:
        print(f"FAILED to resolve URL: {e}")
        return

    # 2. Simulate a request to the admin dashboard (or any admin page that uses base.html)
    # We want to check if the sidebar link is present in the rendered response
    # We'll use the 'dashboard' view for this test
    dashboard_url = reverse('betting_admin:dashboard')
    print(f"Testing sidebar presence on Dashboard URL: {dashboard_url}")

    factory = RequestFactory()
    request = factory.get(dashboard_url)
    
    # Add session middleware to support request.session
    from django.contrib.sessions.middleware import SessionMiddleware
    middleware = SessionMiddleware(lambda x: None)
    middleware.process_request(request)
    request.session.save()
    
    # Also add messages middleware if needed (likely)
    from django.contrib.messages.middleware import MessageMiddleware
    middleware_msg = MessageMiddleware(lambda x: None)
    middleware_msg.process_request(request)

    # Create a superuser for the test
    password = 'testpassword'
    try:
        user = User.objects.get(email='test_superuser@example.com')
    except User.DoesNotExist:
        user = User.objects.create_superuser(
            email='test_superuser@example.com',
            password=password,
            user_type='admin'
        )
    request.user = user

    # Get the view function for the dashboard
    # The view is wrapped by admin_view, but we can call the underlying view logic if we can access it
    # Or simpler: render the base template directly with context
    
    from django.template.loader import render_to_string
    
    # Let's try rendering the base template directly to see if the link is there
    # We need to mock 'request' in the context because the template uses 'request.resolver_match'
    
    # A better approach: Use the actual dashboard view
    from betting.views import admin_dashboard
    
    # We need to handle the admin_site wrapper if we call the view directly?
    # Actually, views.admin_dashboard is just a function. 
    # But betting_admin_site.admin_view wraps it.
    
    # Let's call the view function directly.
    response = admin_dashboard(request)
    
    if response.status_code == 200:
        content = response.content.decode('utf-8')
        
        # Check for the link text
        search_text = "Manual Credit/Debit"
        
        # Extract and print the sidebar content for verification
        import re
        sidebar_match = re.search(r'<ul class="nav flex-column">(.*?)</ul>', content, re.DOTALL)
        if sidebar_match:
            print("\n--- RENDERED SIDEBAR HTML ---")
            print(sidebar_match.group(1))
            print("-----------------------------\n")
        
        if search_text in content:
            print(f"SUCCESS: Found '{search_text}' in the rendered output.")
        else:
            print(f"FAILURE: Could not find '{search_text}' in the rendered output.")
            
            # Print nearby lines for debugging
            # Look for "Withdrawal Requests" which is before it
            if "Withdrawal Requests" in content:
                print("Found 'Withdrawal Requests'. Checking what follows...")
                parts = content.split("Withdrawal Requests")
                if len(parts) > 1:
                    print(parts[1][:500]) # Print next 500 chars
            else:
                print("Could not find 'Withdrawal Requests' either. Something is wrong with the template used.")
                
        # Check for the URL
        if url in content:
            print(f"SUCCESS: Found URL '{url}' in the rendered output.")
        else:
            print(f"FAILURE: Could not find URL '{url}' in the rendered output.")
            
    else:
        print(f"FAILED: Dashboard view returned status code {response.status_code}")

if __name__ == "__main__":
    test_manual_wallet_manager_url_and_template()
