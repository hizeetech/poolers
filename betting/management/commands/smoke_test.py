from django.core.management.base import BaseCommand
from django.test import Client
from django.urls import reverse
from django.contrib.auth import get_user_model
from betting.models import User, Wallet

class Command(BaseCommand):
    help = 'Runs a smoke test to verify critical pages are accessible'

    def handle(self, *args, **options):
        client = Client()
        User = get_user_model()
        
        # Public pages
        urls = [
            'betting:frontpage',
            'betting:login',
            'betting:register',
            'betting:fixtures',
            'betting:check_ticket_status',
        ]

        self.stdout.write(self.style.SUCCESS('Testing Public Pages...'))
        for url_name in urls:
            try:
                url = reverse(url_name)
                response = client.get(url)
                if response.status_code == 200:
                    self.stdout.write(self.style.SUCCESS(f'✓ {url_name} ({url})'))
                else:
                    self.stdout.write(self.style.ERROR(f'✗ {url_name} ({url}) - Status: {response.status_code}'))
            except Exception as e:
                self.stdout.write(self.style.ERROR(f'✗ {url_name} - Error: {str(e)}'))

        # Protected pages (require login)
        self.stdout.write(self.style.SUCCESS('\nTesting Protected Pages (Simulating Player Login)...'))
        
        # Create or get a test player
        email = 'smokeplayer@example.com'
        username = 'smokeplayer'
        password = 'testpassword123'
        
        user = User.objects.filter(username__iexact=username).first()
        if user:
            changed = False
            if user.email != email:
                user.email = email
                changed = True
            if user.user_type != 'player':
                user.user_type = 'player'
                changed = True
            if user.is_superuser:
                user.is_superuser = False
                changed = True
            if user.is_staff:
                user.is_staff = False
                changed = True
            if changed:
                user.save()
        else:
            user = User.objects.create_user(email=email, password=password, username=username, user_type='player')
            # Create wallet for user
            Wallet.objects.get_or_create(user=user)

        client.force_login(user)
        
        protected_urls = [
            'betting:user_dashboard',
            'betting:wallet',
            'betting:profile',
        ]

        for url_name in protected_urls:
            try:
                url = reverse(url_name)
                response = client.get(url)
                if response.status_code == 200:
                    self.stdout.write(self.style.SUCCESS(f'✓ {url_name} ({url})'))
                elif response.status_code == 302:
                     self.stdout.write(self.style.WARNING(f'⚠ {url_name} ({url}) - Redirected to {response.url}'))
                else:
                    self.stdout.write(self.style.ERROR(f'✗ {url_name} ({url}) - Status: {response.status_code}'))
            except Exception as e:
                self.stdout.write(self.style.ERROR(f'✗ {url_name} - Error: {str(e)}'))
