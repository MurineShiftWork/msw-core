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
takes the lock. A simulated run takes the lock too and refuses a live one, but
leaves a dead session's lock in place (it resolves no cameras to reset) and runs
without one.

**`--simulate`** opens no hardware: every device the task requires
(`required_devices`) is a stand-in, also one the setup lacks; no camera group
starts and `CameraSession` refuses real camera backends; a `Stimulation` that
connects its own PulsePal gets a `SimPulsePal`. A task that asks for video gets a
simulated `CameraSession` from `CameraSession.from_run`: it lists one stand-in
`with_video` group, so a task that requires a camera runs, but starts and records
nothing; the log says video recording is off.

**`record_video: false`** starts no `with_video` camera group and gives the
deprecated `make_camera_client` no camera config; `record: always` groups still run.

**Acquisition status** in `session_manifest.yaml` at exit: `complete` (the task ended
by itself), `stopped` (the operator ended it, Ctrl-C), `aborted` (the task or the
session raised). `running` left behind means the process died; the next run on the
setup sets it to `interrupted`.

Every session's `.msw.session.yaml` holds the resolved `task_settings`; a task may
overwrite the section.

Exit codes: `0` completed, `1` failed (including a Bpod that does not connect and a
setup in use), `3` completed degraded: the task asked for video (`record_video` on,
or a camera session of the task holding a `with_video` group), `camera_failure:
continue` let the run go on, and no camera recorded; `4` completed with partial
camera loss: same conditions, but at least one camera recorded and at least one
requested camera did not (lost at preflight, at start or during the session, e.g. 0
frames). The log warning names the lost `group/camera` units. `--simulate` and runs
without video exit `0`.

## Checking the cameras of a setup

```sh
msw cameras check (<setup> | --setup <setup>) [--task <task>] [--json]
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

## Rig sign-off commands

Three commands for checking a rig PC (Windows or Linux) without starting a run. Each
prints a table, or JSON with `--json`, and has the exit codes listed below it.

### `msw config validate`

```sh
msw config validate [--setup <setup> | --all] [--subjects] [--check-ports] [--strict] [--json]
```

Loads each setup YAML (every one by default) and with `--subjects` every subject YAML
through the pydantic models a run uses (subjects after the in-memory schema
migration), so a file that passes loads in a run. Per file it reports `error`s (YAML
or model errors, a subject `schema_version` newer than this software, a valve
calibration without points) and `warning`s (`name` not matching the file name, a
subject `schema_version` missing or older, `task_overrides` for tasks not installed on
this machine, a Bpod setup without `bpod_valve` calibrations, calibrations older than
`stale_days`). `--check-ports` with `--setup` also resolves every device port of that
setup on this machine: an unresolvable `port_by_path` or a missing `/dev` node is an
error; a Windows `COM` port cannot be checked without opening it and is accepted.
Without `--setup` ports are not checked and the report says so: a config dir shared
by several machines does not say which setups run here. Which valves a task rewards is
not checked: setups name no tasks of their own.

Use: on the machine that holds the configs, before a sign-off, and per rig with
`--setup <setup> --check-ports`. Exit codes: `0` no errors (no warnings either with
`--strict`), `1` findings, `2` config dir or setup not found, or nothing to validate.

### `msw cameras reset`

```sh
msw cameras reset --setup <setup> [--json]
```

Ends recordings that a dead session left running on camera units (an RCE Pi keeps
recording when the process that started it dies). Per camera group the backend is
loaded and configured as in `msw cameras check`; a backend implementing
`ResettableCameraBackend` (msw-plugin-api) is asked to `reset()`, which saves and
ends whatever its units are recording and reports each camera. Backends without it
are listed as `no reset`. A group that fails to load does not stop the others.
When every camera reset, a lock left on the setup by a dead session on this machine
is removed and that session's `running` acquisitions are marked `interrupted`; a
live session's lock is never touched.

Use: when a sign-off or `cameras check` finds a camera busy, or after a crashed
session. Exit codes: `0` every reset camera ok, `1` a camera failed to reset, `2` a
config error or an msw-plugin-api too old to have `ResettableCameraBackend`.

### `msw doctor`

```sh
msw doctor --setup <setup> [--skip-hardware] [--json]
```

One report of `ok`/`warn`/`fail`/`skip` checks: Python and platform; msw-core and the
distributions registering `msw.*` entry points, with versions (listed, not loaded);
the machine config (`~/.murineshiftwork/msw_machine.yaml`) readable; config and data
dirs; `config validate` of the setup (with `--check-ports` unless `--skip-hardware`).
Without `--skip-hardware` it then runs the run's device preflight (the data dir is
created and write-tested and every declared serial device is opened and closed, so a
port held by another program fails), `cameras check`, and, when the setup's
`open_ephys_url` or the machine config's `openephys_api_url` is set, one GET of the
Open Ephys `/api/status` with a 3 s timeout (no attach). Open Ephys unreachable fails
for a setup URL and warns for the machine-config fallback.

Use: the per-rig sign-off check; `--skip-hardware` when a session is running on the
rig. Exit codes: `0` no check failed, `1` a check failed, `2` config dir or setup not
found.
