from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from betting.models import BetTicket, User, Wallet


class CheckTicketViewTests(TestCase):
    def setUp(self):
        self.password = "password123"
        self.agent = User.objects.create_user(
            email="check-agent@test.com",
            password=self.password,
            user_type="agent",
            username="check_agent",
        )
        self.cashier = User.objects.create_user(
            email="check-cashier@test.com",
            password=self.password,
            user_type="cashier",
            username="check_cashier",
            agent=self.agent,
        )
        Wallet.objects.create(user=self.agent, balance=Decimal("0.00"))
        Wallet.objects.create(user=self.cashier, balance=Decimal("0.00"))

    def _create_tickets(self, count=55):
        tickets = []
        for index in range(count):
            ticket = BetTicket.objects.create(
                user=self.cashier,
                stake_amount=Decimal("100.00"),
                total_odd=Decimal("2.00"),
                potential_winning=Decimal("200.00"),
                min_winning=Decimal("0.00"),
                max_winning=Decimal("200.00"),
                status="pending",
                bet_type="single",
                original_selections_count=1,
            )
            tickets.append(ticket)
        return tickets

    def test_cashier_recent_tickets_are_paginated_without_mobile_hide_rows(self):
        tickets = self._create_tickets()
        self.client.force_login(self.cashier)

        first_response = self.client.get(reverse("betting:check_ticket_status"))
        second_response = self.client.get(reverse("betting:check_ticket_status"), {"page": 2})

        self.assertEqual(first_response.status_code, 200)
        self.assertContains(first_response, "My Recent Tickets")
        self.assertContains(first_response, "Page 1 of 2")
        self.assertNotContains(first_response, "recent-ticket-history-hide")
        self.assertEqual(first_response.context["ticket_page_numbers"], [1, 2])
        self.assertContains(first_response, "page=2")
        self.assertEqual(first_response.context["tickets_page"].paginator.count, 55)
        self.assertEqual(len(first_response.context["tickets"]), 50)
        self.assertContains(second_response, "Page 2 of 2")
        self.assertContains(second_response, tickets[0].ticket_id)
        self.assertNotContains(second_response, tickets[-1].ticket_id)

    def test_agent_downline_tickets_are_paginated_for_mobile_and_desktop(self):
        tickets = self._create_tickets()
        self.client.force_login(self.agent)

        response = self.client.get(reverse("betting:check_ticket_status"), {"page": 2})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Downline Tickets")
        self.assertContains(response, "Page 2 of 2")
        self.assertEqual(response.context["ticket_page_numbers"], [1, 2])
        self.assertEqual(response.context["tickets_page"].paginator.count, 55)
        self.assertEqual(len(response.context["tickets"]), 5)
        self.assertContains(response, tickets[0].ticket_id)
        self.assertNotContains(response, tickets[-1].ticket_id)
