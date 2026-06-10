from datetime import date

from django.test import TestCase

from commission.tasks import (
    ensure_last_completed_weekly_commission_period_for_date,
    ensure_weekly_commission_period_for_date,
    get_current_weekly_period_bounds,
    get_last_completed_weekly_period_bounds,
)


class WeeklyCommissionPeriodTests(TestCase):
    def test_current_weekly_bounds_match_requested_wednesday_rule(self):
        start_date, end_date = get_current_weekly_period_bounds(date(2026, 6, 10))
        self.assertEqual(start_date, date(2026, 6, 9))
        self.assertEqual(end_date, date(2026, 6, 15))

        next_start, next_end = get_current_weekly_period_bounds(date(2026, 6, 17))
        self.assertEqual(next_start, date(2026, 6, 16))
        self.assertEqual(next_end, date(2026, 6, 22))

    def test_last_completed_weekly_bounds_remain_available_for_processing(self):
        start_date, end_date = get_last_completed_weekly_period_bounds(date(2026, 6, 10))
        self.assertEqual(start_date, date(2026, 6, 2))
        self.assertEqual(end_date, date(2026, 6, 8))

    def test_period_creation_helpers_use_current_and_completed_windows(self):
        current_period, current_created = ensure_weekly_commission_period_for_date(date(2026, 6, 10))
        completed_period, completed_created = ensure_last_completed_weekly_commission_period_for_date(date(2026, 6, 10))

        self.assertTrue(current_created)
        self.assertTrue(completed_created)
        self.assertEqual(current_period.start_date, date(2026, 6, 9))
        self.assertEqual(current_period.end_date, date(2026, 6, 15))
        self.assertEqual(completed_period.start_date, date(2026, 6, 2))
        self.assertEqual(completed_period.end_date, date(2026, 6, 8))
