from decimal import Decimal

from django.contrib.admin.sites import AdminSite
from django.test import RequestFactory, TestCase
from django.urls import reverse

from betting.admin import BetTicketAdmin, TicketSelectionCountFilter
from betting.models import BetTicket, Selection, User, Wallet


class BetTicketAdminTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.site = AdminSite()
        self.admin = BetTicketAdmin(BetTicket, self.site)
        self.password = "password123"

        self.user = User.objects.create_user(
            email="betticket-admin@test.com",
            password=self.password,
            user_type="cashier",
            username="betticket_admin_cashier",
        )
        self.admin_user = User.objects.create_user(
            email="betticket-super@test.com",
            password=self.password,
            user_type="admin",
            username="betticket_admin_root",
            is_staff=True,
            is_superuser=True,
        )
        Wallet.objects.create(user=self.user, balance=Decimal("0.00"))
        Wallet.objects.create(user=self.admin_user, balance=Decimal("0.00"))

    def _create_ticket(self, *, bet_type="single", original_selections_count=None):
        return BetTicket.objects.create(
            user=self.user,
            stake_amount=Decimal("100.00"),
            total_odd=Decimal("2.00"),
            potential_winning=Decimal("200.00"),
            min_winning=Decimal("0.00"),
            max_winning=Decimal("200.00"),
            status="pending",
            bet_type=bet_type,
            original_selections_count=original_selections_count,
        )

    def test_selection_count_uses_original_selections_count_when_present(self):
        ticket = self._create_ticket(bet_type="multiple", original_selections_count=4)
        annotated_ticket = self.admin.get_queryset(self.factory.get("/admin/betting/betticket/")).get(pk=ticket.pk)

        self.assertEqual(self.admin.selection_count(annotated_ticket), 4)

    def test_selection_count_falls_back_to_related_selection_rows(self):
        ticket = self._create_ticket(bet_type="multiple", original_selections_count=None)
        Selection.objects.create(
            bet_ticket=ticket,
            fixture_home_team="Team A",
            fixture_away_team="Team B",
            bet_type="home_win",
            odd_selected=Decimal("1.50"),
        )
        Selection.objects.create(
            bet_ticket=ticket,
            fixture_home_team="Team C",
            fixture_away_team="Team D",
            bet_type="away_win",
            odd_selected=Decimal("2.10"),
        )
        annotated_ticket = self.admin.get_queryset(self.factory.get("/admin/betting/betticket/")).get(pk=ticket.pk)

        self.assertEqual(self.admin.selection_count(annotated_ticket), 2)

    def test_single_and_multiple_filter_split_ticket_queryset_by_selection_count(self):
        single_ticket = self._create_ticket(bet_type="single", original_selections_count=1)
        multiple_ticket = self._create_ticket(bet_type="multiple", original_selections_count=3)
        system_ticket = self._create_ticket(bet_type="system", original_selections_count=4)
        self.client.force_login(self.admin_user)

        single_response = self.client.get(
            reverse("betting_admin:betting_betticket_changelist"),
            {"selection_type": "single"},
        )
        multiple_response = self.client.get(
            reverse("betting_admin:betting_betticket_changelist"),
            {"selection_type": "multiple"},
        )
        system_response = self.client.get(
            reverse("betting_admin:betting_betticket_changelist"),
            {"selection_type": "system"},
        )

        self.assertEqual(single_response.status_code, 200)
        self.assertEqual(multiple_response.status_code, 200)
        self.assertEqual(system_response.status_code, 200)
        self.assertEqual(TicketSelectionCountFilter.title, "Single / Multiple / System")
        self.assertQuerySetEqual(
            single_response.context["cl"].queryset.order_by("id"),
            [single_ticket],
            transform=lambda obj: obj,
        )
        self.assertQuerySetEqual(
            multiple_response.context["cl"].queryset.order_by("id"),
            [multiple_ticket],
            transform=lambda obj: obj,
        )
        self.assertQuerySetEqual(
            system_response.context["cl"].queryset.order_by("id"),
            [system_ticket],
            transform=lambda obj: obj,
        )

    def test_betticket_changelist_includes_websocket_live_refresh_script(self):
        self._create_ticket()
        self.client.force_login(self.admin_user)

        response = self.client.get(reverse("betting_admin:betting_betticket_changelist"))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "betting/admin/betticket_change_list.html")
        self.assertContains(response, "refreshBetTicketList")
        self.assertContains(response, "/ws/admin/betticket/")
        self.assertContains(response, "connectSocket")
