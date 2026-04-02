"""Unit tests for :mod:`chain.decoder` (no live RPC)."""

from __future__ import annotations

from eth_abi import encode

from chain.decoder import TransactionDecoder

# Known topic0 for Transfer(address,address,uint256)
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


def _pad_addr(addr_hex: str) -> bytes:
    """32-byte topic payload for a 20-byte address (left-padded)."""
    h = addr_hex.replace("0x", "")
    return bytes.fromhex(h.zfill(64))


def test_decode_transfer_calldata():
    to_addr = "0x1111111111111111111111111111111111111111"
    amount = 1_000_000
    body = encode(["address", "uint256"], [to_addr, amount])
    calldata = bytes.fromhex("a9059cbb") + body

    out = TransactionDecoder.decode_function_call(calldata)

    assert out["function"] == "transfer"
    assert out["selector"] == "a9059cbb"
    assert out["signature"] == "transfer(address,uint256)"
    assert out["params"] is not None
    assert out["params"]["to"] == to_addr  # checksummed by decoder
    assert out["params"]["value"] == amount
    assert out["param_names"] == ["to", "value"]


def test_decode_unknown_selector():
    calldata = bytes.fromhex("deadbeef") + b"\x00" * 32
    out = TransactionDecoder.decode_function_call(calldata)

    assert out["function"] == "unknown"
    assert out["selector"] == "deadbeef"
    assert out["signature"] is None
    assert out["params"] is None


def test_decode_empty_calldata():
    out = TransactionDecoder.decode_function_call(b"")
    assert out["function"] == "unknown"
    assert out["params"] is None


def test_decode_swap_exact_tokens_signature():
    """Router selector 0x38ed1739 — params may be None if body corrupt; signature still known."""
    out = TransactionDecoder.decode_function_call(bytes.fromhex("38ed1739"))
    assert out["function"] == "swapExactTokensForTokens"
    assert "uint256" in (out["signature"] or "")


def test_decode_total_supply_no_args():
    out = TransactionDecoder.decode_function_call(bytes.fromhex("18160ddd"))
    assert out["function"] == "totalSupply"
    assert out["params"] == {}
    assert out["signature"] == "totalSupply()"


def test_parse_transfer_event():
    from eth_utils import to_checksum_address

    token_contract = "0x2222222222222222222222222222222222222222"
    sender = "0x3333333333333333333333333333333333333333"
    recipient = "0x4444444444444444444444444444444444444444"
    value = 12345

    data = encode(["uint256"], [value])
    log = {
        "address": token_contract,
        "topics": [
            bytes.fromhex(TRANSFER_TOPIC[2:]),
            _pad_addr(sender),
            _pad_addr(recipient),
        ],
        "data": "0x" + data.hex(),
    }

    ev = TransactionDecoder.parse_event(log)
    assert ev["name"] == "Transfer"
    assert ev["decoded"]["value"] == value
    assert ev["decoded"]["from"] == to_checksum_address(sender)
    assert ev["decoded"]["to"] == to_checksum_address(recipient)


def test_parse_events_list():
    log = {
        "address": "0xaa",
        "topics": [],
        "data": "0x",
    }
    out = TransactionDecoder.parse_events([log])
    assert len(out) == 1
    assert out[0]["name"] == "UnknownEvent"


def test_decode_revert_error_string():
    # Error(string): 0x08c379a2 + ABI-encoded string
    payload = bytes.fromhex("08c379a2") + encode(["string"], ["insufficient liquidity"])
    reason = TransactionDecoder.decode_revert_reason(payload)
    assert reason == "insufficient liquidity"


def test_decode_revert_panic():
    # Panic(uint256): 0x4e487b71 + code 0x11 (arithmetic overflow)
    inner = bytes.fromhex("4e487b71") + encode(["uint256"], [0x11])
    reason = TransactionDecoder.decode_revert_reason(inner)
    assert reason is not None
    assert "Panic" in reason
    assert "overflow" in reason.lower() or "arithmetic" in reason.lower()
