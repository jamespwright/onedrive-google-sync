import argparse
import logging
import os
import shutil
import signal
import sys

from . import config as cfgmod
from .gphotos import GoogleAccount
from .onedrive import OneDriveAccount
from .state import State
from .sync import Syncer

log = logging.getLogger("odgsync")
EXAMPLE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.example.yaml")


def load_config(require_targets: bool = True) -> cfgmod.Config:
    try:
        return cfgmod.load(require_targets=require_targets)
    except cfgmod.ConfigError as e:
        if not os.path.exists(cfgmod.CONFIG_PATH) and os.path.exists(EXAMPLE):
            dest = os.path.join(cfgmod.CONFIG_DIR, "config.example.yaml")
            shutil.copyfile(EXAMPLE, dest)
            log.error("%s An example has been written to %s", e, dest)
        else:
            log.error("%s", e)
        sys.exit(2)


def sync_once(cfg: cfgmod.Config) -> bool:
    state = State(cfgmod.STATE_PATH)
    try:
        return Syncer(cfg, state).run()
    finally:
        state.close()


def cmd_serve(_args):
    from .web import serve

    service = serve(int(os.environ.get("WEB_PORT", "8765")), os.environ.get("WEB_PASSWORD") or None)
    # Python as PID 1 ignores SIGTERM unless it has a handler, which makes `docker stop` hang.
    signal.signal(signal.SIGTERM, lambda *_: service.stop.set())
    signal.signal(signal.SIGINT, lambda *_: service.stop.set())
    service.stop.wait()
    service.wake.set()
    service.server.shutdown()
    log.info("stopping")


def cmd_sync(_args):
    sys.exit(0 if sync_once(load_config()) else 1)


def cmd_auth_onedrive(args):
    cfg = load_config(require_targets=False)
    user = OneDriveAccount(args.account, cfg.onedrive_client_id, cfg.onedrive_scopes, cfgmod.TOKEN_DIR).authenticate_interactive()
    print(f"\nOneDrive account '{args.account}' signed in as {user}.")


def cmd_auth_google(args):
    cfg = load_config(require_targets=False)
    GoogleAccount(args.account, cfg.google_client_secret_file, cfgmod.TOKEN_DIR).authenticate_interactive()
    print(f"\nGoogle account '{args.account}' signed in.")


def cmd_list_albums(args):
    cfg = load_config(require_targets=False)
    od = OneDriveAccount(args.account, cfg.onedrive_client_id, cfg.onedrive_scopes, cfgmod.TOKEN_DIR)
    albums = od.albums()
    if not albums:
        print("No albums found. (Albums are only exposed as bundles on personal OneDrive.)")
    for a in albums:
        count = (a.get("bundle") or {}).get("childCount", "?")
        print(f"{a.get('name')!r:40} {count:>5} items   id={a['id']}")


def main():
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    os.makedirs(cfgmod.TOKEN_DIR, exist_ok=True)

    parser = argparse.ArgumentParser(prog="odgsync", description="Sync OneDrive albums to Google Photos albums")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("serve", help="web UI plus daily scheduled sync (default)").set_defaults(func=cmd_serve)
    sub.add_parser("sync", help="sync once and exit").set_defaults(func=cmd_sync)
    for name, func, help_ in (
        ("auth-onedrive", cmd_auth_onedrive, "sign in a OneDrive account (device code)"),
        ("auth-google", cmd_auth_google, "sign in a Google account"),
        ("list-albums", cmd_list_albums, "list a OneDrive account's albums"),
    ):
        p = sub.add_parser(name, help=help_)
        p.add_argument("account", help="account name as used in config.yaml")
        p.set_defaults(func=func)

    args = parser.parse_args()
    (getattr(args, "func", None) or cmd_serve)(args)


if __name__ == "__main__":
    main()
