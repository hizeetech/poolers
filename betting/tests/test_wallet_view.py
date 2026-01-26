from django.test import TestCase, Client
from django.urls import reverse
from betting.models import User, Wallet
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
