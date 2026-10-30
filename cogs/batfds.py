"""Experimental batter drop-impact tracking for Googli Manager.

Only listens in channels where +starttracking is enabled by cogs/fielding.py.
It reads Hand Cricket's Match Status embed (and ignores its other commands),
then snapshots the named batter's score when CHANCE OF WICKET! is posted.

Load with: await bot.load_extension('cogs.batfds')
"""
from __future__ import annotations

import re
import time

import discord
from discord.ext import commands

HANDCRICKET_BOT_ID = 753191385296928808
BOT_OWNER_ID = 1014568928682459198
PENDING_TTL_SECONDS = 120

# Example from the supplied Match Status embed:
# saksham9559: 33 (10) runs
BATTER_LINE = re.compile(
    r"^\s*([^:\n]{1,64})\s*:\s*(\d+)\s*\(\s*\d+\s*\)\s*runs\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def message_text(message: discord.Message) -> str:
    parts = [message.content or ""]
    for embed in message.embeds:
        parts.extend((embed.title or "", embed.description or ""))
        for field in embed.fields:
            parts.extend((field.name or "", field.value or ""))
    return "\n".join(parts)


def parse_match_status(message: discord.Message):
    """Only accept the Match Status embed, not unrelated HC bot messages."""
    if not message.embeds:
        return None
    matching_embed = any(
        (embed.title or "").strip().casefold() == "match status"
        for embed in message.embeds
    )
    text = message_text(message)
    # Fallback for embeds whose title is omitted but contain the exact status labels.
    if not matching_embed:
        upper = text.upper()
        if "MATCH STATUS" not in upper or "BATTERS" not in upper or "OVERS" not in upper:
            return None
    scores = {name.strip().casefold(): int(runs) for name, runs in BATTER_LINE.findall(text)}
    return scores or None


def parse_batter_out(message: discord.Message):
    """Return (dismissed batter name, individual final runs) from HC's Batter out embed."""
    if not message.embeds:
        return None
    for embed in message.embeds:
        text = "\n".join(part for part in (embed.title, embed.description) if part)
        for field in embed.fields:
            text += "\n" + (field.name or "") + "\n" + (field.value or "")
        lines = [re.sub(r"[*_`~]", "", line).strip() for line in text.splitlines()]
        out_index = None
        batter_name = None
        for i, line in enumerate(lines):
            match = re.search(r"^(.+?)\s+is out!$", line, re.IGNORECASE)
            if match:
                batter_name = match.group(1).strip().lstrip("@")
                out_index = i
                break
        if batter_name is None:
            continue
        # The embed prints the dismissed player's name, followed by their own
        # score in the form 151 (34). Do not read the bowler or team score.
        for line in lines[out_index + 1:out_index + 6]:
            score_match = re.search(r"^\(?\s*(\d+)\s*\(\s*\d+\s*\)\s*\)?$", line)
            if score_match:
                return batter_name, int(score_match.group(1))
    return None


def find_member_id(guild: discord.Guild, name: str):
    key = name.strip().casefold().lstrip("@")
    for member in guild.members:
        if member.name.casefold() == key or member.display_name.casefold() == key:
            return member.id
    return None


def parse_opportunity(message: discord.Message):
    """Use the same batsman wording/mention format as fielding.py."""
    text = message_text(message)
    if "CHANCE OF WICKET!" not in text.upper():
        return None
    # Match fielding.py's mention-first parser, preserving the actual batsman ID.
    match = re.search(r"takes\s+this\s+catch,\s*<@!?(\d+)>", text, re.IGNORECASE)
    if match:
        return int(match.group(1)), None
    match = re.search(
        r"takes\s+this\s+catch,\s*@?([A-Za-z0-9_.-]{2,32})",
        text,
        re.IGNORECASE,
    )
    if match:
        return None, match.group(1).casefold()
    return None


class BatterFieldingDropStats(commands.Cog):
    """Collect two experimental batter leaderboards without changing fielding totals."""

    def __init__(self, bot):
        self.bot = bot
        # channel_id -> (session_key, score map, source message id, timestamp)
        self.latest_status = {}
        # channel_id -> one pending opportunity
        self.pending = {}
        self._init_db()

    def _init_db(self):
        db = self.bot.db
        db.execute("""
            CREATE TABLE IF NOT EXISTS batter_drop_lb (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                matches_affected INTEGER NOT NULL DEFAULT 0,
                drops INTEGER NOT NULL DEFAULT 0,
                runs_affected INTEGER NOT NULL DEFAULT 0,
                runs_without_drops INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (guild_id, user_id)
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS batter_drop_finalized (
                message_id INTEGER PRIMARY KEY,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS batter_drop_sessions (
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                session_key TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                first_drop_score INTEGER,
                drop_count INTEGER NOT NULL DEFAULT 0,
                last_score INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (guild_id, channel_id, session_key, user_id)
            )
        """)
        db.commit()

    def _tracking_enabled(self, guild_id: int, channel_id: int) -> bool:
        """Follow the exact per-channel switch controlled by +starttracking."""
        try:
            row = self.bot.db.execute(
                "SELECT 1 FROM fielding_tracking_channels WHERE guild_id=? AND channel_id=?",
                (int(guild_id), int(channel_id)),
            ).fetchone()
            return row is not None
        except Exception:
            # Fail closed: never collect these experimental stats if tracking is off/unavailable.
            return False

    @staticmethod
    def _session_key(message: discord.Message) -> str:
        text = message_text(message)
        def value(pattern):
            match = re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
            return match.group(1).strip() if match else "unknown"
        host = value(r"^Host\s*\n\s*([^\n]+)")
        innings = value(r"^Innings\s*\n\s*([^\n]+)")
        team_a = value(r"^TEAM A\s*\n\s*([^\n]+)")
        team_b = value(r"^TEAM B\s*\n\s*([^\n]+)")
        return "|".join((host, innings, team_a, team_b))

    def _finalize_batter(self, guild_id, channel_id, session_key, user_id, final_score):
        db = self.bot.db
        row = db.execute(
            "SELECT first_drop_score, drop_count FROM batter_drop_sessions "
            "WHERE guild_id=? AND channel_id=? AND session_key=? AND user_id=?",
            (guild_id, channel_id, session_key, user_id),
        ).fetchone()
        if not row:
            return
        first_drop, drops = row[0], int(row[1])
        affected = max(0, int(final_score) - int(first_drop)) if first_drop is not None else 0
        without = int(first_drop) if first_drop is not None else int(final_score)
        db.execute("""
            INSERT INTO batter_drop_lb
                (guild_id,user_id,matches_affected,drops,runs_affected,runs_without_drops)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT(guild_id,user_id) DO UPDATE SET
                matches_affected=matches_affected + excluded.matches_affected,
                drops=drops + excluded.drops,
                runs_affected=runs_affected + excluded.runs_affected,
                runs_without_drops=runs_without_drops + excluded.runs_without_drops
        """, (guild_id, user_id, 1 if drops else 0, drops, affected, without))
        db.execute(
            "DELETE FROM batter_drop_sessions WHERE guild_id=? AND channel_id=? AND session_key=? AND user_id=?",
            (guild_id, channel_id, session_key, user_id),
        )
        db.commit()

    async def _add_tracking_reaction(self, message: discord.Message):
        """Visual confirmation that this message was used by the batter tracker."""
        try:
            await message.add_reaction("☑️")
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            # Missing Add Reactions permission should not interrupt tracking.
            pass

    def _update_live_scores(self, message: discord.Message, scores: dict[str, int]):
        channel_id = message.channel.id
        session_key = self._session_key(message)
        previous = self.latest_status.get(channel_id)
        if previous:
            old_session, old_scores = previous[0], previous[1]
            if old_session != session_key:
                rows = self.bot.db.execute(
                    "SELECT user_id,last_score FROM batter_drop_sessions "
                    "WHERE guild_id=? AND channel_id=? AND session_key=?",
                    (message.guild.id, channel_id, old_session),
                ).fetchall()
                for uid, score in rows:
                    self._finalize_batter(message.guild.id, channel_id, old_session, int(uid), int(score))

        for name, score in scores.items():
            uid = find_member_id(message.guild, name)
            if uid:
                self.bot.db.execute("""
                    INSERT INTO batter_drop_sessions
                        (guild_id,channel_id,session_key,user_id,first_drop_score,drop_count,last_score)
                    VALUES (?,?,?,?,NULL,0,?)
                    ON CONFLICT(guild_id,channel_id,session_key,user_id)
                    DO UPDATE SET last_score=excluded.last_score
                """, (message.guild.id, channel_id, session_key, uid, score))
        self.bot.db.commit()
        self.latest_status[channel_id] = (session_key, scores, message.id, time.monotonic())

    async def _get_latest_status(self, message: discord.Message):
        cached = self.latest_status.get(message.channel.id)
        # The status embed is usually immediately above the opportunity; search recent history
        # if cache is empty, stale, or from another match/innings.
        if cached and time.monotonic() - cached[3] <= PENDING_TTL_SECONDS:
            return cached
        try:
            async for old in message.channel.history(limit=25, before=message):
                scores = parse_match_status(old)
                if scores:
                    self._update_live_scores(old, scores)
                    # Mark the historical Match Status embed used for this opportunity.
                    await self._add_tracking_reaction(old)
                    return self.latest_status.get(message.channel.id)
        except (discord.Forbidden, discord.HTTPException):
            pass
        return cached

    async def _handle_batter_out(self, message: discord.Message):
        parsed = parse_batter_out(message)
        if not parsed:
            return False
        batter_name, final_score = parsed
        user_id = find_member_id(message.guild, batter_name)
        if user_id is None:
            # Match Status names can differ in capitalization; try exact cached username/display name.
            key = batter_name.casefold().lstrip("@")
            for member in message.guild.members:
                if member.name.casefold() == key or member.display_name.casefold() == key:
                    user_id = member.id
                    break
        if user_id is None:
            return False
        channel_id = message.channel.id
        db = self.bot.db
        # Message edits can fire repeatedly; finalize a dismissal only once.
        if db.execute("SELECT 1 FROM batter_drop_finalized WHERE message_id=?", (message.id,)).fetchone():
            return True
        # Prefer the current live session. If the batter had no catch opportunity,
        # create a clean session so their entire final score counts as runs without drops.
        status = self.latest_status.get(channel_id)
        session_key = status[0] if status else None
        row = db.execute(
            "SELECT session_key FROM batter_drop_sessions WHERE guild_id=? AND channel_id=? AND user_id=? ORDER BY rowid DESC LIMIT 1",
            (message.guild.id, channel_id, user_id),
        ).fetchone()
        if row:
            session_key = row[0]
        if session_key is None:
            session_key = f"dismissal:{message.id}"
        db.execute("""
            INSERT INTO batter_drop_sessions
                (guild_id,channel_id,session_key,user_id,first_drop_score,drop_count,last_score)
            VALUES (?,?,?,?,NULL,0,?)
            ON CONFLICT(guild_id,channel_id,session_key,user_id)
            DO UPDATE SET last_score=excluded.last_score
        """, (message.guild.id, channel_id, session_key, user_id, final_score))
        db.execute(
            "INSERT INTO batter_drop_finalized(message_id,guild_id,channel_id,user_id) VALUES (?,?,?,?)",
            (message.id, message.guild.id, channel_id, user_id),
        )
        db.commit()
        self._finalize_batter(message.guild.id, channel_id, session_key, user_id, final_score)
        return True

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.id != HANDCRICKET_BOT_ID or not message.guild:
            return
        if not self._tracking_enabled(message.guild.id, message.channel.id):
            self.pending.pop(message.channel.id, None)
            return

        # The Batter out embed is the authoritative individual final score. It
        # excludes team extras awarded after a dropped catch.
        if await self._handle_batter_out(message):
            return

        # Only Match Status embeds are treated as score updates. All other HC embeds
        # are ignored unless they are one of the three catch-event messages below.
        scores = parse_match_status(message)
        if scores:
            self._update_live_scores(message, scores)
            # Tick confirms this exact Match Status embed was accepted as a score source.
            await self._add_tracking_reaction(message)
            return

        text = message_text(message)
        upper = text.upper()
        channel_id = message.channel.id

        if "CHANCE OF WICKET!" in upper:
            status = await self._get_latest_status(message)
            opportunity = parse_opportunity(message)
            if not status or not opportunity:
                self.pending.pop(channel_id, None)
                return
            user_id, batter_name = opportunity
            session_key, score_map = status[0], status[1]
            if user_id is None:
                user_id = find_member_id(message.guild, batter_name)
            if user_id is None:
                self.pending.pop(channel_id, None)
                return
            member = message.guild.get_member(user_id)
            if member is None:
                try:
                    member = await message.guild.fetch_member(user_id)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    member = None

            # The opportunity embed's <@ID> resolves to a server nickname in Discord.
            # Use that ID to get the member, then match the Match Status username
            # against the member's actual Discord username first (not the nickname).
            possible_names = set()
            if member:
                possible_names.add(member.name.casefold())
                possible_names.add(member.display_name.casefold())
            if batter_name:
                possible_names.add(batter_name.casefold())
            batter_score = None
            for name, score in score_map.items():
                if name.casefold() in possible_names:
                    batter_score = score
                    break
            if batter_score is None:
                self.pending.pop(channel_id, None)
                return
            self.pending[channel_id] = {
                "guild_id": message.guild.id,
                "session_key": session_key,
                "user_id": user_id,
                "score": int(batter_score),
                "created": time.monotonic(),
            }
            # Tick confirms the opportunity was matched to a batter and score.
            await self._add_tracking_reaction(message)
            return

        result = "dropped" if "BOZO DROPPED THE CATCH" in upper else "taken" if "TOOK THE CATCH" in upper else None
        if result is None:
            return
        pending = self.pending.pop(channel_id, None)
        if not pending or time.monotonic() - pending["created"] > PENDING_TTL_SECONDS:
            return

        if result == "dropped":
            db = self.bot.db
            db.execute("""
                INSERT INTO batter_drop_sessions
                    (guild_id,channel_id,session_key,user_id,first_drop_score,drop_count,last_score)
                VALUES (?,?,?,?,?,1,?)
                ON CONFLICT(guild_id,channel_id,session_key,user_id) DO UPDATE SET
                    first_drop_score=COALESCE(batter_drop_sessions.first_drop_score, excluded.first_drop_score),
                    drop_count=batter_drop_sessions.drop_count+1,
                    last_score=excluded.last_score
            """, (pending["guild_id"], channel_id, pending["session_key"], pending["user_id"], pending["score"], pending["score"]))
            db.commit()
        else:
            # The opportunity score is the batter's final score for a taken catch.
            self._finalize_batter(
                pending["guild_id"], channel_id, pending["session_key"],
                pending["user_id"], pending["score"],
            )

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        message = after
        if message.author.id != HANDCRICKET_BOT_ID or not message.guild:
            return
        if not self._tracking_enabled(message.guild.id, message.channel.id):
            return
        if await self._handle_batter_out(message):
            return
        scores = parse_match_status(message)
        if scores:
            self._update_live_scores(message, scores)
            await self._add_tracking_reaction(message)


    @commands.command(name="setbatterimpact")
    @commands.guild_only()
    async def set_batter_impact(self, ctx: commands.Context, member: discord.Member, runs_after_drops: int, runs_without_drops: int):
        if ctx.author.id != BOT_OWNER_ID:
            return await ctx.reply("You are not allowed to use this command.", mention_author=False)
        if runs_after_drops < 0 or runs_without_drops < 0:
            return await ctx.reply("Run values cannot be negative.", mention_author=False)
        self.bot.db.execute("""
            INSERT INTO batter_drop_lb (guild_id,user_id,matches_affected,drops,runs_affected,runs_without_drops)
            VALUES (?,?,0,0,?,?)
            ON CONFLICT(guild_id,user_id) DO UPDATE SET
                runs_affected=excluded.runs_affected,
                runs_without_drops=excluded.runs_without_drops
        """, (ctx.guild.id, member.id, runs_after_drops, runs_without_drops))
        self.bot.db.commit()
        await ctx.reply(f"Updated Batter Impact for **{discord.utils.escape_markdown(member.display_name)}**: **{runs_after_drops}** runs after drops, **{runs_without_drops}** runs without drops.", mention_author=False)

    @commands.command(name="resetbatterimpact")
    @commands.guild_only()
    async def reset_batter_impact(self, ctx: commands.Context, member: discord.Member = None):
        if ctx.author.id != BOT_OWNER_ID:
            return await ctx.reply("You are not allowed to use this command.", mention_author=False)
        if member is None:
            self.bot.db.execute("DELETE FROM batter_drop_lb WHERE guild_id=?", (ctx.guild.id,))
            self.bot.db.execute("DELETE FROM batter_drop_sessions WHERE guild_id=?", (ctx.guild.id,))
            await ctx.reply("Reset Batter Impact stats for everyone in this server.", mention_author=False)
        else:
            self.bot.db.execute("DELETE FROM batter_drop_lb WHERE guild_id=? AND user_id=?", (ctx.guild.id, member.id))
            self.bot.db.execute("DELETE FROM batter_drop_sessions WHERE guild_id=? AND user_id=?", (ctx.guild.id, member.id))
            await ctx.reply(f"Reset Batter Impact stats for **{discord.utils.escape_markdown(member.display_name)}**.", mention_author=False)
        self.bot.db.commit()


async def setup(bot: commands.Bot):
    await bot.add_cog(BatterFieldingDropStats(bot))
