from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("betting", "0071_siteconfiguration_enable_global_cashier_voiding"),
    ]

    operations = [
        migrations.CreateModel(
            name="WalletLedgerEntry",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("direction", models.CharField(choices=[("credit", "Credit"), ("debit", "Debit")], db_index=True, max_length=10)),
                ("amount", models.DecimalField(decimal_places=2, max_digits=12)),
                ("balance_before", models.DecimalField(decimal_places=2, max_digits=12)),
                ("balance_after", models.DecimalField(decimal_places=2, max_digits=12)),
                ("reference", models.CharField(blank=True, db_index=True, default="", max_length=120)),
                ("reason", models.CharField(blank=True, default="", max_length=255)),
                ("metadata", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("actor", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="wallet_ledger_actions", to=settings.AUTH_USER_MODEL)),
                ("transaction", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="wallet_ledger_entries", to="betting.transaction")),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="wallet_ledger_entries", to=settings.AUTH_USER_MODEL)),
                ("wallet", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="ledger_entries", to="betting.wallet")),
            ],
            options={
                "ordering": ["-created_at"],
            },
        ),
        migrations.AddIndex(
            model_name="walletledgerentry",
            index=models.Index(fields=["user", "created_at"], name="bet_wl_user_created_idx"),
        ),
        migrations.AddIndex(
            model_name="walletledgerentry",
            index=models.Index(fields=["wallet", "created_at"], name="bet_wl_wallet_created_idx"),
        ),
    ]
