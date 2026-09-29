"""Smoke test: real Groq LLM + a tiny in-memory 'repo' (no Docker, no adapter needed).
Toy twin case: ImportError for `colorutils`, but the symbol exists locally -> correct fix is a
local import, NOT `pip install`. Run from the project root:  python -m scripts.smoke_solver
"""
import json, os, sys
from dotenv import load_dotenv

load_dotenv()                                   # loads GROQ_API_KEY from .env
if not os.getenv("GROQ_API_KEY"):
    sys.exit("GROQ_API_KEY not found - check .env is in the project root")

from src.llm import make_llm
from src.solver import Solver, Tool

REPO = {
    "app.py": "from colorutils import hex_to_rgb\nprint(hex_to_rgb('#ff0000'))\n",
    "utils/__init__.py": "",
    "utils/colorutils.py": "def hex_to_rgb(h):\n    h = h.lstrip('#')\n    return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))\n",
}
TASK = ("Running `python app.py` fails with:\n"
        "Traceback (most recent call last):\n  File 'app.py', line 1, in <module>\n"
        "    from colorutils import hex_to_rgb\nModuleNotFoundError: No module named 'colorutils'\n"
        "Give the fix as a single shell command or a one-line code change.")

def list_files() -> str: return "\n".join(REPO)
def read_file(path: str) -> str: return REPO.get(path, f"no such file: {path}")
def grep(pattern: str) -> str:
    hits = [f"{p}:{i+1}: {l}" for p, src in REPO.items() for i, l in enumerate(src.splitlines()) if pattern in l]
    return "\n".join(hits) or "no matches"

TOOLS = {t.name: t for t in [
    Tool("list_files", "list_files() - list all files in the repo", list_files),
    Tool("read_file", "read_file(path) - return a file's contents", read_file),
    Tool("grep", "grep(pattern) - find lines containing pattern across the repo", grep),
]}

def verify(task, solution: str) -> bool:        # toy verifier: stands in for 'run the tests'
    s = solution.lower()
    return "utils.colorutils" in s and "pip install" not in s

llm = make_llm({"provider": "groq", "model": os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")})
result = Solver(llm, TOOLS, verify).solve(TASK)

for t in result.trace:
    print(f"[a{t['attempt']} s{t['step']}] {t.get('thought','')}")
    print(f"    -> {t.get('action') or 'FINAL'} {t.get('args') or t.get('final') or ''}")
    if 'observation' in t: print(f"    <- {t['observation'][:150]}")
for r in result.reflections: print("REFLECTION:", r)
print(json.dumps({k: v for k, v in result.__dict__.items() if k not in ("trace", "reflections")}, indent=2))
