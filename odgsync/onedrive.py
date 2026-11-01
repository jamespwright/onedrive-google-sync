import json
import logging
import os
from dataclasses import dataclass

import msal

from .http import AuthError, check, with_retries

log = logging.getLogger(__name__)

GRAPH = "https://graph.microsoft.com/v1.0"
# Bundles (albums) only exist on personal OneDrive, so sign in against consumer accounts.
AUTHORITY = "https://login.microsoftonline.com/consumers"


def cached_username(cache_path: str) -> str | None:
    """Who a token cache belongs to, read from the file without contacting Microsoft."""
    try:
        with open(cache_path, encoding="utf-8") as f:
            accounts = json.load(f).get("Account") or {}
    except (OSError, ValueError):
        return None
    return next((a.get("username") for a in accounts.values()), None)


@dataclass
class Photo:
    key: str  # stable across runs: "<account>:<itemId>" (item ids already embed the drive)
    name: str
    mime: str
    content_url: str
    content_hash: str | None = None  # same bytes, same hash, even across drives


class OneDriveAccount:
    def __init__(self, name: str, client_id: str, scopes: list[str], token_dir: str):
        self.name = name
        self.scopes = scopes
        self.cache_path = os.path.join(token_dir, f"onedrive_{name}.json")
        self.cache = msal.SerializableTokenCache()
        if os.path.exists(self.cache_path):
            with open(self.cache_path, encoding="utf-8") as f:
                self.cache.deserialize(f.read())
        self.app = msal.PublicClientApplication(client_id, authority=AUTHORITY, token_cache=self.cache)
        self.http = with_retries()

    def _save_cache(self):
        if not self.cache.has_state_changed:
            return
        os.makedirs(os.path.dirname(self.cache_path), exist_ok=True)
        tmp = self.cache_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(self.cache.serialize())
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.cache_path)

    def start_device_flow(self) -> dict:
        flow = self.app.initiate_device_flow(scopes=self.scopes)
        if "user_code" not in flow:
            raise RuntimeError(f"Could not start device flow: {flow.get('error_description', flow)}")
        return flow

    def finish_device_flow(self, flow: dict) -> str:
        """Blocks until the user enters the code (or it expires); returns the signed-in username."""
        result = self.app.acquire_token_by_device_flow(flow)
        if "access_token" not in result:
            raise RuntimeError(f"Sign-in failed: {result.get('error_description', result)}")
        self._save_cache()
        return result.get("id_token_claims", {}).get("preferred_username", "unknown")

    def authenticate_interactive(self) -> str:
        flow = self.start_device_flow()
        print(flow["message"], flush=True)
        return self.finish_device_flow(flow)

    def _token(self) -> str:
        accounts = self.app.get_accounts()
        result = self.app.acquire_token_silent(self.scopes, account=accounts[0]) if accounts else None
        if not result or "access_token" not in result:
            raise AuthError(f"OneDrive account '{self.name}' is not signed in; run: auth-onedrive {self.name}")
        self._save_cache()
        return result["access_token"]

    def _get(self, url: str, **kwargs):
        headers = {"Authorization": f"Bearer {self._token()}"}
        return check(self.http.get(url, headers=headers, timeout=120, **kwargs))

    def _get_paged(self, url: str):
        while url:
            data = self._get(url).json()
            yield from data.get("value", [])
            url = data.get("@odata.nextLink")

    def albums(self) -> list[dict]:
        # Bundles also cover plain multi-select shares; only those with an album facet are albums.
        return [b for b in self._get_paged(f"{GRAPH}/me/drive/bundles") if "album" in (b.get("bundle") or {})]

    def resolve_album(self, name: str | None, album_id: str | None) -> str:
        if album_id:
            return album_id
        matches = [a for a in self.albums() if a.get("name", "").casefold() == name.casefold()]
        if not matches:
            raise LookupError(f"No OneDrive album named '{name}' on account '{self.name}'")
        if len(matches) > 1:
            ids = ", ".join(a["id"] for a in matches)
            raise LookupError(f"Several albums named '{name}' on '{self.name}'; set album_id to one of: {ids}")
        return matches[0]["id"]

    def album_photos(self, album_id: str, include_videos: bool) -> list[Photo]:
        photos = []
        for item in self._get_paged(f"{GRAPH}/me/drive/items/{album_id}/children"):
            if "file" not in item:
                continue
            mime = item["file"].get("mimeType") or "application/octet-stream"
            is_video = "video" in item or mime.startswith("video/")
            is_image = "image" in item or "photo" in item or mime.startswith("image/")
            if is_video and not include_videos:
                continue
            if not (is_image or is_video):
                continue
            # Items shared from someone else's drive arrive as remoteItem. For our own items,
            # parentReference.driveId is now an encoded "b!..." id that /drives/{id} rejects,
            # so address them through /me/drive instead.
            remote = item.get("remoteItem")
            if remote:
                drive_id = remote["parentReference"]["driveId"]
                item_id = remote["id"]
                content_url = f"{GRAPH}/drives/{drive_id}/items/{item_id}/content"
            else:
                item_id = item["id"]
                content_url = f"{GRAPH}/me/drive/items/{item_id}/content"
            hashes = item["file"].get("hashes") or ((remote or {}).get("file") or {}).get("hashes") or {}
            content_hash = hashes.get("quickXorHash") or hashes.get("sha1Hash")
            photos.append(Photo(f"{self.name}:{item_id}", item.get("name", item_id), mime, content_url, content_hash))
        return photos

    def download(self, photo: Photo) -> bytes:
        # /content redirects to a pre-authenticated URL; requests drops the bearer
        # header when the redirect changes host, which is what that URL expects.
        return self._get(photo.content_url).content
