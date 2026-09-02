import unittest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock

from airtable_store import AirtablePollStore, PollClosedError


class AirtablePollStoreTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = AirtablePollStore("test-token", "appTest")

    def test_formula_escapes_quotes_and_backslashes(self):
        formula = self.store._formula_equals("Poll Key", "a\\b'c")
        self.assertEqual("{Poll Key}='a\\\\b\\'c'", formula)

    async def test_create_poll_uses_text_ids_and_stable_key(self):
        self.store.get_poll = AsyncMock(return_value=None)
        self.store._create = AsyncMock(
            return_value={
                "id": "recPoll",
                "fields": {
                    "Guild ID": "99",
                    "Channel ID": "456",
                    "Poll Date": "2026-09-01",
                    "Question": "Does anyone want to play any game tonight?",
                    "Status": "open",
                },
            }
        )

        poll = await self.store.create_poll(99, 456, date(2026, 9, 1))

        self.assertEqual("recPoll", poll["id"])
        fields = self.store._create.await_args.args[1]
        self.assertEqual("456:2026-09-01", fields["Poll Key"])
        self.assertEqual("99", fields["Guild ID"])
        self.assertEqual("456", fields["Channel ID"])

    async def test_no_vote_requires_reason(self):
        with self.assertRaisesRegex(ValueError, "reason is required"):
            await self.store.record_response(800, 1, "Rz", "no", "  ")

    async def test_closed_poll_rejects_response(self):
        self.store.get_poll_by_message = AsyncMock(
            return_value={"id": "recPoll", "status": "closed"}
        )
        with self.assertRaises(PollClosedError):
            await self.store.record_response(800, 1, "Rz", "yes", None, "20:00")

    async def test_yes_vote_requires_a_valid_play_time(self):
        with self.assertRaisesRegex(ValueError, "valid play time"):
            await self.store.record_response(800, 1, "Rz", "yes", None)

    async def test_yes_vote_starts_as_not_joined(self):
        self.store.get_poll_by_message = AsyncMock(
            return_value={"id": "recPoll", "status": "open"}
        )
        self.store._find_one = AsyncMock(return_value=None)
        self.store._create = AsyncMock(return_value={"id": "recResponse"})

        poll_id = await self.store.record_response(
            800, 1, "Rz", "yes", None, "20:00"
        )

        self.assertEqual("recPoll", poll_id)
        fields = self.store._create.await_args.args[1]
        self.assertEqual("yes", fields["Choice"])
        self.assertEqual("20:00", fields["Play Time"])
        self.assertEqual("no", fields["Joined Voice Chat"])
        self.assertEqual("", fields["Joined At"])

    async def test_first_voice_join_is_saved_for_yes_voter(self):
        self.store._find_one = AsyncMock(
            return_value={
                "id": "recResponse",
                "fields": {"Choice": "yes", "Joined Voice Chat": "no"},
            }
        )
        self.store._update = AsyncMock()

        changed = await self.store.mark_yes_voice_join("recPoll", 1, 555, "Game Room")

        self.assertTrue(changed)
        fields = self.store._update.await_args.args[2]
        self.assertEqual("yes", fields["Joined Voice Chat"])
        self.assertEqual("555", fields["Voice Channel ID"])
        self.assertEqual("Game Room", fields["Voice Channel Name"])
        self.assertTrue(fields["Joined At"])

    async def test_voice_join_is_ignored_for_non_yes_voter(self):
        self.store._find_one = AsyncMock(
            return_value={"id": "recResponse", "fields": {"Choice": "maybe"}}
        )
        self.store._update = AsyncMock()

        changed = await self.store.mark_yes_voice_join("recPoll", 1, 555, "Game Room")

        self.assertFalse(changed)
        self.store._update.assert_not_awaited()

    async def test_response_pages_are_combined(self):
        self.store._request = AsyncMock(
            side_effect=[
                {
                    "records": [
                        {
                            "fields": {
                                "User ID": "1",
                                "Display Name": "Rz",
                                "Choice": "yes",
                                "Play Time": "20:00",
                                "Responded At": "2026-09-01T05:00:00Z",
                                "Joined Voice Chat": "yes",
                                "Joined At": "2026-09-01T06:00:00Z",
                                "Voice Channel ID": "555",
                                "Voice Channel Name": "Game Room",
                            }
                        }
                    ],
                    "offset": "next-page",
                },
                {
                    "records": [
                        {
                            "fields": {
                                "User ID": "2",
                                "Display Name": "Teemo",
                                "Choice": "no",
                                "Reason": "Working",
                                "Responded At": "2026-09-01T05:01:00Z",
                            }
                        }
                    ]
                },
            ]
        )

        responses = await self.store.get_responses("recPoll")

        self.assertEqual(["Rz", "Teemo"], [row["display_name"] for row in responses])
        self.assertTrue(responses[0]["joined_voice_chat"])
        self.assertEqual("20:00", responses[0]["play_time"])
        self.assertEqual("Game Room", responses[0]["voice_channel_name"])
        self.assertFalse(responses[1]["joined_voice_chat"])
        second_params = self.store._request.await_args_list[1].kwargs["params"]
        self.assertEqual("next-page", second_params["offset"])

    async def test_save_report_counts_and_serializes_no_reasons(self):
        responses = [
            {"user_id": 1, "display_name": "Rz", "choice": "yes", "reason": None},
            {
                "user_id": 2,
                "display_name": "Teemo",
                "choice": "no",
                "reason": "Working late",
            },
        ]
        self.store._find_one = AsyncMock(return_value=None)
        self.store._create = AsyncMock(
            side_effect=lambda _table, fields: {"id": "recReport", "fields": fields}
        )

        report = await self.store.save_report("recPoll", responses)

        self.assertEqual(1, report["yes_count"])
        self.assertEqual(1, report["no_count"])
        self.assertEqual("Working late", report["no_reasons"][0]["reason"])

    async def test_voice_session_records_join_leave_and_duration(self):
        joined_at = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
        active_record = {
            "id": "recVoice",
            "fields": {
                "Active Key": "99:1",
                "Voice Channel ID": "555",
                "Joined At": joined_at.isoformat(),
            },
        }
        self.store._find_one = AsyncMock(side_effect=[None, active_record])
        self.store._create = AsyncMock(return_value={"id": "recVoice"})
        self.store._update = AsyncMock()

        record_id = await self.store.start_voice_session(
            99,
            1,
            "Rz",
            555,
            "Game Room",
            date(2026, 9, 3),
            joined_at=joined_at,
        )
        duration = await self.store.stop_voice_session(
            99, 1, left_at=joined_at + timedelta(minutes=42)
        )

        self.assertEqual("recVoice", record_id)
        created_fields = self.store._create.await_args.args[1]
        self.assertEqual("99:1", created_fields["Active Key"])
        self.assertEqual("555", created_fields["Voice Channel ID"])
        self.assertEqual("2026-09-03", created_fields["Session Date"])
        self.assertEqual(2520, duration)
        closed_fields = self.store._update.await_args.args[2]
        self.assertEqual("closed", closed_fields["Status"])
        self.assertEqual("", closed_fields["Active Key"])
        self.assertEqual(2520, closed_fields["Duration Seconds"])

    async def test_duplicate_voice_join_reuses_active_session(self):
        self.store._find_one = AsyncMock(
            return_value={
                "id": "recVoice",
                "fields": {"Voice Channel ID": "555"},
            }
        )
        self.store._create = AsyncMock()
        self.store._update = AsyncMock()

        record_id = await self.store.start_voice_session(
            99, 1, "Rz", 555, "Game Room", date(2026, 9, 3)
        )

        self.assertEqual("recVoice", record_id)
        self.store._create.assert_not_awaited()
        self.store._update.assert_not_awaited()

    async def test_voice_session_reconciliation_closes_stale_and_starts_missing(self):
        self.store._get_active_voice_sessions = AsyncMock(
            return_value=[
                {
                    "id": "recKeep",
                    "fields": {
                        "Active Key": "99:1",
                        "Voice Channel ID": "555",
                    },
                },
                {
                    "id": "recClose",
                    "fields": {
                        "Active Key": "99:2",
                        "Voice Channel ID": "777",
                    },
                },
            ]
        )
        self.store._close_voice_session_record = AsyncMock(return_value=60)
        self.store._create = AsyncMock(return_value={"id": "recNew"})
        connected = [
            {
                "guild_id": 99,
                "user_id": 1,
                "display_name": "Rz",
                "voice_channel_id": 555,
                "voice_channel_name": "Lobby",
                "session_date": "2026-09-03",
            },
            {
                "guild_id": 99,
                "user_id": 3,
                "display_name": "Ahri",
                "voice_channel_id": 888,
                "voice_channel_name": "Ranked",
                "session_date": "2026-09-03",
            },
        ]

        started, closed = await self.store.reconcile_voice_sessions(connected)

        self.assertEqual((1, 1), (started, closed))
        self.store._close_voice_session_record.assert_awaited_once()
        created_fields = self.store._create.await_args.args[1]
        self.assertEqual("99:3", created_fields["Active Key"])
        self.assertEqual("888", created_fields["Voice Channel ID"])


if __name__ == "__main__":
    unittest.main()
