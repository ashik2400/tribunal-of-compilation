"""Central configuration: configs/config.yaml, with environment overrides on top.

    from src.config import get_llm_config
    llm = make_llm(get_llm_config())

Precedence for the LLM settings (highest first):
    1. environment: GROQ_MODEL (model, groq provider only), LLM_MAX_TOKENS (max_tokens)
    2. an explicit `default_model` passed by the caller (only scripts/smoke_solver.py uses this)
    3. configs/config.yaml
    4. DEFAULTS below (used for any key the YAML lacks, or if the file is absent, e.g. in a container image
       built without configs/)
"""
from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any, Mapping

import yaml

CONFIG_PATH = Path(__file__).resolve().parents[1] / "configs" / "config.yaml"

DEFAULTS: dict[str, Any] = {
    "domain": {"name": "repo_repair"},
    "llm": {"provider": "groq", "model": "qwen/qwen3.8-27b", "temperature": 0.2, "max_tokens": 900},
}


def _merge(base: dict, override: Mapping) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        out[k] = _merge(out[k], v) if isinstance(v, Mapping) and isinstance(out.get(k), dict) else v
    return out


def load_config(path: str | Path | None = None) -> dict:
    """The whole config as a dict: DEFAULTS overlaid with the YAML file (if it exists)."""
    p = Path(path) if path else CONFIG_PATH
    data = yaml.safe_load(p.read_text()) if p.exists() else {}
    return _merge(DEFAULTS, data or {})


def get_llm_config(default_model: str | None = None, path: str | Path | None = None,
                   env: Mapping[str, str] | None = None) -> dict:
    """The dict make_llm() expects: {provider, model, temperature, max_tokens}."""
    env = os.environ if env is None else env
    cfg = dict(load_config(path)["llm"])
    if default_model:
        cfg["model"] = default_model
    if cfg["provider"] == "groq" and env.get("GROQ_MODEL"):
        cfg["model"] = env["GROQ_MODEL"]
    if env.get("LLM_MAX_TOKENS"):
        cfg["max_tokens"] = int(env["LLM_MAX_TOKENS"])
    return cfg
