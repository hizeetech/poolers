from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('betting', '0065_alter_financeauditlog_action_type_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='userwithdrawal',
            name='email_request_admin_sent_at',
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name='userwithdrawal',
            name='email_request_user_sent_at',
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name='userwithdrawal',
            name='email_rejected_admin_sent_at',
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name='userwithdrawal',
            name='email_rejected_user_sent_at',
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name='userwithdrawal',
            name='email_success_admin_sent_at',
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name='userwithdrawal',
            name='email_success_user_sent_at',
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name='userwithdrawal',
            name='last_email_error',
            field=models.TextField(blank=True, default=''),
        ),
    ]

