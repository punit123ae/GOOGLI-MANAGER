"""Batter Catch-Drop Impact tracking for Googli Manager.

Tracks, for every batter, two numbers while Hand Cricket matches are played in
channels where fielding tracking is enabled (``fielding_tracking_channels``):

* Runs Without Catch Drops - the batter's individual score at their FIRST
  dropped catch (their whole score if they were never dropped).
* Runs After Catch Drops   - final individual score minus that baseline.

Everything lives in the bot's existing SQLite database (``bot.db``) so it
survives restarts.  Nothing reaches the permanent ``+lb`` (table
``batter_drop_lb``, already read by lb.py) until the owner runs ``+commitlb``.

This file does NOT touch fielding.py's catch counters; it only listens to the
same Hand Cricket messages independently.

Owner commands
    +previewlb                      live preview of uncommitted statistics
    +previewlb backups              list backups
    +previewlb <n>                  view backup n
    +previewlb delete <n>           delete backup n
    +previewlb markended [channel]  finalise an unfinished match by hand
    +previewlb raw                  last raw Hand Cricket messages seen (debug)
    +commitlb                       move preview into the permanent +lb
    +setbatterimpact @user <runs_after_drops> <runs_without_drops>
    +resetbatterimpact [@user]
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import re
import time
import traceback
from collections import deque
from typing import Any, Callable, Optional

import discord
from discord.ext import commands

HANDCRICKET_BOT_ID = 753191385296928808
BOT_OWNER_ID = 1014568928682459198

CHECK_EMOJI = "☑️"
# A catch opportunity is forgotten if its result does not arrive in this time.
PENDING_TTL_SECONDS = 120
# A match with no activity for this long is treated as abandoned when a new
# Match Status shows up in the same channel.
STALE_MATCH_SECONDS = 12 * 60 * 60
SEEN_RETENTION_SECONDS = 7 * 24 * 60 * 60
CONFIRM_TIMEOUT_SECONDS = 60
MAX_MANUAL_VALUE = 1_000_000
PAGE_SIZE = 10
COMMIT_PREVIEW_LINES = 25
BLUE = discord.Colour.from_rgb(20, 117, 255)
NO_MENTIONS = discord.AllowedMentions.none()


# =============================================================================
# PARSING  (the only part that depends on Hand Cricket's exact message layout)
# =============================================================================
# If the bot's wording ever changes, this is the only section that needs edits.
# +previewlb raw shows the last raw messages the cog saw, for tuning.

INNINGS_RE = re.compile(
    r"\binnings\s*[:#\-]?\s*(one|two|1|2)\b|\b(1st|2nd|first|second)\s+innings\b",
    re.IGNORECASE,
)
_INNINGS_NO = {"one": 1, "1": 1, "1st": 1, "first": 1,
               "two": 2, "2": 2, "2nd": 2, "second": 2}
MENTION_RE = re.compile(r"<@!?(\d+)>")
BATTER_LABEL_RE = re.compile(
    r"\b(?:batter|batsman|batting|striker|on\s+strike)\b", re.IGNORECASE)
_NAME_CUT_RE = re.compile(r"\s[|•·]\s|[|•·(\[\n]|\s[-–—]\s")

# Individual-score patterns, tried in order.  Team totals such as "45/2" are
# deliberately NOT accepted (negative look-ahead on a slash).
_SCORE_PATTERNS = [
    re.compile(r"(?:runs?|score|scored)\s*[:=\-]?\s*(\d+)\b(?!\s*/)", re.IGNORECASE),
    re.compile(r"\b(\d+)\*?\s*\(\s*\d+\s*(?:balls?|b)?\s*\)", re.IGNORECASE),
    re.compile(r"\b(\d+)\s+runs?\b", re.IGNORECASE),
    re.compile(r"(?:^|\s)[-–—:=]\s*\**(\d+)\b(?!\s*[/.])"),
]


def strip_markdown(text: str) -> str:
    return re.sub(r"\*\*|__|`|~~|\|\|", "", text)


def message_text(message) -> str:
    """Message content plus every embed title/description/field/footer."""
    parts = [getattr(message, "content", "") or ""]
    for embed in getattr(message, "embeds", []) or []:
        author = getattr(embed, "author", None)
        parts.append(getattr(author, "name", "") or "")
        parts.append(getattr(embed, "title", "") or "")
        parts.append(getattr(embed, "description", "") or "")
        for field in getattr(embed, "fields", []) or []:
            parts.append(f"{getattr(field, 'name', '') or ''}\n{getattr(field, 'value', '') or ''}")
        footer = getattr(embed, "footer", None)
        parts.append(getattr(footer, "text", "") or "")
    return "\n".join(p for p in parts if p)


def classify(text: str) -> Optional[str]:
    """Return ended / chance / dropped / taken / out / status, or None."""
    up = text.upper()
    if "MATCH HAS ENDED" in up:
        return "ended"
    if "CHANCE OF WICKET" in up:
        return "chance"
    if "BOZO DROPPED THE CATCH" in up:
        return "dropped"
    if "TOOK THE CATCH" in up:
        return "taken"
    if "BATTER OUT" in up:
        return "out"
    labels = {_INNINGS_NO[(m.group(1) or m.group(2)).lower()] for m in INNINGS_RE.finditer(text)}
    if labels == {1, 2} or ("MATCH STATUS" in up and labels):
        return "status"
    return None


def find_score(text: str) -> Optional[int]:
    for pattern in _SCORE_PATTERNS:
        match = pattern.search(text)
        if match:
            return int(match.group(1))
    return None


def _member_names(member) -> list[str]:
    names = []
    for attr in ("name", "display_name", "global_name", "nick"):
        value = getattr(member, attr, None)
        if value and len(value) >= 2 and value not in names:
            names.append(value)
    return names


def resolve_name(name: str, guild) -> Optional[int]:
    """Exact (case-insensitive) member lookup by username/nickname."""
    wanted = name.strip().strip("@").strip().casefold()
    if not wanted or guild is None:
        return None
    for member in getattr(guild, "members", []):
        if any(n.casefold() == wanted for n in _member_names(member)):
            return int(member.id)
    return None


def find_member_in_text(text: str, guild, prefer_id: Optional[int] = None) -> Optional[int]:
    """Find which guild member is named (by mention or by name) in ``text``.

    ``prefer_id`` (the batter from the latest Match Status) wins if present.
    Otherwise the earliest mention/name in the text is returned.
    """
    folded = text.casefold()
    mentions = [int(m) for m in MENTION_RE.findall(text)]
    if prefer_id is not None:
        if prefer_id in mentions:
            return prefer_id
        member = guild.get_member(prefer_id) if guild is not None else None
        if member is not None:
            for name in _member_names(member):
                if _word_index(folded, name.casefold()) >= 0:
                    return prefer_id
    best: tuple[int, int, int] | None = None  # (position, -length, id)
    if mentions:
        best = (text.index(f"<@"), 0, mentions[0])
    if guild is not None:
        for member in getattr(guild, "members", []):
            for name in _member_names(member):
                if len(name) < 3:
                    continue
                pos = _word_index(folded, name.casefold())
                if pos >= 0:
                    candidate = (pos, -len(name), int(member.id))
                    if best is None or candidate < best:
                        best = candidate
    return best[2] if best else None


def _word_index(folded_text: str, folded_name: str) -> int:
    start = 0
    while True:
        pos = folded_text.find(folded_name, start)
        if pos < 0:
            return -1
        before = folded_text[pos - 1] if pos > 0 else " "
        after_i = pos + len(folded_name)
        after = folded_text[after_i] if after_i < len(folded_text) else " "
        if not (before.isalnum() or before == "_") and not (after.isalnum() or after == "_"):
            return pos
        start = pos + 1


class StatusInfo:
    """What one Match Status message tells us."""

    def __init__(self, innings: int, batter_id: Optional[int], score: Optional[int]):
        self.innings = innings
        self.batter_id = batter_id
        self.score = score


def _parse_segment(segment: str, guild) -> Optional[tuple[int, Optional[int]]]:
    """Find (batter_id, individual_score) inside one innings section."""
    lines = [ln for ln in segment.split("\n") if ln.strip()]
    ordered = [i for i, ln in enumerate(lines) if BATTER_LABEL_RE.search(ln)]
    ordered += [i for i, ln in enumerate(lines)
                if i not in ordered and MENTION_RE.search(ln)]
    for i in ordered:
        line = lines[i]
        uid: Optional[int] = None
        mention = MENTION_RE.search(line)
        if mention:
            uid = int(mention.group(1))
        else:
            label = BATTER_LABEL_RE.search(line)
            if label:
                rest = line[label.end():].lstrip(" :-–—")
                rest = _NAME_CUT_RE.split(rest, maxsplit=1)[0]
                uid = resolve_name(rest, guild)
        if uid is None:
            continue
        window = line + "\n" + (lines[i + 1] if i + 1 < len(lines) else "")
        window = MENTION_RE.sub(" ", window)
        return uid, find_score(window)
    return None


def parse_status(text: str, guild) -> Optional[StatusInfo]:
    """Parse a Match Status message into (current innings, batter, score)."""
    clean = strip_markdown(text)
    labels = [(m.start(), m.end(), _INNINGS_NO[(m.group(1) or m.group(2)).lower()])
              for m in INNINGS_RE.finditer(clean)]
    if not labels:
        return None
    segments: dict[int, str] = {}
    for i, (_, end, number) in enumerate(labels):
        stop = labels[i + 1][0] if i + 1 < len(labels) else len(clean)
        segments[number] = segments.get(number, "") + "\n" + clean[end:stop]
    found: dict[int, tuple[int, Optional[int]]] = {}
    for number, segment in segments.items():
        parsed = _parse_segment(segment, guild)
        if parsed:
            found[number] = parsed
    if found:
        innings = max(found)
        uid, score = found[innings]
        return StatusInfo(innings, uid, score)
    # Batter info sits outside the innings sections: use the text before them.
    parsed = _parse_segment(clean[:labels[0][0]], guild)
    if parsed:
        with_digits = [n for n, seg in segments.items() if re.search(r"\d", seg)]
        innings = max(with_digits) if with_digits else min(segments)
        return StatusInfo(innings, parsed[0], parsed[1])
    return None


def parse_out(text: str, guild, current_batter_id: Optional[int]) -> Optional[tuple[int, int]]:
    """Parse a 'Batter out!' embed into (batter_id, authoritative_score)."""
    clean = strip_markdown(text)
    header = re.search(r"batter\s+out!?", clean, re.IGNORECASE)
    body = clean[header.end():] if header else clean
    uid = find_member_in_text(clean, guild, prefer_id=current_batter_id)
    if uid is None:
        return None
    score = find_score(MENTION_RE.sub(" ", body))
    if score is None:
        score = find_score(MENTION_RE.sub(" ", clean))
    if score is None:
        return None
    return uid, score


def parse_chance_hint(text: str) -> Optional[int]:
    """Batter id from the opportunity message itself (only a fallback hint)."""
    match = re.search(r"takes\s+this\s+catch,\s*<@!?(\d+)>", text, re.IGNORECASE)
    return int(match.group(1)) if match else None


# =============================================================================
# Small helpers
# =============================================================================
class CommitStale(Exception):
    """The preview changed (or emptied) between showing it and confirming."""


def owner_only():
    async def predicate(ctx: commands.Context) -> bool:
        if int(ctx.author.id) == BOT_OWNER_ID:
            return True
        with contextlib.suppress(discord.HTTPException):
            await ctx.reply("❌ Only the bot owner can use this command.",
                            mention_author=False, allowed_mentions=NO_MENTIONS)
        return False
    return commands.check(predicate)


def impact_of(score: int, first_drop: Optional[int]) -> tuple[int, int]:
    """(runs_after_drops, runs_without_drops) for one batter innings."""
    if first_drop is None:
        return 0, score
    baseline = min(first_drop, score)
    return score - baseline, baseline


def _ts(value: float) -> str:
    return f"<t:{int(value)}:f>"


class ConfirmView(discord.ui.View):
    """Confirm / Cancel buttons usable only by the person who ran the command."""

    def __init__(self, owner_id: int, action: Callable[[], tuple[str, Optional[discord.Embed]]],
                 cancel_text: str = "Cancelled. Nothing was changed."):
        super().__init__(timeout=CONFIRM_TIMEOUT_SECONDS)
        self.owner_id = owner_id
        self.action = action
        self.cancel_text = cancel_text
        self.message: Optional[discord.Message] = None
        self.done = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Only the person who ran this command can use these buttons.", ephemeral=True)
            return False
        return True

    def _disable(self):
        for child in self.children:
            child.disabled = True

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.done:
            await interaction.response.defer()
            return
        self.done = True
        self._disable()
        self.stop()
        try:
            content, embed = self.action()
        except Exception as exc:  # the action itself rolls back its own transaction
            traceback.print_exc()
            content, embed = f"❌ Failed ({type(exc).__name__}). Nothing was changed.", None
        await interaction.response.edit_message(
            content=content, embed=embed if embed is not None else discord.utils.MISSING,
            view=self)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.done:
            await interaction.response.defer()
            return
        self.done = True
        self._disable()
        self.stop()
        await interaction.response.edit_message(content=self.cancel_text, view=self)

    async def on_timeout(self):
        if self.done:
            return
        self.done = True
        self._disable()
        if self.message is not None:
            with contextlib.suppress(discord.HTTPException):
                await self.message.edit(content="Timed out. Nothing was changed.", view=self)


class PagerView(discord.ui.View):
    """Prev / Next / Refresh for the preview and backup listings."""

    def __init__(self, owner_id: int, build: Callable[[int], tuple[discord.Embed, int]]):
        super().__init__(timeout=300)
        self.owner_id = owner_id
        self.build = build
        self.page = 0
        self.message: Optional[discord.Message] = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Only the person who ran this command can use these buttons.", ephemeral=True)
            return False
        return True

    def render(self) -> discord.Embed:
        embed, pages = self.build(self.page)
        self.page = max(0, min(self.page, pages - 1))
        self.previous.disabled = self.page == 0
        self.next.disabled = self.page >= pages - 1
        return embed

    async def _update(self, interaction: discord.Interaction):
        embed = self.render()
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="◀ Previous", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page -= 1
        await self._update(interaction)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page += 1
        await self._update(interaction)

    @discord.ui.button(label="🔄 Refresh", style=discord.ButtonStyle.primary)
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._update(interaction)

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message is not None:
            with contextlib.suppress(discord.HTTPException):
                await self.message.edit(view=self)


# =============================================================================
# The cog
# =============================================================================
class BatterImpactTracker(commands.Cog):
    """Batter catch-drop impact: tracking, preview, commit and backups."""

    def __init__(self, bot):
        self.bot = bot
        self.raw_log: dict[str, deque] = {
            k: deque(maxlen=5) for k in ("status", "out", "chance", "result", "ended", "failed")
        }
        self._reacted: deque[int] = deque(maxlen=500)
        self._reacted_set: set[int] = set()
        self._init_db()

    # ------------------------------------------------------------------ database
    def _init_db(self):
        db = self.bot.db
        statements = [
            """CREATE TABLE IF NOT EXISTS batfds_matches (
                match_id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                current_innings INTEGER NOT NULL DEFAULT 1,
                cur_batter_id INTEGER,
                cur_score INTEGER,
                started_at REAL NOT NULL,
                last_activity REAL NOT NULL,
                ended_at REAL,
                end_reason TEXT)""",
            "CREATE INDEX IF NOT EXISTS idx_batfds_matches_open ON batfds_matches(channel_id, ended_at)",
            """CREATE TABLE IF NOT EXISTS batfds_batters (
                record_id INTEGER PRIMARY KEY AUTOINCREMENT,
                match_id INTEGER NOT NULL,
                guild_id INTEGER NOT NULL,
                innings INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                latest_score INTEGER NOT NULL DEFAULT 0,
                first_drop_score INTEGER,
                drop_count INTEGER NOT NULL DEFAULT 0,
                final_score INTEGER,
                finalized INTEGER NOT NULL DEFAULT 0,
                committed INTEGER NOT NULL DEFAULT 0,
                UNIQUE (match_id, innings, user_id))""",
            "CREATE INDEX IF NOT EXISTS idx_batfds_batters_guild ON batfds_batters(guild_id, committed, finalized)",
            """CREATE TABLE IF NOT EXISTS batfds_pending (
                message_id INTEGER PRIMARY KEY,
                match_id INTEGER NOT NULL,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                innings INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                score_before INTEGER NOT NULL,
                created REAL NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS batfds_seen (
                message_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                created REAL NOT NULL,
                PRIMARY KEY (message_id, kind))""",
            """CREATE TABLE IF NOT EXISTS batfds_counters (
                guild_id INTEGER PRIMARY KEY,
                next_backup INTEGER NOT NULL DEFAULT 1)""",
            """CREATE TABLE IF NOT EXISTS batfds_backups (
                guild_id INTEGER NOT NULL,
                number INTEGER NOT NULL,
                created_at REAL NOT NULL,
                created_by INTEGER NOT NULL,
                players INTEGER NOT NULL,
                total_after INTEGER NOT NULL,
                total_without INTEGER NOT NULL,
                total_drops INTEGER NOT NULL,
                PRIMARY KEY (guild_id, number))""",
            """CREATE TABLE IF NOT EXISTS batfds_backup_rows (
                guild_id INTEGER NOT NULL,
                number INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                runs_after INTEGER NOT NULL,
                runs_without INTEGER NOT NULL,
                drops INTEGER NOT NULL,
                PRIMARY KEY (guild_id, number, user_id))""",
            # This is the table lb.py already reads for the permanent leaderboard.
            """CREATE TABLE IF NOT EXISTS batter_drop_lb (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                runs_affected INTEGER NOT NULL DEFAULT 0,
                runs_without_drops INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (guild_id, user_id))""",
        ]
        for sql in statements:
            db.execute(sql)
        existing = {row[1] for row in db.execute("PRAGMA table_info(batter_drop_lb)").fetchall()}
        for column in ("runs_affected", "runs_without_drops"):
            if column not in existing:
                db.execute(f"ALTER TABLE batter_drop_lb ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0")
        now = time.time()
        db.execute("DELETE FROM batfds_seen WHERE created < ?", (now - SEEN_RETENTION_SECONDS,))
        db.execute("DELETE FROM batfds_pending WHERE created < ?", (now - PENDING_TTL_SECONDS,))
        db.commit()

    @contextlib.contextmanager
    def _tx(self):
        """One atomic SQLite transaction (rolled back on any error)."""
        db = self.bot.db
        if db.in_transaction:
            db.commit()
        db.execute("BEGIN IMMEDIATE")
        try:
            yield db
        except BaseException:
            db.rollback()
            raise
        else:
            db.commit()

    def _fetch_all(self, sql: str, params: tuple = ()) -> list[dict]:
        cur = self.bot.db.execute(sql, params)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def _fetch_one(self, sql: str, params: tuple = ()) -> Optional[dict]:
        rows = self._fetch_all(sql, params)
        return rows[0] if rows else None

    def _tracking_enabled(self, channel_id: int) -> bool:
        try:
            row = self.bot.db.execute(
                "SELECT 1 FROM fielding_tracking_channels WHERE channel_id=? LIMIT 1",
                (int(channel_id),)).fetchone()
        except Exception:
            return False
        return row is not None

    def _is_seen(self, message_id: int, kind: str) -> bool:
        return self.bot.db.execute(
            "SELECT 1 FROM batfds_seen WHERE message_id=? AND kind=?",
            (int(message_id), kind)).fetchone() is not None

    @staticmethod
    def _mark_seen(db, message_id: int, kind: str):
        db.execute("INSERT OR IGNORE INTO batfds_seen (message_id, kind, created) VALUES (?, ?, ?)",
                   (int(message_id), kind, time.time()))

    def _active_match(self, channel_id: int) -> Optional[dict]:
        return self._fetch_one(
            "SELECT * FROM batfds_matches WHERE channel_id=? AND ended_at IS NULL "
            "ORDER BY match_id DESC LIMIT 1", (int(channel_id),))

    # ------------------------------------------------------------------ state changes
    @staticmethod
    def _finalize_open(db, match_id: int, below_innings: Optional[int] = None) -> int:
        """Finalise not-out batters using their latest saved score."""
        sql = ("UPDATE batfds_batters SET finalized=1, final_score=latest_score "
               "WHERE match_id=? AND finalized=0")
        params: tuple = (match_id,)
        if below_innings is not None:
            sql += " AND innings < ?"
            params = (match_id, below_innings)
        return db.execute(sql, params).rowcount

    @staticmethod
    def _close_match(db, match_id: int, reason: str, now: float):
        db.execute("UPDATE batfds_matches SET ended_at=?, end_reason=?, cur_batter_id=NULL, "
                   "cur_score=NULL WHERE match_id=?", (now, reason, match_id))
        db.execute("DELETE FROM batfds_pending WHERE match_id=?", (match_id,))

    @staticmethod
    def _new_match(db, guild_id: int, channel_id: int, innings: int, now: float) -> dict:
        cur = db.execute(
            "INSERT INTO batfds_matches (guild_id, channel_id, current_innings, started_at, "
            "last_activity) VALUES (?, ?, ?, ?, ?)", (guild_id, channel_id, innings, now, now))
        return {"match_id": cur.lastrowid, "guild_id": guild_id, "channel_id": channel_id,
                "current_innings": innings, "cur_batter_id": None, "cur_score": None,
                "last_activity": now}

    @staticmethod
    def _touch_batter(db, match_id: int, guild_id: int, innings: int, user_id: int,
                      score: Optional[int]) -> None:
        db.execute(
            "INSERT OR IGNORE INTO batfds_batters (match_id, guild_id, innings, user_id, "
            "latest_score) VALUES (?, ?, ?, ?, ?)",
            (match_id, guild_id, innings, user_id, score or 0))
        if score is not None:
            # Scores only go up; MAX() ignores stale or mis-parsed lower numbers.
            db.execute(
                "UPDATE batfds_batters SET latest_score=MAX(latest_score, ?) "
                "WHERE match_id=? AND innings=? AND user_id=? AND finalized=0",
                (score, match_id, innings, user_id))

    def _apply_status(self, guild_id: int, channel_id: int, info: StatusInfo, edited: bool) -> bool:
        now = time.time()
        with self._tx() as db:
            match = self._active_match(channel_id)
            if match is not None:
                stale = now - match["last_activity"] > STALE_MATCH_SECONDS
                regressed = info.innings < match["current_innings"]
                if regressed and edited:
                    return False  # an old status message being edited: ignore it
                if stale or regressed:
                    # A different match started in this channel without us seeing
                    # "Match has ended": close the old one with its latest scores.
                    self._finalize_open(db, match["match_id"])
                    self._close_match(db, match["match_id"],
                                      "stale" if stale else "new_match_detected", now)
                    match = None
            if match is None:
                match = self._new_match(db, guild_id, channel_id, info.innings, now)
            elif info.innings > match["current_innings"]:
                self._finalize_open(db, match["match_id"], below_innings=info.innings)
                db.execute("UPDATE batfds_matches SET current_innings=? WHERE match_id=?",
                           (info.innings, match["match_id"]))
            match_id = match["match_id"]
            cur_score = None
            if info.batter_id is not None:
                self._touch_batter(db, match_id, guild_id, info.innings, info.batter_id, info.score)
                row = db.execute(
                    "SELECT latest_score FROM batfds_batters WHERE match_id=? AND innings=? "
                    "AND user_id=?", (match_id, info.innings, info.batter_id)).fetchone()
                cur_score = int(row[0]) if row else info.score
            db.execute("UPDATE batfds_matches SET cur_batter_id=?, cur_score=?, last_activity=? "
                       "WHERE match_id=?", (info.batter_id, cur_score, now, match_id))
        return True

    def _apply_chance(self, message_id: int, guild_id: int, channel_id: int,
                      hint_uid: Optional[int]) -> bool:
        now = time.time()
        with self._tx() as db:
            db.execute("DELETE FROM batfds_pending WHERE created < ?", (now - PENDING_TTL_SECONDS,))
            match = self._active_match(channel_id)
            if match is None:
                return False
            innings = match["current_innings"]
            uid, score = match["cur_batter_id"], match["cur_score"]
            if uid is None or score is None:
                # No usable Match Status yet; only trust the message's own hint if we
                # already hold a score for that batter in this innings.
                row = db.execute(
                    "SELECT latest_score FROM batfds_batters WHERE match_id=? AND innings=? "
                    "AND user_id=? AND finalized=0",
                    (match["match_id"], innings, hint_uid or 0)).fetchone()
                if hint_uid is None or row is None:
                    return False
                uid, score = hint_uid, int(row[0])
            db.execute(
                "INSERT OR IGNORE INTO batfds_pending (message_id, match_id, guild_id, channel_id, "
                "innings, user_id, score_before, created) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (message_id, match["match_id"], guild_id, channel_id, innings, uid, score, now))
            self._mark_seen(db, message_id, "chance")
            db.execute("UPDATE batfds_matches SET last_activity=? WHERE match_id=?",
                       (now, match["match_id"]))
        return True

    def _apply_result(self, message_id: int, channel_id: int, kind: str,
                      reference_id: Optional[int]) -> Optional[dict]:
        now = time.time()
        with self._tx() as db:
            self._mark_seen(db, message_id, kind)
            db.execute("DELETE FROM batfds_pending WHERE created < ?", (now - PENDING_TTL_SECONDS,))
            pending = None
            if reference_id:
                pending = self._fetch_one(
                    "SELECT * FROM batfds_pending WHERE message_id=? AND channel_id=?",
                    (int(reference_id), int(channel_id)))
            if pending is None:
                # Newest unexpired opportunity; older ones are left alone until they expire.
                pending = self._fetch_one(
                    "SELECT * FROM batfds_pending WHERE channel_id=? "
                    "ORDER BY created DESC, message_id DESC LIMIT 1", (int(channel_id),))
            if pending is None:
                return None
            db.execute("DELETE FROM batfds_pending WHERE message_id=?", (pending["message_id"],))
            if kind != "dropped":
                return {"kind": kind, "user_id": pending["user_id"], "applied": False}
            record = self._fetch_one(
                "SELECT record_id, finalized, first_drop_score FROM batfds_batters "
                "WHERE match_id=? AND innings=? AND user_id=?",
                (pending["match_id"], pending["innings"], pending["user_id"]))
            if record is None:
                self._touch_batter(db, pending["match_id"], pending["guild_id"],
                                   pending["innings"], pending["user_id"], pending["score_before"])
            elif record["finalized"]:
                return {"kind": kind, "user_id": pending["user_id"], "applied": False}
            db.execute(
                "UPDATE batfds_batters SET first_drop_score=COALESCE(first_drop_score, ?), "
                "drop_count=drop_count+1 WHERE match_id=? AND innings=? AND user_id=? "
                "AND finalized=0",
                (pending["score_before"], pending["match_id"], pending["innings"],
                 pending["user_id"]))
            return {"kind": kind, "user_id": pending["user_id"], "applied": True,
                    "score_before": pending["score_before"]}

    def _apply_out(self, message_id: int, guild_id: int, channel_id: int,
                   uid: int, score: int) -> str:
        now = time.time()
        with self._tx() as db:
            self._mark_seen(db, message_id, "out")
            match = self._active_match(channel_id)
            if match is None:
                match = self._new_match(db, guild_id, channel_id, 1, now)
            match_id, innings = match["match_id"], match["current_innings"]
            self._touch_batter(db, match_id, guild_id, innings, uid, score)
            record = db.execute(
                "SELECT finalized, first_drop_score FROM batfds_batters "
                "WHERE match_id=? AND innings=? AND user_id=?",
                (match_id, innings, uid)).fetchone()
            if record[0]:
                return "duplicate"
            first_drop = record[1]
            final = max(score, first_drop) if first_drop is not None else score
            db.execute(
                "UPDATE batfds_batters SET finalized=1, final_score=?, latest_score=? "
                "WHERE match_id=? AND innings=? AND user_id=? AND finalized=0",
                (final, final, match_id, innings, uid))
            db.execute("DELETE FROM batfds_pending WHERE match_id=? AND user_id=?", (match_id, uid))
            db.execute(
                "UPDATE batfds_matches SET last_activity=?, "
                "cur_batter_id=CASE WHEN cur_batter_id=? THEN NULL ELSE cur_batter_id END, "
                "cur_score=CASE WHEN cur_batter_id=? THEN NULL ELSE cur_score END "
                "WHERE match_id=?", (now, uid, uid, match_id))
        return "finalized"

    def _apply_ended(self, message_id: int, channel_id: int) -> Optional[int]:
        now = time.time()
        with self._tx() as db:
            self._mark_seen(db, message_id, "ended")
            match = self._active_match(channel_id)
            if match is None:
                return None
            count = self._finalize_open(db, match["match_id"])
            self._close_match(db, match["match_id"], "match_end", now)
            return count

    def _manual_end(self, match_id: int) -> int:
        now = time.time()
        with self._tx() as db:
            row = db.execute("SELECT ended_at FROM batfds_matches WHERE match_id=?",
                             (match_id,)).fetchone()
            if row is None or row[0] is not None:
                return 0
            count = self._finalize_open(db, match_id)
            self._close_match(db, match_id, "manual", now)
            return count

    # ------------------------------------------------------------------ statistics
    def _aggregate(self, rows: list[dict]) -> dict[int, dict]:
        """Per-user totals from batter-innings rows (final score, or latest if live)."""
        result: dict[int, dict] = {}
        for row in rows:
            finalized = bool(row.get("finalized", 1))
            score = row["final_score"] if finalized and row["final_score"] is not None \
                else row["latest_score"]
            after, without = impact_of(int(score), row["first_drop_score"])
            entry = result.setdefault(int(row["user_id"]), {
                "after": 0, "without": 0, "drops": 0, "live": False})
            entry["after"] += after
            entry["without"] += without
            entry["drops"] += int(row["drop_count"])
            if not finalized:
                entry["live"] = True
        return result

    def _preview_rows(self, guild_id: int) -> dict[int, dict]:
        rows = self._fetch_all(
            "SELECT user_id, latest_score, first_drop_score, drop_count, final_score, finalized "
            "FROM batfds_batters WHERE guild_id=? AND committed=0", (guild_id,))
        return self._aggregate(rows)

    def _commit_rows(self, guild_id: int) -> list[dict]:
        return self._fetch_all(
            "SELECT record_id, user_id, latest_score, first_drop_score, drop_count, final_score, "
            "finalized FROM batfds_batters WHERE guild_id=? AND finalized=1 AND committed=0 "
            "ORDER BY record_id", (guild_id,))

    @staticmethod
    def _signature(rows: list[dict]) -> str:
        digest = hashlib.sha256()
        for r in rows:
            digest.update(
                f"{r['record_id']}:{r['user_id']}:{r['final_score']}:{r['first_drop_score']}:"
                f"{r['drop_count']};".encode())
        return digest.hexdigest()

    def _commit(self, guild_id: int, expected_signature: str, actor_id: int) -> tuple[int, dict]:
        """Atomically back up the preview, add it to the permanent +lb, then reset it."""
        now = time.time()
        with self._tx() as db:
            rows = self._commit_rows(guild_id)
            if not rows:
                raise CommitStale("There is nothing to commit any more.")
            if self._signature(rows) != expected_signature:
                raise CommitStale("The preview changed after it was shown.")
            totals = self._aggregate(rows)
            db.execute("INSERT OR IGNORE INTO batfds_counters (guild_id, next_backup) VALUES (?, 1)",
                       (guild_id,))
            number = int(db.execute("SELECT next_backup FROM batfds_counters WHERE guild_id=?",
                                    (guild_id,)).fetchone()[0])
            db.execute("UPDATE batfds_counters SET next_backup=? WHERE guild_id=?",
                       (number + 1, guild_id))
            db.execute(
                "INSERT INTO batfds_backups (guild_id, number, created_at, created_by, players, "
                "total_after, total_without, total_drops) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (guild_id, number, now, actor_id, len(totals),
                 sum(t["after"] for t in totals.values()),
                 sum(t["without"] for t in totals.values()),
                 sum(t["drops"] for t in totals.values())))
            for uid, t in totals.items():
                db.execute(
                    "INSERT INTO batfds_backup_rows (guild_id, number, user_id, runs_after, "
                    "runs_without, drops) VALUES (?, ?, ?, ?, ?, ?)",
                    (guild_id, number, uid, t["after"], t["without"], t["drops"]))
                cur = db.execute(
                    "UPDATE batter_drop_lb SET runs_affected=runs_affected+?, "
                    "runs_without_drops=runs_without_drops+? WHERE guild_id=? AND user_id=?",
                    (t["after"], t["without"], guild_id, uid))
                if cur.rowcount == 0:
                    db.execute(
                        "INSERT INTO batter_drop_lb (guild_id, user_id, runs_affected, "
                        "runs_without_drops) VALUES (?, ?, ?, ?)",
                        (guild_id, uid, t["after"], t["without"]))
            ids = [r["record_id"] for r in rows]
            marked = 0
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                marked += db.execute(
                    f"UPDATE batfds_batters SET committed=1 WHERE finalized=1 AND committed=0 "
                    f"AND record_id IN ({','.join('?' * len(chunk))})", chunk).rowcount
            if marked != len(ids):
                raise RuntimeError(f"Expected to mark {len(ids)} records, marked {marked}.")
        return number, totals

    # ------------------------------------------------------------------ display helpers
    @staticmethod
    def _player_name(guild, user_id: int) -> str:
        member = guild.get_member(user_id) if guild else None
        name = member.name if member else f"Unknown player ({str(user_id)[-4:]})"
        return discord.utils.escape_markdown(name)

    def _rows_embed(self, guild, title: str, data: dict[int, dict], page: int,
                    footer: str) -> tuple[discord.Embed, int]:
        ordered = sorted(data.items(),
                         key=lambda kv: (-kv[1]["after"], -kv[1]["without"], kv[0]))
        pages = max(1, (len(ordered) + PAGE_SIZE - 1) // PAGE_SIZE)
        page = max(0, min(page, pages - 1))
        chunk = ordered[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
        lines = []
        for rank, (uid, t) in enumerate(chunk, start=page * PAGE_SIZE + 1):
            live = " ⏳" if t.get("live") else ""
            lines.append(
                f"`{rank}.` **{self._player_name(guild, uid)}** → after drops `{t['after']}` • "
                f"without drops `{t['without']}` • drops `{t['drops']}`{live}")
        embed = discord.Embed(
            title=title,
            description="\n".join(lines) if lines else "Nothing recorded yet.",
            colour=BLUE)
        embed.set_footer(text=f"{footer} • Page {page + 1}/{pages} • {len(ordered)} players")
        return embed, pages

    # ------------------------------------------------------------------ listeners
    @commands.Cog.listener()
    async def on_message(self, message):
        await self._on_hc_message(message, edited=False)

    @commands.Cog.listener()
    async def on_message_edit(self, before, after):
        await self._on_hc_message(after, edited=True)

    async def _on_hc_message(self, message, edited: bool):
        if message.author.id != HANDCRICKET_BOT_ID or not message.guild:
            return
        if not self._tracking_enabled(message.channel.id):
            return
        try:
            await self._process(message, edited)
        except Exception:
            print("[batfds] Failed to process a Hand Cricket message:")
            traceback.print_exc()

    async def _process(self, message, edited: bool):
        text = message_text(message)
        kind = classify(text)
        if kind is None:
            return
        guild, channel_id = message.guild, message.channel.id
        # Edits/resends of messages we already handled must never be counted twice.
        if kind != "status" and self._is_seen(message.id, kind):
            return

        if kind == "status":
            self.raw_log["status"].append((channel_id, text))
            info = parse_status(text, guild)
            if info is None or info.batter_id is None:
                self.raw_log["failed"].append((channel_id, "STATUS NOT PARSED\n" + text))
                return
            if self._apply_status(guild.id, channel_id, info, edited):
                await self._react(message)

        elif kind == "chance":
            self.raw_log["chance"].append((channel_id, text))
            if self._apply_chance(message.id, guild.id, channel_id, parse_chance_hint(text)):
                await self._react(message)
            else:
                self.raw_log["failed"].append(
                    (channel_id, "CHANCE NOT MATCHED (no active match / batter)\n" + text))

        elif kind in ("dropped", "taken"):
            self.raw_log["result"].append((channel_id, text))
            reference = getattr(message, "reference", None)
            self._apply_result(message.id, channel_id, kind,
                               getattr(reference, "message_id", None))

        elif kind == "out":
            self.raw_log["out"].append((channel_id, text))
            match = self._active_match(channel_id)
            parsed = parse_out(text, guild, match["cur_batter_id"] if match else None)
            if parsed is None:
                self.raw_log["failed"].append((channel_id, "OUT NOT PARSED\n" + text))
                return
            self._apply_out(message.id, guild.id, channel_id, *parsed)

        elif kind == "ended":
            self.raw_log["ended"].append((channel_id, text))
            self._apply_ended(message.id, channel_id)

    async def _react(self, message):
        if message.id in self._reacted_set:
            return
        if len(self._reacted) == self._reacted.maxlen:
            self._reacted_set.discard(self._reacted[0])
        self._reacted.append(message.id)
        self._reacted_set.add(message.id)
        try:
            await message.add_reaction(CHECK_EMOJI)
        except discord.HTTPException:
            pass  # missing permission etc. - tracking continues regardless

    # ------------------------------------------------------------------ +previewlb
    @commands.group(name="previewlb", invoke_without_command=True)
    @commands.guild_only()
    @owner_only()
    async def previewlb(self, ctx: commands.Context, number: Optional[str] = None):
        """Owner: preview uncommitted batter impact stats (or a backup by number)."""
        if number is not None:
            if not number.isdigit():
                await ctx.reply("Usage: `+previewlb`, `+previewlb backups`, `+previewlb <n>`, "
                                "`+previewlb delete <n>`, `+previewlb markended [channel_id]`.",
                                mention_author=False)
                return
            await self._show_backup(ctx, int(number))
            return

        def build(page: int):
            return self._rows_embed(
                ctx.guild, "🏏 BATTER IMPACT PREVIEW",
                self._preview_rows(ctx.guild.id), page,
                "Not in +lb until +commitlb • ⏳ = still batting")
        view = PagerView(ctx.author.id, build)
        view.message = await ctx.reply(embed=view.render(), view=view, mention_author=False,
                                       allowed_mentions=NO_MENTIONS)

    async def _show_backup(self, ctx: commands.Context, number: int):
        meta = self._fetch_one(
            "SELECT * FROM batfds_backups WHERE guild_id=? AND number=?", (ctx.guild.id, number))
        if meta is None:
            await ctx.reply(f"❌ Backup `{number}` does not exist.", mention_author=False)
            return

        def build(page: int):
            rows = self._fetch_all(
                "SELECT user_id, runs_after, runs_without, drops FROM batfds_backup_rows "
                "WHERE guild_id=? AND number=?", (ctx.guild.id, number))
            data = {r["user_id"]: {"after": r["runs_after"], "without": r["runs_without"],
                                   "drops": r["drops"], "live": False} for r in rows}
            return self._rows_embed(
                ctx.guild, f"🗄️ BATTER IMPACT BACKUP #{number}", data, page,
                f"Saved {time.strftime('%Y-%m-%d %H:%M', time.gmtime(meta['created_at']))} UTC")
        view = PagerView(ctx.author.id, build)
        view.message = await ctx.reply(embed=view.render(), view=view, mention_author=False,
                                       allowed_mentions=NO_MENTIONS)

    @previewlb.command(name="backups")
    @commands.guild_only()
    @owner_only()
    async def previewlb_backups(self, ctx: commands.Context):
        """Owner: list all backups."""
        rows = self._fetch_all(
            "SELECT * FROM batfds_backups WHERE guild_id=? ORDER BY number", (ctx.guild.id,))
        if not rows:
            await ctx.reply("No backups yet. One is created every time you run `+commitlb`.",
                            mention_author=False)
            return
        lines = [f"`#{r['number']}` • {_ts(r['created_at'])} • `{r['players']}` players • "
                 f"after `{r['total_after']}` • without `{r['total_without']}` • "
                 f"drops `{r['total_drops']}`" for r in rows[-40:]]
        embed = discord.Embed(title="🗄️ BATTER IMPACT BACKUPS", description="\n".join(lines),
                              colour=BLUE)
        embed.set_footer(text="+previewlb <n> to view • +previewlb delete <n> to delete")
        await ctx.reply(embed=embed, mention_author=False, allowed_mentions=NO_MENTIONS)

    @previewlb.command(name="delete")
    @commands.guild_only()
    @owner_only()
    async def previewlb_delete(self, ctx: commands.Context, number: int):
        """Owner: delete backup <number>."""
        with self._tx() as db:
            removed = db.execute("DELETE FROM batfds_backups WHERE guild_id=? AND number=?",
                                 (ctx.guild.id, number)).rowcount
            db.execute("DELETE FROM batfds_backup_rows WHERE guild_id=? AND number=?",
                       (ctx.guild.id, number))
        if removed:
            await ctx.reply(f"🗑️ Backup `{number}` deleted.", mention_author=False)
        else:
            await ctx.reply(f"❌ Backup `{number}` does not exist.", mention_author=False)

    @previewlb.command(name="raw")
    @commands.guild_only()
    @owner_only()
    async def previewlb_raw(self, ctx: commands.Context):
        """Owner: show the last raw Hand Cricket messages seen (for debugging)."""
        chunks = []
        for kind, entries in self.raw_log.items():
            if not entries:
                continue
            channel_id, text = entries[-1]
            chunks.append(f"=== {kind.upper()} (latest of {len(entries)}) channel {channel_id} ===\n{text}")
        if not chunks:
            await ctx.reply("No Hand Cricket messages have been seen since the bot started.",
                            mention_author=False)
            return
        payload = "\n\n".join(chunks)
        await ctx.reply(file=discord.File(io.BytesIO(payload.encode("utf-8")), "batfds_raw.txt"),
                        mention_author=False)

    @previewlb.command(name="markended")
    @commands.guild_only()
    @owner_only()
    async def previewlb_markended(self, ctx: commands.Context, channel_id: Optional[int] = None):
        """Owner: finalise an unfinished tracked match (when 'Match has ended' was missed)."""
        matches = self._fetch_all(
            "SELECT * FROM batfds_matches WHERE guild_id=? AND ended_at IS NULL "
            "ORDER BY match_id", (ctx.guild.id,))
        if channel_id is not None:
            matches = [m for m in matches if m["channel_id"] == channel_id]
            if not matches:
                await ctx.reply(f"❌ No unfinished tracked match in channel `{channel_id}`.",
                                mention_author=False)
                return
        if not matches:
            await ctx.reply("There are no unfinished tracked matches.", mention_author=False)
            return
        if len(matches) > 1:
            listing = "\n".join(f"• <#{m['channel_id']}> (`{m['channel_id']}`) — innings "
                                f"{m['current_innings']}, last activity {_ts(m['last_activity'])}"
                                for m in matches)
            await ctx.reply("More than one match is unfinished. Run `+previewlb markended "
                            f"<channel_id>` with one of these:\n{listing}",
                            mention_author=False, allowed_mentions=NO_MENTIONS)
            return
        match = matches[0]
        open_rows = self._fetch_all(
            "SELECT user_id, innings, latest_score FROM batfds_batters "
            "WHERE match_id=? AND finalized=0 ORDER BY innings, user_id", (match["match_id"],))
        lines = [f"• **{self._player_name(ctx.guild, r['user_id'])}** (innings {r['innings']}) "
                 f"→ `{r['latest_score']}` runs" for r in open_rows] or ["• No pending batters."]
        embed = discord.Embed(
            title="⚠️ Mark match as ended?",
            description=f"Channel <#{match['channel_id']}> — these not-out batters will be "
                        "finalised with their latest saved scores:\n" + "\n".join(lines[:25]),
            colour=BLUE)
        embed.set_footer(text="No preview statistics are deleted or reset.")

        def action():
            count = self._manual_end(match["match_id"])
            return f"✅ Match marked as ended. {count} batter record(s) finalised.", None
        view = ConfirmView(ctx.author.id, action)
        view.message = await ctx.reply(embed=embed, view=view, mention_author=False,
                                       allowed_mentions=NO_MENTIONS)

    # ------------------------------------------------------------------ +commitlb
    @commands.command(name="commitlb")
    @commands.guild_only()
    @owner_only()
    async def commitlb(self, ctx: commands.Context):
        """Owner: move the finished preview statistics into the permanent +lb."""
        rows = self._commit_rows(ctx.guild.id)
        live = sum(1 for t in self._preview_rows(ctx.guild.id).values() if t["live"])
        if not rows:
            note = f" ({live} player(s) are still batting and are not included.)" if live else ""
            await ctx.reply("There is nothing finished to commit." + note, mention_author=False)
            return
        signature = self._signature(rows)
        totals = self._aggregate(rows)
        ordered = sorted(totals.items(), key=lambda kv: (-kv[1]["after"], -kv[1]["without"], kv[0]))
        lines = [f"`{i}.` **{self._player_name(ctx.guild, uid)}** → after `{t['after']}` • "
                 f"without `{t['without']}` • drops `{t['drops']}`"
                 for i, (uid, t) in enumerate(ordered[:COMMIT_PREVIEW_LINES], start=1)]
        if len(ordered) > COMMIT_PREVIEW_LINES:
            lines.append(f"…and {len(ordered) - COMMIT_PREVIEW_LINES} more (full list attached).")
        embed = discord.Embed(
            title="📥 COMMIT PREVIEW → PERMANENT +lb", description="\n".join(lines), colour=BLUE)
        embed.add_field(name="Will be added", inline=False, value=(
            f"`{len(totals)}` players • after drops `{sum(t['after'] for t in totals.values())}` • "
            f"without drops `{sum(t['without'] for t in totals.values())}` • "
            f"drops `{sum(t['drops'] for t in totals.values())}`"))
        footer = "A backup is saved first, then the preview is reset."
        if live:
            footer += f" {live} player(s) still batting are NOT included."
        embed.set_footer(text=footer)

        def action():
            try:
                number, committed = self._commit(ctx.guild.id, signature, ctx.author.id)
            except CommitStale as exc:
                return f"⚠️ {exc} Nothing was changed — run `+commitlb` again.", None
            except Exception as exc:
                traceback.print_exc()
                return (f"❌ Commit failed and was rolled back ({type(exc).__name__}). "
                        "Nothing was changed."), None
            return (f"✅ Committed {len(committed)} player(s) to the permanent leaderboard. "
                    f"Backup saved as `+previewlb {number}`. The preview was reset."), None

        view = ConfirmView(ctx.author.id, action, cancel_text="Cancelled. Nothing was committed.")
        kwargs: dict[str, Any] = {}
        if len(ordered) > COMMIT_PREVIEW_LINES:
            full = "\n".join(
                f"{self._player_name(ctx.guild, uid)}\tafter={t['after']}\twithout={t['without']}"
                f"\tdrops={t['drops']}" for uid, t in ordered)
            kwargs["file"] = discord.File(io.BytesIO(full.encode("utf-8")), "commit_preview.txt")
        view.message = await ctx.reply(embed=embed, view=view, mention_author=False,
                                       allowed_mentions=NO_MENTIONS, **kwargs)

    # ------------------------------------------------------------------ permanent stats
    @commands.command(name="setbatterimpact")
    @commands.guild_only()
    @owner_only()
    async def setbatterimpact(self, ctx: commands.Context, member: discord.Member,
                              runs_after_drops: int, runs_without_drops: int):
        """Owner: set a player's permanent batter impact stats to exact values."""
        for value in (runs_after_drops, runs_without_drops):
            if value < 0 or value > MAX_MANUAL_VALUE:
                await ctx.reply(f"❌ Values must be between 0 and {MAX_MANUAL_VALUE:,}.",
                                mention_author=False)
                return
        with self._tx() as db:
            cur = db.execute(
                "UPDATE batter_drop_lb SET runs_affected=?, runs_without_drops=? "
                "WHERE guild_id=? AND user_id=?",
                (runs_after_drops, runs_without_drops, ctx.guild.id, member.id))
            if cur.rowcount == 0:
                db.execute(
                    "INSERT INTO batter_drop_lb (guild_id, user_id, runs_affected, "
                    "runs_without_drops) VALUES (?, ?, ?, ?)",
                    (ctx.guild.id, member.id, runs_after_drops, runs_without_drops))
        name = discord.utils.escape_markdown(member.name)
        await ctx.reply(
            f"✅ **{name}** is now set to `{runs_after_drops}` runs after catch drops and "
            f"`{runs_without_drops}` runs without catch drops.",
            mention_author=False, allowed_mentions=NO_MENTIONS)

    @commands.command(name="resetbatterimpact")
    @commands.guild_only()
    @owner_only()
    async def resetbatterimpact(self, ctx: commands.Context, member: Optional[discord.Member] = None):
        """Owner: reset one player's (or everyone's) permanent stats and preview tracking."""
        guild_id = ctx.guild.id
        if member is not None:
            with self._tx() as db:
                db.execute("DELETE FROM batter_drop_lb WHERE guild_id=? AND user_id=?",
                           (guild_id, member.id))
                db.execute("DELETE FROM batfds_batters WHERE guild_id=? AND user_id=? AND committed=0",
                           (guild_id, member.id))
                db.execute("DELETE FROM batfds_pending WHERE guild_id=? AND user_id=?",
                           (guild_id, member.id))
            await ctx.reply(
                f"✅ Batter impact (permanent and preview) reset for "
                f"**{discord.utils.escape_markdown(member.name)}**.",
                mention_author=False, allowed_mentions=NO_MENTIONS)
            return

        def action():
            with self._tx() as db:
                db.execute("DELETE FROM batter_drop_lb WHERE guild_id=?", (guild_id,))
                db.execute("DELETE FROM batfds_batters WHERE guild_id=? AND committed=0", (guild_id,))
                db.execute("DELETE FROM batfds_pending WHERE guild_id=?", (guild_id,))
            return "✅ All batter impact statistics (permanent and preview) were reset.", None
        view = ConfirmView(ctx.author.id, action)
        view.message = await ctx.reply(
            "⚠️ This resets **all** permanent batter impact stats and the whole preview for this "
            "server. Backups are kept. Continue?", view=view, mention_author=False)


async def setup(bot):
    await bot.add_cog(BatterImpactTracker(bot))
