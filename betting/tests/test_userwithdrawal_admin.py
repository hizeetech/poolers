from decimal import Decimal
from unittest.mock import patch

from django.contrib.admin.sites import AdminSite
from django.test import TestCase
from django.urls import reverse

from betting.admin import UserWithdrawalAdmin
from betting.models import User, UserWithdrawal, Wallet


class UserWithdrawalAdminTests(TestCase):
    def setUp(self):
        self.site = AdminSite()
        self.admin = UserWithdrawalAdmin(UserWithdrawal, self.site)
        self.password = "password123"

        self.withdrawal_user = User.objects.create_user(
            email="withdraw-user@test.com",
            password=self.password,
            user_type="cashier",
            username="withdraw_user_cashier",
        )
        self.admin_user = User.objects.create_user(
            email="withdraw-admin@test.com",
            password=self.password,
            user_type="admin",
            username="withdraw_admin_root",
            is_staff=True,
            is_superuser=True,
        )
        Wallet.objects.create(user=self.withdrawal_user, balance=Decimal("5000.00"))
        Wallet.objects.create(user=self.admin_user, balance=Decimal("0.00"))

    def _create_withdrawal(self, *, status="pending"):
        return UserWithdrawal.objects.create(
            user=self.withdrawal_user,
            amount=Decimal("1000.00"),
            bank_name="Demo Bank",
            account_name="Demo User",
            account_number="0123456789",
            status=status,
        )

    def test_userwithdrawal_changelist_includes_websocket_live_refresh_script(self):
        self._create_withdrawal()
        self.client.force_login(self.admin_user)

        response = self.client.get(reverse("betting_admin:betting_userwithdrawal_changelist"))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "betting/admin/userwithdrawal_change_list.html")
        self.assertContains(response, "refreshUserWithdrawalList")
        self.assertContains(response, "/ws/admin/userwithdrawal/")
        self.assertContains(response, "connectSocket")
        self.assertContains(response, "startPolling")
        self.assertContains(response, "POLL_INTERVAL_MS = 15000")

    def test_userwithdrawal_changelist_reopen_with_insufficient_funds_shows_form_error(self):
        Wallet.objects.filter(user=self.withdrawal_user).update(balance=Decimal("100.00"))
        withdrawal = self._create_withdrawal(status="rejected")
        self.client.force_login(self.admin_user)

        changelist_url = reverse("betting_admin:betting_userwithdrawal_changelist")
        response = self.client.get(changelist_url)

        self.assertEqual(response.status_code, 200)
        formset = response.context["cl"].formset

        post_data = {
            field.html_name: field.value() or ""
            for field in formset.management_form
        }
        for form in formset.forms:
            for field in form:
                post_data[field.html_name] = field.value() or ""

        target_form = next(form for form in formset.forms if form.instance.pk == withdrawal.pk)
        post_data[f"{target_form.prefix}-status"] = "approved"
        post_data["_save"] = "Save"

        post_response = self.client.post(changelist_url, post_data, follow=True)

        self.assertEqual(post_response.status_code, 200)
        self.assertContains(
            post_response,
            "Cannot reopen this withdrawal request because the user&#x27;s wallet balance is insufficient to re-deduct the withdrawal amount.",
            html=False,
        )

        withdrawal.refresh_from_db()
        self.assertEqual(withdrawal.status, "rejected")

    @patch("betting.signals.schedule_admin_userwithdrawal_refresh")
    @patch("betting.signals.transaction.on_commit")
    @patch("betting.signals._run_after_commit_in_background")
    def test_userwithdrawal_save_schedules_admin_refresh(
        self,
        _mock_background,
        _mock_on_commit,
        mock_schedule_refresh,
    ):
        withdrawal = self._create_withdrawal()

        mock_schedule_refresh.assert_called_once_with(
            {
                "withdrawal_id": str(withdrawal.id),
                "status": "pending",
                "created": True,
            }
        )
