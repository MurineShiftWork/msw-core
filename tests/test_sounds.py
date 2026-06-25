"""StereoSound persistent-stream callback: buffer-swap playback logic.

Pure logic test (no audio hardware): the output-stream callback emits the
current sound buffer then silence and clears the buffer when exhausted. This is
the basis of the low-latency design - triggering a sound only swaps the buffer
on an already-running stream, so playback latency is ~one block.
"""

import threading

import numpy as np

from murineshiftwork.logic.sounds import StereoSound


def _bare_stereo() -> StereoSound:
    # Bypass __init__ (which needs an audio device) to test the pure callback.
    s = StereoSound.__new__(StereoSound)
    s._play_lock = threading.Lock()
    s._play_buffer = None
    s._play_pos = 0
    return s


def test_callback_emits_silence_when_no_buffer():
    s = _bare_stereo()
    out = np.full((4, 2), 9.0, dtype="float32")
    s._audio_callback(out, 4, None, None)
    assert (out == 0).all()


def test_callback_streams_buffer_then_clears():
    s = _bare_stereo()
    buf = np.arange(8, dtype="float32").reshape(4, 2)
    s._play_buffer = buf
    s._play_pos = 0

    out = np.zeros((3, 2), dtype="float32")
    s._audio_callback(out, 3, None, None)
    assert np.array_equal(out, buf[:3])
    assert s._play_pos == 3

    # Next block: 1 remaining frame, then silence; buffer cleared when exhausted.
    out2 = np.zeros((2, 2), dtype="float32")
    s._audio_callback(out2, 2, None, None)
    assert np.array_equal(out2[0], buf[3])
    assert (out2[1] == 0).all()
    assert s._play_buffer is None
    assert s._play_pos == 0


def test_default_output_fallback_not_sysdefault(monkeypatch):
    """No named device found -> resolve PortAudio's real default output
    (cross-platform), not the invalid literal 'sysdefault' (silent on Windows)."""
    import sys
    import types

    out_dict = {"name": "Speakers (Realtek)", "default_samplerate": 48000.0}
    fake_sd = types.SimpleNamespace(
        query_devices=lambda kind=None: out_dict,
        default=types.SimpleNamespace(device=(1, 3)),
        check_output_settings=lambda **kw: None,
    )
    monkeypatch.setitem(sys.modules, "sounddevice", fake_sd)
    monkeypatch.setattr(
        "murineshiftwork.logic.sounds.find_sound_device", lambda **kw: None
    )

    s = StereoSound(sound_device="pipewire")  # not found -> default fallback

    assert s.sound_device == (3, out_dict)  # a real device tuple, not "sysdefault"
    assert s._device_id == 3
    assert s.sample_rate == 48000


def _xonar_fake_sd(check):
    import types

    return types.SimpleNamespace(
        check_output_settings=check,
        WasapiSettings=lambda exclusive=False: ("wasapi", exclusive),
    )


def test_high_rate_enables_wasapi_exclusive_on_windows(monkeypatch):
    """On Windows, a device-native rate above the shared default (XONAR 192000)
    keeps the full rate and auto-enables exclusive mode, validated in that mode."""
    import sys

    monkeypatch.setattr(
        "murineshiftwork.logic.sounds.platform.system", lambda: "Windows"
    )
    dev = {"name": "Speakers (XONAR SOUND CARD)", "default_samplerate": 48000.0}
    calls = {}

    def _check(**kw):
        calls.update(sr=kw.get("samplerate"), extra=kw.get("extra_settings"))

    monkeypatch.setitem(sys.modules, "sounddevice", _xonar_fake_sd(_check))
    monkeypatch.setattr(
        "murineshiftwork.logic.sounds.find_sound_device", lambda **kw: (10, dev)
    )

    s = StereoSound(sound_device="XONAR SOUND CARD")

    assert s.sample_rate == 192000  # full rate kept (not downgraded)
    assert s.use_wasapi_exclusive is True
    assert calls["sr"] == 192000
    assert calls["extra"] == ("wasapi", True)  # validated in exclusive mode


def test_falls_back_to_shared_default_when_even_exclusive_rejected(monkeypatch):
    import sys

    monkeypatch.setattr(
        "murineshiftwork.logic.sounds.platform.system", lambda: "Windows"
    )
    dev = {"name": "Speakers (XONAR SOUND CARD)", "default_samplerate": 48000.0}

    def _reject(**kw):
        raise RuntimeError("Invalid sample rate [PaErrorCode -9997]")

    monkeypatch.setitem(sys.modules, "sounddevice", _xonar_fake_sd(_reject))
    monkeypatch.setattr(
        "murineshiftwork.logic.sounds.find_sound_device", lambda **kw: (10, dev)
    )

    s = StereoSound(sound_device="XONAR SOUND CARD")

    assert s.sample_rate == 48000  # fell back to the shared default rate
    assert s.use_wasapi_exclusive is False
