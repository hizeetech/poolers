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
            user_type='cashier'
        )
        # Fund Wallet
        self.wallet, _ = Wallet.objects.get_or_create(user=self.user)
        self.wallet.balance = Decimal('1000.00')
        self.wallet.save(update_fields=['balance'])
        
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
            serial_number='1',
            match_date=timezone.now().date() + datetime.timedelta(days=1),
            match_time=datetime.time(14, 0),
            home_win_odd=1.5,
            draw_odd=3.0,
            away_win_odd=2.5,
            home_or_draw_odd=1.20,
            either_team_win_odd=1.35,
            away_or_draw_odd=1.30,
            status='scheduled'
        )
        self.fixture2 = Fixture.objects.create(
            betting_period=self.period,
            home_team='Team C',
            away_team='Team D',
            serial_number='2',
            match_date=timezone.now().date() + datetime.timedelta(days=1),
            match_time=datetime.time(16, 0),
            home_win_odd=1.8,
            draw_odd=3.2,
            away_win_odd=2.1,
            home_or_draw_odd=1.25,
            either_team_win_odd=1.32,
            away_or_draw_odd=1.28,
            status='scheduled'
        )
        self.fixture3 = Fixture.objects.create(
            betting_period=self.period,
            home_team='Team E',
            away_team='Team F',
            serial_number='3',
            match_date=timezone.now().date() + datetime.timedelta(days=1),
            match_time=datetime.time(18, 0),
            home_win_odd=2.0,
            draw_odd=3.1,
            away_win_odd=2.8,
            home_or_draw_odd=1.30,
            either_team_win_odd=1.40,
            away_or_draw_odd=1.24,
            status='scheduled'
        )
        
        self.client = Client()
        self.client.force_login(self.user)

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
        
        # Check response (success JSON)
        self.assertEqual(response.status_code, 200)
        json_response = response.json()
        self.assertTrue(json_response['success'], msg=json_response.get('message'))
        
        # Check Wallet Deduction
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal('700.00')) # 1000 - 300
        
        # Check Tickets Created
        tickets = BetTicket.objects.filter(user=self.user)
        self.assertEqual(tickets.count(), 1)
        ticket = tickets.first()
        self.assertEqual(ticket.bet_type, 'system')
        
        # Verify max winning
        # Combo 1: F1 (Home) & F2 (Draw) -> Odds: 1.5 * 3.2 = 4.8. Win: 100 * 4.8 = 480
        # Combo 2: F1 (Home) & F3 (Away) -> Odds: 1.5 * 2.8 = 4.2. Win: 100 * 4.2 = 420
        # Combo 3: F2 (Draw) & F3 (Away) -> Odds: 3.2 * 2.8 = 8.96. Win: 100 * 8.96 = 896
        # Total Max Winning = 1796
        
        self.assertAlmostEqual(float(ticket.max_winning), 1796.00, places=2)

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
        self.assertEqual(response.status_code, 200)
        json_response = response.json()
        self.assertTrue(json_response['success'], msg=json_response.get('message'))
        
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
        self.assertEqual(response.status_code, 200)
        json_response = response.json()
        self.assertTrue(json_response['success'], msg=json_response.get('message'))
        
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal('900.00'))
        
        tickets = BetTicket.objects.filter(user=self.user)
        self.assertEqual(tickets.count(), 1)
        self.assertAlmostEqual(float(tickets.first().total_odd), 1.5 * 3.2, places=2)

    def test_ticket_status_updates_when_results_entered(self):
        selections = [
            {'fixtureId': self.fixture1.id, 'outcome': 'home_win', 'odd': 1.5}
        ]

        data = {
            'selections': json.dumps(selections),
            'stake_amount': '100',
            'is_system_bet': 'false',
        }

        response = self.client.post('/place-bet/', data)
        self.assertEqual(response.status_code, 200)
        json_response = response.json()
        self.assertTrue(json_response['success'], msg=json_response.get('message'))

        ticket = BetTicket.objects.get(user=self.user)
        self.assertEqual(ticket.status, 'pending')

        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal('900.00'))

        self.fixture1.home_score = 2
        self.fixture1.away_score = 0
        self.fixture1.status = 'finished'
        self.fixture1.save()

        ticket.refresh_from_db()
        self.wallet.refresh_from_db()

        self.assertEqual(ticket.status, 'won')
        self.assertTrue(ticket.payout_processed)
        self.assertEqual(self.wallet.balance, Decimal('1050.00'))

    def test_double_chance_markets_place_and_auto_settle_from_draw_result(self):
        test_cases = [
            ("home_or_draw", Decimal("1.20"), "won"),
            ("either_team_win", Decimal("1.35"), "lost"),
            ("away_or_draw", Decimal("1.30"), "won"),
        ]

        ticket_ids = []
        for outcome, odd, _expected_status in test_cases:
            response = self.client.post(
                '/place-bet/',
                {
                    'selections': json.dumps([
                        {'fixtureId': self.fixture1.id, 'outcome': outcome, 'odd': float(odd)}
                    ]),
                    'stake_amount': '100',
                    'is_system_bet': 'false',
                },
            )
            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertTrue(payload['success'], msg=payload.get('message'))
            ticket_ids.append(payload['ticket_id'])

        self.fixture1.home_score = 1
        self.fixture1.away_score = 1
        self.fixture1.status = 'finished'
        self.fixture1.save()

        statuses = {
            ticket.ticket_id: ticket.status
            for ticket in BetTicket.objects.filter(ticket_id__in=ticket_ids)
        }
        expected_statuses = {
            ticket_id: expected_status
            for ticket_id, (_outcome, _odd, expected_status) in zip(ticket_ids, test_cases)
        }
        self.assertEqual(statuses, expected_statuses)

    def test_either_team_win_market_wins_when_match_has_a_winner(self):
        response = self.client.post(
            '/place-bet/',
            {
                'selections': json.dumps([
                    {'fixtureId': self.fixture2.id, 'outcome': 'either_team_win', 'odd': 1.32}
                ]),
                'stake_amount': '100',
                'is_system_bet': 'false',
            },
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'], msg=payload.get('message'))

        ticket = BetTicket.objects.get(ticket_id=payload['ticket_id'])
        self.assertEqual(ticket.total_odd, Decimal('1.32'))

        self.fixture2.home_score = 2
        self.fixture2.away_score = 1
        self.fixture2.status = 'finished'
        self.fixture2.save()

        ticket.refresh_from_db()
        self.assertEqual(ticket.status, 'won')

    def test_duplicate_fixture_in_payload_is_rejected(self):
        selections = [
            {'fixtureId': self.fixture1.id, 'outcome': 'home_win', 'odd': 1.5},
            {'fixtureId': self.fixture1.id, 'outcome': 'draw', 'odd': 3.0},
        ]

        data = {
            'selections': json.dumps(selections),
            'stake_amount': '100',
            'is_system_bet': 'false',
        }

        response = self.client.post('/place-bet/', data)
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload.get('success'))

    def test_weekly_commission_multiple_populates_for_multiple_tickets(self):
        from commission.models import CommissionPlan, CommissionPeriod, AgentCommissionProfile
        from commission.services import calculate_weekly_agent_commission_data
        from betting.models import Selection

        agent = User.objects.create_user(
            email='agent1@example.com',
            password='password123',
            user_type='agent'
        )
        cashier = User.objects.create_user(
            email='cashier1@example.com',
            password='password123',
            user_type='cashier',
            agent=agent
        )
        Wallet.objects.get_or_create(user=cashier)

        plan = CommissionPlan.objects.create(
            name='Test Weekly Plan',
            ggr_percent=Decimal('35.00'),
            is_hybrid_active=True,
            enable_single_selection_override=True,
            single_selection_calc_type='percentage_ggr',
            single_selection_value=Decimal('5.00'),
        )
        AgentCommissionProfile.objects.create(user=agent, plan=plan, is_active=True)

        period = CommissionPeriod.objects.create(
            period_type='weekly',
            start_date=timezone.now().date(),
            end_date=timezone.now().date(),
        )

        single_ticket = BetTicket.objects.create(
            user=cashier,
            stake_amount=Decimal('100.00'),
            total_odd=Decimal('1.50'),
            potential_winning=Decimal('150.00'),
            max_winning=Decimal('150.00'),
            status='lost',
            bet_type='single',
            original_selections_count=1,
        )
        Selection.objects.create(
            bet_ticket=single_ticket,
            fixture=self.fixture1,
            betting_period=self.period,
            fixture_serial_number=str(self.fixture1.serial_number),
            fixture_home_team=self.fixture1.home_team,
            fixture_away_team=self.fixture1.away_team,
            fixture_match_date=self.fixture1.match_date,
            fixture_match_time=self.fixture1.match_time,
            bet_type='home_win',
            odd_selected=Decimal('1.50')
        )

        multi_ticket = BetTicket.objects.create(
            user=cashier,
            stake_amount=Decimal('200.00'),
            total_odd=Decimal('3.00'),
            potential_winning=Decimal('600.00'),
            max_winning=Decimal('600.00'),
            status='lost',
            bet_type='multiple',
            original_selections_count=3,
        )
        for fx, bt, odd in [
            (self.fixture1, 'home_win', Decimal('1.50')),
            (self.fixture2, 'draw', Decimal('3.20')),
            (self.fixture3, 'away_win', Decimal('2.80')),
        ]:
            Selection.objects.create(
                bet_ticket=multi_ticket,
                fixture=fx,
                betting_period=self.period,
                fixture_serial_number=str(fx.serial_number),
                fixture_home_team=fx.home_team,
                fixture_away_team=fx.away_team,
                fixture_match_date=fx.match_date,
                fixture_match_time=fx.match_time,
                bet_type=bt,
                odd_selected=odd
            )

        for t in (single_ticket, multi_ticket):
            t.placed_at = timezone.now()
            t.save(update_fields=['placed_at'])

        data = calculate_weekly_agent_commission_data(agent, period, include_breakdown=True)
        self.assertIsNotNone(data)
        self.assertEqual(data['single_ggr'], Decimal('100.00'))
        self.assertEqual(data['commission_single_amount'], Decimal('5.00'))
        self.assertEqual(data['multiple_ggr'], Decimal('200.00'))
        self.assertEqual(data['commission_multiple_amount'], Decimal('70.00'))

    def test_monthly_network_commission_excludes_pending_and_voided_tickets(self):
        from commission.models import CommissionPeriod, NetworkCommissionSettings
        from commission.services import calculate_monthly_network_commission_data

        ma = User.objects.create_user(
            email='ma1@example.com',
            password='password123',
            user_type='master_agent',
        )
        sa = User.objects.create_user(
            email='sa1@example.com',
            password='password123',
            user_type='super_agent',
            master_agent=ma,
        )
        agent = User.objects.create_user(
            email='agent-monthly@example.com',
            password='password123',
            user_type='agent',
            master_agent=ma,
            super_agent=sa,
        )
        cashier = User.objects.create_user(
            email='cashier-monthly@example.com',
            password='password123',
            user_type='cashier',
            agent=agent,
        )
        Wallet.objects.get_or_create(user=cashier)

        NetworkCommissionSettings.objects.create(role='super_agent', commission_percent=Decimal('10.00'))
        NetworkCommissionSettings.objects.create(role='master_agent', commission_percent=Decimal('10.00'))

        today = timezone.localdate()
        period = CommissionPeriod.objects.create(
            period_type='monthly',
            start_date=today - datetime.timedelta(days=1),
            end_date=today + datetime.timedelta(days=1),
        )

        t_pending = BetTicket.objects.create(user=cashier, stake_amount=Decimal('100.00'), max_winning=Decimal('0.00'), status='pending', bet_type='single')
        t_lost = BetTicket.objects.create(user=cashier, stake_amount=Decimal('200.00'), max_winning=Decimal('0.00'), status='lost', bet_type='single')
        t_won = BetTicket.objects.create(user=cashier, stake_amount=Decimal('300.00'), max_winning=Decimal('500.00'), status='won', bet_type='single')
        t_deleted = BetTicket.objects.create(user=cashier, stake_amount=Decimal('400.00'), max_winning=Decimal('0.00'), status='deleted', bet_type='single')
        t_cancelled = BetTicket.objects.create(user=cashier, stake_amount=Decimal('500.00'), max_winning=Decimal('0.00'), status='cancelled', bet_type='single')

        for t in (t_pending, t_lost, t_won, t_deleted, t_cancelled):
            t.placed_at = timezone.now()
            t.save(update_fields=['placed_at'])

        sa_data = calculate_monthly_network_commission_data(sa, period)
        self.assertIsNotNone(sa_data)
        self.assertEqual(sa_data['downline_stake'], Decimal('500.00'))
        self.assertEqual(sa_data['downline_winnings'], Decimal('500.00'))

        ma_data = calculate_monthly_network_commission_data(ma, period)
        self.assertIsNotNone(ma_data)
        self.assertEqual(ma_data['downline_stake'], Decimal('500.00'))
        self.assertEqual(ma_data['downline_winnings'], Decimal('500.00'))
