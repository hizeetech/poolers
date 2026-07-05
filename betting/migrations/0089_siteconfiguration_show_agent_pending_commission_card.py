from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("betting", "0088_dashboardtask"),
    ]

    operations = [
        migrations.AddField(
            model_name="siteconfiguration",
            name="show_agent_pending_commission_card",
            field=models.BooleanField(
                default=True,
                help_text="Show or hide the Pending Commission card on the agent dashboard frontend.",
            ),
        ),
    ]
