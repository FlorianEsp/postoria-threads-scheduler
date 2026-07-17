from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import requests


class GroupStoreError(RuntimeError):
    """Raised when the optional Supabase configuration store cannot be reached."""


class SupabaseGroupStore:
    """Small server-side store for account groups and account preferences."""

    def __init__(self, base_url: str = "", service_key: str = "") -> None:
        self.base_url = str(base_url or "").strip().rstrip("/")
        self.service_key = str(service_key or "").strip()

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.service_key)

    @property
    def _endpoint(self) -> str:
        return f"{self.base_url}/rest/v1/scheduler_group_configs"

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "apikey": self.service_key,
            "Authorization": f"Bearer {self.service_key}",
            "Content-Type": "application/json",
        }

    def load(self, workspace_id: str) -> dict[str, Any] | None:
        if not self.configured:
            return None
        try:
            response = requests.get(
                self._endpoint,
                params={"workspace_id": f"eq.{workspace_id}", "select": "config"},
                headers=self._headers,
                timeout=10,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            raise GroupStoreError(f"Sauvegarde Supabase indisponible : {exc}") from exc
        rows = response.json()
        if not rows:
            return None
        config = rows[0].get("config")
        return config if isinstance(config, dict) else None

    def save(self, workspace_id: str, config: dict[str, Any]) -> None:
        if not self.configured:
            return
        payload = {
            "workspace_id": str(workspace_id),
            "config": config,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        headers = {**self._headers, "Prefer": "resolution=merge-duplicates,return=minimal"}
        try:
            response = requests.post(
                self._endpoint,
                params={"on_conflict": "workspace_id"},
                headers=headers,
                json=payload,
                timeout=10,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            raise GroupStoreError(f"Impossible de sauvegarder les groupes dans Supabase : {exc}") from exc
