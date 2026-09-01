import unittest
from datetime import date
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
            await self.store.record_response(800, 1, "Rz", "yes", None)

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
                                "Responded At": "2026-09-01T05:00:00Z",
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


if __name__ == "__main__":
    unittest.main()
