"""End-to-end test of the CLI against an in-process fake grid
(XML-RPC login, capabilities over HTTP, and a UDP "region")."""

import io
import json
import socket
import struct
import threading
import uuid
import xmlrpc.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

from sl_asset_downloader import cli, llsd, udp

AGENT = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000001")
STRANGER = uuid.UUID("bbbbbbbb-0000-0000-0000-000000000002")
ROOT, PHOTOS, TEXTURES, SUB, NOTES = (uuid.uuid4() for _ in range(5))
FULL = 0x7FFFFFFF
NOTE_TEXT = "Shopping list:\n- prims\n- more prims {}"
NOTECARD_ASSET = (f"Linden text version 2\n{{\nLLEmbeddedItems version 1\n{{\ncount 0\n}}\n"
                  f"Text length {len(NOTE_TEXT)}\n{NOTE_TEXT}}}\n").encode() + b"x" * 1500


def _j2c(color):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), color).save(buf, "JPEG2000", no_jp2=True, irreversible=False)
    return buf.getvalue()


def _item(name, parent, atype, asset_id, creator=AGENT, mask=FULL):
    return {"item_id": uuid.uuid4(), "parent_id": parent, "name": name, "desc": "",
            "asset_id": asset_id, "type": atype, "inv_type": atype, "flags": 0,
            "created_at": 1700000000,
            "permissions": {"creator_id": creator, "owner_id": AGENT, "owner_mask": mask,
                            "base_mask": mask, "group_mask": 0, "everyone_mask": 0,
                            "next_owner_mask": mask}}


class FakeGrid:
    def __init__(self):
        self.assets = {uuid.uuid4(): _j2c((255, 0, 0)), uuid.uuid4(): _j2c((0, 255, 0)),
                       uuid.uuid4(): _j2c((0, 0, 255)), uuid.uuid4(): _j2c((9, 9, 9))}
        red, green, blue, secret = self.assets
        self.note_asset = uuid.uuid4()
        self.items = {
            PHOTOS: [_item("Snapshot", PHOTOS, 0, red), _item("Snapshot", PHOTOS, 0, green)],
            TEXTURES: [_item("brick/wall", TEXTURES, 0, blue),
                       _item("Restricted", TEXTURES, 0, secret, creator=STRANGER, mask=0x2000)],
            SUB: [_item("my sound", SUB, 1, uuid.uuid4())],
            NOTES: [_item("Todo", NOTES, 7, self.note_asset)],
        }
        self.http_requests = []
        self.logged_out = threading.Event()
        self.handshake_replied = threading.Event()

        self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp.bind(("127.0.0.1", 0))
        self.udp.settimeout(0.2)
        self._seq = 1000
        self._stop = threading.Event()
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.base = f"http://127.0.0.1:{self.http.server_address[1]}"
        self._threads = [threading.Thread(target=self.http.serve_forever, daemon=True),
                         threading.Thread(target=self._udp_loop, daemon=True)]
        for t in self._threads:
            t.start()

    def close(self):
        self._stop.set()
        self.http.shutdown()
        self.udp.close()

    # -- HTTP -------------------------------------------------------------

    def _handler(grid):
        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, body, ctype):
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                grid.http_requests.append(self.path)
                if self.path == "/login":
                    (params,), _ = xmlrpc.client.loads(body)
                    assert params["passwd"].startswith("$1$")
                    resp = grid.login_response(params)
                    self._send(xmlrpc.client.dumps((resp,), methodresponse=True).encode(), "text/xml")
                elif self.path == "/seed":
                    names = llsd.parse_xml(body)
                    caps = {"FetchInventoryDescendents2": grid.base + "/fetch",
                            "ViewerAsset": grid.base + "/asset"}
                    self._send(llsd.format_xml({k: v for k, v in caps.items() if k in names}),
                               llsd.CONTENT_TYPE)
                elif self.path == "/fetch":
                    req = llsd.parse_xml(body)
                    folders = []
                    for f in req["folders"]:
                        fid = f["folder_id"]
                        cats = [{"category_id": SUB, "parent_id": TEXTURES, "name": "Sub",
                                 "type_default": -1}] if fid == TEXTURES else []
                        folders.append({"folder_id": fid, "owner_id": AGENT, "agent_id": AGENT,
                                        "version": 1, "descendents": 1, "categories": cats,
                                        "items": grid.items.get(fid, [])})
                    self._send(llsd.format_xml({"folders": folders}), llsd.CONTENT_TYPE)
                else:
                    self.send_error(404)

            def do_GET(self):
                url = urlparse(self.path)
                qs = parse_qs(url.query)
                grid.http_requests.append(self.path)
                if url.path.rstrip("/") == "/asset" and "texture_id" in qs:
                    data = grid.assets.get(uuid.UUID(qs["texture_id"][0]))
                    if data:
                        return self._send(data, "image/x-j2c")
                if url.path.rstrip("/") == "/asset" and "sound_id" in qs:
                    return self._send(b"OggS fake", "audio/ogg")
                self.send_error(404)
        return H

    def login_response(self, params):
        skeleton = [
            {"folder_id": str(ROOT), "parent_id": str(uuid.UUID(int=0)), "name": "My Inventory",
             "type_default": 8, "version": 1},
            {"folder_id": str(PHOTOS), "parent_id": str(ROOT), "name": "Photo Album",
             "type_default": 15, "version": 1},
            {"folder_id": str(TEXTURES), "parent_id": str(ROOT), "name": "Textures",
             "type_default": 0, "version": 1},
            {"folder_id": str(NOTES), "parent_id": str(ROOT), "name": "Notecards",
             "type_default": 7, "version": 1},
            {"folder_id": str(SUB), "parent_id": str(TEXTURES), "name": "Sub",
             "type_default": -1, "version": 1},
        ]
        return {"login": "true", "agent_id": str(AGENT), "session_id": str(uuid.uuid4()),
                "secure_session_id": str(uuid.uuid4()), "circuit_code": 4242,
                "sim_ip": "127.0.0.1", "sim_port": self.udp.getsockname()[1],
                "seed_capability": self.base + "/seed", "first_name": '"Test"', "last_name": "User",
                "inventory-root": [{"folder_id": str(ROOT)}], "inventory-skeleton": skeleton}

    # -- UDP --------------------------------------------------------------

    def _send_udp(self, addr, msg, body, reliable=False):
        self._seq += 1
        flags = udp.FLAG_RELIABLE if reliable else 0
        self.udp.sendto(udp.build_packet(flags, self._seq, msg, body), addr)

    def _udp_loop(self):
        while not self._stop.is_set():
            try:
                data, addr = self.udp.recvfrom(65536)
            except (socket.timeout, OSError):
                continue
            flags, seq, msg, body, _acks = udp.parse_packet(data)
            if flags & udp.FLAG_RELIABLE:
                self._send_udp(addr, udp.MSG_PACKET_ACK, b"\x01" + struct.pack("<I", seq))
            if msg == udp.MSG_COMPLETE_AGENT_MOVEMENT:
                self._send_udp(addr, udp.MSG_REGION_HANDSHAKE, b"\x00" * 20, reliable=True)
                self._send_udp(addr, udp.MSG_START_PING_CHECK, b"\x07\x00\x00\x00\x00")
                self._send_udp(addr, udp.MSG_AGENT_MOVEMENT_COMPLETE, AGENT.bytes + b"\x00" * 60)
            elif msg == udp.MSG_REGION_HANDSHAKE_REPLY:
                self.handshake_replied.set()
            elif msg == udp.MSG_TRANSFER_REQUEST:
                self._on_transfer(addr, body)
            elif msg == udp.MSG_LOGOUT_REQUEST:
                self._send_udp(addr, udp.MSG_LOGOUT_REPLY, b"\x00" * 33, reliable=True)
                self.logged_out.set()

    def _on_transfer(self, addr, body):
        tid = body[:16]
        channel, source, _prio = struct.unpack_from("<iif", body, 16)
        n = struct.unpack_from("<H", body, 28)[0]
        params = body[30:30 + n]
        assert (channel, source, n) == (2, 3, 100)
        asset_id = uuid.UUID(bytes=params[80:96])
        asset_type = struct.unpack_from("<i", params, 96)[0]
        assert asset_id == self.note_asset and asset_type == 7
        data = NOTECARD_ASSET
        chunks = [data[:1000], data[1000:]]
        # Packets out of order, info last: the client must cope.
        self._send_udp(addr, udp.MSG_TRANSFER_PACKET,
                       tid + struct.pack("<iii", 2, 1, 1) + udp.pack_var2(chunks[1]))
        self._send_udp(addr, udp.MSG_TRANSFER_PACKET,
                       tid + struct.pack("<iii", 2, 0, 0) + udp.pack_var2(chunks[0]))
        self._send_udp(addr, udp.MSG_TRANSFER_INFO,
                       tid + struct.pack("<iiii", 2, 2, 0, len(data)) + udp.pack_var2(b""), reliable=True)


@pytest.fixture
def grid(monkeypatch, tmp_path):
    pytest.importorskip("PIL.Image")
    g = FakeGrid()
    monkeypatch.setenv("SL_PASSWORD", "hunter2")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    yield g
    g.close()


def run(grid, tmp_path, *args):
    return cli.main(["download", "-u", "Test User", "--grid", grid.base + "/login",
                     "-o", str(tmp_path / "out"), *args])


def test_default_downloads_photos_and_textures(grid, tmp_path):
    assert run(grid, tmp_path) == 0
    out = tmp_path / "out"
    files = sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file())
    snap2 = [f for f in files if f.startswith("Photo Album/Snapshot [")]
    assert "Photo Album/Snapshot.png" in files and len(snap2) == 1
    assert "Textures/brick_wall.png" in files
    assert not any("Restricted" in f for f in files)   # not full perm, not creator
    assert not any(f.endswith(".ogg") for f in files)  # sounds not requested
    from PIL import Image
    assert Image.open(out / "Textures/brick_wall.png").getpixel((2, 2)) == (0, 0, 255)
    assert grid.handshake_replied.is_set() and grid.logged_out.is_set()

    # A second run downloads nothing new
    before = sum(1 for p in grid.http_requests if p.startswith("/asset"))
    assert run(grid, tmp_path) == 0
    assert sum(1 for p in grid.http_requests if p.startswith("/asset")) == before
    manifest = json.loads((out / ".sl-asset-downloader.json").read_text())
    assert len(manifest["items"]) == 3


def test_notecards_over_udp_and_sounds(grid, tmp_path):
    assert run(grid, tmp_path, "-f", "@notecards", "-f", "Textures/Sub", "-t", "notecard",
               "-t", "sound") == 0
    out = tmp_path / "out"
    assert (out / "Notecards/Todo.txt").read_text() == NOTE_TEXT
    assert (out / "Textures/Sub/my sound.ogg").read_bytes() == b"OggS fake"


def test_unknown_folder_is_an_error(grid, tmp_path, capsys):
    assert run(grid, tmp_path, "-f", "Nope") == 2
    assert "not found" in capsys.readouterr().err
    assert grid.logged_out.is_set()
