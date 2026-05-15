import hashlib
from dataclasses import dataclass
from decimal import Decimal

from django.apps import apps
from django.core.cache import cache
from django.db import transaction
from django.db.models import Count, Sum, Value, DecimalField, Q, Max
from django.db.models.functions import Coalesce
from django.utils import timezone


RISK_SETTINGS_CACHE_KEY = "risk:v1:settings"


def get_risk_settings_cached():
    cached = cache.get(RISK_SETTINGS_CACHE_KEY)
    if cached is not None:
        return cached

    RiskEngineSettings = apps.get_model("risk", "RiskEngineSettings")
    obj = RiskEngineSettings.load()
    data = {
        "is_active": bool(obj.is_active),
        "auto_suspension_enabled": bool(obj.auto_suspension_enabled),
        "max_fixture_liability": Decimal(str(obj.max_fixture_liability)),
        "max_market_liability": Decimal(str(obj.max_market_liability)),
        "max_selection_liability": Decimal(str(obj.max_selection_liability)),
        "risk_threshold_percent": Decimal(str(obj.risk_threshold_percent)),
        "deposit_reminder_threshold": Decimal(str(obj.deposit_reminder_threshold)),
        "rapid_bet_seconds": int(obj.rapid_bet_seconds or 0),
        "duplicate_ticket_window_seconds": int(obj.duplicate_ticket_window_seconds or 0),
        "coordinated_window_seconds": int(obj.coordinated_window_seconds or 0),
        "coordinated_threshold": int(obj.coordinated_threshold or 0),
        "late_bet_minutes": int(obj.late_bet_minutes or 0),
        "high_value_bet_amount": Decimal(str(obj.high_value_bet_amount)),
        "vpn_proxy_detection_enabled": bool(obj.vpn_proxy_detection_enabled),
        "vpn_proxy_block_betting": bool(obj.vpn_proxy_block_betting),
    }
    cache.set(RISK_SETTINGS_CACHE_KEY, data, timeout=60)
    return data


def clear_risk_settings_cache():
    cache.delete(RISK_SETTINGS_CACHE_KEY)


def market_key_for_bet_type(bet_type):
    bt = (bet_type or "").strip().lower()
    if bt in ["home_win", "draw", "away_win"]:
        return "1X2"
    if bt in ["home_dnb", "away_dnb"]:
        return "DNB"
    if bt.startswith("over_") or bt.startswith("under_"):
        return "OU"
    if bt in ["btts_yes", "btts_no"]:
        return "BTTS"
    return "OTHER"


def selection_key_for_bet_type(bet_type):
    return (bet_type or "").strip().lower()


@dataclass(frozen=True)
class SuspensionDecision:
    suspended: bool
    level: str
    reason: str


def is_suspended(fixture_id, market_key=None, selection_key=None):
    FixtureRiskState = apps.get_model("risk", "FixtureRiskState")
    MarketRiskState = apps.get_model("risk", "MarketRiskState")
    SelectionRiskState = apps.get_model("risk", "SelectionRiskState")

    fs = FixtureRiskState.objects.filter(fixture_id=fixture_id, is_suspended=True).values("manual_override").first()
    if fs:
        return True

    if market_key:
        ms = (
            MarketRiskState.objects.filter(fixture_id=fixture_id, market_key=market_key, is_suspended=True)
            .values("manual_override")
            .first()
        )
        if ms:
            return True

    if market_key and selection_key:
        ss = (
            SelectionRiskState.objects.filter(
                fixture_id=fixture_id, market_key=market_key, selection_key=selection_key, is_suspended=True
            )
            .values("manual_override")
            .first()
        )
        if ss:
            return True

    return False


def _ensure_fixture_state(fixture_id):
    FixtureRiskState = apps.get_model("risk", "FixtureRiskState")
    obj, _ = FixtureRiskState.objects.get_or_create(fixture_id=fixture_id)
    return obj


def _ensure_market_state(fixture_id, market_key):
    MarketRiskState = apps.get_model("risk", "MarketRiskState")
    obj, _ = MarketRiskState.objects.get_or_create(fixture_id=fixture_id, market_key=market_key)
    return obj


def _ensure_selection_state(fixture_id, market_key, selection_key):
    SelectionRiskState = apps.get_model("risk", "SelectionRiskState")
    obj, _ = SelectionRiskState.objects.get_or_create(
        fixture_id=fixture_id, market_key=market_key, selection_key=selection_key
    )
    return obj


def auto_suspend_if_needed(
    *,
    actor,
    fixture_id,
    market_key,
    selection_key,
    projected_selection_liability,
    projected_market_liability,
    projected_fixture_liability,
):
    settings = get_risk_settings_cached()
    if not settings.get("is_active") or not settings.get("auto_suspension_enabled"):
        return SuspensionDecision(False, "", "")

    RiskAuditLog = apps.get_model("risk", "RiskAuditLog")

    with transaction.atomic():
        if projected_selection_liability >= settings["max_selection_liability"]:
            state = _ensure_selection_state(fixture_id, market_key, selection_key)
            if not state.manual_override and not state.is_suspended:
                state.is_suspended = True
                state.suspension_reason = "Auto suspended: selection liability exceeded."
                state.suspended_by = actor
                state.suspended_at = timezone.now()
                state.save(update_fields=["is_suspended", "suspension_reason", "suspended_by", "suspended_at", "updated_at"])
                RiskAuditLog.objects.create(
                    action_type="AUTO_SUSPEND_SELECTION",
                    actor=actor,
                    fixture_id=fixture_id,
                    market_key=market_key,
                    selection_key=selection_key,
                    message=state.suspension_reason,
                    data={"projected_selection_liability": str(projected_selection_liability)},
                )
                return SuspensionDecision(True, "selection", state.suspension_reason)

        if projected_market_liability >= settings["max_market_liability"]:
            state = _ensure_market_state(fixture_id, market_key)
            if not state.manual_override and not state.is_suspended:
                state.is_suspended = True
                state.suspension_reason = "Auto suspended: market liability exceeded."
                state.suspended_by = actor
                state.suspended_at = timezone.now()
                state.save(update_fields=["is_suspended", "suspension_reason", "suspended_by", "suspended_at", "updated_at"])
                RiskAuditLog.objects.create(
                    action_type="AUTO_SUSPEND_MARKET",
                    actor=actor,
                    fixture_id=fixture_id,
                    market_key=market_key,
                    message=state.suspension_reason,
                    data={"projected_market_liability": str(projected_market_liability)},
                )
                return SuspensionDecision(True, "market", state.suspension_reason)

        if projected_fixture_liability >= settings["max_fixture_liability"]:
            state = _ensure_fixture_state(fixture_id)
            if not state.manual_override and not state.is_suspended:
                state.is_suspended = True
                state.suspension_reason = "Auto suspended: fixture liability exceeded."
                state.suspended_by = actor
                state.suspended_at = timezone.now()
                state.save(update_fields=["is_suspended", "suspension_reason", "suspended_by", "suspended_at", "updated_at"])
                RiskAuditLog.objects.create(
                    action_type="AUTO_SUSPEND_FIXTURE",
                    actor=actor,
                    fixture_id=fixture_id,
                    message=state.suspension_reason,
                    data={"projected_fixture_liability": str(projected_fixture_liability)},
                )
                return SuspensionDecision(True, "fixture", state.suspension_reason)

    return SuspensionDecision(False, "", "")


def update_liability_snapshots_for_fixture(fixture_id):
    BetTicket = apps.get_model("betting", "BetTicket")
    Selection = apps.get_model("betting", "Selection")
    FixtureLiabilitySnapshot = apps.get_model("risk", "FixtureLiabilitySnapshot")
    MarketLiabilitySnapshot = apps.get_model("risk", "MarketLiabilitySnapshot")
    SelectionLiabilitySnapshot = apps.get_model("risk", "SelectionLiabilitySnapshot")

    ticket_qs = BetTicket.objects.filter(status="pending", selections__fixture_id=fixture_id).distinct()
    fixture_agg = ticket_qs.aggregate(
        total_stake=Coalesce(Sum("stake_amount"), Value(0), output_field=DecimalField()),
        total_payout=Coalesce(Sum("max_winning"), Value(0), output_field=DecimalField()),
        ticket_count=Count("id"),
    )
    total_stake = fixture_agg["total_stake"] or Decimal("0.00")
    total_payout = fixture_agg["total_payout"] or Decimal("0.00")
    net_exposure = (total_payout - total_stake).quantize(Decimal("0.01"))

    snap, _ = FixtureLiabilitySnapshot.objects.get_or_create(fixture_id=fixture_id)
    snap.total_stake = total_stake
    snap.total_potential_payout = total_payout
    snap.net_exposure = net_exposure
    snap.ticket_count = int(fixture_agg["ticket_count"] or 0)
    snap.risk_score = _risk_score_from_liability(total_payout)
    snap.save()

    sel_rows = (
        Selection.objects.filter(bet_ticket__status="pending", fixture_id=fixture_id)
        .values("bet_type")
        .annotate(
            total_stake=Coalesce(Sum("bet_ticket__stake_amount"), Value(0), output_field=DecimalField()),
            total_payout=Coalesce(Sum("bet_ticket__max_winning"), Value(0), output_field=DecimalField()),
            ticket_count=Count("bet_ticket_id", distinct=True),
            last_ticket_at=Max("bet_ticket__placed_at"),
        )
    )

    market_totals = {}
    market_bet_types = {}
    for r in sel_rows:
        bet_type = r["bet_type"]
        market_key = market_key_for_bet_type(bet_type)
        selection_key = selection_key_for_bet_type(bet_type)
        obj, _ = SelectionLiabilitySnapshot.objects.get_or_create(
            fixture_id=fixture_id, market_key=market_key, selection_key=selection_key
        )
        obj.total_stake = r["total_stake"] or Decimal("0.00")
        obj.total_potential_payout = r["total_payout"] or Decimal("0.00")
        obj.ticket_count = int(r["ticket_count"] or 0)
        obj.exposure_percent = (Decimal("0.00") if total_payout <= 0 else (obj.total_potential_payout / total_payout * 100)).quantize(
            Decimal("0.01")
        )
        obj.risk_score = _risk_score_from_liability(obj.total_potential_payout)
        obj.last_ticket_at = r.get("last_ticket_at")
        obj.save()

        market_bet_types.setdefault(market_key, set()).add(bet_type)
        buf = market_totals.setdefault(
            market_key,
            {"total_stake": Decimal("0.00"), "total_payout": Decimal("0.00"), "last_ticket_at": None},
        )
        buf["total_stake"] += obj.total_stake
        buf["total_payout"] += obj.total_potential_payout
        lt = r.get("last_ticket_at")
        if lt and (buf["last_ticket_at"] is None or lt > buf["last_ticket_at"]):
            buf["last_ticket_at"] = lt

    for market_key, buf in market_totals.items():
        market_total_payout = (buf["total_payout"] or Decimal("0.00")).quantize(Decimal("0.01"))
        market_total_stake = (buf["total_stake"] or Decimal("0.00")).quantize(Decimal("0.01"))
        market_net = (market_total_payout - market_total_stake).quantize(Decimal("0.01"))
        bet_types = list(market_bet_types.get(market_key) or [])
        market_ticket_count = (
            BetTicket.objects.filter(status="pending", selections__fixture_id=fixture_id, selections__bet_type__in=bet_types)
            .distinct()
            .count()
            if bet_types
            else 0
        )
        obj, _ = MarketLiabilitySnapshot.objects.get_or_create(fixture_id=fixture_id, market_key=market_key)
        obj.total_stake = market_total_stake
        obj.total_potential_payout = market_total_payout
        obj.net_exposure = market_net
        obj.ticket_count = int(market_ticket_count or 0)
        obj.exposure_percent = (Decimal("0.00") if total_payout <= 0 else (market_total_payout / total_payout * 100)).quantize(
            Decimal("0.01")
        )
        obj.risk_score = _risk_score_from_liability(market_total_payout)
        obj.last_ticket_at = buf.get("last_ticket_at")
        obj.save()


def _risk_score_from_liability(liability):
    settings = get_risk_settings_cached()
    max_fix = settings.get("max_fixture_liability") or Decimal("0.00")
    if max_fix <= 0:
        return 0
    pct = (Decimal(str(liability or 0)) / max_fix * 100).quantize(Decimal("0.01"))
    if pct <= 0:
        return 0
    if pct >= 100:
        return 100
    return int(pct)


def compute_duplicate_ticket_signature(*, user_id, selections, stake_per_line, is_system_bet, permutation_count, fingerprint_hash, ip_address):
    normalized = sorted([(str(s.get("fixture_id") or s.get("fixtureId")), str(s.get("bet_type") or s.get("outcome"))) for s in (selections or [])])
    payload = f"u:{user_id}|sel:{normalized}|stake:{stake_per_line}|sys:{bool(is_system_bet)}|k:{permutation_count}|fp:{fingerprint_hash}|ip:{ip_address}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def log_duplicate_ticket_if_needed(*, user, ticket, signature, ip_address, fingerprint_hash):
    DuplicateTicketLog = apps.get_model("risk", "DuplicateTicketLog")
    SuspiciousActivityLog = apps.get_model("risk", "SuspiciousActivityLog")

    window_key = f"risk:dup:{signature}"
    if cache.get(window_key):
        DuplicateTicketLog.objects.create(
            user=user,
            ticket=ticket,
            signature=signature,
            ip_address=ip_address,
            fingerprint_hash=fingerprint_hash or "",
        )
        SuspiciousActivityLog.objects.create(
            user=user,
            ticket=ticket,
            kind="DUPLICATE_TICKET",
            risk_score=70,
            ip_address=ip_address,
            fingerprint_hash=fingerprint_hash or "",
            data={"signature": signature},
        )
        return True

    settings = get_risk_settings_cached()
    window_seconds = int(settings.get("duplicate_ticket_window_seconds") or 90)
    cache.set(window_key, 1, timeout=max(30, window_seconds))
    return False


def update_agent_exposure_snapshot(agent_id):
    if not agent_id:
        return

    AgentExposureSnapshot = apps.get_model("risk", "AgentExposureSnapshot")
    BetTicket = apps.get_model("betting", "BetTicket")
    User = apps.get_model("betting", "User")

    agent = User.objects.filter(id=agent_id).first()
    if not agent:
        return

    today = timezone.localdate()
    tickets = (
        BetTicket.objects.filter(
            Q(user=agent) | Q(user__agent=agent) | Q(user__super_agent=agent) | Q(user__master_agent=agent),
            placed_at__date=today,
        )
        .exclude(status__in=["deleted", "cancelled"])
    )

    agg = tickets.aggregate(
        total_stake=Coalesce(Sum("stake_amount"), Value(0), output_field=DecimalField()),
        total_payout=Coalesce(Sum("max_winning"), Value(0), output_field=DecimalField()),
        ticket_count=Count("id"),
    )

    snap, _ = AgentExposureSnapshot.objects.get_or_create(agent_id=agent_id)
    snap.total_stake_today = agg["total_stake"] or Decimal("0.00")
    snap.total_potential_payout_today = agg["total_payout"] or Decimal("0.00")
    snap.ticket_count_today = int(agg["ticket_count"] or 0)
    snap.save()


def update_user_exposure_snapshot(user_id):
    if not user_id:
        return
    UserExposureSnapshot = apps.get_model("risk", "UserExposureSnapshot")
    BetTicket = apps.get_model("betting", "BetTicket")
    today = timezone.localdate()
    tickets = BetTicket.objects.filter(user_id=user_id, placed_at__date=today).exclude(status__in=["deleted", "cancelled"])
    agg = tickets.aggregate(
        total_stake=Coalesce(Sum("stake_amount"), Value(0), output_field=DecimalField()),
        total_payout=Coalesce(Sum("max_winning"), Value(0), output_field=DecimalField()),
        ticket_count=Count("id"),
    )
    snap, _ = UserExposureSnapshot.objects.get_or_create(user_id=user_id)
    snap.total_stake_today = agg["total_stake"] or Decimal("0.00")
    snap.total_potential_payout_today = agg["total_payout"] or Decimal("0.00")
    snap.ticket_count_today = int(agg["ticket_count"] or 0)
    snap.save()


def update_betting_period_liability_snapshot(period_id):
    if not period_id:
        return
    BettingPeriodLiabilitySnapshot = apps.get_model("risk", "BettingPeriodLiabilitySnapshot")
    BetTicket = apps.get_model("betting", "BetTicket")
    ticket_qs = BetTicket.objects.filter(status="pending", selections__fixture__betting_period_id=period_id).distinct()
    agg = ticket_qs.aggregate(
        total_stake=Coalesce(Sum("stake_amount"), Value(0), output_field=DecimalField()),
        total_payout=Coalesce(Sum("max_winning"), Value(0), output_field=DecimalField()),
        ticket_count=Count("id"),
    )
    total_stake = agg["total_stake"] or Decimal("0.00")
    total_payout = agg["total_payout"] or Decimal("0.00")
    net_exposure = (total_payout - total_stake).quantize(Decimal("0.01"))
    snap, _ = BettingPeriodLiabilitySnapshot.objects.get_or_create(betting_period_id=period_id)
    snap.total_stake = total_stake
    snap.total_potential_payout = total_payout
    snap.net_exposure = net_exposure
    snap.ticket_count = int(agg["ticket_count"] or 0)
    snap.risk_score = _risk_score_from_liability(total_payout)
    snap.save()


def record_device_fingerprint(*, user, fingerprint_hash, ip_address, user_agent="", timezone_name="", screen="", platform="", language=""):
    if not user or not fingerprint_hash:
        return None
    DeviceFingerprint = apps.get_model("risk", "DeviceFingerprint")
    obj, created = DeviceFingerprint.objects.get_or_create(
        user=user,
        fingerprint_hash=fingerprint_hash,
        defaults={
            "ip_address": ip_address,
            "user_agent": user_agent or "",
            "timezone_name": timezone_name or "",
            "screen": screen or "",
            "platform": platform or "",
            "language": language or "",
            "last_seen_at": timezone.now(),
        },
    )
    if not created:
        obj.ip_address = ip_address
        obj.user_agent = user_agent or obj.user_agent
        obj.timezone_name = timezone_name or obj.timezone_name
        obj.screen = screen or obj.screen
        obj.platform = platform or obj.platform
        obj.language = language or obj.language
        obj.last_seen_at = timezone.now()
        obj.save(update_fields=["ip_address", "user_agent", "timezone_name", "screen", "platform", "language", "last_seen_at"])
    return obj


def evaluate_ticket_risk(
    *,
    user,
    ticket=None,
    ip_address="",
    fingerprint_hash="",
    selections=None,
    stake_amount=None,
):
    settings = get_risk_settings_cached()
    if not settings.get("is_active"):
        return 0

    SuspiciousActivityLog = apps.get_model("risk", "SuspiciousActivityLog")
    ArbitrageAlert = apps.get_model("risk", "ArbitrageAlert")
    Selection = apps.get_model("betting", "Selection")
    Fixture = apps.get_model("betting", "Fixture")
    BetTicket = apps.get_model("betting", "BetTicket")

    now = timezone.now()
    total_risk = 0

    rapid_seconds = int(settings.get("rapid_bet_seconds") or 0)
    if rapid_seconds > 0 and user:
        key = f"risk:rapid:u:{user.id}"
        last = cache.get(key)
        cache.set(key, now.timestamp(), timeout=max(rapid_seconds * 4, 30))
        try:
            if last and (now.timestamp() - float(last)) <= rapid_seconds:
                SuspiciousActivityLog.objects.create(
                    user=user,
                    ticket=ticket,
                    kind="RAPID_BETTING",
                    risk_score=60,
                    ip_address=ip_address or None,
                    fingerprint_hash=fingerprint_hash or "",
                    data={"seconds": rapid_seconds},
                )
                total_risk += 20
        except Exception:
            pass

    late_minutes = int(settings.get("late_bet_minutes") or 0)
    if late_minutes > 0 and selections:
        fixture_ids = {int(s["fixture"].id) if isinstance(s.get("fixture"), Fixture) else int(s.get("fixture_id") or s.get("fixtureId") or 0) for s in selections}
        fixture_ids.discard(0)
        for f in Fixture.objects.filter(id__in=list(fixture_ids)).only("id", "match_date", "match_time"):
            kickoff = timezone.make_aware(timezone.datetime.combine(f.match_date, f.match_time))
            delta = (kickoff - now).total_seconds()
            if 0 <= delta <= (late_minutes * 60):
                SuspiciousActivityLog.objects.create(
                    user=user,
                    ticket=ticket,
                    kind="LATE_BETTING",
                    risk_score=40,
                    ip_address=ip_address or None,
                    fingerprint_hash=fingerprint_hash or "",
                    data={"fixture_id": f.id, "minutes": late_minutes},
                )
                total_risk += 10

    high_value_amount = Decimal(str(settings.get("high_value_bet_amount") or "0"))
    try:
        stake_amount = Decimal(str(stake_amount or "0"))
    except Exception:
        stake_amount = Decimal("0")
    if high_value_amount > 0 and stake_amount >= high_value_amount:
        SuspiciousActivityLog.objects.create(
            user=user,
            ticket=ticket,
            kind="ODDS_TARGETING",
            risk_score=35,
            ip_address=ip_address or None,
            fingerprint_hash=fingerprint_hash or "",
            data={"stake_amount": str(stake_amount), "threshold": str(high_value_amount)},
        )
        total_risk += 10

    if user and selections:
        opposite = {
            "home_win": ["away_win"],
            "away_win": ["home_win"],
            "draw": ["home_win", "away_win"],
            "btts_yes": ["btts_no"],
            "btts_no": ["btts_yes"],
            "home_dnb": ["away_dnb"],
            "away_dnb": ["home_dnb"],
            "over_1_5": ["under_1_5"],
            "under_1_5": ["over_1_5"],
            "over_2_5": ["under_2_5"],
            "under_2_5": ["over_2_5"],
            "over_3_5": ["under_3_5"],
            "under_3_5": ["over_3_5"],
        }
        for s in selections:
            fixture_id = s["fixture"].id if isinstance(s.get("fixture"), Fixture) else int(s.get("fixture_id") or s.get("fixtureId") or 0)
            bt = str(s.get("bet_type") or s.get("outcome") or "").strip().lower()
            if not fixture_id or not bt or bt not in opposite:
                continue
            if Selection.objects.filter(
                bet_ticket__user=user,
                bet_ticket__status="pending",
                fixture_id=fixture_id,
                bet_type__in=opposite[bt],
            ).exists():
                ArbitrageAlert.objects.create(
                    user=user,
                    ticket=ticket,
                    risk_score=80,
                    data={"fixture_id": fixture_id, "bet_type": bt, "opposite": opposite[bt]},
                )
                SuspiciousActivityLog.objects.create(
                    user=user,
                    ticket=ticket,
                    kind="ARBITRAGE",
                    risk_score=80,
                    ip_address=ip_address or None,
                    fingerprint_hash=fingerprint_hash or "",
                    data={"fixture_id": fixture_id, "bet_type": bt, "opposite": opposite[bt]},
                )
                total_risk += 30
                break

    coordinated_window = int(settings.get("coordinated_window_seconds") or 0)
    coordinated_threshold = int(settings.get("coordinated_threshold") or 0)
    if coordinated_window > 0 and coordinated_threshold > 0 and selections and (ip_address or fingerprint_hash):
        bucket = int(now.timestamp() // max(coordinated_window, 1))
        for s in selections:
            fixture_id = s["fixture"].id if isinstance(s.get("fixture"), Fixture) else int(s.get("fixture_id") or s.get("fixtureId") or 0)
            bt = str(s.get("bet_type") or s.get("outcome") or "").strip().lower()
            if not fixture_id or not bt:
                continue
            mk = market_key_for_bet_type(bt)
            sk = selection_key_for_bet_type(bt)
            key = f"risk:coord:{bucket}:f:{fixture_id}:m:{mk}:s:{sk}:ip:{ip_address}:fp:{fingerprint_hash}"
            try:
                count = cache.get(key) or 0
                count = int(count) + 1
                cache.set(key, count, timeout=max(coordinated_window * 2, 120))
                if count == coordinated_threshold:
                    SuspiciousActivityLog.objects.create(
                        user=user,
                        ticket=ticket,
                        kind="COORDINATED_BETTING",
                        risk_score=75,
                        ip_address=ip_address or None,
                        fingerprint_hash=fingerprint_hash or "",
                        data={"fixture_id": fixture_id, "market_key": mk, "selection_key": sk, "count": count},
                    )
                    total_risk += 25
            except Exception:
                continue

    if user and fingerprint_hash:
        DeviceFingerprint = apps.get_model("risk", "DeviceFingerprint")
        try:
            distinct_users = (
                DeviceFingerprint.objects.filter(fingerprint_hash=fingerprint_hash)
                .values("user_id")
                .distinct()
                .count()
            )
            if distinct_users >= 2:
                SuspiciousActivityLog.objects.create(
                    user=user,
                    ticket=ticket,
                    kind="MULTI_ACCOUNTING",
                    risk_score=70,
                    ip_address=ip_address or None,
                    fingerprint_hash=fingerprint_hash or "",
                    data={"distinct_users": distinct_users},
                )
                try:
                    SyndicateGroup = apps.get_model("risk", "SyndicateGroup")
                    SyndicateMember = apps.get_model("risk", "SyndicateMember")
                    reason = f"fingerprint:{fingerprint_hash}"
                    group = SyndicateGroup.objects.filter(reason=reason, is_active=True).first()
                    if not group:
                        group = SyndicateGroup.objects.create(
                            name=f"Fingerprint {fingerprint_hash[:10]}",
                            reason=reason,
                            risk_score=80,
                            is_active=True,
                        )
                    member_ids = list(
                        DeviceFingerprint.objects.filter(fingerprint_hash=fingerprint_hash)
                        .values_list("user_id", flat=True)
                        .distinct()[:50]
                    )
                    for uid in member_ids:
                        SyndicateMember.objects.get_or_create(
                            group=group,
                            user_id=uid,
                            defaults={"evidence": {"fingerprint_hash": fingerprint_hash, "ip_address": ip_address}},
                        )
                except Exception:
                    pass
                total_risk += 20
        except Exception:
            pass

    if settings.get("vpn_proxy_detection_enabled") and ip_address:
        ip_result = check_ip_intelligence(ip_address)
        if ip_result.get("blocked"):
            SuspiciousActivityLog.objects.create(
                user=user,
                ticket=ticket,
                kind="MULTI_ACCOUNTING",
                risk_score=90,
                ip_address=ip_address or None,
                fingerprint_hash=fingerprint_hash or "",
                data={"reason": "vpn_proxy_block", "ip": ip_address},
            )
            total_risk += 40

    return min(100, int(total_risk))


def check_ip_intelligence(ip_address):
    settings = get_risk_settings_cached()
    IPWhitelistEntry = apps.get_model("risk", "IPWhitelistEntry")
    IPIntelligence = apps.get_model("risk", "IPIntelligence")
    if not ip_address:
        return {"blocked": False}

    if IPWhitelistEntry.objects.filter(ip_address=ip_address, is_active=True).exists():
        return {"blocked": False, "whitelisted": True}

    cached_key = f"risk:ipintel:{ip_address}"
    cached = cache.get(cached_key)
    if cached is not None:
        blocked = bool(cached.get("blocked"))
        return {**cached, "blocked": blocked}

    rec = IPIntelligence.objects.filter(ip_address=ip_address).first()
    if rec and rec.checked_at and (timezone.now() - rec.checked_at).total_seconds() < 86400:
        blocked = bool(settings.get("vpn_proxy_block_betting") and (rec.is_vpn or rec.is_proxy or rec.is_tor or rec.is_datacenter))
        out = {
            "blocked": blocked,
            "is_vpn": bool(rec.is_vpn),
            "is_proxy": bool(rec.is_proxy),
            "is_tor": bool(rec.is_tor),
            "is_datacenter": bool(rec.is_datacenter),
            "risk_score": int(rec.risk_score or 0),
            "provider": rec.provider,
        }
        cache.set(cached_key, out, timeout=600)
        return out

    if not settings.get("vpn_proxy_detection_enabled"):
        out = {"blocked": False}
        cache.set(cached_key, out, timeout=600)
        return out

    out = {"blocked": False}
    try:
        import requests

        resp = requests.get(f"https://ipapi.co/{ip_address}/json/", timeout=2.5)
        data = resp.json() if resp.ok else {}
        org = str(data.get("org") or data.get("asn") or "").lower()
        is_dc = any(x in org for x in ["hosting", "cloud", "data center", "datacenter", "vpn", "proxy", "tor"])
        risk_score = 80 if is_dc else 0
        rec, _ = IPIntelligence.objects.get_or_create(ip_address=ip_address)
        rec.provider = "ipapi"
        rec.is_datacenter = bool(is_dc)
        rec.is_vpn = "vpn" in org
        rec.is_proxy = "proxy" in org
        rec.is_tor = "tor" in org
        rec.risk_score = int(risk_score)
        rec.raw = data or {}
        rec.checked_at = timezone.now()
        rec.save()
        out = {
            "is_vpn": bool(rec.is_vpn),
            "is_proxy": bool(rec.is_proxy),
            "is_tor": bool(rec.is_tor),
            "is_datacenter": bool(rec.is_datacenter),
            "risk_score": int(rec.risk_score or 0),
            "provider": rec.provider,
        }
    except Exception:
        out = {}

    blocked = bool(settings.get("vpn_proxy_block_betting") and (out.get("is_vpn") or out.get("is_proxy") or out.get("is_tor") or out.get("is_datacenter")))
    out["blocked"] = blocked
    cache.set(cached_key, out, timeout=600)
    return out
