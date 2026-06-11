from decimal import Decimal
from unittest.mock import patch

from django.test import RequestFactory, TestCase
from django.urls import reverse

from betting.admin import BetTicketAdmin, betting_admin_site
from betting.models import BetTicket, User, Wallet
from void_requests.models import TicketVoidRequest
from void_requests.services import approve_and_void_request


class TicketVoidCommissionRefreshTests(TestCase):
    def setUp(self):
        self.client = self.client_class()
        self.factory = RequestFactory()
        self.password = "password123"

        self.agent = User.objects.create_user(
            email="agent-void@test.com",
            password=self.password,
            user_type="agent",
        )
        self.cashier = User.objects.create_user(
            email="cashier-void@test.com",
            password=self.password,
            user_type="cashier",
            agent=self.agent,
        )
        self.admin = User.objects.create_user(
            email="admin-void@test.com",
            password=self.password,
            user_type="admin",
            is_staff=True,
            is_superuser=True,
        )

        Wallet.objects.create(user=self.cashier, balance=Decimal("0.00"))
        Wallet.objects.create(user=self.agent, balance=Decimal("0.00"))
        Wallet.objects.create(user=self.admin, balance=Decimal("0.00"))

    def _make_ticket(self):
        return BetTicket.objects.create(
            user=self.cashier,
            stake_amount=Decimal("100.00"),
            total_odd=Decimal("2.00"),
            potential_winning=Decimal("200.00"),
            max_winning=Decimal("200.00"),
            status="pending",
            bet_type="single",
            original_selections_count=1,
        )

    @patch("commission.tasks.enqueue_refresh_weekly_commissions_for_ticket_ids")
    def test_agent_void_ticket_triggers_weekly_commission_refresh(self, mock_enqueue):
        ticket = self._make_ticket()
        self.client.force_login(self.agent)

        response = self.client.post(reverse("betting:agent_void_ticket", args=[ticket.ticket_id]))

        self.assertEqual(response.status_code, 302)
        ticket.refresh_from_db()
        self.assertEqual(ticket.status, "cancelled")
        mock_enqueue.assert_called_once_with([str(ticket.id)])

    @patch("commission.tasks.enqueue_refresh_weekly_commissions_for_ticket_ids")
    def test_admin_void_ticket_single_triggers_weekly_commission_refresh(self, mock_enqueue):
        ticket = self._make_ticket()
        self.client.force_login(self.admin)

        response = self.client.post(reverse("betting:admin_void_ticket_single", args=[ticket.id]))

        self.assertEqual(response.status_code, 302)
        ticket.refresh_from_db()
        self.assertEqual(ticket.status, "deleted")
        mock_enqueue.assert_called_once_with([str(ticket.id)])

    @patch("betting.admin.views.log_admin_activity")
    @patch("betting.admin.messages.error")
    @patch("betting.admin.messages.warning")
    @patch("betting.admin.messages.success")
    @patch("commission.tasks.enqueue_refresh_weekly_commissions_for_ticket_ids")
    def test_admin_bulk_void_triggers_weekly_commission_refresh(
        self,
        mock_enqueue,
        _mock_success,
        _mock_warning,
        _mock_error,
        _mock_log,
    ):
        ticket_one = self._make_ticket()
        ticket_two = self._make_ticket()
        request = self.factory.post("/admin/betting/betticket/")
        request.user = self.admin

        admin_instance = BetTicketAdmin(BetTicket, betting_admin_site)
        queryset = BetTicket.objects.filter(id__in=[ticket_one.id, ticket_two.id]).order_by("id")

        admin_instance.void_selected_tickets(request, queryset)

        ticket_one.refresh_from_db()
        ticket_two.refresh_from_db()
        self.assertEqual(ticket_one.status, "deleted")
        self.assertEqual(ticket_two.status, "deleted")
        mock_enqueue.assert_called_once()
        self.assertEqual(
            set(mock_enqueue.call_args.args[0]),
            {str(ticket_one.id), str(ticket_two.id)},
        )

    @patch("void_requests.services.create_notification")
    @patch("commission.tasks.enqueue_refresh_weekly_commissions_for_ticket_ids")
    def test_void_request_approval_triggers_weekly_commission_refresh(self, mock_enqueue, _mock_notification):
        ticket = self._make_ticket()
        void_request = TicketVoidRequest.objects.create(
            ticket=ticket,
            cashier=self.cashier,
            agent=self.agent,
            status=TicketVoidRequest.STATUS_PENDING,
            is_processed=False,
        )

        approve_and_void_request(void_request_id=void_request.id, approved_by=self.admin, is_auto=False)

        ticket.refresh_from_db()
        self.assertEqual(ticket.status, "deleted")
        mock_enqueue.assert_called_once_with([str(ticket.id)])

