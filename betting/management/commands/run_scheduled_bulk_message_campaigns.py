from django.core.management.base import BaseCommand

from betting.views import process_due_bulk_message_campaigns


class Command(BaseCommand):
    help = "Process due CRM bulk message campaigns."

    def add_arguments(self, parser):
        parser.add_argument(
            "--limit",
            type=int,
            default=20,
            help="Maximum number of scheduled campaigns to process in one run.",
        )

    def handle(self, *args, **options):
        limit = max(1, int(options.get("limit") or 20))
        ran = process_due_bulk_message_campaigns(limit=limit)
        self.stdout.write(self.style.SUCCESS(f"Ran {ran} scheduled bulk message campaign(s)."))
