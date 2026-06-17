from __future__ import annotations

from typing import Iterable

from django.contrib.auth import get_user_model


User = get_user_model()


def normalize_email_value(email: str | None) -> str:
    return (email or "").strip().lower()


def is_truthy(value) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def users_with_email(email: str | None, *, exclude_user_id: int | None = None):
    normalized = normalize_email_value(email)
    if not normalized:
        return User.objects.none()
    qs = User.objects.filter(email__iexact=normalized)
    if exclude_user_id:
        qs = qs.exclude(pk=exclude_user_id)
    return qs


def duplicate_email_details(email: str | None, *, exclude_user_id: int | None = None) -> dict:
    qs = users_with_email(email, exclude_user_id=exclude_user_id)
    matches = list(
        qs.order_by("date_joined", "id").values("id", "username", "email", "user_type")
    )
    return {
        "normalized_email": normalize_email_value(email),
        "exists": bool(matches),
        "count": len(matches),
        "matches": matches,
    }


def resolve_user_from_identifier(identifier: str | None):
    raw_identifier = (identifier or "").strip()
    if not raw_identifier:
        return None, "Enter your username."

    if "@" in raw_identifier:
        email_matches = list(users_with_email(raw_identifier)[:2])
        if not email_matches:
            return None, "No user was found with that email address."
        if len(email_matches) > 1:
            return None, "Multiple accounts use this email address. Enter your username instead."
        return email_matches[0], ""

    user = User.objects.filter(username__iexact=raw_identifier).first()
    if not user:
        return None, "No user was found with that username."
    return user, ""


def sync_agent_cashier_emails(agent, new_email: str, *, actor=None) -> list:
    normalized_email = normalize_email_value(new_email)
    updated_cashiers = []
    if not agent or getattr(agent, "user_type", "") != "agent" or not normalized_email:
        return updated_cashiers

    cashiers = list(
        User.objects.filter(agent=agent, user_type="cashier").order_by("id")
    )
    for cashier in cashiers:
        if normalize_email_value(cashier.email) == normalized_email:
            continue
        old_email = cashier.email
        cashier.email = normalized_email
        cashier.save(update_fields=["email"])
        updated_cashiers.append(
            {
                "cashier_id": cashier.id,
                "username": cashier.username,
                "old_email": old_email,
                "new_email": normalized_email,
            }
        )
        log_email_audit(
            action_type="CASHIER_EMAIL_SYNCHRONIZED",
            target_user=cashier,
            email=normalized_email,
            performed_by=actor or agent,
            metadata={"agent_id": agent.id, "old_email": old_email},
        )
    if updated_cashiers:
        log_email_audit(
            action_type="AGENT_CASHIER_EMAIL_SYNC_TRIGGERED",
            target_user=agent,
            email=normalized_email,
            performed_by=actor or agent,
            metadata={"updated_cashiers": updated_cashiers},
        )
    return updated_cashiers


def log_email_audit(*, action_type: str, target_user=None, email: str = "", performed_by=None, metadata: dict | None = None):
    from betting.models import EmailAuditLog

    EmailAuditLog.objects.create(
        target_user=target_user,
        email=normalize_email_value(email) or normalize_email_value(getattr(target_user, "email", "")),
        action_type=action_type,
        performed_by=performed_by,
        metadata=metadata or {},
    )
