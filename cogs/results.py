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
    """Return possible image URLs, prioritizing real image/thumbnail URLs in embeds."""
    candidates: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(url: str | None, filename: str) -> None:
        if not url or url in seen:
            return
        if url.startswith(("https://", "http://")):
            seen.add(url)
            candidates.append((url, filename))

    # Attachments are the most reliable source when present.
    for attachment in message.attachments:
        is_image = (
            bool(attachment.content_type and attachment.content_type.startswith("image/"))
            or attachment.filename.lower().endswith(
                (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".gif")
            )
        )
        if is_image:
            add(attachment.url, attachment.filename or "scorecard.png")

    # Hand-cricket scorecards are often delivered as embed images, not attachments.
    for embed in message.embeds:
        if embed.image:
            # Discord's media proxy is often more accessible than the original
            # third-party embed host (which may block direct bot requests).
            add(embed.image.proxy_url, "scorecard_embed.png")
            add(embed.image.url, "scorecard_embed.png")
        if embed.thumbnail:
            add(embed.thumbnail.proxy_url, "scorecard_thumbnail.png")
            add(embed.thumbnail.url, "scorecard_thumbnail.png")

    # Some bots put the direct image URL in the message text instead of embed.image.
    for match in re.findall(r"https?://[^\s<>]+", message.content or ""):
        add(match.rstrip(".,)>]"), "scorecard_link.png")

    return candidates


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
                scorecard_message = await ctx.channel.fetch_message(reference.message_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                await ctx.reply("❌ I couldn't access the replied scorecard message.", mention_author=False)
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

        match_no, team_a, team_b = answers

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
                    if response.status != 200:
                        await ctx.send("❌ OCR service returned an error. Please try again.")
                        return
                    ocr_data = await response.json(content_type=None)
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

        # Put the preview in the configured results channel. Only the person
        # who started +result may confirm it, even though the prompt is public.
        preview = await channel.send(
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
                and message.channel.id == channel.id
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

        result_message = (
            f"# __Match {match_no}__\n"
            f"**{team_a} vs {team_b}**\n\n"
            f"> ***[{winner}]({image_url}) {margin}***"
        )

        try:
            await channel.send(
                result_message,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.Forbidden:
            await ctx.send("❌ I can't post in the results channel. Check my channel permissions.")
            return
        except discord.HTTPException:
            await ctx.send("❌ Discord couldn't send the result. Please try again.")
            return

        await ctx.send(f"✅ Match {match_no} result posted in {channel.mention}.")


async def setup(bot: commands.Bot):
    await bot.add_cog(ResultsCog(bot))
