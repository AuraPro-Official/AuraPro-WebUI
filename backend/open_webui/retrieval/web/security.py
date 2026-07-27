from __future__ import annotations

import ipaddress
import logging
import socket
import urllib.parse
from collections.abc import Sequence

import aiohttp
import aiohttp.resolver
import urllib3.connection
import urllib3.connectionpool
import validators
from open_webui.config import ENABLE_LOCAL_WEB_FETCH, WEB_FETCH_FILTER_LIST
from open_webui.constants import ERROR_MESSAGES
from open_webui.env import AIOHTTP_CLIENT_TIMEOUT
from open_webui.utils.misc import is_host_allowed
from requests.adapters import HTTPAdapter

log = logging.getLogger(__name__)


def resolve_hostname(hostname):
    if not hostname:
        return [], []

    addr_info = socket.getaddrinfo(hostname, None)
    ipv4_addresses = [info[4][0] for info in addr_info if info[0] == socket.AF_INET]
    ipv6_addresses = [info[4][0] for info in addr_info if info[0] == socket.AF_INET6]
    return ipv4_addresses, ipv6_addresses


def validate_url(url: str | Sequence[str]):
    if isinstance(url, str):
        if isinstance(validators.url(url), validators.ValidationError):
            raise ValueError(ERROR_MESSAGES.INVALID_URL)

        if any(ch in url for ch in ('\\', '\t', '\n', '\r')):
            log.warning('Blocked URL with parser-confusing characters')
            raise ValueError(ERROR_MESSAGES.INVALID_URL)

        parsed_url = urllib.parse.urlparse(url)
        if parsed_url.scheme not in ['http', 'https']:
            log.warning('Blocked non-HTTP(S) protocol: %s', parsed_url.scheme)
            raise ValueError(ERROR_MESSAGES.INVALID_URL)

        if WEB_FETCH_FILTER_LIST and not is_host_allowed(parsed_url.hostname, WEB_FETCH_FILTER_LIST):
            log.warning('URL host blocked by filter list: %s', parsed_url.hostname)
            raise ValueError(ERROR_MESSAGES.INVALID_URL)

        if not ENABLE_LOCAL_WEB_FETCH:
            ipv4_addresses, ipv6_addresses = resolve_hostname(parsed_url.hostname)
            for ip in ipv4_addresses + ipv6_addresses:
                if not ipaddress.ip_address(ip).is_global:
                    raise ValueError(ERROR_MESSAGES.INVALID_URL)

        return True

    if isinstance(url, Sequence):
        return all(validate_url(item) for item in url)

    return False


def _ssrf_safe_new_conn(self):  # noqa: C901
    host = getattr(self, '_dns_host', self.host)
    port = self.port
    infos = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
    if not infos:
        raise OSError(f'getaddrinfo for {host!r} returned empty list')
    if not ENABLE_LOCAL_WEB_FETCH:
        for _, _, _, _, address in infos:
            if not ipaddress.ip_address(address[0]).is_global:
                raise ValueError(ERROR_MESSAGES.INVALID_URL)

    error = None
    for family, kind, protocol, _, address in infos:
        sock = None
        try:
            sock = socket.socket(family, kind, protocol)
            if self.timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                sock.settimeout(self.timeout)
            if getattr(self, 'source_address', None):
                sock.bind(self.source_address)
            for option in getattr(self, 'socket_options', None) or ():
                sock.setsockopt(*option)
            sock.connect(address)
            return sock
        except OSError as exc:
            error = exc
            if sock is not None:
                sock.close()
    raise error or OSError(f'connect to {host!r}:{port} failed')


class _SafeHTTPConn(urllib3.connection.HTTPConnection):
    _new_conn = _ssrf_safe_new_conn


class _SafeHTTPSConn(urllib3.connection.HTTPSConnection):
    _new_conn = _ssrf_safe_new_conn


class _SafeHTTPPool(urllib3.connectionpool.HTTPConnectionPool):
    ConnectionCls = _SafeHTTPConn


class _SafeHTTPSPool(urllib3.connectionpool.HTTPSConnectionPool):
    ConnectionCls = _SafeHTTPSConn


class _SSRFSafeAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        super().init_poolmanager(*args, **kwargs)
        self.poolmanager.pool_classes_by_scheme = {
            'http': _SafeHTTPPool,
            'https': _SafeHTTPSPool,
        }


class _SSRFSafeResolver(aiohttp.resolver.DefaultResolver):
    async def resolve(self, host, port=0, family=socket.AF_INET):
        results = await super().resolve(host, port, family)
        if not ENABLE_LOCAL_WEB_FETCH:
            for entry in results:
                if not ipaddress.ip_address(entry['host']).is_global:
                    raise ValueError(ERROR_MESSAGES.INVALID_URL)
        return results


def get_ssrf_safe_session() -> aiohttp.ClientSession:
    return aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(resolver=_SSRFSafeResolver()),
        timeout=aiohttp.ClientTimeout(total=AIOHTTP_CLIENT_TIMEOUT),
        trust_env=True,
    )
