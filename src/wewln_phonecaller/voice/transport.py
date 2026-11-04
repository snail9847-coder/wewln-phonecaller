"""The audio connection of one call: what the caller says comes in, what wewln says goes out.

- `AudioSocketServer`: Asterisk's AudioSocket protocol over TCP. Asterisk connects to wewln
  (ARI `externalMedia`, encapsulation audiosocket) and first sends the UUID wewln issued for
  that call; a connection with any other UUID is closed at once. Each message is a 3-byte
  header (kind, payload length as 16-bit big-endian) and the payload: 0x00 hangup, 0x01 UUID,
  0x03 DTMF digit, 0x10 audio (8 kHz 16-bit mono PCM), 0xff error. Asterisk 20-22 send and
  accept only 0x10 audio, so the call's audio is always 8 kHz `slin`.
- `memory_pair`: two connected in-memory ends, for tests and simulated calls.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import struct
import uuid as uuidlib
from abc import ABC, abstractmethod

log = logging.getLogger("wewln.voice.transport")

KIND_HANGUP = 0x00
KIND_UUID = 0x01
KIND_DTMF = 0x03
KIND_AUDIO = 0x10
KIND_ERROR = 0xFF
MAX_PAYLOAD = 0xFFFF
HELLO_TIMEOUT_S = 5.0
WRITE_TIMEOUT_S = 5.0


class MediaStream(ABC):
    """Audio of one answered call (8 kHz 16-bit mono PCM both ways)."""

    @abstractmethod
    async def read(self) -> bytes | None:
        """The next piece of the caller's audio; None once the call's audio has ended."""

    @abstractmethod
    async def write(self, pcm: bytes) -> None:
        """Send audio to the caller now (the engine paces it in real time)."""

    @abstractmethod
    async def close(self) -> None:
        """End the audio connection (the call itself is ended by the call service)."""

    @property
    @abstractmethod
    def closed(self) -> bool: ...


def header(kind: int, length: int) -> bytes:
    return struct.pack(">BH", kind, length)


def audio_message(pcm: bytes) -> bytes:
    if len(pcm) > MAX_PAYLOAD:
        raise ValueError("an AudioSocket message carries at most 65535 bytes")
    return header(KIND_AUDIO, len(pcm)) + pcm


def uuid_message(value: str) -> bytes:
    return header(KIND_UUID, 16) + uuidlib.UUID(value).bytes


HANGUP_MESSAGE = header(KIND_HANGUP, 0)


async def read_message(reader: asyncio.StreamReader) -> tuple[int, bytes] | None:
    """(kind, payload), or None at the end of the stream."""
    try:
        head = await reader.readexactly(3)
        kind, length = struct.unpack(">BH", head)
        payload = await reader.readexactly(length) if length else b""
    except (asyncio.IncompleteReadError, ConnectionError, OSError):
        return None
    return kind, payload


class AudioSocketConnection(MediaStream):
    """One accepted AudioSocket connection. A reader task takes every message off the socket
    at once, so that waiting for audio can be cancelled without losing the stream's place."""

    MAX_QUEUED = 1500  # 30 s of 20 ms frames; older audio is dropped if nobody reads

    def __init__(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, call_uuid: str
    ) -> None:
        self.uuid = call_uuid
        self._reader = reader
        self._writer = writer
        self._closed = False
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._pump = asyncio.create_task(self._read_messages())
        self.dtmf: list[str] = []
        self.bytes_in = 0
        self.bytes_out = 0
        # Closed by this end (not because the other end hung up).
        self.closed_here = False

    @property
    def closed(self) -> bool:
        return self._closed

    async def _read_messages(self) -> None:
        try:
            while True:
                message = await read_message(self._reader)
                if message is None:
                    break
                kind, payload = message
                if kind == KIND_AUDIO:
                    self.bytes_in += len(payload)
                    if self._queue.qsize() >= self.MAX_QUEUED:
                        with contextlib.suppress(asyncio.QueueEmpty):
                            self._queue.get_nowait()
                    self._queue.put_nowait(payload)
                elif kind == KIND_HANGUP:
                    break
                elif kind == KIND_DTMF and payload:
                    self.dtmf.append(payload[:1].decode("ascii", "replace"))
                elif kind == KIND_ERROR:
                    log.warning("audiosocket_error", extra={"code": payload[:1].hex()})
                    break
        finally:
            self._closed = True
            self._queue.put_nowait(None)

    async def read(self) -> bytes | None:
        if self._closed and self._queue.empty():
            return None
        item = await self._queue.get()
        if item is None:
            self._queue.put_nowait(None)  # every later read ends too
        return item

    async def write(self, pcm: bytes) -> None:
        if self._closed:
            return
        try:
            for start in range(0, len(pcm), MAX_PAYLOAD - 1):
                chunk = pcm[start : start + MAX_PAYLOAD - 1]
                self._writer.write(audio_message(chunk))
                self.bytes_out += len(chunk)
            await asyncio.wait_for(self._writer.drain(), WRITE_TIMEOUT_S)
        except (ConnectionError, OSError, RuntimeError, TimeoutError):
            self._closed = True

    async def close(self) -> None:
        if not self._closed:
            self.closed_here = True
            with contextlib.suppress(ConnectionError, OSError, RuntimeError):
                self._writer.write(HANGUP_MESSAGE)
        self._closed = True
        with contextlib.suppress(ConnectionError, OSError, RuntimeError):
            self._writer.close()
        with contextlib.suppress(ConnectionError, OSError, RuntimeError, TimeoutError):
            await asyncio.wait_for(self._writer.wait_closed(), 2.0)
        self._pump.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._pump


def _peer_allowed(peer: object) -> bool:
    """Only this computer and private networks (WSL2, a LAN) may connect."""
    host = peer[0] if isinstance(peer, tuple) and peer else None
    if not isinstance(host, str):
        return False
    try:
        address = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return False
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None:
        address = mapped
    return address.is_loopback or address.is_private or address.is_link_local


class AudioSocketServer:
    """Accepts Asterisk's AudioSocket connections for the calls wewln expects (by UUID)."""

    def __init__(self, host: str = "127.0.0.1", port: int = 9092) -> None:
        self.host = host
        self.port = port
        self._server: asyncio.Server | None = None
        self._pending: dict[str, asyncio.Future[AudioSocketConnection]] = {}
        self._open: set[AudioSocketConnection] = set()
        self.rejected = 0

    @property
    def running(self) -> bool:
        return self._server is not None

    async def start(self) -> None:
        if self._server is not None:
            return
        self._server = await asyncio.start_server(self._handle, self.host, self.port)
        sockets = self._server.sockets or ()
        if sockets and self.port == 0:
            self.port = sockets[0].getsockname()[1]
        log.info("audiosocket_listening", extra={"host": self.host, "port": self.port})

    def expect(self) -> tuple[str, asyncio.Future[AudioSocketConnection]]:
        """A new UUID for one call and the future of its connection."""
        call_uuid = str(uuidlib.uuid4())
        future: asyncio.Future[AudioSocketConnection] = asyncio.get_running_loop().create_future()
        self._pending[call_uuid] = future
        return call_uuid, future

    def forget(self, call_uuid: str, error: BaseException | None = None) -> None:
        """Stop expecting the call's connection; whoever waits for it gets `error` (or a
        cancellation when none is given)."""
        future = self._pending.pop(call_uuid, None)
        if future is not None and not future.done():
            if error is None:
                future.cancel()
            else:
                future.set_exception(error)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        if not _peer_allowed(peer):
            self.rejected += 1
            log.warning("audiosocket_peer_rejected")
            writer.close()
            return
        try:
            message = await asyncio.wait_for(read_message(reader), HELLO_TIMEOUT_S)
        except TimeoutError:
            message = None
        call_uuid = None
        if message is not None and message[0] == KIND_UUID and len(message[1]) == 16:
            call_uuid = str(uuidlib.UUID(bytes=message[1]))
        future = self._pending.pop(call_uuid, None) if call_uuid else None
        if future is None or future.done():
            self.rejected += 1
            log.warning("audiosocket_unknown_connection")
            with contextlib.suppress(ConnectionError, OSError, RuntimeError):
                writer.write(HANGUP_MESSAGE)
                writer.close()
            return
        connection = AudioSocketConnection(reader, writer, call_uuid or "")
        self._open.add(connection)
        future.set_result(connection)

    def release(self, connection: AudioSocketConnection) -> None:
        self._open.discard(connection)

    async def close(self) -> None:
        for future in self._pending.values():
            if not future.done():
                future.cancel()
        self._pending.clear()
        for connection in list(self._open):
            await connection.close()
        self._open.clear()
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._server.wait_closed(), 2.0)
            self._server = None


class MemoryMedia(MediaStream):
    """One end of an in-memory audio connection (see memory_pair)."""

    def __init__(self) -> None:
        self._inbox: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.peer: MemoryMedia | None = None
        self._closed = False
        # Closed by this end (not because the other end went away).
        self.closed_here = False
        self.written: list[bytes] = []

    @property
    def closed(self) -> bool:
        return self._closed

    async def read(self) -> bytes | None:
        if self._closed and self._inbox.empty():
            return None
        item = await self._inbox.get()
        if item is None:
            self._closed = True
        return item

    async def write(self, pcm: bytes) -> None:
        if self._closed or self.peer is None or self.peer._closed:
            return
        self.written.append(pcm)
        self.peer._inbox.put_nowait(pcm)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.closed_here = True
        self._inbox.put_nowait(None)
        if self.peer is not None and not self.peer._closed:
            self.peer._inbox.put_nowait(None)


def memory_pair() -> tuple[MemoryMedia, MemoryMedia]:
    """(the call's end for the engine, the phone's end for a simulated caller)."""
    call, phone = MemoryMedia(), MemoryMedia()
    call.peer, phone.peer = phone, call
    return call, phone
