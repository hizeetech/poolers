from datetime import datetime
from django.core.cache import cache
from django.http import HttpResponse
from django.test import TestCase, Client
from django.urls import reverse
from django.utils import timezone
from unittest.mock import patch
from betting.models import User, Wallet, BetTicket, Transaction, UserWithdrawal
from commission.models import CommissionPeriod, WeeklyAgentCommission
from decimal import Decimal

class AccountUserTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.password = 'password123'
        cache.clear()
        
        # Create Account User
        self.account_user = User.objects.create_user(
            email='account_user@test.com', 
            password=self.password, 
            user_type='account_user'
        )
        
        # Create Super Admin
        self.super_admin = User.objects.create_superuser(
            email='superadmin@test.com', 
            password=self.password
        )

        # Create Regular Player
        self.player = User.objects.create_user(
            email='player@test.com', 
            password=self.password, 
            user_type='player'
        )
        
        # Ensure wallets exist
        for user in [self.account_user, self.super_admin, self.player]:
            Wallet.objects.get_or_create(user=user, defaults={'balance': Decimal('0.00')})

    def test_account_user_dashboard_access(self):
        self.client.force_login(self.account_user)
        
        url = reverse('betting:account_user_dashboard')
        response = self.client.get(url)
        
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'betting/account_user_dashboard.html')

    def test_account_user_dashboard_uses_usernames_in_replica_sections(self):
        self.client.force_login(self.account_user)

        agent = User.objects.create_user(
            email='account-agent@test.com',
            password=self.password,
            user_type='agent',
            username='account_agent',
        )
        player = User.objects.create_user(
            email='account-player@test.com',
            password=self.password,
            user_type='player',
            username='account_player',
            agent=agent,
        )
        Wallet.objects.get_or_create(user=agent, defaults={'balance': Decimal('0.00')})
        Wallet.objects.get_or_create(user=player, defaults={'balance': Decimal('0.00')})

        BetTicket.objects.create(
            user=player,
            stake_amount=Decimal('100.00'),
            total_odd=Decimal('2.00'),
            potential_winning=Decimal('200.00'),
            max_winning=Decimal('200.00'),
            status='pending',
            bet_type='single',
        )
        Transaction.objects.create(
            user=player,
            transaction_type='deposit',
            amount=Decimal('250.00'),
            is_successful=True,
            status='completed',
            description='Account user dashboard test deposit',
        )
        UserWithdrawal.objects.create(
            user=player,
            amount=Decimal('50.00'),
            bank_name='Test Bank',
            account_number='1234567890',
            account_name='Account Player',
            status='completed',
        )
        UserWithdrawal.objects.create(
            user=player,
            amount=Decimal('30.00'),
            bank_name='Test Bank',
            account_number='1234567890',
            account_name='Account Player',
            status='pending',
        )

        response = self.client.get(reverse('betting:account_user_dashboard'), {'section': 'bets'})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f'<option value="{agent.id}">account_agent</option>', html=True)
        self.assertContains(response, '<td>account_player</td>', html=True)
        self.assertContains(response, '<div class="small text-muted">account_player</div>', html=True)

    def test_player_cannot_access_account_user_dashboard(self):
        self.client.force_login(self.player)
        
        url = reverse('betting:account_user_dashboard')
        response = self.client.get(url)
        
        # Should be redirected (likely to login or home) or 403 Forbidden
        # Assuming user_passes_test redirects to login URL if check fails
        self.assertNotEqual(response.status_code, 200)

    def test_super_admin_fund_account_user_access(self):
        self.client.force_login(self.super_admin)
        
        url = reverse('betting:super_admin_fund_account_user')
        response = self.client.get(url)
        
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'betting/super_admin_fund_account_user.html')

    def test_account_user_cannot_access_funding_page(self):
        self.client.force_login(self.account_user)
        
        url = reverse('betting:super_admin_fund_account_user')
        response = self.client.get(url)
        
        # Should be redirected or 403
        self.assertNotEqual(response.status_code, 200)

    def test_account_user_search_supports_username_name_email_phone(self):
        self.client.force_login(self.account_user)
        u = User.objects.create_user(
            email='john.doe@test.com',
            password=self.password,
            user_type='player',
            username='JohnDoe99',
            first_name='John',
            last_name='Doe',
            phone_number='08012345678',
        )
        Wallet.objects.get_or_create(user=u, defaults={'balance': Decimal('0.00')})

        url = reverse('betting:account_user_dashboard')

        resp1 = self.client.post(url, {'search_user': '1', 'search_term': 'JohnDoe99'})
        self.assertEqual(resp1.status_code, 200)
        self.assertContains(resp1, 'john.doe@test.com')

        resp2 = self.client.post(url, {'search_user': '1', 'search_term': 'John Doe'})
        self.assertEqual(resp2.status_code, 200)
        self.assertContains(resp2, 'john.doe@test.com')

        resp3 = self.client.post(url, {'search_user': '1', 'search_term': '08012345678'})
        self.assertEqual(resp3.status_code, 200)
        self.assertContains(resp3, 'john.doe@test.com')

    def test_account_user_dashboard_ngr_uses_paid_weekly_commission_for_period(self):
        self.client.force_login(self.account_user)
        captured_context = {}

        player = User.objects.create_user(
            email='overview-player@test.com',
            password=self.password,
            user_type='player',
        )
        agent = User.objects.create_user(
            email='overview-agent@test.com',
            password=self.password,
            user_type='agent',
        )
        Wallet.objects.get_or_create(user=player, defaults={'balance': Decimal('0.00')})
        Wallet.objects.get_or_create(user=agent, defaults={'balance': Decimal('0.00')})

        period = CommissionPeriod.objects.create(
            period_type='weekly',
            start_date=timezone.datetime(2026, 6, 9).date(),
            end_date=timezone.datetime(2026, 6, 15).date(),
        )
        other_period = CommissionPeriod.objects.create(
            period_type='weekly',
            start_date=timezone.datetime(2026, 6, 2).date(),
            end_date=timezone.datetime(2026, 6, 8).date(),
        )

        placed_at = timezone.make_aware(datetime(2026, 6, 10, 12, 0, 0))
        BetTicket.objects.create(
            user=player,
            stake_amount=Decimal('300.00'),
            total_odd=Decimal('2.00'),
            potential_winning=Decimal('600.00'),
            max_winning=Decimal('100.00'),
            status='won',
            bet_type='single',
            original_selections_count=1,
            placed_at=placed_at,
        )
        WeeklyAgentCommission.objects.create(
            agent=agent,
            period=period,
            total_stake=Decimal('300.00'),
            total_winnings=Decimal('100.00'),
            ggr=Decimal('200.00'),
            commission_total_amount=Decimal('50.00'),
            amount_paid=Decimal('50.00'),
            status='paid',
        )
        WeeklyAgentCommission.objects.create(
            agent=agent,
            period=other_period,
            total_stake=Decimal('500.00'),
            total_winnings=Decimal('250.00'),
            ggr=Decimal('250.00'),
            commission_total_amount=Decimal('999.00'),
            amount_paid=Decimal('999.00'),
            status='paid',
        )

        def fake_render(_request, _template_name, context):
            captured_context.update(context)
            return HttpResponse('ok')

        with patch('betting.views.render', side_effect=fake_render):
            response = self.client.get(
                reverse('betting:account_user_dashboard'),
                {
                    'section': 'overview',
                    'start_date': '2026-06-09',
                    'end_date': '2026-06-15',
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(captured_context['kpis']['ggr'], '200.00')
        self.assertEqual(captured_context['kpis']['total_paid_commission'], '50.00')
        self.assertEqual(captured_context['kpis']['ngr'], '150.00')

    def test_account_user_dashboard_total_withdrawals_uses_request_time_with_date_filter(self):
        self.client.force_login(self.account_user)
        captured_context = {}

        in_range_user = User.objects.create_user(
            email='withdrawal-in-range@test.com',
            password=self.password,
            user_type='player',
        )
        out_of_range_user = User.objects.create_user(
            email='withdrawal-out-range@test.com',
            password=self.password,
            user_type='player',
        )
        Wallet.objects.get_or_create(user=in_range_user, defaults={'balance': Decimal('0.00')})
        Wallet.objects.get_or_create(user=out_of_range_user, defaults={'balance': Decimal('0.00')})

        in_range_withdrawal = UserWithdrawal.objects.create(
            user=in_range_user,
            amount=Decimal('150.00'),
            bank_name='Test Bank',
            account_number='1234567890',
            account_name='In Range Player',
            status='approved',
        )
        out_of_range_withdrawal = UserWithdrawal.objects.create(
            user=out_of_range_user,
            amount=Decimal('90.00'),
            bank_name='Test Bank',
            account_number='1234567890',
            account_name='Out Range Player',
            status='approved',
        )

        UserWithdrawal.objects.filter(pk=in_range_withdrawal.pk).update(
            request_time=timezone.make_aware(datetime(2026, 6, 26, 10, 0, 0)),
            approved_rejected_time=timezone.make_aware(datetime(2026, 6, 28, 9, 0, 0)),
        )
        UserWithdrawal.objects.filter(pk=out_of_range_withdrawal.pk).update(
            request_time=timezone.make_aware(datetime(2026, 6, 25, 18, 0, 0)),
            approved_rejected_time=timezone.make_aware(datetime(2026, 6, 26, 11, 0, 0)),
        )

        def fake_render(_request, _template_name, context):
            captured_context.update(context)
            return HttpResponse('ok')

        with patch('betting.views.render', side_effect=fake_render):
            response = self.client.get(
                reverse('betting:account_user_dashboard'),
                {
                    'section': 'overview',
                    'start_date': '2026-06-26',
                    'end_date': '2026-06-27',
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(captured_context['kpis']['total_withdrawals'], '150.00')
        self.assertEqual(
            captured_context['charts_data']['withdrawal_series'],
            [{'day': '2026-06-26', 'withdrawals': '150.00'}],
        )

    @patch('commission.services.calculate_monthly_network_commission_data', return_value=None)
    @patch('commission.services.calculate_weekly_agent_commission_data', return_value=None)
    def test_account_user_dashboard_pending_commissions_total_matches_visible_rows(
        self,
        _mock_weekly_calc,
        _mock_monthly_calc,
    ):
        self.client.force_login(self.account_user)
        captured_context = {}

        period = CommissionPeriod.objects.create(
            period_type='weekly',
            start_date=timezone.datetime(2026, 6, 23).date(),
            end_date=timezone.datetime(2026, 6, 29).date(),
        )
        agent_one = User.objects.create_user(
            email='pending-agent-one@test.com',
            password=self.password,
            user_type='agent',
            username='pending_agent_one',
        )
        agent_two = User.objects.create_user(
            email='pending-agent-two@test.com',
            password=self.password,
            user_type='agent',
            username='pending_agent_two',
        )
        Wallet.objects.get_or_create(user=agent_one, defaults={'balance': Decimal('0.00')})
        Wallet.objects.get_or_create(user=agent_two, defaults={'balance': Decimal('0.00')})

        WeeklyAgentCommission.objects.create(
            agent=agent_one,
            period=period,
            total_stake=Decimal('1000.00'),
            total_winnings=Decimal('500.00'),
            ggr=Decimal('500.00'),
            commission_total_amount=Decimal('300.00'),
            amount_paid=Decimal('50.00'),
            status='pending',
        )
        WeeklyAgentCommission.objects.create(
            agent=agent_two,
            period=period,
            total_stake=Decimal('800.00'),
            total_winnings=Decimal('300.00'),
            ggr=Decimal('500.00'),
            commission_total_amount=Decimal('200.00'),
            amount_paid=Decimal('20.00'),
            status='approved',
        )

        def fake_render(_request, _template_name, context):
            captured_context.update(context)
            return HttpResponse('ok')

        with patch('betting.views.render', side_effect=fake_render):
            response = self.client.get(
                reverse('betting:account_user_dashboard'),
                {
                    'section': 'commissions',
                    'commission_period': str(period.id),
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(captured_context['pending_commissions_total'], Decimal('430.00'))
        self.assertEqual(len(captured_context['pending_commissions'].object_list), 2)

    def test_admin_dashboard_period_ngr_uses_amount_paid_including_partial_payments(self):
        self.client.force_login(self.super_admin)
        captured_context = {}

        player = User.objects.create_user(
            email='admin-overview-player@test.com',
            password=self.password,
            user_type='player',
        )
        agent_one = User.objects.create_user(
            email='admin-overview-agent-one@test.com',
            password=self.password,
            user_type='agent',
        )
        agent_two = User.objects.create_user(
            email='admin-overview-agent-two@test.com',
            password=self.password,
            user_type='agent',
        )
        Wallet.objects.get_or_create(user=player, defaults={'balance': Decimal('0.00')})
        Wallet.objects.get_or_create(user=agent_one, defaults={'balance': Decimal('0.00')})
        Wallet.objects.get_or_create(user=agent_two, defaults={'balance': Decimal('0.00')})

        period = CommissionPeriod.objects.create(
            period_type='weekly',
            start_date=timezone.datetime(2026, 6, 9).date(),
            end_date=timezone.datetime(2026, 6, 15).date(),
        )

        placed_at = timezone.make_aware(datetime(2026, 6, 10, 12, 0, 0))
        BetTicket.objects.create(
            user=player,
            stake_amount=Decimal('300.00'),
            total_odd=Decimal('2.00'),
            potential_winning=Decimal('600.00'),
            max_winning=Decimal('100.00'),
            status='won',
            bet_type='single',
            original_selections_count=1,
            placed_at=placed_at,
        )
        WeeklyAgentCommission.objects.create(
            agent=agent_one,
            period=period,
            total_stake=Decimal('300.00'),
            total_winnings=Decimal('100.00'),
            ggr=Decimal('200.00'),
            commission_total_amount=Decimal('80.00'),
            amount_paid=Decimal('50.00'),
            status='partially_paid',
        )
        WeeklyAgentCommission.objects.create(
            agent=agent_two,
            period=period,
            total_stake=Decimal('300.00'),
            total_winnings=Decimal('100.00'),
            ggr=Decimal('200.00'),
            commission_total_amount=Decimal('40.00'),
            amount_paid=Decimal('40.00'),
            status='paid',
        )

        def fake_render(_request, _template_name, context):
            captured_context.update(context)
            return HttpResponse('ok')

        with patch('betting.views.render', side_effect=fake_render):
            response = self.client.get(
                reverse('betting:admin_dashboard'),
                {'commission_period_id': str(period.id)},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(captured_context['period_ggr'], Decimal('200.00'))
        self.assertEqual(captured_context['period_commission_paid'], Decimal('90.00'))
        self.assertEqual(captured_context['period_ngr'], Decimal('110.00'))
