#!/usr/bin/env python3
"""Prove browser-library/Python HPKE compatibility with disposable keys.

Install the package's remote-entry extra and @hpke/core@1.9.0 separately.
Set SLEEPTALK_HPKE_MODULE to its absolute installed module path. No accounts,
authenticators, network endpoints, setup services, or user credentials are used.
"""
import json
from pathlib import Path
import subprocess

from cryptography.hazmat.primitives.asymmetric import ec
from sleeptalk.remote_crypto import INFO, SUITE, canonical, decode, encode


def prove():
    private = ec.generate_private_key(ec.SECP256R1())
    numbers = private.private_numbers()
    public = {"kty": "EC", "crv": "P-256", "x": encode(numbers.public_numbers.x.to_bytes(32, "big")),
              "y": encode(numbers.public_numbers.y.to_bytes(32, "big"))}
    context = canonical({"request_id": "fixture", "provider_field": "deepgram.api_key", "request_revision": 1})
    info = INFO + context
    plaintext = canonical({"value": "synthetic-secret-\u00e9"})
    ciphertext = SUITE.encrypt(plaintext, private.public_key(), info=info)
    data = {"publicKey": public, "privateKey": {**public, "d": encode(numbers.private_value.to_bytes(32, "big"))},
            "info": encode(info), "plaintext": encode(plaintext), "enc": encode(ciphertext[:65]), "ct": encode(ciphertext[65:])}
    result = subprocess.run(["node", str(Path(__file__).with_suffix(".cjs"))], input=json.dumps(data),
                            text=True, capture_output=True, check=True, timeout=15)
    encrypted = json.loads(result.stdout)
    recovered = SUITE.decrypt(decode(encrypted["enc"]) + decode(encrypted["ct"]), private, info=info)
    assert recovered == plaintext
    print("PASS: JavaScript/Python HPKE in both directions; changed context rejected; no external accounts used")


if __name__ == "__main__":
    prove()
