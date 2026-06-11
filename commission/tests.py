from datetime import date, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from betting.models import BetTicket, User, Wallet
from commission.models import AgentCommissionProfile, CommissionPeriod, CommissionPlan
from commission.services import calculate_weekly_agent_commission, calculate_weekly_agent_commission_data
from commission.tasks import (
    ensure_last_completed_weekly_commission_period_for_date,
    ensure_weekly_commission_period_for_date,
    finalize_last_completed_weekly_commissions,
    get_current_weekly_period_bounds,
    get_last_completed_weekly_period_bounds,
    refresh_weekly_commissions_for_ticket_ids,
)


class WeeklyCommissionPeriodTests(TestCase):
    def test_current_weekly_bounds_match_requested_wednesday_rule(self):
        start_date, end_date = get_current_weekly_period_bounds(date(2026, 6, 10))
        self.assertEqual(start_date, date(2026, 6, 9))
        self.assertEqual(end_date, date(2026, 6, 15))

        next_start, next_end = get_current_weekly_period_bounds(date(2026, 6, 17))
        self.assertEqual(next_start, date(2026, 6, 16))
        self.assertEqual(next_end, date(2026, 6, 22))

    def test_last_completed_weekly_bounds_remain_available_for_processing(self):
        start_date, end_date = get_last_completed_weekly_period_bounds(date(2026, 6, 10))
        self.assertEqual(start_date, date(2026, 6, 2))
        self.assertEqual(end_date, date(2026, 6, 8))

    def test_period_creation_helpers_use_current_and_completed_windows(self):
        current_period, current_created = ensure_weekly_commission_period_for_date(date(2026, 6, 10))
        completed_period, completed_created = ensure_last_completed_weekly_commission_period_for_date(date(2026, 6, 10))

        self.assertTrue(current_created)
        self.assertTrue(completed_created)
        self.assertEqual(current_period.start_date, date(2026, 6, 9))
        self.assertEqual(current_period.end_date, date(2026, 6, 15))
        self.assertEqual(completed_period.start_date, date(2026, 6, 2))
        self.assertEqual(completed_period.end_date, date(2026, 6, 8))

    def test_live_weekly_period_includes_pending_tickets_in_running_totals(self):
        agent = User.objects.create_user(email='live-agent@example.com', password='password123', user_type='agent')
        cashier = User.objects.create_user(
            email='live-cashier@example.com',
            password='password123',
            user_type='cashier',
            agent=agent,
        )
        Wallet.objects.get_or_create(user=cashier)
        plan = CommissionPlan.objects.create(name='Live Weekly Plan', ggr_percent=Decimal('10.00'))
        AgentCommissionProfile.objects.create(user=agent, plan=plan, is_active=True)

        today = timezone.localdate()
        current_period = CommissionPeriod.objects.create(
            period_type='weekly',
            start_date=today,
            end_date=today + timedelta(days=6),
        )
        live_ticket = BetTicket.objects.create(
            user=cashier,
            stake_amount=Decimal('150.00'),
            total_odd=Decimal('2.00'),
            potential_winning=Decimal('300.00'),
            max_winning=Decimal('300.00'),
            status='pending',
            bet_type='single',
            original_selections_count=1,
        )
        live_ticket.placed_at = timezone.now()
        live_ticket.save(update_fields=['placed_at'])

        data = calculate_weekly_agent_commission_data(agent, current_period)
        self.assertIsNotNone(data)
        self.assertTrue(data['is_live_period'])
        self.assertEqual(data['total_stake'], Decimal('150.00'))
        self.assertEqual(data['total_winnings'], Decimal('0.00'))
        self.assertEqual(data['ggr'], Decimal('150.00'))
        self.assertEqual(data['commission_total_amount'], Decimal('15.00'))

    def test_closed_weekly_period_still_excludes_pending_tickets(self):
        agent = User.objects.create_user(email='closed-agent@example.com', password='password123', user_type='agent')
        cashier = User.objects.create_user(
            email='closed-cashier@example.com',
            password='password123',
            user_type='cashier',
            agent=agent,
        )
        Wallet.objects.get_or_create(user=cashier)
        plan = CommissionPlan.objects.create(name='Closed Weekly Plan', ggr_percent=Decimal('10.00'))
        AgentCommissionProfile.objects.create(user=agent, plan=plan, is_active=True)

        today = timezone.localdate()
        closed_period = CommissionPeriod.objects.create(
            period_type='weekly',
            start_date=today - timedelta(days=14),
            end_date=today - timedelta(days=8),
        )
        old_pending_ticket = BetTicket.objects.create(
            user=cashier,
            stake_amount=Decimal('150.00'),
            total_odd=Decimal('2.00'),
            potential_winning=Decimal('300.00'),
            max_winning=Decimal('300.00'),
            status='pending',
            bet_type='single',
            original_selections_count=1,
        )
        old_pending_ticket.placed_at = timezone.now() - timedelta(days=10)
        old_pending_ticket.save(update_fields=['placed_at'])

        data = calculate_weekly_agent_commission_data(agent, closed_period)
        self.assertIsNotNone(data)
        self.assertFalse(data['is_live_period'])
        self.assertEqual(data['total_stake'], Decimal('0.00'))
        self.assertEqual(data['commission_total_amount'], Decimal('0.00'))

    def test_admin_live_fragment_returns_updated_html_for_active_period(self):
        admin_user = User.objects.create_user(
            email='admin-live@example.com',
            password='password123',
            user_type='admin',
            is_staff=True,
            is_superuser=True,
        )
        agent = User.objects.create_user(email='fragment-agent@example.com', password='password123', user_type='agent')
        cashier = User.objects.create_user(
            email='fragment-cashier@example.com',
            password='password123',
            user_type='cashier',
            agent=agent,
        )
        Wallet.objects.get_or_create(user=cashier)
        plan = CommissionPlan.objects.create(name='Fragment Weekly Plan', ggr_percent=Decimal('12.00'))
        AgentCommissionProfile.objects.create(user=agent, plan=plan, is_active=True)

        today = timezone.localdate()
        period = CommissionPeriod.objects.create(
            period_type='weekly',
            start_date=today,
            end_date=today + timedelta(days=6),
        )
        ticket = BetTicket.objects.create(
            user=cashier,
            stake_amount=Decimal('200.00'),
            total_odd=Decimal('2.50'),
            potential_winning=Decimal('500.00'),
            max_winning=Decimal('500.00'),
            status='pending',
            bet_type='single',
            original_selections_count=1,
        )
        ticket.placed_at = timezone.now()
        ticket.save(update_fields=['placed_at'])

        self.client.force_login(admin_user)
        response = self.client.get(
            reverse('admin:commission_weeklyagentcommission_add'),
            {'period_id': period.id, 'live_fragment': '1'},
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['is_live_period'])
        self.assertIn('200.00', payload['results_html'])
        self.assertIn('24.00', payload['results_html'])

    def test_calculate_weekly_agent_commission_persists_record_without_live_view_flag(self):
        agent = User.objects.create_user(email='persist-agent@example.com', password='password123', user_type='agent')
        cashier = User.objects.create_user(
            email='persist-cashier@example.com',
            password='password123',
            user_type='cashier',
            agent=agent,
        )
        Wallet.objects.get_or_create(user=cashier)
        plan = CommissionPlan.objects.create(name='Persist Weekly Plan', ggr_percent=Decimal('10.00'))
        AgentCommissionProfile.objects.create(user=agent, plan=plan, is_active=True)

        today = timezone.localdate()
        period = CommissionPeriod.objects.create(
            period_type='weekly',
            start_date=today,
            end_date=today + timedelta(days=6),
        )
        ticket = BetTicket.objects.create(
            user=cashier,
            stake_amount=Decimal('100.00'),
            total_odd=Decimal('2.00'),
            potential_winning=Decimal('200.00'),
            max_winning=Decimal('200.00'),
            status='pending',
            bet_type='single',
            original_selections_count=1,
        )
        ticket.placed_at = timezone.now()
        ticket.save(update_fields=['placed_at'])

        record = calculate_weekly_agent_commission(agent, period)

        self.assertIsNotNone(record)
        self.assertEqual(record.total_stake, Decimal('100.00'))
        self.assertEqual(record.commission_total_amount, Decimal('10.00'))

    def test_refresh_weekly_commissions_for_ticket_ids_creates_matching_period(self):
        agent = User.objects.create_user(email='refresh-agent@example.com', password='password123', user_type='agent')
        cashier = User.objects.create_user(
            email='refresh-cashier@example.com',
            password='password123',
            user_type='cashier',
            agent=agent,
        )
        Wallet.objects.get_or_create(user=cashier)
        plan = CommissionPlan.objects.create(name='Refresh Weekly Plan', ggr_percent=Decimal('10.00'))
        AgentCommissionProfile.objects.create(user=agent, plan=plan, is_active=True)

        placed_at = timezone.now() - timedelta(days=10)
        ticket = BetTicket.objects.create(
            user=cashier,
            stake_amount=Decimal('150.00'),
            total_odd=Decimal('2.00'),
            potential_winning=Decimal('300.00'),
            max_winning=Decimal('300.00'),
            status='pending',
            bet_type='single',
            original_selections_count=1,
        )
        ticket.placed_at = placed_at
        ticket.save(update_fields=['placed_at'])

        result = refresh_weekly_commissions_for_ticket_ids([str(ticket.id)])
        start_date, end_date = get_current_weekly_period_bounds(placed_at.date())
        period = CommissionPeriod.objects.get(
            period_type='weekly',
            start_date=start_date,
            end_date=end_date,
        )
        record = agent.weekly_commissions.get(period=period)

        self.assertEqual(result['updated'], 1)
        self.assertEqual(result['period_ids'], [period.id])
        self.assertEqual(record.total_stake, Decimal('0.00'))
        self.assertEqual(record.commission_total_amount, Decimal('0.00'))

    def test_finalize_last_completed_weekly_commissions_excludes_pending_tickets(self):
        agent = User.objects.create_user(email='final-agent@example.com', password='password123', user_type='agent')
        cashier = User.objects.create_user(
            email='final-cashier@example.com',
            password='password123',
            user_type='cashier',
            agent=agent,
        )
        Wallet.objects.get_or_create(user=cashier)
        plan = CommissionPlan.objects.create(name='Final Weekly Plan', ggr_percent=Decimal('10.00'))
        AgentCommissionProfile.objects.create(user=agent, plan=plan, is_active=True)

        reference_today = date(2026, 6, 11)
        period_start, period_end = get_last_completed_weekly_period_bounds(reference_today)

        settled_ticket = BetTicket.objects.create(
            user=cashier,
            stake_amount=Decimal('100.00'),
            total_odd=Decimal('2.00'),
            potential_winning=Decimal('200.00'),
            max_winning=Decimal('200.00'),
            status='lost',
            bet_type='single',
            original_selections_count=1,
        )
        settled_ticket.placed_at = timezone.make_aware(datetime(2026, 6, 3, 10, 0, 0))
        settled_ticket.save(update_fields=['placed_at'])

        pending_ticket = BetTicket.objects.create(
            user=cashier,
            stake_amount=Decimal('150.00'),
            total_odd=Decimal('2.00'),
            potential_winning=Decimal('300.00'),
            max_winning=Decimal('300.00'),
            status='pending',
            bet_type='single',
            original_selections_count=1,
        )
        pending_ticket.placed_at = timezone.make_aware(datetime(2026, 6, 4, 10, 0, 0))
        pending_ticket.save(update_fields=['placed_at'])

        with patch('commission.tasks.timezone.localdate', return_value=reference_today):
            result = finalize_last_completed_weekly_commissions.run()

        period = CommissionPeriod.objects.get(
            period_type='weekly',
            start_date=period_start,
            end_date=period_end,
        )
        record = agent.weekly_commissions.get(period=period)

        self.assertTrue(period.is_processed)
        self.assertEqual(result['period_id'], period.id)
        self.assertEqual(record.total_stake, Decimal('100.00'))
        self.assertEqual(record.commission_total_amount, Decimal('10.00'))
