"""Decode audio locally without modifying the source file."""
import av
import numpy as np


def load_audio(path):
    frames = []
    with av.open(str(path)) as container:
        if not container.streams.audio:
            raise ValueError('No audio stream found')
        resampler = av.AudioResampler(format='fltp', layout='mono', rate=16000)
        for frame in container.decode(audio=0):
            for converted in resampler.resample(frame):
                frames.append(converted.to_ndarray().reshape(-1))
        for converted in resampler.resample(None):
            frames.append(converted.to_ndarray().reshape(-1))
    if not frames:
        raise ValueError('Audio has no decoded samples')
    audio = np.concatenate(frames).astype(np.float32)
    if not np.isfinite(audio).all():
        raise ValueError('Audio contains non-finite samples')
    return audio, 16000
