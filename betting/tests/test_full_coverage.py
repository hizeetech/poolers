from django.test import TestCase, Client
from django.urls import reverse
from betting.models import User, Wallet, Fixture, BettingPeriod, BetTicket, UserWithdrawal, Transaction, ActivityLog
from django.utils import timezone
import datetime

class FullCoverageTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.password = 'password123'
        
        # Create Admin User
        self.admin = User.objects.create_user(
            email='admin@test.com', 
            password=self.password, 
            user_type='admin', 
            is_staff=True, 
            is_superuser=True,
            first_name='Admin',
            last_name='User'
        )
        Wallet.objects.create(user=self.admin, balance=1000)
        
        # Create Regular User
        self.user = User.objects.create_user(
            email='user@test.com', 
            password=self.password, 
            user_type='player',
            first_name='Regular',
            last_name='User'
        )
        Wallet.objects.create(user=self.user, balance=500)
        
        # Create Agent
        self.agent = User.objects.create_user(
            email='agent@test.com', 
            password=self.password, 
            user_type='agent',
            first_name='Agent',
            last_name='User'
        )
        Wallet.objects.create(user=self.agent, balance=5000)

        # Create Betting Period
        self.period = BettingPeriod.objects.create(
            name="Test Period",
            start_date=timezone.now().date(),
            end_date=timezone.now().date() + datetime.timedelta(days=7),
            is_active=True
        )

        # Create Fixture
        self.fixture = Fixture.objects.create(
            home_team="Team A",
            away_team="Team B",
            match_date=timezone.now().date(),
            match_time=timezone.now().time(),
            betting_period=self.period,
            serial_number="123",
            is_active=True
        )

        # Create Withdrawal Request
        self.withdrawal = UserWithdrawal.objects.create(
            user=self.user,
            amount=100,
            bank_name="Test Bank",
            account_number="1234567890",
            account_name="Test User",
            status='pending'
        )

    def test_all_urls_admin(self):
        """Test that Admin can access all pages without error (200 OK)."""
        self.client.login(email='admin@test.com', password=self.password)
        
        urls_to_test = [
            ('betting:frontpage', {}),
            ('betting:fixtures', {}),
            ('betting:user_dashboard', {}), # Redirects to admin dashboard
            ('betting:admin_dashboard', {}),
            ('betting:manage_users', {}),
            ('betting:manage_fixtures', {}),
            ('betting:withdraw_request_list', {}),
            ('betting:manage_betting_periods', {}),
            ('betting:manage_agent_payouts', {}),
            # URLs with args
            ('betting:edit_user', {'user_id': self.user.id}),
            ('betting:edit_fixture', {'fixture_id': self.fixture.id}),
            ('betting:edit_betting_period', {'period_id': self.period.id}),
            ('betting:approve_reject_withdrawal', {'withdrawal_id': self.withdrawal.id}),
        ]
        
        for url_name, args in urls_to_test:
            try:
                url = reverse(url_name, kwargs=args)
                response = self.client.get(url)
                
                # Check for 200 or 302 (redirect)
                if response.status_code not in [200, 302]:
                    print(f"Error accessing {url_name}: Status {response.status_code}")
                    # If it's a 500, print the content to debug
                    if response.status_code == 500:
                        print(response.content.decode())
                
                self.assertIn(response.status_code, [200, 302], f"Failed to load {url_name} (Status: {response.status_code})")
            except Exception as e:
                self.fail(f"Exception accessing {url_name}: {e}")

    def test_agent_pages(self):
        """Test Agent specific pages."""
        self.client.login(email='agent@test.com', password=self.password)
        
        urls = [
            ('betting:agent_dashboard', {}),
            ('betting:agent_wallet_report', {}),
            ('betting:agent_sales_winnings_report', {}),
            ('betting:agent_commission_report', {}),
        ]
        
        for url_name, args in urls:
            url = reverse(url_name, kwargs=args)
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200, f"Agent failed to access {url_name}")

    def test_public_pages(self):
        """Test public pages as anonymous user."""
        self.client.logout()
        urls = [
            'betting:frontpage',
            'betting:login',
            'betting:register',
            'betting:fixtures',
        ]
        
        for url_name in urls:
            url = reverse(url_name)
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200, f"Public page {url_name} failed")

