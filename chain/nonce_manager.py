"""Thread-safe monotonic nonce allocation aligned with on-chain ``pending`` count."""

from __future__ import annotations

import logging
import threading
from typing import Any

logger = logging.getLogger(__name__)


class NonceManager:
    """Serializes nonce reads for one sender so concurrent txs do not collide.

    Each call to :meth:`get_nonce` bumps a local counter after syncing with
    ``eth_getTransactionCount(..., 'pending')`` so gaps and races are avoided.
    """

    def __init__(self, address: str, web3: Any) -> None:
        """
        Args:
            address: Hex checksummed sender address string.
            web3: ``Web3`` instance (uses ``web3.eth.get_transaction_count``).
        """
        self.address = address
        self.web3 = web3
        self.local_nonce: int | None = None
        self.lock = threading.Lock()

    def get_nonce(self) -> int:
        """Return the next nonce to use and reserve it locally.

        Returns:
            Nonce integer suitable for ``tx['nonce']``.
        """
        with self.lock:
            chain_nonce = self.web3.eth.get_transaction_count(self.address, "pending")
            logger.debug(
                "nonce sync: addr_suffix=%s chain=%s local_was=%s",
                self.address[-8:],
                chain_nonce,
                self.local_nonce,
            )

            if self.local_nonce is None:
                self.local_nonce = chain_nonce
            else:
                self.local_nonce = max(self.local_nonce, chain_nonce)

            nonce = self.local_nonce
            self.local_nonce += 1
            logger.info("allocated nonce=%s for addr_suffix=%s", nonce, self.address[-8:])
            return nonce
