from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('betting', '0072_walletledgerentry'),
    ]

    operations = [
        migrations.AddField(
            model_name='userwithdrawal',
            name='email_approved_admin_sent_at',
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name='userwithdrawal',
            name='email_approved_user_sent_at',
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name='userwithdrawal',
            name='email_completed_admin_sent_at',
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name='userwithdrawal',
            name='email_completed_user_sent_at',
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
    ]
