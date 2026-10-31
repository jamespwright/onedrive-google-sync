import sqlite3

# albums:  the Google album we created for each (account, title)
# media:   Google media items we uploaded, so a photo that leaves and later rejoins an
#          album is re-added rather than uploaded again
# members: which source photos we have put into each Google album
SCHEMA = """
CREATE TABLE IF NOT EXISTS albums (
    google_account TEXT NOT NULL,
    title TEXT NOT NULL,
    album_id TEXT NOT NULL,
    PRIMARY KEY (google_account, title)
);
CREATE TABLE IF NOT EXISTS media (
    google_account TEXT NOT NULL,
    source_key TEXT NOT NULL,
    media_item_id TEXT NOT NULL,
    PRIMARY KEY (google_account, source_key)
);
CREATE TABLE IF NOT EXISTS members (
    album_id TEXT NOT NULL,
    source_key TEXT NOT NULL,
    PRIMARY KEY (album_id, source_key)
);
"""


class State:
    def __init__(self, path: str):
        # Autocommit: every change is durable immediately, so a crash mid-run never
        # forgets an upload that already happened.
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.executescript(SCHEMA)

    def close(self):
        self.db.close()

    def album_id(self, account: str, title: str) -> str | None:
        row = self.db.execute(
            "SELECT album_id FROM albums WHERE google_account=? AND title=?", (account, title)
        ).fetchone()
        return row[0] if row else None

    def set_album(self, account: str, title: str, album_id: str):
        self.db.execute(
            "INSERT OR REPLACE INTO albums VALUES (?, ?, ?)", (account, title, album_id)
        )

    def media_id(self, account: str, key: str) -> str | None:
        row = self.db.execute(
            "SELECT media_item_id FROM media WHERE google_account=? AND source_key=?", (account, key)
        ).fetchone()
        return row[0] if row else None

    def set_media(self, account: str, key: str, media_item_id: str):
        self.db.execute("INSERT OR REPLACE INTO media VALUES (?, ?, ?)", (account, key, media_item_id))

    def delete_media(self, account: str, key: str):
        self.db.execute("DELETE FROM media WHERE google_account=? AND source_key=?", (account, key))

    def members(self, album_id: str) -> set[str]:
        rows = self.db.execute("SELECT source_key FROM members WHERE album_id=?", (album_id,))
        return {r[0] for r in rows}

    def add_member(self, album_id: str, key: str):
        self.db.execute("INSERT OR IGNORE INTO members VALUES (?, ?)", (album_id, key))

    def remove_member(self, album_id: str, key: str):
        self.db.execute("DELETE FROM members WHERE album_id=? AND source_key=?", (album_id, key))

    def clear_members(self, album_id: str):
        self.db.execute("DELETE FROM members WHERE album_id=?", (album_id,))
