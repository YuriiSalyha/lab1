"""Load and use a local Ethereum account: sign messages, typed data, and transactions."""

from __future__ import annotations

import json
import logging
import os

from eth_account import Account
from eth_account.datastructures import SignedMessage, SignedTransaction
from eth_account.messages import encode_defunct, encode_typed_data

from core._secret_str import _MASKED, SecretStr
from core.errors import WalletSecurityError, WalletValidationError

logger = logging.getLogger(__name__)


class WalletManager:
    """Holds an :class:`eth_account.Account` and exposes signing helpers.

    Private keys are wrapped in :class:`SecretStr` and must never be logged.
    """

    __slots__ = ("__private_key", "_account")

    def __init__(self, private_key: str) -> None:
        """
        Args:
            private_key: Hex-encoded secp256k1 private key (with or without ``0x``).
        """
        if not private_key or not isinstance(private_key, str):
            raise WalletValidationError("Private key must be a non-empty string")
        try:
            self._account = Account.from_key(private_key)
        except Exception:
            logger.warning("rejected invalid private key format")
            raise WalletValidationError("Invalid private key format") from None
        self.__private_key = SecretStr(private_key)
        logger.debug("wallet loaded: address_suffix=%s", self._account.address[-8:])

    @classmethod
    def from_env(cls, env_var: str) -> WalletManager:
        """
        Args:
            env_var: Environment variable name holding the hex private key.

        Returns:
            ``WalletManager`` instance.

        Raises:
            WalletValidationError: Variable is unset or empty.
        """
        private_key = os.getenv(env_var)
        if not private_key:
            logger.error("environment variable %s is not set", env_var)
            raise WalletValidationError(f"Environment variable {env_var} is not set")
        return cls(private_key)

    @classmethod
    def generate(cls) -> tuple[WalletManager, str]:
        """
        Returns:
            ``(manager, private_key_hex)`` — caller must store the key securely.
        """
        account = Account.create()
        pk_hex = account.key.hex()
        logger.info("generated new wallet")
        return cls(pk_hex), pk_hex

    @property
    def address(self) -> str:
        """Checksummed ``0x`` address."""
        return self._account.address

    def _sanitize(self, text: str) -> str:
        """Strip any substring of the private key from *text* (for error messages)."""
        raw = self.__private_key.get_secret_value()
        bare = raw[2:] if raw.startswith("0x") else raw
        sanitized = text.replace(f"0x{bare}", _MASKED)
        sanitized = sanitized.replace(bare, _MASKED)
        return sanitized

    def _validation_error_from_exception(
        self, prefix: str, err: Exception
    ) -> WalletValidationError:
        """Build a :class:`WalletValidationError` with sanitized message (no key leak)."""
        return WalletValidationError(f"{prefix}: {self._sanitize(str(err))}")

    def sign_message(self, message: str) -> SignedMessage:
        """
        Args:
            message: UTF-8 text (EIP-191 personal sign).

        Returns:
            ``SignedMessage`` from eth-account.
        """
        if not message or not isinstance(message, str):
            raise WalletValidationError("Message must be a non-empty string")
        logger.debug("sign_message: length=%s", len(message))
        message_hash = encode_defunct(text=message)
        return self._account.sign_message(message_hash)

    def sign_typed_data(self, domain: dict, types: dict, value: dict) -> SignedMessage:
        """
        Args:
            domain: EIP-712 domain object.
            types: EIP-712 types map.
            value: Message payload.

        Returns:
            EIP-712 signature.

        Raises:
            WalletValidationError: Invalid encoding or empty inputs.
        """
        if not domain or not types or not value:
            raise WalletValidationError("Domain, types, and value must be non-empty dictionaries.")
        try:
            signable_message = encode_typed_data(
                domain_data=domain, message_types=types, message_data=value
            )
            return self._account.sign_message(signable_message)
        except Exception as e:
            raise self._validation_error_from_exception("Failed to encode typed data", e) from None

    def sign_transaction(self, tx: dict) -> SignedTransaction:
        """
        Args:
            tx: Web3 transaction dict (``chainId``, gas, fees, etc.).

        Returns:
            Signed raw transaction wrapper.

        Raises:
            WalletValidationError: Invalid dict or signing failure.
        """
        if not tx or not isinstance(tx, dict):
            raise WalletValidationError("Transaction must be a non-empty dictionary")
        try:
            logger.debug("sign_transaction: keys=%s", sorted(tx.keys()))
            return self._account.sign_transaction(tx)
        except Exception as e:
            raise self._validation_error_from_exception("Failed to sign transaction", e) from None

    def __str__(self) -> str:
        return f"WalletManager(address={self.address})"

    def __repr__(self) -> str:
        return f"WalletManager(address={self.address})"

    def __reduce__(self) -> None:
        raise WalletSecurityError("WalletManager cannot be pickled")

    @classmethod
    def from_keyfile(cls, path: str, password: str) -> WalletManager:
        """
        Args:
            path: Path to encrypted JSON keystore.
            password: Decryption password.

        Returns:
            ``WalletManager`` loaded from the file.

        Raises:
            FileNotFoundError: Path does not exist.
            WalletSecurityError: Wrong password or corrupt file.
        """
        if not os.path.exists(path):
            raise FileNotFoundError(f"Keyfile not found at {path}")
        logger.info("loading keyfile from path=%s", path)
        with open(path, "r") as f:
            encrypted_data = json.load(f)
        try:
            private_key_bytes = Account.decrypt(encrypted_data, password)
            return cls(private_key_bytes.hex())
        except ValueError:
            logger.warning("keyfile decrypt failed")
            raise WalletSecurityError("Invalid password or corrupted keyfile.") from None

    def to_keyfile(self, path: str, password: str) -> None:
        """
        Args:
            path: Output JSON keystore path.
            password: Encryption password (min 8 characters).
        """
        if not password or len(password) < 8:
            raise WalletSecurityError("Password must be at least 8 characters long.")
        encrypted_data = Account.encrypt(self.__private_key.get_secret_value(), password)
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w") as f:
            json.dump(encrypted_data, f)
        logger.info("exported keyfile to path=%s", path)
