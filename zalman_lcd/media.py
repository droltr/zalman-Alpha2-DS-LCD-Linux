"""Baseline JPEG encoder shared by the CLI and embedded LCD clients."""

import io
from PIL import Image

MAX_JPEG = 14000            # keep frames in the Windows size range (~6–15 KB)


def encode_jpeg(img, quality=82, max_bytes=MAX_JPEG):
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

