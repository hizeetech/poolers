import datetime
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.http import QueryDict
from django.test import TestCase, Client, override_settings
from django.urls import reverse
from django.utils import timezone

from betting.models import (
    BettingPeriod,
    Fixture,
    FixtureOddsEditorAssignment,
    FixtureOddsChangeProposal,
)


User = get_user_model()


@override_settings(DEBUG=False)
class FixtureOddsProposalWorkflowTests(TestCase):
    def setUp(self):
        self.password = 'password123'
        self.admin_user = User.objects.create_user(
            email='admin-proposal@test.com',
            password=self.password,
            user_type='admin',
            username='fo_admin',
            is_staff=True,
            is_superuser=True,
        )
        self.rm_user = User.objects.create_user(
            email='rm-proposal@test.com',
            password=self.password,
            user_type='retail_manager',
            username='fo_rm',
        )
        self.rm_not_editor = User.objects.create_user(
            email='rm-noaccess@test.com',
            password=self.password,
            user_type='retail_manager',
            username='fo_rm_no',
        )
        self.crm_user = User.objects.create_user(
            email='crm-proposal@test.com',
            password=self.password,
            user_type='crm',
            username='fo_crm',
        )
        FixtureOddsEditorAssignment.objects.create(
            user=self.rm_user,
            can_edit_odds=True,
            assigned_by=self.admin_user,
        )
        FixtureOddsEditorAssignment.objects.create(
            user=self.crm_user,
            can_edit_odds=True,
            assigned_by=self.admin_user,
        )

        self.period = BettingPeriod.objects.create(
            name=f"Test Period {timezone.now().timestamp()}",
            start_date=timezone.now().date(),
            end_date=timezone.now().date() + datetime.timedelta(days=7),
            is_active=True,
        )
        self.fixture = Fixture.objects.create(
            betting_period=self.period,
            serial_number=9901,
            home_team='Sydney Roosters',
            away_team='Melbourne Storm',
            match_date=timezone.now().date(),
            match_time=datetime.time(19, 0),
            home_win_odd=Decimal('2.10'),
            draw_odd=Decimal('3.30'),
            away_win_odd=Decimal('3.50'),
            home_or_draw_odd=Decimal('1.30'),
            either_team_win_odd=Decimal('1.45'),
            away_or_draw_odd=Decimal('1.48'),
            status='scheduled',
            is_active=True,
        )

    def test_unauthenticated_get_table_403(self):
        c = Client()
        resp = c.get(reverse('betting:odds_editor_fixtures_table'))
        self.assertNotEqual(resp.status_code, 200)

    def test_non_editor_rm_403(self):
        c = Client()
        c.force_login(self.rm_not_editor)
        resp = c.get(reverse('betting:odds_editor_fixtures_table'))
        self.assertEqual(resp.status_code, 403)

    def test_editor_rm_can_access_table(self):
        c = Client()
        c.force_login(self.rm_user)
        resp = c.get(reverse('betting:odds_editor_fixtures_table'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Sydney Roosters")

    def test_editor_crm_can_access_table(self):
        c = Client()
        c.force_login(self.crm_user)
        resp = c.get(reverse('betting:odds_editor_fixtures_table'))
        self.assertEqual(resp.status_code, 200)

    def test_admin_can_access_table(self):
        c = Client()
        c.force_login(self.admin_user)
        resp = c.get(reverse('betting:odds_editor_fixtures_table'))
        self.assertEqual(resp.status_code, 200)

    def test_non_editor_cannot_access_edit(self):
        c = Client()
        c.force_login(self.rm_not_editor)
        resp = c.get(reverse('betting:odds_editor_edit_fixture', args=[self.fixture.id]))
        self.assertEqual(resp.status_code, 403)

    def test_edit_page_loads_six_odds(self):
        c = Client()
        c.force_login(self.rm_user)
        resp = c.get(reverse('betting:odds_editor_edit_fixture', args=[self.fixture.id]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "2.10")
        self.assertContains(resp, "3.30")
        self.assertContains(resp, "3.50")
        self.assertContains(resp, "1.30")
        self.assertContains(resp, "1.45")
        self.assertContains(resp, "1.48")
        self.assertContains(resp, "Sydney Roosters")
        self.assertContains(resp, "Melbourne Storm")

    def test_submit_no_changes_warns(self):
        c = Client()
        c.force_login(self.rm_user)
        data = {
            'home_win_odd': '2.10',
            'draw_odd': '3.30',
            'away_win_odd': '3.50',
            'home_or_draw_odd': '1.30',
            'either_team_win_odd': '1.45',
            'away_or_draw_odd': '1.48',
            'proposer_notes': '',
        }
        with patch('betting.views.send_mail') as mock_send:
            resp = c.post(reverse('betting:odds_editor_edit_fixture', args=[self.fixture.id]), data)
            self.assertEqual(resp.status_code, 302)
            self.assertEqual(mock_send.call_count, 0)
        self.assertEqual(FixtureOddsChangeProposal.objects.count(), 0)

    def test_submit_with_changes_creates_pending_proposal_and_sends_email(self):
        c = Client()
        c.force_login(self.rm_user)
        data = QueryDict(mutable=True)
        data['home_win_odd'] = '2.25'
        data['draw_odd'] = '3.25'
        data['away_win_odd'] = '3.60'
        data['home_or_draw_odd'] = '1.35'
        data['either_team_win_odd'] = '1.40'
        data['away_or_draw_odd'] = '1.55'
        data['proposer_notes'] = 'Update based on latest injury news.'

        with patch('betting.views.send_mail') as mock_send:
            resp = c.post(
                reverse('betting:odds_editor_edit_fixture', args=[self.fixture.id]),
                data,
            )
            self.assertEqual(resp.status_code, 302, msg=getattr(resp, 'content', b''))
            self.assertGreaterEqual(mock_send.call_count, 1)
            subject = mock_send.call_args[0][0]
            self.assertIn('ACTION REQUIRED', subject)
            self.assertIn(str(self.fixture.home_team), subject)

        proposal = FixtureOddsChangeProposal.objects.filter(proposer=self.rm_user, fixture=self.fixture).first()
        self.assertIsNotNone(proposal)
        self.assertEqual(proposal.status, FixtureOddsChangeProposal.STATUS_PENDING)
        self.assertEqual(proposal.proposed_home_win_odd, Decimal('2.25'))
        self.assertEqual(proposal.proposed_away_or_draw_odd, Decimal('1.55'))
        self.assertEqual(proposal.current_home_win_odd, Decimal('2.10'))
        self.assertEqual(proposal.proposer_notes, 'Update based on latest injury news.')

        self.assertEqual(tuple(proposal.changed_odd_fields), (
            'home_win_odd', 'draw_odd', 'away_win_odd',
            'home_or_draw_odd', 'either_team_win_odd', 'away_or_draw_odd',
        ))

        self.fixture.refresh_from_db()
        self.assertEqual(self.fixture.home_win_odd, Decimal('2.10'))

    def test_admin_approval_pushes_odds_to_fixture(self):
        proposal = FixtureOddsChangeProposal.build_from_fixture(
            fixture=self.fixture,
            proposer=self.crm_user,
            proposed_vals={
                'home_win_odd': Decimal('2.50'),
                'draw_odd': Decimal('3.40'),
                'away_win_odd': Decimal('2.90'),
                'home_or_draw_odd': Decimal('1.40'),
                'either_team_win_odd': Decimal('1.30'),
                'away_or_draw_odd': Decimal('1.40'),
            },
            proposer_notes='Adjustments requested by supervisor',
        )
        proposal.status = FixtureOddsChangeProposal.STATUS_PENDING
        proposal.save()

        proposal.status = FixtureOddsChangeProposal.STATUS_APPROVED
        proposal.approved_by = self.admin_user
        proposal.approved_at = timezone.now()
        proposal.save()
        applied = proposal.apply_to_fixture()
        self.assertTrue(applied)

        self.fixture.refresh_from_db()
        self.assertEqual(self.fixture.home_win_odd, Decimal('2.50'))
        self.assertEqual(self.fixture.draw_odd, Decimal('3.40'))
        self.assertEqual(self.fixture.away_win_odd, Decimal('2.90'))
        self.assertEqual(self.fixture.home_or_draw_odd, Decimal('1.40'))
        self.assertEqual(self.fixture.either_team_win_odd, Decimal('1.30'))
        self.assertEqual(self.fixture.away_or_draw_odd, Decimal('1.40'))

    def test_rejected_proposal_does_not_apply_to_fixture(self):
        proposal = FixtureOddsChangeProposal.build_from_fixture(
            fixture=self.fixture,
            proposer=self.rm_user,
            proposed_vals={'home_win_odd': Decimal('5.00'), 'draw_odd': Decimal('6.00')},
            proposer_notes='Bad proposal test',
        )
        proposal.status = FixtureOddsChangeProposal.STATUS_REJECTED
        proposal.save()
        applied = proposal.apply_to_fixture()
        self.assertFalse(applied)
        self.fixture.refresh_from_db()
        self.assertEqual(self.fixture.home_win_odd, Decimal('2.10'))

    def test_can_edit_fixture_odds_helper(self):
        from betting.views import can_edit_fixture_odds

        self.assertTrue(can_edit_fixture_odds(self.admin_user))
        self.assertTrue(can_edit_fixture_odds(self.rm_user))
        self.assertTrue(can_edit_fixture_odds(self.crm_user))
        self.assertFalse(can_edit_fixture_odds(self.rm_not_editor))

        player = User.objects.create_user(email='player-x@test.com', password=self.password, username='p_x', user_type='player')
        self.assertFalse(can_edit_fixture_odds(player))
