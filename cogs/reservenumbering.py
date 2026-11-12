"""Post the RESERVED matches' schedule again, each with its ORIGINAL match number.

Place at cogs/reservenumbering.py and add "cogs.reservenumbering" to bot.py
(anywhere after "cogs.matchnumbers").

It does NOT read or edit any old post, does NOT touch reserves.py, the databases or any other
command. Old schedule posts can be in any format - they are ignored.

    +numberreserves [stage name] [swap A B]

  For every reserved match (stage, reserving team, opposing team) it uses the bot's own saved
  fixtures (the ones +generateschedule made and +schedule posts) to find the fixture of the same
  two teams in that stage and takes THAT fixture's match number - worked out with the same
  function +schedule uses, so it is the number +schedule <stage> <group> <round> shows.
  (If a pair meets more than once, the fixture where the reserving team is the host is preferred
  and a fixture is never used twice.)

  Step 1: a PREVIEW is shown:   `ID 12` Match 37 - Team A vs Team B - Group A - Round 3
          (so you can check each one against +schedule <stage> <group> <round>)
  Step 2: reply `yes` and a clean schedule is posted in this channel: one post per reserve
          matchday (same days as +creatematchups made), every match with its number.
          Nobody is pinged by it.

  Reserved matches with no matching fixture are listed in the preview and left out of the post.

  swap A B: if two days were POSTED the wrong way round (e.g. the bot's day 2 fixtures were sent as
  day 1 and the day 1 fixtures as day 2), add  `swap 1 2`  and those two days trade places when the
  numbers are worked out:   +numberreserves Stage 1 swap 1 2
  (can be repeated:  swap 1 2 swap 3 4). Nothing in the database is changed.

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

VERSION = "v5 - numbers from the bot's saved fixtures"
TEAM_DB = "team_registration.db"
EMBED_LIMIT = 3800


def _norm(value):
    return " ".join(str(value).strip().casefold().split())


def load_reservations():
    """([{id, host, opp, stage}], {reserve id: reserve matchday number})"""
    if not Path(RESERVE_DB).is_file():
        return [], {}
    conn = sqlite3.connect(RESERVE_DB)
    try:
        rows = conn.execute(
            "SELECT id, reserving_team, opposing_team, stage_name FROM reservations ORDER BY id"
        ).fetchall()
        try:
            days = dict(conn.execute("SELECT reservation_id, day_number FROM generated_schedule").fetchall())
        except sqlite3.Error:
            days = {}
    except sqlite3.Error:
        rows, days = [], {}
    finally:
        conn.close()
    return ([{"id": int(r[0]), "host": r[1], "opp": r[2], "stage": r[3]} for r in rows],
            {int(k): int(v) for k, v in days.items()})


def original_numbers(guild_id, reservations, swaps=None):
    """{reserve id: (match number, group name, round as posted, round in the bot)} for the regular
    fixture each reserved match is playing. Reserved matches with no fixture are simply missing.

    swaps: {day: day} - days that were posted the wrong way round trade places in the numbering.
    Numbering = stage start, then fixtures ordered by (day, group position, id) - exactly how
    +schedule numbers them."""
    swaps = swaps or {}
    if not Path(GROUP_DB).is_file():
        return {}
    result, used, stage_data = {}, set(), {}
    conn = sqlite3.connect(GROUP_DB)
    conn.row_factory = sqlite3.Row
    try:
        for res in reservations:
            skey = _norm(res["stage"])
            if skey not in stage_data:
                fixtures = conn.execute(
                    "SELECT f.id, f.group_key, f.day_no, f.team_one, f.team_two, "
                    "COALESCE(g.position, 0) AS pos, COALESCE(g.group_name, '?') AS group_name "
                    "FROM generated_fixtures f LEFT JOIN group_styles g "
                    "ON g.guild_id = f.guild_id AND g.group_key = f.group_key "
                    "WHERE f.guild_id=? AND f.group_key LIKE ? ORDER BY f.day_no, f.id",
                    (guild_id, skey + "::%"),
                ).fetchall()
                numbering = {}
                if fixtures:
                    base = regular_offset(conn, guild_id, res["stage"], fixtures[0]["group_key"], 0)
                    ordered = sorted(fixtures, key=lambda f: (swaps.get(f["day_no"], f["day_no"]),
                                                              f["pos"], f["id"]))
                    numbering = {f["id"]: base + i + 1 for i, f in enumerate(ordered)}
                stage_data[skey] = (fixtures, numbering)
            fixtures, numbering = stage_data[skey]
            pair = {_norm(res["host"]), _norm(res["opp"])}
            candidates = [f for f in fixtures
                          if f["id"] not in used
                          and {_norm(f["team_one"]), _norm(f["team_two"])} == pair]
            if not candidates:
                continue
            # prefer the leg where the reserving team is the host
            fixture = next((f for f in candidates if _norm(f["team_one"]) == _norm(res["host"])),
                           candidates[0])
            used.add(fixture["id"])
            result[res["id"]] = (numbering[fixture["id"]], fixture["group_name"],
                                 swaps.get(fixture["day_no"], fixture["day_no"]), fixture["day_no"])
    except sqlite3.Error:
        return {}
    finally:
        conn.close()
    return result


def _stadiums(guild_id):
    if not Path(GROUP_DB).is_file():
        return {}
    conn = sqlite3.connect(f"file:{Path(GROUP_DB).resolve().as_posix()}?mode=ro", uri=True)
    try:
        return {_norm(n): int(c) for n, c in conn.execute(
            "SELECT team_name, channel_id FROM team_home_stadiums WHERE guild_id=?", (guild_id,))}
    except sqlite3.Error:
        return {}
    finally:
        conn.close()


def _roles(guild_id):
    if not Path(TEAM_DB).is_file():
        return {}
    conn = sqlite3.connect(f"file:{Path(TEAM_DB).resolve().as_posix()}?mode=ro", uri=True)
    try:
        return {_norm(n): int(r) for n, r in conn.execute(
            "SELECT t.name, s.role_id FROM registered_teams t JOIN team_role_settings s "
            "ON s.team_id = t.id WHERE t.guild_id=? AND LOWER(t.status)='approved'", (guild_id,))}
    except sqlite3.Error:
        return {}
    finally:
        conn.close()


def group_by_day(items, days):
    """items: [(reserve id, ...)] -> [(day or 0, [items])] days ascending."""
    out = {}
    for item in items:
        out.setdefault(days.get(item[0], 0), []).append(item)
    return sorted(out.items())


class ReserveNumbering(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def _ask_yes(self, ctx, timeout=300):
        def check(m):
            return (m.author.id == ctx.author.id and m.channel.id == ctx.channel.id
                    and m.content.strip().casefold() in {"yes", "no", "cancel"})
        try:
            answer = await self.bot.wait_for("message", check=check, timeout=timeout)
        except asyncio.TimeoutError:
            await ctx.send("⌛ Timed out; nothing was posted.")
            return False
        if answer.content.strip().casefold() != "yes":
            await ctx.send("❌ Cancelled; nothing was posted.")
            return False
        return True

    @commands.command(name="numberreserves", aliases=["numberreserved", "addreservenumbers"])
    @commands.guild_only()
    async def numberreserves(self, ctx, *, stage_name: str = None):
        """Post the reserved matches' schedule with their original match numbers."""
        if not await owner_or_whitelisted(ctx, app_owner=True):
            await ctx.reply("Only the bot owner or a role whitelisted for this command can use it.",
                            mention_author=False)
            return

        swaps = {}
        if stage_name:
            for a, b in re.findall(r"\bswap\s+(\d+)\s+(\d+)", stage_name, re.IGNORECASE):
                swaps[int(a)], swaps[int(b)] = int(b), int(a)
            stage_name = re.sub(r"\bswap\s+\d+\s+\d+", "", stage_name, flags=re.IGNORECASE).strip() or None

        reservations, days = load_reservations()
        if stage_name:
            reservations = [r for r in reservations if _norm(r["stage"]) == _norm(stage_name)]
        if not reservations:
            await ctx.send("There are no reserved matches" + (f" for **{stage_name}**." if stage_name else "."))
            return

        numbers = original_numbers(ctx.guild.id, reservations, swaps)

        # stages in the order they first appear
        stage_order, names = [], {}
        for r in reservations:
            key = _norm(r["stage"])
            if key not in names:
                names[key] = r["stage"]
                stage_order.append(key)

        # ---- preview
        preview = [f"**Preview - reserved matches with their original match numbers** ({VERSION})"]
        if swaps:
            pairs = sorted({tuple(sorted(kv)) for kv in swaps.items()})
            preview.append("Days swapped: " + ", ".join(f"{a} ↔ {b}" for a, b in pairs))
        missing_total = matched_total = 0
        for key in stage_order:
            stage_res = [r for r in reservations if _norm(r["stage"]) == key]
            preview.append(f"\n**{names[key]}**")
            for r in sorted(stage_res, key=lambda r: (numbers.get(r["id"], (10 ** 9,))[0], r["id"])):
                host = discord.utils.escape_markdown(r["host"])
                opp = discord.utils.escape_markdown(r["opp"])
                if r["id"] in numbers:
                    n, group, rnd, db_rnd = numbers[r["id"]]
                    round_text = f"Round {rnd}" + (f" (bot's round {db_rnd})" if rnd != db_rnd else "")
                    preview.append(f"`ID {r['id']}` **Match {n}** · {host} vs {opp} · Group {group} · {round_text}")
                    matched_total += 1
                else:
                    preview.append(f"`ID {r['id']}` ⚠️ no fixture found · {host} vs {opp}")
                    missing_total += 1
        if not matched_total:
            preview.append("\n❌ None of the reserved matches could be matched to a fixture, so there is "
                           "nothing to post. Check that the stage's fixtures exist (`+generateschedule`) and "
                           "that the team names are the same as in the groups.")
        else:
            preview.append(f"\nCheck a few against `+schedule <stage> <group> <round>`. Reply `yes` to post the "
                           f"numbered schedule for **{matched_total}** match(es) in this channel"
                           + (f" ({missing_total} without a fixture will be left out)" if missing_total else "")
                           + ", or `no` to cancel. (5-minute timeout)")
        chunk = ""
        for line in "\n".join(preview).split("\n"):
            if len(chunk) + len(line) + 1 > 1900:
                await ctx.send(chunk, allowed_mentions=discord.AllowedMentions.none())
                chunk = ""
            chunk += ("\n" if chunk else "") + line
        if chunk:
            await ctx.send(chunk, allowed_mentions=discord.AllowedMentions.none())
        if not matched_total or not await self._ask_yes(ctx):
            return

        # ---- post
        stadiums, roles = _stadiums(ctx.guild.id), _roles(ctx.guild.id)
        color = discord.Color.from_rgb(35, 63, 110)
        none = discord.AllowedMentions.none()
        posted = 0
        for key in stage_order:
            stage_res = [r for r in reservations if _norm(r["stage"]) == key and r["id"] in numbers]
            if not stage_res:
                continue
            await ctx.send(embed=discord.Embed(
                title=f"✦ {names[key].upper()} — RESERVED MATCHES",
                color=color,
                description=(f"**Total reserved matches:** {len(stage_res)}\n"
                             "Each reserved match keeps its original fixture number."),
            ), allowed_mentions=none)
            items = [(r["id"], r) for r in stage_res]
            for day, day_items in group_by_day(items, days):
                day_items.sort(key=lambda item: numbers[item[0]][0])
                blocks = []
                for rid, r in day_items:
                    n = numbers[rid][0]
                    channel_id = stadiums.get(_norm(r["host"]))
                    channel_text = f"<#{channel_id}>" if channel_id and ctx.guild.get_channel(channel_id) else "—"
                    role_a, role_b = roles.get(_norm(r["host"])), roles.get(_norm(r["opp"]))
                    team_line = (f"<@&{role_a}> vs <@&{role_b}>"
                                 if role_a and role_b and ctx.guild.get_role(role_a) and ctx.guild.get_role(role_b)
                                 else f"{discord.utils.escape_markdown(r['host'])} vs {discord.utils.escape_markdown(r['opp'])}")
                    blocks.append(
                        f"`ID {rid}` **Match {n}** · **{discord.utils.escape_markdown(r['host'])}** vs "
                        f"**{discord.utils.escape_markdown(r['opp'])}**\n"
                        f"🏟️ **Host:** {discord.utils.escape_markdown(r['host'])} — {channel_text}\n"
                        f"👥 {team_line}")
                title = f"◆ RESERVE MATCHDAY {day:02d}" if day else "◆ RESERVED MATCHES"
                page, size = [], 0
                for block in blocks:
                    if page and size + len(block) + 2 > EMBED_LIMIT:
                        await ctx.send(embed=discord.Embed(title=title, color=color, description="\n\n".join(page)),
                                       allowed_mentions=none)
                        page, size = [], 0
                    page.append(block)
                    size += len(block) + 2
                if page:
                    await ctx.send(embed=discord.Embed(title=title, color=color, description="\n\n".join(page)),
                                   allowed_mentions=none)
            posted += len(stage_res)
        await ctx.send(f"✅ Posted **{posted}** reserved match(es) with their original match numbers.")


async def setup(bot):
    await bot.add_cog(ReserveNumbering(bot))
