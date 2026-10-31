# onedrive-google-sync

A small container that brings Google Photos albums in step with OneDrive albums, for
example to feed a Nest Hub photo frame from photos you curate in OneDrive. It syncs once a
day at a time you choose, and has a web UI for settings, account sign-in, and a **Sync now** button.

- **One-way**: OneDrive → Google. Nothing in OneDrive is ever modified.
- **Multiple targets**: each target is one Google Photos album.
- **Multiple sources per target**: a target's contents are the union of one or more
  OneDrive albums, from any number of Microsoft accounts.
- Photos added to a OneDrive album are uploaded; photos removed from it are taken off
  the Google album. Videos are skipped unless `include_videos: true`.

## How it works

The container runs all the time, serving the web UI on port 8765. Each sync (daily, or when you
click **Sync now**):

1. Lists each source album through Microsoft Graph (OneDrive albums are *bundles*
   in Graph; they reference your existing files, so nothing is copied in OneDrive).
2. Makes sure the Google album exists (creating it on first run).
3. Compares the result with `/config/state.db` and:
   - uploads new photos (`/v1/uploads` then `mediaItems:batchCreate`, 50 per call);
   - re-adds photos it uploaded before, without uploading them again;
   - removes photos that left every source album (`batchRemoveMediaItems`).
4. Records whether every target synced; the result and log show in the web UI.

If a source can't be read, that target is skipped for the run. If a source album
comes back empty, removals are skipped too (set `allow_empty_source: true` to allow
it). That stops a Graph glitch from emptying your photo frame.

### Google Photos limits

- Since March 2025 the Library API only manages albums and photos the app created
  itself, so the tool always creates its own Google album. You can't point it at an
  album you made by hand.
- The API can't delete photos from your library. Removed photos come off the album
  (and the frame) but stay in Google Photos and keep using storage.
- Uploads count against your Google storage.

## One-time setup

### 1. Microsoft (Entra ID) app

1. Go to <https://entra.microsoft.com> → **App registrations** → **New registration**.
2. **Supported account types**: *Personal Microsoft accounts only* (or *any org
   directory and personal accounts*).
3. No redirect URI is needed. After creating the app, open **Authentication** and set
   **Allow public client flows** to **Yes** (required for device-code sign-in).
4. **API permissions** → add Microsoft Graph delegated **Files.Read** (use
   **Files.Read.All** if your albums include photos other people shared with you, and set
   `onedrive.scopes` to match).
5. Copy the **Application (client) ID** into `onedrive.client_id`.

The same app serves every OneDrive account. Each account just signs in separately.

### 2. Google Cloud OAuth client

1. In <https://console.cloud.google.com>, create a project and enable the
   **Photos Library API**.
2. **OAuth consent screen**: External. Add the three scopes below, then **publish the
   app to "In production"**. If you leave it in *Testing*, refresh tokens expire after
   7 days and sync stops. An unverified app is fine for personal use; you'll click through
   an "unverified app" warning once.
   - `.../auth/photoslibrary.appendonly`
   - `.../auth/photoslibrary.readonly.appcreateddata`
   - `.../auth/photoslibrary.edit.appcreateddata`
3. **Credentials** → **Create credentials** → **OAuth client ID** → **Desktop app**.
   Download the JSON and save it as `/config/google_client_secret.json`.

### 3. Install on Unraid

The GitHub workflow builds the image to `ghcr.io/jamespwright/onedrive-google-sync` on
every push to `main`. Then either:

- copy `unraid/onedrive-google-sync.xml` to `/boot/config/plugins/dockerMan/templates-user/`
  and add the container from **Docker → Add Container → Template**, or
- use `docker-compose.yml` with the Compose Manager plugin.

Config lives in `/mnt/user/appdata/onedrive-google-sync`. The container runs as
`99:100` (nobody:users), so that folder should be owned by them.

### 4. Configure in the web UI

Open `http://<server>:8765/` (the **WebUI** item on the container's icon in Unraid):

1. **Options**: paste the Microsoft app's client ID and click **Save settings**.
2. **Google accounts → Google OAuth client**: paste or pick the client JSON and save it.
3. **OneDrive accounts**: enter a name (e.g. `james`) and click **Sign in**. Open the link
   shown and enter the code while signed in as that person. Repeat for each account.
4. **Google accounts**: enter a name (e.g. `default`) and click **Sign in**. Approve access,
   then copy the `http://localhost:8080/?…code=…` address your browser fails to load and
   paste it into the box.
5. **Albums**: add a Google album, pick its OneDrive account(s) and album(s) (the album box
   lists that account's albums), and save.
6. **Schedule**: pick the daily time and save. It's in the container's time zone, which
   Unraid sets from **Settings → Date and Time**; the page shows which zone that is.
7. Click **Sync now** to check it all works.

Set **Web UI password** in the container settings (`WEB_PASSWORD`) if others on your network
shouldn't reach the page: it can sign accounts in and out. Any username works.

## Command line

Everything in the UI can also be done from the terminal, and `config.yaml` can be edited by
hand (the scheduler re-reads it within 30 seconds; saving from the UI drops its comments).

```sh
docker exec -it onedrive-google-sync python -m odgsync auth-onedrive james
```

| Command | Purpose |
| --- | --- |
| `serve` (default) | Web UI plus the daily scheduled sync |
| `sync` | Sync once and exit (non-zero exit if any target failed) |
| `auth-onedrive NAME` | Device-code sign-in for a OneDrive account |
| `auth-google NAME` | Sign in a Google account |
| `list-albums NAME` | List a OneDrive account's albums with their IDs |

| Variable | Default | Purpose |
| --- | --- | --- |
| `WEB_PORT` | `8765` | Web UI port inside the container |
| `WEB_PASSWORD` | (none) | Password for the web UI |
| `TZ` | UTC | Time zone of the daily sync time |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING` |

## Files in /config

| Path | Contents |
| --- | --- |
| `config.yaml` | Your configuration |
| `google_client_secret.json` | Google OAuth client |
| `tokens/onedrive_NAME.json` | MSAL token cache per OneDrive account |
| `tokens/google_NAME.json` | Google refresh token per account |
| `state.db` | SQLite: Google album IDs, uploaded media IDs, album membership |
| `status.json` | Result of the last sync, shown in the web UI |

Token files are secrets; keep the appdata folder private. Deleting `state.db` makes
the tool create a fresh Google album and upload everything again.

## Notes

- Editing a photo in OneDrive doesn't re-upload it. Photos are tracked by OneDrive item
  ID. To push an edit, take the photo out of the album, wait for a sync, then add it back.
- If you delete the Google album by hand, the next sync creates a new one, and you'll
  need to reselect it on the Nest Hub.
- Renaming a Google album in the settings also creates a new album, because albums are
  matched by account and title.
