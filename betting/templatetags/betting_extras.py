from django import template
from django.utils import timezone

register = template.Library()

@register.filter
def is_within_void_window(ticket, window_minutes):
    if not ticket.placed_at:
        return False
    try:
        window = int(window_minutes)
    except (ValueError, TypeError):
        window = 60
        
    diff = timezone.now() - ticket.placed_at
    minutes = diff.total_seconds() / 60
    return minutes <= window
