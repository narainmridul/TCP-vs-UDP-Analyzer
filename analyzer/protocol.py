"""Wire format shared by client and server.

Every message, on TCP and UDP alike, is a fixed 24-byte header followed by a
payload:

    offset  size  field
    0       2     magic      b"TU"
    2       4     session    random id for one test run
    6       4     seq        0, 1, 2, ... (lets the server detect loss/reorder/dups)
    10      8     send_ts    sender wall clock, seconds (float64)
    18      2     length     payload length in bytes
    20      4     crc32      CRC-32 of the payload (detects corruption)

UDP carries one message per datagram. TCP is a byte stream, so the receiver
reads the header first and then exactly `length` payload bytes (length-prefixed
framing).
"""

import os
import struct
import zlib

MAGIC = b"TU"
HEADER = struct.Struct("!2sIIdHI")
HEADER_SIZE = HEADER.size  # 24
MAX_PAYLOAD = 65_507 - HEADER_SIZE  # largest that still fits in one UDP datagram


def make_payload(size: int) -> bytes:
    return os.urandom(size)


def encode(session: int, seq: int, send_ts: float, payload: bytes) -> bytes:
    return HEADER.pack(MAGIC, session, seq, send_ts, len(payload), zlib.crc32(payload)) + payload


def decode_header(buf: bytes):
    """Return (session, seq, send_ts, length, crc) or None if not one of ours."""
    if len(buf) < HEADER_SIZE:
        return None
    magic, session, seq, send_ts, length, crc = HEADER.unpack_from(buf)
    if magic != MAGIC:
        return None
    return session, seq, send_ts, length, crc


def payload_ok(payload: bytes, length: int, crc: int) -> bool:
    return len(payload) == length and zlib.crc32(payload) == crc
