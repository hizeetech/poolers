from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model
from betting.services.usernames import generate_cashier_email


class Command(BaseCommand):
    help = "Fix cashier emails to use C1/C2 prefix + agent email domain (e.g., C1agent@gmail.com)."

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

            suffix = None
            if (cashier.username or "").endswith("C1"):
                suffix = "C1"
            elif (cashier.username or "").endswith("C2"):
                suffix = "C2"
            else:
                skipped += 1
                continue

            new_email = generate_cashier_email(agent.email, suffix)
            if not new_email or cashier.email == new_email:
                skipped += 1
                continue

            if User.objects.filter(email__iexact=new_email).exclude(pk=cashier.pk).exists():
                local, sep, domain = new_email.partition("@")
                if sep:
                    new_email = f"{local}+{cashier.pk}@{domain}"
                else:
                    skipped += 1
                    continue

            if dry_run:
                self.stdout.write(f"{cashier.pk}: {cashier.email} -> {new_email}")
            else:
                cashier.email = new_email
                cashier.save(update_fields=["email"])

            updated += 1

        self.stdout.write(self.style.SUCCESS(f"Done. updated={updated} skipped={skipped} dry_run={dry_run}"))

