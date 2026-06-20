from unittest.mock import patch

from django.core.management import call_command
from django.contrib.messages import get_messages
from django.db import models
from django.test import TestCase, Client
from django.urls import reverse
from django.utils import timezone
from betting.models import Loan, LoanPendingCredit, Transaction, User, Wallet
from betting.services.loan_overdraft import (
    apply_repayment_and_credit_wallet,
    build_qualification_snapshot,
    create_manual_overdraft,
    remit_overdraft_pending_credit,
)
from betting.tasks import enforce_due_loans_task
from datetime import timedelta
from decimal import Decimal

class WalletViewTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(
            email='testuser@example.com',
            password='testpassword',
            user_type='player'
        )
        # Ensure no wallet exists initially (though signals might create one, we want to test the view's handling)
        Wallet.objects.filter(user=self.user).delete()

    def test_wallet_view_creates_wallet_if_missing(self):
        self.client.login(username=self.user.username, password='testpassword')
        
        # Verify wallet is missing
        self.assertFalse(Wallet.objects.filter(user=self.user).exists())
        
        # Access wallet view
        response = self.client.get(reverse('betting:wallet'))
        
        # Check for 200 OK (not 404)
        self.assertEqual(response.status_code, 200)
        
        # Verify wallet is created
        self.assertTrue(Wallet.objects.filter(user=self.user).exists())
        wallet = Wallet.objects.get(user=self.user)
        self.assertEqual(wallet.balance, Decimal('0.00'))

    def test_wallet_view_renders_enabled_monnify_button(self):
        self.client.login(username=self.user.username, password='testpassword')

        response = self.client.get(reverse('betting:wallet'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            'button type="submit" name="gateway" value="monnify" class="btn btn-dark rounded-pill px-4 py-2 w-100 d-flex align-items-center justify-content-center"'
        )
        self.assertNotContains(
            response,
            'value="monnify" class="btn btn-dark rounded-pill px-4 py-2 w-100 d-flex align-items-center justify-content-center disabled"'
        )
        self.assertNotContains(
            response,
            'value="monnify" class="btn btn-dark rounded-pill px-4 py-2 w-100 d-flex align-items-center justify-content-center" disabled'
        )

    def test_apply_repayment_reserves_new_credit_when_outstanding_loan_exists(self):
        super_agent = User.objects.create_user(
            email='superagent@example.com',
            password='testpassword',
            user_type='super_agent',
        )
        agent = User.objects.create_user(
            email='agent@example.com',
            password='testpassword',
            user_type='agent',
            super_agent=super_agent,
        )
        Wallet.objects.get_or_create(user=agent, defaults={'balance': Decimal('0.00')})
        loan = Loan.objects.create(
            borrower=agent,
            lender=super_agent,
            amount=Decimal('100.00'),
            requested_amount=Decimal('100.00'),
            qualified_amount=Decimal('100.00'),
            outstanding_balance=Decimal('100.00'),
            status='active',
            loan_type='agent_overdraft',
            approval_level='super_agent',
        )

        result = apply_repayment_and_credit_wallet(
            user=agent,
            amount=Decimal('150.00'),
            source='admin_credit',
            reason='Manual top up',
        )

        loan.refresh_from_db()
        agent.wallet.refresh_from_db()

        self.assertEqual(result['repaid_amount'], Decimal('0.00'))
        self.assertEqual(result['wallet_credit_amount'], Decimal('0.00'))
        self.assertEqual(result['pending_credit_amount'], Decimal('150.00'))
        self.assertEqual(loan.outstanding_balance, Decimal('100.00'))
        self.assertEqual(loan.status, 'active')
        self.assertEqual(agent.wallet.balance, Decimal('0.00'))
        self.assertEqual(
            LoanPendingCredit.objects.filter(borrower=agent).aggregate(total_amount=models.Sum('remaining_amount'))['total_amount'],
            Decimal('150.00'),
        )

    def test_wallet_view_shows_withdrawal_disabled_when_overdraft_exists(self):
        super_agent = User.objects.create_user(
            email='walletsuper@example.com',
            password='testpassword',
            user_type='super_agent',
        )
        agent = User.objects.create_user(
            email='walletagent@example.com',
            password='testpassword',
            user_type='agent',
            super_agent=super_agent,
        )
        Wallet.objects.get_or_create(user=agent, defaults={'balance': Decimal('0.00')})
        Loan.objects.create(
            borrower=agent,
            lender=super_agent,
            amount=Decimal('75.00'),
            requested_amount=Decimal('75.00'),
            qualified_amount=Decimal('75.00'),
            outstanding_balance=Decimal('75.00'),
            status='active',
            loan_type='agent_overdraft',
            approval_level='super_agent',
        )

        self.client.force_login(agent)
        response = self.client.get(reverse('betting:wallet'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Withdrawal Disabled')
        self.assertContains(response, 'Outstanding overdraft must be cleared before withdrawals are permitted.')

    def test_enforce_weekly_loans_command_uses_due_date_locking_flow(self):
        super_agent = User.objects.create_user(
            email='overduesuper@example.com',
            password='testpassword',
            user_type='super_agent',
        )
        agent = User.objects.create_user(
            email='overdueagent@example.com',
            password='testpassword',
            user_type='agent',
            super_agent=super_agent,
        )
        overdue_loan = Loan.objects.create(
            borrower=agent,
            lender=super_agent,
            amount=Decimal('125.00'),
            requested_amount=Decimal('125.00'),
            qualified_amount=Decimal('125.00'),
            outstanding_balance=Decimal('125.00'),
            status='active',
            loan_type='agent_overdraft',
            approval_level='super_agent',
            due_date=timezone.now() - timedelta(days=1),
        )

        call_command('enforce_weekly_loans')

        overdue_loan.refresh_from_db()
        agent.refresh_from_db()

        self.assertEqual(overdue_loan.status, 'overdue')
        self.assertTrue(overdue_loan.account_locked_due_to_default)
        self.assertTrue(agent.is_locked)
        self.assertIn('overdraft/loan obligation', agent.lock_reason.lower())

    def test_enforce_due_loans_task_uses_due_date_locking_flow(self):
        super_agent = User.objects.create_user(
            email='taskoverduesuper@example.com',
            password='testpassword',
            user_type='super_agent',
        )
        agent = User.objects.create_user(
            email='taskoverdueagent@example.com',
            password='testpassword',
            user_type='agent',
            super_agent=super_agent,
        )
        overdue_loan = Loan.objects.create(
            borrower=agent,
            lender=super_agent,
            amount=Decimal('95.00'),
            requested_amount=Decimal('95.00'),
            qualified_amount=Decimal('95.00'),
            outstanding_balance=Decimal('95.00'),
            status='active',
            loan_type='agent_overdraft',
            approval_level='super_agent',
            due_date=timezone.now() - timedelta(minutes=5),
        )

        processed = enforce_due_loans_task()

        overdue_loan.refresh_from_db()
        agent.refresh_from_db()

        self.assertEqual(processed, 1)
        self.assertEqual(overdue_loan.status, 'overdue')
        self.assertTrue(overdue_loan.account_locked_due_to_default)
        self.assertTrue(agent.is_locked)
        self.assertIn('overdraft/loan obligation', agent.lock_reason.lower())

    def test_repayment_auto_unlocks_loan_locked_account_after_settlement(self):
        super_agent = User.objects.create_user(
            email='unlocksuper@example.com',
            password='testpassword',
            user_type='super_agent',
        )
        agent = User.objects.create_user(
            email='unlockagent@example.com',
            password='testpassword',
            user_type='agent',
            super_agent=super_agent,
            is_locked=True,
            lock_reason='Due to an unsettled overdraft/loan obligation, your account has been disabled.',
            failed_login_attempts=3,
        )
        cashier = User.objects.create_user(
            email='unlockcashier@example.com',
            password='testpassword',
            user_type='cashier',
            agent=agent,
            is_locked=True,
            lock_reason='Due to an unsettled overdraft/loan obligation, your account has been disabled.',
            failed_login_attempts=2,
        )
        Wallet.objects.get_or_create(user=agent, defaults={'balance': Decimal('0.00')})
        loan = Loan.objects.create(
            borrower=agent,
            lender=super_agent,
            amount=Decimal('80.00'),
            requested_amount=Decimal('80.00'),
            qualified_amount=Decimal('80.00'),
            outstanding_balance=Decimal('80.00'),
            repaid_amount=Decimal('0.00'),
            status='overdue',
            loan_type='agent_overdraft',
            approval_level='super_agent',
            account_locked_due_to_default=True,
            due_date=timezone.now() - timedelta(days=1),
        )

        reserve_result = apply_repayment_and_credit_wallet(
            user=agent,
            amount=Decimal('80.00'),
            source='gateway_deposit',
            reason='Full repayment',
        )
        result = remit_overdraft_pending_credit(user=agent, actor=agent)

        loan.refresh_from_db()
        agent.refresh_from_db()
        cashier.refresh_from_db()

        self.assertEqual(reserve_result['pending_credit_amount'], Decimal('80.00'))
        self.assertEqual(result['repaid_amount'], Decimal('80.00'))
        self.assertEqual(loan.status, 'settled')
        self.assertTrue(loan.account_unlocked_after_settlement)
        self.assertFalse(agent.is_locked)
        self.assertFalse(cashier.is_locked)
        self.assertEqual(agent.lock_reason, '')
        self.assertEqual(cashier.lock_reason, '')

    def test_finance_manual_deposit_completion_clears_outstanding_loan_first(self):
        finance_user = User.objects.create_user(
            email='financeuser@example.com',
            password='testpassword',
            user_type='finance',
            finance_role='accountant',
        )
        super_agent = User.objects.create_user(
            email='financesuper@example.com',
            password='testpassword',
            user_type='super_agent',
        )
        agent = User.objects.create_user(
            email='financeagent@example.com',
            password='testpassword',
            user_type='agent',
            super_agent=super_agent,
        )
        Wallet.objects.get_or_create(user=finance_user, defaults={'balance': Decimal('0.00')})
        Wallet.objects.get_or_create(user=agent, defaults={'balance': Decimal('0.00')})
        loan = Loan.objects.create(
            borrower=agent,
            lender=super_agent,
            amount=Decimal('100.00'),
            requested_amount=Decimal('100.00'),
            qualified_amount=Decimal('100.00'),
            outstanding_balance=Decimal('100.00'),
            status='active',
            loan_type='agent_overdraft',
            approval_level='super_agent',
        )
        tx = Transaction.objects.create(
            user=agent,
            transaction_type='deposit',
            amount=Decimal('150.00'),
            status='pending',
            is_successful=False,
            payment_gateway='paystack',
            external_reference='finance-manual-complete-ref',
        )

        self.client.force_login(finance_user)
        response = self.client.post(
            reverse('betting:finance_dashboard'),
            {
                'tab': 'reconciliation',
                'deposit_complete': '1',
                'tx_id': str(tx.id),
                'reason': 'Manual completion after reconciliation review',
            },
            follow=True,
        )

        loan.refresh_from_db()
        agent.wallet.refresh_from_db()
        tx.refresh_from_db()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(tx.status, 'completed')
        self.assertTrue(tx.is_successful)
        self.assertEqual(loan.status, 'active')
        self.assertEqual(loan.outstanding_balance, Decimal('100.00'))
        self.assertEqual(agent.wallet.balance, Decimal('0.00'))
        self.assertEqual(
            LoanPendingCredit.objects.filter(borrower=agent).aggregate(total_amount=models.Sum('remaining_amount'))['total_amount'],
            Decimal('150.00'),
        )
        self.assertContains(response, 'Deposit completed. Loan repayment: ₦0.00. Wallet credit: ₦0.00. Reserved new credit: ₦150.00.')

    def test_manual_overdraft_remit_removes_wallet_backed_principal_before_releasing_excess(self):
        admin_user = User.objects.create_user(
            email='manualoverdraftadmin@example.com',
            password='testpassword',
            user_type='admin',
        )
        super_agent = User.objects.create_user(
            email='manualoverdraftsuper@example.com',
            password='testpassword',
            user_type='super_agent',
        )
        agent = User.objects.create_user(
            email='manualoverdraftagent@example.com',
            password='testpassword',
            user_type='agent',
            super_agent=super_agent,
        )
        wallet, _ = Wallet.objects.get_or_create(user=agent, defaults={'balance': Decimal('5000.00')})
        if wallet.balance != Decimal('5000.00'):
            wallet.balance = Decimal('5000.00')
            wallet.save(update_fields=['balance'])

        loan = create_manual_overdraft(
            actor=admin_user,
            borrower=agent,
            amount=Decimal('100000.00'),
            reason='Manual overdraft test',
        )

        agent.wallet.refresh_from_db()
        self.assertEqual(agent.wallet.balance, Decimal('105000.00'))
        self.assertEqual(loan.outstanding_balance, Decimal('100000.00'))

        result = apply_repayment_and_credit_wallet(
            user=agent,
            amount=Decimal('150000.00'),
            source='admin_credit',
            actor=admin_user,
            reason='Manual credit after overdraft assignment',
        )
        remit_result = remit_overdraft_pending_credit(
            user=agent,
            actor=agent,
        )

        loan.refresh_from_db()
        agent.wallet.refresh_from_db()

        self.assertEqual(result['repaid_amount'], Decimal('0.00'))
        self.assertEqual(result['wallet_credit_amount'], Decimal('0.00'))
        self.assertEqual(result['pending_credit_amount'], Decimal('150000.00'))
        self.assertEqual(remit_result['repaid_amount'], Decimal('100000.00'))
        self.assertEqual(remit_result['wallet_credit_amount'], Decimal('50000.00'))
        self.assertEqual(loan.status, 'settled')
        self.assertEqual(loan.outstanding_balance, Decimal('0.00'))
        self.assertEqual(agent.wallet.balance, Decimal('55000.00'))

    def test_paystack_credit_remit_leaves_only_true_excess_in_wallet(self):
        admin_user = User.objects.create_user(
            email='paystackremitadmin@example.com',
            password='testpassword',
            user_type='admin',
        )
        super_agent = User.objects.create_user(
            email='paystackremitsuper@example.com',
            password='testpassword',
            user_type='super_agent',
        )
        agent = User.objects.create_user(
            email='paystackremitagent@example.com',
            password='testpassword',
            user_type='agent',
            super_agent=super_agent,
        )
        wallet, _ = Wallet.objects.get_or_create(user=agent, defaults={'balance': Decimal('10000.00')})
        if wallet.balance != Decimal('10000.00'):
            wallet.balance = Decimal('10000.00')
            wallet.save(update_fields=['balance'])

        loan = create_manual_overdraft(
            actor=admin_user,
            borrower=agent,
            amount=Decimal('100000.00'),
            reason='Paystack remit regression',
        )

        result = apply_repayment_and_credit_wallet(
            user=agent,
            amount=Decimal('120000.00'),
            source='gateway_deposit',
            reason='Paystack wallet funding',
        )
        remit_result = remit_overdraft_pending_credit(
            user=agent,
            actor=agent,
        )

        loan.refresh_from_db()
        agent.wallet.refresh_from_db()

        self.assertEqual(result['pending_credit_amount'], Decimal('120000.00'))
        self.assertEqual(remit_result['repaid_amount'], Decimal('100000.00'))
        self.assertEqual(remit_result['wallet_credit_amount'], Decimal('20000.00'))
        self.assertEqual(loan.status, 'settled')
        self.assertEqual(agent.wallet.balance, Decimal('30000.00'))

    def test_wallet_overdraft_status_api_unlocks_withdraw_after_remittance(self):
        admin_user = User.objects.create_user(
            email='walletstatusadmin@example.com',
            password='testpassword',
            user_type='admin',
        )
        super_agent = User.objects.create_user(
            email='walletstatussuper@example.com',
            password='testpassword',
            user_type='super_agent',
        )
        agent = User.objects.create_user(
            email='walletstatusagent@example.com',
            password='testpassword',
            user_type='agent',
            super_agent=super_agent,
        )
        wallet, _ = Wallet.objects.get_or_create(user=agent, defaults={'balance': Decimal('10000.00')})
        if wallet.balance != Decimal('10000.00'):
            wallet.balance = Decimal('10000.00')
            wallet.save(update_fields=['balance'])

        create_manual_overdraft(
            actor=admin_user,
            borrower=agent,
            amount=Decimal('100000.00'),
            reason='Wallet status payload test',
        )
        apply_repayment_and_credit_wallet(
            user=agent,
            amount=Decimal('120000.00'),
            source='gateway_deposit',
            reason='Reserved gateway deposit for remittance',
        )

        self.client.force_login(agent)
        status_before = self.client.get(reverse('betting:api_wallet_overdraft_status'))
        self.assertEqual(status_before.status_code, 200)
        status_before_payload = status_before.json()
        self.assertTrue(status_before_payload['has_outstanding_loan'])
        self.assertFalse(status_before_payload['can_withdraw_from_wallet'])
        self.assertEqual(status_before_payload['outstanding_overdraft_amount'], '100000.00')
        self.assertEqual(len(status_before_payload['outstanding_loans']), 1)
        self.assertEqual(status_before_payload['qualification_deposit_total'], '0.00')
        self.assertEqual(status_before_payload['qualification_qualified_amount'], '0.00')

        remit_response = self.client.post(reverse('betting:remit_overdraft_pending_credit'))
        self.assertEqual(remit_response.status_code, 200)

        status_after = self.client.get(reverse('betting:api_wallet_overdraft_status'))
        self.assertEqual(status_after.status_code, 200)
        status_after_payload = status_after.json()
        self.assertFalse(status_after_payload['has_outstanding_loan'])
        self.assertTrue(status_after_payload['can_withdraw_from_wallet'])
        self.assertEqual(status_after_payload['outstanding_overdraft_amount'], '0.00')
        self.assertEqual(status_after_payload['outstanding_loans'], [])
        self.assertEqual(status_after_payload['qualification_deposit_total'], '20000.00')
        self.assertEqual(status_after_payload['qualification_qualified_amount'], '10000.00')

    def test_super_agent_wallet_transfer_is_blocked_until_overdraft_is_cleared(self):
        admin_user = User.objects.create_user(
            email='superagenttransferadmin@example.com',
            password='testpassword',
            user_type='admin',
        )
        super_agent = User.objects.create_user(
            email='wallettransfer-super@example.com',
            password='testpassword',
            user_type='super_agent',
            can_manage_downline_wallets=True,
        )
        wallet, _ = Wallet.objects.get_or_create(user=super_agent, defaults={'balance': Decimal('10000.00')})
        if wallet.balance != Decimal('10000.00'):
            wallet.balance = Decimal('10000.00')
            wallet.save(update_fields=['balance'])

        create_manual_overdraft(
            actor=admin_user,
            borrower=super_agent,
            amount=Decimal('100000.00'),
            reason='Super agent transfer gating test',
        )
        apply_repayment_and_credit_wallet(
            user=super_agent,
            amount=Decimal('120000.00'),
            source='gateway_deposit',
            reason='Reserved funding before remittance',
        )

        self.client.force_login(super_agent)
        page_response = self.client.get(reverse('betting:wallet'))
        self.assertEqual(page_response.status_code, 200)
        self.assertContains(page_response, 'Wallet Transfer Disabled')

        status_before = self.client.get(reverse('betting:api_wallet_overdraft_status'))
        self.assertEqual(status_before.status_code, 200)
        status_before_payload = status_before.json()
        self.assertFalse(status_before_payload['can_transfer_from_wallet'])
        self.assertFalse(status_before_payload['can_withdraw_from_wallet'])

        remit_response = self.client.post(reverse('betting:remit_overdraft_pending_credit'))
        self.assertEqual(remit_response.status_code, 200)

        status_after = self.client.get(reverse('betting:api_wallet_overdraft_status'))
        self.assertEqual(status_after.status_code, 200)
        status_after_payload = status_after.json()
        self.assertTrue(status_after_payload['can_transfer_from_wallet'])
        self.assertTrue(status_after_payload['can_withdraw_from_wallet'])

    def test_wallet_overdraft_status_api_includes_recent_transactions(self):
        self.client.force_login(self.user)
        wallet = Wallet.objects.get_or_create(user=self.user, defaults={'balance': Decimal('0.00')})[0]
        wallet.balance = Decimal('5000.00')
        wallet.save(update_fields=['balance'])

        Transaction.objects.create(
            user=self.user,
            transaction_type='deposit',
            amount=Decimal('5000.00'),
            status='completed',
            is_successful=True,
            payment_gateway='paystack',
            external_reference='recent-tx-ref',
            description='Recent wallet funding',
        )

        response = self.client.get(reverse('betting:api_wallet_overdraft_status'))
        self.assertEqual(response.status_code, 200)
        payload = response.json()

        self.assertIn('recent_transactions', payload)
        self.assertEqual(len(payload['recent_transactions']), 1)
        self.assertEqual(payload['recent_transactions'][0]['description'], 'Recent wallet funding')
        self.assertEqual(payload['recent_transactions'][0]['details_url'], reverse('betting:deposit_status', args=['recent-tx-ref']))

    def test_qualification_snapshot_counts_only_gateway_excess_after_remittance(self):
        super_agent = User.objects.create_user(
            email='qualificationsuper@example.com',
            password='testpassword',
            user_type='super_agent',
        )
        agent = User.objects.create_user(
            email='qualificationagent@example.com',
            password='testpassword',
            user_type='agent',
            super_agent=super_agent,
        )
        Wallet.objects.get_or_create(user=agent, defaults={'balance': Decimal('0.00')})
        counted_tx = Transaction.objects.create(
            user=agent,
            transaction_type='deposit',
            amount=Decimal('40000.00'),
            status='completed',
            is_successful=True,
            payment_gateway='paystack',
            external_reference='qualified-deposit-kept',
        )
        loan = Loan.objects.create(
            borrower=agent,
            lender=super_agent,
            amount=Decimal('100000.00'),
            requested_amount=Decimal('100000.00'),
            qualified_amount=Decimal('100000.00'),
            outstanding_balance=Decimal('100000.00'),
            status='active',
            loan_type='agent_overdraft',
            approval_level='super_agent',
        )
        reserved_tx = Transaction.objects.create(
            user=agent,
            transaction_type='deposit',
            amount=Decimal('120000.00'),
            status='completed',
            is_successful=True,
            payment_gateway='paystack',
            external_reference='qualified-deposit-excluded',
        )

        apply_repayment_and_credit_wallet(
            user=agent,
            amount=reserved_tx.amount,
            source='gateway_deposit',
            transaction_obj=reserved_tx,
            reason='Gateway deposit reserved for overdraft remittance',
        )

        snapshot_before_remit = build_qualification_snapshot(agent)
        remit_result = remit_overdraft_pending_credit(user=agent, actor=agent)
        snapshot_after_remit = build_qualification_snapshot(agent)

        counted_tx.refresh_from_db()
        loan.refresh_from_db()
        pending_credit = LoanPendingCredit.objects.get(borrower=agent, source_transaction=reserved_tx)

        self.assertEqual(snapshot_before_remit.deposit_total, Decimal('40000.00'))
        self.assertEqual(snapshot_before_remit.qualified_amount, Decimal('20000.00'))
        self.assertEqual(remit_result['wallet_credit_amount'], Decimal('20000.00'))
        self.assertEqual(snapshot_after_remit.deposit_total, Decimal('60000.00'))
        self.assertEqual(snapshot_after_remit.qualified_amount, Decimal('30000.00'))
        self.assertEqual((pending_credit.metadata or {}).get('qualified_deposit_excess_amount'), '20000.00')

    @patch('betting.views.requests.get')
    @patch('betting.views.requests.post')
    def test_verify_monnify_pending_status_does_not_claim_payment_received(self, mock_post, mock_get):
        self.client.login(username=self.user.username, password='testpassword')
        reference = 'monnify-pending-ref'
        Transaction.objects.create(
            user=self.user,
            transaction_type='deposit',
            amount=Decimal('5000.00'),
            status='pending',
            payment_gateway='monnify',
            external_reference=reference,
        )

        mock_post.return_value.json.return_value = {
            'requestSuccessful': True,
            'responseBody': {'accessToken': 'test-token'},
        }
        mock_post.return_value.status_code = 200
        mock_get.return_value.json.return_value = {
            'requestSuccessful': True,
            'responseMessage': 'Transaction is pending',
            'responseBody': {'paymentStatus': 'PENDING'},
        }
        mock_get.return_value.status_code = 200

        response = self.client.get(
            reverse('betting:verify_monnify_deposit'),
            {'paymentReference': reference},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        messages = [message.message for message in get_messages(response.wsgi_request)]
        self.assertIn(
            'Payment is not yet confirmed. If you already completed the payment, your wallet will be credited once Monnify confirms it.',
            messages,
        )
        self.assertNotIn(
            'Payment received, awaiting confirmation. Your wallet will be credited once confirmed.',
            messages,
        )

    @patch('betting.views.requests.get')
    def test_verify_kora_pending_status_does_not_claim_payment_received(self, mock_get):
        self.client.login(username=self.user.username, password='testpassword')
        reference = 'kora-pending-ref'
        Transaction.objects.create(
            user=self.user,
            transaction_type='deposit',
            amount=Decimal('5000.00'),
            status='pending',
            payment_gateway='kora',
            external_reference=reference,
        )

        mock_get.return_value.json.return_value = {
            'status': True,
            'message': 'Transaction is pending',
            'data': {'status': 'pending'},
        }
        mock_get.return_value.status_code = 200

        response = self.client.get(
            reverse('betting:verify_kora_deposit'),
            {'reference': reference},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        messages = [message.message for message in get_messages(response.wsgi_request)]
        self.assertIn(
            'Payment is not yet confirmed. If you already completed the payment, your wallet will be credited once Kora confirms it.',
            messages,
        )
        self.assertNotIn(
            'Payment received, awaiting confirmation. Your wallet will be credited once confirmed.',
            messages,
        )
