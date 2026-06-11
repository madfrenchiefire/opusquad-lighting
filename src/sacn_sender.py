"""sACN (E1.31) DMX sender — multicast UDP to 239.255.x.x."""

import logging
import socket
import struct
import uuid

logger = logging.getLogger(__name__)

# sACN / E1.31 constants
_SACN_PORT = 5568
_PREAMBLE_SIZE = 0x0010
_POSTAMBLE_SIZE = 0x0000
_ACN_PACKET_IDENTIFIER = b"ASC-E1.17\x00\x00\x00"  # 12 bytes
_VECTOR_ROOT_E131_DATA = 0x00000004
_VECTOR_E131_DATA_PACKET = 0x00000002
_VECTOR_DMP_SET_PROPERTY = 0x02
_DMP_ADDRESS_TYPE = 0xA1  # range, relative, one-octet
_DMP_FIRST_PROPERTY_ADDRESS = 0x0000
_DMP_ADDRESS_INCREMENT = 0x0001
_E131_PRIORITY = 100
_SOURCE_NAME_LEN = 64
_UNIVERSE_MIN = 0
_UNIVERSE_MAX = 63999


def _multicast_addr(universe: int) -> str:
    """239.255.{universe>>8}.{universe&0xff} per E1.31 §9.3.1."""
    hi = (universe >> 8) & 0xFF
    lo = universe & 0xFF
    return f"239.255.{hi}.{lo}"


class SACNSender:
    """Send DMX-512 data over sACN (ANSI E1.31) multicast UDP.

    Parameters
    ----------
    universe:
        sACN universe number, 0–63999.  The protocol uses 1-based universe
        numbers in its framing layer, so ``universe + 1`` is placed on the wire.
    source_name:
        UTF-8 label embedded in every packet (truncated to 63 chars + NUL).
    """

    def __init__(
        self,
        universe: int = 0,
        source_name: str = "OpusQuad Lighting",
    ) -> None:
        if not (_UNIVERSE_MIN <= universe <= _UNIVERSE_MAX):
            raise ValueError(f"universe must be 0–{_UNIVERSE_MAX}, got {universe}")

        self.universe = universe
        self._wire_universe = universe + 1          # protocol is 1-based
        self.source_name = source_name
        self._cid: bytes = uuid.uuid4().bytes       # 16-byte random CID
        self._seq: int = 0
        self._dmx = bytearray(512)                 # full universe buffer

        self._multicast_addr = _multicast_addr(universe)
        self._sock = self._open_socket()
        logger.info(
            "SACNSender ready — universe=%d wire=%d multicast=%s",
            universe,
            self._wire_universe,
            self._multicast_addr,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_channels(self, start: int, values: list[int]) -> None:
        """Write *values* into the DMX buffer starting at 0-based *start*."""
        for i, v in enumerate(values):
            idx = start + i
            if 0 <= idx < 512:
                self._dmx[idx] = max(0, min(255, v))

    def send(self) -> None:
        """Build and transmit one sACN data packet."""
        packet = self._build_packet()
        self._sock.sendto(packet, (self._multicast_addr, _SACN_PORT))
        logger.debug(
            "sACN packet sent — universe=%d seq=%d bytes=%d",
            self.universe, self._seq, len(packet),
        )
        self._seq = (self._seq + 1) & 0xFF

    def blackout(self) -> None:
        """Zero all channels and send immediately."""
        self._dmx[:] = b"\x00" * 512
        self.send()
        logger.info("Blackout sent on universe %d", self.universe)

    def close(self) -> None:
        self._sock.close()
        logger.info("SACNSender closed (universe=%d)", self.universe)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _open_socket(self) -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 8)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
        return sock

    def _build_packet(self) -> bytes:
        """Assemble a full E1.31 data packet.

        Layout (byte offsets, all big-endian unless noted):
          0–15   Root layer preamble
          16–37  Root PDU
          38–...  Framing PDU
          ...     DMP PDU
        """
        dmx_data = bytes(self._dmx)             # 512 bytes
        prop_count = len(dmx_data) + 1          # +1 for the start code byte

        # ---- DMP layer (innermost) ----
        # Length field in sACN uses the top 2 bits as flags (0x7 = length included).
        dmp_length = 11 + prop_count            # header(10) + start_code(1) + data(512)
        dmp_flags_length = 0x7000 | dmp_length
        dmp_layer = struct.pack(
            "!HBBHHHx",                         # flags_len, vector, addr_type,
            dmp_flags_length,                   #   first_prop_addr, addr_increment,
            _VECTOR_DMP_SET_PROPERTY,           #   prop_count, (padding x)
            _DMP_ADDRESS_TYPE,
            _DMP_FIRST_PROPERTY_ADDRESS,
            _DMP_ADDRESS_INCREMENT,
            prop_count,
        )
        dmp_layer += b"\x00"                    # DMX512 start code
        dmp_layer += dmx_data

        # ---- Framing layer ----
        source_name_bytes = self.source_name.encode("utf-8")[:63].ljust(
            _SOURCE_NAME_LEN, b"\x00"
        )
        framing_length = 77 + len(dmp_layer)   # fixed header = 77 bytes before DMP
        framing_flags_length = 0x7000 | framing_length
        framing_layer = struct.pack(
            "!IH",
            _VECTOR_E131_DATA_PACKET,
            framing_flags_length,
        )
        # Re-pack in correct field order per E1.31 Table 6-3:
        # flags+length (2), vector (4), source_name (64), priority (1),
        # synchronisation_address (2), sequence_number (1), options (1), universe (2)
        framing_layer = (
            struct.pack("!H", framing_flags_length)
            + struct.pack("!I", _VECTOR_E131_DATA_PACKET)
            + source_name_bytes
            + struct.pack(
                "!BBBBH",
                _E131_PRIORITY,
                0, 0,                           # synchronisation address (2 bytes)
                self._seq,                      # sequence number
                0,                              # options
            )
            + struct.pack("!H", self._wire_universe)
            + dmp_layer
        )

        # ---- Root layer ----
        root_length = 16 + len(framing_layer)
        root_flags_length = 0x7000 | root_length
        root_layer = (
            struct.pack("!H", root_flags_length)
            + struct.pack("!I", _VECTOR_ROOT_E131_DATA)
            + self._cid                         # 16 bytes
            + framing_layer
        )

        # ---- Preamble (16 bytes) ----
        preamble = (
            struct.pack("!HH", _PREAMBLE_SIZE, _POSTAMBLE_SIZE)
            + _ACN_PACKET_IDENTIFIER            # 12 bytes
        )

        return preamble + root_layer
