from django.db.models.signals import post_save
from django.dispatch import receiver
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from betting.models import BetTicket, Transaction
from django.db import transaction
from django.core.cache import cache
from .services import DashboardService
import threading

LARGE_BET_THRESHOLD = 50000
HIGH_EXPOSURE_THRESHOLD = 100000


def _run_in_background(target, *args, **kwargs):
    try:
        thread = threading.Thread(target=target, args=args, kwargs=kwargs)
        thread.daemon = True
        thread.start()
    except Exception:
        return


@receiver(post_save, sender=BetTicket)
def broadcast_bet_activity(sender, instance, created, **kwargs):
    channel_layer = get_channel_layer()
    
    try:
        if not channel_layer:
            return

        dashboard_messages = []
        if created:
            dashboard_messages.append(
                {
                    'type': 'bet_placed',
                    'ticket_id': instance.ticket_id,
                    'amount': str(instance.stake_amount),
                    'user': instance.user.email,
                    'timestamp': str(instance.placed_at),
                }
            )

            if instance.stake_amount >= LARGE_BET_THRESHOLD:
                dashboard_messages.append(
                    {
                        'type': 'alert',
                        'level': 'warning',
                        'title': 'Large Bet Detected',
                        'message': f"User {instance.user.email} placed a bet of {instance.stake_amount}"
                    }
                )

        if instance.potential_winning >= HIGH_EXPOSURE_THRESHOLD:
            dashboard_messages.append(
                {
                    'type': 'alert',
                    'level': 'critical',
                    'title': 'High Exposure Alert',
                    'message': f"Ticket {instance.ticket_id} has potential winning of {instance.potential_winning}"
                }
            )

        def _send_dashboard_messages():
            for message in dashboard_messages:
                async_to_sync(channel_layer.group_send)(
                    'uip_dashboard',
                    {
                        'type': 'dashboard_update',
                        'data': message
                    }
                )

        def send_frequency_update():
            try:
                DashboardService.invalidate_data_version()
                cache.delete('uip_agent_leaderboard')
                cache.delete('uip_live_metrics')
                cache.delete('uip_financial_metrics')
                cache.delete('uip_analytics_metrics')
                data = DashboardService.get_serial_number_frequency()
                async_to_sync(channel_layer.group_send)(
                    'uip_dashboard',
                    {
                        'type': 'dashboard_update',
                        'data': {
                            'type': 'serial_frequency_update',
                            'stats': data
                        }
                    }
                )
            except Exception:
                pass

        def _after_commit_work():
            try:
                if dashboard_messages:
                    _send_dashboard_messages()
                send_frequency_update()
            except Exception:
                pass

        transaction.on_commit(lambda: _run_in_background(_after_commit_work))
        
    except Exception:
        pass # Fail silently if Redis is down

@receiver(post_save, sender=Transaction)
def broadcast_transaction(sender, instance, created, **kwargs):
    if created:
        try:
            channel_layer = get_channel_layer()
            if not channel_layer:
                return
            # 4. Cashflow Movement
            message = {
                'type': 'transaction',
                'amount': str(instance.amount),
                'desc': instance.description,
                'user': instance.user.email,
                'timestamp': str(instance.timestamp)
            }
            transaction.on_commit(
                lambda: _run_in_background(
                    async_to_sync(channel_layer.group_send),
                    'uip_dashboard',
                    {
                        'type': 'dashboard_update',
                        'data': message
                    }
                )
            )
        except Exception:
            pass # Fail silently if Redis is down
