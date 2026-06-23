from django.test import TestCase, Client
from django.urls import reverse
from decimal import Decimal
from datetime import timedelta
from unittest.mock import patch
from django.utils import timezone
from betting.models import User, Wallet, UserWithdrawal, Transaction, CRMActionLog, CreditRequest, WithdrawalReport, BetTicket, BulkMessageCampaign, BulkMessageDelivery, BulkMessageTemplate, CRMOpsAuditLog, SiteConfiguration, CustomerComplaint, CustomerComplaintNote, Loan, OverdraftWallet
from betting.views import process_due_bulk_message_campaigns
from notifications.models import Notification, NotificationCampaign
from notifications.tasks import send_campaign
from betting.tasks import _maybe_alert_stuck_deposit

class RBACTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.password = 'password123'
        
        # Create users for each role
        self.player = User.objects.create_user(email='player@test.com', password=self.password, user_type='player')
        self.cashier = User.objects.create_user(email='cashier@test.com', password=self.password, user_type='cashier')
        self.agent = User.objects.create_user(email='agent@test.com', password=self.password, user_type='agent')
        self.super_agent = User.objects.create_user(email='super_agent@test.com', password=self.password, user_type='super_agent')
        self.master_agent = User.objects.create_user(email='master_agent@test.com', password=self.password, user_type='master_agent')
        self.account_user = User.objects.create_user(email='account_user@test.com', password=self.password, user_type='account_user')
        self.account_user_two = User.objects.create_user(email='account_user_two@test.com', password=self.password, user_type='account_user')
        self.admin = User.objects.create_user(email='admin@test.com', password=self.password, user_type='admin', is_staff=True, is_superuser=True)
        self.plain_admin = User.objects.create_user(email='plain_admin@test.com', password=self.password, user_type='admin', is_staff=True)
        self.crm_viewer = User.objects.create_user(email='crm_viewer@test.com', password=self.password, user_type='crm', crm_role='viewer')
        self.crm_ops = User.objects.create_user(email='crm_ops@test.com', password=self.password, user_type='crm', crm_role='ops')
        self.crm_compliance = User.objects.create_user(email='crm_compliance@test.com', password=self.password, user_type='crm', crm_role='compliance')
        self.crm_supervisor = User.objects.create_user(email='crm_supervisor@test.com', password=self.password, user_type='crm', crm_role='supervisor')
        
        # Create wallets for all users
        for user in [self.player, self.cashier, self.agent, self.super_agent, self.master_agent, self.account_user, self.account_user_two, self.admin, self.plain_admin, self.crm_viewer, self.crm_ops, self.crm_compliance, self.crm_supervisor]:
            Wallet.objects.get_or_create(user=user, defaults={'balance': Decimal('0.00')})

    def test_player_access(self):
        self.client.force_login(self.player)
        
        # Should access user dashboard
        response = self.client.get(reverse('betting:user_dashboard'))
        self.assertEqual(response.status_code, 200)
        
        # Should NOT access agent dashboard
        response = self.client.get(reverse('betting:agent_dashboard'))
        self.assertNotEqual(response.status_code, 200) # Likely 302 redirect to login
        
        # Should NOT access admin dashboard (using custom admin site URL or view)
        # Assuming betting:admin_dashboard view exists
        response = self.client.get(reverse('betting:admin_dashboard'))
        self.assertNotEqual(response.status_code, 200)

    def test_agent_access(self):
        self.client.force_login(self.agent)
        
        # Accessing user_dashboard should redirect to agent_dashboard
        response = self.client.get(reverse('betting:user_dashboard'))
        self.assertRedirects(response, reverse('betting:agent_dashboard'))
        
        # Should access agent dashboard
        response = self.client.get(reverse('betting:agent_dashboard'))
        self.assertEqual(response.status_code, 200)
        
        # Should NOT access master agent dashboard
        # Assuming betting:master_agent_dashboard exists
        try:
            url = reverse('betting:master_agent_dashboard')
            response = self.client.get(url)
            self.assertNotEqual(response.status_code, 200)
        except:
            # If URL doesn't exist, we skip this check but it's good to know
            pass

    def test_agent_dashboard_includes_live_overdraft_refresh_hooks(self):
        self.client.force_login(self.agent)
        response = self.client.get(reverse('betting:agent_dashboard'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'dashboardLoanOutstandingAmount')
        self.assertContains(response, 'dashboardLoanDueDate')
        self.assertContains(response, 'dashboardLoanStatusBadge')
        self.assertContains(response, reverse('betting:api_wallet_overdraft_status'))
        self.assertContains(response, '/ws/notifications/')

    def test_admin_access(self):
        self.client.force_login(self.admin)
        
        # Accessing user_dashboard should redirect to admin dashboard
        response = self.client.get(reverse('betting:user_dashboard'))
        self.assertRedirects(response, reverse('betting:admin_dashboard'))
        
        # Should access admin dashboard
        response = self.client.get(reverse('betting:admin_dashboard'))
        self.assertEqual(response.status_code, 200)

    def test_crm_access(self):
        self.client.force_login(self.crm_viewer)
        resp = self.client.get(reverse('betting:crm_dashboard'))
        self.assertEqual(resp.status_code, 200)

        self.client.force_login(self.player)
        resp2 = self.client.get(reverse('betting:crm_dashboard'))
        self.assertEqual(resp2.status_code, 403)

    def test_crm_dashboard_renders_system_ops_audit_rows_with_null_actor(self):
        CRMOpsAuditLog.objects.create(
            actor=None,
            module='bulk_messaging',
            action='scheduled_campaign_processed',
            target_user=self.player,
            metadata={'source': 'scheduler'},
        )

        self.client.force_login(self.admin)
        response = self.client.get(reverse('betting:crm_dashboard'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'System')
        self.assertContains(response, self.player.username or self.player.email)

    def test_admin_reconciled_credits_dashboard_lists_reconcile_wallet_credits(self):
        tx = Transaction.objects.create(
            user=self.player,
            transaction_type='deposit',
            amount=Decimal('250.00'),
            status='completed',
            is_successful=True,
            payment_gateway='paystack',
            external_reference='recon-ref-123',
            description='Online deposit via paystack successful.',
        )
        self.player.wallet.apply_delta(
            amount=Decimal('250.00'),
            actor=None,
            transaction_obj=tx,
            reference='recon-ref-123',
            reason='Deposit via paystack (reconcile)',
            metadata={'gateway': 'paystack', 'source': 'reconcile'},
        )

        self.client.force_login(self.admin)
        response = self.client.get(
            reverse('betting_admin:admin_reconciled_credits_dashboard'),
            {'gateway': 'paystack', 'q': 'recon-ref-123'},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'recon-ref-123')
        self.assertContains(response, self.player.email)
        self.assertEqual(response.context['summary']['total_count'], 1)

    def test_admin_loan_center_can_approve_pending_super_agent_request(self):
        loan = Loan.objects.create(
            borrower=self.super_agent,
            lender=self.admin,
            amount=Decimal('0.00'),
            requested_amount=Decimal('500.00'),
            qualified_amount=Decimal('500.00'),
            outstanding_balance=Decimal('0.00'),
            status='pending',
            loan_type='super_agent_overdraft',
            approval_level='admin',
        )

        self.client.force_login(self.admin)
        response = self.client.post(
            reverse('betting_admin:admin_loan_overdraft_center') + '?tab=pending',
            {
                'loan_id': str(loan.id),
                'action': 'approve',
                'loan_decision_submit': '1',
            },
            follow=True,
        )

        loan.refresh_from_db()
        self.super_agent.wallet.refresh_from_db()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(loan.status, 'active')
        self.assertEqual(loan.outstanding_balance, Decimal('500.00'))
        self.assertEqual(self.super_agent.wallet.balance, Decimal('500.00'))

    def test_admin_loan_center_can_fund_super_agent_overdraft_wallet(self):
        self.client.force_login(self.admin)
        response = self.client.post(
            reverse('betting_admin:admin_loan_overdraft_center'),
            {
                'super_agent': str(self.super_agent.id),
                'amount': '1200.00',
                'reason': 'Initial overdraft funding',
                'fund_wallet_submit': '1',
            },
            follow=True,
        )

        wallet = OverdraftWallet.objects.get(super_agent=self.super_agent)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(wallet.total_funded, Decimal('1200.00'))
        self.assertEqual(wallet.current_balance, Decimal('1200.00'))

    def test_admin_loan_center_can_run_due_enforcement(self):
        loan = Loan.objects.create(
            borrower=self.agent,
            lender=self.super_agent,
            amount=Decimal('300.00'),
            requested_amount=Decimal('300.00'),
            qualified_amount=Decimal('300.00'),
            outstanding_balance=Decimal('300.00'),
            status='active',
            loan_type='agent_overdraft',
            approval_level='super_agent',
            due_date=timezone.now() - timedelta(days=1),
        )

        self.client.force_login(self.admin)
        response = self.client.post(
            reverse('betting_admin:admin_loan_overdraft_center') + '?tab=overdue',
            {
                'run_due_enforcement': '1',
            },
            follow=True,
        )

        loan.refresh_from_db()
        self.agent.refresh_from_db()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(loan.status, 'overdue')
        self.assertTrue(loan.account_locked_due_to_default)
        self.assertTrue(self.agent.is_locked)

    def test_admin_loan_center_filters_and_summary_follow_filtered_results(self):
        overdue_agent_loan = Loan.objects.create(
            borrower=self.agent,
            lender=self.super_agent,
            amount=Decimal('450.00'),
            requested_amount=Decimal('450.00'),
            qualified_amount=Decimal('450.00'),
            outstanding_balance=Decimal('300.00'),
            status='overdue',
            loan_type='agent_overdraft',
            approval_level='super_agent',
            due_date=timezone.now() - timedelta(days=2),
            qualification_ticket_count=60,
            qualification_deposit_volume=Decimal('900.00'),
            account_locked_due_to_default=True,
        )
        self.agent.is_locked = True
        self.agent.lock_reason = 'Due to an unsettled overdraft/loan obligation, your account has been disabled.'
        self.agent.save(update_fields=['is_locked', 'lock_reason'])

        Loan.objects.create(
            borrower=self.super_agent,
            lender=self.admin,
            amount=Decimal('700.00'),
            requested_amount=Decimal('700.00'),
            qualified_amount=Decimal('700.00'),
            outstanding_balance=Decimal('0.00'),
            status='settled',
            loan_type='super_agent_overdraft',
            approval_level='admin',
            qualification_ticket_count=70,
            qualification_deposit_volume=Decimal('1400.00'),
        )

        self.client.force_login(self.admin)
        response = self.client.get(
            reverse('betting_admin:admin_loan_overdraft_center'),
            {
                'tab': 'overdue',
                'borrower_role': 'agent',
                'loan_type': 'agent_overdraft',
                'lock_state': 'locked',
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(response.context['loans_page'].object_list), [overdue_agent_loan])
        self.assertEqual(response.context['summary']['total_records'], 1)
        self.assertEqual(response.context['summary']['total_requested'], Decimal('450.00'))
        self.assertEqual(response.context['summary']['total_outstanding'], Decimal('300.00'))
        self.assertEqual(response.context['summary']['overdue_balance'], Decimal('300.00'))
        self.assertEqual(response.context['summary']['locked_count'], 1)

    def test_admin_loan_center_filtered_csv_export_matches_filtered_rows(self):
        targeted_loan = Loan.objects.create(
            borrower=self.agent,
            lender=self.super_agent,
            amount=Decimal('350.00'),
            requested_amount=Decimal('350.00'),
            qualified_amount=Decimal('350.00'),
            outstanding_balance=Decimal('200.00'),
            status='overdue',
            loan_type='agent_overdraft',
            approval_level='super_agent',
            due_date=timezone.now() - timedelta(days=3),
        )
        self.agent.is_locked = True
        self.agent.lock_reason = 'Due to an unsettled overdraft/loan obligation, your account has been disabled.'
        self.agent.save(update_fields=['is_locked', 'lock_reason'])

        other_super_agent = User.objects.create_user(
            email='other_super_agent@test.com',
            password=self.password,
            user_type='super_agent',
        )
        other_agent = User.objects.create_user(
            email='other_agent@test.com',
            password=self.password,
            user_type='agent',
            super_agent=other_super_agent,
        )
        Wallet.objects.get_or_create(user=other_super_agent, defaults={'balance': Decimal('0.00')})
        Wallet.objects.get_or_create(user=other_agent, defaults={'balance': Decimal('0.00')})
        Loan.objects.create(
            borrower=other_agent,
            lender=other_super_agent,
            amount=Decimal('900.00'),
            requested_amount=Decimal('900.00'),
            qualified_amount=Decimal('900.00'),
            outstanding_balance=Decimal('400.00'),
            status='overdue',
            loan_type='agent_overdraft',
            approval_level='super_agent',
            due_date=timezone.now() - timedelta(days=1),
        )

        self.client.force_login(self.admin)
        response = self.client.get(
            reverse('betting_admin:admin_loan_overdraft_center'),
            {
                'tab': 'overdue',
                'borrower_role': 'agent',
                'loan_type': 'agent_overdraft',
                'lock_state': 'locked',
                'format': 'csv',
            },
        )

        content = response.content.decode()
        self.assertEqual(response.status_code, 200)
        self.assertIn(str(targeted_loan.id), content)
        self.assertIn(self.agent.username or self.agent.email, content)
        self.assertNotIn(other_agent.username or other_agent.email, content)

    def test_super_agent_can_approve_pending_agent_overdraft_request_from_processing_view(self):
        self.agent.super_agent = self.super_agent
        self.agent.save(update_fields=['super_agent'])
        OverdraftWallet.objects.create(
            super_agent=self.super_agent,
            total_funded=Decimal('1000.00'),
            current_balance=Decimal('1000.00'),
        )
        loan = Loan.objects.create(
            borrower=self.agent,
            lender=self.super_agent,
            amount=Decimal('0.00'),
            requested_amount=Decimal('250.00'),
            qualified_amount=Decimal('250.00'),
            outstanding_balance=Decimal('0.00'),
            status='pending',
            loan_type='agent_overdraft',
            approval_level='super_agent',
        )

        self.client.force_login(self.super_agent)
        response = self.client.post(
            reverse('betting:process_overdraft_request', args=[loan.id]),
            {
                'action': 'approve',
                'return_to': reverse('betting:super_agent_dashboard'),
            },
        )

        loan.refresh_from_db()
        self.agent.wallet.refresh_from_db()
        wallet = OverdraftWallet.objects.get(super_agent=self.super_agent)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse('betting:super_agent_dashboard'))
        self.assertEqual(loan.status, 'active')
        self.assertEqual(loan.outstanding_balance, Decimal('250.00'))
        self.assertEqual(self.agent.wallet.balance, Decimal('250.00'))
        self.assertEqual(wallet.current_balance, Decimal('750.00'))

    def test_admin_loan_center_locked_tab_returns_only_locked_defaulted_loans(self):
        locked_loan = Loan.objects.create(
            borrower=self.agent,
            lender=self.super_agent,
            amount=Decimal('400.00'),
            requested_amount=Decimal('400.00'),
            qualified_amount=Decimal('400.00'),
            outstanding_balance=Decimal('250.00'),
            status='overdue',
            loan_type='agent_overdraft',
            approval_level='super_agent',
            due_date=timezone.now() - timedelta(days=1),
            account_locked_due_to_default=True,
        )
        self.agent.is_locked = True
        self.agent.lock_reason = 'Due to an unsettled overdraft/loan obligation, your account has been disabled.'
        self.agent.save(update_fields=['is_locked', 'lock_reason'])

        unlocked_agent = User.objects.create_user(
            email='unlocked_agent_tab@test.com',
            password=self.password,
            user_type='agent',
        )
        Wallet.objects.get_or_create(user=unlocked_agent, defaults={'balance': Decimal('0.00')})
        Loan.objects.create(
            borrower=unlocked_agent,
            lender=self.super_agent,
            amount=Decimal('500.00'),
            requested_amount=Decimal('500.00'),
            qualified_amount=Decimal('500.00'),
            outstanding_balance=Decimal('500.00'),
            status='active',
            loan_type='agent_overdraft',
            approval_level='super_agent',
            due_date=timezone.now() + timedelta(days=1),
            account_locked_due_to_default=False,
        )

        self.client.force_login(self.admin)
        response = self.client.get(reverse('betting_admin:admin_loan_overdraft_center'), {'tab': 'locked'})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(response.context['loans_page'].object_list), [locked_loan])

    def test_admin_loan_center_settled_tab_returns_only_settled_loans(self):
        settled_loan = Loan.objects.create(
            borrower=self.super_agent,
            lender=self.admin,
            amount=Decimal('600.00'),
            requested_amount=Decimal('600.00'),
            qualified_amount=Decimal('600.00'),
            outstanding_balance=Decimal('0.00'),
            repaid_amount=Decimal('600.00'),
            status='settled',
            loan_type='super_agent_overdraft',
            approval_level='admin',
            settled_at=timezone.now(),
        )
        Loan.objects.create(
            borrower=self.agent,
            lender=self.super_agent,
            amount=Decimal('300.00'),
            requested_amount=Decimal('300.00'),
            qualified_amount=Decimal('300.00'),
            outstanding_balance=Decimal('150.00'),
            status='active',
            loan_type='agent_overdraft',
            approval_level='super_agent',
        )

        self.client.force_login(self.admin)
        response = self.client.get(reverse('betting_admin:admin_loan_overdraft_center'), {'tab': 'settled'})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(response.context['loans_page'].object_list), [settled_loan])

    def test_retail_dashboard_requires_retail_manager_role(self):
        self.client.force_login(self.agent)
        response = self.client.get(reverse('betting:retail_dashboard'))
        self.assertEqual(response.status_code, 403)

    def test_crm_export_requires_crm_role_and_returns_scoped_csv(self):
        self.client.force_login(self.cashier)
        forbidden_response = self.client.get(reverse('betting:crm_export'), {
            'dataset': 'dormant_accounts',
            'format': 'csv',
            'dormant_bucket': 'login_7',
        })
        self.assertEqual(forbidden_response.status_code, 403)

        stale_login = timezone.now() - timedelta(days=10)
        self.agent.first_name = 'Dormant Agent'
        self.agent.last_login = stale_login
        self.agent.save(update_fields=['first_name', 'last_login'])
        self.cashier.agent = self.agent
        self.cashier.last_login = stale_login
        self.cashier.save(update_fields=['agent', 'last_login'])

        self.client.force_login(self.crm_viewer)
        allowed_response = self.client.get(reverse('betting:crm_export'), {
            'dataset': 'dormant_accounts',
            'format': 'csv',
            'dormant_bucket': 'login_7',
        })
        self.assertEqual(allowed_response.status_code, 200)
        exported_identifier = self.agent.username or self.agent.email
        self.assertIn(exported_identifier, allowed_response.content.decode())

    def test_crm_bulk_campaign_supports_custom_users_with_template_defaults(self):
        template = BulkMessageTemplate.objects.create(
            name='Dormancy Reminder',
            category='dormancy_reminders',
            default_channel='in_app',
            subject='We miss you',
            message='Come back and place your next bet.',
            is_active=True,
            created_by=self.crm_viewer,
        )

        self.client.force_login(self.crm_viewer)
        response = self.client.post(reverse('betting:crm_dashboard'), {
            'tab': 'bulk_messaging',
            'save_bulk_campaign': '1',
            'template': str(template.id),
            'channel': 'in_app',
            'target_group': 'custom_users',
            'target_users': [str(self.player.id)],
            'subject': '',
            'message': '',
            'recurring_pattern': 'none',
            'send_now': '1',
        })
        self.assertNotEqual(response.status_code, 500)

        campaign = BulkMessageCampaign.objects.latest('created_at')
        self.assertEqual(campaign.target_group, 'custom_users')
        self.assertEqual(campaign.subject, 'We miss you')
        self.assertEqual(campaign.message, 'Come back and place your next bet.')
        self.assertEqual(campaign.target_user_ids, [self.player.id])
        self.assertEqual(campaign.status, 'sent')
        self.assertEqual(campaign.recipients_count, 1)
        self.assertEqual(campaign.delivered_count, 1)
        self.assertTrue(BulkMessageDelivery.objects.filter(campaign=campaign, recipient=self.player, status='sent').exists())

    def test_scheduled_bulk_campaigns_can_be_processed_outside_dashboard(self):
        campaign = BulkMessageCampaign.objects.create(
            created_by=self.crm_viewer,
            subject='Scheduled Reminder',
            message='This is a scheduled message.',
            channel='in_app',
            target_group='custom_users',
            target_user_ids=[self.player.id],
            schedule_at=timezone.now() - timedelta(minutes=1),
            recurring_pattern='none',
            status='scheduled',
        )

        processed = process_due_bulk_message_campaigns(limit=5)

        self.assertEqual(processed, 1)
        campaign.refresh_from_db()
        self.assertEqual(campaign.status, 'sent')
        self.assertEqual(campaign.recipients_count, 1)
        self.assertEqual(campaign.delivered_count, 1)
        self.assertTrue(BulkMessageDelivery.objects.filter(campaign=campaign, recipient=self.player, status='sent').exists())
        self.assertTrue(
            CRMOpsAuditLog.objects.filter(
                module='bulk_messaging',
                action='campaign_sent',
                campaign=campaign,
            ).exists()
        )

    def test_recurring_bulk_campaign_reschedules_into_the_future(self):
        overdue_schedule = timezone.now() - timedelta(days=3)
        campaign = BulkMessageCampaign.objects.create(
            created_by=self.crm_viewer,
            subject='Recurring Reminder',
            message='This is a recurring message.',
            channel='in_app',
            target_group='custom_users',
            target_user_ids=[self.player.id],
            schedule_at=overdue_schedule,
            recurring_pattern='daily',
            status='scheduled',
        )

        processed = process_due_bulk_message_campaigns(limit=5)

        self.assertEqual(processed, 1)
        campaign.refresh_from_db()
        self.assertEqual(campaign.recipients_count, 1)
        self.assertEqual(campaign.delivered_count, 1)
        self.assertEqual(campaign.status, 'scheduled')
        self.assertIsNotNone(campaign.next_run_at)
        self.assertIsNotNone(campaign.schedule_at)
        self.assertGreater(campaign.next_run_at, timezone.now())
        self.assertGreater(campaign.schedule_at, timezone.now())
        self.assertTrue(BulkMessageDelivery.objects.filter(campaign=campaign, recipient=self.player, status='sent').exists())

    def test_crm_bulk_messaging_export_respects_channel_and_target_group_filters(self):
        BulkMessageCampaign.objects.create(
            created_by=self.crm_viewer,
            subject='Dormant Email Campaign',
            message='Email message',
            channel='email',
            target_group='dormant_users',
            status='sent',
            recipients_count=5,
            delivered_count=5,
        )
        BulkMessageCampaign.objects.create(
            created_by=self.crm_viewer,
            subject='Agent SMS Campaign',
            message='SMS message',
            channel='sms',
            target_group='all_agents',
            status='sent',
            recipients_count=3,
            delivered_count=3,
        )

        self.client.force_login(self.crm_viewer)
        response = self.client.get(
            reverse('betting:crm_export'),
            {
                'dataset': 'bulk_messaging',
                'format': 'csv',
                'campaign_channel': 'email',
                'campaign_target_group': 'dormant_users',
            },
        )

        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn('Dormant Email Campaign', body)
        self.assertNotIn('Agent SMS Campaign', body)

    def test_crm_dormant_export_respects_search_query(self):
        other_agent = User.objects.create_user(
            email='other-export-agent@test.com',
            password='pass12345',
            user_type='agent',
            username='other_export_agent',
        )
        Wallet.objects.create(user=other_agent, balance=Decimal('15.00'))

        stale_login = timezone.now() - timedelta(days=10)
        self.agent.first_name = 'DormantFilterTarget'
        self.agent.last_login = stale_login
        self.agent.save(update_fields=['first_name', 'last_login'])
        other_agent.first_name = 'DormantFilterOther'
        other_agent.last_login = stale_login
        other_agent.save(update_fields=['first_name', 'last_login'])

        self.client.force_login(self.crm_viewer)
        response = self.client.get(
            reverse('betting:crm_export'),
            {
                'dataset': 'dormant_accounts',
                'format': 'csv',
                'dormant_bucket': 'login_7',
                'q': 'DormantFilterTarget',
            },
        )

        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn(self.agent.username or self.agent.email, body)
        self.assertNotIn(other_agent.username, body)

    def test_crm_dormant_export_requires_inactive_agent_and_cashier_hierarchy(self):
        dormant_agent = User.objects.create_user(
            email='dormant-agent-hierarchy@test.com',
            password='pass12345',
            user_type='agent',
            username='dormant_agent_hierarchy',
        )
        active_cashier_agent = User.objects.create_user(
            email='active-cashier-agent@test.com',
            password='pass12345',
            user_type='agent',
            username='active_cashier_agent',
        )
        active_agent = User.objects.create_user(
            email='active-agent-hierarchy@test.com',
            password='pass12345',
            user_type='agent',
            username='active_agent_hierarchy',
        )
        dormant_cashier = User.objects.create_user(
            email='dormant-cashier-hierarchy@test.com',
            password='pass12345',
            user_type='cashier',
            username='dormant_cashier_hierarchy',
            agent=dormant_agent,
        )
        active_cashier = User.objects.create_user(
            email='active-cashier-hierarchy@test.com',
            password='pass12345',
            user_type='cashier',
            username='active_cashier_hierarchy',
            agent=active_cashier_agent,
        )
        inactive_cashier = User.objects.create_user(
            email='inactive-cashier-hierarchy@test.com',
            password='pass12345',
            user_type='cashier',
            username='inactive_cashier_hierarchy',
            agent=active_agent,
        )
        for user in [dormant_agent, active_cashier_agent, active_agent, dormant_cashier, active_cashier, inactive_cashier]:
            Wallet.objects.create(user=user, balance=Decimal('0.00'))

        stale_login = timezone.now() - timedelta(days=10)
        recent_login = timezone.now() - timedelta(days=1)
        User.objects.filter(id__in=[dormant_agent.id, active_cashier_agent.id, dormant_cashier.id, inactive_cashier.id]).update(last_login=stale_login)
        User.objects.filter(id__in=[active_agent.id, active_cashier.id]).update(last_login=recent_login)

        self.client.force_login(self.crm_viewer)
        response = self.client.get(
            reverse('betting:crm_export'),
            {
                'dataset': 'dormant_accounts',
                'format': 'csv',
                'dormant_bucket': 'login_7',
            },
        )

        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn('dormant_agent_hierarchy', body)
        self.assertNotIn('active_cashier_agent', body)
        self.assertNotIn('active_agent_hierarchy', body)

    def test_crm_dormant_dashboard_counts_ignore_global_date_range(self):
        stale_login = timezone.now() - timedelta(days=10)
        self.agent.first_name = 'Dormant Date Range Agent'
        self.agent.last_login = stale_login
        self.agent.save(update_fields=['first_name', 'last_login'])
        self.cashier.agent = self.agent
        self.cashier.last_login = stale_login
        self.cashier.save(update_fields=['agent', 'last_login'])

        self.client.force_login(self.crm_viewer)
        response = self.client.get(
            reverse('betting:crm_dashboard'),
            {
                'tab': 'dormant_accounts',
                'dormant_bucket': 'login_7',
                'start_date': timezone.localdate().isoformat(),
                'end_date': timezone.localdate().isoformat(),
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['dormant_cards']['login_7'], 1)
        self.assertEqual(response.context['dormant_agents_tab_count'], 1)
        self.assertContains(response, 'Dormant Date Range Agent')

    def test_crm_deposit_monitoring_export_respects_status_gateway_and_flag_filters(self):
        site_config = SiteConfiguration.load()
        site_config.crm_large_deposit_threshold = Decimal('1000.00')
        site_config.crm_failed_deposit_repeat_threshold = 2
        site_config.save(update_fields=['crm_large_deposit_threshold', 'crm_failed_deposit_repeat_threshold'])

        matched_player = User.objects.create_user(
            email='matched-deposit-player@test.com',
            password='pass12345',
            user_type='player',
            username='matched_deposit_player',
        )
        other_player = User.objects.create_user(
            email='other-deposit-player@test.com',
            password='pass12345',
            user_type='player',
            username='other_deposit_player',
        )
        Wallet.objects.create(user=matched_player, balance=Decimal('20.00'))
        Wallet.objects.create(user=other_player, balance=Decimal('30.00'))

        Transaction.objects.create(
            user=matched_player,
            transaction_type='deposit',
            amount=Decimal('250.00'),
            is_successful=False,
            status='failed',
            payment_gateway='paystack',
            description='Failed deposit one',
        )
        matched_tx = Transaction.objects.create(
            user=matched_player,
            transaction_type='deposit',
            amount=Decimal('350.00'),
            is_successful=False,
            status='failed',
            payment_gateway='paystack',
            description='Failed deposit two',
        )
        other_tx = Transaction.objects.create(
            user=other_player,
            transaction_type='deposit',
            amount=Decimal('5000.00'),
            is_successful=False,
            status='failed',
            payment_gateway='monnify',
            description='Failed deposit other gateway',
        )

        self.client.force_login(self.crm_viewer)
        response = self.client.get(
            reverse('betting:crm_export'),
            {
                'dataset': 'deposit_monitoring',
                'format': 'csv',
                'deposit_status_filter': 'failed',
                'deposit_gateway': 'paystack',
                'deposit_flag': 'repeat_failed',
            },
        )

        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        matched_ref = matched_tx.external_reference or matched_tx.paystack_reference or str(matched_tx.id)
        other_ref = other_tx.external_reference or other_tx.paystack_reference or str(other_tx.id)
        self.assertIn(matched_ref, body)
        self.assertNotIn(other_ref, body)

    def test_crm_user_activation_export_respects_category_query_and_user_type_filters(self):
        matched_player = User.objects.create_user(
            email='matched-activation-player@test.com',
            password='pass12345',
            user_type='player',
            username='matched_activation_player',
            first_name='ActivationFilterMatch',
        )
        other_player = User.objects.create_user(
            email='other-activation-player@test.com',
            password='pass12345',
            user_type='player',
            username='other_activation_player',
            first_name='ActivationFilterOther',
        )
        Wallet.objects.create(user=matched_player, balance=Decimal('40.00'))
        Wallet.objects.create(user=other_player, balance=Decimal('25.00'))

        Transaction.objects.create(
            user=matched_player,
            transaction_type='deposit',
            amount=Decimal('100.00'),
            is_successful=True,
            status='completed',
            payment_gateway='paystack',
            description='Matched activation deposit',
        )
        Transaction.objects.create(
            user=other_player,
            transaction_type='deposit',
            amount=Decimal('120.00'),
            is_successful=True,
            status='completed',
            payment_gateway='paystack',
            description='Other activation deposit',
        )
        BetTicket.objects.create(
            user=other_player,
            bet_type='single',
            stake_amount=Decimal('10.00'),
            total_odd=Decimal('3.00'),
            potential_winning=Decimal('30.00'),
            min_winning=Decimal('30.00'),
            max_winning=Decimal('30.00'),
            status='won',
        )

        self.client.force_login(self.crm_viewer)
        response = self.client.get(
            reverse('betting:crm_export'),
            {
                'dataset': 'user_activation',
                'format': 'csv',
                'activation_category': 'deposited_never_played',
                'activation_q': 'ActivationFilterMatch',
                'activation_user_type': 'player',
            },
        )

        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn(matched_player.username or matched_player.email, body)
        self.assertNotIn(other_player.username, body)

    def test_crm_complaints_export_respects_query_type_status_and_priority_filters(self):
        matching_complaint = CustomerComplaint.objects.create(
            complaint_type='deposit',
            user=self.player,
            subject='ComplaintExportTarget Deposit Delay',
            description='ComplaintExportTarget description for deposit issue',
            status='escalated',
            priority='critical',
            assigned_to=self.crm_ops,
            created_by=self.crm_viewer,
        )
        CustomerComplaint.objects.create(
            complaint_type='wallet',
            user=self.player,
            subject='Wallet balance issue',
            description='Non matching complaint',
            status='open',
            priority='low',
            assigned_to=self.crm_viewer,
            created_by=self.crm_viewer,
        )

        self.client.force_login(self.crm_viewer)
        response = self.client.get(
            reverse('betting:crm_export'),
            {
                'dataset': 'complaints',
                'format': 'csv',
                'complaint_q': 'ComplaintExportTarget',
                'complaint_type': 'deposit',
                'complaint_status': 'escalated',
                'complaint_priority': 'critical',
            },
        )

        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn(matching_complaint.subject, body)
        self.assertNotIn('Wallet balance issue', body)

    def test_crm_agent_performance_export_respects_entity_and_query_filters(self):
        matched_super_agent = User.objects.create_user(
            email='matched-super-agent@test.com',
            password='pass12345',
            user_type='super_agent',
            username='matched_super_agent',
            first_name='PerfExportTarget',
        )
        other_super_agent = User.objects.create_user(
            email='other-super-agent@test.com',
            password='pass12345',
            user_type='super_agent',
            username='other_super_agent',
            first_name='PerfExportOther',
        )
        matched_agent = User.objects.create_user(
            email='matched-agent@test.com',
            password='pass12345',
            user_type='agent',
            username='matched_agent',
            super_agent=matched_super_agent,
        )
        other_agent = User.objects.create_user(
            email='other-agent@test.com',
            password='pass12345',
            user_type='agent',
            username='other_agent',
            super_agent=other_super_agent,
        )
        matched_player = User.objects.create_user(
            email='matched-performance-player@test.com',
            password='pass12345',
            user_type='player',
            username='matched_performance_player',
            agent=matched_agent,
        )
        other_player = User.objects.create_user(
            email='other-performance-player@test.com',
            password='pass12345',
            user_type='player',
            username='other_performance_player',
            agent=other_agent,
        )
        for user in [matched_super_agent, other_super_agent, matched_agent, other_agent, matched_player, other_player]:
            Wallet.objects.get_or_create(user=user, defaults={'balance': Decimal('0.00')})

        BetTicket.objects.create(
            user=matched_player,
            bet_type='single',
            stake_amount=Decimal('20.00'),
            total_odd=Decimal('2.00'),
            potential_winning=Decimal('40.00'),
            min_winning=Decimal('40.00'),
            max_winning=Decimal('40.00'),
            status='won',
        )
        BetTicket.objects.create(
            user=other_player,
            bet_type='single',
            stake_amount=Decimal('15.00'),
            total_odd=Decimal('1.50'),
            potential_winning=Decimal('22.50'),
            min_winning=Decimal('22.50'),
            max_winning=Decimal('22.50'),
            status='lost',
        )

        self.client.force_login(self.crm_viewer)
        response = self.client.get(
            reverse('betting:crm_export'),
            {
                'dataset': 'agent_performance',
                'format': 'csv',
                'performance_entity': 'super_agent',
                'performance_q': 'PerfExportTarget',
            },
        )

        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn(matched_super_agent.username or matched_super_agent.email, body)
        self.assertNotIn(other_super_agent.username, body)

    def test_crm_complaint_update_persists_assignment_resolution_note_and_audit(self):
        complaint = CustomerComplaint.objects.create(
            complaint_type='wallet',
            user=self.player,
            subject='Wallet issue',
            description='Customer reported a wallet issue.',
            status='open',
            priority='low',
            created_by=self.crm_viewer,
        )

        self.client.force_login(self.crm_viewer)
        response = self.client.post(reverse('betting:crm_dashboard'), {
            'tab': 'complaints',
            'update_complaint': '1',
            'complaint_id': str(complaint.id),
            'status': 'resolved',
            'priority': 'critical',
            'assigned_to': str(self.crm_ops.id),
            'admin_note': 'Resolved after wallet balance review.',
        })

        self.assertEqual(response.status_code, 302)
        complaint.refresh_from_db()
        self.assertEqual(complaint.status, 'resolved')
        self.assertEqual(complaint.priority, 'critical')
        self.assertEqual(complaint.assigned_to, self.crm_ops)
        self.assertIsNotNone(complaint.resolved_at)
        self.assertTrue(
            CustomerComplaintNote.objects.filter(
                complaint=complaint,
                author=self.crm_viewer,
                note='Resolved after wallet balance review.',
                is_internal=True,
            ).exists()
        )
        self.assertTrue(
            CRMOpsAuditLog.objects.filter(
                module='complaints',
                action='complaint_updated',
                complaint=complaint,
                target_user=self.player,
            ).exists()
        )

    def test_crm_complaint_note_add_creates_note_and_audit_log(self):
        complaint = CustomerComplaint.objects.create(
            complaint_type='deposit',
            user=self.player,
            subject='Deposit issue',
            description='Customer reported a deposit issue.',
            status='open',
            priority='medium',
            created_by=self.crm_viewer,
        )

        self.client.force_login(self.crm_viewer)
        response = self.client.post(reverse('betting:crm_dashboard'), {
            'tab': 'complaints',
            'add_complaint_note': '1',
            'complaint_id': str(complaint.id),
            'note': 'Followed up with payment provider.',
            'is_internal': 'on',
        })

        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            CustomerComplaintNote.objects.filter(
                complaint=complaint,
                author=self.crm_viewer,
                note='Followed up with payment provider.',
                is_internal=True,
            ).exists()
        )
        self.assertTrue(
            CRMOpsAuditLog.objects.filter(
                module='complaints',
                action='complaint_note_added',
                complaint=complaint,
                target_user=self.player,
            ).exists()
        )

    def test_crm_dashboard_message_creates_notification_action_log_and_ops_audit(self):
        self.agent.last_login = timezone.now() - timedelta(days=10)
        self.agent.save(update_fields=['last_login'])
        self.client.force_login(self.crm_viewer)
        response = self.client.post(reverse('betting:crm_dashboard'), {
            'tab': 'dormant_accounts',
            'module': 'dormant_accounts',
            'send_dashboard_message': '1',
            'target_user_ids': str(self.agent.id),
            'msg_title': 'We miss you',
            'msg_body': 'Come back and place your next bet.',
            'message_channels': ['in_app'],
        })

        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            Notification.objects.filter(
                recipient=self.agent,
                title='We miss you',
                message='Come back and place your next bet.',
            ).exists()
        )
        self.assertTrue(
            CRMActionLog.objects.filter(
                actor=self.crm_viewer,
                target_user=self.agent,
                action_type='MESSAGE_SENT',
                data__source='dashboard_ops',
                data__module='dormant_accounts',
            ).exists()
        )
        self.assertTrue(
            CRMOpsAuditLog.objects.filter(
                actor=self.crm_viewer,
                module='dormant_accounts',
                action='message_sent',
                target_user=self.agent,
            ).exists()
        )

    def test_crm_deposit_escalation_creates_ops_audit_log(self):
        tx = Transaction.objects.create(
            user=self.player,
            transaction_type='deposit',
            amount=Decimal('250.00'),
            is_successful=False,
            status='failed',
            payment_gateway='paystack',
            description='Failed deposit for escalation test',
        )

        self.client.force_login(self.crm_viewer)
        response = self.client.post(reverse('betting:crm_dashboard'), {
            'tab': 'deposit_monitoring',
            'escalate_deposit': '1',
            'transaction_id': str(tx.id),
            'escalation_note': 'Escalated for manual review.',
        })

        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            CRMOpsAuditLog.objects.filter(
                actor=self.crm_viewer,
                module='deposit_monitoring',
                action='deposit_escalated',
                target_user=self.player,
                transaction=tx,
            ).exists()
        )

    def test_crm_ops_can_approve_and_reject_withdrawals(self):
        self.player.wallet.balance = Decimal('0.00')
        self.player.wallet.save(update_fields=['balance'])

        w = UserWithdrawal.objects.create(
            user=self.player,
            amount=Decimal('500.00'),
            bank_name='Test Bank',
            account_name='Test User',
            account_number='1234567890',
            status='pending',
        )
        self.client.force_login(self.crm_ops)
        resp = self.client.post(reverse('betting:crm_withdrawal_action', args=[w.id]), {'action': 'approve'})
        self.assertNotEqual(resp.status_code, 500)
        w.refresh_from_db()
        self.assertEqual(w.status, 'approved')
        self.assertTrue(CRMActionLog.objects.filter(withdrawal=w, action_type='WITHDRAWAL_APPROVED').exists())

        w2 = UserWithdrawal.objects.create(
            user=self.player,
            amount=Decimal('200.00'),
            bank_name='Test Bank',
            account_name='Test User',
            account_number='1234567890',
            status='pending',
        )
        Wallet.objects.filter(user=self.player).update(balance=Decimal('100.00'))

        resp2 = self.client.post(reverse('betting:crm_withdrawal_action', args=[w2.id]), {'action': 'reject', 'reason': 'Invalid'})
        self.assertNotEqual(resp2.status_code, 500)
        w2.refresh_from_db()
        self.assertEqual(w2.status, 'rejected')
        self.player.wallet.refresh_from_db()
        self.assertEqual(self.player.wallet.balance, Decimal('300.00'))
        self.assertTrue(Transaction.objects.filter(user=self.player, transaction_type='withdrawal_refund', amount=Decimal('200.00')).exists())
        self.assertTrue(CRMActionLog.objects.filter(withdrawal=w2, action_type='WITHDRAWAL_REJECTED').exists())

    def test_crm_compliance_can_suspend_user(self):
        self.client.force_login(self.crm_compliance)
        url = reverse('betting:crm_user_detail', args=[self.player.id])
        resp = self.client.post(url, {'toggle_active': '1', 'make_active': '0', 'reason': 'Fraud'})
        self.assertNotEqual(resp.status_code, 500)
        self.player.refresh_from_db()
        self.assertFalse(self.player.is_active)
        self.assertTrue(CRMActionLog.objects.filter(target_user=self.player, action_type='USER_SUSPENDED').exists())

    def test_crm_in_app_message_creates_popup_notification(self):
        self.client.force_login(self.crm_viewer)
        resp = self.client.post(reverse('betting:crm_user_detail', args=[self.player.id]), {
            'send_message': '1',
            'target_user_id': str(self.player.id),
            'msg_title': 'Support Update',
            'msg_body': 'Please check your account notice.',
            'via_inapp': '1',
        })
        self.assertNotEqual(resp.status_code, 500)
        notif = Notification.objects.filter(recipient=self.player, title='Support Update').latest('created_at')
        self.assertEqual(notif.data.get('popup_category'), 'message')
        self.assertEqual(notif.data.get('delivery_channel'), 'in_app')

    @patch('django.core.mail.EmailMultiAlternatives.send', return_value=1)
    def test_crm_email_message_creates_popup_notification(self, _mock_send):
        self.client.force_login(self.crm_supervisor)
        resp = self.client.post(reverse('betting:crm_user_detail', args=[self.player.id]), {
            'send_message': '1',
            'target_user_id': str(self.player.id),
            'msg_title': 'Email Notice',
            'msg_body': 'Check your email inbox.',
            'via_email': '1',
        })
        self.assertNotEqual(resp.status_code, 500)
        notif = Notification.objects.filter(recipient=self.player, title='Email Notice').latest('created_at')
        self.assertEqual(notif.data.get('popup_category'), 'message')
        self.assertEqual(notif.data.get('delivery_channel'), 'email')

    def test_broadcast_campaign_creates_message_popup_metadata(self):
        campaign = NotificationCampaign.objects.create(
            title='Broadcast Notice',
            message='A new platform-wide broadcast is available.',
            notification_type='SYSTEM_ANNOUNCEMENT',
            send_to_all=False,
            target_user_ids=[self.player.id],
            created_by=self.crm_viewer,
        )

        with patch('notifications.tasks.create_broadcast_notification') as mock_broadcast:
            created = send_campaign(campaign.id)
            mock_broadcast.assert_not_called()

        self.assertEqual(created, 1)
        notif = Notification.objects.filter(recipient=self.player, title='Broadcast Notice').latest('created_at')
        self.assertEqual(notif.data.get('popup_category'), 'message')
        self.assertEqual(notif.data.get('delivery_channel'), 'broadcast')
        self.assertFalse(Notification.objects.filter(recipient=self.agent, title='Broadcast Notice').exists())

    def test_stuck_deposit_alert_is_not_broadcast_to_all_users(self):
        tx = Transaction.objects.create(
            user=self.player,
            transaction_type='deposit',
            status='pending',
            is_successful=False,
            amount=Decimal('7200.00'),
            payment_gateway='paystack',
            external_reference='ref-123',
        )

        _maybe_alert_stuck_deposit(tx=tx, now=timezone.now(), ttl_seconds=60)

        self.assertTrue(Notification.objects.filter(recipient=self.player, title='Deposit pending verification').exists())
        self.assertTrue(Notification.objects.filter(recipient=self.admin, title='Pending deposit requires attention').exists())
        self.assertTrue(Notification.objects.filter(recipient=self.plain_admin, title='Pending deposit requires attention').exists())
        self.assertFalse(Notification.objects.filter(recipient=self.agent, title='Pending deposit requires attention').exists())
        self.assertFalse(Notification.objects.filter(recipient=self.account_user, title='Pending deposit requires attention').exists())

    def test_crm_dashboard_broadcast_sends_immediately(self):
        self.client.force_login(self.crm_viewer)
        resp = self.client.post(reverse('betting:crm_dashboard'), {
            'tab': 'communications',
            'create_campaign': '1',
            'campaign_title': 'Immediate Broadcast',
            'campaign_message': 'This should arrive immediately.',
        })
        self.assertNotEqual(resp.status_code, 500)
        self.assertTrue(NotificationCampaign.objects.filter(title='Immediate Broadcast', sent_at__isnull=False).exists())
        notif = Notification.objects.filter(recipient=self.player, title='Immediate Broadcast').latest('created_at')
        self.assertEqual(notif.data.get('popup_category'), 'message')
        self.assertEqual(notif.data.get('delivery_channel'), 'broadcast')

    def test_crm_wallet_adjust_creates_pending_approval_request_and_account_user_can_approve(self):
        Wallet.objects.filter(user=self.account_user).update(balance=Decimal('500.00'))
        Wallet.objects.filter(user=self.player).update(balance=Decimal('10.00'))

        self.client.force_login(self.crm_compliance)
        resp = self.client.post(reverse('betting:crm_user_detail', args=[self.player.id]), {
            'wallet_adjust': '1',
            'target_user_id': str(self.player.id),
            'action': 'credit',
            'amount': '125.00',
            'reason': 'Manual support top-up',
            'description': 'CRM requested wallet top-up',
        })
        self.assertNotEqual(resp.status_code, 500)

        credit_req = CreditRequest.objects.get(requester=self.player, request_type='crm_credit')
        self.assertEqual(credit_req.status, 'pending')
        self.assertEqual(credit_req.recipient, self.account_user)
        self.player.wallet.refresh_from_db()
        self.assertEqual(self.player.wallet.balance, Decimal('10.00'))
        self.assertTrue(CRMActionLog.objects.filter(target_user=self.player, action_type='WALLET_CREDIT_REQUESTED').exists())

        self.client.force_login(self.account_user)
        resp2 = self.client.post(reverse('betting:approve_credit_request', args=[credit_req.id]), {'action': 'approve'})
        self.assertNotEqual(resp2.status_code, 500)
        credit_req.refresh_from_db()
        self.assertEqual(credit_req.status, 'approved')
        self.account_user.wallet.refresh_from_db()
        self.player.wallet.refresh_from_db()
        self.assertEqual(self.account_user.wallet.balance, Decimal('375.00'))
        self.assertEqual(self.player.wallet.balance, Decimal('135.00'))
        crm_log = CRMActionLog.objects.filter(target_user=self.player, action_type='WALLET_CREDITED').latest('created_at')
        self.assertEqual(crm_log.actor, self.account_user)
        self.assertEqual(crm_log.data.get('approved_by_role'), 'account_user')

    def test_superadmin_can_approve_crm_wallet_request_without_debiting_any_wallet(self):
        Wallet.objects.filter(user=self.admin).update(balance=Decimal('600.00'))
        Wallet.objects.filter(user=self.player).update(balance=Decimal('20.00'))

        credit_req = CreditRequest.objects.create(
            requester=self.player,
            recipient=self.account_user,
            amount=Decimal('150.00'),
            reason='Admin approved CRM top-up',
            request_type='crm_credit',
            status='pending',
        )

        self.client.force_login(self.admin)
        admin_index = self.client.get(reverse('betting_admin:index'))
        self.assertContains(admin_index, 'CRM Wallet Approval Requests')
        self.assertContains(admin_index, '>1<', html=False)
        self.assertContains(admin_index, 'Open Queue')

        process_url = reverse('betting_admin:betting_crmwalletapprovalrequest_process', args=[credit_req.id, 'approve'])
        resp = self.client.post(process_url)
        self.assertNotEqual(resp.status_code, 500)

        credit_req.refresh_from_db()
        self.assertEqual(credit_req.status, 'approved')
        self.admin.wallet.refresh_from_db()
        self.player.wallet.refresh_from_db()
        self.assertEqual(self.admin.wallet.balance, Decimal('600.00'))
        self.assertEqual(self.player.wallet.balance, Decimal('170.00'))
        crm_log = CRMActionLog.objects.filter(target_user=self.player, action_type='WALLET_CREDITED').latest('created_at')
        self.assertEqual(crm_log.actor, self.admin)
        self.assertEqual(crm_log.data.get('approved_by_role'), 'superadmin')
        self.assertEqual(crm_log.data.get('funding_mode'), 'superadmin_override')
        changelist = self.client.get(reverse('betting_admin:betting_crmwalletapprovalrequest_changelist'))
        self.assertContains(changelist, 'No wallet debit')

    def test_admin_can_approve_crm_credit_by_debiting_selected_account_user_wallet(self):
        Wallet.objects.filter(user=self.account_user_two).update(balance=Decimal('500.00'))
        Wallet.objects.filter(user=self.player).update(balance=Decimal('40.00'))

        credit_req = CreditRequest.objects.create(
            requester=self.player,
            recipient=self.account_user,
            amount=Decimal('150.00'),
            reason='Admin selected account user source',
            request_type='crm_credit',
            status='pending',
        )

        self.client.force_login(self.plain_admin)
        process_url = reverse('betting_admin:betting_crmwalletapprovalrequest_process', args=[credit_req.id, 'approve'])
        resp = self.client.post(process_url, {'account_user_wallet_user_id': str(self.account_user_two.id)})
        self.assertNotEqual(resp.status_code, 500)

        credit_req.refresh_from_db()
        self.assertEqual(credit_req.status, 'approved')
        self.account_user_two.wallet.refresh_from_db()
        self.player.wallet.refresh_from_db()
        self.assertEqual(self.account_user_two.wallet.balance, Decimal('350.00'))
        self.assertEqual(self.player.wallet.balance, Decimal('190.00'))
        crm_log = CRMActionLog.objects.filter(target_user=self.player, action_type='WALLET_CREDITED').latest('created_at')
        self.assertEqual(crm_log.actor, self.plain_admin)
        self.assertEqual(crm_log.data.get('approved_by_role'), 'admin')
        self.assertEqual(crm_log.data.get('funding_account_user_email'), self.account_user_two.email)
        self.client.force_login(self.admin)
        changelist = self.client.get(reverse('betting_admin:betting_crmwalletapprovalrequest_changelist'))
        self.assertContains(changelist, self.account_user_two.email)

    def test_admin_can_approve_crm_debit_and_reimburse_selected_account_user(self):
        Wallet.objects.filter(user=self.account_user_two).update(balance=Decimal('75.00'))
        Wallet.objects.filter(user=self.player).update(balance=Decimal('300.00'))

        credit_req = CreditRequest.objects.create(
            requester=self.player,
            recipient=self.account_user,
            amount=Decimal('120.00'),
            reason='Admin debit with reimbursement',
            request_type='crm_debit',
            status='pending',
        )

        self.client.force_login(self.plain_admin)
        process_url = reverse('betting_admin:betting_crmwalletapprovalrequest_process', args=[credit_req.id, 'approve'])
        resp = self.client.post(process_url, {'account_user_wallet_user_id': str(self.account_user_two.id)})
        self.assertNotEqual(resp.status_code, 500)

        credit_req.refresh_from_db()
        self.assertEqual(credit_req.status, 'approved')
        self.account_user_two.wallet.refresh_from_db()
        self.player.wallet.refresh_from_db()
        self.assertEqual(self.account_user_two.wallet.balance, Decimal('195.00'))
        self.assertEqual(self.player.wallet.balance, Decimal('180.00'))
        crm_log = CRMActionLog.objects.filter(target_user=self.player, action_type='WALLET_DEBITED').latest('created_at')
        self.assertEqual(crm_log.actor, self.plain_admin)
        self.assertEqual(crm_log.data.get('approved_by_role'), 'admin')
        self.assertEqual(crm_log.data.get('reimbursement_account_user_email'), self.account_user_two.email)
        self.client.force_login(self.admin)
        changelist = self.client.get(reverse('betting_admin:betting_crmwalletapprovalrequest_changelist'))
        self.assertContains(changelist, self.account_user_two.email)

    def test_crm_agent_detail_includes_downline_tickets_and_withdrawal_reports(self):
        self.player.agent = self.agent
        self.player.save(update_fields=['agent'])

        ticket = BetTicket.objects.create(
            user=self.player,
            stake_amount=Decimal('50.00'),
            total_odd=Decimal('2.00'),
            potential_winning=Decimal('100.00'),
            max_winning=Decimal('100.00'),
            status='pending',
        )
        withdrawal = UserWithdrawal.objects.create(
            user=self.player,
            amount=Decimal('30.00'),
            bank_name='Test Bank',
            account_name='Player Test',
            account_number='0123456789',
            status='pending',
        )
        report = WithdrawalReport.objects.create(
            withdrawal=withdrawal,
            user=self.player,
            username=self.player.email,
            amount=withdrawal.amount,
            bank_name=withdrawal.bank_name,
            account_name=withdrawal.account_name,
            account_number=withdrawal.account_number,
            requested_at=withdrawal.request_time,
            updated_at=withdrawal.request_time,
            transaction_reference='CRMTEST123',
            withdrawal_status=withdrawal.status,
            event='requested',
            is_admin_copy=False,
        )

        self.client.force_login(self.crm_viewer)
        resp = self.client.get(reverse('betting:crm_user_detail', args=[self.agent.id]))
        self.assertEqual(resp.status_code, 200)
        ticket_ids = {t.id for t in resp.context['tickets']}
        withdrawal_ids = {w.id for w in resp.context['withdrawals']}
        report_ids = {r.id for r in resp.context['withdrawal_reports']}
        self.assertIn(ticket.id, ticket_ids)
        self.assertIn(withdrawal.id, withdrawal_ids)
        self.assertIn(report.id, report_ids)
