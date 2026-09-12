# -*- coding: utf-8 -*-
"""Zalman Alpha 2 display driver. USB CDC "USB Display", 0483:5740.

Transport: standard kernel CDC driver through /dev/ttyACM* (as on Windows).
The kernel manages bulk transfers and flow control, preventing a wedged
endpoint (unlike raw libusb with timeouts in the middle of a frame).

Background frame: cmd 0x05 (JPEG). Metrics line: cmd 0x07 (RLE + alpha).
Pixel = (A<<24)|(R<<16)|(G<<8)|B; A is alpha (0xFF opaque, 0x00 transparent).
Wake the screen with the verify handshake (HWCX-TECH-VRFY0/1 + challenge).
"""

import fcntl
import glob
import os
import random
import select
import struct
import termios
import time
import tty as _tty

import numpy as np

from . import dbg

VID_S, PID_S = "0483", "5740"
W = H = 320
N = W * H
CMD_ROTATE = 0x08
TIOCM_DTR = 0x002
TIOCM_RTS = 0x004


class DeviceError(Exception):
    pass


def _ids(tty_name):
    dev = os.path.realpath("/sys/class/tty/%s/device" % tty_name)
    for _ in range(6):
        vf, pf = os.path.join(dev, "idVendor"), os.path.join(dev, "idProduct")
        if os.path.isfile(vf) and os.path.isfile(pf):
            try:
                return (open(vf).read().strip(), open(pf).read().strip())
            except OSError:
                return None
        parent = os.path.dirname(dev)
        if parent == dev:
            break
        dev = parent
    return None


def find_tty():
    for p in sorted(glob.glob("/dev/ttyACM*")):
        if _ids(os.path.basename(p)) == (VID_S, PID_S):
            return p
    cands = sorted(glob.glob("/dev/ttyACM*"))
    return cands[0] if len(cands) == 1 else None


def _usb_sysfs_dir():
    """Sysfs directory for USB device 0483:5740 (for bus reset)."""
    for d in glob.glob("/sys/bus/usb/devices/*/idProduct"):
        base = os.path.dirname(d)
        try:
            if (open(os.path.join(base, "idVendor")).read().strip() == VID_S
                    and open(d).read().strip() == PID_S):
                return base
        except OSError:
            continue
    return None


def available():
    return _usb_sysfs_dir() is not None


# USBDEVFS_RESET ioctl — reset the device without physically unplugging it
_USBDEVFS_RESET = (ord("U") << 8) | 20


def usb_reset():
    """Reset the device through the kernel (USBDEVFS_RESET).

    Clear a wedged endpoint by resetting the device on the bus and
    re-enumerating it. Return True on success. No physical power
    disconnection is required.
    """
    base = _usb_sysfs_dir()
    if not base:
        return False
    try:
        busnum = int(open(os.path.join(base, "busnum")).read())
        devnum = int(open(os.path.join(base, "devnum")).read())
    except OSError:
        return False
    path = "/dev/bus/usb/%03d/%03d" % (busnum, devnum)
    try:
        fd = os.open(path, os.O_WRONLY)
    except OSError:
        return False
    try:
        fcntl.ioctl(fd, _USBDEVFS_RESET, 0)
        dbg.log("usb_reset OK on %s" % path)
        return True
    except OSError as e:
        dbg.log("usb_reset FAILED on %s: %s" % (path, e))
        return False
    finally:
        os.close(fd)


def wait_tty(timeout=6.0):
    """Wait for /dev/ttyACM* to appear after a reset/reconnection."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        p = find_tty()
        if p:
            return p
        time.sleep(0.1)
    return None


class Display:
    def __init__(self, path=None):
        self.path = path or find_tty()
        if not self.path:
            raise DeviceError("display 0483:5740 not found (/dev/ttyACM*)")
        self.fd = os.open(self.path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            _tty.setraw(self.fd)
            i, o, c, l, isp, osp, cc = termios.tcgetattr(self.fd)
            c &= ~termios.PARENB
            c &= ~termios.CSTOPB
            c &= ~termios.CSIZE
            c |= termios.CS8 | termios.CLOCAL | termios.CREAD
            if hasattr(termios, "CRTSCTS"):
                c &= ~termios.CRTSCTS
            i &= ~(termios.IXON | termios.IXOFF | termios.IXANY)
            o &= ~termios.OPOST
            spd = getattr(termios, "B1000000", termios.B115200)
            termios.tcsetattr(self.fd, termios.TCSANOW,
                              [i, o, c, l, spd, spd, cc])
            termios.tcflush(self.fd, termios.TCIOFLUSH)
            fcntl.ioctl(self.fd, termios.TIOCMBIS,
                        struct.pack("I", TIOCM_DTR | TIOCM_RTS))
        except Exception:
            os.close(self.fd)
            raise
        dbg.log("tty opened %s | %s" % (self.path, dbg.usb_state()))
        self.wake()

    # --- transport (kernel CDC through tty) ---
    # Threshold above which a write is considered suspiciously slow and logged.
    SLOW = 0.30

    def _bulk(self, data, timeout=1.5, tag="?"):
        # If the device stops accepting data (a stalled JPEG decoder,
        # etc.), writes hang. Detect the timeout and raise DeviceError
        # with details (stage, bytes sent, elapsed time). The daemon calls
        # usb_reset() and reconnects.
        mv = memoryview(data)
        n = len(mv)
        total = 0
        t0 = time.time()
        deadline = t0 + timeout
        stalls = 0
        while total < n:
            left = deadline - time.time()
            if left <= 0:
                el = time.time() - t0
                dbg.log("STALL/TIMEOUT stage=%s wrote=%d/%d elapsed=%.3fs "
                        "stalls=%d | %s" % (tag, total, n, el, stalls,
                                            dbg.usb_state()))
                raise DeviceError("write timeout stage=%s %d/%d after %.2fs"
                                  % (tag, total, n, el))
            _, w, _ = select.select([], [self.fd], [], left)
            if not w:
                stalls += 1
                continue
            try:
                total += os.write(self.fd, mv[total:])
            except BlockingIOError:
                stalls += 1
                continue
            except OSError as e:
                dbg.log("WRITE-ERR stage=%s wrote=%d/%d err=%s" % (tag, total, n, e))
                raise DeviceError("write error stage=%s: %s" % (tag, e))
        el = time.time() - t0
        if el >= self.SLOW:
            # Early sign that the device is struggling, before a complete freeze.
            dbg.log("SLOW-WRITE stage=%s bytes=%d elapsed=%.3fs stalls=%d | %s"
                    % (tag, n, el, stalls, dbg.usb_state()))
        return total

    def _read(self, length=64, timeout=0.3):
        r, _, _ = select.select([self.fd], [], [], timeout)
        if not r:
            return b""
        try:
            return os.read(self.fd, length)
        except (BlockingIOError, OSError):
            return b""

    # --- protocol ---
    def wake(self):
        """Wake the screen after a cold start."""
        try:
            self._bulk(b"\x01HWCX-TECH-VRFY0", tag="vrfy0"); self._read(); time.sleep(0.02)
            self._bulk(b"\x01HWCX-TECH-VRFY1", tag="vrfy1"); self._read(); time.sleep(0.02)
            b = bytearray(16); b[0] = 1
            for k in range(1, 16):
                b[k] = random.randint(0, 255)
            b[8] = ((b[5] + b[6]) & 0xFF) ^ b[7]
            b[4] = ((b[2] + b[3]) & 0xFF) ^ b[1]
            b[9] = (~b[10]) & 0xFF
            b[11] = (-(sum(b[1:11]) & 0xFF)) & 0xFF
            self._bulk(bytes(b), tag="chal"); r = self._read()
            dbg.log("wake -> %r" % r)
        except DeviceError as e:
            dbg.log("wake FAILED: %s" % e)

    def send_jpeg(self, jpg):
        """BACKGROUND frame (cmd 0x05): header + JPEG (padded to 64) + 0x00 byte."""
        pad = (-len(jpg)) % 64
        self._bulk(bytes([0x05, 0, 0, 0]) + struct.pack("<I", len(jpg))
                   + b"\x00" * 8, tag="bg-hdr")
        self._bulk(jpg + b"\x00" * pad, tag="bg-body")
        self._bulk(b"\x00", tag="bg-term")

    def send_overlay(self, u32):
        """OVERLAY line (cmd 0x07, RLE+alpha): header + body (padded to 64) +
        0x00 + 16×0x00. As in the original DLL: body length is a multiple of 64."""
        body = _rle(u32) + b"\x00\x00\x00\x00"
        clen = len(body)
        pad = (-clen) % 64
        self._bulk(bytes([0x07, 0x03, 0, 0]) + struct.pack("<I", clen)
                   + struct.pack("<HHHH", 0, 0, W, H), tag="ov-hdr")
        self._bulk(body + b"\x00" * pad, tag="ov-body")
        self._bulk(b"\x00", tag="ov-term")
        self._bulk(b"\x00" * 16, tag="ov-tail")

    def video_download(self, fps, count):
        """0x02 SetVideoDownload — start uploading video to device flash.
        16-byte header: [0x02, fps, count>>8, count&0xFF, 0×12]
        (in the DLL: buf[1]=fps, buf[2:4]=count big-endian; verified in captures:
        241 and 118 frames). Send count frames with 0x05, then video_over();
        the device loops the uploaded video from flash on its own.
        This matches Windows, which does not stream 0x05 continuously (that fills
        a ~6 MB buffer and freezes the device)."""
        self._bulk(bytes([0x02, fps & 0xFF, (count >> 8) & 0xFF, count & 0xFF])
                   + b"\x00" * 12, tag="vid-dl")

    def video_over(self):
        """0x06 SetVideoOver — finish uploading; the device starts playing
        the uploaded video from flash. 16-byte header: [0x06, 0×15]."""
        self._bulk(bytes([0x06]) + b"\x00" * 15, tag="vid-over")

    def present(self):
        """Command 0x00 (16 zero bytes, len=0) — commit/flush the display
        pipeline. Windows sends it once per second within the 0x05 stream
        (56 times in 38 s in the capture, ~1.02 s apart). Without it, data
        accumulates in the decoder and causes a hard freeze after N frames
        (larger frames fill it faster). One 16-byte transfer, no terminator."""
        self._bulk(b"\x00" * 16, tag="present")

    def brightness(self, value, rotate=0xFF):
        v = 0xFF if value is None else (0xFF if value > 100
                                        else (value * 90) // 100 + 10)
        self._bulk(bytes([CMD_ROTATE, rotate & 0xFF, v & 0xFF]) + b"\x00" * 13,
                   tag="bright")

    def flush_tx(self):
        """Flush the transmit queue without closing the port, like PurgeComm(TXABORT)
        in the original DLL. Windows recovers from a stalled frame this way:
        discard pending output and continue using the same descriptor, without
        closing/reopening it. Logs suggest that closing the port turns a stalled
        decoder into a hard freeze."""
        try:
            termios.tcflush(self.fd, termios.TCOFLUSH)
        except Exception:
            pass

    def close(self):
        # IMPORTANT: flush buffers BEFORE close(). If the device is stalled
        # and not accepting output, os.close() blocks in tty_wait_until_sent
        # indefinitely, waiting for the buffer to drain. tcflush discards
        # unsent data so close returns immediately.
        try:
            termios.tcflush(self.fd, termios.TCIOFLUSH)
        except Exception:
            pass
        try:
            os.close(self.fd)
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


# ---------------------------------------------------------------------------
# RLE encoder (matches the original byte for byte)
# ---------------------------------------------------------------------------
def _rle(a):
    out = bytearray()
    ne = np.nonzero(a[1:] != a[:-1])[0] + 1
    bounds = np.concatenate(([0], ne, [len(a)]))
    starts = bounds[:-1]
    runs = np.diff(bounds)
    k = 0
    m = len(starts)
    while k < m:
        st = int(starts[k])
        run = int(runs[k])
        if run >= 5:
            out += struct.pack("<II", 0x02000000 | run, int(a[st]))
            k += 1
        else:
            first = st
            total = 0
            while k < m and int(runs[k]) < 5:
                total += int(runs[k])
                k += 1
            out += struct.pack("<I", 0x01000000 | total)
            out += a[first:first + total].tobytes()
    return bytes(out)


def rgba_to_u32(img):
    if img.mode != "RGBA":
        img = img.convert("RGBA")
    a = np.asarray(img, dtype=np.uint32)
    R, G, B, A = a[:, :, 0], a[:, :, 1], a[:, :, 2], a[:, :, 3]
    return ((A << 24) | (R << 16) | (G << 8) | B).reshape(-1).astype(np.uint32)
