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
    RetailManagerDashboardNote,
    AgentTransferLog,
    AccountUnlockAppeal,
    AccountLockAuditLog,
    CustomerComplaint,
    CustomerComplaintNote,
)
from django.utils import timezone
from django.db.models import Q
import datetime
from decimal import Decimal
from notifications.models import Notification

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

    def test_retail_export_supports_new_dashboard_datasets(self):
        password = "pass12345"
        retail_manager = User.objects.create_user(
            email="rm-export@test.com",
            password=password,
            user_type="retail_manager",
            username="rm_export",
        )
        master = User.objects.create_user(
            email="ma-export@test.com",
            password=password,
            user_type="master_agent",
            username="ma_export",
        )
        super_agent = User.objects.create_user(
            email="sa-export@test.com",
            password=password,
            user_type="super_agent",
            username="sa_export",
            master_agent=master,
        )
        agent = User.objects.create_user(
            email="agent-export@test.com",
            password=password,
            user_type="agent",
            username="agent_export",
            master_agent=master,
            super_agent=super_agent,
        )
        player = User.objects.create_user(
            email="player-export@test.com",
            password=password,
            user_type="player",
            username="player_export",
            agent=agent,
            super_agent=super_agent,
            master_agent=master,
        )
        Wallet.objects.create(user=retail_manager, balance=Decimal("0.00"))
        Wallet.objects.create(user=super_agent, balance=Decimal("0.00"))
        Wallet.objects.create(user=agent, balance=Decimal("0.00"))
        Wallet.objects.create(user=player, balance=Decimal("250.00"))
        RetailManagerSuperAgentMapping.objects.create(retail_manager=retail_manager, super_agent=super_agent)

        old_login = timezone.now() - datetime.timedelta(days=10)
        User.objects.filter(id=agent.id).update(last_login=old_login)
        agent.refresh_from_db()

        CustomerComplaint.objects.create(
            complaint_type="wallet",
            user=player,
            subject="Wallet help needed",
            description="Player reported a wallet issue.",
            status="open",
            priority="high",
            created_by=retail_manager,
        )

        BetTicket.objects.create(
            user=player,
            stake_amount=Decimal("150.00"),
            total_odd=Decimal("2.50"),
            potential_winning=Decimal("375.00"),
            max_winning=Decimal("375.00"),
            status="pending",
        )

        self.client.force_login(retail_manager)

        dormant_response = self.client.get(
            reverse("betting:retail_export"),
            {"dataset": "dormant_accounts", "format": "csv", "dormant_bucket": "login_7"},
        )
        self.assertEqual(dormant_response.status_code, 200)
        self.assertIn("agent_export", dormant_response.content.decode())

        complaints_response = self.client.get(
            reverse("betting:retail_export"),
            {"dataset": "complaints", "format": "csv"},
        )
        self.assertEqual(complaints_response.status_code, 200)
        self.assertIn("Wallet help needed", complaints_response.content.decode())

        performance_response = self.client.get(
            reverse("betting:retail_export"),
            {"dataset": "agent_performance", "format": "csv", "performance_entity": "agent"},
        )
        self.assertEqual(performance_response.status_code, 200)
        self.assertIn("agent_export", performance_response.content.decode())

    def test_retail_complaints_export_respects_scope_and_filters(self):
        password = "pass12345"
        retail_manager = User.objects.create_user(
            email="rm-complaint-filter@test.com",
            password=password,
            user_type="retail_manager",
            username="rm_complaint_filter",
        )
        mapped_super_agent = User.objects.create_user(
            email="mapped-sa-complaint@test.com",
            password=password,
            user_type="super_agent",
            username="mapped_sa_complaint",
        )
        unmapped_super_agent = User.objects.create_user(
            email="unmapped-sa-complaint@test.com",
            password=password,
            user_type="super_agent",
            username="unmapped_sa_complaint",
        )
        mapped_agent = User.objects.create_user(
            email="mapped-agent-complaint@test.com",
            password=password,
            user_type="agent",
            username="mapped_agent_complaint",
            super_agent=mapped_super_agent,
        )
        unmapped_agent = User.objects.create_user(
            email="unmapped-agent-complaint@test.com",
            password=password,
            user_type="agent",
            username="unmapped_agent_complaint",
            super_agent=unmapped_super_agent,
        )
        mapped_player = User.objects.create_user(
            email="mapped-player-complaint@test.com",
            password=password,
            user_type="player",
            username="mapped_player_complaint",
            agent=mapped_agent,
            super_agent=mapped_super_agent,
            first_name="RetailComplaintTarget",
        )
        unmapped_player = User.objects.create_user(
            email="unmapped-player-complaint@test.com",
            password=password,
            user_type="player",
            username="unmapped_player_complaint",
            agent=unmapped_agent,
            super_agent=unmapped_super_agent,
            first_name="RetailComplaintOther",
        )
        for user in [retail_manager, mapped_super_agent, unmapped_super_agent, mapped_agent, unmapped_agent, mapped_player, unmapped_player]:
            Wallet.objects.create(user=user, balance=Decimal("0.00"))
        RetailManagerSuperAgentMapping.objects.create(retail_manager=retail_manager, super_agent=mapped_super_agent)

        CustomerComplaint.objects.create(
            complaint_type="deposit",
            user=mapped_player,
            subject="Retail Complaint Export Target",
            description="RetailComplaintTarget description",
            status="escalated",
            priority="critical",
            created_by=retail_manager,
        )
        CustomerComplaint.objects.create(
            complaint_type="deposit",
            user=unmapped_player,
            subject="Retail Complaint Out Of Scope",
            description="RetailComplaintTarget but unmapped",
            status="escalated",
            priority="critical",
            created_by=retail_manager,
        )
        CustomerComplaint.objects.create(
            complaint_type="wallet",
            user=mapped_player,
            subject="Retail Complaint Wrong Filter",
            description="Different filter",
            status="open",
            priority="low",
            created_by=retail_manager,
        )

        self.client.force_login(retail_manager)
        response = self.client.get(
            reverse("betting:retail_export"),
            {
                "dataset": "complaints",
                "format": "csv",
                "complaint_q": "RetailComplaintTarget",
                "complaint_type": "deposit",
                "complaint_status": "escalated",
                "complaint_priority": "critical",
            },
        )

        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn("Retail Complaint Export Target", body)
        self.assertNotIn("Retail Complaint Out Of Scope", body)
        self.assertNotIn("Retail Complaint Wrong Filter", body)

    def test_retail_dormant_and_performance_exports_respect_network_scope(self):
        password = "pass12345"
        retail_manager = User.objects.create_user(
            email="rm-network-export@test.com",
            password=password,
            user_type="retail_manager",
            username="rm_network_export",
        )
        mapped_super_agent = User.objects.create_user(
            email="mapped-sa-network@test.com",
            password=password,
            user_type="super_agent",
            username="mapped_sa_network",
            first_name="RetailPerformanceTarget",
        )
        unmapped_super_agent = User.objects.create_user(
            email="unmapped-sa-network@test.com",
            password=password,
            user_type="super_agent",
            username="unmapped_sa_network",
            first_name="RetailPerformanceOther",
        )
        mapped_agent = User.objects.create_user(
            email="mapped-agent-network@test.com",
            password=password,
            user_type="agent",
            username="mapped_agent_network",
            super_agent=mapped_super_agent,
        )
        unmapped_agent = User.objects.create_user(
            email="unmapped-agent-network@test.com",
            password=password,
            user_type="agent",
            username="unmapped_agent_network",
            super_agent=unmapped_super_agent,
        )
        mapped_player = User.objects.create_user(
            email="mapped-player-network@test.com",
            password=password,
            user_type="player",
            username="mapped_player_network",
            agent=mapped_agent,
            super_agent=mapped_super_agent,
        )
        unmapped_player = User.objects.create_user(
            email="unmapped-player-network@test.com",
            password=password,
            user_type="player",
            username="unmapped_player_network",
            agent=unmapped_agent,
            super_agent=unmapped_super_agent,
        )
        for user in [retail_manager, mapped_super_agent, unmapped_super_agent, mapped_agent, unmapped_agent, mapped_player, unmapped_player]:
            Wallet.objects.create(user=user, balance=Decimal("0.00"))
        RetailManagerSuperAgentMapping.objects.create(retail_manager=retail_manager, super_agent=mapped_super_agent)

        stale_login = timezone.now() - datetime.timedelta(days=10)
        User.objects.filter(id__in=[mapped_agent.id, unmapped_agent.id]).update(last_login=stale_login)

        BetTicket.objects.create(
            user=mapped_player,
            stake_amount=Decimal("125.00"),
            total_odd=Decimal("2.00"),
            potential_winning=Decimal("250.00"),
            max_winning=Decimal("250.00"),
            status="won",
        )
        BetTicket.objects.create(
            user=unmapped_player,
            stake_amount=Decimal("80.00"),
            total_odd=Decimal("1.50"),
            potential_winning=Decimal("120.00"),
            max_winning=Decimal("120.00"),
            status="lost",
        )

        self.client.force_login(retail_manager)

        dormant_response = self.client.get(
            reverse("betting:retail_export"),
            {
                "dataset": "dormant_accounts",
                "format": "csv",
                "dormant_bucket": "login_7",
            },
        )
        self.assertEqual(dormant_response.status_code, 200)
        dormant_body = dormant_response.content.decode()
        self.assertIn("mapped_agent_network", dormant_body)
        self.assertNotIn("unmapped_agent_network", dormant_body)

        performance_response = self.client.get(
            reverse("betting:retail_export"),
            {
                "dataset": "agent_performance",
                "format": "csv",
                "performance_entity": "super_agent",
                "performance_q": "RetailPerformanceTarget",
            },
        )
        self.assertEqual(performance_response.status_code, 200)
        performance_body = performance_response.content.decode()
        self.assertIn("mapped_sa_network", performance_body)
        self.assertNotIn("unmapped_sa_network", performance_body)

    def test_retail_agent_performance_dashboard_renders(self):
        password = "pass12345"
        retail_manager = User.objects.create_user(
            email="rm-performance-dashboard@test.com",
            password=password,
            user_type="retail_manager",
            username="rm_performance_dashboard",
        )
        mapped_super_agent = User.objects.create_user(
            email="mapped-sa-performance-dashboard@test.com",
            password=password,
            user_type="super_agent",
            username="mapped_sa_performance_dashboard",
            first_name="Performance Dashboard Target",
        )
        mapped_agent = User.objects.create_user(
            email="mapped-agent-performance-dashboard@test.com",
            password=password,
            user_type="agent",
            username="mapped_agent_performance_dashboard",
            super_agent=mapped_super_agent,
        )
        mapped_player = User.objects.create_user(
            email="mapped-player-performance-dashboard@test.com",
            password=password,
            user_type="player",
            username="mapped_player_performance_dashboard",
            agent=mapped_agent,
            super_agent=mapped_super_agent,
        )
        for user in [retail_manager, mapped_super_agent, mapped_agent, mapped_player]:
            Wallet.objects.create(user=user, balance=Decimal("0.00"))
        RetailManagerSuperAgentMapping.objects.create(retail_manager=retail_manager, super_agent=mapped_super_agent)
        BetTicket.objects.create(
            user=mapped_player,
            stake_amount=Decimal("50.00"),
            total_odd=Decimal("2.00"),
            potential_winning=Decimal("100.00"),
            max_winning=Decimal("100.00"),
            status="won",
        )

        self.client.force_login(retail_manager)
        response = self.client.get(
            reverse("betting:retail_dashboard"),
            {
                "tab": "agent_performance",
                "performance_entity": "super_agent",
                "performance_q": "Performance Dashboard Target",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Performance Dashboard Target")

    def test_retail_dormant_dashboard_and_export_apply_search_filter(self):
        password = "pass12345"
        retail_manager = User.objects.create_user(
            email="rm-dormant-search@test.com",
            password=password,
            user_type="retail_manager",
            username="rm_dormant_search",
        )
        mapped_super_agent = User.objects.create_user(
            email="mapped-sa-dormant-search@test.com",
            password=password,
            user_type="super_agent",
            username="mapped_sa_dormant_search",
        )
        matching_agent = User.objects.create_user(
            email="dormant-match-agent@test.com",
            password=password,
            user_type="agent",
            username="dormant_match_agent",
            first_name="DormantSearchMatch",
            super_agent=mapped_super_agent,
        )
        other_agent = User.objects.create_user(
            email="dormant-other-agent@test.com",
            password=password,
            user_type="agent",
            username="dormant_other_agent",
            first_name="DormantSearchOther",
            super_agent=mapped_super_agent,
        )
        other_cashier = User.objects.create_user(
            email="dormant-other-cashier@test.com",
            password=password,
            user_type="cashier",
            username="dormant_other_cashier",
            agent=other_agent,
            super_agent=mapped_super_agent,
        )
        for user in [retail_manager, mapped_super_agent, matching_agent, other_agent, other_cashier]:
            Wallet.objects.create(user=user, balance=Decimal("0.00"))
        RetailManagerSuperAgentMapping.objects.create(retail_manager=retail_manager, super_agent=mapped_super_agent)

        stale_login = timezone.now() - datetime.timedelta(days=10)
        recent_login = timezone.now() - datetime.timedelta(days=1)
        User.objects.filter(id__in=[matching_agent.id, other_agent.id]).update(last_login=stale_login)
        User.objects.filter(id=other_cashier.id).update(last_login=recent_login)

        self.client.force_login(retail_manager)

        dashboard_response = self.client.get(
            reverse("betting:retail_dashboard"),
            {
                "tab": "dormant_accounts",
                "dormant_bucket": "login_7",
                "dormant_q": "DormantSearchMatch",
            },
        )
        self.assertEqual(dashboard_response.status_code, 200)
        dashboard_body = dashboard_response.content.decode()
        self.assertIn("dormant_match_agent", dashboard_body)
        self.assertNotIn("dormant_other_agent", dashboard_body)
        self.assertIn('name="dormant_q" value="DormantSearchMatch"', dashboard_body)

        export_response = self.client.get(
            reverse("betting:retail_export"),
            {
                "dataset": "dormant_accounts",
                "format": "csv",
                "dormant_bucket": "login_7",
                "dormant_q": "DormantSearchMatch",
            },
        )
        self.assertEqual(export_response.status_code, 200)
        export_body = export_response.content.decode()
        self.assertIn("dormant_match_agent", export_body)
        self.assertNotIn("dormant_other_agent", export_body)

        broad_export_response = self.client.get(
            reverse("betting:retail_export"),
            {
                "dataset": "dormant_accounts",
                "format": "csv",
                "dormant_bucket": "login_7",
            },
        )
        self.assertEqual(broad_export_response.status_code, 200)
        broad_export_body = broad_export_response.content.decode()
        self.assertIn("dormant_match_agent", broad_export_body)
        self.assertNotIn("dormant_other_agent", broad_export_body)

    def test_retail_complaint_update_persists_note_for_mapped_user(self):
        password = "pass12345"
        retail_manager = User.objects.create_user(
            email="rm-complaint-update@test.com",
            password=password,
            user_type="retail_manager",
            username="rm_complaint_update",
        )
        mapped_super_agent = User.objects.create_user(
            email="mapped-sa-update@test.com",
            password=password,
            user_type="super_agent",
            username="mapped_sa_update",
        )
        mapped_agent = User.objects.create_user(
            email="mapped-agent-update@test.com",
            password=password,
            user_type="agent",
            username="mapped_agent_update",
            super_agent=mapped_super_agent,
        )
        mapped_player = User.objects.create_user(
            email="mapped-player-update@test.com",
            password=password,
            user_type="player",
            username="mapped_player_update",
            agent=mapped_agent,
            super_agent=mapped_super_agent,
        )
        for user in [retail_manager, mapped_super_agent, mapped_agent, mapped_player]:
            Wallet.objects.create(user=user, balance=Decimal("0.00"))
        RetailManagerSuperAgentMapping.objects.create(retail_manager=retail_manager, super_agent=mapped_super_agent)

        complaint = CustomerComplaint.objects.create(
            complaint_type="wallet",
            user=mapped_player,
            subject="Mapped complaint",
            description="Mapped user complaint",
            status="open",
            priority="low",
            created_by=retail_manager,
        )

        self.client.force_login(retail_manager)
        response = self.client.post(
            reverse("betting:retail_dashboard"),
            {
                "tab": "complaints",
                "update_complaint": "1",
                "complaint_id": str(complaint.id),
                "status": "resolved",
                "priority": "high",
                "admin_note": "Resolved with player at shop.",
            },
        )

        self.assertEqual(response.status_code, 302)
        complaint.refresh_from_db()
        self.assertEqual(complaint.status, "resolved")
        self.assertEqual(complaint.priority, "high")
        self.assertIsNotNone(complaint.resolved_at)
        self.assertTrue(
            CustomerComplaintNote.objects.filter(
                complaint=complaint,
                author=retail_manager,
                note="Resolved with player at shop.",
                is_internal=True,
            ).exists()
        )

    def test_retail_complaint_update_rejects_unmapped_user_complaint(self):
        password = "pass12345"
        retail_manager = User.objects.create_user(
            email="rm-complaint-scope@test.com",
            password=password,
            user_type="retail_manager",
            username="rm_complaint_scope",
        )
        mapped_super_agent = User.objects.create_user(
            email="mapped-sa-scope@test.com",
            password=password,
            user_type="super_agent",
            username="mapped_sa_scope",
        )
        unmapped_super_agent = User.objects.create_user(
            email="unmapped-sa-scope@test.com",
            password=password,
            user_type="super_agent",
            username="unmapped_sa_scope",
        )
        unmapped_agent = User.objects.create_user(
            email="unmapped-agent-scope@test.com",
            password=password,
            user_type="agent",
            username="unmapped_agent_scope",
            super_agent=unmapped_super_agent,
        )
        unmapped_player = User.objects.create_user(
            email="unmapped-player-scope@test.com",
            password=password,
            user_type="player",
            username="unmapped_player_scope",
            agent=unmapped_agent,
            super_agent=unmapped_super_agent,
        )
        for user in [retail_manager, mapped_super_agent, unmapped_super_agent, unmapped_agent, unmapped_player]:
            Wallet.objects.create(user=user, balance=Decimal("0.00"))
        RetailManagerSuperAgentMapping.objects.create(retail_manager=retail_manager, super_agent=mapped_super_agent)

        complaint = CustomerComplaint.objects.create(
            complaint_type="deposit",
            user=unmapped_player,
            subject="Unmapped complaint",
            description="Unmapped user complaint",
            status="open",
            priority="medium",
            created_by=retail_manager,
        )

        self.client.force_login(retail_manager)
        response = self.client.post(
            reverse("betting:retail_dashboard"),
            {
                "tab": "complaints",
                "update_complaint": "1",
                "complaint_id": str(complaint.id),
                "status": "resolved",
                "priority": "critical",
                "admin_note": "Should not be allowed.",
            },
        )

        self.assertEqual(response.status_code, 404)
        complaint.refresh_from_db()
        self.assertEqual(complaint.status, "open")
        self.assertEqual(complaint.priority, "medium")
        self.assertFalse(CustomerComplaintNote.objects.filter(complaint=complaint, note="Should not be allowed.").exists())

    def test_agent_remapping_returns_403_for_unauthorized_roles(self):
        password = "pass12345"
        agent_user = User.objects.create_user(
            email="unauth-agent@test.com",
            password=password,
            user_type="agent",
            username="unauth_agent",
        )
        Wallet.objects.create(user=agent_user, balance=Decimal("0.00"))

        self.assertTrue(self.client.login(email="unauth-agent@test.com", password=password))
        response = self.client.get(reverse("betting:agent_remapping"))
        self.assertEqual(response.status_code, 403)
        self.assertIn("Permission Denied", response.content.decode())

    def test_crm_can_remap_agent_and_export_history(self):
        password = "pass12345"
        crm_user = User.objects.create_user(
            email="crm-remap@test.com",
            password=password,
            user_type="crm",
            username="crm_remap",
            crm_role="viewer",
        )
        master_old = User.objects.create_user(
            email="master-old@test.com",
            password=password,
            user_type="master_agent",
            username="master_old",
        )
        master_new = User.objects.create_user(
            email="master-new@test.com",
            password=password,
            user_type="master_agent",
            username="master_new",
        )
        old_super_agent = User.objects.create_user(
            email="old-super@test.com",
            password=password,
            user_type="super_agent",
            username="old_super",
            master_agent=master_old,
        )
        new_super_agent = User.objects.create_user(
            email="new-super@test.com",
            password=password,
            user_type="super_agent",
            username="new_super",
            master_agent=master_new,
        )
        transferred_agent = User.objects.create_user(
            email="transfer-agent@test.com",
            password=password,
            user_type="agent",
            username="transfer_agent",
            first_name="Transfer",
            last_name="Agent",
            super_agent=old_super_agent,
            master_agent=master_old,
        )
        cashier = User.objects.create_user(
            email="transfer-cashier@test.com",
            password=password,
            user_type="cashier",
            username="transfer_cashier",
            agent=transferred_agent,
        )
        for user, balance in [
            (crm_user, Decimal("0.00")),
            (master_old, Decimal("0.00")),
            (master_new, Decimal("0.00")),
            (old_super_agent, Decimal("0.00")),
            (new_super_agent, Decimal("0.00")),
            (transferred_agent, Decimal("100.00")),
            (cashier, Decimal("25.00")),
        ]:
            Wallet.objects.create(user=user, balance=balance)

        self.assertTrue(self.client.login(email="crm-remap@test.com", password=password))
        response = self.client.post(
            reverse("betting:agent_remapping"),
            {
                "subtab": "remap",
                "action": "transfer_agents",
                "current_super_agent": str(old_super_agent.id),
                "destination_super_agent": str(new_super_agent.id),
                "agents": [str(transferred_agent.id)],
                "remarks": "Operational realignment",
            },
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "1 agent(s) transferred successfully.")

        transferred_agent.refresh_from_db()
        cashier.refresh_from_db()
        self.assertEqual(transferred_agent.super_agent_id, new_super_agent.id)
        self.assertEqual(transferred_agent.master_agent_id, master_new.id)
        self.assertEqual(cashier.agent_id, transferred_agent.id)

        old_downline_ids = set(User.objects.filter(Q(super_agent=old_super_agent) | Q(agent__super_agent=old_super_agent)).values_list("id", flat=True))
        new_downline_ids = set(User.objects.filter(Q(super_agent=new_super_agent) | Q(agent__super_agent=new_super_agent)).values_list("id", flat=True))
        self.assertNotIn(transferred_agent.id, old_downline_ids)
        self.assertIn(transferred_agent.id, new_downline_ids)
        self.assertIn(cashier.id, new_downline_ids)

        log = AgentTransferLog.objects.get(agent=transferred_agent)
        self.assertEqual(log.old_super_agent_id, old_super_agent.id)
        self.assertEqual(log.new_super_agent_id, new_super_agent.id)
        self.assertEqual(log.transferred_by_id, crm_user.id)
        self.assertEqual(log.remarks, "Operational realignment")

        self.assertTrue(Notification.objects.filter(recipient=old_super_agent, title="Agent Removed From Downline").exists())
        self.assertTrue(Notification.objects.filter(recipient=new_super_agent, title="New Agent Assigned").exists())
        self.assertTrue(Notification.objects.filter(recipient=transferred_agent, title="Super Agent Reassigned").exists())

        export_response = self.client.get(reverse("betting:agent_remapping_export"), {"format": "xlsx"})
        self.assertEqual(export_response.status_code, 200)
        self.assertEqual(
            export_response["Content-Type"],
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    def test_retail_dashboard_overview_downline_section_shows_only_mapped_super_agent_branches(self):
        password = "pass12345"
        retail_manager = User.objects.create_user(
            email="rm-downline@test.com",
            password=password,
            user_type="retail_manager",
            username="rm_downline",
        )
        master = User.objects.create_user(
            email="ma-downline@test.com",
            password=password,
            user_type="master_agent",
            username="ma_downline",
        )
        sa_mapped = User.objects.create_user(
            email="sa-mapped@test.com",
            password=password,
            user_type="super_agent",
            username="sa_mapped_user",
            master_agent=master,
        )
        sa_unmapped = User.objects.create_user(
            email="sa-unmapped@test.com",
            password=password,
            user_type="super_agent",
            username="sa_unmapped_user",
            master_agent=master,
        )
        mapped_agent = User.objects.create_user(
            email="agent-mapped@test.com",
            password=password,
            user_type="agent",
            username="mapped_agent_user",
            first_name="Mapped",
            last_name="Agent",
            master_agent=master,
            super_agent=sa_mapped,
        )
        unmapped_agent = User.objects.create_user(
            email="agent-unmapped@test.com",
            password=password,
            user_type="agent",
            username="unmapped_agent_user",
            master_agent=master,
            super_agent=sa_unmapped,
        )
        User.objects.create_user(
            email="cashier-mapped@test.com",
            password=password,
            user_type="cashier",
            username="mapped_cashier_user",
            agent=mapped_agent,
        )
        User.objects.create_user(
            email="cashier-unmapped@test.com",
            password=password,
            user_type="cashier",
            username="unmapped_cashier_user",
            agent=unmapped_agent,
        )
        Wallet.objects.create(user=retail_manager, balance=Decimal("0.00"))
        Wallet.objects.create(user=master, balance=Decimal("0.00"))
        Wallet.objects.create(user=sa_mapped, balance=Decimal("0.00"))
        Wallet.objects.create(user=sa_unmapped, balance=Decimal("0.00"))
        Wallet.objects.create(user=mapped_agent, balance=Decimal("100.00"))
        Wallet.objects.create(user=unmapped_agent, balance=Decimal("500.00"))
        Wallet.objects.create(user=User.objects.get(username="mapped_cashier_user"), balance=Decimal("45.00"))
        Wallet.objects.create(user=User.objects.get(username="unmapped_cashier_user"), balance=Decimal("55.00"))

        RetailManagerSuperAgentMapping.objects.create(retail_manager=retail_manager, super_agent=sa_mapped)

        self.client.force_login(retail_manager)
        response = self.client.get(reverse("betting:retail_dashboard"), {"tab": "overview"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Downline Users (Directly under you)")
        self.assertContains(response, "sa_mapped_user")
        self.assertContains(response, "mapped_agent_user")
        self.assertContains(response, "₦145.00")
        self.assertNotContains(response, "sa_unmapped_user")
        self.assertNotContains(response, "unmapped_agent_user")

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

    def test_retail_dashboard_note_tab_saves_and_reloads_note(self):
        password = "pass12345"
        retail_manager = User.objects.create_user(
            email="rm-note@test.com",
            password=password,
            user_type="retail_manager",
            username="retail_note_manager",
        )
        Wallet.objects.create(user=retail_manager, balance=Decimal("0.00"))
        self.client.force_login(retail_manager)

        get_response = self.client.get(reverse("betting:retail_dashboard"), {"tab": "note"})
        self.assertEqual(get_response.status_code, 200)
        self.assertContains(get_response, ">Note</a>", html=False)
        self.assertContains(get_response, "Reporting Note")

        note_html = "<p>Weekly retail note</p>"
        post_response = self.client.post(
            reverse("betting:retail_dashboard"),
            {
                "tab": "note",
                "save_note": "1",
                "content": note_html,
            },
            follow=True,
        )
        self.assertEqual(post_response.status_code, 200)
        self.assertContains(post_response, "Note saved successfully.")

        note = RetailManagerDashboardNote.objects.get(retail_manager=retail_manager)
        self.assertEqual(note.content, note_html)

        reload_response = self.client.get(reverse("betting:retail_dashboard"), {"tab": "note"})
        self.assertEqual(reload_response.status_code, 200)
        self.assertContains(reload_response, "Weekly retail note")

        overview_response = self.client.get(reverse("betting:retail_dashboard"), {"tab": "overview"})
        self.assertEqual(overview_response.status_code, 200)
        self.assertContains(overview_response, "Latest Saved Note")
        self.assertContains(overview_response, "Weekly retail note")

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

    def test_retail_and_admin_overview_top_agent_cards_use_usernames(self):
        password = "pass12345"
        retail_manager = User.objects.create_user(
            email="rm-top@test.com",
            password=password,
            user_type="retail_manager",
            username="rm_top",
        )
        agent = User.objects.create_user(
            email="topagent@test.com",
            password=password,
            user_type="agent",
            username="top_agent_user",
        )
        player = User.objects.create_user(
            email="topplayer@test.com",
            password=password,
            user_type="player",
            username="top_player_user",
            agent=agent,
        )
        Wallet.objects.create(user=retail_manager, balance=Decimal("0.00"))
        Wallet.objects.create(user=agent, balance=Decimal("0.00"))
        Wallet.objects.create(user=player, balance=Decimal("0.00"))
        RetailManagerAgentMapping.objects.create(retail_manager=retail_manager, agent=agent)

        BetTicket.objects.create(
            user=player,
            stake_amount=Decimal("150.00"),
            bet_type="single",
            status="pending",
            total_odd=Decimal("2.00"),
            potential_winning=Decimal("300.00"),
            max_winning=Decimal("300.00"),
        )

        self.client.force_login(retail_manager)
        retail_response = self.client.get(reverse("betting:retail_dashboard"), {"tab": "overview"})
        self.assertEqual(retail_response.status_code, 200)
        self.assertContains(retail_response, "<td class=\"text-muted small\">top_agent_user</td>", html=True)

        self.client.force_login(self.admin)
        admin_response = self.client.get(reverse("betting:admin_dashboard"))
        self.assertEqual(admin_response.status_code, 200)
        self.assertContains(admin_response, "<td>top_agent_user</td>", html=True)

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
        from betting.models import SiteConfiguration

        cashier = User.objects.create_user(
            email="cashier@test.com",
            password=self.password,
            user_type="cashier",
            agent=self.agent,
        )
        Wallet.objects.create(user=cashier, balance=Decimal("0.00"))
        config = SiteConfiguration.load()
        config.enable_global_cashier_voiding = True
        config.save(update_fields=["enable_global_cashier_voiding"])
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

    def test_super_agent_locked_accounts_tab_is_scoped_to_his_downline(self):
        password = "pass12345"
        master = User.objects.create_user(
            email="scope-master@test.com",
            password=password,
            user_type="master_agent",
            username="scope_master",
        )
        super_agent = User.objects.create_user(
            email="scope-super@test.com",
            password=password,
            user_type="super_agent",
            username="scope_super",
            master_agent=master,
        )
        other_super_agent = User.objects.create_user(
            email="scope-super-other@test.com",
            password=password,
            user_type="super_agent",
            username="scope_super_other",
            master_agent=master,
        )
        owned_agent = User.objects.create_user(
            email="scope-agent@test.com",
            password=password,
            user_type="agent",
            username="scope_agent",
            super_agent=super_agent,
            master_agent=master,
        )
        owned_cashier = User.objects.create_user(
            email="scope-cashier@test.com",
            password=password,
            user_type="cashier",
            username="scope_cashier",
            agent=owned_agent,
        )
        outsider_agent = User.objects.create_user(
            email="scope-agent-other@test.com",
            password=password,
            user_type="agent",
            username="scope_agent_other",
            super_agent=other_super_agent,
            master_agent=master,
        )
        for user in [master, super_agent, other_super_agent, owned_agent, owned_cashier, outsider_agent]:
            Wallet.objects.get_or_create(user=user, defaults={"balance": Decimal("0.00")})

        now = timezone.now()
        User.objects.filter(id__in=[owned_agent.id, owned_cashier.id, outsider_agent.id]).update(
            is_locked=True,
            locked_at=now,
            lock_reason="Compliance Review",
        )
        AccountLockAuditLog.objects.create(locked_user=owned_agent, action="locked", lock_reason="Compliance Review")
        AccountLockAuditLog.objects.create(locked_user=owned_cashier, action="locked", lock_reason="Compliance Review")
        AccountLockAuditLog.objects.create(locked_user=outsider_agent, action="locked", lock_reason="Compliance Review")

        self.client.force_login(super_agent)
        response = self.client.get(reverse("betting:super_agent_dashboard"), {"locked_q": "scope_", "locked_user_type": ""})
        self.assertEqual(response.status_code, 200)
        locked_rows = list(response.context["locked_accounts_rows"])
        self.assertEqual({row.username for row in locked_rows}, {"scope_agent", "scope_cashier"})

    def test_retail_manager_can_submit_unlock_appeal_for_mapped_locked_account(self):
        password = "pass12345"
        retail_manager = User.objects.create_user(
            email="appeal-rm@test.com",
            password=password,
            user_type="retail_manager",
            username="appeal_rm",
        )
        master = User.objects.create_user(
            email="appeal-master@test.com",
            password=password,
            user_type="master_agent",
            username="appeal_master",
        )
        super_agent = User.objects.create_user(
            email="appeal-super@test.com",
            password=password,
            user_type="super_agent",
            username="appeal_super",
            master_agent=master,
        )
        mapped_agent = User.objects.create_user(
            email="appeal-agent@test.com",
            password=password,
            user_type="agent",
            username="appeal_agent",
            super_agent=super_agent,
            master_agent=master,
        )
        outsider_super = User.objects.create_user(
            email="appeal-super-outsider@test.com",
            password=password,
            user_type="super_agent",
            username="appeal_super_outsider",
            master_agent=master,
        )
        outsider_agent = User.objects.create_user(
            email="appeal-agent-outsider@test.com",
            password=password,
            user_type="agent",
            username="appeal_agent_outsider",
            super_agent=outsider_super,
            master_agent=master,
        )
        for user in [retail_manager, master, super_agent, mapped_agent, outsider_super, outsider_agent]:
            Wallet.objects.get_or_create(user=user, defaults={"balance": Decimal("0.00")})

        RetailManagerSuperAgentMapping.objects.create(retail_manager=retail_manager, super_agent=super_agent)
        now = timezone.now()
        User.objects.filter(id__in=[mapped_agent.id, outsider_agent.id]).update(is_locked=True, locked_at=now, lock_reason="KYC Issue")
        AccountLockAuditLog.objects.create(locked_user=mapped_agent, action="locked", lock_reason="KYC Issue")
        AccountLockAuditLog.objects.create(locked_user=outsider_agent, action="locked", lock_reason="KYC Issue")

        self.client.force_login(retail_manager)
        page = self.client.get(reverse("betting:retail_dashboard"), {"tab": "locked_accounts"})
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "appeal_agent")
        self.assertNotContains(page, "appeal_agent_outsider")

        submit_response = self.client.post(
            reverse("betting:submit_account_unlock_appeal", args=[mapped_agent.id]),
            {"appeal_reason": "KYC has now been completed."},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(submit_response.status_code, 200)
        self.assertTrue(submit_response.json()["ok"])
        appeal = AccountUnlockAppeal.objects.get(locked_user=mapped_agent)
        self.assertEqual(appeal.status, "pending")
        self.assertEqual(appeal.appealed_by_id, retail_manager.id)
        self.assertTrue(AccountLockAuditLog.objects.filter(locked_user=mapped_agent, action="appeal_submitted").exists())
        self.assertTrue(Notification.objects.filter(recipient=self.admin, title="New Account Unlock Appeal").exists())

    def test_crm_can_approve_appeal_and_unlock_account(self):
        password = "pass12345"
        crm_user = User.objects.create_user(
            email="appeal-crm@test.com",
            password=password,
            user_type="crm",
            username="appeal_crm",
            crm_role="viewer",
        )
        retail_manager = User.objects.create_user(
            email="appeal-review-rm@test.com",
            password=password,
            user_type="retail_manager",
            username="appeal_review_rm",
        )
        master = User.objects.create_user(
            email="appeal-review-master@test.com",
            password=password,
            user_type="master_agent",
            username="appeal_review_master",
        )
        super_agent = User.objects.create_user(
            email="appeal-review-super@test.com",
            password=password,
            user_type="super_agent",
            username="appeal_review_super",
            master_agent=master,
        )
        locked_agent = User.objects.create_user(
            email="appeal-review-agent@test.com",
            password=password,
            user_type="agent",
            username="appeal_review_agent",
            super_agent=super_agent,
            master_agent=master,
            is_locked=True,
            locked_at=timezone.now(),
            lock_reason="Suspicious Activity",
        )
        for user in [crm_user, retail_manager, master, super_agent, locked_agent]:
            Wallet.objects.get_or_create(user=user, defaults={"balance": Decimal("0.00")})
        RetailManagerSuperAgentMapping.objects.create(retail_manager=retail_manager, super_agent=super_agent)
        AccountLockAuditLog.objects.create(locked_user=locked_agent, action="locked", lock_reason="Suspicious Activity")
        appeal = AccountUnlockAppeal.objects.create(
            user=locked_agent,
            locked_user=locked_agent,
            appealed_by=retail_manager,
            appeal_reason="Please unlock this account.",
            status="pending",
        )

        self.client.force_login(crm_user)
        response = self.client.post(
            reverse("betting:account_appeals_review"),
            {
                "review_appeal": "1",
                "appeal_id": str(appeal.id),
                "action": "approve",
                "admin_comment": "Verified and approved.",
                "return_to": reverse("betting:account_appeals_review"),
            },
            follow=True,
        )
        self.assertEqual(response.status_code, 200)

        locked_agent.refresh_from_db()
        appeal.refresh_from_db()
        self.assertFalse(locked_agent.is_locked)
        self.assertEqual(locked_agent.failed_login_attempts, 0)
        self.assertEqual(appeal.status, "approved")
        self.assertEqual(appeal.reviewed_by_id, crm_user.id)
        self.assertTrue(AccountLockAuditLog.objects.filter(locked_user=locked_agent, action="appeal_approved").exists())
        self.assertTrue(AccountLockAuditLog.objects.filter(locked_user=locked_agent, action="unlocked").exists())
        self.assertTrue(Notification.objects.filter(recipient=locked_agent, title="Unlock Appeal Approved").exists())
        self.assertTrue(Notification.objects.filter(recipient=super_agent, title="Unlock Appeal Approved").exists())
        self.assertTrue(Notification.objects.filter(recipient=retail_manager, title="Unlock Appeal Approved").exists())

    def test_reject_requires_comment_and_exports_enforce_permissions(self):
        password = "pass12345"
        crm_user = User.objects.create_user(
            email="appeal-export-crm@test.com",
            password=password,
            user_type="crm",
            username="appeal_export_crm",
            crm_role="viewer",
        )
        super_agent = User.objects.create_user(
            email="appeal-export-super@test.com",
            password=password,
            user_type="super_agent",
            username="appeal_export_super",
        )
        locked_agent = User.objects.create_user(
            email="appeal-export-agent@test.com",
            password=password,
            user_type="agent",
            username="appeal_export_agent",
            super_agent=super_agent,
            is_locked=True,
            locked_at=timezone.now(),
            lock_reason="Manual Administrative Lock",
        )
        for user in [crm_user, super_agent, locked_agent]:
            Wallet.objects.get_or_create(user=user, defaults={"balance": Decimal("0.00")})
        AccountLockAuditLog.objects.create(locked_user=locked_agent, action="locked", lock_reason="Manual Administrative Lock")
        appeal = AccountUnlockAppeal.objects.create(
            user=locked_agent,
            locked_user=locked_agent,
            appealed_by=super_agent,
            appeal_reason="Please review.",
            status="pending",
        )

        self.client.force_login(crm_user)
        reject_response = self.client.post(
            reverse("betting:account_appeals_review"),
            {
                "review_appeal": "1",
                "appeal_id": str(appeal.id),
                "action": "reject",
                "admin_comment": "",
                "return_to": reverse("betting:account_appeals_review"),
            },
            follow=True,
        )
        self.assertEqual(reject_response.status_code, 200)
        appeal.refresh_from_db()
        self.assertEqual(appeal.status, "pending")
        self.assertContains(reject_response, "Admin comment is required when rejecting an appeal.")

        export_response = self.client.get(reverse("betting:locked_accounts_export"), {"format": "csv"})
        self.assertEqual(export_response.status_code, 200)
        self.assertEqual(export_response["Content-Type"], "text/csv")

        self.client.force_login(super_agent)
        forbidden_export = self.client.get(reverse("betting:locked_accounts_export"), {"format": "csv"})
        self.assertEqual(forbidden_export.status_code, 403)
