"""Plans and runs the downloads for a set of inventory items."""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from . import assets
from .assets import AssetKind
from .http import HttpClient
from .inventory import Inventory, Item
from .udp import TransferError, TransferManager

log = logging.getLogger(__name__)

MANIFEST_NAME = ".sl-asset-downloader.json"
_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
             *(f"LPT{i}" for i in range(1, 10))}


def sanitize(name: str) -> str:
    name = _BAD_CHARS.sub("_", name).strip().rstrip(". ")
    if not name:
        name = "unnamed"
    if name.split(".")[0].upper() in _RESERVED:
        name = "_" + name
    return name[:120]


def may_export(item: Item, agent_id: uuid.UUID) -> bool:
    """Same rule the viewers use for "Save As": you created it, or you have
    full (copy/modify/transfer) permissions on it."""
    return item.creator_id == agent_id or item.is_full_perm


@dataclass
class Job:
    item: Item
    kind: AssetKind
    rel_dir: Path
    stem: str


@dataclass
class Result:
    downloaded: int = 0
    skipped_existing: int = 0
    failed: int = 0


class Downloader:
    def __init__(self, inventory: Inventory, http: HttpClient, caps: dict[str, str],
                 transfers: TransferManager | None, out_dir: Path, image_format: str = "png",
                 workers: int = 4, overwrite: bool = False):
        self.inventory = inventory
        self.http = http
        self.caps = caps
        self.transfers = transfers
        self.out_dir = out_dir
        self.image_format = image_format
        self.workers = max(1, workers)
        self.overwrite = overwrite
        self.manifest_path = out_dir / MANIFEST_NAME
        self.manifest: dict[str, dict] = self._load_manifest()
        self._manifest_lock = threading.Lock()
        self._transfer_slots = threading.Semaphore(2)
        self._print_lock = threading.Lock()

    # -- planning ---------------------------------------------------------

    def plan(self, items: list[Item]) -> list[Job]:
        jobs = []
        for item in items:
            kind = assets.KINDS_BY_TYPE[item.asset_type]
            rel_dir = Path(*[sanitize(p) for p in self.inventory.path_of(item.parent_id)])
            jobs.append(Job(item, kind, rel_dir, sanitize(item.name)))
        jobs.sort(key=lambda j: (str(j.rel_dir).lower(), j.stem.lower(), str(j.item.item_id)))
        # Make file names unique per directory; same-named items get an id suffix.
        used: dict[Path, set[str]] = {}
        for job in jobs:
            names = used.setdefault(job.rel_dir, set())
            if job.stem.lower() in names:
                job.stem = f"{job.stem} [{str(job.item.item_id)[:8]}]"
            names.add(job.stem.lower())
        return jobs

    # -- running ----------------------------------------------------------

    def run(self, jobs: list[Job]) -> Result:
        result = Result()
        try:
            self._run_all(jobs, result)
        finally:
            self._save_manifest()
        return result

    def _run_all(self, jobs: list[Job], result: Result) -> None:
        total = len(jobs)
        done = 0
        pool = ThreadPoolExecutor(max_workers=self.workers)
        try:
            futures = {pool.submit(self._run_job, job): job for job in jobs}
            for fut in as_completed(futures):
                job = futures[fut]
                done += 1
                label = str(job.rel_dir / job.stem)
                try:
                    status = fut.result()
                except Exception as e:  # noqa: BLE001 - one bad item must not stop the rest
                    result.failed += 1
                    self._print(f"[{done}/{total}] FAILED {label}: {e}")
                    log.debug("failure details", exc_info=True)
                    continue
                if status == "exists":
                    result.skipped_existing += 1
                    log.info("[%d/%d] already downloaded: %s", done, total, label)
                else:
                    result.downloaded += 1
                    self._print(f"[{done}/{total}] {status}")
        finally:
            # On Ctrl-C, drop queued jobs instead of finishing them all.
            pool.shutdown(wait=True, cancel_futures=True)

    def _run_job(self, job: Job) -> str:
        item = job.item
        key = str(item.item_id)
        prev = self.manifest.get(key)
        if (not self.overwrite and prev and prev.get("asset_id") == str(item.asset_id)
                and (self.out_dir / prev["file"]).exists()):
            return "exists"

        data = self._fetch(job)
        ext = job.kind.ext
        if job.kind.name == "texture":
            data, ext = assets.convert_texture(data, self.image_format)
        elif job.kind.name == "notecard":
            data = assets.notecard_text(data)

        rel = job.rel_dir / (job.stem + ext)
        path = self.out_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".part")
        tmp.write_bytes(data)
        os.replace(tmp, path)
        if item.created_at > 0:
            os.utime(path, (item.created_at, item.created_at))

        with self._manifest_lock:
            self.manifest[key] = {
                "asset_id": str(item.asset_id),
                "file": rel.as_posix(),
                "name": item.name,
                "type": job.kind.name,
                "created_at": item.created_at,
            }
        return rel.as_posix()

    def _fetch(self, job: Job) -> bytes:
        item, kind = job.item, job.kind
        if kind.via_http:
            cap = self.caps.get("ViewerAsset")
            if not cap and kind.name == "texture":
                cap = self.caps.get("GetTexture")
            if not cap:
                raise TransferError("region did not grant the ViewerAsset capability")
            return self.http.get(f"{cap.rstrip('/')}/?{kind.cap_name}_id={item.asset_id}")
        if not self.transfers:
            raise TransferError("no UDP circuit available for this asset type")
        with self._transfer_slots:
            return self.transfers.fetch_inventory_asset(
                item.item_id, item.asset_id, kind.asset_type, item.owner_id)

    # -- manifest ---------------------------------------------------------

    def _load_manifest(self) -> dict[str, dict]:
        try:
            return json.loads(self.manifest_path.read_text()).get("items", {})
        except (OSError, ValueError):
            return {}

    def _save_manifest(self) -> None:
        if not self.manifest:
            return
        self.out_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.manifest_path.with_name(MANIFEST_NAME + ".part")
        tmp.write_text(json.dumps({"items": self.manifest}, indent=1, sort_keys=True))
        os.replace(tmp, self.manifest_path)

    def _print(self, msg: str) -> None:
        with self._print_lock:
            print(msg, file=sys.stderr, flush=True)
