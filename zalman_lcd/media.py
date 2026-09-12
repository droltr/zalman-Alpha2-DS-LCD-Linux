"""Baseline JPEG encoder shared by the CLI and embedded LCD clients."""

import io
from PIL import Image

MAX_JPEG = 14000            # держим кадр в диапазоне Windows (~6..15КБ)


def encode_jpeg(img, quality=82, max_bytes=MAX_JPEG):
    """Кодирование кадра фона в JPEG, БАЙТ-СТРУКТУРНО как у Windows-приложения.

    Критично: пересобираем картинку через Image.new+paste, чтобы .info было
    ПУСТЫМ. Иначе PIL тащит метаданные исходника (у GIF в info есть 'comment')
    и вставляет в JPEG маркер 0xFE (COM). Аппаратный JPEG-декодер дисплея на
    неожиданном COM-маркере ЗАВИСАЕТ и перестаёт забирать данные с шины.
    Windows такой маркер никогда не шлёт. Также принудительно baseline + 4:2:0,
    без progressive/optimize/EXIF.

    Плюс держим РАЗМЕР кадра в диапазоне Windows (~10КБ): слишком большой JPEG
    дольше декодируется железным декодером и повышает шанс висяка. Снижаем
    quality, пока кадр не влезет в max_bytes (пол — 40)."""
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

