"""Thin LLM adapter. The Solver only ever sees `LLM = Callable[[str, list[dict]], str]`,
so swapping providers (or injecting a scripted fake in tests) never touches solver logic."""
import re
from typing import Callable

LLM = Callable[..., str]  # (system_prompt, messages, json_mode=False) -> assistant text


def make_llm(cfg: dict) -> LLM:
    """cfg comes from configs/config.yaml -> llm: {provider, model, max_tokens, temperature}"""
    provider = cfg["provider"]
    model = cfg["model"]
    max_tokens = cfg.get("max_tokens", 4096)  # headroom: reasoning models spend tokens thinking
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
            r = client.chat.completions.create(
                model=model, max_tokens=max_tokens, temperature=temperature,
                messages=[{"role": "system", "content": system}, *messages], **extra)
            return clean(r.choices[0].message.content)
        return call

    raise ValueError(f"unknown provider: {provider}")