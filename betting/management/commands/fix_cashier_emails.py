from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model


class Command(BaseCommand):
    help = "Sync existing cashier emails to exactly match their parent agent email address."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        dry_run = bool(options.get("dry_run"))
        User = get_user_model()

        cashiers = User.objects.filter(user_type="cashier").select_related("agent")
        updated = 0
        skipped = 0

        for cashier in cashiers:
            agent = getattr(cashier, "agent", None)
            if not agent or not getattr(agent, "email", ""):
                skipped += 1
                continue

            new_email = (agent.email or "").strip()
            if not new_email or cashier.email == new_email:
                skipped += 1
                continue

            if dry_run:
                self.stdout.write(f"{cashier.pk}: {cashier.email} -> {new_email}")
            else:
                cashier.email = new_email
                cashier.save(update_fields=["email"])

            updated += 1

        self.stdout.write(self.style.SUCCESS(f"Done. updated={updated} skipped={skipped} dry_run={dry_run}"))
