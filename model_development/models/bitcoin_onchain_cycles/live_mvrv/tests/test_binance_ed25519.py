import base64
import hashlib
import hmac
from pathlib import Path
import sys
from urllib.parse import urlencode

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa
from cryptography.hazmat.primitives.serialization import Encoding, PrivateFormat, BestAvailableEncryption, NoEncryption

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from live_mvrv.binance_spot import Spot


@pytest.fixture
def pem_file(tmp_path):
    # Synthetic disposable key only. Never use the user's actual private PEM.
    key = ed25519.Ed25519PrivateKey.generate()
    path = tmp_path / "synthetic.pem"
    path.write_bytes(key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))
    return key, path


def test_ed25519_signs_exact_binance_payload(pem_file):
    key, path = pem_file
    spot = Spot(environment="testnet", key="fake-api", key_type="ed25519", private_key_file=path)
    payload = urlencode({"symbol": "BTCUSDT", "clientOrderId": "a+b/c", "timestamp": 1})
    signature = spot.signature(payload)
    key.public_key().verify(base64.b64decode(signature), payload.encode("ascii"))
    assert len(signature) == 88


def test_hmac_remains_compatible():
    spot = Spot(environment="testnet", key="fake-api", secret="fake-secret", key_type="hmac")
    assert spot.signature("timestamp=1") == hmac.new(b"fake-secret", b"timestamp=1", hashlib.sha256).hexdigest()


def test_signed_ed25519_request_roundtrip(pem_file):
    key, path = pem_file
    class Response:
        status_code = 200
        def json(self): return {"balances": []}
    class Session:
        def request(self, method, url, **kwargs):
            params = dict(kwargs["params"])
            signature = params.pop("signature")
            key.public_key().verify(base64.b64decode(signature), urlencode(params).encode("ascii"))
            assert kwargs["headers"]["X-MBX-APIKEY"] == "fake-api"
            assert "secret" not in params and "private_key" not in params
            assert kwargs["allow_redirects"] is False
            return Response()
    Spot(environment="testnet", key="fake-api", key_type="ed25519", private_key_file=path, session=Session()).account()


def encrypted(tmp_path):
    key = ed25519.Ed25519PrivateKey.generate()
    path = tmp_path / "encrypted.pem"
    path.write_bytes(key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, BestAvailableEncryption(b"synthetic-password")))
    return key, path


def test_encrypted_key_passphrase_file(tmp_path, monkeypatch):
    key, path = encrypted(tmp_path)
    secret = tmp_path / "test.passphrase"
    secret.write_bytes(b"\xef\xbb\xbfsynthetic-password\r\n")
    monkeypatch.setenv("BINANCE_PRIVATE_KEY_PASSPHRASE_FILE", str(secret))
    spot = Spot(key="fake-api", key_type="ed25519", private_key_file=path)
    key.public_key().verify(base64.b64decode(spot.signature("a=1")), b"a=1")
    secret.unlink()  # Loaded signer is cached; not reread on every request.
    spot.signature("a=2")


def test_encrypted_key_interactive_status(tmp_path, monkeypatch):
    key, path = encrypted(tmp_path)
    monkeypatch.delenv("BINANCE_PRIVATE_KEY_PASSPHRASE_FILE", raising=False)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("live_mvrv.binance_spot.getpass.getpass", lambda *a: "synthetic-password")
    spot = Spot(key="fake-api", key_type="ed25519", private_key_file=path, prompt_passphrase=True)
    key.public_key().verify(base64.b64decode(spot.signature("a=1")), b"a=1")


def test_unattended_encrypted_key_never_prompts(tmp_path, monkeypatch):
    _, path = encrypted(tmp_path)
    monkeypatch.delenv("BINANCE_PRIVATE_KEY_PASSPHRASE_FILE", raising=False)
    monkeypatch.setattr("live_mvrv.binance_spot.getpass.getpass", lambda *a: pytest.fail("No prompt in worker"))
    with pytest.raises(ValueError, match="passphrase"):
        Spot(key="fake-api", key_type="ed25519", private_key_file=path).signature("a=1")


def test_wrong_passphrase_redacted(tmp_path, monkeypatch):
    _, path = encrypted(tmp_path)
    secret = tmp_path / "test.passphrase"
    secret.write_bytes(b"WRONG-SECRET")
    monkeypatch.setenv("BINANCE_PRIVATE_KEY_PASSPHRASE_FILE", str(secret))
    with pytest.raises(ValueError) as error:
        Spot(key="fake-api", key_type="ed25519", private_key_file=path).signature("a=1")
    assert "WRONG-SECRET" not in str(error.value)


@pytest.mark.parametrize("kind", ["missing", "invalid", "rsa"])
def test_wrong_key_fails_safely(tmp_path, kind):
    path = tmp_path / "wrong.pem"
    if kind == "invalid": path.write_bytes(b"INVALID-SECRET-PEM")
    if kind == "rsa":
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        path.write_bytes(key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))
    with pytest.raises(ValueError) as error:
        Spot(key="fake-api", key_type="ed25519", private_key_file=path).signature("a=1")
    assert "INVALID-SECRET-PEM" not in str(error.value)


def test_ed25519_does_not_unlock_production_post(pem_file):
    _, path = pem_file
    spot = Spot(environment="production_readonly", key="fake-api", key_type="ed25519", private_key_file=path)
    with pytest.raises(ValueError, match="blocked"): spot.request("POST", "/api/v3/order", signed=True)
    assert spot._private_key is None
