from django.test import TestCase, Client
from django.urls import reverse
from betting.models import User, Wallet
from decimal import Decimal

class AccountUserTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.password = 'password123'
        
        # Create Account User
        self.account_user = User.objects.create_user(
            email='account_user@test.com', 
            password=self.password, 
            user_type='account_user'
        )
        
        # Create Super Admin
        self.super_admin = User.objects.create_superuser(
            email='superadmin@test.com', 
            password=self.password
        )

        # Create Regular Player
        self.player = User.objects.create_user(
            email='player@test.com', 
            password=self.password, 
            user_type='player'
        )
        
        # Ensure wallets exist
        for user in [self.account_user, self.super_admin, self.player]:
            Wallet.objects.get_or_create(user=user, defaults={'balance': Decimal('0.00')})

    def test_account_user_dashboard_access(self):
        self.client.force_login(self.account_user)
        
        url = reverse('betting:account_user_dashboard')
        response = self.client.get(url)
        
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'betting/account_user_dashboard.html')

    def test_player_cannot_access_account_user_dashboard(self):
        self.client.force_login(self.player)
        
        url = reverse('betting:account_user_dashboard')
        response = self.client.get(url)
        
        # Should be redirected (likely to login or home) or 403 Forbidden
        # Assuming user_passes_test redirects to login URL if check fails
        self.assertNotEqual(response.status_code, 200)

    def test_super_admin_fund_account_user_access(self):
        self.client.force_login(self.super_admin)
        
        url = reverse('betting:super_admin_fund_account_user')
        response = self.client.get(url)
        
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'betting/super_admin_fund_account_user.html')

    def test_account_user_cannot_access_funding_page(self):
        self.client.force_login(self.account_user)
        
        url = reverse('betting:super_admin_fund_account_user')
        response = self.client.get(url)
        
        # Should be redirected or 403
        self.assertNotEqual(response.status_code, 200)

    def test_account_user_search_supports_username_name_email_phone(self):
        self.client.force_login(self.account_user)
        u = User.objects.create_user(
            email='john.doe@test.com',
            password=self.password,
            user_type='player',
            username='JohnDoe99',
            first_name='John',
            last_name='Doe',
            phone_number='08012345678',
        )
        Wallet.objects.get_or_create(user=u, defaults={'balance': Decimal('0.00')})

        url = reverse('betting:account_user_dashboard')

        resp1 = self.client.post(url, {'search_user': '1', 'search_term': 'JohnDoe99'})
        self.assertEqual(resp1.status_code, 200)
        self.assertContains(resp1, 'john.doe@test.com')

        resp2 = self.client.post(url, {'search_user': '1', 'search_term': 'John Doe'})
        self.assertEqual(resp2.status_code, 200)
        self.assertContains(resp2, 'john.doe@test.com')

        resp3 = self.client.post(url, {'search_user': '1', 'search_term': '08012345678'})
        self.assertEqual(resp3.status_code, 200)
        self.assertContains(resp3, 'john.doe@test.com')
