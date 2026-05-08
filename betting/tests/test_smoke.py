from django.test import TestCase, Client
from django.urls import reverse
from django.urls.resolvers import URLPattern, URLResolver
from django.contrib.auth import get_user_model
from betting.models import State

class SmokeTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.password = 'password123'
        User = get_user_model()

        self.state = State.objects.create(state_name='Test State', abbreviation='TS')
        
        # Create Admin User
        self.admin = User.objects.create_user(
            email='smokeadmin@test.com', 
            password=self.password, 
            user_type='admin', 
            is_staff=True, 
            is_superuser=True
        )

        self.agent = User.objects.create_user(
            email='smokeagent@test.com',
            password=self.password,
            user_type='agent',
            first_name='Agent',
            last_name='User',
            other_name='Test',
            state=self.state,
            is_active=True,
            is_staff=True
        )

        self.cashier = User.objects.create_user(
            email='smokecashier@test.com',
            password=self.password,
            user_type='cashier',
            first_name='Cashier',
            last_name='User',
            other_name='Test',
            agent=self.agent,
            state=self.state,
            is_active=True,
            is_staff=True
        )

    def _iter_named_zero_arg_urls(self):
        def walk(patterns, namespace=None):
            for p in patterns:
                if isinstance(p, URLPattern):
                    if not p.name:
                        continue
                    route = getattr(p.pattern, '_route', None)
                    if route and '<' in route:
                        continue
                    name = f"{namespace}:{p.name}" if namespace else p.name
                    yield name
                elif isinstance(p, URLResolver):
                    ns = namespace
                    if p.namespace:
                        ns = f"{namespace}:{p.namespace}" if namespace else p.namespace
                    yield from walk(p.url_patterns, namespace=ns)

        from django.urls import get_resolver
        names = sorted(set(walk(get_resolver().url_patterns)))
        return names

    def _assert_no_500s(self, url_names, client, label):
        failures = []
        for url_name in url_names:
            try:
                url = reverse(url_name)
            except Exception:
                continue
            response = client.get(url)
            if response.status_code >= 500:
                failures.append((url_name, url, response.status_code))
        if failures:
            msg = "\n".join([f"{n} -> {u} ({s})" for n, u, s in failures])
            self.fail(f"Smoke failures ({label}):\n{msg}")

    def test_public_urls(self):
        url_names = self._iter_named_zero_arg_urls()
        self._assert_no_500s(url_names, self.client, label="anonymous")

    def test_protected_urls_admin(self):
        self.client.force_login(self.admin)
        url_names = self._iter_named_zero_arg_urls()
        self._assert_no_500s(url_names, self.client, label="admin")

    def test_dashboard_redirection(self):
        """Test dashboard redirection for admin."""
        self.client.force_login(self.admin)
        url = reverse('betting:user_dashboard')
        response = self.client.get(url)
        self.assertIn(response.status_code, [200, 302])

    def test_login_post_cashier_does_not_500(self):
        url = reverse('betting:login')
        response = self.client.post(url, data={'username': self.cashier.email, 'password': self.password})
        self.assertLess(response.status_code, 500)

    def test_basic_authenticated_pages_agent(self):
        self.client.force_login(self.agent)
        url_names = [
            'betting:wallet',
            'betting:profile',
            'betting:user_dashboard',
        ]
        self._assert_no_500s(url_names, self.client, label="agent-basic")

    def test_basic_authenticated_pages_cashier(self):
        self.client.force_login(self.cashier)
        url_names = [
            'betting:wallet',
            'betting:profile',
            'betting:user_dashboard',
        ]
        self._assert_no_500s(url_names, self.client, label="cashier-basic")
