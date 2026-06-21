from django.core.validators import RegexValidator
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("betting", "0086_tickettransactionledger"),
    ]

    operations = [
        migrations.AddField(
            model_name="bettingperiod",
            name="fixture_theme_color",
            field=models.CharField(
                default="#0b4f3a",
                help_text="Hex color used for fixture page section headers for this betting period.",
                max_length=7,
                validators=[
                    RegexValidator(
                        message="Enter a valid hex color in the format #RRGGBB.",
                        regex="^#[0-9A-Fa-f]{6}$",
                    )
                ],
            ),
        ),
    ]
