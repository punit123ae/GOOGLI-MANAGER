"""Cards for the TEST stock exchange (new design): market page, one stock, portfolio page.

Self-contained (Pillow only) so it never touches the main stock cards in stocks_image.py.
Everything is drawn at 2x and scaled down once, which keeps edges smooth. Sizes match the reference cards.

market     {"page", "pages", "rows": [{"rank", "name", "avatar", "price", "change", "left", "supply"}]}
stock      {"name", "avatar", "games", "price", "change", "left", "supply", "max_each", "owned",
            "tiles": [(label, value, tag)], "breakdown": [(label, amount)],
            "history": [(game_no, price, delta)]}
portfolio  {"user": {"name", "avatar"}, "players", "shares", "worth", "paid", "pl", "page", "pages", "fee_pct",
            "rows": [{"name", "avatar", "shares", "max", "price", "value", "pl"}]}
avatar = image bytes, a PIL image, or None.
"""
import io
import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

try:                                   # use the bot's own fonts when they are there
    from .teaminfo_image import font as _bot_font
except Exception:                      # pragma: no cover - fallback for running this file on its own
    _bot_font = None

S = 2                                  # supersampling factor

NAVY = (7, 13, 33)
PANEL = (13, 22, 46)
EDGE = (42, 56, 92)
WHITE = (242, 246, 255)
GRAY = (136, 150, 182)
DIM = (96, 110, 146)
BLUE = (98, 160, 255)
GOLD = (245, 186, 62)
GREEN = (62, 208, 124)
RED = (240, 82, 92)
GREEN_FILL = (20, 62, 48)
RED_FILL = (72, 26, 36)

_HERE = Path(__file__).resolve().parent
_FONT_FILES = ("DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
               str(_HERE / "Agrandir-TextBold.otf"), "LiberationSans-Bold.ttf")
_font_cache = {}


def _font(size):
    key = int(size * S)
    if key in _font_cache:
        return _font_cache[key]
    f = None
    for name in _FONT_FILES:
        try:
            f = ImageFont.truetype(name, key)
            break
        except Exception:
            continue
    if f is None and _bot_font is not None:
        try:
            f = _bot_font(key, "head")
        except Exception:
            f = None
    if f is None:
        f = ImageFont.load_default()
    _font_cache[key] = f
    return f


def mix(a, b, t):
    return tuple(int(a[i] * (1 - t) + b[i] * t) for i in range(3))


def money(v):
    return f"{int(round(v)):,}"


def signed(v):
    v = int(round(v))
    return f"{'+' if v > 0 else '−' if v < 0 else ''}{abs(v):,}"


def pct(v):
    return f"{abs(v):.1f}%"


# --------------------------------------------------------------------------- background
_bg_cache = {}


def _background(w, h):
    """Dark navy with a blurred candlestick chart behind everything (built once per size, then copied)."""
    key = (w, h)
    if key in _bg_cache:
        return _bg_cache[key].copy()
    W, H = w * S, h * S
    img = Image.new("RGB", (W, H), NAVY)
    d = ImageDraw.Draw(img)
    for y in range(H):                                   # soft vertical gradient
        t = y / H
        d.line((0, y, W, y), fill=mix((8, 16, 42), (5, 9, 24), t))
    chart = Image.new("RGB", (W, H), (0, 0, 0))
    cd = ImageDraw.Draw(chart)
    rnd = random.Random(11)
    x, level = int(-0.02 * W), 0.55
    colors = [(30, 170, 150), (30, 170, 150), (230, 120, 40), (60, 120, 230), (30, 170, 150)]
    while x < W:
        move = rnd.uniform(-0.09, 0.08)
        top = level + min(0, move) - rnd.uniform(0.01, 0.05)
        bot = level + max(0, move) + rnd.uniform(0.01, 0.05)
        col = rnd.choice(colors)
        bw = int(W * 0.022)
        cx = x + bw // 2
        cd.line((cx, int(top * H), cx, int(bot * H)), fill=col, width=max(2, bw // 8))
        a, b = sorted((level, level + move))
        cd.rectangle((x, int(a * H), x + bw, int(b * H) + 4), fill=col)
        level = min(0.85, max(0.25, level + move * 0.8))
        x += int(bw * 1.9)
    chart = chart.filter(ImageFilter.GaussianBlur(14 * S))
    img = Image.blend(img, Image.composite(chart, img, chart.convert("L").point(lambda p: min(255, p * 2))), 0.75)
    img = Image.blend(img, Image.new("RGB", (W, H), NAVY), 0.25)
    _bg_cache.clear()
    _bg_cache[key] = img
    return img.copy()


# --------------------------------------------------------------------------- canvas
class Canvas:
    def __init__(self, w, h):
        self.w, self.h = w, h
        self.img = _background(w, h).convert("RGBA")
        self.glass = Image.new("RGBA", self.img.size, (0, 0, 0, 0))
        self.gd = ImageDraw.Draw(self.glass)
        self.d = None

    def panel(self, box, r=18, fill=PANEL, alpha=205, outline=EDGE):
        x1, y1, x2, y2 = [v * S for v in box]
        self.gd.rounded_rectangle((x1, y1, x2, y2), radius=r * S, fill=fill + (alpha,), outline=outline + (255,), width=S)

    def stripe(self, box, r=10):
        """A slightly lighter row band; must be called before seal()."""
        x1, y1, x2, y2 = [v * S for v in box]
        self.gd.rounded_rectangle((x1, y1, x2, y2), radius=r * S, fill=mix(PANEL, (255, 255, 255), 0.07) + (215,))

    def seal(self):
        """Merge the glass panels into the picture; everything drawn after this sits on top of them."""
        self.img = Image.alpha_composite(self.img, self.glass)
        self.d = ImageDraw.Draw(self.img)
        return self.d

    # all coordinates below are in 1x units
    def rrect(self, box, r, fill=None, outline=None, width=1):
        x1, y1, x2, y2 = [v * S for v in box]
        self.d.rounded_rectangle((x1, y1, x2, y2), radius=r * S, fill=fill, outline=outline, width=width * S)

    def text(self, xy, s, size, fill=WHITE, anchor="la"):
        self.d.text((xy[0] * S, xy[1] * S), s, font=_font(size), fill=fill, anchor=anchor)

    def width(self, s, size):
        return self.d.textlength(s, font=_font(size)) / S

    def fit(self, s, max_w, size, min_size=10):
        while size > min_size and self.width(s, size) > max_w:
            size -= 1
        if self.width(s, size) > max_w:                       # still too long: cut with an ellipsis
            while len(s) > 1 and self.width(s + "…", size) > max_w:
                s = s[:-1]
            s += "…"
        return s, size

    def spaced(self, xy, s, size, fill, gap=3):
        """Small caps label with wide letter spacing."""
        x = xy[0]
        for ch in s:
            self.text((x, xy[1]), ch, size, fill, "la")
            x += self.width(ch, size) + gap

    def coin(self, cx, cy, r):
        self.d.ellipse(((cx - r) * S, (cy - r) * S, (cx + r) * S, (cy + r) * S), fill=(255, 196, 70), outline=(190, 128, 20), width=S)
        k = r * 0.52
        self.d.ellipse(((cx - k) * S, (cy - k) * S, (cx + k) * S, (cy + k) * S), outline=(200, 138, 24), width=max(1, S * r // 8))

    def tri(self, x, y, size, up, color):
        """Filled triangle; (x, y) is its centre."""
        s = size * S
        cx, cy = x * S, y * S
        if up:
            pts = [(cx - s, cy + s * 0.75), (cx + s, cy + s * 0.75), (cx, cy - s * 0.85)]
        else:
            pts = [(cx - s, cy - s * 0.75), (cx + s, cy - s * 0.75), (cx, cy + s * 0.85)]
        self.d.polygon(pts, fill=color)

    def avatar(self, avatar, cx, cy, size, ring=GOLD, ring_w=2, initial="?"):
        px = size * S
        big = Image.new("RGBA", (px * 2, px * 2), (0, 0, 0, 0))     # 2x again: smooth circle edges
        bd = ImageDraw.Draw(big)
        bd.ellipse((0, 0, px * 2 - 1, px * 2 - 1), fill=ring + (255,))
        inner = px * 2 - ring_w * S * 4
        face = _open(avatar)
        if face is None:
            face = Image.new("RGBA", (inner, inner), (30, 44, 84, 255))
            fd = ImageDraw.Draw(face)
            fd.text((inner // 2, inner // 2), (initial or "?")[:1].upper(), font=_font(size), fill=WHITE, anchor="mm")
        else:
            side = min(face.size)
            face = face.crop(((face.width - side) // 2, (face.height - side) // 2,
                              (face.width + side) // 2, (face.height + side) // 2)).resize((inner, inner), Image.LANCZOS)
        mask = Image.new("L", (inner, inner), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, inner - 1, inner - 1), fill=255)
        off = (px * 2 - inner) // 2
        big.paste(face, (off, off), mask)
        small = big.resize((px, px), Image.LANCZOS)
        self.img.alpha_composite(small, (int(cx * S - px / 2), int(cy * S - px / 2)))
        self.d = ImageDraw.Draw(self.img)

    def pill(self, box, label, color, fill, up=None, size=12):
        x1, y1, x2, y2 = box
        self.rrect(box, (y2 - y1) / 2, fill=fill, outline=color, width=1)
        if up is not None:
            self.tri(x1 + 14, (y1 + y2) / 2, 4.5, up, color)
            self.text((x2 - 12, (y1 + y2) / 2), label, size, color, "rm")
        else:
            self.text(((x1 + x2) / 2, (y1 + y2) / 2), label, size, color, "mm")

    def move_pill(self, box, change, size=12):
        up = change >= 0
        col = GREEN if up else RED
        self.pill(box, pct(change), col, GREEN_FILL if up else RED_FILL, up=up, size=size)

    def done(self):
        out = self.img.convert("RGB").resize((self.w, self.h), Image.LANCZOS)
        buf = io.BytesIO()
        out.save(buf, "PNG", optimize=False, compress_level=3)
        return buf.getvalue()


def _open(src):
    if src is None:
        return None
    try:
        if isinstance(src, Image.Image):
            return src.convert("RGBA")
        if isinstance(src, (bytes, bytearray)):
            return Image.open(io.BytesIO(src)).convert("RGBA")
        if hasattr(src, "read"):
            return Image.open(src).convert("RGBA")
    except Exception:
        return None
    return None


def _name(s, limit=24):
    return (s or "?").strip()[:limit]


# =========================================================================== market
def render_market(data):
    c = Canvas(1000, 700)
    c.panel((24, 60, 976, 676))
    for i in range(0, min(10, len(data["rows"])), 2):
        c.stripe((36, 128 + i * 54 - 24, 964, 128 + i * 54 + 24), 12)
    c.seal()
    c.spaced((30, 26), "PLAYER STOCK MARKET", 12, BLUE, 3)
    c.text((970, 34), f"Page {data['page']} / {data['pages']}", 12, GRAY, "rm")
    for label, x, anchor in (("#", 60, "la"), ("PLAYER", 110, "la"), ("PRICE", 560, "ra"), ("MOVE", 643, "ma"),
                             ("SHARES LEFT", 930, "ra")):
        c.text((x, 82), label, 11, GRAY, anchor)
    rows = data["rows"]
    if not rows:
        c.text((500, 380), "NO PLAYERS LISTED YET", 20, DIM, "mm")
    for i, row in enumerate(rows[:10]):
        cy = 128 + i * 54
        rank_col = GOLD if row["rank"] <= 3 else GRAY
        c.text((64, cy), str(row["rank"]), 15, rank_col, "lm")
        c.avatar(row.get("avatar"), 106, cy, 40, GOLD, 2, row["name"][:1])
        name, size = c.fit(_name(row["name"], 30), 290, 17, 11)
        c.text((134, cy), name, size, WHITE, "lm")
        c.coin(470, cy, 10)
        c.text((560, cy), money(row["price"]), 19, WHITE, "rm")
        c.move_pill((594, cy - 13, 691, cy + 13), row["change"], 12)
        supply = max(1, row["supply"])
        frac = max(0.0, min(1.0, row["left"] / supply))
        c.rrect((760, cy - 5, 900, cy + 5), 5, fill=(48, 58, 88))
        if frac > 0:
            c.rrect((760, cy - 5, 760 + max(10, 140 * frac), cy + 5), 5, fill=(88, 156, 255))
        c.text((930, cy), str(row["left"]), 14, GOLD if row["left"] == 0 else WHITE, "rm")
    return c.done()


# =========================================================================== stock
def render_stock(data):
    c = Canvas(1000, 560)
    c.panel((24, 54, 344, 536))
    for x in (356, 512, 668, 824):
        c.panel((x, 54, x + 140, 160), 16)
    c.panel((356, 176, 976, 358), 16)
    c.panel((356, 372, 976, 536), 16)
    c.seal()
    c.spaced((30, 22), "STOCK", 12, BLUE, 3)

    # left card
    c.avatar(data.get("avatar"), 184, 140, 132, GOLD, 3, data["name"][:1])
    name, size = c.fit(data["name"].upper(), 290, 28, 14)
    c.text((184, 246), name, size, WHITE, "mm")
    c.text((184, 276), f"{data['games']} games played", 12, GRAY, "mm")
    c.rrect((58, 302, 310, 372), 14, fill=(40, 34, 28), outline=GOLD, width=1)
    c.text((184, 322), "PRICE", 11, GOLD, "mm")
    price = money(data["price"])
    pw = c.width(price, 30) + 36
    px = 184 - pw / 2
    c.coin(px + 12, 349, 12)
    c.text((px + 36, 349), price, 30, WHITE, "lm")
    c.text((48, 405), "Last move", 13, GRAY, "lm")
    c.move_pill((194, 391, 306, 419), data["change"], 12)
    c.text((48, 449), "Shares", 13, GRAY, "lm")
    supply = max(1, data["supply"])
    frac = max(0.0, min(1.0, data["left"] / supply))
    c.rrect((130, 444, 306, 454), 5, fill=(48, 58, 88))
    if frac > 0:
        c.rrect((130, 444, 130 + max(10, 176 * frac), 454), 5, fill=(88, 156, 255))
    c.text((218, 476), f"{data['left']} left of {data['supply']} · max {data['max_each']} each", 11, GRAY, "mm")
    c.d.line((48 * S, 494 * S, 320 * S, 494 * S), fill=EDGE, width=S)
    c.text((48, 516), "You own", 13, GRAY, "lm")
    owned = data.get("owned", 0)
    c.text((320, 516), f"{owned} share{'s' if owned != 1 else ''}" if owned else "none", 13,
           WHITE if owned else GRAY, "rm")

    # four stat tiles
    for (label, value, tag), x in zip(data["tiles"], (356, 512, 668, 824)):
        c.text((x + 18, 76), label, 10, GRAY, "lm")
        c.text((x + 18, 108), value, 28, WHITE, "lm")
        c.text((x + 18, 142), tag, 11, GOLD, "lm")

    # what moves the price
    title = "WHAT MOVES THE PRICE"
    if data.get("base"):
        title += f"  ·  STARTS AT {money(data['base'])}"
    c.text((376, 198), title, 10, GRAY, "lm")
    for i, (label, amount) in enumerate(data["breakdown"][:6]):
        y = 224 + i * 21.5
        c.text((376, y), label, 14, WHITE, "lm")
        c.text((956, y), signed(amount), 14, GREEN if amount > 0 else RED if amount < 0 else GRAY, "rm")

    # last games chart
    hist = data["history"][-5:]
    c.text((376, 392), f"PRICE · LAST {len(hist) if len(hist) > 1 else 5} GAMES", 10, GRAY, "lm")
    if hist:
        left, right, top, bottom = 440, 940, 440, 480
        prices = [p for _g, p, _d in hist]
        lo, hi = min(prices), max(prices)
        span = max(hi - lo, 1)
        for gy in (top, (top + bottom) / 2, bottom):
            c.d.line((376 * S, gy * S, 960 * S, gy * S), fill=(30, 42, 74), width=S)
        pts = []
        for i, (_g, p, _d) in enumerate(hist):
            x = left + (right - left) * (i / (len(hist) - 1) if len(hist) > 1 else 0.5)
            y = bottom - (bottom - top) * (p - lo) / span if hi != lo else (top + bottom) / 2
            pts.append((x, y))
        if len(pts) > 1:
            c.d.line([(x * S, y * S) for x, y in pts], fill=(236, 168, 52), width=2 * S, joint="curve")
        for (x, y), (g, p, dlt) in zip(pts, hist):
            col = GREEN if dlt > 0 else RED if dlt < 0 else GRAY
            c.d.ellipse(((x - 5) * S, (y - 5) * S, (x + 5) * S, (y + 5) * S), fill=col, outline=(20, 30, 60), width=S)
            c.text((x, y - 16), money(p), 11, WHITE, "mm")
            c.text((x, 492), f"Game {g}", 10, GRAY, "mm")
            if dlt:
                c.tri(x - c.width(money(abs(dlt)), 11) / 2 - 9, 513, 4.5, dlt > 0, col)
            c.text((x + (0 if not dlt else 6), 513), money(abs(dlt)), 11, col if dlt else GRAY, "mm")
    else:
        c.text((666, 460), "NO GAMES YET", 16, DIM, "mm")
    return c.done()


# =========================================================================== portfolio
def render_portfolio(data):
    c = Canvas(1000, 560)
    for x1, x2 in ((470, 630), (642, 802), (814, 974)):
        c.panel((x1, 56, x2, 144), 16)
    c.panel((24, 164, 976, 536))
    for i in range(0, min(8, len(data["rows"])), 2):
        c.stripe((36, 232 + i * 36.5 - 18, 964, 232 + i * 36.5 + 18), 10)
    c.seal()
    c.spaced((30, 24), "PORTFOLIO", 12, BLUE, 3)
    user = data["user"]
    c.avatar(user.get("avatar"), 80, 100, 68, GOLD, 3, user["name"][:1])
    uname, size = c.fit(_name(user["name"], 28), 300, 26, 14)
    c.text((130, 88), uname, size, WHITE, "lm")
    c.text((130, 118), f"{data['players']} players · {data['shares']} shares", 13, GRAY, "lm")
    for (x1, x2), label in zip(((470, 630), (642, 802), (814, 974)), ("WORTH NOW", "PAID", "PROFIT / LOSS")):
        c.text((x1 + 14, 76), label, 10, GRAY, "lm")
    c.coin(490, 112, 9)
    c.text((506, 112), money(data["worth"]), 17, WHITE, "lm")
    c.coin(662, 112, 9)
    c.text((678, 112), money(data["paid"]), 17, WHITE, "lm")
    pl = data["pl"]
    plc = GREEN if pl > 0 else RED if pl < 0 else GRAY
    c.text((828, 112), signed(pl), 21, plc, "lm")

    for label, x, anchor in (("PLAYER", 60, "la"), ("SHARES", 430, "ra"), ("PRICE", 560, "ra"), ("VALUE", 730, "ra"),
                             ("PROFIT / LOSS", 930, "ra")):
        c.text((x, 184), label, 11, GRAY, anchor)
    rows = data["rows"]
    if not rows:
        c.text((500, 330), "NO SHARES YET", 24, DIM, "mm")
        c.text((500, 366), "Buy one with +testbuy <player> <amount>", 13, GRAY, "mm")
    for i, row in enumerate(rows[:8]):
        cy = 232 + i * 36.5
        c.avatar(row.get("avatar"), 77, cy, 28, GOLD, 2, row["name"][:1])
        name, size = c.fit(_name(row["name"], 30), 250, 16, 10)
        c.text((108, cy), name, size, WHITE, "lm")
        c.text((430, cy), f"{row['shares']} / {row['max']}", 15, WHITE, "rm")
        c.text((560, cy), money(row["price"]), 15, GRAY, "rm")
        c.text((730, cy), money(row["value"]), 16, WHITE, "rm")
        up = row["pl"] >= 0
        col = GREEN if up else RED
        c.pill((770, cy - 14, 931, cy + 14), money(abs(row["pl"])), col, GREEN_FILL if up else RED_FILL, up=up, size=13)
    c.text((500, 522), f"Page {data['page']}/{data['pages']} · Sell fee {data['fee_pct']:g}% · prices update when stats are added",
           10, GRAY, "mm")
    return c.done()
