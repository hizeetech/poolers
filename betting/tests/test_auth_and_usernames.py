from django.contrib.auth import authenticate, get_user_model
from django.test import RequestFactory
from django.test import TestCase

from betting.models import State
from betting.services.usernames import (
    create_agent_and_cashiers,
    generate_agent_username,
    generate_cashier_usernames,
)


User = get_user_model()


class UsernameGenerationTests(TestCase):
    def setUp(self):
        self.state, _ = State.objects.get_or_create(state_name="Kano", defaults={"abbreviation": "Kan"})

    def test_generate_agent_username_prefers_first_last_other_in_order(self):
        User.objects.create_user(
            email="a1@internal.invalid",
            password="pass12345",
            username="KanPeter",
            first_name="Peter",
            last_name="Paul",
            other_name="James",
            state=self.state,
            user_type="agent",
            is_staff=True,
        )
        User.objects.create_user(
            email="a2@internal.invalid",
            password="pass12345",
            username="KanPaul",
            first_name="Peter",
            last_name="Paul",
            other_name="James",
            state=self.state,
            user_type="agent",
            is_staff=True,
        )

        username, roots, base_root = generate_agent_username(
            User, self.state.abbreviation, "Peter", "Paul", "James"
        )
        self.assertEqual(username, "KanJames")
        self.assertIn("KanPeter", roots)
        self.assertIn("KanPaul", roots)
        self.assertIn("KanJames", roots)
        self.assertEqual(base_root, "KanPeter")

    def test_generate_agent_username_appends_numeric_suffix_when_all_taken(self):
        for taken in ["KanPeter", "KanPaul", "KanJames", "KanPeter1", "KanPeter2"]:
            User.objects.create_user(
                email=f"{taken.lower()}@internal.invalid",
                password="pass12345",
                username=taken,
                first_name="Peter",
                last_name="Paul",
                other_name="James",
                state=self.state,
                user_type="agent",
                is_staff=True,
            )

        username, _, _ = generate_agent_username(User, self.state.abbreviation, "Peter", "Paul", "James")
        self.assertEqual(username, "KanPeter3")

    def test_generate_cashier_usernames_uses_alternate_root_when_preferred_conflicts(self):
        preferred_root = "KanPeter"
        roots = ["KanPeter", "KanPaul", "KanJames"]
        base_root = "KanPeter"

        User.objects.create_user(
            email="existing@internal.invalid",
            password="pass12345",
            username="KanPeterC1",
            first_name="Peter",
            last_name="Paul",
            other_name="James",
            state=self.state,
            user_type="cashier",
            is_staff=True,
        )

        c1, c2, chosen_root = generate_cashier_usernames(User, preferred_root, roots, base_root)
        self.assertEqual((c1, c2), ("KanPaulC1", "KanPaulC2"))
        self.assertEqual(chosen_root, "KanPaul")


class AgentProvisioningTests(TestCase):
    def setUp(self):
        self.state, _ = State.objects.get_or_create(state_name="Lagos", defaults={"abbreviation": "Lag"})
        self.factory = RequestFactory()

    def test_create_agent_and_cashiers_creates_exactly_two_cashiers(self):
        agent, cashiers, cashier_root = create_agent_and_cashiers(
            User,
            email="agent@example.com",
            password="pass12345",
            first_name="Peter",
            last_name="Paul",
            other_name="James",
            state=self.state,
            phone_number="+2340000000000",
            shop_address="Test Shop",
        )

        self.assertEqual(agent.user_type, "agent")
        self.assertTrue(agent.username)
        self.assertEqual(len(cashiers), 2)
        self.assertTrue(cashiers[0].username.endswith("C1"))
        self.assertTrue(cashiers[1].username.endswith("C2"))
        self.assertEqual(cashiers[0].email, "C1agent@example.com")
        self.assertEqual(cashiers[1].email, "C2agent@example.com")
        self.assertEqual(cashiers[0].agent_id, agent.id)
        self.assertEqual(cashiers[1].agent_id, agent.id)
        self.assertIn(cashier_root, cashiers[0].username)

    def test_authenticate_with_email_or_username(self):
        agent, _, _ = create_agent_and_cashiers(
            User,
            email="agent2@example.com",
            password="pass12345",
            first_name="Ada",
            last_name="Lovelace",
            other_name="Augusta",
            state=self.state,
        )

        request = self.factory.post("/login/")
        user_by_email = authenticate(request=request, username="agent2@example.com", password="pass12345")
        self.assertIsNotNone(user_by_email)
        self.assertEqual(user_by_email.id, agent.id)

        user_by_username = authenticate(request=request, username=agent.username, password="pass12345")
        self.assertIsNotNone(user_by_username)
        self.assertEqual(user_by_username.id, agent.id)

    def test_login_view_accepts_identifier_for_agent_and_cashier(self):
        agent, cashiers, _ = create_agent_and_cashiers(
            User,
            email="agent3@example.com",
            password="pass12345",
            first_name="Peter",
            last_name="Paul",
            other_name="James",
            state=self.state,
        )

        resp_agent = self.client.post("/login/", {"identifier": agent.username, "password": "pass12345"})
        self.assertEqual(resp_agent.status_code, 302)

        self.client.logout()
        cashier = cashiers[0]
        resp_cashier = self.client.post("/login/", {"identifier": cashier.username, "password": "pass12345"})
        self.assertEqual(resp_cashier.status_code, 302)
