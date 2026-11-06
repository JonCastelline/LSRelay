import unittest
from unittest.mock import AsyncMock, MagicMock, patch
import time
import json
import asyncio

from relay import (
    strip_json_comments,
    get_channels_from_config,
    select_channel,
    sanitize_for_game,
    split_message,
    format_game_message,
    is_mode_allowed,
    RelayCore
)

class TestLSRelayHelpers(unittest.TestCase):

    def test_strip_json_comments(self):
        jsonc = """
        {
            // This is a single line comment
            "bot_token": "token123", /* inline comment */
            "channels": {
                "general": 111, // channel 1
            },
            "url": "http://example.com/test//not_a_comment"
        }
        """
        cleaned = strip_json_comments(jsonc)
        parsed = json.loads(cleaned)
        self.assertEqual(parsed["bot_token"], "token123")
        self.assertEqual(parsed["channels"]["general"], 111)
        self.assertEqual(parsed["url"], "http://example.com/test//not_a_comment")

    def test_get_channels_from_config_dict(self):
        cfg = {
            "channels": {
                "Channel 1": 1001,
                "Channel 2": "1002",
                "_comment": "ignore me"
            }
        }
        channels = get_channels_from_config(cfg)
        self.assertEqual(channels, {"Channel 1": 1001, "Channel 2": 1002})

    def test_get_channels_from_config_list(self):
        cfg = {
            "channels": [
                {"name": "Alpha", "id": 2001},
                {"name": "Beta", "id": "2002"},
                {"name": "_skip", "id": 9999}
            ]
        }
        channels = get_channels_from_config(cfg)
        self.assertEqual(channels, {"Alpha": 2001, "Beta": 2002})

    def test_get_channels_from_config_legacy(self):
        cfg = {"channel_id": "3003"}
        channels = get_channels_from_config(cfg)
        self.assertEqual(channels, {"Default": 3003})

    def test_select_channel_single(self):
        channels = {"OnlyOne": 12345}
        name, cid = select_channel(channels)
        self.assertEqual(name, "OnlyOne")
        self.assertEqual(cid, 12345)

    def test_select_channel_cli_arg(self):
        channels = {"First": 111, "Second": 222}
        name, cid = select_channel(channels, cli_arg="2")
        self.assertEqual(name, "Second")
        self.assertEqual(cid, 222)

        name2, cid2 = select_channel(channels, cli_arg="first")
        self.assertEqual(name2, "First")
        self.assertEqual(cid2, 111)

    def test_select_channel_interactive(self):
        channels = {"A": 10, "B": 20}
        with patch('builtins.input', return_value="2"):
            name, cid = select_channel(channels, input_func=lambda _: "2")
            self.assertEqual(name, "B")
            self.assertEqual(cid, 20)

    def test_sanitize_for_game(self):
        # Emojis stripped
        text_with_emoji = "Hello world! 🔥🎉🚀"
        sanitized = sanitize_for_game(text_with_emoji)
        self.assertEqual(sanitized, "Hello world!")

        # Newlines normalized
        text_with_newlines = "Line 1\r\nLine 2\n\nLine 3"
        sanitized2 = sanitize_for_game(text_with_newlines)
        self.assertEqual(sanitized2, "Line 1 Line 2 Line 3")

        # Control characters removed
        text_with_ctrl = "Hello\x00\x07World"
        sanitized3 = sanitize_for_game(text_with_ctrl)
        self.assertEqual(sanitized3, "HelloWorld")

    def test_split_message(self):
        short = "Just a short message."
        self.assertEqual(split_message(short, 120), [short])

        exact = "A" * 120
        self.assertEqual(split_message(exact, 120), [exact])

        # Word boundary split
        long_msg = "Word1 " * 30 # 180 chars
        chunks = split_message(long_msg, 50)
        self.assertTrue(len(chunks) > 1)
        for c in chunks:
            self.assertLessEqual(len(c), 50)
        rejoined = ' '.join(chunks)
        self.assertEqual(rejoined, long_msg.strip())

        # Giant single word split
        giant_word = "X" * 150
        giant_chunks = split_message(giant_word, 50)
        self.assertEqual(len(giant_chunks), 3)
        self.assertEqual(giant_chunks[0], "X" * 50)
        self.assertEqual(giant_chunks[1], "X" * 50)
        self.assertEqual(giant_chunks[2], "X" * 50)

    def test_format_game_message(self):
        res = format_game_message("Jon", "Hello world", "[{author}] {message}")
        self.assertEqual(res, "[Jon] Hello world")

        res_raw = format_game_message("Jon", "Hello world", "{message}")
        self.assertEqual(res_raw, "Hello world")

    def test_is_mode_allowed(self):
        self.assertTrue(is_mode_allowed('ls1', 'ls1'))
        self.assertFalse(is_mode_allowed('ls2', 'ls1'))

        self.assertTrue(is_mode_allowed('ls2', 'ls2'))
        self.assertFalse(is_mode_allowed('ls1', 'ls2'))

        self.assertTrue(is_mode_allowed('ls1', 'both'))
        self.assertTrue(is_mode_allowed('ls2', 'both'))
        self.assertFalse(is_mode_allowed('say', 'both'))

class TestRelayCore(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.config = {
            "linkshell_mode": "ls1",
            "game_chat_format": "[{author}] {message}",
            "tcp_host": "127.0.0.1",
            "tcp_port": 45124
        }
        self.core = RelayCore(self.config)

    def test_deduplication(self):
        self.core.record_discord_injection("ls1", "Hello game!")
        self.assertTrue(self.core.is_recent_discord_injection("ls1", "Hello game!"))
        # Different mode
        self.assertFalse(self.core.is_recent_discord_injection("ls2", "Hello game!"))
        # Different text
        self.assertFalse(self.core.is_recent_discord_injection("ls1", "Different text"))

        # Pruning check after dedup window
        self.core.dedup_window = 0.01
        time.sleep(0.02)
        self.assertFalse(self.core.is_recent_discord_injection("ls1", "Hello game!"))

    async def test_process_game_message_relayed_to_discord(self):
        mock_channel = AsyncMock()
        self.core.active_channel = mock_channel

        # LS1 message
        game_line = json.dumps({
            "type": "chat",
            "mode": "ls1",
            "sender": "Frodobald",
            "message": "Heading to Reisenjima!"
        })
        await self.core.process_game_message(game_line)
        mock_channel.send.assert_awaited_once_with("**[LS] Frodobald:** Heading to Reisenjima!")

    async def test_process_game_message_mode_both(self):
        self.core.linkshell_mode = "both"
        mock_channel = AsyncMock()
        self.core.active_channel = mock_channel

        game_line = json.dumps({
            "type": "chat",
            "mode": "ls2",
            "sender": "Teejay",
            "message": "Dynamis tonight?"
        })
        await self.core.process_game_message(game_line)
        mock_channel.send.assert_awaited_once_with("**[LS2] Teejay:** Dynamis tonight?")

    async def test_process_game_message_dedup_suppression(self):
        mock_channel = AsyncMock()
        self.core.active_channel = mock_channel

        # Record Discord injection
        self.core.record_discord_injection("ls1", "[Jon] Be right there")

        # Game echoes the exact message back
        game_line = json.dumps({
            "type": "chat",
            "mode": "ls1",
            "sender": "Jon",
            "message": "[Jon] Be right there"
        })
        await self.core.process_game_message(game_line)
        # Should NOT have sent to Discord because it's a dedup echo
        mock_channel.send.assert_not_awaited()

    async def test_process_discord_message_success(self):
        mock_msg = MagicMock()
        mock_msg.content = "!l Ready to pull!"
        mock_msg.author.display_name = "Jon"
        mock_msg.reply = AsyncMock()
        mock_msg.add_reaction = AsyncMock()

        # Add mock client writer
        mock_writer = MagicMock()
        mock_writer.drain = AsyncMock()
        self.core.active_clients.add(mock_writer)

        await self.core.process_discord_message(mock_msg)

        # Verified writer was written to with injected chat
        mock_writer.write.assert_called_once()
        written_data = json.loads(mock_writer.write.call_args[0][0].decode('utf-8'))
        self.assertEqual(written_data["type"], "inject_chat")
        self.assertEqual(written_data["mode"], "ls1")
        self.assertEqual(written_data["message"], "[Jon] Ready to pull!")

        # Verified reaction added
        mock_msg.add_reaction.assert_awaited_once_with("✅")

    async def test_process_discord_message_wrong_mode(self):
        # Configured for ls1 only
        mock_msg = MagicMock()
        mock_msg.content = "!l2 Test on ls2"
        mock_msg.reply = AsyncMock()

        await self.core.process_discord_message(mock_msg)
        mock_msg.reply.assert_awaited_once()
        self.assertIn("Linkshell `LS2` is not active", mock_msg.reply.call_args[0][0])

    async def test_tcp_server_client_integration(self):
        # Test full TCP connection, handshake, and two-way messaging
        server = await asyncio.start_server(self.core.handle_tcp_client, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        async with server:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)

            # Receive initial config from server
            line = await reader.readline()
            init_cfg = json.loads(line.decode('utf-8'))
            self.assertEqual(init_cfg["type"], "config")
            self.assertEqual(init_cfg["linkshell_mode"], "ls1")

            # Game client sends chat to server
            mock_channel = AsyncMock()
            self.core.active_channel = mock_channel

            chat_payload = {
                "type": "chat",
                "mode": "ls1",
                "sender": "Jon",
                "message": "Hello from TCP!"
            }
            writer.write((json.dumps(chat_payload) + '\n').encode('utf-8'))
            await writer.drain()

            # Wait briefly for server processing
            await asyncio.sleep(0.05)
            mock_channel.send.assert_awaited_once_with("**[LS] Jon:** Hello from TCP!")

            # Server broadcasts chat injection to client
            broadcast_ok = await self.core.broadcast_to_game({
                "type": "inject_chat",
                "mode": "ls1",
                "message": "[DiscordUser] Greetings!"
            })
            self.assertTrue(broadcast_ok)

            inject_line = await reader.readline()
            inject_data = json.loads(inject_line.decode('utf-8'))
            self.assertEqual(inject_data["type"], "inject_chat")
            self.assertEqual(inject_data["message"], "[DiscordUser] Greetings!")

            writer.close()
            await writer.wait_closed()


if __name__ == '__main__':
    unittest.main()
