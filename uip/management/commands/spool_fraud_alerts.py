from django.core.management.base import BaseCommand
from django.utils import timezone
from uip.models import Alert, FraudAlert
from uip.services import FraudDetectionService
from uip.alerts import AlertService

class Command(BaseCommand):
    help = 'Spools existing risk-related Alerts into the new FraudAlert system'

    def handle(self, *args, **options):
        self.stdout.write("Starting to spool existing alerts...")
        
        # 1. Trigger a fresh detection cycle to catch anything currently active
        self.stdout.write("Running fresh detection cycle...")
        AlertService.check_and_send_alerts()
        
        # 2. Specifically look for recent Multi-Account alerts that might have been missed
        from uip.services import DashboardService
        metrics = DashboardService.get_risk_metrics()
        
        count = 0
        if metrics['suspicious_ips']:
            for item in metrics['suspicious_ips']:
                ip = item['ip_address']
                if not FraudAlert.objects.filter(alert_type='multi_account', related_ips__contains=[ip]).exists():
                    from betting.models import User
                    users = User.objects.filter(login_attempts__ip_address=ip, login_attempts__status='success').distinct()
                    FraudDetectionService.create_fraud_alert(
                        alert_type='multi_account',
                        description=f"Backfilled: Multiple accounts ({item['user_count']}) detected using IP: {ip}",
                        severity='high',
                        related_users=users,
                        related_ips=[ip]
                    )
                    count += 1
                    self.stdout.write(self.style.SUCCESS(f"Spooled multi-account alert for IP: {ip}"))

        if metrics['bonus_abusers']:
            for item in metrics['bonus_abusers']:
                from betting.models import User
                user = User.objects.filter(id=item.get('user')).first()
                if not user:
                    continue
                if not FraudAlert.objects.filter(alert_type='bonus_abuse', affected_users=user).exists():
                    FraudDetectionService.create_fraud_alert(
                        alert_type='bonus_abuse',
                        description=f"Backfilled: User {user.username or user.email} has claimed {item['bonus_count']} bonuses recently.",
                        severity='medium',
                        related_users=[user]
                    )
                    count += 1
                    self.stdout.write(self.style.SUCCESS(f"Spooled bonus abuse alert for user: {user.username or user.email}"))

        self.stdout.write(self.style.SUCCESS(f"Successfully spooled {count} new fraud alerts."))
