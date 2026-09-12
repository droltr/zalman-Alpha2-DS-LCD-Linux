# Zalman Alpha 2 display protocol (reverse-engineered from Zalman OZ)

Recovered from `LcdComm.dll` (native x86-64) and USB captures (USBPcap) of the
working Windows application. The original notes report verification on a physical device.

> These notes describe the initial reverse-engineering findings. The current
> driver uses the kernel CDC transport through `/dev/ttyACM*` and uploads
> backgrounds to flash with `0x02` / `0x05` / `0x06` for autonomous playback.
> The libusb and continuous-streaming descriptions below are historical;
> see [README.md](README.md) and [device.py](zalman_lcd/device.py) for current behavior.

## Overview

- Zalman OZ is a **.NET/WPF** application, not Lua. The discovered `*.lua` files
  belong to the embedded VLC player for video themes and are unrelated to the protocol.
- Device: USB **`VID 0483 : PID 5740`** ("USB Display", STM CDC). The second
  device, `0145:2001`, is the pump/fan/RGB controller (HID), not the display.
- Screen: **320×320**.
- Transport: **bulk OUT, endpoint `0x02`**. The initial driver used **libusb**
  directly, bypassing `cdc_acm`, to simplify control over transfer integrity.
- Identify responses arrive on bulk IN `0x82`.

## Wake-up (verify) — required after a cold start

The screen stays dark until the host completes a handshake. Sequence (bulk OUT):

1. `01` + `"HWCX-TECH-VRFY0"` (16 bytes)
2. `01` + `"HWCX-TECH-VRFY1"` (16 bytes)
3. A 16-byte challenge: `[01, r1..r15]`, where each r is random, then fields
   are overwritten: `b[8]=(b[5]+b[6])^b[7]`, `b[4]=(b[2]+b[3])^b[1]`,
   `b[9]=~b[10]`, checksum `b[11]=(-sum(b[1..10]))&0xFF`.

The device responds with 17 bytes containing `"HWCX-TECH-320x320"` (the model).
DTR/RTS must also be asserted when using a tty; this is not required with libusb.

## Pixels and layers

- Each pixel is 32 bits: **`(A<<24)|(R<<16)|(G<<8)|B`** (BGRA), where **A is alpha**.
  A=0xFF is opaque; A=0 is transparent and reveals the layer underneath.
- Two layers are alpha-composited: **background** (`0x05` frames) and **overlay** (`0x07`).

## Commands

### 0x05 — background frame (JPEG) ★ smooth video

```text
[05,00,00,00, jpeglen(u32 LE), 0×8]   (16-byte header, separate transfer)
<JPEG data>                           (ffd8…ffd9), zero-padded to a multiple of 64
```

Compressed JPEG is approximately 10 KB, compared with approximately 400 KB raw,
allowing roughly 30–60 fps over full-speed USB for smooth video/GIF playback.
The initial implementation streamed frames consecutively, retaining the background layer.

### 0x07 — overlay (RLE + alpha)

```text
[07, sub, 0,0, clen(u32 LE), x(u16) y(u16) w(u16) h(u16)]   (16 bytes)
<RLE body> + terminator 00 00 00 00
```

- `sub` is 0x03. The two sub-buffers are combined for display; write to only one.
- `x=y=0, w=h=320` (full frame). **Do not use partial regions: they hang the
  firmware parser, requiring a power cycle.**
- RLE operates on 32-bit pixels:
  - RUN: `0x02000000 | count`, then one pixel repeated count times.
  - LITERAL: `0x01000000 | count`, then count pixels.
  - End token: `0x00000000`.
- A transparent background (A=0) with opaque text (A=0xFF) places the metrics
  line over the background. The overlay persists and is composited over `0x05` frames.

### 0x08 — brightness / rotation

```text
[08, rotate, brightness, 0…]   (16 bytes)
```

`0xFF` in a field means "leave unchanged". Brightness is scaled as `v*90/100+10`.
The driver performs rotation in software instead of using this command.

### 0x02 / 0x06 — upload a theme to flash

`[02, fps, count(u16 BE)]` + count×(`0x05`+JPEG) + `[06,…]` uploads a theme to flash.
The initial notes observed playback after a device restart and did not identify
how to start it at runtime, so the initial driver used `0x05` streaming instead.
The current driver uses these commands: `0x06` completes the upload and starts
playback from flash (see `video_begin()` and `video_over()` in `device.py`).

## `.th` format (stock themes)

`FF FD×6 FF` + u32 BE: width, height, fps, frameCount, dataOffset; followed by an
offset table and concatenated 320×320 JPEG frames.

## Pitfalls observed during testing

- A **partial region** in `0x07` (w<320 or h<320) stalls the parser and requires
  a power cycle. The driver always sends a full frame.
- A **bulk-transfer timeout mid-frame** causes an incomplete transfer, loss of
  synchronization, and a wedged endpoint. Use a long timeout; do not interrupt a frame.
- Writing to flash with an incorrect format also froze the device. Avoid
  experimenting with unverified formats on physical hardware.
