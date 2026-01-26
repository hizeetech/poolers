from django.db.models.signals import post_save, post_delete, pre_save
from django.contrib.auth.signals import user_logged_in, user_logged_out
from django.dispatch import receiver
from django.db import transaction
from django.utils import timezone
from .models import ActivityLog, User, BetTicket, Wallet, Transaction, UserWithdrawal
from .middleware import get_current_user, get_client_ip, get_current_request
from .utils import get_ip_details
import threading

def fetch_and_update_isp(log_id, ip_address):
    try:
        data = get_ip_details(ip_address)
        if data and data.get('connection') and data['connection'].get('isp'):
            # Re-fetch to avoid race conditions or stale data
            log = ActivityLog.objects.get(id=log_id)
            log.isp = data['connection']['isp']
            # Optional: Add country/city if needed, but user asked for ISP
            # log.location = f"{data.get('city')}, {data.get('country')}"
            log.save(update_fields=['isp'])
    except Exception as e:
        print(f"Failed to update ISP for log {log_id}: {e}")

@receiver(post_save, sender=ActivityLog)
def enrich_activity_log(sender, instance, created, **kwargs):
    if created and instance.ip_address and not instance.isp:
        # Run in a separate thread to avoid blocking the response
        thread = threading.Thread(
            target=fetch_and_update_isp, 
            args=(instance.id, instance.ip_address)
        )
        thread.daemon = True
        thread.start()

@receiver(user_logged_in)
def log_user_login(sender, request, user, **kwargs):
    ip = get_client_ip(request)
    ActivityLog.objects.create(
        user=user,
        action_type='LOGIN',
        action=f"User logged in",
        ip_address=ip,
        user_agent=request.META.get('HTTP_USER_AGENT', ''),
        path=request.path
    )

@receiver(user_logged_out)
def log_user_logout(sender, request, user, **kwargs):
    if user:
        ip = get_client_ip(request)
        ActivityLog.objects.create(
            user=user,
            action_type='LOGOUT',
            action=f"User logged out",
            ip_address=ip,
            user_agent=request.META.get('HTTP_USER_AGENT', ''),
            path=request.path
        )

# Helper to avoid logging ActivityLog creation itself to prevent recursion
def should_skip_logging(sender):
    return sender == ActivityLog

@receiver(post_save, sender=User)
def log_user_changes(sender, instance, created, **kwargs):
    user = get_current_user()
    
    # Ensure user is a valid User instance or None
    if user and not user.is_authenticated:
        user = None
    
    # Fallback: if no logged-in user (e.g., registration or system task), use the instance itself
    if not user:
        user = instance

    request = get_current_request()
    ip = get_client_ip(request) if request else None
    
    action_type = 'CREATE' if created else 'UPDATE'
    action_desc = f"User {'created' if created else 'updated'}: {instance.email}"
    
    # Avoid logging if system action (no user context) unless strictly needed
    # But for now, log everything if possible
    
    ActivityLog.objects.create(
        user=user, # Might be None if system task
        action_type=action_type,
        action=action_desc,
        affected_object=f"User: {instance.email}",
        ip_address=ip,
        user_agent=request.META.get('HTTP_USER_AGENT', '') if request else '',
        path=request.path if request else ''
    )

@receiver(post_save, sender=BetTicket)
def log_bet_ticket(sender, instance, created, **kwargs):
    user = get_current_user()
    if not user and instance.user:
        user = instance.user

    request = get_current_request()
    ip = get_client_ip(request) if request else None
    
    if created:
        action_type = 'BET_PLACED'
        action_desc = f"Bet placed: {instance.ticket_id} - Stake: {instance.stake_amount}"
    else:
        action_type = 'UPDATE'
        action_desc = f"Bet ticket updated: {instance.ticket_id} - Status: {instance.status}"
        
    ActivityLog.objects.create(
        user=user,
        action_type=action_type,
        action=action_desc,
        affected_object=f"BetTicket: {instance.ticket_id}",
        ip_address=ip,
        user_agent=request.META.get('HTTP_USER_AGENT', '') if request else '',
        path=request.path if request else ''
    )

@receiver(pre_save, sender=UserWithdrawal)
def handle_withdrawal_status_change(sender, instance, **kwargs):
    if instance.pk:
        try:
            old_instance = UserWithdrawal.objects.get(pk=instance.pk)
        except UserWithdrawal.DoesNotExist:
            return

        # Check if refund logic is manually handled (e.g., by a view with specific reason)
        if getattr(instance, '_skip_signal_refund', False):
            return

        if old_instance.status != instance.status:
            user = get_current_user()
            
            # If becoming rejected, refund.
            if instance.status == 'rejected' and old_instance.status != 'rejected':
                with transaction.atomic():
                    wallet = Wallet.objects.select_for_update().get(user=instance.user)
                    wallet.balance += instance.amount
                    wallet.save()
                    
                    Transaction.objects.create(
                        user=instance.user,
                        initiating_user=user if user and user.is_authenticated else None,
                        target_user=instance.user,
                        transaction_type='withdrawal_refund',
                        amount=instance.amount,
                        is_successful=True,
                        status='completed',
                        description=f"Refund for rejected withdrawal request {instance.id}",
                        timestamp=timezone.now()
                    )
            
            # If was rejected, and now not rejected (re-opening), deduct again.
            elif old_instance.status == 'rejected' and instance.status != 'rejected':
                with transaction.atomic():
                    wallet = Wallet.objects.select_for_update().get(user=instance.user)
                    if wallet.balance < instance.amount:
                        # Prevent status change if insufficient funds
                        # Raising error here will abort the save
                        raise ValueError("Insufficient funds to reopen withdrawal request.")
                    
                    wallet.balance -= instance.amount
                    wallet.save()
            
            # Update audit fields
            if instance.status in ['approved', 'rejected']:
                if not instance.approved_rejected_time:
                    instance.approved_rejected_time = timezone.now()
                if not instance.approved_rejected_by and user and user.is_authenticated:
                    instance.approved_rejected_by = user

@receiver(post_save, sender=UserWithdrawal)
def log_withdrawal(sender, instance, created, **kwargs):
    user = get_current_user()
    if not user and instance.user:
        user = instance.user

    request = get_current_request()
    ip = get_client_ip(request) if request else None
    
    action_type = 'CREATE' if created else 'UPDATE'
    action_desc = f"Withdrawal request {'created' if created else 'updated'}: {instance.id} - Amount: {instance.amount} - Status: {instance.status}"
    
    ActivityLog.objects.create(
        user=user,
        action_type=action_type,
        action=action_desc,
        affected_object=f"Withdrawal: {instance.id}",
        ip_address=ip,
        user_agent=request.META.get('HTTP_USER_AGENT', '') if request else '',
        path=request.path if request else ''
    )

@receiver(post_save, sender=User)
def create_user_wallet(sender, instance, created, **kwargs):
    if created:
        Wallet.objects.get_or_create(user=instance)
