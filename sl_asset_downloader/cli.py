"""Command line interface."""

from __future__ import annotations

import argparse
import contextlib
import getpass
import logging
import os
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Iterator

from . import __version__, assets
from .downloader import Downloader, may_export
from .http import HttpClient, HttpError
from .inventory import AT_LINK, FOLDER_SHORTCUTS, Inventory
from .login import GRIDS, LoginError, Session, login
from .udp import Circuit, TransferManager

log = logging.getLogger("sl_asset_downloader")

CAPS = ["FetchInventoryDescendents2", "FetchInventory2", "ViewerAsset", "GetTexture"]
DEFAULT_FOLDERS = ["@photos", "@textures"]
DEFAULT_TYPES = ["texture"]


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    acct = common.add_argument_group("account")
    acct.add_argument("-u", "--username", default=os.environ.get("SL_USERNAME"),
                      help='account name, e.g. "jane.doe" or "Jane Doe" or just "janedoe" '
                           "(or set SL_USERNAME). The password is read from SL_PASSWORD or prompted for.")
    acct.add_argument("--grid", default="agni",
                      help="'agni' (main grid, default), 'aditi' (beta grid) or a login URI")
    acct.add_argument("--start", default="last",
                      help="where the avatar logs in: 'last' (default), 'home', or a region URI")
    acct.add_argument("--agree-to-tos", action="store_true",
                      help="accept updated Terms of Service if the login server asks")
    net = common.add_argument_group("network")
    net.add_argument("--ca-bundle", help="CA certificate bundle for HTTPS verification")
    net.add_argument("--insecure", action="store_true",
                     help="do not verify HTTPS certificates (not recommended)")
    common.add_argument("-v", "--verbose", action="count", default=0, help="more logging (-vv for debug)")

    parser = argparse.ArgumentParser(
        prog="sl-asset-downloader",
        description="Download textures, photos, sounds, notecards and more from your Second Life inventory.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("folders", parents=[common], help="show the inventory folder tree",
                   description="Log in and print the inventory folder tree, to find folder names for --folder.")

    dl = sub.add_parser(
        "download", parents=[common], help="download assets from inventory folders",
        description="Log in and download assets from the given inventory folders. Only items you "
                    "created, or have full (copy/modify/transfer) permissions on, are downloaded.")
    dl.add_argument("-f", "--folder", action="append", dest="folders", metavar="FOLDER",
                    help="inventory folder to download from; repeatable. A path like 'Textures/Skins', "
                         "a unique folder name, a folder UUID, or a shortcut: "
                         + ", ".join("@" + k for k in FOLDER_SHORTCUTS)
                         + f". Default: {' '.join(DEFAULT_FOLDERS)}")
    dl.add_argument("-t", "--type", action="append", dest="types", metavar="TYPE",
                    choices=[*assets.KINDS, "all"],
                    help=f"asset type to download; repeatable. One of: {', '.join(assets.KINDS)}, all. "
                         f"Default: {' '.join(DEFAULT_TYPES)}")
    dl.add_argument("-o", "--output", type=Path, default=Path("sl-downloads"),
                    help="output directory (default: ./sl-downloads)")
    dl.add_argument("--format", choices=list(assets.IMAGE_FORMATS), default="png",
                    help="image format for textures and photos (default: png; j2c = raw JPEG2000)")
    dl.add_argument("--no-recursive", dest="recursive", action="store_false",
                    help="do not descend into subfolders")
    dl.add_argument("--no-links", dest="follow_links", action="store_false",
                    help="ignore inventory links instead of downloading what they point to")
    dl.add_argument("--workers", type=int, default=4, help="parallel downloads (default: 4)")
    dl.add_argument("--overwrite", action="store_true",
                    help="download again even if a file was already downloaded")
    dl.add_argument("--dry-run", action="store_true",
                    help="only list what would be downloaded")
    return parser


def _login_uri(grid: str) -> str:
    return GRIDS.get(grid.lower(), grid)


@contextlib.contextmanager
def connected(args) -> Iterator[tuple[Session, HttpClient, dict[str, str], Circuit]]:
    if not args.username:
        raise SystemExit("error: --username is required (or set SL_USERNAME)")
    password = os.environ.get("SL_PASSWORD") or getpass.getpass(f"Password for {args.username}: ")
    http = HttpClient(ca_bundle=args.ca_bundle, insecure=args.insecure)

    def mfa_prompt(message: str) -> str:
        print(message, file=sys.stderr)
        return input("MFA token: ")

    uri = _login_uri(args.grid)
    print(f"Logging in to {uri} ...", file=sys.stderr)
    session = login(http, uri, args.username, password, start=args.start,
                    agree_to_tos=args.agree_to_tos, mfa_prompt=mfa_prompt)
    print(f"Logged in as {session.display_name}; connecting to region ...", file=sys.stderr)

    circuit = Circuit(session)
    try:
        circuit.connect()
        caps = _request_caps(http, session.seed_capability)
        yield session, http, caps, circuit
    finally:
        print("Logging out ...", file=sys.stderr)
        circuit.logout()


def _request_caps(http: HttpClient, seed_url: str) -> dict[str, str]:
    last_error: Exception | None = None
    for attempt in range(6):
        try:
            caps = http.post_llsd(seed_url, CAPS) or {}
            if caps:
                log.debug("capabilities: %s", ", ".join(sorted(caps)))
                return caps
        except HttpError as e:
            last_error = e
        time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"could not get capabilities from the region ({last_error})")


def cmd_folders(args) -> int:
    shortcut_for = {}
    for name, ftype in FOLDER_SHORTCUTS.items():
        shortcut_for.setdefault(ftype, "@" + name)
    with connected(args) as (session, http, caps, _circuit):
        inv = Inventory(session, http, caps)

        def walk(folder_id, depth):
            for f in inv.children(folder_id):
                hint = shortcut_for.get(f.type_default) if f.parent_id == inv.root_id else None
                print("  " * depth + f.name + (f"   ({hint})" if hint else ""))
                walk(f.folder_id, depth + 1)

        print(inv.folders[inv.root_id].name if inv.root_id in inv.folders else "My Inventory")
        walk(inv.root_id, 1)
    return 0


def cmd_download(args) -> int:
    folder_specs = args.folders or DEFAULT_FOLDERS
    type_names = args.types or DEFAULT_TYPES
    kinds = list(assets.KINDS.values()) if "all" in type_names else [assets.KINDS[t] for t in type_names]
    wanted_types = {k.asset_type for k in kinds}

    with connected(args) as (session, http, caps, circuit):
        inv = Inventory(session, http, caps)
        roots = []
        for spec in folder_specs:
            try:
                folder = inv.resolve(spec)
            except LookupError as e:
                print(f"error: {e}", file=sys.stderr)
                return 2
            print(f"Folder: {'/'.join(inv.path_of(folder.folder_id)) or folder.name}", file=sys.stderr)
            roots.append(folder.folder_id)

        print("Reading inventory ...", file=sys.stderr)
        items = inv.fetch_items(roots, recursive=args.recursive)
        links = [i for i in items if i.asset_type == AT_LINK]
        items = [i for i in items if i.asset_type != AT_LINK]
        if args.follow_links and links:
            have = {i.item_id for i in items}
            items += [t for t in inv.resolve_links(links) if t.item_id not in have]

        selected = [i for i in items if i.asset_type in wanted_types]
        allowed = [i for i in selected if may_export(i, session.agent_id)]
        not_allowed = len(selected) - len(allowed)
        counts = Counter(assets.KINDS_BY_TYPE[i.asset_type].name for i in allowed)
        summary = ", ".join(f"{n} {k}{'s' if n != 1 else ''}" for k, n in counts.items()) or "nothing"
        print(f"Found {summary} to download.", file=sys.stderr)
        if not_allowed:
            print(f"Skipping {not_allowed} item(s) you did not create and do not have full "
                  "(copy/modify/transfer) permissions on.", file=sys.stderr)

        transfers = TransferManager(circuit) if any(not k.via_http for k in kinds) else None
        dl = Downloader(inv, http, caps, transfers, args.output, image_format=args.format,
                        workers=args.workers, overwrite=args.overwrite)
        jobs = dl.plan(allowed)
        if args.dry_run:
            for job in jobs:
                print((job.rel_dir / job.stem).as_posix())
            return 0
        result = dl.run(jobs)

    print(f"Done: {result.downloaded} downloaded, {result.skipped_existing} already present, "
          f"{result.failed} failed. Files are in {args.output.resolve()}", file=sys.stderr)
    return 1 if result.failed else 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    level = logging.WARNING - 10 * min(args.verbose, 2)
    logging.basicConfig(level=level, format="%(levelname)s %(name)s: %(message)s")
    try:
        if args.command == "folders":
            return cmd_folders(args)
        return cmd_download(args)
    except LoginError as e:
        print(f"Login failed: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except (RuntimeError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
