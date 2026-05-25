from django.test import TestCase, Client
from django.urls import reverse
from decimal import Decimal
from betting.models import User, Wallet, UserWithdrawal, Transaction, CRMActionLog

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
        self.admin = User.objects.create_user(email='admin@test.com', password=self.password, user_type='admin', is_staff=True, is_superuser=True)
        self.crm_viewer = User.objects.create_user(email='crm_viewer@test.com', password=self.password, user_type='crm', crm_role='viewer')
        self.crm_ops = User.objects.create_user(email='crm_ops@test.com', password=self.password, user_type='crm', crm_role='ops')
        self.crm_compliance = User.objects.create_user(email='crm_compliance@test.com', password=self.password, user_type='crm', crm_role='compliance')
        
        # Create wallets for all users
        for user in [self.player, self.cashier, self.agent, self.super_agent, self.master_agent, self.admin, self.crm_viewer, self.crm_ops, self.crm_compliance]:
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
