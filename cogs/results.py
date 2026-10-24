"""Kashmir Titans interactive match-results command cog.

Commands:
    +setresultchannel #channel
    +result

Load this extension from bot.py with:
    await bot.load_extension("results")
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlparse

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

        prompts = [
            ("match number", "Enter the match number (e.g. `81`)."),
            ("Team A", "Enter Team A's name."),
            ("Team B", "Enter Team B's name."),
            ("winning team", "Enter the winning team's name."),
            ("winning margin", "Enter the winning margin (e.g. `won by 9 wickets`)."),
            ("scorecard image URL", "Enter the scorecard image URL."),
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
                await ctx.send("⌛ Result setup timed out. Run `+result` to start again.")
                return

            answer = reply.content.strip()
            if answer.casefold() == "cancel":
                await ctx.send("❌ Result setup cancelled.")
                return
            if not answer:
                await ctx.send(f"❌ {field.capitalize()} cannot be empty. Run `+result` again.")
                return
            answers.append(answer)

        match_no, team_a, team_b, winner, margin, image_url = answers

        if team_a.casefold() == team_b.casefold():
            await ctx.send("❌ Team A and Team B must be different. Run `+result` again.")
            return
        if winner.casefold() not in {team_a.casefold(), team_b.casefold()}:
            await ctx.send("❌ The winning team must match Team A or Team B. Run `+result` again.")
            return

        parsed_url = urlparse(image_url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            await ctx.send("❌ That doesn't look like a valid HTTP(S) image URL. Run `+result` again.")
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
