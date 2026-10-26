"""
Central governance for outbound email notifications.

WHY THIS EXISTS
    Email volume grew until the sending domain was being suspended by the host
    (Namecheap) for too many failed deliveries. Root cause: mail was being sent
    to unreachable / non-functioning addresses with no way to switch a
    notification off without a code deploy.

TWO ENFORCEMENT LAYERS
    Layer 1 (universal, zero call-site changes)
        `load_policy()` + `filter_with_policy()` are applied inside
        `betting.email_backend.GuardedEmailBackend` to EVERY outbound message
        produced by ANY code path. They enforce:
            * the master kill switch
            * the suppression list (known hard-bounce addresses)
            * the recipient allowlist (optional hard lock)
            * address syntax validation
            * de-duplication across TO / CC / BCC

    Layer 2 (granular, opt-in per notification)
        `email_enabled("<field_name>")` is called at each notification site so
        the admin can silence one specific notification type without touching
        any other.

FAIL-OPEN POLICY
    If SiteConfiguration cannot be read (DB hiccup, missing row, unexpected
    exception) every gate returns its default and mail proceeds exactly as it
    did before this module existed. Notification loss is worse than a failed
    send, and the pre-existing behaviour is preserved.
"""

import re

MASTER_FIELD = "email_notifications_enabled"
ALLOWLIST_FIELD = "email_recipient_allowlist"
SUPPRESSION_FIELD = "email_suppression_list"

# ---------------------------------------------------------------------------
# TOGGLE REGISTRY
# Each entry is (field_name, verbose_name, default).
# Keeping the defaults at True means installing this module changes NOTHING
# until an admin actively unticks a box.
# ---------------------------------------------------------------------------

ADMIN_TOGGLES = [
    ("email_admin_deposit_initiated", "Admin -> Deposit Initiated"),
    ("email_admin_deposit_successful", "Admin -> Deposit Successful"),
    ("email_admin_deposit_failed", "Admin -> Deposit Failed"),
    ("email_admin_withdrawal_requested", "Admin -> Withdrawal Requested"),
    ("email_admin_withdrawal_approved", "Admin -> Withdrawal Approved"),
    ("email_admin_withdrawal_completed", "Admin -> Withdrawal Completed"),
    ("email_admin_withdrawal_rejected", "Admin -> Withdrawal Rejected"),
    ("email_admin_stuck_deposit_alert", "Admin -> Stuck Deposit Alert"),
    ("email_admin_new_cashier_request", "Admin -> New Cashier Request"),
    ("email_admin_crm_daily_report", "Admin -> CRM Daily Report"),
    ("email_admin_retail_daily_report", "Admin -> Retail Daily Report"),
    ("email_admin_fixture_odds_proposal", "Admin -> Fixture Odds Proposal"),
    ("email_admin_uip_alerts", "Admin -> UIP Alerts"),
    ("email_admin_finance_scheduled_report", "Admin -> Finance Scheduled Report"),
]

USER_TOGGLES = [
    ("email_user_deposit_initiated", "User -> Deposit Initiated"),
    ("email_user_deposit_successful", "User -> Deposit Successful"),
    ("email_user_deposit_failed", "User -> Deposit Failed"),
    ("email_user_withdrawal_requested", "User -> Withdrawal Requested"),
    ("email_user_withdrawal_approved", "User -> Withdrawal Approved"),
    ("email_user_withdrawal_completed", "User -> Withdrawal Completed"),
    ("email_user_withdrawal_rejected", "User -> Withdrawal Rejected"),
    ("email_user_password_reset", "User -> Password Reset Link"),
    ("email_user_account_credentials", "User -> New Account Credentials"),
    ("email_user_crm_direct_message", "User -> CRM Direct Message"),
    ("email_user_crm_campaign", "User -> CRM Campaign"),
    ("email_user_crm_password_reset", "User -> CRM Password Reset"),
]

AGENT_TOGGLES = [
    ("email_agent_transaction_cc", "Agent -> Deposit/Withdrawal Copy (CC)"),
    ("email_agent_cashier_registration_approved", "Agent -> Cashier Registration Approved"),
    ("email_agent_registration_approved", "Agent -> Registration Approved"),
    ("email_agent_registration_rejected", "Agent -> Registration Rejected"),
    ("email_agent_credentials_resend", "Agent -> Credentials Resent"),
    ("email_agent_commission_profile_assigned", "Agent -> Commission Profile Assigned"),
    ("email_agent_commission_recalled", "Agent -> Commission Recalled"),
    ("email_agent_loan_overdraft_events", "Agent -> Loan / Overdraft Events"),
    ("email_agent_unlock_appeal_resolution", "Agent -> Unlock Appeal Resolution"),
]

CASHIER_TOGGLES = [
    ("email_cashier_withdrawal_updates", "Cashier -> Withdrawal Updates"),
    ("email_cashier_crm_messages", "Cashier -> CRM Messages"),
]

SAFETY_FIELDS = [
    (MASTER_FIELD, "MASTER SWITCH - Stop ALL outbound email"),
    (ALLOWLIST_FIELD, "Recipient Allowlist (optional hard lock)"),
    (SUPPRESSION_FIELD, "Suppressed (bounced / dead) addresses"),
]

ALL_TOGGLE_FIELDS = [
    name for name, _label in (ADMIN_TOGGLES + USER_TOGGLES + AGENT_TOGGLES + CASHIER_TOGGLES)
]

AGENT_ROLE_TYPES = {"agent", "super_agent", "master_agent"}

# Pragmatic syntax check: local@domain.tld with no spaces / separators.
_ADDRESS_RE = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[A-Za-z]{2,}$")


def _config():
    """Return the SiteConfiguration singleton, or None if it cannot be read."""
    try:
        from .models import SiteConfiguration

        return SiteConfiguration.load()
    except Exception:
        return None


def email_enabled(field_name, default=True):
    """
    True if the named notification toggle is switched on.

    `field_name` is one of the names registered in this module, e.g.
    'email_user_withdrawal_approved'. Unknown names return the default so a
    typo can never silently swallow mail.
    """
    if not field_name:
        return default
    cfg = _config()
    if cfg is None:
        return default
    return bool(getattr(cfg, field_name, default))


def parse_addresses(raw):
    """
    Split a comma / semicolon / newline separated blob into a clean,
    order-preserving, de-duplicated address list WITHOUT validating syntax.
    Used for the suppression list and the allowlist, which may legitimately
    contain typo'd addresses that we want to match and block.
    """
    if not raw:
        return []
    out = []
    seen = set()
    for chunk in re.split(r"[,;\s]+", str(raw)):
        addr = chunk.strip().strip("<>").strip()
        if not addr:
            continue
        key = addr.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(addr)
    return out


def is_sendable_address(address):
    """Syntax check only - no allow/suppression policy applied."""
    return bool(address and _ADDRESS_RE.match(str(address).strip()))


def load_policy():
    """
    Read the three global gates ONCE, so a whole batch of messages costs a
    single SiteConfiguration read instead of one read per recipient.

    Returns (master_on, suppressed_set, allowed_set_or_None).
    Any failure returns the fail-open defaults: mail allowed, nothing
    suppressed, no allowlist.
    """
    cfg = _config()
    if cfg is None:
        return True, set(), None

    master_on = bool(getattr(cfg, MASTER_FIELD, True))
    suppressed = {a.lower() for a in parse_addresses(getattr(cfg, SUPPRESSION_FIELD, ""))}
    allowed = parse_addresses(getattr(cfg, ALLOWLIST_FIELD, ""))
    allowed_set = {a.lower() for a in allowed} if allowed else None
    return master_on, suppressed, allowed_set


def filter_with_policy(addresses, suppressed, allowed_set):
    """
    Layer-1 gate. Returns the subset of *addresses* that is safe to send to,
    given a policy already loaded by `load_policy()`.

    Drops:
        * blank / malformed addresses
        * addresses on the suppression list
        * addresses outside the allowlist (when an allowlist is configured)
        * repeats (case-insensitive)
    """
    if not addresses:
        return []

    out = []
    seen = set()
    for raw in addresses:
        addr = str(raw or "").strip().strip("<>").strip()
        if not addr:
            continue
        key = addr.lower()
        if key in seen:
            continue
        if not is_sendable_address(addr):
            continue
        if key in suppressed:
            continue
        if allowed_set is not None and key not in allowed_set:
            continue
        seen.add(key)
        out.append(addr)
    return out


# ---------------------------------------------------------------------------
# ROLE-AWARE KEY RESOLUTION
# ---------------------------------------------------------------------------

def is_cashier(user):
    return bool(user) and (getattr(user, "user_type", "") or "").strip().lower() == "cashier"


def is_agent_family(user):
    return bool(user) and (getattr(user, "user_type", "") or "").strip().lower() in AGENT_ROLE_TYPES


def withdrawal_user_key(user, event):
    """
    Toggle for the withdrawal notification delivered to the transacting user.
    Cashiers have their own switch so their mail can be muted independently.
    """
    event = (event or "").strip().lower()
    if is_cashier(user):
        return "email_cashier_withdrawal_updates"
    return f"email_user_withdrawal_{event}"


def deposit_user_key(user, event):
    event = (event or "").strip().lower()
    return f"email_user_deposit_{event}"


def withdrawal_admin_key(event):
    event = (event or "").strip().lower()
    return f"email_admin_withdrawal_{event}"


def deposit_admin_key(event):
    event = (event or "").strip().lower()
    return f"email_admin_deposit_{event}"


def crm_message_key(recipient):
    """CRM direct message / campaign target - cashiers are grouped separately."""
    if is_cashier(recipient):
        return "email_cashier_crm_messages"
    return "email_user_crm_direct_message"
