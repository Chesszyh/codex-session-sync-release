"""Verify Cloudflare Access application tokens before serving public history."""
import re
import threading
import time

import jwt


class AccessVerifier:
    def __init__(self, team, audience):
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", team or ""):
            raise ValueError("Access team must be the team subdomain, without a URL")
        if not re.fullmatch(r"[a-f0-9]{64}", audience or ""):
            raise ValueError("Access audience must be the application's 64-character AUD tag")
        self.issuer = f"https://{team}.cloudflareaccess.com"
        self.audience = audience
        self.client = jwt.PyJWKClient(self.issuer + "/cdn-cgi/access/certs", timeout=5,
                                      lifespan=300, cooldown_duration=30)
        self.lock = threading.Lock()
        self.retry_after = 0

    def verify(self, token):
        if not isinstance(token, str) or not token or len(token) > 16384:
            return False
        try:
            header = jwt.get_unverified_header(token)
            kid = header.get("kid")
            if header.get("alg") != "RS256" or not isinstance(kid, str) or not 1 <= len(kid) <= 256:
                return False
            with self.lock:
                if time.monotonic() < self.retry_after:
                    return False
                try:
                    key = self.client.get_signing_key(kid)
                except jwt.PyJWKClientConnectionError:
                    # A keys endpoint outage must not start a network request per viewer poll.
                    self.retry_after = time.monotonic() + 30
                    return False
            claims = jwt.decode(token, key.key, algorithms=["RS256"], audience=self.audience,
                                issuer=self.issuer, options={"require": ["iss", "aud", "exp", "iat", "sub"]})
            return claims.get("type") == "app"
        except (jwt.PyJWTError, ValueError, TypeError, KeyError):
            return False
