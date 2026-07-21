from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import db
from postoria_client import PostoriaClient
from utils import caption_hash


class ReuseScheduledPostsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.previous_path = db.DB_PATH
        self.temp_dir = tempfile.TemporaryDirectory()
        db.DB_PATH = Path(self.temp_dir.name) / "reuse.db"
        db.init_db()

    def tearDown(self) -> None:
        db.DB_PATH = self.previous_path
        self.temp_dir.cleanup()

    def add_scheduled(
        self,
        caption: str,
        status: str,
        scheduled_utc: str,
        postoria_post_id: int | None,
    ) -> None:
        with db.connect() as conn:
            conn.execute(
                """
                INSERT INTO scheduled_posts
                (library_post_id, caption_hash, caption, account_id, account_name,
                 scheduled_time_local, scheduled_time_utc, status, postoria_post_id)
                VALUES (1, ?, ?, 10, 'Compte', '2099-07-22 10:00:00', ?, ?, ?)
                """,
                (caption_hash(caption), caption, scheduled_utc, status, postoria_post_id),
            )

    def test_returns_each_future_accepted_caption_once(self) -> None:
        self.add_scheduled("Texte A", "scheduled", "2099-07-22T08:00:00Z", 100)
        self.add_scheduled("Texte A", "scheduled", "2099-07-23T08:00:00Z", 101)
        self.add_scheduled("Texte failed", "failed", "2099-07-22T09:00:00Z", None)
        self.add_scheduled("Texte preview", "preview", "2099-07-22T10:00:00Z", None)
        self.add_scheduled("Texte ancien", "published", "2000-07-22T08:00:00Z", 102)

        future = db.list_reusable_scheduled_posts(future_only=True)
        history = db.list_reusable_scheduled_posts(future_only=False)

        self.assertEqual(["Texte A"], [row["caption"] for row in future])
        self.assertEqual({"Texte A", "Texte ancien"}, {row["caption"] for row in history})

    def test_reactivated_posts_can_be_selected_again(self) -> None:
        _, _, post_ids = db.add_posts_with_ids(["Texte A"])
        db.set_posts_active(post_ids, False)

        changed = db.set_posts_active(post_ids, True)
        active = {int(post["id"]) for post in db.list_posts(active_only=True)}

        self.assertEqual(1, changed)
        self.assertEqual(set(post_ids), active)

    def test_postoria_list_posts_follows_cursor_with_a_limit(self) -> None:
        client = PostoriaClient(api_key="test", base_url="https://example.test/v1")
        calls: list[dict] = []

        def fake_request(method: str, path: str, **kwargs):
            calls.append(kwargs.get("params", {}))
            if len(calls) == 1:
                return {
                    "data": [{"id": 1}, {"id": 2}],
                    "pagination": {"has_more": True, "next_cursor": "next"},
                }
            return {
                "data": [{"id": 3}, {"id": 4}],
                "pagination": {"has_more": False, "next_cursor": ""},
            }

        client._request = fake_request
        rows = client.list_posts(10, max_items=3)

        self.assertEqual([1, 2, 3], [row["id"] for row in rows])
        self.assertEqual("next", calls[1]["cursor"])


if __name__ == "__main__":
    unittest.main()
