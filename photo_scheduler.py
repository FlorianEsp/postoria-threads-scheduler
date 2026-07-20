from __future__ import annotations

import math
import random
from collections import defaultdict
from typing import Any


def spaced_post_indices(
    total_posts: int,
    requested: int,
    spacing_percent: int,
    rng: random.Random,
) -> list[int]:
    """Pick random, well-spaced post positions without placing photos back-to-back."""
    total = max(0, int(total_posts))
    wanted = max(0, int(requested))
    if not total or not wanted:
        return []

    min_distance = max(2, math.ceil(total * max(0, int(spacing_percent)) / 100))
    target = min(wanted, 1 + (total - 1) // min_distance)
    best: list[int] = []

    for _ in range(max(80, total * 12)):
        candidates = list(range(total))
        rng.shuffle(candidates)
        picked: list[int] = []
        for candidate in candidates:
            if all(abs(candidate - previous) >= min_distance for previous in picked):
                picked.append(candidate)
                if len(picked) == target:
                    break
        picked.sort()
        if len(picked) > len(best):
            best = picked
        if len(best) == target:
            break

    if len(best) < target:
        best = list(range(0, total, min_distance))[:target]
    return best


def _reconciled_rotation(
    assets: list[dict[str, Any]],
    configured_order: list[str],
    used_keys: list[str],
    rng: random.Random,
) -> tuple[list[str], list[str]]:
    active_keys = [str(asset["asset_key"]) for asset in assets]
    active_set = set(active_keys)
    order = [key for key in configured_order if key in active_set]
    missing = [key for key in active_keys if key not in order]
    rng.shuffle(missing)
    order.extend(missing)

    used = [key for key in used_keys if key in active_set]
    if active_set and active_set.issubset(set(used)):
        used = []
        rng.shuffle(order)
    return order, used


def assign_photos_to_schedule(
    rows: list[dict[str, Any]],
    group_configs: list[dict[str, Any]],
    reserved_asset_keys: set[str] | None = None,
    seed: int | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[int, tuple[list[str], list[str]]]]:
    """Reserve ready photos for eligible account/day rows using a group-wide deck."""
    rng = random.Random(seed)
    output = [dict(row) for row in rows]
    reserved = {str(key) for key in (reserved_asset_keys or set())}
    reports: list[dict[str, Any]] = []
    rotation_updates: dict[int, tuple[list[str], list[str]]] = {}

    rows_by_account_day: dict[tuple[int, str], list[int]] = defaultdict(list)
    for index, row in enumerate(output):
        account_id = int(row.get("account_id") or 0)
        day = str(row.get("scheduled_time_local") or "")[:10]
        rows_by_account_day[(account_id, day)].append(index)

    for group in group_configs:
        group_id = int(group["id"])
        group_name = str(group.get("name") or "Groupe photo")
        account_quotas = {
            int(account["account_id"]): int(
                group.get("global_quota", 0)
                if str(group.get("quota_mode") or "global") == "global"
                else account.get("quota_override", 0)
            )
            for account in group.get("accounts", [])
        }
        ready_assets = [
            asset
            for asset in group.get("assets", [])
            if str(asset.get("media_status") or "") == "ready" and str(asset.get("media_id") or "").strip()
        ]
        order, used = _reconciled_rotation(
            ready_assets,
            [str(key) for key in group.get("rotation_order", [])],
            [str(key) for key in group.get("rotation_used", [])],
            rng,
        )
        rotation_updates[group_id] = (order, used)
        assets_by_key = {str(asset["asset_key"]): asset for asset in ready_assets}
        available_keys = [key for key in order if key not in set(used) and key not in reserved]

        candidate_slots: list[tuple[int, int, str]] = []
        group_requested = 0
        spacing_percent = int(group.get("spacing_percent") or 0)
        for (account_id, day), row_indices in rows_by_account_day.items():
            if account_id not in account_quotas:
                continue
            quota = max(0, account_quotas[account_id])
            if not quota:
                continue
            eligible_indices = [
                index for index in sorted(row_indices, key=lambda item: str(output[item].get("scheduled_time_local") or ""))
                if not output[index].get("media_ids") and not output[index].get("local_photo_asset_ids")
            ]
            picked_positions = spaced_post_indices(len(eligible_indices), quota, spacing_percent, rng)
            group_requested += quota
            candidate_slots.extend((eligible_indices[position], account_id, day) for position in picked_positions)
            if len(picked_positions) < quota:
                reports.append(
                    {
                        "group_id": group_id,
                        "group_name": group_name,
                        "account_id": account_id,
                        "day": day,
                        "requested": quota,
                        "assigned": len(picked_positions),
                        "reason": "espacement ou nombre de posts insuffisant",
                    }
                )

        rng.shuffle(candidate_slots)
        assigned_by_account_day: dict[tuple[int, str], int] = defaultdict(int)
        for row_index, account_id, day in candidate_slots:
            if not available_keys:
                break
            asset_key = available_keys.pop(0)
            asset = assets_by_key[asset_key]
            output[row_index].update(
                {
                    "media_ids": [str(asset["media_id"])],
                    "local_photo_asset_ids": [int(asset["id"])],
                    "content_type": "image",
                    "photo_asset_id": int(asset["id"]),
                    "photo_group_id": group_id,
                    "photo_assignment_state": "reserved",
                }
            )
            reserved.add(asset_key)
            assigned_by_account_day[(account_id, day)] += 1

        if len(candidate_slots) > sum(assigned_by_account_day.values()):
            missing = len(candidate_slots) - sum(assigned_by_account_day.values())
            reports.append(
                {
                    "group_id": group_id,
                    "group_name": group_name,
                    "account_id": None,
                    "day": "",
                    "requested": group_requested,
                    "assigned": sum(assigned_by_account_day.values()),
                    "reason": f"{missing} photo(s) non placée(s): rotation prête épuisée",
                }
            )

    return output, reports, rotation_updates
