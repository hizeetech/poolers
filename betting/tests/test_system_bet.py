from django.test import TestCase, Client
from django.contrib.auth import get_user_model
from django.utils import timezone
from betting.models import BettingPeriod, Fixture, Wallet, BetTicket
import json
from decimal import Decimal
import datetime

User = get_user_model()

class SystemBetTestCase(TestCase):
    def setUp(self):
        # Create User
        self.user = User.objects.create_user(
            email='test@example.com',
            password='password123',
            user_type='player'
        )
        # Fund Wallet
        self.wallet = Wallet.objects.create(user=self.user, balance=Decimal('1000.00'))
        
        # Create Betting Period
        self.period = BettingPeriod.objects.create(
            name='Week 1',
            start_date=timezone.now().date(),
            end_date=timezone.now().date() + datetime.timedelta(days=7),
            is_active=True
        )
        
        # Create Fixtures
        self.fixture1 = Fixture.objects.create(
            betting_period=self.period,
            home_team='Team A',
            away_team='Team B',
            match_date=timezone.now().date() + datetime.timedelta(days=1),
            match_time=datetime.time(14, 0),
            home_win_odd=1.5,
            draw_odd=3.0,
            away_win_odd=2.5,
            status='pending'
        )
        self.fixture2 = Fixture.objects.create(
            betting_period=self.period,
            home_team='Team C',
            away_team='Team D',
            match_date=timezone.now().date() + datetime.timedelta(days=1),
            match_time=datetime.time(16, 0),
            home_win_odd=1.8,
            draw_odd=3.2,
            away_win_odd=2.1,
            status='pending'
        )
        self.fixture3 = Fixture.objects.create(
            betting_period=self.period,
            home_team='Team E',
            away_team='Team F',
            match_date=timezone.now().date() + datetime.timedelta(days=1),
            match_time=datetime.time(18, 0),
            home_win_odd=2.0,
            draw_odd=3.1,
            away_win_odd=2.8,
            status='pending'
        )
        
        self.client = Client()
        self.client.login(email='test@example.com', password='password123')

    def test_system_bet_placement(self):
        # Prepare Selections (3 fixtures)
        selections = [
            {'fixtureId': self.fixture1.id, 'outcome': 'home_win', 'odd': 1.5},
            {'fixtureId': self.fixture2.id, 'outcome': 'draw', 'odd': 3.2},
            {'fixtureId': self.fixture3.id, 'outcome': 'away_win', 'odd': 2.8},
        ]
        
        # System Bet: 2 of 3 (3 combinations)
        # Combos: (F1, F2), (F1, F3), (F2, F3)
        # Stake per line: 100
        # Total Stake: 300
        
        data = {
            'selections': json.dumps(selections),
            'stake_amount': '100',
            'is_system_bet': 'true',
            'permutation_count': '2'
        }
        
        response = self.client.post('/place-bet/', data)
        
        # Check redirection (success)
        self.assertEqual(response.status_code, 302)
        
        # Check Wallet Deduction
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal('700.00')) # 1000 - 300
        
        # Check Tickets Created
        tickets = BetTicket.objects.filter(user=self.user)
        self.assertEqual(tickets.count(), 3)
        
        # Verify specific tickets
        # Combo 1: F1 (Home) & F2 (Draw) -> Odds: 1.5 * 3.2 = 4.8
        # Combo 2: F1 (Home) & F3 (Away) -> Odds: 1.5 * 2.8 = 4.2
        # Combo 3: F2 (Draw) & F3 (Away) -> Odds: 3.2 * 2.8 = 8.96
        
        odds = sorted([float(t.total_odd) for t in tickets])
        expected_odds = sorted([1.5 * 3.2, 1.5 * 2.8, 3.2 * 2.8])
        
        for o, e in zip(odds, expected_odds):
            self.assertAlmostEqual(o, e, places=2)

    def test_single_bet_placement_with_new_logic(self):
        # Single Bet (Accumulator of 1)
        selections = [
            {'fixtureId': self.fixture1.id, 'outcome': 'home_win', 'odd': 1.5}
        ]
        
        data = {
            'selections': json.dumps(selections),
            'stake_amount': '100',
            'is_system_bet': 'false',
            'permutation_count': '2' # Should be ignored
        }
        
        response = self.client.post('/place-bet/', data)
        self.assertEqual(response.status_code, 302)
        
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal('900.00'))
        
        tickets = BetTicket.objects.filter(user=self.user)
        self.assertEqual(tickets.count(), 1)
        self.assertEqual(tickets.first().total_odd, Decimal('1.50'))

    def test_accumulator_bet_placement(self):
        # Accumulator (2 fixtures, 1 line)
        selections = [
            {'fixtureId': self.fixture1.id, 'outcome': 'home_win', 'odd': 1.5},
            {'fixtureId': self.fixture2.id, 'outcome': 'draw', 'odd': 3.2}
        ]
        
        data = {
            'selections': json.dumps(selections),
            'stake_amount': '100',
            'is_system_bet': 'false', # Not a system bet
        }
        
        response = self.client.post('/place-bet/', data)
        self.assertEqual(response.status_code, 302)
        
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal('900.00'))
        
        tickets = BetTicket.objects.filter(user=self.user)
        self.assertEqual(tickets.count(), 1)
        self.assertAlmostEqual(float(tickets.first().total_odd, 1.5 * 3.2, places=2))
