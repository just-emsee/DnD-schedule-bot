# 🎲 DnD Scheduler Bot

A Discord bot for scheduling D&D sessions across multiple servers, with weekly availability tracking, automatic reminders, and rollover.

---

## Features

- **Unlimited players** — any server member can submit availability
- **Multi-server** — each server's data is fully isolated
- **Weekly cycle** — the upcoming week is open for submissions; current week locks on Monday 00:00 UTC
- **Weekend reminders** — Saturday and Sunday at 10:00 UTC, pings anyone who hasn't submitted
- **Auto-rollover** — on Monday, the current week locks, old data is purged, and the next week opens
- **Admin controls** — pause/resume reminders, set channels, manually lock or reset weeks

---

## Setup

### 1. Create a Discord Application & Bot

1. Go to https://discord.com/developers/applications
2. Click **New Application**, give it a name
3. Go to **Bot** → **Add Bot**
4. Under **Privileged Gateway Intents**, enable **Server Members Intent**
5. Copy the **Token**

### 2. Invite the Bot to Your Server

In **OAuth2 → URL Generator**:
- Scopes: `bot`, `applications.commands`
- Bot Permissions: `Send Messages`, `Read Message History`, `Mention Everyone` (for reminders)

Copy the generated URL and open it to invite.

### 3. Install & Run

```bash
# Clone or copy the bot files
cd dnd-bot

# Install dependencies
pip install -r requirements.txt

# Create your .env file
cp .env.example .env
# Edit .env and paste your token

# Run the bot
python bot.py
```

---

## How the Weekly Cycle Works

```
Mon  Tue  Wed  Thu  Fri  Sat  Sun  |  Mon
                                   |
 ← current week (locked Mon 00UTC) |  ← next week opens
                          ↑ ↑      |
                    reminders sent |  rollover happens
                     (10:00 UTC)   |
```

1. **Monday 00:00 UTC** — current week locks, old data purged, next week's window opens
2. **Saturday & Sunday 10:00 UTC** — anyone who hasn't submitted for next week gets pinged
3. Players can edit their submission any time before the week locks

---

## Availability Input Format

When you use `/availability`, a form pops up. Type your available days in any of these formats:

- `Mon, Wed, Fri`
- `Monday Wednesday Friday`
- `all` — available every day
- `none` — not available at all

---

## Multi-Server Notes

Each server stores its own:
- Availability data
- Reminder channel setting
- Pause/resume state
- Locked weeks

No data is shared between servers.

---

## File Structure

```
dnd-bot/
├── bot.py              # Main bot code
├── requirements.txt    # Python dependencies
├── .env.example        # Token template
├── .env                # Your actual token (do not commit!)
└── dnd_scheduler.db    # SQLite database (auto-created)
```

---

## Keeping the Bot Running

To run 24/7, use one of:
- **Screen/tmux** on a VPS: `screen -S dndbot python bot.py`
- **systemd** service on Linux
- **PM2**: `pm2 start bot.py --interpreter python3`
- **Railway / Fly.io / Render** — free-tier cloud hosting
