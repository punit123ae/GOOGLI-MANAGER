"""Live team draws for the Kashmir Titans Discord bot.

Load this extension with: await bot.load_extension("draws")
It reads approved teams from the same team_registration.db file used by teams.py.
Live-draw settings are saved separately in livedraw_settings.db.
"""

import asyncio
import random
import sqlite3
from pathlib import Path

import discord
from discord.ext import commands

from .utility import owner_or_whitelisted

OWNER_ID = 1014568928682459198
TICK = "✅"
CROSS = "❌"
TEAM_DB = Path("team_registration.db")
SETTINGS_DB = Path("livedraw_settings.db")


class LiveDraws(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.db = sqlite3.connect(str(SETTINGS_DB))
        self.db.row_factory = sqlite3.Row
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS live_draw_settings (
                guild_id INTEGER NOT NULL,
                setting_key TEXT NOT NULL,
                setting_name TEXT NOT NULL,
                group_count INTEGER NOT NULL,
                team_names_json TEXT NOT NULL,
                created_by INTEGER NOT NULL,
                PRIMARY KEY (guild_id, setting_key)
            )"""
        )
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS secret_draw_groups (
                guild_id INTEGER NOT NULL,
                setting_key TEXT NOT NULL,
                team_key TEXT NOT NULL,
                team_name TEXT NOT NULL,
                group_index INTEGER NOT NULL,
                assigned_by INTEGER NOT NULL,
                PRIMARY KEY (guild_id, setting_key, team_key)
            )"""
        )
        self.db.commit()
        self.active_guilds = set()

    def cog_unload(self):
        self.db.close()

    @staticmethod
    def _setting_key(name):
        return " ".join(name.casefold().split())

    def _approved_teams(self, guild_id):
        if not TEAM_DB.exists():
            raise FileNotFoundError(
                f"I couldn't find `{TEAM_DB}` in the bot's current working directory. "
                "Make sure draws.py is running from the same directory as the existing bot."
            )
        conn = sqlite3.connect(str(TEAM_DB))
        conn.row_factory = sqlite3.Row
        try:
            # Match the existing TeamRegistration database schema exactly.
            rows = conn.execute(
                "SELECT name FROM registered_teams "
                "WHERE guild_id=? AND status='approved' ORDER BY name COLLATE NOCASE",
                (guild_id,),
            ).fetchall()
            return [row["name"] for row in rows]
        finally:
            conn.close()

    async def _wait_for_reply(self, ctx, timeout=180):
        def check(message):
            return (
                message.author.id == ctx.author.id
                and message.channel.id == ctx.channel.id
                and message.guild is not None
                and message.guild.id == ctx.guild.id
            )

        return await self.bot.wait_for("message", check=check, timeout=timeout)

    async def _send_team_list(self, ctx, names):
        lines = [f"{i}. {discord.utils.escape_markdown(name)}" for i, name in enumerate(names, 1)]
        chunks = []
        current = ""
        for line in lines:
            addition = line + "\n"
            if len(current) + len(addition) > 1850:
                chunks.append(current.rstrip())
                current = ""
            current += addition
        if current:
            chunks.append(current.rstrip())
        for index, chunk in enumerate(chunks, 1):
            heading = f"**Detected approved teams ({len(names)} total)**" if index == 1 else f"**Team list — continued ({index}/{len(chunks)})**"
            await ctx.send(f"{heading}\n{chunk}")

    @commands.command(name="setuplivedraws")
    @commands.guild_only()
    async def setup_live_draws(self, ctx):
        """Interactively save a live-draw setting for this server."""
        if not await owner_or_whitelisted(ctx):
            await ctx.send("You are not authorized to use this command.")
            return

        try:
            teams = self._approved_teams(ctx.guild.id)
        except (sqlite3.Error, FileNotFoundError) as exc:
            await ctx.send(f"Couldn't read the team database: {exc}")
            return

        if not teams:
            await ctx.send("There are no approved teams in this server's team database.")
            return

        await ctx.send("**1/3 — Verify teams**\nI detected the approved teams below. Is this list correct? Reply `yes` to continue or `no` to cancel.")
        await self._send_team_list(ctx, teams)
        try:
            answer = (await self._wait_for_reply(ctx)).content.strip().casefold()
        except asyncio.TimeoutError:
            await ctx.send("Live-draw setup timed out. Nothing was saved.")
            return
        if answer not in ("yes", "y"):
            await ctx.send("Setup cancelled. Nothing was saved.")
            return

        await ctx.send(f"**2/3 — Number of groups**\nThere are **{len(teams)} approved teams**. How many groups do you want? Reply with a whole number from 1 to {len(teams)}.")
        try:
            reply = await self._wait_for_reply(ctx)
            group_count = int(reply.content.strip())
        except asyncio.TimeoutError:
            await ctx.send("Live-draw setup timed out. Nothing was saved.")
            return
        except ValueError:
            await ctx.send("That wasn't a valid whole number. Run `+setuplivedraws` again.")
            return
        if not 1 <= group_count <= len(teams):
            await ctx.send(f"Choose a number from 1 to {len(teams)}. Run `+setuplivedraws` again.")
            return

        await ctx.send("**3/3 — Setting name**\nWhat should this live-draw setting be called? Example: `KASHMIR TITANS S1`.")
        try:
            setting_name = (await self._wait_for_reply(ctx)).content.strip()
        except asyncio.TimeoutError:
            await ctx.send("Live-draw setup timed out. Nothing was saved.")
            return
        if not setting_name or len(setting_name) > 80:
            await ctx.send("The setting name must be between 1 and 80 characters. Run `+setuplivedraws` again.")
            return

        import json
        key = self._setting_key(setting_name)
        self.db.execute(
            "INSERT INTO live_draw_settings(guild_id, setting_key, setting_name, group_count, team_names_json, created_by) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(guild_id, setting_key) DO UPDATE SET "
            "setting_name=excluded.setting_name, group_count=excluded.group_count, "
            "team_names_json=excluded.team_names_json, created_by=excluded.created_by",
            (ctx.guild.id, key, setting_name, group_count, json.dumps(teams, ensure_ascii=False), ctx.author.id),
        )
        self.db.commit()
        await ctx.send(
            f"Live-draw setting **{discord.utils.escape_markdown(setting_name)}** saved.\n"
            f"Teams: **{len(teams)}** | Groups: **{group_count}**\n"
            f"Start it with `+startgroupdraws {setting_name}`."
        )

    def _is_bot_owner(self, ctx):
        return ctx.author.id == OWNER_ID

    @staticmethod
    def _group_labels(group_count):
        return [chr(ord("A") + i) if i < 26 else str(i + 1) for i in range(group_count)]

    @commands.command(name="setdrawgroup")
    @commands.guild_only()
    async def set_draw_group(self, ctx, *, details: str):
        """Privately assign one team to a group for a saved draw setting."""
        if not self._is_bot_owner(ctx):
            await ctx.send("Only the bot owner can manage secret draw assignments.")
            return

        parts = [part.strip() for part in details.split("|", 2)]
        if len(parts) != 3 or not all(parts):
            await ctx.author.send(
                "Usage: `+setdrawgroup SETTING NAME | TEAM NAME | GROUP`\n"
                "Example: `+setdrawgroup KASHMIR TITANS S1 | Team Alpha | A`"
            )
            return

        setting_name, team_name, group_label = parts
        setting_key = self._setting_key(setting_name)
        setting = self.db.execute(
            "SELECT * FROM live_draw_settings WHERE guild_id=? AND setting_key=?",
            (ctx.guild.id, setting_key),
        ).fetchone()
        if not setting:
            await ctx.author.send(f"Draw setting **{discord.utils.escape_markdown(setting_name)}** was not found.")
            return

        import json
        saved_teams = json.loads(setting["team_names_json"])
        matching = [name for name in saved_teams if name.casefold() == team_name.casefold()]
        if not matching:
            await ctx.author.send(f"Team **{discord.utils.escape_markdown(team_name)}** is not in this draw setting's approved team list.")
            return
        actual_team = matching[0]
        labels = self._group_labels(setting["group_count"])
        normalized_label = group_label.strip().upper()
        if normalized_label not in labels:
            await ctx.author.send(f"Invalid group. Choose one of: {', '.join(labels)}.")
            return

        self.db.execute(
            "INSERT INTO secret_draw_groups(guild_id, setting_key, team_key, team_name, group_index, assigned_by) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(guild_id, setting_key, team_key) DO UPDATE SET "
            "team_name=excluded.team_name, group_index=excluded.group_index, assigned_by=excluded.assigned_by",
            (ctx.guild.id, setting_key, self._setting_key(actual_team), actual_team,
             labels.index(normalized_label), ctx.author.id),
        )
        self.db.commit()
        await ctx.author.send(
            f"Secret assignment saved for **{discord.utils.escape_markdown(setting["setting_name"])}**. "
            f"The team will be placed in Group {normalized_label}."
        )

    @commands.command(name="listdrawgroups")
    @commands.guild_only()
    async def list_draw_groups(self, ctx, *, setting_name: str):
        """Show secret assignments to the bot owner by DM only."""
        if not self._is_bot_owner(ctx):
            await ctx.send("Only the bot owner can view secret draw assignments.")
            return
        key = self._setting_key(setting_name)
        setting = self.db.execute(
            "SELECT * FROM live_draw_settings WHERE guild_id=? AND setting_key=?",
            (ctx.guild.id, key),
        ).fetchone()
        if not setting:
            await ctx.author.send("That draw setting was not found.")
            return
        rows = self.db.execute(
            "SELECT team_name, group_index FROM secret_draw_groups WHERE guild_id=? AND setting_key=? ORDER BY team_name COLLATE NOCASE",
            (ctx.guild.id, key),
        ).fetchall()
        if not rows:
            await ctx.author.send(f"No secret assignments are saved for **{discord.utils.escape_markdown(setting['setting_name'])}**.")
            return
        labels = self._group_labels(setting["group_count"])
        lines = [f"**{discord.utils.escape_markdown(row['team_name'])}** → Group {labels[row['group_index']]}" for row in rows]
        await ctx.author.send(f"**Private assignments — {discord.utils.escape_markdown(setting['setting_name'])}**\n" + "\n".join(lines))

    @commands.command(name="cleardrawgroups")
    @commands.guild_only()
    async def clear_draw_groups(self, ctx, *, setting_name: str):
        """Clear all secret team assignments for one draw setting."""
        if not self._is_bot_owner(ctx):
            await ctx.send("Only the bot owner can clear secret draw assignments.")
            return
        key = self._setting_key(setting_name)
        cursor = self.db.execute(
            "DELETE FROM secret_draw_groups WHERE guild_id=? AND setting_key=?",
            (ctx.guild.id, key),
        )
        self.db.commit()
        await ctx.author.send(f"Cleared **{cursor.rowcount}** secret assignment(s) for **{discord.utils.escape_markdown(setting_name)}**.")

    @commands.command(name="viewgroupsetups", aliases=["listgroupsetups"])
    @commands.guild_only()
    async def view_group_setups(self, ctx):
        """Show all saved draw settings and their secret assignment counts to the bot owner."""
        if not self._is_bot_owner(ctx):
            await ctx.send("Only the bot owner can view secret group setups.")
            return

        settings = self.db.execute(
            "SELECT setting_key, setting_name, group_count FROM live_draw_settings "
            "WHERE guild_id=? ORDER BY setting_name COLLATE NOCASE",
            (ctx.guild.id,),
        ).fetchall()

        if not settings:
            await ctx.author.send("There are no saved draw setups in this server.")
            return

        lines = []
        for setting in settings:
            count = self.db.execute(
                "SELECT COUNT(*) FROM secret_draw_groups WHERE guild_id=? AND setting_key=?",
                (ctx.guild.id, setting["setting_key"]),
            ).fetchone()[0]
            labels = ", ".join(self._group_labels(setting["group_count"]))
            lines.append(
                f"**{discord.utils.escape_markdown(setting['setting_name'])}** — "
                f"{setting['group_count']} groups ({labels}) | "
                f"{count} secret assignment(s)"
            )

        chunks, current = [], "**Saved group draw setups (private)**\n"
        for line in lines:
            if len(current) + len(line) + 1 > 1900:
                chunks.append(current)
                current = ""
            current += line + "\n"
        if current:
            chunks.append(current)
        for chunk in chunks:
            await ctx.author.send(chunk)

    @commands.command(name="deletegroupsetup", aliases=["removegroupsetup"])
    @commands.guild_only()
    async def delete_group_setup(self, ctx, *, details: str):
        """Remove one team's secret group assignment from a draw setup."""
        if not self._is_bot_owner(ctx):
            await ctx.send("Only the bot owner can delete secret group assignments.")
            return

        parts = [part.strip() for part in details.split("|", 1)]
        if len(parts) != 2 or not all(parts):
            await ctx.author.send(
                "Usage: `+deletegroupsetup SETTING NAME | TEAM NAME`\n"
                "Example: `+deletegroupsetup KASHMIR TITANS S1 | Kashmir Titans`"
            )
            return

        setting_name, team_name = parts
        setting_key = self._setting_key(setting_name)
        setting = self.db.execute(
            "SELECT setting_name FROM live_draw_settings WHERE guild_id=? AND setting_key=?",
            (ctx.guild.id, setting_key),
        ).fetchone()
        if not setting:
            await ctx.author.send(
                f"Draw setting **{discord.utils.escape_markdown(setting_name)}** was not found."
            )
            return

        cursor = self.db.execute(
            "DELETE FROM secret_draw_groups WHERE guild_id=? AND setting_key=? AND team_key=?",
            (ctx.guild.id, setting_key, self._setting_key(team_name)),
        )
        self.db.commit()
        if cursor.rowcount:
            await ctx.author.send(
                f"Removed the secret group assignment for **{discord.utils.escape_markdown(team_name)}** "
                f"from **{discord.utils.escape_markdown(setting['setting_name'])}**."
            )
        else:
            await ctx.author.send(
                f"No secret assignment was found for **{discord.utils.escape_markdown(team_name)}** "
                f"in **{discord.utils.escape_markdown(setting['setting_name'])}**."
            )

    @commands.command(name="startgroupdraws", aliases=["startlivedraw", "startlivedraws"])
    @commands.guild_only()
    async def start_live_draw(self, ctx, *, setting_name: str):
        """Draw one team at a time, retaining the complete draw in each new message."""
        if not await owner_or_whitelisted(ctx):
            await ctx.send("You are not authorized to use this command.")
            return

        key = self._setting_key(setting_name)
        setting = self.db.execute(
            "SELECT * FROM live_draw_settings WHERE guild_id=? AND setting_key=?",
            (ctx.guild.id, key),
        ).fetchone()
        if not setting:
            await ctx.send(f"I couldn't find the live-draw setting **{discord.utils.escape_markdown(setting_name)}**. Run `+setuplivedraws` first.")
            return
        if ctx.guild.id in self.active_guilds:
            await ctx.send("A live draw is already running in this server.")
            return

        import json
        try:
            current_teams = self._approved_teams(ctx.guild.id)
        except (sqlite3.Error, FileNotFoundError) as exc:
            await ctx.send(f"Couldn't read the team database: {exc}")
            return
        saved_teams = json.loads(setting["team_names_json"])
        # A draw uses the exact verified setup list. Warn if the live database has changed.
        if set(current_teams) != set(saved_teams):
            await ctx.send(
                "The approved team list has changed since this setting was created. "
                "Run `+setuplivedraws` again to verify and save the current list before drawing."
            )
            return
        if not saved_teams:
            await ctx.send("This setting contains no teams.")
            return

        group_count = setting["group_count"]
        groups = [[] for _ in range(group_count)]
        preset_rows = self.db.execute(
            "SELECT team_name, group_index FROM secret_draw_groups WHERE guild_id=? AND setting_key=?",
            (ctx.guild.id, key),
        ).fetchall()
        saved_lookup = {self._setting_key(name): name for name in saved_teams}
        preset = {}
        for row in preset_rows:
            team_key = self._setting_key(row["team_name"])
            if team_key in saved_lookup and 0 <= row["group_index"] < group_count:
                preset[saved_lookup[team_key]] = row["group_index"]

        preset_teams = list(preset.keys())
        random.shuffle(preset_teams)
        remaining_teams = [name for name in saved_teams if name not in preset]
        random.shuffle(remaining_teams)
        draw_order = [(name, preset[name]) for name in preset_teams]

        # Pre-assigned teams are drawn first and keep their configured groups.
        # Continue the normal A -> B -> C rotation AFTER the group of the
        # last pre-assigned team drawn. For example, if the last preset team
        # goes into Group A, the next random team starts in Group B.
        next_group_index = (
            (preset[preset_teams[-1]] + 1) % group_count
            if preset_teams else 0
        )
        for name in remaining_teams:
            draw_order.append((name, next_group_index))
            next_group_index = (next_group_index + 1) % group_count
        self.active_guilds.add(ctx.guild.id)
        try:
            total = len(draw_order)
            for drawn_count, (team_name, group_index) in enumerate(draw_order, 1):
                rolling = await ctx.send(
                    f"Randomly rolling a team from **{total} teams** in **3 seconds...**"
                )
                for seconds in (2, 1):
                    await asyncio.sleep(1)
                    await rolling.edit(
                        content=f"Randomly rolling a team from **{total} teams** in **{seconds} second{'s' if seconds != 1 else ''}...**"
                    )
                await asyncio.sleep(1)

                await rolling.edit(
                    content=f"🎲 Randomly rolled **{discord.utils.escape_markdown(team_name)}** from **{total} teams**."
                )

                # The destination is fixed privately before the draw starts.
                # Pre-assigned teams are processed first; other teams have random balanced destinations.
                groups[group_index].append(team_name)
                content = self._format_results(setting["setting_name"], groups, drawn_count, total)
                await ctx.send(content)

                if drawn_count >= total:
                    break

                def check(message):
                    return (
                        message.author.id == ctx.author.id
                        and message.channel.id == ctx.channel.id
                        and message.guild is not None
                        and message.guild.id == ctx.guild.id
                        and message.content.strip().casefold() in {"next", "next draw"}
                    )

                try:
                    await self.bot.wait_for("message", check=check, timeout=300)
                except asyncio.TimeoutError:
                    await ctx.send(
                        f"Live draw **{setting['setting_name']}** ended due to 5 minutes of inactivity. "
                        f"**{drawn_count}/{total} teams drawn.**"
                    )
                    return

            await ctx.send(
                f"Live draw **{setting['setting_name']}** is complete — **{total}/{total} teams drawn**."
            )
        finally:
            self.active_guilds.discard(ctx.guild.id)

    @staticmethod
    def _format_results(setting_name, groups, drawn_count, total):
        letters = [chr(ord("A") + i) if i < 26 else f"{i + 1}" for i in range(len(groups))]
        lines = [f"## {setting_name}", ""]
        for index, teams in enumerate(groups):
            lines.append(f"**__GROUP - {letters[index]}__**")
            if teams:
                lines.extend(f"{number}. **__{name}__**" for number, name in enumerate(teams, 1))
            else:
                lines.append("No teams yet.")
            lines.append("")
        lines.append(f"*{drawn_count}/{total} teams drawn*")
        result = "\n".join(lines)
        if len(result) > 1900:
            # Discord's message limit is 2000 characters. Keep names safe from truncation.
            raise ValueError("The draw results exceed Discord's 2000-character message limit. Use fewer/shorter group names or team names.")
        return result


async def setup(bot):
    await bot.add_cog(LiveDraws(bot))
