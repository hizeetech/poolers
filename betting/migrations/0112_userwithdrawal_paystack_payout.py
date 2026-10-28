from django.db import migrations, models


class Migration(migrations.Migration):
    """Adds the Paystack payout fields to UserWithdrawal.

    All fields are additive and nullable/blank, so existing withdrawal rows are
    untouched: payout_status stays empty, meaning "never sent to Paystack".
    """

    dependencies = [
        ('betting', '0111_email_notification_governance'),
    ]

    operations = [
        migrations.AddField(
            model_name='userwithdrawal',
            name='bank_code',
            field=models.CharField(
                blank=True,
                default='',
                help_text='Paystack bank code, e.g. 057 = Zenith Bank. Required to send to bank.',
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name='userwithdrawal',
            name='payout_status',
            field=models.CharField(
                blank=True,
                choices=[
                    ('', 'Not sent to bank'),
                    ('processing', 'Processing'),
                    ('success', 'Sent successfully'),
                    ('failed', 'Failed - held for review'),
                    ('reversed', 'Reversed by provider'),
                ],
                db_index=True,
                default='',
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name='userwithdrawal',
            name='payout_reference',
            field=models.CharField(
                blank=True,
                help_text='Paystack transfer reference. Guarantees a withdrawal cannot be paid twice.',
                max_length=100,
                null=True,
                unique=True,
            ),
        ),
        migrations.AddField(
            model_name='userwithdrawal',
            name='payout_recipient_code',
            field=models.CharField(blank=True, default='', max_length=100),
        ),
        migrations.AddField(
            model_name='userwithdrawal',
            name='payout_error',
            field=models.TextField(blank=True, default=''),
        ),
        migrations.AddField(
            model_name='userwithdrawal',
            name='payout_attempts',
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name='userwithdrawal',
            name='paid_at',
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
    ]
