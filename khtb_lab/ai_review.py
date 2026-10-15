
from __future__ import annotations

import json
import logging
import math
import time

log = logging.getLogger(__name__)


class AIReviewer:
    """
    Optional AI commentary for technically qualified opportunities.

    AI failure is reported explicitly. It is never treated as approval
    to execute a trade.
    """

    def __init__(self, settings):
        self.settings = settings
        self.keys = list(settings.gemini_api_pool)
        self.index = 0
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
            raise ValueError("AI response must be a JSON object")

        decision = str(data.get("decision", "")).upper().strip()

        if decision not in {"TAKE", "IGNORE", "CONSIDER"}:
            raise ValueError("Invalid AI decision")

        try:
            confidence = float(data.get("confidence", 0))
        except (TypeError, ValueError):
            confidence = 0

        if not math.isfinite(confidence):
            confidence = 0

        confidence = max(0.0, min(100.0, confidence))
        comment = str(data.get("comment", ""))[:300]

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
                comment="AI review is disabled; technical signal remains available.",
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
            int(getattr(self.settings, "ai_review_cooldown", 900)),
        )
        last_review = self.last.get(symbol, 0)

        if now - last_review < cooldown:
            remaining = int(cooldown - (now - last_review))

            log.info(
                "AI REVIEW SKIPPED | symbol=%s | reason=cooldown | "
                "remaining=%ss",
                symbol,
                remaining,
            )

            return self._result(
                "NOT_REVIEWED",
                comment=f"AI cooldown active; retry in about {remaining}s.",
                status="COOLDOWN",
            )

        self.last[symbol] = now

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

            response_text = getattr(response, "text", None)

            if not response_text or not response_text.strip():
                raise ValueError("Gemini returned an empty response")

            decision, confidence, comment = self._parse_response(
                response_text
            )

            log.info(
                "AI REVIEW COMPLETE | symbol=%s | decision=%s | "
                "confidence=%.1f",
                symbol,
                decision,
                confidence,
            )

            return self._result(
                decision,
                confidence=confidence,
                comment=comment,
                status="OK",
            )

        except Exception as exc:
            log.exception(
                "AI REVIEW FAILED | symbol=%s",
                symbol,
            )

            # Never convert an API failure into approval.
            return self._result(
                "NOT_REVIEWED",
                comment=(
                    f"AI review unavailable ({type(exc).__name__}). "
                    "Technical signal retained; no AI approval."
                ),
                status="ERROR",
            )


def review_signal(settings, symbol, signal):
    return AIReviewer(settings).review(symbol, signal)
