from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("betting", "0096_remove_cashoutsettings_cashout_percent_1_win_and_more"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="cashoutsettings",
            name="disable_cash_out_during_live_events",
        ),
    ]
