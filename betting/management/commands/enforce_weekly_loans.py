from django.core.management.base import BaseCommand
from django.utils import timezone
from betting.models import Loan, User, LoginAttempt

class Command(BaseCommand):
    help = 'Deactivate/Lock accounts with outstanding loans. Intended to be run every Monday at 23:59.'

    def handle(self, *args, **kwargs):
        self.stdout.write("Starting weekly loan enforcement...")
        
        # Find all active loans with outstanding balance
        # We enforce strictly: Any active loan on Monday night triggers a lock.
        active_loans = Loan.objects.filter(status='active', outstanding_balance__gt=0)
        
        processed_users = 0
        
        for loan in active_loans:
            user = loan.borrower
            if not user.is_locked:
                user.is_locked = True
                user.lock_reason = f"Weekly Enforcement: Outstanding loan (ID: {loan.id}) of {loan.outstanding_balance}"
                user.locked_at = timezone.now()
                user.save()
                
                # Log the lock event
                LoginAttempt.objects.create(
                    user=user,
                    username_attempted=user.email,
                    status='locked',
                    ip_address='127.0.0.1', # System action
                    user_agent='System/ManagementCommand'
                )
                
                self.stdout.write(self.style.WARNING(f"Locked user {user.email} due to outstanding loan {loan.id}."))
                processed_users += 1
            else:
                # Update reason if already locked? No, keep original reason or append.
                pass
                
        self.stdout.write(self.style.SUCCESS(f"Successfully processed loans. Locked {processed_users} users."))
