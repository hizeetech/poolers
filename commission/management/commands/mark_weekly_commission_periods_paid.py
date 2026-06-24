from django.core.management.base import BaseCommand, CommandError
from django.utils.dateparse import parse_date

from commission.models import CommissionPeriod
from commission.services import mark_weekly_commission_period_paid_without_payout


class Command(BaseCommand):
    help = "Mark selected weekly commission periods as paid without creating new wallet credits."

    def add_arguments(self, parser):
        parser.add_argument(
            "--period-id",
            action="append",
            dest="period_ids",
            default=[],
            help="Weekly CommissionPeriod id to mark as paid. Repeat for multiple periods.",
        )
        parser.add_argument(
            "--period-range",
            action="append",
            dest="period_ranges",
            default=[],
            help="Weekly period range in YYYY-MM-DD:YYYY-MM-DD format. Repeat for multiple periods.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Preview matched periods without saving changes.",
        )

    def handle(self, *args, **options):
        period_ids = options.get("period_ids") or []
        period_ranges = options.get("period_ranges") or []
        dry_run = bool(options.get("dry_run"))

        periods = self._resolve_periods(period_ids=period_ids, period_ranges=period_ranges)
        if not periods:
            raise CommandError("Provide at least one --period-id or --period-range.")

        if dry_run:
            self.stdout.write(self.style.WARNING("Dry run mode: no changes will be saved."))
            for period in periods:
                self.stdout.write(f"Would mark weekly period as paid: {period} (id={period.id})")
            return

        for period in periods:
            result = mark_weekly_commission_period_paid_without_payout(period)
            self.stdout.write(
                self.style.SUCCESS(
                    f"Marked {period} as paid without payout: "
                    f"created={result['created_count']}, updated={result['updated_count']}, total={result['total_count']}"
                )
            )

    def _resolve_periods(self, *, period_ids, period_ranges):
        periods = []
        seen = set()

        for raw_id in period_ids:
            try:
                period_id = int(str(raw_id).strip())
            except Exception as exc:
                raise CommandError(f"Invalid --period-id value: {raw_id}") from exc
            period = CommissionPeriod.objects.filter(id=period_id, period_type="weekly").first()
            if not period:
                raise CommandError(f"Weekly CommissionPeriod not found for id={period_id}.")
            if period.id not in seen:
                periods.append(period)
                seen.add(period.id)

        for raw_range in period_ranges:
            start_date, end_date = self._parse_period_range(raw_range)
            period = CommissionPeriod.objects.filter(
                period_type="weekly",
                start_date=start_date,
                end_date=end_date,
            ).first()
            if not period:
                raise CommandError(
                    f"Weekly CommissionPeriod not found for range {start_date} to {end_date}."
                )
            if period.id not in seen:
                periods.append(period)
                seen.add(period.id)

        return periods

    def _parse_period_range(self, raw_value):
        value = (raw_value or "").strip()
        if ":" not in value:
            raise CommandError(
                f"Invalid --period-range value: {raw_value}. Use YYYY-MM-DD:YYYY-MM-DD."
            )
        start_raw, end_raw = value.split(":", 1)
        start_date = parse_date(start_raw.strip())
        end_date = parse_date(end_raw.strip())
        if start_date is None or end_date is None:
            raise CommandError(
                f"Invalid --period-range value: {raw_value}. Use YYYY-MM-DD:YYYY-MM-DD."
            )
        if start_date > end_date:
            raise CommandError(
                f"Invalid --period-range value: {raw_value}. Start date cannot be after end date."
            )
        return start_date, end_date
