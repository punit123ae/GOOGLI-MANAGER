import asyncio
import hashlib
import json
import os
import sqlite3
import time
import traceback
from pathlib import Path

import discord
from discord.ext import commands
from dotenv import load_dotenv

try:   # fewer glibc memory arenas: image threads otherwise each keep their own pile of freed memory
    import ctypes
    ctypes.CDLL("libc.so.6").mallopt(-8, 2)       # M_ARENA_MAX = 2
except Exception:
    pass

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
# Events nothing in this bot listens to; each one is a stream of packets to decode on a 25% CPU host.
intents.typing = False
intents.voice_states = False
intents.invites = False
intents.webhooks = False
intents.integrations = False
intents.scheduled_events = False
intents.presences = False


SYNC_HASH_FILE = BASE_DIR / ".command_sync_hash"


def _command_signature(tree, guild):
    """A short fingerprint of every slash command's name, description and options."""
    parts = []
    for command in tree.get_commands(guild=guild):
        nodes = [command] + (list(command.walk_commands()) if hasattr(command, "walk_commands") else [])
        for node in nodes:
            options = [(p.name, p.description, str(p.type), p.required) for p in getattr(node, "parameters", [])]
            parts.append((node.qualified_name, getattr(node, "description", ""), options))
    return hashlib.sha256(json.dumps(sorted(parts, key=str), default=str).encode()).hexdigest()


class GoogIManagerBot(commands.Bot):
    async def _sync_slash_commands(self, guild):
        """Sync slash commands only when they changed, so restarts don't hit Discord's rate limits."""
        try:
            signature = _command_signature(self.tree, guild)
        except Exception as exc:
            print(f"Could not fingerprint slash commands ({type(exc).__name__}); syncing anyway.")
            signature = None
        try:
            with open(SYNC_HASH_FILE, encoding="utf-8") as handle:
                saved = handle.read().strip()
        except OSError:
            saved = None
        if signature is not None and signature == saved:
            print("Slash commands unchanged; skipping sync.")
            return
        try:
            await self.tree.sync(guild=guild)
        except discord.HTTPException as exc:
            print(f"Slash command sync failed ({exc.status}); the bot keeps running with the old commands.")
            return
        if signature is not None:
            try:
                with open(SYNC_HASH_FILE, "w", encoding="utf-8") as handle:
                    handle.write(signature)
            except OSError:
                pass
        print("Slash commands synced to the configured server.")

    async def setup_hook(self):
        # Drawing images happens in worker threads; two are plenty and keep memory down.
        import concurrent.futures
        asyncio.get_running_loop().set_default_executor(
            concurrent.futures.ThreadPoolExecutor(max_workers=3, thread_name_prefix="googli"))
        # Load each feature as a separate module.
        for extension in (
            "cogs.utility",
            "cogs.scheduling",
            "cogs.matchday",
            "cogs.reschedule",
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
            "cogs.fielderlines",
            "cogs.team_emojis",
            "cogs.h2h",
            "cogs.betting",
            "cogs.reservenumbering",
            "cogs.batcatches",
            "cogs.moderation",
            "cogs.compareteams",
            "cogs.matchpreview",
            "cogs.xi",
            "cogs.potd",
            "cogs.insights",
            "cogs.memory_trim",
            "cogs.testversus",
            "cogs.awards",
            "cogs.similar",
            "cogs.strengths",
            "cogs.streaks",
            "cogs.qualify",
        ):
            await self.load_extension(extension)

        guild = discord.Object(id=GUILD_ID)
        self.tree.copy_global_to(guild=guild)
        await self._sync_slash_commands(guild)


bot = GoogIManagerBot(
    command_prefix=("+"),
    intents=intents,
    help_command=None,
    max_messages=200,          # default 1000 cached messages is a lot on a 256 MB host
)

bot.db = sqlite3.connect(DB_FILE)
bot.db.row_factory = sqlite3.Row
bot.db.execute("PRAGMA foreign_keys = ON")
bot.db.execute("PRAGMA cache_size = -4000")      # 4 MB page cache instead of letting it grow
bot.db.execute("PRAGMA mmap_size = 0")           # mapped DB pages would count against the RAM limit
bot.db.execute("PRAGMA temp_store = FILE")       # big sorts go to disk, not memory


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

    stats_import_columns = {r["name"] for r in db.execute("PRAGMA table_info(stats_imports)")}
    if "competition" not in stats_import_columns:
        db.execute("ALTER TABLE stats_imports ADD COLUMN competition TEXT NOT NULL DEFAULT 'MAIN TOURNAMENT'")

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


def _log_command(kind, command_name, user, guild, status):
    from datetime import datetime, timezone

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    server_name = guild.name if guild else "DM"
    server_id = guild.id if guild else "N/A"
    user_name = f"{user} ({user.id})" if user else "Unknown"
    print(
        f"[COMMAND {kind}] [{timestamp}] "
        f"server={server_name!r} ({server_id}) "
        f"user={user_name} command={command_name!r} status={status}",
        flush=True,
    )


@bot.event
async def on_command_completion(ctx):
    _log_command(
        "PREFIX", ctx.command.qualified_name, ctx.author, ctx.guild, "SUCCESS"
    )


@bot.event
async def on_command_error(ctx, error):
    # Log command errors too, including failed checks and invalid arguments.
    _log_command(
        "PREFIX", ctx.command.qualified_name if ctx.command else "unknown",
        ctx.author, ctx.guild, f"ERROR: {type(error).__name__}: {error}"
    )


@bot.event
async def on_interaction(interaction):
    # Only log slash/application commands, not buttons, selects, or other components.
    if interaction.type is discord.InteractionType.application_command:
        command = interaction.command
        command_name = (
            command.qualified_name if command is not None
            else interaction.data.get("name", "unknown")
        )
        _log_command(
            "SLASH", command_name, interaction.user, interaction.guild, "RECEIVED"
        )


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    if not getattr(bot, "_gc_tuned", False):
        # Everything loaded so far lives for the whole run: park it so garbage collections skip it
        # (less CPU), and collect the young generation less often.
        import gc
        gc.collect()
        gc.freeze()
        gc.set_threshold(2000, 20, 20)
        bot._gc_tuned = True



def _run_forever_guarded():
    """Start the bot; if it dies on startup, wait before exiting so the panel's auto-restart
    doesn't hammer Discord with logins (that gets the host's IP rate-limited)."""
    try:
        bot.run(TOKEN)
    except discord.HTTPException as exc:
        print(f"Discord refused the login ({exc.status}). Waiting 5 minutes before exiting.", flush=True)
        time.sleep(300)
        raise
    except Exception:
        traceback.print_exc()
        print("The bot crashed while starting. Waiting 60 seconds before exiting.", flush=True)
        time.sleep(60)
        raise SystemExit(1)


_run_forever_guarded()
