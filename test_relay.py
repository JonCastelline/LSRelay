import unittest
from unittest.mock import AsyncMock, MagicMock, patch
import time
import json
import asyncio
import io
import os
import tempfile
import discord

from relay import (
    strip_json_comments,
    get_channels_from_config,
    select_channel,
    sanitize_for_game,
    split_message,
    is_mode_allowed,
    PairingManager,
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

    def test_select_channel_interactive(self):
        channels = {"A": 10, "B": 20}
        with patch('sys.stdout', new=io.StringIO()):
            name, cid = select_channel(channels, input_func=lambda _: "2")
            self.assertEqual(name, "B")
            self.assertEqual(cid, 20)

    def test_sanitize_for_game(self):
        text_with_emoji = "Hello world! 🔥🎉🚀 ☀️❤️⭐"
        sanitized = sanitize_for_game(text_with_emoji)
        self.assertEqual(sanitized, "Hello world!")

        text_with_newlines = "Line 1\r\nLine 2\n\nLine 3"
        sanitized2 = sanitize_for_game(text_with_newlines)
        self.assertEqual(sanitized2, "Line 1 Line 2 Line 3")

    def test_split_message(self):
        short = "Just a short message."
        self.assertEqual(split_message(short, 120), [short])

        long_msg = "Word1 " * 30
        chunks = split_message(long_msg, 50)
        self.assertTrue(len(chunks) > 1)
        for c in chunks:
            self.assertLessEqual(len(c), 50)
        self.assertEqual(' '.join(chunks), long_msg.strip())

    def test_is_mode_allowed(self):
        self.assertTrue(is_mode_allowed('ls1', 'ls1'))
        self.assertFalse(is_mode_allowed('ls2', 'ls1'))
        self.assertTrue(is_mode_allowed('ls2', 'ls2'))
        self.assertFalse(is_mode_allowed('ls1', 'ls2'))
        self.assertTrue(is_mode_allowed('ls1', 'both'))
        self.assertTrue(is_mode_allowed('ls2', 'both'))

class TestPairingManager(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.pairings_file = os.path.join(self.temp_dir.name, "pairings.json")
        self.pm = PairingManager(filepath=self.pairings_file, code_expiry_seconds=1.0)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_generate_and_claim_code(self):
        code = self.pm.generate_code("JonChar")
        self.assertRegex(code, r'^\d{3}-\d{3}$')

        # Claim with formatted string
        claimed = self.pm.claim_code(code, 123456789)
        self.assertEqual(claimed, "JonChar")
        self.assertEqual(self.pm.get_character_for_user(123456789), "JonChar")
        self.assertEqual(self.pm.get_user_for_character("jonchar"), 123456789)

        # Claim again should fail (code consumed)
        self.assertIsNone(self.pm.claim_code(code, 999))

    def test_code_expiration(self):
        self.pm.code_expiry_seconds = 0.01
        code = self.pm.generate_code("AliceChar")
        time.sleep(0.02)
        self.assertIsNone(self.pm.claim_code(code, 987654))

    def test_unlink_user(self):
        code = self.pm.generate_code("BobChar")
        self.pm.claim_code(code, 5555)
        self.assertEqual(self.pm.get_character_for_user(5555), "BobChar")

        unlinked = self.pm.unlink_user(5555)
        self.assertEqual(unlinked, "BobChar")
        self.assertIsNone(self.pm.get_character_for_user(5555))
        self.assertIsNone(self.pm.unlink_user(5555))

    def test_relink_character_to_different_user(self):
        code1 = self.pm.generate_code("SharedChar")
        self.pm.claim_code(code1, 111)
        self.assertEqual(self.pm.get_character_for_user(111), "SharedChar")

        # Re-link same character to user 222
        code2 = self.pm.generate_code("SharedChar")
        self.pm.claim_code(code2, 222)
        self.assertEqual(self.pm.get_character_for_user(222), "SharedChar")
        # Old user 111 should no longer be linked to SharedChar
        self.assertIsNone(self.pm.get_character_for_user(111))

    def test_persistence(self):
        code = self.pm.generate_code("SaveChar")
        self.pm.claim_code(code, 7777)

        # Reload from file in new instance
        new_pm = PairingManager(filepath=self.pairings_file)
        self.assertEqual(new_pm.get_character_for_user(7777), "SaveChar")

class TestRelayCoreMultiTenant(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.pairings_file = os.path.join(self.temp_dir.name, "pairings.json")
        self.pm = PairingManager(filepath=self.pairings_file)
        self.config = {
            "linkshell_mode": "ls1",
            "tcp_host": "127.0.0.1",
            "tcp_port": 45124
        }
        self.core = RelayCore(self.config, pairing_manager=self.pm)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_client_registration_and_online_list(self):
        mock_writer_jon = MagicMock()
        mock_writer_alice = MagicMock()

        self.core.register_client(mock_writer_jon, "JonChar")
        self.core.register_client(mock_writer_alice, "AliceChar")

        online = self.core.get_online_characters()
        self.assertIn("Jonchar", online)
        self.assertIn("Alicechar", online)

        self.core.unregister_client(mock_writer_jon)
        self.assertNotIn("Jonchar", self.core.get_online_characters())
        self.assertIn("Alicechar", self.core.get_online_characters())

    def test_client_character_switch_on_same_writer(self):
        mock_writer = MagicMock()
        self.core.register_client(mock_writer, "CharOne")
        self.assertIn("Charone", self.core.get_online_characters())

        # Same writer switches to CharTwo
        self.core.register_client(mock_writer, "CharTwo")
        online = self.core.get_online_characters()
        self.assertNotIn("Charone", online)
        self.assertIn("Chartwo", online)

    async def test_process_discord_message_unlinked(self):
        mock_msg = MagicMock()
        mock_msg.clean_content = "!l Hello everyone"
        mock_msg.author.id = 111111 # Unlinked
        mock_msg.reply = AsyncMock()

        await self.core.process_discord_message(mock_msg)
        mock_msg.reply.assert_awaited_once()
        self.assertIn("You haven't linked your character yet", mock_msg.reply.call_args[0][0])

    async def test_process_discord_message_offline(self):
        # Link user but do not connect client
        code = self.pm.generate_code("JonChar")
        self.pm.claim_code(code, 222222)

        mock_msg = MagicMock()
        mock_msg.clean_content = "!l Hello from Discord"
        mock_msg.author.id = 222222
        mock_msg.reply = AsyncMock()

        await self.core.process_discord_message(mock_msg)
        mock_msg.reply.assert_awaited_once()
        self.assertIn("is not currently online", mock_msg.reply.call_args[0][0])

    async def test_process_discord_message_targeted_delivery(self):
        # Link user and connect client
        code = self.pm.generate_code("JonChar")
        self.pm.claim_code(code, 333333)

        mock_writer_jon = MagicMock()
        mock_writer_jon.drain = AsyncMock()
        self.core.register_client(mock_writer_jon, "JonChar")

        # Also connect another client (Alice)
        mock_writer_alice = MagicMock()
        mock_writer_alice.drain = AsyncMock()
        self.core.register_client(mock_writer_alice, "AliceChar")

        mock_msg = MagicMock()
        mock_msg.clean_content = "!l Heading to Dynamis!"
        mock_msg.author.id = 333333
        mock_msg.reply = AsyncMock()
        mock_msg.add_reaction = AsyncMock()

        await self.core.process_discord_message(mock_msg)

        # Delivered to Jon ONLY, not Alice
        mock_writer_jon.write.assert_called_once()
        written = json.loads(mock_writer_jon.write.call_args[0][0].decode('utf-8'))
        self.assertEqual(written["type"], "inject_chat")
        self.assertEqual(written["mode"], "ls1")
        self.assertEqual(written["message"], "Heading to Dynamis!")

        mock_writer_alice.write.assert_not_called()
        mock_msg.add_reaction.assert_awaited_once_with("✅")

    async def test_global_chat_deduplication_multi_client(self):
        mock_channel = AsyncMock()
        self.core.active_channel = mock_channel

        # Client 1 reports message from third-party in LS
        payload1 = json.dumps({
            "type": "chat",
            "mode": "ls1",
            "sender": "RandomPlayer",
            "message": "Anyone need help?"
        })
        await self.core.process_game_message(payload1, MagicMock())
        self.assertEqual(mock_channel.send.call_count, 1)
        self.assertEqual(mock_channel.send.call_args[0][0], "**[LS] RandomPlayer:** Anyone need help?")

        # Client 2 reports the same message 50ms later
        await self.core.process_game_message(payload1, MagicMock())
        # Should be dropped as global duplicate
        self.assertEqual(mock_channel.send.call_count, 1)

    async def test_echo_suppression_for_author_and_other_clients(self):
        mock_channel = AsyncMock()
        self.core.active_channel = mock_channel

        # Jon injects message from Discord
        self.core.record_discord_injection("ls1", "JonChar", "Pulling now!")

        # Client 1 (JonChar's game) echoes message back
        echo_payload = json.dumps({
            "type": "chat",
            "mode": "ls1",
            "sender": "JonChar",
            "message": "Pulling now!"
        })
        await self.core.process_game_message(echo_payload, MagicMock())
        # Client 1's echo consumed, nothing sent to Discord
        mock_channel.send.assert_not_awaited()

        # Client 2 (AliceChar's game, in the same LS) also hears JonChar say "Pulling now!"
        await self.core.process_game_message(echo_payload, MagicMock())
        # Client 2's packet is dropped by global deduplication, still nothing sent to Discord!
        mock_channel.send.assert_not_awaited()

    async def test_tcp_pairing_request_flow(self):
        server = await asyncio.start_server(self.core.handle_tcp_client, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        async with server:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)

            # Receive initial config
            await reader.readline()

            # Addon requests pair code
            req = {"type": "request_pair_code", "player": "PlayerOne"}
            writer.write((json.dumps(req) + '\n').encode('utf-8'))
            await writer.drain()

            resp_line = await reader.readline()
            resp = json.loads(resp_line.decode('utf-8'))
            self.assertEqual(resp["type"], "pair_code")
            self.assertRegex(resp["code"], r'^\d{3}-\d{3}$')

            # Verify claiming in pairing manager works
            char = self.pm.claim_code(resp["code"], 4444)
            self.assertEqual(char, "PlayerOne")

            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), timeout=1.0)
            except Exception:
                pass

    async def test_incoming_tell_forwarded_to_discord_dm(self):
        code = self.pm.generate_code("JonChar")
        self.pm.claim_code(code, 111111)

        mock_user = AsyncMock()
        mock_bot = MagicMock()
        mock_bot.get_user.return_value = mock_user
        self.core.bot = mock_bot

        mock_writer = MagicMock()
        self.core.register_client(mock_writer, "JonChar")

        tell_payload = json.dumps({
            "type": "tell",
            "sender": "AliceFriend",
            "message": "Are you ready for Dyna?"
        })
        await self.core.process_game_message(tell_payload, mock_writer)

        mock_user.send.assert_awaited_once()
        sent_text = mock_user.send.call_args[0][0]
        self.assertIn("[Tell from AliceFriend]:", sent_text)
        self.assertIn("Are you ready for Dyna?", sent_text)
        self.assertEqual(self.core.last_tell_by_user[111111], "AliceFriend")

    async def test_incoming_gm_alert_forwarded_to_discord_dm(self):
        code = self.pm.generate_code("JonChar")
        self.pm.claim_code(code, 111111)

        mock_user = AsyncMock()
        mock_bot = MagicMock()
        mock_bot.get_user.return_value = mock_user
        self.core.bot = mock_bot

        mock_writer = MagicMock()
        self.core.register_client(mock_writer, "JonChar")

        gm_payload = json.dumps({
            "type": "gm",
            "sender": "GM_Dave",
            "message": "Greetings, please respond."
        })
        await self.core.process_game_message(gm_payload, mock_writer)

        mock_user.send.assert_awaited_once()
        sent_text = mock_user.send.call_args[0][0]
        self.assertIn("CRITICAL GM ALERT", sent_text)
        self.assertIn("GM_Dave", sent_text)
        self.assertIn("Greetings, please respond.", sent_text)

    async def test_process_dm_message_tell_and_reply(self):
        code = self.pm.generate_code("JonChar")
        self.pm.claim_code(code, 111111)

        mock_writer = MagicMock()
        mock_writer.drain = AsyncMock()
        self.core.register_client(mock_writer, "JonChar")

        # 1. Send tell: !t Bob Hello Bob!
        mock_msg = MagicMock()
        mock_msg.clean_content = "!t Bob Hello Bob!"
        mock_msg.author.id = 111111
        mock_msg.reply = AsyncMock()
        mock_msg.add_reaction = AsyncMock()

        await self.core.process_dm_message(mock_msg)

        mock_writer.write.assert_called_once()
        written = json.loads(mock_writer.write.call_args[0][0].decode('utf-8'))
        self.assertEqual(written["type"], "inject_chat")
        self.assertEqual(written["mode"], "tell")
        self.assertEqual(written["target"], "Bob")
        self.assertEqual(written["message"], "Hello Bob!")
        mock_msg.add_reaction.assert_awaited_once_with("✅")
        self.assertEqual(self.core.last_tell_by_user[111111], "Bob")

        # 2. Reply: !r Thanks!
        mock_writer.write.reset_mock()
        mock_msg.add_reaction.reset_mock()
        mock_msg.clean_content = "!r Thanks!"

        await self.core.process_dm_message(mock_msg)

        mock_writer.write.assert_called_once()
        written2 = json.loads(mock_writer.write.call_args[0][0].decode('utf-8'))
        self.assertEqual(written2["type"], "inject_chat")
        self.assertEqual(written2["mode"], "tell")
        self.assertEqual(written2["target"], "Bob")
        self.assertEqual(written2["message"], "Thanks!")
        mock_msg.add_reaction.assert_awaited_once_with("✅")

    async def test_process_dm_message_unlinked(self):
        mock_msg = MagicMock()
        mock_msg.clean_content = "!t Bob Hello"
        mock_msg.author.id = 999999
        mock_msg.reply = AsyncMock()

        await self.core.process_dm_message(mock_msg)
        mock_msg.reply.assert_awaited_once()
        self.assertIn("haven't linked", mock_msg.reply.call_args[0][0])

    async def test_process_dm_message_reply_without_prior_tell(self):
        code = self.pm.generate_code("JonChar")
        self.pm.claim_code(code, 111111)

        mock_writer = MagicMock()
        self.core.register_client(mock_writer, "JonChar")

        mock_msg = MagicMock()
        mock_msg.clean_content = "!r Still there?"
        mock_msg.author.id = 111111
        mock_msg.reply = AsyncMock()

        await self.core.process_dm_message(mock_msg)
        mock_msg.reply.assert_awaited_once()
        self.assertIn("No recent tell sender", mock_msg.reply.call_args[0][0])

if __name__ == '__main__':
    unittest.main()
