from types import SimpleNamespace
from decimal import Decimal

from khtb_lab.execution import ExecutionEngine


def _ready_demo_engine():
    engine = ExecutionEngine.__new__(ExecutionEngine)
    engine.settings = SimpleNamespace(
        trading_env="DEMO",
        enable_exchange_orders=True,
        demo_armed=True,
        live_armed=False,
        leverage=2,
    )
    engine.environment = "DEMO"
    engine.ready = True
    engine.account = object()
    engine.exchange = None
    engine.positions_reconciled = True
    return engine


def test_prepare_accepts_a_valid_order_plan():
    engine = _ready_demo_engine()

    plan = engine.prepare("BTC/USDT", "LONG", 1, 100, 95, 110)

    assert plan["ready"] is True
    assert plan["error"] is None
    assert plan["leverage"] == 2


def test_prepare_rejects_invalid_long_levels():
    engine = _ready_demo_engine()

    plan = engine.prepare("BTC/USDT", "LONG", 1, 100, 105, 110)

    assert plan["ready"] is False
    assert plan["error"] == "invalid_long_stop_or_target"


class _PrecisionExchange:
    def fetch_ticker(self, symbol):
        return {"last": 100}

    def amount_to_precision(self, symbol, quantity):
        return "0.1234"

    def price_to_precision(self, symbol, price):
        return str(Decimal(str(price)).quantize(Decimal("0.1")))

    def market(self, symbol):
        return {"limits": {"amount": {"min": 0.001}}}


class _ExecutionAccount:
    def __init__(self, calls, reject_entry=False):
        self.calls = calls
        self.reject_entry = reject_entry
        self.tpsl_order = None
        self.positions = []
        self.ignore_empty_tp = False
        self.modify_error = None
        self.remove_order_on_modify_error = False

    def set_leverage(self, symbol, leverage):
        self.calls.append("leverage")

    def place_order(self, symbol, side, quantity, client_oid):
        self.calls.append(("entry", quantity))
        if self.reject_entry:
            raise RuntimeError(
                "Bitget HTTP 400: 25200 [param_validate_error] "
                "parameter validation error"
            )
        return {"orderId": "entry-1"}

    def place_tpsl_order(
        self, symbol, side, client_oid, stop, target, pos_side
    ):
        self.calls.append(("tpsl", stop, target))
        self.tpsl_order = {
            "symbol": symbol.replace("/", ""),
            "orderId": "tpsl-1",
            "clientOid": client_oid,
            "status": "pending",
            "stopLoss": stop,
            "takeProfit": target,
        }
        return {"orderId": "tpsl-1", "clientOid": client_oid}

    def unfilled_strategy_orders(self):
        return [self.tpsl_order] if self.tpsl_order else []

    def current_positions(self, symbol=None):
        return self.positions

    def modify_tpsl_order(
        self, symbol, order_id, client_oid, quantity, stop, target, pos_side
    ):
        self.calls.append(("modify-tpsl", stop, target))
        if self.modify_error:
            if self.remove_order_on_modify_error:
                self.tpsl_order = None
            raise self.modify_error
        self.tpsl_order["stopLoss"] = stop
        if target is not None or not self.ignore_empty_tp:
            self.tpsl_order["takeProfit"] = target
        return {"orderId": order_id, "clientOid": client_oid}

    def place_tpsl_order(
        self, symbol, side, client_oid, stop, target, pos_side
    ):
        self.calls.append((
            "restore-tpsl" if target is None else "tpsl",
            stop,
            target,
        ))
        self.tpsl_order = {
            "symbol": symbol.replace("/", ""),
            "orderId": "tpsl-restored",
            "clientOid": client_oid,
            "status": "live",
            "stopLoss": stop,
            "takeProfit": target,
        }
        return {
            "orderId": "tpsl-restored",
            "clientOid": client_oid,
        }


class _ProtectionPriceExchange:
    def __init__(self, marks):
        self.marks = iter(marks)

    def fetch_ticker(self, symbol):
        return {"mark": next(self.marks)}

    def price_to_precision(self, symbol, price):
        return str(Decimal(str(price)).quantize(Decimal("0.01")))

    def amount_to_precision(self, symbol, quantity):
        return str(quantity)


def _executable_engine(reject_entry=False):
    engine = _ready_demo_engine()
    engine.settings.max_entry_deviation_pct = 1
    engine.exchange = _PrecisionExchange()
    calls = []
    engine.account = _ExecutionAccount(calls, reject_entry=reject_entry)

    def reconcile_order(symbol, order_id, client_oid, seconds=12):
        calls.append("entry-filled")
        return order_id, {
            "orderStatus": "filled",
            "cumExecQty": "0.1234",
            "avgPrice": "100",
        }

    engine._reconcile_order = reconcile_order
    return engine, calls


def test_execute_rounds_values_and_places_tpsl_after_entry_fill():
    engine, calls = _executable_engine()
    plan = {
        "ready": True,
        "environment": "DEMO",
        "symbol": "BTC/USDT",
        "direction": "LONG",
        "quantity": 0.123456,
        "entry": 100,
        "stop": 95.67,
        "target": 110.04,
    }

    result = engine.execute(plan)

    assert result["success"] is True
    assert result["protection"]["ok"] is True
    assert result["stop"] == "95.7"
    assert result["target"] == "110.0"
    assert calls == [
        "leverage",
        ("entry", "0.1234"),
        "entry-filled",
        ("tpsl", "95.7", "110.0"),
    ]


def test_parameter_rejection_is_reported_without_order_reconciliation():
    engine, _ = _executable_engine(reject_entry=True)

    def unexpected_reconciliation(*args, **kwargs):
        raise AssertionError("Rejected parameters must not be reconciled")

    engine._reconcile_order = unexpected_reconciliation
    plan = {
        "ready": True,
        "environment": "DEMO",
        "symbol": "BTC/USDT",
        "direction": "LONG",
        "quantity": 0.123456,
        "entry": 100,
        "stop": 95,
        "target": 110,
    }

    result = engine.execute(plan)

    assert result["success"] is False
    assert result["reconcile_required"] is False
    assert "25200" in result["error"]


class _ReconciliationAccount:
    def __init__(self, positions, strategy_orders):
        self.positions = positions
        self.strategy_orders = strategy_orders

    def current_positions(self):
        return self.positions

    def unfilled_strategy_orders(self):
        return self.strategy_orders


def test_position_reconciliation_requires_matching_live_position_and_protection():
    engine = _ready_demo_engine()
    engine.account = _ReconciliationAccount(
        [
            {
                "symbol": "BTCUSDT",
                "holdSide": "long",
                "total": "0.1234",
            }
        ],
        [
            {
                "symbol": "BTCUSDT",
                "status": "live",
                "orderId": "tpsl-1",
                "clientOid": "tpsl-client",
                "stopLoss": "95",
                "takeProfit": "110",
            }
        ],
    )
    trade = {
        "id": 5,
        "symbol": "BTC/USDT:USDT",
        "direction": "LONG",
        "qty": 0.1234,
        "entry": 100,
        "stop": 95,
        "target": 110,
        "source": "DEMO",
        "protection_order_id": "tpsl-1",
        "protection_client_oid": "tpsl-client",
    }

    result = engine.reconcile_positions([trade])

    assert result["ok"] is True
    assert result["live_positions"] == 1
    assert result["tracked_positions"] == 1


def test_empty_exchange_state_is_a_successful_reconciliation():
    engine = _ready_demo_engine()
    engine.account = _ReconciliationAccount([], [])

    result = engine.reconcile_positions([])

    assert result["ok"] is True
    assert result["live_positions"] == 0
    assert result["live_position_keys"] == []
    assert engine.positions_reconciled is True


def test_position_reconciliation_reports_only_safer_stop_changes():
    engine = _ready_demo_engine()
    engine.account = _ReconciliationAccount(
        [{"symbol": "BTCUSDT", "holdSide": "long", "total": "0.1234"}],
        [{
            "symbol": "BTCUSDT",
            "status": "live",
            "orderId": "tpsl-1",
            "clientOid": "tracked-tpsl",
            "stopLoss": "100",
            "takeProfit": "110",
        }],
    )
    trade = {
        "id": 12,
        "symbol": "BTC/USDT:USDT",
        "direction": "LONG",
        "qty": 0.1234,
        "entry": 100,
        "stop": 95,
        "target": 110,
        "source": "DEMO",
    }

    result = engine.reconcile_positions([trade])

    assert result["ok"] is True
    assert result["protection_updates"] == [{
        "trade_id": 12,
        "stop": 100.0,
        "management_stage": 0,
        "favorable_price": 100.0,
        "order_id": "tpsl-1",
        "client_oid": "tracked-tpsl",
    }]


def test_position_reconciliation_fails_closed_on_missing_protection():
    engine = _ready_demo_engine()
    engine.account = _ReconciliationAccount(
        [{"symbol": "BTCUSDT", "holdSide": "long", "total": "0.1234"}],
        [],
    )
    trade = {
        "symbol": "BTC/USDT:USDT",
        "direction": "LONG",
        "qty": 0.1234,
        "stop": 95,
        "target": 110,
        "source": "DEMO",
    }

    result = engine.reconcile_positions([trade])

    assert result["ok"] is False
    assert result["mismatches"] == ["protection_unconfirmed:BTCUSDT:LONG"]


def test_reconciliation_recovers_confirmed_virtual_tp2_after_restart():
    engine = _ready_demo_engine()
    engine.account = _ReconciliationAccount(
        [{"symbol": "BTCUSDT", "holdSide": "long", "total": "0.5"}],
        [{
            "symbol": "BTCUSDT",
            "orderId": "tpsl-1",
            "clientOid": "tpsl-client",
            "status": "live",
            "stopLoss": "106",
            "takeProfit": "",
        }],
    )
    trade = {
        "id": 8,
        "symbol": "BTC/USDT",
        "direction": "LONG",
        "qty": 0.5,
        "entry": 100,
        "stop": 105,
        "target": 120,
        "management_stage": 1,
        "protection_order_id": "tpsl-1",
        "protection_client_oid": "tpsl-client",
        "source": "DEMO",
    }

    result = engine.reconcile_positions([trade])

    assert result["ok"] is True
    assert result["protection_updates"] == [{
        "trade_id": 8,
        "stop": 106.0,
        "management_stage": 2,
        "favorable_price": 100.0,
        "order_id": "tpsl-1",
        "client_oid": "tpsl-client",
    }]


def test_position_reconciliation_fails_closed_on_untracked_position():
    engine = _ready_demo_engine()
    engine.account = _ReconciliationAccount(
        [{"symbol": "BTCUSDT", "holdSide": "long", "total": "0.1234"}],
        [],
    )

    result = engine.reconcile_positions([])

    assert result["ok"] is False
    assert result["mismatches"] == [
        "untracked_exchange_position:BTCUSDT:LONG"
    ]


def test_exchange_execution_gate_requires_successful_position_reconciliation():
    engine = _ready_demo_engine()
    engine.positions_reconciled = False

    allowed, reason = engine.can_execute()

    assert allowed is False
    assert reason == "account_state_not_reconciled"


def test_tpsl_update_requires_unique_tracked_order_and_confirms_full_protection():
    engine = _ready_demo_engine()
    calls = []
    order = {
        "symbol": "BTCUSDT",
        "orderId": "tpsl-1",
        "clientOid": "tracked-tpsl",
        "status": "live",
        "stopLoss": "95",
        "takeProfit": "110",
    }
    engine.account = _ExecutionAccount(calls)
    engine.account.positions = [{
        "symbol": "BTCUSDT",
        "holdSide": "long",
        "total": "0.5",
    }]
    engine.account.tpsl_order = order
    trade = {
        "id": 7,
        "symbol": "BTC/USDT",
        "direction": "LONG",
        "qty": 0.5,
        "stop": 95,
        "target": 110,
        "management_stage": 1,
        "protection_order_id": "tpsl-1",
        "protection_client_oid": "tracked-tpsl",
    }

    result = engine.update_tpsl_protection(trade, 105, None)

    assert result["ok"] is True
    assert result["stop_confirmed"] is True
    assert result["target_confirmed"] is True
    assert order["stopLoss"] == 105
    assert order["takeProfit"] is None
    assert calls == [("modify-tpsl", 105, None)]
    assert engine.positions_reconciled is True


def test_tpsl_update_fails_closed_when_matching_protection_is_ambiguous():
    engine = _ready_demo_engine()
    calls = []
    engine.account = _ExecutionAccount(calls)
    engine.account.positions = [{
        "symbol": "BTCUSDT",
        "holdSide": "long",
        "total": "0.5",
    }]
    engine.account.tpsl_order = {
        "symbol": "BTCUSDT",
        "orderId": "tpsl-1",
        "clientOid": "tracked-tpsl",
        "status": "live",
        "stopLoss": "95",
        "takeProfit": "110",
    }
    engine.account.unfilled_strategy_orders = lambda: [
        engine.account.tpsl_order,
        {**engine.account.tpsl_order, "orderId": "tpsl-2"},
    ]
    trade = {
        "id": 7,
        "symbol": "BTC/USDT",
        "direction": "LONG",
        "qty": 0.5,
        "stop": 95,
        "target": 110,
        "management_stage": 1,
    }

    result = engine.update_tpsl_protection(trade, 105, 110)

    assert result == {
        "ok": False,
        "reason": "multiple_matching_protection_orders",
    }
    assert calls == []
    assert engine.positions_reconciled is False


def test_tpsl_update_fails_closed_if_exchange_does_not_remove_tp():
    engine = _ready_demo_engine()
    calls = []
    engine.account = _ExecutionAccount(calls)
    engine.account.positions = [{
        "symbol": "BTCUSDT",
        "holdSide": "long",
        "total": "0.5",
    }]
    engine.account.tpsl_order = {
        "symbol": "BTCUSDT",
        "orderId": "tpsl-1",
        "clientOid": "tracked-tpsl",
        "status": "live",
        "stopLoss": "95",
        "takeProfit": "110",
    }
    engine.account.ignore_empty_tp = True
    engine._reconcile_tpsl_order = lambda *args, **kwargs: {
        "ok": False,
        "reason": "updated_protection_not_confirmed",
    }
    trade = {
        "id": 7,
        "symbol": "BTC/USDT",
        "direction": "LONG",
        "entry": 100,
        "qty": 0.5,
        "stop": 95,
        "target": 110,
        "management_stage": 1,
        "protection_order_id": "tpsl-1",
        "protection_client_oid": "tracked-tpsl",
    }

    result = engine.update_tpsl_protection(trade, 105, None)

    assert result["ok"] is False
    assert engine.account.tpsl_order["stopLoss"] == 105
    assert engine.account.tpsl_order["takeProfit"] == "110"
    assert engine.positions_reconciled is False


def _short_protection_update_engine(mark_prices):
    engine = _ready_demo_engine()
    calls = []
    engine.account = _ExecutionAccount(calls)
    engine.account.positions = [{
        "symbol": "BTCUSDT",
        "holdSide": "short",
        "total": "0.5",
    }]
    engine.account.tpsl_order = {
        "symbol": "BTCUSDT",
        "orderId": "tpsl-1",
        "clientOid": "tracked-tpsl",
        "status": "live",
        "stopLoss": "105",
        "takeProfit": "90",
    }
    engine.exchange = _ProtectionPriceExchange(mark_prices)
    trade = {
        "id": 7,
        "symbol": "BTC/USDT",
        "direction": "SHORT",
        "qty": 0.5,
        "entry": 100,
        "stop": 105,
        "target": 90,
        "management_stage": 1,
        "protection_order_id": "tpsl-1",
        "protection_client_oid": "tracked-tpsl",
    }
    return engine, calls, trade


def test_short_tpsl_update_is_skipped_when_new_stop_is_at_or_below_mark():
    engine, calls, trade = _short_protection_update_engine(["99.67"])

    result = engine.update_tpsl_protection(trade, 99.66, 90)

    assert result["ok"] is False
    assert result["protection_preserved"] is True
    assert float(result["actual_stop"]) == 105
    assert float(engine.account.tpsl_order["stopLoss"]) == 105
    assert engine.positions_reconciled is True
    assert calls == []


def test_short_mark_price_race_keeps_old_confirmed_stop_after_rejection():
    engine, calls, trade = _short_protection_update_engine(
        ["99.65", "99.67"]
    )
    engine.account.modify_error = RuntimeError(
        "Bitget HTTP 400: 25592 stop-loss trigger price must be greater "
        "than the mark price"
    )

    result = engine.update_tpsl_protection(trade, 99.66, 90)

    assert result["ok"] is False
    assert result["protection_preserved"] is True
    assert result["actual_stop"] == "105"
    assert engine.account.tpsl_order["stopLoss"] == "105"
    assert engine.positions_reconciled is True
    assert calls == [("modify-tpsl", 99.66, 90)]


def test_missing_old_protection_is_replaced_with_confirmed_stop_only_order():
    engine, calls, trade = _short_protection_update_engine(
        ["99.65", "99.65"]
    )
    engine.account.modify_error = RuntimeError(
        "Bitget HTTP 400: 25592 stop-loss trigger price must be greater "
        "than the mark price"
    )
    engine.account.remove_order_on_modify_error = True

    result = engine.update_tpsl_protection(trade, 99.66, 90)

    assert result["ok"] is False
    assert result["protection_preserved"] is True
    assert result["protection_restored"] is True
    assert float(result["actual_stop"]) == 105
    assert result["actual_target"] is None
    assert float(engine.account.tpsl_order["stopLoss"]) == 105
    assert engine.account.tpsl_order["takeProfit"] is None
    assert engine.positions_reconciled is True
    assert calls == [
        ("modify-tpsl", 99.66, 90),
        ("restore-tpsl", "105.00", None),
    ]


def test_failed_position_reconciliation_resets_execution_gate():
    engine = _ready_demo_engine()
    engine.account = _ReconciliationAccount([], [])
    engine.account.current_positions = lambda: (_ for _ in ()).throw(
        RuntimeError("account unavailable")
    )

    result = engine.reconcile_positions([])

    assert result["ok"] is False
    assert engine.positions_reconciled is False
    assert engine.can_execute() == (False, "account_state_not_reconciled")


def test_individual_close_checks_exchange_state_before_submission():
    engine = _ready_demo_engine()
    engine.settings.position_mode = "one_way"

    class _AmbiguousAccount:
        def close_all(self, symbol=None):
            raise AssertionError("Ambiguous close must not submit")

        def current_positions(self, symbol=None):
            return [
                {"symbol": "BTCUSDT", "holdSide": "long", "total": "0.5"},
                {"symbol": "BTCUSDT", "holdSide": "short", "total": "0.2"},
            ]

    engine.account = _AmbiguousAccount()
    result = engine.close_position({
        "id": 7,
        "symbol": "BTC/USDT",
        "direction": "LONG",
        "qty": 0.5,
    })

    assert result["success"] is False
    assert result["error"] == "symbol_position_is_ambiguous"


def test_individual_close_requires_one_way_position_and_exchange_confirmation():
    engine = _ready_demo_engine()
    engine.settings.position_mode = "one_way"

    class _CloseAccount:
        def __init__(self):
            self.calls = 0

        def current_positions(self, symbol=None):
            self.calls += 1
            if self.calls == 1:
                return [{
                    "symbol": "BTCUSDT",
                    "holdSide": "long",
                    "total": "0.5",
                }]
            return []

        def close_all(self, symbol=None):
            assert symbol == "BTC/USDT"
            return {"success": True}

    engine.account = _CloseAccount()
    trade = {
        "id": 7,
        "symbol": "BTC/USDT",
        "direction": "LONG",
        "qty": 0.5,
    }

    result = engine.close_position(trade, timeout=1)

    assert result == {"success": True, "already_closed": False}
    assert engine.account.calls == 2


def test_individual_close_is_blocked_for_hedge_mode():
    engine = _ready_demo_engine()
    engine.settings.position_mode = "hedge"

    result = engine.close_position({
        "id": 1,
        "symbol": "BTC/USDT",
        "direction": "LONG",
        "qty": 1,
    })

    assert result["success"] is False
    assert result["error"] == "individual_close_not_supported_in_hedge_mode"
