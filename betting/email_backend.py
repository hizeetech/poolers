"""
Guarded SMTP backend.

This is the SINGLE choke point for every outbound message in the project
(Django `send_mail`, `EmailMessage`, `EmailMultiAlternatives`, error mails,
anything). Routing EMAIL_BACKEND through it means the Site Configuration
master switch, suppression list, allowlist and address validation apply to
100% of mail without editing a single call site.

Behaviour is identical to django.core.mail.backends.smtp.EmailBackend except
that recipients are pre-filtered; if anything goes wrong during filtering the
original messages are sent untouched (fail-open, never lose notifications).
"""

import logging

from django.core.mail.backends.smtp import EmailBackend as _SMTPEmailBackend

from .email_prefs import filter_with_policy, load_policy

logger = logging.getLogger(__name__)


class GuardedEmailBackend(_SMTPEmailBackend):
    def send_messages(self, email_messages):
        if not email_messages:
            return 0

        allowed = list(email_messages)
        try:
            # One SiteConfiguration read for the whole batch, not one per message.
            master_on, suppressed, allowed_set = load_policy()
            if not master_on:
                logger.warning(
                    "GuardedEmailBackend: master switch OFF - dropped %d message(s)",
                    len(email_messages),
                )
                return 0

            def _filter(addresses):
                return filter_with_policy(addresses, suppressed, allowed_set)

            filtered = []
            for msg in email_messages:
                to = _filter(getattr(msg, "to", None) or [])
                to_keys = {a.lower() for a in to}

                cc = [a for a in _filter(getattr(msg, "cc", None) or []) if a.lower() not in to_keys]
                cc_keys = {a.lower() for a in cc}

                blocked = to_keys | cc_keys
                bcc = [a for a in _filter(getattr(msg, "bcc", None) or []) if a.lower() not in blocked]

                if not (to or cc or bcc):
                    logger.warning(
                        "GuardedEmailBackend: dropped message %r - no deliverable recipients left "
                        "(original: to=%s cc=%s)",
                        getattr(msg, "subject", ""),
                        getattr(msg, "to", None),
                        getattr(msg, "cc", None),
                    )
                    continue

                msg.to = to
                try:
                    msg.cc = cc
                except Exception:
                    pass
                try:
                    msg.bcc = bcc
                except Exception:
                    pass
                filtered.append(msg)

            allowed = filtered
        except Exception:
            logger.exception("GuardedEmailBackend: recipient filtering failed - sending unmodified")
            allowed = list(email_messages)

        if not allowed:
            return 0
        return super().send_messages(allowed)
