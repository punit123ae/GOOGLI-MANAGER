"""Team leaders: +perms / +removeperms, a fixed +vc (alias +setvc), and +pingteam / +teamchat that
vice-captains can use.

Place at cogs/teamleaders.py and add "cogs.teamleaders" to bot.py AFTER the team cogs
(after "cogs.teams1").  No other file has to be edited.

When it loads it REPLACES the old +pingteam, +vc and +teamchat commands (the old code in
teams2.py / teams3.py simply stops being used; you can delete it whenever you like).
Everything else in the team system (teams, rosters, roles, cooldown, +setpingcooldown ...) is reused.

Commands
  +perms @user [@user2 ...]     CAPTAIN ONLY. Lets those users use +pingteam for your team.
                                Several users at once (mentions, IDs or exact usernames).
                                +perms with nothing after it lists who has the permission.
  +removeperms @user [...]      CAPTAIN ONLY. Takes the permission away again
  (+unperms, +revokeperms)      (+removeperms all removes everybody).

  +pingteam                     captain, vice-captain, or anyone the captain gave +perms to.
                                (same per-team cooldown as before, shared by all of them)
  +teamchat <message>           captain or vice-captain (DMs the whole team, attachments too).

  +vc <user> [team name]        (alias +setvc)
      * a team captain:     +setvc @user            -> their own team, no team name needed
      * bot owner / a role whitelisted for "vc":
                            +setvc @user            -> the team that user is already on
                            +vc @user Team Name     -> any team, even one they don't captain
"""

import re
from datetime import datetime, timezone

import discord
from discord.ext import commands

from .utility import owner_or_whitelisted

REPLACED_COMMANDS = ("pingteam", "vc", "teamchat")
MAX_USERS_PER_CALL = 25
NO_MENTIONS = discord.AllowedMentions.none()


def _split_tokens(text):
    return [t for t in re.split(r"[\s,]+", (text or "").strip()) if t][:MAX_USERS_PER_CALL]


ID_RE = re.compile(r"<@!?(\d+)>|@?(\d{15,22})")


class TeamLeaders(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # ------------------------------------------------------------------ helpers
    async def _core(self, ctx):
        """The TeamRegistration cog (teams1.py) - all the team helpers live there."""
        core = self.bot.get_cog("TeamRegistration")
        if core is None:
            await ctx.reply("The team module isn't loaded.", mention_author=False)
        return core

    @staticmethod
    def _ensure_tables(core):
        core._ensure_ping_tables()          # cooldown tables (teams2.py)
        core.db.execute(
            "CREATE TABLE IF NOT EXISTS team_ping_perms ("
            "team_id INTEGER NOT NULL, user_id INTEGER NOT NULL, granted_by INTEGER NOT NULL, "
            "granted_at TEXT NOT NULL, PRIMARY KEY(team_id, user_id), "
            "FOREIGN KEY(team_id) REFERENCES registered_teams(id) ON DELETE CASCADE)")
        core.db.commit()

    @staticmethod
    def _captain_team(core, guild_id, user_id):
        return core.db.execute(
            "SELECT * FROM registered_teams WHERE guild_id=? AND status='approved' AND captain_id=? LIMIT 1",
            (guild_id, user_id)).fetchone()

    async def _is_admin(self, ctx):
        """Bot owner, or a role whitelisted for this command (same checks the other team commands use)."""
        try:
            if await owner_or_whitelisted(ctx, command_name="vc", app_owner=True):
                return True
        except Exception:
            pass
        core = self.bot.get_cog("TeamRegistration")
        if core is not None:
            try:
                return bool(await core._command_allowed(ctx, "vc"))
            except Exception:
                return False
        return False

    @staticmethod
    def _name(user_id, guild):
        member = guild.get_member(user_id) if guild else None
        return member.mention if member else f"<@{user_id}>"

    # ------------------------------------------------------------------ +perms / +removeperms
    @commands.command(name="perms", aliases=["giveperms", "addperms"])
    @commands.guild_only()
    async def perms(self, ctx, *, users: str = ""):
        """Captain only: let players use +pingteam for your team."""
        core = await self._core(ctx)
        if core is None:
            return
        self._ensure_tables(core)
        team = self._captain_team(core, ctx.guild.id, ctx.author.id)
        if team is None:
            await ctx.reply("Only an approved team captain can use this command.", mention_author=False)
            return

        if not users.strip():
            rows = core.db.execute(
                "SELECT user_id FROM team_ping_perms WHERE team_id=? ORDER BY granted_at", (team["id"],)).fetchall()
            listing = "\n".join(f"• {self._name(int(r['user_id']), ctx.guild)}" for r in rows) or "Nobody yet."
            await ctx.reply(
                f"**+pingteam permission — {discord.utils.escape_markdown(team['name'])}**\n{listing}\n\n"
                "Give it: `+perms @user @user2` · Take it away: `+removeperms @user` (or `+removeperms all`).",
                mention_author=False, allowed_mentions=NO_MENTIONS)
            return

        added, already, skipped, missing = [], [], [], []
        seen = set()
        for token in _split_tokens(users):
            member = await core._get_member(ctx.guild, token)
            if member is None:
                missing.append(token)
                continue
            if member.id in seen:
                continue
            seen.add(member.id)
            if member.bot:
                skipped.append(f"{member.mention} (a bot)")
            elif member.id == int(team["captain_id"]):
                skipped.append(f"{member.mention} (you're the captain)")
            elif team["vice_captain_id"] is not None and member.id == int(team["vice_captain_id"]):
                skipped.append(f"{member.mention} (vice-captain already can)")
            else:
                cursor = core.db.execute(
                    "INSERT OR IGNORE INTO team_ping_perms(team_id,user_id,granted_by,granted_at) VALUES(?,?,?,?)",
                    (team["id"], member.id, ctx.author.id, datetime.now(timezone.utc).isoformat()))
                (added if cursor.rowcount else already).append(member.mention)
        core.db.commit()

        lines = []
        if added:
            lines.append("✅ **Can now use `+pingteam`:** " + ", ".join(added))
        if already:
            lines.append("ℹ️ **Already had it:** " + ", ".join(already))
        if skipped:
            lines.append("⏭️ **Skipped:** " + ", ".join(skipped))
        if missing:
            lines.append("⚠️ **Couldn't find:** " + ", ".join(f"`{m}`" for m in missing)
                         + " (use a mention, an ID or an exact username)")
        await ctx.reply("\n".join(lines) or "Nothing to do.", mention_author=False, allowed_mentions=NO_MENTIONS)

    @commands.command(name="removeperms", aliases=["unperms", "revokeperms", "removeperm"])
    @commands.guild_only()
    async def removeperms(self, ctx, *, users: str = ""):
        """Captain only: take +pingteam permission away from players."""
        core = await self._core(ctx)
        if core is None:
            return
        self._ensure_tables(core)
        team = self._captain_team(core, ctx.guild.id, ctx.author.id)
        if team is None:
            await ctx.reply("Only an approved team captain can use this command.", mention_author=False)
            return
        if not users.strip():
            await ctx.reply("Usage: `+removeperms @user [@user2 ...]` or `+removeperms all`.", mention_author=False)
            return

        if users.strip().casefold() == "all":
            cursor = core.db.execute("DELETE FROM team_ping_perms WHERE team_id=?", (team["id"],))
            core.db.commit()
            await ctx.reply(f"✅ Removed `+pingteam` permission from **{cursor.rowcount}** player(s).",
                            mention_author=False)
            return

        removed, not_had, missing = [], [], []
        seen = set()
        for token in _split_tokens(users):
            match = ID_RE.fullmatch(token)
            if match:                                   # an ID works even if the player left the server
                user_id = int(match.group(1) or match.group(2))
            else:
                member = await core._get_member(ctx.guild, token)
                if member is None:
                    missing.append(token)
                    continue
                user_id = member.id
            if user_id in seen:
                continue
            seen.add(user_id)
            cursor = core.db.execute("DELETE FROM team_ping_perms WHERE team_id=? AND user_id=?",
                                     (team["id"], user_id))
            (removed if cursor.rowcount else not_had).append(self._name(user_id, ctx.guild))
        core.db.commit()

        lines = []
        if removed:
            lines.append("✅ **Permission removed:** " + ", ".join(removed))
        if not_had:
            lines.append("ℹ️ **Didn't have it:** " + ", ".join(not_had))
        if missing:
            lines.append("⚠️ **Couldn't find:** " + ", ".join(f"`{m}`" for m in missing))
        await ctx.reply("\n".join(lines) or "Nothing to do.", mention_author=False, allowed_mentions=NO_MENTIONS)

    # ------------------------------------------------------------------ +pingteam (captain / vice / +perms)
    @commands.command(name="pingteam", aliases=["pteam", "teamping"])
    @commands.guild_only()
    async def ping_team(self, ctx):
        """Ping your team's role: captain, vice-captain, or a player the captain gave +perms to."""
        core = await self._core(ctx)
        if core is None:
            return
        self._ensure_tables(core)
        uid = ctx.author.id
        team = core.db.execute(
            "SELECT t.* FROM registered_teams t WHERE t.guild_id=? AND t.status='approved' AND ("
            "t.captain_id=? OR t.vice_captain_id=? OR EXISTS ("
            "SELECT 1 FROM team_ping_perms p WHERE p.team_id=t.id AND p.user_id=?)) LIMIT 1",
            (ctx.guild.id, uid, uid, uid)).fetchone()
        if team is None:
            await ctx.reply("Only a team captain, the vice-captain, or a player the captain gave permission "
                            "(`+perms`) can use this command.", mention_author=False)
            return

        setting = core.db.execute("SELECT role_id FROM team_role_settings WHERE team_id=?", (team["id"],)).fetchone()
        if setting is None:
            await ctx.reply("Your team doesn't have a ping role configured. Ask the server owner to use `+setteamrole`.",
                            mention_author=False)
            return
        role = ctx.guild.get_role(int(setting["role_id"]))
        if role is None:
            await ctx.reply("Your team's configured role no longer exists. Ask the server owner to set it again.",
                            mention_author=False)
            return

        cooldown_row = core.db.execute("SELECT cooldown_seconds FROM team_ping_settings WHERE guild_id=?",
                                       (ctx.guild.id,)).fetchone()
        cooldown = int(cooldown_row["cooldown_seconds"]) if cooldown_row else 60
        usage = core.db.execute("SELECT last_ping_at FROM team_ping_usage WHERE team_id=?", (team["id"],)).fetchone()
        last_ping = float(usage["last_ping_at"]) if usage else 0.0
        remaining = cooldown - (datetime.now(timezone.utc).timestamp() - last_ping)
        if cooldown > 0 and remaining > 0:
            await ctx.reply(f"Your team can be pinged again in **{int(remaining) + 1} seconds**.", mention_author=False)
            return

        try:
            await ctx.send(
                f"{role.mention} — **{discord.utils.escape_markdown(team['name'])}** needs your attention!",
                allowed_mentions=discord.AllowedMentions(roles=[role], users=False, everyone=False))
        except (discord.Forbidden, discord.HTTPException) as exc:
            await ctx.reply(f"I couldn't send the team ping: `{type(exc).__name__}`. Check my channel permissions.",
                            mention_author=False)
            return

        core.db.execute(
            "INSERT INTO team_ping_usage(team_id,last_ping_at) VALUES(?,?) "
            "ON CONFLICT(team_id) DO UPDATE SET last_ping_at=excluded.last_ping_at",
            (team["id"], datetime.now(timezone.utc).timestamp()))
        core.db.commit()

    # ------------------------------------------------------------------ +teamchat (captain / vice)
    @commands.command(name="teamchat")
    @commands.guild_only()
    async def team_chat(self, ctx, *, message: str = ""):
        """DM a message and any attached images/files to your team (captain or vice-captain)."""
        core = await self._core(ctx)
        if core is None:
            return
        team = core._membership(ctx.guild.id, ctx.author.id)
        leaders = set()
        if team is not None:
            leaders.add(int(team["captain_id"]))
            if team["vice_captain_id"] is not None:
                leaders.add(int(team["vice_captain_id"]))
        if team is None or ctx.author.id not in leaders:
            await ctx.reply("Only a team captain or vice-captain can use `+teamchat` for their own team.",
                            mention_author=False)
            return
        attachments = list(ctx.message.attachments)
        if not message.strip() and not attachments:
            await ctx.reply("Usage: `+teamchat <message>` and optionally attach an image/file.", mention_author=False)
            return

        player_rows = core.db.execute(
            "SELECT user_id FROM team_players WHERE team_id=? ORDER BY added_at", (team["id"],)).fetchall()
        recipient_ids = list(dict.fromkeys(
            [int(team["captain_id"])]
            + ([int(team["vice_captain_id"])] if team["vice_captain_id"] is not None else [])
            + [int(row["user_id"]) for row in player_rows]))
        sent = failed = 0
        for user_id in recipient_ids:
            try:
                recipient = ctx.guild.get_member(user_id) or self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
                files = [await attachment.to_file() for attachment in attachments]
                await recipient.send(
                    content=f"**Team chat • {team['name']}**\n{message}".strip() if message.strip()
                    else f"**Team chat • {team['name']}**",
                    files=files)
                sent += 1
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                failed += 1
        await ctx.reply(f"Team chat sent to **{sent}** team member(s)."
                        + (f" Could not DM **{failed}** member(s)." if failed else ""), mention_author=False)

    # ------------------------------------------------------------------ +vc / +setvc
    @commands.command(name="vc", aliases=["setvc"])
    @commands.guild_only()
    async def set_vice_captain(self, ctx, target: str, *, team_name: str = None):
        """Set a team's vice-captain. +setvc @user (team found automatically) or +vc @user Team Name."""
        core = await self._core(ctx)
        if core is None:
            return
        member = await core._get_member(ctx.guild, target)
        if member is None:
            await ctx.reply("I couldn't find that server member. Use a mention, username, or ID.", mention_author=False)
            return

        if team_name and team_name.strip():
            team = core._team(ctx.guild.id, team_name)
            if not team or team["status"] != "approved":
                await ctx.reply("I couldn't find an approved team with that name.", mention_author=False)
                return
        else:
            team = self._captain_team(core, ctx.guild.id, ctx.author.id)
            if team is None:
                if not await self._is_admin(ctx):
                    await ctx.reply("Only a team captain can use this command.", mention_author=False)
                    return
                team = core._membership(ctx.guild.id, member.id)
                if team is None:
                    await ctx.reply(
                        f"**{discord.utils.escape_markdown(member.name)}** isn't on any team yet, so I can't tell "
                        "which team to use. Use `+vc <user> <team name>`.", mention_author=False)
                    return

        if ctx.author.id != int(team["captain_id"]) and not await self._is_admin(ctx):
            await ctx.reply("Only the team captain (or the bot owner / a whitelisted role) can set this team's "
                            "vice-captain.", mention_author=False)
            return

        if member.id == int(team["captain_id"]):
            await ctx.reply("The captain cannot also be the vice-captain.", mention_author=False)
            return
        current = core._membership(ctx.guild.id, member.id)
        if current and current["id"] != team["id"]:
            await ctx.reply(f"{member.name} is already in **{current['name']}**.", mention_author=False)
            return
        if member.id not in core._team_player_ids(team["id"]) and \
                core._team_player_count(team["id"]) >= core._max_players(ctx.guild.id):
            await ctx.reply(f"**{team['name']}** has reached its maximum roster size.", mention_author=False)
            return

        core.db.execute("UPDATE registered_teams SET vice_captain_id=? WHERE id=?", (member.id, team["id"]))
        core.db.execute(
            "INSERT OR IGNORE INTO team_players(team_id,user_id,added_by,added_at) VALUES(?,?,?,?)",
            (team["id"], member.id, ctx.author.id, datetime.now(timezone.utc).isoformat()))
        core.db.commit()
        await core._sync_base_role_member(ctx.guild, member)
        await ctx.reply(f"{member.name} is now vice-captain of **{team['name']}**.", mention_author=False)
        await core._dm(member, f"You have been appointed vice-captain of **{team['name']}**.")


async def setup(bot):
    # Replace the old versions (teams2.py / teams3.py) so the names are free for this cog.
    for name in REPLACED_COMMANDS:
        bot.remove_command(name)
    await bot.add_cog(TeamLeaders(bot))
