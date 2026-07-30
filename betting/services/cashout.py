import hashlib
import uuid
from dataclasses import dataclass
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from betting.models import (
    BetTicket,
    BetTicketCashOut,
    CashOutAuditLog,
    CashOutSettings,
    Transaction,
    User,
    Wallet,
)


ZERO = Decimal("0.00")


@dataclass(frozen=True)
class CashOutQuote:
    eligible: bool
    reason: str
    cashout_amount: Decimal
    original_odds: Decimal
    completed_odds: Decimal
    progress_percent: Decimal
    potential_win: Decimal
    settled_count: int = 0
    winning_count: int = 0
    losing_count: int = 0
    pending_count: int = 0
    charge_type: str = ""
    charge_value: Decimal = ZERO
    offer_percent_of_potential: Decimal = ZERO
    won_odds: Decimal = Decimal("0.000000")
    remaining_odds: Decimal = Decimal("0.000000")
    risk_discount: Decimal = Decimal("0.000000")
    company_margin_percent: Decimal = Decimal("0.00")
    risk_multiplier: Decimal = Decimal("0.0000")


class CashOutError(Exception):
    pass


def _quantize_money(value):
    return Decimal(str(value or "0.00")).quantize(Decimal("0.01"))


def _quantize_ratio(value):
    return Decimal(str(value or "0.0000")).quantize(Decimal("0.0001"))


def _safe_decimal(value, default=ZERO):
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal(str(default))


def _clamp(value, min_value, max_value):
    if value < min_value:
        return min_value
    if value > max_value:
        return max_value
    return value


def _log_invalid_quote(*, ticket, settings_obj, metadata):
    try:
        CashOutAuditLog.objects.create(
            ticket=ticket,
            cashout=None,
            actor=None,
            action="CASHOUT_QUOTE_INVALID",
            message="Invalid cash out quote blocked",
            ip_address=None,
            user_agent="",
            metadata={
                "ticket_id": getattr(ticket, "ticket_id", ""),
                "settings": _cashout_settings_snapshot(settings_obj),
                **(metadata or {}),
            },
        )
    except Exception:
        return


def _fixture_start_dt(selection):
    fixture = getattr(selection, "fixture", None)
    match_date = getattr(fixture, "match_date", None) or getattr(selection, "fixture_match_date", None)
    match_time = getattr(fixture, "match_time", None) or getattr(selection, "fixture_match_time", None)
    if not match_date or not match_time:
        return None
    try:
        naive = timezone.datetime.combine(match_date, match_time)
        return timezone.make_aware(naive, timezone.get_current_timezone())
    except Exception:
        return None


def _is_fixture_started(selection, now):
    fixture = getattr(selection, "fixture", None)
    status = (getattr(fixture, "status", "") or "").strip().lower()
    if status in {"live", "finished", "settled", "cancelled", "postponed", "abandoned", "no_result"}:
        return True
    start_dt = _fixture_start_dt(selection)
    if start_dt and now >= start_dt:
        return True
    return False


def _is_fixture_finished(selection):
    fixture = getattr(selection, "fixture", None)
    status = (getattr(fixture, "status", "") or "").strip().lower()
    return status in {"finished", "settled", "cancelled", "postponed", "abandoned", "no_result"}


def _selection_outcome(selection):
    fixture = getattr(selection, "fixture", None)
    if fixture is None:
        return None, False

    status = (getattr(fixture, "status", "") or "").strip().lower()
    if status in {"cancelled", "postponed", "abandoned", "no_result"}:
        return None, True
    if status not in {"settled", "finished"}:
        return None, False

    home = getattr(fixture, "home_score", None)
    away = getattr(fixture, "away_score", None)
    if home is None or away is None:
        return None, False

    bet_type = (getattr(selection, "bet_type", "") or "").strip().lower()
    try:
        total_goals = home + away
    except Exception:
        total_goals = 0

    if bet_type == "home_win":
        return home > away, True
    if bet_type == "draw":
        return home == away, True
    if bet_type == "away_win":
        return home < away, True
    if bet_type == "home_or_draw":
        return home >= away, True
    if bet_type == "either_team_win":
        return home != away, True
    if bet_type == "away_or_draw":
        return home <= away, True
    if bet_type == "over_1_5":
        return Decimal(total_goals) > Decimal("1.5"), True
    if bet_type == "under_1_5":
        return Decimal(total_goals) <= Decimal("1.5"), True
    if bet_type == "over_2_5":
        return Decimal(total_goals) > Decimal("2.5"), True
    if bet_type == "under_2_5":
        return Decimal(total_goals) <= Decimal("2.5"), True
    if bet_type == "over_3_5":
        return Decimal(total_goals) > Decimal("3.5"), True
    if bet_type == "under_3_5":
        return Decimal(total_goals) <= Decimal("3.5"), True
    if bet_type == "btts_yes":
        return home > 0 and away > 0, True
    if bet_type == "btts_no":
        return home == 0 or away == 0, True
    if bet_type == "home_dnb":
        return (None if home == away else home > away), True
    if bet_type == "away_dnb":
        return (None if home == away else home < away), True
    return False, True


def _ticket_has_pending_fixtures(selections):
    for sel in selections:
        if not _is_fixture_finished(sel):
            return True
    return False


def _ticket_is_alive_by_state(*, ticket, selections, winning_count, losing_count, pending_count):
    if ticket.is_voided or ticket.status in {"won", "lost", "cashed_out"}:
        return False

    if ticket.bet_type == "system" and ticket.system_min_count:
        k = int(ticket.system_min_count or 0)
        if k <= 0:
            return False
        return (winning_count + pending_count) >= k and pending_count > 0

    if losing_count > 0:
        return False
    return pending_count > 0


def _risk_discount_from_remaining_odds(*, remaining_odds, risk_multiplier):
    remaining_odds = _safe_decimal(remaining_odds, Decimal("1.00"))
    if remaining_odds <= Decimal("1.00"):
        return Decimal("1.000000")

    rm = _clamp(_safe_decimal(risk_multiplier, Decimal("0.0500")), Decimal("0.0000"), Decimal("1000.0000"))
    denom = Decimal("1.00") + (rm * (remaining_odds - Decimal("1.00")))
    if denom <= Decimal("0.00"):
        return Decimal("0.000000")
    discount = (Decimal("1.00") / denom).quantize(Decimal("0.000001"))
    return _clamp(discount, Decimal("0.000000"), Decimal("1.000000"))


def _ticket_is_settled(*, ticket, selections):
    if ticket.status in {"won", "lost"}:
        return True
    if ticket.status != "pending":
        return False
    return not _ticket_has_pending_fixtures(selections)


def _cashout_settings_snapshot(settings_obj):
    return {
        "enable_cash_out": bool(settings_obj.enable_cash_out),
        "enable_cash_out_nap": bool(settings_obj.enable_cash_out_nap),
        "enable_cash_out_permutation": bool(settings_obj.enable_cash_out_permutation),
        "enable_full_cash_out": bool(getattr(settings_obj, "enable_full_cash_out", True)),
        "enable_partial_cash_out": bool(getattr(settings_obj, "enable_partial_cash_out", False)),
        "enable_pre_match_cash_out": bool(settings_obj.enable_pre_match_cash_out),
        "disable_cash_out_during_live_events": bool(settings_obj.disable_cash_out_during_live_events),
        "charge_type": str(settings_obj.charge_type),
        "fixed_charge_amount": str(settings_obj.fixed_charge_amount),
        "percentage_charge": str(settings_obj.percentage_charge),
        "company_margin_percent": str(getattr(settings_obj, "company_margin_percent", "0.00")),
        "risk_multiplier": str(getattr(settings_obj, "risk_multiplier", "0.0000")),
        "minimum_stake_eligible": str(settings_obj.minimum_stake_eligible),
        "maximum_stake_eligible": str(settings_obj.maximum_stake_eligible),
        "minimum_cash_out_amount": str(settings_obj.minimum_cash_out_amount),
        "maximum_cash_out_amount": str(settings_obj.maximum_cash_out_amount),
        "manually_closed": bool(settings_obj.manually_closed),
    }


def _cashout_disabled_reason(*, ticket, selections, settings_obj):
    if not settings_obj.enable_cash_out:
        return "Cash Out is currently disabled."
    if settings_obj.manually_closed:
        return "Cash Out has been closed by the administrator."
    if ticket.is_voided:
        return "Cash Out is not available for voided tickets."
    if ticket.status == "cashed_out" or getattr(ticket, "cashout_id", None) or hasattr(ticket, "cashout"):
        return "This ticket has already been cashed out."
    if _ticket_is_settled(ticket=ticket, selections=selections):
        return "Cash Out is not available because this ticket is already settled."

    stake = _safe_decimal(ticket.stake_amount, ZERO)
    if stake < _safe_decimal(settings_obj.minimum_stake_eligible, ZERO):
        return "Cash Out is not available for this stake amount."
    if stake > _safe_decimal(settings_obj.maximum_stake_eligible, stake):
        return "Cash Out is not available for this stake amount."

    if ticket.bet_type == "system":
        if not settings_obj.enable_cash_out_permutation:
            return "Cash Out is disabled for permutation tickets."
    else:
        if not settings_obj.enable_cash_out_nap:
            return "Cash Out is disabled for NAP tickets."

    return ""


def build_cashout_quote(*, ticket, settings_obj=None, now=None):
    settings_obj = settings_obj or CashOutSettings.load()
    now = now or timezone.now()
    selections = list(ticket.selections.select_related("fixture").all())

    reason = _cashout_disabled_reason(ticket=ticket, selections=selections, settings_obj=settings_obj)
    if reason:
        return CashOutQuote(
            eligible=False,
            reason=reason,
            cashout_amount=ZERO,
            original_odds=ZERO,
            completed_odds=ZERO,
            progress_percent=ZERO,
            potential_win=_quantize_money(ticket.potential_winning),
        )

    potential_win = _quantize_money(ticket.potential_winning)
    maximum_win = _quantize_money(getattr(ticket, "max_winning", None) or potential_win)
    ticket_cap = min(potential_win, maximum_win)

    min_cashout_setting = _quantize_money(settings_obj.minimum_cash_out_amount)
    max_cashout_setting = _quantize_money(settings_obj.maximum_cash_out_amount)

    started_not_finished = 0
    settled_count = 0
    winning_count = 0
    losing_count = 0
    pending_count = 0
    for sel in selections:
        if _is_fixture_started(sel, now) and not _is_fixture_finished(sel):
            started_not_finished += 1
            continue

        outcome, is_settled = _selection_outcome(sel)
        if is_settled:
            settled_count += 1
            if outcome is True:
                winning_count += 1
            elif outcome is False:
                losing_count += 1
            continue

        if not _is_fixture_started(sel, now):
            pending_count += 1

    if started_not_finished > 0 and settings_obj.disable_cash_out_during_live_events:
        return CashOutQuote(
            eligible=False,
            reason="Cash Out unavailable. One or more events are currently in progress.",
            cashout_amount=ZERO,
            original_odds=ZERO,
            completed_odds=ZERO,
            progress_percent=ZERO,
            potential_win=potential_win,
            settled_count=settled_count,
            winning_count=winning_count,
            losing_count=losing_count,
            pending_count=pending_count,
        )

    if started_not_finished > 0:
        return CashOutQuote(
            eligible=False,
            reason="Cash Out unavailable. Waiting for official result.",
            cashout_amount=ZERO,
            original_odds=ZERO,
            completed_odds=ZERO,
            progress_percent=ZERO,
            potential_win=potential_win,
            settled_count=settled_count,
            winning_count=winning_count,
            losing_count=losing_count,
            pending_count=pending_count,
        )

    if settled_count == 0:
        if not settings_obj.enable_pre_match_cash_out:
            return CashOutQuote(
                eligible=False,
                reason="Cash Out is not available for this ticket.",
                cashout_amount=ZERO,
                original_odds=ZERO,
                completed_odds=ZERO,
                progress_percent=ZERO,
                potential_win=potential_win,
                settled_count=settled_count,
                winning_count=winning_count,
                losing_count=losing_count,
                pending_count=pending_count,
            )

        stake = _quantize_money(ticket.stake_amount)
        charge_type = str(settings_obj.charge_type)
        charge_value = ZERO
        if charge_type == CashOutSettings.CHARGE_TYPE.PERCENTAGE:
            percent = _clamp(_safe_decimal(settings_obj.percentage_charge, ZERO), ZERO, Decimal("100.00"))
            charge_value = (stake * (percent / Decimal("100.00"))).quantize(Decimal("0.01"))
        else:
            charge_value = _quantize_money(settings_obj.fixed_charge_amount)

        cashout_amount = (stake - charge_value).quantize(Decimal("0.01"))
        if cashout_amount < ZERO:
            cashout_amount = ZERO

        cashout_amount = min(cashout_amount, stake)
        cashout_amount = min(cashout_amount, max_cashout_setting)

        if cashout_amount <= ZERO:
            return CashOutQuote(
                eligible=False,
                reason="Cash Out is not available for this ticket.",
                cashout_amount=ZERO,
                original_odds=ZERO,
                completed_odds=ZERO,
                progress_percent=ZERO,
                potential_win=potential_win,
                settled_count=settled_count,
                winning_count=winning_count,
                losing_count=losing_count,
                pending_count=pending_count,
                charge_type=charge_type,
                charge_value=charge_value,
            )

        return CashOutQuote(
            eligible=True,
            reason="",
            cashout_amount=_quantize_money(cashout_amount),
            original_odds=ZERO,
            completed_odds=ZERO,
            progress_percent=ZERO,
            potential_win=potential_win,
            settled_count=settled_count,
            winning_count=winning_count,
            losing_count=losing_count,
            pending_count=pending_count,
            charge_type=charge_type,
            charge_value=charge_value,
            offer_percent_of_potential=ZERO,
        )

    alive = _ticket_is_alive_by_state(
        ticket=ticket,
        selections=selections,
        winning_count=winning_count,
        losing_count=losing_count,
        pending_count=pending_count,
    )
    if not alive:
        return CashOutQuote(
            eligible=False,
            reason="Cash Out is no longer available because this ticket no longer qualifies.",
            cashout_amount=ZERO,
            original_odds=ZERO,
            completed_odds=ZERO,
            progress_percent=ZERO,
            potential_win=potential_win,
            settled_count=settled_count,
            winning_count=winning_count,
            losing_count=losing_count,
            pending_count=pending_count,
        )

    if pending_count <= 0:
        return CashOutQuote(
            eligible=False,
            reason="Cash Out is not available because this ticket is already settled.",
            cashout_amount=ZERO,
            original_odds=ZERO,
            completed_odds=ZERO,
            progress_percent=ZERO,
            potential_win=potential_win,
            settled_count=settled_count,
            winning_count=winning_count,
            losing_count=losing_count,
            pending_count=pending_count,
        )

    max_cashout_allowed = min(ticket_cap, max_cashout_setting)
    if max_cashout_allowed <= ZERO or min_cashout_setting > max_cashout_allowed:
        return CashOutQuote(
            eligible=False,
            reason=f"Cash Out is not available because the minimum cash out amount (₦{min_cashout_setting:,.2f}) is above this ticket's allowed maximum (₦{max_cashout_allowed:,.2f}).",
            cashout_amount=ZERO,
            original_odds=ZERO,
            completed_odds=ZERO,
            progress_percent=ZERO,
            potential_win=potential_win,
            settled_count=settled_count,
            winning_count=winning_count,
            losing_count=losing_count,
            pending_count=pending_count,
        )

    won_odds = Decimal("1.000000")
    pending_odds = []
    for sel in selections:
        outcome, is_settled = _selection_outcome(sel)
        if is_settled and outcome is True:
            won_odds *= _safe_decimal(getattr(sel, "odd_selected", "1.00"), Decimal("1.00"))
        if not is_settled and not _is_fixture_started(sel, now):
            pending_odds.append(_safe_decimal(getattr(sel, "odd_selected", "1.00"), Decimal("1.00")))

    won_odds = _safe_decimal(won_odds, Decimal("1.00")).quantize(Decimal("0.000001"))

    remaining_odds = Decimal("1.000000")
    if ticket.bet_type == "system" and ticket.system_min_count:
        k = int(ticket.system_min_count or 0)
        remaining_needed = max(0, k - int(winning_count or 0))
        pending_sorted = sorted(pending_odds)
        if remaining_needed > 0:
            for odd in pending_sorted[:remaining_needed]:
                remaining_odds *= odd
    else:
        for odd in pending_odds:
            remaining_odds *= odd

    remaining_odds = _safe_decimal(remaining_odds, Decimal("1.00")).quantize(Decimal("0.000001"))

    risk_multiplier = _safe_decimal(getattr(settings_obj, "risk_multiplier", Decimal("0.0500")), Decimal("0.0500"))
    risk_discount = _risk_discount_from_remaining_odds(remaining_odds=remaining_odds, risk_multiplier=risk_multiplier)

    margin_percent = _clamp(_safe_decimal(getattr(settings_obj, "company_margin_percent", ZERO), ZERO), ZERO, Decimal("100.00"))
    margin_factor = (Decimal("1.00") - (margin_percent / Decimal("100.00")))

    stake = _quantize_money(ticket.stake_amount)
    secured_value = (stake * won_odds).quantize(Decimal("0.01"))
    cashout_amount = (secured_value * _safe_decimal(risk_discount, ZERO) * margin_factor).quantize(Decimal("0.01"))
    cashout_amount = min(cashout_amount, max_cashout_allowed)

    if cashout_amount < min_cashout_setting or cashout_amount <= ZERO:
        return CashOutQuote(
            eligible=False,
            reason=f"Cash Out is not available because the calculated amount (₦{cashout_amount:,.2f}) is below the minimum cash out amount (₦{min_cashout_setting:,.2f}).",
            cashout_amount=ZERO,
            original_odds=ZERO,
            completed_odds=ZERO,
            progress_percent=_quantize_ratio(risk_discount * Decimal("100.00")),
            potential_win=potential_win,
            settled_count=settled_count,
            winning_count=winning_count,
            losing_count=losing_count,
            pending_count=pending_count,
            won_odds=won_odds,
            remaining_odds=remaining_odds,
            risk_discount=risk_discount,
            company_margin_percent=margin_percent,
            risk_multiplier=risk_multiplier,
        )

    if cashout_amount > potential_win or cashout_amount > maximum_win or cashout_amount < ZERO:
        _log_invalid_quote(
            ticket=ticket,
            settings_obj=settings_obj,
            metadata={
                "cashout_amount": str(cashout_amount),
                "potential_win": str(potential_win),
                "maximum_win": str(maximum_win),
                "ticket_cap": str(ticket_cap),
                "won_odds": str(won_odds),
                "remaining_odds": str(remaining_odds),
                "risk_discount": str(risk_discount),
                "risk_multiplier": str(risk_multiplier),
                "company_margin_percent": str(margin_percent),
                "settled_count": settled_count,
                "winning_count": winning_count,
                "losing_count": losing_count,
                "pending_count": pending_count,
            },
        )
        return CashOutQuote(
            eligible=False,
            reason="Cash Out is not available for this ticket.",
            cashout_amount=ZERO,
            original_odds=ZERO,
            completed_odds=ZERO,
            progress_percent=ZERO,
            potential_win=potential_win,
            settled_count=settled_count,
            winning_count=winning_count,
            losing_count=losing_count,
            pending_count=pending_count,
            won_odds=won_odds,
            remaining_odds=remaining_odds,
            risk_discount=risk_discount,
            company_margin_percent=margin_percent,
            risk_multiplier=risk_multiplier,
        )

    return CashOutQuote(
        eligible=True,
        reason="",
        cashout_amount=_quantize_money(cashout_amount),
        original_odds=ZERO,
        completed_odds=ZERO,
        progress_percent=_quantize_ratio(risk_discount * Decimal("100.00")),
        potential_win=potential_win,
        settled_count=settled_count,
        winning_count=winning_count,
        losing_count=losing_count,
        pending_count=pending_count,
        won_odds=won_odds,
        remaining_odds=remaining_odds,
        risk_discount=risk_discount,
        company_margin_percent=margin_percent,
        risk_multiplier=risk_multiplier,
    )


def _build_cashout_reference(ticket):
    seed = f"{ticket.ticket_id}-{timezone.now().isoformat()}-{uuid.uuid4().hex}"
    suffix = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:10].upper()
    return f"CASHOUT-{ticket.ticket_id}-{suffix}"


def execute_cashout(*, ticket_id, actor, ip_address="", user_agent=""):
    settings_obj = CashOutSettings.load()
    now = timezone.now()

    with transaction.atomic():
        ticket = (
            BetTicket.objects.select_for_update()
            .select_related("user")
            .get(pk=ticket_id)
        )

        existing_cashout = BetTicketCashOut.objects.filter(ticket=ticket).first()
        if ticket.status == "cashed_out" or existing_cashout:
            raise CashOutError("This ticket has already been cashed out.")

        selections = list(ticket.selections.select_related("fixture").all())
        reason = _cashout_disabled_reason(ticket=ticket, selections=selections, settings_obj=settings_obj)
        if reason:
            raise CashOutError(reason)

        quote = build_cashout_quote(ticket=ticket, settings_obj=settings_obj, now=now)
        if not quote.eligible:
            raise CashOutError(quote.reason or "Cash Out is not available.")

        reference = _build_cashout_reference(ticket)
        wallet = Wallet.objects.select_for_update().get(user=ticket.user)

        tx = Transaction.objects.create(
            user=ticket.user,
            initiating_user=actor if getattr(actor, "is_authenticated", False) else None,
            target_user=ticket.user,
            transaction_type="bet_cashout",
            amount=quote.cashout_amount,
            is_successful=True,
            status="completed",
            description=f"Cash out for ticket {ticket.ticket_id}",
            related_bet_ticket=ticket,
            timestamp=now,
            external_reference=reference,
        )

        wallet.apply_delta(
            amount=quote.cashout_amount,
            actor=actor if getattr(actor, "is_authenticated", False) else None,
            transaction_obj=tx,
            reference=ticket.ticket_id,
            reason=tx.description or "",
            metadata={
                "ticket_id": ticket.ticket_id,
                "source": "cashout",
                "cashout_reference": reference,
            },
        )

        cashier = ticket.user if getattr(ticket.user, "user_type", "") == "cashier" else None
        agent = getattr(ticket.user, "agent", None) if cashier else getattr(ticket.user, "agent", None)
        if cashier and getattr(cashier, "agent_id", None):
            agent = cashier.agent

        settings_snapshot = _cashout_settings_snapshot(settings_obj)
        cashout = BetTicketCashOut.objects.create(
            reference=reference,
            ticket=ticket,
            user=ticket.user,
            cashier=cashier,
            agent=agent,
            ticket_type=ticket.bet_type,
            ticket_status=ticket.status,
            stake_amount=_quantize_money(ticket.stake_amount),
            potential_win=quote.potential_win,
            original_odds=ZERO,
            completed_odds=ZERO,
            progress_percent=_safe_decimal(quote.progress_percent, ZERO).quantize(Decimal("0.0001")),
            company_margin_percent=_safe_decimal(quote.company_margin_percent, ZERO).quantize(Decimal("0.01")),
            charge_type=str(quote.charge_type or ""),
            charge_value=_quantize_money(quote.charge_value),
            settled_count=int(quote.settled_count or 0),
            winning_count=int(quote.winning_count or 0),
            losing_count=int(quote.losing_count or 0),
            pending_count=int(quote.pending_count or 0),
            offer_percent_of_potential=_safe_decimal(quote.offer_percent_of_potential, ZERO).quantize(Decimal("0.01")),
            won_odds=_safe_decimal(quote.won_odds, Decimal("0.00")).quantize(Decimal("0.000001")),
            remaining_odds=_safe_decimal(quote.remaining_odds, Decimal("0.00")).quantize(Decimal("0.000001")),
            risk_discount=_safe_decimal(quote.risk_discount, Decimal("0.00")).quantize(Decimal("0.000001")),
            risk_multiplier_used=_safe_decimal(quote.risk_multiplier, Decimal("0.0000")).quantize(Decimal("0.0001")),
            cashout_amount=quote.cashout_amount,
            calculation_strategy="odds_based",
            settings_snapshot=settings_snapshot,
            processed_at=now,
            processed_by=actor if isinstance(actor, User) else None,
            status=BetTicketCashOut.STATUS.COMPLETED,
        )

        ticket.status = "cashed_out"
        ticket.cashout_amount = quote.cashout_amount
        ticket.cashout_reference = reference
        ticket.cashout_company_margin_percent = _safe_decimal(quote.company_margin_percent, ZERO).quantize(Decimal("0.01"))
        ticket.cashout_original_odds = ZERO
        ticket.cashout_completed_odds = ZERO
        ticket.cashout_progress_percent = _safe_decimal(quote.progress_percent, ZERO).quantize(Decimal("0.0001"))
        ticket.cashout_strategy = "odds_based"
        ticket.cashout_processed_at = now
        ticket.cashout_processed_by = actor if isinstance(actor, User) else None
        ticket.cashout_settings_snapshot = settings_snapshot
        ticket.save(
            update_fields=[
                "status",
                "cashout_amount",
                "cashout_reference",
                "cashout_company_margin_percent",
                "cashout_original_odds",
                "cashout_completed_odds",
                "cashout_progress_percent",
                "cashout_strategy",
                "cashout_processed_at",
                "cashout_processed_by",
                "cashout_settings_snapshot",
                "last_updated",
            ]
        )

        CashOutAuditLog.objects.create(
            ticket=ticket,
            cashout=cashout,
            actor=actor if isinstance(actor, User) else None,
            action="CASHOUT_COMPLETED",
            message="Cash out completed",
            ip_address=ip_address or None,
            user_agent=user_agent or "",
            metadata={
                "reference": reference,
                "cashout_amount": str(quote.cashout_amount),
                "won_odds": str(_safe_decimal(quote.won_odds, Decimal("0.00"))),
                "remaining_odds": str(_safe_decimal(quote.remaining_odds, Decimal("0.00"))),
                "risk_discount": str(_safe_decimal(quote.risk_discount, Decimal("0.00"))),
                "risk_multiplier": str(_safe_decimal(quote.risk_multiplier, Decimal("0.0000"))),
                "company_margin_percent": str(_safe_decimal(quote.company_margin_percent, ZERO)),
                "settled_count": int(quote.settled_count or 0),
                "winning_count": int(quote.winning_count or 0),
                "losing_count": int(quote.losing_count or 0),
                "pending_count": int(quote.pending_count or 0),
                "charge_type": str(quote.charge_type or ""),
                "charge_value": str(_quantize_money(quote.charge_value)),
                "offer_percent_of_potential": str(_safe_decimal(quote.offer_percent_of_potential, ZERO)),
                "strategy": "odds_based",
                "formula_version": cashout.formula_version,
            },
        )

        return cashout
