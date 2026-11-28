import sqlite3
import discord
from discord.ext import commands

# Uses the SAME owner + command whitelist system from utility.py
from cogs.utility import owner_or_whitelisted


DB_PATH = "betting.db"

HEADER_EMOJI = "<:emoji_60:1562481891037290637>"
VS_EMOJI = "<:VS128:1559620966709993472>"
ARROW_EMOJI = "<a:arrow_arrow:1565966619291811984>"
LINE = "━━━━━━━━━━━━━━━━━━"


class Betting(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._setup_database()

    def _setup_database(self):
        conn = sqlite3.connect(DB_PATH)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS betting_team_emojis (
                team_name TEXT PRIMARY KEY COLLATE NOCASE,
                emoji TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS odds_sim_matches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                team_a TEXT NOT NULL,
                team_b TEXT NOT NULL,
                pool_a INTEGER NOT NULL DEFAULT 0,
                pool_b INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'open',
                winner TEXT,
                created_by INTEGER NOT NULL,
                created_at REAL NOT NULL
            )
        """)
        conn.commit()
        conn.close()

    async def _allowed(self, ctx, command_name=None):
        if await owner_or_whitelisted(ctx, command_name):
            return True
        await ctx.reply(
            "❌ You are not allowed to use this command.",
            mention_author=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return False

    def _find_team(self, query):
        query = (query or "").strip()
        if not query:
            return {"status": "not_found"}

        conn = sqlite3.connect(DB_PATH)

        # Exact name first
        row = conn.execute(
            "SELECT team_name, emoji FROM betting_team_emojis "
            "WHERE LOWER(team_name)=LOWER(?)",
            (query,),
        ).fetchone()

        if row:
            conn.close()
            return {
                "status": "found",
                "team_name": row[0],
                "emoji": row[1],
            }

        # Prefix match is preferred:
        # ERROR -> ERROR 404
        rows = conn.execute(
            "SELECT team_name, emoji FROM betting_team_emojis "
            "WHERE LOWER(team_name) LIKE LOWER(?) "
            "ORDER BY LENGTH(team_name), team_name COLLATE NOCASE",
            (query + "%",),
        ).fetchall()

        # If no prefix match, allow contained text too
        if not rows:
            rows = conn.execute(
                "SELECT team_name, emoji FROM betting_team_emojis "
                "WHERE LOWER(team_name) LIKE LOWER(?) "
                "ORDER BY LENGTH(team_name), team_name COLLATE NOCASE",
                ("%" + query + "%",),
            ).fetchall()

        conn.close()

        if len(rows) == 1:
            return {
                "status": "found",
                "team_name": rows[0][0],
                "emoji": rows[0][1],
            }

        if len(rows) > 1:
            return {
                "status": "multiple",
                "matches": [row[0] for row in rows],
            }

        return {"status": "not_found"}

    @commands.command(name="setteamemoji")
    @commands.guild_only()
    async def setteamemoji(self, ctx, *, args=None):
        """Save/update: +setteamemoji ERROR 404 <:E4:123>"""
        if not await self._allowed(ctx, "setteamemoji"):
            return

        if not args:
            await ctx.reply(
                "❌ Usage: `+setteamemoji <full team name> <emoji>`",
                mention_author=False,
            )
            return

        parts = args.rsplit(maxsplit=1)
        if len(parts) != 2:
            await ctx.reply(
                "❌ Usage: `+setteamemoji <full team name> <emoji>`",
                mention_author=False,
            )
            return

        team_name, emoji = parts[0].strip(), parts[1].strip()

        if not team_name or not emoji:
            await ctx.reply("❌ Please provide both the team name and emoji.", mention_author=False)
            return

        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "INSERT INTO betting_team_emojis(team_name, emoji) VALUES(?, ?) "
            "ON CONFLICT(team_name) DO UPDATE SET emoji=excluded.emoji",
            (team_name, emoji),
        )
        conn.commit()
        conn.close()

        await ctx.reply(
            f"✅ Saved **{discord.utils.escape_markdown(team_name)}** {emoji}",
            mention_author=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.command(name="removeteamemoji")
    @commands.guild_only()
    async def removeteamemoji(self, ctx, *, team_query=None):
        if not await self._allowed(ctx, "removeteamemoji"):
            return

        result = self._find_team(team_query)

        if result["status"] == "not_found":
            await ctx.reply("❌ No matching saved team was found.", mention_author=False)
            return

        if result["status"] == "multiple":
            await ctx.reply(
                "⚠️ Multiple teams found. Please be more specific:\n"
                + "\n".join(f"• **{discord.utils.escape_markdown(x)}**" for x in result["matches"][:15]),
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "DELETE FROM betting_team_emojis WHERE LOWER(team_name)=LOWER(?)",
            (result["team_name"],),
        )
        conn.commit()
        conn.close()

        await ctx.reply(
            f"✅ Removed emoji for **{discord.utils.escape_markdown(result['team_name'])}**.",
            mention_author=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.command(name="listteamemojis")
    @commands.guild_only()
    async def listteamemojis(self, ctx):
        if not await self._allowed(ctx, "listteamemojis"):
            return

        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute(
            "SELECT team_name, emoji FROM betting_team_emojis "
            "ORDER BY team_name COLLATE NOCASE"
        ).fetchall()
        conn.close()

        if not rows:
            await ctx.reply("❌ No team emojis have been saved yet.", mention_author=False)
            return

        message = "# <:emoji_60:1562481891037290637> `SAVED TEAM EMOJIS`\n\n"

        for team_name, emoji in rows:
            line = f"{emoji} **{discord.utils.escape_markdown(team_name)}**\n"
            if len(message) + len(line) > 1900:
                await ctx.send(message, allowed_mentions=discord.AllowedMentions.none())
                message = ""
            message += line

        if message.strip():
            await ctx.send(message, allowed_mentions=discord.AllowedMentions.none())

    @commands.command(name="teamemoji")
    @commands.guild_only()
    async def teamemoji(self, ctx, *, team_query=None):
        if not await self._allowed(ctx, "teamemoji"):
            return

        result = self._find_team(team_query)

        if result["status"] == "not_found":
            await ctx.reply("❌ No matching saved team was found.", mention_author=False)
            return

        if result["status"] == "multiple":
            await ctx.reply(
                "⚠️ Multiple teams found. Please be more specific:\n"
                + "\n".join(f"• **{discord.utils.escape_markdown(x)}**" for x in result["matches"][:15]),
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        await ctx.reply(
            f"{result['emoji']} **{discord.utils.escape_markdown(result['team_name'])}**",
            mention_author=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.command(name="betting")
    @commands.guild_only()
    async def betting(self, ctx, round_number=None, *, match_data=None):
        """
        Example:
        +betting 4 ERROR | 1.50 | MOONLIGHT | 1.50 ; PHANTOM | 1.80 | BOTUS | 1.40

        Format for every match:
        Team 1 | Team 1 odds | Team 2 | Team 2 odds

        Separate matches with ;
        """
        if not await self._allowed(ctx, "betting"):
            return

        if not round_number or not match_data:
            await ctx.reply(
                "❌ **Usage:**\n"
                "`+betting <round> <team1> | <odds1> | <team2> | <odds2> ; <team1> | <odds1> | <team2> | <odds2>`\n\n"
                "**Example:**\n"
                "`+betting 4 ERROR | 1.50 | MOONLIGHT | 1.50 ; PHANTOM | 1.80 | BOTUS | 1.40`",
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        raw_matches = [x.strip() for x in match_data.split(";") if x.strip()]

        if not raw_matches:
            await ctx.reply("❌ Please provide at least one match.", mention_author=False)
            return

        matches = []

        for index, raw in enumerate(raw_matches, start=1):
            parts = [x.strip() for x in raw.split("|")]

            if len(parts) != 4 or not all(parts):
                await ctx.reply(
                    f"❌ Match **{index}** is invalid.\n"
                    "Use: `Team 1 | Odds 1 | Team 2 | Odds 2`",
                    mention_author=False,
                )
                return

            team1_query, odds1, team2_query, odds2 = parts

            team1 = self._find_team(team1_query)
            if team1["status"] != "found":
                await self._send_team_error(ctx, index, team1_query, team1)
                return

            team2 = self._find_team(team2_query)
            if team2["status"] != "found":
                await self._send_team_error(ctx, index, team2_query, team2)
                return

            matches.append((team1, odds1, team2, odds2))

        # EXACT STYLE BASED ON YOUR FORMAT
        output = (
            f"# {HEADER_EMOJI} `KASHMIR TITANS S1 - BETTING [ROUND {round_number}]` "
            f"{HEADER_EMOJI}\n\n"
            f"{LINE}\n"
        )

        for number, (team1, odds1, team2, odds2) in enumerate(matches, start=1):
            output += (
                f"\n"
                f"### {number}. {team1['emoji']} {team1['team_name'].upper()} {team1['emoji']} "
                f"{VS_EMOJI} {team2['emoji']} {team2['team_name'].upper()} {team2['emoji']}\n\n"
                f"{ARROW_EMOJI}  __{team1['team_name']}__ — **__`{odds1}x`__**\n"
                f"{ARROW_EMOJI}  __{team2['team_name']}__ — **__`{odds2}x`__**\n"
            )

        output += f"\n{LINE}"

        # Discord's message limit
        if len(output) > 2000:
            await ctx.reply(
                "❌ The generated announcement is too long for one Discord message. "
                "Please use fewer matches.",
                mention_author=False,
            )
            return

        await ctx.send(
            output,
            allowed_mentions=discord.AllowedMentions.none(),
        )


    # Test-only odds model. Pool figures are hypothetical staff-entered inputs;
    # members cannot submit stakes and the simulator has no currency or payouts.
    @staticmethod
    def _sim_odds(pool, total):
        if pool <= 0 or total <= 0:
            return 1.50
        # 0.25x total-market margin, split 0.125x per outcome.
        # Equal pools: 1.875x + 1.875x = 3.75x combined.
        return max(1.50, (total / pool) - 0.125)

    def _sim_get(self, guild_id, match_id):
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM odds_sim_matches WHERE guild_id=? AND id=?",
            (int(guild_id), int(match_id)),
        ).fetchone()
        conn.close()
        return row

    def _sim_text(self, row):
        total = int(row["pool_a"]) + int(row["pool_b"])
        a = self._sim_odds(int(row["pool_a"]), total)
        b = self._sim_odds(int(row["pool_b"]), total)
        status = "OPEN" if row["status"] == "open" else "SETTLED — WINNER: " + str(row["winner"])
        return (
            "TEST ODDS SIMULATOR — MATCH #" + str(row["id"]) + "\n"
            + str(row["team_a"]) + " vs " + str(row["team_b"]) + "\n"
            + "Status: " + status + "\n\n"
            + "Hypothetical pool totals:\n"
            + str(row["team_a"]) + ": " + format(int(row["pool_a"]), ",") + " → " + format(a, ".2f") + "x\n"
            + str(row["team_b"]) + ": " + format(int(row["pool_b"]), ",") + " → " + format(b, ".2f") + "x\n"
            + "Combined test pool: " + format(total, ",") + "\n\n"
            + "SIMULATION ONLY — no member stakes, currency, or payouts."
        )

    @commands.command(name="oddscreate")
    @commands.guild_only()
    async def oddscreate(self, ctx, *, teams=None):
        if not await self._allowed(ctx, "oddscreate"):
            return
        parts = [x.strip() for x in (teams or "").split("|")]
        if len(parts) != 2 or not all(parts):
            return await ctx.reply("Usage: +oddscreate Team A | Team B", mention_author=False)
        names = []
        for query in parts:
            result = self._find_team(query)
            if result["status"] != "found":
                return await self._send_team_error(ctx, 1, query, result)
            names.append(result["team_name"])
        if names[0].casefold() == names[1].casefold():
            return await ctx.reply("Choose two different teams.", mention_author=False)
        conn = sqlite3.connect(DB_PATH)
        cur = conn.execute(
            "INSERT INTO odds_sim_matches(guild_id,team_a,team_b,created_by,created_at) "
            "VALUES(?,?,?,?,strftime('%s','now'))",
            (ctx.guild.id, names[0], names[1], ctx.author.id),
        )
        match_id = cur.lastrowid
        conn.commit()
        conn.close()
        await ctx.reply(self._sim_text(self._sim_get(ctx.guild.id, match_id)), mention_author=False)

    @commands.command(name="oddstest")
    @commands.guild_only()
    async def oddstest(self, ctx, match_id: int = None, *, pools=None):
        if not await self._allowed(ctx, "oddstest"):
            return
        if match_id is None or not pools:
            return await ctx.reply("Usage: +oddstest <match#> <pool A> | <pool B>\nExample: +oddstest 1 100000 | 100000", mention_author=False)
        row = self._sim_get(ctx.guild.id, match_id)
        if row is None:
            return await ctx.reply("Test match not found in this server.", mention_author=False)
        if row["status"] != "open":
            return await ctx.reply("This test scenario is already settled.", mention_author=False)
        values = [x.strip().replace(",", "") for x in pools.split("|")]
        try:
            if len(values) != 2:
                raise ValueError
            pool_a, pool_b = (int(x) for x in values)
            if pool_a < 0 or pool_b < 0 or pool_a + pool_b <= 0:
                raise ValueError
        except ValueError:
            return await ctx.reply("Enter two whole-number totals, zero or higher, and not both zero.", mention_author=False)
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "UPDATE odds_sim_matches SET pool_a=?,pool_b=? WHERE guild_id=? AND id=? AND status='open'",
            (pool_a, pool_b, ctx.guild.id, match_id),
        )
        conn.commit()
        conn.close()
        await ctx.reply(self._sim_text(self._sim_get(ctx.guild.id, match_id)), mention_author=False)

    @commands.command(name="oddsview")
    @commands.guild_only()
    async def oddsview(self, ctx, match_id: int = None):
        if match_id is None:
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM odds_sim_matches WHERE guild_id=? ORDER BY id DESC LIMIT 1",
                (ctx.guild.id,),
            ).fetchone()
            conn.close()
        else:
            row = self._sim_get(ctx.guild.id, match_id)
        if row is None:
            return await ctx.reply("No test scenario found. Staff can create one with +oddscreate.", mention_author=False)
        await ctx.reply(self._sim_text(row), mention_author=False)

    @commands.command(name="oddsresult")
    @commands.guild_only()
    async def oddsresult(self, ctx, match_id: int = None, *, winner=None):
        if not await self._allowed(ctx, "oddsresult"):
            return
        if match_id is None or not winner:
            return await ctx.reply("Usage: +oddsresult <match#> <winning team>", mention_author=False)
        row = self._sim_get(ctx.guild.id, match_id)
        if row is None:
            return await ctx.reply("Test match not found in this server.", mention_author=False)
        if row["status"] != "open":
            return await ctx.reply("This test scenario is already settled.", mention_author=False)
        chosen = next((n for n in (row["team_a"], row["team_b"]) if n.casefold() == winner.strip().casefold()), None)
        if chosen is None:
            return await ctx.reply("Winner must be " + row["team_a"] + " or " + row["team_b"] + ".", mention_author=False)
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "UPDATE odds_sim_matches SET status='settled',winner=? WHERE guild_id=? AND id=?",
            (chosen, ctx.guild.id, match_id),
        )
        conn.commit()
        conn.close()
        await ctx.reply("Test scenario #" + str(match_id) + " settled. Winner: " + chosen + "\n\n" + self._sim_text(self._sim_get(ctx.guild.id, match_id)), mention_author=False)

    @commands.command(name="oddsdelete")
    @commands.guild_only()
    async def oddsdelete(self, ctx, match_id: int = None):
        if not await self._allowed(ctx, "oddsdelete"):
            return
        if match_id is None:
            return await ctx.reply("Usage: +oddsdelete <match#>", mention_author=False)
        conn = sqlite3.connect(DB_PATH)
        cur = conn.execute("DELETE FROM odds_sim_matches WHERE guild_id=? AND id=?", (ctx.guild.id, match_id))
        conn.commit()
        deleted = cur.rowcount
        conn.close()
        await ctx.reply("Deleted test scenario #" + str(match_id) + "." if deleted else "Test match not found.", mention_author=False)

    async def _send_team_error(self, ctx, match_number, query, result):
        if result["status"] == "multiple":
            choices = "\n".join(
                f"• **{discord.utils.escape_markdown(x)}**"
                for x in result["matches"][:15]
            )
            await ctx.reply(
                f"⚠️ Match **{match_number}**: `{query}` matches multiple teams:\n{choices}\n"
                "Please use a more specific team name.",
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        await ctx.reply(
            f"❌ Match **{match_number}**: No saved team found for `{query}`.\n"
            "Use `+setteamemoji <full team name> <emoji>` first.",
            mention_author=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )


async def setup(bot):
    await bot.add_cog(Betting(bot))
