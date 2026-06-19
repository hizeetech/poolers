from django.core.management.base import BaseCommand
from django.utils import timezone
from betting.models import Loan
from betting.services.loan_overdraft import enforce_due_loans

class Command(BaseCommand):
    help = 'Enforce overdue overdraft loans using the configured repayment deadline and lock workflow.'

    def handle(self, *args, **kwargs):
        now = timezone.now()
        due_loans = Loan.objects.filter(
            status__in=['active', 'overdue', 'defaulted'],
            outstanding_balance__gt=0,
            due_date__lt=now,
        ).select_related('borrower')

        self.stdout.write(f"Starting overdraft enforcement at {timezone.localtime(now):%Y-%m-%d %H:%M:%S %Z}...")
        for loan in due_loans:
            self.stdout.write(
                self.style.WARNING(
                    f"Overdue loan #{loan.id} borrower={loan.borrower.username or loan.borrower.email} "
                    f"balance={loan.outstanding_balance} due={timezone.localtime(loan.due_date):%Y-%m-%d %H:%M:%S %Z}"
                )
            )

        processed_loans = enforce_due_loans(reference_dt=now)
        self.stdout.write(self.style.SUCCESS(f"Successfully processed {processed_loans} overdue overdraft loan(s)."))
