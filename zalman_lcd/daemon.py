# -*- coding: utf-8 -*-
"""Daemon: background (JPEG stream, cmd 0x05) + metrics line (overlay, cmd 0x07),
as in the Windows application: refresh the overlay every few background
frames (otherwise the next background frame covers it).
"""

import io
import os
import signal
import time

from PIL import Image, ImageSequence

from . import config as cfgmod
from . import dbg
from . import device
from . import sources
from .render import StatsBar, to_u32
from .sensors import Sensors

_ROT = {0: None, 90: Image.ROTATE_90, 180: Image.ROTATE_180,
        270: Image.ROTATE_270}
MAX_FRAMES = 360            # device flash buffer is ~6 MB; limit the frame set
MAX_UPLOAD_BYTES = 5_000_000  # cap total frame data at ~5 MB (buffer is ~6 MB)
STATS_INTERVAL = 2.0        # present(0x00) + overlay every 2 s (fewer redraws)
STALL_ESCALATE = 12         # consecutive stalled frames before reconnect+usb_reset
HEARTBEAT = 30.0            # interval between log heartbeat entries


MAX_JPEG = 14000            # keep frames in the Windows size range (~6–15 KB)


def _jpeg(img, quality=82, max_bytes=MAX_JPEG):
    """Encode a background JPEG with the same byte structure as the Windows app.

    Crucial: rebuild the image with Image.new+paste so .info is empty.
    Otherwise PIL carries source metadata (GIF info contains 'comment')
    into the JPEG as a 0xFE (COM) marker. The display's hardware JPEG decoder
    hangs on an unexpected COM marker and stops accepting bus data.
    Windows never sends this marker. Also enforce baseline JPEG and 4:2:0,
    without progressive encoding, optimization, or EXIF.

    Keep frame size in the Windows range (~10 KB): larger JPEGs take longer
    for the hardware decoder to process and increase the chance of a freeze.
    Reduce quality until the frame fits max_bytes (minimum quality: 40)."""
    rgb = img.convert("RGB")
    clean = Image.new("RGB", rgb.size)
    clean.paste(rgb)
    q = quality
    while True:
        b = io.BytesIO()
        clean.save(b, "JPEG", quality=q, subsampling="4:2:0",
                   progressive=False, optimize=False)
        data = b.getvalue()
        if len(data) <= max_bytes or q <= 40:
            return data
        q -= 8


class Daemon:
    def __init__(self, verbose=True):
        self.verbose = verbose
        self.running = True
        self.cfg = cfgmod.load()
        self.sensors = Sensors(self.cfg.get("gpu", "auto"))
        self._cfg_mtime = cfgmod.mtime()
        self.stats = StatsBar(self.cfg, self.sensors)
        self._prep_key = None
        self._frames = None         # JPEG frames to upload to flash
        self._fps = 10
        self._need_upload = True
        self._last_brightness = None
        self._ov_cache = None       # (text, u32) overlay cache
        self._ov_key = None
        self._blank = None          # transparent overlay for clearing stats

    def log(self, *a):
        if self.verbose:
            print("[zalman-display]", *a, flush=True)

    def _rot_img(self, img):
        r = _ROT.get(int(self.cfg.get("rotate", 0)) % 360)
        return img.transpose(r) if r is not None else img

    def _prepare(self):
        """Build background frames (a list of JPEGs) for uploading to flash.
        Update self._frames/self._fps and set self._need_upload only when
        the background actually changes, avoiding redundant uploads."""
        bg = self.cfg.get("background")
        mt = os.path.getmtime(bg) if bg and os.path.isfile(bg) else 0
        rot = int(self.cfg.get("rotate", 0)) % 360
        key = (bg, mt, rot)
        if key == self._prep_key:
            return
        self._prep_key = key
        self._ov_key = None
        ext = os.path.splitext(bg)[1].lower() if bg else ""
        try:
            if bg and ext in sources.VIDEO_EXT:
                self._frames, self._fps = self._frames_video(bg)
                self.log("background: video", os.path.basename(bg), "| frames:",
                         len(self._frames))
            elif bg and os.path.isfile(bg):
                self._frames, self._fps = self._frames_image(bg)
                self.log("background:", os.path.basename(bg), "| frames:",
                         len(self._frames))
            else:
                self._frames = [_jpeg(Image.new("RGB", (320, 320), (0, 0, 0)))]
                self._fps = 1
        except Exception as e:
            self.log("could not open background (%s), using black" % e)
            self._frames = [_jpeg(Image.new("RGB", (320, 320), (0, 0, 0)))]
            self._fps = 1
        self._need_upload = True

    def _cap_total(self, frames):
        """Keep the frame set within the ~6 MB flash buffer by limiting total bytes."""
        out, total = [], 0
        for f in frames:
            total += len(f)
            if total > MAX_UPLOAD_BYTES and out:
                self.log("frame set truncated to %d frames to fit the buffer" % len(out))
                break
            out.append(f)
        return out

    def _frames_image(self, path):
        """Image/GIF -> JPEG list, one frame at a time without retaining RGB data."""
        jpegs, durs = [], []
        with Image.open(path) as im:
            total = getattr(im, "n_frames", 1)
            take = min(total, MAX_FRAMES)
            step = total / take if take else 1
            want = {int(i * step) for i in range(take)}
            for idx, fr in enumerate(ImageSequence.Iterator(im)):
                if idx in want:
                    jpegs.append(_jpeg(self._rot_img(sources.fit(fr))))
                    durs.append(max(20, fr.info.get("duration", 100)))
        if not jpegs:
            jpegs = [_jpeg(Image.new("RGB", (320, 320), (0, 0, 0)))]
        if len(jpegs) > 1 and durs:
            avg = sum(durs) / len(durs) / 1000.0
            fps = max(1, min(30, round(1.0 / avg))) if avg else 15
        else:
            fps = 1
        return self._cap_total(jpegs), fps

    def _frames_video(self, path):
        """Video -> JPEG list (up to MAX_FRAMES frames) via ffmpeg."""
        fps = int(self.cfg.get("fps", 20)) or 20
        src = sources.VideoSource(path, fps=fps)
        jpegs = []
        try:
            for _ in range(MAX_FRAMES):
                img = src.next()
                jpegs.append(_jpeg(self._rot_img(img)))
        finally:
            src.close()
        if not jpegs:
            jpegs = [_jpeg(Image.new("RGB", (320, 320), (0, 0, 0)))]
        return self._cap_total(jpegs), fps

    def _reload_if_changed(self):
        m = cfgmod.mtime()
        if m != self._cfg_mtime:
            self._cfg_mtime = m
            old_gpu = self.cfg.get("gpu", "auto")
            self.cfg = cfgmod.load()
            if self.cfg.get("gpu", "auto") != old_gpu:
                self.sensors.retarget(self.cfg.get("gpu", "auto"))
                self._ov_key = None          # GPU values will change — redraw
            self.stats.update(self.cfg)
            self._prepare()
            self._last_brightness = None
            self.log("configuration reloaded")

    def _apply_brightness(self, dev):
        b = int(self.cfg.get("brightness", 80))
        if b != self._last_brightness:
            dev.brightness(b)
            self._last_brightness = b

    def _overlay_u32(self):
        """Metrics overlay; re-encode only when values change.
        self._ov_key changes exactly when the overlay image changes, so the
        daemon sends an overlay only when needed, reducing flicker."""
        from .render import _lines as fmt
        ts = time.time()
        lines = tuple(fmt(self.sensors))
        sd = time.time() - ts
        if sd >= 0.2:
            dbg.log("SLOW-SENSORS %.3fs" % sd)
        key = (lines, self.cfg.get("rotate", 0), self.cfg.get("text_color"),
               self.cfg.get("position"), self.cfg.get("stats_bg"))
        if key != self._ov_key:
            self._ov_cache = to_u32(self._rot_img(self.stats.image()))
            self._ov_key = key
        return self._ov_cache

    def _blank_u32(self):
        """Fully transparent overlay to erase stats text on the device."""
        if self._blank is None:
            self._blank = to_u32(Image.new("RGBA", (320, 320), (0, 0, 0, 0)))
        return self._blank

    def _upload(self, dev):
        """Upload background to device flash: 0x02(fps,count) -> count× 0x05 -> 0x06.
        The device then loops it from flash; we send only the stats overlay.
        Errors propagate so run() reconnects and retries the upload.
        Frames must not be skipped, or the frame counter will go out of sync."""
        frames = self._frames or [_jpeg(Image.new("RGB", (320, 320), (0, 0, 0)))]
        total = sum(len(f) for f in frames)
        fps = max(1, min(255, int(self._fps)))
        dbg.log("upload: %d frames @ %dfps, %d bytes" % (len(frames), fps, total))
        dev.video_download(fps, len(frames))
        for f in frames:
            dev.send_jpeg(f)
        dev.video_over()
        self._need_upload = False
        dbg.log("upload done -> device playing from flash")

    def run(self):
        signal.signal(signal.SIGTERM, self._stop)
        signal.signal(signal.SIGINT, self._stop)
        dbg.enable(to_stderr=self.verbose)
        dbg.log("daemon start | %s" % dbg.usb_state())
        fails = 0
        while self.running:
            try:
                self._session()
                fails = 0
            except device.DeviceError as e:
                fails += 1
                self.log("device disconnected (%s)" % e)
                dbg.log("RECONNECT reason=%s fails=%d | %s"
                        % (e, fails, dbg.usb_state()))
                # On the first failure, simply reopen. Repeated failures indicate
                # a stalled device: a hardware USB reset clears the stall without
                # physically disconnecting power.
                if fails >= 2 and device.available():
                    self.log("resetting USB device…")
                    dbg.log("usb_reset attempt (fails=%d)" % fails)
                    if device.usb_reset():
                        p = device.wait_tty(8.0)
                        dbg.log("after reset: tty=%s | %s" % (p, dbg.usb_state()))
                        fails = 0
                    else:
                        self.log("reset failed (missing permissions for /dev/bus/usb?)")
                self._sleep(0.5)
            except Exception as e:
                self.log("error:", e, "— retrying in 2 s")
                dbg.log("UNEXPECTED %s: %s" % (type(e).__name__, e))
                self._sleep(2)
        self.log("stopped")

    def _session(self):
        ov_count = 0
        sess_t0 = time.time()
        dev = device.Display()
        self.log("device opened (cdc)")
        try:
            self._prepare()
            self._last_brightness = None
            self._apply_brightness(dev)
            # Upload the background to flash (as on Windows); the device then
            # loops it itself, without continuous 0x05 traffic filling the buffer.
            self._upload(dev)
            last_ov = 0.0
            hb_t = sess_t0
            hb_frames = 0          # overlays during this heartbeat interval
            consec = 0
            stalls_total = 0
            ov_sent = object()     # key of the last overlay actually sent
            cleared = False        # transparent overlay already sent (stats off)
            dbg.log("session begin: fps=%s stats=%s frames=%d rss=%.0fMB"
                    % (self._fps, self.stats.show,
                       len(self._frames or []), dbg.rss_mb()))
            while self.running:
                self._reload_if_changed()
                # Background changed — upload to flash again.
                if self._need_upload:
                    self._apply_brightness(dev)
                    self._upload(dev)
                    ov_sent = object()      # resend the overlay after uploading
                    cleared = False
                now = time.time()
                # Keep the connection open on stalls (close hangs the decoder).
                # As on Windows: flush TX and continue on the same descriptor.
                try:
                    self._apply_brightness(dev)
                    # Once per second: present(0x00) + overlay, in Windows order.
                    if now - last_ov >= STATS_INTERVAL:
                        dev.present()                   # 0x00 first (as on Windows)
                        if self.stats.show:
                            cleared = False
                            # Send an overlay only when the image changes.
                            # Redundant full-screen redraws cause flicker.
                            u = self._overlay_u32()
                            if self._ov_key != ov_sent:
                                dev.send_overlay(u)
                                ov_sent = self._ov_key
                                ov_count += 1
                                hb_frames += 1
                        elif not cleared:
                            # Stats turned off — erase the text once.
                            dev.send_overlay(self._blank_u32())
                            cleared = True
                            ov_sent = object()
                        last_ov = now
                    consec = 0
                except device.DeviceError as e:
                    consec += 1
                    stalls_total += 1
                    dbg.log("stall #%d (total %d): %s -> flush+continue"
                            % (consec, stalls_total, e))
                    dev.flush_tx()
                    if consec >= STALL_ESCALATE:
                        dbg.log("%d consecutive stalls -> escalating (reconnect+reset)"
                                % consec)
                        raise
                    self._sleep(0.1)
                    continue
                # Log heartbeat infrequently to limit disk usage.
                if now - hb_t >= HEARTBEAT:
                    dbg.log("hb ov=%d stalls=%d rss=%.0fMB | %s"
                            % (hb_frames, stalls_total, dbg.rss_mb(),
                               dbg.usb_state()))
                    hb_t = now
                    hb_frames = 0
                self._sleep(0.2)
        finally:
            dur = time.time() - sess_t0
            dbg.log("session end: ov=%d dur=%.1fs" % (ov_count, dur))
            tc = time.time()
            dev.close()
            dbg.log("closed in %.3fs" % (time.time() - tc))

    def _stop(self, *a):
        self.running = False

    def _sleep(self, dur):
        end = time.time() + dur
        while self.running and time.time() < end:
            time.sleep(min(0.1, end - time.time()))


def run(verbose=True, **kw):
    Daemon(verbose=verbose).run()
