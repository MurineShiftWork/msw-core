import importlib
import logging
import threading
import time


class BpodFactory:
    """Context-manager wrapper around pybpodapi Bpod with auto 4/8-port detection
    and retry on transient serial errors.

    Root cause of first-connect failures: opening a USB-CDC ttyACM device toggles
    DTR, which resets the Arduino firmware. The firmware takes ~1-2 s to boot;
    pybpodapi's handshake fires immediately and gets garbage bytes back
    (UnicodeDecodeError, wrong-byte BpodErrorException, or empty reads). Sleeping
    retry_delay_s before the next attempt lets the firmware settle.

    Connection happens in open(), not in __init__, so callers can construct
    BpodFactory() before the serial port is accessible and call open() later.

    _write_lock serialises serial writes for future ControllerSession override
    injection during a running state machine.
    """

    _SETTINGS_STANDARD = "murineshiftwork.hardware.bpod.user_settings"
    _SETTINGS_8PORT = "murineshiftwork.hardware.bpod.user_settings_8port"

    def __init__(
        self,
        serial_port="/dev/ttyACM0",
        workspace_path=None,
        session_name=None,
        connect_retries=5,
        retry_delay_s=2.0,
        **kwargs,
    ):
        self.serial_port = serial_port
        self.workspace_path = workspace_path
        self.session_name = session_name
        self.connect_retries = connect_retries
        self.retry_delay_s = retry_delay_s
        self._bpod_kwargs = kwargs

        self._bpod = None
        self._connected = False
        self._exiting = False
        self._write_lock = threading.Lock()
        self._port_config = "unknown"

    # ------------------------------------------------------------------
    # Context manager

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close_safely()

    def __del__(self):
        self.close_safely()

    # ------------------------------------------------------------------
    # Public API

    def open(self, max_try=None):
        """Open the Bpod connection, retrying on transient serial errors.

        Sleeps retry_delay_s before each retry so the Arduino firmware has
        time to finish booting after the USB-DTR reset that open() triggers.
        """
        if self._connected or self._exiting:
            return
        retries = max_try if max_try is not None else self.connect_retries
        last_exc: Exception | None = None
        for attempt in range(1, retries + 1):
            if attempt > 1:
                logging.warning(
                    "Bpod connect attempt %d/%d on %s "
                    "(sleeping %.1f s for firmware settle)...",
                    attempt,
                    retries,
                    self.serial_port,
                    self.retry_delay_s,
                )
                time.sleep(self.retry_delay_s)
            try:
                self._bpod = self._create_bpod_object()
                self._connected = True
                hw = self._bpod._hardware
                fw = getattr(hw, "firmware_version", "?")
                mt = getattr(hw, "machine_type", None)
                n_ports = self._behavior_port_count(self._bpod)
                # machine_type alone does not disambiguate board model (e.g. mt=2
                # is reported by both the 4-port r0.7 and the 8-port r1.0); the
                # behaviour-port count from the descriptor is authoritative.
                logging.info(
                    "Bpod connected on %s | %s | fw %s | machine_type=%s | %d behaviour ports",
                    self.serial_port,
                    self._port_config,
                    fw,
                    mt,
                    n_ports,
                )
                return
            except Exception as exc:
                self._close_partial(self._bpod)
                self._bpod = None
                last_exc = exc
                logging.warning(
                    "Bpod connect attempt %d/%d failed: %s: %s",
                    attempt,
                    retries,
                    type(exc).__name__,
                    exc,
                )
        raise RuntimeError(
            f"Failed to connect to Bpod at {self.serial_port!r} after {retries} attempts. "
            f"Last error: {type(last_exc).__name__}: {last_exc}. "
            "Power-cycle the Bpod and try again."
        ) from last_exc

    def close_safely(self):
        """Stop any running trial and close the connection."""
        self._exiting = True
        if self._connected and self._bpod is not None:
            try:
                self._bpod.stop_trial()
                self._bpod.close()
            except Exception as exc:
                logging.warning("Bpod safe-close error: %s", exc)
            finally:
                self._connected = False

    # ------------------------------------------------------------------
    # Proxy all attribute access to the underlying Bpod object

    @property
    def softcode_handler_function(self):
        return self._bpod.softcode_handler_function

    @softcode_handler_function.setter
    def softcode_handler_function(self, value):
        self._bpod.softcode_handler_function = value

    def __getattr__(self, name):
        bpod = self.__dict__.get("_bpod")
        if bpod is None:
            raise AttributeError(name)
        return getattr(bpod, name)

    # ------------------------------------------------------------------
    # Internal

    def _close_partial(self, bpod) -> None:
        """Close the serial port on a partially-opened pybpodapi Bpod.

        After a failed open(), _arcom may hold an open serial.Serial fd.
        Closing it releases the port so the next retry can open it cleanly.
        """
        if bpod is None:
            return
        try:
            if hasattr(bpod, "_arcom") and bpod._arcom is not None:
                bpod._arcom.close()
        except Exception:
            pass

    @staticmethod
    def _behavior_port_count(bpod) -> int:
        """Number of behaviour ports ('P' channels) in the connected device's
        hardware descriptor (read from the board during handshake)."""
        hw = getattr(bpod, "_hardware", None)
        inputs = getattr(hw, "inputs", None) or []
        return sum(1 for ch in inputs if ch == "P")

    def _open_bpod(self, settings: str):
        """Apply a pybpodapi settings module and open a Bpod on the serial port."""
        from confapp import conf
        from pybpodapi import protocol as bpod_protocol

        conf += settings
        importlib.reload(bpod_protocol)
        bpod = bpod_protocol.Bpod(
            serial_port=self.serial_port,
            workspace_path=self.workspace_path,
            session_name=self.session_name,
            **self._bpod_kwargs,
        )
        logging.getLogger("pybpodapi").setLevel(logging.WARNING)
        return bpod

    def _create_bpod_object(self):
        """Connect a pybpodapi Bpod, detecting 4- vs 8-port from the device.

        The board model is determined from the hardware descriptor the device
        reports during handshake, not assumed from the settings module. We open
        with standard (4-port) settings, then:

        - if the descriptor reports more than 4 behaviour ports, re-open with
          8-port settings so the wired ports are enabled correctly (a 4-port
          settings module can open an 8-port board without error but only
          under-configures it - this is the case that previously logged a
          spurious "4-port"); or
        - if enabling the 4-port wired ports overruns the descriptor
          (IndexError), switch to 8-port settings.

        All other exceptions propagate to open() which handles retry/settle.
        """
        partial = None
        try:
            partial = self._open_bpod(self._SETTINGS_STANDARD)
            if self._behavior_port_count(partial) <= 4:
                self._port_config = "4-port"
                return partial
            logging.info(
                "Device reports >4 behaviour ports: switching to 8-port settings"
            )
        except IndexError:
            logging.info(
                "4-port config mismatch (IndexError): switching to 8-port settings"
            )

        self._close_partial(partial)
        bpod = self._open_bpod(self._SETTINGS_8PORT)
        self._port_config = "8-port"
        return bpod
