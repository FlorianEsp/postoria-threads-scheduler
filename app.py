from __future__ import annotations

import os
from collections import defaultdict
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

# streamlit-calendar crashes this local Python/Streamlit runtime with exit 139.
# Keep the scheduler stable and use the richer table preview instead.
calendar = None

load_dotenv()
db.init_db()

APP_TZ = os.getenv("APP_TIMEZONE", "Europe/Brussels")


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
        db.update_account_preferences(int(account["id"]), group_name, bool(row["active"]))
        if not bool(row["use"]) or not bool(row["active"]):
            continue
        grouped.setdefault(group_name, {"accounts": []})
        grouped[group_name]["accounts"].append({**account, "group_name": group_name})
    return grouped


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
    df.loc[:, "day"] = df["scheduled_time_local"].astype(str).str.slice(0, 10)
    df.loc[:, "time"] = df["scheduled_time_local"].astype(str).str.slice(11, 16)
    df.loc[:, "group_name"] = df["group_name"].fillna("Sans groupe")
    df.loc[:, "photos"] = df["media_ids"].apply(lambda value: len(value or []))
    df.loc[:, "text"] = df["caption"].astype(str).str.slice(0, 140)
    df.loc[:, "error"] = df.get("error", "").fillna("") if "error" in df.columns else ""
    df.loc[:, "replies"] = df.get("chain_replies", []).apply(lambda value: len(value or [])) if "chain_replies" in df.columns else 0
    df.loc[:, "threads_url"] = df.get("threads_url", "").fillna("") if "threads_url" in df.columns else ""
    df.loc[:, "preview_batch"] = df.get("preview_batch_id", "").fillna("") if "preview_batch_id" in df.columns else ""
    return df


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


def is_failed_status(row: dict) -> bool:
    return any(token in str(row.get("status", "")).lower() for token in ("fail", "error"))


def schedule_category_counts(rows: list[dict]) -> dict[str, int]:
    preview_count = sum(1 for row in rows if str(row.get("status")) == "preview")
    saved_preview_count = sum(1 for row in rows if str(row.get("status")) == "preview_saved")
    failed_count = sum(1 for row in rows if is_failed_status(row))
    planned_count = sum(
        1
        for row in rows
        if str(row.get("status")) not in ("preview", "preview_saved") and not is_failed_status(row)
    )
    return {
        "Preview brouillon": preview_count,
        "Anciennes previews": saved_preview_count,
        "Déjà planifiés": planned_count,
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


def max_posts_for_window(start: time, end: time, min_gap: int) -> int:
    minutes = window_minutes(start, end)
    if minutes <= 0:
        return 0
    return floor(minutes / max(1, min_gap)) + 1


def distribution_sentence(current: dict) -> str:
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


def h(value) -> str:
    return escape(str(value or ""), quote=True)


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
) -> None:
    steps = [
        ("1", "Comptes", accounts_ready),
        ("2", "Cadence", cadence_ready),
        ("3", "Posts/photos", posts_ready),
        ("4", "Preview", preview_ready),
        ("5", "Analytics", analytics_ready),
        ("6", "Envoi", send_ready),
    ]
    items = []
    for number, label, ready in steps:
        items.append(
            f"<div class='flow-step {'is-done' if ready else 'is-pending'}'>"
            f"<span>{number}</span>"
            f"<strong>{h(label)}</strong>"
            f"<small>{'OK' if ready else 'À faire'}</small>"
            "</div>"
        )
    st.markdown("<div class='flow-rail'>" + "".join(items) + "</div>", unsafe_allow_html=True)


def render_app_header(api_exists: bool, dry_run: bool, app_tz: str) -> None:
    api_state = "API détectée" if api_exists else "API manquante"
    run_state = "Dry-run actif" if dry_run else "Envoi réel armé"
    st.markdown(
        "<div class='app-hero'>"
        "<div>"
        "<span class='eyebrow'>Bulk Threads</span>"
        "<h1>Scheduler de publication</h1>"
        "<p>Un parcours en 5 étapes pour choisir les comptes, calculer la cadence, sélectionner les contenus, vérifier la preview, puis envoyer.</p>"
        "</div>"
        "<div class='hero-status'>"
        f"<span class='status-pill {'ok' if api_exists else 'warn'}'>{h(api_state)}</span>"
        f"<span class='status-pill {'warn' if dry_run else 'ok'}'>{h(run_state)}</span>"
        f"<span class='status-pill neutral'>{h(app_tz)}</span>"
        "</div>"
        "</div>",
        unsafe_allow_html=True,
    )


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
    if clear_accounts:
        st.session_state["selected_accounts"] = []
        st.session_state["grouped_accounts"] = {}
        st.session_state["selected_group_filters"] = []
        st.session_state.pop("_account_group_signature", None)
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
    name = st.text_input("Nom du groupe", placeholder="ex: w-u, tous, group 5 post", key=f"{form_key}_name")
    color_labels = [label for label, _, _ in GROUP_COLOR_CHOICES]
    color_label = st.radio("Couleur", color_labels, horizontal=True, key=f"{form_key}_color_label")
    color = next(dot for label, dot, _ in GROUP_COLOR_CHOICES if label == color_label)
    st.markdown(
        "<div class='group-color-preview'>"
        + "".join(
            f"<span style='background:{dot}; opacity:{'1' if dot == color else '.28'}'></span>"
            for _, dot, _ in GROUP_COLOR_CHOICES
        )
        + "</div>",
        unsafe_allow_html=True,
    )
    if st.button("Créer le groupe", disabled=not name.strip(), key=f"{form_key}_save"):
        created = db.upsert_group(name, color=color)
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
    .stTabs [data-baseweb="tab-list"] {
        gap: 8px;
        border-bottom: 1px solid var(--line);
    }
    .stTabs [data-baseweb="tab"] {
        border-radius: 8px 8px 0 0;
        padding: 12px 14px;
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
        position: relative;
        border: 1px solid var(--line);
        border-radius: 8px;
        padding: 13px 14px 12px;
        min-height: 82px;
        background: var(--panel);
        overflow: hidden;
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
        grid-template-columns: 44px 2.2fr 1.1fr 1fr .9fr 1fr;
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
    @media (max-width: 900px) {
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

with st.sidebar:
    st.header("Configuration")
    api_exists = bool(os.getenv("POSTORIA_API_KEY"))
    st.write("Clé API .env :", "détectée" if api_exists else "manquante")
    dry_run = st.toggle("Mode dry-run", value=True)
    st.write("Timezone :", APP_TZ)
    st.divider()
    st.write("Flux conseillé")
    st.write("1. Choisir groupes/comptes")
    st.write("2. Définir la cadence")
    st.write("3. Charger textes + médias")
    st.write("4. Vérifier la preview")
    st.write("5. Lire les analytics")
    st.write("6. Confirmer l'envoi")
    st.divider()
    st.write("Recommencer")
    if st.button("Recommencer planning"):
        st.session_state["reset_dialog_mode"] = "planning"
    if st.button("Recommencer tout"):
        st.session_state["reset_dialog_mode"] = "all"

client = None
if api_exists:
    try:
        client = PostoriaClient()
    except Exception as e:
        st.error(str(e))

stored_accounts = db.list_accounts()
posts = db.list_posts(active_only=False)
preview = db.list_scheduled("preview")
selected_accounts_count = len(st.session_state.get("selected_accounts", []))
selected_posts_count = len(st.session_state.get("selected_posts", []))
current = settings()
capacity_now = max_posts_for_window(current["start_time"], current["end_time"], int(current["min_interval"]))
accounts_ready = selected_accounts_count > 0
cadence_ready = accounts_ready and int(current["posts_max"]) <= capacity_now
posts_ready = selected_posts_count > 0
preview_ready = len(preview) > 0
analytics_ready = len(db.list_scheduled()) > 0
send_ready = preview_ready and api_exists and not dry_run

render_app_header(api_exists, dry_run, APP_TZ)
if st.session_state.get("reset_dialog_mode"):
    render_reset_dialog(str(st.session_state["reset_dialog_mode"]))
if st.session_state.get("preview_cleared_notice"):
    st.info(st.session_state.pop("preview_cleared_notice"))
render_metric_strip(
    [
        ("Comptes prêts", str(selected_accounts_count), "sélection depuis groupes"),
        ("Textes prêts", str(selected_posts_count), "rotation possible"),
        ("Preview", str(len(preview)), "posts à vérifier"),
        ("Analytics", str(len(db.list_scheduled())), "posts analysés"),
    ]
)

render_flow_status(accounts_ready, cadence_ready, posts_ready, preview_ready, analytics_ready, send_ready)

tabs = st.tabs(["1. Comptes", "2. Cadence", "3. Posts & Photos", "4. Preview", "5. Analytics", "6. Envoi"])

with tabs[0]:
    st.subheader("Comptes et groupes")
    section_intro(
        "Étape 1",
        "Crée tes groupes, assigne les comptes, puis sélectionne les groupes à inclure.",
        "Choisir un groupe sélectionne tous ses comptes. Tu peux ensuite décocher certains comptes dans le tableau.",
    )
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
                        db.upsert_accounts(threads_accounts)
                        st.session_state["threads_accounts"] = db.list_accounts()
                        st.success(f"{len(threads_accounts)} comptes Threads trouvés.")
                    except Exception as e:
                        st.error(str(e))

    accounts = st.session_state.get("threads_accounts") or db.list_accounts()
    if not accounts:
        st.info("Aucun compte local. Charge les comptes Postoria d'abord.")
    else:
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

        st.caption("Action principale: prends un groupe ou tous les comptes actifs. Ensuite ajuste seulement les exceptions dans la liste.")
        preset_a, preset_c = st.columns([1, 1])
        if preset_a.button("Tous les comptes actifs"):
            st.session_state["selected_group_filters"] = group_options
            st.session_state.pop("_account_group_signature", None)
            for account in accounts:
                account_id = int(account["id"])
                st.session_state[f"account_use_{account_id}"] = bool(account.get("active_for_day", 1))
            st.rerun()
        if preset_c.button("Créer un groupe rapide"):
            st.session_state["show_group_dialog"] = True
            st.rerun()
        if st.session_state.get("show_group_dialog"):
            render_create_group_dialog()

        selected_group_filters = st.session_state.get("selected_group_filters", [])
        group_signature = tuple(selected_group_filters)
        if st.session_state.get("_account_group_signature") != group_signature:
            for account in accounts:
                account_id = int(account["id"])
                group_name = account.get("group_name") or "tous"
                st.session_state[f"account_use_{account_id}"] = bool(account.get("active_for_day", 1)) and group_name in selected_group_filters
            st.session_state["_account_group_signature"] = group_signature

        group_cols = st.columns(min(3, max(1, len(groups))))
        for idx, group in enumerate(groups):
            group_name = group["name"]
            group_accounts = group_accounts_by_name.get(group_name, [])
            selected_in_group = sum(
                1 for account in group_accounts
                if st.session_state.get(f"account_use_{int(account['id'])}", False)
            )
            with group_cols[idx % len(group_cols)]:
                group_is_selected = group_name in st.session_state.get("selected_group_filters", [])
                st.markdown(
                    "<div class='step-note'>"
                    f"{render_group_badge(group_name, group.get('color'))}<br>"
                    f"<b>{selected_in_group}/{len(group_accounts)}</b> comptes sélectionnés"
                    "</div>",
                    unsafe_allow_html=True,
                )
                group_key = f"{idx}_{widget_slug(group_name)}"
                action_label = "Enlever ce groupe" if group_is_selected else "Utiliser ce groupe"
                if st.button(action_label, key=f"toggle_group_{group_key}", disabled=not group_accounts):
                    selected = set(st.session_state.get("selected_group_filters", []))
                    if group_is_selected:
                        selected.discard(group_name)
                    else:
                        selected.add(group_name)
                    st.session_state["selected_group_filters"] = [name for name in group_options if name in selected]
                    st.session_state.pop("_account_group_signature", None)
                    st.rerun()

        with st.expander("Sélection avancée par nom de groupe"):
            st.caption("Coche uniquement les groupes à inclure. Aucun champ texte ici.")
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
                st.rerun()

        selected_group_filters = st.session_state.get("selected_group_filters", [])
        render_group_cards(groups, st.session_state.get("grouped_accounts", {}))
        if selected_group_filters:
            selected_labels = []
            for group_name in selected_group_filters:
                group_accounts = group_accounts_by_name.get(group_name, [])
                selected_labels.append(f"{group_name} ({len(group_accounts)})")
            st.caption("Groupes retenus : " + ", ".join(selected_labels))
        else:
            st.info("Prends un groupe ou choisis tous les comptes actifs pour commencer.")

        st.markdown("#### Ajuster seulement les exceptions")
        st.caption("Décoche un compte, change son groupe ou mets-le en pause seulement si nécessaire.")

        for account in accounts:
            account_id = int(account["id"])
            st.session_state.setdefault(f"account_group_{account_id}", account.get("group_name") or "tous")
            st.session_state.setdefault(f"account_active_{account_id}", bool(account.get("active_for_day", 1)))
            st.session_state.setdefault(
                f"account_status_{account_id}",
                "Active" if bool(st.session_state.get(f"account_active_{account_id}", True)) else "Paused",
            )
            st.session_state.setdefault(f"account_use_{account_id}", False)

        top_a, top_b, top_c = st.columns([1.15, 1.15, .65])
        with top_a:
            account_query = st.text_input("Search accounts", placeholder="Search accounts...", label_visibility="collapsed")
        with top_b:
            status_filter = st.radio(
                "Statut comptes",
                ["All", "Active", "Paused", "Rate limited"],
                horizontal=True,
                label_visibility="collapsed",
            )
        visible_accounts = []
        query = account_query.strip().lower()
        for account in accounts:
            account_id = int(account["id"])
            group_name = st.session_state.get(f"account_group_{account_id}", account.get("group_name") or "tous")
            active = bool(st.session_state.get(f"account_active_{account_id}", bool(account.get("active_for_day", 1))))
            enriched = {**account, "group_name": group_name, "active_for_day": int(active)}
            status = account_status_label(enriched)
            label_text = f"{account.get('name','')} {account.get('username','')} {group_name}".lower()
            if query and query not in label_text:
                continue
            if status_filter != "All" and status != status_filter:
                continue
            visible_accounts.append(enriched)
        with top_c:
            st.markdown(f"<div class='accounts-count'>{len(visible_accounts)} of {len(accounts)}</div>", unsafe_allow_html=True)

        selected_total = sum(1 for account in accounts if st.session_state.get(f"account_use_{int(account['id'])}", False))
        selected_visible = sum(1 for account in visible_accounts if st.session_state.get(f"account_use_{int(account['id'])}", False))
        paused_visible = sum(1 for account in visible_accounts if not st.session_state.get(f"account_active_{int(account['id'])}", True))
        st.markdown(
            "<div class='account-selection-summary'>"
            f"<div><span>Sélection totale</span><strong>{selected_total}</strong></div>"
            f"<div><span>Dans la vue</span><strong>{selected_visible}/{len(visible_accounts)}</strong></div>"
            f"<div><span>Paused visibles</span><strong>{paused_visible}</strong></div>"
            "</div>",
            unsafe_allow_html=True,
        )

        selected_accounts_preview = [
            account for account in accounts
            if st.session_state.get(f"account_use_{int(account['id'])}", False)
        ]
        with st.container(border=True):
            header_left, header_right = st.columns([2, 1])
            with header_left:
                st.markdown(f"**Comptes sélectionnés · {len(selected_accounts_preview)}**")
            with header_right:
                if st.button("Vider sélection", disabled=not selected_accounts_preview):
                    for account in selected_accounts_preview:
                        st.session_state[f"account_use_{int(account['id'])}"] = False
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
                st.session_state["account_step_done"] = True
                st.success("Comptes validés. Ouvre l'onglet 2. Cadence pour régler le volume et les horaires.")

        st.markdown(
            "<div class='accounts-shell-lite'>"
            "<div class='account-table-head'>"
            "<span></span><span>Username</span><span>Group</span><span>Next post</span><span>Status</span><span>Actions</span>"
            "</div>"
            "</div>",
            unsafe_allow_html=True,
        )

        next_by_account = next_post_map(db.list_scheduled())
        if not visible_accounts:
            render_locked_step(
                "Aucun compte trouvé avec ces filtres.",
                ["Change la recherche, le filtre de statut, ou sélectionne un autre groupe."],
            )

        rows = []
        for row_index, account in enumerate(visible_accounts):
            account_id = int(account["id"])
            if row_index:
                st.markdown("<div class='account-row-divider'></div>", unsafe_allow_html=True)
            row_cols = st.columns([.38, 2.2, 1.15, .9, .95, .9])
            with row_cols[0]:
                use_account = st.checkbox("Utiliser", key=f"account_use_{account_id}", label_visibility="collapsed")
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
                group_index = 0
                current_group = st.session_state.get(f"account_group_{account_id}", account.get("group_name") or "tous")
                if current_group in group_options:
                    group_index = group_options.index(current_group)
                selected_group = choose_option(
                    f"Groupe {account_id}",
                    group_options,
                    index=group_index,
                    key=f"account_group_{account_id}",
                    horizontal=len(group_options) <= 3,
                    label_visibility="collapsed",
                )
                st.markdown(
                    render_group_badge(selected_group, group_color_by_name.get(selected_group)),
                    unsafe_allow_html=True,
                )
            with row_cols[3]:
                st.markdown(
                    f"<span class='next-post-pill'>{h(next_by_account.get(account_id, '-'))}</span>",
                    unsafe_allow_html=True,
                )
            with row_cols[4]:
                active_before = bool(st.session_state.get(f"account_active_{account_id}", bool(account.get("active_for_day", 1))))
                status_choice = choose_option(
                    f"Statut {account_id}",
                    ["Active", "Paused"],
                    index=0 if active_before else 1,
                    key=f"account_status_{account_id}",
                    horizontal=True,
                    label_visibility="collapsed",
                )
                active_account = status_choice == "Active"
                st.session_state[f"account_active_{account_id}"] = active_account
                status_text = account_status_label({**account, "group_name": selected_group, "active_for_day": int(active_account)})
                st.markdown(f"<span class='status-text'>{h(status_text)}</span>", unsafe_allow_html=True)
            with row_cols[5]:
                account_url = account_threads_url(account) or account.get("url")
                action_link = (
                    f"<a href='{h(account_url)}' target='_blank' rel='noreferrer'>Threads</a>"
                    if account_url
                    else "<span>Aucun lien</span>"
                )
                st.markdown(f"<div class='account-actions'>{action_link}</div>", unsafe_allow_html=True)
            rows.append(
                {
                    "use": bool(use_account),
                    "id": account_id,
                    "compte": account_label(account),
                    "group": selected_group,
                    "active": bool(active_account),
                    "url": account.get("url", ""),
                }
            )

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
                    "active": bool(st.session_state.get(f"account_active_{account_id}", bool(account.get("active_for_day", 1)))),
                    "url": account.get("url", ""),
                }
            )
        edited_accounts = pd.DataFrame(rows)
        grouped = build_grouped_accounts(accounts, edited_accounts)
        selected_accounts = [account for group in grouped.values() for account in group["accounts"]]
        st.session_state["grouped_accounts"] = grouped
        st.session_state["selected_accounts"] = selected_accounts
        if selected_accounts:
            st.success(f"{len(selected_accounts)} comptes prêts dans {len(grouped)} groupes.")
        else:
            st.warning("Aucun compte sélectionné. Les étapes suivantes restent bloquées.")
        render_group_summary(grouped)

with tabs[1]:
    st.subheader("Cadence de publication")
    section_intro(
        "Étape 2",
        "Décide combien chaque compte doit publier et dans quelle fenêtre.",
        "Ces réglages ne publient rien. Ils servent seulement à construire une preview vérifiable.",
    )
    if not st.session_state.get("selected_accounts"):
        render_locked_step(
            "Étape bloquée: choisis d'abord les comptes.",
            ["Va dans 1. Comptes, sélectionne un ou plusieurs groupes, puis ajuste les comptes si besoin."],
        )
    else:
        current = settings()
        q1, q2, q3 = st.columns(3)
        with q1:
            publish_date = st.date_input("Date", value=current["publish_date"])
            caption_mode = st.radio("Ordre des textes", ["Rotate", "Random"], horizontal=True, index=0 if current["caption_mode"] == "Rotate" else 1)
        with q2:
            start_time = st.time_input("Début", value=current["start_time"])
            end_time = st.time_input("Fin", value=current["end_time"])
        with q3:
            count_mode = st.radio("Posts par compte", ["Exact", "Range"], horizontal=True, index=0 if current["count_mode"] == "Exact" else 1)
            if count_mode == "Exact":
                posts_min = st.number_input("Nombre exact", min_value=1, max_value=50, value=int(current["posts_min"]), step=1)
                posts_max = posts_min
            else:
                posts_min = st.number_input("Min", min_value=1, max_value=50, value=int(current["posts_min"]), step=1)
                posts_max = st.number_input("Max", min_value=int(posts_min), max_value=50, value=max(int(current["posts_max"]), int(posts_min)), step=1)

        q4, q5 = st.columns(2)
        with q4:
            min_interval = st.number_input("Écart min entre 2 posts du même compte (min)", min_value=1, max_value=1440, value=int(current["min_interval"]), step=5)
        with q5:
            max_possible = max_posts_for_window(start_time, end_time, int(min_interval))
            selected_count = len(st.session_state.get("selected_accounts", []))
            total_min, total_max = planned_total_range(selected_count, int(posts_min), int(posts_max))
            st.metric("Comptes sélectionnés", selected_count)
            st.caption(planned_total_sentence(selected_count, int(posts_min), int(posts_max), max_possible))
            st.caption(f"Total à créer : {total_min if total_min == total_max else f'{total_min}-{total_max}'} publications.")
            st.caption(f"Capacité horaire calculée : {max_possible} posts max par compte.")
            if int(posts_max) > max_possible:
                st.error(
                    f"Question à trancher : avec {start_time.strftime('%H:%M')}-{end_time.strftime('%H:%M')} "
                    f"et {int(min_interval)}min d'écart, chaque compte peut recevoir {max_possible} posts max. "
                    "Réduis posts par compte, baisse l'écart, ou élargis la plage."
                )

        with st.expander("Option anti-doublon texte entre comptes"):
            avoid_same_text = st.checkbox(
                "Éviter le même texte sur deux comptes trop proches",
                value=bool(current.get("avoid_same_text", False)),
            )
            same_text_gap = st.number_input(
                "Écart min pour réutiliser le même texte sur un autre compte (min)",
                min_value=1,
                max_value=1440,
                value=int(current.get("same_text_gap", 60)),
                step=5,
                disabled=not avoid_same_text,
            )

        st.session_state["settings"] = {
            "publish_date": publish_date,
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
        st.info(distribution_sentence(settings()))

with tabs[2]:
    st.subheader("Posts & Photos")
    section_intro(
        "Étape 3",
        "Ajoute puis sélectionne les textes, les variables, les dossiers média et les replies en chaîne.",
        "Les médias utilisent des media IDs déjà disponibles. Les replies sont préparées en preview.",
    )
    current = settings()
    selected_count = len(st.session_state.get("selected_accounts", []))
    capacity = max_posts_for_window(current["start_time"], current["end_time"], int(current["min_interval"]))
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
    else:
        st.info(
            f"{selected_count} comptes sélectionnés. Besoin planning: "
            f"{posts_min_required if posts_min_required == posts_max_required else f'{posts_min_required}-{posts_max_required}'} publications. "
            "Si tu sélectionnes moins de textes que nécessaire, ils tourneront en rotation."
        )
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

        st.markdown("#### Posts")
        import_col, manual_col = st.columns(2)
        with import_col:
            uploaded = st.file_uploader(
                "Importer CSV de contenus",
                type=["csv"],
                help="Colonnes: text, media_ids, media_folder, reply_1, reply_2. Autres colonnes = variables {colonne}.",
            )
            if uploaded:
                frame = pd.read_csv(uploaded)
                records = make_post_records(frame)
                added, skipped, imported_ids = db.add_posts_with_ids(records)
                imported_posts = [
                    post for post in db.list_posts(active_only=False)
                    if int(post["id"]) in set(imported_ids)
                ]
                st.session_state["selected_posts"] = imported_posts
                st.session_state["_selected_posts_signature"] = tuple(sorted(imported_ids))
                st.session_state["posts_editor_version"] = st.session_state.get("posts_editor_version", 0) + 1
                if imported_ids:
                    if clear_preview_draft("Preview brouillon supprimée: nouveaux posts importés. Les posts déjà planifiés restent conservés."):
                        st.info("Ancienne preview supprimée. Les posts déjà planifiés restent conservés.")
                st.success(f"{added} posts ajoutés, {skipped} doublons réutilisés. Lot actif: {len(imported_posts)} posts du CSV.")
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
                    st.session_state["_selected_posts_signature"] = tuple(sorted(imported_ids))
                    st.session_state["posts_editor_version"] = st.session_state.get("posts_editor_version", 0) + 1
                    if imported_ids:
                        if clear_preview_draft("Preview brouillon supprimée: nouveaux posts ajoutés. Les posts déjà planifiés restent conservés."):
                            st.info("Ancienne preview supprimée. Les posts déjà planifiés restent conservés.")
                    st.success(f"{added} posts ajoutés, {skipped} doublons réutilisés. Lot actif: {len(imported_posts)} posts.")
                    posts = db.list_posts(active_only=False)

        posts = db.list_posts(active_only=False)
        if not posts:
            st.warning("Aucun post dans la bibliothèque.")
        else:
            selected_post_ids = {int(p["id"]) for p in st.session_state.get("selected_posts", [])}
            default_posts = not selected_post_ids
            post_rows = []
            for post in posts:
                post_rows.append(
                    {
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
            edited_posts = st.data_editor(
                pd.DataFrame(post_rows),
                hide_index=True,
                use_container_width=True,
                height=520,
                column_config={
                    "use": st.column_config.CheckboxColumn("Utiliser"),
                    "active": st.column_config.CheckboxColumn("Actif"),
                    "id": st.column_config.NumberColumn("ID", disabled=True),
                    "caption": st.column_config.TextColumn("Texte", disabled=True, width="large"),
                    "media_ids": st.column_config.TextColumn("Media IDs", width="medium"),
                    "media_folder": st.column_config.TextColumn("Dossier média", disabled=True),
                    "variables": st.column_config.TextColumn("Variables", width="medium"),
                    "reply_chain": st.column_config.TextColumn("Replies", width="medium"),
                    "photo_note": st.column_config.TextColumn("Note photo", width="medium"),
                    "used": st.column_config.NumberColumn("Usages", disabled=True),
                },
                disabled=["id", "caption", "media_folder", "used"],
                key=f"posts_editor_{st.session_state.get('posts_editor_version', 0)}",
            )
            if st.button("Tout sélectionner"):
                clear_preview_draft("Preview brouillon supprimée: sélection de posts changée. Les posts déjà planifiés restent conservés.")
                st.session_state["selected_posts"] = db.list_posts(active_only=True)
                st.session_state["posts_editor_version"] = st.session_state.get("posts_editor_version", 0) + 1
                st.rerun()
            if st.button("Sauver posts/photos"):
                for _, row in edited_posts.iterrows():
                    db.update_post_metadata(
                        int(row["id"]),
                        row["media_ids"],
                        str(row.get("photo_note", "")),
                        bool(row["active"]),
                        str(row.get("media_folder", "")),
                        parse_variables_text(str(row.get("variables", ""))),
                        str(row.get("reply_chain", "")),
                    )
                if clear_preview_draft("Preview brouillon supprimée: posts/photos modifiés. Les posts déjà planifiés restent conservés."):
                    st.info("Ancienne preview supprimée. Les posts déjà planifiés restent conservés.")
                posts = db.list_posts(active_only=False)
                st.success("Posts/photos sauvegardés.")

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
            selected_signature = tuple(sorted(int(post["id"]) for post in selected_posts))
            previous_signature = st.session_state.get("_selected_posts_signature")
            if previous_signature is not None and previous_signature != selected_signature:
                clear_preview_draft("Preview brouillon supprimée: sélection de posts changée. Les posts déjà planifiés restent conservés.")
            st.session_state["_selected_posts_signature"] = selected_signature
            if selected_posts:
                st.success(f"{len(selected_posts)} posts sélectionnés, dont {sum(1 for p in selected_posts if p.get('media_ids'))} avec photo/media.")
                if len(selected_posts) < int(current["posts_max"]):
                    st.warning(
                        f"{len(selected_posts)} textes pour jusqu'à {int(current['posts_max'])} posts par compte: "
                        "rotation activée, certains textes seront réutilisés."
                    )
            else:
                st.warning("Aucun post sélectionné. Preview bloquée.")

with tabs[3]:
    st.subheader("Preview du planning")
    section_intro(
        "Étape 4",
        "Contrôle exactement ce qui va partir: compte, groupe, heure, média, statut, erreurs.",
        "Utilise les filtres avant de passer à l'envoi. Failed et erreurs restent visibles ici.",
    )
    current = settings()
    capacity = max_posts_for_window(current["start_time"], current["end_time"], int(current["min_interval"]))
    enough_context = (
        bool(st.session_state.get("selected_posts"))
        and bool(st.session_state.get("grouped_accounts"))
        and int(current["posts_max"]) <= capacity
    )
    if not enough_context:
        blockers = []
        if not st.session_state.get("grouped_accounts"):
            blockers.append("Aucun compte sélectionné.")
        if int(current["posts_max"]) > capacity:
            blockers.append("Cadence impossible avec la plage horaire et l'intervalle actuels.")
        if not st.session_state.get("selected_posts"):
            blockers.append("Aucun post sélectionné.")
        render_locked_step("Preview bloquée: termine les étapes précédentes.", blockers)
    if st.button("Générer preview", disabled=not enough_context):
        try:
            rows = generate_schedule(
                selected_posts=st.session_state.get("selected_posts", []),
                grouped_accounts=st.session_state.get("grouped_accounts", {}),
                publish_date=current["publish_date"],
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
            db.save_preview(rows)
            st.session_state["preview_rows"] = db.list_scheduled("preview")
            st.success(f"Planning généré : {len(rows)} posts.")
        except Exception as e:
            st.error(str(e))

    all_scheduled = attach_threads_urls(db.list_scheduled())
    if all_scheduled:
        st.markdown("### Preview, planifiés & failed")
        render_status_counts(all_scheduled)
        category_counts = schedule_category_counts(all_scheduled)
        category_labels = [
            f"Preview brouillon ({category_counts['Preview brouillon']})",
            f"Anciennes previews ({category_counts['Anciennes previews']})",
            f"Déjà planifiés ({category_counts['Déjà planifiés']})",
            f"Failed ({category_counts['Failed']})",
            f"Tout ({category_counts['Tout']})",
        ]
        category_choice = st.radio(
            "Catégorie",
            category_labels,
            horizontal=True,
            help="La preview est le brouillon du nouveau lot. Les posts déjà planifiés/envoyés restent dans leur catégorie séparée.",
        )
        category = category_choice.split(" (", 1)[0]
        if category == "Preview brouillon":
            category_rows = [row for row in all_scheduled if str(row.get("status")) == "preview"]
        elif category == "Anciennes previews":
            category_rows = [row for row in all_scheduled if str(row.get("status")) == "preview_saved"]
        elif category == "Déjà planifiés":
            category_rows = [
                row for row in all_scheduled
                if str(row.get("status")) not in ("preview", "preview_saved") and not is_failed_status(row)
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

            visible_cols = ["day", "time", "preview_batch", "account_name", "threads_url", "group_name", "status", "photos", "replies", "text", "error"]
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

with tabs[4]:
    st.subheader("Analytics de volume")
    section_intro(
        "Étape 5",
        "Contrôle les volumes par compte, groupe et période.",
        "Les analytics utilisent tous les posts connus: preview, scheduled, published, failed et erreurs.",
    )
    render_analytics(db.list_scheduled())

with tabs[5]:
    st.subheader("Envoi Postoria")
    section_intro(
        "Étape 6",
        "Dernier verrou avant action réelle.",
        "L'envoi reste bloqué tant qu'il manque comptes, posts, preview, API ou que dry-run est actif.",
    )
    with st.expander("Workspace Postoria", expanded=not st.session_state.get("workspace_id")):
        workspace_id = render_workspace_picker(client, "send")
    preview = db.list_scheduled("preview")
    total_photos = sum(len(row.get("media_ids") or []) for row in preview)
    total_replies = sum(len(row.get("chain_replies") or []) for row in preview)
    st.write(f"{len(preview)} posts en preview, {total_photos} media IDs attachés.")
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
            failures = defaultdict(int)
            for row in preview:
                if failures[row["account_id"]] >= 2:
                    db.update_scheduled_result(row["id"], None, "skipped", "Compte désactivé après 2 échecs d'affilée")
                    continue
                try:
                    res = client.create_post(
                        int(workspace_id),
                        int(row["account_id"]),
                        row["caption"],
                        row["scheduled_time_utc"],
                        row.get("media_ids") or [],
                    )
                    db.update_scheduled_result(row["id"], res.get("id"), res.get("status", "scheduled"), None)
                    failures[row["account_id"]] = 0
                except Exception as e:
                    failures[row["account_id"]] += 1
                    db.update_scheduled_result(row["id"], None, "failed", str(e))
            st.success("Traitement terminé.")

    if st.button("Vérifier statuts Postoria"):
        if not client or not workspace_id:
            st.error("Client Postoria ou workspace manquant.")
        else:
            checked = 0
            for row in db.list_scheduled():
                if row.get("postoria_post_id"):
                    try:
                        res = client.get_post(int(workspace_id), int(row["postoria_post_id"]))
                        db.update_scheduled_result(row["id"], row["postoria_post_id"], res.get("status", "unknown"), None)
                        checked += 1
                    except Exception as e:
                        db.update_scheduled_result(row["id"], row["postoria_post_id"], "status_error", str(e))
            st.success(f"Statuts mis à jour pour {checked} posts Postoria.")

    st.caption("Aucune suppression Postoria n'est disponible dans cette app. Les posts envoyés restent conservés côté Postoria.")

    scheduled = attach_threads_urls(db.list_scheduled())
    if scheduled:
        st.dataframe(
            pd.DataFrame(scheduled),
            use_container_width=True,
            hide_index=True,
            column_config={"threads_url": st.column_config.LinkColumn("Threads", display_text="Ouvrir")},
        )
