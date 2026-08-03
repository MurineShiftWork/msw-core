"""logic.config subpackage.

Submodules:
  ini   : YAML task-config reader (read_config, read_task_modes, validate_config_file_path)
  io    : YAML-based setup/subject config I/O
  models: Pydantic config models (SetupConfig, SubjectConfig, ...)

All public symbols are re-exported here so existing imports like
  from murineshiftwork.logic.config import read_config
  from murineshiftwork.logic.config import SetupConfig
continue to work without change.
"""

from murineshiftwork.logic.config.ini import (  # noqa: F401
    deep_merge,
    read_config,
    read_task_modes,
    validate_config_file_path,
)
from murineshiftwork.logic.config.io import (  # noqa: F401
    SchemaVersionError,
    load_setup_config,
    load_subject_config,
    migrate_schema,
    save_subject_task_overrides,
    save_subject_task_stage_position,
    save_subject_task_state,
    update_stage_config,
    update_valve_calibration,
)
from murineshiftwork.logic.config.models import (  # noqa: F401
    SUBJECT_CONFIG_SCHEMA_VERSION,
    AxisConfig,
    BpodDevice,
    Calibrations,
    CameraConfig,
    DeviceUnion,
    ExecutionConfig,
    GenericSerialDevice,
    PulsePalDevice,
    ScaleDevice,
    SerialDevice,
    SetupConfig,
    StageTowerDevice,
    SubjectConfig,
    ValveCalibration,
)
