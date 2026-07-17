from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from betting.models import (
    User,
    Wallet,
    RetailDailyReport,
    RetailAdminComment,
)
from notifications.models import Notification


class RetailDailyReportingTests(TestCase):
    def setUp(self):
        self.password = 'password123'
        self.admin = User.objects.create_user(
            email='admin-retail-report@test.com',
            password=self.password,
            user_type='admin',
            is_staff=True,
            is_superuser=True,
            username='admin_retail_report',
        )
        self.retail_manager = User.objects.create_user(
            email='retail-manager@test.com',
            password=self.password,
            user_type='retail_manager',
            username='retail_manager_one',
        )
        self.retail_manager_two = User.objects.create_user(
            email='retail-manager-two@test.com',
            password=self.password,
            user_type='retail_manager',
            username='retail_manager_two',
        )
        for user in [self.admin, self.retail_manager, self.retail_manager_two]:
            Wallet.objects.get_or_create(user=user, defaults={'balance': Decimal('0.00')})

    def _payload(self, *, report_date=None):
        return {
            'action': 'draft',
            'report_date': (report_date or timezone.localdate()).isoformat(),
            'branch_name': 'Mainland Zone',
            'shops_visited': '4',
            'agents_supported': '8',
            'cashiers_supported': '5',
            'support_calls_made': '7',
            'support_calls_received': '6',
            'whatsapp_followups': '9',
            'escalation_cases': '2',
            'general_notes': 'Retail network support completed.',
            'pending_withdrawals_reviewed': '5',
            'withdrawals_resolved': '4',
            'dormant_accounts_contacted': '6',
            'dormant_accounts_reactivated': '2',
            'agent_complaints_received': '3',
            'agent_complaints_resolved': '2',
            'shop_issues_identified': '4',
            'shop_issues_resolved': '3',
            'new_agents_onboarded': '2',
            'training_sessions_conducted': '1',
            'compliance_checks_completed': '3',
            'terminals_checked': '6',
            'terminals_fixed': '2',
            'stock_requests_handled': '1',
            'marketing_support_requests': '1',
            'field_visit_notes': 'Visited high-volume shops.',
            'total_stake_influenced': '50000.00',
            'estimated_revenue_influenced': '7200.00',
            'commissions_followed_up': '2',
            'fraud_cases_flagged': '1',
            'retention_actions_taken': '3',
            'customers_assisted_to_bet': '7',
            'high_value_players_contacted': '2',
            'inactive_shops_reactivated': '1',
            'positive_feedback': 'Agents liked the quick response.',
            'negative_feedback': 'Some shops need more POS rolls.',
            'recommendations': '<p>Increase field coverage for dormant shops.</p>',
            'support_shop_or_agent[]': ['Shop A'],
            'support_issue[]': ['Withdrawal delay'],
            'support_escalated_to[]': ['Ops Lead'],
            'support_status[]': ['Resolved'],
            'support_remarks[]': ['Handled same day'],
            'campaign_name[]': ['Weekend Shop Drive'],
            'campaign_type[]': ['Activation'],
            'campaign_channel[]': ['field_visit'],
            'campaign_target_count[]': ['30'],
            'campaign_responses[]': ['10'],
            'campaign_conversions[]': ['5'],
            'campaign_revenue_generated[]': ['1500.00'],
            'campaign_remarks[]': ['Strong shop turnout'],
            'challenge_title[]': ['Low internet uptime'],
            'challenge_impact[]': ['Delayed settlements'],
            'challenge_action_taken[]': ['Escalated to tech team'],
            'challenge_status[]': ['Monitoring'],
            'next_day_task[]': ['Visit Lekki shops'],
            'next_day_priority[]': ['high'],
            'next_day_outcome[]': ['Improve support turnaround'],
            'next_day_deadline[]': [timezone.now().strftime('%Y-%m-%dT%H:%M')],
        }

    def test_retail_manager_can_save_draft_and_duplicate_date_is_blocked(self):
        self.client.force_login(self.retail_manager)
        response = self.client.post(reverse('betting:retail_daily_report_create'), self._payload())
        self.assertEqual(response.status_code, 302)

        report = RetailDailyReport.objects.get()
        self.assertEqual(report.status, RetailDailyReport.STATUS.DRAFT)
        self.assertEqual(report.retail_manager, self.retail_manager)
        self.assertEqual(report.support_rows.count(), 1)
        self.assertEqual(report.campaign_rows.count(), 1)

        duplicate_response = self.client.post(reverse('betting:retail_daily_report_create'), self._payload())
        self.assertEqual(duplicate_response.status_code, 200)
        self.assertContains(duplicate_response, 'A retail daily report already exists for this date.')

    def test_retail_manager_submit_notifies_admin_and_cannot_view_other_manager_report(self):
        report = RetailDailyReport.objects.create(
            retail_manager=self.retail_manager_two,
            created_by=self.retail_manager_two,
            updated_by=self.retail_manager_two,
            report_date=timezone.localdate(),
            status=RetailDailyReport.STATUS.SUBMITTED,
        )
        self.client.force_login(self.retail_manager)
        payload = self._payload()
        payload['action'] = 'submit'
        response = self.client.post(reverse('betting:retail_daily_report_create'), payload)
        self.assertEqual(response.status_code, 302)

        saved_report = RetailDailyReport.objects.get(retail_manager=self.retail_manager)
        self.assertEqual(saved_report.status, RetailDailyReport.STATUS.SUBMITTED)
        self.assertTrue(Notification.objects.filter(recipient=self.admin, title='Retail Daily Report Submitted').exists())

        detail_response = self.client.get(reverse('betting:retail_daily_report_detail', args=[report.id]))
        self.assertEqual(detail_response.status_code, 404)

    def test_admin_can_approve_retail_report_and_manager_receives_notification(self):
        report = RetailDailyReport.objects.create(
            retail_manager=self.retail_manager,
            created_by=self.retail_manager,
            updated_by=self.retail_manager,
            report_date=timezone.localdate(),
            status=RetailDailyReport.STATUS.SUBMITTED,
            submitted_at=timezone.now(),
        )
        self.client.force_login(self.admin)
        response = self.client.post(
            reverse('betting:retail_daily_report_action', args=[report.id]),
            {'action': 'approve', 'comment': 'Strong field support today.'},
        )
        self.assertEqual(response.status_code, 302)
        report.refresh_from_db()
        self.assertEqual(report.status, RetailDailyReport.STATUS.APPROVED)
        self.assertEqual(report.reviewed_by, self.admin)
        self.assertTrue(RetailAdminComment.objects.filter(report=report, action='approved').exists())
        self.assertTrue(Notification.objects.filter(recipient=self.retail_manager, title='Retail Daily Report Approved').exists())

    def test_dashboard_export_and_admin_sidebar_route_work(self):
        report_one = RetailDailyReport.objects.create(
            retail_manager=self.retail_manager,
            created_by=self.retail_manager,
            updated_by=self.retail_manager,
            report_date=timezone.localdate(),
            branch_name='Mainland Zone',
            status=RetailDailyReport.STATUS.SUBMITTED,
            submitted_at=timezone.now(),
            shops_visited=3,
            agents_supported=4,
            cashiers_supported=2,
            pending_withdrawals_reviewed=5,
            withdrawals_resolved=4,
            estimated_revenue_influenced=Decimal('2200.00'),
            overall_productivity_score=Decimal('75.00'),
        )
        RetailDailyReport.objects.create(
            retail_manager=self.retail_manager_two,
            created_by=self.retail_manager_two,
            updated_by=self.retail_manager_two,
            report_date=timezone.localdate(),
            branch_name='Island Zone',
            status=RetailDailyReport.STATUS.APPROVED,
            submitted_at=timezone.now(),
            approved_at=timezone.now(),
            overall_productivity_score=Decimal('85.00'),
        )
        self.client.force_login(self.admin)
        dashboard = self.client.get(
            reverse('betting:retail_daily_reports_dashboard'),
            {'range': 'today', 'branch': 'Mainland Zone'},
        )
        self.assertEqual(dashboard.status_code, 200)
        self.assertEqual(dashboard.context['cards']['pending_reports'], 1)
        self.assertEqual(dashboard.context['reports_page'].paginator.count, 1)
        self.assertEqual(dashboard.context['reports_page'].object_list[0].id, report_one.id)

        export = self.client.get(
            reverse('betting:retail_daily_report_export'),
            {'range': 'today', 'branch': 'Mainland Zone', 'format': 'csv'},
        )
        self.assertEqual(export.status_code, 200)
        body = export.content.decode()
        self.assertIn('retail_manager_one', body)
        self.assertIn('Mainland Zone', body)
        self.assertNotIn('Island Zone', body)

        admin_page = self.client.get('/admin/betting/retaildailyreport/')
        self.assertEqual(admin_page.status_code, 200)
