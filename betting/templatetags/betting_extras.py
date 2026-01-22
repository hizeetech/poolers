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

@register.filter
def status_color_class(status):
    status = str(status).lower()
    if status == 'won':
        return 'text-success'
    elif status == 'lost':
        return 'text-danger'
    elif status == 'pending':
        return 'text-warning'
    elif status == 'cashed_out':
        return 'text-primary'
    elif status == 'cancelled':
        return 'text-secondary'
    elif status == 'deleted':
        return 'text-dark'
    return 'text-body'
