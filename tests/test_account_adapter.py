import logging
from types import SimpleNamespace

import pytest

from khtb_lab.account_adapter import BitgetUTAAdapter


def _adapter(position_mode="one_way"):
    adapter = BitgetUTAAdapter.__new__(BitgetUTAAdapter)
    adapter.settings = SimpleNamespace(
        product_category="USDT-FUTURES",
        margin_mode="crossed",
        position_mode=position_mode,
    )
    requests = []

    def signed_request(method, path, params=None, body=None):
        requests.append((method, path, params, body))
        if path.endswith("place-order"):
            return {"orderId": "entry-1"}
        if path.endswith("place-strategy-order"):
            return {"orderId": "tpsl-1"}
        return []

    adapter._signed_request = signed_request
    return adapter, requests


def test_market_order_is_submitted_without_attached_protection_fields():
    adapter, requests = _adapter()

    result = adapter.place_order(
        "BTC/USDT:USDT",
        "buy",
        "0.1234",
        "entry-oid",
    )

    assert result["orderId"] == "entry-1"
    method, path, _, body = requests[0]
    assert method == "POST"
    assert path == "/api/v3/trade/place-order"
    assert body["qty"] == "0.1234"
    assert "stopLoss" not in body
    assert "takeProfit" not in body


def test_tpsl_strategy_order_is_separate_and_reduce_only_in_one_way_mode():
    adapter, requests = _adapter()

    result = adapter.place_tpsl_order(
        "BTC/USDT:USDT",
        side="sell",
        client_oid="tpsl-oid",
        stop="95.7",
        target="110.0",
        pos_side="long",
    )

    assert result["orderId"] == "tpsl-1"
    method, path, _, body = requests[0]
    assert method == "POST"
    assert path == "/api/v3/trade/place-strategy-order"
    assert body["type"] == "tpsl"
    assert body["tpslMode"] == "full"
    assert body["reduceOnly"] == "yes"
    assert body["stopLoss"] == "95.7"
    assert body["takeProfit"] == "110.0"
    assert "posSide" not in body


def test_tpsl_strategy_order_identifies_hedge_position_side():
    adapter, requests = _adapter(position_mode="hedge")

    adapter.place_tpsl_order(
        "BTC/USDT:USDT",
        side="buy",
        client_oid="tpsl-oid",
        stop="110",
        target="90",
        pos_side="short",
    )

    body = requests[0][3]
    assert body["side"] == "buy"
    assert body["posSide"] == "short"
    assert "reduceOnly" not in body


def test_current_positions_rejects_unexpected_response_shape():
    adapter, _ = _adapter()
    adapter._signed_request = lambda *args, **kwargs: {}

    with pytest.raises(RuntimeError, match="unexpected current-position"):
        adapter.current_positions()


@pytest.mark.parametrize(
    "response",
    [
        None,
        [],
        {"list": None},
        {"positions": None},
        {"positionList": None},
        {"list": []},
        {"positions": []},
    ],
)
def test_current_positions_accepts_successful_empty_position_responses(
    response,
):
    adapter, _ = _adapter()
    adapter._signed_request = lambda *args, **kwargs: response

    assert adapter.current_positions() == []


def test_current_positions_accepts_position_list_wrapper():
    adapter, _ = _adapter()
    positions = [{"symbol": "BTCUSDT", "holdSide": "long", "total": "1"}]
    adapter._signed_request = lambda *args, **kwargs: {"list": positions}

    assert adapter.current_positions() == positions


def test_invalid_position_response_logs_shape_without_values(caplog):
    adapter, _ = _adapter()
    adapter._signed_request = lambda *args, **kwargs: {
        "unexpected": [{"apiKey": "must-not-log"}],
    }

    with caplog.at_level(logging.ERROR):
        with pytest.raises(RuntimeError, match="unexpected current-position"):
            adapter.current_positions()

    assert "BITGET CURRENT POSITIONS RESPONSE SHAPE INVALID" in caplog.text
    assert "unexpected" in caplog.text
    assert "must-not-log" not in caplog.text


def test_unfilled_strategy_orders_rejects_unexpected_response_shape():
    adapter, _ = _adapter()
    adapter._signed_request = lambda *args, **kwargs: {}

    with pytest.raises(RuntimeError, match="unexpected strategy-order"):
        adapter.unfilled_strategy_orders()


@pytest.mark.parametrize("response", [None, [], {"list": []}])
def test_unfilled_strategy_orders_accepts_empty_success_responses(response):
    adapter, _ = _adapter()
    adapter._signed_request = lambda *args, **kwargs: response

    assert adapter.unfilled_strategy_orders() == []


def test_entry_order_rejection_logs_safe_parameters_not_credentials(caplog):
    adapter = BitgetUTAAdapter.__new__(BitgetUTAAdapter)
    adapter.settings = SimpleNamespace(
        request_timeout=10,
    )
    adapter.base_url = "https://api.example.test"
    adapter.environment = "DEMO"
    adapter.api_key = "private-api-key"
    adapter.api_secret = "private-api-secret"
    adapter.passphrase = "private-passphrase"
    adapter.session = SimpleNamespace(
        request=lambda *args, **kwargs: SimpleNamespace(
            status_code=400,
            json=lambda: {"code": "25200", "msg": "parameter validation"},
        )
    )

    with caplog.at_level(logging.ERROR):
        with pytest.raises(RuntimeError, match="25200"):
            adapter._signed_request(
                "POST",
                "/api/v3/trade/place-order",
                body={
                    "category": "USDT-FUTURES",
                    "symbol": "AVAXUSDT",
                    "qty": "1.18",
                    "side": "sell",
                    "orderType": "market",
                    "clientOid": "khtb-test-order",
                    "reduceOnly": "no",
                    "marginMode": "crossed",
                },
            )

    assert "BITGET ENTRY ORDER REJECTED" in caplog.text
    assert "'qty': '1.18'" in caplog.text
    assert "private-api-key" not in caplog.text
    assert "private-api-secret" not in caplog.text
    assert "private-passphrase" not in caplog.text
