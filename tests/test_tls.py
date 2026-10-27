import datetime as dt
import http.server
import ipaddress
import socket
import ssl
import threading

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
)
from cryptography.x509.oid import AuthorityInformationAccessOID, NameOID

from onlybirds import tls


def _cert(subject, issuer, key, signer, *, ca, aia_url=None, san=None):
    now = dt.datetime.now(dt.UTC)
    b = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, subject)]))
        .issuer_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, issuer)]))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
    )
    if aia_url:
        b = b.add_extension(
            x509.AuthorityInformationAccess(
                [x509.AccessDescription(AuthorityInformationAccessOID.CA_ISSUERS, x509.UniformResourceIdentifier(aia_url))]
            ),
            critical=False,
        )
    if san:
        b = b.add_extension(x509.SubjectAlternativeName(san), critical=False)
    return b.sign(signer, hashes.SHA256())


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def broken_chain_server(tmp_path):
    """HTTPS server that sends only its leaf certificate, plus an HTTP server
    hosting the intermediate at the leaf's AIA "CA Issuers" URL."""
    root_key, int_key, leaf_key = (ec.generate_private_key(ec.SECP256R1()) for _ in range(3))
    root = _cert("Test Root", "Test Root", root_key, root_key, ca=True)
    inter = _cert("Test Intermediate", "Test Root", int_key, root_key, ca=True)

    aia_port = _free_port()
    (tmp_path / "int.der").write_bytes(inter.public_bytes(Encoding.DER))
    handler = lambda *a: http.server.SimpleHTTPRequestHandler(*a, directory=str(tmp_path))
    aia = http.server.ThreadingHTTPServer(("127.0.0.1", aia_port), handler)
    threading.Thread(target=aia.serve_forever, daemon=True).start()

    leaf = _cert(
        "localhost", "Test Intermediate", leaf_key, int_key, ca=False,
        aia_url=f"http://127.0.0.1:{aia_port}/int.der",
        san=[x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))],
    )
    (tmp_path / "leaf.pem").write_bytes(leaf.public_bytes(Encoding.PEM))
    (tmp_path / "leaf.key").write_bytes(
        leaf_key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
    )
    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(tmp_path / "leaf.pem", tmp_path / "leaf.key")
    https = http.server.ThreadingHTTPServer(("127.0.0.1", 0), http.server.SimpleHTTPRequestHandler)
    https.socket = server_ctx.wrap_socket(https.socket, server_side=True)
    threading.Thread(target=https.serve_forever, daemon=True).start()

    yield https.server_address[1], root.public_bytes(Encoding.PEM).decode()
    https.shutdown()
    aia.shutdown()


def _client_ctx(root_pem=None):
    ctx = tls.default_context()
    if root_pem:
        ctx.load_verify_locations(cadata=root_pem)
    return ctx


def _get(ctx, port):
    with httpx.Client(verify=ctx) as c:
        return c.get(f"https://localhost:{port}/")


def test_missing_intermediate_fails_then_is_fetched(broken_chain_server, monkeypatch):
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("HTTP_PROXY", raising=False)
    port, root_pem = broken_chain_server
    ctx = _client_ctx(root_pem)
    with pytest.raises(httpx.ConnectError) as e:
        _get(ctx, port)
    assert tls.is_verify_error(e.value)

    ctx = tls.with_intermediates("localhost", ctx, port=port)
    assert _get(ctx, port).status_code in (200, 404)


def test_fetched_intermediate_is_not_a_trust_anchor(broken_chain_server, monkeypatch):
    # Without the root, the downloaded intermediate alone must not be trusted.
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("HTTP_PROXY", raising=False)
    port, _ = broken_chain_server
    ctx = tls.with_intermediates("localhost", _client_ctx(), port=port)
    with pytest.raises(httpx.ConnectError):
        _get(ctx, port)
