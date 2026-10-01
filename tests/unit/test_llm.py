import pytest
from src.llm import call_with_backoff


class Err(Exception):
    def __init__(self, msg, status=429):
        super().__init__(msg)
        self.status_code = status


def test_retries_rate_limit_and_honours_wait_hint():
    waits, calls = [], iter([Err("Rate limit. Please try again in 7.5s."), Err("try again in 250ms"), "ok"])
    def fn():
        r = next(calls)
        if isinstance(r, Exception):
            raise r
        return r
    assert call_with_backoff(fn, sleep=waits.append) == "ok"
    assert waits == [pytest.approx(8.5), pytest.approx(1.25)]


def test_request_too_large_fails_fast_with_advice():
    def fn():
        raise Err("Request too large for model on output tokens per minute")
    with pytest.raises(RuntimeError, match="LLM_MAX_TOKENS"):
        call_with_backoff(fn, sleep=lambda s: pytest.fail("must not wait"), max_tokens=4096)


def test_other_errors_are_not_swallowed_and_retries_are_bounded():
    with pytest.raises(Err):
        call_with_backoff(lambda: (_ for _ in ()).throw(Err("bad key", status=401)), sleep=lambda s: None)
    n = []
    def always():
        n.append(1)
        raise Err("slow down")
    with pytest.raises(Err):
        call_with_backoff(always, retries=2, sleep=lambda s: None)
    assert len(n) == 3
