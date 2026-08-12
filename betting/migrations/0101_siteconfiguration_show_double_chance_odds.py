from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("betting", "0100_cashout_pricing_controls"),
    ]

    operations = [
        migrations.AddField(
            model_name="siteconfiguration",
            name="show_double_chance_odds",
            field=models.BooleanField(
                default=False,
                help_text="If enabled, Double Chance odds (1X / 12 / X2) are shown on the frontend fixtures page in a collapsible 'More Markets' sub-row below the 1X2 row.",
            ),
        ),
    ]
