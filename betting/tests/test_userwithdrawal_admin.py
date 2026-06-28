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
