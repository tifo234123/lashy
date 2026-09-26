"""Continuous, cancellable speech playback with a buffered PortAudio callback.

A response shares one output stream across sentences. Decoding and synthesis
happen off the audio callback; it only copies prepared PCM. Stop/Mute abort the
actual stream and invalidate its epoch so late synthesis cannot restart speech.
"""
import asyncio
from collections import deque
import io
import threading

import av
import numpy as np
import sounddevice as sd

OUTPUT_RATE = 48000
_stop_event = threading.Event()
_stream_lock = threading.Lock()
_active_stream = None
_active_player = None
_epoch = 0


def _cancelled(epoch):
    return _stop_event.is_set() or epoch != _epoch


def stop_audio() -> None:
    global _epoch, _active_stream, _active_player
    _stop_event.set()
    with _stream_lock:
        _epoch += 1
        stream, player = _active_stream, _active_player
        _active_stream = _active_player = None
    if stream is not None:
        try:
            stream.abort()
        except Exception:
            pass
        try:
            stream.close()
        except Exception:
            pass
    if player is not None:
        player.release_waiters()


def begin_audio_session() -> None:
    global _epoch
    stop_audio()
    with _stream_lock:
        _epoch += 1
        _stop_event.clear()


def decode_mp3_to_pcm(mp3_bytes: bytes) -> tuple[np.ndarray, int]:
    """Prepare consistent mono PCM and remove only excess edge silence.

    Keep 50 ms before the first audible sample and 120 ms after the last. Pauses
    inside speech are untouched. This avoids stacking provider padding between
    separately synthesized sentences without cutting quiet consonants.
    """
    chunks = []
    with av.open(io.BytesIO(mp3_bytes)) as container:
        stream = container.streams.audio[0]
        resampler = av.AudioResampler(format='flt', layout='mono', rate=OUTPUT_RATE)
        for frame in container.decode(stream):
            chunks.extend(r.to_ndarray().reshape(-1) for r in resampler.resample(frame))
        chunks.extend(r.to_ndarray().reshape(-1) for r in resampler.resample(None))
    if not chunks:
        return np.zeros(0, dtype=np.float32), OUTPUT_RATE
    pcm = np.concatenate(chunks).astype(np.float32)
    audible = np.flatnonzero(np.abs(pcm) > .0005)
    if audible.size:
        start = max(0, int(audible[0]) - int(.05 * OUTPUT_RATE))
        end = min(len(pcm), int(audible[-1]) + int(.12 * OUTPUT_RATE) + 1)
        pcm = pcm[start:end].copy()
    peak = float(np.max(np.abs(pcm))) if pcm.size else 0
    if peak > .92:
        pcm *= .92 / peak
    return pcm, OUTPUT_RATE


class _Segment:
    def __init__(self, pcm):
        self.pcm = pcm
        self.offset = 0
        self.consumed = threading.Event()


class _PCMPlayer:
    def __init__(self, sr, epoch):
        self.sr = sr
        self.epoch = epoch
        self.segments = deque()
        self.last_sample_time = 0.
        self.underflows = 0
        self.started = False
        self.stream = sd.OutputStream(
            samplerate=sr, channels=1, dtype='float32', blocksize=0,
            latency=.12, callback=self._callback,
        )

    def _callback(self, outdata, frames, timing, status):
        outdata.fill(0)
        if _cancelled(self.epoch):
            raise sd.CallbackAbort
        if status.output_underflow:
            self.underflows += 1
        filled = 0
        while filled < frames and self.segments:
            segment = self.segments[0]
            n = min(frames - filled, len(segment.pcm) - segment.offset)
            outdata[filled:filled+n, 0] = segment.pcm[segment.offset:segment.offset+n]
            filled += n
            segment.offset += n
            self.last_sample_time = timing.outputBufferDacTime + filled / self.sr
            if segment.offset == len(segment.pcm):
                self.segments.popleft()
                # Wake the producer as soon as samples reach the output buffer,
                # so the next sentence is ready before the next callback.
                segment.consumed.set()

    def release_waiters(self):
        while self.segments:
            self.segments.popleft().consumed.set()


def _blocking_play_chunked(pcm: np.ndarray, sr: int, epoch: int) -> None:
    """Queue prepared samples and wait until copied to the device buffer."""
    global _active_player, _active_stream
    if pcm.size == 0 or _cancelled(epoch):
        return
    pcm = np.asarray(pcm, dtype=np.float32).reshape(-1)
    peak = float(np.max(np.abs(pcm)))
    if peak > .92:
        pcm = pcm * (.92 / peak)
    segment = _Segment(pcm)
    with _stream_lock:
        if _cancelled(epoch):
            return
        if _active_player is None:
            _active_player = _PCMPlayer(sr, epoch)
            _active_stream = _active_player.stream
        player = _active_player
        if player.sr != sr:
            raise ValueError('Speech chunks must use a consistent output sample rate')
        player.segments.append(segment)
        if not player.started:
            player.stream.start()
            player.started = True
    while not segment.consumed.wait(.02):
        if _cancelled(epoch):
            return
        if not player.stream.active:
            raise RuntimeError('Audio output stopped unexpectedly')


async def drain_audio_async() -> None:
    """Wait for the final samples to reach the speaker before request cleanup."""
    player = _active_player
    if player is None:
        return
    while not _cancelled(player.epoch):
        try:
            if player.stream.time >= player.last_sample_time:
                return
        except Exception:
            if _cancelled(player.epoch):
                return
            raise
        await asyncio.sleep(.01)


async def play_pcm_async(pcm: np.ndarray, sr: int, *, drain=True) -> None:
    if pcm.size == 0 or _stop_event.is_set():
        return
    epoch = _epoch
    await asyncio.to_thread(_blocking_play_chunked, pcm, sr, epoch)
    if drain and not _cancelled(epoch):
        await drain_audio_async()


async def play_mp3_async(mp3_bytes: bytes) -> None:
    if not mp3_bytes or _stop_event.is_set():
        return
    epoch = _epoch
    pcm, sr = await asyncio.to_thread(decode_mp3_to_pcm, mp3_bytes)
    if pcm.size and not _cancelled(epoch):
        await play_pcm_async(pcm, sr)
