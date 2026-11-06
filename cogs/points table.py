"""Manual group points tables rendered on the KT league-table template."""

import io
import re
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

# Exact row centres for the supplied 1080x1350 template.
ROW_CENTRES = [328, 430, 515, 600, 685, 770, 854, 938, 1022, 1105, 1182]

# Table column positions.
RANK_X = 145
TEAM_X = 169
PL_X = 447
W_X = 560
L_X = 678
PTS_X = 765

WHITE = (255, 255, 255)
LIME = (190, 239, 28)


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
    conn.execute("""
        CREATE TABLE IF NOT EXISTS team_match_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            team_name_key TEXT NOT NULL,
            result TEXT NOT NULL CHECK(result IN ('W', 'L')),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_team_match_history_lookup
        ON team_match_history (guild_id, team_name_key, id DESC)
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS imported_match_results (
            guild_id INTEGER NOT NULL,
            match_no TEXT NOT NULL,
            winner_team_key TEXT NOT NULL,
            loser_team_key TEXT NOT NULL,
            imported_by INTEGER NOT NULL,
            imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (guild_id, match_no)
        )
    """)
    conn.commit()
    return conn


_HERE = Path(__file__).resolve().parent

# Keep Agrandir-TextBold.otf in the same folder as this file (or in ./fonts/).
_ANG_FILES = [
    "Agrandir-TextBold.otf",
    "AngrandirWide.ttf",
    "Angrandir Wide.ttf",
    "Angrandir-Wide.ttf",
]

_SYSTEM_REGULAR = [
    "/usr/share/fonts/truetype/arimo/Arimo-Regular.ttf",
    "/usr/share/fonts/truetype/croscore/Arimo-Regular.ttf",
    "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "/Library/Fonts/Arial.ttf",
]
_SYSTEM_BOLD = [
    "/usr/share/fonts/truetype/arimo/Arimo-Bold.ttf",
    "/usr/share/fonts/truetype/croscore/Arimo-Bold.ttf",
    "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "/Library/Fonts/Arial Bold.ttf",
]

_font_cache = {}


def _try_font(path, size):
    try:
        return ImageFont.truetype(str(path), size)
    except OSError:
        return None


def load_font(size, *, ang=False, bold=False):
    """Always returns a font of the REQUESTED size.

    The old version fell back to ImageFont.load_default() (a tiny ~10px bitmap
    font) whenever no font file was found - that is why the text was tiny.
    """
    key = (size, ang, bold)
    if key in _font_cache:
        return _font_cache[key]

    candidates = []
    if ang:
        candidates += [_HERE / name for name in _ANG_FILES]
    candidates += [_HERE / "fonts" / name for name in _ANG_FILES]
    candidates += _SYSTEM_BOLD if bold else _SYSTEM_REGULAR

    font = None
    for candidate in candidates:
        font = _try_font(candidate, size)
        if font:
            break

    if font is None:
        try:  # Pillow >= 10.1 ships a scalable built-in font
            font = ImageFont.load_default(size)
        except TypeError:
            font = ImageFont.load_default()

    _font_cache[key] = font
    return font


def draw_centered(draw, x, y, text, font, fill=WHITE):
    draw.text((x, y), str(text), font=font, fill=fill, anchor="mm")


def fit_font(draw, text, max_width, start_size, min_size=14):
    size = start_size
    while size >= min_size:
        font = load_font(size, ang=True)
        box = draw.textbbox((0, 0), str(text), font=font)
        if (box[2] - box[0]) <= max_width:
            return font
        size -= 1
    return load_font(min_size, ang=True)



def draw_form_badge(draw, x, y, result):
    """Draw one of the last-five W/L form circles."""
    if result == "W":
        fill = (38, 169, 47)
    else:
        fill = (255, 58, 63)

    radius = 13
    draw.ellipse(
        (x - radius, y - radius, x + radius, y + radius),
        fill=fill,
    )
    font = load_font(15, bold=True)
    draw.text((x, y + 1), result, font=font, fill=WHITE, anchor="mm")


def draw_form(draw, y, history):
    # Corrected vertically to sit centred on the row, not above it.
    # Oldest -> newest left to right.
    positions = [852, 882, 912, 942, 972]
    recent = list(history)[-5:]

    for x, result in zip(positions[-len(recent):], recent):
        draw_form_badge(draw, x, y, result)


def make_table_image(group_name, rows):
    if not TEMPLATE.exists():
        raise FileNotFoundError(
            f"Points-table background is missing: {TEMPLATE.name}"
        )

    image = Image.open(TEMPLATE).convert("RGB")
    draw = ImageDraw.Draw(image)

    # The supplied background already contains "Group A".
    # For B/C, cover only the A character and draw the requested letter.
    group_letter = str(group_name).strip().upper()
    if group_letter != "A":
        # Sample the nearby background colour so only the letter changes.
        cover = image.crop((215, 188, 252, 222)).resize((1, 1)).getpixel((0, 0))
        draw.rectangle((214, 186, 254, 224), fill=cover)
        group_font = load_font(31, ang=True)
        draw.text((217, 208), group_letter, font=group_font, fill=LIME, anchor="lm")

    rank_font = load_font(25, ang=True)
    team_start_size = 23
    stat_font = load_font(25, ang=True)

    for index, row in enumerate(rows[:MAX_ROWS]):
        y = ROW_CENTRES[index]

        rank = index + 1
        name = str(row["team_name"])
        wins = int(row["matches_won"])
        losses = int(row["matches_lost"])
        played = wins + losses
        points = int(row["points"])

        # Rank numbers are already printed on the template - don't redraw them.

        # Team name
        team_font = fit_font(draw, name, max_width=235, start_size=team_start_size)
        draw.text((TEAM_X, y), name.upper(), font=team_font, fill=WHITE, anchor="lm")

        # Statistics
        draw_centered(draw, PL_X, y, played, stat_font)
        draw_centered(draw, W_X, y, wins, stat_font)
        draw_centered(draw, L_X, y, losses, stat_font)

        # PTS is slightly heavier in the reference.
        pts_font = load_font(27, ang=True, bold=True)
        draw_centered(draw, PTS_X, y, points, pts_font)

        # Latest five match results, oldest -> newest.
        draw_form(draw, y, row.get("history", []))

    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    output.seek(0)
    return output


class PointsTable(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        connect_points_db().close()

    async def _allowed(self, ctx):
        if await owner_or_whitelisted(
            ctx,
            command_name=ctx.command.name if ctx.command else None,
            app_owner=True,
        ):
            return True
        await ctx.reply(
            "Only the bot owner or a role whitelisted for this command can use it.",
            mention_author=False,
        )
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
                """
                SELECT team_name, team_name_key
                FROM team_groups
                WHERE guild_id=? AND group_key=?
                ORDER BY team_name COLLATE NOCASE
                """,
                (guild_id, group_key),
            ).fetchall()
        finally:
            group_conn.close()

        points_conn = connect_points_db()
        try:
            stats = {
                row["team_name_key"]: row
                for row in points_conn.execute(
                    """
                    SELECT team_name_key, matches_won, matches_lost
                    FROM team_points
                    WHERE guild_id=?
                    """,
                    (guild_id,),
                ).fetchall()
            }
            histories = {}
            history_rows = points_conn.execute(
                """
                SELECT team_name_key, result
                FROM team_match_history
                WHERE guild_id=?
                ORDER BY id ASC
                """,
                (guild_id,),
            ).fetchall()

            for history_row in history_rows:
                histories.setdefault(history_row["team_name_key"], []).append(
                    history_row["result"]
                )
        finally:
            points_conn.close()

        rows = []
        for team in teams:
            stat = stats.get(team["team_name_key"])
            wins = int(stat["matches_won"]) if stat else 0
            losses = int(stat["matches_lost"]) if stat else 0

            history = histories.get(team["team_name_key"], [])
            # +setpt sets totals without logging matches, so the history can be
            # shorter than W+L. Treat the missing matches as older results.
            missing_w = max(0, wins - history.count("W"))
            missing_l = max(0, losses - history.count("L"))
            history = ["W"] * missing_w + ["L"] * missing_l + history

            rows.append({
                "team_name": team["team_name"],
                "team_name_key": team["team_name_key"],
                "matches_won": wins,
                "matches_lost": losses,
                "points": wins * POINTS_PER_WIN,
                "history": history[-5:],
            })

        # KT rules: win = 2 points, loss = 0, no draws.
        rows.sort(
            key=lambda item: (
                -item["points"],
                -item["matches_won"],
                item["team_name"].casefold(),
            )
        )

        return group["group_name"], rows

    @commands.command(name="setpt")
    @commands.guild_only()
    async def set_points(self, ctx, *, arguments: str = ""):
        """Set a team's wins and losses: +setpt Team Name 4 1"""
        if not await self._allowed(ctx):
            return

        parts = arguments.rsplit(maxsplit=2)
        if len(parts) != 3:
            await ctx.reply(
                "Usage: `+setpt <team name> <matches won> <matches lost>`\n"
                "Example: `+setpt Kashmir Titans 4 1`",
                mention_author=False,
            )
            return

        team_name, wins_text, losses_text = parts

        try:
            wins, losses = int(wins_text), int(losses_text)
            if wins < 0 or losses < 0:
                raise ValueError
        except ValueError:
            await ctx.reply(
                "Wins and losses must be whole numbers of 0 or more.",
                mention_author=False,
            )
            return

        group_conn = sqlite3.connect(GROUP_DB)
        group_conn.row_factory = sqlite3.Row
        try:
            team = group_conn.execute(
                """
                SELECT team_name, team_name_key, group_key
                FROM team_groups
                WHERE guild_id=? AND team_name_key=?
                """,
                (ctx.guild.id, norm(team_name)),
            ).fetchone()

            if team is None:
                candidates = group_conn.execute(
                    """
                    SELECT team_name, team_name_key, group_key
                    FROM team_groups
                    WHERE guild_id=?
                    """,
                    (ctx.guild.id,),
                ).fetchall()

                team = next(
                    (
                        item for item in candidates
                        if norm(item["team_name"]) == norm(team_name)
                    ),
                    None,
                )
        finally:
            group_conn.close()

        if team is None:
            await ctx.reply(
                "I couldn't find that team in `+setteamgroups`. "
                "Check the exact team name and try again.",
                mention_author=False,
            )
            return

        conn = connect_points_db()
        try:
            conn.execute(
                """
                INSERT INTO team_points
                    (guild_id, team_name_key, matches_won, matches_lost,
                     updated_by, updated_at)
                VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(guild_id, team_name_key) DO UPDATE SET
                    matches_won=excluded.matches_won,
                    matches_lost=excluded.matches_lost,
                    updated_by=excluded.updated_by,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (
                    ctx.guild.id,
                    team["team_name_key"],
                    wins,
                    losses,
                    ctx.author.id,
                ),
            )

            # +setpt is an overwrite, so rebuild the form history to match the
            # new totals exactly (old results must not survive an overwrite).
            conn.execute(
                "DELETE FROM team_match_history WHERE guild_id=? AND team_name_key=?",
                (ctx.guild.id, team["team_name_key"]),
            )
            conn.executemany(
                "INSERT INTO team_match_history (guild_id, team_name_key, result) "
                "VALUES (?, ?, ?)",
                [(ctx.guild.id, team["team_name_key"], r)
                 for r in ["W"] * wins + ["L"] * losses],
            )
            conn.commit()
        finally:
            conn.close()

        group_name_conn = sqlite3.connect(GROUP_DB)
        try:
            group_row = group_name_conn.execute(
                """
                SELECT group_name
                FROM group_styles
                WHERE guild_id=? AND group_key=?
                """,
                (ctx.guild.id, team["group_key"]),
            ).fetchone()
        finally:
            group_name_conn.close()

        group_name = group_row[0] if group_row else team["group_key"]

        await ctx.reply(
            f"✅ Updated **{team['team_name']}** in Group **{group_name}**.\n"
            f"Wins: **{wins}** | Losses: **{losses}** | "
            f"Points: **{wins * POINTS_PER_WIN}** (2 per win).",
            mention_author=False,
        )

    async def _addresult_from_reply(self, ctx):
        """Import both teams' W/L from a reply to a published match result."""
        reference = ctx.message.reference
        if reference is None or reference.message_id is None:
            await ctx.reply(
                "Reply to a match result message in the results channel, then run `+addresult`.",
                mention_author=False,
            )
            return

        replied = reference.resolved
        if not isinstance(replied, discord.Message):
            try:
                channel_id = getattr(reference, "channel_id", None) or ctx.channel.id
                channel = ctx.guild.get_channel(channel_id) or await self.bot.fetch_channel(channel_id)
                replied = await channel.fetch_message(reference.message_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException, AttributeError):
                await ctx.reply("I couldn't fetch the message you replied to.", mention_author=False)
                return

        if replied.guild is None or replied.guild.id != ctx.guild.id:
            await ctx.reply("The replied result must be from this server.", mention_author=False)
            return
        if replied.author.id != self.bot.user.id:
            await ctx.reply("Reply directly to a match-result message posted by this bot.", mention_author=False)
            return

        body = replied.content.strip()
        match_header = re.search(r"^# __Match (.+?)__\s*\n\*\*(.+?) vs (.+?)\*\*", body, re.IGNORECASE)
        winner_line = re.search(r">\s*\*{3}\[(.+?)\]\((https?://[^)]+)\)\s+won by\s+\d+\s+(?:runs?|wickets?)\*{3}", body, re.IGNORECASE)
        if not match_header or not winner_line:
            await ctx.reply(
                "That message doesn't match the published match-result format. "
                "Reply to the actual result post; its format remains unchanged.",
                mention_author=False,
            )
            return

        match_no = match_header.group(1).strip()
        team_a_name = match_header.group(2).strip()
        team_b_name = match_header.group(3).strip()
        winner_name = winner_line.group(1).strip()

        if norm(team_a_name) == norm(team_b_name):
            await ctx.reply("The result message contains the same team twice.", mention_author=False)
            return

        if norm(winner_name) == norm(team_a_name):
            winner_input, loser_input = team_a_name, team_b_name
        elif norm(winner_name) == norm(team_b_name):
            winner_input, loser_input = team_b_name, team_a_name
        else:
            await ctx.reply("The winner listed in the result doesn't match either team.", mention_author=False)
            return

        group_conn = sqlite3.connect(GROUP_DB)
        group_conn.row_factory = sqlite3.Row
        try:
            assigned = group_conn.execute(
                "SELECT team_name, team_name_key FROM team_groups WHERE guild_id=?",
                (ctx.guild.id,),
            ).fetchall()
            def resolve_team(supplied):
                key = norm(supplied)
                return next((row for row in assigned
                             if norm(row["team_name"]) == key or norm(row["team_name_key"]) == key), None)
            winner = resolve_team(winner_input)
            loser = resolve_team(loser_input)
        finally:
            group_conn.close()

        if winner is None or loser is None:
            missing = winner_input if winner is None else loser_input
            await ctx.reply(
                f"Couldn't match **{missing}** to a team assigned with `+setteamgroups`. "
                "Make sure both teams are assigned to the points table.",
                mention_author=False,
            )
            return
        if winner["team_name_key"] == loser["team_name_key"]:
            await ctx.reply("Both result teams resolved to the same points-table team.", mention_author=False)
            return

        conn = connect_points_db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            duplicate = conn.execute(
                "SELECT 1 FROM imported_match_results WHERE guild_id=? AND match_no=?",
                (ctx.guild.id, match_no),
            ).fetchone()
            if duplicate:
                conn.rollback()
                await ctx.reply(f"Match **{match_no}** has already been imported into the points table.", mention_author=False)
                return

            conn.execute(
                "INSERT INTO imported_match_results (guild_id, match_no, winner_team_key, loser_team_key, imported_by) VALUES (?, ?, ?, ?, ?)",
                (ctx.guild.id, match_no, winner["team_name_key"], loser["team_name_key"], ctx.author.id),
            )

            for team, result in ((winner, "W"), (loser, "L")):
                conn.execute(
                    """
                    INSERT INTO team_points
                        (guild_id, team_name_key, matches_won, matches_lost, updated_by, updated_at)
                    VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(guild_id, team_name_key) DO UPDATE SET
                        matches_won=team_points.matches_won + excluded.matches_won,
                        matches_lost=team_points.matches_lost + excluded.matches_lost,
                        updated_by=excluded.updated_by,
                        updated_at=CURRENT_TIMESTAMP
                    """,
                    (ctx.guild.id, team["team_name_key"], 1 if result == "W" else 0,
                     1 if result == "L" else 0, ctx.author.id),
                )
                conn.execute(
                    "INSERT INTO team_match_history (guild_id, team_name_key, result) VALUES (?, ?, ?)",
                    (ctx.guild.id, team["team_name_key"], result),
                )
            conn.commit()
        except sqlite3.Error:
            conn.rollback()
            raise
        finally:
            conn.close()

        await ctx.reply(
            f"✅ Imported **Match {match_no}** into the points table.\n"
            f"🏆 **{winner['team_name']}**: +1 Win, +{POINTS_PER_WIN} Points\n"
            f"**{loser['team_name']}**: +1 Loss, +0 Points\n"
            "Both teams' PL/W/L/PTS and recent form have been updated.",
            mention_author=False,
        )

    @commands.command(name="addresult")
    @commands.guild_only()
    async def add_result(self, ctx, *, arguments: str = ""):
        """Record one match result and automatically update W/L/PTS."""
        if not await self._allowed(ctx):
            return

        if not arguments.strip():
            await self._addresult_from_reply(ctx)
            return

        parts = arguments.rsplit(maxsplit=1)
        if len(parts) != 2:
            await ctx.reply(
                "Usage: `+addresult <team name> <W/L>`\n"
                "Example: `+addresult Kashmir Titans W`",
                mention_author=False,
            )
            return

        team_name, result = parts
        result = result.strip().upper()

        if result not in ("W", "L"):
            await ctx.reply(
                "Result must be `W` or `L`.\n"
                "Example: `+addresult Kashmir Titans W`",
                mention_author=False,
            )
            return

        group_conn = sqlite3.connect(GROUP_DB)
        group_conn.row_factory = sqlite3.Row
        try:
            team = group_conn.execute(
                """
                SELECT team_name, team_name_key, group_key
                FROM team_groups
                WHERE guild_id=? AND team_name_key=?
                """,
                (ctx.guild.id, norm(team_name)),
            ).fetchone()

            if team is None:
                candidates = group_conn.execute(
                    """
                    SELECT team_name, team_name_key, group_key
                    FROM team_groups
                    WHERE guild_id=?
                    """,
                    (ctx.guild.id,),
                ).fetchall()
                team = next(
                    (
                        item for item in candidates
                        if norm(item["team_name"]) == norm(team_name)
                    ),
                    None,
                )
        finally:
            group_conn.close()

        if team is None:
            await ctx.reply(
                "I couldn't find that team in `+setteamgroups`.",
                mention_author=False,
            )
            return

        conn = connect_points_db()
        try:
            existing = conn.execute(
                """
                SELECT matches_won, matches_lost
                FROM team_points
                WHERE guild_id=? AND team_name_key=?
                """,
                (ctx.guild.id, team["team_name_key"]),
            ).fetchone()

            wins = int(existing["matches_won"]) if existing else 0
            losses = int(existing["matches_lost"]) if existing else 0

            if result == "W":
                wins += 1
            else:
                losses += 1

            conn.execute(
                """
                INSERT INTO team_points
                    (guild_id, team_name_key, matches_won, matches_lost,
                     updated_by, updated_at)
                VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(guild_id, team_name_key) DO UPDATE SET
                    matches_won=excluded.matches_won,
                    matches_lost=excluded.matches_lost,
                    updated_by=excluded.updated_by,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (
                    ctx.guild.id,
                    team["team_name_key"],
                    wins,
                    losses,
                    ctx.author.id,
                ),
            )

            conn.execute(
                """
                INSERT INTO team_match_history
                    (guild_id, team_name_key, result)
                VALUES (?, ?, ?)
                """,
                (ctx.guild.id, team["team_name_key"], result),
            )

            conn.commit()
        finally:
            conn.close()

        await ctx.reply(
            f"✅ Recorded **{result}** for **{team['team_name']}**.\n"
            f"PL: **{wins + losses}** | W: **{wins}** | "
            f"L: **{losses}** | PTS: **{wins * POINTS_PER_WIN}**",
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
                """
                SELECT group_key, group_name
                FROM group_styles
                WHERE guild_id=?
                ORDER BY position
                """,
                (ctx.guild.id,),
            ).fetchall()
        finally:
            group_conn.close()

        if not configured:
            await ctx.reply(
                "No groups are configured. Run `+groupstyle` and "
                "`+setteamgroups` first.",
                mention_author=False,
            )
            return

        requested = group.strip().casefold()

        if requested == "all":
            targets = configured
        else:
            target = next(
                (
                    item for item in configured
                    if item["group_key"].casefold() == requested
                    or item["group_name"].casefold() == requested
                ),
                None,
            )

            if target is None:
                await ctx.reply(
                    "That group isn't configured. Use `+points A` or "
                    "`+points all`.",
                    mention_author=False,
                )
                return

            targets = [target]

        files = []

        try:
            for target in targets:
                group_name, rows = self._group_data(
                    ctx.guild.id,
                    target["group_key"],
                )

                if not rows:
                    await ctx.send(
                        f"Group **{group_name}** has no assigned teams. "
                        "Run `+setteamgroups` first."
                    )
                    continue

                if len(rows) > MAX_ROWS:
                    await ctx.send(
                        f"⚠️ Group **{group_name}** has {len(rows)} teams; "
                        f"this template displays the first {MAX_ROWS} only."
                    )

                image = make_table_image(group_name, rows[:MAX_ROWS])

                safe_name = "".join(
                    ch if ch.isalnum() or ch in "-_" else "_"
                    for ch in group_name
                )

                files.append(
                    discord.File(
                        image,
                        filename=f"points_{safe_name}.png",
                    )
                )

        except (OSError, sqlite3.Error) as exc:
            await ctx.reply(
                f"Couldn't generate the points table: {exc}",
                mention_author=False,
            )
            return

        if files:
            await ctx.send(files=files)


async def setup(bot):
    await bot.add_cog(PointsTable(bot))
