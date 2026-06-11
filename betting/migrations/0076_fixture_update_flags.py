from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("betting", "0075_crmwalletapprovalrequest_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="fixture",
            name="datetime_updated_at",
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name="fixture",
            name="odds_updated_at",
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
    ]

