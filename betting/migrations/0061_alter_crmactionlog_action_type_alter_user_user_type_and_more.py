import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('betting', '0060_user_kyc_status_user_vip_level_user_vip_manager'),
    ]

    operations = [
        migrations.AlterField(
            model_name='crmactionlog',
            name='action_type',
            field=models.CharField(choices=[('WITHDRAWAL_APPROVED', 'Withdrawal Approved'), ('WITHDRAWAL_REJECTED', 'Withdrawal Rejected'), ('USER_SUSPENDED', 'User Suspended'), ('USER_UNSUSPENDED', 'User Unsuspended'), ('PROFILE_EDITED', 'Profile Edited'), ('WITHDRAWAL_FROZEN', 'Withdrawals Frozen'), ('WITHDRAWAL_UNFROZEN', 'Withdrawals Unfrozen'), ('WALLET_CREDITED', 'Wallet Credited'), ('WALLET_DEBITED', 'Wallet Debited'), ('PASSWORD_RESET', 'Password Reset'), ('MESSAGE_SENT', 'Message Sent'), ('VIP_UPDATED', 'VIP/KYC Updated'), ('CASHIER_REG_APPROVED', 'Cashier Registration Approved'), ('CASHIER_REG_REJECTED', 'Cashier Registration Rejected'), ('AGENT_REG_APPROVED', 'Agent Registration Approved'), ('AGENT_REG_REJECTED', 'Agent Registration Rejected')], db_index=True, max_length=50),
        ),
        migrations.AlterField(
            model_name='user',
            name='user_type',
            field=models.CharField(choices=[('player', 'Player'), ('cashier', 'Cashier'), ('agent', 'Agent'), ('super_agent', 'Super Agent'), ('master_agent', 'Master Agent'), ('retail_manager', 'Retail Manager'), ('account_user', 'Account User'), ('crm', 'CRM'), ('admin', 'Admin')], default='player', max_length=20),
        ),
        migrations.CreateModel(
            name='RetailManagerAgentMapping',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True, db_index=True)),
                ('agent', models.ForeignKey(limit_choices_to={'user_type': 'agent'}, on_delete=django.db.models.deletion.CASCADE, related_name='mapped_to_retail_managers_as_agent', to=settings.AUTH_USER_MODEL)),
                ('retail_manager', models.ForeignKey(limit_choices_to={'user_type': 'retail_manager'}, on_delete=django.db.models.deletion.CASCADE, related_name='mapped_agents', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'verbose_name': 'Retail Manager → Agent Mapping',
                'verbose_name_plural': 'Retail Manager → Agent Mappings',
                'unique_together': {('retail_manager', 'agent')},
            },
        ),
        migrations.CreateModel(
            name='RetailManagerMasterAgentMapping',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True, db_index=True)),
                ('master_agent', models.ForeignKey(limit_choices_to={'user_type': 'master_agent'}, on_delete=django.db.models.deletion.CASCADE, related_name='mapped_to_retail_managers', to=settings.AUTH_USER_MODEL)),
                ('retail_manager', models.ForeignKey(limit_choices_to={'user_type': 'retail_manager'}, on_delete=django.db.models.deletion.CASCADE, related_name='mapped_master_agents', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'verbose_name': 'Retail Manager → Master Agent Mapping',
                'verbose_name_plural': 'Retail Manager → Master Agent Mappings',
                'unique_together': {('retail_manager', 'master_agent')},
            },
        ),
        migrations.CreateModel(
            name='RetailManagerSuperAgentMapping',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True, db_index=True)),
                ('retail_manager', models.ForeignKey(limit_choices_to={'user_type': 'retail_manager'}, on_delete=django.db.models.deletion.CASCADE, related_name='mapped_super_agents', to=settings.AUTH_USER_MODEL)),
                ('super_agent', models.ForeignKey(limit_choices_to={'user_type': 'super_agent'}, on_delete=django.db.models.deletion.CASCADE, related_name='mapped_to_retail_managers_as_super', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'verbose_name': 'Retail Manager → Super Agent Mapping',
                'verbose_name_plural': 'Retail Manager → Super Agent Mappings',
                'unique_together': {('retail_manager', 'super_agent')},
            },
        ),
    ]
