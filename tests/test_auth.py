"""Sign-in tests: browser OAuth (the default) and API client credentials (the fallback), against a fake Sigma.

The fake authorization server verifies PKCE, single-use refresh tokens and redirect URIs. The "browser" is a thread that
makes a real HTTP request to the tool's real loopback listener, so the actual sign-in code path is exercised.
Run from the repository root:   python3 -m unittest discover -s tests -v
"""
import base64
import contextlib
import hashlib
import io
import json
import os
import stat
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "skills", "sigma-navigable-erd", "scripts"))
sys.path.insert(0, os.path.join(ROOT, "examples"))

import auth  # noqa: E402
import blame  # noqa: E402
import fake_sigma  # noqa: E402

BASE = fake_sigma.API_HOST
REAL_REQUEST = auth._request          # the genuine function, before the tests swap in the fake server


def fake_browser(approve=True, stray_first=True):
    """Return a replacement for webbrowser.open that signs in by calling the tool's loopback listener."""
    def open_url(url):
        q = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlparse(url).query).items()}
        redirect, state = q["redirect_uri"], q["state"]
        code = fake_sigma.AUTH.issue_code(q["client_id"], redirect, q["code_challenge"])

        def go():
            time.sleep(0.05)
            if stray_first:                       # a favicon request and a wrong-state request must both be ignored
                for bad in (redirect.replace("/oauth/callback", "/favicon.ico"), f"{redirect}?code=evil&state=wrong"):
                    with contextlib.suppress(urllib.error.HTTPError):
                        urllib.request.urlopen(bad, timeout=5).read()
            if approve:
                urllib.request.urlopen(f"{redirect}?code={code}&state={state}", timeout=5).read()
        threading.Thread(target=go, daemon=True).start()
        return True
    return open_url


class AuthTestCase(unittest.TestCase):
    def setUp(self):
        fake_sigma.install(blame)
        self._open = auth.webbrowser.open
        auth.webbrowser.open = fake_browser()

    def tearDown(self):
        auth.webbrowser.open = self._open

    def sign_in(self):
        with contextlib.redirect_stderr(io.StringIO()):
            return auth.login(BASE, timeout=10, interactive=False)

    def session(self):
        return json.loads(fake_sigma.STORE.get(auth._account(BASE)))


class DiscoveryTests(AuthTestCase):
    def test_discovery_follows_the_api_challenge_to_the_endpoints(self):
        ep = auth.discover(BASE)
        self.assertEqual(ep["authorize"], BASE + "/mcp/authorize")
        self.assertEqual(ep["token"], BASE + "/mcp/token")
        self.assertEqual(ep["register"], BASE + "/mcp/register")
        self.assertEqual(ep["scope"], "api:access")

    def test_a_host_that_offers_no_browser_sign_in_is_reported_as_unavailable(self):
        fake_sigma.AUTH.offer_discovery = False
        with self.assertRaises(auth.AuthUnavailable):
            auth.discover(BASE)

    def test_an_endpoint_on_a_foreign_host_is_refused(self):
        real = fake_sigma.AUTH.request

        def evil(method, url, **kw):
            status, h, text = real(method, url, **kw)
            if url.endswith("oauth-authorization-server"):
                text = text.replace(BASE + "/mcp/token", "https://evil.example.com/token")
            return status, h, text
        auth._request = evil
        with self.assertRaises(auth.AuthError) as cm:
            auth.discover(BASE)
        self.assertIn("Refusing", str(cm.exception))

    def test_only_https_sigma_hosts_are_accepted(self):
        for ok in ("https://api.sigmacomputing.com", "https://aws-api.sigmacomputing.com/v2", "https://sigmacomputing.com"):
            auth.check_host(ok)
        for bad in ("http://api.sigmacomputing.com", "https://sigmacomputing.com.evil.io", "https://evil.example.com",
                    "https://api.sigmacomputing.com.attacker.net", "ftp://api.sigmacomputing.com"):
            with self.assertRaises(auth.AuthError, msg=bad):
                auth.check_host(bad)

    def test_the_real_request_function_refuses_foreign_hosts_before_any_network_call(self):
        for bad in ("https://evil.example.com/x", "http://api.sigmacomputing.com/x"):
            with self.assertRaises(auth.AuthError, msg=bad):
                REAL_REQUEST("GET", bad)

    def test_requests_never_follow_redirects(self):
        self.assertIsNone(auth._NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://evil.example.com"))


class PkceTests(unittest.TestCase):
    def test_challenge_is_the_s256_hash_of_the_verifier(self):
        verifier, challenge = auth.pkce_pair()
        self.assertTrue(43 <= len(verifier) <= 128)
        self.assertEqual(challenge, base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode())
        self.assertNotIn("=", challenge)
        self.assertNotEqual(auth.pkce_pair()[0], verifier)

    def test_a_pasted_redirect_must_carry_the_expected_state(self):
        self.assertEqual(auth._parse_pasted("http://127.0.0.1:1/oauth/callback?code=c&state=s1", "s1")["code"], "c")
        with self.assertRaises(auth.AuthError):
            auth._parse_pasted("http://127.0.0.1:1/oauth/callback?code=c&state=other", "s1")


class BrowserLoginTests(AuthTestCase):
    def test_login_registers_a_public_loopback_client_and_stores_the_session(self):
        tok = self.sign_in()
        self.assertEqual(tok.method, "browser sign-in")
        reg = next(iter(fake_sigma.AUTH.clients.values()))
        self.assertEqual(reg["token_endpoint_auth_method"], "none")            # a public client: no secret exists
        self.assertTrue(reg["redirect_uris"][0].startswith("http://127.0.0.1:"))
        s = self.session()
        self.assertTrue(s["refresh_token"].startswith("refresh-"))
        self.assertEqual(s["token_endpoint"], BASE + "/mcp/token")

    def test_stray_and_wrong_state_requests_are_ignored(self):
        self.assertEqual(self.sign_in().method, "browser sign-in")             # the fake browser sends both first

    def test_a_failed_exchange_does_not_save_anything(self):
        fake_sigma.AUTH.reject_token_requests = True
        with self.assertRaises(auth.AuthError):
            self.sign_in()
        self.assertIsNone(fake_sigma.STORE.get(auth._account(BASE)))

    def test_a_token_the_api_rejects_at_sign_in_is_never_saved(self):
        real_issue = fake_sigma.AUTH._issue

        def issue_dud(client_id):
            tok = real_issue(client_id)
            fake_sigma.AUTH.valid_access.discard(tok["access_token"])        # the API will not accept it
            return tok
        fake_sigma.AUTH._issue = issue_dud
        try:
            with self.assertRaises(auth.AuthError):
                self.sign_in()
        finally:
            fake_sigma.AUTH._issue = real_issue                                # the fake server is shared: always restore it
        self.assertIsNone(fake_sigma.STORE.get(auth._account(BASE)))

    def test_registration_refused_means_fall_back(self):
        fake_sigma.AUTH.allow_registration = False
        with self.assertRaises(auth.AuthUnavailable):
            self.sign_in()

    def test_no_keychain_is_reported_before_a_browser_opens(self):
        opened = []
        auth.webbrowser.open = lambda url: opened.append(url)
        auth.backend = lambda: None
        with self.assertRaises(auth.AuthUnavailable) as cm:
            auth.login(BASE, timeout=1, interactive=False)
        self.assertIn("SIGMA_OAUTH_STORE=file", str(cm.exception))
        self.assertEqual(opened, [])

    def test_a_browser_that_never_returns_times_out(self):
        auth.webbrowser.open = fake_browser(approve=False, stray_first=False)
        with self.assertRaises(auth.AuthError) as cm, contextlib.redirect_stderr(io.StringIO()):
            auth.login(BASE, timeout=1, interactive=False)
        self.assertIn("timed out", str(cm.exception))


class ModeTests(AuthTestCase):
    def test_default_prefers_the_saved_browser_session_over_client_credentials(self):
        self.sign_in()
        fake_sigma.AUTH.log.clear()
        tok = auth.get_token(BASE, interactive=False)
        self.assertEqual(tok.method, "browser sign-in")
        self.assertNotIn(("POST", BASE + "/v2/auth/token"), fake_sigma.AUTH.log)   # the fallback was not used

    def test_default_falls_back_to_client_credentials_when_there_is_no_session(self):
        tok = auth.get_token(BASE, interactive=False)
        self.assertEqual(tok.method, "client credentials")

    def test_client_credentials_mode_ignores_a_saved_session(self):
        self.sign_in()
        self.assertEqual(auth.get_token(BASE, "client-credentials", interactive=False).method, "client credentials")

    def test_browser_mode_without_a_session_asks_for_login_when_not_interactive(self):
        with self.assertRaises(auth.AuthRequired) as cm:
            auth.get_token(BASE, "browser", interactive=False)
        self.assertIn("run.py login", str(cm.exception))

    def test_nothing_available_and_not_interactive_explains_both_ways_in(self):
        os.environ.pop("SIGMA_CLIENT_ID")
        with self.assertRaises(auth.AuthRequired) as cm:
            auth.get_token(BASE, interactive=False)
        self.assertIn("run.py login", str(cm.exception))
        self.assertIn("SIGMA_CLIENT_ID", str(cm.exception))

    def test_nothing_available_but_interactive_signs_in_through_the_browser(self):
        os.environ.pop("SIGMA_CLIENT_ID")
        with contextlib.redirect_stderr(io.StringIO()):
            tok = auth.get_token(BASE, interactive=True)
        self.assertEqual(tok.method, "browser sign-in")

    def test_a_token_already_held_is_used_after_verification(self):
        fake_sigma.AUTH.valid_access.add("held-token")
        os.environ["SIGMA_API_TOKEN"] = "held-token"
        try:
            tok = auth.get_token(BASE, interactive=False)
            self.assertEqual((tok.value, tok.method), ("held-token", "SIGMA_API_TOKEN"))
            os.environ["SIGMA_API_TOKEN"] = "stale-token"
            with self.assertRaises(auth.AuthError):
                auth.get_token(BASE, interactive=False)
        finally:
            os.environ.pop("SIGMA_API_TOKEN", None)

    def test_an_unknown_mode_is_rejected(self):
        with self.assertRaises(auth.AuthError):
            auth.resolve_mode("magic")


class RefreshTests(AuthTestCase):
    def expire(self):
        s = self.session()
        s["expires_at"] = time.time() - 10
        fake_sigma.STORE.set(auth._account(BASE), json.dumps(s))
        return s

    def test_expired_access_token_is_refreshed_and_the_rotated_refresh_token_is_saved(self):
        self.sign_in()
        old = self.expire()
        tok = auth.get_token(BASE, interactive=False)
        self.assertEqual(tok.method, "browser sign-in")
        new = self.session()
        self.assertNotEqual(new["refresh_token"], old["refresh_token"])         # it rotated, and the new one was kept
        self.assertNotIn(old["refresh_token"], fake_sigma.AUTH.refresh_tokens)  # the old one is single use
        self.assertEqual(auth.get_token(BASE, interactive=False).value, tok.value)   # the cached token is reused

    def test_the_replacement_is_saved_before_it_is_used(self):
        self.sign_in()
        self.expire()
        order = []
        real_save, real_verify = auth.save_session, auth.verify
        auth.save_session = lambda *a, **k: (order.append("save"), real_save(*a, **k))[1]
        auth.verify = lambda *a, **k: (order.append("verify"), real_verify(*a, **k))[1]
        try:
            auth.get_token(BASE, interactive=False)
        finally:
            auth.save_session, auth.verify = real_save, real_verify
        self.assertLess(order.index("save"), len(order) - 1 - order[::-1].index("verify"))

    def test_a_revoked_session_falls_back_to_client_credentials_by_default(self):
        self.sign_in()
        self.expire()
        fake_sigma.AUTH.refresh_tokens.clear()                                   # the server forgot the session
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(auth.get_token(BASE, interactive=False).method, "client credentials")

    def test_a_revoked_session_in_browser_mode_asks_you_to_sign_in_again(self):
        self.sign_in()
        self.expire()
        fake_sigma.AUTH.refresh_tokens.clear()
        with self.assertRaises(auth.AuthRequired):
            auth.get_token(BASE, "browser", interactive=False)

    def test_forget_session_removes_it(self):
        self.sign_in()
        auth.forget_session(BASE)
        self.assertIsNone(auth.load_session(BASE))


class SafetyTests(AuthTestCase):
    def test_no_token_ever_reaches_stdout_or_stderr(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            auth.login(BASE, timeout=10, interactive=False)
            auth.get_token(BASE, interactive=False)
            s = self.session()
            s["expires_at"] = 0
            fake_sigma.STORE.set(auth._account(BASE), json.dumps(s))
            auth.get_token(BASE, interactive=False)
            auth.get_token(BASE, "client-credentials", interactive=False)
        seen = out.getvalue() + err.getvalue()
        for needle in ("access-", "refresh-", "cc-", fake_sigma.CLIENT_SECRET):
            self.assertNotIn(needle, seen)

    def test_a_token_the_api_rejects_is_never_trusted(self):
        fake_sigma.AUTH.valid_access.clear()
        real = fake_sigma.AUTH.request

        def hand_out_dud(method, url, **kw):
            if url.endswith("/v2/auth/token"):
                return 200, {}, json.dumps({"access_token": "not-registered", "expires_in": 3600})
            return real(method, url, **kw)
        auth._request = hand_out_dud
        with self.assertRaises(auth.AuthError):
            auth.get_token(BASE, "client-credentials", interactive=False)

    def test_a_private_file_store_is_mode_600_and_refuses_loosened_permissions(self):
        if os.name == "nt":
            self.skipTest("POSIX permissions")
        with tempfile.TemporaryDirectory() as home:
            old = os.environ.get("HOME")
            os.environ["HOME"] = home
            try:
                store = auth.FileStore()
                store.set("api.sigmacomputing.com", '{"x": 1}')
                path = store._path("api.sigmacomputing.com")
                self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
                self.assertEqual(store.get("api.sigmacomputing.com"), '{"x": 1}')
                os.chmod(path, 0o644)
                with self.assertRaises(auth.AuthError):
                    store.get("api.sigmacomputing.com")
                os.chmod(path, 0o600)
                store.delete("api.sigmacomputing.com")
                self.assertIsNone(store.get("api.sigmacomputing.com"))
            finally:
                if old is None:
                    os.environ.pop("HOME", None)
                else:
                    os.environ["HOME"] = old


if __name__ == "__main__":
    unittest.main()
