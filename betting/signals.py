from django.db.models.signals import post_save, post_delete, pre_save
from django.contrib.auth.signals import user_logged_in, user_logged_out
from django.dispatch import receiver
from django.db import transaction
from django.utils import timezone
from .models import ActivityLog, User, BetTicket, Wallet, Transaction, UserWithdrawal, Fixture, BonusRule, GlobalBettingSettings, AgentBettingLimitOverride, UserBettingLimitOverride
from .middleware import get_current_user, get_current_request
from .utils import get_ip_details, get_client_ip, log_debug, clear_bonus_rules_cache, clear_betting_limits_cache
from notifications.services import create_notification
from django.core.cache import cache
import threading
from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync

def fetch_and_update_isp(log_id, ip_address):
    try:
        log_debug(f"Thread started for log {log_id}, IP: {ip_address}")
        data = get_ip_details(ip_address)
        if data and data.get('connection') and data['connection'].get('isp'):
            # Re-fetch to avoid race conditions or stale data
            log = ActivityLog.objects.get(id=log_id)
            log.isp = data['connection']['isp']
            # Optional: Add country/city if needed, but user asked for ISP
            # log.location = f"{data.get('city')}, {data.get('country')}"
            log.save(update_fields=['isp'])
            log_debug(f"Updated ISP for log {log_id}: {log.isp}")
        else:
            log_debug(f"No ISP data found for log {log_id}")
    except Exception as e:
        log_debug(f"Failed to update ISP for log {log_id}: {e}")
        print(f"Failed to update ISP for log {log_id}: {e}")

@receiver(post_save, sender=ActivityLog)
def enrich_activity_log(sender, instance, created, **kwargs):
    if created and instance.ip_address and not instance.isp:
        log_debug(f"Enriching ActivityLog {instance.id} with IP {instance.ip_address}")
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


def _broadcast_retail_event_for_user(*, user, payload):
    if not user:
        return
    try:
        agent_id = getattr(user, 'agent_id', None)
        super_agent_id = getattr(user, 'super_agent_id', None)
        master_agent_id = getattr(user, 'master_agent_id', None)
        t = getattr(user, 'user_type', None)
        if t == 'agent':
            agent_id = user.id
        elif t == 'super_agent':
            super_agent_id = user.id
        elif t == 'master_agent':
            master_agent_id = user.id

        from .models import RetailManagerMasterAgentMapping, RetailManagerSuperAgentMapping, RetailManagerAgentMapping

        rm_ids = set()
        if agent_id:
            rm_ids.update(RetailManagerAgentMapping.objects.filter(agent_id=agent_id).values_list('retail_manager_id', flat=True))
        if super_agent_id:
            rm_ids.update(RetailManagerSuperAgentMapping.objects.filter(super_agent_id=super_agent_id).values_list('retail_manager_id', flat=True))
        if master_agent_id:
            rm_ids.update(RetailManagerMasterAgentMapping.objects.filter(master_agent_id=master_agent_id).values_list('retail_manager_id', flat=True))
        if not rm_ids:
            return

        channel_layer = get_channel_layer()
        for rid in rm_ids:
            async_to_sync(channel_layer.group_send)(
                f"notifications_user_{rid}",
                {"type": "retail.event", "payload": payload},
            )
    except Exception:
        return


def _broadcast_finance_event(payload):
    try:
        channel_layer = get_channel_layer()
        async_to_sync(channel_layer.group_send)(
            "finance_broadcast",
            {"type": "finance.event", "payload": payload},
        )
    except Exception:
        return

@receiver(post_save, sender=User)
def log_user_changes(sender, instance, created, **kwargs):
    user = get_current_user()
    
    
    if user and not user.is_authenticated:
        user = None
    
    
    if not user:
        user = instance

    request = get_current_request()
    ip = get_client_ip(request) if request else None
    
    action_type = 'CREATE' if created else 'UPDATE'
    action_desc = f"User {'created' if created else 'updated'}: {instance.email}"
    
    
    ActivityLog.objects.create(
        user=user,
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

    if created and instance.user:
        _broadcast_retail_event_for_user(
            user=instance.user,
            payload={
                "ts": instance.placed_at.isoformat() if instance.placed_at else "",
                "event_type": "bet",
                "user": (instance.user.email or instance.user.username or "-"),
                "label": f"Bet placed ({instance.ticket_id or ''})".strip(),
                "amount": str(instance.stake_amount),
                "status": instance.status,
                "kpi_deltas": {"bets_today": 1, "stake_today": float(instance.stake_amount)},
            },
        )
        _broadcast_finance_event(
            {
                "ts": instance.placed_at.isoformat() if instance.placed_at else "",
                "event_type": "bet",
                "user": (instance.user.email or instance.user.username or "-"),
                "label": f"Bet placed ({instance.ticket_id or ''})".strip(),
                "amount": str(instance.stake_amount),
                "status": instance.status,
            }
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
            request = get_current_request()
            
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
            if instance.status in ['approved', 'rejected', 'completed']:
                if not instance.approved_rejected_time:
                    instance.approved_rejected_time = timezone.now()
                if not instance.approved_rejected_by and user and user.is_authenticated:
                    instance.approved_rejected_by = user
                if not instance.processed_ip and request:
                    instance.processed_ip = get_client_ip(request)

                try:
                    user_wallet = Wallet.objects.select_for_update().get(user=instance.user)
                except Wallet.DoesNotExist:
                    user_wallet = None

                if user_wallet and (instance.balance_before is None or instance.balance_after is None):
                    if instance.status in ['approved', 'completed']:
                        instance.balance_after = user_wallet.balance
                        instance.balance_before = user_wallet.balance + instance.amount
                    elif instance.status == 'rejected':
                        instance.balance_before = user_wallet.balance
                        instance.balance_after = user_wallet.balance - instance.amount

                if user and user.is_authenticated and (instance.approver_balance_before is None or instance.approver_balance_after is None):
                    try:
                        approver_wallet = Wallet.objects.select_for_update().get(user=user)
                    except Wallet.DoesNotExist:
                        approver_wallet = None
                    if approver_wallet:
                        instance.approver_balance_before = instance.approver_balance_before if instance.approver_balance_before is not None else approver_wallet.balance
                        instance.approver_balance_after = instance.approver_balance_after if instance.approver_balance_after is not None else approver_wallet.balance

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

    if created and instance.user:
        _broadcast_retail_event_for_user(
            user=instance.user,
            payload={
                "ts": instance.request_time.isoformat() if getattr(instance, 'request_time', None) else "",
                "event_type": "withdrawal",
                "user": (instance.user.email or instance.user.username or "-"),
                "label": "Withdrawal request",
                "amount": str(instance.amount),
                "status": instance.status,
                "kpi_deltas": {"withdrawals_today": float(instance.amount), "pending_withdrawals": 1},
            },
        )
        _broadcast_finance_event(
            {
                "ts": instance.request_time.isoformat() if getattr(instance, 'request_time', None) else "",
                "event_type": "withdrawal",
                "user": (instance.user.email or instance.user.username or "-"),
                "label": "Withdrawal request",
                "amount": str(instance.amount),
                "status": instance.status,
                "kpi_deltas": {"withdrawals_today": float(instance.amount), "pending_withdrawals": 1},
            }
        )


@receiver(post_save, sender=Transaction)
def retail_tx_broadcast(sender, instance, created, **kwargs):
    if not created:
        return
    if not instance.is_successful or instance.status != 'completed':
        return
    if not instance.user:
        return
    t = instance.transaction_type
    kpi = {}
    if t == 'deposit':
        kpi = {"deposits_today": float(instance.amount)}
    elif t == 'withdrawal':
        kpi = {"withdrawals_today": float(instance.amount)}
    elif t == 'commission_payout':
        kpi = {"commission": float(instance.amount)}
    _broadcast_retail_event_for_user(
        user=instance.user,
        payload={
            "ts": instance.timestamp.isoformat() if getattr(instance, 'timestamp', None) else "",
            "event_type": "transaction",
            "user": (instance.user.email or instance.user.username or "-"),
            "label": t,
            "amount": str(instance.amount),
            "status": instance.status,
            "kpi_deltas": kpi,
        },
    )
    _broadcast_finance_event(
        {
            "ts": instance.timestamp.isoformat() if getattr(instance, 'timestamp', None) else "",
            "event_type": "transaction",
            "user": (instance.user.email or instance.user.username or "-"),
            "label": t,
            "amount": str(instance.amount),
            "status": instance.status,
            "kpi_deltas": kpi,
        }
    )

@receiver(post_save, sender=User)
def create_user_wallet(sender, instance, created, **kwargs):
    if created:
        try:
            import sys
            if 'test' in sys.argv:
                return
        except Exception:
            pass
        Wallet.objects.get_or_create(user=instance)

@receiver(post_save, sender=BonusRule)
def clear_bonus_cache_on_save(sender, instance, **kwargs):
    clear_bonus_rules_cache()

@receiver(post_delete, sender=BonusRule)
def clear_bonus_cache_on_delete(sender, instance, **kwargs):
    clear_bonus_rules_cache()

@receiver(post_save, sender=GlobalBettingSettings)
def clear_betting_limits_cache_on_global_save(sender, instance, **kwargs):
    clear_betting_limits_cache()

@receiver(post_save, sender=AgentBettingLimitOverride)
def clear_betting_limits_cache_on_override_save(sender, instance, **kwargs):
    clear_betting_limits_cache(agent_id=instance.agent_id)

@receiver(post_delete, sender=AgentBettingLimitOverride)
def clear_betting_limits_cache_on_override_delete(sender, instance, **kwargs):
    clear_betting_limits_cache(agent_id=instance.agent_id)

@receiver(post_save, sender=UserBettingLimitOverride)
def clear_betting_limits_cache_on_user_override_save(sender, instance, **kwargs):
    clear_betting_limits_cache(user_id=instance.user_id)

@receiver(post_delete, sender=UserBettingLimitOverride)
def clear_betting_limits_cache_on_user_override_delete(sender, instance, **kwargs):
    clear_betting_limits_cache(user_id=instance.user_id)


@receiver(pre_save, sender=Fixture)
def notify_fixture_status_and_odds_change(sender, instance, **kwargs):
    if not instance.pk:
        return
    try:
        old = Fixture.objects.get(pk=instance.pk)
    except Fixture.DoesNotExist:
        return

    status_changed = old.status != instance.status
    odds_fields = [
        "home_win_odd",
        "draw_odd",
        "away_win_odd",
        "home_dnb_odd",
        "away_dnb_odd",
        "over_1_5_odd",
        "under_1_5_odd",
        "over_2_5_odd",
        "under_2_5_odd",
        "over_3_5_odd",
        "under_3_5_odd",
        "btts_yes_odd",
        "btts_no_odd",
    ]
    odds_changed = any(getattr(old, f) != getattr(instance, f) for f in odds_fields)

    if not status_changed and not odds_changed:
        return

    affected_user_ids = (
        BetTicket.objects.filter(status="pending", selections__fixture_id=instance.pk)
        .values_list("user_id", flat=True)
        .distinct()
    )
    users_qs = User.objects.filter(id__in=list(affected_user_ids), is_active=True).only("id")

    if status_changed and instance.status in ["postponed", "abandoned", "cancelled", "no_result"]:
        notif_type = "FIXTURE_POSTPONED" if instance.status == "postponed" else "EVENT_ABANDONED"
        title = "Fixture Updated"
        message = f"{instance.home_team} vs {instance.away_team} status changed to {instance.get_status_display()}."
        for u in users_qs.iterator():
            try:
                create_notification(
                    recipient=u,
                    notification_type=notif_type,
                    title=title,
                    message=message,
                    data={"fixture_id": instance.pk, "status": instance.status},
                )
            except Exception:
                continue

    if odds_changed and instance.status == "scheduled":
        dedupe_key = f"notifications:odds_changed:{instance.pk}"
        if cache.get(dedupe_key):
            return
        cache.set(dedupe_key, 1, timeout=600)
        title = "Odds Changed"
        message = f"Odds updated for {instance.home_team} vs {instance.away_team}."
        for u in users_qs.iterator():
            try:
                create_notification(
                    recipient=u,
                    notification_type="ODDS_CHANGED",
                    title=title,
                    message=message,
                    data={"fixture_id": instance.pk},
                )
            except Exception:
                continue
