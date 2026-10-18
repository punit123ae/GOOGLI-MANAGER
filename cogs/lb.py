"""Kashmir Titans paginated embed leaderboard for GOOGLI MANAGER.

Uses the existing stats_imports/player_match_stats tables. No image generation.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

import discord
from discord.ext import commands

BLUE = discord.Colour.from_rgb(20, 117, 255)
PAGE_SIZE = 10

METRICS = {
    "Batting": [
        ("runs", "Runs"),
        ("bat_avg", "Batting Average"),
        ("fifties", "Most 50s"),
        ("hundreds", "Most 100s"),
        ("ducks", "Ducks"),
        ("highest", "Highest Runs"),
        ("lowest_strike_rate", "Lowest Strike Rate (minimum 50 runs)"),
        ("strike_rate", "Highest Strike Rate (minimum 50 runs)"),
    ],
    "Bowling": [
        ("wickets", "Wickets"),
        ("bowl_avg", "Average"),
        ("economy", "Economy"),
        ("best_figures", "Bowling Figures"),
        ("overs", "Most Overs Bowled"),
        ("runs_leaked", "Most Runs Leaked"),
    ],
    "Fantasy Points": [("fantasy", "Total Fantasy Points")],
}


def _fmt(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.2f}".rstrip("0").rstrip(".") or "0"
    return str(value)


def _totals(rows: list[dict]) -> dict[str, float | int]:
    runs = sum(r["runs"] for r in rows)
    faced = sum(r["balls_faced"] for r in rows)
    conceded = sum(r["runs_conceded"] for r in rows)
    bowled = sum(r["balls_bowled"] for r in rows)
    wickets = sum(r["wickets"] for r in rows)
    dismissals = sum(1 for r in rows if not r["not_out"])
    return {
        "runs": runs,
        "balls_faced": faced,
        "runs_conceded": conceded,
        "balls_bowled": bowled,
        "wickets": wickets,
        "bat_avg": runs / dismissals if dismissals else runs,
        "strike_rate": runs * 100 / faced if faced else 0,
        "bowl_avg": conceded / wickets if wickets else 0,
        "economy": conceded * 6 / bowled if bowled else 0,
        "bowl_sr": bowled / wickets if wickets else 0,
    }


def _fantasy_points(rows: list[dict]) -> float:
    """Use the same fantasy formula as the stats cog."""
    t = _totals(rows)
    fifties = sum(50 <= r["runs"] < 100 for r in rows)
    hundreds = sum(r["runs"] >= 100 for r in rows)
    ducks = sum(r["runs"] == 0 and not r["not_out"] for r in rows)
    batting_raw = (50 + t["bat_avg"] * 2 + t["runs"] * 0.1
                   - t["balls_faced"] * 0.1 - ducks * 10
                   + fifties * 5 + hundreds * 10)
    if t["balls_bowled"] <= 0:
        bowling_raw = 0
    else:
        three = sum(3 <= r["wickets"] < 5 for r in rows)
        five = sum(5 <= r["wickets"] < 7 for r in rows)
        seven = sum(r["wickets"] >= 7 for r in rows)
        haul_bonus = three * 7 + five * 15 + seven * 25
        bowling_raw = (70 - t["bowl_avg"] / 2 + t["wickets"] * 2
                       - max(0, t["economy"] - 5) * 2 + haul_bonus)
    return batting_raw + bowling_raw


def _value(key: str, rows: list[dict]) -> float | int | tuple[int, int]:
    t = _totals(rows)
    if key in ("runs", "bat_avg", "strike_rate", "bowl_avg", "economy", "bowl_sr", "wickets"):
        return t[key]
    if key == "highest":
        return max((r["runs"] for r in rows), default=0)
    if key == "lowest_strike_rate":
        faced = sum(r["balls_faced"] for r in rows)
        runs = t["runs"]
        return runs * 100 / faced if faced else 0
    if key == "fifties":
        return sum(1 for r in rows if 50 <= r["runs"] < 100)
    if key == "hundreds":
        return sum(1 for r in rows if r["runs"] >= 100)
    if key == "ducks":
        return sum(1 for r in rows if r["runs"] == 0 and not r["not_out"])
    if key == "overs":
        return t["balls_bowled"] / 6
    if key == "runs_leaked":
        return t["runs_conceded"]
    if key == "best_figures":
        best = max(((r["wickets"], -r["runs_conceded"]) for r in rows), default=(0, 0))
        return best[0], -best[1]
    if key == "fantasy":
        return _fantasy_points(rows)
    return 0


def _eligible(key: str, rows: list[dict]) -> bool:
    t = _totals(rows)
    if key in ("runs", "bat_avg", "highest", "strike_rate", "lowest_strike_rate", "fifties", "hundreds", "ducks"):
        if key in ("strike_rate", "lowest_strike_rate"):
            return t["runs"] >= 50 and t["balls_faced"] > 0
        if key == "ducks":
            return any(r["runs"] == 0 and not r["not_out"] for r in rows)
        return t["runs"] > 0
    if key in ("wickets", "bowl_avg", "economy", "best_figures", "bowl_sr", "overs", "runs_leaked"):
        return t["balls_bowled"] > 0 or t["wickets"] > 0
    return bool(rows)


def _sort_key(key: str, value: Any):
    if key == "lowest_strike_rate":
        return (value,)
    if key in ("bowl_avg", "economy"):
        return (value == 0, value)
    if key == "best_figures":
        return (-value[0], value[1])
    return (-value,)


def _display(key: str, value: Any) -> str:
    if key == "best_figures":
        return f"{value[0]}/{value[1]}"
    return _fmt(value)


class CategorySelect(discord.ui.Select):
    def __init__(self, view: "LeaderboardView"):
        self.parent_view = view
        category_emojis = {
            "Batting": "<:cricketbat:1561294474951393360>",
            "Bowling": "<:cricketball:1561294577392951338>",
            "Fantasy Points": "<:Crown:1561296003104243803>",
        }
        options = [discord.SelectOption(label=name, emoji=category_emojis[name], default=name == view.category)
                   for name in METRICS]
        super().__init__(placeholder="Choose category", min_values=1, max_values=1,
                         options=options, row=0)

    async def callback(self, interaction: discord.Interaction):
        self.parent_view.category = self.values[0]
        self.parent_view.metric_key, self.parent_view.metric_label = METRICS[self.values[0]][0]
        await self.parent_view.refresh(interaction)


class MetricSelect(discord.ui.Select):
    def __init__(self, view: "LeaderboardView"):
        self.parent_view = view
        options = [discord.SelectOption(label=label, value=key, default=key == view.metric_key)
                   for key, label in METRICS[view.category]]
        super().__init__(placeholder="Choose statistic", min_values=1, max_values=1,
                         options=options, row=1)

    async def callback(self, interaction: discord.Interaction):
        self.parent_view.metric_key = self.values[0]
        self.parent_view.metric_label = next(
            label for key, label in METRICS[self.parent_view.category]
            if key == self.parent_view.metric_key
        )
        self.parent_view.page = 0
        await self.parent_view.refresh(interaction)


class PageButton(discord.ui.Button):
    def __init__(self, direction: int):
        self.direction = direction
        super().__init__(label="◀ Previous" if direction < 0 else "Next ▶",
                         style=discord.ButtonStyle.secondary, row=2)

    async def callback(self, interaction: discord.Interaction):
        view: LeaderboardView = self.view  # type: ignore[assignment]
        view.page += self.direction
        await view.refresh(interaction)


class LeaderboardView(discord.ui.View):
    def __init__(self, cog: "LeaderboardCog", ctx: commands.Context):
        super().__init__(timeout=300)
        self.cog = cog
        self.ctx = ctx
        self.owner_id = ctx.author.id
        self.category = "Batting"
        self.metric_key, self.metric_label = METRICS[self.category][0]
        self.page = 0
        self._rebuild_items()

    def _rebuild_items(self):
        self.clear_items()
        self.add_item(CategorySelect(self))
        self.add_item(MetricSelect(self))
        self.add_item(PageButton(-1))
        self.add_item(PageButton(1))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Only the person who ran `+lb` can use these menus.", ephemeral=True)
            return False
        return True

    def make_embed(self) -> discord.Embed:
        guild = self.ctx.guild
        rows_by_user = self.cog.rows_by_user
        computed = []
        for uid, rows in rows_by_user.items():
            if not _eligible(self.metric_key, rows):
                continue
            value = _value(self.metric_key, rows)
            computed.append((uid, value))
        computed.sort(key=lambda item: (_sort_key(self.metric_key, item[1]), item[0]))
        total = len(computed)
        pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        self.page = max(0, min(self.page, pages - 1))
        current = computed[self.page * PAGE_SIZE:(self.page + 1) * PAGE_SIZE]

        embed = discord.Embed(
            title="# **`KASHMIR TITANS S1`**",
            description=f"**{self.category.upper()} LEADERBOARD — {self.metric_label.upper()}**\n"
                        f"**{total} players in this category**\n\n",
            colour=BLUE,
        )
        lines = []
        for index, (uid, value) in enumerate(current, start=self.page * PAGE_SIZE + 1):
            member = guild.get_member(uid) if guild else None
            username = member.name if member else f"Unknown player ({str(uid)[-4:]})"
            lines.append(f"`{index}.` **{discord.utils.escape_markdown(username)}** → `{_display(self.metric_key, value)}`")
        embed.description += "\n".join(lines) if lines else "No players qualify for this statistic yet."
        embed.set_footer(text=f"Kashmir Titans S1 • Page {self.page + 1}/{pages} • {PAGE_SIZE} players per page")
        self.children[-2].disabled = self.page == 0
        self.children[-1].disabled = self.page >= pages - 1
        return embed

    async def refresh(self, interaction: discord.Interaction):
        await interaction.response.defer()
        self._rebuild_items()
        embed = self.make_embed()
        await interaction.message.edit(embed=embed, attachments=[], view=self)


class LeaderboardCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.rows_by_user: dict[int, list[dict]] = {}

    def _rows(self, guild_id: int) -> dict[int, list[dict]]:
        db = getattr(self.bot, "db", None)
        if db is None:
            raise RuntimeError("The bot database connection is not available.")
        result: dict[int, list[dict]] = defaultdict(list)
        query = """SELECT p.user_id, p.runs, p.balls_faced, p.runs_conceded,
                          p.balls_bowled, p.wickets, p.not_out
                   FROM player_match_stats p
                   JOIN stats_imports i ON i.source_message_id = p.source_message_id
                   WHERE i.guild_id = ?
                   ORDER BY i.imported_at ASC, p.source_message_id ASC"""
        for row in db.execute(query, (guild_id,)).fetchall():
            result[int(row["user_id"])].append({
                "runs": int(row["runs"]),
                "balls_faced": int(row["balls_faced"]),
                "runs_conceded": int(row["runs_conceded"]),
                "balls_bowled": int(row["balls_bowled"]),
                "wickets": int(row["wickets"]),
                "not_out": int(row["not_out"]),
            })
        return result

    @commands.command(name="lb", aliases=["leaderboard", "leaderboards"])
    @commands.guild_only()
    async def leaderboard(self, ctx: commands.Context):
        try:
            self.rows_by_user = self._rows(ctx.guild.id)
            view = LeaderboardView(self, ctx)
            message = await ctx.send(embed=view.make_embed(), view=view)
            view.message = message
        except Exception as exc:
            print(f"[lb] Failed to generate leaderboard: {type(exc).__name__}: {exc}")
            await ctx.send("I couldn't generate the leaderboard. Check the bot console for the error.")


async def setup(bot: commands.Bot):
    await bot.add_cog(LeaderboardCog(bot))
