from django.core.management.base import BaseCommand

from betting.tasks import run_scheduled_finance_reports


class Command(BaseCommand):
    def handle(self, *args, **options):
        ran = run_scheduled_finance_reports()
        self.stdout.write(self.style.SUCCESS(f"Ran {ran} scheduled finance report(s)."))

