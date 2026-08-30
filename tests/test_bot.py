import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from aiohttp import web


_clip_dir = tempfile.TemporaryDirectory()
os.environ["DISCORD_TOKEN"] = "test-token"
os.environ["CLIP_DIR"] = _clip_dir.name

import bot


class ClipSourceTests(unittest.IsolatedAsyncioTestCase):
    def test_setclip_exposes_optional_attachment_and_url(self):
        command = bot.bot.tree.get_command("setclip")
        parameters = {parameter.name: parameter for parameter in command.parameters}

        self.assertEqual({"member", "audio", "url"}, set(parameters))
        self.assertTrue(parameters["member"].required)
        self.assertFalse(parameters["audio"].required)
        self.assertFalse(parameters["url"].required)

    def test_extension_can_come_from_url_or_content_type(self):
        self.assertEqual(
            ".mp3",
            bot._extension_from_url_or_type(
                "https://cdn.example/name.MP3?signature=abc", "application/octet-stream"),
        )
        self.assertEqual(
            ".ogg",
            bot._extension_from_url_or_type(
                "https://cdn.example/download?id=1", "audio/ogg; charset=binary"),
        )
        self.assertIsNone(
            bot._extension_from_url_or_type(
                "https://example.com/watch?v=1", "text/html"),
        )

    async def test_private_network_url_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "private or local"):
            await bot._validate_public_url("http://127.0.0.1/name.mp3")

    async def test_direct_audio_url_is_downloaded(self):
        async def audio_response(_request):
            return web.Response(body=b"fake-mp3-data", content_type="audio/mpeg")

        app = web.Application()
        app.router.add_get("/clip", audio_response)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]

        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                temp_path = os.path.join(temp_dir, "download.tmp")
                with patch.object(bot, "_validate_public_url", new=AsyncMock()):
                    ext = await bot._download_url_clip(
                        f"http://127.0.0.1:{port}/clip", temp_path)

                self.assertEqual(".mp3", ext)
                with open(temp_path, "rb") as downloaded:
                    self.assertEqual(b"fake-mp3-data", downloaded.read())
        finally:
            await runner.cleanup()

    def test_install_replaces_an_older_format(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            old_path = os.path.join(temp_dir, "123.wav")
            temp_path = os.path.join(temp_dir, ".123.new.tmp")
            with open(old_path, "wb") as old_file:
                old_file.write(b"old")
            with open(temp_path, "wb") as new_file:
                new_file.write(b"new")

            with patch.object(bot, "CLIP_DIR", temp_dir):
                bot._install_clip(temp_path, 123, ".mp3")

            self.assertFalse(os.path.exists(old_path))
            self.assertFalse(os.path.exists(temp_path))
            with open(os.path.join(temp_dir, "123.mp3"), "rb") as installed:
                self.assertEqual(b"new", installed.read())


if __name__ == "__main__":
    unittest.main()
