# Generated locally: adds Fixture.disabled_for_betting BooleanField default=False
# to allow admin to toggle events unselectable on /fixtures/ page while keeping
# them visible (greyed out / no clicks). Also applies to Smart Picks grid and
# Popular Picks card.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('betting', '0109_superagenttransferlog'),
    ]

    operations = [
        migrations.AddField(
            model_name='fixture',
            name='disabled_for_betting',
            field=models.BooleanField(
                default=False,
                help_text='When checked, this fixture remains visible on /fixtures/ but is completely unselectable (greyed out, no clicks, Smart Picks & Popular Picks skip it). Toggle via the Action column in the admin list.',
            ),
        ),
    ]
