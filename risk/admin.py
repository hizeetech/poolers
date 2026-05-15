from django.contrib import admin, messages
from django.db import transaction
from django.db.utils import OperationalError, ProgrammingError
from django.utils import timezone

from .models import (
    RiskEngineSettings,
    FixtureRiskState,
    MarketRiskState,
    SelectionRiskState,
    FixtureLiabilitySnapshot,
    MarketLiabilitySnapshot,
    SelectionLiabilitySnapshot,
    AgentExposureSnapshot,
    UserExposureSnapshot,
    BettingPeriodLiabilitySnapshot,
    RiskAuditLog,
    SuspiciousActivityLog,
    SharpBettorProfile,
    DeviceFingerprint,
    SyndicateGroup,
    SyndicateMember,
    DuplicateTicketLog,
    ArbitrageAlert,
    IPWhitelistEntry,
    IPIntelligence,
)
from .services import clear_risk_settings_cache


@admin.register(RiskEngineSettings)
class RiskEngineSettingsAdmin(admin.ModelAdmin):
    list_display = ("is_active", "auto_suspension_enabled", "max_fixture_liability", "risk_threshold_percent", "updated_at")
    readonly_fields = ("created_at", "updated_at", "created_by", "updated_by")

    def has_delete_permission(self, request, obj=None):
        return False

    def has_add_permission(self, request):
        try:
            return not RiskEngineSettings.objects.exists()
        except (OperationalError, ProgrammingError):
            return True

    def save_model(self, request, obj, form, change):
        obj.updated_by = request.user
        if not obj.created_by:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)
        clear_risk_settings_cache()


@admin.register(FixtureRiskState)
class FixtureRiskStateAdmin(admin.ModelAdmin):
    list_display = ("fixture", "is_suspended", "manual_override", "suspension_reason", "updated_at")
    list_filter = ("is_suspended", "manual_override")
    search_fields = ("fixture__home_team", "fixture__away_team")
    actions = ("suspend_selected", "resume_selected", "set_override", "clear_override")

    @admin.action(description="Suspend selected fixtures")
    def suspend_selected(self, request, queryset):
        with transaction.atomic():
            updated = 0
            for obj in queryset.select_for_update():
                if obj.manual_override:
                    continue
                if not obj.is_suspended:
                    obj.is_suspended = True
                    obj.suspension_reason = obj.suspension_reason or "Manual suspension"
                    obj.suspended_by = request.user
                    obj.suspended_at = timezone.now()
                    obj.save()
                    RiskAuditLog.objects.create(
                        action_type="MANUAL_SUSPEND",
                        actor=request.user,
                        fixture=obj.fixture,
                        message=obj.suspension_reason,
                        data={"level": "fixture"},
                    )
                    updated += 1
        self.message_user(request, f"Suspended {updated} fixtures.", level=messages.SUCCESS)

    @admin.action(description="Resume selected fixtures")
    def resume_selected(self, request, queryset):
        with transaction.atomic():
            updated = 0
            for obj in queryset.select_for_update():
                if obj.manual_override:
                    continue
                if obj.is_suspended:
                    obj.is_suspended = False
                    obj.resumed_by = request.user
                    obj.resumed_at = timezone.now()
                    obj.save()
                    RiskAuditLog.objects.create(
                        action_type="MANUAL_RESUME",
                        actor=request.user,
                        fixture=obj.fixture,
                        message="Manual resume",
                        data={"level": "fixture"},
                    )
                    updated += 1
        self.message_user(request, f"Resumed {updated} fixtures.", level=messages.SUCCESS)

    @admin.action(description="Set manual override on selected fixtures")
    def set_override(self, request, queryset):
        updated = queryset.update(manual_override=True)
        self.message_user(request, f"Manual override enabled for {updated} fixtures.", level=messages.SUCCESS)

    @admin.action(description="Clear manual override on selected fixtures")
    def clear_override(self, request, queryset):
        updated = queryset.update(manual_override=False)
        self.message_user(request, f"Manual override cleared for {updated} fixtures.", level=messages.SUCCESS)


@admin.register(MarketRiskState)
class MarketRiskStateAdmin(admin.ModelAdmin):
    list_display = ("fixture", "market_key", "is_suspended", "manual_override", "updated_at")
    list_filter = ("is_suspended", "manual_override", "market_key")
    search_fields = ("fixture__home_team", "fixture__away_team", "market_key")
    actions = ("suspend_selected", "resume_selected", "set_override", "clear_override")

    @admin.action(description="Suspend selected markets")
    def suspend_selected(self, request, queryset):
        with transaction.atomic():
            updated = 0
            for obj in queryset.select_for_update():
                if obj.manual_override:
                    continue
                if not obj.is_suspended:
                    obj.is_suspended = True
                    obj.suspension_reason = obj.suspension_reason or "Manual suspension"
                    obj.suspended_by = request.user
                    obj.suspended_at = timezone.now()
                    obj.save()
                    RiskAuditLog.objects.create(
                        action_type="MANUAL_SUSPEND",
                        actor=request.user,
                        fixture=obj.fixture,
                        market_key=obj.market_key,
                        message=obj.suspension_reason,
                        data={"level": "market"},
                    )
                    updated += 1
        self.message_user(request, f"Suspended {updated} markets.", level=messages.SUCCESS)

    @admin.action(description="Resume selected markets")
    def resume_selected(self, request, queryset):
        with transaction.atomic():
            updated = 0
            for obj in queryset.select_for_update():
                if obj.manual_override:
                    continue
                if obj.is_suspended:
                    obj.is_suspended = False
                    obj.resumed_by = request.user
                    obj.resumed_at = timezone.now()
                    obj.save()
                    RiskAuditLog.objects.create(
                        action_type="MANUAL_RESUME",
                        actor=request.user,
                        fixture=obj.fixture,
                        market_key=obj.market_key,
                        message="Manual resume",
                        data={"level": "market"},
                    )
                    updated += 1
        self.message_user(request, f"Resumed {updated} markets.", level=messages.SUCCESS)

    @admin.action(description="Set manual override on selected markets")
    def set_override(self, request, queryset):
        updated = queryset.update(manual_override=True)
        self.message_user(request, f"Manual override enabled for {updated} markets.", level=messages.SUCCESS)

    @admin.action(description="Clear manual override on selected markets")
    def clear_override(self, request, queryset):
        updated = queryset.update(manual_override=False)
        self.message_user(request, f"Manual override cleared for {updated} markets.", level=messages.SUCCESS)


@admin.register(SelectionRiskState)
class SelectionRiskStateAdmin(admin.ModelAdmin):
    list_display = ("fixture", "market_key", "selection_key", "is_suspended", "manual_override", "updated_at")
    list_filter = ("is_suspended", "manual_override", "market_key")
    search_fields = ("fixture__home_team", "fixture__away_team", "selection_key")
    actions = ("suspend_selected", "resume_selected", "set_override", "clear_override")

    @admin.action(description="Suspend selected selections")
    def suspend_selected(self, request, queryset):
        with transaction.atomic():
            updated = 0
            for obj in queryset.select_for_update():
                if obj.manual_override:
                    continue
                if not obj.is_suspended:
                    obj.is_suspended = True
                    obj.suspension_reason = obj.suspension_reason or "Manual suspension"
                    obj.suspended_by = request.user
                    obj.suspended_at = timezone.now()
                    obj.save()
                    RiskAuditLog.objects.create(
                        action_type="MANUAL_SUSPEND",
                        actor=request.user,
                        fixture=obj.fixture,
                        market_key=obj.market_key,
                        selection_key=obj.selection_key,
                        message=obj.suspension_reason,
                        data={"level": "selection"},
                    )
                    updated += 1
        self.message_user(request, f"Suspended {updated} selections.", level=messages.SUCCESS)

    @admin.action(description="Resume selected selections")
    def resume_selected(self, request, queryset):
        with transaction.atomic():
            updated = 0
            for obj in queryset.select_for_update():
                if obj.manual_override:
                    continue
                if obj.is_suspended:
                    obj.is_suspended = False
                    obj.resumed_by = request.user
                    obj.resumed_at = timezone.now()
                    obj.save()
                    RiskAuditLog.objects.create(
                        action_type="MANUAL_RESUME",
                        actor=request.user,
                        fixture=obj.fixture,
                        market_key=obj.market_key,
                        selection_key=obj.selection_key,
                        message="Manual resume",
                        data={"level": "selection"},
                    )
                    updated += 1
        self.message_user(request, f"Resumed {updated} selections.", level=messages.SUCCESS)

    @admin.action(description="Set manual override on selected selections")
    def set_override(self, request, queryset):
        updated = queryset.update(manual_override=True)
        self.message_user(request, f"Manual override enabled for {updated} selections.", level=messages.SUCCESS)

    @admin.action(description="Clear manual override on selected selections")
    def clear_override(self, request, queryset):
        updated = queryset.update(manual_override=False)
        self.message_user(request, f"Manual override cleared for {updated} selections.", level=messages.SUCCESS)


@admin.register(FixtureLiabilitySnapshot)
class FixtureLiabilitySnapshotAdmin(admin.ModelAdmin):
    list_display = ("fixture", "total_stake", "total_potential_payout", "net_exposure", "ticket_count", "risk_score", "updated_at")
    search_fields = ("fixture__home_team", "fixture__away_team")
    list_filter = ("risk_score",)

    def has_add_permission(self, request):
        return False


@admin.register(SelectionLiabilitySnapshot)
class SelectionLiabilitySnapshotAdmin(admin.ModelAdmin):
    list_display = ("fixture", "market_key", "selection_key", "total_stake", "total_potential_payout", "ticket_count", "risk_score", "updated_at")
    search_fields = ("fixture__home_team", "fixture__away_team", "selection_key")
    list_filter = ("market_key", "risk_score")

    def has_add_permission(self, request):
        return False


@admin.register(MarketLiabilitySnapshot)
class MarketLiabilitySnapshotAdmin(admin.ModelAdmin):
    list_display = (
        "fixture",
        "market_key",
        "total_stake",
        "total_potential_payout",
        "net_exposure",
        "ticket_count",
        "risk_score",
        "updated_at",
    )
    search_fields = ("fixture__home_team", "fixture__away_team", "market_key")
    list_filter = ("market_key", "risk_score")

    def has_add_permission(self, request):
        return False


@admin.register(AgentExposureSnapshot)
class AgentExposureSnapshotAdmin(admin.ModelAdmin):
    list_display = ("agent", "total_stake_today", "total_potential_payout_today", "ticket_count_today", "updated_at")
    search_fields = ("agent__email", "agent__username")

    def has_add_permission(self, request):
        return False


@admin.register(UserExposureSnapshot)
class UserExposureSnapshotAdmin(admin.ModelAdmin):
    list_display = ("user", "total_stake_today", "total_potential_payout_today", "ticket_count_today", "updated_at")
    search_fields = ("user__email", "user__username")

    def has_add_permission(self, request):
        return False


@admin.register(BettingPeriodLiabilitySnapshot)
class BettingPeriodLiabilitySnapshotAdmin(admin.ModelAdmin):
    list_display = ("betting_period", "total_stake", "total_potential_payout", "net_exposure", "ticket_count", "risk_score", "updated_at")
    search_fields = ("betting_period__name",)
    list_filter = ("risk_score",)

    def has_add_permission(self, request):
        return False


@admin.register(IPWhitelistEntry)
class IPWhitelistEntryAdmin(admin.ModelAdmin):
    list_display = ("ip_address", "is_active", "reason", "created_at")
    list_filter = ("is_active",)
    search_fields = ("ip_address", "reason")


@admin.register(IPIntelligence)
class IPIntelligenceAdmin(admin.ModelAdmin):
    list_display = ("ip_address", "risk_score", "is_vpn", "is_proxy", "is_tor", "is_datacenter", "checked_at", "updated_at")
    list_filter = ("is_vpn", "is_proxy", "is_tor", "is_datacenter")
    search_fields = ("ip_address",)
    readonly_fields = ("updated_at",)

@admin.register(RiskAuditLog)
class RiskAuditLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "action_type", "actor", "fixture", "market_key", "selection_key")
    list_filter = ("action_type", "created_at")
    search_fields = ("message", "actor__email", "fixture__home_team", "fixture__away_team", "market_key", "selection_key")
    readonly_fields = [f.name for f in RiskAuditLog._meta.fields]
    date_hierarchy = "created_at"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(SuspiciousActivityLog)
class SuspiciousActivityLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "kind", "risk_score", "user", "ticket")
    list_filter = ("kind", "risk_score", "created_at")
    search_fields = ("user__email", "user__username", "ticket__ticket_id", "data")
    readonly_fields = [f.name for f in SuspiciousActivityLog._meta.fields]
    date_hierarchy = "created_at"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(SharpBettorProfile)
class SharpBettorProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "is_flagged", "win_rate", "roi", "yield_percent", "ticket_count", "last_calculated_at")
    list_filter = ("is_flagged",)
    search_fields = ("user__email", "user__username")
    readonly_fields = ("last_calculated_at", "updated_at")


@admin.register(DeviceFingerprint)
class DeviceFingerprintAdmin(admin.ModelAdmin):
    list_display = ("user", "fingerprint_hash", "ip_address", "last_seen_at")
    list_filter = ("last_seen_at",)
    search_fields = ("user__email", "user__username", "fingerprint_hash", "ip_address")
    readonly_fields = ("created_at",)


@admin.register(SyndicateGroup)
class SyndicateGroupAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "risk_score", "is_active", "updated_at")
    list_filter = ("is_active",)
    search_fields = ("name", "reason")


@admin.register(SyndicateMember)
class SyndicateMemberAdmin(admin.ModelAdmin):
    list_display = ("group", "user", "role", "joined_at")
    search_fields = ("group__name", "user__email", "user__username")


@admin.register(DuplicateTicketLog)
class DuplicateTicketLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "user", "ticket", "signature", "ip_address")
    list_filter = ("created_at",)
    search_fields = ("user__email", "user__username", "signature", "ticket__ticket_id", "ip_address")
    readonly_fields = [f.name for f in DuplicateTicketLog._meta.fields]
    date_hierarchy = "created_at"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(ArbitrageAlert)
class ArbitrageAlertAdmin(admin.ModelAdmin):
    list_display = ("created_at", "user", "ticket", "risk_score")
    list_filter = ("created_at", "risk_score")
    search_fields = ("user__email", "user__username", "ticket__ticket_id")
    readonly_fields = [f.name for f in ArbitrageAlert._meta.fields]
    date_hierarchy = "created_at"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
