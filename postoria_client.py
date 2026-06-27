from __future__ import annotations

import os
import requests
from dotenv import load_dotenv

load_dotenv()


class PostoriaClient:
    def __init__(self) -> None:
        self.api_key = os.getenv("POSTORIA_API_KEY", "").strip()
        self.base_url = os.getenv("POSTORIA_BASE_URL", "https://api.postoria.io/v1").rstrip("/")
        if not self.api_key:
            raise RuntimeError("POSTORIA_API_KEY manquant dans .env")

    @property
    def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _request(self, method: str, path: str, **kwargs):
        url = f"{self.base_url}{path}"
        res = requests.request(method, url, headers=self.headers, timeout=30, **kwargs)
        if res.status_code == 204:
            return None
        try:
            data = res.json()
        except Exception:
            data = {"raw": res.text}
        if not res.ok:
            message = data.get("error", {}).get("message") or str(data)
            raise RuntimeError(f"Postoria API error {res.status_code}: {message}")
        return data

    def list_workspaces(self) -> list[dict]:
        return self._request("GET", "/workspaces").get("data", [])

    def list_social_accounts(self, workspace_id: int) -> list[dict]:
        return self._request("GET", f"/workspaces/{workspace_id}/social-accounts").get("data", [])

    def create_post(
        self,
        workspace_id: int,
        account_id: int,
        caption: str,
        scheduled_time_utc: str,
        media_ids: list[str] | None = None,
    ) -> dict:
        media_ids = media_ids or []
        payload = {
            "publish_mode": "schedule",
            "social_account_ids": [account_id],
            "content_type": "image" if media_ids else "text",
            "caption": caption,
            "media_ids": media_ids,
            "link_url": None,
            "first_comment": None,
            "scheduled_time": scheduled_time_utc,
            "queue_id": None,
            "repost": None,
            "youtube": None,
            "tiktok": None,
        }
        return self._request("POST", f"/workspaces/{workspace_id}/posts", json=payload)

    def create_text_post(self, workspace_id: int, account_id: int, caption: str, scheduled_time_utc: str) -> dict:
        return self.create_post(int(workspace_id), int(account_id), caption, scheduled_time_utc, [])

    def get_post(self, workspace_id: int, post_id: int) -> dict:
        return self._request("GET", f"/workspaces/{workspace_id}/posts/{post_id}")
