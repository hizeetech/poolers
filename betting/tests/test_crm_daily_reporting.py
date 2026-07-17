from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from betting.models import (
    User,
    Wallet,
    CRMDailyReport,
    CRMAdminComment,
    CRMReportAttachment,
)
from notifications.models import Notification


class CRMDailyReportingTests(TestCase):
    def setUp(self):
        self.password = 'password123'
        self.admin = User.objects.create_user(
            email='admin-crm-report@test.com',
            password=self.password,
            user_type='admin',
            is_staff=True,
            is_superuser=True,
        )
        self.crm_staff = User.objects.create_user(
            email='crm-staff@test.com',
            password=self.password,
            user_type='crm',
            crm_role='viewer',
            username='crm_staff_one',
        )
        self.crm_staff_two = User.objects.create_user(
            email='crm-staff-two@test.com',
            password=self.password,
            user_type='crm',
            crm_role='viewer',
            username='crm_staff_two',
        )
        for user in [self.admin, self.crm_staff, self.crm_staff_two]:
            Wallet.objects.get_or_create(user=user, defaults={'balance': Decimal('0.00')})

    def _payload(self, *, report_date=None):
        return {
            'action': 'draft',
            'report_date': (report_date or timezone.localdate()).isoformat(),
            'branch_name': 'Lagos Desk',
            'calls_made': '14',
            'calls_received': '6',
            'whatsapp_conversations': '8',
            'emails_sent': '3',
            'sms_sent': '2',
            'push_notifications_sent': '1',
            'social_media_responses': '4',
            'general_notes': 'Daily follow-up completed.',
            'complaints_received': '4',
            'complaints_resolved': '3',
            'pending_complaints': '1',
            'escalated_cases': '1',
            'reopened_cases': '0',
            'dormant_customers_contacted': '5',
            'active_customers_followed_up': '4',
            'vip_customers_contacted': '2',
            'welcome_calls': '2',
            'birthday_messages': '1',
            'loyalty_calls': '1',
            'engagement_remarks': 'Good response rate.',
            'dormant_customers_reactivated': '2',
            'returning_customers': '2',
            'customers_retained': '3',
            'high_risk_customers_identified': '1',
            'customers_lost': '1',
            'reason_for_loss': 'Delayed response.',
            'new_registrations': '4',
            'first_time_depositors': '2',
            'repeat_depositors': '1',
            'customers_assisted_to_deposit': '3',
            'customers_assisted_to_place_bets': '2',
            'total_deposits_influenced': '30000.00',
            'estimated_revenue_influenced': '4500.00',
            'agents_contacted': '2',
            'retail_shops_contacted': '1',
            'agent_complaints_resolved': '1',
            'training_conducted': '1',
            'support_visits': '1',
            'positive_feedback': 'Customers liked the campaign.',
            'negative_feedback': 'One customer complained about delay.',
            'customer_suggestions': 'More evening outreach.',
            'recommendations': '<p>Increase outreach to dormant users.</p>',
            'complaint_customer_name[]': ['Ada'],
            'complaint_text[]': ['Login issue'],
            'complaint_escalated_to[]': ['Support Lead'],
            'complaint_status[]': ['Open'],
            'complaint_remarks[]': ['Pending callback'],
            'campaign_name[]': ['July Winback'],
            'campaign_type[]': ['Retention'],
            'campaign_channel[]': ['whatsapp'],
            'campaign_audience_size[]': ['100'],
            'campaign_responses[]': ['20'],
            'campaign_conversions[]': ['10'],
            'campaign_revenue_generated[]': ['2500.00'],
            'campaign_remarks[]': ['Strong conversion'],
            'challenge_title[]': ['Low pickup rate'],
            'challenge_impact[]': ['Reduced calls reached'],
            'challenge_action_taken[]': ['Moved calls to evening'],
            'challenge_status[]': ['Monitoring'],
            'next_day_task[]': ['Call dormant VIP customers'],
            'next_day_priority[]': ['high'],
            'next_day_outcome[]': ['Improve retention'],
            'next_day_deadline[]': [timezone.now().strftime('%Y-%m-%dT%H:%M')],
        }

    def test_crm_staff_can_save_draft_and_duplicate_date_is_blocked(self):
        self.client.force_login(self.crm_staff)
        response = self.client.post(reverse('betting:crm_daily_report_create'), self._payload())
        self.assertEqual(response.status_code, 302)

        report = CRMDailyReport.objects.get()
        self.assertEqual(report.status, CRMDailyReport.STATUS.DRAFT)
        self.assertEqual(report.staff, self.crm_staff)
        self.assertEqual(report.branch_name, 'Lagos Desk')
        self.assertEqual(report.complaint_rows.count(), 1)
        self.assertEqual(report.campaign_rows.count(), 1)

        duplicate_response = self.client.post(reverse('betting:crm_daily_report_create'), self._payload())
        self.assertEqual(duplicate_response.status_code, 200)
        self.assertContains(duplicate_response, 'A daily report already exists for this date.')
        self.assertEqual(CRMDailyReport.objects.count(), 1)

    def test_crm_staff_submit_notifies_admin_and_cannot_view_other_staff_detail(self):
        report = CRMDailyReport.objects.create(
            staff=self.crm_staff_two,
            created_by=self.crm_staff_two,
            updated_by=self.crm_staff_two,
            report_date=timezone.localdate(),
            status=CRMDailyReport.STATUS.SUBMITTED,
        )

        self.client.force_login(self.crm_staff)
        payload = self._payload()
        payload['action'] = 'submit'
        response = self.client.post(reverse('betting:crm_daily_report_create'), payload)
        self.assertEqual(response.status_code, 302)

        saved_report = CRMDailyReport.objects.get(staff=self.crm_staff)
        self.assertEqual(saved_report.status, CRMDailyReport.STATUS.SUBMITTED)
        self.assertTrue(Notification.objects.filter(recipient=self.admin, title='CRM Daily Report Submitted').exists())

        detail_response = self.client.get(reverse('betting:crm_daily_report_detail', args=[report.id]))
        self.assertEqual(detail_response.status_code, 404)

    def test_admin_can_approve_report_and_staff_receives_notification(self):
        report = CRMDailyReport.objects.create(
            staff=self.crm_staff,
            created_by=self.crm_staff,
            updated_by=self.crm_staff,
            report_date=timezone.localdate(),
            status=CRMDailyReport.STATUS.SUBMITTED,
            submitted_at=timezone.now(),
        )
        self.client.force_login(self.admin)
        response = self.client.post(
            reverse('betting:crm_daily_report_action', args=[report.id]),
            {'action': 'approve', 'comment': 'Solid daily execution.'},
        )
        self.assertEqual(response.status_code, 302)

        report.refresh_from_db()
        self.assertEqual(report.status, CRMDailyReport.STATUS.APPROVED)
        self.assertEqual(report.reviewed_by, self.admin)
        self.assertTrue(CRMAdminComment.objects.filter(report=report, action='approved').exists())
        self.assertTrue(Notification.objects.filter(recipient=self.crm_staff, title='CRM Daily Report Approved').exists())

    def test_dashboard_and_export_reflect_filtered_reports(self):
        report_one = CRMDailyReport.objects.create(
            staff=self.crm_staff,
            created_by=self.crm_staff,
            updated_by=self.crm_staff,
            report_date=timezone.localdate(),
            branch_name='Lagos Desk',
            status=CRMDailyReport.STATUS.SUBMITTED,
            submitted_at=timezone.now(),
            calls_made=10,
            calls_received=5,
            complaints_received=4,
            complaints_resolved=3,
            dormant_customers_contacted=5,
            dormant_customers_reactivated=2,
            vip_customers_contacted=1,
            active_customers_followed_up=2,
            new_registrations=2,
            first_time_depositors=1,
            repeat_depositors=1,
            customers_assisted_to_place_bets=1,
            estimated_revenue_influenced=Decimal('2500.00'),
            overall_productivity_score=Decimal('67.50'),
        )
        report_two = CRMDailyReport.objects.create(
            staff=self.crm_staff_two,
            created_by=self.crm_staff_two,
            updated_by=self.crm_staff_two,
            report_date=timezone.localdate(),
            branch_name='Abuja Desk',
            status=CRMDailyReport.STATUS.APPROVED,
            submitted_at=timezone.now(),
            approved_at=timezone.now(),
            overall_productivity_score=Decimal('90.00'),
        )

        self.client.force_login(self.admin)
        dashboard = self.client.get(
            reverse('betting:crm_daily_reports_dashboard'),
            {'range': 'today', 'branch': 'Lagos Desk'},
        )
        self.assertEqual(dashboard.status_code, 200)
        self.assertEqual(dashboard.context['cards']['pending_reports'], 1)
        self.assertEqual(dashboard.context['cards']['approved_reports'], 0)
        self.assertEqual(dashboard.context['reports_page'].paginator.count, 1)
        self.assertEqual(dashboard.context['reports_page'].object_list[0].id, report_one.id)

        export = self.client.get(
            reverse('betting:crm_daily_report_export'),
            {'range': 'today', 'branch': 'Lagos Desk', 'format': 'csv'},
        )
        self.assertEqual(export.status_code, 200)
        self.assertIn('crm_staff_one', export.content.decode())
        self.assertIn('Lagos Desk', export.content.decode())
        self.assertNotIn('Abuja Desk', export.content.decode())

    def test_autosave_returns_json_and_ignores_attachments(self):
        self.client.force_login(self.crm_staff)
        payload = self._payload()
        payload['action'] = 'autosave'
        payload['attachments'] = SimpleUploadedFile('voice-note.mp3', b'voice', content_type='audio/mpeg')
        response = self.client.post(
            reverse('betting:crm_daily_report_create'),
            payload,
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        self.assertEqual(response.status_code, 200)
        self.assertJSONEqual(response.content, {
            'ok': True,
            'message': 'Draft autosaved.',
            'report_id': CRMDailyReport.objects.get().id,
            'detail_url': reverse('betting:crm_daily_report_detail', args=[CRMDailyReport.objects.get().id]),
        })
        self.assertEqual(CRMReportAttachment.objects.count(), 0)
