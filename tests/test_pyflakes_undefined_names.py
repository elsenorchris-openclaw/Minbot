"""Regression test: pyflakes-style scan for undefined names (F821) in min_bot.

Catches the 2026-05-03 `config.STATIONS` NameError class — silent failures
where a copy-paste from V1/V2 references a variable not in min_bot's scope.
"""
import subprocess
import sys
from pathlib import Path

MIN_BOT_SRC = Path(__file__).parent.parent / "paper_min_bot.py"


def test_no_undefined_names():
    """pyflakes must report zero undefined-name errors in paper_min_bot.py."""
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pyflakes", str(MIN_BOT_SRC)],
            capture_output=True, text=True, timeout=60,
        )
    except FileNotFoundError:
        import pytest
        pytest.skip("pyflakes not available")
        return

    output = result.stdout + result.stderr
    undefined = [line for line in output.splitlines() if "undefined name" in line.lower()]
    assert not undefined, (
        f"pyflakes found {len(undefined)} undefined-name errors in min_bot:\n"
        + "\n".join(undefined)
    )
