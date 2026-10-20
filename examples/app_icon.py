#!/usr/bin/env python3
"""app_icon.py — draws dd's app icon and packs it as .icns (pure Python; no iconutil needed).

  python3 examples/app_icon.py OUT.icns [--png OUT.png]
  python3 examples/app_icon.py --selftest
"""
from __future__ import annotations

import io
import struct
import sys
from pathlib import Path

# icns element types that hold PNG data, by pixel size (ic11..ic14 are the @2x variants)
ICNS_TYPES = {16: b"icp4", 32: b"icp5", 64: b"icp6", 128: b"ic07", 256: b"ic08", 512: b"ic09", 1024: b"ic10"}
RETINA_TYPES = {32: b"ic11", 64: b"ic12", 256: b"ic13", 512: b"ic14"}
FONTS = ["/usr/share/fonts/truetype/google-fonts/Poppins-Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
         "/System/Library/Fonts/SFCompact.ttf", "/System/Library/Fonts/Supplemental/Arial Bold.ttf", "/Library/Fonts/Arial Bold.ttf"]


def draw(size: int = 1024):
    from PIL import Image, ImageDraw, ImageFilter, ImageFont
    s = size
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    # macOS-style rounded square (continuous corner look), dark slate with a soft diagonal glow
    base = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    grad = Image.new("RGBA", (s, s))
    px = grad.load()
    for y in range(s):
        for x in range(s):
            t = (x + y) / (2 * s)
            r = int(24 + 30 * t)
            g = int(27 + 38 * t)
            b = int(38 + 70 * t)
            px[x, y] = (r, g, b, 255)
    mask = Image.new("L", (s, s), 0)
    ImageDraw.Draw(mask).rounded_rectangle((s * 0.08, s * 0.08, s * 0.92, s * 0.92), radius=int(s * 0.19), fill=255)
    base.paste(grad, (0, 0), mask)
    # accent ring: a listening "pulse" in dd green
    ring = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    rd = ImageDraw.Draw(ring)
    rd.ellipse((s * 0.22, s * 0.22, s * 0.78, s * 0.78), outline=(158, 206, 106, 200), width=int(s * 0.028))
    ring = ring.filter(ImageFilter.GaussianBlur(s * 0.004))
    base.alpha_composite(ring)
    # the letters
    d = ImageDraw.Draw(base)
    font = None
    for f in FONTS:
        if Path(f).exists():
            font = ImageFont.truetype(f, int(s * 0.36))
            break
    font = font or ImageFont.load_default()
    text = "dd"
    bbox = d.textbbox((0, 0), text, font=font)
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    d.text(((s - w) / 2 - bbox[0], (s - h) / 2 - bbox[1] - s * 0.01), text, font=font, fill=(240, 243, 248, 255))
    img.alpha_composite(base)
    return img


def png_bytes(img, size: int) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    img.resize((size, size), Image.LANCZOS).save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def icns_bytes(img) -> bytes:
    entries = b""
    for size, typ in sorted(ICNS_TYPES.items()):
        data = png_bytes(img, size)
        entries += typ + struct.pack(">I", 8 + len(data)) + data
    for size, typ in sorted(RETINA_TYPES.items()):
        data = png_bytes(img, size)
        entries += typ + struct.pack(">I", 8 + len(data)) + data
    return b"icns" + struct.pack(">I", 8 + len(entries)) + entries


def parse_icns(blob: bytes) -> dict[bytes, int]:
    """type -> payload length; validates the container structure."""
    assert blob[:4] == b"icns" and struct.unpack(">I", blob[4:8])[0] == len(blob), "bad icns header"
    out, i = {}, 8
    while i < len(blob):
        typ, ln = blob[i:i + 4], struct.unpack(">I", blob[i + 4:i + 8])[0]
        assert 8 < ln <= len(blob) - i, "bad icns element"
        payload = blob[i + 8:i + ln]
        assert payload[:8] == b"\x89PNG\r\n\x1a\n", f"{typ!r} is not PNG"
        out[typ] = len(payload)
        i += ln
    return out


def main(argv: list[str]) -> int:
    if "--selftest" in argv:
        return selftest()
    if not argv:
        print(__doc__)
        return 2
    img = draw()
    Path(argv[0]).write_bytes(icns_bytes(img))
    if "--png" in argv:
        Path(argv[argv.index("--png") + 1]).write_bytes(png_bytes(img, 512))
    print(f"wrote {argv[0]}")
    return 0


def selftest() -> int:
    img = draw(256)
    blob = icns_bytes(img)
    kinds = parse_icns(blob)
    assert set(kinds) == set(ICNS_TYPES.values()) | set(RETINA_TYPES.values()), kinds
    assert all(v > 100 for v in kinds.values())
    assert img.getpixel((128, 128))[3] == 255 and img.getpixel((2, 2))[3] == 0  # opaque centre, transparent corner
    print(f"app_icon selftest ok ({len(kinds)} icns elements, {len(blob) // 1024} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
