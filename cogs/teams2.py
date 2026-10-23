import asyncio
import re
import sqlite3
from datetime import datetime, timezone
from urllib.parse import urlparse

import discord
from discord.ext import commands

from .teams1 import TeamCore, ConfirmDeleteAllView, TeamApprovalView, TeamListView, JoinRequestView, LeaveTeamConfirmView, CAPTAIN_EMOJI, VICE_CAPTAIN_EMOJI

class TeamCommandsA:
    """Command mixin; methods are registered by TeamRegistration in teams1.py."""

    def _ensure_ping_tables(self):
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS team_ping_settings ("
            "guild_id INTEGER PRIMARY KEY, cooldown_seconds INTEGER NOT NULL DEFAULT 60)"
        )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS team_ping_usage ("
            "team_id INTEGER PRIMARY KEY, last_ping_at REAL NOT NULL DEFAULT 0, "
            "FOREIGN KEY(team_id) REFERENCES registered_teams(id) ON DELETE CASCADE)"
        )
        self.db.commit()

    @commands.command(name="pingteam", aliases=["pteam", "teamping"])
    @commands.guild_only()
    async def ping_team(self, ctx):
        """Let an approved team captain ping their configured team role."""
        self._ensure_ping_tables()
        team = self.db.execute(
            "SELECT * FROM registered_teams WHERE guild_id=? AND status='approved' AND captain_id=? LIMIT 1",
            (ctx.guild.id, ctx.author.id),
        ).fetchone()
        if team is None:
            await ctx.reply("Only an approved team captain can use this command.", mention_author=False)
            return

        setting = self.db.execute(
            "SELECT role_id FROM team_role_settings WHERE team_id=?", (team["id"],)
        ).fetchone()
        if setting is None:
            await ctx.reply("Your team doesn't have a ping role configured. Ask the server owner to use `+setteamrole`.", mention_author=False)
            return
        role = ctx.guild.get_role(int(setting["role_id"]))
        if role is None:
            await ctx.reply("Your team's configured role no longer exists. Ask the server owner to set it again.", mention_author=False)
            return

        cooldown_row = self.db.execute(
            "SELECT cooldown_seconds FROM team_ping_settings WHERE guild_id=?", (ctx.guild.id,)
        ).fetchone()
        cooldown = int(cooldown_row["cooldown_seconds"]) if cooldown_row else 60
        usage = self.db.execute(
            "SELECT last_ping_at FROM team_ping_usage WHERE team_id=?", (team["id"],)
        ).fetchone()
        last_ping = float(usage["last_ping_at"]) if usage else 0.0
        remaining = cooldown - (datetime.now(timezone.utc).timestamp() - last_ping)
        if cooldown > 0 and remaining > 0:
            await ctx.reply(
                f"Your team can be pinged again in **{int(remaining) + 1} seconds**.",
                mention_author=False,
            )
            return

        try:
            await ctx.send(
                f"{role.mention} — **{discord.utils.escape_markdown(team['name'])}** needs your attention!",
                allowed_mentions=discord.AllowedMentions(roles=[role], users=False, everyone=False),
            )
        except (discord.Forbidden, discord.HTTPException) as exc:
            await ctx.reply(f"I couldn't send the team ping: `{type(exc).__name__}`. Check my channel permissions.", mention_author=False)
            return

        self.db.execute(
            "INSERT INTO team_ping_usage(team_id,last_ping_at) VALUES(?,?) "
            "ON CONFLICT(team_id) DO UPDATE SET last_ping_at=excluded.last_ping_at",
            (team["id"], datetime.now(timezone.utc).timestamp()),
        )
        self.db.commit()

    @commands.command(name="setpingcooldown")
    @commands.guild_only()
    async def set_ping_cooldown(self, ctx, seconds: int):
        """Configure the per-team ping cooldown; server owner only."""
        if ctx.author.id not in (ctx.guild.owner_id, 1014568928682459198):
            await ctx.reply("Only the server owner or bot owner can use this command.", mention_author=False)
            return
        if seconds < 0 or seconds > 86400:
            await ctx.reply("Choose a cooldown from **0 to 86400 seconds**. Use `0` to disable it.", mention_author=False)
            return
        self._ensure_ping_tables()
        self.db.execute(
            "INSERT INTO team_ping_settings(guild_id,cooldown_seconds) VALUES(?,?) "
            "ON CONFLICT(guild_id) DO UPDATE SET cooldown_seconds=excluded.cooldown_seconds",
            (ctx.guild.id, seconds),
        )
        self.db.commit()
        await ctx.reply(
            f"Team ping cooldown set to **{seconds} seconds**" + (" (disabled)." if seconds == 0 else "."),
            mention_author=False,
        )

    @commands.command(name="setbaserole")
    @commands.guild_only()
    async def set_base_role(self, ctx, role_id: str):
        if not await self._command_allowed(ctx, "setbaserole"):
            await ctx.reply("You are not allowed to use this command.", mention_author=False)
            return
        match = re.fullmatch(r"<@&?(\d+)>|([0-9]+)", role_id.strip())
        if not match:
            await ctx.reply("Usage: `+setbaserole ROLE_ID` (a role mention also works).", mention_author=False)
            return
        rid = int(match.group(1) or match.group(2))
        role = ctx.guild.get_role(rid)
        if role is None:
            await ctx.reply("That role ID doesn't exist in this server.", mention_author=False)
            return
        me = ctx.guild.me
        if not me or not me.guild_permissions.manage_roles or role >= me.top_role:
            await ctx.reply("I can't manage that role. Give me Manage Roles and move my highest role above it.", mention_author=False)
            return
        self.db.execute(
            "INSERT INTO team_base_role_settings(guild_id, role_id) VALUES(?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET role_id=excluded.role_id",
            (ctx.guild.id, role.id),
        )
        self.db.commit()
        await ctx.reply(f"Base role set to **{discord.utils.escape_markdown(role.name)}** (`{role.id}`). Use `+syncbaserole` to synchronise existing members.", mention_author=False)

    @commands.command(name="syncbaserole")
    @commands.guild_only()
    async def sync_base_role(self, ctx):
        if not await self._command_allowed(ctx, "syncbaserole"):
            await ctx.reply("You are not allowed to use this command.", mention_author=False)
            return
        setting = self.db.execute(
            "SELECT role_id FROM team_base_role_settings WHERE guild_id=?", (ctx.guild.id,)
        ).fetchone()
        if not setting:
            await ctx.reply("No base role is configured. Use `+setbaserole ROLE_ID` first.", mention_author=False)
            return
        role = ctx.guild.get_role(int(setting["role_id"]))
        if role is None:
            await ctx.reply("The configured base role no longer exists. Set it again with `+setbaserole ROLE_ID`.", mention_author=False)
            return

        # Reply immediately so the command never appears to do nothing while the member list loads.
        status_message = await ctx.reply("Base-role sync started. Loading server members…", mention_author=False)
        try:
            members = {m.id: m for m in ctx.guild.members}
            fetch_note = None
            try:
                async for member in ctx.guild.fetch_members(limit=None):
                    members[member.id] = member
            except (discord.Forbidden, discord.HTTPException) as exc:
                fetch_note = f"Could not fetch the full member list ({type(exc).__name__}); synced cached and known team members."

            # Include stored team users even if they weren't in the member cache.
            rows = self.db.execute(
                "SELECT DISTINCT user_id FROM ("
                "SELECT t.captain_id AS user_id FROM registered_teams t WHERE t.guild_id=? AND t.status='approved' "
                "UNION SELECT t.vice_captain_id FROM registered_teams t WHERE t.guild_id=? AND t.status='approved' "
                "UNION SELECT p.user_id FROM team_players p JOIN registered_teams t ON t.id=p.team_id "
                "WHERE t.guild_id=? AND t.status='approved')",
                (ctx.guild.id, ctx.guild.id, ctx.guild.id),
            ).fetchall()
            for row in rows:
                # Captains/vice-captains can be NULL in the database; skip them.
                if row["user_id"] is None:
                    continue
                uid = int(row["user_id"])
                if uid not in members:
                    try:
                        members[uid] = await ctx.guild.fetch_member(uid)
                    except discord.NotFound:
                        continue
                    except (discord.Forbidden, discord.HTTPException):
                        continue

            counts = {"added": 0, "removed": 0, "already_correct": 0}
            failures = []
            for member in members.values():
                try:
                    result = await self._sync_base_role_member(ctx.guild, member)
                except Exception as exc:
                    result = f"unexpected_error_{type(exc).__name__}"
                if result in counts:
                    counts[result] += 1
                elif result != "not_configured":
                    failures.append(f"{discord.utils.escape_markdown(str(member))} (`{member.id}`): {result.replace('_', ' ')}")

            embed = discord.Embed(title="Base Role Sync Complete", color=discord.Color.from_rgb(55, 150, 255))
            embed.add_field(name="Members checked", value=str(len(members)), inline=True)
            embed.add_field(name="Roles added", value=str(counts["added"]), inline=True)
            embed.add_field(name="Roles removed", value=str(counts["removed"]), inline=True)
            embed.add_field(name="Already correct", value=str(counts["already_correct"]), inline=True)
            embed.add_field(name=f"Failures ({len(failures)})", value="\n".join(failures[:15]) if failures else "None", inline=False)
            if len(failures) > 15:
                embed.set_footer(text=f"{len(failures) - 15} additional failures omitted.")
            if fetch_note:
                embed.add_field(name="Member-list note", value=fetch_note, inline=False)
            await status_message.edit(content=None, embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except Exception as exc:
            await status_message.edit(
                content=f"Base-role sync failed: `{type(exc).__name__}: {discord.utils.escape_markdown(str(exc))[:500]}`. Check the bot console for details.",
                embed=None,
                allowed_mentions=discord.AllowedMentions.none(),
            )

    @commands.command(name="teamlist")
    @commands.guild_only()
    async def team_list(self, ctx):
        total = self.db.execute(
            "SELECT COUNT(*) FROM registered_teams WHERE guild_id=? AND status='approved'", (ctx.guild.id,)
        ).fetchone()[0]
        await ctx.send(embed=self.make_teamlist_embed(ctx.guild.id, 0),
                       view=TeamListView(self, ctx.guild.id) if total > 10 else None)

    @commands.command(name="setteamname")
    @commands.guild_only()
    async def set_team_name(self, ctx, *, names: str):
        """Rename an approved team. Team names may contain spaces."""
        if not await self._command_allowed(ctx, "setteamname"):
            await ctx.reply("You are not allowed to use this command.", mention_author=False)
            return

        raw = " ".join(names.strip().split())
        approved_teams = self.db.execute(
            "SELECT * FROM registered_teams WHERE guild_id=? AND status='approved' ORDER BY LENGTH(name) DESC",
            (ctx.guild.id,),
        ).fetchall()

        # Match the longest approved team-name prefix, allowing both old and
        # new team names to contain spaces in: +setteamname Current Name New Name
        team = None
        new_name = None
        raw_folded = raw.casefold()
        for candidate in approved_teams:
            old_name = " ".join(str(candidate["name"]).split())
            if raw_folded.startswith(old_name.casefold() + " "):
                proposed = raw[len(old_name):].strip()
                if proposed:
                    team = candidate
                    new_name = proposed
                    break

        if team is None or not new_name:
            await ctx.reply(
                "Usage: `+setteamname <current team name> <new team name>\n"
                "Example: `+setteamname Dark Spectors Dark Specters`",
                mention_author=False,
            )
            return
        if len(new_name) > 50:
            await ctx.reply("Team names must be between 1 and 50 characters.", mention_author=False)
            return
        new_key = self._key(new_name)
        if new_key == team["name_key"]:
            await ctx.reply("The new team name is the same as the current name.", mention_author=False)
            return
        existing = self._team(ctx.guild.id, new_name)
        if existing and int(existing["id"]) != int(team["id"]):
            await ctx.reply("A team with that name already exists or is pending approval.", mention_author=False)
            return
        try:
            self.db.execute(
                "UPDATE registered_teams SET name=?, name_key=? WHERE id=? AND guild_id=? AND status='approved'",
                (new_name, new_key, int(team["id"]), ctx.guild.id),
            )
            self.db.commit()
        except sqlite3.IntegrityError:
            self.db.rollback()
            await ctx.reply("A team with that name already exists or is pending approval.", mention_author=False)
            return

        old_name = team["name"]
        await ctx.reply(
            f"Renamed **{discord.utils.escape_markdown(old_name)}** to **{discord.utils.escape_markdown(new_name)}**.",
            mention_author=False,
        )
        await self._dm_captain(
            ctx.guild, team,
            f"An administrator changed your team's name from **{old_name}** to **{new_name}**."
        )

    @commands.command(name="setmaxplayers")
    @commands.guild_only()
    async def set_max_players(self, ctx, maximum: int):
        if not await self._command_allowed(ctx, "setmaxplayers"):
            await ctx.reply("You are not allowed to use this command.", mention_author=False)
            return
        if not 1 <= maximum <= 100:
            await ctx.reply("Choose a maximum between 1 and 100 players per team.", mention_author=False)
            return
        self.db.execute(
            "INSERT INTO team_limits(guild_id,max_players) VALUES(?,?) "
            "ON CONFLICT(guild_id) DO UPDATE SET max_players=excluded.max_players",
            (ctx.guild.id, maximum)
        )
        self.db.commit()
        await ctx.reply(
            f"Maximum roster size set to **{maximum} players per team**. Existing oversized rosters won't be deleted, but no further players can be added to a full team.",
            mention_author=False
        )

    @commands.command(name="setcaptain")
    @commands.guild_only()
    async def set_captain(self, ctx, target: str, *, team_name: str):
        if not await self._command_allowed(ctx, "setcaptain"):
            await ctx.reply("You are not allowed to use this command.", mention_author=False)
            return
        team = self._team(ctx.guild.id, team_name)
        if not team or team["status"] != "approved":
            await ctx.reply("I couldn't find an approved team with that name.", mention_author=False)
            return
        member = await self._get_member(ctx.guild, target)
        if member is None:
            await ctx.reply("I couldn't find that server member. Use a mention, username, or ID.", mention_author=False)
            return
        if member.id == team["captain_id"]:
            await ctx.reply(f"{member.name} is already the captain of **{team['name']}**.", mention_author=False)
            return
        current = self._membership(ctx.guild.id, member.id)
        if current and current["id"] != team["id"]:
            await ctx.reply(f"{member.name} is already in **{current['name']}**.", mention_author=False)
            return
        if member.id not in self._team_player_ids(team["id"]) and self._team_player_count(team["id"]) >= self._max_players(ctx.guild.id):
            await ctx.reply(f"**{team['name']}** has reached its roster limit. Remove a player before appointing someone new.", mention_author=False)
            return
        old_captain = team["captain_id"]
        self.db.execute(
            "INSERT OR IGNORE INTO team_players(team_id,user_id,added_by,added_at) VALUES(?,?,?,?)",
            (team["id"], old_captain, ctx.author.id, datetime.now(timezone.utc).isoformat())
        )
        self.db.execute("DELETE FROM team_players WHERE team_id=? AND user_id=?", (team["id"], member.id))
        self.db.execute("UPDATE registered_teams SET captain_id=? WHERE id=?", (member.id, team["id"]))
        self.db.commit()
        await self._sync_base_role_member(ctx.guild, member)
        await ctx.reply(f"**{member.name}** is now captain of **{team['name']}**. The previous captain remains on the roster.", mention_author=False)
        await self._dm(member, f"You have been appointed captain of **{team['name']}** by a server administrator.")
        try:
            old_captain_user = ctx.guild.get_member(int(old_captain)) or self.bot.get_user(int(old_captain)) or await self.bot.fetch_user(int(old_captain))
            await self._dm(old_captain_user, f"You are no longer the captain of **{team['name']}**. An administrator changed the team captain.")
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass

    @commands.command(name="setteamrole", aliases=["setteamroles"])
    @commands.guild_only()
    async def set_team_role(self, ctx, *, arguments: str):
        if not await self._command_allowed(ctx, "setteamrole"):
            await ctx.reply("You are not allowed to use this command.", mention_author=False)
            return
        parts = arguments.rsplit(maxsplit=1)
        if len(parts) != 2 or not parts[1].isdigit():
            await ctx.reply("Usage: `+setteamrole Team Name ROLE_ID`", mention_author=False)
            return
        team_name, role_id_text = parts
        team = self._team(ctx.guild.id, team_name)
        if not team or team["status"] != "approved":
            await ctx.reply("I couldn't find an approved team with that name.", mention_author=False)
            return
        role = ctx.guild.get_role(int(role_id_text))
        if role is None:
            await ctx.reply("That role ID doesn't exist in this server.", mention_author=False)
            return
        self.db.execute(
            "INSERT INTO team_role_settings(team_id,role_id,enabled) VALUES(?,?,1) "
            "ON CONFLICT(team_id) DO UPDATE SET role_id=excluded.role_id,enabled=1",
            (team["id"], role.id)
        )
        self.db.commit()
        # Never use role.mention here; suppress all mentions for this confirmation.
        safe_role_name = discord.utils.escape_markdown(role.name)
        await ctx.reply(
            f"Automatic role assignment for **{team['name']}** is configured to use **{safe_role_name}** (ID: `{role.id}`). "
            "It applies to approved `+jointeam` requests and administrator `+addplayer` additions, not `+bulkadd`.",
            mention_author=False,
            allowed_mentions=discord.AllowedMentions.none()
        )

    @commands.command(name="teamroles", aliases=["approvedteamroles", "listteamroles"])
    @commands.guild_only()
    async def list_approved_team_roles(self, ctx):
        """List configured roles for all approved teams, showing role IDs without pinging."""
        if not await self._command_allowed(ctx, "teamroles"):
            await ctx.reply("You are not allowed to use this command.", mention_author=False)
            return
        teams = self.db.execute(
            "SELECT * FROM registered_teams WHERE guild_id=? AND status='approved' ORDER BY name COLLATE NOCASE",
            (ctx.guild.id,),
        ).fetchall()
        if not teams:
            await ctx.reply("There are no approved teams in this server.", mention_author=False)
            return

        lines = []
        for team in teams:
            setting = self.db.execute(
                "SELECT role_id, enabled FROM team_role_settings WHERE team_id=?", (team["id"],)
            ).fetchone()
            if not setting:
                role_text = "Not configured"
            else:
                role = ctx.guild.get_role(setting["role_id"])
                if role is None:
                    role_text = f"Role not found (`{setting['role_id']}`)"
                else:
                    safe_name = discord.utils.escape_markdown(role.name).replace("@", "＠")
                    state = "enabled" if setting["enabled"] else "disabled"
                    role_text = f"{safe_name} (`{role.id}`) — {state}"
            lines.append(f"**{discord.utils.escape_markdown(team['name'])}** — {role_text}")

        # Split across multiple embeds/messages to stay within Discord's limits.
        chunks, current = [], []
        length = 0
        for line in lines:
            if length + len(line) + 1 > 3800:
                chunks.append("\n".join(current))
                current, length = [], 0
            current.append(line)
            length += len(line) + 1
        if current:
            chunks.append("\n".join(current))
        for index, chunk in enumerate(chunks, start=1):
            embed = discord.Embed(
                title="Approved Team Roles" if len(chunks) == 1 else f"Approved Team Roles ({index}/{len(chunks)})",
                description=chunk,
                color=discord.Color.from_rgb(55, 150, 255),
            )
            await ctx.reply(embed=embed, mention_author=False, allowed_mentions=discord.AllowedMentions.none())

    @commands.command(name="stopteamroles")
    @commands.guild_only()
    async def stop_team_roles(self, ctx, *, team_name: str):
        if not await self._command_allowed(ctx, "stopteamroles"):
            await ctx.reply("You are not allowed to use this command.", mention_author=False)
            return
        team = self._team(ctx.guild.id, team_name)
        if not team or team["status"] != "approved":
            await ctx.reply("I couldn't find an approved team.", mention_author=False)
            return
        row = self.db.execute("SELECT role_id FROM team_role_settings WHERE team_id=?", (team["id"],)).fetchone()
        if not row:
            await ctx.reply("No team role has been configured. Use `+setteamrole` first.", mention_author=False)
            return
        self.db.execute("UPDATE team_role_settings SET enabled=0 WHERE team_id=?", (team["id"],))
        self.db.commit()
        await ctx.reply(f"Automatic role assignment is disabled for **{team['name']}**.", mention_author=False)

    @commands.command(name="startteamroles")
    @commands.guild_only()
    async def start_team_roles(self, ctx, *, team_name: str):
        if not await self._command_allowed(ctx, "startteamroles"):
            await ctx.reply("You are not allowed to use this command.", mention_author=False)
            return
        team = self._team(ctx.guild.id, team_name)
        if not team or team["status"] != "approved":
            await ctx.reply("I couldn't find an approved team.", mention_author=False)
            return
        row = self.db.execute("SELECT role_id FROM team_role_settings WHERE team_id=?", (team["id"],)).fetchone()
        if not row:
            await ctx.reply("No team role has been configured. Use `+setteamrole` first.", mention_author=False)
            return
        self.db.execute("UPDATE team_role_settings SET enabled=1 WHERE team_id=?", (team["id"],))
        self.db.commit()
        await ctx.reply(f"Automatic role assignment is enabled for **{team['name']}**.", mention_author=False)

    @commands.command(name="stopallteamroles")
    @commands.guild_only()
    async def stop_all_team_roles(self, ctx):
        if not await self._command_allowed(ctx, "stopallteamroles"):
            await ctx.reply("You are not allowed to use this command.", mention_author=False)
            return
        self.db.execute(
            "INSERT INTO server_team_role_settings(guild_id,enabled) VALUES(?,0) "
            "ON CONFLICT(guild_id) DO UPDATE SET enabled=0", (ctx.guild.id,)
        )
        self.db.commit()
        await ctx.reply("Automatic team-role assignment is now disabled server-wide.", mention_author=False)

    @commands.command(name="startallteamroles")
    @commands.guild_only()
    async def start_all_team_roles(self, ctx):
        if not await self._command_allowed(ctx, "startallteamroles"):
            await ctx.reply("You are not allowed to use this command.", mention_author=False)
            return
        self.db.execute(
            "INSERT INTO server_team_role_settings(guild_id,enabled) VALUES(?,1) "
            "ON CONFLICT(guild_id) DO UPDATE SET enabled=1", (ctx.guild.id,)
        )
        self.db.commit()
        await ctx.reply("Server-wide automatic team-role assignment is enabled. Team-specific settings still apply.", mention_author=False)

    @commands.command(name="syncteamroles", aliases=["syncteamrole", "syncroles"])
    @commands.guild_only()
    async def sync_team_roles(self, ctx):
        """Give existing team members their configured team roles when missing."""
        if not await self._command_allowed(ctx, "syncteamroles"):
            await ctx.reply("You are not allowed to use this command.", mention_author=False)
            return
        guild = ctx.guild
        global_setting = self.db.execute(
            "SELECT enabled FROM server_team_role_settings WHERE guild_id=?", (guild.id,)
        ).fetchone()
        if global_setting and not global_setting["enabled"]:
            await ctx.reply(
                "Server-wide automatic team roles are still disabled. Run `+startallteamroles` first, then retry `+syncteamroles`.",
                mention_author=False,
            )
            return

        teams = self.db.execute(
            "SELECT * FROM registered_teams WHERE guild_id=? AND status='approved' ORDER BY name COLLATE NOCASE",
            (guild.id,),
        ).fetchall()
        if not teams:
            await ctx.reply("There are no approved teams to synchronise.", mention_author=False)
            return

        added, already, failed, skipped = [], [], [], []
        for team in teams:
            setting = self.db.execute(
                "SELECT role_id, enabled FROM team_role_settings WHERE team_id=?", (team["id"],)
            ).fetchone()
            if not setting:
                skipped.append(f"{team['name']} — no team role configured")
                continue
            if not setting["enabled"]:
                skipped.append(f"{team['name']} — team role assignment is disabled")
                continue
            role = guild.get_role(setting["role_id"])
            if role is None:
                skipped.append(f"{team['name']} — configured role no longer exists")
                continue

            me = guild.me
            if not me or not me.guild_permissions.manage_roles or role >= me.top_role:
                failed.append(f"{team['name']} — bot cannot assign role **{discord.utils.escape_markdown(role.name)}** (check Manage Roles and role hierarchy)")
                continue

            user_ids = self._team_player_ids(team["id"])
            for user_id in sorted(user_ids):
                member = guild.get_member(user_id)
                if member is None:
                    try:
                        member = await guild.fetch_member(user_id)
                    except discord.NotFound:
                        skipped.append(f"{user_id} — member not found (team: {team['name']})")
                        continue
                    except discord.Forbidden:
                        failed.append(f"{user_id} — bot lacks permission to fetch member (team: {team['name']})")
                        continue
                    except discord.HTTPException as exc:
                        failed.append(f"{user_id} — fetch error {type(exc).__name__} (team: {team['name']})")
                        continue

                if role in member.roles:
                    already.append(f"{member.name} — {team['name']}")
                    continue
                try:
                    await member.add_roles(role, reason=f"Team-role sync requested by {ctx.author}")
                    added.append(f"{member.name} — {team['name']}")
                except discord.Forbidden:
                    failed.append(f"{member.name} — permission denied assigning {role.name} (team: {team['name']})")
                except discord.HTTPException as exc:
                    failed.append(f"{member.name} — role assignment error {type(exc).__name__} (team: {team['name']})")

        # Remove configured team roles from members who are not on the corresponding team.
        # This also cleans up stale roles left behind by older/manual role assignments.
        removed, remove_failed = [], []
        for team in teams:
            setting = self.db.execute(
                "SELECT role_id FROM team_role_settings WHERE team_id=?", (team["id"],)
            ).fetchone()
            if not setting:
                continue
            role = guild.get_role(setting["role_id"])
            if role is None:
                continue
            me = guild.me
            if not me or not me.guild_permissions.manage_roles or role >= me.top_role:
                remove_failed.append(f"{team['name']} — bot cannot remove role **{discord.utils.escape_markdown(role.name)}** (check Manage Roles and role hierarchy)")
                continue

            current_team_ids = self._team_player_ids(team["id"])
            for member in list(guild.members):
                if role not in member.roles or member.id in current_team_ids:
                    continue
                try:
                    await member.remove_roles(role, reason=f"Team-role sync: member is not on {team['name']}")
                    removed.append(f"{member.name} — removed stale role for {team['name']}")
                except discord.Forbidden:
                    remove_failed.append(f"{member.name} — permission denied removing role for {team['name']}")
                except discord.HTTPException as exc:
                    remove_failed.append(f"{member.name} — removal error {type(exc).__name__} (team: {team['name']})")

        failed.extend(remove_failed)

        def format_sync_list(items):
            if not items:
                return "None"
            # Keep each embed field under Discord's 1024-character field-value limit.
            lines, length = [], 0
            for item in items:
                line = f"• {discord.utils.escape_markdown(str(item))}"
                if length + len(line) + 1 > 1000:
                    lines.append(f"…and {len(items) - len(lines)} more")
                    break
                lines.append(line)
                length += len(line) + 1
            return "\n".join(lines) or "None"

        embed = discord.Embed(
            title="Team role synchronisation complete",
            description="Team roles were synchronised: missing roles were added to team members, and configured team roles were removed from members who are not on the corresponding team. Team membership was not changed.",
            color=discord.Color.from_rgb(55, 150, 255),
        )
        embed.add_field(name=f"Roles added ({len(added)})", value=format_sync_list(added), inline=False)
        embed.add_field(name=f"Stale roles removed ({len(removed)})", value=format_sync_list(removed), inline=False)
        embed.add_field(name=f"Already synced ({len(already)})", value=format_sync_list(already), inline=False)
        embed.add_field(name=f"Failed ({len(failed)})", value=format_sync_list(failed), inline=False)
        embed.add_field(name=f"Skipped ({len(skipped)})", value=format_sync_list(skipped), inline=False)
        await ctx.reply(embed=embed, mention_author=False, allowed_mentions=discord.AllowedMentions.none())

    @commands.command(name="toggle")
    @commands.guild_only()
    async def toggle_feature(self, ctx, feature: str):
        if not await self._command_allowed(ctx, "toggle"):
            await ctx.reply("You are not allowed to use this command.", mention_author=False)
            return
        feature = feature.casefold()
        if feature not in ("jointeam", "leaveteam", "createteam"):
            await ctx.reply("Usage: `+toggle jointeam`, `+toggle leaveteam`, or `+toggle createteam`.", mention_author=False)
            return
        value = 0 if self._feature_enabled(ctx.guild.id, feature) else 1
        self.db.execute(
            "INSERT INTO team_feature_toggles(guild_id,feature,enabled) VALUES(?,?,?) "
            "ON CONFLICT(guild_id,feature) DO UPDATE SET enabled=excluded.enabled",
            (ctx.guild.id, feature, value)
        )
        self.db.commit()
        await ctx.reply(f"`{feature}` has been **{'enabled' if value else 'disabled'}** by an administrator.", mention_author=False)

    @commands.command(name="deleteallteams")
    @commands.guild_only()
    async def delete_all_teams(self, ctx):
        if not await self._command_allowed(ctx, "deleteallteams"):
            await ctx.reply("You are not allowed to use this command.", mention_author=False)
            return
        count = self.db.execute("SELECT COUNT(*) FROM registered_teams WHERE guild_id=?", (ctx.guild.id,)).fetchone()[0]
        if not count:
            await ctx.reply("There are no teams to delete.", mention_author=False)
            return
        await ctx.reply(
            f"**Warning:** This will permanently delete all **{count} team(s)** and their rosters and join requests from this server's database. Other servers will not be affected. Confirm below:",
            view=ConfirmDeleteAllView(self, ctx.guild.id, ctx.author.id), mention_author=False
        )

