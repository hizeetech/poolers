from django.test import TestCase
from betting.models import User, Wallet
from betting.forms import WalletTransferForm
from decimal import Decimal

class AccountUserTransferTest(TestCase):
    def setUp(self):
        self.password = 'password123'
        
        # Create Account User
        self.account_user = User.objects.create_user(
            email='account_user@test.com', 
            password=self.password, 
            user_type='account_user'
        )
        Wallet.objects.create(user=self.account_user, balance=Decimal('1000.00'))
        
        # Create Agent
        self.agent = User.objects.create_user(
            email='agent@test.com', 
            password=self.password, 
            user_type='agent'
        )
        Wallet.objects.create(user=self.agent, balance=Decimal('0.00'))

        # Create Master Agent
        self.master_agent = User.objects.create_user(
            email='master_agent@test.com', 
            password=self.password, 
            user_type='master_agent'
        )
        Wallet.objects.create(user=self.master_agent, balance=Decimal('0.00'))

    def test_account_user_can_transfer_to_agent(self):
        form_data = {
            'recipient_identifier': self.agent.email,
            'amount': '100.00',
            'transaction_type': 'credit'
        }
        form = WalletTransferForm(sender_user=self.account_user, data=form_data)
        self.assertTrue(form.is_valid(), f"Form errors: {form.errors}")
        
        # Verify permissions logic inside clean() passed
        self.assertIn('recipient_user_obj', form.cleaned_data)
        self.assertEqual(form.cleaned_data['recipient_user_obj'], self.agent)

    def test_account_user_can_transfer_to_master_agent(self):
        form_data = {
            'recipient_identifier': self.master_agent.email,
            'amount': '100.00',
            'transaction_type': 'credit'
        }
        form = WalletTransferForm(sender_user=self.account_user, data=form_data)
        self.assertTrue(form.is_valid(), f"Form errors: {form.errors}")

    def test_account_user_cannot_transfer_to_invalid_recipient(self):
        # e.g. another account_user? Or player? 
        # Requirement says: master agent, super agent, agent and cashier.
        # It didn't explicitly forbid players, but logic implies it.
        # Let's test player.
        player = User.objects.create_user(email='player@test.com', password='password', user_type='player')
        Wallet.objects.create(user=player, balance=Decimal('0.00'))
        
        form_data = {
            'recipient_identifier': player.email,
            'amount': '100.00',
            'transaction_type': 'credit'
        }
        form = WalletTransferForm(sender_user=self.account_user, data=form_data)
        self.assertFalse(form.is_valid())
        self.assertIn('recipient_identifier', form.errors)
