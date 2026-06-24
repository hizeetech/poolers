from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.db.utils import ProgrammingError
from django.utils import timezone

from betting.models import BetTicket, TicketTransactionLedger, Transaction, User, UserWithdrawal, Wallet, WalletLedgerEntry
from commission.models import CommissionPeriod, NetworkCommissionSettings, WeeklyAgentCommission
from commission.services import calculate_weekly_agent_commission, pay_weekly_commission_amount
from void_requests.models import TicketVoidRequest
from void_requests.services import approve_and_void_request


class TicketTransactionLedgerTests(TestCase):
    def setUp(self):
        self.password = "password123"
        self.agent = User.objects.create_user(
            email="ledger-agent@test.com",
            password=self.password,
            user_type="agent",
            username="ledger_agent",
        )
        self.cashier = User.objects.create_user(
            email="ledger-cashier@test.com",
            password=self.password,
            user_type="cashier",
            username="ledger_cashier",
            agent=self.agent,
        )
        self.other_agent = User.objects.create_user(
            email="ledger-other-agent@test.com",
            password=self.password,
            user_type="agent",
            username="ledger_other_agent",
        )
        self.super_agent = User.objects.create_user(
            email="ledger-super-agent@test.com",
            password=self.password,
            user_type="super_agent",
            username="ledger_super_agent",
        )
        self.retail_manager = User.objects.create_user(
            email="ledger-retail@test.com",
            password=self.password,
            user_type="retail_manager",
            username="ledger_retail_manager",
        )
        self.admin_user = User.objects.create_user(
            email="ledger-admin@test.com",
            password=self.password,
            user_type="admin",
            username="ledger_admin",
            is_staff=True,
            is_superuser=True,
        )
        self.other_cashier = User.objects.create_user(
            email="ledger-other-cashier@test.com",
            password=self.password,
            user_type="cashier",
            username="ledger_other_cashier",
            agent=self.other_agent,
        )
        for user in [self.agent, self.cashier, self.other_agent, self.super_agent, self.retail_manager, self.admin_user, self.other_cashier]:
            Wallet.objects.create(user=user, balance=Decimal("0.00"))

    def _create_ticket(self, user):
        return BetTicket.objects.create(
            user=user,
            stake_amount=Decimal("100.00"),
            total_odd=Decimal("2.00"),
            potential_winning=Decimal("200.00"),
            min_winning=Decimal("0.00"),
            max_winning=Decimal("200.00"),
            status="pending",
            bet_type="single",
        )

    def test_wallet_apply_delta_creates_ticket_transaction_ledger_entry(self):
        ticket = self._create_ticket(self.cashier)
        Wallet.objects.filter(user=self.cashier).update(balance=Decimal("200.00"))
        tx = Transaction.objects.create(
            user=self.cashier,
            transaction_type="bet_placement",
            amount=Decimal("100.00"),
            is_successful=True,
            status="completed",
            description=f"Placed bet on ticket {ticket.ticket_id}",
            related_bet_ticket=ticket,
        )

        wallet = Wallet.objects.get(user=self.cashier)
        wallet.apply_delta(
            amount=-Decimal("100.00"),
            actor=self.cashier,
            transaction_obj=tx,
            reference=str(ticket.ticket_id),
            reason=tx.description,
            metadata={"ticket_id": ticket.ticket_id, "source": "ticket_purchase"},
        )

        ledger = TicketTransactionLedger.objects.get(wallet_ledger_entry__transaction=tx)
        self.assertEqual(ledger.user, self.cashier)
        self.assertEqual(ledger.ticket, ticket)
        self.assertEqual(ledger.transaction_type, "Ticket Purchase")
        self.assertEqual(ledger.debit, Decimal("100.00"))
        self.assertEqual(ledger.credit, Decimal("0.00"))
        self.assertEqual(ledger.balance_before, Decimal("200.00"))
        self.assertEqual(ledger.balance_after, Decimal("100.00"))

    def test_voided_ticket_refund_is_projected_to_ticket_transactions(self):
        ticket = self._create_ticket(self.cashier)
        Wallet.objects.filter(user=self.cashier).update(balance=Decimal("0.00"))

        ticket.status = "deleted"
        ticket.deleted_by = self.admin_user
        ticket.save()

        refund_entry = WalletLedgerEntry.objects.filter(
            user=self.cashier,
            direction="credit",
            reference=str(ticket.ticket_id),
        ).latest("created_at")
        ledger = TicketTransactionLedger.objects.get(wallet_ledger_entry=refund_entry)

        self.assertEqual(ledger.ticket, ticket)
        self.assertEqual(ledger.transaction_type, "Ticket Voided")
        self.assertEqual(ledger.source, "Ticket Void")
        self.assertEqual(ledger.credit, Decimal("100.00"))
        self.assertEqual(ledger.balance_before, Decimal("0.00"))
        self.assertEqual(ledger.balance_after, Decimal("100.00"))
        self.assertIn(ticket.ticket_id, ledger.description)

    def test_void_request_approval_refund_is_projected_to_ticket_transactions(self):
        ticket = self._create_ticket(self.cashier)
        Wallet.objects.filter(user=self.cashier).update(balance=Decimal("0.00"))
        void_request = TicketVoidRequest.objects.create(
            ticket=ticket,
            cashier=self.cashier,
            agent=self.agent,
            status=TicketVoidRequest.STATUS_PENDING,
            is_processed=False,
        )

        approve_and_void_request(void_request_id=void_request.id, approved_by=self.admin_user, is_auto=False)

        refund_entry = WalletLedgerEntry.objects.filter(
            user=self.cashier,
            direction="credit",
            reference=str(ticket.ticket_id),
        ).latest("created_at")
        ledger = TicketTransactionLedger.objects.get(wallet_ledger_entry=refund_entry)

        self.assertEqual(ledger.ticket, ticket)
        self.assertEqual(ledger.transaction_type, "Ticket Voided")
        self.assertEqual(ledger.source, "Ticket Void")
        self.assertEqual(ledger.credit, Decimal("100.00"))

    def test_backfill_ticket_transactions_is_safe_to_rerun_for_legacy_transactions(self):
        ticket = self._create_ticket(self.agent)
        legacy_tx = Transaction.objects.create(
            user=self.agent,
            transaction_type="bet_payout",
            amount=Decimal("250.00"),
            is_successful=True,
            status="completed",
            description=f"Legacy payout for ticket {ticket.ticket_id}",
            related_bet_ticket=ticket,
        )

        call_command("backfill_ticket_transactions")
        self.assertTrue(
            TicketTransactionLedger.objects.filter(event_key=f"legacy-transaction:{legacy_tx.id}").exists()
        )
        count_after_first_run = TicketTransactionLedger.objects.count()

        call_command("backfill_ticket_transactions")
        self.assertEqual(TicketTransactionLedger.objects.count(), count_after_first_run)

    def test_agent_ticket_transactions_view_is_scoped_to_own_and_mapped_cashiers(self):
        self.client.force_login(self.agent)

        agent_wallet = Wallet.objects.get(user=self.agent)
        cashier_wallet = Wallet.objects.get(user=self.cashier)
        outsider_wallet = Wallet.objects.get(user=self.other_cashier)
        Wallet.objects.filter(user=self.cashier).update(balance=Decimal("100.00"))

        agent_tx = Transaction.objects.create(
            user=self.agent,
            transaction_type="deposit",
            amount=Decimal("500.00"),
            is_successful=True,
            status="completed",
            description="Agent deposit",
            payment_gateway="paystack",
        )
        cashier_tx = Transaction.objects.create(
            user=self.cashier,
            transaction_type="bet_placement",
            amount=Decimal("50.00"),
            is_successful=True,
            status="completed",
            description="Mapped cashier stake",
        )
        outsider_tx = Transaction.objects.create(
            user=self.other_cashier,
            transaction_type="deposit",
            amount=Decimal("700.00"),
            is_successful=True,
            status="completed",
            description="Outside cashier deposit",
            payment_gateway="monnify",
        )

        agent_wallet.apply_delta(
            amount=Decimal("500.00"),
            actor=self.agent,
            transaction_obj=agent_tx,
            reference="PAYSTACK-AGENT-1",
            reason=agent_tx.description,
            metadata={"source": "gateway_deposit"},
        )
        cashier_wallet.apply_delta(
            amount=-Decimal("50.00"),
            actor=self.cashier,
            transaction_obj=cashier_tx,
            reference="TICKET-CASHIER-1",
            reason=cashier_tx.description,
            metadata={"source": "ticket_purchase"},
        )
        outsider_wallet.apply_delta(
            amount=Decimal("700.00"),
            actor=self.other_cashier,
            transaction_obj=outsider_tx,
            reference="MONNIFY-OUT-1",
            reason=outsider_tx.description,
            metadata={"source": "gateway_deposit"},
        )

        response = self.client.get(reverse("betting:ticket_transactions"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.agent.username)
        self.assertContains(response, self.cashier.username)
        self.assertNotContains(response, self.other_cashier.username)

    def test_admin_ticket_transactions_page_is_available_inside_django_admin(self):
        self.client.force_login(self.admin_user)

        response = self.client.get(reverse("betting_admin:admin_ticket_transactions"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Ticket Transactions")
        self.assertContains(response, "Backfill Ticket Transactions")
        self.assertContains(response, "Backfill Refund Reversal Adjustments")

    @patch("betting.views.backfill_ticket_transaction_ledgers", return_value=7)
    def test_admin_backfill_ticket_transactions_route_runs_backfill(self, mocked_backfill):
        self.client.force_login(self.admin_user)

        response = self.client.post(reverse("betting_admin:admin_backfill_ticket_transactions"))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("betting_admin:admin_ticket_transactions"))
        mocked_backfill.assert_called_once_with()

    @patch(
        "betting.views.backfill_incorrect_refund_reversal_adjustments",
        return_value={"adjusted": 2, "already_adjusted": 1, "eligible": 3, "scanned": 3, "skipped": 0},
    )
    def test_admin_backfill_refund_reversal_adjustments_route_runs_backfill(self, mocked_backfill):
        self.client.force_login(self.admin_user)

        response = self.client.post(
            reverse("betting_admin:admin_backfill_ticket_refund_reversal_adjustments")
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("betting_admin:admin_ticket_transactions"))
        mocked_backfill.assert_called_once_with(actor=self.admin_user)

    def test_admin_ticket_transactions_page_supports_filters(self):
        self.client.force_login(self.admin_user)
        ticket = self._create_ticket(self.cashier)
        wallet = Wallet.objects.get(user=self.cashier)
        Wallet.objects.filter(user=self.cashier).update(balance=Decimal("150.00"))
        tx = Transaction.objects.create(
            user=self.cashier,
            transaction_type="bet_placement",
            amount=Decimal("50.00"),
            is_successful=True,
            status="completed",
            description=f"Filtered stake for {ticket.ticket_id}",
            related_bet_ticket=ticket,
        )
        wallet.refresh_from_db()
        wallet.apply_delta(
            amount=-Decimal("50.00"),
            actor=self.cashier,
            transaction_obj=tx,
            reference=str(ticket.ticket_id),
            reason=tx.description,
            metadata={"ticket_id": ticket.ticket_id, "source": "ticket_purchase"},
        )

        response = self.client.get(
            reverse("betting_admin:admin_ticket_transactions"),
            {
                "username": self.cashier.username,
                "ticket_id": ticket.ticket_id,
                "reference_id": ticket.ticket_id,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.cashier.username)
        self.assertContains(response, ticket.ticket_id)
        self.assertContains(response, "Apply Filters")

    def test_admin_ticket_transactions_page_supports_role_filter(self):
        self.client.force_login(self.admin_user)

        agent_wallet = Wallet.objects.get(user=self.agent)
        cashier_wallet = Wallet.objects.get(user=self.cashier)
        Wallet.objects.filter(user=self.agent).update(balance=Decimal("200.00"))
        Wallet.objects.filter(user=self.cashier).update(balance=Decimal("100.00"))

        agent_tx = Transaction.objects.create(
            user=self.agent,
            transaction_type="deposit",
            amount=Decimal("50.00"),
            is_successful=True,
            status="completed",
            description="Agent deposit for role filter",
            payment_gateway="paystack",
        )
        cashier_tx = Transaction.objects.create(
            user=self.cashier,
            transaction_type="deposit",
            amount=Decimal("30.00"),
            is_successful=True,
            status="completed",
            description="Cashier deposit for role filter",
            payment_gateway="monnify",
        )

        agent_wallet.refresh_from_db()
        cashier_wallet.refresh_from_db()
        agent_wallet.apply_delta(
            amount=Decimal("50.00"),
            actor=self.agent,
            transaction_obj=agent_tx,
            reference="PAYSTACK-ROLE-AGENT",
            reason=agent_tx.description,
            metadata={"source": "gateway_deposit"},
        )
        cashier_wallet.apply_delta(
            amount=Decimal("30.00"),
            actor=self.cashier,
            transaction_obj=cashier_tx,
            reference="MONNIFY-ROLE-CASHIER",
            reason=cashier_tx.description,
            metadata={"source": "gateway_deposit"},
        )

        response = self.client.get(
            reverse("betting_admin:admin_ticket_transactions"),
            {"role": "cashier"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Role")
        self.assertContains(response, self.cashier.username)
        self.assertNotContains(response, self.agent.username)

    def test_rejected_withdrawal_uses_wallet_ledger_entry_balances(self):
        wallet = Wallet.objects.get(user=self.cashier)
        Wallet.objects.filter(user=self.cashier).update(balance=Decimal("200.00"))
        withdrawal = UserWithdrawal.objects.create(
            user=self.cashier,
            amount=Decimal("50.00"),
            bank_name="Demo Bank",
            account_name="Ledger Cashier",
            account_number="0123456789",
            balance_before=Decimal("200.00"),
            balance_after=Decimal("150.00"),
            status="pending",
        )
        tx = Transaction.objects.create(
            user=self.cashier,
            transaction_type="withdrawal",
            amount=Decimal("50.00"),
            is_successful=True,
            status="completed",
            description=f"Withdrawal request {withdrawal.id} created (deducted from wallet).",
            related_withdrawal_request=withdrawal,
        )

        wallet.refresh_from_db()
        wallet.apply_delta(
            amount=-Decimal("50.00"),
            actor=self.cashier,
            transaction_obj=tx,
            reference=str(withdrawal.id),
            reason=tx.description,
            metadata={"withdrawal_id": withdrawal.id, "source": "withdraw_request"},
        )

        withdrawal.status = "rejected"
        withdrawal.save()
        withdrawal.refresh_from_db()

        refund_entry = WalletLedgerEntry.objects.filter(
            user=self.cashier,
            reference=str(withdrawal.id),
            direction="credit",
        ).latest("created_at")

        self.assertEqual(withdrawal.balance_before, refund_entry.balance_before)
        self.assertEqual(withdrawal.balance_after, refund_entry.balance_after)
        refund_ledger = TicketTransactionLedger.objects.get(wallet_ledger_entry=refund_entry)
        self.assertEqual(refund_ledger.transaction_type, "Withdrawal Refund")

    def test_backfill_ticket_refund_reversal_adjustments_restores_wrongly_debited_void_refund_even_if_wallet_stays_negative(self):
        ticket = self._create_ticket(self.cashier)
        wallet = Wallet.objects.get(user=self.cashier)
        Wallet.objects.filter(user=self.cashier).update(balance=Decimal("0.00"))

        ticket.status = "deleted"
        ticket.deleted_by = self.admin_user
        ticket.save()
        wallet.refresh_from_db()
        self.assertEqual(wallet.balance, Decimal("100.00"))

        original_refund_tx = Transaction.objects.filter(
            related_bet_ticket=ticket,
            transaction_type="ticket_deletion_refund",
            status="completed",
        ).latest("timestamp")
        reversal_tx = Transaction.objects.create(
            user=self.cashier,
            initiating_user=self.admin_user,
            transaction_type="ticket_refund_reversal",
            amount=Decimal("100.00"),
            is_successful=True,
            status="completed",
            description=(
                f"Result correction reversal of ticket_deletion_refund for ticket {ticket.ticket_id}. "
                "Fixture 922 result corrected"
            ),
            related_bet_ticket=ticket,
        )
        wallet.apply_delta(
            amount=-Decimal("100.00"),
            actor=self.admin_user,
            transaction_obj=reversal_tx,
            reference=str(ticket.ticket_id),
            reason=reversal_tx.description,
            metadata={
                "ticket_id": ticket.ticket_id,
                "source": "result_backfill",
                "reversed_tx_id": str(original_refund_tx.id),
            },
        )

        wallet.refresh_from_db()
        self.assertEqual(wallet.balance, Decimal("0.00"))
        Wallet.objects.filter(user=self.cashier).update(balance=Decimal("-250.00"))

        call_command("backfill_ticket_refund_reversal_adjustments")
        wallet.refresh_from_db()

        self.assertEqual(wallet.balance, Decimal("-150.00"))
        adjustment_entry = WalletLedgerEntry.objects.get(
            metadata__refund_reversal_adjustment_for=str(reversal_tx.id)
        )
        adjustment_ledger = TicketTransactionLedger.objects.get(wallet_ledger_entry=adjustment_entry)
        self.assertEqual(adjustment_entry.direction, "credit")
        self.assertEqual(adjustment_entry.amount, Decimal("100.00"))
        self.assertEqual(adjustment_ledger.transaction_type, "Ticket Voided")

        call_command("backfill_ticket_refund_reversal_adjustments")
        wallet.refresh_from_db()
        self.assertEqual(wallet.balance, Decimal("-150.00"))
        self.assertEqual(
            WalletLedgerEntry.objects.filter(
                metadata__refund_reversal_adjustment_for=str(reversal_tx.id)
            ).count(),
            1,
        )

    def test_super_agent_dashboard_embeds_ticket_transactions_panel(self):
        self.client.force_login(self.super_agent)

        response = self.client.get(reverse("betting:super_agent_dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Dashboard Ticket Transactions")

    def test_super_agent_dashboard_uses_custom_monthly_commission_period_for_card(self):
        direct_agent = User.objects.create_user(
            email="dashboard-agent@test.com",
            password=self.password,
            user_type="agent",
            username="dashboard_agent",
            super_agent=self.super_agent,
        )
        direct_cashier = User.objects.create_user(
            email="dashboard-cashier@test.com",
            password=self.password,
            user_type="cashier",
            username="dashboard_cashier",
            agent=direct_agent,
            super_agent=self.super_agent,
        )
        Wallet.objects.create(user=direct_agent, balance=Decimal("0.00"))
        Wallet.objects.create(user=direct_cashier, balance=Decimal("0.00"))

        today = timezone.localdate()
        period = CommissionPeriod.objects.create(
            period_type="monthly",
            start_date=today - timedelta(days=2),
            end_date=today + timedelta(days=2),
        )
        NetworkCommissionSettings.objects.create(role="super_agent", commission_percent=Decimal("10.00"))

        ticket = BetTicket.objects.create(
            user=direct_cashier,
            stake_amount=Decimal("1000.00"),
            total_odd=Decimal("2.00"),
            potential_winning=Decimal("2000.00"),
            min_winning=Decimal("0.00"),
            max_winning=Decimal("0.00"),
            status="lost",
            bet_type="single",
        )
        ticket.placed_at = timezone.make_aware(
            datetime.combine(today, datetime.min.time())
        )
        ticket.save(update_fields=["placed_at"])

        self.client.force_login(self.super_agent)
        response = self.client.get(reverse("betting:super_agent_dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Monthly Commission")
        self.assertEqual(response.context["monthly_commission_amount"], Decimal("100.00"))
        self.assertEqual(response.context["monthly_commission_period_start"], period.start_date)
        self.assertEqual(response.context["monthly_commission_period_end"], period.end_date)

    @patch("betting.views._ticket_transaction_filtered_queryset", side_effect=ProgrammingError("relation missing"))
    def test_admin_dashboard_handles_missing_ticket_transaction_table(self, _mock_queryset):
        self.client.force_login(self.admin_user)

        response = self.client.get(reverse("betting:admin_dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Admin Dashboard Overview")

    @patch("betting.views._ticket_transaction_filtered_queryset", side_effect=ProgrammingError("relation missing"))
    def test_retail_dashboard_handles_missing_ticket_transaction_table(self, _mock_queryset):
        self.client.force_login(self.retail_manager)

        response = self.client.get(reverse("betting:retail_dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Retail Manager Dashboard")

    def test_retail_dashboard_renders_for_retail_manager_with_ticket_transaction_widget(self):
        self.client.force_login(self.retail_manager)

        response = self.client.get(reverse("betting:retail_dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Retail Manager Dashboard")
        self.assertContains(response, "Retail Manager Ticket Transactions")

    def test_partial_weekly_commission_payment_closes_record_as_paid(self):
        today = timezone.localdate()
        period = CommissionPeriod.objects.create(
            period_type="weekly",
            start_date=today - timedelta(days=14),
            end_date=today - timedelta(days=8),
        )
        comm = WeeklyAgentCommission.objects.create(
            agent=self.agent,
            period=period,
            commission_total_amount=Decimal("100.00"),
            commission_single_amount=Decimal("40.00"),
            commission_multiple_amount=Decimal("60.00"),
            status="pending",
            amount_paid=Decimal("0.00"),
        )

        ok, _msg = pay_weekly_commission_amount(comm, Decimal("30.00"), actor=self.admin_user)
        self.assertTrue(ok)

        comm.refresh_from_db()
        self.agent.wallet.refresh_from_db()
        self.assertEqual(comm.status, "paid")
        self.assertEqual(comm.amount_paid, Decimal("100.00"))
        self.assertEqual(self.agent.wallet.balance, Decimal("30.00"))

        recalc = calculate_weekly_agent_commission(self.agent, period)
        self.assertIsNotNone(recalc)
        recalc.refresh_from_db()
        self.assertEqual(recalc.status, "paid")
        self.assertEqual(recalc.amount_paid, Decimal("100.00"))

    def test_agent_dashboard_pending_commission_is_only_latest_weekly_period(self):
        today = timezone.localdate()
        older = CommissionPeriod.objects.create(
            period_type="weekly",
            start_date=today - timedelta(days=21),
            end_date=today - timedelta(days=15),
        )
        latest = CommissionPeriod.objects.create(
            period_type="weekly",
            start_date=today - timedelta(days=14),
            end_date=today - timedelta(days=8),
        )
        WeeklyAgentCommission.objects.create(
            agent=self.agent,
            period=older,
            commission_total_amount=Decimal("10.00"),
            commission_single_amount=Decimal("5.00"),
            commission_multiple_amount=Decimal("5.00"),
            status="pending",
            amount_paid=Decimal("0.00"),
        )
        WeeklyAgentCommission.objects.create(
            agent=self.agent,
            period=latest,
            commission_total_amount=Decimal("20.00"),
            commission_single_amount=Decimal("8.00"),
            commission_multiple_amount=Decimal("12.00"),
            status="pending",
            amount_paid=Decimal("0.00"),
        )

        self.client.force_login(self.agent)
        response = self.client.get(reverse("betting:agent_dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["pending_commission"], Decimal("20.00"))
