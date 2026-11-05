"""Player head-to-head for Googli Manager.

+h2h @player            you vs that player
+h2h @player1 @player2  two players against each other

Page 1 compares their batting, bowling and fielding side by side. Page 2 lists the
matches where they were on opposing teams. Page 2 relies on the team names saved when
a match is imported (match_teams), so only matches imported after that was added count.
"""

import discord
from discord.ext import commands

from .lb import _totals

BLUE = discord.Color.from_rgb(55, 150, 255)
NAME_WIDTH = 9
MAX_MATCHES_SHOWN = 8
BETTER = "▲"


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
                   WHERE i.guild_id = ? AND p.user_id = ?""",
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
        """Matches both played in, on different teams (teams as saved at import), newest first."""
        try:
            return self.bot.db.execute(
                """
                SELECT i.imported_at,
                       a.runs, a.balls_faced, a.runs_conceded, a.balls_bowled, a.wickets, a.not_out, ta.team_name,
                       b.runs, b.balls_faced, b.runs_conceded, b.balls_bowled, b.wickets, b.not_out, tb.team_name
                FROM player_match_stats a
                JOIN player_match_stats b ON b.source_message_id = a.source_message_id
                JOIN stats_imports i ON i.source_message_id = a.source_message_id
                JOIN match_teams ta ON ta.source_message_id = a.source_message_id AND ta.user_id = a.user_id
                JOIN match_teams tb ON tb.source_message_id = b.source_message_id AND tb.user_id = b.user_id
                WHERE i.guild_id = ? AND a.user_id = ? AND b.user_id = ?
                  AND ta.team_name != tb.team_name
                ORDER BY i.imported_at DESC, a.source_message_id DESC
                """,
                (int(guild_id), int(a_id), int(b_id)),
            ).fetchall()
        except Exception:
            return []

    # ------------------------------------------------------------------ embeds
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

    # ------------------------------------------------------------------ command
    @commands.command(name="h2h", aliases=["headtohead"])
    @commands.guild_only()
    async def h2h(self, ctx, first: discord.Member, second: discord.Member = None):
        """Compare two players: +h2h @player  or  +h2h @player1 @player2."""
        if second is None:
            first, second = ctx.author, first
        if first.id == second.id:
            await ctx.reply("Pick two different players to compare.", mention_author=False)
            return

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


async def setup(bot):
    await bot.add_cog(PlayerH2H(bot))
