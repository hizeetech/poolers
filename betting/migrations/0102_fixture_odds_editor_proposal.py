from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("betting", "0101_siteconfiguration_show_double_chance_odds"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="FixtureOddsEditorAssignment",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "can_edit_odds",
                    models.BooleanField(
                        db_index=True,
                        default=True,
                        help_text="If ticked, this user may submit fixture odds change proposals (6 main markets only) for admin approval.",
                    ),
                ),
                (
                    "assigned_at",
                    models.DateTimeField(auto_now_add=True, db_index=True),
                ),
                (
                    "updated_at",
                    models.DateTimeField(auto_now=True, db_index=True),
                ),
                ("notes", models.TextField(blank=True, default="")),
                (
                    "assigned_by",
                    models.ForeignKey(
                        blank=True,
                        limit_choices_to=models.Q(user_type="admin") | models.Q(is_superuser=True),
                        null=True,
                        on_delete=models.deletion.SET_NULL,
                        related_name="fixture_odds_editor_assignments_made",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "user",
                    models.OneToOneField(
                        limit_choices_to=models.Q(user_type__in=["retail_manager", "crm"]) | models.Q(is_staff=True),
                        on_delete=models.deletion.CASCADE,
                        related_name="fixture_odds_editor_assignment",
                        to=settings.AUTH_USER_MODEL,
                        help_text="Retail Manager or CRM user designated by admin to submit fixture odds change proposals.",
                    ),
                ),
            ],
            options={
                "verbose_name": "Fixture Odds Editor Assignment",
                "verbose_name_plural": "Fixture Odds Editor Assignments",
                "ordering": ("-updated_at",),
            },
        ),
        migrations.CreateModel(
            name="FixtureOddsChangeProposal",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("fixture_serial_number", models.CharField(blank=True, db_index=True, default="", max_length=50)),
                ("fixture_match_date", models.DateField(blank=True, db_index=True, null=True)),
                ("fixture_home_team", models.CharField(blank=True, default="", max_length=255)),
                ("fixture_away_team", models.CharField(blank=True, default="", max_length=255)),
                ("current_home_win_odd", models.DecimalField(blank=True, decimal_places=2, max_digits=5, null=True)),
                ("current_draw_odd", models.DecimalField(blank=True, decimal_places=2, max_digits=5, null=True)),
                ("current_away_win_odd", models.DecimalField(blank=True, decimal_places=2, max_digits=5, null=True)),
                ("current_home_or_draw_odd", models.DecimalField(blank=True, decimal_places=2, max_digits=5, null=True)),
                ("current_either_team_win_odd", models.DecimalField(blank=True, decimal_places=2, max_digits=5, null=True)),
                ("current_away_or_draw_odd", models.DecimalField(blank=True, decimal_places=2, max_digits=5, null=True)),
                ("proposed_home_win_odd", models.DecimalField(blank=True, decimal_places=2, max_digits=5, null=True)),
                ("proposed_draw_odd", models.DecimalField(blank=True, decimal_places=2, max_digits=5, null=True)),
                ("proposed_away_win_odd", models.DecimalField(blank=True, decimal_places=2, max_digits=5, null=True)),
                ("proposed_home_or_draw_odd", models.DecimalField(blank=True, decimal_places=2, max_digits=5, null=True)),
                ("proposed_either_team_win_odd", models.DecimalField(blank=True, decimal_places=2, max_digits=5, null=True)),
                ("proposed_away_or_draw_odd", models.DecimalField(blank=True, decimal_places=2, max_digits=5, null=True)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "Pending Admin Approval"),
                            ("approved", "Approved & Published"),
                            ("rejected", "Rejected"),
                        ],
                        db_index=True,
                        default="pending",
                        max_length=20,
                    ),
                ),
                ("proposer_notes", models.TextField(blank=True, default="")),
                ("admin_notes", models.TextField(blank=True, default="")),
                ("approved_at", models.DateTimeField(blank=True, db_index=True, null=True)),
                ("rejected_at", models.DateTimeField(blank=True, db_index=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("updated_at", models.DateTimeField(auto_now=True, db_index=True)),
                (
                    "approved_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=models.deletion.SET_NULL,
                        related_name="odds_change_proposals_approved",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "betting_period",
                    models.ForeignKey(
                        blank=True,
                        db_index=True,
                        null=True,
                        on_delete=models.deletion.SET_NULL,
                        related_name="odds_change_proposals",
                        to="betting.bettingperiod",
                    ),
                ),
                (
                    "fixture",
                    models.ForeignKey(
                        on_delete=models.deletion.CASCADE,
                        related_name="odds_change_proposals",
                        to="betting.fixture",
                    ),
                ),
                (
                    "proposer",
                    models.ForeignKey(
                        on_delete=models.deletion.CASCADE,
                        related_name="odds_change_proposals_submitted",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "rejected_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=models.deletion.SET_NULL,
                        related_name="odds_change_proposals_rejected",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "verbose_name": "Fixture Odds Change Proposal",
                "verbose_name_plural": "Fixture Odds Change Proposals",
                "ordering": ("-created_at",),
                "indexes": [
                    models.Index(fields=["status", "created_at"], name="focp_status_created_at_idx"),
                    models.Index(fields=["fixture", "status"], name="focp_fixture_status_idx"),
                ],
            },
        ),
    ]
