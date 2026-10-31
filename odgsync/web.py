"""Web UI and daily scheduler: the long-running mode of the container."""
import base64
import collections
import glob
import hmac
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import config as cfgmod
from .gphotos import GoogleAccount
from .onedrive import OneDriveAccount, cached_username
from .state import State
from .sync import Syncer

log = logging.getLogger(__name__)
INDEX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "index.html")
STATUS_PATH = os.path.join(cfgmod.CONFIG_DIR, "status.json")
# Account names become token file names, so keep them to safe characters.
NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")
# Browsers can't send a custom header cross-site without a CORS preflight (which we never
# approve), so requiring it on writes stops other pages from driving the API.
CSRF_HEADER = "X-ODGS"


class LogBuffer(logging.Handler):
    def __init__(self, size: int = 1000):
        super().__init__()
        self.lines = collections.deque(maxlen=size)
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S"))

    def emit(self, record):
        try:
            self.lines.append(self.format(record))
        except Exception:
            self.handleError(record)


def next_run(hhmm: str, now: datetime) -> datetime:
    h, m = map(int, hhmm.split(":"))
    at = now.replace(hour=h, minute=m, second=0, microsecond=0)
    return at if at > now else at + timedelta(days=1)


def iso(dt: datetime | None) -> str | None:
    return dt.isoformat(timespec="seconds") if dt else None


class Service:
    def __init__(self):
        self.lock = threading.Lock()  # held for the whole of a sync
        self.wake = threading.Event()  # set when settings change or on shutdown
        self.stop = threading.Event()
        self.logs = LogBuffer()
        self.running: str | None = None
        self.next_run: datetime | None = None
        self.onedrive_signins: dict[str, dict] = {}
        self.google_flows: dict[str, object] = {}
        self.server: ThreadingHTTPServer | None = None
        try:
            with open(STATUS_PATH, encoding="utf-8") as f:
                self.last = json.load(f)
        except (OSError, ValueError):
            self.last = {}

    # --- syncing ---

    def sync(self, reason: str) -> bool:
        """Runs one sync; returns False without doing anything if one is already running."""
        if not self.lock.acquire(blocking=False):
            log.info("%s sync skipped: a sync is already running", reason)
            return False
        self.running = reason
        started, ok = datetime.now(), False
        log.info("%s sync started", reason)
        try:
            cfg = cfgmod.load()
            state = State(cfgmod.STATE_PATH)
            try:
                ok = Syncer(cfg, state).run()
            finally:
                state.close()
        except cfgmod.ConfigError as e:
            log.error("%s", e)
        except Exception:
            log.exception("sync failed")
        finally:
            self.last = {"reason": reason, "started": iso(started), "finished": iso(datetime.now()), "ok": ok}
            try:
                with open(STATUS_PATH, "w", encoding="utf-8") as f:
                    json.dump(self.last, f)
            except OSError as e:
                log.warning("could not save %s: %s", STATUS_PATH, e)
            self.running = None
            self.lock.release()
        log.info("%s sync %s", reason, "finished" if ok else "finished with errors")
        return True

    def sync_in_background(self, reason: str) -> bool:
        if self.lock.locked():
            return False
        threading.Thread(target=self.sync, args=(reason,), daemon=True).start()
        return True

    def _schedule(self) -> str | None:
        try:
            cfg = cfgmod.load(require_targets=False)
        except cfgmod.ConfigError:
            return None
        return cfg.schedule_time if cfg.schedule_enabled else None

    def scheduler(self):
        schedule, due = None, None
        while not self.stop.is_set():
            # Re-read every pass so hand edits to config.yaml apply too. The due time only
            # moves when the schedule itself changes, so a check landing just after it still runs.
            current = self._schedule()
            if current != schedule:
                schedule = current
                due = next_run(schedule, datetime.now()) if schedule else None
            self.next_run = due
            if due and datetime.now() >= due:
                self.sync("scheduled")
                due = next_run(schedule, datetime.now())
                continue
            self.wake.wait(30)
            self.wake.clear()

    # --- accounts ---

    @staticmethod
    def _names(prefix: str, extra) -> list[str]:
        found = {os.path.basename(p)[len(prefix) + 1 : -5] for p in glob.glob(os.path.join(cfgmod.TOKEN_DIR, f"{prefix}_*.json"))}
        return sorted(n for n in found | set(extra) if NAME_RE.match(n))

    def accounts(self) -> dict:
        raw = cfgmod.read_raw()
        targets = raw.get("targets") or []
        od_used = {s.get("onedrive_account") for t in targets for s in (t.get("sources") or [])}
        g_used = {t.get("google_account", "default") for t in targets}
        secret = self._client_secret_path(raw)
        client_id = None
        try:
            with open(secret, encoding="utf-8") as f:
                data = json.load(f)
            client_id = (data.get("installed") or data.get("web") or {}).get("client_id")
        except (OSError, ValueError):
            pass
        onedrive = []
        for name in self._names("onedrive", od_used | self.onedrive_signins.keys()):
            signin = self.onedrive_signins.get(name)
            if signin and signin["state"] == "pending" and time.time() > signin["expires_at"]:
                signin["state"] = "error"
                signin["error"] = "The code expired before it was used."
            onedrive.append({
                "name": name,
                "user": cached_username(os.path.join(cfgmod.TOKEN_DIR, f"onedrive_{name}.json")),
                "used": name in od_used,
                "signin": signin,
            })
        google = [
            {
                "name": name,
                "signed_in": os.path.exists(os.path.join(cfgmod.TOKEN_DIR, f"google_{name}.json")),
                "used": name in g_used,
                "pending": name in self.google_flows,
            }
            for name in self._names("google", g_used | self.google_flows.keys())
        ]
        return {"onedrive": onedrive, "google": google, "google_client": {"present": client_id is not None, "client_id": client_id}}

    @staticmethod
    def _client_secret_path(raw: dict) -> str:
        secret = (raw.get("google") or {}).get("client_secret_file", "google_client_secret.json")
        return secret if os.path.isabs(secret) else os.path.join(cfgmod.CONFIG_DIR, secret)

    def onedrive_account(self, name: str) -> OneDriveAccount:
        cfg = cfgmod.load(require_targets=False)
        return OneDriveAccount(name, cfg.onedrive_client_id, cfg.onedrive_scopes, cfgmod.TOKEN_DIR)

    def onedrive_signin(self, name: str) -> dict:
        od = self.onedrive_account(name)
        flow = od.start_device_flow()
        entry = {"state": "pending", "code": flow["user_code"], "url": flow["verification_uri"], "expires_at": flow["expires_at"]}
        self.onedrive_signins[name] = entry

        def wait():
            try:
                entry["user"] = od.finish_device_flow(flow)
                entry["state"] = "done"
                log.info("OneDrive account '%s' signed in as %s", name, entry["user"])
            except Exception as e:
                entry["state"] = "error"
                entry["error"] = str(e)

        threading.Thread(target=wait, daemon=True).start()
        return entry

    def google_signin(self, name: str) -> str:
        cfg = cfgmod.load(require_targets=False)
        flow, url = GoogleAccount(name, cfg.google_client_secret_file, cfgmod.TOKEN_DIR).start_auth()
        self.google_flows[name] = flow
        return url

    def google_finish(self, name: str, pasted: str):
        flow = self.google_flows.get(name)
        if flow is None:
            raise ValueError("Start the Google sign-in again; this one is no longer pending.")
        cfg = cfgmod.load(require_targets=False)
        GoogleAccount(name, cfg.google_client_secret_file, cfgmod.TOKEN_DIR).finish_auth(flow, pasted)
        del self.google_flows[name]
        log.info("Google account '%s' signed in", name)

    def save_google_client(self, text: str):
        data = json.loads(text)
        if not isinstance(data, dict) or not (data.get("installed") or data.get("web") or {}).get("client_id"):
            raise ValueError("That isn't a Google OAuth client JSON file (no installed.client_id).")
        path = self._client_secret_path(cfgmod.read_raw())
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)

    def remove_account(self, kind: str, name: str):
        path = os.path.join(cfgmod.TOKEN_DIR, f"{kind}_{name}.json")
        if os.path.exists(path):
            os.remove(path)
        if kind == "onedrive":
            self.onedrive_signins.pop(name, None)
        else:
            self.google_flows.pop(name, None)
        log.info("%s account '%s' signed out", "OneDrive" if kind == "onedrive" else "Google", name)

    # --- status ---

    def target_status(self) -> list[dict]:
        try:
            cfg = cfgmod.load(require_targets=False)
        except cfgmod.ConfigError:
            return []
        if not os.path.exists(cfgmod.STATE_PATH):
            return [{"google_album": t.google_album, "synced": None} for t in cfg.targets]
        state = State(cfgmod.STATE_PATH)
        try:
            out = []
            for t in cfg.targets:
                album_id = state.album_id(t.google_account, t.google_album)
                out.append({"google_album": t.google_album, "synced": len(state.members(album_id)) if album_id else None})
            return out
        finally:
            state.close()

    def status(self) -> dict:
        return {
            "running": self.running,
            "next_run": iso(self.next_run),
            "last": self.last,
            "timezone": time.strftime("%Z"),
            "now": iso(datetime.now()),
            "targets": self.target_status(),
            "logs": list(self.logs.lines)[-400:],
        }


def clean_config(raw: dict) -> dict:
    """Drops blank fields the form sends, so the YAML stays tidy and validation is accurate."""
    def strip(d):
        return {k: v for k, v in d.items() if v not in (None, "", [])}

    out = {
        "schedule": {"enabled": bool((raw.get("schedule") or {}).get("enabled", True)),
                     "time": str((raw.get("schedule") or {}).get("time") or "03:00")},
        "include_videos": bool(raw.get("include_videos")),
        "allow_empty_source": bool(raw.get("allow_empty_source")),
        "onedrive": strip({"client_id": str((raw.get("onedrive") or {}).get("client_id") or "").strip(),
                           "scopes": [s for s in (raw.get("onedrive") or {}).get("scopes") or [] if s]}),
        "google": strip({"client_secret_file": (raw.get("google") or {}).get("client_secret_file")}),
        "targets": [],
    }
    for t in raw.get("targets") or []:
        sources = [strip({k: str(s.get(k) or "").strip() for k in ("onedrive_account", "album", "album_id")})
                   for s in t.get("sources") or []]
        out["targets"].append({
            "google_account": str(t.get("google_account") or "default").strip(),
            "google_album": str(t.get("google_album") or "").strip(),
            "sources": [s for s in sources if s],
        })
    return out


class Handler(BaseHTTPRequestHandler):
    service: Service
    password: str | None
    server_version = "odgsync"

    def log_message(self, fmt, *args):
        log.debug("%s %s", self.address_string(), fmt % args)

    def _send(self, status: int, body: bytes, ctype: str):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data, status: int = 200):
        self._send(status, json.dumps(data).encode(), "application/json")

    def _error(self, status: int, message: str):
        self._json({"error": message}, status)

    def _authorized(self) -> bool:
        if not self.password:
            return True
        header = self.headers.get("Authorization", "")
        if header.startswith("Basic "):
            try:
                _, _, pw = base64.b64decode(header[6:]).decode().partition(":")
            except ValueError:
                pw = ""
            if hmac.compare_digest(pw.encode(), self.password.encode()):
                return True
        self.send_response(HTTPStatus.UNAUTHORIZED)
        self.send_header("WWW-Authenticate", 'Basic realm="onedrive-google-sync"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length) or b"{}") if length else {}

    def do_GET(self):
        if not self._authorized():
            return
        url = urlparse(self.path)
        if url.path == "/":
            with open(INDEX, "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        if url.path == "/api/status":
            return self._json(self.service.status())
        if url.path == "/api/config":
            exists = os.path.exists(cfgmod.CONFIG_PATH)
            return self._json({"exists": exists, "config": clean_config(cfgmod.read_raw())})
        if url.path == "/api/accounts":
            return self._json(self.service.accounts())
        if url.path == "/api/onedrive/albums":
            name = (parse_qs(url.query).get("account") or [""])[0]
            if not NAME_RE.match(name):
                return self._error(400, "Invalid account name")
            if self.service.lock.locked():
                return self._error(409, "A sync is running; try again when it finishes.")
            try:
                albums = self.service.onedrive_account(name).albums()
            except Exception as e:
                return self._error(502, str(e))
            return self._json([{"name": a.get("name"), "id": a["id"], "count": (a.get("bundle") or {}).get("childCount")}
                               for a in albums])
        self._error(404, "Not found")

    def do_POST(self):
        if not self._authorized():
            return
        if self.headers.get(CSRF_HEADER) != "1":
            return self._error(403, "Missing request header")
        try:
            body = self._body()
        except ValueError:
            return self._error(400, "Body must be JSON")
        path = urlparse(self.path).path
        svc = self.service
        try:
            if path == "/api/sync":
                if not svc.sync_in_background("manual"):
                    return self._error(409, "A sync is already running.")
                return self._json({"started": True}, 202)
            if path == "/api/config":
                cfgmod.save_raw(clean_config(body))
                svc.wake.set()
                log.info("settings saved")
                return self._json({"saved": True})
            if path == "/api/google/client":
                svc.save_google_client(body.get("json") or "")
                log.info("Google OAuth client saved")
                return self._json({"saved": True})

            name = str(body.get("name") or "")
            if not NAME_RE.match(name):
                return self._error(400, "Account names may use letters, digits, '.', '_' and '-' (up to 40).")
            if path == "/api/onedrive/signin":
                return self._json(svc.onedrive_signin(name))
            if path == "/api/google/signin":
                return self._json({"url": svc.google_signin(name)})
            if path == "/api/google/finish":
                svc.google_finish(name, str(body.get("url") or ""))
                return self._json({"saved": True})
            if path in ("/api/onedrive/remove", "/api/google/remove"):
                svc.remove_account(path.split("/")[2], name)
                return self._json({"removed": True})
        except (cfgmod.ConfigError, ValueError, FileNotFoundError) as e:
            return self._error(400, str(e))
        except Exception as e:
            log.exception("request %s failed", path)
            return self._error(500, str(e))
        self._error(404, "Not found")


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        # Browsers drop idle keep-alive connections all the time; that's not worth a traceback.
        log.debug("connection from %s failed", client_address, exc_info=True)


def serve(port: int, password: str | None) -> Service:
    """Starts the scheduler and web server in background threads and returns the service."""
    service = Service()
    logging.getLogger().addHandler(service.logs)
    handler = type("BoundHandler", (Handler,), {"service": service, "password": password})
    server = Server(("0.0.0.0", port), handler)
    threading.Thread(target=service.scheduler, daemon=True).start()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    service.server = server
    log.info("web UI listening on port %d%s", port, "" if password else " (no password set)")
    return service
