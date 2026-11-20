# msw-core

[![PyPI](https://img.shields.io/pypi/v/msw-core.svg)](https://pypi.org/project/msw-core)

Murine Shift Work acquisition stack: CLI, hardware drivers, session hooks, and task logic.

Provides the `murineshiftwork.cli`, `murineshiftwork.hardware`, `murineshiftwork.hooks`,
and `murineshiftwork.logic` namespace sub-packages. Installs the `msw` / `murineshiftwork`
CLI entry points.

Part of the [MurineShiftWork](https://github.com/MurineShiftWork) namespace package ecosystem.

## Checking the cameras of a setup

```sh
msw cameras check <setup> [--task <task>] [--json]
```

Loads, configures and preflights the setup's camera groups through their backends and
never records; no Bpod or other device is needed. Without `--task` every group is checked;
with it, only the groups a run of that task starts (`record: always` groups, and the video
groups when `record_video` is on, narrowed by `record_cameras`). The `camera_failure` policy
is not applied: every camera is listed with its group, backend and version, `hw_id`, state
and error, and the backends are closed afterwards.

Exit codes: `0` every camera ready, `1` a camera missing or failed, `2` a config error
(unknown backend, settings the backend refuses, missing RCE ensemble YAML or Bonsai
executable).
