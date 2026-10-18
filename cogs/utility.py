import re
import discord
from discord.ext import commands

BOT_OWNER_ID = 1014568928682459198

RECOVERY_COMMANDS = {
    "toggleall", "untoggleall", "whitelist", "unwhitelist", "whitelistlist",
    "enableall", "enablechannel",
}


def _normalise_command_name(name):
    return str(name or "").strip().lower().lstrip("+!/").replace(" ", "")


def role_ids(member):
    return {int(role.id) for role in getattr(member, "roles", [])}


def has_any_whitelisted_role(bot, guild_id, member):
    if not guild_id or member is None:
        return False
    rows = bot.db.execute(
        "SELECT DISTINCT role_id FROM command_whitelist WHERE guild_id=?",
        (int(guild_id),),
    ).fetchall()
    allowed = {int(row[0]) for row in rows}
    return bool(role_ids(member) & allowed)


def has_command_whitelist(bot, guild_id, member, command_name):
    if not guild_id or member is None or not command_name:
        return False
    name = _normalise_command_name(command_name)
    rows = bot.db.execute(
        "SELECT role_id FROM command_whitelist WHERE guild_id=? AND command_name=?",
        (int(guild_id), name),
    ).fetchall()
    allowed = {int(row[0]) for row in rows}
    return bool(role_ids(member) & allowed)


class PostApprovedTeamsView(discord.ui.View):
    """Owner-only confirmation before publishing approved team embeds."""
    def __init__(self, cog, guild_id, channel_id, author_id):
        super().__init__(timeout=60)
        self.cog = cog
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.author_id = author_id

    async def interaction_check(self, interaction):
        if interaction.user.id != self.author_id or interaction.user.id != BOT_OWNER_ID:
            await interaction.response.send_message(
                "Only the bot owner who started this action can confirm it.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Confirm and post", style=discord.ButtonStyle.success)
    async def confirm(self, interaction, button):
        if interaction.user.id != BOT_OWNER_ID:
            await interaction.response.send_message("Only the bot owner can do this.", ephemeral=True)
            return
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            content="Posting approved team embeds… please wait.", view=self
        )
        count, error = await self.cog._post_approved_team_embeds(
            self.guild_id, self.channel_id
        )
        try:
            await interaction.edit_original_response(
                content=(f"✅ Posted **{count}** approved team embed(s)." if error is None
                         else f"⚠️ Posted **{count}** team embed(s), then stopped: {error}"),
                view=self,
            )
        except discord.HTTPException:
            pass

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, button):
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            content="Cancelled. No team embeds were posted.", view=self
        )


class Utility(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.db = bot.db
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS command_whitelist (
            guild_id INTEGER NOT NULL,
            role_id INTEGER NOT NULL,
            command_name TEXT NOT NULL,
            PRIMARY KEY (guild_id, role_id, command_name)
        );
        CREATE TABLE IF NOT EXISTS utility_guild_state (
            guild_id INTEGER PRIMARY KEY,
            commands_locked INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS utility_disabled_channels (
            guild_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            PRIMARY KEY (guild_id, channel_id)
        );
        """)
        self.db.commit()
        self.bot.add_check(self.global_command_check)
        # discord.py's app-command tree supports a global interaction check.
        self._previous_tree_check = getattr(self.bot.tree, "interaction_check", None)
        self.bot.tree.interaction_check = self.global_interaction_check

    def cog_unload(self):
        self.bot.remove_check(self.global_command_check)
        # Restore the previous check if this cog is unloaded.
        if getattr(self.bot.tree, "interaction_check", None) == self.global_interaction_check:
            self.bot.tree.interaction_check = self._previous_tree_check

    def is_locked(self, guild_id):
        if not guild_id:
            return False
        row = self.db.execute(
            "SELECT commands_locked FROM utility_guild_state WHERE guild_id=?",
            (int(guild_id),),
        ).fetchone()
        return bool(row[0]) if row else False

    def is_channel_disabled(self, guild_id, channel_id):
        if not guild_id or not channel_id:
            return False
        return self.db.execute(
            "SELECT 1 FROM utility_disabled_channels WHERE guild_id=? AND channel_id=?",
            (int(guild_id), int(channel_id)),
        ).fetchone() is not None

    async def command_allowed(self, ctx_or_interaction, command_name):
        """Permission gate for formerly admin-only commands in any supported cog.

        While unlocked, the exact command must be whitelisted for one of the
        member's roles. During the global lock, any whitelisted role can use
        commands. Channel locks always apply, except for recovery commands.
        """
        guild = getattr(ctx_or_interaction, "guild", None)
        guild_id = getattr(ctx_or_interaction, "guild_id", None) or getattr(guild, "id", None)
        member = getattr(ctx_or_interaction, "author", None) or getattr(ctx_or_interaction, "user", None)
        channel = getattr(ctx_or_interaction, "channel", None)
        channel_id = getattr(ctx_or_interaction, "channel_id", None) or getattr(channel, "id", None)
        name = _normalise_command_name(command_name)

        if member is not None and int(getattr(member, "id", 0)) == BOT_OWNER_ID:
            return True
        if not guild_id or member is None:
            return False
        if self.is_channel_disabled(guild_id, channel_id) and name not in RECOVERY_COMMANDS:
            return False
        if self.is_locked(guild_id):
            return has_any_whitelisted_role(self.bot, guild_id, member)
        return has_command_whitelist(self.bot, guild_id, member, name)

    async def global_command_check(self, ctx):
        if not ctx.guild:
            return True
        if int(ctx.author.id) == BOT_OWNER_ID:
            return True
        name = _normalise_command_name(getattr(ctx.command, "name", ""))
        if self.is_channel_disabled(ctx.guild.id, ctx.channel.id):
            if name in RECOVERY_COMMANDS and await self._can_manage_utility(ctx.guild.id, ctx.author):
                return True
            return False
        if self.is_locked(ctx.guild.id):
            return has_any_whitelisted_role(self.bot, ctx.guild.id, ctx.author)
        return True

    async def global_interaction_check(self, interaction):
        # Preserve any interaction check that existed before this cog loaded.
        previous = self._previous_tree_check
        if previous is not None:
            result = previous(interaction)
            if hasattr(result, "__await__"):
                result = await result
            if not result:
                return False

        if interaction.guild_id is None:
            return True
        if int(interaction.user.id) == BOT_OWNER_ID:
            return True
        command = getattr(interaction, "command", None)
        name = _normalise_command_name(getattr(command, "name", ""))
        if self.is_channel_disabled(interaction.guild_id, interaction.channel_id):
            return False
        if self.is_locked(interaction.guild_id):
            return has_any_whitelisted_role(self.bot, interaction.guild_id, interaction.user)
        return True

    async def _can_manage_utility(self, guild_id, member):
        # Only the configured bot owner may change whitelist and lock settings.
        # Discord Administrator permission does not grant this access.
        return member is not None and int(getattr(member, "id", 0)) == BOT_OWNER_ID

    async def _require_manager(self, ctx):
        if await self._can_manage_utility(ctx.guild.id, ctx.author):
            return True
        await ctx.reply(
            "You are not allowed to manage utility settings.",
            mention_author=False,
        )
        return False

    @commands.command(name="whitelist")
    @commands.guild_only()
    async def whitelist(self, ctx, role: str, *, command_name: str):
        if not await self._require_manager(ctx):
            return
        match = re.fullmatch(r"<@&?(\d+)>|@?(\d+)", role.strip())
        if not match:
            await ctx.reply(
                "Usage: `+whitelist @Role commandname` or `+whitelist ROLE_ID commandname`.",
                mention_author=False,
            )
            return
        role_id = int(next(group for group in match.groups() if group))
        discord_role = ctx.guild.get_role(role_id)
        if discord_role is None:
            await ctx.reply("That role ID doesn't exist in this server.", mention_author=False)
            return
        name = _normalise_command_name(command_name)
        if not re.fullmatch(r"[a-z0-9_-]+", name):
            await ctx.reply("Give one command name, without a prefix or arguments.", mention_author=False)
            return
        if name in {"whitelist", "unwhitelist", "toggleall", "untoggleall"}:
            await ctx.reply(
                "That utility control command cannot be individually whitelisted. Use the server lock carefully.",
                mention_author=False,
            )
            return
        self.db.execute(
            "INSERT OR IGNORE INTO command_whitelist(guild_id,role_id,command_name) VALUES(?,?,?)",
            (ctx.guild.id, role_id, name),
        )
        self.db.commit()
        await ctx.reply(
            f"Whitelisted **{discord.utils.escape_markdown(discord_role.name)}** (`{role_id}`) for `+{name}`.",
            mention_author=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.command(name="unwhitelist")
    @commands.guild_only()
    async def unwhitelist(self, ctx, role: str, *, command_name: str):
        if not await self._require_manager(ctx):
            return
        match = re.fullmatch(r"<@&?(\d+)>|@?(\d+)", role.strip())
        if not match:
            await ctx.reply(
                "Usage: `+unwhitelist @Role commandname` or `+unwhitelist ROLE_ID commandname`.",
                mention_author=False,
            )
            return
        role_id = int(next(group for group in match.groups() if group))
        name = _normalise_command_name(command_name)
        cur = self.db.execute(
            "DELETE FROM command_whitelist WHERE guild_id=? AND role_id=? AND command_name=?",
            (ctx.guild.id, role_id, name),
        )
        self.db.commit()
        await ctx.reply(
            "Whitelist rule removed." if cur.rowcount else "No matching whitelist rule was found.",
            mention_author=False,
        )

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
            role_label = (
                f"{discord.utils.escape_markdown(role_obj.name)} (`{row['role_id']}`)"
                if role_obj else f"Missing role (`{row['role_id']}`)"
            )
            lines.append(f"{role_label} → `+{row['command_name']}`")
        await ctx.reply(
            "\n".join(lines)[:1900],
            mention_author=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.command(name="toggleall")
    @commands.guild_only()
    async def toggleall(self, ctx):
        if not await self._require_manager(ctx):
            return
        self.db.execute(
            "INSERT INTO utility_guild_state(guild_id,commands_locked) VALUES(?,1) "
            "ON CONFLICT(guild_id) DO UPDATE SET commands_locked=1",
            (ctx.guild.id,),
        )
        self.db.commit()
        await ctx.reply(
            "🔒 Server-wide command lock enabled. Only members with a role appearing in the whitelist can use commands until unlocked.",
            mention_author=False,
        )

    @commands.command(name="untoggleall")
    @commands.guild_only()
    async def untoggleall(self, ctx):
        if not await self._require_manager(ctx):
            return
        self.db.execute(
            "INSERT INTO utility_guild_state(guild_id,commands_locked) VALUES(?,0) "
            "ON CONFLICT(guild_id) DO UPDATE SET commands_locked=0",
            (ctx.guild.id,),
        )
        self.db.commit()
        await ctx.reply("🔓 Server-wide command lock disabled.", mention_author=False)

    @commands.command(name="disableall")
    @commands.guild_only()
    async def disableall(self, ctx):
        if not await self._require_manager(ctx):
            return
        self.db.execute(
            "INSERT OR IGNORE INTO utility_disabled_channels(guild_id,channel_id) VALUES(?,?)",
            (ctx.guild.id, ctx.channel.id),
        )
        self.db.commit()
        await ctx.reply(
            "🔇 Bot commands are now disabled in this channel. Use `+enablechannel` here to restore them.",
            mention_author=False,
        )

    @commands.command(name="enablechannel")
    @commands.guild_only()
    async def enablechannel(self, ctx):
        if not await self._require_manager(ctx):
            return
        self.db.execute(
            "DELETE FROM utility_disabled_channels WHERE guild_id=? AND channel_id=?",
            (ctx.guild.id, ctx.channel.id),
        )
        self.db.commit()
        await ctx.reply("🔊 Bot commands are enabled in this channel again.", mention_author=False)

    @commands.command(name="enableall")
    @commands.guild_only()
    async def enableall(self, ctx):
        if not await self._require_manager(ctx):
            return
        self.db.execute(
            "DELETE FROM utility_disabled_channels WHERE guild_id=?",
            (ctx.guild.id,),
        )
        self.db.commit()
        await ctx.reply("🔊 Bot commands are enabled in all channels again.", mention_author=False)


    async def _post_approved_team_embeds(self, guild_id, channel_id):
        """Read approved teams from team_registration.db and post one embed per team."""
        import sqlite3

        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return 0, "I couldn't access the selected channel."
        if not isinstance(channel, discord.TextChannel) or channel.guild.id != guild_id:
            return 0, "The selected channel is not a text channel in this server."

        conn = None
        try:
            conn = sqlite3.connect("team_registration.db")
            conn.row_factory = sqlite3.Row
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS approved_team_post_settings (
                    guild_id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS approved_team_posted (
                    guild_id INTEGER NOT NULL, team_id INTEGER NOT NULL,
                    message_id INTEGER, PRIMARY KEY(guild_id, team_id)
                );
            """)
            rows = conn.execute(
                "SELECT id, name, captain_id, logo_url FROM registered_teams "
                "WHERE guild_id=? AND status='approved' ORDER BY name COLLATE NOCASE",
                (guild_id,),
            ).fetchall()
        except sqlite3.Error as exc:
            return 0, f"Couldn't read `team_registration.db` ({type(exc).__name__})."
        finally:
            if conn is not None:
                conn.close()

        if not rows:
            return 0, "No approved teams were found in this server."

        # Persist the chosen channel so later approvals can be announced automatically.
        try:
            conn = sqlite3.connect("team_registration.db")
            conn.execute(
                "INSERT INTO approved_team_post_settings(guild_id, channel_id) VALUES(?, ?) "
                "ON CONFLICT(guild_id) DO UPDATE SET channel_id=excluded.channel_id",
                (guild_id, channel_id),
            )
            conn.commit()
        finally:
            conn.close()

        # Continue numbering after the teams already posted, instead of numbering
        # by alphabetical position (which could label a newly posted team "3").
        conn = sqlite3.connect("team_registration.db")
        already_posted = conn.execute(
            "SELECT COUNT(*) FROM approved_team_posted WHERE guild_id=?",
            (guild_id,),
        ).fetchone()[0]
        conn.close()

        posted = 0
        for team in rows:
            conn = sqlite3.connect("team_registration.db")
            exists = conn.execute(
                "SELECT 1 FROM approved_team_posted WHERE guild_id=? AND team_id=?",
                (guild_id, int(team["id"])),
            ).fetchone()
            conn.close()
            if exists:
                continue
            # The next new embed gets the next sequential number in the channel.
            number = already_posted + posted + 1
            name = discord.utils.escape_markdown(str(team["name"] or "Unknown Team"))
            captain_id = int(team["captain_id"])
            logo_url = str(team["logo_url"] or "").strip()
            embed = discord.Embed(
                title="Kashmir Titans S1",
                description=(
                    f"{number}. **__Team name:__** **`{name}`**\n"
                    f"**__Team captain:__** <@{captain_id}>"
                ),
                color=discord.Color.from_rgb(55, 150, 255),
            )
            if logo_url.startswith(("https://", "http://")):
                # Thumbnail on the right and the full-size image at the bottom.
                embed.set_thumbnail(url=logo_url)
                embed.set_image(url=logo_url)
            try:
                # Captain mention is shown inside the embed only; no separate ping message.
                message = await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
                conn = sqlite3.connect("team_registration.db")
                conn.execute(
                    "INSERT OR IGNORE INTO approved_team_posted(guild_id, team_id, message_id) VALUES(?,?,?)",
                    (guild_id, int(team["id"]), message.id),
                )
                conn.commit()
                conn.close()
                posted += 1
            except discord.HTTPException as exc:
                return posted, f"Discord returned {type(exc).__name__}."
        return posted, None

    @commands.command(name="setapprovedteamschannel", aliases=["setteamannouncechannel"])
    @commands.guild_only()
    async def set_approved_teams_channel(self, ctx, channel: discord.TextChannel):
        """Owner-only: choose the one channel for future approved-team announcements."""
        if ctx.author.id != BOT_OWNER_ID:
            await ctx.reply("Only the bot owner can use this command.", mention_author=False)
            return
        if channel.guild.id != ctx.guild.id:
            await ctx.reply("Choose a text channel from this server.", mention_author=False)
            return
        import sqlite3
        conn = None
        try:
            conn = sqlite3.connect("team_registration.db")
            conn.execute("""CREATE TABLE IF NOT EXISTS approved_team_post_settings (
                guild_id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL
            )""")
            conn.execute(
                "INSERT INTO approved_team_post_settings(guild_id, channel_id) VALUES(?, ?) "
                "ON CONFLICT(guild_id) DO UPDATE SET channel_id=excluded.channel_id",
                (ctx.guild.id, channel.id),
            )
            conn.commit()
        except sqlite3.Error as exc:
            await ctx.reply(f"Couldn't save the channel ({type(exc).__name__}).", mention_author=False)
            return
        finally:
            if conn is not None:
                conn.close()
        await ctx.reply(
            f"Newly approved teams will be posted in {channel.mention} only. "
            "This setting is saved across bot restarts.",
            mention_author=False,
        )

    @commands.command(name="postapprovedteams", aliases=["postteams"])
    @commands.guild_only()
    async def post_approved_teams(self, ctx, channel: discord.TextChannel):
        """Owner-only: preview and confirm posting every approved team to a channel."""
        if ctx.author.id != BOT_OWNER_ID:
            await ctx.reply("Only the bot owner can use this command.", mention_author=False)
            return

        import sqlite3
        conn = None
        try:
            conn = sqlite3.connect("team_registration.db")
            count = conn.execute(
                "SELECT COUNT(*) FROM registered_teams WHERE guild_id=? AND status='approved'",
                (ctx.guild.id,),
            ).fetchone()[0]
        except sqlite3.Error as exc:
            await ctx.reply(f"Couldn't read `team_registration.db` ({type(exc).__name__}).", mention_author=False)
            return
        finally:
            if conn is not None:
                conn.close()

        if count == 0:
            await ctx.reply("There are no approved teams to post.", mention_author=False)
            return
        view = PostApprovedTeamsView(self, ctx.guild.id, channel.id, ctx.author.id)
        await ctx.reply(
            f"**Confirm posting {count} approved team embed(s)** in {channel.mention}?\n"
            "Each team gets one embed with its logo as both the right-side thumbnail and the full-size image at the bottom.",
            view=view,
            mention_author=False,
        )


async def setup(bot):
    await bot.add_cog(Utility(bot))
