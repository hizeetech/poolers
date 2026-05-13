import requests
import logging
import os
from django.conf import settings
from django.core.cache import cache
from django.apps import apps
from decimal import Decimal
from django.db.models import Sum, Q, Value, DecimalField
from django.db.models.functions import Coalesce
from django.utils import timezone

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

BONUS_RULES_CACHE_KEY = "bonus_rules:v2:active"

def get_active_bonus_rules_cached():
    cached = cache.get(BONUS_RULES_CACHE_KEY)
    if cached is not None:
        return cached

    BonusRule = apps.get_model('betting', 'BonusRule')
    rules = (
        BonusRule.objects
        .filter(is_active=True)
        .order_by('-min_selections', '-bonus_percentage', 'min_odd_per_selection')
        .values(
            'id',
            'min_selections',
            'max_selections',
            'min_odd_per_selection',
            'bonus_percentage',
            'max_bonus_cap',
            'bonus_base',
            'allow_system_bets',
            'allow_accumulator_bets',
            'allow_single_bets',
        )
    )
    data = []
    for r in rules:
        data.append({
            'id': r['id'],
            'min': int(r['min_selections'] or 0),
            'max': int(r['max_selections']) if r['max_selections'] is not None else None,
            'min_odd': Decimal(str(r['min_odd_per_selection'])),
            'pct': Decimal(str(r['bonus_percentage'])),
            'cap': Decimal(str(r['max_bonus_cap'])) if r['max_bonus_cap'] is not None else None,
            'base': r['bonus_base'] or 'gross',
            'allow_system': bool(r['allow_system_bets']),
            'allow_acca': bool(r['allow_accumulator_bets']),
            'allow_single': bool(r['allow_single_bets']),
        })

    cache.set(BONUS_RULES_CACHE_KEY, data, timeout=300)
    return data

def clear_bonus_rules_cache():
    cache.delete(BONUS_RULES_CACHE_KEY)

def select_bonus_rule(bet_type, selection_count, odds):
    selection_count = int(selection_count or 0)
    if selection_count <= 0:
        return None

    if bet_type == 'system':
        flag = 'allow_system'
    elif bet_type == 'single':
        flag = 'allow_single'
    else:
        flag = 'allow_acca'

    odds_list = [Decimal(str(o)) for o in (odds or [])]
    for rule in get_active_bonus_rules_cached():
        if not rule.get(flag):
            continue
        if selection_count < rule['min']:
            continue
        if rule['max'] is not None and selection_count > rule['max']:
            continue
        if odds_list:
            min_odd = min(odds_list)
            if min_odd < rule['min_odd']:
                continue
        return rule

    return None

def compute_bonus_amount(base_amount, pct, cap=None):
    base_amount = Decimal(str(base_amount or 0))
    if base_amount < 0:
        base_amount = Decimal('0.00')
    pct = Decimal(str(pct or 0))
    amount = (base_amount * pct).quantize(Decimal('0.01'))
    if cap is not None:
        cap = Decimal(str(cap))
        amount = min(amount, cap)
    if amount < 0:
        amount = Decimal('0.00')
    return amount

def symmetric_sum_k_decimal(odds, k):
    kk = int(k or 0)
    if kk <= 0:
        return Decimal('0.00')
    dp = [Decimal('0.00')] * (kk + 1)
    dp[0] = Decimal('1.00')
    count = 0
    for raw in (odds or []):
        try:
            o = Decimal(str(raw))
        except Exception:
            o = Decimal('0.00')
        count += 1
        upper = min(kk, count)
        for j in range(upper, 0, -1):
            dp[j] = dp[j] + (dp[j - 1] * o)
    return dp[kk].quantize(Decimal('0.01'))

def system_line_odds_bounds(odds, k):
    kk = int(k or 0)
    items = []
    for raw in (odds or []):
        try:
            items.append(Decimal(str(raw)))
        except Exception:
            continue
    if kk <= 0 or len(items) < kk:
        return Decimal('0.00'), Decimal('0.00')

    items_sorted = sorted(items)
    min_line_odd = Decimal('1.00')
    for o in items_sorted[:kk]:
        min_line_odd *= o

    max_line_odd = Decimal('1.00')
    for o in reversed(items_sorted[-kk:]):
        max_line_odd *= o

    return min_line_odd.quantize(Decimal('0.01')), max_line_odd.quantize(Decimal('0.01'))

def system_bet_payout_projections(odds, stake_per_line, k):
    stake = Decimal(str(stake_per_line or 0)).quantize(Decimal('0.01'))
    kk = int(k or 0)
    if kk <= 0:
        return {
            'min_potential_winning': Decimal('0.00'),
            'max_potential_winning': Decimal('0.00'),
            'min_line_odd': Decimal('0.00'),
            'max_line_odd': Decimal('0.00'),
        }

    min_line_odd, max_line_odd = system_line_odds_bounds(odds, kk)
    sum_products = symmetric_sum_k_decimal(odds, kk)
    max_potential_winning = (stake * sum_products).quantize(Decimal('0.01'))
    min_potential_winning = (stake * min_line_odd).quantize(Decimal('0.01'))
    return {
        'min_potential_winning': min_potential_winning,
        'max_potential_winning': max_potential_winning,
        'min_line_odd': min_line_odd,
        'max_line_odd': max_line_odd,
    }

GLOBAL_BETTING_LIMITS_CACHE_KEY = "betting_limits:v1:global"
AGENT_BETTING_LIMITS_CACHE_PREFIX = "betting_limits:v1:agent:"

def clear_betting_limits_cache(agent_id=None):
    cache.delete(GLOBAL_BETTING_LIMITS_CACHE_KEY)
    if agent_id:
        cache.delete(f"{AGENT_BETTING_LIMITS_CACHE_PREFIX}{agent_id}")

def get_global_betting_settings_cached():
    cached = cache.get(GLOBAL_BETTING_LIMITS_CACHE_KEY)
    if cached is not None:
        return cached

    GlobalBettingSettings = apps.get_model('betting', 'GlobalBettingSettings')
    obj = GlobalBettingSettings.load()
    data = {
        'is_active': bool(obj.is_active),
        'betting_enabled': bool(obj.betting_enabled),
        'min_stake': Decimal(str(obj.min_stake)),
        'max_stake': Decimal(str(obj.max_stake)),
        'max_winning': Decimal(str(obj.max_winning)),
        'max_stake_by_ticket_type': dict(obj.max_stake_by_ticket_type or {}),
        'max_winning_by_ticket_type': dict(obj.max_winning_by_ticket_type or {}),
        'max_odds_per_ticket': Decimal(str(obj.max_odds_per_ticket)) if obj.max_odds_per_ticket is not None else None,
        'max_selections_per_ticket': int(obj.max_selections_per_ticket) if obj.max_selections_per_ticket is not None else None,
        'max_payout_per_day': Decimal(str(obj.max_payout_per_day)) if obj.max_payout_per_day is not None else None,
        'max_payout_per_user_per_day': Decimal(str(obj.max_payout_per_user_per_day)) if obj.max_payout_per_user_per_day is not None else None,
    }
    cache.set(GLOBAL_BETTING_LIMITS_CACHE_KEY, data, timeout=300)
    return data

def _get_agent_for_user(user):
    if not user:
        return None
    if getattr(user, 'user_type', None) in ['agent', 'super_agent', 'master_agent']:
        return user
    agent = getattr(user, 'agent', None)
    if agent:
        return agent
    super_agent = getattr(user, 'super_agent', None)
    if super_agent:
        return super_agent
    master_agent = getattr(user, 'master_agent', None)
    if master_agent:
        return master_agent
    return None

def get_agent_betting_override_cached(agent_id):
    if not agent_id:
        return None

    cache_key = f"{AGENT_BETTING_LIMITS_CACHE_PREFIX}{agent_id}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    AgentBettingLimitOverride = apps.get_model('betting', 'AgentBettingLimitOverride')
    override = (
        AgentBettingLimitOverride.objects
        .filter(agent_id=agent_id, is_active=True, custom_limits_enabled=True)
        .select_related('agent')
        .first()
    )
    if not override:
        cache.set(cache_key, None, timeout=300)
        return None

    data = {
        'agent_id': agent_id,
        'min_stake': Decimal(str(override.min_stake)) if override.min_stake is not None else None,
        'max_stake': Decimal(str(override.max_stake)) if override.max_stake is not None else None,
        'max_winning': Decimal(str(override.max_winning)) if override.max_winning is not None else None,
        'max_stake_by_ticket_type': dict(override.max_stake_by_ticket_type or {}),
        'max_winning_by_ticket_type': dict(override.max_winning_by_ticket_type or {}),
        'max_odds_per_ticket': Decimal(str(override.max_odds_per_ticket)) if override.max_odds_per_ticket is not None else None,
        'max_selections_per_ticket': int(override.max_selections_per_ticket) if override.max_selections_per_ticket is not None else None,
        'max_payout_per_agent_per_day': Decimal(str(override.max_payout_per_agent_per_day)) if override.max_payout_per_agent_per_day is not None else None,
        'max_payout_per_user_per_day': Decimal(str(override.max_payout_per_user_per_day)) if override.max_payout_per_user_per_day is not None else None,
    }
    cache.set(cache_key, data, timeout=300)
    return data

def _normalize_ticket_type(ticket_type):
    if not ticket_type:
        return None
    t = str(ticket_type).strip().lower()
    if t in ['single', 'multiple', 'system', 'pool']:
        return t
    return t

def _as_decimal_or_none(value):
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None

def _get_ticket_type_decimal(map_dict, ticket_type):
    if not map_dict or not ticket_type:
        return None
    raw = map_dict.get(ticket_type)
    if raw is None:
        return None
    return _as_decimal_or_none(raw)

def get_effective_betting_limits_for_user(user, ticket_type=None):
    global_limits = get_global_betting_settings_cached()
    agent = _get_agent_for_user(user)
    override = get_agent_betting_override_cached(getattr(agent, 'id', None)) if agent else None

    effective = dict(global_limits)
    effective['agent_id'] = getattr(agent, 'id', None)
    effective['has_agent_override'] = bool(override)

    if override:
        for k in ['min_stake', 'max_stake', 'max_winning', 'max_odds_per_ticket', 'max_selections_per_ticket', 'max_payout_per_agent_per_day', 'max_payout_per_user_per_day']:
            if override.get(k) is not None:
                effective[k] = override[k]
        if override.get('max_stake_by_ticket_type'):
            merged = dict(effective.get('max_stake_by_ticket_type') or {})
            merged.update(override.get('max_stake_by_ticket_type') or {})
            effective['max_stake_by_ticket_type'] = merged
        if override.get('max_winning_by_ticket_type'):
            merged = dict(effective.get('max_winning_by_ticket_type') or {})
            merged.update(override.get('max_winning_by_ticket_type') or {})
            effective['max_winning_by_ticket_type'] = merged

    ticket_type = _normalize_ticket_type(ticket_type)
    effective['ticket_type'] = ticket_type
    max_stake_effective = effective.get('max_stake')
    max_winning_effective = effective.get('max_winning')
    stake_cap = _get_ticket_type_decimal(effective.get('max_stake_by_ticket_type') or {}, ticket_type)
    win_cap = _get_ticket_type_decimal(effective.get('max_winning_by_ticket_type') or {}, ticket_type)
    if stake_cap is not None:
        if max_stake_effective is None:
            max_stake_effective = stake_cap
        else:
            max_stake_effective = min(max_stake_effective, stake_cap)
    if win_cap is not None:
        if max_winning_effective is None:
            max_winning_effective = win_cap
        else:
            max_winning_effective = min(max_winning_effective, win_cap)
    effective['max_stake_effective'] = max_stake_effective
    effective['max_winning_effective'] = max_winning_effective

    return effective

class BettingLimitViolation(Exception):
    def __init__(self, message, code='LIMIT_VIOLATION', data=None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.data = data or {}

def acquire_ticket_placement_lock(user_id, timeout_seconds=6):
    if not user_id:
        return None
    key = f"betting_limits:lock:user:{user_id}"
    try:
        if cache.add(key, "1", timeout=timeout_seconds):
            return key
    except Exception:
        return None
    return None

def release_ticket_placement_lock(lock_key):
    if not lock_key:
        return
    try:
        cache.delete(lock_key)
    except Exception:
        pass

def _agent_network_ticket_filter(agent):
    if not agent:
        return Q()
    t = getattr(agent, 'user_type', None)
    if t == 'agent':
        return Q(user=agent) | Q(user__agent=agent)
    if t == 'super_agent':
        return Q(user=agent) | Q(user__super_agent=agent) | Q(user__agent__super_agent=agent)
    if t == 'master_agent':
        return (
            Q(user=agent)
            | Q(user__master_agent=agent)
            | Q(user__super_agent__master_agent=agent)
            | Q(user__agent__master_agent=agent)
            | Q(user__agent__super_agent__master_agent=agent)
        )
    return Q(user=agent)

def serialize_limits(limits):
    out = {}
    for k, v in (limits or {}).items():
        if isinstance(v, Decimal):
            out[k] = str(v)
        else:
            out[k] = v
    return out

def validate_ticket_against_limits(
    *,
    user,
    ticket_type,
    selection_count,
    total_stake,
    max_winning,
    ticket_odds,
    include_exposure=True,
):
    ticket_type = _normalize_ticket_type(ticket_type)
    limits = get_effective_betting_limits_for_user(user, ticket_type=ticket_type)

    if not limits.get('is_active', True) or not limits.get('betting_enabled', True):
        raise BettingLimitViolation("Betting is temporarily disabled.", code="BETTING_DISABLED", data={'limits': serialize_limits(limits)})

    if total_stake is None or _as_decimal_or_none(total_stake) is None or Decimal(str(total_stake)) <= 0:
        raise BettingLimitViolation("Invalid stake amount.", code="INVALID_STAKE", data={'limits': serialize_limits(limits)})

    total_stake = Decimal(str(total_stake)).quantize(Decimal('0.01'))
    max_winning = Decimal(str(max_winning or 0)).quantize(Decimal('0.01'))
    ticket_odds = Decimal(str(ticket_odds or 0)).quantize(Decimal('0.01'))
    selection_count = int(selection_count or 0)

    min_stake = limits.get('min_stake')
    if min_stake is not None and total_stake < min_stake:
        raise BettingLimitViolation(f"Minimum stake is ₦{min_stake:.2f}.", code="MIN_STAKE", data={'min_stake': str(min_stake), 'limits': serialize_limits(limits)})

    max_stake = limits.get('max_stake_effective') if ticket_type else limits.get('max_stake')
    if max_stake is not None and total_stake > max_stake:
        raise BettingLimitViolation(f"Maximum stake is ₦{max_stake:.2f}.", code="MAX_STAKE", data={'max_stake': str(max_stake), 'limits': serialize_limits(limits)})

    max_sel = limits.get('max_selections_per_ticket')
    if max_sel is not None and selection_count > int(max_sel):
        raise BettingLimitViolation(f"Maximum selections per ticket is {int(max_sel)}.", code="MAX_SELECTIONS", data={'max_selections_per_ticket': int(max_sel), 'limits': serialize_limits(limits)})

    max_odds = limits.get('max_odds_per_ticket')
    if max_odds is not None and ticket_odds > max_odds:
        raise BettingLimitViolation(f"Maximum odds per ticket is {max_odds:.2f}.", code="MAX_ODDS", data={'max_odds_per_ticket': str(max_odds), 'limits': serialize_limits(limits)})

    max_win_limit = limits.get('max_winning_effective') if ticket_type else limits.get('max_winning')
    if max_win_limit is not None and max_winning > max_win_limit:
        raise BettingLimitViolation(f"Maximum winning per ticket is ₦{max_win_limit:.2f}.", code="MAX_WINNING", data={'max_winning': str(max_win_limit), 'limits': serialize_limits(limits)})

    if include_exposure:
        BetTicket = apps.get_model('betting', 'BetTicket')
        today = timezone.localdate()
        status_filter = ['pending', 'won']

        max_user_day = limits.get('max_payout_per_user_per_day')
        if max_user_day is not None:
            user_day_sum = (
                BetTicket.objects
                .filter(user=user, placed_at__date=today, status__in=status_filter)
                .aggregate(total=Coalesce(Sum('max_winning'), Value(0), output_field=DecimalField()))['total']
            )
            if (user_day_sum + max_winning) > max_user_day:
                raise BettingLimitViolation(
                    f"Daily payout limit reached for this user (₦{max_user_day:.2f}).",
                    code="USER_DAILY_PAYOUT",
                    data={'user_day_total': str(user_day_sum), 'max_payout_per_user_per_day': str(max_user_day), 'limits': serialize_limits(limits)}
                )

        agent = _get_agent_for_user(user)
        max_agent_day = limits.get('max_payout_per_agent_per_day')
        if max_agent_day is not None and agent:
            agent_day_sum = (
                BetTicket.objects
                .filter(_agent_network_ticket_filter(agent), placed_at__date=today, status__in=status_filter)
                .aggregate(total=Coalesce(Sum('max_winning'), Value(0), output_field=DecimalField()))['total']
            )
            if (agent_day_sum + max_winning) > max_agent_day:
                raise BettingLimitViolation(
                    f"Daily payout limit reached for this agent network (₦{max_agent_day:.2f}).",
                    code="AGENT_DAILY_PAYOUT",
                    data={'agent_day_total': str(agent_day_sum), 'max_payout_per_agent_per_day': str(max_agent_day), 'limits': serialize_limits(limits)}
                )

        max_platform_day = limits.get('max_payout_per_day')
        if max_platform_day is not None:
            platform_day_sum = (
                BetTicket.objects
                .filter(placed_at__date=today, status__in=status_filter)
                .aggregate(total=Coalesce(Sum('max_winning'), Value(0), output_field=DecimalField()))['total']
            )
            if (platform_day_sum + max_winning) > max_platform_day:
                raise BettingLimitViolation(
                    f"Daily payout limit reached for the platform (₦{max_platform_day:.2f}).",
                    code="PLATFORM_DAILY_PAYOUT",
                    data={'platform_day_total': str(platform_day_sum), 'max_payout_per_day': str(max_platform_day), 'limits': serialize_limits(limits)}
                )

    return limits
