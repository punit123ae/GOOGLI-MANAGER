"""Test commands for the two-sided comparison images.

+testcompareteams <team> | <team>   compare two teams (one name = your team vs that team)
+testh2h @player [@player]          player head-to-head

Owner or whitelisted roles only (whitelist the command names above). Pages are drawn when opened.
"""
import asyncio
import io
import re
import time

import discord
from discord.ext import commands

from . import versus_cards as vi
from .lb import _totals
from .utility import is_bot_owner_id

BOT_OWNER_ID = 1014568928682459198


class VersusView(discord.ui.View):
    """Named page buttons; each page is drawn the first time it is opened."""

    def __init__(self, ctx, renderer, pages, filename):
        super().__init__(timeout=180)
        self.owner_id = ctx.author.id
        self.renderer = renderer
        self.filename = filename
        self.message = None
        self.current = 1
        self.buttons = {}
        for number, (label, emoji) in enumerate(pages, start=1):
            button = discord.ui.Button(label=label, emoji=emoji, row=0, style=discord.ButtonStyle.secondary)
            button.callback = self._make_callback(number)
            self.buttons[number] = button
            self.add_item(button)
        self._mark()

    def _mark(self):
        for number, button in self.buttons.items():
            button.style = discord.ButtonStyle.primary if number == self.current else discord.ButtonStyle.secondary

    def _make_callback(self, number):
        async def callback(interaction):
            previous = self.current
            self.current = number
            self._mark()
            await interaction.response.defer()
            try:
                png = await asyncio.to_thread(self.renderer.render, number)
            except Exception as exc:
                print(f"[testversus] page render failed: {type(exc).__name__}: {exc}", flush=True)
                self.current = previous
                self._mark()
                await interaction.followup.send("I couldn't draw that page. Check the bot logs.", ephemeral=True)
                return
            await interaction.edit_original_response(
                attachments=[discord.File(io.BytesIO(png), filename=self.filename)], view=self)
        return callback

    async def interaction_check(self, interaction):
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message("Only the person who ran the command can switch pages.", ephemeral=True)
        return False

    async def on_timeout(self):
        if self.message is not None:
            try:
                await self.message.edit(view=None)
            except Exception:
                pass


class TestVersus(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def _allowed(self, ctx, name):
        if is_bot_owner_id(self.bot, ctx.author.id) or await self.bot.is_owner(ctx.author):
            return True
        try:
            from .utility import owner_or_whitelisted
            if ctx.guild is not None and await owner_or_whitelisted(ctx, name):
                return True
        except Exception:
            pass
        await ctx.send(f"❌ Only the bot owner or a role whitelisted for `+{name}` can use this command.")
        return False

    @staticmethod
    async def _avatar(user, size=256):
        try:
            return await asyncio.wait_for(user.display_avatar.replace(size=size, static_format="png").read(), timeout=8)
        except Exception:
            return None

    # ------------------------------------------------------------------ teams
    def _teams_cog(self):
        for cog in self.bot.cogs.values():
            if hasattr(cog, "_team_profile_data") and hasattr(cog, "_resolve_team"):
                return cog
        return None

    @staticmethod
    def _ordered_ids(cog, team):
        rows = cog.db.execute("SELECT user_id FROM team_players WHERE team_id=? ORDER BY added_at", (team["id"],)).fetchall()
        return list(dict.fromkeys([int(team["captain_id"])] + [int(r["user_id"]) for r in rows]))

    @staticmethod
    def _team_of(cog, guild_id, user_id):
        row = cog.db.execute(
            """SELECT * FROM registered_teams WHERE guild_id=? AND status='approved'
               AND (captain_id=? OR id IN (SELECT team_id FROM team_players WHERE user_id=?)) LIMIT 1""",
            (guild_id, user_id, user_id)).fetchone()
        return row

    @commands.command(name="testcompareteams")
    @commands.guild_only()
    async def test_compare_teams(self, ctx, *, teams: str):
        """Test: compare two teams. Usage: +testcompareteams <team> | <team>"""
        if not await self._allowed(ctx, "testcompareteams"):
            return
        cog = self._teams_cog()
        if cog is None:
            await ctx.send("The team system isn't loaded.")
            return
        parts = [p.strip() for p in re.split(r"\s*(?:\||,|\s+vs\.?\s+|\s+v\s+)\s*", teams, flags=re.I) if p.strip()]
        found = []
        if len(parts) == 1:
            own = self._team_of(cog, ctx.guild.id, ctx.author.id)
            if own is None:
                await ctx.send("Give two teams: `+testcompareteams Team A | Team B`.")
                return
            found.append(own)
        for query in parts[:2]:
            team, error = cog._resolve_team(ctx.guild.id, query)
            if error == "short":
                await ctx.send(f"Type at least the first **4 letters** of `{query}`.")
                return
            if error == "multiple":
                await ctx.send(f"More than one team matches `{query}`: " + ", ".join(r["name"] for r in team[:8]))
                return
            if not team:
                await ctx.send(f"I couldn't find a team matching `{query}`.")
                return
            found.append(team)
        if len(found) != 2 or int(found[0]["id"]) == int(found[1]["id"]):
            await ctx.send("Pick two different teams.")
            return

        loading = await ctx.send("⏳ Comparing the teams...")
        started = time.perf_counter()
        try:
            datas = await asyncio.gather(*(
                cog._team_profile_data(ctx, t, self._ordered_ids(cog, t), fetch_avatars=False) for t in found))
            # only the six leaders need an avatar
            wanted = {}
            for data in datas:
                for item in data["leaders"]:
                    player = item.get("player")
                    if player:
                        wanted[player["uid"]] = player
            members = {uid: ctx.guild.get_member(uid) for uid in wanted}
            avatars = await asyncio.gather(*(
                (cog._fetch_avatar(members[uid]) if members[uid] else asyncio.sleep(0)) for uid in wanted))
            for (uid, player), blob in zip(wanted.items(), avatars):
                player["avatar_bytes"] = blob or None
            a, b = datas
            left, right, rows, edges = vi.team_inputs(a, b)
            leaders = vi.team_leader_inputs(a, b)
            renderer = vi.Renderer({
                1: lambda: vi.render_overview("TEAM COMPARISON", left, right, rows, edges, "GOOGLI MANAGER  •  TEAM COMPARISON"),
                2: lambda: vi.render_leaders("TEAM LEADERS", left, right, leaders, "GOOGLI MANAGER  •  TEAM COMPARISON"),
            })
            png = await asyncio.to_thread(renderer.render, 1)
            view = VersusView(ctx, renderer, [("Overview", "📊"), ("Key Players", "⭐")], "compare.png")
            await loading.delete()
            view.message = await ctx.send(file=discord.File(io.BytesIO(png), filename="compare.png"), view=view)
            print(f"[testversus] compareteams ready in {time.perf_counter()-started:.1f}s", flush=True)
        except Exception as exc:
            print(f"[testversus] compareteams failed: {type(exc).__name__}: {exc}", flush=True)
            await loading.edit(content="I couldn't build the comparison. Check the bot logs.")

    # ------------------------------------------------------------------ players
    def _match_rows(self, guild_id, user_id):
        try:
            rows = self.bot.db.execute(
                """SELECT p.runs, p.balls_faced, p.runs_conceded, p.balls_bowled, p.wickets, p.not_out
                   FROM player_match_stats p JOIN stats_imports i ON i.source_message_id = p.source_message_id
                   WHERE i.guild_id = ? AND p.user_id = ?
                     AND COALESCE(i.competition, 'MAIN TOURNAMENT') = 'MAIN TOURNAMENT'""",
                (int(guild_id), int(user_id))).fetchall()
        except Exception:
            return []
        return [{"runs": int(r[0]), "balls_faced": int(r[1]), "runs_conceded": int(r[2]),
                 "balls_bowled": int(r[3]), "wickets": int(r[4]), "not_out": int(r[5])} for r in rows]

    def _fielding(self, guild_id, user_id):
        try:
            row = self.bot.db.execute(
                "SELECT catches_caught, catches_dropped FROM fielding_stats WHERE guild_id=? AND user_id=?",
                (int(guild_id), int(user_id))).fetchone()
        except Exception:
            return 0, 0
        return (int(row[0]), int(row[1])) if row else (0, 0)

    def _opposed(self, guild_id, a_id, b_id, teams):
        try:
            matches = self.bot.db.execute(
                """SELECT i.imported_at, a.runs, a.balls_faced, a.runs_conceded, a.balls_bowled, a.wickets, a.not_out,
                          b.runs, b.balls_faced, b.runs_conceded, b.balls_bowled, b.wickets, b.not_out,
                          (SELECT team_name FROM match_teams WHERE source_message_id = a.source_message_id AND user_id = a.user_id),
                          (SELECT team_name FROM match_teams WHERE source_message_id = b.source_message_id AND user_id = b.user_id)
                   FROM player_match_stats a
                   JOIN player_match_stats b ON b.source_message_id = a.source_message_id
                   JOIN stats_imports i ON i.source_message_id = a.source_message_id
                   WHERE i.guild_id = ? AND a.user_id = ? AND b.user_id = ?
                     AND COALESCE(i.competition, 'MAIN TOURNAMENT') = 'MAIN TOURNAMENT'
                   ORDER BY i.imported_at DESC, a.source_message_id DESC""",
                (int(guild_id), int(a_id), int(b_id))).fetchall()
        except Exception:
            return []
        out = []
        for m in matches:
            a_team = m[13] if m[13] is not None else teams.get(int(a_id))
            b_team = m[14] if m[14] is not None else teams.get(int(b_id))
            if a_team is None or b_team is None or a_team == b_team:
                continue
            out.append({"ts": m[0], "a": m[1:7], "b": m[7:13], "a_team": a_team, "b_team": b_team})
        return out

    @staticmethod
    def _bat(runs, faced, not_out):
        return f"{runs}{'*' if not_out else ''} ({faced})" if faced > 0 else "DID NOT BAT"

    @staticmethod
    def _bowl(wickets, conceded, balls):
        return f"BOWL {wickets}/{conceded}" if balls > 0 else "DID NOT BOWL"

    @commands.command(name="testh2h")
    @commands.guild_only()
    async def test_h2h(self, ctx, first: discord.Member, second: discord.Member = None):
        """Test: image head-to-head. Usage: +testh2h @player [@player]"""
        if not await self._allowed(ctx, "testh2h"):
            return
        if second is None:
            first, second = ctx.author, first
        if first.id == second.id:
            await ctx.send("Pick two different players.")
            return
        loading = await ctx.send("⏳ Building the head-to-head...")
        try:
            guild = ctx.guild
            rows_a, rows_b = self._match_rows(guild.id, first.id), self._match_rows(guild.id, second.id)
            ta, tb = _totals(rows_a), _totals(rows_b)
            fa, fb = self._fielding(guild.id, first.id), self._fielding(guild.id, second.id)
            team_names = {}
            try:
                from .lb import _team_rosters
                for team in _team_rosters(guild.id).values():
                    for uid in team["members"]:
                        team_names[int(uid)] = str(team["name"])
            except Exception:
                pass
            avatar_a, avatar_b = await asyncio.gather(self._avatar(first), self._avatar(second))

            def side(member, blob, rows):
                color = member.color.to_rgb() if member.color.value else None
                chips = [team_names.get(member.id, "FREE AGENT").upper(), f"{len(rows)} MATCHES"]
                return {"name": member.name, "image": blob, "color": color, "chips": chips}
            left, right = side(first, avatar_a, rows_a), side(second, avatar_b, rows_b)

            def eff(rec):
                total = rec[0] + rec[1]
                return rec[0] / total * 100 if total else None

            def hi(rows):
                return max((r["runs"] for r in rows), default=0)

            def fiftys(rows):
                return sum(1 for r in rows if 50 <= r["runs"] < 100)

            def n(value, digits=1, suffix=""):
                return "—" if value is None else (f"{int(value):,}{suffix}" if digits == 0 else f"{value:.{digits}f}{suffix}")

            specs = [
                ("RUNS", ta["runs"], tb["runs"], "high", 0, "", "BATTING"),
                ("BATTING AVERAGE", ta["bat_avg"] if rows_a else None, tb["bat_avg"] if rows_b else None, "high", 1, "", "BATTING"),
                ("STRIKE RATE", ta["strike_rate"] if ta["balls_faced"] else None, tb["strike_rate"] if tb["balls_faced"] else None, "high", 1, "", "BATTING"),
                ("HIGHEST SCORE", hi(rows_a), hi(rows_b), "high", 0, "", "BATTING"),
                ("FIFTIES", fiftys(rows_a), fiftys(rows_b), "high", 0, "", "BATTING"),
                ("WICKETS", ta["wickets"], tb["wickets"], "high", 0, "", "BOWLING"),
                ("ECONOMY", ta["economy"] if ta["balls_bowled"] else None, tb["economy"] if tb["balls_bowled"] else None, "low", 2, "", "BOWLING"),
                ("BOWLING AVERAGE", ta["bowl_avg"] if ta["wickets"] else None, tb["bowl_avg"] if tb["wickets"] else None, "low", 1, "", "BOWLING"),
                ("CATCHES", fa[0], fb[0], "high", 0, "", "FIELDING"),
                ("FIELDING EFFICIENCY", eff(fa), eff(fb), "high", 0, "%", "FIELDING"),
            ]
            rows = [{"label": l, "a": a, "b": b, "fa": n(a, d, s), "fb": n(b, d, s), "better": better, "group": g}
                    for l, a, b, better, d, s, g in specs]
            edges = []
            for group in ("BATTING", "BOWLING", "FIELDING"):
                lw, rw = vi.tally([r for r in rows if r["group"] == group])
                winner = 1 if lw > rw else 2 if rw > lw else 0
                who = first.name if winner == 1 else second.name if winner == 2 else "LEVEL"
                edges.append((f"{group} EDGE", winner, f"{who.upper()}||{max(lw, rw)}–{min(lw, rw)}"))
            total_l, total_r = vi.tally(rows)
            winner = 1 if total_l > total_r else 2 if total_r > total_l else 0
            who = first.name if winner == 1 else second.name if winner == 2 else "LEVEL"
            edges.append(("OVERALL", winner, f"{who.upper()}||{max(total_l, total_r)}–{min(total_l, total_r)}"))

            opposed = self._opposed(guild.id, first.id, second.id, team_names)
            a_more = sum(1 for m in opposed if m["a"][0] > m["b"][0])
            b_more = sum(1 for m in opposed if m["b"][0] > m["a"][0])
            matches = []
            for m in opposed[:6]:
                ar, af, ac, ab, aw, ano = m["a"]
                br, bf, bc, bb, bw, bno = m["b"]
                matches.append({
                    "date": time.strftime("%d %b %Y", time.gmtime(m["ts"])).upper(),
                    "teams": f"{m['a_team']}  vs  {m['b_team']}",
                    "a_text": (self._bat(int(ar), int(af), int(ano)), self._bowl(int(aw), int(ac), int(ab))),
                    "b_text": (self._bat(int(br), int(bf), int(bno)), self._bowl(int(bw), int(bc), int(bb))),
                    "winner": 1 if ar > br else 2 if br > ar else 0,
                })
            summary = (f"{len(opposed)} match(es) on opposing teams  •  {first.name} scored more in {a_more}, "
                       f"{second.name} in {b_more}") if opposed else "No matches against each other yet"
            renderer = vi.Renderer({
                1: lambda: vi.render_overview("PLAYER HEAD TO HEAD", left, right, rows, edges, "GOOGLI MANAGER  •  HEAD TO HEAD"),
                2: lambda: vi.render_matches("PLAYER HEAD TO HEAD", left, right, matches, summary, "GOOGLI MANAGER  •  HEAD TO HEAD"),
            })
            png = await asyncio.to_thread(renderer.render, 1)
            view = VersusView(ctx, renderer, [("Stats", "📊"), ("Matches", "🆚")], "h2h.png")
            await loading.delete()
            view.message = await ctx.send(file=discord.File(io.BytesIO(png), filename="h2h.png"), view=view)
        except Exception as exc:
            print(f"[testversus] h2h failed: {type(exc).__name__}: {exc}", flush=True)
            await loading.edit(content="I couldn't build the head-to-head. Check the bot logs.")


async def setup(bot):
    await bot.add_cog(TestVersus(bot))
