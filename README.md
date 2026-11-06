# LSRelay

A multi-tenant, bi-directional Linkshell relay between Final Fantasy XI (Windower 4) and Discord.

Designed to run as a central **Hub & Spoke** service for an entire Linkshell or party. A single Discord bot runs on your server, while players run the lightweight Windower addon on their PCs.

---

## Highlights

- **Direct In-Game Speech**:
  - When you type `!l <message>` in Discord, your message speaks **directly from your in-game character** into Linkshell.
  - When your friends type `!l <message>`, it speaks from **their** characters.
- **One-Time Pairing (`//lsr link` & `!lslink`)**:
  - Friends don't need Discord developer accounts or bot tokens. They simply run `//lsr link` in FFXI and enter their 6-digit code in Discord.
- **Multi-Client Deduplication**:
  - If multiple members are online in the same linkshell, in-game chat is captured and posted to Discord **exactly once**.
  - Echo suppression guarantees Discord-injected messages do not loop back into Discord.
- **Zero Client Lag**:
  - Uses non-blocking TCP sockets (`LuaSocket`) with stream buffering. Zero disk I/O, zero file locks, zero frame drops.
- **Safe & Sanitized**:
  - Mentions resolve to clean `@Username` text.
  - `@everyone` / `@here` mass pings from in-game chat are disabled (`allowed_mentions=none`).
  - Emojis (astral and BMP) and control codes are stripped to protect FFXI's Shift-JIS chat encoding.
  - Long messages split cleanly into $\le 120$ character chunks, throttled at 600ms between lines.
- **Privacy & Security**:
  - Secrets (`config.json`) and runtime player pairings (`data/pairings.json`) are strictly git-ignored.

---

## Hub Setup (Server / PC)

The Hub hosts the Discord bot and the TCP server for connecting game clients.

### 1. Requirements
- Python 3.10+
- `pip install -r requirements.txt`

### 2. Configuration
Copy `config.example.json` to `config.json`:
```json
{
    "bot_token": "YOUR_DISCORD_BOT_TOKEN",
    "channels": {
        "Main Server - ls-chat": 123456789012345678
    },
    "linkshell_mode": "ls1",
    "tcp_host": "0.0.0.0",
    "tcp_port": 45124
}
```

- `bot_token`: Your Discord Bot Application token (requires `Message Content Intent` enabled in the Discord Developer Portal).
- `channels`: Mapping of channel names to Channel IDs. If multiple channels are defined, you can pick one interactively or via CLI argument.
- `linkshell_mode`: `"ls1"` (default), `"ls2"`, or `"both"`.
- `tcp_host`: `"0.0.0.0"` to accept connections from local network or internet.
- `tcp_port`: Default `45124`.

### 3. Run the Hub

#### Windows Setup
You can run the Hub directly from PowerShell or Command Prompt:
```powershell
python relay.py
# Or specify the channel name directly:
python relay.py "Main Server - ls-chat"
```

To run continuously in the background on Windows, you can use Task Scheduler (trigger on user login, Action: `python.exe relay.py "Main Server - ls-chat"`) or run via a tool like [NSSM](https://nssm.cc/) to run as a native Windows service.

#### Linux / systemd Setup
Create `/etc/systemd/system/lsrelay.service`:
```ini
[Unit]
Description=LSRelay Discord Hub
After=network.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/LSRelay
ExecStart=/usr/bin/python3 /home/pi/LSRelay/relay.py "Main Server - ls-chat"
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```
Enable and start:
```bash
sudo systemctl daemon-reload
sudo systemctl enable lsrelay
sudo systemctl start lsrelay
```

---

## Player Setup (Windower 4)

Linkshell members only need the `LSRelay` Windower addon.

1. Copy the `LSRelay` folder to `C:\FFXI\addons\LSRelay`.
2. In FFXI console:
   ```text
   //lua load LSRelay
   ```
3. Set your Hub server address (if not running locally):
   ```text
   //lsr host your-server-ip-or-domain.com
   ```
4. Link your Discord account:
   ```text
   //lsr link
   ```
   The addon will display a 6-digit code:
   `[LSRelay] Your Discord pairing code is: 582-194`
5. In your Discord channel, type:
   ```text
   !lslink 582-194
   ```
   The bot will confirm your account is linked!

---

## Commands Reference

### Discord Commands

| Command | Description |
| :--- | :--- |
| `!l <message>` | Speaks `<message>` into Linkshell 1 through your in-game character. |
| `!l2 <message>` | Speaks `<message>` into Linkshell 2 through your in-game character (only active if `linkshell_mode` is `"both"` or `"ls2"`). |
| `!lslink <code>` | Links your Discord account to your character using a 6-digit code. |
| `!lsunlink` | Removes your Discord account linkage. |
| `!lswho` | Displays the directory of linked members and who is currently online in-game. |
| `!lsstatus` | Displays Hub server and connection statistics. |
| `!lsr mode <ls1\|ls2\|both>` | Switches the Hub's active Linkshell relay mode. |

> **Note on Linkshell Targeting**: By default, `linkshell_mode` is set to `"ls1"` for your main Linkshell. In this mode, `!l2` is disabled and in-game LS2 chat is ignored. Support for a second Linkshell is strictly opt-in by configuring `"linkshell_mode": "both"` in `config.json` or switching dynamically with `!lsr mode both`.

### In-Game Addon Commands (`//lsr` or `//lsrelay`)

| Command | Description |
| :--- | :--- |
| `//lsr link` | Requests a 5-minute pairing code to link with Discord. |
| `//lsr status` | Displays character name, Hub address, connection status, and mode. |
| `//lsr host <host>` | Configures the Hub IP or domain (e.g. `//lsr host pi.home.net`). |
| `//lsr port <port>` | Configures the Hub TCP port. |
| `//lsr mode <ls1\|ls2\|both>` | Switches monitored Linkshell locally. |
| `//lsr reconnect` | Forces a reconnect to the Hub. |
| `//lsr debug` | Toggles verbose network and queue debug output. |

---

## Testing

Run the automated test suite with Python 3.10+:
```bash
python test_relay.py
```
All 21 unit and integration tests cover pairing codes, targeted client routing, global deduplication, and connection lifecycle.
