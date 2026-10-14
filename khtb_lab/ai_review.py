from __future__ import annotations
import logging
log=logging.getLogger(__name__)

def review_signal(settings, symbol: str, signal: dict) -> str | None:
    """Optional commentary only. Never changes direction, levels, risk checks or execution."""
    if not settings.enable_ai_review or not settings.gemini_api_key:
        return None
    try:
        from google import genai
        client=genai.Client(api_key=settings.gemini_api_key)
        prompt=(
            "You are a cautious crypto market reviewer. Do not give guarantees. "
            "Do not change the supplied signal or invent data. In at most 60 words, "
            "identify uncertainty or missing confirmation from this technical snapshot. "
            "This is commentary only and must not instruct execution.\n"
            f"Symbol: {symbol}\nSignal: {signal.get('direction')}\n"
            f"Score: {signal.get('score')}\nSetup: {signal.get('setup')}\n"
            f"Snapshot: {signal.get('snapshot')}\nReason: {signal.get('reason')}"
        )
        response=client.models.generate_content(model=settings.gemini_model,contents=prompt)
        text=(getattr(response,'text',None) or '').strip()
        return text[:1000] or None
    except Exception:
        log.exception('Optional Gemini review failed; technical signal remains unchanged')
        return None
