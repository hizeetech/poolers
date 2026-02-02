from django.db.models.signals import post_save
from django.dispatch import receiver
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from betting.models import BetTicket, Transaction
from django.db import transaction
from django.core.cache import cache
from .services import DashboardService
import json

LARGE_BET_THRESHOLD = 50000
HIGH_EXPOSURE_THRESHOLD = 100000

@receiver(post_save, sender=BetTicket)
def broadcast_bet_activity(sender, instance, created, **kwargs):
    channel_layer = get_channel_layer()
    
    try:
        if created:
            message = {
                'type': 'bet_placed',
                'ticket_id': instance.ticket_id,
                'amount': str(instance.stake_amount),
                'user': instance.user.email,
                'timestamp': str(instance.placed_at),
            }
            async_to_sync(channel_layer.group_send)(
                'uip_dashboard',
                {
                    'type': 'dashboard_update',
                    'data': message
                }
            )
            
            # 2. Large Bet Alert
            if instance.stake_amount >= LARGE_BET_THRESHOLD:
                alert_msg = {
                    'type': 'alert',
                    'level': 'warning',
                    'title': 'Large Bet Detected',
                    'message': f"User {instance.user.email} placed a bet of {instance.stake_amount}"
                }
                async_to_sync(channel_layer.group_send)(
                    'uip_dashboard',
                    {
                        'type': 'dashboard_update',
                        'data': alert_msg
                    }
                )

        # 3. Frequency Update logic moved to on_commit handler to ensure data consistency


        # 4. High Exposure Alert
        if instance.potential_winning >= HIGH_EXPOSURE_THRESHOLD:
            alert_msg = {
                'type': 'alert',
                'level': 'critical',
                'title': 'High Exposure Alert',
                'message': f"Ticket {instance.ticket_id} has potential winning of {instance.potential_winning}"
            }
            async_to_sync(channel_layer.group_send)(
                'uip_dashboard',
                {
                    'type': 'dashboard_update',
                    'data': alert_msg
                }
            )

        # 4. Serial Number Frequency Update (Real-time)
        # This runs for both created (new bets) and updated (status change like void/cancel)
        def send_frequency_update():
            try:
                # Invalidate cache globally by incrementing version
                try:
                    cache.incr('uip_serial_freq_version')
                except ValueError:
                    cache.set('uip_serial_freq_version', 1)
                
                # Fetch fresh data (will use new version)
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
                pass # Fail silently if Redis is down
        
        # Use on_commit to ensure Selections are saved (for created=True)
        transaction.on_commit(send_frequency_update)
        
    except Exception:
        pass # Fail silently if Redis is down

@receiver(post_save, sender=Transaction)
def broadcast_transaction(sender, instance, created, **kwargs):
    if created:
        try:
            channel_layer = get_channel_layer()
            # 4. Cashflow Movement
            message = {
                'type': 'transaction',
                'amount': str(instance.amount),
                'desc': instance.description,
                'user': instance.user.email,
                'timestamp': str(instance.timestamp)
            }
            async_to_sync(channel_layer.group_send)(
                'uip_dashboard',
                {
                    'type': 'dashboard_update',
                    'data': message
                }
            )
        except Exception:
            pass # Fail silently if Redis is down
