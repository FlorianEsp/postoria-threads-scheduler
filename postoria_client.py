from __future__ import annotations

import os
import requests
from dotenv import load_dotenv

load_dotenv()


class PostoriaClient:
    def __init__(self, api_key: str | None = None, base_url: str | None = None) -> None:
        self.api_key = str(api_key if api_key is not None else os.getenv("POSTORIA_API_KEY", "")).strip()
        self.base_url = str(base_url if base_url is not None else os.getenv("POSTORIA_BASE_URL", "https://api.postoria.io/v1")).rstrip("/")
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

    @staticmethod
    def _data_payload(response: dict | None) -> dict:
        if not isinstance(response, dict):
            return {}
        data = response.get("data")
        return data if isinstance(data, dict) else response

    def list_workspaces(self) -> list[dict]:
        return self._request("GET", "/workspaces").get("data", [])

    def list_social_accounts(self, workspace_id: int) -> list[dict]:
        return self._request("GET", f"/workspaces/{workspace_id}/social-accounts").get("data", [])

    def list_posts(self, workspace_id: int, max_items: int = 500) -> list[dict]:
        """Read recent Postoria posts without loading an unbounded workspace history."""
        rows: list[dict] = []
        cursor = ""
        safe_max = max(1, min(int(max_items), 1000))
        while len(rows) < safe_max:
            params = {"limit": min(100, safe_max - len(rows))}
            if cursor:
                params["cursor"] = cursor
            response = self._request(
                "GET",
                f"/workspaces/{int(workspace_id)}/posts",
                params=params,
            )
            page = response.get("data", []) if isinstance(response, dict) else []
            if not isinstance(page, list) or not page:
                break
            rows.extend(item for item in page if isinstance(item, dict))
            pagination = response.get("pagination", {}) if isinstance(response, dict) else {}
            cursor = str(pagination.get("next_cursor") or "") if isinstance(pagination, dict) else ""
            if not pagination.get("has_more") or not cursor:
                break
        return rows[:safe_max]

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

    def create_media_upload(self, workspace_id: int, name: str, content_type: str) -> dict:
        return self._data_payload(
            self._request(
                "POST",
                f"/workspaces/{int(workspace_id)}/media/uploads",
                json={"name": str(name), "content_type": str(content_type)},
            )
        )

    def complete_media_upload(self, workspace_id: int, media_id: int | str) -> dict:
        return self._data_payload(
            self._request(
                "POST",
                f"/workspaces/{int(workspace_id)}/media/{media_id}/complete",
                json={},
            )
        )

    def get_media(self, workspace_id: int, media_id: int | str) -> dict:
        return self._data_payload(
            self._request("GET", f"/workspaces/{int(workspace_id)}/media/{media_id}")
        )

    def upload_media(
        self,
        workspace_id: int,
        name: str,
        content_type: str,
        file_bytes: bytes,
    ) -> dict:
        created = self.create_media_upload(int(workspace_id), name, content_type)
        media_id = created.get("id") if isinstance(created, dict) else None
        upload_url = created.get("upload", {}).get("url") if isinstance(created, dict) else None
        if not media_id or not upload_url:
            raise RuntimeError(f"Réponse upload Postoria incomplète: {created}")
        response = requests.put(
            str(upload_url),
            data=file_bytes,
            headers={"Content-Type": str(content_type)},
            timeout=90,
        )
        response.raise_for_status()
        completed = self.complete_media_upload(int(workspace_id), media_id)
        result = {"id": media_id, "status": "processing"}
        if isinstance(completed, dict):
            result.update(completed)
        if not result.get("id"):
            result["id"] = media_id
        return result
