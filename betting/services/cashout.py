import hashlib
import uuid
from dataclasses import dataclass
from decimal import Decimal, localcontext
from math import comb

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
    total_stake_amount: Decimal = ZERO
    cash_stake_amount: Decimal = ZERO
    bonus_stake_amount: Decimal = ZERO
    settled_count: int = 0
    winning_count: int = 0
    losing_count: int = 0
    pending_count: int = 0
    required_wins: int = 0
    maximum_possible_wins: int = 0
    system_progress_factor: Decimal = Decimal("1.0000")
    system_paths_factor: Decimal = Decimal("1.0000")
    system_winning_paths: int = 0
    system_total_paths: int = 0
    charge_type: str = ""
    charge_value: Decimal = ZERO
    offer_percent_of_potential: Decimal = ZERO
    won_odds: Decimal = Decimal("0.000000")
    remaining_odds: Decimal = Decimal("0.000000")
    risk_discount: Decimal = Decimal("0.000000")
    company_margin_percent: Decimal = Decimal("0.00")
    risk_multiplier: Decimal = Decimal("0.0000")
    cash_out_scaling_factor: Decimal = Decimal("1.0000")
    max_cash_out_cap_percent: Decimal = Decimal("0.00")
    max_cash_out_cap_amount: Decimal = ZERO
    cashout_before_scaling: Decimal = ZERO
    cashout_after_scaling: Decimal = ZERO
    risk_discount_exponent: Decimal = Decimal("1.0000")


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


def _stake_split(ticket):
    total_stake = _quantize_money(getattr(ticket, "stake_amount", ZERO))
    cash_raw = getattr(ticket, "cash_stake_amount", None)
    bonus_raw = getattr(ticket, "bonus_stake_amount", None)

    if cash_raw is None and bonus_raw is None:
        return total_stake, total_stake, ZERO

    if cash_raw is None and bonus_raw is not None:
        bonus_stake = _quantize_money(bonus_raw)
        cash_stake = (total_stake - bonus_stake).quantize(Decimal("0.01"))
        if cash_stake < ZERO:
            cash_stake = ZERO
        return total_stake, cash_stake, bonus_stake

    cash_stake = _quantize_money(cash_raw)
    if bonus_raw is None:
        bonus_stake = (total_stake - cash_stake).quantize(Decimal("0.01"))
        if bonus_stake < ZERO:
            bonus_stake = ZERO
        return total_stake, min(cash_stake, total_stake), bonus_stake

    bonus_stake = _quantize_money(bonus_raw)
    cash_stake = min(cash_stake, total_stake)
    bonus_stake = min(bonus_stake, (total_stake - cash_stake).quantize(Decimal("0.01")))
    if bonus_stake < ZERO:
        bonus_stake = ZERO
    return total_stake, cash_stake, bonus_stake


def _system_progress_factor(*, winning_count, required_wins):
    try:
        required_wins = int(required_wins or 0)
        winning_count = int(winning_count or 0)
    except Exception:
        required_wins = 0
        winning_count = 0

    if required_wins <= 0:
        return Decimal("1.0000")

    progress_ratio = Decimal(str(winning_count)) / Decimal(str(required_wins))
    return _clamp(_quantize_ratio(progress_ratio), Decimal("0.1000"), Decimal("1.0000"))


def _system_paths_factor(*, pending_count, remaining_needed):
    try:
        pending_count = int(pending_count or 0)
        remaining_needed = int(remaining_needed or 0)
    except Exception:
        pending_count = 0
        remaining_needed = 0

    if remaining_needed <= 0:
        return Decimal("1.0000"), 1, 1
    if pending_count <= 0:
        return Decimal("0.0000"), 0, 0
    if remaining_needed > pending_count:
        return Decimal("0.0000"), 0, 0

    total_paths = 1 << pending_count
    losing_paths = 0
    for i in range(0, remaining_needed):
        losing_paths += comb(pending_count, i)
    winning_paths = max(0, total_paths - losing_paths)
    factor = _clamp(_quantize_ratio(Decimal(str(winning_paths)) / Decimal(str(total_paths))), Decimal("0.0000"), Decimal("1.0000"))
    return factor, winning_paths, total_paths


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
    selection_result = getattr(selection, "is_winning_selection", None)
    if selection_result is True:
        return True, True
    if selection_result is False:
        return False, True

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


def _risk_discount_from_remaining_odds(*, remaining_odds, risk_multiplier, exponent=None):
    remaining_odds = _safe_decimal(remaining_odds, Decimal("1.00"))
    if remaining_odds <= Decimal("1.00"):
        return Decimal("1.000000")

    rm = _clamp(_safe_decimal(risk_multiplier, Decimal("0.0500")), Decimal("0.0000"), Decimal("1000.0000"))
    exp_val = _clamp(_safe_decimal(exponent, Decimal("1.0000")), Decimal("1.0000"), Decimal("10.0000"))
    delta = remaining_odds - Decimal("1.00")
    if delta <= Decimal("0.00"):
        powered = Decimal("0.000000")
    else:
        with localcontext() as ctx:
            ctx.prec = 40
            powered = (delta.ln() * exp_val).exp()

    denom = Decimal("1.00") + (rm * powered)
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
        "charge_type": str(settings_obj.charge_type),
        "fixed_charge_amount": str(settings_obj.fixed_charge_amount),
        "percentage_charge": str(settings_obj.percentage_charge),
        "company_margin_percent": str(getattr(settings_obj, "company_margin_percent", "0.00")),
        "risk_multiplier": str(getattr(settings_obj, "risk_multiplier", "0.0000")),
        "cash_out_scaling_factor": str(getattr(settings_obj, "cash_out_scaling_factor", "1.0000")),
        "max_pre_match_cash_out_percent": str(getattr(settings_obj, "max_pre_match_cash_out_percent", "0.00")),
        "max_in_progress_cash_out_percent": str(getattr(settings_obj, "max_in_progress_cash_out_percent", "0.00")),
        "risk_discount_exponent": str(getattr(settings_obj, "risk_discount_exponent", "1.0000")),
        "minimum_stake_eligible": str(settings_obj.minimum_stake_eligible),
        "maximum_stake_eligible": str(settings_obj.maximum_stake_eligible),
        "minimum_cash_out_amount": str(settings_obj.minimum_cash_out_amount),
        "maximum_cash_out_amount": str(settings_obj.maximum_cash_out_amount),
        "manually_closed": bool(settings_obj.manually_closed),
    }


def _log_quote(*, ticket, quote, settings_obj=None, actor=None, ip_address=None, user_agent="", source=""):
    try:
        settings_obj = settings_obj or CashOutSettings.load()
        required_wins = 0
        try:
            if getattr(ticket, "bet_type", "") == "system":
                required_wins = int(getattr(ticket, "system_min_count", None) or 0)
        except Exception:
            required_wins = 0

        total_stake_amount, cash_stake_amount, bonus_stake_amount = _stake_split(ticket)

        CashOutAuditLog.objects.create(
            ticket=ticket,
            cashout=None,
            actor=actor if isinstance(actor, User) else None,
            action="CASHOUT_QUOTE",
            message="Cash out quote generated",
            ip_address=ip_address or None,
            user_agent=user_agent or "",
            metadata={
                "source": (source or "").strip(),
                "eligible": bool(getattr(quote, "eligible", False)),
                "reason": getattr(quote, "reason", ""),
                "ticket_bet_type": str(getattr(ticket, "bet_type", "") or ""),
                "stake_total": str(getattr(ticket, "stake_amount", "0.00")),
                "stake_cash": str(cash_stake_amount),
                "stake_bonus": str(bonus_stake_amount),
                "cashout_basis": str(cash_stake_amount),
                "potential_win": str(getattr(ticket, "potential_winning", "0.00")),
                "max_winning": str(getattr(ticket, "max_winning", "0.00")),
                "cashout_amount": str(getattr(quote, "cashout_amount", ZERO)),
                "original_odds": str(getattr(quote, "original_odds", ZERO)),
                "completed_odds": str(getattr(quote, "completed_odds", ZERO)),
                "remaining_odds": str(getattr(quote, "remaining_odds", ZERO)),
                "progress_percent": str(getattr(quote, "progress_percent", ZERO)),
                "risk_discount": str(getattr(quote, "risk_discount", ZERO)),
                "risk_multiplier": str(getattr(quote, "risk_multiplier", ZERO)),
                "risk_discount_exponent": str(getattr(quote, "risk_discount_exponent", ZERO)),
                "company_margin_percent": str(getattr(quote, "company_margin_percent", ZERO)),
                "cash_out_scaling_factor": str(getattr(quote, "cash_out_scaling_factor", ZERO)),
                "max_cash_out_cap_percent": str(getattr(quote, "max_cash_out_cap_percent", ZERO)),
                "max_cash_out_cap_amount": str(getattr(quote, "max_cash_out_cap_amount", ZERO)),
                "cashout_before_scaling": str(getattr(quote, "cashout_before_scaling", ZERO)),
                "cashout_after_scaling": str(getattr(quote, "cashout_after_scaling", ZERO)),
                "settled_count": int(getattr(quote, "settled_count", 0) or 0),
                "winning_count": int(getattr(quote, "winning_count", 0) or 0),
                "losing_count": int(getattr(quote, "losing_count", 0) or 0),
                "pending_count": int(getattr(quote, "pending_count", 0) or 0),
                "required_wins": int(getattr(quote, "required_wins", 0) or required_wins or 0),
                "maximum_possible_wins": int(getattr(quote, "maximum_possible_wins", 0) or 0),
                "system_progress_factor": str(getattr(quote, "system_progress_factor", Decimal("1.0000"))),
                "system_progress_ratio": str(
                    _quantize_ratio(
                        (Decimal(str(int(getattr(quote, "winning_count", 0) or 0))) / Decimal(str(int(getattr(quote, "required_wins", 0) or required_wins or 0))))
                        if int(getattr(quote, "required_wins", 0) or required_wins or 0) > 0
                        else Decimal("0.0000")
                    )
                ),
                "system_paths_factor": str(getattr(quote, "system_paths_factor", Decimal("1.0000"))),
                "system_winning_paths": int(getattr(quote, "system_winning_paths", 0) or 0),
                "system_total_paths": int(getattr(quote, "system_total_paths", 0) or 0),
                "charge_type": str(getattr(quote, "charge_type", "") or ""),
                "charge_value": str(getattr(quote, "charge_value", ZERO)),
                "settings": _cashout_settings_snapshot(settings_obj=settings_obj),
            },
        )
    except Exception:
        return


def _cashout_disabled_reason(*, ticket, selections, settings_obj):
    if not settings_obj.enable_cash_out:
        return "Cash Out is currently disabled."
    if settings_obj.manually_closed:
        return "Cash Out has been closed by the administrator."
    if not getattr(settings_obj, "enable_full_cash_out", True):
        if getattr(settings_obj, "enable_partial_cash_out", False):
            return "Partial Cash Out is enabled but is not available yet."
        return "Cash Out is currently disabled."
    if ticket.is_voided:
        return "Cash Out is not available for voided tickets."
    if ticket.status == "cashed_out" or getattr(ticket, "cashout_id", None) or hasattr(ticket, "cashout"):
        return "This ticket has already been cashed out."
    if _ticket_is_settled(ticket=ticket, selections=selections):
        return "Cash Out is not available because this ticket is already settled."

    total_stake, cash_stake, bonus_stake = _stake_split(ticket)
    if cash_stake <= ZERO:
        return "Cash Out is not available because this ticket was funded with bonus/promotional credits."

    stake = _safe_decimal(cash_stake, ZERO)
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


def _compute_original_odds(*, ticket, selections):
    try:
        display_total = _safe_decimal(ticket.get_display_total_odd(), Decimal("0.00"))
    except Exception:
        display_total = Decimal("0.00")

    if display_total > Decimal("0.00"):
        try:
            stored_total = _safe_decimal(getattr(ticket, "total_odd", None) or Decimal("0.00"), Decimal("0.00"))
            if stored_total <= Decimal("0.00"):
                ticket.total_odd = display_total.quantize(Decimal("0.01"))
                ticket.save(update_fields=["total_odd", "last_updated"])
        except Exception:
            pass
        return display_total.quantize(Decimal("0.000001"))

    product = Decimal("1.000000")
    for sel in selections:
        product *= _safe_decimal(getattr(sel, "odd_selected", "1.00"), Decimal("1.00"))
    product = _safe_decimal(product, Decimal("1.00")).quantize(Decimal("0.000001"))
    try:
        stored_total = _safe_decimal(getattr(ticket, "total_odd", None) or Decimal("0.00"), Decimal("0.00"))
        if stored_total <= Decimal("0.00"):
            ticket.total_odd = product.quantize(Decimal("0.01"))
            ticket.save(update_fields=["total_odd", "last_updated"])
    except Exception:
        pass
    return product


def _progress_percent_from_odds(*, original_odds, completed_odds):
    original_odds = _safe_decimal(original_odds, Decimal("0.00"))
    completed_odds = _safe_decimal(completed_odds, Decimal("0.00"))
    if original_odds <= Decimal("0.00") or completed_odds <= Decimal("0.00"):
        return ZERO
    return _quantize_ratio((completed_odds / original_odds) * Decimal("100.00"))



def build_cashout_quote(*, ticket, settings_obj=None, now=None, actor=None, ip_address=None, user_agent="", source=""):
    settings_obj = settings_obj or CashOutSettings.load()
    now = now or timezone.now()
    selections = list(ticket.selections.select_related("fixture").all())
    original_odds = _compute_original_odds(ticket=ticket, selections=selections)
    total_stake_amount, cash_stake_amount, bonus_stake_amount = _stake_split(ticket)
    required_wins = 0
    try:
        if getattr(ticket, "bet_type", "") == "system":
            required_wins = int(getattr(ticket, "system_min_count", None) or 0)
    except Exception:
        required_wins = 0

    reason = _cashout_disabled_reason(ticket=ticket, selections=selections, settings_obj=settings_obj)
    if reason:
        quote = CashOutQuote(
            eligible=False,
            reason=reason,
            cashout_amount=ZERO,
            original_odds=original_odds,
            completed_odds=Decimal("0.000000"),
            progress_percent=ZERO,
            potential_win=_quantize_money(ticket.potential_winning),
            total_stake_amount=total_stake_amount,
            cash_stake_amount=cash_stake_amount,
            bonus_stake_amount=bonus_stake_amount,
            required_wins=required_wins,
        )
        _log_quote(ticket=ticket, quote=quote, settings_obj=settings_obj, actor=actor, ip_address=ip_address, user_agent=user_agent, source=source)
        return quote

    potential_win = _quantize_money(ticket.potential_winning)
    maximum_win = _quantize_money(getattr(ticket, "max_winning", None) or potential_win)
    ticket_cap = min(potential_win, maximum_win)

    min_cashout_setting = _quantize_money(settings_obj.minimum_cash_out_amount)
    max_cashout_setting = _quantize_money(settings_obj.maximum_cash_out_amount)
    cash_out_scaling_factor = _clamp(
        _safe_decimal(getattr(settings_obj, "cash_out_scaling_factor", Decimal("1.0000")), Decimal("1.0000")),
        Decimal("0.1000"),
        Decimal("1.0000"),
    ).quantize(Decimal("0.0001"))
    max_pre_match_cash_out_percent = _clamp(
        _safe_decimal(getattr(settings_obj, "max_pre_match_cash_out_percent", Decimal("100.00")), Decimal("100.00")),
        ZERO,
        Decimal("100.00"),
    ).quantize(Decimal("0.01"))
    max_in_progress_cash_out_percent = _clamp(
        _safe_decimal(getattr(settings_obj, "max_in_progress_cash_out_percent", Decimal("100.00")), Decimal("100.00")),
        ZERO,
        Decimal("100.00"),
    ).quantize(Decimal("0.01"))
    risk_discount_exponent = _clamp(
        _safe_decimal(getattr(settings_obj, "risk_discount_exponent", Decimal("1.0000")), Decimal("1.0000")),
        Decimal("1.0000"),
        Decimal("10.0000"),
    ).quantize(Decimal("0.0001"))

    started_not_finished = 0
    any_started = False
    settled_count = 0
    winning_count = 0
    losing_count = 0
    pending_count = 0
    for sel in selections:
        started = _is_fixture_started(sel, now)
        finished = _is_fixture_finished(sel)
        if started:
            any_started = True
        if started and not finished:
            started_not_finished += 1
            continue

        outcome, is_settled = _selection_outcome(sel)
        if finished and not is_settled:
            started_not_finished += 1
            continue
        if is_settled:
            settled_count += 1
            if outcome is True:
                winning_count += 1
            elif outcome is False:
                losing_count += 1
            continue

        if not started:
            pending_count += 1

    if started_not_finished > 0:
        quote = CashOutQuote(
            eligible=False,
            reason="Cash Out is temporarily unavailable while event results are pending.",
            cashout_amount=ZERO,
            original_odds=original_odds,
            completed_odds=Decimal("0.000000"),
            progress_percent=ZERO,
            potential_win=potential_win,
            total_stake_amount=total_stake_amount,
            cash_stake_amount=cash_stake_amount,
            bonus_stake_amount=bonus_stake_amount,
            settled_count=settled_count,
            winning_count=winning_count,
            losing_count=losing_count,
            pending_count=pending_count,
            required_wins=required_wins,
            maximum_possible_wins=int((winning_count or 0) + (pending_count or 0)),
        )
        _log_quote(ticket=ticket, quote=quote, settings_obj=settings_obj, actor=actor, ip_address=ip_address, user_agent=user_agent, source=source)
        return quote

    if settled_count == 0 and not any_started:
        if not settings_obj.enable_pre_match_cash_out:
            quote = CashOutQuote(
                eligible=False,
                reason="Cash Out is not available for this ticket.",
                cashout_amount=ZERO,
                original_odds=original_odds,
                completed_odds=Decimal("0.000000"),
                progress_percent=ZERO,
                potential_win=potential_win,
                settled_count=settled_count,
                winning_count=winning_count,
                losing_count=losing_count,
                pending_count=pending_count,
                cash_out_scaling_factor=cash_out_scaling_factor,
                max_cash_out_cap_percent=max_pre_match_cash_out_percent,
                max_cash_out_cap_amount=ZERO,
                cashout_before_scaling=ZERO,
                cashout_after_scaling=ZERO,
                risk_discount_exponent=risk_discount_exponent,
            )
            _log_quote(ticket=ticket, quote=quote, settings_obj=settings_obj, actor=actor, ip_address=ip_address, user_agent=user_agent, source=source)
            return quote

        stake = _quantize_money(cash_stake_amount)
        charge_type = str(settings_obj.charge_type)
        charge_value = ZERO
        if charge_type == CashOutSettings.CHARGE_TYPE.PERCENTAGE:
            percent = _clamp(_safe_decimal(settings_obj.percentage_charge, ZERO), ZERO, Decimal("100.00"))
            charge_value = (stake * (percent / Decimal("100.00"))).quantize(Decimal("0.01"))
        else:
            charge_value = _quantize_money(settings_obj.fixed_charge_amount)

        if charge_value <= ZERO:
            min_charge = max(Decimal("10.00"), (stake * Decimal("0.01")).quantize(Decimal("0.01")))
            if min_charge >= stake:
                min_charge = (stake - Decimal("0.01")).quantize(Decimal("0.01"))
            charge_value = _clamp(min_charge, ZERO, stake)

        cashout_amount = (stake - charge_value).quantize(Decimal("0.01"))
        if cashout_amount < ZERO:
            cashout_amount = ZERO

        cashout_before_scaling = _quantize_money(cashout_amount)
        cashout_after_scaling = (cashout_before_scaling * cash_out_scaling_factor).quantize(Decimal("0.01"))
        cap_amount = (stake * (max_pre_match_cash_out_percent / Decimal("100.00"))).quantize(Decimal("0.01"))
        cashout_amount = min(cashout_after_scaling, stake, max_cashout_setting, cap_amount)

        if cashout_amount <= ZERO:
            quote = CashOutQuote(
                eligible=False,
                reason="Cash Out is not available for this ticket.",
                cashout_amount=ZERO,
                original_odds=original_odds,
                completed_odds=Decimal("0.000000"),
                progress_percent=ZERO,
                potential_win=potential_win,
                settled_count=settled_count,
                winning_count=winning_count,
                losing_count=losing_count,
                pending_count=pending_count,
                charge_type=charge_type,
                charge_value=charge_value,
                required_wins=required_wins,
                cash_out_scaling_factor=cash_out_scaling_factor,
                max_cash_out_cap_percent=max_pre_match_cash_out_percent,
                max_cash_out_cap_amount=_quantize_money(cap_amount),
                cashout_before_scaling=_quantize_money(cashout_before_scaling),
                cashout_after_scaling=_quantize_money(cashout_after_scaling),
                risk_discount_exponent=risk_discount_exponent,
            )
            _log_quote(ticket=ticket, quote=quote, settings_obj=settings_obj, actor=actor, ip_address=ip_address, user_agent=user_agent, source=source)
            return quote

        quote = CashOutQuote(
            eligible=True,
            reason="",
            cashout_amount=_quantize_money(cashout_amount),
            original_odds=original_odds,
            completed_odds=Decimal("0.000000"),
            progress_percent=ZERO,
            potential_win=potential_win,
            total_stake_amount=total_stake_amount,
            cash_stake_amount=cash_stake_amount,
            bonus_stake_amount=bonus_stake_amount,
            settled_count=settled_count,
            winning_count=winning_count,
            losing_count=losing_count,
            pending_count=pending_count,
            charge_type=charge_type,
            charge_value=charge_value,
            offer_percent_of_potential=ZERO,
            required_wins=required_wins,
            cash_out_scaling_factor=cash_out_scaling_factor,
            max_cash_out_cap_percent=max_pre_match_cash_out_percent,
            max_cash_out_cap_amount=_quantize_money(cap_amount),
            cashout_before_scaling=_quantize_money(cashout_before_scaling),
            cashout_after_scaling=_quantize_money(cashout_after_scaling),
            risk_discount_exponent=risk_discount_exponent,
        )
        _log_quote(ticket=ticket, quote=quote, settings_obj=settings_obj, actor=actor, ip_address=ip_address, user_agent=user_agent, source=source)
        return quote

    maximum_possible_wins = int((winning_count or 0) + (pending_count or 0))
    if ticket.bet_type == "system":
        if required_wins <= 0:
            quote = CashOutQuote(
                eligible=False,
                reason="Cash Out is not available because this ticket configuration is invalid.",
                cashout_amount=ZERO,
                original_odds=original_odds,
                completed_odds=Decimal("0.000000"),
                progress_percent=ZERO,
                potential_win=potential_win,
                settled_count=settled_count,
                winning_count=winning_count,
                losing_count=losing_count,
                pending_count=pending_count,
                required_wins=required_wins,
                maximum_possible_wins=maximum_possible_wins,
            )
            _log_quote(ticket=ticket, quote=quote, settings_obj=settings_obj, actor=actor, ip_address=ip_address, user_agent=user_agent, source=source)
            return quote

        if maximum_possible_wins < required_wins:
            quote = CashOutQuote(
                eligible=False,
                reason="Ticket is mathematically eliminated.",
                cashout_amount=ZERO,
                original_odds=original_odds,
                completed_odds=Decimal("0.000000"),
                progress_percent=ZERO,
                potential_win=potential_win,
                settled_count=settled_count,
                winning_count=winning_count,
                losing_count=losing_count,
                pending_count=pending_count,
                required_wins=required_wins,
                maximum_possible_wins=maximum_possible_wins,
            )
            _log_quote(ticket=ticket, quote=quote, settings_obj=settings_obj, actor=actor, ip_address=ip_address, user_agent=user_agent, source=source)
            return quote
    else:
        if losing_count > 0:
            quote = CashOutQuote(
                eligible=False,
                reason="Cash Out is not available because this ticket has a losing selection.",
                cashout_amount=ZERO,
                original_odds=original_odds,
                completed_odds=Decimal("0.000000"),
                progress_percent=ZERO,
                potential_win=potential_win,
                settled_count=settled_count,
                winning_count=winning_count,
                losing_count=losing_count,
                pending_count=pending_count,
                required_wins=required_wins,
                maximum_possible_wins=maximum_possible_wins,
            )
            _log_quote(ticket=ticket, quote=quote, settings_obj=settings_obj, actor=actor, ip_address=ip_address, user_agent=user_agent, source=source)
            return quote

    if pending_count <= 0:
        quote = CashOutQuote(
            eligible=False,
            reason="Cash Out is not available because this ticket is already settled.",
            cashout_amount=ZERO,
            original_odds=original_odds,
            completed_odds=Decimal("0.000000"),
            progress_percent=ZERO,
            potential_win=potential_win,
            settled_count=settled_count,
            winning_count=winning_count,
            losing_count=losing_count,
            pending_count=pending_count,
            required_wins=required_wins,
            maximum_possible_wins=maximum_possible_wins,
        )
        _log_quote(ticket=ticket, quote=quote, settings_obj=settings_obj, actor=actor, ip_address=ip_address, user_agent=user_agent, source=source)
        return quote
        return quote

    max_cashout_allowed = min(ticket_cap, max_cashout_setting)
    if max_cashout_allowed <= ZERO or min_cashout_setting > max_cashout_allowed:
        quote = CashOutQuote(
            eligible=False,
            reason=f"Cash Out is not available because the minimum cash out amount (₦{min_cashout_setting:,.2f}) is above this ticket's allowed maximum (₦{max_cashout_allowed:,.2f}).",
            cashout_amount=ZERO,
            original_odds=original_odds,
            completed_odds=Decimal("0.000000"),
            progress_percent=ZERO,
            potential_win=potential_win,
            settled_count=settled_count,
            winning_count=winning_count,
            losing_count=losing_count,
            pending_count=pending_count,
            required_wins=required_wins,
            maximum_possible_wins=maximum_possible_wins,
        )
        _log_quote(ticket=ticket, quote=quote, settings_obj=settings_obj, actor=actor, ip_address=ip_address, user_agent=user_agent, source=source)
        return quote

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
    if ticket.bet_type == "system" and required_wins:
        remaining_needed = max(0, int(required_wins or 0) - int(winning_count or 0))
        if remaining_needed <= 0:
            remaining_odds = Decimal("1.000000")
        elif not pending_odds:
            remaining_odds = Decimal("1.000000")
        else:
            pending_product = Decimal("1.000000")
            for odd in pending_odds:
                pending_product *= odd

            exponent = (Decimal(str(remaining_needed)) / Decimal(str(len(pending_odds)))).quantize(Decimal("0.000001"))
            with localcontext() as ctx:
                ctx.prec = 40
                remaining_odds = (pending_product.ln() * exponent).exp()
    else:
        for odd in pending_odds:
            remaining_odds *= odd

    remaining_odds = _safe_decimal(remaining_odds, Decimal("1.00")).quantize(Decimal("0.000001"))

    risk_multiplier = _safe_decimal(getattr(settings_obj, "risk_multiplier", Decimal("0.0500")), Decimal("0.0500"))
    risk_discount = _risk_discount_from_remaining_odds(
        remaining_odds=remaining_odds,
        risk_multiplier=risk_multiplier,
        exponent=risk_discount_exponent,
    )

    margin_percent = _clamp(_safe_decimal(getattr(settings_obj, "company_margin_percent", ZERO), ZERO), ZERO, Decimal("100.00"))
    margin_factor = (Decimal("1.00") - (margin_percent / Decimal("100.00")))

    system_progress_factor = Decimal("1.0000")
    if ticket.bet_type == "system" and required_wins:
        system_progress_factor = _system_progress_factor(winning_count=winning_count, required_wins=required_wins)

    system_paths_factor = Decimal("1.0000")
    system_winning_paths = 0
    system_total_paths = 0
    if ticket.bet_type == "system" and required_wins:
        remaining_needed = max(0, int(required_wins or 0) - int(winning_count or 0))
        system_paths_factor, system_winning_paths, system_total_paths = _system_paths_factor(
            pending_count=len(pending_odds),
            remaining_needed=remaining_needed,
        )

    stake = _quantize_money(cash_stake_amount)
    secured_value = (stake * won_odds).quantize(Decimal("0.01"))
    cashout_before_scaling = (
        secured_value
        * _safe_decimal(risk_discount, ZERO)
        * _safe_decimal(system_progress_factor, Decimal("1.0000"))
        * _safe_decimal(system_paths_factor, Decimal("1.0000"))
        * margin_factor
    ).quantize(Decimal("0.01"))
    cashout_after_scaling = (cashout_before_scaling * cash_out_scaling_factor).quantize(Decimal("0.01"))
    cap_amount = (stake * (max_in_progress_cash_out_percent / Decimal("100.00"))).quantize(Decimal("0.01"))
    max_cashout_allowed = min(max_cashout_allowed, cap_amount)
    cashout_amount = min(cashout_after_scaling, max_cashout_allowed)
    completed_odds = _safe_decimal(won_odds, Decimal("1.00")).quantize(Decimal("0.000001"))
    progress_percent = _progress_percent_from_odds(original_odds=original_odds, completed_odds=completed_odds)

    if cashout_amount < min_cashout_setting or cashout_amount <= ZERO:
        quote = CashOutQuote(
            eligible=False,
            reason=f"Cash Out is not available because the calculated amount (₦{cashout_amount:,.2f}) is below the minimum cash out amount (₦{min_cashout_setting:,.2f}).",
            cashout_amount=ZERO,
            original_odds=original_odds,
            completed_odds=completed_odds,
            progress_percent=progress_percent,
            potential_win=potential_win,
            settled_count=settled_count,
            winning_count=winning_count,
            losing_count=losing_count,
            pending_count=pending_count,
            required_wins=required_wins,
            maximum_possible_wins=maximum_possible_wins,
            system_progress_factor=system_progress_factor,
            system_paths_factor=system_paths_factor,
            system_winning_paths=system_winning_paths,
            system_total_paths=system_total_paths,
            won_odds=won_odds,
            remaining_odds=remaining_odds,
            risk_discount=risk_discount,
            company_margin_percent=margin_percent,
            risk_multiplier=risk_multiplier,
            cash_out_scaling_factor=cash_out_scaling_factor,
            max_cash_out_cap_percent=max_in_progress_cash_out_percent,
            max_cash_out_cap_amount=_quantize_money(cap_amount),
            cashout_before_scaling=_quantize_money(cashout_before_scaling),
            cashout_after_scaling=_quantize_money(cashout_after_scaling),
            risk_discount_exponent=risk_discount_exponent,
        )
        _log_quote(ticket=ticket, quote=quote, settings_obj=settings_obj, actor=actor, ip_address=ip_address, user_agent=user_agent, source=source)
        return quote

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
                "cash_out_scaling_factor": str(cash_out_scaling_factor),
                "max_in_progress_cash_out_percent": str(max_in_progress_cash_out_percent),
                "max_in_progress_cap_amount": str(cap_amount),
                "cashout_before_scaling": str(cashout_before_scaling),
                "cashout_after_scaling": str(cashout_after_scaling),
                "risk_discount_exponent": str(risk_discount_exponent),
                "company_margin_percent": str(margin_percent),
                "settled_count": settled_count,
                "winning_count": winning_count,
                "losing_count": losing_count,
                "pending_count": pending_count,
            },
        )
        quote = CashOutQuote(
            eligible=False,
            reason="Cash Out is not available for this ticket.",
            cashout_amount=ZERO,
            original_odds=original_odds,
            completed_odds=completed_odds,
            progress_percent=ZERO,
            potential_win=potential_win,
            settled_count=settled_count,
            winning_count=winning_count,
            losing_count=losing_count,
            pending_count=pending_count,
            required_wins=required_wins,
            maximum_possible_wins=maximum_possible_wins,
            system_progress_factor=system_progress_factor,
            system_paths_factor=system_paths_factor,
            system_winning_paths=system_winning_paths,
            system_total_paths=system_total_paths,
            won_odds=won_odds,
            remaining_odds=remaining_odds,
            risk_discount=risk_discount,
            company_margin_percent=margin_percent,
            risk_multiplier=risk_multiplier,
            cash_out_scaling_factor=cash_out_scaling_factor,
            max_cash_out_cap_percent=max_in_progress_cash_out_percent,
            max_cash_out_cap_amount=_quantize_money(cap_amount),
            cashout_before_scaling=_quantize_money(cashout_before_scaling),
            cashout_after_scaling=_quantize_money(cashout_after_scaling),
            risk_discount_exponent=risk_discount_exponent,
        )
        _log_quote(ticket=ticket, quote=quote, settings_obj=settings_obj, actor=actor, ip_address=ip_address, user_agent=user_agent, source=source)
        return quote

    quote = CashOutQuote(
        eligible=True,
        reason="",
        cashout_amount=_quantize_money(cashout_amount),
        original_odds=original_odds,
        completed_odds=completed_odds,
        progress_percent=progress_percent,
        potential_win=potential_win,
        settled_count=settled_count,
        winning_count=winning_count,
        losing_count=losing_count,
        pending_count=pending_count,
        required_wins=required_wins,
        maximum_possible_wins=maximum_possible_wins,
        system_progress_factor=system_progress_factor,
        system_paths_factor=system_paths_factor,
        system_winning_paths=system_winning_paths,
        system_total_paths=system_total_paths,
        won_odds=won_odds,
        remaining_odds=remaining_odds,
        risk_discount=risk_discount,
        company_margin_percent=margin_percent,
        risk_multiplier=risk_multiplier,
        cash_out_scaling_factor=cash_out_scaling_factor,
        max_cash_out_cap_percent=max_in_progress_cash_out_percent,
        max_cash_out_cap_amount=_quantize_money(cap_amount),
        cashout_before_scaling=_quantize_money(cashout_before_scaling),
        cashout_after_scaling=_quantize_money(cashout_after_scaling),
        risk_discount_exponent=risk_discount_exponent,
    )
    _log_quote(ticket=ticket, quote=quote, settings_obj=settings_obj, actor=actor, ip_address=ip_address, user_agent=user_agent, source=source)
    return quote


def _build_cashout_reference(ticket):
    seed = f"{ticket.ticket_id}-{timezone.now().isoformat()}-{uuid.uuid4().hex}"
    suffix = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:10].upper()
    return f"CASHOUT-{ticket.ticket_id}-{suffix}"


def execute_cashout(*, ticket_id, actor, ip_address="", user_agent=""):
    settings_obj = CashOutSettings.load()
    now = timezone.now()

    with transaction.atomic():
        if not getattr(settings_obj, "enable_full_cash_out", True):
            if getattr(settings_obj, "enable_partial_cash_out", False):
                raise CashOutError("Partial Cash Out is enabled but is not available yet.")
            raise CashOutError("Cash Out is currently disabled.")

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

        quote = build_cashout_quote(
            ticket=ticket,
            settings_obj=settings_obj,
            now=now,
            actor=actor,
            ip_address=ip_address,
            user_agent=user_agent,
            source="execute",
        )
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
        original_odds = _safe_decimal(getattr(quote, "original_odds", ZERO), ZERO).quantize(Decimal("0.000001"))
        completed_odds = _safe_decimal(getattr(quote, "completed_odds", ZERO), ZERO).quantize(Decimal("0.000001"))
        total_stake_amount, cash_stake_amount, bonus_stake_amount = _stake_split(ticket)
        cashout = BetTicketCashOut.objects.create(
            reference=reference,
            ticket=ticket,
            user=ticket.user,
            cashier=cashier,
            agent=agent,
            ticket_type=ticket.bet_type,
            ticket_status=ticket.status,
            stake_amount=total_stake_amount,
            cash_stake_amount=cash_stake_amount,
            bonus_stake_amount=bonus_stake_amount,
            cashout_basis_amount=cash_stake_amount,
            potential_win=quote.potential_win,
            original_odds=original_odds,
            completed_odds=completed_odds,
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
        ticket.cashout_original_odds = original_odds
        ticket.cashout_completed_odds = completed_odds
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
                "ticket_bet_type": str(getattr(ticket, "bet_type", "") or ""),
                "stake_total": str(total_stake_amount),
                "stake_cash": str(cash_stake_amount),
                "stake_bonus": str(bonus_stake_amount),
                "cashout_basis": str(cash_stake_amount),
                "required_wins": int(getattr(quote, "required_wins", 0) or 0),
                "maximum_possible_wins": int(getattr(quote, "maximum_possible_wins", 0) or 0),
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
