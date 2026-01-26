from django.core.mail import send_mail
from django.conf import settings
from django.utils import timezone
from betting.models import User
from .services import DashboardService
from .models import Alert

class AlertService:
    @staticmethod
    def check_and_send_alerts():
        """
        Checks for risk metrics and sends alerts if thresholds are breached.
        This should be called periodically (e.g., via Celery).
        """
        metrics = DashboardService.get_risk_metrics()
        
        # 1. Multi-Account Users
        if metrics['suspicious_ips']:
            AlertService.create_alert(
                title="Potential Multi-Account Detected",
                message=f"Detected {len(metrics['suspicious_ips'])} IPs with multiple users. Please investigate.",
                severity='warning'
            )

        # 2. Bonus Abuse
        if metrics['bonus_abusers']:
            AlertService.create_alert(
                title="Bonus Abuse Suspected",
                message=f"Detected {len(metrics['bonus_abusers'])} users with excessive bonus claims (>3/week).",
                severity='warning'
            )

        # 3. High Value Bets (Example threshold check)
        # Note: metrics['large_bets'] returns a QuerySet or list
        if metrics['large_bets']:
            count = len(metrics['large_bets'])
            AlertService.create_alert(
                title="High Value Bets Placed",
                message=f"{count} bets exceeding the high-value threshold have been placed recently.",
                severity='info'
            )

    @staticmethod
    def create_alert(title, message, severity='info'):
        """
        Creates an Alert record and sends an email notification.
        """
        # Avoid duplicate alerts for the same issue on the same day? 
        # For simplicity, we just create it. Real implementation needs de-duplication.
        
        # Check if similar alert exists today to avoid spam
        today = timezone.now().date()
        if Alert.objects.filter(title=title, created_at__date=today, is_resolved=False).exists():
            return

        alert = Alert.objects.create(
            title=title,
            message=message,
            severity=severity
        )
        
        # Send Email to Admins
        AlertService.send_email_notification(alert)
        
        return alert

    @staticmethod
    def send_email_notification(alert):
        """
        Sends email to all admins.
        """
        subject = f"[UIP ALERT] [{alert.severity.upper()}] {alert.title}"
        body = f"""
        Unified Intelligence Platform Alert System
        ------------------------------------------
        Severity: {alert.severity.upper()}
        Title: {alert.title}
        Time: {alert.created_at}
        
        Message:
        {alert.message}
        
        Please log in to the UIP Dashboard to investigate.
        """
        
        admin_emails = User.objects.filter(user_type='admin').values_list('email', flat=True)
        if not admin_emails:
            return

        try:
            send_mail(
                subject,
                body,
                settings.DEFAULT_FROM_EMAIL,
                list(admin_emails),
                fail_silently=True,
            )
        except Exception as e:
            print(f"Failed to send alert email: {e}")
