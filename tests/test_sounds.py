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


def _bare_for_make(channel_mode="both", ttl_channel=1, sample_rate=1000) -> StereoSound:
    # Bypass __init__ (needs audio hardware) to exercise the pure _make_sound path.
    s = StereoSound.__new__(StereoSound)
    s.sample_rate = sample_rate
    s.ttl_channel = ttl_channel
    s.channel_mode = channel_mode
    return s


def test_make_sound_both_duplicates_tone_on_each_channel():
    s = _bare_for_make(channel_mode="both")
    buf = s._make_sound(frequency=200, duration=0.05, amplitude=1.0)
    assert buf.shape[1] == 2
    assert np.array_equal(buf[:, 0], buf[:, 1])  # identical on both channels
    assert np.abs(buf).max() > 0  # actually a tone, not silence


def test_make_sound_ttl_puts_full_scale_marker_on_ttl_channel():
    s = _bare_for_make(channel_mode="ttl", ttl_channel=1)
    buf = s._make_sound(frequency=200, duration=0.05, amplitude=0.3)
    tone, ttl = buf[:, 0], buf[:, 1]
    assert np.allclose(ttl, 1.0)  # full-scale sync level held for the tone
    assert (
        np.abs(tone).max() > 0 and np.abs(tone).max() <= 0.3
    )  # tone respects amplitude


def test_make_sound_ttl_respects_channel_index():
    s = _bare_for_make(channel_mode="ttl", ttl_channel=0)
    buf = s._make_sound(frequency=200, duration=0.05, amplitude=0.3)
    assert np.allclose(buf[:, 0], 1.0)  # ttl on channel 0
    assert np.abs(buf[:, 1]).max() > 0  # tone on channel 1


def test_make_sound_rejects_unknown_channel_mode():
    s = _bare_for_make(channel_mode="nonsense")
    import pytest

    with pytest.raises(ValueError, match="channel_mode"):
        s._make_sound(frequency=200, duration=0.05, amplitude=1.0)


def test_register_new_sound_uses_instance_channel_mode_by_default():
    s = _bare_for_make(channel_mode="both")
    s._sounds = {}
    code = s.register_new_sound(frequency=200, duration=0.05, amplitude=1.0)
    buf = s._sounds[code]["sound"]
    assert np.array_equal(
        buf[:, 0], buf[:, 1]
    )  # 'both' applied without an explicit arg


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


def test_falls_back_to_default_rate_when_device_rejects(monkeypatch):
    """Device found but rejecting the configured rate (e.g. XONAR 192000 when its
    driver/Windows format is not at 192000): fall back to the device default so
    playback is correct-speed (audible) rather than deflated. Full 192000 needs
    the XONAR driver preset (7.1ch / 192000) - see the hardware setup docs.
    Exclusive mode is not auto-enabled (it was silent on the XONAR output)."""
    import sys
    import types

    dev = {"name": "Speakers (XONAR SOUND CARD)", "default_samplerate": 48000.0}

    def _reject(**kw):
        raise RuntimeError("Invalid sample rate [PaErrorCode -9997]")

    fake_sd = types.SimpleNamespace(check_output_settings=_reject)
    monkeypatch.setitem(sys.modules, "sounddevice", fake_sd)
    monkeypatch.setattr(
        "murineshiftwork.logic.sounds.find_sound_device", lambda **kw: (10, dev)
    )

    s = StereoSound(sound_device="XONAR SOUND CARD")

    assert s.sample_rate == 48000  # correct-speed fallback, not deflated
    assert s.use_wasapi_exclusive is False  # exclusive not auto-enabled
