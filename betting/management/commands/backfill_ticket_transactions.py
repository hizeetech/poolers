from django.core.management.base import BaseCommand

from betting.services.ticket_transaction_ledger import backfill_ticket_transaction_ledgers


class Command(BaseCommand):
    help = "Backfill TicketTransactionLedger from historical wallet and transaction activity."

    def handle(self, *args, **options):
        processed = backfill_ticket_transaction_ledgers()
        self.stdout.write(self.style.SUCCESS(f"Ticket transaction ledger backfill completed. Processed {processed} event(s)."))
