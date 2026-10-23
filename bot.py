import discord
from discord import app_commands
from discord.ext import tasks
import os
import asyncio
import json
from datetime import datetime
from roblox_api import RobloxAPI
from aiohttp import web
from dotenv import load_dotenv

load_dotenv()

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True

client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)
roblox_api = RobloxAPI()

# ---------------------------------------------------------------------------
# Simple JSON file database (replaces MongoDB / motor)
# Stores everything in data.json. Safe for single-process use.
# ---------------------------------------------------------------------------


class JSONDatabase:
    def __init__(self, path: str = 'data.json'):
        self.path = path
        self._lock = asyncio.Lock()
        self._data: dict = {"guild_settings": {}, "tracked_players": {}}
        self._loaded = False

    async def _load(self):
        if self._loaded:
            return
        async with self._lock:
            if self._loaded:
                return
            if os.path.exists(self.path):
                try:
                    with open(self.path, 'r', encoding='utf-8') as f:
                        loaded = json.load(f)
                    if isinstance(loaded, dict):
                        self._data = loaded
                    self._data.setdefault("guild_settings", {})
                    self._data.setdefault("tracked_players", {})
                except Exception as e:
                    print(f"Failed to load {self.path}: {e}", flush=True)
                    self._data = {"guild_settings": {}, "tracked_players": {}}
            else:
                self._data = {"guild_settings": {}, "tracked_players": {}}
            self._loaded = True

    async def _save(self):
        tmp = self.path + ".tmp"
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(self._data, f, indent=2)
        os.replace(tmp, self.path)

    def _matches(self, row: dict, filters: dict) -> bool:
        for k, v in filters.items():
            if str(row.get(k)) != str(v):
                return False
        return True

    async def select(self, table: str, filters=None, limit=None):
        await self._load()
        async with self._lock:
            rows = list(self._data.get(table, {}).values())
            if filters:
                rows = [r for r in rows if self._matches(r, filters)]
            if limit is not None:
                rows = rows[:limit]
            return rows

    async def select_one(self, table: str, filters=None):
        rows = await self.select(table, filters=filters, limit=1)
        return rows[0] if rows else None

    async def upsert(self, table: str, data: dict, on_conflict=None):
        await self._load()
        async with self._lock:
            table_data = self._data.setdefault(table, {})
            key = None
            if on_conflict:
                conflict_cols = [c.strip() for c in on_conflict.split(',')]
                for existing_id, existing in table_data.items():
                    if all(str(existing.get(c)) == str(data.get(c)) for c in conflict_cols):
                        key = existing_id
                        break
            if key is None:
                key = str(len(table_data) + 1)
                while key in table_data:
                    key = str(int(key) + 1)
            merged = {**table_data.get(key, {}), **data}
            table_data[key] = merged
            await self._save()
            return merged

    async def update(self, table: str, filters: dict, data: dict):
        await self._load()
        async with self._lock:
            table_data = self._data.setdefault(table, {})
            for existing_id, existing in table_data.items():
                if self._matches(existing, filters):
                    table_data[existing_id] = {**existing, **data}
                    await self._save()
                    return table_data[existing_id]
            return None

    async def delete(self, table: str, filters: dict):
        await self._load()
        async with self._lock:
            table_data = self._data.setdefault(table, {})
            to_delete = [eid for eid, existing in table_data.items()
                         if self._matches(existing, filters)]
            for eid in to_delete:
                del table_data[eid]
            if to_delete:
                await self._save()
            return {"deleted": len(to_delete)}


db = JSONDatabase(os.getenv('DATA_FILE', 'data.json'))

# Logical collections in the JSON file (replace MongoDB collections)
GUILD_SETTINGS = 'guild_settings'
TRACKED_PLAYERS = 'tracked_players'

OWNER_USER_ID = 1117540437016727612


@tree.command(name="add-player", description="Add a Roblox player to track by their user ID")
@app_commands.describe(roblox_id="The Roblox user ID (Profile ID) to track")
async def add_player(interaction: discord.Interaction, roblox_id: str):
    try:
        user_id = int(roblox_id)
    except ValueError:
        embed = discord.Embed(
            description="Invalid Roblox ID. Please provide a valid Profile ID (numbers only).",
            color=0xFFFFFF
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    user_info = await roblox_api.get_user_info(user_id)

    if not user_info:
        embed = discord.Embed(
            description=f"Could not find Roblox user with ID: {user_id}",
            color=0xFFFFFF
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    guild_id = str(interaction.guild_id)

    await db.upsert(
        TRACKED_PLAYERS,
        {
            "guild_id": guild_id,
            "roblox_id": str(user_id),
            "username": user_info['name'],
            "display_name": user_info['displayName'],
            "added_at": datetime.utcnow().isoformat(),
            "last_status": "offline"
        },
        on_conflict="guild_id,roblox_id"
    )

    embed = discord.Embed(
        description=f"Now tracking **{user_info['displayName']}** (@{user_info['name']})\n Profile ID: `{user_id}`",
        color=0xFFFFFF
    )

    await interaction.response.send_message(embed=embed)

    await refresh_live_tracklist(guild_id)


@tree.command(name="list-tracked", description="Shows all tracked players with a dropdown menu")
async def list_tracked(interaction: discord.Interaction):
    guild_id = str(interaction.guild_id)

    players = await db.select(TRACKED_PLAYERS, filters={"guild_id": guild_id}, limit=100)

    if not players:
        embed = discord.Embed(
            description="No players are currently being tracked.\nUse `/add-player <roblox_id>` to start tracking players.",
            color=0xFFFFFF
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    class PlayerSelect(discord.ui.Select):
        def __init__(self, players, guild_id):
            self.guild_id = guild_id
            options = [
                discord.SelectOption(
                    label=f"{p['display_name']} (@{p['username']})",
                    description=f"ID: {p['roblox_id']} - Click to remove",
                    value=p['roblox_id']
                )
                for p in players
            ]
            super().__init__(placeholder="Select a player to remove from tracking", options=options, min_values=1, max_values=1)

        async def callback(self, interaction: discord.Interaction):
            selected_id = self.values[0]

            player_data = await db.select_one(
                TRACKED_PLAYERS,
                {"guild_id": self.guild_id, "roblox_id": selected_id}
            )

            if player_data:
                # Delete old message if exists
                if player_data.get('message_id'):
                    settings = await db.select_one(GUILD_SETTINGS, {"guild_id": self.guild_id})
                    if settings and settings.get('notification_channel_id'):
                        try:
                            channel = await client.fetch_channel(settings['notification_channel_id'])
                            msg = await channel.fetch_message(player_data['message_id'])
                            await msg.delete()
                        except:
                            pass

                # Remove from tracking
                await db.delete(
                    TRACKED_PLAYERS,
                    {"guild_id": self.guild_id, "roblox_id": selected_id}
                )

                embed = discord.Embed(
                    description=f"Removed **{player_data['display_name']}** (@{player_data['username']}) from tracking.",
                    color=0xFFFFFF
                )
                await interaction.response.send_message(embed=embed, ephemeral=True)

                await refresh_live_tracklist(self.guild_id)
            else:
                embed = discord.Embed(
                    description="Player not found in tracking list.",
                    color=0xFFFFFF
                )
                await interaction.response.send_message(embed=embed, ephemeral=True)

    class PlayerView(discord.ui.View):
        def __init__(self, players, guild_id):
            super().__init__()
            self.add_item(PlayerSelect(players, guild_id))

    player_list = "\n".join([
        f"• **{p['display_name']}** (@{p['username']}) - ID: `{p['roblox_id']}`"
        for p in players
    ])

    embed = discord.Embed(
        title="Tracked Players",
        description=f"{player_list}\n\n**Select a player below to remove them from tracking:**",
        color=0xFFFFFF
    )

    await interaction.response.send_message(embed=embed, view=PlayerView(players, guild_id), ephemeral=True)


@tree.command(name="set-channel", description="Sets where notifications are sent")
@app_commands.describe(channel="The channel to send notifications to")
async def set_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    guild_id = str(interaction.guild_id)

    await db.upsert(
        GUILD_SETTINGS,
        {"guild_id": guild_id, "notification_channel_id": channel.id},
        on_conflict="guild_id"
    )

    embed = discord.Embed(
        description=f"Notifications will now be sent to {channel.mention}",
        color=0xFFFFFF
    )

    await interaction.response.send_message(embed=embed)


@tree.command(name="set-role", description="Sets which role gets pinged when a player is online")
@app_commands.describe(role="The role to ping for notifications")
async def set_role(interaction: discord.Interaction, role: discord.Role):
    guild_id = str(interaction.guild_id)

    await db.upsert(
        GUILD_SETTINGS,
        {"guild_id": guild_id, "ping_role_id": role.id},
        on_conflict="guild_id"
    )

    embed = discord.Embed(
        description=f"Will now ping {role.mention} when a tracked player is online",
        color=0xFFFFFF
    )

    await interaction.response.send_message(embed=embed)


@tree.command(name="live-tracklist", description="Posts a live leaderboard of all tracked players")
@app_commands.describe(channel="The channel where the live tracklist should be posted")
async def live_tracklist(interaction: discord.Interaction, channel: discord.TextChannel):
    if interaction.guild is None:
        await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    guild_id = str(interaction.guild_id)

    if not channel.permissions_for(interaction.guild.me).send_messages:
        await interaction.followup.send(
            "I don't have permission to send messages in that channel.",
            ephemeral=True
        )
        return

    settings = await db.select_one(GUILD_SETTINGS, {"guild_id": guild_id})
    if settings and settings.get('tracklist_channel_id') and settings.get('tracklist_message_id'):
        try:
            old_channel = await client.fetch_channel(settings['tracklist_channel_id'])
            old_message = await old_channel.fetch_message(settings['tracklist_message_id'])
            await old_message.delete()
        except Exception:
            pass

    view = await build_tracklist_view(guild_id)

    try:
        message = await channel.send(view=view)
    except Exception as e:
        await interaction.followup.send(f"Failed to post the tracklist: {e}", ephemeral=True)
        return

    await db.upsert(
        GUILD_SETTINGS,
        {
            "guild_id": guild_id,
            "tracklist_channel_id": channel.id,
            "tracklist_message_id": message.id,
        },
        on_conflict="guild_id"
    )

    embed = discord.Embed(
        description=f"Live tracklist created in {channel.mention}",
        color=0xFFFFFF
    )
    await interaction.followup.send(embed=embed, ephemeral=True)


def build_player_view(*, username: str, display_name: str, avatar_url: str,
                      online: bool, role_mention: str = "",
                      join_url: str = None) -> discord.ui.LayoutView:
    """Build a Components V2 player card (no colour bar)."""
    view = discord.ui.LayoutView(timeout=None)
    container = discord.ui.Container()  # no accent_colour -> no colour bar

    header = f"## {username} | {display_name}"
    if online and role_mention:
        header += f"\n-# {role_mention}"
    container.add_item(discord.ui.TextDisplay(header))
    container.add_item(discord.ui.Separator())

    container.add_item(discord.ui.TextDisplay(
        "A player has been caught\n"
        "Server link and user live user status are found below!"
    ))

    state = "Online" if online else "Offline"
    container.add_item(discord.ui.TextDisplay(f"> Current status — {state}"))

    if avatar_url:
        container.add_item(discord.ui.MediaGallery(
            discord.MediaGalleryItem(avatar_url)
        ))

    if join_url:
        label = "Join Server | Ropro" if online else "Join Server | Ropro / User Offline ❕"
        container.add_item(discord.ui.ActionRow(
            discord.ui.Button(
                label=label,
                style=discord.ButtonStyle.link,
                url=join_url,
            )
        ))

    view.add_item(container)
    return view


async def build_tracklist_view(guild_id: str) -> discord.ui.LayoutView:
    """Build the live tracklist board (no colour bar, avatars as thumbnails)."""
    players = await db.select(TRACKED_PLAYERS, filters={"guild_id": guild_id}, limit=1000)
    players.sort(key=lambda p: p.get('added_at') or '')

    # Components V2 allows 40 components per message; keep the board inside that limit.
    max_entries = 9
    shown_players = players[:max_entries]

    async def avatar_for(player_data: dict):
        try:
            return await roblox_api.get_user_avatar_url(int(player_data['roblox_id']))
        except Exception:
            return None

    avatars = await asyncio.gather(*(avatar_for(p) for p in shown_players))

    view = discord.ui.LayoutView(timeout=None)
    container = discord.ui.Container()  # no accent_colour -> no colour bar

    container.add_item(discord.ui.TextDisplay("## Live Tracklist"))

    if players:
        count = len(players)
        line = f"-# Tracking {count} player{'s' if count != 1 else ''}"
        if count > max_entries:
            line += f" — showing first {max_entries}"
        container.add_item(discord.ui.TextDisplay(line))
    else:
        container.add_item(discord.ui.TextDisplay("-# No players are currently tracked."))

    for index, (player, avatar_url) in enumerate(zip(shown_players, avatars), start=1):
        container.add_item(discord.ui.Separator())

        entry = discord.ui.TextDisplay(
            f"**{index}. {player.get('username', 'Unknown')}**\n"
            f"ID: `{player.get('roblox_id', 'Unknown')}`"
        )

        if avatar_url:
            container.add_item(discord.ui.Section(
                entry,
                accessory=discord.ui.Thumbnail(avatar_url),
            ))
        else:
            container.add_item(entry)

    view.add_item(container)
    return view


async def refresh_live_tracklist(guild_id: str):
    """Update the guild's live tracklist message after a player is added or removed."""
    settings = await db.select_one(GUILD_SETTINGS, {"guild_id": guild_id})
    if not settings or not settings.get('tracklist_channel_id') or not settings.get('tracklist_message_id'):
        return

    try:
        channel = await client.fetch_channel(settings['tracklist_channel_id'])
        view = await build_tracklist_view(guild_id)

        try:
            message = await channel.fetch_message(settings['tracklist_message_id'])
        except discord.NotFound:
            message = await channel.send(view=view)
            await db.update(
                GUILD_SETTINGS,
                {"guild_id": guild_id},
                {"tracklist_message_id": message.id}
            )
            return

        await message.edit(view=view)
    except Exception as e:
        print(f"Failed to refresh live tracklist for guild {guild_id}: {e}", flush=True)


async def send_online_notification(guild_id: str, user_id: str, player_data: dict, status_info: dict):
    user_info = status_info.get('user_info', {})
    display_name = user_info.get('displayName', player_data.get('display_name', 'Unknown'))
    username = user_info.get('name', player_data.get('username', 'Unknown'))

    avatar_url = await roblox_api.get_user_avatar_url(int(user_id))

    settings = await db.select_one(GUILD_SETTINGS, {"guild_id": guild_id})

    if not settings or not settings.get('notification_channel_id'):
        return

    try:
        channel = await client.fetch_channel(settings['notification_channel_id'])
    except Exception as e:
        print(f"Failed to fetch channel: {e}", flush=True)
        return

    role_mention = ""
    if settings.get('ping_role_id'):
        role_mention = f"<@&{settings['ping_role_id']}>"

    presence = status_info.get('presence', {})
    place_id = presence.get('placeId')
    universe_id = presence.get('universeId')
    job_id = presence.get('gameId')

    ropro_url = await roblox_api.get_ropro_invite(universe_id, job_id)
    if ropro_url:
        print(f"RoPro invite for {user_id}: {ropro_url}", flush=True)

    join_url = ropro_url
    if not join_url and place_id:
        join_url = f"https://www.roblox.com/games/start?placeId={place_id}&launchData=user:{user_id}"

    view = build_player_view(
        username=username,
        display_name=display_name,
        avatar_url=avatar_url,
        online=True,
        role_mention=role_mention,
        join_url=join_url,
    )

    msg = await channel.send(
        view=view,
        allowed_mentions=discord.AllowedMentions(roles=True, users=False, everyone=False),
    )

    await db.update(
        TRACKED_PLAYERS,
        {"guild_id": guild_id, "roblox_id": user_id},
        {"message_id": msg.id, "join_url": join_url}
    )


async def update_offline_notification(guild_id: str, user_id: str, player_data: dict):
    if not player_data.get('message_id'):
        return

    settings = await db.select_one(GUILD_SETTINGS, {"guild_id": guild_id})
    if not settings or not settings.get('notification_channel_id'):
        return

    try:
        channel = await client.fetch_channel(settings['notification_channel_id'])
        msg = await channel.fetch_message(player_data['message_id'])

        display_name = player_data.get('display_name', 'Unknown')
        username = player_data.get('username', 'Unknown')
        avatar_url = await roblox_api.get_user_avatar_url(int(user_id))

        view = build_player_view(
            username=username,
            display_name=display_name,
            avatar_url=avatar_url,
            online=False,
            join_url=player_data.get('join_url'),
        )

        try:
            await msg.edit(view=view)
        except discord.HTTPException as e:
            # Messages created before Components V2 cannot be converted.
            # Replace the old message with a fresh V2 card.
            print(f"Could not edit old message ({e}); replacing it.", flush=True)
            try:
                await msg.delete()
            except Exception:
                pass
            await channel.send(view=view)

        # Clear message_id after marking offline to avoid re-editing
        await db.update(
            TRACKED_PLAYERS,
            {"guild_id": guild_id, "roblox_id": user_id},
            {"message_id": None}
        )
    except Exception as e:
        print(f"Failed to update offline message for {user_id}: {e}", flush=True)


@tasks.loop(seconds=30)
async def check_players():
    try:
        all_players = await db.select(TRACKED_PLAYERS, limit=1000)

        for player_data in all_players:
            guild_id = player_data['guild_id']
            user_id = player_data['roblox_id']

            try:
                status_info = await roblox_api.get_player_status(int(user_id))
                current_status = 'online' if status_info.get('online', False) else 'offline'
                last_status = player_data.get('last_status')

                # Offline -> Online
                if current_status == 'online' and last_status != 'online':
                    await send_online_notification(guild_id, user_id, player_data, status_info)

                # Online -> Offline
                elif current_status == 'offline' and last_status == 'online':
                    await update_offline_notification(guild_id, user_id, player_data)

                await db.update(
                    TRACKED_PLAYERS,
                    {"guild_id": guild_id, "roblox_id": user_id},
                    {"last_status": current_status}
                )

            except Exception as e:
                print(f"Error checking player {user_id}: {e}", flush=True)

            await asyncio.sleep(0.5)

    except Exception as e:
        print(f"Error in check_players loop: {e}", flush=True)


@check_players.before_loop
async def before_check_players():
    await client.wait_until_ready()


@client.event
async def on_ready():
    await tree.sync()
    print(f'Logged in as {client.user}', flush=True)

    if not check_players.is_running():
        check_players.start()


async def health_check(request):
    return web.Response(text="Bot is running!")


async def start_web_server():
    app = web.Application()
    app.router.add_get('/', health_check)
    app.router.add_get('/health', health_check)

    port = int(os.getenv('PORT', 8080))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    print(f'Health check server running on port {port}', flush=True)


async def main():
    token = os.getenv('DISCORD_BOT_TOKEN') or os.getenv('TOKEN')

    if not token:
        print("ERROR: DISCORD_BOT_TOKEN (or TOKEN) environment variable not set!")
        return

    await start_web_server()
    await client.start(token)


if __name__ == "__main__":
    asyncio.run(main())
