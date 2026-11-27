"""Cheaper big blurs for every image card (the bot runs on a 25% CPU host).

A Gaussian blur costs time in proportion to its radius, and the cards use large soft glows. A blur that
wide is smooth anyway, so it is done on a 2x or 4x smaller copy and scaled back up: about 4-16 times less
work, no visible difference. Small blurs (text shadows and the like) are left exactly as they were.
Importing this module is all that is needed; it is imported by cogs/h2h.py.
"""
from PIL import Image, ImageFilter

_original_filter = Image.Image.filter


def _fast_filter(self, filt):
    radius = getattr(filt, "radius", None)
    if (isinstance(filt, ImageFilter.GaussianBlur) and isinstance(radius, (int, float)) and radius >= 12
            and min(self.size) >= 64 and self.mode in ("L", "RGB", "RGBA")):
        factor = 2 if radius < 24 else 4
        small = self.reduce(factor)           # same (non-premultiplied) averaging the full-size blur does
        small = _original_filter(small, ImageFilter.GaussianBlur(radius / factor))
        big = small.resize(self.size, Image.Resampling.BILINEAR)
        return big
    return _original_filter(self, filt)


if Image.Image.filter is not _fast_filter:
    Image.Image.filter = _fast_filter
