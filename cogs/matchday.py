"""Matchday flow.

1. Post the daily fixtures message (Match N / @TeamRole vs @TeamRole in #stadium).
2. Reply to it with +openmatch  (or +openmatch 77 for a single match).
   The bot unlocks each stadium, finds both teams' captains and pings ONLY the captains.
3. The bot then reads the two captains' messages in that stadium. When both agree on a
   time (same time, or one proposes and the other accepts) it LOCKS the stadium and posts
   a no-ping message with Yes / No buttons that only the confirmation role
   (+setconfirmrole) can press (buttons never expire, work once).
4. Yes -> the match is saved, announced (both teams pinged) and each captain is asked about
   DMs, exactly like /match_schedule. No -> the stadium is unlocked and the captains pick
   a new time.

Times are India time (IST) unless the captain names another timezone.
"""

import re
import sqlite3
import time
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands

from .utility import owner_or_whitelisted

IST = ZoneInfo("Asia/Kolkata")
NEGOTIATION_TTL = 24 * 60 * 60          # stop listening 24h after +openmatch

# Keep these in sync with scheduling.py
MATCH_EMOJI = "<a:arrow_arrow:1565966619291811984>"
VS_EMOJI = "<:VS128:1559620966709993472>"
HEADER_EMOJI = "<:emoji_60:1562481891037290637>"
TIME_EMOJI = "<:time:1565970550067691710>"

# --------------------------------------------------------------------------- time parsing
TZ_NAMES = {
    "ist": "Asia/Kolkata", "pkt": "Asia/Karachi", "bdt": "Asia/Dhaka",
    "utc": "UTC", "gmt": "UTC",
    "est": "America/New_York", "edt": "America/New_York",
    "pst": "America/Los_Angeles", "pdt": "America/Los_Angeles",
    "mst": "America/Denver", "mdt": "America/Denver",
    "cet": "Europe/Paris", "cest": "Europe/Paris", "msk": "Europe/Moscow",
    "sgt": "Asia/Singapore", "jst": "Asia/Tokyo", "kst": "Asia/Seoul", "wib": "Asia/Jakarta",
    "aest": "Australia/Sydney", "aedt": "Australia/Sydney",
    "nzst": "Pacific/Auckland", "nzdt": "Pacific/Auckland", "sast": "Africa/Johannesburg",
}
TZ_RE = re.compile(r"\b(" + "|".join(TZ_NAMES) + r")\b", re.I)
OFFSET_RE = re.compile(r"\b(?:utc|gmt)\s*([+-])\s*(\d{1,2})(?::?([0-5]\d))?(?!\d)", re.I)
TOMORROW_RE = re.compile(r"\b(?:tomorrow|tommorow|tomorow|tmrw|tmr)\b", re.I)

_NOT_PREV = r"(?<![\d:.,;/\-])"
_AP = r"(?P<ap>[ap])\.?m\.?(?![a-z])"
# 8:15 | 8 15 | 8.15 | 8,15 | 8;15  (+ optional am/pm)
HM_RE = re.compile(
    _NOT_PREV + r"(?P<h>\d{1,2})(?:\s*[:.,;]\s*|\s+)(?P<m>[0-5]\d)(?!\d)(?:\s*" + _AP + r")?", re.I)
# 8pm | 8 pm | 8 p.m.
H_AP_RE = re.compile(_NOT_PREV + r"(?P<h>\d{1,2})\s*" + _AP, re.I)
# "at 8" / "@8" / "around 8" / "lets do 9" / "make it 9" / "8 baje"
H_AT_RE = re.compile(
    r"(?:\bat|@|\bby|\baround|\bdo|\bplay|\bstart|\bmake\s+it|\bkeep|\bset|\bfix|\bcome|\bjoin|\btime)"
    r"\s*(?P<h>\d{1,2})(?![\d:.,;]*\d)"
    r"(?!\s*(?:players?|overs?|runs?|wickets?|balls?|mins?|minutes?|hours?|hrs?)\b)", re.I)
H_BAJE_RE = re.compile(_NOT_PREV + r"(?P<h>\d{1,2})\s*(?:baje|bje|bajey)\b", re.I)
WHOLE_NUM_RE = re.compile(r"^\W*(?P<h>\d{1,2})\W*$")


def _to_24h(hour, minute, ap):
    """Bare 1-11 means PM, 13-23 is 24-hour time, bare 12 means midnight."""
    if ap:
        if not 1 <= hour <= 12:
            return None
        return hour % 12 + (12 if ap == "p" else 0), minute
    if hour > 23:
        return None
    if 1 <= hour <= 11:
        return hour + 12, minute
    if hour == 12:
        return 0, minute
    return hour, minute


def extract_times(text, now_utc=None):
    """Every time mentioned in `text`, as aware datetimes, in order of appearance."""
    now_utc = now_utc or datetime.now(timezone.utc)
    cleaned = re.sub(r"<[^>]+>|https?://\S+", " ", text or "")

    tz = IST
    offset = OFFSET_RE.search(cleaned)
    if offset and int(offset.group(2)) <= 14:
        sign = 1 if offset.group(1) == "+" else -1
        tz = timezone(sign * timedelta(hours=int(offset.group(2)), minutes=int(offset.group(3) or 0)))
        cleaned = cleaned[:offset.start()] + " " * (offset.end() - offset.start()) + cleaned[offset.end():]
    else:
        named = TZ_RE.search(cleaned)
        if named:
            tz = ZoneInfo(TZ_NAMES[named.group(1).lower()])
            cleaned = cleaned[:named.start()] + " " * (named.end() - named.start()) + cleaned[named.end():]

    taken, found = [], []

    def free(start, end):
        return all(end <= a or start >= b for a, b in taken)

    for pattern in (HM_RE, H_AP_RE, H_AT_RE, H_BAJE_RE):
        for match in pattern.finditer(cleaned):
            if not free(*match.span()):
                continue
            groups = match.groupdict()
            parsed = _to_24h(int(groups["h"]), int(groups.get("m") or 0), (groups.get("ap") or "").lower() or None)
            if parsed is None:
                continue
            taken.append(match.span())
            found.append((match.start(), parsed))

    if not found:
        whole = WHOLE_NUM_RE.match(cleaned)
        if whole:
            parsed = _to_24h(int(whole.group("h")), 0, None)
            if parsed:
                found.append((0, parsed))

    tomorrow = bool(TOMORROW_RE.search(cleaned))
    now_local = now_utc.astimezone(tz)
    results = []
    for _, (hour, minute) in sorted(found):
        candidate = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if tomorrow:
            candidate += timedelta(days=1)
        elif candidate < now_local - timedelta(minutes=10):
            candidate += timedelta(days=1)      # that time already passed today
        results.append(candidate)
    return results


ACCEPT_WORDS = {
    "yes", "yeah", "yea", "yep", "yup", "ye", "ya", "yaa", "yh", "y", "ok", "okay", "okk", "okey",
    "okie", "kk", "k", "kay", "accept", "accepted", "approve", "approved", "confirm", "confirmed",
    "fine", "sure", "agree", "agreed", "deal", "done", "fixed", "locked", "cool", "alright",
    "aight", "np", "ofc", "absolutely", "definitely", "correct", "noted",
    "haan", "han", "haanji", "hanji", "hnji", "theek", "thik", "sahi", "bilkul", "chalo",
    "chalega", "works", "sounds", "good", "great", "perfect",
}
# Stretched spellings: yesss, yaaa, okkk, okayyy, kkk, haaan ...
ACCEPT_STRETCH_RE = re.compile(
    r"^(?:y+e+s+|y+e+a+h?|y+a+h*|y+e+p+|y+u+p+|y+e+|o+k+(?:a+y+)?|o+k+i+e+|k+|h+a+a*n+(?:j+i+)?)$")
# Words that reject the time in a message ("can't do 8").
NEGATIVE_WORDS = {
    "cant", "can't", "cannot", "dont", "don't", "wont", "won't", "not", "never",
    "doesnt", "doesn't", "isnt", "isn't", "nahi", "nhi", "nahin", "mat",
}
# Words that only block a plain acceptance ("no" -> not an accept; "no, 9 pm" is still a proposal).
SOFT_NEGATIVE_WORDS = {"no", "nope", "nah"}
ACCEPT_EMOJI = ("👍", "✅", "🆗", "👌", "🤝", "✔️", "☑️", "💯", "🫡")


def classify_message(text, now_utc=None):
    """('time', datetime) | ('accept', None) | ('none', None)."""
    times = extract_times(text, now_utc)
    lowered = re.sub(r"<[^>]+>", " ", (text or "").lower())
    polite_accept = "no problem" in lowered or "no worries" in lowered
    lowered = lowered.replace("no problem", " ").replace("no worries", " ")
    tokens = re.findall(r"[a-z']+", lowered)
    negative = any(token in NEGATIVE_WORDS for token in tokens)

    if times:
        if negative and len(times) == 1:
            return "none", None             # e.g. "can't do 8"
        return "time", (times[-1] if negative else times[0])

    accepted = (polite_accept
                or any(token in ACCEPT_WORDS or ACCEPT_STRETCH_RE.match(token) for token in tokens)
                or any(e in (text or "") for e in ACCEPT_EMOJI))
    soft_negative = any(token in SOFT_NEGATIVE_WORDS for token in tokens)
    if accepted and not negative and not soft_negative and len(tokens) <= 8:
        return "accept", None
    return "none", None


# --------------------------------------------------------------------------- fixtures parsing
MATCH_HEAD_RE = re.compile(r"match\s*(\d+)", re.I)
ROLE_RE = re.compile(r"<@&(\d+)>")
CHANNEL_RE = re.compile(r"<#(\d+)>")
GROUP_RE = re.compile(r"group\s*_*\s*([A-Za-z0-9]+)", re.I)
ROUND_RE = re.compile(r"round\s*(\d+)", re.I)


def parse_fixtures(text):
    """Returns (round_no, [ {match_no, group, role_a, role_b, channel_id}, ... ])."""
    round_match = ROUND_RE.search(text)
    round_no = round_match.group(1) if round_match else None
    groups = [(m.start(), m.group(1).upper()) for m in GROUP_RE.finditer(text)]
    heads = list(MATCH_HEAD_RE.finditer(text))
    fixtures = []
    for index, head in enumerate(heads):
        end = heads[index + 1].start() if index + 1 < len(heads) else len(text)
        segment = text[head.end():end]
        roles = ROLE_RE.findall(segment)
        channels = CHANNEL_RE.findall(segment)
        if len(roles) < 2 or not channels:
            continue
        group = next((name for pos, name in reversed(groups) if pos < head.start()), None)
        fixtures.append({
            "match_no": head.group(1), "group": group,
            "role_a": int(roles[0]), "role_b": int(roles[1]),   # a 3rd role (e.g. "Rest:") is ignored
            "channel_id": int(channels[0]),
        })
    return round_no, fixtures


# --------------------------------------------------------------------------- message pieces
def _code(name):
    # Backticks inside a team name would break the inline-code formatting.
    return str(name).replace("`", "'")


def title_line(name_a, name_b):
    return f"# {HEADER_EMOJI} `{_code(name_a)}` {VS_EMOJI} `{_code(name_b)}` {HEADER_EMOJI}"


def header_line(round_no, group, match_no):
    return " · ".join(part for part in (
        f"Round {round_no}" if round_no else None,
        f"Group {group}" if group else None,
        f"Match {match_no}" if match_no else None) if part)


class ApprovalView(discord.ui.View):
    # Yes / No buttons for the confirmation role. No timeout, and re-attached after restarts.
    def __init__(self, cog, negotiation_id):
        super().__init__(timeout=None)
        self.cog, self.negotiation_id = cog, negotiation_id
        yes = discord.ui.Button(label="Yes", style=discord.ButtonStyle.success,
                                custom_id=f"mdapprove:yes:{negotiation_id}")
        no = discord.ui.Button(label="No", style=discord.ButtonStyle.danger,
                               custom_id=f"mdapprove:no:{negotiation_id}")
        yes.callback, no.callback = self.on_yes, self.on_no
        self.add_item(yes)
        self.add_item(no)

    async def on_yes(self, interaction):
        await self.cog.handle_approval(interaction, self.negotiation_id, True)

    async def on_no(self, interaction):
        await self.cog.handle_approval(interaction, self.negotiation_id, False)


# --------------------------------------------------------------------------- cog
class MatchDay(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.db = bot.db
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS matchday_negotiations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                match_no TEXT, round_no TEXT, group_name TEXT,
                role_a_id INTEGER NOT NULL, role_b_id INTEGER NOT NULL,
                team_a_name TEXT NOT NULL, team_b_name TEXT NOT NULL,
                captain_a_id INTEGER NOT NULL, captain_b_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                proposal_a INTEGER, proposal_b INTEGER, last_proposer TEXT,
                agreed_ts INTEGER, match_id TEXT,
                opened_by INTEGER NOT NULL, opened_at REAL NOT NULL
            )""")
        self.db.commit()
        # Columns added later (safe to run every start).
        for column in ("original_send INTEGER", "approval_message_id INTEGER"):
            try:
                self.db.execute(f"ALTER TABLE matchday_negotiations ADD COLUMN {column}")
                self.db.commit()
            except sqlite3.OperationalError:
                pass                            # column already exists
        # channel_id -> negotiation id, so ordinary chat costs nothing to ignore.
        self.active = {
            row["channel_id"]: row["id"]
            for row in self.db.execute(
                "SELECT id, channel_id FROM matchday_negotiations WHERE status = 'open' ORDER BY id")
        }

    async def cog_load(self):
        # Re-attach the Yes/No buttons of confirmations that were waiting during a restart.
        rows = self.db.execute(
            "SELECT id, approval_message_id FROM matchday_negotiations "
            "WHERE status = 'awaiting' AND approval_message_id IS NOT NULL").fetchall()
        for row in rows:
            self.bot.add_view(ApprovalView(self, row["id"]), message_id=row["approval_message_id"])

    # ------------------------------------------------------------------ helpers
    async def _allowed(self, ctx, name):
        if await owner_or_whitelisted(ctx, command_name=name, app_owner=True):
            return True
        await ctx.reply("Only the bot owner or a role whitelisted for this command can use it.",
                        mention_author=False, allowed_mentions=discord.AllowedMentions.none())
        return False

    def _confirm_role_id(self, guild_id):
        row = self.db.execute("SELECT value FROM settings WHERE key = ?",
                              (f"matchday_confirm_role_{guild_id}",)).fetchone()
        return int(row["value"]) if row else None

    async def _member(self, guild, user_id):
        member = guild.get_member(user_id)
        if member is None:
            try:
                member = await guild.fetch_member(user_id)
            except discord.HTTPException:
                member = None
        return member

    async def _log(self, guild, text):
        scheduling = self.bot.get_cog("Scheduling")
        if scheduling is not None:
            await scheduling.send_log(guild, text)

    def _supersede_open(self, channel_id):
        """A fresh +openmatch replaces whatever was running in that stadium."""
        rows = self.db.execute(
            "SELECT id, match_id FROM matchday_negotiations "
            "WHERE channel_id = ? AND status IN ('open', 'awaiting', 'approved', 'confirmed')",
            (channel_id,)).fetchall()
        for row in rows:
            self.db.execute("UPDATE matchday_negotiations SET status = 'superseded' WHERE id = ?", (row["id"],))
            if row["match_id"]:
                self.db.execute("UPDATE matches SET status = 'cancelled' WHERE id = ? AND status = 'scheduled'",
                                (row["match_id"],))
        self.db.commit()
        self.active.pop(channel_id, None)

    # ------------------------------------------------------------------ commands
    @commands.command(name="setconfirmrole")
    @commands.guild_only()
    async def setconfirmrole(self, ctx, role: discord.Role = None):
        """+setconfirmrole @role (or the role's ID / name) — the role that approves agreed times."""
        if not await self._allowed(ctx, "setconfirmrole"):
            return
        none = discord.AllowedMentions.none()
        if role is None:
            current = self._confirm_role_id(ctx.guild.id)
            text = (f"Confirmation role: <@&{current}>" if current else "No confirmation role is set.")
            await ctx.reply(text + "\nUsage: `+setconfirmrole <role ID or name>` (an ID/name doesn't ping the role)",
                            mention_author=False, allowed_mentions=none)
            return
        self.db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                        (f"matchday_confirm_role_{ctx.guild.id}", str(role.id)))
        self.db.commit()
        await ctx.reply(f"✅ Confirmation role set to {role.mention}. When both captains agree on a time I'll "
                        "lock the stadium and show this role Yes/No buttons (no ping).",
                        mention_author=False, allowed_mentions=none)

    async def _fetch_replied(self, ctx):
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


    def _fixture_message_text(self, message):
        """Combine normal message text and embed text for fixture parsing."""
        parts = [message.content or ""]
        for embed in message.embeds:
            if embed.title:
                parts.append(str(embed.title))
            if embed.description:
                parts.append(str(embed.description))
            for field in embed.fields:
                parts.append(str(field.name or ""))
                parts.append(str(field.value or ""))
        return "\n".join(parts)

    @commands.command(name="forceannounce")
    @commands.guild_only()
    async def forceannounce(self, ctx, *, when_text: str = ""):
        """Bot owner only: reply to a fixtures embed/message with +forceannounce <time>."""
        none = discord.AllowedMentions.none()

        if not await self.bot.is_owner(ctx.author):
            await ctx.reply(
                "Only the bot owner can use this command.",
                mention_author=False,
                allowed_mentions=none,
            )
            return

        if ctx.message.reference is None:
            await ctx.reply(
                "Reply to the fixtures embed/message with `+forceannounce <time>`.",
                mention_author=False,
                allowed_mentions=none,
            )
            return

        replied = await self._fetch_replied(ctx)
        if replied is None:
            await ctx.reply(
                "I couldn't fetch the message you replied to.",
                mention_author=False,
                allowed_mentions=none,
            )
            return

        times = extract_times(when_text)
        if not times:
            await ctx.reply(
                "Give me a valid time, for example: `+forceannounce 8 pm`, `+forceannounce 8:15 pm` or `+forceannounce tomorrow 8 pm`.",
                mention_author=False,
                allowed_mentions=none,
            )
            return

        start_time = int(times[0].timestamp())
        if start_time <= int(time.time()):
            await ctx.reply(
                "That time has already passed. Use a future time.",
                mention_author=False,
                allowed_mentions=none,
            )
            return

        round_no, fixtures = parse_fixtures(self._fixture_message_text(replied))
        if not fixtures:
            await ctx.reply(
                "I couldn't find any matches in that message/embed. Each match needs `Match N`, two team roles, and a stadium channel.",
                mention_author=False,
                allowed_mentions=none,
            )
            return

        scheduling = self.bot.get_cog("Scheduling")
        if scheduling is None:
            await ctx.reply(
                "The scheduling module isn't loaded.",
                mention_author=False,
                allowed_mentions=none,
            )
            return

        results = []
        announced = 0

        for fixture in fixtures:
            label = f"Match {fixture['match_no']}"
            channel = ctx.guild.get_channel(fixture["channel_id"])
            team_a = ctx.guild.get_role(fixture["role_a"])
            team_b = ctx.guild.get_role(fixture["role_b"])

            if not isinstance(channel, discord.TextChannel):
                results.append(f"⚠️ **{label}** — stadium channel not found.")
                continue
            if team_a is None or team_b is None:
                results.append(f"⚠️ **{label}** — one or both team roles were not found.")
                continue

            match_id = uuid.uuid4().hex[:8].upper()
            current = channel.overwrites_for(ctx.guild.default_role).send_messages
            original_send = None if current is None else int(current)

            try:
                self.db.execute(
                    "INSERT INTO matches (id, guild_id, team_a_id, team_b_id, channel_id, start_time, status, "
                    "created_by, created_at, original_send_messages, announcement_message_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, 'scheduled', ?, ?, ?, NULL)",
                    (
                        match_id,
                        ctx.guild.id,
                        team_a.id,
                        team_b.id,
                        channel.id,
                        start_time,
                        ctx.author.id,
                        time.time(),
                        original_send,
                    ),
                )
                self.db.commit()

                announcement = await channel.send(
                    f"{team_a.mention} {VS_EMOJI} {team_b.mention}\n\n"
                    f"{MATCH_EMOJI} **Match at** <t:{start_time}:F>",
                    allowed_mentions=scheduling.role_mentions(team_a, team_b),
                )

                self.db.execute(
                    "UPDATE matches SET announcement_message_id = ? WHERE id = ?",
                    (announcement.id, match_id),
                )
                self.db.commit()

                announced += 1
                results.append(
                    f"✅ **{label}** — {team_a.name} vs {team_b.name} announced in {channel.mention}."
                )

                await self._log(
                    ctx.guild,
                    f"⚡ **FORCE ANNOUNCED** — {label}\n"
                    f"**Teams:** {team_a.name} vs {team_b.name}\n"
                    f"**Channel:** {channel.mention}\n"
                    f"**Match at:** <t:{start_time}:F>\n"
                    f"**Forced by:** <@{ctx.author.id}>\n"
                    f"**Match ID:** `{match_id}`",
                )

            except discord.HTTPException as exc:
                self.db.execute(
                    "UPDATE matches SET status = 'cancelled', error = ? WHERE id = ?",
                    (f"Force announcement failed: {exc}", match_id),
                )
                self.db.commit()
                results.append(f"⚠️ **{label}** — database entry created, but the announcement failed: {exc}")
            except Exception as exc:
                self.db.rollback()
                results.append(f"⚠️ **{label}** — failed: {exc}")

        await ctx.reply(
            f"**Force announced {announced}/{len(fixtures)} match(es)** for <t:{start_time}:F>\n"
            + "\n".join(results)[:1700],
            mention_author=False,
            allowed_mentions=none,
        )

    @commands.command(name="openmatch")
    @commands.guild_only()
    async def openmatch(self, ctx, *, which: str = ""):
        """Reply to the fixtures message: +openmatch  (all matches) or +openmatch 77 (one match)."""
        if not await self._allowed(ctx, "openmatch"):
            return
        none = discord.AllowedMentions.none()
        if ctx.message.reference is None:
            await ctx.reply("Reply to the fixtures message with `+openmatch` "
                            "(or `+openmatch <match number>` for a single match).",
                            mention_author=False, allowed_mentions=none)
            return
        replied = await self._fetch_replied(ctx)
        if replied is None:
            await ctx.reply("I couldn't fetch the message you replied to.", mention_author=False)
            return

        round_no, fixtures = parse_fixtures(replied.content or "")
        wanted = re.search(r"\d+", which or "")
        if wanted:
            fixtures = [f for f in fixtures if f["match_no"] == wanted.group(0)]
        if not fixtures:
            await ctx.reply("I couldn't find any matches in that message"
                            + (f" for match {wanted.group(0)}." if wanted else
                               ". Each match needs `Match N` and a line with two team roles and `in #channel`."),
                            mention_author=False, allowed_mentions=none)
            return

        lines, opened = [], 0
        for fixture in fixtures:
            ok, text = await self._open_one(ctx, fixture, round_no)
            opened += ok
            lines.append(("✅ " if ok else "⚠️ ") + text)
        await ctx.reply(f"**Opened {opened}/{len(fixtures)} match(es)**\n" + "\n".join(lines)[:1800],
                        mention_author=False, allowed_mentions=none)

    async def _open_one(self, ctx, fixture, round_no):
        guild = ctx.guild
        label = f"Match {fixture['match_no']}"
        channel = guild.get_channel(fixture["channel_id"])
        if not isinstance(channel, discord.TextChannel):
            return False, f"**{label}** — stadium channel <#{fixture['channel_id']}> not found."

        scheduling = self.bot.get_cog("Scheduling")
        if scheduling is None:
            return False, f"**{label}** — the scheduling module isn't loaded."

        teams = []
        for role_id in (fixture["role_a"], fixture["role_b"]):
            role = guild.get_role(role_id)
            role_name = role.name if role else str(role_id)
            try:
                linked = scheduling.teams_for_role(guild.id, role_id)
            except Exception as exc:  # database problems must not crash the command
                return False, f"**{label}** — couldn't read the team database ({exc})."
            if len(linked) != 1:
                reason = "no approved team is linked to" if not linked else "more than one team is linked to"
                return False, f"**{label}** — {reason} role **{scheduling._safe_name(role_name)}** (`+setteamrole`)."
            teams.append(linked[0])
        (_, name_a, cap_a_id), (_, name_b, cap_b_id) = teams
        if cap_a_id == cap_b_id:
            return False, f"**{label}** — both teams have the same captain."
        cap_a, cap_b = await self._member(guild, cap_a_id), await self._member(guild, cap_b_id)
        if cap_a is None or cap_b is None:
            missing = name_a if cap_a is None else name_b
            return False, f"**{label}** — the captain of **{scheduling._safe_name(missing)}** is not in this server."

        # Unlock the stadium.
        overwrite = channel.overwrites_for(guild.default_role)
        overwrite.send_messages = True
        try:
            await channel.set_permissions(guild.default_role, overwrite=overwrite,
                                          reason=f"Match {fixture['match_no']} opened by {ctx.author}")
        except discord.HTTPException as exc:
            return False, f"**{label}** — couldn't unlock {channel.mention}: {exc}"

        self._supersede_open(channel.id)
        cursor = self.db.execute(
            "INSERT INTO matchday_negotiations (guild_id, channel_id, match_no, round_no, group_name, "
            "role_a_id, role_b_id, team_a_name, team_b_name, captain_a_id, captain_b_id, status, "
            "opened_by, opened_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)",
            (guild.id, channel.id, fixture["match_no"], round_no, fixture["group"],
             fixture["role_a"], fixture["role_b"], name_a, name_b, cap_a_id, cap_b_id,
             ctx.author.id, time.time()))
        self.db.commit()
        self.active[channel.id] = cursor.lastrowid

        header = header_line(round_no, fixture["group"], fixture["match_no"])
        embed = discord.Embed(
            description=(
                f"{title_line(name_a, name_b)}\n"
                f"**{header}**\n\n"
                f"🏟️ This stadium is now **unlocked**.\n"
                f"{TIME_EMOJI} Captains, agree on a match time here — just say a time like `8 pm`, `8:15`, "
                f"`at 9`, `9 baje` or `tomorrow 9 pm` (IST unless you mention another timezone, "
                f"e.g. `9 pm EST`).\n"
                f"✅ To accept the other captain's time just reply `yes`, `ok`, `k`, `haan` or 👍. "
                f"When you both agree, it's confirmed automatically."),
            color=discord.Color.blurple())
        try:
            await channel.send(
                f"{cap_a.mention} {cap_b.mention}", embed=embed,
                allowed_mentions=discord.AllowedMentions(users=[cap_a, cap_b], roles=False,
                                                         everyone=False, replied_user=False))
        except discord.HTTPException as exc:
            return False, f"**{label}** — unlocked {channel.mention} but couldn't post there: {exc}"

        await self._log(guild, f"🏟️ **MATCH OPENED** — {label}\n**Teams:** {name_a} vs {name_b}\n"
                               f"**Channel:** {channel.mention}\n**Opened by:** <@{ctx.author.id}>")
        return True, f"**{label}** — {channel.mention} unlocked, {cap_a.mention} & {cap_b.mention} pinged."

    # ------------------------------------------------------------------ captain time tracking
    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot or message.guild is None:
            return
        negotiation_id = self.active.get(message.channel.id)
        if negotiation_id is None:
            return
        row = self.db.execute("SELECT * FROM matchday_negotiations WHERE id = ? AND status = 'open'",
                              (negotiation_id,)).fetchone()
        if row is None:
            self.active.pop(message.channel.id, None)
            return
        if time.time() - row["opened_at"] > NEGOTIATION_TTL:
            self.db.execute("UPDATE matchday_negotiations SET status = 'expired' WHERE id = ?", (row["id"],))
            self.db.commit()
            self.active.pop(message.channel.id, None)
            return

        if message.author.id == row["captain_a_id"]:
            me, other = "a", "b"
        elif message.author.id == row["captain_b_id"]:
            me, other = "b", "a"
        else:
            return                              # only the two captains count

        kind, when = classify_message(message.content)
        if kind == "none":
            return

        if kind == "time":
            proposed = int(when.timestamp())
            self.db.execute(f"UPDATE matchday_negotiations SET proposal_{me} = ?, last_proposer = ? WHERE id = ?",
                            (proposed, me, row["id"]))
            self.db.commit()
            if row[f"proposal_{other}"] != proposed:
                try:
                    await message.add_reaction("🕒")     # "I understood that time"
                except discord.HTTPException:
                    pass
                return
            agreed = proposed
        else:                                   # a plain "yes / ok / accept"
            agreed = row[f"proposal_{other}"]
            if row["last_proposer"] != other or agreed is None:
                return                          # nothing from the other captain to accept
        await self._confirm(row, agreed, message)

    def _can_answer(self, member, guild_id):
        role_id = self._confirm_role_id(guild_id)
        if role_id:
            return any(role.id == role_id for role in member.roles)
        return member.guild_permissions.manage_guild     # no role set yet: admins only

    def _approval_embed(self, row, state, by=None, note=None, locked=True):
        ts = int(row["agreed_ts"])
        ist_text = datetime.fromtimestamp(ts, IST).strftime("%I:%M %p").lstrip("0")
        lines = [
            title_line(row["team_a_name"], row["team_b_name"]),
            f"**{header_line(row['round_no'], row['group_name'], row['match_no'])}**",
            "",
            f"{TIME_EMOJI} **Match Time:** <t:{ts}:F> (<t:{ts}:R>) · **{ist_text} IST**",
            "✅ Both teams matched times.",
        ]
        if locked and state != "declined":
            lines.append("🔒 This stadium is now **locked**.")
        lines.append("")
        if state == "awaiting":
            role_id = self._confirm_role_id(row["guild_id"])
            if role_id:
                lines.append(f"⏳ Waiting for <@&{role_id}> to confirm this match.")
            else:
                lines.append("⚠️ No confirmation role is set (`+setconfirmrole`). Until one is set, members with "
                             "**Manage Server** can answer.")
            color = discord.Color.gold()
        elif state == "approved":
            lines.append(f"✅ Confirmed by {by}. The match is scheduled.")
            color = discord.Color.green()
        else:
            lines.append(f"❌ Not confirmed by {by}." + (f" {note}" if note else ""))
            color = discord.Color.red()
        return discord.Embed(description="\n".join(lines), color=color)

    async def _confirm(self, row, agreed_ts, message):
        claimed = self.db.execute(
            "UPDATE matchday_negotiations SET status = 'awaiting', agreed_ts = ? WHERE id = ? AND status = 'open'",
            (agreed_ts, row["id"]))
        self.db.commit()
        if claimed.rowcount == 0:
            return                              # already confirmed by the other captain's message
        self.active.pop(row["channel_id"], None)

        guild, channel = message.guild, message.channel
        # Lock the stadium (remember what it was so it can be restored).
        overwrite = channel.overwrites_for(guild.default_role)
        original = None if overwrite.send_messages is None else int(overwrite.send_messages)
        self.db.execute("UPDATE matchday_negotiations SET original_send = ? WHERE id = ?", (original, row["id"]))
        self.db.commit()
        overwrite.send_messages = False
        locked = True
        try:
            await channel.set_permissions(guild.default_role, overwrite=overwrite,
                                          reason=f"Match {row['match_no']} time agreed")
        except discord.HTTPException as exc:
            locked = False
            await self._log(guild, f"⚠️ Couldn't lock {channel.mention} for Match {row['match_no']}: {exc}")

        fresh = self.db.execute("SELECT * FROM matchday_negotiations WHERE id = ?", (row["id"],)).fetchone()
        try:
            sent = await channel.send(
                embed=self._approval_embed(fresh, "awaiting", locked=locked),
                view=ApprovalView(self, row["id"]),
                allowed_mentions=discord.AllowedMentions.none())      # nobody is pinged
            self.db.execute("UPDATE matchday_negotiations SET approval_message_id = ? WHERE id = ?",
                            (sent.id, row["id"]))
            self.db.commit()
        except discord.HTTPException as exc:
            await self._log(guild, f"⚠️ Couldn't post the confirmation message for Match {row['match_no']}: {exc}")
        await self._log(guild, f"🤝 **MATCH TIME AGREED** — Match {row['match_no']} (waiting for the confirmation role)\n"
                               f"**Teams:** {row['team_a_name']} vs {row['team_b_name']}\n"
                               f"**Channel:** {channel.mention}\n**Time:** <t:{agreed_ts}:F>")

    async def handle_approval(self, interaction, negotiation_id, approved):
        none = discord.AllowedMentions.none()
        row = self.db.execute("SELECT * FROM matchday_negotiations WHERE id = ?", (negotiation_id,)).fetchone()
        if row is None or row["status"] != "awaiting":
            await interaction.response.send_message(
                "This confirmation was already answered or is no longer active.", ephemeral=True)
            try:
                await interaction.message.edit(view=None)
            except (discord.HTTPException, AttributeError):
                pass
            return
        guild = interaction.guild
        if guild is None or not isinstance(interaction.user, discord.Member) \
                or not self._can_answer(interaction.user, guild.id):
            await interaction.response.send_message(
                "Only the confirmation role can answer this.", ephemeral=True)
            return
        scheduling = self.bot.get_cog("Scheduling")
        team_a, team_b = guild.get_role(row["role_a_id"]), guild.get_role(row["role_b_id"])
        if approved and (scheduling is None or team_a is None or team_b is None):
            await interaction.response.send_message(
                "I can't schedule this right now (scheduling module or a team role is missing).", ephemeral=True)
            return
        note = None
        if approved and row["agreed_ts"] <= time.time():
            approved, note = False, "That time has already passed."

        # Claim it first so the buttons can only ever be used once.
        claim = self.db.execute(
            "UPDATE matchday_negotiations SET status = ? WHERE id = ? AND status = 'awaiting'",
            ("approved" if approved else "declined", row["id"]))
        self.db.commit()
        if claim.rowcount == 0:
            await interaction.response.send_message("This confirmation was already answered.", ephemeral=True)
            return
        by = interaction.user.mention
        await interaction.response.edit_message(
            embed=self._approval_embed(row, "approved" if approved else "declined", by=by, note=note),
            view=None, allowed_mentions=none)

        channel = interaction.channel
        if approved:
            await self._schedule_match(interaction, guild, channel, row, scheduling, team_a, team_b)
        else:
            await self._reopen(guild, channel, row, interaction.user, note)

    async def _schedule_match(self, interaction, guild, channel, row, scheduling, team_a, team_b):
        match_id = uuid.uuid4().hex[:8].upper()
        agreed_ts = int(row["agreed_ts"])
        self.db.execute(
            "INSERT INTO matches (id, guild_id, team_a_id, team_b_id, channel_id, start_time, status, "
            "created_by, created_at, original_send_messages, announcement_message_id) "
            "VALUES (?, ?, ?, ?, ?, ?, 'scheduled', ?, ?, ?, NULL)",
            (match_id, guild.id, row["role_a_id"], row["role_b_id"], channel.id, agreed_ts,
             row["opened_by"], time.time(), row["original_send"]))
        self.db.execute("UPDATE matchday_negotiations SET match_id = ? WHERE id = ?", (match_id, row["id"]))
        self.db.commit()

        # The same announcement /match_schedule posts: both teams pinged.
        announcement_id = None
        try:
            announcement = await channel.send(
                f"{team_a.mention} {VS_EMOJI} {team_b.mention}\n\n"
                f"{MATCH_EMOJI} **Match at** <t:{agreed_ts}:F>",
                allowed_mentions=scheduling.role_mentions(team_a, team_b))
            announcement_id = announcement.id
            self.db.execute("UPDATE matches SET announcement_message_id = ? WHERE id = ?", (announcement_id, match_id))
        except discord.HTTPException as exc:
            self.db.execute("UPDATE matches SET error = ? WHERE id = ?", (f"Announcement failed: {exc}", match_id))
        self.db.commit()

        await self._log(
            guild,
            f"📋 **MATCH SCHEDULED**\n**Match ID:** `{match_id}`\n"
            f"**Team A:** {team_a.name}\n**Team B:** {team_b.name}\n"
            f"**Channel:** <#{channel.id}>\n**Match at:** <t:{agreed_ts}:F>\n"
            f"**Confirmed by:** <@{interaction.user.id}>\n"
            f"**Announcement message ID:** `{announcement_id}`")

        # Ask each captain whether to DM their team, as before.
        prompts_sent, errors = await scheduling.send_captain_prompts(guild, channel, scheduling.get_match(match_id))
        if errors:
            await self._log(guild, f"⚠️ **CAPTAIN PROMPT ERROR — `{match_id}`**\n" + "\n".join(errors))
            try:
                await interaction.followup.send(
                    f"⚠️ Match scheduled (`{match_id}`), but some captain prompts failed:\n"
                    + "\n".join(f"• {e}" for e in errors)[:1800],
                    ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                pass

    async def _reopen(self, guild, channel, row, user, note):
        # "No": unlock the stadium and let the captains agree on a new time.
        original = row["original_send"]
        overwrite = channel.overwrites_for(guild.default_role)
        overwrite.send_messages = None if original is None else bool(original)
        try:
            await channel.set_permissions(guild.default_role, overwrite=overwrite,
                                          reason=f"Match {row['match_no']} time not confirmed")
        except discord.HTTPException:
            pass
        self.db.execute(
            "UPDATE matchday_negotiations SET status = 'open', proposal_a = NULL, proposal_b = NULL, "
            "last_proposer = NULL, agreed_ts = NULL, approval_message_id = NULL, opened_at = ? WHERE id = ?",
            (time.time(), row["id"]))
        self.db.commit()
        self.active[channel.id] = row["id"]
        captains = [discord.Object(id=row["captain_a_id"]), discord.Object(id=row["captain_b_id"])]
        try:
            await channel.send(
                f"<@{row['captain_a_id']}> <@{row['captain_b_id']}>\n"
                f"❌ That time was not confirmed.{' ' + note if note else ''}\n"
                f"🏟️ This stadium is **unlocked** again. {TIME_EMOJI} Please agree on a new match time.",
                allowed_mentions=discord.AllowedMentions(users=captains, roles=False,
                                                         everyone=False, replied_user=False))
        except discord.HTTPException:
            pass
        await self._log(guild, f"❌ **MATCH TIME NOT CONFIRMED** — Match {row['match_no']}\n"
                               f"**Teams:** {row['team_a_name']} vs {row['team_b_name']}\n"
                               f"**Channel:** {channel.mention}\n**Answered by:** <@{user.id}>")


async def setup(bot):
    await bot.add_cog(MatchDay(bot))
