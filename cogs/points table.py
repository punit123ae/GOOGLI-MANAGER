"""Manual group points tables rendered on the KT league-table template."""

import asyncio
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
TEMPLATE = Path(__file__).with_name("points_table_landscape_bg.png")

# Rendered group images are saved here and only re-drawn when results change
# (+addresult / +setpt). +points just sends the saved files.
CACHE_DIR = Path("points_cache")

MAX_ROWS = 11
POINTS_PER_WIN = 2

# Layout for the 1376x768 landscape template.
ROW0_Y = 296        # centre of row 1
ROW_H = 39.9        # distance between row centres


def row_y(index):
    return round(ROW0_Y + ROW_H * index)


TEAM_X = 215        # team names are left aligned here
PL_X = 636          # centre of the P column
W_X = 740           # centre of the W column
L_X = 844           # centre of the L column
PTS_X = 967         # centre of the POINTS column
FORM_LEFT = 1038    # FORM column spans FORM_LEFT..FORM_RIGHT (5 slots)
FORM_RIGHT = 1276
GROUP_LABEL_POS = (688, 744)

WHITE = (255, 255, 255)
GREEN = (46, 230, 90)
RED = (255, 64, 64)
CYAN = (0, 229, 255)


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



def draw_form(draw, y, history):
    """Last five results as coloured letters, oldest on the left.

    With fewer than five matches the letters fill slots from the left; once a
    6th match is played the oldest result drops off and the newest is added on
    the right (history is already trimmed to the last 5 by the caller).
    """
    recent = list(history)[-5:]
    slot_width = (FORM_RIGHT - FORM_LEFT) / 5
    form_font = load_font(26, ang=True)

    for slot, result in enumerate(recent):
        x = FORM_LEFT + slot_width * (slot + 0.5)
        colour = GREEN if result == "W" else RED
        draw_centered(draw, round(x), y, result, form_font, fill=colour)


def group_label(group_name):
    name = str(group_name).strip()
    if name.casefold().startswith("group"):
        return name.upper()
    return f"GROUP {name.upper()}"


def make_table_image(group_name, rows):
    if not TEMPLATE.exists():
        raise FileNotFoundError(
            f"Points-table background is missing: {TEMPLATE.name}"
        )

    image = Image.open(TEMPLATE).convert("RGB")
    draw = ImageDraw.Draw(image)

    # Group name under the table.
    draw_centered(
        draw, GROUP_LABEL_POS[0], GROUP_LABEL_POS[1],
        group_label(group_name), load_font(34, ang=True), fill=CYAN,
    )

    team_start_size = 27
    stat_font = load_font(26, ang=True)

    for index, row in enumerate(rows[:MAX_ROWS]):
        y = row_y(index)

        name = str(row["team_name"])
        wins = int(row["matches_won"])
        losses = int(row["matches_lost"])
        played = wins + losses
        points = int(row["points"])

        # Rank numbers are already printed on the template - don't redraw them.

        # Team name (white)
        team_font = fit_font(draw, name, max_width=350, start_size=team_start_size)
        draw.text((TEAM_X, y), name.upper(), font=team_font, fill=WHITE, anchor="lm")

        # P (white), W (green), L (red), POINTS (white)
        draw_centered(draw, PL_X, y, played, stat_font, fill=WHITE)
        draw_centered(draw, W_X, y, wins, stat_font, fill=GREEN)
        draw_centered(draw, L_X, y, losses, stat_font, fill=RED)
        draw_centered(draw, PTS_X, y, points, stat_font, fill=WHITE)

        # Latest five match results, oldest -> newest.
        draw_form(draw, y, row.get("history", []))

    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    output.seek(0)
    return output


def cache_file(guild_id, group_key):
    safe_key = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in str(group_key))
    return CACHE_DIR / f"points_{guild_id}_{safe_key}.png"


class PointsPagesView(discord.ui.View):
    """Prev / Next buttons that flip between the saved group images."""

    def __init__(self, owner_id, pages):
        super().__init__(timeout=300)
        self.owner_id = owner_id
        self.pages = pages          # list of (group_name, png_bytes)
        self.index = 0
        self.message = None

    def _file(self):
        group_name, data = self.pages[self.index]
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in group_name)
        return discord.File(io.BytesIO(data), filename=f"points_{safe}.png")

    def content(self):
        return f"Page {self.index + 1}/{len(self.pages)}"

    async def interaction_check(self, interaction):
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message(
            "Run `+points` yourself to flip through the pages.", ephemeral=True
        )
        return False

    async def _turn(self, interaction, step):
        self.index = (self.index + step) % len(self.pages)
        await interaction.response.edit_message(
            content=self.content(), attachments=[self._file()], view=self
        )

    @discord.ui.button(label="◀ Prev", style=discord.ButtonStyle.secondary)
    async def prev_page(self, interaction, button):
        await self._turn(interaction, -1)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.primary)
    async def next_page(self, interaction, button):
        await self._turn(interaction, 1)

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


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

    def _configured_groups(self, guild_id):
        group_conn = sqlite3.connect(GROUP_DB)
        group_conn.row_factory = sqlite3.Row
        try:
            return group_conn.execute(
                """
                SELECT group_key, group_name
                FROM group_styles
                WHERE guild_id=?
                ORDER BY position
                """,
                (guild_id,),
            ).fetchall()
        finally:
            group_conn.close()

    def _render_and_save_guild(self, guild_id):
        """Draw every group's table and save the PNGs. Runs in a worker thread."""
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        for group in self._configured_groups(guild_id):
            group_name, rows = self._group_data(guild_id, group["group_key"])
            if not rows:
                continue
            image = make_table_image(group_name, rows[:MAX_ROWS])
            cache_file(guild_id, group["group_key"]).write_bytes(image.getvalue())

    async def _sync_points_images(self, guild_id):
        """Re-draw and save the points-table images (only called when results change)."""
        await asyncio.to_thread(self._render_and_save_guild, guild_id)

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

        await self._sync_points_images(ctx.guild.id)

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

        await self._sync_points_images(ctx.guild.id)

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

        await self._sync_points_images(ctx.guild.id)

        await ctx.reply(
            f"✅ Recorded **{result}** for **{team['team_name']}**.\n"
            f"PL: **{wins + losses}** | W: **{wins}** | "
            f"L: **{losses}** | PTS: **{wins * POINTS_PER_WIN}**",
            mention_author=False,
        )

    @commands.command(name="points")
    @commands.guild_only()
    async def points(self, ctx):
        """Show the points table: one page per group (A, B, C), flip with the buttons."""
        configured = self._configured_groups(ctx.guild.id)

        if not configured:
            await ctx.reply(
                "No groups are configured. Run `+groupstyle` and "
                "`+setteamgroups` first.",
                mention_author=False,
            )
            return

        pages = []
        try:
            for group in configured:
                path = cache_file(ctx.guild.id, group["group_key"])
                if not path.exists():
                    # First use only: nothing saved yet, so draw everything once.
                    await self._sync_points_images(ctx.guild.id)
                if path.exists():
                    pages.append((group["group_name"], path.read_bytes()))
        except (OSError, sqlite3.Error) as exc:
            await ctx.reply(
                f"Couldn't load the points table: {exc}",
                mention_author=False,
            )
            return

        if not pages:
            await ctx.reply(
                "No group has assigned teams yet. Run `+setteamgroups` first.",
                mention_author=False,
            )
            return

        if len(pages) == 1:
            name, data = pages[0]
            await ctx.send(file=discord.File(io.BytesIO(data), filename="points_table.png"))
            return

        view = PointsPagesView(ctx.author.id, pages)
        view.message = await ctx.send(
            content=view.content(), file=view._file(), view=view
        )


async def setup(bot):
    await bot.add_cog(PointsTable(bot))
