from django.test import TestCase, Client
from django.urls import reverse
from django.contrib.auth import get_user_model

class SmokeTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.password = 'password123'
        User = get_user_model()
        
        # Create Admin User
        self.admin = User.objects.create_user(
            email='smokeadmin@test.com', 
            password=self.password, 
            user_type='admin', 
            is_staff=True, 
            is_superuser=True
        )

    def test_public_urls(self):
        """Test public URLs are reachable."""
        urls = [
            'betting:frontpage',
            'betting:register',
            'betting:login',
            'betting:fixtures',
        ]
        for url_name in urls:
            url = reverse(url_name)
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200, f"Failed public URL: {url}")

    def test_protected_urls_admin(self):
        """Test protected URLs for admin user."""
        self.client.force_login(self.admin)
        
        urls = [
            'betting:profile',
            'betting:wallet',
            'betting_admin:dashboard', 
            'uip:dashboard',
            'uip:financials',
            'uip:risk',
        ]
        for url_name in urls:
            url = reverse(url_name)
            response = self.client.get(url)
            # Admin dashboard might be 200.
            # Some views might redirect.
            self.assertIn(response.status_code, [200, 302], f"Failed protected URL: {url}")

    def test_dashboard_redirection(self):
        """Test dashboard redirection for admin."""
        self.client.force_login(self.admin)
        url = reverse('betting:user_dashboard')
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)
        # Should redirect to admin dashboard
        # Note: exact location might depend on how reverse resolves 'betting_admin:dashboard'
        # but we just check it redirects.
