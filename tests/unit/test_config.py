"""Central config: YAML loading, defaults, and environment overrides."""
from src.config import DEFAULTS, get_llm_config, load_config

# What the five call sites hard-coded before (provider groq, GROQ_MODEL or qwen) plus make_llm's own defaults
# for groq (temperature 0.2, max_tokens 900). The shipped config must reproduce it exactly.
BEFORE = {"provider": "groq", "model": "qwen/qwen3.8-27b", "temperature": 0.2, "max_tokens": 900}


def test_shipped_config_reproduces_previous_behaviour():
    assert get_llm_config(env={}) == BEFORE
    assert load_config()["domain"]["name"] == "repo_repair"


def test_env_overrides_model_and_max_tokens():
    cfg = get_llm_config(env={"GROQ_MODEL": "llama-3.3-70b-versatile", "LLM_MAX_TOKENS": "512"})
    assert cfg["model"] == "llama-3.3-70b-versatile" and cfg["max_tokens"] == 512
    assert cfg["provider"] == "groq" and cfg["temperature"] == 0.2


def test_yaml_values_are_read(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("llm:\n  model: some/model\n  temperature: 0.7\n")
    cfg = get_llm_config(path=p, env={})
    assert cfg == {**BEFORE, "model": "some/model", "temperature": 0.7}      # missing keys fall back to defaults


def test_missing_file_falls_back_to_defaults(tmp_path):
    assert load_config(tmp_path / "absent.yaml") == DEFAULTS
    assert get_llm_config(path=tmp_path / "absent.yaml", env={}) == BEFORE


def test_caller_default_model_sits_between_yaml_and_env():
    assert get_llm_config(default_model="m-smoke", env={})["model"] == "m-smoke"
    assert get_llm_config(default_model="m-smoke", env={"GROQ_MODEL": "m-env"})["model"] == "m-env"


def test_groq_model_env_does_not_leak_into_another_provider(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("llm:\n  provider: anthropic\n  model: claude-x\n")
    assert get_llm_config(path=p, env={"GROQ_MODEL": "qwen/x"})["model"] == "claude-x"
