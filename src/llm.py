"""Thin LLM adapter. The Solver only ever sees `LLM = Callable[[str, list[dict]], str]`,
so swapping providers (or injecting a scripted fake in tests) never touches solver logic."""
import os
import re
import time
from typing import Callable

LLM = Callable[..., str]  # (system_prompt, messages, json_mode=False) -> assistant text


def call_with_backoff(fn, retries: int = 6, sleep=time.sleep, max_tokens: int | None = None):
    """Retry on rate limits (HTTP 429), waiting as long as the provider asks. A request that is simply
    too big for the per-minute limit can never succeed by waiting, so that fails fast with advice."""
    for attempt in range(retries + 1):
        try:
            return fn()
        except Exception as e:
            if getattr(e, "status_code", None) != 429:
                raise
            msg = str(e)
            if "Request too large" in msg:
                raise RuntimeError(
                    "Groq rejected the request as larger than your per-minute output-token limit "
                    f"(max_tokens={max_tokens}). Lower LLM_MAX_TOKENS in .env, or check your limits at "
                    "console.groq.com/settings/limits.") from e
            if attempt == retries:
                raise
            m = re.search(r"try again in ([\d.]+)\s*(ms|s)\b", msg)
            wait = (float(m.group(1)) / (1000 if m.group(2) == "ms" else 1) + 1) if m else min(60, 10 * 2 ** attempt)
            print(f"  [rate limited, waiting {wait:.0f}s]")
            sleep(min(wait, 90))


def salvage_failed_json(e: Exception) -> str | None:
    """Groq rejects a JSON-mode reply it cannot validate (often one cut off by max_tokens) with HTTP 400
    json_validate_failed. Return the partial text so callers can treat it as a malformed reply."""
    if getattr(e, "status_code", None) != 400 or "json_validate_failed" not in str(e):
        return None
    body = getattr(e, "body", None)
    err = body.get("error", body) if isinstance(body, dict) else {}
    return str(err.get("failed_generation", "")) if isinstance(err, dict) else ""


def make_llm(cfg: dict) -> LLM:
    """cfg comes from configs/config.yaml -> llm: {provider, model, max_tokens, temperature}"""
    provider = cfg["provider"]
    model = cfg["model"]
    max_tokens = int(os.getenv("LLM_MAX_TOKENS") or cfg.get("max_tokens", 900 if cfg["provider"] == "groq" else 4096))
    temperature = cfg.get("temperature", 0.2)

    def clean(text: str | None) -> str:
        """Drop <think>...</think> reasoning (Qwen etc.) so the parser only sees the answer."""
        text = text or ""
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
        return text.split("</think>")[-1].strip()

    if provider == "anthropic":
        import anthropic
        client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY

        def call(system: str, messages: list[dict], json_mode: bool = False) -> str:
            r = client.messages.create(model=model, system=system, messages=messages,
                                       max_tokens=max_tokens, temperature=temperature)
            return "".join(b.text for b in r.content if b.type == "text")
        return call

    if provider == "groq":
        from groq import Groq
        client = Groq(timeout=60.0, max_retries=3)  # reads GROQ_API_KEY

        def call(system: str, messages: list[dict], json_mode: bool = False) -> str:
            extra = {"response_format": {"type": "json_object"}} if json_mode else {}
            try:
                r = call_with_backoff(lambda: client.chat.completions.create(
                    model=model, max_tokens=max_tokens, temperature=temperature,
                    messages=[{"role": "system", "content": system}, *messages], **extra), max_tokens=max_tokens)
            except Exception as e:
                salvaged = salvage_failed_json(e)
                if salvaged is None:
                    raise
                return clean(salvaged)
            return clean(r.choices[0].message.content)
        return call

    raise ValueError(f"unknown provider: {provider}")