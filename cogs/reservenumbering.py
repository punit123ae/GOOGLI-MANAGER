"""Post the RESERVED matches' schedule again, each with its ORIGINAL match number.

Place at cogs/reservenumbering.py and add "cogs.reservenumbering" to bot.py
(anywhere after "cogs.matchnumbers").

It does NOT read or edit any old post, does NOT touch reserves.py, the databases or any other
command. Old schedule posts can be in any format - they are ignored.

    +numberreserves [stage name] [swap A B] [ping]

  For every reserved match (stage, reserving team, opposing team) it uses the bot's own saved
  fixtures (the ones +generateschedule made and +schedule posts) to find the fixture of the same
  two teams in that stage and takes THAT fixture's match number - worked out with the same
  function +schedule uses, so it is the number +schedule <stage> <group> <round> shows.
  (If a pair meets more than once, the fixture where the reserving team is the host is preferred
  and a fixture is never used twice.)

  Step 1: a PREVIEW is shown, reserve day by reserve day, lowest match number first:
          Match 37 - Team A vs Team B - Group A (ID 12)
  Step 2: reply `yes` and the schedule is posted in the SAME FORMAT AS +schedule:

          # KASHMIR TITANS - STAGE 1
          ## RESERVE DAY 1
          # GROUP A

          **Match 37**
          - @Team A vs @Team B in #stadium
          **Rest:** @Team C, @Team D

  * One post per reserve day per group, in match-number order (low -> high).
  * The number of reserve days = the reserve limit set with +setreserves (so a limit of 4 gives
    RESERVE DAY 1 - 4, even if a day has no matches).
  * EVERY team of the group is on every day: either playing a match or in the Rest line.
  * The group letter (A / B / C ...) is the group the fixture really belongs to.
  * Days: if +creatematchups already saved days that fit the limit, the same matches stay on the
    same days. Otherwise the days are worked out so lower match numbers come first. A team is never
    put twice on one day unless it has more reserved matches than there are days - then it plays
    twice on a day rather than a day being added beyond the limit (the preview warns about it).
  * Roles are shown as mentions but NOBODY is pinged. Add the word `ping` to ping the roles like
    +schedule does:   +numberreserves Stage 1 ping

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

VERSION = "v6 - +schedule format, reserve days = limit, rest lines"
TEAM_DB = "team_registration.db"
TITLE = "KASHMIR TITANS"          # same heading +schedule uses
MESSAGE_LIMIT = 1900


def _norm(value):
    return " ".join(str(value).strip().casefold().split())


def load_reservations():
    """([{id, host, opp, stage}], {reserve id: reserve matchday number saved by +creatematchups})"""
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


def reserve_limit():
    """The number set with +setreserves (0 if not set)."""
    if not Path(RESERVE_DB).is_file():
        return 0
    conn = sqlite3.connect(RESERVE_DB)
    try:
        row = conn.execute("SELECT total_limit FROM reserve_settings WHERE id = 1").fetchone()
        return int(row[0]) if row else 0
    except sqlite3.Error:
        return 0
    finally:
        conn.close()


def original_numbers(guild_id, reservations, swaps=None):
    """{reserve id: (match number, group name, round as posted, round in the bot, group key)} for the
    regular fixture each reserved match is playing. Reserved matches with no fixture are missing.

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
                                 swaps.get(fixture["day_no"], fixture["day_no"]), fixture["day_no"],
                                 fixture["group_key"])
    except sqlite3.Error:
        return {}
    finally:
        conn.close()
    return result


def stage_groups_and_teams(guild_id, stage_name):
    """([(group_key, group_name, position)] in group order, {group_key: [team names]})"""
    if not Path(GROUP_DB).is_file():
        return [], {}
    skey = _norm(stage_name)
    conn = sqlite3.connect(f"file:{Path(GROUP_DB).resolve().as_posix()}?mode=ro", uri=True)
    try:
        groups = [(k, n, int(p)) for k, n, p in conn.execute(
            "SELECT group_key, group_name, position FROM group_styles WHERE guild_id=? AND group_key LIKE ? "
            "ORDER BY position", (guild_id, skey + "::%"))]
        teams = {}
        for name, key in conn.execute(
                "SELECT team_name, group_key FROM team_groups WHERE guild_id=? AND team_name_key LIKE ? "
                "ORDER BY team_name COLLATE NOCASE", (guild_id, skey + "::%")):
            teams.setdefault(key, []).append(name)
        return groups, teams
    except sqlite3.Error:
        return [], {}
    finally:
        conn.close()


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


# --------------------------------------------------------------------------- reserve-day planning
def _search_days(matches, day_count, node_cap=200_000):
    """Backtracking, matches already in number order, lowest day first. A team never plays twice on a
    day. Returns [day per match] or None when it does not fit / the search gets too long."""
    teams = [(_norm(m["host"]), _norm(m["opp"])) for m in matches]
    busy = [set() for _ in range(day_count)]
    chosen = [0] * len(matches)
    nodes = 0

    def go(i):
        nonlocal nodes
        nodes += 1
        if nodes > node_cap:
            return False
        if i == len(matches):
            return True
        a, b = teams[i]
        tried_empty = False
        for d in range(day_count):
            if a in busy[d] or b in busy[d]:
                continue
            if not busy[d]:                 # all empty days are equivalent
                if tried_empty:
                    continue
                tried_empty = True
            busy[d].update((a, b))
            chosen[i] = d + 1
            if go(i + 1):
                return True
            busy[d].difference_update((a, b))
        return False

    return chosen if go(0) else None


def plan_days(matches, saved_days, limit):
    """matches: [{id, host, opp, number}] of ONE stage. Returns ({id: day}, day_count, warnings).

    1. +creatematchups' saved days are kept when every one of these matches has one, they fit inside
       the limit and nobody plays twice on a day.
    2. Otherwise a fresh plan with exactly `limit` days (lowest match numbers on the earliest days).
    3. If that is impossible (a team has more matches than there are days) the extra matches go on
       the least busy day for that team - no day beyond the limit is ever added."""
    warnings = []
    ordered = sorted(matches, key=lambda m: (m["number"], m["id"]))
    if not ordered:
        return {}, limit, warnings

    saved_ok = all(m["id"] in saved_days for m in ordered)
    if saved_ok:
        seen, clash = set(), False
        for m in ordered:
            for team in (_norm(m["host"]), _norm(m["opp"])):
                if (saved_days[m["id"]], team) in seen:
                    clash = True
                seen.add((saved_days[m["id"]], team))
        top = max(saved_days[m["id"]] for m in ordered)
        if not clash and (limit <= 0 or top <= limit):
            return {m["id"]: saved_days[m["id"]] for m in ordered}, max(limit, top), warnings

    days_needed = max(limit, 1)
    if limit <= 0:
        # no limit stored - use the smallest number of days that works
        for days_needed in range(1, len(ordered) + 1):
            if _search_days(ordered, days_needed):
                break
    found = _search_days(ordered, days_needed)
    if found:
        return {m["id"]: d for m, d in zip(ordered, found)}, days_needed, warnings

    # more matches for a team than days: keep inside the limit, repeat a team on a day if it must
    plan, busy, load = {}, [dict() for _ in range(days_needed)], [0] * days_needed
    for m in ordered:
        a, b = _norm(m["host"]), _norm(m["opp"])
        best = min(range(days_needed),
                   key=lambda d: (busy[d].get(a, 0) + busy[d].get(b, 0), load[d], d))
        plan[m["id"]] = best + 1
        busy[best][a] = busy[best].get(a, 0) + 1
        busy[best][b] = busy[best].get(b, 0) + 1
        load[best] += 1
    repeats = sorted({m["host"] for m in ordered if any(
        busy[d].get(_norm(m["host"]), 0) > 1 for d in range(days_needed))} |
        {m["opp"] for m in ordered if any(busy[d].get(_norm(m["opp"]), 0) > 1 for d in range(days_needed))})
    warnings.append(f"The matches could not all be placed with every team playing at most once a day inside "
                    f"{days_needed} reserve day(s), so these teams play more than once on a day: "
                    + (", ".join(repeats) or "?"))
    return plan, days_needed, warnings


# --------------------------------------------------------------------------- text in +schedule format
def _chunks(blocks, limit=MESSAGE_LIMIT):
    """Join text blocks into messages shorter than `limit`."""
    out, current = [], ""
    for block in blocks:
        addition = block if not current else "\n" + block
        if current and len(current) + len(addition) > limit:
            out.append(current)
            current = block
        else:
            current += addition
    if current:
        out.append(current)
    return out


def build_group_post(stage_name, day, group_name, group_matches, rest_teams, role_text, channel_text):
    """Exactly the +schedule layout, for one reserve day of one group.

    group_matches: [{number, host, opp}] already in match-number order
    rest_teams: [team names] not playing that day
    role_text(name) / channel_text(host name): how a team / stadium is shown
    Returns a list of message strings (normally one)."""
    head = [f"# {TITLE} — {stage_name.upper()}", f"## RESERVE DAY {day}", f"# GROUP {group_name}"]
    blocks = ["\n".join(head)]
    for m in group_matches:
        blocks.append(f"\n**Match {m['number']}**\n- {role_text(m['host'])} vs {role_text(m['opp'])} "
                      f"in {channel_text(m['host'])}")
    if not group_matches:
        blocks.append("\n*No reserve matches in this group on this day.*")
    if rest_teams:
        blocks.append("**Rest:** " + ", ".join(role_text(t) for t in rest_teams))
    return _chunks(blocks)


def build_day_groups(day_matches, groups, group_teams):
    """Order of the group posts of one day and who rests in each.

    day_matches: [{number, host, opp, group_key}] ; groups: [(key, name, position)]
    Returns [(group_name, [matches by number], [resting teams])] - groups with the lowest match
    number first, groups with no match that day last (in group order)."""
    playing = {_norm(t) for m in day_matches for t in (m["host"], m["opp"])}
    sections = []
    for key, name, position in groups:
        mine = sorted((m for m in day_matches if m["group_key"] == key), key=lambda m: m["number"])
        rest = [t for t in group_teams.get(key, []) if _norm(t) not in playing]
        sections.append(((mine[0]["number"] if mine else 10 ** 9, position), name, mine, rest))
    sections.sort(key=lambda s: s[0])
    return [(name, mine, rest) for _k, name, mine, rest in sections]


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
        """Post the reserved matches' schedule (+schedule format) with their original match numbers."""
        if not await owner_or_whitelisted(ctx, app_owner=True):
            await ctx.reply("Only the bot owner or a role whitelisted for this command can use it.",
                            mention_author=False)
            return

        swaps, ping = {}, False
        if stage_name:
            for a, b in re.findall(r"\bswap\s+(\d+)\s+(\d+)", stage_name, re.IGNORECASE):
                swaps[int(a)], swaps[int(b)] = int(b), int(a)
            stage_name = re.sub(r"\bswap\s+\d+\s+\d+", "", stage_name, flags=re.IGNORECASE).strip()
            if re.search(r"(?:^|\s)ping\s*$", stage_name, re.IGNORECASE):
                ping = True
                stage_name = re.sub(r"(?:^|\s)ping\s*$", "", stage_name, flags=re.IGNORECASE).strip()
            stage_name = stage_name or None

        reservations, saved_days = load_reservations()
        if stage_name:
            reservations = [r for r in reservations if _norm(r["stage"]) == _norm(stage_name)]
        if not reservations:
            await ctx.send("There are no reserved matches" + (f" for **{stage_name}**." if stage_name else "."))
            return

        numbers = original_numbers(ctx.guild.id, reservations, swaps)
        limit = reserve_limit()

        # stages in the order they first appear
        stage_order, names = [], {}
        for r in reservations:
            key = _norm(r["stage"])
            if key not in names:
                names[key] = r["stage"]
                stage_order.append(key)

        # ---- reserve days per stage
        plans = {}      # stage key -> (matches, {id: day}, day_count, warnings)
        for key in stage_order:
            matches = [{"id": r["id"], "host": r["host"], "opp": r["opp"], "number": numbers[r["id"]][0],
                        "group_key": numbers[r["id"]][4], "group": numbers[r["id"]][1],
                        "round": numbers[r["id"]][2], "db_round": numbers[r["id"]][3]}
                       for r in reservations if _norm(r["stage"]) == key and r["id"] in numbers]
            day_of, day_count, warnings = plan_days(matches, saved_days, limit)
            plans[key] = (matches, day_of, day_count, warnings)

        # ---- preview
        preview = [f"**Preview - reserved matches by reserve day, lowest match number first** ({VERSION})"]
        if swaps:
            pairs = sorted({tuple(sorted(kv)) for kv in swaps.items()})
            preview.append("Days swapped: " + ", ".join(f"{a} ↔ {b}" for a, b in pairs))
        missing_total = matched_total = 0
        for key in stage_order:
            matches, day_of, day_count, warnings = plans[key]
            preview.append(f"\n**{names[key]}** — {day_count} reserve day(s)")
            for w in warnings:
                preview.append(f"⚠️ {w}")
            for day in range(1, day_count + 1):
                todays = sorted((m for m in matches if day_of[m["id"]] == day), key=lambda m: m["number"])
                preview.append(f"__Reserve Day {day}__" + ("" if todays else " — no matches (everyone rests)"))
                for m in todays:
                    host = discord.utils.escape_markdown(m["host"])
                    opp = discord.utils.escape_markdown(m["opp"])
                    extra = f" · bot's round {m['db_round']}" if m["round"] != m["db_round"] else ""
                    preview.append(f"`ID {m['id']}` **Match {m['number']}** · {host} vs {opp} · "
                                   f"Group {m['group']}{extra}")
                    matched_total += 1
            for r in sorted((r for r in reservations if _norm(r["stage"]) == key and r["id"] not in numbers),
                            key=lambda r: r["id"]):
                preview.append(f"`ID {r['id']}` ⚠️ no fixture found · "
                               f"{discord.utils.escape_markdown(r['host'])} vs {discord.utils.escape_markdown(r['opp'])}")
                missing_total += 1
        if not matched_total:
            preview.append("\n❌ None of the reserved matches could be matched to a fixture, so there is "
                           "nothing to post. Check that the stage's fixtures exist (`+generateschedule`) and "
                           "that the team names are the same as in the groups.")
        else:
            preview.append(f"\nCheck a few against `+schedule <stage> <group> <round>`. Reply `yes` to post "
                           f"**{matched_total}** match(es) in the `+schedule` format"
                           + (" (roles WILL be pinged)" if ping else " (nobody is pinged)")
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

        # ---- post (same layout as +schedule)
        stadiums, roles = _stadiums(ctx.guild.id), _roles(ctx.guild.id)
        used_roles = set()

        def role_text(team):
            role = ctx.guild.get_role(roles.get(_norm(team), 0))
            if role is None:
                return f"**{discord.utils.escape_markdown(team)}**"
            used_roles.add(role)
            return role.mention

        def channel_text(host):
            channel_id = stadiums.get(_norm(host))
            return f"<#{channel_id}>" if channel_id and ctx.guild.get_channel(channel_id) else "*(no home stadium set)*"

        posted = 0
        for key in stage_order:
            matches, day_of, day_count, _warnings = plans[key]
            if not matches:
                continue
            groups, group_teams = stage_groups_and_teams(ctx.guild.id, names[key])
            known = {g[0] for g in groups}
            for m in matches:                       # a group missing from group_styles still gets shown
                if m["group_key"] not in known:
                    groups.append((m["group_key"], m["group"], 10 ** 6))
                    known.add(m["group_key"])
            for day in range(1, day_count + 1):
                todays = [m for m in matches if day_of[m["id"]] == day]
                for group_name, group_matches, rest in build_day_groups(todays, groups, group_teams):
                    used_roles.clear()
                    texts = build_group_post(names[key], day, group_name, group_matches, rest,
                                             role_text, channel_text)
                    mentions = (discord.AllowedMentions(everyone=False, users=False, roles=list(used_roles),
                                                        replied_user=False)
                                if ping else discord.AllowedMentions.none())
                    for text in texts:
                        await ctx.send(text, allowed_mentions=mentions)
                    posted += len(group_matches)
        await ctx.send(f"✅ Posted **{posted}** reserved match(es) with their original match numbers.")


async def setup(bot):
    await bot.add_cog(ReserveNumbering(bot))
