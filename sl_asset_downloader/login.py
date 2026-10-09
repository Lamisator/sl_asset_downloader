"""XML-RPC login to a Second Life (or OpenSimulator) grid."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import time
import uuid
import xmlrpc.client
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import __version__
from .http import HttpClient, HttpError

log = logging.getLogger(__name__)

GRIDS = {
    "agni": "https://login.agni.lindenlab.com/cgi-bin/login.cgi",   # main grid
    "aditi": "https://login.aditi.lindenlab.com/cgi-bin/login.cgi",  # beta grid
}

CHANNEL = "SL Asset Downloader"


class LoginError(Exception):
    def __init__(self, reason: str, message: str):
        self.reason = reason
        super().__init__(message or reason)


@dataclass
class InventoryFolder:
    folder_id: uuid.UUID
    parent_id: uuid.UUID
    name: str
    type_default: int
    version: int = 0


@dataclass
class Session:
    login_uri: str
    agent_id: uuid.UUID
    session_id: uuid.UUID
    secure_session_id: uuid.UUID
    circuit_code: int
    sim_ip: str
    sim_port: int
    seed_capability: str
    first_name: str
    last_name: str
    inventory_root: uuid.UUID
    inventory_skeleton: list[InventoryFolder] = field(default_factory=list)

    @property
    def display_name(self) -> str:
        if self.last_name.lower() == "resident":
            return self.first_name
        return f"{self.first_name} {self.last_name}"


def split_username(username: str) -> tuple[str, str]:
    parts = username.replace(".", " ").split()
    if len(parts) == 1:
        return parts[0], "Resident"
    if len(parts) == 2:
        return parts[0], parts[1]
    raise ValueError(f"invalid Second Life username: {username!r}")


def hash_password(password: str) -> str:
    # SL only ever uses the first 16 characters of a password.
    return "$1$" + hashlib.md5(password[:16].encode("utf-8")).hexdigest()


def _platform() -> tuple[str, str]:
    system = platform.system()
    code = {"Windows": "win", "Darwin": "mac"}.get(system, "lnx")
    return code, platform.release()


def _machine_hash() -> str:
    return hashlib.md5(str(uuid.getnode()).encode()).hexdigest()


class MfaStore:
    """Remembers the per-account MFA hash, like the official viewer does,
    so the authenticator code is not needed on every login."""

    def __init__(self, path: Path | None = None):
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
        self.path = path or base / "sl-asset-downloader" / "mfa.json"

    def _load(self) -> dict[str, str]:
        try:
            return json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}

    def get(self, key: str) -> str:
        return self._load().get(key, "")

    def set(self, key: str, value: str) -> None:
        data = self._load()
        data[key] = value
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)


def _strip_quotes(s: str) -> str:
    return s.strip().strip('"')


def login(http: HttpClient, login_uri: str, username: str, password: str,
          start: str = "last", agree_to_tos: bool = False,
          mfa_prompt: Callable[[str], str] | None = None,
          mfa_store: MfaStore | None = None, presence_retry_delay: float = 10.0) -> Session:
    first, last = split_username(username)
    plat, plat_version = _platform()
    mfa_store = mfa_store or MfaStore()
    mfa_key = f"{login_uri}|{first.lower()} {last.lower()}"
    machine = _machine_hash()

    params = {
        "first": first,
        "last": last,
        "passwd": hash_password(password),
        "start": start,
        "channel": CHANNEL,
        "version": __version__,
        "platform": plat,
        "platform_version": plat_version,
        "address_size": 64,
        "mac": machine,
        "id0": machine,
        "host_id": "",
        "agree_to_tos": agree_to_tos,
        "read_critical": agree_to_tos,
        "extended_errors": True,
        "last_exec_event": 0,
        "token": "",
        "mfa_hash": mfa_store.get(mfa_key),
        "options": ["inventory-root", "inventory-skeleton"],
    }

    presence_retries = 2
    for _attempt in range(6):
        body = xmlrpc.client.dumps((params,), "login_to_simulator").encode("utf-8")
        try:
            raw, _ = http.request("POST", login_uri, body, {"Content-Type": "text/xml"})
        except HttpError as e:
            raise LoginError("http", f"login server returned HTTP {e.status}") from None
        try:
            (resp,), _ = xmlrpc.client.loads(raw)
        except xmlrpc.client.Fault as e:
            raise LoginError("fault", f"login server fault: {e.faultString}") from None

        if str(resp.get("login")).lower() == "true":
            if resp.get("mfa_hash"):
                mfa_store.set(mfa_key, resp["mfa_hash"])
            return _session_from_response(login_uri, resp)

        reason = resp.get("reason", "")
        message = resp.get("message", "") or f"unexpected login response (login={resp.get('login')!r})"
        if reason == "mfa_challenge" and mfa_prompt:
            params["token"] = "".join(mfa_prompt(message or "Enter your MFA token:").split())
            continue
        if reason == "presence" and presence_retries:
            # A previous session (e.g. an interrupted run) is still around. The
            # rejected attempt makes the grid clean it up; try again shortly.
            presence_retries -= 1
            log.warning("account still appears to be logged in; retrying in %.0fs", presence_retry_delay)
            time.sleep(presence_retry_delay)
            continue
        if reason in ("tos", "critical") and not agree_to_tos:
            message += ("\n\nRe-run with --agree-to-tos after reading the new terms "
                        "(e.g. by logging in once with the official viewer).")
        raise LoginError(reason, message)
    raise LoginError(reason, message)


def _session_from_response(login_uri: str, r: dict) -> Session:
    skeleton = [
        InventoryFolder(
            folder_id=uuid.UUID(f["folder_id"]),
            parent_id=uuid.UUID(f["parent_id"]) if f.get("parent_id") else uuid.UUID(int=0),
            name=f.get("name", ""),
            type_default=int(f.get("type_default", -1)),
            version=int(f.get("version", 0)),
        )
        for f in r.get("inventory-skeleton", []) or []
    ]
    roots = r.get("inventory-root") or []
    root_id = uuid.UUID(roots[0]["folder_id"]) if roots else uuid.UUID(int=0)
    return Session(
        login_uri=login_uri,
        agent_id=uuid.UUID(r["agent_id"]),
        session_id=uuid.UUID(r["session_id"]),
        secure_session_id=uuid.UUID(r["secure_session_id"]),
        circuit_code=int(r["circuit_code"]),
        sim_ip=r["sim_ip"],
        sim_port=int(r["sim_port"]),
        seed_capability=r["seed_capability"],
        first_name=_strip_quotes(r.get("first_name", "")),
        last_name=_strip_quotes(r.get("last_name", "")),
        inventory_root=root_id,
        inventory_skeleton=skeleton,
    )
