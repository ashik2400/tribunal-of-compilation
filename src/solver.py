"""Solver: ReAct inner loop + Reflexion outer loop.

ReAct    (inner): think -> act (tool) -> observe, repeated until the model commits to a final answer.
Reflexion (outer): if the attempt fails verification (or runs out of steps), write a short lesson
                   in words, keep it in episodic memory, and retry with those lessons in the prompt.

The Solver never sees ground truth. `verify` is the domain's *in-sandbox* signal (e.g. run the
repo's tests). `validate` is a cheap format gate: a malformed answer is bounced back as an
observation (costs a step, not an attempt) so format slips never trigger a bogus Reflexion round.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable

from src.llm import LLM


@dataclass
class Tool:
    name: str
    description: str          # shown to the model, include arg names
    fn: Callable[..., str]    # must return a string observation


@dataclass
class SolveResult:
    solution: Any
    verified: bool
    confidence: float
    self_reported_confidence: float | None   # logged, NOT trusted (LLMs are badly calibrated)
    attempts: int
    steps: int                                # total tool-loop steps across all attempts
    reflections: list[str] = field(default_factory=list)
    trace: list[dict] = field(default_factory=list)   # full audit trail -> MLflow / Compiler input


SYSTEM = """You are a repo-repair agent. Fix the task using the tools.
Each turn, reply with ONE JSON object and nothing else:
  {{"thought": "...", "action": "<tool name>", "args": {{...}}}}      to use a tool
  {{"thought": "...", "final": {{"solution": <your answer>, "confidence": <0..1>}}}}   when done
"final" is NOT a tool: to finish, reply with the "final" key, never with an "action".
Base claims on tool observations, not on memory.
Tools:
{tools}
{instructions}
{lessons}"""


def _parse_json(text: str) -> dict:
    """Pull the first JSON object out of a reply, tolerating prose or code fences around it."""
    start = text.find("{")
    if start == -1:
        raise ValueError("no JSON object found")
    obj, _ = json.JSONDecoder().raw_decode(text[start:])
    if not isinstance(obj, dict):
        raise ValueError("JSON is not an object")
    return obj


def _extract_final(msg: dict) -> dict | None:
    """Return {'solution', 'confidence'} if the reply is a final answer, else None.
    Lenient on one very common slip: {"action": "final", "args": {"solution": ...}}."""
    if "final" in msg:
        return {"solution": msg["final"]["solution"], "confidence": msg["final"].get("confidence")}
    args = msg.get("args")
    if msg.get("action") == "final" and isinstance(args, dict) and "solution" in args:
        return {"solution": args["solution"], "confidence": args.get("confidence")}
    return None


class Solver:
    def __init__(self, llm: LLM, tools: dict[str, Tool],
                 verify: Callable[[Any, Any], Any],
                 max_steps: int = 10, max_attempts: int = 3, obs_limit: int = 2000,
                 instructions: str = "", render_task: Callable[[Any], str] = str,
                 validate: Callable[[Any], str | None] | None = None):
        """verify(task, solution) -> bool, or (bool, reason). validate(solution) -> error text or None."""
        self.llm, self.tools, self.verify = llm, tools, verify
        self.max_steps, self.max_attempts, self.obs_limit = max_steps, max_attempts, obs_limit
        self.instructions, self.render_task, self.validate = instructions, render_task, validate

    # ---------- public ----------
    def solve(self, task: Any) -> SolveResult:
        reflections: list[str] = []
        trace: list[dict] = []
        total_steps = 0
        last: dict | None = None

        for attempt in range(1, self.max_attempts + 1):
            final, steps, attempt_trace = self._react(task, reflections, attempt)
            total_steps += steps
            trace.extend(attempt_trace)

            if final is not None:
                last = final
                out = self.verify(task, final["solution"])
                ok, why = out if isinstance(out, tuple) else (bool(out), "")
                if ok:
                    return self._result(final, True, attempt, total_steps, reflections, trace)
                outcome = (f"Your fix {final['solution']!r} was rejected by verification: "
                           f"{why or 'the problem still occurs after applying it'}")
            else:
                outcome = f"You ran out of steps ({self.max_steps}) without a final answer."

            if attempt < self.max_attempts:
                reflections.append(self._reflect(task, attempt_trace, outcome))

        return self._result(last, False, self.max_attempts, total_steps, reflections, trace)

    # ---------- ReAct inner loop ----------
    def _react(self, task, reflections, attempt):
        system = SYSTEM.format(
            tools="\n".join(f"- {t.name}: {t.description}" for t in self.tools.values()),
            instructions=self.instructions,
            lessons=("\nLessons from your earlier failed attempts (your own guesses, possibly wrong; "
                     "trust tool observations over them):\n" +
                     "\n".join(f"- {r}" for r in reflections)) if reflections else "")
        messages = [{"role": "user", "content": f"Task:\n{self.render_task(task)}"}]
        trace: list[dict] = []

        for step in range(1, self.max_steps + 1):
            reply = self.llm(system, messages, json_mode=True)
            messages.append({"role": "assistant", "content": reply})
            msg: dict = {}
            try:
                msg = _parse_json(reply)
                final = _extract_final(msg)
                if final is not None:
                    err = self.validate(final["solution"]) if self.validate else None
                    if err is None:
                        trace.append({"attempt": attempt, "step": step, "type": "final",
                                      "thought": msg.get("thought", ""), "final": final})
                        return final, step, trace
                    obs = f"ERROR: invalid answer format: {err} Reply again with a corrected \"final\"."
                    msg = {"thought": msg.get("thought", ""), "action": "invalid_final",
                           "args": {"solution": final["solution"]}}
                else:
                    name, args = msg["action"], msg.get("args", {})
                    if name not in self.tools:
                        obs = f"ERROR: unknown tool {name!r}. Available: {list(self.tools)}"
                    else:
                        try:
                            obs = str(self.tools[name].fn(**args))
                        except Exception as e:                      # tool failure is an observation,
                            obs = f"ERROR: {type(e).__name__}: {e}"  # not a crash -> agent can recover
            except (ValueError, KeyError, TypeError) as e:
                obs = f"ERROR: malformed reply ({e}). Reply with exactly one JSON object."
                msg = {}
            obs = obs[: self.obs_limit]
            trace.append({"attempt": attempt, "step": step, "type": "act", **msg, "observation": obs})
            messages.append({"role": "user", "content": f"Observation: {obs}"})

        return None, self.max_steps, trace

    # ---------- Reflexion outer loop ----------
    def _reflect(self, task, attempt_trace, outcome) -> str:
        transcript = "\n".join(
            f"[{t['type']}] {t.get('thought','')[:300]} | {t.get('action', t.get('final',''))} "
            f"-> {t.get('observation','')[:200]}" for t in attempt_trace)
        prompt = (f"Task:\n{self.render_task(task)}\n\nYour attempt:\n{transcript}\n\n{outcome}\n\n"
                  "In 2-3 sentences: what went wrong and what will you do differently? "
                  "Only state facts supported by the transcript or the rejection reason above; "
                  "do not assert facts about the outside world you have not observed. No JSON.")
        return self.llm("You write concise self-critiques for a debugging agent.",
                        [{"role": "user", "content": prompt}]).strip()

    # ---------- confidence ----------
    def _result(self, final, verified, attempts, steps, reflections, trace) -> SolveResult:
        reported = final["confidence"] if final else None
        if verified:
            # Earned, not claimed: fewer attempts and fewer wasted steps -> higher confidence.
            conf = max(0.5, 0.95 - 0.15 * (attempts - 1) - 0.01 * max(0, steps - 6))
        else:
            conf = min(0.3, reported or 0.0)
        return SolveResult(final["solution"] if final else None, verified, round(conf, 3),
                           reported, attempts, steps, reflections, trace)