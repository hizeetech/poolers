# Email notification governance: master kill switch, recipient safety lists,
# and 37 per-notification enable/disable toggles on SiteConfiguration.
#
# All toggles default to True so applying this migration changes NOTHING until
# an admin actively switches a notification off.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('betting', '0110_fixture_disabled_for_betting'),
    ]

    operations = [
        # --- Safety / global controls ------------------------------------
        migrations.AddField(
            model_name='siteconfiguration',
            name='email_notifications_enabled',
            field=models.BooleanField(
                default=True,
                verbose_name='Outbound Email — MASTER SWITCH (uncheck to stop ALL email)',
                help_text='Kill switch. While this is UNCHECKED nothing is emailed at all, no matter what the per-notification toggles below say. Use this for emergencies or when the host threatens suspension for failed deliveries.',
            ),
        ),
        migrations.AddField(
            model_name='siteconfiguration',
            name='email_recipient_allowlist',
            field=models.TextField(
                blank=True,
                default='',
                verbose_name='Recipient Allowlist (optional hard lock)',
                help_text='Comma-separated. If filled, mail is ONLY ever delivered to these addresses and every other recipient is silently dropped everywhere on the site. Leave blank to allow all recipients.',
            ),
        ),
        migrations.AddField(
            model_name='siteconfiguration',
            name='email_suppression_list',
            field=models.TextField(
                blank=True,
                default='',
                verbose_name='Suppressed (bounced / dead) Addresses',
                help_text='Comma-separated. These addresses are NEVER emailed by any notification, in any role. Add every address that hard-bounces here to protect the sending domain reputation.',
            ),
        ),
        # --- Administrators ----------------------------------------------
        migrations.AddField(
            model_name='siteconfiguration',
            name='email_admin_deposit_initiated',
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name='siteconfiguration',
            name='email_admin_deposit_successful',
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name='siteconfiguration',
            name='email_admin_deposit_failed',
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name='siteconfiguration',
            name='email_admin_withdrawal_requested',
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name='siteconfiguration',
            name='email_admin_withdrawal_approved',
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name='siteconfiguration',
            name='email_admin_withdrawal_completed',
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name='siteconfiguration',
            name='email_admin_withdrawal_rejected',
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name='siteconfiguration',
            name='email_admin_stuck_deposit_alert',
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name='siteconfiguration',
            name='email_admin_new_cashier_request',
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name='siteconfiguration',
            name='email_admin_crm_daily_report',
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name='siteconfiguration',
            name='email_admin_retail_daily_report',
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name='siteconfiguration',
            name='email_admin_fixture_odds_proposal',
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name='siteconfiguration',
            name='email_admin_uip_alerts',
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name='siteconfiguration',
            name='email_admin_finance_scheduled_report',
            field=models.BooleanField(default=True),
        ),
        # --- Players / non-agent users ------------------------------------
        migrations.AddField(
            model_name='siteconfiguration',
            name='email_user_deposit_initiated',
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name='siteconfiguration',
            name='email_user_deposit_successful',
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name='siteconfiguration',
            name='email_user_deposit_failed',
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name='siteconfiguration',
            name='email_user_withdrawal_requested',
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name='siteconfiguration',
            name='email_user_withdrawal_approved',
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name='siteconfiguration',
            name='email_user_withdrawal_completed',
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name='siteconfiguration',
            name='email_user_withdrawal_rejected',
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name='siteconfiguration',
            name='email_user_password_reset',
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name='siteconfiguration',
            name='email_user_account_credentials',
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name='siteconfiguration',
            name='email_user_crm_direct_message',
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name='siteconfiguration',
            name='email_user_crm_campaign',
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name='siteconfiguration',
            name='email_user_crm_password_reset',
            field=models.BooleanField(default=True),
        ),
        # --- Agents / super agents / master agents ------------------------
        migrations.AddField(
            model_name='siteconfiguration',
            name='email_agent_transaction_cc',
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name='siteconfiguration',
            name='email_agent_cashier_registration_approved',
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name='siteconfiguration',
            name='email_agent_registration_approved',
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name='siteconfiguration',
            name='email_agent_registration_rejected',
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name='siteconfiguration',
            name='email_agent_credentials_resend',
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name='siteconfiguration',
            name='email_agent_commission_profile_assigned',
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name='siteconfiguration',
            name='email_agent_commission_recalled',
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name='siteconfiguration',
            name='email_agent_loan_overdraft_events',
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name='siteconfiguration',
            name='email_agent_unlock_appeal_resolution',
            field=models.BooleanField(default=True),
        ),
        # --- Cashiers ------------------------------------------------------
        migrations.AddField(
            model_name='siteconfiguration',
            name='email_cashier_withdrawal_updates',
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name='siteconfiguration',
            name='email_cashier_crm_messages',
            field=models.BooleanField(default=True),
        ),
    ]
