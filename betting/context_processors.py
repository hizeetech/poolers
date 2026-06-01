# betting/context_processors.py
from datetime import timedelta
from django.utils import timezone
from django.db.models import Q
from .models import Wallet, SiteConfiguration, FooterBadge, FooterPage, ActivityLog, User

def wallet_balance(request):
    """
    Adds the current user's wallet balance to the template context.
    """
    balance = None
    if request.user.is_authenticated:
        try:
            # We use select_related('user') to optimize fetching the related User object
            # if 'user' is frequently accessed on the Wallet object.
            wallet = Wallet.objects.select_related('user').get(user=request.user)
            balance = wallet.balance
        except Wallet.DoesNotExist:
            balance = None # User has no wallet yet, or an error occurred
    return {'user_wallet_balance': balance}

def site_configuration(request):
    badges = FooterBadge.objects.filter(is_active=True).order_by('order', 'id')
    unique_badges = []
    seen = set()
    for b in badges:
        key = b.content_hash or b.image.name
        if key in seen:
            continue
        seen.add(key)
        unique_badges.append(b)
    now = timezone.localtime(timezone.now())
    return {
        'site_config': SiteConfiguration.load(),
        'footer_pages': FooterPage.objects.filter(is_active=True, show_in_footer=True).order_by('order', 'footer_label'),
        'footer_badges': unique_badges,
        'server_epoch_ms': int(now.timestamp() * 1000),
        'server_tz': timezone.get_current_timezone_name(),
    }

def impersonation_context(request):
    is_impersonating = bool(request.session.get('impersonation_active'))
    target_email = request.user.email if is_impersonating and request.user.is_authenticated else None
    return {
        'impersonation_active': is_impersonating,
        'impersonation_target_email': target_email,
    }

def agent_downline_activity_notifications(request):
    default_context = {
        'show_agent_notifications': False,
        'agent_notifications_unread_count': 0,
        'agent_notifications_recent': [],
        'agent_notifications_last_seen_at': None,
    }

    if not request.user.is_authenticated:
        return default_context

    if request.user.user_type not in ['agent', 'super_agent', 'master_agent']:
        return default_context

    user = request.user

    if user.user_type == 'agent':
        downline_qs = User.objects.filter(agent=user, user_type='cashier')
    elif user.user_type == 'super_agent':
        downline_qs = User.objects.filter(Q(agent__super_agent=user) | Q(super_agent=user), user_type__in=['agent', 'cashier'])
    else:
        downline_qs = User.objects.filter(
            Q(agent__super_agent__master_agent=user) | Q(super_agent__master_agent=user) | Q(master_agent=user),
            user_type__in=['super_agent', 'agent', 'cashier']
        )

    since = timezone.now() - timedelta(days=7)
    base_qs = ActivityLog.objects.filter(
        user__in=downline_qs,
        action_type__in=['LOGIN', 'LOGOUT'],
        timestamp__gte=since,
    ).select_related('user').order_by('-timestamp')

    last_seen = user.downline_activity_last_seen_at
    if last_seen:
        unread_count = base_qs.filter(timestamp__gt=last_seen).count()
    else:
        unread_count = base_qs.count()

    recent = list(base_qs[:10])

    return {
        'show_agent_notifications': True,
        'agent_notifications_unread_count': unread_count,
        'agent_notifications_recent': recent,
        'agent_notifications_last_seen_at': last_seen,
    }
