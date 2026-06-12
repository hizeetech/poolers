from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import django_ckeditor_5.fields


class Migration(migrations.Migration):

    dependencies = [
        ('betting', '0077_fixture_odds_update_direction'),
    ]

    operations = [
        migrations.CreateModel(
            name='RetailManagerDashboardNote',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('content', django_ckeditor_5.fields.CKEditor5Field(blank=True, config_name='default', default='')),
                ('created_at', models.DateTimeField(auto_now_add=True, db_index=True)),
                ('updated_at', models.DateTimeField(auto_now=True, db_index=True)),
                ('retail_manager', models.OneToOneField(limit_choices_to={'user_type': 'retail_manager'}, on_delete=django.db.models.deletion.CASCADE, related_name='retail_dashboard_note', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'verbose_name': 'Retail Manager Dashboard Note',
                'verbose_name_plural': 'Retail Manager Dashboard Notes',
                'ordering': ['-updated_at'],
            },
        ),
    ]
