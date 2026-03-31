import json
import logging
import os

from eth_account import Account
from eth_account.datastructures import SignedMessage, SignedTransaction
from eth_account.messages import encode_defunct, encode_typed_data

from core._secret_str import _MASKED, _secret_str
from core.errors import WalletSecurityError, WalletValidationError

logger = logging.getLogger(__name__)


class WalletManager:
    """
    Manages wallet operations: key loading, signing, verification.

    Keys can be loaded from:
    - Environment variable
    - Encrypted keyfile

    CRITICAL: Private key must never appear in logs, errors, or string representations.
    """

    __slots__ = ("__private_key", "_account")

    def __init__(self, private_key: str):
        if not private_key or not isinstance(private_key, str):
            raise WalletValidationError("Private key must be a non-empty string")
        try:
            self._account = Account.from_key(private_key)
        except Exception:
            raise WalletValidationError("Invalid private key format") from None
        self.__private_key = _secret_str(private_key)

    @classmethod
    def from_env(cls, env_var: str) -> "WalletManager":
        """Load private key from environment variable."""
        private_key = os.getenv(env_var)
        if not private_key:
            logger.error("Environment variable %s is not set", env_var)
            raise WalletValidationError(f"Environment variable {env_var} is not set")
        return cls(private_key)

    @classmethod
    def generate(cls) -> tuple["WalletManager", str]:
        """Generate a new random wallet. Returns (manager, private_key_hex).

        The caller is responsible for displaying/storing the key securely.
        """
        account = Account.create()
        pk_hex = account.key.hex()
        return cls(pk_hex), pk_hex

    @property
    def address(self) -> str:
        """Returns checksummed address."""
        return self._account.address

    def sign_message(self, message: str) -> SignedMessage:
        """Sign an arbitrary message (with EIP-191 prefix)."""
        if not message or not isinstance(message, str):
            raise WalletValidationError("Message must be a non-empty string")
        message_hash = encode_defunct(text=message)
        return self._account.sign_message(message_hash)

    def sign_typed_data(self, domain: dict, types: dict, value: dict) -> SignedMessage:
        """Sign EIP-712 typed data (used by many DeFi protocols)."""
        if not domain or not types or not value:
            raise WalletValidationError("Domain, types, and value must be non-empty dictionaries.")
        try:
            signable_message = encode_typed_data(
                domain_data=domain, message_types=types, message_data=value
            )
            return self._account.sign_message(signable_message)
        except Exception as e:
            raise WalletValidationError(
                f"Failed to encode typed data: {self._sanitize(str(e))}"
            ) from None

    def sign_transaction(self, tx: dict) -> SignedTransaction:
        """Sign a transaction dict."""
        if not tx or not isinstance(tx, dict):
            raise WalletValidationError("Transaction must be a non-empty dictionary")
        try:
            return self._account.sign_transaction(tx)
        except Exception as e:
            raise WalletValidationError(
                f"Failed to sign transaction: {self._sanitize(str(e))}"
            ) from None

    def _sanitize(self, text: str) -> str:
        """Remove any accidental private key occurrence from text."""
        raw = self.__private_key.get_secret_value()
        bare = raw[2:] if raw.startswith("0x") else raw
        sanitized = text.replace(f"0x{bare}", _MASKED)
        sanitized = sanitized.replace(bare, _MASKED)
        return sanitized

    def __str__(self) -> str:
        return f"WalletManager(address={self.address})"

    def __repr__(self) -> str:
        return f"WalletManager(address={self.address})"

    def __reduce__(self):
        raise WalletSecurityError("WalletManager cannot be pickled")

    @classmethod
    def from_keyfile(cls, path: str, password: str) -> "WalletManager":
        """Load from encrypted JSON keyfile."""
        if not os.path.exists(path):
            raise FileNotFoundError(f"Keyfile not found at {path}")
        logger.info("Loading keyfile from %s", path)
        with open(path, "r") as f:
            encrypted_data = json.load(f)
        try:
            private_key_bytes = Account.decrypt(encrypted_data, password)
            return cls(private_key_bytes.hex())
        except ValueError:
            raise WalletSecurityError("Invalid password or corrupted keyfile.") from None

    def to_keyfile(self, path: str, password: str) -> None:
        """Export to encrypted keyfile."""
        if not password or len(password) < 8:
            raise WalletSecurityError("Password must be at least 8 characters long.")
        encrypted_data = Account.encrypt(self.__private_key.get_secret_value(), password)
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w") as f:
            json.dump(encrypted_data, f)
        logger.info("Exported keyfile to %s", path)
