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
            "orderId": "tpsl-1",
            "clientOid": client_oid,
            "status": "pending",
            "stopLoss": stop,
            "takeProfit": target,
        }
        return {"orderId": "tpsl-1", "clientOid": client_oid}

    def unfilled_strategy_orders(self):
        return [self.tpsl_order] if self.tpsl_order else []


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
                "stopLoss": "95",
                "takeProfit": "110",
            }
        ],
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

    assert result["ok"] is True
    assert result["live_positions"] == 1
    assert result["tracked_positions"] == 1


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
