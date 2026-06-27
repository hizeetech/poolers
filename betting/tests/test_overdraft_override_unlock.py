from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from betting.models import (
    AccountLockAuditLog,
    BettingPeriod,
    Fixture,
    Loan,
    LoanAuditLog,
    LoanRepayment,
    Transaction,
    User,
    Wallet,
)
from betting.forms import AdminOverdraftWalletFundingForm
from betting.services.loan_overdraft import can_user_place_bet, enforce_due_loans


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

    def test_override_unlock_keeps_withdrawals_blocked_but_restores_cashier_bet_access(self):
        loan = self._create_overdue_locked_loan()
        today = timezone.localdate()
        period = BettingPeriod.objects.create(
            name="Override Unlock Week",
            start_date=today - timedelta(days=1),
            end_date=today + timedelta(days=5),
            is_active=True,
        )
        Fixture.objects.create(
            betting_period=period,
            serial_number=11,
            home_team="Override Home",
            away_team="Override Away",
            match_date=today + timedelta(days=1),
            match_time=timezone.localtime().time().replace(hour=18, minute=0, second=0, microsecond=0),
            status="scheduled",
            is_active=True,
            draw_odd="3.10",
        )

        self.assertFalse(can_user_place_bet(self.cashier))

        self.client.force_login(self.superadmin)
        response = self.client.post(
            reverse("betting_admin:admin_loan_overdraft_center") + "?tab=locked",
            {
                "loan_id": str(loan.id),
                "reason": "Restore login and ticket sales while keeping repayment enforcement active",
                "override_unlock_submit": "1",
            },
            follow=True,
        )
        self.assertEqual(response.status_code, 200)

        loan.refresh_from_db()
        self.agent.refresh_from_db()
        self.cashier.refresh_from_db()

        self.assertTrue((loan.workflow_snapshot or {}).get("lock_override_active"))
        self.assertFalse(self.agent.is_locked)
        self.assertFalse(self.cashier.is_locked)
        self.assertTrue(can_user_place_bet(self.cashier))

        agent_client = self.client_class()
        cashier_client = self.client_class()
        self.assertTrue(agent_client.login(username=self.agent.username, password=self.password))
        self.assertTrue(cashier_client.login(username=self.cashier.username, password=self.password))

        wallet_response = agent_client.get(reverse("betting:wallet"))
        self.assertEqual(wallet_response.status_code, 200)
        self.assertFalse(wallet_response.context["can_withdraw_from_wallet"])
        self.assertContains(wallet_response, "Withdrawal Disabled")

        fixtures_response = cashier_client.get(reverse("betting:fixtures_with_period", args=[period.id]))
        self.assertEqual(fixtures_response.status_code, 200)
        self.assertTrue(fixtures_response.context["can_place_bet"])

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

    def test_superadmin_can_relock_after_override_unlock(self):
        loan = self._create_overdue_locked_loan()

        self.client.force_login(self.superadmin)
        unlock_response = self.client.post(
            reverse("betting_admin:admin_loan_overdraft_center") + "?tab=locked",
            {
                "loan_id": str(loan.id),
                "reason": "Temporary admin relief pending reconciliation review",
                "override_unlock_submit": "1",
            },
            follow=True,
        )
        self.assertEqual(unlock_response.status_code, 200)

        relock_response = self.client.post(
            reverse("betting_admin:admin_loan_overdraft_center") + "?tab=locked",
            {
                "loan_id": str(loan.id),
                "reason": "Unlock was granted in error",
                "relock_submit": "1",
            },
            follow=True,
        )

        self.assertEqual(relock_response.status_code, 200)
        loan.refresh_from_db()
        self.agent.refresh_from_db()
        self.cashier.refresh_from_db()

        self.assertTrue(loan.account_locked_due_to_default)
        self.assertFalse((loan.workflow_snapshot or {}).get("lock_override_active", False))
        self.assertEqual((loan.workflow_snapshot or {}).get("lock_relock_reason"), "Unlock was granted in error")
        self.assertTrue(self.agent.is_locked)
        self.assertTrue(self.cashier.is_locked)
        relock_audit = LoanAuditLog.objects.filter(loan=loan, action="override").latest("created_at")
        self.assertEqual(relock_audit.performed_by, self.superadmin)
        self.assertEqual(relock_audit.metadata.get("override_type"), "relock_after_override")
        self.assertTrue(
            AccountLockAuditLog.objects.filter(
                locked_user=self.agent,
                action="locked",
                locked_by=self.superadmin,
            ).exists()
        )
        self.assertTrue(
            AccountLockAuditLog.objects.filter(
                locked_user=self.cashier,
                action="locked",
                locked_by=self.superadmin,
            ).exists()
        )

    def test_overdue_center_shows_total_balance_and_new_action_buttons(self):
        loan = self._create_overdue_locked_loan()
        Wallet.objects.filter(user=self.agent).update(balance=Decimal("120.00"))
        Wallet.objects.filter(user=self.cashier).update(balance=Decimal("80.00"))

        self.client.force_login(self.superadmin)
        response = self.client.get(reverse("betting_admin:admin_loan_overdraft_center"), {"tab": "overdue"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Total Balance (Agent + Downline)")
        self.assertContains(response, "Recall Overdraft")
        self.assertContains(response, "Clear Overdraft")

    def test_overdraft_wallet_funding_form_lists_super_agents_by_username(self):
        second_super_agent = User.objects.create_user(
            email="sa-second@test.com",
            password=self.password,
            user_type="super_agent",
            username="sa_second",
        )
        Wallet.objects.get_or_create(user=second_super_agent, defaults={"balance": Decimal("0.00")})

        form = AdminOverdraftWalletFundingForm()
        labels = [label for _value, label in form.fields["super_agent"].choices if label and label != "Select super agent"]

        self.assertIn("sa_override", labels)
        self.assertIn("sa_second", labels)
        self.assertNotIn("sa-override@test.com", labels)
        self.assertNotIn("sa-second@test.com", labels)

    def test_outstanding_tab_shows_offset_ready_highlight_for_overdue_loan(self):
        loan = self._create_overdue_locked_loan()
        Wallet.objects.filter(user=self.agent).update(balance=Decimal("500.00"))
        Wallet.objects.filter(user=self.cashier).update(balance=Decimal("400.00"))

        self.client.force_login(self.superadmin)
        response = self.client.get(reverse("betting_admin:admin_loan_overdraft_center"), {"tab": "outstanding"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "overdraft-balance-ready")
        self.assertContains(response, "overdraft-ready-star")
        self.assertContains(response, "₦900.00")

    def test_overdue_center_shows_relock_for_unlocked_past_due_loan_and_relocks(self):
        loan = self._create_overdue_locked_loan()
        loan.status = "active"
        loan.account_locked_due_to_default = False
        loan.workflow_snapshot = {}
        loan.save(update_fields=["status", "account_locked_due_to_default", "workflow_snapshot", "updated_at"])
        self.agent.is_locked = False
        self.agent.lock_reason = ""
        self.agent.locked_at = None
        self.agent.save(update_fields=["is_locked", "lock_reason", "locked_at"])
        self.cashier.is_locked = False
        self.cashier.lock_reason = ""
        self.cashier.locked_at = None
        self.cashier.save(update_fields=["is_locked", "lock_reason", "locked_at"])

        self.client.force_login(self.superadmin)
        response = self.client.get(reverse("betting_admin:admin_loan_overdraft_center"), {"tab": "overdue"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Re-lock")
        self.assertContains(response, "Overdue")

        relock_response = self.client.post(
            reverse("betting_admin:admin_loan_overdraft_center") + "?tab=overdue",
            {
                "loan_id": str(loan.id),
                "reason": "Past due borrower must be locked again",
                "relock_submit": "1",
            },
            follow=True,
        )

        self.assertEqual(relock_response.status_code, 200)
        loan.refresh_from_db()
        self.agent.refresh_from_db()
        self.cashier.refresh_from_db()

        self.assertEqual(loan.status, "overdue")
        self.assertTrue(loan.account_locked_due_to_default)
        self.assertTrue(self.agent.is_locked)
        self.assertTrue(self.cashier.is_locked)

    def test_recall_overdraft_fully_settles_and_unlocks_agent_and_cashier(self):
        loan = self._create_overdue_locked_loan()
        loan.outstanding_balance = Decimal("150.00")
        loan.amount = Decimal("150.00")
        loan.requested_amount = Decimal("150.00")
        loan.save(update_fields=["outstanding_balance", "amount", "requested_amount", "updated_at"])
        Wallet.objects.filter(user=self.agent).update(balance=Decimal("100.00"))
        Wallet.objects.filter(user=self.cashier).update(balance=Decimal("80.00"))

        self.client.force_login(self.superadmin)
        response = self.client.post(
            reverse("betting_admin:admin_loan_overdraft_center") + "?tab=overdue",
            {
                "loan_id": str(loan.id),
                "recall_overdraft_submit": "1",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        loan.refresh_from_db()
        self.agent.refresh_from_db()
        self.cashier.refresh_from_db()
        self.agent.wallet.refresh_from_db()
        self.cashier.wallet.refresh_from_db()

        self.assertEqual(loan.outstanding_balance, Decimal("0.00"))
        self.assertEqual(loan.status, "settled")
        self.assertFalse(self.agent.is_locked)
        self.assertFalse(self.cashier.is_locked)
        self.assertEqual(self.agent.wallet.balance, Decimal("0.00"))
        self.assertEqual(self.cashier.wallet.balance, Decimal("30.00"))
        self.assertTrue(
            Transaction.objects.filter(
                user=self.agent,
                transaction_type="account_user_debit",
                description__icontains=f"loan #{loan.id}",
            ).exists()
        )
        self.assertTrue(
            Transaction.objects.filter(
                user=self.cashier,
                transaction_type="account_user_debit",
                description__icontains=f"loan #{loan.id}",
            ).exists()
        )
        self.assertTrue(
            LoanRepayment.objects.filter(
                loan=loan,
                amount=Decimal("150.00"),
                source="manual_settlement",
            ).exists()
        )
        self.assertTrue(
            LoanAuditLog.objects.filter(
                loan=loan,
                action="loan_cleared",
            ).exists()
        )

    def test_recall_overdraft_partially_settles_and_keeps_overdue_lock(self):
        loan = self._create_overdue_locked_loan()
        loan.outstanding_balance = Decimal("150.00")
        loan.amount = Decimal("150.00")
        loan.requested_amount = Decimal("150.00")
        loan.save(update_fields=["outstanding_balance", "amount", "requested_amount", "updated_at"])
        Wallet.objects.filter(user=self.agent).update(balance=Decimal("30.00"))
        Wallet.objects.filter(user=self.cashier).update(balance=Decimal("20.00"))

        self.client.force_login(self.superadmin)
        response = self.client.post(
            reverse("betting_admin:admin_loan_overdraft_center") + "?tab=overdue",
            {
                "loan_id": str(loan.id),
                "recall_overdraft_submit": "1",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        loan.refresh_from_db()
        self.agent.refresh_from_db()
        self.cashier.refresh_from_db()
        self.agent.wallet.refresh_from_db()
        self.cashier.wallet.refresh_from_db()

        self.assertEqual(loan.outstanding_balance, Decimal("100.00"))
        self.assertEqual(loan.status, "overdue")
        self.assertTrue(self.agent.is_locked)
        self.assertTrue(self.cashier.is_locked)
        self.assertEqual(self.agent.wallet.balance, Decimal("0.00"))
        self.assertEqual(self.cashier.wallet.balance, Decimal("0.00"))
        self.assertTrue(
            LoanRepayment.objects.filter(
                loan=loan,
                amount=Decimal("50.00"),
                source="manual_settlement",
            ).exists()
        )

    def test_clear_overdraft_resets_balance_to_zero_and_unlocks(self):
        loan = self._create_overdue_locked_loan()
        loan.outstanding_balance = Decimal("220.00")
        loan.amount = Decimal("220.00")
        loan.requested_amount = Decimal("220.00")
        loan.save(update_fields=["outstanding_balance", "amount", "requested_amount", "updated_at"])

        self.client.force_login(self.superadmin)
        response = self.client.post(
            reverse("betting_admin:admin_loan_overdraft_center") + "?tab=overdue",
            {
                "loan_id": str(loan.id),
                "clear_overdraft_submit": "1",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        loan.refresh_from_db()
        self.agent.refresh_from_db()
        self.cashier.refresh_from_db()

        self.assertEqual(loan.outstanding_balance, Decimal("0.00"))
        self.assertEqual(loan.status, "settled")
        self.assertFalse(self.agent.is_locked)
        self.assertFalse(self.cashier.is_locked)
        clear_audit = LoanAuditLog.objects.filter(loan=loan, action="loan_cleared").latest("created_at")
        self.assertEqual(clear_audit.metadata.get("clear_method"), "admin_clear")
        self.assertFalse(
            Transaction.objects.filter(
                user=self.agent,
                transaction_type="account_user_debit",
                description__icontains=f"loan #{loan.id}",
            ).exists()
        )
