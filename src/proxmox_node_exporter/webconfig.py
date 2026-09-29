"""TLS and basic-auth configuration for the HTTP endpoint.

The web config file is INI (parsed with the standard library)::

    [tls]
    cert_file = /etc/proxmox-node-exporter/tls.crt
    key_file = /etc/proxmox-node-exporter/tls.key
    # Optional: require client certificates signed by this CA (mutual TLS).
    client_ca_file = /etc/proxmox-node-exporter/client-ca.crt
    # TLSv1.2 (default) or TLSv1.3
    min_version = TLSv1.2

    [basic_auth_users]
    # username = hash produced by `proxmox-node-exporter --hash-password`
    prometheus = pbkdf2_sha256$200000$...$...
"""

from __future__ import annotations

import base64
import binascii
import configparser
import hashlib
import hmac
import logging
import os
import ssl
import stat
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

HASH_ALGORITHM = "pbkdf2_sha256"
DEFAULT_ITERATIONS = 200_000
_MAX_ITERATIONS = 10_000_000


class ConfigError(Exception):
    pass


def hash_password(password: str, iterations: int = DEFAULT_ITERATIONS) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return "$".join(
        (HASH_ALGORITHM, str(iterations), _b64(salt), _b64(digest)),
    )


def _parse_hash(encoded: str) -> tuple[int, bytes, bytes]:
    try:
        algorithm, iterations_text, salt_text, digest_text = encoded.split("$")
        iterations = int(iterations_text)
        salt = base64.b64decode(salt_text, validate=True)
        digest = base64.b64decode(digest_text, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ConfigError("malformed password hash") from exc
    if algorithm != HASH_ALGORITHM:
        raise ConfigError(f"unsupported password hash algorithm {algorithm!r}")
    if not 1 <= iterations <= _MAX_ITERATIONS or len(salt) < 8 or len(digest) < 16:
        raise ConfigError("password hash parameters out of range")
    return iterations, salt, digest


def verify_password(password: str, encoded: str) -> bool:
    try:
        iterations, salt, expected = _parse_hash(encoded)
    except ConfigError:
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(actual, expected)


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


@dataclass
class WebConfig:
    tls_cert_file: str | None = None
    tls_key_file: str | None = None
    tls_client_ca_file: str | None = None
    tls_min_version: str = "TLSv1.2"
    users: dict[str, str] = field(default_factory=dict)

    @property
    def tls_enabled(self) -> bool:
        return bool(self.tls_cert_file)


_TLS_KEYS = {"cert_file", "key_file", "client_ca_file", "min_version"}


def load_web_config(path: str) -> WebConfig:
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str  # type: ignore[assignment,method-assign]  # keep usernames' case
    try:
        with open(path, encoding="utf-8") as fh:
            mode = os.fstat(fh.fileno()).st_mode
            parser.read_file(fh)
    except (OSError, configparser.Error) as exc:
        raise ConfigError(f"cannot read web config {path}: {exc}") from exc

    config = WebConfig()
    if parser.defaults():  # configparser would merge [DEFAULT] into every section
        raise ConfigError(f"{path}: unknown section [{parser.default_section}]")
    for section in parser.sections():
        if section not in ("tls", "basic_auth_users"):
            raise ConfigError(f"{path}: unknown section [{section}]")

    if parser.has_section("tls"):
        tls = parser["tls"]
        unknown = set(tls) - _TLS_KEYS
        if unknown:
            raise ConfigError(f"{path}: unknown [tls] keys: {', '.join(sorted(unknown))}")
        config.tls_cert_file = tls.get("cert_file") or None
        config.tls_key_file = tls.get("key_file") or None
        config.tls_client_ca_file = tls.get("client_ca_file") or None
        config.tls_min_version = tls.get("min_version", "TLSv1.2")
        if bool(config.tls_cert_file) != bool(config.tls_key_file):
            raise ConfigError(f"{path}: [tls] needs both cert_file and key_file")
        if config.tls_client_ca_file and not config.tls_cert_file:
            raise ConfigError(f"{path}: client_ca_file requires cert_file and key_file")
        if config.tls_min_version not in ("TLSv1.2", "TLSv1.3"):
            raise ConfigError(f"{path}: min_version must be TLSv1.2 or TLSv1.3")

    if parser.has_section("basic_auth_users"):
        for user, encoded in parser["basic_auth_users"].items():
            if not user or ":" in user:
                raise ConfigError(f"{path}: invalid username {user!r}")
            try:
                _parse_hash(encoded)
            except ConfigError as exc:
                raise ConfigError(f"{path}: user {user!r}: {exc}") from None
            config.users[user] = encoded
        if config.users and stat.S_IMODE(mode) & 0o007:
            log.warning("%s is world-readable; restrict it with chmod 600", path)
    return config


class _TokenBucket:
    def __init__(self, rate: float) -> None:
        self.rate = rate
        self.capacity = max(1.0, rate)
        self.tokens = self.capacity
        self.last = time.monotonic()

    def take(self) -> bool:
        now = time.monotonic()
        self.tokens = min(self.capacity, self.tokens + (now - self.last) * self.rate)
        self.last = now
        if self.tokens < 1:
            return False
        self.tokens -= 1
        return True


class BasicAuth:
    """Checks ``Authorization: Basic`` headers against PBKDF2 hashes.

    The password that last verified for each configured user is remembered
    and compared in constant time, so regular scrapes do not pay the
    key-derivation cost (it is never stored as a fast hash). Checking
    credentials that do not match it is rate limited per client address and
    overall, so failed logins can neither burn the CPU nor lock out other
    clients.
    """

    _MAX_CLIENTS = 1024

    def __init__(
        self,
        users: dict[str, str],
        attempts_per_second: float = 5.0,
        total_attempts_per_second: float = 20.0,
    ) -> None:
        self._users = dict(users)
        self._verified: dict[str, bytes] = {}  # at most one entry per configured user
        self._lock = threading.Lock()
        self._rate = attempts_per_second
        self._clients: OrderedDict[str, _TokenBucket] = OrderedDict()
        self._global = _TokenBucket(max(attempts_per_second, total_attempts_per_second))
        # Unknown users are checked against a dummy hash with the same work
        # factor as the real ones, so timing does not reveal valid usernames.
        iterations = max(
            (_parse_hash(h)[0] for h in self._users.values()), default=DEFAULT_ITERATIONS
        )
        self._dummy = hash_password(os.urandom(8).hex(), iterations=iterations)

    def check(self, header: str | None, client: str = "") -> bool | None:
        """True if authorised, False if not, None if rate limited."""
        if not header or not header.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(header[6:].strip(), validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError):  # ValueError: non-ASCII
            return False
        user, sep, password = decoded.partition(":")
        if not sep:
            return False
        secret = password.encode("utf-8")
        with self._lock:
            cached = self._verified.get(user)
        if cached is not None and hmac.compare_digest(cached, secret):
            return True
        with self._lock:
            if not self._take_token(client):
                return None
        encoded = self._users.get(user)
        if encoded is None:
            verify_password(password, self._dummy)  # same cost as a real check
            return False
        if not verify_password(password, encoded):
            return False
        with self._lock:
            self._verified[user] = secret
        return True

    def _take_token(self, client: str) -> bool:
        bucket = self._clients.get(client)
        if bucket is None:
            bucket = self._clients[client] = _TokenBucket(self._rate)
            while len(self._clients) > self._MAX_CLIENTS:
                self._clients.popitem(last=False)
        else:
            self._clients.move_to_end(client)
        return bucket.take() and self._global.take()


class TLSContextProvider:
    """Builds the server SSL context and reloads it when the files change,
    so renewed certificates are picked up without a restart."""

    _CHECK_EVERY = 30.0

    def __init__(self, config: WebConfig) -> None:
        self._config = config
        self._lock = threading.Lock()
        self._checked = time.monotonic()
        self._stamp = self._file_stamp()
        self._context = self._build()

    def _paths(self) -> tuple[str, ...]:
        c = self._config
        return tuple(p for p in (c.tls_cert_file, c.tls_key_file, c.tls_client_ca_file) if p)

    def _file_stamp(self) -> tuple[tuple[float, int], ...]:
        stamp = []
        for path in self._paths():
            try:
                st = os.stat(path)
                stamp.append((st.st_mtime, st.st_size))
            except OSError:
                stamp.append((0.0, -1))
        return tuple(stamp)

    def _build(self) -> ssl.SSLContext:
        c = self._config
        if not c.tls_cert_file or not c.tls_key_file:
            raise ConfigError("TLS needs cert_file and key_file")
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = (
            ssl.TLSVersion.TLSv1_3 if c.tls_min_version == "TLSv1.3" else ssl.TLSVersion.TLSv1_2
        )
        try:
            # An empty password makes an encrypted key fail instead of prompting.
            ctx.load_cert_chain(c.tls_cert_file, c.tls_key_file, password=b"")
            if c.tls_client_ca_file:
                ctx.load_verify_locations(cafile=c.tls_client_ca_file)
                ctx.verify_mode = ssl.CERT_REQUIRED
        except (OSError, ssl.SSLError) as exc:
            raise ConfigError(f"cannot load TLS material: {exc}") from exc
        return ctx

    def get(self) -> ssl.SSLContext:
        with self._lock:
            now = time.monotonic()
            if now - self._checked >= self._CHECK_EVERY:
                self._checked = now
                stamp = self._file_stamp()
                if stamp != self._stamp:
                    try:
                        self._context = self._build()
                        self._stamp = stamp
                        log.info("reloaded TLS certificate")
                    except ConfigError as exc:
                        log.error("keeping previous TLS certificate: %s", exc)
            return self._context
