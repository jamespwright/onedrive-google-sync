import logging
import os
from urllib.parse import parse_qs, urlparse

from google.auth.transport.requests import AuthorizedSession, Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow

from .http import AuthError, check, with_retries

log = logging.getLogger(__name__)

API = "https://photoslibrary.googleapis.com/v1"
# Since March 2025 the Library API only sees media and albums this app created.
SCOPES = [
    "https://www.googleapis.com/auth/photoslibrary.appendonly",
    "https://www.googleapis.com/auth/photoslibrary.readonly.appcreateddata",
    "https://www.googleapis.com/auth/photoslibrary.edit.appcreateddata",
]
# Desktop OAuth clients accept any loopback redirect. Nothing listens there: the user
# copies the URL the browser fails to load and pastes it back in.
REDIRECT_URI = "http://localhost:8080/"
BATCH_LIMIT = 50


class GoogleAccount:
    def __init__(self, name: str, client_secret_file: str, token_dir: str):
        self.name = name
        self.client_secret_file = client_secret_file
        self.token_path = os.path.join(token_dir, f"google_{name}.json")
        self._session = None

    def _save(self, creds: Credentials):
        os.makedirs(os.path.dirname(self.token_path), exist_ok=True)
        tmp = self.token_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(creds.to_json())
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.token_path)

    def start_auth(self) -> tuple[Flow, str]:
        """Returns the flow (keep it for finish_auth) and the URL the user must open."""
        if not os.path.exists(self.client_secret_file):
            raise FileNotFoundError(f"Google OAuth client file not found: {self.client_secret_file}")
        flow = Flow.from_client_secrets_file(self.client_secret_file, scopes=SCOPES, redirect_uri=REDIRECT_URI)
        url, _ = flow.authorization_url(access_type="offline", prompt="consent")
        return flow, url

    def finish_auth(self, flow: Flow, pasted: str):
        """pasted is the localhost URL the browser ended on, or just its code parameter."""
        pasted = pasted.strip()
        code = parse_qs(urlparse(pasted).query).get("code", [pasted])[0]
        flow.fetch_token(code=code)
        if not flow.credentials.refresh_token:
            raise RuntimeError("Google did not return a refresh token; revoke the app's access and retry")
        self._save(flow.credentials)

    @property
    def signed_in(self) -> bool:
        return os.path.exists(self.token_path)

    def authenticate_interactive(self):
        flow, url = self.start_auth()
        print("1. Open this URL in a browser and approve access:\n")
        print(url, "\n")
        print("2. The browser will end on a localhost page that fails to load. That is expected.")
        print("   Copy the full URL from the address bar and paste it here.\n")
        self.finish_auth(flow, input("URL: "))

    @property
    def session(self) -> AuthorizedSession:
        if self._session is None:
            if not os.path.exists(self.token_path):
                raise AuthError(f"Google account '{self.name}' is not signed in; run: auth-google {self.name}")
            creds = Credentials.from_authorized_user_file(self.token_path, SCOPES)
            if not creds.valid:
                try:
                    creds.refresh(Request())
                except Exception as e:
                    raise AuthError(
                        f"Google token for '{self.name}' could not be refreshed ({e}); run: auth-google {self.name}"
                    ) from e
                self._save(creds)
            self._session = with_retries(AuthorizedSession(creds))
        return self._session

    def _post(self, path: str, **kwargs):
        return check(self.session.post(f"{API}/{path}", timeout=300, **kwargs))

    def album_exists(self, album_id: str) -> bool:
        r = self.session.get(f"{API}/albums/{album_id}", timeout=60)
        if r.status_code in (400, 404):
            return False
        check(r)
        return True

    def create_album(self, title: str) -> str:
        return self._post("albums", json={"album": {"title": title}}).json()["id"]

    def upload(self, data: bytes, mime: str) -> str:
        headers = {
            "Content-Type": "application/octet-stream",
            "X-Goog-Upload-Content-Type": mime,
            "X-Goog-Upload-Protocol": "raw",
        }
        return self._post("uploads", data=data, headers=headers).text

    def batch_create(self, album_id: str, items: list[tuple[str, str]]) -> dict[str, str | None]:
        """items is [(upload_token, filename)]; returns {upload_token: media_item_id or None}."""
        body = {
            "albumId": album_id,
            "newMediaItems": [{"simpleMediaItem": {"uploadToken": t, "fileName": n}} for t, n in items],
        }
        results = {}
        for res in self._post("mediaItems:batchCreate", json=body).json().get("newMediaItemResults", []):
            status = res.get("status") or {}
            if status.get("code", 0) == 0 and "mediaItem" in res:
                results[res["uploadToken"]] = res["mediaItem"]["id"]
            else:
                log.warning("Google rejected an item: %s", status.get("message", status))
                results[res["uploadToken"]] = None
        return results

    def add_to_album(self, album_id: str, media_ids: list[str]):
        self._post(f"albums/{album_id}:batchAddMediaItems", json={"mediaItemIds": media_ids})

    def remove_from_album(self, album_id: str, media_ids: list[str]):
        self._post(f"albums/{album_id}:batchRemoveMediaItems", json={"mediaItemIds": media_ids})
