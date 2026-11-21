"""Premium image pages for the team profile (+testteaminfo).

Pure Pillow, no Discord objects: the command gathers the data, this module draws it.

Pages:
  1  Overview   - hero banner, record ring, captain / vice-captain, team leaders
  2  Team stats - batting / bowling / fielding totals with top-5 bar charts
  3+ Squad      - ranked scoreboard table, 10 players per page

The whole colour theme is taken from the team's logo.
"""
import colorsys
import io
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, ImageOps

W, H = 1600, 900
ROWS_PER_SQUAD_PAGE = 10
DEFAULT_ACCENT = (70, 140, 255)
CORAL, VIOLET, MINT, GOLD = (255, 128, 94), (156, 128, 255), (62, 224, 172), (255, 206, 90)
WHITE, MUTED, DIM = (244, 248, 255), (150, 168, 196), (104, 124, 156)
WIN, LOSS = (62, 224, 140), (255, 96, 120)

_HERE = Path(__file__).resolve().parent
_HEAD_FONTS = [_HERE / "Agrandir-TextBold.otf", _HERE / "fonts" / "Agrandir-TextBold.otf"]
_BOLD_FONTS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSansNarrow-Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf", "/Library/Fonts/Arial Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]
_REG_FONTS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSansNarrow-Regular.ttf",
    "C:/Windows/Fonts/arial.ttf", "/Library/Fonts/Arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]
_font_cache = {}


def font(size, kind="bold"):
    """kind: 'head' (the bot's Agrandir), 'bold' or 'reg'. Always returns a real scalable font."""
    key = (int(size), kind)
    if key in _font_cache:
        return _font_cache[key]
    candidates = (list(_HEAD_FONTS) if kind == "head" else []) + (_REG_FONTS if kind == "reg" else _BOLD_FONTS)
    found = None
    for path in candidates:
        try:
            found = ImageFont.truetype(str(path), int(size))
            break
        except OSError:
            continue
    if found is None:
        try:
            found = ImageFont.load_default(int(size))
        except TypeError:
            found = ImageFont.load_default()
    _font_cache[key] = found
    return found


# --------------------------------------------------------------------------- colour helpers

def _mix(a, b, t):
    return tuple(int(round(a[i]*(1-t) + b[i]*t)) for i in range(3))


def _lighten(color, t):
    return _mix(color, (255, 255, 255), t)


def _darken(color, t):
    return _mix(color, (0, 0, 0), t)


def accent_from_logo(logo):
    """Dominant vivid colour of a logo (PIL image or None), tuned to read well on a dark page."""
    if logo is None:
        return DEFAULT_ACCENT
    try:
        small = logo.convert("RGBA").resize((48, 48), Image.Resampling.BILINEAR)
    except Exception:
        return DEFAULT_ACCENT
    bins = {}
    for r, g, b, a in small.getdata():
        if a < 140:
            continue
        h, s, v = colorsys.rgb_to_hsv(r/255, g/255, b/255)
        if s < 0.38 or v < 0.30:
            continue
        slot = int(h*24) % 24
        total = bins.setdefault(slot, [0.0, 0.0, 0.0, 0.0])
        weight = s * v
        total[0] += r*weight
        total[1] += g*weight
        total[2] += b*weight
        total[3] += weight
    if not bins:
        return DEFAULT_ACCENT
    best = max(bins.values(), key=lambda item: item[3])
    color = tuple(int(best[i]/best[3]) for i in range(3))
    h, s, v = colorsys.rgb_to_hsv(*(c/255 for c in color))
    s = max(s, 0.62)
    v = min(1.0, max(v, 0.88))                       # bright enough for a dark background
    return tuple(int(c*255) for c in colorsys.hsv_to_rgb(h, s, v))


# --------------------------------------------------------------------------- drawing helpers

def _text(d, xy, value, size, fill=WHITE, anchor="la", kind="bold"):
    d.text(xy, str(value), font=font(size, kind), fill=fill, anchor=anchor)


def _fit(d, value, max_width, start, minimum, kind="bold"):
    """Largest font size (<= start) that keeps `value` within max_width."""
    size = start
    while size > minimum and d.textlength(str(value), font=font(size, kind)) > max_width:
        size -= 2
    return size


def _clip(d, value, max_width, size, kind="bold"):
    """Shorten text with an ellipsis so it fits max_width."""
    value = str(value)
    f = font(size, kind)
    if d.textlength(value, font=f) <= max_width:
        return value
    while value and d.textlength(value + "…", font=f) > max_width:
        value = value[:-1]
    return value.rstrip() + "…"


def _pill(d, x, y, label, size, fill=None, fg=WHITE, outline=None, pad=16, h=None, kind="bold"):
    f = font(size, kind)
    width = int(d.textlength(label, font=f)) + pad*2
    height = h or size + 18
    d.rounded_rectangle((x, y, x+width, y+height), radius=height//2, fill=fill, outline=outline, width=2 if outline else 0)
    d.text((x+width/2, y+height/2), label, font=f, fill=fg, anchor="mm")
    return width


def _open(data):
    if not data:
        return None
    try:
        return Image.open(io.BytesIO(data)).convert("RGBA")
    except Exception:
        return None


def _round_image(source, size, ring=None, ring_w=5, initial="?", fill=(20, 30, 52)):
    """Circular avatar/logo with an anti-aliased ring. `source` is a PIL image or None."""
    ss = 3
    total = size + 2*ring_w if ring else size
    canvas = Image.new("RGBA", (total*ss, total*ss), (0, 0, 0, 0))
    cd = ImageDraw.Draw(canvas)
    if ring:
        cd.ellipse((0, 0, total*ss-1, total*ss-1), fill=ring + (255,))
    inner = size*ss
    off = (total*ss - inner)//2
    cd.ellipse((off, off, off+inner-1, off+inner-1), fill=fill + (255,))
    if source is not None:
        pic = ImageOps.fit(source, (inner, inner), method=Image.Resampling.LANCZOS)
        mask = Image.new("L", (inner, inner), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, inner-1, inner-1), fill=255)
        canvas.paste(pic, (off, off), ImageChops.multiply(mask, pic.getchannel("A")))
    else:
        cd.text((total*ss/2, total*ss/2), str(initial)[:1].upper(), font=font(int(size*0.46*ss), "head"),
                fill=WHITE, anchor="mm")
    return canvas.resize((total, total), Image.Resampling.LANCZOS)


def _paste(image, layer, xy):
    """Alpha-paste an RGBA layer onto the RGB page at xy (xy = top-left)."""
    image.paste(layer, (int(xy[0]), int(xy[1])), layer)


def _donut(size, pct, color, track, width):
    ss = 3
    layer = Image.new("RGBA", (size*ss, size*ss), (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer)
    box = (width*ss//2, width*ss//2, size*ss - width*ss//2, size*ss - width*ss//2)
    ld.arc(box, start=0, end=360, fill=track + (255,), width=width*ss)
    if pct > 0:
        ld.arc(box, start=-90, end=-90 + 360*min(1.0, pct), fill=color + (255,), width=width*ss)
    return layer.resize((size, size), Image.Resampling.LANCZOS)


@lru_cache(maxsize=24)
def _background(accent):
    """Dark page tinted with the team colour: gradient, glows, light beams, grid, vignette."""
    accent2 = _lighten(accent, 0.35)
    base = Image.composite(
        Image.new("RGB", (W, H), _mix((10, 20, 44), accent, 0.10)),
        Image.new("RGB", (W, H), (4, 8, 18)),
        Image.linear_gradient("L").resize((W, H)),
    ).convert("RGBA")

    def glow(cx, cy, rx, ry, color, alpha, blur):
        layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ImageDraw.Draw(layer).ellipse((cx-rx, cy-ry, cx+rx, cy+ry), fill=color + (alpha,))
        return layer.filter(ImageFilter.GaussianBlur(blur))
    base = Image.alpha_composite(base, glow(1380, 40, 600, 320, accent, 135, 130))
    base = Image.alpha_composite(base, glow(80, 880, 480, 280, accent2, 70, 120))
    base = Image.alpha_composite(base, glow(800, 460, 640, 220, (40, 90, 200), 26, 110))

    beams = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    bd = ImageDraw.Draw(beams)
    bd.polygon([(0, 0), (190, 0), (50, H), (0, H)], fill=accent + (40,))
    bd.polygon([(235, 0), (262, 0), (126, H), (98, H)], fill=accent2 + (26,))
    bd.polygon([(1380, 0), (1600, 0), (1600, 100), (1490, H), (1410, H)], fill=accent + (20,))
    base = Image.alpha_composite(base, beams.filter(ImageFilter.GaussianBlur(8)))

    grid = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(grid)
    for gx in range(0, W, 60):
        gd.line((gx, 0, gx, H), fill=(255, 255, 255, 6))
    for gy in range(0, H, 60):
        gd.line((0, gy, W, gy), fill=(255, 255, 255, 6))
    base = Image.alpha_composite(base, grid)

    vignette = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    vignette.putalpha(Image.radial_gradient("L").resize((W, H)).point(lambda v: int(max(0, v-110)*1.1)))
    return Image.alpha_composite(base, vignette).convert("RGB")


class _Page:
    """One 1600x900 page: queue the glass cards first, then draw content on top."""

    def __init__(self, theme):
        self.theme = theme
        self.image = _background(theme.get("bg", theme["accent"])).copy()
        self.cards = []
        self._marked = False

    def card(self, box, radius=22, tint=None):
        self.cards.append((box, radius, tint))

    def seal(self):
        accent = self.theme["accent"]
        layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ld = ImageDraw.Draw(layer)
        edge = _mix((44, 66, 104), accent, 0.30)
        for box, radius, tint in self.cards:
            fill = (9, 16, 32, 216) if tint is None else _mix((9, 16, 32), tint, 0.18) + (222,)
            ld.rounded_rectangle(box, radius=radius, fill=fill, outline=edge, width=1)
        self.image = Image.alpha_composite(self.image.convert("RGBA"), layer).convert("RGB")
        if not self._marked:   # watermark sits above the glass but below all content
            self._marked = True
            self._watermark()
        self.d = ImageDraw.Draw(self.image)
        self.cards = []
        return self.d


    def _watermark(self):
        """Team logo exactly centred, fitted without distortion, softly translucent."""
        logo = self.theme.get("logo")
        if logo is None:
            return
        size = 560
        src = ImageOps.contain(logo.convert("RGBA"), (size, size), method=Image.Resampling.LANCZOS)
        alpha = src.getchannel("A").point(lambda v: int(v*0.13))
        src.putalpha(alpha)
        layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        layer.paste(src, ((W-src.width)//2, (H-src.height)//2))
        self.image = Image.alpha_composite(self.image.convert("RGBA"), layer).convert("RGB")


def _luma(color):
    return 0.2126*color[0] + 0.7152*color[1] + 0.0722*color[2]


def _visible_accent(color):
    """Text/badge colour that stays readable on the dark canvas.

    Dark colours are lifted (black/grey become a clean silver) while the
    background keeps the real team colour.
    """
    color = tuple(max(0, min(255, int(channel))) for channel in color)
    h, saturation, value = colorsys.rgb_to_hsv(*(channel / 255 for channel in color))
    if _luma(color) < 150:
        if saturation < 0.18:
            grey = int(min(235, 205 + _luma(color) * 0.2))
            return (grey, grey + 3, min(255, grey + 12))
        saturation = min(max(saturation, 0.55), 0.85)
        value = max(value, 0.95)
        lifted = tuple(int(round(channel * 255)) for channel in colorsys.hsv_to_rgb(h, saturation, value))
        if _luma(lifted) < 150:
            lifted = _mix(lifted, (255, 255, 255), 0.35)
        return lifted
    return color


def _backdrop_color(color):
    """Real role colour for the background, tamed only if it would glare (white/pastel)."""
    color = tuple(max(0, min(255, int(channel))) for channel in color)
    if _luma(color) > 170:
        return _mix(color, (0, 0, 0), 0.45)
    return color


def _theme(data):
    logo_bytes = data.get("logo")
    print(
        f"[teaminfo][logo:6] Renderer received bytes={len(logo_bytes) if logo_bytes else 0}",
        flush=True,
    )
    logo = _open(logo_bytes)
    if logo is None:
        print("[teaminfo][logo:ERROR] Renderer could not open logo bytes; using fallback badge", flush=True)
    else:
        print(f"[teaminfo][logo:7] Renderer opened logo size={logo.size} mode={logo.mode}", flush=True)
    # Prefer the actual Discord team-role colour; use the logo only when no
    # usable team role colour is configured for this team. Brighten dark colours
    # so text, numbers and decorative accents remain readable on the dark canvas.
    role_color = data.get("team_color")
    raw_accent = tuple(int(channel) for channel in role_color) if role_color else accent_from_logo(logo)
    accent = _visible_accent(raw_accent)
    return {"accent": accent, "accent2": _lighten(accent, 0.35), "bg": _backdrop_color(raw_accent), "logo": logo}


def _tag(name):
    words = [w for w in str(name).replace("-", " ").split() if w]
    if len(words) >= 2:
        return "".join(w[0] for w in words[:4]).upper()
    return str(name)[:4].upper()


def _num(value, decimals=0):
    if value is None:
        return "—"
    if decimals:
        return f"{value:.{decimals}f}"
    return f"{int(round(value))}"


def _chrome(page, data, number, total, title):
    """Header (logo, team name, page chip) and footer shared by pages 2+."""
    theme, accent = page.theme, page.theme["accent"]
    page.card((50, 22, 1550, 106), 24)
    d = page.seal()
    badge = _round_image(theme["logo"], 58, ring=accent, ring_w=3, initial=data["name"][:1])
    _paste(page.image, badge, (72, 34))
    _text(d, (150, 29), f"TEAM PROFILE  •  {title}", 15, accent, kind="bold")
    _text(d, (150, 53), data["name"].upper(), _fit(d, data["name"].upper(), 760, 36, 22, "head"), WHITE, kind="head")
    label = f"PAGE {number} / {total}"
    chip_w = int(d.textlength(label, font=font(20))) + 32
    _pill(d, 1550 - 24 - chip_w, 40, label, 20, fill=accent, fg=(12, 18, 32))
    return d


def _footer(page, data, number, total):
    d = page.d
    _text(d, (60, 872), f"{data.get('guild', '').upper()}  •  TEAM PROFILE", 13, DIM)
    _text(d, (1540, 872), f"{_tag(data['name'])}  •  {number}/{total}", 13, DIM, "ra")


# --------------------------------------------------------------------------- page 1: roster

def _page_roster(data, theme, avatars, total_pages):
    page = _Page(theme)
    accent = theme["accent"]
    page.card((50, 126, 1550, 852), 26)
    d = _chrome(page, data, 1, total_pages, "ROSTER")
    # Leadership cards mirror the roster-first layout of the normal +teaminfo embed.
    leadership = [
        ((76, 148, 790, 252), data.get("captain"), "CAPTAIN"),
        ((810, 148, 1524, 252), data.get("vice"), "VICE-CAPTAIN"),
    ]
    for box, who, label in leadership:
        x1, y1, x2, y2 = box
        d.rounded_rectangle(box, radius=18, fill=(18, 29, 54), outline=(40, 58, 92), width=2)
        d.rounded_rectangle((x1, y1+14, x1+8, y2-14), radius=4, fill=accent)
        _text(d, (x1+24, y1+18), label, 13, accent)
        if who:
            badge = _round_image(avatars.get(("p", who["uid"])), 58, ring=accent, ring_w=3, initial=who["name"][:1])
            _paste(page.image, badge, (x1+24, y1+39))
            _text(d, (x1+98, y1+70), _clip(d, who["name"], x2-x1-125, 25, "head"), 25, WHITE, "lm", "head")
        else:
            _text(d, (x1+24, y1+72), "NOT ASSIGNED", 20, DIM, "lm")
    players = list(data.get("players", []))
    _text(d, (82, 278), f"PLAYERS  •  {len(players)}", 17, accent, kind="bold")
    left_x, right_x = 82, 824
    row_h = 40
    start_y = 310
    for index, who in enumerate(players):
        column = 0 if index < 13 else 1
        row = index if column == 0 else index - 13
        x = left_x if column == 0 else right_x
        y = start_y + row * row_h
        d.rounded_rectangle((x, y, x+694, y+34), radius=10,
                            fill=(18, 29, 54) if row % 2 == 0 else (14, 24, 46))
        _text(d, (x+14, y+17), f"{index+1:02d}", 14, accent, "lm")
        badge = _round_image(avatars.get(("p", who["uid"])), 28, ring=None, initial=who["name"][:1])
        _paste(page.image, badge, (x+52, y+3))
        role = "  •  CAPTAIN" if who.get("role") == "C" else ("  •  VICE-CAPTAIN" if who.get("role") == "VC" else "")
        _text(d, (x+92, y+17), _clip(d, who["name"] + role, 560, 17), 17,
              accent if role else WHITE, "lm")
    if not players:
        _text(d, (800, 520), "NO PLAYERS IN THIS TEAM YET", 26, DIM, "mm")
    _footer(page, data, 1, total_pages)
    return page.image


# --------------------------------------------------------------------------- overview

def _player_card(page, box, who, label, accent, avatar_img, stripe_color=None):
    x1, y1, x2, y2 = box
    d = page.d
    if who is None:
        _text(d, ((x1+x2)/2, (y1+y2)/2 - 12), label, 22, DIM, "mm")
        _text(d, ((x1+x2)/2, (y1+y2)/2 + 18), "NOT ASSIGNED", 18, DIM, "mm")
        return
    d.rounded_rectangle((x1, y1+20, x1+10, y2-20), radius=5, fill=stripe_color or accent)
    badge = _round_image(avatar_img, 168, ring=accent, ring_w=6, initial=who["name"][:1])
    _paste(page.image, badge, (x1+26, y1+(y2-y1-badge.size[1])//2))
    tx = x1 + 242
    _pill(d, tx, y1+30, label, 15, fill=accent, fg=(12, 18, 32), pad=14, h=30)
    name_room = x2 - tx - 230                      # keep clear of the big fantasy number
    shown = _clip(d, who["name"], name_room, _fit(d, who["name"], name_room, 44, 26, "head"), "head")
    _text(d, (tx, y1+72), shown, _fit(d, shown, name_room, 44, 26, "head"), WHITE, kind="head")
    _text(d, (tx, y1+130), "@" + who.get("handle", who["name"]), 19, MUTED, kind="reg")
    _text(d, (x2-34, y1+34), "FANTASY PTS", 14, MUTED, "ra")
    _text(d, (x2-34, y1+52), _num(who["fantasy"]), 66, accent, "ra", "head")
    stats = [("MATCHES", who["matches"]), ("RUNS", who["runs"]), ("WICKETS", who["wickets"]), ("CATCHES", who["catches"])]
    col = (x2 - tx - 20) / 4
    for i, (name, value) in enumerate(stats):
        _text(d, (tx + i*col, y1+170), name, 13, DIM)
        _text(d, (tx + i*col, y1+188), value, 32, WHITE, kind="head")


def _page_overview(data, theme, avatars, number, total_pages):
    page = _Page(theme)
    accent, accent2 = theme["accent"], theme["accent2"]
    standing = data.get("standing")
    page.card((50, 26, 1550, 336), 30)
    page.card((50, 356, 790, 596), 26)
    page.card((810, 356, 1550, 596), 26)
    leader_boxes = [(50 + i*510, 616, 530 + i*510, 852) for i in range(3)]
    for box in leader_boxes:
        page.card(box, 24)
    d = page.seal()
    d = page.d

    # hero ----------------------------------------------------------------
    d.rounded_rectangle((50, 26, 60, 336), radius=5, fill=accent)
    logo_badge = _round_image(theme["logo"], 226, ring=accent, ring_w=7, initial=data["name"][:1])
    _paste(page.image, logo_badge, (92, 66))
    _text(d, (362, 50), "TEAM PROFILE", 17, accent)
    name = data["name"].upper()
    _text(d, (360, 76), name, _fit(d, name, 600, 84, 40, "head"), WHITE, kind="head")
    chips = []
    if standing:
        chips.append((str(standing["group"]).upper(), None, WHITE, accent))
        chips.append((f"RANK #{standing['rank']} OF {standing['of']}", accent, (12, 18, 32), None))
    chips.append((f"ROSTER {data['roster_size']}/{data['roster_max']}", None, WHITE, MUTED))
    cx = 362
    for label, fill, fg, outline in chips:
        cx += _pill(d, cx, 196, label, 19, fill=fill, fg=fg, outline=outline) + 12
    played = (standing["won"] + standing["lost"]) if standing else 0
    won = standing["won"] if standing else 0
    lost = standing["lost"] if standing else 0
    points = standing["points"] if standing else 0
    win_rate = (won / played) if played else 0.0
    ring = _donut(196, win_rate, accent, (30, 44, 72), 16)
    _paste(page.image, ring, (1010, 52))
    _text(d, (1108, 138), f"{win_rate*100:.0f}%", 44, WHITE, "mm", "head")
    _text(d, (1108, 176), "WIN RATE", 13, MUTED, "mm")
    grid = [("PLAYED", played, WHITE), ("WON", won, WIN), ("LOST", lost, LOSS), ("POINTS", points, accent2)]
    for i, (label, value, color) in enumerate(grid):
        gx = 1250 + (i % 2) * 145
        gy = 64 + (i // 2) * 98
        _text(d, (gx, gy), label, 14, MUTED)
        _text(d, (gx, gy+20), value, 54, color, kind="head")
    _text(d, (1010, 274), "FORM", 14, MUTED)
    history = (standing or {}).get("history", [])[-5:]
    if history:
        for i, result in enumerate(history):
            cx0 = 1088 + i*44
            win = result == "W"
            d.ellipse((cx0, 262, cx0+34, 296), fill=WIN if win else LOSS)
            _text(d, (cx0+17, 279), result, 17, (10, 16, 28), "mm")
    else:
        _text(d, (1088, 279), "NO RESULTS YET", 15, DIM, "lm")

    # captain / vice-captain ------------------------------------------------
    _player_card(page, (50, 356, 790, 596), data.get("captain"), "CAPTAIN", accent, avatars.get(("p", (data.get("captain") or {}).get("uid"))), stripe_color=accent)
    _player_card(page, (810, 356, 1550, 596), data.get("vice"), "VICE-CAPTAIN", accent2, avatars.get(("p", (data.get("vice") or {}).get("uid"))), stripe_color=accent)

    # team leaders ------------------------------------------------------------
    leaders = data.get("leaders", [])
    for box, leader in zip(leader_boxes, leaders):
        x1, y1, x2, y2 = box
        color = leader["color"]
        d.rounded_rectangle((x1, y1+18, x1+10, y2-18), radius=5, fill=color)
        d.rounded_rectangle((x1+26, y1+22, x1+38, y1+34), radius=4, fill=color)
        _text(d, (x1+50, y1+17), leader["label"], 16, color)
        if leader.get("player") is None:
            _text(d, ((x1+x2)/2, y1+130), "NO DATA YET", 22, DIM, "mm")
            continue
        who = leader["player"]
        badge = _round_image(avatars.get(("p", who["uid"])), 112, ring=color, ring_w=4, initial=who["name"][:1])
        _paste(page.image, badge, (x1+26, y1+62))
        tx = x1 + 168
        _text(d, (tx, y1+66), _clip(d, who["name"], x2-tx-20, 28, "head"), 28, WHITE, kind="head")
        _text(d, (tx, y1+108), leader["value"], 64, color, kind="head")
        width = d.textlength(str(leader["value"]), font=font(64, "head"))
        _text(d, (tx+width+10, y1+142), leader["unit"], 18, MUTED)
        _text(d, (x1+26, y1+196), leader["sub"], 17, MUTED, kind="reg")
    _footer(page, data, number, total_pages)
    return page.image


# --------------------------------------------------------------------------- team stats

def _page_stats(data, theme, avatars, number, total_pages):
    page = _Page(theme)
    accent = theme["accent"]
    columns = [(50 + i*510, 132, 530 + i*510, 852) for i in range(3)]
    for box in columns:
        page.card(box, 26)
    _chrome(page, data, number, total_pages, "TEAM STATS")      # seals the header and the three columns together
    d = page.d
    sections = data["sections"]
    for box, section in zip(columns, sections):
        x1, y1, x2, y2 = box
        color = section["color"]
        d.rounded_rectangle((x1+28, y1+26, x1+78, y1+32), radius=3, fill=color)
        _text(d, (x1+28, y1+44), section["title"], 34, color, kind="head")
        tile_w = (x2 - x1 - 56) / 3
        for i, (label, value) in enumerate(section["tiles"]):
            tx = x1 + 28 + i*tile_w
            _text(d, (tx, y1+104), label, 13, MUTED)
            _text(d, (tx, y1+122), value, 40, WHITE, kind="head")
        d.line((x1+28, y1+190, x2-28, y1+190), fill=(40, 58, 92), width=1)
        _text(d, (x1+28, y1+204), section["list_title"], 14, DIM)
        top = section["top"]
        peak = max([item["value"] for item in top] + [1])
        if not top:
            _text(d, ((x1+x2)/2, y1+420), "NO RECORDS YET", 20, DIM, "mm")
        for i, item in enumerate(top[:5]):
            ry = y1 + 240 + i*92
            who = item["player"]
            badge = _round_image(avatars.get(("p", who["uid"])), 54, ring=color if i == 0 else None, ring_w=3, initial=who["name"][:1])
            _paste(page.image, badge, (x1+28, ry+2))
            _text(d, (x1+100, ry), _clip(d, who["name"], 250, 22), 22, WHITE)
            _text(d, (x2-28, ry-2), item["label"], 28, color if i == 0 else WHITE, "ra", "head")
            bar_x, bar_w = x1+100, x2-28-(x1+100)
            d.rounded_rectangle((bar_x, ry+38, bar_x+bar_w, ry+48), radius=5, fill=(30, 44, 72))
            fill_w = max(10, int(bar_w * item["value"] / peak)) if item["value"] > 0 else 0
            if fill_w:
                d.rounded_rectangle((bar_x, ry+38, bar_x+fill_w, ry+48), radius=5, fill=color if i == 0 else _darken(color, 0.25))
    _footer(page, data, number, total_pages)
    return page.image


# --------------------------------------------------------------------------- squad pages

SQUAD_COLUMNS = [
    ("MAT", 770, CORAL), ("RUNS", 855, CORAL), ("AVG", 940, CORAL), ("SR", 1025, CORAL), ("HS", 1105, CORAL),
    ("WKT", 1190, VIOLET), ("ECON", 1275, VIOLET), ("BEST", 1365, VIOLET),
    ("CT", 1420, MINT), ("FANTASY", 1522, GOLD),
]


def _page_squad(data, theme, avatars, number, total_pages, players, first_rank):
    page = _Page(theme)
    accent = theme["accent"]
    page.card((50, 126, 1550, 852), 26)
    _chrome(page, data, number, total_pages, "SQUAD")      # seals the header and the table card together
    d = page.d
    d.rounded_rectangle((50, 126, 1550, 190), radius=26, fill=(14, 24, 46))
    d.rectangle((50, 160, 1550, 190), fill=(14, 24, 46))
    _text(d, (86, 158), "#", 15, MUTED, "lm")
    _text(d, (170, 158), "PLAYER", 15, MUTED, "lm")
    for label, x, color in SQUAD_COLUMNS:
        _text(d, (x, 158), label, 15, color, "rm")
    row_h = 63
    for i, who in enumerate(players):
        y = 198 + i*row_h
        if i % 2 == 0:
            d.rounded_rectangle((62, y, 1538, y+row_h-6), radius=14, fill=(18, 29, 54))
        top3 = first_rank + i <= 3
        if top3:
            d.rounded_rectangle((62, y, 70, y+row_h-6), radius=4, fill=accent)
        rank_fill = accent if top3 else (34, 50, 82)
        d.ellipse((74, y+9, 104, y+39), fill=rank_fill)
        _text(d, (89, y+24), first_rank + i, 15, (12, 18, 32) if top3 else WHITE, "mm")
        badge = _round_image(avatars.get(("p", who["uid"])), 40, ring=None, initial=who["name"][:1])
        _paste(page.image, badge, (118, y+3))
        name_x = 170
        name_w = d.textlength(_clip(d, who["name"], 330, 22), font=font(22))
        _text(d, (name_x, y+24), _clip(d, who["name"], 330, 22), 22, WHITE, "lm")
        if who.get("role"):
            _pill(d, name_x + name_w + 12, y+10, who["role"], 13, fill=accent, fg=(12, 18, 32), pad=10, h=26)
        played = who["matches"] > 0
        batted, bowled = who["batted"], who["bowled"]
        cells = [
            who["matches"] if played else "—",
            who["runs"] if batted else "—",
            _num(who["bat_avg"], 1) if batted else "—",
            _num(who["strike_rate"], 1) if batted else "—",
            who["hs"] if batted else "—",
            who["wickets"] if bowled else "—",
            _num(who["economy"], 1) if bowled else "—",
            who["best"] if bowled else "—",
            who["catches"] if who["catches"] or who["dropped"] else "—",
            _num(who["fantasy"], 1) if played else "—",
        ]
        for (label, x, color), value in zip(SQUAD_COLUMNS, cells):
            if value == "—":
                shade = DIM
            elif label == "FANTASY":
                shade = GOLD
            elif label in ("RUNS", "WKT"):
                shade = color
            else:
                shade = WHITE
            _text(d, (x, y+24), value, 22 if label in ("RUNS", "WKT", "FANTASY") else 20, shade, "rm")
    if not players:
        _text(d, (800, 480), "NO PLAYERS IN THIS TEAM YET", 28, DIM, "mm")
    _footer(page, data, number, total_pages)
    return page.image


# --------------------------------------------------------------------------- public entry point

def render_team_pages(data):
    """data -> roster first, then overview, team stats and squad pages."""
    theme = _theme(data)
    players = list(data.get("players", []))
    ranked = sorted(players, key=lambda p: (-p["fantasy"], p["name"].casefold()))
    chunks = [ranked[i:i+ROWS_PER_SQUAD_PAGE] for i in range(0, len(ranked), ROWS_PER_SQUAD_PAGE)] or [[]]
    total_pages = 3 + len(chunks)

    avatars = {}
    for who in players:
        img = _open(who.get("avatar_bytes"))
        avatars[("p", who["uid"])] = img
    for key in ("captain", "vice"):
        who = data.get(key)
        if who is not None and ("p", who["uid"]) not in avatars:
            avatars[("p", who["uid"])] = _open(who.get("avatar_bytes"))
    for section in data.get("sections", []):
        for item in section["top"]:
            who = item["player"]
            avatars.setdefault(("p", who["uid"]), _open(who.get("avatar_bytes")))
    for leader in data.get("leaders", []):
        who = leader.get("player")
        if who is not None:
            avatars.setdefault(("p", who["uid"]), _open(who.get("avatar_bytes")))

    images = [_page_roster(data, theme, avatars, total_pages)]
    images.append(_page_overview(data, theme, avatars, 2, total_pages))
    images.append(_page_stats(data, theme, avatars, 3, total_pages))
    rank = 1
    for index, chunk in enumerate(chunks):
        images.append(_page_squad(data, theme, avatars, 4 + index, total_pages, chunk, rank))
        rank += len(chunk)
    pages = []
    for image in images:
        out = io.BytesIO()
        image.save(out, format="PNG", compress_level=3)
        pages.append(out.getvalue())
    return pages
