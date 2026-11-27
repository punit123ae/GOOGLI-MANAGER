"""Match predictions: admins add matches, everybody predicts them.

+addmatch Team A | Team B     (admins) add an upcoming match
+addmatch                     list the saved matches
+addmatch close <n> / open <n>  stop / allow predictions for a match
+addmatch remove <n>          delete a match and its predictions
+predict [n]                  make (or change) your predictions for match n (the newest open one by default).
                              Pick the winner and, for runs, wickets, catches and fantasy, the top performer of
                              each team and of the match, then press Save: your card is posted in the channel.
+predict view [n] [@user]     show a saved prediction card (yours by default)
"""
import asyncio
import io
import json
import re

import discord
from discord.ext import commands

from .h2h import fetch_avatar, on_cooldown, render_png
from .predict_image import render_prediction_png
from .utility import is_bot_owner_id, owner_or_whitelisted

SIDE_A, SIDE_B = (70, 140, 255), (255, 120, 60)
CATS = [("runs", "Top run scorer", "RUNS"), ("wickets", "Top wicket taker", "WICKETS"),
        ("catches", "Top fielder (catches)", "FIELDING"), ("fantasy", "Top fantasy scorer", "FANTASY")]
TOTAL_PICKS = 1 + 3 * len(CATS)


def picks_done(picks):
    return sum(1 for v in picks.values() if v is not None)


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

    def _status(self):
        a, b = (str(t["name"]) for t in self.teams)
        done = picks_done(self.picks)
        head = f"**Match #{self.match['number']}: {a} vs {b}**\nPicks made: **{done}/{TOTAL_PICKS}**  (you can leave some empty)"
        hint = {"winner": "Who will win the match?"}.get(self.tab)
        if hint is None:
            title = next(t for k, t, _l in CATS if k == self.tab)
            hint = f"{title}: pick one player from each team and the best of the whole match."
        return f"{head}\n{hint}"

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
                    (name, str(uid), (f"AI PICK • {score:g}" if i == 0 and score else f"Score: {score:g}"))
                    for i, (uid, name, score) in enumerate(entries)
                ])
            match_entries = sorted(
                [(uid, name, score, team["name"]) for team, entries in zip(self.teams, ranked)
                 for uid, name, score in entries],
                key=lambda item: (-item[2], item[0])
            )
            match_options = [
                (f"{name} ({team_name})", str(uid),
                 (f"AI PICK • {score:g}" if i == 0 and score else f"{team_name} • {score:g}"))
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

    async def _cancel(self, interaction):
        self.stop()
        await interaction.response.edit_message(content="Prediction cancelled.", view=None)

    async def _save(self, interaction):
        if picks_done(self.picks) == 0:
            await interaction.response.send_message("Make at least one pick first.", ephemeral=True)
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
            self.cog.save_picks(self.ctx.guild.id, self.match["number"], self.ctx.author.id, self.picks)
            png = await self.cog.card(self.ctx.guild, self.ctx.author, self.match, *self.teams, self.picks)
            channel = self.cog.output_channel(self.ctx.guild, interaction.channel)
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
        """Order roster by imported performance; stable fallback is user ID."""
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
        field = {}
        try:
            for row in self.bot.db.execute(
                f"SELECT user_id,catches_caught,catches_dropped FROM fielding_stats WHERE guild_id=? AND user_id IN ({placeholders})",
                (int(guild_id), *roster)):
                field[int(row["user_id"])] = (int(row["catches_caught"]), int(row["catches_dropped"]))
        except Exception:
            pass
        scores = {}
        for uid in roster:
            player_rows = by_user.get(uid, [])
            if category == "runs":
                score = sum(int(r["runs"]) for r in player_rows)
            elif category == "wickets":
                score = sum(int(r["wickets"]) for r in player_rows)
            elif category == "catches":
                score = field.get(uid, (0, 0))[0]
            else:
                caught, dropped = field.get(uid, (0, 0))
                score = fantasy_raw_points(player_rows)[2] + caught * 5 - dropped * 10
            scores[uid] = score
        return sorted(roster, key=lambda uid: (-scores.get(uid, 0), uid)), scores

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
            if channel is not None and hasattr(channel, "send"):
                me = guild.me
                perms = channel.permissions_for(me) if me else None
                if perms and perms.send_messages and perms.attach_files:
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
            (guild_id, number, user_id, json.dumps(picks)))
        self.bot.db.commit()

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

    # ------------------------------------------------------------------ card
    async def card(self, guild, user, match, team_a, team_b, picks):
        compare = self.compare
        import types
        shim = types.SimpleNamespace(guild=guild)
        from .h2h import team_role_color
        core = compare.core
        sides = []
        for team in (team_a, team_b):
            sides.append({"name": str(team["name"]), "logo": await compare._logo_for(shim, team),
                          "color": team_role_color(core, guild, team) or (SIDE_A if not sides else SIDE_B)})
        winner = picks.get("winner")
        cats, wanted, side_of = {}, set(), {}
        for team_index, team in enumerate((team_a, team_b)):
            for uid in self._roster(team):
                side_of.setdefault(uid, team_index)
        ai_picks = {}
        for key, _t, _l in CATS:
            cats[key] = {s: picks.get(f"{key}:{s}") for s in "abm"}
            wanted |= {v for v in cats[key].values() if v is not None}
            ra, sa = self._ranked_roster(guild.id, team_a, key)
            rb, sb = self._ranked_roster(guild.id, team_b, key)
            all_ranked = sorted([(uid, score) for uid, score in sa.items()] +
                                [(uid, score) for uid, score in sb.items()],
                                key=lambda item: (-item[1], item[0]))
            ai_picks[key] = {
                "a": ra[0] if ra and sa.get(ra[0], 0) > 0 else None,
                "b": rb[0] if rb and sb.get(rb[0], 0) > 0 else None,
                "m": all_ranked[0][0] if all_ranked and all_ranked[0][1] > 0 else None,
            }
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
                people[uid] = {"name": compare._display_name(guild, uid), "avatar": await fetch_avatar(self.bot, member),
                               "color": sides[side_of.get(uid, 0)]["color"]}

        async def mine():
            return await fetch_avatar(self.bot, user)

        await asyncio.gather(*(one(u) for u in wanted), return_exceptions=True)
        data = {"guild": guild.name, "number": int(match["number"]),
                "user": {"name": compare._display_name(guild, user.id), "avatar": await mine()},
                "a": sides[0], "b": sides[1], "winner": None if winner is None else int(winner),
                "cats": cats, "people": people, "ai_picks": ai_picks}
        async with self._lock:
            return await render_png(render_prediction_png, data)

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
                lines.append(f"**#{r['number']}**  {a['name'] if a else '?'} vs {b['name'] if b else '?'}  "
                             f"({'closed' if r['closed'] else 'open'}, {count} prediction{'s' if count != 1 else ''})")
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
                    return await ctx.reply("This match already has an official result and points. Re-enter the result with +predictresult to correct scoring; it cannot be reopened.", mention_author=False)
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



    @commands.command(name="predictresult", aliases=["setpredictresult", "predresult"])
    @commands.guild_only()
    async def predictresult(self, ctx, *, text: str = ""):
        """Record the official prediction result and calculate points."""
        if not await owner_or_whitelisted(ctx, command_name="predictresult", app_owner=True):
            return await ctx.reply("Only the bot owner or a whitelisted role can enter prediction results.", mention_author=False)
        match = re.match(r"^#?(\d+)\s+(.*)$", text.strip())
        if not match:
            return await ctx.reply("Usage: +predictresult <match> winner=A|B runs_a=<ID> runs_b=<ID> runs_m=<ID> wickets_a=<ID> wickets_b=<ID> wickets_m=<ID> catches_a=<ID> catches_b=<ID> catches_m=<ID> fantasy_a=<ID> fantasy_b=<ID> fantasy_m=<ID>. Player fields are optional; use Discord mentions or IDs.", mention_author=False)
        number, raw = int(match.group(1)), match.group(2)
        row, error = self._match(ctx.guild.id, str(number))
        if error:
            return await ctx.reply(error, mention_author=False)
        team_a, team_b = self._team_by_id(row["team_a_id"]), self._team_by_id(row["team_b_id"])
        if not team_a or not team_b:
            return await ctx.reply("The teams for this match could not be found.", mention_author=False)
        values = dict(re.findall(r"([a-zA-Z_]+)=([^\s]+)", raw))
        allowed = {"winner"} | {f"{cat}_{side}" for cat, _, _ in CATS for side in ("a", "b", "m")}
        unknown = set(values) - allowed
        if unknown:
            return await ctx.reply("Unknown result field(s): " + ", ".join(sorted(unknown)), mention_author=False)
        if "winner" not in values:
            return await ctx.reply("You must include winner=A or winner=B.", mention_author=False)
        winner_text = values["winner"].lower()
        if winner_text in ("a", "0", team_a["name"].lower()):
            winner = 0
        elif winner_text in ("b", "1", team_b["name"].lower()):
            winner = 1
        else:
            return await ctx.reply("winner must be A or B.", mention_author=False)
        roster_a, roster_b = set(self._roster(team_a)), set(self._roster(team_b))
        roster_all = roster_a | roster_b
        result = {"winner": winner}
        for key, value in values.items():
            if key == "winner":
                continue
            uid_text = re.sub(r"\D", "", value)
            if not uid_text:
                return await ctx.reply(f"{key} must be a Discord user ID or mention.", mention_author=False)
            uid = int(uid_text)
            side = key.rsplit("_", 1)[1]
            valid = (uid in roster_a if side == "a" else uid in roster_b if side == "b" else uid in roster_all)
            if not valid:
                return await ctx.reply(f"The player supplied for {key} is not in the correct match roster.", mention_author=False)
            result[key] = uid
        self.bot.db.execute("INSERT INTO predict_results (guild_id, number, result) VALUES (?, ?, ?) ON CONFLICT(guild_id, number) DO UPDATE SET result=excluded.result, updated_at=CURRENT_TIMESTAMP", (ctx.guild.id, number, json.dumps(result)))
        self.bot.db.execute("UPDATE predict_matches SET closed=1 WHERE guild_id=? AND number=?", (ctx.guild.id, number))
        picks_rows = self.bot.db.execute("SELECT user_id,picks FROM predict_picks WHERE guild_id=? AND number=?", (ctx.guild.id, number)).fetchall()
        scored = []
        rank_positions = {}
        for cat, _, _ in CATS:
            ranked_a, scores_a = self._ranked_roster(ctx.guild.id, team_a, cat)
            ranked_b, scores_b = self._ranked_roster(ctx.guild.id, team_b, cat)
            ranked_match = sorted(
                [(uid, score) for uid, score in scores_a.items()] +
                [(uid, score) for uid, score in scores_b.items()],
                key=lambda item: (-item[1], item[0])
            )
            def tied_ranks(ordered, score_map):
                positions, previous, rank = {}, object(), 0
                for index, uid in enumerate(ordered):
                    score = score_map.get(uid, 0)
                    if index == 0 or score != previous:
                        rank = index
                    positions[uid] = rank
                    previous = score
                return positions
            match_order = [uid for uid, _score in ranked_match]
            match_scores = {uid: score for uid, score in ranked_match}
            rank_positions[cat] = {
                "a": tied_ranks(ranked_a, scores_a),
                "b": tied_ranks(ranked_b, scores_b),
                "m": tied_ranks(match_order, match_scores),
            }
        for saved in picks_rows:
            try:
                picks = json.loads(saved["picks"] or "{}")
            except (TypeError, ValueError):
                picks = {}
            points, details = 0, {}
            if picks.get("winner") is not None and int(picks["winner"]) == winner:
                points += 5
                details["winner"] = 5
            for cat, _, _ in CATS:
                for side in ("a", "b", "m"):
                    actual = result.get(f"{cat}_{side}")
                    picked = picks.get(f"{cat}:{side}")
                    if actual is None or picked is None:
                        continue
                    positions = rank_positions[cat][side]
                    if int(picked) not in positions or int(actual) not in positions:
                        award = 0
                    else:
                        distance = abs(positions[int(picked)] - positions[int(actual)])
                        award = 5 if distance == 0 else 3 if distance == 1 else 1 if distance == 2 else 0
                    points += award
                    details[f"{cat}:{side}"] = award
            self.bot.db.execute("INSERT INTO predict_scores (guild_id,number,user_id,points,details) VALUES (?,?,?,?,?) ON CONFLICT(guild_id,number,user_id) DO UPDATE SET points=excluded.points,details=excluded.details", (ctx.guild.id, number, int(saved["user_id"]), points, json.dumps(details)))
            scored.append((int(saved["user_id"]), points))
        self.bot.db.commit()
        scored.sort(key=lambda item: (-item[1], item[0]))
        preview = "\n".join(f"{i}. <@{uid}> — **{pts} pts**" for i, (uid, pts) in enumerate(scored[:10], 1)) or "No predictions were submitted for this match."
        await ctx.reply(f"Official result saved for match #{number}; predictions are now closed.\n\n**Prediction points**\n{preview}", mention_author=False, allowed_mentions=discord.AllowedMentions.none())

    @commands.command(name="predictlb", aliases=["predictleaderboard", "predictpoints"])
    @commands.guild_only()
    async def predictlb(self, ctx, number: int = None):
        """Show prediction points for a match, or cumulative points across all matches."""
        if number is None:
            rows = self.bot.db.execute("SELECT user_id,SUM(points) AS points,COUNT(*) AS matches FROM predict_scores WHERE guild_id=? GROUP BY user_id ORDER BY points DESC,user_id LIMIT 20", (ctx.guild.id,)).fetchall()
            title = "Overall prediction leaderboard"
        else:
            rows = self.bot.db.execute("SELECT user_id,points,1 AS matches FROM predict_scores WHERE guild_id=? AND number=? ORDER BY points DESC,user_id LIMIT 20", (ctx.guild.id, number)).fetchall()
            title = f"Prediction leaderboard • Match #{number}"
        if not rows:
            return await ctx.reply("No prediction points have been awarded yet.", mention_author=False)
        lines = [f"**{i}.** <@{int(r['user_id'])}> — **{int(r['points'])} pts**" + (f" • {int(r['matches'])} matches scored" if number is None else "") for i, r in enumerate(rows, 1)]
        await ctx.reply("**" + title + "**\n" + "\n".join(lines), mention_author=False, allowed_mentions=discord.AllowedMentions.none())
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
