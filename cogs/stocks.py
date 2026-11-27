"""Googli stock exchange: every player with imported stats is a stock whose price follows their form.

+stocks                    the market: top gainers, most valuable players, top losers
+stocks <player>           one stock: price, chart over the last matches, your position
+stocks lb                 richest traders (cash + value of shares)
+stocks daily              claim your daily coins
+buy <player> <amount|all>
+sell <player> <amount|all>
+portfolio [@user]         cash, shares, profit and loss

How a price is made
-------------------
Each match a player has played gives a fantasy-point score (the same one the fantasy leaderboard uses).
Form is a moving average of those scores that leans on the latest matches (35% weight on the newest),
pulled towards the league average for players with only a few matches. The price is the form compared with
the league average, times 100: an average player trades at 100, a player in a hot streak at 150 or more.
Prices only change when new match stats are imported, so there is no live work and nothing to refresh.
Trading costs a 1% fee, everybody starts with 10,000 coins and can claim 500 more once a day.
"""
import asyncio
import io
import re
import time

import discord
from discord.ext import commands

from .h2h import fetch_avatar, on_cooldown, render_png
from .predict_image import render_board_png
from .stocks_image import (DOWN, GOLD, UP, render_market_png, render_portfolio_png, render_stock_png)

START_CASH = 10_000
DAILY_COINS = 500
DAILY_WAIT = 20 * 3600
FEE = 0.01
MAX_SHARES = 500             # per player, per trader
EMA_WEIGHT = 0.35
MIN_PRICE = 10
MAX_PRICE = 1000


def fee_for(amount):
    return max(1, int(round(amount * FEE)))


def build_prices(rows_by_player, league_avg):
    """{uid: {price, prev, change, history, form, matches, high, low}} from per-match fantasy scores (oldest first)."""
    out = {}
    for uid, scores in rows_by_player.items():
        history, ema = [], None
        for k, score in enumerate(scores, 1):
            ema = score if ema is None else EMA_WEIGHT * score + (1 - EMA_WEIGHT) * ema
            form = (ema * k + 2 * league_avg) / (k + 2)
            history.append(int(max(MIN_PRICE, min(MAX_PRICE, round(100 * form / league_avg)))))
        prev = history[-2] if len(history) > 1 else 100
        out[uid] = {"price": history[-1], "prev": prev, "change": (history[-1] - prev) / prev * 100,
                    "history": history, "form": history[-1] / 100, "matches": len(history),
                    "high": max(history), "low": min(history)}
    return out


class Stocks(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._lock = asyncio.Lock()
        self._cache = {}            # guild id -> (version, market)
        db = bot.db
        db.execute("CREATE TABLE IF NOT EXISTS stock_accounts (guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, "
                   "cash INTEGER NOT NULL, last_daily REAL NOT NULL DEFAULT 0, PRIMARY KEY (guild_id, user_id))")
        db.execute("CREATE TABLE IF NOT EXISTS stock_holdings (guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, "
                  "player_id INTEGER NOT NULL, shares INTEGER NOT NULL, cost INTEGER NOT NULL, "
                  "PRIMARY KEY (guild_id, user_id, player_id))")
        db.commit()

    @property
    def compare(self):
        return self.bot.get_cog("CompareTeams")

    # ------------------------------------------------------------------ market data
    def _market(self, guild_id):
        """{uid: stock dict} for every player with stats; rebuilt only when new stats were imported."""
        db = self.bot.db
        version = db.execute("SELECT COUNT(*), COALESCE(MAX(imported_at), '') FROM stats_imports WHERE guild_id=?",
                             (int(guild_id),)).fetchone()
        version = (version[0], str(version[1]))
        hit = self._cache.get(int(guild_id))
        if hit and hit[0] == version:
            return hit[1]
        from .stats import fantasy_raw_points
        rows = db.execute(
            "SELECT p.user_id,p.runs,p.balls_faced,p.runs_conceded,p.balls_bowled,p.wickets,p.not_out "
            "FROM player_match_stats p JOIN stats_imports i ON i.source_message_id=p.source_message_id "
            "WHERE i.guild_id=? AND COALESCE(i.competition,'MAIN TOURNAMENT')='MAIN TOURNAMENT' "
            "ORDER BY i.imported_at, p.source_message_id", (int(guild_id),)).fetchall()
        scores, everything = {}, []
        for row in rows:
            value = max(0.0, float(fantasy_raw_points([dict(row)])[2]))
            scores.setdefault(int(row["user_id"]), []).append(value)
            everything.append(value)
        league_avg = (sum(everything) / len(everything)) if everything else 0
        market = build_prices(scores, league_avg) if league_avg > 0 else {}
        self._cache = {int(guild_id): (version, market)}      # one server at a time keeps memory small
        return market

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
    def _account(self, guild_id, user_id):
        db = self.bot.db
        db.execute("INSERT OR IGNORE INTO stock_accounts (guild_id, user_id, cash) VALUES (?,?,?)",
                   (int(guild_id), int(user_id), START_CASH))
        return db.execute("SELECT cash, last_daily FROM stock_accounts WHERE guild_id=? AND user_id=?",
                          (int(guild_id), int(user_id))).fetchone()

    def _holdings(self, guild_id, user_id):
        return self.bot.db.execute("SELECT player_id, shares, cost FROM stock_holdings WHERE guild_id=? AND user_id=? "
                                   "AND shares>0", (int(guild_id), int(user_id))).fetchall()

    def _worth(self, guild_id, user_id, market, cash=None):
        """(net worth, value of shares)."""
        if cash is None:
            cash = self._account(guild_id, user_id)["cash"]
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

    async def _send_image(self, ctx, fn, data, filename):
        async with self._lock:
            png = await render_png(fn, data)
        await ctx.send(file=discord.File(io.BytesIO(png), filename=filename))

    # ------------------------------------------------------------------ +stocks
    @commands.command(name="stocks", aliases=["stock", "market", "exchange"])
    @commands.guild_only()
    async def stocks(self, ctx, *, text: str = ""):
        """The player stock market: +stocks, +stocks <player>, +stocks lb, +stocks daily."""
        guild, text = ctx.guild, text.strip()
        word = text.casefold()
        if word in ("daily", "claim", "bonus"):
            return await self._daily(ctx)
        if word in ("help", "guide", "how"):
            return await ctx.reply(
                "**Googli Stock Exchange**\nEvery player with match stats is a stock. The price follows their fantasy "
                "form: an average player trades at 100, hot players climb, cold ones fall. Prices change when new "
                "match stats are imported.\n"
                "`+stocks` market  •  `+stocks <player>` one stock  •  `+stocks lb` richest traders\n"
                "`+buy <player> <amount|all>`  •  `+sell <player> <amount|all>`  •  `+portfolio [@user]`\n"
                f"`+stocks daily` gives {DAILY_COINS:,} coins every 20 hours. You start with {START_CASH:,}. "
                "Each trade costs a 1% fee.", mention_author=False)
        market = self._market(guild.id)
        if not market:
            return await ctx.reply("The market is empty: it opens once match stats have been imported.",
                                   mention_author=False)
        if word in ("lb", "top", "leaderboard", "rich", "richest"):
            return await self._leaderboard(ctx, market)
        wait = on_cooldown(ctx.author.id)
        if wait:
            return await ctx.reply(f"Please wait {wait:.0f}s before drawing another image.", mention_author=False)
        tickers = self._tickers(guild, market)
        async with ctx.typing():
            try:
                if text:
                    uid, error = self._find(guild, market, tickers, text)
                    if error:
                        return await ctx.reply(error, mention_author=False, allowed_mentions=discord.AllowedMentions.none())
                    data = await self._stock_data(ctx, market, tickers, uid)
                    return await self._send_image(ctx, render_stock_png, data, "stock.png")
                data = await self._market_data(ctx, market, tickers)
                await self._send_image(ctx, render_market_png, data, "market.png")
            except Exception as exc:
                import traceback
                traceback.print_exc()
                print(f"[stocks] failed: {type(exc).__name__}: {exc}", flush=True)
                await ctx.reply("I couldn't draw the market. Check the bot logs.", mention_author=False)

    async def _market_data(self, ctx, market, tickers):
        guild = ctx.guild
        by_change = sorted(market, key=lambda u: (-market[u]["change"], u))
        by_price = sorted(market, key=lambda u: (-market[u]["price"], u))
        gainers, losers, top = by_change[:6], list(reversed(by_change))[:6], by_price[:6]
        avatars = await self._avatars(guild, set(gainers) | set(losers) | set(top))

        def rows(uids):
            return [{"name": self._name(guild, u), "ticker": tickers[u], "avatar": avatars.get(u),
                     "price": market[u]["price"], "change": market[u]["change"]} for u in uids]

        trend = sum(s["change"] for s in market.values()) / len(market)
        return {"guild": guild.name, "count": len(market), "trend": trend,
                "tiles": [("TOP GAINERS", UP, rows(gainers)), ("MOST VALUABLE", GOLD, rows(top)),
                          ("TOP LOSERS", DOWN, rows(losers))]}

    async def _stock_data(self, ctx, market, tickers, uid):
        guild, stock = ctx.guild, market[uid]
        rank = 1 + sum(1 for s in market.values() if s["price"] > stock["price"])
        held = next((h for h in self._holdings(guild.id, ctx.author.id) if int(h["player_id"]) == uid), None)
        holding = {"shares": int(held["shares"]), "value": int(held["shares"]) * stock["price"],
                   "cost": int(held["cost"])} if held else None
        avatars = await self._avatars(guild, {uid})
        return {"guild": guild.name, "name": self._name(guild, uid), "ticker": tickers[uid], "avatar": avatars.get(uid),
                "price": stock["price"], "change": stock["change"], "history": stock["history"], "form": stock["form"],
                "matches": stock["matches"], "high": stock["high"], "low": stock["low"], "rank": rank,
                "listed": len(market), "holding": holding}

    async def _daily(self, ctx):
        acct = self._account(ctx.guild.id, ctx.author.id)
        wait = DAILY_WAIT - (time.time() - float(acct["last_daily"]))
        if wait > 0:
            hours, minutes = int(wait // 3600), int(wait % 3600 // 60)
            self.bot.db.commit()
            return await ctx.reply(f"You already claimed. Come back in **{hours}h {minutes}m**.", mention_author=False)
        self.bot.db.execute("UPDATE stock_accounts SET cash=cash+?, last_daily=? WHERE guild_id=? AND user_id=?",
                            (DAILY_COINS, time.time(), ctx.guild.id, ctx.author.id))
        self.bot.db.commit()
        await ctx.reply(f"💰 **+{DAILY_COINS:,} coins!** You now have **{acct['cash'] + DAILY_COINS:,}**.",
                        mention_author=False)

    async def _leaderboard(self, ctx, market):
        guild = ctx.guild
        accounts = self.bot.db.execute("SELECT user_id, cash FROM stock_accounts WHERE guild_id=?", (guild.id,)).fetchall()
        if not accounts:
            return await ctx.reply("Nobody has traded yet. Start with `+stocks daily` or `+buy`.", mention_author=False)
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
            await self._send_image(ctx, render_board_png, data, "traders.png")

    # ------------------------------------------------------------------ +portfolio
    @commands.command(name="portfolio", aliases=["wallet", "holdings"])
    @commands.guild_only()
    async def portfolio(self, ctx, member: discord.Member = None):
        """Your cash, shares and profit/loss (or another player's)."""
        guild, member = ctx.guild, member or ctx.author
        market = self._market(guild.id)
        if not market:
            return await ctx.reply("The market is empty: it opens once match stats have been imported.",
                                   mention_author=False)
        wait = on_cooldown(ctx.author.id)
        if wait:
            return await ctx.reply(f"Please wait {wait:.0f}s before drawing another image.", mention_author=False)
        tickers = self._tickers(guild, market)
        acct = self._account(guild.id, member.id)
        self.bot.db.commit()
        cash = int(acct["cash"])
        holdings = sorted(self._holdings(guild.id, member.id),
                          key=lambda h: -int(h["shares"]) * market.get(int(h["player_id"]), {}).get("price", 0))
        networth, invested = self._worth(guild.id, member.id, market, cash)
        cost = sum(int(h["cost"]) for h in holdings)
        pl = invested - cost
        everyone = self.bot.db.execute("SELECT user_id, cash FROM stock_accounts WHERE guild_id=?", (guild.id,)).fetchall()
        richer = sum(1 for a in everyone
                     if self._worth(guild.id, int(a["user_id"]), market, int(a["cash"]))[0] > networth)
        async with ctx.typing():
            try:
                avatars = await self._avatars(guild, {int(h["player_id"]) for h in holdings[:7]} | {member.id})
                rows = []
                for h in holdings[:7]:
                    uid, shares = int(h["player_id"]), int(h["shares"])
                    price = market.get(uid, {}).get("price", 0)
                    rows.append({"name": self._name(guild, uid), "ticker": tickers.get(uid, "????"),
                                 "avatar": avatars.get(uid), "shares": shares, "avg": int(h["cost"]) / shares,
                                 "price": price, "value": shares * price,
                                 "pl_pct": (shares * price - int(h["cost"])) / int(h["cost"]) * 100 if h["cost"] else 0})
                data = {"guild": guild.name, "user": {"name": self._name(guild, member.id) if member.id in market
                                                      else member.name, "avatar": avatars.get(member.id)},
                        "networth": networth, "rank": richer + 1, "players": len(everyone), "cash": cash,
                        "invested": invested, "pl": pl, "pl_pct": pl / cost * 100 if cost else 0, "rows": rows}
                await self._send_image(ctx, render_portfolio_png, data, "portfolio.png")
            except Exception as exc:
                import traceback
                traceback.print_exc()
                print(f"[stocks] portfolio failed: {type(exc).__name__}: {exc}", flush=True)
                await ctx.reply("I couldn't draw the portfolio. Check the bot logs.", mention_author=False)

    # ------------------------------------------------------------------ +buy / +sell
    def _parse_trade(self, ctx, text, verb):
        """(player query, amount or 'all', error)."""
        parts = text.split()
        if len(parts) < 2:
            return None, None, f"Usage: `+{verb} <player> <amount|all>`, for example `+{verb} Moonlight 10`."
        amount = parts[-1].casefold()
        if amount in ("all", "max"):
            return " ".join(parts[:-1]), "all", None
        if not amount.isdigit() or int(amount) <= 0:
            return None, None, f"The amount must be a whole number or `all`. Example: `+{verb} Moonlight 10`."
        return " ".join(parts[:-1]), int(amount), None

    @commands.command(name="buy", aliases=["buystock"])
    @commands.guild_only()
    async def buy(self, ctx, *, text: str = ""):
        """Buy shares of a player: +buy <player> <amount|all>."""
        query, amount, error = self._parse_trade(ctx, text, "buy")
        if error:
            return await ctx.reply(error, mention_author=False)
        guild, db = ctx.guild, self.bot.db
        market = self._market(guild.id)
        if not market:
            return await ctx.reply("The market is empty: it opens once match stats have been imported.",
                                   mention_author=False)
        tickers = self._tickers(guild, market)
        uid, error = self._find(guild, market, tickers, query)
        if error:
            return await ctx.reply(error, mention_author=False, allowed_mentions=discord.AllowedMentions.none())
        price = market[uid]["price"]
        acct = self._account(guild.id, ctx.author.id)
        cash = int(acct["cash"])
        held = db.execute("SELECT shares, cost FROM stock_holdings WHERE guild_id=? AND user_id=? AND player_id=?",
                          (guild.id, ctx.author.id, uid)).fetchone()
        owned = int(held["shares"]) if held else 0
        room = MAX_SHARES - owned
        if amount == "all":
            amount = int(cash / (price * (1 + FEE)))
            while amount > 0 and price * amount + fee_for(price * amount) > cash:
                amount -= 1
            amount = min(amount, room)
        if amount <= 0:
            db.commit()
            return await ctx.reply("You can't afford a single share." if room > 0 else
                                   f"You already hold the maximum of {MAX_SHARES} shares of this player.",
                                   mention_author=False)
        if amount > room:
            db.commit()
            return await ctx.reply(f"You can hold at most {MAX_SHARES} shares of one player (you have {owned}).",
                                   mention_author=False)
        cost = price * amount
        fee = fee_for(cost)
        if cost + fee > cash:
            db.commit()
            return await ctx.reply(f"You need **{cost + fee:,}** coins (price {cost:,} + fee {fee:,}) but have **{cash:,}**.",
                                   mention_author=False)
        db.execute("UPDATE stock_accounts SET cash=cash-? WHERE guild_id=? AND user_id=?", (cost + fee, guild.id, ctx.author.id))
        db.execute("INSERT INTO stock_holdings (guild_id, user_id, player_id, shares, cost) VALUES (?,?,?,?,?) "
                   "ON CONFLICT(guild_id, user_id, player_id) DO UPDATE SET shares=shares+excluded.shares, "
                   "cost=cost+excluded.cost", (guild.id, ctx.author.id, uid, amount, cost + fee))
        db.commit()
        await ctx.reply(f"📈 Bought **{amount:,} × {tickers[uid]}** ({self._name(guild, uid)}) at **{price:,}** "
                        f"for **{cost:,}** + {fee:,} fee. Cash left: **{cash - cost - fee:,}**.",
                        mention_author=False, allowed_mentions=discord.AllowedMentions.none())

    @commands.command(name="sell", aliases=["sellstock"])
    @commands.guild_only()
    async def sell(self, ctx, *, text: str = ""):
        """Sell shares of a player: +sell <player> <amount|all>."""
        query, amount, error = self._parse_trade(ctx, text, "sell")
        if error:
            return await ctx.reply(error, mention_author=False)
        guild, db = ctx.guild, self.bot.db
        market = self._market(guild.id)
        if not market:
            return await ctx.reply("The market is empty: it opens once match stats have been imported.",
                                   mention_author=False)
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
        fee = fee_for(gross)
        basis = int(round(int(held["cost"]) * amount / owned))
        net = gross - fee
        self._account(guild.id, ctx.author.id)
        db.execute("UPDATE stock_accounts SET cash=cash+? WHERE guild_id=? AND user_id=?", (net, guild.id, ctx.author.id))
        if amount == owned:
            db.execute("DELETE FROM stock_holdings WHERE guild_id=? AND user_id=? AND player_id=?",
                       (guild.id, ctx.author.id, uid))
        else:
            db.execute("UPDATE stock_holdings SET shares=shares-?, cost=cost-? WHERE guild_id=? AND user_id=? AND player_id=?",
                       (amount, basis, guild.id, ctx.author.id, uid))
        db.commit()
        profit = net - basis
        cash = int(self._account(guild.id, ctx.author.id)["cash"])
        await ctx.reply(f"{'📈' if profit >= 0 else '📉'} Sold **{amount:,} × {tickers[uid]}** ({self._name(guild, uid)}) at "
                        f"**{price:,}** for **{gross:,}** − {fee:,} fee. "
                        f"{'Profit' if profit >= 0 else 'Loss'}: **{abs(profit):,}**. Cash: **{cash:,}**.",
                        mention_author=False, allowed_mentions=discord.AllowedMentions.none())


async def setup(bot):
    await bot.add_cog(Stocks(bot))
