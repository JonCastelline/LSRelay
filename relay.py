import discord
from discord.ext import commands
import json
import os
import sys
import re
import time
import asyncio
import random
from datetime import datetime, timezone
from collections import deque

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(SCRIPT_DIR, 'data')
CONFIG_PATH = os.path.join(SCRIPT_DIR, 'config.json')
PAIRINGS_PATH = os.path.join(DATA_DIR, 'pairings.json')

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
    print(" LSRelay Discord Hub")
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
    text = re.sub(r'[\r\n\t]+', ' ', text)
    # Retain standard printable ASCII (32-126) for safe in-game rendering
    text = ''.join(c for c in text if 32 <= ord(c) <= 126)
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
        split_idx = remaining.rfind(' ', 0, max_length)
        if split_idx <= 0:
            split_idx = max_length
        chunks.append(remaining[:split_idx].strip())
        remaining = remaining[split_idx:].strip()
    if remaining:
        chunks.append(remaining)
    return chunks

def is_mode_allowed(target_mode, configured_mode):
    """Returns True if target_mode is permitted under configured_mode."""
    conf = (configured_mode or 'ls1').lower()
    tgt = (target_mode or 'ls1').lower()
    if conf == 'both':
        return tgt in ('ls1', 'ls2')
    return tgt == conf

class PairingManager:
    """Manages persistent Discord user to in-game character pairings and temporary pairing codes."""

    def __init__(self, filepath=PAIRINGS_PATH, code_expiry_seconds=300):
        self.filepath = filepath
        self.code_expiry_seconds = code_expiry_seconds
        # pending_codes: { "123456": {"character_name": "Jon", "created_at": float} }
        self.pending_codes = {}
        self.pairings = self.load_pairings()

    def load_pairings(self):
        if not os.path.exists(self.filepath):
            return {}
        try:
            with open(self.filepath, 'r', encoding='utf-8') as f:
                data = json.load(f)
                return data if isinstance(data, dict) else {}
        except Exception as e:
            print(f"[Pairings] Error loading pairings from {self.filepath}: {e}")
            return {}

    def save_pairings(self):
        os.makedirs(os.path.dirname(os.path.abspath(self.filepath)), exist_ok=True)
        tmp_path = self.filepath + '.tmp'
        try:
            with open(tmp_path, 'w', encoding='utf-8') as f:
                json.dump(self.pairings, f, indent=4)
            os.replace(tmp_path, self.filepath)
        except Exception as e:
            print(f"[Pairings] Error saving pairings to {self.filepath}: {e}")

    def generate_code(self, character_name):
        self._prune_expired_codes()
        # Clean existing pending codes for this character
        char_lower = character_name.strip().lower()
        for c, data in list(self.pending_codes.items()):
            if data['character_name'].lower() == char_lower:
                del self.pending_codes[c]

        code_digits = f"{random.randint(100000, 999999):06d}"
        formatted = f"{code_digits[:3]}-{code_digits[3:]}"
        self.pending_codes[code_digits] = {
            "character_name": character_name.strip(),
            "created_at": time.time()
        }
        return formatted

    def claim_code(self, code_input, discord_user_id):
        self._prune_expired_codes()
        clean_code = re.sub(r'[^0-9]', '', str(code_input))
        if clean_code in self.pending_codes:
            char_name = self.pending_codes[clean_code]["character_name"]
            del self.pending_codes[clean_code]

            # If this character was previously linked to another Discord user, remove the old link
            char_lower = char_name.lower()
            for uid, data in list(self.pairings.items()):
                if data.get("character_name", "").lower() == char_lower:
                    del self.pairings[uid]

            self.pairings[str(discord_user_id)] = {
                "character_name": char_name,
                "linked_at": datetime.now(timezone.utc).isoformat()
            }
            self.save_pairings()
            return char_name
        return None

    def unlink_user(self, discord_user_id):
        key = str(discord_user_id)
        if key in self.pairings:
            removed = self.pairings.pop(key)
            self.save_pairings()
            return removed.get("character_name")
        return None

    def get_character_for_user(self, discord_user_id):
        entry = self.pairings.get(str(discord_user_id))
        return entry.get("character_name") if entry else None

    def get_user_for_character(self, character_name):
        char_lower = character_name.strip().lower()
        for uid, data in self.pairings.items():
            if data.get("character_name", "").lower() == char_lower:
                return int(uid)
        return None

    def _prune_expired_codes(self):
        now = time.time()
        expired = [c for c, data in self.pending_codes.items() if now - data["created_at"] > self.code_expiry_seconds]
        for c in expired:
            del self.pending_codes[c]

class RelayCore:
    def __init__(self, config, pairing_manager=None):
        self.config = config
        self.pairing_manager = pairing_manager or PairingManager()
        self.linkshell_mode = config.get('linkshell_mode', 'ls1').lower()
        self.tcp_host = config.get('tcp_host', '0.0.0.0')
        self.tcp_port = int(config.get('tcp_port', 45124))
        
        # Multi-client tracking:
        # writer -> character_name (lowercase)
        self.char_by_writer = {}
        # character_name (lowercase) -> writer
        self.clients_by_char = {}
        
        # Deduplication cache for Discord-injected messages: deque of (mode, char_name_lower, message_text, timestamp)
        self.recent_discord_injections = deque()
        self.dedup_window = 10.0 # seconds

        # Global multi-client deduplication cache: dict of chat_hash -> timestamp
        self.global_chat_dedup = {}
        self.global_dedup_window = 3.0 # seconds

        self.message_count_relayed = 0
        self.bot = None
        self.active_channel = None

    def record_discord_injection(self, mode, character_name, text):
        now = time.time()
        self._prune_dedup_cache(now)
        clean = text.strip()
        mode_clean = mode.lower()
        char_clean = character_name.lower()
        self.recent_discord_injections.append((mode_clean, char_clean, clean, now))
        # Pre-populate global deduplication so all other online clients drop this injection when heard in-game
        key = f"{mode_clean}:{char_clean}:{clean}"
        self.global_chat_dedup[key] = now

    def consume_discord_echo(self, mode, sender, text):
        """Returns True and consumes the cached echo if this message matches a recent
        Discord injection originating from this specific character."""
        now = time.time()
        self._prune_dedup_cache(now)

        clean = text.strip()
        mode_clean = mode.lower()
        sender_clean = sender.lower()
        for idx, (m, c, t, _) in enumerate(self.recent_discord_injections):
            if m == mode_clean and c == sender_clean and t == clean:
                del self.recent_discord_injections[idx]
                # Keep global dedup refreshed so other online clients in the same LS don't relay it
                key = f"{mode_clean}:{sender_clean}:{clean}"
                self.global_chat_dedup[key] = now
                return True
        return False

    def is_global_duplicate(self, mode, sender, text):
        """Checks if another connected client already reported this identical in-game chat message."""
        now = time.time()
        # Prune old hashes
        expired = [h for h, ts in self.global_chat_dedup.items() if (now - ts) > self.global_dedup_window]
        for h in expired:
            del self.global_chat_dedup[h]

        key = f"{mode.lower()}:{sender.lower()}:{text.strip()}"
        if key in self.global_chat_dedup:
            return True
        self.global_chat_dedup[key] = now
        return False

    def _prune_dedup_cache(self, now):
        while self.recent_discord_injections and (now - self.recent_discord_injections[0][3] > self.dedup_window):
            self.recent_discord_injections.popleft()

    def get_online_characters(self):
        """Returns a list of character names currently connected to the hub."""
        return [c.capitalize() for c in self.clients_by_char.keys()]

    async def send_to_character(self, character_name, payload):
        """Sends a JSON line to a specific character's connected game client."""
        writer = self.clients_by_char.get(character_name.lower())
        if not writer:
            return False
        data = (json.dumps(payload) + '\n').encode('utf-8')
        try:
            writer.write(data)
            await writer.drain()
            return True
        except Exception as e:
            print(f"[TCP] Error writing to {character_name}: {e}")
            self.unregister_client(writer)
            return False

    async def broadcast_to_game(self, payload):
        """Sends a JSON line to all connected game clients."""
        if not self.char_by_writer:
            return False
        data = (json.dumps(payload) + '\n').encode('utf-8')
        dead_writers = set()
        for writer in list(self.char_by_writer.keys()):
            try:
                writer.write(data)
                await writer.drain()
            except Exception as e:
                print(f"[TCP] Error broadcasting to client: {e}")
                dead_writers.add(writer)
        for w in dead_writers:
            self.unregister_client(w)
        return True

    def register_client(self, writer, character_name):
        char_clean = character_name.strip().lower()
        # If this writer was previously registered under a different character, remove old mapping
        old_char = self.char_by_writer.get(writer)
        if old_char and old_char != char_clean:
            self.clients_by_char.pop(old_char, None)

        # If this character name had a different active writer, close the old one
        old_writer = self.clients_by_char.get(char_clean)
        if old_writer and old_writer != writer:
            self.char_by_writer.pop(old_writer, None)
            try:
                old_writer.close()
            except Exception:
                pass
        self.clients_by_char[char_clean] = writer
        self.char_by_writer[writer] = char_clean
        print(f"[TCP] Registered client for character '{character_name}'")

    def unregister_client(self, writer):
        char_clean = self.char_by_writer.pop(writer, None)
        if char_clean and self.clients_by_char.get(char_clean) == writer:
            del self.clients_by_char[char_clean]
            print(f"[TCP] Unregistered client for character '{char_clean}'")
        try:
            writer.close()
        except Exception:
            pass

    async def handle_tcp_client(self, reader, writer):
        addr = writer.get_extra_info('peername')
        print(f"[TCP] New connection from {addr}")

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
                            await self.process_game_message(line, writer)
                        except Exception as msg_err:
                            print(f"[TCP] Error processing game message: {msg_err}")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"[TCP] Client connection error from {addr}: {e}")
        finally:
            print(f"[TCP] Disconnected: {addr}")
            self.unregister_client(writer)

    async def process_game_message(self, line, writer):
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            print(f"[Game->Relay] Invalid JSON: {line}")
            return

        msg_type = payload.get('type')
        if msg_type == 'hello':
            player = payload.get('player', 'Unknown')
            if player and player != 'Unknown':
                self.register_client(writer, player)
            return

        if msg_type == 'request_pair_code':
            player = payload.get('player', 'Unknown')
            if player and player != 'Unknown':
                self.register_client(writer, player)
                code = self.pairing_manager.generate_code(player)
                resp = {
                    "type": "pair_code",
                    "code": code,
                    "expires_in": self.pairing_manager.code_expiry_seconds
                }
                writer.write((json.dumps(resp) + '\n').encode('utf-8'))
                await writer.drain()
                print(f"[Pairing] Generated code '{code}' for character '{player}'")
            return

        if msg_type == 'chat':
            mode = payload.get('mode', 'ls1').lower()
            sender = payload.get('sender', 'Unknown')
            message = payload.get('message', '').strip()

            if not is_mode_allowed(mode, self.linkshell_mode):
                return

            # Check if this was a Discord-injected message echoing back from that character
            if self.consume_discord_echo(mode, sender, message):
                return

            # Check if another online client already relayed this exact message to Discord
            if self.is_global_duplicate(mode, sender, message):
                return

            self.message_count_relayed += 1
            if self.active_channel:
                if self.linkshell_mode == 'both':
                    formatted = f"**[{mode.upper()}] {sender}:** {message}"
                else:
                    formatted = f"**[LS] {sender}:** {message}"
                try:
                    await self.active_channel.send(
                        formatted,
                        allowed_mentions=discord.AllowedMentions.none()
                    )
                except Exception as e:
                    print(f"[Relay->Discord] Failed to send message: {e}")
            else:
                print(f"[Relay] Warning: No active Discord channel bound. Message not sent: {message}")

        elif msg_type == 'config':
            # Addon requests mode change
            new_mode = payload.get('linkshell_mode')
            if new_mode in ('ls1', 'ls2', 'both'):
                self.linkshell_mode = new_mode
                print(f"[Config] Mode updated from addon: {self.linkshell_mode}")

    async def process_discord_message(self, message):
        """Processes an incoming Discord message, looks up linked character, and routes to that character's client."""
        content = message.clean_content.strip()

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

        # Check if target mode is permitted under current hub config
        if not is_mode_allowed(target_mode, self.linkshell_mode):
            await message.reply(
                f"⚠️ Linkshell `{target_mode.upper()}` is not active. Current mode is `{self.linkshell_mode}`."
            )
            return

        # Look up linked character for this Discord author
        char_name = self.pairing_manager.get_character_for_user(message.author.id)
        if not char_name:
            await message.reply(
                "⚠️ You haven't linked your character yet!\n"
                "In FFXI, type `//lsr link` to get a 6-digit code, then type `!lslink <code>` here."
            )
            return

        # Check if character's client is currently connected
        char_clean = char_name.lower()
        if char_clean not in self.clients_by_char:
            await message.reply(
                f"⚠️ Your character **{char_name}** is not currently online or connected to LSRelay."
            )
            return

        # Sanitize for FFXI chat (strip emojis/control chars)
        sanitized = sanitize_for_game(raw_text)
        if not sanitized:
            await message.reply("⚠️ Message contained only unsupported characters or was empty.")
            return

        # Split into max 120 character chunks for FFXI chat bar
        chunks = split_message(sanitized, max_length=120)

        # Route chunks strictly to THIS character's client
        for chunk in chunks:
            self.record_discord_injection(target_mode, char_name, chunk)
            sent = await self.send_to_character(char_name, {
                "type": "inject_chat",
                "mode": target_mode,
                "message": chunk
            })
            if not sent:
                await message.reply(f"⚠️ Failed to deliver message to {char_name}'s client.")
                return

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

        # Check for bot commands
        if content.startswith(('!lsr', '!relay', '!lslink', '!lsunlink', '!lswho', '!lsstatus')):
            await bot.process_commands(message)
            return

        # Check strictly for !l, !ls, !l2, !ls2 command prefixes
        if content.startswith(('!l ', '!ls ', '!l2 ', '!ls2 ')) or content in ('!l', '!ls', '!l2', '!ls2'):
            await core.process_discord_message(message)
            return

        await bot.process_commands(message)

    @bot.command(name='lslink')
    async def lslink_cmd(ctx, code: str = None):
        """Links your Discord user to your in-game character using a pairing code."""
        if not code:
            await ctx.reply("Usage: `!lslink <6-digit-code>` (Get your code in FFXI using `//lsr link`)")
            return
        char_name = core.pairing_manager.claim_code(code, ctx.author.id)
        if char_name:
            await ctx.reply(f"✅ Successfully linked your Discord account to **{char_name}**!")
        else:
            await ctx.reply("❌ Invalid or expired pairing code. In FFXI, type `//lsr link` to generate a new code.")

    @bot.command(name='lsunlink')
    async def lsunlink_cmd(ctx):
        """Unlinks your Discord user from your in-game character."""
        removed = core.pairing_manager.unlink_user(ctx.author.id)
        if removed:
            await ctx.reply(f"✅ Unlinked your Discord account from **{removed}**.")
        else:
            await ctx.reply("ℹ️ You do not currently have a linked character.")

    @bot.command(name='lswho')
    async def lswho_cmd(ctx):
        """Lists linked characters and their online status."""
        pairings = core.pairing_manager.pairings
        if not pairings:
            await ctx.send("No characters are currently linked. Type `//lsr link` in FFXI to get started.")
            return

        online_chars = {c.lower() for c in core.clients_by_char.keys()}
        lines = []
        for uid_str, data in pairings.items():
            char = data.get("character_name", "Unknown")
            is_online = char.lower() in online_chars
            status_dot = "🟢 **Online**" if is_online else "⚪ *Offline*"
            lines.append(f"• <@{uid_str}> → **{char}** ({status_dot})")

        embed = discord.Embed(title="Linkshell Directory", description="\n".join(lines), color=0x3498db)
        await ctx.send(embed=embed)

    @bot.command(name='lsstatus')
    async def lsstatus_cmd(ctx):
        """Displays hub status."""
        online = core.get_online_characters()
        online_str = ", ".join(online) if online else "None"
        embed = discord.Embed(title="LSRelay Hub Status", color=0x2ecc71 if online else 0xe74c3c)
        embed.add_field(name="Online Characters", value=online_str, inline=False)
        embed.add_field(name="Total Linked", value=str(len(core.pairing_manager.pairings)), inline=True)
        embed.add_field(name="Linkshell Mode", value=core.linkshell_mode.upper(), inline=True)
        embed.add_field(name="Relayed Messages", value=str(core.message_count_relayed), inline=True)
        embed.set_footer(text="Use !l <message> to speak in LS1 | !lswho to view linked members")
        await ctx.send(embed=embed)

    @bot.command(name='lsr')
    async def lsr_cmd(ctx, action: str = None, *, arg: str = None):
        """Relay status and control command."""
        action = (action or 'status').lower()
        if action == 'status':
            await lsstatus_cmd(ctx)
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
            await ctx.send("Available: `!lsr status`, `!lsr mode <ls1|ls2|both>`, `!lslink <code>`, `!lsunlink`, `!lswho`")

    return bot

async def main():
    config = load_config(CONFIG_PATH)
    channels = get_channels_from_config(config)
    cli_arg = sys.argv[1] if len(sys.argv) > 1 else None
    chan_name, chan_id = select_channel(channels, cli_arg)
    print(f"[Config] Selected channel: '{chan_name}' ({chan_id})")

    core = RelayCore(config)
    bot = create_bot(core)

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
