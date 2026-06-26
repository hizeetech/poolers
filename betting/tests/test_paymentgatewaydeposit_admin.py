from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from betting.models import PaymentGatewayDeposit


User = get_user_model()


class PaymentGatewayDepositAdminTests(TestCase):
    def setUp(self):
        self.password = "password123"
        self.superadmin = User.objects.create_superuser(
            email="pgd-admin@test.com",
            password=self.password,
        )
        self.customer = User.objects.create_user(
            email="pgd-customer@test.com",
            password=self.password,
            user_type="player",
            username="pgd_customer",
        )
        self.deposit = PaymentGatewayDeposit.objects.create(
            user=self.customer,
            initiating_user=self.customer,
            target_user=self.customer,
            transaction_type="deposit",
            amount=Decimal("2500.00"),
            is_successful=True,
            status="completed",
            description="Gateway deposit test",
            payment_gateway="monnify",
            external_reference="pgd-admin-test-ext-ref",
            paystack_reference="pgd-admin-test-pay-ref",
        )

    def test_paymentgatewaydeposit_change_page_renders(self):
        self.client.force_login(self.superadmin)

        response = self.client.get(
            reverse("betting_admin:betting_paymentgatewaydeposit_change", args=[self.deposit.pk])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Gateway deposit test")
        self.assertContains(response, "pgd-customer@test.com")
