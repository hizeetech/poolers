from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("commission", "0007_weeklyagentcommission_split_ggr_commissions"),
    ]

    operations = [
        migrations.AddField(
            model_name="commissionperiod",
            name="is_active",
            field=models.BooleanField(default=True),
        ),
    ]

