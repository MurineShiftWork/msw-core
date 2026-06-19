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
