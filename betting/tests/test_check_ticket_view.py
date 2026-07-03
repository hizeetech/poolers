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

    def test_reprint_ticket_json_includes_min_winning_for_system_tickets(self):
        ticket = BetTicket.objects.create(
            user=self.cashier,
            stake_amount=Decimal("2000.00"),
            total_odd=Decimal("0.00"),
            potential_winning=Decimal("225772.00"),
            min_winning=Decimal("11444.40"),
            max_winning=Decimal("230287.44"),
            bonus_amount=Decimal("4515.44"),
            bonus_is_final=True,
            status="pending",
            bet_type="system",
            system_min_count=3,
            original_selections_count=5,
        )
        self.client.force_login(self.agent)

        response = self.client.get(
            reverse("betting:get_ticket_details_json"),
            {"ticket_id": ticket.ticket_id, "mode": "reprint"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["success"])
        self.assertEqual(payload["ticket"]["bet_type"], "system")
        self.assertEqual(payload["ticket"]["min_winning"], 11444.4)

    def test_check_ticket_page_reprint_modal_contains_original_receipt_labels(self):
        self._create_tickets(count=1)
        self.client.force_login(self.agent)

        response = self.client.get(reverse("betting:check_ticket_status"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "POT. WIN")
        self.assertContains(response, "MIN WIN")
        self.assertContains(response, "TOTAL WIN")
        self.assertNotContains(response, "SETTLED WINNING")
        self.assertNotContains(response, "TOTAL PAYOUT")
        self.assertContains(response, "REPRINTED COPY")
        self.assertContains(response, "ticket-reprint-watermark")
