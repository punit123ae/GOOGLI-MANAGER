"""Cards for the Googli stock exchange: market board, one stock, a portfolio.

Prices come from player form (see cogs/stocks.py). Data shapes:

market   {"guild", "count", "trend", "page", "pages", "sort", "supply", "rows": [{"name", "ticker", "avatar", "price",
          "change", "left", "rank", "spark": [prices]}]}                 10 player cards a page (5 x 2)
stock    {"guild", "name", "ticker", "avatar", "price", "change", "base", "factors": [(label, value)], "sr", "sr_par",
          "wickets", "wpo", "eco", "eco_par", "ducks", "duck_pen", "games", "history": [prices], "first": game number
          of history[0], "rank", "listed", "left", "supply", "max", "holding": {"shares", "value", "cost"} | None}
portfolio {"guild", "user": {"name", "avatar"}, "rank", "players", "cash", "worth", "paid", "pl", "shares", "count",
           "page", "pages", "max", "alloc": [(label, value)], "rows": [{"name", "ticker", "avatar", "shares", "price",
           "value", "pl", "spark"}]}   8 holding tiles a page (2 x 4)
"""
from .awards import _tile_glass
from PIL import Image, ImageDraw
from .teaminfo_image import DIM, MUTED, WHITE, _clip, _fit, _mix, _open, _paste, _pill, _round_image, _text, font
from .versus_image import _Canvas, _footer, _png, _title

_INK = (12, 18, 32)
GOLD = (255, 206, 90)
UP = (62, 224, 172)
DOWN = (255, 100, 112)
BLUE = (100, 185, 255)
VIOLET = (170, 130, 255)


def money(value):
    return f"{int(round(value)):,}"


def pct(value):
    return f"{'+' if value > 0 else ''}{value:.1f}%"


def tone(change):
    return UP if change > 0.05 else DOWN if change < -0.05 else MUTED


def _coin(d, x, y, r=11):
    """A small gold coin centred on (x, y)."""
    d.ellipse((x - r, y - r, x + r, y + r), fill=GOLD)
    d.ellipse((x - r + 4, y - r + 4, x + r - 4, y + r - 4), outline=_mix(GOLD, _INK, 0.45), width=2)


def signed(value):
    return ("+" if value > 0 else "\u2212" if value < 0 else "") + money(abs(value))


def _arrow(d, x, y, up, color, size=9):
    """A small filled triangle (fonts lack arrow glyphs)."""
    if up:
        d.polygon([(x, y + size), (x + size * 2, y + size), (x + size, y - size // 2)], fill=color)
    else:
        d.polygon([(x, y - size // 2), (x + size * 2, y - size // 2), (x + size, y + size)], fill=color)


def _change_pill(d, x, y, change, h=26, size=14, right=False):
    text = pct(change)
    col = tone(change)
    width = int(d.textlength(text, font=font(size, "head"))) + 52
    if right:
        x = x - width
    d.rounded_rectangle((x, y, x + width, y + h), radius=h // 2, fill=_mix(col, _INK, 0.76), outline=col, width=2)
    if abs(change) > 0.05:
        _arrow(d, x + 12, y + h // 2 - 3, change > 0, col, 6)
    _text(d, (x + 34, y + h // 2), text, size, col, "lm", "head")
    return width


# =========================================================================== shared pieces

def _spark(c, box, values, color, width=3):
    """A small area chart of prices in box, ending in a dot."""
    d = c.d
    x1, y1, x2, y2 = box
    values = list(values) or [0, 0]
    if len(values) == 1:
        values = values * 2
    lo, hi = min(values), max(values)
    span = max(hi - lo, 1, hi * 0.01)
    pts = [(x1 + (x2 - x1) * i / (len(values) - 1), y2 - (y2 - y1) * (v - lo) / span) for i, v in enumerate(values)]
    d.polygon(pts + [(x2, y2 + 4), (x1, y2 + 4)], fill=_mix(color, _INK, 0.86))
    d.line(pts, fill=color, width=width, joint="curve")
    x, y = pts[-1]
    d.ellipse((x - 5, y - 5, x + 5, y + 5), fill=WHITE, outline=color, width=3)


def _donut(c, center, r_out, r_in, slices):
    """slices [(value, colour)] as a smooth ring (drawn 4x larger, then shrunk)."""
    k = 4
    size = (r_out * 2 + 4) * k
    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer)
    total = sum(v for v, _c in slices) or 1
    box = (2 * k, 2 * k, size - 2 * k, size - 2 * k)
    angle = -90.0
    for value, col in slices:
        sweep = 360.0 * value / total
        if sweep > 0.4:
            ld.pieslice(box, angle, angle + sweep - 0.8, fill=col)
        angle += sweep
    inner = r_in * k
    mid = size // 2
    ld.ellipse((mid - inner, mid - inner, mid + inner, mid + inner), fill=(0, 0, 0, 0))
    layer = layer.resize((r_out * 2 + 4, r_out * 2 + 4), Image.LANCZOS)
    c.image.paste(layer, (center[0] - r_out - 2, center[1] - r_out - 2), layer)


def _pips(d, x, y, filled, total, color, size=9, gap=5):
    for i in range(total):
        cx = x + i * (size + gap)
        if i < filled:
            d.rounded_rectangle((cx, y, cx + size, y + size), radius=3, fill=color)
        else:
            d.rounded_rectangle((cx, y, cx + size, y + size), radius=3, outline=_mix(MUTED, _INK, 0.6), width=1)


# =========================================================================== market (10 wide cards a page, 2 columns)

def render_market(data):
    c = _Canvas(UP, BLUE)
    c.card((50, 22, 1550, 100), 26, 168)
    cw, ch, gx, gy, top = 742, 134, 16, 12, 114
    boxes = []
    for i in range(10):
        x = 50 + (i % 2) * (cw + gx)
        y = top + (i // 2) * (ch + gy)
        boxes.append((x, y, x + cw, y + ch))
        c.card(boxes[-1], 24, 150)
    d = c.seal()
    _tile_glass(c, (50, 22, 1550, 100), GOLD)
    _title(d, (76, 61), "PLAYER STOCK MARKET", 34, WHITE, "lm", "head")
    _text(d, (690, 61), f"{data['sort']}  •  {data['count']} LISTED", 14, GOLD, "lm", "head")
    _text(d, (1270, 61), "MARKET MOOD", 12, MUTED, "rm", "bold")
    _change_pill(d, 1400, 48, data.get("trend", 0.0), h=28, size=15, right=True)
    _text(d, (1524, 61), f"PAGE {data['page']}/{data['pages']}", 15, WHITE, "rm", "head")
    rows = data["rows"]
    for box, row in zip(boxes, rows):
        x1, y1, x2, y2 = box
        col = tone(row["change"]) if abs(row["change"]) > 0.05 else GOLD
        _tile_glass(c, box, col)
        top3 = row["rank"] <= 3
        d.ellipse((x1 + 16, y1 + 16, x1 + 52, y1 + 52), fill=_mix(GOLD, _INK, 0.65) if top3 else _mix(MUTED, _INK, 0.78))
        _text(d, (x1 + 34, y1 + 34), str(row["rank"]), 16, GOLD if top3 else MUTED, "mm", "head")
        ring = _round_image(_open(row.get("avatar")), 76, ring=col, ring_w=4, initial=(row.get("name") or "?")[:1])
        _paste(c.image, ring, (x1 + 66, y1 + 67 - ring.size[1] // 2))
        nx = x1 + 164
        name = (row.get("name") or "?").upper()
        size = _fit(d, name, 220, 21, 12, "head")
        _text(d, (nx, y1 + 38), _clip(d, name, 220, size, "head"), size, WHITE, "lm", "head")
        _pill(d, nx, y1 + 56, row["ticker"], 10, fill=_mix(BLUE, _INK, 0.75), fg=BLUE, pad=8, h=20)
        supply = max(1, data["supply"])
        frac = max(0.0, min(1.0, row["left"] / supply))
        d.rounded_rectangle((nx, y1 + 100, nx + 110, y1 + 108), radius=4, fill=_mix(MUTED, _INK, 0.8))
        if frac > 0:
            d.rounded_rectangle((nx, y1 + 100, nx + max(8, int(110 * frac)), y1 + 108), radius=4, fill=BLUE if frac > 0.2 else GOLD)
        _text(d, (nx + 124, y1 + 104), "SOLD OUT" if not row["left"] else f"{row['left']} OF {supply} LEFT", 11,
              DOWN if not row["left"] else MUTED, "lm", "bold")
        _spark(c, (x1 + 400, y1 + 34, x1 + 540, y1 + 92), row.get("spark") or [row["price"]], col, 3)
        label = money(row["price"])
        tw = d.textlength(label, font=font(30, "head"))
        _coin(d, int(x2 - 22 - tw - 20), y1 + 44, 10)
        _text(d, (x2 - 22, y1 + 44), label, 30, WHITE, "rm", "head")
        _change_pill(d, x2 - 22, y1 + 76, row["change"], h=28, size=14, right=True)
    if not rows:
        _text(d, (800, 480), "NOTHING ON THIS PAGE", 26, DIM, "mm", "head")
    _footer(c, "GOOGLI MANAGER  •  STOCK EXCHANGE  •  +stock <player> OPENS A FULL CARD", data.get("guild"))
    return c.image


def render_market_png(data):
    return _png(render_market(data))


# =========================================================================== one stock

def _history_chart(c, box, data, color):
    d = c.d
    x1, y1, x2, y2 = box
    values = data["history"]
    base = data["base"]
    left, right, top, bottom = x1 + 96, x2 - 50, y1 + 96, y2 - 64
    lo, hi = min(values + [base]), max(values + [base])
    span = max(hi - lo, 1, hi * 0.02)
    lo -= span * 0.15
    hi += span * 0.2
    def y_of(v):
        return bottom - (bottom - top) * (v - lo) / (hi - lo)
    for step in range(4):
        v = lo + (hi - lo) * step / 3
        y = y_of(v)
        d.line((left, y, right, y), fill=_mix(MUTED, _INK, 0.82), width=1)
        _text(d, (left - 14, y), f"{v / 1000:.1f}K" if hi > 9999 else money(v), 12, DIM, "rm", "bold")
    n = len(values)
    pts = [(left + (right - left) * (i / (n - 1) if n > 1 else 0.5), y_of(v)) for i, v in enumerate(values)]
    if n > 1:
        d.polygon(pts + [(pts[-1][0], bottom), (pts[0][0], bottom)], fill=_mix(color, _INK, 0.84))
        d.line(pts, fill=color, width=5, joint="curve")
    by = y_of(base)
    for xx in range(int(left), int(right), 16):
        d.line((xx, by, xx + 8, by), fill=GOLD, width=2)
    _text(d, (right, by - 14), f"BASE {money(base)}", 12, GOLD, "rm", "bold")
    for i, (x, y) in enumerate(pts):
        last = i == n - 1
        r = 9 if last else 4
        d.ellipse((x - r, y - r, x + r, y + r), fill=WHITE if last else color, outline=color, width=3)
        game = data["first"] + i
        if last or n <= 8 or i % 2 == 0:
            _text(d, (x, bottom + 24), f"G{game}", 12, DIM, "mm", "bold")
    x, y = pts[-1]
    label = money(values[-1])
    w = int(d.textlength(label, font=font(17, "head"))) + 24
    lx = min(x - w / 2, right - w + 30)
    d.rounded_rectangle((lx, y - 48, lx + w, y - 20), radius=14, fill=color)
    _text(d, (lx + w / 2, y - 34), label, 17, _INK, "mm", "head")


def _waterfall(c, box, data):
    d = c.d
    x1, y1, x2, y2 = box
    mid = (x1 + x2) // 2
    factors = data["factors"]
    big = max([abs(v) for _l, v in factors] + [1])
    d.line((mid, y1 + 70, mid, y1 + 70 + len(factors) * 52), fill=_mix(MUTED, _INK, 0.7), width=2)
    half = (x2 - x1) // 2 - 52
    for i, (label, value) in enumerate(factors):
        y = y1 + 70 + i * 52
        col = tone(value)
        _text(d, (x1 + 28, y + 12), label.upper(), 12, MUTED, "lm", "bold")
        _text(d, (x2 - 28, y + 12), signed(value), 14, col, "rm", "head")
        length = int(half * abs(value) / big)
        if value:
            bx = (mid, mid + max(6, length)) if value > 0 else (mid - max(6, length), mid)
            d.rounded_rectangle((bx[0], y + 26, bx[1], y + 40), radius=7, fill=col)
    ty = y1 + 70 + len(factors) * 52 + 4
    d.line((x1 + 28, ty, x2 - 28, ty), fill=_mix(MUTED, _INK, 0.75), width=1)
    _text(d, (x1 + 28, ty + 26), f"STARTED AT {money(data['base'])}", 13, MUTED, "lm", "bold")
    _text(d, (x2 - 28, ty + 26), f"NOW {money(data['price'])}", 18, WHITE, "rm", "head")


def render_stock(data):
    accent = UP if data["change"] > 0.05 else DOWN if data["change"] < -0.05 else GOLD
    c = _Canvas(accent, BLUE)
    c.card((50, 22, 1550, 226), 28, 168)
    c.card((50, 242, 880, 852), 26, 150)
    c.card((896, 242, 1550, 852), 26, 150)
    d = c.seal()
    _tile_glass(c, (50, 22, 1550, 226), accent)
    ring = _round_image(_open(data.get("avatar")), 150, ring=accent, ring_w=6, initial=data["name"][:1])
    _paste(c.image, ring, (82, 124 - ring.size[1] // 2))
    name = data["name"].upper()
    size = _fit(d, name, 430, 40, 20, "head")
    _text(d, (262, 82), _clip(d, name, 430, size, "head"), size, WHITE, "lm", "head")
    _pill(d, 262, 112, data["ticker"], 15, fill=_mix(BLUE, _INK, 0.75), fg=BLUE, pad=18, h=30)
    _text(d, (262, 172), f"#{data['rank']} OF {data['listed']} BY PRICE  •  {data['games']} GAMES", 14, MUTED, "lm", "bold")
    label = money(data["price"])
    _coin(d, 760, 100, 18)
    _text(d, (792, 100), label, 70, WHITE, "lm", "head")
    _change_pill(d, 792, 150, data["change"], h=34, size=18)
    _text(d, (948, 167), "SINCE LAST GAME", 12, MUTED, "lm", "bold")
    # supply ring and the viewer's position
    left, supply = data["left"], max(1, data["supply"])
    _donut(c, (1240, 124), 70, 54, [(supply - left, _mix(MUTED, _INK, 0.55)), (left, BLUE)] if left else
           [(1, DOWN)])
    _text(d, (1240, 116), str(left), 34, WHITE, "mm", "head")
    _text(d, (1240, 146), "FREE", 11, MUTED, "mm", "bold")
    _text(d, (1240, 208), f"OF {data['supply']} SHARES", 11, DIM, "mm", "bold")
    hold = data.get("holding")
    d.rounded_rectangle((1340, 52, 1524, 196), radius=22, fill=_mix(GOLD, _INK, 0.9), outline=_mix(GOLD, _INK, 0.5), width=2)
    _text(d, (1432, 76), "YOU HOLD", 12, GOLD, "mm", "head")
    if hold:
        pl = hold["value"] - hold["cost"]
        _text(d, (1432, 116), f"{hold['shares']}", 36, WHITE, "mm", "head")
        _text(d, (1432, 150), f"OF {data['max']} ALLOWED", 11, MUTED, "mm", "bold")
        _text(d, (1432, 176), signed(pl), 15, tone(pl), "mm", "head")
    else:
        _text(d, (1432, 120), "NONE", 28, DIM, "mm", "head")
        _text(d, (1432, 164), f"LIMIT {data['max']} EACH", 11, MUTED, "mm", "bold")
    _tile_glass(c, (50, 242, 880, 852), accent)
    _text(d, (82, 278), "PRICE JOURNEY", 18, accent, "lm", "head")
    _text(d, (848, 278), f"LAST {len(data['history'])} GAMES", 12, DIM, "rm", "bold")
    if data["history"]:
        _history_chart(c, (50, 242, 880, 852), data, accent)
    else:
        _text(d, (465, 560), "NO GAMES YET", 24, DIM, "mm", "head")
    _tile_glass(c, (896, 242, 1550, 852), BLUE)
    _text(d, (928, 278), "WHERE THE COINS CAME FROM", 18, BLUE, "lm", "head")
    _waterfall(c, (896, 242, 1550, 852), data)
    facts = [("STRIKE RATE", f"{data['sr']:.0f}", f"LEAGUE {data['sr_par']:.0f}", BLUE),
             ("WICKETS", str(data["wickets"]), f"{data['wpo']:.1f} / OVER", VIOLET),
             ("ECONOMY", f"{data['eco']:.1f}", f"LEAGUE {data['eco_par']:.1f}", GOLD),
             ("DUCKS", str(data["ducks"]), "OUT FOR 0", DOWN),
             ("CATCHES", str(data["catches"]), "TAKEN", UP),
             ("DROPS", str(data["drops"]), "DROPPED", DOWN)]
    for i, (label, value, sub, col) in enumerate(facts):
        bx = 920 + (i % 3) * 205
        by = 690 + (i // 3) * 76
        d.rounded_rectangle((bx, by, bx + 195, by + 68), radius=16, fill=_mix(col, _INK, 0.9), outline=_mix(col, _INK, 0.55), width=1)
        _text(d, (bx + 14, by + 20), label, 11, col, "lm", "head")
        _text(d, (bx + 14, by + 48), sub, 10, MUTED, "lm", "bold")
        _text(d, (bx + 181, by + 34), value, 26, WHITE, "rm", "head")
    _footer(c, f"GOOGLI MANAGER  •  {data['ticker']}  •  PRICE FOLLOWS MATCH STATS", data.get("guild"))
    return c.image


def render_stock_png(data):
    return _png(render_stock(data))


# =========================================================================== portfolio (wealth donut + 8 holding tiles)

_SLICES = [(100, 185, 255), (170, 130, 255), (62, 224, 172), (255, 150, 90), (255, 110, 170), (140, 150, 175), (255, 214, 90)]


def render_portfolio(data):
    c = _Canvas(GOLD, BLUE)
    c.card((50, 22, 1550, 168), 28, 168)
    c.card((50, 184, 500, 852), 26, 150)
    tiles = []
    for i in range(8):
        x = 516 + (i % 2) * 519
        y = 184 + (i // 2) * 170
        tiles.append((x, y, x + 505, y + 158))
        c.card(tiles[-1], 22, 150)
    d = c.seal()
    _tile_glass(c, (50, 22, 1550, 168), GOLD)
    user = data["user"]
    ring = _round_image(_open(user.get("avatar")), 110, ring=GOLD, ring_w=5, initial=user["name"][:1])
    _paste(c.image, ring, (80, 95 - ring.size[1] // 2))
    name = user["name"].upper()
    size = _fit(d, name, 320, 32, 16, "head")
    _text(d, (212, 68), _clip(d, name, 320, size, "head"), size, WHITE, "lm", "head")
    _text(d, (212, 104), f"RANK {data['rank']} OF {data['players']}  •  {data['count']} PLAYERS  •  {data['shares']} SHARES", 13, GOLD, "lm", "head")
    _text(d, (212, 130), f"CASH IN HAND  {money(data['cash'])}", 12, MUTED, "lm", "bold")
    label = money(data["worth"])
    _text(d, (700, 52), "SHARES WORTH", 12, MUTED, "lm", "bold")
    _coin(d, 712, 100, 16)
    _text(d, (738, 100), label, _fit(d, label, 300, 54, 26, "head"), WHITE, "lm", "head")
    pl = data["pl"]
    _text(d, (1140, 52), "PROFIT / LOSS", 12, MUTED, "lm", "bold")
    _text(d, (1140, 98), signed(pl), _fit(d, signed(pl), 230, 40, 20, "head"), tone(pl), "lm", "head")
    pct = pl / data["paid"] * 100 if data["paid"] else 0.0
    _change_pill(d, 1140, 122, pct, h=26, size=14)
    _text(d, (1524, 52), f"PAGE {data['page']}/{data['pages']}", 13, MUTED, "rm", "head")
    _text(d, (1524, 84), "PAID", 11, MUTED, "rm", "bold")
    _text(d, (1524, 108), money(data["paid"]), 18, BLUE, "rm", "head")
    # where the wealth sits
    _tile_glass(c, (50, 184, 500, 852), BLUE)
    _text(d, (82, 220), "WHERE YOUR WEALTH SITS", 15, BLUE, "lm", "head")
    alloc = data["alloc"]
    slices = [(v, _SLICES[i % len(_SLICES)]) for i, (_l, v) in enumerate(alloc)]
    total = sum(v for _l, v in alloc)
    _donut(c, (275, 400), 130, 94, slices or [(1, _mix(MUTED, _INK, 0.6))])
    _text(d, (275, 388), money(total), _fit(d, money(total), 150, 30, 16, "head"), WHITE, "mm", "head")
    _text(d, (275, 420), "NET WORTH", 11, MUTED, "mm", "bold")
    for i, (lab, v) in enumerate(alloc[:7]):
        y = 572 + i * 38
        col = _SLICES[i % len(_SLICES)]
        d.rounded_rectangle((82, y - 8, 98, y + 8), radius=5, fill=col)
        _text(d, (110, y), lab.upper(), 13, WHITE, "lm", "bold")
        _text(d, (468, y), f"{(v / total * 100) if total else 0:.0f}%", 14, MUTED, "rm", "head")
    rows = data["rows"]
    if not rows:
        _text(d, (1033, 520), "NO SHARES YET", 28, DIM, "mm", "head")
        _text(d, (1033, 566), "BUY ONE WITH  +buy <player> <amount>", 17, MUTED, "mm", "bold")
    for box, row in zip(tiles, rows):
        x1, y1, x2, y2 = box
        col = tone(row["pl"]) if row["pl"] else GOLD
        _tile_glass(c, box, col)
        ring = _round_image(_open(row.get("avatar")), 58, ring=col, ring_w=3, initial=(row.get("name") or "?")[:1])
        _paste(c.image, ring, (x1 + 20, y1 + 20))
        label = (row.get("name") or "?").upper()
        size = _fit(d, label, 220, 18, 11, "head")
        _text(d, (x1 + 92, y1 + 34), _clip(d, label, 220, size, "head"), size, WHITE, "lm", "head")
        _pill(d, x1 + 92, y1 + 52, row["ticker"], 10, fill=_mix(BLUE, _INK, 0.75), fg=BLUE, pad=8, h=19)
        _text(d, (x2 - 20, y1 + 34), money(row["value"]), 24, WHITE, "rm", "head")
        _text(d, (x2 - 20, y1 + 62), f"@ {money(row['price'])}", 12, MUTED, "rm", "bold")
        _spark(c, (x1 + 20, y1 + 96, x1 + 210, y1 + 128), row.get("spark") or [row["price"]], col, 3)
        _pips(d, x1 + 232, y1 + 98, row["shares"], data["max"], GOLD, size=9, gap=4)
        _text(d, (x1 + 232, y1 + 130), f"{row['shares']} / {data['max']} SHARES", 11, MUTED, "lm", "bold")
        w = int(d.textlength(money(abs(row["pl"])), font=font(15, "head"))) + 46
        d.rounded_rectangle((x2 - 20 - w, y1 + 100, x2 - 20, y1 + 130), radius=15, fill=_mix(col, _INK, 0.78), outline=col, width=2)
        if row["pl"]:
            _arrow(d, x2 - 20 - w + 12, y1 + 112, row["pl"] > 0, col, 6)
        _text(d, (x2 - 32, y1 + 116), money(abs(row["pl"])), 15, col, "rm", "head")
    _footer(c, "GOOGLI MANAGER  •  STOCK EXCHANGE  •  PORTFOLIO", data.get("guild"))
    return c.image


def render_portfolio_png(data):
    return _png(render_portfolio(data))


# =========================================================================== trade receipt

def render_trade(data):
    buy = data["kind"] == "buy"
    profit = data.get("profit", 0)
    accent = BLUE if buy else (UP if profit >= 0 else DOWN)
    c = _Canvas(accent, VIOLET if buy else BLUE)
    c.card((50, 22, 520, 852), 28, 168)
    c.card((545, 22, 1550, 520), 26, 150)
    tiles = [(545 + i * 340, 536, 545 + i * 340 + 325, 852) for i in range(3)]
    for box in tiles:
        c.card(box, 24, 150)
    d = c.seal()
    _tile_glass(c, (50, 22, 520, 852), accent)
    ring = _round_image(_open(data.get("avatar")), 200, ring=accent, ring_w=6, initial=data["name"][:1])
    _paste(c.image, ring, (285 - ring.size[0] // 2, 70))
    name = data["name"].upper()
    size = _fit(d, name, 400, 32, 17, "head")
    _text(d, (285, 318), _clip(d, name, 400, size, "head"), size, WHITE, "mm", "head")
    width = int(d.textlength(data["ticker"], font=font(15, "bold"))) + 40
    _pill(d, 285 - width // 2, 344, data["ticker"], 15, fill=_mix(BLUE, _INK, 0.75), fg=BLUE, pad=20, h=32)
    d.rounded_rectangle((76, 430, 494, 560), radius=26, fill=_mix(accent, _INK, 0.82), outline=accent, width=3)
    _text(d, (285, 470), "BOUGHT" if buy else "SOLD", 30, accent, "mm", "head")
    _text(d, (285, 520), f"{data['amount']:,} SHARE{'S' if data['amount'] != 1 else ''}", 34, WHITE, "mm", "head")
    _text(d, (285, 610), f"AT {money(data['price'])} EACH", 16, MUTED, "mm", "bold")
    _text(d, (285, 700), f"{data['left']} OF {data['supply']} SHARES", 13, DIM, "mm", "bold")
    _text(d, (285, 726), "STILL ON THE MARKET", 13, DIM, "mm", "bold")
    _tile_glass(c, (545, 22, 1550, 520), accent)
    _text(d, (580, 58), "RECEIPT", 20, accent, "lm", "head")
    _text(d, (1520, 58), str(data.get("guild", "")).upper(), 12, DIM, "rm")
    lines = [(f"{data['amount']:,} × {money(data['price'])}", money(data["subtotal"]), WHITE),
             (f"Fee ({data['fee_pct']:g}%)", ("+" if buy else "−") + money(data["fee"]), DOWN)]
    for i, (lab, val, col) in enumerate(lines):
        y = 124 + i * 66
        _text(d, (590, y), lab.upper(), 17, MUTED, "lm", "bold")
        _coin(d, 1520 - int(d.textlength(val, font=font(30, "head"))) - 26, y, 10)
        _text(d, (1520, y), val, 30, col, "rm", "head")
        d.line((580, y + 34, 1520, y + 34), fill=_mix(MUTED, _INK, 0.78), width=1)
    d.rounded_rectangle((580, 282, 1520, 490), radius=24, fill=_mix(accent, _INK, 0.88), outline=_mix(accent, _INK, 0.45), width=2)
    _text(d, (616, 326), "YOU PAID" if buy else "YOU RECEIVED", 18, accent, "lm", "head")
    label = money(data["total"])
    fs = _fit(d, label, 520, 92, 40, "head")
    _text(d, (1486, 408), label, fs, WHITE, "rm", "head")
    _coin(d, int(1486 - d.textlength(label, font=font(fs, "head")) - 34), 408, 18)
    if buy:
        facts = [("COINS LEFT", money(data["cash"]), "IN YOUR WALLET", GOLD),
                 ("NOW HOLDING", f"{data['owned']} / {data['max']}", "SHARES OF THIS PLAYER", BLUE),
                 ("AVERAGE COST", money(data["avg"]), "PER SHARE, FEES INCLUDED", VIOLET)]
    else:
        facts = [("PROFIT / LOSS", signed(profit), f"{pct(data['profit_pct'])} ON THE {data['amount']} SOLD", tone(profit)),
                 ("COINS NOW", money(data["cash"]), "IN YOUR WALLET", GOLD),
                 ("STILL HOLDING", f"{data['owned']} / {data['max']}", f"TOTAL BOOKED {signed(data['realized'])}", BLUE)]
    for box, (label, value, sub, col) in zip(tiles, facts):
        x1, y1, x2, y2 = box
        _tile_glass(c, box, col)
        _text(d, ((x1 + x2) // 2, y1 + 48), label, 17, col, "mm", "head")
        shade = col if label == "PROFIT / LOSS" else WHITE
        _text(d, ((x1 + x2) // 2, y1 + 150), value, _fit(d, value, 290, 58, 26, "head"), shade, "mm", "head")
        _text(d, ((x1 + x2) // 2, y1 + 248), sub, 12, MUTED, "mm", "bold")
    _footer(c, f"GOOGLI MANAGER  •  {'BUY' if buy else 'SELL'} RECEIPT  •  {data['ticker']}", data.get("guild"))
    return c.image


def render_trade_png(data):
    return _png(render_trade(data))


# =========================================================================== wallet

def render_wallet(data):
    start = data["start"]
    growth = data["net"] - start
    gpct = growth / start * 100 if start else 0.0
    accent = UP if growth > 0 else DOWN if growth < 0 else GOLD
    c = _Canvas(accent, BLUE)
    c.card((50, 22, 560, 852), 28, 168)
    tiles = []
    for i in range(8):
        x = 576 + (i % 2) * 494
        y = 22 + (i // 2) * 209
        tiles.append((x, y, x + 480, y + 195))
        c.card(tiles[-1], 24, 150)
    d = c.seal()
    _tile_glass(c, (50, 22, 560, 852), accent)
    user = data["user"]
    ring = _round_image(_open(user.get("avatar")), 150, ring=accent, ring_w=6, initial=user["name"][:1])
    _paste(c.image, ring, (305 - ring.size[0] // 2, 56))
    name = user["name"].upper()
    size = _fit(d, name, 430, 32, 16, "head")
    _text(d, (305, 244), _clip(d, name, 430, size, "head"), size, WHITE, "mm", "head")
    _text(d, (305, 278), f"RANK {data['rank']} OF {data['players']} TRADERS", 13, GOLD, "mm", "head")
    _text(d, (305, 336), "NET WORTH", 14, MUTED, "mm", "bold")
    label = money(data["net"])
    fs = _fit(d, label, 340, 66, 30, "head")
    _coin(d, int(305 - d.textlength(label, font=font(fs, "head")) / 2 - 26), 392, 14)
    _text(d, (305 + 12, 392), label, fs, WHITE, "mm", "head")
    _change_pill(d, 305 - 66, 430, gpct, h=34, size=18)
    _text(d, (305, 492), f"{signed(growth)} SINCE STARTING WITH {money(start)}", 12, MUTED, "mm", "bold")
    d.line((86, 530, 524, 530), fill=_mix(MUTED, _INK, 0.75), width=1)
    for i, (lab, item, col) in enumerate((("BEST HOLDING", data.get("best"), UP), ("WEAKEST HOLDING", data.get("worst"), DOWN))):
        y = 556 + i * 134
        d.rounded_rectangle((80, y, 530, y + 118), radius=20, fill=_mix(col, _INK, 0.9), outline=_mix(col, _INK, 0.55), width=1)
        _text(d, (104, y + 28), lab, 12, col, "lm", "head")
        if item:
            nm = item[0].upper()
            _text(d, (104, y + 62), _clip(d, nm, 400, 20, "head"), 20, WHITE, "lm", "head")
            _text(d, (104, y + 94), signed(item[1]), 20, tone(item[1]), "lm", "head")
        else:
            _text(d, (104, y + 72), "NO SHARES HELD", 16, DIM, "lm", "bold")
    facts = [("COINS IN HAND", money(data["cash"]), "READY TO SPEND", GOLD),
             ("SHARES WORTH", money(data["worth"]), f"{data['holdings']} PLAYERS HELD", BLUE),
             ("PAID FOR SHARES", money(data["paid"]), "COST BASIS INCL. FEES", VIOLET),
             ("PROFIT / LOSS ON HOLDINGS", signed(data["unrealized"]), "NOT BOOKED YET", tone(data["unrealized"])),
             ("BOOKED PROFIT / LOSS", signed(data["realized"]), "FROM SHARES SOLD", tone(data["realized"])),
             ("FEES PAID", money(data["fees"]), "ON ALL TRADES", DOWN),
             ("TRADES MADE", str(data["trades"]), "BUYS AND SELLS", BLUE),
             ("WEALTH CHANGE", pct(gpct), "NET WORTH VS START", tone(growth))]
    for box, (label, value, sub, col) in zip(tiles, facts):
        x1, y1, x2, y2 = box
        _tile_glass(c, box, col)
        _text(d, ((x1 + x2) // 2, y1 + 36), label, 14, col, "mm", "head")
        shade = WHITE if col in (GOLD, BLUE, VIOLET, DOWN) and label not in ("PROFIT / LOSS ON HOLDINGS", "BOOKED PROFIT / LOSS") else col
        _text(d, ((x1 + x2) // 2, y1 + 104), value, _fit(d, value, 420, 48, 24, "head"), shade, "mm", "head")
        _text(d, ((x1 + x2) // 2, y1 + 164), sub, 11, MUTED, "mm", "bold")
    _footer(c, "GOOGLI MANAGER  •  STOCK EXCHANGE  •  WALLET", data.get("guild"))
    return c.image


def render_wallet_png(data):
    return _png(render_wallet(data))
