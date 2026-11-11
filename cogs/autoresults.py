"""Auto result poster for GOOGLI MANAGER.

Place at cogs/autoresults.py and add "cogs.autoresults" to bot.py (after cogs.results).
Needs cogs/matchnumbers.py too.

Commands (all use the same OCR_SPACE_API_KEY as +result):

    +autoresult [force]
        Reply to a +schedule post OR a reserve "MATCHDAY" post. For each match in it the bot
        finds the newest Hand Cricket "Result Scorecard" in that match's stadium channel
        (posted after the schedule), reads winner + margin with OCR, shows a preview, and on
        "yes" posts the results in match-number order using the +result format.

    +rebuildresults [#channel]
        Re-does the whole results channel in order: reads the result posts already there,
        adds the RESERVED matches' results (found from the reserve schedule posts), then
        posts everything again from the first match to the last.
        - With no channel: the old result posts made by the bot are deleted and re-posted
          in the results channel.
        - With a channel: nothing is deleted; the full sorted list is posted there.
"""

from __future__ import annotations

import asyncio
import os
import re
import sqlite3
from difflib import SequenceMatcher
from pathlib import Path

import aiohttp
import discord
from discord.ext import commands

from .matchnumbers import RESERVE_DB, reserve_numbers_for
from .results import _load_config, _scorecard_image_candidates
from .utility import owner_or_whitelisted

TEAM_DB = "team_registration.db"
AUTO_DB = "auto_results.db"
SCAN_LIMIT = 300          # messages scanned per stadium channel
MAX_OCR_PER_MATCH = 8     # scorecards OCR'd per match before giving up
HISTORY_LIMIT = 5000      # results-channel messages read by +rebuildresults

MATCH_RE = re.compile(
    r"\*\*Match\s+(\d+)\*\*\s*\n\s*-\s*<@&(\d+)>\s+vs\s+<@&(\d+)>\s+in\s+<#(\d+)>",
    re.IGNORECASE,
)
# Reserve schedule line:  `ID 12` ... \n 🏟️ Host ... <#channel> \n 👥 <@&host> vs <@&opponent>
RESERVE_RE = re.compile(
    r"`ID\s+(\d+)`[^\n]*\n[^\n]*<#(\d+)>[^\n]*\n[^\n]*<@&(\d+)>\s+vs\s+<@&(\d+)>"
)
WIN_RE = re.compile(
    r"won\s+the\s+(?:game|match)\s+by\s+(\d{1,3})\s+(runs?|wickets?)", re.IGNORECASE
)
POST_RE = re.compile(r"^\s*#\s*__Match\s+(\d+)__", re.IGNORECASE)


def _clean(text):
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", str(text).casefold()).split())


def _score(team, ntext):
    """0..1 - how well a team name appears in OCR text (tolerates small OCR typos)."""
    t = _clean(team)
    if not t:
        return 0.0
    if t in ntext:
        return 1.0
    n = len(t.split())
    words = ntext.split()
    best = 0.0
    for i in range(max(1, len(words) - n + 1)):
        best = max(best, SequenceMatcher(None, t, " ".join(words[i:i + n])).ratio())
    return best


def _role_team_names(guild_id):
    if not os.path.exists(TEAM_DB):
        return {}
    conn = sqlite3.connect(TEAM_DB)
    try:
        rows = conn.execute(
            "SELECT s.role_id, t.name FROM registered_teams t "
            "JOIN team_role_settings s ON s.team_id = t.id "
            "WHERE t.guild_id = ? AND LOWER(t.status) = 'approved'",
            (guild_id,),
        ).fetchall()
    finally:
        conn.close()
    return {int(role_id): str(name) for role_id, name in rows}


def _analyse(ocr_text, team_a, team_b):
    """Return (winner, loser, margin) or None if this scorecard isn't this pair."""
    ntext = _clean(ocr_text)
    if _score(team_a, ntext) < 0.75 or _score(team_b, ntext) < 0.75:
        return None
    flat = re.sub(r"\s+", " ", ocr_text)
    m = WIN_RE.search(flat)
    if not m:
        return None
    unit = "runs" if m.group(2).lower().startswith("run") else "wickets"
    margin = f"won by {m.group(1)} {unit}"
    words = _clean(flat[: m.start()]).split()

    def tail_score(team):
        # The winner's name sits immediately before "won the game by".
        t = _clean(team)
        n = len(t.split())
        return SequenceMatcher(None, t, " ".join(words[-n:])).ratio()

    score_a, score_b = tail_score(team_a), tail_score(team_b)
    if max(score_a, score_b) < 0.75 or abs(score_a - score_b) < 0.05:
        return None
    if score_a > score_b:
        return team_a, team_b, margin
    return team_b, team_a, margin


def parse_schedule(message, guild_id):
    """[(match_no, role_a_id, role_b_id, channel_id)] from a +schedule post or a reserve
    MATCHDAY post. For reserves the match number comes from matchnumbers."""
    entries = [(int(n), int(a), int(b), int(c))
               for n, a, b, c in MATCH_RE.findall(message.content or "")]
    embed_text = "\n".join((e.description or "") for e in message.embeds)
    reserve_hits = RESERVE_RE.findall(embed_text)
    if reserve_hits:
        numbers = reserve_numbers_for(guild_id)
        for reserve_id, channel_id, role_a, role_b in reserve_hits:
            number = numbers.get(int(reserve_id))
            if number:
                entries.append((number, int(role_a), int(role_b), int(channel_id)))
    return entries


def result_text(match_no, winner, loser, margin, url):
    return f"# __Match {match_no}__\n**{winner} vs {loser}**\n\n> ***[{winner}]({url}) {margin}***"


class AutoResults(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        conn = sqlite3.connect(AUTO_DB)
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS auto_posted ("
                "guild_id INTEGER NOT NULL, match_no INTEGER NOT NULL, "
                "posted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
                "PRIMARY KEY (guild_id, match_no))"
            )
            conn.commit()
        finally:
            conn.close()

    async def cog_check(self, ctx):
        return await owner_or_whitelisted(ctx)

    # ------------------------------------------------------------------ small helpers
    def _already_posted(self, guild_id):
        conn = sqlite3.connect(AUTO_DB)
        try:
            return {r[0] for r in conn.execute(
                "SELECT match_no FROM auto_posted WHERE guild_id=?", (guild_id,))}
        finally:
            conn.close()

    def _mark_posted(self, guild_id, match_no):
        conn = sqlite3.connect(AUTO_DB)
        try:
            conn.execute("INSERT OR IGNORE INTO auto_posted (guild_id, match_no) VALUES (?, ?)",
                         (guild_id, match_no))
            conn.commit()
        finally:
            conn.close()

    def _result_channel(self, guild):
        channel_id = _load_config().get(str(guild.id))
        channel = guild.get_channel(channel_id) if channel_id else None
        return channel if isinstance(channel, discord.TextChannel) else None

    @staticmethod
    def _new_session():
        return aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=60),
            headers={"User-Agent": "Mozilla/5.0 (compatible; GOOGLI-MANAGER/1.0)",
                     "Referer": "https://discord.com/"},
        )

    @staticmethod
    async def _ocr(session, api_key, image_bytes, filename):
        form = aiohttp.FormData()
        form.add_field("apikey", api_key)
        form.add_field("language", "eng")
        form.add_field("OCREngine", "2")
        form.add_field("scale", "true")
        form.add_field("file", image_bytes, filename=filename,
                       content_type="application/octet-stream")
        try:
            async with session.post("https://api.ocr.space/parse/image", data=form) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError):
            return None
        if data.get("IsErroredOnProcessing"):
            return None
        return " ".join(p.get("ParsedText", "") for p in (data.get("ParsedResults") or []))

    @staticmethod
    async def _download(session, url):
        try:
            async with session.get(url, allow_redirects=True) as resp:
                if resp.status != 200:
                    return None
                body = await resp.read()
                return body if body and len(body) <= 15 * 1024 * 1024 else None
        except (aiohttp.ClientError, TimeoutError):
            return None

    async def _find_results(self, guild, entries, session, api_key, skip=frozenset()):
        """entries: [(match_no, role_a_id, role_b_id, channel_id, after_message)].

        Returns (found, problems); found = {match_no: (winner, loser, margin, image_url, message)}.
        """
        names = _role_team_names(guild.id)
        found, problems, used_messages, ocr_cache = {}, [], set(), {}
        for match_no, role_a_id, role_b_id, channel_id, after_msg in sorted(entries, key=lambda e: e[0]):
            if match_no in skip:
                problems.append(f"Match **{match_no}**: already posted (use `force` to repost).")
                continue
            role_a, role_b = guild.get_role(role_a_id), guild.get_role(role_b_id)
            team_a = names.get(role_a_id) or (role_a.name if role_a else None)
            team_b = names.get(role_b_id) or (role_b.name if role_b else None)
            channel = guild.get_channel(channel_id)
            if not team_a or not team_b or channel is None:
                problems.append(f"Match **{match_no}**: team role or stadium channel not found.")
                continue

            checked, hit = 0, None
            async for msg in channel.history(limit=SCAN_LIMIT, after=after_msg, oldest_first=True):
                if msg.id in used_messages or msg.author.id == self.bot.user.id:
                    continue
                candidates = _scorecard_image_candidates(msg)
                if not candidates:
                    continue
                url, filename = candidates[0]
                if msg.id not in ocr_cache:
                    if checked >= MAX_OCR_PER_MATCH:
                        break
                    checked += 1
                    body = await self._download(session, url)
                    ocr_cache[msg.id] = (await self._ocr(session, api_key, body, filename)
                                         if body else None)
                text = ocr_cache[msg.id]
                if not text:
                    continue
                analysed = _analyse(text, team_a, team_b)
                if analysed:
                    hit = (analysed, url, msg)
                    break
            if hit is None:
                problems.append(f"Match **{match_no}** ({team_a} vs {team_b}): no matching scorecard "
                                f"found in {channel.mention} after the schedule.")
                continue
            (winner, loser, margin), url, msg = hit
            used_messages.add(msg.id)
            found[match_no] = (winner, loser, margin, url, msg)
        return found, problems

    async def _ask_yes(self, ctx, timeout=600):
        def check(m):
            return (m.author.id == ctx.author.id and m.channel.id == ctx.channel.id
                    and m.content.strip().casefold() in {"yes", "no", "cancel"})
        try:
            answer = await self.bot.wait_for("message", check=check, timeout=timeout)
        except TimeoutError:
            await ctx.send("⌛ Timed out; nothing was changed.")
            return False
        if answer.content.strip().casefold() != "yes":
            await ctx.send("❌ Cancelled; nothing was changed.")
            return False
        return True

    # ------------------------------------------------------------------ +autoresult
    @commands.command(name="autoresult", aliases=["autoresults"])
    @commands.guild_only()
    async def autoresult(self, ctx, option: str = ""):
        """Reply to a schedule post: find each match's scorecard and post results in order."""
        force = option.casefold() == "force"
        result_channel = self._result_channel(ctx.guild)
        if result_channel is None:
            await ctx.reply("❌ Set the results channel first with `+setresultchannel #channel`.",
                            mention_author=False)
            return
        api_key = os.getenv("OCR_SPACE_API_KEY")
        if not api_key:
            await ctx.reply("❌ `OCR_SPACE_API_KEY` is missing in the hosting environment.",
                            mention_author=False)
            return
        ref = ctx.message.reference
        if ref is None or ref.message_id is None:
            await ctx.reply("❌ Run `+autoresult` as a reply to the schedule post.",
                            mention_author=False)
            return
        try:
            ref_channel = ctx.guild.get_channel(ref.channel_id) or ctx.channel
            schedule_msg = await ref_channel.fetch_message(ref.message_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            await ctx.reply("❌ I couldn't read the schedule post you replied to.", mention_author=False)
            return
        parsed = parse_schedule(schedule_msg, ctx.guild.id)
        if not parsed:
            await ctx.reply("❌ I couldn't find any matches in that post. Reply to a `+schedule` post "
                            "or a reserve `MATCHDAY` post.", mention_author=False)
            return
        entries = [(n, a, b, c, schedule_msg) for n, a, b, c in parsed]
        posted_before = set() if force else self._already_posted(ctx.guild.id)

        status = await ctx.send(f"⏳ Looking for scorecards for **{len(entries)}** matches...")
        async with self._new_session() as session:
            found, problems = await self._find_results(
                ctx.guild, entries, session, api_key, skip=posted_before)
        if not found:
            await status.edit(content="❌ No scorecards could be matched.\n" +
                              "\n".join(f"• {p}" for p in problems)[:1800])
            return

        lines = [f"**Auto result preview — {len(found)} of {len(entries)} matches ready**"]
        for n in sorted(found):
            winner, loser, margin, _url, msg = found[n]
            lines.append(f"`{n}` 🏆 **{winner}** {margin} vs {loser} · [scorecard]({msg.jump_url})")
        if problems:
            lines.append(f"\n⚠️ **Not ready ({len(problems)}):**")
            lines.extend(f"• {p}" for p in problems)
        lines.append(f"\nReply `yes` to post these in order in {result_channel.mention}, or `no` to cancel. "
                     "(10-minute timeout)")
        await status.edit(content="\n".join(lines)[:1990], allowed_mentions=discord.AllowedMentions.none())
        if not await self._ask_yes(ctx):
            return

        results_cog = self.bot.get_cog("ResultsCog")
        posted = 0
        for n in sorted(found):  # strictly first match -> last match
            winner, loser, margin, url, _msg = found[n]
            try:
                await result_channel.send(result_text(n, winner, loser, margin, url),
                                          allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException as exc:
                await ctx.send(f"❌ Stopped at Match {n}: {exc}. Posted {posted} so far; "
                               "fix it and run `+autoresult` again.")
                return
            self._mark_posted(ctx.guild.id, n)
            if results_cog is not None:
                results_cog._published_results.add((ctx.guild.id, str(n)))
            posted += 1
        await ctx.send(f"✅ Posted **{posted}** result(s) in {result_channel.mention}, in match order."
                       + (f"\n⚠️ {len(problems)} match(es) were skipped (see the preview)." if problems else ""))

    # ------------------------------------------------------------------ +rebuildresults
    async def _reserve_entries(self, guild):
        """Every reserved match from the saved reserve schedule posts:
        [(match_no, role_a, role_b, channel_id, after_message)]."""
        if not RESERVE_DB.is_file():
            return []
        conn = sqlite3.connect(RESERVE_DB)
        try:
            states = conn.execute(
                "SELECT channel_id, message_ids FROM reserve_stage_schedule_state "
                "WHERE is_generated = 1 AND message_ids <> ''").fetchall()
        except sqlite3.Error:
            states = []
        finally:
            conn.close()
        entries, seen = [], set()
        for channel_id, message_ids in states:
            channel = guild.get_channel(int(channel_id)) if channel_id else None
            if channel is None:
                continue
            anchor = None
            for raw_id in str(message_ids).split(","):
                if not raw_id.strip().isdigit():
                    continue
                try:
                    message = await channel.fetch_message(int(raw_id))
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    continue
                anchor = anchor or message  # intro post = earliest, scorecards come after it
                for n, a, b, c in parse_schedule(message, guild.id):
                    if n not in seen:
                        seen.add(n)
                        entries.append((n, a, b, c, anchor))
        return entries

    @commands.command(name="rebuildresults", aliases=["redoresults"])
    @commands.guild_only()
    async def rebuildresults(self, ctx, target: discord.TextChannel = None):
        """Re-post ALL results in match order, adding the reserved matches' results."""
        source = self._result_channel(ctx.guild)
        if source is None:
            await ctx.reply("❌ Set the results channel first with `+setresultchannel #channel`.",
                            mention_author=False)
            return
        api_key = os.getenv("OCR_SPACE_API_KEY")
        if not api_key:
            await ctx.reply("❌ `OCR_SPACE_API_KEY` is missing in the hosting environment.",
                            mention_author=False)
            return
        destination = target or source
        delete_old = destination.id == source.id

        status = await ctx.send("⏳ Reading the existing results...")
        existing, old_messages = {}, []
        async for msg in source.history(limit=HISTORY_LIMIT, oldest_first=True):
            hit = POST_RE.match(msg.content or "")
            if hit and msg.author.id == self.bot.user.id:
                existing[int(hit.group(1))] = msg.content  # a later post replaces an earlier one
                old_messages.append(msg)
        if not existing:
            await status.edit(content="❌ I found no `# __Match N__` result posts made by me in "
                                      f"{source.mention}.")
            return

        await status.edit(content=f"⏳ Found **{len(existing)}** results. Looking for reserved matches' "
                                  "scorecards...")
        reserve_entries = await self._reserve_entries(ctx.guild)
        new_results, problems = {}, []
        if reserve_entries:
            wanted = [e for e in reserve_entries if e[0] not in existing]
            async with self._new_session() as session:
                new_results, problems = await self._find_results(ctx.guild, wanted, session, api_key)

        merged = dict(existing)
        for n, (winner, loser, margin, url, _msg) in new_results.items():
            merged[n] = result_text(n, winner, loser, margin, url)
        order = sorted(merged)

        lines = [f"**Rebuild preview**",
                 f"Existing results: **{len(existing)}** · Reserved results added: **{len(new_results)}** · "
                 f"Total: **{len(merged)}** (Match {order[0]} → Match {order[-1]})"]
        if new_results:
            lines.append("Added: " + ", ".join(f"`{n}`" for n in sorted(new_results)))
        if problems:
            lines.append(f"\n⚠️ **Reserved matches not added ({len(problems)}):**")
            lines.extend(f"• {p}" for p in problems[:10])
            if len(problems) > 10:
                lines.append(f"• ...and {len(problems) - 10} more")
        if delete_old:
            lines.append(f"\n🗑️ This will **delete my {len(old_messages)} old result posts** in "
                         f"{source.mention} and re-post all {len(merged)} in order.")
        else:
            lines.append(f"\nNothing will be deleted. All {len(merged)} results will be posted in "
                         f"{destination.mention}.")
        lines.append("Reply `yes` to continue or `no` to cancel. (10-minute timeout)")
        await status.edit(content="\n".join(lines)[:1990], allowed_mentions=discord.AllowedMentions.none())
        if not await self._ask_yes(ctx):
            return

        if delete_old:
            deleted = 0
            for msg in old_messages:
                try:
                    await msg.delete()
                    deleted += 1
                except discord.NotFound:
                    deleted += 1
                except discord.HTTPException:
                    pass
                await asyncio.sleep(0.4)
            if deleted < len(old_messages):
                await ctx.send(f"⚠️ I could only delete {deleted}/{len(old_messages)} old posts "
                               "(missing Manage Messages?). Not re-posting, to avoid duplicates.")
                return

        results_cog = self.bot.get_cog("ResultsCog")
        posted = 0
        for n in order:
            try:
                await destination.send(merged[n], allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException as exc:
                await ctx.send(f"❌ Stopped at Match {n}: {exc}. Posted {posted}/{len(order)}. "
                               "Run `+rebuildresults #other-channel` to post the full list somewhere safe.")
                return
            self._mark_posted(ctx.guild.id, n)
            if results_cog is not None:
                results_cog._published_results.add((ctx.guild.id, str(n)))
            posted += 1
            await asyncio.sleep(0.6)
        await ctx.send(f"✅ Re-posted **{posted}** results in {destination.mention}, in order "
                       f"(Match {order[0]} → {order[-1]}). After this, run `+syncresult` from the first "
                       "post if you want the points table to pick up the reserved results.")


async def setup(bot):
    await bot.add_cog(AutoResults(bot))
