"""Kashmir Titans interactive leaderboard command for GOOGLI MANAGER.

Uses the existing stats_imports/player_match_stats tables. Does not modify stats.py.
Requires discord.py and Pillow (already used by the stats cog).
"""
from __future__ import annotations

import io
import math
import sqlite3
from collections import defaultdict
from typing import Any
from pathlib import Path

import discord
from discord.ext import commands
from PIL import Image, ImageDraw, ImageFont, ImageFilter

# All available categories and statistics. Keep the display labels user-friendly.
METRICS = {
    "Batting": [
        ("runs", "Runs"),
        ("bat_avg", "Batting Average"),
        ("fifties", "Most 50s"),
        ("hundreds", "Most 100s"),
        ("ducks", "Most Ducks"),
        ("highest", "Highest Score"),
    ],
    "Bowling": [
        ("wickets", "Wickets"),
        ("bowl_avg", "Bowling Average"),
        ("economy", "Economy"),
        ("best_figures", "Best Bowling Figures"),
        ("bowl_sr", "Bowling Strike Rate"),
    ],
}

NAVY = (3, 12, 29)
PANEL = (5, 24, 48)
BLUE = (20, 117, 255)
ICE = (65, 199, 255)
GOLD = (255, 190, 61)
WHITE = (238, 246, 255)
MUTED = (151, 185, 217)


def _font(size: int, bold: bool = False):
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


def _fmt(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.2f}".rstrip("0").rstrip(".") or "0"
    return str(value)


def _metric_value(key: str, rows: list[dict]) -> float | int | tuple:
    runs = sum(r["runs"] for r in rows)
    balls_faced = sum(r["balls_faced"] for r in rows)
    conceded = sum(r["runs_conceded"] for r in rows)
    balls_bowled = sum(r["balls_bowled"] for r in rows)
    wickets = sum(r["wickets"] for r in rows)
    dismissals = sum(1 for r in rows if not r["not_out"])
    if key == "runs":
        return runs
    if key == "bat_avg":
        return runs / dismissals if dismissals else 0
    if key == "fifties":
        return sum(1 for r in rows if 50 <= r["runs"] < 100)
    if key == "hundreds":
        return sum(1 for r in rows if r["runs"] >= 100)
    if key == "ducks":
        return sum(1 for r in rows if r["runs"] == 0 and not r["not_out"])
    if key == "highest":
        return max((r["runs"] for r in rows), default=0)
    if key == "wickets":
        return wickets
    if key == "bowl_avg":
        return conceded / wickets if wickets else 0
    if key == "economy":
        return conceded * 6 / balls_bowled if balls_bowled else 0
    if key == "bowl_sr":
        return balls_bowled / wickets if wickets else 0
    if key == "best_figures":
        # Most wickets in an imported match, then fewest runs conceded.
        best = max(((r["wickets"], -r["runs_conceded"]) for r in rows), default=(0, 0))
        return (best[0], -best[1])
    return 0


def _display_value(key: str, value: Any) -> str:
    if key == "best_figures":
        return f"{value[0]}/{value[1]}"
    return _fmt(value)


def _sort_value(key: str, value: Any):
    # Ducks and bowling average are best when lower; all other lists are descending.
    if key in ("ducks", "bowl_avg"):
        return (value == 0, value)
    if key == "best_figures":
        return (-value[0], value[1])
    return (-value,)


def _make_trophy(draw: ImageDraw.ImageDraw, cx: int, cy: int, scale: float = 1.0):
    """Draw an original trophy silhouette into the background."""
    gold = (48, 102, 158)
    glow = (15, 76, 142)
    # Side handles
    draw.arc((cx-175*scale, cy-145*scale, cx-40*scale, cy+40*scale), 65, 295, fill=glow, width=max(4, int(18*scale)))
    draw.arc((cx+40*scale, cy-145*scale, cx+175*scale, cy+40*scale), 245, 115, fill=glow, width=max(4, int(18*scale)))
    cup = [(cx-100*scale, cy-155*scale), (cx+100*scale, cy-155*scale),
           (cx+78*scale, cy-25*scale), (cx+35*scale, cy+22*scale),
           (cx-35*scale, cy+22*scale), (cx-78*scale, cy-25*scale)]
    draw.polygon(cup, fill=(7, 42, 82), outline=(38, 148, 255), width=max(2, int(5*scale)))
    draw.line((cx, cy-145*scale, cx, cy+10*scale), fill=gold, width=max(2, int(4*scale)))
    draw.rectangle((cx-25*scale, cy+22*scale, cx+25*scale, cy+50*scale), fill=(14, 76, 132))
    draw.rectangle((cx-65*scale, cy+50*scale, cx+65*scale, cy+67*scale), fill=(16, 91, 161), outline=(55, 179, 255), width=max(1, int(2*scale)))
    draw.text((cx, cy-75*scale), "KT", font=_font(max(18, int(42*scale)), True), fill=(73, 174, 255), anchor="mm")


def _background(width: int, height: int) -> Image.Image:
    """Load the supplied clean blue leaderboard template without the old design."""
    asset = Path(__file__).with_name("kt_lb_background.png")
    if not asset.exists():
        raise FileNotFoundError(
            "kt_lb_background.png is missing. Keep the supplied background image beside lb.py."
        )
    return Image.open(asset).convert("RGBA").resize((width, height), Image.Resampling.LANCZOS)


async def _avatar_bytes(member: discord.abc.User) -> bytes | None:
    try:
        return await member.display_avatar.replace(size=128).read()
    except Exception:
        return None


def _paste_avatar(im: Image.Image, avatar_data: bytes | None, x: int, y: int, size: int, fallback: str):
    d = ImageDraw.Draw(im)
    if avatar_data:
        try:
            avatar = Image.open(io.BytesIO(avatar_data)).convert("RGBA").resize((size, size), Image.Resampling.LANCZOS)
            mask = Image.new("L", (size, size), 0)
            ImageDraw.Draw(mask).ellipse((1, 1, size-2, size-2), fill=255)
            # blue outer ring
            d.ellipse((x-3, y-3, x+size+3, y+size+3), fill=ICE)
            im.paste(avatar, (x, y), mask)
            return
        except Exception:
            pass
    d.ellipse((x-3, y-3, x+size+3, y+size+3), fill=ICE)
    d.ellipse((x, y, x+size, y+size), fill=(11, 55, 96))
    d.text((x+size//2, y+size//2), fallback[:2].upper(), font=_font(size//3, True), fill=WHITE, anchor="mm")


async def make_leaderboard_image(guild: discord.Guild, rows_by_user: dict[int, list[dict]], metric_key: str, category: str, metric_label: str, requester_id: int) -> bytes:
    width, height = 1024, 1536
    im = _background(width, height)
    d = ImageDraw.Draw(im)

    # Use the supplied blue template as-is. Draw only live leaderboard text and avatars.
    d.text((width // 2, 128), "LEADERBOARD", font=_font(58, True), fill=WHITE,
           anchor="mm", stroke_width=1, stroke_fill=(0, 35, 90))
    d.text((width // 2, 184), f"{category.upper()}  -  {metric_label.upper()}",
           font=_font(27, True), fill=ICE, anchor="mm")

    computed = []
    for uid, player_rows in rows_by_user.items():
        val = _metric_value(metric_key, player_rows)
        relevant = (sum(r["runs"] for r in player_rows) > 0) if category == "Batting" else (
            sum(r["balls_bowled"] for r in player_rows) > 0 or sum(r["wickets"] for r in player_rows) > 0)
        if metric_key == "ducks":
            relevant = any(r["runs"] == 0 and not r["not_out"] for r in player_rows)
        if metric_key == "fifties":
            relevant = any(50 <= r["runs"] < 100 for r in player_rows)
        if metric_key == "hundreds":
            relevant = any(r["runs"] >= 100 for r in player_rows)
        if metric_key == "highest":
            relevant = any(r["runs"] > 0 for r in player_rows)
        if metric_key == "best_figures":
            relevant = any(r["balls_bowled"] > 0 or r["wickets"] > 0 for r in player_rows)
        if relevant:
            computed.append({"id": uid, "rows": player_rows, "value": val})
    computed.sort(key=lambda p: (_sort_value(metric_key, p["value"]), p["id"]))
    top = computed[:10]
    rank_map = {p["id"]: i + 1 for i, p in enumerate(computed)}
    requester = next((p for p in computed if p["id"] == requester_id), None)

    # Coordinates match the ten empty blue rows in the supplied template.
    row_centers = [343, 425, 508, 591, 674, 757, 840, 923, 1006, 1089]
    for i, player in enumerate(top):
        cy = row_centers[i]
        rank = i + 1
        uid = player["id"]
        member = guild.get_member(uid)
        if member is None:
            try:
                member = await guild.fetch_member(uid)
            except Exception:
                member = None
        # Use the Discord username, not the server nickname/display name.
        username = member.name if member else f"Unknown player ({str(uid)[-4:]})"
        avatar_data = await _avatar_bytes(member) if member else None
        avatar_size = 68 if rank == 1 else 48
        avatar_x = 145 if rank == 1 else 155
        _paste_avatar(im, avatar_data, avatar_x, cy - avatar_size // 2, avatar_size, username)
        d = ImageDraw.Draw(im)

        rank_color = GOLD if rank == 1 else (205, 226, 245) if rank == 2 else (255, 173, 91) if rank == 3 else ICE
        d.text((105, cy), f"{rank}.", font=_font(29 if rank == 1 else 25, True),
               fill=rank_color, anchor="mm", stroke_width=1, stroke_fill=(0, 20, 55))

        name_x = 230 if rank == 1 else 220
        name_max_width = 590
        name_font = _font(30 if rank == 1 else 27, True)
        while d.textbbox((0, 0), username, font=name_font)[2] > name_max_width and getattr(name_font, "size", 27) > 16:
            name_font = _font(getattr(name_font, "size", 27) - 1, True)
        d.text((name_x, cy), username, font=name_font, fill=WHITE, anchor="lm",
               stroke_width=1, stroke_fill=(0, 20, 55))

        value_text = _display_value(metric_key, player["value"])
        score_font = _font(31 if rank == 1 else 28, True)
        while d.textbbox((0, 0), value_text, font=score_font)[2] > 115 and getattr(score_font, "size", 28) > 18:
            score_font = _font(getattr(score_font, "size", 28) - 1, True)
        d.text((908, cy), value_text, font=score_font, fill=GOLD if rank == 1 else ICE,
               anchor="rm", stroke_width=1, stroke_fill=(0, 20, 55))

    # The final standalone box is always the person who ran +lb.
    panel_top, panel_bottom = 1229, 1328
    if requester:
        own_rank = rank_map[requester_id]
        own_member = guild.get_member(requester_id)
        if own_member is None:
            try:
                own_member = await guild.fetch_member(requester_id)
            except Exception:
                own_member = None
        own_name = own_member.name if own_member else f"Unknown player ({str(requester_id)[-4:]})"
        own_avatar = await _avatar_bytes(own_member) if own_member else None
        _paste_avatar(im, own_avatar, 105, 1257, 57, own_name)
        d = ImageDraw.Draw(im)
        d.text((181, 1285), f"#{own_rank}", font=_font(24, True), fill=GOLD, anchor="lm")
        own_font = _font(25, True)
        while d.textbbox((0, 0), own_name, font=own_font)[2] > 450 and getattr(own_font, "size", 25) > 15:
            own_font = _font(getattr(own_font, "size", 25) - 1, True)
        d.text((270, 1285), own_name, font=own_font, fill=WHITE, anchor="lm")
        d.text((910, 1285), _display_value(metric_key, requester["value"]),
               font=_font(33, True), fill=GOLD, anchor="rm")
    else:
        d.text((512, 1290), "No qualifying score yet", font=_font(22, True), fill=WHITE, anchor="mm")

    # Keep the background's lower branding unobstructed; no new branding text is drawn.
    out = io.BytesIO()
    im.save(out, format="PNG", optimize=True)
    return out.getvalue()


class CategorySelect(discord.ui.Select):
    def __init__(self, view: "LeaderboardView"):
        self.parent_view = view
        super().__init__(
            placeholder="Choose category",
            min_values=1,
            max_values=1,
            options=[discord.SelectOption(label="Batting", emoji="🏏", default=view.category == "Batting"),
                     discord.SelectOption(label="Bowling", emoji="🎯", default=view.category == "Bowling")],
            row=0,
        )

    async def callback(self, interaction: discord.Interaction):
        self.parent_view.category = self.values[0]
        self.parent_view.metric_key = METRICS[self.parent_view.category][0][0]
        self.parent_view.metric_label = METRICS[self.parent_view.category][0][1]
        await self.parent_view.refresh(interaction)


class MetricSelect(discord.ui.Select):
    def __init__(self, view: "LeaderboardView"):
        self.parent_view = view
        options = [discord.SelectOption(label=label, value=key, default=key == view.metric_key) for key, label in METRICS[view.category]]
        super().__init__(placeholder="Choose statistic", min_values=1, max_values=1, options=options, row=1)

    async def callback(self, interaction: discord.Interaction):
        self.parent_view.metric_key = self.values[0]
        self.parent_view.metric_label = next(label for key, label in METRICS[self.parent_view.category] if key == self.parent_view.metric_key)
        await self.parent_view.refresh(interaction)


class LeaderboardView(discord.ui.View):
    def __init__(self, cog: "LeaderboardCog", ctx: commands.Context, category: str = "Batting"):
        super().__init__(timeout=300)
        self.cog = cog
        self.ctx = ctx
        self.owner_id = ctx.author.id
        self.category = category
        self.metric_key, self.metric_label = METRICS[category][0]
        self.message: discord.Message | None = None
        self._rebuild_items()

    def _rebuild_items(self):
        self.clear_items()
        self.add_item(CategorySelect(self))
        self.add_item(MetricSelect(self))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Only the person who ran `+lb` can use these menus.", ephemeral=True)
            return False
        return True

    async def refresh(self, interaction: discord.Interaction):
        # Acknowledge the menu interaction immediately so Discord does not time out
        # while the leaderboard image and avatars are being rendered.
        await interaction.response.defer()
        self._rebuild_items()
        png = await self.cog.render(self.ctx.guild, self.metric_key, self.category, self.metric_label, self.owner_id)
        file = discord.File(io.BytesIO(png), filename="leaderboard.png")
        embed = discord.Embed(color=discord.Colour.from_rgb(*BLUE))
        embed.set_image(url="attachment://leaderboard.png")
        await interaction.edit_original_response(embed=embed, attachments=[file], view=self)


class LeaderboardCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

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
                "runs": int(row["runs"]), "balls_faced": int(row["balls_faced"]),
                "runs_conceded": int(row["runs_conceded"]), "balls_bowled": int(row["balls_bowled"]),
                "wickets": int(row["wickets"]), "not_out": int(row["not_out"]),
            })
        return result

    async def render(self, guild: discord.Guild, metric_key: str, category: str, metric_label: str, requester_id: int) -> bytes:
        rows = self._rows(guild.id)
        return await make_leaderboard_image(guild, rows, metric_key, category, metric_label, requester_id)

    @commands.command(name="lb", aliases=["leaderboard", "leaderboards"])
    @commands.guild_only()
    async def leaderboard(self, ctx: commands.Context):
        try:
            view = LeaderboardView(self, ctx, "Batting")
            png = await self.render(ctx.guild, view.metric_key, view.category, view.metric_label, ctx.author.id)
            file = discord.File(io.BytesIO(png), filename="leaderboard.png")
            embed = discord.Embed(color=discord.Colour.from_rgb(*BLUE))
            embed.set_image(url="attachment://leaderboard.png")
            message = await ctx.send(embed=embed, file=file, view=view)
            view.message = message
        except Exception as exc:
            # Keep the command from failing silently; do not expose a traceback to members.
            print(f"[lb] Failed to generate leaderboard: {type(exc).__name__}: {exc}")
            await ctx.send("I couldn't generate the leaderboard. Check the bot console for the error.")


async def setup(bot: commands.Bot):
    await bot.add_cog(LeaderboardCog(bot))
