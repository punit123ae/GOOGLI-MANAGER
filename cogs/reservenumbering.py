"""Put the ORIGINAL match number on the reserved matches' schedule posts.

Place at cogs/reservenumbering.py and add "cogs.reservenumbering" to bot.py
(anywhere after "cogs.matchnumbers").

It does NOT touch reserves.py, the databases or any existing command.

A reserved match is one of the normal fixtures played early, so it keeps the number that
fixture has in the +schedule posts. For every reserved match the cog:

  1. takes the reserving team + opposing team + stage,
  2. finds that pair's fixture in the stage's generated fixtures (the host side is preferred
     when a pair meets more than once, and a fixture is never used twice),
  3. works out its number exactly like +schedule does (same function, same offsets).

    +numberreserves
        Edits the reserve MATCHDAY posts saved by +creatematchups (every stage) so each line shows

            `ID 12` **Match 37** · **Team A** vs **Team B**

        Posts keep their message ID and nobody is pinged by an edit. A wrong number added by an
        earlier version is replaced; a reserved match with no matching fixture has any number
        removed and is listed in the reply.

    +numberreserves #schedule-channel
        Same, but also scans that channel's recent history for the bot's reserve MATCHDAY
        posts (use it if the saved post IDs are gone, e.g. after +resetmatchups).

Safe to run again any time.

NOTE: +autoresult / +rebuildresults / +reservenumbers still use the old "after the last matchday"
numbers from matchnumbers.py; this cog does not change them.
"""

import asyncio
import re
import sqlite3
from pathlib import Path

import discord
from discord.ext import commands

from .matchnumbers import GROUP_DB, RESERVE_DB, regular_offset
from .utility import owner_or_whitelisted

# First line of a reserve entry:  `ID 12` **Team A** vs **Team B**
# (optionally already carrying a number:  `ID 12` **Match 91** · **Team A** ...)
ID_LINE_RE = re.compile(r"^(`ID\s+(\d+)`)[ \t]*(?:\*\*Match\s+\d+\*\*[ \t]*·[ \t]*)?(.*)$")
SCAN_LIMIT = 500
EMBED_LIMIT = 4096


def _norm(value):
    return " ".join(str(value).strip().casefold().split())


def original_numbers(guild_id):
    """{reservation id: the match number of the regular fixture it is playing}."""
    if not Path(RESERVE_DB).is_file() or not Path(GROUP_DB).is_file():
        return {}
    rconn = sqlite3.connect(RESERVE_DB)
    try:
        reservations = rconn.execute(
            "SELECT id, reserving_team, opposing_team, stage_name FROM reservations ORDER BY id"
        ).fetchall()
    except sqlite3.Error:
        reservations = []
    finally:
        rconn.close()

    numbers, used, fixtures_by_stage = {}, set(), {}
    conn = sqlite3.connect(GROUP_DB)
    conn.row_factory = sqlite3.Row
    try:
        for reserve_id, host, opponent, stage in reservations:
            skey = _norm(stage)
            if skey not in fixtures_by_stage:
                fixtures_by_stage[skey] = conn.execute(
                    "SELECT id, group_key, day_no, team_one, team_two FROM generated_fixtures "
                    "WHERE guild_id=? AND group_key LIKE ? ORDER BY day_no, id",
                    (guild_id, skey + "::%"),
                ).fetchall()
            pair = {_norm(host), _norm(opponent)}
            candidates = [f for f in fixtures_by_stage[skey]
                          if f["id"] not in used
                          and {_norm(f["team_one"]), _norm(f["team_two"])} == pair]
            if not candidates:
                continue
            # prefer the leg where the reserving team is the host
            fixture = next((f for f in candidates if _norm(f["team_one"]) == _norm(host)), candidates[0])
            used.add(fixture["id"])
            before = conn.execute(
                "SELECT COUNT(*) FROM generated_fixtures WHERE guild_id=? AND group_key=? "
                "AND day_no=? AND id<?",
                (guild_id, fixture["group_key"], fixture["day_no"], fixture["id"]),
            ).fetchone()[0]
            numbers[int(reserve_id)] = (
                regular_offset(conn, guild_id, stage, fixture["group_key"], fixture["day_no"]) + before + 1
            )
    except sqlite3.Error:
        return {}
    finally:
        conn.close()
    return numbers


def _saved_posts():
    """[(stage_name, channel_id, [message ids])] for every generated reserve schedule."""
    if not Path(RESERVE_DB).is_file():
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
    """Set `**Match N** ·` on every `ID n` line (or remove it if there is no number).

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
            new_line = f"{m.group(1)} {m.group(3)}"
        else:
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
        """Put each reserved match's original match number on its schedule post."""
        if not await owner_or_whitelisted(ctx, app_owner=True):
            await ctx.reply("Only the bot owner or a role whitelisted for this command can use it.",
                            mention_author=False)
            return

        numbers = original_numbers(ctx.guild.id)
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
            lines.append(f"✅ Updated **{numbered_lines}** reserved match line(s) across "
                         f"**{edited_posts}** schedule post(s) with their original match numbers.")
        if already_ok:
            lines.append(f"ℹ️ {already_ok} reserved match(es) already showed the correct number.")
        if not edited_posts and not already_ok and not no_number:
            lines.append("❌ I couldn't find any reserve schedule posts to update.")
            if channel is None:
                lines.append("Run `+numberreserves #the-schedule-channel` so I can scan that channel for them.")
        if no_number:
            lines.append("⚠️ No original fixture found for reserve ID(s) "
                         + ", ".join(f"`{i}`" for i in sorted(no_number))
                         + " (not in the same stage's groups, or the pair has no free fixture left). "
                           "They show no number.")
        if too_long:
            lines.append("⚠️ Skipped (post would be too long): " + ", ".join(too_long[:5]))
        if failed:
            lines.append("⚠️ Couldn't edit: " + ", ".join(failed[:5]))
        await status.edit(content="\n".join(lines)[:1990], allowed_mentions=discord.AllowedMentions.none())


async def setup(bot):
    await bot.add_cog(ReserveNumbering(bot))
