import requests
import logging
from django.conf import settings

logger = logging.getLogger(__name__)

def get_ip_details(ip_address):
    """
    Fetches IP details including ISP from ipwho.org (or ipwhois.pro).
    """
    if not ip_address or ip_address == '127.0.0.1':
        return None

    # API Key provided by user
    api_key = "sk.efd861dd24fe08680f5c1251781a23e6b384e353e5a35332eda77319f0c10153"
    
    # Try the Pro endpoint first as requested
    url = f"https://ipwhois.pro/{ip_address}?key={api_key}"
    
    try:
        response = requests.get(url, timeout=5)
        data = response.json()
        
        if data.get('success'):
            return data
        else:
            logger.warning(f"IPWho Pro API failed for {ip_address}: {data.get('message')}. Falling back to free tier.")
            
            # Fallback to free tier (no key)
            url_free = f"https://ipwho.is/{ip_address}"
            response_free = requests.get(url_free, timeout=5)
            data_free = response_free.json()
            
            if data_free.get('success'):
                return data_free
            else:
                logger.error(f"IPWho Free API failed for {ip_address}: {data_free.get('message')}")
                return None
                
    except Exception as e:
        logger.error(f"Error fetching IP details for {ip_address}: {e}")
        return None
