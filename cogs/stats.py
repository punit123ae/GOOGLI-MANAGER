import io
import re
from collections import defaultdict
from datetime import datetime, timezone

import discord
from discord.ext import commands
from PIL import Image, ImageDraw, ImageFont


def parse_raw_stats(content):
    """Parse valid seven-integer rows; blank lines/code fences are ignored."""
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
        raise ValueError("No valid player-stat rows found. Reply to the raw-statistics message.")
    if len({row[0] for row in rows}) != len(rows):
        raise ValueError("Duplicate player IDs found in the raw statistics.")
    return rows


def fmt(value, digits=2):
    if isinstance(value, float):
        return f"{value:.{digits}f}".rstrip("0").rstrip(".") or "0"
    return str(value)


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
        "matches": len(rows), "runs": runs, "balls_faced": faced,
        "runs_conceded": conceded, "balls_bowled": bowled, "wickets": wickets,
        "not_outs": sum(r["not_out"] for r in rows),
        "strike_rate": runs / faced * 100 if faced else 0,
        "bat_avg": runs / dismissals if dismissals else runs,
        "overs": bowled / 6,
        "economy": conceded * 6 / bowled if bowled else 0,
        "bowl_avg": conceded / wickets if wickets else 0,
        "bowl_sr": bowled / wickets if wickets else 0,
    }


def classify_player(t):
    """Classify from recorded batting/bowling contribution, not an AI guess."""
    bat = (t["runs"] >= 50 or t["balls_faced"] >= 20) and (t["runs"] / max(t["balls_faced"], 1) >= 0.75)
    bowl = t["wickets"] >= 2 or (t["balls_bowled"] >= 12 and t["economy"] <= 10)
    if bat and bowl:
        return "ALL-ROUNDER"
    if bowl and not bat:
        return "BOWLER"
    if bat:
        return "BATSMAN"
    if t["wickets"] > 0 and t["runs"] == 0:
        return "BOWLER"
    return "BATSMAN" if t["runs"] >= t["wickets"] * 10 else "BOWLER"


def impact_scores(t):
    """Simple repeatable 0-100 impact indicators derived from recorded stats."""
    bat_score = min(100, round(min(t["runs"] / 5, 55) + min(t["strike_rate"] / 4, 25) + min(t["bat_avg"] / 5, 20)))
    wickets_component = min(55, t["wickets"] * 8)
    economy_component = max(0, min(25, 25 - max(0, t["economy"] - 4) * 2)) if t["balls_bowled"] else 0
    bowling_score = min(100, round(wickets_component + economy_component + min(20, t["balls_bowled"] / 6)))
    return bat_score, bowling_score


def make_stats_image(member, rows, guild_icon=None, avatar=None):
    """Player performance card. Every displayed statistic is computed from imported DB rows."""
    t = totals(rows)
    role = classify_player(t)
    bat_impact, bowl_impact = impact_scores(t)
    rating = round((bat_impact * 0.55 + bowl_impact * 0.45) if role == "ALL-ROUNDER" else (bat_impact if role == "BATSMAN" else bowl_impact))
    width, height = 1600, 900
    bg = (7, 13, 23); panel = (15, 23, 35); panel2 = (19, 30, 45)
    white, muted, blue, lime, cyan = (242, 246, 253), (143, 160, 181), (35, 119, 231), (194, 255, 51), (81, 218, 238)
    image = Image.new("RGB", (width, height), bg)
    draw = ImageDraw.Draw(image)
    # Subtle diagonal background accents, original layout inspired by sports analytics cards.
    draw.polygon([(1190, 0), (1600, 0), (1600, 900), (1440, 900)], fill=(8, 26, 48))
    draw.polygon([(0, 0), (42, 0), (220, 900), (158, 900)], fill=(12, 49, 91))
    draw.line((55, 118, 1545, 118), fill=(39, 53, 70), width=2)
    draw.text((58, 32), member.guild.name.upper()[:35], font=load_font(25, True), fill=white)
    draw.text((58, 70), "PLAYER PERFORMANCE  /  SEASON PROFILE", font=load_font(17, True), fill=blue)
    draw.text((1130, 48), "PLAYER IDENTITY CARD", font=load_font(31, True), fill=white)

    # Left identity card
    draw.rounded_rectangle((55, 145, 555, 835), radius=28, fill=panel, outline=(35, 57, 83), width=2)
    draw.text((88, 175), "OVERALL RATING", font=load_font(18, True), fill=blue)
    draw.text((82, 200), str(rating), font=load_font(94, True), fill=white)
    draw.text((215, 258), "/100", font=load_font(23, True), fill=muted)
    pill_w = 230 if role == "ALL-ROUNDER" else 190
    draw.rounded_rectangle((330, 180, 330 + pill_w, 228), radius=20, fill=(12, 42, 76), outline=(29, 88, 151), width=1)
    draw.text((348, 194), role, font=load_font(18, True), fill=blue)

    # Circular avatar and small server mark; fallback initials if Discord images are unavailable.
    avatar_box = (155, 315, 455, 615)
    draw.ellipse(avatar_box, fill=(39, 56, 75), outline=white, width=6)
    if avatar:
        try:
            import io as _io
            av = Image.open(_io.BytesIO(avatar)).convert("RGB").resize((280, 280))
            mask = Image.new("L", (280, 280), 0); ImageDraw.Draw(mask).ellipse((0, 0, 279, 279), fill=255)
            image.paste(av, (165, 325), mask)
            draw.ellipse((155, 315, 455, 615), outline=white, width=6)
        except Exception:
            pass
    else:
        initials = (member.display_name[:1] or "?").upper()
        draw.text((275, 405), initials, font=load_font(90, True), fill=white)
    if guild_icon:
        try:
            import io as _io
            gi = Image.open(_io.BytesIO(guild_icon)).convert("RGB").resize((82, 82))
            image.paste(gi, (395, 535))
            draw.ellipse((392, 532, 482, 622), outline=blue, width=5)
        except Exception:
            pass
    draw.text((85, 642), member.display_name[:25], font=load_font(35, True), fill=white)
    draw.text((88, 687), "TEAM NOT CONNECTED", font=load_font(18, True), fill=muted)
    draw.text((88, 735), "BATTING IMPACT", font=load_font(17, True), fill=muted)
    draw.text((475, 727), str(bat_impact), font=load_font(26, True), fill=white, anchor="ra")
    draw.rounded_rectangle((88, 765, 510, 778), radius=6, fill=(39, 47, 58))
    draw.rounded_rectangle((88, 765, 88 + int(422 * bat_impact / 100), 778), radius=6, fill=lime)
    draw.text((88, 790), "BOWLING IMPACT", font=load_font(17, True), fill=muted)
    draw.text((475, 782), str(bowl_impact), font=load_font(26, True), fill=white, anchor="ra")
    draw.rounded_rectangle((88, 820, 510, 833), radius=6, fill=(39, 47, 58))
    draw.rounded_rectangle((88, 820, 88 + int(422 * bowl_impact / 100), 833), radius=6, fill=cyan)

    # Main stats, intentionally excluding MVP and fielding/catches until those are tracked.
    x0, x1 = 595, 1545
    draw.text((x0, 150), "SEASON SNAPSHOT", font=load_font(20, True), fill=white)
    cards = [("MATCHES", t["matches"], blue), ("RUNS", t["runs"], lime), ("WICKETS", t["wickets"], cyan), ("STRIKE RATE", fmt(t["strike_rate"]), blue)]
    card_y = 188; card_w = 220; gap = 20
    for i, (label, value, accent) in enumerate(cards):
        x = x0 + i * (card_w + gap)
        draw.rounded_rectangle((x, card_y, x + card_w, card_y + 105), radius=18, fill=panel, outline=(36, 47, 61), width=2)
        draw.text((x + 20, card_y + 18), label, font=load_font(15, True), fill=muted)
        draw.ellipse((x + card_w - 31, card_y + 19, x + card_w - 17, card_y + 33), fill=accent)
        draw.text((x + 20, card_y + 52), str(value), font=load_font(31, True), fill=accent if label != "MATCHES" else white)

    # Batting panel
    draw.rounded_rectangle((x0, 320, x0 + 455, 680), radius=23, fill=panel, outline=(35, 48, 64), width=2)
    draw.rounded_rectangle((x0 + 25, 340, x0 + 65, 347), radius=4, fill=lime)
    draw.text((x0 + 25, 362), "BATTING", font=load_font(21, True), fill=lime)
    batting = [("INNINGS", t["matches"]), ("AVERAGE", fmt(t["bat_avg"])), ("BALLS FACED", t["balls_faced"]), ("HIGH SCORE", max((r["runs"] for r in rows), default=0)), ("FIFTIES", sum(50 <= r["runs"] < 100 for r in rows)), ("HUNDREDS", sum(r["runs"] >= 100 for r in rows)), ("DUCKS", sum(r["runs"] == 0 and not r["not_out"] for r in rows)), ("NOT OUTS", t["not_outs"])]
    for i, (label, value) in enumerate(batting):
        col, row = i % 2, i // 2
        x = x0 + 30 + col * 210; y = 405 + row * 64
        draw.text((x, y), label, font=load_font(13, True), fill=muted)
        draw.text((x, y + 21), str(value), font=load_font(25, True), fill=white)

    # Bowling panel
    bx = x0 + 475
    draw.rounded_rectangle((bx, 320, bx + 475, 680), radius=23, fill=panel, outline=(35, 48, 64), width=2)
    draw.rounded_rectangle((bx + 25, 340, bx + 65, 347), radius=4, fill=cyan)
    draw.text((bx + 25, 362), "BOWLING", font=load_font(21, True), fill=cyan)
    best = max(((r["wickets"], -r["runs_conceded"]) for r in rows if r["balls_bowled"] > 0), default=(0, 0))
    bowling = [("INNINGS", sum(r["balls_bowled"] > 0 for r in rows)), ("OVERS", fmt(t["overs"])), ("AVERAGE", fmt(t["bowl_avg"])), ("ECONOMY", fmt(t["economy"])), ("STRIKE RATE", fmt(t["bowl_sr"])), ("BEST FIGURES", f"{best[0]}/{-best[1]}")]
    for i, (label, value) in enumerate(bowling):
        col, row = i % 2, i // 2
        x = bx + 30 + col * 220; y = 405 + row * 78
        draw.text((x, y), label, font=load_font(13, True), fill=muted)
        draw.text((x, y + 22), str(value), font=load_font(25, True), fill=white)

    draw.text((x0, 725), "GENERATED FROM RECORDED MATCH STATS  •  TEAM DETAILS CAN BE ADDED LATER", font=load_font(15, True), fill=muted)
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    output.seek(0)
    return output


def build_overview_embed(player_name, rows):
    t = totals(rows)
    embed = discord.Embed(
        title=f"Overall Performance — {player_name}",
        description=f"**Matches recorded:** {t['matches']}\nUse the dropdown to view overall performance or the player's most recent five matches.",
        color=discord.Color.from_rgb(24, 47, 82),
    )
    embed.add_field(name="Batting", value=(
        f"Runs: **{t['runs']}**\nBalls: **{t['balls_faced']}**\n"
        f"Average: **{fmt(t['bat_avg'])}**\nStrike rate: **{fmt(t['strike_rate'])}**\nNot outs: **{t['not_outs']}**"
    ), inline=True)
    embed.add_field(name="Bowling", value=(
        f"Wickets: **{t['wickets']}**\nRuns conceded: **{t['runs_conceded']}**\n"
        f"Balls bowled: **{t['balls_bowled']}**\nEconomy: **{fmt(t['economy'])}**\n"
        f"Average: **{fmt(t['bowl_avg'])}**\nStrike rate: **{fmt(t['bowl_sr'])}**"
    ), inline=True)
    embed.set_footer(text="GOOGLI MANAGER • Overall performance")
    return embed


def build_recent_embed(player_name, rows):
    recent = list(reversed(rows[-5:]))
    embed = discord.Embed(
        title=f"Recent 5 Matches — {player_name}",
        description="Most recent imported records first. Match names/opponents can be connected later.",
        color=discord.Color.from_rgb(24, 47, 82),
    )
    for index, row in enumerate(recent, start=1):
        outs = "not out" if row["not_out"] else "out"
        embed.add_field(
            name=f"Recent match {index}",
            value=(f"**Batting:** {row['runs']} runs off {row['balls_faced']} balls ({outs})\n"
                   f"**Bowling:** {row['wickets']} wickets, {row['runs_conceded']} runs conceded, "
                   f"{row['balls_bowled']} balls bowled"),
            inline=False,
        )
    embed.set_footer(text=f"Showing {len(recent)} of {len(rows)} recorded matches")
    return embed


class StatsSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(placeholder="Choose a stats view...", min_values=1, max_values=1, options=[
            discord.SelectOption(label="Overall Performance", description="Career totals and averages", emoji="📊", value="overall"),
            discord.SelectOption(label="Recent 5 Matches", description="Detailed recent match records", emoji="📋", value="recent"),
        ])

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        if interaction.user.id != view.requester_id:
            await interaction.response.send_message("This stats menu belongs to the person who requested it.", ephemeral=True)
            return
        if self.values[0] == "recent":
            await interaction.response.edit_message(embed=build_recent_embed(view.player_name, view.rows), attachments=[], view=view)
        else:
            await interaction.response.edit_message(embed=build_overview_embed(view.player_name, view.rows), attachments=[], view=view)


class StatsView(discord.ui.View):
    def __init__(self, requester_id, player_name, rows):
        super().__init__(timeout=600)
        self.requester_id = requester_id
        self.player_name = player_name
        self.rows = rows
        self.add_item(StatsSelect())


# Leaderboard definitions: (key, label, category, sort-descending)
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


class Stats(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.db = bot.db

    def admin(self, ctx):
        return ctx.guild is not None and ctx.author.guild_permissions.administrator

    async def replied_message(self, ctx):
        ref = ctx.message.reference
        if ref is None or ref.message_id is None:
            return None
        if isinstance(ref.resolved, discord.Message):
            return ref.resolved
        try:
            channel = self.bot.get_channel(ref.channel_id) or await self.bot.fetch_channel(ref.channel_id)
            return await channel.fetch_message(ref.message_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException, AttributeError):
            return None

    def get_player_rows(self, guild_id, user_id):
        records = self.db.execute(
            """SELECT p.source_message_id, p.user_id, p.runs, p.balls_faced,
                      p.runs_conceded, p.balls_bowled, p.wickets, p.not_out
               FROM player_match_stats p JOIN stats_imports i ON i.source_message_id = p.source_message_id
               WHERE i.guild_id = ? AND p.user_id = ?
               ORDER BY i.imported_at ASC, p.source_message_id ASC""", (guild_id, user_id)
        ).fetchall()
        return [dict(row) for row in records]

    @commands.command(name="as", aliases=["addstats"])
    async def add_stats(self, ctx):
        if not self.admin(ctx):
            await ctx.reply("Administrator permission is required.", mention_author=False)
            return
        source = await self.replied_message(ctx)
        if source is None or source.guild is None or source.guild.id != ctx.guild.id:
            await ctx.reply("Reply directly to the raw-statistics message with `+as`.", mention_author=False)
            return
        try:
            rows = parse_raw_stats(source.content)
        except ValueError as exc:
            await ctx.reply(f"❌ {exc}", mention_author=False)
            return
        try:
            with self.db:
                self.db.execute(
                    "INSERT INTO stats_imports (source_message_id, guild_id, imported_by, imported_at) VALUES (?, ?, ?, ?)",
                    (source.id, ctx.guild.id, ctx.author.id, datetime.now(timezone.utc).timestamp()))
                self.db.executemany(
                    """INSERT INTO player_match_stats (source_message_id, user_id, runs, balls_faced,
                       runs_conceded, balls_bowled, wickets, not_out) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    [(source.id, *row) for row in rows])
        except Exception as exc:
            if "UNIQUE constraint failed" in str(exc) or "PRIMARY KEY" in str(exc):
                await ctx.reply("❌ These raw statistics have already been imported.", mention_author=False)
            else:
                await ctx.reply(f"❌ Import failed; no rows were saved. Error: `{type(exc).__name__}`", mention_author=False)
            return
        await ctx.reply(f"✅ Imported statistics for **{len(rows)} players**.\nMatch record: `{source.id}`", mention_author=False)

    @commands.command(name="rs", aliases=["removestats"])
    async def remove_stats(self, ctx):
        if not self.admin(ctx):
            await ctx.reply("Administrator permission is required.", mention_author=False)
            return
        source = await self.replied_message(ctx)
        if source is None or source.guild is None or source.guild.id != ctx.guild.id:
            await ctx.reply("Reply to the original raw-statistics message with `+rs`.", mention_author=False)
            return
        record = self.db.execute("SELECT source_message_id FROM stats_imports WHERE source_message_id=? AND guild_id=?", (source.id, ctx.guild.id)).fetchone()
        if record is None:
            await ctx.reply("❌ That message has not been imported, or it was already removed.", mention_author=False)
            return
        with self.db:
            self.db.execute("DELETE FROM player_match_stats WHERE source_message_id=?", (source.id,))
            self.db.execute("DELETE FROM stats_imports WHERE source_message_id=?", (source.id,))
        await ctx.reply("✅ Removed that match's imported statistics. Other match records remain unchanged.", mention_author=False)

    @commands.command(name="stats")
    async def player_stats(self, ctx, member: discord.Member = None):
        member = member or ctx.author
        rows = self.get_player_rows(ctx.guild.id, member.id)
        if not rows:
            await ctx.reply(f"No imported stats found for {member.mention} yet. Import a match with `+as` first.", mention_author=False)
            return
        avatar = guild_icon = None
        try:
            avatar = await member.display_avatar.replace(size=256, static_format="png").read()
        except Exception:
            pass
        try:
            if ctx.guild.icon:
                guild_icon = await ctx.guild.icon.replace(size=128, static_format="png").read()
        except Exception:
            pass
        image = make_stats_image(member, rows, guild_icon=guild_icon, avatar=avatar)
        filename = f"player_{member.id}_stats.png"
        file = discord.File(image, filename=filename)
        embed = build_overview_embed(member.display_name, rows)
        embed.set_image(url=f"attachment://{filename}")
        view = StatsView(ctx.author.id, member.display_name, rows)
        await ctx.send(embed=embed, file=file, view=view, allowed_mentions=discord.AllowedMentions.none())


async def setup(bot):
    await bot.add_cog(Stats(bot))
