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
        if ctx.author.id != OWNER_ID:
            await ctx.send("You are not authorized to use live-draw commands.")
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
            f"Start it with `+startlivedraw {setting_name}`."
        )

    @commands.command(name="startlivedraw", aliases=["startlivedraws"])
    @commands.guild_only()
    async def start_live_draw(self, ctx, *, setting_name: str):
        """Draw one team at a time, retaining the complete draw in each new message."""
        if ctx.author.id != OWNER_ID:
            await ctx.send("You are not authorized to use live-draw commands.")
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
        random_teams = list(saved_teams)
        random.shuffle(random_teams)
        self.active_guilds.add(ctx.guild.id)
        try:
            total = len(random_teams)
            for drawn_count, team_name in enumerate(random_teams, 1):
                remaining_before_draw = total - drawn_count + 1
                rolling = await ctx.send(
                    f"Randomly drawing a team from **{remaining_before_draw} teams** in **3 seconds...**"
                )
                for seconds in (2, 1):
                    await asyncio.sleep(1)
                    await rolling.edit(
                        content=f"Randomly drawing a team from **{remaining_before_draw} teams** in **{seconds} second{'s' if seconds != 1 else ''}...**"
                    )
                await asyncio.sleep(1)

                # Distribute in order A, B, C... then cycle back to A.
                group_index = (drawn_count - 1) % group_count
                groups[group_index].append(team_name)
                content = self._format_results(setting["setting_name"], groups, drawn_count, total)
                await rolling.edit(content=content)

                if drawn_count >= total:
                    break

                def check(message):
                    return (
                        message.author.id == OWNER_ID
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
