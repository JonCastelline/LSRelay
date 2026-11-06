# LSRelay

A bi-directional Linkshell relay between Final Fantasy XI (Windower 4) and Discord.

## Features

- **Bi-Directional Relay**:
  - **In-Game $\rightarrow$ Discord**: Relays in-game Linkshell (`/l` or `/l2`) messages to your designated Discord channel with sender attribution.
  - **Discord $\rightarrow$ In-Game**: Allows Discord members to speak into in-game Linkshell using `!l <message>` (or `!l2 <message>`), spoken through your character with Discord author attribution (e.g. `[Author] message`).
- **Flexible Linkshell Targeting**:
  - Configure to relay `ls1`, `ls2`, or `both`.
  - Interactive and CLI channel selection mirroring `Heartbeat`.
- **Zero Game Client Lag**:
  - Uses non-blocking TCP sockets (`127.0.0.1`) for sub-millisecond latency without disk I/O or file-lock contention.
- **Robust In-Game Chat Handling**:
  - Splits long Discord messages into safe, clean multi-line chunks to respect FFXI's chat line limits.
  - Throttled queue prevents chat spam disconnection.
  - Automatic sanitization of Discord emojis and control characters.
  - Deduplication prevents Discord-injected messages from echoing back into Discord.
- **In-Game Commands**:
  - `//lsr status`: View connection status and current relay mode.
  - `//lsr mode <ls1|ls2|both>`: Dynamically switch tracked linkshells.
  - `//lsr reconnect`: Reconnect to the local Python relay.

## Setup & Usage

### 1. Configure Discord Bot
Copy `config.example.json` to `config.json`:
```json
{
    "bot_token": "YOUR_DISCORD_BOT_TOKEN",
    "channels": {
        "Main Server - ls-chat": 123456789012345678
    },
    "linkshell_mode": "ls1",
    "game_chat_format": "[{author}] {message}",
    "tcp_host": "127.0.0.1",
    "tcp_port": 45124
}
```

### 2. Run the Python Relay
```bash
python relay.py
```

### 3. Load the Windower Addon
In FFXI:
```text
//lua load LSRelay
```
