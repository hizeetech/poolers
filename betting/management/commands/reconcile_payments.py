import json
import os
import base64
from decimal import Decimal
from datetime import timedelta

import requests
from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction as db_transaction
from django.utils import timezone

from betting.models import PaymentGatewayEventLog, Transaction, Wallet


class Command(BaseCommand):
    help = "Reconcile pending/failed deposits by re-verifying with gateways and crediting missed wallets."

    def add_arguments(self, parser):
        parser.add_argument("--gateway", default="all", choices=["all", "paystack", "kora", "monnify"])
        parser.add_argument("--days", type=int, default=7)
        parser.add_argument("--limit", type=int, default=200)
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--include-failed", action="store_true")

    def handle(self, *args, **options):
        gateway = (options.get("gateway") or "all").strip().lower()
        days = int(options.get("days") or 7)
        limit = int(options.get("limit") or 200)
        dry_run = bool(options.get("dry_run"))
        include_failed = bool(options.get("include_failed"))

        cutoff = timezone.now() - timedelta(days=max(days, 1))
        qs = Transaction.objects.filter(transaction_type="deposit", timestamp__gte=cutoff).exclude(status="completed")
        if not include_failed:
            qs = qs.filter(status="pending")
        if gateway != "all":
            qs = qs.filter(payment_gateway=gateway)

        candidates = list(qs.order_by("-timestamp")[:limit])
        self.stdout.write(f"Reconciling {len(candidates)} deposits (gateway={gateway}, days={days}, dry_run={dry_run})")

        processed = 0
        credited = 0
        skipped = 0
        failed = 0

        for tx in candidates:
            processed += 1
            ref = (tx.external_reference or "").strip()
            gw = (getattr(tx, "payment_gateway", "") or "paystack").strip().lower()
            if not ref:
                skipped += 1
                PaymentGatewayEventLog.objects.create(
                    gateway=gw,
                    event_type="reconcile",
                    reference="",
                    transaction=tx,
                    user=tx.user,
                    amount=tx.amount,
                    success=False,
                    message="Missing external_reference",
                    payload={},
                )
                continue

            try:
                ok, amount_verified, payload, http_status, msg = self._verify_with_gateway(tx=tx, gateway=gw, reference=ref)
            except Exception as e:
                failed += 1
                PaymentGatewayEventLog.objects.create(
                    gateway=gw,
                    event_type="reconcile",
                    reference=ref,
                    transaction=tx,
                    user=tx.user,
                    amount=tx.amount,
                    success=False,
                    message=str(e),
                    payload={},
                )
                continue

            PaymentGatewayEventLog.objects.create(
                gateway=gw,
                event_type="reconcile",
                reference=ref,
                transaction=tx,
                user=tx.user,
                amount=tx.amount,
                success=bool(ok),
                http_status=http_status,
                message=(msg or ""),
                payload=payload or {},
            )

            if not ok:
                skipped += 1
                continue

            amount_q = self._q(amount_verified)
            if amount_q is None or amount_q <= 0:
                failed += 1
                continue

            if self._q(tx.amount) != amount_q:
                failed += 1
                with db_transaction.atomic():
                    locked = Transaction.objects.select_for_update().get(pk=tx.pk)
                    locked.status = "failed"
                    locked.is_successful = False
                    locked.description = f"Amount mismatch: Expected {locked.amount}, Got {amount_q}"
                    locked.save(update_fields=["status", "is_successful", "description"])
                continue

            if dry_run:
                credited += 1
                continue

            with db_transaction.atomic():
                locked = Transaction.objects.select_for_update().select_related("user").get(pk=tx.pk)
                if locked.status == "completed" and locked.is_successful:
                    continue
                wallet, _ = Wallet.objects.select_for_update().get_or_create(user=locked.user, defaults={"balance": Decimal("0.00")})
                wallet.apply_delta(
                    amount=amount_q,
                    actor=None,
                    transaction_obj=locked,
                    reference=ref,
                    reason=f"Deposit via {gw} (reconcile)",
                    metadata={"gateway": gw, "source": "reconcile"},
                )
                locked.status = "completed"
                locked.is_successful = True
                locked.description = f"Online deposit via {gw} successful."
                locked.timestamp = timezone.now()
                locked.save(update_fields=["status", "is_successful", "description", "timestamp"])

            credited += 1

        self.stdout.write(
            f"Done. processed={processed} credited={credited} skipped={skipped} failed={failed}"
        )

    def _q(self, amount):
        try:
            return Decimal(str(amount)).quantize(Decimal("0.01"))
        except Exception:
            return None

    def _verify_with_gateway(self, *, tx, gateway, reference):
        gateway = (gateway or "").strip().lower()
        if gateway == "paystack":
            secret = (getattr(settings, "PAYSTACK_SECRET_KEY", None) or "").strip()
            if not secret:
                raise RuntimeError("Missing PAYSTACK_SECRET_KEY")
            url = f"https://api.paystack.co/transaction/verify/{reference}"
            resp = requests.get(url, headers={"Authorization": f"Bearer {secret}"}, timeout=15)
            payload = resp.json()
            data = payload.get("data") or {}
            ok = bool(payload.get("status") and data.get("status") == "success")
            amount_verified = (Decimal(str(data.get("amount") or "0")) / Decimal("100")).quantize(Decimal("0.01"))
            msg = str(data.get("gateway_response") or data.get("message") or payload.get("message") or "")
            return ok, amount_verified, {"response": payload}, getattr(resp, "status_code", None), msg

        if gateway == "kora":
            secret_key = (os.getenv("KORA_SECRET_KEY") or os.getenv("KORAPAY_SECRET_KEY") or "").strip()
            base_url = os.getenv("KORA_BASE_URL") or os.getenv("KORAPAY_BASE_URL") or "https://api.korapay.com/merchant/api/v1"
            if base_url.rstrip("/").endswith("/merchant/api"):
                base_url = f"{base_url.rstrip('/')}/v1"
            if not secret_key:
                raise RuntimeError("Missing KORA_SECRET_KEY")
            url = f"{base_url.rstrip('/')}/charges/{reference}"
            resp = requests.get(url, headers={"Authorization": f"Bearer {secret_key}"}, timeout=15)
            payload = resp.json()
            ok = bool(payload.get("status") and (payload.get("data") or {}).get("status") == "success")
            amount_verified = Decimal(str((payload.get("data") or {}).get("amount") or "0"))
            msg = str(payload.get("message") or "")
            return ok, amount_verified, {"response": payload}, getattr(resp, "status_code", None), msg

        if gateway == "monnify":
            api_key = (os.getenv("MONNIFY_API_KEY") or "").strip()
            secret_key = (os.getenv("MONNIFY_SECRET_KEY") or "").strip()
            base_url = (os.getenv("MONNIFY_BASE_URL") or "").strip()
            if not base_url:
                raise RuntimeError("Missing MONNIFY_BASE_URL")
            if not api_key or not secret_key:
                raise RuntimeError("Missing MONNIFY credentials")

            auth_str = base64.b64encode(f"{api_key}:{secret_key}".encode()).decode()
            auth_url = f"{base_url.rstrip('/')}/api/v1/auth/login"
            auth_resp = requests.post(auth_url, headers={"Authorization": f"Basic {auth_str}"}, timeout=15)
            auth_payload = auth_resp.json() if auth_resp.content else {}
            if not bool(auth_payload.get("requestSuccessful")):
                msg = str(auth_payload.get("responseMessage") or "Authentication failed")
                return False, Decimal("0.00"), {"auth": auth_payload}, getattr(auth_resp, "status_code", None), msg

            token = ((auth_payload.get("responseBody") or {}).get("accessToken") or "").strip()
            if not token:
                return False, Decimal("0.00"), {"auth": auth_payload}, getattr(auth_resp, "status_code", None), "Missing access token"

            verify_url = f"{base_url.rstrip('/')}/api/v1/merchant/transactions/query?paymentReference={reference}"
            verify_resp = requests.get(verify_url, headers={"Authorization": f"Bearer {token}"}, timeout=15)
            verify_payload = verify_resp.json() if verify_resp.content else {}
            body = verify_payload.get("responseBody") or {}
            ok = bool(verify_payload.get("requestSuccessful") and body.get("paymentStatus") == "PAID")
            amount_verified = Decimal(str(body.get("amountPaid") or "0"))
            msg = str(verify_payload.get("responseMessage") or body.get("paymentStatus") or "")
            return ok, amount_verified, {"response": verify_payload}, getattr(verify_resp, "status_code", None), msg

        raise RuntimeError(f"Unsupported gateway: {gateway}")
