"""Inventory folder resolution and item fetching via capabilities."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from .http import HttpClient
from .login import InventoryFolder, Session

log = logging.getLogger(__name__)

NULL_UUID = uuid.UUID(int=0)

# Asset types (llassettype.h)
AT_TEXTURE = 0
AT_SOUND = 1
AT_LANDMARK = 3
AT_CLOTHING = 5
AT_NOTECARD = 7
AT_LSL_TEXT = 10
AT_BODYPART = 13
AT_ANIMATION = 20
AT_GESTURE = 21
AT_LINK = 24
AT_LINK_FOLDER = 25
AT_SETTINGS = 56
AT_MATERIAL = 57

# Folder types (llfoldertype.h) that matter for shortcuts
FT_TEXTURE = 0
FT_SOUND = 1
FT_NOTECARD = 7
FT_ROOT_INVENTORY = 8
FT_LSL_TEXT = 10
FT_TRASH = 14
FT_SNAPSHOT_CATEGORY = 15
FT_ANIMATION = 20

# "@name" shortcuts for system folders, independent of their display name
FOLDER_SHORTCUTS = {
    "photos": FT_SNAPSHOT_CATEGORY,
    "photo-album": FT_SNAPSHOT_CATEGORY,
    "snapshots": FT_SNAPSHOT_CATEGORY,
    "textures": FT_TEXTURE,
    "sounds": FT_SOUND,
    "notecards": FT_NOTECARD,
    "scripts": FT_LSL_TEXT,
    "animations": FT_ANIMATION,
    "root": FT_ROOT_INVENTORY,
}

# Permission bits (llpermissionsflags.h)
PERM_TRANSFER = 1 << 13
PERM_MODIFY = 1 << 14
PERM_COPY = 1 << 15
PERM_FULL = PERM_TRANSFER | PERM_MODIFY | PERM_COPY


@dataclass
class Item:
    item_id: uuid.UUID
    parent_id: uuid.UUID
    name: str
    desc: str
    asset_id: uuid.UUID
    asset_type: int
    inv_type: int
    creator_id: uuid.UUID
    owner_id: uuid.UUID
    owner_mask: int
    created_at: int

    @property
    def is_full_perm(self) -> bool:
        return self.owner_mask & PERM_FULL == PERM_FULL


def _uuid(v) -> uuid.UUID:
    if isinstance(v, uuid.UUID):
        return v
    try:
        return uuid.UUID(str(v))
    except ValueError:
        return NULL_UUID


def parse_item(d: dict) -> Item:
    perms = d.get("permissions") or {}
    return Item(
        item_id=_uuid(d.get("item_id")),
        parent_id=_uuid(d.get("parent_id")),
        name=d.get("name", "") or "",
        desc=d.get("desc", "") or "",
        asset_id=_uuid(d.get("asset_id") or d.get("linked_id")),
        asset_type=int(d.get("type", -1)),
        inv_type=int(d.get("inv_type", -1)),
        creator_id=_uuid(perms.get("creator_id")),
        owner_id=_uuid(perms.get("owner_id")),
        owner_mask=int(perms.get("owner_mask", 0)) & 0xFFFFFFFF,
        created_at=int(d.get("created_at", 0) or 0),
    )


class Inventory:
    def __init__(self, session: Session, http: HttpClient, caps: dict[str, str]):
        self.session = session
        self.http = http
        self.caps = caps
        self.folders: dict[uuid.UUID, InventoryFolder] = {
            f.folder_id: f for f in session.inventory_skeleton}
        self.root_id = session.inventory_root
        if self.root_id == NULL_UUID:
            roots = [f for f in self.folders.values() if f.type_default == FT_ROOT_INVENTORY]
            if roots:
                self.root_id = roots[0].folder_id

    # -- folder tree ------------------------------------------------------

    def children(self, folder_id: uuid.UUID) -> list[InventoryFolder]:
        kids = [f for f in self.folders.values() if f.parent_id == folder_id]
        return sorted(kids, key=lambda f: f.name.lower())

    def path_of(self, folder_id: uuid.UUID) -> list[str]:
        parts: list[str] = []
        seen = set()
        fid = folder_id
        while fid in self.folders and fid != self.root_id and fid not in seen:
            seen.add(fid)
            f = self.folders[fid]
            parts.append(f.name)
            fid = f.parent_id
        return list(reversed(parts))

    def resolve(self, spec: str) -> InventoryFolder:
        """Find a folder by "@shortcut", UUID, "A/B/C" path, or unique name."""
        spec = spec.strip()
        if spec.startswith("@"):
            key = spec[1:].lower()
            if key not in FOLDER_SHORTCUTS:
                raise LookupError(f"unknown folder shortcut {spec!r}; "
                                  f"known: {', '.join('@' + k for k in FOLDER_SHORTCUTS)}")
            ftype = FOLDER_SHORTCUTS[key]
            matches = [f for f in self.folders.values() if f.type_default == ftype]
            # Prefer the system folder directly under the root
            matches.sort(key=lambda f: (f.parent_id != self.root_id, f.name))
            if not matches:
                raise LookupError(f"no {spec} folder in this inventory")
            return matches[0]
        try:
            fid = uuid.UUID(spec)
            if fid in self.folders:
                return self.folders[fid]
        except ValueError:
            pass

        parts = [p for p in spec.strip("/").split("/") if p]
        if not parts:
            return self.folders[self.root_id]
        # Full path from the root
        current = self.root_id
        for part in parts:
            nxt = [f for f in self.children(current) if f.name.lower() == part.lower()]
            if not nxt:
                break
            current = nxt[0].folder_id
        else:
            return self.folders[current]
        # Unique folder name anywhere
        if len(parts) == 1:
            named = [f for f in self.folders.values() if f.name.lower() == parts[0].lower()]
            if len(named) == 1:
                return named[0]
            if len(named) > 1:
                paths = ", ".join("/".join(self.path_of(f.folder_id)) for f in named)
                raise LookupError(f"folder name {spec!r} is ambiguous: {paths}")
        raise LookupError(f"folder {spec!r} not found (use the 'folders' command to list them)")

    # -- item fetching ----------------------------------------------------

    def fetch_items(self, root_ids: list[uuid.UUID], recursive: bool = True,
                    batch_size: int = 5) -> list[Item]:
        url = self.caps.get("FetchInventoryDescendents2")
        if not url:
            raise RuntimeError("region did not grant the FetchInventoryDescendents2 capability")
        queue = list(dict.fromkeys(root_ids))
        visited: set[uuid.UUID] = set()
        items: dict[uuid.UUID, Item] = {}
        while queue:
            batch = []
            while queue and len(batch) < batch_size:
                fid = queue.pop(0)
                if fid not in visited:
                    visited.add(fid)
                    batch.append(fid)
            if not batch:
                continue
            body = {"folders": [
                {"folder_id": fid, "owner_id": self.session.agent_id,
                 "fetch_folders": True, "fetch_items": True, "sort_order": 1}
                for fid in batch]}
            resp = self.http.post_llsd(url, body) or {}
            for bad in resp.get("bad_folders", []) or []:
                log.warning("server could not fetch folder %s: %s",
                            bad.get("folder_id"), bad.get("error"))
            for folder in resp.get("folders", []) or []:
                parent = _uuid(folder.get("folder_id"))
                for cat in folder.get("categories", []) or []:
                    cid = _uuid(cat.get("category_id") or cat.get("folder_id"))
                    if cid not in self.folders:
                        self.folders[cid] = InventoryFolder(
                            folder_id=cid, parent_id=parent, name=cat.get("name", ""),
                            type_default=int(cat.get("type_default", -1)))
                    # Never descend into the Trash unless it was asked for directly
                    is_trash = self.folders[cid].type_default == FT_TRASH
                    if recursive and cid not in visited and not is_trash:
                        queue.append(cid)
                for raw in folder.get("items", []) or []:
                    item = parse_item(raw)
                    items[item.item_id] = item
        return list(items.values())

    def resolve_links(self, links: list[Item]) -> list[Item]:
        """Fetch the items that inventory links point to."""
        url = self.caps.get("FetchInventory2")
        if not url or not links:
            return []
        resolved: list[Item] = []
        for i in range(0, len(links), 50):
            chunk = links[i:i + 50]
            body = {"agent_id": self.session.agent_id, "items": [
                {"owner_id": self.session.agent_id, "item_id": link.asset_id} for link in chunk]}
            resp = self.http.post_llsd(url, body) or {}
            targets = {}
            for raw in resp.get("items", []) or []:
                t = parse_item(raw)
                targets[t.item_id] = t
            for link in chunk:
                target = targets.get(link.asset_id)
                if target:
                    # Keep the link's name and location, the target's content
                    target.parent_id = link.parent_id
                    target.name = link.name or target.name
                    resolved.append(target)
        return resolved
