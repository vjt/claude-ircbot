"""The watchdog's main loop, run against a fake clock and a fake transcript dir.

A /clear typed while a turn is running waits in Claude Code's queue until the
turn ends, and the turn keeps writing to the old transcript meanwhile. On
2026-11-28 at 02:30 the watchdog read those lines as the same session still
over its context budget and fired a second warning + /clear 61 s after the
first, which then landed on the fresh session's /resume.
"""

import json
from pathlib import Path

import pytest

import aup_watchdog as wd


def assistant(ctx: int) -> str:
    return json.dumps({
        "type": "assistant",
        "message": {"stop_reason": "end_turn",
                    "content": [{"type": "text", "text": "ok"}],
                    "usage": {"cache_read_input_tokens": ctx}},
    }) + "\n"


class Clock:
    """time.time/time.sleep for the module: sleeping runs the scenario."""

    def __init__(self, steps, until):
        self.now = 1_000_000.0
        self.start = self.now
        self.steps = sorted(steps, key=lambda s: s[0])
        self.until = until

    def time(self):
        return self.now

    def sleep(self, sec):
        self.now += sec
        elapsed = self.now - self.start
        while self.steps and self.steps[0][0] <= elapsed:
            self.steps.pop(0)[1]()
        if elapsed >= self.until:
            raise KeyboardInterrupt


def run(monkeypatch, tmp_path: Path, steps, until):
    fires = []
    clock = Clock(steps, until)
    monkeypatch.setattr(wd, "PROJECT_DIR", tmp_path)
    monkeypatch.setattr(wd, "time", clock)
    monkeypatch.setattr(wd, "IDLE_ENABLED", False)
    monkeypatch.setattr(wd, "grant_read", lambda p: None)
    monkeypatch.setattr(wd, "log", lambda m: None)
    monkeypatch.setattr(
        wd, "fire_clear",
        lambda reason, **kw: fires.append((clock.now - clock.start, reason)) or True)
    wd.main()
    return fires


def append(path: Path, ctx: int):
    return lambda: path.open("a").write(assistant(ctx))


@pytest.fixture
def old(tmp_path):
    path = tmp_path / "old.jsonl"
    path.write_text("")
    return path


def test_turn_still_writing_the_old_transcript_does_not_fire_again(
        monkeypatch, tmp_path, old):
    steps = [(4, append(old, 350_000))]
    steps += [(t, append(old, 355_000)) for t in range(40, 600, 40)]
    fires = run(monkeypatch, tmp_path, steps, until=600)
    assert len(fires) == 1


def test_new_session_rearms(monkeypatch, tmp_path, old):
    new = tmp_path / "new.jsonl"
    steps = [(4, append(old, 350_000)),
             (100, lambda: new.write_text("")),
             (200, append(new, 360_000))]
    fires = run(monkeypatch, tmp_path, steps, until=300)
    assert len(fires) == 2


def test_clear_never_seen_fires_again_after_clear_lost_sec(
        monkeypatch, tmp_path, old):
    steps = [(4, append(old, 350_000))]
    steps += [(t, append(old, 355_000))
              for t in range(60, wd.CLEAR_LOST_SEC + 120, 60)]
    fires = run(monkeypatch, tmp_path, steps, until=wd.CLEAR_LOST_SEC + 120)
    assert len(fires) == 2
    assert fires[1][0] >= wd.CLEAR_LOST_SEC
