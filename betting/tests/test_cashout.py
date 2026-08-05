from decimal import Decimal, localcontext

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from betting.models import (
    BetTicket,
    BetTicketCashOut,
    BettingPeriod,
    CashOutSettings,
    Fixture,
    Selection,
    Transaction,
    User,
    Wallet,
)
from betting.services.cashout import CashOutError, build_cashout_quote, execute_cashout


class CashOutTests(TestCase):
    def setUp(self):
        self.password = "password123"
        self.user = User.objects.create_user(
            email="cashout-user@test.com",
            password=self.password,
            user_type="cashier",
            username="cashout_user",
        )
        Wallet.objects.create(user=self.user, balance=Decimal("0.00"))

        CashOutSettings.objects.update_or_create(
            pk=1,
            defaults={
                "enable_cash_out": True,
                "enable_cash_out_nap": True,
                "enable_cash_out_permutation": True,
                "enable_full_cash_out": True,
                "enable_partial_cash_out": False,
                "enable_pre_match_cash_out": True,
                "charge_type": CashOutSettings.CHARGE_TYPE.FIXED,
                "fixed_charge_amount": Decimal("100.00"),
                "percentage_charge": Decimal("0.00"),
                "company_margin_percent": Decimal("10.00"),
                "risk_multiplier": Decimal("0.0500"),
                "cash_out_scaling_factor": Decimal("0.4500"),
                "max_pre_match_cash_out_percent": Decimal("90.00"),
                "max_in_progress_cash_out_percent": Decimal("60.00"),
                "risk_discount_exponent": Decimal("1.7000"),
                "minimum_stake_eligible": Decimal("0.00"),
                "maximum_stake_eligible": Decimal("100000000.00"),
                "minimum_cash_out_amount": Decimal("0.00"),
                "maximum_cash_out_amount": Decimal("100000000.00"),
                "manually_closed": False,
            },
        )

        today = timezone.localdate()
        self.period = BettingPeriod.objects.create(
            name="CashOut Period",
            start_date=today,
            end_date=today,
            is_active=True,
        )

    def _fixture(self, *, status="scheduled", home_score=None, away_score=None, serial=1):
        today = timezone.localdate()
        match_date = today
        match_time = timezone.now().time()
        if status == "scheduled":
            match_date = today + timedelta(days=1)
            match_time = timezone.now().time()
        return Fixture.objects.create(
            betting_period=self.period,
            serial_number=serial,
            home_team=f"Home {serial}",
            away_team=f"Away {serial}",
            match_date=match_date,
            match_time=match_time,
            status=status,
            is_active=True,
            home_score=home_score,
            away_score=away_score,
            home_win_odd=Decimal("2.00"),
            draw_odd=Decimal("3.00"),
            away_win_odd=Decimal("2.50"),
        )

    def test_cashout_disabled_when_globally_disabled(self):
        CashOutSettings.objects.filter(pk=1).update(enable_cash_out=False)
        ticket = BetTicket.objects.create(
            user=self.user,
            stake_amount=Decimal("2000.00"),
            total_odd=Decimal("2.00"),
            potential_winning=Decimal("4000.00"),
            min_winning=Decimal("4000.00"),
            max_winning=Decimal("4000.00"),
            status="pending",
            bet_type="single",
        )
        f1 = self._fixture(status="scheduled", serial=1)
        Selection.objects.create(bet_ticket=ticket, fixture=f1, betting_period=self.period, bet_type="home_win", odd_selected=Decimal("2.00"))

        quote = build_cashout_quote(ticket=ticket)
        self.assertFalse(quote.eligible)
        self.assertIn("disabled", quote.reason.lower())

    def test_prematch_cashout_uses_stake_minus_fixed_charge(self):
        ticket = BetTicket.objects.create(
            user=self.user,
            stake_amount=Decimal("2000.00"),
            cash_stake_amount=Decimal("2000.00"),
            bonus_stake_amount=Decimal("0.00"),
            total_odd=Decimal("2.00"),
            potential_winning=Decimal("4000.00"),
            min_winning=Decimal("4000.00"),
            max_winning=Decimal("4000.00"),
            status="pending",
            bet_type="single",
        )
        f1 = self._fixture(status="scheduled", serial=1)
        Selection.objects.create(bet_ticket=ticket, fixture=f1, betting_period=self.period, bet_type="home_win", odd_selected=Decimal("2.00"))

        quote = build_cashout_quote(ticket=ticket)
        self.assertTrue(quote.eligible)
        self.assertEqual(quote.cashout_amount, Decimal("855.00"))
        self.assertEqual(quote.charge_type, "fixed")
        self.assertEqual(quote.charge_value, Decimal("100.00"))
        self.assertEqual(quote.original_odds, Decimal("2.000000"))
        self.assertEqual(quote.completed_odds, Decimal("0.000000"))

    def test_prematch_cashout_uses_cash_stake_only_when_ticket_mixed_cash_and_bonus(self):
        ticket = BetTicket.objects.create(
            user=self.user,
            stake_amount=Decimal("2500.00"),
            cash_stake_amount=Decimal("2000.00"),
            bonus_stake_amount=Decimal("500.00"),
            total_odd=Decimal("2.00"),
            potential_winning=Decimal("5000.00"),
            min_winning=Decimal("5000.00"),
            max_winning=Decimal("5000.00"),
            status="pending",
            bet_type="single",
        )
        f1 = self._fixture(status="scheduled", serial=1)
        Selection.objects.create(bet_ticket=ticket, fixture=f1, betting_period=self.period, bet_type="home_win", odd_selected=Decimal("2.00"))

        quote = build_cashout_quote(ticket=ticket)
        self.assertTrue(quote.eligible)
        self.assertEqual(quote.cashout_amount, Decimal("855.00"))

    def test_cashout_ineligible_when_ticket_funded_entirely_by_bonus(self):
        ticket = BetTicket.objects.create(
            user=self.user,
            stake_amount=Decimal("1000.00"),
            cash_stake_amount=Decimal("0.00"),
            bonus_stake_amount=Decimal("1000.00"),
            total_odd=Decimal("2.00"),
            potential_winning=Decimal("2000.00"),
            min_winning=Decimal("2000.00"),
            max_winning=Decimal("2000.00"),
            status="pending",
            bet_type="single",
        )
        f1 = self._fixture(status="scheduled", serial=1)
        Selection.objects.create(bet_ticket=ticket, fixture=f1, betting_period=self.period, bet_type="home_win", odd_selected=Decimal("2.00"))

        quote = build_cashout_quote(ticket=ticket)
        self.assertFalse(quote.eligible)
        self.assertEqual(quote.cashout_amount, Decimal("0.00"))

    def test_prematch_cashout_with_zero_charge_deducts_minimum(self):
        CashOutSettings.objects.filter(pk=1).update(fixed_charge_amount=Decimal("0.00"), percentage_charge=Decimal("0.00"))
        ticket = BetTicket.objects.create(
            user=self.user,
            stake_amount=Decimal("500.00"),
            total_odd=Decimal("2.00"),
            potential_winning=Decimal("1000.00"),
            min_winning=Decimal("1000.00"),
            max_winning=Decimal("1000.00"),
            status="pending",
            bet_type="single",
        )
        f1 = self._fixture(status="scheduled", serial=1)
        Selection.objects.create(bet_ticket=ticket, fixture=f1, betting_period=self.period, bet_type="home_win", odd_selected=Decimal("2.00"))

        quote = build_cashout_quote(ticket=ticket)
        self.assertTrue(quote.eligible)
        self.assertLess(quote.cashout_amount, Decimal("500.00"))
        self.assertEqual(quote.cashout_amount, Decimal("220.50"))

    def test_cashout_disabled_when_event_is_in_progress(self):
        ticket = BetTicket.objects.create(
            user=self.user,
            stake_amount=Decimal("8660.00"),
            total_odd=Decimal("46.00"),
            potential_winning=Decimal("400620.26"),
            min_winning=Decimal("400620.26"),
            max_winning=Decimal("408632.67"),
            status="pending",
            bet_type="multiple",
        )
        f1 = self._fixture(status="live", serial=1)
        f2 = self._fixture(status="scheduled", serial=2)
        Selection.objects.create(bet_ticket=ticket, fixture=f1, betting_period=self.period, bet_type="home_win", odd_selected=Decimal("2.00"))
        Selection.objects.create(bet_ticket=ticket, fixture=f2, betting_period=self.period, bet_type="home_win", odd_selected=Decimal("2.00"))

        quote = build_cashout_quote(ticket=ticket)
        self.assertFalse(quote.eligible)
        self.assertIn("temporarily unavailable", quote.reason.lower())

    def test_cashout_disabled_when_event_is_started_but_not_settled(self):
        ticket = BetTicket.objects.create(
            user=self.user,
            stake_amount=Decimal("1000.00"),
            total_odd=Decimal("4.00"),
            potential_winning=Decimal("4000.00"),
            min_winning=Decimal("4000.00"),
            max_winning=Decimal("4000.00"),
            status="pending",
            bet_type="multiple",
        )
        f1 = self._fixture(status="live", serial=1)
        f2 = self._fixture(status="scheduled", serial=2)
        Selection.objects.create(bet_ticket=ticket, fixture=f1, betting_period=self.period, bet_type="home_win", odd_selected=Decimal("2.00"))
        Selection.objects.create(bet_ticket=ticket, fixture=f2, betting_period=self.period, bet_type="home_win", odd_selected=Decimal("2.00"))

        quote = build_cashout_quote(ticket=ticket)
        self.assertFalse(quote.eligible)
        self.assertIn("temporarily unavailable", quote.reason.lower())

    def test_cashout_disabled_when_min_cashout_exceeds_ticket_cap(self):
        CashOutSettings.objects.filter(pk=1).update(minimum_cash_out_amount=Decimal("1000000.00"))
        ticket = BetTicket.objects.create(
            user=self.user,
            stake_amount=Decimal("8660.00"),
            total_odd=Decimal("46.00"),
            potential_winning=Decimal("400620.26"),
            min_winning=Decimal("400620.26"),
            max_winning=Decimal("408632.67"),
            status="pending",
            bet_type="multiple",
        )
        f1 = self._fixture(status="scheduled", serial=1)
        Selection.objects.create(bet_ticket=ticket, fixture=f1, betting_period=self.period, bet_type="home_win", odd_selected=Decimal("2.00"))

        quote = build_cashout_quote(ticket=ticket)
        self.assertTrue(quote.eligible)
        self.assertEqual(quote.cashout_amount, Decimal("3852.00"))

    def test_min_cashout_blocks_post_result_offers(self):
        CashOutSettings.objects.filter(pk=1).update(minimum_cash_out_amount=Decimal("1000000.00"))
        ticket = BetTicket.objects.create(
            user=self.user,
            stake_amount=Decimal("2000.00"),
            total_odd=Decimal("2.00"),
            potential_winning=Decimal("4000.00"),
            min_winning=Decimal("4000.00"),
            max_winning=Decimal("4000.00"),
            status="pending",
            bet_type="multiple",
        )
        f1 = self._fixture(status="finished", home_score=2, away_score=1, serial=1)
        f2 = self._fixture(status="scheduled", serial=2)
        f3 = self._fixture(status="scheduled", serial=3)
        Selection.objects.create(bet_ticket=ticket, fixture=f1, betting_period=self.period, bet_type="home_win", odd_selected=Decimal("2.00"))
        Selection.objects.create(bet_ticket=ticket, fixture=f2, betting_period=self.period, bet_type="home_win", odd_selected=Decimal("2.00"))
        Selection.objects.create(bet_ticket=ticket, fixture=f3, betting_period=self.period, bet_type="home_win", odd_selected=Decimal("2.00"))

        quote = build_cashout_quote(ticket=ticket)
        self.assertFalse(quote.eligible)

    def test_nap_cashout_disabled_when_ticket_dead(self):
        ticket = BetTicket.objects.create(
            user=self.user,
            stake_amount=Decimal("2000.00"),
            total_odd=Decimal("4.00"),
            potential_winning=Decimal("8000.00"),
            min_winning=Decimal("8000.00"),
            max_winning=Decimal("8000.00"),
            status="pending",
            bet_type="multiple",
        )
        f1 = self._fixture(status="finished", home_score=0, away_score=2, serial=1)
        f2 = self._fixture(status="scheduled", serial=2)
        Selection.objects.create(bet_ticket=ticket, fixture=f1, betting_period=self.period, bet_type="home_win", odd_selected=Decimal("2.00"))
        Selection.objects.create(bet_ticket=ticket, fixture=f2, betting_period=self.period, bet_type="home_win", odd_selected=Decimal("2.00"))

        quote = build_cashout_quote(ticket=ticket)
        self.assertFalse(quote.eligible)
        self.assertIn("losing selection", quote.reason.lower())

    def test_permutation_cashout_eliminated_when_required_wins_not_achievable(self):
        ticket = BetTicket.objects.create(
            user=self.user,
            stake_amount=Decimal("1000.00"),
            total_odd=Decimal("0.00"),
            potential_winning=Decimal("10000.00"),
            min_winning=Decimal("0.00"),
            max_winning=Decimal("10000.00"),
            status="pending",
            bet_type="system",
            system_min_count=6,
            original_selections_count=7,
        )

        f1 = self._fixture(status="finished", home_score=2, away_score=1, serial=1)
        f2 = self._fixture(status="finished", home_score=2, away_score=1, serial=2)
        f3 = self._fixture(status="finished", home_score=0, away_score=1, serial=3)
        f4 = self._fixture(status="finished", home_score=0, away_score=1, serial=4)
        f5 = self._fixture(status="finished", home_score=0, away_score=1, serial=5)
        f6 = self._fixture(status="finished", home_score=0, away_score=1, serial=6)
        f7 = self._fixture(status="scheduled", serial=7)

        Selection.objects.create(bet_ticket=ticket, fixture=f1, betting_period=self.period, bet_type="home_win", odd_selected=Decimal("1.50"))
        Selection.objects.create(bet_ticket=ticket, fixture=f2, betting_period=self.period, bet_type="home_win", odd_selected=Decimal("1.50"))
        Selection.objects.create(bet_ticket=ticket, fixture=f3, betting_period=self.period, bet_type="home_win", odd_selected=Decimal("1.50"))
        Selection.objects.create(bet_ticket=ticket, fixture=f4, betting_period=self.period, bet_type="home_win", odd_selected=Decimal("1.50"))
        Selection.objects.create(bet_ticket=ticket, fixture=f5, betting_period=self.period, bet_type="home_win", odd_selected=Decimal("1.50"))
        Selection.objects.create(bet_ticket=ticket, fixture=f6, betting_period=self.period, bet_type="home_win", odd_selected=Decimal("1.50"))
        Selection.objects.create(bet_ticket=ticket, fixture=f7, betting_period=self.period, bet_type="home_win", odd_selected=Decimal("1.50"))

        quote = build_cashout_quote(ticket=ticket)
        self.assertFalse(quote.eligible)
        self.assertEqual(quote.cashout_amount, Decimal("0.00"))
        self.assertIn("mathematically eliminated", quote.reason.lower())

        with self.assertRaises(CashOutError):
            execute_cashout(ticket_id=ticket.id, actor=self.user, ip_address="127.0.0.1", user_agent="test")

    def test_after_result_cashout_uses_won_odds_and_remaining_odds(self):
        ticket = BetTicket.objects.create(
            user=self.user,
            stake_amount=Decimal("2000.00"),
            total_odd=Decimal("13.93"),
            potential_winning=Decimal("27852.00"),
            min_winning=Decimal("27852.00"),
            max_winning=Decimal("27852.00"),
            status="pending",
            bet_type="multiple",
        )
        f1 = self._fixture(status="finished", home_score=2, away_score=1, serial=1)
        f2 = self._fixture(status="scheduled", serial=2)
        Selection.objects.create(bet_ticket=ticket, fixture=f1, betting_period=self.period, bet_type="home_win", odd_selected=Decimal("3.30"))
        Selection.objects.create(bet_ticket=ticket, fixture=f2, betting_period=self.period, bet_type="home_win", odd_selected=Decimal("4.22"))

        quote = build_cashout_quote(ticket=ticket)
        self.assertTrue(quote.eligible)
        won_odds = Decimal("3.30")
        remaining_odds = Decimal("4.22")
        exponent = Decimal("1.7000")
        with localcontext() as ctx:
            ctx.prec = 40
            powered = ((remaining_odds - Decimal("1.00")).ln() * exponent).exp()
        risk_discount = (Decimal("1.00") / (Decimal("1.00") + (Decimal("0.0500") * powered))).quantize(Decimal("0.000001"))
        expected_before_scaling = (Decimal("2000.00") * won_odds * risk_discount * Decimal("0.90")).quantize(Decimal("0.01"))
        expected_after_scaling = (expected_before_scaling * Decimal("0.4500")).quantize(Decimal("0.01"))
        cap_amount = (Decimal("2000.00") * Decimal("0.60")).quantize(Decimal("0.01"))
        expected_final = min(expected_after_scaling, cap_amount)
        self.assertEqual(quote.cashout_before_scaling, expected_before_scaling)
        self.assertEqual(quote.cashout_after_scaling, expected_after_scaling)
        self.assertEqual(quote.cashout_amount, expected_final)
        self.assertEqual(quote.completed_odds, Decimal("3.300000"))
        self.assertEqual(quote.original_odds, Decimal("13.930000"))

    def test_permutation_cashout_only_when_mathematically_alive(self):
        k = 3
        potential_win = Decimal("20000.00")
        ticket = BetTicket.objects.create(
            user=self.user,
            stake_amount=Decimal("600.00"),
            total_odd=Decimal("0.00"),
            potential_winning=potential_win,
            min_winning=Decimal("0.00"),
            max_winning=potential_win,
            status="pending",
            bet_type="system",
            system_min_count=k,
            original_selections_count=5,
        )

        f1 = self._fixture(status="finished", home_score=2, away_score=1, serial=1)
        f2 = self._fixture(status="finished", home_score=2, away_score=1, serial=2)
        f3 = self._fixture(status="finished", home_score=2, away_score=1, serial=3)
        f4 = self._fixture(status="scheduled", serial=4)
        f5 = self._fixture(status="scheduled", serial=5)

        Selection.objects.create(bet_ticket=ticket, fixture=f1, betting_period=self.period, bet_type="home_win", odd_selected=Decimal("2.00"))
        Selection.objects.create(bet_ticket=ticket, fixture=f2, betting_period=self.period, bet_type="home_win", odd_selected=Decimal("2.00"))
        Selection.objects.create(bet_ticket=ticket, fixture=f3, betting_period=self.period, bet_type="home_win", odd_selected=Decimal("2.00"))
        Selection.objects.create(bet_ticket=ticket, fixture=f4, betting_period=self.period, bet_type="home_win", odd_selected=Decimal("2.00"))
        Selection.objects.create(bet_ticket=ticket, fixture=f5, betting_period=self.period, bet_type="home_win", odd_selected=Decimal("2.00"))

        quote = build_cashout_quote(ticket=ticket)
        self.assertTrue(quote.eligible)
        self.assertEqual(quote.cashout_amount, Decimal("360.00"))

        f1.status = "finished"
        f1.home_score = 0
        f1.away_score = 2
        f1.save()
        quote_after_loss = build_cashout_quote(ticket=ticket)
        self.assertTrue(quote_after_loss.eligible)
        self.assertEqual(quote_after_loss.cashout_amount, Decimal("360.00"))
        self.assertEqual(quote_after_loss.system_progress_factor, Decimal("0.6667"))
        self.assertEqual(quote_after_loss.system_paths_factor, Decimal("0.7500"))
        self.assertEqual(quote_after_loss.system_winning_paths, 3)
        self.assertEqual(quote_after_loss.system_total_paths, 4)

    def test_system_cashout_applies_system_progress_factor_before_k_met(self):
        ticket = BetTicket.objects.create(
            user=self.user,
            stake_amount=Decimal("20000.00"),
            total_odd=Decimal("1649.24"),
            potential_winning=Decimal("816829.78"),
            min_winning=Decimal("0.00"),
            max_winning=Decimal("816829.78"),
            status="pending",
            bet_type="system",
            system_min_count=3,
            original_selections_count=6,
        )

        f1 = self._fixture(status="finished", home_score=2, away_score=1, serial=1)
        f2 = self._fixture(status="finished", home_score=2, away_score=1, serial=2)
        f3 = self._fixture(status="scheduled", serial=3)
        f4 = self._fixture(status="scheduled", serial=4)
        f5 = self._fixture(status="scheduled", serial=5)
        f6 = self._fixture(status="scheduled", serial=6)

        Selection.objects.create(bet_ticket=ticket, fixture=f1, betting_period=self.period, bet_type="home_win", odd_selected=Decimal("3.50"))
        Selection.objects.create(bet_ticket=ticket, fixture=f2, betting_period=self.period, bet_type="home_win", odd_selected=Decimal("3.25"))
        Selection.objects.create(bet_ticket=ticket, fixture=f3, betting_period=self.period, bet_type="home_win", odd_selected=Decimal("3.33"))
        Selection.objects.create(bet_ticket=ticket, fixture=f4, betting_period=self.period, bet_type="home_win", odd_selected=Decimal("4.00"))
        Selection.objects.create(bet_ticket=ticket, fixture=f5, betting_period=self.period, bet_type="home_win", odd_selected=Decimal("3.50"))
        Selection.objects.create(bet_ticket=ticket, fixture=f6, betting_period=self.period, bet_type="home_win", odd_selected=Decimal("3.11"))

        quote = build_cashout_quote(ticket=ticket)
        self.assertTrue(quote.eligible)
        self.assertEqual(quote.required_wins, 3)
        self.assertEqual(quote.winning_count, 2)
        self.assertEqual(quote.pending_count, 4)
        self.assertEqual(quote.system_progress_factor, Decimal("0.6667"))
        self.assertEqual(quote.system_paths_factor, Decimal("0.9375"))
        self.assertEqual(quote.system_winning_paths, 15)
        self.assertEqual(quote.system_total_paths, 16)
        self.assertEqual(quote.cashout_amount, Decimal("12000.00"))

    def test_cashout_never_exceeds_potential_or_max_winning(self):
        ticket = BetTicket.objects.create(
            user=self.user,
            stake_amount=Decimal("2000.00"),
            total_odd=Decimal("2.00"),
            potential_winning=Decimal("2000.00"),
            min_winning=Decimal("2000.00"),
            max_winning=Decimal("1000.00"),
            status="pending",
            bet_type="multiple",
        )
        f1 = self._fixture(status="finished", home_score=2, away_score=1, serial=1)
        f2 = self._fixture(status="scheduled", serial=2)
        Selection.objects.create(bet_ticket=ticket, fixture=f1, betting_period=self.period, bet_type="home_win", odd_selected=Decimal("3.00"))
        Selection.objects.create(bet_ticket=ticket, fixture=f2, betting_period=self.period, bet_type="home_win", odd_selected=Decimal("3.00"))

        quote = build_cashout_quote(ticket=ticket)
        self.assertTrue(quote.eligible)
        self.assertLessEqual(quote.cashout_amount, Decimal("2000.00"))
        self.assertLessEqual(quote.cashout_amount, Decimal("1000.00"))

    def test_execute_cashout_credits_wallet_and_is_idempotent(self):
        ticket = BetTicket.objects.create(
            user=self.user,
            stake_amount=Decimal("2000.00"),
            total_odd=Decimal("2.00"),
            potential_winning=Decimal("4000.00"),
            min_winning=Decimal("4000.00"),
            max_winning=Decimal("4000.00"),
            status="pending",
            bet_type="single",
        )
        f1 = self._fixture(status="scheduled", serial=1)
        Selection.objects.create(bet_ticket=ticket, fixture=f1, betting_period=self.period, bet_type="home_win", odd_selected=Decimal("2.00"))

        cashout = execute_cashout(ticket_id=ticket.id, actor=self.user, ip_address="127.0.0.1", user_agent="test")
        ticket.refresh_from_db()
        wallet = Wallet.objects.get(user=self.user)

        self.assertEqual(ticket.status, "cashed_out")
        self.assertTrue(BetTicketCashOut.objects.filter(ticket=ticket).exists())
        self.assertEqual(wallet.balance, Decimal("855.00"))
        self.assertTrue(Transaction.objects.filter(transaction_type="bet_cashout", related_bet_ticket=ticket).exists())

        with self.assertRaises(CashOutError):
            execute_cashout(ticket_id=ticket.id, actor=self.user, ip_address="127.0.0.1", user_agent="test")
