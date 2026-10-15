#!/usr/bin/env python3
"""Sign in to the Sigma API: browser OAuth by default, API client credentials as the fallback.

Written from the public OAuth standards, not copied from any other project:
  RFC 9728  protected resource metadata   (the API's 401 points at it)
  RFC 8414  authorization server metadata
  RFC 7591  dynamic client registration   (the tool registers itself as a public client, no secret)
  RFC 7636  PKCE, S256
  RFC 8252  native apps: a one-shot loopback redirect on 127.0.0.1

Modes (SIGMA_AUTH_MODE or --auth-mode):
  auto                 saved browser sign-in, then client credentials, then (in a terminal) sign in now   [default]
  browser              saved browser sign-in only; in a terminal, sign in now if there is none
  client-credentials   SIGMA_CLIENT_ID and SIGMA_CLIENT_SECRET only
A token already in SIGMA_API_TOKEN is used as is, whatever the mode.

Safety rules this module follows:
  * Only HTTPS hosts under sigmacomputing.com are contacted, including every endpoint found during discovery.
  * Requests never follow redirects, so a token cannot be forwarded anywhere.
  * A browser session (refresh token) is kept in the OS keychain (macOS `security`, Linux `secret-tool`),
    never in the workspace. Secrets are passed on stdin, not on a command line.
    Where no keychain exists, SIGMA_OAUTH_STORE=file opts in to a private (mode 600) file instead.
  * Refresh tokens can rotate, so a replacement is saved before it is used.
  * Every new token is verified with GET /v2/whoami before it is used.
  * Tokens are never printed.
"""
import base64
import hashlib
import http.server
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from typing import NamedTuple

ALLOWED_HOST_SUFFIXES = (".sigmacomputing.com",)
AUTH_MODES = ("auto", "browser", "client-credentials")
SERVICE = "sigma-navigable-erd"
REFRESH_MARGIN = 120          # seconds before expiry at which a cached access token is renewed
LOGIN_TIMEOUT = 120           # seconds to wait for the browser sign-in to finish


class AuthError(Exception):
    """Sign-in failed. The message is safe to show to the user."""


class AuthUnavailable(AuthError):
    """This host, account or machine cannot use the requested sign-in method."""


class AuthRequired(AuthError):
    """No usable sign-in exists and one cannot be started from here."""


class Token(NamedTuple):
    value: str
    method: str          # "SIGMA_API_TOKEN", "browser sign-in" or "client credentials"
    expires_at: float    # unix time; 0 when unknown


def log(msg):
    print(msg, file=sys.stderr)


# ---------------------------------------------------------------------------------------------------------------
# Host checks and the single HTTP choke point
# ---------------------------------------------------------------------------------------------------------------
def check_host(url, what="URL"):
    """Refuse anything that is not an HTTPS sigmacomputing.com host."""
    u = urllib.parse.urlparse(url)
    host = u.hostname or ""
    ok = u.scheme == "https" and (host == "sigmacomputing.com" or host.endswith(ALLOWED_HOST_SUFFIXES))
    if not ok:
        raise AuthError(f"Refusing {what} {url!r}: it must be an HTTPS Sigma host (sigmacomputing.com).")
    return url


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def _request(method, url, headers=None, data=None, form=None, json_body=None, timeout=30):
    """Make one request and return (status, lower-cased headers, body text). Tests replace this function."""
    check_host(url)
    headers = dict(headers or {})
    body = data
    if form is not None:
        body = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif json_body is not None:
        body = json.dumps(json_body).encode()
        headers["Content-Type"] = "application/json"
    headers.setdefault("Accept", "application/json")
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with _opener.open(req, timeout=timeout) as r:
            return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in e.headers.items()}, e.read().decode("utf-8", "replace")
    except urllib.error.URLError as e:
        raise AuthError(f"Could not reach {urllib.parse.urlparse(url).hostname}: {e.reason}") from None


def _json(text, what):
    try:
        v = json.loads(text)
    except ValueError:
        raise AuthError(f"{what} did not return JSON.") from None
    if not isinstance(v, dict):
        raise AuthError(f"{what} returned an unexpected response.")
    return v


# ---------------------------------------------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------------------------------------------
def verify(base, access_token):
    """Require GET /v2/whoami to answer 200 with JSON before a token is trusted."""
    status, _, text = _request("GET", f"{base}/v2/whoami", headers={"Authorization": f"Bearer {access_token}"})
    if status != 200:
        raise AuthError(f"The token was rejected by {urllib.parse.urlparse(base).hostname} (HTTP {status}).")
    _json(text, "whoami")


# ---------------------------------------------------------------------------------------------------------------
# Discovery (RFC 9728, RFC 8414)
# ---------------------------------------------------------------------------------------------------------------
def discover(base):
    status, headers, _ = _request("GET", f"{base}/v2/whoami")
    challenge = headers.get("www-authenticate", "")
    meta = re.search(r'resource_metadata="([^"]+)"', challenge)
    if status != 401 or not meta:
        raise AuthUnavailable("This Sigma host does not offer browser sign-in. Use API client credentials instead.")
    scope = (re.search(r'scope="([^"]+)"', challenge) or [None, "api:access"])[1]
    rs, _, text = _request("GET", check_host(meta.group(1), "resource metadata URL"))
    resource = _json(text, "Resource metadata")
    servers = resource.get("authorization_servers") or []
    if rs != 200 or not servers:
        raise AuthUnavailable("The Sigma API did not name an authorization server.")
    issuer = check_host(servers[0], "authorization server").rstrip("/")
    st, _, text = _request("GET", f"{issuer}/.well-known/oauth-authorization-server")
    md = _json(text, "Authorization server metadata")
    if st != 200:
        raise AuthUnavailable("The authorization server metadata could not be read.")
    need = ("authorization_endpoint", "token_endpoint", "registration_endpoint")
    if not all(md.get(k) for k in need):
        raise AuthUnavailable("The authorization server does not support self-registration.")
    if "S256" not in (md.get("code_challenge_methods_supported") or []):
        raise AuthUnavailable("The authorization server does not support PKCE (S256).")
    if "none" not in (md.get("token_endpoint_auth_methods_supported") or ["none"]):
        raise AuthUnavailable("The authorization server does not allow public clients.")
    return {"authorize": check_host(md["authorization_endpoint"], "authorize endpoint"),
            "token": check_host(md["token_endpoint"], "token endpoint"),
            "register": check_host(md["registration_endpoint"], "registration endpoint"),
            "scope": scope}


# ---------------------------------------------------------------------------------------------------------------
# PKCE and the loopback listener (RFC 7636, RFC 8252)
# ---------------------------------------------------------------------------------------------------------------
def pkce_pair():
    verifier = secrets.token_urlsafe(64)                      # 86 characters, within the 43-128 allowed
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


class _Callback(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        if u.path != "/oauth/callback" or not secrets.compare_digest(q.get("state", ""), self.server.expected_state):
            self.send_response(404)                           # favicon requests, strays, or a wrong state: ignore
            self.end_headers()
            return
        self.server.result = q
        page = b"<!doctype html><meta charset=utf-8><title>Signed in</title><p>Signed in. You can close this tab and return to the terminal."
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(page)))
        self.end_headers()
        self.wfile.write(page)

    def log_message(self, *args):
        pass


def _wait_for_callback(server, timeout):
    server.timeout = 1
    deadline = time.time() + timeout
    while server.result is None and time.time() < deadline:
        server.handle_request()
    return server.result


def _parse_pasted(text, expected_state):
    """Accept a pasted redirect URL or query string, for when the loopback listener could not be used."""
    q = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlparse(text.strip()).query or text.strip()).items()}
    if not secrets.compare_digest(q.get("state", ""), expected_state):
        raise AuthError("The pasted address does not match this sign-in attempt.")
    return q


# ---------------------------------------------------------------------------------------------------------------
# Session storage: OS keychain, or a private file when the user opts in
# ---------------------------------------------------------------------------------------------------------------
class MemoryStore:
    """In-memory store, used by tests."""
    def __init__(self):
        self.data = {}

    def get(self, account):
        return self.data.get(account)

    def set(self, account, value):
        self.data[account] = value

    def delete(self, account):
        self.data.pop(account, None)


class MacKeychain:
    name = "macOS keychain"

    def get(self, account):
        r = subprocess.run(["security", "find-generic-password", "-a", account, "-s", SERVICE, "-w"], capture_output=True, text=True)
        return base64.b64decode(r.stdout.strip()).decode() if r.returncode == 0 and r.stdout.strip() else None

    def set(self, account, value):
        blob = base64.b64encode(value.encode()).decode()           # base64 has no characters that need quoting
        cmd = f"add-generic-password -U -a {account} -s {SERVICE} -w {blob}\n"
        r = subprocess.run(["security", "-i"], input=cmd, capture_output=True, text=True)    # stdin, not argv
        if r.returncode != 0 or "error" in (r.stderr or "").lower():
            raise AuthError("Could not save the sign-in to the macOS keychain.")

    def delete(self, account):
        subprocess.run(["security", "delete-generic-password", "-a", account, "-s", SERVICE], capture_output=True)


class SecretTool:
    name = "Linux keyring (secret-tool)"

    def get(self, account):
        r = subprocess.run(["secret-tool", "lookup", "service", SERVICE, "account", account], capture_output=True, text=True)
        return r.stdout if r.returncode == 0 and r.stdout else None

    def set(self, account, value):
        r = subprocess.run(["secret-tool", "store", "--label", f"Navigable ERD ({account})", "service", SERVICE, "account", account],
                           input=value, capture_output=True, text=True)
        if r.returncode != 0:
            raise AuthError("Could not save the sign-in to the Linux keyring.")

    def delete(self, account):
        subprocess.run(["secret-tool", "clear", "service", SERVICE, "account", account], capture_output=True)


class FileStore:
    """Opt-in fallback (SIGMA_OAUTH_STORE=file) for machines with no keychain. A private file, mode 600."""
    name = "private file in ~/.sigma-lineage"

    def _path(self, account):
        d = os.path.expanduser("~/.sigma-lineage")
        os.makedirs(d, mode=0o700, exist_ok=True)
        return os.path.join(d, f"oauth-{re.sub(r'[^A-Za-z0-9.-]', '_', account)}.json")

    def get(self, account):
        p = self._path(account)
        if not os.path.exists(p):
            return None
        if os.name != "nt" and os.stat(p).st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise AuthError(f"{p} is readable by other users. Run: chmod 600 {p}")
        with open(p) as f:
            return f.read()

    def set(self, account, value):
        p = self._path(account)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(p), prefix=".oauth-")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(value)
            os.chmod(tmp, 0o600)
            os.replace(tmp, p)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def delete(self, account):
        try:
            os.unlink(self._path(account))
        except FileNotFoundError:
            pass


def backend():
    """The store to use on this machine, or None. Tests replace this function."""
    if os.environ.get("SIGMA_OAUTH_STORE", "").lower() == "file":
        return FileStore()
    if sys.platform == "darwin" and shutil.which("security"):
        return MacKeychain()
    if sys.platform.startswith("linux") and shutil.which("secret-tool"):
        return SecretTool()
    return None


def _account(base):
    return urllib.parse.urlparse(base).hostname


def load_session(base):
    store = backend()
    raw = store.get(_account(base)) if store else None
    if not raw:
        return None
    try:
        s = json.loads(raw)
        return s if s.get("refresh_token") and s.get("client_id") and s.get("token_endpoint") else None
    except ValueError:
        return None


NO_STORE = ("No OS keychain was found on this machine. Set SIGMA_OAUTH_STORE=file to keep the sign-in in a "
            "private file in ~/.sigma-lineage, or use API client credentials.")


def save_session(base, session):
    store = backend()
    if store is None:
        raise AuthUnavailable(NO_STORE)
    store.set(_account(base), json.dumps(session))


def forget_session(base):
    store = backend()
    if store:
        store.delete(_account(base))


# ---------------------------------------------------------------------------------------------------------------
# Browser sign-in
# ---------------------------------------------------------------------------------------------------------------
def login(base, timeout=None, interactive=None):
    """Sign in through the browser, save the session, and return a verified Token."""
    timeout = LOGIN_TIMEOUT if timeout is None else timeout
    if interactive is None:
        interactive = sys.stdin.isatty() and sys.stdout.isatty()
    if backend() is None:
        raise AuthUnavailable(NO_STORE)                            # say so before a browser opens, not after
    ep = discover(base)
    server = http.server.HTTPServer(("127.0.0.1", 0), _Callback)  # loopback only, an OS-assigned free port
    server.result, server.expected_state = None, secrets.token_urlsafe(24)
    try:
        redirect = f"http://127.0.0.1:{server.server_address[1]}/oauth/callback"
        status, _, text = _request("POST", ep["register"], json_body={
            "client_name": "Navigable ERD", "redirect_uris": [redirect], "scope": ep["scope"],
            "token_endpoint_auth_method": "none", "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"]})
        reg = _json(text, "Client registration")
        if status not in (200, 201) or not reg.get("client_id"):
            raise AuthUnavailable("Sigma did not allow this tool to register for browser sign-in. Use API client credentials.")
        verifier, challenge = pkce_pair()
        url = ep["authorize"] + "?" + urllib.parse.urlencode({
            "response_type": "code", "client_id": reg["client_id"], "redirect_uri": redirect, "scope": ep["scope"],
            "state": server.expected_state, "code_challenge": challenge, "code_challenge_method": "S256"})
        log("Opening your browser to sign in to Sigma. If it does not open, visit:\n  " + url)
        try:
            webbrowser.open(url)
        except Exception:
            pass
        result = _wait_for_callback(server, timeout)
    finally:
        server.server_close()
    if result is None and interactive:
        pasted = input("Sign-in did not finish. Paste the full address from your browser's address bar, or press Enter to cancel: ")
        result = _parse_pasted(pasted, server.expected_state) if pasted.strip() else None
    if result is None:
        raise AuthError("Sign-in timed out. Run the login command again in a terminal.")
    if result.get("error"):
        raise AuthError(f"Sigma declined the sign-in: {result.get('error_description') or result['error']}")
    if not result.get("code"):
        raise AuthError("The sign-in response had no authorization code.")
    status, _, text = _request("POST", ep["token"], form={
        "grant_type": "authorization_code", "code": result["code"], "redirect_uri": redirect,
        "code_verifier": verifier, "client_id": reg["client_id"]})
    tok = _json(text, "Token exchange")
    if status != 200 or not tok.get("access_token"):
        raise AuthError("Sigma did not issue a token. Your account type may need the 'Use Sigma API with OAuth' permission.")
    verify(base, tok["access_token"])
    session = {"client_id": reg["client_id"], "token_endpoint": ep["token"], "refresh_token": tok.get("refresh_token", ""),
               "access_token": tok["access_token"], "expires_at": time.time() + float(tok.get("expires_in") or 3600)}
    if not session["refresh_token"]:
        raise AuthUnavailable("Sigma did not issue a refresh token, so the sign-in cannot be kept.")
    save_session(base, session)
    return Token(session["access_token"], "browser sign-in", session["expires_at"])


def _session_token(base, session):
    """A valid access token from a saved session: the cached one, or a refreshed one."""
    if session.get("access_token") and session.get("expires_at", 0) - REFRESH_MARGIN > time.time():
        try:
            verify(base, session["access_token"])
            return Token(session["access_token"], "browser sign-in", session["expires_at"])
        except AuthError:
            pass
    check_host(session["token_endpoint"], "token endpoint")
    status, _, text = _request("POST", session["token_endpoint"], form={
        "grant_type": "refresh_token", "refresh_token": session["refresh_token"], "client_id": session["client_id"]})
    tok = _json(text, "Token refresh")
    if status != 200 or not tok.get("access_token"):
        raise AuthRequired("Your saved Sigma sign-in has expired or was revoked. Sign in again with: run.py login")
    new = dict(session, access_token=tok["access_token"], expires_at=time.time() + float(tok.get("expires_in") or 3600))
    if tok.get("refresh_token"):
        new["refresh_token"] = tok["refresh_token"]               # refresh tokens can rotate
    save_session(base, new)                                        # persist the replacement before using anything
    verify(base, new["access_token"])
    return Token(new["access_token"], "browser sign-in", new["expires_at"])


# ---------------------------------------------------------------------------------------------------------------
# Client credentials (the fallback)
# ---------------------------------------------------------------------------------------------------------------
def client_credentials(base, client_id, client_secret):
    status, _, text = _request("POST", f"{base}/v2/auth/token", form={
        "grant_type": "client_credentials", "client_id": client_id, "client_secret": client_secret})
    tok = _json(text, "Token request")
    if status != 200 or not tok.get("access_token"):
        raise AuthError("Sigma refused the client ID and secret. Check them and the API host.")
    verify(base, tok["access_token"])
    return Token(tok["access_token"], "client credentials", time.time() + float(tok.get("expires_in") or 3600))


# ---------------------------------------------------------------------------------------------------------------
# The one entry point
# ---------------------------------------------------------------------------------------------------------------
def resolve_mode(mode=None):
    mode = mode or os.environ.get("SIGMA_AUTH_MODE", "auto")
    if mode not in AUTH_MODES:
        raise AuthError("SIGMA_AUTH_MODE must be one of: auto, browser, client-credentials")
    return mode


def get_token(base, mode=None, interactive=None):
    """Return a verified Token. See the module docstring for the order of preference."""
    check_host(base, "API base URL")
    base = base.rstrip("/")
    mode = resolve_mode(mode)
    if interactive is None:
        interactive = sys.stdin.isatty() and sys.stdout.isatty()

    held = os.environ.get("SIGMA_API_TOKEN")
    if held:                                                       # a token the caller already holds
        verify(base, held)
        return Token(held, "SIGMA_API_TOKEN", 0)

    problems = []
    if mode in ("auto", "browser"):
        session = None
        try:
            session = load_session(base)
        except AuthError as e:
            problems.append(str(e))
        if session:
            try:
                return _session_token(base, session)
            except AuthRequired as e:
                problems.append(str(e))
                if mode == "browser" and not interactive:
                    raise
    if mode in ("auto", "client-credentials"):
        cid, secret = os.environ.get("SIGMA_CLIENT_ID"), os.environ.get("SIGMA_CLIENT_SECRET")
        if cid and secret:
            return client_credentials(base, cid, secret)
        if mode == "client-credentials":
            raise AuthRequired("Client-credentials mode needs SIGMA_CLIENT_ID and SIGMA_CLIENT_SECRET.")
    if mode in ("auto", "browser") and interactive:
        log("No saved Sigma sign-in or API credentials were found, so I will sign you in through the browser.")
        return login(base, interactive=True)
    raise AuthRequired("No Sigma sign-in is available. Run `run.py login` in a terminal to sign in with your browser, "
                       "or set SIGMA_CLIENT_ID and SIGMA_CLIENT_SECRET for an API client."
                       + ("  (" + problems[0] + ")" if problems else ""))
