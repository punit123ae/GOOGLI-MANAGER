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
        """Ask the owner for each field, then publish the result."""
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

        for attachment in scorecard_message.attachments:
            if (attachment.content_type and attachment.content_type.startswith("image/")) or attachment.filename.lower().endswith(
                (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff")
            ):
                try:
                    image_bytes = await attachment.read()
                    image_filename = attachment.filename or image_filename
                    image_url = attachment.url
                    break
                except discord.HTTPException:
                    pass

        if image_bytes is None:
            for embed in scorecard_message.embeds:
                candidate = embed.image.url if embed.image else None
                if not candidate and embed.thumbnail:
                    candidate = embed.thumbnail.url
                if candidate:
                    image_url = candidate
                    break

        if image_bytes is None and image_url:
            try:
                timeout = aiohttp.ClientTimeout(total=30)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.get(image_url) as response:
                        if response.status == 200:
                            image_bytes = await response.read()
            except (aiohttp.ClientError, TimeoutError):
                image_bytes = None

        if not image_bytes:
            await ctx.reply(
                "❌ The replied message doesn't contain an accessible scorecard image.",
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
            ("Team A", "Enter Team A's name."),
            ("Team B", "Enter Team B's name."),
            ("winning team", "Enter the winning team's name."),
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
                f"**Match result setup — {field}**\\n{prompt}\\n"
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

        match_no, team_a, team_b, winner = answers

        if team_a.casefold() == team_b.casefold():
            await ctx.send("❌ Team A and Team B must be different. Run `+result` again.")
            return
        if winner.casefold() not in {team_a.casefold(), team_b.casefold()}:
            await ctx.send("❌ The winning team must match Team A or Team B. Run `+result` again.")
            return

        # Send only the scorecard image to OCR.space. Never send Discord credentials or messages.
        try:
            form = aiohttp.FormData()
            form.add_field("apikey", api_key)
            form.add_field("language", "eng")
            form.add_field("isOverlayRequired", "false")
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
        ocr_text = "\\n".join(item.get("ParsedText", "") for item in parsed_results)
        margin_match = re.search(
            r"\\bwon\\s+(?:(?:the\\s+)?(?:game|match)\\s+)?by\\s+(\\d+)\\s+(runs?|wickets?)\\b",
            ocr_text,
            flags=re.IGNORECASE,
        )
        if not margin_match:
            await ctx.send(
                "❌ I couldn't detect a margin in the format `won by __ runs/wickets` from the scorecard. "
                "Please use a clearer scorecard image or try again."
            )
            return

        margin_number = margin_match.group(1)
        margin_type = margin_match.group(2).lower()
        margin_type = "runs" if margin_type.startswith("run") else "wickets"
        margin = f"won by {margin_number} {margin_type}"

        await ctx.send(
            f"**OCR detected:** `{margin}`\\n"
            "Reply `yes` to post this result or `no` to cancel. (2-minute timeout)"
        )
        try:
            confirmation = await self.bot.wait_for(
                "message",
                check=check,
                timeout=120,
            )
        except TimeoutError:
            await ctx.send("⌛ Confirmation timed out. Run `+result` again.")
            return

        if confirmation.content.strip().casefold() != "yes":
            await ctx.send("❌ Result posting cancelled. Reply `yes` to confirm next time.")
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
