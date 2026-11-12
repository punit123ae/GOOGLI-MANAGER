"""Add tournament match numbers to the reserved matches' schedule posts.

Place at cogs/reservenumbering.py and add "cogs.reservenumbering" to bot.py
(anywhere after "cogs.matchnumbers").

It does NOT touch reserves.py, the databases or any existing command.

    +numberreserves
        Finds the reserve MATCHDAY posts saved by +creatematchups (every stage), reads which
        reserved match (`ID n`) each line is, looks up its tournament match number with the
        same numbering used by +schedule / +autoresult / +reservenumbers
        (stage block = regular fixtures first, then that stage's reserves in reserve-ID order)
        and EDITS those posts in place so each line shows its number:

            `ID 12` **Match 91** · **Team A** vs **Team B**

        Edited posts keep their message ID and nobody is pinged by an edit.

    +numberreserves #schedule-channel
        Same, but also scans that channel's recent history for the bot's reserve MATCHDAY
        posts. Use this if the schedule was reset (+resetmatchups) or the saved post IDs are
        gone, so the bot no longer remembers where the posts are.

Safe to run again any time: numbers already on a line are refreshed, not duplicated.
Use +reservenumbers first if you only want to LOOK at the numbers.
"""

import asyncio
import re
import sqlite3

import discord
from discord.ext import commands

from .matchnumbers import RESERVE_DB, reserve_numbers_for
from .utility import owner_or_whitelisted

# First line of a reserve entry:  `ID 12` **Team A** vs **Team B**
# (optionally already carrying a number:  `ID 12` **Match 91** · **Team A** ...)
ID_LINE_RE = re.compile(r"^(`ID\s+(\d+)`)[ \t]*(?:\*\*Match\s+\d+\*\*[ \t]*·[ \t]*)?(.*)$")
SCAN_LIMIT = 500
EMBED_LIMIT = 4096


def _saved_posts():
    """[(stage_name, channel_id, [message ids])] for every generated reserve schedule."""
    if not RESERVE_DB.is_file():
        return []
    conn = sqlite3.connect(RESERVE_DB)
    try:
        rows = conn.execute(
            "SELECT stage_name, channel_id, message_ids FROM reserve_stage_schedule_state "
            "WHERE is_generated = 1 AND message_ids IS NOT NULL AND message_ids <> ''"
        ).fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        conn.close()
    out = []
    for stage, channel_id, raw in rows:
        ids = [int(x) for x in str(raw).split(",") if x.strip().isdigit()]
        if channel_id and ids:
            out.append((stage, int(channel_id), ids))
    return out


def number_description(text, numbers):
    """Add/refresh `**Match N** ·` on every `ID n` line.

    Returns (new_text, total_reserve_lines, changed_lines, ids_without_a_number).
    """
    lines = text.split("\n")
    total, changed, missing = 0, 0, []
    for i, line in enumerate(lines):
        m = ID_LINE_RE.match(line)
        if not m:
            continue
        total += 1
        reserve_id = int(m.group(2))
        number = numbers.get(reserve_id)
        if number is None:
            missing.append(reserve_id)
            continue
        new_line = f"{m.group(1)} **Match {number}** · {m.group(3)}"
        if new_line != line:
            lines[i] = new_line
            changed += 1
    return "\n".join(lines), total, changed, missing


class ReserveNumbering(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="numberreserves", aliases=["numberreserved", "addreservenumbers"])
    @commands.guild_only()
    async def numberreserves(self, ctx, channel: discord.TextChannel = None):
        """Add match numbers to the reserved matches' schedule posts."""
        if not await owner_or_whitelisted(ctx, app_owner=True):
            await ctx.reply("Only the bot owner or a role whitelisted for this command can use it.",
                            mention_author=False)
            return

        numbers = reserve_numbers_for(ctx.guild.id)
        if not numbers:
            await ctx.send("There are no reserved matches (or their stage isn't registered), "
                           "so there are no match numbers to add.")
            return

        status = await ctx.send("⏳ Looking for the reserve schedule posts...")

        # 1) posts remembered by +creatematchups, 2) optional scan of a channel
        targets, seen = [], set()
        for _stage, channel_id, message_ids in _saved_posts():
            saved_channel = ctx.guild.get_channel(channel_id)
            if saved_channel is None:
                continue
            for message_id in message_ids:
                try:
                    message = await saved_channel.fetch_message(message_id)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    continue
                if message.id not in seen:
                    seen.add(message.id)
                    targets.append(message)
        if channel is not None:
            async for message in channel.history(limit=SCAN_LIMIT, oldest_first=True):
                if message.id not in seen and message.author.id == self.bot.user.id and message.embeds:
                    seen.add(message.id)
                    targets.append(message)

        edited_posts = numbered_lines = already_ok = 0
        no_number, too_long, failed = set(), [], []
        for message in targets:
            if message.author.id != self.bot.user.id or not message.embeds:
                continue
            new_embeds, total_in_post, changed_in_post = [], 0, 0
            for embed in message.embeds:
                new_embed = embed.copy()
                if embed.description:
                    description, total, changed, missing = number_description(embed.description, numbers)
                    total_in_post += total
                    changed_in_post += changed
                    no_number.update(missing)
                    new_embed.description = description
                new_embeds.append(new_embed)
            if total_in_post == 0:
                continue  # intro post or something unrelated
            if changed_in_post == 0:
                already_ok += total_in_post
                continue
            if any(len(e.description or "") > EMBED_LIMIT for e in new_embeds):
                too_long.append(message.jump_url)
                continue
            try:
                await message.edit(embeds=new_embeds, allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException as exc:
                failed.append(f"{message.jump_url} ({type(exc).__name__})")
                continue
            edited_posts += 1
            numbered_lines += changed_in_post
            await asyncio.sleep(0.5)  # stay well inside Discord's edit rate limit

        lines = []
        if edited_posts:
            lines.append(f"✅ Added match numbers to **{numbered_lines}** reserved match(es) "
                         f"across **{edited_posts}** schedule post(s).")
        if already_ok:
            lines.append(f"ℹ️ {already_ok} reserved match(es) already showed the correct number.")
        if not edited_posts and not already_ok:
            lines.append("❌ I couldn't find any reserve schedule posts to update.")
            if channel is None:
                lines.append("Run `+numberreserves #the-schedule-channel` so I can scan that channel for them.")
        if no_number:
            lines.append("⚠️ No match number available for reserve ID(s) "
                         + ", ".join(f"`{i}`" for i in sorted(no_number))
                         + " (their stage isn't registered or they were deleted).")
        if too_long:
            lines.append("⚠️ Skipped (post would be too long): " + ", ".join(too_long[:5]))
        if failed:
            lines.append("⚠️ Couldn't edit: " + ", ".join(failed[:5]))
        lines.append("See the full list any time with `+reservenumbers`.")
        await status.edit(content="\n".join(lines)[:1990], allowed_mentions=discord.AllowedMentions.none())


async def setup(bot):
    await bot.add_cog(ReserveNumbering(bot))
