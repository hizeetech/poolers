from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('betting', '0073_userwithdrawal_separate_approval_completion_email_flags'),
    ]

    operations = [
        migrations.AlterField(
            model_name='creditrequest',
            name='request_type',
            field=models.CharField(
                choices=[
                    ('credit', 'Normal Credit'),
                    ('loan', 'Loan'),
                    ('crm_credit', 'CRM Credit Approval'),
                    ('crm_debit', 'CRM Debit Approval'),
                ],
                default='credit',
                max_length=20,
            ),
        ),
        migrations.AlterField(
            model_name='crmactionlog',
            name='action_type',
            field=models.CharField(
                choices=[
                    ('WITHDRAWAL_APPROVED', 'Withdrawal Approved'),
                    ('WITHDRAWAL_REJECTED', 'Withdrawal Rejected'),
                    ('USER_SUSPENDED', 'User Suspended'),
                    ('USER_UNSUSPENDED', 'User Unsuspended'),
                    ('PROFILE_EDITED', 'Profile Edited'),
                    ('WITHDRAWAL_FROZEN', 'Withdrawals Frozen'),
                    ('WITHDRAWAL_UNFROZEN', 'Withdrawals Unfrozen'),
                    ('WALLET_CREDITED', 'Wallet Credited'),
                    ('WALLET_DEBITED', 'Wallet Debited'),
                    ('WALLET_CREDIT_REQUESTED', 'Wallet Credit Requested'),
                    ('WALLET_DEBIT_REQUESTED', 'Wallet Debit Requested'),
                    ('PASSWORD_RESET', 'Password Reset'),
                    ('MESSAGE_SENT', 'Message Sent'),
                    ('VIP_UPDATED', 'VIP/KYC Updated'),
                    ('CASHIER_REG_APPROVED', 'Cashier Registration Approved'),
                    ('CASHIER_REG_REJECTED', 'Cashier Registration Rejected'),
                    ('AGENT_REG_APPROVED', 'Agent Registration Approved'),
                    ('AGENT_REG_REJECTED', 'Agent Registration Rejected'),
                ],
                db_index=True,
                max_length=50,
            ),
        ),
    ]
