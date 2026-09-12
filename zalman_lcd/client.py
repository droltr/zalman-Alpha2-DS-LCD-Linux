"""Embedded LCD client for liquidctl and other host applications.

Owns one persistent serial connection; starts no daemon or sensor polling thread.
Static dashboard updates use the RLE overlay, never repeated flash downloads.
"""

import logging
import threading

import numpy as np
from PIL import Image, ImageColor, ImageOps

from .device import Display, DeviceError, find_tty, rgba_to_u32
from .media import encode_jpeg

_LOG = logging.getLogger(__name__)


class LcdClient:
    def __init__(self, bus=None, address=None):
        self.bus = bus
        self.address = address
        self._display = None
        self._lock = threading.RLock()
        self._last_pixels = None
        self._image = None
        self._brightness = 80
        self._rotation = 0

    def _connect(self):
        if self._display is not None:
            return self._display
        path = find_tty(self.bus, self.address)
        if path is None:
            raise DeviceError("Matching LCD serial port not found; cdc_acm must own the device")
        display = Display(path)
        try:
            display.brightness(self._brightness)
            # Replace any previous animated background once, then use overlays.
            display.video_download(1, 1)
            display.send_jpeg(encode_jpeg(Image.new("RGB", (320, 320), "black")))
            display.video_over()
        except Exception:
            display.close()
            raise
        self._display = display
        self._last_pixels = None
        _LOG.info("Zalman LCD connected through %s (cdc_acm)", path)
        return display

    def close(self):
        with self._lock:
            if self._display is not None:
                self._display.close()
                self._display = None
            self._last_pixels = None

    def _send(self, image):
        rotated = image.rotate(self._rotation) if self._rotation else image
        pixels = rgba_to_u32(rotated)
        try:
            display = self._connect()
            if self._last_pixels is not None and np.array_equal(pixels, self._last_pixels):
                return
            display.present()
            display.send_overlay(pixels)
            self._last_pixels = pixels
            _LOG.debug("Zalman LCD overlay updated")
        except (OSError, DeviceError):
            # Never retry indefinitely on CoolerControl's worker. The next update
            # reconnects, while errors remain visible to the caller.
            self.close()
            raise

    def set_image(self, path):
        with Image.open(path) as source:
            image = ImageOps.fit(source.convert("RGBA"), (320, 320))
        # A dashboard is a complete frame; composite transparency onto black.
        opaque = Image.new("RGBA", (320, 320), "black")
        opaque.alpha_composite(image)
        with self._lock:
            self._send(opaque)
            self._image = opaque

    def set_color(self, value):
        if len(value) == 6 and all(c in "0123456789abcdefABCDEF" for c in value):
            value = "#" + value
        image = Image.new("RGBA", (320, 320), ImageColor.getrgb(value) + (255,))
        with self._lock:
            self._send(image)
            self._image = image

    def set_brightness(self, value):
        value = int(value)
        if not 0 <= value <= 100:
            raise ValueError("Brightness must be between 0 and 100")
        with self._lock:
            self._brightness = value
            try:
                self._connect().brightness(value)
            except (OSError, DeviceError):
                self.close()
                raise

    def set_orientation(self, value):
        value = int(value)
        if value not in (0, 90, 180, 270):
            raise ValueError("Orientation must be 0, 90, 180 or 270")
        with self._lock:
            self._rotation = value
            if self._image is not None:
                self._send(self._image)
