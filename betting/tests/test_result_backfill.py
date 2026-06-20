from decimal import Decimal
import datetime
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from betting.models import BetTicket, BettingPeriod, Fixture, Selection, Transaction, Wallet


User = get_user_model()


class ResultBackfillTests(TestCase):
    def setUp(self):
        self.invalidate_data_version_patcher = patch('uip.services.DashboardService.invalidate_data_version', return_value=1)
        self.get_serial_frequency_patcher = patch('uip.services.DashboardService.get_serial_number_frequency', return_value={})
        self.invalidate_data_version_patcher.start()
        self.get_serial_frequency_patcher.start()
        self.addCleanup(self.invalidate_data_version_patcher.stop)
        self.addCleanup(self.get_serial_frequency_patcher.stop)

        self.user = User.objects.create_user(
            email='result-backfill@example.com',
            password='password123',
            user_type='cashier',
        )
        self.wallet, _ = Wallet.objects.get_or_create(user=self.user)
        self.period = BettingPeriod.objects.create(
            name='Result Backfill Period',
            start_date=timezone.now().date(),
            end_date=timezone.now().date() + datetime.timedelta(days=7),
            is_active=True,
        )
        self.fixture = Fixture.objects.create(
            betting_period=self.period,
            home_team='Alpha FC',
            away_team='Beta FC',
            serial_number=100,
            match_date=timezone.now().date(),
            match_time=datetime.time(16, 0),
            home_win_odd=Decimal('2.00'),
            away_win_odd=Decimal('3.00'),
            draw_odd=Decimal('3.50'),
            status='scheduled',
            is_active=True,
        )
        self.ticket = BetTicket.objects.create(
            user=self.user,
            stake_amount=Decimal('100.00'),
            total_odd=Decimal('2.00'),
            potential_winning=Decimal('200.00'),
            max_winning=Decimal('200.00'),
            min_winning=Decimal('200.00'),
            status='pending',
            bet_type='single',
        )
        Selection.objects.create(
            bet_ticket=self.ticket,
            fixture=self.fixture,
            betting_period=self.period,
            fixture_serial_number=self.fixture.serial_number,
            fixture_home_team=self.fixture.home_team,
            fixture_away_team=self.fixture.away_team,
            fixture_match_date=self.fixture.match_date,
            fixture_match_time=self.fixture.match_time,
            bet_type='home_win',
            odd_selected=Decimal('2.00'),
        )

    def test_corrected_result_backfills_lost_ticket_to_won_and_pays_wallet(self):
        self.fixture.home_score = 0
        self.fixture.away_score = 1
        self.fixture.status = 'finished'
        self.fixture.save()

        self.ticket.refresh_from_db()
        self.wallet.refresh_from_db()
        self.assertEqual(self.ticket.status, 'lost')
        self.assertEqual(self.wallet.balance, Decimal('0.00'))

        self.fixture.home_score = 2
        self.fixture.away_score = 0
        self.fixture.status = 'finished'
        self.fixture.save()

        self.ticket.refresh_from_db()
        self.wallet.refresh_from_db()
        payout_tx = Transaction.objects.get(
            related_bet_ticket=self.ticket,
            transaction_type='bet_payout',
            status='completed',
        )

        self.assertEqual(self.ticket.status, 'won')
        self.assertTrue(self.ticket.payout_processed)
        self.assertEqual(self.wallet.balance, Decimal('200.00'))
        self.assertEqual(payout_tx.amount, Decimal('200.00'))

    def test_corrected_result_backfills_won_ticket_to_lost_and_reverses_payout(self):
        self.fixture.home_score = 2
        self.fixture.away_score = 0
        self.fixture.status = 'finished'
        self.fixture.save()

        self.ticket.refresh_from_db()
        self.wallet.refresh_from_db()
        original_payout = Transaction.objects.get(
            related_bet_ticket=self.ticket,
            transaction_type='bet_payout',
            status='completed',
        )

        self.assertEqual(self.ticket.status, 'won')
        self.assertEqual(self.wallet.balance, Decimal('200.00'))

        self.fixture.home_score = 0
        self.fixture.away_score = 1
        self.fixture.status = 'finished'
        self.fixture.save()

        self.ticket.refresh_from_db()
        self.wallet.refresh_from_db()
        original_payout.refresh_from_db()
        reversal_tx = Transaction.objects.get(
            related_bet_ticket=self.ticket,
            transaction_type='bet_payout_reversal',
            status='completed',
        )

        self.assertEqual(self.ticket.status, 'lost')
        self.assertFalse(self.ticket.payout_processed)
        self.assertEqual(self.wallet.balance, Decimal('0.00'))
        self.assertEqual(original_payout.status, 'reversed')
        self.assertFalse(original_payout.is_successful)
        self.assertEqual(reversal_tx.amount, Decimal('200.00'))

    def test_result_correction_can_reverse_all_linked_tickets_even_if_wallet_goes_negative(self):
        grouped_fixture = Fixture.objects.create(
            betting_period=self.period,
            home_team='Grouped Alpha',
            away_team='Grouped Beta',
            serial_number=200,
            match_date=timezone.now().date(),
            match_time=datetime.time(18, 0),
            home_win_odd=Decimal('5.00'),
            away_win_odd=Decimal('3.00'),
            draw_odd=Decimal('3.50'),
            status='scheduled',
            is_active=True,
        )

        ticket_specs = [
            (Decimal('20000.00'), Decimal('4.00')),
            (Decimal('30000.00'), Decimal('5.00')),
            (Decimal('10000.00'), Decimal('1.20')),
        ]
        tickets = []
        for stake, odd in ticket_specs:
            ticket = BetTicket.objects.create(
                user=self.user,
                stake_amount=stake,
                total_odd=odd,
                potential_winning=(stake * odd).quantize(Decimal('0.01')),
                max_winning=(stake * odd).quantize(Decimal('0.01')),
                min_winning=(stake * odd).quantize(Decimal('0.01')),
                status='pending',
                bet_type='single',
            )
            Selection.objects.create(
                bet_ticket=ticket,
                fixture=grouped_fixture,
                betting_period=self.period,
                fixture_serial_number=grouped_fixture.serial_number,
                fixture_home_team=grouped_fixture.home_team,
                fixture_away_team=grouped_fixture.away_team,
                fixture_match_date=grouped_fixture.match_date,
                fixture_match_time=grouped_fixture.match_time,
                bet_type='home_win',
                odd_selected=odd,
            )
            tickets.append(ticket)

        grouped_fixture.home_score = 1
        grouped_fixture.away_score = 0
        grouped_fixture.status = 'finished'
        grouped_fixture.save()

        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal('242000.00'))

        self.wallet.balance = Decimal('220000.00')
        self.wallet.save(update_fields=['balance'])

        grouped_fixture.home_score = 0
        grouped_fixture.away_score = 1
        grouped_fixture.status = 'finished'
        grouped_fixture.save()

        self.wallet.refresh_from_db()
        for ticket in tickets:
            ticket.refresh_from_db()

        self.assertEqual(self.wallet.balance, Decimal('-22000.00'))
        self.assertTrue(all(ticket.status == 'lost' for ticket in tickets))
        self.assertEqual(
            Transaction.objects.filter(
                related_bet_ticket__in=tickets,
                transaction_type='bet_payout_reversal',
            ).count(),
            3,
        )
