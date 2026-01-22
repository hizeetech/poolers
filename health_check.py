import os
import django
import sys
from django.conf import settings
from django.test import Client
from django.urls import get_resolver
from django.db.models import Q
from datetime import date, timedelta

# Setup Django environment
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'poolbetting.settings')
django.setup()

# Patch ALLOWED_HOSTS for testing
settings.ALLOWED_HOSTS += ['testserver', '127.0.0.1']

from django.contrib.auth import get_user_model
from betting.models import BetTicket, BettingPeriod, Fixture, UserWithdrawal, AgentPayout, Wallet
from decimal import Decimal
from commission.models import CommissionPeriod

User = get_user_model()

def get_test_users():
    # Admin
    admin_user = User.objects.filter(user_type='admin').first()
    if not admin_user:
        print("Creating temp admin user...")
        admin_user = User.objects.create_superuser('temp_admin@test.com', 'password')
    if not Wallet.objects.filter(user=admin_user).exists():
        Wallet.objects.create(user=admin_user, balance=Decimal('0.00'))

    # Agent
    agent_user = User.objects.filter(user_type='agent').first()
    if not agent_user:
        print("Creating temp agent user...")
        agent_user = User.objects.create_user('temp_agent@test.com', 'password', user_type='agent')
    if not Wallet.objects.filter(user=agent_user).exists():
        Wallet.objects.create(user=agent_user, balance=Decimal('0.00'))

    # Regular User (Player)
    player_user = User.objects.filter(user_type='player').first()
    if not player_user:
        print("Creating temp player user...")
        player_user = User.objects.create_user('temp_player@test.com', 'password', user_type='player')
    if not Wallet.objects.filter(user=player_user).exists():
        Wallet.objects.create(user=player_user, balance=Decimal('0.00'))
        
    return admin_user, agent_user, player_user

def get_dummy_args():
    args = {}
    
    # Betting Period (needed for Fixture, AgentPayout)
    period = BettingPeriod.objects.first()
    if not period:
        period = BettingPeriod.objects.create(
            name="Test Period",
            start_date=date.today(),
            end_date=date.today() + timedelta(days=7),
            is_active=True
        )
    args['period_id'] = period.id

    # Fixture
    fixture = Fixture.objects.first()
    if not fixture:
        fixture = Fixture.objects.create(
            betting_period=period,
            serial_number="123",
            home_team="Team A",
            away_team="Team B",
            match_date=date.today(),
            match_time="12:00:00",
            status='scheduled'
        )
    args['fixture_id'] = fixture.id

    # User
    user = User.objects.first()
    if not user:
        # Should be covered by get_test_users but just in case
        user = User.objects.create_user('fallback_user@test.com', 'password', user_type='player')
        Wallet.objects.create(user=user, balance=Decimal('0.00'))
    args['user_id'] = user.id
    
    cashier = User.objects.filter(user_type='cashier').first()
    args['cashier_id'] = cashier.id if cashier else user.id

    # Ticket
    ticket = BetTicket.objects.first()
    if not ticket:
        ticket = BetTicket.objects.create(
            user=user,
            ticket_id="TEST1234",
            stake_amount=Decimal('100.00'),
            total_odd=Decimal('2.00'),
            potential_winning=Decimal('200.00'),
            status='pending'
        )
    args['ticket_id_uuid'] = ticket.id
    args['ticket_id_str'] = ticket.ticket_id

    # Commission Period
    comm_period = CommissionPeriod.objects.first()
    if not comm_period:
        comm_period = CommissionPeriod.objects.create(
            start_date=date.today(),
            end_date=date.today() + timedelta(days=30)
        )
    args['comm_period_id'] = comm_period.id

    # Withdrawal
    withdrawal = UserWithdrawal.objects.first()
    if not withdrawal:
        withdrawal = UserWithdrawal.objects.create(
            user=user,
            amount=Decimal('100.00'),
            status='pending',
            bank_name="Test Bank",
            account_number="1234567890",
            account_name="Test User"
        )
    args['withdrawal_id'] = withdrawal.id

    # Payout
    payout = AgentPayout.objects.first()
    if not payout:
        agent = User.objects.filter(user_type='agent').first()
        if agent and period:
            payout = AgentPayout.objects.create(
                agent=agent,
                betting_period=period,
                total_turnover=Decimal('1000.00'),
                total_winnings=Decimal('500.00'),
                ggr=Decimal('500.00'),
                commission_rate=Decimal('0.10'),
                commission_amount=Decimal('50.00'),
                status='pending'
            )
        elif not agent: # Create agent if missing (though get_test_users should handle it)
             agent = User.objects.create_user('temp_agent_payout@test.com', 'password', user_type='agent')
             Wallet.objects.create(user=agent, balance=Decimal('0.00'))
             payout = AgentPayout.objects.create(
                agent=agent,
                betting_period=period,
                total_turnover=Decimal('1000.00'),
                total_winnings=Decimal('500.00'),
                ggr=Decimal('500.00'),
                commission_rate=Decimal('0.10'),
                commission_amount=Decimal('50.00'),
                status='pending'
            )
    
    args['payout_id'] = payout.id if payout else 1

    return args

def resolve_url_args(url_pattern, args_map):
    # This is a heuristic based on parameter names found in urls.py
    # Returns a tuple of args or None if we can't satisfy them
    
    import re
    # Extract param names from pattern regex or string
    # Django 2.0+ path() objects have .pattern.converters
    
    route = str(url_pattern.pattern)
    required_params = []
    
    # Simple regex to find <type:name> or <name>
    params = re.findall(r'<(?:\w+:)?(\w+)>', route)
    
    resolved_args = []
    
    for param in params:
        val = None
        if param == 'ticket_id':
            # Check type in route
            if '<uuid:' in route:
                val = args_map.get('ticket_id_uuid')
            else:
                val = args_map.get('ticket_id_str')
        elif param == 'user_id':
            val = args_map.get('user_id')
        elif param == 'cashier_id':
            val = args_map.get('cashier_id')
        elif param == 'period_id':
            # Could be betting or commission period. 
            # Context matters but for health check we try one.
            val = args_map.get('period_id')
        elif param == 'fixture_id':
            val = args_map.get('fixture_id')
        elif param == 'withdrawal_id':
            val = args_map.get('withdrawal_id')
        elif param == 'payout_id':
            val = args_map.get('payout_id')
        elif param == 'pk':
             # Fallback for generic pk, usually user or object
             val = args_map.get('user_id') 
        
        if val is None:
            return None # Can't resolve this URL
        resolved_args.append(val)
        
    return resolved_args

def run_checks():
    admin_user, agent_user, player_user = get_test_users()
    args_map = get_dummy_args()
    
    client = Client()
    
    resolver = get_resolver()
    url_patterns = resolver.url_patterns
    
    # Flatten patterns (simplified, ignoring deep nesting for now if not needed)
    # But Django URL patterns can be nested (include()).
    
    def extract_views(patterns, prefix=''):
        views = []
        for p in patterns:
            if hasattr(p, 'url_patterns'):
                # It's an include
                # We need to handle prefix if possible, but path() objects make this tricky to reconstruct string
                # For this simple check, we iterate recursively
                # Warning: prefix handling with objects is complex.
                # Let's try to reverse() if possible? 
                # No, iterating patterns is better to find ALL defined ones.
                views.extend(extract_views(p.url_patterns, prefix + str(p.pattern)))
            else:
                views.append((prefix + str(p.pattern), p.name, p))
        return views

    # Note: extracting raw regex/route strings is messy. 
    # Better approach: Iterate and try to construct path.
    # For now, let's manually list critical URLs or use a flattened list if possible.
    
    # Let's try to get all reversible names? No, some might not be named.
    # Let's stick to the list we read from betting/urls.py and construct them manually for safety.
    
    # Actually, iterating the resolver is robust.
    all_routes = extract_views(url_patterns)
    
    print(f"Found {len(all_routes)} URL patterns.")
    
    errors = []
    
    # We will test as Admin (most permissions) to catch 500s.
    # We can also test as Anonymous to check public pages.
    
    client.force_login(admin_user)
    print("Running checks as ADMIN...")
    
    for route_str, name, pattern in all_routes:
        # Skip admin site internals, static, media
        if route_str.startswith('admin/') or route_str.startswith('static/') or route_str.startswith('media/'):
            continue
            
        # Skip API for now
        if 'api/' in route_str:
            continue
            
        # Skip logout
        if 'logout' in route_str:
            continue
            
        # Construct path
        # We need to inject args
        # route_str is like 'fixtures/<int:period_id>/'
        
        import re
        # Naive replacement
        test_path = route_str
        
        # Check params
        params = re.findall(r'<(?:\w+:)?(\w+)>', route_str)
        skip = False
        for param in params:
            val = None
            if param == 'ticket_id':
                if '<uuid:' in route_str:
                    val = args_map.get('ticket_id_uuid')
                else:
                    val = args_map.get('ticket_id_str')
            elif param == 'user_id':
                val = args_map.get('user_id')
            elif param == 'cashier_id':
                val = args_map.get('cashier_id')
            elif param == 'period_id':
                val = args_map.get('period_id')
            elif param == 'fixture_id':
                val = args_map.get('fixture_id')
            elif param == 'withdrawal_id':
                val = args_map.get('withdrawal_id')
            elif param == 'payout_id':
                val = args_map.get('payout_id')
            elif param == 'pk':
                val = args_map.get('user_id') 
            
            if val is None:
                print(f"SKIPPING {name} ({route_str}): Missing arg for {param}")
                skip = True
                break
            
            # Replace in string
            # Regex replace to handle type prefix
            test_path = re.sub(r'<(?:\w+:)?' + param + r'>', str(val), test_path, 1)
            
        if skip:
            continue
            
        # Clean up optional chars regex? Django route patterns are not simple strings.
        # But for 'path', str(pattern.pattern) returns the route string usually.
        # If it's re_path, it returns regex.
        
        if '^' in test_path or '$' in test_path or '?' in test_path:
             # It's likely regex or complex. Skip for safety in this simple script.
             # Most of our urls are path().
             pass
             
        # Normalize path
        if not test_path.startswith('/'):
            test_path = '/' + test_path
            
        print(f"Checking {test_path} ...", end='')
        try:
            resp = client.get(test_path)
            if resp.status_code >= 500:
                print(f" [FAIL] {resp.status_code}")
                errors.append(f"500 ERROR at {test_path} (View: {name})")
            elif resp.status_code == 404:
                print(f" [404] (Likely missing data)")
            else:
                print(f" [OK] {resp.status_code}")
        except Exception as e:
            print(f" [EXCEPTION] {e}")
            errors.append(f"EXCEPTION at {test_path}: {e}")
            
    print("\n" + "="*30)
    if errors:
        print(f"FOUND {len(errors)} ERRORS:")
        for e in errors:
            print(e)
    else:
        print("NO 500 ERRORS FOUND.")

if __name__ == '__main__':
    run_checks()
