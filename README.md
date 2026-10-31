# onedrive-google-sync

A small run-once container that brings Google Photos albums in step with OneDrive albums, for
example to feed a Nest Hub photo frame from photos you curate in OneDrive.

- **One-way**: OneDrive → Google. Nothing in OneDrive is ever modified.
- **Multiple targets**: each target is one Google Photos album.
- **Multiple sources per target**: a target's contents are the union of one or more
  OneDrive albums, from any number of Microsoft accounts.
- Photos added to a OneDrive album are uploaded; photos removed from it are taken off
  the Google album. Videos are skipped unless `include_videos: true`.

## How it works

Each time the container starts it does one sync, then exits:

1. Lists each source album through Microsoft Graph (OneDrive albums are *bundles*
   in Graph; they reference your existing files, so nothing is copied in OneDrive).
2. Makes sure the Google album exists (creating it on first run).
3. Compares the result with `/config/state.db` and:
   - uploads new photos (`/v1/uploads` then `mediaItems:batchCreate`, 50 per call);
   - re-adds photos it uploaded before, without uploading them again;
   - removes photos that left every source album (`batchRemoveMediaItems`).
4. Exits with code 0 if every target synced, or 1 if any failed.

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
every push to `main`. Make the package public in GitHub (or `docker login ghcr.io` on
Unraid). Then either:

- copy `unraid/onedrive-google-sync.xml` to `/boot/config/plugins/dockerMan/templates-user/`
  and add the container from **Docker → Add Container → Template**, or
- use `docker-compose.yml` with the Compose Manager plugin.

Or build it on the server and create the container directly. If you do, use
`onedrive-google-sync:latest` in place of the ghcr image name below:

```sh
git clone https://github.com/jamespwright/onedrive-google-sync /mnt/user/appdata/onedrive-google-sync-src
docker build -t onedrive-google-sync:latest /mnt/user/appdata/onedrive-google-sync-src
docker create --name onedrive-google-sync --user 99:100 --restart no \
  -v /mnt/user/appdata/onedrive-google-sync:/config \
  -v /etc/localtime:/etc/localtime:ro \
  onedrive-google-sync:latest
```

Config lives in `/mnt/user/appdata/onedrive-google-sync`. The container runs as
`99:100` (nobody:users), so that folder should be owned by them.

Because the container exits after each sync, the commands below start a temporary
container for each step. For brevity, `ODGS` stands for:

```sh
ODGS="docker run --rm -it --user 99:100 -v /mnt/user/appdata/onedrive-google-sync:/config ghcr.io/jamespwright/onedrive-google-sync"
```

### 4. Configure and sign in

1. Copy `config.example.yaml` to `/mnt/user/appdata/onedrive-google-sync/config.yaml`.
   Fill in `onedrive.client_id` and leave the targets for now. (If you start the
   container with no config, it writes the example there for you.)
2. Sign in each OneDrive account. Use the names you'll reference in `config.yaml`:

   ```sh
   $ODGS auth-onedrive james
   $ODGS auth-onedrive partner
   ```

   Each one prints a code. Enter it at <https://microsoft.com/devicelogin> while signed
   in as that person.
3. **Check that your albums are visible to Graph.** It's unconfirmed whether albums made in
   the newer OneDrive Photos experience still show up as bundles, so do this before going
   further:

   ```sh
   $ODGS list-albums james
   ```

4. Sign in Google:

   ```sh
   $ODGS auth-google default
   ```

   Open the printed URL, approve access, then copy the `http://localhost:8080/?code=…`
   URL your browser fails to load and paste it back into the terminal.
5. Add your `targets` to `config.yaml`, then run one sync to check it:

   ```sh
   docker start -a onedrive-google-sync
   ```

## Running a sync

Start the container whenever you want to sync; it exits when done. From your existing
bash script (e.g. after your OneDrive sync step):

```sh
if docker start -a onedrive-google-sync; then
  echo "Google Photos sync OK"
else
  echo "Google Photos sync failed (see: docker logs onedrive-google-sync)" >&2
fi
```

`-a` attaches to the output and waits, so the script blocks until the sync finishes and
gets its exit code. Make sure the container's **Autostart** is off in Unraid if you only
want it to run from your script (or leave it on to also sync at every array start).

## Commands

| Command | Purpose |
| --- | --- |
| `sync` (default) | Sync once and exit (non-zero exit if any target failed) |
| `run` | Keep syncing on a loop every `interval_minutes` |
| `auth-onedrive NAME` | Device-code sign-in for a OneDrive account |
| `auth-google NAME` | Sign in a Google account |
| `list-albums NAME` | List a OneDrive account's albums with their IDs |

## Files in /config

| Path | Contents |
| --- | --- |
| `config.yaml` | Your configuration |
| `google_client_secret.json` | Google OAuth client |
| `tokens/onedrive_NAME.json` | MSAL token cache per OneDrive account |
| `tokens/google_NAME.json` | Google refresh token per account |
| `state.db` | SQLite: Google album IDs, uploaded media IDs, album membership |

Token files are secrets; keep the appdata folder private. Deleting `state.db` makes
the tool create a fresh Google album and upload everything again.

## Notes

- Editing a photo in OneDrive doesn't re-upload it. Photos are tracked by OneDrive item
  ID. To push an edit, take the photo out of the album, wait for a sync, then add it back.
- If you delete the Google album by hand, the next sync creates a new one, and you'll
  need to reselect it on the Nest Hub.
- Renaming `google_album` in the config also creates a new album, because albums are
  matched by account and title.
