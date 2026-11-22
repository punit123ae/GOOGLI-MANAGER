"""Player head-to-head for Googli Manager (image version).

+h2h @player            you vs that player
+h2h @player1 @player2  two players against each other

Page 1 is an image comparing batting, bowling, fielding and fantasy points side by side.
Page 2 is an image listing the matches where they were on opposing teams. The background
is split in the two players' team colours (their Discord team role colour).

Page 2 relies on the team names saved when a match is imported (match_teams), so only
matches imported after that was added count. If drawing the images ever fails, the
original embed version is sent instead, so +h2h keeps working.

This file also holds ImagePager and the small helpers that +compareteams reuses.
"""

import asyncio
import io
import sqlite3

import discord
from discord.ext import commands

from .lb import _totals
from .versus_image import BAT_COLOR, BOWL_COLOR, FIELD_COLOR, render_h2h_pages, stat_row

BLUE = discord.Color.from_rgb(55, 150, 255)
NAME_WIDTH = 9
MAX_MATCHES_SHOWN = 8
BETTER = "▲"


# --------------------------------------------------------------------------- shared helpers

class ImagePager(discord.ui.View):
    """Flip between image pages (PNG bytes). Only the person who ran the command can use it."""

    def __init__(self, ctx, pages, names, filename):
        super().__init__(timeout=180)
        self.owner_id = ctx.author.id
        self.pages = list(pages)
        self.filename = filename
        self.index = 0
        self.message = None

        self.prev_button = discord.ui.Button(label="Previous", emoji="◀️", style=discord.ButtonStyle.secondary, row=0)
        self.counter = discord.ui.Button(label="1/1", style=discord.ButtonStyle.primary, disabled=True, row=0)
        self.next_button = discord.ui.Button(label="Next", emoji="▶️", style=discord.ButtonStyle.secondary, row=0)
        self.prev_button.callback = self._previous
        self.counter.callback = self._noop
        self.next_button.callback = self._next
        for item in (self.prev_button, self.counter, self.next_button):
            self.add_item(item)

        self.name_buttons = []
        for number, name in enumerate(names[:len(self.pages)]):
            button = discord.ui.Button(label=name, style=discord.ButtonStyle.secondary, row=1)
            button.callback = self._goto(number)
            self.name_buttons.append(button)
            self.add_item(button)
        self._sync()

    def _sync(self):
        self.prev_button.disabled = self.index <= 0
        self.next_button.disabled = self.index >= len(self.pages) - 1
        self.counter.label = f"{self.index + 1}/{len(self.pages)}"
        for number, button in enumerate(self.name_buttons):
            button.style = discord.ButtonStyle.success if number == self.index else discord.ButtonStyle.secondary

    async def interaction_check(self, interaction):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Only the person who ran this command can turn the pages.", ephemeral=True
            )
            return False
        return True

    async def _show(self, interaction, index):
        self.index = max(0, min(index, len(self.pages) - 1))
        self._sync()
        file = discord.File(io.BytesIO(self.pages[self.index]), filename=self.filename)
        await interaction.response.edit_message(attachments=[file], view=self)

    async def _previous(self, interaction):
        await self._show(interaction, self.index - 1)

    async def _next(self, interaction):
        await self._show(interaction, self.index + 1)

    async def _noop(self, interaction):
        await interaction.response.defer()

    def _goto(self, number):
        async def callback(interaction):
            await self._show(interaction, number)
        return callback

    async def on_timeout(self):
        self.clear_items()
        self.pages = []
        if self.message is not None:
            try:
                await self.message.edit(view=None)
            except discord.HTTPException:
                pass


def team_role_color(core, guild, team):
    """The Discord role colour configured for a team (same rule as +teaminfo), or None."""
    try:
        setting = core.db.execute(
            "SELECT role_id FROM team_role_settings WHERE team_id=? AND enabled=1",
            (int(team["id"]),),
        ).fetchone()
        if setting:
            role = guild.get_role(int(setting["role_id"]))
            if role is not None and role.color.value:
                return role.color.to_rgb()
    except Exception:
        pass
    return None


async def fetch_avatar(bot, member):
    """Avatar PNG bytes (cached and retried by the team cog when it is loaded), or None."""
    if member is None:
        return None
    core = bot.get_cog("TeamRegistration")
    if core is not None and hasattr(core, "_fetch_avatar"):
        try:
            return await core._fetch_avatar(member)
        except Exception:
            pass
    try:
        return await asyncio.wait_for(
            member.display_avatar.replace(size=256, static_format="png").read(), timeout=10
        )
    except Exception:
        return None



# --------------------------------------------------------------------------- overall side colour
# +h2h all / +ctall compare one player / team with "everyone else". That second side has no
# team colour, so the bot owner picks one of these (saved per server, changed with +overallcolor).
OVERALL_COLORS = {
    "red": (225, 40, 60),
    "yellow": (255, 196, 40),
    "blue": (50, 120, 255),
}
DEFAULT_OVERALL_COLOR = "red"


def _ensure_color_table(bot):
    bot.db.execute(
        "CREATE TABLE IF NOT EXISTS overall_side_color ("
        "guild_id INTEGER PRIMARY KEY, color TEXT NOT NULL)"
    )


def get_overall_color_name(bot, guild_id):
    """Saved colour name for this server, or None if the owner has not chosen one yet."""
    try:
        _ensure_color_table(bot)
        row = bot.db.execute(
            "SELECT color FROM overall_side_color WHERE guild_id=?", (int(guild_id),)
        ).fetchone()
    except sqlite3.Error:
        return None
    return str(row[0]) if row and str(row[0]) in OVERALL_COLORS else None


def set_overall_color_name(bot, guild_id, name):
    _ensure_color_table(bot)
    bot.db.execute(
        "INSERT INTO overall_side_color (guild_id, color) VALUES (?, ?) "
        "ON CONFLICT(guild_id) DO UPDATE SET color=excluded.color",
        (int(guild_id), name),
    )
    bot.db.commit()


class OverallColorView(discord.ui.View):
    """Red / Yellow / Blue buttons for the bot owner. The pick is saved when pressed."""

    def __init__(self, bot, ctx):
        super().__init__(timeout=60)
        self.bot, self.ctx = bot, ctx
        self.choice = None
        self.message = None
        for name in OVERALL_COLORS:
            button = discord.ui.Button(
                label=name.title(),
                emoji={"red": "🔴", "yellow": "🟡", "blue": "🔵"}[name],
                style=discord.ButtonStyle.secondary,
            )
            button.callback = self._pick(name)
            self.add_item(button)

    async def interaction_check(self, interaction):
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message("Only the bot owner can pick this.", ephemeral=True)
            return False
        return True

    def _pick(self, name):
        async def callback(interaction):
            set_overall_color_name(self.bot, self.ctx.guild.id, name)
            self.choice = name
            await interaction.response.edit_message(
                content=f"✅ Overall side colour set to **{name}**. Change it any time with `+overallcolor`.",
                view=None,
            )
            self.stop()
        return callback

    async def on_timeout(self):
        if self.message is not None and self.choice is None:
            try:
                await self.message.edit(content="No colour chosen, using the default for now.", view=None)
            except discord.HTTPException:
                pass


async def overall_color_for(bot, ctx):
    """RGB for the 'everyone else' side. The first time the owner uses an overall command, ask."""
    name = get_overall_color_name(bot, ctx.guild.id)
    if name is None and await bot.is_owner(ctx.author):
        view = OverallColorView(bot, ctx)
        view.message = await ctx.reply(
            "🎨 Which colour should the **overall** side use? (red / yellow / blue)\n"
            "You can change it later with `+overallcolor`.",
            view=view, mention_author=False,
        )
        await view.wait()
        name = view.choice
    return OVERALL_COLORS[name or DEFAULT_OVERALL_COLOR]


def contrasting_overall_color(color, side_color):
    """The chosen colour, unless it is too close to the other side - then the most different of the three."""
    def dist(x, y):
        return sum((p - q) ** 2 for p, q in zip(x, y)) ** 0.5
    if side_color is None or dist(color, side_color) >= 90:
        return color
    return max(OVERALL_COLORS.values(), key=lambda c: dist(c, side_color))


# --------------------------------------------------------------------------- embed fallback helpers

def _short(name, width=NAME_WIDTH):
    name = str(name)
    return name if len(name) <= width else name[: width - 1] + "…"


def _cell(text, mark):
    return f"{text:>8}{BETTER if mark else ' '}"


def _compare(a, b, direction):
    """(a_better, b_better). direction: 'high', 'low' or None for info-only rows."""
    if direction is None or a is None or b is None or a == b:
        return False, False
    if direction == "high":
        return a > b, b > a
    return a < b, b < a


def _fmt(value, kind):
    if value is None:
        return "-"
    if kind == "text":
        return str(value)
    if kind == "int":
        return str(int(value))
    if kind == "pct":
        return f"{value:.1f}%"
    return f"{value:.2f}"


def _table(a_name, b_name, rows):
    """Monospace table. rows: (label, a_value, b_value, kind, direction).

    Returns (text, a_leads, b_leads) where the leads count the rows each player wins.
    """
    header = f"{'':<13}{_short(a_name):>9} {_short(b_name):>9}"
    lines = [header]
    a_leads = b_leads = 0
    for label, a, b, kind, direction in rows:
        a_better, b_better = _compare(a, b, direction)
        a_leads += a_better
        b_leads += b_better
        lines.append(f"{label:<13}{_cell(_fmt(a, kind), a_better)} {_cell(_fmt(b, kind), b_better)}")
    return "```\n" + "\n".join(lines) + "\n```", a_leads, b_leads


def _batting_rows(ra, rb, ta, tb):
    def hi(rows):
        return max((r["runs"] for r in rows), default=0)

    return [
        ("Innings", len(ra), len(rb), "int", None),
        ("Runs", ta["runs"], tb["runs"], "int", "high"),
        ("Average", ta["bat_avg"] if ra else None, tb["bat_avg"] if rb else None, "num", "high"),
        ("Strike rate", ta["strike_rate"] if ta["balls_faced"] else None,
         tb["strike_rate"] if tb["balls_faced"] else None, "num", "high"),
        ("Highest", hi(ra), hi(rb), "int", "high"),
        ("50s", sum(1 for r in ra if 50 <= r["runs"] < 100), sum(1 for r in rb if 50 <= r["runs"] < 100), "int", "high"),
        ("100s", sum(1 for r in ra if r["runs"] >= 100), sum(1 for r in rb if r["runs"] >= 100), "int", "high"),
    ]


def _bowling_rows(ta, tb):
    def overs(t):
        return f"{t['balls_bowled'] // 6}.{t['balls_bowled'] % 6}"

    return [
        ("Wickets", ta["wickets"], tb["wickets"], "int", "high"),
        ("Economy", ta["economy"] if ta["balls_bowled"] else None,
         tb["economy"] if tb["balls_bowled"] else None, "num", "low"),
        ("Average", ta["bowl_avg"] if ta["wickets"] else None,
         tb["bowl_avg"] if tb["wickets"] else None, "num", "low"),
        ("Overs", overs(ta), overs(tb), "text", None),
    ]


def _fielding_rows(fa, fb):
    def eff(rec):
        total = rec[0] + rec[1]
        return rec[0] / total * 100 if total else None

    return [
        ("Caught", fa[0], fb[0], "int", "high"),
        ("Dropped", fa[1], fb[1], "int", "low"),
        ("Efficiency", eff(fa), eff(fb), "pct", "high"),
    ]


# --------------------------------------------------------------------------- image data

def _overs_text(balls):
    return f"{balls // 6}.{balls % 6}"


def _efficiency(rec):
    chances = rec[0] + rec[1]
    return rec[0] / chances * 100 if chances else None


def _best_figures(rows):
    """(sortable number, "w/r" text) of the best spell, or (None, "—") if never bowled."""
    spells = [r for r in rows if r["balls_bowled"] > 0]
    if not spells:
        return None, "—"
    best = max(spells, key=lambda r: (r["wickets"], -r["runs_conceded"]))
    # wickets first, fewer runs conceded breaks a tie
    return best["wickets"] + 1 / (1 + best["runs_conceded"]), f"{best['wickets']}/{best['runs_conceded']}"


def _image_groups(ra, rb, fa, fb):
    """The three comparison cards for page 1: batting, bowling, fielding & fantasy."""
    from .stats import fantasy_raw_points

    ta, tb = _totals(ra), _totals(rb)

    def hi(rows):
        return max((r["runs"] for r in rows), default=0)

    def fifties(rows):
        return sum(1 for r in rows if 50 <= r["runs"] < 100)

    def hundreds(rows):
        return sum(1 for r in rows if r["runs"] >= 100)

    def spells(rows):
        return sum(1 for r in rows if r["balls_bowled"] > 0)

    batting = [
        stat_row("Innings", len(ra), len(rb), "int", None),
        stat_row("Runs", ta["runs"], tb["runs"], "int", "high"),
        stat_row("Average", ta["bat_avg"] if ra else None, tb["bat_avg"] if rb else None, "num2", "high"),
        stat_row("Strike rate", ta["strike_rate"] if ta["balls_faced"] else None,
                 tb["strike_rate"] if tb["balls_faced"] else None, "num1", "high"),
        stat_row("Highest", hi(ra) if ra else None, hi(rb) if rb else None, "int", "high"),
        stat_row("50s", fifties(ra), fifties(rb), "int", "high"),
        stat_row("100s", hundreds(ra), hundreds(rb), "int", "high"),
    ]
    best_a, best_a_text = _best_figures(ra)
    best_b, best_b_text = _best_figures(rb)
    bowling = [
        stat_row("Wickets", ta["wickets"], tb["wickets"], "int", "high"),
        stat_row("Economy", ta["economy"] if ta["balls_bowled"] else None,
                 tb["economy"] if tb["balls_bowled"] else None, "num2", "low"),
        stat_row("Average", ta["bowl_avg"] if ta["wickets"] else None,
                 tb["bowl_avg"] if tb["wickets"] else None, "num2", "low"),
        stat_row("Strike rate", ta["bowl_sr"] if ta["wickets"] else None,
                 tb["bowl_sr"] if tb["wickets"] else None, "num1", "low"),
        stat_row("Overs", ta["balls_bowled"], tb["balls_bowled"], "text", None,
                 _overs_text(ta["balls_bowled"]), _overs_text(tb["balls_bowled"])),
        stat_row("Best figures", best_a, best_b, "text", "high", best_a_text, best_b_text),
        stat_row("Spells", spells(ra), spells(rb), "int", None),
    ]
    bat_a, bowl_a, _ = fantasy_raw_points(ra)
    bat_b, bowl_b, _ = fantasy_raw_points(rb)
    field_a, field_b = fa[0] * 5 - fa[1] * 10, fb[0] * 5 - fb[1] * 10
    fielding = [
        stat_row("Caught", fa[0], fb[0], "int", "high"),
        stat_row("Dropped", fa[1], fb[1], "int", "low"),
        stat_row("Efficiency", _efficiency(fa), _efficiency(fb), "pct", "high"),
        stat_row("Batting pts", bat_a, bat_b, "num1", "high"),
        stat_row("Bowling pts", bowl_a, bowl_b, "num1", "high"),
        stat_row("Fielding pts", field_a, field_b, "int", "high"),
        stat_row("Total fantasy", bat_a + bowl_a + field_a, bat_b + bowl_b + field_b, "num1", "high"),
    ]
    totals = (bat_a + bowl_a + field_a, bat_b + bowl_b + field_b)
    return [
        {"title": "BATTING", "color": BAT_COLOR, "rows": batting},
        {"title": "BOWLING", "color": BOWL_COLOR, "rows": bowling},
        {"title": "FIELDING & FANTASY", "color": FIELD_COLOR, "rows": fielding},
    ], totals


def _overall_groups(ra, fa, others, field_others):
    """Cards for one player vs the average of all OTHER players.

    others: {user_id: rows} for every other player with matches; field_others: {user_id: (caught, dropped)}.
    Counting stats are per-player averages; rates (average, strike rate, economy...) are pooled
    over everybody's matches.
    """
    from .stats import fantasy_raw_points

    n = len(others)
    pooled = [r for rows in others.values() for r in rows]
    ta, tb = _totals(ra), _totals(pooled)

    def mean(values):
        values = list(values)
        return sum(values) / len(values) if values else None

    def cnt(label, a, b, better="high", digits=1):
        text = None if b is None else (f"{b:.{digits}f}")
        return stat_row(label, a, b, "int", better, None, text)

    def best_w(rows):
        w = [r["wickets"] for r in rows if r["balls_bowled"] > 0]
        return max(w) if w else None

    avg_fifties = mean(sum(1 for r in rows if 50 <= r["runs"] < 100) for rows in others.values())
    avg_hundreds = mean(sum(1 for r in rows if r["runs"] >= 100) for rows in others.values())
    mine_best_num, mine_best_text = _best_figures(ra)
    bowler_best = [best_w(rows) for rows in others.values() if best_w(rows) is not None]
    avg_best = mean(bowler_best)
    balls_each = tb["balls_bowled"] / n

    batting = [
        cnt("Innings", len(ra), mean(len(rows) for rows in others.values()), None),
        cnt("Runs", ta["runs"], tb["runs"] / n),
        stat_row("Average", ta["bat_avg"] if ra else None, tb["bat_avg"], "num2", "high"),
        stat_row("Strike rate", ta["strike_rate"] if ta["balls_faced"] else None,
                 tb["strike_rate"] if tb["balls_faced"] else None, "num1", "high"),
        cnt("Highest", max((r["runs"] for r in ra), default=None),
            mean(max(r["runs"] for r in rows) for rows in others.values())),
        cnt("50s", sum(1 for r in ra if 50 <= r["runs"] < 100), avg_fifties),
        cnt("100s", sum(1 for r in ra if r["runs"] >= 100), avg_hundreds),
    ]
    bowling = [
        cnt("Wickets", ta["wickets"], tb["wickets"] / n),
        stat_row("Economy", ta["economy"] if ta["balls_bowled"] else None,
                 tb["economy"] if tb["balls_bowled"] else None, "num2", "low"),
        stat_row("Average", ta["bowl_avg"] if ta["wickets"] else None,
                 tb["bowl_avg"] if tb["wickets"] else None, "num2", "low"),
        stat_row("Strike rate", ta["bowl_sr"] if ta["wickets"] else None,
                 tb["bowl_sr"] if tb["wickets"] else None, "num1", "low"),
        stat_row("Overs", ta["balls_bowled"], balls_each, "text", None,
                 _overs_text(ta["balls_bowled"]), _overs_text(int(round(balls_each)))),
        stat_row("Best figures", mine_best_num, avg_best, "text", "high", mine_best_text,
                 None if avg_best is None else f"{avg_best:.1f} wkts"),
        cnt("Spells", sum(1 for r in ra if r["balls_bowled"] > 0),
            mean(sum(1 for r in rows if r["balls_bowled"] > 0) for rows in others.values()), None),
    ]
    bat_a, bowl_a, _ = fantasy_raw_points(ra)
    parts = [fantasy_raw_points(rows) for rows in others.values()]
    bat_b, bowl_b = mean(p[0] for p in parts), mean(p[1] for p in parts)
    field_a = fa[0] * 5 - fa[1] * 10
    field_b = mean(c * 5 - d * 10 for c, d in field_others.values()) if field_others else 0.0
    caught_b = mean(c for c, _ in field_others.values()) if field_others else None
    dropped_b = mean(d for _, d in field_others.values()) if field_others else None
    pooled_caught = sum(c for c, _ in field_others.values())
    pooled_chances = pooled_caught + sum(d for _, d in field_others.values())
    eff_b = pooled_caught / pooled_chances * 100 if pooled_chances else None
    total_a = bat_a + bowl_a + field_a
    total_b = bat_b + bowl_b + field_b
    fielding = [
        cnt("Caught", fa[0], caught_b),
        cnt("Dropped", fa[1], dropped_b, "low"),
        stat_row("Efficiency", _efficiency(fa), eff_b, "pct", "high"),
        stat_row("Batting pts", bat_a, bat_b, "num1", "high"),
        stat_row("Bowling pts", bowl_a, bowl_b, "num1", "high"),
        stat_row("Fielding pts", field_a, field_b, "num1", "high"),
        stat_row("Total fantasy", total_a, total_b, "num1", "high"),
    ]
    return [
        {"title": "BATTING", "color": BAT_COLOR, "rows": batting},
        {"title": "BOWLING", "color": BOWL_COLOR, "rows": bowling},
        {"title": "FIELDING & FANTASY", "color": FIELD_COLOR, "rows": fielding},
    ], (total_a, total_b)


class PlayerH2H(commands.Cog):
    """+h2h: compare two players."""

    def __init__(self, bot):
        self.bot = bot

    # ------------------------------------------------------------------ data
    def _match_rows(self, guild_id, user_id):
        try:
            rows = self.bot.db.execute(
                """SELECT p.runs, p.balls_faced, p.runs_conceded, p.balls_bowled,
                          p.wickets, p.not_out
                   FROM player_match_stats p
                   JOIN stats_imports i ON i.source_message_id = p.source_message_id
                   WHERE i.guild_id = ? AND p.user_id = ?
                     AND COALESCE(i.competition, 'MAIN TOURNAMENT') = 'MAIN TOURNAMENT'""",
                (int(guild_id), int(user_id)),
            ).fetchall()
        except Exception:
            return []
        return [
            {"runs": int(r[0]), "balls_faced": int(r[1]), "runs_conceded": int(r[2]),
             "balls_bowled": int(r[3]), "wickets": int(r[4]), "not_out": int(r[5])}
            for r in rows
        ]

    def _fielding(self, guild_id, user_id):
        try:
            row = self.bot.db.execute(
                "SELECT catches_caught, catches_dropped FROM fielding_stats WHERE guild_id=? AND user_id=?",
                (int(guild_id), int(user_id)),
            ).fetchone()
        except Exception:
            return 0, 0
        return (int(row[0]), int(row[1])) if row else (0, 0)

    def _opposed_matches(self, guild_id, a_id, b_id):
        """Matches both played in, on different teams, newest first.

        Teams are the ones saved when the match was imported. Matches imported before
        that was added have none saved, so for those the players' CURRENT teams are
        used instead (a player who changed teams since may be missed or mislabelled).
        """
        try:
            matches = self.bot.db.execute(
                """
                SELECT i.imported_at, a.source_message_id,
                       a.runs, a.balls_faced, a.runs_conceded, a.balls_bowled, a.wickets, a.not_out,
                       b.runs, b.balls_faced, b.runs_conceded, b.balls_bowled, b.wickets, b.not_out,
                       (SELECT team_name FROM match_teams
                         WHERE source_message_id = a.source_message_id AND user_id = a.user_id),
                       (SELECT team_name FROM match_teams
                         WHERE source_message_id = b.source_message_id AND user_id = b.user_id)
                FROM player_match_stats a
                JOIN player_match_stats b ON b.source_message_id = a.source_message_id
                JOIN stats_imports i ON i.source_message_id = a.source_message_id
                WHERE i.guild_id = ? AND a.user_id = ? AND b.user_id = ?
                  AND COALESCE(i.competition, 'MAIN TOURNAMENT') = 'MAIN TOURNAMENT'
                ORDER BY i.imported_at DESC, a.source_message_id DESC
                """,
                (int(guild_id), int(a_id), int(b_id)),
            ).fetchall()
        except Exception:
            return []

        current = {}
        if any(m[14] is None or m[15] is None for m in matches):
            try:
                from .lb import _team_rosters
                for team in _team_rosters(int(guild_id)).values():
                    for uid in team["members"]:
                        current[int(uid)] = str(team["name"])
            except Exception:
                current = {}

        result = []
        for m in matches:
            a_team = m[14] if m[14] is not None else current.get(int(a_id))
            b_team = m[15] if m[15] is not None else current.get(int(b_id))
            if a_team is None or b_team is None or a_team == b_team:
                continue
            result.append((m[0], *m[2:8], a_team, *m[8:14], b_team))
        return result

    def _team_of(self, guild, member):
        """(team name, role colour) of a player's current team, or (None, None)."""
        core = self.bot.get_cog("TeamRegistration")
        if core is None:
            return None, None
        try:
            team = core._membership(guild.id, member.id)
        except Exception:
            return None, None
        if not team:
            return None, None
        return str(team["name"]), team_role_color(core, guild, team)

    # ------------------------------------------------------------------ embeds (fallback)
    @staticmethod
    def _name(member):
        return discord.utils.escape_markdown(member.display_name)

    def _stats_page(self, guild, a, b):
        ra, rb = self._match_rows(guild.id, a.id), self._match_rows(guild.id, b.id)
        ta, tb = _totals(ra), _totals(rb)
        fa, fb = self._fielding(guild.id, a.id), self._fielding(guild.id, b.id)

        embed = discord.Embed(
            title=f"⚔️ {self._name(a)} vs {self._name(b)}",
            color=BLUE,
        )
        a_total = b_total = 0
        for title, rows in (
            ("🏏 BATTING", _batting_rows(ra, rb, ta, tb)),
            ("🎯 BOWLING", _bowling_rows(ta, tb)),
            ("🧤 FIELDING", _fielding_rows(fa, fb)),
        ):
            text, a_leads, b_leads = _table(a.display_name, b.display_name, rows)
            a_total += a_leads
            b_total += b_leads
            embed.add_field(name=title, value=text, inline=False)

        if a_total == b_total:
            verdict = f"Level: both lead in `{a_total}` stat(s)."
        else:
            leader, lead, other = (a, a_total, b_total) if a_total > b_total else (b, b_total, a_total)
            verdict = f"**{self._name(leader)}** leads in `{lead}` stats to `{other}`."
        embed.description = f"{verdict}\n{BETTER} marks the better value."
        embed.set_footer(text="Page 1 of 2 • career totals from recorded matches")
        return embed

    @staticmethod
    def _bat_text(runs, faced, not_out):
        return f"{runs}{'*' if not_out else ''} ({faced})" if faced > 0 else "DNB"

    @staticmethod
    def _bowl_text(wickets, conceded, balls):
        return f"{wickets}/{conceded}" if balls > 0 else "DNB"

    def _matches_page(self, guild, a, b):
        rows = self._opposed_matches(guild.id, a.id, b.id)
        a_name, b_name = self._name(a), self._name(b)
        embed = discord.Embed(title=f"🆚 {a_name} vs {b_name} • Matches against each other", color=BLUE)
        if not rows:
            embed.description = (
                "No recorded matches where they were on opposing teams.\n"
                "Only matches imported after team tracking was added can be matched up."
            )
            embed.set_footer(text="Page 2 of 2")
            return embed

        a_more = b_more = 0
        lines = []
        for i, r in enumerate(rows):
            (ts, a_runs, a_faced, a_conc, a_balls, a_wk, a_no, a_team,
             b_runs, b_faced, b_conc, b_balls, b_wk, b_no, b_team) = r
            if int(a_runs) > int(b_runs):
                a_more += 1
            elif int(b_runs) > int(a_runs):
                b_more += 1
            if i >= MAX_MATCHES_SHOWN:
                continue
            lines.append(
                f"`{i + 1}.` <t:{int(ts)}:d> • **{discord.utils.escape_markdown(str(a_team))}** vs "
                f"**{discord.utils.escape_markdown(str(b_team))}**\n"
                f"　{a_name}: 🏏 `{self._bat_text(int(a_runs), int(a_faced), int(a_no))}` "
                f"🎯 `{self._bowl_text(int(a_wk), int(a_conc), int(a_balls))}`\n"
                f"　{b_name}: 🏏 `{self._bat_text(int(b_runs), int(b_faced), int(b_no))}` "
                f"🎯 `{self._bowl_text(int(b_wk), int(b_conc), int(b_balls))}`"
            )
        summary = (
            f"**{len(rows)}** match(es) on opposing teams • "
            f"{a_name} scored more in `{a_more}`, {b_name} in `{b_more}`"
        )
        extra = len(rows) - MAX_MATCHES_SHOWN
        tail = f"\n\n…and {extra} older match(es)." if extra > 0 else ""
        embed.description = summary + "\n\n" + "\n".join(lines) + tail
        embed.set_footer(text="Page 2 of 2 • newest first")
        return embed

    # ------------------------------------------------------------------ images
    async def _image_pages(self, guild, a, b):
        """Gather everything and draw both pages. Returns a list of PNG bytes."""
        ra, rb = self._match_rows(guild.id, a.id), self._match_rows(guild.id, b.id)
        fa, fb = self._fielding(guild.id, a.id), self._fielding(guild.id, b.id)
        groups, fantasy = _image_groups(ra, rb, fa, fb)
        (a_team, a_color), (b_team, b_color) = self._team_of(guild, a), self._team_of(guild, b)
        a_avatar, b_avatar = await asyncio.gather(fetch_avatar(self.bot, a), fetch_avatar(self.bot, b))

        opposed = self._opposed_matches(guild.id, a.id, b.id)
        a_more = b_more = 0
        matches = []
        for r in opposed:
            (ts, a_runs, a_faced, a_conc, a_balls, a_wk, a_no, a_name_team,
             b_runs, b_faced, b_conc, b_balls, b_wk, b_no, b_name_team) = r
            a_more += int(a_runs) > int(b_runs)
            b_more += int(b_runs) > int(a_runs)
            matches.append({
                "ts": ts, "a_runs": int(a_runs), "b_runs": int(b_runs),
                "a_team": a_name_team, "b_team": b_name_team,
                "a_bat": self._bat_text(int(a_runs), int(a_faced), int(a_no)),
                "a_bowl": self._bowl_text(int(a_wk), int(a_conc), int(a_balls)),
                "b_bat": self._bat_text(int(b_runs), int(b_faced), int(b_no)),
                "b_bowl": self._bowl_text(int(b_wk), int(b_conc), int(b_balls)),
            })

        data = {
            "a": {"name": a.display_name, "avatar": a_avatar, "team": a_team, "color": a_color,
                  "mini": [("MATCHES", str(len(ra))), ("FANTASY", f"{fantasy[0]:.0f}")]},
            "b": {"name": b.display_name, "avatar": b_avatar, "team": b_team, "color": b_color,
                  "mini": [("MATCHES", str(len(rb))), ("FANTASY", f"{fantasy[1]:.0f}")]},
            "groups": groups, "matches": matches, "match_total": len(matches),
            "a_more": a_more, "b_more": b_more, "guild": guild.name,
        }
        return await asyncio.to_thread(render_h2h_pages, data)

    async def _send_embeds(self, ctx, first, second):
        pages = [
            self._stats_page(ctx.guild, first, second),
            self._matches_page(ctx.guild, first, second),
        ]
        try:
            from .fielding import PagedEmbedView
            view = PagedEmbedView(ctx, pages)
            view.message = await ctx.reply(
                embed=pages[0], view=view, mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except ImportError:
            await ctx.reply(
                embed=pages[0], mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )

    # ------------------------------------------------------------------ overall (vs everyone else)
    def _all_player_rows(self, guild_id, exclude_id):
        """{user_id: match rows} for every other player with recorded main-tournament matches."""
        try:
            rows = self.bot.db.execute(
                """SELECT p.user_id, p.runs, p.balls_faced, p.runs_conceded, p.balls_bowled,
                          p.wickets, p.not_out
                   FROM player_match_stats p
                   JOIN stats_imports i ON i.source_message_id = p.source_message_id
                   WHERE i.guild_id = ? AND p.user_id != ?
                     AND COALESCE(i.competition, 'MAIN TOURNAMENT') = 'MAIN TOURNAMENT'""",
                (int(guild_id), int(exclude_id)),
            ).fetchall()
        except Exception:
            return {}
        result = {}
        for r in rows:
            result.setdefault(int(r[0]), []).append({
                "runs": int(r[1]), "balls_faced": int(r[2]), "runs_conceded": int(r[3]),
                "balls_bowled": int(r[4]), "wickets": int(r[5]), "not_out": int(r[6]),
            })
        return result

    def _all_fielding(self, guild_id, exclude_id):
        try:
            rows = self.bot.db.execute(
                "SELECT user_id, catches_caught, catches_dropped FROM fielding_stats "
                "WHERE guild_id=? AND user_id != ? AND (catches_caught > 0 OR catches_dropped > 0)",
                (int(guild_id), int(exclude_id)),
            ).fetchall()
        except Exception:
            return {}
        return {int(r[0]): (int(r[1]), int(r[2])) for r in rows}

    async def _overall_image(self, ctx, member):
        guild = ctx.guild
        ra, fa = self._match_rows(guild.id, member.id), self._fielding(guild.id, member.id)
        others = self._all_player_rows(guild.id, member.id)
        if not others:
            return None
        groups, totals = _overall_groups(ra, fa, others, self._all_fielding(guild.id, member.id))
        team, color = self._team_of(guild, member)
        avatar = await fetch_avatar(self.bot, member)
        overall = contrasting_overall_color(await overall_color_for(self.bot, ctx), color)
        data = {
            "a": {"name": member.display_name, "avatar": avatar, "team": team, "color": color,
                  "mini": [("MATCHES", str(len(ra))), ("FANTASY", f"{totals[0]:.0f}")]},
            "b": {"name": "All players", "avatar": None, "team": "LEAGUE AVERAGE", "color": overall,
                  "mini": [("PLAYERS", str(len(others))), ("FANTASY", f"{totals[1]:.0f}")]},
            "groups": groups, "matches": [], "match_total": 0, "a_more": 0, "b_more": 0,
            "guild": guild.name, "caption": "Vs all players",
        }
        return (await asyncio.to_thread(render_h2h_pages, data, True))[0]

    # ------------------------------------------------------------------ command
    @commands.command(name="h2h", aliases=["headtohead"])
    @commands.guild_only()
    async def h2h(self, ctx, first: str, second: discord.Member = None):
        """+h2h @player | +h2h @p1 @p2 | +h2h all (you vs everyone) | +h2h all @player"""
        if first.strip().lower() in ("all", "overall"):
            subject = second or ctx.author
            loading = await ctx.reply("⏳ Building the overall comparison...", mention_author=False)
            try:
                png = await self._overall_image(ctx, subject)
            except Exception as exc:
                print(f"[h2h all] failed: {type(exc).__name__}: {exc}", flush=True)
                png = None
                await loading.edit(content="I couldn't build the overall comparison. Check the bot logs.")
                return
            if png is None:
                await loading.edit(content="There are no other players with recorded matches yet.")
                return
            await loading.edit(content=None, attachments=[discord.File(io.BytesIO(png), filename="h2h_all.png")])
            return

        try:
            first = await commands.MemberConverter().convert(ctx, first)
        except commands.BadArgument:
            await ctx.reply("Use `+h2h @player`, `+h2h @player1 @player2` or `+h2h all`.", mention_author=False)
            return
        if second is None:
            first, second = ctx.author, first
        if first.id == second.id:
            await ctx.reply("Pick two different players to compare.", mention_author=False)
            return

        loading = await ctx.reply("⏳ Building the head-to-head...", mention_author=False)
        try:
            pngs = await self._image_pages(ctx.guild, first, second)
            view = ImagePager(ctx, pngs, ("Stats", "Matches"), "h2h.png")
            view.message = await loading.edit(
                content=None, attachments=[discord.File(io.BytesIO(pngs[0]), filename="h2h.png")], view=view,
            )
        except Exception as exc:
            print(f"[h2h] image generation failed: {type(exc).__name__}: {exc}", flush=True)
            try:
                await loading.delete()
            except Exception:
                pass
            await self._send_embeds(ctx, first, second)

    @commands.command(name="overallcolor", aliases=["overallcolour", "setoverallcolor"])
    @commands.guild_only()
    async def overall_color(self, ctx, color: str = None):
        """Bot owner: choose the colour of the 'overall' side (red / yellow / blue)."""
        if not await self.bot.is_owner(ctx.author):
            await ctx.reply("Only the bot owner can change this.", mention_author=False)
            return
        if color is None:
            current = get_overall_color_name(self.bot, ctx.guild.id) or f"{DEFAULT_OVERALL_COLOR} (default)"
            view = OverallColorView(self.bot, ctx)
            view.message = await ctx.reply(
                f"🎨 Overall side colour is **{current}**. Pick a new one:", view=view, mention_author=False
            )
            return
        name = color.strip().lower()
        if name not in OVERALL_COLORS:
            await ctx.reply("Choose `red`, `yellow` or `blue`.", mention_author=False)
            return
        set_overall_color_name(self.bot, ctx.guild.id, name)
        await ctx.reply(f"✅ Overall side colour set to **{name}**.", mention_author=False)


async def setup(bot):
    await bot.add_cog(PlayerH2H(bot))
