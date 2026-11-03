"""
team_emojis.py - GOOGLI MANAGER

Adds one command:  +syncemojis [refresh]

Uploads every approved team's logo as a custom server emoji named after the
team's short form (e.g. Kashmir Titans -> :KT:) and stores the emoji ID/name
in a NEW table (team_emoji_records) inside team_registration.db.

This file does not touch existing cogs, commands, tables or schemas.
"""

import asyncio
import hashlib
import io
import logging
import re
import sqlite3
import time
import unicodedata
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

import aiohttp
import discord
from discord.ext import commands
from discord.http import Route

try:  # Pillow is optional but strongly recommended (resizes/converts logos).
    from PIL import Image, ImageOps, UnidentifiedImageError
    PIL_AVAILABLE = True
except ImportError:  # pragma: no cover
    PIL_AVAILABLE = False

log = logging.getLogger(__name__)

DB_PATH = "team_registration.db"
RECORD_TABLE = "team_emoji_records"

# Same bot-owner ID already hard-coded in the existing team commands
# (+setpingcooldown). bot.is_owner() is checked first, like +setteamlogo does.
EXTRA_BOT_OWNER_IDS = {1014568928682459198}

EMOJI_MAX_BYTES = 256 * 1024          # Discord's per-emoji upload limit
EMOJI_SIZE = 128                      # Discord's recommended emoji size
MAX_DOWNLOAD_BYTES = 10 * 1024 * 1024  # same ceiling the team cog uses for logos
MAX_IMAGE_PIXELS = 40_000_000          # decompression-bomb guard
UPLOAD_DELAY_SECONDS = 2.0             # gentle pacing; discord.py also honours 429s
PROGRESS_EDIT_INTERVAL = 1.5

# If a short-form column is ever added to registered_teams, it will be used.
SHORT_FORM_COLUMNS = ("short_name", "short_form", "shortname", "abbreviation", "abbr", "short_code", "tag", "code")
STOP_WORDS = {"the", "of", "and", "for"}


class InvalidLogo(Exception):
    """Raised when a logo is missing, undownloadable or not a usable image."""


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def _ascii_words(text: str) -> list[str]:
    normalized = unicodedata.normalize("NFKD", text or "")
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    return re.findall(r"[A-Za-z0-9]+", ascii_text)


def sanitize_emoji_name(value: str) -> str:
    """Discord emoji names: 2-32 chars, letters/digits/underscore only."""
    cleaned = re.sub(r"[^A-Za-z0-9_]", "", value or "")
    return cleaned[:32]


def generate_short_form(team_name: str, fallback_number: Optional[int] = None) -> str:
    """Kashmir Titans -> KT, Chennai Super Kings -> CSK, Royal Challengers Bengaluru -> RCB."""
    words = _ascii_words(team_name)
    significant = [w for w in words if w.lower() not in STOP_WORDS] or words
    short = "".join(w[0] for w in significant).upper()
    if len(short) < 2 and significant:
        # Single-word names: use the first three letters (Titans -> TIT).
        short = significant[0][:3].upper()
    if len(short) < 2:
        short = f"TEAM{fallback_number}" if fallback_number is not None else "TEAM"
    return sanitize_emoji_name(short[:10])


def _sniff_format(data: bytes) -> Optional[str]:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    return None


def prepare_emoji_image(data: bytes, content_type: Optional[str] = None) -> tuple[bytes, bool]:
    """
    Validate a logo and convert it into something Discord accepts as an emoji.
    Returns (image_bytes, is_animated). Raises InvalidLogo on any problem.
    Runs synchronously - call it through asyncio.to_thread.
    """
    if not data:
        raise InvalidLogo("Logo image is empty")
    fmt = _sniff_format(data)

    if not PIL_AVAILABLE:
        if fmt is None:
            raise InvalidLogo("Unsupported image format (install Pillow to convert WebP etc.)")
        if len(data) > EMOJI_MAX_BYTES:
            raise InvalidLogo(f"Image is {len(data) // 1024} KB, over the 256 KB emoji limit (install Pillow to auto-resize)")
        return data, fmt == "gif"

    try:
        with Image.open(io.BytesIO(data)) as img:
            width, height = img.size
            if width < 1 or height < 1 or width * height > MAX_IMAGE_PIXELS:
                raise InvalidLogo("Image dimensions are invalid or too large")
            # Keep small animated GIFs animated; everything else becomes a clean PNG.
            if img.format == "GIF" and getattr(img, "is_animated", False) and len(data) <= EMOJI_MAX_BYTES:
                return data, True
            img.seek(0)
            frame = ImageOps.exif_transpose(img).convert("RGBA")
    except InvalidLogo:
        raise
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError, SyntaxError) as exc:
        source_type = f"; server reported {content_type}" if content_type else ""
        raise InvalidLogo(
            f"Image data could not be decoded ({type(exc).__name__}{source_type})"
        ) from exc

    frame.thumbnail((EMOJI_SIZE, EMOJI_SIZE), Image.Resampling.LANCZOS)
    canvas = Image.new("RGBA", (EMOJI_SIZE, EMOJI_SIZE), (0, 0, 0, 0))
    canvas.paste(frame, ((EMOJI_SIZE - frame.width) // 2, (EMOJI_SIZE - frame.height) // 2), frame)
    out = io.BytesIO()
    canvas.save(out, format="PNG", optimize=True)
    result = out.getvalue()
    if len(result) > EMOJI_MAX_BYTES:
        raise InvalidLogo("Image is still over 256 KB after resizing")
    return result, False


def _truncate(text: str, limit: int) -> str:
    text = str(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _format_list(lines: list[str], limit: int = 1000) -> str:
    if not lines:
        return "None"
    out, used = [], 0
    for index, line in enumerate(lines):
        line = _truncate(line, 160)
        if used + len(line) + 1 > limit - 30:
            out.append(f"…and {len(lines) - index} more")
            break
        out.append(line)
        used += len(line) + 1
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

class TeamEmojis(commands.Cog):
    """Syncs approved team logos to custom server emojis."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = sqlite3.connect(DB_PATH, timeout=30)
        self.db.row_factory = sqlite3.Row
        self._session: Optional[aiohttp.ClientSession] = None
        self._locks: dict[int, asyncio.Lock] = {}
        self._init_db()

    async def cog_unload(self):
        try:
            self.db.close()
        except sqlite3.Error:
            pass
        if self._session and not self._session.closed:
            await self._session.close()

    # -- database ----------------------------------------------------------

    def _init_db(self):
        # New table only. Existing tables are never altered.
        self.db.execute(
            f"""CREATE TABLE IF NOT EXISTS {RECORD_TABLE} (
                guild_id   INTEGER NOT NULL,
                team_id    INTEGER NOT NULL,
                emoji_id   INTEGER NOT NULL,
                emoji_name TEXT    NOT NULL,
                short_form TEXT    NOT NULL,
                animated   INTEGER NOT NULL DEFAULT 0,
                logo_hash  TEXT,
                created_at TEXT    NOT NULL,
                PRIMARY KEY (guild_id, team_id)
            )"""
        )
        self.db.commit()

    def _approved_teams(self, guild_id: int) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM registered_teams WHERE guild_id=? AND status='approved' "
            "ORDER BY team_number ASC, created_at ASC, id ASC"
            if self._has_column("registered_teams", "team_number") else
            "SELECT * FROM registered_teams WHERE guild_id=? AND status='approved' ORDER BY id ASC",
            (guild_id,),
        ).fetchall()

    def _has_column(self, table: str, column: str) -> bool:
        return any(r[1] == column for r in self.db.execute(f"PRAGMA table_info({table})").fetchall())

    def _load_records(self, guild_id: int) -> dict[int, sqlite3.Row]:
        rows = self.db.execute(f"SELECT * FROM {RECORD_TABLE} WHERE guild_id=?", (guild_id,)).fetchall()
        return {int(r["team_id"]): r for r in rows}

    def _save_record(self, guild_id, team_id, emoji: discord.Emoji, short_form, logo_hash):
        self.db.execute(
            f"INSERT OR REPLACE INTO {RECORD_TABLE} "
            "(guild_id, team_id, emoji_id, emoji_name, short_form, animated, logo_hash, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (guild_id, team_id, emoji.id, emoji.name, short_form, int(emoji.animated),
             logo_hash, datetime.now(timezone.utc).isoformat()),
        )
        self.db.commit()

    def _delete_record(self, guild_id: int, team_id: int):
        self.db.execute(f"DELETE FROM {RECORD_TABLE} WHERE guild_id=? AND team_id=?", (guild_id, team_id))
        self.db.commit()

    # -- permissions -------------------------------------------------------

    async def _is_bot_owner(self, user) -> bool:
        try:
            if await self.bot.is_owner(user):
                return True
        except Exception:  # is_owner can raise if application info is unavailable
            pass
        return user.id in EXTRA_BOT_OWNER_IDS

    # -- logos -------------------------------------------------------------

    async def _refresh_discord_url(self, url: str) -> Optional[str]:
        """Ask Discord for a fresh signed link when a stored CDN attachment URL has expired."""
        host = (urlparse(url).hostname or "").lower()
        if host not in ("cdn.discordapp.com", "media.discordapp.net"):
            return None
        try:
            data = await self.bot.http.request(
                Route("POST", "/attachments/refresh-urls"), json={"attachment_urls": [url]}
            )
            refreshed = (data or {}).get("refreshed_urls") or []
            return refreshed[0].get("refreshed") if refreshed else None
        except Exception:
            log.warning("Could not refresh expired Discord attachment URL", exc_info=True)
            return None

    async def _download(self, url: str, _retried: bool = False) -> tuple[bytes, str]:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        try:
            async with self._session.get(url, headers={"User-Agent": "GOOGLI-MANAGER/1.0"}) as resp:
                if resp.status in (401, 403, 404) and not _retried:
                    fresh = await self._refresh_discord_url(url)
                    if fresh:
                        return await self._download(fresh, _retried=True)
                if resp.status != 200:
                    raise InvalidLogo(f"Logo download failed (HTTP {resp.status}); the link may have expired")
                if resp.content_length and resp.content_length > MAX_DOWNLOAD_BYTES:
                    raise InvalidLogo("Logo file is larger than 10 MB")
                content_type = resp.content_type.casefold()
                if content_type in ("text/html", "application/xhtml+xml", "application/json"):
                    raise InvalidLogo(
                        f"Logo link returned {content_type} instead of image data. "
                        "Use a direct image URL or upload the image itself."
                    )
                data = await resp.content.read(MAX_DOWNLOAD_BYTES + 1)
                content_type = resp.content_type
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise InvalidLogo("Could not download the logo from its link") from exc
        if not data:
            raise InvalidLogo("Downloaded logo is empty")
        if len(data) > MAX_DOWNLOAD_BYTES:
            raise InvalidLogo("Logo file is larger than 10 MB")
        return data, content_type

    @staticmethod
    def _logo_sources(team: sqlite3.Row) -> tuple[Optional[bytes], Optional[str]]:
        """Returns (stored_blob, usable_url). Both None means the team has no logo."""
        keys = team.keys()
        blob = team["logo_data"] if "logo_data" in keys else None
        blob = bytes(blob) if blob else None
        url = str(team["logo_url"] or "").strip().strip("<>") if "logo_url" in keys else ""
        url = url if url.startswith(("https://", "http://")) else None
        return blob, url

    async def _load_logo(self, team: sqlite3.Row) -> tuple[bytes, str, bool]:
        """
        Find a working logo. Uses the logo_url shown by +teaminfo (and the
        logo_data bytes first, only if that column exists in your database). Returns (emoji_bytes, source_hash, animated).
        """
        blob, url = self._logo_sources(team)
        last_error: Optional[InvalidLogo] = None
        if blob:
            try:
                image, animated = await asyncio.to_thread(prepare_emoji_image, blob)
                return image, hashlib.sha256(blob).hexdigest(), animated
            except InvalidLogo as exc:
                last_error = exc
        if url:
            try:
                raw, content_type = await self._download(url)
                image, animated = await asyncio.to_thread(prepare_emoji_image, raw, content_type)
                return image, hashlib.sha256(raw).hexdigest(), animated
            except InvalidLogo as exc:
                last_error = exc
        raise last_error or InvalidLogo("No logo")

    # -- naming ------------------------------------------------------------

    def _short_form_for(self, team: sqlite3.Row) -> str:
        keys = team.keys()
        for column in SHORT_FORM_COLUMNS:  # use an existing short form if one ever exists
            if column in keys and team[column]:
                existing = sanitize_emoji_name(str(team[column]))
                if len(existing) >= 2:
                    return existing
        number = team["team_number"] if "team_number" in keys and team["team_number"] else team["id"]
        return generate_short_form(team["name"], int(number))

    @staticmethod
    def _unique_name(base: str, taken: set[str]) -> str:
        base = sanitize_emoji_name(base) or "TEAM"
        if len(base) < 2:
            base = (base + "_")[:2]
        if base.lower() not in taken:
            return base
        counter = 2
        while True:
            suffix = str(counter)
            candidate = base[: 32 - len(suffix)] + suffix
            if candidate.lower() not in taken:
                return candidate
            counter += 1

    # -- command -----------------------------------------------------------

    @commands.command(name="syncemojis")
    @commands.guild_only()
    async def sync_emojis(self, ctx: commands.Context, mode: str = ""):
        """Upload approved team logos as server emojis (bot owner only).

        Usage: +syncemojis            -> create missing emojis, skip existing ones
               +syncemojis refresh    -> also replace emojis whose team logo has changed
        """
        if not await self._is_bot_owner(ctx.author):
            await ctx.reply("Only the bot owner can use this command.", mention_author=False)
            return

        mode = mode.strip().casefold()
        if mode not in ("", "refresh"):
            await ctx.reply("Usage: `+syncemojis` or `+syncemojis refresh`.", mention_author=False)
            return
        refresh = mode == "refresh"

        guild = ctx.guild
        me = guild.me
        if me is None or not me.guild_permissions.manage_expressions:
            await ctx.reply("I need the **Manage Expressions** permission to create emojis.", mention_author=False)
            return

        lock = self._locks.setdefault(guild.id, asyncio.Lock())
        if lock.locked():
            await ctx.reply("An emoji sync is already running for this server.", mention_author=False)
            return

        async with lock:
            try:
                await self._run_sync(ctx, refresh)
            except Exception:
                log.exception("Unexpected error during +syncemojis")
                await ctx.send("Something went wrong during the sync. Check the bot console for details.")

    async def _run_sync(self, ctx: commands.Context, refresh: bool):
        guild = ctx.guild

        try:
            teams = self._approved_teams(guild.id)
        except sqlite3.Error:
            log.exception("Could not read registered_teams")
            await ctx.reply("I couldn't read the team database. Make sure the team registration cog has loaded.", mention_author=False)
            return

        if not teams:
            await ctx.reply("There are no approved teams in this server.", mention_author=False)
            return

        try:
            guild_emojis = await guild.fetch_emojis()  # fresh data, not cache
        except discord.HTTPException:
            await ctx.reply("I couldn't fetch this server's emojis from Discord. Try again shortly.", mention_author=False)
            return

        emoji_by_id = {e.id: e for e in guild_emojis}
        taken = {e.name.lower() for e in guild_emojis}
        limit = guild.emoji_limit
        used = {False: sum(not e.animated for e in guild_emojis), True: sum(e.animated for e in guild_emojis)}
        records = self._load_records(guild.id)

        total = len(teams)
        created, updated, renamed = 0, 0, 0
        skipped: list[str] = []
        missing: list[str] = []
        failed: list[str] = []
        fatal_reason: Optional[str] = None

        progress = await ctx.send(f"⏳ Syncing team emojis… 0/{total}")
        last_edit = 0.0

        async def update_progress(index: int, name: str, force: bool = False):
            nonlocal last_edit
            now = time.monotonic()
            if not force and now - last_edit < PROGRESS_EDIT_INTERVAL:
                return
            last_edit = now
            try:
                await progress.edit(content=f"⏳ Syncing team emojis… **{index}/{total}** — {discord.utils.escape_markdown(name)}")
            except discord.HTTPException:
                pass

        for index, team in enumerate(teams, start=1):
            team_id = int(team["id"])
            team_name = str(team["name"] or f"Team {team_id}")

            if fatal_reason:
                failed.append(f"{team_name} — not processed: {fatal_reason}")
                continue

            await update_progress(index, team_name)

            try:
                record = records.get(team_id)
                existing_emoji = emoji_by_id.get(int(record["emoji_id"])) if record else None
                replacing = False

                if record and existing_emoji:
                    if not refresh:
                        skipped.append(team_name)
                        continue
                    # Refresh mode: only replace when the logo actually changed.
                    blob, url = self._logo_sources(team)
                    if not blob and not url:
                        skipped.append(team_name)
                        continue
                    try:
                        _, new_hash, _ = await self._load_logo(team)
                    except InvalidLogo:
                        skipped.append(team_name)  # keep the working emoji
                        continue
                    if not record["logo_hash"] or record["logo_hash"] == new_hash:
                        skipped.append(team_name)
                        continue
                    replacing = True
                elif record and not existing_emoji:
                    # Emoji was deleted from the server - drop the stale record and recreate.
                    self._delete_record(guild.id, team_id)

                blob, url = self._logo_sources(team)
                if not blob and not url:
                    missing.append(team_name)
                    continue

                try:
                    image, logo_hash, animated = await self._load_logo(team)
                except InvalidLogo as exc:
                    failed.append(f"{team_name} — {exc}")
                    continue

                if replacing:
                    try:
                        await existing_emoji.delete(reason="Replacing changed team logo")
                    except discord.Forbidden:
                        fatal_reason = "missing permission to delete/create emojis"
                        failed.append(f"{team_name} — {fatal_reason}")
                        continue
                    except discord.NotFound:
                        pass
                    except discord.HTTPException as exc:
                        failed.append(f"{team_name} — couldn't remove old emoji: {_truncate(exc.text or exc, 80)}")
                        continue
                    used[existing_emoji.animated] -= 1
                    taken.discard(existing_emoji.name.lower())
                    self._delete_record(guild.id, team_id)

                if used[animated] >= limit:
                    kind = "animated" if animated else "static"
                    failed.append(f"{team_name} — server {kind} emoji limit reached ({limit})")
                    continue

                short_form = self._short_form_for(team)
                name = self._unique_name(short_form, taken)
                if name != short_form:
                    renamed += 1
                taken.add(name.lower())

                try:
                    emoji = await guild.create_custom_emoji(
                        name=name, image=image, reason=_truncate(f"Team emoji for {team_name}", 100)
                    )
                except discord.Forbidden:
                    taken.discard(name.lower())
                    fatal_reason = "missing permission to create emojis"
                    failed.append(f"{team_name} — {fatal_reason}")
                    continue
                except discord.HTTPException as exc:
                    taken.discard(name.lower())
                    if getattr(exc, "code", None) == 30008:
                        failed.append(f"{team_name} — server emoji limit reached")
                    else:
                        failed.append(f"{team_name} — Discord rejected the upload: {_truncate(exc.text or exc, 80)}")
                    continue

                used[emoji.animated] += 1
                emoji_by_id[emoji.id] = emoji
                try:
                    self._save_record(guild.id, team_id, emoji, short_form, logo_hash)
                except sqlite3.Error as exc:
                    log.exception("Emoji %s created but its record could not be saved", emoji.id)
                    failed.append(
                        f"{team_name} — emoji created ({emoji}) but saving it failed: "
                        f"{_truncate(exc, 100)}"
                    )
                    continue

                if replacing:
                    updated += 1
                else:
                    created += 1
                await asyncio.sleep(UPLOAD_DELAY_SECONDS)

            except Exception as exc:  # never let one team crash the whole sync
                log.exception("Failed to process team %s", team_id)
                failed.append(f"{team_name} — unexpected error: {_truncate(exc, 80)}")

        # -- final report ----------------------------------------------------
        embed = discord.Embed(
            title="Team Emoji Sync Report",
            color=discord.Color.green() if not failed and not missing else discord.Color.orange(),
            description=f"Finished syncing emojis for **{discord.utils.escape_markdown(guild.name)}**.",
        )
        embed.add_field(name="Total approved teams", value=str(total), inline=True)
        embed.add_field(name="Emojis created", value=str(created), inline=True)
        embed.add_field(name="Already existing / skipped", value=str(len(skipped)), inline=True)
        if refresh:
            embed.add_field(name="Replaced (logo changed)", value=str(updated), inline=True)
        embed.add_field(
            name=f"Teams with missing logos ({len(missing)})",
            value=_format_list([f"• {n}" for n in missing]),
            inline=False,
        )
        embed.add_field(
            name=f"Failed uploads ({len(failed)})",
            value=_format_list([f"• {n}" for n in failed]),
            inline=False,
        )
        footer = f"Static emojis: {used[False]}/{limit} • Animated: {used[True]}/{limit}"
        if renamed:
            footer += f" • {renamed} name(s) adjusted to avoid duplicates"
        embed.set_footer(text=footer)

        try:
            await progress.edit(content=None, embed=embed)
        except discord.HTTPException:
            await ctx.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(TeamEmojis(bot))
