import datetime
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

import betting.models as betting_models
from betting.models import BetTicket, BettingPeriod, Fixture, Result, Selection, Wallet


User = get_user_model()


class ResultAdminTests(TestCase):
    def setUp(self):
        self.invalidate_data_version_patcher = patch('uip.services.DashboardService.invalidate_data_version', return_value=1)
        self.get_serial_frequency_patcher = patch('uip.services.DashboardService.get_serial_number_frequency', return_value={})
        self.invalidate_data_version_patcher.start()
        self.get_serial_frequency_patcher.start()
        self.addCleanup(self.invalidate_data_version_patcher.stop)
        self.addCleanup(self.get_serial_frequency_patcher.stop)

        self.password = 'password123'
        self.superadmin = User.objects.create_superuser(
            email='result-admin@test.com',
            password=self.password,
        )
        self.cashier = User.objects.create_user(
            email='result-admin-cashier@test.com',
            password=self.password,
            user_type='cashier',
            username='result_admin_cashier',
        )
        self.wallet, _ = Wallet.objects.get_or_create(user=self.cashier)
        self.period = BettingPeriod.objects.create(
            name=f'Result Admin Period {timezone.now().timestamp()}',
            start_date=timezone.now().date(),
            end_date=timezone.now().date() + datetime.timedelta(days=7),
            is_active=True,
        )
        self.fixture = Fixture.objects.create(
            betting_period=self.period,
            home_team='Admin Alpha',
            away_team='Admin Beta',
            serial_number=7001,
            match_date=timezone.now().date(),
            match_time=datetime.time(18, 0),
            home_win_odd=Decimal('2.00'),
            away_win_odd=Decimal('3.00'),
            draw_odd=Decimal('3.50'),
            status='scheduled',
            is_active=True,
        )
        self.ticket = BetTicket.objects.create(
            user=self.cashier,
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
            fixture_serial_number=str(self.fixture.serial_number),
            fixture_home_team=self.fixture.home_team,
            fixture_away_team=self.fixture.away_team,
            fixture_match_date=self.fixture.match_date,
            fixture_match_time=self.fixture.match_time,
            bet_type='home_win',
            odd_selected=Decimal('2.00'),
        )

    def test_result_change_page_shows_reprocess_button(self):
        self.client.force_login(self.superadmin)
        response = self.client.get(
            reverse('betting_admin:betting_result_change', args=[self.fixture.pk])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Reprocess Affected Tickets')

    def test_superadmin_can_reprocess_affected_tickets_after_result_edit(self):
        self.client.force_login(self.superadmin)

        self.fixture.home_score = 0
        self.fixture.away_score = 1
        self.fixture.status = 'finished'
        self.fixture.save()

        self.ticket.refresh_from_db()
        self.wallet.refresh_from_db()
        self.assertEqual(self.ticket.status, 'lost')
        self.assertEqual(self.wallet.balance, Decimal('0.00'))

        Fixture.objects.filter(pk=self.fixture.pk).update(home_score=2, away_score=0, status='finished')

        response = self.client.post(
            reverse('betting_admin:betting_result_reprocess', args=[self.fixture.pk]),
            follow=True,
        )

        self.ticket.refresh_from_db()
        self.wallet.refresh_from_db()
        result_obj = Result.objects.get(pk=self.fixture.pk)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Reprocessed 1 affected ticket(s)')
        self.assertEqual(self.ticket.status, 'won')
        self.assertTrue(self.ticket.payout_processed)
        self.assertEqual(self.wallet.balance, Decimal('200.00'))
        self.assertEqual(result_obj.home_score, 2)
        self.assertEqual(result_obj.away_score, 0)

    def test_result_save_defers_background_recalc_until_after_commit_when_no_workers(self):
        callbacks = []

        with patch.object(betting_models.sys, 'argv', ['manage.py', 'runserver']), \
             patch('betting.models.cache.get', return_value=False), \
             patch('betting.models.cache.set'), \
             patch('betting.models.transaction.on_commit') as on_commit_mock, \
             patch('betting.models.threading.Thread') as thread_cls:
            on_commit_mock.side_effect = callbacks.append

            self.fixture.home_score = 1
            self.fixture.away_score = 0
            self.fixture.status = 'finished'
            self.fixture.save()

            self.assertEqual(len(callbacks), 1)
            thread_cls.assert_not_called()

            callbacks[0]()

            thread_cls.assert_called_once()
            thread_cls.return_value.start.assert_called_once()
