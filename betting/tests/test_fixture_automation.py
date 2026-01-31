from django.test import TestCase
from django.utils import timezone
from betting.models import Fixture, BettingPeriod
from betting.tasks import update_started_fixtures_status
from datetime import timedelta
from django.contrib.auth import get_user_model

User = get_user_model()

class FixtureAutomationTests(TestCase):
    def setUp(self):
        self.period = BettingPeriod.objects.create(
            name="Test Period",
            start_date=timezone.now().date(),
            end_date=timezone.now().date() + timedelta(days=7),
            is_active=True
        )
        
        # Future Fixture (Tomorrow)
        self.future_fixture = Fixture.objects.create(
            betting_period=self.period,
            home_team="Future Home",
            away_team="Future Away",
            match_date=timezone.now().date() + timedelta(days=1),
            match_time="12:00:00",
            status="scheduled",
            is_active=True,
            serial_number="101"
        )
        
        # Past Fixture (Yesterday)
        # Using yesterday guarantees it's in the past regardless of time
        past_date = timezone.now().date() - timedelta(days=1)
        self.past_fixture = Fixture.objects.create(
            betting_period=self.period,
            home_team="Past Home",
            away_team="Past Away",
            match_date=past_date,
            match_time="12:00:00",
            status="scheduled",
            is_active=True,
            serial_number="102"
        )
        
        # Just Started Fixture (1 minute ago)
        # Need careful handling of timezones here.
        # Tests run with override_settings usually, but let's trust logic.
        local_now = timezone.localtime(timezone.now())
        just_started_time = local_now - timedelta(minutes=10)
        
        self.started_fixture = Fixture.objects.create(
            betting_period=self.period,
            home_team="Started Home",
            away_team="Started Away",
            match_date=just_started_time.date(),
            match_time=just_started_time.time(),
            status="scheduled",
            is_active=True,
            serial_number="103"
        )

    def test_view_filtering_logic(self):
        """Test that the view helper filters out past fixtures."""
        from betting.views import _get_fixtures_data
        
        fixtures, _ = _get_fixtures_data()
        
        # Future fixture should be visible
        self.assertTrue(fixtures.filter(id=self.future_fixture.id).exists())
        
        # Past fixture should be hidden
        self.assertFalse(fixtures.filter(id=self.past_fixture.id).exists())
        
        # Just started fixture should be hidden
        self.assertFalse(fixtures.filter(id=self.started_fixture.id).exists())

    def test_task_updates_status(self):
        """Test that the Celery task updates status and deactivates started fixtures."""
        update_started_fixtures_status()
        
        self.past_fixture.refresh_from_db()
        self.started_fixture.refresh_from_db()
        self.future_fixture.refresh_from_db()
        
        # Past fixture should be deactivated and set to live
        self.assertFalse(self.past_fixture.is_active)
        self.assertEqual(self.past_fixture.status, 'live')
        
        # Started fixture should be deactivated and set to live
        self.assertFalse(self.started_fixture.is_active)
        self.assertEqual(self.started_fixture.status, 'live')
        
        # Future fixture should remain untouched
        self.assertTrue(self.future_fixture.is_active)
        self.assertEqual(self.future_fixture.status, 'scheduled')
