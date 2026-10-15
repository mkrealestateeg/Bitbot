
from __future__ import annotations

import json
import logging
import math
import time

log = logging.getLogger(__name__)


class AIReviewer:
    """
    Reviews technically qualified opportunities using Gemini.

    Cooldown applies only after a successful AI response for the
    same symbol and materially similar opportunity.

    Failed requests never become approvals and never start cooldown.
    """

    def __init__(self, settings):
        self.settings = settings
        self.keys = list(settings.gemini_api_pool)
        self.index = 0

        # symbol -> {"time": timestamp, "signature": tuple}
        # Stored in memory for the lifetime of this process.
        self.last = {}

    @staticmethod
    def _result(
        decision,
        confidence=0,
        comment="",
        status="OK",
    ):
        return {
            "decision": decision,
            "confidence": float(confidence),
            "comment": str(comment)[:300],
            "status": status,
        }

    @staticmethod
    def _number(value):
        try:
            number = float(value)
            return number if math.isfinite(number) else None
        except (TypeError, ValueError):
            return None

    @classmethod
    def _opportunity_signature(cls, signal):
        """
        Build a stable fingerprint for a qualified opportunity.

        Small price fluctuations alone should not bypass cooldown.
        A change in direction, setup, score band, or risk/target
        distances can represent a materially different opportunity.
        """
        direction = str(
            signal.get("direction", "")
        ).upper().strip()

        setup = str(
            signal.get("setup", "")
        ).upper().strip()

        score = cls._number(signal.get("score"))
        entry = cls._number(signal.get("price"))
        stop = cls._number(signal.get("stop"))
        target = cls._number(signal.get("target"))

        score_band = (
            int(score // 10)
            if score is not None
            else None
        )

        if entry is not None and entry > 0:
            stop_distance_pct = (
                round(abs(entry - stop) / entry * 100, 1)
                if stop is not None
                else None
            )

            target_distance_pct = (
                round(abs(target - entry) / entry * 100, 1)
                if target is not None
                else None
            )
        else:
            stop_distance_pct = None
            target_distance_pct = None

        return (
            direction,
            setup,
            score_band,
            stop_distance_pct,
            target_distance_pct,
        )

    @staticmethod
    def _parse_response(text):
        text = (text or "").strip()

        # Handle responses wrapped in Markdown code fences.
        if text.startswith("```"):
            lines = text.splitlines()

            if lines and lines[0].startswith("```"):
                lines = lines[1:]

            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]

            text = "\n".join(lines).strip()

        data = json.loads(text)

        if not isinstance(data, dict):
            raise ValueError(
                "AI response must be a JSON object"
            )

        decision = str(
            data.get("decision", "")
        ).upper().strip()

        if decision not in {
            "TAKE",
            "IGNORE",
            "CONSIDER",
        }:
            raise ValueError("Invalid AI decision")

        try:
            confidence = float(
                data.get("confidence", 0)
            )
        except (TypeError, ValueError):
            confidence = 0

        if not math.isfinite(confidence):
            confidence = 0

        confidence = max(
            0.0,
            min(100.0, confidence),
        )

        comment = str(
            data.get("comment", "")
        )[:300]

        return decision, confidence, comment

    def review(self, symbol, signal):
        # Disabled AI is not an AI rejection.
        if not self.settings.enable_ai_review:
            log.info(
                "AI REVIEW SKIPPED | symbol=%s | reason=disabled",
                symbol,
            )
            return self._result(
                "NOT_REVIEWED",
                comment=(
                    "AI review is disabled; "
                    "technical signal remains available."
                ),
                status="DISABLED",
            )

        if not self.keys:
            log.warning(
                "AI REVIEW SKIPPED | symbol=%s | reason=no_api_keys",
                symbol,
            )
            return self._result(
                "NOT_REVIEWED",
                comment="No Gemini API key is configured.",
                status="NO_API_KEY",
            )

        now = time.time()

        cooldown = max(
            0,
            int(
                getattr(
                    self.settings,
                    "ai_review_cooldown",
                    300,
                )
            ),
        )

        signature = self._opportunity_signature(signal)
        previous = self.last.get(symbol)

        # Apply cooldown only to a materially similar opportunity.
        if previous and previous["signature"] == signature:
            elapsed = now - previous["time"]

            if elapsed < cooldown:
                remaining = max(
                    0,
                    int(cooldown - elapsed),
                )

                log.info(
                    "AI REVIEW SKIPPED | symbol=%s | "
                    "reason=cooldown | remaining=%ss",
                    symbol,
                    remaining,
                )

                return self._result(
                    "NOT_REVIEWED",
                    comment=(
                        "Same opportunity is within AI cooldown; "
                        f"retry in about {remaining}s."
                    ),
                    status="COOLDOWN",
                )

        # Rotate through configured Gemini keys.
        key = self.keys[self.index % len(self.keys)]
        self.index += 1

        try:
            from google import genai

            client = genai.Client(api_key=key)

            prompt = f"""
Review this already-qualified technical cryptocurrency opportunity.

You provide commentary only.
Do not invent market data.
Do not choose leverage.
Do not override risk controls.
Do not claim certainty or guaranteed profitability.

Return valid JSON only, with this exact structure:
{{
  "decision": "TAKE",
  "confidence": 0,
  "comment": "Brief explanation, maximum 300 characters"
}}

Allowed decisions: TAKE, IGNORE, CONSIDER.
Confidence must be between 0 and 100.

Symbol: {symbol}
Direction: {signal.get("direction")}
Technical score: {signal.get("score")}
Setup: {signal.get("setup")}
Entry: {signal.get("price")}
Stop loss: {signal.get("stop")}
Take profit: {signal.get("target")}
Snapshot: {signal.get("snapshot")}
Technical reason: {signal.get("reason")}
""".strip()

            response = client.models.generate_content(
                model=self.settings.gemini_model,
                contents=prompt,
            )

            response_text = getattr(
                response,
                "text",
                None,
            )

            if (
                not response_text
                or not response_text.strip()
            ):
                raise ValueError(
                    "Gemini returned an empty response"
                )

            decision, confidence, comment = (
                self._parse_response(response_text)
            )

            # Record cooldown only after a valid AI response.
            # A TAKE, CONSIDER, or IGNORE response counts as
            # a successful review; API/parse failures do not.
            self.last[symbol] = {
                "time": time.time(),
                "signature": signature,
            }

            log.info(
                "AI REVIEW COMPLETE | symbol=%s | "
                "decision=%s | confidence=%.1f | "
                "cooldown=%ss",
                symbol,
                decision,
                confidence,
                cooldown,
            )

            return self._result(
                decision,
                confidence=confidence,
                comment=comment,
                status="OK",
            )

        except Exception as exc:
            log.exception(
                "AI REVIEW FAILED | symbol=%s | "
                "cooldown_not_started=true",
                symbol,
            )

            # Never turn an API failure into approval.
            # Do not update self.last here.
            return self._result(
                "NOT_REVIEWED",
                comment=(
                    f"AI review unavailable "
                    f"({type(exc).__name__}). "
                    "No AI approval was received."
                ),
                status="ERROR",
            )


def review_signal(settings, symbol, signal):
    return AIReviewer(settings).review(symbol, signal)
