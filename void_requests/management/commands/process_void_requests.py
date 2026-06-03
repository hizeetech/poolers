from django.core.management.base import BaseCommand

from void_requests.services import process_due_void_requests


class Command(BaseCommand):
    help = "Process pending ticket void requests that have passed their auto-void time."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=200)

    def handle(self, *args, **options):
        limit = options.get("limit") or 200
        processed = process_due_void_requests(limit=limit)
        self.stdout.write(str(processed))

