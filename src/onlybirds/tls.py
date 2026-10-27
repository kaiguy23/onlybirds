"""TLS for sites that send an incomplete certificate chain.

Some servers (eBird's Cornell sign-in among them, as seen from GitHub's
runners) leave out the intermediate certificate. Browsers quietly download it
from the address in the server certificate's "Authority Information Access"
extension; Python's ssl module doesn't, so the handshake fails with
CERTIFICATE_VERIFY_FAILED ("unable to get local issuer certificate").

`with_intermediates` does the browser's part: it reads the host's certificate
without verifying it, follows the AIA "CA Issuers" links, and returns an SSL
context that trusts certifi's roots, the OS store, and those intermediates.
The roots still decide what's trusted: an intermediate only fills the gap,
and the chain must still reach one of them.
"""

import ssl

import certifi
import httpx
from cryptography import x509
from cryptography.hazmat.primitives.serialization import Encoding, pkcs7
from cryptography.x509.oid import AuthorityInformationAccessOID

MAX_DEPTH = 3


def is_verify_error(exc: BaseException) -> bool:
    return "CERTIFICATE_VERIFY_FAILED" in str(exc)


def default_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context(cafile=certifi.where())
    ctx.load_default_certs()
    # Intermediates added below must still chain up to a root: never let one
    # act as a trust anchor (Python 3.13+ turns partial chains on by default).
    ctx.verify_flags &= ~ssl.VERIFY_X509_PARTIAL_CHAIN
    return ctx


def ca_issuer_urls(cert: x509.Certificate) -> list[str]:
    try:
        aia = cert.extensions.get_extension_for_class(x509.AuthorityInformationAccess).value
    except x509.ExtensionNotFound:
        return []
    return [
        d.access_location.value
        for d in aia
        if d.access_method == AuthorityInformationAccessOID.CA_ISSUERS
        and isinstance(d.access_location, x509.UniformResourceIdentifier)
    ]


def _parse_certs(data: bytes) -> list[x509.Certificate]:
    # CA Issuers files come as DER, PEM, or a PKCS#7 bundle (.p7c).
    for load in (
        lambda d: [x509.load_der_x509_certificate(d)],
        x509.load_pem_x509_certificates,
        pkcs7.load_der_pkcs7_certificates,
        pkcs7.load_pem_pkcs7_certificates,
    ):
        try:
            return load(data)
        except ValueError:
            continue
    return []


def fetch_intermediates(leaf: x509.Certificate, client: httpx.Client) -> list[x509.Certificate]:
    """Follow AIA "CA Issuers" links up from `leaf`, stopping at a self-signed root."""
    found: list[x509.Certificate] = []
    cert = leaf
    for _ in range(MAX_DEPTH):
        if cert.issuer == cert.subject:
            break
        issuers = [c for url in ca_issuer_urls(cert) for c in _parse_certs(_get(client, url))]
        issuer = next((c for c in issuers if c.subject == cert.issuer), None)
        if issuer is None:
            break
        found.append(issuer)
        cert = issuer
    return found


def _get(client: httpx.Client, url: str) -> bytes:
    try:
        r = client.get(url)
    except httpx.HTTPError:
        return b""
    return r.content if r.status_code == 200 else b""


def with_intermediates(host: str, ctx: ssl.SSLContext | None = None, port: int = 443) -> ssl.SSLContext:
    """`ctx` (or a fresh default context) plus the intermediates `host` fails to send."""
    ctx = ctx or default_context()
    ctx.verify_flags &= ~ssl.VERIFY_X509_PARTIAL_CHAIN
    leaf = x509.load_pem_x509_certificate(ssl.get_server_certificate((host, port), timeout=15).encode())
    with httpx.Client(timeout=15, follow_redirects=True) as client:
        intermediates = fetch_intermediates(leaf, client)
    if intermediates:
        ctx.load_verify_locations(cadata="".join(c.public_bytes(Encoding.PEM).decode() for c in intermediates))
    return ctx
