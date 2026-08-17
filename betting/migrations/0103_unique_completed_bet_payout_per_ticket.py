from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('betting', '0102_fixture_odds_editor_proposal'),
    ]

    operations = [
        # STEP 1: Pre-clean any existing duplicate completed bet_payout rows
        # (these are legacy duplicates caused by the parallel-worker race
        #  before the idempotency guard + DB unique constraint).
        # We keep the OLDEST completed bet_payout per ticket (smallest id / earliest timestamp)
        # and mark every extra duplicate as status='reversed' so they no longer
        # count as "active" payouts and the partial unique index can be created.
        migrations.RunSQL(
            sql="""
            WITH ranked AS (
                SELECT id,
                       ROW_NUMBER() OVER (
                           PARTITION BY related_bet_ticket_id
                           ORDER BY timestamp ASC, id ASC
                       ) AS rn
                FROM betting_transaction
                WHERE transaction_type = 'bet_payout'
                  AND status = 'completed'
                  AND related_bet_ticket_id IS NOT NULL
            )
            UPDATE betting_transaction t
               SET status = 'reversed'
              FROM ranked r
             WHERE t.id = r.id
               AND r.rn > 1;
            """,
            reverse_sql="""
            -- Rollback is intentionally a no-op: once we've reversed duplicate rows
            -- we never want to un-reverse them (they'd block index re-creation and
            -- represent already-reversed over-payments that were deduplicated).
            SELECT 1;
            """,
        ),
        # STEP 2: Add the DB-enforced partial unique index as the final guard
        # against any future parallel-worker / signal-storm race producing
        # multiple completed bet_payout rows per ticket.
        migrations.AddConstraint(
            model_name='transaction',
            constraint=models.UniqueConstraint(
                condition=models.Q(('status', 'completed'), ('transaction_type', 'bet_payout')),
                fields=('related_bet_ticket',),
                name='unique_completed_bet_payout_per_ticket',
                violation_error_code='duplicate_bet_payout',
                violation_error_message='A completed bet payout already exists for this ticket.',
            ),
        ),
    ]
