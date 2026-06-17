from django.contrib.auth import authenticate, get_user_model
from django.test import RequestFactory
from django.test import TestCase
from django.urls import reverse
import json

from betting.forms import AdminUserCreationForm, CRMUserProfileForm, ProfileEditForm
from betting.models import EmailAuditLog
from pending_registration.forms import AgentRegistrationForm
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
        self.assertEqual(cashiers[0].email, "agent@example.com")
        self.assertEqual(cashiers[1].email, "agent@example.com")
        self.assertEqual(cashiers[0].agent_id, agent.id)
        self.assertEqual(cashiers[1].agent_id, agent.id)
        self.assertIn(cashier_root, cashiers[0].username)

    def test_authenticate_with_username_after_agent_provisioning(self):
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
        user_by_username = authenticate(request=request, username=agent.username, password="pass12345")
        self.assertIsNotNone(user_by_username)
        self.assertEqual(user_by_username.id, agent.id)

    def test_authenticate_rejects_email_login_even_when_email_exists(self):
        User.objects.create_user(
            email="shared@example.com",
            password="pass12345",
            username="shared_user_one",
            user_type="agent",
        )

        request = self.factory.post("/login/")
        self.assertIsNone(authenticate(request=request, username="shared@example.com", password="pass12345"))
 
    def test_authenticate_with_duplicate_email_requires_username(self):
        User.objects.create_user(
            email="shared@example.com",
            password="pass12345",
            username="shared_user_one",
            user_type="agent",
        )
        second = User.objects.create_user(
            email="shared@example.com",
            password="pass12345",
            username="shared_user_two",
            user_type="agent",
        )

        request = self.factory.post("/login/")
        user_by_username = authenticate(request=request, username=second.username, password="pass12345")
        self.assertIsNotNone(user_by_username)
        self.assertEqual(user_by_username.id, second.id)

    def test_forgot_password_with_duplicate_email_requires_username(self):
        user = User.objects.create_user(
            email="recover-shared@example.com",
            password="pass12345",
            username="recover_shared_one",
            user_type="agent",
        )
        User.objects.create_user(
            email="recover-shared@example.com",
            password="pass12345",
            username="recover_shared_two",
            user_type="agent",
        )

        response = self.client.post(
            reverse("betting:forgot_password"),
            {"identifier": "recover-shared@example.com"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(response.status_code, 200)
        self.assertJSONEqual(
            response.content,
            {
                "status": "error",
                "message": "Multiple accounts use this email address. Enter your username instead.",
            },
        )

        username_response = self.client.post(
            reverse("betting:forgot_password"),
            {"identifier": user.username},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(username_response.status_code, 200)
        self.assertEqual(username_response.json()["status"], "success")

    def test_email_usage_check_reports_duplicate_email(self):
        User.objects.create_user(
            email="dupe-check@example.com",
            password="pass12345",
            username="dupe_check_one",
            user_type="agent",
        )
        User.objects.create_user(
            email="dupe-check@example.com",
            password="pass12345",
            username="dupe_check_two",
            user_type="agent",
        )

        response = self.client.get(reverse("betting:check_email_usage"), {"email": "dupe-check@example.com"})
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["exists"])
        self.assertEqual(payload["count"], 2)
        self.assertEqual({m["username"] for m in payload["matches"]}, {"dupe_check_one", "dupe_check_two"})

    def test_user_model_uses_username_as_auth_field(self):
        self.assertEqual(User.USERNAME_FIELD, "username")
        self.assertIn("email", User.REQUIRED_FIELDS)

    def test_pending_registration_form_requires_duplicate_email_confirmation(self):
        User.objects.create_user(
            email="pending-shared@example.com",
            password="pass12345",
            username="existing_pending_shared",
            user_type="agent",
        )

        data = {
            "full_name": "Pending Shared Agent",
            "email": "pending-shared@example.com",
            "phone": "+2348000000000",
            "state": "Lagos",
            "user_type": "agent",
            "password": "pass12345",
            "confirm_password": "pass12345",
        }

        form = AgentRegistrationForm(data=data)
        self.assertFalse(form.is_valid())
        self.assertIn("email", form.errors)

        confirmed_form = AgentRegistrationForm(data={**data, "confirm_duplicate_email": "1"})
        self.assertTrue(confirmed_form.is_valid(), confirmed_form.errors)

    def test_webauthn_login_begin_rejects_email_identifier(self):
        User.objects.create_user(
            email="shared-bio@example.com",
            password="pass12345",
            username="shared_bio_one",
            user_type="agent",
        )
        User.objects.create_user(
            email="shared-bio@example.com",
            password="pass12345",
            username="shared_bio_two",
            user_type="agent",
        )

        response = self.client.post(
            reverse("betting:webauthn_login_begin"),
            data=json.dumps({"email": "shared-bio@example.com"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json()["message"],
            "Use your username to sign in. Email login is not supported.",
        )

    def test_login_view_rejects_email_identifier(self):
        user = User.objects.create_user(
            email="email-login-reject@example.com",
            password="pass12345",
            username="email_login_reject",
            user_type="agent",
        )

        response = self.client.post("/login/", {"identifier": user.email, "password": "pass12345"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Use your username to log in. Email login is not supported.")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_profile_edit_form_syncs_cashier_emails_after_confirmation(self):
        User.objects.create_user(
            email="taken-profile@example.com",
            password="pass12345",
            username="taken_profile_user",
            user_type="player",
        )
        agent, cashiers, _ = create_agent_and_cashiers(
            User,
            email="agent-profile@example.com",
            password="pass12345",
            first_name="Profile",
            last_name="Agent",
            other_name="Sync",
            state=self.state,
        )
        request = self.factory.post("/profile/")
        request.user = agent

        form = ProfileEditForm(
            data={
                "first_name": agent.first_name,
                "last_name": agent.last_name,
                "email": "taken-profile@example.com",
                "phone_number": agent.phone_number or "",
                "shop_address": agent.shop_address or "",
            },
            instance=agent,
            request=request,
        )
        self.assertFalse(form.is_valid())
        self.assertIn("email", form.errors)

        confirmed_form = ProfileEditForm(
            data={
                "first_name": agent.first_name,
                "last_name": agent.last_name,
                "email": "taken-profile@example.com",
                "phone_number": agent.phone_number or "",
                "shop_address": agent.shop_address or "",
                "confirm_duplicate_email": "1",
                "sync_cashier_emails": "1",
            },
            instance=agent,
            request=request,
        )
        self.assertTrue(confirmed_form.is_valid(), confirmed_form.errors)
        updated_agent = confirmed_form.save()

        self.assertEqual(updated_agent.email, "taken-profile@example.com")
        self.assertEqual(
            list(User.objects.filter(id__in=[c.id for c in cashiers]).values_list("email", flat=True)),
            ["taken-profile@example.com", "taken-profile@example.com"],
        )
        self.assertTrue(
            EmailAuditLog.objects.filter(
                target_user=updated_agent,
                action_type="DUPLICATE_EMAIL_UPDATED",
                email="taken-profile@example.com",
            ).exists()
        )
        self.assertEqual(
            EmailAuditLog.objects.filter(action_type="CASHIER_EMAIL_SYNCHRONIZED", email="taken-profile@example.com").count(),
            2,
        )

    def test_crm_profile_form_requires_duplicate_email_confirmation(self):
        crm_user = User.objects.create_user(
            email="crm-profile@example.com",
            password="pass12345",
            username="crm_profile_actor",
            user_type="crm",
        )
        target = User.objects.create_user(
            email="target-profile@example.com",
            password="pass12345",
            username="target_profile_user",
            user_type="player",
        )
        User.objects.create_user(
            email="shared-crm@example.com",
            password="pass12345",
            username="existing_shared_crm",
            user_type="agent",
        )
        request = self.factory.post("/crm/user-detail/")
        request.user = crm_user

        form = CRMUserProfileForm(
            data={
                "first_name": target.first_name or "",
                "last_name": target.last_name or "",
                "other_name": target.other_name or "",
                "email": "shared-crm@example.com",
                "phone_number": target.phone_number or "",
                "state": "",
                "shop_address": target.shop_address or "",
                "bank_account_name": target.bank_account_name or "",
                "kyc_status": target.kyc_status,
                "vip_level": target.vip_level,
                "vip_manager": "",
            },
            instance=target,
            request=request,
        )
        self.assertFalse(form.is_valid())
        self.assertIn("email", form.errors)

        confirmed_form = CRMUserProfileForm(
            data={
                "first_name": target.first_name or "",
                "last_name": target.last_name or "",
                "other_name": target.other_name or "",
                "email": "shared-crm@example.com",
                "phone_number": target.phone_number or "",
                "state": "",
                "shop_address": target.shop_address or "",
                "bank_account_name": target.bank_account_name or "",
                "kyc_status": target.kyc_status,
                "vip_level": target.vip_level,
                "vip_manager": "",
                "confirm_duplicate_email": "1",
            },
            instance=target,
            request=request,
        )
        self.assertTrue(confirmed_form.is_valid(), confirmed_form.errors)
        updated_user = confirmed_form.save()
        self.assertEqual(updated_user.email, "shared-crm@example.com")
        self.assertTrue(
            EmailAuditLog.objects.filter(
                target_user=updated_user,
                action_type="DUPLICATE_EMAIL_UPDATED",
                email="shared-crm@example.com",
            ).exists()
        )

    def test_admin_user_creation_form_allows_duplicate_email_when_confirmed(self):
        admin_user = User.objects.create_superuser(
            email="superadmin-auth@example.com",
            password="pass12345",
            username="superadmin_auth",
        )
        User.objects.create_user(
            email="admin-dupe@example.com",
            password="pass12345",
            username="existing_admin_dupe",
            user_type="player",
        )
        request = self.factory.post("/admin/users/add/")
        request.user = admin_user

        form = AdminUserCreationForm(
            data={
                "email": "admin-dupe@example.com",
                "username": "new_admin_dupe_user",
                "password": "pass12345",
                "password2": "pass12345",
                "user_type": "player",
                "is_active": "on",
                "confirm_duplicate_email": "1",
            },
            request=request,
        )
        self.assertTrue(form.is_valid(), form.errors)
        created = form.save()
        self.assertEqual(created.email, "admin-dupe@example.com")
        self.assertTrue(created.username)
        self.assertTrue(
            EmailAuditLog.objects.filter(
                target_user=created,
                action_type="DUPLICATE_EMAIL_ASSIGNED",
                email="admin-dupe@example.com",
            ).exists()
        )

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
