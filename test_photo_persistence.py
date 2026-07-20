from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import db


class PhotoPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.previous_path = db.DB_PATH
        self.temp_dir = tempfile.TemporaryDirectory()
        db.DB_PATH = Path(self.temp_dir.name) / "photos.db"
        db.init_db()
        db.upsert_accounts(
            [
                {"id": 10, "name": "Compte A", "network": "threads", "username": "compte_a"},
                {"id": 20, "name": "Compte B", "network": "threads", "username": "compte_b"},
            ]
        )

    def tearDown(self) -> None:
        db.DB_PATH = self.previous_path
        self.temp_dir.cleanup()

    def test_account_moves_between_exclusive_photo_groups(self) -> None:
        first_group = db.upsert_photo_group("Lifestyle")
        second_group = db.upsert_photo_group("Studio")
        db.set_photo_group_accounts(first_group, {10: 2, 20: 1})
        moved = db.set_photo_group_accounts(second_group, {10: 3})

        memberships = db.photo_account_memberships()
        self.assertEqual(second_group, int(memberships[10]["group_id"]))
        self.assertEqual(first_group, int(memberships[20]["group_id"]))
        self.assertEqual([10], [int(item["account_id"]) for item in moved])

    def test_marking_assignment_used_is_idempotent(self) -> None:
        group_id = db.upsert_photo_group("Lifestyle")
        asset_id = db.add_photo_asset(
            "Lifestyle", "photo.jpg", "media-1", "image/jpeg", b"image", media_status="ready"
        )
        asset = db.get_photo_asset(asset_id)
        with db.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO scheduled_posts
                (library_post_id, caption_hash, caption, account_id, account_name,
                 scheduled_time_local, scheduled_time_utc, media_ids, local_photo_asset_ids,
                 content_type, status, photo_asset_id, photo_group_id, photo_assignment_state)
                VALUES (1, 'hash', 'Texte', 10, 'Compte A', '2026-07-20 10:00:00',
                        '2026-07-20T08:00:00Z', '[\"media-1\"]', ?, 'image', 'scheduled', ?, ?, 'reserved')
                """,
                (f"[{asset_id}]", asset_id, group_id),
            )
            scheduled_id = int(cursor.lastrowid)

        db.mark_photo_assignment_used(scheduled_id)
        db.mark_photo_assignment_used(scheduled_id)

        refreshed = db.get_photo_asset(asset_id)
        group = next(item for item in db.list_photo_groups() if int(item["id"]) == group_id)
        self.assertEqual(1, int(refreshed["usage_count"]))
        self.assertEqual([str(asset["asset_key"])], group["rotation_used"])

    def test_remote_configuration_round_trip_keeps_photo_metadata(self) -> None:
        group_id = db.upsert_photo_group(
            "Lifestyle", quota_mode="per_account", global_quota=4, spacing_percent=25
        )
        db.set_photo_group_accounts(group_id, {10: 3})
        asset_id = db.add_photo_asset(
            "Lifestyle", "photo.jpg", "media-1", "image/jpeg", b"image", media_status="ready"
        )
        db.update_photo_asset(asset_id, storage_path="workspace/key.jpg")
        asset_key = str(db.get_photo_asset(asset_id)["asset_key"])
        db.save_photo_rotation(group_id, [asset_key], [])
        snapshot = db.export_group_configuration()

        with db.connect() as conn:
            conn.execute("DELETE FROM photo_group_accounts")
            conn.execute("DELETE FROM photo_assets")
            conn.execute("DELETE FROM photo_groups")
        db.apply_group_configuration(snapshot)

        restored_group = next(item for item in db.list_photo_groups() if item["name"] == "Lifestyle")
        restored_asset = db.get_photo_asset_by_key(asset_key)
        membership = db.photo_account_memberships()[10]
        self.assertEqual("per_account", restored_group["quota_mode"])
        self.assertEqual(25, int(restored_group["spacing_percent"]))
        self.assertEqual("workspace/key.jpg", restored_asset["storage_path"])
        self.assertEqual("Lifestyle", membership["group_name"])


if __name__ == "__main__":
    unittest.main()
