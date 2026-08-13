"""P1-8 core: collab remote-dispatch hardening (DNS-rebinding TOCTOU + plaintext).

Covers:
  (a) TOCTOU — after validation the connection is pinned to the validated IP
      and the Host header carries the original hostname; the connect path never
      re-resolves DNS (a rebound address is never used);
  (b) scheme selection (METANO_COLLAB_SCHEME / collab.scheme / default https)
      and the TLS verification toggle;
  (c) per-host A2A token selection
      (METANO_A2A_TOKEN_<HOST> > collab.tokens[host] > METANO_A2A_TOKEN);
  (d) the existing SSRF rejection policy is unchanged.
"""

import socket
import yaml
from types import SimpleNamespace

import pytest

from metano import collab

pytestmark = pytest.mark.usefixtures("isolated_env")


# ── helpers ─────────────────────────────────────────────────────────────────

class _FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status = status

    def read(self):
        return self._payload


class _FakeConn:
    """Records the request like http.client.HTTPConnection but never connects."""

    def __init__(self, payload):
        self._payload = payload
        self.method = None
        self.path = None
        self.headers = []
        self.body = None
        self.closed = False

    def putrequest(self, method, path, *args, **kwargs):
        self.method = method
        self.path = path

    def putheader(self, name, value):
        self.headers.append((name, value))

    def endheaders(self, body):
        self.body = body

    def getresponse(self):
        return _FakeResponse(self._payload)

    def close(self):
        self.closed = True


@pytest.fixture()
def no_config(monkeypatch, tmp_path):
    """Point collab at an empty config dir and clear collab env overrides."""
    monkeypatch.setattr(collab, "CONFIG_PATH", tmp_path / "gateway_config.yaml")
    for key in (
        "METANO_COLLAB_SCHEME",
        "METANO_COLLAB_VERIFY_SSL",
        "METANO_A2A_TOKEN",
        "METANO_A2A_TOKEN_EXAMPLE_COM",
    ):
        monkeypatch.delenv(key, raising=False)
    return tmp_path


def _write_collab_config(tmp_path, section: dict):
    p = tmp_path / "gateway_config.yaml"
    p.write_text(yaml.dump({"collab": section}, allow_unicode=True), encoding="utf-8")
    return p


# ── (a) TOCTOU: connection pinned to validated IP + Host header ─────────────

def test_toctou_connection_pinned_to_validated_ip_with_host_header(no_config, monkeypatch):
    """The connect target must be the validated IP with the original hostname in
    the Host header. A second DNS resolution (rebinding) must never happen and
    the attacker IP must never be used."""
    dns_calls = []
    captured = []
    conns = []
    payloads = iter([
        b'{"jsonrpc":"2.0","result":{"id":"rid-1"}}',
        b'{"jsonrpc":"2.0","result":{"status":{"state":"completed"},'
        b'"artifacts":[{"parts":[{"text":"done"}]}]}}',
    ])

    def fake_getaddrinfo(host, port, type=0):
        dns_calls.append(host)
        # First resolution (the SSRF validation) → a legitimate public IP.
        # If the code re-resolved (the old TOCTOU), a second call returns the
        # attacker IP — the connection must never see it.
        if len(dns_calls) == 1:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.51.100.66", port))]

    def fake_open_conn(scheme, ip, port, hostname, verify_ssl, timeout):
        captured.append((scheme, ip, port, hostname, verify_ssl, timeout))
        c = _FakeConn(next(payloads))
        conns.append(c)
        return c

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    monkeypatch.setattr(collab, "_open_conn", fake_open_conn)

    result = collab._dispatch_remote_task(
        {"target": "remote-example.com:9120", "prompt": "hi", "id": "task1"},
        timeout=5,
    )

    assert result["status"] == "completed"
    assert result["result"] == "done"
    # Exactly ONE DNS resolution (validation); the connect path never re-resolves.
    assert dns_calls == ["example.com"]
    # Two RPC calls (message/send + tasks/get), both pinned to the validated IP,
    # with the original hostname and the default https scheme / verify_ssl.
    assert len(captured) == 2
    for scheme, ip, port, hostname, verify_ssl, timeout in captured:
        assert scheme == "https"
        assert ip == "93.184.216.34"        # validated IP, NOT 198.51.100.66
        assert port == 9120
        assert hostname == "example.com"
        assert verify_ssl is True
    # The Host header carries the original hostname (virtual-host routing).
    assert conns[0].path == "/a2a/rpc"
    assert conns[0].headers[0] == ("Host", "example.com:9120")
    assert ("Content-Type", "application/json") in conns[0].headers


def test_host_header_uses_original_hostname_and_port():
    assert collab._host_header("example.com", 9120, "https") == "example.com:9120"
    assert collab._host_header("example.com", 443, "https") == "example.com"
    assert collab._host_header("example.com", 80, "http") == "example.com"
    assert collab._host_header("2001:db8::1", 9120, "http") == "[2001:db8::1]:9120"


# ── (b) scheme + TLS verification selection ─────────────────────────────────

def test_scheme_default_is_https(no_config, monkeypatch):
    assert collab._collab_scheme() == "https"


def test_scheme_env_override(no_config, monkeypatch):
    monkeypatch.setenv("METANO_COLLAB_SCHEME", "http")
    assert collab._collab_scheme() == "http"
    monkeypatch.setenv("METANO_COLLAB_SCHEME", "https")
    assert collab._collab_scheme() == "https"


def test_scheme_from_config(no_config, monkeypatch):
    p = _write_collab_config(no_config, {"scheme": "http"})
    monkeypatch.setattr(collab, "CONFIG_PATH", p)
    assert collab._collab_scheme() == "http"


def test_scheme_env_overrides_config(no_config, monkeypatch):
    p = _write_collab_config(no_config, {"scheme": "http"})
    monkeypatch.setattr(collab, "CONFIG_PATH", p)
    monkeypatch.setenv("METANO_COLLAB_SCHEME", "https")
    assert collab._collab_scheme() == "https"


def test_scheme_invalid_env_falls_through(no_config, monkeypatch):
    monkeypatch.setenv("METANO_COLLAB_SCHEME", "ftp")
    assert collab._collab_scheme() == "https"


def test_verify_ssl_default_true(no_config, monkeypatch):
    assert collab._collab_verify_ssl() is True


def test_verify_ssl_env_and_config(no_config, monkeypatch):
    monkeypatch.setenv("METANO_COLLAB_VERIFY_SSL", "false")
    assert collab._collab_verify_ssl() is False
    monkeypatch.setenv("METANO_COLLAB_VERIFY_SSL", "true")
    assert collab._collab_verify_ssl() is True
    monkeypatch.delenv("METANO_COLLAB_VERIFY_SSL")
    p = _write_collab_config(no_config, {"verify_ssl": False})
    monkeypatch.setattr(collab, "CONFIG_PATH", p)
    assert collab._collab_verify_ssl() is False


# ── (c) per-host A2A token selection ────────────────────────────────────────

def test_env_token_key_mapping():
    assert collab._env_token_key("peer.example.com") == "METANO_A2A_TOKEN_PEER_EXAMPLE_COM"
    assert collab._env_token_key("host") == "METANO_A2A_TOKEN_HOST"


def test_token_per_host_env_beats_legacy(no_config, monkeypatch):
    monkeypatch.setenv("METANO_A2A_TOKEN_PEER_EXAMPLE_COM", "host-token")
    monkeypatch.setenv("METANO_A2A_TOKEN", "legacy-token")
    assert collab._collab_token("peer.example.com") == "host-token"


def test_token_per_host_config_beats_legacy(no_config, monkeypatch):
    monkeypatch.setenv("METANO_A2A_TOKEN", "legacy-token")
    p = _write_collab_config(no_config, {"tokens": {"peer.example.com": "cfg-token"}})
    monkeypatch.setattr(collab, "CONFIG_PATH", p)
    assert collab._collab_token("peer.example.com") == "cfg-token"


def test_token_env_beats_config(no_config, monkeypatch):
    monkeypatch.setenv("METANO_A2A_TOKEN_PEER_EXAMPLE_COM", "env-token")
    p = _write_collab_config(no_config, {"tokens": {"peer.example.com": "cfg-token"}})
    monkeypatch.setattr(collab, "CONFIG_PATH", p)
    assert collab._collab_token("peer.example.com") == "env-token"


def test_token_fallback_to_legacy(no_config, monkeypatch):
    monkeypatch.setenv("METANO_A2A_TOKEN", "legacy-token")
    assert collab._collab_token("peer.example.com") == "legacy-token"


def test_token_isolated_per_host(no_config, monkeypatch):
    monkeypatch.setenv("METANO_A2A_TOKEN_PEER_EXAMPLE_COM", "peer-token")
    monkeypatch.setenv("METANO_A2A_TOKEN", "legacy-token")
    # A different host must NOT receive peer.example.com's token.
    assert collab._collab_token("other.example.com") == "legacy-token"


# ── (d) SSRF rejection policy is unchanged ──────────────────────────────────

def test_ssrf_rejects_loopback_hostname():
    with pytest.raises(ValueError):
        collab._validate_remote_host("localhost")


def test_ssrf_rejects_loopback_ip():
    with pytest.raises(ValueError):
        collab._validate_remote_host("127.0.0.1")


def test_ssrf_rejects_cloud_metadata():
    with pytest.raises(ValueError):
        collab._validate_remote_host("169.254.169.254")


def test_ssrf_rejects_private_by_default():
    with pytest.raises(ValueError):
        collab._validate_remote_host("10.0.0.1")


def test_ssrf_private_allowed_when_flag_on(monkeypatch):
    monkeypatch.setattr(collab, "_COLLAB_ALLOW_PRIVATE", True)
    host, port, ip = collab._validate_remote_host("10.0.0.1")
    assert host == "10.0.0.1"
    assert port == collab._COLLAB_DEFAULT_PORT
    assert ip == "10.0.0.1"


def test_ssrf_public_ip_passes_and_returns_pinned_ip(monkeypatch):
    def fake_getaddrinfo(host, port, type=0):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    host, port, ip = collab._validate_remote_host("example.com:9120")
    assert host == "example.com"
    assert port == 9120
    assert ip == "93.184.216.34"


def test_ssrf_unknown_host_rejected(monkeypatch):
    def fake_getaddrinfo(host, port, type=0):
        raise socket.gaierror(8, "Name or service not known")

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(ValueError):
        collab._validate_remote_host("no-such-host.invalid")
