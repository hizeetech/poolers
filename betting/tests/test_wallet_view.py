from unittest.mock import patch

from django.contrib.messages import get_messages
from django.test import TestCase, Client
from django.urls import reverse
from betting.models import Transaction, User, Wallet
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
        self.client.login(email='testuser@example.com', password='testpassword')
        
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
        self.client.login(email='testuser@example.com', password='testpassword')

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

    @patch('betting.views.requests.get')
    @patch('betting.views.requests.post')
    def test_verify_monnify_pending_status_does_not_claim_payment_received(self, mock_post, mock_get):
        self.client.login(email='testuser@example.com', password='testpassword')
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
        self.client.login(email='testuser@example.com', password='testpassword')
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
