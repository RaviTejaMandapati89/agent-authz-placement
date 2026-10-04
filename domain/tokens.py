"""JWT issuance and verification for the identity layer.

Two separate EC P-256 key pairs (ES256):
  agent-identity-issuer — issues agent tokens and exchanged tokens
  identity-issuer       — issues user tokens

Private keys stay in-process; never written to disk.
Public keys served via jwks().
"""
import json
from typing import Any

import jwt
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.asymmetric.ec import SECP256R1, generate_private_key

from domain import simclock

FIXED_SCOPES: frozenset[str] = frozenset({
    "expenses:read",
    "expenses:submit",
    "expenses:approve",
    "travel:book",
    "payments:pay",
})
DEFAULT_LIFETIME: int = 300
MAX_LIFETIME: int = 3600
SERVER_AUDIENCE: str = "mcp-server"

AGENT_ISSUER: str = "agent-identity-issuer"
USER_ISSUER: str = "identity-issuer"

_AGENT_KID: str = "agent-key"
_USER_KID: str = "user-key"

# Generated at import time; never written to disk.
_agent_private_key = generate_private_key(SECP256R1(), default_backend())
_user_private_key = generate_private_key(SECP256R1(), default_backend())

_KEYS: dict[str, tuple[str, Any]] = {
    _AGENT_KID: (AGENT_ISSUER, _agent_private_key),
    _USER_KID:  (USER_ISSUER,  _user_private_key),
}

_ISSUER_TO_KID: dict[str, str] = {
    AGENT_ISSUER: _AGENT_KID,
    USER_ISSUER:  _USER_KID,
}


def jwks() -> dict:
    """Public JWKS containing both issuers' public keys."""
    keys = []
    for kid, (_, private_key) in _KEYS.items():
        pub = private_key.public_key()
        jwk = json.loads(jwt.algorithms.ECAlgorithm.to_jwk(pub))
        jwk.update({"kid": kid, "use": "sig", "alg": "ES256"})
        keys.append(jwk)
    return {"keys": keys}


def _encode(payload: dict, kid: str) -> str:
    _, private_key = _KEYS[kid]
    return jwt.encode(payload, private_key, algorithm="ES256", headers={"kid": kid})


def _issue(issuer: str, sub: str, aud: str, scope: list[str], lifetime: int) -> str:
    lifetime = max(1, min(lifetime, MAX_LIFETIME))
    now_ts = int(simclock.now())
    valid_scope = sorted(s for s in scope if s in FIXED_SCOPES)
    payload = {
        "iss": issuer,
        "sub": sub,
        "aud": aud,
        "scope": " ".join(valid_scope),
        "iat": now_ts,
        "exp": now_ts + lifetime,
    }
    return _encode(payload, _ISSUER_TO_KID[issuer])


def issue_agent_token(sub: str, aud: str, scope: list[str], lifetime: int = DEFAULT_LIFETIME) -> str:
    """Issue a token signed by agent-identity-issuer."""
    return _issue(AGENT_ISSUER, sub, aud, scope, lifetime)


def issue_user_token(sub: str, aud: str, scope: list[str], lifetime: int = DEFAULT_LIFETIME) -> str:
    """Issue a token signed by identity-issuer."""
    return _issue(USER_ISSUER, sub, aud, scope, lifetime)


def _verify_one(token: str, audience: str | None = None) -> dict:
    """Verify a token against our known keys. Raises jwt.JWTError on failure."""
    try:
        header = jwt.get_unverified_header(token)
    except jwt.DecodeError as exc:
        raise jwt.InvalidTokenError("malformed token header") from exc

    kid = header.get("kid")
    if kid not in _KEYS:
        raise jwt.InvalidTokenError(f"unknown kid {kid!r}")

    expected_issuer, private_key = _KEYS[kid]
    pub = private_key.public_key()

    opts: dict = {"verify_exp": False}  # expiry checked manually against simclock
    if audience is None:
        opts["verify_aud"] = False

    claims = jwt.decode(
        token,
        pub,
        algorithms=["ES256"],
        audience=audience,
        options=opts,
    )

    # Manual expiry check against simulated clock (not real time)
    if claims.get("exp", 0) < simclock.now():
        raise jwt.ExpiredSignatureError("token has expired")

    # Issuer must match the key's registered issuer
    if claims.get("iss") != expected_issuer:
        raise jwt.InvalidIssuerError(
            f"issuer {claims.get('iss')!r} does not match key issuer {expected_issuer!r}"
        )

    return claims


def verify_bearer(token: str, audience: str = SERVER_AUDIENCE) -> dict:
    """Verify a Bearer token presented to a tool call. Raises jwt.JWTError on failure."""
    claims = _verify_one(token, audience=audience)
    if "act" not in claims:
        raise jwt.InvalidTokenError("act claim missing — on-behalf-of token required")
    return claims


def _hop_count(claims: dict) -> int:
    depth, act = 0, claims.get("act")
    while act:
        depth += 1
        act = act.get("act")
    return depth


def exchange(
    subject_token: str,
    actor_token: str,
    requested_scope: str | None = None,
    audience: str | None = None,
) -> str:
    """RFC 8693 token exchange. Returns a new JWT signed by identity-issuer."""
    subject_claims = _verify_one(subject_token, audience=None)
    actor_claims = _verify_one(actor_token, audience=None)

    # subject_token.aud must equal actor_token.sub
    subject_aud = subject_claims.get("aud")
    actor_sub = actor_claims.get("sub")
    if subject_aud != actor_sub:
        raise ValueError(
            f"subject_token.aud {subject_aud!r} must equal actor_token.sub {actor_sub!r}"
        )

    # Refuse chains longer than two hops
    if _hop_count(subject_claims) >= 2:
        raise ValueError("delegation chain exceeds two hops")

    # Scope: intersection of subject and actor (intersect requested, if given)
    subject_scope = set(subject_claims.get("scope", "").split())
    actor_scope = set(actor_claims.get("scope", "").split())
    intersection = subject_scope & actor_scope
    if requested_scope is None:
        result_scope = intersection & FIXED_SCOPES
    else:
        requested = set(requested_scope.split()) & FIXED_SCOPES
        if not requested.issubset(intersection):
            raise ValueError(
                f"requested scopes not present in both subject and actor: "
                f"{requested - intersection!r}"
            )
        result_scope = requested

    now_ts = int(simclock.now())
    exp = min(subject_claims["exp"], actor_claims.get("exp", subject_claims["exp"]))

    act_claim: dict = {"sub": actor_sub}
    if subject_claims.get("act"):
        act_claim["act"] = subject_claims["act"]

    payload = {
        "iss": USER_ISSUER,
        "sub": subject_claims["sub"],
        "aud": audience if audience is not None else SERVER_AUDIENCE,
        "scope": " ".join(sorted(result_scope)),
        "iat": now_ts,
        "exp": exp,
        "act": act_claim,
    }
    return _encode(payload, _USER_KID)


def claims_to_caller(claims: dict) -> dict:
    """Extract user/agent identity from verified token claims."""
    act = claims.get("act")
    if act:
        chain: list[str] = []
        a = act
        while a:
            chain.append(a.get("sub", ""))
            a = a.get("act")
        return {
            "user": claims.get("sub", ""),
            "agent": chain if len(chain) > 1 else chain[0],
        }
    return {"user": claims.get("sub", ""), "agent": claims.get("sub", "")}
