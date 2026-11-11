"""Shared match numbering for GOOGLI MANAGER.

Place at cogs/matchnumbers.py and add "cogs.matchnumbers" to bot.py.

One running number across the whole tournament, stage by stage (stages in the order
they were created):

    Stage block = regular fixtures first (round by round, group by group)
                  then that stage's reserved matches (in reserve ID order)

Example (3 groups x 5 matches per round):
    Round 1 -> 1-15, Round 2 -> 16-30, Round 3 -> 31-45 ...
    then the stage's reserved matches continue the numbers, then the next stage starts.

Used by: +schedule (groups.py), +creatematchups (reserves.py), +autoresult and
+rebuildresults (autoresults.py), and the +reservenumbers command below.
"""

import sqlite3
from pathlib import Path

import discord
from discord.ext import commands

GROUP_DB = "groups_schedule.db"
RESERVE_DB = Path(__file__).resolve().parent.parent / "reserve_matches.db"


def _norm(value):
    return " ".join(str(value).strip().casefold().split())


def _reserve_ids_by_stage():
    """{normalized stage name: [reservation ids in order]}"""
    if not RESERVE_DB.is_file():
        return {}
    conn = sqlite3.connect(RESERVE_DB)
    try:
        rows = conn.execute("SELECT id, stage_name FROM reservations ORDER BY id").fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        conn.close()
    out = {}
    for reserve_id, stage in rows:
        out.setdefault(_norm(stage), []).append(int(reserve_id))
    return out


def stage_blocks(conn, guild_id):
    """[(stage_key, stage_name, first_number, regular_count, reserve_ids)] in creation order.

    `conn` is a connection to groups_schedule.db (row_factory = sqlite3.Row).
    """
    reserves = _reserve_ids_by_stage()
    blocks, start = [], 1
    stages = conn.execute(
        "SELECT stage_key, stage_name FROM tournament_stages WHERE guild_id=? ORDER BY created_at, rowid",
        (guild_id,),
    ).fetchall()
    for row in stages:
        regular = conn.execute(
            "SELECT COUNT(*) FROM generated_fixtures WHERE guild_id=? AND group_key LIKE ?",
            (guild_id, row["stage_key"] + "::%"),
        ).fetchone()[0]
        reserve_ids = reserves.get(_norm(row["stage_name"]), [])
        blocks.append((row["stage_key"], row["stage_name"], start, regular, reserve_ids))
        start += regular + len(reserve_ids)
    return blocks


def regular_offset(conn, guild_id, stage_name, group_key, round_no):
    """Number of matches that come before this group's `round_no` (add the 1-based
    position of the match inside the post to get its match number)."""
    skey = _norm(stage_name)
    start = next((b[2] for b in stage_blocks(conn, guild_id) if b[0] == skey), 1)
    row = conn.execute(
        "SELECT position FROM group_styles WHERE guild_id=? AND group_key=?",
        (guild_id, group_key),
    ).fetchone()
    position = row[0] if row else 0
    before = conn.execute(
        "SELECT COUNT(*) FROM generated_fixtures f "
        "JOIN group_styles g ON g.guild_id=f.guild_id AND g.group_key=f.group_key "
        "WHERE f.guild_id=? AND f.group_key LIKE ? "
        "AND (f.day_no < ? OR (f.day_no = ? AND g.position < ?))",
        (guild_id, skey + "::%", round_no, round_no, position),
    ).fetchone()[0]
    return start - 1 + before


def reserve_numbers(conn, guild_id):
    """{reservation id: match number}"""
    out = {}
    for _key, _name, start, regular, reserve_ids in stage_blocks(conn, guild_id):
        for index, reserve_id in enumerate(reserve_ids):
            out[reserve_id] = start + regular + index
    return out


def reserve_numbers_for(guild_id):
    """Same as reserve_numbers() but opens groups_schedule.db itself."""
    if not Path(GROUP_DB).is_file():
        return {}
    conn = sqlite3.connect(GROUP_DB)
    conn.row_factory = sqlite3.Row
    try:
        return reserve_numbers(conn, guild_id)
    except sqlite3.Error:
        return {}
    finally:
        conn.close()


class MatchNumbers(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="reservenumbers", aliases=["reservenos"])
    @commands.guild_only()
    async def reservenumbers(self, ctx, *, stage_name: str = None):
        """List every reserved match with its tournament match number."""
        numbers = reserve_numbers_for(ctx.guild.id)
        if not RESERVE_DB.is_file() or not numbers:
            await ctx.send("There are no reserved matches yet.")
            return
        conn = sqlite3.connect(RESERVE_DB)
        try:
            rows = conn.execute(
                "SELECT id, reserving_team, opposing_team, stage_name FROM reservations ORDER BY id"
            ).fetchall()
        finally:
            conn.close()
        if stage_name:
            rows = [r for r in rows if _norm(r[3]) == _norm(stage_name)]
        rows = sorted((r for r in rows if int(r[0]) in numbers), key=lambda r: numbers[int(r[0])])
        if not rows:
            await ctx.send("No reserved matches found for that stage.")
            return
        lines = ["**Reserved matches (match number order)**"]
        for reserve_id, host, opponent, stage in rows:
            lines.append(f"**Match {numbers[int(reserve_id)]}** — {discord.utils.escape_markdown(host)} vs "
                         f"{discord.utils.escape_markdown(opponent)} · *{stage}* · `ID {reserve_id}`")
        chunk = ""
        for line in lines:
            if len(chunk) + len(line) + 1 > 1900:
                await ctx.send(chunk, allowed_mentions=discord.AllowedMentions.none())
                chunk = ""
            chunk += ("\n" if chunk else "") + line
        if chunk:
            await ctx.send(chunk, allowed_mentions=discord.AllowedMentions.none())


async def setup(bot):
    await bot.add_cog(MatchNumbers(bot))
