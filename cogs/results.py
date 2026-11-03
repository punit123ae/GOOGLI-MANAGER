"""Kashmir Titans interactive match-results command cog.

Commands:
    +setresultchannel #channel
    +result

Load this extension from bot.py with:
    await bot.load_extension("results")
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import unicodedata
from pathlib import Path

import aiohttp
import discord
from discord.ext import commands

from .utility import owner_or_whitelisted

# Only this Discord user can use the commands in this cog.
OWNER_ID = 1014568928682459198
CONFIG_PATH = Path(__file__).with_name("results_config.json")


def _load_config() -> dict[str, int]:
    try:
        with CONFIG_PATH.open("r", encoding="utf-8") as file:
            data = json.load(file)
        return {str(guild_id): int(channel_id) for guild_id, channel_id in data.items()}
    except (FileNotFoundError, json.JSONDecodeError, TypeError, ValueError):
        return {}


def _save_config(config: dict[str, int]) -> None:
    with CONFIG_PATH.open("w", encoding="utf-8") as file:
        json.dump(config, file, indent=2)


def _scorecard_image_candidates(message: discord.Message) -> list[tuple[str, str]]:
    """Collect image URLs from attachments, embeds, and exposed forwarded snapshots."""
    candidates: list[tuple[str, str]] = []
    seen: set[str] = set()
    visited: set[int] = set()

    def add(url: str | None, filename: str) -> None:
        if not url or url in seen or not url.startswith(("https://", "http://")):
            return
        seen.add(url)
        candidates.append((url, filename))

    def inspect(obj, depth: int = 0) -> None:
        if obj is None or depth > 4 or id(obj) in visited:
            return
        visited.add(id(obj))
        for attachment in getattr(obj, "attachments", ()) or ():
            filename = getattr(attachment, "filename", "scorecard.png") or "scorecard.png"
            content_type = getattr(attachment, "content_type", None)
            if (content_type and content_type.startswith("image/")) or filename.lower().endswith(
                (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".gif")
            ):
                add(getattr(attachment, "url", None), filename)
        for embed in getattr(obj, "embeds", ()) or ():
            image = getattr(embed, "image", None)
            thumbnail = getattr(embed, "thumbnail", None)
            if image:
                add(getattr(image, "proxy_url", None), "scorecard_embed.png")
                add(getattr(image, "url", None), "scorecard_embed.png")
            if thumbnail:
                add(getattr(thumbnail, "proxy_url", None), "scorecard_thumbnail.png")
                add(getattr(thumbnail, "url", None), "scorecard_thumbnail.png")
        content = getattr(obj, "content", "") or ""
        for match in re.findall(r"https?://[^\s<>]+", content):
            add(match.rstrip(".,)>]"), "scorecard_link.png")
        # Discord.py versions expose forwarded payloads under differing attributes.
        for attr in ("message_snapshots", "forwarded_messages", "snapshots"):
            for snapshot in getattr(obj, attr, ()) or ():
                inspect(getattr(snapshot, "message", snapshot), depth + 1)
        reference = getattr(obj, "reference", None)
        resolved = getattr(reference, "resolved", None) if reference else None
        if resolved is not None and resolved is not obj:
            inspect(resolved, depth + 1)

    inspect(message)
    return candidates


def _normalize_team_key(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


def _team_database_path() -> Path:
    """Find the existing registration DB in common project/cog locations."""
    here = Path(__file__).resolve()
    candidates = [here.with_name("team_registration.db"), here.parent.parent / "team_registration.db"]
    for parent in here.parents[:4]:
        candidates.extend((parent / "team_registration.db", parent / "database" / "team_registration.db", parent / "data" / "team_registration.db"))
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("team_registration.db was not found beside the cog or in its parent project folders")


def _resolve_approved_team(guild_id: int, supplied: str) -> tuple[str | None, list[str]]:
    """Resolve exact names, initials, short forms, or unique prefixes from approved rows."""
    db_path = _team_database_path()
    with sqlite3.connect(db_path, timeout=10) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT name, name_key FROM registered_teams WHERE guild_id = ? AND status = 'approved'",
            (guild_id,),
        ).fetchall()
    query = _normalize_team_key(supplied)
    if not query:
        return None, []
    matches: set[str] = set()
    for row in rows:
        name = str(row["name"])
        normalized = _normalize_team_key(name)
        name_key = _normalize_team_key(str(row["name_key"] or name))
        words = normalized.split()
        initials = "".join(word[0] for word in words if word)
        aliases = {normalized, name_key, initials}
        # Prefix of the full normalized name and each component word.
        if normalized.startswith(query) or any(word.startswith(query) for word in words):
            matches.add(name)
        if query in aliases:
            matches.add(name)
        # Common short-form behavior: initials with optional separators.
        if query.replace(" ", "") == initials:
            matches.add(name)
    if len(matches) == 1:
        return next(iter(matches)), []
    return None, sorted(matches, key=str.casefold)


def _extract_winning_margin(ocr_text: str) -> str | None:
    """Normalize scorecard wording to exactly 'won by N runs/wickets'."""
    text = ocr_text.replace("\u00a0", " ").replace("\u200b", " ")
    text = re.sub(r"\s+", " ", text).strip()

    # OCR sometimes confuses punctuation around the result sentence. Keep the
    # match strict about the result verb, number, and result unit.
    patterns = (
        r"\bwon\s+(?:(?:the\s+)?(?:game|match)\s+)?by\s+(\d{1,3})\s+(runs?|wickets?)\b",
        r"\bwon\s+(?:the\s+)?(?:game|match)\s+by\s+(\d{1,3})\s+(runs?|wickets?)\b",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            number = match.group(1)
            unit = "runs" if match.group(2).lower().startswith("run") else "wickets"
            return f"won by {number} {unit}"
    return None


class ResultsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.result_channels = _load_config()
        self._published_results: set[tuple[int, str]] = set()

    async def cog_check(self, ctx: commands.Context) -> bool:
        # Resolve authorization per command, rather than granting cog-wide access.
        return await owner_or_whitelisted(ctx)

    async def cog_command_error(
        self, ctx: commands.Context, error: commands.CommandError
    ) -> None:
        if isinstance(error, commands.CheckFailure):
            await ctx.reply(
                "❌ Only the bot owner or a role whitelisted for this command can use it.",
                mention_author=False,
            )
            return
        raise error

    @commands.command(name="setresultchannel")
    @commands.guild_only()
    async def set_result_channel(
        self, ctx: commands.Context, channel: discord.TextChannel
    ):
        """Set the channel where match results will be posted."""
        if channel.guild.id != ctx.guild.id:
            await ctx.reply(
                "❌ Choose a text channel from this server.", mention_author=False
            )
            return

        self.result_channels[str(ctx.guild.id)] = channel.id
        _save_config(self.result_channels)
        await ctx.reply(
            f"✅ Match results will be posted in {channel.mention}.",
            mention_author=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.command(name="result")
    @commands.guild_only()
    async def post_result(self, ctx: commands.Context):
        """Read a replied scorecard embed and publish the result after confirmation."""
        channel_id = self.result_channels.get(str(ctx.guild.id))
        channel = ctx.guild.get_channel(channel_id) if channel_id else None
        if not isinstance(channel, discord.TextChannel):
            await ctx.reply(
                "❌ Results channel isn't configured. Use `+setresultchannel #channel` first.",
                mention_author=False,
            )
            return

        # +result must be sent as a reply to the scorecard image message.
        reference = ctx.message.reference
        if reference is None or reference.message_id is None:
            await ctx.reply(
                "❌ Run `+result` as a reply to the scorecard image message.",
                mention_author=False,
            )
            return

        scorecard_message = reference.resolved
        if not isinstance(scorecard_message, discord.Message):
            try:
                ref_channel_id = getattr(reference, "channel_id", None) or ctx.channel.id
                ref_channel = ctx.guild.get_channel(ref_channel_id)
                if ref_channel is None:
                    ref_channel = await self.bot.fetch_channel(ref_channel_id)
                if not hasattr(ref_channel, "fetch_message"):
                    raise discord.NotFound(response=None, message="Referenced channel unavailable")
                scorecard_message = await ref_channel.fetch_message(reference.message_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException, AttributeError):
                await ctx.reply("❌ I couldn't access the replied scorecard message. If it was forwarded, reply to the forwarded message itself.", mention_author=False)
                return

        image_bytes = None
        image_filename = "scorecard.png"
        image_url = None
        image_candidates = _scorecard_image_candidates(scorecard_message)

        if not image_candidates:
            await ctx.reply(
                "❌ I couldn't find an image in that message's attachments or embed. "
                "Make sure you reply directly to the scorecard image message.",
                mention_author=False,
            )
            return

        # Try each available source URL. Embed image URLs and Discord proxy URLs
        # are both checked because some embeds expose only one reliably.
        download_errors: list[str] = []
        try:
            timeout = aiohttp.ClientTimeout(total=45, connect=15, sock_read=30)
            headers = {
                "User-Agent": "Mozilla/5.0 (compatible; GOOGLI-MANAGER/1.0)",
                "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
                "Referer": "https://discord.com/",
            }
            async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
                for candidate_url, candidate_filename in image_candidates:
                    try:
                        async with session.get(candidate_url, allow_redirects=True) as response:
                            if response.status != 200:
                                download_errors.append(f"HTTP {response.status}")
                                continue
                            body = await response.read()
                            content_type = response.headers.get("Content-Type", "").lower()
                            # Check actual image signatures as some image hosts return
                            # generic or incorrect Content-Type headers.
                            is_image = (
                                content_type.startswith("image/")
                                or body.startswith(b"\x89PNG\r\n\x1a\n")
                                or body.startswith(b"\xff\xd8\xff")
                                or body.startswith((b"GIF87a", b"GIF89a"))
                                or (body.startswith(b"RIFF") and body[8:12] == b"WEBP")
                                or (len(body) > 12 and body[4:8] == b"ftyp")
                            )
                            if body and len(body) <= 15 * 1024 * 1024 and is_image:
                                image_bytes = body
                                image_filename = candidate_filename
                                image_url = candidate_url
                                break
                            download_errors.append("Response was not a supported image")
                    except (aiohttp.ClientError, TimeoutError) as exc:
                        download_errors.append(type(exc).__name__)
                        continue
        except (aiohttp.ClientError, TimeoutError) as exc:
            download_errors.append(type(exc).__name__)

        if not image_bytes or not image_url:
            await ctx.reply(
                "❌ I found the scorecard embed, but couldn't download its image. "
                "The image host may be blocking requests, or the embed image may have expired. "
                "Please try replying to the original scorecard message again.",
                mention_author=False,
            )
            return

        api_key = os.getenv("OCR_SPACE_API_KEY")
        if not api_key:
            await ctx.reply(
                "❌ OCR API key is missing. Add `OCR_SPACE_API_KEY` to your hosting environment variables.",
                mention_author=False,
            )
            return

        prompts = [
            ("match number", "Enter the match number (e.g. `81`)."),
            ("Team A (winner)", "Enter Team A's name. Team A will automatically be treated as the winner."),
            ("Team B (loser)", "Enter Team B's name."),
        ]
        answers: list[str] = []

        def check(message: discord.Message) -> bool:
            return (
                message.author.id == ctx.author.id
                and message.channel.id == ctx.channel.id
                and message.guild is not None
                and message.guild.id == ctx.guild.id
            )

        for field, prompt in prompts:
            await ctx.send(
                f"**Match result setup — {field}**\n{prompt}\n"
                "Reply `cancel` at any time to stop. (2-minute timeout per answer)"
            )
            try:
                reply = await self.bot.wait_for("message", check=check, timeout=120)
            except TimeoutError:
                await ctx.send("⌛ Result setup timed out. Run `+result` again.")
                return

            answer = reply.content.strip()
            if answer.casefold() == "cancel":
                await ctx.send("❌ Result setup cancelled.")
                return
            if not answer:
                await ctx.send(f"❌ {field.capitalize()} cannot be empty. Run `+result` again.")
                return
            answers.append(answer)

        match_no, team_a_input, team_b_input = answers

        try:
            team_a, ambiguous_a = _resolve_approved_team(ctx.guild.id, team_a_input)
            team_b, ambiguous_b = _resolve_approved_team(ctx.guild.id, team_b_input)
        except (sqlite3.Error, FileNotFoundError) as exc:
            await ctx.send(f"❌ Couldn't read the approved teams database ({type(exc).__name__}). Please contact a bot administrator.")
            return

        if team_a is None:
            choices = ", ".join(ambiguous_a) if ambiguous_a else "No approved team matched."
            await ctx.send(f"❌ Team A `{team_a_input}` is ambiguous or not approved. Enter a more specific registered name. Matches: {choices}")
            return
        if team_b is None:
            choices = ", ".join(ambiguous_b) if ambiguous_b else "No approved team matched."
            await ctx.send(f"❌ Team B `{team_b_input}` is ambiguous or not approved. Enter a more specific registered name. Matches: {choices}")
            return
        if team_a.casefold() == team_b.casefold():
            await ctx.send("❌ Team A and Team B must be different. Run `+result` again.")
            return

        # Team A is always the winner; Team B is always the opponent/loser.
        winner = team_a

        # Send only the scorecard image to OCR.space. Never send Discord credentials or messages.
        try:
            form = aiohttp.FormData()
            form.add_field("apikey", api_key)
            form.add_field("language", "eng")
            form.add_field("isOverlayRequired", "false")
            form.add_field("OCREngine", "2")
            form.add_field("scale", "true")
            form.add_field("detectOrientation", "true")
            form.add_field(
                "file",
                image_bytes,
                filename=image_filename,
                content_type="application/octet-stream",
            )
            timeout = aiohttp.ClientTimeout(total=60)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post("https://api.ocr.space/parse/image", data=form) as response:
                    # OCR.space can return non-200 responses for invalid/expired keys,
                    # quota limits, request-size limits, or service-side failures.
                    # Surface a sanitized diagnostic instead of hiding the cause.
                    if response.status != 200:
                        try:
                            error_body = (await response.text())[:1200]
                        except (aiohttp.ClientError, UnicodeError):
                            error_body = "<response body unavailable>"
                        # Never echo the secret API key if the provider includes it.
                        error_body = error_body.replace(api_key, "[REDACTED]") if api_key else error_body
                        error_body = re.sub(r"(?i)(apikey\s*[:=]\s*)[^\s,;]+", r"\1[REDACTED]", error_body)
                        error_body = re.sub(r"[\r\n]+", " ", error_body).strip()
                        await ctx.send(
                            f"❌ OCR.space returned HTTP {response.status}. "
                            f"Details: `{discord.utils.escape_markdown(error_body or 'No error details returned')[:900]}`. "
                            "Check that `OCR_SPACE_API_KEY` is valid and that your OCR.space account quota is available."
                        )
                        return
                    try:
                        ocr_data = await response.json(content_type=None)
                    except (aiohttp.ClientError, ValueError):
                        await ctx.send("❌ OCR.space returned an invalid response (not valid JSON). Please try again later.")
                        return
        except (aiohttp.ClientError, TimeoutError, ValueError):
            await ctx.send("❌ Couldn't connect to the OCR service. Please try again.")
            return

        if ocr_data.get("IsErroredOnProcessing"):
            await ctx.send("❌ OCR couldn't read this scorecard. Please try a clearer image.")
            return

        parsed_results = ocr_data.get("ParsedResults") or []
        ocr_text = " ".join(item.get("ParsedText", "") for item in parsed_results)
        margin = _extract_winning_margin(ocr_text)
        if not margin:
            # Include the provider's error only as a diagnostic, without exposing
            # API keys or dumping the entire OCR response into the Discord channel.
            await ctx.send(
                "❌ I couldn't find a clear `won by __ runs/wickets` result on the scorecard. "
                "Please check that the result line is visible in the embed image, then try again."
            )
            return

        # Confirmation remains in the command channel; publication uses configured channel.
        preview = await ctx.channel.send(
            f"**Match {match_no} — Result confirmation**\n"
            f"🏆 **Winner:** {team_a}\n"
            f"❌ **Loser:** {team_b}\n"
            f"**Margin:** `{margin}`\n\n"
            "Reply `yes` to publish this result or `no` to cancel. "
            "(10-minute timeout)",
            allowed_mentions=discord.AllowedMentions.none(),
        )

        def confirmation_check(message: discord.Message) -> bool:
            return (
                message.author.id == ctx.author.id
                and message.channel.id == ctx.channel.id
                and message.guild is not None
                and message.guild.id == ctx.guild.id
                and message.content.strip().casefold() in {"yes", "no", "cancel"}
            )

        try:
            confirmation = await self.bot.wait_for(
                "message",
                check=confirmation_check,
                timeout=600,
            )
        except TimeoutError:
            try:
                await preview.edit(content=(preview.content + "\n\n⌛ Confirmation expired; result was not posted."))
            except discord.HTTPException:
                pass
            return

        if confirmation.content.strip().casefold() != "yes":
            try:
                await preview.edit(content=(preview.content + "\n\n❌ Cancelled; result was not posted."))
            except discord.HTTPException:
                pass
            return

        result_key = (ctx.guild.id, str(match_no).strip())
        if result_key in self._published_results:
            await ctx.send(f"❌ Match {match_no} already has a result published by this bot session.")
            return

        result_message = (
            f"# __Match {match_no}__\n"
            f"**{team_a} vs {team_b}**\n\n"
            f"> ***[{winner}]({image_url}) {margin}***"
        )

        try:
            self._published_results.add(result_key)
            await channel.send(
                result_message,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.Forbidden:
            self._published_results.discard(result_key)
            await ctx.send("❌ I can't post in the results channel. Check my channel permissions.")
            return
        except discord.HTTPException:
            self._published_results.discard(result_key)
            await ctx.send("❌ Discord couldn't send the result. Please try again.")
            return

        await ctx.send(f"✅ Match {match_no} result posted in {channel.mention}.")


async def setup(bot: commands.Bot):
    await bot.add_cog(ResultsCog(bot))
