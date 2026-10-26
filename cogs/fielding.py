"""Hand Cricket catch tracking for Googli Manager."""

import re
from collections import defaultdict

import discord
from discord.ext import commands

HANDCRICKET_BOT_ID = 753191385296928808
BOT_OWNER_ID = 1014568928682459198


def _normalise_command_name(name):
    return str(name or "").strip().lower().lstrip("+!/").replace(" ", "")


def _member_has_whitelist_role(bot, guild_id, member, command_name):
    """Check the exact command's role whitelist stored by the utility cog."""
    if not guild_id or member is None:
        return False
    try:
        rows = bot.db.execute(
            "SELECT role_id FROM command_whitelist WHERE guild_id=? AND command_name=?",
            (int(guild_id), _normalise_command_name(command_name)),
        ).fetchall()
    except Exception:
        # If utility's whitelist table is unavailable, fail closed for non-owners.
        return False
    allowed = {int(row[0]) for row in rows}
    member_roles = {int(role.id) for role in getattr(member, "roles", [])}
    return bool(allowed & member_roles)


class CatchTracker(commands.Cog):
    """Tracks catch chances and outcomes in channels where tracking is started."""

    def __init__(self, bot):
        self.bot = bot
        # Stats remain in memory for this first tracking version.
        self.stats = defaultdict(
            lambda: defaultdict(
                lambda: {"taken": 0, "dropped": 0, "chances": 0, "caught": 0}
            )
        )
        self.pending = {}  # channel_id -> fielder_id, batsman_id, message_id
        self.enabled_channels = set()

    @staticmethod
    def _message_text(message):
        parts = [message.content or ""]
        for embed in message.embeds:
            parts.extend([embed.title or "", embed.description or ""])
            for field in embed.fields:
                parts.extend([field.name or "", field.value or ""])
        return "\n".join(parts)

    @staticmethod
    def _find_player(text, pattern, guild):
        match = re.search(pattern + r"\s*<@!?(\d+)>" , text, re.IGNORECASE)
        if match:
            return int(match.group(1))
        # Fallback for embeds that contain plain @username text.
        match = re.search(pattern + r"\s*@?([A-Za-z0-9_.-]{2,32})", text, re.IGNORECASE)
        if match and guild:
            username = match.group(1).casefold()
            for member in guild.members:
                if member.name.casefold() == username or member.display_name.casefold() == username:
                    return member.id
        return None

    def _parse_opportunity(self, message):
        text = self._message_text(message)
        if "CHANCE OF WICKET!" not in text.upper():
            return None
        fielder_id = self._find_player(text, r"coming\s+toward\s+", message.guild)
        batsman_id = self._find_player(text, r"takes\s+this\s+catch,\s*", message.guild)
        if fielder_id is None:
            return None
        return fielder_id, batsman_id

    @staticmethod
    def _result_kind(message):
        text = CatchTracker._message_text(message).upper()
        if "BOZO DROPPED THE CATCH" in text:
            return "dropped"
        if "TOOK THE CATCH" in text:
            return "taken"
        return None

    @staticmethod
    def _efficiency(values):
        total = values["taken"] + values["dropped"]
        return values["taken"] / total * 100 if total else 0.0

    async def _send_stats_embed(self, channel, fielder_id, result=None):
        """Send a NEW snapshot embed; never edit a previous fielding embed."""
        values = self.stats[channel.id][fielder_id]
        member = channel.guild.get_member(fielder_id) if channel.guild else None
        display_name = discord.utils.escape_markdown(
            member.display_name if member else f"<@{fielder_id}>"
        )

        if result == "taken":
            embed_color = discord.Color.green()
        elif result == "dropped":
            embed_color = discord.Color.red()
        else:
            embed_color = discord.Color.from_rgb(55, 150, 255)

        embed = discord.Embed(
            title=f"🧤 FIELDING STATS FOR {display_name}",
            color=embed_color,
        )
        if member:
            embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="CATCHES TAKEN", value=f"`{values['taken']}`", inline=False)
        embed.add_field(name="CATCHES DROPPED", value=f"`{values['dropped']}`", inline=False)
        embed.add_field(name="CATCH EFFICIENCY", value=f"`{self._efficiency(values):.2f}%`", inline=False)
        embed.set_footer(text="Fielding tracker • stats reset when bot restarts")
        await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    async def _tracking_permission(self, ctx, command_name):
        if int(ctx.author.id) == BOT_OWNER_ID:
            return True
        if _member_has_whitelist_role(self.bot, ctx.guild.id, ctx.author, command_name):
            return True
        await ctx.reply(
            f"❌ You need a role whitelisted for `+{command_name}` to use this command.",
            mention_author=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return False

    @commands.command(name="starttracking")
    @commands.guild_only()
    async def start_tracking(self, ctx):
        """Start Hand Cricket catch tracking in this channel."""
        if not await self._tracking_permission(ctx, "starttracking"):
            return
        if ctx.channel.id in self.enabled_channels:
            await ctx.reply("Catch tracking is already active in this channel.", mention_author=False)
            return
        self.enabled_channels.add(ctx.channel.id)
        self.stats[ctx.channel.id]
        self.pending.pop(ctx.channel.id, None)
        await ctx.reply(
            "✅ Catch tracking started in this channel. It will watch Hand Cricket catch opportunities and results.",
            mention_author=False,
        )

    @commands.command(name="stoptracking")
    @commands.guild_only()
    async def stop_tracking(self, ctx):
        """Stop catch tracking in this channel."""
        if not await self._tracking_permission(ctx, "stoptracking"):
            return
        if ctx.channel.id not in self.enabled_channels:
            await ctx.reply("Catch tracking is not active in this channel.", mention_author=False)
            return
        self.enabled_channels.discard(ctx.channel.id)
        self.pending.pop(ctx.channel.id, None)
        await ctx.reply(
            "🛑 Catch tracking stopped. Current stats remain in memory until bot restart.",
            mention_author=False,
        )

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.id != HANDCRICKET_BOT_ID:
            return
        channel_id = message.channel.id
        if channel_id not in self.enabled_channels or not message.guild:
            return

        opportunity = self._parse_opportunity(message)
        if opportunity:
            fielder_id, batsman_id = opportunity
            self.pending[channel_id] = {
                "fielder_id": fielder_id,
                "batsman_id": batsman_id,
                "message_id": message.id,
            }
            self.stats[channel_id][fielder_id]["chances"] += 1
            if batsman_id is not None:
                self.stats[channel_id][batsman_id]["chances"] += 1
            # Post the fielder's current stats as a new blue embed for this chance.
            await self._send_stats_embed(message.channel, fielder_id)
            return

        result = self._result_kind(message)
        if result is None:
            return
        pending = self.pending.get(channel_id)
        if not pending:
            return

        fielder_id = pending["fielder_id"]
        batsman_id = pending["batsman_id"]
        if result == "taken":
            self.stats[channel_id][fielder_id]["taken"] += 1
            if batsman_id is not None:
                self.stats[channel_id][batsman_id]["caught"] += 1
        else:
            self.stats[channel_id][fielder_id]["dropped"] += 1

        # Post a separate embed showing updated stats and result colour.
        await self._send_stats_embed(message.channel, fielder_id, result=result)
        self.pending.pop(channel_id, None)


async def setup(bot):
    await bot.add_cog(CatchTracker(bot))
