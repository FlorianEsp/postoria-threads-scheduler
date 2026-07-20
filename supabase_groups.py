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

    @property
    def photo_bucket(self) -> str:
        return "postoria-photo-library"

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

    def ensure_photo_bucket(self) -> None:
        if not self.configured:
            return
        try:
            current = requests.get(
                f"{self.base_url}/storage/v1/bucket/{self.photo_bucket}",
                headers=self._headers,
                timeout=15,
            )
            if current.status_code == 200:
                return
            if current.status_code != 404:
                current.raise_for_status()
            response = requests.post(
                f"{self.base_url}/storage/v1/bucket",
                headers=self._headers,
                json={"id": self.photo_bucket, "name": self.photo_bucket, "public": False},
                timeout=15,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            raise GroupStoreError(f"Bucket photos Supabase indisponible : {exc}") from exc

    def upload_photo(self, workspace_id: str, asset_key: str, name: str, content_type: str, data: bytes) -> str:
        if not self.configured:
            return ""
        self.ensure_photo_bucket()
        suffix = str(name or "photo").rsplit(".", 1)[-1].lower()
        suffix = suffix if suffix in {"jpg", "jpeg", "png", "webp", "gif"} else "jpg"
        path = f"{workspace_id}/{asset_key}.{suffix}"
        headers = {
            **self._headers,
            "Content-Type": str(content_type or "image/jpeg"),
            "x-upsert": "true",
        }
        try:
            response = requests.post(
                f"{self.base_url}/storage/v1/object/{self.photo_bucket}/{path}",
                headers=headers,
                data=data,
                timeout=60,
            )
            response.raise_for_status()
            return path
        except requests.RequestException as exc:
            raise GroupStoreError(f"Upload photo Supabase impossible : {exc}") from exc

    def download_photo(self, path: str) -> bytes:
        if not self.configured or not str(path or "").strip():
            return b""
        try:
            response = requests.get(
                f"{self.base_url}/storage/v1/object/{self.photo_bucket}/{str(path).lstrip('/')}",
                headers=self._headers,
                timeout=45,
            )
            response.raise_for_status()
            return response.content
        except requests.RequestException as exc:
            raise GroupStoreError(f"Lecture photo Supabase impossible : {exc}") from exc
