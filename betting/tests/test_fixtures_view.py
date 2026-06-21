from datetime import timedelta

from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from betting.models import BettingPeriod, Fixture


class FixturesViewThemeTests(TestCase):
    def setUp(self):
        self.client = Client()
        today = timezone.localdate()
        self.period = BettingPeriod.objects.create(
            name="Australia Championship Week 51 : RED",
            start_date=today - timedelta(days=1),
            end_date=today + timedelta(days=6),
            is_active=True,
            fixture_theme_color="#d11a2a",
        )
        Fixture.objects.create(
            betting_period=self.period,
            serial_number=1,
            home_team="Blacktown City",
            away_team="Apia Leichhardt",
            match_date=today + timedelta(days=1),
            match_time=timezone.localtime().time().replace(hour=18, minute=0, second=0, microsecond=0),
            status="scheduled",
            is_active=True,
            draw_odd="3.20",
        )

    def test_fixtures_view_uses_betting_period_theme_color(self):
        response = self.client.get(reverse("betting:fixtures_with_period", args=[self.period.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "--fixture-period-bg: #d11a2a;")
        self.assertContains(response, "fixture-period-accent text-center rounded-top-3")
        self.assertContains(response, "fixture-period-accent-badge")
        self.assertContains(response, 'id="place-bet-btn" class="btn fixture-period-accent-button')
        self.assertContains(response, "fixture-period-odd-button")
        self.assertContains(response, "smart-pick-number.is-selected[data-tt-type=")
