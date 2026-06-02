from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('commission', '0006_commissionrecall_commissionrecallapproval_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='weeklyagentcommission',
            name='single_stake',
            field=models.DecimalField(decimal_places=2, default=0, max_digits=12),
        ),
        migrations.AddField(
            model_name='weeklyagentcommission',
            name='single_winnings',
            field=models.DecimalField(decimal_places=2, default=0, max_digits=12),
        ),
        migrations.AddField(
            model_name='weeklyagentcommission',
            name='single_ggr',
            field=models.DecimalField(decimal_places=2, default=0, max_digits=12),
        ),
        migrations.AddField(
            model_name='weeklyagentcommission',
            name='multiple_stake',
            field=models.DecimalField(decimal_places=2, default=0, max_digits=12),
        ),
        migrations.AddField(
            model_name='weeklyagentcommission',
            name='multiple_winnings',
            field=models.DecimalField(decimal_places=2, default=0, max_digits=12),
        ),
        migrations.AddField(
            model_name='weeklyagentcommission',
            name='multiple_ggr',
            field=models.DecimalField(decimal_places=2, default=0, max_digits=12),
        ),
        migrations.AddField(
            model_name='weeklyagentcommission',
            name='commission_single_amount',
            field=models.DecimalField(decimal_places=2, default=0.0, max_digits=12),
        ),
        migrations.AddField(
            model_name='weeklyagentcommission',
            name='commission_multiple_amount',
            field=models.DecimalField(decimal_places=2, default=0.0, max_digits=12),
        ),
    ]
