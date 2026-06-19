from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from betting.models import BetTicket, Transaction, User
from uip.models import FraudAlert
from uip.services import DashboardService


class UIPDashboardServiceTests(TestCase):
    def test_agent_leaderboards_aggregate_downline_without_join_multiplication(self):
        agent = User.objects.create_user(
            email='leader-agent@example.com',
            password='testpass123',
            user_type='agent',
            username='leader_agent',
        )
        cashier = User.objects.create_user(
            email='leader-cashier@example.com',
            password='testpass123',
            user_type='cashier',
            username='leader_cashier',
            agent=agent,
        )

        BetTicket.objects.create(
            user=cashier,
            stake_amount=Decimal('1000.00'),
            max_winning=Decimal('0.00'),
            status='lost',
            bet_type='single',
        )
        won_ticket = BetTicket.objects.create(
            user=cashier,
            stake_amount=Decimal('1500.00'),
            max_winning=Decimal('4000.00'),
            status='won',
            bet_type='single',
        )
        Transaction.objects.create(
            user=cashier,
            transaction_type='deposit',
            amount=Decimal('5000.00'),
            status='completed',
            is_successful=True,
            description='Cashier deposit',
        )

        start_time = timezone.now() - timezone.timedelta(days=1)
        end_time = timezone.now() + timezone.timedelta(days=1)

        data = DashboardService.get_agent_leaderboards(start_time, end_time, limit=10)

        self.assertEqual(len(data['top_turnover']), 1)
        top_turnover = data['top_turnover'][0]
        self.assertEqual(top_turnover.email, agent.email)
        self.assertEqual(top_turnover.total_turnover, Decimal('2500.00'))
        self.assertEqual(top_turnover.tickets_sold, 2)
        self.assertEqual(top_turnover.winnings_paid, Decimal('4000.00'))
        self.assertEqual(top_turnover.total_deposits, Decimal('5000.00'))

        self.assertEqual(len(data['top_deposits']), 1)
        self.assertEqual(data['top_deposits'][0].total_deposits, Decimal('5000.00'))

        self.assertEqual(len(data['top_margin']), 1)
        self.assertEqual(data['top_margin'][0]['agent'].email, agent.email)
        self.assertEqual(data['top_margin'][0]['tickets'], 2)

    def test_uip_dashboard_search_filter_no_longer_errors_without_q_import(self):
        admin_user = User.objects.create_user(
            email='uip-admin@example.com',
            password='testpass123',
            user_type='admin',
            username='uip_admin',
        )
        FraudAlert.objects.create(
            alert_type='multi_account',
            description='Searchable fraud alert',
            severity='high',
        )

        self.client.force_login(admin_user)
        response = self.client.get(reverse('uip:dashboard'), {'search': 'fraud'})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Searchable fraud alert')
