# betting/context_processors.py
from .models import Wallet, SiteConfiguration

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
    return {'site_config': SiteConfiguration.load()}