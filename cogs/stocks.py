"""Googli stock exchange: every player with imported stats is a stock whose price follows their play.

+stocks [gainers|losers|cheap]   the market, 10 players a page with Previous/Next buttons
+stock <player>                  one stock: price, where the coins came from, history, shares left, your position
+stocks lb                       richest traders (coins + value of shares)
+buy <player> <amount|all>  /  +sell <player> <amount|all>     each trade posts a receipt card
+portfolio [@user]               holdings, 8 a page with buttons
+wallet [@user]                  coins, shares worth, profit and loss, fees, trades
+stockset [name value|default]   (admins) base price, weights, share supply, limits and more
+stockreset confirm              (admins) wipe every account and holding of this server

How a price is made (fair both ways)
------------------------------------
Every player starts at the base price. After each match the price moves by how the player did compared with the
league average, so an average match changes nothing, a better one adds coins and a worse one takes the same
amount off:
  runs above or below the average innings, wickets above or below the average spell, a strike-rate edge (runs vs
  what the league scores off the same balls), an economy edge (runs saved or leaked vs the league rate over the
  balls bowled), ducks against the usual duck rate, and fielding (catches above or below the average player,
  drops the other way, spread over the player's matches).
Across the whole league the gains and losses cancel out. Each player has a limited number of shares and a
trader can hold only a few of each. Prices change only when stats are imported or settings change.
"""
import asyncio
import io
import re

import discord
from discord.ext import commands

from .h2h import ImagePager, fetch_avatar, on_cooldown, render_png
from .predict_image import render_board_png
from .stocks_image import (render_market_png, render_portfolio_png, render_stock_png, render_trade_png,
                           render_wallet_png)
from .utility import owner_or_whitelisted

# name -> (db key, default, min, max, what it does)
SETTINGS = {
    "base": ("base_price", 100, 1, 100_000_000, "price every player's stock starts at"),
    "run": ("per_run", 0.5, 0, 100_000, "coins per run above (or below) the average innings"),
    "wicket": ("per_wicket", 5, 0, 1_000_000, "coins per wicket above (or below) the average spell"),
    "sr": ("sr_weight", 0.3, 0, 100_000, "coins per run scored above (or below) the league rate off the same balls"),
    "eco": ("eco_weight", 0.3, 0, 100_000, "coins per run saved (or leaked) against the league rate while bowling"),
    "duck": ("duck_penalty", 4, 0, 1_000_000, "coins lost for a duck (a player with fewer ducks than usual gains)"),
    "catch": ("catch_bonus", 4, 0, 1_000_000, "coins per catch above (or below) the average player"),
    "drop": ("drop_penalty", 4, 0, 1_000_000, "coins lost per dropped catch above the average player"),
    "minprice": ("min_price", 10, 1, 100_000_000, "a price never drops below this"),
    "supply": ("supply", 30, 1, 10_000, "shares that exist of each player"),
    "maxshares": ("max_per_user", 5, 1, 10_000, "most shares of one player a trader can hold"),
    "start": ("start_cash", 1000, 0, 10 ** 12, "coins a new trader starts with"),
    "fee": ("fee_pct", 2, 0, 50, "percent charged on every buy and sell"),
}
FLOAT_KEYS = {"per_run", "per_wicket", "sr_weight", "eco_weight", "duck_penalty", "catch_bonus", "drop_penalty", "fee_pct"}
PRICE_KEYS = ("base", "run", "wicket", "sr", "eco", "duck", "catch", "drop", "minprice")
ALIASES = {"baseprice": "base", "base_price": "base", "price": "base", "runs": "run", "perrun": "run",
           "wickets": "wicket", "wkt": "wicket", "wkts": "wicket", "strikerate": "sr", "economy": "eco",
           "ducks": "duck", "catches": "catch", "fielding": "catch", "drops": "drop", "dropped": "drop",
           "min": "minprice", "minimum": "minprice", "shares": "supply", "total": "supply",
           "max": "maxshares", "limit": "maxshares", "cash": "start", "startcash": "start", "coins": "start",
           "fees": "fee", "tax": "fee", "margin": "fee"}
PAGE_MARKET = 10
PAGE_PORTFOLIO = 8
BALLS_PER_OVER = 6


def fee_for(amount, fee_pct):
    return 0 if fee_pct <= 0 else max(1, int(round(amount * fee_pct / 100)))


def build_prices(matches_by_player, pars, S, fielding):
    """{uid: stock}. matches_by_player: per-match rows oldest first. pars: league averages. fielding: {uid: points}.

    Every part is measured against the league average, so the league's gains and losses cancel out."""
    out = {}
    for uid, matches in matches_by_player.items():
        total = {"runs": 0.0, "wickets": 0.0, "sr": 0.0, "eco": 0.0, "duck": 0.0, "field": 0.0}
        field_each = fielding.get(uid, 0.0) / len(matches)
        history, deltas = [], []
        runs = balls = conceded = bowled = wickets = ducks = 0
        value = float(S["base_price"])
        for m in matches:
            parts = dict.fromkeys(total, 0.0)
            if m["balls_faced"] > 0:
                parts["runs"] = (m["runs"] - pars["avg_runs"]) * S["per_run"]
                parts["sr"] = (m["runs"] - m["balls_faced"] * pars["bat"]) * S["sr_weight"]
                is_duck = m["runs"] == 0 and not m["not_out"]
                parts["duck"] = -S["duck_penalty"] * ((1.0 if is_duck else 0.0) - pars["duck_rate"])
                ducks += 1 if is_duck else 0
            if m["balls_bowled"] > 0:
                parts["wickets"] = (m["wickets"] - pars["avg_wkts"]) * S["per_wicket"]
                parts["eco"] = (m["balls_bowled"] * pars["bowl"] - m["runs_conceded"]) * S["eco_weight"]
            parts["field"] = field_each
            for k, v in parts.items():
                total[k] += v
            before = max(S["min_price"], int(round(value)))

            # Convert the performance score into a percentage of the base price.
            # This keeps movement consistent at 100, 50k, or any other base price.
            # Negative scores lower the stock; positive scores raise it.
            raw_delta = sum(parts.values())
            base_price = max(1.0, float(S["base_price"]))
            move_pct = (raw_delta / base_price) * 10.0
            move_pct = max(-8.0, min(8.0, move_pct))

            value = max(float(S["min_price"]), value * (1.0 + move_pct / 100.0))
            now = max(S["min_price"], int(round(value)))
            deltas.append(now - before)
            history.append(now)
            runs += m["runs"]; balls += m["balls_faced"]; conceded += m["runs_conceded"]
            bowled += m["balls_bowled"]; wickets += m["wickets"]
        price = history[-1]
        prev = history[-2] if len(history) > 1 else S["base_price"]
        out[uid] = {"price": price, "prev": prev, "change": (price - prev) / prev * 100 if prev else 0.0,
                    "history": history, "deltas": deltas, "games": len(history), "factors": total,
                    "runs": runs, "balls": balls, "conceded": conceded, "bowled": bowled, "wickets": wickets,
                    "ducks": ducks}
    return out


class StockPager(ImagePager):
    """Previous/Next buttons; each page is drawn (avatars fetched) only when somebody opens it."""

    def __init__(self, ctx, first_png, count, build, filename, start_index=0):
        super().__init__(ctx, first_png, count, None, [], filename)
        self.pages = [None] * count
        self.pages[start_index] = first_png
        self.index = start_index
        self.build = build
        self.first_button = discord.ui.Button(label="First", emoji="⏮️", style=discord.ButtonStyle.secondary, row=0)
        self.last_button = discord.ui.Button(label="Last", emoji="⏭️", style=discord.ButtonStyle.secondary, row=0)
        self.first_button.callback = self._first
        self.last_button.callback = self._last
        self.add_item(self.first_button)
        self.add_item(self.last_button)
        self._sync()

    def _sync(self):
        super()._sync()
        if hasattr(self, "first_button"):
            self.first_button.disabled = self.index <= 0
            self.last_button.disabled = self.index >= len(self.pages) - 1

    async def _first(self, interaction):
        await self._show(interaction, 0)

    async def _last(self, interaction):
        await self._show(interaction, len(self.pages) - 1)

    async def _show(self, interaction, index):
        self.index = max(0, min(index, len(self.pages) - 1))
        self._sync()
        await interaction.response.defer()
        if self.pages[self.index] is None:
            try:
                self.pages[self.index] = await self.build(self.index)
            except Exception as exc:
                print(f"[stocks] page render failed: {type(exc).__name__}: {exc}", flush=True)
                await interaction.followup.send("I couldn't draw that page. Check the bot logs.", ephemeral=True)
                return
        await interaction.edit_original_response(
            attachments=[discord.File(io.BytesIO(self.pages[self.index]), filename=self.filename)], view=self)


class Stocks(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._lock = asyncio.Lock()
        self._cache = {}            # guild id -> (version, market)
        self._catches = {}
        db = bot.db
        db.execute("CREATE TABLE IF NOT EXISTS stock_accounts (guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, "
                   "cash INTEGER NOT NULL, last_daily REAL NOT NULL DEFAULT 0, PRIMARY KEY (guild_id, user_id))")
        db.execute("CREATE TABLE IF NOT EXISTS stock_holdings (guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, "
                  "player_id INTEGER NOT NULL, shares INTEGER NOT NULL, cost INTEGER NOT NULL, "
                  "PRIMARY KEY (guild_id, user_id, player_id))")
        db.execute("CREATE TABLE IF NOT EXISTS stock_settings (guild_id INTEGER NOT NULL, key TEXT NOT NULL, "
                  "value REAL NOT NULL, PRIMARY KEY (guild_id, key))")
        db.execute("CREATE TABLE IF NOT EXISTS stock_meta (key TEXT PRIMARY KEY, value TEXT)")
        columns = {r[1] for r in db.execute("PRAGMA table_info(stock_accounts)")}
        for column in ("realized", "fees", "trades"):
            if column not in columns:
                db.execute(f"ALTER TABLE stock_accounts ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0")
        # earlier versions used other price scales and wallets; those accounts do not fit this model
        row = db.execute("SELECT value FROM stock_meta WHERE key='model'").fetchone()
        if not row or row[0] != "3":
            db.execute("DELETE FROM stock_accounts")
            db.execute("DELETE FROM stock_holdings")
            db.execute("DELETE FROM stock_settings")
            db.execute("INSERT OR REPLACE INTO stock_meta (key, value) VALUES ('model', '3')")
        db.commit()

    @property
    def compare(self):
        return self.bot.get_cog("CompareTeams")

    # ------------------------------------------------------------------ settings
    def _settings(self, guild_id):
        S = {key: default for key, default, _lo, _hi, _t in SETTINGS.values()}
        for row in self.bot.db.execute("SELECT key, value FROM stock_settings WHERE guild_id=?", (int(guild_id),)):
            if row["key"] in S:
                S[row["key"]] = float(row["value"]) if row["key"] in FLOAT_KEYS else int(row["value"])

        # Keep movement weights and the minimum price proportional to the base.
        # build_prices converts the resulting score to a capped percentage change.
        scale = max(0.01, float(S["base_price"]) / 100.0)
        for key in ("per_run", "per_wicket", "sr_weight", "eco_weight",
                    "duck_penalty", "catch_bonus", "drop_penalty", "min_price"):
            S[key] = S[key] * scale
        return S

    # ------------------------------------------------------------------ market data
    def _market(self, guild_id):
        """{uid: stock dict} for every player with stats; rebuilt only when stats or settings changed."""
        db = self.bot.db
        S = self._settings(guild_id)
        version = db.execute("SELECT COUNT(*), COALESCE(MAX(imported_at), '') FROM stats_imports WHERE guild_id=?",
                             (int(guild_id),)).fetchone()
        try:
            fv = db.execute("SELECT COUNT(*), COALESCE(SUM(catches_caught),0), COALESCE(SUM(catches_dropped),0) "
                            "FROM fielding_stats WHERE guild_id=?", (int(guild_id),)).fetchone()
            fv = tuple(fv)
        except Exception:
            fv = ()
        version = (version[0], str(version[1]), tuple(sorted(S.items())), fv)
        hit = self._cache.get(int(guild_id))
        if hit and hit[0] == version:
            return hit[1], S
        rows = db.execute(
            "SELECT p.user_id,p.runs,p.balls_faced,p.runs_conceded,p.balls_bowled,p.wickets,p.not_out "
            "FROM player_match_stats p JOIN stats_imports i ON i.source_message_id=p.source_message_id "
            "WHERE i.guild_id=? AND COALESCE(i.competition,'MAIN TOURNAMENT')='MAIN TOURNAMENT' "            "ORDER BY i.imported_at, p.source_message_id", (int(guild_id),)).fetchall()
        per = {}
        runs = balls = conceded = bowled = wkts = innings = spells = ducks = 0
        for r in rows:
            per.setdefault(int(r["user_id"]), []).append({k: int(r[k]) for k in r.keys() if k != "user_id"})
            if r["balls_faced"] > 0:
                innings += 1; runs += int(r["runs"]); balls += int(r["balls_faced"])
                ducks += 1 if (r["runs"] == 0 and not r["not_out"]) else 0
            if r["balls_bowled"] > 0:
                spells += 1; wkts += int(r["wickets"]); bowled += int(r["balls_bowled"]); conceded += int(r["runs_conceded"])
        pars = {"bat": runs / balls if balls else 0.0, "bowl": conceded / bowled if bowled else 0.0,
                "avg_runs": runs / innings if innings else 0.0, "avg_wkts": wkts / spells if spells else 0.0,
                "duck_rate": ducks / innings if innings else 0.0}
        field = {}
        if per:
            caught = dict(self._fielding(guild_id))
            mean_c = sum(caught.get(u, (0, 0))[0] for u in per) / len(per)
            mean_d = sum(caught.get(u, (0, 0))[1] for u in per) / len(per)
            field = {u: (caught.get(u, (0, 0))[0] - mean_c) * S["catch_bonus"] - (caught.get(u, (0, 0))[1] - mean_d) * S["drop_penalty"]
                     for u in per}
            for u in per:
                self._catches[u] = caught.get(u, (0, 0))
        market = build_prices(per, pars, S, field) if per else {}
        for uid, stock in market.items():
            stock["par_bat"], stock["par_bowl"] = pars["bat"], pars["bowl"]
            stock["catches"], stock["drops"] = self._catches.get(uid, (0, 0))
        self._cache = {int(guild_id): (version, market)}      # one server at a time keeps memory small
        return market, S

    def _fielding(self, guild_id):
        try:
            return {int(r[0]): (int(r[1]), int(r[2])) for r in self.bot.db.execute(
                "SELECT user_id, catches_caught, catches_dropped FROM fielding_stats WHERE guild_id=?", (int(guild_id),))}
        except Exception:
            return {}

    def _held_totals(self, guild_id):
        return {int(r[0]): int(r[1]) for r in self.bot.db.execute(
            "SELECT player_id, SUM(shares) FROM stock_holdings WHERE guild_id=? GROUP BY player_id", (int(guild_id),))}

    def _name(self, guild, uid):
        if self.compare is not None:
            return self.compare._display_name(guild, uid)
        member = guild.get_member(int(uid))
        return member.name if member else f"Player {str(uid)[-4:]}"

    def _tickers(self, guild, market):
        """{uid: 'ABCD'}: four letters of the username, a digit added on a clash."""
        taken, out = set(), {}
        for uid in sorted(market):
            base = (re.sub(r"[^A-Za-z0-9]", "", self._name(guild, uid)).upper() or "PLAY")[:4]
            ticker, n = base, 1
            while ticker in taken:
                n += 1
                ticker = f"{base[:3]}{n}" if n < 10 else f"{base[:2]}{n}"
            taken.add(ticker)
            out[uid] = ticker
        return out

    def _find(self, guild, market, tickers, query):
        """(uid, error) for a mention, id, ticker or (part of a) username."""
        query = query.strip()
        if not query:
            return None, "Tell me which player."
        m = re.match(r"^<@!?(\d+)>$", query) or re.match(r"^(\d{15,25})$", query)
        if m:
            uid = int(m.group(1))
            return (uid, None) if uid in market else (None, "That player has no match stats yet, so there's no stock.")
        needle = query.casefold().lstrip("@")
        exact = [u for u in market if tickers[u].casefold() == needle or self._name(guild, u).casefold() == needle]
        if len(exact) == 1:
            return exact[0], None
        loose = [u for u in market if needle in self._name(guild, u).casefold() or tickers[u].casefold().startswith(needle)]
        if len(loose) == 1:
            return loose[0], None
        if not loose and not exact:
            return None, f"I can't find a listed player matching `{query}`."
        pool = exact or loose
        names = ", ".join(f"{self._name(guild, u)} (`{tickers[u]}`)" for u in pool[:6])
        return None, f"That matches several players: {names}. Use the ticker or a mention."

    # ------------------------------------------------------------------ accounts
    def _account(self, guild_id, user_id, S=None):
        db = self.bot.db
        start = (S or self._settings(guild_id))["start_cash"]
        db.execute("INSERT OR IGNORE INTO stock_accounts (guild_id, user_id, cash) VALUES (?,?,?)",
                   (int(guild_id), int(user_id), int(start)))
        return db.execute("SELECT cash, realized, fees, trades FROM stock_accounts WHERE guild_id=? AND user_id=?",
                          (int(guild_id), int(user_id))).fetchone()

    def _holdings(self, guild_id, user_id):
        return self.bot.db.execute("SELECT player_id, shares, cost FROM stock_holdings WHERE guild_id=? AND user_id=? "
                                   "AND shares>0", (int(guild_id), int(user_id))).fetchall()

    def _worth(self, guild_id, user_id, market, cash=None, S=None):
        """(net worth, value of shares)."""
        if cash is None:
            cash = self._account(guild_id, user_id, S)["cash"]
        invested = sum(int(h["shares"]) * market.get(int(h["player_id"]), {}).get("price", 0)
                       for h in self._holdings(guild_id, user_id))
        return cash + invested, invested

    async def _avatars(self, guild, uids):
        out, sem = {}, asyncio.Semaphore(6)

        async def one(uid):
            async with sem:
                member = guild.get_member(uid) or self.bot.get_user(uid)
                if member is None:
                    try:
                        member = await self.bot.fetch_user(uid)
                    except discord.HTTPException:
                        member = None
                out[uid] = await fetch_avatar(self.bot, member)

        await asyncio.gather(*(one(u) for u in set(uids)), return_exceptions=True)
        return out

    async def _draw(self, fn, data):
        async with self._lock:
            return await render_png(fn, data)

    async def _send_png(self, ctx, png, filename, view=None):
        message = await ctx.send(file=discord.File(io.BytesIO(png), filename=filename), view=view)
        if view is not None:
            view.message = message

    async def _send_pages(self, ctx, count, build, filename, start_index=0):
        start_index = max(0, min(int(start_index), count - 1))
        first = await build(start_index)
        if count <= 1:
            return await self._send_png(ctx, first, filename)
        await self._send_png(ctx, first, filename,
                             StockPager(ctx, first, count, build, filename, start_index))

    async def _empty(self, ctx):
        await ctx.reply("The market is empty: it opens once match stats have been imported.", mention_author=False)

    # ------------------------------------------------------------------ +stocks
    SORTS = {"gainers": ("TOP GAINERS", lambda m, u: (-m[u]["change"], -m[u]["price"], u)),
             "losers": ("BIGGEST FALLERS", lambda m, u: (m[u]["change"], -m[u]["price"], u)),
             "cheap": ("CHEAPEST FIRST", lambda m, u: (m[u]["price"], u)),
             "price": ("MOST VALUABLE FIRST", lambda m, u: (-m[u]["price"], u))}
    SORT_WORDS = {"gainers": "gainers", "gainer": "gainers", "gain": "gainers", "up": "gainers", "top": "price",
                  "losers": "losers", "loser": "losers", "fallers": "losers", "down": "losers",
                  "cheap": "cheap", "cheapest": "cheap", "low": "cheap", "price": "price", "value": "price"}

    @commands.command(name="stocks", aliases=["stock", "market", "exchange"])
    @commands.guild_only()
    async def stocks(self, ctx, *, text: str = ""):
        """The player stock market: +stocks [gainers|losers|cheap], +stock <player>, +stocks lb."""
        guild, text = ctx.guild, text.strip()
        word = text.casefold()
        requested_page = None
        if word.isdigit():
            requested_page = int(word)
            text = ""
            word = ""
        elif word.startswith("page ") and word[5:].strip().isdigit():
            requested_page = int(word[5:].strip())
            text = ""
            word = ""
        if word in ("help", "guide", "how"):
            S = self._settings(guild.id)
            return await ctx.reply(
                "**Googli Stock Exchange**\nEvery player with match stats is a stock. It starts at "
                f"**{S['base_price']:,}** and moves after each match by how they did compared with the league average "
                "(runs, wickets, strike rate, economy, ducks, catches). A normal match changes nothing, a better one "
                "gains and a worse one loses the same amount. "
                f"Only **{S['supply']}** shares exist of each player and you can hold **{S['max_per_user']}**.\n"
                "`+stocks` market (buttons to change page)  •  `+stock <player>` one stock  •  `+stocks lb` richest\n"
                "`+buy <player> <amount|all>`  •  `+sell <player> <amount|all>`  •  `+portfolio [@user]`  •  `+wallet [@user]`\n"
                f"You start with **{S['start_cash']:,}** coins. Each buy and sell costs a {S['fee_pct']:g}% fee.",
                mention_author=False)
        market, S = self._market(guild.id)
        if not market:
            return await self._empty(ctx)
        if word in ("lb", "leaderboard", "rich", "richest", "traders"):
            return await self._leaderboard(ctx, market, S)
        wait = on_cooldown(ctx.author.id)
        if wait:
            return await ctx.reply(f"Please wait {wait:.0f}s before drawing another image.", mention_author=False)
        tickers = self._tickers(guild, market)
        async with ctx.typing():
            try:
                if text and word not in self.SORT_WORDS and word not in ("list", "all", "page"):
                    uid, error = self._find(guild, market, tickers, text)
                    if error:
                        return await ctx.reply(error, mention_author=False, allowed_mentions=discord.AllowedMentions.none())
                    png = await self._draw(render_stock_png, await self._stock_data(ctx, market, tickers, uid, S))
                    return await self._send_png(ctx, png, "stock.png")
                mode = self.SORT_WORDS.get(word, "price")
                label, key = self.SORTS[mode]
                order = sorted(market, key=lambda u: key(market, u))
                held = self._held_totals(guild.id)
                trend = sum(s["change"] for s in market.values()) / len(market)
                pages = (len(order) + PAGE_MARKET - 1) // PAGE_MARKET

                async def build(index):
                    chunk = order[index * PAGE_MARKET:(index + 1) * PAGE_MARKET]
                    avatars = await self._avatars(guild, chunk)
                    rows = [{"name": self._name(guild, u), "ticker": tickers[u], "avatar": avatars.get(u),
                             "price": market[u]["price"], "change": market[u]["change"],
                             "spark": market[u]["history"][-8:], "left": max(0, S["supply"] - held.get(u, 0)), "rank": index * PAGE_MARKET + n + 1}
                            for n, u in enumerate(chunk)]
                    return await self._draw(render_market_png, {
                        "guild": guild.name, "count": len(market), "trend": trend, "page": index + 1, "pages": pages,
                        "sort": label, "supply": S["supply"], "rows": rows})

                if requested_page is not None:
                    if requested_page < 1 or requested_page > pages:
                        return await ctx.reply(f"Choose a market page from 1 to {pages}.", mention_author=False)
                    start_index = requested_page - 1
                else:
                    start_index = 0
                await self._send_pages(ctx, pages, build, "market.png", start_index=start_index)
            except Exception as exc:
                import traceback
                traceback.print_exc()
                print(f"[stocks] failed: {type(exc).__name__}: {exc}", flush=True)
                await ctx.reply("I couldn't draw the market. Check the bot logs.", mention_author=False)

    async def _stock_data(self, ctx, market, tickers, uid, S):
        guild, stock = ctx.guild, market[uid]
        rank = 1 + sum(1 for s in market.values() if s["price"] > stock["price"])
        held = next((h for h in self._holdings(guild.id, ctx.author.id) if int(h["player_id"]) == uid), None)
        holding = {"shares": int(held["shares"]), "value": int(held["shares"]) * stock["price"],
                   "cost": int(held["cost"])} if held else None
        avatars = await self._avatars(guild, {uid})
        f = stock["factors"]
        sr = stock["runs"] / stock["balls"] * 100 if stock["balls"] else 0.0
        eco = stock["conceded"] / stock["bowled"] * BALLS_PER_OVER if stock["bowled"] else 0.0
        overs = stock["bowled"] / BALLS_PER_OVER
        n = len(stock["history"])
        shown = stock["history"][-20:]
        return {"guild": guild.name, "name": self._name(guild, uid), "ticker": tickers[uid], "avatar": avatars.get(uid),
                "price": stock["price"], "change": stock["change"], "base": S["base_price"],
                "factors": [("Runs vs average", int(round(f["runs"]))), ("Wickets vs average", int(round(f["wickets"]))),
                            ("Strike-rate edge", int(round(f["sr"]))), ("Economy edge", int(round(f["eco"]))),
                            ("Ducks", int(round(f["duck"]))), ("Fielding", int(round(f["field"])))],
                "sr": sr, "sr_par": stock["par_bat"] * 100, "wickets": stock["wickets"],
                "wpo": stock["wickets"] / overs if overs else 0.0, "eco": eco,
                "eco_par": stock["par_bowl"] * BALLS_PER_OVER, "ducks": stock["ducks"],
                "catches": stock.get("catches", 0), "drops": stock.get("drops", 0),
                "games": stock["games"], "history": shown, "first": n - len(shown) + 1, "rank": rank, "listed": len(market),
                "left": max(0, S["supply"] - self._held_totals(guild.id).get(uid, 0)), "supply": S["supply"],
                "max": S["max_per_user"], "holding": holding}

    @commands.command(name="playerrating", aliases=["playerperformance", "prating"])
    @commands.guild_only()
    async def playerrating(self, ctx, *, query: str = ""):
        """Show a balanced performance rating using every stat stored for the player."""
        if not query.strip():
            return await ctx.reply("Usage: `+playerrating <player>`.", mention_author=False)

        market, _settings = self._market(ctx.guild.id)
        if not market:
            return await self._empty(ctx)
        tickers = self._tickers(ctx.guild, market)
        uid, error = self._find(ctx.guild, market, tickers, query)
        if error:
            return await ctx.reply(error, mention_author=False,
                                   allowed_mentions=discord.AllowedMentions.none())

        db, gid = self.bot.db, ctx.guild.id
        rows = db.execute(
            "SELECT p.user_id, p.runs, p.balls_faced, p.runs_conceded, "
            "p.balls_bowled, p.wickets, p.not_out "
            "FROM player_match_stats p JOIN stats_imports i "
            "ON i.source_message_id=p.source_message_id "
            "WHERE i.guild_id=? AND COALESCE(i.competition,'MAIN TOURNAMENT')='MAIN TOURNAMENT' "
            "ORDER BY i.imported_at, p.source_message_id", (gid,)
        ).fetchall()

        totals = {}
        for row in rows:
            player_id = int(row["user_id"])
            item = totals.setdefault(player_id, {
                "matches": 0, "bat_innings": 0, "runs": 0, "balls_faced": 0,
                "not_outs": 0, "ducks": 0, "bowl_spells": 0, "runs_conceded": 0,
                "balls_bowled": 0, "wickets": 0,
            })
            item["matches"] += 1
            item["runs"] += int(row["runs"])
            item["balls_faced"] += int(row["balls_faced"])
            item["not_outs"] += int(row["not_out"])
            if int(row["balls_faced"]) > 0:
                item["bat_innings"] += 1
                if int(row["runs"]) == 0 and not int(row["not_out"]):
                    item["ducks"] += 1
            if int(row["balls_bowled"]) > 0:
                item["bowl_spells"] += 1
                item["runs_conceded"] += int(row["runs_conceded"])
                item["balls_bowled"] += int(row["balls_bowled"])
                item["wickets"] += int(row["wickets"])

        fielding = self._fielding(gid)
        metrics = {}
        for player_id, item in totals.items():
            dismissals = max(0, item["bat_innings"] - item["not_outs"])
            catches, drops = fielding.get(player_id, (0, 0))
            chances = catches + drops
            metrics[player_id] = {
                "runs_per_innings": item["runs"] / item["bat_innings"] if item["bat_innings"] else None,
                "batting_average": item["runs"] / dismissals if dismissals else None,
                "strike_rate": item["runs"] / item["balls_faced"] * 100 if item["balls_faced"] else None,
                "wickets_per_spell": item["wickets"] / item["bowl_spells"] if item["bowl_spells"] else None,
                "economy": item["runs_conceded"] / item["balls_bowled"] * BALLS_PER_OVER if item["balls_bowled"] else None,
                "bowling_strike_rate": item["balls_bowled"] / item["wickets"] if item["wickets"] else None,
                "duck_rate": item["ducks"] / item["bat_innings"] if item["bat_innings"] else None,
                "catch_success": catches / chances if chances else None,
                "drops_per_chance": drops / chances if chances else None,
            }

        # Convert each available metric into a league percentile. Lower economy,
        # bowling strike rate, duck rate and drop rate are better. Every category
        # has equal weight, avoiding raw-count or one-stat domination.
        higher_is_better = {
            "runs_per_innings": True, "batting_average": True, "strike_rate": True,
            "wickets_per_spell": True, "economy": False, "bowling_strike_rate": False,
            "duck_rate": False, "catch_success": True, "drops_per_chance": False,
        }
        scores = {}
        for player_id, values in metrics.items():
            parts = []
            for key, higher in higher_is_better.items():
                value = values[key]
                if value is None:
                    continue
                peers = [other[key] for other in metrics.values() if other[key] is not None]
                if len(peers) <= 1:
                    percentile = 50.0
                else:
                    below = sum(1 for peer in peers if peer < value)
                    equal = sum(1 for peer in peers if peer == value)
                    percentile = (below + (equal - 1) / 2) / (len(peers) - 1) * 100
                parts.append(percentile if higher else 100 - percentile)
            scores[player_id] = sum(parts) / len(parts) if parts else 50.0

        item = totals.get(uid)
        if not item:
            return await ctx.reply("No main-tournament stats found for that player.", mention_author=False)
        catches, drops = fielding.get(uid, (0, 0))
        dismissals = max(0, item["bat_innings"] - item["not_outs"])
        sr = item["runs"] / item["balls_faced"] * 100 if item["balls_faced"] else 0.0
        bat_avg = item["runs"] / dismissals if dismissals else 0.0
        overs = item["balls_bowled"] / BALLS_PER_OVER
        economy = item["runs_conceded"] / overs if overs else 0.0
        bowl_sr = item["balls_bowled"] / item["wickets"] if item["wickets"] else 0.0
        rank = 1 + sum(1 for other_id, score in scores.items()
                       if score > scores.get(uid, 50.0))
        report = (
            f"**{self._name(ctx.guild, uid)} — Performance Rating**\n"
            f"**{scores[uid]:.1f}/100**  •  League percentile rank **#{rank}/{len(scores)}**\n\n"
            f"**Batting**\n"
            f"Runs: **{item['runs']}** • Balls: **{item['balls_faced']}** • Innings: **{item['bat_innings']}**\n"
            f"Average: **{bat_avg:.2f}** • Strike rate: **{sr:.2f}**\n"
            f"Not outs: **{item['not_outs']}** • Ducks: **{item['ducks']}**\n\n"
            f"**Bowling**\n"
            f"Wickets: **{item['wickets']}** • Overs: **{overs:.1f}** • Runs conceded: **{item['runs_conceded']}**\n"
            f"Economy: **{economy:.2f}** • Bowling strike rate: **{bowl_sr:.2f}**\n"
            f"Bowling spells: **{item['bowl_spells']}**\n\n"
            f"**Fielding**\n"
            f"Catches: **{catches}** • Dropped catches: **{drops}**\n"
            f"Catch success: **{(catches / (catches + drops) * 100) if catches + drops else 0:.1f}%**\n\n"
            f"<sub>Rating equally weighs available batting, bowling and fielding indicators against the league. "
            f"Only stats currently stored by the bot are included; run-outs, stumpings and unrecorded events "
            f"cannot be scored.</sub>"
        )
        await ctx.reply(report, mention_author=False,
                        allowed_mentions=discord.AllowedMentions.none())

    async def _leaderboard(self, ctx, market, S):
        guild = ctx.guild
        accounts = self.bot.db.execute("SELECT user_id, cash FROM stock_accounts WHERE guild_id=?", (guild.id,)).fetchall()
        if not accounts:
            return await ctx.reply("Nobody has traded yet. Start with `+buy`.", mention_author=False)
        worth = sorted(((self._worth(guild.id, int(a["user_id"]), market, int(a["cash"]))[0], int(a["user_id"]))
                        for a in accounts), key=lambda item: (-item[0], item[1]))[:10]
        wait = on_cooldown(ctx.author.id)
        if wait:
            return await ctx.reply(f"Please wait {wait:.0f}s before drawing another image.", mention_author=False)
        async with ctx.typing():
            avatars = await self._avatars(guild, {uid for _w, uid in worth})
            rows = [{"name": self._name(guild, uid), "avatar": avatars.get(uid), "points": int(w),
                     "note": f"{len(self._holdings(guild.id, uid))} HOLDINGS"} for w, uid in worth]
            data = {"guild": guild.name, "title": "RICHEST TRADERS", "subtitle": "NET WORTH  •  CASH + SHARES",
                    "unit": "COINS", "footer": "GOOGLI MANAGER  •  STOCK EXCHANGE", "rows": rows}
            await self._send_png(ctx, await self._draw(render_board_png, data), "traders.png")

    # ------------------------------------------------------------------ +portfolio
    @commands.command(name="portfolio", aliases=["holdings", "port"])
    @commands.guild_only()
    async def portfolio(self, ctx, member: discord.Member = None):
        """Your holdings, profit and loss (or another player's)."""
        guild, member = ctx.guild, member or ctx.author
        market, S = self._market(guild.id)
        if not market:
            return await self._empty(ctx)
        wait = on_cooldown(ctx.author.id)
        if wait:
            return await ctx.reply(f"Please wait {wait:.0f}s before drawing another image.", mention_author=False)
        tickers = self._tickers(guild, market)
        acct = self._account(guild.id, member.id, S)
        self.bot.db.commit()
        cash = int(acct["cash"])
        holdings = sorted(self._holdings(guild.id, member.id),
                          key=lambda h: (-int(h["shares"]) * market.get(int(h["player_id"]), {}).get("price", 0),
                                         int(h["player_id"])))
        networth, invested = self._worth(guild.id, member.id, market, cash)
        paid = sum(int(h["cost"]) for h in holdings)
        everyone = self.bot.db.execute("SELECT user_id, cash FROM stock_accounts WHERE guild_id=?", (guild.id,)).fetchall()
        richer = sum(1 for a in everyone
                     if self._worth(guild.id, int(a["user_id"]), market, int(a["cash"]))[0] > networth)
        pages = max(1, (len(holdings) + PAGE_PORTFOLIO - 1) // PAGE_PORTFOLIO)
        values = [(self._name(guild, int(h["player_id"])), int(h["shares"]) * market.get(int(h["player_id"]), {}).get("price", 0))
                  for h in holdings]
        alloc = values[:5]
        if len(values) > 5:
            alloc.append(("Other players", sum(v for _n, v in values[5:])))
        if cash > 0:
            alloc.append(("Cash", cash))
        shares = sum(int(h["shares"]) for h in holdings)
        async with ctx.typing():
            try:
                my_avatar = (await self._avatars(guild, {member.id})).get(member.id)

                async def build(index):
                    chunk = holdings[index * PAGE_PORTFOLIO:(index + 1) * PAGE_PORTFOLIO]
                    avatars = await self._avatars(guild, {int(h["player_id"]) for h in chunk})
                    rows = []
                    for h in chunk:
                        uid, n = int(h["player_id"]), int(h["shares"])
                        price = market.get(uid, {}).get("price", 0)
                        rows.append({"name": self._name(guild, uid), "ticker": tickers.get(uid, "????"),
                                     "avatar": avatars.get(uid), "shares": n, "price": price, "value": n * price,
                                     "pl": n * price - int(h["cost"]),
                                     "spark": market.get(uid, {}).get("history", [])[-8:]})
                    return await self._draw(render_portfolio_png, {
                        "guild": guild.name, "user": {"name": member.name, "avatar": my_avatar}, "rank": richer + 1,
                        "players": len(everyone), "cash": cash, "worth": invested, "paid": paid, "pl": invested - paid,
                        "shares": shares, "count": len(holdings), "page": index + 1, "pages": pages,
                        "max": S["max_per_user"], "alloc": alloc, "rows": rows})

                await self._send_pages(ctx, pages, build, "portfolio.png")
            except Exception as exc:
                import traceback
                traceback.print_exc()
                print(f"[stocks] portfolio failed: {type(exc).__name__}: {exc}", flush=True)
                await ctx.reply("I couldn't draw the portfolio. Check the bot logs.", mention_author=False)

    # ------------------------------------------------------------------ +wallet
    @commands.command(name="wallet", aliases=["coins", "balance", "bal", "mywallet"])
    @commands.guild_only()
    async def wallet(self, ctx, member: discord.Member = None):
        """Coins, shares worth, profit and loss, fees and trades (yours or another player's)."""
        guild, member = ctx.guild, member or ctx.author
        market, S = self._market(guild.id)
        wait = on_cooldown(ctx.author.id)
        if wait:
            return await ctx.reply(f"Please wait {wait:.0f}s before drawing another image.", mention_author=False)
        acct = self._account(guild.id, member.id, S)
        self.bot.db.commit()
        cash = int(acct["cash"])
        holdings = self._holdings(guild.id, member.id)
        worth = paid = 0
        best = worst = None
        for h in holdings:
            uid = int(h["player_id"])
            value = int(h["shares"]) * market.get(uid, {}).get("price", 0)
            pl = value - int(h["cost"])
            worth += value
            paid += int(h["cost"])
            if best is None or pl > best[1]:
                best = (uid, pl)
            if worst is None or pl < worst[1]:
                worst = (uid, pl)
        net = cash + worth
        everyone = self.bot.db.execute("SELECT user_id, cash FROM stock_accounts WHERE guild_id=?", (guild.id,)).fetchall()
        richer = sum(1 for a in everyone if self._worth(guild.id, int(a["user_id"]), market, int(a["cash"]))[0] > net)
        async with ctx.typing():
            try:
                avatar = (await self._avatars(guild, {member.id})).get(member.id)
                png = await self._draw(render_wallet_png, {
                    "guild": guild.name, "user": {"name": member.name, "avatar": avatar}, "rank": richer + 1,
                    "players": len(everyone), "cash": cash, "worth": worth, "paid": paid, "net": net,
                    "start": S["start_cash"], "unrealized": worth - paid, "realized": int(acct["realized"]),
                    "fees": int(acct["fees"]), "trades": int(acct["trades"]), "holdings": len(holdings),
                    "best": (self._name(guild, best[0]), best[1]) if best else None,
                    "worst": (self._name(guild, worst[0]), worst[1]) if worst else None})
                await self._send_png(ctx, png, "wallet.png")
            except Exception as exc:
                import traceback
                traceback.print_exc()
                print(f"[stocks] wallet failed: {type(exc).__name__}: {exc}", flush=True)
                await ctx.reply("I couldn't draw the wallet. Check the bot logs.", mention_author=False)

    # ------------------------------------------------------------------ +stockset / +stockreset
    @commands.command(name="stockset", aliases=["stocksettings", "setstock", "stockconfig"])
    @commands.guild_only()
    async def stockset(self, ctx, name: str = "", *, value: str = ""):
        """(admins) Show or change the stock market settings: +stockset, +stockset base 40000, +stockset default."""
        if not await owner_or_whitelisted(ctx, command_name="stockset", app_owner=True):
            return await ctx.reply("Only the bot owner or someone trusted in this server can change stock settings.",
                                   mention_author=False)
        db, gid = self.bot.db, ctx.guild.id
        key = ALIASES.get(name.casefold().replace(" ", ""), name.casefold())
        if name.casefold() in ("default", "defaults", "reset"):
            db.execute("DELETE FROM stock_settings WHERE guild_id=?", (gid,))
            db.commit()
            return await ctx.reply("Stock settings are back to the defaults. Prices were recalculated.", mention_author=False)
        if key in SETTINGS:
            dbkey, default, lo, hi, text = SETTINGS[key]
            try:
                number = float(value.replace(",", "").replace("_", "").rstrip("%"))
            except ValueError:
                return await ctx.reply(f"Give a number, for example `+stockset {key} {default}`.", mention_author=False)
            if dbkey not in FLOAT_KEYS:
                number = int(round(number))
            if not lo <= number <= hi:
                return await ctx.reply(f"`{key}` must be between {lo:,} and {hi:,}.", mention_author=False)
            db.execute("INSERT INTO stock_settings (guild_id, key, value) VALUES (?,?,?) "
                       "ON CONFLICT(guild_id, key) DO UPDATE SET value=excluded.value", (gid, dbkey, number))
            db.commit()
            extra = " Only new traders get this." if key == "start" else \
                " Every price was recalculated." if key in PRICE_KEYS else ""
            return await ctx.reply(f"✅ **{key}** is now **{number:,.10g}** ({text}).{extra}", mention_author=False)
        if name:
            return await ctx.reply(f"I don't know a setting called `{name}`. Run `+stockset` to see the list.",
                                   mention_author=False)
        S = self._settings(gid)
        lines = [f"`{k:<9}` **{S[v[0]]:,.10g}** — {v[4]}" for k, v in SETTINGS.items()]
        await ctx.reply("**Stock market settings**\n" + "\n".join(lines) +
                        "\n\nChange one with `+stockset <name> <value>` (for example `+stockset base 40000`). "
                        "`+stockset default` restores everything. Price settings recalculate all prices at once; "
                        "use `+stockreset confirm` to give everyone a fresh start.", mention_author=False)

    @commands.command(name="addbal", aliases=["addstockbal", "addstockcash"])
    @commands.guild_only()
    async def addbal(self, ctx, member: discord.Member = None, amount: str = ""):
        """Add coins to a member stock wallet: +addbal @user 50000."""
        if not await owner_or_whitelisted(ctx, command_name="addbal", app_owner=True):
            return await ctx.reply("Only the bot owner or someone trusted in this server can add stock balance.", mention_author=False)
        if member is None or not amount:
            return await ctx.reply("Usage: `+addbal @user <positive amount>`.", mention_author=False)
        try:
            value = int(amount.replace(",", "").replace("_", ""))
        except ValueError:
            value = 0
        if value <= 0:
            return await ctx.reply("Amount must be a positive whole number.", mention_author=False)
        db, gid = self.bot.db, ctx.guild.id
        self._account(gid, member.id, self._settings(gid))
        db.execute("UPDATE stock_accounts SET cash=cash+? WHERE guild_id=? AND user_id=?", (value, gid, member.id))
        db.commit()
        cash = db.execute("SELECT cash FROM stock_accounts WHERE guild_id=? AND user_id=?", (gid, member.id)).fetchone()["cash"]
        await ctx.reply(f"✅ Added **{value:,}** coins to {member.mention} stock wallet. New balance: **{int(cash):,}**.", mention_author=False, allowed_mentions=discord.AllowedMentions.none())

    @commands.command(name="removebal", aliases=["removestockbal", "removestockcash"])
    @commands.guild_only()
    async def removebal(self, ctx, member: discord.Member = None, amount: str = ""):
        """Remove coins from a member stock wallet: +removebal @user 50000."""
        if not await owner_or_whitelisted(ctx, command_name="removebal", app_owner=True):
            return await ctx.reply("Only the bot owner or someone trusted in this server can remove stock balance.", mention_author=False)
        if member is None or not amount:
            return await ctx.reply("Usage: `+removebal @user <positive amount>`.", mention_author=False)
        try:
            value = int(amount.replace(",", "").replace("_", ""))
        except ValueError:
            value = 0
        if value <= 0:
            return await ctx.reply("Amount must be a positive whole number.", mention_author=False)
        db, gid = self.bot.db, ctx.guild.id
        self._account(gid, member.id, self._settings(gid))
        row = db.execute("SELECT cash FROM stock_accounts WHERE guild_id=? AND user_id=?", (gid, member.id)).fetchone()
        current = int(row["cash"])
        if value > current:
            return await ctx.reply(f"{member.mention} only has **{current:,}** coins. Cannot remove **{value:,}**.", mention_author=False, allowed_mentions=discord.AllowedMentions.none())
        db.execute("UPDATE stock_accounts SET cash=cash-? WHERE guild_id=? AND user_id=?", (value, gid, member.id))
        db.commit()
        await ctx.reply(f"✅ Removed **{value:,}** coins from {member.mention} stock wallet. New balance: **{current-value:,}**.", mention_author=False, allowed_mentions=discord.AllowedMentions.none())
    @commands.command(name="stockreset", aliases=["resetstocks", "resetstockmarket"])
    @commands.guild_only()
    async def stockreset(self, ctx, confirm: str = ""):
        """(admins) Wipe every trader's coins and shares in this server."""
        if not await owner_or_whitelisted(ctx, command_name="stockreset", app_owner=True):
            return await ctx.reply("Only the bot owner or someone trusted in this server can reset the market.",
                                   mention_author=False)
        if confirm.casefold() != "confirm":
            return await ctx.reply("This deletes every account and holding in this server. "
                                   "Run `+stockreset confirm` to do it.", mention_author=False)
        db = self.bot.db
        db.execute("DELETE FROM stock_accounts WHERE guild_id=?", (ctx.guild.id,))
        db.execute("DELETE FROM stock_holdings WHERE guild_id=?", (ctx.guild.id,))
        db.commit()
        await ctx.reply("🧹 The market was reset: every trader starts fresh.", mention_author=False)

    # ------------------------------------------------------------------ +buy / +sell
    def _parse_trade(self, text, verb):
        """(player query, amount or 'all', error)."""
        parts = text.split()
        if len(parts) < 2:
            return None, None, f"Usage: `+{verb} <player> <amount|all>`, for example `+{verb} Moonlight 2`."
        amount = parts[-1].casefold()
        if amount in ("all", "max"):
            return " ".join(parts[:-1]), "all", None
        if not amount.isdigit() or int(amount) <= 0:
            return None, None, f"The amount must be a whole number or `all`. Example: `+{verb} Moonlight 2`."
        return " ".join(parts[:-1]), int(amount), None

    @commands.command(name="buy", aliases=["buystock"])
    @commands.guild_only()
    async def buy(self, ctx, *, text: str = ""):
        """Buy shares of a player: +buy <player> <amount|all>."""
        query, amount, error = self._parse_trade(text, "buy")
        if error:
            return await ctx.reply(error, mention_author=False)
        guild, db = ctx.guild, self.bot.db
        market, S = self._market(guild.id)
        if not market:
            return await self._empty(ctx)
        tickers = self._tickers(guild, market)
        uid, error = self._find(guild, market, tickers, query)
        if error:
            return await ctx.reply(error, mention_author=False, allowed_mentions=discord.AllowedMentions.none())
        price, fee_pct = market[uid]["price"], S["fee_pct"]
        acct = self._account(guild.id, ctx.author.id, S)
        cash = int(acct["cash"])
        held = db.execute("SELECT shares, cost FROM stock_holdings WHERE guild_id=? AND user_id=? AND player_id=?",
                          (guild.id, ctx.author.id, uid)).fetchone()
        owned = int(held["shares"]) if held else 0
        left = max(0, S["supply"] - self._held_totals(guild.id).get(uid, 0))
        room = min(S["max_per_user"] - owned, left)
        name = f"{tickers[uid]} ({self._name(guild, uid)})"
        if room <= 0:
            db.commit()
            return await ctx.reply(f"You already hold the maximum of {S['max_per_user']} shares of {name}." if
                                   owned >= S["max_per_user"] else f"All {S['supply']} shares of {name} are taken. "
                                   "Wait for someone to sell.", mention_author=False,
                                   allowed_mentions=discord.AllowedMentions.none())
        if amount == "all":
            amount = room
            while amount > 0 and price * amount + fee_for(price * amount, fee_pct) > cash:
                amount -= 1
        if amount <= 0:
            db.commit()
            return await ctx.reply("You can't afford a single share.", mention_author=False)
        if amount > room:
            db.commit()
            why = (f"you can hold {S['max_per_user']} at most (you have {owned})" if S["max_per_user"] - owned <= left
                   else f"only {left} are left")
            return await ctx.reply(f"You can't buy {amount} of {name}: {why}.", mention_author=False,
                                   allowed_mentions=discord.AllowedMentions.none())
        cost = price * amount
        fee = fee_for(cost, fee_pct)
        if cost + fee > cash:
            db.commit()
            return await ctx.reply(f"You need **{cost + fee:,}** coins (price {cost:,} + fee {fee:,}) but have **{cash:,}**.",
                                   mention_author=False)
        db.execute("UPDATE stock_accounts SET cash=cash-?, fees=fees+?, trades=trades+1 WHERE guild_id=? AND user_id=?",
                   (cost + fee, fee, guild.id, ctx.author.id))
        db.execute("INSERT INTO stock_holdings (guild_id, user_id, player_id, shares, cost) VALUES (?,?,?,?,?) "
                   "ON CONFLICT(guild_id, user_id, player_id) DO UPDATE SET shares=shares+excluded.shares, "
                   "cost=cost+excluded.cost", (guild.id, ctx.author.id, uid, amount, cost + fee))
        db.commit()
        total_cost = (int(held["cost"]) if held else 0) + cost + fee
        left_after = max(0, S["supply"] - self._held_totals(guild.id).get(uid, 0))
        text = (f"📈 Bought **{amount:,} × {tickers[uid]}** ({self._name(guild, uid)}) at **{price:,}** "
                f"for **{cost:,}** + {fee:,} fee ({fee_pct:g}%). Coins left: **{cash - cost - fee:,}**.")
        await self._receipt(ctx, uid, {
            "kind": "buy", "guild": guild.name, "name": self._name(guild, uid), "ticker": tickers[uid],
            "amount": amount, "price": price, "subtotal": cost, "fee": fee, "fee_pct": fee_pct, "total": cost + fee,
            "cash": cash - cost - fee, "owned": owned + amount, "max": S["max_per_user"],
            "avg": total_cost / (owned + amount), "left": left_after, "supply": S["supply"]}, text)

    async def _receipt(self, ctx, uid, data, text):
        """The trade card; plain text if the picture cannot be drawn."""
        try:
            data["avatar"] = (await self._avatars(ctx.guild, {uid})).get(uid)
            png = await self._draw(render_trade_png, data)
            await self._send_png(ctx, png, "trade.png")
        except Exception as exc:
            print(f"[stocks] receipt failed: {type(exc).__name__}: {exc}", flush=True)
            await ctx.reply(text, mention_author=False, allowed_mentions=discord.AllowedMentions.none())

    @commands.command(name="sell", aliases=["sellstock"])
    @commands.guild_only()
    async def sell(self, ctx, *, text: str = ""):
        """Sell shares of a player: +sell <player> <amount|all>."""
        query, amount, error = self._parse_trade(text, "sell")
        if error:
            return await ctx.reply(error, mention_author=False)
        guild, db = ctx.guild, self.bot.db
        market, S = self._market(guild.id)
        if not market:
            return await self._empty(ctx)
        tickers = self._tickers(guild, market)
        uid, error = self._find(guild, market, tickers, query)
        if error:
            return await ctx.reply(error, mention_author=False, allowed_mentions=discord.AllowedMentions.none())
        held = db.execute("SELECT shares, cost FROM stock_holdings WHERE guild_id=? AND user_id=? AND player_id=?",
                          (guild.id, ctx.author.id, uid)).fetchone()
        owned = int(held["shares"]) if held else 0
        if owned <= 0:
            return await ctx.reply(f"You don't own any **{tickers[uid]}**.", mention_author=False)
        amount = owned if amount == "all" else amount
        if amount > owned:
            return await ctx.reply(f"You only own **{owned:,}** shares of {tickers[uid]}.", mention_author=False)
        price = market[uid]["price"]
        gross = price * amount
        fee = fee_for(gross, S["fee_pct"])
        basis = int(round(int(held["cost"]) * amount / owned))
        net = gross - fee
        self._account(guild.id, ctx.author.id, S)
        profit = net - basis
        db.execute("UPDATE stock_accounts SET cash=cash+?, realized=realized+?, fees=fees+?, trades=trades+1 "
                   "WHERE guild_id=? AND user_id=?", (net, profit, fee, guild.id, ctx.author.id))
        if amount == owned:
            db.execute("DELETE FROM stock_holdings WHERE guild_id=? AND user_id=? AND player_id=?",
                       (guild.id, ctx.author.id, uid))
        else:
            db.execute("UPDATE stock_holdings SET shares=shares-?, cost=cost-? WHERE guild_id=? AND user_id=? AND player_id=?",
                       (amount, basis, guild.id, ctx.author.id, uid))
        db.commit()
        acct = self._account(guild.id, ctx.author.id, S)
        cash = int(acct["cash"])
        text = (f"{'📈' if profit >= 0 else '📉'} Sold **{amount:,} × {tickers[uid]}** ({self._name(guild, uid)}) at "
                f"**{price:,}** for **{gross:,}** − {fee:,} fee. "
                f"{'Profit' if profit >= 0 else 'Loss'}: **{abs(profit):,}**. Coins: **{cash:,}**.")
        await self._receipt(ctx, uid, {
            "kind": "sell", "guild": guild.name, "name": self._name(guild, uid), "ticker": tickers[uid],
            "amount": amount, "price": price, "subtotal": gross, "fee": fee, "fee_pct": S["fee_pct"], "total": net,
            "cash": cash, "owned": owned - amount, "max": S["max_per_user"], "basis": basis, "profit": profit,
            "profit_pct": profit / basis * 100 if basis else 0.0, "realized": int(acct["realized"]),
            "left": max(0, S["supply"] - self._held_totals(guild.id).get(uid, 0)), "supply": S["supply"]}, text)


async def setup(bot):
    await bot.add_cog(Stocks(bot))