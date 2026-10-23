# BloxTrap

Discord bot that tracks Roblox players' online status and posts notifications with a direct **Join Server** button.

## Features

- Track Roblox players per Discord server
- Online/offline notifications with avatars and profile links
- **Join Server** button using RoPro invite links (falls back to the Roblox follow link if unavailable)
- Per-guild notification channel and ping role
- Slash commands
- Local JSON storage (`data.json`) — no external database required
- Web health check at `/health`

## Setup

1. Create a bot at the [Discord Developer Portal](https://discord.com/developers/applications).
2. Invite it with the `bot` + `applications.commands` scopes and these permissions:
   - View Channels
   - Send Messages
   - Read Message History
   - Mention Everyone (optional, for role pings)
3. Install dependencies:

   ```bash
   pip install -r requirements.txt
   ```

4. Copy the env template and fill it in:

   ```bash
   cp .env.example .env
   # edit .env and set TOKEN
   ```

5. Run:

   ```bash
   python bot.py
   ```

## Commands

| Command | Description |
|---|---|
| `/add-player <roblox_id>` | Start tracking a Roblox player |
| `/list-tracked` | List tracked players and remove them |
| `/set-channel <channel>` | Set the notification channel |
| `/set-role <role>` | Set the role to ping when a player comes online |

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `TOKEN` or `DISCORD_BOT_TOKEN` | Yes | Discord bot token |
| `DATA_FILE` | No | JSON database path (default: `data.json`) |
| `PORT` | No | Health-check server port (default: `8080`) |

## Components V2

`discord.py>=2.6.0` supports Discord **Components V2**. Send a `LayoutView` instead of an embed:

```python
import discord

view = discord.ui.LayoutView()
container = discord.ui.Container(accent_colour=discord.Colour.blurple())
container.add_item(discord.ui.TextDisplay("## Player Online"))
container.add_item(discord.ui.Separator())
container.add_item(discord.ui.Section(
    "**dimepro1236** is now online",
    accessory=discord.ui.Thumbnail("https://example.com/avatar.png"),
))
view.add_item(container)

await channel.send(view=view)
```

Notes:
- `content`, `embeds`, and stickers **cannot** be combined with Components V2 in the same message.
- `discord.py` sets the `IS_COMPONENTS_V2` flag automatically when the view contains V2 components.

## Notes

- `data.json` is created automatically on first run and is gitignored.
- `.env` is gitignored — never commit your bot token.
