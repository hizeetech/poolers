from django.test import TestCase, Client
from django.urls import reverse
from django.contrib.sessions.models import Session
from betting.models import (
    User,
    Wallet,
    Fixture,
    BettingPeriod,
    BetTicket,
    UserWithdrawal,
    Transaction,
    ActivityLog,
    RetailManagerSuperAgentMapping,
    RetailManagerAgentMapping,
)
from django.utils import timezone
import datetime
from decimal import Decimal

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

    def test_retail_manager_hierarchy_limits_to_mapped_super_agents(self):
        from betting.views import get_retail_manager_super_agents, get_retail_manager_agents, get_retail_network_users_qs

        password = "pass12345"
        retail_manager = User.objects.create_user(
            email="rm@test.com",
            password=password,
            user_type="retail_manager",
            first_name="Retail",
            last_name="Manager",
        )

        master = User.objects.create_user(email="ma@test.com", password=password, user_type="master_agent")
        sa_mapped = User.objects.create_user(
            email="sa1@test.com", password=password, user_type="super_agent", master_agent=master
        )
        sa_unmapped = User.objects.create_user(
            email="sa2@test.com", password=password, user_type="super_agent", master_agent=master
        )

        agent_under_mapped = User.objects.create_user(
            email="a1@test.com",
            password=password,
            user_type="agent",
            first_name="Agent",
            last_name="Mapped",
            master_agent=master,
            super_agent=sa_mapped,
        )
        User.objects.create_user(
            email="a2@test.com", password=password, user_type="agent", master_agent=master, super_agent=sa_unmapped
        )

        RetailManagerSuperAgentMapping.objects.create(retail_manager=retail_manager, super_agent=sa_mapped)

        sas = get_retail_manager_super_agents(retail_manager)
        self.assertEqual(set(sas.values_list("id", flat=True)), {sa_mapped.id})

        agents = get_retail_manager_agents(retail_manager, super_agents_qs=sas)
        self.assertEqual(set(agents.values_list("id", flat=True)), {agent_under_mapped.id})

        network = get_retail_network_users_qs(retail_manager)
        self.assertIn(sa_mapped.id, set(network.values_list("id", flat=True)))
        self.assertNotIn(sa_unmapped.id, set(network.values_list("id", flat=True)))

        self.client.force_login(retail_manager)
        response = self.client.get(reverse("betting:retail_dashboard"), {"tab": "hierarchy"})
        self.assertContains(response, "<td>Agent Mapped</td>", html=True)

    def test_retail_dashboard_uses_usernames_in_bets_and_finance_tabs(self):
        password = "pass12345"
        retail_manager = User.objects.create_user(
            email="rm2@test.com",
            password=password,
            user_type="retail_manager",
            first_name="Retail",
            last_name="Manager",
            username="retailmanager2",
        )
        agent = User.objects.create_user(
            email="agent2@test.com",
            password=password,
            user_type="agent",
            first_name="Agent",
            last_name="Two",
            other_name="Mapped",
            username="agent_two",
        )
        player = User.objects.create_user(
            email="player2@test.com",
            password=password,
            user_type="player",
            username="player_two",
            agent=agent,
        )
        Wallet.objects.create(user=retail_manager, balance=Decimal("0.00"))
        Wallet.objects.create(user=agent, balance=Decimal("0.00"))
        Wallet.objects.create(user=player, balance=Decimal("0.00"))
        RetailManagerAgentMapping.objects.create(retail_manager=retail_manager, agent=agent)

        BetTicket.objects.create(user=player, stake_amount=Decimal("100.00"), bet_type="single")
        Transaction.objects.create(
            user=player,
            transaction_type="deposit",
            amount=Decimal("250.00"),
            is_successful=True,
            status="completed",
            description="Test deposit",
        )
        UserWithdrawal.objects.create(
            user=player,
            amount=Decimal("50.00"),
            bank_name="Test Bank",
            account_number="1234567890",
            account_name="Player Two",
            status="completed",
        )

        self.client.force_login(retail_manager)

        bets_response = self.client.get(reverse("betting:retail_dashboard"), {"tab": "bets"})
        self.assertEqual(bets_response.status_code, 200)
        self.assertContains(bets_response, f'<option value="{agent.id}">agent_two</option>', html=True)

        finance_response = self.client.get(reverse("betting:retail_dashboard"), {"tab": "finance"})
        self.assertEqual(finance_response.status_code, 200)
        self.assertContains(finance_response, "<td class=\"text-muted small\">player_two</td>", html=True)

    def test_crm_and_finance_dashboards_use_usernames_in_replica_tabs(self):
        password = "pass12345"
        crm_user = User.objects.create_user(
            email="crm2@test.com",
            password=password,
            user_type="crm",
            crm_role="viewer",
            username="crmviewer2",
        )
        finance_user = User.objects.create_user(
            email="finance2@test.com",
            password=password,
            user_type="finance",
            finance_role="manager",
            username="financemgr2",
        )
        agent = User.objects.create_user(
            email="agent3@test.com",
            password=password,
            user_type="agent",
            first_name="Agent",
            last_name="Three",
            other_name="Mapped",
            username="agent_three",
        )
        player = User.objects.create_user(
            email="player3@test.com",
            password=password,
            user_type="player",
            username="player_three",
            agent=agent,
        )
        Wallet.objects.create(user=crm_user, balance=Decimal("0.00"))
        Wallet.objects.create(user=finance_user, balance=Decimal("0.00"))
        Wallet.objects.create(user=agent, balance=Decimal("0.00"))
        Wallet.objects.create(user=player, balance=Decimal("0.00"))

        BetTicket.objects.create(user=player, stake_amount=Decimal("100.00"), bet_type="single")
        Transaction.objects.create(
            user=player,
            transaction_type="deposit",
            amount=Decimal("250.00"),
            is_successful=True,
            status="completed",
            description="Test deposit",
        )
        UserWithdrawal.objects.create(
            user=player,
            amount=Decimal("50.00"),
            bank_name="Test Bank",
            account_number="1234567890",
            account_name="Player Three",
            status="pending",
        )

        self.client.force_login(crm_user)
        crm_response = self.client.get(reverse("betting:crm_dashboard"), {"tab": "bets"})
        self.assertEqual(crm_response.status_code, 200)
        self.assertContains(crm_response, f'<option value="{agent.id}">agent_three</option>', html=True)

        self.client.force_login(finance_user)
        finance_bets_response = self.client.get(reverse("betting:finance_dashboard"), {"tab": "bets"})
        self.assertEqual(finance_bets_response.status_code, 200)
        self.assertContains(finance_bets_response, f'<option value="{agent.id}">agent_three</option>', html=True)

        finance_transactions_response = self.client.get(reverse("betting:finance_dashboard"), {"tab": "transactions"})
        self.assertEqual(finance_transactions_response.status_code, 200)
        self.assertContains(finance_transactions_response, "<td class=\"text-muted small\">player_three</td>", html=True)

        finance_withdrawals_response = self.client.get(reverse("betting:finance_dashboard"), {"tab": "withdrawals"})
        self.assertEqual(finance_withdrawals_response.status_code, 200)
        self.assertContains(finance_withdrawals_response, "<td class=\"text-muted small\">player_three</td>", html=True)

    def test_password_change_logs_user_out_from_all_active_sessions(self):
        client_one = Client()
        client_two = Client()
        self.assertTrue(client_one.login(email='user@test.com', password=self.password))
        self.assertTrue(client_two.login(email='user@test.com', password=self.password))

        response = client_one.post(reverse('betting:change_password'), {
            'old_password': self.password,
            'new_password1': 'NewSecurePass123!',
            'new_password2': 'NewSecurePass123!',
        }, follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(any(
            session.get_decoded().get('_auth_user_id') == str(self.user.id)
            for session in Session.objects.all()
        ))
        protected = client_two.get(reverse('betting:user_dashboard'))
        self.assertEqual(protected.status_code, 302)
        self.assertIn(reverse('betting:login'), protected.url)

    def test_cashier_void_request_auto_voids_and_refunds(self):
        from void_requests.services import create_void_request, process_due_void_requests

        cashier = User.objects.create_user(
            email="cashier@test.com",
            password=self.password,
            user_type="cashier",
            agent=self.agent,
        )
        Wallet.objects.create(user=cashier, balance=Decimal("0.00"))
        ticket = BetTicket.objects.create(user=cashier, stake_amount=Decimal("100.00"), bet_type="single")

        vr = create_void_request(ticket=ticket, cashier=cashier, reason="")
        vr.auto_void_at = timezone.now() - datetime.timedelta(minutes=5)
        vr.save(update_fields=["auto_void_at"])

        processed = process_due_void_requests(limit=10)
        self.assertEqual(processed, 1)

        ticket.refresh_from_db()
        vr.refresh_from_db()
        self.assertEqual(ticket.status, "deleted")
        self.assertEqual(vr.status, "auto_voided")
        self.assertTrue(vr.is_processed)

        wallet = Wallet.objects.get(user=cashier)
        self.assertEqual(wallet.balance, Decimal("100.00"))
