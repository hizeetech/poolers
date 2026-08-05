from decimal import Decimal

import django.core.validators
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("betting", "0099_wallet_bonus_resident_and_ticket_stake_split"),
    ]

    operations = [
        migrations.AddField(
            model_name="cashoutsettings",
            name="cash_out_scaling_factor",
            field=models.DecimalField(
                decimal_places=4,
                default=Decimal("0.4500"),
                max_digits=6,
                validators=[django.core.validators.MinValueValidator(Decimal("0.0000"))],
            ),
        ),
        migrations.AddField(
            model_name="cashoutsettings",
            name="max_pre_match_cash_out_percent",
            field=models.DecimalField(
                decimal_places=2,
                default=Decimal("90.00"),
                max_digits=5,
                validators=[django.core.validators.MinValueValidator(Decimal("0.00"))],
            ),
        ),
        migrations.AddField(
            model_name="cashoutsettings",
            name="max_in_progress_cash_out_percent",
            field=models.DecimalField(
                decimal_places=2,
                default=Decimal("60.00"),
                max_digits=5,
                validators=[django.core.validators.MinValueValidator(Decimal("0.00"))],
            ),
        ),
        migrations.AddField(
            model_name="cashoutsettings",
            name="risk_discount_exponent",
            field=models.DecimalField(
                decimal_places=4,
                default=Decimal("1.7000"),
                max_digits=6,
                validators=[django.core.validators.MinValueValidator(Decimal("0.0000"))],
            ),
        ),
    ]

