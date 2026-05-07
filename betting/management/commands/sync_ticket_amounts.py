from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction

from betting.models import BetTicket, Transaction, User


class Command(BaseCommand):
    help = "Sync settled ticket amounts from transactions and normalize lost tickets to zero winnings."

    def add_arguments(self, parser):
        parser.add_argument("--username", type=str, default=None)
        parser.add_argument("--ticket-id", type=str, default=None)
        parser.add_argument("--dry-run", action="store_true", default=False)

    def handle(self, *args, **options):
        username = options["username"]
        ticket_id = options["ticket_id"]
        dry_run = options["dry_run"]

        qs = BetTicket.objects.all()

        if username:
            user = User.objects.filter(username__iexact=username).first()
            if not user:
                self.stdout.write(self.style.ERROR(f"User not found: {username}"))
                return
            qs = qs.filter(user=user)

        if ticket_id:
            qs = qs.filter(ticket_id__iexact=ticket_id)

        qs = qs.filter(status__in=["won", "lost", "cancelled", "deleted", "cashed_out"]).order_by("placed_at")

        updated = 0
        inspected = 0

        for ticket in qs.iterator():
            inspected += 1

            new_potential = None
            new_max = None

            if ticket.status == "lost":
                new_potential = Decimal("0.00")
                new_max = Decimal("0.00")
            elif ticket.status in ["won", "cashed_out"]:
                payout = (
                    Transaction.objects.filter(
                        related_bet_ticket=ticket,
                        transaction_type="bet_payout",
                        status="completed",
                        is_successful=True,
                    )
                    .order_by("-timestamp")
                    .values_list("amount", flat=True)
                    .first()
                )
                if payout is not None:
                    new_potential = payout
                    new_max = payout

            if new_potential is None or new_max is None:
                continue

            if ticket.potential_winning == new_potential and ticket.max_winning == new_max:
                continue

            updated += 1
            if dry_run:
                continue

            with transaction.atomic():
                t = BetTicket.objects.select_for_update().get(pk=ticket.pk)
                t.potential_winning = new_potential
                t.max_winning = new_max
                t.save(update_fields=["potential_winning", "max_winning"])

        self.stdout.write(self.style.SUCCESS(f"Inspected: {inspected}"))
        self.stdout.write(self.style.SUCCESS(f"Updated: {updated}{' (dry-run)' if dry_run else ''}"))

