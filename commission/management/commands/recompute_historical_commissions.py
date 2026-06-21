from django.core.management.base import BaseCommand, CommandError
from django.utils.dateparse import parse_date

from commission.models import MonthlyNetworkCommission, WeeklyAgentCommission
from commission.services import (
    recompute_saved_monthly_commission_record,
    recompute_saved_weekly_commission_record,
)


class Command(BaseCommand):
    help = "Recompute saved weekly and monthly commission records using current commission rules."

    def add_arguments(self, parser):
        parser.add_argument(
            "--weekly",
            action="store_true",
            help="Recompute saved WeeklyAgentCommission records only.",
        )
        parser.add_argument(
            "--monthly",
            action="store_true",
            help="Recompute saved MonthlyNetworkCommission records only.",
        )
        parser.add_argument(
            "--start-date",
            help="Optional inclusive period start-date filter in YYYY-MM-DD format.",
        )
        parser.add_argument(
            "--end-date",
            help="Optional inclusive period end-date filter in YYYY-MM-DD format.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Preview impacted records without saving changes.",
        )

    def handle(self, *args, **options):
        start_date = self._parse_date(options.get("start_date"), "--start-date")
        end_date = self._parse_date(options.get("end_date"), "--end-date")
        if start_date and end_date and start_date > end_date:
            raise CommandError("--start-date cannot be later than --end-date.")

        run_weekly = options["weekly"] or not options["monthly"]
        run_monthly = options["monthly"] or not options["weekly"]
        dry_run = bool(options["dry_run"])

        if dry_run:
            self.stdout.write(self.style.WARNING("Dry run mode: no changes will be saved."))

        if run_weekly:
            self._recompute_weekly(start_date=start_date, end_date=end_date, dry_run=dry_run)
        if run_monthly:
            self._recompute_monthly(start_date=start_date, end_date=end_date, dry_run=dry_run)

        self.stdout.write(self.style.SUCCESS("Historical commission recomputation completed."))

    def _parse_date(self, value, flag_name):
        if not value:
            return None
        parsed = parse_date(value)
        if parsed is None:
            raise CommandError(f"Invalid {flag_name} value: {value}. Use YYYY-MM-DD.")
        return parsed

    def _weekly_queryset(self, *, start_date=None, end_date=None):
        qs = WeeklyAgentCommission.objects.select_related("agent", "period")
        if start_date:
            qs = qs.filter(period__start_date__gte=start_date)
        if end_date:
            qs = qs.filter(period__end_date__lte=end_date)
        return qs.order_by("period__start_date", "agent_id", "id")

    def _monthly_queryset(self, *, start_date=None, end_date=None):
        qs = MonthlyNetworkCommission.objects.select_related("user", "period")
        if start_date:
            qs = qs.filter(period__start_date__gte=start_date)
        if end_date:
            qs = qs.filter(period__end_date__lte=end_date)
        return qs.order_by("period__start_date", "user_id", "id")

    def _recompute_weekly(self, *, start_date=None, end_date=None, dry_run=False):
        qs = self._weekly_queryset(start_date=start_date, end_date=end_date)
        total = qs.count()
        updated = 0
        overpaid = 0

        self.stdout.write(f"Recomputing {total} saved weekly commission record(s)...")
        for record in qs.iterator():
            result = recompute_saved_weekly_commission_record(record, persist=not dry_run)
            if result["updated"]:
                updated += 1
            if result["overpaid"]:
                overpaid += 1

        style = self.style.WARNING if overpaid else self.style.SUCCESS
        self.stdout.write(
            style(
                f"Weekly summary: total={total}, changed={updated}, overpaid_after_recompute={overpaid}"
            )
        )

    def _recompute_monthly(self, *, start_date=None, end_date=None, dry_run=False):
        qs = self._monthly_queryset(start_date=start_date, end_date=end_date)
        total = qs.count()
        updated = 0
        overpaid = 0

        self.stdout.write(f"Recomputing {total} saved monthly commission record(s)...")
        for record in qs.iterator():
            result = recompute_saved_monthly_commission_record(record, persist=not dry_run)
            if result["updated"]:
                updated += 1
            if result["overpaid"]:
                overpaid += 1

        style = self.style.WARNING if overpaid else self.style.SUCCESS
        self.stdout.write(
            style(
                f"Monthly summary: total={total}, changed={updated}, overpaid_after_recompute={overpaid}"
            )
        )
