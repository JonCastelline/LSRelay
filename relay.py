import discord
from discord.ext import commands
import json
import os
import sys
import re
import time
import asyncio
from collections import deque

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, 'config.json')

# Helper: Strip comments and trailing commas from JSON/JSONC text
def strip_json_comments(text):
    """Strips JavaScript/JSONC style single-line (//) and multi-line (/* */)
    comments and trailing commas so standard json.loads can parse it."""
    out = []
    in_string = False
    escape = False
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if escape:
            out.append(c)
            escape = False
            i += 1
            continue
        if c == '\\' and in_string:
            out.append(c)
            escape = True
            i += 1
            continue
        if c == '"':
            in_string = not in_string
            out.append(c)
            i += 1
            continue
        if not in_string:
            if c == '/' and i + 1 < n and text[i+1] == '/':
                while i < n and text[i] != '\n':
                    i += 1
                continue
            if c == '/' and i + 1 < n and text[i+1] == '*':
                i += 2
                while i + 1 < n and not (text[i] == '*' and text[i+1] == '/'):
                    i += 1
                i += 2
                continue
        out.append(c)
        i += 1
    cleaned = ''.join(out)
    return re.sub(r',\s*([\]}])', r'\1', cleaned)

def load_config(config_path=CONFIG_PATH):
    if not os.path.exists(config_path):
        raise FileNotFoundError(f"Configuration file not found: {config_path}")
    with open(config_path, 'r', encoding='utf-8') as f:
        raw_text = f.read()
    cleaned = strip_json_comments(raw_text)
    return json.loads(cleaned)

def get_channels_from_config(cfg):
    """Returns a dict of {str(name): int(channel_id)} from configuration.
    Supports both 'channels' dict/list and legacy 'channel_id'."""
    raw_channels = cfg.get('channels')
    result = {}
    if isinstance(raw_channels, dict):
        for name, cid in raw_channels.items():
            if str(name).startswith('_'):
                continue
            try:
                result[str(name)] = int(cid)
            except (ValueError, TypeError):
                continue
    elif isinstance(raw_channels, list):
        for item in raw_channels:
            if isinstance(item, dict) and 'id' in item:
                name = str(item.get('name', item['id']))
                if name.startswith('_'):
                    continue
                try:
                    result[name] = int(item['id'])
                except (ValueError, TypeError):
                    continue
    if not result and 'channel_id' in cfg:
        try:
            result['Default'] = int(cfg['channel_id'])
        except (ValueError, TypeError):
            pass
    return result

def select_channel(channels, cli_arg=None, input_func=input):
    """Selects a channel from available channels dict.
    Returns (channel_name, channel_id)."""
    if not channels:
        raise ValueError("No valid channels configured in config.json")
    
    channel_list = list(channels.items())

    # Check CLI argument first
    if cli_arg is not None:
        cli_clean = str(cli_arg).strip().lower()
        if cli_clean.isdigit():
            idx = int(cli_clean)
            if 1 <= idx <= len(channel_list):
                return channel_list[idx - 1]
        for name, cid in channel_list:
            if name.lower() == cli_clean:
                return name, cid
        print(f"Warning: Channel '{cli_arg}' not found in configuration.")

    # If only one channel is configured, auto-select it
    if len(channel_list) == 1:
        return channel_list[0]

    # Multiple channels available, prompt user in terminal
    print("\n" + "=" * 40)
    print(" LSRelay Discord Bridge")
    print("=" * 40)
    print("Available channels:")
    for i, (name, cid) in enumerate(channel_list, 1):
        print(f"  [{i}] {name} ({cid})")
    print("=" * 40)

    while True:
        try:
            choice = input_func(f"Select channel [1-{len(channel_list)}] (default 1): ").strip()
        except EOFError:
            print("\nNon-interactive input closed (EOF). Cannot select channel.", file=sys.stderr)
            sys.exit(1)
        except KeyboardInterrupt:
            print("\nStartup cancelled.")
            sys.exit(0)

        if not choice:
            return channel_list[0]
        if choice.isdigit():
            idx = int(choice)
            if 1 <= idx <= len(channel_list):
                return channel_list[idx - 1]
        for name, cid in channel_list:
            if name.lower() == choice.lower():
                return name, cid
        print(f"Invalid selection: '{choice}'. Enter a number 1-{len(channel_list)} or channel name.")

def sanitize_for_game(text):
    """Sanitizes text for FFXI chat:
    - Normalizes newlines and whitespace
    - Strips all non-printable ASCII and emojis (BMP and astral plane)
    """
    if not text:
        return ""
    # Replace newlines/tabs with spaces
    text = re.sub(r'[\r\n\t]+', ' ', text)
    # Retain standard printable ASCII (32-126) for safe in-game rendering
    text = ''.join(c for c in text if 32 <= ord(c) <= 126)
    # Collapse multiple consecutive spaces
    text = re.sub(r'\s{2,}', ' ', text).strip()
    return text

def split_message(text, max_length=120):
    """Splits a message into chunks not exceeding max_length characters,
    preferring to break at whitespace boundaries."""
    if not text:
        return []
    if len(text) <= max_length:
        return [text]

    chunks = []
    remaining = text.strip()
    while len(remaining) > max_length:
        # Find the last space within max_length
        split_idx = remaining.rfind(' ', 0, max_length)
        if split_idx <= 0:
            # No space found, hard cut
            split_idx = max_length
        chunks.append(remaining[:split_idx].strip())
        remaining = remaining[split_idx:].strip()
    if remaining:
        chunks.append(remaining)
    return chunks

def format_game_message(author, message, template="[{author}] {message}"):
    """Formats the text to be spoken in game."""
    return template.format(author=author, message=message)

def is_mode_allowed(target_mode, configured_mode):
    """Returns True if target_mode is permitted under configured_mode."""
    conf = (configured_mode or 'ls1').lower()
    tgt = (target_mode or 'ls1').lower()
    if conf == 'both':
        return tgt in ('ls1', 'ls2')
    return tgt == conf

class RelayCore:
    def __init__(self, config):
        self.config = config
        self.linkshell_mode = config.get('linkshell_mode', 'ls1').lower()
        self.game_chat_format = config.get('game_chat_format', '[{author}] {message}')
        self.tcp_host = config.get('tcp_host', '127.0.0.1')
        self.tcp_port = int(config.get('tcp_port', 45124))
        
        # Connected game client writers
        self.active_clients = set()
        # Deduplication cache: deque of (mode, message_text, timestamp)
        self.recent_discord_messages = deque()
        self.dedup_window = 10.0 # seconds
        self.message_count_relayed = 0
        self.bot = None
        self.active_channel = None
        self.game_player_name = None

    def record_discord_injection(self, mode, text):
        now = time.time()
        self._prune_dedup_cache(now)
        self.recent_discord_messages.append((mode.lower(), text.strip(), now))

    def consume_discord_echo(self, mode, sender, text):
        """Returns True and consumes the cached echo if this message matches a recent
        Discord injection originating from our character."""
        now = time.time()
        self._prune_dedup_cache(now)
        
        # Echo suppression only applies to our own in-game character
        if self.game_player_name and sender.lower() != self.game_player_name.lower():
            return False

        clean = text.strip()
        mode_clean = mode.lower()
        for idx, (m, t, _) in enumerate(self.recent_discord_messages):
            if m == mode_clean and t == clean:
                # Consume the entry so it cannot suppress future legitimate messages
                del self.recent_discord_messages[idx]
                return True
        return False

    def _prune_dedup_cache(self, now):
        while self.recent_discord_messages and (now - self.recent_discord_messages[0][2] > self.dedup_window):
            self.recent_discord_messages.popleft()

    async def broadcast_to_game(self, payload):
        """Sends a JSON line to all connected game clients."""
        if not self.active_clients:
            return False
        data = (json.dumps(payload) + '\n').encode('utf-8')
        dead_clients = set()
        for writer in self.active_clients:
            try:
                writer.write(data)
                await writer.drain()
            except Exception as e:
                print(f"[TCP] Error writing to client: {e}")
                dead_clients.add(writer)
                try:
                    writer.close()
                except Exception:
                    pass
        self.active_clients.difference_update(dead_clients)
        return True

    async def handle_tcp_client(self, reader, writer):
        addr = writer.get_extra_info('peername')
        print(f"[TCP] Windower client connected from {addr}")
        self.active_clients.add(writer)

        # Send initial config to addon
        init_payload = {
            "type": "config",
            "linkshell_mode": self.linkshell_mode
        }
        try:
            writer.write((json.dumps(init_payload) + '\n').encode('utf-8'))
            await writer.drain()
        except Exception as e:
            print(f"[TCP] Error sending initial config: {e}")

        buffer = ""
        try:
            while True:
                data = await reader.read(4096)
                if not data:
                    break
                buffer += data.decode('utf-8', errors='replace')
                while '\n' in buffer:
                    line, buffer = buffer.split('\n', 1)
                    line = line.strip()
                    if line:
                        try:
                            await self.process_game_message(line)
                        except Exception as msg_err:
                            print(f"[TCP] Error processing game message: {msg_err}")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"[TCP] Client connection error: {e}")
        finally:
            print(f"[TCP] Windower client disconnected from {addr}")
            self.active_clients.discard(writer)
            try:
                writer.close()
            except Exception:
                pass

    async def process_game_message(self, line):
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            print(f"[Game->Relay] Invalid JSON: {line}")
            return

        msg_type = payload.get('type')
        if msg_type == 'hello':
            self.game_player_name = payload.get('player')
            print(f"[Game->Relay] Connected player name: {self.game_player_name}")
            return

        if msg_type == 'chat':
            mode = payload.get('mode', 'ls1').lower()
            sender = payload.get('sender', 'Unknown')
            message = payload.get('message', '').strip()

            if not is_mode_allowed(mode, self.linkshell_mode):
                return

            # Check if this was a Discord-injected message echoing back
            if self.consume_discord_echo(mode, sender, message):
                # Suppress echo
                return

            self.message_count_relayed += 1
            if self.active_channel:
                if self.linkshell_mode == 'both':
                    formatted = f"**[{mode.upper()}] {sender}:** {message}"
                else:
                    formatted = f"**[LS] {sender}:** {message}"
                try:
                    # Prevent mass-mentions like @everyone or @here
                    await self.active_channel.send(
                        formatted,
                        allowed_mentions=discord.AllowedMentions.none()
                    )
                except Exception as e:
                    print(f"[Relay->Discord] Failed to send message: {e}")
            else:
                print(f"[Relay] Warning: No active Discord channel bound. Message not sent: {message}")

        elif msg_type == 'status':
            print(f"[Game->Relay] Status update: {payload}")

    async def process_discord_message(self, message):
        """Processes an incoming Discord message, parses commands, and sends to game."""
        # Use clean_content to resolve mentions (@User, #channel) to clean text
        content = message.clean_content.strip()

        # Parse command prefixes explicitly
        target_mode = None
        raw_text = None
        if content.startswith(('!l ', '!ls ')):
            target_mode = 'ls1'
            raw_text = content.split(' ', 1)[1].strip()
        elif content.startswith(('!l2 ', '!ls2 ')):
            target_mode = 'ls2'
            raw_text = content.split(' ', 1)[1].strip()
        elif content in ('!l', '!ls', '!l2', '!ls2'):
            await message.reply(f"Usage: `{content} <message>`")
            return
        else:
            return

        if not raw_text:
            return

        # Check if target mode is permitted
        if not is_mode_allowed(target_mode, self.linkshell_mode):
            await message.reply(
                f"⚠️ Linkshell `{target_mode.upper()}` is not active. Current mode is `{self.linkshell_mode}`."
            )
            return

        # Check game client connectivity
        if not self.active_clients:
            await message.reply("⚠️ Game client is not currently connected to LSRelay.")
            return

        # Format and sanitize
        formatted = format_game_message(message.author.display_name, raw_text, self.game_chat_format)
        sanitized = sanitize_for_game(formatted)
        if not sanitized:
            await message.reply("⚠️ Message contained only unsupported characters or was empty.")
            return

        # Split into max 120 character chunks for FFXI chat bar
        chunks = split_message(sanitized, max_length=120)

        for chunk in chunks:
            self.record_discord_injection(target_mode, chunk)
            await self.broadcast_to_game({
                "type": "inject_chat",
                "mode": target_mode,
                "message": chunk
            })

        try:
            await message.add_reaction("✅")
        except Exception:
            pass

def create_bot(core):
    intents = discord.Intents.default()
    intents.message_content = True
    bot = commands.Bot(
        command_prefix='!',
        intents=intents,
        allowed_mentions=discord.AllowedMentions.none()
    )
    core.bot = bot

    @bot.event
    async def on_ready():
        print(f"\n[Discord] Logged in as {bot.user.name} ({bot.user.id})")
        if core.active_channel:
            print(f"[Discord] Bound to active channel: {core.active_channel.name} ({core.active_channel.id})")
        print(f"[Relay] Active linkshell mode: {core.linkshell_mode}")

    @bot.event
    async def on_message(message):
        if message.author.bot:
            return
        if not core.active_channel or message.channel.id != core.active_channel.id:
            return

        content = message.clean_content.strip()

        # Check for relay bot commands like !lsr status
        if content.startswith(('!lsr', '!relay')):
            await bot.process_commands(message)
            return

        # Check strictly for !l, !ls, !l2, !ls2 command prefixes
        if content.startswith(('!l ', '!ls ', '!l2 ', '!ls2 ')) or content in ('!l', '!ls', '!l2', '!ls2'):
            await core.process_discord_message(message)
            return

        await bot.process_commands(message)

    @bot.command(name='lsr')
    async def lsr_cmd(ctx, action: str = None, *, arg: str = None):
        """Relay status and control command."""
        action = (action or 'status').lower()
        if action == 'status':
            client_status = f"Connected ({len(core.active_clients)} active client)" if core.active_clients else "Disconnected"
            embed = discord.Embed(title="LSRelay Status", color=0x2ecc71 if core.active_clients else 0xe74c3c)
            embed.add_field(name="Game Client", value=client_status, inline=True)
            embed.add_field(name="Character", value=core.game_player_name or "Unknown", inline=True)
            embed.add_field(name="Linkshell Mode", value=core.linkshell_mode.upper(), inline=True)
            embed.add_field(name="Channel", value=f"<#{core.active_channel.id}>" if core.active_channel else "None", inline=True)
            embed.add_field(name="Relayed Messages", value=str(core.message_count_relayed), inline=True)
            embed.add_field(name="Chat Format", value=f"`{core.game_chat_format}`", inline=False)
            embed.set_footer(text="Use !l <message> for LS1, !l2 <message> for LS2")
            await ctx.send(embed=embed)
        elif action == 'mode':
            if not arg or arg.lower() not in ('ls1', 'ls2', 'both'):
                await ctx.send("Usage: `!lsr mode <ls1|ls2|both>`")
                return
            core.linkshell_mode = arg.lower()
            await core.broadcast_to_game({
                "type": "config",
                "linkshell_mode": core.linkshell_mode
            })
            await ctx.send(f"✅ Linkshell mode updated to `{core.linkshell_mode.upper()}`.")
        else:
            await ctx.send("Unknown action. Available: `!lsr status`, `!lsr mode <ls1|ls2|both>`")

    return bot

async def main():
    config = load_config(CONFIG_PATH)
    channels = get_channels_from_config(config)
    cli_arg = sys.argv[1] if len(sys.argv) > 1 else None
    chan_name, chan_id = select_channel(channels, cli_arg)
    print(f"[Config] Selected channel: '{chan_name}' ({chan_id})")

    core = RelayCore(config)
    bot = create_bot(core)

    # Start TCP Server
    server = await asyncio.start_server(core.handle_tcp_client, core.tcp_host, core.tcp_port)
    print(f"[TCP] Server listening on {core.tcp_host}:{core.tcp_port}")

    async with server:
        async with bot:
            token = config.get('bot_token')
            if not token or token == "YOUR_DISCORD_BOT_TOKEN_HERE":
                print("Error: bot_token is not set in config.json!", file=sys.stderr)
                sys.exit(1)

            @bot.event
            async def on_connect():
                core.active_channel = bot.get_channel(chan_id)
                if not core.active_channel:
                    try:
                        core.active_channel = await bot.fetch_channel(chan_id)
                    except Exception as e:
                        print(f"Warning: Could not fetch channel {chan_id}: {e}")

            await bot.start(token)

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nRelay stopped by user.")
