from __future__ import annotations

import random
import re
from datetime import datetime, date, time, timedelta
from zoneinfo import ZoneInfo
from utils import caption_hash


VAR_PATTERN = re.compile(r"\{([a-zA-Z0-9_]+)\}")


def render_variables(text: str, variables: dict) -> str:
    def replace(match: re.Match) -> str:
        key = match.group(1)
        value = variables.get(key)
        return str(value) if value is not None else match.group(0)

    return VAR_PATTERN.sub(replace, str(text or ""))


def account_variables(account: dict, group_name: str) -> dict:
    username = str(account.get("username") or account.get("name") or "").lstrip("@")
    return {
        "account_id": account.get("id"),
        "account_name": account.get("name", ""),
        "name": account.get("name", ""),
        "username": username,
        "group": group_name,
        "group_name": group_name,
    }


def balanced_random_offsets(
    count: int,
    available_minutes: int,
    min_interval_minutes: int,
    rng: random.Random,
    global_minute_load: dict[int, int],
) -> list[int]:
    # Treat each post as reserving a full interval inside the active window.
    # This avoids the common off-by-one where 8 intervals become 9 posts.
    required = count * min_interval_minutes
    if available_minutes < required:
        raise ValueError(
            f"Fenêtre trop courte: {available_minutes}min disponibles pour {count} posts "
            f"avec {min_interval_minutes}min d'écart."
        )
    slack = available_minutes - required
    if count == 1:
        candidates = range(0, slack + 1)
        return [min(candidates, key=lambda minute: (global_minute_load.get(minute, 0), rng.random()))]

    best_offsets: list[int] | None = None
    best_score: tuple[int, int, float] | None = None
    attempts = max(80, count * 20)
    bucket_count = count + 1
    for _ in range(attempts):
        slack_buckets = [0] * bucket_count
        for _ in range(slack):
            slack_buckets[rng.randrange(bucket_count)] += 1

        offsets: list[int] = []
        cursor = slack_buckets[0]
        for slot in range(count):
            offsets.append(cursor)
            cursor += min_interval_minutes
            if slot + 1 < count:
                cursor += slack_buckets[slot + 1]

        load_score = sum(global_minute_load.get(minute, 0) for minute in offsets)
        peak_score = max((global_minute_load.get(minute, 0) for minute in offsets), default=0)
        regularity_score = max(
            abs((offsets[i + 1] - offsets[i]) - min_interval_minutes)
            for i in range(len(offsets) - 1)
        )
        score = (peak_score, load_score, -regularity_score, rng.random())
        if best_score is None or score < best_score:
            best_score = score
            best_offsets = offsets

    return best_offsets or [slot * min_interval_minutes for slot in range(count)]

def generate_schedule(
    selected_posts: list[dict],
    grouped_accounts: dict[str, dict],
    publish_date: date,
    start_time: time,
    end_time: time,
    publish_end_date: date | None = None,
    posts_per_account: int = 17,
    posts_per_account_max: int | None = None,
    min_interval_minutes: int = 75,
    same_caption_margin_minutes: int = 60,
    tz_name: str = "Europe/Brussels",
    randomize_captions: bool = False,
    randomize_times: bool = True,
    media_library: dict[str, list[str]] | None = None,
    schedule_seed: int | None = None,
) -> list[dict]:
    min_posts = int(posts_per_account)
    max_posts = int(posts_per_account if posts_per_account_max is None else posts_per_account_max)
    if min_posts < 0 or max_posts < min_posts:
        raise ValueError("Le nombre de posts par compte est invalide.")
    if max_posts == 0:
        return []
    if not selected_posts:
        raise ValueError("Aucun post sélectionné.")

    tz = ZoneInfo(tz_name)
    start_dt = datetime.combine(publish_date, start_time).replace(tzinfo=tz)
    end_dt = datetime.combine(publish_end_date or publish_date, end_time).replace(tzinfo=tz)
    if end_dt <= start_dt:
        raise ValueError("La fin du planning doit être après son début.")

    required_minutes = max_posts * min_interval_minutes
    available_minutes = int((end_dt - start_dt).total_seconds() / 60)
    if available_minutes < required_minutes:
        raise ValueError(
            f"Timeframe trop court. Il faut au moins {required_minutes} minutes "
            f"pour {max_posts} posts avec {min_interval_minutes} min d'écart."
        )

    rows: list[dict] = []
    caption_times: dict[str, list[datetime]] = {}
    global_minute_load: dict[int, int] = {}
    post_count = len(selected_posts)
    account_serial = 0
    rng = random.Random(schedule_seed or int(datetime.now().timestamp()))
    media_library = media_library or {}

    for group_name, group in grouped_accounts.items():
        accounts = group.get("accounts", [])

        for account_index, account in enumerate(accounts):
            account_used_hashes: set[str] = set()
            allow_account_reuse = post_count < max_posts
            account_start = start_dt
            account_post_count = min_posts if min_posts == max_posts else rng.randint(min_posts, max_posts)
            account_posts = list(selected_posts)
            if randomize_captions:
                rng.shuffle(account_posts)
            account_available = int((end_dt - account_start).total_seconds() / 60)
            account_required = account_post_count * min_interval_minutes
            if account_available < account_required:
                raise ValueError(
                    f"Impossible de placer {account_post_count} posts pour {account.get('name')} "
                    f"avec {min_interval_minutes} min d'écart dans cette plage."
                )
            if randomize_times and account_post_count > 1:
                minute_offsets = balanced_random_offsets(
                    account_post_count,
                    account_available,
                    min_interval_minutes,
                    rng,
                    global_minute_load,
                )
            else:
                minute_offsets = [slot * min_interval_minutes for slot in range(account_post_count)]

            account_second_offset = rng.randint(7, 53)
            for slot in range(account_post_count):
                scheduled_at = account_start + timedelta(minutes=minute_offsets[slot], seconds=account_second_offset)
                if scheduled_at > end_dt:
                    raise ValueError(f"Impossible de placer tous les posts pour {account.get('name')} dans le timeframe.")

                chosen = None
                for attempt in range(post_count):
                    idx = (slot + account_serial * 7 + attempt) % post_count
                    post = account_posts[idx]
                    h = post.get("caption_hash") or caption_hash(post["caption"])
                    if h in account_used_hashes and not allow_account_reuse:
                        continue
                    if allow_account_reuse and len(account_used_hashes) < post_count and h in account_used_hashes:
                        continue
                    too_close = any(
                        abs((scheduled_at - previous).total_seconds()) < same_caption_margin_minutes * 60
                        for previous in caption_times.get(h, [])
                    ) if same_caption_margin_minutes > 0 else False
                    if too_close:
                        continue
                    chosen = {**post, "caption_hash": h}
                    break

                if chosen is None:
                    raise ValueError(
                        f"Pas assez de captions pour respecter la règle anti-répétition de {same_caption_margin_minutes} minutes. "
                        "Ajoute plus de posts, réduis le nombre de comptes/posts, ou augmente le timeframe."
                    )

                h = chosen["caption_hash"]
                account_used_hashes.add(h)
                caption_times.setdefault(h, []).append(scheduled_at)
                global_minute_load[minute_offsets[slot]] = global_minute_load.get(minute_offsets[slot], 0) + 1
                utc_dt = scheduled_at.astimezone(ZoneInfo("UTC"))
                media_ids = chosen.get("media_ids") or []
                media_folder = str(chosen.get("media_folder") or "").strip()
                if not media_ids and media_folder and media_library.get(media_folder):
                    media_ids = [rng.choice(media_library[media_folder])]
                variables = {
                    **account_variables(account, group_name),
                    **(chosen.get("variables") or {}),
                }
                rendered_caption = render_variables(chosen["caption"], variables)
                chain_replies = [
                    render_variables(reply, variables)
                    for reply in (chosen.get("reply_chain") or [])
                ]

                rows.append({
                    "library_post_id": chosen["id"],
                    "caption_hash": h,
                    "caption": rendered_caption,
                    "media_ids": media_ids,
                    "content_type": "image" if media_ids else "text",
                    "variables": variables,
                    "chain_replies": chain_replies,
                    "account_id": int(account["id"]),
                    "account_name": account.get("name", str(account["id"])),
                    "group_name": group_name,
                    "scheduled_time_local": scheduled_at.strftime("%Y-%m-%d %H:%M:%S %Z"),
                    "scheduled_time_utc": utc_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                })

            account_serial += 1

    counts_by_account: dict[int, int] = {}
    times_by_account: dict[int, list[datetime]] = {}
    for row in rows:
        account_id = int(row["account_id"])
        counts_by_account[account_id] = counts_by_account.get(account_id, 0) + 1
        local_dt = datetime.strptime(row["scheduled_time_local"][:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=tz)
        times_by_account.setdefault(account_id, []).append(local_dt)

    overflow = {account_id: count for account_id, count in counts_by_account.items() if count > max_posts}
    if overflow:
        details = ", ".join(f"{account_id}: {count}" for account_id, count in sorted(overflow.items()))
        raise ValueError(f"Limite max dépassée par compte ({max_posts}): {details}.")

    for account_id, account_times in times_by_account.items():
        ordered = sorted(account_times)
        for previous, current in zip(ordered, ordered[1:]):
            gap = int((current - previous).total_seconds() / 60)
            if gap < min_interval_minutes:
                raise ValueError(
                    f"Écart trop court pour le compte {account_id}: {gap}min au lieu de {min_interval_minutes}min."
                )

    return sorted(rows, key=lambda r: r["scheduled_time_utc"])
