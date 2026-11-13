"""Batter-side catch tracking for GOOGLI MANAGER.

For every Hand Cricket "CHANCE OF WICKET!" this records whose catch it was (the BATTER)
and whether it was TAKEN (batter out) or DROPPED (batter survived). +lb and +stats read it.

* Live: every new result message in a +starttracking channel is recorded automatically.
* Backfill: +scanbatcatches [hours] [apply] replays old Hand Cricket messages from the
  tracked channels. Events are keyed by the result message id, so scanning twice, or
  scanning messages the live tracker already saw, never double counts.

It does not touch batfds.py, fielding.py or any existing table. New table: batter_catch_events.
Add "cogs.batcatches" to the extension list in bot.py (right after "cogs.batfds").
"""
from __future__ import annotations

import io
import logging
import sqlite3
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands

from .batfds import (
    BOT_OWNER_ID,
    HANDCRICKET_BOT_ID,
    PENDING_TTL_SECONDS,
    find_member_id,
    message_text,
    parse_opportunity,
)

log = logging.getLogger(__name__)


def _message_kind(message: discord.Message):
    """'chance', 'dropped', 'taken' or None for one Hand Cricket message."""
    upper = message_text(message).upper()
    if "CHANCE OF WICKET!" in upper:
        return "chance"
    if "BOZO DROPPED THE CATCH" in upper:
        return "dropped"
    if "TOOK THE CATCH" in upper:
        return "taken"
    return None


def replay_batter_catches(messages, resolve):
    """Replay Hand Cricket messages (oldest first).

    resolve(name) -> user id or None.
    Returns [(result_message_id, channel_id, batter_id, result, timestamp), ...].
    """
    events = []
    pending = {}  # channel_id -> (batter_id, timestamp, chance_message_id)
    for m in messages:
        kind = _message_kind(m)
        if kind is None:
            continue
        cid = m.channel.id
        ts = m.created_at.timestamp() if getattr(m, "created_at", None) else 0.0
        if kind == "chance":
            pending.pop(cid, None)
            opp = parse_opportunity(m)
            if opp:
                batter = opp[0] if opp[0] else (resolve(opp[1]) if opp[1] else None)
                if batter:
                    pending[cid] = (int(batter), ts, m.id)
            continue
        pend = pending.pop(cid, None)
        if pend and m.id > pend[2] and ts - pend[1] <= PENDING_TTL_SECONDS:
            events.append((m.id, cid, pend[0], kind, ts))
    return events


class BatterCatches(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._pending: dict[int, tuple[int, float, int]] = {}
        bot.db.execute("""
            CREATE TABLE IF NOT EXISTS batter_catch_events (
                message_id INTEGER PRIMARY KEY,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                result TEXT NOT NULL CHECK (result IN ('taken', 'dropped')),
                created_at REAL NOT NULL)
        """)
        bot.db.execute(
            "CREATE INDEX IF NOT EXISTS batter_catch_events_guild_user "
            "ON batter_catch_events (guild_id, user_id)"
        )
        bot.db.commit()

    # ------------------------------------------------------------------ helpers
    def _tracking_enabled(self, guild_id: int, channel_id: int) -> bool:
        try:
            return self.bot.db.execute(
                "SELECT 1 FROM fielding_tracking_channels WHERE guild_id=? AND channel_id=?",
                (int(guild_id), int(channel_id)),
            ).fetchone() is not None
        except sqlite3.Error:
            return False

    def _seen(self, message_id: int) -> bool:
        return self.bot.db.execute(
            "SELECT 1 FROM batter_catch_events WHERE message_id=?", (int(message_id),)
        ).fetchone() is not None

    def _save(self, guild_id, channel_id, message_id, user_id, result, ts) -> bool:
        cur = self.bot.db.execute(
            "INSERT OR IGNORE INTO batter_catch_events "
            "(message_id, guild_id, channel_id, user_id, result, created_at) VALUES (?,?,?,?,?,?)",
            (int(message_id), int(guild_id), int(channel_id), int(user_id), result, float(ts)),
        )
        self.bot.db.commit()
        return cur.rowcount > 0

    def totals(self, guild_id: int) -> dict[int, tuple[int, int]]:
        """{user_id: (dropped, taken)} for this server."""
        rows = self.bot.db.execute(
            "SELECT user_id, SUM(result='dropped'), SUM(result='taken') "
            "FROM batter_catch_events WHERE guild_id=? GROUP BY user_id",
            (int(guild_id),),
        ).fetchall()
        return {int(r[0]): (int(r[1] or 0), int(r[2] or 0)) for r in rows}

    # ------------------------------------------------------------------ live tracking
    async def _handle(self, message: discord.Message):
        if message.author.id != HANDCRICKET_BOT_ID or message.guild is None:
            return
        if not self._tracking_enabled(message.guild.id, message.channel.id):
            return
        kind = _message_kind(message)
        if kind is None:
            return
        cid = message.channel.id
        ts = message.created_at.timestamp()

        if kind == "chance":
            prev = self._pending.get(cid)
            if prev and message.id < prev[2]:
                return  # an edit of an older chance message
            opp = parse_opportunity(message)
            batter = None
            if opp:
                if opp[0]:
                    batter = int(opp[0])
                elif opp[1]:
                    batter = find_member_id(message.guild, opp[1])
                    if batter is None:
                        helper = self.bot.get_cog("BatterFieldingDropStats")
                        if helper is not None:
                            try:
                                await helper._resolve_missing(message.guild, [opp[1]])
                                batter = find_member_id(message.guild, opp[1])
                            except Exception:
                                log.exception("batcatches: member lookup failed")
            if batter:
                self._pending[cid] = (int(batter), ts, message.id)
            else:
                self._pending.pop(cid, None)
            return

        pend = self._pending.get(cid)
        if not pend or message.id < pend[2] or self._seen(message.id):
            return
        self._pending.pop(cid, None)
        if ts - pend[1] > PENDING_TTL_SECONDS:
            return
        self._save(message.guild.id, cid, message.id, pend[0], kind, ts)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        try:
            await self._handle(message)
        except Exception:
            log.exception("batcatches: live tracking failed")

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        try:
            await self._handle(after)
        except Exception:
            log.exception("batcatches: live tracking failed")

    # ------------------------------------------------------------------ backfill
    @commands.command(name="scanbatcatches", aliases=["rescanbatcatches"])
    @commands.guild_only()
    async def scan_batter_catches(self, ctx: commands.Context, hours: int = 72, mode: str = "preview"):
        """Rebuild batter-side catches (taken/dropped) from old Hand Cricket messages.

        Usage: +scanbatcatches [hours] [apply]
        Reads every +starttracking channel for the last <hours> hours (max 2880).
        Without "apply" nothing is written. Safe to run repeatedly.
        """
        if ctx.author.id != BOT_OWNER_ID:
            return await ctx.reply("You are not allowed to use this command.", mention_author=False)
        apply = mode.lower() in ("apply", "confirm")
        hours = max(1, min(int(hours), 24 * 120))
        guild, db = ctx.guild, self.bot.db
        if not guild.chunked:
            try:
                await guild.chunk()
            except Exception:
                log.warning("batcatches: could not chunk the guild member list")
        channel_ids = [int(r[0]) for r in db.execute(
            "SELECT channel_id FROM fielding_tracking_channels WHERE guild_id=?", (guild.id,))]
        if not channel_ids:
            return await ctx.reply("No tracked channels. Use `+starttracking` first.", mention_author=False)

        progress = await ctx.reply(
            f"Reading the last {hours}h of {len(channel_ids)} tracked channel(s)…", mention_author=False)
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        per_channel, names = {}, set()
        for cid in channel_ids:
            channel = guild.get_channel(cid) or guild.get_thread(cid)
            if channel is None:
                try:
                    channel = await guild.fetch_channel(cid)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    continue
            msgs = []
            try:
                async for m in channel.history(after=cutoff, oldest_first=True, limit=None):
                    if m.author.id == HANDCRICKET_BOT_ID:
                        msgs.append(m)
            except (discord.Forbidden, discord.HTTPException):
                continue
            per_channel[cid] = msgs
            for m in msgs:
                if _message_kind(m) == "chance":
                    opp = parse_opportunity(m)
                    if opp and not opp[0] and opp[1]:
                        names.add(opp[1])
            try:
                await progress.edit(content=f"Read {sum(len(v) for v in per_channel.values())} Hand Cricket "
                                            f"messages from {len(per_channel)}/{len(channel_ids)} channel(s)…")
            except discord.HTTPException:
                pass

        helper = self.bot.get_cog("BatterFieldingDropStats")
        if helper is not None and names:
            try:
                await helper._resolve_missing(guild, list(names))
            except Exception:
                log.exception("batcatches: member lookup failed")
        cache = {}

        def resolve(name):
            if name not in cache:
                cache[name] = find_member_id(guild, name)
            return cache[name]

        events = []
        for msgs in per_channel.values():
            events.extend(replay_batter_catches(msgs, resolve))
        unresolved = sorted(n for n, v in cache.items() if v is None)

        known = {int(r[0]) for r in db.execute(
            "SELECT message_id FROM batter_catch_events WHERE guild_id=?", (guild.id,))}
        new = [e for e in events if e[0] not in known]
        per_user: dict[int, list[int]] = {}
        for _, _, uid, result, _ in new:
            rec = per_user.setdefault(uid, [0, 0])  # [taken, dropped]
            rec[0 if result == "taken" else 1] += 1

        lines = []
        for uid, (taken, dropped) in sorted(per_user.items(), key=lambda kv: -(kv[1][0] + kv[1][1])):
            member = guild.get_member(uid)
            lines.append(f"{member.name if member else uid} ({uid}): taken {taken}, dropped {dropped}")
        if unresolved:
            lines += ["", "Batter names that could not be matched to a member (their catches are NOT counted):"]
            lines += [f"  {n}" for n in unresolved]
        file = discord.File(io.BytesIO("\n".join(lines).encode("utf-8")), filename="batter_catches_scan.txt")

        taken_total = sum(1 for e in new if e[3] == "taken")
        summary = [
            f"**Scan of the last {hours}h:** {len(events)} batter catch result(s) found, "
            f"**{len(new)} new** ({taken_total} taken, {len(new) - taken_total} dropped) "
            f"for {len(per_user)} player(s). {len(events) - len(new)} were already recorded.",
        ]
        if unresolved:
            summary.append(f"⚠️ {len(unresolved)} name(s) could not be matched (listed in the file).")
        if not new:
            return await ctx.reply("\n".join(summary) + "\nNothing to add.", mention_author=False)
        if not apply:
            summary.append(f"Nothing was written. Back up the database, then run `+scanbatcatches {hours} apply`.")
            return await ctx.reply("\n".join(summary), file=file, mention_author=False)

        db.execute("BEGIN IMMEDIATE")
        try:
            db.executemany(
                "INSERT OR IGNORE INTO batter_catch_events "
                "(message_id, guild_id, channel_id, user_id, result, created_at) VALUES (?,?,?,?,?,?)",
                [(mid, guild.id, cid, uid, result, ts) for mid, cid, uid, result, ts in new],
            )
            db.commit()
        except Exception:
            db.rollback()
            raise
        summary.append("✅ Saved. Open `+lb` → Batting to see the new leaderboards.")
        await ctx.reply("\n".join(summary), file=file, mention_author=False)

    # ------------------------------------------------------------------ small tools
    @commands.command(name="batcatches")
    @commands.guild_only()
    async def batter_catches(self, ctx: commands.Context, member: discord.Member = None):
        """Show how many of a batter's catches were taken / dropped."""
        member = member or ctx.author
        dropped, taken = self.totals(ctx.guild.id).get(member.id, (0, 0))
        await ctx.reply(
            f"**{discord.utils.escape_markdown(member.display_name)}** — catches taken (out): `{taken}` | "
            f"catches dropped (survived): `{dropped}`",
            mention_author=False, allowed_mentions=discord.AllowedMentions.none())

    @commands.command(name="resetbatcatches")
    @commands.guild_only()
    async def reset_batter_catches(self, ctx: commands.Context, confirm: str = ""):
        """Delete all recorded batter-side catches of this server (owner only)."""
        if ctx.author.id != BOT_OWNER_ID:
            return await ctx.reply("You are not allowed to use this command.", mention_author=False)
        if confirm.lower() != "confirm":
            return await ctx.reply("This deletes every recorded batter-side catch in this server. "
                                   "Run `+resetbatcatches confirm` to continue.", mention_author=False)
        cur = self.bot.db.execute("DELETE FROM batter_catch_events WHERE guild_id=?", (ctx.guild.id,))
        self.bot.db.commit()
        await ctx.reply(f"Deleted {cur.rowcount} recorded event(s).", mention_author=False)


async def setup(bot: commands.Bot):
    await bot.add_cog(BatterCatches(bot))
