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
HEADER_RE = re.compile(
    r"^[#\s_*]*Match\s+([^\n_*]+?)[_*\s]*\n+[\s*_]*(.+?)\s+vs\.?\s+(.+?)[\s*_]*$",
    re.IGNORECASE | re.MULTILINE,
)
WINNER_LINE_RE = re.compile(
    r"^[ \t]*>?[ \t]*\*{0,3}\[?([^\n\[\]*]+?)\]?(?:\(https?://[^)\s]+\))?\*{0,3}[ \t]+won\b",
    re.IGNORECASE | re.MULTILINE,
)
SYNC_SCAN_LIMIT = 500   # max messages +syncresult scans after the replied message
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
    # Older databases don't have match_no on the history table yet.
    history_cols = [r[1] for r in conn.execute("PRAGMA table_info(team_match_history)")]
    if "match_no" not in history_cols:
        conn.execute("ALTER TABLE team_match_history ADD COLUMN match_no TEXT")
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
    conn.execute("""
        CREATE TABLE IF NOT EXISTS points_settings (
            guild_id INTEGER PRIMARY KEY,
            stage_key TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn


def _stage_list(guild_id):
    """[(stage_key, stage_name)] of this server, oldest first."""
    if not Path(GROUP_DB).is_file():
        return []
    conn = sqlite3.connect(GROUP_DB)
    try:
        rows = [(r[0], r[1]) for r in conn.execute(
            "SELECT stage_key, stage_name FROM tournament_stages WHERE guild_id=? ORDER BY created_at, rowid",
            (guild_id,))]
        if not rows:    # stages table empty: fall back to the stage prefixes of the saved groups
            seen = []
            for (group_key,) in conn.execute(
                    "SELECT group_key FROM group_styles WHERE guild_id=? ORDER BY position", (guild_id,)):
                prefix = str(group_key).split("::", 1)[0] if "::" in str(group_key) else "stage 1"
                if prefix not in seen:
                    seen.append(prefix)
            rows = [(k, k.title()) for k in seen]
        return rows
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def first_stage_key(guild_id):
    stages = _stage_list(guild_id)
    return stages[0][0] if stages else None


def find_stage(guild_id, text):
    """(stage_key, stage_name) for what the owner typed (name or key, any case), else None."""
    wanted = norm(text)
    return next(((k, n) for k, n in _stage_list(guild_id) if norm(k) == wanted or norm(n) == wanted), None)


def get_active_stage(guild_id):
    """(stage_key, stage_name) that +points shows and +setpt / +addresult / +syncresult record into:
    the one chosen with +pointsstage, otherwise the newest stage that already has teams assigned."""
    stages = _stage_list(guild_id)
    if not stages:
        return None
    conn = connect_points_db()
    try:
        row = conn.execute("SELECT stage_key FROM points_settings WHERE guild_id=?", (guild_id,)).fetchone()
    finally:
        conn.close()
    if row:
        chosen = next((s for s in stages if s[0] == row["stage_key"]), None)
        if chosen:
            return chosen
    with_teams = set()
    gconn = sqlite3.connect(GROUP_DB)
    try:
        for (group_key,) in gconn.execute("SELECT DISTINCT group_key FROM team_groups WHERE guild_id=?", (guild_id,)):
            with_teams.add(str(group_key).split("::", 1)[0])
    except sqlite3.Error:
        pass
    finally:
        gconn.close()
    for stage in reversed(stages):
        if stage[0] in with_teams:
            return stage
    return stages[0]


def set_active_stage(guild_id, stage_key):
    conn = connect_points_db()
    try:
        conn.execute(
            "INSERT INTO points_settings (guild_id, stage_key) VALUES (?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET stage_key=excluded.stage_key",
            (guild_id, stage_key))
        conn.commit()
    finally:
        conn.close()


def points_storage_key(conn, guild_id, team_name_key, legacy_ok=None):
    """Key a team's W/L is stored under.

    Only the FIRST stage may reuse the old un-prefixed key (that is where the points from before
    stages existed live). Every other stage has its own key, so a team that plays in Stage 1 and
    Stage 2 has separate points in each."""
    candidates = [team_name_key]
    if "::" in team_name_key:
        prefix, rest = team_name_key.split("::", 1)
        if legacy_ok is None:
            legacy_ok = prefix == first_stage_key(guild_id)
        if legacy_ok:
            candidates.append(rest)

    for candidate in candidates:
        found = conn.execute(
            """
            SELECT 1 FROM team_points
            WHERE guild_id=? AND team_name_key=?
            UNION ALL
            SELECT 1 FROM team_match_history
            WHERE guild_id=? AND team_name_key=?
            LIMIT 1
            """,
            (guild_id, candidate, guild_id, candidate),
        ).fetchone()
        if found:
            return candidate
    return team_name_key


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

    def __init__(self, owner_id, pages, stage_name=""):
        super().__init__(timeout=300)
        self.owner_id = owner_id
        self.pages = pages          # list of (group_name, png_bytes)
        self.stage_name = stage_name
        self.index = 0
        self.message = None

    def _file(self):
        group_name, data = self.pages[self.index]
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in group_name)
        return discord.File(io.BytesIO(data), filename=f"points_{safe}.png")

    def content(self):
        head = f"**{self.stage_name}** · " if self.stage_name else ""
        return f"{head}Page {self.index + 1}/{len(self.pages)}"

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


class ConfirmResetView(discord.ui.View):
    """Confirm / Cancel buttons for +resetpt (only the command author can press)."""

    def __init__(self, author_id):
        super().__init__(timeout=30)
        self.author_id = author_id
        self.confirmed = None
        self.message = None

    async def interaction_check(self, interaction):
        if interaction.user.id == self.author_id:
            return True
        await interaction.response.send_message("Only the person who ran the command can confirm.", ephemeral=True)
        return False

    @discord.ui.button(label="Yes, reset everything", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction, button):
        self.confirmed = True
        await interaction.response.edit_message(content="Resetting...", view=None)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, button):
        self.confirmed = False
        await interaction.response.edit_message(content="Reset cancelled.", view=None)
        self.stop()


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
                SELECT id, team_name_key, result, match_no
                FROM team_match_history
                WHERE guild_id=?
                ORDER BY id ASC
                """,
                (guild_id,),
            ).fetchall()

            def form_order(item):
                # Imported matches are ordered by match number, whatever order
                # they were synced in. Manual / +setpt entries (no number) count
                # as older; non-numeric labels (Final, SF1...) count as newer.
                number = (item["match_no"] or "").strip()
                if not number:
                    return (-1, 0, item["id"])
                if number.isdigit():
                    return (0, int(number), item["id"])
                return (1, 0, item["id"])

            for history_row in sorted(history_rows, key=form_order):
                histories.setdefault(history_row["team_name_key"], []).append(
                    history_row["result"]
                )
            legacy_ok = str(group_key).split("::", 1)[0] == first_stage_key(guild_id)
            storage_keys = {
                team["team_name_key"]: points_storage_key(points_conn, guild_id, team["team_name_key"], legacy_ok)
                for team in teams
            }
        finally:
            points_conn.close()

        rows = []
        for team in teams:
            storage_key = storage_keys[team["team_name_key"]]
            stat = stats.get(storage_key)
            wins = int(stat["matches_won"]) if stat else 0
            losses = int(stat["matches_lost"]) if stat else 0

            history = histories.get(storage_key, [])
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

    def _configured_groups(self, guild_id, stage_key=None):
        """Groups of the server (one stage when stage_key is given), in group order."""
        group_conn = sqlite3.connect(GROUP_DB)
        group_conn.row_factory = sqlite3.Row
        try:
            if stage_key is None:
                return group_conn.execute(
                    """
                    SELECT group_key, group_name
                    FROM group_styles
                    WHERE guild_id=?
                    ORDER BY position
                    """,
                    (guild_id,),
                ).fetchall()
            prefix = stage_key + "::"
            return group_conn.execute(
                """
                SELECT group_key, group_name
                FROM group_styles
                WHERE guild_id=? AND substr(group_key, 1, ?) = ?
                ORDER BY position
                """,
                (guild_id, len(prefix), prefix),
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

        stage = get_active_stage(ctx.guild.id)
        stage_label = stage[1] if stage else "?"
        team = self._find_team(ctx.guild.id, team_name)

        if team is None:
            await ctx.reply(
                f"I couldn't find that team in **{stage_label}** (`+setteamgroups {stage_label}`). "
                "Check the exact team name, or pick another stage with `+pointsstage`.",
                mention_author=False,
            )
            return

        conn = connect_points_db()
        try:
            storage_key = points_storage_key(conn, ctx.guild.id, team["team_name_key"])
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
                    storage_key,
                    wins,
                    losses,
                    ctx.author.id,
                ),
            )

            # +setpt is an overwrite, so rebuild the form history to match the
            # new totals exactly (old results must not survive an overwrite).
            conn.execute(
                "DELETE FROM team_match_history WHERE guild_id=? AND team_name_key=?",
                (ctx.guild.id, storage_key),
            )
            conn.executemany(
                "INSERT INTO team_match_history (guild_id, team_name_key, result) "
                "VALUES (?, ?, ?)",
                [(ctx.guild.id, storage_key, r)
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
            f"✅ Updated **{team['team_name']}** in **{stage_label}** · Group **{group_name}**.\n"
            f"Wins: **{wins}** | Losses: **{losses}** | "
            f"Points: **{wins * POINTS_PER_WIN}** (2 per win).",
            mention_author=False,
        )

    # ------------------------------------------------------------------
    # Result import helpers (shared by +addresult and +syncresult)
    # ------------------------------------------------------------------

    @staticmethod
    def _message_text(message):
        """Message content plus any embed text (results may be posted as embeds)."""
        parts = [message.content or ""]
        for embed in message.embeds:
            for piece in (embed.title, embed.description):
                if piece:
                    parts.append(piece)
            for field in embed.fields:
                parts.append(f"{field.name}\n{field.value}")
        return "\n".join(p for p in parts if p).strip()

    @staticmethod
    def _parse_result(body):
        """Return ((match_no, team_a, team_b, winner), None) or (None, error)."""
        header = HEADER_RE.search(body)
        if not header:
            return None, "I couldn't find the `Match N` / `Team A vs Team B` header."

        winner_line = WINNER_LINE_RE.search(body)
        if not winner_line:
            return None, ("I couldn't find a `<team> won by ...` line "
                          "(ties / no-results can't be imported automatically).")

        def clean(text):
            return text.strip().strip("*_~ \t")

        match_no = clean(header.group(1))
        team_a = clean(header.group(2))
        team_b = clean(header.group(3))
        winner = clean(winner_line.group(1))

        if norm(team_a) == norm(team_b):
            return None, "The result message contains the same team twice."
        if norm(winner) not in (norm(team_a), norm(team_b)):
            return None, "The winner listed in the result doesn't match either team."
        return (match_no, team_a, team_b, winner), None

    def _find_team(self, guild_id, name, stage_key=None):
        """Look a team up in +setteamgroups by name or key - inside the points stage only
        (the one from +pointsstage), so the same team in two stages never gets mixed up."""
        key = norm(name)
        if stage_key is None:
            stage = get_active_stage(guild_id)
            stage_key = stage[0] if stage else None
        conn = sqlite3.connect(GROUP_DB)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT team_name, team_name_key, group_key FROM team_groups WHERE guild_id=?",
                (guild_id,),
            ).fetchall()
        finally:
            conn.close()
        if stage_key is not None:
            prefix = stage_key + "::"
            rows = [r for r in rows if str(r["group_key"]).startswith(prefix)]
        return next(
            (r for r in rows if norm(r["team_name"]) == key or norm(r["team_name_key"]) == key),
            None,
        )

    @staticmethod
    def _bump(conn, guild_id, storage_key, result, user_id, match_no=None):
        """Add one W or L to a team (atomic increment + history row)."""
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
            (guild_id, storage_key, 1 if result == "W" else 0,
             1 if result == "L" else 0, user_id),
        )
        conn.execute(
            "INSERT INTO team_match_history (guild_id, team_name_key, result, match_no) "
            "VALUES (?, ?, ?, ?)",
            (guild_id, storage_key, result, match_no),
        )

    @staticmethod
    def _unbump(conn, guild_id, storage_key, result, match_no=None):
        """Reverse one W or L (used when a synced result changes)."""
        column = "matches_won" if result == "W" else "matches_lost"
        conn.execute(
            f"UPDATE team_points SET {column}=MAX({column}-1, 0) "
            "WHERE guild_id=? AND team_name_key=?",
            (guild_id, storage_key),
        )
        # Remove this match's own history row; older rows saved before match
        # numbers existed fall back to the team's latest row of that result.
        removed = conn.execute(
            """
            DELETE FROM team_match_history WHERE id=(
                SELECT id FROM team_match_history
                WHERE guild_id=? AND team_name_key=? AND result=? AND match_no=?
                ORDER BY id DESC LIMIT 1)
            """,
            (guild_id, storage_key, result, match_no),
        ).rowcount
        if not removed:
            conn.execute(
                """
                DELETE FROM team_match_history WHERE id=(
                    SELECT id FROM team_match_history
                    WHERE guild_id=? AND team_name_key=? AND result=?
                    ORDER BY id DESC LIMIT 1)
                """,
                (guild_id, storage_key, result),
            )

    def _record_match(self, guild_id, user_id, match_no, winner, loser, resync, guard=False):
        """Returns 'new', 'corrected', 'unchanged', 'duplicate' or 'conflict'.

        guard=True (bulk sync): if this match number is already stored for two
        completely different teams, it's probably a different match that reuses
        the number, so don't overwrite it - report 'conflict' instead.
        """
        conn = connect_points_db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            w_key = points_storage_key(conn, guild_id, winner["team_name_key"])
            l_key = points_storage_key(conn, guild_id, loser["team_name_key"])
            old = conn.execute(
                "SELECT winner_team_key, loser_team_key FROM imported_match_results "
                "WHERE guild_id=? AND match_no=?",
                (guild_id, match_no),
            ).fetchone()

            status = "new"
            if old:
                if not resync:
                    conn.rollback()
                    return "duplicate"
                if old["winner_team_key"] == w_key and old["loser_team_key"] == l_key:
                    conn.rollback()
                    return "unchanged"
                if guard and not ({old["winner_team_key"], old["loser_team_key"]} & {w_key, l_key}):
                    conn.rollback()
                    return "conflict"
                self._unbump(conn, guild_id, old["winner_team_key"], "W", match_no)
                self._unbump(conn, guild_id, old["loser_team_key"], "L", match_no)
                status = "corrected"

            self._bump(conn, guild_id, w_key, "W", user_id, match_no)
            self._bump(conn, guild_id, l_key, "L", user_id, match_no)
            conn.execute(
                "INSERT OR REPLACE INTO imported_match_results "
                "(guild_id, match_no, winner_team_key, loser_team_key, imported_by) "
                "VALUES (?, ?, ?, ?, ?)",
                (guild_id, match_no, w_key, l_key, user_id),
            )
            conn.commit()
            return status
        except sqlite3.Error:
            conn.rollback()
            raise
        finally:
            conn.close()

    async def _get_replied_message(self, ctx):
        reference = ctx.message.reference
        if reference is None or reference.message_id is None:
            return None
        replied = reference.resolved
        if isinstance(replied, discord.Message):
            return replied
        try:
            channel_id = getattr(reference, "channel_id", None) or ctx.channel.id
            channel = ctx.guild.get_channel(channel_id) or await self.bot.fetch_channel(channel_id)
            return await channel.fetch_message(reference.message_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException, AttributeError):
            return None

    async def _find_latest_result_message(self, ctx):
        """Fallback for +syncresult without a reply: newest parseable result post."""
        async for msg in ctx.channel.history(limit=50, before=ctx.message):
            parsed, _ = self._parse_result(self._message_text(msg))
            if parsed:
                return msg
        return None

    def _process_result_text(self, guild_id, user_id, text, resync, guard=False):
        """Parse one message and apply it. No Discord replies here.

        Returns (status, match_no, winner_name, loser_name, detail) where status
        is new / corrected / unchanged / duplicate / skip (not a result post) /
        error (result post that couldn't be applied; detail says why).
        """
        parsed, error = self._parse_result(text)
        if parsed is None:
            return "skip", None, None, None, error
        match_no, team_a, team_b, winner_name = parsed
        loser_name = team_b if norm(winner_name) == norm(team_a) else team_a

        winner = self._find_team(guild_id, winner_name)
        loser = self._find_team(guild_id, loser_name)
        if winner is None or loser is None:
            missing = winner_name if winner is None else loser_name
            stage = get_active_stage(guild_id)
            where = f" in **{stage[1]}**" if stage else ""
            return ("error", match_no, winner_name, loser_name,
                    f"team **{missing}** isn't assigned with `+setteamgroups`{where} "
                    "(another stage? `+pointsstage <stage name>`)")
        if winner["team_name_key"] == loser["team_name_key"]:
            return "error", match_no, winner_name, loser_name, "both teams resolved to the same team"

        status = self._record_match(guild_id, user_id, match_no, winner, loser, resync, guard)
        detail = None
        if status == "conflict":
            old_w, old_l = self._stored_match_teams(guild_id, match_no)
            detail = (f"number already used by **{old_w}** vs **{old_l}** "
                      f"(this post is {winner['team_name']} vs {loser['team_name']})")
        return status, match_no, winner["team_name"], loser["team_name"], detail

    def _stored_match_teams(self, guild_id, match_no):
        """Display names of the teams stored for an imported match number."""
        conn = connect_points_db()
        try:
            row = conn.execute(
                "SELECT winner_team_key, loser_team_key FROM imported_match_results "
                "WHERE guild_id=? AND match_no=?",
                (guild_id, match_no),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return "?", "?"

        def display(key):
            found = self._find_team(guild_id, key.split("::", 1)[-1])
            return found["team_name"] if found else key
        return display(row["winner_team_key"]), display(row["loser_team_key"])

    @staticmethod
    def _looks_like_result(text):
        return bool(re.search(r"(?<![A-Za-z])Match\s+\d+", text, re.IGNORECASE)
                    and re.search(r"\bvs\b|\bwon\b", text, re.IGNORECASE))

    async def _import_result_message(self, ctx, replied, resync):
        """Single-message import (used by +addresult and +syncresult fallback)."""
        if replied.guild is None or replied.guild.id != ctx.guild.id:
            await ctx.reply("The result message must be from this server.", mention_author=False)
            return

        status, match_no, winner, loser, detail = self._process_result_text(
            ctx.guild.id, ctx.author.id, self._message_text(replied), resync
        )

        if status == "skip":
            await ctx.reply(
                f"{detail}\nReply to the actual match-result post.", mention_author=False
            )
            return
        if status == "error":
            await ctx.reply(f"Match **{match_no}**: {detail}.", mention_author=False)
            return
        if status == "duplicate":
            await ctx.reply(
                f"Match **{match_no}** is already in the points table. "
                "Use `+syncresult` (reply to it) if the result needs correcting.",
                mention_author=False,
            )
            return
        if status == "unchanged":
            await ctx.reply(
                f"Match **{match_no}** is already synced - nothing to change.",
                mention_author=False,
            )
            return

        await self._sync_points_images(ctx.guild.id)

        verb = "Corrected" if status == "corrected" else "Imported"
        await ctx.reply(
            f"✅ {verb} **Match {match_no}**.\n"
            f"🏆 **{winner}**: +1 Win, +{POINTS_PER_WIN} Points\n"
            f"**{loser}**: +1 Loss, +0 Points\n"
            "PL/W/L/PTS and recent form are updated.",
            mention_author=False,
        )

    async def _sync_from_message_onwards(self, ctx, first):
        """Sync `first` and every message after it, up to the latest in the channel."""
        if first.guild is None or first.guild.id != ctx.guild.id:
            await ctx.reply("The message must be from this server.", mention_author=False)
            return

        messages = [first]
        truncated = False
        async with ctx.typing():
            async for msg in first.channel.history(
                limit=SYNC_SCAN_LIMIT + 1, after=first, oldest_first=True
            ):
                if msg.id == ctx.message.id:
                    continue
                messages.append(msg)
            if len(messages) > SYNC_SCAN_LIMIT:
                messages = messages[:SYNC_SCAN_LIMIT]
                truncated = True

            counts = {"new": 0, "corrected": 0, "unchanged": 0, "duplicate": 0,
                      "error": 0, "conflict": 0}
            problems = []
            unreadable = []
            for msg in messages:
                text = self._message_text(msg)
                status, match_no, _w, _l, detail = self._process_result_text(
                    ctx.guild.id, ctx.author.id, text, True, guard=True
                )
                if status == "skip":
                    if self._looks_like_result(text):
                        unreadable.append(f"[this post]({msg.jump_url}) - {detail}")
                    continue
                counts[status] += 1
                if status in ("error", "conflict"):
                    problems.append(f"Match **{match_no}** ([post]({msg.jump_url})): {detail}")

            changed = counts["new"] + counts["corrected"]
            if changed:
                await self._sync_points_images(ctx.guild.id)

        found = sum(counts.values())
        if found == 0 and not unreadable:
            await ctx.reply(
                "I didn't find any match-result posts from that message onwards.",
                mention_author=False,
            )
            return

        lines = [f"✅ Synced from that message to the latest ({len(messages)} messages checked)."]
        lines.append(f"New: **{counts['new']}** | Corrected: **{counts['corrected']}** | "
                     f"Already synced: **{counts['unchanged']}**")
        if problems:
            lines.append(f"⚠️ **{len(problems)}** not imported:")
            lines.extend(f"• {p}" for p in problems[:6])
            if len(problems) > 6:
                lines.append(f"• ...and {len(problems) - 6} more")
        if unreadable:
            lines.append(f"❓ **{len(unreadable)}** post(s) look like results but I couldn't read them:")
            lines.extend(f"• {u}" for u in unreadable[:6])
            if len(unreadable) > 6:
                lines.append(f"• ...and {len(unreadable) - 6} more")
        if truncated:
            lines.append(f"Only the first {SYNC_SCAN_LIMIT} messages were scanned - "
                         "run it again from the last synced result to continue.")
        await ctx.reply("\n".join(lines)[:1900], mention_author=False)

    async def _addresult_from_reply(self, ctx):
        replied = await self._get_replied_message(ctx)
        if replied is None:
            await ctx.reply(
                "Reply to a match result message, then run `+addresult` "
                "(or use `+addresult <team> <W/L>`).",
                mention_author=False,
            )
            return
        await self._import_result_message(ctx, replied, resync=False)

    @commands.command(name="syncresult", aliases=["syncpt"])
    @commands.guild_only()
    async def sync_result(self, ctx):
        """Reply to a result post: sync it AND every result posted after it.

        Reads winner/loser from each result post from the replied message up to
        the latest message in the channel and updates the points table.
        Safe to run again - already-synced matches aren't counted twice, and a
        changed winner is corrected. Without a reply it syncs the latest post.
        """
        if not await self._allowed(ctx):
            return

        replied = await self._get_replied_message(ctx)
        if replied is not None:
            await self._sync_from_message_onwards(ctx, replied)
            return

        latest = await self._find_latest_result_message(ctx)
        if latest is None:
            await ctx.reply(
                "Reply to the first result post you want to sync from and run `+syncresult`.",
                mention_author=False,
            )
            return
        await self._import_result_message(ctx, latest, resync=True)

    @commands.command(name="editresult", aliases=["fixresult"])
    @commands.guild_only()
    async def edit_result(self, ctx, *, arguments: str = ""):
        """Fix a wrong result post in place: +editresult <winner> [by <margin>].

        Reply to the bot's result post. It edits that message to the new winner
        (keeping the scorecard link) and corrects the points table to match.
        Example: +editresult REDBLADES by 5 wickets
        """
        if not await self._allowed(ctx):
            return

        usage = ("Reply to the bot's result post, then run "
                 "`+editresult <winner>` or `+editresult <winner> by <margin>`.\n"
                 "Example: `+editresult REDBLADES by 5 wickets`")
        replied = await self._get_replied_message(ctx)
        if replied is None or not arguments.strip():
            await ctx.reply(usage, mention_author=False)
            return
        if replied.guild is None or replied.guild.id != ctx.guild.id:
            await ctx.reply("The result message must be from this server.", mention_author=False)
            return
        if replied.author.id != self.bot.user.id:
            await ctx.reply(
                "I can only edit my own messages. For someone else's post, ask them to "
                "edit it, then reply to it with `+syncresult`.",
                mention_author=False,
            )
            return

        content = replied.content or ""
        parsed, error = self._parse_result(content)
        winner_match = WINNER_LINE_RE.search(content)
        if parsed is None or winner_match is None:
            await ctx.reply(
                f"{error or 'No result line found.'}\nI can only edit result posts that are plain message text.",
                mention_author=False,
            )
            return
        match_no, team_a, team_b, _old_winner = parsed

        split = re.split(r"\s+by\s+", arguments.strip(), maxsplit=1, flags=re.IGNORECASE)
        wanted = norm(split[0])
        margin = split[1].strip().strip("*_ ") if len(split) == 2 else ""
        if len(margin) > 40:
            await ctx.reply("That margin is too long.", mention_author=False)
            return

        candidates = [t for t in (team_a, team_b) if norm(t) == wanted]
        if not candidates:
            candidates = [t for t in (team_a, team_b) if wanted and wanted in norm(t)]
        if len(candidates) != 1:
            await ctx.reply(
                f"The winner must be **{team_a}** or **{team_b}**.", mention_author=False
            )
            return
        new_winner = candidates[0]

        # Rewrite only the team name (and margin) on the winner line.
        line_end = content.find("\n", winner_match.end())
        line_end = len(content) if line_end == -1 else line_end
        line_start = content.rfind("\n", 0, winner_match.start(1)) + 1
        line = content[line_start:line_end]
        name_start = winner_match.start(1) - line_start
        name_end = winner_match.end(1) - line_start
        line = line[:name_start] + new_winner + line[name_end:]
        if margin:
            won_at = re.search(r"\bwon\b", line[name_start + len(new_winner):])
            head = line[: name_start + len(new_winner) + won_at.start()] + "won by "
            if line.rstrip().endswith("***"):
                tail = f"{margin}***"
            elif "**" in line[name_start + len(new_winner) + won_at.start():]:
                tail = f"**{margin}**"
            else:
                tail = margin
            line = head + tail
        new_content = content[:line_start] + line + content[line_end:]

        # Update the points table first; abort (without editing) if it can't apply.
        status, _m, winner_name, loser_name, detail = self._process_result_text(
            ctx.guild.id, ctx.author.id, new_content, True
        )
        if status in ("error", "skip"):
            await ctx.reply(
                f"Nothing changed - match **{match_no}**: {detail}.", mention_author=False
            )
            return

        edit_ok = True
        if new_content != content:
            try:
                await replied.edit(content=new_content)
            except discord.HTTPException:
                edit_ok = False

        if status in ("new", "corrected"):
            await self._sync_points_images(ctx.guild.id)

        table_note = {
            "corrected": "Points table corrected (old result reversed).",
            "new": "Points table updated.",
            "unchanged": "Points table already matched this winner.",
        }.get(status, "Points table updated.")
        message_note = ("Result post edited." if edit_ok
                        else "⚠️ I couldn't edit the message itself (missing permission?).")
        await ctx.reply(
            f"✅ **Match {match_no}**: 🏆 **{winner_name}** beat **{loser_name}**.\n"
            f"{message_note} {table_note}",
            mention_author=False,
        )

    @commands.command(name="editteams", aliases=["fixteams"])
    @commands.guild_only()
    async def edit_teams(self, ctx, *, arguments: str = ""):
        """Fix wrong team names in a result post: +editteams <Team A> vs <Team B>.

        Reply to the bot's result post. It rewrites the team names in the post
        (the winner keeps their position: A stays the winner if A was) and moves
        the W/L from the wrong teams to the right ones in the points table.
        To also change the winner add: won <team>.
        Example: +editteams REDBLADES vs Delhi Dogs won Delhi Dogs
        """
        if not await self._allowed(ctx):
            return

        usage = ("Reply to the bot's result post, then run\n"
                 "`+editteams <Team A> vs <Team B>`\n"
                 "Add `won <team>` if the winner changes too.\n"
                 "Example: `+editteams REDBLADES vs Delhi Dogs won Delhi Dogs`")
        replied = await self._get_replied_message(ctx)
        if replied is None or not arguments.strip():
            await ctx.reply(usage, mention_author=False)
            return
        if replied.guild is None or replied.guild.id != ctx.guild.id:
            await ctx.reply("The result message must be from this server.", mention_author=False)
            return
        if replied.author.id != self.bot.user.id:
            await ctx.reply(
                "I can only edit my own messages. For someone else's post, ask them to "
                "edit it, then reply to it with `+syncresult`.",
                mention_author=False,
            )
            return

        content = replied.content or ""
        parsed, error = self._parse_result(content)
        header = HEADER_RE.search(content)
        winner_match = WINNER_LINE_RE.search(content)
        if parsed is None or header is None or winner_match is None:
            await ctx.reply(
                f"{error or 'No result found.'}\nI can only edit result posts that are plain message text.",
                mention_author=False,
            )
            return
        match_no, old_a, old_b, old_winner = parsed

        # Parse "<A> vs <B> [won <W>]"
        winner_arg = None
        parts = re.split(r"\s+won\s+", arguments.strip(), maxsplit=1, flags=re.IGNORECASE)
        if len(parts) == 2:
            winner_arg = parts[1].strip()
        names = re.split(r"\s+vs\.?\s+", parts[0].strip(), maxsplit=1, flags=re.IGNORECASE)
        if len(names) != 2 or not names[0].strip() or not names[1].strip():
            await ctx.reply(usage, mention_author=False)
            return

        team_a = self._find_team(ctx.guild.id, names[0].strip())
        team_b = self._find_team(ctx.guild.id, names[1].strip())
        for typed, found in ((names[0], team_a), (names[1], team_b)):
            if found is None:
                await ctx.reply(
                    f"I couldn't find **{typed.strip()}** in `+setteamgroups`. "
                    "Use the exact team name.",
                    mention_author=False,
                )
                return
        if team_a["team_name_key"] == team_b["team_name_key"]:
            await ctx.reply("Team A and Team B can't be the same team.", mention_author=False)
            return

        # Who wins: keep the old winner's position unless a winner was given.
        if winner_arg:
            if norm(winner_arg) == norm(team_a["team_name"]):
                new_winner = team_a["team_name"]
            elif norm(winner_arg) == norm(team_b["team_name"]):
                new_winner = team_b["team_name"]
            else:
                await ctx.reply(
                    f"The winner must be **{team_a['team_name']}** or **{team_b['team_name']}**.",
                    mention_author=False,
                )
                return
        else:
            new_winner = (team_a if norm(old_winner) == norm(old_a) else team_b)["team_name"]

        def inner_span(group):
            """Span of the group's text without the markdown (*, _, ~) around it."""
            start, end = header.start(group), header.end(group)
            while start < end and content[start] in "*_~ \t":
                start += 1
            while end > start and content[end - 1] in "*_~ \t":
                end -= 1
            return start, end

        a_start, a_end = inner_span(2)
        b_start, b_end = inner_span(3)
        edits = [
            (winner_match.start(1), winner_match.end(1), new_winner),
            (b_start, b_end, team_b["team_name"]),
            (a_start, a_end, team_a["team_name"]),
        ]
        new_content = content
        for start, end, text in sorted(edits, reverse=True):
            new_content = new_content[:start] + text + new_content[end:]

        # Update the points table first; abort (without editing) if it can't apply.
        status, _m, winner_name, loser_name, detail = self._process_result_text(
            ctx.guild.id, ctx.author.id, new_content, True
        )
        if status in ("error", "skip"):
            await ctx.reply(
                f"Nothing changed - match **{match_no}**: {detail}.", mention_author=False
            )
            return

        edit_ok = True
        if new_content != content:
            try:
                await replied.edit(content=new_content)
            except discord.HTTPException:
                edit_ok = False

        if status in ("new", "corrected"):
            await self._sync_points_images(ctx.guild.id)

        table_note = {
            "corrected": "Points table corrected - the wrong teams' result was reversed.",
            "new": "Points table updated.",
            "unchanged": "Points table already matched.",
        }.get(status, "Points table updated.")
        message_note = ("Result post edited." if edit_ok
                        else "⚠️ I couldn't edit the message itself (missing permission?).")
        await ctx.reply(
            f"✅ **Match {match_no}**: **{team_a['team_name']}** vs **{team_b['team_name']}**\n"
            f"🏆 **{winner_name}** won, **{loser_name}** lost.\n"
            f"{message_note} {table_note}",
            mention_author=False,
        )

    @commands.command(name="missingresults", aliases=["missing"])
    @commands.guild_only()
    async def missing_results(self, ctx, total: int = 0):
        """List match numbers not in the table: +missingresults [total matches].

        Run it in the results channel. It also searches the channel for posts
        about each missing match so you can see why it wasn't imported.
        """
        if not await self._allowed(ctx):
            return

        conn = connect_points_db()
        try:
            rows = conn.execute(
                "SELECT match_no FROM imported_match_results WHERE guild_id=?",
                (ctx.guild.id,),
            ).fetchall()
        finally:
            conn.close()

        numbers = {int(r["match_no"]) for r in rows if str(r["match_no"]).isdigit()}
        labelled = len(rows) - len(numbers)
        if not numbers:
            await ctx.reply("No numbered matches are in the points table yet.", mention_author=False)
            return

        last = total if total > 0 else max(numbers)
        missing = [n for n in range(1, last + 1) if n not in numbers]
        beyond = sorted(n for n in numbers if n > last)

        def ranges(values):
            out, start, prev = [], None, None
            for v in values:
                if start is None:
                    start = prev = v
                elif v == prev + 1:
                    prev = v
                else:
                    out.append(f"{start}-{prev}" if start != prev else str(start))
                    start = prev = v
            if start is not None:
                out.append(f"{start}-{prev}" if start != prev else str(start))
            return ", ".join(out)

        lines = [f"📋 Imported: **{len(rows)}** matches (numbers 1-{last} covered: "
                 f"**{last - len(missing)}** of {last})."]
        if labelled:
            lines.append(f"{labelled} imported with non-numeric labels (Final, SF1...).")
        if beyond:
            lines.append(f"Numbers above {last}: {ranges(beyond)}")
        if not missing:
            lines.append("✅ No numbers missing in that range.")
            await ctx.reply("\n".join(lines), mention_author=False)
            return
        lines.append(f"❌ Missing: **{ranges(missing)}**")

        # Look for posts about the missing numbers in this channel.
        wanted = set(missing)
        found = {}
        async with ctx.typing():
            async for msg in ctx.channel.history(limit=2000, before=ctx.message):
                text = self._message_text(msg)
                hit = re.search(r"(?<![A-Za-z])Match\s+(\d+)", text, re.IGNORECASE)
                if hit and int(hit.group(1)) in wanted and int(hit.group(1)) not in found:
                    found[int(hit.group(1))] = msg
        shown = missing[:10]
        lines.append("")
        for n in shown:
            msg = found.get(n)
            if msg is None:
                lines.append(f"• Match **{n}**: no post found in the last 2000 messages here")
                continue
            parsed, error = self._parse_result(self._message_text(msg))
            why = error if parsed is None else "readable - run `+syncresult` from it"
            lines.append(f"• Match **{n}**: [post found]({msg.jump_url}) - {why}")
        if len(missing) > len(shown):
            lines.append(f"• ...and {len(missing) - len(shown)} more")
        await ctx.reply("\n".join(lines)[:1900], mention_author=False)

    def _stage_storage_keys(self, guild_id, stage_key, conn):
        """Storage keys of every team assigned in one stage."""
        prefix = stage_key + "::"
        gconn = sqlite3.connect(GROUP_DB)
        try:
            rows = gconn.execute(
                "SELECT team_name_key FROM team_groups WHERE guild_id=? AND substr(group_key, 1, ?) = ?",
                (guild_id, len(prefix), prefix)).fetchall()
        finally:
            gconn.close()
        legacy_ok = stage_key == first_stage_key(guild_id)
        return {points_storage_key(conn, guild_id, r[0], legacy_ok) for r in rows}

    @commands.command(name="resetpt", aliases=["resetpoints"])
    @commands.guild_only()
    async def reset_points(self, ctx, *, scope: str = None):
        """Reset points: +resetpt (the current points stage), +resetpt <stage name>, +resetpt all."""
        if not await self._allowed(ctx):
            return

        text = (scope or "").strip()
        if text.casefold() == "all":
            stage = None
        elif text:
            stage = find_stage(ctx.guild.id, text)
            if stage is None:
                names = ", ".join(n for _, n in _stage_list(ctx.guild.id)) or "none"
                await ctx.reply(f"I couldn't find a stage called **{text}**. Stages: {names}", mention_author=False)
                return
        else:
            stage = get_active_stage(ctx.guild.id)

        if stage is None:
            warning = ("⚠️ This will reset **every team's** W/L/points and form, and forget all "
                       "imported matches for this server. This can't be undone.")
        else:
            warning = (f"⚠️ This will reset the W/L/points and form of every team in **{stage[1]}** and forget "
                       f"its imported matches. Other stages are not touched. This can't be undone.")
        view = ConfirmResetView(ctx.author.id)
        view.message = await ctx.reply(warning, view=view, mention_author=False)
        await view.wait()

        if view.confirmed is not True:
            text = "Reset cancelled." if view.confirmed is False else "Reset timed out - nothing changed."
            try:
                await view.message.edit(content=text, view=None)
            except discord.HTTPException:
                pass
            return

        conn = connect_points_db()
        try:
            if stage is None:
                for table in ("team_points", "team_match_history", "imported_match_results"):
                    conn.execute(f"DELETE FROM {table} WHERE guild_id=?", (ctx.guild.id,))
            else:
                for key in self._stage_storage_keys(ctx.guild.id, stage[0], conn):
                    conn.execute("DELETE FROM team_points WHERE guild_id=? AND team_name_key=?",
                                 (ctx.guild.id, key))
                    conn.execute("DELETE FROM team_match_history WHERE guild_id=? AND team_name_key=?",
                                 (ctx.guild.id, key))
                    conn.execute("DELETE FROM imported_match_results WHERE guild_id=? "
                                 "AND (winner_team_key=? OR loser_team_key=?)", (ctx.guild.id, key, key))
            conn.commit()
        finally:
            conn.close()

        if CACHE_DIR.exists():
            if stage is None:
                old_files = list(CACHE_DIR.glob(f"points_{ctx.guild.id}_*.png"))
            else:
                old_files = [cache_file(ctx.guild.id, g["group_key"])
                             for g in self._configured_groups(ctx.guild.id, stage[0])]
            for old in old_files:
                try:
                    old.unlink()
                except OSError:
                    pass
        await self._sync_points_images(ctx.guild.id)

        done = "✅ Points table reset" + (f" for **{stage[1]}**" if stage else "") + ". Everyone is back to 0."
        try:
            await view.message.edit(content=done, view=None)
        except discord.HTTPException:
            await ctx.send(done)

    @commands.command(name="addresult")
    @commands.guild_only()
    async def add_result(self, ctx, *, arguments: str = ""):
        """Record one match result: reply to a result post, or +addresult <team> <W/L>."""
        if not await self._allowed(ctx):
            return

        if not arguments.strip():
            await self._addresult_from_reply(ctx)
            return

        parts = arguments.rsplit(maxsplit=1)
        result = parts[1].strip().upper() if len(parts) == 2 else ""
        if result not in ("W", "L"):
            await ctx.reply(
                "Usage: `+addresult <team name> <W/L>` or reply to a result post "
                "with `+addresult`.\nExample: `+addresult Kashmir Titans W`",
                mention_author=False,
            )
            return

        team = self._find_team(ctx.guild.id, parts[0])
        if team is None:
            stage = get_active_stage(ctx.guild.id)
            where = f" **{stage[1]}**" if stage else ""
            await ctx.reply(f"I couldn't find that team in{where} (`+setteamgroups`). "
                            "Another stage? Use `+pointsstage <stage name>`.", mention_author=False)
            return

        conn = connect_points_db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            storage_key = points_storage_key(conn, ctx.guild.id, team["team_name_key"])
            self._bump(conn, ctx.guild.id, storage_key, result, ctx.author.id)
            row = conn.execute(
                "SELECT matches_won, matches_lost FROM team_points "
                "WHERE guild_id=? AND team_name_key=?",
                (ctx.guild.id, storage_key),
            ).fetchone()
            conn.commit()
        except sqlite3.Error:
            conn.rollback()
            raise
        finally:
            conn.close()

        wins, losses = int(row["matches_won"]), int(row["matches_lost"])
        await self._sync_points_images(ctx.guild.id)

        await ctx.reply(
            f"✅ Recorded **{result}** for **{team['team_name']}**.\n"
            f"PL: **{wins + losses}** | W: **{wins}** | "
            f"L: **{losses}** | PTS: **{wins * POINTS_PER_WIN}**",
            mention_author=False,
        )

    @commands.command(name="pointsstage", aliases=["setpointsstage", "ptstage"])
    @commands.guild_only()
    async def points_stage(self, ctx, *, stage_name: str = None):
        """Show or choose the stage that +setpt / +addresult / +syncresult record into (and +points shows)."""
        stages = _stage_list(ctx.guild.id)
        if not stages:
            await ctx.reply("No stages exist yet. Create one with `+createnewstage <name>`.", mention_author=False)
            return
        active = get_active_stage(ctx.guild.id)
        if not stage_name or not stage_name.strip():
            listing = " · ".join(f"**{n}**" if k == active[0] else n for k, n in stages)
            await ctx.reply(f"Points stage: **{active[1]}**\nStages: {listing}\n"
                            "Change it with `+pointsstage <stage name>`. "
                            "(`+points <stage name>` shows any stage without changing it.)",
                            mention_author=False)
            return
        if not await self._allowed(ctx):
            return
        stage = find_stage(ctx.guild.id, stage_name)
        if stage is None:
            await ctx.reply(f"I couldn't find a stage called **{stage_name.strip()}**. "
                            f"Stages: {', '.join(n for _, n in stages)}", mention_author=False)
            return
        set_active_stage(ctx.guild.id, stage[0])
        await ctx.reply(f"✅ Points stage is now **{stage[1]}**. `+points`, `+setpt`, `+addresult` and "
                        "`+syncresult` use it.", mention_author=False)

    @commands.command(name="points")
    @commands.guild_only()
    async def points(self, ctx, *, stage_name: str = None):
        """Show a stage's points table: +points [stage name]. One page per group (A, B, C)."""
        stages = _stage_list(ctx.guild.id)
        if stage_name and stage_name.strip():
            stage = find_stage(ctx.guild.id, stage_name)
            if stage is None:
                names = ", ".join(n for _, n in stages) or "none"
                await ctx.reply(f"I couldn't find a stage called **{stage_name.strip()}**. Stages: {names}",
                                mention_author=False)
                return
        else:
            stage = get_active_stage(ctx.guild.id)
        if stage is None:
            await ctx.reply("No stages or groups are configured. Run `+groupstyle` and "
                            "`+setteamgroups` first.", mention_author=False)
            return
        stage_key, stage_label = stage
        configured = self._configured_groups(ctx.guild.id, stage_key)

        if not configured:
            await ctx.reply(
                f"No groups are configured for **{stage_label}**. Run `+groupstyle {stage_label}` and "
                f"`+setteamgroups {stage_label}` first.",
                mention_author=False,
            )
            return

        pages = []
        try:
            # Images are re-drawn only when something changed: results (+addresult / +setpt ...) or the
            # groups / teams (+setteamgroups etc. touch the groups database, so a newer database = redraw).
            groups_changed = Path(GROUP_DB).stat().st_mtime
            redrawn = False
            for group in configured:
                path = cache_file(ctx.guild.id, group["group_key"])
                if not redrawn and (not path.exists() or path.stat().st_mtime < groups_changed):
                    await self._sync_points_images(ctx.guild.id)
                    redrawn = True
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
                f"No group in **{stage_label}** has assigned teams yet. Run `+setteamgroups {stage_label}` first.",
                mention_author=False,
            )
            return

        if len(pages) == 1:
            name, data = pages[0]
            await ctx.send(content=f"**{stage_label}**",
                           file=discord.File(io.BytesIO(data), filename="points_table.png"))
            return

        view = PointsPagesView(ctx.author.id, pages, stage_label)
        view.message = await ctx.send(
            content=view.content(), file=view._file(), view=view
        )


async def setup(bot):
    await bot.add_cog(PointsTable(bot))
