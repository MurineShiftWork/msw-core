"""Calibration visualisation: matplotlib / PDF rendering of setup valve curves."""

import logging
import warnings
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
from scipy.optimize import OptimizeWarning, curve_fit

from murineshiftwork.logic.calibration.stats import _exponential_function


def plot_setup_valve_calibrations(
    config_dir: str | Path | None = None,
    setup_name: str | None = None,
    save_fig: bool = False,
    show: bool = True,
) -> "plt.Figure":
    """Plot bpod_valve calibration curves for one or all setups.

    Parameters
    ----------
    config_dir:
        Path to the msw_configs directory.  Resolved via machine config if None.
    setup_name:
        If given, plot only that setup.  If None, plot all setups found in
        ``config_dir/setups/``.
    save_fig:
        Save PNG next to each setup YAML when True.
    show:
        Call ``plt.show()`` when True (disable for batch/headless use).

    Returns
    -------
    matplotlib Figure
    """
    import yaml

    from murineshiftwork.logic.machine_config import resolve_config_dir

    config_dir = Path(config_dir) if config_dir else Path(resolve_config_dir())
    setups_dir = config_dir / "setups"

    if setup_name:
        yaml_files = [setups_dir / f"{setup_name}.yaml"]
    else:
        yaml_files = sorted(setups_dir.glob("*.yaml"))

    if not yaml_files:
        raise FileNotFoundError(f"No setup YAMLs found in {setups_dir}")

    # Collect calibration data
    all_data: dict = {}  # {setup_name: {valve_id: {"open_s": [...], "volume_ul": [...]}}}
    for yf in yaml_files:
        if not yf.exists():
            logging.warning(f"Setup YAML not found: {yf}")
            continue
        with yf.open() as f:
            raw = yaml.safe_load(f) or {}
        cal = raw.get("calibrations", {}).get("bpod_valve", {})
        if not cal:
            continue
        sname = raw.get("name", yf.stem)
        all_data[sname] = {}
        for valve_id, vdata in cal.items():
            pts = vdata.get("points", [])
            if not pts:
                continue
            pts_arr = np.array(pts, dtype=float)
            all_data[sname][str(valve_id)] = {
                "open_s": pts_arr[:, 0],
                "volume_ul": pts_arr[:, 1],
                "updated": vdata.get("updated", ""),
            }

    if not all_data:
        raise ValueError("No bpod_valve calibration data found in selected setups.")

    n_setups = len(all_data)
    fig, axes = plt.subplots(
        1, n_setups, figsize=(4 * n_setups, 4), squeeze=False, sharey=False
    )
    fig.suptitle("Bpod valve calibration", fontsize=12)

    colors = plt.rcParams["axes.prop_cycle"].by_key()["color"]

    for col_idx, (sname, valves) in enumerate(all_data.items()):
        ax = axes[0, col_idx]
        for v_idx, (valve_id, vdata) in enumerate(valves.items()):
            x = vdata["open_s"]
            y = vdata["volume_ul"]
            color = colors[v_idx % len(colors)]
            ax.scatter(x, y, color=color, zorder=3, label=f"valve {valve_id}")

            # Fit curve if enough points
            if len(x) >= 3:
                try:
                    mask = y > 0
                    xs, ys = x[mask], y[mask]
                    s_span = float(xs.max() - xs.min()) if len(xs) >= 2 else 1.0
                    ul_min, ul_max = float(ys.min()), float(ys.max())
                    b0 = (
                        np.log(ul_max / ul_min) / s_span
                        if s_span > 0 and ul_min > 0
                        else 5.0
                    )
                    with warnings.catch_warnings():
                        warnings.simplefilter("ignore", OptimizeWarning)
                        popt, _ = curve_fit(
                            _exponential_function,
                            xs,
                            ys,
                            p0=[ul_min, b0, 0.0],
                            bounds=([0.0, 0.0, -np.inf], [np.inf, np.inf, np.inf]),
                            maxfev=5000,
                        )
                    x_fit = np.linspace(x.min() * 0.9, x.max() * 1.1, 200)
                    y_fit = _exponential_function(x_fit, *popt)
                    ax.plot(x_fit, y_fit, color=color, linewidth=1.2, alpha=0.7)
                except Exception:
                    pass

        ax.set_title(sname, fontsize=10)
        ax.set_xlabel("Valve open time (s)")
        ax.set_ylabel("Volume (uL)")
        ax.legend(fontsize=8)
        ax.grid(True, alpha=0.3)

    fig.tight_layout()

    if save_fig:
        for sname in all_data:
            out = setups_dir / f"{sname}.calibration_plot.png"
            fig.savefig(out, dpi=150)
            logging.info(f"Saved calibration plot: {out}")

    if show:
        plt.show()

    return fig


def save_calibration_pdfs(
    config_dir: "str | Path | None" = None,
    setup_name: "str | None" = None,
    output_dir: "str | Path | None" = None,
) -> list[str]:
    """Save one PDF calibration chart per setup to output_dir.

    Parameters
    ----------
    config_dir:
        msw_configs directory.  Resolved from machine config if None.
    setup_name:
        Plot only this setup; plots all setups if None or empty.
    output_dir:
        Directory for PDFs.  Defaults to the current working directory.

    Returns
    -------
    List of absolute paths to saved PDF files.
    """
    from datetime import datetime

    import yaml

    from murineshiftwork.logic.machine_config import resolve_config_dir

    config_dir = Path(config_dir) if config_dir else Path(resolve_config_dir())
    output_dir = Path(output_dir or ".").expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    setups_dir = config_dir / "setups"
    yaml_files = (
        [setups_dir / f"{setup_name}.yaml"]
        if setup_name
        else sorted(setups_dir.glob("*.yaml"))
    )

    dt_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    saved: list[str] = []

    for yf in yaml_files:
        if not yf.exists():
            logging.warning(f"Setup YAML not found: {yf}")
            continue
        with yf.open() as f:
            raw = yaml.safe_load(f) or {}
        sname = raw.get("name", yf.stem)
        if not raw.get("calibrations", {}).get("bpod_valve"):
            logging.info(f"No bpod_valve calibration in '{sname}', skipping")
            continue
        try:
            fig = plot_setup_valve_calibrations(
                config_dir=config_dir,
                setup_name=sname,
                save_fig=False,
                show=False,
            )
            out = output_dir / f"{sname}--{dt_str}.pdf"
            fig.savefig(out, format="pdf", bbox_inches="tight")
            plt.close(fig)
            saved.append(str(out))
            logging.info(f"Saved calibration PDF: {out}")
        except Exception as exc:
            logging.warning(f"Failed to plot calibration for '{sname}': {exc}")

    return saved
