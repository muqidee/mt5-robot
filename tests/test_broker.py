import importlib
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from bijisatu.broker import BrokerError, MT5Broker, OrderUncertain
from bijisatu.config import Config
from bijisatu.models import OrderPlan, SymbolSpec

try:
    # Import constants only. Never initialize or call the real terminal API.
    MT5 = importlib.import_module("MetaTrader5")
except ImportError:
    MT5 = None


@unittest.skipIf(MT5 is None, "MetaTrader5 is not installed; native-constant contract requires it")
class BrokerFixture(unittest.TestCase):
    def setUp(self):
        self.config = Config(account_login=123, broker_utc_offset_hours=0, commission_per_lot=0)
        self.broker = MT5Broker(self.config)
        self.mt5 = Mock(spec_set=MT5)
        for name in dir(MT5):
            if name.isupper():
                value = getattr(MT5, name)
                if isinstance(value, (int, float, str)):
                    setattr(self.mt5, name, value)
        self.broker.mt5 = self.mt5
        self.broker.identity = (123, "test-server")
        self.raw_account = SimpleNamespace(login=123, server="test-server", currency="USD",
                                           equity=10000, balance=10000, margin_free=10000,
                                           trade_allowed=True, trade_expert=True,
                                           trade_mode=MT5.ACCOUNT_TRADE_MODE_DEMO)
        self.mt5.account_info.return_value = self.raw_account
        self.mt5.terminal_info.return_value = SimpleNamespace(connected=True, trade_allowed=True,
                                                             tradeapi_disabled=False)
        self.mt5.positions_get.return_value = ()
        self.mt5.orders_get.return_value = ()
        self.mt5.symbol_select.return_value = True
        self.raw_symbol = SimpleNamespace(
            point=0.00001, digits=5, trade_tick_size=0.00001,
            volume_min=0.01, volume_max=100, volume_step=0.01, trade_stops_level=0,
            trade_mode=MT5.SYMBOL_TRADE_MODE_FULL,
            # Terminal bitmask: market orders (1), SL (16), TP (32).
            order_mode=1 | 16 | 32,
            filling_mode=1, trade_exemode=MT5.SYMBOL_TRADE_EXECUTION_MARKET,
        )
        self.mt5.symbol_info.return_value = self.raw_symbol
        self.mt5.symbol_info_tick.return_value = SimpleNamespace(bid=1.1, ask=1.1001, time=1791288000)
        self.mt5.order_calc_margin.return_value = 100
        self.mt5.order_check.return_value = SimpleNamespace(retcode=0)
        self.mt5.order_send.return_value = SimpleNamespace(retcode=MT5.TRADE_RETCODE_DONE,
                                                          order=10, deal=20, volume=0.1)
        self.plan = OrderPlan("EURUSD", "buy", 0.1, 1.1001, 1.0971, 1.1046, 35, 1791287940)

    def tearDown(self):
        self.mt5.initialize.assert_not_called()

    def raw_position(self, **changes):
        values = dict(ticket=1, symbol="GBPUSD", type=MT5.POSITION_TYPE_BUY,
                      volume=0.1, price_current=1.1, sl=1.09, magic=self.config.magic)
        values.update(changes)
        return SimpleNamespace(**values)


class BrokerNativeConstantsTests(BrokerFixture):
    def test_symbol_order_flags_work_with_installed_package(self):
        spec = self.broker.spec("EURUSD")
        self.assertEqual(spec.volume_min, 0.01)
        self.assertEqual(spec.point, 0.00001)

    def test_missing_required_symbol_order_capabilities_are_rejected(self):
        for missing in (1, 16, 32):
            with self.subTest(missing=missing):
                self.raw_symbol.order_mode = (1 | 16 | 32) & ~missing
                with self.assertRaises(BrokerError):
                    self.broker.send(self.plan)
                self.mt5.order_check.assert_not_called()
                self.mt5.order_send.assert_not_called()

    def test_installed_filling_mask_contract(self):
        # SYMBOL_FILLING_* are bit flags 1/2, not ORDER_FILLING_* enum values 0/1.
        # Keep the mock restricted to names actually exported by the installed package.
        cases = ((1, MT5.SYMBOL_TRADE_EXECUTION_MARKET, MT5.ORDER_FILLING_FOK),
                 (2, MT5.SYMBOL_TRADE_EXECUTION_MARKET, MT5.ORDER_FILLING_IOC),
                 (3, MT5.SYMBOL_TRADE_EXECUTION_MARKET, MT5.ORDER_FILLING_FOK),
                 (0, MT5.SYMBOL_TRADE_EXECUTION_INSTANT, MT5.ORDER_FILLING_RETURN))
        spec = SymbolSpec("EURUSD", 0.00001, 5, 0.00001, 0.01, 100, 0.01, 0)
        # Test filling independently of symbol-order flag compatibility above.
        with patch.object(self.broker, "spec", return_value=spec):
            for mask, execution, expected in cases:
                with self.subTest(mask=mask, execution=execution):
                    self.mt5.order_send.reset_mock()
                    self.raw_symbol.filling_mode = mask
                    self.raw_symbol.trade_exemode = execution
                    self.broker.send(self.plan)
                    self.mt5.order_send.assert_called_once()
                    self.assertEqual(self.mt5.order_send.call_args.args[0]["type_filling"], expected)


class BrokerTests(BrokerFixture):
    def test_filter_claim_callback_only_after_all_preflight_guards(self):
        callback = Mock()
        def before_send():
            self.mt5.order_check.assert_called_once()
            self.mt5.order_send.assert_not_called()
            callback()
        self.broker.send(self.plan, before_send=before_send)
        callback.assert_called_once()
        self.mt5.order_send.assert_called_once()

    def test_rejected_preflight_never_calls_filter_claim(self):
        callback = Mock()
        self.mt5.order_check.return_value = SimpleNamespace(retcode=1)
        with self.assertRaises(BrokerError):
            self.broker.send(self.plan, before_send=callback)
        callback.assert_not_called()
        self.mt5.order_send.assert_not_called()

    def test_filter_final_guard_failure_never_sends(self):
        callback = Mock(side_effect=BrokerError("Approval expired"))
        with self.assertRaises(BrokerError):
            self.broker.send(self.plan, before_send=callback)
        callback.assert_called_once()
        self.mt5.order_check.assert_called_once()
        self.mt5.order_send.assert_not_called()

    def test_market_execution_without_supported_mask_never_sends(self):
        self.raw_symbol.filling_mode = 0
        with self.assertRaises(BrokerError):
            self.broker.send(self.plan)
        self.mt5.order_check.assert_not_called()
        self.mt5.order_send.assert_not_called()

    def test_order_check_precedes_send_with_same_protective_request(self):
        self.broker.send(self.plan)
        self.mt5.order_check.assert_called_once()
        self.mt5.order_send.assert_called_once()
        request = self.mt5.order_check.call_args.args[0]
        self.assertEqual(self.mt5.order_send.call_args.args[0], request)
        self.assertLess(self.mt5.mock_calls.index(call.order_check(request)),
                        self.mt5.mock_calls.index(call.order_send(request)))
        for key, expected in dict(symbol=self.plan.symbol, volume=self.plan.volume,
                                  sl=self.plan.sl, tp=self.plan.tp, magic=self.config.magic,
                                  action=MT5.TRADE_ACTION_DEAL, type=MT5.ORDER_TYPE_BUY).items():
            self.assertEqual(request[key], expected)

    def test_rejected_or_missing_check_never_sends(self):
        for check in (None, SimpleNamespace(retcode=MT5.TRADE_RETCODE_INVALID_STOPS)):
            with self.subTest(check=check):
                self.mt5.order_check.return_value = check
                with self.assertRaises(BrokerError):
                    self.broker.send(self.plan)
                self.mt5.order_send.assert_not_called()

    def test_missing_or_uncertain_result_is_never_retried(self):
        results = [None] + [SimpleNamespace(retcode=code) for code in (
            MT5.TRADE_RETCODE_PLACED, MT5.TRADE_RETCODE_TIMEOUT, MT5.TRADE_RETCODE_CONNECTION,
        )]
        for result in results:
            with self.subTest(result=result):
                self.mt5.order_send.reset_mock()
                self.mt5.order_send.return_value = result
                with self.assertRaises(OrderUncertain):
                    self.broker.send(self.plan)
                self.mt5.order_send.assert_called_once()

    def test_send_exception_is_uncertain_and_not_retried(self):
        self.mt5.order_send.side_effect = RuntimeError("transport interrupted")
        with self.assertRaises(OrderUncertain):
            self.broker.send(self.plan)
        self.mt5.order_send.assert_called_once()

    def test_unknown_retcode_is_uncertain_and_not_retried(self):
        self.mt5.order_send.return_value = SimpleNamespace(retcode=987654321)
        with self.assertRaises(OrderUncertain):
            self.broker.send(self.plan)
        self.mt5.order_send.assert_called_once()

    def test_partial_fill_is_acknowledged_without_topping_up(self):
        self.mt5.order_send.return_value = SimpleNamespace(retcode=MT5.TRADE_RETCODE_DONE_PARTIAL,
                                                          order=10, deal=20, volume=0.05)
        self.broker.send(self.plan)
        self.mt5.order_send.assert_called_once()

    def test_account_identity_change_blocks_send(self):
        for field, value in (("login", 456), ("server", "other-server")):
            with self.subTest(field=field):
                raw = SimpleNamespace(**vars(self.raw_account))
                setattr(raw, field, value)
                self.mt5.account_info.return_value = raw
                with self.assertRaises(BrokerError):
                    self.broker.send(self.plan)
                self.mt5.order_send.assert_not_called()
                self.mt5.order_check.assert_not_called()

    def test_account_change_during_order_check_blocks_send(self):
        def check(request):
            self.mt5.account_info.return_value = SimpleNamespace(**dict(vars(self.raw_account), login=456))
            return SimpleNamespace(retcode=0)
        self.mt5.order_check.side_effect = check
        with self.assertRaises(BrokerError):
            self.broker.send(self.plan)
        self.mt5.order_check.assert_called_once()
        self.mt5.order_send.assert_not_called()

    def test_expected_login_is_checked_even_before_identity_is_pinned(self):
        self.broker.identity = None
        self.mt5.account_info.return_value = SimpleNamespace(**dict(vars(self.raw_account), login=456))
        with self.assertRaises(BrokerError):
            self.broker.account()

    def test_disabled_trading_blocks_order(self):
        self.raw_account.trade_expert = False
        with self.assertRaises(BrokerError):
            self.broker.send(self.plan)
        self.mt5.order_check.assert_not_called()
        self.mt5.order_send.assert_not_called()

    def test_existing_position_or_pending_order_blocks_send(self):
        self.mt5.positions_get.return_value = [SimpleNamespace(
            ticket=1, symbol="EURUSD", type=MT5.POSITION_TYPE_BUY, volume=0.1,
            price_current=1.1, sl=1.09, magic=self.config.magic,
        )]
        with self.assertRaises(BrokerError):
            self.broker.send(self.plan)
        self.mt5.positions_get.return_value = ()
        self.mt5.orders_get.return_value = [SimpleNamespace(ticket=2)]
        with self.assertRaises(BrokerError):
            self.broker.send(self.plan)
        self.mt5.order_send.assert_not_called()

    def test_send_reads_a_single_position_snapshot(self):
        self.mt5.positions_get.return_value = [self.raw_position()]
        self.broker.send(self.plan)
        self.mt5.positions_get.assert_called_once_with()
        self.mt5.order_send.assert_called_once()

    def test_maximum_positions_foreign_magic_or_missing_stop_block_send(self):
        cases = ([self.raw_position(ticket=index + 1, symbol=f"HELD{index}")
                  for index in range(self.config.max_positions)],
                 [self.raw_position(magic=self.config.magic + 1)],
                 [self.raw_position(sl=0)], [self.raw_position(sl=-1)])
        for positions in cases:
            with self.subTest(positions=positions):
                self.mt5.positions_get.reset_mock()
                self.mt5.positions_get.return_value = positions
                with self.assertRaises(BrokerError):
                    self.broker.send(self.plan)
                self.mt5.positions_get.assert_called_once_with()
                self.mt5.order_check.assert_not_called()
                self.mt5.order_send.assert_not_called()

    def test_nonfinite_position_stop_blocks_send(self):
        for stop in (float("nan"), float("inf")):
            with self.subTest(stop=stop):
                self.mt5.order_send.reset_mock()
                self.mt5.positions_get.return_value = [self.raw_position(sl=stop)]
                with self.assertRaises(BrokerError):
                    self.broker.send(self.plan)
                self.mt5.order_send.assert_not_called()

    def test_excessive_quote_movement_blocks_send(self):
        with self.assertRaises(BrokerError):
            self.broker.send(replace(self.plan, entry=1.11))
        self.mt5.order_check.assert_not_called()
        self.mt5.order_send.assert_not_called()

    def test_insufficient_margin_blocks_before_check(self):
        self.mt5.order_calc_margin.return_value = self.raw_account.margin_free * 0.81
        with self.assertRaises(BrokerError):
            self.broker.send(self.plan)
        self.mt5.order_check.assert_not_called()
        self.mt5.order_send.assert_not_called()

    def test_history_distinguishes_trades_cashflows_and_baseline_boundary(self):
        start = 1791288000
        def deal(kind, profit=0, timestamp=start + 1, **changes):
            values = dict(type=kind, profit=profit, commission=0, swap=0, fee=0,
                          time=timestamp, time_msc=timestamp * 1000)
            values.update(changes)
            return SimpleNamespace(**values)
        cases = (([], (False, False)),
                 ([deal(MT5.DEAL_TYPE_BUY, -50)], (True, False)),
                 ([deal(MT5.DEAL_TYPE_SELL, 10, commission=-1)], (True, False)),
                 ([deal(MT5.DEAL_TYPE_BALANCE, 100)], (False, True)),
                 ([deal(MT5.DEAL_TYPE_BALANCE, -100)], (False, True)),
                 ([deal(MT5.DEAL_TYPE_BALANCE, 100, timestamp=start)], (False, False)),
                 ([deal(MT5.DEAL_TYPE_BALANCE, fee=-1)], (False, True)))
        for deals, expected in cases:
            with self.subTest(deals=deals):
                self.mt5.history_deals_get.return_value = deals
                self.assertEqual(self.broker.activity(start, start + 60), expected)

    def test_unavailable_account_history_or_exposure_fails_closed(self):
        for method, api, args in ((self.broker.activity, self.mt5.history_deals_get, (1791288000, 1791288060)),
                                  (self.broker.positions, self.mt5.positions_get, ()),
                                  (self.broker.has_orders, self.mt5.orders_get, ())):
            with self.subTest(method=method.__name__):
                api.return_value = None
                with self.assertRaises(BrokerError):
                    method(*args)
        self.mt5.order_send.assert_not_called()

    def test_bars_request_closed_candles_only(self):
        self.mt5.copy_rates_from_pos.return_value = [dict(time=1791287940, open=1.1,
                                                        high=1.2, low=1.0, close=1.15, tick_volume=10)]
        for minutes, timeframe in ((1, MT5.TIMEFRAME_M1), (5, MT5.TIMEFRAME_M5)):
            with self.subTest(minutes=minutes):
                bars = self.broker.bars("EURUSD", minutes)
                self.mt5.copy_rates_from_pos.assert_called_with("EURUSD", timeframe, 1, 300)
                self.assertEqual(bars[0].time, 1791287940)
                self.assertEqual(bars[0].close, 1.15)


if __name__ == "__main__":
    unittest.main()

