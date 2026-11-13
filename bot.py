import os
import sqlite3
from pathlib import Path

import discord
from discord.ext import commands
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID", "0"))
DB_FILE = BASE_DIR / "matches.db"

if not TOKEN or TOKEN == "PASTE_YOUR_NEW_BOT_TOKEN_HERE":
    raise RuntimeError("Set DISCORD_TOKEN in your .env file.")
if not GUILD_ID:
    raise RuntimeError("Set GUILD_ID in your .env file.")

intents = discord.Intents.default()
intents.members = True
intents.message_content = True
intents.reactions = True


class GoogIManagerBot(commands.Bot):
    async def setup_hook(self):
        # Load each feature as a separate module.
        for extension in (
            "cogs.utility",
            "cogs.scheduling",
            "cogs.matchday",
            "cogs.stats",
            "cogs.lb",
            "cogs.teams1",
            "cogs.teamleaders",
            "cogs.predictions",
            "cogs.draws",
            "cogs.custom_image_commands",
            "cogs.mvp",
            "cogs.results",
            "cogs.matchnumbers",
            "cogs.autoresults",
            "cogs.dm",
            "cogs.noteam",
            "cogs.reserves",
            "cogs.groups",
            "cogs.autolock",
            "cogs.help_menu",
            "cogs.fielding",
            "cogs.points table",
            "cogs.batfds",
            "cogs.fielderlines",
            "cogs.team_emojis",
            "cogs.h2h",
            "cogs.betting",
            "cogs.reservenumbering",
            "cogs.batcatches",
        ):
            await self.load_extension(extension)

        guild = discord.Object(id=GUILD_ID)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)
        print("Slash commands synced to the configured server.")


bot = GoogIManagerBot(
    command_prefix=("+"),
    intents=intents,
    help_command=None,
)

bot.db = sqlite3.connect(DB_FILE)
bot.db.row_factory = sqlite3.Row
bot.db.execute("PRAGMA foreign_keys = ON")


def initialize_database():
    db = bot.db

    db.execute("""
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )""")

    db.execute("""
    CREATE TABLE IF NOT EXISTS matches (
        id TEXT PRIMARY KEY,
        guild_id INTEGER NOT NULL,
        team_a_id INTEGER NOT NULL,
        team_b_id INTEGER NOT NULL,
        channel_id INTEGER NOT NULL,
        start_time REAL NOT NULL,
        status TEXT NOT NULL,
        created_by INTEGER NOT NULL,
        created_at REAL NOT NULL,
        original_send_messages INTEGER,
        error TEXT
    )""")

    columns = {
        r["name"]
        for r in db.execute("PRAGMA table_info(matches)")
    }

    if "announcement_message_id" not in columns:
        db.execute(
            "ALTER TABLE matches "
            "ADD COLUMN announcement_message_id INTEGER"
        )

    db.execute("""
    CREATE TABLE IF NOT EXISTS stats_imports (
        source_message_id INTEGER PRIMARY KEY,
        guild_id INTEGER NOT NULL,
        imported_by INTEGER NOT NULL,
        imported_at REAL NOT NULL
    )""")

    db.execute("""
    CREATE TABLE IF NOT EXISTS player_match_stats (
        source_message_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        runs INTEGER NOT NULL,
        balls_faced INTEGER NOT NULL,
        runs_conceded INTEGER NOT NULL,
        balls_bowled INTEGER NOT NULL,
        wickets INTEGER NOT NULL,
        not_out INTEGER NOT NULL,
        PRIMARY KEY (source_message_id, user_id),
        FOREIGN KEY (source_message_id)
            REFERENCES stats_imports(source_message_id)
            ON DELETE CASCADE
    )""")

    db.commit()


initialize_database()


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")


bot.run(TOKEN)
