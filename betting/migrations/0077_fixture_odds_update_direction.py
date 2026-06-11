from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("betting", "0076_fixture_update_flags"),
    ]

    operations = [
        migrations.AddField(
            model_name="fixture",
            name="odds_update_direction",
            field=models.CharField(blank=True, choices=[("up", "Up"), ("down", "Down"), ("mixed", "Mixed")], default="", max_length=10),
        ),
    ]
