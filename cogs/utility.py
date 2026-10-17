import re
import discord
from discord.ext import commands

RECOVERY_COMMANDS = {
    "toggleall", "untoggleall", "whitelist", "unwhitelist", "whitelistlist",
    "enableall", "enablechannel",
}

def role_ids(member):
    return {role.id for role in getattr(member, "roles", [])}

def has_any_whitelisted_role(bot, guild_id, member):
    if not guild_id or not member:
        return False
    rows = bot.db.execute(
        "SELECT DISTINCT role_id FROM command_whitelist WHERE guild_id=?", (int(guild_id),)
    ).fetchall()
    allowed = {int(row[0]) for row in rows}
    return bool(role_ids(member) & allowed)

def has_command_whitelist(bot, guild_id, member, command_name):
    if not guild_id or not member or not command_name:
        return False
    name = str(command_name).strip().lower().lstrip("+!").replace(" ", "")
    rows = bot.db.execute(
        "SELECT role_id FROM command_whitelist WHERE guild_id=? AND command_name=?",
        (int(guild_id), name),
    ).fetchall()
    allowed = {int(row[0]) for row in rows}
    return bool(role_ids(member) & allowed)

def is_bot_admin(ctx):
    command_name = getattr(getattr(ctx, "command", None), "name", None)
    return bool(ctx.guild and has_command_whitelist(ctx.bot, ctx.guild.id, ctx.author, command_name))

def is_interaction_bot_admin(bot, interaction, command_name):
    return bool(interaction.guild_id and has_command_whitelist(
        bot, interaction.guild_id, interaction.user, command_name
    ))

class Utility(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.db = bot.db
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS command_whitelist (
            guild_id INTEGER NOT NULL, role_id INTEGER NOT NULL, command_name TEXT NOT NULL,
            PRIMARY KEY (guild_id, role_id, command_name)
        );
        CREATE TABLE IF NOT EXISTS utility_guild_state (
            guild_id INTEGER PRIMARY KEY, commands_locked INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS utility_disabled_channels (
            guild_id INTEGER NOT NULL, channel_id INTEGER NOT NULL,
            PRIMARY KEY (guild_id, channel_id)
        );
        """)
        self.db.commit()
        self.bot.add_check(self.global_command_check)

    def cog_unload(self):
        self.bot.remove_check(self.global_command_check)

    def is_locked(self, guild_id):
        row = self.db.execute("SELECT commands_locked FROM utility_guild_state WHERE guild_id=?", (guild_id,)).fetchone()
        return bool(row[0]) if row else False

    def is_channel_disabled(self, guild_id, channel_id):
        return self.db.execute(
            "SELECT 1 FROM utility_disabled_channels WHERE guild_id=? AND channel_id=?",
            (guild_id, channel_id),
        ).fetchone() is not None

    async def global_command_check(self, ctx):
        if not ctx.guild:
            return True
        name = (getattr(ctx.command, "name", "") or "").lower()
        if self.is_channel_disabled(ctx.guild.id, ctx.channel.id):
            if name in {"enablechannel", "enableall", "toggleall", "untoggleall", "whitelist", "unwhitelist", "whitelistlist"}:
                return True
            return False
        if self.is_locked(ctx.guild.id):
            if name in RECOVERY_COMMANDS:
                # Recovery/configuration commands are usable by explicitly whitelisted roles only.
                if has_any_whitelisted_role(self.bot, ctx.guild.id, ctx.author):
                    return True
                return False
            return has_any_whitelisted_role(self.bot, ctx.guild.id, ctx.author)
        return True

    async def _require_manager(self, ctx):
        # While unlocked, Discord admins can manage whitelist and lock settings.
        # During a server lock, a role already on the whitelist must manage recovery.
        if self.is_locked(ctx.guild.id):
            allowed = has_any_whitelisted_role(self.bot, ctx.guild.id, ctx.author)
        else:
            allowed = bool(ctx.author.guild_permissions.administrator)
        if not allowed:
            await ctx.reply("You are not allowed to manage utility settings.", mention_author=False)
        return allowed

    @commands.command(name="whitelist")
    @commands.guild_only()
    async def whitelist(self, ctx, role: str, *, command_name: str):
        if not await self._require_manager(ctx):
            return
        match = re.fullmatch(r"<@&?(\d+)>|@?(\d+)", role.strip())
        if not match:
            await ctx.reply("Usage: `+whitelist @Role commandname` or `+whitelist ROLE_ID commandname`.", mention_author=False)
            return
        role_id = int(next(group for group in match.groups() if group))
        discord_role = ctx.guild.get_role(role_id)
        if discord_role is None:
            await ctx.reply("That role ID doesn't exist in this server.", mention_author=False)
            return
        name = command_name.strip().lower().lstrip("+!").replace(" ", "")
        if not re.fullmatch(r"[a-z0-9_-]+", name):
            await ctx.reply("Give one command name, without a prefix or arguments.", mention_author=False)
            return
        if name in {"whitelist", "unwhitelist", "toggleall", "untoggleall"}:
            await ctx.reply("That utility control command cannot be individually whitelisted. Use the server lock carefully.", mention_author=False)
            return
        self.db.execute(
            "INSERT OR IGNORE INTO command_whitelist(guild_id,role_id,command_name) VALUES(?,?,?)",
            (ctx.guild.id, role_id, name),
        )
        self.db.commit()
        await ctx.reply(f"Whitelisted **{discord.utils.escape_markdown(discord_role.name)}** (`{role_id}`) for `+{name}`.", mention_author=False, allowed_mentions=discord.AllowedMentions.none())

    @commands.command(name="unwhitelist")
    @commands.guild_only()
    async def unwhitelist(self, ctx, role: str, *, command_name: str):
        if not await self._require_manager(ctx):
            return
        match = re.fullmatch(r"<@&?(\d+)>|@?(\d+)", role.strip())
        if not match:
            await ctx.reply("Usage: `+unwhitelist @Role commandname` or `+unwhitelist ROLE_ID commandname`.", mention_author=False)
            return
        role_id = int(next(group for group in match.groups() if group))
        name = command_name.strip().lower().lstrip("+!").replace(" ", "")
        cur = self.db.execute(
            "DELETE FROM command_whitelist WHERE guild_id=? AND role_id=? AND command_name=?",
            (ctx.guild.id, role_id, name),
        )
        self.db.commit()
        await ctx.reply("Whitelist rule removed." if cur.rowcount else "No matching whitelist rule was found.", mention_author=False)

    @commands.command(name="whitelistlist")
    @commands.guild_only()
    async def whitelistlist(self, ctx):
        if not await self._require_manager(ctx):
            return
        rows = self.db.execute(
            "SELECT role_id, command_name FROM command_whitelist WHERE guild_id=? ORDER BY role_id, command_name",
            (ctx.guild.id,),
        ).fetchall()
        if not rows:
            await ctx.reply("No whitelist rules are configured.", mention_author=False)
            return
        lines = []
        for row in rows:
            role_obj = ctx.guild.get_role(row["role_id"])
            role_label = f"{discord.utils.escape_markdown(role_obj.name)} (`{row['role_id']}`)" if role_obj else f"Missing role (`{row['role_id']}`)"
            lines.append(f"{role_label} → `+{row['command_name']}`")
        await ctx.reply("\n".join(lines)[:1900], mention_author=False, allowed_mentions=discord.AllowedMentions.none())

    @commands.command(name="toggleall")
    @commands.guild_only()
    async def toggleall(self, ctx):
        if not await self._require_manager(ctx):
            return
        self.db.execute(
            "INSERT INTO utility_guild_state(guild_id,commands_locked) VALUES(?,1) "
            "ON CONFLICT(guild_id) DO UPDATE SET commands_locked=1", (ctx.guild.id,)
        )
        self.db.commit()
        await ctx.reply("🔒 Server-wide command lock enabled. Only members with a role appearing in the whitelist can use commands until unlocked.", mention_author=False)

    @commands.command(name="untoggleall")
    @commands.guild_only()
    async def untoggleall(self, ctx):
        if not await self._require_manager(ctx):
            return
        self.db.execute(
            "INSERT INTO utility_guild_state(guild_id,commands_locked) VALUES(?,0) "
            "ON CONFLICT(guild_id) DO UPDATE SET commands_locked=0", (ctx.guild.id,)
        )
        self.db.commit()
        await ctx.reply("🔓 Server-wide command lock disabled.", mention_author=False)

    @commands.command(name="disableall")
    @commands.guild_only()
    async def disableall(self, ctx):
        if not await self._require_manager(ctx):
            return
        self.db.execute("INSERT OR IGNORE INTO utility_disabled_channels(guild_id,channel_id) VALUES(?,?)", (ctx.guild.id, ctx.channel.id))
        self.db.commit()
        await ctx.reply("🔇 Bot commands are now disabled in this channel. Use `+enablechannel` here to restore them.", mention_author=False)

    @commands.command(name="enablechannel")
    @commands.guild_only()
    async def enablechannel(self, ctx):
        if not await self._require_manager(ctx):
            return
        self.db.execute("DELETE FROM utility_disabled_channels WHERE guild_id=? AND channel_id=?", (ctx.guild.id, ctx.channel.id))
        self.db.commit()
        await ctx.reply("🔊 Bot commands are enabled in this channel again.", mention_author=False)

    @commands.command(name="enableall")
    @commands.guild_only()
    async def enableall(self, ctx):
        if not await self._require_manager(ctx):
            return
        self.db.execute("DELETE FROM utility_disabled_channels WHERE guild_id=?", (ctx.guild.id,))
        self.db.commit()
        await ctx.reply("🔊 Bot commands are enabled in all channels again.", mention_author=False)

async def setup(bot):
    await bot.add_cog(Utility(bot))
