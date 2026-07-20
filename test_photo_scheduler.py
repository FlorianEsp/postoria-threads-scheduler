from __future__ import annotations

import unittest

from photo_scheduler import assign_photos_to_schedule, spaced_post_indices


class PhotoSchedulerTests(unittest.TestCase):
    @staticmethod
    def ready_assets(count: int) -> list[dict]:
        return [
            {
                "id": index,
                "asset_key": f"asset-{index}",
                "media_id": f"media-{index}",
                "media_status": "ready",
            }
            for index in range(1, count + 1)
        ]

    def test_spacing_never_places_consecutive_photos(self) -> None:
        picked = spaced_post_indices(10, 4, 20, __import__("random").Random(7))
        self.assertEqual(4, len(picked))
        self.assertTrue(all(right - left >= 2 for left, right in zip(picked, picked[1:])))

    def test_group_rotation_does_not_repeat_reserved_asset(self) -> None:
        rows = [
            {
                "account_id": 10,
                "scheduled_time_local": f"2026-07-20 {hour:02d}:00:00",
                "media_ids": [],
                "local_photo_asset_ids": [],
            }
            for hour in range(8, 14)
        ]
        config = {
            "id": 1,
            "name": "Lifestyle",
            "quota_mode": "global",
            "global_quota": 3,
            "spacing_percent": 10,
            "accounts": [{"account_id": 10, "quota_override": 0}],
            "assets": [
                {"id": 1, "asset_key": "a", "media_id": "101", "media_status": "ready"},
                {"id": 2, "asset_key": "b", "media_id": "102", "media_status": "ready"},
                {"id": 3, "asset_key": "c", "media_id": "103", "media_status": "ready"},
            ],
            "rotation_order": ["a", "b", "c"],
            "rotation_used": [],
        }
        assigned, reports, _ = assign_photos_to_schedule(rows, [config], {"a"}, seed=4)
        media = [row["media_ids"][0] for row in assigned if row.get("media_ids")]
        self.assertEqual(2, len(media))
        self.assertEqual(len(media), len(set(media)))
        self.assertTrue(reports)

    def test_global_quota_repeats_for_each_day(self) -> None:
        rows = []
        for day in (20, 21):
            rows.extend(
                {
                    "account_id": 10,
                    "scheduled_time_local": f"2026-07-{day:02d} {hour:02d}:00:00",
                    "media_ids": [],
                    "local_photo_asset_ids": [],
                }
                for hour in range(8, 14)
            )
        assets = self.ready_assets(4)
        config = {
            "id": 1,
            "name": "Lifestyle",
            "quota_mode": "global",
            "global_quota": 2,
            "spacing_percent": 10,
            "accounts": [{"account_id": 10, "quota_override": 0}],
            "assets": assets,
            "rotation_order": [asset["asset_key"] for asset in assets],
            "rotation_used": [],
        }
        assigned, reports, _ = assign_photos_to_schedule(rows, [config], seed=12)
        by_day = {}
        for row in assigned:
            if row.get("photo_asset_id"):
                day = row["scheduled_time_local"][:10]
                by_day[day] = by_day.get(day, 0) + 1
        self.assertEqual({"2026-07-20": 2, "2026-07-21": 2}, by_day)
        self.assertFalse(reports)

    def test_per_account_zero_quota_excludes_account(self) -> None:
        rows = [
            {
                "account_id": account_id,
                "scheduled_time_local": f"2026-07-20 {hour:02d}:00:00",
                "media_ids": [],
                "local_photo_asset_ids": [],
            }
            for account_id in (10, 20)
            for hour in range(8, 14)
        ]
        assets = self.ready_assets(2)
        config = {
            "id": 1,
            "name": "Lifestyle",
            "quota_mode": "per_account",
            "global_quota": 8,
            "spacing_percent": 10,
            "accounts": [
                {"account_id": 10, "quota_override": 2},
                {"account_id": 20, "quota_override": 0},
            ],
            "assets": assets,
            "rotation_order": [asset["asset_key"] for asset in assets],
            "rotation_used": [],
        }
        assigned, _, _ = assign_photos_to_schedule(rows, [config], seed=8)
        assigned_accounts = [row["account_id"] for row in assigned if row.get("photo_asset_id")]
        self.assertEqual([10, 10], assigned_accounts)


if __name__ == "__main__":
    unittest.main()
