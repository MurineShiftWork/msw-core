import logging
import platform
import threading

import numpy as np

sample_rate_dict = {
    "sysdefault": 44100,
    "default": 44100,
    "pipewire": 44100,
    "HDA Intel PCH": 44100,
    "XONAR SOUND CARD": 192000,
    "XONAR AE": 192000,
}


def get_sample_rate(target_device_name=None):
    for k in sample_rate_dict:
        if k in str(target_device_name):
            return sample_rate_dict[k]

    return None


def find_sound_device(target_device=None, return_first=True, prefer_wasapi=True):
    import sounddevice as sd

    devices = sd.query_devices()

    matches = [(i, d) for i, d in enumerate(devices) if target_device in d["name"]]
    # On Windows the same physical device is enumerated under several host APIs;
    # PortAudio lists MME first, which has high (~100 ms) latency. Prefer the
    # WASAPI instance so playback latency is low (closer to ASIO / PsychToolbox).
    if matches and prefer_wasapi and platform.system() == "Windows":
        hostapis = sd.query_hostapis()
        wasapi_idx = next(
            (i for i, h in enumerate(hostapis) if "WASAPI" in h.get("name", "")), None
        )
        if wasapi_idx is not None:
            wasapi_matches = [
                (i, d) for i, d in matches if d.get("hostapi") == wasapi_idx
            ]
            if wasapi_matches:
                matches = wasapi_matches
    # When several outputs of one card match (e.g. analog + SPDIF), prefer a
    # non-digital output so selection lands on the analog speaker rather than a
    # digital passthrough (SPDIF). Pin an exact sound_device in config to override.
    if len(matches) > 1:
        analog = [
            (i, d)
            for i, d in matches
            if not any(k in d["name"].upper() for k in ("SPDIF", "DIGITAL", "S/PDIF"))
        ]
        if analog:
            matches = analog
    if len(matches) > 0 and return_first:
        return matches[0]
    return matches


class StereoSound:
    default_sound_device = "XONAR SOUND CARD"
    default_ttl_channel = 1  # choices: 0 or 1 -> idx of position on
    default_ttl_duration = 0.001  # 1 ms
    default_sound_channels = 2  # stereo
    default_sound_latency = "low"
    # WASAPI exclusive mode bypasses the Windows mixer for the lowest latency,
    # but it binds the device directly and can be silent on some outputs (it
    # broke playback on a rig where shared-mode sd.play worked). Default to
    # shared mode (robust, mixer-routed); opt in per rig with use_wasapi_exclusive=True.
    use_wasapi_exclusive = False

    sound_stop_code = 99

    def __init__(
        self,
        sound_device: str | None = None,
        sample_rate: int | None = None,
        ttl_channel: int = 1,
        ttl_duration: float = 0.001,
        allow_sys_default_device=True,
        **kwargs,
    ):
        super().__init__()

        # Instance-level sounds dict (not class-level: avoid cross-instance sharing)
        self._sounds: dict = {}

        # Persistent low-latency output stream (set up in setup_sound_device).
        # Kept open so playback does not pay per-sound stream-open cost, which is
        # the dominant poke->sound delay on Windows. _play_buffer is the sound the
        # callback is currently streaming; swapping it is how a sound is triggered.
        self._stream = None
        self._play_lock = threading.Lock()
        self._play_buffer: np.ndarray | None = None
        self._play_pos = 0

        found_input_device = (
            find_sound_device(target_device=sound_device)
            if sound_device is not None
            else None
        )
        self.sound_device = found_input_device or find_sound_device(
            target_device=self.default_sound_device
        )

        if not self.sound_device:
            if not allow_sys_default_device:
                raise ValueError(
                    f"No sound device found for input '{sound_device}' "
                    f"or default '{self.default_sound_device}'"
                )
            # Fall back to PortAudio's real default output. The previous literal
            # "sysdefault" is an ALSA-only name and is NOT a valid PortAudio device
            # on Windows, so the stream open / sd.play silently failed there (no
            # audio). Resolving the actual default output works cross-platform.
            import sounddevice as sd

            try:
                out_dict = sd.query_devices(kind="output")
                out_idx = (
                    sd.default.device[1]
                    if isinstance(sd.default.device, list | tuple)
                    else sd.default.device
                )
            except Exception as exc:
                logging.warning(
                    "Could not query a default output device (%s); using None.", exc
                )
                out_dict, out_idx = {}, None
            self.sound_device = (out_idx, out_dict)
            logging.warning(
                "Sound device '%s' not found; falling back to the default output "
                "'%s' (idx %s).",
                sound_device or self.default_sound_device,
                out_dict.get("name", "?"),
                out_idx,
            )

        _dev_dict = (
            self.sound_device[1] if not isinstance(self.sound_device, str) else {}
        )
        # Extract int index for explicit device passing to sd.play(); keep full tuple
        # on self.sound_device for backward compatibility with callers that read it.
        if isinstance(self.sound_device, list | tuple):
            self._device_id = self.sound_device[0]
        else:
            self._device_id = self.sound_device  # string fallback ("sysdefault")

        _sr: int | None = (
            sample_rate
            or get_sample_rate(
                target_device_name=_dev_dict.get("name", self.sound_device)
            )
            or int(_dev_dict.get("default_samplerate", 0))
            or None
        )
        if _sr is None:
            raise ValueError(
                f"Could not find sample rate for device '{self.sound_device}'. "
                f"Change device or provide sample rate in input."
            )
        self.sample_rate: int = _sr

        # On Windows, WASAPI shared mode is locked to the mixer/default-format
        # rate, so a higher device-native rate (e.g. the XONAR's 192000) needs
        # EXCLUSIVE mode to bind the device directly. Auto-enable exclusive when
        # the chosen rate exceeds the device's shared default, so the full rate is
        # used rather than rejected (-9997) or resampled too slow ("deflated").
        _dev_default = int(_dev_dict.get("default_samplerate", 0))
        if (
            platform.system() == "Windows"
            and _dev_default
            and self.sample_rate > _dev_default
        ):
            self.use_wasapi_exclusive = True

        # Validate the chosen rate in the mode we will actually open (exclusive
        # when enabled). If even that is rejected, fall back to the device's
        # default rate in shared mode so the stream opens at a supported rate
        # (correct speed, lower rate) instead of failing or playing deflated.
        try:
            import sounddevice as sd

            sd.check_output_settings(
                device=self._device_id,
                channels=self.default_sound_channels,
                samplerate=self.sample_rate,
                dtype="float32",
                extra_settings=self._wasapi_extra_settings(),
            )
        except Exception as exc:
            _fallback_sr = _dev_default or 48000
            if _fallback_sr != self.sample_rate or self.use_wasapi_exclusive:
                logging.warning(
                    "Sound device %r rejects %d Hz (exclusive=%s): %s; using "
                    "%d Hz shared.",
                    self._device_id,
                    self.sample_rate,
                    self.use_wasapi_exclusive,
                    exc,
                    _fallback_sr,
                )
                self.sample_rate = _fallback_sr
                self.use_wasapi_exclusive = False

        self.ttl_channel = ttl_channel or self.default_ttl_channel
        if self.ttl_channel != 0 and self.ttl_channel != 1:
            raise ValueError(
                f"'ttl_channel' has to be 0 or 1 for stereo output, but '{self.ttl_channel}' given"
            )

        self.ttl_duration = ttl_duration or self.default_ttl_duration

        dev_name = _dev_dict.get("name", self.sound_device)
        logging.info(
            f"StereoSound: device={dev_name!r} id={self._device_id} sr={self.sample_rate}"
        )

        for k, v in kwargs.items():
            if hasattr(self, k):
                setattr(self, k, v)

    @property
    def sounds(self):
        return self._sounds

    @sounds.setter
    def sounds(self, new_sounds: dict):
        self._sounds = new_sounds

    def _wasapi_extra_settings(self):
        """WASAPI exclusive-mode settings when opted in on Windows, else None.

        Exclusive mode bypasses the Windows audio mixer for the lowest latency,
        but binds the device directly and can be silent on some outputs. It is
        OFF by default (shared mode, mixer-routed, robust); enable per rig with
        ``use_wasapi_exclusive=True``. Returns None on non-Windows, when not
        opted in, or if WASAPI settings are unavailable (stream stays shared).
        """
        if not self.use_wasapi_exclusive:
            return None
        if platform.system() != "Windows":
            return None
        import sounddevice as sd

        try:
            return sd.WasapiSettings(exclusive=True)
        except Exception as exc:  # WASAPI not present / device not exclusive-capable
            logging.debug("WASAPI exclusive settings unavailable: %s", exc)
            return None

    def _audio_callback(self, outdata, frames, time_info, status):
        """Stream callback: emit the current sound buffer, else silence.

        Triggering a sound is just swapping self._play_buffer; the already-running
        stream picks it up within one block, so playback latency is ~one block.
        """
        if status:
            logging.debug("Sound stream status: %s", status)
        with self._play_lock:
            buf = self._play_buffer
            pos = self._play_pos
            if buf is None:
                outdata[:] = 0
                return
            end = pos + frames
            chunk = buf[pos:end]
            n = len(chunk)
            outdata[:n] = chunk
            if n < frames:
                outdata[n:] = 0
            if end >= len(buf):
                self._play_buffer = None
                self._play_pos = 0
            else:
                self._play_pos = end

    def setup_sound_device(self):
        """Set sounddevice defaults and open a persistent low-latency stream."""
        import sounddevice as sd

        sd.default.device = self._device_id
        sd.default.latency = self.default_sound_latency
        sd.default.channels = self.default_sound_channels
        sd.default.samplerate = self.sample_rate

        # Open one stream and keep it running, so a sound trigger only swaps the
        # buffer (no per-sound stream open/start, the main Windows latency cost).
        # Fall back to per-call sd.play() if the stream cannot be opened.
        try:
            self._stream = sd.OutputStream(
                samplerate=self.sample_rate,
                channels=self.default_sound_channels,
                device=self._device_id,
                latency=self.default_sound_latency,
                dtype="float32",
                callback=self._audio_callback,
                extra_settings=self._wasapi_extra_settings(),
            )
            self._stream.start()
            logging.info(
                "StereoSound: persistent output stream open (latency~%.1f ms)",
                self._stream.latency * 1000,
            )
        except Exception as exc:
            logging.warning(
                "StereoSound: persistent output stream could not open on device "
                "%r (%s); falling back to sd.play(). If there is no audio, verify "
                "the device exists on this host (sounddevice.query_devices()).",
                self._device_id,
                exc,
            )
            self._stream = None

    def close(self):
        """Stop and close the persistent stream (releases an exclusive device)."""
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception as exc:
                logging.debug("StereoSound stream close error: %s", exc)
            finally:
                self._stream = None

    def _make_bup(self, amplitude: float) -> np.ndarray:
        """Single 5 ms broadband bup matching the MATLAB singlebup defaults.

        Harmonics at 2k, 4k, 8k, 16k Hz summed equally, normalised, then
        amplitude-scaled.  2 ms cos²-ramp applied to onset and offset.
        """
        bup_dur = int(0.005 * self.sample_rate)
        t = np.arange(bup_dur) / self.sample_rate
        bup = np.zeros(bup_dur)
        for _f in (2000, 4000, 8000, 16000):
            bup += np.sin(2 * np.pi * _f * t)
        peak = np.max(np.abs(bup))
        if peak > 0:
            bup /= peak
        ramp_n = int(0.002 * self.sample_rate)
        ramp = np.cos(np.linspace(np.pi / 2, 0, ramp_n)) ** 2
        bup[:ramp_n] *= ramp[::-1]
        bup[-ramp_n:] *= ramp
        return amplitude * bup

    def _make_bup_train(
        self,
        bup_rate: float = 5.0,
        duration: float = 1.5,
        amplitude: float = 0.05,
    ) -> np.ndarray:
        """Train of bups matching the MATLAB MakeBupperSwoop reward sound.

         Default parameters replicate: MakeBupperSwoop(sr, 0, 5, 5, 750, 750, 0, 0.1)
        : 5 Hz bup rate, 1.5 s total, broadband clicks.
        """
        n_samples = int(duration * self.sample_rate)
        mono = np.zeros(n_samples)
        bup = self._make_bup(amplitude)
        interval = int(self.sample_rate / bup_rate)
        for start in range(0, n_samples, interval):
            end = min(start + len(bup), n_samples)
            mono[start:end] += bup[: end - start]
        return mono

    def _make_sound(
        self,
        frequency=None,
        duration=None,
        amplitude=None,
        fade_duration=0.01,
        bup_rate: float = 5.0,
    ):
        """Build a stereo sound array.

        frequency=-2 → bup train (MATLAB MakeBupperSwoop style, broadband clicks).
        frequency=-1 → white noise.
        frequency>0  → pure sine tone.
        """
        if frequency == -2:
            mono = self._make_bup_train(
                bup_rate=bup_rate, duration=duration, amplitude=amplitude
            )
        else:
            tvec = np.linspace(0, duration, int(duration * self.sample_rate))
            if frequency == -1:
                mono = amplitude * np.random.randn(len(tvec))
            else:
                mono = amplitude * np.sin(2 * np.pi * frequency * tvec)

            len_fade = int(fade_duration * self.sample_rate)
            fade_io = np.hanning(len_fade * 2)
            win = np.ones(len(tvec))
            win[:len_fade] = fade_io[:len_fade]
            win[-len_fade:] = fade_io[len_fade:]
            mono = mono * win

        null = np.zeros(len(mono))
        if self.ttl_channel == 0:
            return np.array([mono, null]).T
        elif self.ttl_channel == 1:
            return np.array([null, mono]).T
        else:
            raise ValueError(
                f"'ttl_channel' has to be 0 or 1 for stereo output, but '{self.ttl_channel}' given"
            )

    def register_new_sound(
        self,
        frequency=None,
        duration=None,
        amplitude=None,
        fade_duration=0.01,
        play_blocking=True,
        bup_rate: float = 5.0,
        **kwargs,
    ):
        """ """
        new_sound_dict = kwargs
        new_sound_dict["play_blocking"] = play_blocking
        new_sound_dict["sound"] = self._make_sound(
            frequency=frequency,
            duration=duration,
            amplitude=amplitude,
            fade_duration=fade_duration,
            bup_rate=bup_rate,
        )

        # 1-indexed: Bpod SoftCode 0 is not a valid user softcode
        new_sound_key = len(self._sounds) + 1
        self._sounds[new_sound_key] = new_sound_dict
        return new_sound_key

    def execute_sound_handler(self, sound_code=None, raise_errors=False):
        import sounddevice as sd

        if sound_code in self.sounds:
            logging.debug(f"Playing sound # {sound_code}.")
            buf = np.ascontiguousarray(
                self.sounds[sound_code]["sound"], dtype="float32"
            )
            if self._stream is not None:
                # Hand the buffer to the running stream (low-latency path).
                with self._play_lock:
                    self._play_buffer = buf
                    self._play_pos = 0
                if self.sounds[sound_code]["play_blocking"]:
                    import time as _time

                    _time.sleep(len(buf) / self.sample_rate)
            else:
                # Best-effort: this runs inside the Bpod softcode handler thread,
                # so an audio backend error must not crash the session.
                try:
                    sd.play(
                        buf,
                        self.sample_rate,
                        device=self._device_id,
                        blocking=self.sounds[sound_code]["play_blocking"],
                    )
                except Exception as exc:
                    logging.warning(
                        "StereoSound: sd.play failed on device %r (%s); no audio "
                        "this trial.",
                        self._device_id,
                        exc,
                    )
                    if raise_errors:
                        raise
        elif sound_code == self.sound_stop_code:
            logging.debug("Stopped current sound.")
            if self._stream is not None:
                with self._play_lock:
                    self._play_buffer = None
                    self._play_pos = 0
            else:
                sd.stop()
        else:
            msg = f"No such sound index: {sound_code}"
            if raise_errors:
                raise ValueError(msg)
            else:
                logging.debug(msg)


if __name__ == "__main__":
    print(" ")
