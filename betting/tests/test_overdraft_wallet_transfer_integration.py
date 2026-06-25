from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from betting.models import Loan, Transaction, User, Wallet, WalletLedgerEntry


class OverdraftWalletTransferIntegrationTests(TestCase):
    def setUp(self):
        self.account_user = User.objects.create_user(
            email="acct-issuer@test.com",
            password="testpassword",
            user_type="account_user",
            username="acct_issuer",
        )
        self.admin_user = User.objects.create_user(
            email="ops-admin@test.com",
            password="testpassword",
            user_type="admin",
            username="ops_admin",
            is_staff=True,
        )
        self.super_agent = User.objects.create_user(
            email="sa-issuer@test.com",
            password="testpassword",
            user_type="super_agent",
            username="sa_issuer",
        )
        self.agent = User.objects.create_user(
            email="agent-borrower@test.com",
            password="testpassword",
            user_type="agent",
            username="agent_borrower",
            phone_number="08010000001",
            super_agent=self.super_agent,
        )
        self.other_agent = User.objects.create_user(
            email="other-agent@test.com",
            password="testpassword",
            user_type="agent",
            username="other_agent",
            phone_number="08010000002",
            super_agent=self.super_agent,
        )

        for user, balance in [
            (self.account_user, Decimal("1000.00")),
            (self.admin_user, Decimal("0.00")),
            (self.super_agent, Decimal("1000.00")),
            (self.agent, Decimal("0.00")),
            (self.other_agent, Decimal("0.00")),
        ]:
            Wallet.objects.get_or_create(user=user, defaults={"balance": balance})
            wallet = Wallet.objects.get(user=user)
            wallet.balance = balance
            wallet.save(update_fields=["balance"])

    def test_wallet_transfer_checkbox_creates_overdraft_and_marks_credit_entry(self):
        self.client.force_login(self.account_user)

        response = self.client.post(
            reverse("betting:wallet_transfer"),
            {
                "recipient_identifier": str(self.agent.id),
                "amount": "100.00",
                "transaction_type": "credit",
                "description": "Wallet transfer overdraft",
                "treat_as_overdraft": "1",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        loan = Loan.objects.get(borrower=self.agent)
        self.assertEqual(loan.lender, self.account_user)
        self.assertEqual(loan.amount, Decimal("100.00"))
        self.assertEqual(loan.outstanding_balance, Decimal("100.00"))
        self.assertEqual(loan.status, "active")

        self.account_user.wallet.refresh_from_db()
        self.agent.wallet.refresh_from_db()
        self.assertEqual(self.account_user.wallet.balance, Decimal("900.00"))
        self.assertEqual(self.agent.wallet.balance, Decimal("100.00"))

        credit_entry = WalletLedgerEntry.objects.filter(
            user=self.agent,
            transaction__transaction_type="wallet_transfer_in",
        ).latest("id")
        self.assertEqual(credit_entry.metadata.get("classification"), "overdraft")
        self.assertTrue(credit_entry.metadata.get("overdraft"))
        self.assertEqual(credit_entry.metadata.get("loan_id"), loan.id)

    def test_admin_retroactive_conversion_creates_overdraft_without_double_crediting_wallet(self):
        source_tx = Transaction.objects.create(
            user=self.agent,
            initiating_user=self.account_user,
            target_user=self.agent,
            transaction_type="account_user_credit",
            amount=Decimal("150.00"),
            is_successful=True,
            status="completed",
            description="Manual credit before conversion",
            timestamp=timezone.now(),
        )
        self.agent.wallet.apply_delta(
            amount=Decimal("150.00"),
            actor=self.account_user,
            transaction_obj=source_tx,
            reference="manual-credit-1",
            reason=source_tx.description,
            metadata={"classification": "manual_credit", "source": "account_user_credit"},
        )
        original_balance = self.agent.wallet.balance
        credit_entry = WalletLedgerEntry.objects.get(
            user=self.agent,
            transaction=source_tx,
            direction="credit",
        )

        self.client.force_login(self.admin_user)
        response = self.client.post(
            reverse("betting_admin:admin_retail_manual_adjustments"),
            {
                "convert_entry_to_overdraft": "1",
                "wallet_entry_id": str(credit_entry.id),
                "conversion_reason": "Issuer forgot to tick overdraft",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        loan = Loan.objects.get(borrower=self.agent, amount=Decimal("150.00"))
        self.assertEqual(loan.outstanding_balance, Decimal("150.00"))

        self.agent.wallet.refresh_from_db()
        self.assertEqual(self.agent.wallet.balance, original_balance)

        credit_entry.refresh_from_db()
        self.assertEqual(credit_entry.metadata.get("classification"), "overdraft")
        self.assertTrue(credit_entry.metadata.get("converted_to_overdraft"))
        self.assertEqual(credit_entry.metadata.get("loan_id"), loan.id)

    def test_admin_issued_overdraft_page_renders_account_user_role_label(self):
        Loan.objects.create(
            borrower=self.agent,
            lender=self.account_user,
            amount=Decimal("200.00"),
            requested_amount=Decimal("200.00"),
            qualified_amount=Decimal("250.00"),
            outstanding_balance=Decimal("125.00"),
            repaid_amount=Decimal("75.00"),
            status="active",
            approval_level="account_user",
            approved_by=self.account_user,
            approved_at=timezone.now() - timedelta(days=1),
            due_date=timezone.now() + timedelta(days=2),
        )

        self.client.force_login(self.admin_user)
        response = self.client.get(reverse("betting_admin:admin_issued_overdrafts"))

        self.assertEqual(response.status_code, 200)
        rows = list(response.context["rows_page"].object_list)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["agent_username"], "agent_borrower")
        self.assertEqual(rows[0]["issuer_role"], "Account User")
        self.assertContains(response, "Issued Overdraft")
        self.assertContains(response, "Account User")

    def test_account_user_dashboard_issued_overdrafts_only_lists_account_user_issuances(self):
        Loan.objects.create(
            borrower=self.agent,
            lender=self.account_user,
            amount=Decimal("180.00"),
            requested_amount=Decimal("180.00"),
            qualified_amount=Decimal("200.00"),
            outstanding_balance=Decimal("180.00"),
            status="active",
            approval_level="account_user",
            approved_by=self.account_user,
            approved_at=timezone.now() - timedelta(hours=5),
            due_date=timezone.now() + timedelta(days=3),
        )
        Loan.objects.create(
            borrower=self.other_agent,
            lender=self.super_agent,
            amount=Decimal("220.00"),
            requested_amount=Decimal("220.00"),
            qualified_amount=Decimal("240.00"),
            outstanding_balance=Decimal("220.00"),
            status="active",
            approval_level="super_agent",
            approved_by=self.super_agent,
            approved_at=timezone.now() - timedelta(hours=2),
            due_date=timezone.now() + timedelta(days=3),
        )

        self.client.force_login(self.account_user)
        response = self.client.get(reverse("betting:account_user_dashboard"))

        self.assertEqual(response.status_code, 200)
        rows = response.context["issued_overdraft_rows"]
        usernames = [row["agent_username"] for row in rows]
        self.assertIn("agent_borrower", usernames)
        self.assertNotIn("other_agent", usernames)
        self.assertContains(response, "Issued Overdrafts")
