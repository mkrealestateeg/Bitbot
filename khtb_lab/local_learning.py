from __future__ import annotations

import hashlib
import json
import logging
import math
from datetime import datetime, timezone

log = logging.getLogger(__name__)

FEATURE_NAMES = (
    "technical_score",
    "direction",
    "rsi",
    "macd_atr",
    "macd_signal_atr",
    "adx",
    "atr_pct",
    "volume_ratio",
    "stop_distance_pct",
    "reward_risk",
    "trend_1h",
    "trend_4h",
    "setup_momentum",
    "setup_range",
    "setup_brr",
)
MODEL_VERSION = 1
MIN_SAMPLES = 30
MIN_CLASS_SAMPLES = 8


def _number(value, default=0.0):
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def _bounded(value, low, high):
    return max(low, min(high, value))


def _trend_value(value):
    normalized = str(value or "").upper()
    if normalized in {"UP", "LONG", "BULLISH"}:
        return 1.0
    if normalized in {"DOWN", "SHORT", "BEARISH"}:
        return -1.0
    return 0.0


def feature_vector(signal):
    """Convert the entry-time strategy snapshot to a stable numeric vector."""
    snapshot = signal.get("snapshot") or {}
    if not isinstance(snapshot, dict):
        snapshot = {}

    entry = _number(signal.get("price"))
    stop = _number(signal.get("stop"))
    target = _number(signal.get("target"))
    atr = _number(snapshot.get("atr"))
    macd = _number(snapshot.get("macd"))
    macd_signal = _number(snapshot.get("macd_signal"))
    direction = str(signal.get("direction") or "").upper()
    setup = str(signal.get("setup") or "").upper()

    risk = abs(entry - stop)
    reward = abs(target - entry)
    return [
        _bounded(_number(signal.get("score")) / 100.0, 0.0, 1.0),
        1.0 if direction == "LONG" else -1.0 if direction == "SHORT" else 0.0,
        _bounded(_number(snapshot.get("rsi"), 50.0) / 100.0, 0.0, 1.0),
        _bounded(macd / atr / 5.0 if atr > 0 else 0.0, -1.0, 1.0),
        _bounded(
            macd_signal / atr / 5.0 if atr > 0 else 0.0,
            -1.0,
            1.0,
        ),
        _bounded(_number(snapshot.get("adx")) / 100.0, 0.0, 1.0),
        _bounded(atr / entry if entry > 0 else 0.0, 0.0, 0.1),
        _bounded(_number(snapshot.get("volume_ratio")) / 5.0, 0.0, 1.0),
        _bounded(risk / entry if entry > 0 else 0.0, 0.0, 0.2),
        _bounded(reward / risk if risk > 0 else 0.0, 0.0, 10.0) / 10.0,
        _trend_value(snapshot.get("trend_1h")),
        _trend_value(snapshot.get("trend_4h")),
        1.0 if setup == "MOMENTUM" else 0.0,
        1.0 if setup == "HORIZONTAL_RANGE" else 0.0,
        1.0 if setup == "BRR" else 0.0,
    ]


def capture_entry_snapshot(signal, source):
    """Keep raw strategy context and a versioned local training vector."""
    snapshot = signal.get("snapshot")
    stored = dict(snapshot) if isinstance(snapshot, dict) else {}
    stored["local_learning"] = {
        "schema_version": MODEL_VERSION,
        "source": str(source).upper(),
        "direction": str(signal.get("direction") or "").upper(),
        "setup": str(signal.get("setup") or "").upper(),
        "score": _number(signal.get("score")),
        "entry": _number(signal.get("price")),
        "stop": _number(signal.get("stop")),
        "target": _number(signal.get("target")),
        "feature_names": list(FEATURE_NAMES),
        "features": feature_vector(signal),
    }
    return stored


def _sigmoid(value):
    value = _bounded(value, -35.0, 35.0)
    return 1.0 / (1.0 + math.exp(-value))


def _fit_logistic(features, labels):
    dimensions = len(FEATURE_NAMES)
    means = [
        sum(row[index] for row in features) / len(features)
        for index in range(dimensions)
    ]
    scales = []
    for index, mean in enumerate(means):
        variance = sum(
            (row[index] - mean) ** 2 for row in features
        ) / len(features)
        scale = math.sqrt(variance)
        scales.append(scale if scale > 1e-9 else 1.0)

    normalized = [
        [(value - means[index]) / scales[index]
         for index, value in enumerate(row)]
        for row in features
    ]
    weights = [0.0] * (dimensions + 1)
    learning_rate = 0.12
    regularization = 0.01

    for _ in range(700):
        gradients = [0.0] * len(weights)
        for row, label in zip(normalized, labels):
            prediction = _sigmoid(
                weights[0] + sum(
                    weight * value
                    for weight, value in zip(weights[1:], row)
                )
            )
            error = prediction - label
            gradients[0] += error
            for index, value in enumerate(row, start=1):
                gradients[index] += error * value

        count = len(labels)
        weights[0] -= learning_rate * gradients[0] / count
        for index in range(1, len(weights)):
            gradient = gradients[index] / count + regularization * weights[index]
            weights[index] -= learning_rate * gradient

    return {
        "version": MODEL_VERSION,
        "feature_names": list(FEATURE_NAMES),
        "means": means,
        "scales": scales,
        "weights": weights,
        "samples": len(labels),
        "wins": sum(labels),
        "losses": len(labels) - sum(labels),
        "trained_at": datetime.now(timezone.utc).isoformat(),
    }


class LocalTradeLearner:
    """Persist an interpretable, advisory-only model per trade environment."""

    def __init__(self, database):
        self.db = database

    @staticmethod
    def _key(source):
        return f"local_trade_model:{str(source).upper()}"

    def _load(self, source):
        raw = self.db.get_value(self._key(source))
        if not raw:
            return {}
        try:
            state = json.loads(raw)
            return state if isinstance(state, dict) else {}
        except (TypeError, ValueError):
            log.error(
                "LOCAL MODEL STATE INVALID | source=%s",
                str(source).upper(),
            )
            return {}

    def refresh(self, source):
        source = str(source).upper()
        examples = self.db.local_learning_examples(source)
        fingerprint_input = [
            (row["id"], row["pnl"], row["learning"]["features"])
            for row in examples
        ]
        fingerprint = hashlib.sha256(
            json.dumps(
                fingerprint_input,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

        state = self._load(source)
        if state.get("dataset_hash") == fingerprint:
            return state

        labels = [1 if float(row["pnl"]) > 0 else 0 for row in examples]
        wins = sum(labels)
        losses = len(labels) - wins
        if len(labels) < MIN_SAMPLES or min(wins, losses) < MIN_CLASS_SAMPLES:
            state = {
                "version": MODEL_VERSION,
                "status": "WARMING",
                "samples": len(labels),
                "wins": wins,
                "losses": losses,
                "required_samples": MIN_SAMPLES,
                "required_per_class": MIN_CLASS_SAMPLES,
                "dataset_hash": fingerprint,
            }
        else:
            features = [
                [float(value) for value in row["learning"]["features"]]
                for row in examples
            ]
            state = _fit_logistic(features, labels)
            state.update({
                "status": "READY",
                "dataset_hash": fingerprint,
                "advisory_only": True,
            })

        self.db.set_value(
            self._key(source),
            json.dumps(state, separators=(",", ":")),
        )
        log.info(
            "LOCAL MODEL REFRESHED | source=%s | status=%s | samples=%s | "
            "wins=%s | losses=%s",
            source,
            state["status"],
            state["samples"],
            state["wins"],
            state["losses"],
        )
        return state

    def predict(self, source, signal):
        state = self._load(source)
        if state.get("status") != "READY":
            return None
        if (
            state.get("version") != MODEL_VERSION
            or state.get("feature_names") != list(FEATURE_NAMES)
        ):
            return None

        means = state.get("means")
        scales = state.get("scales")
        weights = state.get("weights")
        expected_dimensions = len(FEATURE_NAMES)
        if (
            not isinstance(means, list)
            or not isinstance(scales, list)
            or not isinstance(weights, list)
            or len(means) != expected_dimensions
            or len(scales) != expected_dimensions
            or len(weights) != expected_dimensions + 1
        ):
            log.error(
                "LOCAL MODEL PARAMETERS INVALID | source=%s",
                str(source).upper(),
            )
            return None
        try:
            means = [float(value) for value in means]
            scales = [float(value) for value in scales]
            weights = [float(value) for value in weights]
        except (TypeError, ValueError):
            log.error(
                "LOCAL MODEL PARAMETERS INVALID | source=%s",
                str(source).upper(),
            )
            return None
        if (
            any(not math.isfinite(value) for value in means + scales + weights)
            or any(value <= 0 for value in scales)
        ):
            log.error(
                "LOCAL MODEL PARAMETERS INVALID | source=%s",
                str(source).upper(),
            )
            return None

        features = feature_vector(signal)
        normalized = [
            (value - mean) / scale
            for value, mean, scale in zip(
                features, means, scales
            )
        ]
        probability = _sigmoid(
            weights[0]
            + sum(
                weight * value
                for weight, value in zip(
                    weights[1:], normalized
                )
            )
        )
        return {
            "win_probability": probability,
            "samples": int(state["samples"]),
            "source": str(source).upper(),
            "advisory_only": True,
        }

    def status(self, source):
        state = self._load(source)
        if not state:
            return {
                "status": "WARMING",
                "samples": 0,
                "wins": 0,
                "losses": 0,
                "required_samples": MIN_SAMPLES,
                "required_per_class": MIN_CLASS_SAMPLES,
            }
        return {
            key: state[key]
            for key in (
                "status",
                "samples",
                "wins",
                "losses",
                "required_samples",
                "required_per_class",
                "trained_at",
            )
            if key in state
        }
