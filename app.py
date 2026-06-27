from __future__ import annotations

import os
from collections import defaultdict
from datetime import date, datetime, time, timedelta
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
        if row is None or not bool(row["use"]) or not bool(row["active"]):
            continue
        group_name = str(row["group"] or "tous").strip()
        db.update_account_preferences(int(account["id"]), group_name, bool(row["active"]))
        grouped.setdefault(group_name, {"offset_minutes": len(grouped) * 10, "accounts": []})
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
    return df


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
        f"Chaque post garde au moins {current['min_interval']}min d'écart avec le post précédent du même compte."
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


def render_locked_step(title: str, blockers: list[str]) -> None:
    st.warning(title)
    for blocker in blockers:
        st.caption(f"- {blocker}")


def render_group_cards(groups: list[dict], grouped: dict[str, dict] | None = None) -> None:
    if not groups:
        st.info("Aucun groupe. Crée un groupe, puis assigne les comptes.")
        return
    grouped = grouped or {}
    cols = st.columns(min(4, max(1, len(groups))))
    for idx, group in enumerate(groups):
        name = group["name"]
        selected_count = len(grouped.get(name, {}).get("accounts", []))
        with cols[idx % len(cols)]:
            st.markdown(
                "<div class='step-note'>"
                f"<b>{name}</b><br>"
                f"{selected_count} sélectionnés<br>"
                f"<small>{group.get('account_count', 0)} comptes assignés</small>"
                "</div>",
                unsafe_allow_html=True,
            )


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
    send_ready: bool,
) -> None:
    steps = [
        ("1", "Comptes", accounts_ready),
        ("2", "Cadence", cadence_ready),
        ("3", "Posts/photos", posts_ready),
        ("4", "Preview", preview_ready),
        ("5", "Envoi", send_ready),
    ]
    cols = st.columns(5)
    for col, (number, label, ready) in zip(cols, steps):
        state = "OK" if ready else "À faire"
        col.markdown(
            "<div class='step-note'>"
            f"<b>{number}. {label}</b><br>{state}"
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

    picked_workspace = st.selectbox(
        "Workspace Postoria",
        options=workspace_ids,
        index=current_index,
        format_func=lambda wid: next(str(w.get("name", wid)) for w in workspaces if str(w["id"]) == str(wid)),
        key=f"{key_prefix}_workspace_id",
    )
    st.session_state["workspace_id"] = picked_workspace
    return picked_workspace


st.set_page_config(page_title="Postoria Threads Scheduler", layout="wide")
st.markdown(
    """
    <style>
    .block-container {padding-top: 1.4rem; max-width: 1280px;}
    h1 {letter-spacing: 0;}
    [data-testid="stCaptionContainer"] p {line-height: 1.55;}
    div[data-testid="stMetric"] {
        border: 1px solid rgba(255,255,255,.12);
        border-radius: 8px;
        padding: 12px 14px;
        background: rgba(255,255,255,.035);
    }
    div[data-testid="stMetric"] label {
        color: rgba(250,250,250,.72);
        font-size: .82rem;
    }
    div[data-testid="stMetricValue"] {
        font-size: 2rem;
    }
    div[data-testid="stDataFrame"] {
        border: 1px solid rgba(255,255,255,.10);
        border-radius: 8px;
        overflow: hidden;
    }
    .step-note {
        border: 1px solid rgba(255,255,255,.12);
        border-radius: 8px;
        padding: 14px 16px;
        background: rgba(255,255,255,.035);
        min-height: 76px;
    }
    .section-intro {
        border: 1px solid rgba(255,255,255,.12);
        border-radius: 8px;
        padding: 14px 16px;
        margin: 10px 0 18px 0;
        background: rgba(255,255,255,.032);
    }
    .section-intro span {
        display: inline-block;
        color: #ff5f7e;
        font-size: .78rem;
        font-weight: 700;
        letter-spacing: .08em;
        text-transform: uppercase;
        margin-bottom: 6px;
    }
    .section-intro strong {
        display: block;
        font-size: 1.05rem;
        margin-bottom: 4px;
    }
    .section-intro p {
        color: rgba(250,250,250,.68);
        margin: 0;
        line-height: 1.45;
    }
    .warn-copy {color: #ff5f7e; font-weight: 700;}
    </style>
    """,
    unsafe_allow_html=True,
)

st.title("Bulk Threads Scheduler")
st.caption("Prépare un gros planning Threads: groupes, textes, médias, horaires, preview, puis envoi Postoria.")

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
    st.write("5. Confirmer l'envoi")

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
send_ready = preview_ready and api_exists and not dry_run

metric_a, metric_b, metric_c, metric_d = st.columns(4)
metric_a.metric("Comptes prêts", selected_accounts_count)
metric_b.metric("Textes prêts", selected_posts_count)
metric_c.metric("Posts en preview", len(preview))
metric_d.metric("Médias attachés", sum(1 for p in st.session_state.get("selected_posts", []) if p.get("media_ids")))

render_flow_status(accounts_ready, cadence_ready, posts_ready, preview_ready, send_ready)

tabs = st.tabs(["1. Comptes", "2. Cadence", "3. Posts & Photos", "4. Preview", "5. Envoi"])

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
                workspace_id = st.selectbox(
                    "Workspace",
                    options=[w["id"] for w in workspaces],
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
        st.markdown("#### Groupes")
        create_col, offset_col, button_col = st.columns([2, 1, 1])
        with create_col:
            new_group_name = st.text_input("Créer un groupe", placeholder="ex: w-u, group 5 post")
        with offset_col:
            new_group_offset = st.number_input("Offset min", min_value=0, max_value=1440, value=0, step=5)
        with button_col:
            st.write("")
            st.write("")
            if st.button("Ajouter groupe", disabled=not new_group_name.strip()):
                created = db.upsert_group(new_group_name, int(new_group_offset))
                st.success("Groupe créé." if created else "Groupe mis à jour.")

        groups = db.list_groups()
        group_options = [g["name"] for g in groups] or ["tous"]
        selected_group_filters = st.multiselect(
            "Groupes à utiliser",
            options=group_options,
            default=st.session_state.get("selected_group_filters", []),
            key="selected_group_filters",
            help="Choisir un groupe ajoute tous ses comptes. Tu peux décocher un compte ensuite.",
        )
        selected_ids = {int(a["id"]) for a in st.session_state.get("selected_accounts", [])}

        render_group_cards(groups, st.session_state.get("grouped_accounts", {}))
        if selected_group_filters:
            st.markdown("#### Comptes dans les groupes sélectionnés")
            for group_name in selected_group_filters:
                group_accounts = [
                    account for account in accounts
                    if (account.get("group_name") or "tous") == group_name
                ]
                labels = [account_label(account) for account in group_accounts]
                with st.expander(f"{group_name} · {len(group_accounts)} comptes", expanded=True):
                    st.write(", ".join(labels) if labels else "Aucun compte dans ce groupe.")
        else:
            st.info("Sélectionne un ou plusieurs groupes pour commencer.")

        st.markdown("#### Ajuster les comptes")
        st.caption("Les groupes remplissent la sélection. Le tableau sert à décocher un compte ou changer son groupe.")
        rows = []
        for account in accounts:
            active = bool(account.get("active_for_day", 1))
            group_name = account.get("group_name") or "tous"
            if group_name not in group_options:
                group_options.append(group_name)
            selected_by_group = group_name in selected_group_filters if selected_group_filters else False
            rows.append(
                {
                    "use": active and (selected_by_group or int(account["id"]) in selected_ids),
                    "id": int(account["id"]),
                    "compte": account_label(account),
                    "group": account.get("group_name") or "tous",
                    "active": active,
                    "url": account.get("url", ""),
                }
            )
        edited_accounts = st.data_editor(
            pd.DataFrame(rows),
            hide_index=True,
            use_container_width=True,
            height=min(760, 140 + max(1, len(rows)) * 36),
            column_config={
                "use": st.column_config.CheckboxColumn("Utiliser"),
                "id": st.column_config.NumberColumn("ID", disabled=True),
                "compte": st.column_config.TextColumn("Compte", disabled=True),
                "group": st.column_config.SelectboxColumn("Groupe", options=group_options),
                "active": st.column_config.CheckboxColumn("Actif"),
                "url": st.column_config.LinkColumn("Lien", disabled=True),
            },
            disabled=["id", "compte", "url"],
            key="accounts_editor",
        )
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
                added, skipped = db.add_posts(make_post_records(frame))
                st.success(f"{added} posts ajoutés, {skipped} ignorés/doublons.")
                posts = db.list_posts(active_only=False)
        with manual_col:
            with st.form("manual_posts"):
                bulk = st.text_area("Ajouter textes", height=120, placeholder="Un texte par ligne")
                media_for_bulk = st.text_input("Media IDs pour ces textes")
                folder_for_bulk = st.selectbox("Dossier média", folder_options)
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
                    added, skipped = db.add_posts(records)
                    st.success(f"{added} posts ajoutés, {skipped} ignorés/doublons.")
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
                    "media_folder": st.column_config.SelectboxColumn("Dossier média", options=folder_options),
                    "variables": st.column_config.TextColumn("Variables", width="medium"),
                    "reply_chain": st.column_config.TextColumn("Replies", width="medium"),
                    "photo_note": st.column_config.TextColumn("Note photo", width="medium"),
                    "used": st.column_config.NumberColumn("Usages", disabled=True),
                },
                disabled=["id", "caption", "used"],
                key="posts_editor",
            )
            if st.button("Tout sélectionner"):
                st.session_state["selected_posts"] = db.list_posts(active_only=True)
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

    all_scheduled = db.list_scheduled()
    if all_scheduled:
        st.markdown("### Preview & statuts")
        render_status_counts(all_scheduled)
        filter_col, list_col = st.columns([1, 3])
        all_df = scheduled_dataframe(all_scheduled)
        account_options = ["Tous les comptes"] + sorted(all_df["account_name"].dropna().astype(str).unique().tolist())
        group_options = ["Tous les groupes"] + sorted(all_df["group_name"].fillna("Sans groupe").astype(str).unique().tolist())
        status_options = ["Tous"] + sorted(all_df["status"].dropna().astype(str).unique().tolist())
        with filter_col:
            st.markdown("#### Filtres")
            status_filter = st.selectbox("Statut", status_options, index=0)
            date_filter = st.radio("Date", ["Tout", "Aujourd'hui", "Semaine", "Mois"], horizontal=False)
            account_filter = st.selectbox("Compte", account_options, index=0)
            group_filter = st.selectbox("Groupe", group_options, index=0)
            view_mode = st.radio("Vue", ["Tout", "Par compte", "Par jour", "Par groupe", "Failed"], horizontal=False)
            sort_mode = st.selectbox("Tri", ["Heure", "Compte", "Jour", "Statut"], index=0)
        with list_col:
            query = st.text_input("Rechercher posts, comptes, erreurs", placeholder="Recherche...")
            filtered = filter_scheduled_rows(all_scheduled, status_filter, date_filter, account_filter, group_filter, query)
            if view_mode == "Failed":
                filtered = filtered[filtered["status"].astype(str).str.contains("fail|error", case=False, regex=True)]
            if sort_mode == "Compte":
                filtered = filtered.sort_values(["account_name", "scheduled_time_local"])
            elif sort_mode == "Jour":
                filtered = filtered.sort_values(["day", "scheduled_time_local", "account_name"])
            elif sort_mode == "Statut":
                filtered = filtered.sort_values(["status", "scheduled_time_local"])
            else:
                filtered = filtered.sort_values(["scheduled_time_local", "account_name"])

            st.caption(f"{len(filtered)} posts affichés sur {len(all_scheduled)}")
            failed_rows = filtered[filtered["status"].astype(str).str.contains("fail|error", case=False, regex=True)]
            if not failed_rows.empty:
                st.error(f"{len(failed_rows)} posts failed/error. Question: corriger les posts, relancer ces comptes, ou supprimer ces programmations ?")

            visible_cols = ["day", "time", "account_name", "group_name", "status", "photos", "replies", "text", "error"]
            if filtered.empty:
                st.info("Aucun post trouvé avec ces filtres.")
            elif view_mode == "Par compte":
                for account_name, chunk in filtered.groupby("account_name", sort=True):
                    with st.expander(f"{account_name} - {len(chunk)} posts", expanded=True):
                        st.dataframe(chunk[visible_cols], use_container_width=True, hide_index=True)
            elif view_mode == "Par jour":
                for day, chunk in filtered.groupby("day", sort=True):
                    with st.expander(f"{day} - {len(chunk)} posts", expanded=True):
                        st.dataframe(chunk[visible_cols], use_container_width=True, hide_index=True)
            elif view_mode == "Par groupe":
                for group_name, chunk in filtered.groupby("group_name", sort=True):
                    with st.expander(f"{group_name or 'Sans groupe'} - {len(chunk)} posts", expanded=True):
                        st.dataframe(chunk[visible_cols], use_container_width=True, hide_index=True)
            else:
                st.dataframe(filtered[visible_cols], use_container_width=True, hide_index=True, height=620)

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

with tabs[4]:
    st.subheader("Envoi Postoria")
    section_intro(
        "Étape 5",
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

    if send_blockers:
        st.warning("Envoi bloqué : " + ", ".join(send_blockers) + ".")
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

    check_col, delete_col = st.columns(2)
    with check_col:
        if st.button("Vérifier statuts Postoria"):
            if not client or not workspace_id:
                st.error("Client Postoria ou workspace manquant.")
            else:
                for row in db.list_scheduled():
                    if row.get("postoria_post_id"):
                        try:
                            res = client.get_post(int(workspace_id), int(row["postoria_post_id"]))
                            db.update_scheduled_result(row["id"], row["postoria_post_id"], res.get("status", "unknown"), None)
                        except Exception as e:
                            db.update_scheduled_result(row["id"], row["postoria_post_id"], "status_error", str(e))
                st.success("Statuts mis à jour.")
    with delete_col:
        if st.button("Supprimer posts programmés dans Postoria"):
            if not client or not workspace_id:
                st.error("Client Postoria ou workspace manquant.")
            else:
                for row in db.list_scheduled():
                    if row.get("postoria_post_id"):
                        try:
                            client.delete_post(int(workspace_id), int(row["postoria_post_id"]))
                            db.update_scheduled_result(row["id"], row["postoria_post_id"], "deleted", None)
                        except Exception as e:
                            db.update_scheduled_result(row["id"], row["postoria_post_id"], "delete_error", str(e))
                st.success("Suppression terminée.")

    scheduled = db.list_scheduled()
    if scheduled:
        st.dataframe(pd.DataFrame(scheduled), use_container_width=True, hide_index=True)
