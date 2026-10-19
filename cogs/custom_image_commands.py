"""Standalone cog for owner-managed custom image embed commands.

Load with: await bot.load_extension("custom_image_commands")
Commands:
  +createimagecommand  - create a trigger, image URL and embed colour
  +editimagecommand    - replace an existing trigger's image and colour
  +deleteimagecommand <name> - delete a trigger
  +listimagecommands   - list saved triggers

Example: after creating the trigger "points A", users can type +points A.
"""

import asyncio
import re
import sqlite3
from pathlib import Path

import discord
from discord.ext import commands

BOT_OWNER_ID = 1014568928682459198
DB_PATH = Path("custom_image_commands.db")


def normalise_trigger(value: str) -> str:
    return " ".join(str(value or "").strip().lower().lstrip("+!/ ").split())


def parse_colour(value: str):
    value = value.strip().lstrip("#")
    if not re.fullmatch(r"[0-9a-fA-F]{6}", value):
        return None
    return int(value, 16), value.upper()


class CustomImageCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.db = sqlite3.connect(str(DB_PATH))
        self.db.row_factory = sqlite3.Row
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS custom_image_commands (
                guild_id INTEGER NOT NULL,
                trigger TEXT NOT NULL,
                image_url TEXT NOT NULL,
                embed_colour INTEGER NOT NULL,
                created_by INTEGER NOT NULL,
                PRIMARY KEY (guild_id, trigger)
            )"""
        )
        self.db.commit()

    def cog_unload(self):
        self.db.close()

    async def _ask(self, ctx, prompt):
        await ctx.send(prompt)

        def check(message):
            return (
                message.author.id == ctx.author.id
                and message.channel.id == ctx.channel.id
                and message.guild is not None
                and message.guild.id == ctx.guild.id
            )

        return await self.bot.wait_for("message", check=check, timeout=180)

    async def _owner_only(self, ctx):
        if ctx.author.id == BOT_OWNER_ID:
            return True
        await ctx.reply("Only the bot owner can use this command.", mention_author=False)
        return False

    @commands.command(name="createimagecommand", aliases=["addimagecommand", "setimagecommand"])
    @commands.guild_only()
    async def create_image_command(self, ctx):
        """Create a custom command that sends an image embed."""
        if not await self._owner_only(ctx):
            return
        try:
            reply = await self._ask(
                ctx,
                "**Step 1/3 — Command name**\nWhat should the command be called? Example: `points A`. Users will run `+points A`.",
            )
            trigger = normalise_trigger(reply.content)
            if not trigger or len(trigger) > 80 or not re.fullmatch(r"[a-z0-9_-]+(?: [a-z0-9_-]+)*", trigger):
                await ctx.send("Invalid command name. Use letters, numbers, `_` or `-`, with spaces allowed. Nothing was saved.")
                return
            if len(trigger.split()) == 1 and self.bot.get_command(trigger) is not None:
                await ctx.send("That name is already a bot command. Choose a different name.")
                return
            exists = self.db.execute(
                "SELECT 1 FROM custom_image_commands WHERE guild_id=? AND trigger=?",
                (ctx.guild.id, trigger),
            ).fetchone()
            if exists:
                await ctx.send("That custom command already exists. Use `+editimagecommand` to change it.")
                return

            reply = await self._ask(ctx, "**Step 2/3 — Image**\nSend a direct image URL beginning with `http://` or `https://`.")
            image_url = reply.content.strip().strip("<>")
            if not image_url.startswith(("https://", "http://")):
                await ctx.send("That doesn't look like an HTTP(S) image URL. Nothing was saved.")
                return

            reply = await self._ask(ctx, "**Step 3/3 — Embed colour**\nSend a hex colour, e.g. `#3796FF`.")
            parsed = parse_colour(reply.content)
            if parsed is None:
                await ctx.send("Invalid colour. Use six hex digits, e.g. `#3796FF`. Nothing was saved.")
                return
            colour, colour_text = parsed
            self.db.execute(
                "INSERT INTO custom_image_commands(guild_id, trigger, image_url, embed_colour, created_by) VALUES(?,?,?,?,?)",
                (ctx.guild.id, trigger, image_url, colour, ctx.author.id),
            )
            self.db.commit()
            await ctx.send(f"✅ Created `+{trigger}`.\nEmbed colour: `#{colour_text}`\nAnyone in this server can use it.")
        except asyncio.TimeoutError:
            await ctx.send("Setup timed out after 3 minutes. Nothing was saved.")

    @commands.command(name="editimagecommand", aliases=["changeimagecommand"])
    @commands.guild_only()
    async def edit_image_command(self, ctx):
        """Edit an existing command's image URL and embed colour."""
        if not await self._owner_only(ctx):
            return
        try:
            reply = await self._ask(ctx, "**Step 1/3 — Command name**\nType the existing command name, e.g. `points A`.")
            trigger = normalise_trigger(reply.content)
            row = self.db.execute(
                "SELECT 1 FROM custom_image_commands WHERE guild_id=? AND trigger=?",
                (ctx.guild.id, trigger),
            ).fetchone()
            if not row:
                await ctx.send("I couldn't find that custom command. Use `+listimagecommands` to see saved commands. Nothing was changed.")
                return

            reply = await self._ask(ctx, "**Step 2/3 — New image**\nSend the replacement image URL beginning with `http://` or `https://`.")
            image_url = reply.content.strip().strip("<>")
            if not image_url.startswith(("https://", "http://")):
                await ctx.send("That doesn't look like an HTTP(S) image URL. Nothing was changed.")
                return

            reply = await self._ask(ctx, "**Step 3/3 — New embed colour**\nSend a hex colour, e.g. `#3796FF`.")
            parsed = parse_colour(reply.content)
            if parsed is None:
                await ctx.send("Invalid colour. Use six hex digits, e.g. `#3796FF`. Nothing was changed.")
                return
            colour, colour_text = parsed
            self.db.execute(
                "UPDATE custom_image_commands SET image_url=?, embed_colour=?, created_by=? WHERE guild_id=? AND trigger=?",
                (image_url, colour, ctx.author.id, ctx.guild.id, trigger),
            )
            self.db.commit()
            await ctx.send(f"✅ Updated `+{trigger}`.\nNew embed colour: `#{colour_text}`")
        except asyncio.TimeoutError:
            await ctx.send("Edit timed out after 3 minutes. Nothing was changed.")

    @commands.command(name="deleteimagecommand", aliases=["removeimagecommand"])
    @commands.guild_only()
    async def delete_image_command(self, ctx, *, trigger: str):
        if not await self._owner_only(ctx):
            return
        trigger = normalise_trigger(trigger)
        cursor = self.db.execute(
            "DELETE FROM custom_image_commands WHERE guild_id=? AND trigger=?",
            (ctx.guild.id, trigger),
        )
        self.db.commit()
        await ctx.reply(
            f"Deleted custom command `+{trigger}`." if cursor.rowcount else "I couldn't find that custom command.",
            mention_author=False,
        )

    @commands.command(name="listimagecommands", aliases=["imagecommands"])
    @commands.guild_only()
    async def list_image_commands(self, ctx):
        rows = self.db.execute(
            "SELECT trigger FROM custom_image_commands WHERE guild_id=? ORDER BY trigger",
            (ctx.guild.id,),
        ).fetchall()
        if not rows:
            await ctx.reply("No custom image commands have been created yet.", mention_author=False)
            return
        await ctx.reply("**Custom image commands**\n" + "\n".join(f"`+{row['trigger']}`" for row in rows), mention_author=False)

    @commands.Cog.listener()
    async def on_message(self, message):
        """Respond to saved triggers like +points A with an image-only embed."""
        if message.author.bot or message.guild is None or not message.content.startswith("+"):
            return
        trigger = normalise_trigger(message.content[1:])
        if not trigger:
            return
        row = self.db.execute(
            "SELECT image_url, embed_colour FROM custom_image_commands WHERE guild_id=? AND trigger=?",
            (message.guild.id, trigger),
        ).fetchone()
        if row is None:
            return
        embed = discord.Embed(colour=discord.Colour(int(row["embed_colour"])))
        embed.set_image(url=row["image_url"])
        try:
            await message.channel.send(embed=embed)
        except discord.HTTPException:
            await message.channel.send("I couldn't display that image. Check that the URL points to an image Discord can access.")


async def setup(bot):
    await bot.add_cog(CustomImageCommands(bot))
