"""A permanent guard for the whole test suite: no real credentials and no real outbound request.

Why: a developer's `.env` holds LIVE Slack, HubSpot and Famly credentials, and `core.config` loads it for any
code that imports it. A test whose code path ended in a real client (the plan flag's `HubSpotDealWriter` did)
would then have talked to the real services. So, once, when the `tests` package is imported:

  1. every credential is BLANKED in the environment (Slack, HubSpot and Famly settings, and anything named
     like a token, secret or password). They are set to "" and not removed, so `load_dotenv(override=False)`
     cannot put the real values back. A test that needs a token sets a fake one with `mock.patch.dict`;
  2. any outbound HTTP request raises `RealNetworkAttempted` (at `requests.adapters.HTTPAdapter.send`, the
     one place every `requests` call passes through) and any non-local socket connection or DNS lookup
     raises it too. It is a BaseException on purpose: code under test that catches `Exception` cannot swallow
     it, so a real request always fails the test loudly. Every attempt is also recorded in `ATTEMPTS`
     (method and host only), which `test_zzz_network_sentinel` checks at the end of a full run.

A test that really needs a response opts in with a FAKE transport: `with fake_transport(handler): ...`
(handler(request) -> `response(...)`), or by mounting its own adapter on a session. Tests that patch
`requests.post` and friends, or a client class, never reach the guard and need nothing.

This module imports nothing from the project, so the environment is blank before `core.config` loads.
"""

import os
import socket
from contextlib import contextmanager
from unittest import mock

CREDENTIAL_PREFIXES = ("SLACK_", "HUBSPOT_", "FAMLY_")
CREDENTIAL_WORDS = ("TOKEN", "SECRET", "PASSWORD", "API_KEY", "CREDENTIAL")
LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost", "0.0.0.0", ""}

ATTEMPTS: list[str] = []
_installed = False


class RealNetworkAttempted(BaseException):
    """A test tried to make a real outbound request. Use a fake transport (`fake_transport`) or patch the client."""


def is_credential(name: str) -> bool:
    upper = name.upper()
    return upper.startswith(CREDENTIAL_PREFIXES) or any(word in upper for word in CREDENTIAL_WORDS)


def blank_credentials(environ=None) -> list[str]:
    """Set every credential in `environ` (default os.environ) to "". Returns the names blanked."""
    environ = os.environ if environ is None else environ
    names = sorted(name for name in list(environ) if is_credential(name))
    for name in names:
        environ[name] = ""
    return names


def _refuse(what: str) -> None:
    ATTEMPTS.append(what)
    raise RealNetworkAttempted(
        f"a test tried a real outbound request ({what}). Tests must use fakes: patch the client, or use "
        "tests.network_guard.fake_transport(handler) for a fake transport.")


def _local(host) -> bool:
    return str(host).strip().lower() in LOCAL_HOSTS


def install() -> None:
    """Blank the credentials and block real outbound requests, once per process."""
    global _installed
    if _installed:
        return
    _installed = True
    blank_credentials()

    import requests

    def send(self, request, *args, **kwargs):
        host = request.url.split("//", 1)[-1].split("/", 1)[0] if request.url else "?"
        _refuse(f"{request.method} {host}")

    requests.adapters.HTTPAdapter.send = send

    real_connect, real_connect_ex = socket.socket.connect, socket.socket.connect_ex
    real_getaddrinfo = socket.getaddrinfo

    def guarded(real):
        def connect(self, address):
            if self.family in (socket.AF_INET, socket.AF_INET6) and not _local(address[0]):
                _refuse(f"socket connect {address[0]}")
            return real(self, address)
        return connect

    def getaddrinfo(host, *args, **kwargs):
        if host is not None and not _local(host) and not str(host).replace(".", "").isdigit():
            _refuse(f"dns lookup {host}")
        return real_getaddrinfo(host, *args, **kwargs)

    socket.socket.connect = guarded(real_connect)
    socket.socket.connect_ex = guarded(real_connect_ex)
    socket.getaddrinfo = getaddrinfo


def response(status: int = 200, json_body=None, text: str | None = None):
    """A `requests.Response` for a fake transport."""
    import json as json_module

    import requests

    made = requests.Response()
    made.status_code = status
    made._content = (text if text is not None else json_module.dumps(json_body if json_body is not None else {})
                     ).encode("utf-8")
    made.headers["Content-Type"] = "application/json"
    return made


@contextmanager
def fake_transport(handler):
    """Opt in: every `requests` call inside the block is answered by `handler(request)` (a `response(...)`),
    never sent. The guard is back afterwards."""
    import requests

    def send(self, request, *args, **kwargs):
        made = handler(request)
        made.request = request
        made.url = request.url
        return made
    with mock.patch.object(requests.adapters.HTTPAdapter, "send", send):
        yield


@contextmanager
def expected_attempts():
    """For the guard's own tests: attempts made inside the block are not counted by the end-of-run sentinel."""
    before = len(ATTEMPTS)
    try:
        yield
    finally:
        del ATTEMPTS[before:]


install()
