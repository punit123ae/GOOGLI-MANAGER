"""Match predictions: admins add matches and enter results, everybody predicts them.

+addmatch Team A | Team B       (admins) add an upcoming match
+addmatch                       list the saved matches
+addmatch close <n> / open <n>  stop / allow predictions for a match
+addmatch remove <n>            delete a match, its predictions and its result
+predict [n]                    make (or change) your predictions for a match (the newest open one by default).
                                Winner plus, for runs, wickets and fantasy, the top performer of each team and
                                of the match. "AI pick" fills everything from the stats. Save posts your card.
+predict view [n] [@user]       show a saved prediction card (with points once the match is scored)
+setresult <n> <winner> | <top 5 runs> | <top 5 wickets> | <top 5 fantasy>      (admins) enter the real result
+predictlb [n]                  prediction leaderboard (one match, or overall)
"""
import asyncio
import io
import json
import re
import time

import discord
from discord.ext import commands

from .h2h import fetch_avatar, on_cooldown, render_png
from .predict_image import render_board_png, render_prediction_png, render_result_png
from .utility import is_bot_owner_id, owner_or_whitelisted

SIDE_A, SIDE_B = (70, 140, 255), (255, 120, 60)
CATS = [("runs", "Top run scorer", "RUNS"), ("wickets", "Top wicket taker", "WICKETS"),
        ("fantasy", "Top fantasy scorer", "FANTASY")]
TOTAL_PICKS = 1 + 3 * len(CATS)
VALID_KEYS = {"winner"} | {f"{k}:{s}" for k, _t, _l in CATS for s in "abm"}
POSITION_POINTS = {1: 5, 2: 3, 3: 2, 4: 1, 5: 1}     # points for a pick that finished n-th in the real top 5
WINNER_POINTS = 5
MAX_POINTS = WINNER_POINTS + 3 * len(CATS) * 5


def clean_picks(picks):
    """Only the keys the game uses (old saves may hold fielding picks, which no longer exist)."""
    return {k: v for k, v in (picks or {}).items() if k in VALID_KEYS and v is not None}


def picks_done(picks):
    return len(clean_picks(picks))


def grade_picks(picks, result, roster_a, roster_b):
    """(points, {pick key: points}) for one user's picks against the official result.

    A team pick scores full marks when it is that team's best player of the match, a match pick when it
    finished first; otherwise the points for the position the player really finished in the top 5
    (a team pick is capped at 3 then).
    """
    picks = clean_picks(picks)
    details, total = {}, 0
    if result.get("winner") is not None and picks.get("winner") is not None:
        got = WINNER_POINTS if int(picks["winner"]) == int(result["winner"]) else 0
        details["winner"] = got
        total += got
    for cat, _t, _l in CATS:
        top = [int(u) for u in result.get("tops", {}).get(cat, [])]
        if not top:
            continue
        best = {"a": next((u for u in top if u in roster_a), None), "b": next((u for u in top if u in roster_b), None),
                "m": top[0]}
        for side in "abm":
            picked = picks.get(f"{cat}:{side}")
            if picked is None:
                continue
            picked = int(picked)
            if best[side] is not None and picked == best[side]:
                got = 5
            elif picked in top:
                got = POSITION_POINTS.get(top.index(picked) + 1, 0)
                got = min(got, 3) if side != "m" else got
            else:
                got = 0
            details[f"{cat}:{side}"] = got
            total += got
    return total, details


class PredictView(discord.ui.View):
    """The form: five tabs (winner, runs, wickets, fielding, fantasy) with drop-downs, then Save."""

    def __init__(self, cog, ctx, match, team_a, team_b, squads, picks):
        super().__init__(timeout=600)
        self.cog, self.ctx, self.match = cog, ctx, match
        self.teams = (team_a, team_b)
        self.squads = squads                     # [[(uid, name), ...], [...]]
        self.picks = picks
        self.tab = "winner"
        self.page = 0
        self.page_count = 1
        self.message = None
        self._build()

    # ------------------------------------------------------------------ building
    def _tab_done(self, tab):
        if tab == "winner":
            return self.picks.get("winner") is not None
        return all(self.picks.get(f"{tab}:{s}") is not None for s in "abm")

    def _name(self, uid):
        return self.cog.compare._display_name(self.ctx.guild, uid)

    def _status(self):
        a, b = (str(t["name"]) for t in self.teams)
        done = picks_done(self.picks)
        lines = [f"**Match #{self.match['number']}: {a} vs {b}**",
                 f"Picks made: **{done}/{TOTAL_PICKS}**  (leave any empty, change them until the match closes)"]
        if self.tab == "winner":
            current = self.picks.get("winner")
            lines.append("Who will win the match?" + (f"  Your pick: **{(a, b)[int(current)]}**" if current is not None else ""))
        else:
            title = next(t for k, t, _l in CATS if k == self.tab)
            lines.append(f"{title}: one player from each team and the best of the whole match. "
                         "The list is ordered by what the AI expects from each player.")
            mine = []
            for side, label in (("a", a), ("b", b), ("m", "Match")):
                uid = self.picks.get(f"{self.tab}:{side}")
                if uid is not None:
                    mine.append(f"{label}: **{self._name(uid)}**")
            if mine:
                lines.append("Your picks  " + "  •  ".join(mine))
        return "\n".join(lines)

    def _build(self):
        self.clear_items()
        tabs = [("winner", "WINNER")] + [(k, label) for k, _t, label in CATS]
        for key, label in tabs:
            done = self._tab_done(key)
            button = discord.ui.Button(
                label=label, emoji="✅" if done else None, row=0,
                style=discord.ButtonStyle.primary if key == self.tab else discord.ButtonStyle.secondary)
            button.callback = self._open_tab(key)
            self.add_item(button)
        a, b = (str(t["name"]) for t in self.teams)
        if self.tab == "winner":
            self._select("winner", f"Winner of the match", [(a, "0"), (b, "1")], 1)
        else:
            title = next(t for k, t, _l in CATS if k == self.tab)
            ranked = []
            option_lists = []
            for team in self.teams:
                ids, scores = self.cog._ranked_roster(self.ctx.guild.id, team, self.tab)
                entries = [(uid, self.cog.compare._display_name(self.ctx.guild, uid), scores.get(uid, 0)) for uid in ids]
                ranked.append(entries)
                option_lists.append([
                    (name, str(uid), (f"AI PICK • avg {score:g}" if i == 0 and score else f"avg {score:g} per match"))
                    for i, (uid, name, score) in enumerate(entries)
                ])
            match_entries = sorted(
                [(uid, name, score, team["name"]) for team, entries in zip(self.teams, ranked)
                 for uid, name, score in entries],
                key=lambda item: (-item[2], item[0])
            )
            match_options = [
                (f"{name} ({team_name})", str(uid),
                 (f"AI PICK • avg {score:g}" if i == 0 and score else f"{team_name} • avg {score:g}"))
                for i, (uid, name, score, team_name) in enumerate(match_entries)
            ]
            self.page_count = max(1, (len(option_lists[0]) + 24) // 25,
                                  (len(option_lists[1]) + 24) // 25,
                                  (len(match_options) + 24) // 25)
            self.page = min(self.page, self.page_count - 1)
            self._select(f"{self.tab}:a", f"{a}: {title.lower()} • ranked", option_lists[0], 1)
            self._select(f"{self.tab}:b", f"{b}: {title.lower()} • ranked", option_lists[1], 2)
            self._select(f"{self.tab}:m", f"Best of the match: {title.lower()} • ranked", match_options, 3)
        if self.tab != "winner" and self.page_count > 1:
            previous = discord.ui.Button(label="Prev players", style=discord.ButtonStyle.secondary,
                                          row=4, disabled=self.page <= 0)
            previous.callback = self._change_page(-1)
            following = discord.ui.Button(label=f"More players ({self.page + 1}/{self.page_count})",
                                          style=discord.ButtonStyle.secondary, row=4,
                                          disabled=self.page >= self.page_count - 1)
            following.callback = self._change_page(1)
            self.add_item(previous)
            self.add_item(following)
        ai = discord.ui.Button(label="AI pick", emoji="🤖", style=discord.ButtonStyle.primary, row=4)
        ai.callback = self._ai_pick
        self.add_item(ai)
        save = discord.ui.Button(label="Save & post", emoji="📨", style=discord.ButtonStyle.success, row=4)
        save.callback = self._save
        cancel = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.danger, row=4)
        cancel.callback = self._cancel
        self.add_item(save)
        self.add_item(cancel)

    def _select(self, key, placeholder, options, row):
        current = self.picks.get(key)
        current = None if current is None else str(current)
        if key == "winner":
            visible_options = options
        else:
            start = self.page * 25
            visible_options = options[start:start + 25]
        items = []
        for opt in visible_options:
            label, value = opt[0], opt[1]
            items.append(discord.SelectOption(label=str(label)[:100], value=value,
                                              description=(opt[2][:100] if len(opt) > 2 else None),
                                              default=(value == current)))
        if not items:
            items = [discord.SelectOption(label="No players on this team", value="none")]
        select = discord.ui.Select(placeholder=placeholder[:150], options=items[:25], min_values=1, max_values=1, row=row)
        select.callback = self._chosen(key, select)
        self.add_item(select)

    def _change_page(self, delta):
        async def callback(interaction):
            self.page = max(0, min(self.page_count - 1, self.page + delta))
            self._build()
            await interaction.response.edit_message(content=self._status(), view=self)
        return callback

    # ------------------------------------------------------------------ callbacks
    async def interaction_check(self, interaction):
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message("This form belongs to someone else. Use `+predict` for your own.",
                                                    ephemeral=True)
            return False
        return True

    def _open_tab(self, key):
        async def callback(interaction):
            self.tab = key
            self._build()
            await interaction.response.edit_message(content=self._status(), view=self)
        return callback

    def _chosen(self, key, select):
        async def callback(interaction):
            value = select.values[0]
            if value != "none":
                self.picks[key] = int(value)
            self._build()
            await interaction.response.edit_message(content=self._status(), view=self)
        return callback

    async def _ai_pick(self, interaction):
        """Fill every pick with the AI's choice: the best expected player of each team and of the match."""
        await interaction.response.defer()
        ai = await self.cog.ai_choices(self.ctx, self.match, *self.teams)
        self.picks.update({k: v for k, v in ai.items() if v is not None})
        self._build()
        await interaction.edit_original_response(
            content=self._status() + "\n🤖 AI picks filled in. Change anything you like, then Save.", view=self)

    async def _cancel(self, interaction):
        self.stop()
        await interaction.response.edit_message(content="Prediction cancelled.", view=None)

    async def _save(self, interaction):
        self.picks = clean_picks(self.picks)
        if picks_done(self.picks) == 0:
            await interaction.response.send_message("Make at least one pick first.", ephemeral=True)
            return
        wait = on_cooldown(self.ctx.author.id)
        if wait:
            await interaction.response.send_message(f"Please wait {wait:.0f}s before drawing another image.", ephemeral=True)
            return
        if not self.cog.predictions_enabled():
            self.stop()
            await interaction.response.edit_message(content="Predictions are temporarily disabled by the bot owner.", view=None)
            return
        if self.cog.match_closed(self.ctx.guild.id, self.match["number"]):
            self.stop()
            await interaction.response.edit_message(content="Predictions for this match are closed.", view=None)
            return
        await interaction.response.defer()
        try:
            channel = self.cog.output_channel(self.ctx.guild, interaction.channel)
            if channel is None:
                await interaction.followup.send("The configured prediction channel is missing or I no longer have Send Messages and Attach Files there. Ask an admin to run +setpredictchannel again.", ephemeral=True)
                return
            self.cog.save_picks(self.ctx.guild.id, self.match["number"], self.ctx.author.id, self.picks)
            png = await self.cog.card(self.ctx.guild, self.ctx.author, self.match, *self.teams, self.picks)
            await channel.send(
                content=f"{self.ctx.author.mention} locked in their predictions for match #{self.match['number']}!",
                file=discord.File(io.BytesIO(png), filename="prediction.png"),
                allowed_mentions=discord.AllowedMentions(users=[self.ctx.author]))
            await interaction.edit_original_response(content="✅ Saved and posted. Run `+predict` again to change it.",
                                                     view=None)
        except Exception as exc:
            import traceback
            traceback.print_exc()
            print(f"[predict] save failed: {type(exc).__name__}: {exc}", flush=True)
            await interaction.followup.send("I couldn't post your prediction. Check the bot logs.", ephemeral=True)
            return
        self.stop()

    async def on_timeout(self):
        if self.message is not None:
            try:
                await self.message.edit(content="This prediction form timed out. Run `+predict` again.", view=None)
            except discord.HTTPException:
                pass


class Predictions(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._lock = asyncio.Lock()           # one card at a time
        self._rank_cache = {}                 # (guild, team, category) -> (time, ids, scores)
        db = bot.db
        db.execute(
            "CREATE TABLE IF NOT EXISTS predict_matches (id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, "
            "number INTEGER NOT NULL, team_a_id INTEGER NOT NULL, team_b_id INTEGER NOT NULL, added_by INTEGER, "
            "added_at TEXT DEFAULT CURRENT_TIMESTAMP, closed INTEGER NOT NULL DEFAULT 0, UNIQUE(guild_id, number))")
        try:
            db.execute("ALTER TABLE predict_matches ADD COLUMN closed INTEGER NOT NULL DEFAULT 0")
        except Exception:
            pass
        db.execute(
            "CREATE TABLE IF NOT EXISTS predict_picks (guild_id INTEGER NOT NULL, number INTEGER NOT NULL, "
            "user_id INTEGER NOT NULL, picks TEXT NOT NULL, updated_at TEXT DEFAULT CURRENT_TIMESTAMP, "
            "PRIMARY KEY (guild_id, number, user_id))")
        try:
            db.execute("ALTER TABLE predict_picks ADD COLUMN rank_snapshot TEXT NOT NULL DEFAULT '{}'")
        except Exception:
            pass
        db.execute("CREATE TABLE IF NOT EXISTS predict_settings (guild_id INTEGER PRIMARY KEY, channel_id INTEGER)")
        db.execute("CREATE TABLE IF NOT EXISTS predict_global_state (id INTEGER PRIMARY KEY CHECK(id=1), enabled INTEGER NOT NULL DEFAULT 1)")
        db.execute("INSERT OR IGNORE INTO predict_global_state (id, enabled) VALUES (1, 1)")
        db.execute("CREATE TABLE IF NOT EXISTS predict_results (guild_id INTEGER NOT NULL, number INTEGER NOT NULL, result TEXT NOT NULL, updated_at TEXT DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY (guild_id, number))")
        db.execute("CREATE TABLE IF NOT EXISTS predict_scores (guild_id INTEGER NOT NULL, number INTEGER NOT NULL, user_id INTEGER NOT NULL, points INTEGER NOT NULL, details TEXT NOT NULL, PRIMARY KEY (guild_id, number, user_id))")
        db.commit()

    @property
    def compare(self):
        return self.bot.get_cog("CompareTeams")

    # ------------------------------------------------------------------ data
    def _team_by_id(self, team_id):
        return self.compare.core.db.execute("SELECT * FROM registered_teams WHERE id=?", (int(team_id),)).fetchone()

    def _roster(self, team):
        core = self.compare.core
        rows = core.db.execute("SELECT user_id FROM team_players WHERE team_id=? ORDER BY added_at",
                               (team["id"],)).fetchall()
        return list(dict.fromkeys([int(team["captain_id"])]
                                  + ([int(team["vice_captain_id"])] if team["vice_captain_id"] else [])
                                  + [int(r["user_id"]) for r in rows]))

    def _ranked_roster(self, guild_id, team, category):
        """(player ids best first, {id: expected value per match}) for runs, wickets or fantasy.

        The expectation is each player's average per match, pulled towards the squad's average by two
        "pseudo-matches" so a single big game (or a single tiny one) does not decide it. Players with
        no imported matches score 0 and are never the AI's pick.
        """
        key = (int(guild_id), int(team["id"]), category)
        hit = self._rank_cache.get(key)
        if hit and time.monotonic() - hit[0] < 120:
            return list(hit[1]), dict(hit[2])
        from .stats import fantasy_raw_points
        roster = self._roster(team)
        if not roster:
            return [], {}
        placeholders = ",".join("?" for _ in roster)
        rows = self.bot.db.execute(
            f"SELECT p.user_id,p.runs,p.balls_faced,p.runs_conceded,p.balls_bowled,p.wickets,p.not_out "
            f"FROM player_match_stats p JOIN stats_imports i ON i.source_message_id=p.source_message_id "
            f"WHERE i.guild_id=? AND p.user_id IN ({placeholders})",
            (int(guild_id), *roster)).fetchall()
        by_user = {uid: [] for uid in roster}
        for row in rows:
            by_user.setdefault(int(row["user_id"]), []).append(dict(row))
        average = {}
        for uid in roster:
            player_rows = by_user.get(uid, [])
            if not player_rows:
                continue
            if category == "runs":
                total = sum(int(r["runs"]) for r in player_rows)
            elif category == "wickets":
                total = sum(int(r["wickets"]) for r in player_rows)
            else:
                total = fantasy_raw_points(player_rows)[2]
            average[uid] = (total / len(player_rows), len(player_rows))
        prior = sum(v for v, _n in average.values()) / len(average) if average else 0.0
        scores = {}
        for uid in roster:
            if uid in average:
                value, n = average[uid]
                scores[uid] = round((value * n + 2 * prior) / (n + 2), 1)
            else:
                scores[uid] = 0
        ordered = sorted(roster, key=lambda uid: (-scores.get(uid, 0), uid))
        self._rank_cache[key] = (time.monotonic(), ordered, scores)
        return list(ordered), dict(scores)

    def _teams_from(self, guild_id, text):
        parts = [p.strip() for p in re.split(r"\s*\|\s*|\s+vs\.?\s+", text, flags=re.I) if p.strip()]
        if len(parts) != 2:
            return None, None, "Give two teams: `Team A | Team B`."
        found = []
        for query in parts:
            team, error = self.compare._resolve(guild_id, query)
            if error:
                return None, None, error
            found.append(team)
        if int(found[0]["id"]) == int(found[1]["id"]):
            return None, None, "Pick two different teams."
        return found[0], found[1], None

    def predictions_enabled(self):
        row = self.bot.db.execute("SELECT enabled FROM predict_global_state WHERE id=1").fetchone()
        return bool(row[0]) if row else True

    def output_channel(self, guild, fallback):
        row = self.bot.db.execute("SELECT channel_id FROM predict_settings WHERE guild_id=?", (guild.id,)).fetchone()
        if row and row[0]:
            channel = guild.get_channel(int(row[0]))
            if channel is None or not hasattr(channel, "send"):
                return None
            me = guild.me
            perms = channel.permissions_for(me) if me else None
            if not perms or not perms.send_messages or not perms.attach_files:
                return None
            return channel
        return fallback

    def match_closed(self, guild_id, number):
        row = self.bot.db.execute("SELECT closed FROM predict_matches WHERE guild_id=? AND number=?",
                                  (guild_id, number)).fetchone()
        return row is None or bool(row[0])

    def load_picks(self, guild_id, number, user_id):
        row = self.bot.db.execute("SELECT picks FROM predict_picks WHERE guild_id=? AND number=? AND user_id=?",
                                  (guild_id, number, user_id)).fetchone()
        try:
            return json.loads(row[0]) if row else {}
        except (TypeError, ValueError):
            return {}

    def save_picks(self, guild_id, number, user_id, picks):
        self.bot.db.execute(
            "INSERT INTO predict_picks (guild_id, number, user_id, picks) VALUES (?,?,?,?) "
            "ON CONFLICT(guild_id, number, user_id) DO UPDATE SET picks=excluded.picks, updated_at=CURRENT_TIMESTAMP",
            (guild_id, number, user_id, json.dumps(clean_picks(picks))))
        self.bot.db.commit()

    def load_result(self, guild_id, number):
        row = self.bot.db.execute("SELECT result FROM predict_results WHERE guild_id=? AND number=?",
                                  (guild_id, number)).fetchone()
        try:
            result = json.loads(row[0]) if row else None
        except (TypeError, ValueError):
            return None
        return result if isinstance(result, dict) and "tops" in result else None      # old-format results are ignored

    async def ai_winner(self, guild, team_a, team_b):
        """0 or 1: the side the team comparison rates higher (falls back to total fantasy form)."""
        try:
            import types
            from .compareteams import predict
            ctx = types.SimpleNamespace(guild=guild)
            side_a = await self.compare._side(ctx, team_a, avatars=False)
            side_b = await self.compare._side(ctx, team_b, avatars=False)
            chance = predict(side_a, side_b)
            return 0 if chance["a"] >= chance["b"] else 1
        except Exception:
            total = [sum(self._ranked_roster(guild.id, t, "fantasy")[1].values()) for t in (team_a, team_b)]
            return 0 if total[0] >= total[1] else 1

    async def ai_choices(self, ctx, match, team_a, team_b):
        """The AI's full set of picks: stronger side to win, best expected player for each slot."""
        picks = {"winner": await self.ai_winner(ctx.guild, team_a, team_b)}
        for cat, _t, _l in CATS:
            ra, sa = self._ranked_roster(ctx.guild.id, team_a, cat)
            rb, sb = self._ranked_roster(ctx.guild.id, team_b, cat)
            both = sorted(list(sa.items()) + list(sb.items()), key=lambda item: (-item[1], item[0]))
            picks[f"{cat}:a"] = ra[0] if ra and sa.get(ra[0], 0) > 0 else None
            picks[f"{cat}:b"] = rb[0] if rb and sb.get(rb[0], 0) > 0 else None
            picks[f"{cat}:m"] = both[0][0] if both and both[0][1] > 0 else None
        return picks

    def _match(self, guild_id, text):
        """(match row, error). No number = the newest match that is still open."""
        db = self.bot.db
        if text.isdigit():
            row = db.execute("SELECT * FROM predict_matches WHERE guild_id=? AND number=?", (guild_id, int(text))).fetchone()
        elif not text:
            row = db.execute("SELECT * FROM predict_matches WHERE guild_id=? AND closed=0 ORDER BY number DESC LIMIT 1",
                             (guild_id,)).fetchone()
            if row is None:
                return None, "There is no open match to predict right now."
        else:
            return None, "Use `+predict` or `+predict <match number>`."
        return (row, None) if row else (None, "I can't find that match.")

    # ------------------------------------------------------------------ cards
    async def _sides(self, guild, team_a, team_b):
        import types
        from .h2h import team_role_color
        compare, shim = self.compare, types.SimpleNamespace(guild=guild)
        out = []
        for team in (team_a, team_b):
            out.append({"name": str(team["name"]), "logo": await compare._logo_for(shim, team),
                        "color": team_role_color(compare.core, guild, team) or (SIDE_A if not out else SIDE_B)})
        return out

    async def _people(self, guild, uids, sides, team_a, team_b):
        """{uid: {name, avatar, color}} with the avatar in the colour of the player's team."""
        side_of = {}
        for index, team in enumerate((team_a, team_b)):
            for uid in self._roster(team):
                side_of.setdefault(uid, index)
        sem = asyncio.Semaphore(6)
        people = {}

        async def one(uid):
            async with sem:
                member = guild.get_member(uid) or self.bot.get_user(uid)
                if member is None:
                    try:
                        member = await self.bot.fetch_user(uid)
                    except discord.HTTPException:
                        member = None
                people[uid] = {"name": self.compare._display_name(guild, uid),
                               "avatar": await fetch_avatar(self.bot, member),
                               "color": sides[side_of.get(uid, 0)]["color"]}

        await asyncio.gather(*(one(u) for u in set(uids)), return_exceptions=True)
        return people

    async def card(self, guild, user, match, team_a, team_b, picks):
        """One user's picks; graded with points and the real answers once the match has a result."""
        compare = self.compare
        picks = clean_picks(picks)
        sides = await self._sides(guild, team_a, team_b)
        winner = picks.get("winner")
        cats, wanted, ai_picks = {}, set(), {}
        for key, _t, _l in CATS:
            cats[key] = {s: picks.get(f"{key}:{s}") for s in "abm"}
            wanted |= {v for v in cats[key].values() if v is not None}
            ra, sa = self._ranked_roster(guild.id, team_a, key)
            rb, sb = self._ranked_roster(guild.id, team_b, key)
            both = sorted(list(sa.items()) + list(sb.items()), key=lambda item: (-item[1], item[0]))
            ai_picks[key] = {"a": ra[0] if ra and sa.get(ra[0], 0) > 0 else None,
                             "b": rb[0] if rb and sb.get(rb[0], 0) > 0 else None,
                             "m": both[0][0] if both and both[0][1] > 0 else None}
        data = {"guild": guild.name, "number": int(match["number"]), "a": sides[0], "b": sides[1],
                "winner": None if winner is None else int(winner), "cats": cats, "ai_picks": ai_picks}
        result = self.load_result(guild.id, int(match["number"]))
        if result is not None:
            roster_a, roster_b = set(self._roster(team_a)), set(self._roster(team_b))
            total, details = grade_picks(picks, result, roster_a, roster_b)
            actual = {}
            for key, _t, _l in CATS:
                top = [int(u) for u in result["tops"].get(key, [])]
                actual[key] = {"a": next((u for u in top if u in roster_a), None),
                               "b": next((u for u in top if u in roster_b), None), "m": top[0] if top else None}
                wanted |= {v for v in actual[key].values() if v is not None}
            details.update(total=total, max=MAX_POINTS)
            data.update(result_winner=result.get("winner"), grade=details, actual=actual)
        data["people"] = await self._people(guild, wanted, sides, team_a, team_b)
        data["user"] = {"name": compare._display_name(guild, user.id), "avatar": await fetch_avatar(self.bot, user)}
        async with self._lock:
            return await render_png(render_prediction_png, data)

    async def result_card(self, guild, match, team_a, team_b, result, board):
        """Official result plus the top predictors: board = [(uid, points)] best first."""
        sides = await self._sides(guild, team_a, team_b)
        uids = {int(u) for top in result["tops"].values() for u in top} | {uid for uid, _p in board[:5]}
        people = await self._people(guild, uids, sides, team_a, team_b)
        data = {"guild": guild.name, "number": int(match["number"]), "a": sides[0], "b": sides[1],
                "winner": result.get("winner"), "tops": {k: [int(u) for u in v] for k, v in result["tops"].items()},
                "people": people, "players": len(board), "max": MAX_POINTS,
                "board": [{"name": people.get(uid, {}).get("name", "?"), "avatar": people.get(uid, {}).get("avatar"),
                           "points": pts} for uid, pts in board[:5]]}
        async with self._lock:
            return await render_png(render_result_png, data)

    # ------------------------------------------------------------------ +addmatch
    @commands.command(name="addmatch", aliases=["addpredictmatch"])
    @commands.guild_only()
    async def addmatch(self, ctx, *, text: str = ""):
        """+addmatch Team A | Team B adds a match to predict. No text lists them; remove/close/open <n> manage one."""
        if self.compare is None:
            return await ctx.reply("The team system isn't loaded right now.", mention_author=False)
        db, none = self.bot.db, discord.AllowedMentions.none()
        text = text.strip()
        if not text:
            rows = db.execute("SELECT * FROM predict_matches WHERE guild_id=? ORDER BY number DESC LIMIT 15",
                              (ctx.guild.id,)).fetchall()
            if not rows:
                return await ctx.reply("No matches added yet. Add one with `+addmatch Team A | Team B`.",
                                       mention_author=False)
            lines = []
            for r in rows:
                a, b = self._team_by_id(r["team_a_id"]), self._team_by_id(r["team_b_id"])
                count = db.execute("SELECT COUNT(*) FROM predict_picks WHERE guild_id=? AND number=?",
                                   (ctx.guild.id, r["number"])).fetchone()[0]
                scored = db.execute("SELECT 1 FROM predict_results WHERE guild_id=? AND number=?",
                                    (ctx.guild.id, r["number"])).fetchone()
                state = "scored" if scored else "closed" if r["closed"] else "open"
                lines.append(f"**#{r['number']}**  {a['name'] if a else '?'} vs {b['name'] if b else '?'}  "
                             f"({state}, {count} prediction{'s' if count != 1 else ''})")
            return await ctx.reply("**Matches** (newest first; `+predict <number>` to predict one)\n" + "\n".join(lines),
                                   mention_author=False, allowed_mentions=none)
        if not await owner_or_whitelisted(ctx, command_name="addmatch", app_owner=True):
            return await ctx.reply("Only the bot owner or a role whitelisted for this command can manage matches.",
                                   mention_author=False)
        m = re.match(r"(?i)^(remove|delete|del|close|open)\s+#?(\d+)$", text)
        if m:
            action, number = m.group(1).lower(), int(m.group(2))
            if action in ("close", "open"):
                if action == "open" and db.execute(
                    "SELECT 1 FROM predict_results WHERE guild_id=? AND number=?", (ctx.guild.id, number)
                ).fetchone():
                    return await ctx.reply("This match already has an official result and points. Run +setresult again to correct the scoring; it cannot be reopened.", mention_author=False)
                cur = db.execute("UPDATE predict_matches SET closed=? WHERE guild_id=? AND number=?",
                                 (1 if action == "close" else 0, ctx.guild.id, number))
                db.commit()
                return await ctx.reply((f"✅ Match #{number} is now {action}d." if action == "close" else
                                        f"✅ Match #{number} is open again.") if cur.rowcount else
                                       f"There is no match #{number}.", mention_author=False)
            cur = db.execute("DELETE FROM predict_matches WHERE guild_id=? AND number=?", (ctx.guild.id, number))
            db.execute("DELETE FROM predict_picks WHERE guild_id=? AND number=?", (ctx.guild.id, number))
            db.execute("DELETE FROM predict_results WHERE guild_id=? AND number=?", (ctx.guild.id, number))
            db.execute("DELETE FROM predict_scores WHERE guild_id=? AND number=?", (ctx.guild.id, number))
            db.commit()
            return await ctx.reply(f"✅ Match #{number} and its predictions were removed." if cur.rowcount else
                                   f"There is no match #{number}.", mention_author=False)
        team_a, team_b, error = self._teams_from(ctx.guild.id, text)
        if error:
            return await ctx.reply(error, mention_author=False, allowed_mentions=none)
        number = db.execute("SELECT COALESCE(MAX(number), 0) FROM predict_matches WHERE guild_id=?",
                            (ctx.guild.id,)).fetchone()[0] + 1
        db.execute("INSERT INTO predict_matches (guild_id, number, team_a_id, team_b_id, added_by) VALUES (?,?,?,?,?)",
                   (ctx.guild.id, number, int(team_a["id"]), int(team_b["id"]), ctx.author.id))
        db.commit()
        await ctx.reply(f"✅ Match **#{number}** added: **{team_a['name']}** vs **{team_b['name']}**. "
                        "Everyone can now predict it with `+predict`.", mention_author=False, allowed_mentions=none)



    # ------------------------------------------------------------------ results
    def _player_ids(self, guild, segment, roster_ids):
        """Player ids from mentions / ids / comma-separated names, in the order written. (ids, problems)."""
        found = [int(a or b) for a, b in re.findall(r"<@!?(\d{15,25})>|\b(\d{15,25})\b", segment)]
        problems = []
        if not found:
            for chunk in [c.strip() for c in segment.split(",") if c.strip()]:
                needle = chunk.casefold().lstrip("@")
                hits = [uid for uid in roster_ids
                        if needle in (self.compare._display_name(guild, uid).casefold(),
                                      (getattr(guild.get_member(uid), "display_name", "") or "").casefold())]
                if len(hits) == 1:
                    found.append(hits[0])
                else:
                    problems.append(chunk)
        return list(dict.fromkeys(found)), problems

    @commands.command(name="setresult", aliases=["predictresult", "setpredictresult", "predresult", "matchresult"])
    @commands.guild_only()
    async def setresult(self, ctx, *, text: str = ""):
        """+setresult <match> <winner> | <top 5 runs> | <top 5 wickets> | <top 5 fantasy> scores the match."""
        if not (await owner_or_whitelisted(ctx, command_name="setresult", app_owner=True)
                or await owner_or_whitelisted(ctx, command_name="predictresult", app_owner=True)):
            return await ctx.reply("Only the bot owner or a role whitelisted for this command can enter results.",
                                   mention_author=False)
        usage = ("**Usage:** `+setresult <match> <winner> | <top 5 run scorers> | <top 5 wicket takers> | "
                 "<top 5 fantasy>`\nWinner is `A`, `B`, a team name or `tie`. Players are mentions (best first, "
                 "up to 5); a list can be left empty. Example:\n"
                 "`+setresult 3 A | @p1 @p2 @p3 @p4 @p5 | @q1 @q2 @q3 | @r1 @r2 @r3 @r4 @r5`\n"
                 "You can also label lists (`runs: ...`, `wickets: ...`, `fantasy: ...`) and give them in any order.")
        m = re.match(r"^#?(\d+)\s*(.*)$", text.strip(), re.S)
        if not m:
            return await ctx.reply(usage, mention_author=False)
        number = int(m.group(1))
        row, error = self._match(ctx.guild.id, str(number))
        if error:
            return await ctx.reply(error, mention_author=False)
        team_a, team_b = self._team_by_id(row["team_a_id"]), self._team_by_id(row["team_b_id"])
        if not team_a or not team_b:
            return await ctx.reply("One of the teams in that match no longer exists.", mention_author=False)
        segments = [x.strip() for x in m.group(2).split("|")]
        word = segments[0].strip().casefold()
        names = [str(team_a["name"]).casefold(), str(team_b["name"]).casefold()]
        if word in ("a", "1", "team a"):
            winner = 0
        elif word in ("b", "2", "team b"):
            winner = 1
        elif word in ("tie", "draw", "tied", "nr", "none", "no result", "abandoned"):
            winner = None
        elif word:
            hits = [i for i, n in enumerate(names) if word == n or (len(word) >= 3 and word in n)]
            if len(hits) != 1:
                return await ctx.reply(f"I couldn't tell which team won from `{segments[0]}`.\n" + usage,
                                       mention_author=False)
            winner = hits[0]
        else:
            return await ctx.reply(usage, mention_author=False)
        roster_a, roster_b = self._roster(team_a), self._roster(team_b)
        everyone = list(dict.fromkeys(roster_a + roster_b))
        tops = {}
        labels = {"run": "runs", "runs": "runs", "wicket": "wickets", "wickets": "wickets", "wkt": "wickets",
                  "wkts": "wickets", "fantasy": "fantasy", "fp": "fantasy", "points": "fantasy"}
        for index, segment in enumerate(segments[1:4]):
            cat = CATS[index][0]
            label = re.match(r"(?i)^([a-z]+)\s*[:=]\s*(.*)$", segment, re.S)
            if label and label.group(1).lower() in labels:
                cat, segment = labels[label.group(1).lower()], label.group(2)
            ids, problems = self._player_ids(ctx.guild, segment, everyone)
            if problems:
                return await ctx.reply("I couldn't find these players for " + cat + ": " + ", ".join(problems)
                                       + ". Use mentions.", mention_author=False, allowed_mentions=discord.AllowedMentions.none())
            outsiders = [u for u in ids if u not in everyone]
            if outsiders:
                return await ctx.reply("Not in either squad: " + " ".join(f"<@{u}>" for u in outsiders)
                                       + f"\nOnly players of **{team_a['name']}** and **{team_b['name']}** can be in the lists.",
                                       mention_author=False, allowed_mentions=discord.AllowedMentions.none())
            tops[cat] = ids[:5]
        if not any(tops.values()) and winner is None:
            return await ctx.reply("Nothing to save: give a winner and/or at least one list.\n" + usage,
                                   mention_author=False)
        result = {"winner": winner, "tops": {c: tops.get(c, []) for c, _t, _l in CATS}}
        db = self.bot.db
        db.execute("INSERT INTO predict_results (guild_id, number, result) VALUES (?,?,?) "
                   "ON CONFLICT(guild_id, number) DO UPDATE SET result=excluded.result, updated_at=CURRENT_TIMESTAMP",
                   (ctx.guild.id, number, json.dumps(result)))
        db.execute("UPDATE predict_matches SET closed=1 WHERE guild_id=? AND number=?", (ctx.guild.id, number))
        db.execute("DELETE FROM predict_scores WHERE guild_id=? AND number=?", (ctx.guild.id, number))
        board = []
        ra, rb = set(roster_a), set(roster_b)
        for saved in db.execute("SELECT user_id,picks FROM predict_picks WHERE guild_id=? AND number=?",
                                (ctx.guild.id, number)).fetchall():
            try:
                picks = json.loads(saved["picks"] or "{}")
            except (TypeError, ValueError):
                picks = {}
            points, details = grade_picks(picks, result, ra, rb)
            db.execute("INSERT INTO predict_scores (guild_id,number,user_id,points,details) VALUES (?,?,?,?,?)",
                       (ctx.guild.id, number, int(saved["user_id"]), points, json.dumps(details)))
            board.append((int(saved["user_id"]), points))
        db.commit()
        board.sort(key=lambda item: (-item[1], item[0]))
        wait = on_cooldown(ctx.author.id)
        summary = (f"✅ Result saved for match **#{number}**. Predictions are closed and **{len(board)}** "
                   f"{'player was' if len(board) == 1 else 'players were'} scored.")
        if wait:
            return await ctx.reply(summary + " (The result card will not be drawn yet: wait a few seconds and run it again.)",
                                   mention_author=False)
        channel = self.output_channel(ctx.guild, ctx.channel)
        async with ctx.typing():
            try:
                png = await self.result_card(ctx.guild, row, team_a, team_b, result, board)
            except Exception as exc:
                import traceback
                traceback.print_exc()
                print(f"[predict] result card failed: {type(exc).__name__}: {exc}", flush=True)
                return await ctx.reply(summary + " I couldn't draw the result card, check the bot logs.",
                                       mention_author=False)
        if channel is None:
            return await ctx.reply(summary + " The prediction channel is missing or I can't post there, so no card was sent.",
                                   mention_author=False)
        await channel.send(content=f"🏁 **Match #{number} result is in!** Check your points with `+predict view {number}`.",
                           file=discord.File(io.BytesIO(png), filename="result.png"),
                           allowed_mentions=discord.AllowedMentions.none())
        if channel.id != ctx.channel.id:
            await ctx.reply(summary + f" Card posted in {channel.mention}.", mention_author=False)

    @commands.command(name="predictlb", aliases=["predictleaderboard", "predictpoints"])
    @commands.guild_only()
    async def predictlb(self, ctx, number: int = None):
        """Prediction leaderboard: +predictlb for overall, +predictlb <match> for one match."""
        db = self.bot.db
        if number is None:
            rows = db.execute("SELECT user_id,SUM(points) AS points,COUNT(*) AS matches FROM predict_scores "
                              "WHERE guild_id=? GROUP BY user_id ORDER BY points DESC,user_id LIMIT 10",
                              (ctx.guild.id,)).fetchall()
            scored = db.execute("SELECT COUNT(DISTINCT number) FROM predict_scores WHERE guild_id=?",
                                (ctx.guild.id,)).fetchone()[0]
            subtitle = f"Overall  •  {scored} match{'es' if scored != 1 else ''} scored"
        else:
            rows = db.execute("SELECT user_id,points,1 AS matches FROM predict_scores WHERE guild_id=? AND number=? "
                              "ORDER BY points DESC,user_id LIMIT 10", (ctx.guild.id, number)).fetchall()
            subtitle = f"Match #{number}"
        if not rows:
            return await ctx.reply("No prediction points have been awarded yet.", mention_author=False)
        wait = on_cooldown(ctx.author.id)
        if wait:
            return await ctx.reply(f"Please wait {wait:.0f}s before drawing another image.", mention_author=False)
        async with ctx.typing():
            try:
                shown = []
                for r in rows:
                    uid = int(r["user_id"])
                    member = ctx.guild.get_member(uid) or self.bot.get_user(uid)
                    shown.append({"name": self.compare._display_name(ctx.guild, uid) if self.compare else str(uid),
                                  "avatar": await fetch_avatar(self.bot, member), "points": int(r["points"]),
                                  "matches": int(r["matches"])})
                async with self._lock:
                    png = await render_png(render_board_png, {"guild": ctx.guild.name, "subtitle": subtitle, "rows": shown})
            except Exception as exc:
                print(f"[predict] leaderboard failed: {type(exc).__name__}: {exc}", flush=True)
                return await ctx.reply("I couldn't draw the leaderboard. Check the bot logs.", mention_author=False)
        await ctx.send(file=discord.File(io.BytesIO(png), filename="predictions.png"))

    # ---------------------------------------------------------- prediction setup
    @commands.command(name="setpredictchannel", aliases=["predictsetchannel", "setpredchannel"])
    @commands.guild_only()
    async def setpredictchannel(self, ctx, channel: discord.TextChannel = None):
        """Set the channel where prediction cards are posted, or show the current setting."""
        row = self.bot.db.execute("SELECT channel_id FROM predict_settings WHERE guild_id=?", (ctx.guild.id,)).fetchone()
        if channel is None:
            current = ctx.guild.get_channel(int(row[0])) if row and row[0] else None
            return await ctx.reply(f"Prediction card channel: {current.mention if current else 'Not set (cards use the command channel).'}", mention_author=False)
        if not await owner_or_whitelisted(ctx, command_name="setpredictchannel", app_owner=True):
            return await ctx.reply("Only the bot owner or a whitelisted role can change the prediction channel.", mention_author=False)
        me = ctx.guild.me
        perms = channel.permissions_for(me) if me else None
        if not perms or not perms.send_messages or not perms.attach_files:
            return await ctx.reply("I need Send Messages and Attach Files permissions in that channel before I can set it.", mention_author=False)
        self.bot.db.execute("INSERT INTO predict_settings (guild_id, channel_id) VALUES (?, ?) ON CONFLICT(guild_id) DO UPDATE SET channel_id=excluded.channel_id", (ctx.guild.id, channel.id))
        self.bot.db.commit()
        await ctx.reply(f"Prediction cards will now be posted in {channel.mention}.", mention_author=False)

    @commands.command(name="predicttoggle", aliases=["togglepredict", "predictlock"])
    @commands.check(lambda ctx: is_bot_owner_id(ctx.bot, ctx.author.id))
    @commands.guild_only()
    async def predicttoggle(self, ctx, state: str = ""):
        """Bot owner only: pause or resume predictions globally."""
        current = self.predictions_enabled()
        wanted = (not current) if not state else state.lower() in ("on", "enable", "enabled", "open")
        if state and state.lower() not in ("on", "enable", "enabled", "open", "off", "disable", "disabled", "close"):
            return await ctx.reply("Use +predicttoggle on or +predicttoggle off.", mention_author=False)
        self.bot.db.execute("UPDATE predict_global_state SET enabled=? WHERE id=1", (int(wanted),))
        self.bot.db.commit()
        await ctx.reply("Predictions are now OPEN globally." if wanted else "Predictions are now PAUSED globally. Existing predictions can still be viewed.", mention_author=False)
    # ------------------------------------------------------------------ +predict
    @commands.command(name="predict", aliases=["prediction", "predictmatch", "pred", "mypredict", "mypred"])
    @commands.guild_only()
    async def predict(self, ctx, *, text: str = ""):
        """Predict a match: +predict, +predict <number>. Show one: +predict view [number] [@user]."""
        if self.compare is None:
            return await ctx.reply("The team system isn't loaded right now.", mention_author=False)
        none = discord.AllowedMentions.none()
        text = text.strip().lstrip("#")
        view_mode = False
        m = re.match(r"(?i)^(view|show)\b\s*(.*)$", text)
        target = ctx.author
        if m:
            view_mode = True
            if ctx.message.mentions:
                target = ctx.message.mentions[0]
            text = re.sub(r"<@!?\d+>", "", m.group(2)).strip().lstrip("#")
        if view_mode and not text:
            row = self.bot.db.execute(
                "SELECT * FROM predict_matches WHERE guild_id=? ORDER BY number DESC LIMIT 1",
                (ctx.guild.id,),
            ).fetchone()
            error = None if row else "There are no prediction matches yet."
        else:
            row, error = self._match(ctx.guild.id, text)
        if error:
            return await ctx.reply(error, mention_author=False)
        team_a, team_b = self._team_by_id(row["team_a_id"]), self._team_by_id(row["team_b_id"])
        if team_a is None or team_b is None:
            return await ctx.reply("One of the teams in that match no longer exists.", mention_author=False)
        number = int(row["number"])
        if view_mode:
            picks = self.load_picks(ctx.guild.id, number, target.id)
            if not picks:
                who = "You haven't" if target.id == ctx.author.id else f"{target.display_name} hasn't"
                return await ctx.reply(f"{who} predicted match #{number} yet.", mention_author=False,
                                       allowed_mentions=none)
            wait = on_cooldown(ctx.author.id)
            if wait:
                return await ctx.reply(f"Please wait {wait:.0f}s before drawing another image.", mention_author=False)
            async with ctx.typing():
                try:
                    png = await self.card(ctx.guild, target, row, team_a, team_b, picks)
                except Exception as exc:
                    print(f"[predict] view failed: {type(exc).__name__}: {exc}", flush=True)
                    return await ctx.reply("I couldn't draw that prediction. Check the bot logs.", mention_author=False)
            channel = self.output_channel(ctx.guild, ctx.channel)
            if channel is None:
                return await ctx.reply("The configured prediction channel is missing or I no longer have Send Messages and Attach Files there. Ask an admin to run +setpredictchannel again.", mention_author=False)
            await channel.send(
                content=f"Prediction by **{target.display_name}** • Match #{number}",
                file=discord.File(io.BytesIO(png), filename="prediction.png"),
                allowed_mentions=none,
            )
            if channel.id != ctx.channel.id:
                return await ctx.reply(f"Posted {target.display_name}'s prediction in {channel.mention}.", mention_author=False)
            return
        if row["closed"]:
            return await ctx.reply(f"Predictions for match #{number} are closed.", mention_author=False)
        if not self.predictions_enabled():
            return await ctx.reply("Predictions are temporarily disabled by the bot owner.", mention_author=False)
        squads = []
        for team in (team_a, team_b):
            ranked, _scores = self._ranked_roster(ctx.guild.id, team, "runs")
            squads.append([(uid, self.compare._display_name(ctx.guild, uid)) for uid in ranked])
        picks = self.load_picks(ctx.guild.id, number, ctx.author.id)
        view = PredictView(self, ctx, row, team_a, team_b, squads, picks)
        view.message = await ctx.reply(view._status(), view=view, mention_author=False)


async def setup(bot):
    await bot.add_cog(Predictions(bot))
