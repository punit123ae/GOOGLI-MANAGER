"""Manual group points tables rendered on the supplied KT league-table template."""

import io
import sqlite3
from pathlib import Path

import discord
from discord.ext import commands
from PIL import Image, ImageDraw, ImageFont

from .groups import GROUP_DB, norm
from .utility import owner_or_whitelisted

POINTS_DB = Path("points_table.db")
TEMPLATE = Path(__file__).with_name("points_table_background.jpeg")
MAX_ROWS = 11
POINTS_PER_WIN = 2

# Coordinates tuned to the supplied 1080x1350 template.
ROW_CENTRES = [510, 586, 647, 709, 771, 833, 895, 958, 1020, 1082, 1144]


def connect_points_db():
    conn = sqlite3.connect(POINTS_DB)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS team_points (
            guild_id INTEGER NOT NULL,
            team_name_key TEXT NOT NULL,
            matches_won INTEGER NOT NULL DEFAULT 0,
            matches_lost INTEGER NOT NULL DEFAULT 0,
            updated_by INTEGER NOT NULL,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (guild_id, team_name_key)
        )
    """)
    conn.commit()
    return conn


def load_font(size, bold=False):
    candidates = []
    if bold:
        candidates += ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "C:/Windows/Fonts/arialbd.ttf"]
    candidates += ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "C:/Windows/Fonts/arial.ttf"]
    for candidate in candidates:
        try:
            return ImageFont.truetype(candidate, size)
        except OSError:
            pass
    return ImageFont.load_default()


def draw_centered(draw, xy, text, font, fill):
    draw.text(xy, str(text), font=font, fill=fill, anchor="mm", stroke_width=0)


def make_table_image(group_name, rows):
    if not TEMPLATE.exists():
        raise FileNotFoundError(f"Points-table background is missing: {TEMPLATE.name}")
    image = Image.open(TEMPLATE).convert("RGB")
    draw = ImageDraw.Draw(image)

    # The supplied template is kept intact. Only the group label is drawn into
    # the original "GROUP -" subtitle area, and team statistics are placed in
    # the existing blank table rows.
    subtitle_candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-BoldOblique.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-BoldItalic.ttf",
        "C:/Windows/Fonts/arialbi.ttf",
    ]
    subtitle_font = None
    for candidate in subtitle_candidates:
        try:
            subtitle_font = ImageFont.truetype(candidate, 47)
            break
        except OSError:
            continue
    if subtitle_font is None:
        subtitle_font = load_font(47, bold=True)

    # Match the template's red subtitle colour and approximate its original baseline.
    label = f"GROUP {str(group_name).upper()}"
    draw.text((312, 286), label, font=subtitle_font, fill=(255, 53, 61), stroke_width=0)

    # Existing template columns: #, TEAM, PL, W, L, PTS.
    # PL is calculated from wins + losses; the original headings and dividers stay untouched.
    team_font_size = 22
    stat_font = load_font(24, bold=True)
    for index, row in enumerate(rows[:MAX_ROWS]):
        y = ROW_CENTRES[index]
        name = str(row["team_name"])
        font_size = team_font_size
        while font_size > 14 and draw.textbbox((0, 0), name, font=load_font(font_size, True))[2] > 285:
            font_size -= 1
        row_font = load_font(font_size, bold=True)
        draw.text((222, y), name, font=row_font, fill=(255, 255, 255), anchor="lm")
        draw_centered(draw, (530, y), int(row["matches_won"]) + int(row["matches_lost"]), stat_font, (255, 255, 255))
        draw_centered(draw, (655, y), row["matches_won"], stat_font, (255, 255, 255))
        draw_centered(draw, (770, y), row["matches_lost"], stat_font, (255, 255, 255))
        draw_centered(draw, (915, y), row["points"], stat_font, (255, 255, 255))

    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    output.seek(0)
    return output


class PointsTable(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        connect_points_db().close()

    async def _allowed(self, ctx):
        if await owner_or_whitelisted(ctx, command_name=ctx.command.name if ctx.command else None, app_owner=True):
            return True
        await ctx.reply("Only the bot owner or a role whitelisted for this command can use it.", mention_author=False)
        return False

    def _group_data(self, guild_id, group_key):
        group_conn = sqlite3.connect(GROUP_DB)
        group_conn.row_factory = sqlite3.Row
        try:
            group = group_conn.execute(
                "SELECT group_name FROM group_styles WHERE guild_id=? AND group_key=?",
                (guild_id, group_key),
            ).fetchone()
            if group is None:
                return None, []
            teams = group_conn.execute(
                "SELECT team_name, team_name_key FROM team_groups WHERE guild_id=? AND group_key=? ORDER BY team_name COLLATE NOCASE",
                (guild_id, group_key),
            ).fetchall()
        finally:
            group_conn.close()

        points_conn = connect_points_db()
        try:
            stats = {
                row["team_name_key"]: row
                for row in points_conn.execute(
                    "SELECT team_name_key, matches_won, matches_lost FROM team_points WHERE guild_id=?",
                    (guild_id,),
                ).fetchall()
            }
        finally:
            points_conn.close()

        rows = []
        for team in teams:
            stat = stats.get(team["team_name_key"])
            wins = int(stat["matches_won"]) if stat else 0
            losses = int(stat["matches_lost"]) if stat else 0
            rows.append({
                "team_name": team["team_name"],
                "team_name_key": team["team_name_key"],
                "matches_won": wins,
                "matches_lost": losses,
                "points": wins * POINTS_PER_WIN,
            })
        rows.sort(key=lambda item: (-item["points"], -item["matches_won"], item["team_name"].casefold()))
        return group["group_name"], rows

    @commands.command(name="setpt")
    @commands.guild_only()
    async def set_points(self, ctx, *, arguments: str = ""):
        """Set a team's wins and losses: +setpt Team Name 4 1"""
        if not await self._allowed(ctx):
            return
        parts = arguments.rsplit(maxsplit=2)
        if len(parts) != 3:
            await ctx.reply("Usage: `+setpt <team name> <matches won> <matches lost>`\nExample: `+setpt Kashmir Titans 4 1`", mention_author=False)
            return
        team_name, wins_text, losses_text = parts
        try:
            wins, losses = int(wins_text), int(losses_text)
            if wins < 0 or losses < 0:
                raise ValueError
        except ValueError:
            await ctx.reply("Wins and losses must be whole numbers of 0 or more.", mention_author=False)
            return

        group_conn = sqlite3.connect(GROUP_DB)
        group_conn.row_factory = sqlite3.Row
        try:
            team = group_conn.execute(
                "SELECT team_name, team_name_key, group_key FROM team_groups WHERE guild_id=? AND team_name_key=?",
                (ctx.guild.id, norm(team_name)),
            ).fetchone()
            if team is None:
                # Allow matching the stored display name too, ignoring case and whitespace.
                candidates = group_conn.execute(
                    "SELECT team_name, team_name_key, group_key FROM team_groups WHERE guild_id=?",
                    (ctx.guild.id,),
                ).fetchall()
                team = next((item for item in candidates if norm(item["team_name"]) == norm(team_name)), None)
        finally:
            group_conn.close()
        if team is None:
            await ctx.reply("I couldn't find that team in `+setteamgroups`. Check the exact team name and try again.", mention_author=False)
            return

        conn = connect_points_db()
        try:
            conn.execute(
                "INSERT INTO team_points (guild_id, team_name_key, matches_won, matches_lost, updated_by, updated_at) "
                "VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP) "
                "ON CONFLICT(guild_id, team_name_key) DO UPDATE SET matches_won=excluded.matches_won, "
                "matches_lost=excluded.matches_lost, updated_by=excluded.updated_by, updated_at=CURRENT_TIMESTAMP",
                (ctx.guild.id, team["team_name_key"], wins, losses, ctx.author.id),
            )
            conn.commit()
        finally:
            conn.close()
        group_name_conn = sqlite3.connect(GROUP_DB)
        try:
            group_row = group_name_conn.execute(
                "SELECT group_name FROM group_styles WHERE guild_id=? AND group_key=?",
                (ctx.guild.id, team["group_key"]),
            ).fetchone()
        finally:
            group_name_conn.close()
        group_name = group_row[0] if group_row else team["group_key"]
        await ctx.reply(
            f"✅ Updated **{team['team_name']}** in Group **{group_name}**.\n"
            f"Wins: **{wins}** | Losses: **{losses}** | Points: **{wins * POINTS_PER_WIN}** (2 per win).",
            mention_author=False,
        )

    @commands.command(name="points")
    @commands.guild_only()
    async def points(self, ctx, group: str = "all"):
        """Generate one group or all group points-table images."""
        group_conn = sqlite3.connect(GROUP_DB)
        group_conn.row_factory = sqlite3.Row
        try:
            configured = group_conn.execute(
                "SELECT group_key, group_name FROM group_styles WHERE guild_id=? ORDER BY position",
                (ctx.guild.id,),
            ).fetchall()
        finally:
            group_conn.close()
        if not configured:
            await ctx.reply("No groups are configured. Run `+groupstyle` and `+setteamgroups` first.", mention_author=False)
            return

        requested = group.strip().casefold()
        if requested == "all":
            targets = configured
        else:
            target = next((item for item in configured if item["group_key"].casefold() == requested or item["group_name"].casefold() == requested), None)
            if target is None:
                await ctx.reply("That group isn't configured. Use `+points A` or `+points all`.", mention_author=False)
                return
            targets = [target]

        files = []
        try:
            for target in targets:
                group_name, rows = self._group_data(ctx.guild.id, target["group_key"])
                if not rows:
                    await ctx.send(f"Group **{group_name}** has no assigned teams. Run `+setteamgroups` first.")
                    continue
                if len(rows) > MAX_ROWS:
                    await ctx.send(f"⚠️ Group **{group_name}** has {len(rows)} teams; this template displays the first {MAX_ROWS} only.")
                image = make_table_image(group_name, rows[:MAX_ROWS])
                safe_name = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in group_name)
                files.append(discord.File(image, filename=f"points_{safe_name}.png"))
        except (OSError, sqlite3.Error) as exc:
            await ctx.reply(f"Couldn't generate the points table: {exc}", mention_author=False)
            return
        if files:
            await ctx.send(files=files)


async def setup(bot):
    await bot.add_cog(PointsTable(bot))
