import io
import struct
import uuid

import pytest

from sl_asset_downloader import assets, llsd, udp
from sl_asset_downloader.downloader import Downloader, may_export, sanitize
from sl_asset_downloader.inventory import (AT_NOTECARD, AT_TEXTURE, FT_ROOT_INVENTORY,
                                           FT_SNAPSHOT_CATEGORY, FT_TEXTURE, Inventory, Item,
                                           parse_item)
from sl_asset_downloader.login import InventoryFolder, Session, hash_password, split_username

AGENT = uuid.UUID("11111111-1111-1111-1111-111111111111")
OTHER = uuid.UUID("22222222-2222-2222-2222-222222222222")


def test_llsd_roundtrip():
    value = {"folders": [{"folder_id": AGENT, "fetch_items": True, "sort_order": 1,
                          "name": "Photo Album", "size": 1.5, "blob": b"\x00\x01", "none": None}]}
    assert llsd.parse_xml(llsd.format_xml(value)) == value


def test_llsd_parses_server_style_document():
    doc = b"""<?xml version="1.0" ?><llsd><map><key>ViewerAsset</key><string>https://x/cap/1</string>
    <key>n</key><integer>7</integer><key>ok</key><boolean>1</boolean><key>e</key><array /></map></llsd>"""
    assert llsd.parse_xml(doc) == {"ViewerAsset": "https://x/cap/1", "n": 7, "ok": True, "e": []}


def test_username_and_password():
    assert split_username("jane.doe") == ("jane", "doe")
    assert split_username("Jane Doe") == ("Jane", "Doe")
    assert split_username("janedoe") == ("janedoe", "Resident")
    assert hash_password("x" * 20) == hash_password("x" * 16)
    assert hash_password("secret").startswith("$1$")


def test_packet_roundtrip_with_zerocode_and_appended_acks():
    body = AGENT.bytes + b"\x00" * 10 + b"\x05"
    pkt = udp.build_packet(udp.FLAG_RELIABLE, 42, udp.MSG_COMPLETE_AGENT_MOVEMENT, body)
    flags, seq, msg, out, acks = udp.parse_packet(pkt)
    assert (flags, seq, msg, out, acks) == (udp.FLAG_RELIABLE, 42, udp.MSG_COMPLETE_AGENT_MOVEMENT, body, [])

    # Zerocoded payload plus two appended acks (big endian, count last)
    # Low 154 is ff ff 00 9a, so its zero byte is run-length encoded too
    raw = b"\xff\xff\x00\x01\x9a" + b"\x01\x02\x00\x03\x07"
    pkt = struct.pack(">BIB", udp.FLAG_ZEROCODED | udp.FLAG_ACK, 9, 0) + raw + struct.pack(">II", 5, 6) + b"\x02"
    flags, seq, msg, out, acks = udp.parse_packet(pkt)
    assert msg == udp.MSG_TRANSFER_INFO
    assert out == b"\x01\x02\x00\x00\x00\x07"
    assert acks == [5, 6]


def test_message_numbers():
    assert udp.encode_msg_num(udp.MSG_START_PING_CHECK) == b"\x01"
    assert udp.encode_msg_num(udp.MSG_USE_CIRCUIT_CODE) == b"\xff\xff\x00\x03"
    assert udp.encode_msg_num(udp.MSG_PACKET_ACK) == b"\xff\xff\xff\xfb"
    assert udp.decode_msg_num(b"\xff\xff\xff\xfb") == (udp.MSG_PACKET_ACK, 4)
    assert udp.decode_msg_num(b"\xff\x06") == (("Medium", 6), 2)


def test_notecard_text():
    text = "Hello\nWorld {braces}"
    asset = (f"Linden text version 2\n{{\nLLEmbeddedItems version 1\n{{\ncount 0\n}}\n"
             f"Text length {len(text)}\n{text}}}\n").encode()
    assert assets.notecard_text(asset) == text.encode()


def test_texture_conversion_roundtrip():
    Image = pytest.importorskip("PIL.Image")
    img = Image.new("RGBA", (16, 8), (255, 0, 0, 128))
    buf = io.BytesIO()
    try:
        img.save(buf, "JPEG2000", no_jp2=True, irreversible=False)
    except (OSError, KeyError):
        pytest.skip("Pillow built without OpenJPEG")
    data, ext = assets.convert_texture(buf.getvalue(), "png")
    assert ext == ".png"
    out = Image.open(io.BytesIO(data))
    assert out.size == (16, 8) and out.mode == "RGBA"
    assert out.getpixel((3, 3)) == (255, 0, 0, 128)


def test_bad_texture_falls_back_to_raw():
    assert assets.convert_texture(b"not an image", "png") == (b"not an image", ".j2c")


def test_sanitize():
    assert sanitize('a/b:c*?"<>|') == "a_b_c______"
    assert sanitize("  ...  ") == "unnamed"
    assert sanitize("CON") == "_CON"
    assert sanitize("photo.") == "photo"


def _item(name, creator=AGENT, mask=0x7FFFFFFF, parent=None, atype=AT_TEXTURE, item_id=None):
    return Item(item_id=item_id or uuid.uuid4(), parent_id=parent or uuid.uuid4(), name=name, desc="",
                asset_id=uuid.uuid4(), asset_type=atype, inv_type=0, creator_id=creator,
                owner_id=AGENT, owner_mask=mask, created_at=0)


def test_may_export():
    assert may_export(_item("mine", mask=0), AGENT)
    assert may_export(_item("fullperm", creator=OTHER), AGENT)
    assert not may_export(_item("copy-only", creator=OTHER, mask=0x8000), AGENT)


def test_parse_item_masks_and_ids():
    raw = {"item_id": AGENT, "parent_id": str(OTHER), "name": "x", "asset_id": AGENT, "type": 7,
           "inv_type": 7, "created_at": 123,
           "permissions": {"creator_id": AGENT, "owner_id": AGENT, "owner_mask": -1}}
    item = parse_item(raw)
    assert item.parent_id == OTHER and item.asset_type == AT_NOTECARD and item.is_full_perm


def _inventory():
    root = uuid.uuid4()
    photos, textures, sub, other_sub = (uuid.uuid4() for _ in range(4))
    session = Session(
        login_uri="x", agent_id=AGENT, session_id=uuid.uuid4(), secure_session_id=uuid.uuid4(),
        circuit_code=1, sim_ip="127.0.0.1", sim_port=1, seed_capability="x", first_name="a",
        last_name="b", inventory_root=root, inventory_skeleton=[
            InventoryFolder(root, uuid.UUID(int=0), "My Inventory", FT_ROOT_INVENTORY),
            InventoryFolder(photos, root, "Photo Album", FT_SNAPSHOT_CATEGORY),
            InventoryFolder(textures, root, "Textures", FT_TEXTURE),
            InventoryFolder(sub, textures, "Skins", -1),
            InventoryFolder(other_sub, photos, "Skins", -1),
        ])
    return Inventory(session, None, {}), dict(root=root, photos=photos, textures=textures, sub=sub)


def test_folder_resolution():
    inv, ids = _inventory()
    assert inv.resolve("@photos").folder_id == ids["photos"]
    assert inv.resolve("@textures").folder_id == ids["textures"]
    assert inv.resolve("textures/skins").folder_id == ids["sub"]
    assert inv.resolve(str(ids["sub"])).folder_id == ids["sub"]
    assert inv.path_of(ids["sub"]) == ["Textures", "Skins"]
    with pytest.raises(LookupError, match="ambiguous"):
        inv.resolve("Skins")
    with pytest.raises(LookupError):
        inv.resolve("Nope")


def test_plan_makes_unique_paths(tmp_path):
    inv, ids = _inventory()
    dl = Downloader(inv, None, {}, None, tmp_path)
    a = _item("Snapshot", parent=ids["photos"], item_id=uuid.UUID(int=1))
    b = _item("Snapshot", parent=ids["photos"], item_id=uuid.UUID(int=2))
    c = _item("rock", parent=ids["sub"])
    jobs = {j.item.item_id: j for j in dl.plan([b, c, a])}
    assert str(jobs[a.item_id].rel_dir / jobs[a.item_id].stem) == "Photo Album/Snapshot"
    assert jobs[b.item_id].stem == "Snapshot [00000000]"
    assert jobs[c.item_id].rel_dir.as_posix() == "Textures/Skins"
