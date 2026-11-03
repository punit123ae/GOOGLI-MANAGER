"""
team_emojis.py  -  GOOGLI MANAGER

Turns the logo of every APPROVED team in ``team_registration.db`` into a custom
emoji on the server and remembers which emoji belongs to which team.

Commands (bot-owner only)
    +syncemojis   Create missing team emojis and show a final report.
    +emojischema  Diagnostic: show what the cog detected in your database
                  (table, columns, approval format, logo format). Read-only.

Design notes
    * Nothing about your existing tables is assumed. The team table, the name /
      approval / short-form / logo columns and the logo format (BLOB, URL, local
      path, data URI, base64) are detected at runtime from the real database.
    * Existing tables are only ever READ. The only thing this cog writes is its
      own new table, ``team_emoji_records``.
    * Optional dependency: Pillow. With it, logos are validated and resized to
      fit Discord's emoji rules automatically. Without it, only images that
      already satisfy Discord's limits (PNG/JPEG/GIF/WEBP, <= 256 KB) work.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import io
import logging
import os
import re
import sqlite3
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import aiohttp
import discord
from discord.ext import commands

try:  # optional
    from PIL import Image

    PIL_AVAILABLE = True
except Exception:  # pragma: no cover - depends on the host environment
    Image = None  # type: ignore[assignment]
    PIL_AVAILABLE = False

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

DB_FILENAME = "team_registration.db"
DB_ENV_VAR = "TEAM_REGISTRATION_DB"  # optional override
EMOJI_TABLE = "team_emoji_records"  # the only table this cog creates / writes

MAX_EMOJI_BYTES = 256 * 1024  # Discord emoji upload limit
MAX_DOWNLOAD_BYTES = 10 * 1024 * 1024  # refuse absurdly large logo files
EMOJI_PIXELS = 128
UPLOAD_DELAY = 1.0  # seconds between uploads (Discord rate-limits emoji creation)
PROGRESS_INTERVAL = 2.0  # seconds between progress message edits
DISCORD_MAX_EMOJIS_CODE = 30008

# Column-name candidates, in priority order (case-insensitive).
NAME_COLS = ("team_name", "teamname", "name", "team", "title")
APPROVAL_COLS = (
    "approved",
    "is_approved",
    "approval_status",
    "registration_status",
    "status",
    "verified",
    "is_verified",
    "accepted",
)
SHORT_COLS = (
    "short_name",
    "shortname",
    "short_form",
    "shortform",
    "short_code",
    "abbreviation",
    "abbr",
    "team_tag",
    "acronym",
    "initials",
    "short",
)
LOGO_COLS = (
    "logo_url",
    "logo",
    "team_logo",
    "logo_path",
    "logo_file",
    "logo_link",
    "logo_image",
    "logo_blob",
    "logo_data",
    "image_url",
    "image",
    "icon_url",
    "icon",
    "emblem",
    "crest",
    "badge",
    "avatar",
)
LOGO_FUZZY = ("logo", "image", "icon", "emblem", "crest", "badge")
KEY_COLS = ("team_id", "id", "teamid")
APPROVED_TOKENS = {"approved", "accepted", "confirmed", "verified", "yes", "y", "true", "t", "1"}
FILLER_WORDS = {"of", "the", "and", "for", "de", "da", "&"}
LOGO_SUBDIRS = ("", "logos", "logo", "team_logos", "assets", "uploads", "images", "data", "static")
SKIP_DIRS = {".git", "venv", ".venv", "env", "node_modules", "__pycache__", "site-packages"}


# --------------------------------------------------------------------------- #
# Errors / data classes
# --------------------------------------------------------------------------- #


class SchemaError(Exception):
    """The database layout could not be understood safely."""


class LogoMissing(Exception):
    """The team has no usable logo (empty value / file not found)."""


class LogoError(Exception):
    """A logo exists but could not be fetched or used."""


@dataclass
class Schema:
    db_path: Path
    table: str
    columns: list[str]
    name_col: str
    key_col: Optional[str]
    approval_col: Optional[str]
    approval_implied: bool  # table itself only holds approved teams (e.g. "approved_teams")
    short_col: Optional[str]
    logo_cols: list[str]
    # logo stored in a different table: (column in team table, table, column in logo table)
    logo_link: Optional[tuple[str, str, str]] = None
    logo_link_cols: list[str] = field(default_factory=list)
    guild_col: Optional[str] = None


@dataclass
class Team:
    key: str
    name: str
    short: str
    logos: list[Any]


@dataclass
class TeamData:
    total_rows: int
    approved: list[Team]
    status_values: list[str]
    other_guild: list[str] = field(default_factory=list)  # approved, but registered in another server
    duplicates: list[str] = field(default_factory=list)  # approved, but same team name already counted


@dataclass
class RunState:
    taken_names: set[str]
    static_used: int
    animated_used: int
    limit: int


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #


def q(identifier: str) -> str:
    """Quote an SQLite identifier."""
    return '"' + identifier.replace('"', '""') + '"'


def _pick(colmap: dict[str, str], priority: tuple[str, ...]) -> Optional[str]:
    for candidate in priority:
        if candidate in colmap:
            return colmap[candidate]
    return None


def _empty(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, (bytes, bytearray, memoryview)):
        return len(value) == 0
    if isinstance(value, str):
        return value.strip().lower() in {"", "none", "null", "n/a", "na", "-", "nan"}
    return False


def is_approved_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value == 1
    if isinstance(value, str):
        return value.strip().lower() in APPROVED_TOKENS
    return False


def ascii_words(name: str) -> list[str]:
    folded = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    tokens = re.findall(r"[A-Za-z0-9]+", folded)
    words = [t for t in tokens if t.lower() not in FILLER_WORDS]
    return words or tokens


def generate_short(name: str) -> str:
    """Kashmir Titans -> KT, Chennai Super Kings -> CSK, single word -> first 3 letters."""
    words = ascii_words(name)
    if not words:
        return "TEAM"
    if len(words) == 1:
        short = words[0][:3].upper()
    else:
        short = "".join(w[0] for w in words)[:6].upper()
    if len(short) < 2:
        short = (words[0] + "X")[:2].upper()
    return short


def clean_existing_short(value: Any) -> Optional[str]:
    """Use a short form already stored in the DB if it is sane."""
    if _empty(value) or not isinstance(value, str):
        return None
    cleaned = re.sub(r"[^A-Za-z0-9_]", "", value.strip())
    if 2 <= len(cleaned) <= 10:
        return cleaned
    return None


def unique_emoji_name(base: str, name: str, taken: set[str]) -> str:
    """Return an emoji name not in ``taken`` (case-insensitive) and valid for Discord."""
    base = re.sub(r"[^A-Za-z0-9_]", "", base)[:32] or "team"
    if len(base) < 2:
        base = (base + "_")[:2]

    candidates = [base]
    words = ascii_words(name)
    if len(words) > 1:  # e.g. Mumbai Indians -> MaI when MI is already taken
        alt = words[0][:2].capitalize() + "".join(w[0] for w in words[1:])
        candidates.append(alt[:32])
    for candidate in candidates:
        if candidate.lower() not in taken:
            return candidate
    n = 2
    while True:
        suffix = str(n)
        candidate = base[: 32 - len(suffix)] + suffix
        if candidate.lower() not in taken:
            return candidate
        n += 1


def clip_lines(lines: list[str], limit: int = 1000) -> str:
    """Join lines for an embed field without exceeding Discord's 1024-char limit."""
    if not lines:
        return "—"
    out: list[str] = []
    used = 0
    for i, line in enumerate(lines):
        line = line if len(line) <= 200 else line[:197] + "..."
        extra = len(line) + 1
        if used + extra > limit - 30:
            out.append(f"…and {len(lines) - i} more")
            break
        out.append(line)
        used += extra
    return "\n".join(out)


def classify_logo(value: Any) -> str:
    if _empty(value):
        return "empty"
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "embedded image (BLOB)"
    if isinstance(value, str):
        s = value.strip()
        low = s.lower()
        if low.startswith("data:"):
            return "data URI"
        if low.startswith(("http://", "https://")):
            return "URL"
        if re.fullmatch(r"[A-Za-z0-9+/=\s]{200,}", s):
            return "base64 text"
        return "local file path / file name"
    return f"unsupported ({type(value).__name__})"


# --------------------------------------------------------------------------- #
# Database layer (synchronous; always called through asyncio.to_thread)
# --------------------------------------------------------------------------- #


def find_database(extra: list[Path]) -> Optional[Path]:
    env = os.environ.get(DB_ENV_VAR)
    candidates: list[Path] = [Path(env)] if env else []
    candidates.extend(extra)

    here = Path(__file__).resolve().parent
    cwd = Path.cwd()
    for base in (cwd, cwd / "data", cwd / "database", here, here.parent, here.parent / "data", here.parent.parent):
        candidates.append(base / DB_FILENAME)

    for c in candidates:
        try:
            if c.is_file():
                return c.resolve()
        except OSError:
            continue

    # Shallow search of the project tree as a last resort.
    for root in {cwd, here.parent}:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            depth = len(Path(dirpath).relative_to(root).parts)
            if depth >= 3:
                dirnames[:] = []
            if DB_FILENAME in filenames:
                return (Path(dirpath) / DB_FILENAME).resolve()
    return None


def _connect(db_path: Path, *, write: bool = False) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.text_factory = lambda b: b.decode("utf-8", "replace")
    conn.row_factory = sqlite3.Row
    if not write:
        conn.execute("PRAGMA query_only = ON")
    return conn


def _table_columns(conn: sqlite3.Connection, table: str) -> list[sqlite3.Row]:
    return conn.execute(f"PRAGMA table_info({q(table)})").fetchall()


def detect_schema(db_path: Path) -> Schema:
    conn = _connect(db_path)
    try:
        tables = [
            r["name"]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")
            if not r["name"].startswith("sqlite_") and r["name"] != EMOJI_TABLE
        ]
        if not tables:
            raise SchemaError("The database contains no tables.")

        info: dict[str, list[sqlite3.Row]] = {t: _table_columns(conn, t) for t in tables}
        best: Optional[tuple[int, str]] = None

        for table in tables:
            colmap = {r["name"].lower(): r["name"] for r in info[table]}
            if _pick(colmap, NAME_COLS) is None:
                continue
            tl = table.lower()
            score = 0
            if tl in {"teams", "team", "team_registrations", "registrations", "team_registration"}:
                score += 10
            if "team" in tl:
                score += 6
            if "regist" in tl:
                score += 2
            if _pick(colmap, APPROVAL_COLS):
                score += 4
            elif "approved" in tl:
                score += 4
            if _pick(colmap, SHORT_COLS):
                score += 1
            if any(k in colmap for k in LOGO_COLS) or any(f in c for c in colmap for f in LOGO_FUZZY):
                score += 3
            if any(bad in tl for bad in ("player", "member", "roster", "match", "fixture", "score")):
                score -= 8
            if score > 0 and (best is None or score > best[0]):
                best = (score, table)

        if best is None:
            raise SchemaError(
                "Could not find a team table. Tables seen: "
                + ", ".join(f"{t}({', '.join(r['name'] for r in info[t])})" for t in tables)
            )

        table = best[1]
        cols = [r["name"] for r in info[table]]
        colmap = {c.lower(): c for c in cols}
        pk_cols = [r["name"] for r in info[table] if r["pk"]]

        name_col = _pick(colmap, NAME_COLS)
        assert name_col
        key_col = pk_cols[0] if len(pk_cols) == 1 else _pick(colmap, KEY_COLS)
        approval_col = _pick(colmap, APPROVAL_COLS)
        implied = approval_col is None and "approved" in table.lower()
        short_col = _pick(colmap, SHORT_COLS)

        logo_cols = [colmap[c] for c in LOGO_COLS if c in colmap]
        for c in cols:  # fuzzy extras
            if c not in logo_cols and any(f in c.lower() for f in LOGO_FUZZY):
                logo_cols.append(c)

        schema = Schema(
            db_path=db_path,
            table=table,
            columns=cols,
            name_col=name_col,
            key_col=key_col,
            approval_col=approval_col,
            approval_implied=implied,
            short_col=short_col,
            logo_cols=logo_cols,
            guild_col=_pick(colmap, ("guild_id", "server_id", "guild")),
        )

        # Logo stored in a different table? Try to link it.
        if not logo_cols:
            # (column in team table, acceptable column names in the logo table)
            link_options: list[tuple[str, tuple[str, ...]]] = []
            if key_col:
                link_options.append((key_col, (key_col.lower(), "team_id", "teamid")))
            link_options.append((name_col, NAME_COLS))
            for other in tables:
                if other == table:
                    continue
                ocols = [r["name"] for r in info[other]]
                omap = {c.lower(): c for c in ocols}
                o_logo = [omap[c] for c in LOGO_COLS if c in omap]
                o_logo += [c for c in ocols if c not in o_logo and any(f in c.lower() for f in LOGO_FUZZY)]
                if not o_logo:
                    continue
                for mine, theirs in link_options:
                    remote_col = _pick(omap, theirs)
                    if remote_col and remote_col not in o_logo:
                        schema.logo_link = (mine, other, remote_col)
                        schema.logo_link_cols = o_logo
                        break
                if schema.logo_link:
                    break

        return schema
    finally:
        conn.close()


def load_teams(schema: Schema, guild_id: Optional[int] = None) -> TeamData:
    conn = _connect(schema.db_path)
    try:
        rows = conn.execute(f"SELECT * FROM {q(schema.table)}").fetchall()

        remote: dict[str, list[Any]] = {}
        if schema.logo_link:
            mine, other, theirs = schema.logo_link
            for r in conn.execute(f"SELECT * FROM {q(other)}"):
                k = r[theirs]
                if k is None:
                    continue
                remote.setdefault(str(k).strip().lower(), []).extend(
                    r[c] for c in schema.logo_link_cols if not _empty(r[c])
                )

        statuses: list[str] = []
        approved: list[Team] = []
        other_guild: list[str] = []
        duplicates: list[str] = []
        seen: set[str] = set()
        seen_names: set[str] = set()

        for r in rows:
            raw_name = r[schema.name_col]
            if _empty(raw_name):
                continue
            name = str(raw_name).strip()

            if schema.approval_col:
                val = r[schema.approval_col]
                label = repr(val) if not isinstance(val, str) else val
                if label not in statuses:
                    statuses.append(label)
                if not is_approved_value(val):
                    continue
            elif not schema.approval_implied:
                continue  # unreachable: run_sync refuses this case earlier

            if schema.guild_col and guild_id is not None and str(r[schema.guild_col]) != str(guild_id):
                other_guild.append(f"{name} (guild {r[schema.guild_col]})")
                continue

            key = str(r[schema.key_col]) if schema.key_col and r[schema.key_col] is not None else name.lower()
            norm = re.sub(r"\s+", " ", name).strip().lower()
            if key in seen or norm in seen_names:
                duplicates.append(f"{name} (id {key})")
                continue
            seen.add(key)
            seen_names.add(norm)

            short = None
            if schema.short_col:
                short = clean_existing_short(r[schema.short_col])
            short = short or generate_short(name)

            logos = [r[c] for c in schema.logo_cols if not _empty(r[c])]
            if schema.logo_link:
                link_val = r[schema.logo_link[0]]
                if link_val is not None:
                    logos.extend(remote.get(str(link_val).strip().lower(), []))

            approved.append(Team(key=key, name=name, short=short, logos=logos))

        return TeamData(
            total_rows=len(rows),
            approved=approved,
            status_values=statuses,
            other_guild=other_guild,
            duplicates=duplicates,
        )
    finally:
        conn.close()


def init_records_table(db_path: Path) -> None:
    conn = _connect(db_path, write=True)
    try:
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {q(EMOJI_TABLE)} (
                guild_id   INTEGER NOT NULL,
                team_key   TEXT    NOT NULL,
                team_name  TEXT    NOT NULL,
                emoji_id   INTEGER NOT NULL,
                emoji_name TEXT    NOT NULL,
                animated   INTEGER NOT NULL DEFAULT 0,
                created_at TEXT    NOT NULL,
                PRIMARY KEY (guild_id, team_key)
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def load_records(db_path: Path, guild_id: int) -> dict[str, dict[str, Any]]:
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            f"SELECT team_key, team_name, emoji_id, emoji_name FROM {q(EMOJI_TABLE)} WHERE guild_id = ?",
            (guild_id,),
        ).fetchall()
        return {r["team_key"]: dict(r) for r in rows}
    finally:
        conn.close()


def save_record(
    db_path: Path, guild_id: int, team_key: str, team_name: str, emoji_id: int, emoji_name: str, animated: bool
) -> None:
    conn = _connect(db_path, write=True)
    try:
        conn.execute(
            f"""
            INSERT INTO {q(EMOJI_TABLE)}
                (guild_id, team_key, team_name, emoji_id, emoji_name, animated, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, team_key) DO UPDATE SET
                team_name  = excluded.team_name,
                emoji_id   = excluded.emoji_id,
                emoji_name = excluded.emoji_name,
                animated   = excluded.animated,
                created_at = excluded.created_at
            """,
            (
                guild_id,
                team_key,
                team_name,
                emoji_id,
                emoji_name,
                int(animated),
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
            ),
        )
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Logo loading + image preparation
# --------------------------------------------------------------------------- #


def read_local_logo(raw: str, base_dirs: list[Path]) -> Optional[bytes]:
    s = raw.strip().strip("\"'")
    if s.lower().startswith("file://"):
        s = s[7:]
    p = Path(s).expanduser()
    candidates: list[Path] = [p] if p.is_absolute() else []
    for base in base_dirs:
        for sub in LOGO_SUBDIRS:
            candidates.append(base / sub / p)
            if p.name != s:
                candidates.append(base / sub / p.name)
    for c in candidates:
        try:
            if c.is_file():
                if c.stat().st_size > MAX_DOWNLOAD_BYTES:
                    raise LogoError(f"logo file too large ({c.stat().st_size // 1024} KB)")
                return c.read_bytes()
        except OSError:
            continue
    return None


def decode_data_uri(uri: str) -> bytes:
    try:
        header, payload = uri.split(",", 1)
        if ";base64" in header.lower():
            return base64.b64decode(payload, validate=False)
        from urllib.parse import unquote_to_bytes

        return unquote_to_bytes(payload)
    except (ValueError, binascii.Error) as exc:
        raise LogoError("invalid data URI") from exc


def sniff_image(data: bytes) -> Optional[str]:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def prepare_image(data: bytes) -> tuple[bytes, bool]:
    """Validate a logo and make it Discord-emoji-ready. Returns (bytes, animated)."""
    if not data:
        raise ValueError("empty image data")
    kind = sniff_image(data)
    if kind is None:
        raise ValueError("not a supported image (needs PNG, JPEG, GIF or WEBP)")

    if not PIL_AVAILABLE:
        if len(data) > MAX_EMOJI_BYTES:
            raise ValueError(f"image is {len(data) // 1024} KB (limit 256 KB); install Pillow to auto-resize")
        animated = kind == "gif" and data.count(b"\x00\x21\xf9\x04") > 1
        return data, animated

    try:
        with Image.open(io.BytesIO(data)) as probe:
            probe.verify()  # detects truncated / corrupt files
        with Image.open(io.BytesIO(data)) as im:
            animated = bool(getattr(im, "is_animated", False))
            if animated and kind == "gif" and len(data) <= MAX_EMOJI_BYTES:
                return data, True  # keep the animation untouched
            im.seek(0)
            frame = im.convert("RGBA")
    except Exception as exc:
        raise ValueError(f"invalid or corrupt image ({type(exc).__name__})") from exc

    resample = getattr(getattr(Image, "Resampling", Image), "LANCZOS")
    for size in (EMOJI_PIXELS, 96, 64, 32):
        work = frame.copy()
        work.thumbnail((size, size), resample)
        buf = io.BytesIO()
        work.save(buf, format="PNG", optimize=True)
        if buf.tell() <= MAX_EMOJI_BYTES:
            return buf.getvalue(), False
    raise ValueError("could not shrink image below 256 KB")


# --------------------------------------------------------------------------- #
# Cog
# --------------------------------------------------------------------------- #


async def _bot_owner_only(ctx: commands.Context) -> bool:
    """Owner gate. Uses discord.py's own owner system (bot.owner_id / bot.owner_ids,
    or the application owner/team). Replace the body if your bot uses a custom list."""
    if await ctx.bot.is_owner(ctx.author):
        return True
    raise commands.NotOwner("Only the bot owner can use this command.")


class TeamEmojis(commands.Cog):
    """Create custom server emojis from approved team logos."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._lock = asyncio.Lock()
        self._session: Optional[aiohttp.ClientSession] = None

    async def cog_unload(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    # ---- infrastructure --------------------------------------------------- #

    def _http(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"User-Agent": "GOOGLI-MANAGER team_emojis cog"},
                timeout=aiohttp.ClientTimeout(total=25),
            )
        return self._session

    def _bot_attr_paths(self) -> list[Path]:
        found: list[Path] = []
        for attr in ("db_path", "team_db_path", "database_path", "DB_PATH"):
            v = getattr(self.bot, attr, None)
            if isinstance(v, (str, os.PathLike)):
                found.append(Path(v))
        return found

    async def _locate(self) -> tuple[Path, Schema]:
        db_path = await asyncio.to_thread(find_database, self._bot_attr_paths())
        if db_path is None:
            raise SchemaError(
                f"Could not find `{DB_FILENAME}`. Put it in the bot's working directory or set the "
                f"`{DB_ENV_VAR}` environment variable to its full path."
            )
        schema = await asyncio.to_thread(detect_schema, db_path)
        return db_path, schema

    def _base_dirs(self, schema: Schema) -> list[Path]:
        here = Path(__file__).resolve().parent
        dirs = [schema.db_path.parent, schema.db_path.parent.parent, Path.cwd(), here.parent, here]
        unique: list[Path] = []
        for d in dirs:
            if d not in unique:
                unique.append(d)
        return unique

    # ---- logo retrieval ---------------------------------------------------- #

    async def _download(self, url: str) -> bytes:
        try:
            async with self._http().get(url) as resp:
                if resp.status != 200:
                    hint = " (link may have expired or been deleted)" if resp.status in (403, 404, 410) else ""
                    raise LogoError(f"logo URL returned HTTP {resp.status}{hint}")
                if resp.content_length and resp.content_length > MAX_DOWNLOAD_BYTES:
                    raise LogoError("logo download is too large")
                data = await resp.content.read(MAX_DOWNLOAD_BYTES + 1)
                if len(data) > MAX_DOWNLOAD_BYTES:
                    raise LogoError("logo download is too large")
                return data
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise LogoError(f"could not download logo ({type(exc).__name__})") from exc

    async def _resolve_one(self, raw: Any, schema: Schema) -> bytes:
        if _empty(raw):
            raise LogoMissing("logo field is empty")
        if isinstance(raw, (bytes, bytearray, memoryview)):
            return bytes(raw)
        if not isinstance(raw, str):
            raise LogoMissing(f"unsupported logo value type: {type(raw).__name__}")

        s = raw.strip()
        low = s.lower()
        if low.startswith("data:"):
            return decode_data_uri(s)
        if low.startswith(("http://", "https://")):
            return await self._download(s)

        data = await asyncio.to_thread(read_local_logo, s, self._base_dirs(schema))
        if data is not None:
            return data
        if re.fullmatch(r"[A-Za-z0-9+/=\s]{200,}", s):
            try:
                return base64.b64decode(s)
            except (binascii.Error, ValueError) as exc:
                raise LogoError("logo looked like base64 but could not be decoded") from exc
        raise LogoMissing(f"logo file not found: `{s[:80]}`")

    async def _get_logo(self, schema: Schema, values: list[Any]) -> bytes:
        if not values:
            raise LogoMissing("no logo set")
        last_error: Optional[Exception] = None
        missing: Optional[LogoMissing] = None
        for raw in values:
            try:
                return await self._resolve_one(raw, schema)
            except LogoMissing as exc:
                missing = exc
            except LogoError as exc:
                last_error = exc
        if last_error:
            raise last_error
        raise missing or LogoMissing("no logo set")

    # ---- per-team processing ---------------------------------------------- #

    async def _process_team(
        self,
        guild: discord.Guild,
        schema: Schema,
        team: Team,
        records: dict[str, dict[str, Any]],
        live: dict[int, discord.Emoji],
        state: RunState,
    ) -> tuple[str, Any]:
        """Returns (status, detail). status: created | skipped | missing | failed."""
        rec = records.get(team.key)
        if rec and int(rec["emoji_id"]) in live:
            return "skipped", f"{team.name} → :{live[int(rec['emoji_id'])].name}:"

        try:
            data = await self._get_logo(schema, team.logos)
        except LogoMissing as exc:
            return "missing", f"{team.name} — {exc}"
        except LogoError as exc:
            return "failed", f"{team.name} — {exc}"
        except Exception:
            log.exception("Unexpected error fetching logo for %s", team.name)
            return "failed", f"{team.name} — unexpected error reading logo"

        try:
            image, animated = await asyncio.to_thread(prepare_image, data)
        except ValueError as exc:
            return "failed", f"{team.name} — {exc}"
        except Exception:
            log.exception("Unexpected error preparing image for %s", team.name)
            return "failed", f"{team.name} — could not process image"

        used = state.animated_used if animated else state.static_used
        kind = "animated" if animated else "static"
        if used >= state.limit:
            return "failed", f"{team.name} — no free {kind} emoji slots ({used}/{state.limit})"

        name = unique_emoji_name(team.short, team.name, state.taken_names)
        try:
            emoji = await guild.create_custom_emoji(
                name=name, image=image, reason=f"Team emoji sync for {team.name}"
            )
        except discord.Forbidden:
            return "failed", f"{team.name} — bot lacks permission to create emojis"
        except discord.HTTPException as exc:
            if exc.code == DISCORD_MAX_EMOJIS_CODE:
                if animated:
                    state.animated_used = state.limit
                else:
                    state.static_used = state.limit
                return "failed", f"{team.name} — server emoji limit reached"
            return "failed", f"{team.name} — Discord rejected upload ({exc.status}): {(exc.text or '')[:120]}"
        except Exception:
            log.exception("Unexpected error uploading emoji for %s", team.name)
            return "failed", f"{team.name} — unexpected upload error"

        state.taken_names.add(name.lower())
        live[emoji.id] = emoji
        if animated:
            state.animated_used += 1
        else:
            state.static_used += 1

        try:
            await asyncio.to_thread(
                save_record, schema.db_path, guild.id, team.key, team.name, emoji.id, emoji.name, emoji.animated
            )
        except Exception:
            log.exception("Emoji %s uploaded but DB record failed for %s", emoji.id, team.name)
            return "failed", f"{team.name} — uploaded as {emoji} (id {emoji.id}) but saving the record failed"

        await asyncio.sleep(UPLOAD_DELAY)
        return "created", emoji

    # ---- commands ---------------------------------------------------------- #

    @commands.command(name="syncemojis")
    @commands.guild_only()
    @commands.check(_bot_owner_only)
    async def syncemojis(self, ctx: commands.Context) -> None:
        """Create custom emojis from approved team logos (bot owner only)."""
        if self._lock.locked():
            await ctx.send("⏳ An emoji sync is already running. Please wait for it to finish.")
            return
        async with self._lock:
            try:
                await self._run_sync(ctx)
            except SchemaError as exc:
                await ctx.send(f"❌ {exc}")
            except Exception:
                log.exception("syncemojis crashed")
                await ctx.send("❌ Something went wrong during the sync. Check the bot console for details.")

    @commands.command(name="emojischema")
    @commands.guild_only()
    @commands.check(_bot_owner_only)
    async def emojischema(self, ctx: commands.Context) -> None:
        """Show what the cog detected in the team database (bot owner only, read-only)."""
        try:
            _, schema = await self._locate()
            data = await asyncio.to_thread(load_teams, schema, ctx.guild.id)
        except SchemaError as exc:
            await ctx.send(f"❌ {exc}")
            return
        except Exception:
            log.exception("emojischema failed")
            await ctx.send("❌ Could not inspect the database. Check the bot console for details.")
            return

        kinds: dict[str, int] = {}
        for t in data.approved:
            kinds[classify_logo(t.logos[0] if t.logos else None)] = kinds.get(
                classify_logo(t.logos[0] if t.logos else None), 0
            ) + 1

        approval = (
            f"`{schema.approval_col}` (values seen: {', '.join(f'`{s}`' for s in data.status_values[:8]) or 'none'})"
            if schema.approval_col
            else ("table name implies approved" if schema.approval_implied else "**NOT FOUND**")
        )
        logo_src = ", ".join(f"`{c}`" for c in schema.logo_cols) or "—"
        if schema.logo_link:
            m, t, r = schema.logo_link
            logo_src = f"table `{t}` columns {', '.join(f'`{c}`' for c in schema.logo_link_cols)} (linked by `{m}`)"

        embed = discord.Embed(title="Team emoji — detected layout", colour=discord.Colour.blurple())
        embed.add_field(name="Database", value=f"`{schema.db_path}`", inline=False)
        embed.add_field(name="Team table", value=f"`{schema.table}`", inline=True)
        embed.add_field(name="Name column", value=f"`{schema.name_col}`", inline=True)
        embed.add_field(name="Key column", value=f"`{schema.key_col}`" if schema.key_col else "name", inline=True)
        embed.add_field(name="Approval", value=approval, inline=False)
        embed.add_field(
            name="Short form", value=f"`{schema.short_col}`" if schema.short_col else "auto-generated", inline=True
        )
        embed.add_field(name="Logo source", value=logo_src, inline=False)
        embed.add_field(
            name=f"Rows: {data.total_rows} total / {len(data.approved)} approved",
            value=clip_lines([f"{k}: {v}" for k, v in kinds.items()]) if kinds else "—",
            inline=False,
        )
        embed.add_field(
            name="Server filter",
            value=f"`{schema.guild_col}` = this server" if schema.guild_col else "none (no guild column)",
            inline=True,
        )
        if data.other_guild:
            embed.add_field(
                name=f"Approved but in another server ({len(data.other_guild)})",
                value=clip_lines(data.other_guild),
                inline=False,
            )
        if data.duplicates:
            embed.add_field(
                name=f"Approved duplicates skipped ({len(data.duplicates)})",
                value=clip_lines(data.duplicates),
                inline=False,
            )
        embed.add_field(
            name="Approved teams counted",
            value=clip_lines([f"{i}. {t.name} → {t.short}" for i, t in enumerate(data.approved, 1)], 1000),
            inline=False,
        )
        embed.add_field(name="All columns", value=clip_lines([", ".join(schema.columns)]), inline=False)
        embed.set_footer(text=f"Pillow: {'yes' if PIL_AVAILABLE else 'no (images are not auto-resized)'}")
        await ctx.send(embed=embed)

    # ---- main workflow ------------------------------------------------------ #

    async def _run_sync(self, ctx: commands.Context) -> None:
        guild = ctx.guild
        assert guild is not None

        me = guild.me
        if me is None or not me.guild_permissions.manage_expressions:
            await ctx.send("❌ I need the **Manage Expressions** permission (Manage Emojis and Stickers) to do this.")
            return

        progress = await ctx.send("🔍 Reading the team database…")

        db_path, schema = await self._locate()
        if schema.approval_col is None and not schema.approval_implied:
            raise SchemaError(
                f"Found team table `{schema.table}` but no approval/status column "
                f"(columns: {', '.join(schema.columns)}). I won't guess which teams are approved. "
                "Run `+emojischema` to see what I detected."
            )

        await asyncio.to_thread(init_records_table, db_path)
        data = await asyncio.to_thread(load_teams, schema, guild.id)
        records = await asyncio.to_thread(load_records, db_path, guild.id)

        if not data.approved:
            seen = ", ".join(f"`{s}`" for s in data.status_values[:8]) or "none"
            await progress.edit(
                content=(
                    f"⚠️ No approved teams found in `{schema.table}` "
                    f"({data.total_rows} rows; `{schema.approval_col}` values seen: {seen})."
                )
            )
            return

        fresh = await guild.fetch_emojis()
        live: dict[int, discord.Emoji] = {e.id: e for e in fresh}
        state = RunState(
            taken_names={e.name.lower() for e in fresh},
            static_used=sum(1 for e in fresh if not e.animated),
            animated_used=sum(1 for e in fresh if e.animated),
            limit=guild.emoji_limit,
        )
        # Names assigned to teams in this run must also not collide with each other.
        # (Handled by state.taken_names growing as emojis are created.)

        created: list[discord.Emoji] = []
        created_lines: list[str] = []
        skipped: list[str] = []
        missing: list[str] = []
        failed: list[str] = []
        total = len(data.approved)
        last_edit = 0.0

        for index, team in enumerate(data.approved, start=1):
            now = time.monotonic()
            if now - last_edit >= PROGRESS_INTERVAL:
                last_edit = now
                filled = int(10 * (index - 1) / total)
                bar = "█" * filled + "░" * (10 - filled)
                try:
                    await progress.edit(
                        content=(
                            f"⏳ **Syncing team emojis…**\n`[{bar}]` {index - 1}/{total} — now: **{team.name}**\n"
                            f"✅ {len(created)} created • ⏭️ {len(skipped)} skipped • "
                            f"🖼️ {len(missing)} no logo • ❌ {len(failed)} failed"
                        )
                    )
                except discord.HTTPException:
                    pass

            try:
                status, detail = await self._process_team(guild, schema, team, records, live, state)
            except Exception:  # last-resort guard: one team must never kill the run
                log.exception("Unhandled error processing team %s", team.name)
                status, detail = "failed", f"{team.name} — unexpected error"

            if status == "created":
                created.append(detail)
                created_lines.append(f"{detail} `:{detail.name}:` — {team.name}")
            elif status == "skipped":
                skipped.append(detail)
            elif status == "missing":
                missing.append(detail)
            else:
                failed.append(detail)

        colour = discord.Colour.green() if not failed else discord.Colour.orange()
        embed = discord.Embed(
            title="Team emoji sync complete",
            colour=colour,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Total approved teams", value=str(total), inline=True)
        embed.add_field(name="Emojis created", value=str(len(created)), inline=True)
        embed.add_field(name="Already existing / skipped", value=str(len(skipped)), inline=True)
        embed.add_field(name="Missing logos", value=str(len(missing)), inline=True)
        embed.add_field(name="Failed uploads", value=str(len(failed)), inline=True)
        embed.add_field(
            name="Server slots",
            value=f"static {state.static_used}/{state.limit} • animated {state.animated_used}/{state.limit}",
            inline=True,
        )
        if created_lines:
            embed.add_field(name="✅ Created", value=clip_lines(created_lines), inline=False)
        if missing:
            embed.add_field(name="🖼️ Teams without a logo", value=clip_lines(missing), inline=False)
        if failed:
            embed.add_field(name="❌ Failed", value=clip_lines(failed), inline=False)
        embed.set_footer(text=f"Records saved in `{EMOJI_TABLE}`")

        try:
            await progress.edit(content="✅ Done.", embed=embed)
        except discord.HTTPException:
            await ctx.send(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(TeamEmojis(bot))
