# -*- coding: utf-8 -*-
"""Background frame sources: still images, GIFs, and video (via ffmpeg).

All sources produce 320x320 RGB frames (centered scaling and cropping).
Source interface:
        .next() -> PIL.Image (RGB 320x320) — next frame
        .fps    -> recommended frame rate
    .close()
"""

import os
import shutil
import subprocess

from PIL import Image, ImageSequence

SCREEN = (320, 320)
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".mkv", ".avi", ".webm"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp"}


def fit(im):
    """Fill the 320x320 screen (cover): scale to cover, then crop."""
    im = im.convert("RGB")
    sw, sh = SCREEN
    scale = max(sw / im.width, sh / im.height)
    nw, nh = max(sw, int(round(im.width * scale))), max(sh, int(round(im.height * scale)))
    im = im.resize((nw, nh), Image.LANCZOS)
    left, top = (nw - sw) // 2, (nh - sh) // 2
    return im.crop((left, top, left + sw, top + sh))


class SolidSource:
    """Solid-color background (when no background is set)."""
    fps = 1
    animated = False

    def __init__(self, color=(0, 0, 0)):
        self._img = Image.new("RGB", SCREEN, color)

    def next(self):
        return self._img

    def close(self):
        pass


class ImageSource:
    fps = 1
    animated = False

    def __init__(self, path):
        self._img = fit(Image.open(path))

    def next(self):
        return self._img

    def close(self):
        pass


class GifSource:
    animated = True

    def __init__(self, path):
        im = Image.open(path)
        self.frames = []
        self.durations = []
        for fr in ImageSequence.Iterator(im):
            self.frames.append(fit(fr))
            self.durations.append(max(20, fr.info.get("duration", 100)))
        if not self.frames:
            self.frames = [Image.new("RGB", SCREEN, (0, 0, 0))]
            self.durations = [1000]
        avg = sum(self.durations) / len(self.durations) / 1000.0
        self.fps = max(1, min(30, round(1.0 / avg))) if avg else 10
        self._i = 0

    def next(self):
        img = self.frames[self._i]
        self._i = (self._i + 1) % len(self.frames)
        return img

    def close(self):
        pass


class VideoSource:
    """Loop video frames via ffmpeg (rawvideo rgb24 320x320)."""

    def __init__(self, path, fps=20):
        if not shutil.which("ffmpeg"):
            raise RuntimeError("Video requires ffmpeg (sudo pacman -S ffmpeg)")
        self.path = path
        self.fps = fps
        self._proc = None
        self._start()

    def _start(self):
        vf = ("scale=320:320:force_original_aspect_ratio=increase,"
              "crop=320:320,fps=%d" % self.fps)
        self._proc = subprocess.Popen(
            ["ffmpeg", "-hide_banner", "-loglevel", "error",
             "-stream_loop", "-1", "-re", "-i", self.path,
             "-vf", vf, "-pix_fmt", "rgb24", "-f", "rawvideo", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    def next(self):
        n = SCREEN[0] * SCREEN[1] * 3
        buf = self._read_exact(n)
        if buf is None:                 # end of stream — restart
            self.close()
            self._start()
            buf = self._read_exact(n)
            if buf is None:
                return Image.new("RGB", SCREEN, (0, 0, 0))
        return Image.frombytes("RGB", SCREEN, buf)

    def _read_exact(self, n):
        data = b""
        while len(data) < n:
            chunk = self._proc.stdout.read(n - len(data))
            if not chunk:
                return None
            data += chunk
        return data

    def close(self):
        if self._proc:
            try:
                self._proc.kill()
                self._proc.wait(timeout=1)
            except Exception:
                pass
            self._proc = None


def open_source(path, fps=20):
    """Create a source from a path; determine its type by extension."""
    if not path:
        return SolidSource()
    ext = os.path.splitext(path)[1].lower()
    if ext in VIDEO_EXT:
        return VideoSource(path, fps=fps)
    if ext == ".gif":
        g = GifSource(path)
        return g if len(g.frames) > 1 else ImageSource(path)
    if ext in IMAGE_EXT:
        return ImageSource(path)
    # Try opening it as an image.
    return ImageSource(path)
