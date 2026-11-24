# msw-core

[![PyPI](https://img.shields.io/pypi/v/msw-core.svg)](https://pypi.org/project/msw-core)

Murine Shift Work acquisition stack: CLI, hardware drivers, session hooks, and task logic.

Provides the `murineshiftwork.cli`, `murineshiftwork.hardware`, `murineshiftwork.hooks`,
and `murineshiftwork.logic` namespace sub-packages. Installs the `msw` / `murineshiftwork`
CLI entry points.

Part of the [MurineShiftWork](https://github.com/MurineShiftWork) namespace package ecosystem.

## Running a session

```sh
msw run -s <subject> -t <task> --setup <setup> [--simulate]
```

Before any hardware opens, the run refuses a subject YAML whose `schema_version` is
missing or older than this software's (`msw config migrate-subjects` upgrades it;
the loader alone would migrate only in memory).

**One session per setup.** `msw run` holds `~/.murineshiftwork/locks/<setup>.lock`
(YAML: `pid`, `host`, `started_at`, `setup`, `session_container`) from before the
first device opens until it exits, normally or on an exception. A lock held by a
live process on this host, or by another host, refuses the run. A lock left by a
dead process means that session was killed: the run warns, resets the setup's
camera groups whose backend implements `ResettableCameraBackend` (each camera saves
what it recorded), marks that session's `running` acquisitions `interrupted`, then
takes the lock. A simulated run takes no lock.

**`--simulate`** opens no hardware: every device the task requires
(`required_devices`) is a stand-in, also one the setup lacks; no camera group
starts and `CameraSession` refuses real camera backends; a `Stimulation` that
connects its own PulsePal gets a `SimPulsePal`.

**`record_video: false`** starts no `with_video` camera group and gives the
deprecated `make_camera_client` no camera config; `record: always` groups still run.

**Acquisition status** in `session_manifest.yaml` at exit: `complete` (the task ended
by itself), `stopped` (the operator ended it, Ctrl-C), `aborted` (the task or the
session raised). `running` left behind means the process died; the next run on the
setup sets it to `interrupted`.

Every session's `.msw.session.yaml` holds the resolved `task_settings`; a task may
overwrite the section.

Exit codes: `0` completed, `1` failed (including a Bpod that does not connect and a
setup in use), `3` completed degraded: `record_video` was on, `camera_failure:
continue` let the run go on, and no camera recorded.

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
