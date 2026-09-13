"""Egress proxy for the stateful browser (spec §6.1).

`fetch_page` vets one URL and pins its address with `--host-resolver-rules`.
A persistent, interactive browser cannot work that way: links, redirects,
scripts, iframes and subresources all open connections the daemon never sees
as a URL. So every connection the browser makes is forced through this proxy
(`--proxy-server`, with `<-loopback>` removing Chromium's implicit localhost
bypass), and the proxy — not the page — decides where a socket may go:

* the host is resolved HERE, every answer must be a global address, and the
  proxy then connects to the address it vetted, so a rebinding DNS answer can
  never swap an internal host in after the check;
* only web ports are reachable, so the browser cannot be turned into a relay
  for SMTP or anything else.

It speaks just enough HTTP/1.1 for a browser: `CONNECT host:port` for TLS and
WebSockets, and absolute-URI requests for plain http. Stdlib only.
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
from typing import Callable
from urllib.parse import urlparse, urlsplit

from ares.core.utils.logging import get_logger

logger = get_logger(__name__)

ALLOWED_PORTS = frozenset({80, 443, 8080, 8443})
MAX_HEAD_BYTES = 16384
HEAD_TIMEOUT_S = 15
CONNECT_TIMEOUT_S = 15
# A hostile page must not be able to exhaust the daemon's fds with tunnels, or
# pin them open forever: the proxy runs inside the daemon, not the browser uid.
MAX_CONNECTIONS = 128
IDLE_TIMEOUT_S = 600
_CHUNK = 65536
_HOP_HEADERS = ("proxy-connection", "proxy-authorization", "connection", "keep-alive")


def _is_forbidden_ip(ip: str) -> bool:
    """True for any address the daemon must not be able to reach via a URL.

    Blocks the host-private TAP link (Home Assistant, dashboard, updater hook),
    loopback, link-local incl. cloud metadata, and every other non-global range.
    """
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True
    return not addr.is_global


def _split_host_port(authority: str, default_port: int) -> tuple[str, int]:
    """Split `host:port` / `[v6]:port`; raises ValueError on garbage."""
    parts = urlsplit("//" + authority)
    if not parts.hostname:
        raise ValueError("no host")
    return parts.hostname, parts.port or default_port


async def vet_destination(
    host: str,
    port: int,
    is_forbidden: Callable[[str], bool] = _is_forbidden_ip,
    allowed_ports: frozenset[int] = ALLOWED_PORTS,
) -> tuple[list[str], str | None]:
    """Resolve `host`; return (addresses, None) or ([], refusal reason)."""
    if port not in allowed_ports:
        return [], f"port {port} is not a web port"
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        return [], f"could not resolve {host}: {e}"
    addresses = [info[4][0] for info in infos]
    if not addresses:
        return [], f"could not resolve {host}"
    for ip in addresses:
        if is_forbidden(ip):
            # Any private answer rejects the whole name (mixed-answer bypass).
            return [], f"{host} resolves to a private/internal address"
    return addresses, None


async def validate_url(raw: str) -> str | None:
    """Return a refusal reason for a model/operator-supplied URL, else None."""
    raw = (raw or "").strip()
    if not raw or any(ch.isspace() or ord(ch) < 32 for ch in raw):
        return "a URL without whitespace is required"
    parsed = urlparse(raw)
    if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
        return "only http(s) URLs with a host can be opened"
    try:
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    except ValueError:
        return "the URL has an invalid port"
    # The proxy enforces this on every connection anyway; checking up front
    # just gives the model/operator a clear reason instead of a tunnel error.
    _, reason = await vet_destination(parsed.hostname, port)
    return reason


class EgressProxy:
    """A local HTTP proxy that only ever connects to public web addresses."""

    def __init__(
        self,
        is_forbidden: Callable[[str], bool] = _is_forbidden_ip,
        allowed_ports: frozenset[int] = ALLOWED_PORTS,
    ) -> None:
        """Store the vetting policy (injectable for tests)."""
        self.is_forbidden = is_forbidden
        self.allowed_ports = allowed_ports
        self._server: asyncio.base_events.Server | None = None
        self._conns: set[asyncio.Task] = set()
        self.port: int | None = None

    async def start(self) -> int:
        """Listen on an ephemeral loopback port; return the port."""
        self._server = await asyncio.start_server(self._accept, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self.port

    async def aclose(self) -> None:
        """Stop listening and drop every open tunnel."""
        if self._server is not None:
            self._server.close()
            self._server = None
        for task in list(self._conns):
            task.cancel()
        if self._conns:
            await asyncio.gather(*self._conns, return_exceptions=True)
        self._conns.clear()

    async def _accept(self, reader, writer) -> None:
        if len(self._conns) >= MAX_CONNECTIONS:
            await self._refuse(writer, 503, "too many open connections")
            writer.close()
            return
        task = asyncio.current_task()
        if task is not None:
            self._conns.add(task)
        try:
            await self._handle(reader, writer)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # a bad connection must never hurt the daemon
            logger.debug("browser proxy: connection error: %s", e)
        finally:
            writer.close()
            if task is not None:
                self._conns.discard(task)

    async def vet(self, host: str, port: int) -> tuple[list[str], str | None]:
        """Resolve `host`; return (addresses, None) or ([], refusal reason)."""
        return await vet_destination(host, port, self.is_forbidden, self.allowed_ports)

    async def _handle(self, reader, writer) -> None:
        try:
            head = await asyncio.wait_for(
                reader.readuntil(b"\r\n\r\n"), timeout=HEAD_TIMEOUT_S
            )
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError):
            return
        if len(head) > MAX_HEAD_BYTES:
            await self._refuse(writer, 431, "request head too large")
            return

        lines = head.decode("latin-1").split("\r\n")
        try:
            method, target, version = lines[0].split(" ", 2)
        except ValueError:
            await self._refuse(writer, 400, "bad request line")
            return

        if method.upper() == "CONNECT":
            try:
                host, port = _split_host_port(target, 443)
            except ValueError:
                await self._refuse(writer, 400, "bad CONNECT target")
                return
            forward = None
        else:
            parts = urlsplit(target)
            if parts.scheme.lower() != "http" or not parts.hostname:
                await self._refuse(writer, 400, "only http:// absolute URIs are proxied")
                return
            host, port = parts.hostname, parts.port or 80
            path = parts.path or "/"
            if parts.query:
                path += "?" + parts.query
            headers = [
                h for h in lines[1:]
                if h and h.split(":", 1)[0].strip().lower() not in _HOP_HEADERS
            ]
            forward = (
                f"{method} {path} {version}\r\n"
                + "".join(h + "\r\n" for h in headers)
                + "Connection: close\r\n\r\n"
            ).encode("latin-1")

        addresses, reason = await self.vet(host, port)
        if reason:
            logger.info("browser proxy: refused %s:%s (%s)", host, port, reason)
            await self._refuse(writer, 403, reason)
            return

        upstream = None
        for ip in addresses:
            try:
                upstream = await asyncio.wait_for(
                    asyncio.open_connection(ip, port), timeout=CONNECT_TIMEOUT_S
                )
                break
            except (OSError, asyncio.TimeoutError):
                continue
        if upstream is None:
            await self._refuse(writer, 502, f"could not connect to {host}")
            return
        up_reader, up_writer = upstream

        try:
            if forward is None:
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await writer.drain()
            else:
                up_writer.write(forward)
                await up_writer.drain()
            await _pipe_both(reader, writer, up_reader, up_writer)
        finally:
            up_writer.close()

    @staticmethod
    async def _refuse(writer, status: int, reason: str) -> None:
        body = f"ARES browser proxy refused this connection: {reason}\n".encode()
        writer.write(
            f"HTTP/1.1 {status} Refused\r\nContent-Type: text/plain\r\n"
            f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
            + body
        )
        try:
            await writer.drain()
        except (ConnectionError, OSError):
            pass


async def _copy(reader, writer) -> None:
    try:
        while True:
            chunk = await asyncio.wait_for(reader.read(_CHUNK), timeout=IDLE_TIMEOUT_S)
            if not chunk:
                break
            writer.write(chunk)
            await writer.drain()
    except (ConnectionError, OSError, asyncio.TimeoutError):
        pass
    finally:
        try:
            if writer.can_write_eof():
                writer.write_eof()
        except (OSError, RuntimeError):
            pass


async def _pipe_both(c_reader, c_writer, u_reader, u_writer) -> None:
    """Shuttle bytes both ways until both directions have closed."""
    await asyncio.gather(_copy(c_reader, u_writer), _copy(u_reader, c_writer))
