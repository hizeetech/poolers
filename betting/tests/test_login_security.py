from django.test import TestCase, Client, RequestFactory
from django.urls import reverse
from django.utils import timezone
from betting.models import Loan, User, LoginAttempt, Transaction
from betting.services.loan_overdraft import LOAN_LOCK_REASON
from datetime import timedelta
from decimal import Decimal

class LoginSecurityTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.login_url = reverse('betting:login')
        self.password = 'password123'
        self.user = User.objects.create_user(
            email='testuser@example.com',
            password=self.password,
            user_type='player'
        )
        self.factory = RequestFactory()

    def test_failed_login_increments_counter(self):
        response = self.client.post(self.login_url, {
            'identifier': self.user.username,
            'password': 'wrongpassword'
        })
        self.user.refresh_from_db()
        self.assertEqual(self.user.failed_login_attempts, 1)
        self.assertContains(response, "attempts remaining")
        
        # Check Audit Log
        self.assertTrue(LoginAttempt.objects.filter(user=self.user, status='failed').exists())

    def test_successful_login_resets_counter(self):
        # Fail once
        self.user.failed_login_attempts = 1
        self.user.save()
        
        # Login successfully
        response = self.client.post(self.login_url, {
            'identifier': self.user.username,
            'password': self.password
        })
        
        self.user.refresh_from_db()
        self.assertEqual(self.user.failed_login_attempts, 0)
        self.assertIsNone(self.user.last_failed_login)
        
        # Check Audit Log
        self.assertTrue(LoginAttempt.objects.filter(user=self.user, status='success').exists())

    def test_account_lockout(self):
        # 3 Failed attempts
        for i in range(3):
            self.client.post(self.login_url, {
                'identifier': self.user.username,
                'password': 'wrongpassword'
            })
            self.user.refresh_from_db()
            self.assertEqual(self.user.failed_login_attempts, i + 1)
        
        # 4th Failed attempt -> Lock
        response = self.client.post(self.login_url, {
            'identifier': self.user.username,
            'password': 'wrongpassword'
        })
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_locked)
        self.assertIsNotNone(self.user.locked_at)
        self.assertContains(response, "Your account has been locked")
        
        # Check Audit Log
        self.assertTrue(LoginAttempt.objects.filter(user=self.user, status='locked').exists())

    def test_locked_account_cannot_login(self):
        self.user.is_locked = True
        self.user.save()
        
        response = self.client.post(self.login_url, {
            'identifier': self.user.username,
            'password': self.password # Correct password
        })
        
        # Should fail with lock message
        self.assertContains(response, "Your account has been locked")
        self.assertFalse(response.wsgi_request.user.is_authenticated)

    def test_overdraft_locked_account_can_login_when_overdraft_is_not_overdue(self):
        super_agent = User.objects.create_user(
            email="login-unlock-super@example.com",
            password="password123",
            user_type="super_agent",
        )
        agent = User.objects.create_user(
            email="login-unlock-agent@example.com",
            password=self.password,
            user_type="agent",
            super_agent=super_agent,
            is_locked=True,
            lock_reason=LOAN_LOCK_REASON,
        )
        Loan.objects.create(
            borrower=agent,
            lender=super_agent,
            amount=Decimal("70000.00"),
            requested_amount=Decimal("70000.00"),
            qualified_amount=Decimal("70000.00"),
            outstanding_balance=Decimal("70000.00"),
            status="active",
            loan_type="agent_overdraft",
            approval_level="super_agent",
            due_date=timezone.now() + timedelta(days=5),
        )

        response = self.client.post(
            self.login_url,
            {
                "identifier": agent.username,
                "password": self.password,
            },
        )
        agent.refresh_from_db()
        self.assertFalse(agent.is_locked)
        self.assertEqual(response.status_code, 302)

    def test_attempts_remaining_message(self):
        # 1st fail
        response = self.client.post(self.login_url, {
            'identifier': self.user.username,
            'password': 'wrongpassword'
        })
        self.assertContains(response, "3 attempts remaining")
        
        # 2nd fail
        response = self.client.post(self.login_url, {
            'identifier': self.user.username,
            'password': 'wrongpassword'
        })
        self.assertContains(response, "2 attempts remaining")

    def test_non_existent_user_audit(self):
        response = self.client.post(self.login_url, {
            'identifier': 'ghostuser',
            'password': 'any'
        })
        self.assertTrue(LoginAttempt.objects.filter(username_attempted='ghostuser', status='failed').exists())

    def test_login_page_is_not_cached_and_sets_csrf_cookie(self):
        response = self.client.get(self.login_url)
        cache_control = response.headers.get('Cache-Control', '')

        self.assertIn('max-age=0', cache_control)
        self.assertIn('no-cache', cache_control)
        self.assertIn('no-store', cache_control)
        self.assertIn('must-revalidate', cache_control)
        self.assertIn('csrftoken', response.cookies)

    def test_admin_manual_wallet_page_is_not_cached_and_sets_csrf_cookie(self):
        admin_user = User.objects.create_superuser(
            email='admin-wallet@test.com',
            password=self.password,
        )
        self.client.force_login(admin_user)

        response = self.client.get(reverse('betting_admin:admin_manual_wallet_manager'))
        cache_control = response.headers.get('Cache-Control', '')

        self.assertEqual(response.status_code, 200)
        self.assertIn('max-age=0', cache_control)
        self.assertIn('no-cache', cache_control)
        self.assertIn('no-store', cache_control)
        self.assertIn('must-revalidate', cache_control)
        self.assertIn('csrftoken', response.cookies)

    def test_admin_manual_wallet_page_shows_recent_action_bulk_delete_controls(self):
        admin_user = User.objects.create_superuser(
            email='admin-wallet-controls@test.com',
            password=self.password,
        )
        Transaction.objects.create(
            user=self.user,
            initiating_user=admin_user,
            transaction_type='account_user_credit',
            amount=Decimal('500.00'),
            status='completed',
            is_successful=True,
            description='Manual credit log row',
        )
        self.client.force_login(admin_user)

        response = self.client.get(reverse('betting_admin:admin_manual_wallet_manager'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'name="recent_action"', html=False)
        self.assertContains(response, 'value="delete_selected"', html=False)
        self.assertContains(response, 'name="selected_transaction_ids"', html=False)

    def test_admin_manual_wallet_bulk_delete_removes_selected_manual_logs_only(self):
        admin_user = User.objects.create_superuser(
            email='admin-wallet-delete@test.com',
            password=self.password,
        )
        manual_credit = Transaction.objects.create(
            user=self.user,
            initiating_user=admin_user,
            transaction_type='manual_credit',
            amount=Decimal('100.00'),
            status='completed',
            is_successful=True,
            description='Delete this manual credit log',
        )
        manual_debit = Transaction.objects.create(
            user=self.user,
            initiating_user=admin_user,
            transaction_type='manual_debit',
            amount=Decimal('50.00'),
            status='completed',
            is_successful=True,
            description='Delete this manual debit log',
        )
        non_manual = Transaction.objects.create(
            user=self.user,
            initiating_user=admin_user,
            transaction_type='deposit',
            amount=Decimal('75.00'),
            status='completed',
            is_successful=True,
            description='Do not delete this deposit',
        )
        self.client.force_login(admin_user)

        response = self.client.post(
            reverse('betting_admin:admin_manual_wallet_manager'),
            {
                'recent_action': 'delete_selected',
                'apply_recent_action': '1',
                'selected_transaction_ids': [str(manual_credit.id), str(manual_debit.id), str(non_manual.id)],
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(Transaction.objects.filter(id=manual_credit.id).exists())
        self.assertFalse(Transaction.objects.filter(id=manual_debit.id).exists())
        self.assertTrue(Transaction.objects.filter(id=non_manual.id).exists())
        self.assertContains(response, 'Deleted 2 selected manual action log(s).')
