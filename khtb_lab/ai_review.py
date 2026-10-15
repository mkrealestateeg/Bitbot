from __future__ import annotations
import json,logging,time
log=logging.getLogger(__name__)
class AIReviewer:
    def __init__(self,settings):
        self.settings=settings;self.keys=list(settings.gemini_api_pool);self.index=0;self.last={}
    def review(self,symbol,signal):
        if not self.settings.enable_ai_review or not self.keys:return None
        now=time.time()
        if now-self.last.get(symbol,0)<self.settings.ai_review_cooldown:return None
        self.last[symbol]=now
        key=self.keys[self.index%len(self.keys)];self.index+=1
        try:
            from google import genai
            client=genai.Client(api_key=key)
            prompt=f"""Review this already-qualified technical crypto opportunity. You are commentary only.
Do not invent market data. Do not choose leverage or override risk controls. Return JSON only:
{{"decision":"TAKE|IGNORE|CONSIDER","confidence":0-100,"comment":"max 300 chars"}}
Symbol: {symbol}
Direction: {signal.get("direction")}
Technical score: {signal.get("score")}
Setup: {signal.get("setup")}
Entry: {signal.get("price")}
Stop: {signal.get("stop")}
Target: {signal.get("target")}
Snapshot: {signal.get("snapshot")}
Reason: {signal.get("reason")}"""
            r=client.models.generate_content(model=self.settings.gemini_model,contents=prompt)
            text=(getattr(r,"text",None) or "").strip()
            try:
                data=json.loads(text)
            except Exception:
                data={"decision":"CONSIDER","confidence":0,"comment":text[:300]}
            data["decision"]=str(data.get("decision","CONSIDER")).upper()
            data["confidence"]=float(data.get("confidence",0) or 0)
            data["comment"]=str(data.get("comment",""))[:300]
            return data
        except Exception:
            log.exception("Gemini opportunity review failed");return None

def review_signal(settings,symbol,signal):
    return AIReviewer(settings).review(symbol,signal)
