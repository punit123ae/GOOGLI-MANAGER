"""Hand Cricket catch tracking for Googli Manager.

Persistent fielding stats (per server, per player) stored in the bot's existing
SQLite database (bot.db), plus per-channel tracking switches that survive restarts.
"""

import re
import time

import discord
from discord.ext import commands

HANDCRICKET_BOT_ID = 753191385296928808
BOT_OWNER_ID = 1014568928682459198

# A catch opportunity is forgotten if no result arrives within this many seconds,
# so a stale "pending" can never be matched to an unrelated later result.
PENDING_TTL_SECONDS = 120
# Sanity cap for manually entered totals.
MAX_MANUAL_TOTAL = 1_000_000
PROMPT_TIMEOUT_SECONDS = 60


def _normalise_command_name(name):
    return str(name or "").strip().lower().lstrip("+!/").replace(" ", "")


def _member_has_whitelist_role(bot, guild_id, member, command_name):
    """Check the exact command's role whitelist stored by the utility cog."""
    if not guild_id or member is None:
        return False
    try:
        rows = bot.db.execute(
            "SELECT role_id FROM command_whitelist WHERE guild_id=? AND command_name=?",
            (int(guild_id), _normalise_command_name(command_name)),
        ).fetchall()
    except Exception:
        # If utility's whitelist table is unavailable, fail closed for non-owners.
        return False
    allowed = {int(row[0]) for row in rows}
    member_roles = {int(role.id) for role in getattr(member, "roles", [])}
    return bool(allowed & member_roles)


def _efficiency(caught, dropped):
    total = caught + dropped
    return caught / total * 100 if total else 0.0


def _rank_fielders(stats_by_user):
    """Rank players who have fielded: most catches first, then efficiency, then chances.

    stats_by_user: {user_id: (caught, dropped)}. Players with no recorded catch
    chance (caught + dropped == 0) are not ranked at all.
    Returns a list of (user_id, caught, dropped) best -> worst.
    """
    fielders = [
        (uid, c, d) for uid, (c, d) in stats_by_user.items() if c + d > 0
    ]
    fielders.sort(key=lambda r: (-r[1], -_efficiency(r[1], r[2]), -(r[1] + r[2]), r[0]))
    return fielders


def _fielder_title(ranked, user_id):
    """Return (label_kind, rank, total) for a ranked player, or None if unranked.

    Ties share a rank. kind is one of: best, top, bottom, worst.
    - best:   nobody on the team is strictly ahead of them
    - worst:  nobody on the team is strictly behind them (team of 2+ fielders)
    - top:    upper half of the team's fielders
    - bottom: lower half of the team's fielders
    """
    entry = next((r for r in ranked if r[0] == user_id), None)
    if entry is None:
        return None
    total = len(ranked)

    def key(r):
        return (r[1], _efficiency(r[1], r[2]), r[1] + r[2])

    mine = key(entry)
    ahead = sum(1 for r in ranked if key(r) > mine)
    behind = sum(1 for r in ranked if key(r) < mine)
    rank = ahead + 1
    if ahead == 0:
        kind = "best"
    elif behind == 0:
        kind = "worst"
    elif ahead < behind:
        kind = "top"
    else:
        kind = "bottom"
    return kind, rank, total


_TITLE_TEXT = {
    "best": "BEST FIELDER OF",
    "top": "ONE OF THE BEST FIELDERS OF",
    "bottom": "ONE OF THE WORST FIELDERS OF",
    "worst": "WORST FIELDER OF",
}
_TITLE_COLOR = {
    "best": discord.Color.green(),
    "top": discord.Color.from_rgb(55, 150, 255),
    "bottom": discord.Color.orange(),
    "worst": discord.Color.red(),
}


class PagedEmbedView(discord.ui.View):
    """Previous / next buttons for a fixed list of embeds, usable only by the requester."""

    def __init__(self, ctx, embeds):
        super().__init__(timeout=120)
        self.owner_id = ctx.author.id
        self.embeds = embeds
        self.index = 0
        self.message = None
        self._sync()

    def _sync(self):
        self.prev_button.disabled = self.index <= 0
        self.next_button.disabled = self.index >= len(self.embeds) - 1
        self.counter.label = f"{self.index + 1}/{len(self.embeds)}"

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Only the person who ran this command can turn the pages.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index = max(0, self.index - 1)
        self._sync()
        await interaction.response.edit_message(embed=self.embeds[self.index], view=self)

    @discord.ui.button(label="1/1", style=discord.ButtonStyle.primary, disabled=True)
    async def counter(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index = min(len(self.embeds) - 1, self.index + 1)
        self._sync()
        await interaction.response.edit_message(embed=self.embeds[self.index], view=self)

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class ResetConfirmView(discord.ui.View):
    """Confirm / cancel buttons usable only by the person who ran the reset."""

    def __init__(self, cog, ctx):
        super().__init__(timeout=30)
        self.cog = cog
        self.ctx = ctx
        self.owner_id = ctx.author.id
        self.message = None
        self.finished = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Only the person who ran this command can confirm or cancel it.",
                ephemeral=True,
            )
            return False
        return True

    def _disable(self):
        for child in self.children:
            child.disabled = True

    @discord.ui.button(label="Confirm reset", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.finished:
            await interaction.response.defer()
            return
        self.finished = True
        deleted = self.cog._reset_guild_stats(self.ctx.guild.id)
        self._disable()
        self.stop()
        await interaction.response.edit_message(
            content=f"✅ Fielding stats for this server were reset ({deleted} player record(s) removed).",
            view=self,
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.finished:
            await interaction.response.defer()
            return
        self.finished = True
        self._disable()
        self.stop()
        await interaction.response.edit_message(
            content="Cancelled. No fielding stats were changed.", view=self
        )

    async def on_timeout(self):
        if self.finished:
            return
        self.finished = True
        self._disable()
        if self.message is not None:
            try:
                await self.message.edit(
                    content="Reset timed out. No fielding stats were changed.", view=self
                )
            except discord.HTTPException:
                pass


class CatchTracker(commands.Cog):
    """Tracks catch chances and outcomes in channels where tracking is started."""

    def __init__(self, bot):
        self.bot = bot
        self.pending = {}  # channel_id -> {fielder_id, batsman_id, message_id, created}
        self._init_db()
        self.enabled_channels = self._load_enabled_channels()

    # ------------------------------------------------------------------ database
    def _init_db(self):
        db = self.bot.db
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS fielding_stats (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                catches_caught INTEGER NOT NULL DEFAULT 0,
                catches_dropped INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (guild_id, user_id)
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS fielding_tracking_channels (
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                PRIMARY KEY (guild_id, channel_id)
            )
            """
        )
        db.commit()

    def _load_enabled_channels(self):
        rows = self.bot.db.execute(
            "SELECT channel_id FROM fielding_tracking_channels"
        ).fetchall()
        return {int(row[0]) for row in rows}

    def _get_stats(self, guild_id, user_id):
        row = self.bot.db.execute(
            "SELECT catches_caught, catches_dropped FROM fielding_stats "
            "WHERE guild_id=? AND user_id=?",
            (int(guild_id), int(user_id)),
        ).fetchone()
        if not row:
            return 0, 0
        return int(row[0]), int(row[1])

    def _set_stats(self, guild_id, user_id, caught, dropped):
        self.bot.db.execute(
            """
            INSERT INTO fielding_stats (guild_id, user_id, catches_caught, catches_dropped)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(guild_id, user_id) DO UPDATE SET
                catches_caught=excluded.catches_caught,
                catches_dropped=excluded.catches_dropped
            """,
            (int(guild_id), int(user_id), int(caught), int(dropped)),
        )
        self.bot.db.commit()

    def _increment(self, guild_id, user_id, result):
        caught = 1 if result == "taken" else 0
        dropped = 1 if result == "dropped" else 0
        self.bot.db.execute(
            """
            INSERT INTO fielding_stats (guild_id, user_id, catches_caught, catches_dropped)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(guild_id, user_id) DO UPDATE SET
                catches_caught=catches_caught + excluded.catches_caught,
                catches_dropped=catches_dropped + excluded.catches_dropped
            """,
            (int(guild_id), int(user_id), caught, dropped),
        )
        self.bot.db.commit()

    def _reset_guild_stats(self, guild_id):
        cur = self.bot.db.execute(
            "DELETE FROM fielding_stats WHERE guild_id=?", (int(guild_id),)
        )
        self.bot.db.commit()
        return cur.rowcount

    # ------------------------------------------------------------------ parsing
    @staticmethod
    def _message_text(message):
        parts = [message.content or ""]
        for embed in message.embeds:
            parts.extend([embed.title or "", embed.description or ""])
            for field in embed.fields:
                parts.extend([field.name or "", field.value or ""])
        return "\n".join(parts)

    @staticmethod
    def _find_player(text, pattern, guild):
        match = re.search(pattern + r"\s*<@!?(\d+)>", text, re.IGNORECASE)
        if match:
            return int(match.group(1))
        # Fallback for embeds that contain plain @username text.
        match = re.search(pattern + r"\s*@?([A-Za-z0-9_.-]{2,32})", text, re.IGNORECASE)
        if match and guild:
            username = match.group(1).casefold()
            for member in guild.members:
                if member.name.casefold() == username or member.display_name.casefold() == username:
                    return member.id
        return None

    def _parse_opportunity(self, message):
        text = self._message_text(message)
        if "CHANCE OF WICKET!" not in text.upper():
            return None
        fielder_id = self._find_player(text, r"coming\s+toward\s+", message.guild)
        batsman_id = self._find_player(text, r"takes\s+this\s+catch,\s*", message.guild)
        if fielder_id is None:
            return None
        return fielder_id, batsman_id

    @staticmethod
    def _result_kind(message):
        text = CatchTracker._message_text(message).upper()
        if "BOZO DROPPED THE CATCH" in text:
            return "dropped"
        if "TOOK THE CATCH" in text:
            return "taken"
        return None

    # ------------------------------------------------------------------ embeds
    def _recent_matches(self, guild_id, user_id, limit=5):
        """Last matches this player fielded in, newest first, from imported match stats."""
        try:
            rows = self.bot.db.execute(
                """
                SELECT i.imported_at, f.opportunities, f.catches_taken, f.catches_dropped,
                       f.source_message_id
                FROM player_match_fielding f
                JOIN stats_imports i ON i.source_message_id = f.source_message_id
                WHERE i.guild_id = ? AND f.user_id = ?
                ORDER BY i.imported_at DESC, f.source_message_id DESC
                LIMIT ?
                """,
                (int(guild_id), int(user_id), int(limit)),
            ).fetchall()
        except Exception:
            return []
        return [
            (float(r[0]), int(r[1]), int(r[2]), int(r[3]), int(r[4])) for r in rows
        ]

    def _opponent_name(self, match_id, user_id, rosters, team_of):
        """Name of the team the player faced in this match, or None if unknown.

        Match records only store players, not teams, so the opponent is the other
        team with the most players in that match (using current team rosters).
        """
        my_team = team_of.get(user_id)
        if my_team is None:
            return None
        try:
            rows = self.bot.db.execute(
                "SELECT user_id FROM player_match_stats WHERE source_message_id = ?",
                (int(match_id),),
            ).fetchall()
        except Exception:
            return None
        counts = {}
        for (uid,) in rows:
            team_id = team_of.get(int(uid))
            if team_id is not None and team_id != my_team:
                counts[team_id] = counts.get(team_id, 0) + 1
        if not counts:
            return None
        opponent = max(counts, key=lambda t: (counts[t], -t))
        return rosters[opponent]["name"]

    def _recent_field_value(self, guild_id, user_id):
        rows = self._recent_matches(guild_id, user_id)
        if not rows:
            return "No match records yet."
        # Reuse the roster logic from lb.py to map players to their teams.
        try:
            from .lb import _team_rosters
            rosters = _team_rosters(guild_id)
        except Exception:
            rosters = {}
        team_of = {
            uid: team_id for team_id, t in rosters.items() for uid in t["members"]
        }
        lines = []
        for i, (ts, opps, taken, dropped, match_id) in enumerate(rows, start=1):
            chances = max(opps, taken + dropped)
            opponent = self._opponent_name(match_id, user_id, rosters, team_of)
            if opponent:
                label = f"vs **{discord.utils.escape_markdown(opponent)}**"
            else:
                label = f"<t:{int(ts)}:d>"  # opponent unknown: fall back to the date
            lines.append(
                f"`{i}.` {label} → `{taken}` caught • `{dropped}` dropped • `{chances}` chances"
            )
        total_taken = sum(r[2] for r in rows)
        total_dropped = sum(r[3] for r in rows)
        lines.append(
            f"**Last {len(rows)}:** `{total_taken}` caught, `{total_dropped}` dropped "
            f"(`{_efficiency(total_taken, total_dropped):.2f}%`)"
        )
        return "\n".join(lines)

    def _build_stats_embed(self, guild, user_id, result=None, recent=False):
        caught, dropped = self._get_stats(guild.id, user_id)
        member = guild.get_member(user_id)
        display_name = discord.utils.escape_markdown(
            member.display_name if member else f"<@{user_id}>"
        )

        if result == "taken":
            embed_color = discord.Color.green()
        elif result == "dropped":
            embed_color = discord.Color.red()
        else:
            embed_color = discord.Color.from_rgb(55, 150, 255)

        embed = discord.Embed(
            title=f"🧤 FIELDING STATS FOR {display_name}",
            color=embed_color,
        )
        if member:
            embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="CATCHES CAUGHT", value=f"`{caught}`", inline=False)
        embed.add_field(name="CATCHES DROPPED", value=f"`{dropped}`", inline=False)
        embed.add_field(
            name="CATCH EFFICIENCY",
            value=f"`{_efficiency(caught, dropped):.2f}%`",
            inline=False,
        )
        if recent:
            embed.add_field(
                name="RECENT 5 MATCHES",
                value=self._recent_field_value(guild.id, user_id),
                inline=False,
            )
        embed.set_footer(text="Fielding tracker")
        return embed

    def _build_team_embed(self, guild, member_id):
        """Comparison of one player with their team's fielders."""
        # Reuse the roster logic from lb.py (same membership rules as +lb and +stats).
        try:
            from .lb import _team_rosters
            rosters = _team_rosters(guild.id)
        except Exception:
            rosters = {}

        member = guild.get_member(member_id)
        name = discord.utils.escape_markdown(
            member.display_name if member else f"<@{member_id}>"
        )
        team = next(
            (t for t in rosters.values() if member_id in t["members"]), None
        )
        embed = discord.Embed(
            title=f"🧤 TEAM FIELDING COMPARISON FOR {name}",
            color=discord.Color.from_rgb(55, 150, 255),
        )
        if member:
            embed.set_thumbnail(url=member.display_avatar.url)

        if team is None:
            embed.description = "This player is not on an approved team, so there is nothing to compare with."
            embed.set_footer(text="Fielding tracker")
            return embed

        team_name = discord.utils.escape_markdown(team["name"])
        stats = {uid: self._get_stats(guild.id, uid) for uid in team["members"]}
        ranked = _rank_fielders(stats)
        caught, dropped = stats.get(member_id, (0, 0))
        team_caught = sum(c for c, _ in stats.values())
        team_dropped = sum(d for _, d in stats.values())

        title = _fielder_title(ranked, member_id)
        if title is None:
            embed.description = (
                f"**{name}** has no recorded fielding yet, so they are not ranked in **{team_name}**."
            )
        else:
            kind, rank, total = title
            embed.title = f"🧤 {_TITLE_TEXT[kind]} {team_name}".upper()
            embed.color = _TITLE_COLOR[kind]
            embed.description = f"**{name}** ranks `#{rank}` of `{total}` fielder(s) in **{team_name}**."
            embed.add_field(name="CATCHES CAUGHT", value=f"`{caught}`", inline=True)
            embed.add_field(name="CATCHES DROPPED", value=f"`{dropped}`", inline=True)
            embed.add_field(
                name="CATCH EFFICIENCY",
                value=f"`{_efficiency(caught, dropped):.2f}%`",
                inline=True,
            )
            share = caught / team_caught * 100 if team_caught else 0.0
            embed.add_field(
                name="SHARE OF TEAM CATCHES", value=f"`{share:.2f}%`", inline=True
            )
            avg_caught = team_caught / total if total else 0.0
            embed.add_field(
                name="TEAM AVERAGE (PER FIELDER)",
                value=f"`{avg_caught:.2f}` caught",
                inline=True,
            )
            embed.add_field(
                name="TEAM EFFICIENCY",
                value=f"`{_efficiency(team_caught, team_dropped):.2f}%`",
                inline=True,
            )
            lines = []
            for i, (uid, c, d) in enumerate(ranked[:5], start=1):
                m = guild.get_member(uid)
                nm = discord.utils.escape_markdown(
                    m.display_name if m else f"Unknown ({str(uid)[-4:]})"
                )
                marker = " ⬅️" if uid == member_id else ""
                lines.append(
                    f"`{i}.` **{nm}** → `{c}` caught • `{_efficiency(c, d):.1f}%`{marker}"
                )
            embed.add_field(name=f"TOP FIELDERS OF {team_name}".upper(), value="\n".join(lines), inline=False)
        embed.set_footer(text="Ranked by catches caught, then catch efficiency • only players with fielding records")
        return embed

    def _build_overview_embed(self, guild):
        rows = self.bot.db.execute(
            "SELECT user_id, catches_caught, catches_dropped FROM fielding_stats WHERE guild_id=?",
            (guild.id,),
        ).fetchall()
        total_caught = sum(int(r[1]) for r in rows)
        total_dropped = sum(int(r[2]) for r in rows)
        embed = discord.Embed(
            title="🧤 SERVER FIELDING OVERVIEW",
            color=discord.Color.from_rgb(55, 150, 255),
        )
        embed.add_field(name="PLAYERS TRACKED", value=f"`{len(rows)}`", inline=False)
        embed.add_field(name="TOTAL CATCHES CAUGHT", value=f"`{total_caught}`", inline=False)
        embed.add_field(name="TOTAL CATCHES DROPPED", value=f"`{total_dropped}`", inline=False)
        embed.add_field(
            name="OVERALL CATCH EFFICIENCY",
            value=f"`{_efficiency(total_caught, total_dropped):.2f}%`",
            inline=False,
        )
        top = sorted(rows, key=lambda r: (-int(r[1]), int(r[0])))[:3]
        top = [r for r in top if int(r[1]) > 0]
        if top:
            lines = []
            for i, r in enumerate(top, start=1):
                m = guild.get_member(int(r[0]))
                nm = discord.utils.escape_markdown(m.display_name if m else f"Unknown ({str(r[0])[-4:]})")
                lines.append(f"`{i}.` **{nm}** → `{int(r[1])}` caught")
            embed.add_field(name="TOP FIELDERS", value="\n".join(lines), inline=False)
        embed.set_footer(text="Use +fieldingstats @user for one player • +lb for the leaderboard")
        return embed

    async def _send_stats_embed(self, channel, fielder_id, result=None):
        """Send a NEW snapshot embed; never edit a previous fielding embed."""
        embed = self._build_stats_embed(channel.guild, fielder_id, result)
        await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    # ------------------------------------------------------------------ permissions
    async def _tracking_permission(self, ctx, command_name):
        if int(ctx.author.id) == BOT_OWNER_ID:
            return True
        if _member_has_whitelist_role(self.bot, ctx.guild.id, ctx.author, command_name):
            return True
        await ctx.reply(
            f"❌ You need a role whitelisted for `+{command_name}` to use this command.",
            mention_author=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return False

    # ------------------------------------------------------------------ tracking commands
    @commands.command(name="starttracking")
    @commands.guild_only()
    async def start_tracking(self, ctx):
        """Start Hand Cricket catch tracking in this channel."""
        if not await self._tracking_permission(ctx, "starttracking"):
            return
        if ctx.channel.id in self.enabled_channels:
            await ctx.reply("Catch tracking is already active in this channel.", mention_author=False)
            return
        self.bot.db.execute(
            "INSERT OR IGNORE INTO fielding_tracking_channels (guild_id, channel_id) VALUES (?, ?)",
            (ctx.guild.id, ctx.channel.id),
        )
        self.bot.db.commit()
        self.enabled_channels.add(ctx.channel.id)
        self.pending.pop(ctx.channel.id, None)
        await ctx.reply(
            "✅ Catch tracking started in this channel. It will watch Hand Cricket catch opportunities and results.",
            mention_author=False,
        )

    @commands.command(name="stoptracking")
    @commands.guild_only()
    async def stop_tracking(self, ctx):
        """Stop catch tracking in this channel."""
        if not await self._tracking_permission(ctx, "stoptracking"):
            return
        if ctx.channel.id not in self.enabled_channels:
            await ctx.reply("Catch tracking is not active in this channel.", mention_author=False)
            return
        self.bot.db.execute(
            "DELETE FROM fielding_tracking_channels WHERE guild_id=? AND channel_id=?",
            (ctx.guild.id, ctx.channel.id),
        )
        self.bot.db.commit()
        self.enabled_channels.discard(ctx.channel.id)
        self.pending.pop(ctx.channel.id, None)
        await ctx.reply(
            "🛑 Catch tracking stopped in this channel. Recorded fielding stats were kept.",
            mention_author=False,
        )

    @commands.command(name="fieldingtrackingstatus")
    @commands.guild_only()
    async def fielding_tracking_status(self, ctx):
        """Show channels in this server where catch tracking is enabled."""
        rows = self.bot.db.execute(
            "SELECT channel_id FROM fielding_tracking_channels WHERE guild_id=? ORDER BY channel_id",
            (ctx.guild.id,),
        ).fetchall()
        if not rows:
            await ctx.reply(
                "Catch tracking is not enabled in any channel of this server.",
                mention_author=False,
            )
            return
        lines = []
        for row in rows:
            channel_id = int(row[0])
            channel = ctx.guild.get_channel(channel_id)
            if channel is not None:
                lines.append(f"• {channel.mention}")
            else:
                lines.append(f"• Deleted or inaccessible channel (`{channel_id}`)")
        embed = discord.Embed(
            title="🧤 Catch tracking channels",
            description="\n".join(lines),
            color=discord.Color.from_rgb(55, 150, 255),
        )
        await ctx.reply(
            embed=embed,
            mention_author=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    # ------------------------------------------------------------------ stats commands
    async def _ask_number(self, ctx, question):
        """Ask for a non-negative whole number. Returns int, or None on cancel/timeout."""
        await ctx.send(question, allowed_mentions=discord.AllowedMentions.none())

        def check(m):
            return m.author.id == ctx.author.id and m.channel.id == ctx.channel.id

        for _ in range(3):
            try:
                reply = await self.bot.wait_for(
                    "message", check=check, timeout=PROMPT_TIMEOUT_SECONDS
                )
            except Exception:  # asyncio.TimeoutError
                await ctx.send("⏱️ Timed out. No fielding stats were changed.")
                return None
            text = reply.content.strip()
            if text.lower() in ("cancel", "stop"):
                await ctx.send("Cancelled. No fielding stats were changed.")
                return None
            if text.isascii() and text.isdigit():
                value = int(text)
                if value <= MAX_MANUAL_TOTAL:
                    return value
                await ctx.send(
                    f"Please enter a number up to {MAX_MANUAL_TOTAL:,} (or `cancel`)."
                )
            else:
                await ctx.send(
                    "Please enter a whole number of 0 or more (or `cancel`)."
                )
        await ctx.send("Too many invalid answers. No fielding stats were changed.")
        return None

    @commands.command(name="changefieldingstats")
    @commands.guild_only()
    async def change_fielding_stats(self, ctx, member: discord.Member):
        """Set a player's exact total catches caught and dropped."""
        if not await self._tracking_permission(ctx, "changefieldingstats"):
            return
        name = discord.utils.escape_markdown(member.display_name)
        caught = await self._ask_number(
            ctx, f"How many total **catches caught** does **{name}** have? (type `cancel` to stop)"
        )
        if caught is None:
            return
        dropped = await self._ask_number(
            ctx, f"How many total **catches dropped** does **{name}** have? (type `cancel` to stop)"
        )
        if dropped is None:
            return
        self._set_stats(ctx.guild.id, member.id, caught, dropped)
        embed = self._build_stats_embed(ctx.guild, member.id)
        await ctx.send(
            f"✅ Updated fielding stats for **{name}**.",
            embed=embed,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.command(name="fieldingstats", aliases=["fdstats"])
    @commands.guild_only()
    async def fielding_stats(self, ctx, member: discord.Member = None):
        """Paged fielding stats.

        +fieldingstats          -> server overview, your stats, your team comparison
        +fieldingstats <user>   -> their stats, their team comparison
        """
        if member is not None:
            pages = [
                self._build_stats_embed(ctx.guild, member.id, recent=True),
                self._build_team_embed(ctx.guild, member.id),
            ]
        else:
            pages = [
                self._build_overview_embed(ctx.guild),
                self._build_stats_embed(ctx.guild, ctx.author.id, recent=True),
                self._build_team_embed(ctx.guild, ctx.author.id),
            ]

        view = PagedEmbedView(ctx, pages)
        view.message = await ctx.reply(
            embed=pages[0],
            view=view,
            mention_author=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.command(name="resetfieldingstats")
    @commands.guild_only()
    async def reset_fielding_stats(self, ctx):
        """Reset ALL fielding stats for this server (asks for confirmation)."""
        if not await self._tracking_permission(ctx, "resetfieldingstats"):
            return
        view = ResetConfirmView(self, ctx)
        view.message = await ctx.reply(
            "⚠️ This will permanently reset **all fielding stats** for this server. "
            "Batting, bowling and other data are not affected. Continue?",
            view=view,
            mention_author=False,
        )

    # ------------------------------------------------------------------ listener
    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.id != HANDCRICKET_BOT_ID:
            return
        if not message.guild:
            return
        channel_id = message.channel.id
        if channel_id not in self.enabled_channels:
            return

        opportunity = self._parse_opportunity(message)
        if opportunity:
            fielder_id, batsman_id = opportunity
            # A new opportunity always replaces any older pending one.
            self.pending[channel_id] = {
                "fielder_id": fielder_id,
                "batsman_id": batsman_id,
                "message_id": message.id,
                "created": time.monotonic(),
            }
            # Post the fielder's current stats as a new blue embed for this chance.
            await self._send_stats_embed(message.channel, fielder_id)
            return

        result = self._result_kind(message)
        if result is None:
            return

        # Take the pending event out BEFORE doing anything else so one chance
        # can never be counted twice, even if two result messages arrive.
        pending = self.pending.pop(channel_id, None)
        if not pending:
            return
        if time.monotonic() - pending["created"] > PENDING_TTL_SECONDS:
            return

        self._increment(message.guild.id, pending["fielder_id"], result)

        # Optional custom fielder line (fielderlines.py), sent right after the
        # Hand Cricket result message. Any failure here must never affect catch tracking.
        lines_cog = self.bot.get_cog("FielderLines")
        if lines_cog is not None:
            try:
                await lines_cog.send_line(
                    message.channel, pending["fielder_id"], result,
                    batter_id=pending.get("batsman_id"))
            except Exception as exc:
                print(f"[fielding] custom fielder line failed: {type(exc).__name__}: {exc}")

        # Post a separate embed showing updated stats and result colour.
        await self._send_stats_embed(message.channel, pending["fielder_id"], result=result)


async def setup(bot):
    await bot.add_cog(CatchTracker(bot))
