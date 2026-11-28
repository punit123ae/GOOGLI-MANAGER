"""TEST version of the Player Stock Market (new design). The main +stocks cog is NOT touched.

Everything here lives in its own tables (tstock_*) and its own commands (+test...), so you can try it on the
live server next to the old market. Delete this file, teststocks_image.py and the line in bot.py to remove it.

For players
  +teststock <player>              one stock: price, what moves it, last 5 games  (alias +tstock)
  +testmarket [page] [sort]        the market, 10 players a page, buttons + sort menu  (alias +tmarket)
  +testportfolio [@user]           shares, paid, worth now, profit/loss, 8 a page  (alias +tport)
  +testbuy <player> <amount|all>   +testsell <player> <amount|all>
  +testwallet [@user]              HCG balance            +testtraders   richest traders
  +testtrades [@user]              last trades

For admins (owner, or a role whitelisted for that command name)
  +teststockset                    show every setting;  +teststockset <setting> <value|reset>
  +teststockplayer <player> base <price> | supply <n> | freeze | unfreeze | reset
  +teststockwallet <add|remove|set> @user <amount>
  +teststockmarket <open|close>    +teststockreset confirm

How the price is made (all numbers are settings, see +teststockset)
  price = base price
        + runs x per_run + wickets x per_wicket
        + (strike rate - sr_par) x sr_weight            batting quality
        + wickets per over x wpo_weight                  bowling quality
        + (econ_par - economy) x econ_weight             economy trim (can be positive or negative)
        - ducks x duck_penalty - expensive spells x expensive_penalty
The price after every game is stored, so the card can show the last five moves. Prices only change when match
stats are imported, so there is no background work.
"""
import asyncio
import io
import math
import re
import time

import discord
from discord.ext import commands

from .h2h import fetch_avatar, on_cooldown, render_png
from .utility import is_bot_owner_id
from . import teststocks_image as img

CURRENCY = "HCG"
MARKET_PAGE = 10
PORTFOLIO_PAGE = 8

# key: (default, minimum, maximum, description)
SETTINGS = {
    "base_price": (30000, 1, 10_000_000, "Starting price of every player"),
    "supply": (30, 1, 1000, "Shares that exist per player"),
    "max_per_user": (5, 1, 1000, "Most shares of one player a user can hold"),
    "sell_fee": (2.0, 0, 50, "Fee when selling, in percent"),
    "buy_fee": (0.0, 0, 50, "Fee when buying, in percent"),
    "per_run": (30, 0, 100000, "Price added per run scored"),
    "per_wicket": (375, 0, 1000000, "Price added per wicket taken"),
    "sr_par": (100, 0, 1000, "Strike rate that counts as neutral"),
    "sr_weight": (0.5, 0, 10000, "Price per strike-rate point above/below par"),
    "wpo_weight": (1750, 0, 1000000, "Price per 1.0 wickets-per-over"),
    "econ_par": (17.0, 0, 100, "Economy that counts as neutral"),
    "econ_weight": (100, 0, 100000, "Price per economy point below/above par"),
    "duck_penalty": (1200, 0, 1000000, "Price taken per duck"),
    "expensive_econ": (24.0, 0, 200, "A spell worse than this economy is 'expensive'"),
    "expensive_penalty": (750, 0, 1000000, "Price taken per expensive spell (min 2 overs)"),
    "min_price": (5000, 1, 10_000_000, "A price never falls below this"),
    "max_price": (250000, 1, 100_000_000, "A price never rises above this"),
    "start_cash": (0, 0, 1_000_000_000, f"{CURRENCY} given to a wallet the first time it is used"),
    "market_open": (1, 0, 1, "1 = trading open, 0 = closed (also +teststockmarket)"),
}
ALIASES = {
    "base": "base_price", "baseprice": "base_price", "price": "base_price",
    "shares": "supply", "stock": "supply", "max": "max_per_user", "limit": "max_per_user", "maxper": "max_per_user",
    "fee": "sell_fee", "sellfee": "sell_fee", "buyfee": "buy_fee",
    "run": "per_run", "runs": "per_run", "wicket": "per_wicket", "wickets": "per_wicket",
    "duck": "duck_penalty", "ducks": "duck_penalty", "srweight": "sr_weight", "srpar": "sr_par",
    "wpo": "wpo_weight", "econweight": "econ_weight", "econpar": "econ_par", "min": "min_price", "minprice": "min_price",
    "maxprice": "max_price", "cash": "start_cash", "startcash": "start_cash", "open": "market_open",
}
MARKET_SORTS = {"price": "Highest price", "gainers": "Top gainers", "losers": "Top losers", "cheap": "Lowest price",
                "left": "Most shares left", "scarce": "Fewest shares left"}
MARKET_SORT_WORDS = {"top": "price", "high": "price", "gain": "gainers", "up": "gainers", "loss": "losers",
                     "down": "losers", "low": "cheap", "cheapest": "cheap", "most": "left", "least": "scarce"}
PORT_SORTS = {"value": "Biggest value", "profit": "Most profit", "loss": "Biggest loss", "name": "Name A-Z"}


# =========================================================================== pure price model
def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def _components(runs, bf, rc, bb, wk, ducks, exp, cfg):
    """The six lines of 'what moves the price', as whole numbers."""
    sr = runs / bf * 100 if bf else 0.0
    overs = bb / 6 if bb else 0.0
    wpo = wk / overs if bb else 0.0
    econ = rc / overs if bb else 0.0
    bat_sample = min(1.0, bf / 60)            # small samples count for less
    bowl_sample = min(1.0, bb / 36)
    return {
        "runs": round(runs * cfg["per_run"]),
        "wickets": round(wk * cfg["per_wicket"]),
        "sr": round((sr - cfg["sr_par"]) * cfg["sr_weight"] * bat_sample) if bf else 0,
        "wpo": round(wpo * cfg["wpo_weight"] * bowl_sample) if bb else 0,
        "econ": round((cfg["econ_par"] - econ) * cfg["econ_weight"] * bowl_sample) if bb else 0,
        "pen": -round(ducks * cfg["duck_penalty"] + exp * cfg["expensive_penalty"]),
    }, sr, wpo, econ


def build_stock(games, cfg, base):
    """games: oldest first, dicts with runs, balls_faced, runs_conceded, balls_bowled, wickets, not_out."""
    runs = bf = rc = bb = wk = ducks = exp = 0
    history, deltas, comps = [], [], None
    sr = wpo = econ = 0.0
    prev = base
    for g in games:
        runs += g["runs"]; bf += g["balls_faced"]; rc += g["runs_conceded"]; bb += g["balls_bowled"]; wk += g["wickets"]
        if g["balls_faced"] > 0 and g["runs"] == 0 and not g["not_out"]:
            ducks += 1
        if g["balls_bowled"] >= 12 and g["runs_conceded"] * 6 / g["balls_bowled"] > cfg["expensive_econ"]:
            exp += 1
        comps, sr, wpo, econ = _components(runs, bf, rc, bb, wk, ducks, exp, cfg)
        price = int(_clamp(base + sum(comps.values()), cfg["min_price"], cfg["max_price"]))
        history.append(price)
        deltas.append(price - prev)
        prev = price
    price = history[-1] if history else int(base)
    before = history[-2] if len(history) > 1 else int(base)
    return {"price": price, "prev": before, "delta": price - before,
            "change": (price - before) / before * 100 if before else 0.0,
            "history": history, "deltas": deltas, "games": len(games), "base": int(base), "comps": comps or {},
            "runs": runs, "wickets": wk, "sr": sr, "wpo": wpo, "econ": econ, "bf": bf, "bb": bb,
            "ducks": ducks, "expensive": exp}


def parse_amount(text):
    """'30000', '30,000', '30k', '1.5m' -> float, or None."""
    t = str(text).strip().lower().replace(",", "").replace("_", "")
    mult = 1
    if t.endswith("k"):
        mult, t = 1_000, t[:-1]
    elif t.endswith("m"):
        mult, t = 1_000_000, t[:-1]
    try:
        return float(t) * mult
    except ValueError:
        return None


def tier(value, steps):
    for limit, word in steps:
        if value >= limit:
            return word
    return steps[-1][1]


def sr_tag(sr):
    return tier(sr, ((400, "elite"), (300, "great"), (200, "decent"), (100, "okay"), (-1, "weak")))


def econ_tag(e):
    for limit, word in ((10, "elite"), (14, "great"), (17, "good"), (20, "okay")):
        if e <= limit:
            return word
    return "poor"


# =========================================================================== paging view
class GoToPage(discord.ui.Modal, title="Go to page"):
    page = discord.ui.TextInput(label="Page number", placeholder="e.g. 12", max_length=4)

    def __init__(self, view):
        super().__init__()
        self.view = view

    async def on_submit(self, interaction):
        try:
            number = int(str(self.page.value).strip())
        except ValueError:
            return await interaction.response.send_message("Type a page number.", ephemeral=True)
        await self.view.go(interaction, page=number)


class PagedView(discord.ui.View):
    """Image pages with first/prev/next/last, a jump button and an optional sort menu.
    draw(page, sort) -> (png bytes, total pages)."""

    def __init__(self, ctx, draw, pages, page=1, sorts=None, sort=None, filename="page.png"):
        super().__init__(timeout=240)
        self.owner_id = ctx.author.id
        self.draw, self.pages, self.page, self.sort, self.filename = draw, pages, page, sort, filename
        self.message = None
        self._busy = False
        sec = discord.ButtonStyle.secondary
        self.first = discord.ui.Button(emoji="⏮", style=sec, row=0)
        self.prev = discord.ui.Button(emoji="◀", style=sec, row=0)
        self.label = discord.ui.Button(style=discord.ButtonStyle.primary, row=0, disabled=True)
        self.next = discord.ui.Button(emoji="▶", style=sec, row=0)
        self.last = discord.ui.Button(emoji="⏭", style=sec, row=0)
        self.jump = discord.ui.Button(label="Go to", emoji="🔢", style=sec, row=0)
        self.first.callback = self._cb(lambda: 1)
        self.prev.callback = self._cb(lambda: self.page - 1)
        self.next.callback = self._cb(lambda: self.page + 1)
        self.last.callback = self._cb(lambda: self.pages)
        self.jump.callback = self._jump
        for b in (self.first, self.prev, self.label, self.next, self.last, self.jump):
            self.add_item(b)
        self.select = None
        if sorts:
            self.select = discord.ui.Select(
                placeholder="Sort by…", row=1,
                options=[discord.SelectOption(label=name, value=key, default=key == sort) for key, name in sorts.items()])
            self.select.callback = self._sorted
            self.add_item(self.select)
        self._sync()

    def _sync(self):
        self.label.label = f"{self.page} / {self.pages}"
        self.first.disabled = self.prev.disabled = self.page <= 1
        self.next.disabled = self.last.disabled = self.page >= self.pages
        self.jump.disabled = self.pages <= 2
        if self.select is not None:
            for opt in self.select.options:
                opt.default = opt.value == self.sort

    def _cb(self, target):
        async def callback(interaction):
            await self.go(interaction, page=target())
        return callback

    async def _jump(self, interaction):
        await interaction.response.send_modal(GoToPage(self))

    async def _sorted(self, interaction):
        await self.go(interaction, sort=self.select.values[0])

    async def go(self, interaction, page=None, sort=None):
        if self._busy:                                   # ignore button mashing while a page is being drawn
            return await interaction.response.defer()
        self._busy = True
        try:
            await interaction.response.defer()
            old = (self.page, self.sort)
            if sort is not None:
                self.sort, self.page = sort, 1
            elif page is not None:
                self.page = max(1, min(self.pages, page))
            try:
                png, self.pages = await self.draw(self.page, self.sort)
                self.page = max(1, min(self.page, self.pages))
            except Exception as exc:
                import traceback
                traceback.print_exc()
                print(f"[teststocks] page failed: {type(exc).__name__}: {exc}", flush=True)
                self.page, self.sort = old
                self._sync()
                return await interaction.followup.send("I couldn't draw that page. Check the bot logs.", ephemeral=True)
            self._sync()
            await interaction.edit_original_response(
                attachments=[discord.File(io.BytesIO(png), filename=self.filename)], view=self)
        finally:
            self._busy = False

    async def interaction_check(self, interaction):
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message("Only the person who ran the command can use these buttons. "
                                                "Run it yourself to get your own.", ephemeral=True)
        return False

    async def on_timeout(self):
        if self.message is not None:
            try:
                await self.message.edit(view=None)
            except Exception:
                pass


# =========================================================================== the cog
class TestStocks(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._lock = asyncio.Lock()
        self._ver = 0                        # bumped whenever a setting or per-player override changes
        self._cfg_cache = {}
        self._market_cache = {}
        self._avatar_cache = {}
        db = bot.db
        db.execute("CREATE TABLE IF NOT EXISTS tstock_settings (guild_id INTEGER NOT NULL, key TEXT NOT NULL, "
                   "value TEXT NOT NULL, PRIMARY KEY (guild_id, key))")
        db.execute("CREATE TABLE IF NOT EXISTS tstock_players (guild_id INTEGER NOT NULL, player_id INTEGER NOT NULL, "
                   "base_price INTEGER, supply INTEGER, frozen INTEGER NOT NULL DEFAULT 0, "
                   "PRIMARY KEY (guild_id, player_id))")
        db.execute("CREATE TABLE IF NOT EXISTS tstock_wallets (guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, "
                   "cash INTEGER NOT NULL, PRIMARY KEY (guild_id, user_id))")
        db.execute("CREATE TABLE IF NOT EXISTS tstock_holdings (guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, "
                   "player_id INTEGER NOT NULL, shares INTEGER NOT NULL, cost INTEGER NOT NULL, "
                   "PRIMARY KEY (guild_id, user_id, player_id))")
        db.execute("CREATE TABLE IF NOT EXISTS tstock_trades (id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, "
                   "user_id INTEGER NOT NULL, player_id INTEGER NOT NULL, side TEXT NOT NULL, shares INTEGER NOT NULL, "
                   "price INTEGER NOT NULL, fee INTEGER NOT NULL, ts REAL NOT NULL)")
        db.commit()

    # ------------------------------------------------------------------ access
    async def _allowed(self, ctx):
        name = ctx.command.name
        if is_bot_owner_id(self.bot, ctx.author.id) or await self.bot.is_owner(ctx.author):
            return True
        try:
            from .utility import owner_or_whitelisted
            if ctx.guild is not None and await owner_or_whitelisted(ctx, name):
                return True
        except Exception:
            pass
        await ctx.send(f"❌ Only the bot owner or a role whitelisted for `+{name}` can use this test command.")
        return False

    # ------------------------------------------------------------------ settings
    def _cfg(self, guild_id):
        hit = self._cfg_cache.get(int(guild_id))
        if hit is not None:
            return hit
        cfg = {k: v[0] for k, v in SETTINGS.items()}
        for row in self.bot.db.execute("SELECT key, value FROM tstock_settings WHERE guild_id=?", (int(guild_id),)):
            if row["key"] in SETTINGS:
                try:
                    num = float(row["value"])
                    cfg[row["key"]] = int(num) if isinstance(SETTINGS[row["key"]][0], int) else num
                except ValueError:
                    pass
        self._cfg_cache[int(guild_id)] = cfg
        return cfg

    def _changed(self):
        self._ver += 1
        self._cfg_cache.clear()
        self._market_cache.clear()

    # ------------------------------------------------------------------ market data
    def _market(self, guild_id):
        """{uid: stock dict} for every player with stats; rebuilt only when stats or settings changed."""
        db, gid = self.bot.db, int(guild_id)
        row = db.execute("SELECT COUNT(*), COALESCE(MAX(imported_at), '') FROM stats_imports WHERE guild_id=?",
                         (gid,)).fetchone()
        version = (row[0], str(row[1]), self._ver)
        hit = self._market_cache.get(gid)
        if hit and hit[0] == version:
            return hit[1]
        cfg = self._cfg(gid)
        over = {int(r["player_id"]): r for r in db.execute(
            "SELECT player_id, base_price, supply, frozen FROM tstock_players WHERE guild_id=?", (gid,))}
        rows = db.execute(
            "SELECT p.user_id,p.runs,p.balls_faced,p.runs_conceded,p.balls_bowled,p.wickets,p.not_out "
            "FROM player_match_stats p JOIN stats_imports i ON i.source_message_id=p.source_message_id "
            "WHERE i.guild_id=? AND COALESCE(i.competition,'MAIN TOURNAMENT')='MAIN TOURNAMENT' "
            "ORDER BY i.imported_at, p.source_message_id", (gid,)).fetchall()
        games = {}
        for r in rows:
            games.setdefault(int(r["user_id"]), []).append(
                {"runs": int(r["runs"]), "balls_faced": int(r["balls_faced"]), "runs_conceded": int(r["runs_conceded"]),
                 "balls_bowled": int(r["balls_bowled"]), "wickets": int(r["wickets"]), "not_out": int(r["not_out"])})
        market = {}
        for uid, played in games.items():
            o = over.get(uid)
            base = o["base_price"] if o is not None and o["base_price"] is not None else cfg["base_price"]
            stock = build_stock(played, cfg, base)
            stock["supply"] = int(o["supply"]) if o is not None and o["supply"] is not None else int(cfg["supply"])
            stock["frozen"] = bool(o["frozen"]) if o is not None else False
            market[uid] = stock
        self._market_cache = {gid: (version, market)}
        return market

    def _held_totals(self, guild_id):
        return {int(r[0]): int(r[1]) for r in self.bot.db.execute(
            "SELECT player_id, SUM(shares) FROM tstock_holdings WHERE guild_id=? GROUP BY player_id", (int(guild_id),))}

    @staticmethod
    def _left(stock, held):
        return 0 if stock["frozen"] else max(0, stock["supply"] - held)

    def _holdings(self, guild_id, user_id):
        return self.bot.db.execute("SELECT player_id, shares, cost FROM tstock_holdings WHERE guild_id=? AND user_id=? "
                                   "AND shares>0", (int(guild_id), int(user_id))).fetchall()

    def _wallet(self, guild_id, user_id):
        cfg = self._cfg(guild_id)
        self.bot.db.execute("INSERT OR IGNORE INTO tstock_wallets (guild_id, user_id, cash) VALUES (?,?,?)",
                            (int(guild_id), int(user_id), int(cfg["start_cash"])))
        return int(self.bot.db.execute("SELECT cash FROM tstock_wallets WHERE guild_id=? AND user_id=?",
                                       (int(guild_id), int(user_id))).fetchone()[0])

    # ------------------------------------------------------------------ names, search, avatars
    def _name(self, guild, uid):
        name = ""
        compare = self.bot.get_cog("CompareTeams")
        if compare is not None:
            try:
                name = str(compare._display_name(guild, uid) or "")
            except Exception:
                name = ""
        member = guild.get_member(int(uid))
        if not name or re.fullmatch(r"Player \d{3,5}", name):
            name = member.display_name if member else ""
        name = re.sub(r"[\U00010000-\U0010ffff\u200d\ufe0f]", "", name).strip()     # emoji draw as empty boxes
        return name or "Player left"

    def _find(self, guild, market, query):
        query = query.strip()
        if not query:
            return None, "Tell me which player."
        m = re.match(r"^<@!?(\d+)>$", query) or re.match(r"^(\d{15,25})$", query)
        if m:
            uid = int(m.group(1))
            return (uid, None) if uid in market else (None, "That player has no match stats yet, so there's no stock.")
        needle = query.casefold().lstrip("@")
        names = {u: self._name(guild, u).casefold() for u in market}
        exact = [u for u in market if names[u] == needle]
        if len(exact) == 1:
            return exact[0], None
        loose = [u for u in market if needle in names[u]]
        if len(loose) == 1:
            return loose[0], None
        if not loose and not exact:
            return None, f"I can't find a listed player matching `{query}`."
        pool = exact or loose
        listing = ", ".join(self._name(guild, u) for u in pool[:6])
        return None, f"That matches several players: {listing}. Be more specific or mention them."

    async def _avatars(self, guild, uids, allow_fetch=False):
        out, need, now = {}, [], time.time()
        for uid in set(uids):
            hit = self._avatar_cache.get(uid)
            if hit and now - hit[0] < 900:
                out[uid] = hit[1]
            else:
                need.append(uid)
        sem = asyncio.Semaphore(5)

        async def one(uid):
            async with sem:
                member = guild.get_member(uid) or self.bot.get_user(uid)
                if member is None and allow_fetch:
                    try:
                        member = await self.bot.fetch_user(uid)
                    except discord.HTTPException:
                        member = None
                blob = await fetch_avatar(self.bot, member) if member is not None else None
                out[uid] = blob
                if blob is not None:
                    if len(self._avatar_cache) > 300:
                        self._avatar_cache.pop(next(iter(self._avatar_cache)))
                    self._avatar_cache[uid] = (time.time(), blob)

        await asyncio.gather(*(one(u) for u in need), return_exceptions=True)
        return out

    async def _render(self, fn, data):
        async with self._lock:
            return await render_png(fn, data)

    # ------------------------------------------------------------------ +teststock
    @commands.command(name="teststock", aliases=["tstock"])
    @commands.guild_only()
    async def teststock(self, ctx, *, text: str = ""):
        """Test: one player's stock card. Usage: +teststock <player>"""
        if not await self._allowed(ctx):
            return
        guild = ctx.guild
        market = self._market(guild.id)
        if not market:
            return await ctx.reply("The market is empty: it opens once match stats are imported.", mention_author=False)
        text = text.strip()
        if not text:
            if ctx.author.id not in market:
                return await ctx.reply("Usage: `+teststock <player>`", mention_author=False)
            uid, error = ctx.author.id, None
        else:
            uid, error = self._find(guild, market, text)
        if error:
            return await ctx.reply(error, mention_author=False, allowed_mentions=discord.AllowedMentions.none())
        wait = on_cooldown(ctx.author.id)
        if wait:
            return await ctx.reply(f"Please wait {wait:.0f}s before drawing another image.", mention_author=False)
        async with ctx.typing():
            try:
                data = await self._stock_data(ctx, market, uid)
                png = await self._render(img.render_stock, data)
                await ctx.send(file=discord.File(io.BytesIO(png), filename="stock.png"))
            except Exception as exc:
                import traceback
                traceback.print_exc()
                print(f"[teststocks] stock failed: {type(exc).__name__}: {exc}", flush=True)
                await ctx.reply("I couldn't draw the stock. Check the bot logs.", mention_author=False)

    async def _stock_data(self, ctx, market, uid):
        guild, cfg, s = ctx.guild, self._cfg(ctx.guild.id), market[uid]
        held = self._held_totals(guild.id).get(uid, 0)
        mine = next((int(h["shares"]) for h in self._holdings(guild.id, ctx.author.id) if int(h["player_id"]) == uid), 0)
        c = s["comps"]
        last = s["history"][-5:]
        deltas = s["deltas"][-5:]
        first_game = s["games"] - len(last) + 1
        avatars = await self._avatars(guild, {uid}, allow_fetch=True)
        sr_text = f"{s['sr']:.0f}" if s["bf"] else "—"
        tiles = [("STRIKE RATE", sr_text, sr_tag(s["sr"]) if s["bf"] else "no batting yet"),
                 ("WICKETS", str(s["wickets"]), f"{s['wpo']:.1f} / over" if s["bb"] else "no overs yet"),
                 ("ECONOMY", f"{s['econ']:.1f}" if s["bb"] else "—", econ_tag(s["econ"]) if s["bb"] else "no overs yet"),
                 ("DUCKS", str(s["ducks"]), f"−{int(cfg['duck_penalty']):,} each")]
        return {"name": self._name(guild, uid), "avatar": avatars.get(uid), "games": s["games"], "price": s["price"],
                "change": s["change"], "left": self._left(s, held), "supply": s["supply"],
                "max_each": int(cfg["max_per_user"]), "owned": mine, "tiles": tiles, "base": s["base"],
                "breakdown": [("Runs scored", c.get("runs", 0)), ("Wickets taken", c.get("wickets", 0)),
                              ("Batting (strike rate)", c.get("sr", 0)), ("Bowling (wickets / over)", c.get("wpo", 0)),
                              ("Economy trim", c.get("econ", 0)), ("Ducks & bowling penalties", c.get("pen", 0))],
                "history": [(first_game + i, p, d) for i, (p, d) in enumerate(zip(last, deltas))]}

    # ------------------------------------------------------------------ +testmarket
    @commands.command(name="testmarket", aliases=["tmarket"])
    @commands.guild_only()
    async def testmarket(self, ctx, *, text: str = ""):
        """Test: the market with page buttons. Usage: +testmarket [page] [price|gainers|losers|cheap|left|scarce]"""
        if not await self._allowed(ctx):
            return
        guild = ctx.guild
        if not self._market(guild.id):
            return await ctx.reply("The market is empty: it opens once match stats are imported.", mention_author=False)
        wait = on_cooldown(ctx.author.id)
        if wait:
            return await ctx.reply(f"Please wait {wait:.0f}s before drawing another image.", mention_author=False)
        page, sort = 1, "price"
        for token in text.casefold().split():
            if token.isdigit():
                page = max(1, int(token))
            elif token in MARKET_SORTS:
                sort = token
            elif token in MARKET_SORT_WORDS:
                sort = MARKET_SORT_WORDS[token]

        async def draw(pg, srt):
            return await self._draw_market(guild, pg, srt or "price")

        async with ctx.typing():
            try:
                png, pages = await draw(page, sort)
            except Exception as exc:
                import traceback
                traceback.print_exc()
                print(f"[teststocks] market failed: {type(exc).__name__}: {exc}", flush=True)
                return await ctx.reply("I couldn't draw the market. Check the bot logs.", mention_author=False)
        view = PagedView(ctx, draw, pages, page=min(page, pages), sorts=MARKET_SORTS, sort=sort, filename="market.png")
        view.message = await ctx.send(file=discord.File(io.BytesIO(png), filename="market.png"),
                                      view=view)

    async def _draw_market(self, guild, page, sort):
        market = self._market(guild.id)
        held = self._held_totals(guild.id)
        left = {u: self._left(s, held.get(u, 0)) for u, s in market.items()}
        keys = {"price": lambda u: (-market[u]["price"], u), "gainers": lambda u: (-market[u]["change"], u),
                "losers": lambda u: (market[u]["change"], u), "cheap": lambda u: (market[u]["price"], u),
                "left": lambda u: (-left[u], u), "scarce": lambda u: (left[u], u)}
        order = sorted(market, key=keys.get(sort, keys["price"]))
        pages = max(1, math.ceil(len(order) / MARKET_PAGE))
        page = max(1, min(pages, page))
        start = (page - 1) * MARKET_PAGE
        chunk = order[start:start + MARKET_PAGE]
        avatars = await self._avatars(guild, chunk)
        rows = [{"rank": start + i + 1, "name": self._name(guild, u), "avatar": avatars.get(u), "price": market[u]["price"],
                 "change": market[u]["change"], "left": left[u], "supply": market[u]["supply"]}
                for i, u in enumerate(chunk)]
        return await self._render(img.render_market, {"page": page, "pages": pages, "rows": rows}), pages

    # ------------------------------------------------------------------ +testportfolio
    @commands.command(name="testportfolio", aliases=["tport", "testport"])
    @commands.guild_only()
    async def testportfolio(self, ctx, member: discord.Member = None):
        """Test: your portfolio (or another player's) with page buttons."""
        if not await self._allowed(ctx):
            return
        guild, member = ctx.guild, member or ctx.author
        market = self._market(guild.id)
        if not market:
            return await ctx.reply("The market is empty: it opens once match stats are imported.", mention_author=False)
        wait = on_cooldown(ctx.author.id)
        if wait:
            return await ctx.reply(f"Please wait {wait:.0f}s before drawing another image.", mention_author=False)

        async def draw(pg, srt):
            return await self._draw_portfolio(guild, member, pg, srt or "value")

        async with ctx.typing():
            try:
                png, pages = await draw(1, "value")
            except Exception as exc:
                import traceback
                traceback.print_exc()
                print(f"[teststocks] portfolio failed: {type(exc).__name__}: {exc}", flush=True)
                return await ctx.reply("I couldn't draw the portfolio. Check the bot logs.", mention_author=False)
        view = PagedView(ctx, draw, pages, sorts=PORT_SORTS, sort="value", filename="portfolio.png")
        view.message = await ctx.send(file=discord.File(io.BytesIO(png), filename="portfolio.png"), view=view)

    async def _draw_portfolio(self, guild, member, page, sort):
        market, cfg = self._market(guild.id), self._cfg(guild.id)
        rows = []
        for h in self._holdings(guild.id, member.id):
            uid, shares, cost = int(h["player_id"]), int(h["shares"]), int(h["cost"])
            price = market[uid]["price"] if uid in market else 0
            rows.append({"uid": uid, "shares": shares, "cost": cost, "price": price, "value": shares * price,
                         "pl": shares * price - cost})
        keys = {"value": lambda r: (-r["value"], r["uid"]), "profit": lambda r: (-r["pl"], r["uid"]),
                "loss": lambda r: (r["pl"], r["uid"]), "name": lambda r: (self._name(guild, r["uid"]).casefold(), r["uid"])}
        rows.sort(key=keys.get(sort, keys["value"]))
        pages = max(1, math.ceil(len(rows) / PORTFOLIO_PAGE))
        page = max(1, min(pages, page))
        chunk = rows[(page - 1) * PORTFOLIO_PAGE: page * PORTFOLIO_PAGE]
        avatars = await self._avatars(guild, [r["uid"] for r in chunk] + [member.id])
        worth, paid = sum(r["value"] for r in rows), sum(r["cost"] for r in rows)
        data = {"user": {"name": re.sub(r"[\U00010000-\U0010ffff\u200d\ufe0f]", "", member.display_name).strip() or member.name,
                         "avatar": avatars.get(member.id)},
                "players": len(rows), "shares": sum(r["shares"] for r in rows), "worth": worth, "paid": paid,
                "pl": worth - paid, "page": page, "pages": pages, "fee_pct": cfg["sell_fee"],
                "rows": [{"name": self._name(guild, r["uid"]), "avatar": avatars.get(r["uid"]), "shares": r["shares"],
                          "max": int(cfg["max_per_user"]), "price": r["price"], "value": r["value"], "pl": r["pl"]}
                         for r in chunk]}
        return await self._render(img.render_portfolio, data), pages

    # ------------------------------------------------------------------ +testbuy / +testsell
    @staticmethod
    def _parse_trade(text, verb):
        parts = text.split()
        if len(parts) < 2:
            return None, None, f"Usage: `+test{verb} <player> <amount|all>`, for example `+test{verb} Arpit 2`."
        amount = parts[-1].casefold()
        if amount in ("all", "max"):
            return " ".join(parts[:-1]), "all", None
        if not amount.isdigit() or int(amount) <= 0:
            return None, None, f"The amount must be a whole number or `all`. Example: `+test{verb} Arpit 2`."
        return " ".join(parts[:-1]), int(amount), None

    @staticmethod
    def _fee(amount, pct):
        return int(round(amount * pct / 100)) if pct else 0

    @commands.command(name="testbuy")
    @commands.guild_only()
    async def testbuy(self, ctx, *, text: str = ""):
        """Test: buy shares. Usage: +testbuy <player> <amount|all>"""
        if not await self._allowed(ctx):
            return
        query, amount, error = self._parse_trade(text, "buy")
        if error:
            return await ctx.reply(error, mention_author=False)
        guild, db, none = ctx.guild, self.bot.db, discord.AllowedMentions.none()
        cfg, market = self._cfg(guild.id), self._market(guild.id)
        if not cfg["market_open"]:
            return await ctx.reply("🔒 The market is closed right now.", mention_author=False)
        if not market:
            return await ctx.reply("The market is empty: it opens once match stats are imported.", mention_author=False)
        uid, error = self._find(guild, market, query)
        if error:
            return await ctx.reply(error, mention_author=False, allowed_mentions=none)
        stock, name = market[uid], self._name(guild, uid)
        price, cash = stock["price"], self._wallet(guild.id, ctx.author.id)
        if stock["frozen"]:
            db.commit()
            return await ctx.reply(f"🔒 **{name}** is frozen: no new shares can be bought.", mention_author=False,
                                   allowed_mentions=none)
        held_row = db.execute("SELECT shares FROM tstock_holdings WHERE guild_id=? AND user_id=? AND player_id=?",
                              (guild.id, ctx.author.id, uid)).fetchone()
        owned = int(held_row[0]) if held_row else 0
        left = self._left(stock, self._held_totals(guild.id).get(uid, 0))
        room = int(cfg["max_per_user"]) - owned
        limit = min(room, left)
        if amount == "all":
            amount = limit
            while amount > 0 and price * amount + self._fee(price * amount, cfg["buy_fee"]) > cash:
                amount -= 1
        db.commit()
        if left <= 0:
            return await ctx.reply(f"**{name}** is sold out: every share is already owned.", mention_author=False,
                                   allowed_mentions=none)
        if room <= 0:
            return await ctx.reply(f"You already hold the maximum of **{int(cfg['max_per_user'])}** shares of {name}.",
                                   mention_author=False, allowed_mentions=none)
        if amount <= 0:
            return await ctx.reply(f"You can't afford a share (**{price:,}** {CURRENCY}, your wallet has **{cash:,}**).",
                                   mention_author=False)
        if amount > room:
            return await ctx.reply(f"You can hold at most **{int(cfg['max_per_user'])}** shares of one player "
                                   f"(you have {owned}, so {room} more).", mention_author=False)
        if amount > left:
            return await ctx.reply(f"Only **{left}** share{'s' if left != 1 else ''} of {name} left.", mention_author=False,
                                   allowed_mentions=none)
        cost = price * amount
        fee = self._fee(cost, cfg["buy_fee"])
        if cost + fee > cash:
            return await ctx.reply(f"You need **{cost + fee:,}** {CURRENCY} but your wallet has **{cash:,}**.",
                                   mention_author=False)
        # no await from here to the commit, so nothing else can slip in between
        db.execute("UPDATE tstock_wallets SET cash=cash-? WHERE guild_id=? AND user_id=?", (cost + fee, guild.id, ctx.author.id))
        db.execute("INSERT INTO tstock_holdings (guild_id, user_id, player_id, shares, cost) VALUES (?,?,?,?,?) "
                   "ON CONFLICT(guild_id, user_id, player_id) DO UPDATE SET shares=shares+excluded.shares, "
                   "cost=cost+excluded.cost", (guild.id, ctx.author.id, uid, amount, cost + fee))
        db.execute("INSERT INTO tstock_trades (guild_id, user_id, player_id, side, shares, price, fee, ts) "
                   "VALUES (?,?,?,?,?,?,?,?)", (guild.id, ctx.author.id, uid, "buy", amount, price, fee, time.time()))
        db.commit()
        embed = discord.Embed(title=f"📈 Bought {amount} × {name}", color=0x3ED07C)
        embed.add_field(name="Price each", value=f"{price:,}")
        embed.add_field(name="Total", value=f"{cost:,}" + (f" + {fee:,} fee" if fee else ""))
        embed.add_field(name="Wallet left", value=f"{cash - cost - fee:,}")
        embed.set_footer(text=f"You now hold {owned + amount}/{int(cfg['max_per_user'])} · {left - amount} left to buy")
        await ctx.reply(embed=embed, mention_author=False, allowed_mentions=none)

    @commands.command(name="testsell")
    @commands.guild_only()
    async def testsell(self, ctx, *, text: str = ""):
        """Test: sell shares. Usage: +testsell <player> <amount|all>"""
        if not await self._allowed(ctx):
            return
        query, amount, error = self._parse_trade(text, "sell")
        if error:
            return await ctx.reply(error, mention_author=False)
        guild, db, none = ctx.guild, self.bot.db, discord.AllowedMentions.none()
        cfg, market = self._cfg(guild.id), self._market(guild.id)
        if not cfg["market_open"]:
            return await ctx.reply("🔒 The market is closed right now.", mention_author=False)
        uid, error = self._find(guild, market, query)
        if error:
            return await ctx.reply(error, mention_author=False, allowed_mentions=none)
        name = self._name(guild, uid)
        held = db.execute("SELECT shares, cost FROM tstock_holdings WHERE guild_id=? AND user_id=? AND player_id=?",
                          (guild.id, ctx.author.id, uid)).fetchone()
        owned = int(held["shares"]) if held else 0
        if owned <= 0:
            return await ctx.reply(f"You don't own any **{name}**.", mention_author=False, allowed_mentions=none)
        amount = owned if amount == "all" else amount
        if amount > owned:
            return await ctx.reply(f"You only own **{owned}** share{'s' if owned != 1 else ''} of {name}.",
                                   mention_author=False, allowed_mentions=none)
        price = market[uid]["price"]
        gross = price * amount
        fee = self._fee(gross, cfg["sell_fee"])
        net = gross - fee
        basis = int(round(int(held["cost"]) * amount / owned))
        self._wallet(guild.id, ctx.author.id)
        db.execute("UPDATE tstock_wallets SET cash=cash+? WHERE guild_id=? AND user_id=?", (net, guild.id, ctx.author.id))
        if amount == owned:
            db.execute("DELETE FROM tstock_holdings WHERE guild_id=? AND user_id=? AND player_id=?", (guild.id, ctx.author.id, uid))
        else:
            db.execute("UPDATE tstock_holdings SET shares=shares-?, cost=cost-? WHERE guild_id=? AND user_id=? AND player_id=?",
                       (amount, basis, guild.id, ctx.author.id, uid))
        db.execute("INSERT INTO tstock_trades (guild_id, user_id, player_id, side, shares, price, fee, ts) "
                   "VALUES (?,?,?,?,?,?,?,?)", (guild.id, ctx.author.id, uid, "sell", amount, price, fee, time.time()))
        db.commit()
        profit = net - basis
        cash = self._wallet(guild.id, ctx.author.id)
        db.commit()
        embed = discord.Embed(title=f"{'📈' if profit >= 0 else '📉'} Sold {amount} × {name}",
                              color=0x3ED07C if profit >= 0 else 0xF0525C)
        embed.add_field(name="Price each", value=f"{price:,}")
        embed.add_field(name="You get", value=f"{net:,}" + (f" ({fee:,} fee)" if fee else ""))
        embed.add_field(name="Profit / loss", value=f"{'+' if profit > 0 else '−' if profit < 0 else ''}{abs(profit):,}")
        embed.set_footer(text=f"Wallet: {cash:,} {CURRENCY}")
        await ctx.reply(embed=embed, mention_author=False, allowed_mentions=none)

    # ------------------------------------------------------------------ wallet, traders, trades
    @commands.command(name="testwallet", aliases=["twallet"])
    @commands.guild_only()
    async def testwallet(self, ctx, member: discord.Member = None):
        """Test: HCG balance."""
        if not await self._allowed(ctx):
            return
        member = member or ctx.author
        cash = self._wallet(ctx.guild.id, member.id)
        self.bot.db.commit()
        await ctx.reply(f"💰 **{discord.utils.escape_markdown(member.display_name)}** has **{cash:,}** {CURRENCY} in the "
                        "test stock wallet.", mention_author=False, allowed_mentions=discord.AllowedMentions.none())

    @commands.command(name="testtraders", aliases=["ttraders"])
    @commands.guild_only()
    async def testtraders(self, ctx):
        """Test: richest traders (wallet + value of shares)."""
        if not await self._allowed(ctx):
            return
        guild, db = ctx.guild, self.bot.db
        market = self._market(guild.id)
        worth = {}
        for r in db.execute("SELECT user_id, cash FROM tstock_wallets WHERE guild_id=?", (guild.id,)):
            worth[int(r["user_id"])] = int(r["cash"])
        for r in db.execute("SELECT user_id, player_id, shares FROM tstock_holdings WHERE guild_id=? AND shares>0", (guild.id,)):
            worth[int(r["user_id"])] = worth.get(int(r["user_id"]), 0) + int(r["shares"]) * market.get(int(r["player_id"]), {}).get("price", 0)
        top = sorted(((w, u) for u, w in worth.items() if w > 0), key=lambda x: (-x[0], x[1]))[:10]
        if not top:
            return await ctx.reply("Nobody has traded yet.", mention_author=False)
        wait = on_cooldown(ctx.author.id)
        if wait:
            return await ctx.reply(f"Please wait {wait:.0f}s before drawing another image.", mention_author=False)
        from .predict_image import render_board_png
        async with ctx.typing():
            avatars = await self._avatars(guild, {u for _w, u in top})
            held = {u: len(self._holdings(guild.id, u)) for _w, u in top}
            rows = [{"name": self._name(guild, u), "avatar": avatars.get(u), "points": int(w),
                     "note": f"{held[u]} PLAYERS HELD"} for w, u in top]
            data = {"guild": guild.name, "title": "RICHEST TRADERS", "subtitle": "NET WORTH  •  WALLET + SHARES",
                    "unit": CURRENCY, "footer": "GOOGLI MANAGER  •  TEST STOCK EXCHANGE", "rows": rows}
            png = await self._render(render_board_png, data)
            await ctx.send(file=discord.File(io.BytesIO(png), filename="traders.png"))

    @commands.command(name="testtrades", aliases=["ttrades"])
    @commands.guild_only()
    async def testtrades(self, ctx, member: discord.Member = None):
        """Test: the last 10 trades of you (or another player)."""
        if not await self._allowed(ctx):
            return
        member = member or ctx.author
        rows = self.bot.db.execute("SELECT player_id, side, shares, price, fee, ts FROM tstock_trades WHERE guild_id=? "
                                   "AND user_id=? ORDER BY id DESC LIMIT 10", (ctx.guild.id, member.id)).fetchall()
        if not rows:
            return await ctx.reply("No trades yet.", mention_author=False)
        lines = [f"<t:{int(r['ts'])}:d> {'🟢 BUY ' if r['side'] == 'buy' else '🔴 SELL'} {r['shares']} × "
                 f"**{discord.utils.escape_markdown(self._name(ctx.guild, int(r['player_id'])))}** at {r['price']:,}"
                 + (f" (fee {r['fee']:,})" if r["fee"] else "") for r in rows]
        embed = discord.Embed(title=f"Last trades · {member.display_name}", description="\n".join(lines), color=0x62A0FF)
        await ctx.reply(embed=embed, mention_author=False, allowed_mentions=discord.AllowedMentions.none())

    # ------------------------------------------------------------------ admin: settings
    @commands.command(name="teststockset", aliases=["tstockset"])
    @commands.guild_only()
    async def teststockset(self, ctx, setting: str = None, *, value: str = None):
        """Admin: show or change a setting. +teststockset base_price 30000 | +teststockset fee 2 | ... reset"""
        if not await self._allowed(ctx):
            return
        gid, cfg = ctx.guild.id, self._cfg(ctx.guild.id)
        if setting is None:
            lines = [f"`{k}` = **{cfg[k]:g}**" + (f" (default {SETTINGS[k][0]:g})" if cfg[k] != SETTINGS[k][0] else "")
                     + f"\n└ {SETTINGS[k][3]}" for k in SETTINGS]
            embed = discord.Embed(title="Test stock settings", description="\n".join(lines)[:4000], color=0x62A0FF)
            embed.set_footer(text="+teststockset <setting> <value>   ·   +teststockset <setting> reset   ·   30k and 1.5m work")
            return await ctx.reply(embed=embed, mention_author=False)
        key = setting.casefold().replace("-", "_")
        key = ALIASES.get(key.replace("_", ""), ALIASES.get(key, key))
        if key not in SETTINGS:
            return await ctx.reply(f"I don't know the setting `{setting}`. Run `+teststockset` to see them all.",
                                   mention_author=False)
        default, lo, hi, desc = SETTINGS[key]
        if value is None:
            return await ctx.reply(f"`{key}` = **{cfg[key]:g}** (default {default:g}). {desc}.", mention_author=False)
        if value.strip().casefold() in ("reset", "default"):
            self.bot.db.execute("DELETE FROM tstock_settings WHERE guild_id=? AND key=?", (gid, key))
            number = default
        else:
            number = parse_amount(value)
            if number is None or not (lo <= number <= hi):
                return await ctx.reply(f"`{key}` needs a number from {lo:g} to {hi:g}.", mention_author=False)
            if isinstance(default, int) and float(number) != int(number):
                return await ctx.reply(f"`{key}` must be a whole number.", mention_author=False)
            self.bot.db.execute("INSERT INTO tstock_settings (guild_id, key, value) VALUES (?,?,?) "
                                "ON CONFLICT(guild_id, key) DO UPDATE SET value=excluded.value", (gid, key, str(number)))
        self.bot.db.commit()
        self._changed()
        note = ""
        if key not in ("sell_fee", "buy_fee", "max_per_user", "start_cash", "market_open"):
            note = f" Prices were recalculated for **{len(self._market(gid))}** players."
        await ctx.reply(f"✅ `{key}` is now **{number:g}**.{note}", mention_author=False)

    @commands.command(name="teststockplayer", aliases=["tstockplayer"])
    @commands.guild_only()
    async def teststockplayer(self, ctx, *, text: str = ""):
        """Admin: per-player overrides. +teststockplayer <player> base 45000 | supply 20 | freeze | unfreeze | reset"""
        if not await self._allowed(ctx):
            return
        guild, db = ctx.guild, self.bot.db
        parts = text.split()
        action, number = None, None
        if parts and parts[-1].casefold() in ("freeze", "unfreeze", "reset"):
            action, query = parts[-1].casefold(), " ".join(parts[:-1])
        elif len(parts) >= 3 and parts[-2].casefold() in ("base", "supply") and parse_amount(parts[-1]) is not None:
            action, number, query = parts[-2].casefold(), int(parse_amount(parts[-1])), " ".join(parts[:-2])
        if action is None:
            return await ctx.reply("Usage: `+teststockplayer <player> base <price>` · `supply <n>` · `freeze` · "
                                   "`unfreeze` · `reset`", mention_author=False)
        market = self._market(guild.id)
        uid, error = self._find(guild, market, query)
        if error:
            return await ctx.reply(error, mention_author=False, allowed_mentions=discord.AllowedMentions.none())
        if action in ("base", "supply") and number < 1:
            return await ctx.reply("That has to be at least 1.", mention_author=False)
        db.execute("INSERT OR IGNORE INTO tstock_players (guild_id, player_id) VALUES (?,?)", (guild.id, uid))
        if action == "base":
            db.execute("UPDATE tstock_players SET base_price=? WHERE guild_id=? AND player_id=?", (number, guild.id, uid))
        elif action == "supply":
            db.execute("UPDATE tstock_players SET supply=? WHERE guild_id=? AND player_id=?", (number, guild.id, uid))
        elif action in ("freeze", "unfreeze"):
            db.execute("UPDATE tstock_players SET frozen=? WHERE guild_id=? AND player_id=?",
                       (1 if action == "freeze" else 0, guild.id, uid))
        else:
            db.execute("DELETE FROM tstock_players WHERE guild_id=? AND player_id=?", (guild.id, uid))
        db.commit()
        self._changed()
        s = self._market(guild.id)[uid]
        await ctx.reply(f"✅ **{discord.utils.escape_markdown(self._name(guild, uid))}**: {action}"
                        + (f" {number:,}" if number else "")
                        + f". Now **{s['price']:,}** (base {s['base']:,}), supply {s['supply']}"
                        + (", frozen" if s["frozen"] else "") + ".",
                        mention_author=False, allowed_mentions=discord.AllowedMentions.none())

    @commands.command(name="teststockwallet", aliases=["tstockwallet"])
    @commands.guild_only()
    async def teststockwallet(self, ctx, action: str = "", member: discord.Member = None, amount: str = ""):
        """Admin: +teststockwallet add|remove|set @user <amount>"""
        if not await self._allowed(ctx):
            return
        number = parse_amount(amount) if amount else None
        action = action.casefold()
        if action not in ("add", "remove", "set") or member is None or number is None or number < 0 or number != int(number):
            return await ctx.reply("Usage: `+teststockwallet add|remove|set @user <amount>` (30k and 1.5m work)",
                                   mention_author=False)
        number, db, gid = int(number), self.bot.db, ctx.guild.id
        before = self._wallet(gid, member.id)
        after = before + number if action == "add" else max(0, before - number) if action == "remove" else number
        db.execute("UPDATE tstock_wallets SET cash=? WHERE guild_id=? AND user_id=?", (after, gid, member.id))
        db.commit()
        await ctx.reply(f"✅ **{discord.utils.escape_markdown(member.display_name)}**'s wallet: {before:,} → **{after:,}** {CURRENCY}.",
                        mention_author=False, allowed_mentions=discord.AllowedMentions.none())

    @commands.command(name="teststockmarket", aliases=["tstockmarket"])
    @commands.guild_only()
    async def teststockmarket(self, ctx, state: str = ""):
        """Admin: open or close trading. +teststockmarket open|close"""
        if not await self._allowed(ctx):
            return
        state = state.casefold()
        if state not in ("open", "close", "closed"):
            now = "open" if self._cfg(ctx.guild.id)["market_open"] else "closed"
            return await ctx.reply(f"The market is **{now}**. Use `+teststockmarket open` or `close`.", mention_author=False)
        value = 1 if state == "open" else 0
        self.bot.db.execute("INSERT INTO tstock_settings (guild_id, key, value) VALUES (?,?,?) "
                            "ON CONFLICT(guild_id, key) DO UPDATE SET value=excluded.value", (ctx.guild.id, "market_open", str(value)))
        self.bot.db.commit()
        self._changed()
        await ctx.reply("🔓 Trading is **open**." if value else "🔒 Trading is **closed**. Buying and selling are paused.",
                        mention_author=False)

    @commands.command(name="teststockreset", aliases=["tstockreset"])
    @commands.guild_only()
    async def teststockreset(self, ctx, confirm: str = ""):
        """Admin: wipe test wallets, holdings and trades (settings stay). +teststockreset confirm"""
        if not await self._allowed(ctx):
            return
        if confirm.casefold() != "confirm":
            return await ctx.reply("⚠️ This deletes every test wallet, holding and trade on this server. "
                                   "Type `+teststockreset confirm` to do it.", mention_author=False)
        for table in ("tstock_wallets", "tstock_holdings", "tstock_trades"):
            self.bot.db.execute(f"DELETE FROM {table} WHERE guild_id=?", (ctx.guild.id,))
        self.bot.db.commit()
        await ctx.reply("✅ Test wallets, holdings and trades were cleared.", mention_author=False)

    # ------------------------------------------------------------------ help
    @commands.command(name="teststockhelp", aliases=["tstockhelp"])
    @commands.guild_only()
    async def teststockhelp(self, ctx):
        """Test: list of the test stock commands."""
        if not await self._allowed(ctx):
            return
        cfg = self._cfg(ctx.guild.id)
        embed = discord.Embed(title="Test Player Stock Market", color=0x62A0FF, description=(
            f"Every player with match stats is a stock. Price starts at **{int(cfg['base_price']):,}** and moves with "
            f"runs, wickets, strike rate, economy and ducks. **{int(cfg['supply'])}** shares exist per player, "
            f"max **{int(cfg['max_per_user'])}** each, sell fee **{cfg['sell_fee']:g}%**."))
        embed.add_field(name="Players", inline=False, value=(
            "`+teststock <player>` one stock\n`+testmarket [page] [sort]` the market (buttons + sort menu)\n"
            "`+testportfolio [@user]` holdings\n`+testbuy <player> <amount|all>`  `+testsell <player> <amount|all>`\n"
            "`+testwallet`  `+testtraders`  `+testtrades`"))
        embed.add_field(name="Admins", inline=False, value=(
            "`+teststockset` settings (base price, supply, max per user, fees, weights)\n"
            "`+teststockplayer <player> base|supply|freeze|unfreeze|reset`\n"
            "`+teststockwallet add|remove|set @user <amount>`\n`+teststockmarket open|close`  `+teststockreset confirm`"))
        await ctx.reply(embed=embed, mention_author=False)


async def setup(bot):
    await bot.add_cog(TestStocks(bot))
