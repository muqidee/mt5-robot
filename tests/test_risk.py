import math
import unittest
from dataclasses import replace
from decimal import Decimal

from bijisatu.models import SymbolSpec, Tick
from bijisatu.risk import price_levels, size_volume


def sizing(**changes):
    values = dict(equity=10000, risk_fraction=0.01, remaining_budget=100,
                  loss_per_lot=300, volume_min=0.01, volume_max=10, volume_step=0.01)
    values.update(changes)
    return size_volume(**values)


def symbol(**changes):
    values = dict(name="TEST", point=0.01, digits=2, tick_size=0.05,
                  volume_min=0.01, volume_max=10, volume_step=0.01, stops_level=10)
    values.update(changes)
    return SymbolSpec(**values)


class VolumeTests(unittest.TestCase):
    def test_floors_risk_and_remaining_budget(self):
        self.assertEqual(sizing(), 0.33)
        self.assertEqual(sizing(remaining_budget=50), 0.16)
        self.assertEqual(sizing(equity=5000), 0.16)

    def test_decimal_exact_boundary_does_not_lose_step(self):
        self.assertEqual(sizing(equity=30, risk_fraction=0.01, remaining_budget=0.3,
                                loss_per_lot=1, volume_min=0.1, volume_step=0.1), 0.3)
        self.assertEqual(sizing(loss_per_lot=1000), 0.1)
        self.assertEqual(sizing(remaining_budget=99.999999, loss_per_lot=1000), 0.09)

    def test_minimum_is_never_forced_upward(self):
        self.assertEqual(sizing(remaining_budget=2.99), 0)
        self.assertEqual(sizing(remaining_budget=3), 0.01)
        self.assertEqual(sizing(remaining_budget=3.01), 0.01)

    def test_maximum_is_floored_to_grid(self):
        self.assertEqual(sizing(loss_per_lot=1, volume_max=0.2), 0.2)
        self.assertEqual(sizing(loss_per_lot=1, volume_max=0.205), 0.2)
        self.assertEqual(sizing(loss_per_lot=1, volume_max=0.01), 0.01)

    def test_nonzero_grid_origin(self):
        args = dict(volume_min=0.03, volume_step=0.02, loss_per_lot=1000)
        self.assertEqual(sizing(**args), 0.09)
        self.assertEqual(sizing(**args, volume_max=0.08), 0.07)
        self.assertEqual(sizing(**args, remaining_budget=30), 0.03)
        self.assertEqual(sizing(**args, remaining_budget=49), 0.03)
        self.assertEqual(sizing(**args, remaining_budget=50), 0.05)
        self.assertEqual(sizing(**args, remaining_budget=29), 0)
        self.assertEqual(sizing(volume_min=0.03, volume_max=0.04,
                                volume_step=1, loss_per_lot=1), 0.03)

    def test_invalid_inputs_return_zero(self):
        fields = ("equity", "risk_fraction", "remaining_budget", "loss_per_lot",
                  "volume_min", "volume_max", "volume_step")
        for field in fields:
            for value in (0, -1, math.nan, math.inf, -math.inf, None, "1", True):
                with self.subTest(field=field, value=value):
                    self.assertEqual(sizing(**{field: value}), 0)
        self.assertEqual(sizing(risk_fraction=1.01), 0)
        self.assertEqual(sizing(volume_max=0.001), 0)
        self.assertEqual(sizing(risk_fraction=1, remaining_budget=10000,
                                loss_per_lot=1000), 10)

    def test_cent_account_equivalent_currency_units(self):
        standard = sizing(equity=1234, remaining_budget=8.76, loss_per_lot=234)
        cents = sizing(equity=123400, remaining_budget=876, loss_per_lot=23400)
        self.assertEqual(standard, cents)
        self.assertEqual(standard, 0.03)

    def test_decimal_inputs(self):
        self.assertEqual(sizing(equity=Decimal("10000"),
                                remaining_budget=Decimal("100")), 0.33)

    def test_many_caps_never_exceeded_and_volumes_on_origin_grid(self):
        for minimum, step in ((0.01, 0.01), (0.03, 0.02), (0.1, 0.25)):
            for budget in (0.01, 0.3, 3, 7.999999, 31, 99.999999, 100, 200):
                for maximum in (0.1, 0.33, 1, 10):
                    with self.subTest(minimum=minimum, step=step, budget=budget,
                                      maximum=maximum):
                        volume = Decimal(str(sizing(
                            volume_min=minimum, volume_step=step, volume_max=maximum,
                            remaining_budget=budget,
                        )))
                        self.assertLessEqual(volume * 300, min(Decimal(str(budget)), 100))
                        self.assertLessEqual(volume, Decimal(str(maximum)))
                        if volume:
                            self.assertGreaterEqual(volume, Decimal(str(minimum)))
                            self.assertEqual((volume - Decimal(str(minimum)))
                                             % Decimal(str(step)), 0)

    def test_float_conversion_does_not_cross_a_cap(self):
        # This decimal grid point rounds upward when converted to a float.
        self.assertEqual(sizing(
            equity=1, risk_fraction=1, remaining_budget=Decimal("0.100000000000000015"),
            loss_per_lot=1, volume_min=Decimal("0.100000000000000015"),
            volume_max=1, volume_step=Decimal("0.01"),
        ), 0)


class PriceLevelTests(unittest.TestCase):
    def test_atr_stops_entry_and_reward_use_rounded_risk(self):
        tick = Tick(bid=100, ask=100.03, time=0)
        self.assertEqual(price_levels("buy", tick, symbol(), atr=0.37),
                         (100.03, 99.45, 100.9))
        self.assertEqual(price_levels("sell", tick, symbol(), atr=0.37),
                         (100.0, 100.6, 99.1))

    def test_broker_stop_spread_and_extra_tick_dominate_atr(self):
        tick = Tick(bid=100, ask=100.2, time=0)
        spec = symbol(stops_level=30)
        self.assertEqual(price_levels("buy", tick, spec, atr=0.01),
                         (100.2, 99.65, 101.05))
        self.assertEqual(price_levels("sell", tick, spec, atr=0.01),
                         (100.0, 100.55, 99.15))

    def test_small_reward_still_obeys_broker_minimum(self):
        tick = Tick(bid=100, ask=100.2, time=0)
        for side in ("buy", "sell"):
            entry, sl, tp = price_levels(side, tick, symbol(stops_level=30),
                                         atr=0.01, reward_ratio=0.01)
            self.assertGreaterEqual(abs(tp - entry) + 1e-12, 0.55)
            self.assertGreaterEqual(abs(sl - entry) + 1e-12, 0.55)

    def test_outward_rounding_on_nondecimal_tick_grid(self):
        tick = Tick(bid=100.1, ask=100.15, time=1)
        spec = symbol(tick_size=0.25, stops_level=0)
        self.assertEqual(price_levels("buy", tick, spec, atr=0.21, reward_ratio=2),
                         (100.15, 99.75, 101.0))
        self.assertEqual(price_levels("sell", tick, spec, atr=0.21, reward_ratio=2),
                         (100.1, 100.5, 99.25))

    def test_exact_tick_boundaries_not_rounded_an_extra_tick(self):
        tick = Tick(bid=100, ask=100, time=1)
        for side, expected in (("buy", (100.0, 99.5, 101.0)),
                               ("sell", (100.0, 100.5, 99.0))):
            self.assertEqual(price_levels(side, tick, symbol(stops_level=0),
                                          atr=0.5, stop_atr=1, reward_ratio=2), expected)

    def test_forex_point_and_tick_are_distinct(self):
        spec = symbol(point=0.00001, digits=5, tick_size=0.0001, stops_level=20)
        tick = Tick(bid=1.10001, ask=1.10013, time=1)
        for side in ("buy", "sell"):
            entry, sl, tp = map(lambda value: Decimal(str(value)),
                               price_levels(side, tick, spec, atr=0.0001))
            self.assertEqual(sl % Decimal("0.0001"), 0)
            self.assertEqual(tp % Decimal("0.0001"), 0)
            self.assertGreaterEqual(abs(entry - sl), Decimal("0.00042"))
            self.assertGreaterEqual(abs(tp - entry), abs(entry - sl) * Decimal("1.5"))
            self.assertEqual(entry, Decimal(str(tick.ask if side == "buy" else tick.bid)))

    def test_invalid_side_and_parameters_raise(self):
        tick = Tick(bid=100, ask=100.03, time=0)
        for side in ("BUY", "short", "", None, 1):
            with self.subTest(side=side), self.assertRaises(ValueError):
                price_levels(side, tick, symbol(), 1)
        for field in ("atr", "stop_atr", "reward_ratio"):
            for value in (0, -1, math.nan, math.inf, -math.inf, None, "1", True):
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    args = dict(atr=1, stop_atr=1.5, reward_ratio=1.5)
                    args[field] = value
                    price_levels("buy", tick, symbol(), **args)

    def test_invalid_quotes_raise(self):
        tick = Tick(bid=100, ask=100.03, time=0)
        for field in ("bid", "ask"):
            for value in (0, -1, math.nan, math.inf, None, "100", True):
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    price_levels("buy", replace(tick, **{field: value}), symbol(), 1)
        with self.assertRaises(ValueError):
            price_levels("buy", replace(tick, bid=101), symbol(), 1)
        for time in (-1, math.inf, True, 0.5):
            with self.subTest(time=time), self.assertRaises(ValueError):
                price_levels("buy", replace(tick, time=time), symbol(), 1)

    def test_invalid_broker_metadata_raise(self):
        tick = Tick(bid=100, ask=100.03, time=0)
        for field in ("point", "tick_size"):
            for value in (0, -1, math.nan, math.inf, "1", None, True):
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    price_levels("buy", tick, symbol(**{field: value}), 1)
        for field in ("stops_level", "digits"):
            for value in (-1, math.inf, math.nan, None, True, 0.5):
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    price_levels("buy", tick, symbol(**{field: value}), 1)

    def test_nonpositive_or_unrepresentable_protection_raises(self):
        for side in ("buy", "sell"):
            with self.subTest(side=side), self.assertRaises(ValueError):
                price_levels(side, Tick(1, 1.1, 0), symbol(), atr=10)
            with self.subTest(side=side), self.assertRaises(ValueError):
                price_levels(side, Tick(1e308, 1e308, 0), symbol(), atr=1e308)


if __name__ == "__main__":
    unittest.main()

