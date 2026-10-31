"""Custom celebration lines for Googli Manager.

Every player can save FOUR lines (each may be text, a GIF link, or both):

    Fielder - I took the catch        sent when YOU catch someone out
    Fielder - I dropped the catch     sent when YOU drop a catch
    Batter  - my catch was taken      sent when a fielder catches YOU out
    Batter  - my catch was dropped    sent when a fielder drops YOUR catch

fielding.py calls ``send_line`` right after the Hand Cricket bot posts
"TOOK THE CATCH" / "BOZO DROPPED THE CATCH".  The fielder's line is sent first,
then the batter's line.

Lines are stored in the bot's SQLite database (bot.db) per (server, player), so
they survive restarts and 100+ players is no problem.

Player commands
    +setceleb                 menu to set / change / remove all four lines
    +mylines                  show your saved lines
    +clearlines [which]       remove lines (all if you give no argument)
    +settakeline <text>       quick set: fielder, I took the catch
    +setdropline <text>       quick set: fielder, I dropped the catch
Admin commands (bot owner or Manage Server)
    +clearlinesfor @user [which]

``which`` is one of: all, take, drop, bat_take, bat_drop
"""
from __future__ import annotations

import re
import time
from typing import Optional

import discord
from discord.ext import commands

BOT_OWNER_ID = 1014568928682459198
MAX_LINE_LENGTH = 300
MENU_TIMEOUT_SECONDS = 300
NO_MENTIONS = discord.AllowedMentions.none()
_URL_RE = re.compile(r"(?:https?://|www\.)[^\s<>]+", re.IGNORECASE)
_INVITE_RE = re.compile(r"discord\.gg|discord(?:app)?\.com/invite", re.IGNORECASE)
# GIF links are allowed so Discord shows them as a normal GIF preview.
_GIF_HOSTS = (
    "tenor.com", "giphy.com", "imgur.com", "gfycat.com",
    "cdn.discordapp.com", "media.discordapp.net", "klipy.com",
)
_GIF_EXT = (".gif", ".gifv", ".webp", ".mp4")

# slot key -> (db column, menu label, menu description, emoji, field title)
SLOTS = {
    "take": ("take_text", "Fielder - I took the catch",
             "Sent when YOU catch the batter out", "🧤", "🧤 Fielder · I took the catch"),
    "drop": ("drop_text", "Fielder - I dropped the catch",
             "Sent when YOU drop a catch", "🧤", "🧤 Fielder · I dropped the catch"),
    "bat_take": ("bat_take_text", "Batter - my catch was taken",
                 "Sent when a fielder catches YOU out", "🏏", "🏏 Batter · my catch was taken"),
    "bat_drop": ("bat_drop_text", "Batter - my catch was dropped",
                 "Sent when a fielder drops YOUR catch", "🏏", "🏏 Batter · my catch was dropped"),
}
_KINDS = {key: spec[0] for key, spec in SLOTS.items()}


def _is_gif_url(url: str) -> bool:
    m = re.match(r"(?:https?://|www\.)([^/?#\s]+)([^?#\s]*)", url, re.IGNORECASE)
    if not m:
        return False
    host = m.group(1).lower().split("@")[-1].split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    path = m.group(2).lower()
    if any(host == h or host.endswith("." + h) for h in _GIF_HOSTS):
        return True
    return path.endswith(_GIF_EXT)


def clean_line(raw: str) -> tuple[Optional[str], Optional[str]]:
    """Return (cleaned_text, error).  Exactly one of them is None."""
    text = " ".join((raw or "").split())  # one line, single spaces
    if not text:
        return None, "The line can't be empty."
    if len(text) > MAX_LINE_LENGTH:
        return None, f"That's {len(text)} characters. The limit is {MAX_LINE_LENGTH}."
    if _INVITE_RE.search(text):
        return None, "Server invites aren't allowed in custom lines."
    for url in _URL_RE.findall(text):
        if not _is_gif_url(url):
            return None, ("Only GIF links are allowed (Tenor, Giphy, Imgur or a direct "
                          "`.gif` link). Other links are blocked.")
    if text[0] in "+/!":
        return None, "A line can't start with `+`, `/` or `!` (it could trigger other bots)."
    return text, None


def _kinds_for(which: str):
    which = (which or "all").lower()
    if which in ("all", "both"):
        return list(SLOTS)
    return [which] if which in SLOTS else None


# --------------------------------------------------------------------------- menu UI
class LineModal(discord.ui.Modal):
    """Pop-up where the player types the text / GIF link for the chosen slot."""

    def __init__(self, view: "CelebView", key: str):
        super().__init__(title=SLOTS[key][1][:45])
        self.celeb_view = view
        self.key = key
        current = view.cog.get_all(view.guild_id, view.owner_id)[key]
        self.line_input = discord.ui.TextInput(
            label="Your line (text and/or GIF link)",
            style=discord.TextStyle.paragraph,
            default=current,
            required=False,
            max_length=MAX_LINE_LENGTH,
            placeholder="Leave empty and press Submit to remove this line",
        )
        self.add_item(self.line_input)

    async def on_submit(self, interaction: discord.Interaction):
        view, cog = self.celeb_view, self.celeb_view.cog
        raw = str(self.line_input.value or "")
        if not raw.strip():
            cog._set(view.guild_id, view.owner_id, self.key, None)
        else:
            cleaned, error = clean_line(raw)
            if error:
                await interaction.response.send_message(f"❌ {error}", ephemeral=True)
                return
            cog._set(view.guild_id, view.owner_id, self.key, cleaned)
        await interaction.response.edit_message(
            embed=cog.build_embed(view.guild_id, view.owner_id, interaction.user), view=view)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        print(f"[fielderlines] menu error: {type(error).__name__}: {error}")
        try:
            await interaction.response.send_message("❌ Something went wrong. Try again.", ephemeral=True)
        except Exception:
            pass


class LinePicker(discord.ui.Select):
    def __init__(self):
        super().__init__(
            placeholder="Choose which line to set or change…",
            min_values=1, max_values=1,
            options=[discord.SelectOption(label=spec[1], value=key, description=spec[2], emoji=spec[3])
                     for key, spec in SLOTS.items()],
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(LineModal(self.view, self.values[0]))


class CelebView(discord.ui.View):
    def __init__(self, cog, owner_id: int, guild_id: int):
        super().__init__(timeout=MENU_TIMEOUT_SECONDS)
        self.cog, self.owner_id, self.guild_id = cog, owner_id, guild_id
        self.message = None
        self.add_item(LinePicker())

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "This menu belongs to someone else. Use `+setceleb` to open your own.", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except Exception:
                pass


class FielderLines(commands.Cog):
    """Custom text / GIFs the bot posts after a catch is taken or dropped."""

    def __init__(self, bot):
        self.bot = bot
        db = bot.db
        db.execute(
            """CREATE TABLE IF NOT EXISTS fielder_lines (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                take_text TEXT,
                drop_text TEXT,
                updated_at REAL NOT NULL,
                PRIMARY KEY (guild_id, user_id)
            )"""
        )
        # Upgrade older databases that only have the two fielder columns.
        existing = {row[1] for row in db.execute("PRAGMA table_info(fielder_lines)").fetchall()}
        for column in ("bat_take_text", "bat_drop_text"):
            if column not in existing:
                db.execute(f"ALTER TABLE fielder_lines ADD COLUMN {column} TEXT")
        db.commit()

    # ------------------------------------------------------------------ diagnostics
    @commands.Cog.listener()
    async def on_command_error(self, ctx, error):
        """Print why one of OUR commands failed or was blocked (console only)."""
        if ctx.command is not None and ctx.command.cog is self:
            print(f"[fielderlines] +{ctx.command.name} failed: {type(error).__name__}: {error}")

    @commands.Cog.listener()
    async def on_command(self, ctx):
        if ctx.command is not None and ctx.command.cog is self:
            print(f"[fielderlines] +{ctx.command.name} used by {ctx.author.id}")

    # ------------------------------------------------------------------ storage
    def get_all(self, guild_id: int, user_id: int) -> dict:
        """All four lines as {slot_key: text_or_None}."""
        row = self.bot.db.execute(
            "SELECT take_text, drop_text, bat_take_text, bat_drop_text FROM fielder_lines "
            "WHERE guild_id=? AND user_id=?",
            (int(guild_id), int(user_id)),
        ).fetchone()
        values = tuple(row) if row else (None, None, None, None)
        return dict(zip(SLOTS, values))

    def _get(self, guild_id: int, user_id: int) -> tuple[Optional[str], Optional[str]]:
        lines = self.get_all(guild_id, user_id)
        return lines["take"], lines["drop"]

    def _set(self, guild_id: int, user_id: int, kind: str, text: Optional[str]):
        column = _KINDS[kind]  # fixed whitelist, never user input
        db = self.bot.db
        db.execute(
            f"INSERT INTO fielder_lines (guild_id, user_id, {column}, updated_at) "
            f"VALUES (?, ?, ?, ?) ON CONFLICT(guild_id, user_id) DO UPDATE SET "
            f"{column}=excluded.{column}, updated_at=excluded.updated_at",
            (int(guild_id), int(user_id), text, time.time()),
        )
        # Drop the row entirely once a player has no lines left.
        db.execute(
            "DELETE FROM fielder_lines WHERE guild_id=? AND user_id=? "
            "AND take_text IS NULL AND drop_text IS NULL "
            "AND bat_take_text IS NULL AND bat_drop_text IS NULL",
            (int(guild_id), int(user_id)),
        )
        db.commit()

    # ------------------------------------------------------------------ called by fielding.py
    async def send_line(self, channel, fielder_id: int, result: str, batter_id: Optional[int] = None) -> bool:
        """Send the fielder's line, then the batter's line, for ``result``
        ('taken' or 'dropped').  Returns True if anything was sent."""
        suffix = "take" if result == "taken" else "drop"
        guild_id = channel.guild.id
        sent = False

        text = self.get_all(guild_id, fielder_id)[suffix]
        if text:
            print(f"[fielderlines] {result}: sending fielder line for user {fielder_id}")
            await channel.send(text, allowed_mentions=NO_MENTIONS)
            sent = True
        else:
            print(f"[fielderlines] {result}: no fielder line saved for user {fielder_id}")

        if batter_id and int(batter_id) != int(fielder_id):
            text = self.get_all(guild_id, batter_id)["bat_" + suffix]
            if text:
                print(f"[fielderlines] {result}: sending batter line for user {batter_id}")
                await channel.send(text, allowed_mentions=NO_MENTIONS)
                sent = True
            else:
                print(f"[fielderlines] {result}: no batter line saved for user {batter_id}")
        return sent

    # ------------------------------------------------------------------ menu
    def build_embed(self, guild_id: int, user_id: int, member=None) -> discord.Embed:
        lines = self.get_all(guild_id, user_id)
        embed = discord.Embed(
            title="🎉 Your celebration lines",
            description=("Pick a line in the menu below, type your text or paste a GIF link, "
                         "then press **Submit**. It's saved straight away."),
            color=discord.Color.from_rgb(55, 150, 255),
        )
        for key, spec in SLOTS.items():
            embed.add_field(name=spec[4], value=lines[key] or "*not set*", inline=False)
        embed.set_footer(text="Fielder lines play when you catch / drop. Batter lines play when your catch is taken / dropped.")
        return embed

    @commands.command(name="setceleb")
    @commands.guild_only()
    async def set_celeb(self, ctx):
        """Open the menu to set your fielder and batter celebration lines."""
        view = CelebView(self, ctx.author.id, ctx.guild.id)
        view.message = await ctx.reply(
            embed=self.build_embed(ctx.guild.id, ctx.author.id, ctx.author),
            view=view, mention_author=False, allowed_mentions=NO_MENTIONS)

    # ------------------------------------------------------------------ player commands
    async def _save(self, ctx, kind: str, text: str):
        cleaned, error = clean_line(text)
        if error:
            await ctx.reply(f"❌ {error}", mention_author=False, allowed_mentions=NO_MENTIONS)
            return
        self._set(ctx.guild.id, ctx.author.id, kind, cleaned)
        label = "take a catch" if kind == "take" else "drop a catch"
        await ctx.reply(
            f"✅ Saved. When you {label}, I'll send:\n{cleaned}\n"
            f"*Tip: `+setceleb` sets your batter lines too.*",
            mention_author=False, allowed_mentions=NO_MENTIONS)

    @commands.command(name="settakeline")
    @commands.guild_only()
    async def set_take_line(self, ctx, *, text: str = ""):
        """Set the line sent when you take a catch."""
        if not text.strip():
            await ctx.reply("Usage: `+settakeline <your line>` (or use `+setceleb`)", mention_author=False)
            return
        await self._save(ctx, "take", text)

    @commands.command(name="setdropline")
    @commands.guild_only()
    async def set_drop_line(self, ctx, *, text: str = ""):
        """Set the line sent when you drop a catch."""
        if not text.strip():
            await ctx.reply("Usage: `+setdropline <your line>` (or use `+setceleb`)", mention_author=False)
            return
        await self._save(ctx, "drop", text)

    @commands.command(name="mylines")
    @commands.guild_only()
    async def my_lines(self, ctx):
        """Show your saved celebration lines."""
        await ctx.reply(
            embed=self.build_embed(ctx.guild.id, ctx.author.id, ctx.author),
            mention_author=False, allowed_mentions=NO_MENTIONS)

    @commands.command(name="clearlines")
    @commands.guild_only()
    async def clear_lines(self, ctx, which: str = "all"):
        """Remove your lines: all, take, drop, bat_take or bat_drop."""
        kinds = _kinds_for(which)
        if kinds is None:
            await ctx.reply("Usage: `+clearlines` or `+clearlines take|drop|bat_take|bat_drop`",
                            mention_author=False)
            return
        for kind in kinds:
            self._set(ctx.guild.id, ctx.author.id, kind, None)
        await ctx.reply("🗑️ Removed." if len(kinds) == len(SLOTS) else f"🗑️ Removed your `{which.lower()}` line.",
                        mention_author=False)

    # ------------------------------------------------------------------ admin
    @commands.command(name="clearlinesfor")
    @commands.guild_only()
    async def clear_lines_for(self, ctx, member: discord.Member, which: str = "all"):
        """Admin: remove a player's custom lines (e.g. an inappropriate one)."""
        if ctx.author.id != BOT_OWNER_ID and not ctx.author.guild_permissions.manage_guild:
            await ctx.reply("❌ You need the Manage Server permission for this.", mention_author=False)
            return
        kinds = _kinds_for(which)
        if kinds is None:
            await ctx.reply("Usage: `+clearlinesfor @user [take|drop|bat_take|bat_drop]`", mention_author=False)
            return
        for kind in kinds:
            self._set(ctx.guild.id, member.id, kind, None)
        await ctx.reply(
            f"🗑️ Removed {'all' if len(kinds) == len(SLOTS) else which.lower()} custom line(s) for "
            f"**{discord.utils.escape_markdown(member.name)}**.",
            mention_author=False, allowed_mentions=NO_MENTIONS)


async def setup(bot):
    await bot.add_cog(FielderLines(bot))
    print("[fielderlines] loaded: +setceleb +mylines +clearlines +settakeline +setdropline")
