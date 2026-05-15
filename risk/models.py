from decimal import Decimal

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.utils import timezone


class RiskEngineSettings(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    is_active = models.BooleanField(default=True)

    auto_suspension_enabled = models.BooleanField(default=True)

    max_fixture_liability = models.DecimalField(
        max_digits=16,
        decimal_places=2,
        default=Decimal("20000000.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
        help_text="Auto-suspend fixture when projected payout liability exceeds this amount.",
    )
    max_market_liability = models.DecimalField(
        max_digits=16,
        decimal_places=2,
        default=Decimal("10000000.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
        help_text="Auto-suspend market when projected payout liability exceeds this amount.",
    )
    max_selection_liability = models.DecimalField(
        max_digits=16,
        decimal_places=2,
        default=Decimal("7000000.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
        help_text="Auto-suspend selection when projected payout liability exceeds this amount.",
    )

    risk_threshold_percent = models.DecimalField(
        max_digits=6,
        decimal_places=2,
        default=Decimal("85.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
        help_text="0-100. Threshold used by risk scoring and alerting.",
    )

    deposit_reminder_threshold = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal("1000.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
        help_text="If wallet balance is less than or equal to this amount, send deposit reminder notifications.",
    )

    rapid_bet_seconds = models.PositiveIntegerField(
        default=4,
        help_text="Flag rapid consecutive bet placements by the same user within this number of seconds.",
    )
    duplicate_ticket_window_seconds = models.PositiveIntegerField(
        default=90,
        help_text="Time window to treat tickets as potential duplicates (same signature).",
    )
    coordinated_window_seconds = models.PositiveIntegerField(
        default=60,
        help_text="Time window (seconds) for coordinated betting detection per IP/fingerprint.",
    )
    coordinated_threshold = models.PositiveIntegerField(
        default=5,
        help_text="If this many tickets on the same selection are detected in the coordinated window, flag as coordinated betting.",
    )
    late_bet_minutes = models.PositiveIntegerField(
        default=5,
        help_text="Flag late betting when a ticket is placed within this many minutes of kickoff.",
    )
    high_value_bet_amount = models.DecimalField(
        max_digits=16,
        decimal_places=2,
        default=Decimal("100000.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
        help_text="Flag a ticket as high-value when total stake is greater than or equal to this amount.",
    )
    vpn_proxy_detection_enabled = models.BooleanField(default=False)
    vpn_proxy_block_betting = models.BooleanField(default=False)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_risk_engine_settings",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="updated_risk_engine_settings",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Risk Engine Settings"
        verbose_name_plural = "Risk Engine Settings"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def __str__(self):
        return "Risk Engine Settings"


class FixtureRiskState(models.Model):
    fixture = models.OneToOneField("betting.Fixture", on_delete=models.CASCADE, related_name="risk_state")
    is_suspended = models.BooleanField(default=False, db_index=True)
    suspension_reason = models.CharField(max_length=255, blank=True, default="")
    suspended_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="fixture_suspensions",
    )
    suspended_at = models.DateTimeField(null=True, blank=True)
    resumed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="fixture_resumptions",
    )
    resumed_at = models.DateTimeField(null=True, blank=True)
    manual_override = models.BooleanField(default=False, help_text="If set, automated actions will not change this state.")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=["is_suspended", "updated_at"])]

    def __str__(self):
        return f"FixtureRiskState({self.fixture_id})"


class MarketRiskState(models.Model):
    fixture = models.ForeignKey("betting.Fixture", on_delete=models.CASCADE, related_name="market_risk_states")
    market_key = models.CharField(max_length=50, db_index=True)
    is_suspended = models.BooleanField(default=False, db_index=True)
    suspension_reason = models.CharField(max_length=255, blank=True, default="")
    suspended_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="market_suspensions",
    )
    suspended_at = models.DateTimeField(null=True, blank=True)
    resumed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="market_resumptions",
    )
    resumed_at = models.DateTimeField(null=True, blank=True)
    manual_override = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = (("fixture", "market_key"),)
        indexes = [models.Index(fields=["fixture", "market_key", "is_suspended"])]

    def __str__(self):
        return f"MarketRiskState({self.fixture_id},{self.market_key})"


class SelectionRiskState(models.Model):
    fixture = models.ForeignKey("betting.Fixture", on_delete=models.CASCADE, related_name="selection_risk_states")
    market_key = models.CharField(max_length=50, db_index=True)
    selection_key = models.CharField(max_length=50, db_index=True)
    is_suspended = models.BooleanField(default=False, db_index=True)
    suspension_reason = models.CharField(max_length=255, blank=True, default="")
    suspended_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="selection_suspensions",
    )
    suspended_at = models.DateTimeField(null=True, blank=True)
    resumed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="selection_resumptions",
    )
    resumed_at = models.DateTimeField(null=True, blank=True)
    manual_override = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = (("fixture", "market_key", "selection_key"),)
        indexes = [models.Index(fields=["fixture", "market_key", "selection_key", "is_suspended"])]

    def __str__(self):
        return f"SelectionRiskState({self.fixture_id},{self.market_key},{self.selection_key})"


class FixtureLiabilitySnapshot(models.Model):
    fixture = models.OneToOneField("betting.Fixture", on_delete=models.CASCADE, related_name="liability_snapshot")
    total_stake = models.DecimalField(max_digits=16, decimal_places=2, default=Decimal("0.00"))
    total_potential_payout = models.DecimalField(max_digits=16, decimal_places=2, default=Decimal("0.00"))
    net_exposure = models.DecimalField(max_digits=16, decimal_places=2, default=Decimal("0.00"))
    ticket_count = models.PositiveIntegerField(default=0)
    risk_score = models.PositiveSmallIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True, db_index=True)

    class Meta:
        indexes = [models.Index(fields=["risk_score", "updated_at"])]

    def __str__(self):
        return f"FixtureLiabilitySnapshot({self.fixture_id})"


class MarketLiabilitySnapshot(models.Model):
    fixture = models.ForeignKey("betting.Fixture", on_delete=models.CASCADE, related_name="market_liabilities")
    market_key = models.CharField(max_length=50, db_index=True)

    total_stake = models.DecimalField(max_digits=16, decimal_places=2, default=Decimal("0.00"))
    total_potential_payout = models.DecimalField(max_digits=16, decimal_places=2, default=Decimal("0.00"))
    net_exposure = models.DecimalField(max_digits=16, decimal_places=2, default=Decimal("0.00"))
    ticket_count = models.PositiveIntegerField(default=0)

    exposure_percent = models.DecimalField(max_digits=7, decimal_places=2, default=Decimal("0.00"))
    risk_score = models.PositiveSmallIntegerField(default=0)
    last_ticket_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True, db_index=True)

    class Meta:
        unique_together = (("fixture", "market_key"),)
        indexes = [
            models.Index(fields=["fixture", "market_key"]),
            models.Index(fields=["risk_score", "updated_at"]),
        ]

    def __str__(self):
        return f"MarketLiabilitySnapshot({self.fixture_id},{self.market_key})"


class SelectionLiabilitySnapshot(models.Model):
    fixture = models.ForeignKey("betting.Fixture", on_delete=models.CASCADE, related_name="selection_liabilities")
    market_key = models.CharField(max_length=50, db_index=True)
    selection_key = models.CharField(max_length=50, db_index=True)

    total_stake = models.DecimalField(max_digits=16, decimal_places=2, default=Decimal("0.00"))
    total_potential_payout = models.DecimalField(max_digits=16, decimal_places=2, default=Decimal("0.00"))
    ticket_count = models.PositiveIntegerField(default=0)

    exposure_percent = models.DecimalField(max_digits=7, decimal_places=2, default=Decimal("0.00"))
    risk_score = models.PositiveSmallIntegerField(default=0)

    last_ticket_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True, db_index=True)

    class Meta:
        unique_together = (("fixture", "market_key", "selection_key"),)
        indexes = [
            models.Index(fields=["fixture", "market_key", "selection_key"]),
            models.Index(fields=["risk_score", "updated_at"]),
        ]

    def __str__(self):
        return f"SelectionLiabilitySnapshot({self.fixture_id},{self.market_key},{self.selection_key})"


class AgentExposureSnapshot(models.Model):
    agent = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="agent_exposure_snapshot")
    total_stake_today = models.DecimalField(max_digits=16, decimal_places=2, default=Decimal("0.00"))
    total_potential_payout_today = models.DecimalField(max_digits=16, decimal_places=2, default=Decimal("0.00"))
    ticket_count_today = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True, db_index=True)

    class Meta:
        indexes = [models.Index(fields=["updated_at"])]

    def __str__(self):
        return f"AgentExposureSnapshot({self.agent_id})"


class UserExposureSnapshot(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="user_exposure_snapshot")
    total_stake_today = models.DecimalField(max_digits=16, decimal_places=2, default=Decimal("0.00"))
    total_potential_payout_today = models.DecimalField(max_digits=16, decimal_places=2, default=Decimal("0.00"))
    ticket_count_today = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True, db_index=True)

    class Meta:
        indexes = [models.Index(fields=["updated_at"])]

    def __str__(self):
        return f"UserExposureSnapshot({self.user_id})"


class BettingPeriodLiabilitySnapshot(models.Model):
    betting_period = models.OneToOneField("betting.BettingPeriod", on_delete=models.CASCADE, related_name="liability_snapshot")
    total_stake = models.DecimalField(max_digits=16, decimal_places=2, default=Decimal("0.00"))
    total_potential_payout = models.DecimalField(max_digits=16, decimal_places=2, default=Decimal("0.00"))
    net_exposure = models.DecimalField(max_digits=16, decimal_places=2, default=Decimal("0.00"))
    ticket_count = models.PositiveIntegerField(default=0)
    risk_score = models.PositiveSmallIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True, db_index=True)

    class Meta:
        indexes = [models.Index(fields=["risk_score", "updated_at"])]

    def __str__(self):
        return f"BettingPeriodLiabilitySnapshot({self.betting_period_id})"


class IPWhitelistEntry(models.Model):
    ip_address = models.GenericIPAddressField(db_index=True, unique=True)
    reason = models.CharField(max_length=255, blank=True, default="")
    is_active = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "IP Whitelist Entry"
        verbose_name_plural = "IP Whitelist Entries"

    def __str__(self):
        return self.ip_address


class IPIntelligence(models.Model):
    ip_address = models.GenericIPAddressField(db_index=True, unique=True)
    provider = models.CharField(max_length=40, blank=True, default="")
    is_vpn = models.BooleanField(default=False, db_index=True)
    is_proxy = models.BooleanField(default=False, db_index=True)
    is_tor = models.BooleanField(default=False, db_index=True)
    is_datacenter = models.BooleanField(default=False, db_index=True)
    risk_score = models.PositiveSmallIntegerField(default=0, db_index=True)
    raw = models.JSONField(blank=True, default=dict)
    checked_at = models.DateTimeField(null=True, blank=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True, db_index=True)

    def __str__(self):
        return f"IPIntelligence({self.ip_address})"


class RiskAuditLog(models.Model):
    ACTION_CHOICES = (
        ("AUTO_SUSPEND_FIXTURE", "Auto Suspend Fixture"),
        ("AUTO_SUSPEND_MARKET", "Auto Suspend Market"),
        ("AUTO_SUSPEND_SELECTION", "Auto Suspend Selection"),
        ("MANUAL_SUSPEND", "Manual Suspend"),
        ("MANUAL_RESUME", "Manual Resume"),
        ("OVERRIDE_SET", "Override Set"),
        ("OVERRIDE_CLEARED", "Override Cleared"),
        ("RISK_ALERT", "Risk Alert"),
    )

    action_type = models.CharField(max_length=40, choices=ACTION_CHOICES, db_index=True)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    fixture = models.ForeignKey("betting.Fixture", on_delete=models.SET_NULL, null=True, blank=True)
    market_key = models.CharField(max_length=50, blank=True, default="")
    selection_key = models.CharField(max_length=50, blank=True, default="")
    message = models.TextField(blank=True, default="")
    data = models.JSONField(blank=True, default=dict)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.action_type} {self.created_at}"


class SuspiciousActivityLog(models.Model):
    KIND_CHOICES = (
        ("DUPLICATE_TICKET", "Duplicate Ticket"),
        ("RAPID_BETTING", "Rapid Betting"),
        ("COORDINATED_BETTING", "Coordinated Betting"),
        ("LATE_BETTING", "Late Betting"),
        ("ODDS_TARGETING", "Unusual Odds Targeting"),
        ("HIGH_ROI", "High ROI"),
        ("ARBITRAGE", "Arbitrage"),
        ("MULTI_ACCOUNTING", "Multi Accounting"),
    )

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="suspicious_logs")
    ticket = models.ForeignKey("betting.BetTicket", on_delete=models.SET_NULL, null=True, blank=True, related_name="suspicious_logs")
    kind = models.CharField(max_length=40, choices=KIND_CHOICES, db_index=True)
    risk_score = models.PositiveSmallIntegerField(default=0, db_index=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    fingerprint_hash = models.CharField(max_length=128, blank=True, default="")
    data = models.JSONField(blank=True, default=dict)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.kind} {self.user_id}"


class SharpBettorProfile(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="sharp_profile")
    is_flagged = models.BooleanField(default=False, db_index=True)
    flagged_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="flagged_sharp_bettors"
    )
    win_rate = models.DecimalField(max_digits=6, decimal_places=2, default=Decimal("0.00"))
    roi = models.DecimalField(max_digits=9, decimal_places=4, default=Decimal("0.0000"))
    yield_percent = models.DecimalField(max_digits=7, decimal_places=2, default=Decimal("0.00"))
    avg_odds = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    total_stake = models.DecimalField(max_digits=16, decimal_places=2, default=Decimal("0.00"))
    total_profit = models.DecimalField(max_digits=16, decimal_places=2, default=Decimal("0.00"))
    ticket_count = models.PositiveIntegerField(default=0)
    last_calculated_at = models.DateTimeField(null=True, blank=True, db_index=True)
    notes = models.TextField(blank=True, default="")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=["is_flagged", "roi"])]

    def __str__(self):
        return f"SharpBettorProfile({self.user_id})"


class DeviceFingerprint(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="device_fingerprints")
    fingerprint_hash = models.CharField(max_length=128, db_index=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True, db_index=True)
    user_agent = models.TextField(blank=True, default="")
    timezone_name = models.CharField(max_length=60, blank=True, default="")
    screen = models.CharField(max_length=60, blank=True, default="")
    platform = models.CharField(max_length=60, blank=True, default="")
    language = models.CharField(max_length=40, blank=True, default="")
    last_seen_at = models.DateTimeField(default=timezone.now, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["fingerprint_hash", "ip_address"])]

    def __str__(self):
        return f"DeviceFingerprint({self.user_id})"


class SyndicateGroup(models.Model):
    name = models.CharField(max_length=120, blank=True, default="")
    risk_score = models.PositiveSmallIntegerField(default=0, db_index=True)
    reason = models.CharField(max_length=255, blank=True, default="")
    is_active = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.name or f"SyndicateGroup({self.id})"


class SyndicateMember(models.Model):
    group = models.ForeignKey(SyndicateGroup, on_delete=models.CASCADE, related_name="members")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="syndicate_memberships")
    role = models.CharField(max_length=30, blank=True, default="")
    evidence = models.JSONField(blank=True, default=dict)
    joined_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = (("group", "user"),)
        indexes = [models.Index(fields=["group", "user"])]

    def __str__(self):
        return f"SyndicateMember({self.group_id},{self.user_id})"


class DuplicateTicketLog(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="duplicate_ticket_logs")
    ticket = models.ForeignKey("betting.BetTicket", on_delete=models.SET_NULL, null=True, blank=True)
    signature = models.CharField(max_length=128, db_index=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True, db_index=True)
    fingerprint_hash = models.CharField(max_length=128, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    data = models.JSONField(blank=True, default=dict)

    class Meta:
        indexes = [models.Index(fields=["signature", "created_at"])]

    def __str__(self):
        return f"DuplicateTicketLog({self.user_id})"


class ArbitrageAlert(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="arbitrage_alerts")
    ticket = models.ForeignKey("betting.BetTicket", on_delete=models.SET_NULL, null=True, blank=True)
    risk_score = models.PositiveSmallIntegerField(default=0, db_index=True)
    data = models.JSONField(blank=True, default=dict)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"ArbitrageAlert({self.user_id})"
