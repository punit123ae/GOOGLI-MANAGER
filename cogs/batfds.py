"""Persistent batter catch-drop impact tracking for Googli Manager.

Tracks only channels enabled by +starttracking in cogs/fielding.py.
Live innings are read from Hand Cricket Match Status embeds. Innings ONE -> TWO
finalises first-innings not-outs; "Match has ended." finalises the current innings.
Temporary totals live in SQLite until the owner commits them to the permanent +lb.
"""
from __future__ import annotations

import re
import time
import uuid
from datetime import datetime, timezone

import discord
from discord.ext import commands

HANDCRICKET_BOT_ID = 753191385296928808
BOT_OWNER_ID = 1014568928682459198
PENDING_TTL_SECONDS = 120

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


def _clean_discord_name(value: str) -> str:
    """Remove common Discord markdown wrappers around a displayed player name."""
    return re.sub(r"[`*_~]", "", value or "").strip().casefold()


def parse_match_status(message: discord.Message):
    if not message.embeds:
        return None
    text = message_text(message)
    if not any((e.title or "").strip().casefold() == "match status" for e in message.embeds):
        if "MATCH STATUS" not in text.upper() or "OVERS" not in text.upper():
            return None

    # Hand Cricket embeds may wrap player names in Discord markdown. Parse each
    # batter line independently so formatting does not prevent score updates.
    scores = {}
    for line in text.splitlines():
        clean_line = re.sub(r"[`*_~]", "", line).strip()
        match = re.match(
            r"^(.{1,64}?)\s*:\s*(\d+)\s*\(\s*\d+\s*\)\s*runs\s*$",
            clean_line,
            re.IGNORECASE,
        )
        if match:
            name = _clean_discord_name(match.group(1))
            if name:
                scores[name] = int(match.group(2))
    if not scores:
        return None

    inning_match = re.search(r"^\s*Innings\s*\n\s*(ONE|TWO|1ST|2ND)\b", text,
                             re.IGNORECASE | re.MULTILINE)
    innings = None
    if inning_match:
        token = inning_match.group(1).upper()
        innings = "ONE" if token in ("ONE", "1ST") else "TWO"
    ended = bool(re.search(r"\bMatch\s+has\s+ended\s*\.?", text, re.IGNORECASE))
    return {"scores": scores, "innings": innings, "ended": ended}


def parse_batter_out(message: discord.Message):
    if not message.embeds:
        return None
    for embed in message.embeds:
        text = "\n".join(part for part in (embed.title, embed.description) if part)
        for field in embed.fields:
            text += "\n" + (field.name or "") + "\n" + (field.value or "")
        lines = [re.sub(r"[*_`~]", "", line).strip() for line in text.splitlines()]
        for i, line in enumerate(lines):
            match = re.search(r"^(.+?)\s+is out!$", line, re.IGNORECASE)
            if not match:
                continue
            batter_name = match.group(1).strip().lstrip("@")
            for score_line in lines[i + 1:i + 6]:
                score_match = re.search(r"^\(?\s*(\d+)\s*\(\s*\d+\s*\)\s*\)?$", score_line)
                if score_match:
                    return batter_name, int(score_match.group(1))
    return None


def _compact_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", _clean_discord_name(value))


def _find_member_id_scan(guild: discord.Guild, name: str):
    """Original full member scan. Kept as the reference behaviour (and for tests)."""
    key = _clean_discord_name(name).lstrip("@")
    exact = [
        member for member in guild.members
        if _clean_discord_name(member.name) == key
        or _clean_discord_name(member.display_name) == key
    ]
    if len(exact) == 1:
        return exact[0].id

    # Hand Cricket sometimes prints usernames without spaces/punctuation.
    # Use this fallback only when it identifies exactly one server member.
    compact = _compact_name(key)
    matches = [
        member for member in guild.members
        if compact and (
            _compact_name(member.name) == compact
            or _compact_name(member.display_name) == compact
        )
    ]
    return matches[0].id if len({m.id for m in matches}) == 1 and matches else None



# --- fast name lookup ---------------------------------------------------------
# find_member_id() used to read the whole member list on every call. It now keeps
# a per-server table of cleaned name -> member ids. The table is rebuilt only when
# members join / leave / rename (see the cog listeners) and, as a safety net, when a
# name is not found and the table is older than _NAME_INDEX_MAX_AGE seconds.
_NAME_INDEX_MAX_AGE = 30.0
_name_index: dict = {}  # guild_id -> {"exact": {...}, "compact": {...}, "built": monotonic}


def invalidate_name_index(guild_id=None):
    """Forget the cached table for one server (or all servers)."""
    if guild_id is None:
        _name_index.clear()
    else:
        _name_index.pop(int(guild_id), None)


def _build_name_index(guild: discord.Guild) -> dict:
    exact: dict = {}
    compact: dict = {}
    for member in guild.members:
        for raw in (member.name, member.display_name):
            exact.setdefault(_clean_discord_name(raw), set()).add(member.id)
            short = _compact_name(raw)
            if short:
                compact.setdefault(short, set()).add(member.id)
    index = {"exact": exact, "compact": compact, "built": time.monotonic()}
    _name_index[guild.id] = index
    return index


def _lookup_name(index: dict, key: str):
    ids = index["exact"].get(key, ())
    if len(ids) == 1:
        return next(iter(ids))
    # Hand Cricket sometimes prints usernames without spaces/punctuation.
    # Use this fallback only when it identifies exactly one server member.
    short = _compact_name(key)
    ids = index["compact"].get(short, ()) if short else ()
    return next(iter(ids)) if len(ids) == 1 else None


def find_member_id(guild: discord.Guild, name: str):
    key = _clean_discord_name(name).lstrip("@")
    index = _name_index.get(guild.id)
    if index is None:
        index = _build_name_index(guild)
    result = _lookup_name(index, key)
    if result is None and time.monotonic() - index["built"] > _NAME_INDEX_MAX_AGE:
        # Not found in an old table: members may have changed. Rebuild once and retry.
        index = _build_name_index(guild)
        result = _lookup_name(index, key)
    return result


def parse_opportunity(message: discord.Message):
    """Return the batter who would be out, not the fielder taking the catch."""
    text = message_text(message)
    if "CHANCE OF WICKET!" not in text.upper():
        return None

    # Format: "If @Fielder takes this catch, @Batter will be out!"
    # The first mention is the fielder; the second is the batter.
    match = re.search(
        r"takes\s+this\s+catch,\s*<@!?(\d+)>\s+will\s+be\s+out",
        text,
        re.IGNORECASE,
    )
    if match:
        return int(match.group(1)), None

    match = re.search(
        r"takes\s+this\s+catch,\s*@?([A-Za-z0-9_.-]{2,32})\s+will\s+be\s+out",
        text,
        re.IGNORECASE,
    )
    return (None, match.group(1).casefold()) if match else None


class CommitConfirmView(discord.ui.View):
    def __init__(self, cog: "BatterFieldingDropStats", ctx: commands.Context):
        super().__init__(timeout=60)
        self.cog = cog
        self.ctx = ctx
        self.done = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != BOT_OWNER_ID:
            await interaction.response.send_message("Only the bot owner can confirm this.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Confirm commit", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.done:
            return await interaction.response.send_message("This commit has already been handled.", ephemeral=True)
        try:
            backup_no = self.cog._commit_preview(self.ctx.guild.id)
        except RuntimeError as exc:
            return await interaction.response.send_message(str(exc), ephemeral=True)
        self.done = True
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            content=f"Preview committed to the permanent `+lb`. Backup created: `+previewlb {backup_no}`. Preview has been reset.",
            view=self,
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.done = True
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="Commit cancelled. Preview data was not changed.", view=self)


class MarkEndedConfirmView(discord.ui.View):
    """Owner confirmation before manually closing a missed match-end event."""
    def __init__(self, cog: "BatterFieldingDropStats", ctx: commands.Context, channel_id: int):
        super().__init__(timeout=60)
        self.cog = cog
        self.ctx = ctx
        self.channel_id = int(channel_id)
        self.done = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != BOT_OWNER_ID:
            await interaction.response.send_message("Only the bot owner can confirm this.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Confirm: mark ended", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.done:
            return await interaction.response.send_message("This action has already been handled.", ephemeral=True)
        try:
            finalized = self.cog._mark_match_ended(self.ctx.guild.id, self.channel_id)
        except (RuntimeError, ValueError) as exc:
            return await interaction.response.send_message(str(exc), ephemeral=True)
        self.done = True
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            content=(f"Marked <#{self.channel_id}> as ended. Finalised **{finalized}** unfinished batter record(s). "
                     "Preview data has been preserved."),
            view=self,
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.done = True
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="Cancelled. No tracking data was changed.", view=self)


class ResetBatterImpactConfirmView(discord.ui.View):
    """Owner-only confirmation for destructive Batter Impact reset."""
    def __init__(self, cog: "BatterFieldingDropStats", ctx: commands.Context, member: discord.Member = None):
        super().__init__(timeout=60)
        self.cog, self.ctx, self.member = cog, ctx, member
        self.done = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != BOT_OWNER_ID:
            await interaction.response.send_message("Only the bot owner can confirm this.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Confirm reset", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.done:
            return await interaction.response.send_message("This reset has already been handled.", ephemeral=True)
        backup_no = self.cog._backup_before_reset(self.ctx.guild.id, self.member.id if self.member else None)
        self.cog._perform_reset(self.ctx.guild.id, self.member.id if self.member else None)
        self.done = True
        for child in self.children:
            child.disabled = True
        target = self.member.mention if self.member else "everyone"
        await interaction.response.edit_message(
            content=f"Batter Impact reset for {target}. Automatic backup created: `+previewlb {backup_no}`.", view=self
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.done = True
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="Reset cancelled. No data was changed.", view=self)


class RemoveRepStatsConfirmView(discord.ui.View):
    def __init__(self, cog: "BatterFieldingDropStats", ctx: commands.Context, user_ids: list[int]):
        super().__init__(timeout=60)
        self.cog, self.ctx, self.user_ids, self.done = cog, ctx, user_ids, False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != BOT_OWNER_ID:
            await interaction.response.send_message("Only the bot owner can confirm this.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Confirm remove reps", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.done:
            return await interaction.response.send_message("This action was already handled.", ephemeral=True)
        db = self.cog.bot.db
        cur = db.execute("INSERT INTO batter_drop_backups(guild_id,created_at) VALUES(?,?)",
                         (self.ctx.guild.id, datetime.now(timezone.utc).isoformat(timespec="seconds")))
        backup_no = int(cur.lastrowid)
        for uid in self.user_ids:
            row = db.execute("SELECT matches_affected,drops,runs_affected,runs_without_drops FROM batter_drop_lb WHERE guild_id=? AND user_id=?",
                             (self.ctx.guild.id, uid)).fetchone()
            if row:
                db.execute("INSERT INTO batter_drop_backup_rows(backup_no,user_id,matches_affected,drops,runs_affected,runs_without_drops) VALUES(?,?,?,?,?,?)",
                           (backup_no, uid, *row))
            for table in ("batter_drop_lb", "batter_drop_preview", "batter_drop_sessions", "batter_drop_pending", "batter_drop_finalized"):
                db.execute(f"DELETE FROM {table} WHERE guild_id=? AND user_id=?", (self.ctx.guild.id, uid))
        db.commit()
        self.done = True
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            content=f"Removed Batter Impact/preview records for **{len(self.user_ids)} rep(s)**. Backup created: `+previewlb {backup_no}`. Imported `+as` stats and fantasy points were untouched.", view=self
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.done = True
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="Cancelled; no rep stats were removed.", view=self)


class BatterFieldingDropStats(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.latest_status = {}  # channel -> (match_id, innings, score map, monotonic timestamp)
        self.pending = {}
        self._init_db()

    def _init_db(self):
        db = self.bot.db
        db.execute("""
            CREATE TABLE IF NOT EXISTS batter_drop_lb (
                guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
                matches_affected INTEGER NOT NULL DEFAULT 0,
                drops INTEGER NOT NULL DEFAULT 0,
                runs_affected INTEGER NOT NULL DEFAULT 0,
                runs_without_drops INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (guild_id, user_id))
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS batter_drop_preview (
                guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
                matches_affected INTEGER NOT NULL DEFAULT 0,
                drops INTEGER NOT NULL DEFAULT 0,
                runs_affected INTEGER NOT NULL DEFAULT 0,
                runs_without_drops INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (guild_id, user_id))
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS batter_drop_sessions (
                guild_id INTEGER NOT NULL, channel_id INTEGER NOT NULL,
                session_key TEXT NOT NULL, user_id INTEGER NOT NULL,
                first_drop_score INTEGER, drop_count INTEGER NOT NULL DEFAULT 0,
                last_score INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (guild_id, channel_id, session_key, user_id))
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS batter_drop_matches (
                guild_id INTEGER NOT NULL, channel_id INTEGER NOT NULL,
                match_id TEXT NOT NULL, innings TEXT NOT NULL DEFAULT 'ONE',
                ended INTEGER NOT NULL DEFAULT 0, last_status_id INTEGER,
                PRIMARY KEY (guild_id, channel_id))
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS batter_drop_pending (
                channel_id INTEGER PRIMARY KEY, guild_id INTEGER NOT NULL,
                session_key TEXT NOT NULL, user_id INTEGER NOT NULL,
                score INTEGER NOT NULL, created_at REAL NOT NULL)
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS batter_drop_finalized (
                message_id INTEGER PRIMARY KEY, guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL, user_id INTEGER NOT NULL)
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS batter_drop_reaction_messages (
                message_id INTEGER PRIMARY KEY)
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS batter_drop_backups (
                backup_no INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL, created_at TEXT NOT NULL)
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS batter_drop_backup_rows (
                backup_no INTEGER NOT NULL, user_id INTEGER NOT NULL,
                matches_affected INTEGER NOT NULL, drops INTEGER NOT NULL,
                runs_affected INTEGER NOT NULL, runs_without_drops INTEGER NOT NULL,
                PRIMARY KEY (backup_no, user_id))
        """)
        db.commit()

    def _tracking_enabled(self, guild_id: int, channel_id: int) -> bool:
        try:
            return self.bot.db.execute(
                "SELECT 1 FROM fielding_tracking_channels WHERE guild_id=? AND channel_id=?",
                (int(guild_id), int(channel_id)),
            ).fetchone() is not None
        except Exception:
            return False

    @staticmethod
    def _session_key(match_id: str, innings: str) -> str:
        return f"{match_id}:{innings}"

    def _get_or_start_match(self, guild_id: int, channel_id: int, innings: str | None):
        db = self.bot.db
        row = db.execute(
            "SELECT match_id, innings, ended FROM batter_drop_matches WHERE guild_id=? AND channel_id=?",
            (guild_id, channel_id),
        ).fetchone()
        if row is None or (int(row[2]) and innings == "ONE"):
            match_id = uuid.uuid4().hex
            current_innings = innings or "ONE"
            db.execute("""
                INSERT INTO batter_drop_matches(guild_id,channel_id,match_id,innings,ended,last_status_id)
                VALUES(?,?,?,?,0,NULL)
                ON CONFLICT(guild_id,channel_id) DO UPDATE SET
                    match_id=excluded.match_id, innings=excluded.innings, ended=0, last_status_id=NULL
            """, (guild_id, channel_id, match_id, current_innings))
            return match_id, current_innings
        match_id, current_innings, ended = row[0], row[1], int(row[2])
        if innings == "TWO" and current_innings == "ONE":
            self._finalize_session(guild_id, channel_id, self._session_key(match_id, "ONE"))
            current_innings = "TWO"
            db.execute(
                "UPDATE batter_drop_matches SET innings='TWO', ended=0 WHERE guild_id=? AND channel_id=?",
                (guild_id, channel_id),
            )
        elif innings and not ended:
            current_innings = innings
            db.execute(
                "UPDATE batter_drop_matches SET innings=? WHERE guild_id=? AND channel_id=?",
                (current_innings, guild_id, channel_id),
            )
        return match_id, current_innings

    def _add_to_preview(self, guild_id, user_id, drops, affected, without):
        self.bot.db.execute("""
            INSERT INTO batter_drop_preview
                (guild_id,user_id,matches_affected,drops,runs_affected,runs_without_drops)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT(guild_id,user_id) DO UPDATE SET
                matches_affected=matches_affected+excluded.matches_affected,
                drops=drops+excluded.drops,
                runs_affected=runs_affected+excluded.runs_affected,
                runs_without_drops=runs_without_drops+excluded.runs_without_drops
        """, (guild_id, user_id, 1 if drops else 0, drops, affected, without))

    def _finalize_batter(self, guild_id, channel_id, session_key, user_id, final_score):
        db = self.bot.db
        row = db.execute("""
            SELECT first_drop_score, drop_count FROM batter_drop_sessions
            WHERE guild_id=? AND channel_id=? AND session_key=? AND user_id=?
        """, (guild_id, channel_id, session_key, user_id)).fetchone()
        if not row:
            return
        first_drop, drops = row[0], int(row[1])
        affected = max(0, int(final_score) - int(first_drop)) if first_drop is not None else 0
        without = int(first_drop) if first_drop is not None else int(final_score)
        self._add_to_preview(guild_id, user_id, drops, affected, without)
        db.execute("""
            DELETE FROM batter_drop_sessions
            WHERE guild_id=? AND channel_id=? AND session_key=? AND user_id=?
        """, (guild_id, channel_id, session_key, user_id))
        db.commit()

    def _finalize_session(self, guild_id, channel_id, session_key):
        rows = self.bot.db.execute("""
            SELECT user_id,last_score FROM batter_drop_sessions
            WHERE guild_id=? AND channel_id=? AND session_key=?
        """, (guild_id, channel_id, session_key)).fetchall()
        for uid, score in rows:
            self._finalize_batter(guild_id, channel_id, session_key, int(uid), int(score))

    async def _add_tracking_reaction(self, message: discord.Message):
        db = self.bot.db
        cursor = db.execute(
            "INSERT OR IGNORE INTO batter_drop_reaction_messages(message_id) VALUES(?)",
            (message.id,),
        )
        db.commit()
        if cursor.rowcount == 0:
            return
        try:
            await message.add_reaction("☑️")
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            pass

    def _update_live_scores(self, message: discord.Message, status):
        guild_id, channel_id = message.guild.id, message.channel.id
        match_id, innings = self._get_or_start_match(guild_id, channel_id, status["innings"])
        session_key = self._session_key(match_id, innings)
        db = self.bot.db
        for name, score in status["scores"].items():
            uid = find_member_id(message.guild, name)
            if uid:
                db.execute("""
                    INSERT INTO batter_drop_sessions
                        (guild_id,channel_id,session_key,user_id,first_drop_score,drop_count,last_score)
                    VALUES (?,?,?,?,NULL,0,?)
                    ON CONFLICT(guild_id,channel_id,session_key,user_id)
                    DO UPDATE SET last_score=excluded.last_score
                """, (guild_id, channel_id, session_key, uid, score))
        db.execute(
            "UPDATE batter_drop_matches SET last_status_id=? WHERE guild_id=? AND channel_id=?",
            (message.id, guild_id, channel_id),
        )
        db.commit()
        self.latest_status[channel_id] = (match_id, innings, status["scores"], time.monotonic())
        if status["ended"]:
            self._finalize_session(guild_id, channel_id, session_key)
            db.execute(
                "UPDATE batter_drop_matches SET ended=1 WHERE guild_id=? AND channel_id=?",
                (guild_id, channel_id),
            )
            db.commit()

    async def _get_latest_status(self, message: discord.Message):
        cached = self.latest_status.get(message.channel.id)
        if cached and time.monotonic() - cached[3] <= PENDING_TTL_SECONDS:
            return cached
        try:
            async for old in message.channel.history(limit=30, before=message):
                status = parse_match_status(old)
                if status:
                    self._update_live_scores(old, status)
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
        uid = find_member_id(message.guild, batter_name)
        if uid is None:
            return False
        db = self.bot.db
        if db.execute("SELECT 1 FROM batter_drop_finalized WHERE message_id=?", (message.id,)).fetchone():
            return True
        match = db.execute(
            "SELECT match_id,innings FROM batter_drop_matches WHERE guild_id=? AND channel_id=?",
            (message.guild.id, message.channel.id),
        ).fetchone()
        if not match:
            return False
        session_key = self._session_key(match[0], match[1])
        # Ensure a batter dismissed without any catch opportunity still gets their full score.
        db.execute("""
            INSERT INTO batter_drop_sessions
                (guild_id,channel_id,session_key,user_id,first_drop_score,drop_count,last_score)
            VALUES (?,?,?,?,NULL,0,?)
            ON CONFLICT(guild_id,channel_id,session_key,user_id)
            DO UPDATE SET last_score=excluded.last_score
        """, (message.guild.id, message.channel.id, session_key, uid, final_score))
        db.execute(
            "INSERT INTO batter_drop_finalized(message_id,guild_id,channel_id,user_id) VALUES(?,?,?,?)",
            (message.id, message.guild.id, message.channel.id, uid),
        )
        db.commit()
        self._finalize_batter(message.guild.id, message.channel.id, session_key, uid, final_score)
        return True

    async def on_handcricket_message(self, message: discord.Message):
        if message.author.id != HANDCRICKET_BOT_ID or not message.guild:
            return
        if not self._tracking_enabled(message.guild.id, message.channel.id):
            return

        if await self._handle_batter_out(message):
            return

        status = parse_match_status(message)
        if status:
            self._update_live_scores(message, status)
            await self._add_tracking_reaction(message)
            return

        text = message_text(message)
        upper = text.upper()
        channel_id = message.channel.id

        if "CHANCE OF WICKET!" in upper:
            latest = await self._get_latest_status(message)
            opportunity = parse_opportunity(message)
            if not latest or not opportunity:
                return
            user_id, batter_name = opportunity
            if user_id is None:
                user_id = find_member_id(message.guild, batter_name)
            if user_id is None:
                return
            member = message.guild.get_member(user_id)
            if member is None:
                try:
                    member = await message.guild.fetch_member(user_id)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    return
            possible_names = {_clean_discord_name(member.name), _clean_discord_name(member.display_name)}
            if batter_name:
                possible_names.add(_clean_discord_name(batter_name))
            batter_score = next((score for name, score in latest[2].items()
                                 if _clean_discord_name(name) in possible_names), None)
            if batter_score is None:
                return
            session_key = self._session_key(latest[0], latest[1])
            self.bot.db.execute("""
                INSERT INTO batter_drop_pending(channel_id,guild_id,session_key,user_id,score,created_at)
                VALUES(?,?,?,?,?,?)
                ON CONFLICT(channel_id) DO UPDATE SET
                    guild_id=excluded.guild_id,session_key=excluded.session_key,
                    user_id=excluded.user_id,score=excluded.score,created_at=excluded.created_at
            """, (channel_id, message.guild.id, session_key, user_id, int(batter_score), time.time()))
            self.bot.db.commit()
            await self._add_tracking_reaction(message)
            return

        result = "dropped" if "BOZO DROPPED THE CATCH" in upper else (
            "taken" if "TOOK THE CATCH" in upper else None)
        if result is None:
            return
        pending = self.bot.db.execute(
            "SELECT guild_id,session_key,user_id,score,created_at FROM batter_drop_pending WHERE channel_id=?",
            (channel_id,),
        ).fetchone()
        self.bot.db.execute("DELETE FROM batter_drop_pending WHERE channel_id=?", (channel_id,))
        self.bot.db.commit()
        if not pending or time.time() - float(pending[4]) > PENDING_TTL_SECONDS:
            return
        guild_id, session_key, user_id, score = int(pending[0]), pending[1], int(pending[2]), int(pending[3])
        if result == "dropped":
            self.bot.db.execute("""
                INSERT INTO batter_drop_sessions
                    (guild_id,channel_id,session_key,user_id,first_drop_score,drop_count,last_score)
                VALUES (?,?,?,?,?,1,?)
                ON CONFLICT(guild_id,channel_id,session_key,user_id) DO UPDATE SET
                    first_drop_score=COALESCE(batter_drop_sessions.first_drop_score,excluded.first_drop_score),
                    drop_count=batter_drop_sessions.drop_count+1
            """, (guild_id, channel_id, session_key, user_id, score, score))
            self.bot.db.commit()
        else:
            # A catch being taken is not a batter finalisation event. The
            # authoritative Batter out! embed supplies the batter's final score.
            return

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        invalidate_name_index(member.guild.id)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        invalidate_name_index(member.guild.id)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        # Fires for role changes too, so only react to name changes.
        if before.name != after.name or before.display_name != after.display_name:
            invalidate_name_index(after.guild.id)

    @commands.Cog.listener()
    async def on_user_update(self, before: discord.User, after: discord.User):
        # A global username change affects every server the person is in.
        if before.name != after.name:
            invalidate_name_index()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        await self.on_handcricket_message(message)

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        await self.on_handcricket_message(after)

    def _mark_match_ended(self, guild_id: int, channel_id: int) -> int:
        """Atomically finalise the current innings and close one missed match-end."""
        db = self.bot.db
        row = db.execute(
            "SELECT match_id,innings,ended FROM batter_drop_matches WHERE guild_id=? AND channel_id=?",
            (int(guild_id), int(channel_id)),
        ).fetchone()
        if row is None:
            raise ValueError("No tracked match was found for that channel.")
        if int(row[2]):
            raise ValueError("That tracked match is already marked as ended.")

        session_key = self._session_key(str(row[0]), str(row[1]))
        db.execute("BEGIN IMMEDIATE")
        try:
            batter_rows = db.execute(
                "SELECT user_id,first_drop_score,drop_count,last_score FROM batter_drop_sessions "
                "WHERE guild_id=? AND channel_id=? AND session_key=?",
                (int(guild_id), int(channel_id), session_key),
            ).fetchall()
            for uid, first_drop, drops, last_score in batter_rows:
                affected = max(0, int(last_score) - int(first_drop)) if first_drop is not None else 0
                without = int(first_drop) if first_drop is not None else int(last_score)
                db.execute("""
                    INSERT INTO batter_drop_preview
                        (guild_id,user_id,matches_affected,drops,runs_affected,runs_without_drops)
                    VALUES (?,?,?,?,?,?)
                    ON CONFLICT(guild_id,user_id) DO UPDATE SET
                        matches_affected=matches_affected+excluded.matches_affected,
                        drops=drops+excluded.drops,
                        runs_affected=runs_affected+excluded.runs_affected,
                        runs_without_drops=runs_without_drops+excluded.runs_without_drops
                """, (int(guild_id), int(uid), 1 if int(drops) else 0, int(drops), affected, without))
            db.execute(
                "DELETE FROM batter_drop_sessions WHERE guild_id=? AND channel_id=? AND session_key=?",
                (int(guild_id), int(channel_id), session_key),
            )
            db.execute(
                "DELETE FROM batter_drop_pending WHERE guild_id=? AND channel_id=?",
                (int(guild_id), int(channel_id)),
            )
            db.execute(
                "UPDATE batter_drop_matches SET ended=1 WHERE guild_id=? AND channel_id=?",
                (int(guild_id), int(channel_id)),
            )
            db.commit()
            return len(batter_rows)
        except Exception:
            db.rollback()
            raise

    def _preview_rows(self, guild_id: int):
        rows = self.bot.db.execute(
            "SELECT user_id,matches_affected,drops,runs_affected,runs_without_drops "
            "FROM batter_drop_preview WHERE guild_id=? ORDER BY user_id", (guild_id,)
        ).fetchall()
        data = {int(r[0]): [int(x) for x in r[1:]] for r in rows}
        # Include live innings as provisional figures without writing them twice.
        active = self.bot.db.execute("""
            SELECT s.user_id,s.first_drop_score,s.drop_count,s.last_score
            FROM batter_drop_sessions s
            WHERE s.guild_id=?
        """, (guild_id,)).fetchall()
        for uid, first_drop, drops, last_score in active:
            uid = int(uid)
            rec = data.setdefault(uid, [0, 0, 0, 0])
            rec[1] += int(drops)
            rec[2] += max(0, int(last_score) - int(first_drop)) if first_drop is not None else 0
            rec[3] += int(first_drop) if first_drop is not None else int(last_score)
            if int(drops):
                rec[0] += 1
        return data

    def _commit_preview(self, guild_id: int) -> int:
        db = self.bot.db
        active = db.execute(
            "SELECT COUNT(*) FROM batter_drop_matches WHERE guild_id=? AND ended=0",
            (guild_id,),
        ).fetchone()[0]
        if active:
            raise RuntimeError(
                f"There are {active} tracked match(es) not marked as ended. Wait for `Match has ended.` in each tracked channel before committing."
            )
        db.execute("BEGIN IMMEDIATE")
        try:
            created = datetime.now(timezone.utc).isoformat(timespec="seconds")
            cur = db.execute(
                "INSERT INTO batter_drop_backups(guild_id,created_at) VALUES(?,?)",
                (guild_id, created),
            )
            backup_no = int(cur.lastrowid)
            rows = db.execute("""
                SELECT user_id,matches_affected,drops,runs_affected,runs_without_drops
                FROM batter_drop_preview WHERE guild_id=?
            """, (guild_id,)).fetchall()
            for uid, matches, drops, affected, without in rows:
                db.execute("""
                    INSERT INTO batter_drop_backup_rows
                        (backup_no,user_id,matches_affected,drops,runs_affected,runs_without_drops)
                    VALUES(?,?,?,?,?,?)
                """, (backup_no, uid, matches, drops, affected, without))
                db.execute("""
                    INSERT INTO batter_drop_lb
                        (guild_id,user_id,matches_affected,drops,runs_affected,runs_without_drops)
                    VALUES(?,?,?,?,?,?)
                    ON CONFLICT(guild_id,user_id) DO UPDATE SET
                        matches_affected=matches_affected+excluded.matches_affected,
                        drops=drops+excluded.drops,
                        runs_affected=runs_affected+excluded.runs_affected,
                        runs_without_drops=runs_without_drops+excluded.runs_without_drops
                """, (guild_id, uid, matches, drops, affected, without))
            db.execute("DELETE FROM batter_drop_preview WHERE guild_id=?", (guild_id,))
            db.commit()
            return backup_no
        except Exception:
            db.rollback()
            raise

    @commands.group(name="previewlb", invoke_without_command=True)
    @commands.guild_only()
    async def previewlb(self, ctx: commands.Context, backup_no: int = None):
        if ctx.author.id != BOT_OWNER_ID:
            return await ctx.reply("You are not allowed to use this command.", mention_author=False)
        if backup_no is not None:
            await self._send_backup(ctx, backup_no)
            return
        data = self._preview_rows(ctx.guild.id)
        if not data:
            return await ctx.reply("Preview leaderboard is empty.", mention_author=False)
        lines = []
        for uid, (matches, drops, affected, without) in sorted(
            data.items(), key=lambda item: (-(item[1][2] if item[1][2] else item[1][3]), item[0])
        ):
            member = ctx.guild.get_member(uid)
            name = discord.utils.escape_markdown(member.display_name if member else f"Unknown ({str(uid)[-4:]})")
            lines.append(f"**{name}** — After drops: `{affected}` | Without drops: `{without}` | Drops: `{drops}`")
        embed = discord.Embed(title="Preview Batter Impact Leaderboard", description="\n".join(lines)[:4000],
                              colour=discord.Colour.blurple())
        embed.set_footer(text="Live provisional figures are included. Commit only after every tracked match has ended.")
        await ctx.send(embed=embed)

    @previewlb.command(name="markended")
    @commands.guild_only()
    async def previewlb_markended(self, ctx: commands.Context, channel_id: int = None):
        """Manually close a match when Hand Cricket's end message was missed.

        Usage: +previewlb markended [channel_id]
        """
        if ctx.author.id != BOT_OWNER_ID:
            return await ctx.reply("You are not allowed to use this command.", mention_author=False)
        db = self.bot.db
        if channel_id is None:
            active = db.execute(
                "SELECT channel_id,match_id,innings FROM batter_drop_matches "
                "WHERE guild_id=? AND ended=0 ORDER BY channel_id",
                (ctx.guild.id,),
            ).fetchall()
            if not active:
                return await ctx.reply("There are no unfinished tracked matches.", mention_author=False)
            if len(active) > 1:
                lines = [f"Channel ID `{int(r[0])}` — innings **{r[2]}**" for r in active]
                return await ctx.reply(
                    "More than one tracked match is unfinished. Specify the channel ID:\n" + "\n".join(lines) +
                    "\nExample: `+previewlb markended 123456789012345678`",
                    mention_author=False,
                )
            channel_id = int(active[0][0])
        else:
            channel_id = int(channel_id)
        row = db.execute(
            "SELECT innings,ended FROM batter_drop_matches WHERE guild_id=? AND channel_id=?",
            (ctx.guild.id, channel_id),
        ).fetchone()
        if row is None:
            return await ctx.reply("No tracked match was found for that channel ID.", mention_author=False)
        if int(row[1]):
            return await ctx.reply("That tracked match is already marked as ended.", mention_author=False)
        channel = ctx.guild.get_channel(channel_id)
        channel_label = channel.mention if channel else f"channel `{channel_id}`"
        view = MarkEndedConfirmView(self, ctx, channel_id)
        await ctx.send(
            f"Are you sure you want to manually mark the match in {channel_label} as ended (innings **{row[0]}**)?\n"
            "The bot will finalise unfinished batters using their latest saved scores. Preview data will be kept, "
            "but any score updates missed after the last Match Status will not be included.",
            view=view,
        )

    @previewlb.command(name="backups")
    async def previewlb_backups(self, ctx: commands.Context):
        if ctx.author.id != BOT_OWNER_ID:
            return await ctx.reply("You are not allowed to use this command.", mention_author=False)
        rows = self.bot.db.execute(
            "SELECT backup_no,created_at FROM batter_drop_backups WHERE guild_id=? ORDER BY backup_no DESC",
            (ctx.guild.id,),
        ).fetchall()
        if not rows:
            return await ctx.reply("No preview backups found.", mention_author=False)
        await ctx.send("**Preview backups**\n" + "\n".join(
            f"`+previewlb {r[0]}` — {r[1]}" for r in rows
        ))

    @previewlb.command(name="delete")
    async def previewlb_delete(self, ctx: commands.Context, backup_no: int):
        if ctx.author.id != BOT_OWNER_ID:
            return await ctx.reply("You are not allowed to use this command.", mention_author=False)
        db = self.bot.db
        db.execute("DELETE FROM batter_drop_backup_rows WHERE backup_no=? AND backup_no IN "
                   "(SELECT backup_no FROM batter_drop_backups WHERE guild_id=?)",
                   (backup_no, ctx.guild.id))
        cur = db.execute("DELETE FROM batter_drop_backups WHERE backup_no=? AND guild_id=?",
                         (backup_no, ctx.guild.id))
        db.commit()
        await ctx.reply("Backup deleted." if cur.rowcount else "Backup not found.", mention_author=False)

    async def _send_backup(self, ctx: commands.Context, backup_no: int):
        rows = self.bot.db.execute("""
            SELECT user_id,matches_affected,drops,runs_affected,runs_without_drops
            FROM batter_drop_backup_rows WHERE backup_no=?
            AND backup_no IN (SELECT backup_no FROM batter_drop_backups WHERE guild_id=?)
            ORDER BY user_id
        """, (backup_no, ctx.guild.id)).fetchall()
        if not rows:
            return await ctx.reply("Backup not found or empty.", mention_author=False)
        lines = []
        for uid, matches, drops, affected, without in rows:
            member = ctx.guild.get_member(int(uid))
            name = discord.utils.escape_markdown(member.display_name if member else f"Unknown ({str(uid)[-4:]})")
            lines.append(f"**{name}** — After drops: `{affected}` | Without drops: `{without}` | Drops: `{drops}`")
        await ctx.send(embed=discord.Embed(title=f"Preview Backup {backup_no}",
                                           description="\n".join(lines)[:4000],
                                           colour=discord.Colour.blurple()))

    @previewlb.command(name="view")
    async def previewlb_view(self, ctx: commands.Context, backup_no: int):
        if ctx.author.id != BOT_OWNER_ID:
            return await ctx.reply("You are not allowed to use this command.", mention_author=False)
        await self._send_backup(ctx, backup_no)

    @commands.command(name="commitlb")
    @commands.guild_only()
    async def commitlb(self, ctx: commands.Context):
        if ctx.author.id != BOT_OWNER_ID:
            return await ctx.reply("You are not allowed to use this command.", mention_author=False)
        active = self.bot.db.execute(
            "SELECT COUNT(*) FROM batter_drop_matches WHERE guild_id=? AND ended=0",
            (ctx.guild.id,),
        ).fetchone()[0]
        if active:
            return await ctx.reply(
                f"`{active}` tracked match(es) are not marked as ended yet. Commit is blocked to protect unfinished innings.",
                mention_author=False,
            )
        count = self.bot.db.execute(
            "SELECT COUNT(*) FROM batter_drop_preview WHERE guild_id=?", (ctx.guild.id,)
        ).fetchone()[0]
        if not count:
            return await ctx.reply("Preview leaderboard is empty; nothing to commit.", mention_author=False)
        view = CommitConfirmView(self, ctx)
        await ctx.send(
            f"Commit the current preview stats for **{count} players** to the permanent `+lb`? "
            "A backup will be created and the preview will then be reset.",
            view=view,
        )

    @commands.command(name="setbatterimpact")
    @commands.guild_only()
    async def set_batter_impact(self, ctx: commands.Context, member: discord.Member,
                               runs_after_drops: int, runs_without_drops: int):
        if ctx.author.id != BOT_OWNER_ID:
            return await ctx.reply("You are not allowed to use this command.", mention_author=False)
        if runs_after_drops < 0 or runs_without_drops < 0:
            return await ctx.reply("Run values cannot be negative.", mention_author=False)
        self.bot.db.execute("""
            INSERT INTO batter_drop_lb (guild_id,user_id,matches_affected,drops,runs_affected,runs_without_drops)
            VALUES (?,?,0,0,?,?)
            ON CONFLICT(guild_id,user_id) DO UPDATE SET
                runs_affected=excluded.runs_affected,runs_without_drops=excluded.runs_without_drops
        """, (ctx.guild.id, member.id, runs_after_drops, runs_without_drops))
        self.bot.db.commit()
        await ctx.reply(
            f"Updated Batter Impact for **{discord.utils.escape_markdown(member.display_name)}**: "
            f"**{runs_after_drops}** runs after drops, **{runs_without_drops}** runs without drops.",
            mention_author=False,
        )

    @commands.command(name="removerepstats")
    @commands.guild_only()
    async def remove_rep_stats(self, ctx: commands.Context):
        """Reply to Hand Cricket raw statistics with +removerepstats to remove those IDs from Batter Impact."""
        if ctx.author.id != BOT_OWNER_ID:
            return await ctx.reply("You are not allowed to use this command.", mention_author=False)
        source = ctx.message.reference.resolved if ctx.message.reference else None
        if source is None or not isinstance(source, discord.Message):
            return await ctx.reply("Reply directly to the Hand Cricket raw-statistics message with `+removerepstats`.", mention_author=False)
        content = source.content or ""
        heading = re.search(r"Raw statistics of the match\s*:", content, re.IGNORECASE)
        if heading:
            content = content[heading.end():]
        block = re.search(r"```python\s*\n(.*?)```", content, re.IGNORECASE | re.DOTALL)
        if not block:
            return await ctx.reply("I couldn't find the Python raw-statistics block in that message.", mention_author=False)
        user_ids = []
        for line in block.group(1).splitlines():
            parts = [part.strip() for part in line.strip().strip("`").split(",")]
            if len(parts) == 7 and all(re.fullmatch(r"\d+", part) for part in parts):
                user_ids.append(int(parts[0]))
        user_ids = sorted(set(user_ids))
        if not user_ids:
            return await ctx.reply("No valid player IDs were found in the raw statistics.", mention_author=False)
        view = RemoveRepStatsConfirmView(self, ctx, user_ids)
        await ctx.send(
            f"Found **{len(user_ids)} player ID(s)** in the replied raw stats. This removes their Batter Impact and preview records only; it does not delete imported `+as` stats. Backup will be created first.",
            view=view,
        )

    def _backup_before_reset(self, guild_id: int, user_id: int = None) -> int:
        """Snapshot permanent Batter Impact totals before a destructive reset."""
        db = self.bot.db
        created = datetime.now(timezone.utc).isoformat(timespec="seconds")
        cur = db.execute("INSERT INTO batter_drop_backups(guild_id,created_at) VALUES(?,?)", (guild_id, created))
        backup_no = int(cur.lastrowid)
        query = "SELECT user_id,matches_affected,drops,runs_affected,runs_without_drops FROM batter_drop_lb WHERE guild_id=?"
        params = (guild_id,)
        if user_id is not None:
            query += " AND user_id=?"
            params = (guild_id, user_id)
        rows = db.execute(query, params).fetchall()
        for uid, matches, drops, affected, without in rows:
            db.execute("INSERT INTO batter_drop_backup_rows(backup_no,user_id,matches_affected,drops,runs_affected,runs_without_drops) VALUES(?,?,?,?,?,?)",
                       (backup_no, uid, matches, drops, affected, without))
        db.commit()
        return backup_no

    def _perform_reset(self, guild_id: int, user_id: int = None):
        db = self.bot.db
        tables = ("batter_drop_lb", "batter_drop_preview", "batter_drop_sessions",
                  "batter_drop_pending", "batter_drop_finalized")
        for table in tables:
            if user_id is None:
                db.execute(f"DELETE FROM {table} WHERE guild_id=?", (guild_id,))
            else:
                db.execute(f"DELETE FROM {table} WHERE guild_id=? AND user_id=?", (guild_id, user_id))
        if user_id is None:
            db.execute("DELETE FROM batter_drop_matches WHERE guild_id=?", (guild_id,))
        db.commit()

    @commands.command(name="resetbatterimpact")
    @commands.guild_only()
    async def reset_batter_impact(self, ctx: commands.Context, member: discord.Member = None):
        if ctx.author.id != BOT_OWNER_ID:
            return await ctx.reply("You are not allowed to use this command.", mention_author=False)
        target = member.mention if member else "everyone in this server"
        view = ResetBatterImpactConfirmView(self, ctx, member)
        await ctx.send(
            f"⚠️ Confirm reset of Batter Impact stats for **{target}**? An automatic backup will be created first.",
            view=view,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(BatterFieldingDropStats(bot))
