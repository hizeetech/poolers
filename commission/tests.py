from datetime import date, datetime, timedelta
from decimal import Decimal
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from betting.models import BetTicket, User, Wallet
from commission.models import AgentCommissionProfile, CommissionPeriod, CommissionPlan, NetworkCommissionSettings
from commission.services import (
    calculate_monthly_network_commission_data,
    calculate_weekly_agent_commission,
    calculate_weekly_agent_commission_data,
)
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

    def test_live_weekly_period_excludes_cancelled_and_deleted_tickets(self):
        agent = User.objects.create_user(email='void-live-agent@example.com', password='password123', user_type='agent')
        cashier = User.objects.create_user(
            email='void-live-cashier@example.com',
            password='password123',
            user_type='cashier',
            agent=agent,
        )
        Wallet.objects.get_or_create(user=cashier)
        plan = CommissionPlan.objects.create(name='Void Weekly Plan', ggr_percent=Decimal('10.00'))
        AgentCommissionProfile.objects.create(user=agent, plan=plan, is_active=True)

        today = timezone.localdate()
        current_period = CommissionPeriod.objects.create(
            period_type='weekly',
            start_date=today,
            end_date=today + timedelta(days=6),
        )

        included_ticket = BetTicket.objects.create(
            user=cashier,
            stake_amount=Decimal('200.00'),
            total_odd=Decimal('2.00'),
            potential_winning=Decimal('400.00'),
            max_winning=Decimal('0.00'),
            status='lost',
            bet_type='single',
            original_selections_count=1,
        )
        included_ticket.placed_at = timezone.now()
        included_ticket.save(update_fields=['placed_at'])

        for idx, status in enumerate(['cancelled', 'deleted'], start=1):
            ticket = BetTicket.objects.create(
                user=cashier,
                stake_amount=Decimal('150.00'),
                total_odd=Decimal('2.00'),
                potential_winning=Decimal('300.00'),
                max_winning=Decimal('0.00'),
                status=status,
                bet_type='single',
                original_selections_count=1,
            )
            ticket.placed_at = timezone.now() + timedelta(seconds=idx)
            ticket.save(update_fields=['placed_at'])

        data = calculate_weekly_agent_commission_data(agent, current_period)
        self.assertIsNotNone(data)
        self.assertEqual(data['total_stake'], Decimal('200.00'))
        self.assertEqual(data['ggr'], Decimal('200.00'))
        self.assertEqual(data['commission_total_amount'], Decimal('20.00'))

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

    def test_monthly_network_commission_excludes_cancelled_and_deleted_tickets(self):
        master_agent = User.objects.create_user(
            email='master-commission@example.com',
            password='password123',
            user_type='master_agent',
        )
        super_agent = User.objects.create_user(
            email='super-commission@example.com',
            password='password123',
            user_type='super_agent',
            master_agent=master_agent,
        )
        agent = User.objects.create_user(
            email='agent-commission@example.com',
            password='password123',
            user_type='agent',
            super_agent=super_agent,
            master_agent=master_agent,
        )
        cashier = User.objects.create_user(
            email='cashier-commission@example.com',
            password='password123',
            user_type='cashier',
            agent=agent,
            super_agent=super_agent,
            master_agent=master_agent,
        )
        Wallet.objects.get_or_create(user=cashier)
        NetworkCommissionSettings.objects.create(role='super_agent', commission_percent=Decimal('10.00'))

        period = CommissionPeriod.objects.create(
            period_type='monthly',
            start_date=date(2026, 6, 1),
            end_date=date(2026, 6, 30),
        )

        included_ticket = BetTicket.objects.create(
            user=cashier,
            stake_amount=Decimal('120.00'),
            total_odd=Decimal('2.00'),
            potential_winning=Decimal('240.00'),
            max_winning=Decimal('0.00'),
            status='lost',
            bet_type='single',
            original_selections_count=1,
        )
        included_ticket.placed_at = timezone.make_aware(datetime(2026, 6, 5, 9, 0, 0))
        included_ticket.save(update_fields=['placed_at'])

        for idx, status in enumerate(['cancelled', 'deleted'], start=1):
            ticket = BetTicket.objects.create(
                user=cashier,
                stake_amount=Decimal('80.00'),
                total_odd=Decimal('2.00'),
                potential_winning=Decimal('160.00'),
                max_winning=Decimal('0.00'),
                status=status,
                bet_type='single',
                original_selections_count=1,
            )
            ticket.placed_at = timezone.make_aware(datetime(2026, 6, 5, 9, idx, 0))
            ticket.save(update_fields=['placed_at'])

        data = calculate_monthly_network_commission_data(super_agent, period)
        self.assertIsNotNone(data)
        self.assertEqual(data['downline_stake'], Decimal('120.00'))
        self.assertEqual(data['downline_winnings'], Decimal('0.00'))
        self.assertEqual(data['ngr'], Decimal('120.00'))
        self.assertEqual(data['commission_amount'], Decimal('12.00'))

    def test_recompute_historical_commissions_updates_saved_weekly_records(self):
        agent = User.objects.create_user(email='recompute-weekly-agent@example.com', password='password123', user_type='agent')
        cashier = User.objects.create_user(
            email='recompute-weekly-cashier@example.com',
            password='password123',
            user_type='cashier',
            agent=agent,
        )
        Wallet.objects.get_or_create(user=cashier)
        plan = CommissionPlan.objects.create(name='Recompute Weekly Plan', ggr_percent=Decimal('10.00'))
        AgentCommissionProfile.objects.create(user=agent, plan=plan, is_active=True)

        today = timezone.localdate()
        period = CommissionPeriod.objects.create(
            period_type='weekly',
            start_date=today - timedelta(days=14),
            end_date=today - timedelta(days=8),
        )
        ticket = BetTicket.objects.create(
            user=cashier,
            stake_amount=Decimal('150.00'),
            total_odd=Decimal('2.00'),
            potential_winning=Decimal('300.00'),
            max_winning=Decimal('0.00'),
            status='lost',
            bet_type='single',
            original_selections_count=1,
        )
        ticket.placed_at = timezone.now() - timedelta(days=10)
        ticket.save(update_fields=['placed_at'])

        record = calculate_weekly_agent_commission(agent, period)
        self.assertEqual(record.total_stake, Decimal('150.00'))
        self.assertEqual(record.commission_total_amount, Decimal('15.00'))

        ticket.status = 'cancelled'
        ticket.save(update_fields=['status', 'last_updated'])

        out = StringIO()
        call_command(
            'recompute_historical_commissions',
            '--weekly',
            '--start-date',
            period.start_date.isoformat(),
            '--end-date',
            period.end_date.isoformat(),
            stdout=out,
        )

        record.refresh_from_db()
        self.assertEqual(record.total_stake, Decimal('0.00'))
        self.assertEqual(record.ggr, Decimal('0.00'))
        self.assertEqual(record.commission_total_amount, Decimal('0.00'))
        self.assertIn('Weekly summary: total=1, changed=1', out.getvalue())

    def test_recompute_historical_commissions_updates_saved_monthly_records(self):
        master_agent = User.objects.create_user(
            email='recompute-master@example.com',
            password='password123',
            user_type='master_agent',
        )
        super_agent = User.objects.create_user(
            email='recompute-super@example.com',
            password='password123',
            user_type='super_agent',
            master_agent=master_agent,
        )
        agent = User.objects.create_user(
            email='recompute-agent@example.com',
            password='password123',
            user_type='agent',
            super_agent=super_agent,
            master_agent=master_agent,
        )
        cashier = User.objects.create_user(
            email='recompute-monthly-cashier@example.com',
            password='password123',
            user_type='cashier',
            agent=agent,
            super_agent=super_agent,
            master_agent=master_agent,
        )
        Wallet.objects.get_or_create(user=cashier)
        NetworkCommissionSettings.objects.create(role='super_agent', commission_percent=Decimal('10.00'))

        period = CommissionPeriod.objects.create(
            period_type='monthly',
            start_date=date(2026, 6, 1),
            end_date=date(2026, 6, 30),
        )
        ticket = BetTicket.objects.create(
            user=cashier,
            stake_amount=Decimal('120.00'),
            total_odd=Decimal('2.00'),
            potential_winning=Decimal('240.00'),
            max_winning=Decimal('0.00'),
            status='lost',
            bet_type='single',
            original_selections_count=1,
        )
        ticket.placed_at = timezone.make_aware(datetime(2026, 6, 5, 9, 0, 0))
        ticket.save(update_fields=['placed_at'])

        data = calculate_monthly_network_commission_data(super_agent, period)
        self.assertIsNotNone(data)
        record = super_agent.monthly_commissions.create(period=period, **data)
        self.assertEqual(record.downline_stake, Decimal('120.00'))
        self.assertEqual(record.commission_amount, Decimal('12.00'))

        ticket.status = 'deleted'
        ticket.save(update_fields=['status', 'last_updated'])

        out = StringIO()
        call_command(
            'recompute_historical_commissions',
            '--monthly',
            '--start-date',
            period.start_date.isoformat(),
            '--end-date',
            period.end_date.isoformat(),
            stdout=out,
        )

        record.refresh_from_db()
        self.assertEqual(record.downline_stake, Decimal('0.00'))
        self.assertEqual(record.ngr, Decimal('0.00'))
        self.assertEqual(record.commission_amount, Decimal('0.00'))
        self.assertIn('Monthly summary: total=1, changed=1', out.getvalue())
