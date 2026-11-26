"""Minimal Modbus TCP framing helpers.

Frame layout on the DTU <-> cloud channel (standard Modbus TCP / MBAP):
    [transaction id:2][protocol id:2 = 0][length:2][unit id:1][function:1][data...]
The DTU (HF5111B, UartProto=Modbus) converts these frames to Modbus RTU on
the RS485 bus of the heat-pump controller and back.

The DTU also sends a 14 byte ASCII heartbeat "00<MAC>" on the same socket
(e.g. b"0034EAE7000000"); it is not a Modbus frame.
"""

from __future__ import annotations

import re

HEARTBEAT_PREFIX = b"00"
#: ASCII heartbeat sent by the HF5111B: "00" + 12 hex chars (+ optional CR/LF)
_HEARTBEAT_RE = re.compile(rb"^00[0-9A-Fa-f]{12}(?:\r\n|\r|\n)?")
#: MBAP frames are small; anything larger means the stream is desynchronised
_MAX_MBAP_LENGTH = 260

FUNC_READ_HOLDING = 0x03
FUNC_READ_INPUT = 0x04
FUNC_WRITE_SINGLE = 0x06
FUNC_WRITE_MULTIPLE = 0x10


def build_frame(transaction_id: int, pdu: bytes) -> bytes:
    """Wrap a Modbus PDU into an MBAP (Modbus TCP) frame."""
    return (
        bytes(((transaction_id >> 8) & 0xFF, transaction_id & 0xFF))
        + b"\x00\x00"
        + len(pdu).to_bytes(2, "big")
        + pdu
    )


def read_pdu(addr: int, count: int, unit_id: int = 1, func: int = FUNC_READ_HOLDING) -> bytes:
    return bytes(
        (
            unit_id,
            func,
            (addr >> 8) & 0xFF,
            addr & 0xFF,
            (count >> 8) & 0xFF,
            count & 0xFF,
        )
    )


def write_pdu(addr: int, value: int, unit_id: int = 1) -> bytes:
    return bytes(
        (
            unit_id,
            FUNC_WRITE_SINGLE,
            (addr >> 8) & 0xFF,
            addr & 0xFF,
            (value >> 8) & 0xFF,
            value & 0xFF,
        )
    )


def write_multiple_pdu(addr: int, values: list[int], unit_id: int = 1) -> bytes:
    data = b"".join(bytes(((v >> 8) & 0xFF, v & 0xFF)) for v in values)
    return bytes(
        (
            unit_id,
            FUNC_WRITE_MULTIPLE,
            (addr >> 8) & 0xFF,
            addr & 0xFF,
            (len(values) >> 8) & 0xFF,
            len(values) & 0xFF,
            len(data) & 0xFF,
        )
    ) + data


class Frame:
    """A parsed MBAP frame."""

    __slots__ = ("raw", "transaction_id", "pdu")

    def __init__(self, raw: bytes, transaction_id: int, pdu: bytes) -> None:
        self.raw = raw
        self.transaction_id = transaction_id
        self.pdu = pdu

    @property
    def function(self) -> int | None:
        return self.pdu[1] if len(self.pdu) >= 2 else None

    @property
    def is_exception(self) -> bool:
        return self.function is not None and bool(self.function & 0x80)

    def decode_read(self) -> list[int]:
        """Decode a read-holding/input response into a list of 16 bit registers."""
        if self.is_exception or len(self.pdu) < 3:
            raise ValueError("not a read response: %s" % self.raw.hex())
        count = self.pdu[2]
        data = self.pdu[3 : 3 + count]
        if len(data) != count or count % 2:
            raise ValueError("bad byte count in read response: %s" % self.raw.hex())
        return [(data[2 * i] << 8) | data[2 * i + 1] for i in range(count // 2)]

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return "<Frame tid=0x%04x pdu=%s>" % (self.transaction_id, self.pdu.hex())


def parse_frames(buffer: bytes) -> tuple[list[Frame | bytes], bytes, int]:
    """Split a byte stream into frames.

    Returns ``(items, remaining, dropped_bytes)`` where ``items`` contains
    :class:`Frame` objects and raw ``bytes`` for DTU heartbeats.  ``dropped``
    counts bytes that had to be discarded to resynchronise after garbage; those
    are never forwarded to the vendor cloud (that used to be able to confuse
    the vendor session).
    """
    items: list[Frame | bytes] = []
    dropped = 0
    while True:
        valid_mbap = False
        length = 0
        if len(buffer) >= 6:
            protocol = (buffer[2] << 8) | buffer[3]
            length = (buffer[4] << 8) | buffer[5]
            valid_mbap = protocol == 0 and 2 <= length <= _MAX_MBAP_LENGTH
        if valid_mbap:
            total = 6 + length
            if len(buffer) < total:
                break
            transaction_id = (buffer[0] << 8) | buffer[1]
            items.append(Frame(buffer[:total], transaction_id, buffer[6:total]))
            buffer = buffer[total:]
            continue
        # Not a valid MBAP frame: maybe the DTU heartbeat, maybe garbage.
        match = _HEARTBEAT_RE.match(buffer)
        if match is not None:
            items.append(buffer[: match.end()])
            buffer = buffer[match.end():]
            continue
        if buffer[:2] == HEARTBEAT_PREFIX and len(buffer) < 14:
            # possibly an incomplete heartbeat: wait for the rest
            break
        if len(buffer) < 6:
            break
        buffer = buffer[1:]
        dropped += 1
    return items, buffer, dropped
