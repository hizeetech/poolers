from django.core.management.base import BaseCommand

from betting.services.ticket_refund_reversal_adjustments import (
    backfill_incorrect_refund_reversal_adjustments,
)


class Command(BaseCommand):
    help = (
        "Backfill compensating wallet credits for historical incorrect "
        "ticket refund reversal debits created after result correction."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Preview how many affected refund reversals would be adjusted without saving changes.",
        )

    def handle(self, *args, **options):
        dry_run = bool(options.get("dry_run"))
        if dry_run:
            self.stdout.write("Dry run mode: no changes will be saved.")

        summary = backfill_incorrect_refund_reversal_adjustments(dry_run=dry_run)
        self.stdout.write(
            "Refund reversal adjustment summary: "
            f"scanned={summary['scanned']}, "
            f"eligible={summary['eligible']}, "
            f"adjusted={summary['adjusted']}, "
            f"already_adjusted={summary['already_adjusted']}, "
            f"skipped={summary['skipped']}"
        )
