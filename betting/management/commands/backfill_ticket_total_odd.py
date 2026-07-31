from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction

from betting.models import BetTicket


class Command(BaseCommand):
    help = "Backfill BetTicket.total_odd where missing/invalid (<= 0)."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=0)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        limit = int(options.get("limit") or 0)
        dry_run = bool(options.get("dry_run"))

        qs = BetTicket.objects.filter(total_odd__lte=Decimal("0.00")).order_by("placed_at")
        if limit > 0:
            qs = qs[:limit]

        updated = 0
        scanned = 0

        for ticket in qs.iterator(chunk_size=500):
            scanned += 1
            try:
                computed = ticket.get_display_total_odd()
            except Exception:
                continue

            try:
                computed = Decimal(str(computed)).quantize(Decimal("0.01"))
            except Exception:
                continue

            if computed <= Decimal("0.00"):
                continue

            if dry_run:
                updated += 1
                continue

            try:
                with transaction.atomic():
                    locked = BetTicket.objects.select_for_update().get(pk=ticket.pk)
                    if Decimal(str(locked.total_odd or "0.00")) > Decimal("0.00"):
                        continue
                    locked.total_odd = computed
                    locked.save(update_fields=["total_odd", "last_updated"])
                    updated += 1
            except Exception:
                continue

        self.stdout.write(f"Scanned: {scanned}")
        self.stdout.write(f"Updated: {updated}{' (dry-run)' if dry_run else ''}")

