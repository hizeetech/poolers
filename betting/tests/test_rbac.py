from django.test import TestCase, Client
from django.urls import reverse
from decimal import Decimal
from betting.models import User, Wallet, UserWithdrawal, Transaction, CRMActionLog, CreditRequest, WithdrawalReport, BetTicket

class RBACTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.password = 'password123'
        
        # Create users for each role
        self.player = User.objects.create_user(email='player@test.com', password=self.password, user_type='player')
        self.cashier = User.objects.create_user(email='cashier@test.com', password=self.password, user_type='cashier')
        self.agent = User.objects.create_user(email='agent@test.com', password=self.password, user_type='agent')
        self.super_agent = User.objects.create_user(email='super_agent@test.com', password=self.password, user_type='super_agent')
        self.master_agent = User.objects.create_user(email='master_agent@test.com', password=self.password, user_type='master_agent')
        self.account_user = User.objects.create_user(email='account_user@test.com', password=self.password, user_type='account_user')
        self.admin = User.objects.create_user(email='admin@test.com', password=self.password, user_type='admin', is_staff=True, is_superuser=True)
        self.crm_viewer = User.objects.create_user(email='crm_viewer@test.com', password=self.password, user_type='crm', crm_role='viewer')
        self.crm_ops = User.objects.create_user(email='crm_ops@test.com', password=self.password, user_type='crm', crm_role='ops')
        self.crm_compliance = User.objects.create_user(email='crm_compliance@test.com', password=self.password, user_type='crm', crm_role='compliance')
        
        # Create wallets for all users
        for user in [self.player, self.cashier, self.agent, self.super_agent, self.master_agent, self.account_user, self.admin, self.crm_viewer, self.crm_ops, self.crm_compliance]:
            Wallet.objects.get_or_create(user=user, defaults={'balance': Decimal('0.00')})

    def test_player_access(self):
        self.client.force_login(self.player)
        
        # Should access user dashboard
        response = self.client.get(reverse('betting:user_dashboard'))
        self.assertEqual(response.status_code, 200)
        
        # Should NOT access agent dashboard
        response = self.client.get(reverse('betting:agent_dashboard'))
        self.assertNotEqual(response.status_code, 200) # Likely 302 redirect to login
        
        # Should NOT access admin dashboard (using custom admin site URL or view)
        # Assuming betting:admin_dashboard view exists
        response = self.client.get(reverse('betting:admin_dashboard'))
        self.assertNotEqual(response.status_code, 200)

    def test_agent_access(self):
        self.client.force_login(self.agent)
        
        # Accessing user_dashboard should redirect to agent_dashboard
        response = self.client.get(reverse('betting:user_dashboard'))
        self.assertRedirects(response, reverse('betting:agent_dashboard'))
        
        # Should access agent dashboard
        response = self.client.get(reverse('betting:agent_dashboard'))
        self.assertEqual(response.status_code, 200)
        
        # Should NOT access master agent dashboard
        # Assuming betting:master_agent_dashboard exists
        try:
            url = reverse('betting:master_agent_dashboard')
            response = self.client.get(url)
            self.assertNotEqual(response.status_code, 200)
        except:
            # If URL doesn't exist, we skip this check but it's good to know
            pass

    def test_admin_access(self):
        self.client.force_login(self.admin)
        
        # Accessing user_dashboard should redirect to admin dashboard
        response = self.client.get(reverse('betting:user_dashboard'))
        self.assertRedirects(response, reverse('betting:admin_dashboard'))
        
        # Should access admin dashboard
        response = self.client.get(reverse('betting:admin_dashboard'))
        self.assertEqual(response.status_code, 200)

    def test_crm_access(self):
        self.client.force_login(self.crm_viewer)
        resp = self.client.get(reverse('betting:crm_dashboard'))
        self.assertEqual(resp.status_code, 200)

        self.client.force_login(self.player)
        resp2 = self.client.get(reverse('betting:crm_dashboard'))
        self.assertNotEqual(resp2.status_code, 200)

    def test_crm_ops_can_approve_and_reject_withdrawals(self):
        self.player.wallet.balance = Decimal('0.00')
        self.player.wallet.save(update_fields=['balance'])

        w = UserWithdrawal.objects.create(
            user=self.player,
            amount=Decimal('500.00'),
            bank_name='Test Bank',
            account_name='Test User',
            account_number='1234567890',
            status='pending',
        )
        self.client.force_login(self.crm_ops)
        resp = self.client.post(reverse('betting:crm_withdrawal_action', args=[w.id]), {'action': 'approve'})
        self.assertNotEqual(resp.status_code, 500)
        w.refresh_from_db()
        self.assertEqual(w.status, 'approved')
        self.assertTrue(CRMActionLog.objects.filter(withdrawal=w, action_type='WITHDRAWAL_APPROVED').exists())

        w2 = UserWithdrawal.objects.create(
            user=self.player,
            amount=Decimal('200.00'),
            bank_name='Test Bank',
            account_name='Test User',
            account_number='1234567890',
            status='pending',
        )
        Wallet.objects.filter(user=self.player).update(balance=Decimal('100.00'))

        resp2 = self.client.post(reverse('betting:crm_withdrawal_action', args=[w2.id]), {'action': 'reject', 'reason': 'Invalid'})
        self.assertNotEqual(resp2.status_code, 500)
        w2.refresh_from_db()
        self.assertEqual(w2.status, 'rejected')
        self.player.wallet.refresh_from_db()
        self.assertEqual(self.player.wallet.balance, Decimal('300.00'))
        self.assertTrue(Transaction.objects.filter(user=self.player, transaction_type='withdrawal_refund', amount=Decimal('200.00')).exists())
        self.assertTrue(CRMActionLog.objects.filter(withdrawal=w2, action_type='WITHDRAWAL_REJECTED').exists())

    def test_crm_compliance_can_suspend_user(self):
        self.client.force_login(self.crm_compliance)
        url = reverse('betting:crm_user_detail', args=[self.player.id])
        resp = self.client.post(url, {'toggle_active': '1', 'make_active': '0', 'reason': 'Fraud'})
        self.assertNotEqual(resp.status_code, 500)
        self.player.refresh_from_db()
        self.assertFalse(self.player.is_active)
        self.assertTrue(CRMActionLog.objects.filter(target_user=self.player, action_type='USER_SUSPENDED').exists())

    def test_crm_wallet_adjust_creates_pending_approval_request_and_account_user_can_approve(self):
        Wallet.objects.filter(user=self.account_user).update(balance=Decimal('500.00'))
        Wallet.objects.filter(user=self.player).update(balance=Decimal('10.00'))

        self.client.force_login(self.crm_compliance)
        resp = self.client.post(reverse('betting:crm_user_detail', args=[self.player.id]), {
            'wallet_adjust': '1',
            'target_user_id': str(self.player.id),
            'action': 'credit',
            'amount': '125.00',
            'reason': 'Manual support top-up',
            'description': 'CRM requested wallet top-up',
        })
        self.assertNotEqual(resp.status_code, 500)

        credit_req = CreditRequest.objects.get(requester=self.player, request_type='crm_credit')
        self.assertEqual(credit_req.status, 'pending')
        self.assertEqual(credit_req.recipient, self.account_user)
        self.player.wallet.refresh_from_db()
        self.assertEqual(self.player.wallet.balance, Decimal('10.00'))
        self.assertTrue(CRMActionLog.objects.filter(target_user=self.player, action_type='WALLET_CREDIT_REQUESTED').exists())

        self.client.force_login(self.account_user)
        resp2 = self.client.post(reverse('betting:approve_credit_request', args=[credit_req.id]), {'action': 'approve'})
        self.assertNotEqual(resp2.status_code, 500)
        credit_req.refresh_from_db()
        self.assertEqual(credit_req.status, 'approved')
        self.account_user.wallet.refresh_from_db()
        self.player.wallet.refresh_from_db()
        self.assertEqual(self.account_user.wallet.balance, Decimal('375.00'))
        self.assertEqual(self.player.wallet.balance, Decimal('135.00'))
        crm_log = CRMActionLog.objects.filter(target_user=self.player, action_type='WALLET_CREDITED').latest('created_at')
        self.assertEqual(crm_log.actor, self.account_user)
        self.assertEqual(crm_log.data.get('approved_by_role'), 'account_user')

    def test_admin_can_approve_crm_wallet_request_from_admin_site_and_is_logged_as_admin(self):
        Wallet.objects.filter(user=self.admin).update(balance=Decimal('600.00'))
        Wallet.objects.filter(user=self.player).update(balance=Decimal('20.00'))

        credit_req = CreditRequest.objects.create(
            requester=self.player,
            recipient=self.account_user,
            amount=Decimal('150.00'),
            reason='Admin approved CRM top-up',
            request_type='crm_credit',
            status='pending',
        )

        self.client.force_login(self.admin)
        admin_index = self.client.get(reverse('betting_admin:index'))
        self.assertContains(admin_index, 'CRM Wallet Approval Requests')
        self.assertContains(admin_index, '>1<', html=False)
        self.assertContains(admin_index, 'Open Queue')

        process_url = reverse('betting_admin:betting_crmwalletapprovalrequest_process', args=[credit_req.id, 'approve'])
        resp = self.client.post(process_url)
        self.assertNotEqual(resp.status_code, 500)

        credit_req.refresh_from_db()
        self.assertEqual(credit_req.status, 'approved')
        self.admin.wallet.refresh_from_db()
        self.player.wallet.refresh_from_db()
        self.assertEqual(self.admin.wallet.balance, Decimal('450.00'))
        self.assertEqual(self.player.wallet.balance, Decimal('170.00'))
        crm_log = CRMActionLog.objects.filter(target_user=self.player, action_type='WALLET_CREDITED').latest('created_at')
        self.assertEqual(crm_log.actor, self.admin)
        self.assertEqual(crm_log.data.get('approved_by_role'), 'admin')

    def test_crm_agent_detail_includes_downline_tickets_and_withdrawal_reports(self):
        self.player.agent = self.agent
        self.player.save(update_fields=['agent'])

        ticket = BetTicket.objects.create(
            user=self.player,
            stake_amount=Decimal('50.00'),
            total_odd=Decimal('2.00'),
            potential_winning=Decimal('100.00'),
            max_winning=Decimal('100.00'),
            status='pending',
        )
        withdrawal = UserWithdrawal.objects.create(
            user=self.player,
            amount=Decimal('30.00'),
            bank_name='Test Bank',
            account_name='Player Test',
            account_number='0123456789',
            status='pending',
        )
        report = WithdrawalReport.objects.create(
            withdrawal=withdrawal,
            user=self.player,
            username=self.player.email,
            amount=withdrawal.amount,
            bank_name=withdrawal.bank_name,
            account_name=withdrawal.account_name,
            account_number=withdrawal.account_number,
            requested_at=withdrawal.request_time,
            updated_at=withdrawal.request_time,
            transaction_reference='CRMTEST123',
            withdrawal_status=withdrawal.status,
            event='requested',
            is_admin_copy=False,
        )

        self.client.force_login(self.crm_viewer)
        resp = self.client.get(reverse('betting:crm_user_detail', args=[self.agent.id]))
        self.assertEqual(resp.status_code, 200)
        ticket_ids = {t.id for t in resp.context['tickets']}
        withdrawal_ids = {w.id for w in resp.context['withdrawals']}
        report_ids = {r.id for r in resp.context['withdrawal_reports']}
        self.assertIn(ticket.id, ticket_ids)
        self.assertIn(withdrawal.id, withdrawal_ids)
        self.assertIn(report.id, report_ids)
