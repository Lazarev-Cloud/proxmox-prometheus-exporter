"""Tests for password hashing, the INI web config, basic auth and TLS reloading."""

from __future__ import annotations

import base64
import logging
import os
import shutil
import ssl
import subprocess
import time
from pathlib import Path
from typing import Callable

import pytest

from proxmox_node_exporter import webconfig
from proxmox_node_exporter.webconfig import (
    DEFAULT_ITERATIONS,
    BasicAuth,
    ConfigError,
    TLSContextProvider,
    WebConfig,
    _parse_hash,
    hash_password,
    load_web_config,
    verify_password,
)

FAST = 1000  # PBKDF2 iterations for test hashes
LOGGER = "proxmox_node_exporter.webconfig"
USERS = {
    "prometheus": hash_password("s3cret-pass", iterations=FAST),
    "Grafana": hash_password("with:colon", iterations=FAST),
}


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def _basic(user: str, password: str) -> str:
    return "Basic " + _b64(f"{user}:{password}".encode())


SALT = _b64(b"s" * 16)
DIGEST = _b64(b"d" * 32)


# -- certificates -----------------------------------------------------------


def _openssl(*args: str | Path) -> None:
    subprocess.run(["openssl", *map(str, args)], check=True, capture_output=True)


def _make_ca(directory: Path, name: str) -> tuple[Path, Path]:
    crt, key = directory / f"{name}.crt", directory / f"{name}.key"
    _openssl(
        "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
        "-keyout", key, "-out", crt, "-days", "2", "-subj", f"/CN={name}",
    )  # fmt: skip
    return crt, key


def _make_leaf(directory: Path, name: str, ca: tuple[Path, Path], serial: int) -> tuple[Path, Path]:
    crt, key = directory / f"{name}.crt", directory / f"{name}.key"
    csr, ext = directory / f"{name}.csr", directory / f"{name}.ext"
    ext.write_text(
        "basicConstraints=critical,CA:FALSE\n"
        "keyUsage=critical,digitalSignature\n"
        "extendedKeyUsage=serverAuth,clientAuth\n"
        "subjectAltName=IP:127.0.0.1,DNS:localhost\n"
        "authorityKeyIdentifier=keyid\n"
        "subjectKeyIdentifier=hash\n"
    )
    _openssl(
        "req", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
        "-keyout", key, "-out", csr, "-subj", f"/CN={name}",
    )  # fmt: skip
    _openssl(
        "x509", "-req", "-in", csr, "-CA", ca[0], "-CAkey", ca[1], "-set_serial", str(serial),
        "-days", "2", "-extfile", ext, "-out", crt,
    )  # fmt: skip
    return crt, key


@pytest.fixture(scope="module")
def certs(tmp_path_factory: pytest.TempPathFactory) -> dict[str, tuple[Path, Path]]:
    directory = tmp_path_factory.mktemp("certs")
    ca = _make_ca(directory, "test-ca")
    return {
        "ca": ca,
        "a": _make_leaf(directory, "server-a", ca, 2),
        "b": _make_leaf(directory, "server-b", ca, 3),
    }


def _install(pair: tuple[Path, Path], crt: Path, key: Path, bump: float = 5.0) -> None:
    """Copy a cert/key pair into place, making sure the mtime visibly changes."""
    for src, dst in zip(pair, (crt, key)):
        shutil.copyfile(src, dst)
        _bump_mtime(dst, bump)


def _bump_mtime(path: Path, seconds: float) -> None:
    st = os.stat(path)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + int(seconds * 1e9)))


# -- password hashes ---------------------------------------------------------


def test_hash_round_trip() -> None:
    encoded = hash_password("correct horse", iterations=FAST)
    algorithm, iterations, salt, digest = encoded.split("$")
    assert algorithm == "pbkdf2_sha256"
    assert iterations == str(FAST)
    assert len(base64.b64decode(salt)) == 16
    assert len(base64.b64decode(digest)) == 32
    assert verify_password("correct horse", encoded) is True


@pytest.mark.parametrize("attempt", ["correct horsE", "correct horse ", "", "correct"])
def test_wrong_password(attempt: str) -> None:
    assert verify_password(attempt, hash_password("correct horse", iterations=FAST)) is False


def test_salt_is_random_and_unicode_works() -> None:
    first = hash_password("pässwörd ✓", iterations=FAST)
    second = hash_password("pässwörd ✓", iterations=FAST)
    assert first != second
    assert verify_password("pässwörd ✓", first)
    assert verify_password("pässwörd ✓", second)
    assert not verify_password("passwort ✓", first)


def test_default_iterations() -> None:
    assert DEFAULT_ITERATIONS >= 100_000
    encoded = hash_password("x")
    assert encoded.split("$")[1] == str(DEFAULT_ITERATIONS)
    assert verify_password("x", encoded)


def test_tampered_digest_fails() -> None:
    algorithm, iterations, salt, digest = hash_password("x", iterations=FAST).split("$")
    raw = bytearray(base64.b64decode(digest))
    raw[0] ^= 1
    assert not verify_password("x", "$".join((algorithm, iterations, salt, _b64(bytes(raw)))))


@pytest.mark.parametrize(
    "encoded",
    [
        "",
        "garbage",
        f"pbkdf2_sha256$1000${SALT}",
        f"pbkdf2_sha256$1000${SALT}${DIGEST}$extra",
        f"pbkdf2_sha256$many${SALT}${DIGEST}",
        f"pbkdf2_sha256$1000$!!!notbase64!!!${DIGEST}",
        f"pbkdf2_sha256$1000${SALT}$c2FsdA",  # bad padding
        f"pbkdf2_sha256$1000${SALT}${DIGEST[:-4]} {DIGEST[-4:]}",
    ],
)
def test_malformed_hashes(encoded: str) -> None:
    with pytest.raises(ConfigError, match="malformed password hash"):
        _parse_hash(encoded)
    assert verify_password("anything", encoded) is False


@pytest.mark.parametrize("algorithm", ["md5", "pbkdf2_sha1", "PBKDF2_SHA256", "bcrypt"])
def test_unsupported_algorithms(algorithm: str) -> None:
    encoded = f"{algorithm}$1000${SALT}${DIGEST}"
    with pytest.raises(ConfigError, match="unsupported password hash algorithm"):
        _parse_hash(encoded)
    assert verify_password("anything", encoded) is False


@pytest.mark.parametrize(
    "encoded",
    [
        f"pbkdf2_sha256$0${SALT}${DIGEST}",
        f"pbkdf2_sha256$-5${SALT}${DIGEST}",
        f"pbkdf2_sha256$10000001${SALT}${DIGEST}",
        f"pbkdf2_sha256$999999999999${SALT}${DIGEST}",
        f"pbkdf2_sha256$1000${_b64(b's' * 7)}${DIGEST}",
        f"pbkdf2_sha256$1000${SALT}${_b64(b'd' * 15)}",
    ],
)
def test_hash_parameters_out_of_range(encoded: str) -> None:
    with pytest.raises(ConfigError, match="out of range"):
        _parse_hash(encoded)
    start = time.monotonic()
    assert verify_password("anything", encoded) is False
    assert time.monotonic() - start < 0.5  # rejected before any key derivation


def test_iteration_bounds_are_inclusive() -> None:
    assert _parse_hash(f"pbkdf2_sha256$1${SALT}${DIGEST}")[0] == 1
    assert _parse_hash(f"pbkdf2_sha256$10000000${SALT}${DIGEST}")[0] == 10_000_000
    assert verify_password("x", hash_password("x", iterations=1))


# -- web config file ---------------------------------------------------------


@pytest.fixture
def write_config(tmp_path: Path) -> Callable[..., str]:
    def write(text: str, mode: int = 0o600) -> str:
        path = tmp_path / "web.ini"
        path.write_text(text)
        path.chmod(mode)
        return str(path)

    return write


def test_load_web_config_happy_path(
    write_config: Callable[..., str], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger=LOGGER)
    h1, h2 = USERS["prometheus"], USERS["Grafana"]
    path = write_config(
        "# comment\n"
        "[tls]\n"
        "cert_file = /etc/pne/tls.crt\n"
        "key_file = /etc/pne/tls.key\n"
        "client_ca_file = /etc/pne/ca.crt\n"
        "min_version = TLSv1.3\n"
        "\n"
        "[basic_auth_users]\n"
        f"Prometheus = {h1}\n"
        f"prometheus = {h2}\n"
    )
    config = load_web_config(path)
    assert config == WebConfig(
        tls_cert_file="/etc/pne/tls.crt",
        tls_key_file="/etc/pne/tls.key",
        tls_client_ca_file="/etc/pne/ca.crt",
        tls_min_version="TLSv1.3",
        users={"Prometheus": h1, "prometheus": h2},
    )
    assert config.tls_enabled
    assert caplog.records == []


def test_load_web_config_defaults(write_config: Callable[..., str]) -> None:
    config = load_web_config(write_config(""))
    assert config == WebConfig()
    assert not config.tls_enabled
    assert config.tls_min_version == "TLSv1.2"

    auth_only = load_web_config(write_config(f"[basic_auth_users]\nu = {USERS['prometheus']}\n"))
    assert not auth_only.tls_enabled
    assert auth_only.users == {"u": USERS["prometheus"]}

    tls_only = load_web_config(write_config("[tls]\ncert_file = c\nkey_file = k\n"))
    assert tls_only.tls_enabled
    assert tls_only.tls_min_version == "TLSv1.2"
    assert tls_only.tls_client_ca_file is None


@pytest.mark.parametrize(
    ("text", "error"),
    [
        ("[web]\nlisten = :9101\n", r"unknown section \[web\]"),
        ("[TLS]\ncert_file = c\nkey_file = k\n", r"unknown section \[TLS\]"),
        ("[tls]\ncert_file = c\nkey_file = k\npassword = x\n", r"unknown \[tls\] keys: password"),
        ("[tls]\nCert_File = c\nkey_file = k\n", r"unknown \[tls\] keys: Cert_File"),
        ("[tls]\ncert_file = c\n", r"\[tls\] needs both cert_file and key_file"),
        ("[tls]\nkey_file = k\n", r"\[tls\] needs both cert_file and key_file"),
        ("[tls]\ncert_file = c\nkey_file =\n", r"\[tls\] needs both cert_file and key_file"),
        ("[tls]\nclient_ca_file = ca\n", "client_ca_file requires cert_file and key_file"),
        ("[tls]\ncert_file=c\nkey_file=k\nmin_version=TLSv1.1\n", "min_version must be"),
        ("[tls]\ncert_file=c\nkey_file=k\nmin_version=tlsv1.2\n", "min_version must be"),
        ("[tls]\ncert_file=c\nkey_file=k\nmin_version=\n", "min_version must be"),
        ("[basic_auth_users]\nalice = plaintext\n", "user 'alice': malformed password hash"),
        ("[basic_auth_users]\nalice =\n", "user 'alice': malformed password hash"),
        (
            f"[basic_auth_users]\nalice = md5$1${SALT}${DIGEST}\n",
            "user 'alice': unsupported password hash algorithm",
        ),
        ("[basic_auth_users]\na = x\na = y\n", "cannot read web config"),
        ("cert_file = c\n", "cannot read web config"),
        ("[basic_auth_users]\nalice\n", "cannot read web config"),
    ],
)
def test_load_web_config_errors(write_config: Callable[..., str], text: str, error: str) -> None:
    with pytest.raises(ConfigError, match=error):
        load_web_config(write_config(text))


def test_default_section_is_rejected(write_config: Callable[..., str]) -> None:
    # configparser merges [DEFAULT] into every section; it must not silently
    # add users (or TLS settings) that do not appear under their own section.
    digest = USERS["prometheus"]
    text = f"[DEFAULT]\nmallory = {digest}\n[basic_auth_users]\nalice = {digest}\n"
    with pytest.raises(ConfigError, match=r"unknown section \[DEFAULT\]"):
        load_web_config(write_config(text))


def test_missing_web_config_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="cannot read web config"):
        load_web_config(str(tmp_path / "missing.ini"))


@pytest.mark.parametrize(
    ("mode", "warns"), [(0o600, False), (0o640, False), (0o604, True), (0o644, True), (0o666, True)]
)
def test_world_readable_warning(
    write_config: Callable[..., str], caplog: pytest.LogCaptureFixture, mode: int, warns: bool
) -> None:
    caplog.set_level(logging.WARNING, logger=LOGGER)
    load_web_config(write_config(f"[basic_auth_users]\nu = {USERS['prometheus']}\n", mode))
    warnings = [r for r in caplog.records if "world-readable" in r.getMessage()]
    assert len(warnings) == (1 if warns else 0)
    if warns:
        assert warnings[0].levelno == logging.WARNING


def test_no_world_readable_warning_without_users(
    write_config: Callable[..., str], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger=LOGGER)
    load_web_config(write_config("[tls]\ncert_file = c\nkey_file = k\n", 0o644))
    assert caplog.records == []


# -- basic auth --------------------------------------------------------------


@pytest.fixture
def fast_dummy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the unknown-user dummy hash cheap to build and check."""
    monkeypatch.setattr(webconfig, "DEFAULT_ITERATIONS", FAST)


@pytest.fixture
def verify_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    calls: list[tuple[str, str]] = []
    real = webconfig.verify_password

    def counting(password: str, encoded: str) -> bool:
        calls.append((password, encoded))
        return real(password, encoded)

    monkeypatch.setattr(webconfig, "verify_password", counting)
    return calls


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now


@pytest.mark.usefixtures("fast_dummy")
def test_basic_auth_valid_credentials() -> None:
    auth = BasicAuth(USERS)
    assert auth.check(_basic("prometheus", "s3cret-pass")) is True
    assert auth.check(_basic("Grafana", "with:colon")) is True  # password may contain ':'


@pytest.mark.usefixtures("fast_dummy")
@pytest.mark.parametrize(
    "header",
    [
        None,
        "",
        "Basic",
        "Basic ",
        "Bearer abc",
        "Digest username=prometheus",
        "basic " + _b64(b"prometheus:s3cret-pass"),  # scheme is matched exactly
        "Basic !!!not-base64!!!",
        "Basic " + _b64(b"prometheus:s3cret-pass")[:-1],  # broken padding
        "Basic " + _b64(b"prometheuss3cret-pass"),  # no colon
        "Basic " + _b64(b"\xff\xfe:s3cret-pass"),  # not UTF-8
        "Basic " + _b64(b":s3cret-pass"),  # empty user
        "Basic éééé",  # non-ASCII (headers are decoded as Latin-1)
        _basic("prometheus", "wrong"),
        _basic("prometheus", "s3cret-pass "),
        _basic("Prometheus", "s3cret-pass"),  # usernames are case-sensitive
        _basic("nobody", "s3cret-pass"),
    ],
)
def test_basic_auth_rejects(header: str | None) -> None:
    assert BasicAuth(USERS).check(header) is False


def test_unknown_user_costs_a_dummy_verification(verify_calls: list[tuple[str, str]]) -> None:
    auth = BasicAuth(USERS)
    assert auth.check(_basic("nobody", "guess")) is False
    assert len(verify_calls) == 1
    password, encoded = verify_calls[0]
    assert password == "guess"
    assert encoded not in USERS.values()
    # Same work factor as the configured users, so timing does not reveal
    # whether a username exists.
    assert encoded.split("$")[1] == str(FAST)


def test_dummy_verification_uses_the_highest_configured_work_factor() -> None:
    users = {"a": hash_password("x", iterations=FAST), "b": hash_password("y", iterations=FAST * 2)}
    assert BasicAuth(users)._dummy.split("$")[1] == str(FAST * 2)
    assert BasicAuth({})._dummy.split("$")[1] == str(DEFAULT_ITERATIONS)


@pytest.mark.usefixtures("fast_dummy")
def test_verified_headers_are_cached(verify_calls: list[tuple[str, str]]) -> None:
    auth = BasicAuth(USERS, attempts_per_second=2.0)
    header = _basic("prometheus", "s3cret-pass")
    assert auth.check(header) is True
    assert len(verify_calls) == 1
    # Far more than the rate limit allows: cache hits skip PBKDF2 and the limiter.
    for _ in range(50):
        assert auth.check(header) is True
    assert len(verify_calls) == 1
    # Failures are never cached.
    bad = _basic("prometheus", "wrong")
    assert auth.check(bad) is False
    assert len(verify_calls) == 2


@pytest.mark.usefixtures("fast_dummy")
def test_cache_is_bounded(verify_calls: list[tuple[str, str]]) -> None:
    users = {name: hash_password(name, iterations=FAST) for name in ("a", "b", "c")}
    auth = BasicAuth(users, attempts_per_second=100.0)
    auth._CACHE_SIZE = 2
    for name in ("a", "b", "c"):
        assert auth.check(_basic(name, name)) is True
    assert len(verify_calls) == 3
    assert auth.check(_basic("c", "c")) is True  # still cached
    assert len(verify_calls) == 3
    assert auth.check(_basic("a", "a")) is True  # evicted, verified again
    assert len(verify_calls) == 4


@pytest.mark.usefixtures("fast_dummy")
def test_rate_limiting(
    monkeypatch: pytest.MonkeyPatch, verify_calls: list[tuple[str, str]]
) -> None:
    clock = FakeClock()
    monkeypatch.setattr(webconfig, "time", clock)
    auth = BasicAuth(USERS, attempts_per_second=2.0)
    bad = _basic("prometheus", "wrong")
    good = _basic("prometheus", "s3cret-pass")

    assert auth.check(bad) is False
    assert auth.check(bad) is False
    assert auth.check(bad) is None  # bucket empty
    assert auth.check(good) is None  # uncached valid credentials wait too
    assert auth.check(_basic("nobody", "x")) is None
    assert len(verify_calls) == 2  # no key derivation while limited

    clock.now += 0.5  # one token refills
    assert auth.check(good) is True
    assert auth.check(bad) is None
    assert auth.check(good) is True  # cached: not limited

    clock.now += 3600  # the bucket never holds more than one second's worth
    assert [auth.check(bad) for _ in range(3)] == [False, False, None]


# -- TLS context ---------------------------------------------------------------


def _config(crt: Path, key: Path, **kwargs: str) -> WebConfig:
    return WebConfig(tls_cert_file=str(crt), tls_key_file=str(key), **kwargs)


def test_tls_context_defaults(certs: dict[str, tuple[Path, Path]]) -> None:
    provider = TLSContextProvider(_config(*certs["a"]))
    ctx = provider.get()
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.minimum_version == ssl.TLSVersion.TLSv1_2
    assert ctx.verify_mode == ssl.CERT_NONE
    assert provider.get() is ctx


def test_tls_context_min_version_and_client_ca(certs: dict[str, tuple[Path, Path]]) -> None:
    config = _config(*certs["a"], tls_client_ca_file=str(certs["ca"][0]), tls_min_version="TLSv1.3")
    ctx = TLSContextProvider(config).get()
    assert ctx.minimum_version == ssl.TLSVersion.TLSv1_3
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert [dict(x[0] for x in c["subject"]) for c in ctx.get_ca_certs()] == [
        {"commonName": "test-ca"}
    ]


def test_tls_context_errors(certs: dict[str, tuple[Path, Path]], tmp_path: Path) -> None:
    crt_a, key_a = certs["a"]
    with pytest.raises(ConfigError, match="needs cert_file and key_file"):
        TLSContextProvider(WebConfig())
    with pytest.raises(ConfigError, match="cannot load TLS material"):
        TLSContextProvider(_config(tmp_path / "missing.crt", key_a))
    with pytest.raises(ConfigError, match="cannot load TLS material"):
        TLSContextProvider(_config(crt_a, certs["b"][1]))  # key does not match
    garbage = tmp_path / "garbage.pem"
    garbage.write_text("not a certificate\n")
    with pytest.raises(ConfigError, match="cannot load TLS material"):
        TLSContextProvider(_config(crt_a, key_a, tls_client_ca_file=str(garbage)))


@pytest.fixture
def live_files(tmp_path: Path, certs: dict[str, tuple[Path, Path]]) -> tuple[Path, Path]:
    crt, key = tmp_path / "tls.crt", tmp_path / "tls.key"
    _install(certs["a"], crt, key, bump=0)
    return crt, key


def test_tls_reload_when_files_change(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    certs: dict[str, tuple[Path, Path]],
    live_files: tuple[Path, Path],
) -> None:
    monkeypatch.setattr(TLSContextProvider, "_CHECK_EVERY", 0.0)
    caplog.set_level(logging.INFO, logger=LOGGER)
    provider = TLSContextProvider(_config(*live_files))
    first = provider.get()
    assert provider.get() is first  # unchanged files: no rebuild

    _install(certs["b"], *live_files)
    second = provider.get()
    assert second is not first
    assert "reloaded TLS certificate" in caplog.text
    assert provider.get() is second


def test_tls_reload_keeps_old_context_on_broken_files(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    certs: dict[str, tuple[Path, Path]],
    live_files: tuple[Path, Path],
) -> None:
    monkeypatch.setattr(TLSContextProvider, "_CHECK_EVERY", 0.0)
    caplog.set_level(logging.INFO, logger=LOGGER)
    crt, key = live_files
    provider = TLSContextProvider(_config(crt, key))
    first = provider.get()

    crt.write_text("-----BEGIN CERTIFICATE-----\ntruncated\n")
    _bump_mtime(crt, 5)
    assert provider.get() is first
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert errors
    assert "keeping previous TLS certificate" in errors[0].getMessage()

    crt.unlink()  # a missing file is kept around as well
    assert provider.get() is first

    _install(certs["b"], crt, key, bump=10)  # repaired: picked up on the next check
    assert provider.get() is not first


def test_tls_reload_waits_for_check_interval(
    certs: dict[str, tuple[Path, Path]], live_files: tuple[Path, Path]
) -> None:
    provider = TLSContextProvider(_config(*live_files))
    first = provider.get()
    _install(certs["b"], *live_files)
    assert provider.get() is first  # not re-checked before _CHECK_EVERY elapses


def test_rate_limit_is_per_client() -> None:
    auth = BasicAuth(USERS, attempts_per_second=1.0)
    assert auth.check(_basic("prometheus", "wrong"), "10.0.0.66") is False
    assert auth.check(_basic("prometheus", "wrong"), "10.0.0.66") is None
    # A client guessing passwords does not lock out everyone else.
    assert auth.check(_basic("prometheus", "s3cret-pass"), "10.0.0.5") is True


def test_malformed_headers_do_not_use_up_attempts() -> None:
    auth = BasicAuth(USERS, attempts_per_second=1.0)
    for header in ("Basic !!!", "Basic " + _b64(b"no-colon")):
        assert auth.check(header, "10.0.0.5") is False
    assert auth.check(_basic("prometheus", "s3cret-pass"), "10.0.0.5") is True


def test_encrypted_key_fails_instead_of_prompting(tmp_path: Path) -> None:
    key, cert = tmp_path / "key.pem", tmp_path / "cert.pem"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-keyout", str(key), "-out",
         str(cert), "-days", "1", "-subj", "/CN=test", "-passout", "pass:secret"],
        check=True, capture_output=True,
    )  # fmt: skip
    with pytest.raises(ConfigError, match="cannot load TLS material"):
        TLSContextProvider(WebConfig(tls_cert_file=str(cert), tls_key_file=str(key)))
