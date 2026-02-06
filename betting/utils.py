import requests
import logging
import os
from django.conf import settings

logger = logging.getLogger(__name__)

def log_debug(message):
    try:
        with open(os.path.join(settings.BASE_DIR, 'isp_debug.log'), 'a') as f:
            f.write(f"{message}\n")
    except Exception:
        pass

def get_ip_details(ip_address):
    """
    Fetches IP details including ISP from ipwho.org (or ipwhois.pro).
    """
    log_debug(f"Starting ISP fetch for: {ip_address}")
    
    if not ip_address or ip_address == '127.0.0.1':
        log_debug("IP is localhost or empty, skipping.")
        return None

    # API Key provided by user
    api_key = "sk.efd861dd24fe08680f5c1251781a23e6b384e353e5a35332eda77319f0c10153"
    
    # Try the Pro endpoint first as requested
    url = f"https://ipwhois.pro/{ip_address}?key={api_key}"
    
    try:
        log_debug(f"Requesting Pro API: {url}")
        response = requests.get(url, timeout=5)
        data = response.json()
        
        if data.get('success'):
            log_debug(f"Pro API Success: {data.get('connection', {}).get('isp')}")
            return data
        else:
            log_debug(f"Pro API Failed: {data.get('message')}")
            logger.warning(f"IPWho Pro API failed for {ip_address}: {data.get('message')}. Falling back to free tier.")
            
            # Fallback to free tier (no key)
            url_free = f"https://ipwho.is/{ip_address}"
            log_debug(f"Requesting Free API: {url_free}")
            response_free = requests.get(url_free, timeout=5)
            data_free = response_free.json()
            
            if data_free.get('success'):
                log_debug(f"Free API Success: {data_free.get('connection', {}).get('isp')}")
                return data_free
            else:
                log_debug(f"Free API Failed: {data_free.get('message')}")
                logger.error(f"IPWho Free API failed for {ip_address}: {data_free.get('message')}")
                return None
                
    except Exception as e:
        log_debug(f"Exception during ISP fetch: {str(e)}")
        logger.error(f"Error fetching IP details for {ip_address}: {e}")
        return None

def get_client_ip(request):
    """
    Retrieves the client IP address from the request, handling proxies.
    """
    x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
    if x_forwarded_for:
        ip = x_forwarded_for.split(',')[0].strip()
    else:
        ip = request.META.get('REMOTE_ADDR')
    return ip
