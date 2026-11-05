"""Persistent batter catch-drop impact tracking for Googli Manager.

Tracks only channels enabled by +starttracking in cogs/fielding.py.
Live innings are read from Hand Cricket Match Status embeds. Innings ONE -> TWO
finalises first-innings not-outs; "Match has ended." finalises the current innings.
Temporary totals live in SQLite until the owner commits them to the permanent +lb.
"""
from __future__ import annotations

import io
import logging
import re
import sqlite3
import time
import uuid
from datetime import datetime, timezone

import discord
from discord.ext import commands, tasks

from .utility import owner_or_whitelisted

HANDCRICKET_BOT_ID = 753191385296928808
BOT_OWNER_ID = 1014568928682459198
PENDING_TTL_SECONDS = 120
# Ended matches that still hold unfinalised batter records are recovered automatically
# once they have been quiet for this long (gives Hand Cricket time to post its raw stats).
AUTO_RECOVER_GRACE_SECONDS = 600
SWEEP_INTERVAL_SECONDS = 120
# Set to True to let the bot count stuck ended matches by itself (checked every
# SWEEP_INTERVAL_SECONDS, and before +previewlb / +commitlb). When False, nothing is counted
# automatically: use "+previewlb markended" / "+recoverlb" yourself. +commitlb still refuses
# to run while an ended match has uncounted records.
AUTO_RECOVER_ENABLED = False

log = logging.getLogger(__name__)

BATTER_LINE = re.compile(
    r"^\s*([^:\n]{1,64})\s*:\s*(\d+)\s*\(\s*\d+\s*\)\s*runs\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def parse_rep_raw_stats(message: discord.Message) -> list[tuple[int, int]]:
    """Parse the final fenced code block: (user_id, runs_scored) for each rep."""
    blocks = re.findall(r"```(?:[A-Za-z0-9_-]+)?\s*([\s\S]*?)```", message.content or "")
    if not blocks:
        return []
    reps = []
    seen = set()
    for line in blocks[-1].splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 7:
            continue
        try:
            user_id = int(parts[0])
            runs = int(parts[1])
            # Validate the full seven-field raw-stat record.
            [int(value) for value in parts[2:7]]
        except (TypeError, ValueError):
            continue
        if user_id > 0 and runs >= 0 and user_id not in seen:
            seen.add(user_id)
            reps.append((user_id, runs))
    return reps


def message_text(message: discord.Message) -> str:
    parts = [message.content or ""]
    for embed in message.embeds:
        parts.extend((embed.title or "", embed.description or ""))
        for field in embed.fields:
            parts.extend((field.name or "", field.value or ""))
        # Hand Cricket puts "Match has ended." in the embed footer, which was never read before,
        # so the end of a match was never detected.
        footer = getattr(embed, "footer", None)
        parts.append(getattr(footer, "text", None) or "")
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


class MarkAllEndedConfirmView(discord.ui.View):
    """Owner confirmation before closing every unfinished tracked match in the server."""
    def __init__(self, cog: "BatterFieldingDropStats", ctx: commands.Context, channel_ids):
        super().__init__(timeout=60)
        self.cog = cog
        self.ctx = ctx
        self.channel_ids = [int(c) for c in channel_ids]
        self.done = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != BOT_OWNER_ID:
            await interaction.response.send_message("Only the bot owner can confirm this.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Confirm: mark all ended", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.done:
            return await interaction.response.send_message("This action has already been handled.", ephemeral=True)
        self.done = True
        for child in self.children:
            child.disabled = True
        ok, failed, total = [], [], 0
        for channel_id in self.channel_ids:
            try:
                finalized = self.cog._mark_match_ended(self.ctx.guild.id, channel_id)
            except (RuntimeError, ValueError, sqlite3.Error) as exc:
                failed.append(f"<#{channel_id}> — {exc}")
                continue
            total += finalized
            ok.append(f"<#{channel_id}> — {finalized} record(s)")
        text = (f"Marked **{len(ok)}** match(es) as ended and finalised **{total}** batter record(s). "
                "Preview data has been preserved.")
        if ok:
            text += "\n" + "\n".join(ok)
        if failed:
            text += "\n⚠️ Not changed:\n" + "\n".join(failed)
        if len(text) > 1900:
            text = text[:1900] + "…"
        await interaction.response.edit_message(content=text, view=self)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.done = True
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="Cancelled. No tracking data was changed.", view=self)


class FixBatterImpactConfirmView(discord.ui.View):
    """Owner confirmation before correcting every wrong Batter Impact total."""
    def __init__(self, cog: "BatterFieldingDropStats", ctx: commands.Context):
        super().__init__(timeout=90)
        self.cog = cog
        self.ctx = ctx
        self.done = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != BOT_OWNER_ID:
            await interaction.response.send_message("Only the bot owner can confirm this.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Confirm: fix all", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.done:
            return await interaction.response.send_message("This action has already been handled.", ephemeral=True)
        try:
            # Re-check right now: the data may have changed since the command was run.
            _, plan, blocked = self.cog._plan_fix(self.ctx.guild.id)
            applied, revert = self.cog._apply_fix(self.ctx.guild.id, plan)
        except Exception as exc:
            return await interaction.response.send_message(
                f"Fix failed and was rolled back: `{type(exc).__name__}: {exc}`", ephemeral=True)
        self.done = True
        for child in self.children:
            child.disabled = True
        text = f"✅ Corrected Batter Impact for **{applied}** player(s)."
        if blocked:
            text += f"\n⚠️ {len(blocked)} player(s) left unchanged (preview already holds more than the target; see `+commitlb`)."
        await interaction.response.edit_message(content=text, view=self)
        if applied:
            file = discord.File(io.BytesIO(revert.encode("utf-8")), filename="batter_impact_before_fix.txt")
            await interaction.followup.send(
                "Previous values attached. Paste those `+setbatterimpact` lines to undo.", file=file)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.done = True
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="Cancelled. Nothing was changed.", view=self)


class BatterFieldingDropStats(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.latest_status = {}  # channel -> (match_id, innings, score map, monotonic timestamp)
        self.pending = {}
        self._warned_names = set()
        self._init_db()

    async def cog_load(self):
        if AUTO_RECOVER_ENABLED and not self._sweep_task.is_running():
            self._sweep_task.start()

    async def cog_unload(self):
        self._sweep_task.cancel()

    @tasks.loop(seconds=SWEEP_INTERVAL_SECONDS)
    async def _sweep_task(self):
        try:
            self._sweep_orphans()
        except Exception:
            log.exception("batfds: orphan sweep failed")

    @_sweep_task.before_loop
    async def _before_sweep_task(self):
        await self.bot.wait_until_ready()

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
        db.execute("""
            CREATE TABLE IF NOT EXISTS batter_drop_rep_removal_backups (
                backup_no INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL, created_at TEXT NOT NULL,
                source_message_id INTEGER NOT NULL, created_by INTEGER NOT NULL)
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS batter_drop_rep_removal_backup_rows (
                backup_no INTEGER NOT NULL, table_name TEXT NOT NULL,
                user_id INTEGER NOT NULL, matches_affected INTEGER NOT NULL,
                drops INTEGER NOT NULL, runs_affected INTEGER NOT NULL,
                runs_without_drops INTEGER NOT NULL,
                PRIMARY KEY (backup_no, table_name, user_id))
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS batter_drop_recoveries (
                guild_id INTEGER NOT NULL, channel_id INTEGER NOT NULL,
                match_id TEXT NOT NULL, recovered_by INTEGER NOT NULL,
                recovered_at TEXT NOT NULL, sessions INTEGER NOT NULL,
                players INTEGER NOT NULL,
                UNIQUE (guild_id, channel_id))
        """)
        # Remembers which innings were already counted when a match ended, so records that
        # reappear later under the same key are never counted a second time.
        db.execute("""
            CREATE TABLE IF NOT EXISTS batter_drop_session_finalized (
                guild_id INTEGER NOT NULL, session_key TEXT NOT NULL,
                finalized_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, session_key))
        """)
        # Automatic recoveries are tracked per match (channels are reused for later matches).
        db.execute("""
            CREATE TABLE IF NOT EXISTS batter_drop_recovered_matches (
                guild_id INTEGER NOT NULL, channel_id INTEGER NOT NULL,
                match_id TEXT NOT NULL, recovered_by INTEGER NOT NULL,
                recovered_at TEXT NOT NULL, sessions INTEGER NOT NULL,
                players INTEGER NOT NULL, raw_source_id INTEGER,
                corrected INTEGER NOT NULL DEFAULT 0,
                UNIQUE (guild_id, match_id))
        """)
        # Batters already counted for an innings. A late / edited Match Status that still lists
        # them must not recreate their session (that counted the same runs a second time).
        db.execute("""
            CREATE TABLE IF NOT EXISTS batter_drop_out_batters (
                guild_id INTEGER NOT NULL, channel_id INTEGER NOT NULL,
                session_key TEXT NOT NULL, user_id INTEGER NOT NULL,
                PRIMARY KEY (guild_id, channel_id, session_key, user_id))
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

    def _batter_done(self, guild_id, channel_id, session_key, user_id) -> bool:
        return self.bot.db.execute(
            "SELECT 1 FROM batter_drop_out_batters "
            "WHERE guild_id=? AND channel_id=? AND session_key=? AND user_id=?",
            (int(guild_id), int(channel_id), session_key, int(user_id)),
        ).fetchone() is not None

    def _finalize_batter(self, guild_id, channel_id, session_key, user_id, final_score):
        db = self.bot.db
        row = db.execute("""
            SELECT first_drop_score, drop_count FROM batter_drop_sessions
            WHERE guild_id=? AND channel_id=? AND session_key=? AND user_id=?
        """, (guild_id, channel_id, session_key, user_id)).fetchone()
        if not row:
            return
        first_drop, drops = row[0], int(row[1])
        if first_drop is not None and int(final_score) < int(first_drop):
            log.warning("batfds: batter %s finished on %s but their first drop was recorded at %s (channel %s)",
                        user_id, final_score, first_drop, channel_id)
        affected = max(0, int(final_score) - int(first_drop)) if first_drop is not None else 0
        without = int(first_drop) if first_drop is not None else int(final_score)
        self._add_to_preview(guild_id, user_id, drops, affected, without)
        db.execute("""
            DELETE FROM batter_drop_sessions
            WHERE guild_id=? AND channel_id=? AND session_key=? AND user_id=?
        """, (guild_id, channel_id, session_key, user_id))
        db.execute(
            "INSERT OR IGNORE INTO batter_drop_out_batters(guild_id,channel_id,session_key,user_id) VALUES(?,?,?,?)",
            (guild_id, channel_id, session_key, user_id),
        )
        db.commit()

    def _finalize_session(self, guild_id, channel_id, session_key):
        rows = self.bot.db.execute("""
            SELECT user_id,last_score FROM batter_drop_sessions
            WHERE guild_id=? AND channel_id=? AND session_key=?
        """, (guild_id, channel_id, session_key)).fetchall()
        for uid, score in rows:
            self._finalize_batter(guild_id, channel_id, session_key, int(uid), int(score))
        self.bot.db.execute(
            "INSERT OR IGNORE INTO batter_drop_session_finalized(guild_id,session_key,finalized_at) VALUES(?,?,?)",
            (guild_id, session_key, datetime.now(timezone.utc).isoformat(timespec="seconds")),
        )
        self.bot.db.commit()

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
        db = self.bot.db
        # An edited / re-read status from a match that already ended must not start a phantom
        # match or finalise the same scores twice.
        known = db.execute(
            "SELECT ended,last_status_id FROM batter_drop_matches WHERE guild_id=? AND channel_id=?",
            (guild_id, channel_id),
        ).fetchone()
        # Also ignore a status older than the newest one already processed (edited / re-read old
        # messages used to roll the innings back and rewrite scores, which counted runs twice).
        if known and known[1] is not None:
            last_id = int(known[1])
            if message.id < last_id or (int(known[0]) and message.id == last_id):
                return
        if known and not int(known[0]) and status["innings"] == "ONE":
            cur = db.execute(
                "SELECT innings FROM batter_drop_matches WHERE guild_id=? AND channel_id=?",
                (guild_id, channel_id),
            ).fetchone()
            gap = (self._snowflake_ts(message.id) - self._snowflake_ts(known[1])) if known[1] else 0
            if cur and (cur[0] == "TWO" or gap > 4 * 3600):
                # An innings-ONE status after TWO (or after hours of silence) means the previous
                # match ended without the bot seeing it. Count it now instead of merging two matches.
                try:
                    self._mark_match_ended(guild_id, channel_id)
                except (ValueError, sqlite3.Error):
                    log.exception("batfds: could not close unfinished match in channel %s", channel_id)
        match_id, innings = self._get_or_start_match(guild_id, channel_id, status["innings"])
        session_key = self._session_key(match_id, innings)
        for name, score in status["scores"].items():
            uid = find_member_id(message.guild, name)
            if not uid and (channel_id, name) not in self._warned_names:
                self._warned_names.add((channel_id, name))
                log.warning("batfds: could not match batter %r to a member in channel %s; their runs are not tracked",
                            name, channel_id)
            if uid and not self._batter_done(guild_id, channel_id, session_key, uid):
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

    async def _get_status_before(self, message: discord.Message):
        """Scores from the Match Status closest before this message.

        That is the batter's score at the moment of the catch chance. It is read from the
        channel itself instead of a cached copy, so a missed, delayed or out-of-order status
        event cannot give the drop a stale score.
        """
        try:
            async for old in message.channel.history(limit=30, before=message):
                status = parse_match_status(old)
                if not status:
                    continue
                self._update_live_scores(old, status)
                await self._add_tracking_reaction(old)
                cached = self.latest_status.get(message.channel.id)
                if cached and cached[2] == status["scores"]:
                    return cached
                return None  # that status belongs to a match that has already ended
        except (discord.Forbidden, discord.HTTPException):
            pass
        return await self._get_latest_status(message)

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
        if self._batter_done(message.guild.id, message.channel.id, session_key, uid):
            # Already counted for this innings (duplicate "is out!" message): never count twice.
            db.execute(
                "INSERT OR IGNORE INTO batter_drop_finalized(message_id,guild_id,channel_id,user_id) VALUES(?,?,?,?)",
                (message.id, message.guild.id, message.channel.id, uid),
            )
            db.commit()
            return True
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

        if re.search(r"\bMatch\s+has\s+ended\b", text, re.IGNORECASE):
            try:
                self._mark_match_ended(message.guild.id, channel_id)
            except (ValueError, sqlite3.Error):
                pass  # not tracked / already ended
            return

        if "CHANCE OF WICKET!" in upper:
            # An edited / re-delivered chance message must not overwrite the score taken when it was first seen.
            if self.bot.db.execute(
                "SELECT 1 FROM batter_drop_reaction_messages WHERE message_id=?", (message.id,)
            ).fetchone():
                return
            latest = await self._get_status_before(message)
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
                # Names in Match Status can differ from the name in the chance message
                # (nickname vs username, symbols). Match by member id instead.
                for name, score in latest[2].items():
                    if find_member_id(message.guild, name) == user_id:
                        batter_score = score
                        break
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
            if self._batter_done(guild_id, channel_id, session_key, user_id):
                return
            self.bot.db.execute("""
                INSERT INTO batter_drop_sessions
                    (guild_id,channel_id,session_key,user_id,first_drop_score,drop_count,last_score)
                VALUES (?,?,?,?,?,1,?)
                ON CONFLICT(guild_id,channel_id,session_key,user_id) DO UPDATE SET
                    first_drop_score=COALESCE(batter_drop_sessions.first_drop_score,excluded.first_drop_score),
                    drop_count=batter_drop_sessions.drop_count+1
            """, (guild_id, channel_id, session_key, user_id, score, score))
            self.bot.db.commit()
            log.info("batfds: drop recorded for %s at %s runs in channel %s", user_id, score, channel_id)
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
                "INSERT OR IGNORE INTO batter_drop_session_finalized(guild_id,session_key,finalized_at) VALUES(?,?,?)",
                (int(guild_id), session_key, datetime.now(timezone.utc).isoformat(timespec="seconds")),
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
              AND NOT EXISTS (
                  SELECT 1 FROM batter_drop_recoveries r
                  WHERE r.guild_id=s.guild_id AND r.channel_id=s.channel_id
                    AND s.session_key LIKE r.match_id || ':%'
              )
              AND NOT EXISTS (
                  SELECT 1 FROM batter_drop_recovered_matches a
                  WHERE a.guild_id=s.guild_id AND a.channel_id=s.channel_id
                    AND s.session_key LIKE a.match_id || ':%'
              )
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

    def _orphaned_sessions(self, guild_id: int):
        """Ended matches that still hold unfinalised session rows and were not recovered.

        Returns [(channel_id, session_rows), ...]. Such rows are shown provisionally in
        +previewlb but are never written to the preview table, so +commitlb would skip them.
        """
        return self.bot.db.execute("""
            SELECT m.channel_id, COUNT(*) FROM batter_drop_matches m
            JOIN batter_drop_sessions s
              ON s.guild_id=m.guild_id AND s.channel_id=m.channel_id
             AND s.session_key LIKE m.match_id || ':%'
            WHERE m.guild_id=? AND m.ended=1
              AND NOT EXISTS (
                  SELECT 1 FROM batter_drop_recoveries r
                  WHERE r.guild_id=m.guild_id AND r.channel_id=m.channel_id AND r.match_id=m.match_id)
              AND NOT EXISTS (
                  SELECT 1 FROM batter_drop_recovered_matches a
                  WHERE a.guild_id=m.guild_id AND a.match_id=m.match_id)
            GROUP BY m.channel_id ORDER BY m.channel_id
        """, (int(guild_id),)).fetchall()

    @staticmethod
    def _orphan_message(orphans) -> str:
        lines = "\n".join(f"<#{int(ch)}> (`{int(ch)}`) — {int(n)} unfinalised record(s)" for ch, n in orphans)
        return ("Commit is blocked: these ended matches still have unfinalised batter records that would be "
                "left out of the leaderboard:\n" + lines +
                "\nRun `+recoverlb <channel_id>` for each, then `+previewlb`, then try `+commitlb` again.")

    def _commit_preview(self, guild_id: int) -> int:
        db = self.bot.db
        orphans = self._orphaned_sessions(guild_id)
        if orphans:
            raise RuntimeError(self._orphan_message(orphans))
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
        self._safe_sweep()
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

    @previewlb.command(name="markallended")
    @commands.guild_only()
    async def previewlb_markallended(self, ctx: commands.Context):
        """Close every unfinished tracked match in this server at once (owner only, asks to confirm).

        Usage: +previewlb markallended
        """
        if ctx.author.id != BOT_OWNER_ID:
            return await ctx.reply("You are not allowed to use this command.", mention_author=False)
        db = self.bot.db
        active = db.execute(
            "SELECT channel_id,match_id,innings FROM batter_drop_matches "
            "WHERE guild_id=? AND ended=0 ORDER BY channel_id",
            (ctx.guild.id,),
        ).fetchall()
        if not active:
            return await ctx.reply("There are no unfinished tracked matches.", mention_author=False)
        lines = []
        for channel_id, match_id, innings in active:
            n = db.execute(
                "SELECT COUNT(*) FROM batter_drop_sessions WHERE guild_id=? AND channel_id=? AND session_key=?",
                (ctx.guild.id, int(channel_id), self._session_key(str(match_id), str(innings))),
            ).fetchone()[0]
            lines.append(f"<#{int(channel_id)}> — innings **{innings}**, {int(n)} unfinished batter record(s)")
        shown = "\n".join(lines)
        if len(shown) > 1500:
            shown = shown[:1500] + "\n…"
        view = MarkAllEndedConfirmView(self, ctx, [int(r[0]) for r in active])
        await ctx.send(
            f"Mark **{len(active)}** unfinished match(es) as ended?\n{shown}\n"
            "Unfinished batters are finalised with their latest saved scores. Preview data is kept, but score "
            "updates missed after the last Match Status will not be included.",
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
        self._safe_sweep()
        active = self.bot.db.execute(
            "SELECT COUNT(*) FROM batter_drop_matches WHERE guild_id=? AND ended=0",
            (ctx.guild.id,),
        ).fetchone()[0]
        if active:
            return await ctx.reply(
                f"`{active}` tracked match(es) are not marked as ended yet. Commit is blocked to protect unfinished innings.",
                mention_author=False,
            )
        orphans = self._orphaned_sessions(ctx.guild.id)
        if orphans:
            return await ctx.reply(self._orphan_message(orphans), mention_author=False)
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

    # ------------------------------------------------------------------ recovery helpers
    @staticmethod
    def _snowflake_ts(snowflake: int) -> float:
        return ((int(snowflake) >> 22) + 1420070400000) / 1000.0

    def _load_raw_runs(self, guild_id: int, source_id: int) -> dict:
        """Final runs per player from one Hand Cricket raw-stats import (empty if unavailable)."""
        try:
            rows = self.bot.db.execute(
                "SELECT p.user_id,p.runs FROM player_match_stats p "
                "JOIN stats_imports i ON i.source_message_id=p.source_message_id "
                "WHERE i.guild_id=? AND p.source_message_id=?",
                (int(guild_id), int(source_id)),
            ).fetchall()
        except sqlite3.Error:
            return {}
        return {int(u): int(r) for u, r in rows}

    def _find_raw_import(self, guild_id: int, rows, last_status_id):
        """Find the raw-stats import that belongs to a finished match.

        Matches on player overlap, agreement with the saved scores and timing. Returns
        (source_message_id, {user_id: runs}) or None when nothing fits confidently.
        """
        db = self.bot.db
        try:
            imports = [int(r[0]) for r in db.execute(
                "SELECT source_message_id FROM stats_imports WHERE guild_id=?", (int(guild_id),))]
            used = {int(r[0]) for r in db.execute(
                "SELECT raw_source_id FROM batter_drop_recovered_matches "
                "WHERE guild_id=? AND raw_source_id IS NOT NULL", (int(guild_id),))}
        except sqlite3.Error:
            return None
        saved = {int(r[0]): int(r[3]) for r in rows}
        if not saved:
            return None
        ref = self._snowflake_ts(last_status_id) if last_status_id else None
        need = -(-len(saved) * 9 // 10)  # ceil(90%)
        best = None
        for sid in imports:
            if sid in used:
                continue
            delta = 0.0
            if ref is not None:
                delta = self._snowflake_ts(sid) - ref
                if delta < -180 or delta > 3600:
                    continue
            raw = self._load_raw_runs(guild_id, sid)
            common = [u for u in saved if u in raw]
            if len(common) < max(1, need):
                continue
            same = sum(1 for u in common if raw[u] == saved[u])
            if same < 0.6 * len(common):
                continue
            closeness = delta if delta >= 0 else abs(delta) * 5
            key = (same, len(common), -closeness)
            if best is None or key > best[0]:
                best = (key, sid, raw)
        return (best[1], best[2]) if best else None

    def _recover_channel(self, guild_id: int, channel_id: int, recovered_by: int,
                         raw_source_id: int | None = None, auto: bool = False):
        """Atomically add one ended match's saved sessions to the preview.

        Same formulas and _add_to_preview as _finalize_batter/_mark_match_ended. Final scores
        are taken from Hand Cricket's raw stats for the match when it can be identified
        (or is given), otherwise from the saved last score. Session rows are never deleted or
        edited and batter_drop_lb is never touched.
        Returns (sessions, players, corrected, raw_source_id).
        """
        db = self.bot.db
        guild_id, channel_id = int(guild_id), int(channel_id)
        db.execute("BEGIN IMMEDIATE")
        try:
            match = db.execute(
                "SELECT match_id,ended,last_status_id FROM batter_drop_matches WHERE guild_id=? AND channel_id=?",
                (guild_id, channel_id),
            ).fetchone()
            if match is None:
                raise ValueError("No tracked match was found for that channel in this server.")
            if not int(match[1]):
                raise ValueError("That tracked match is not marked as ended, so it cannot be recovered.")
            match_id = str(match[0])
            already = db.execute(
                "SELECT 1 FROM batter_drop_recovered_matches WHERE guild_id=? AND match_id=?",
                (guild_id, match_id),
            ).fetchone()
            if not already:
                if auto:
                    already = db.execute(
                        "SELECT 1 FROM batter_drop_recoveries WHERE guild_id=? AND channel_id=? AND match_id=?",
                        (guild_id, channel_id, match_id),
                    ).fetchone()
                else:
                    # Per match (not per channel), so a channel reused for a later match can be recovered again.
                    already = db.execute(
                        "SELECT 1 FROM batter_drop_recoveries WHERE guild_id=? AND channel_id=? AND match_id=?",
                        (guild_id, channel_id, match_id),
                    ).fetchone()
            if already:
                raise ValueError("That channel has already been recovered.")
            all_rows = db.execute(
                "SELECT user_id,first_drop_score,drop_count,last_score,session_key FROM batter_drop_sessions "
                "WHERE guild_id=? AND channel_id=? AND session_key LIKE ? ORDER BY user_id",
                (guild_id, channel_id, match_id + ":%"),
            ).fetchall()
            if not all_rows:
                raise ValueError("No saved session records exist for that channel's match; nothing to recover.")
            counted_keys = {r[0] for r in db.execute(
                "SELECT session_key FROM batter_drop_session_finalized WHERE guild_id=?", (guild_id,))}
            rows = [r[:4] for r in all_rows if r[4] not in counted_keys]
            if not rows:
                # Everything left over was already counted when the match ended: these are duplicates.
                db.execute(
                    "INSERT OR IGNORE INTO batter_drop_recovered_matches"
                    "(guild_id,channel_id,match_id,recovered_by,recovered_at,sessions,players,raw_source_id,corrected) "
                    "VALUES(?,?,?,?,?,0,0,NULL,0)",
                    (guild_id, channel_id, match_id, int(recovered_by),
                     datetime.now(timezone.utc).isoformat(timespec="seconds")),
                )
                db.commit()
                return 0, 0, 0, None

            raw, raw_id = {}, None
            if raw_source_id is not None:
                raw = self._load_raw_runs(guild_id, int(raw_source_id))
                if not raw:
                    raise ValueError("That raw stats message was not found in this server's imported stats.")
                raw_id = int(raw_source_id)
            else:
                found = self._find_raw_import(guild_id, rows, match[2])
                if found:
                    raw_id, raw = found

            players, corrected = set(), 0
            for uid, first_drop, drops, last_score in rows:
                uid = int(uid)
                final = int(last_score)
                if uid in raw and raw[uid] != final:
                    final = raw[uid]
                    corrected += 1
                affected = max(0, final - int(first_drop)) if first_drop is not None else 0
                without = int(first_drop) if first_drop is not None else final
                self._add_to_preview(guild_id, uid, int(drops), affected, without)
                players.add(uid)
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            if auto:
                db.execute(
                    "INSERT INTO batter_drop_recovered_matches"
                    "(guild_id,channel_id,match_id,recovered_by,recovered_at,sessions,players,raw_source_id,corrected) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (guild_id, channel_id, match_id, 0, now, len(rows), len(players), raw_id, corrected),
                )
            else:
                # recoveries is UNIQUE per channel: move an older match's row into recovered_matches
                # (which is per match and checked everywhere) so this match can be stored.
                db.execute(
                    "INSERT OR IGNORE INTO batter_drop_recovered_matches"
                    "(guild_id,channel_id,match_id,recovered_by,recovered_at,sessions,players,raw_source_id,corrected) "
                    "SELECT guild_id,channel_id,match_id,recovered_by,recovered_at,sessions,players,NULL,0 "
                    "FROM batter_drop_recoveries WHERE guild_id=? AND channel_id=? AND match_id<>?",
                    (guild_id, channel_id, match_id),
                )
                db.execute(
                    "DELETE FROM batter_drop_recoveries WHERE guild_id=? AND channel_id=? AND match_id<>?",
                    (guild_id, channel_id, match_id),
                )
                db.execute(
                    "INSERT INTO batter_drop_recoveries"
                    "(guild_id,channel_id,match_id,recovered_by,recovered_at,sessions,players) VALUES(?,?,?,?,?,?,?)",
                    (guild_id, channel_id, match_id, int(recovered_by), now, len(rows), len(players)),
                )
                db.execute(
                    "INSERT OR IGNORE INTO batter_drop_recovered_matches"
                    "(guild_id,channel_id,match_id,recovered_by,recovered_at,sessions,players,raw_source_id,corrected) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (guild_id, channel_id, match_id, int(recovered_by), now, len(rows), len(players), raw_id, corrected),
                )
            db.commit()
            return len(rows), len(players), corrected, raw_id
        except sqlite3.IntegrityError:
            db.rollback()
            raise ValueError("That channel has already been recovered.")
        except Exception:
            db.rollback()
            raise

    def _sweep_orphans(self):
        """Automatically count ended matches whose batter records were never finalised."""
        db = self.bot.db
        if db.in_transaction:
            return []
        try:
            candidates = db.execute("""
                SELECT m.guild_id, m.channel_id, m.last_status_id FROM batter_drop_matches m
                JOIN batter_drop_sessions s
                  ON s.guild_id=m.guild_id AND s.channel_id=m.channel_id
                 AND s.session_key LIKE m.match_id || ':%'
                WHERE m.ended=1
                  AND NOT EXISTS (SELECT 1 FROM batter_drop_recoveries r
                                  WHERE r.guild_id=m.guild_id AND r.channel_id=m.channel_id AND r.match_id=m.match_id)
                  AND NOT EXISTS (SELECT 1 FROM batter_drop_recovered_matches a
                                  WHERE a.guild_id=m.guild_id AND a.match_id=m.match_id)
                GROUP BY m.guild_id, m.channel_id
            """).fetchall()
        except sqlite3.Error:
            log.exception("batfds: could not look for unfinalised matches")
            return []
        done = []
        for guild_id, channel_id, last_status_id in candidates:
            if last_status_id is not None and time.time() - self._snowflake_ts(last_status_id) < AUTO_RECOVER_GRACE_SECONDS:
                continue
            try:
                result = self._recover_channel(guild_id, channel_id, 0, auto=True)
            except ValueError as exc:
                log.info("batfds: skipped auto-recovery of channel %s: %s", channel_id, exc)
                continue
            except sqlite3.Error:
                log.exception("batfds: auto-recovery of channel %s failed and was rolled back", channel_id)
                break
            if result[0] == 0:
                log.warning("batfds: channel %s had leftover records that were already counted; ignored them",
                            channel_id)
                done.append((int(guild_id), int(channel_id), result))
                continue
            log.warning(
                "batfds: match in channel %s had ended but was never finalised; auto-recovered "
                "%s session(s) for %s player(s), %s score(s) corrected from raw stats %s",
                channel_id, result[0], result[1], result[2], result[3],
            )
            done.append((int(guild_id), int(channel_id), result))
        return done

    def _safe_sweep(self):
        if not AUTO_RECOVER_ENABLED:
            return
        try:
            self._sweep_orphans()
        except Exception:
            log.exception("batfds: orphan sweep failed")

    def _recoverable_channels(self, guild_id: int):
        """Ended matches whose batter records were never counted: [{channel_id, match_id, ...}]."""
        db = self.bot.db
        guild_id = int(guild_id)
        matches = db.execute("""
            SELECT m.channel_id,m.match_id,m.innings,m.last_status_id FROM batter_drop_matches m
            WHERE m.guild_id=? AND m.ended=1
              AND NOT EXISTS (SELECT 1 FROM batter_drop_recoveries r
                              WHERE r.guild_id=m.guild_id AND r.channel_id=m.channel_id AND r.match_id=m.match_id)
              AND NOT EXISTS (SELECT 1 FROM batter_drop_recovered_matches a
                              WHERE a.guild_id=m.guild_id AND a.match_id=m.match_id)
            ORDER BY m.channel_id
        """, (guild_id,)).fetchall()
        counted = {r[0] for r in db.execute(
            "SELECT session_key FROM batter_drop_session_finalized WHERE guild_id=?", (guild_id,))}
        out = []
        for channel_id, match_id, innings, last_status_id in matches:
            rows = [r for r in db.execute(
                "SELECT user_id,first_drop_score,drop_count,last_score,session_key FROM batter_drop_sessions "
                "WHERE guild_id=? AND channel_id=? AND session_key LIKE ?",
                (guild_id, int(channel_id), str(match_id) + ":%")).fetchall() if r[4] not in counted]
            if not rows:
                continue
            raw = self._find_raw_import(guild_id, [r[:4] for r in rows], last_status_id)
            out.append({"channel_id": int(channel_id), "match_id": str(match_id), "innings": innings,
                        "last_status_id": last_status_id, "records": len(rows),
                        "players": len({int(r[0]) for r in rows}), "raw_id": raw[0] if raw else None})
        return out

    async def _send_recoverable(self, ctx: commands.Context):
        ready = self._recoverable_channels(ctx.guild.id)
        unfinished = self.bot.db.execute(
            "SELECT channel_id,innings FROM batter_drop_matches WHERE guild_id=? AND ended=0 ORDER BY channel_id",
            (ctx.guild.id,),
        ).fetchall()
        parts = []
        if ready:
            lines = []
            for it in ready:
                when = ""
                if it["last_status_id"]:
                    when = f", last update <t:{int(self._snowflake_ts(it['last_status_id']))}:R>"
                raw = ("raw stats found ✅ (scores will be checked)" if it["raw_id"]
                       else "no raw stats found (saved scores will be used)")
                lines.append(f"<#{it['channel_id']}> — {it['records']} record(s), {it['players']} player(s), "
                             f"innings {it['innings']}{when}\n    {raw} → `+recoverlb {it['channel_id']}`")
            parts.append(f"**Can be recovered ({len(ready)})**\n" + "\n".join(lines))
        else:
            parts.append("**Nothing to recover.** No ended match has uncounted batter records.")
        if unfinished:
            lines = [f"<#{int(c)}> — innings {i}" for c, i in unfinished]
            parts.append(f"**Not ended yet ({len(unfinished)})** — close them first with "
                         "`+previewlb markended <channel_id>` or `+previewlb markallended`:\n" + "\n".join(lines))
        text = "\n\n".join(parts)
        if len(text) > 1950:
            text = text[:1950] + "\n…"
        await ctx.reply(text, mention_author=False, allowed_mentions=discord.AllowedMentions.none())

    @commands.command(name="recoverlb")
    @commands.guild_only()
    async def recoverlb(self, ctx: commands.Context, channel_id: int = None, raw_message_id: int = None):
        """Recover an ended match's unfinalised batter sessions into the preview.

        Usage: +recoverlb                 -> list the channels that can be recovered
               +recoverlb <channel_id> [raw_stats_message_id]
        Final scores are checked against Hand Cricket's raw stats for that match.
        """
        if ctx.author.id != BOT_OWNER_ID:
            return await ctx.reply("You are not allowed to use this command.", mention_author=False)
        if channel_id is None:
            return await self._send_recoverable(ctx)
        try:
            sessions, players, corrected, raw_id = self._recover_channel(
                ctx.guild.id, channel_id, ctx.author.id, raw_source_id=raw_message_id)
        except ValueError as exc:
            return await ctx.reply(str(exc), mention_author=False)
        except Exception as exc:
            return await ctx.reply(f"Recovery failed and was rolled back: `{type(exc).__name__}: {exc}`",
                                   mention_author=False)
        if sessions == 0:
            return await ctx.reply(
                f"<#{int(channel_id)}>: these records were already counted when the match ended, so nothing "
                "was added. The leftover duplicates are now ignored.", mention_author=False)
        if raw_id is None:
            note = "No matching Hand Cricket raw stats were found, so the saved scores were used."
        else:
            note = (f"Final scores were checked against raw stats message `{raw_id}`; "
                    f"**{corrected}** player score(s) corrected.")
        await ctx.reply(
            f"Recovered <#{int(channel_id)}>: **{sessions}** session record(s) for **{players}** player(s) "
            f"added to the preview leaderboard. {note} Check it with `+previewlb`. "
            "Nothing was committed to `+lb`.",
            mention_author=False,
        )

    # ------------------------------------------------------------------ audit
    def _audit(self, guild_id: int):
        """Compare recorded Batter Impact runs with Hand Cricket's raw match stats.

        Recorded = permanent +lb + preview (+ provisional live sessions).
        Expected = total runs per player across the guild's imported raw stats.
        """
        db = self.bot.db
        guild_id = int(guild_id)
        expected = {int(u): (int(r), int(n)) for u, r, n in db.execute(
            "SELECT p.user_id,SUM(p.runs),COUNT(*) FROM player_match_stats p "
            "JOIN stats_imports i ON i.source_message_id=p.source_message_id "
            "WHERE i.guild_id=? GROUP BY p.user_id", (guild_id,))}
        lb = {int(u): (int(a), int(w), int(d)) for u, a, w, d in db.execute(
            "SELECT user_id,runs_affected,runs_without_drops,drops FROM batter_drop_lb WHERE guild_id=?",
            (guild_id,))}
        prev = self._preview_rows(guild_id)  # uid -> [matches, drops, affected, without]
        live = {int(u) for (u,) in db.execute(
            "SELECT DISTINCT s.user_id FROM batter_drop_sessions s "
            "JOIN batter_drop_matches m ON m.guild_id=s.guild_id AND m.channel_id=s.channel_id "
            "AND s.session_key LIKE m.match_id || ':%' WHERE s.guild_id=? AND m.ended=0", (guild_id,))}
        reps = {int(u) for (u,) in db.execute(
            "SELECT DISTINCT r.user_id FROM batter_drop_rep_removal_backup_rows r "
            "JOIN batter_drop_rep_removal_backups b ON b.backup_no=r.backup_no WHERE b.guild_id=?", (guild_id,))}
        wrong, skipped_live, skipped_rep, no_raw = [], [], [], []
        for uid in sorted(set(expected) | set(lb) | set(prev)):
            la, lw, ld = lb.get(uid, (0, 0, 0))
            pm, pd, pa, pw = prev.get(uid, [0, 0, 0, 0])
            after, without, drops = la + pa, lw + pw, ld + pd
            recorded = after + without
            exp = expected.get(uid, (0, 0))[0]
            if recorded == exp:
                continue
            delta = exp - recorded
            if delta > 0:
                if after > 0 or drops > 0:
                    new_after, new_without = after + delta, without
                else:
                    new_after, new_without = after, without + delta
            else:
                cut = -delta
                take = min(without, cut)
                new_without, new_after = without - take, max(0, after - (cut - take))
            item = {"user": uid, "recorded": recorded, "after": after, "without": without, "drops": drops,
                    "expected": exp, "new_after": new_after, "new_without": new_without}
            if uid in live:
                skipped_live.append(item)
            elif uid in reps:
                skipped_rep.append(item)
            elif uid not in expected:
                no_raw.append(item)
            else:
                wrong.append(item)
        try:
            fielder_drops = int(db.execute(
                "SELECT COALESCE(SUM(f.catches_dropped),0) FROM player_match_fielding f "
                "JOIN stats_imports i ON i.source_message_id=f.source_message_id WHERE i.guild_id=?",
                (guild_id,)).fetchone()[0])
        except sqlite3.Error:
            fielder_drops = None
        batter_drops = sum(v[2] for v in lb.values()) + sum(v[1] for v in prev.values())
        return {"wrong": wrong, "live": skipped_live, "rep": skipped_rep, "no_raw": no_raw,
                "checked": len(expected), "fielder_drops": fielder_drops, "batter_drops": batter_drops}

    @commands.command(name="auditbattingimpact", aliases=["auditbatterimpact", "audittbattingimpact"])
    @commands.guild_only()
    async def audit_batting_impact(self, ctx: commands.Context, member: discord.Member = None):
        """Check every player's Batter Impact runs against Hand Cricket's raw match stats.

        Usage: +auditbattingimpact [@player]
        Read-only: nothing is changed. Wrong players are listed with what they should have.
        """
        if ctx.author.id != BOT_OWNER_ID:
            return await ctx.reply("You are not allowed to use this command.", mention_author=False)
        self._safe_sweep()
        try:
            result = self._audit(ctx.guild.id)
        except sqlite3.Error as exc:
            return await ctx.reply(f"Audit failed: `{type(exc).__name__}: {exc}`", mention_author=False)

        def label(uid):
            m = ctx.guild.get_member(uid)
            return discord.utils.escape_markdown(m.display_name) if m else f"Unknown"

        def line(it):
            gap = it["expected"] - it["recorded"]
            return (f"<@{it['user']}> ({label(it['user'])}) — recorded **{it['recorded']}** "
                    f"(after {it['after']} / without {it['without']}), should be **{it['expected']}** "
                    f"({'short by' if gap > 0 else 'over by'} {abs(gap)}) → "
                    f"`+setbatterimpact <@{it['user']}> {it['new_after']} {it['new_without']}`")

        items = result["wrong"]
        if member is not None:
            hit = [it for it in items + result["live"] + result["rep"] + result["no_raw"] if it["user"] == member.id]
            if not hit:
                return await ctx.reply(f"{discord.utils.escape_markdown(member.display_name)}'s Batter Impact "
                                       "runs match Hand Cricket's match stats.", mention_author=False)
            return await ctx.reply(line(hit[0]) + "\n_Run `+commitlb` first if there is preview data, then apply._",
                                   mention_author=False, allowed_mentions=discord.AllowedMentions.none())
        summary = [
            f"**Batter Impact audit** — {result['checked']} player(s) with match stats checked.",
            f"❌ Wrong: **{len(items)}**",
        ]
        if result["no_raw"]:
            summary.append(f"ℹ️ Recorded runs but no match stats (manual edits?): {len(result['no_raw'])}")
        if result["rep"]:
            summary.append(f"ℹ️ Skipped (rep removal applied, cannot verify exactly): {len(result['rep'])}")
        if result["live"]:
            summary.append(f"ℹ️ Skipped (match still in progress): {len(result['live'])}")
        if result["fielder_drops"] is not None:
            summary.append(f"Drops: match reports show **{result['fielder_drops']}** dropped catches; "
                           f"Batter Impact records **{result['batter_drops']}**.")
        if not items:
            summary.append("✅ Every checked player matches.")
            return await ctx.reply("\n".join(summary), mention_author=False)
        footer = ("\n\nRun `+fixbatterimpact` to correct every ❌ player in one go.\n_Totals are exact. The after/without split is a best guess: if a player had a drop the "
                  "missing runs go to \"after\", otherwise to \"without\". Run `+commitlb` first if there is "
                  "preview data, then apply. Players you edited with +setbatterimpact, or everyone after a "
                  "reset, will also show here. Full list attached._")
        head = "\n".join(summary) + "\n\n"
        budget = 1950 - len(head) - len(footer) - 40
        shown = []
        for it in items:
            text_line = line(it)
            if sum(len(x) + 1 for x in shown) + len(text_line) > budget:
                break
            shown.append(text_line)
        text = head + "\n".join(shown)
        if len(shown) < len(items):
            text += f"\n…and {len(items) - len(shown)} more."
        text += footer
        full = "\n".join(line(it) for it in items + result["no_raw"] + result["rep"])
        file = discord.File(io.BytesIO(full.encode("utf-8")), filename="batter_impact_audit.txt")
        await ctx.reply(text, file=file, mention_author=False,
                        allowed_mentions=discord.AllowedMentions.none())

    def _plan_fix(self, guild_id: int):
        """Work out the permanent +lb value that makes (+lb + preview) equal Hand Cricket's raw total."""
        result = self._audit(guild_id)
        prev = self._preview_rows(guild_id)
        plan, blocked = [], []
        for it in result["wrong"]:
            _, _, pa, pw = prev.get(it["user"], [0, 0, 0, 0])
            lb_after, lb_without = it["new_after"] - pa, it["new_without"] - pw
            if lb_after < 0 or lb_without < 0:
                blocked.append(it)
                continue
            plan.append({**it, "lb_after": lb_after, "lb_without": lb_without})
        return result, plan, blocked

    def _apply_fix(self, guild_id: int, plan):
        """Write the plan in one transaction. Returns (players_changed, text_to_undo)."""
        db = self.bot.db
        guild_id = int(guild_id)
        undo = []
        db.execute("BEGIN IMMEDIATE")
        try:
            for p in plan:
                old = db.execute(
                    "SELECT runs_affected,runs_without_drops FROM batter_drop_lb WHERE guild_id=? AND user_id=?",
                    (guild_id, p["user"]),
                ).fetchone()
                oa, ow = (int(old[0]), int(old[1])) if old else (0, 0)
                undo.append(f"+setbatterimpact <@{p['user']}> {oa} {ow}")
                db.execute("""
                    INSERT INTO batter_drop_lb (guild_id,user_id,matches_affected,drops,runs_affected,runs_without_drops)
                    VALUES (?,?,0,0,?,?)
                    ON CONFLICT(guild_id,user_id) DO UPDATE SET
                        runs_affected=excluded.runs_affected,runs_without_drops=excluded.runs_without_drops
                """, (guild_id, p["user"], p["lb_after"], p["lb_without"]))
            db.commit()
        except Exception:
            db.rollback()
            raise
        return len(plan), "\n".join(undo)

    @commands.command(name="fixbatterimpact", aliases=["fixallbatterimpact", "correctbatterimpact"])
    @commands.guild_only()
    async def fix_batter_impact(self, ctx: commands.Context):
        """Correct every wrong player from +auditbatterimpact in one go (owner only, asks to confirm).

        Usage: +fixbatterimpact
        Same result as running the suggested +setbatterimpact line for each player.
        """
        if ctx.author.id != BOT_OWNER_ID:
            return await ctx.reply("You are not allowed to use this command.", mention_author=False)
        self._safe_sweep()
        try:
            result, plan, blocked = self._plan_fix(ctx.guild.id)
        except sqlite3.Error as exc:
            return await ctx.reply(f"Audit failed: `{type(exc).__name__}: {exc}`", mention_author=False)
        if not plan and not blocked:
            return await ctx.reply("✅ Nothing to fix: every checked player matches Hand Cricket's match stats.",
                                   mention_author=False)

        def label(uid):
            m = ctx.guild.get_member(uid)
            return discord.utils.escape_markdown(m.display_name) if m else f"Unknown ({str(uid)[-4:]})"

        head = [f"Correct **{len(plan)}** player(s)? Totals will be set to Hand Cricket's raw match stats."]
        shown = []
        for p in plan:
            gap = p["expected"] - p["recorded"]
            shown.append(f"**{label(p['user'])}** {p['recorded']} → {p['expected']} "
                         f"({'+' if gap > 0 else '-'}{abs(gap)})")
        body = "\n".join(shown[:15])
        if len(shown) > 15:
            body += f"\n…and {len(shown) - 15} more."
        notes = []
        if blocked:
            notes.append(f"⚠️ {len(blocked)} player(s) will be skipped: the preview already holds more than their "
                         "correct total. Run `+commitlb` and audit again.")
        skipped = len(result["live"]) + len(result["rep"]) + len(result["no_raw"])
        if skipped:
            notes.append(f"ℹ️ {skipped} player(s) are never auto-fixed (match still live, rep removal applied, "
                         "or no raw stats).")
        if self._orphaned_sessions(ctx.guild.id):
            notes.append("⚠️ Some ended matches are not recovered yet (`+recoverlb`); their provisional runs are included.")
        notes.append("Drops/matches counts and the preview are not touched. A file with the old values is sent afterwards.")
        text = "\n".join(head) + ("\n" + body if body else "") + "\n" + "\n".join(notes)
        if not plan:
            return await ctx.reply(text.split("\n", 1)[1] if "\n" in text else text, mention_author=False)
        await ctx.send(text[:1990], view=FixBatterImpactConfirmView(self, ctx),
                       allowed_mentions=discord.AllowedMentions.none())

    @commands.command(name="removerep")
    @commands.guild_only()
    async def remove_rep_stats(self, ctx: commands.Context):
        """Remove rep runs from Batter Impact by replying to Hand Cricket raw stats."""
        if not await owner_or_whitelisted(ctx, "removerep"):
            return await ctx.reply(
                "❌ Only the bot owner or a role whitelisted for `+removerep` can use this command.",
                mention_author=False,
            )

        reference = ctx.message.reference
        if reference is None or reference.message_id is None:
            return await ctx.reply(
                "Reply to the Hand Cricket raw statistics message, then run `+removerep`.",
                mention_author=False,
            )
        try:
            raw_message = reference.resolved
            if raw_message is None:
                raw_message = await ctx.channel.fetch_message(reference.message_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return await ctx.reply("I couldn't read the replied-to message.", mention_author=False)

        if raw_message.author.id != HANDCRICKET_BOT_ID:
            return await ctx.reply("The replied-to message is not from the Hand Cricket bot.", mention_author=False)
        rep_records = parse_rep_raw_stats(raw_message)
        if not rep_records:
            return await ctx.reply(
                "I couldn't find valid rep records in the final separate code block. "
                "Make sure you replied to the complete raw statistics message.",
                mention_author=False,
            )

        db = self.bot.db
        guild_id = int(ctx.guild.id)
        eligible = []
        skipped = []
        for user_id, rep_runs in rep_records:
            preview = db.execute(
                "SELECT runs_affected,runs_without_drops FROM batter_drop_preview WHERE guild_id=? AND user_id=?",
                (guild_id, user_id),
            ).fetchone()
            permanent = db.execute(
                "SELECT runs_affected,runs_without_drops FROM batter_drop_lb WHERE guild_id=? AND user_id=?",
                (guild_id, user_id),
            ).fetchone()
            after_drops = (int(preview[0]) if preview else 0) + (int(permanent[0]) if permanent else 0)
            before_drops = (int(preview[1]) if preview else 0) + (int(permanent[1]) if permanent else 0)

            if after_drops > 0:
                skipped.append((user_id, f"has {after_drops} runs after catch drops"))
            elif not preview and not permanent:
                skipped.append((user_id, "has no Batter Impact record"))
            elif before_drops < rep_runs:
                skipped.append((user_id, f"only {before_drops} before-drop runs are recorded; cannot subtract {rep_runs}"))
            else:
                eligible.append((user_id, rep_runs))

        if not eligible:
            lines = [f"<@{uid}> — {reason}" for uid, reason in skipped]
            return await ctx.reply(
                "No rep stats were removed.\n" + "\n".join(lines),
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )

        created = datetime.now(timezone.utc).isoformat(timespec="seconds")
        db.execute("BEGIN IMMEDIATE")
        try:
            cur = db.execute(
                "INSERT INTO batter_drop_rep_removal_backups(guild_id,created_at,source_message_id,created_by) "
                "VALUES(?,?,?,?)",
                (guild_id, created, int(raw_message.id), int(ctx.author.id)),
            )
            backup_no = int(cur.lastrowid)
            for user_id, rep_runs in eligible:
                rows = []
                for table_name in ("batter_drop_preview", "batter_drop_lb"):
                    row = db.execute(
                        f"SELECT matches_affected,drops,runs_affected,runs_without_drops FROM {table_name} "
                        "WHERE guild_id=? AND user_id=?",
                        (guild_id, user_id),
                    ).fetchone()
                    if row is not None:
                        rows.append((table_name, row))
                        db.execute(
                            "INSERT INTO batter_drop_rep_removal_backup_rows "
                            "(backup_no,table_name,user_id,matches_affected,drops,runs_affected,runs_without_drops) "
                            "VALUES(?,?,?,?,?,?,?)",
                            (backup_no, table_name, user_id, *[int(v) for v in row]),
                        )

                remaining = int(rep_runs)
                # Subtract from preview first, then permanent +lb if the preview total is insufficient.
                for table_name, row in rows:
                    available = int(row[3])
                    take = min(available, remaining)
                    if take:
                        db.execute(
                            f"UPDATE {table_name} SET runs_without_drops=runs_without_drops-? "
                            "WHERE guild_id=? AND user_id=?",
                            (take, guild_id, user_id),
                        )
                        remaining -= take
                    if remaining == 0:
                        break
                if remaining:
                    raise RuntimeError(f"Unexpected balance mismatch for rep {user_id}; transaction cancelled")
            db.commit()
        except Exception:
            db.rollback()
            raise

        removed_lines = [f"<@{uid}> — subtracted **{runs}** runs from before-catch-drop runs"
                         for uid, runs in eligible]
        skipped_lines = [f"<@{uid}> — {reason}; not changed" for uid, reason in skipped]
        report = [
            "✅ **Rep stats removal complete.**",
            f"Automatic backup created: `Rep removal backup #{backup_no}`.",
            *removed_lines,
        ]
        if skipped_lines:
            report.extend(["\n⚠️ **Skipped reps:**", *skipped_lines])
        await ctx.reply(
            "\n".join(report),
            mention_author=False,
            allowed_mentions=discord.AllowedMentions.none(),
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

    @commands.command(name="resetbatterimpact")
    @commands.guild_only()
    async def reset_batter_impact(self, ctx: commands.Context, member: discord.Member = None):
        if ctx.author.id != BOT_OWNER_ID:
            return await ctx.reply("You are not allowed to use this command.", mention_author=False)
        if member is None:
            self.bot.db.execute("DELETE FROM batter_drop_lb WHERE guild_id=?", (ctx.guild.id,))
            self.bot.db.execute("DELETE FROM batter_drop_preview WHERE guild_id=?", (ctx.guild.id,))
            self.bot.db.execute("DELETE FROM batter_drop_sessions WHERE guild_id=?", (ctx.guild.id,))
            self.bot.db.execute("DELETE FROM batter_drop_matches WHERE guild_id=?", (ctx.guild.id,))
            self.bot.db.execute("DELETE FROM batter_drop_pending WHERE guild_id=?", (ctx.guild.id,))
            self.bot.db.execute("DELETE FROM batter_drop_finalized WHERE guild_id=?", (ctx.guild.id,))
            self.bot.db.execute("DELETE FROM batter_drop_out_batters WHERE guild_id=?", (ctx.guild.id,))
            await ctx.reply("Reset Batter Impact stats and preview tracking for everyone in this server.",
                            mention_author=False)
        else:
            self.bot.db.execute("DELETE FROM batter_drop_lb WHERE guild_id=? AND user_id=?",
                                (ctx.guild.id, member.id))
            self.bot.db.execute("DELETE FROM batter_drop_preview WHERE guild_id=? AND user_id=?",
                                (ctx.guild.id, member.id))
            self.bot.db.execute("DELETE FROM batter_drop_sessions WHERE guild_id=? AND user_id=?",
                                (ctx.guild.id, member.id))
            self.bot.db.execute("DELETE FROM batter_drop_pending WHERE guild_id=? AND user_id=?",
                                (ctx.guild.id, member.id))
            self.bot.db.execute("DELETE FROM batter_drop_finalized WHERE guild_id=? AND user_id=?",
                                (ctx.guild.id, member.id))
            self.bot.db.execute("DELETE FROM batter_drop_out_batters WHERE guild_id=? AND user_id=?",
                                (ctx.guild.id, member.id))
            await ctx.reply(f"Reset Batter Impact stats for **{discord.utils.escape_markdown(member.display_name)}**.",
                            mention_author=False)
        self.bot.db.commit()


async def setup(bot: commands.Bot):
    await bot.add_cog(BatterFieldingDropStats(bot))
