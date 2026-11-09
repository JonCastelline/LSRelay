# LSRelay (Windower 4 Addon)

A lightweight, non-blocking Final Fantasy XI (Windower 4) addon that bridges in-game Linkshell chat, private tells, and GM emergency alerts directly to Discord.

---

## Features

- **Bi-Directional Linkshell Chat**:
  - In-game chat from your Linkshell appears in your community's Discord channel.
  - When you type `!l <message>` in Discord, the message speaks **directly from your character** in-game with no awkward bot prefixes.
- **Private Tells via Discord DMs**:
  - Incoming `/tell` messages are forwarded privately to your 1-on-1 Discord DM with the bot.
  - Reply straight from Discord with `!r <msg>` or send a tell with `!t <PlayerName> <msg>`.
- **GM Emergency Alerts**:
  - Game Master communications trigger a high-priority Discord alert DM, an audible alarm chime, and an automatic safety protocol (halts combat actions and unloads configured automation addons).
- **Automatic Pearl Slot Detection**:
  - Wear linkpearls in Slot 1, Slot 2, or both across your Inventory, Mog Satchel, Mog Sack, or Mog Case—the addon automatically resolves linkshell names and routes chat seamlessly.
- **Zero Game Lag**:
  - Uses non-blocking TCP sockets (`LuaSocket`) with rate-throttled chat injection. Zero disk I/O, zero stuttering, zero frame drops.

---

## Installation

1. Create a folder named `LSRelay` inside your Windower addons directory:
   ```text
   Windower4/addons/LSRelay/
   ```
2. Download [`LSRelay.lua`](https://github.com/JonCastelline/LSRelay/blob/master/LSRelay.lua) and place it inside that folder:
   ```text
   Windower4/addons/LSRelay/LSRelay.lua
   ```
3. In-game, load the addon:
   ```text
   //lua load LSRelay
   ```
   *(To load automatically on startup, add `lua load LSRelay` to your `Windower4/scripts/init.txt`)*

---

## Setup & Pairing

1. **Configure Hub Host** (if connecting to a remote or hosted server):
   ```text
   //lsr host <host_or_ip>
   ```

2. **Link Your Character to Discord**:
   - In-game, type:
     ```text
     //lsr link
     ```
   - The addon will print a 6-digit pairing code (valid for 5 minutes).
   - In your Linkshell's Discord channel, type:
     ```text
     !lslink 123-456
     ```
   - The bot will confirm your linkage! From now on, `!l <message>` in Discord speaks from your character in-game.

---

## In-Game Commands

| Command | Description |
| :--- | :--- |
| `//lsr status` | Displays character name, Hub address, connection status, equipped linkshells, and alert settings. |
| `//lsr link` | Requests a 5-minute pairing code to link with your Discord account. |
| `//lsr host <host>` | Configures the Hub IP or domain name. |
| `//lsr port <port>` | Configures the Hub TCP port (default: `45124`). |
| `//lsr tells [on\|off]` | Toggles forwarding private tells to your Discord DM. |
| `//lsr gm [on\|off]` | Toggles GM emergency alerts and safety protocol. |
| `//lsr gmunload <addons>` | Comma-separated list of addons to unload on GM alert (e.g. `trust,react`). |
| `//lsr reconnect` | Forces an immediate reconnect to the Hub. |
| `//lsr debug` | Toggles verbose network and queue debug messages. |

---

## Discord Commands

| Command | Location | Description |
| :--- | :--- | :--- |
| `!l <message>` | Linkshell Channel | Speaks `<message>` into Linkshell through your character. |
| `!l2 <message>` | Linkshell Channel | Speaks `<message>` into Linkshell 2 through your character. |
| `!t <Player> <msg>` | Bot DM | Sends a private tell `/t <Player> <msg>` from your character. |
| `!r <message>` | Bot DM | Replies `/t <LastSender> <msg>` to the last person who sent you a tell. |
| `!lslink <code>` | Linkshell Channel | Links your Discord account using a 6-digit pairing code. |
| `!lsunlink` | Linkshell Channel | Removes your character's Discord linkage. |
| `!lswho` | Linkshell Channel | Lists linked members and who is currently online in-game. |
| `!lsstatus` | Linkshell Channel | Displays server connection and linkshell status. |
