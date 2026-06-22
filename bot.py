import discord
from discord.ext import commands, tasks
from discord import app_commands
import aiosqlite
import os
from datetime import datetime, timedelta, date
import pytz
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
DB_PATH = os.getenv("DB_PATH", "dnd_scheduler.db")

# Ensure the directory for the DB file exists
_db_dir = os.path.dirname(DB_PATH)
if _db_dir:
    os.makedirs(_db_dir, exist_ok=True)

# ─── Helpers ───────────────────────────────────────────────────────────────────

DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
DAYS_SHORT = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

def get_week_start(offset_weeks: int = 0) -> date:
    """Return the Monday of the current week + offset."""
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    return monday + timedelta(weeks=offset_weeks)

def week_label(monday: date) -> str:
    sunday = monday + timedelta(days=6)
    return f"{monday.strftime('%b %d')} – {sunday.strftime('%b %d, %Y')}"

def date_of_day(monday: date, day_index: int) -> date:
    return monday + timedelta(days=day_index)

# ─── Database ──────────────────────────────────────────────────────────────────

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS guild_config (
                guild_id       INTEGER PRIMARY KEY,
                paused         INTEGER DEFAULT 0,
                remind_channel INTEGER DEFAULT NULL,
                auto_lock      INTEGER DEFAULT 1
            )
        """)
        # Migrate existing rows that predate the auto_lock column
        try:
            await db.execute("ALTER TABLE guild_config ADD COLUMN auto_lock INTEGER DEFAULT 1")
        except Exception:
            pass  # Column already exists
        await db.execute("""
            CREATE TABLE IF NOT EXISTS availability (
                guild_id    INTEGER NOT NULL,
                user_id     INTEGER NOT NULL,
                username    TEXT NOT NULL,
                week_start  TEXT NOT NULL,   -- ISO date of that Monday
                day_index   INTEGER NOT NULL, -- 0=Mon … 6=Sun
                available   INTEGER NOT NULL, -- 1=yes 0=no
                PRIMARY KEY (guild_id, user_id, week_start, day_index)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS locked_weeks (
                guild_id    INTEGER NOT NULL,
                week_start  TEXT NOT NULL,
                PRIMARY KEY (guild_id, week_start)
            )
        """)
        await db.commit()

async def ensure_guild(guild_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO guild_config (guild_id) VALUES (?)", (guild_id,)
        )
        await db.commit()

async def is_paused(guild_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT paused FROM guild_config WHERE guild_id = ?", (guild_id,)
        ) as cur:
            row = await cur.fetchone()
            return bool(row[0]) if row else False

async def is_locked(guild_id: int, week_start: date) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT 1 FROM locked_weeks WHERE guild_id = ? AND week_start = ?",
            (guild_id, week_start.isoformat()),
        ) as cur:
            return await cur.fetchone() is not None

async def get_auto_lock(guild_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT auto_lock FROM guild_config WHERE guild_id = ?", (guild_id,)
        ) as cur:
            row = await cur.fetchone()
            return bool(row[0]) if row else True

async def get_remind_channel(guild_id: int) -> int | None:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT remind_channel FROM guild_config WHERE guild_id = ?", (guild_id,)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else None

async def set_remind_channel(guild_id: int, channel_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE guild_config SET remind_channel = ? WHERE guild_id = ?",
            (channel_id, guild_id),
        )
        await db.commit()

async def save_availability(guild_id: int, user_id: int, username: str,
                             week_start: date, days: list[int]):
    """days = list of day indices (0-6) the user is available."""
    async with aiosqlite.connect(DB_PATH) as db:
        # Clear previous entries for this week
        await db.execute(
            "DELETE FROM availability WHERE guild_id=? AND user_id=? AND week_start=?",
            (guild_id, user_id, week_start.isoformat()),
        )
        for d in range(7):
            await db.execute(
                """INSERT INTO availability (guild_id, user_id, username, week_start, day_index, available)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (guild_id, user_id, username, week_start.isoformat(), d, 1 if d in days else 0),
            )
        await db.commit()

async def get_availability(guild_id: int, week_start: date) -> dict[int, dict[int, bool]]:
    """Returns {user_id: {day_index: available}}"""
    result: dict[int, dict[int, bool]] = {}
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT user_id, day_index, available FROM availability WHERE guild_id=? AND week_start=?",
            (guild_id, week_start.isoformat()),
        ) as cur:
            async for user_id, day_index, available in cur:
                result.setdefault(user_id, {})[day_index] = bool(available)
    return result

async def get_usernames(guild_id: int, week_start: date) -> dict[int, str]:
    result: dict[int, str] = {}
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT DISTINCT user_id, username FROM availability WHERE guild_id=? AND week_start=?",
            (guild_id, week_start.isoformat()),
        ) as cur:
            async for user_id, username in cur:
                result[user_id] = username
    return result

async def get_users_submitted(guild_id: int, week_start: date) -> set[int]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT DISTINCT user_id FROM availability WHERE guild_id=? AND week_start=?",
            (guild_id, week_start.isoformat()),
        ) as cur:
            return {row[0] async for row in cur}

async def lock_week(guild_id: int, week_start: date):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO locked_weeks (guild_id, week_start) VALUES (?, ?)",
            (guild_id, week_start.isoformat()),
        )
        await db.commit()

async def unlock_week(guild_id: int, week_start: date):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM locked_weeks WHERE guild_id = ? AND week_start = ?",
            (guild_id, week_start.isoformat()),
        )
        await db.commit()

async def purge_old_weeks(guild_id: int):
    """Remove data older than 2 weeks ago."""
    cutoff = (get_week_start(-2)).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM availability WHERE guild_id=? AND week_start < ?",
            (guild_id, cutoff),
        )
        await db.execute(
            "DELETE FROM locked_weeks WHERE guild_id=? AND week_start < ?",
            (guild_id, cutoff),
        )
        await db.commit()

# ─── Bot setup ─────────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.members = True

class DnDBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        await init_db()
        await self.add_cog(SchedulerCog(self))
        await self.tree.sync()
        print("Commands synced.")

    async def on_ready(self):
        print(f"Logged in as {self.user} (ID: {self.user.id})")

bot = DnDBot()

# ─── Availability Button View ─────────────────────────────────────────────────

class DayToggleButton(discord.ui.Button):
    def __init__(self, day_index: int, day_date: date):
        self.day_index = day_index
        label = f"{DAYS_SHORT[day_index]}\n{day_date.strftime('%b %d')}"
        super().__init__(
            label=label,
            style=discord.ButtonStyle.secondary,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction):
        view: AvailabilityView = self.view
        if self.day_index in view.selected:
            view.selected.discard(self.day_index)
            self.style = discord.ButtonStyle.secondary
        else:
            view.selected.add(self.day_index)
            self.style = discord.ButtonStyle.success
        await interaction.response.edit_message(
            content=view.build_prompt(), view=view
        )


class AvailabilityView(discord.ui.View):
    def __init__(self, week_start: date, user: discord.Member, previously_selected: set[int]):
        super().__init__(timeout=120)
        self.week_start = week_start
        self.user = user
        self.selected: set[int] = set(previously_selected)

        for i in range(7):
            btn = DayToggleButton(i, date_of_day(week_start, i))
            if i in self.selected:
                btn.style = discord.ButtonStyle.success
            self.add_item(btn)

    def build_prompt(self) -> str:
        header = f"📅 **{self.user.display_name}** — {week_label(self.week_start)}\n"
        if self.selected:
            days = ", ".join(DAYS[i] for i in sorted(self.selected))
            header += f"Selected: **{days}**\n"
        else:
            header += "Selected: *none — click days to toggle them*\n"
        header += "\nTap days to toggle availability, then confirm."
        return header

    @discord.ui.button(label="✅ Confirm", style=discord.ButtonStyle.primary, row=1)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("This isn't your availability picker!", ephemeral=True)
            return

        await save_availability(
            interaction.guild_id,
            self.user.id,
            self.user.display_name,
            self.week_start,
            list(self.selected),
        )

        self.stop()
        for child in self.children:
            child.disabled = True

        if self.selected:
            day_names = ", ".join(DAYS[i] for i in sorted(self.selected))
            msg = f"✅ **{self.user.display_name}**, saved for **{week_label(self.week_start)}**:\n> {day_names}"
        else:
            msg = f"✅ **{self.user.display_name}**, marked as **unavailable** for **{week_label(self.week_start)}**."

        await interaction.response.edit_message(content=msg, view=self)

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.danger, row=1)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("This isn't your availability picker!", ephemeral=True)
            return
        self.stop()
        await interaction.response.edit_message(content="❌ Cancelled — nothing was saved.", view=None)

    async def on_timeout(self):
        # Silently expire — the ephemeral message just stops responding
        self.stop()


# ─── Cog ───────────────────────────────────────────────────────────────────────

class SchedulerCog(commands.Cog):
    def __init__(self, bot: DnDBot):
        self.bot = bot
        self.weekly_reminder.start()
        self.weekly_rollover.start()

    def cog_unload(self):
        self.weekly_reminder.cancel()
        self.weekly_rollover.cancel()

    # ── /availability ──────────────────────────────────────────────────────────

    @app_commands.command(name="availability", description="Set your availability for the upcoming week.")
    @app_commands.describe(week="Which week? (current or next)")
    @app_commands.choices(week=[
        app_commands.Choice(name="Next week (default)", value="next"),
        app_commands.Choice(name="Current week", value="current"),
    ])
    async def availability(self, interaction: discord.Interaction, week: str = "next"):
        await ensure_guild(interaction.guild_id)
        offset = 0 if week == "current" else 1
        monday = get_week_start(offset)

        if await is_locked(interaction.guild_id, monday):
            await interaction.response.send_message(
                f"⛔ The week of **{week_label(monday)}** is locked and can no longer be edited.",
                ephemeral=True,
            )
            return

        # Pre-load any existing selection for this user
        existing = await get_availability(interaction.guild_id, monday)
        user_avail = existing.get(interaction.user.id, {})
        previously_selected = {d for d, avail in user_avail.items() if avail}

        view = AvailabilityView(monday, interaction.user, previously_selected)
        await interaction.response.send_message(
            content=view.build_prompt(),
            view=view,
            ephemeral=True,
        )


    # ── /dates ─────────────────────────────────────────────────────────────────

    @app_commands.command(name="dates", description="Show days where everyone who responded is available.")
    @app_commands.describe(week="Which week to check")
    @app_commands.choices(week=[
        app_commands.Choice(name="Next week (default)", value="next"),
        app_commands.Choice(name="Current week", value="current"),
    ])
    async def dates(self, interaction: discord.Interaction, week: str = "next"):
        await ensure_guild(interaction.guild_id)
        offset = 0 if week == "current" else 1
        monday = get_week_start(offset)

        avail = await get_availability(interaction.guild_id, monday)
        usernames = await get_usernames(interaction.guild_id, monday)

        if not avail:
            await interaction.response.send_message(
                f"📭 No availability submitted yet for **{week_label(monday)}**.", ephemeral=False
            )
            return

        # Days where ALL respondents are available
        all_days: list[int] = []
        for day in range(7):
            if all(avail[uid].get(day, False) for uid in avail):
                all_days.append(day)

        embed = discord.Embed(
            title=f"📅 Availability — {week_label(monday)}",
            color=0x5865F2,
        )

        if all_days:
            day_lines = "\n".join(
                f"✅ **{DAYS[d]}** ({date_of_day(monday, d).strftime('%b %d')})"
                for d in all_days
            )
            embed.add_field(name="🎲 Everyone's free on:", value=day_lines, inline=False)
        else:
            embed.add_field(name="😬 No day works for everyone.", value="See overview below.", inline=False)

        # Per-person summary
        lines = []
        for uid, days in avail.items():
            name = usernames.get(uid, f"<@{uid}>")
            free = [DAYS_SHORT[d] for d in range(7) if days.get(d)]
            lines.append(f"**{name}**: {', '.join(free) if free else 'none'}")
        embed.add_field(name="👥 Individual availability", value="\n".join(lines), inline=False)

        locked = await is_locked(interaction.guild_id, monday)
        embed.set_footer(text="🔒 Week is locked." if locked else "✏️ Week is open for edits.")

        await interaction.response.send_message(embed=embed)

    # ── /overview ─────────────────────────────────────────────────────────────

    @app_commands.command(name="overview", description="Show a full availability grid for the week.")
    @app_commands.describe(week="Which week")
    @app_commands.choices(week=[
        app_commands.Choice(name="Next week (default)", value="next"),
        app_commands.Choice(name="Current week", value="current"),
    ])
    async def overview(self, interaction: discord.Interaction, week: str = "next"):
        await ensure_guild(interaction.guild_id)
        offset = 0 if week == "current" else 1
        monday = get_week_start(offset)

        avail = await get_availability(interaction.guild_id, monday)
        usernames = await get_usernames(interaction.guild_id, monday)

        if not avail:
            await interaction.response.send_message(
                f"📭 No availability submitted yet for **{week_label(monday)}**.", ephemeral=False
            )
            return

        header = "```\n"
        header += f"{'Player':<16}" + "".join(f"{DAYS_SHORT[d]:^5}" for d in range(7)) + "\n"
        header += "─" * (16 + 35) + "\n"

        rows = ""
        for uid, days in avail.items():
            name = usernames.get(uid, str(uid))[:15]
            row = f"{name:<16}" + "".join("  ✓  " if days.get(d) else "  ✗  " for d in range(7))
            rows += row + "\n"

        # Totals row
        totals = f"{'Total':<16}" + "".join(
            f"{sum(1 for uid in avail if avail[uid].get(d)):^5}"
            for d in range(7)
        )
        rows += "─" * (16 + 35) + "\n"
        rows += totals + "\n```"

        embed = discord.Embed(
            title=f"📊 Availability Grid — {week_label(monday)}",
            description=header + rows,
            color=0x57F287,
        )
        await interaction.response.send_message(embed=embed)

    # ── /status ───────────────────────────────────────────────────────────────

    @app_commands.command(name="status", description="See who has and hasn't submitted their availability.")
    @app_commands.describe(week="Which week")
    @app_commands.choices(week=[
        app_commands.Choice(name="Next week (default)", value="next"),
        app_commands.Choice(name="Current week", value="current"),
    ])
    async def status(self, interaction: discord.Interaction, week: str = "next"):
        await ensure_guild(interaction.guild_id)
        offset = 0 if week == "current" else 1
        monday = get_week_start(offset)

        submitted = await get_users_submitted(interaction.guild_id, monday)
        members = [m for m in interaction.guild.members if not m.bot]

        done = [m for m in members if m.id in submitted]
        pending = [m for m in members if m.id not in submitted]

        embed = discord.Embed(
            title=f"📋 Submission Status — {week_label(monday)}",
            color=0xFEE75C,
        )
        embed.add_field(
            name=f"✅ Submitted ({len(done)})",
            value="\n".join(m.display_name for m in done) or "Nobody yet",
            inline=True,
        )
        embed.add_field(
            name=f"⏳ Pending ({len(pending)})",
            value="\n".join(m.display_name for m in pending) or "Everyone's in!",
            inline=True,
        )
        await interaction.response.send_message(embed=embed)

    # ── /remind ───────────────────────────────────────────────────────────────

    @app_commands.command(name="remind", description="Manually remind everyone who hasn't submitted their availability.")
    async def remind(self, interaction: discord.Interaction):
        await ensure_guild(interaction.guild_id)
        next_monday = get_week_start(1)

        submitted = await get_users_submitted(interaction.guild_id, next_monday)
        pending = [m for m in interaction.guild.members if not m.bot and m.id not in submitted]

        if not pending:
            await interaction.response.send_message(
                f"✅ Everyone has already submitted their availability for **{week_label(next_monday)}**!",
                ephemeral=True,
            )
            return

        mentions = " ".join(m.mention for m in pending)
        await interaction.response.send_message(
            f"📣 Hey {mentions}! Don't forget to submit your availability for "
            f"**{week_label(next_monday)}** using `/availability`!"
        )

    # ── /unlock-week ──────────────────────────────────────────────────────────

    @app_commands.command(name="unlock-week", description="[Admin] Unlock a week so availability can be edited again.")
    @app_commands.describe(week="Which week to unlock")
    @app_commands.choices(week=[
        app_commands.Choice(name="Current week (default)", value="current"),
        app_commands.Choice(name="Next week", value="next"),
    ])
    @app_commands.checks.has_permissions(manage_guild=True)
    async def unlock_week_cmd(self, interaction: discord.Interaction, week: str = "current"):
        await ensure_guild(interaction.guild_id)
        offset = 0 if week == "current" else 1
        monday = get_week_start(offset)
        await unlock_week(interaction.guild_id, monday)
        await interaction.response.send_message(
            f"🔓 Week of **{week_label(monday)}** is now unlocked. Availability can be edited again.",
            ephemeral=True,
        )

    # ── /auto-lock-enable ─────────────────────────────────────────────────────

    @app_commands.command(name="auto-lock-enable", description="[Admin] Enable automatic week locking on Monday.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def auto_lock_enable(self, interaction: discord.Interaction):
        await ensure_guild(interaction.guild_id)
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE guild_config SET auto_lock = 1 WHERE guild_id = ?", (interaction.guild_id,)
            )
            await db.commit()
        await interaction.response.send_message(
            "🔒 Auto-lock enabled. Weeks will automatically lock every Monday at 00:00 UTC.",
            ephemeral=True,
        )

    # ── /auto-lock-disable ────────────────────────────────────────────────────

    @app_commands.command(name="auto-lock-disable", description="[Admin] Disable automatic week locking on Monday.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def auto_lock_disable(self, interaction: discord.Interaction):
        await ensure_guild(interaction.guild_id)
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE guild_config SET auto_lock = 0 WHERE guild_id = ?", (interaction.guild_id,)
            )
            await db.commit()
        await interaction.response.send_message(
            "🔓 Auto-lock disabled. Weeks will stay open until you manually lock them with `/lock-week`.",
            ephemeral=True,
        )

    # ── /settings ─────────────────────────────────────────────────────────────

    @app_commands.command(name="settings", description="Show current bot settings for this server.")
    async def settings(self, interaction: discord.Interaction):
        await ensure_guild(interaction.guild_id)
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT paused, remind_channel, auto_lock FROM guild_config WHERE guild_id = ?",
                (interaction.guild_id,),
            ) as cur:
                row = await cur.fetchone()

        paused, channel_id, auto_lock = row if row else (0, None, 1)

        current_monday = get_week_start(0)
        next_monday = get_week_start(1)
        current_locked = await is_locked(interaction.guild_id, current_monday)
        next_locked = await is_locked(interaction.guild_id, next_monday)

        channel_str = f"<#{channel_id}>" if channel_id else "❌ Not set — use `/set-reminder-channel`"

        embed = discord.Embed(
            title="⚙️ Server Settings",
            color=0xEB459E,
        )
        embed.add_field(
            name="🔔 Reminders",
            value=(
                f"**Status:** {'⏸️ Paused' if paused else '▶️ Active'}\n"
                f"**Channel:** {channel_str}"
            ),
            inline=False,
        )
        embed.add_field(
            name="🔒 Locking",
            value=(
                f"**Auto-lock:** {'✅ Enabled' if auto_lock else '❌ Disabled'}\n"
                f"**Current week:** {'🔒 Locked' if current_locked else '🔓 Open'} ({week_label(current_monday)})\n"
                f"**Next week:** {'🔒 Locked' if next_locked else '🔓 Open'} ({week_label(next_monday)})"
            ),
            inline=False,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── /help ─────────────────────────────────────────────────────────────────

    @app_commands.command(name="help", description="Show all available bot commands.")
    async def help(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="🎲 DnD Scheduler — Commands",
            color=0x5865F2,
        )
        embed.add_field(
            name="📅 Scheduling",
            value=(
                "`/availability` — Set your available days for next (or current) week\n"
                "`/dates` — Show days where all respondents are free\n"
                "`/overview` — Full availability grid for the week\n"
                "`/status` — See who has and hasn't submitted yet\n"
                "`/remind` — Manually ping everyone who hasn't submitted yet"
            ),
            inline=False,
        )
        embed.add_field(
            name="⚙️ Admin (requires Manage Server)",
            value=(
                "`/set-reminder-channel` — Set the channel for automatic reminders\n"
                "`/pause` — Stop automatic weekend reminders\n"
                "`/resume` — Resume automatic weekend reminders\n"
                "`/lock-week` — Manually lock the current week\n"
                "`/unlock-week` — Unlock a week so availability can be edited again\n"
                "`/auto-lock-enable` — Automatically lock weeks every Monday\n"
                "`/auto-lock-disable` — Disable automatic locking\n"
                "`/admin-reset` — Clear all availability for a week"
            ),
            inline=False,
        )
        embed.add_field(
            name="📋 Info",
            value=(
                "`/settings` — View current bot settings for this server\n"
                "`/help` — Show this message"
            ),
            inline=False,
        )
        embed.add_field(
            name="🕐 Automatic behaviour",
            value=(
                "• **Sat & Sun at 10:00 UTC** — reminds anyone who hasn't submitted\n"
                "• **Monday at 00:00 UTC** — locks current week (if auto-lock on), opens next week, purges old data"
            ),
            inline=False,
        )
        embed.set_footer(text="Weeks run Monday–Sunday. Use /availability to fill in next week!")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── /pause ────────────────────────────────────────────────────────────────

    @app_commands.command(name="pause", description="Pause weekend reminders for this server.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def pause(self, interaction: discord.Interaction):
        await ensure_guild(interaction.guild_id)
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE guild_config SET paused = 1 WHERE guild_id = ?", (interaction.guild_id,)
            )
            await db.commit()
        await interaction.response.send_message("⏸️ Reminders paused.", ephemeral=True)

    # ── /resume ───────────────────────────────────────────────────────────────

    @app_commands.command(name="resume", description="Resume weekend reminders for this server.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def resume(self, interaction: discord.Interaction):
        await ensure_guild(interaction.guild_id)
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE guild_config SET paused = 0 WHERE guild_id = ?", (interaction.guild_id,)
            )
            await db.commit()
        await interaction.response.send_message("▶️ Reminders resumed.", ephemeral=True)

    # ── /set-reminder-channel ────────────────────────────────────────────────

    @app_commands.command(name="set-reminder-channel", description="Set the channel for weekend reminders.")
    @app_commands.describe(channel="The channel to send reminders in")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def set_reminder_channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        await ensure_guild(interaction.guild_id)
        await set_remind_channel(interaction.guild_id, channel.id)
        await interaction.response.send_message(
            f"✅ Reminders will be sent in {channel.mention}.", ephemeral=True
        )

    # ── /lock-week ────────────────────────────────────────────────────────────

    @app_commands.command(name="lock-week", description="[Admin] Manually lock the current week.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def lock_week_cmd(self, interaction: discord.Interaction):
        await ensure_guild(interaction.guild_id)
        monday = get_week_start(0)
        await lock_week(interaction.guild_id, monday)
        await interaction.response.send_message(
            f"🔒 Week of **{week_label(monday)}** is now locked.", ephemeral=True
        )

    # ── /admin-reset ──────────────────────────────────────────────────────────

    @app_commands.command(name="admin-reset", description="[Admin] Clear all availability for a week.")
    @app_commands.describe(week="Which week to reset")
    @app_commands.choices(week=[
        app_commands.Choice(name="Next week", value="next"),
        app_commands.Choice(name="Current week", value="current"),
    ])
    @app_commands.checks.has_permissions(manage_guild=True)
    async def admin_reset(self, interaction: discord.Interaction, week: str = "next"):
        offset = 0 if week == "current" else 1
        monday = get_week_start(offset)
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "DELETE FROM availability WHERE guild_id=? AND week_start=?",
                (interaction.guild_id, monday.isoformat()),
            )
            await db.execute(
                "DELETE FROM locked_weeks WHERE guild_id=? AND week_start=?",
                (interaction.guild_id, monday.isoformat()),
            )
            await db.commit()
        await interaction.response.send_message(
            f"🗑️ Cleared all availability for **{week_label(monday)}**.", ephemeral=True
        )

    # ── Background: Weekend reminders ────────────────────────────────────────

    @tasks.loop(hours=1)
    async def weekly_reminder(self):
        now = datetime.now(pytz.utc)
        # Fire at 10:00 UTC on Sat (5) and Sun (6)
        if now.weekday() not in (5, 6) or now.hour != 10:
            return

        next_monday = get_week_start(1)

        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT guild_id, paused, remind_channel FROM guild_config") as cur:
                guilds = await cur.fetchall()

        for guild_id, paused, channel_id in guilds:
            if paused or not channel_id:
                continue

            guild = self.bot.get_guild(guild_id)
            if not guild:
                continue

            channel = guild.get_channel(channel_id)
            if not channel:
                continue

            submitted = await get_users_submitted(guild_id, next_monday)
            pending = [m for m in guild.members if not m.bot and m.id not in submitted]

            if not pending:
                await channel.send(
                    f"✅ Everyone has submitted their availability for **{week_label(next_monday)}**! Use `/dates` to find the best day."
                )
                continue

            mentions = " ".join(m.mention for m in pending)
            await channel.send(
                f"📣 Reminder! The following adventurers haven't submitted their availability for "
                f"**{week_label(next_monday)}** yet:\n{mentions}\n\nUse `/availability` to fill it in!"
            )

    @weekly_reminder.before_loop
    async def before_reminder(self):
        await self.bot.wait_until_ready()

    # ── Background: Weekly rollover ──────────────────────────────────────────

    @tasks.loop(hours=1)
    async def weekly_rollover(self):
        now = datetime.now(pytz.utc)
        # Fire Monday at 00:00 UTC
        if now.weekday() != 0 or now.hour != 0:
            return

        current_monday = get_week_start(0)

        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT guild_id, remind_channel, auto_lock FROM guild_config") as cur:
                guilds = await cur.fetchall()

        for guild_id, channel_id, auto_lock in guilds:
            # Lock current week only if auto_lock is enabled
            if auto_lock:
                await lock_week(guild_id, current_monday)
            # Purge old data
            await purge_old_weeks(guild_id)

            if channel_id:
                guild = self.bot.get_guild(guild_id)
                if guild:
                    channel = guild.get_channel(channel_id)
                    if channel:
                        next_monday = get_week_start(1)
                        await channel.send(
                            f"🔒 The week of **{week_label(current_monday)}** is now locked.\n"
                            f"📅 Availability for **{week_label(next_monday)}** is now open! Use `/availability` to submit."
                        )

    @weekly_rollover.before_loop
    async def before_rollover(self):
        await self.bot.wait_until_ready()

    # ── Error handler ─────────────────────────────────────────────────────────

    @pause.error
    @resume.error
    @set_reminder_channel.error
    @lock_week_cmd.error
    @unlock_week_cmd.error
    @auto_lock_enable.error
    @auto_lock_disable.error
    @admin_reset.error
    async def admin_error(self, interaction: discord.Interaction, error):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message(
                "⛔ You need **Manage Server** permission to use this command.", ephemeral=True
            )

bot.run(TOKEN)