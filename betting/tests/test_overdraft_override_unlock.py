from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from betting.models import AccountLockAuditLog, Loan, LoanAuditLog, User, Wallet
from betting.services.loan_overdraft import enforce_due_loans


class OverdraftOverrideUnlockTests(TestCase):
    def setUp(self):
        self.password = "password123"
        self.superadmin = User.objects.create_superuser(
            email="superadmin-override@test.com",
            password=self.password,
        )
        self.admin_user = User.objects.create_user(
            email="admin-override@test.com",
            password=self.password,
            user_type="admin",
            username="admin_override",
            is_staff=True,
        )
        self.super_agent = User.objects.create_user(
            email="sa-override@test.com",
            password=self.password,
            user_type="super_agent",
            username="sa_override",
        )
        self.agent = User.objects.create_user(
            email="agent-override@test.com",
            password=self.password,
            user_type="agent",
            username="agent_override",
            super_agent=self.super_agent,
        )
        self.cashier = User.objects.create_user(
            email="cashier-override@test.com",
            password=self.password,
            user_type="cashier",
            username="cashier_override",
            agent=self.agent,
        )
        for user in [self.superadmin, self.admin_user, self.super_agent, self.agent, self.cashier]:
            Wallet.objects.get_or_create(user=user, defaults={"balance": Decimal("0.00")})

    def _create_overdue_locked_loan(self):
        loan = Loan.objects.create(
            borrower=self.agent,
            lender=self.super_agent,
            amount=Decimal("800.00"),
            requested_amount=Decimal("800.00"),
            qualified_amount=Decimal("1000.00"),
            outstanding_balance=Decimal("800.00"),
            status="active",
            loan_type="agent_overdraft",
            approval_level="super_agent",
            due_date=timezone.now() - timedelta(days=2),
        )
        enforce_due_loans()
        loan.refresh_from_db()
        self.agent.refresh_from_db()
        self.cashier.refresh_from_db()
        return loan

    def test_superadmin_can_override_unlock_without_payment_and_debt_remains(self):
        loan = self._create_overdue_locked_loan()
        self.assertTrue(loan.account_locked_due_to_default)
        self.assertTrue(self.agent.is_locked)
        self.assertTrue(self.cashier.is_locked)

        self.client.force_login(self.superadmin)
        response = self.client.post(
            reverse("betting_admin:admin_loan_overdraft_center") + "?tab=locked",
            {
                "loan_id": str(loan.id),
                "reason": "Temporary admin relief pending reconciliation review",
                "override_unlock_submit": "1",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        loan.refresh_from_db()
        self.agent.refresh_from_db()
        self.cashier.refresh_from_db()

        self.assertFalse(self.agent.is_locked)
        self.assertFalse(self.cashier.is_locked)
        self.assertFalse(loan.account_locked_due_to_default)
        self.assertTrue((loan.workflow_snapshot or {}).get("lock_override_active"))
        self.assertEqual(loan.outstanding_balance, Decimal("800.00"))
        self.assertTrue(
            LoanAuditLog.objects.filter(
                loan=loan,
                action="override",
                performed_by=self.superadmin,
            ).exists()
        )
        self.assertTrue(
            LoanAuditLog.objects.filter(
                loan=loan,
                action="account_unlocked",
                performed_by=self.superadmin,
            ).exists()
        )
        self.assertTrue(
            AccountLockAuditLog.objects.filter(
                locked_user=self.agent,
                action="unlocked",
                reviewed_by=self.superadmin,
            ).exists()
        )
        self.assertTrue(
            AccountLockAuditLog.objects.filter(
                locked_user=self.cashier,
                action="unlocked",
                reviewed_by=self.superadmin,
            ).exists()
        )

        enforce_due_loans()
        loan.refresh_from_db()
        self.agent.refresh_from_db()
        self.cashier.refresh_from_db()
        self.assertFalse(loan.account_locked_due_to_default)
        self.assertFalse(self.agent.is_locked)
        self.assertFalse(self.cashier.is_locked)

    def test_non_superuser_admin_cannot_override_unlock_without_payment(self):
        loan = self._create_overdue_locked_loan()

        self.client.force_login(self.admin_user)
        response = self.client.post(
            reverse("betting_admin:admin_loan_overdraft_center") + "?tab=locked",
            {
                "loan_id": str(loan.id),
                "reason": "Attempted admin override without superadmin rights",
                "override_unlock_submit": "1",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        loan.refresh_from_db()
        self.agent.refresh_from_db()
        self.cashier.refresh_from_db()
        self.assertTrue(loan.account_locked_due_to_default)
        self.assertTrue(self.agent.is_locked)
        self.assertTrue(self.cashier.is_locked)
        self.assertFalse((loan.workflow_snapshot or {}).get("lock_override_active", False))
        self.assertFalse(LoanAuditLog.objects.filter(loan=loan, action="override").exists())
