"""Player Of The Day card, drawn completely in Pillow (no template image needed).

1122 x 1402. Icy mountain scene, big title from the server name, team logo, framed photo,
angled name plate, "vs opponent" and three stat boxes. data = {
    title, initials, team, team_logo (bytes|None), team_color, opponent, opponent_logo,
    photo (bytes|None), player, stage (str), batting, bowling, fielding }
"""
import io
import math
import random
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, ImageOps

W, H = 1122, 1402
HERE = Path(__file__).parent
_BG = {}


def _font(size, kind="display"):
    names = {
        "display": [HERE / "Bangers-Regular.ttf"],
        "head": [HERE / "Agrandir-TextBold.otf"],
        "script": ["/usr/share/fonts/truetype/google-fonts/Poppins-BoldItalic.ttf",
                   "/usr/share/fonts/truetype/dejavu/DejaVuSerif-BoldItalic.ttf"],
    }[kind]
    names = list(names) + [HERE / "Agrandir-TextBold.otf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]
    for p in names:
        try:
            return ImageFont.truetype(str(p), int(size))
        except OSError:
            continue
    return ImageFont.load_default(int(size))


def _fit(d, text, width, size, kind, minimum=18):
    while size > minimum and d.textlength(text, font=_font(size, kind)) > width:
        size -= 2
    return _font(size, kind)


def _clip(d, text, width, font):
    if d.textlength(text, font=font) <= width:
        return text
    while text and d.textlength(text + "…", font=font) > width:
        text = text[:-1]
    return text.rstrip() + "…"


def _mix(a, b, t):
    return tuple(int(a[i] * (1 - t) + b[i] * t) for i in range(3))


def _open(data):
    if not data:
        return None
    try:
        im = Image.open(io.BytesIO(data))
        im.load()
        return im.convert("RGBA")
    except Exception:
        return None


def _ridge(rng, base, amp, step=36):
    pts, y = [], base
    for x in range(-step, W + step * 2, step):
        y = max(base - amp, min(base + amp, y + rng.uniform(-amp * .55, amp * .55)))
        pts.append((x, y))
    return pts


def _background(tint):
    """Cached sky + mist + mountains + pines + dark edges. tint nudges the blue toward the team."""
    key = tuple(tint)
    if key in _BG:
        return _BG[key].copy()
    rng = random.Random(7)
    top, bottom = (196, 224, 246), (86, 132, 182)
    sky = Image.linear_gradient("L").resize((W, H))
    img = Image.composite(Image.new("RGB", (W, H), bottom), Image.new("RGB", (W, H), top), sky)
    img = Image.blend(img, Image.new("RGB", (W, H), tint), 0.10)
    d = ImageDraw.Draw(img, "RGBA")
    # soft sun glow
    glow = Image.new("L", (W, H), 0)
    ImageDraw.Draw(glow).ellipse((W // 2 - 380, 40, W // 2 + 380, 700), fill=120)
    img = Image.composite(Image.new("RGB", (W, H), (240, 248, 255)), img, glow.filter(ImageFilter.GaussianBlur(110)))
    d = ImageDraw.Draw(img, "RGBA")
    layers = [(560, 70, (150, 188, 222), 0), (700, 90, (112, 154, 198), 1), (840, 80, (78, 118, 168), 2),
              (980, 70, (48, 82, 128), 3)]
    for base, amp, col, n in layers:
        pts = _ridge(rng, base, amp)
        d.polygon(pts + [(W + 80, H), (-40, H)], fill=col + (255,))
        # snowy highlights: lighter edge line along the ridge
        d.line(pts, fill=_mix(col, (255, 255, 255), .45) + (255,), width=3)
        mist = Image.new("L", (W, H), 0)
        ImageDraw.Draw(mist).rectangle((0, base + amp - 30, W, base + amp + 90), fill=110)
        img = Image.composite(Image.new("RGB", (W, H), (232, 244, 255)), img, mist.filter(ImageFilter.GaussianBlur(40)))
        d = ImageDraw.Draw(img, "RGBA")
    # pine trees along the bottom + sides
    for _ in range(95):
        x = rng.choice([rng.uniform(-30, 260), rng.uniform(W - 260, W + 30), rng.uniform(0, W)])
        y = rng.uniform(1000, 1420)
        h = rng.uniform(70, 190)
        col = _mix((26, 52, 82), (10, 24, 44), min(1, (y - 1000) / 420))
        for k in range(4):
            top_y = y - h + k * h * .23
            w = h * (.20 + k * .09)
            d.polygon([(x, top_y), (x - w, top_y + h * .36), (x + w, top_y + h * .36)], fill=col + (255,))
        d.rectangle((x - 3, y - h * .02, x + 3, y + h * .1), fill=col + (255,))
    # dark brushy vignette on the edges
    vig = Image.new("L", (W, H), 0)
    vd = ImageDraw.Draw(vig)
    for _ in range(90):
        side = rng.choice("tblr")
        r = rng.uniform(40, 110)
        if side == "t":
            x, y = rng.uniform(-40, W + 40), rng.uniform(-60, -10)
        elif side == "b":
            x, y = rng.uniform(-40, W + 40), rng.uniform(H + 10, H + 60)
        elif side == "l":
            x, y = rng.uniform(-60, -10), rng.uniform(-40, H + 40)
        else:
            x, y = rng.uniform(W + 10, W + 60), rng.uniform(-40, H + 40)
        vd.ellipse((x - r, y - r * .5, x + r, y + r * .5), fill=255)
    vig = vig.filter(ImageFilter.GaussianBlur(34))
    img = Image.composite(Image.new("RGB", (W, H), (8, 16, 34)), img, vig.point(lambda v: int(v * .7)))
    _BG[key] = img
    return img.copy()


def _text_stroke(d, xy, text, font, fill, stroke, sw, anchor="mm"):
    d.text(xy, text, font=font, fill=fill, anchor=anchor, stroke_width=sw, stroke_fill=stroke)


def _crown(d, cx, y, w, fill, edge):
    h = w * .55
    x0 = cx - w / 2
    pts = [(x0, y + h), (x0, y + h * .25), (x0 + w * .25, y + h * .6), (cx, y), (x0 + w * .75, y + h * .6),
           (x0 + w, y + h * .25), (x0 + w, y + h)]
    d.polygon(pts, fill=fill, outline=edge)
    d.line(pts + [pts[0]], fill=edge, width=3)
    for px, py in ((x0, y + h * .25), (cx, y), (x0 + w, y + h * .25)):
        d.ellipse((px - 7, py - 7, px + 7, py + 7), fill=(255, 255, 255), outline=edge, width=2)


def _circle(image, data, size, ring, initial="?"):
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    od = ImageDraw.Draw(out)
    od.ellipse((0, 0, size - 1, size - 1), fill=ring + (255,))
    inner = size - 14
    src = _open(data)
    if src is None:
        face = Image.new("RGBA", (inner, inner), (20, 34, 60, 255))
        fd = ImageDraw.Draw(face)
        fd.text((inner // 2, inner // 2), initial[:1].upper(), font=_font(inner // 2, "display"), fill=(255, 255, 255), anchor="mm")
    else:
        face = ImageOps.fit(src, (inner, inner), Image.LANCZOS)
    mask = Image.new("L", (inner, inner), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, inner - 1, inner - 1), fill=255)
    out.paste(face, (7, 7), mask)
    return out


def _shadowed(base, layer, xy, blur=14, alpha=120):
    pad = blur * 3
    sh = Image.new("RGBA", (layer.size[0] + pad * 2, layer.size[1] + pad * 2), (0, 0, 0, 0))
    a = Image.new("L", sh.size, 0)
    a.paste(layer.getchannel("A").point(lambda v: int(v * alpha / 255)), (pad, pad))
    sh.putalpha(a.filter(ImageFilter.GaussianBlur(blur)))
    base.alpha_composite(sh, (xy[0] - pad + 4, xy[1] - pad + 10))
    base.alpha_composite(layer, xy)


def _visible(color):
    r, g, b = color
    lum = .299 * r + .587 * g + .114 * b
    if lum < 70:
        return _mix(color, (255, 255, 255), .4)
    return color


def render_potd(data):
    team_col = _visible(tuple(data.get("team_color") or (46, 134, 222)))
    dark = _mix(team_col, (6, 14, 30), .72)
    img = _background(_mix((120, 170, 220), team_col, .35)).convert("RGBA")
    d = ImageDraw.Draw(img, "RGBA")
    cx = W // 2

    # ---- title block
    _crown(d, cx, 44, 118, (255, 255, 255), (30, 60, 100))
    title = (data.get("title") or "PLAYER").upper()
    f = _fit(d, title, 940, 176, "display", 60)
    _text_stroke(d, (cx + 5, 232 + 6), title, f, (10, 24, 50), (10, 24, 50), 4)
    _text_stroke(d, (cx, 232), title, f, (255, 255, 255), (28, 62, 108), 7)
    # initials watermark behind the photo
    ini = (data.get("initials") or title[:2]).upper()
    wm = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(wm).text((cx, 640), ini, font=_font(560, "display"), fill=(255, 255, 255, 70), anchor="mm")
    img.alpha_composite(wm.filter(ImageFilter.GaussianBlur(2)))
    d = ImageDraw.Draw(img, "RGBA")
    # script line in a ribbon
    ribbon = [(cx - 330, 330), (cx + 330, 330), (cx + 360, 372), (cx + 330, 414), (cx - 330, 414), (cx - 360, 372)]
    d.polygon([(x, y + 6) for x, y in ribbon], fill=(0, 0, 0, 90))
    d.polygon(ribbon, fill=dark + (245,), outline=(255, 255, 255, 255))
    d.line(ribbon + [ribbon[0]], fill=(255, 255, 255, 230), width=3)
    _text_stroke(d, (cx, 372), "Player Of The Day", _font(54, "script"), (255, 255, 255), dark, 2)
    stage = (data.get("stage") or "").strip()
    if stage:
        sf = _fit(d, stage.upper(), 560, 26, "head", 14)
        _text_stroke(d, (cx, 446), stage.upper(), sf, (255, 255, 255), (12, 28, 56), 3)

    # ---- framed photo
    fx1, fy1, fx2, fy2 = 296, 480, 826, 1060
    frame = Image.new("RGBA", (fx2 - fx1 + 40, fy2 - fy1 + 40), (0, 0, 0, 0))
    fd = ImageDraw.Draw(frame)
    fd.rounded_rectangle((0, 0, frame.size[0] - 1, frame.size[1] - 1), radius=34, fill=team_col + (255,))
    fd.rounded_rectangle((8, 8, frame.size[0] - 9, frame.size[1] - 9), radius=28, fill=(255, 255, 255, 255))
    pw, ph = fx2 - fx1, fy2 - fy1
    photo = _open(data.get("photo"))
    if photo is None:
        pic = Image.new("RGBA", (pw, ph), dark + (255,))
        ImageDraw.Draw(pic).text((pw // 2, ph // 2), (data.get("player") or "?")[:1].upper(),
                                 font=_font(300, "display"), fill=(255, 255, 255, 200), anchor="mm")
    else:
        pic = ImageOps.fit(photo, (pw, ph), Image.LANCZOS, centering=(.5, .3))
    pm = Image.new("L", (pw, ph), 0)
    ImageDraw.Draw(pm).rounded_rectangle((0, 0, pw - 1, ph - 1), radius=22, fill=255)
    frame.paste(pic, (20, 20), pm)
    _shadowed(img, frame, (fx1 - 20, fy1 - 20), 22, 170)
    d = ImageDraw.Draw(img, "RGBA")

    # ---- team on the left, opponent on the right
    def side(cx_, label, logo, name):
        ring = _circle(img, logo, 190, (255, 255, 255), name)
        _shadowed(img, ring, (cx_ - 95, 560))
        dd = ImageDraw.Draw(img, "RGBA")
        nf = _fit(dd, name.upper(), 220, 28, "head", 14)
        t = _clip(dd, name.upper(), 220, nf)
        _text_stroke(dd, (cx_, 786), t, nf, (255, 255, 255), (10, 24, 50), 4)
        _text_stroke(dd, (cx_, 540), label, _font(30, "head"), (255, 255, 255), (10, 24, 50), 4)
    side(150, "TEAM", data.get("team_logo"), data.get("team") or "TEAM")
    if data.get("opponent"):
        side(W - 150, "VS", data.get("opponent_logo"), data["opponent"])

    # ---- angled name plate
    py1, py2 = 1000, 1108
    plate = [(110, py1 + 20), (W - 60, py1 - 10), (W - 110, py2 - 20), (60, py2 + 10)]
    d.polygon([(x, y + 10) for x, y in plate], fill=(0, 0, 0, 110))
    d.polygon(plate, fill=dark + (255,))
    d.polygon([(x, y) for x, y in plate], outline=team_col + (255,))
    d.line(plate + [plate[0]], fill=team_col + (255,), width=6)
    name = (data.get("player") or "PLAYER").upper()
    nf = _fit(d, name, 780, 84, "display", 34)
    _text_stroke(d, (cx, 1050), name, nf, (255, 255, 255), (6, 14, 30), 3)
    tm = (data.get("team") or "").upper()
    if tm:
        tag = _clip(d, tm, 520, _font(26, "head"))
        tw = int(d.textlength(tag, font=_font(26, "head"))) + 56
        box = (cx - tw // 2, 1104, cx + tw // 2, 1144)
        d.rounded_rectangle(box, radius=20, fill=(255, 255, 255, 255))
        d.text((cx, 1125), tag, font=_font(26, "head"), fill=dark, anchor="mm")

    # ---- three stat boxes
    boxes = [("BATTING", data.get("batting") or "DNB"), ("BOWLING", data.get("bowling") or "DNB"),
             ("FIELDING", data.get("fielding") or "DNF")]
    bw, gap = 320, 28
    x0 = (W - (bw * 3 + gap * 2)) // 2
    y1, y2 = 1190, 1330
    for i, (label, value) in enumerate(boxes):
        bx = x0 + i * (bw + gap)
        d.rounded_rectangle((bx, y1 + 8, bx + bw, y2 + 8), radius=26, fill=(0, 0, 0, 100))
        d.rounded_rectangle((bx, y1, bx + bw, y2), radius=26, fill=dark + (250,), outline=team_col + (255,), width=4)
        d.rounded_rectangle((bx + 4, y1 + 4, bx + bw - 4, y1 + 50), radius=22, fill=team_col + (255,))
        d.rectangle((bx + 4, y1 + 30, bx + bw - 4, y1 + 50), fill=team_col + (255,))
        tc = (255, 255, 255) if (.299 * team_col[0] + .587 * team_col[1] + .114 * team_col[2]) < 170 else (10, 20, 40)
        d.text((bx + bw // 2, y1 + 28), label, font=_font(26, "head"), fill=tc, anchor="mm")
        vf = _fit(d, str(value), bw - 36, 66, "display", 28)
        d.text((bx + bw // 2, y1 + 96), str(value), font=vf, fill=(255, 255, 255), anchor="mm")
    d.text((cx, 1372), "GOOGLI MANAGER", font=_font(16, "head"), fill=(190, 214, 238, 200), anchor="mm")

    out = io.BytesIO()
    img.convert("RGB").save(out, "PNG", optimize=False)
    return out.getvalue()
