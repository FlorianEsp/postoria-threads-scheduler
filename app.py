from __future__ import annotations

import os
import base64
import hashlib
import io
import random
from datetime import date, datetime, time, timedelta
from html import escape
from math import floor
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

import db
from postoria_client import PostoriaClient
from scheduler import generate_schedule
from supabase_groups import GroupStoreError, SupabaseGroupStore

# streamlit-calendar crashes this local Python/Streamlit runtime with exit 139.
# Keep the scheduler stable and use the richer table preview instead.
calendar = None

load_dotenv()
db.init_db()

APP_TZ = os.getenv("APP_TIMEZONE", "Europe/Brussels")


def optional_secret(name: str) -> str:
    """Read a local env var or a Streamlit Cloud secret without exposing it in the UI."""
    try:
        value = st.secrets.get(name, os.getenv(name, ""))
    except Exception:
        value = os.getenv(name, "")
    return str(value or "").strip()


GROUP_STORE = SupabaseGroupStore(
    base_url=optional_secret("SUPABASE_URL"),
    service_key=optional_secret("SUPABASE_SERVICE_KEY"),
)


def mark_group_config_dirty() -> None:
    st.session_state["remote_group_config_dirty"] = True


def persist_group_config_if_needed(force: bool = False) -> bool:
    """Persist account groups only after the local SQLite changes are complete."""
    workspace_id = str(st.session_state.get("workspace_id") or "").strip()
    if not GROUP_STORE.configured or not workspace_id:
        return False
    if not force and not st.session_state.get("remote_group_config_dirty"):
        return False
    try:
        GROUP_STORE.save(workspace_id, db.export_group_configuration())
        st.session_state["remote_group_config_dirty"] = False
        st.session_state.pop("remote_group_config_error", None)
        return True
    except GroupStoreError as exc:
        st.session_state["remote_group_config_error"] = str(exc)
        return False


def media_ids_text(value) -> str:
    return ", ".join(db.parse_media_ids(value))


def threads_profile_url(username: str | None) -> str:
    raw = str(username or "").strip()
    if not raw:
        return ""
    if raw.startswith("http") and "threads." in raw:
        return raw
    clean = raw.strip().strip("/").lstrip("@")
    if "/" in clean:
        clean = clean.rsplit("/", 1)[-1].lstrip("@")
    if not clean or " " in clean:
        return ""
    return f"https://www.threads.com/@{clean}"


def account_threads_url(account: dict) -> str:
    account_url = str(account.get("url") or "").strip()
    if "threads." in account_url:
        return account_url
    return threads_profile_url(account.get("username") or account.get("name") or account.get("account_name"))


def attach_threads_urls(rows: list[dict]) -> list[dict]:
    account_map = {int(account["id"]): account for account in db.list_accounts()}
    enriched_rows = []
    for row in rows:
        account = account_map.get(int(row.get("account_id", 0) or 0), {})
        threads_url = account_threads_url(account) or threads_profile_url(row.get("account_name"))
        enriched_rows.append({**row, "threads_url": threads_url})
    return enriched_rows


def make_post_records(frame: pd.DataFrame) -> list[dict]:
    records: list[dict] = []
    text_column = next((c for c in ("text", "caption") if c in frame.columns), frame.columns[0] if len(frame.columns) else None)
    media_column = next((c for c in ("media_ids", "media_id", "photo_id", "photo_ids") if c in frame.columns), None)
    note_column = next((c for c in ("photo_note", "photo", "image", "media") if c in frame.columns), None)
    folder_column = next((c for c in ("media_folder", "folder", "media_bucket") if c in frame.columns), None)
    chain_columns = [c for c in frame.columns if c.startswith("reply_")]
    known = {
        "text", "caption", "media_ids", "media_id", "photo_id", "photo_ids",
        "photo_note", "photo", "image", "media", "media_folder", "folder", "media_bucket",
        *chain_columns,
    }
    for _, row in frame.iterrows():
        variables = {
            str(col): "" if pd.isna(row.get(col)) else row.get(col)
            for col in frame.columns
            if col not in known
        }
        reply_chain = [
            str(row.get(col)).strip()
            for col in chain_columns
            if str(row.get(col) or "").strip()
        ]
        records.append(
            {
                "caption": row.get(text_column, "") if text_column else "",
                "media_ids": row.get(media_column, "") if media_column else "",
                "photo_note": row.get(note_column, "") if note_column else "",
                "media_folder": row.get(folder_column, "") if folder_column else "",
                "variables": variables,
                "reply_chain": reply_chain,
            }
        )
    return records


def parse_post_csv(raw: bytes, file_name: str) -> list[dict]:
    """Read a small text-post CSV defensively before inserting it into the library."""
    max_bytes = 5 * 1024 * 1024
    max_rows = 5_000
    clean_name = str(file_name or "le CSV")
    if not raw:
        raise ValueError(f"{clean_name} est vide.")
    if len(raw) > max_bytes:
        raise ValueError(f"{clean_name} dépasse 5 Mo. Découpe-le en plusieurs CSV plus petits.")

    frame: pd.DataFrame | None = None
    parse_error: Exception | None = None
    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            frame = pd.read_csv(
                io.BytesIO(raw),
                encoding=encoding,
                dtype=object,
                keep_default_na=False,
            )
            break
        except (UnicodeDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as exc:
            parse_error = exc

    if frame is None:
        raise ValueError(f"Impossible de lire {clean_name}. Vérifie que le fichier est bien un CSV.") from parse_error
    if frame.empty:
        raise ValueError(f"{clean_name} ne contient aucun post.")
    if len(frame.index) > max_rows:
        raise ValueError(f"{clean_name} contient plus de {max_rows:,} lignes. Découpe-le avant l'import.")

    records = make_post_records(frame)
    if not any(str(record.get("caption") or "").strip() for record in records):
        raise ValueError(f"{clean_name} doit avoir une colonne text ou caption avec au moins un texte.")
    return records


def parse_variables_text(value: str) -> dict:
    data: dict[str, str] = {}
    for part in str(value or "").replace("\n", ",").split(","):
        if "=" not in part:
            continue
        key, raw = part.split("=", 1)
        key = key.strip()
        if key:
            data[key] = raw.strip()
    return data


def variables_text(value: dict | None) -> str:
    return ", ".join(f"{k}={v}" for k, v in (value or {}).items())


def settings() -> dict:
    defaults = {
        "publish_date": datetime.now(ZoneInfo(APP_TZ)).date(),
        "publish_end_date": datetime.now(ZoneInfo(APP_TZ)).date(),
        "start_time": time(4, 0),
        "end_time": time(23, 0),
        "count_mode": "Exact",
        "posts_min": 3,
        "posts_max": 3,
        "min_interval": 120,
        "avoid_same_text": False,
        "same_text_gap": 60,
        "caption_mode": "Rotate",
    }
    current = st.session_state.setdefault("settings", defaults.copy())
    for key, value in defaults.items():
        current.setdefault(key, value)
    return current


def account_label(account: dict) -> str:
    username = account.get("username")
    name = account.get("name") or str(account.get("id"))
    return f"@{username}" if username else name


def build_grouped_accounts(accounts: list[dict], edited: pd.DataFrame) -> dict[str, dict]:
    edited_by_id = {int(row["id"]): row for _, row in edited.iterrows()}
    grouped: dict[str, dict] = {}
    for account in accounts:
        row = edited_by_id.get(int(account["id"]))
        if row is None:
            continue
        group_name = str(row["group"] or "tous").strip()
        db.update_account_preferences(int(account["id"]), group_name, True, bool(row["use"]))
        if not bool(row["use"]):
            continue
        grouped.setdefault(group_name, {"accounts": []})
        grouped[group_name]["accounts"].append({**account, "group_name": group_name})
    return grouped


def restore_account_selection_from_db(accounts: list[dict]) -> None:
    if st.session_state.get("_accounts_restored_from_db"):
        return
    rows = []
    selected_groups: set[str] = set()
    account_states: list[tuple[int, str, bool, bool]] = []
    for account in accounts:
        account_id = int(account["id"])
        group_name = account.get("group_name") or "tous"
        active = True
        selected = bool(account.get("selected_for_schedule", False))
        account_states.append((account_id, group_name, active, selected))
        st.session_state.setdefault(f"account_group_{account_id}", group_name)
        st.session_state[f"account_status_enabled_v2_{account_id}"] = True
        st.session_state.setdefault(f"account_use_{account_id}", selected)
        if selected:
            selected_groups.add(group_name)
        rows.append(
            {
                "use": active and selected,
                "id": account_id,
                "compte": account_label(account),
                "group": group_name,
                "active": active,
                "url": account.get("url", ""),
            }
        )
    if selected_groups and not st.session_state.get("selected_group_filters"):
        st.session_state["selected_group_filters"] = sorted(selected_groups)
    if selected_groups:
        manual_excluded = [
            account_id
            for account_id, group_name, active, selected in account_states
            if group_name in selected_groups and not selected
        ]
        manual_included = [
            account_id
            for account_id, group_name, active, selected in account_states
            if selected and group_name not in selected_groups
        ]
        st.session_state.setdefault("manual_excluded_accounts", sorted(manual_excluded))
        st.session_state.setdefault("manual_included_accounts", sorted(manual_included))
    if rows and not st.session_state.get("grouped_accounts"):
        grouped = build_grouped_accounts(accounts, pd.DataFrame(rows))
        st.session_state["grouped_accounts"] = grouped
        st.session_state["selected_accounts"] = [account for group in grouped.values() for account in group["accounts"]]
    st.session_state["_accounts_restored_from_db"] = True


def reset_account_session_after_sync(accounts: list[dict]) -> None:
    """Discard account selection widgets that may still refer to removed remote accounts."""
    for key in list(st.session_state):
        if key.startswith(("account_group_", "account_use_", "account_status_enabled_v2_")):
            del st.session_state[key]
    st.session_state["threads_accounts"] = accounts
    st.session_state["selected_accounts"] = []
    st.session_state["grouped_accounts"] = {}
    st.session_state["selected_group_filters"] = []
    st.session_state["manual_included_accounts"] = []
    st.session_state["manual_excluded_accounts"] = []
    st.session_state.pop("_account_group_signature", None)
    st.session_state.pop("_accounts_restored_from_db", None)


def preview_dataframe(rows: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["photos"] = df["media_ids"].apply(lambda value: len(value or []))
    return df[["id", "scheduled_time_local", "account_name", "group_name", "content_type", "photos", "caption", "status"]]


def scheduled_dataframe(rows: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(rows).copy()
    if df.empty:
        return df
    now = datetime.now(ZoneInfo(APP_TZ))
    df.loc[:, "day"] = df["scheduled_time_local"].astype(str).str.slice(0, 10)
    df.loc[:, "time"] = df["scheduled_time_local"].astype(str).str.slice(11, 19)
    df.loc[:, "group_name"] = df["group_name"].fillna("Sans groupe")
    parsed_times = df["scheduled_time_local"].apply(parse_local_scheduled)
    df.loc[:, "time_state"] = parsed_times.apply(
        lambda value: "Déjà passé / à vérifier" if value is not None and value <= now else "À poster"
    )
    df.loc[:, "photos"] = df["media_ids"].apply(lambda value: len(value or []))
    df.loc[:, "text"] = df["caption"].astype(str).str.slice(0, 140)
    df.loc[:, "error"] = df.get("error", "").fillna("") if "error" in df.columns else ""
    df.loc[:, "replies"] = df.get("chain_replies", []).apply(lambda value: len(value or [])) if "chain_replies" in df.columns else 0
    df.loc[:, "threads_url"] = df.get("threads_url", "").fillna("") if "threads_url" in df.columns else ""
    df.loc[:, "preview_batch"] = df.get("preview_batch_id", "").fillna("") if "preview_batch_id" in df.columns else ""
    return df


def account_delivery_dataframe(rows: list[dict]) -> pd.DataFrame:
    df = scheduled_dataframe(rows)
    if df.empty:
        return pd.DataFrame(
            columns=[
                "account_name", "threads_url", "group_name", "total", "preview",
                "scheduled", "failed", "past", "next_post", "first_post", "last_post",
            ]
        )
    work = df.copy()
    work["is_preview"] = work["status"].astype(str).eq("preview")
    work["is_scheduled"] = ~work["status"].astype(str).isin(["preview", "preview_saved"]) & ~work["status"].astype(str).str.contains("fail|error", case=False, regex=True)
    work["is_failed"] = work["status"].astype(str).str.contains("fail|error", case=False, regex=True)
    work["is_past"] = work["time_state"].astype(str).str.contains("passé|vérifier", case=False, regex=True)
    summary = (
        work.groupby(["account_name", "threads_url", "group_name"], dropna=False)
        .agg(
            total=("id", "count"),
            preview=("is_preview", "sum"),
            scheduled=("is_scheduled", "sum"),
            failed=("is_failed", "sum"),
            past=("is_past", "sum"),
            next_post=("scheduled_time_local", "min"),
            first_post=("scheduled_time_local", "min"),
            last_post=("scheduled_time_local", "max"),
        )
        .reset_index()
        .sort_values(["failed", "preview", "next_post", "account_name"], ascending=[False, False, True, True])
    )
    for column in ["preview", "scheduled", "failed", "past", "total"]:
        summary[column] = summary[column].astype(int)
    summary["next_post"] = summary["next_post"].astype(str).str.slice(0, 19)
    summary["first_post"] = summary["first_post"].astype(str).str.slice(0, 19)
    summary["last_post"] = summary["last_post"].astype(str).str.slice(0, 19)
    return summary


def analytics_dataframe(rows: list[dict]) -> pd.DataFrame:
    df = scheduled_dataframe(rows)
    if df.empty:
        return df
    days = df["day"].apply(parse_day)
    df.loc[:, "week"] = days.apply(
        lambda value: f"{value.isocalendar().year}-W{value.isocalendar().week:02d}" if value else "Date inconnue"
    )
    df.loc[:, "month"] = df["day"].astype(str).str.slice(0, 7)
    df.loc[:, "month"] = df["month"].where(df["month"].str.len() == 7, "Date inconnue")
    df.loc[:, "account_name"] = df["account_name"].fillna("Compte inconnu")
    df.loc[:, "status"] = df["status"].fillna("unknown")
    return df


def count_by(df: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=[*columns, "posts"])
    return (
        df.groupby(columns, dropna=False)
        .size()
        .reset_index(name="posts")
        .sort_values("posts", ascending=False)
        .reset_index(drop=True)
    )


def pivot_counts(df: pd.DataFrame, index: str, columns: str) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    pivot = pd.pivot_table(
        df,
        index=index,
        columns=columns,
        values="id",
        aggfunc="count",
        fill_value=0,
    )
    pivot.loc[:, "Total"] = pivot.sum(axis=1)
    return pivot.sort_values("Total", ascending=False)


def filter_scheduled_rows(rows: list[dict], status_filter: str, date_filter: str, account_filter: str, group_filter: str, query: str) -> pd.DataFrame:
    df = scheduled_dataframe(rows)
    if df.empty:
        return df
    today = datetime.now(ZoneInfo(APP_TZ)).date()
    if status_filter != "Tous":
        df = df[df["status"].astype(str) == status_filter]
    dates = df["day"].apply(parse_day)
    if date_filter == "Aujourd'hui":
        df = df[dates == today]
    elif date_filter == "Semaine":
        df = df[(dates >= today) & (dates <= today + timedelta(days=7))]
    elif date_filter == "Mois":
        df = df[(dates >= today) & (dates <= today + timedelta(days=31))]
    if account_filter != "Tous les comptes":
        df = df[df["account_name"].astype(str) == account_filter]
    if group_filter != "Tous les groupes":
        df = df[df["group_name"].fillna("Sans groupe").astype(str) == group_filter]
    if query.strip():
        needle = query.strip().lower()
        haystack = (
            df["caption"].astype(str) + " " +
            df["account_name"].astype(str) + " " +
            df["group_name"].fillna("").astype(str) + " " +
            df["status"].astype(str) + " " +
            df["error"].astype(str)
        ).str.lower()
        df = df[haystack.str.contains(needle, regex=False)]
    return df


def render_status_counts(rows: list[dict]) -> None:
    counts = pd.Series([r.get("status", "unknown") for r in rows]).value_counts().to_dict() if rows else {}
    cols = st.columns(4)
    for col, status in zip(cols, ["preview", "scheduled", "published", "failed"]):
        col.metric(status.title(), counts.get(status, 0))


def render_account_delivery_panel(rows: list[dict], key_prefix: str, title: str = "Contrôle par compte") -> None:
    account_df = account_delivery_dataframe(rows)
    if account_df.empty:
        return
    st.markdown(f"#### {title}")
    total_posts = int(account_df["total"].sum())
    failed_accounts = int((account_df["failed"] > 0).sum())
    preview_accounts = int((account_df["preview"] > 0).sum())
    late_accounts = int((account_df["past"] > 0).sum())
    render_metric_strip(
        [
            ("Comptes", str(len(account_df)), "avec lignes visibles"),
            ("Posts", str(total_posts), "dans cette vue"),
            ("À envoyer", str(int(account_df["preview"].sum())), f"{preview_accounts} comptes"),
            ("Failed/passés", f"{failed_accounts}/{late_accounts}", "à contrôler"),
        ]
    )
    st.dataframe(
        account_df[
            [
                "account_name", "threads_url", "group_name", "total", "preview",
                "scheduled", "failed", "past", "next_post", "first_post", "last_post",
            ]
        ],
        use_container_width=True,
        hide_index=True,
        height=min(520, 96 + len(account_df) * 42),
        column_config={
            "account_name": st.column_config.TextColumn("Compte", width="medium"),
            "threads_url": st.column_config.LinkColumn("Threads", display_text="Ouvrir"),
            "group_name": st.column_config.TextColumn("Groupe", width="small"),
            "total": st.column_config.NumberColumn("Total", width="small"),
            "preview": st.column_config.NumberColumn("À envoyer", width="small"),
            "scheduled": st.column_config.NumberColumn("Envoyés/planifiés", width="small"),
            "failed": st.column_config.NumberColumn("Failed", width="small"),
            "past": st.column_config.NumberColumn("Passés", width="small"),
            "next_post": st.column_config.TextColumn("Prochain post", width="medium"),
            "first_post": st.column_config.TextColumn("Début", width="medium"),
            "last_post": st.column_config.TextColumn("Fin", width="medium"),
        },
    )
    st.session_state.setdefault(f"{key_prefix}_links_per_account", 5)
    link_limit = st.number_input(
        "Posts visibles par compte",
        min_value=1,
        max_value=50,
        step=1,
        key=f"{key_prefix}_links_per_account",
        help="Change seulement l'affichage des lignes par compte. L'envoi Postoria tente quand même toute la preview.",
    )
    df = scheduled_dataframe(rows)
    if df.empty:
        return
    df = df.sort_values(["account_name", "scheduled_time_local"])
    with st.expander("Détail horaire par compte", expanded=False):
        for account_name, chunk in df.groupby("account_name", sort=True):
            shown = chunk.head(int(link_limit))
            suffix = "" if len(chunk) <= int(link_limit) else f" · {len(chunk) - int(link_limit)} masqués"
            st.markdown(f"**{account_name} · {len(chunk)} posts{suffix}**")
            st.dataframe(
                shown[["day", "time", "time_state", "threads_url", "group_name", "status", "text", "error"]],
                use_container_width=True,
                hide_index=True,
                column_config={"threads_url": st.column_config.LinkColumn("Threads", display_text="Ouvrir")},
            )


def photo_data_uri(asset: dict | None) -> str:
    if not asset or not asset.get("image_bytes"):
        return ""
    mime_type = str(asset.get("mime_type") or "image/jpeg")
    encoded = base64.b64encode(asset["image_bytes"]).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def preview_asset_for_row(row: dict, assets_by_id: dict[int, dict], assets_by_media_id: dict[str, dict]) -> dict | None:
    for asset_id in row.get("local_photo_asset_ids") or []:
        asset = assets_by_id.get(int(asset_id))
        if asset:
            return asset
    for media_id in row.get("media_ids") or []:
        asset = assets_by_media_id.get(str(media_id))
        if asset:
            return asset
    return None


def preview_card_html(row: dict, assets_by_id: dict[int, dict] | None = None, assets_by_media_id: dict[str, dict] | None = None) -> str:
    assets_by_id = assets_by_id or {}
    assets_by_media_id = assets_by_media_id or {}
    local_time = str(row.get("scheduled_time_local") or "")
    day = local_time[:10] or "-"
    hour = local_time[11:16] or "-"
    caption = str(row.get("caption") or "")
    media_count = len(row.get("media_ids") or [])
    local_count = len(row.get("local_photo_asset_ids") or [])
    status = str(row.get("status") or "preview")
    status_class = "is-failed" if any(token in status.lower() for token in ("fail", "error")) else "is-ready"
    group_name = str(row.get("group_name") or "Sans groupe")
    media_label = f"{media_count} media ID" if media_count else ("Photo locale" if local_count else "Texte seul")
    text = h(caption[:210] + ("..." if len(caption) > 210 else ""))
    asset = preview_asset_for_row(row, assets_by_id, assets_by_media_id)
    image_src = photo_data_uri(asset)
    image_html = (
        f"<div class='preview-thumb'><img src='{image_src}' alt=''></div>"
        if image_src
        else "<div class='preview-thumb is-empty'><span>Image</span></div>"
    )
    return (
        f"<article class='preview-card {status_class}'>"
        f"{image_html}"
        "<div class='preview-card-top'>"
        f"<span class='preview-time'>{h(hour)}</span>"
        f"<span class='preview-status'>{h(status)}</span>"
        "</div>"
        f"<div class='preview-day'>{h(day)}</div>"
        f"<strong>{h(row.get('account_name') or 'Compte')}</strong>"
        f"<small>{h(group_name)} · {h(media_label)}</small>"
        f"<p>{text}</p>"
        "</article>"
    )


def render_visual_preview(rows: list[dict], key_prefix: str) -> None:
    if not rows:
        return
    st.markdown("#### Aperçu visuel")
    mode = st.radio(
        "Organisation visuelle",
        ["Par compte", "Par heure", "Par groupe"],
        horizontal=True,
        key=f"{key_prefix}_visual_mode",
    )
    st.session_state.setdefault(f"{key_prefix}_visual_limit", 36)
    limit = st.number_input(
        "Cartes visibles",
        min_value=6,
        max_value=120,
        step=6,
        key=f"{key_prefix}_visual_limit",
    )
    sorted_rows = sorted(rows, key=lambda row: (str(row.get("scheduled_time_utc")), str(row.get("account_name"))))
    if mode == "Par compte":
        sorted_rows = sorted(rows, key=lambda row: (str(row.get("account_name")), str(row.get("scheduled_time_utc"))))
    elif mode == "Par groupe":
        sorted_rows = sorted(rows, key=lambda row: (str(row.get("group_name")), str(row.get("account_name")), str(row.get("scheduled_time_utc"))))
    photo_assets = db.list_photo_assets()
    assets_by_id = {int(asset["id"]): asset for asset in photo_assets}
    assets_by_media_id = {
        str(asset.get("media_id")): asset
        for asset in photo_assets
        if str(asset.get("media_id") or "").strip()
    }
    cards = "".join(preview_card_html(row, assets_by_id, assets_by_media_id) for row in sorted_rows[: int(limit)])
    st.markdown(f"<div class='preview-card-grid'>{cards}</div>", unsafe_allow_html=True)
    if len(sorted_rows) > int(limit):
        st.caption(f"{len(sorted_rows) - int(limit)} posts masqués. Augmente Cartes visibles pour en voir plus.")


def render_preview_media_tools(preview_rows: list[dict]) -> None:
    rows = [row for row in preview_rows if str(row.get("status")) == "preview"]
    if not rows:
        return
    with st.expander("Photos sur une ligne preview", expanded=False):
        st.caption("Choisis un post précis. Le + ajoute une photo, le - retire les photos de cette ligne. Rien n'est envoyé à Postoria avant l'étape Envoi.")
        options = [int(row["id"]) for row in rows]
        picked_id = choose_option(
            "Post preview",
            options,
            format_func=lambda row_id: next(
                f"{str(row.get('scheduled_time_local'))[:16]} · {row.get('account_name')} · {str(row.get('caption'))[:70]}"
                for row in rows
                if int(row["id"]) == int(row_id)
            ),
            key="preview_media_target",
        )
        picked_row = next((row for row in rows if int(row["id"]) == int(picked_id)), None)
        current_media = list(picked_row.get("media_ids") or []) if picked_row else []
        current_local = list(picked_row.get("local_photo_asset_ids") or []) if picked_row else []
        st.caption(f"Actuel: {len(current_media)} media IDs Postoria, {len(current_local)} photos locales.")
        remove_col, add_col = st.columns([1, 2])
        with remove_col:
            if st.button("- Retirer photos", disabled=not picked_row or (not current_media and not current_local), use_container_width=True):
                db.update_scheduled_media(int(picked_id), [], [])
                st.warning("Photos retirées de cette ligne preview. Rien supprimé dans les groupes photos.")
                st.rerun()
        with add_col:
            add_mode = st.radio(
                "+ Ajouter",
                ["Photo précise", "Random groupe", "Media ID manuel"],
                horizontal=True,
                key="preview_add_photo_mode",
            )
            photo_assets = db.list_photo_assets()
            photo_groups = db.list_photo_groups()
            selected_asset = None
            manual_media_ids = ""
            if add_mode == "Photo précise":
                ready_assets = photo_assets
                if ready_assets:
                    asset_id = choose_option(
                        "Choisir photo",
                        [int(asset["id"]) for asset in ready_assets],
                        format_func=lambda aid: next(
                            f"{asset['group_name']} / {asset['name']} / media:{asset.get('media_id') or 'manquant'}"
                            for asset in ready_assets
                            if int(asset["id"]) == int(aid)
                        ),
                        key="preview_precise_photo_asset",
                    )
                    selected_asset = next((asset for asset in ready_assets if int(asset["id"]) == int(asset_id)), None)
                else:
                    st.info("Aucune photo locale. Ajoute des photos dans 3. Posts/photos.")
            elif add_mode == "Random groupe":
                group_names = [group["name"] for group in photo_groups if int(group.get("photo_count") or 0) > 0]
                if group_names:
                    group_name = choose_option("Groupe photo", group_names, key="preview_random_photo_group")
                    candidates = db.list_photo_assets(str(group_name))
                    if candidates:
                        selected_asset = random.choice(candidates)
                        st.caption(f"Random prêt: {selected_asset['name']} / media:{selected_asset.get('media_id') or 'manquant'}")
                else:
                    st.info("Aucun groupe photo avec image.")
            else:
                manual_media_ids = st.text_input("Media IDs à ajouter", placeholder="12345, 67890", key="preview_manual_media_ids")

            if st.button("+ Ajouter photo", disabled=not picked_row, use_container_width=True):
                new_media = list(current_media)
                new_local = list(current_local)
                if add_mode in ("Photo précise", "Random groupe"):
                    if not selected_asset:
                        st.error("Choisis une photo avant d'ajouter.")
                    else:
                        asset_id = int(selected_asset["id"])
                        if asset_id not in new_local:
                            new_local.append(asset_id)
                        media_id = str(selected_asset.get("media_id") or "").strip()
                        if media_id and media_id not in new_media:
                            new_media.append(media_id)
                        if not media_id:
                            st.warning("Photo ajoutée en aperçu local, mais sans media ID Postoria elle ne partira pas à l'envoi.")
                        db.update_scheduled_media(int(picked_id), new_media, new_local)
                        st.success("Photo ajoutée à cette ligne preview.")
                        st.rerun()
                else:
                    added_media = db.parse_media_ids(manual_media_ids)
                    if not added_media:
                        st.error("Ajoute au moins un media ID.")
                    else:
                        for media_id in added_media:
                            if media_id not in new_media:
                                new_media.append(media_id)
                        db.update_scheduled_media(int(picked_id), new_media, new_local)
                        st.success("Media ID ajouté à cette ligne preview.")
                        st.rerun()


def is_failed_status(row: dict) -> bool:
    return any(token in str(row.get("status", "")).lower() for token in ("fail", "error"))


def schedule_category_counts(rows: list[dict]) -> dict[str, int]:
    now = datetime.now(ZoneInfo(APP_TZ))
    preview_future_count = sum(
        1 for row in rows
        if str(row.get("status")) == "preview" and not is_past_scheduled(row, now)
    )
    preview_past_count = sum(
        1 for row in rows
        if str(row.get("status")) == "preview" and is_past_scheduled(row, now)
    )
    saved_preview_count = sum(1 for row in rows if str(row.get("status")) == "preview_saved")
    failed_count = sum(1 for row in rows if is_failed_status(row))
    planned_future_count = sum(
        1
        for row in rows
        if str(row.get("status")) not in ("preview", "preview_saved") and not is_failed_status(row) and not is_past_scheduled(row, now)
    )
    planned_past_count = sum(
        1
        for row in rows
        if str(row.get("status")) not in ("preview", "preview_saved") and not is_failed_status(row) and is_past_scheduled(row, now)
    )
    return {
        "Preview à poster": preview_future_count,
        "Preview déjà passée": preview_past_count,
        "Anciennes previews": saved_preview_count,
        "Planifiés à venir": planned_future_count,
        "Déjà passés / à vérifier": planned_past_count,
        "Failed": failed_count,
        "Tout": len(rows),
    }


def account_status_label(account: dict) -> str:
    if int(account.get("consecutive_failures", 0) or 0) >= 2:
        return "Rate limited"
    return "Active" if bool(account.get("active_for_day", 1)) else "Paused"


def account_initials(account: dict) -> str:
    label = str(account.get("username") or account.get("name") or "?").strip().lstrip("@")
    parts = [part for part in label.replace("_", " ").replace(".", " ").split(" ") if part]
    if not parts:
        return "?"
    return "".join(part[0].upper() for part in parts[:2])[:2]


GROUP_COLOR_CHOICES = [
    ("Violet", "#8b5cf6", "rgba(139, 92, 246, .16)"),
    ("Red", "#ef4444", "rgba(239, 68, 68, .14)"),
    ("Slate", "#64748b", "rgba(100, 116, 139, .16)"),
    ("Green", "#2fbf7b", "rgba(47, 191, 123, .14)"),
    ("Gold", "#e7b958", "rgba(231, 185, 88, .14)"),
    ("Blue", "#3b82f6", "rgba(59, 130, 246, .14)"),
]


def group_color(group_name: str, color_override: str | None = None) -> tuple[str, str]:
    if color_override:
        for _, dot, bg in GROUP_COLOR_CHOICES:
            if dot.lower() == str(color_override).lower():
                return dot, bg
    palette = [
        ("#8b5cf6", "rgba(139, 92, 246, .16)"),
        ("#ef4444", "rgba(239, 68, 68, .14)"),
        ("#64748b", "rgba(100, 116, 139, .16)"),
        ("#2fbf7b", "rgba(47, 191, 123, .14)"),
        ("#e7b958", "rgba(231, 185, 88, .14)"),
    ]
    index = sum(ord(char) for char in str(group_name or "tous")) % len(palette)
    return palette[index]


def render_group_badge(group_name: str, color_override: str | None = None) -> str:
    dot, bg = group_color(group_name, color_override)
    return (
        f"<span class='account-group-badge' style='background:{bg}; color:{dot};'>"
        f"<i style='background:{dot};'></i>{h(group_name)}</span>"
    )


def next_post_map(rows: list[dict]) -> dict[int, str]:
    result: dict[int, str] = {}
    for row in rows:
        account_id = int(row.get("account_id", 0) or 0)
        if account_id and account_id not in result:
            result[account_id] = str(row.get("scheduled_time_local", ""))[11:16] or "-"
    return result


def render_group_summary(grouped: dict[str, dict]) -> None:
    st.markdown("#### Groupes sélectionnés")
    if not grouped:
        st.info("Aucun compte sélectionné.")
        return
    cols = st.columns(min(3, max(1, len(grouped))))
    for idx, (group_name, group) in enumerate(grouped.items()):
        accounts = group.get("accounts", [])
        labels = [account_label(account) for account in accounts]
        with cols[idx % len(cols)]:
            st.markdown(
                "<div class='step-note'>"
                f"<b>{group_name}</b><br>{len(accounts)} comptes<br>"
                f"<small>{', '.join(labels[:8])}{'...' if len(labels) > 8 else ''}</small>"
                "</div>",
                unsafe_allow_html=True,
            )


def window_minutes(start: time, end: time) -> int:
    today = date.today()
    start_ts = datetime.combine(today, start)
    end_ts = datetime.combine(today, end)
    return max(0, int((end_ts - start_ts).total_seconds() / 60))


def parse_day(value) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def parse_local_scheduled(value) -> datetime | None:
    try:
        return datetime.strptime(str(value)[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=ZoneInfo(APP_TZ))
    except ValueError:
        return None


def is_past_scheduled(row: dict, now: datetime | None = None) -> bool:
    scheduled_at = parse_local_scheduled(row.get("scheduled_time_local"))
    if scheduled_at is None:
        return False
    now = now or datetime.now(ZoneInfo(APP_TZ))
    return scheduled_at <= now


def max_posts_for_window(start: time, end: time, min_gap: int) -> int:
    minutes = window_minutes(start, end)
    if minutes <= 0:
        return 0
    return floor(minutes / max(1, min_gap))


def max_posts_for_period(start_date: date, end_date: date, start: time, end: time, min_gap: int) -> int:
    """Return the conservative per-account capacity for a scheduling period."""
    start_dt = datetime.combine(start_date, start)
    end_dt = datetime.combine(end_date, end)
    minutes = int((end_dt - start_dt).total_seconds() / 60)
    if minutes <= 0:
        return 0
    return floor(minutes / max(1, min_gap))


def distribution_sentence(current: dict) -> str:
    if int(current.get("posts_max", 0)) == 0:
        return (
            "Mode 0 post: aucune publication ne sera créée. "
            "Générer une preview dans ce mode vide le brouillon courant sans toucher aux posts déjà planifiés/envoyés."
        )
    if current["count_mode"] == "Range":
        count = f"entre {current['posts_min']} et {current['posts_max']}"
    else:
        count = str(current["posts_min"])
    return (
        f"Chaque compte reçoit {count} posts répartis entre "
        f"{current['start_time'].strftime('%H:%M')} et {current['end_time'].strftime('%H:%M')}. "
        f"Chaque post garde au moins {current['min_interval']}min d'écart avec le post précédent du même compte. "
        "Les minutes sont randomisées par compte pour éviter que tous les comptes partent au même moment."
    )


def planned_total_sentence(account_count: int, posts_min: int, posts_max: int, capacity_per_account: int) -> str:
    if posts_min == posts_max:
        total = account_count * posts_min
        return f"{account_count} comptes sélectionnés x {posts_min} posts = {total} posts à programmer."
    total_min = account_count * posts_min
    total_max = account_count * posts_max
    return f"{account_count} comptes sélectionnés x {posts_min}-{posts_max} posts = {total_min}-{total_max} posts à programmer."


def planned_total_range(account_count: int, posts_min: int, posts_max: int) -> tuple[int, int]:
    return account_count * posts_min, account_count * posts_max


def parse_utc_scheduled(value) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        if raw.endswith("Z"):
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        parsed = datetime.fromisoformat(raw)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=ZoneInfo("UTC"))
    except ValueError:
        return None


def response_get_nested(data: dict, *path):
    current = data
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def postoria_response_id(response) -> int | None:
    if not isinstance(response, dict):
        return None
    candidates = [
        response.get("id"),
        response.get("post_id"),
        response.get("postoria_post_id"),
        response_get_nested(response, "data", "id"),
        response_get_nested(response, "post", "id"),
        response_get_nested(response, "data", "post", "id"),
    ]
    for candidate in candidates:
        if candidate is None or str(candidate).strip() == "":
            continue
        try:
            return int(candidate)
        except (TypeError, ValueError):
            continue
    return None


def postoria_response_status(response) -> str:
    if not isinstance(response, dict):
        return "scheduled"
    candidates = [
        response.get("status"),
        response_get_nested(response, "data", "status"),
        response_get_nested(response, "post", "status"),
        response_get_nested(response, "data", "post", "status"),
    ]
    for candidate in candidates:
        text = str(candidate or "").strip()
        if text:
            return text
    return "scheduled"


def short_debug(value, limit: int = 500) -> str:
    text = str(value or "")
    return text if len(text) <= limit else text[:limit] + "..."


def refresh_postoria_statuses(client: PostoriaClient, workspace_id: int | str) -> tuple[int, int]:
    checked = 0
    errors = 0
    for row in db.list_scheduled():
        if not row.get("postoria_post_id"):
            continue
        try:
            res = client.get_post(int(workspace_id), int(row["postoria_post_id"]))
            db.update_scheduled_result(row["id"], row["postoria_post_id"], postoria_response_status(res), None)
            checked += 1
        except Exception as e:
            db.update_scheduled_result(row["id"], row["postoria_post_id"], "status_error", short_debug(e))
            errors += 1
    return checked, errors


def retryable_failed_posts(rows: list[dict]) -> list[dict]:
    return [
        row for row in rows
        if is_failed_status(row) and not row.get("postoria_post_id")
    ]


def local_photo_without_media_id(rows: list[dict]) -> list[dict]:
    return [
        row for row in rows
        if row.get("local_photo_asset_ids") and not row.get("media_ids")
    ]


def retry_failed_posts_direct(
    client: PostoriaClient,
    workspace_id: int | str,
    rows: list[dict],
) -> tuple[int, int, list[dict]]:
    sent_count = 0
    failed_count = 0
    errors: list[dict] = []
    for row in rows:
        try:
            if not parse_utc_scheduled(row.get("scheduled_time_utc")):
                raise RuntimeError(f"Heure UTC invalide: {row.get('scheduled_time_utc')}")
            res = client.create_post(
                int(workspace_id),
                int(row["account_id"]),
                row["caption"],
                row["scheduled_time_utc"],
                row.get("media_ids") or [],
            )
            postoria_id = postoria_response_id(res)
            postoria_status = postoria_response_status(res)
            if postoria_id is None:
                raise RuntimeError(f"Réponse Postoria sans post id: {short_debug(res)}")
            db.update_scheduled_result(row["id"], postoria_id, postoria_status, None)
            sent_count += 1
        except Exception as e:
            failed_count += 1
            error = short_debug(e)
            db.update_scheduled_result(row["id"], None, "failed", error)
            errors.append(
                {
                    "id": row.get("id"),
                    "compte": row.get("account_name"),
                    "heure_initiale": row.get("scheduled_time_local"),
                    "utc_initial": row.get("scheduled_time_utc"),
                    "erreur": error,
                }
            )
    return sent_count, failed_count, errors


def h(value) -> str:
    return escape(str(value or ""), quote=True)


def post_readable_card_html(post: dict, selected: bool, has_media: bool) -> str:
    caption = str(post.get("caption") or "")
    text = h(caption).replace("\n", "<br>")
    media_ids = media_ids_text(post.get("media_ids"))
    media_folder = str(post.get("media_folder") or "").strip()
    media_label = media_ids or media_folder or "texte seul"
    import_batches = str(post.get("import_batches") or "").strip()
    variables = variables_text(post.get("variables"))
    replies = len(post.get("reply_chain") or [])
    selected_label = "Sélectionné" if selected else "Non sélectionné"
    media_class = "has-media" if has_media else "text-only"
    return (
        f"<article class='post-readable-card {'is-selected' if selected else ''}'>"
        "<div class='post-readable-top'>"
        f"<span>#{int(post.get('id') or 0)}</span>"
        f"<b>{h(selected_label)}</b>"
        "</div>"
        f"<p>{text}</p>"
        "<div class='post-readable-meta'>"
        f"<small class='{media_class}'>{h(media_label)}</small>"
        f"<small>{len(caption)} caractères</small>"
        f"<small>{replies} replies</small>"
        f"{f'<small>{h(import_batches)}</small>' if import_batches else ''}"
        f"{f'<small>{h(variables)}</small>' if variables else ''}"
        "</div>"
        "</article>"
    )


def render_post_visual_card(post: dict, selected: bool, has_media: bool, widget_key: str) -> bool:
    caption = str(post.get("caption") or "")
    media_ids = media_ids_text(post.get("media_ids"))
    media_folder = str(post.get("media_folder") or "").strip()
    import_batches = str(post.get("import_batches") or "").strip()
    media_label = media_ids or media_folder or "Texte"
    import_label = import_batches or "Bibliothèque"
    state_label = "Sélectionné" if selected else "Non sélectionné"
    checkbox_col, content_col = st.columns([0.28, 5.72], vertical_alignment="center")
    with checkbox_col:
        checked = st.checkbox(
            "Sélectionner ce post",
            value=bool(selected),
            key=widget_key,
            label_visibility="collapsed",
            help="Coche pour inclure ce texte dans la prochaine preview.",
        )
    with content_col:
        st.markdown(
            "<article class='post-pick-shell'>"
            f"<span class='post-pick-kind {'has-media' if has_media else ''}'>{'IMG' if has_media else 'T'}</span>"
            "<div class='post-pick-copy'>"
            f"<p>{h(caption).replace(chr(10), '<br>')}</p>"
            "<div class='post-pick-details'>"
            f"<span>#{int(post.get('id') or 0)}</span>"
            f"<span>{h(media_label)}</span>"
            f"<span>{len(caption)} caractères</span>"
            f"<span>{h(import_label)}</span>"
            "</div>"
            "</div>"
            f"<span class='post-pick-state {'is-selected' if selected else ''}'>{h(state_label)}</span>"
            "</article>",
            unsafe_allow_html=True,
        )
    return checked


def import_batch_card_html(batch: dict, active: bool = False) -> str:
    created_at = str(batch.get("created_at") or "")[:16]
    linked = int(batch.get("linked_count") or batch.get("post_count") or 0)
    active_count = int(batch.get("active_count") or 0)
    return (
        f"<article class='import-batch-card {'is-active' if active else ''}'>"
        f"<strong>{h(batch.get('file_name') or batch.get('name'))}</strong>"
        f"<span>{h(created_at)}</span>"
        "<div>"
        f"<b>{linked}</b><small>posts</small>"
        f"<b>{active_count}</b><small>actifs</small>"
        f"<b>{int(batch.get('added_count') or 0)}</b><small>nouveaux</small>"
        f"<b>{int(batch.get('reused_count') or 0)}</b><small>déjà vus</small>"
        "</div>"
        "</article>"
    )


def choose_option(
    label: str,
    options: list,
    index: int = 0,
    key: str | None = None,
    format_func=None,
    horizontal: bool = False,
    label_visibility: str = "visible",
    help: str | None = None,
):
    if not options:
        return None
    safe_index = min(max(int(index), 0), len(options) - 1)
    return st.radio(
        label,
        options,
        index=safe_index,
        key=key,
        format_func=format_func or (lambda value: str(value)),
        horizontal=horizontal,
        label_visibility=label_visibility,
        help=help,
    )


def render_post_editor_dialog(post_id: int) -> None:
    """Edit or delete one library post without leaving the library."""
    post = next((item for item in db.list_posts(active_only=False) if int(item["id"]) == int(post_id)), None)
    if not post:
        st.session_state.pop("post_library_edit_id", None)
        return

    @st.dialog("Modifier le post")
    def edit_dialog() -> None:
        st.caption("Les changements concernent la bibliothèque. Les posts déjà acceptés par Postoria ne sont jamais supprimés.")
        with st.form(f"library_edit_post_{post_id}"):
            revised_caption = st.text_area("Texte du post", value=str(post.get("caption") or ""), height=190)
            favorite = st.checkbox("Ajouter aux favoris", value=bool(post.get("is_favorite")))
            save_post = st.form_submit_button("Enregistrer", type="primary", use_container_width=True)
        if save_post:
            if not db.update_post_caption(int(post_id), revised_caption):
                st.warning("Le texte doit être rempli et unique.")
            else:
                db.set_post_favorite(int(post_id), favorite)
                st.session_state["posts_editor_version"] = st.session_state.get("posts_editor_version", 0) + 1
                st.session_state.pop("post_library_edit_id", None)
                clear_preview_draft("Preview brouillon supprimée: texte modifié. Les posts déjà planifiés restent conservés.")
                st.rerun()

        if st.button("Supprimer de la bibliothèque", key=f"library_delete_post_{post_id}", use_container_width=True):
            result = db.delete_or_deactivate_posts([int(post_id)])
            selected_ids = {
                int(item["id"])
                for item in st.session_state.get("selected_posts", [])
                if int(item["id"]) != int(post_id)
            }
            post_by_id = {int(item["id"]): item for item in db.list_posts(active_only=False)}
            st.session_state["selected_posts"] = [post_by_id[item_id] for item_id in sorted(selected_ids) if item_id in post_by_id]
            st.session_state["_selected_posts_signature"] = tuple(sorted(selected_ids))
            st.session_state["posts_editor_version"] = st.session_state.get("posts_editor_version", 0) + 1
            st.session_state.pop("library_preview_post_id", None)
            st.session_state.pop("post_library_edit_id", None)
            clear_preview_draft("Preview brouillon supprimée: post retiré de la bibliothèque. Les posts déjà planifiés restent conservés.")
            st.rerun()
        if st.button("Annuler", key=f"library_cancel_edit_{post_id}", use_container_width=True):
            st.session_state.pop("post_library_edit_id", None)
            st.rerun()

    edit_dialog()


def render_post_library_workspace() -> None:
    """Finder-like batches, a readable post list, and one persistent preview."""
    batches = db.list_post_import_batches()
    all_posts = db.list_posts(active_only=False)
    if not st.session_state.get("post_library_selection_defaults_v2"):
        st.session_state["selected_posts"] = []
        st.session_state["posts_selection_explicit"] = True
        st.session_state["_selected_posts_signature"] = tuple()
        st.session_state["post_library_selection_defaults_v2"] = True

    if not batches and not all_posts:
        st.markdown("<div class='post-library-workspace'></div>", unsafe_allow_html=True)
        st.info("Importe un premier CSV ou crée un post pour démarrer la bibliothèque.")
        return

    batch_ids = [str(batch["id"]) for batch in batches]
    valid_views = {"all", "favorites", *batch_ids}
    active_batch_id = str(st.session_state.get("post_library_batch_id") or "all")
    if active_batch_id not in valid_views:
        active_batch_id = "all"
        st.session_state["post_library_batch_id"] = active_batch_id

    post_by_id = {int(post["id"]): post for post in all_posts}
    selected_ids = {
        int(post["id"])
        for post in st.session_state.get("selected_posts", [])
        if int(post["id"]) in post_by_id
    }

    def persist_selection(next_ids: set[int], notice: str | None = None) -> None:
        clean_ids = {int(post_id) for post_id in next_ids if int(post_id) in post_by_id}
        st.session_state["selected_posts"] = [post_by_id[post_id] for post_id in sorted(clean_ids)]
        st.session_state["posts_selection_explicit"] = True
        st.session_state["_selected_posts_signature"] = tuple(sorted(clean_ids))
        st.session_state["posts_editor_version"] = st.session_state.get("posts_editor_version", 0) + 1
        clear_preview_draft(notice or "Preview brouillon supprimée: sélection de la bibliothèque modifiée. Les posts déjà planifiés restent conservés.")

    def set_post_selected(post_id: int, state_key: str) -> None:
        current_ids = {int(post["id"]) for post in st.session_state.get("selected_posts", [])}
        if bool(st.session_state.get(state_key)):
            current_ids.add(int(post_id))
        else:
            current_ids.discard(int(post_id))
        persist_selection(current_ids)

    def batch_post_ids(batch_id: str) -> set[int]:
        return {int(post_id) for post_id in db.post_ids_for_import_batch(batch_id)}

    if active_batch_id == "favorites":
        visible_posts = [post for post in all_posts if bool(post.get("is_favorite"))]
    elif active_batch_id == "all":
        visible_posts = all_posts
    else:
        current_batch_ids = batch_post_ids(active_batch_id)
        visible_posts = [post for post in all_posts if int(post["id"]) in current_batch_ids]

    import_notice = str(st.session_state.pop("post_library_import_notice", "") or "").strip()
    page_size = 18
    page_key = f"post_library_page_{active_batch_id}"
    page_count = max(1, (len(visible_posts) + page_size - 1) // page_size)
    current_page = max(1, min(int(st.session_state.get(page_key, 1) or 1), page_count))
    st.session_state[page_key] = current_page
    page_start = (current_page - 1) * page_size
    page_posts = visible_posts[page_start:page_start + page_size]

    st.markdown(
        "<div class='post-library-workspace'></div>"
        "<div class='post-library-header'><div><span>Bibliothèque</span><h3>Posts</h3>"
        "<p>Les coches définissent la prochaine preview. L'aperçu reste indépendant.</p></div>"
        f"<b>{len(selected_ids)} sélectionné(s)</b></div>",
        unsafe_allow_html=True,
    )
    if import_notice:
        st.success(import_notice)

    finder_col, list_col = st.columns([1.18, 3.82], gap="large")
    with finder_col:
        with st.container(border=True):
            st.markdown("<div class='post-library-finder-title'>BIBLIOTHÈQUE</div>", unsafe_allow_html=True)
            with st.expander("Importer un CSV", expanded=False):
                uploaded_files = st.file_uploader(
                    "Fichiers CSV",
                    type=["csv"],
                    accept_multiple_files=True,
                    key="library_csv_upload",
                    label_visibility="collapsed",
                )
                if st.button("Importer", disabled=not uploaded_files, type="primary", key="library_import_csv", use_container_width=True):
                    existing_hashes = {str(batch.get("file_hash") or "") for batch in db.list_post_import_batches()}
                    latest_batch_id = ""
                    imported_post_ids: set[int] = set()
                    summaries: list[str] = []
                    errors: list[str] = []
                    with st.spinner("Import des CSV en cours..."):
                        for uploaded in uploaded_files or []:
                            raw = uploaded.getvalue()
                            file_hash = hashlib.sha256(raw).hexdigest()
                            if file_hash in existing_hashes:
                                summaries.append(f"{uploaded.name}: déjà présent")
                                continue
                            try:
                                records = parse_post_csv(raw, uploaded.name)
                                added, skipped, post_ids = db.add_posts_with_ids(records)
                                latest_batch_id = db.record_post_import_batch(
                                    uploaded.name, file_hash, len(raw), added, skipped, post_ids,
                                )
                                imported_post_ids.update(int(post_id) for post_id in post_ids)
                                summaries.append(f"{uploaded.name}: {added} ajoutés, {skipped} ignorés")
                            except (OSError, ValueError, pd.errors.ParserError) as exc:
                                errors.append(f"{uploaded.name}: {exc}")
                    if latest_batch_id:
                        st.session_state["post_library_batch_id"] = latest_batch_id
                        st.session_state["post_import_batch_filter"] = latest_batch_id
                        persist_selection(selected_ids | imported_post_ids)
                        st.session_state["post_library_import_notice"] = " | ".join(summaries)
                        if errors:
                            st.session_state["post_library_import_notice"] += f". {len(errors)} fichier(s) non importé(s)."
                        st.rerun()
                    if errors:
                        st.error(" | ".join(errors))
                    if summaries and not latest_batch_id:
                        st.info(" | ".join(summaries))

            if st.button("Nouveau post", key="library_open_new_post", use_container_width=True):
                st.session_state["post_library_new_post"] = True
                st.rerun()

            finder_buttons = [
                ("all", f"Tous les posts ({len(all_posts)})"),
                ("favorites", f"Favoris ({sum(1 for post in all_posts if bool(post.get('is_favorite')))})"),
            ]
            for view_id, label in finder_buttons:
                if st.button(label, key=f"library_view_{view_id}", type="primary" if active_batch_id == view_id else "secondary", use_container_width=True):
                    st.session_state["post_library_batch_id"] = view_id
                    st.rerun()

            st.markdown("<div class='post-library-finder-title is-subtitle'>IMPORTS CSV</div>", unsafe_allow_html=True)
            for batch in batches:
                batch_id = str(batch["id"])
                batch_ids_for_row = batch_post_ids(batch_id)
                checked_count = len(batch_ids_for_row & selected_ids)
                label = f"{batch.get('file_name') or batch.get('name') or 'CSV'}\n{len(batch_ids_for_row)} posts · {checked_count} cochés"
                if st.button(
                    label,
                    key=f"library_batch_{batch_id}",
                    type="primary" if active_batch_id == batch_id else "secondary",
                    use_container_width=True,
                ):
                    st.session_state["post_library_batch_id"] = batch_id
                    st.rerun()

    with list_col:
        with st.container(border=True):
            st.markdown(
                "<div class='post-library-list-title'><div><span>POSTS</span>"
                f"<strong>{len(visible_posts)} post(s)</strong></div>"
                f"<b>{len(selected_ids)} pour la preview</b></div>",
                unsafe_allow_html=True,
            )
            action_a, action_b, action_c = st.columns([1.05, 1.2, 1.65], gap="small")
            visible_ids = {int(post["id"]) for post in visible_posts if bool(post.get("is_active", 1))}
            with action_a:
                select_label = "Cocher ce CSV" if active_batch_id not in {"all", "favorites"} else "Cocher la vue"
                if st.button(select_label, key="library_select_batch", use_container_width=True):
                    persist_selection(selected_ids | visible_ids)
                    st.rerun()
            with action_b:
                clear_label = "Tout décocher" if active_batch_id == "all" else "Décocher la vue"
                if st.button(clear_label, key="library_select_none", use_container_width=True):
                    persist_selection(set() if active_batch_id == "all" else selected_ids - visible_ids)
                    st.rerun()
            with action_c:
                if st.button("Continuer vers Preview", key="library_continue_preview", type="primary", use_container_width=True):
                    if not selected_ids:
                        st.warning("Coche au moins un post avant de passer à la preview.")
                    else:
                        st.session_state["active_step"] = 3
                        st.session_state["app_page"] = "preview"
                        st.rerun()

            page_label = (
                "Aucun post" if not visible_posts
                else f"{page_start + 1}-{min(page_start + page_size, len(visible_posts))} sur {len(visible_posts)}"
            )
            page_prev, page_info, page_next = st.columns([1, 2.4, 1], gap="small")
            with page_prev:
                if st.button("Précédent", key=f"library_page_previous_{active_batch_id}", disabled=current_page <= 1, use_container_width=True):
                    st.session_state[page_key] = current_page - 1
                    st.rerun()
            with page_info:
                st.markdown(
                    f"<div class='post-library-page-info'>{page_label}</div>",
                    unsafe_allow_html=True,
                )
            with page_next:
                if st.button("Suivant", key=f"library_page_next_{active_batch_id}", disabled=current_page >= page_count, use_container_width=True):
                    st.session_state[page_key] = current_page + 1
                    st.rerun()

            st.markdown("<div class='post-list-head post-list-head-new'><span></span><span>POST</span><span>MÉDIA</span><span>UTILISÉ</span><span>MODIFIER</span></div>", unsafe_allow_html=True)
            with st.container(height=690, border=False):
                if not visible_posts:
                    st.markdown("<div class='post-library-empty'>Aucun post dans cette vue.</div>", unsafe_allow_html=True)
                else:
                    for post in page_posts:
                        post_id = int(post["id"])
                        selection_key = f"library_selected_{post_id}_{st.session_state.get('posts_editor_version', 0)}"
                        row_cols = st.columns([.44, 8.5, .9, .85, 1.15], gap="small")
                        with row_cols[0]:
                            st.checkbox(
                                "Sélectionner",
                                value=bool(post.get("is_active", 1)) and post_id in selected_ids,
                                key=selection_key,
                                disabled=not bool(post.get("is_active", 1)),
                                label_visibility="collapsed",
                                on_change=set_post_selected,
                                args=(post_id, selection_key),
                            )
                        with row_cols[1]:
                            st.markdown(
                                "<div class='post-library-row-caption'>"
                                "<i>T</i>"
                                f"<span>{h(str(post.get('caption') or 'Post sans texte'))}</span>"
                                "</div>",
                                unsafe_allow_html=True,
                            )
                        with row_cols[2]:
                            st.markdown(
                                f"<div class='post-library-row-meta'>{'Photo' if media_ids_text(post.get('media_ids')) else '—'}</div>",
                                unsafe_allow_html=True,
                            )
                        with row_cols[3]:
                            st.markdown(
                                f"<div class='post-library-row-meta'>{int(post.get('total_used') or 0)}</div>",
                                unsafe_allow_html=True,
                            )
                        with row_cols[4]:
                            if st.button("Modifier", key=f"library_edit_post_{post_id}", use_container_width=True):
                                st.session_state["post_library_edit_id"] = post_id
                                st.rerun()
                        st.markdown("<div class='post-row-divider'></div>", unsafe_allow_html=True)

    if st.session_state.get("post_library_new_post"):
        @st.dialog("Nouveau post")
        def new_post_dialog() -> None:
            with st.form("library_create_post"):
                new_caption = st.text_area("Texte", height=180, placeholder="Écris le post ici...")
                new_media_ids = st.text_input("Identifiants média", placeholder="Optionnel")
                create_post = st.form_submit_button("Créer le post", type="primary", use_container_width=True)
            if create_post:
                _, _, post_ids = db.add_posts_with_ids([{"caption": new_caption, "media_ids": new_media_ids}])
                if not post_ids:
                    st.warning("Ajoute un texte unique avant de créer le post.")
                else:
                    st.session_state.pop("post_library_new_post", None)
                    st.session_state["library_preview_post_id"] = int(post_ids[0])
                    st.session_state["posts_editor_version"] = st.session_state.get("posts_editor_version", 0) + 1
                    st.rerun()
            if st.button("Annuler", key="library_cancel_new_post", use_container_width=True):
                st.session_state.pop("post_library_new_post", None)
                st.rerun()
        new_post_dialog()

    if st.session_state.get("post_library_edit_id"):
        render_post_editor_dialog(int(st.session_state["post_library_edit_id"]))


def widget_slug(value: str) -> str:
    return "".join(char.lower() if char.isalnum() else "_" for char in str(value or "item")).strip("_") or "item"


def render_locked_step(title: str, blockers: list[str]) -> None:
    items = "".join(f"<li>{h(blocker)}</li>" for blocker in blockers)
    st.markdown(
        "<div class='blocked-panel'>"
        "<div class='blocked-kicker'>Action requise</div>"
        f"<strong>{h(title)}</strong>"
        f"<ul>{items}</ul>"
        "</div>",
        unsafe_allow_html=True,
    )


def render_group_cards(groups: list[dict], grouped: dict[str, dict] | None = None) -> None:
    if not groups:
        st.info("Aucun groupe. Crée un groupe, puis assigne les comptes.")
        return
    grouped = grouped or {}
    chips = []
    for group in groups:
        name = group["name"]
        selected_count = len(grouped.get(name, {}).get("accounts", []))
        state = "is-active" if selected_count else ""
        dot, bg = group_color(name, group.get("color"))
        chips.append(
            f"<div class='group-chip {state}' style='background:{bg};'>"
            f"<span><i style='background:{dot};'></i>{h(name)}</span>"
            f"<b>{selected_count}</b>"
            f"<small>{int(group.get('account_count', 0) or 0)} assignés</small>"
            "</div>"
        )
    st.markdown("<div class='group-strip'>" + "".join(chips) + "</div>", unsafe_allow_html=True)


def render_accounts_group_board(
    groups: list[dict],
    group_accounts_by_name: dict[str, list[dict]],
    selected_group_filters: list[str],
) -> None:
    chips = []
    selected_names = set(selected_group_filters)
    for group in groups:
        group_name = str(group["name"])
        dot, _ = group_color(group_name, group.get("color"))
        group_accounts = group_accounts_by_name.get(group_name, [])
        selected_count = sum(
            1
            for account in group_accounts
            if st.session_state.get(f"account_use_{int(account['id'])}", False)
        )
        chips.append(
            f"<div class='accounts-group-chip {'is-selected' if group_name in selected_names else ''}'>"
            f"<i style='background:{dot};'></i>"
            f"<span>{h(group_name)}</span>"
            f"<b>{selected_count}/{len(group_accounts)}</b>"
            "</div>"
        )
    content = "".join(chips) or "<span class='accounts-group-empty'>Aucun groupe créé</span>"
    st.markdown(
        "<section class='accounts-group-board'>"
        "<header><span>GROUPES POUR CE PLANNING</span><small>Clique un groupe juste dessous pour utiliser ses comptes.</small></header>"
        f"<div>{content}</div>"
        "</section>",
        unsafe_allow_html=True,
    )


def render_group_planning_selector(
    groups: list[dict],
    group_accounts_by_name: dict[str, list[dict]],
    selected_group_filters: list[str],
) -> None:
    """Direct group selection for the next schedule instead of decorative-only chips."""
    if not groups:
        return
    st.markdown("<div class='group-plan-picker-label'>Choisir le groupe à planifier</div>", unsafe_allow_html=True)
    group_columns = st.columns(min(4, max(1, len(groups))))
    selected_names = set(selected_group_filters)
    for index, group in enumerate(groups):
        group_name = str(group["name"])
        account_count = len(group_accounts_by_name.get(group_name, []))
        is_selected = group_name in selected_names
        label = f"{'✓ ' if is_selected else ''}{group_name} · {account_count} compte{'s' if account_count != 1 else ''}"
        with group_columns[index % len(group_columns)]:
            if st.button(
                label,
                key=f"plan_group_{index}_{widget_slug(group_name)}",
                type="primary" if is_selected else "secondary",
                disabled=not account_count,
                use_container_width=True,
            ):
                # A direct choice means only this group's accounts go to Cadence.
                st.session_state["selected_group_filters"] = [group_name]
                st.session_state["manual_included_accounts"] = []
                st.session_state["manual_excluded_accounts"] = []
                st.session_state.pop("_account_group_signature", None)
                mark_group_config_dirty()
                st.rerun()


def section_intro(step: str, title: str, body: str) -> None:
    st.markdown(
        "<div class='section-intro'>"
        f"<span>{step}</span>"
        f"<strong>{title}</strong>"
        f"<p>{body}</p>"
        "</div>",
        unsafe_allow_html=True,
    )


def render_flow_status(
    accounts_ready: bool,
    cadence_ready: bool,
    posts_ready: bool,
    preview_ready: bool,
    analytics_ready: bool,
    send_ready: bool,
    tracking_ready: bool,
) -> int:
    active_step = int(st.session_state.get("active_step", 0))
    active_step = min(6, max(0, active_step))
    steps = [
        ("1", "Comptes", accounts_ready),
        ("2", "Cadence", cadence_ready),
        ("3", "Posts/photos", posts_ready),
        ("4", "Preview", preview_ready),
        ("5", "Analytics", analytics_ready),
        ("6", "Envoi", send_ready),
        ("7", "Suivi", tracking_ready),
    ]
    cols = st.columns([1.05, .8, 1.05, .8, .9, .75, .75])
    for idx, (number, label, ready) in enumerate(steps):
        status = "OK" if ready else "À faire"
        active_mark = "● " if idx == active_step else ""
        with cols[idx]:
            if st.button(
                f"{active_mark}{number}  {label}\n{status}",
                key=f"step_nav_{idx}",
                use_container_width=True,
            ):
                st.session_state["active_step"] = idx
                active_step = idx
    return active_step


def render_step_links(active_step: int) -> None:
    """Compact in-app links between the scheduler steps."""
    steps = [
        ("accounts", "Comptes"),
        ("cadence", "Cadence"),
        ("posts", "Posts"),
        ("preview", "Preview"),
        ("analytics", "Analytics"),
        ("send", "Envoi"),
        ("tracking", "Suivi"),
    ]
    cols = st.columns(len(steps))
    for index, (page_key, label) in enumerate(steps):
        with cols[index]:
            if st.button(
                label,
                key=f"step_link_{page_key}",
                type="primary" if index == active_step else "secondary",
                use_container_width=True,
            ):
                st.session_state["app_page"] = page_key
                st.session_state["active_step"] = index
                st.rerun()


def render_app_header(api_exists: bool, dry_run: bool, app_tz: str) -> None:
    api_state = "API détectée" if api_exists else "API manquante"
    run_state = "Dry-run actif" if dry_run else "Envoi réel armé"
    st.markdown(
        "<div class='app-hero app-topbar'>"
        "<span class='topbar-title'>Postoria Threads</span>"
        "<div class='hero-status'>"
        f"<span class='status-pill {'ok' if api_exists else 'warn'}'>{h(api_state)}</span>"
        f"<span class='status-pill {'warn' if dry_run else 'ok'}'>{h(run_state)}</span>"
        f"<span class='status-pill neutral'>{h(app_tz)}</span>"
        "</div>"
        "</div>",
        unsafe_allow_html=True,
    )


def render_dashboard_overview(accounts: list[dict], posts: list[dict], preview_rows: list[dict], scheduled_rows: list[dict]) -> None:
    failed_rows = [row for row in scheduled_rows if is_failed_status(row)]
    planned_rows = [
        row for row in scheduled_rows
        if row.get("postoria_post_id") and not is_failed_status(row)
    ]
    active_accounts = sum(1 for account in accounts if bool(account.get("active_for_day", 1)))
    now_local = datetime.now(ZoneInfo(APP_TZ))
    upcoming_rows = sorted(
        [row for row in preview_rows if not is_past_scheduled(row, now_local)],
        key=lambda row: str(row.get("scheduled_time_local") or ""),
    )[:5]
    # The dashboard only counts posts that Postoria actually accepted.
    # Local previews and retries still waiting for an accepted Postoria ID stay out of this total.
    schedule_df = scheduled_dataframe(planned_rows)
    day_dates = [now_local.date() - timedelta(days=offset) for offset in range(6, -1, -1)]
    day_keys = [item.isoformat() for item in day_dates]
    day_counts = {day: 0 for day in day_keys}
    if not schedule_df.empty:
        for day, count in schedule_df["day"].value_counts().to_dict().items():
            if str(day) in day_counts:
                day_counts[str(day)] = int(count)
    max_day_count = max(day_counts.values(), default=0) or 1
    chart_items = "".join(
        "<div class='dashboard-chart-day'>"
        f"<div class='dashboard-chart-bar' style='height:{max(4, round(day_counts[day] / max_day_count * 100))}%;'></div>"
        f"<span>{day_dates[index].strftime('%a')}</span>"
        "</div>"
        for index, day in enumerate(day_keys)
    )
    upcoming_html = "".join(
        "<div class='dashboard-list-row'>"
        "<span class='dashboard-list-time'>"
        f"{h(str(row.get('scheduled_time_local') or '')[11:16] or '--:--')}"
        "</span>"
        "<div>"
        f"<strong>{h(row.get('account_name') or 'Compte')}</strong>"
        f"<small>{h(str(row.get('caption') or 'Sans texte')[:72])}</small>"
        "</div>"
        f"<em>{h(row.get('group_name') or 'Sans groupe')}</em>"
        "</div>"
        for row in upcoming_rows
    ) or "<div class='dashboard-empty'>Aucune preview à venir</div>"
    if schedule_df.empty:
        top_accounts_html = "<div class='dashboard-empty'>Les comptes apparaîtront après la première preview.</div>"
    else:
        top_accounts = (
            schedule_df.groupby("account_name", dropna=False)
            .size()
            .sort_values(ascending=False)
            .head(5)
        )
        top_account_max = int(top_accounts.max()) or 1
        top_accounts_html = "".join(
            "<div class='dashboard-account-row'>"
            f"<span>{h(account_name or 'Compte')}</span>"
            f"<i><b style='width:{max(5, round(int(post_count) / top_account_max * 100))}%'></b></i>"
            f"<strong>{int(post_count)}</strong>"
            "</div>"
            for account_name, post_count in top_accounts.items()
        )

    st.markdown(
        "<section class='dashboard-heading'>"
        "<div><span>DASHBOARD</span><h1>Planification Threads</h1>"
        "<p>Comptes, bibliothèque et programmation dans une seule vue.</p></div>"
        f"<small>{h(now_local.strftime('%d %b %Y · %H:%M'))}</small>"
        "</section>"
        "<section class='dashboard-metric-grid'>"
        "<article><i class='dashboard-icon'>A</i><span>COMPTES</span>"
        f"<strong>{len(accounts)}</strong><small>{active_accounts} actifs</small></article>"
        "<article><i class='dashboard-icon'>P</i><span>BIBLIOTHÈQUE</span>"
        f"<strong>{len(posts)}</strong><small>posts importés</small></article>"
        "<article><i class='dashboard-icon'>Q</i><span>À ENVOYER</span>"
        f"<strong>{len(preview_rows)}</strong><small>dans la preview</small></article>"
        "<article><i class='dashboard-icon is-alert'>!</i><span>FAILED</span>"
        f"<strong>{len(failed_rows)}</strong><small>à contrôler</small></article>"
        "</section>"
        "<section class='dashboard-grid'>"
        "<article class='dashboard-panel dashboard-upcoming'>"
        "<header><span>PROCHAINES PUBLICATIONS</span><b>Preview</b></header>"
        f"<div class='dashboard-list'>{upcoming_html}</div>"
        "</article>"
        "<article class='dashboard-panel dashboard-chart'>"
        "<header><span>PUBLICATIONS · 7 DERNIERS JOURS</span>"
        f"<b>{len(planned_rows)} planifiés</b></header>"
        "<div class='dashboard-chart-bars'>"
        f"{chart_items}"
        "</div>"
        "</article>"
        "<article class='dashboard-panel dashboard-accounts'>"
        "<header><span>VOLUME PAR COMPTE</span><b>Historique local</b></header>"
        f"<div class='dashboard-account-list'>{top_accounts_html}</div>"
        "</article>"
        "</section>",
        unsafe_allow_html=True,
    )
    dashboard_actions_a, dashboard_actions_b = st.columns([1, 1.25])
    with dashboard_actions_a:
        if st.button("Voir les comptes", key="dashboard_accounts", use_container_width=True):
            st.session_state["app_page"] = "accounts"
            st.rerun()
    with dashboard_actions_b:
        if st.button("Ouvrir la preview", key="dashboard_preview", use_container_width=True):
            st.session_state["app_page"] = "preview"
            st.rerun()


def render_sidebar_navigation(active_page: str, api_exists: bool, dry_run_default: bool) -> tuple[str, bool]:
    page_groups = [
        ("PLANIFICATION", [("dashboard", "Dashboard"), ("accounts", "Comptes"), ("cadence", "Cadence"), ("posts", "Posts"), ("preview", "Preview")]),
        ("ANALYSE", [("analytics", "Analytics")]),
        ("ENVOI", [("send", "Envoi Postoria"), ("tracking", "Suivi")]),
    ]
    with st.sidebar:
        st.markdown(
            "<div class='sidebar-brand'><i>P</i><div><b>Postoria</b><span>THREADS SCHEDULER</span></div></div>",
            unsafe_allow_html=True,
        )
        for section, pages in page_groups:
            st.markdown(f"<div class='sidebar-section'>{section}</div>", unsafe_allow_html=True)
            for page_key, label in pages:
                if st.button(
                    label,
                    key=f"sidebar_nav_{page_key}",
                    type="primary" if active_page == page_key else "secondary",
                    use_container_width=True,
                ):
                    active_page = page_key
                    st.session_state["app_page"] = page_key
        st.divider()
        with st.expander("Réglages", expanded=False):
            dry_run = st.toggle("Mode dry-run", value=dry_run_default, key="sidebar_dry_run")
            st.caption("API " + ("détectée" if api_exists else "manquante"))
            st.caption("Fuseau : " + APP_TZ)
        st.markdown("<div class='sidebar-section'>RECOMMENCER</div>", unsafe_allow_html=True)
        if st.button("Recommencer planning", key="sidebar_reset_planning", use_container_width=True):
            st.session_state["reset_dialog_mode"] = "planning"
        if st.button("Recommencer tout", key="sidebar_reset_all", use_container_width=True):
            st.session_state["reset_dialog_mode"] = "all"
    return active_page, bool(st.session_state.get("sidebar_dry_run", dry_run_default))


def render_metric_strip(metrics: list[tuple[str, str, str]]) -> None:
    items = []
    for label, value, helper in metrics:
        items.append(
            "<div class='metric-cell'>"
            f"<span>{h(label)}</span>"
            f"<strong>{h(value)}</strong>"
            f"<small>{h(helper)}</small>"
            "</div>"
        )
    st.markdown("<div class='metric-strip'>" + "".join(items) + "</div>", unsafe_allow_html=True)


def render_analytics(rows: list[dict]) -> None:
    df = analytics_dataframe(rows)
    if df.empty:
        render_locked_step(
            "Analytics indisponibles.",
            ["Génère une preview ou récupère des statuts Postoria pour créer les données d'analyse."],
        )
        return

    status_options = ["Tous"] + sorted(df["status"].dropna().astype(str).unique().tolist())
    group_options = ["Tous les groupes"] + sorted(df["group_name"].dropna().astype(str).unique().tolist())
    account_options = ["Tous les comptes"] + sorted(df["account_name"].dropna().astype(str).unique().tolist())

    f1, f2, f3 = st.columns(3)
    with f1:
        status_filter = choose_option("Statut", status_options, key="analytics_status")
    with f2:
        group_filter = choose_option("Groupe", group_options, key="analytics_group")
    with f3:
        account_filter = choose_option("Compte", account_options, key="analytics_account")

    filtered = df.copy()
    if status_filter != "Tous":
        filtered = filtered[filtered["status"].astype(str) == status_filter]
    if group_filter != "Tous les groupes":
        filtered = filtered[filtered["group_name"].astype(str) == group_filter]
    if account_filter != "Tous les comptes":
        filtered = filtered[filtered["account_name"].astype(str) == account_filter]

    total_posts = len(filtered)
    total_accounts = filtered["account_name"].nunique() if not filtered.empty else 0
    total_groups = filtered["group_name"].nunique() if not filtered.empty else 0
    total_days = filtered["day"].nunique() if not filtered.empty else 0
    render_metric_strip(
        [
            ("Posts", str(total_posts), "dans le filtre"),
            ("Comptes", str(total_accounts), "avec volume"),
            ("Groupes", str(total_groups), "avec volume"),
            ("Jours", str(total_days), "couverts"),
        ]
    )

    if filtered.empty:
        render_locked_step(
            "Aucun volume pour ces filtres.",
            ["Change le statut, le groupe ou le compte pour afficher les analytics."],
        )
        return

    st.markdown("#### Volumes simples")
    simple_tabs = st.tabs(["Par compte", "Par groupe", "Par jour", "Par semaine", "Par mois"])
    simple_specs = [
        (simple_tabs[0], ["account_name"], "account_name"),
        (simple_tabs[1], ["group_name"], "group_name"),
        (simple_tabs[2], ["day"], "day"),
        (simple_tabs[3], ["week"], "week"),
        (simple_tabs[4], ["month"], "month"),
    ]
    for tab, dimensions, chart_key in simple_specs:
        with tab:
            summary = count_by(filtered, dimensions)
            c1, c2 = st.columns([1.2, 1])
            with c1:
                st.dataframe(summary, use_container_width=True, hide_index=True, height=420)
            with c2:
                chart_data = summary.set_index(chart_key)["posts"] if chart_key in summary.columns else summary["posts"]
                st.bar_chart(chart_data)

    st.markdown("#### Matrices de contrôle")
    matrix_tabs = st.tabs([
        "Compte x jour",
        "Compte x semaine",
        "Compte x mois",
        "Groupe x jour",
        "Groupe x semaine",
        "Groupe x mois",
    ])
    matrix_specs = [
        (matrix_tabs[0], "account_name", "day"),
        (matrix_tabs[1], "account_name", "week"),
        (matrix_tabs[2], "account_name", "month"),
        (matrix_tabs[3], "group_name", "day"),
        (matrix_tabs[4], "group_name", "week"),
        (matrix_tabs[5], "group_name", "month"),
    ]
    for tab, index, columns in matrix_specs:
        with tab:
            matrix = pivot_counts(filtered, index, columns)
            st.dataframe(matrix, use_container_width=True, height=520)


def render_blocker_chips(blockers: list[str]) -> None:
    if not blockers:
        st.success("Envoi débloqué. Dernière vérification recommandée avant action réelle.")
        return
    chips = "".join(f"<span>{h(item)}</span>" for item in blockers)
    st.markdown(
        "<div class='send-blockers'>"
        "<strong>Envoi bloqué</strong>"
        f"<div>{chips}</div>"
        "</div>",
        unsafe_allow_html=True,
    )


def render_workspace_picker(client: PostoriaClient | None, key_prefix: str) -> int | str | None:
    current_workspace = st.session_state.get("workspace_id")
    if not client:
        st.info("Clé API Postoria manquante ou invalide. Ajoute les secrets avant l'envoi réel.")
        return current_workspace

    if st.button("Récupérer workspaces Postoria", key=f"{key_prefix}_load_workspaces"):
        try:
            st.session_state["workspaces"] = client.list_workspaces()
        except Exception as e:
            st.error(str(e))

    workspaces = st.session_state.get("workspaces", [])
    if not workspaces:
        st.info("Aucun workspace chargé. Clique sur Récupérer workspaces Postoria.")
        return current_workspace

    workspace_ids = [w["id"] for w in workspaces]
    current_index = 0
    if current_workspace is not None:
        for idx, workspace_id in enumerate(workspace_ids):
            if str(workspace_id) == str(current_workspace):
                current_index = idx
                break
    if len(workspace_ids) == 1:
        st.session_state["workspace_id"] = workspace_ids[0]
        st.success(f"Workspace sélectionné : {workspaces[0].get('name', workspace_ids[0])}")
        return workspace_ids[0]

    picked_workspace = choose_option(
        "Workspace Postoria",
        workspace_ids,
        index=current_index,
        format_func=lambda wid: next(str(w.get("name", wid)) for w in workspaces if str(w["id"]) == str(wid)),
        key=f"{key_prefix}_workspace_id",
    )
    st.session_state["workspace_id"] = picked_workspace
    return picked_workspace


def reset_workflow_state(clear_accounts: bool = True, clear_posts: bool = True, clear_preview: bool = True) -> None:
    if clear_preview:
        db.clear_preview()
        st.session_state.pop("preview_rows", None)
    if clear_posts:
        st.session_state["selected_posts"] = []
        st.session_state["posts_selection_explicit"] = False
    if clear_accounts:
        st.session_state["selected_accounts"] = []
        st.session_state["grouped_accounts"] = {}
        st.session_state["selected_group_filters"] = []
        st.session_state.pop("_account_group_signature", None)
        st.session_state.pop("_accounts_restored_from_db", None)
        for account in db.list_accounts():
            db.update_account_preferences(
                int(account["id"]),
                account.get("group_name") or "tous",
                bool(account.get("active_for_day", 1)),
                False,
            )
        for key in list(st.session_state.keys()):
            if key.startswith("account_use_"):
                st.session_state[key] = False


def clear_preview_draft(reason: str | None = None) -> bool:
    if db.list_scheduled("preview"):
        db.clear_preview()
        st.session_state.pop("preview_rows", None)
        st.session_state["preview_cleared_notice"] = reason or "Preview brouillon supprimée."
        return True
    return False


def render_group_form(form_key: str, close_on_save: bool = False) -> None:
    st.markdown(
        "<div class='group-form-intro'><strong>Nouveau groupe</strong>"
        "<span>Donne-lui un nom et une couleur. Tu pourras ensuite y classer tes comptes depuis le tableau.</span></div>",
        unsafe_allow_html=True,
    )
    name = st.text_input("Nom du groupe", placeholder="ex: w-u, tous, group 5 post", key=f"{form_key}_name")
    color_labels = [label for label, _, _ in GROUP_COLOR_CHOICES]
    color_state_key = f"{form_key}_color_label"
    st.session_state.setdefault(color_state_key, color_labels[0])
    st.markdown("<div class='group-colour-label'>Couleur</div>", unsafe_allow_html=True)
    color_cols = st.columns(len(color_labels))
    for index, label in enumerate(color_labels):
        dot = next(dot for choice, dot, _ in GROUP_COLOR_CHOICES if choice == label)
        selected = st.session_state[color_state_key] == label
        with color_cols[index]:
            if st.button(label, key=f"{form_key}_colour_{index}", type="primary" if selected else "secondary"):
                st.session_state[color_state_key] = label
                st.rerun()
    color_label = st.session_state[color_state_key]
    color = next(dot for label, dot, _ in GROUP_COLOR_CHOICES if label == color_label)
    st.markdown(
        f"<div class='group-colour-preview'><i style='background:{color}'></i><span>{h(color_label)}</span></div>",
        unsafe_allow_html=True,
    )
    if st.button("Créer le groupe", disabled=not name.strip(), key=f"{form_key}_save"):
        created = db.upsert_group(name, color=color)
        mark_group_config_dirty()
        st.success("Groupe créé." if created else "Groupe mis à jour.")
        if close_on_save:
            st.session_state["show_group_dialog"] = False
        st.rerun()


def render_create_group_dialog() -> None:
    if hasattr(st, "dialog"):
        @st.dialog("Créer un groupe")
        def _dialog() -> None:
            render_group_form("dialog_group", close_on_save=True)

        _dialog()
    else:
        with st.expander("Créer un groupe", expanded=True):
            render_group_form("inline_group", close_on_save=False)


def render_reset_dialog(mode: str) -> None:
    full_reset = mode == "all"
    title = "Recommencer tout" if full_reset else "Recommencer la planification"
    body = (
        "Vide la sélection de comptes, la sélection de posts et la preview brouillon. "
        "Les comptes, groupes, posts importés et posts déjà programmés/envoyés restent en base."
        if full_reset
        else "Efface seulement la preview brouillon. Les comptes, posts sélectionnés et posts déjà programmés/envoyés restent en place."
    )

    def _content() -> None:
        st.write(body)
        left, right = st.columns(2)
        if left.button("Annuler", key=f"cancel_reset_{mode}"):
            st.session_state.pop("reset_dialog_mode", None)
            st.rerun()
        if right.button("Confirmer", key=f"confirm_reset_{mode}", type="primary"):
            reset_workflow_state(
                clear_accounts=full_reset,
                clear_posts=full_reset,
                clear_preview=True,
            )
            st.session_state.pop("reset_dialog_mode", None)
            st.rerun()

    if hasattr(st, "dialog"):
        @st.dialog(title)
        def _dialog() -> None:
            _content()

        _dialog()
    else:
        with st.expander(title, expanded=True):
            _content()


st.set_page_config(page_title="Postoria Threads Scheduler", layout="wide")
st.markdown(
    """
    <style>
    :root {
        --bg: #111318;
        --panel: rgba(244, 246, 251, .042);
        --panel-strong: rgba(244, 246, 251, .07);
        --line: rgba(226, 232, 240, .12);
        --line-strong: rgba(226, 232, 240, .2);
        --text: rgba(248, 250, 252, .94);
        --muted: rgba(203, 213, 225, .66);
        --faint: rgba(148, 163, 184, .48);
        --accent: #df4d6e;
        --accent-soft: rgba(223, 77, 110, .13);
        --success: #2fbf7b;
        --success-soft: rgba(47, 191, 123, .13);
        --warn: #e7b958;
        --warn-soft: rgba(231, 185, 88, .13);
    }
    .stApp {
        background:
            radial-gradient(circle at 18% 0%, rgba(223, 77, 110, .08), transparent 28rem),
            linear-gradient(180deg, #141720 0%, var(--bg) 34rem);
        color: var(--text);
        font-family: Geist, Satoshi, "SF Pro Display", -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    .block-container {padding-top: 1.15rem; max-width: 1360px;}
    h1, h2, h3 {letter-spacing: 0;}
    [data-testid="stSidebar"] {
        background: rgba(12, 14, 19, .72);
        border-right: 1px solid var(--line);
    }
    [data-testid="stCaptionContainer"] p {line-height: 1.55; color: var(--muted);}
    div[data-testid="stMetric"] {
        border: 1px solid var(--line);
        border-radius: 8px;
        padding: 12px 14px;
        background: var(--panel);
    }
    div[data-testid="stMetric"] label {
        color: var(--muted);
        font-size: .82rem;
    }
    div[data-testid="stMetricValue"] {
        font-size: 2rem;
        font-variant-numeric: tabular-nums;
    }
    div[data-testid="stDataFrame"] {
        border: 1px solid var(--line);
        border-radius: 8px;
        overflow: hidden;
    }
    div[data-testid="stButton"] button, div[data-testid="stFormSubmitButton"] button {
        border-radius: 8px;
        border: 1px solid var(--line-strong);
        transition: transform .22s cubic-bezier(.16, 1, .3, 1), border-color .22s cubic-bezier(.16, 1, .3, 1), background .22s cubic-bezier(.16, 1, .3, 1);
    }
    div[data-testid="stButton"] button:hover, div[data-testid="stFormSubmitButton"] button:hover {
        border-color: rgba(223, 77, 110, .58);
        background: rgba(223, 77, 110, .09);
    }
    div[data-testid="stButton"] button:active, div[data-testid="stFormSubmitButton"] button:active {
        transform: translateY(1px) scale(.99);
    }
    .app-hero {
        display: grid;
        grid-template-columns: minmax(0, 1.65fr) minmax(260px, .75fr);
        gap: 24px;
        align-items: end;
        border-bottom: 1px solid var(--line);
        padding: 12px 0 22px;
        margin-bottom: 18px;
    }
    .app-hero .eyebrow {
        color: var(--accent);
        display: inline-block;
        font-size: .78rem;
        font-weight: 800;
        letter-spacing: .14em;
        text-transform: uppercase;
        margin-bottom: 10px;
    }
    .app-hero h1 {
        margin: 0;
        font-size: clamp(2.05rem, 4vw, 3.7rem);
        line-height: .96;
        font-weight: 820;
    }
    .app-hero p {
        color: var(--muted);
        margin: 14px 0 0;
        max-width: 68ch;
        line-height: 1.5;
    }
    .hero-status {
        display: flex;
        justify-content: flex-end;
        align-items: flex-end;
        gap: 8px;
        flex-wrap: wrap;
    }
    .status-pill {
        border: 1px solid var(--line);
        border-radius: 999px;
        padding: 7px 10px;
        color: var(--muted);
        background: var(--panel);
        font-size: .8rem;
        font-weight: 720;
        white-space: nowrap;
    }
    .status-pill.ok {
        border-color: rgba(47, 191, 123, .34);
        color: rgba(211, 250, 229, .92);
        background: var(--success-soft);
    }
    .status-pill.warn {
        border-color: rgba(231, 185, 88, .34);
        color: rgba(255, 237, 193, .92);
        background: var(--warn-soft);
    }
    .metric-strip {
        display: grid;
        grid-template-columns: 1.15fr .95fr 1.05fr .85fr;
        gap: 1px;
        border: 1px solid var(--line);
        border-radius: 10px;
        overflow: hidden;
        background: var(--line);
        margin: 12px 0 18px;
    }
    .metric-cell {
        background: rgba(17, 19, 24, .76);
        padding: 14px 16px;
    }
    .metric-cell span, .metric-cell small {
        display: block;
        color: var(--faint);
        font-size: .76rem;
        font-weight: 700;
        letter-spacing: .055em;
        text-transform: uppercase;
    }
    .metric-cell strong {
        display: block;
        color: var(--text);
        font-size: 1.55rem;
        margin: 5px 0 3px;
        font-variant-numeric: tabular-nums;
        line-height: 1;
    }
    .metric-cell small {
        color: var(--muted);
        font-size: .74rem;
        text-transform: none;
        letter-spacing: 0;
        font-weight: 500;
    }
    .flow-rail {
        display: grid;
        grid-template-columns: 1.15fr .85fr 1.05fr .8fr .95fr .8fr;
        gap: 8px;
        margin: 8px 0 18px;
    }
    .flow-step {
        display: block;
        position: relative;
        border: 1px solid var(--line);
        border-radius: 8px;
        padding: 13px 14px 12px;
        min-height: 82px;
        background: var(--panel);
        overflow: hidden;
        text-decoration: none !important;
        transition: transform .25s cubic-bezier(.16, 1, .3, 1), border-color .25s cubic-bezier(.16, 1, .3, 1);
    }
    .flow-step:hover {transform: translateY(-1px); border-color: var(--line-strong);}
    .flow-step span {
        display: inline-grid;
        place-items: center;
        width: 24px;
        height: 24px;
        border-radius: 999px;
        border: 1px solid var(--line-strong);
        color: var(--muted);
        font-size: .76rem;
        font-weight: 800;
        margin-bottom: 9px;
    }
    .flow-step strong, .flow-step small {display: block;}
    .flow-step strong {font-size: .94rem;}
    .flow-step small {color: var(--muted); margin-top: 2px; font-size: .8rem;}
    .flow-step.is-done {
        background: linear-gradient(180deg, rgba(47, 191, 123, .12), rgba(244, 246, 251, .035));
        border-color: rgba(47, 191, 123, .34);
    }
    .flow-step.is-done span {
        color: rgba(211, 250, 229, .95);
        border-color: rgba(47, 191, 123, .45);
        background: rgba(47, 191, 123, .16);
    }
    .flow-step.is-active {
        background: linear-gradient(180deg, rgba(223, 77, 110, .16), rgba(244, 246, 251, .035));
        border-color: rgba(223, 77, 110, .52);
    }
    .flow-step.is-active span {
        color: rgba(255, 224, 231, .96);
        border-color: rgba(223, 77, 110, .58);
        background: rgba(223, 77, 110, .18);
    }
    .step-note {
        border: 1px solid var(--line);
        border-radius: 8px;
        padding: 17px 18px;
        background: var(--panel);
        min-height: 92px;
        margin: 8px 0 10px;
    }
    .section-intro {
        border-top: 1px solid var(--line);
        border-bottom: 1px solid var(--line);
        border-radius: 8px;
        padding: 16px 18px;
        margin: 10px 0 18px 0;
        background: linear-gradient(90deg, rgba(244, 246, 251, .05), rgba(244, 246, 251, .018));
    }
    .section-intro span {
        display: inline-block;
        color: var(--accent);
        font-size: .78rem;
        font-weight: 700;
        letter-spacing: .08em;
        text-transform: uppercase;
        margin-bottom: 6px;
    }
    .section-intro strong {
        display: block;
        font-size: 1.12rem;
        margin-bottom: 4px;
    }
    .section-intro p {
        color: var(--muted);
        margin: 0;
        line-height: 1.45;
    }
    .blocked-panel {
        border: 1px solid rgba(231, 185, 88, .28);
        border-radius: 10px;
        background: linear-gradient(180deg, rgba(231, 185, 88, .10), rgba(244, 246, 251, .035));
        padding: 16px 18px;
        margin: 8px 0 18px;
    }
    .blocked-panel .blocked-kicker {
        color: var(--warn);
        font-size: .75rem;
        font-weight: 800;
        letter-spacing: .12em;
        text-transform: uppercase;
        margin-bottom: 6px;
    }
    .blocked-panel strong {display: block; margin-bottom: 8px;}
    .blocked-panel ul {margin: 0; padding-left: 18px; color: var(--muted); line-height: 1.55;}
    .group-strip {
        display: flex;
        gap: 14px;
        overflow-x: auto;
        padding: 6px 0 18px;
        margin: 8px 0 16px;
    }
    .group-chip {
        min-width: 176px;
        border: 1px solid var(--line);
        border-radius: 999px;
        padding: 11px 14px;
        display: grid;
        grid-template-columns: minmax(0, 1fr) auto;
        gap: 2px 8px;
        align-items: center;
        background: rgba(244, 246, 251, .032);
    }
    .group-chip span {
        display: inline-flex;
        align-items: center;
        gap: 8px;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
        font-weight: 750;
    }
    .group-chip span i {
        width: 9px;
        height: 9px;
        border-radius: 999px;
        flex: 0 0 auto;
    }
    .group-chip b {
        font-variant-numeric: tabular-nums;
        color: var(--accent);
    }
    .group-chip small {
        grid-column: 1 / -1;
        color: var(--faint);
        font-size: .74rem;
    }
    .group-chip.is-active {
        border-color: rgba(223, 77, 110, .38);
        background: var(--accent-soft);
    }
    .send-blockers {
        border: 1px solid rgba(223, 77, 110, .28);
        background: rgba(223, 77, 110, .09);
        border-radius: 10px;
        padding: 14px 16px;
        margin: 10px 0 16px;
    }
    .send-blockers strong {
        display: block;
        margin-bottom: 9px;
    }
    .send-blockers div {
        display: flex;
        gap: 8px;
        flex-wrap: wrap;
    }
    .send-blockers span {
        border: 1px solid rgba(223, 77, 110, .32);
        border-radius: 999px;
        padding: 5px 9px;
        color: rgba(255, 214, 223, .94);
        background: rgba(17, 19, 24, .35);
        font-size: .78rem;
    }
    .accounts-shell {
        border: 1px solid var(--line);
        border-radius: 12px;
        background: rgba(17, 19, 24, .58);
        overflow: hidden;
        margin-top: 14px;
    }
    .accounts-shell-lite {
        border: 1px solid var(--line);
        border-radius: 12px 12px 0 0;
        background: rgba(17, 19, 24, .58);
        overflow: hidden;
        margin-top: 22px;
    }
    .accounts-toolbar {
        display: grid;
        grid-template-columns: minmax(220px, .9fr) minmax(320px, 1fr) auto;
        gap: 14px;
        align-items: end;
        padding: 14px 16px;
        border-bottom: 1px solid var(--line);
        background: rgba(244, 246, 251, .028);
    }
    .accounts-count {
        color: var(--muted);
        font-weight: 740;
        font-variant-numeric: tabular-nums;
        text-align: right;
        padding-bottom: 10px;
    }
    .account-table-head {
        display: grid;
        grid-template-columns: 44px 2.2fr 1.1fr 1fr 44px;
        gap: 16px;
        align-items: center;
        padding: 16px 20px;
        border-bottom: 1px solid var(--line);
        color: var(--faint);
        font-size: .74rem;
        font-weight: 820;
        letter-spacing: .14em;
        text-transform: uppercase;
    }
    .account-row-divider {
        border-top: 1px solid var(--line);
        margin: 2px -16px;
    }
    .account-person {
        display: grid;
        grid-template-columns: 48px minmax(0, 1fr);
        gap: 14px;
        align-items: center;
        min-height: 68px;
    }
    .account-avatar, .account-avatar-fallback {
        width: 44px;
        height: 44px;
        border-radius: 50%;
        border: 1px solid rgba(255, 255, 255, .1);
        overflow: hidden;
        display: grid;
        place-items: center;
        background: linear-gradient(135deg, rgba(223,77,110,.28), rgba(100,116,139,.18));
        color: rgba(248,250,252,.9);
        font-size: .82rem;
        font-weight: 850;
    }
    .account-avatar img {
        width: 100%;
        height: 100%;
        object-fit: cover;
        display: block;
    }
    .account-name strong, .account-name small {
        display: block;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
    }
    .account-name strong {
        color: var(--text);
        font-size: .98rem;
        line-height: 1.2;
    }
    .account-name small {
        color: var(--muted);
        margin-top: 3px;
    }
    .account-group-badge {
        display: inline-flex;
        align-items: center;
        max-width: 100%;
        gap: 7px;
        border-radius: 8px;
        padding: 8px 11px;
        font-size: .84rem;
        font-weight: 760;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
    }
    .account-group-badge i {
        width: 8px;
        height: 14px;
        border-radius: 999px;
        flex: 0 0 auto;
    }
    .next-post-pill, .status-text {
        color: var(--muted);
        font-variant-numeric: tabular-nums;
        font-weight: 720;
    }
    .account-actions {
        display: inline-flex;
        gap: 8px;
        align-items: center;
        color: var(--muted);
    }
    .account-actions a {
        color: var(--muted) !important;
        text-decoration: none;
        border: 1px solid var(--line);
        border-radius: 8px;
        padding: 6px 8px;
        transition: border-color .22s cubic-bezier(.16, 1, .3, 1), background .22s cubic-bezier(.16, 1, .3, 1);
    }
    .account-actions a:hover {
        border-color: rgba(223, 77, 110, .45);
        background: rgba(223, 77, 110, .08);
    }
    .account-selection-summary {
        display: grid;
        grid-template-columns: repeat(3, minmax(0, 1fr));
        gap: 1px;
        border: 1px solid var(--line);
        border-radius: 10px;
        overflow: hidden;
        background: var(--line);
        margin: 18px 0 18px;
    }
    .account-selection-summary div {
        background: rgba(17, 19, 24, .66);
        padding: 13px 15px;
    }
    .account-selection-summary span {
        display: block;
        color: var(--faint);
        font-size: .72rem;
        font-weight: 800;
        letter-spacing: .08em;
        text-transform: uppercase;
    }
    .account-selection-summary strong {
        display: block;
        margin-top: 3px;
        color: var(--text);
        font-size: 1.12rem;
        font-variant-numeric: tabular-nums;
    }
    .selected-panel {
        border: 1px solid var(--line);
        border-radius: 12px;
        background: rgba(17, 19, 24, .58);
        padding: 13px;
        position: sticky;
        top: 12px;
    }
    .selected-panel h4 {
        margin: 0 0 10px;
        font-size: .9rem;
    }
    .selected-account-chip {
        display: grid;
        grid-template-columns: minmax(0, 1fr) auto;
        gap: 8px;
        align-items: center;
        border-top: 1px solid var(--line);
        padding: 9px 0;
        color: var(--muted);
        font-size: .82rem;
    }
    .selected-account-chip strong {
        display: block;
        color: var(--text);
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
    }
    .posts-control-panel {
        border: 1px solid var(--line);
        border-radius: 12px;
        background: linear-gradient(180deg, rgba(244, 246, 251, .052), rgba(244, 246, 251, .026));
        padding: 14px 16px;
        margin: 16px 0 14px;
    }
    .posts-control-panel strong {
        display: block;
        color: var(--text);
        font-size: 1rem;
        margin-bottom: 4px;
    }
    .posts-control-panel p {
        margin: 0;
        color: var(--muted);
        line-height: 1.45;
    }
    .post-library-header {
        display: flex;
        align-items: flex-end;
        justify-content: space-between;
        gap: 20px;
        padding: 4px 0 14px;
        border-bottom: 1px solid var(--line);
        margin-bottom: 14px;
    }
    .post-library-header span,
    .post-batch-state span {
        display: block;
        color: var(--faint);
        font-size: .72rem;
        font-weight: 820;
        letter-spacing: .09em;
        text-transform: uppercase;
    }
    .post-library-header h3 {
        margin: 4px 0 5px;
        font-size: 1.7rem;
    }
    .post-library-header p {
        margin: 0;
        color: var(--muted);
        font-size: .92rem;
    }
    .post-library-header > b {
        flex: 0 0 auto;
        color: var(--accent);
        font-size: .88rem;
        font-variant-numeric: tabular-nums;
        padding: 7px 10px;
        border: 1px solid rgba(244, 63, 94, .28);
        border-radius: 7px;
        background: rgba(244, 63, 94, .08);
    }
    .post-batch-toolbar {
        margin: 14px 0 10px;
    }
    .post-batch-state {
        min-height: 42px;
        padding: 4px 0;
    }
    .post-batch-state strong {
        display: block;
        margin-top: 3px;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
        color: var(--text);
        font-size: .9rem;
    }
    .post-library-summary {
        min-height: 42px;
        display: flex;
        flex-direction: column;
        justify-content: center;
    }
    .post-library-summary strong {
        color: var(--text);
        font-size: .95rem;
    }
    .post-library-summary span {
        color: var(--muted);
        font-size: .82rem;
        margin-top: 2px;
    }
    .post-library-pane-title,
    .post-preview-pane-title {
        color: var(--faint);
        font-size: .73rem;
        font-weight: 820;
        letter-spacing: .09em;
        text-transform: uppercase;
        padding: 0 0 9px;
        border-bottom: 1px solid var(--line);
        margin-bottom: 0;
    }
    .post-list-head {
        display: grid;
        grid-template-columns: .54fr .72fr 5.5fr 1.2fr .82fr;
        gap: 8px;
        align-items: center;
        min-height: 36px;
        padding: 0 5px 9px;
        border-bottom: 1px solid var(--line);
        color: var(--faint);
        font-size: .71rem;
        font-weight: 820;
        letter-spacing: .08em;
        text-transform: uppercase;
    }
    .post-row-type {
        width: 42px;
        height: 42px;
        display: grid;
        place-items: center;
        border-radius: 9px;
        background: rgba(9, 9, 11, .56);
        color: var(--muted);
        font-size: 1rem;
        font-weight: 760;
    }
    .post-row-caption {
        min-height: 42px;
        display: -webkit-box;
        -webkit-box-orient: vertical;
        -webkit-line-clamp: 2;
        overflow: hidden;
        color: var(--text);
        font-weight: 650;
        line-height: 1.42;
        padding-top: 5px;
    }
    .post-row-meta {
        min-height: 42px;
        display: flex;
        align-items: center;
        color: var(--muted);
        font-size: .84rem;
        font-variant-numeric: tabular-nums;
    }
    .post-row-divider {
        height: 1px;
        margin: 0 -1rem;
        background: var(--line);
    }
    .post-preview-empty,
    .post-preview-card {
        min-height: 650px;
        padding: 28px 24px;
    }
    .post-preview-empty {
        display: flex;
        flex-direction: column;
        align-items: center;
        justify-content: center;
        text-align: center;
        gap: 8px;
    }
    .post-preview-symbol {
        width: 52px;
        height: 52px;
        display: grid;
        place-items: center;
        border-radius: 12px;
        background: rgba(244, 63, 94, .10);
        color: var(--accent);
        font-size: 1.25rem;
        font-weight: 780;
        margin-bottom: 5px;
    }
    .post-preview-empty strong {
        color: var(--text);
        font-size: 1.05rem;
    }
    .post-preview-empty span {
        max-width: 280px;
        color: var(--muted);
        line-height: 1.5;
    }
    .post-preview-card {
        display: flex;
        flex-direction: column;
        gap: 18px;
    }
    .post-preview-card-head {
        display: flex;
        justify-content: space-between;
        align-items: center;
        gap: 12px;
    }
    .post-preview-card-head span {
        color: var(--faint);
        font-size: .72rem;
        font-weight: 820;
        letter-spacing: .08em;
        text-transform: uppercase;
    }
    .post-preview-card-head b {
        color: var(--accent);
        background: rgba(244, 63, 94, .10);
        padding: 5px 8px;
        border-radius: 6px;
        font-size: .76rem;
        font-weight: 720;
    }
    .post-preview-card p {
        margin: 4px 0 auto;
        white-space: pre-wrap;
        color: var(--text);
        font-size: 1.04rem;
        line-height: 1.62;
    }
    .post-preview-card-meta {
        display: flex;
        gap: 10px;
        flex-wrap: wrap;
        padding-top: 15px;
        border-top: 1px solid var(--line);
    }
    .post-preview-card-meta span {
        color: var(--muted);
        font-size: .82rem;
    }
    /* Post library: compact Finder navigation, dense list, full-height preview. */
    div[data-testid="stVerticalBlockBorderWrapper"]:has(.post-library-finder-title),
    div[data-testid="stVerticalBlockBorderWrapper"]:has(.post-library-list-title),
    div[data-testid="stVerticalBlockBorderWrapper"]:has(.post-preview-pane-title) {
        min-width: 0;
        background: rgba(24, 24, 27, .68) !important;
        padding: 14px !important;
    }
    .post-library-finder-title {
        color: var(--faint);
        font-size: .71rem;
        font-weight: 820;
        letter-spacing: .1em;
        padding: 2px 0 5px;
    }
    .post-library-finder-title.is-subtitle {
        border-top: 1px solid var(--line);
        margin-top: 2px;
        padding-top: 14px;
    }
    .post-library-list-title {
        display: flex;
        align-items: end;
        justify-content: space-between;
        gap: 12px;
        padding-bottom: 6px;
    }
    .post-library-list-title div {display: grid; gap: 3px;}
    .post-library-list-title span,
    .post-library-list-title b {
        color: var(--faint);
        font-size: .71rem;
        font-weight: 820;
        letter-spacing: .09em;
    }
    .post-library-list-title strong {
        color: var(--text);
        font-size: 1rem;
    }
    .post-library-list-title b {
        color: var(--accent);
        letter-spacing: 0;
        text-transform: none;
        white-space: nowrap;
    }
    div[data-testid="stVerticalBlockBorderWrapper"]:has(.post-library-finder-title) [data-testid="stExpander"],
    div[data-testid="stVerticalBlockBorderWrapper"]:has(.post-library-finder-title) [data-testid="stButton"] button {
        border-radius: 8px !important;
    }
    div[data-testid="stVerticalBlockBorderWrapper"]:has(.post-library-finder-title) [data-testid="stButton"] button {
        min-height: 36px !important;
        padding: 7px 10px !important;
        justify-content: flex-start !important;
        text-align: left !important;
    }
    div[data-testid="stVerticalBlockBorderWrapper"]:has(.post-library-finder-title) [data-testid="stButton"] button p {
        text-align: left !important;
        font-size: .82rem;
        font-weight: 630 !important;
    }
    div[data-testid="stVerticalBlockBorderWrapper"]:has(.post-library-finder-title) [data-testid="stExpander"] summary {
        min-height: 36px !important;
        padding-top: 0 !important;
        padding-bottom: 0 !important;
        font-size: .84rem;
    }
    div[data-testid="stVerticalBlockBorderWrapper"]:has(.post-library-finder-title) [data-testid="stFileUploader"] section {
        min-height: 112px !important;
        padding: 13px !important;
    }
    div[data-testid="stVerticalBlockBorderWrapper"]:has(.post-library-finder-title) [data-testid="stFileUploader"] section > div {
        min-height: 86px !important;
        gap: 10px !important;
        align-items: center !important;
    }
    div[data-testid="stVerticalBlockBorderWrapper"]:has(.post-library-finder-title) [data-testid="stFileUploader"] small {
        line-height: 1.35 !important;
        text-align: center !important;
    }
    div[data-testid="stVerticalBlockBorderWrapper"]:has(.post-library-finder-title) [data-testid="stFileUploader"] button {
        min-height: 34px !important;
        padding: 6px 10px !important;
        white-space: nowrap !important;
    }
    .post-list-head-new {
        grid-template-columns: .44fr 8.5fr .9fr .85fr 1.15fr;
        gap: 12px;
        margin-top: 14px;
        min-height: 42px;
        padding-left: 0;
        padding-right: 0;
    }
    .post-library-row-caption {
        display: grid;
        grid-template-columns: 40px minmax(0, 1fr);
        gap: 14px;
        align-items: center;
        min-height: 82px;
        padding: 8px 0;
    }
    .post-library-row-caption i {
        width: 40px;
        height: 40px;
        display: grid;
        place-items: center;
        border-radius: 8px;
        background: rgba(9, 9, 11, .56);
        color: var(--muted);
        font-style: normal;
        font-size: .88rem;
        font-weight: 760;
    }
    .post-library-row-caption span {
        min-width: 0;
        overflow: hidden;
        display: -webkit-box;
        -webkit-box-orient: vertical;
        -webkit-line-clamp: 3;
        color: var(--text);
        font-size: 1rem;
        font-weight: 620;
        line-height: 1.48;
    }
    .post-library-row-meta {
        min-height: 82px;
        display: flex;
        align-items: center;
        color: var(--muted);
        font-size: .84rem;
        font-variant-numeric: tabular-nums;
    }
    div[data-testid="stVerticalBlockBorderWrapper"]:has(.post-library-list-title) [data-testid="stButton"] button {
        min-height: 38px !important;
        padding: 8px 10px !important;
        font-size: .8rem !important;
    }
    div[data-testid="stVerticalBlockBorderWrapper"]:has(.post-library-list-title) [data-testid="stCheckbox"] {
        display: flex;
        min-height: 82px;
        align-items: center;
        justify-content: center;
    }
    .post-library-empty {
        min-height: 460px;
        display: grid;
        place-items: center;
        text-align: center;
        color: var(--muted);
        font-size: .9rem;
    }
    div[data-testid="stDataFrame"]:has([aria-label*="Texte du post"]),
    div[data-testid="stDataFrameResizable"]:has([aria-label*="Texte du post"]) {
        margin: 10px 0 14px !important;
        border: 1px solid var(--line);
        border-radius: 10px;
        overflow: hidden;
    }
    @media (max-width: 760px) {
        .post-library-header {
            align-items: flex-start;
            flex-direction: column;
            gap: 10px;
        }
    }
    .posts-stats {
        display: grid;
        grid-template-columns: repeat(4, minmax(0, 1fr));
        gap: 1px;
        border: 1px solid var(--line);
        border-radius: 10px;
        overflow: hidden;
        background: var(--line);
        margin: 10px 0 14px;
    }
    .posts-stats div {
        background: rgba(17, 19, 24, .68);
        padding: 12px 14px;
    }
    .posts-stats span {
        display: block;
        color: var(--faint);
        font-size: .72rem;
        font-weight: 820;
        letter-spacing: .08em;
        text-transform: uppercase;
    }
    .posts-stats b {
        display: block;
        margin-top: 4px;
        color: var(--text);
        font-size: 1.18rem;
        font-variant-numeric: tabular-nums;
    }
    .post-readable-grid {
        display: grid;
        grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
        gap: 14px;
        margin: 14px 0 18px;
    }
    .post-readable-card {
        border: 1px solid rgba(255,255,255,.10);
        border-radius: 14px;
        background: linear-gradient(180deg, rgba(24,24,27,.74), rgba(12,14,18,.58));
        padding: 16px;
        min-width: 0;
    }
    .post-readable-card.is-selected {
        border-color: rgba(244,63,94,.38);
        box-shadow: inset 0 1px 0 rgba(255,255,255,.06);
    }
    .post-readable-top {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 12px;
        margin-bottom: 10px;
    }
    .post-readable-top span,
    .post-readable-top b {
        color: var(--faint);
        font-size: .76rem;
        font-weight: 820;
        letter-spacing: .06em;
        text-transform: uppercase;
    }
    .post-readable-card.is-selected .post-readable-top b {
        color: var(--accent);
    }
    .post-readable-card p {
        color: rgba(244,244,245,.94);
        font-size: .98rem;
        line-height: 1.55;
        white-space: normal;
        overflow-wrap: anywhere;
        margin: 0 0 14px;
    }
    .post-readable-meta {
        display: flex;
        flex-wrap: wrap;
        gap: 7px;
    }
    .post-readable-meta small {
        border: 1px solid rgba(255,255,255,.09);
        border-radius: 999px;
        background: rgba(255,255,255,.04);
        color: var(--muted);
        padding: 5px 8px;
        font-size: .74rem;
        max-width: 100%;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
    }
    .post-library-page-info {
        min-height: 36px;
        display: grid;
        place-items: center;
        color: var(--muted);
        font-size: .82rem;
        font-variant-numeric: tabular-nums;
    }
    .post-readable-meta small.has-media {
        color: rgba(220,252,231,.92);
        border-color: rgba(34,197,94,.26);
        background: rgba(34,197,94,.09);
    }
    .post-pick-shell {
        border: 1px solid rgba(255,255,255,.10);
        border-radius: 14px;
        background: linear-gradient(180deg, rgba(24,24,27,.82), rgba(12,14,18,.62));
        padding: 18px 18px 14px;
        min-height: 190px;
        box-shadow: inset 0 1px 0 rgba(255,255,255,.05);
    }
    .post-pick-head {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 12px;
        margin-bottom: 12px;
    }
    .post-pick-head span,
    .post-pick-head b {
        color: var(--faint);
        font-size: .74rem;
        font-weight: 820;
        letter-spacing: .06em;
        text-transform: uppercase;
    }
    .post-pick-head b {
        color: var(--accent);
    }
    .post-pick-shell p {
        color: rgba(244,244,245,.96);
        font-size: 1.02rem;
        line-height: 1.62;
        margin: 0 0 16px;
        overflow-wrap: anywhere;
        white-space: normal;
    }
    div[data-testid="stVerticalBlock"]:has(> div .post-pick-shell) {
        border: 1px solid rgba(255,255,255,.08);
        border-radius: 16px;
        padding: 10px 10px 8px;
        background: rgba(255,255,255,.025);
    }
    .import-batch-grid {
        display: grid;
        grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
        gap: 12px;
        margin: 12px 0 18px;
    }
    .import-batch-card {
        border: 1px solid rgba(255,255,255,.10);
        border-radius: 14px;
        background: rgba(17,19,24,.62);
        padding: 14px;
        min-width: 0;
    }
    .import-batch-card.is-active {
        border-color: rgba(244,63,94,.42);
        background: linear-gradient(180deg, rgba(244,63,94,.13), rgba(17,19,24,.66));
    }
    .import-batch-card strong,
    .import-batch-card span {
        display: block;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
    }
    .import-batch-card strong {
        color: var(--text);
        font-size: .92rem;
    }
    .import-batch-card span {
        color: var(--faint);
        font-size: .74rem;
        margin-top: 3px;
    }
    .import-batch-card div {
        display: grid;
        grid-template-columns: repeat(4, minmax(0, 1fr));
        gap: 6px;
        margin-top: 12px;
    }
    .import-batch-card b,
    .import-batch-card small {
        display: block;
        text-align: center;
    }
    .import-batch-card b {
        color: var(--text);
        font-size: 1rem;
        font-variant-numeric: tabular-nums;
    }
    .import-batch-card small {
        color: var(--faint);
        font-size: .66rem;
        text-transform: uppercase;
        letter-spacing: .05em;
    }
    .posts-editor-wrap {
        border: 1px solid var(--line);
        border-radius: 12px;
        background: rgba(17, 19, 24, .58);
        padding: 12px;
        margin-top: 10px;
    }
    .posts-editor-wrap div[data-testid="stDataFrame"] {
        border-color: rgba(226, 232, 240, .16);
    }
    .posts-editor-wrap [data-testid="stDataFrame"] div[role="columnheader"] {
        color: rgba(203, 213, 225, .78);
        font-weight: 780;
    }
    .group-color-preview {
        display: flex;
        gap: 8px;
        margin: 4px 0 12px;
    }
    .group-color-preview span {
        width: 22px;
        height: 22px;
        border-radius: 999px;
        border: 1px solid rgba(255,255,255,.18);
    }
    .mobile-account-card {
        display: none;
    }
    .warn-copy {color: var(--accent); font-weight: 700;}

    /* shadcn-inspired Streamlit skin */
    :root {
        --bg: #09090b;
        --panel: #0f1117;
        --panel-strong: #18181b;
        --line: rgba(255, 255, 255, .10);
        --line-strong: rgba(255, 255, 255, .18);
        --text: #fafafa;
        --muted: #a1a1aa;
        --faint: #71717a;
        --accent: #f43f5e;
        --accent-soft: rgba(244, 63, 94, .12);
        --success: #22c55e;
        --success-soft: rgba(34, 197, 94, .10);
        --warn: #eab308;
        --warn-soft: rgba(234, 179, 8, .10);
        --radius: 12px;
    }
    .stApp {
        background:
            radial-gradient(circle at 12% -8%, rgba(244, 63, 94, .07), transparent 34rem),
            linear-gradient(180deg, #09090b 0%, #0a0a0d 100%);
        color: var(--text);
    }
    .block-container {
        max-width: 1380px;
        margin-left: auto;
        margin-right: auto;
        padding-top: 1.4rem;
        padding-bottom: 4rem;
    }
    h1, h2, h3, h4 {
        color: var(--text);
        font-weight: 760;
        letter-spacing: -.01em;
    }
    [data-testid="stSidebar"] {
        background: rgba(9, 9, 11, .94);
        border-right: 1px solid var(--line);
    }
    [data-testid="stSidebar"] [data-testid="stMarkdownContainer"] p,
    [data-testid="stSidebar"] label {
        color: var(--muted);
    }
    .app-hero, .section-intro, .step-note, .blocked-panel,
    .posts-control-panel, .posts-editor-wrap, .selected-panel,
    .accounts-shell, .accounts-shell-lite, .send-blockers {
        border-radius: var(--radius);
        border: 1px solid var(--line);
        background: rgba(24, 24, 27, .72);
        box-shadow: 0 1px 0 rgba(255,255,255,.03) inset, 0 18px 60px rgba(0,0,0,.18);
    }
    .app-hero {
        padding: 22px 24px;
        margin-bottom: 20px;
        text-align: center;
    }
    .section-intro {
        padding: 18px 20px;
        background: linear-gradient(180deg, rgba(24,24,27,.86), rgba(15,17,23,.74));
        text-align: center;
    }
    .section-intro span, .app-hero .eyebrow {
        color: var(--accent);
        letter-spacing: .11em;
    }
    div[data-testid="stButton"] button,
    div[data-testid="stFormSubmitButton"] button,
    [data-testid="baseButton-secondary"],
    [data-testid="baseButton-primary"] {
        display: inline-flex !important;
        align-items: center !important;
        justify-content: center !important;
        min-height: 42px;
        padding: 10px 16px !important;
        border-radius: 10px;
        border: 1px solid var(--line-strong) !important;
        background: #18181b !important;
        color: var(--text) !important;
        font-weight: 650;
        box-shadow: 0 1px 0 rgba(255,255,255,.04) inset;
    }
    div[data-testid="stButton"] button:hover,
    div[data-testid="stFormSubmitButton"] button:hover {
        background: #27272a !important;
        border-color: rgba(244, 63, 94, .55) !important;
        transform: translateY(-1px);
    }
    div[data-testid="stButton"] button:active,
    div[data-testid="stFormSubmitButton"] button:active {
        transform: translateY(0) scale(.99);
    }
    div[data-testid="stButton"] button[kind="primary"],
    div[data-testid="stFormSubmitButton"] button[kind="primary"],
    [data-testid="baseButton-primary"] {
        background: var(--accent) !important;
        border-color: rgba(244, 63, 94, .85) !important;
        color: #fff !important;
    }
    div[data-testid="stTextInput"] input,
    div[data-testid="stNumberInput"] input,
    div[data-testid="stDateInput"] input,
    div[data-testid="stTimeInput"] input,
    textarea,
    [data-baseweb="select"] > div {
        min-height: 44px;
        border-radius: 10px !important;
        border: 1px solid var(--line-strong) !important;
        background: #18181b !important;
        color: var(--text) !important;
        box-shadow: none !important;
    }
    div[data-testid="stTextInput"] input:focus,
    div[data-testid="stNumberInput"] input:focus,
    div[data-testid="stDateInput"] input:focus,
    div[data-testid="stTimeInput"] input:focus,
    textarea:focus,
    [data-baseweb="select"] > div:focus-within {
        border-color: rgba(244, 63, 94, .72) !important;
        box-shadow: 0 0 0 3px rgba(244, 63, 94, .16) !important;
    }
    label, [data-testid="stCaptionContainer"] p {
        color: var(--muted) !important;
    }
    div[data-testid="stMetric"] {
        border-radius: var(--radius);
        border: 1px solid var(--line);
        background: #111113;
        padding: 16px;
    }
    div[data-testid="stMetricValue"] {
        color: var(--text);
        font-weight: 720;
    }
    .metric-strip, .posts-stats, .account-selection-summary {
        border-radius: var(--radius);
        border: 1px solid var(--line);
        background: var(--line);
    }
    .metric-cell, .posts-stats div, .account-selection-summary div {
        background: #111113;
    }
    div[data-testid="stDataFrame"] {
        border-radius: var(--radius);
        border: 1px solid var(--line);
        background: #09090b;
    }
    div[data-testid="stDataFrame"] * {
        font-variant-numeric: tabular-nums;
    }
    div[data-testid="stForm"] {
        border: 1px solid var(--line);
        border-radius: var(--radius);
        background: rgba(24,24,27,.52);
        padding: 18px;
    }
    [data-testid="stExpander"] {
        border: 1px solid var(--line);
        border-radius: var(--radius);
        background: rgba(24,24,27,.46);
        overflow: hidden;
    }
    .stTabs [data-baseweb="tab-list"] {
        gap: 6px;
        border-bottom: 1px solid var(--line);
    }
    .stTabs [data-baseweb="tab"] {
        border-radius: 10px 10px 0 0;
        color: var(--muted);
        padding: 10px 12px;
    }
    .stTabs [aria-selected="true"] {
        color: var(--text) !important;
        background: #18181b;
    }

    /* stronger Streamlit/BaseWeb overrides */
    html, body, [data-testid="stAppViewContainer"], [data-testid="stHeader"] {
        background: #09090b !important;
    }
    [data-testid="stHeader"] {
        border-bottom: 1px solid rgba(255,255,255,.08) !important;
    }
    [data-testid="stVerticalBlock"] > [style*="flex-direction: column"] {
        gap: 1.05rem !important;
    }
    [data-testid="stVerticalBlockBorderWrapper"],
    [data-testid="stForm"],
    [data-testid="stExpander"],
    [data-testid="stMetric"],
    [data-testid="stDataFrameResizable"] {
        border-radius: 14px !important;
        border: 1px solid rgba(255,255,255,.10) !important;
        background: #111113 !important;
        box-shadow: 0 1px 0 rgba(255,255,255,.04) inset !important;
    }
    div[data-testid="stButton"] > button {
        width: 100%;
        justify-content: center !important;
        text-align: center !important;
        white-space: pre-line;
    }
    div[data-testid="stButton"] > button p {
        color: inherit !important;
        font-weight: 700 !important;
        line-height: 1.35 !important;
        width: 100%;
        text-align: center !important;
        margin: 0 auto !important;
    }
    div[data-testid="stFormSubmitButton"] > button p {
        width: 100%;
        text-align: center !important;
        margin: 0 auto !important;
    }
    [data-testid="stMarkdownContainer"] {
        text-align: inherit;
    }
    .metric-cell,
    .posts-stats div,
    .account-selection-summary div,
    .step-note,
    .blocked-panel,
    .send-blockers {
        text-align: center;
    }
    .metric-cell span,
    .metric-cell small,
    .metric-cell strong,
    .posts-stats span,
    .posts-stats b,
    .account-selection-summary span,
    .account-selection-summary strong {
        text-align: center;
    }
    .posts-editor-wrap,
    div[data-testid="stDataFrame"],
    div[data-testid="stDataFrameResizable"] {
        width: 100% !important;
        margin: 14px auto 20px auto !important;
    }
    div[data-testid="stDataFrame"] [role="columnheader"],
    div[data-testid="stDataFrame"] [role="gridcell"] {
        text-align: center !important;
        justify-content: center !important;
    }
    table, th, td {
        text-align: center !important;
        vertical-align: middle !important;
    }
    [data-testid="stHorizontalBlock"] {
        align-items: center !important;
        gap: 1rem !important;
    }
    [data-baseweb="input"],
    [data-baseweb="base-input"],
    [data-baseweb="textarea"],
    [data-baseweb="select"] {
        border-radius: 10px !important;
        background: #18181b !important;
        border-color: rgba(255,255,255,.14) !important;
    }
    [data-baseweb="input"] input,
    [data-baseweb="base-input"] input,
    [data-baseweb="textarea"] textarea {
        color: #fafafa !important;
        background: transparent !important;
    }
    [data-baseweb="input"]:focus-within,
    [data-baseweb="base-input"]:focus-within,
    [data-baseweb="textarea"]:focus-within,
    [data-baseweb="select"]:focus-within {
        border-color: rgba(244,63,94,.72) !important;
        box-shadow: 0 0 0 3px rgba(244,63,94,.16) !important;
    }
    [data-testid="stRadio"] label {
        background: transparent !important;
    }
    [data-testid="stRadio"] [role="radiogroup"] {
        gap: 1rem;
    }
    [data-testid="stDataFrame"] {
        box-shadow: 0 0 0 1px rgba(255,255,255,.06), 0 18px 60px rgba(0,0,0,.18) !important;
    }
    [data-testid="stFileUploader"] section {
        border-radius: 14px !important;
        border: 1px dashed rgba(255,255,255,.18) !important;
        background: #111113 !important;
    }
    [data-testid="stAlert"] {
        border-radius: 12px !important;
        border: 1px solid rgba(255,255,255,.10) !important;
    }

    /* final airy layout pass */
    .block-container {
        max-width: 1440px !important;
        padding: 2rem 2.4rem 5rem !important;
    }
    .main .block-container > div {
        padding-left: .1rem;
        padding-right: .1rem;
    }
    h1 {margin: .2rem 0 1.1rem !important;}
    h2, h3 {margin: 1.25rem 0 .85rem !important;}
    h4 {margin: 1rem 0 .65rem !important;}
    .app-hero {
        text-align: left !important;
        padding: 28px 30px !important;
        margin: 6px 0 28px !important;
    }
    .section-intro {
        text-align: left !important;
        padding: 24px 28px !important;
        margin: 18px 0 26px !important;
    }
    .section-intro strong {
        line-height: 1.3 !important;
    }
    .metric-strip,
    .posts-stats,
    .account-selection-summary {
        margin: 18px 0 24px !important;
        gap: 1px !important;
    }
    .metric-cell,
    .posts-stats div,
    .account-selection-summary div {
        padding: 18px 20px !important;
        text-align: left !important;
    }
    .metric-cell span,
    .metric-cell small,
    .metric-cell strong,
    .posts-stats span,
    .posts-stats b,
    .account-selection-summary span,
    .account-selection-summary strong {
        text-align: left !important;
    }
    div[data-testid="stForm"] {
        padding: 26px !important;
        margin: 16px 0 26px !important;
    }
    [data-testid="stExpander"] {
        margin: 14px 0 20px !important;
    }
    [data-testid="stHorizontalBlock"] {
        gap: 1.4rem !important;
        align-items: flex-start !important;
    }
    div[data-testid="stVerticalBlock"] {
        gap: 1.15rem !important;
    }
    label {
        display: block !important;
        margin-bottom: .35rem !important;
        line-height: 1.35 !important;
    }
    div[data-testid="stTextInput"],
    div[data-testid="stNumberInput"],
    div[data-testid="stDateInput"],
    div[data-testid="stTimeInput"],
    div[data-testid="stSelectbox"],
    div[data-testid="stRadio"] {
        margin-bottom: .8rem !important;
    }
    div[data-testid="stButton"] > button,
    div[data-testid="stFormSubmitButton"] > button {
        min-height: 48px !important;
        padding: 12px 18px !important;
    }
    div[data-testid="stButton"] > button p,
    div[data-testid="stFormSubmitButton"] > button p {
        line-height: 1.25 !important;
    }
    div[data-testid="stDataFrame"],
    div[data-testid="stDataFrameResizable"] {
        margin: 18px auto 28px auto !important;
    }
    div[data-testid="stDataFrame"] [role="columnheader"] {
        min-height: 42px !important;
    }
    div[data-testid="stDataFrame"] [role="gridcell"] {
        min-height: 40px !important;
        padding-top: 8px !important;
        padding-bottom: 8px !important;
    }
    .accounts-shell,
    .accounts-shell-lite,
    .posts-editor-wrap,
    .selected-panel,
    .blocked-panel,
    .send-blockers {
        margin-top: 18px !important;
        margin-bottom: 24px !important;
    }
    .posts-control-panel {
        padding: 20px 22px !important;
        margin: 22px 0 20px !important;
    }
    .group-strip {
        padding: 10px 0 24px !important;
        gap: 16px !important;
    }
    [data-testid="stCaptionContainer"] p {
        margin-top: .35rem !important;
        margin-bottom: .75rem !important;
    }
    .preview-card-grid {
        display: grid;
        grid-template-columns: repeat(auto-fill, minmax(265px, 1fr));
        gap: 16px;
        margin: 18px 0 28px;
    }
    .preview-card {
        border: 1px solid rgba(255,255,255,.11);
        border-radius: 16px;
        background: linear-gradient(180deg, rgba(24,24,27,.88), rgba(15,17,23,.68));
        box-shadow: 0 1px 0 rgba(255,255,255,.05) inset, 0 18px 48px rgba(0,0,0,.18);
        padding: 18px;
        min-height: 228px;
        display: flex;
        flex-direction: column;
        gap: 9px;
        transition: transform .22s cubic-bezier(.16,1,.3,1), border-color .22s cubic-bezier(.16,1,.3,1);
    }
    .preview-thumb {
        width: 100%;
        aspect-ratio: 4 / 3;
        border-radius: 12px;
        overflow: hidden;
        border: 1px solid rgba(255,255,255,.10);
        background: rgba(255,255,255,.04);
        display: grid;
        place-items: center;
        margin-bottom: 4px;
    }
    .preview-thumb img {
        width: 100%;
        height: 100%;
        object-fit: cover;
        display: block;
    }
    .preview-thumb.is-empty span {
        color: var(--faint);
        font-size: .78rem;
        font-weight: 760;
        letter-spacing: .08em;
        text-transform: uppercase;
    }
    .preview-card:hover {
        transform: translateY(-1px);
        border-color: rgba(244, 63, 94, .38);
    }
    .preview-card-top {
        display: flex;
        justify-content: space-between;
        gap: 12px;
        align-items: flex-start;
    }
    .preview-time {
        color: #fafafa;
        font-size: 2.2rem;
        font-weight: 780;
        line-height: .9;
        font-variant-numeric: tabular-nums;
    }
    .preview-status {
        border: 1px solid rgba(255,255,255,.12);
        border-radius: 999px;
        color: var(--muted);
        background: rgba(255,255,255,.04);
        padding: 5px 9px;
        font-size: .72rem;
        font-weight: 760;
        white-space: nowrap;
    }
    .preview-card.is-ready .preview-status {
        color: rgba(220, 252, 231, .9);
        border-color: rgba(34, 197, 94, .32);
        background: rgba(34, 197, 94, .10);
    }
    .preview-card.is-failed .preview-status {
        color: rgba(255, 218, 225, .95);
        border-color: rgba(244, 63, 94, .38);
        background: rgba(244, 63, 94, .12);
    }
    .preview-day {
        color: var(--faint);
        font-size: .78rem;
        font-weight: 720;
        letter-spacing: .04em;
        text-transform: uppercase;
    }
    .preview-card strong {
        display: block;
        color: var(--text);
        font-size: 1.02rem;
        line-height: 1.25;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
    }
    .preview-card small {
        color: var(--muted);
        font-size: .82rem;
        line-height: 1.4;
    }
    .preview-card p {
        color: rgba(244,244,245,.88);
        line-height: 1.5;
        margin: 6px 0 0;
        font-size: .92rem;
    }
    .photo-asset-grid {
        display: grid;
        grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
        gap: 14px;
        margin: 16px 0 28px;
    }
    .photo-asset-card {
        border: 1px solid rgba(255,255,255,.10);
        border-radius: 14px;
        background: rgba(24,24,27,.58);
        padding: 10px;
        min-width: 0;
    }
    .photo-asset-card img {
        width: 100%;
        aspect-ratio: 1 / 1;
        object-fit: cover;
        border-radius: 10px;
        display: block;
        margin-bottom: 9px;
    }
    .photo-asset-card strong,
    .photo-asset-card small {
        display: block;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
    }
    .photo-asset-card strong {
        color: var(--text);
        font-size: .9rem;
    }
    .photo-asset-card small {
        color: var(--muted);
        font-size: .78rem;
        margin-top: 2px;
    }
    /* Dense post library: text stays primary; selection and metadata stay compact. */
    div[data-testid="stHorizontalBlock"]:has(.post-pick-shell) {
        align-items: stretch !important;
        gap: 0 !important;
        margin: 0 !important;
        border: 1px solid var(--line);
        border-bottom: 0;
        background: rgba(24, 24, 27, .54);
    }
    div[data-testid="stHorizontalBlock"]:has(.post-pick-shell):first-of-type {
        border-radius: 10px 10px 0 0;
    }
    div[data-testid="stHorizontalBlock"]:has(.post-pick-shell) > div:first-child {
        display: flex;
        align-items: center;
        justify-content: center;
        min-width: 52px;
        border-right: 1px solid var(--line);
        background: rgba(9, 9, 11, .22);
    }
    div[data-testid="stHorizontalBlock"]:has(.post-pick-shell) > div:first-child label {
        margin: 0 !important;
    }
    div[data-testid="stHorizontalBlock"]:has(.post-pick-shell) > div:last-child {
        min-width: 0;
    }
    .post-pick-shell {
        display: grid;
        grid-template-columns: 38px minmax(0, 1fr) auto;
        align-items: center;
        column-gap: 13px;
        min-height: 68px;
        padding: 10px 14px;
        border: 0 !important;
        border-radius: 0 !important;
        background: transparent !important;
        box-shadow: none !important;
    }
    .post-pick-kind {
        display: grid;
        width: 38px;
        height: 38px;
        place-items: center;
        border-radius: 8px;
        background: rgba(255, 255, 255, .045);
        color: var(--faint);
        font-size: .66rem;
        font-weight: 800;
        letter-spacing: .06em;
    }
    .post-pick-kind.has-media {
        color: rgba(220, 252, 231, .92);
        background: rgba(47, 191, 123, .10);
    }
    .post-pick-copy {min-width: 0;}
    .post-pick-shell p {
        display: -webkit-box;
        margin: 0 !important;
        overflow: hidden;
        color: var(--text);
        font-size: .96rem !important;
        font-weight: 620;
        line-height: 1.35 !important;
        -webkit-box-orient: vertical;
        -webkit-line-clamp: 2;
    }
    .post-pick-details {
        display: flex;
        gap: 10px;
        min-width: 0;
        margin-top: 4px;
        color: var(--faint);
        font-size: .72rem;
        line-height: 1.2;
        white-space: nowrap;
    }
    .post-pick-details span {
        overflow: hidden;
        text-overflow: ellipsis;
    }
    .post-pick-details span:not(:last-child)::after {
        content: "·";
        margin-left: 10px;
        color: rgba(255, 255, 255, .22);
    }
    .post-pick-state {
        justify-self: end;
        color: var(--faint);
        font-size: .72rem;
        font-weight: 720;
        white-space: nowrap;
    }
    .post-pick-state.is-selected {color: var(--accent);}
    div[data-testid="stVerticalBlock"]:has(> div .post-pick-shell) {
        border: 0 !important;
        border-radius: 0 !important;
        padding: 0 !important;
        background: transparent !important;
    }
    /* Application shell inspired by operational social scheduling tools. */
    [data-testid="stSidebar"] {
        min-width: 252px;
        max-width: 252px;
        background: #111113 !important;
        border-right: 1px solid var(--line) !important;
    }
    [data-testid="stSidebar"] > div:first-child {
        padding: 18px 12px 22px;
    }
    .sidebar-brand {
        display: flex;
        align-items: center;
        gap: 10px;
        padding: 8px 10px 26px;
    }
    .sidebar-brand > i {
        display: grid;
        width: 34px;
        height: 34px;
        place-items: center;
        border: 1px solid rgba(244, 63, 94, .5);
        border-radius: 50%;
        background: rgba(244, 63, 94, .12);
        color: var(--accent);
        font-size: .9rem;
        font-style: normal;
        font-weight: 820;
    }
    .sidebar-brand b {
        color: var(--text);
        font-size: 1.12rem;
        letter-spacing: 0;
    }
    .sidebar-brand span,
    .sidebar-section,
    .page-kicker {
        color: var(--faint);
        font-size: .68rem;
        font-weight: 800;
        letter-spacing: .12em;
    }
    .sidebar-section {
        margin: 18px 10px 7px;
    }
    [data-testid="stSidebar"] [data-testid="stButton"] {
        margin: 1px 0 !important;
    }
    [data-testid="stSidebar"] [data-testid="stButton"] button {
        min-height: 38px !important;
        padding: 8px 11px !important;
        justify-content: flex-start !important;
        border-color: transparent !important;
        background: transparent !important;
        box-shadow: none !important;
        color: var(--muted) !important;
        font-size: .9rem;
        font-weight: 620;
    }
    [data-testid="stSidebar"] [data-testid="stButton"] button:hover {
        transform: none !important;
        border-color: transparent !important;
        background: rgba(255, 255, 255, .055) !important;
        color: var(--text) !important;
    }
    [data-testid="stSidebar"] [data-testid="stButton"] button[kind="primary"] {
        position: relative;
        background: rgba(255, 255, 255, .065) !important;
        color: var(--text) !important;
    }
    [data-testid="stSidebar"] [data-testid="stButton"] button[kind="primary"]::before {
        position: absolute;
        left: 0;
        width: 3px;
        height: 18px;
        border-radius: 0 3px 3px 0;
        background: var(--accent);
        content: "";
    }
    [data-testid="stSidebar"] [data-testid="stExpander"] {
        margin: 14px 0 8px !important;
        background: rgba(255, 255, 255, .025) !important;
    }
    .app-hero.app-topbar {
        display: flex;
        align-items: center;
        justify-content: space-between;
        min-height: 58px;
        margin: -2rem -2.4rem 26px !important;
        padding: 0 2.4rem !important;
        border: 0;
        border-bottom: 1px solid var(--line);
        border-radius: 0;
        background: rgba(17, 17, 19, .76);
        box-shadow: none;
    }
    .topbar-title {
        color: var(--muted);
        font-size: .82rem;
        font-weight: 740;
        letter-spacing: .04em;
        text-transform: uppercase;
    }
    .app-topbar .hero-status {align-items: center;}
    .app-topbar .status-pill {
        padding: 5px 8px;
        font-size: .72rem;
    }
    .page-kicker {
        display: block;
        margin: 2px 0 8px;
        color: var(--accent);
    }
    .stApp h1 {
        font-size: clamp(2rem, 3vw, 2.7rem) !important;
        line-height: 1.06 !important;
        margin: 0 0 .35rem !important;
    }
    .stApp h2 {
        font-size: 1.5rem !important;
        margin: 0 0 .8rem !important;
    }
    .section-intro {
        border: 0 !important;
        border-left: 2px solid rgba(244, 63, 94, .55) !important;
        border-radius: 0 !important;
        background: transparent !important;
        box-shadow: none !important;
        padding: 4px 0 4px 16px !important;
        margin: 8px 0 24px !important;
    }
    .section-intro strong {font-size: 1.12rem;}
    .section-intro p {max-width: 72ch;}
    .metric-strip {
        grid-template-columns: repeat(4, minmax(0, 1fr));
        margin: 18px 0 26px !important;
        border-radius: 10px;
    }
    .metric-cell,
    .posts-stats div,
    .account-selection-summary div {
        padding: 14px 16px !important;
    }
    .metric-cell strong {font-size: 1.45rem;}
    .dashboard-heading {
        display: flex;
        align-items: end;
        justify-content: space-between;
        gap: 24px;
        margin: 28px 0 22px;
    }
    .dashboard-heading span {
        display: block;
        margin-bottom: 10px;
        color: var(--faint);
        font-size: .72rem;
        font-weight: 800;
        letter-spacing: .12em;
    }
    .dashboard-heading h1 {
        margin: 0 !important;
        color: var(--text);
        font-size: clamp(2.15rem, 3.4vw, 3.05rem) !important;
        font-weight: 780;
        line-height: 1;
    }
    .dashboard-heading p {
        margin: 10px 0 0;
        color: var(--muted);
        font-size: .95rem;
    }
    .dashboard-heading > small {
        padding-bottom: 4px;
        color: var(--faint);
        font-size: .78rem;
        white-space: nowrap;
    }
    .dashboard-metric-grid {
        display: grid;
        grid-template-columns: repeat(4, minmax(0, 1fr));
        gap: 12px;
        margin-bottom: 22px;
    }
    .dashboard-metric-grid article,
    .dashboard-panel {
        border: 1px solid rgba(255,255,255,.08);
        border-radius: 12px;
        background: #18181b;
        box-shadow: inset 0 1px 0 rgba(255,255,255,.025);
    }
    .dashboard-metric-grid article {
        position: relative;
        min-height: 136px;
        padding: 18px 18px 16px;
    }
    .dashboard-metric-grid span {
        display: block;
        padding-left: 38px;
        color: var(--faint);
        font-size: .72rem;
        font-weight: 800;
        letter-spacing: .1em;
    }
    .dashboard-metric-grid strong {
        display: block;
        margin-top: 26px;
        color: var(--text);
        font-size: 1.85rem;
        font-variant-numeric: tabular-nums;
        line-height: 1;
    }
    .dashboard-metric-grid small {
        display: block;
        margin-top: 9px;
        color: var(--muted);
        font-size: .78rem;
    }
    .dashboard-icon {
        position: absolute;
        display: grid;
        width: 28px;
        height: 28px;
        place-items: center;
        border-radius: 8px;
        background: rgba(244, 63, 94, .12);
        color: var(--accent);
        font-size: .7rem;
        font-style: normal;
        font-weight: 840;
    }
    .dashboard-icon.is-alert {background: rgba(231, 185, 88, .12); color: var(--warn);}
    .dashboard-grid {
        display: grid;
        grid-template-columns: minmax(280px, .75fr) minmax(0, 1.65fr);
        gap: 14px;
        align-items: stretch;
    }
    .dashboard-panel {min-width: 0; overflow: hidden;}
    .dashboard-panel header {
        display: flex;
        justify-content: space-between;
        gap: 14px;
        padding: 15px 18px;
        border-bottom: 1px solid rgba(255,255,255,.08);
    }
    .dashboard-panel header span {
        color: var(--faint);
        font-size: .7rem;
        font-weight: 800;
        letter-spacing: .09em;
    }
    .dashboard-panel header b {
        color: var(--accent);
        font-size: .75rem;
        font-weight: 700;
        white-space: nowrap;
    }
    .dashboard-list {min-height: 290px;}
    .dashboard-list-row {
        display: grid;
        grid-template-columns: 48px minmax(0, 1fr) auto;
        gap: 10px;
        align-items: center;
        min-height: 57px;
        padding: 8px 18px;
        border-bottom: 1px solid rgba(255,255,255,.06);
    }
    .dashboard-list-row:last-child {border-bottom: 0;}
    .dashboard-list-time {
        color: var(--text);
        font-size: .8rem;
        font-variant-numeric: tabular-nums;
        font-weight: 720;
    }
    .dashboard-list-row strong,
    .dashboard-list-row small {
        display: block;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
    }
    .dashboard-list-row strong {color: var(--text); font-size: .83rem;}
    .dashboard-list-row small {margin-top: 3px; color: var(--muted); font-size: .73rem;}
    .dashboard-list-row em {
        color: var(--faint);
        font-size: .7rem;
        font-style: normal;
        white-space: nowrap;
    }
    .dashboard-empty {
        display: grid;
        min-height: 180px;
        place-items: center;
        padding: 20px;
        color: var(--muted);
        font-size: .85rem;
        text-align: center;
    }
    .dashboard-chart {min-height: 332px;}
    .dashboard-chart-bars {
        display: grid;
        grid-template-columns: repeat(7, 1fr);
        align-items: end;
        gap: 16px;
        height: 255px;
        padding: 24px 30px 16px;
        background-image: linear-gradient(to bottom, rgba(255,255,255,.055) 1px, transparent 1px);
        background-size: 100% 33.33%;
    }
    .dashboard-chart-day {
        display: grid;
        grid-template-rows: 1fr 18px;
        align-items: end;
        height: 100%;
    }
    .dashboard-chart-bar {
        width: 100%;
        align-self: end;
        min-height: 4px;
        border-radius: 4px 4px 0 0;
        background: linear-gradient(180deg, rgba(244,63,94,.9), rgba(244,63,94,.42));
    }
    .dashboard-chart-day span {
        margin-top: 8px;
        color: var(--faint);
        font-size: .72rem;
        text-align: center;
    }
    .dashboard-accounts {
        grid-column: 1 / -1;
    }
    .dashboard-account-list {padding: 5px 18px 12px;}
    .dashboard-account-row {
        display: grid;
        grid-template-columns: minmax(120px, .6fr) minmax(90px, 1.8fr) 30px;
        gap: 14px;
        align-items: center;
        min-height: 38px;
    }
    .dashboard-account-row span {
        overflow: hidden;
        color: var(--muted);
        font-size: .8rem;
        text-overflow: ellipsis;
        white-space: nowrap;
    }
    .dashboard-account-row i {
        display: block;
        height: 4px;
        overflow: hidden;
        border-radius: 99px;
        background: rgba(255,255,255,.08);
    }
    .dashboard-account-row i b {
        display: block;
        height: 100%;
        border-radius: inherit;
        background: var(--accent);
    }
    .dashboard-account-row strong {
        color: var(--text);
        font-size: .8rem;
        font-variant-numeric: tabular-nums;
        text-align: right;
    }
    .accounts-heading {
        margin: 28px 0 20px;
    }
    .accounts-heading span {
        display: block;
        margin-bottom: 9px;
        color: var(--faint);
        font-size: .72rem;
        font-weight: 800;
        letter-spacing: .12em;
    }
    .accounts-heading h1 {
        margin: 0 !important;
        color: var(--text);
        font-size: clamp(2.1rem, 3.3vw, 3rem) !important;
        font-weight: 780;
        line-height: 1;
    }
    .accounts-heading p {
        margin: 10px 0 0;
        color: var(--muted);
        font-size: .94rem;
    }
    .accounts-group-board {
        margin: 16px 0 12px;
        padding: 16px 20px 18px;
        border: 1px solid rgba(255,255,255,.10);
        border-radius: 12px;
        background: #18181b;
    }
    .accounts-group-board header {
        display: flex;
        justify-content: space-between;
        align-items: center;
        margin-bottom: 16px;
    }
    .accounts-group-board header span {
        color: var(--faint);
        font-size: .72rem;
        font-weight: 800;
        letter-spacing: .1em;
    }
    .accounts-group-board header small {
        color: var(--faint);
        font-size: .74rem;
    }
    .accounts-group-board > div {
        display: flex;
        flex-wrap: wrap;
        gap: 10px 20px;
        align-items: center;
    }
    .group-plan-picker-label {
        margin: 4px 0 8px;
        color: var(--muted);
        font-size: .8rem;
        font-weight: 700;
    }
    .accounts-group-chip {
        display: inline-grid;
        grid-template-columns: 10px minmax(0, 1fr) auto;
        gap: 8px;
        align-items: center;
        min-width: 120px;
        color: var(--muted);
        font-size: .86rem;
    }
    .accounts-group-chip i {
        width: 10px;
        height: 10px;
        border-radius: 50%;
        box-shadow: 0 0 0 8px rgba(255,255,255,.025);
    }
    .accounts-group-chip span {
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
    }
    .accounts-group-chip b {
        min-width: 32px;
        padding: 3px 6px;
        border-radius: 5px;
        background: rgba(0,0,0,.22);
        color: var(--faint);
        font-size: .7rem;
        font-weight: 740;
        text-align: center;
    }
    .accounts-group-chip.is-selected {color: var(--text);}
    .accounts-group-chip.is-selected b {color: var(--accent);}
    .accounts-group-empty {color: var(--muted); font-size: .84rem;}
    .group-form-intro {
        display: grid;
        gap: 5px;
        margin: 0 0 18px;
        padding: 14px 16px;
        border: 1px solid var(--line);
        border-radius: 10px;
        background: rgba(244, 63, 94, .055);
    }
    .group-form-intro strong {color: var(--text); font-size: .96rem;}
    .group-form-intro span {color: var(--muted); font-size: .8rem; line-height: 1.45;}
    .group-colour-label {
        margin: 14px 0 7px;
        color: var(--muted);
        font-size: .78rem;
        font-weight: 720;
    }
    .group-colour-preview {
        display: inline-flex;
        align-items: center;
        gap: 8px;
        margin: 10px 0 16px;
        color: var(--muted);
        font-size: .78rem;
    }
    .group-colour-preview i {
        width: 10px;
        height: 10px;
        border-radius: 50%;
        box-shadow: 0 0 0 4px rgba(255,255,255,.04);
    }
    .cadence-heading {
        display: flex;
        align-items: end;
        justify-content: space-between;
        gap: 20px;
        margin: 18px 0 14px;
    }
    .cadence-heading span {
        display: block;
        margin-bottom: 7px;
        color: var(--faint);
        font-size: .72rem;
        font-weight: 800;
        letter-spacing: .12em;
    }
    .cadence-heading h1 {
        margin: 0 !important;
        font-size: 2rem !important;
        line-height: 1;
    }
    .cadence-summary-top {
        display: inline-flex;
        align-items: center;
        gap: 9px;
        padding: 8px 11px;
        border: 1px solid var(--line);
        border-radius: 8px;
        background: #18181b;
        color: var(--muted);
        font-size: .8rem;
        white-space: nowrap;
    }
    .cadence-summary-top b {color: var(--text);}
    .cadence-summary-top i {
        width: 7px;
        height: 7px;
        border-radius: 50%;
        background: var(--success);
    }
    .cadence-form-anchor {display: none;}
    div[data-testid="stForm"]:has(.cadence-form-anchor) {
        padding: 22px 24px !important;
        margin: 14px 0 !important;
        border-radius: 12px !important;
    }
    div[data-testid="stForm"]:has(.cadence-form-anchor) [data-testid="stVerticalBlock"] {
        gap: .7rem !important;
    }
    div[data-testid="stForm"]:has(.cadence-form-anchor) [data-testid="stMarkdownContainer"] {
        padding: 0 !important;
    }
    div[data-testid="stForm"]:has(.cadence-form-anchor) [data-testid="stHorizontalBlock"] {
        gap: .85rem !important;
    }
    div[data-testid="stForm"]:has(.cadence-form-anchor) [data-testid="stNumberInput"] input,
    div[data-testid="stForm"]:has(.cadence-form-anchor) [data-testid="stDateInput"] input,
    div[data-testid="stForm"]:has(.cadence-form-anchor) [data-testid="stTimeInput"] input {
        min-height: 42px !important;
        padding-block: .45rem !important;
    }
    div[data-testid="stForm"]:has(.cadence-form-anchor) [data-testid="stNumberInput"],
    div[data-testid="stForm"]:has(.cadence-form-anchor) [data-testid="stDateInput"],
    div[data-testid="stForm"]:has(.cadence-form-anchor) [data-testid="stTimeInput"] {
        margin-bottom: 0 !important;
    }
    .cadence-section-label {
        margin: 0 0 6px;
        color: var(--muted);
        font-size: .78rem;
        font-weight: 760;
    }
    .cadence-date-add {display:block; height:1.45rem;}
    .cadence-total-card {
        min-height: 109px;
        box-sizing: border-box;
        padding: 15px 16px;
        border: 1px solid rgba(244,63,94,.4);
        border-radius: 10px;
        background: linear-gradient(135deg, rgba(244,63,94,.16), rgba(244,63,94,.045));
    }
    .cadence-total-card span {display:block; color:#fb7185; font-size:.68rem; font-weight:800; letter-spacing:.1em;}
    .cadence-total-card strong {display:block; margin:2px 0; color:#fff1f3; font-size:2.25rem; font-weight:850; line-height:1;}
    .cadence-total-card small {display:block; color:var(--muted); font-size:.76rem;}
    .cadence-capacity {margin: 6px 0 0; color: var(--faint); font-size: .76rem;}
    .cadence-submit-summary {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 16px;
        padding: 11px 13px;
        border: 1px solid var(--line);
        border-radius: 9px;
        background: rgba(244,63,94,.055);
    }
    .cadence-submit-summary span {color: var(--muted); font-size: .8rem;}
    .cadence-submit-summary strong {color: var(--text); font-size: .88rem;}
    .accounts-selection-line {
        display: flex;
        justify-content: space-between;
        gap: 14px;
        margin: 16px 0 10px;
        padding: 0 2px;
    }
    .accounts-selection-line strong {
        color: var(--text);
        font-size: .86rem;
        font-weight: 720;
    }
    .accounts-selection-line span {
        color: var(--faint);
        font-size: .78rem;
        text-align: right;
    }
    .accounts-shell-lite {
        margin-top: 10px !important;
        border-radius: 12px 12px 0 0 !important;
        background: #18181b !important;
    }
    .account-table-head {
        grid-template-columns: 44px minmax(260px, 2.25fr) minmax(120px, .9fr) minmax(105px, .78fr) 44px;
        min-height: 50px;
        padding: 10px 18px;
        letter-spacing: .1em;
    }
    div[data-testid="stHorizontalBlock"]:has(.account-person) {
        align-items: center !important;
        gap: 14px !important;
        min-height: 72px;
        margin: 0 !important;
        padding: 6px 18px;
        border-right: 1px solid var(--line);
        border-bottom: 1px solid var(--line);
        border-left: 1px solid var(--line);
        background: rgba(24,24,27,.72);
    }
    div[data-testid="stHorizontalBlock"]:has(.account-person):hover {
        background: rgba(255,255,255,.025);
    }
    div[data-testid="stHorizontalBlock"]:has(.account-person):has([data-testid="stCheckbox"] input:checked) {
        background: rgba(223, 77, 110, .065);
        box-shadow: inset 3px 0 0 var(--accent);
    }
    div[data-testid="stHorizontalBlock"]:has(.account-person) > div {
        min-width: 0;
    }
    /* Columns use Streamlit border wrappers internally. They must stay invisible in this dense table. */
    div[data-testid="stHorizontalBlock"]:has(.account-person) [data-testid="stVerticalBlockBorderWrapper"] {
        border: 0 !important;
        border-radius: 0 !important;
        background: transparent !important;
        box-shadow: none !important;
    }
    div[data-testid="stHorizontalBlock"]:has(.account-person) [data-testid="stVerticalBlock"] {
        gap: 0 !important;
    }
    div[data-testid="stHorizontalBlock"]:has(.account-person) [data-testid="stMarkdownContainer"] {
        padding: 0 !important;
    }
    div[data-testid="stHorizontalBlock"]:has(.account-person) [data-testid="stCheckbox"] {
        display: flex;
        align-items: center;
        justify-content: center;
    }
    div[data-testid="stHorizontalBlock"]:has(.account-person) [data-testid="stSelectbox"] {
        margin: 0 !important;
    }
    div[data-testid="stHorizontalBlock"]:has(.account-person) [data-testid="stSelectbox"] [data-baseweb="select"] > div {
        min-height: 34px !important;
        border-radius: 7px !important;
        font-size: .76rem !important;
    }
    div[data-testid="stHorizontalBlock"]:has(.account-person) [data-testid="stToggle"] {
        display: flex;
        justify-content: center;
        margin: 0 !important;
    }
    div[data-testid="stHorizontalBlock"]:has(.account-person) [data-testid="stPopover"] {
        margin: 0 !important;
    }
    div[data-testid="stHorizontalBlock"]:has(.account-person) [data-testid="stPopover"] button {
        width: auto !important;
        max-width: 92px !important;
        min-height: 26px !important;
        padding: 2px 7px !important;
        border-radius: 7px !important;
        background: rgba(223, 77, 110, .10) !important;
        border-color: rgba(223, 77, 110, .24) !important;
        color: #f7c6d2 !important;
        font-size: .72rem !important;
        white-space: nowrap !important;
    }
    div[data-testid="stHorizontalBlock"]:has(.account-person) [data-testid="stPopover"] [data-testid="baseButton-secondary"],
    div[data-testid="stHorizontalBlock"]:has(.account-person) [data-testid="stPopover"] [data-testid="baseButton-primary"] {
        width: auto !important;
        min-height: 26px !important;
        padding: 2px 7px !important;
        border-radius: 7px !important;
    }
    div[data-testid="stHorizontalBlock"]:has(.account-person) [data-testid="stPopover"] button p {
        width: auto !important;
        font-weight: 720 !important;
        overflow: hidden !important;
        text-overflow: ellipsis !important;
    }
    .account-row-divider {display: none;}
    .account-person {
        grid-template-columns: 42px minmax(0, 1fr);
        gap: 10px;
        min-height: 52px;
    }
    .account-avatar, .account-avatar-fallback {
        width: 40px;
        height: 40px;
    }
    .account-name strong {font-size: .9rem;}
    .account-name small {
        max-width: 25ch;
        font-size: .78rem;
    }
    .account-group-badge {
        padding: 5px 8px;
        font-size: .76rem;
    }
    .next-post-pill, .status-text {font-size: .78rem;}
    .account-actions a {
        display: inline-grid;
        place-items: center;
        width: 28px;
        height: 28px;
        padding: 0;
        border: 1px solid var(--line);
        border-radius: 7px;
        color: var(--muted);
        font-size: 1rem;
        line-height: 1;
        text-decoration: none;
    }
    div[data-testid="stHorizontalBlock"]:has(.account-person) .account-actions {
        display: flex;
        align-items: center;
        justify-content: center;
        min-height: 28px;
    }
    .account-actions a:hover {
        color: var(--text);
        border-color: rgba(223, 77, 110, .55);
        background: var(--accent-soft);
    }
    @media (max-width: 900px) {
        .block-container {
            padding: 1.3rem 1rem 4rem !important;
        }
        .app-hero,
        .section-intro {
            padding: 20px !important;
        }
        .app-hero.app-topbar {
            min-height: 52px;
            margin: -1.3rem -1rem 18px !important;
            padding: 0 1rem !important;
        }
        .topbar-title {display: none;}
        .app-topbar .hero-status {width: 100%; justify-content: space-between;}
        .dashboard-heading {align-items: flex-start; margin-top: 20px;}
        .dashboard-heading > small {display: none;}
        .dashboard-metric-grid {grid-template-columns: 1fr 1fr; gap: 8px;}
        .dashboard-metric-grid article {min-height: 120px; padding: 14px;}
        .dashboard-grid {grid-template-columns: 1fr;}
        .dashboard-accounts {grid-column: auto;}
        .dashboard-chart-bars {gap: 8px; padding: 20px 16px 14px;}
        .accounts-heading {margin-top: 20px;}
        .accounts-group-board {padding: 14px;}
        .accounts-group-board header small {display: none;}
        .accounts-selection-line {align-items: flex-start; flex-direction: column;}
        .accounts-selection-line span {text-align: left;}
        .app-hero, .metric-strip, .flow-rail {
            grid-template-columns: 1fr;
        }
        .hero-status {justify-content: flex-start;}
        .accounts-toolbar {
            grid-template-columns: 1fr;
        }
        .accounts-count {
            text-align: left;
            padding-bottom: 0;
        }
        .account-table-head, .desktop-account-row {
            display: none;
        }
        .account-selection-summary {
            grid-template-columns: 1fr;
        }
        .posts-stats {
            grid-template-columns: 1fr 1fr;
        }
        .preview-card-grid {
            grid-template-columns: 1fr;
        }
        .post-pick-shell {
            grid-template-columns: 34px minmax(0, 1fr);
            min-height: 62px;
            padding: 10px 12px;
            column-gap: 10px;
        }
        .post-pick-kind {width: 34px; height: 34px;}
        .post-pick-state {display: none;}
        .post-pick-details span:nth-child(n + 3) {display: none;}
        .mobile-account-card {
            display: block;
            border-top: 1px solid var(--line);
            padding: 14px 0;
        }
    }
    </style>
    """,
    unsafe_allow_html=True,
)

api_exists = bool(os.getenv("POSTORIA_API_KEY"))
st.session_state.setdefault("app_page", "dashboard")
st.session_state.setdefault("sidebar_dry_run", True)
dry_run = bool(st.session_state["sidebar_dry_run"])

client = None
if api_exists:
    try:
        client = PostoriaClient()
    except Exception as e:
        st.error(str(e))

# Apply the one-time default before restoring session selection/status widgets.
activated_account_defaults = db.activate_all_accounts_once("v7")
stored_accounts = db.list_accounts()
if activated_account_defaults:
    for account in stored_accounts:
        st.session_state[f"account_status_enabled_v2_{int(account['id'])}"] = True
restore_account_selection_from_db(stored_accounts)
posts = db.list_posts(active_only=False)
preview = db.list_scheduled("preview")
scheduled_all = db.list_scheduled()
selected_accounts_count = len(st.session_state.get("selected_accounts", []))
selected_posts_count = len(st.session_state.get("selected_posts", []))
current = settings()
capacity_now = max_posts_for_period(
    current["publish_date"], current["publish_end_date"], current["start_time"], current["end_time"], int(current["min_interval"])
)
accounts_ready = selected_accounts_count > 0
cadence_ready = accounts_ready and int(current["posts_max"]) <= capacity_now
posts_ready = selected_posts_count > 0 or int(current["posts_max"]) == 0
preview_ready = len(preview) > 0
analytics_ready = len(scheduled_all) > 0
send_ready = preview_ready and api_exists and not dry_run
tracking_ready = any(str(row.get("status")) != "preview" or row.get("postoria_post_id") for row in scheduled_all)

active_page, dry_run = render_sidebar_navigation(
    str(st.session_state.get("app_page", "dashboard")),
    api_exists,
    dry_run,
)
st.session_state["app_page"] = active_page
page_to_step = {
    "accounts": 0,
    "cadence": 1,
    "posts": 2,
    "preview": 3,
    "analytics": 4,
    "send": 5,
    "tracking": 6,
}
active_step = page_to_step.get(active_page, int(st.session_state.get("active_step", 0)))
st.session_state["active_step"] = active_step

render_app_header(api_exists, dry_run, APP_TZ)
if active_page != "dashboard":
    render_step_links(active_step)
if st.session_state.get("reset_dialog_mode"):
    render_reset_dialog(str(st.session_state["reset_dialog_mode"]))
if st.session_state.get("preview_cleared_notice"):
    st.info(st.session_state.pop("preview_cleared_notice"))

if active_page == "dashboard":
    render_dashboard_overview(stored_accounts, posts, preview, scheduled_all)

if active_page != "dashboard" and active_step == 0:
    st.markdown(
        "<section class='accounts-heading'>"
        "<span>COMPTES</span><h1>Threads Accounts</h1>"
        "<p>Gère les groupes, sélectionne les comptes, puis passe à la cadence.</p>"
        "</section>",
        unsafe_allow_html=True,
    )
    with st.expander("Synchronisation Postoria", expanded=False):
        if not client:
            st.warning("Ajoute POSTORIA_API_KEY dans .env pour récupérer les comptes.")
        else:
            load_col, workspace_col = st.columns([1, 2])
            with load_col:
                if st.button("Récupérer workspaces"):
                    try:
                        st.session_state["workspaces"] = client.list_workspaces()
                    except Exception as e:
                        st.error(str(e))
            workspaces = st.session_state.get("workspaces", [])
            with workspace_col:
                if workspaces:
                    workspace_ids = [w["id"] for w in workspaces]
                    workspace_id = choose_option(
                        "Workspace",
                        workspace_ids,
                        format_func=lambda wid: next(w["name"] for w in workspaces if w["id"] == wid),
                    )
                    st.session_state["workspace_id"] = workspace_id
                    if st.button("Charger comptes Threads"):
                        try:
                            accounts = client.list_social_accounts(int(workspace_id))
                            threads_accounts = [a for a in accounts if str(a.get("network", "")).lower() == "threads"]
                            sync_result = db.sync_accounts(threads_accounts)
                            restored_message = ""
                            if GROUP_STORE.configured:
                                try:
                                    saved_config = GROUP_STORE.load(str(workspace_id))
                                    if saved_config:
                                        restored = db.apply_group_configuration(saved_config)
                                        restored_message = (
                                            f" {restored['groups']} groupe(s) et "
                                            f"{restored['accounts']} préférence(s) restauré(s)."
                                        )
                                        st.session_state["remote_group_config_dirty"] = False
                                    st.session_state["remote_group_config_workspace"] = str(workspace_id)
                                    st.session_state.pop("remote_group_config_error", None)
                                except GroupStoreError as exc:
                                    st.session_state["remote_group_config_error"] = str(exc)
                            fresh_accounts = db.list_accounts()
                            reset_account_session_after_sync(fresh_accounts)
                            removed_message = f" {sync_result['removed']} ancien(s) compte(s) retiré(s)." if sync_result["removed"] else ""
                            st.success(f"{sync_result['synced']} comptes Threads synchronisés.{removed_message}{restored_message}")
                            st.rerun()
                        except Exception as e:
                            st.error(str(e))

    # New and existing accounts start active once. Selection remains a separate state.
    # The database is the source of truth for active/paused state; session data can be stale after a sync.
    accounts = db.list_accounts()
    st.session_state["threads_accounts"] = accounts
    if not accounts:
        st.info("Aucun compte local. Charge les comptes Postoria d'abord.")
    else:
        if activated_account_defaults:
            for account in accounts:
                st.session_state[f"account_status_enabled_v2_{int(account['id'])}"] = True
        st.markdown("#### Choisir les comptes")
        groups = db.list_groups()
        group_color_by_name = {group["name"]: group.get("color") for group in groups}
        group_options = [g["name"] for g in groups] or ["tous"]
        group_accounts_by_name = {
            group_name: [
                account for account in accounts
                if (account.get("group_name") or "tous") == group_name
            ]
            for group_name in group_options
        }

        group_action_a, group_action_b, group_action_c = st.columns([1.2, 1, 2.4])
        with group_action_a:
            all_active = st.button("Sélectionner tous les comptes", use_container_width=True)
        with group_action_b:
            create_group = st.button("+ Créer un groupe", use_container_width=True)
        with group_action_c:
            st.caption("Choisis d'abord des groupes, puis ajuste seulement les exceptions dans le tableau.")
        if all_active:
            st.session_state["selected_group_filters"] = group_options
            st.session_state["manual_included_accounts"] = []
            st.session_state["manual_excluded_accounts"] = []
            st.session_state.pop("_account_group_signature", None)
            for account in accounts:
                account_id = int(account["id"])
                st.session_state[f"account_use_{account_id}"] = True
            mark_group_config_dirty()
            st.rerun()
        if create_group:
            st.session_state["show_group_dialog"] = True
            st.rerun()
        if st.session_state.get("show_group_dialog"):
            render_create_group_dialog()

        if GROUP_STORE.configured:
            st.caption("Sauvegarde durable des groupes : active pour ce workspace.")
        elif st.session_state.get("workspace_id"):
            st.caption("Groupes locaux uniquement. Configure Supabase pour les retrouver après un redémarrage Cloud.")
        if st.session_state.get("remote_group_config_error"):
            st.warning(st.session_state["remote_group_config_error"])

        selected_group_filters = st.session_state.get("selected_group_filters", [])
        manual_included = set(st.session_state.get("manual_included_accounts", []))
        manual_excluded = set(st.session_state.get("manual_excluded_accounts", []))
        group_signature = tuple(selected_group_filters)
        if st.session_state.get("_account_group_signature") != group_signature:
            for account in accounts:
                account_id = int(account["id"])
                group_name = st.session_state.get(f"account_group_{account_id}", account.get("group_name") or "tous")
                base_use = group_name in selected_group_filters
                if account_id in manual_excluded:
                    st.session_state[f"account_use_{account_id}"] = False
                elif account_id in manual_included:
                    st.session_state[f"account_use_{account_id}"] = True
                else:
                    st.session_state[f"account_use_{account_id}"] = base_use
            st.session_state["_account_group_signature"] = group_signature

        render_accounts_group_board(groups, group_accounts_by_name, selected_group_filters)
        render_group_planning_selector(groups, group_accounts_by_name, selected_group_filters)
        with st.expander("Ajouter ou retirer d'autres groupes", expanded=False):
            st.caption("Optionnel : combine plusieurs groupes pour la même planification.")
            group_cols = st.columns(min(3, max(1, len(groups))))
            for idx, group in enumerate(groups):
                group_name = group["name"]
                group_accounts = group_accounts_by_name.get(group_name, [])
                group_is_selected = group_name in st.session_state.get("selected_group_filters", [])
                with group_cols[idx % len(group_cols)]:
                    group_key = f"{idx}_{widget_slug(group_name)}"
                    action_label = f"Retirer {group_name}" if group_is_selected else f"Utiliser {group_name}"
                    if st.button(action_label, key=f"toggle_group_{group_key}", disabled=not group_accounts):
                        selected = set(st.session_state.get("selected_group_filters", []))
                        if group_is_selected:
                            selected.discard(group_name)
                        else:
                            selected.add(group_name)
                        st.session_state["selected_group_filters"] = [name for name in group_options if name in selected]
                        st.session_state.pop("_account_group_signature", None)
                        mark_group_config_dirty()
                        st.rerun()
            st.divider()
            current_groups = set(st.session_state.get("selected_group_filters", []))
            advanced_groups = []
            for group_name in group_options:
                checked = st.checkbox(
                    group_name,
                    value=group_name in current_groups,
                    key=f"advanced_group_{widget_slug(group_name)}",
                )
                if checked:
                    advanced_groups.append(group_name)
            if set(advanced_groups) != current_groups:
                st.session_state["selected_group_filters"] = advanced_groups
                st.session_state.pop("_account_group_signature", None)
                mark_group_config_dirty()
                st.rerun()

        for account in accounts:
            account_id = int(account["id"])
            st.session_state.setdefault(f"account_group_{account_id}", account.get("group_name") or "tous")
            st.session_state.setdefault(f"account_use_{account_id}", False)

        top_a, top_d = st.columns([3.2, .55])
        with top_a:
            account_query = st.text_input("Search accounts", placeholder="Search accounts...", label_visibility="collapsed")
        visible_accounts = []
        query = account_query.strip().lower()
        for account in accounts:
            account_id = int(account["id"])
            group_name = st.session_state.get(f"account_group_{account_id}", account.get("group_name") or "tous")
            enriched = {**account, "group_name": group_name, "active_for_day": 1}
            label_text = f"{account.get('name','')} {account.get('username','')} {group_name}".lower()
            if query and query not in label_text:
                continue
            visible_accounts.append(enriched)
        with top_d:
            st.markdown(f"<div class='accounts-count'>{len(visible_accounts)} of {len(accounts)}</div>", unsafe_allow_html=True)
        selected_total = sum(
            1 for account in accounts
            if st.session_state.get(f"account_use_{int(account['id'])}", False)
        )
        selected_visible = sum(
            1 for account in visible_accounts
            if st.session_state.get(f"account_use_{int(account['id'])}", False)
        )
        st.markdown(
            "<div class='accounts-selection-line'>"
            f"<strong>{selected_total} comptes sélectionnés</strong>"
            f"<span>{selected_visible}/{len(visible_accounts)} dans cette vue</span>"
            "</div>",
            unsafe_allow_html=True,
        )

        selected_accounts_preview = [
            account for account in accounts
            if st.session_state.get(f"account_use_{int(account['id'])}", False)
        ]
        with st.expander(f"Comptes sélectionnés ({len(selected_accounts_preview)})", expanded=False):
            header_left, header_right = st.columns([2, 1])
            with header_left:
                st.markdown(f"**Comptes sélectionnés · {len(selected_accounts_preview)}**")
            with header_right:
                if st.button("Vider sélection", disabled=not selected_accounts_preview):
                    st.session_state["selected_group_filters"] = []
                    st.session_state["manual_included_accounts"] = []
                    st.session_state["manual_excluded_accounts"] = []
                    st.session_state.pop("_account_group_signature", None)
                    for account in selected_accounts_preview:
                        st.session_state[f"account_use_{int(account['id'])}"] = False
                    mark_group_config_dirty()
                    st.rerun()
            if selected_accounts_preview:
                preview_cols = st.columns(3)
                for idx, account in enumerate(selected_accounts_preview[:18]):
                    account_id = int(account["id"])
                    group_name = st.session_state.get(f"account_group_{account_id}", account.get("group_name") or "tous")
                    with preview_cols[idx % 3]:
                        st.markdown(
                            f"**{h(account_label(account))}**  \n"
                            f"{render_group_badge(group_name, group_color_by_name.get(group_name))}",
                            unsafe_allow_html=True,
                        )
                if len(selected_accounts_preview) > 18:
                    st.caption(f"+ {len(selected_accounts_preview) - 18} autres comptes sélectionnés.")
            else:
                st.caption("Aucun compte sélectionné pour l'instant.")

        continue_left, continue_right = st.columns([2, 1])
        with continue_left:
            if selected_accounts_preview:
                st.caption("Sélection prête. Tu peux encore ajuster un compte dans le tableau, ou passer à la cadence.")
            else:
                st.caption("Prends au moins un groupe ou sélectionne des comptes avant de continuer.")
        with continue_right:
            if st.button("Continuer vers Cadence", type="primary", disabled=not selected_accounts_preview):
                # Build the exact selection now: rerunning before the table's final
                # persistence pass previously left Cadence with an older one-account list.
                continuation_groups: dict[str, dict] = {}
                continuation_accounts: list[dict] = []
                for account in selected_accounts_preview:
                    account_id = int(account["id"])
                    group_name = st.session_state.get(f"account_group_{account_id}", account.get("group_name") or "tous")
                    selected_account = {**account, "group_name": group_name, "active_for_day": 1}
                    continuation_groups.setdefault(group_name, {"accounts": []})["accounts"].append(selected_account)
                    continuation_accounts.append(selected_account)
                    db.update_account_preferences(account_id, group_name, True, True)
                st.session_state["grouped_accounts"] = continuation_groups
                st.session_state["selected_accounts"] = continuation_accounts
                persist_group_config_if_needed(force=True)
                st.session_state["account_step_done"] = True
                st.session_state["active_step"] = 1
                st.session_state["app_page"] = "cadence"
                st.rerun()

        st.markdown(
            "<div class='accounts-shell-lite'>"
            "<div class='account-table-head'>"
            "<span></span><span>COMPTE</span><span>GROUPE</span><span>PROCHAIN POST</span><span></span>"
            "</div>"
            "</div>",
            unsafe_allow_html=True,
        )

        next_by_account = next_post_map(db.list_scheduled())
        if not visible_accounts:
            render_locked_step(
                "Aucun compte trouvé avec ces filtres.",
                ["Change la recherche ou sélectionne un autre groupe."],
            )

        rows = []
        manual_included = set(st.session_state.get("manual_included_accounts", []))
        manual_excluded = set(st.session_state.get("manual_excluded_accounts", []))
        for row_index, account in enumerate(visible_accounts):
            account_id = int(account["id"])
            if row_index:
                st.markdown("<div class='account-row-divider'></div>", unsafe_allow_html=True)
            row_cols = st.columns([.32, 2.25, .9, .78, .3])
            with row_cols[0]:
                use_account = st.checkbox(
                    "Utiliser",
                    key=f"account_use_{account_id}",
                    label_visibility="collapsed",
                    on_change=mark_group_config_dirty,
                )
            with row_cols[1]:
                avatar_url = account.get("avatar_url")
                display_name = account.get("name") or account_label(account)
                handle = account_label(account)
                avatar_html = (
                    f"<span class='account-avatar'><img src='{h(avatar_url)}' alt=''></span>"
                    if avatar_url
                    else f"<span class='account-avatar-fallback'>{h(account_initials(account))}</span>"
                )
                st.markdown(
                    "<div class='account-person'>"
                    f"{avatar_html}"
                    "<div class='account-name'>"
                    f"<strong>{h(display_name)}</strong>"
                    f"<small>{h(handle)}</small>"
                    "</div></div>",
                    unsafe_allow_html=True,
                )
            with row_cols[2]:
                current_group = st.session_state.get(f"account_group_{account_id}", account.get("group_name") or "tous")
                group_button_label = current_group if len(current_group) <= 18 else f"{current_group[:17]}..."
                selected_group = current_group
                with st.popover(group_button_label, help="Changer le groupe"):
                    st.caption("Choisir un groupe")
                    for option_index, group_name in enumerate(group_options):
                        if st.button(
                            group_name,
                            key=f"account_group_choice_{account_id}_{option_index}",
                            type="primary" if group_name == current_group else "secondary",
                        ):
                            st.session_state[f"account_group_{account_id}"] = group_name
                            mark_group_config_dirty()
                            st.rerun()
            with row_cols[3]:
                st.markdown(
                    f"<span class='next-post-pill'>{h(next_by_account.get(account_id, '-'))}</span>",
                    unsafe_allow_html=True,
                )
            base_use = selected_group in st.session_state.get("selected_group_filters", [])
            if bool(use_account) == base_use:
                manual_included.discard(account_id)
                manual_excluded.discard(account_id)
            elif bool(use_account):
                manual_included.add(account_id)
                manual_excluded.discard(account_id)
            else:
                manual_excluded.add(account_id)
                manual_included.discard(account_id)
            with row_cols[4]:
                account_url = account_threads_url(account) or account.get("url")
                action_link = (
                    f"<a href='{h(account_url)}' target='_blank' rel='noreferrer' title='Ouvrir ce compte dans Threads' aria-label='Ouvrir ce compte dans Threads'>&nearr;</a>"
                    if account_url
                    else ""
                )
                st.markdown(f"<div class='account-actions'>{action_link}</div>", unsafe_allow_html=True)
            rows.append(
                {
                    "use": bool(use_account),
                    "id": account_id,
                    "compte": account_label(account),
                    "group": selected_group,
                    "active": True,
                    "url": account.get("url", ""),
                }
            )
        st.session_state["manual_included_accounts"] = sorted(manual_included)
        st.session_state["manual_excluded_accounts"] = sorted(manual_excluded)

        hidden_ids = {int(account["id"]) for account in visible_accounts}
        for account in accounts:
            account_id = int(account["id"])
            if account_id in hidden_ids:
                continue
            rows.append(
                {
                    "use": bool(st.session_state.get(f"account_use_{account_id}", False)),
                    "id": account_id,
                    "compte": account_label(account),
                    "group": st.session_state.get(f"account_group_{account_id}", account.get("group_name") or "tous"),
                    "active": True,
                    "url": account.get("url", ""),
                }
            )
        edited_accounts = pd.DataFrame(rows)
        grouped = build_grouped_accounts(accounts, edited_accounts)
        selected_accounts = [account for group in grouped.values() for account in group["accounts"]]
        st.session_state["grouped_accounts"] = grouped
        st.session_state["selected_accounts"] = selected_accounts
        persist_group_config_if_needed()
        if selected_accounts:
            st.success(f"{len(selected_accounts)} comptes prêts dans {len(grouped)} groupes.")
        else:
            st.warning("Aucun compte sélectionné. Les étapes suivantes restent bloquées.")
        render_group_summary(grouped)

if active_page != "dashboard" and active_step == 1:
    if not st.session_state.get("selected_accounts"):
        st.subheader("Cadence de publication")
        render_locked_step(
            "Étape bloquée: choisis d'abord les comptes.",
            ["Va dans 1. Comptes, sélectionne un ou plusieurs groupes, puis ajuste les comptes si besoin."],
        )
    else:
        current = settings()
        cadence_signature = (
            current["publish_date"],
            current["publish_end_date"],
            current["start_time"],
            current["end_time"],
            current["count_mode"],
            int(current["posts_min"]),
            int(current["posts_max"]),
            int(current["min_interval"]),
            bool(current.get("avoid_same_text", False)),
            int(current.get("same_text_gap", 60)),
            current["caption_mode"],
        )
        if st.session_state.get("_cadence_form_signature") != cadence_signature:
            st.session_state["cadence_publish_date"] = current["publish_date"]
            st.session_state["cadence_publish_end_date"] = current["publish_end_date"]
            st.session_state["cadence_show_end_date"] = current["publish_end_date"] > current["publish_date"]
            st.session_state["cadence_start_time"] = current["start_time"]
            st.session_state["cadence_end_time"] = current["end_time"]
            st.session_state["cadence_count_mode"] = current["count_mode"]
            st.session_state["cadence_posts_min"] = int(current["posts_min"])
            st.session_state["cadence_posts_max"] = int(current["posts_max"])
            st.session_state["cadence_posts_exact"] = int(current["posts_min"])
            st.session_state["cadence_posts_range"] = (
                int(current["posts_min"]),
                max(int(current["posts_max"]), int(current["posts_min"])),
            )
            st.session_state["cadence_posts_range_min"] = int(current["posts_min"])
            st.session_state["cadence_posts_range_max"] = max(int(current["posts_max"]), int(current["posts_min"]))
            st.session_state["cadence_min_interval"] = int(current["min_interval"])
            st.session_state["cadence_avoid_same_text"] = bool(current.get("avoid_same_text", False))
            st.session_state["cadence_same_text_gap"] = int(current.get("same_text_gap", 60))
            st.session_state["cadence_caption_mode"] = current["caption_mode"]
            st.session_state["_cadence_form_signature"] = cadence_signature
        st.session_state.setdefault("cadence_posts_exact", int(current["posts_min"]))
        st.session_state.setdefault(
            "cadence_posts_range",
            (int(current["posts_min"]), max(int(current["posts_max"]), int(current["posts_min"]))),
        )
        st.session_state.setdefault("cadence_posts_range_min", int(current["posts_min"]))
        st.session_state.setdefault("cadence_posts_range_max", max(int(current["posts_max"]), int(current["posts_min"])))
        selected_count = len(st.session_state.get("selected_accounts", []))
        selected_group_count = len(st.session_state.get("grouped_accounts", {}))
        st.markdown(
            "<section class='cadence-heading'><div><span>CADENCE</span><h1>Planifier les publications</h1></div>"
            f"<div class='cadence-summary-top'><i></i><b>{selected_count} comptes</b> · {selected_group_count} groupes</div></section>",
            unsafe_allow_html=True,
        )
        st.session_state.setdefault("cadence_show_end_date", current["publish_end_date"] > current["publish_date"])
        count_mode = st.radio(
            "Posts par compte",
            ["Exact", "Range"],
            horizontal=True,
            key="cadence_count_mode",
            help="Exact: même volume pour chaque compte. Range: un nombre aléatoire entre min et max.",
        )
        with st.form("cadence_settings_form"):
            st.markdown("<span class='cadence-form-anchor'></span>", unsafe_allow_html=True)
            if st.session_state["cadence_show_end_date"]:
                date_col, extra_date_col, start_col, end_col, _ = st.columns([1.15, 1.15, .48, .48, .6])
                with date_col:
                    publish_date = st.date_input("Date de début", key="cadence_publish_date")
                with extra_date_col:
                    publish_end_date = st.date_input("Autre date", key="cadence_publish_end_date")
                    remove_extra_date = st.form_submit_button("−", help="Retirer la date supplémentaire")
                    add_extra_date = False
            else:
                date_col, add_col, start_col, end_col, _ = st.columns([1.4, .14, .48, .48, .9])
                with date_col:
                    publish_date = st.date_input("Date", key="cadence_publish_date")
                with add_col:
                    st.markdown("<span class='cadence-date-add'></span>", unsafe_allow_html=True)
                    publish_end_date = publish_date
                    add_extra_date = st.form_submit_button("+", help="Ajouter une autre date de publication")
                    remove_extra_date = False
            with start_col:
                start_time = st.time_input("Début", key="cadence_start_time")
            with end_col:
                end_time = st.time_input("Fin", key="cadence_end_time")

            posts_col, interval_col, summary_col, _ = st.columns([.9, .8, 1.25, .8])
            with posts_col:
                st.markdown("<div class='cadence-section-label'>Posts par compte</div>", unsafe_allow_html=True)
                if count_mode == "Exact":
                    posts_min = st.number_input("Nombre", min_value=0, max_value=50, step=1, key="cadence_posts_exact")
                    posts_max = int(posts_min)
                else:
                    range_min_col, range_max_col = st.columns(2)
                    with range_min_col:
                        posts_min = st.number_input("Min", min_value=0, max_value=50, step=1, key="cadence_posts_range_min")
                    with range_max_col:
                        posts_max = st.number_input("Max", min_value=0, max_value=50, step=1, key="cadence_posts_range_max")
                    posts_min, posts_max = int(posts_min), max(int(posts_max), int(posts_min))
                    st.caption(f"Entre {posts_min} et {posts_max} posts par compte.")
            with interval_col:
                st.markdown("<div class='cadence-section-label'>Rythme</div>", unsafe_allow_html=True)
                min_interval = st.number_input(
                    "Écart min (min)", min_value=1, max_value=1440, step=1,
                    key="cadence_min_interval", help="Temps minimum entre deux posts du même compte.",
                )
                max_possible = max_posts_for_period(publish_date, publish_end_date, start_time, end_time, int(min_interval))
                st.markdown(f"<p class='cadence-capacity'>Capacité: {max_possible} posts max / compte</p>", unsafe_allow_html=True)
            with summary_col:
                total_min, total_max = planned_total_range(selected_count, int(posts_min), int(posts_max))
                st.markdown(
                    "<div class='cadence-total-card'>"
                    "<span>PUBLICATIONS À CRÉER</span>"
                    f"<strong>{total_min if total_min == total_max else f'{total_min}-{total_max}'}</strong>"
                    f"<small>{selected_count} comptes · {posts_min if posts_min == posts_max else f'{posts_min}-{posts_max}'} posts / compte</small>"
                    "</div>",
                    unsafe_allow_html=True,
                )
                if int(posts_max) > max_possible:
                    st.error(f"Maximum possible: {max_possible} posts par compte avec cette fenêtre.")
                elif int(posts_max) == 0:
                    st.warning("0 post: la prochaine preview sera vide.")

            caption_mode = st.radio("Ordre des textes", ["Rotate", "Random"], horizontal=True, key="cadence_caption_mode")
            # Hidden from this compact screen, but preserved for existing plans.
            avoid_same_text = bool(current.get("avoid_same_text", False))
            same_text_gap = int(current.get("same_text_gap", 60))
            _, apply_col, next_col = st.columns([2.1, .7, .7])
            with apply_col:
                apply_cadence = st.form_submit_button("Appliquer", use_container_width=True)
            with next_col:
                next_step = st.form_submit_button("Suivant →", type="primary", use_container_width=True)

        if add_extra_date:
            st.session_state["cadence_show_end_date"] = True
            st.session_state["cadence_publish_end_date"] = publish_date + timedelta(days=1)
            st.rerun()
        if remove_extra_date:
            st.session_state["cadence_show_end_date"] = False
            st.session_state["cadence_publish_end_date"] = publish_date
            st.rerun()
        if apply_cadence or next_step:
            st.session_state["settings"] = {
                "publish_date": publish_date,
                "publish_end_date": publish_end_date,
                "start_time": start_time,
                "end_time": end_time,
                "count_mode": count_mode,
                "posts_min": int(posts_min),
                "posts_max": int(posts_max),
                "min_interval": int(min_interval),
                "avoid_same_text": bool(avoid_same_text),
                "same_text_gap": int(same_text_gap),
                "caption_mode": caption_mode,
            }
            st.session_state["cadence_posts_min"] = int(posts_min)
            st.session_state["cadence_posts_max"] = int(posts_max)
            st.session_state["cadence_posts_range"] = (int(posts_min), int(posts_max))
            st.session_state["_cadence_form_signature"] = (
                publish_date,
                publish_end_date,
                start_time,
                end_time,
                count_mode,
                int(posts_min),
                int(posts_max),
                int(min_interval),
                bool(avoid_same_text),
                int(same_text_gap),
                caption_mode,
            )
            clear_preview_draft("Preview brouillon supprimée: cadence changée. Les posts déjà planifiés restent conservés.")
            if next_step:
                st.session_state["active_step"] = 2
                st.session_state["app_page"] = "posts"
                st.rerun()
            st.success("Cadence appliquée.")
        st.info(distribution_sentence(settings()))

if active_page != "dashboard" and active_step == 2:
    st.subheader("Bibliothèque de posts")
    section_intro(
        "Étape 3",
        "Importe un CSV, travaille son dernier lot, puis choisis les textes qui iront dans la prochaine preview.",
        "Tu peux créer, modifier, désactiver ou enlever des posts localement. Rien ici ne supprime un post déjà envoyé.",
    )
    current = settings()
    selected_count = len(st.session_state.get("selected_accounts", []))
    capacity = max_posts_for_period(current["publish_date"], current["publish_end_date"], current["start_time"], current["end_time"], int(current["min_interval"]))
    posts_min_required, posts_max_required = planned_total_range(selected_count, int(current["posts_min"]), int(current["posts_max"]))
    if not selected_count:
        render_locked_step(
            "Étape bloquée: choisis les comptes avant les posts.",
            ["Va dans 1. Comptes, sélectionne un ou plusieurs groupes, puis reviens ici."],
        )
    elif int(current["posts_max"]) > capacity:
        render_locked_step(
            "Étape bloquée: cadence impossible.",
            [
                f"Capacité actuelle: {capacity} posts max par compte.",
                f"Demande actuelle: {int(current['posts_max'])} posts max par compte.",
                "Va dans 2. Cadence et ajuste la plage horaire, l'intervalle ou le range.",
            ],
        )
    elif int(current["posts_max"]) == 0:
        st.info(
            "Cadence à 0: aucun post n'est requis. Va dans 4. Preview et clique sur Générer preview "
            "pour vider le brouillon courant sans toucher aux posts déjà planifiés/envoyés."
        )
    else:
        st.info(
            f"{selected_count} comptes sélectionnés. Besoin planning: "
            f"{posts_min_required if posts_min_required == posts_max_required else f'{posts_min_required}-{posts_max_required}'} publications. "
            "Si tu sélectionnes moins de textes que nécessaire, ils tourneront en rotation."
        )
        posts_view = st.radio(
            "Section Posts",
            ["Bibliothèque", "Médias"],
            horizontal=True,
            key="posts_workspace_view",
            label_visibility="collapsed",
        )
        if posts_view == "Bibliothèque":
            render_post_library_workspace()
            st.stop()

        st.markdown("#### Dossiers média")
        folders = db.list_media_folders()
        media_col1, media_col2, media_col3 = st.columns([1, 2, 1])
        with media_col1:
            folder_name = st.text_input("Nom dossier média", placeholder="ex: selfies_jade")
        with media_col2:
            folder_media_ids = st.text_area("Media IDs du dossier", height=80, placeholder="12345, 67890, 99999")
        with media_col3:
            folder_note = st.text_input("Note dossier")
            if st.button("Sauver dossier média", disabled=not folder_name.strip()):
                created = db.upsert_media_folder(folder_name, folder_media_ids, folder_note)
                st.success("Dossier créé." if created else "Dossier mis à jour.")
                folders = db.list_media_folders()
        if folders:
            st.dataframe(
                pd.DataFrame([{"dossier": f["name"], "media": f["media_count"], "note": f.get("note", "")} for f in folders]),
                hide_index=True,
                use_container_width=True,
            )
        folder_options = [""] + [f["name"] for f in folders]

        st.markdown("#### Photos")
        st.caption("Ajoute les images d'abord. Ensuite ouvre la galerie, sélectionne une photo, puis range-la dans un groupe ou ajoute son media ID Postoria.")
        upload_col, gallery_col = st.columns([1.35, 1])
        with upload_col:
            photo_uploads = st.file_uploader(
                "+ Ajouter des photos",
                type=["png", "jpg", "jpeg", "webp"],
                accept_multiple_files=True,
                help="Import local uniquement. Tu pourras choisir le groupe après dans la galerie.",
            )
            if st.button("Ajouter les photos", disabled=not photo_uploads, use_container_width=True):
                saved = 0
                for uploaded_photo in photo_uploads or []:
                    db.add_photo_asset(
                        "",
                        uploaded_photo.name,
                        "",
                        uploaded_photo.type or "image/jpeg",
                        uploaded_photo.getvalue(),
                        "",
                    )
                    saved += 1
                st.success(f"{saved} photos ajoutées. Ouvre la galerie pour les ranger.")
                st.session_state["show_photo_gallery"] = True
                st.rerun()
        with gallery_col:
            photo_groups = db.list_photo_groups()
            photo_assets = db.list_photo_assets()
            st.metric("Photos", len(photo_assets))
            st.metric("Groupes", len([group for group in photo_groups if group["name"] != db.DEFAULT_PHOTO_GROUP]))
            toggle_label = "Fermer galerie" if st.session_state.get("show_photo_gallery") else "Ouvrir galerie"
            if st.button(toggle_label, disabled=not photo_assets, use_container_width=True):
                st.session_state["show_photo_gallery"] = not st.session_state.get("show_photo_gallery", False)
                st.rerun()

        photo_groups = db.list_photo_groups()
        photo_assets = db.list_photo_assets()
        if photo_groups:
            visible_groups = [group for group in photo_groups if group["name"] != db.DEFAULT_PHOTO_GROUP]
            if visible_groups:
                st.dataframe(
                    pd.DataFrame(
                        [
                            {
                                "groupe": group["name"],
                                "photos": int(group.get("photo_count") or 0),
                                "avec_media_id": int(group.get("postoria_ready_count") or 0),
                                "note": group.get("note", ""),
                            }
                            for group in visible_groups
                        ]
                    ),
                    hide_index=True,
                    use_container_width=True,
                )

        if photo_assets and st.session_state.get("show_photo_gallery"):
            gallery_filter_options = ["Toutes"] + [group["name"] for group in photo_groups]
            gallery_filter = choose_option("Voir", gallery_filter_options, key="photo_gallery_filter")
            gallery_assets = photo_assets if gallery_filter == "Toutes" else db.list_photo_assets(str(gallery_filter))
            st.markdown("##### Galerie")
            asset_cols = st.columns(4)
            for idx, asset in enumerate(gallery_assets[:24]):
                with asset_cols[idx % 4]:
                    st.image(asset["image_bytes"], use_container_width=True)
                    media_badge = asset.get("media_id") or "media ID manquant"
                    st.caption(f"{asset['group_name']} · {media_badge}")
                    if st.button("Choisir", key=f"choose_photo_asset_{asset['id']}", use_container_width=True):
                        st.session_state["selected_photo_asset_id"] = int(asset["id"])
                        st.rerun()
            if len(gallery_assets) > 24:
                st.caption(f"{len(gallery_assets) - 24} photos masquées. Filtre par groupe pour réduire la galerie.")

        selected_photo_id = st.session_state.get("selected_photo_asset_id")
        selected_photo = db.get_photo_asset(int(selected_photo_id)) if selected_photo_id else None
        if selected_photo:
            st.markdown("##### Photo sélectionnée")
            detail_col1, detail_col2 = st.columns([1, 1.4])
            with detail_col1:
                st.image(selected_photo["image_bytes"], use_container_width=True)
                st.caption(f"{selected_photo['name']} · groupe actuel: {selected_photo['group_name']}")
            with detail_col2:
                existing_group_names = [group["name"] for group in db.list_photo_groups() if group["name"] != db.DEFAULT_PHOTO_GROUP]
                new_group_name = st.text_input("Créer nouveau groupe", placeholder="ex: tenue rouge, selfie miroir")
                group_options = existing_group_names + ([new_group_name.strip()] if new_group_name.strip() and new_group_name.strip() not in existing_group_names else [])
                target_group = choose_option(
                    "Mettre dans le groupe",
                    group_options or [db.DEFAULT_PHOTO_GROUP],
                    key=f"selected_photo_group_{selected_photo['id']}",
                )
                selected_media_id = st.text_input(
                    "Media ID Postoria",
                    value=str(selected_photo.get("media_id") or ""),
                    placeholder="optionnel, nécessaire pour l'envoi avec image",
                    key=f"selected_photo_media_{selected_photo['id']}",
                )
                selected_note = st.text_input(
                    "Note",
                    value=str(selected_photo.get("note") or ""),
                    placeholder="optionnel",
                    key=f"selected_photo_note_{selected_photo['id']}",
                )
                save_detail_col, close_detail_col = st.columns(2)
                with save_detail_col:
                    if st.button("Enregistrer photo", use_container_width=True):
                        db.update_photo_asset(
                            int(selected_photo["id"]),
                            str(target_group),
                            selected_media_id,
                            selected_note,
                        )
                        st.success("Photo mise à jour.")
                        st.rerun()
                with close_detail_col:
                    if st.button("Désélectionner", use_container_width=True):
                        st.session_state.pop("selected_photo_asset_id", None)
                        st.rerun()

        # The library lives in its own view above; the media view stops here.
        st.stop()
        st.markdown("### Bibliothèque")
        import_col, manual_col = st.columns(2)
        with import_col:
            uploaded_files = st.file_uploader(
                "Importer CSV de contenus",
                type=["csv"],
                accept_multiple_files=True,
                help="Colonnes: text, media_ids, media_folder, reply_1, reply_2. Autres colonnes = variables {colonne}.",
            )
            if uploaded_files:
                existing_hashes = {
                    str(batch.get("file_hash") or "")
                    for batch in db.list_post_import_batches()
                    if str(batch.get("file_hash") or "").strip()
                }
                prepared_files = []
                for uploaded in uploaded_files:
                    csv_bytes = uploaded.getvalue()
                    csv_hash = hashlib.sha256(csv_bytes).hexdigest()
                    prepared_files.append((uploaded, csv_bytes, csv_hash, csv_hash in existing_hashes))
                st.caption(f"{len(prepared_files)} CSV prêts à importer.")
                already_count = sum(1 for _, _, _, exists in prepared_files if exists)
                if already_count:
                    st.info(f"{already_count} fichier(s) semblent déjà enregistrés. Ils seront ignorés sauf si tu forces le réimport.")
                allow_reimport = st.checkbox("Autoriser le réimport des CSV déjà enregistrés", key="allow_reimport_csv_files")
                if st.button("Importer les CSV", disabled=not prepared_files, use_container_width=True):
                    all_imported_ids: set[int] = set()
                    latest_batch_id = ""
                    summaries = []
                    for uploaded, csv_bytes, csv_hash, exists in prepared_files:
                        if exists and not allow_reimport:
                            summaries.append(f"{uploaded.name}: déjà enregistré, ignoré")
                            continue
                        frame = pd.read_csv(io.BytesIO(csv_bytes))
                        records = make_post_records(frame)
                        added, skipped, imported_ids = db.add_posts_with_ids(records)
                        latest_batch_id = db.record_post_import_batch(
                            uploaded.name,
                            csv_hash,
                            len(csv_bytes),
                            added,
                            skipped,
                            imported_ids,
                        )
                        all_imported_ids.update(int(post_id) for post_id in imported_ids)
                        summaries.append(f"{uploaded.name}: {added} nouveaux, {skipped} doublons/vides, {len(set(imported_ids))} liés")
                    if latest_batch_id:
                        latest_imported_ids = set(db.post_ids_for_import_batch(latest_batch_id))
                        imported_posts = [
                            post for post in db.list_posts(active_only=False)
                            if int(post["id"]) in latest_imported_ids
                        ]
                        st.session_state["selected_posts"] = imported_posts
                        st.session_state["posts_selection_explicit"] = True
                        st.session_state["_selected_posts_signature"] = tuple(sorted(latest_imported_ids))
                        st.session_state["post_import_batch_filter"] = latest_batch_id
                        st.session_state["posts_editor_version"] = st.session_state.get("posts_editor_version", 0) + 1
                        if clear_preview_draft("Preview brouillon supprimée: nouveaux CSV importés. Les posts déjà planifiés restent conservés."):
                            st.info("Ancienne preview supprimée. Les posts déjà planifiés restent conservés.")
                    st.success(
                        "Import terminé. Le dernier CSV importé est maintenant affiché et ses posts sont sélectionnés. "
                        + " | ".join(summaries[:4])
                    )
                    if len(summaries) > 4:
                        st.caption("Autres fichiers: " + " | ".join(summaries[4:]))
                    posts = db.list_posts(active_only=False)
        with manual_col:
            with st.form("manual_posts"):
                bulk = st.text_area("Ajouter textes", height=120, placeholder="Un texte par ligne")
                media_for_bulk = st.text_input("Media IDs pour ces textes")
                folder_for_bulk = choose_option("Dossier média", folder_options, horizontal=len(folder_options) <= 4)
                variables_for_bulk = st.text_input("Variables", placeholder="firstname=Lucas, city=Paris")
                replies_for_bulk = st.text_area("Réponses automatiques", height=90, placeholder="Une réponse par ligne")
                add_manual = st.form_submit_button("Ajouter")
                if add_manual:
                    records = [
                        {
                            "caption": line.strip(),
                            "media_ids": media_for_bulk,
                            "media_folder": folder_for_bulk,
                            "variables": parse_variables_text(variables_for_bulk),
                            "reply_chain": db.parse_lines(replies_for_bulk),
                        }
                        for line in bulk.splitlines()
                        if line.strip()
                    ]
                    added, skipped, imported_ids = db.add_posts_with_ids(records)
                    imported_posts = [
                        post for post in db.list_posts(active_only=False)
                        if int(post["id"]) in set(imported_ids)
                    ]
                    st.session_state["selected_posts"] = imported_posts
                    st.session_state["posts_selection_explicit"] = True
                    st.session_state["_selected_posts_signature"] = tuple(sorted(imported_ids))
                    st.session_state["posts_editor_version"] = st.session_state.get("posts_editor_version", 0) + 1
                    if imported_ids:
                        if clear_preview_draft("Preview brouillon supprimée: nouveaux posts ajoutés. Les posts déjà planifiés restent conservés."):
                            st.info("Ancienne preview supprimée. Les posts déjà planifiés restent conservés.")
                    st.success(f"{added} posts ajoutés, {skipped} doublons réutilisés. Lot actif: {len(imported_posts)} posts.")
                    posts = db.list_posts(active_only=False)

        all_posts = db.list_posts(active_only=False)
        import_batches = db.list_post_import_batches()
        selected_import_filter = "Tous les posts"
        if import_batches:
            st.markdown("#### Lots CSV enregistrés")
            filter_col, hint_col = st.columns([1, 1.4])
            batch_lookup = {str(batch["id"]): batch for batch in import_batches}
            with filter_col:
                selected_import_filter = choose_option(
                    "Filtrer la bibliothèque",
                    ["Tous les posts"] + [str(batch["id"]) for batch in import_batches],
                    key="post_import_batch_filter",
                    format_func=lambda value: "Tous les posts" if value == "Tous les posts" else (
                        f"{batch_lookup[str(value)]['file_name']} · {int(batch_lookup[str(value)].get('linked_count') or 0)} posts"
                    ),
                )
            with hint_col:
                st.caption("Chaque CSV importé reste enregistré ici. Tu peux filtrer un lot précis, puis sélectionner/enlever ses posts.")
            cards = "".join(
                import_batch_card_html(batch, active=str(batch["id"]) == str(selected_import_filter))
                for batch in import_batches[:8]
            )
            st.markdown(f"<div class='import-batch-grid'>{cards}</div>", unsafe_allow_html=True)

        if selected_import_filter != "Tous les posts":
            batch_post_ids = set(db.post_ids_for_import_batch(str(selected_import_filter)))
            posts = [post for post in all_posts if int(post["id"]) in batch_post_ids]
        else:
            posts = all_posts

        if not posts:
            st.warning("Aucun post dans cette vue.")
        else:
            selected_post_ids = {int(p["id"]) for p in st.session_state.get("selected_posts", [])}
            selection_explicit = bool(st.session_state.get("posts_selection_explicit", False))
            default_posts = not selection_explicit and not selected_post_ids
            active_posts = [post for post in posts if bool(post.get("is_active", 1))]
            posts_with_media = [
                post for post in active_posts
                if db.parse_media_ids(post.get("media_ids")) or str(post.get("media_folder") or "").strip()
            ]
            selected_count = len(active_posts) if default_posts else len(selected_post_ids)
            selected_media_count = sum(
                1
                for post in posts
                if bool(post.get("is_active", 1))
                and (default_posts or int(post["id"]) in selected_post_ids)
                and (db.parse_media_ids(post.get("media_ids")) or str(post.get("media_folder") or "").strip())
            )
            st.markdown(
                "<div class='posts-control-panel'>"
                "<strong>Choisir les posts à utiliser</strong>"
                "<p>Décoche plusieurs lignes tranquillement, puis clique sur Appliquer. Le tableau ne relance plus l'app à chaque clic.</p>"
                "</div>",
                unsafe_allow_html=True,
            )
            st.markdown(
                "<div class='posts-stats'>"
                f"<div><span>Bibliothèque</span><b>{len(posts)}</b></div>"
                f"<div><span>Sélection</span><b>{selected_count}</b></div>"
                f"<div><span>Avec média</span><b>{selected_media_count}</b></div>"
                f"<div><span>Inactifs</span><b>{len(posts) - len(active_posts)}</b></div>"
                "</div>",
                unsafe_allow_html=True,
            )
            quick_a, quick_b, quick_c = st.columns(3)
            if quick_a.button("Tout sélectionner", use_container_width=True):
                clear_preview_draft("Preview brouillon supprimée: sélection de posts changée. Les posts déjà planifiés restent conservés.")
                st.session_state["selected_posts"] = active_posts
                st.session_state["posts_selection_explicit"] = True
                st.session_state["_selected_posts_signature"] = tuple(sorted(int(post["id"]) for post in active_posts))
                st.session_state["posts_editor_version"] = st.session_state.get("posts_editor_version", 0) + 1
                st.rerun()
            if quick_b.button("Tout décocher", use_container_width=True):
                clear_preview_draft("Preview brouillon supprimée: sélection de posts changée. Les posts déjà planifiés restent conservés.")
                st.session_state["selected_posts"] = []
                st.session_state["posts_selection_explicit"] = True
                st.session_state["_selected_posts_signature"] = tuple()
                st.session_state["posts_editor_version"] = st.session_state.get("posts_editor_version", 0) + 1
                st.rerun()
            if quick_c.button("Seulement avec média", use_container_width=True, disabled=not posts_with_media):
                clear_preview_draft("Preview brouillon supprimée: sélection de posts changée. Les posts déjà planifiés restent conservés.")
                st.session_state["selected_posts"] = posts_with_media
                st.session_state["posts_selection_explicit"] = True
                st.session_state["_selected_posts_signature"] = tuple(sorted(int(post["id"]) for post in posts_with_media))
                st.session_state["posts_editor_version"] = st.session_state.get("posts_editor_version", 0) + 1
                st.rerun()

            readable_a, readable_b, readable_c = st.columns([1, 1.4, 1])
            with readable_a:
                readable_filter = choose_option(
                    "Vue texte",
                    ["Sélection actuelle", "Tous", "Avec média"],
                    key="posts_readable_filter",
                )
            with readable_b:
                readable_query = st.text_input("Chercher dans les posts", placeholder="mot, phrase, media ID...", key="posts_readable_query")
            with readable_c:
                readable_limit = st.slider("Posts visibles", 6, 60, 18, 6, key="posts_readable_limit")

            readable_posts = []
            needle = readable_query.strip().lower()
            for post in posts:
                post_id = int(post["id"])
                is_active = bool(post.get("is_active", 1))
                is_selected = is_active and (default_posts or post_id in selected_post_ids)
                has_media = bool(db.parse_media_ids(post.get("media_ids")) or str(post.get("media_folder") or "").strip())
                haystack = " ".join(
                    [
                        str(post.get("caption") or ""),
                        media_ids_text(post.get("media_ids")),
                        str(post.get("media_folder") or ""),
                        variables_text(post.get("variables")),
                    ]
                ).lower()
                if readable_filter == "Sélection actuelle" and not is_selected:
                    continue
                if readable_filter == "Avec média" and not has_media:
                    continue
                if needle and needle not in haystack:
                    continue
                readable_posts.append((post, is_selected, has_media))

            if readable_posts:
                visible_readable_posts = readable_posts[: int(readable_limit)]
                visible_ids = [int(post["id"]) for post, _, _ in visible_readable_posts]
                st.caption(f"Sélection visuelle : {len(visible_readable_posts)} affichés sur {len(readable_posts)} posts filtrés.")
                visual_a, visual_b = st.columns(2)
                if visual_a.button("Sélectionner les posts affichés", disabled=not visible_ids, use_container_width=True):
                    base_selected_ids = {
                        int(post["id"]) for post in active_posts
                    } if default_posts else set(selected_post_ids)
                    base_selected_ids.update(visible_ids)
                    selected_posts_now = [
                        post for post in all_posts
                        if int(post["id"]) in base_selected_ids and bool(post.get("is_active", 1))
                    ]
                    clear_preview_draft("Preview brouillon supprimée: sélection de posts changée. Les posts déjà planifiés restent conservés.")
                    st.session_state["selected_posts"] = selected_posts_now
                    st.session_state["posts_selection_explicit"] = True
                    st.session_state["_selected_posts_signature"] = tuple(sorted(base_selected_ids))
                    st.session_state["posts_editor_version"] = st.session_state.get("posts_editor_version", 0) + 1
                    st.rerun()
                if visual_b.button("Désélectionner les posts affichés", disabled=not visible_ids, use_container_width=True):
                    base_selected_ids = {
                        int(post["id"]) for post in active_posts
                    } if default_posts else set(selected_post_ids)
                    base_selected_ids.difference_update(visible_ids)
                    selected_posts_now = [
                        post for post in all_posts
                        if int(post["id"]) in base_selected_ids and bool(post.get("is_active", 1))
                    ]
                    clear_preview_draft("Preview brouillon supprimée: sélection de posts changée. Les posts déjà planifiés restent conservés.")
                    st.session_state["selected_posts"] = selected_posts_now
                    st.session_state["posts_selection_explicit"] = True
                    st.session_state["_selected_posts_signature"] = tuple(sorted(base_selected_ids))
                    st.session_state["posts_editor_version"] = st.session_state.get("posts_editor_version", 0) + 1
                    st.rerun()

                base_selected_ids = {
                    int(post["id"]) for post in active_posts
                } if default_posts else set(selected_post_ids)
                visual_selected_ids = set(base_selected_ids)
                for post, is_selected, has_media in visible_readable_posts:
                    post_id = int(post["id"])
                    checked = render_post_visual_card(
                        post,
                        post_id in base_selected_ids,
                        has_media,
                        f"visual_post_use_{post_id}_{st.session_state.get('posts_editor_version', 0)}",
                    )
                    if checked:
                        visual_selected_ids.add(post_id)
                    else:
                        visual_selected_ids.discard(post_id)

                if visual_selected_ids != base_selected_ids:
                    selected_posts_now = [
                        post for post in all_posts
                        if int(post["id"]) in visual_selected_ids and bool(post.get("is_active", 1))
                    ]
                    clear_preview_draft("Preview brouillon supprimée: sélection de posts changée. Les posts déjà planifiés restent conservés.")
                    st.session_state["selected_posts"] = selected_posts_now
                    st.session_state["posts_selection_explicit"] = True
                    st.session_state["_selected_posts_signature"] = tuple(sorted(visual_selected_ids))
                    st.session_state["posts_editor_version"] = st.session_state.get("posts_editor_version", 0) + 1
                    st.rerun()
            else:
                st.info("Aucun post dans cette vue lisible avec les filtres actuels.")

            post_rows = []
            for post in posts:
                post_rows.append(
                    {
                        "remove": False,
                        "use": bool(post.get("is_active", 1)) and (default_posts or int(post["id"]) in selected_post_ids),
                        "active": bool(post.get("is_active", 1)),
                        "id": int(post["id"]),
                        "caption": post["caption"],
                        "media_ids": media_ids_text(post.get("media_ids")),
                        "media_folder": post.get("media_folder", ""),
                        "variables": variables_text(post.get("variables")),
                        "reply_chain": "\n".join(post.get("reply_chain") or []),
                        "photo_note": post.get("photo_note", ""),
                        "used": int(post.get("total_used", 0) or 0),
                    }
                )
            with st.form("posts_selection_form"):
                st.markdown("<div class='posts-editor-wrap'>", unsafe_allow_html=True)
                edited_posts = st.data_editor(
                    pd.DataFrame(post_rows),
                    hide_index=True,
                    use_container_width=True,
                    height=560,
                    column_order=[
                        "remove", "use", "caption", "media_ids", "media_folder", "variables",
                        "reply_chain", "photo_note", "active", "used", "id",
                    ],
                    column_config={
                        "remove": st.column_config.CheckboxColumn("Enlever", help="Retire localement ce post de la bibliothèque."),
                        "use": st.column_config.CheckboxColumn("Publier", help="Inclus dans la prochaine preview."),
                        "active": st.column_config.CheckboxColumn("Actif", help="Désactive le post dans la bibliothèque."),
                        "id": st.column_config.NumberColumn("ID", disabled=True, width="small"),
                        "caption": st.column_config.TextColumn("Texte", width="large"),
                        "media_ids": st.column_config.TextColumn("Media IDs", width="medium"),
                        "media_folder": st.column_config.TextColumn("Dossier", disabled=True, width="small"),
                        "variables": st.column_config.TextColumn("Variables", width="medium"),
                        "reply_chain": st.column_config.TextColumn("Replies", width="medium"),
                        "photo_note": st.column_config.TextColumn("Note photo", width="medium"),
                        "used": st.column_config.NumberColumn("Usages", disabled=True, width="small"),
                    },
                    disabled=["id", "media_folder", "used"],
                    key=f"posts_editor_{st.session_state.get('posts_editor_version', 0)}",
                )
                st.markdown("</div>", unsafe_allow_html=True)
                submit_a, submit_b, submit_c = st.columns([1, 1, 1])
                apply_posts = submit_a.form_submit_button("Appliquer sélection", type="primary", use_container_width=True)
                save_posts = submit_b.form_submit_button("Sauver posts/photos", use_container_width=True)
                remove_posts = submit_c.form_submit_button("Enlever cochés", use_container_width=True)

            if remove_posts:
                ids_to_remove = [int(row["id"]) for _, row in edited_posts.iterrows() if bool(row.get("remove", False))]
                if not ids_to_remove:
                    st.warning("Aucun post coché dans la colonne Enlever.")
                else:
                    result = db.delete_or_deactivate_posts(ids_to_remove)
                    removed_ids = set(ids_to_remove)
                    st.session_state["selected_posts"] = [
                        post for post in st.session_state.get("selected_posts", [])
                        if int(post["id"]) not in removed_ids
                    ]
                    st.session_state["posts_selection_explicit"] = True
                    st.session_state["_selected_posts_signature"] = tuple(
                        sorted(int(post["id"]) for post in st.session_state.get("selected_posts", []))
                    )
                    st.session_state["posts_editor_version"] = st.session_state.get("posts_editor_version", 0) + 1
                    clear_preview_draft("Preview brouillon supprimée: posts enlevés de la bibliothèque. Les posts déjà planifiés restent conservés.")
                    st.warning(
                        f"{result['deleted']} supprimés localement, {result['deactivated']} désactivés car déjà utilisés. "
                        "Rien n'est supprimé sur Postoria."
                    )
                    st.rerun()

            if apply_posts or save_posts:
                if save_posts:
                    caption_errors = 0
                    for _, row in edited_posts.iterrows():
                        if not db.update_post_caption(int(row["id"]), str(row.get("caption", ""))):
                            caption_errors += 1
                        db.update_post_metadata(
                            int(row["id"]),
                            row["media_ids"],
                            str(row.get("photo_note", "")),
                            bool(row["active"]),
                            str(row.get("media_folder", "")),
                            parse_variables_text(str(row.get("variables", ""))),
                            str(row.get("reply_chain", "")),
                        )
                    posts = db.list_posts(active_only=False)
                    st.session_state["posts_editor_version"] = st.session_state.get("posts_editor_version", 0) + 1
                    if caption_errors:
                        st.warning(f"{caption_errors} texte(s) non modifiés: vide ou déjà présent dans la bibliothèque.")

                post_by_id = {int(p["id"]): p for p in db.list_posts(active_only=False)}
                selected_posts = []
                for _, row in edited_posts.iterrows():
                    if bool(row["use"]) and bool(row["active"]):
                        base = post_by_id[int(row["id"])]
                        selected_posts.append(
                            {
                                **base,
                                "media_ids": db.parse_media_ids(row["media_ids"]),
                                "media_folder": str(row.get("media_folder", "")),
                                "variables": parse_variables_text(str(row.get("variables", ""))),
                                "reply_chain": db.parse_lines(str(row.get("reply_chain", ""))),
                                "photo_note": str(row.get("photo_note", "")),
                            }
                        )
                st.session_state["selected_posts"] = selected_posts
                st.session_state["posts_selection_explicit"] = True
                selected_signature = tuple(sorted(int(post["id"]) for post in selected_posts))
                previous_signature = st.session_state.get("_selected_posts_signature")
                if previous_signature is not None and previous_signature != selected_signature:
                    clear_preview_draft("Preview brouillon supprimée: sélection de posts changée. Les posts déjà planifiés restent conservés.")
                elif save_posts:
                    clear_preview_draft("Preview brouillon supprimée: posts/photos modifiés. Les posts déjà planifiés restent conservés.")
                st.session_state["_selected_posts_signature"] = selected_signature
                st.success(f"{len(selected_posts)} posts sélectionnés et enregistrés pour la prochaine preview.")

            selected_posts = st.session_state.get("selected_posts", [])
            if selected_posts:
                st.success(f"{len(selected_posts)} posts sélectionnés, dont {sum(1 for p in selected_posts if p.get('media_ids'))} avec photo/media.")
                if len(selected_posts) < int(current["posts_max"]):
                    st.warning(
                        f"{len(selected_posts)} textes pour jusqu'à {int(current['posts_max'])} posts par compte: "
                        "rotation activée, certains textes seront réutilisés."
                    )
            else:
                st.warning("Aucun post sélectionné. Preview bloquée.")

if active_page != "dashboard" and active_step == 3:
    st.subheader("Preview du planning")
    section_intro(
        "Étape 4",
        "Contrôle exactement ce qui va partir: compte, groupe, heure, média, statut, erreurs.",
        "Utilise les filtres avant de passer à l'envoi. Failed et erreurs restent visibles ici.",
    )
    current = settings()
    capacity = max_posts_for_period(current["publish_date"], current["publish_end_date"], current["start_time"], current["end_time"], int(current["min_interval"]))
    enough_context = (
        bool(st.session_state.get("grouped_accounts"))
        and int(current["posts_max"]) <= capacity
        and (int(current["posts_max"]) == 0 or bool(st.session_state.get("selected_posts")))
    )
    if not enough_context:
        blockers = []
        if not st.session_state.get("grouped_accounts"):
            blockers.append("Aucun compte sélectionné.")
        if int(current["posts_max"]) > capacity:
            blockers.append("Cadence impossible avec la plage horaire et l'intervalle actuels.")
        if int(current["posts_max"]) > 0 and not st.session_state.get("selected_posts"):
            blockers.append("Aucun post sélectionné.")
        render_locked_step("Preview bloquée: termine les étapes précédentes.", blockers)
    default_batch_name = f"Lot {datetime.now(ZoneInfo(APP_TZ)).strftime('%Y-%m-%d %H:%M')}"
    preview_batch_name = st.text_input(
        "Nom du nouveau lot preview",
        value=st.session_state.get("next_preview_batch_name", default_batch_name),
        help="Nom local seulement. Il sert à retrouver/restaurer une preview, pas à publier.",
    )
    st.session_state["next_preview_batch_name"] = preview_batch_name
    if st.button("Générer preview", disabled=not enough_context):
        try:
            rows = generate_schedule(
                selected_posts=st.session_state.get("selected_posts", []),
                grouped_accounts=st.session_state.get("grouped_accounts", {}),
                publish_date=current["publish_date"],
                publish_end_date=current["publish_end_date"],
                start_time=current["start_time"],
                end_time=current["end_time"],
                posts_per_account=int(current["posts_min"]),
                posts_per_account_max=int(current["posts_max"]),
                min_interval_minutes=int(current["min_interval"]),
                same_caption_margin_minutes=int(current["same_text_gap"]) if current.get("avoid_same_text") else 0,
                tz_name=APP_TZ,
                randomize_captions=current["caption_mode"] == "Random",
                randomize_times=True,
                media_library=db.media_folder_map(),
            )
            batch_id = db.save_preview(rows, preview_batch_name)
            st.session_state["preview_rows"] = db.list_scheduled("preview")
            if int(current["posts_max"]) == 0:
                st.warning("Preview vidée: 0 post créé. Les posts déjà planifiés/envoyés sont conservés.")
            else:
                st.success(f"Planning généré : {len(rows)} posts dans le lot {batch_id}.")
        except Exception as e:
            st.error(str(e))

    all_scheduled = attach_threads_urls(db.list_scheduled())
    if all_scheduled:
        st.markdown("### Preview, planifiés & failed")
        render_status_counts(all_scheduled)
        render_visual_preview(all_scheduled, "preview")
        render_preview_media_tools(db.list_scheduled("preview"))
        render_account_delivery_panel(all_scheduled, "preview_account_control")
        batches = db.list_preview_batches()
        if batches:
            with st.expander("Lots de preview", expanded=False):
                st.caption("Restaurer remplace seulement la preview brouillon. Supprimer enlève uniquement les lignes locales en preview/ancienne preview, jamais les posts déjà planifiés sur Postoria.")
                batch_frame = pd.DataFrame(
                    [
                        {
                            "lot": batch["name"],
                            "statut": batch["status"],
                            "posts": int(batch.get("post_count") or 0),
                            "preview": int(batch.get("preview_count") or 0),
                            "ancienne_preview": int(batch.get("saved_count") or 0),
                            "planifiés_ou_envoyés": int(batch.get("sent_or_scheduled_count") or 0),
                            "début": batch.get("first_post") or "",
                            "fin": batch.get("last_post") or "",
                            "id": batch["id"],
                        }
                        for batch in batches
                    ]
                )
                st.dataframe(batch_frame, use_container_width=True, hide_index=True, height=min(360, 80 + len(batch_frame) * 36))
                for batch in batches[:8]:
                    batch_id = str(batch["id"])
                    cols = st.columns([2.2, .8, .8, .8])
                    with cols[0]:
                        new_name = st.text_input(
                            "Nom du lot",
                            value=str(batch.get("name") or batch_id),
                            key=f"preview_batch_name_{batch_id}",
                            label_visibility="collapsed",
                        )
                    with cols[1]:
                        if st.button("Renommer", key=f"rename_preview_batch_{batch_id}"):
                            db.update_preview_batch_name(batch_id, new_name)
                            st.rerun()
                    with cols[2]:
                        can_restore = int(batch.get("saved_count") or 0) > 0
                        if st.button("Restaurer", key=f"restore_preview_batch_{batch_id}", disabled=not can_restore):
                            restored = db.restore_preview_batch(batch_id)
                            st.success(f"{restored} posts restaurés en preview brouillon.")
                            st.rerun()
                    with cols[3]:
                        deletable = int(batch.get("preview_count") or 0) + int(batch.get("saved_count") or 0)
                        if st.button("Suppr. local", key=f"delete_preview_batch_{batch_id}", disabled=not deletable):
                            deleted = db.delete_preview_batch(batch_id)
                            st.warning(f"{deleted} posts preview supprimés localement. Postoria n'est pas modifié.")
                            st.rerun()
        category_counts = schedule_category_counts(all_scheduled)
        category_labels = [
            f"Preview à poster ({category_counts['Preview à poster']})",
            f"Preview déjà passée ({category_counts['Preview déjà passée']})",
            f"Anciennes previews ({category_counts['Anciennes previews']})",
            f"Planifiés à venir ({category_counts['Planifiés à venir']})",
            f"Déjà passés / à vérifier ({category_counts['Déjà passés / à vérifier']})",
            f"Failed ({category_counts['Failed']})",
            f"Tout ({category_counts['Tout']})",
        ]
        now_local = datetime.now(ZoneInfo(APP_TZ))
        st.caption(f"Heure utilisée pour classer la preview : {now_local.strftime('%Y-%m-%d %H:%M:%S')} ({APP_TZ}).")
        category_choice = st.radio(
            "Catégorie",
            category_labels,
            horizontal=True,
            help="La séparation à poster/déjà passée utilise l'heure locale de l'appareil/app. Déjà passée ne prouve pas que Threads a publié: vérifie le statut ou ouvre le compte.",
        )
        category = category_choice.split(" (", 1)[0]
        if category == "Preview à poster":
            category_rows = [
                row for row in all_scheduled
                if str(row.get("status")) == "preview" and not is_past_scheduled(row, now_local)
            ]
        elif category == "Preview déjà passée":
            category_rows = [
                row for row in all_scheduled
                if str(row.get("status")) == "preview" and is_past_scheduled(row, now_local)
            ]
        elif category == "Anciennes previews":
            category_rows = [row for row in all_scheduled if str(row.get("status")) == "preview_saved"]
        elif category == "Planifiés à venir":
            category_rows = [
                row for row in all_scheduled
                if str(row.get("status")) not in ("preview", "preview_saved")
                and not is_failed_status(row)
                and not is_past_scheduled(row, now_local)
            ]
        elif category == "Déjà passés / à vérifier":
            category_rows = [
                row for row in all_scheduled
                if str(row.get("status")) not in ("preview", "preview_saved")
                and not is_failed_status(row)
                and is_past_scheduled(row, now_local)
            ]
        elif category == "Failed":
            category_rows = [row for row in all_scheduled if is_failed_status(row)]
        else:
            category_rows = all_scheduled

        filter_col, list_col = st.columns([1, 3])
        all_df = scheduled_dataframe(category_rows)
        if all_df.empty:
            account_options = ["Tous les comptes"]
            group_options = ["Tous les groupes"]
            status_options = ["Tous"]
        else:
            account_options = ["Tous les comptes"] + sorted(all_df["account_name"].dropna().astype(str).unique().tolist())
            group_options = ["Tous les groupes"] + sorted(all_df["group_name"].fillna("Sans groupe").astype(str).unique().tolist())
            status_options = ["Tous"] + sorted(all_df["status"].dropna().astype(str).unique().tolist())
        with filter_col:
            st.markdown("#### Filtres")
            status_filter = choose_option("Statut", status_options, index=0)
            date_filter = st.radio("Date", ["Tout", "Aujourd'hui", "Semaine", "Mois"], horizontal=False)
            account_filter = choose_option("Compte", account_options, index=0)
            group_filter = choose_option("Groupe", group_options, index=0)
            view_mode = st.radio("Vue", ["Tout", "Par compte", "Par jour", "Par groupe", "Failed"], horizontal=False)
            sort_mode = choose_option("Tri", ["Heure", "Compte", "Jour", "Statut"], index=0)
        with list_col:
            query = st.text_input("Rechercher posts, comptes, erreurs", placeholder="Recherche...")
            filtered = filter_scheduled_rows(category_rows, status_filter, date_filter, account_filter, group_filter, query)
            if view_mode == "Failed" and not filtered.empty:
                filtered = filtered[filtered["status"].astype(str).str.contains("fail|error", case=False, regex=True)]
            if filtered.empty:
                sorted_filtered = filtered
            elif sort_mode == "Compte":
                sorted_filtered = filtered.sort_values(["account_name", "scheduled_time_local"])
            elif sort_mode == "Jour":
                sorted_filtered = filtered.sort_values(["day", "scheduled_time_local", "account_name"])
            elif sort_mode == "Statut":
                sorted_filtered = filtered.sort_values(["status", "scheduled_time_local"])
            else:
                sorted_filtered = filtered.sort_values(["scheduled_time_local", "account_name"])
            filtered = sorted_filtered

            st.caption(f"{len(filtered)} posts affichés sur {len(category_rows)} dans {category}. Total connu: {len(all_scheduled)}.")
            failed_rows = (
                filtered[filtered["status"].astype(str).str.contains("fail|error", case=False, regex=True)]
                if not filtered.empty
                else filtered
            )
            if not failed_rows.empty:
                st.error(f"{len(failed_rows)} posts failed/error. Question: corriger les posts, relancer ces comptes, ou supprimer ces programmations ?")

            visible_cols = ["day", "time", "time_state", "preview_batch", "account_name", "threads_url", "group_name", "status", "photos", "replies", "text", "error"]
            column_config = {
                "threads_url": st.column_config.LinkColumn("Threads", display_text="Ouvrir"),
            }
            if filtered.empty:
                st.info("Aucun post trouvé avec ces filtres.")
            elif view_mode == "Par compte":
                for account_name, chunk in filtered.groupby("account_name", sort=True):
                    with st.expander(f"{account_name} - {len(chunk)} posts", expanded=True):
                        st.dataframe(chunk[visible_cols], use_container_width=True, hide_index=True, column_config=column_config)
            elif view_mode == "Par jour":
                for day, chunk in filtered.groupby("day", sort=True):
                    with st.expander(f"{day} - {len(chunk)} posts", expanded=True):
                        st.dataframe(chunk[visible_cols], use_container_width=True, hide_index=True, column_config=column_config)
            elif view_mode == "Par groupe":
                for group_name, chunk in filtered.groupby("group_name", sort=True):
                    with st.expander(f"{group_name or 'Sans groupe'} - {len(chunk)} posts", expanded=True):
                        st.dataframe(chunk[visible_cols], use_container_width=True, hide_index=True, column_config=column_config)
            else:
                st.dataframe(filtered[visible_cols], use_container_width=True, hide_index=True, height=620, column_config=column_config)

        if calendar and not all_df.empty:
            with st.expander("Calendrier visuel"):
                events = [
                    {
                        "title": f"{r['account_name']} - {r['caption'][:30]}",
                        "start": r["scheduled_time_local"][:19].replace(" ", "T"),
                        "end": r["scheduled_time_local"][:19].replace(" ", "T"),
                    }
                    for r in all_scheduled
                ]
                calendar(events=events, options={"initialView": "timeGridDay", "height": 700})
    else:
        render_locked_step(
            "Aucune preview générée.",
            [
                "Sélectionne les groupes et comptes.",
                "Valide la cadence.",
                "Sélectionne les posts et photos.",
                "Clique sur Générer preview.",
            ],
        )

if active_page != "dashboard" and active_step == 4:
    st.subheader("Analytics de volume")
    section_intro(
        "Étape 5",
        "Contrôle les volumes par compte, groupe et période.",
        "Par défaut, les brouillons preview sont exclus pour ne pas mélanger planification test et historique réel.",
    )
    include_preview_analytics = st.checkbox(
        "Inclure les previews dans les analytics",
        value=False,
        help="Active seulement pour analyser un lot avant envoi. Les volumes réels excluent preview et anciennes previews.",
    )
    analytics_rows = db.list_scheduled()
    if not include_preview_analytics:
        analytics_rows = [
            row for row in analytics_rows
            if str(row.get("status")) not in ("preview", "preview_saved")
        ]
    render_analytics(analytics_rows)

if active_page != "dashboard" and active_step == 5:
    st.subheader("Envoi Postoria")
    section_intro(
        "Étape 6",
        "Dernier verrou avant action réelle.",
        "L'envoi reste bloqué tant qu'il manque comptes, posts, preview, API ou que dry-run est actif.",
    )
    send_local_rows = db.list_scheduled()
    clear_all_col, clear_hint_col = st.columns([1, 2])
    with clear_all_col:
        if st.button("Tout enlever de l'envoi", disabled=not send_local_rows, use_container_width=True):
            deleted = db.clear_all_scheduled_local()
            st.session_state.pop("preview_rows", None)
            st.warning(f"{deleted} lignes locales enlevées. Rien supprimé sur Postoria.")
            st.rerun()
    with clear_hint_col:
        st.caption("Visible ici, en haut: vide toute la liste locale Envoi/Preview/Analytics. Ne supprime jamais les posts sur app.postoria.io.")

    with st.expander("Workspace Postoria", expanded=not st.session_state.get("workspace_id")):
        workspace_id = render_workspace_picker(client, "send")
    preview = db.list_scheduled("preview")
    total_photos = sum(len(row.get("media_ids") or []) for row in preview)
    total_replies = sum(len(row.get("chain_replies") or []) for row in preview)
    preview_past = [row for row in preview if is_past_scheduled(row)]
    local_photo_missing_media = local_photo_without_media_id(preview)
    st.write(f"{len(preview)} posts en preview, {total_photos} media IDs attachés.")
    if preview_past:
        st.error(
            f"{len(preview_past)} posts de la preview ont déjà une heure passée. "
            "Regénère la preview avec une date/heure future avant envoi."
        )
    if local_photo_missing_media:
        st.error(
            f"{len(local_photo_missing_media)} posts ont une photo locale sans media ID Postoria. "
            "Ajoute le media ID ou retire la photo avant l'envoi."
        )
    if preview:
        render_account_delivery_panel(attach_threads_urls(preview), "send_account_control", "Contrôle avant envoi")
    clear_col, keep_col = st.columns([1, 2])
    with clear_col:
        if st.button("Tout enlever de la preview", disabled=not preview, use_container_width=True):
            db.clear_preview()
            st.session_state.pop("preview_rows", None)
            st.warning("Preview vidée. Rien supprimé sur Postoria.")
            st.rerun()
    with keep_col:
        st.caption("Action locale: enlève seulement les posts en preview brouillon. Posts déjà programmés/envoyés restent conservés.")
    if total_replies:
        st.warning(f"{total_replies} replies en thread chain sont en preview. Envoi Postoria actuel publie seulement le post principal.")

    c1, c2, c3 = st.columns(3)
    ok_accounts = c1.checkbox("Comptes vérifiés")
    ok_posts = c2.checkbox("Posts/photos vérifiés")
    ok_times = c3.checkbox("Horaires vérifiés")
    confirm_text = st.text_input("Tape DEMARRER pour débloquer l'envoi")
    send_blockers = []
    if not st.session_state.get("selected_accounts"):
        send_blockers.append("pas de comptes")
    if not st.session_state.get("selected_posts"):
        send_blockers.append("pas de posts")
    if not preview:
        send_blockers.append("pas de preview")
    if preview_past:
        send_blockers.append(f"{len(preview_past)} horaires déjà passés")
    if local_photo_missing_media:
        send_blockers.append(f"{len(local_photo_missing_media)} photos locales sans media ID Postoria")
    if not api_exists or not client:
        send_blockers.append("API manquante")
    if dry_run:
        send_blockers.append("dry-run activé")
    if not workspace_id:
        send_blockers.append("workspace Postoria non choisi")
    if not (ok_accounts and ok_posts and ok_times):
        send_blockers.append("confirmations non cochées")
    if confirm_text.strip().upper() != "DEMARRER":
        send_blockers.append("confirmation DEMARRER manquante")
    can_send = not send_blockers

    render_blocker_chips(send_blockers)
    if st.button("Programmer via Postoria", disabled=not can_send or dry_run):
        if not client or not workspace_id:
            st.error("Client Postoria ou workspace manquant.")
        else:
            sent_count = 0
            failed_count = 0
            error_rows = []
            now_utc = datetime.now(ZoneInfo("UTC"))
            progress = st.progress(0, text="Envoi Postoria en cours...")
            for index, row in enumerate(preview, start=1):
                account_id = int(row["account_id"])
                try:
                    scheduled_utc = parse_utc_scheduled(row.get("scheduled_time_utc"))
                    if scheduled_utc is None:
                        raise RuntimeError(f"Heure UTC invalide: {row.get('scheduled_time_utc')}")
                    if scheduled_utc <= now_utc + timedelta(seconds=30):
                        raise RuntimeError(
                            f"Heure déjà passée ou trop proche ({row.get('scheduled_time_utc')}). "
                            "Regénère une preview future."
                        )
                    res = client.create_post(
                        int(workspace_id),
                        account_id,
                        row["caption"],
                        row["scheduled_time_utc"],
                        row.get("media_ids") or [],
                    )
                    postoria_id = postoria_response_id(res)
                    postoria_status = postoria_response_status(res)
                    if postoria_id is None:
                        raise RuntimeError(f"Réponse Postoria sans post id: {short_debug(res)}")
                    db.update_scheduled_result(row["id"], postoria_id, postoria_status, None)
                    sent_count += 1
                except Exception as e:
                    failed_count += 1
                    error = short_debug(e)
                    db.update_scheduled_result(row["id"], None, "failed", error)
                    error_rows.append({
                        "id": row["id"],
                        "compte": row["account_name"],
                        "heure": row["scheduled_time_local"],
                        "erreur": error,
                    })
                progress.progress(index / len(preview), text=f"Envoi Postoria {index}/{len(preview)}")
            progress.empty()
            remaining_preview = len(db.list_scheduled("preview"))
            if sent_count or failed_count:
                st.session_state["active_step"] = 6
            if failed_count:
                st.error(f"Envoi terminé: {sent_count} envoyés, {failed_count} failed, {remaining_preview} encore en preview. Ouvre l'onglet Suivi pour contrôler.")
                st.dataframe(pd.DataFrame(error_rows), use_container_width=True, hide_index=True)
            else:
                st.success(f"Envoi complet: {sent_count} posts programmés sur Postoria, {remaining_preview} post restant en preview. Ouvre l'onglet Suivi pour contrôler.")

    if st.button("Vérifier statuts Postoria"):
        if not client or not workspace_id:
            st.error("Client Postoria ou workspace manquant.")
        else:
            checked, errors = refresh_postoria_statuses(client, workspace_id)
            st.success(f"Statuts mis à jour pour {checked} posts Postoria. Erreurs: {errors}.")

    st.caption("Aucune suppression Postoria n'est disponible dans cette app. Les posts envoyés restent conservés côté Postoria.")

    scheduled = attach_threads_urls(db.list_scheduled())
    if scheduled:
        st.dataframe(
            pd.DataFrame(scheduled),
            use_container_width=True,
            hide_index=True,
            column_config={"threads_url": st.column_config.LinkColumn("Threads", display_text="Ouvrir")},
        )

if active_page != "dashboard" and active_step == 6:
    st.subheader("Suivi après envoi")
    section_intro(
        "Étape 7",
        "Vérifie ce que Postoria a accepté, ce qui a échoué, et ce qui doit être contrôlé sur Threads.",
        "Cet onglet ne programme rien. Il lit les statuts enregistrés localement et peut rafraîchir les posts déjà créés sur Postoria.",
    )
    with st.expander("Workspace Postoria", expanded=not st.session_state.get("workspace_id")):
        follow_workspace_id = render_workspace_picker(client, "follow")

    follow_rows = attach_threads_urls(db.list_scheduled())
    if not follow_rows:
        render_locked_step(
            "Aucun post à suivre.",
            ["Génère une preview, puis envoie les posts à Postoria pour créer un historique de suivi."],
        )
    else:
        refresh_col, note_col = st.columns([1, 2])
        with refresh_col:
            if st.button("Rafraîchir statuts Postoria", disabled=not client or not follow_workspace_id, use_container_width=True):
                checked, errors = refresh_postoria_statuses(client, follow_workspace_id)
                st.success(f"{checked} statuts mis à jour. Erreurs: {errors}.")
                st.rerun()
        with note_col:
            st.caption("Le refresh interroge seulement les posts qui ont déjà un ID Postoria. Les previews locales ne sont pas envoyées.")

        follow_rows = attach_threads_urls(db.list_scheduled())
        retry_candidates = retryable_failed_posts(follow_rows)
        retry_a, retry_b = st.columns([1, 2])
        with retry_a:
            retry_disabled = not retry_candidates or not client or not follow_workspace_id or dry_run
            if st.button(
                f"Retenter failed sans ID ({len(retry_candidates)})",
                disabled=retry_disabled,
                type="primary",
                use_container_width=True,
            ):
                progress = st.progress(0, text="Retry failed Postoria en cours...")
                sent_count = 0
                failed_count = 0
                error_rows: list[dict] = []
                for index, row in enumerate(retry_candidates, start=1):
                    sent, failed, errors = retry_failed_posts_direct(client, follow_workspace_id, [row])
                    sent_count += sent
                    failed_count += failed
                    error_rows.extend(errors)
                    progress.progress(index / len(retry_candidates), text=f"Retry failed {index}/{len(retry_candidates)}")
                progress.empty()
                if failed_count:
                    st.error(f"Retry terminé: {sent_count} acceptés Postoria, {failed_count} encore failed.")
                    st.dataframe(pd.DataFrame(error_rows), use_container_width=True, hide_index=True)
                else:
                    st.success(f"Retry terminé: {sent_count} failed relancés et acceptés par Postoria.")
        with retry_b:
            retry_blockers = []
            if not retry_candidates:
                retry_blockers.append("aucun failed sans ID Postoria à relancer")
            if not client:
                retry_blockers.append("clé API Postoria manquante")
            if not follow_workspace_id:
                retry_blockers.append("workspace Postoria non choisi")
            if dry_run:
                retry_blockers.append("dry-run actif")
            if retry_blockers:
                st.warning("Retry indisponible : " + " ; ".join(retry_blockers) + ".")
            else:
                st.caption(
                    "Le retry reprend uniquement les failed sans postoria_post_id. "
                    "Même compte, même texte, mêmes médias, même scheduled_time. Les posts déjà acceptés ne sont jamais renvoyés."
                )

        if retry_candidates:
            retry_reasons = pd.DataFrame(
                [
                    {
                        "Compte": row.get("account_name") or "Compte",
                        "Horaire initial": row.get("scheduled_time_local") or "",
                        "Dernière erreur": row.get("error") or "Aucun détail enregistré",
                    }
                    for row in retry_candidates
                ]
            )
            with st.expander(f"Pourquoi {len(retry_candidates)} post(s) sont à relancer", expanded=False):
                st.caption("Ces erreurs viennent de la dernière tentative. Le retry conserve les mêmes données et le même horaire initial.")
                st.dataframe(retry_reasons, use_container_width=True, hide_index=True)

        follow_rows = attach_threads_urls(db.list_scheduled())
        render_status_counts(follow_rows)
        render_account_delivery_panel(follow_rows, "follow_account_control", "Vue par compte")

        follow_df = scheduled_dataframe(follow_rows)
        if follow_df.empty:
            st.info("Aucune ligne lisible pour le suivi.")
        else:
            f1, f2, f3 = st.columns([1, 1, 1])
            with f1:
                follow_status = st.radio(
                    "Statut",
                    ["Tous", "Acceptés Postoria", "Failed", "À vérifier", "Preview locale"],
                    horizontal=False,
                    key="follow_status_filter",
                )
            with f2:
                follow_group = choose_option(
                    "Groupe",
                    ["Tous les groupes"] + sorted(follow_df["group_name"].fillna("Sans groupe").astype(str).unique().tolist()),
                    key="follow_group_filter",
                )
            with f3:
                follow_account = choose_option(
                    "Compte",
                    ["Tous les comptes"] + sorted(follow_df["account_name"].fillna("Compte inconnu").astype(str).unique().tolist()),
                    key="follow_account_filter",
                )

            visible = follow_df.copy()
            if follow_status == "Acceptés Postoria":
                visible = visible[
                    ~visible["status"].astype(str).isin(["preview", "preview_saved"])
                    & ~visible["status"].astype(str).str.contains("fail|error", case=False, regex=True)
                ]
            elif follow_status == "Failed":
                visible = visible[visible["status"].astype(str).str.contains("fail|error", case=False, regex=True)]
            elif follow_status == "À vérifier":
                visible = visible[
                    visible["time_state"].astype(str).str.contains("passé|vérifier", case=False, regex=True)
                    & ~visible["status"].astype(str).eq("preview")
                ]
            elif follow_status == "Preview locale":
                visible = visible[visible["status"].astype(str).isin(["preview", "preview_saved"])]
            if follow_group != "Tous les groupes":
                visible = visible[visible["group_name"].fillna("Sans groupe").astype(str) == follow_group]
            if follow_account != "Tous les comptes":
                visible = visible[visible["account_name"].fillna("Compte inconnu").astype(str) == follow_account]

            visible = visible.sort_values(["scheduled_time_local", "account_name"])
            st.caption(f"{len(visible)} posts dans cette vue.")
            st.dataframe(
                visible[[
                    "day", "time", "time_state", "account_name", "threads_url",
                    "group_name", "status", "photos", "replies", "text", "error",
                ]],
                use_container_width=True,
                hide_index=True,
                height=680,
                column_config={"threads_url": st.column_config.LinkColumn("Threads", display_text="Ouvrir")},
            )
