import datetime
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from betting.models import BetTicket, BettingPeriod, Fixture, Selection, Wallet


User = get_user_model()


class DoubleChanceSettlementTests(TestCase):
    def setUp(self):
        self.invalidate_data_version_patcher = patch('uip.services.DashboardService.invalidate_data_version', return_value=1)
        self.get_serial_frequency_patcher = patch('uip.services.DashboardService.get_serial_number_frequency', return_value={})
        self.invalidate_data_version_patcher.start()
        self.get_serial_frequency_patcher.start()
        self.addCleanup(self.invalidate_data_version_patcher.stop)
        self.addCleanup(self.get_serial_frequency_patcher.stop)

        self.password = 'password123'
        self.user = User.objects.create_user(
            email='dc-settle@test.com',
            password=self.password,
            user_type='agent',
            username='dc_settle_agent',
        )
        self.wallet, _ = Wallet.objects.get_or_create(user=self.user)
        self.wallet.balance = Decimal('10000.00')
        self.wallet.save()

        self.period = BettingPeriod.objects.create(
            name=f'DC Settlement Period {timezone.now().timestamp()}',
            start_date=timezone.now().date(),
            end_date=timezone.now().date() + datetime.timedelta(days=7),
            is_active=True,
        )
        self.fixture = Fixture.objects.create(
            betting_period=self.period,
            home_team='Sydney FC',
            away_team='Melbourne Victory',
            serial_number=9101,
            match_date=timezone.now().date(),
            match_time=datetime.time(18, 0),
            home_win_odd=Decimal('2.20'),
            draw_odd=Decimal('3.30'),
            away_win_odd=Decimal('3.40'),
            home_or_draw_odd=Decimal('1.35'),
            either_team_win_odd=Decimal('1.50'),
            away_or_draw_odd=Decimal('1.45'),
            status='scheduled',
            is_active=True,
        )

    def _make_single_ticket(self, bet_type, odd_value):
        ticket = BetTicket.objects.create(
            user=self.user,
            stake_amount=Decimal('100.00'),
            total_odd=Decimal(str(odd_value)),
            potential_winning=(Decimal('100.00') * Decimal(str(odd_value))).quantize(Decimal('0.01')),
            max_winning=(Decimal('100.00') * Decimal(str(odd_value))).quantize(Decimal('0.01')),
            min_winning=(Decimal('100.00') * Decimal(str(odd_value))).quantize(Decimal('0.01')),
            status='pending',
            bet_type='single',
        )
        Selection.objects.create(
            bet_ticket=ticket,
            fixture=self.fixture,
            betting_period=self.period,
            fixture_serial_number=str(self.fixture.serial_number),
            fixture_home_team=self.fixture.home_team,
            fixture_away_team=self.fixture.away_team,
            fixture_match_date=self.fixture.match_date,
            fixture_match_time=self.fixture.match_time,
            bet_type=bet_type,
            odd_selected=Decimal(str(odd_value)),
        )
        return ticket

    def _settle_fixture(self, home_score, away_score):
        self.fixture.home_score = home_score
        self.fixture.away_score = away_score
        self.fixture.status = 'finished'
        self.fixture.save()

    def test_home_win_outcome_six_bet_types(self):
        t_h = self._make_single_ticket('home_win', '2.20')
        t_d = self._make_single_ticket('draw', '3.30')
        t_a = self._make_single_ticket('away_win', '3.40')
        t_1x = self._make_single_ticket('home_or_draw', '1.35')
        t_12 = self._make_single_ticket('either_team_win', '1.50')
        t_x2 = self._make_single_ticket('away_or_draw', '1.45')

        self._settle_fixture(2, 1)

        for t in [t_h, t_d, t_a, t_1x, t_12, t_x2]:
            t.refresh_from_db()

        self.assertEqual(t_h.status, 'won')
        self.assertEqual(t_d.status, 'lost')
        self.assertEqual(t_a.status, 'lost')
        self.assertEqual(t_1x.status, 'won')
        self.assertEqual(t_12.status, 'won')
        self.assertEqual(t_x2.status, 'lost')

        for t in [t_h, t_d, t_a, t_1x, t_12, t_x2]:
            sel = t.selections.first()
            self.assertIsNotNone(sel)
            t.refresh_from_db()
            sel.refresh_from_db()
            if t.status == 'won':
                self.assertTrue(sel.is_winning_selection)
            elif t.status == 'lost':
                self.assertFalse(sel.is_winning_selection)

    def test_draw_outcome_six_bet_types(self):
        t_h = self._make_single_ticket('home_win', '2.20')
        t_d = self._make_single_ticket('draw', '3.30')
        t_a = self._make_single_ticket('away_win', '3.40')
        t_1x = self._make_single_ticket('home_or_draw', '1.35')
        t_12 = self._make_single_ticket('either_team_win', '1.50')
        t_x2 = self._make_single_ticket('away_or_draw', '1.45')

        self._settle_fixture(1, 1)

        for t in [t_h, t_d, t_a, t_1x, t_12, t_x2]:
            t.refresh_from_db()

        self.assertEqual(t_h.status, 'lost')
        self.assertEqual(t_d.status, 'won')
        self.assertEqual(t_a.status, 'lost')
        self.assertEqual(t_1x.status, 'won')
        self.assertEqual(t_12.status, 'lost')
        self.assertEqual(t_x2.status, 'won')

        for t in [t_h, t_d, t_a, t_1x, t_12, t_x2]:
            sel = t.selections.first()
            self.assertIsNotNone(sel)
            t.refresh_from_db()
            sel.refresh_from_db()
            if t.status == 'won':
                self.assertTrue(sel.is_winning_selection)
            elif t.status == 'lost':
                self.assertFalse(sel.is_winning_selection)

    def test_away_win_outcome_six_bet_types(self):
        t_h = self._make_single_ticket('home_win', '2.20')
        t_d = self._make_single_ticket('draw', '3.30')
        t_a = self._make_single_ticket('away_win', '3.40')
        t_1x = self._make_single_ticket('home_or_draw', '1.35')
        t_12 = self._make_single_ticket('either_team_win', '1.50')
        t_x2 = self._make_single_ticket('away_or_draw', '1.45')

        self._settle_fixture(0, 3)

        for t in [t_h, t_d, t_a, t_1x, t_12, t_x2]:
            t.refresh_from_db()

        self.assertEqual(t_h.status, 'lost')
        self.assertEqual(t_d.status, 'lost')
        self.assertEqual(t_a.status, 'won')
        self.assertEqual(t_1x.status, 'lost')
        self.assertEqual(t_12.status, 'won')
        self.assertEqual(t_x2.status, 'won')

        for t in [t_h, t_d, t_a, t_1x, t_12, t_x2]:
            sel = t.selections.first()
            self.assertIsNotNone(sel)
            t.refresh_from_db()
            sel.refresh_from_db()
            if t.status == 'won':
                self.assertTrue(sel.is_winning_selection)
            elif t.status == 'lost':
                self.assertFalse(sel.is_winning_selection)

    def test_void_status_propagates_to_all_six_bet_types(self):
        tickets = [
            self._make_single_ticket('home_win', '2.20'),
            self._make_single_ticket('draw', '3.30'),
            self._make_single_ticket('away_win', '3.40'),
            self._make_single_ticket('home_or_draw', '1.35'),
            self._make_single_ticket('either_team_win', '1.50'),
            self._make_single_ticket('away_or_draw', '1.45'),
        ]

        self.fixture.status = 'cancelled'
        self.fixture.save()

        for t in tickets:
            t.refresh_from_db()
            sel = t.selections.first()
            sel.refresh_from_db()
            self.assertIsNone(sel.is_winning_selection)
            self.assertIn(t.status, ('voided', 'cancelled'))
