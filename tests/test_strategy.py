import math
import unittest
from dataclasses import replace

from bijisatu.models import Bar
from bijisatu.strategy import evaluate


def candle(time, close, open_price=None):
    open_price = close if open_price is None else open_price
    return Bar(time, open_price, max(open_price, close) + 0.2,
               min(open_price, close) - 0.2, close, 10)


def setup_bars(side="buy"):
    m5 = [candle(index * 300, 80 + index * 0.2) for index in range(100)]
    m1 = [candle(24000 + index * 60, 100) for index in range(100)]
    m1[-2] = candle(m1[-2].time, 99, 100)
    m1[-1] = candle(m1[-1].time, 101, 99.2)
    if side == "sell":
        def reflect(bar):
            return Bar(bar.time, 200 - bar.open, 200 - bar.low,
                       200 - bar.high, 200 - bar.close, bar.tick_volume)
        m1 = [reflect(bar) for bar in m1]
        m5 = [reflect(bar) for bar in m5]
    return m1, m5


class StrategyTests(unittest.TestCase):
    def test_symmetric_trend_pullback_reclaim(self):
        results = []
        for side in ("buy", "sell"):
            with self.subTest(side=side):
                m1, m5 = setup_bars(side)
                signal = evaluate(m1, m5)
                self.assertIsNotNone(signal)
                self.assertEqual(signal.side, side)
                self.assertEqual(signal.bar_time, m1[-1].time)
                self.assertGreater(signal.atr, 0)
                self.assertTrue(math.isfinite(signal.atr))
                self.assertIn("EMA20/50", signal.reason)
                self.assertIn("pullback/reclaim", signal.reason)
                results.append(signal.atr)
        self.assertAlmostEqual(*results)

    def test_wilder_atr_includes_gaps_and_latest_closed_candle(self):
        m1, m5 = setup_bars()
        m1[80] = candle(m1[80].time, 106, 105)
        ranges = [m1[0].high - m1[0].low]
        for index in range(1, len(m1)):
            bar = m1[index]
            previous_close = m1[index - 1].close
            ranges.append(max(bar.high - bar.low, abs(bar.high - previous_close),
                              abs(bar.low - previous_close)))
        expected = sum(ranges[:14]) / 14
        for true_range in ranges[14:]:
            expected = (expected * 13 + true_range) / 14
        self.assertAlmostEqual(evaluate(m1, m5).atr, expected)

    def test_insufficient_history(self):
        m1, m5 = setup_bars()
        for first, second in (([], m5), (m1, []), (m1[-99:], m5), (m1, m5[-99:])):
            with self.subTest(m1=len(first), m5=len(second)):
                self.assertIsNone(evaluate(first, second))

    def test_flat_or_choppy_m5_has_no_signal(self):
        m1, m5 = setup_bars()
        for closes in ([100] * 100, [100 + (-1) ** index for index in range(100)]):
            with self.subTest(last=closes[-1]):
                bars = [candle(bar.time, close) for bar, close in zip(m5, closes)]
                self.assertIsNone(evaluate(m1, bars))

    def test_opposite_trend_has_no_signal(self):
        buy_m1, buy_m5 = setup_bars()
        sell_m1, sell_m5 = setup_bars("sell")
        self.assertIsNone(evaluate(buy_m1, sell_m5))
        self.assertIsNone(evaluate(sell_m1, buy_m5))

    def test_no_pullback_no_reclaim_and_wrong_candle_direction(self):
        for side in ("buy", "sell"):
            direction = 1 if side == "buy" else -1
            for scenario in ("no_pullback", "no_reclaim", "wrong_body", "doji"):
                with self.subTest(side=side, scenario=scenario):
                    m1, m5 = setup_bars(side)
                    if scenario == "no_pullback":
                        m1[-2] = candle(m1[-2].time, 100 + direction * 0.5)
                    elif scenario == "no_reclaim":
                        m1[-1] = candle(m1[-1].time, 100 - direction * 0.5,
                                        100 - direction)
                    elif scenario == "wrong_body":
                        m1[-1] = candle(m1[-1].time, 100 + direction,
                                        100 + direction * 2)
                    else:
                        m1[-1] = candle(m1[-1].time, 100 + direction)
                    self.assertIsNone(evaluate(m1, m5))

    def test_touch_of_previous_ema_counts_as_pullback(self):
        for side in ("buy", "sell"):
            m1, m5 = setup_bars(side)
            m1[-2] = candle(m1[-2].time, 100)
            self.assertEqual(evaluate(m1, m5).side, side)

    def test_future_m5_does_not_change_signal(self):
        for side in ("buy", "sell"):
            m1, m5 = setup_bars(side)
            expected = evaluate(m1, m5)
            future = [candle(30000 + index * 300, 20 + index) for index in range(100)]
            self.assertEqual(evaluate(m1, m5 + future), expected)
            # Even an already opened M5 candle is unavailable until its close.
            self.assertEqual(evaluate(m1, m5 + [candle(29760, 500)]), expected)
            self.assertEqual(evaluate(m1, m5 + [replace(future[0], close=math.nan)]),
                             expected)

    def test_future_m5_does_not_count_toward_warmup(self):
        m1, m5 = setup_bars()
        m5[-1] = replace(m5[-1], time=m5[-1].time + 1)
        self.assertIsNone(evaluate(m1, m5))

    def test_exact_m5_close_cutoff_is_included(self):
        m1, m5 = setup_bars()
        self.assertEqual(m5[-1].time + 300, m1[-1].time + 60)
        self.assertIsNotNone(evaluate(m1, m5))
        m1 = [replace(bar, time=bar.time - 1) for bar in m1]
        self.assertIsNone(evaluate(m1, m5))

    def test_nonfinite_and_nonnumeric_data_fail_closed(self):
        for series in (0, 1):
            for field in ("open", "high", "low", "close", "tick_volume"):
                for value in (math.nan, math.inf, -math.inf, "100", None, True, 10 ** 1000):
                    with self.subTest(series=series, field=field, value=value):
                        bars = list(setup_bars())
                        bars[series][70] = replace(bars[series][70], **{field: value})
                        self.assertIsNone(evaluate(*bars))

    def test_invalid_candles_fail_closed(self):
        for changes in ({"high": 1}, {"low": 200}, {"low": 0},
                        {"close": -1}, {"tick_volume": -1}):
            for series in (0, 1):
                with self.subTest(series=series, changes=changes):
                    bars = list(setup_bars())
                    bars[series][70] = replace(bars[series][70], **changes)
                    self.assertIsNone(evaluate(*bars))

    def test_timestamps_must_be_nonnegative_integers_strictly_increasing(self):
        for series in (0, 1):
            for value in (-1, math.nan, math.inf, "100", None, True, 100.5):
                with self.subTest(series=series, time=value):
                    bars = list(setup_bars())
                    bars[series][70] = replace(bars[series][70], time=value)
                    self.assertIsNone(evaluate(*bars))
            for offset in (0, -1):
                bars = list(setup_bars())
                bars[series][70] = replace(bars[series][70],
                                          time=bars[series][69].time + offset)
                self.assertIsNone(evaluate(*bars))

    def test_inputs_are_not_mutated_and_results_are_repeatable(self):
        m1, m5 = setup_bars()
        original = (list(m1), list(m5))
        first = evaluate(m1, m5)
        self.assertEqual(evaluate(m1, m5), first)
        self.assertEqual((m1, m5), original)


if __name__ == "__main__":
    unittest.main()

