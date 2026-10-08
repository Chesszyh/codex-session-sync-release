import http.client
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
import urllib.error
from unittest.mock import patch

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from replica.access import AccessVerifier
from replica.gateway import create_server
from replica.view import View


class AccessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.jwk = {**json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(cls.private_key.public_key())),
                   "kid": "fixture-key", "use": "sig", "alg": "RS256"}

    def setUp(self):
        self.verifier = AccessVerifier("fixture", "a" * 64)
        self.fetch = patch("urllib.request.OpenerDirector.open", side_effect=lambda *a, **k:
                           io.BytesIO(json.dumps({"keys": [self.jwk]}).encode())).start()
        self.addCleanup(patch.stopall)

    def token(self, **changes):
        claims = {"iss": self.verifier.issuer, "aud": [self.verifier.audience],
                  "iat": int(time.time()) - 1, "exp": int(time.time()) + 60,
                  "sub": "fixture-user", "type": "app", **changes}
        return jwt.encode(claims, self.private_key, algorithm="RS256", headers={"kid": "fixture-key"})

    def test_valid_signature_and_required_claims(self):
        self.assertTrue(self.verifier.verify(self.token()))
        for claims in ({"aud": "b" * 64}, {"iss": "https://other.cloudflareaccess.com"},
                       {"exp": int(time.time()) - 10}, {"iat": int(time.time()) + 60},
                       {"sub": None}, {"exp": None}, {"type": "org"}):
            with self.subTest(claims=claims):
                self.assertFalse(self.verifier.verify(self.token(**claims)))
        self.assertEqual(self.fetch.call_count, 1)

    def test_missing_claims_bad_signature_and_algorithm_are_rejected(self):
        valid = self.token()
        claims = jwt.decode(valid, options={"verify_signature": False})
        for name in ("iss", "aud", "exp", "iat", "sub"):
            value = {k: v for k, v in claims.items() if k != name}
            self.assertFalse(self.verifier.verify(jwt.encode(value, self.private_key, algorithm="RS256", headers={"kid": "fixture-key"})))
        other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.assertFalse(self.verifier.verify(jwt.encode(claims, other, algorithm="RS256", headers={"kid": "fixture-key"})))
        for token in (None, "", "malformed", "x" * 16385,
                      jwt.encode(claims, "x" * 32, algorithm="HS256", headers={"kid": "fixture-key"}),
                      jwt.encode(claims, "", algorithm="none", headers={"kid": "fixture-key"})):
            with self.subTest(token_type=type(token).__name__):
                self.assertFalse(self.verifier.verify(token))

    def test_unknown_keys_and_outages_do_not_trigger_a_fetch_per_request(self):
        self.assertTrue(self.verifier.verify(self.token()))
        claims = jwt.decode(self.token(), options={"verify_signature": False})
        for i in range(5):
            token = jwt.encode(claims, self.private_key, algorithm="RS256", headers={"kid": f"unknown-{i}"})
            self.assertFalse(self.verifier.verify(token))
        self.assertEqual(self.fetch.call_count, 1)
        self.fetch.side_effect = urllib.error.URLError("fixture outage")
        verifier = AccessVerifier("fixture", "a" * 64)
        for _ in range(3):
            self.assertFalse(verifier.verify(self.token()))
        self.assertEqual(self.fetch.call_count, 2)

    def test_invalid_configuration_is_rejected(self):
        for team, audience in (("https://fixture.example", "a" * 64), ("fixture/../other", "a" * 64),
                               ("fixture", ""), ("fixture", "not-an-aud")):
            with self.subTest(team=team), self.assertRaises(ValueError):
                AccessVerifier(team, audience)

    def test_gateway_serves_history_only_with_verified_application_token(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            view = View(root)
            with view.db:
                view.set_entity("private", "item", "fixture", {"text": "SYNTHETIC_PRIVATE_HISTORY"})
            view.close()
            server = create_server(root, 0, public_origin="https://archive.example.com",
                                   access_team="fixture", access_audience="a" * 64)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                for token, status in ((None, 403), (self.token(aud="b" * 64), 403), (self.token(), 200)):
                    conn = http.client.HTTPConnection(*server.server_address, timeout=3)
                    headers = {"Host": "archive.example.com"}
                    if token:
                        headers["Cf-Access-Jwt-Assertion"] = token
                    conn.request("GET", "/api/snapshot", headers=headers)
                    response = conn.getresponse()
                    body = response.read()
                    self.assertEqual(response.status, status)
                    self.assertEqual(b"SYNTHETIC_PRIVATE_HISTORY" in body, status == 200)
                    conn.close()
            finally:
                server.shutdown()
                server.server_close()
                worker.join()
