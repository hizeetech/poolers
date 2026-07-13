from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("betting", "0089_siteconfiguration_show_agent_pending_commission_card"),
    ]

    operations = [
        migrations.AddField(
            model_name="fixture",
            name="away_or_draw_odd",
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=5, null=True),
        ),
        migrations.AddField(
            model_name="fixture",
            name="either_team_win_odd",
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=5, null=True),
        ),
        migrations.AddField(
            model_name="fixture",
            name="home_or_draw_odd",
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=5, null=True),
        ),
    ]
