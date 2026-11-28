"""Cards for the Googli stock exchange: market board, one stock, a portfolio.

Prices come from player form (see cogs/stocks.py). Data shapes:

market   {"guild", "count", "trend", "page", "pages", "sort", "supply", "rows": [{"name", "ticker", "avatar", "price",
          "change", "left", "rank"}]}                      change in percent, 10 rows a page
stock    {"guild", "name", "ticker", "avatar", "price", "change", "base", "factors": [(label, value)], "sr", "sr_par",
          "wickets", "wpo", "eco", "eco_par", "ducks", "duck_pen", "games", "last": [(game, price, delta)], "rank",
          "listed", "left", "supply", "max", "holding": {"shares", "value", "cost"} | None}
portfolio {"guild", "user": {"name", "avatar"}, "rank", "players", "cash", "worth", "paid", "pl", "shares", "count",
           "page", "pages", "max", "rows": [{"name", "ticker", "avatar", "shares", "price", "value", "pl"}]}
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


# =========================================================================== market (10 players a page)

def render_market(data):
    c = _Canvas(UP, BLUE)
    c.card((50, 22, 1550, 146), 26, 168)
    c.card((50, 162, 1550, 852), 26, 150)
    d = c.seal()
    _tile_glass(c, (50, 22, 1550, 146), GOLD)
    _title(d, (76, 66), "PLAYER STOCK MARKET", 42, WHITE, "lm", "head")
    _text(d, (76, 114), f"{data['sort']}  •  {data['count']} PLAYERS LISTED", 15, GOLD, "lm", "head")
    _text(d, (1524, 50), f"PAGE {data['page']} OF {data['pages']}", 15, MUTED, "rm", "head")
    _text(d, (1524, 82), "MARKET TREND", 12, MUTED, "rm")
    _change_pill(d, 1524, 94, data.get("trend", 0.0), h=30, size=16, right=True)
    _tile_glass(c, (50, 162, 1550, 852), BLUE)
    for label, x, anchor in (("#", 104, "mm"), ("PLAYER", 196, "lm"), ("PRICE", 1010, "rm"), ("MOVE", 1115, "mm"),
                             ("SHARES LEFT", 1262, "lm")):
        _text(d, (x, 194), label, 13, MUTED, anchor, "bold")
    d.line((80, 214, 1520, 214), fill=_mix(MUTED, _INK, 0.75), width=1)
    rows = data["rows"]
    if not rows:
        _text(d, (800, 500), "NOTHING ON THIS PAGE", 26, DIM, "mm", "head")
    for i, row in enumerate(rows[:10]):
        y = 224 + i * 62
        if i % 2 == 0:
            d.rounded_rectangle((70, y, 1530, y + 56), radius=16, fill=_mix(BLUE, _INK, 0.93))
        top = row["rank"] <= 3
        _text(d, (104, y + 28), str(row["rank"]), 22, GOLD if top else MUTED, "mm", "head")
        ring = _round_image(_open(row.get("avatar")), 44, ring=GOLD if top else MUTED, ring_w=3,
                            initial=(row.get("name") or "?")[:1])
        _paste(c.image, ring, (138, y + 28 - ring.size[1] // 2))
        name = (row.get("name") or "?").upper()
        size = _fit(d, name, 330, 21, 12, "head")
        _text(d, (196, y + 19), _clip(d, name, 330, size, "head"), size, WHITE, "lm", "head")
        _pill(d, 196, y + 33, row["ticker"], 10, fill=_mix(BLUE, _INK, 0.75), fg=BLUE, pad=8, h=19)
        label = money(row["price"])
        _coin(d, int(1010 - d.textlength(label, font=font(25, "head")) - 22), y + 28, 10)
        _text(d, (1010, y + 28), label, 25, WHITE, "rm", "head")
        _change_pill(d, 1115 - 62, y + 15, row["change"], h=26, size=14)
        supply = max(1, data["supply"])
        frac = max(0.0, min(1.0, row["left"] / supply))
        d.rounded_rectangle((1262, y + 22, 1432, y + 34), radius=6, fill=_mix(MUTED, _INK, 0.8))
        if frac > 0:
            d.rounded_rectangle((1262, y + 22, 1262 + max(12, int(170 * frac)), y + 34), radius=6,
                                fill=BLUE if frac > 0.25 else GOLD if frac > 0 else DOWN)
        _text(d, (1510, y + 28), str(row["left"]), 20, WHITE if row["left"] else DOWN, "rm", "head")
    _footer(c, "GOOGLI MANAGER  •  STOCK EXCHANGE  •  +stock <player> FOR DETAILS", data.get("guild"))
    return c.image


def render_market_png(data):
    return _png(render_market(data))


# =========================================================================== one stock

def _last_games(c, box, last, color):
    """Up to five games: price above each dot, the game number and the move under it."""
    d = c.d
    x1, y1, x2, y2 = box
    left, right = x1 + 90, x2 - 90
    top, bottom = y1 + 108, y2 - 96
    prices = [p for _g, p, _dl in last]
    low, high = min(prices), max(prices)
    span = max(high - low, 1, high * 0.01)
    low -= span * 0.3
    high += span * 0.3
    for step in range(3):
        y = top + (bottom - top) * step / 2
        d.line((x1 + 30, y, x2 - 30, y), fill=_mix(MUTED, _INK, 0.8), width=1)
    count = len(last)
    pts = []
    for i, (_g, price, _dl) in enumerate(last):
        x = left + (right - left) * (i / (count - 1) if count > 1 else 0.5)
        pts.append((x, bottom - (bottom - top) * (price - low) / (high - low)))
    if count > 1:
        d.line(pts, fill=color, width=4, joint="curve")
    for (x, y), (game, price, delta) in zip(pts, last):
        col = tone(delta)
        d.ellipse((x - 8, y - 8, x + 8, y + 8), fill=col, outline=WHITE, width=2)
        _text(d, (x, y - 26), money(price), 16, WHITE, "mm", "head")
        _text(d, (x, y2 - 62), f"GAME {game}", 12, MUTED, "mm", "bold")
        if abs(delta) >= 1:
            _arrow(d, x - 36, y2 - 34, delta > 0, col, 6)
            _text(d, (x - 20, y2 - 30), money(abs(delta)), 15, col, "lm", "head")
        else:
            _text(d, (x, y2 - 30), "NO CHANGE", 12, DIM, "mm", "bold")


def render_stock(data):
    accent = UP if data["change"] > 0.05 else DOWN if data["change"] < -0.05 else GOLD
    c = _Canvas(accent, BLUE)
    c.card((50, 22, 520, 852), 26, 168)
    tiles = [(545 + i * 255, 22, 545 + i * 255 + 240, 170) for i in range(4)]
    for box in tiles:
        c.card(box, 24, 150)
    c.card((545, 186, 1550, 528), 26, 150)
    c.card((545, 544, 1550, 852), 26, 150)
    d = c.seal()
    _tile_glass(c, (50, 22, 520, 852), accent)
    ring = _round_image(_open(data.get("avatar")), 190, ring=accent, ring_w=6, initial=data["name"][:1])
    _paste(c.image, ring, (285 - ring.size[0] // 2, 56))
    name = data["name"].upper()
    size = _fit(d, name, 400, 32, 17, "head")
    _text(d, (285, 288), _clip(d, name, 400, size, "head"), size, WHITE, "mm", "head")
    width = int(d.textlength(data["ticker"], font=font(15, "bold"))) + 40
    _pill(d, 285 - width // 2, 312, data["ticker"], 15, fill=_mix(BLUE, _INK, 0.75), fg=BLUE, pad=20, h=32)
    _text(d, (285, 372), f"RANK {data['rank']} OF {data['listed']}", 12, MUTED, "mm", "bold")
    d.rounded_rectangle((76, 394, 494, 500), radius=22, fill=_mix(GOLD, _INK, 0.88), outline=_mix(GOLD, _INK, 0.45), width=2)
    _text(d, (285, 418), "PRICE PER SHARE", 12, GOLD, "mm", "head")
    label = money(data["price"])
    tw = d.textlength(label, font=font(50, "head"))
    _coin(d, int(285 - tw / 2 - 24), 462, 15)
    _text(d, (285 + 18, 462), label, 50, WHITE, "mm", "head")
    _text(d, (76, 538), "LAST MOVE", 14, MUTED, "lm", "bold")
    _change_pill(d, 494, 524, data["change"], h=30, size=16, right=True)
    _text(d, (76, 586), "SHARES LEFT", 14, MUTED, "lm", "bold")
    _text(d, (494, 586), f"{data['left']} / {data['supply']}", 16, WHITE, "rm", "head")
    d.rounded_rectangle((76, 606, 494, 620), radius=7, fill=_mix(MUTED, _INK, 0.8))
    frac = max(0.0, min(1.0, data["left"] / max(1, data["supply"])))
    if frac > 0:
        d.rounded_rectangle((76, 606, 76 + max(14, int(418 * frac)), 620), radius=7, fill=BLUE)
    _text(d, (76, 640), f"EACH TRADER CAN HOLD UP TO {data['max']}", 12, DIM, "lm", "bold")
    d.line((76, 672, 494, 672), fill=_mix(MUTED, _INK, 0.75), width=1)
    hold = data.get("holding")
    _text(d, (76, 700), "YOU OWN", 14, MUTED, "lm", "bold")
    if hold:
        pl = hold["value"] - hold["cost"]
        _text(d, (494, 700), f"{hold['shares']} SHARES", 18, WHITE, "rm", "head")
        _text(d, (76, 746), "WORTH", 13, MUTED, "lm", "bold")
        _text(d, (494, 746), money(hold["value"]), 18, WHITE, "rm", "head")
        _text(d, (76, 790), "PROFIT / LOSS", 13, MUTED, "lm", "bold")
        _text(d, (494, 790), signed(pl), 20, tone(pl), "rm", "head")
    else:
        _text(d, (494, 700), "NONE YET", 18, DIM, "rm", "head")
        _text(d, (76, 760), "+buy <player> <amount>", 14, MUTED, "lm", "bold")
    games = data["games"]
    facts = [("STRIKE RATE", f"{data['sr']:.0f}", f"LEAGUE {data['sr_par']:.0f}", BLUE),
             ("WICKETS", str(data["wickets"]), f"{data['wpo']:.1f} PER OVER", VIOLET),
             ("ECONOMY", f"{data['eco']:.1f}", f"LEAGUE {data['eco_par']:.1f}", GOLD),
             ("DUCKS", str(data["ducks"]), f"−{money(data['duck_pen'])} EACH", DOWN)]
    for box, (label, value, sub, col) in zip(tiles, facts):
        x1, y1, x2, y2 = box
        _tile_glass(c, box, col)
        _text(d, ((x1 + x2) // 2, y1 + 28), label, 14, col, "mm", "head")
        _text(d, ((x1 + x2) // 2, y1 + 80), value, _fit(d, value, 200, 52, 26, "head"), WHITE, "mm", "head")
        _text(d, ((x1 + x2) // 2, y1 + 126), sub, 12, MUTED, "mm", "bold")
    _tile_glass(c, (545, 186, 1550, 528), accent)
    _text(d, (580, 222), "WHAT MOVES THE PRICE", 18, accent, "lm", "head")
    _text(d, (1520, 222), f"{games} GAMES PLAYED", 13, DIM, "rm", "bold")
    rows = [("Starting price", data["base"], None)] + [(lab, val, val) for lab, val in data["factors"]]
    for i, (lab, val, sign) in enumerate(rows):
        y = 262 + i * 42
        _text(d, (584, y + 18), lab, 19, WHITE if sign is not None else MUTED, "lm", "bold")
        if sign is None:
            _text(d, (1516, y + 18), money(val), 20, MUTED, "rm", "head")
        else:
            _text(d, (1516, y + 18), signed(val), 20, tone(val), "rm", "head")
        d.line((580, y + 38, 1520, y + 38), fill=_mix(MUTED, _INK, 0.82), width=1)
    _tile_glass(c, (545, 544, 1550, 852), BLUE)
    last = data["last"]
    _text(d, (580, 578), f"PRICE • LAST {len(last)} GAMES" if last else "PRICE HISTORY", 18, BLUE, "lm", "head")
    if last:
        _last_games(c, (545, 544, 1550, 852), last, accent)
    else:
        _text(d, (1047, 700), "NO GAMES YET", 22, DIM, "mm", "head")
    _footer(c, f"GOOGLI MANAGER  •  {data['ticker']}  •  PRICE FOLLOWS MATCH STATS", data.get("guild"))
    return c.image


def render_stock_png(data):
    return _png(render_stock(data))


# =========================================================================== portfolio (8 holdings a page)

def render_portfolio(data):
    c = _Canvas(GOLD, BLUE)
    c.card((50, 22, 1550, 190), 26, 168)
    c.card((50, 206, 1550, 852), 26, 150)
    d = c.seal()
    _tile_glass(c, (50, 22, 1550, 190), GOLD)
    user = data["user"]
    ring = _round_image(_open(user.get("avatar")), 112, ring=GOLD, ring_w=5, initial=user["name"][:1])
    _paste(c.image, ring, (80, 106 - ring.size[1] // 2))
    _text(d, (214, 62), "PORTFOLIO", 13, MUTED, "lm", "bold")
    name = user["name"].upper()
    size = _fit(d, name, 330, 34, 18, "head")
    _text(d, (214, 96), _clip(d, name, 330, size, "head"), size, WHITE, "lm", "head")
    _text(d, (214, 134), f"{data['count']} PLAYERS  •  {data['shares']} SHARES", 14, GOLD, "lm", "head")
    _text(d, (214, 160), f"CASH {money(data['cash'])}  •  RANK {data['rank']} OF {data['players']}", 12, MUTED, "lm", "bold")
    stats = [("WORTH NOW", money(data["worth"]), WHITE), ("PAID", money(data["paid"]), BLUE),
             ("PROFIT / LOSS", signed(data["pl"]), tone(data["pl"]))]
    for i, (label, value, col) in enumerate(stats):
        x1 = 640 + i * 300
        d.rounded_rectangle((x1, 50, x1 + 280, 160), radius=22, fill=_mix(col, _INK, 0.9), outline=_mix(col, _INK, 0.55), width=2)
        _text(d, (x1 + 22, 78), label, 12, MUTED, "lm", "bold")
        _coin(d, x1 + 34, 124, 10)
        _text(d, (x1 + 54, 124), value, _fit(d, value, 210, 28, 15, "head"), col, "lm", "head")
    _tile_glass(c, (50, 206, 1550, 852), BLUE)
    for label, x, anchor in (("PLAYER", 120, "lm"), ("SHARES", 760, "rm"), ("PRICE", 960, "rm"), ("VALUE", 1190, "rm"),
                             ("PROFIT / LOSS", 1486, "rm")):
        _text(d, (x, 236), label, 13, MUTED, anchor, "bold")
    d.line((80, 256, 1520, 256), fill=_mix(MUTED, _INK, 0.75), width=1)
    rows = data["rows"]
    if not rows:
        _text(d, (800, 520), "NO SHARES YET", 28, DIM, "mm", "head")
        _text(d, (800, 566), "BUY ONE WITH  +buy <player> <amount>", 17, MUTED, "mm", "bold")
    for i, row in enumerate(rows[:8]):
        y = 266 + i * 72
        if i % 2 == 0:
            d.rounded_rectangle((70, y, 1530, y + 66), radius=18, fill=_mix(BLUE, _INK, 0.93))
        ring = _round_image(_open(row.get("avatar")), 44, ring=GOLD, ring_w=3, initial=(row.get("name") or "?")[:1])
        _paste(c.image, ring, (90, y + 33 - ring.size[1] // 2))
        label = (row.get("name") or "?").upper()
        size = _fit(d, label, 420, 22, 12, "head")
        _text(d, (150, y + 23), _clip(d, label, 420, size, "head"), size, WHITE, "lm", "head")
        _pill(d, 150, y + 40, row["ticker"], 10, fill=_mix(BLUE, _INK, 0.75), fg=BLUE, pad=8, h=19)
        full = row["shares"] >= data["max"]
        _text(d, (760, y + 33), f"{row['shares']} / {data['max']}", 22, GOLD if full else WHITE, "rm", "head")
        _text(d, (960, y + 33), money(row["price"]), 22, MUTED, "rm", "head")
        _text(d, (1190, y + 33), money(row["value"]), 24, WHITE, "rm", "head")
        col = tone(row["pl"])
        d.rounded_rectangle((1290, y + 14, 1486, y + 52), radius=19, fill=_mix(col, _INK, 0.78), outline=col, width=2)
        if row["pl"]:
            _arrow(d, 1310, y + 30, row["pl"] > 0, col, 7)
        _text(d, (1470, y + 33), money(abs(row["pl"])), 20, col, "rm", "head")
    _footer(c, f"GOOGLI MANAGER  •  PAGE {data['page']} OF {data['pages']}  •  PORTFOLIO", data.get("guild"))
    return c.image


def render_portfolio_png(data):
    return _png(render_portfolio(data))
