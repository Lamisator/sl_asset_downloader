"""Minimal implementation of the Second Life UDP message protocol.

Just enough to establish a circuit with the login region (which keeps the
session alive while capabilities are used), answer pings, ack reliable
packets, log out cleanly, and run asset transfers for item types that are
not served over HTTP (notecards, scripts).

Message layouts come from Linden Lab's message_template.msg.
"""

from __future__ import annotations

import logging
import socket
import struct
import threading
import time
import uuid
from typing import Callable

from .login import Session

log = logging.getLogger(__name__)

# Packet header flags
FLAG_ZEROCODED = 0x80
FLAG_RELIABLE = 0x40
FLAG_RESENT = 0x20
FLAG_ACK = 0x10

# (frequency, number). Fixed messages 0xFFFFFFxx decode as Low 0xFFxx.
HIGH, MEDIUM, LOW = "High", "Medium", "Low"
MSG_START_PING_CHECK = (HIGH, 1)
MSG_COMPLETE_PING_CHECK = (HIGH, 2)
MSG_TRANSFER_PACKET = (HIGH, 17)
MSG_USE_CIRCUIT_CODE = (LOW, 3)
MSG_REGION_HANDSHAKE = (LOW, 148)
MSG_REGION_HANDSHAKE_REPLY = (LOW, 149)
MSG_DISABLE_SIMULATOR = (LOW, 152)
MSG_TRANSFER_REQUEST = (LOW, 153)
MSG_TRANSFER_INFO = (LOW, 154)
MSG_TRANSFER_ABORT = (LOW, 155)
MSG_KICK_USER = (LOW, 163)
MSG_COMPLETE_AGENT_MOVEMENT = (LOW, 249)
MSG_AGENT_MOVEMENT_COMPLETE = (LOW, 250)
MSG_LOGOUT_REQUEST = (LOW, 252)
MSG_LOGOUT_REPLY = (LOW, 253)
MSG_PACKET_ACK = (LOW, 0xFFFB)

RESEND_AFTER = 1.5
MAX_RESENDS = 8

Handler = Callable[[bytes], None]


def encode_msg_num(msg: tuple[str, int]) -> bytes:
    freq, num = msg
    if freq == HIGH:
        return bytes([num])
    if freq == MEDIUM:
        return b"\xff" + bytes([num])
    return b"\xff\xff" + num.to_bytes(2, "big")


def decode_msg_num(data: bytes) -> tuple[tuple[str, int], int]:
    """Returns ((freq, num), header_length)."""
    if data[0] != 0xFF:
        return (HIGH, data[0]), 1
    if data[1] != 0xFF:
        return (MEDIUM, data[1]), 2
    return (LOW, int.from_bytes(data[2:4], "big")), 4


def zero_decode(data: bytes) -> bytes:
    out = bytearray()
    i, n = 0, len(data)
    while i < n:
        b = data[i]
        if b == 0 and i + 1 < n:
            out.extend(b"\x00" * data[i + 1])
            i += 2
        else:
            out.append(b)
            i += 1
    return bytes(out)


def build_packet(flags: int, seq: int, msg: tuple[str, int], body: bytes) -> bytes:
    return struct.pack(">BIB", flags, seq, 0) + encode_msg_num(msg) + body


def parse_packet(data: bytes) -> tuple[int, int, tuple[str, int], bytes, list[int]]:
    """Returns (flags, seq, msg, body, appended_acks)."""
    flags, seq, extra = struct.unpack_from(">BIB", data, 0)
    payload = data[6 + extra:]
    acks: list[int] = []
    if flags & FLAG_ACK and payload:
        count = payload[-1]
        start = len(payload) - 1 - 4 * count
        acks = [int.from_bytes(payload[start + 4 * i:start + 4 * i + 4], "big") for i in range(count)]
        payload = payload[:start]
    if flags & FLAG_ZEROCODED:
        payload = zero_decode(payload)
    msg, hlen = decode_msg_num(payload)
    return flags, seq, msg, payload[hlen:], acks


def pack_var2(data: bytes) -> bytes:
    return struct.pack("<H", len(data)) + data


class CircuitClosed(Exception):
    pass


class Circuit:
    def __init__(self, session: Session):
        self.session = session
        self.addr = (session.sim_ip, session.sim_port)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.settimeout(0.2)
        self._seq = 0
        self._lock = threading.Lock()
        self._unacked: dict[int, list] = {}  # seq -> [packet, last_sent, resends]
        self._pending_acks: list[int] = []
        self._gave_up: set[int] = set()
        self._handlers: dict[tuple[str, int], list[Handler]] = {}
        self._stop = threading.Event()
        self.closed = threading.Event()
        self.close_reason = ""
        self.movement_complete = threading.Event()
        self.logout_reply = threading.Event()
        self._threads: list[threading.Thread] = []

        self.on(MSG_START_PING_CHECK, self._on_ping)
        self.on(MSG_REGION_HANDSHAKE, self._on_region_handshake)
        self.on(MSG_AGENT_MOVEMENT_COMPLETE, lambda _b: self.movement_complete.set())
        self.on(MSG_LOGOUT_REPLY, lambda _b: self.logout_reply.set())
        self.on(MSG_KICK_USER, self._on_kick)
        self.on(MSG_DISABLE_SIMULATOR, lambda _b: self._mark_closed("region disabled the circuit"))

    # -- public API -------------------------------------------------------

    def on(self, msg: tuple[str, int], handler: Handler) -> None:
        self._handlers.setdefault(msg, []).append(handler)

    def connect(self, timeout: float = 30.0) -> None:
        s = self.session
        for target in (self._recv_loop, self._maintenance_loop):
            t = threading.Thread(target=target, daemon=True)
            t.start()
            self._threads.append(t)
        self.send(MSG_USE_CIRCUIT_CODE,
                  struct.pack("<I", s.circuit_code) + s.session_id.bytes + s.agent_id.bytes,
                  reliable=True)
        self.send(MSG_COMPLETE_AGENT_MOVEMENT,
                  s.agent_id.bytes + s.session_id.bytes + struct.pack("<I", s.circuit_code),
                  reliable=True)
        if not self.movement_complete.wait(timeout):
            log.warning("region did not confirm arrival within %.0fs; continuing anyway", timeout)

    def send(self, msg: tuple[str, int], body: bytes, reliable: bool = False) -> int:
        if self.closed.is_set():
            raise CircuitClosed(self.close_reason)
        with self._lock:
            self._seq += 1
            seq = self._seq
            flags = FLAG_RELIABLE if reliable else 0
            packet = build_packet(flags, seq, msg, body)
            if reliable:
                self._unacked[seq] = [packet, time.monotonic(), 0]
        self.sock.sendto(packet, self.addr)
        return seq

    def is_acked(self, seq: int) -> bool:
        with self._lock:
            return seq not in self._unacked and seq not in self._gave_up

    def logout(self, timeout: float = 5.0) -> None:
        if not self.closed.is_set():
            s = self.session
            try:
                seq = self.send(MSG_LOGOUT_REQUEST, s.agent_id.bytes + s.session_id.bytes, reliable=True)
                deadline = time.monotonic() + timeout
                acked_at = None
                while not self.logout_reply.wait(0.1):
                    now = time.monotonic()
                    if acked_at is None and self.is_acked(seq):
                        acked_at = now
                    # The region acked the request; some (e.g. OpenSim) close
                    # the circuit without ever sending LogoutReply.
                    if acked_at is not None and now - acked_at > 1.0:
                        break
                    if now > deadline:
                        # Seen with OpenSim, which closes the circuit before its
                        # reply goes out. The logout itself still happens.
                        log.info("region did not confirm the logout")
                        break
            except OSError as e:
                log.warning("logout failed: %s", e)
        self.close()

    def close(self) -> None:
        self._stop.set()
        self._mark_closed(self.close_reason or "closed")
        for t in self._threads:
            t.join(timeout=2)
        self.sock.close()

    # -- internals --------------------------------------------------------

    def _mark_closed(self, reason: str) -> None:
        if not self.closed.is_set():
            self.close_reason = reason
            self.closed.set()

    def _on_ping(self, body: bytes) -> None:
        self.send(MSG_COMPLETE_PING_CHECK, body[:1])

    def _on_region_handshake(self, _body: bytes) -> None:
        s = self.session
        self.send(MSG_REGION_HANDSHAKE_REPLY,
                  s.agent_id.bytes + s.session_id.bytes + struct.pack("<I", 0),
                  reliable=True)

    def _on_kick(self, body: bytes) -> None:
        # TargetBlock(6) + AgentID + SessionID + Reason(var2)
        reason = ""
        try:
            n = struct.unpack_from("<H", body, 38)[0]
            reason = body[40:40 + n].rstrip(b"\x00").decode("utf-8", "replace")
        except struct.error:
            pass
        log.error("kicked by the region: %s", reason or "(no reason given)")
        self._mark_closed(f"kicked: {reason}")

    def _recv_loop(self) -> None:
        while not self._stop.is_set():
            try:
                data, addr = self.sock.recvfrom(65536)
            except (socket.timeout, ConnectionResetError):
                # Windows reports ICMP "port unreachable" as a reset on UDP sockets
                continue
            except OSError:
                break
            if addr != self.addr or len(data) < 7:
                continue
            try:
                flags, seq, msg, body, acks = parse_packet(data)
            except (struct.error, IndexError):
                log.debug("dropping malformed packet")
                continue
            if flags & FLAG_RELIABLE:
                with self._lock:
                    self._pending_acks.append(seq)
            if acks:
                self._handle_acks(acks)
            if msg == MSG_PACKET_ACK:
                count = body[0] if body else 0
                self._handle_acks(list(struct.unpack_from(f"<{count}I", body, 1)))
                continue
            for handler in self._handlers.get(msg, ()):
                try:
                    handler(body)
                except CircuitClosed:
                    pass
                except Exception:  # noqa: BLE001 - never let a handler kill the loop
                    log.exception("error handling message %s", msg)

    def _handle_acks(self, acks: list[int]) -> None:
        with self._lock:
            for a in acks:
                self._unacked.pop(a, None)

    def _maintenance_loop(self) -> None:
        while not self._stop.wait(0.1):
            with self._lock:
                acks, self._pending_acks = self._pending_acks, []
            for i in range(0, len(acks), 255):
                chunk = acks[i:i + 255]
                try:
                    self.send(MSG_PACKET_ACK, bytes([len(chunk)]) + struct.pack(f"<{len(chunk)}I", *chunk))
                except (CircuitClosed, OSError):
                    pass
            now = time.monotonic()
            resend = []
            with self._lock:
                for seq, entry in list(self._unacked.items()):
                    if now - entry[1] < RESEND_AFTER:
                        continue
                    if entry[2] >= MAX_RESENDS:
                        log.debug("giving up on reliable packet %d", seq)
                        del self._unacked[seq]
                        self._gave_up.add(seq)
                        continue
                    entry[0] = bytes([entry[0][0] | FLAG_RESENT]) + entry[0][1:]
                    entry[1] = now
                    entry[2] += 1
                    resend.append(entry[0])
            for packet in resend:
                try:
                    self.sock.sendto(packet, self.addr)
                except OSError:
                    pass


# --------------------------------------------------------------------------
# Asset transfers (TransferRequest / TransferInfo / TransferPacket)
# --------------------------------------------------------------------------

CHANNEL_ASSET = 2
SOURCE_SIM_INV_ITEM = 3
STATUS_OK, STATUS_DONE = 0, 1
STATUS_TEXT = {
    -1: "transfer error",
    -2: "asset not found",
    -3: "insufficient permissions",
    2: "skipped",
    3: "aborted",
}


class TransferError(Exception):
    pass


class _Transfer:
    def __init__(self) -> None:
        self.size: int | None = None
        self.packets: dict[int, bytes] = {}
        self.last_packet: int | None = None
        self.error: str | None = None
        self.done = threading.Event()

    def check_complete(self) -> None:
        if self.last_packet is not None and all(i in self.packets for i in range(self.last_packet + 1)):
            self.done.set()
        elif self.size is not None and sum(map(len, self.packets.values())) >= self.size and self.packets:
            self.done.set()


class TransferManager:
    """Fetches inventory item assets over the UDP transfer system."""

    def __init__(self, circuit: Circuit):
        self.circuit = circuit
        self._transfers: dict[uuid.UUID, _Transfer] = {}
        self._lock = threading.Lock()
        circuit.on(MSG_TRANSFER_INFO, self._on_info)
        circuit.on(MSG_TRANSFER_PACKET, self._on_packet)
        circuit.on(MSG_TRANSFER_ABORT, self._on_abort)

    def fetch_inventory_asset(self, item_id: uuid.UUID, asset_id: uuid.UUID, asset_type: int,
                              owner_id: uuid.UUID, timeout: float = 60.0) -> bytes:
        s = self.circuit.session
        transfer_id = uuid.uuid4()
        t = _Transfer()
        with self._lock:
            self._transfers[transfer_id] = t
        params = (s.agent_id.bytes + s.session_id.bytes + owner_id.bytes + uuid.UUID(int=0).bytes
                  + item_id.bytes + asset_id.bytes + struct.pack("<i", asset_type))
        body = (transfer_id.bytes + struct.pack("<iif", CHANNEL_ASSET, SOURCE_SIM_INV_ITEM, 101.0)
                + pack_var2(params))
        try:
            self.circuit.send(MSG_TRANSFER_REQUEST, body, reliable=True)
            if not t.done.wait(timeout):
                raise TransferError("timed out")
            if t.error:
                raise TransferError(t.error)
            data = b"".join(t.packets[i] for i in sorted(t.packets))
            return data[:t.size] if t.size is not None else data
        finally:
            with self._lock:
                self._transfers.pop(transfer_id, None)

    def _get(self, body: bytes) -> _Transfer | None:
        with self._lock:
            return self._transfers.get(uuid.UUID(bytes=body[:16]))

    def _on_info(self, body: bytes) -> None:
        t = self._get(body)
        if not t:
            return
        _channel, _target, status, size = struct.unpack_from("<iiii", body, 16)
        if status not in (STATUS_OK, STATUS_DONE):
            t.error = STATUS_TEXT.get(status, f"status {status}")
            t.done.set()
            return
        t.size = size
        t.check_complete()

    def _on_packet(self, body: bytes) -> None:
        t = self._get(body)
        if not t:
            return
        _channel, packet, status = struct.unpack_from("<iii", body, 16)
        n = struct.unpack_from("<H", body, 28)[0]
        t.packets[packet] = body[30:30 + n]
        if status == STATUS_DONE:
            t.last_packet = packet
        elif status != STATUS_OK:
            t.error = STATUS_TEXT.get(status, f"status {status}")
            t.done.set()
            return
        t.check_complete()

    def _on_abort(self, body: bytes) -> None:
        t = self._get(body)
        if t:
            t.error = "aborted by region"
            t.done.set()
