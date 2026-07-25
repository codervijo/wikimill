"""Marker output and the JSONL run log."""

from __future__ import annotations

import io
import json

from wikimill.constants import Marker
from wikimill.logging import RunLog


def test_markers_written_to_stream(tmp_path):
    stream = io.StringIO()
    with RunLog("test", tmp_path / "logs", stream=stream) as log:
        log.ok("built")
        log.warn("skipped")
        log.fail("broke")
    out = stream.getvalue()
    assert Marker.OK in out and Marker.WARN in out and Marker.FAIL in out


def test_counts_and_failed_flag(tmp_path):
    with RunLog("test", tmp_path / "logs", quiet=True) as log:
        log.ok("a")
        log.ok("b")
        log.warn("c")
        assert not log.failed
        log.fail("d")
        assert log.failed
        assert log.counts[Marker.OK.value] == 2


def test_jsonl_log_is_written_and_parseable(tmp_path):
    logs = tmp_path / "logs"
    with RunLog("test", logs, quiet=True) as log:
        log.ok("step-one", "detail here")
        run_id = log.run_id
    lines = (logs / f"{run_id}.jsonl").read_text(encoding="utf-8").splitlines()
    events = [json.loads(line) for line in lines]
    assert events[0]["event"] == "run_start"
    assert events[-1]["event"] == "run_end"
    assert any(e.get("step") == "step-one" for e in events)


def test_unwritable_log_dir_does_not_abort(tmp_path):
    """A missing log directory must never kill a run — the terminal is what matters."""
    blocker = tmp_path / "notadir"
    blocker.write_text("x", encoding="utf-8")
    with RunLog("test", blocker / "logs", quiet=True) as log:
        log.ok("still works")
    assert log.counts[Marker.OK.value] == 1


def test_markers_go_to_stderr_by_default(capsys, tmp_path):
    """stdout stays clean so `export --format jsonl > file` is not polluted."""
    with RunLog("test", tmp_path / "logs") as log:
        log.ok("noise")
    captured = capsys.readouterr()
    assert Marker.OK in captured.err
    assert Marker.OK not in captured.out


def test_no_colour_when_not_a_tty(tmp_path):
    stream = io.StringIO()  # not a tty
    with RunLog("test", tmp_path / "logs", stream=stream) as log:
        log.fail("plain")
    assert "\033[" not in stream.getvalue()
