from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from betting.models import Loan, RetailManagerSuperAgentMapping, User, Wallet


class OverdraftReportingTests(TestCase):
    def setUp(self):
        self.password = "pass12345"

        self.retail_manager = User.objects.create_user(
            email="rm-overdraft@test.com",
            password=self.password,
            user_type="retail_manager",
            username="rm_overdraft",
        )
        self.crm_user = User.objects.create_user(
            email="crm-overdraft@test.com",
            password=self.password,
            user_type="crm",
            username="crm_overdraft",
        )
        self.finance_user = User.objects.create_user(
            email="finance-overdraft@test.com",
            password=self.password,
            user_type="finance",
            username="finance_overdraft",
            finance_role="manager",
        )
        self.account_user = User.objects.create_user(
            email="account-overdraft@test.com",
            password=self.password,
            user_type="account_user",
            username="account_overdraft",
        )

        self.master_agent_a = User.objects.create_user(
            email="ma-a@test.com",
            password=self.password,
            user_type="master_agent",
            username="ma_a",
        )
        self.master_agent_b = User.objects.create_user(
            email="ma-b@test.com",
            password=self.password,
            user_type="master_agent",
            username="ma_b",
        )
        self.super_agent_a = User.objects.create_user(
            email="sa-a@test.com",
            password=self.password,
            user_type="super_agent",
            username="sa_a",
            master_agent=self.master_agent_a,
        )
        self.super_agent_b = User.objects.create_user(
            email="sa-b@test.com",
            password=self.password,
            user_type="super_agent",
            username="sa_b",
            master_agent=self.master_agent_b,
        )
        self.agent_mapped = User.objects.create_user(
            email="mapped-agent@test.com",
            password=self.password,
            user_type="agent",
            username="mapped_agent",
            first_name="Mapped",
            last_name="Agent",
            master_agent=self.master_agent_a,
            super_agent=self.super_agent_a,
            phone_number="08011111111",
        )
        self.agent_outsider = User.objects.create_user(
            email="outsider-agent@test.com",
            password=self.password,
            user_type="agent",
            username="outsider_agent",
            first_name="Outsider",
            last_name="Agent",
            master_agent=self.master_agent_b,
            super_agent=self.super_agent_b,
            phone_number="08022222222",
        )

        for user in [
            self.retail_manager,
            self.crm_user,
            self.finance_user,
            self.account_user,
            self.super_agent_a,
            self.super_agent_b,
            self.agent_mapped,
            self.agent_outsider,
        ]:
            Wallet.objects.get_or_create(user=user, defaults={"balance": Decimal("0.00")})

        RetailManagerSuperAgentMapping.objects.create(
            retail_manager=self.retail_manager,
            super_agent=self.super_agent_a,
        )

        self.mapped_loan = Loan.objects.create(
            borrower=self.agent_mapped,
            lender=self.finance_user,
            amount=Decimal("1000.00"),
            requested_amount=Decimal("1000.00"),
            qualified_amount=Decimal("1200.00"),
            qualification_ticket_count=6,
            qualification_deposit_volume=Decimal("4000.00"),
            outstanding_balance=Decimal("400.00"),
            repaid_amount=Decimal("600.00"),
            status="active",
            approval_level="admin",
            approved_by=self.finance_user,
            approved_at=timezone.now() - timedelta(days=4),
            due_date=timezone.now() + timedelta(days=3),
        )
        self.outsider_loan = Loan.objects.create(
            borrower=self.agent_outsider,
            lender=self.finance_user,
            amount=Decimal("2000.00"),
            requested_amount=Decimal("2000.00"),
            qualified_amount=Decimal("2500.00"),
            qualification_ticket_count=8,
            qualification_deposit_volume=Decimal("7000.00"),
            outstanding_balance=Decimal("1500.00"),
            repaid_amount=Decimal("500.00"),
            status="active",
            approval_level="admin",
            approved_by=self.finance_user,
            approved_at=timezone.now() - timedelta(days=6),
            due_date=timezone.now() - timedelta(days=1),
        )

    def test_retail_manager_overdraft_monitoring_is_hierarchy_scoped(self):
        self.client.force_login(self.retail_manager)

        response = self.client.get(reverse("betting:retail_dashboard"), {"tab": "overdraft_monitoring"})

        self.assertEqual(response.status_code, 200)
        rows = list(response.context["overdraft_reporting_page"].object_list)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["agent_username"], "mapped_agent")
        self.assertContains(response, "mapped_agent")
        self.assertNotContains(response, "outsider_agent")

    def test_retail_manager_cannot_open_outsider_overdraft_detail(self):
        self.client.force_login(self.retail_manager)

        allowed_response = self.client.get(reverse("betting:overdraft_report_detail", args=[self.mapped_loan.id]))
        denied_response = self.client.get(reverse("betting:overdraft_report_detail", args=[self.outsider_loan.id]))

        self.assertEqual(allowed_response.status_code, 200)
        self.assertEqual(denied_response.status_code, 404)

    def test_crm_dashboard_overdraft_center_has_global_visibility(self):
        self.client.force_login(self.crm_user)

        response = self.client.get(reverse("betting:crm_dashboard"), {"tab": "overdraft_center"})

        self.assertEqual(response.status_code, 200)
        rows = list(response.context["overdraft_reporting_page"].object_list)
        usernames = {row["agent_username"] for row in rows}
        self.assertEqual(usernames, {"mapped_agent", "outsider_agent"})

    def test_finance_export_includes_all_overdraft_rows(self):
        self.client.force_login(self.finance_user)

        response = self.client.get(
            reverse("betting:finance_export"),
            {"dataset": "overdrafts", "format": "csv"},
        )

        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn("mapped_agent", body)
        self.assertIn("outsider_agent", body)

    def test_account_user_can_export_and_view_global_overdraft_data(self):
        self.client.force_login(self.account_user)

        dashboard_response = self.client.get(reverse("betting:account_user_dashboard"))
        export_response = self.client.get(
            reverse("betting:account_user_export"),
            {"dataset": "overdrafts", "format": "csv"},
        )

        self.assertEqual(dashboard_response.status_code, 200)
        rows = list(dashboard_response.context["overdraft_reporting_page"].object_list)
        usernames = {row["agent_username"] for row in rows}
        self.assertEqual(usernames, {"mapped_agent", "outsider_agent"})
        self.assertEqual(export_response.status_code, 200)
        self.assertIn("mapped_agent", export_response.content.decode())
        self.assertIn("outsider_agent", export_response.content.decode())

    def test_fully_settled_loan_renders_no_lock_columns_even_if_borrower_flags_are_stale(self):
        self.agent_mapped.is_locked = True
        self.agent_mapped.withdrawal_locked = True
        self.agent_mapped.save(update_fields=["is_locked", "withdrawal_locked"])
        self.mapped_loan.status = "settled"
        self.mapped_loan.outstanding_balance = Decimal("0.00")
        self.mapped_loan.save(update_fields=["status", "outstanding_balance", "updated_at"])

        self.client.force_login(self.account_user)
        response = self.client.get(reverse("betting:account_user_dashboard"))

        self.assertEqual(response.status_code, 200)
        mapped_row = next(
            row for row in response.context["overdraft_reporting_page"].object_list
            if row["agent_username"] == "mapped_agent"
        )
        self.assertEqual(mapped_row["status"], "Fully Settled")
        self.assertEqual(mapped_row["withdrawal_locked"], "No")
        self.assertEqual(mapped_row["account_locked"], "No")

    def test_account_locked_no_filter_includes_fully_settled_loan_with_stale_borrower_lock_flags(self):
        self.agent_mapped.is_locked = True
        self.agent_mapped.withdrawal_locked = True
        self.agent_mapped.save(update_fields=["is_locked", "withdrawal_locked"])
        self.mapped_loan.status = "settled"
        self.mapped_loan.outstanding_balance = Decimal("0.00")
        self.mapped_loan.save(update_fields=["status", "outstanding_balance", "updated_at"])

        self.client.force_login(self.crm_user)
        response = self.client.get(
            reverse("betting:crm_dashboard"),
            {"tab": "overdraft_center", "loan_account_locked": "no"},
        )

        self.assertEqual(response.status_code, 200)
        usernames = {row["agent_username"] for row in response.context["overdraft_reporting_page"].object_list}
        self.assertIn("mapped_agent", usernames)
