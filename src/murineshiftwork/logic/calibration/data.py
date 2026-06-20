"""Calibration data containers.

Pandas-backed measurement holders for liquid-valve and sound-latency
calibration, plus the shared base class.  Fitting math lives in
:mod:`murineshiftwork.logic.calibration.stats`; the per-instance
``save_calibration_plot`` helpers use matplotlib directly.
"""

import logging
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import seaborn as sns


class CalibrationData:
    file_path = None
    calibration_data: Any = None
    columns: list = []
    columns_to_drop = ["Unnamed: 0"]

    def __init__(self, file_path=None, **kwargs):
        """ """
        super().__init__(**kwargs)
        self.file_path = file_path or self.file_path
        logging.debug(f"Calibration data path: {self.file_path}")

        for k, v in kwargs.items():
            if hasattr(self, k):
                setattr(self, k, v)

        self.load()

    def __add__(self, other):
        assert isinstance(other, dict)
        other.update({"measurement_time": datetime.now()})
        import pandas as pd

        self.calibration_data = pd.concat(
            [self.calibration_data, pd.DataFrame([other])], ignore_index=True
        )
        return self

    def __repr__(self):
        return f"{type(self)} with {self.calibration_data.shape[0]} entries."

    def __str__(self):
        return str(self.calibration_data)

    def load(self, file_path=None):
        if file_path is not None:
            self.file_path = file_path

        if self.file_path and Path(self.file_path).exists():
            self.calibration_data = pd.read_csv(self.file_path)
            logging.debug(
                f"Updated calibration data with {self.calibration_data.shape[0]} measurements."
            )
        else:
            self.calibration_data = pd.DataFrame(columns=self.columns)

        for target_column in self.columns_to_drop:
            if target_column in self.calibration_data.columns:
                self.calibration_data = self.calibration_data.drop(
                    target_column, axis=1
                )

        return self.calibration_data

    def save(self, file_path=None, overwrite=False):
        if file_path is not None:
            self.file_path = file_path

        if self.file_path is None:
            return

        file_path = Path(self.file_path)
        if self.calibration_data is not None and not self.calibration_data.empty:
            file_path.expanduser().parent.mkdir(exist_ok=True, parents=True)

            if file_path.exists() and not overwrite:
                raise FileExistsError(
                    f"File exists and not allowed to overwrite. {file_path}"
                )
            self.calibration_data.to_csv(file_path)
            logging.info(f"Saved calibration: {file_path}")


class CalibrationDataLiquid(CalibrationData):
    allowable_offset_days = 30
    columns = [
        "measurement_time",
        "valve_id",
        "valve_opening_time",
        "n_drops",
        "inter_pulse_interval",
        "weight",
        "weight_per_drop",
        "volume_ul",
    ]

    def load(self, file_path=None):
        if file_path is not None:
            self.file_path = file_path
        # Back-compat: if the .liquid. file doesn't exist, try the old .water. name
        if self.file_path and not Path(self.file_path).exists():
            old_path = str(self.file_path).replace(".liquid.", ".water.")
            if old_path != str(self.file_path) and Path(old_path).exists():
                logging.warning(
                    f"Calibration file not found at {self.file_path}; "
                    f"falling back to legacy path {old_path}. "
                    f"Rename it to {Path(self.file_path).name} to silence this warning."
                )
                self.file_path = old_path
        return super().load()

    def add_calibration_point(
        self,
        valve_id=None,
        valve_opening_time=None,
        n_drops=None,
        inter_pulse_interval=None,
        liquid_weight_g=None,
    ):
        self.__add__(
            {
                "valve_id": valve_id,
                "valve_opening_time": valve_opening_time,
                "n_drops": n_drops,
                "inter_pulse_interval": inter_pulse_interval,
                "liquid_weight_g": liquid_weight_g,
            }
        )

    def _compute_volumes(self):
        """Compute weight_per_drop and volume_ul columns in-place. Idempotent."""
        self.upgrade_calibration_file_field_names()
        self.calibration_data["weight_per_drop"] = np.round(
            self.calibration_data["liquid_weight_g"] / self.calibration_data["n_drops"],
            3,
        )
        self.calibration_data["volume_ul"] = np.round(
            self.calibration_data["weight_per_drop"] * 1e3, 3
        )
        self.calibration_data = self.calibration_data.sort_values(
            by="valve_opening_time"
        )

    def liquid_volume_to_valve_time(self, valves=None, target_volume=None):
        if self.calibration_data is None or self.calibration_data.empty:
            raise ValueError(
                f"Liquid calibration data is empty. "
                f"Ensure a calibration CSV exists at: {self.file_path}"
            )
        self._compute_volumes()

        if not hasattr(valves, "__iter__"):
            valves = [valves]

        # Get target valve opening times (seconds) for given volumes via exponential fit.
        # Preferred path: use SetupConfig.valve_s_for_ul() which calls
        # ValveCalibration.s_for_ul() with the same exponential model.
        # This CSV path is kept for backward compat when SetupConfig is absent.
        calibration_targets = {}
        for this_valve in valves:
            data_for_valve = self.calibration_data.loc[
                self.calibration_data["valve_id"] == this_valve
            ].sort_values("valve_opening_time")

            points = list(
                zip(
                    data_for_valve["valve_opening_time"].tolist(),
                    data_for_valve["volume_ul"].tolist(),
                    strict=False,
                )
            )
            from murineshiftwork.logic.config import ValveCalibration

            vc = ValveCalibration(points=[[t, u] for t, u in points])
            calibration_targets[this_valve] = vc.s_for_ul(target_volume)
        return calibration_targets

    def save_calibration_plot(self):
        if (
            self.file_path is not None
            and self.calibration_data is not None
            and not self.calibration_data.empty
        ):
            self._compute_volumes()

            # PLOT
            f = plt.figure(dpi=450)
            sns.lineplot(
                data=self.calibration_data,
                x="valve_opening_time",
                y="volume_ul",
                hue="valve_id",
            )
            plt.title("Valve opening times to pass water volume [uL].")
            plt.ylabel("Volume [uL]")
            plt.xlabel("Valve opening time [ms]")
            f.savefig(str(Path(self.file_path).with_suffix(".png")))

    def to_valve_calibration(self, valve_id: int):
        """Convert collected measurements for one valve into a ValveCalibration.

        Returns a ValveCalibration with the updated timestamp set to now and
        points = [[open_time_ms, volume_ul], ...], one entry per calibration
        measurement, sorted by open time.

        Caller should run .check_quality() before writing to setup config.
        """
        from datetime import datetime

        from murineshiftwork.logic.config import ValveCalibration

        df = self.calibration_data.copy()
        df = df[df["valve_id"] == valve_id].copy()
        if df.empty:
            raise ValueError(f"No calibration data for valve {valve_id}")

        df["_ul"] = np.round((df["liquid_weight_g"] / df["n_drops"]) * 1e3, 3)
        # Normalise valve_opening_time to 4 d.p. before groupby so that floating-point
        # near-duplicates (e.g. np.linspace artifact 0.07999... vs round()-produced 0.08)
        # are treated as the same key.  .last() then keeps the most-recent measurement.
        df["valve_opening_time"] = df["valve_opening_time"].round(4)
        df = (
            df.groupby("valve_opening_time", as_index=False)["_ul"]
            .last()
            .sort_values("valve_opening_time")
        )
        df["_ul"] = np.round(df["_ul"], 3)
        # Drop dead-zone measurements (valve barely opens, volume <= 0 is pure noise).
        df = df[df["_ul"] > 0]

        points = [
            [float(row["valve_opening_time"]), float(row["_ul"])]
            for _, row in df.iterrows()
        ]

        return ValveCalibration(
            updated=datetime.now().isoformat(timespec="seconds"),
            points=points,
        )

    def upgrade_calibration_file_field_names(self):
        """Ensure compatibility between old calibration data files and new columns format."""
        if "water_weight_g" in self.calibration_data.columns:
            self.calibration_data = self.calibration_data.rename(
                columns={"water_weight_g": "liquid_weight_g"}
            )
        if "microliters" in self.calibration_data.columns:
            logging.debug(
                "Liquid calibration file has old field names. Making backup copy and overwriting original.."
            )
            self.calibration_data = self.calibration_data.rename(
                {
                    "valve": "valve_id",
                    "valve_time": "valve_opening_time",
                    "weight": "liquid_weight_g",
                    "microliters": "volume_ul",
                }
            )
            backup_file = str(self.file_path) + ".bak"
            shutil.copyfile(src=self.file_path, dst=backup_file)
            if Path(backup_file).exists():
                self.save()
            else:
                raise FileNotFoundError(
                    f"backup file should have been made by copying {self.file_path} to {backup_file}."
                )


class CalibrationDataSound(CalibrationData):
    columns = ["measurement_time", "trial", "delay"]

    def add_calibration_point(self, trial=None, delay=None):
        self.__add__(
            {
                "trial": trial,
                "delay": delay,
            }
        )

    def calculate_sound_delay_correction(self):
        return np.round(self.calibration_data["delay"].median(), 3)

    def save_calibration_plot(self):
        if (
            self.file_path is not None
            and self.calibration_data is not None
            and not self.calibration_data.empty
        ):
            f = plt.figure(dpi=450)
            plt.plot(self.calibration_data["delay"] * 1000, "k*--")
            plt.title(
                "Delays from sound softcode to soundcard TTL received by Bpod BNC-in."
            )
            plt.ylabel("Delay [ms]")
            plt.xlabel("Trial [#]")
            f.savefig(str(Path(self.file_path).with_suffix(".png")))
