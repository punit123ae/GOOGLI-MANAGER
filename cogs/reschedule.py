"""Reschedule an announced match without deleting and re-announcing it.

Reply to the match announcement with the new time:

    +reschedule 9:30 pm
    +reschedule tomorrow 8 pm
    +reschedule 25 nov 9 pm          (also: 2026-11-25 9pm, 25/11 9pm, nov 25 9pm)
    +reschedule 9 pm EST             (IST unless another timezone is named)

If the announcement is gone you can use the match ID instead of replying:

    +reschedule A1B2C3D4 9:30 pm

What changes, all at once:
  * matches.start_time       -> /match_list, /match_info and the scheduler read this, so the
                                "MATCH TIME!" ping and the channel unlock move to the new time
  * the announcement message -> edited in place (re-posted only if it was deleted)
  * a short "rescheduled" notice in the match channel (pings both team roles)
  * DMs already sent to players -> found in the players' DMs and edited
  * the matchday confirmation embed, the matchday negotiation record, an audit row,
    and a line in the match logs channel
Pending captain "DM your team?" prompts need nothing: they build the DM from the saved time
when the captain presses Yes.
"""

import asyncio
import re
import sqlite3
import time
import traceback
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands

from .matchday import IST, extract_times
from .scheduling import HEADER_EMOJI, MATCH_EMOJI, PIN_EMOJI, TIME_EMOJI, VS_EMOJI
from .utility import owner_or_whitelisted

# Post a short notice (pinging both team roles) in the match channel after a reschedule.
# An edited message does not notify anyone, so this is what tells the teams.
PING_TEAMS_ON_RESCHEDULE = True

DM_NOTE_PREFIX = "⏰ *Rescheduled"

# --------------------------------------------------------------------------- date parsing
MONTH_NUM = {name: number for number, name in
             enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split(), 1)}
_MON = (r"(?P<mon>jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|"
        r"aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)")
_ORD = r"(?:st|nd|rd|th)?"
_YEAR = r"(?:[\s,]+(?P<y>\d{4})(?!\d))?"

DATE_PATTERNS = (
    # 2026-11-25
    re.compile(r"(?<![\d-])(?P<y>\d{4})-(?P<m>\d{1,2})-(?P<d>\d{1,2})(?![\d-])"),
    # 25/11, 25/11/2026 (day first, as in India)
    re.compile(r"(?<![\d/])(?P<d>\d{1,2})/(?P<m>\d{1,2})(?:/(?P<y>\d{4}|\d{2}))?(?![\d/])"),
    # 25 nov, 25th november 2026
    re.compile(r"(?<![\w:])(?P<d>\d{1,2})" + _ORD + r"\s*(?:of\s+)?" + _MON + r"\b" + _YEAR, re.I),
    # nov 25, november 25th, nov 25 2026
    re.compile(r"\b" + _MON + r"\.?\s*(?P<d>\d{1,2})" + _ORD + r"(?![\d:.])" + _YEAR, re.I),
)


def pull_date(text):
    """Find one calendar date in `text`.

    Returns ((year_or_None, month, day) or None, text_with_the_date_removed).
    Raises ValueError for something date-shaped that is not a real date (e.g. 31/02).
    """
    for pattern in DATE_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        groups = match.groupdict()
        month = MONTH_NUM[groups["mon"][:3].lower()] if groups.get("mon") else int(groups["m"])
        day = int(groups["d"])
        year = int(groups["y"]) if groups.get("y") else None
        if year is not None and year < 100:
            year += 2000
        if not (1 <= month <= 12 and 1 <= day <= 31):
            raise ValueError("That date doesn't look right.")
        remaining = text[:match.start()] + " " + text[match.end():]
        return (year, month, day), remaining
    return None, text


def parse_when(text, now_utc=None):
    """Turn free text like '25 nov 9 pm' into an aware datetime.

    Returns (datetime, None) or (None, error_message).
    """
    now_utc = now_utc or datetime.now(timezone.utc)
    try:
        date_part, rest = pull_date(text or "")
    except ValueError as exc:
        return None, str(exc)

    times = extract_times(rest, now_utc)
    if not times:
        return None, "I couldn't find a time in that."
    when = times[0]
    if date_part is None:
        return when, None

    year, month, day = date_part
    local_now = now_utc.astimezone(when.tzinfo)
    try:
        candidate = when.replace(year=year or local_now.year, month=month, day=day)
        if year is None and candidate < local_now - timedelta(days=1):
            candidate = candidate.replace(year=candidate.year + 1)   # "1 jan" said in December
    except ValueError:
        return None, "That date doesn't exist."
    return candidate, None


# --------------------------------------------------------------------------- cog
def _announcement_text(team_a, team_b, ts):
    return f"{team_a.mention} {VS_EMOJI} {team_b.mention}\n\n{MATCH_EMOJI} **Match at** <t:{ts}:F>"


def _ist_clock(ts):
    return datetime.fromtimestamp(ts, IST).strftime("%I:%M %p").lstrip("0")


class Reschedule(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.db = bot.db
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS match_reschedules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                match_id TEXT NOT NULL,
                guild_id INTEGER NOT NULL,
                old_start_time REAL NOT NULL,
                new_start_time REAL NOT NULL,
                changed_by INTEGER NOT NULL,
                changed_at REAL NOT NULL
            )""")
        self.db.commit()
        self._match_locks = {}      # match id -> lock, so two reschedules can't interleave
        self._dm_locks = {}         # match id -> lock, so DM edits are applied in order
        self._tasks = set()         # keeps background DM jobs alive until they finish

    # ------------------------------------------------------------------ helpers
    def _lock(self, table, match_id):
        return table.setdefault(match_id, asyncio.Lock())

    async def _log(self, guild, text):
        scheduling = self.bot.get_cog("Scheduling")
        if scheduling is not None:
            await scheduling.send_log(guild, text)

    def _find_match(self, guild_id, text, reference):
        """(match_row, remaining_text, problem). `problem` is a user-facing message or None."""
        if reference is not None and reference.message_id is not None:
            row = self.db.execute(
                "SELECT * FROM matches WHERE guild_id = ? AND announcement_message_id = ?",
                (guild_id, reference.message_id)).fetchone()
            if row is None:
                return None, text, ("That message isn't a match announcement I know about. Reply to the "
                                    "announcement itself, or use `+reschedule <MATCH_ID> <time>`.")
            return row, text, None
        token = re.match(r"^\s*([0-9A-Fa-f]{8})\b\s*(.*)$", text or "", re.S)
        if token:
            row = self.db.execute("SELECT * FROM matches WHERE id = ? AND guild_id = ?",
                                  (token.group(1).upper(), guild_id)).fetchone()
            if row is not None:
                return row, token.group(2), None
        return None, text, None

    # ------------------------------------------------------------------ the command
    @commands.command(name="reschedule", aliases=["changetime", "retime"])
    @commands.guild_only()
    async def reschedule(self, ctx, *, when_text: str = ""):
        """Reply to a match announcement with +reschedule <new time> to move the match."""
        none = discord.AllowedMentions.none()
        if not await owner_or_whitelisted(ctx, command_name="reschedule", app_owner=True):
            await ctx.reply("Only the bot owner or a role whitelisted for this command can use it.",
                            mention_author=False, allowed_mentions=none)
            return

        usage = ("Reply to the match announcement with the new time, for example:\n"
                 "`+reschedule 9:30 pm` · `+reschedule tomorrow 8 pm` · `+reschedule 25 nov 9 pm`\n"
                 "Or without replying: `+reschedule <MATCH_ID> 9:30 pm`. Times are IST unless you add "
                 "a timezone (e.g. `9 pm EST`).")
        row, text, problem = self._find_match(ctx.guild.id, when_text, ctx.message.reference)
        if problem:
            await ctx.reply(problem, mention_author=False, allowed_mentions=none)
            return
        if row is None:
            await ctx.reply(usage, mention_author=False, allowed_mentions=none)
            return
        if row["status"] != "scheduled":
            await ctx.reply(f"Match `{row['id']}` is already `{row['status']}`, so its time can't be changed.",
                            mention_author=False, allowed_mentions=none)
            return

        when, error = parse_when(text)
        if when is None:
            await ctx.reply(f"❌ {error}\n{usage}", mention_author=False, allowed_mentions=none)
            return
        new_ts = int(when.timestamp())
        if new_ts <= int(time.time()):
            await ctx.reply(f"❌ <t:{new_ts}:F> has already passed. Give a future time.",
                            mention_author=False, allowed_mentions=none)
            return
        if new_ts == int(row["start_time"]):
            await ctx.reply(f"Match `{row['id']}` is already set for <t:{new_ts}:F>. Nothing to change.",
                            mention_author=False, allowed_mentions=none)
            return

        outcome = await self.apply_reschedule(ctx.guild, row["id"], new_ts, ctx.author.id)
        if outcome["error"]:
            await ctx.reply(f"❌ {outcome['error']}", mention_author=False, allowed_mentions=none)
            return

        lines = [
            f"✅ **Match `{row['id']}` rescheduled**",
            f"🕒 ~~<t:{outcome['old_ts']}:F>~~ → **<t:{new_ts}:F>** (<t:{new_ts}:R>)",
            f"📢 Announcement: {outcome['announcement']}",
            f"🔔 Channel notice: {outcome['notice']}",
            "📋 Saved time updated — `/match_list` and `/match_info` show the new time; the "
            "**MATCH TIME!** ping and channel unlock now happen then.",
        ]
        if outcome["approval"]:
            lines.append("🧾 Confirmation message updated.")
        lines.append(f"✉️ Player DMs: {outcome['dm_summary']}")
        reply = await ctx.reply("\n".join(lines)[:1990], mention_author=False, allowed_mentions=none)

        if outcome["dm_job"] is not None:
            task = asyncio.create_task(self._dm_job_then_report(ctx.guild, reply, lines, outcome))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    # ------------------------------------------------------------------ core
    async def apply_reschedule(self, guild, match_id, new_ts, actor_id):
        """Move a scheduled match to `new_ts` everywhere. Returns a dict describing what happened."""
        outcome = {"error": None, "old_ts": None, "announcement": "—", "notice": "—",
                   "approval": False, "dm_summary": "none sent for this match", "dm_job": None}
        none = discord.AllowedMentions.none()

        async with self._lock(self._match_locks, match_id):
            row = self.db.execute("SELECT * FROM matches WHERE id = ?", (match_id,)).fetchone()
            if row is None or row["status"] != "scheduled":
                outcome["error"] = "That match has just started or been cancelled, so it can't be moved."
                return outcome
            if new_ts <= int(time.time()):
                outcome["error"] = "That time has already passed."
                return outcome
            old_ts = int(row["start_time"])
            outcome["old_ts"] = old_ts

            # 1) The saved time. Everything time-driven (scheduler ping + unlock, /match_list,
            #    /match_info, the DM text built when a captain presses Yes) reads this value.
            changed = self.db.execute(
                "UPDATE matches SET start_time = ? WHERE id = ? AND status = 'scheduled'",
                (float(new_ts), match_id))
            if changed.rowcount == 0:
                self.db.rollback()
                outcome["error"] = "That match has just started or been cancelled, so it can't be moved."
                return outcome
            self.db.execute(
                "INSERT INTO match_reschedules (match_id, guild_id, old_start_time, new_start_time, "
                "changed_by, changed_at) VALUES (?, ?, ?, ?, ?, ?)",
                (match_id, guild.id, float(old_ts), float(new_ts), actor_id, time.time()))
            try:   # matches made through +openmatch also keep the agreed time here
                self.db.execute("UPDATE matchday_negotiations SET agreed_ts = ? WHERE match_id = ?",
                                (new_ts, match_id))
            except sqlite3.OperationalError:
                pass
            self.db.commit()

            channel = guild.get_channel(row["channel_id"])
            team_a, team_b = guild.get_role(row["team_a_id"]), guild.get_role(row["team_b_id"])
            scheduling = self.bot.get_cog("Scheduling")
            role_mentions = (scheduling.role_mentions(team_a, team_b)
                             if scheduling and team_a and team_b else none)
            old_tag, new_tag = f"<t:{old_ts}:F>", f"<t:{new_ts}:F>"

            # 2) The announcement message.
            repost = False
            if not isinstance(channel, discord.TextChannel):
                outcome["announcement"] = "⚠️ match channel not found"
            elif not row["announcement_message_id"]:
                repost = True
            else:
                try:
                    message = await channel.fetch_message(row["announcement_message_id"])
                    content = message.content or ""
                    if old_tag in content:
                        content = content.replace(old_tag, new_tag)
                    elif team_a and team_b:
                        content = _announcement_text(team_a, team_b, new_ts)
                    await message.edit(content=content, allowed_mentions=none)
                    outcome["announcement"] = "edited"
                except discord.NotFound:
                    repost = True
                except (discord.Forbidden, discord.HTTPException) as exc:
                    outcome["announcement"] = f"⚠️ couldn't edit it ({type(exc).__name__})"
            if repost and isinstance(channel, discord.TextChannel) and team_a and team_b:
                try:
                    message = await channel.send(_announcement_text(team_a, team_b, new_ts),
                                                 allowed_mentions=role_mentions)
                    self.db.execute("UPDATE matches SET announcement_message_id = ? WHERE id = ?",
                                    (message.id, match_id))
                    self.db.commit()
                    outcome["announcement"] = "original was deleted — posted a new one"
                except discord.HTTPException as exc:
                    outcome["announcement"] = f"⚠️ original was deleted and re-posting failed ({exc})"

            # 3) A visible notice, because an edited message doesn't notify anyone.
            if repost:
                outcome["notice"] = "not needed (the new announcement already pinged both teams)"
            elif isinstance(channel, discord.TextChannel) and team_a and team_b:
                try:
                    await channel.send(
                        f"# {HEADER_EMOJI} `MATCH RESCHEDULED` {HEADER_EMOJI}\n"
                        f"{team_a.mention} {VS_EMOJI} {team_b.mention}\n"
                        f"{TIME_EMOJI}  ~~{old_tag}~~ → **{new_tag}** (<t:{new_ts}:R>)",
                        allowed_mentions=role_mentions if PING_TEAMS_ON_RESCHEDULE else none)
                    outcome["notice"] = "posted" + (" (teams pinged)" if PING_TEAMS_ON_RESCHEDULE else "")
                except discord.HTTPException as exc:
                    outcome["notice"] = f"⚠️ couldn't post it ({exc})"

            # 4) The matchday confirmation embed ("Match Time: …") for matches made via +openmatch.
            outcome["approval"] = await self._update_approval_embed(guild, match_id, old_ts, new_ts)

            # 5) Player DMs: only teams whose captain pressed Yes have any.
            prompts = self.db.execute(
                "SELECT * FROM match_dm_prompts WHERE match_id = ? AND status = 'yes'",
                (match_id,)).fetchall()
            waiting = self.db.execute(
                "SELECT COUNT(*) FROM match_dm_prompts WHERE match_id = ? AND status = 'pending'",
                (match_id,)).fetchone()[0]
            parts = []
            if prompts:
                parts.append(f"updating the DMs already sent ({len(prompts)} team(s))…")
                outcome["dm_job"] = (match_id, [int(p["role_id"]) for p in prompts], old_ts, new_ts)
            if waiting:
                parts.append(f"{waiting} captain prompt(s) still pending — they'll send the new time")
            if parts:
                outcome["dm_summary"] = "; ".join(parts)

            await self._log(
                guild,
                f"🔁 **MATCH RESCHEDULED**\n**Match ID:** `{match_id}`\n"
                f"**Teams:** {team_a.name if team_a else row['team_a_id']} vs "
                f"{team_b.name if team_b else row['team_b_id']}\n"
                f"**Channel:** <#{row['channel_id']}>\n"
                f"**Old time:** {old_tag}\n**New time:** {new_tag}\n"
                f"**Changed by:** <@{actor_id}>\n**Announcement:** {outcome['announcement']}")
        return outcome

    async def _update_approval_embed(self, guild, match_id, old_ts, new_ts):
        try:
            negotiation = self.db.execute(
                "SELECT channel_id, approval_message_id FROM matchday_negotiations "
                "WHERE match_id = ? AND approval_message_id IS NOT NULL", (match_id,)).fetchone()
        except sqlite3.OperationalError:
            return False
        if negotiation is None:
            return False
        channel = guild.get_channel(negotiation["channel_id"])
        if not isinstance(channel, discord.TextChannel):
            return False
        try:
            message = await channel.fetch_message(negotiation["approval_message_id"])
            if not message.embeds:
                return False
            embed = message.embeds[0].copy()
            text = embed.description or ""
            text = text.replace(f"<t:{old_ts}:", f"<t:{new_ts}:")
            text = text.replace(f"**{_ist_clock(old_ts)} IST**", f"**{_ist_clock(new_ts)} IST**")
            embed.description = text
            await message.edit(embed=embed, allowed_mentions=discord.AllowedMentions.none())
            return True
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return False

    # ------------------------------------------------------------------ DMs
    async def _dm_job_then_report(self, guild, reply, lines, outcome):
        match_id, role_ids, old_ts, new_ts = outcome["dm_job"]
        try:
            async with self._lock(self._dm_locks, match_id):
                edited, sent, failed = await self._update_dms(guild, match_id, role_ids, old_ts, new_ts)
        except Exception:
            traceback.print_exc()
            summary = "⚠️ something went wrong while updating the DMs (see the console)."
        else:
            summary = (f"edited **{edited}**, sent a fresh notice to **{sent}** "
                       f"(old DM not found), failed/unavailable **{failed}**")
            await self._log(guild, f"✉️ **RESCHEDULE DMs — `{match_id}`**\n"
                                   f"✏️ Edited: **{edited}**\n📨 New notice sent: **{sent}**\n"
                                   f"⚠️ Failed/unavailable: **{failed}**")
        final = [line for line in lines if not line.startswith("✉️ Player DMs:")]
        final.append(f"✉️ Player DMs: {summary}")
        try:
            await reply.edit(content="\n".join(final)[:1990], allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            pass

    async def _update_dms(self, guild, match_id, role_ids, old_ts, new_ts):
        row = self.db.execute("SELECT * FROM matches WHERE id = ?", (match_id,)).fetchone()
        channel = guild.get_channel(row["channel_id"]) if row else None
        team_a = guild.get_role(row["team_a_id"]) if row else None
        team_b = guild.get_role(row["team_b_id"]) if row else None
        if row is None or channel is None or team_a is None or team_b is None:
            return 0, 0, 0
        wanted = set(role_ids)
        members = {}
        try:
            async for member in guild.fetch_members(limit=None):
                if member.bot:
                    continue
                if any(role.id in wanted for role in member.roles):
                    members[member.id] = member
        except discord.HTTPException:
            return 0, 0, len(members)

        old_tag, new_tag = f"<t:{old_ts}:F>", f"<t:{new_ts}:F>"
        name_a, name_b = team_a.name.replace("`", "'"), team_b.name.replace("`", "'")
        fresh = (f"# {HEADER_EMOJI} `{name_a}` {VS_EMOJI} `{name_b}` {HEADER_EMOJI}\n"
                 f"⏰ **The match time has changed.**\n"
                 f"{TIME_EMOJI}  **Match Time:** {new_tag}\n"
                 f"{PIN_EMOJI} **Match Channel:** [Join Match]({channel.jump_url})")
        edited = sent = failed = 0
        for member in members.values():
            result = await self._update_member_dm(member, channel.jump_url, old_tag, new_tag, fresh)
            if result == "edited":
                edited += 1
            elif result == "sent":
                sent += 1
            else:
                failed += 1
            await asyncio.sleep(1)      # same gentle pace as the original DMs
        return edited, sent, failed

    async def _update_member_dm(self, member, jump_url, old_tag, new_tag, fresh_text):
        """Edit this bot's earlier match DM to `member`; if it can't be found, send a new notice."""
        no_mentions = discord.AllowedMentions.none()
        try:
            dm = member.dm_channel or await member.create_dm()
            found = False
            async for message in dm.history(limit=100):
                if message.author.id != self.bot.user.id:
                    continue
                content = message.content or ""
                if old_tag not in content or jump_url not in content:
                    continue
                body = [line for line in content.replace(old_tag, new_tag).split("\n")
                        if not line.startswith(DM_NOTE_PREFIX)]
                body.append(f"{DM_NOTE_PREFIX} — previously {old_tag}*")
                await message.edit(content="\n".join(body), allowed_mentions=no_mentions)
                found = True
            if found:
                return "edited"
            await member.send(fresh_text, allowed_mentions=no_mentions)
            return "sent"
        except (discord.Forbidden, discord.HTTPException):
            return "failed"


async def setup(bot):
    await bot.add_cog(Reschedule(bot))
