"""Cards for the Googli stock exchange: market board, one stock, a portfolio.

Prices come from player form (see cogs/stocks.py). Data shapes:

market   {"guild", "count", "trend", "tiles": [(title, colour, [row, ...]), ...]}
         row = {"name", "ticker", "avatar", "price", "change"}      change in percent
stock    {"guild", "name", "ticker", "avatar", "price", "change", "history": [prices], "form", "matches",
          "high", "low", "rank", "listed", "holding": {"shares", "value", "cost"} | None}
portfolio {"guild", "user": {"name", "avatar"}, "networth", "rank", "players", "cash", "invested", "pl", "pl_pct",
           "rows": [{"name", "ticker", "avatar", "shares", "avg", "price", "value", "pl_pct"}]}
"""
from .awards import _tile_glass
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


# =========================================================================== market

def render_market(data):
    c = _Canvas(UP, BLUE)
    c.card((50, 22, 1550, 170), 26, 168)
    boxes = [(50, 186, 540, 852), (555, 186, 1045, 852), (1060, 186, 1550, 852)]
    for box in boxes:
        c.card(box, 26, 150)
    d = c.seal()
    _tile_glass(c, (50, 22, 1550, 170), GOLD)
    _title(d, (76, 78), "GOOGLI STOCK EXCHANGE", 44, WHITE, "lm", "head")
    _text(d, (76, 128), f"{data['count']} PLAYERS LISTED  •  AN AVERAGE PLAYER TRADES AT 100", 16, GOLD, "lm", "head")
    _text(d, (1524, 50), str(data.get("guild", "")).upper(), 13, DIM, "rm")
    _text(d, (1524, 86), "MARKET TREND", 13, MUTED, "rm")
    _change_pill(d, 1524, 104, data.get("trend", 0.0), h=34, size=19, right=True)
    for box, (title, color, rows) in zip(boxes, data["tiles"]):
        x1, y1, x2, y2 = box
        _tile_glass(c, box, color)
        d.ellipse((x1 + 28, y1 + 26, x1 + 40, y1 + 38), fill=color)
        _text(d, (x1 + 54, y1 + 18), title, 24, color, "la", "head")
        if not rows:
            _text(d, ((x1 + x2) // 2, (y1 + y2) // 2), "NOTHING YET", 18, DIM, "mm", "bold")
        for i, row in enumerate(rows[:6]):
            y = y1 + 70 + i * 96
            if i == 0:
                d.rounded_rectangle((x1 + 12, y - 6, x2 - 12, y + 84), radius=18, fill=_mix(color, _INK, 0.88),
                                    outline=_mix(color, _INK, 0.55), width=1)
            ring = _round_image(_open(row.get("avatar")), 58, ring=color if i == 0 else MUTED, ring_w=3,
                                initial=(row.get("name") or "?")[:1])
            _paste(c.image, ring, (x1 + 26, y + 39 - ring.size[1] // 2))
            nx = x1 + 26 + ring.size[0] + 14
            name = (row.get("name") or "?").upper()
            size = _fit(d, name, x2 - 150 - nx, 24, 13, "head")
            _text(d, (nx, y + 22), _clip(d, name, x2 - 150 - nx, size, "head"), size, WHITE, "lm", "head")
            _pill(d, nx, y + 46, row["ticker"], 11, fill=_mix(BLUE, _INK, 0.75), fg=BLUE, pad=10, h=24)
            _text(d, (x2 - 26, y + 22), money(row["price"]), 30, WHITE, "rm", "head")
            _change_pill(d, x2 - 26, y + 48, row["change"], right=True)
    _footer(c, "GOOGLI MANAGER  •  STOCK EXCHANGE  •  PRICES MOVE WITH PLAYER FORM", data.get("guild"))
    return c.image


def render_market_png(data):
    return _png(render_market(data))


# =========================================================================== one stock

def _chart(c, box, history, color):
    d = c.d
    x1, y1, x2, y2 = box
    pad_l, pad_r, pad_t, pad_b = 70, 40, 24, 46
    left, right, top, bottom = x1 + pad_l, x2 - pad_r, y1 + pad_t, y2 - pad_b
    values = history[-14:]
    low, high = min(values), max(values)
    span = max(high - low, max(6, high * 0.08))
    low -= span * 0.12
    high += span * 0.12
    for step in range(4):
        y = top + (bottom - top) * step / 3
        d.line((left, y, right, y), fill=_mix(MUTED, _INK, 0.78), width=1)
        _text(d, (left - 12, y), money(high - (high - low) * step / 3), 13, DIM, "rm", "bold")
    count = len(values)
    points = []
    for i, v in enumerate(values):
        x = left + (right - left) * (i / (count - 1) if count > 1 else 0.5)
        y = bottom - (bottom - top) * (v - low) / (high - low)
        points.append((x, y))
    if count > 1:
        d.polygon(points + [(points[-1][0], bottom), (points[0][0], bottom)], fill=_mix(color, _INK, 0.84))
        d.line(points, fill=color, width=5, joint="curve")
    for i, (x, y) in enumerate(points):
        last = i == count - 1
        r = 9 if last else 5
        d.ellipse((x - r, y - r, x + r, y + r), fill=WHITE if last else color, outline=color, width=3)
        offset = len(history) - count + i + 1
        _text(d, (x, bottom + 22), f"M{offset}" if (last or count < 9 or i % 2 == 0) else "", 12, DIM, "mm", "bold")
    if points:
        x, y = points[-1]
        label = money(values[-1])
        width = int(d.textlength(label, font=font(16, "head"))) + 22
        lx = min(x - width / 2, right - width + 10)
        ly = y - 44 if y - 44 > top - 10 else y + 16
        d.rounded_rectangle((lx, ly, lx + width, ly + 28), radius=14, fill=color)
        _text(d, (lx + width / 2, ly + 14), label, 16, _INK, "mm", "head")


def render_stock(data):
    up = data["change"] >= 0
    accent = UP if data["change"] > 0.05 else DOWN if data["change"] < -0.05 else GOLD
    c = _Canvas(accent, BLUE)
    c.card((50, 22, 520, 852), 26, 168)
    c.card((545, 22, 1550, 560), 26, 150)
    tiles = [(545 + i * 255, 578, 545 + i * 255 + 240, 852) for i in range(4)]
    for box in tiles:
        c.card(box, 26, 150)
    d = c.seal()
    _tile_glass(c, (50, 22, 520, 852), accent)
    ring = _round_image(_open(data.get("avatar")), 210, ring=accent, ring_w=6, initial=data["name"][:1])
    _paste(c.image, ring, (285 - ring.size[0] // 2, 70))
    name = data["name"].upper()
    size = _fit(d, name, 400, 34, 18, "head")
    _text(d, (285, 330), _clip(d, name, 400, size, "head"), size, WHITE, "mm", "head")
    width = int(d.textlength(data["ticker"], font=font(16, "bold"))) + 40
    _pill(d, 285 - width // 2, 360, data["ticker"], 16, fill=_mix(BLUE, _INK, 0.75), fg=BLUE, pad=20, h=34)
    _text(d, (285, 466), money(data["price"]), 84, WHITE, "mm", "head")
    _text(d, (285, 516), "COINS PER SHARE", 13, MUTED, "mm", "bold")
    _change_pill(d, 285 - 68, 540, data["change"], h=34, size=19)
    _text(d, (285, 606), f"RANK {data['rank']} OF {data['listed']} BY PRICE", 13, MUTED, "mm", "bold")
    hold = data.get("holding")
    d.rounded_rectangle((76, 640, 494, 826), radius=20, fill=_mix(GOLD, _INK, 0.88), outline=_mix(GOLD, _INK, 0.5), width=1)
    _text(d, (98, 660), "YOUR POSITION", 14, GOLD, "la", "head")
    if hold:
        pl = hold["value"] - hold["cost"]
        pl_pct = pl / hold["cost"] * 100 if hold["cost"] else 0
        _text(d, (98, 696), f"{hold['shares']:,} SHARES", 24, WHITE, "la", "head")
        _text(d, (98, 734), f"WORTH {money(hold['value'])}", 17, MUTED, "la", "bold")
        _text(d, (98, 770), "PROFIT / LOSS", 13, MUTED, "la", "bold")
        _text(d, (98, 790), f"{'+' if pl > 0 else ''}{money(pl)}  ({pct(pl_pct)})", 20, tone(pl), "la", "head")
    else:
        _text(d, (98, 716), "YOU DON'T OWN THIS STOCK", 16, DIM, "la", "bold")
        _text(d, (98, 748), "+buy <player> <amount>", 15, MUTED, "la", "bold")
    _tile_glass(c, (545, 22, 1550, 560), accent)
    _text(d, (580, 56), "PRICE AFTER EACH MATCH", 24, accent, "lm", "head")
    _text(d, (1520, 56), str(data.get("guild", "")).upper(), 13, DIM, "rm")
    _chart(c, (545, 80, 1550, 560), data["history"] or [data["price"]], accent)
    facts = [("FORM", f"{data['form']:.2f}x", "VS AN AVERAGE PLAYER", VIOLET),
             ("MATCHES", str(data["matches"]), "PLAYED SO FAR", BLUE),
             ("ALL-TIME HIGH", money(data["high"]), "BEST PRICE", UP),
             ("ALL-TIME LOW", money(data["low"]), "WORST PRICE", DOWN)]
    for box, (label, value, sub, col) in zip(tiles, facts):
        x1, y1, x2, y2 = box
        _tile_glass(c, box, col)
        _text(d, ((x1 + x2) // 2, y1 + 50), label, 18, col, "mm", "head")
        _text(d, ((x1 + x2) // 2, y1 + 140), value, _fit(d, value, 200, 56, 28, "head"), WHITE, "mm", "head")
        _text(d, ((x1 + x2) // 2, y1 + 216), sub, 12, MUTED, "mm", "bold")
    _footer(c, f"GOOGLI MANAGER  •  {data['ticker']}  •  PRICE FOLLOWS FANTASY FORM", data.get("guild"))
    return c.image


def render_stock_png(data):
    return _png(render_stock(data))


# =========================================================================== portfolio

def render_portfolio(data):
    c = _Canvas(GOLD, BLUE)
    c.card((50, 22, 1550, 250), 26, 168)
    c.card((50, 266, 1550, 852), 26, 150)
    d = c.seal()
    _tile_glass(c, (50, 22, 1550, 250), GOLD)
    user = data["user"]
    ring = _round_image(_open(user.get("avatar")), 130, ring=GOLD, ring_w=5, initial=user["name"][:1])
    _paste(c.image, ring, (80, 62))
    _text(d, (240, 70), "PORTFOLIO OF", 14, MUTED, "la", "bold")
    name = user["name"].upper()
    size = _fit(d, name, 360, 36, 18, "head")
    _text(d, (240, 96), _clip(d, name, 360, size, "head"), size, WHITE, "la", "head")
    _text(d, (240, 150), f"RANK {data['rank']} OF {data['players']} TRADERS", 15, GOLD, "la", "head")
    stats = [("NET WORTH", money(data["networth"]), WHITE),
             ("CASH", money(data["cash"]), BLUE),
             ("IN SHARES", money(data["invested"]), VIOLET),
             ("PROFIT / LOSS", ("+" if data["pl"] > 0 else "") + money(data["pl"]), tone(data["pl"]))]
    for i, (label, value, col) in enumerate(stats):
        cx = 700 + i * 220
        _text(d, (cx, 84), label, 14, MUTED, "mm", "bold")
        _text(d, (cx, 140), value, _fit(d, value, 200, 40 if i else 46, 22, "head"), col, "mm", "head")
    _change_pill(d, 700 + 3 * 220 - 50, 176, data["pl_pct"], h=28, size=15)
    _tile_glass(c, (50, 266, 1550, 852), BLUE)
    heads = [("PLAYER", 120, "la"), ("SHARES", 760, "ra"), ("AVG BUY", 930, "ra"), ("PRICE", 1090, "ra"),
             ("VALUE", 1260, "ra"), ("PROFIT / LOSS", 1510, "ra")]
    for label, x, anchor in heads:
        _text(d, (x, 296), label, 13, MUTED, anchor, "bold")
    d.line((80, 326, 1520, 326), fill=_mix(MUTED, _INK, 0.75), width=1)
    rows = data["rows"]
    if not rows:
        _text(d, (800, 560), "NO SHARES YET", 28, DIM, "mm", "head")
        _text(d, (800, 606), "BUY ONE WITH  +buy <player> <amount>", 17, MUTED, "mm", "bold")
    for i, row in enumerate(rows[:7]):
        y = 340 + i * 72
        ring = _round_image(_open(row.get("avatar")), 46, ring=MUTED, ring_w=2, initial=(row.get("name") or "?")[:1])
        _paste(c.image, ring, (90, y + 31 - ring.size[1] // 2))
        label = (row.get("name") or "?").upper()
        size = _fit(d, label, 420, 24, 13, "head")
        _text(d, (160, y + 20), _clip(d, label, 420, size, "head"), size, WHITE, "lm", "head")
        _pill(d, 160, y + 38, row["ticker"], 10, fill=_mix(BLUE, _INK, 0.75), fg=BLUE, pad=9, h=22)
        for text, x, col in ((f"{row['shares']:,}", 760, WHITE), (money(row["avg"]), 930, MUTED),
                             (money(row["price"]), 1090, WHITE), (money(row["value"]), 1260, WHITE)):
            _text(d, (x, y + 31), text, 22, col, "rm", "head")
        _text(d, (1510, y + 31), pct(row["pl_pct"]), 22, tone(row["pl_pct"]), "rm", "head")
    if len(rows) > 7:
        _text(d, (800, 836), f"+ {len(rows) - 7} MORE HOLDINGS", 13, DIM, "mm", "bold")
    _footer(c, "GOOGLI MANAGER  •  STOCK EXCHANGE  •  PORTFOLIO", data.get("guild"))
    return c.image


def render_portfolio_png(data):
    return _png(render_portfolio(data))
