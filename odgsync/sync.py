import logging

from . import config as cfgmod
from .config import Config, Target
from .gphotos import BATCH_LIMIT, GoogleAccount
from .http import AuthError
from .onedrive import OneDriveAccount, Photo
from .state import State

log = logging.getLogger(__name__)


def chunks(seq: list, size: int = BATCH_LIMIT):
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


class Syncer:
    def __init__(self, cfg: Config, state: State):
        self.cfg = cfg
        self.state = state
        self._onedrive: dict[str, OneDriveAccount] = {}
        self._google: dict[str, GoogleAccount] = {}

    def onedrive(self, name: str) -> OneDriveAccount:
        if name not in self._onedrive:
            self._onedrive[name] = OneDriveAccount(
                name, self.cfg.onedrive_client_id, self.cfg.onedrive_scopes, cfgmod.TOKEN_DIR
            )
        return self._onedrive[name]

    def google(self, name: str) -> GoogleAccount:
        if name not in self._google:
            self._google[name] = GoogleAccount(name, self.cfg.google_client_secret_file, cfgmod.TOKEN_DIR)
        return self._google[name]

    def run(self) -> bool:
        ok = True
        for target in self.cfg.targets:
            try:
                self.sync_target(target)
            except AuthError as e:
                log.error("[%s] %s", target.google_album, e)
                ok = False
            except Exception:
                log.exception("[%s] sync failed", target.google_album)
                ok = False
        return ok

    def ensure_album(self, google: GoogleAccount, title: str) -> str:
        album_id = self.state.album_id(google.name, title)
        if album_id and google.album_exists(album_id):
            return album_id
        if album_id:
            log.warning("[%s] Google album %s is gone; creating a new one", title, album_id)
            self.state.clear_members(album_id)
        album_id = google.create_album(title)
        self.state.set_album(google.name, title, album_id)
        log.info("[%s] created Google album %s", title, album_id)
        return album_id

    def sync_target(self, target: Target):
        title = target.google_album
        google = self.google(target.google_account)

        # Gather everything first: if any source fails we abort before touching Google,
        # rather than treating the missing photos as removals.
        desired: dict[str, tuple[OneDriveAccount, Photo]] = {}
        empty_sources = []
        for src in target.sources:
            od = self.onedrive(src.onedrive_account)
            photos = od.album_photos(od.resolve_album(src.album, src.album_id), self.cfg.include_videos)
            if not photos:
                empty_sources.append(src.label)
            for p in photos:
                desired[p.key] = (od, p)

        album_id = self.ensure_album(google, title)
        current = self.state.members(album_id)
        to_remove = sorted(current - desired.keys())
        to_add = [k for k in desired if k not in current]
        log.info("[%s] %d in OneDrive, %d synced, %d to add, %d to remove",
                 title, len(desired), len(current), len(to_add), len(to_remove))

        if to_remove and empty_sources and not self.cfg.allow_empty_source:
            log.warning("[%s] source(s) %s returned no photos; skipping %d removals this run "
                        "(set allow_empty_source: true if that is intended)",
                        title, ", ".join(empty_sources), len(to_remove))
        elif to_remove:
            self._remove(google, album_id, to_remove)

        if to_add:
            self._add(google, album_id, to_add, desired)

    def _remove(self, google: GoogleAccount, album_id: str, keys: list[str]):
        acct = google.name
        with_media = [(k, self.state.media_id(acct, k)) for k in keys]
        for batch in chunks([km for km in with_media if km[1]]):
            try:
                google.remove_from_album(album_id, [m for _, m in batch])
            except Exception as e:
                # The batch call is all-or-nothing; one item already removed by hand fails it.
                log.debug("batch remove failed (%s); retrying one by one", e)
                for k, m in batch:
                    try:
                        google.remove_from_album(album_id, [m])
                    except Exception as e1:
                        log.warning("could not remove %s from album: %s", k, e1)
        for k, _ in with_media:
            self.state.remove_member(album_id, k)
        log.info("removed %d item(s) from Google album", len(keys))

    def _add(self, google: GoogleAccount, album_id: str, keys: list[str], desired):
        acct = google.name
        reuse = [(k, m) for k in keys if (m := self.state.media_id(acct, k))]
        upload = [k for k in keys if not self.state.media_id(acct, k)]
        added = 0

        for batch in chunks(reuse):
            try:
                google.add_to_album(album_id, [m for _, m in batch])
                for k, _ in batch:
                    self.state.add_member(album_id, k)
                added += len(batch)
            except Exception:
                for k, m in batch:
                    try:
                        google.add_to_album(album_id, [m])
                        self.state.add_member(album_id, k)
                        added += 1
                    except Exception:
                        # Most likely deleted from the Google library; upload it again.
                        self.state.delete_media(acct, k)
                        upload.append(k)

        pending: list[tuple[str, str, str]] = []  # (source_key, upload_token, filename)

        def flush():
            nonlocal added
            if not pending:
                return
            results = google.batch_create(album_id, [(t, n) for _, t, n in pending])
            for k, t, n in pending:
                media_id = results.get(t)
                if media_id:
                    self.state.set_media(acct, k, media_id)
                    self.state.add_member(album_id, k)
                    added += 1
                else:
                    log.warning("failed to create %s in Google Photos", n)
            pending.clear()

        for k in upload:
            od, photo = desired[k]
            try:
                token = google.upload(od.download(photo), photo.mime)
            except AuthError:
                raise
            except Exception as e:
                log.warning("skipping %s: %s", photo.name, e)
                continue
            pending.append((k, token, photo.name))
            if len(pending) >= BATCH_LIMIT:
                flush()
        flush()
        log.info("added %d of %d item(s) to Google album", added, len(keys))
