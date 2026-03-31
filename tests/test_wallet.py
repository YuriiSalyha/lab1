import logging
import os
import pickle
from unittest.mock import patch

import pytest
from eth_account import Account

from core._secret_str import _MASKED, SecretStr
from core.errors import WalletSecurityError, WalletValidationError
from core.wallet import WalletManager

# Deterministic test key — NOT a real wallet, safe to hardcode in tests.
TEST_PRIVATE_KEY = "0x" + "ab" * 32
TEST_ACCOUNT = Account.from_key(TEST_PRIVATE_KEY)
TEST_ADDRESS = TEST_ACCOUNT.address


# ---------------------------------------------------------------------------
# SecretStr
# ---------------------------------------------------------------------------


class TestSecretStr:
    def test_str_is_masked(self):
        s = SecretStr("supersecret")
        assert str(s) == _MASKED

    def test_repr_is_masked(self):
        s = SecretStr("supersecret")
        assert repr(s) == _MASKED

    def test_get_secret_value_returns_original(self):
        s = SecretStr("supersecret")
        assert s.get_secret_value() == "supersecret"

    def test_equality(self):
        a = SecretStr("same")
        b = SecretStr("same")
        assert a == b

    def test_inequality(self):
        a = SecretStr("one")
        b = SecretStr("two")
        assert a != b

    def test_not_equal_to_plain_string(self):
        s = SecretStr("value")
        assert s != "value"

    def test_hash_consistency(self):
        a = SecretStr("key")
        b = SecretStr("key")
        assert hash(a) == hash(b)

    def test_cannot_pickle(self):
        s = SecretStr("secret")
        with pytest.raises(WalletSecurityError, match="Cannot pickle"):
            pickle.dumps(s)

    def test_fstring_is_masked(self):
        s = SecretStr("leaked")
        assert "leaked" not in f"value={s}"
        assert _MASKED in f"value={s}"


# ---------------------------------------------------------------------------
# WalletManager.__init__
# ---------------------------------------------------------------------------


class TestWalletManagerInit:
    def test_valid_hex_key(self):
        wm = WalletManager(TEST_PRIVATE_KEY)
        assert wm.address == TEST_ADDRESS

    def test_valid_key_without_prefix(self):
        wm = WalletManager("ab" * 32)
        assert wm.address == TEST_ADDRESS

    def test_invalid_key_format(self):
        with pytest.raises(WalletValidationError, match="Invalid private key format"):
            WalletManager("not-a-valid-key")

    def test_empty_string_rejected(self):
        with pytest.raises(WalletValidationError, match="non-empty string"):
            WalletManager("")

    def test_none_rejected(self):
        with pytest.raises(WalletValidationError, match="non-empty string"):
            WalletManager(None)

    def test_int_rejected(self):
        with pytest.raises(WalletValidationError, match="non-empty string"):
            WalletManager(12345)


# ---------------------------------------------------------------------------
# WalletManager.from_env
# ---------------------------------------------------------------------------


class TestWalletManagerFromEnv:
    def test_loads_key_from_env(self):
        with patch.dict(os.environ, {"TEST_PK": TEST_PRIVATE_KEY}):
            wm = WalletManager.from_env("TEST_PK")
            assert wm.address == TEST_ADDRESS

    def test_missing_env_var_raises(self):
        env = os.environ.copy()
        env.pop("MISSING_VAR", None)
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(WalletValidationError, match="not set"):
                WalletManager.from_env("MISSING_VAR")


# ---------------------------------------------------------------------------
# WalletManager.generate
# ---------------------------------------------------------------------------


class TestWalletManagerGenerate:
    def test_returns_manager_and_key(self):
        manager, pk_hex = WalletManager.generate()
        assert isinstance(manager, WalletManager)
        assert isinstance(pk_hex, str)
        assert len(pk_hex) > 0

    def test_generated_key_matches_address(self):
        manager, pk_hex = WalletManager.generate()
        expected_address = Account.from_key(pk_hex).address
        assert manager.address == expected_address

    def test_each_generation_is_unique(self):
        _, pk1 = WalletManager.generate()
        _, pk2 = WalletManager.generate()
        assert pk1 != pk2


# ---------------------------------------------------------------------------
# WalletManager.address
# ---------------------------------------------------------------------------


class TestWalletManagerAddress:
    def test_returns_checksummed_address(self):
        wm = WalletManager(TEST_PRIVATE_KEY)
        assert wm.address.startswith("0x")
        assert wm.address == TEST_ADDRESS


# ---------------------------------------------------------------------------
# WalletManager.sign_message
# ---------------------------------------------------------------------------


class TestWalletManagerSignMessage:
    def test_sign_valid_message(self):
        wm = WalletManager(TEST_PRIVATE_KEY)
        signed = wm.sign_message("hello world")
        assert signed is not None
        assert hasattr(signed, "signature")

    def test_deterministic_signature(self):
        wm = WalletManager(TEST_PRIVATE_KEY)
        sig1 = wm.sign_message("deterministic")
        sig2 = wm.sign_message("deterministic")
        assert sig1.signature == sig2.signature

    def test_different_messages_different_signatures(self):
        wm = WalletManager(TEST_PRIVATE_KEY)
        sig1 = wm.sign_message("message A")
        sig2 = wm.sign_message("message B")
        assert sig1.signature != sig2.signature

    def test_empty_string_rejected(self):
        wm = WalletManager(TEST_PRIVATE_KEY)
        with pytest.raises(WalletValidationError, match="non-empty string"):
            wm.sign_message("")

    def test_non_string_rejected(self):
        wm = WalletManager(TEST_PRIVATE_KEY)
        with pytest.raises(WalletValidationError):
            wm.sign_message(123)

    def test_none_rejected(self):
        wm = WalletManager(TEST_PRIVATE_KEY)
        with pytest.raises(WalletValidationError):
            wm.sign_message(None)


# ---------------------------------------------------------------------------
# WalletManager.sign_transaction
# ---------------------------------------------------------------------------

VALID_TX = {
    "to": "0x0000000000000000000000000000000000000000",
    "value": 0,
    "gas": 21000,
    "gasPrice": 1_000_000_000,
    "nonce": 0,
    "chainId": 1,
}


class TestWalletManagerSignTransaction:
    def test_sign_valid_transaction(self):
        wm = WalletManager(TEST_PRIVATE_KEY)
        signed = wm.sign_transaction(VALID_TX)
        assert signed is not None
        assert hasattr(signed, "raw_transaction")

    def test_empty_dict_rejected(self):
        wm = WalletManager(TEST_PRIVATE_KEY)
        with pytest.raises(WalletValidationError):
            wm.sign_transaction({})

    def test_non_dict_rejected(self):
        wm = WalletManager(TEST_PRIVATE_KEY)
        with pytest.raises(WalletValidationError):
            wm.sign_transaction("not a dict")

    def test_none_rejected(self):
        wm = WalletManager(TEST_PRIVATE_KEY)
        with pytest.raises(WalletValidationError):
            wm.sign_transaction(None)

    def test_invalid_fields_raise_validation_error(self):
        wm = WalletManager(TEST_PRIVATE_KEY)
        with pytest.raises(WalletValidationError, match="Failed to sign"):
            wm.sign_transaction({"invalid_field": "value"})


# ---------------------------------------------------------------------------
# WalletManager.sign_typed_data
# ---------------------------------------------------------------------------


class TestWalletManagerSignTypedData:
    def test_empty_domain_rejected(self):
        wm = WalletManager(TEST_PRIVATE_KEY)
        with pytest.raises(WalletValidationError, match="non-empty"):
            wm.sign_typed_data({}, {"T": [{"name": "x", "type": "string"}]}, {"x": "1"})

    def test_empty_types_rejected(self):
        wm = WalletManager(TEST_PRIVATE_KEY)
        with pytest.raises(WalletValidationError, match="non-empty"):
            wm.sign_typed_data({"name": "D"}, {}, {"x": "1"})

    def test_empty_value_rejected(self):
        wm = WalletManager(TEST_PRIVATE_KEY)
        with pytest.raises(WalletValidationError, match="non-empty"):
            wm.sign_typed_data({"name": "D"}, {"T": [{"name": "x", "type": "string"}]}, {})

    def test_invalid_schema_raises_validation_error(self):
        wm = WalletManager(TEST_PRIVATE_KEY)
        with pytest.raises(WalletValidationError, match="Failed to encode"):
            wm.sign_typed_data(
                {"name": "Bad"},
                {"BadType": "not a list"},
                {"field": "value"},
            )


# ---------------------------------------------------------------------------
# Security: private key must NEVER leak
# ---------------------------------------------------------------------------


class TestWalletManagerSecurity:
    def test_str_does_not_expose_key(self):
        wm = WalletManager(TEST_PRIVATE_KEY)
        text = str(wm)
        assert TEST_PRIVATE_KEY not in text
        assert TEST_PRIVATE_KEY[2:] not in text

    def test_repr_does_not_expose_key(self):
        wm = WalletManager(TEST_PRIVATE_KEY)
        text = repr(wm)
        assert TEST_PRIVATE_KEY not in text
        assert TEST_PRIVATE_KEY[2:] not in text

    def test_no_dict_access(self):
        """__slots__ prevents __dict__, so vars()/inspection won't leak the key."""
        wm = WalletManager(TEST_PRIVATE_KEY)
        with pytest.raises(AttributeError):
            _ = wm.__dict__

    def test_cannot_pickle(self):
        wm = WalletManager(TEST_PRIVATE_KEY)
        with pytest.raises(WalletSecurityError, match="cannot be pickled"):
            pickle.dumps(wm)

    def test_exception_does_not_contain_key(self):
        wm = WalletManager(TEST_PRIVATE_KEY)
        try:
            wm.sign_transaction({"invalid": "data"})
        except WalletValidationError as e:
            assert TEST_PRIVATE_KEY not in str(e)
            assert TEST_PRIVATE_KEY[2:] not in str(e)

    def test_sanitize_scrubs_key_from_error_message(self):
        """If an underlying library leaks the key in an error, _sanitize removes it."""
        wm = WalletManager(TEST_PRIVATE_KEY)
        bare_key = TEST_PRIVATE_KEY[2:]
        dirty_msg = f"Something failed with key {bare_key} involved"
        cleaned = wm._sanitize(dirty_msg)
        assert bare_key not in cleaned
        assert _MASKED in cleaned

    def test_sanitize_scrubs_prefixed_key(self):
        wm = WalletManager(TEST_PRIVATE_KEY)
        dirty_msg = f"Error: key={TEST_PRIVATE_KEY}"
        cleaned = wm._sanitize(dirty_msg)
        assert TEST_PRIVATE_KEY not in cleaned
        assert _MASKED in cleaned

    def test_exception_with_leaked_key_is_sanitized(self):
        """Simulate an underlying library raising an error that contains the private key."""
        wm = WalletManager(TEST_PRIVATE_KEY)
        bare_key = TEST_PRIVATE_KEY[2:]

        with patch.object(
            wm._account,
            "sign_transaction",
            side_effect=Exception(f"internal error key={bare_key}"),
        ):
            with pytest.raises(WalletValidationError) as exc_info:
                wm.sign_transaction(VALID_TX)
            assert bare_key not in str(exc_info.value)
            assert _MASKED in str(exc_info.value)

    def test_exception_chain_is_broken(self):
        """from None prevents the original exception (with local vars) from leaking."""
        wm = WalletManager(TEST_PRIVATE_KEY)
        with pytest.raises(WalletValidationError) as exc_info:
            wm.sign_transaction({"invalid": "data"})
        assert exc_info.value.__cause__ is None

    def test_logging_does_not_expose_key(self, caplog):
        with caplog.at_level(logging.DEBUG, logger="core.wallet"):
            wm = WalletManager(TEST_PRIVATE_KEY)
            _ = str(wm)
            _ = repr(wm)
        for record in caplog.records:
            assert TEST_PRIVATE_KEY not in record.getMessage()
            assert TEST_PRIVATE_KEY[2:] not in record.getMessage()

    def test_private_key_wrapper_in_slot(self):
        """Even if someone accesses the mangled slot, they get SecretStr, not raw key."""
        wm = WalletManager(TEST_PRIVATE_KEY)
        mangled = wm._WalletManager__private_key
        assert isinstance(mangled, SecretStr)
        assert str(mangled) == _MASKED
        assert repr(mangled) == _MASKED


# ---------------------------------------------------------------------------
# WalletManager keyfile round-trip
# ---------------------------------------------------------------------------

KEYFILE_PASSWORD = "strong_password_123"


class TestWalletManagerKeyfile:
    def test_export_and_import_roundtrip(self, tmp_path):
        wm = WalletManager(TEST_PRIVATE_KEY)
        keyfile = str(tmp_path / "wallet.json")
        wm.to_keyfile(keyfile, KEYFILE_PASSWORD)

        loaded = WalletManager.from_keyfile(keyfile, KEYFILE_PASSWORD)
        assert loaded.address == wm.address

    def test_wrong_password_raises_security_error(self, tmp_path):
        wm = WalletManager(TEST_PRIVATE_KEY)
        keyfile = str(tmp_path / "wallet.json")
        wm.to_keyfile(keyfile, KEYFILE_PASSWORD)

        with pytest.raises(WalletSecurityError, match="Invalid password"):
            WalletManager.from_keyfile(keyfile, "wrong_password!!")

    def test_missing_keyfile_raises(self):
        with pytest.raises(FileNotFoundError, match="not found"):
            WalletManager.from_keyfile("/nonexistent/path.json", "password")

    def test_weak_password_rejected(self, tmp_path):
        wm = WalletManager(TEST_PRIVATE_KEY)
        keyfile = str(tmp_path / "wallet.json")
        with pytest.raises(WalletSecurityError, match="at least 8 characters"):
            wm.to_keyfile(keyfile, "short")

    def test_empty_password_rejected(self, tmp_path):
        wm = WalletManager(TEST_PRIVATE_KEY)
        keyfile = str(tmp_path / "wallet.json")
        with pytest.raises(WalletSecurityError):
            wm.to_keyfile(keyfile, "")

    def test_keyfile_does_not_contain_raw_key(self, tmp_path):
        wm = WalletManager(TEST_PRIVATE_KEY)
        keyfile = str(tmp_path / "wallet.json")
        wm.to_keyfile(keyfile, KEYFILE_PASSWORD)

        with open(keyfile, "r") as f:
            content = f.read()
        assert TEST_PRIVATE_KEY not in content
        assert TEST_PRIVATE_KEY[2:] not in content
