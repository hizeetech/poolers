from decimal import Decimal
from unittest.mock import patch

from django.contrib.admin.sites import AdminSite
from django.test import RequestFactory
from django.test import TestCase
from django.urls import reverse

from betting.admin import (
    ProcessedWithdrawalAdmin,
    UserWithdrawalAdmin,
    UserWithdrawalAdminForm,
    betting_admin_site,
)
from betting.models import ProcessedWithdrawal, User, UserWithdrawal, Wallet


class UserWithdrawalAdminTests(TestCase):
    def setUp(self):
        self.site = AdminSite()
        self.admin = UserWithdrawalAdmin(UserWithdrawal, self.site)
        self.processed_admin = ProcessedWithdrawalAdmin(ProcessedWithdrawal, self.site)
        self.factory = RequestFactory()
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
        self.assertContains(response, "SUBMIT_REFRESH_PAUSE_MS = 5000")
        self.assertContains(response, "pauseLiveRefresh()")
        self.assertContains(response, "LIVE_REFRESH_PAUSE_KEY = 'userWithdrawalAdminPauseUntil'")

    def test_userwithdrawal_admin_queryset_only_shows_pending_requests(self):
        pending_withdrawal = self._create_withdrawal(status="pending")
        self._create_withdrawal(status="approved")
        self._create_withdrawal(status="rejected")
        self._create_withdrawal(status="completed")

        request = self.factory.get("/admin/betting/userwithdrawal/")
        request.user = self.admin_user

        qs = self.admin.get_queryset(request)

        self.assertEqual(list(qs.values_list("id", flat=True)), [pending_withdrawal.id])

    def test_processed_withdrawal_admin_queryset_only_shows_updated_requests(self):
        self._create_withdrawal(status="pending")
        approved_withdrawal = self._create_withdrawal(status="approved")
        rejected_withdrawal = self._create_withdrawal(status="rejected")
        completed_withdrawal = self._create_withdrawal(status="completed")

        request = self.factory.get("/admin/betting/processedwithdrawal/")
        request.user = self.admin_user

        qs = self.processed_admin.get_queryset(request)

        self.assertCountEqual(
            list(qs.values_list("id", flat=True)),
            [approved_withdrawal.id, rejected_withdrawal.id, completed_withdrawal.id],
        )

    def test_processed_withdrawal_admin_list_display_shows_audit_columns(self):
        self.assertEqual(
            self.processed_admin.list_display,
            (
                "short_id",
                "user",
                "amount",
                "status",
                "balance_before_display",
                "balance_after_display",
                "approved_rejected_by_display",
                "approved_rejected_time_display",
                "request_time",
                "bank_name",
                "account_number",
                "account_name",
            ),
        )

    def test_userwithdrawal_admin_form_blocks_reopen_with_insufficient_funds(self):
        Wallet.objects.filter(user=self.withdrawal_user).update(balance=Decimal("100.00"))
        withdrawal = self._create_withdrawal(status="rejected")
        form = UserWithdrawalAdminForm(
            data={
                "user": str(withdrawal.user_id),
                "amount": str(withdrawal.amount),
                "bank_name": withdrawal.bank_name,
                "account_name": withdrawal.account_name,
                "account_number": withdrawal.account_number,
                "status": "approved",
            },
            instance=withdrawal,
        )

        self.assertFalse(form.is_valid())
        self.assertIn(
            "Cannot reopen this withdrawal request because the user's wallet balance is insufficient to re-deduct the withdrawal amount.",
            form.non_field_errors(),
        )

        withdrawal.refresh_from_db()
        self.assertEqual(withdrawal.status, "rejected")

    def test_userwithdrawal_changelist_approves_pending_withdrawal_on_first_save(self):
        withdrawal = self._create_withdrawal(status="pending")
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
        self.assertContains(post_response, "1 user withdrawal was changed successfully.")

        withdrawal.refresh_from_db()
        self.assertEqual(withdrawal.status, "approved")
        self.assertIsNotNone(withdrawal.approved_rejected_time)
        self.assertEqual(withdrawal.approved_rejected_by_id, self.admin_user.id)

    def test_admin_index_shows_pending_only_user_withdrawal_counter(self):
        self._create_withdrawal(status="pending")
        self._create_withdrawal(status="approved")
        self._create_withdrawal(status="rejected")
        self.client.force_login(self.admin_user)

        response = self.client.get(reverse("betting_admin:index"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "User Withdrawal")
        self.assertContains(response, "Pending withdrawals: 1")
        self.assertContains(response, "Processed Withdrawals")
        self.assertContains(response, reverse("betting_admin:betting_processedwithdrawal_changelist"))

    def test_admin_app_list_moves_processed_withdrawals_to_processed_withdrawals_section(self):
        request = self.factory.get("/admin/")
        request.user = self.admin_user

        app_list = betting_admin_site.get_app_list(request)

        processed_withdrawals_app = next(app for app in app_list if app["name"] == "Processed Withdrawals")
        self.assertEqual(processed_withdrawals_app["models"][0]["name"], "Processed Withdrawals")

        betting_app = next(app for app in app_list if app["app_label"] == "betting")
        self.assertFalse(any(model["object_name"] == "ProcessedWithdrawal" for model in betting_app["models"]))

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
