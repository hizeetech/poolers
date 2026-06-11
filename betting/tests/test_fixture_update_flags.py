from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from betting.models import BettingPeriod, Fixture


class FixtureUpdateFlagTests(TestCase):
    def setUp(self):
        today = timezone.localdate()
        self.period = BettingPeriod.objects.create(
            name="Fixture Update Flags Period",
            start_date=today,
            end_date=today + timedelta(days=7),
            is_active=True,
        )
        self.fixture = Fixture.objects.create(
            betting_period=self.period,
            serial_number=1,
            home_team="Home",
            away_team="Away",
            match_date=today + timedelta(days=1),
            match_time=timezone.localtime(timezone.now()).time().replace(second=0, microsecond=0),
            status="scheduled",
            is_active=True,
            draw_odd="3.50",
        )

    def test_flags_not_set_on_create(self):
        self.fixture.refresh_from_db()
        self.assertIsNone(self.fixture.datetime_updated_at)
        self.assertIsNone(self.fixture.odds_updated_at)

    def test_datetime_updated_at_set_when_match_time_changes(self):
        self.fixture.match_time = (timezone.localtime(timezone.now()) + timedelta(hours=1)).time().replace(second=0, microsecond=0)
        self.fixture.save()
        self.fixture.refresh_from_db()
        self.assertIsNotNone(self.fixture.datetime_updated_at)
        self.assertIsNone(self.fixture.odds_updated_at)

    def test_odds_updated_at_set_when_odds_change(self):
        self.fixture.draw_odd = "3.60"
        self.fixture.save()
        self.fixture.refresh_from_db()
        self.assertIsNone(self.fixture.datetime_updated_at)
        self.assertIsNotNone(self.fixture.odds_updated_at)

