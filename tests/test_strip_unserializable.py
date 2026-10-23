"""_strip_unserializable makes task settings YAML-safe before update_session_yaml."""

from pathlib import Path

import yaml

from murineshiftwork.logic.task_process import (
    _strip_unserializable,
    update_session_yaml,
)


def test_paths_stringified_callables_dropped():
    base = Path("/data") / "seq209"
    settings = {
        "session_paths": {"basepath": base, "parts": [Path("a")]},
        "valve_s_for_ul": lambda ul: ul,
        "hooks": [print, 1],
        "n": 3,
    }
    out = _strip_unserializable(settings)
    assert out == {
        "session_paths": {"basepath": str(base), "parts": ["a"]},
        "hooks": [1],
        "n": 3,
    }
    yaml.safe_dump(out)  # would raise RepresenterError on a Path


def test_session_yaml_keeps_session_paths(tmp_path):
    session = tmp_path / "s"
    update_session_yaml(
        session, task_settings={"session_paths": {"basepath": tmp_path}}
    )
    written = next(tmp_path.glob("*session.yaml"))
    data = yaml.safe_load(written.read_text())
    assert data["task_settings"]["session_paths"]["basepath"] == str(tmp_path)
