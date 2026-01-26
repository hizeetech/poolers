from django.test import TestCase, Client, RequestFactory
from django.urls import reverse
from betting.models import User, LoginAttempt
from betting.forms import LoginForm

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
            'email': self.user.email,
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
            'email': self.user.email,
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
                'email': self.user.email,
                'password': 'wrongpassword'
            })
            self.user.refresh_from_db()
            self.assertEqual(self.user.failed_login_attempts, i + 1)
        
        # 4th Failed attempt -> Lock
        response = self.client.post(self.login_url, {
            'email': self.user.email,
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
            'email': self.user.email,
            'password': self.password # Correct password
        })
        
        # Should fail with lock message
        self.assertContains(response, "Your account has been locked")
        self.assertFalse(response.wsgi_request.user.is_authenticated)

    def test_attempts_remaining_message(self):
        # 1st fail
        response = self.client.post(self.login_url, {
            'email': self.user.email,
            'password': 'wrongpassword'
        })
        self.assertContains(response, "3 attempts remaining")
        
        # 2nd fail
        response = self.client.post(self.login_url, {
            'email': self.user.email,
            'password': 'wrongpassword'
        })
        self.assertContains(response, "2 attempts remaining")

    def test_non_existent_user_audit(self):
        response = self.client.post(self.login_url, {
            'email': 'ghost@example.com',
            'password': 'any'
        })
        self.assertTrue(LoginAttempt.objects.filter(username_attempted='ghost@example.com', status='failed').exists())
