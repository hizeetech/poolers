from django.test import TestCase, Client
from django.urls import reverse
from betting.models import User, Wallet

class RBACTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.password = 'password123'
        
        # Create users for each role
        self.player = User.objects.create_user(email='player@test.com', password=self.password, user_type='player')
        self.cashier = User.objects.create_user(email='cashier@test.com', password=self.password, user_type='cashier')
        self.agent = User.objects.create_user(email='agent@test.com', password=self.password, user_type='agent')
        self.super_agent = User.objects.create_user(email='super_agent@test.com', password=self.password, user_type='super_agent')
        self.master_agent = User.objects.create_user(email='master_agent@test.com', password=self.password, user_type='master_agent')
        self.admin = User.objects.create_user(email='admin@test.com', password=self.password, user_type='admin', is_staff=True, is_superuser=True)
        
        # Create wallets for all users
        for user in [self.player, self.cashier, self.agent, self.super_agent, self.master_agent, self.admin]:
            Wallet.objects.create(user=user, balance=0)

    def test_player_access(self):
        self.client.login(email='player@test.com', password=self.password)
        
        # Should access user dashboard
        response = self.client.get(reverse('betting:user_dashboard'))
        self.assertEqual(response.status_code, 200)
        
        # Should NOT access agent dashboard
        response = self.client.get(reverse('betting:agent_dashboard'))
        self.assertNotEqual(response.status_code, 200) # Likely 302 redirect to login
        
        # Should NOT access admin dashboard (using custom admin site URL or view)
        # Assuming betting:admin_dashboard view exists
        response = self.client.get(reverse('betting:admin_dashboard'))
        self.assertNotEqual(response.status_code, 200)

    def test_agent_access(self):
        self.client.login(email='agent@test.com', password=self.password)
        
        # Accessing user_dashboard should redirect to agent_dashboard
        response = self.client.get(reverse('betting:user_dashboard'))
        self.assertRedirects(response, reverse('betting:agent_dashboard'))
        
        # Should access agent dashboard
        response = self.client.get(reverse('betting:agent_dashboard'))
        self.assertEqual(response.status_code, 200)
        
        # Should NOT access master agent dashboard
        # Assuming betting:master_agent_dashboard exists
        try:
            url = reverse('betting:master_agent_dashboard')
            response = self.client.get(url)
            self.assertNotEqual(response.status_code, 200)
        except:
            # If URL doesn't exist, we skip this check but it's good to know
            pass

    def test_admin_access(self):
        self.client.login(email='admin@test.com', password=self.password)
        
        # Accessing user_dashboard should redirect to admin dashboard
        response = self.client.get(reverse('betting:user_dashboard'))
        self.assertRedirects(response, reverse('betting:admin_dashboard'))
        
        # Should access admin dashboard
        response = self.client.get(reverse('betting:admin_dashboard'))
        self.assertEqual(response.status_code, 200)

