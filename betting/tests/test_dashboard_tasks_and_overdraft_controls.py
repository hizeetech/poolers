from datetime import timedelta
from decimal import Decimal

from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from betting.models import BettingPeriod, DashboardTask, Fixture, User
from betting.services.loan_overdraft import create_manual_overdraft


class DashboardTasksAndOverdraftControlsTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.admin_user = User.objects.create_user(
            email="opsadmin@example.com",
            password="testpassword",
            user_type="admin",
            is_staff=True,
        )
        self.crm_user = User.objects.create_user(
            email="crmuser@example.com",
            password="testpassword",
            user_type="crm",
        )
        self.retail_manager = User.objects.create_user(
            email="retailmanager@example.com",
            password="testpassword",
            user_type="retail_manager",
        )

    def test_crm_dashboard_places_tabs_above_ticket_widget_and_completes_task_report(self):
        task = DashboardTask.objects.create(
            title="Call inactive agents",
            description="Follow up with inactive agents and submit outcome.",
            assigned_to=self.crm_user,
            created_by=self.admin_user,
        )
        self.client.force_login(self.crm_user)

        response = self.client.get(reverse("betting:crm_dashboard"), {"tab": "tasks"})

        self.assertEqual(response.status_code, 200)
        content = response.content.decode("utf-8")
        self.assertLess(content.index('id="crm-dashboard-tabs"'), content.index("CRM Ticket Transactions"))
        self.assertContains(response, "Tasks")
        self.assertContains(response, task.title)

        submit_response = self.client.post(
            reverse("betting:crm_dashboard"),
            {
                "tab": "tasks",
                "submit_task_report": "1",
                "task_id": str(task.id),
                "completion_report": "Completed the outreach and logged each response.",
            },
            follow=True,
        )

        self.assertEqual(submit_response.status_code, 200)
        task.refresh_from_db()
        self.assertEqual(task.status, DashboardTask.STATUS.COMPLETED)
        self.assertIn("Completed the outreach", task.completion_report)
        self.assertIsNotNone(task.completed_at)

    def test_retail_dashboard_tasks_tab_renders_assigned_task_and_accepts_report(self):
        task = DashboardTask.objects.create(
            title="Inspect dormant outlets",
            description="Visit assigned outlets and submit inspection notes.",
            assigned_to=self.retail_manager,
            created_by=self.admin_user,
        )
        self.client.force_login(self.retail_manager)

        response = self.client.get(reverse("betting:retail_dashboard"), {"tab": "tasks"})

        self.assertEqual(response.status_code, 200)
        content = response.content.decode("utf-8")
        self.assertLess(content.index('id="retail-dashboard-tabs"'), content.index("Retail Manager Ticket Transactions"))
        self.assertContains(response, task.title)

        submit_response = self.client.post(
            reverse("betting:retail_dashboard"),
            {
                "tab": "tasks",
                "submit_task_report": "1",
                "task_id": str(task.id),
                "completion_report": "Visited all outlets and attached the field summary.",
            },
            follow=True,
        )

        self.assertEqual(submit_response.status_code, 200)
        task.refresh_from_db()
        self.assertEqual(task.status, DashboardTask.STATUS.COMPLETED)
        self.assertIn("Visited all outlets", task.completion_report)

    def test_cashier_place_bet_is_blocked_when_uplink_has_outstanding_overdraft(self):
        super_agent = User.objects.create_user(
            email="cashierblock-super@example.com",
            password="testpassword",
            user_type="super_agent",
        )
        agent = User.objects.create_user(
            email="cashierblock-agent@example.com",
            password="testpassword",
            user_type="agent",
            super_agent=super_agent,
        )
        cashier = User.objects.create_user(
            email="cashierblock-cashier@example.com",
            password="testpassword",
            user_type="cashier",
            agent=agent,
            super_agent=super_agent,
        )
        create_manual_overdraft(
            actor=self.admin_user,
            borrower=agent,
            amount=Decimal("5000.00"),
            reason="Agent overdraft keeps cashier bet access disabled.",
        )

        today = timezone.localdate()
        period = BettingPeriod.objects.create(
            name="Blocked Week",
            start_date=today - timedelta(days=1),
            end_date=today + timedelta(days=5),
            is_active=True,
        )
        Fixture.objects.create(
            betting_period=period,
            serial_number=1,
            home_team="Team A",
            away_team="Team B",
            match_date=today + timedelta(days=1),
            match_time=timezone.localtime().time().replace(hour=17, minute=0, second=0, microsecond=0),
            status="scheduled",
            is_active=True,
            draw_odd="3.00",
        )

        self.client.force_login(cashier)
        page_response = self.client.get(reverse("betting:fixtures_with_period", args=[period.id]))
        self.assertEqual(page_response.status_code, 200)
        self.assertContains(
            page_response,
            'id="place-bet-btn" class="btn fixture-period-accent-button w-100 rounded-pill py-2 shadow-sm" disabled',
        )

        bet_response = self.client.post(
            reverse("betting:place_bet"),
            {},
            HTTP_ACCEPT="application/json",
        )
        self.assertEqual(bet_response.status_code, 200)
        self.assertEqual(bet_response.json()["message"], "You are not authorized to place a bet")
