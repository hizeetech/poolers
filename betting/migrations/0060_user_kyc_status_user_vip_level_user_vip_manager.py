import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('betting', '0059_crm_dashboard_rbac'),
    ]

    operations = [
        migrations.AddField(
            model_name='user',
            name='kyc_status',
            field=models.CharField(choices=[('unverified', 'Unverified'), ('pending', 'Pending'), ('verified', 'Verified'), ('rejected', 'Rejected')], db_index=True, default='unverified', max_length=20),
        ),
        migrations.AddField(
            model_name='user',
            name='vip_level',
            field=models.CharField(choices=[('standard', 'Standard'), ('vip1', 'VIP 1'), ('vip2', 'VIP 2'), ('vip3', 'VIP 3')], db_index=True, default='standard', max_length=20),
        ),
        migrations.AddField(
            model_name='user',
            name='vip_manager',
            field=models.ForeignKey(blank=True, limit_choices_to={'user_type': 'crm'}, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='vip_customers', to=settings.AUTH_USER_MODEL),
        ),
    ]
