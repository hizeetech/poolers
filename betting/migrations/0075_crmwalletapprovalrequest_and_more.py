from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('betting', '0074_alter_creditrequest_request_type_and_more'),
    ]

    operations = [
        migrations.CreateModel(
            name='CRMWalletApprovalRequest',
            fields=[],
            options={
                'verbose_name': 'CRM Wallet Approval Request',
                'verbose_name_plural': 'CRM Wallet Approval Requests',
                'proxy': True,
                'indexes': [],
                'constraints': [],
            },
            bases=('betting.creditrequest',),
        ),
    ]
