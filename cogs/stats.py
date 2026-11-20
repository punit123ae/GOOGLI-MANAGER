import asyncio
import io
import re
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from functools import lru_cache
from types import SimpleNamespace

import discord
from discord.ext import commands

from .utility import owner_or_whitelisted
from PIL import Image, ImageDraw, ImageFont, ImageOps


def parse_raw_stats(content):
    """Parse only the first Python code block after the raw-statistics heading."""
    heading = re.search(r"Raw statistics of the match\s*:", content, re.IGNORECASE)
    if heading:
        content = content[heading.end():]

    # Match the first Python-fenced block only. Any later code blocks or text
    # (for example, extra player rows pasted after the main block) are ignored.
    code_block = re.search(r"```python\s*\n(.*?)```", content, re.IGNORECASE | re.DOTALL)
    if code_block:
        content = code_block.group(1)
    else:
        # Also support messages where the heading is followed by plain rows,
        # but stop at the next fenced block so trailing stats aren't imported.
        content = re.split(r"```", content, maxsplit=1)[0]

    rows = []
    for line in content.splitlines():
        line = line.strip().strip("`").strip()
        parts = [part.strip() for part in line.split(",")]
        if len(parts) != 7 or not all(re.fullmatch(r"\d+", part) for part in parts):
            continue
        user_id, runs, balls_faced, runs_conceded, balls_bowled, wickets, not_out = map(int, parts)
        if not_out not in (0, 1):
            raise ValueError(f"Invalid final flag for user {user_id}; expected 0 or 1.")
        rows.append((user_id, runs, balls_faced, runs_conceded, balls_bowled, wickets, not_out))
    if not rows:
        raise ValueError("No valid player-stat rows found in the Python block after 'Raw statistics of the match:'.")
    if len({row[0] for row in rows}) != len(rows):
        raise ValueError("Duplicate player IDs found in the raw statistics.")
    return rows


def fmt(value, digits=2):
    if isinstance(value, float):
        return f"{value:.{digits}f}".rstrip("0").rstrip(".") or "0"
    return str(value)


@lru_cache(maxsize=64)
def load_font(size, bold=False):
    candidates = [
        "C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            pass
    return ImageFont.load_default()


def totals(rows):
    runs = sum(r["runs"] for r in rows)
    faced = sum(r["balls_faced"] for r in rows)
    conceded = sum(r["runs_conceded"] for r in rows)
    bowled = sum(r["balls_bowled"] for r in rows)
    wickets = sum(r["wickets"] for r in rows)
    dismissals = sum(1 for r in rows if not r["not_out"])
    return {
        "matches": len(rows),
        "runs": runs,
        "balls_faced": faced,
        "runs_conceded": conceded,
        "balls_bowled": bowled,
        "wickets": wickets,
        "not_outs": sum(r["not_out"] for r in rows),
        "strike_rate": runs / faced * 100 if faced else 0,
        "bat_avg": runs / dismissals if dismissals else runs,
        "overs": bowled / 6,
        "economy": conceded * 6 / bowled if bowled else 0,
        "bowl_avg": conceded / wickets if wickets else 0,
        "bowl_sr": bowled / wickets if wickets else 0,
    }


def classify_player(t, rows=None):
    """Classify using the player's batting and bowling fantasy-point contributions."""
    if rows is not None:
        batting_points, bowling_points, _ = fantasy_raw_points(rows, t)
        # A genuine all-rounder must contribute positively in both disciplines,
        # with the weaker contribution reaching at least half of the stronger one.
        if batting_points > 0 and bowling_points > 0:
            stronger = max(batting_points, bowling_points)
            weaker = min(batting_points, bowling_points)
            if weaker >= stronger * 0.5:
                return "ALL-ROUNDER"
        return "BATTER" if batting_points >= bowling_points else "BOWLER"

    # Keep the original fallback for callers that only have season totals.
    bat = (t["runs"] >= 50 or t["balls_faced"] >= 20) and (
        t["runs"] / max(t["balls_faced"], 1) >= 0.75
    )
    bowl = t["wickets"] >= 2 or (
        t["balls_bowled"] >= 12 and t["economy"] <= 10
    )
    if bat and bowl:
        return "ALL-ROUNDER"
    if bowl and not bat:
        return "BOWLER"
    if bat:
        return "BATTER"
    if t["wickets"] > 0 and t["runs"] == 0:
        return "BOWLER"
    return "BATTER" if t["runs"] >= t["wickets"] * 10 else "BOWLER"




def fantasy_raw_points(rows, t=None):
    """Return batting raw points, bowling raw points and total fantasy points."""
    batting_raw = 0.0
    bowling_raw = 0.0

    # Apply batting points and milestone bonuses separately for each innings.
    for row in rows:
        runs = int(row.get("runs", 0))
        balls_faced = int(row.get("balls_faced", 0))
        not_out = bool(row.get("not_out", 0))

        # A row with no runs and no balls faced represents no batting contribution.
        batted = runs > 0 or balls_faced > 0
        if batted:
            batting_raw += runs - (balls_faced * 0.1)

            # Only the highest batting milestone bonus applies per innings.
            if runs >= 100:
                batting_raw += 30
            elif runs >= 50:
                batting_raw += 15

            # A duck penalty applies only when dismissed on zero runs.
            if runs == 0 and not not_out:
                batting_raw -= 15

    # The stricter run penalty depends on the bowler's overall totals
    # across all rows/innings being scored, not on each row independently.
    total_balls_bowled = sum(int(row.get("balls_bowled", 0)) for row in rows)
    total_wickets = sum(int(row.get("wickets", 0)) for row in rows)

    if total_balls_bowled > 0:
        run_penalty = 1.0 if total_wickets == 0 and total_balls_bowled > 5 else 0.5

        for row in rows:
            balls_bowled = int(row.get("balls_bowled", 0))
            if balls_bowled <= 0:
                continue

            wickets = int(row.get("wickets", 0))
            runs_conceded = int(row.get("runs_conceded", 0))
            bowling_raw += (
                (wickets * 18)
                - (runs_conceded * run_penalty)
                - (balls_bowled * 0.1)
            )

            # Only the highest wicket milestone bonus applies per spell.
            if wickets >= 9:
                bowling_raw += 30
            elif wickets >= 7:
                bowling_raw += 20
            elif wickets >= 5:
                bowling_raw += 15
            elif wickets >= 3:
                bowling_raw += 6

    total_raw = batting_raw + bowling_raw
    return batting_raw, bowling_raw, total_raw


def impact_scores(rows, t=None):
    """Return batting and bowling ratings, each bounded from 0 to 99."""
    batting_raw, bowling_raw, _ = fantasy_raw_points(rows, t)

    bat_score = max(0, min(99, round(batting_raw)))
    bowl_score = max(0, min(99, round(bowling_raw)))

    return bat_score, bowl_score


def overall_rating(bat_score, bowl_score):
    """Calculate overall rating using the batting and bowling ratings."""
    if bat_score >= 75 and bowl_score >= 75:
        rating = (bat_score + bowl_score) / 2
    else:
        rating = max(bat_score, bowl_score)

    return max(0, min(99, round(rating)))




# ============================================================
# DESIGN 1 - ORIGINAL RENDERER
# Keep this renderer unchanged to preserve the existing design.
# ============================================================

def make_stats_image(member, rows, guild_icon=None, avatar=None):
    """Player performance card. Every displayed statistic is computed from imported DB rows."""
    t = totals(rows)
    role = classify_player(t, rows)
    bat_impact, bowl_impact = impact_scores(rows, t)
    rating = round(
        (bat_impact * 0.55 + bowl_impact * 0.45)
        if role == "ALL-ROUNDER"
        else (bat_impact if role == "BATSMAN" else bowl_impact)
    )
    width, height = 1600, 900
    bg = (7, 13, 23)
    panel = (15, 23, 35)
    panel2 = (19, 30, 45)
    white, muted, blue, lime, cyan = (
        (242, 246, 253),
        (143, 160, 181),
        (35, 119, 231),
        (194, 255, 51),
        (81, 218, 238),
    )
    image = Image.new("RGB", (width, height), bg)
    draw = ImageDraw.Draw(image)

    # Original background accents.
    draw.polygon(
        [(1190, 0), (1600, 0), (1600, 900), (1440, 900)],
        fill=(8, 26, 48),
    )
    draw.polygon(
        [(0, 0), (42, 0), (220, 900), (158, 900)],
        fill=(12, 49, 91),
    )
    draw.line((55, 118, 1545, 118), fill=(39, 53, 70), width=2)
    draw.text(
        (58, 32),
        member.guild.name.upper()[:35],
        font=load_font(25, True),
        fill=white,
    )
    draw.text(
        (58, 70),
        "PLAYER PERFORMANCE  /  SEASON PROFILE",
        font=load_font(17, True),
        fill=blue,
    )
    draw.text(
        (1130, 48),
        "PLAYER IDENTITY CARD",
        font=load_font(31, True),
        fill=white,
    )

    # Left identity card.
    draw.rounded_rectangle(
        (55, 145, 555, 835),
        radius=28,
        fill=panel,
        outline=(35, 57, 83),
        width=2,
    )
    draw.text(
        (88, 175), "OVERALL RATING",
        font=load_font(18, True), fill=blue,
    )
    draw.text((82, 200), str(rating), font=load_font(94, True), fill=white)
    draw.text((215, 258), "/100", font=load_font(23, True), fill=muted)

    pill_w = 230 if role == "ALL-ROUNDER" else 190
    draw.rounded_rectangle(
        (330, 180, 330 + pill_w, 228),
        radius=20,
        fill=(12, 42, 76),
        outline=(29, 88, 151),
        width=1,
    )
    draw.text((348, 194), role, font=load_font(18, True), fill=blue)

    # Circular avatar and optional server icon.
    avatar_box = (155, 315, 455, 615)
    draw.ellipse(avatar_box, fill=(39, 56, 75), outline=white, width=6)

    if avatar:
        try:
            av = Image.open(io.BytesIO(avatar)).convert("RGB").resize((280, 280))
            mask = Image.new("L", (280, 280), 0)
            ImageDraw.Draw(mask).ellipse((0, 0, 279, 279), fill=255)
            image.paste(av, (165, 325), mask)
            draw.ellipse((155, 315, 455, 615), outline=white, width=6)
        except Exception:
            pass
    else:
        initials = (member.display_name[:1] or "?").upper()
        draw.text(
            (275, 405), initials,
            font=load_font(90, True), fill=white,
        )

    if guild_icon:
        try:
            gi = Image.open(io.BytesIO(guild_icon)).convert("RGB").resize((82, 82))
            image.paste(gi, (395, 535))
            draw.ellipse((392, 532, 482, 622), outline=blue, width=5)
        except Exception:
            pass

    draw.text(
        (85, 642), member.display_name[:25],
        font=load_font(35, True), fill=white,
    )
    draw.text(
        (88, 687), "TEAM NOT CONNECTED",
        font=load_font(18, True), fill=muted,
    )
    draw.text(
        (88, 735), "BATTING IMPACT",
        font=load_font(17, True), fill=muted,
    )
    draw.text(
        (475, 727), str(bat_impact),
        font=load_font(26, True), fill=white, anchor="ra",
    )
    draw.rounded_rectangle(
        (88, 765, 510, 778), radius=6, fill=(39, 47, 58),
    )
    draw.rounded_rectangle(
        (88, 765, 88 + int(422 * bat_impact / 100), 778),
        radius=6, fill=lime,
    )
    draw.text(
        (88, 790), "BOWLING IMPACT",
        font=load_font(17, True), fill=muted,
    )
    draw.text(
        (475, 782), str(bowl_impact),
        font=load_font(26, True), fill=white, anchor="ra",
    )
    draw.rounded_rectangle(
        (88, 820, 510, 833), radius=6, fill=(39, 47, 58),
    )
    draw.rounded_rectangle(
        (88, 820, 88 + int(422 * bowl_impact / 100), 833),
        radius=6, fill=cyan,
    )

    # Original main stats.
    x0, x1 = 595, 1545
    draw.text(
        (x0, 150), "SEASON SNAPSHOT",
        font=load_font(20, True), fill=white,
    )
    cards = [
        ("MATCHES", t["matches"], blue),
        ("RUNS", t["runs"], lime),
        ("WICKETS", t["wickets"], cyan),
        ("STRIKE RATE", fmt(t["strike_rate"]), blue),
    ]
    card_y = 188
    card_w = 220
    gap = 20

    for i, (label, value, accent) in enumerate(cards):
        x = x0 + i * (card_w + gap)
        draw.rounded_rectangle(
            (x, card_y, x + card_w, card_y + 105),
            radius=18,
            fill=panel,
            outline=(36, 47, 61),
            width=2,
        )
        draw.text(
            (x + 20, card_y + 18), label,
            font=load_font(15, True), fill=muted,
        )
        draw.ellipse(
            (x + card_w - 31, card_y + 19, x + card_w - 17, card_y + 33),
            fill=accent,
        )
        draw.text(
            (x + 20, card_y + 52), str(value),
            font=load_font(31, True),
            fill=accent if label != "MATCHES" else white,
        )

    # Original batting panel.
    draw.rounded_rectangle(
        (x0, 320, x0 + 455, 680),
        radius=23,
        fill=panel,
        outline=(35, 48, 64),
        width=2,
    )
    draw.rounded_rectangle(
        (x0 + 25, 340, x0 + 65, 347), radius=4, fill=lime,
    )
    draw.text(
        (x0 + 25, 362), "BATTING",
        font=load_font(21, True), fill=lime,
    )
    batting = [
        ("INNINGS", t["matches"]),
        ("AVERAGE", fmt(t["bat_avg"])),
        ("BALLS FACED", t["balls_faced"]),
        ("HIGH SCORE", max((r["runs"] for r in rows), default=0)),
        ("FIFTIES", sum(50 <= r["runs"] < 100 for r in rows)),
        ("HUNDREDS", sum(r["runs"] >= 100 for r in rows)),
        ("DUCKS", sum(r["runs"] == 0 and not r["not_out"] for r in rows)),
        ("NOT OUTS", t["not_outs"]),
    ]
    for i, (label, value) in enumerate(batting):
        col, row = i % 2, i // 2
        x = x0 + 30 + col * 210
        y = 405 + row * 64
        draw.text(
            (x, y), label, font=load_font(13, True), fill=muted,
        )
        draw.text(
            (x, y + 21), str(value),
            font=load_font(25, True), fill=white,
        )

    # Original bowling panel.
    bx = x0 + 475
    draw.rounded_rectangle(
        (bx, 320, bx + 475, 680),
        radius=23,
        fill=panel,
        outline=(35, 48, 64),
        width=2,
    )
    draw.rounded_rectangle(
        (bx + 25, 340, bx + 65, 347), radius=4, fill=cyan,
    )
    draw.text(
        (bx + 25, 362), "BOWLING",
        font=load_font(21, True), fill=cyan,
    )
    best = max(
        (
            (r["wickets"], -r["runs_conceded"])
            for r in rows if r["balls_bowled"] > 0
        ),
        default=(0, 0),
    )
    bowling = [
        ("INNINGS", sum(r["balls_bowled"] > 0 for r in rows)),
        ("OVERS", fmt(t["overs"])),
        ("AVERAGE", fmt(t["bowl_avg"])),
        ("ECONOMY", fmt(t["economy"])),
        ("STRIKE RATE", fmt(t["bowl_sr"])),
        ("BEST FIGURES", f"{best[0]}/{-best[1]}"),
    ]
    for i, (label, value) in enumerate(bowling):
        col, row = i % 2, i // 2
        x = bx + 30 + col * 220
        y = 405 + row * 78
        draw.text(
            (x, y), label, font=load_font(13, True), fill=muted,
        )
        draw.text(
            (x, y + 22), str(value),
            font=load_font(25, True), fill=white,
        )

    draw.text(
        (x0, 725),
        "GENERATED FROM RECORDED MATCH STATS  •  TEAM DETAILS CAN BE ADDED LATER",
        font=load_font(15, True),
        fill=muted,
    )
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=False, compress_level=2)
    output.seek(0)
    return output


# ============================================================
# DESIGN 2 - PROVIDED BLUE BACKGROUND
# ============================================================

def get_player_team_name(member):
    """Look up the player's approved team from TeamRegistration's database."""
    import sqlite3

    # teams.py stores teams in team_registration.db, not teams.db.
    db_path = Path(__file__).resolve().parent.parent / "team_registration.db"
    if not db_path.is_file():
        return "NO TEAM CONNECTED"

    guild = getattr(member, "guild", None)
    if guild is None:
        return "NO TEAM CONNECTED"

    connection = None
    try:
        connection = sqlite3.connect(
            f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=3
        )
        connection.row_factory = sqlite3.Row
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if not {"registered_teams", "team_players"}.issubset(tables):
            return "NO TEAM CONNECTED"

        # First check accepted roster membership. Only approved teams in this
        # Discord server should appear on the player's stats card.
        row = connection.execute(
            """
            SELECT t.name
            FROM registered_teams AS t
            LEFT JOIN team_players AS p ON p.team_id = t.id
            WHERE t.guild_id = ?
              AND t.status = 'approved'
              AND (
                    p.user_id = ?
                    OR t.captain_id = ?
                    OR t.vice_captain_id = ?
              )
            ORDER BY
              CASE WHEN t.captain_id = ? THEN 0
                   WHEN t.vice_captain_id = ? THEN 1
                   ELSE 2 END,
              t.name COLLATE NOCASE
            LIMIT 1
            """,
            (
                int(guild.id),
                int(member.id),
                int(member.id),
                int(member.id),
                int(member.id),
                int(member.id),
            ),
        ).fetchone()
        return str(row["name"]) if row and row["name"] else "NO TEAM CONNECTED"
    except (sqlite3.Error, OSError, ValueError, TypeError, AttributeError):
        return "NO TEAM CONNECTED"
    finally:
        if connection is not None:
            connection.close()


@lru_cache(maxsize=1)
def _cached_design2_background():
    """Load and resize the static Design 2 background once per process."""
    from PIL import ImageOps

    background_path = Path(__file__).resolve().with_name("stats_design2_bg.png")
    if not background_path.is_file():
        raise FileNotFoundError(
            f"Design 2 background image is missing: {background_path}"
        )
    with Image.open(background_path) as source:
        return ImageOps.fit(
            source.convert("RGB"), (1600, 900),
            method=Image.Resampling.LANCZOS, centering=(0.5, 0.5),
        )


def design2_impact_scores(rows, t=None):
    """Balanced 0-99 impact ratings based on per-innings output and efficiency.

    Unlike raw fantasy points, these scores are normalized, so long careers do
    not automatically hit 99 and one huge innings cannot dominate the rating.
    """
    t = t or totals(rows)
    matches = max(int(t.get("matches", 0)), 1)
    batting_innings = sum(
        1 for row in rows
        if int(row.get("runs", 0)) > 0 or int(row.get("balls_faced", 0)) > 0
    )
    bowling_innings = sum(1 for row in rows if int(row.get("balls_bowled", 0)) > 0)

    def clamp(value):
        return max(0.0, min(100.0, float(value)))

    # Batting rewards run output, average and scoring rate without allowing
    # any single metric to overwhelm the others.
    runs_per_innings = t["runs"] / max(batting_innings, 1)
    bat_run_score = clamp(runs_per_innings / 50.0 * 100)
    bat_average_score = clamp(t["bat_avg"] / 40.0 * 100)
    bat_rate_score = clamp(t["strike_rate"] / 160.0 * 100)
    bat_score = round(
        bat_run_score * 0.45
        + bat_average_score * 0.30
        + bat_rate_score * 0.25
    )

    # Bowling gives the greatest weight to taking wickets, then rewards
    # economical and effective spells. No-wicket spells cannot earn a
    # misleadingly high impact from economy alone.
    wickets_per_innings = t["wickets"] / max(bowling_innings, 1)
    bowl_wicket_score = clamp(wickets_per_innings / 2.0 * 100)
    bowl_economy_score = clamp((14.0 - t["economy"]) / 10.0 * 100)
    bowl_average_score = clamp((40.0 - t["bowl_avg"]) / 35.0 * 100) if t["wickets"] else 0
    bowl_rate_score = clamp((60.0 - t["bowl_sr"]) / 50.0 * 100) if t["wickets"] else 0
    bowl_score = round(
        bowl_wicket_score * 0.55
        + bowl_economy_score * 0.25
        + bowl_average_score * 0.10
        + bowl_rate_score * 0.10
    )
    if t["wickets"] == 0:
        bowl_score = min(bowl_score, 35)

    return max(0, min(99, bat_score)), max(0, min(99, bowl_score))


def make_stats_image_design2(member, rows, avatar=None, guild_icon=None, fielding_points=0, catches_caught=0, catches_dropped=0, batter_drops=0, batter_taken=0):
    """Render Design 2: batting left, player profile centre, bowling right."""
    width, height = 1600, 900
    # Cached pre-resized background avoids disk I/O and resampling per request.
    image = _cached_design2_background().copy().convert("RGBA")

    # Semi-transparent panels add depth while keeping the blue background visible.
    overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    panel = (4, 13, 27, 230)
    panel_inner = (10, 25, 43, 236)
    border = (39, 133, 247, 235)
    od.rounded_rectangle((54, 258, 520, 805), radius=25, fill=panel, outline=border, width=2)
    od.rounded_rectangle((548, 258, 1052, 805), radius=25, fill=panel, outline=(63, 155, 255, 230), width=2)
    od.rounded_rectangle((1080, 258, 1546, 805), radius=25, fill=panel, outline=border, width=2)
    od.rounded_rectangle((54, 145, 1546, 235), radius=20, fill=(4, 13, 27, 232), outline=(43, 99, 153, 220), width=2)
    image = Image.alpha_composite(image, overlay)
    draw = ImageDraw.Draw(image)

    white = (245, 249, 255, 255)
    muted = (232, 242, 253, 255)
    blue = (61, 151, 255, 255)
    lime = (190, 255, 48, 255)
    cyan = (64, 220, 235, 255)
    gold = (255, 190, 72, 255)
    violet = (187, 139, 255, 255)
    red = (255, 75, 91, 255)
    divider = (100, 142, 181, 190)

    t = totals(rows)
    batting_points, bowling_points, _ = fantasy_raw_points(rows, t)
    bat_impact, bowl_impact = design2_impact_scores(rows, t)
    rating = overall_rating(bat_impact, bowl_impact)
    role = classify_player(t, rows)
    if float(fielding_points) > batting_points and float(fielding_points) > bowling_points:
        role = "FIELDER"
    _, _, fantasy_points = fantasy_raw_points(rows, t)
    fantasy_points += float(fielding_points)
    guild = getattr(member, "guild", None)
    guild_name = getattr(guild, "name", "DISCORD SERVER").upper()
    team_name = get_player_team_name(member).upper()

    # Server name only; Design 2 intentionally has no server logo.
    header_size = 34
    header_font = load_font(header_size, True)
    while draw.textbbox((0, 0), guild_name, font=header_font)[2] > 500 and header_size > 19:
        header_size -= 1
        header_font = load_font(header_size, True)
    draw.text((800, 34), guild_name, font=header_font, fill=white, anchor="mt")
    draw.text((800, 80), "PLAYER PERFORMANCE  /  SEASON PROFILE", font=load_font(16, True), fill=blue, anchor="mt")
    draw.line((55, 119, 1545, 119), fill=(55, 125, 205, 210), width=2)
    draw.rounded_rectangle((55, 116, 300, 122), radius=3, fill=blue)

    # Season snapshot. Total fantasy points are shown here, not over the avatar.
    draw.text((78, 155), "SEASON SNAPSHOT", font=load_font(16, True), fill=muted)
    snapshot = [
        ("MATCHES", t["matches"], blue),
        ("RUNS", t["runs"], gold),
        ("WICKETS", t["wickets"], cyan),
        ("FANTASY POINTS", fmt(fantasy_points), violet),
    ]
    start_x, card_w, card_gap = 72, 350, 18
    for i, (label, value, accent) in enumerate(snapshot):
        x = start_x + i * (card_w + card_gap)
        od2 = ImageDraw.Draw(image)
        od2.rounded_rectangle((x, 178, x + card_w, 222), radius=12,
                              fill=(17, 35, 57, 220), outline=(42, 80, 119, 170), width=1)
        # Keep each snapshot label and its value on one horizontal line.
        # Vertically centre both inside the card and align the value to the right.
        card_mid_y = (178 + 222) // 2
        draw.text((x + 16, card_mid_y), label, font=load_font(16, True),
                  fill=muted, anchor="lm")
        draw.text((x + card_w - 16, card_mid_y), str(value),
                  font=load_font(22, True), fill=accent, anchor="rm")

    # Left panel: batting statistics, with the high score alone on the final row.
    draw.rounded_rectangle((78, 280, 119, 287), radius=3, fill=lime)
    draw.text((78, 300), "BATTING", font=load_font(21, True), fill=lime)
    draw.text((494, 306), "AT THE CREASE", font=load_font(12, True), fill=muted, anchor="ra")
    batting = [
        ("INNINGS", t["matches"], blue),
        ("STRIKE RATE", fmt(t["strike_rate"]), gold),
        ("RUNS", t["runs"], gold),
        ("AVERAGE", fmt(t["bat_avg"]), blue),
        ("FIFTIES", sum(50 <= r["runs"] < 100 for r in rows), lime),
        ("HUNDREDS", sum(r["runs"] >= 100 for r in rows), gold),
        ("NOT OUTS", t["not_outs"], cyan),
        ("DUCKS", sum(r["runs"] == 0 and not r["not_out"] for r in rows), red),
    ]
    # Generous gap between columns; the divider is centred in the gap.
    col_x = (82, 318)
    row_y = (354, 442, 530, 618)
    for i, (label, value, accent) in enumerate(batting):
        col, row = i % 2, i // 2
        x, y = col_x[col], row_y[row]
        draw.rounded_rectangle((x - 7, y - 8, x + 190, y + 61), radius=10,
                               fill=(19, 26, 36, 205), outline=(65, 82, 101, 115), width=1)
        draw.text((x + 4, y), label, font=load_font(16, True), fill=(157, 172, 190, 255))
        # All batting values are white except the ducks number, which is red.
        draw.text((x + 4, y + 23), str(value), font=load_font(26, True), fill=red if label == "DUCKS" else white)
    draw.line((292, 351, 292, 682), fill=divider, width=2)

    # High score is deliberately alone across the bottom of the batting panel.
    high_score = max((r["runs"] for r in rows), default=0)
    draw.rounded_rectangle((82, 713, 492, 775), radius=12, fill=(14, 31, 50, 175), outline=(44, 82, 120, 150), width=1)
    draw.text((98, 722), "HIGH SCORE", font=load_font(16, True), fill=muted)
    draw.text((98, 741), str(high_score), font=load_font(25, True), fill=gold)

    # Centre profile: avatar, username, team and role. No rating here.
    # Role pill is anchored to the upper-right of the profile area, clear of the avatar.
    role_label = role.upper()
    role_font = load_font(14, True)
    role_width = draw.textbbox((0, 0), role_label, font=role_font)[2]
    role_pill_width = max(150, role_width + 34)
    role_right = 1018
    role_left = role_right - role_pill_width
    draw.rounded_rectangle((role_left, 280, role_right, 319), radius=17,
                           fill=(13, 35, 59, 245), outline=(42, 113, 181, 220), width=1)
    # Keep the existing role badge in place; center only its label vertically and horizontally.
    draw.text(((role_left + role_right) // 2, 299), role_label,
              font=role_font, fill=blue, anchor="mm")

    # Reference-inspired overall rating: bold score and /100 above-left of avatar.
    # Keep the avatar and role badge in their existing positions.
    draw.text((584, 286), "OVERALL RATING", font=load_font(16, True),
              fill=blue, anchor="lt")
    draw.text((582, 306), str(rating), font=load_font(66, True),
              fill=white, anchor="lt")
    draw.text((671, 347), "/100", font=load_font(17, True),
              fill=muted, anchor="lm")

    avatar_size, avatar_x, avatar_y = 206, 697, 338
    draw.ellipse((avatar_x - 9, avatar_y - 9, avatar_x + avatar_size + 9, avatar_y + avatar_size + 9),
                 fill=(2, 11, 24, 255), outline=blue, width=5)
    pasted_avatar = False
    if avatar:
        try:
            av = Image.open(io.BytesIO(avatar)).convert("RGB")
            av = ImageOps.fit(av, (avatar_size, avatar_size), method=Image.Resampling.LANCZOS)
            mask = Image.new("L", (avatar_size, avatar_size), 0)
            ImageDraw.Draw(mask).ellipse((0, 0, avatar_size - 1, avatar_size - 1), fill=255)
            image.paste(av, (avatar_x, avatar_y), mask)
            pasted_avatar = True
        except Exception:
            pass
    draw = ImageDraw.Draw(image)
    if not pasted_avatar:
        initials = (getattr(member, "display_name", "?")[:1] or "?").upper()
        draw.text((800, avatar_y + 60), initials, font=load_font(74, True), fill=white, anchor="mt")
    draw.ellipse((avatar_x - 9, avatar_y - 9, avatar_x + avatar_size + 9, avatar_y + avatar_size + 9), outline=blue, width=5)

    username = getattr(member, "display_name", "Player")
    username_size = 29
    username_font = load_font(username_size, True)
    while draw.textbbox((0, 0), username, font=username_font)[2] > 455 and username_size > 16:
        username_size -= 1
        username_font = load_font(username_size, True)
    draw.text((800, 566), username, font=username_font, fill=white, anchor="mt")

    team_font_size = 15
    team_font = load_font(team_font_size, True)
    while draw.textbbox((0, 0), team_name, font=team_font)[2] > 455 and team_font_size > 10:
        team_font_size -= 1
        team_font = load_font(team_font_size, True)
    draw.text((800, 608), team_name, font=team_font, fill=muted, anchor="mt")
    # Fantasy points repeated in the player identity area as requested.
    draw.rounded_rectangle((650, 631, 950, 671), radius=12,
                           fill=(14, 31, 50, 220), outline=(54, 95, 135, 170), width=1)
    draw.text((670, 651), "FANTASY POINTS", font=load_font(13, True),
              fill=muted, anchor="lm")
    draw.text((930, 651), fmt(fantasy_points), font=load_font(22, True),
              fill=violet, anchor="rm")
    draw.line((590, 681, 1010, 681), fill=(64, 94, 126, 200), width=1)

    # Wider, thicker impact bars make the rating strength immediately visible.
    draw.rounded_rectangle((590, 692, 1010, 740), radius=12,
                           fill=(14, 31, 50, 210), outline=(54, 95, 135, 170), width=1)
    draw.text((610, 701), "BATTING IMPACT", font=load_font(13, True), fill=muted)
    draw.text((990, 698), str(bat_impact), font=load_font(23, True),
              fill=lime, anchor="ra")
    draw.rounded_rectangle((610, 725, 990, 734), radius=4, fill=(36, 48, 60, 255))
    if bat_impact > 0:
        draw.rounded_rectangle((610, 725, 610 + int(380 * bat_impact / 99), 734),
                               radius=4, fill=lime)

    draw.rounded_rectangle((590, 750, 1010, 798), radius=12,
                           fill=(14, 31, 50, 210), outline=(54, 95, 135, 170), width=1)
    draw.text((610, 759), "BOWLING IMPACT", font=load_font(13, True), fill=muted)
    draw.text((990, 756), str(bowl_impact), font=load_font(23, True),
              fill=cyan, anchor="ra")
    draw.rounded_rectangle((610, 783, 990, 792), radius=4, fill=(36, 48, 60, 255))
    if bowl_impact > 0:
        draw.rounded_rectangle((610, 783, 610 + int(380 * bowl_impact / 99), 792),
                               radius=4, fill=cyan)

    # Right panel: bowling stats with well-spaced paired columns.
    draw.rounded_rectangle((1105, 280, 1146, 287), radius=3, fill=cyan)
    draw.text((1105, 300), "BOWLING", font=load_font(21, True), fill=cyan)
    draw.text((1518, 306), "WITH THE BALL", font=load_font(12, True), fill=muted, anchor="ra")
    best = max(
        ((r["wickets"], -r["runs_conceded"]) for r in rows if r["balls_bowled"] > 0),
        default=(0, 0),
    )
    bowling = [
        ("INNINGS", sum(r["balls_bowled"] > 0 for r in rows), cyan),
        ("OVERS", fmt(t["overs"]), blue),
        ("WICKETS", t["wickets"], cyan),
        ("AVERAGE", fmt(t["bowl_avg"]), blue),
        ("ECONOMY", fmt(t["economy"]), cyan),
        ("STRIKE RATE", fmt(t["bowl_sr"]), blue),
        ("3W HAULS", sum(3 <= r["wickets"] < 5 for r in rows), gold),
        ("5W HAULS", sum(r["wickets"] >= 5 for r in rows), gold),
    ]
    bowl_col_x = (1108, 1345)
    bowl_row_y = (354, 442, 530, 618)
    for i, (label, value, accent) in enumerate(bowling):
        col, row = i % 2, i // 2
        x, y = bowl_col_x[col], bowl_row_y[row]
        draw.rounded_rectangle((x - 5, y - 8, x + 190, y + 61), radius=10,
                               fill=(19, 26, 36, 205), outline=(65, 82, 101, 115), width=1)
        draw.text((x + 5, y), label, font=load_font(16, True), fill=(157, 172, 190, 255))
        draw.text((x + 5, y + 23), str(value), font=load_font(26, True), fill=white)
    draw.line((1320, 351, 1320, 682), fill=(73, 94, 116, 190), width=2)

    # Best figures gets the same full-width highlight-card treatment as batting high score.
    draw.rounded_rectangle((1105, 713, 1518, 775), radius=12,
                           fill=(19, 26, 36, 205), outline=(65, 82, 101, 145), width=1)
    draw.text((1121, 722), "BEST FIGURES", font=load_font(16, True),
              fill=(157, 172, 190, 255))
    draw.text((1121, 741), f"{best[0]}/{-best[1]}", font=load_font(25, True),
              fill=cyan)

    # Dedicated fielding category, matching the existing full-width footer style.
    draw.rounded_rectangle((55, 805, 1545, 850), radius=12,
                           fill=(10, 25, 43, 236), outline=(39, 133, 247, 150), width=1)
    draw.text((78, 827), "FIELDING", font=load_font(15, True), fill=cyan, anchor="lm")
    field_items = [
        ("CATCHES", catches_caught),
        ("DROPS", catches_dropped),
        ("FIELDING PTS", fmt(fielding_points)),
        ("BATTER DROPS", batter_drops),
        ("BATTER CATCHES", batter_taken),
    ]
    for i, (label, value) in enumerate(field_items):
        x = 300 + i * 235
        if i:
            draw.line((x - 22, 814, x - 22, 841), fill=(64, 94, 126, 150), width=1)
        draw.text((x, 814), label, font=load_font(10, True), fill=muted)
        draw.text((x, 830), str(value), font=load_font(17, True),
                  fill=violet if label == "FIELDING PTS" else white)

    draw.text((800, 862), "GOOGLI MANAGER  •  PLAYER PERFORMANCE FROM RECORDED MATCH STATS",
              font=load_font(12, True), fill=muted, anchor="mt")

    output = io.BytesIO()
    image.convert("RGB").save(output, format="PNG", optimize=False, compress_level=2)
    output.seek(0)
    return output


# ============================================================
# DESIGN 3 - TIERED PLAYER CARD
# ============================================================

@lru_cache(maxsize=64)
def load_stats_display_font(size, bold=False):
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSansNarrow-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation/LiberationSansNarrow-Regular.ttf",
        "C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def make_stats_image_design3(
    member, rows, avatar=None, fielding_points=0,
    catches_caught=0, catches_dropped=0,
    batter_drops=0, batter_taken=0, recent_matches=None,
):
    """Asymmetric broadcast dashboard with a horizontal player header."""
    width, height = 1600, 900
    t = totals(rows)
    bat_impact, bowl_impact = impact_scores(rows, t)
    batting_points, bowling_points, fantasy_points = fantasy_raw_points(rows, t)
    fantasy_points += float(fielding_points)
    role = classify_player(t, rows)
    if float(fielding_points) > batting_points and float(fielding_points) > bowling_points:
        role = "FIELDER"
    rating = overall_rating(bat_impact, bowl_impact)

    bg, panel, panel2 = (7, 13, 22), (13, 21, 32), (17, 29, 42)
    white, muted = (242, 246, 252), (137, 153, 173)
    blue, cyan, lime, gold, red = (16, 111, 202), (78, 216, 235), (190, 255, 48), (239, 190, 91), (242, 102, 119)
    stroke = (34, 49, 66)
    F = load_stats_display_font

    image = _cached_design2_background().copy().convert("RGB")
    if image.size != (width, height):
        image = image.resize((width, height), Image.Resampling.LANCZOS)
    draw = ImageDraw.Draw(image)
    guild = getattr(getattr(member, "guild", None), "name", "GOOGLI MANAGER").upper()
    username = getattr(member, "display_name", "PLAYER")
    team_name = get_player_team_name(member).upper() or "NO TEAM CONNECTED"

    # Reuse Design 2's exact background artwork; only the layout and panels differ.
    draw.rectangle((0, 0, width, 7), fill=blue)
    draw.text((55, 22), guild[:42], font=F(23, True), fill=white)
    draw.text((56, 51), "PLAYER PERFORMANCE  /  SEASON REPORT", font=F(16, True), fill=(71, 164, 244))
    draw.text((1545, 27), "KT • PLAYER FILE", font=F(13, True), fill=muted, anchor="ra")
    draw.text((1545, 48), "SEASON STATISTICS", font=F(30, True), fill=white, anchor="ra")
    draw.line((55, 83, 1545, 83), fill=stroke, width=2)
    draw.line((55, 83, 225, 83), fill=blue, width=3)
    draw.line((225, 83, 275, 83), fill=lime, width=3)

    # Full-width identity banner: rating left, identity centered, avatar right.
    draw.rounded_rectangle((55, 105, 1545, 260), radius=22, fill=panel, outline=stroke, width=2)
    draw.rounded_rectangle((55, 138, 61, 227), radius=3, fill=blue)
    draw.text((84, 125), "OVERALL RATING", font=F(11, True), fill=(71, 164, 244))
    draw.text((80, 148), str(rating), font=F(58, True), fill=gold)
    draw.text((173, 190), "/ 99", font=F(15, True), fill=muted)
    draw.line((255, 126, 255, 238), fill=stroke, width=1)

    name_size = 44
    name_font = F(name_size, True)
    while draw.textbbox((0, 0), username, font=name_font)[2] > 790 and name_size > 20:
        name_size -= 1
        name_font = F(name_size, True)
    draw.text((300, 145), username, font=name_font, fill=white)
    team_size = 18
    team_font = F(team_size, True)
    while draw.textbbox((0, 0), team_name, font=team_font)[2] > 780 and team_size > 11:
        team_size -= 1
        team_font = F(team_size, True)
    draw.text((303, 193), team_name, font=team_font, fill=muted)
    draw.rounded_rectangle((302, 218, 435, 242), radius=11, fill=(13, 48, 79),
                           outline=(29, 91, 145), width=1)
    draw.text((368, 230), role, font=F(10, True), fill=cyan, anchor="mm")
    draw.text((480, 225), f"BATTING IMPACT  {bat_impact}", font=F(10, True), fill=lime)
    draw.text((700, 225), f"BOWLING IMPACT  {bowl_impact}", font=F(10, True), fill=cyan)
    draw.line((1265, 126, 1265, 238), fill=stroke, width=1)

    av_size, av_x, av_y = 126, 1342, 119
    pasted = False
    if avatar:
        try:
            av = Image.open(io.BytesIO(avatar)).convert("RGB")
            av = ImageOps.fit(av, (av_size, av_size), method=Image.Resampling.LANCZOS)
            mask = Image.new("L", (av_size, av_size), 0)
            ImageDraw.Draw(mask).ellipse((0, 0, av_size - 1, av_size - 1), fill=255)
            image.paste(av, (av_x, av_y), mask)
            pasted = True
        except Exception:
            pass
    if not pasted:
        draw.ellipse((av_x, av_y, av_x + av_size, av_y + av_size), fill=panel2)
        draw.text((av_x + av_size // 2, av_y + av_size // 2), username[:1].upper(),
                  font=F(45, True), fill=white, anchor="mm")
    draw.ellipse((av_x - 4, av_y - 4, av_x + av_size + 4, av_y + av_size + 4),
                 outline=white, width=4)
    draw.text((1406, 246), "PLAYER AVATAR", font=F(10, True), fill=muted, anchor="mm")

    # Compact six-metric ribbon.
    draw.text((57, 281), "SEASON SNAPSHOT", font=F(17, True), fill=white)
    snapshot = [
        ("MATCHES", t["matches"], blue),
        ("TOTAL RUNS", t["runs"], lime),
        ("WICKETS", t["wickets"], cyan),
        ("BATTING AVG", fmt(t["bat_avg"]), white),
        ("STRIKE RATE", fmt(t["strike_rate"]), gold),
        ("FANTASY PTS", fmt(fantasy_points), (57, 139, 220)),
    ]
    snap_x, snap_y, snap_gap = 55, 307, 12
    snap_w = (1490 - snap_gap * 5) // 6
    for i, (label, value, accent) in enumerate(snapshot):
        x = snap_x + i * (snap_w + snap_gap)
        draw.rounded_rectangle((x, snap_y, x + snap_w, snap_y + 75), radius=13,
                               fill=panel2, outline=stroke, width=1)
        draw.text((x + 15, snap_y + 11), label, font=F(11, True), fill=muted)
        draw.text((x + 15, snap_y + 32), str(value), font=F(30, True), fill=accent)
        draw.rounded_rectangle((x + snap_w - 20, snap_y + 13, x + snap_w - 11, snap_y + 22),
                               radius=4, fill=accent)

    # Three balanced columns: batting, recent matches, bowling.
    top, bottom = 402, 786
    boxes = [(55, top, 535, bottom), (560, top, 1040, bottom), (1065, top, 1545, bottom)]
    for box in boxes:
        draw.rounded_rectangle(box, radius=19, fill=panel, outline=stroke, width=2)

    # Batting column uses a single aligned list to avoid the previous overlapping split columns.
    draw.rounded_rectangle((80, 424, 122, 431), radius=3, fill=lime)
    draw.text((80, 447), "BATTING", font=F(23, True), fill=lime)
    draw.text((510, 451), "AT THE CREASE", font=F(11, True), fill=muted, anchor="ra")
    batting = [
        ("INNINGS", t["matches"]), ("RUNS SCORED", t["runs"]),
        ("BALLS FACED", t["balls_faced"]), ("AVERAGE", fmt(t["bat_avg"])),
        ("STRIKE RATE", fmt(t["strike_rate"])), ("NOT OUTS", t["not_outs"]),
        ("FIFTIES", sum(50 <= r["runs"] < 100 for r in rows)),
        ("HUNDREDS", sum(r["runs"] >= 100 for r in rows)),
        ("DUCKS", sum(r["runs"] == 0 and not r["not_out"] for r in rows)),
        ("HIGH SCORE", max((r["runs"] for r in rows), default=0)),
    ]
    for i, (label, value) in enumerate(batting):
        y = 483 + i * 29
        draw.text((83, y), label, font=F(11, True), fill=muted)
        color = red if label == "DUCKS" and value else (lime if label == "HIGH SCORE" else white)
        draw.text((505, y - 2), str(value), font=F(19, True), fill=color, anchor="ra")
        if i < len(batting) - 1:
            draw.line((82, y + 21, 508, y + 21), fill=(24, 37, 52), width=1)

    # Recent match list is now the center feature rather than a tiny bottom strip.
    draw.rounded_rectangle((585, 424, 627, 431), radius=3, fill=blue)
    draw.text((585, 447), "RECENT MATCHES", font=F(23, True), fill=white)
    draw.text((1015, 451), "LATEST 5", font=F(9, True), fill=muted, anchor="ra")
    recent_matches = recent_matches or []
    for i in range(5):
        y = 480 + i * 57
        item = recent_matches[i] if i < len(recent_matches) else None
        accent = blue if item is None else (lime if int(item.get("runs", 0)) >= 20 else cyan)
        draw.rounded_rectangle((582, y, 1018, y + 49), radius=10,
                               fill=panel2, outline=(27, 42, 58), width=1)
        draw.rounded_rectangle((582, y + 8, 586, y + 41), radius=2, fill=accent)
        if item:
            opponent = str(item.get("opponent") or "OPPONENT NOT SAVED").upper()
            if len(opponent) > 25:
                opponent = opponent[:23] + "…"
            draw.text((598, y + 8), opponent, font=F(10, True), fill=muted)
            score = f'{item.get("runs", 0)}{"*" if item.get("not_out") else ""}'
            draw.text((598, y + 22), score, font=F(22, True), fill=accent)
            draw.text((670, y + 31), f'{item.get("balls_faced", 0)} BALLS',
                      font=F(10, True), fill=muted)
            draw.line((770, y + 9, 770, y + 40), fill=stroke, width=1)
            draw.text((786, y + 9), "BOWLING", font=F(8, True), fill=muted)
            bowl = (f'{item.get("wickets", 0)}/{item.get("runs_conceded", 0)}'
                    if int(item.get("balls_bowled", 0)) > 0 else "DNB")
            draw.text((786, y + 23), bowl, font=F(17, True), fill=white)
        else:
            draw.text((598, y + 18), "NO MATCH DATA", font=F(9, True), fill=muted)

    # Bowling column, same single-column alignment for clean readability.
    draw.rounded_rectangle((1090, 424, 1132, 431), radius=3, fill=cyan)
    draw.text((1090, 447), "BOWLING", font=F(23, True), fill=cyan)
    draw.text((1520, 451), "WITH THE BALL", font=F(9, True), fill=muted, anchor="ra")
    best = max(((r["wickets"], -r["runs_conceded"]) for r in rows if r["balls_bowled"] > 0),
               default=(0, 0))
    bowling = [
        ("INNINGS", sum(r["balls_bowled"] > 0 for r in rows)),
        ("OVERS", fmt(t["overs"])), ("WICKETS TAKEN", t["wickets"]),
        ("RUNS CONCEDED", t["runs_conceded"]), ("AVERAGE", fmt(t["bowl_avg"])),
        ("ECONOMY", fmt(t["economy"])), ("STRIKE RATE", fmt(t["bowl_sr"])),
        ("BEST FIGURES", f"{best[0]}/{-best[1]}"),
        ("3-WICKET HAULS", sum(3 <= r["wickets"] < 5 for r in rows)),
        ("5-WICKET HAULS", sum(r["wickets"] >= 5 for r in rows)),
    ]
    for i, (label, value) in enumerate(bowling):
        y = 485 + i * 27
        draw.text((1093, y), label, font=F(9, True), fill=muted)
        color = cyan if label in ("WICKETS TAKEN", "BEST FIGURES") else white
        draw.text((1515, y - 2), str(value), font=F(15, True), fill=color, anchor="ra")
        if i < len(bowling) - 1:
            draw.line((1092, y + 21, 1518, y + 21), fill=(24, 37, 52), width=1)

    # Fielding footer is full-width and separate from the three columns.
    draw.rounded_rectangle((55, 805, 1545, 865), radius=13,
                           fill=panel2, outline=stroke, width=1)
    draw.text((78, 827), "FIELDING", font=F(15, True), fill=cyan)
    field_items = [
        ("CATCHES / DROPS", f"{catches_caught} / {catches_dropped}"),
        ("FIELDING POINTS", fmt(fielding_points)),
        ("BATTER DROPS", batter_drops),
        ("BATTER CATCHES", batter_taken),
    ]
    for i, (label, value) in enumerate(field_items):
        x = 285 + i * 300
        draw.line((x - 18, 817, x - 18, 853), fill=stroke, width=1)
        draw.text((x, 815), label, font=F(8, True), fill=muted)
        draw.text((x, 834), str(value), font=F(15, True), fill=cyan if i == 1 else white)

    output = io.BytesIO()
    image.save(output, format="PNG", optimize=False, compress_level=2)
    output.seek(0)
    return output

# ============================================================
# EXISTING EMBEDS AND DROPDOWN
# ============================================================

def build_overview_embed(player_name, rows):
    t = totals(rows)
    embed = discord.Embed(
        title=f"Overall Performance — {player_name}",
        description=(
            f"**Matches recorded:** {t['matches']}\n"
            "Use the dropdown to view overall performance or the player's "
            "most recent five matches."
        ),
        color=discord.Color.from_rgb(24, 47, 82),
    )
    embed.add_field(
        name="Batting",
        value=(
            f"Runs: **{t['runs']}**\n"
            f"Balls: **{t['balls_faced']}**\n"
            f"Average: **{fmt(t['bat_avg'])}**\n"
            f"Strike rate: **{fmt(t['strike_rate'])}**\n"
            f"Not outs: **{t['not_outs']}**"
        ),
        inline=True,
    )
    embed.add_field(
        name="Bowling",
        value=(
            f"Wickets: **{t['wickets']}**\n"
            f"Runs conceded: **{t['runs_conceded']}**\n"
            f"Balls bowled: **{t['balls_bowled']}**\n"
            f"Economy: **{fmt(t['economy'])}**\n"
            f"Average: **{fmt(t['bowl_avg'])}**\n"
            f"Strike rate: **{fmt(t['bowl_sr'])}**"
        ),
        inline=True,
    )
    embed.set_footer(text="GOOGLI MANAGER • Overall performance")
    return embed


def build_recent_embed(player_name, rows):
    recent = list(reversed(rows[-5:]))
    embed = discord.Embed(
        title=f"Recent 5 Matches — {player_name}",
        description=(
            "Most recent imported records first. "
            "Match names/opponents can be connected later."
        ),
        color=discord.Color.from_rgb(24, 47, 82),
    )
    for index, row in enumerate(recent, start=1):
        outs = "not out" if row["not_out"] else "out"
        embed.add_field(
            name=f"Recent match {index}",
            value=(
                f"**Batting:** {row['runs']} runs off "
                f"{row['balls_faced']} balls ({outs})\n"
                f"**Bowling:** {row['wickets']} wickets, "
                f"{row['runs_conceded']} runs conceded, "
                f"{row['balls_bowled']} balls bowled"
            ),
            inline=False,
        )
    embed.set_footer(text=f"Showing {len(recent)} of {len(rows)} recorded matches")
    return embed


def make_recent_matches_image(player_name, rows):
    """Render recent match records as an image so the command never sends stats text."""
    width, height = 1600, 900
    image = _cached_design2_background().copy().convert("RGBA")
    overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    od.rounded_rectangle((55, 65, 1545, 835), radius=28,
                         fill=(4, 13, 27, 232), outline=(39, 133, 247, 235), width=3)
    image = Image.alpha_composite(image, overlay)
    draw = ImageDraw.Draw(image)
    white = (245, 249, 255, 255)
    muted = (232, 242, 253, 255)
    blue = (61, 151, 255, 255)
    draw.text((800, 100), "RECENT MATCHES", font=load_font(42, True), fill=white, anchor="mt")
    draw.text((800, 157), player_name.upper(), font=load_font(23, True), fill=blue, anchor="mt")
    recent = list(reversed(rows[-5:]))
    if not recent:
        draw.text((800, 430), "NO RECENT MATCH DATA", font=load_font(28, True), fill=muted, anchor="mm")
    else:
        gap = 16
        top = 220
        card_h = min(108, (580 - gap * (len(recent) - 1)) // len(recent))
        for i, row in enumerate(recent, start=1):
            y = top + (i - 1) * (card_h + gap)
            draw.rounded_rectangle((95, y, 1505, y + card_h), radius=17,
                                   fill=(10, 25, 43, 242), outline=(39, 133, 247, 210), width=2)
            draw.text((125, y + 18), f"MATCH {i}", font=load_font(22, True), fill=blue)
            status = "NOT OUT" if row["not_out"] else "OUT"
            draw.text((125, y + 54), status, font=load_font(16, True), fill=muted)
            draw.text((520, y + card_h // 2), f"{row['runs']} RUNS  •  {row['balls_faced']} BALLS",
                      font=load_font(22, True), fill=white, anchor="lm")
            draw.text((1030, y + card_h // 2),
                      f"{row['wickets']} WKTS  •  {row['runs_conceded']} RUNS  •  {row['balls_bowled']} BALLS",
                      font=load_font(19, True), fill=white, anchor="lm")
    output = io.BytesIO()
    image.convert("RGB").save(output, format="PNG", optimize=False, compress_level=2)
    output.seek(0)
    return output


class StatsSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(
            placeholder="Choose a stats view...", min_values=1, max_values=1,
            options=[
                discord.SelectOption(label="Overview", description="Show the quick stats overview", emoji="<:overview_:1564906690925830164>", value="overview", default=True),
                discord.SelectOption(label="Deep Stats", description="Show extended stats and recent five match scores", emoji="<:deep_stats:1564906372187947008>", value="deep"),
                discord.SelectOption(label="Image", description="Show the generated stats image", emoji="💎", value="image"),
            ],
        )

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        if interaction.user.id != view.requester_id:
            await interaction.response.send_message(
                "This stats menu belongs to the person who requested it.", ephemeral=True
            )
            return

        selected = self.values[0]
        for option in self.options:
            option.default = option.value == selected

        if selected == "overview":
            await interaction.response.edit_message(
                content=None, embed=view.overview_embed, attachments=[], view=view
            )
            return

        if selected == "deep":
            await interaction.response.edit_message(
                content=None, embed=view.deep_embed, attachments=[], view=view
            )
            return

        image = io.BytesIO(view.overall_image)
        image.seek(0)
        file = discord.File(image, filename=f"player_{view.member_id}_stats.png")
        await interaction.response.edit_message(
            content=None, embed=None, attachments=[file], view=view
        )


class StatsCompetitionSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(
            placeholder="Switch competition stats...",
            min_values=1, max_values=1,
            options=[
                discord.SelectOption(label="MAIN TOURNAMENT", value="MAIN TOURNAMENT"),
                discord.SelectOption(label="THE LAST GATE", value="THE LAST GATE"),
            ],
            row=1,
        )

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        if interaction.user.id != view.requester_id:
            await interaction.response.send_message(
                "This stats menu belongs to the person who requested it.", ephemeral=True
            )
            return
        selected = self.values[0]
        await interaction.response.defer()
        # Re-render into this same message instead of posting a second stats message.
        pending = getattr(view.cog, "_stats_switch_interactions", None)
        if pending is None:
            pending = {}
            view.cog._stats_switch_interactions = pending
        context_key = int(view.ctx.message.id)
        pending[context_key] = interaction
        try:
            await view.cog.player_stats.callback(
                view.cog, view.ctx, user_id=f"{view.member_id} | {selected}"
            )
        finally:
            pending.pop(context_key, None)


class StatsView(discord.ui.View):
    def __init__(self, requester_id, member_id, overall_image, overview_embed, deep_embed,
                 cog=None, ctx=None, has_last_gate=False):
        super().__init__(timeout=600)
        self.requester_id = requester_id
        self.member_id = member_id
        self.overall_image = overall_image
        self.overview_embed = overview_embed
        self.deep_embed = deep_embed
        self.cog = cog
        self.ctx = ctx
        self.add_item(StatsSelect())
        if has_last_gate and cog is not None and ctx is not None:
            self.add_item(StatsCompetitionSelect())


# Existing leaderboard metric definitions.
LB_METRICS = [
    ("runs", "Runs", "Batting", True),
    ("bat_avg", "Batting Average", "Batting", True),
    ("fifties", "Most 50s", "Batting", True),
    ("hundreds", "Most 100s", "Batting", True),
    ("ducks", "Most Ducks", "Batting", True),
    ("highest", "Highest Score", "Batting", True),
    ("wickets", "Wickets", "Bowling", True),
    ("bowl_avg", "Bowling Average", "Bowling", False),
    ("economy", "Economy", "Bowling", False),
    ("best_figures", "Best Bowling Figures", "Bowling", True),
    ("bowl_sr", "Bowling Strike Rate", "Bowling", False),
]


# ============================================================
# STATS COG AND COMMANDS
# ============================================================

class ResetServerStatsView(discord.ui.View):
    """Confirmation controls for the authorized user's server stats reset."""

    def __init__(self, cog, guild_id, owner_id):
        super().__init__(timeout=60)
        self.cog = cog
        self.guild_id = guild_id
        self.owner_id = owner_id
        self.completed = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Only the user who started this reset can confirm or cancel it.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="Confirm reset", style=discord.ButtonStyle.danger)
    async def confirm_reset(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.completed:
            await interaction.response.send_message(
                "This reset confirmation has already been used.", ephemeral=True
            )
            return

        with self.cog.db:
            self.cog.db.execute(
                """DELETE FROM match_teams
                   WHERE source_message_id IN (
                       SELECT source_message_id FROM stats_imports WHERE guild_id = ?
                   )""",
                (self.guild_id,),
            )
            self.cog.db.execute(
                """DELETE FROM player_match_stats
                   WHERE source_message_id IN (
                       SELECT source_message_id FROM stats_imports WHERE guild_id = ?
                   )""",
                (self.guild_id,),
            )
            result = self.cog.db.execute(
                "DELETE FROM stats_imports WHERE guild_id = ?",
                (self.guild_id,),
            )
        deleted_matches = result.rowcount
        self.completed = True
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(
            content=(
                f"✅ Server stats have been reset. Deleted **{deleted_matches}** imported "
                "match record(s) and their player statistics. Other servers and design settings "
                "were not changed."
            ),
            view=self,
        )
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_reset(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.completed = True
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(
            content="Reset cancelled. No server stats were changed.", view=self
        )
        self.stop()

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True




class CompetitionImportSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(
            placeholder="Choose which competition these stats belong to...",
            min_values=1, max_values=1,
            options=[
                discord.SelectOption(label="MAIN TOURNAMENT", value="MAIN TOURNAMENT",
                                     description="Import into the main tournament stats"),
                discord.SelectOption(label="THE LAST GATE", value="THE LAST GATE",
                                     description="Import only into The Last Gate stats"),
            ],
        )

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        if interaction.user.id != view.requester_id:
            await interaction.response.send_message("Only the person who ran +as can choose this.", ephemeral=True)
            return
        view.choice = self.values[0]
        await interaction.response.edit_message(
            content=f"Competition selected: **{view.choice}**. Continuing stats import…", view=None
        )
        view.stop()


class CompetitionImportView(discord.ui.View):
    def __init__(self, requester_id):
        super().__init__(timeout=60)
        self.requester_id = requester_id
        self.choice = None
        self.add_item(CompetitionImportSelect())


class CatchSyncView(discord.ui.View):
    """Ask whether +as should scan recent Hand Cricket catch messages."""

    def __init__(self, requester_id):
        super().__init__(timeout=60)
        self.requester_id = requester_id
        self.choice = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "Only the person who ran `+as` can choose this.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Yes, sync catches", style=discord.ButtonStyle.success)
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.choice = True
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(
            content="How many hours back should I scan? Send a whole number (for example, `3`).",
            view=self,
        )
        self.stop()

    @discord.ui.button(label="No, skip", style=discord.ButtonStyle.secondary)
    async def no(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.choice = False
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(
            content="Catch sync skipped. Calculating fantasy points without fielding points…",
            view=self,
        )
        self.stop()


class Stats(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.db = bot.db

        # A separate table avoids assumptions about the existing settings schema.
        # IF NOT EXISTS does not delete or recreate existing tables or records.
        self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS stats_design_settings (
                guild_id INTEGER PRIMARY KEY,
                design INTEGER NOT NULL DEFAULT 1
                    CHECK (design IN (1, 2, 3))
            )
            """
        )
        # Upgrade the older two-design CHECK constraint without losing saved
        # per-server selections. SQLite cannot alter a CHECK constraint in place.
        design_schema = self.db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='stats_design_settings'"
        ).fetchone()
        if design_schema and "1, 2, 3" not in str(design_schema[0]):
            self.db.execute("ALTER TABLE stats_design_settings RENAME TO stats_design_settings_old")
            self.db.execute(
                """
                CREATE TABLE stats_design_settings (
                    guild_id INTEGER PRIMARY KEY,
                    design INTEGER NOT NULL DEFAULT 1
                        CHECK (design IN (1, 2, 3))
                )
                """
            )
            self.db.execute(
                "INSERT INTO stats_design_settings (guild_id, design) "
                "SELECT guild_id, design FROM stats_design_settings_old"
            )
            self.db.execute("DROP TABLE stats_design_settings_old")
        self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS player_match_fielding (
                source_message_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                opportunities INTEGER NOT NULL DEFAULT 0,
                catches_taken INTEGER NOT NULL DEFAULT 0,
                catches_dropped INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (source_message_id, user_id)
            )
            """
        )
        # Each player's team as it was when the match was imported, so a later
        # transfer does not change who a past match was played against.
        self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS match_teams (
                source_message_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                team_name TEXT NOT NULL,
                PRIMARY KEY (source_message_id, user_id)
            )
            """
        )
        self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS raw_stats_report_channels (
                guild_id INTEGER PRIMARY KEY,
                channel_id INTEGER NOT NULL,
                configured_by INTEGER NOT NULL,
                configured_at REAL NOT NULL
            )
            """
        )
        self.db.commit()

    def get_player_transfers(self, guild_id, user_id):
        """Return this player's recorded team transfers, oldest first."""
        path = Path(__file__).resolve().parent.parent / "team_registration.db"
        if not path.is_file():
            return []
        try:
            with sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=5) as conn:
                conn.row_factory = sqlite3.Row
                return [dict(row) for row in conn.execute(
                    "SELECT old_team_name, new_team_name, transferred_at "
                    "FROM player_team_transfers WHERE guild_id=? AND user_id=? ORDER BY id",
                    (int(guild_id), int(user_id)),
                ).fetchall()]
        except sqlite3.Error:
            return []

    def _saved_player_team(self, match_id, user_id):
        row = self.db.execute(
            "SELECT team_name FROM match_teams WHERE source_message_id=? AND user_id=?",
            (int(match_id), int(user_id)),
        ).fetchone()
        return str(row[0]) if row else None

    def _saved_opponent(self, match_id, user_id, expected_team_name=None, exclude_team_names=()):
        """Find the opposing side without mistaking a transferred player's new team for its opponent."""
        try:
            rows = self.db.execute(
                "SELECT user_id, team_name FROM match_teams WHERE source_message_id = ?",
                (int(match_id),),
            ).fetchall()
        except Exception:
            return None
        saved_mine = next((str(r[1]) for r in rows if int(r[0]) == int(user_id)), None)
        own_names = {str(name).casefold() for name in exclude_team_names if name}
        if expected_team_name:
            own_names.add(str(expected_team_name).casefold())
        if saved_mine:
            own_names.add(saved_mine.casefold())
        counts = {}
        for row_user_id, team in rows:
            team = str(team)
            if int(row_user_id) == int(user_id) or team.casefold() in own_names:
                continue
            counts[team] = counts.get(team, 0) + 1
        return max(counts, key=lambda t: (counts[t], t)) if counts else None

    @staticmethod
    def _transfer_time(value):
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
        except (TypeError, ValueError, OverflowError):
            return None

    def _team_for_performance(self, row, user_id, competition, transfers):
        """Resolve the team at match time, preserving the Last Gate's pre-steal team."""
        saved = self._saved_player_team(row["source_message_id"], user_id)
        if not transfers:
            return saved
        # Use the raw-stat message snowflake time, not the import time: an old
        # match may be imported after a player has already been transferred.
        try:
            match_at = discord.utils.snowflake_time(
                int(row["source_message_id"])
            ).timestamp()
        except (TypeError, ValueError, OverflowError):
            match_at = float(row.get("imported_at", 0) or 0)
        expected = str(transfers[0]["old_team_name"])
        for transfer in transfers:
            transferred_at = self._transfer_time(transfer.get("transferred_at"))
            if transferred_at is None:
                continue
            if match_at >= transferred_at:
                expected = str(transfer["new_team_name"])
            else:
                break
        return expected or saved

    def _match_team_snapshot(self, guild_id, user_ids):
        """{user_id: team name} from the current approved rosters, for players on a team.

        Never raises: if the team data is unavailable the match is simply imported
        without a snapshot and the opponent falls back to the current rosters.
        """
        try:
            from .lb import _team_rosters  # lazy: lb imports this module
            wanted = {int(u) for u in user_ids}
            snapshot = {}
            for team in _team_rosters(int(guild_id)).values():
                for uid in team["members"]:
                    if uid in wanted:
                        snapshot[uid] = str(team["name"])
            return snapshot
        except Exception:
            return {}

    def get_raw_stats_report_channel_id(self, guild_id):
        row = self.db.execute(
            "SELECT channel_id FROM raw_stats_report_channels WHERE guild_id = ?",
            (int(guild_id),),
        ).fetchone()
        return int(row[0]) if row else None

    async def send_raw_stats_report(self, ctx, source, rows, catch_counts, catch_points, hours):
        """Post an audit report to the configured raw-stats channel."""
        channel_id = self.get_raw_stats_report_channel_id(ctx.guild.id)
        if not channel_id:
            return
        channel = ctx.guild.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return
        if not hasattr(channel, "send"):
            return

        # Match IDs are not standardized in raw-stat messages, so extract common forms.
        source_text = source.content or ""
        match = re.search(
            r"\bmatch\s*(?:id|no\.?|number)?\s*[:#-]?\s*#?([A-Za-z0-9_-]+)",
            source_text, re.IGNORECASE,
        )
        match_id = match.group(1) if match else "Not found in raw-stat message"
        source_link = f"https://discord.com/channels/{ctx.guild.id}/{source.channel.id}/{source.id}"

        catch_lines = []
        for user_id, values in sorted(catch_counts.items(), key=lambda item: item[0]):
            member = ctx.guild.get_member(int(user_id))
            name = member.display_name if member else f"User {user_id}"
            name = discord.utils.escape_markdown(name)
            catch_lines.append(
                f"• **{name}** — caught: `{values['taken']}`, dropped: `{values['dropped']}`, "
                f"fielding points: `{values['taken'] * 5 - values['dropped'] * 10:+}`"
            )
        if not catch_lines:
            catch_lines = ["No catch events detected in the selected time range."]

        point_lines = []
        for row in rows:
            user_id, runs, balls_faced, runs_conceded, balls_bowled, wickets, not_out = row
            player_row = {
                "user_id": user_id, "runs": runs, "balls_faced": balls_faced,
                "runs_conceded": runs_conceded, "balls_bowled": balls_bowled,
                "wickets": wickets, "not_out": bool(not_out),
            }
            _, _, without_fielding = fantasy_raw_points([player_row])
            fielding = catch_points.get(int(user_id), 0)
            with_fielding = without_fielding + fielding
            member = ctx.guild.get_member(int(user_id))
            name = member.display_name if member else f"User {user_id}"
            name = discord.utils.escape_markdown(name)
            point_lines.append(
                f"• **{name}** — without fielding: `{without_fielding:.2f}`, "
                f"with fielding: `{with_fielding:.2f}` (fielding `{fielding:+.2f}`)"
            )

        header = discord.Embed(
            title=f"📊 Raw Stats Report — Match {match_id}",
            color=discord.Color.from_rgb(55, 150, 255),
            timestamp=datetime.now(timezone.utc),
        )
        header.description = (
            f"**Added by:** {ctx.author.mention}\n"
            f"**Raw stats:** [Open original message]({source_link})\n"
            f"**Catch scan:** {hours} hour(s)\n" if hours else "**Catch scan:** skipped\n"
            f"**Imported players:** {len(rows)}\n"
            f"**Catch points:** +5 caught / -10 dropped"
        )
        await channel.send(embed=header, allowed_mentions=discord.AllowedMentions.none())

        # Discord embeds have field/description limits. Split each report section safely.
        async def send_section(title, lines):
            chunks, current = [], ""
            for line in lines:
                if len(current) + len(line) + 1 > 3800:
                    chunks.append(current)
                    current = ""
                current += ("\n" if current else "") + line
            if current:
                chunks.append(current)
            for index, chunk in enumerate(chunks, start=1):
                embed = discord.Embed(
                    title=title + (f" (part {index})" if len(chunks) > 1 else ""),
                    description=chunk[:4000],
                    color=discord.Color.from_rgb(55, 150, 255),
                )
                await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

        await send_section("<:gloves_ios:1564891803067613245> Catch Sync — Fielders", catch_lines)
        await send_section("🏏 Fantasy Points — All Imported Players", point_lines or ["No player rows found."])

    def get_selected_design(self, guild_id):
        row = self.db.execute(
            "SELECT design FROM stats_design_settings WHERE guild_id = ?",
            (guild_id,),
        ).fetchone()
        return int(row[0]) if row else 1

    def get_fielding_stats(self, guild_id, user_id, competition="MAIN TOURNAMENT"):
        """Return fielding totals for the selected competition."""
        if competition == "THE LAST GATE":
            try:
                row = self.db.execute(
                    """SELECT SUM(f.catches_taken), SUM(f.catches_dropped)
                       FROM player_match_fielding f
                       JOIN stats_imports i ON i.source_message_id=f.source_message_id
                       WHERE i.guild_id=? AND i.competition=? AND f.user_id=?""",
                    (int(guild_id), competition, int(user_id)),
                ).fetchone()
            except Exception:
                return 0, 0, 0
            caught, dropped = (int(row[0] or 0), int(row[1] or 0)) if row else (0, 0)
            return caught, dropped, caught * 5 - dropped * 10
        try:
            row = self.db.execute(
                "SELECT catches_caught, catches_dropped FROM fielding_stats WHERE guild_id=? AND user_id=?",
                (int(guild_id), int(user_id)),
            ).fetchone()
        except Exception:
            return 0, 0, 0
        caught, dropped = (int(row[0]), int(row[1])) if row else (0, 0)
        return caught, dropped, caught * 5 - dropped * 10

    def get_batter_drops(self, guild_id, user_id):
        """How many of this batter's catches were dropped (batter_drop_lb.drops)."""
        try:
            row = self.db.execute(
                "SELECT drops FROM batter_drop_lb WHERE guild_id=? AND user_id=?",
                (int(guild_id), int(user_id)),
            ).fetchone()
        except Exception:
            return 0
        return int(row[0]) if row else 0

    def get_batter_catches(self, guild_id, user_id, competition="MAIN TOURNAMENT"):
        """(catches_dropped, catches_taken) as a batter, scoped to a competition."""
        try:
            row = self.db.execute(
                "SELECT SUM(result='dropped'), SUM(result='taken') FROM batter_catch_events "
                "WHERE guild_id=? AND user_id=? AND competition=?",
                (int(guild_id), int(user_id), competition),
            ).fetchone()
        except Exception:
            # batcatches may not have created its table yet.
            return 0, 0
        return (int(row[0] or 0), int(row[1] or 0)) if row else (0, 0)

    def get_batter_impact(self, guild_id, user_id):
        """Return (runs_after_catch_drops, runs_without_catch_drops) for this server/player.

        Same table and columns the +lb "Batter Impact" category used to read
        (batter_drop_lb.runs_affected / runs_without_drops). No longer shown in +stats.
        """
        try:
            row = self.db.execute(
                "SELECT runs_affected, runs_without_drops FROM batter_drop_lb "
                "WHERE guild_id=? AND user_id=?",
                (int(guild_id), int(user_id)),
            ).fetchone()
        except Exception:
            # batfds may not have created its table yet.
            return 0, 0
        return (int(row[0]), int(row[1])) if row else (0, 0)

    async def find_member_by_name(self, guild, query):
        """Resolve a username, display name or nickname to a server member.

        Returns (member_or_None, candidates). member is None when nothing matched
        or when several members matched and none could be chosen safely.
        """
        needle = str(query or "").strip().lstrip("@").strip().casefold()
        if not needle:
            return None, []

        def names(m):
            raw = (m.name, getattr(m, "global_name", None), getattr(m, "nick", None), m.display_name)
            return {n.casefold() for n in raw if n}

        def search(members):
            exact_username = [m for m in members if m.name.casefold() == needle]
            if exact_username:
                return exact_username
            exact_display = [m for m in members if needle in names(m)]
            if exact_display:
                return exact_display
            if len(needle) >= 3:
                return [m for m in members if any(n.startswith(needle) for n in names(m))]
            return []

        found = search(list(guild.members))
        if not found:
            # The member cache can be incomplete; ask Discord for prefix matches.
            try:
                found = search(await guild.query_members(query=needle, limit=10))
            except Exception:
                found = []
        found = list({m.id: m for m in found}.values())
        if len(found) == 1:
            return found[0], found
        if len(found) > 1:
            # Prefer the one candidate who actually has imported stats.
            with_stats = [m for m in found if self.get_player_rows(guild.id, m.id)]
            if len(with_stats) == 1:
                return with_stats[0], found
        return None, found

    async def command_allowed(self, ctx, command_name):
        """Use the shared utility cog for command whitelist/lock checks."""
        utility = self.bot.get_cog("Utility")
        if utility is None:
            # Fail closed until the shared permission system is loaded.
            return False
        return await utility.command_allowed(ctx, command_name)

    async def replied_message(self, ctx):
        ref = ctx.message.reference
        if ref is None or ref.message_id is None:
            return None

        if isinstance(ref.resolved, discord.Message):
            return ref.resolved

        try:
            channel = (
                self.bot.get_channel(ref.channel_id)
                or await self.bot.fetch_channel(ref.channel_id)
            )
            return await channel.fetch_message(ref.message_id)
        except (
            discord.NotFound,
            discord.Forbidden,
            discord.HTTPException,
            AttributeError,
        ):
            return None

    def get_player_rows(self, guild_id, user_id, competition="MAIN TOURNAMENT"):
        records = self.db.execute(
            """
            SELECT p.source_message_id, p.user_id, p.runs, p.balls_faced,
                   p.runs_conceded, p.balls_bowled, p.wickets, p.not_out, i.imported_at
            FROM player_match_stats p
            JOIN stats_imports i
              ON i.source_message_id = p.source_message_id
            WHERE i.guild_id = ? AND p.user_id = ? AND i.competition = ?
            ORDER BY i.imported_at ASC, p.source_message_id ASC
            """,
            (guild_id, user_id, competition),
        ).fetchall()
        return [dict(row) for row in records]

    async def scan_recent_catches(self, ctx, hours):
        """Return per-player catch points from recent Hand Cricket messages only.

        This is read-only: it never updates the permanent fielding tracker database.
        """
        hand_cricket_bot_id = 753191385296928808
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        tracker = self.bot.get_cog("CatchTracker")
        counts = defaultdict(lambda: {"taken": 0, "dropped": 0})
        opportunities = defaultdict(int)
        pending_fielder_id = None

        async for message in ctx.channel.history(
            limit=None, after=cutoff, oldest_first=True
        ):
            if message.author.id != hand_cricket_bot_id:
                continue
            if tracker is not None:
                opportunity = tracker._parse_opportunity(message)
                if opportunity is not None:
                    pending_fielder_id = opportunity[0]
                    opportunities[pending_fielder_id] += 1
                    continue
                result = tracker._result_kind(message)
            else:
                # Fallback if the fielding cog is not loaded: the catch-result
                # text is enough, while opportunities still need a player mention.
                text = (message.content or "") + "\n" + "\n".join(
                    (e.title or "") + "\n" + (e.description or "")
                    for e in message.embeds
                )
                upper = text.upper()
                if "CHANCE OF WICKET!" in upper:
                    match = re.search(
                        r"coming\s+toward\s+<@!?(\d+)>|coming\s+toward\s+@?([A-Za-z0-9_.-]{2,32})",
                        text, re.IGNORECASE,
                    )
                    if match:
                        if match.group(1):
                            pending_fielder_id = int(match.group(1))
                        else:
                            name = match.group(2).casefold()
                            member = discord.utils.find(
                                lambda m: m.name.casefold() == name or m.display_name.casefold() == name,
                                ctx.guild.members,
                            )
                            pending_fielder_id = member.id if member else None
                        if pending_fielder_id is not None:
                            opportunities[pending_fielder_id] += 1
                    continue
                result = ("dropped" if "BOZO DROPPED THE CATCH" in upper
                          else "taken" if "TOOK THE CATCH" in upper else None)

            if result and pending_fielder_id is not None:
                counts[pending_fielder_id][result] += 1
                pending_fielder_id = None

        return {
            user_id: values["taken"] * 5 - values["dropped"] * 10
            for user_id, values in counts.items()
        }, counts, opportunities

    @commands.command(name="setrawstats")
    @commands.guild_only()
    async def set_raw_stats_channel(self, ctx, channel: discord.TextChannel):
        """Configure where +as import audit reports are sent."""
        if not await self.command_allowed(ctx, "setrawstats"):
            await ctx.reply("You are not allowed to use this command.", mention_author=False)
            return
        with self.db:
            self.db.execute(
                """
                INSERT INTO raw_stats_report_channels (guild_id, channel_id, configured_by, configured_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET
                    channel_id=excluded.channel_id,
                    configured_by=excluded.configured_by,
                    configured_at=excluded.configured_at
                """,
                (ctx.guild.id, channel.id, ctx.author.id, datetime.now(timezone.utc).timestamp()),
            )
        await ctx.reply(
            f"✅ Raw-stat reports will be sent to {channel.mention}.",
            mention_author=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.command(name="as", aliases=["addstats"])
    async def add_stats(self, ctx):
        if not await self.command_allowed(ctx, "as"):
            await ctx.reply(
                "You are not allowed to use this command.",
                mention_author=False,
            )
            return

        source = await self.replied_message(ctx)
        if (
            source is None
            or source.guild is None
            or source.guild.id != ctx.guild.id
        ):
            await ctx.reply(
                "Reply directly to the raw-statistics message with `+as`.",
                mention_author=False,
            )
            return

        try:
            rows = parse_raw_stats(source.content)
        except ValueError as exc:
            await ctx.reply(f"❌ {exc}", mention_author=False)
            return

        competition_view = CompetitionImportView(ctx.author.id)
        competition_message = await ctx.reply(
            "Choose the competition for this match's statistics:",
            view=competition_view, mention_author=False,
        )
        await competition_view.wait()
        if competition_view.choice is None:
            await competition_message.edit(
                content="⌛ Competition selection timed out. No stats were imported.", view=None
            )
            return
        competition = competition_view.choice

        prompt = CatchSyncView(ctx.author.id)
        prompt_message = await ctx.reply(
            "<:gloves_ios:1564891803067613245> Do you want to sync catch stats from this channel before calculating fantasy points?",
            view=prompt, mention_author=False,
        )
        await prompt.wait()
        if prompt.choice is None:
            await prompt_message.edit(
                content="⌛ Catch-sync prompt timed out. Run `+as` again.", view=None
            )
            return

        catch_points = {}
        catch_counts = {}
        catch_opportunities = {}
        hours = 0
        if prompt.choice:
            def valid_hours(message):
                return (
                    message.author.id == ctx.author.id
                    and message.channel.id == ctx.channel.id
                    and not message.author.bot
                )
            try:
                hours_message = await self.bot.wait_for(
                    "message", check=valid_hours, timeout=60
                )
                hours_text = hours_message.content.strip()
                if not re.fullmatch(r"[1-9]\d{0,3}", hours_text):
                    await ctx.send("❌ Enter a whole number of hours from `1` to `9999`. Run `+as` again.")
                    return
                hours = int(hours_text)
            except asyncio.TimeoutError:
                await ctx.send("⌛ Hours prompt timed out. Run `+as` again.")
                return
            try:
                catch_points, catch_counts, catch_opportunities = await self.scan_recent_catches(ctx, hours)
            except (discord.Forbidden, discord.HTTPException) as exc:
                await ctx.send(
                    f"❌ I couldn't read this channel's message history ({type(exc).__name__}).\n"
                    "Give the bot Read Message History permission and try again."
                )
                return

        team_snapshot = self._match_team_snapshot(ctx.guild.id, [row[0] for row in rows])

        try:
            with self.db:
                self.db.execute(
                    """
                    INSERT INTO stats_imports
                        (source_message_id, guild_id, imported_by, imported_at, competition)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        source.id,
                        ctx.guild.id,
                        ctx.author.id,
                        datetime.now(timezone.utc).timestamp(),
                        competition,
                    ),
                )
                self.db.executemany(
                    """
                    INSERT INTO player_match_stats
                        (source_message_id, user_id, runs, balls_faced,
                         runs_conceded, balls_bowled, wickets, not_out)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [(source.id, *row) for row in rows],
                )
                self.db.executemany(
                    """INSERT INTO player_match_fielding
                       (source_message_id, user_id, opportunities, catches_taken, catches_dropped)
                       VALUES (?, ?, ?, ?, ?)""",
                    [(source.id, int(uid), int(catch_opportunities.get(uid, 0)),
                      int(catch_counts.get(uid, {}).get("taken", 0)),
                      int(catch_counts.get(uid, {}).get("dropped", 0)))
                     for uid in set(catch_opportunities) | set(catch_counts)]
                )
                if team_snapshot:
                    self.db.executemany(
                        "INSERT OR REPLACE INTO match_teams (source_message_id, user_id, team_name) VALUES (?, ?, ?)",
                        [(source.id, int(uid), name) for uid, name in team_snapshot.items()],
                    )
                self.db.execute(
                    "CREATE TABLE IF NOT EXISTS lb_stats_revision "
                    "(guild_id INTEGER PRIMARY KEY, revision INTEGER NOT NULL DEFAULT 0)"
                )
                self.db.execute(
                    "INSERT INTO lb_stats_revision (guild_id, revision) VALUES (?, 1) "
                    "ON CONFLICT(guild_id) DO UPDATE SET revision = revision + 1",
                    (ctx.guild.id,),
                )
        except Exception as exc:
            if (
                "UNIQUE constraint failed" in str(exc)
                or "PRIMARY KEY" in str(exc)
            ):
                await ctx.send("❌ These raw statistics have already been imported.")
            else:
                await ctx.send(
                    f"❌ Import failed; no rows were saved. Error: `{type(exc).__name__}`"
                )
            return

        try:
            await self.send_raw_stats_report(ctx, source, rows, catch_counts, catch_points, hours)
        except (discord.Forbidden, discord.HTTPException) as exc:
            await ctx.send(
                f"⚠️ Stats imported, but the raw-stats report could not be sent ({type(exc).__name__}). Check the configured channel permissions."
            )

        player_points = []
        for row in rows:
            user_id, runs, balls_faced, runs_conceded, balls_bowled, wickets, not_out = row
            player_row = {
                "user_id": user_id, "runs": runs, "balls_faced": balls_faced,
                "runs_conceded": runs_conceded, "balls_bowled": balls_bowled,
                "wickets": wickets, "not_out": bool(not_out),
            }
            _, _, base_points = fantasy_raw_points([player_row])
            points = base_points + catch_points.get(user_id, 0)
            player_points.append((user_id, points))

        highest_points = max(points for _, points in player_points)
        top_players = [
            (user_id, points) for user_id, points in player_points
            if abs(points - highest_points) < 1e-9
        ]
        top_names = []
        for user_id, points in top_players:
            member = ctx.guild.get_member(user_id)
            player_name = member.mention if member else f"<@{user_id}>"
            top_names.append(f"{player_name} (**{points:.2f} points**)")

        catch_summary = ""
        if prompt.choice:
            caught_total = sum(v["taken"] for v in catch_counts.values())
            dropped_total = sum(v["dropped"] for v in catch_counts.values())
            catch_summary = (
                f"\n<:gloves_ios:1564891803067613245> **Catch sync ({hours}h):** `{caught_total}` taken (+{caught_total * 5}), "
                f"`{dropped_total}` dropped (-{dropped_total * 10})."
            )
        await ctx.send(
            f"✅ Imported **{competition}** statistics for **{len(rows)} players**.\n"
            f"Match record: `{source.id}`\n"
            f"🏆 **Highest fantasy points:** {', '.join(top_names)}"
            f"{catch_summary}",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.command(name="rs", aliases=["removestats"])
    async def remove_stats(self, ctx):
        if not await self.command_allowed(ctx, "rs"):
            await ctx.reply(
                "You are not allowed to use this command.",
                mention_author=False,
            )
            return

        source = await self.replied_message(ctx)
        if (
            source is None
            or source.guild is None
            or source.guild.id != ctx.guild.id
        ):
            await ctx.reply(
                "Reply to the original raw-statistics message with `+rs`.",
                mention_author=False,
            )
            return

        record = self.db.execute(
            """
            SELECT source_message_id
            FROM stats_imports
            WHERE source_message_id = ? AND guild_id = ?
            """,
            (source.id, ctx.guild.id),
        ).fetchone()

        if record is None:
            await ctx.reply(
                "❌ That message has not been imported, or it was already removed.",
                mention_author=False,
            )
            return

        with self.db:
            self.db.execute(
                "DELETE FROM player_match_stats WHERE source_message_id = ?",
                (source.id,),
            )
            self.db.execute(
                "DELETE FROM player_match_fielding WHERE source_message_id = ?",
                (source.id,),
            )
            self.db.execute(
                "DELETE FROM match_teams WHERE source_message_id = ?",
                (source.id,),
            )
            self.db.execute(
                "DELETE FROM stats_imports WHERE source_message_id = ?",
                (source.id,),
            )
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS lb_stats_revision "
                "(guild_id INTEGER PRIMARY KEY, revision INTEGER NOT NULL DEFAULT 0)"
            )
            self.db.execute(
                "INSERT INTO lb_stats_revision (guild_id, revision) VALUES (?, 1) "
                "ON CONFLICT(guild_id) DO UPDATE SET revision = revision + 1",
                (ctx.guild.id,),
            )

        await ctx.reply(
            "✅ Removed that match's imported statistics. "
            "Other match records remain unchanged.",
            mention_author=False,
        )
        report_channel_id = self.get_raw_stats_report_channel_id(ctx.guild.id)
        report_channel = ctx.guild.get_channel(report_channel_id) if report_channel_id else None
        if report_channel is not None:
            source_link = f"https://discord.com/channels/{ctx.guild.id}/{source.channel.id}/{source.id}"
            audit = discord.Embed(
                title="🗑️ Raw Stats Removal Report",
                description=(f"**Removed by:** {discord.utils.escape_markdown(ctx.author.display_name)} (`{ctx.author.id}`)\n"
                             f"**Original raw stats:** [Open original message]({source_link})\n"
                             f"**Match record ID:** `{source.id}`"),
                color=discord.Color.red(),
                timestamp=datetime.now(timezone.utc),
            )
            try:
                await report_channel.send(embed=audit, allowed_mentions=discord.AllowedMentions.none())
            except (discord.Forbidden, discord.HTTPException):
                pass

    @commands.command(name="resetstats")
    async def reset_server_stats(self, ctx):
        """Reset imported stats for this server; restricted to the bot owner."""
        BOT_OWNER_ID = 1014568928682459198

        if not await owner_or_whitelisted(ctx):
            await ctx.reply(
                "❌ Only the bot owner or a role whitelisted for this command can use it.",
                mention_author=False,
            )
            return

        if ctx.guild is None:
            await ctx.reply(
                "❌ Use this command inside the server whose stats you want to reset.",
                mention_author=False,
            )
            return

        view = ResetServerStatsView(self, ctx.guild.id, ctx.author.id)
        await ctx.reply(
            "⚠️ **Confirm server stats reset**\n"
            "This permanently deletes imported match statistics for BOTH **KASHMIR TITANS S1** "
            "and **THE LAST GATE** in this server. It does not preserve either competition's stats. "
            "Other servers and saved design settings are not affected.\n\n"
            "Press **Confirm reset** to continue, or **Cancel** to keep the stats.",
            view=view,
            mention_author=False,
        )

    # New administrator-only design selector.
    @commands.command(name="setdesign")
    async def set_design(self, ctx, design_number: str = None):
        if not await self.command_allowed(ctx, "setdesign"):
            await ctx.reply(
                "You are not allowed to use this command.",
                mention_author=False,
            )
            return

        if design_number not in ("1", "2", "3"):
            await ctx.reply(
                "Invalid design number. Use `+setdesign 1`, `+setdesign 2`, or `+setdesign 3`.",
                mention_author=False,
            )
            return

        design = int(design_number)

        # Validate Design 2's background BEFORE changing the saved setting.
        if design == 2:
            background_path = Path(__file__).resolve().with_name(
                "stats_design2_bg.png"
            )

            if not background_path.is_file():
                await ctx.reply(
                    "❌ Design 2 background is missing. Place "
                    "`stats_design2_bg.png` in the same folder as "
                    "`cogs/stats.py`. The saved design was not changed.",
                    mention_author=False,
                )
                return

            try:
                with Image.open(background_path) as background:
                    background.verify()
            except Exception:
                await ctx.reply(
                    "❌ `stats_design2_bg.png` exists but is not a "
                    "readable image. The saved design was not changed.",
                    mention_author=False,
                )
                return

        # Store the selection independently for each guild.
        with self.db:
            self.db.execute(
                """
                INSERT INTO stats_design_settings (guild_id, design)
                VALUES (?, ?)
                ON CONFLICT(guild_id)
                DO UPDATE SET design = excluded.design
                """,
                (ctx.guild.id, design),
            )

        design_names = {1: "Original", 2: "Blue Background", 3: "Tiered Player Card"}
        await ctx.reply(
            f"✅ Stats design for this server is now **Design {design} — {design_names[design]}**.",
            mention_author=False,
        )

    @commands.command(name="stats")
    async def player_stats(self, ctx, *, user_id: str = None):
        # Optional suffix: +stats @user | THE LAST GATE
        competition = "MAIN TOURNAMENT"
        if user_id and "|" in user_id:
            user_id, requested_competition = user_id.rsplit("|", 1)
            requested_competition = requested_competition.strip().upper()
            if requested_competition in ("LAST GATE", "THE LAST GATE"):
                competition = "THE LAST GATE"
            elif requested_competition not in ("MAIN", "MAIN TOURNAMENT"):
                await ctx.reply("Use MAIN TOURNAMENT or THE LAST GATE after the |.", mention_author=False)
                return
        # Accept a server member, a mention, a numeric Discord user ID,
        # or a username / display name / nickname.
        if not user_id:
            member = ctx.author
        else:
            cleaned_id = user_id.strip().strip("<@!>")
            if not cleaned_id.isdigit():
                member, candidates = await self.find_member_by_name(ctx.guild, user_id)
                if member is None:
                    shown = discord.utils.escape_markdown(discord.utils.escape_mentions(user_id.strip()))
                    if candidates:
                        options = ", ".join(
                            f"`{discord.utils.escape_markdown(m.name)}` ({m.id})" for m in candidates[:5]
                        )
                        message = (
                            f"More than one member matches **{shown}**: {options}. "
                            "Use a mention or user ID instead."
                        )
                    else:
                        message = (
                            f"I couldn't find a member matching **{shown}**. "
                            "Use `+stats`, `+stats @user`, `+stats USER_ID`, or `+stats username`."
                        )
                    await ctx.reply(
                        message,
                        mention_author=False,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    return
            else:
                target_id = int(cleaned_id)
                member = ctx.guild.get_member(target_id)
                if member is None:
                    user = self.bot.get_user(target_id)
                    if user is None:
                        try:
                            user = await self.bot.fetch_user(target_id)
                        except (discord.NotFound, discord.HTTPException):
                            user = None
                    # A departed user may no longer be a Guild Member. Keep the
                    # same rendering interface while using their stored ID/stats.
                    member = SimpleNamespace(
                        id=target_id,
                        name=getattr(user, "name", f"User {target_id}"),
                        display_name=getattr(user, "name", f"User {target_id}"),
                        mention=f"<@{target_id}>",
                        display_avatar=getattr(user, "display_avatar", None),
                        guild=ctx.guild,
                    )

        rows = self.get_player_rows(ctx.guild.id, member.id, competition)
        if not rows and competition == "MAIN TOURNAMENT":
            last_gate_rows = self.get_player_rows(ctx.guild.id, member.id, "THE LAST GATE")
            if last_gate_rows:
                competition = "THE LAST GATE"
                rows = last_gate_rows
        if not rows:
            message = (
                f"No imported stats found for {member.mention} in **{competition}** yet. "
                "Import a match with +as first."
            )
            pending_switches = getattr(self, "_stats_switch_interactions", {})
            switch_interaction = pending_switches.pop(int(ctx.message.id), None)
            if switch_interaction is not None:
                await switch_interaction.edit_original_response(
                    content=message, embed=None, attachments=[], view=None,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            else:
                await ctx.reply(message, mention_author=False)
            return

        avatar = guild_icon = None

        try:
            if member.display_avatar is not None:
                avatar = await member.display_avatar.replace(
                    size=256, static_format="png"
                ).read()
        except Exception:
            pass

        try:
            if ctx.guild.icon:
                guild_icon = await ctx.guild.icon.replace(
                    size=128, static_format="png"
                ).read()
        except Exception:
            pass

        design = self.get_selected_design(ctx.guild.id)
        catches_caught, catches_dropped, fielding_points = self.get_fielding_stats(
            ctx.guild.id, member.id, competition
        )
        batter_drops, batter_taken = self.get_batter_catches(ctx.guild.id, member.id, competition)

        try:
            if design == 3:
                recent_match_cards = []
                for recent_row in reversed(rows[-5:]):
                    recent_match_cards.append({
                        "opponent": self._saved_opponent(
                            recent_row["source_message_id"], member.id
                        ),
                        "runs": recent_row["runs"],
                        "balls_faced": recent_row["balls_faced"],
                        "not_out": recent_row["not_out"],
                        "wickets": recent_row["wickets"],
                        "runs_conceded": recent_row["runs_conceded"],
                        "balls_bowled": recent_row["balls_bowled"],
                    })
                image = await asyncio.to_thread(
                    make_stats_image_design3,
                    member,
                    rows,
                    avatar=avatar,
                    fielding_points=fielding_points,
                    catches_caught=catches_caught,
                    catches_dropped=catches_dropped,
                    batter_drops=batter_drops,
                    batter_taken=batter_taken,
                    recent_matches=recent_match_cards,
                )
            elif design == 2:
                # Pillow rendering is CPU-bound; keep it off Discord's event loop.
                image = await asyncio.to_thread(
                    make_stats_image_design2,
                    member,
                    rows,
                    avatar=avatar,
                    guild_icon=guild_icon,
                    fielding_points=fielding_points,
                    catches_caught=catches_caught,
                    catches_dropped=catches_dropped,
                    batter_drops=batter_drops,
                    batter_taken=batter_taken,
                )
            else:
                # Design 1 continues to use the original renderer.
                image = await asyncio.to_thread(
                    make_stats_image,
                    member,
                    rows,
                    guild_icon=guild_icon,
                    avatar=avatar,
                )
        except FileNotFoundError:
            await ctx.reply(
                "❌ Design 2 background is missing. Ask a server "
                "administrator to upload `stats_design2_bg.png` "
                "beside `cogs/stats.py`.",
                mention_author=False,
            )
            return
        except Exception as exc:
            await ctx.reply(
                f"❌ Could not generate the stats image "
                f"({type(exc).__name__}).",
                mention_author=False,
            )
            return

        image.seek(0)
        overall_image = image.getvalue()

        t = totals(rows)
        batting_points, bowling_points, total_points = fantasy_raw_points(rows, t)
        total_points += fielding_points
        transfers = self.get_player_transfers(ctx.guild.id, member.id)
        last_gate_rows = self.get_player_rows(ctx.guild.id, member.id, "THE LAST GATE")
        last_gate_team_names = []
        for last_gate_row in last_gate_rows:
            represented_team = self._team_for_performance(
                last_gate_row, member.id, "THE LAST GATE", transfers
            )
            if represented_team and represented_team not in last_gate_team_names:
                last_gate_team_names.append(represented_team)
        last_gate_team_name = " / ".join(last_gate_team_names) or None
        fifties = sum(50 <= r["runs"] < 100 for r in rows)
        hundreds = sum(r["runs"] >= 100 for r in rows)
        ducks = sum(r["runs"] == 0 and not r["not_out"] for r in rows)
        highest = max((r["runs"] for r in rows), default=0)
        bowling_innings = sum(r["balls_bowled"] > 0 for r in rows)
        overs_whole = t["balls_bowled"] // 6
        overs_remainder = t["balls_bowled"] % 6
        overs_text = f"{overs_whole}.{overs_remainder}"
        _best_bowl = max(
            (r for r in rows if r["balls_bowled"] > 0),
            key=lambda r: (r["wickets"], -r["runs_conceded"]),
            default=None,
        )
        overview_best_bowling = f"{_best_bowl['wickets']}/{_best_bowl['runs_conceded']}" if _best_bowl else "0/0"
        embed = discord.Embed(
            title=f"{member.display_name} stats for `KASHMIR TITANS S1` • {competition}",
            description=f"**MATCHES RECORDED:** `{t['matches']}`",
            colour=discord.Colour.blurple(),
        )
        if member.display_avatar is not None:
            embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(
            name="<:cricketbat:1561294474951393360> **__BATTING__**",
            value=(
                f"**Innings:** `{t['matches']}`\n"
                f"**Runs:** `{t['runs']}`\n"
                f"**Average:** `{t['bat_avg']:.2f}`\n"
                f"**50s:** `{fifties}`\n"
                f"**100s:** `{hundreds}`\n"
                f"**Highest score:** `{highest}`\n"
                f"**Ducks:** `{ducks}`\n"
                f"**Fantasy points:** `{batting_points:.2f}`"
            ),
            inline=True,
        )
        embed.add_field(
            name="<:cricketball:1561294577392951338> **__BOWLING__**",
            value=(
                f"**Innings:** `{bowling_innings}`\n"
                f"**Overs bowled:** `{overs_text}`\n"
                f"**Wickets:** `{t['wickets']}`\n"
                f"**Average:** `{t['bowl_avg']:.2f}`\n"
                f"**3W hauls:** `{sum(3 <= r['wickets'] < 5 for r in rows)}`\n"
                f"**5W hauls:** `{sum(5 <= r['wickets'] < 7 for r in rows)}`\n"
                f"**Economy:** `{t['economy']:.2f}`\n"
                f"**BBF:** `{overview_best_bowling}`\n"
                f"**Fantasy points:** `{bowling_points:.2f}`"
            ),
            inline=True,
        )
        embed.add_field(
            name="<:gloves_ios:1564891803067613245> **__FIELDING__**",
            value=(
                f"**Catches caught:** `{catches_caught}`\n"
                f"**Catches dropped:** `{catches_dropped}`\n"
                f"**Fantasy points:** `{fielding_points:.2f}`"
            ),
            inline=True,
        )
        embed.add_field(name="<:Crown:1561296003104243803> **__TOTAL FANTASY POINTS__**", value=f"**`{total_points:.2f}`**", inline=False)
        if last_gate_team_name:
            embed.add_field(
                name="🚪 **__THE LAST GATE TEAM__**",
                value=f"**{discord.utils.escape_markdown(last_gate_team_name)}** (team(s) represented in Last Gate matches)",
                inline=False,
            )

        # Extended stats use the same recorded match rows and existing fantasy formula.
        best_batting = max(rows, key=lambda r: (r["runs"], r["balls_faced"]), default=None)
        bowling_rows = [r for r in rows if r["balls_bowled"] > 0]
        best_bowling = max(
            bowling_rows,
            key=lambda r: (r["wickets"], -r["runs_conceded"]),
            default=None,
        )
        recent_rows = list(reversed(rows[-5:]))
        recent_lines = []
        recent_team_names = [
            self._team_for_performance(row, member.id, competition, transfers)
            for row in recent_rows
        ]
        # Recent rows are newest-first. A divider is shown only at a real
        # team change between adjacent saved match snapshots, not merely because
        # a team appears anywhere in the player's transfer history.
        transfer_pairs = {
            (str(item["old_team_name"]).casefold(), str(item["new_team_name"]).casefold()):
            str(item["new_team_name"])
            for item in transfers
        }
        for index, row in enumerate(recent_rows, start=1):
            team_name = recent_team_names[index - 1]
            older_team = recent_team_names[index] if index < len(recent_team_names) else None
            pair = ((older_team or "").casefold(), (team_name or "").casefold())
            if team_name and older_team and pair in transfer_pairs and pair[0] != pair[1]:
                recent_lines.append(
                    f"**-- {discord.utils.escape_markdown(transfer_pairs[pair])} --**"
                )
            status = "*" if row["not_out"] else ""
            fielding_row = self.db.execute(
                """SELECT f.opportunities, f.catches_taken, f.catches_dropped
                   FROM player_match_fielding f
                   JOIN stats_imports i ON i.source_message_id=f.source_message_id
                   WHERE f.source_message_id=? AND f.user_id=? AND i.competition=?""",
                (row["source_message_id"], member.id, competition),
            ).fetchone()
            if fielding_row and int(fielding_row[0]) > 0:
                fielding_text = f"<:gloves_ios:1564891803067613245> `{fielding_row[1]}/{fielding_row[2]}`"
            else:
                fielding_text = "<:gloves_ios:1564891803067613245> `DNF`"
            batting_text = (
                f"{row['runs']}{status} ({row['balls_faced']})"
                if int(row["balls_faced"]) > 0 else "DNB"
            )
            bowling_text = (
                f"{row['wickets']}/{row['runs_conceded']}"
                if int(row["balls_bowled"]) > 0 else "DNB"
            )
            saved_team = self._saved_player_team(row["source_message_id"], member.id)
            opponent = self._saved_opponent(
                row["source_message_id"], member.id,
                expected_team_name=team_name,
                exclude_team_names=([saved_team] if saved_team else []),
            )
            versus = f"vs **{discord.utils.escape_markdown(opponent)}**\n　" if opponent else ""
            recent_lines.append(
                f"**{index}.** {versus}<:cricketbat:1561294474951393360> `{batting_text}`  ·  "
                f"<:cricketball:1561294577392951338> `{bowling_text}`  ·  {fielding_text}"
            )
        recent_text = "\n".join(recent_lines) if recent_lines else "No recent match records."
        if recent_rows:
            # Recent form: the same last-5 matches rolled up into one line each for batting and bowling.
            form = totals(recent_rows)
            form_bat = (
                f"`{form['runs']}` runs • avg `{form['bat_avg']:.2f}` • SR `{form['strike_rate']:.1f}`"
                if form["balls_faced"] > 0 else "`Did not bat`"
            )
            form_bowl = (
                f"`{form['wickets']}` wkts • econ `{form['economy']:.2f}`"
                if form["balls_bowled"] > 0 else "`Did not bowl`"
            )
            recent_text += (
                f"\n\n**Form (last {len(recent_rows)}):**\n"
                f"<:cricketbat:1561294474951393360> {form_bat}\n"
                f"<:cricketball:1561294577392951338> {form_bowl}"
            )
        best_batting_text = (
            f"{best_batting['runs']} ({best_batting['balls_faced']})"
            f"{'*' if best_batting['not_out'] else ''}"
            if best_batting else "0 (0)"
        )
        best_bowling_text = (
            f"{best_bowling['wickets']}/{best_bowling['runs_conceded']}"
            if best_bowling else "0/0"
        )
        deep_embed = discord.Embed(
            title=f"{member.display_name} stats for `KASHMIR TITANS S1` • {competition} — Deep Stats",
            description=(
                f"**MATCHES RECORDED:** `{t['matches']}`\n"
                + (f"🚪 **THE LAST GATE TEAM(S):** **{discord.utils.escape_markdown(last_gate_team_name)}**\n" if last_gate_team_name else "")
                + f"\n<:recent:1561311937843957801>  **__RECENT 5 MATCH SCORES__**\n{recent_text}"
            ),
            colour=discord.Colour.blurple(),
        )
        if member.display_avatar is not None:
            deep_embed.set_thumbnail(url=member.display_avatar.url)
        deep_embed.add_field(
            name="<:cricketbat:1561294474951393360> **__BATTING__**",
            value=(
                f"**Innings:** `{t['matches']}`\n"
                f"**Runs:** `{t['runs']}`\n"
                f"**Catches dropped (as batter):** `{batter_drops}`\n"
                f"**Catches taken (as batter):** `{batter_taken}`\n"
                f"**Balls played:** `{t['balls_faced']}`\n"
                f"**Strike rate:** `{t['strike_rate']:.2f}`\n"
                f"**Average:** `{t['bat_avg']:.2f}`\n"
                f"**50s:** `{fifties}`\n"
                f"**100s:** `{hundreds}`\n"
                f"**Not outs:** `{t['not_outs']}`\n"
                f"**Best batting figures:** `{best_batting_text}`\n"
                f"**Fantasy points:** `{batting_points:.2f}`"
            ),
            inline=True,
        )
        deep_embed.add_field(
            name="<:cricketball:1561294577392951338> **__BOWLING__**",
            value=(
                f"**Innings:** `{bowling_innings}`\n"
                f"**Wickets:** `{t['wickets']}`\n"
                f"**Balls bowled:** `{t['balls_bowled']}`\n"
                f"**Average:** `{t['bowl_avg']:.2f}`\n"
                f"**Strike rate:** `{t['bowl_sr']:.2f}`\n"
                f"**Economy:** `{t['economy']:.2f}`\n"
                f"**3W hauls:** `{sum(3 <= r['wickets'] < 5 for r in rows)}`\n"
                f"**5W hauls:** `{sum(5 <= r['wickets'] < 7 for r in rows)}`\n"
                f"**Runs conceded:** `{t['runs_conceded']}`\n"
                f"**Best bowling figures:** `{best_bowling_text}`\n"
                f"**Fantasy points:** `{bowling_points:.2f}`"
            ),
            inline=True,
        )
        deep_embed.add_field(
            name="<:gloves_ios:1564891803067613245> **__FIELDING__**",
            value=(
                f"**Catches caught:** `{catches_caught}`\n"
                f"**Catches dropped:** `{catches_dropped}`\n"
                f"**Fantasy points:** `{fielding_points:.2f}`"
            ),
            inline=True,
        )
        deep_embed.add_field(
            name="<:Crown:1561296003104243803> **__TOTAL FANTASY POINTS__**",
            value=f"**`{total_points:.2f}`**",
            inline=False,
        )
        if transfers:
            transfer_lines = [
                f"**{discord.utils.escape_markdown(item['old_team_name'])} → "
                f"{discord.utils.escape_markdown(item['new_team_name'])}**"
                for item in transfers
            ]
            transfer_text = "\n".join(transfer_lines)
            embed.add_field(name="TEAM TRANSFER HISTORY", value=transfer_text, inline=False)
            deep_embed.add_field(name="TEAM TRANSFER HISTORY", value=transfer_text, inline=False)

        has_last_gate = self.db.execute(
            """SELECT 1 FROM stats_imports i
               JOIN player_match_stats p ON p.source_message_id=i.source_message_id
               WHERE i.guild_id=? AND i.competition='THE LAST GATE' AND p.user_id=? LIMIT 1""",
            (ctx.guild.id, member.id),
        ).fetchone() is not None
        view = StatsView(
            ctx.author.id, member.id, overall_image, embed, deep_embed,
            cog=self, ctx=ctx, has_last_gate=has_last_gate,
        )

        # First response is a stats overview embed; the dropdown switches between Overview, Deep Stats, and Image.
        pending_switches = getattr(self, "_stats_switch_interactions", {})
        switch_interaction = pending_switches.pop(int(ctx.message.id), None)
        if switch_interaction is not None:
            await switch_interaction.edit_original_response(
                embed=embed,
                view=view,
                attachments=[],
                allowed_mentions=discord.AllowedMentions.none(),
            )
        else:
            await ctx.send(
                embed=embed,
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )


async def setup(bot):
    await bot.add_cog(Stats(bot))
