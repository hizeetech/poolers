# betting/context_processors.py
from .models import Wallet, SiteConfiguration, FooterBadge, FooterPage

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
    return {
        'site_config': SiteConfiguration.load(),
        'footer_pages': FooterPage.objects.filter(is_active=True, show_in_footer=True).order_by('order', 'footer_label'),
        'footer_badges': unique_badges,
    }

def impersonation_context(request):
    is_impersonating = bool(request.session.get('impersonation_active'))
    target_email = request.user.email if is_impersonating and request.user.is_authenticated else None
    return {
        'impersonation_active': is_impersonating,
        'impersonation_target_email': target_email,
    }
