import pytest

from core.errors import WalletValidationError
from core.serializer import CanonicalSerializer

# ---------------------------------------------------------------------------
# Basic serialization
# ---------------------------------------------------------------------------


class TestSerializeBasic:
    def test_simple_dict(self):
        assert CanonicalSerializer.serialize({"b": 2, "a": 1}) == b'{"a":1,"b":2}'

    def test_simple_list(self):
        assert CanonicalSerializer.serialize([3, 1, 2]) == b"[3,1,2]"

    def test_string(self):
        assert CanonicalSerializer.serialize("hello") == b'"hello"'

    def test_integer(self):
        assert CanonicalSerializer.serialize(42) == b"42"

    def test_boolean_true(self):
        assert CanonicalSerializer.serialize(True) == b"true"

    def test_boolean_false(self):
        assert CanonicalSerializer.serialize(False) == b"false"

    def test_tuple_serialized_as_list(self):
        assert CanonicalSerializer.serialize((1, 2, 3)) == b"[1,2,3]"


# ---------------------------------------------------------------------------
# Edge case 1: Nested objects with mixed key orders
# ---------------------------------------------------------------------------


class TestNestedMixedKeyOrders:
    def test_nested_dicts_sorted(self):
        obj = {"z": {"b": 2, "a": 1}, "a": {"y": 3, "x": 4}}
        assert CanonicalSerializer.serialize(obj) == b'{"a":{"x":4,"y":3},"z":{"a":1,"b":2}}'

    def test_same_data_different_insertion_order(self):
        obj_a = {"z": 1, "a": 2, "m": 3}
        obj_b = {"a": 2, "m": 3, "z": 1}
        assert CanonicalSerializer.serialize(obj_a) == CanonicalSerializer.serialize(obj_b)

    def test_deeply_nested(self):
        obj_a = {"b": {"d": {"f": 1, "e": 2}, "c": 3}, "a": 4}
        obj_b = {"a": 4, "b": {"c": 3, "d": {"e": 2, "f": 1}}}
        assert CanonicalSerializer.serialize(obj_a) == CanonicalSerializer.serialize(obj_b)

    def test_dict_inside_list(self):
        obj = [{"b": 1, "a": 2}, {"d": 3, "c": 4}]
        assert CanonicalSerializer.serialize(obj) == b'[{"a":2,"b":1},{"c":4,"d":3}]'

    def test_three_level_nesting(self):
        a = {"c": {"b": {"a": 1}}}
        b = {"c": {"b": {"a": 1}}}
        assert CanonicalSerializer.serialize(a) == CanonicalSerializer.serialize(b)


# ---------------------------------------------------------------------------
# Edge case 2: Unicode strings (emoji, non-ASCII)
# ---------------------------------------------------------------------------


class TestUnicodeStrings:
    def test_emoji_preserved(self):
        result = CanonicalSerializer.serialize({"emoji": "🚀🌍"})
        assert "🚀🌍" in result.decode("utf-8")

    def test_cjk_characters(self):
        result = CanonicalSerializer.serialize({"名前": "太郎"})
        decoded = result.decode("utf-8")
        assert "名前" in decoded
        assert "太郎" in decoded

    def test_mixed_unicode_and_ascii(self):
        obj = {"name": "José", "city": "München", "note": "café ☕"}
        result = CanonicalSerializer.serialize(obj)
        decoded = result.decode("utf-8")
        assert "José" in decoded
        assert "München" in decoded
        assert "café ☕" in decoded

    def test_emoji_in_list(self):
        result = CanonicalSerializer.serialize(["🎉", "✅", "❌"])
        assert result == '["🎉","✅","❌"]'.encode("utf-8")

    def test_unicode_determinism(self):
        obj = {"emoji": "🎉", "text": "日本語"}
        assert CanonicalSerializer.verify_determinism(obj)


# ---------------------------------------------------------------------------
# Edge case 3: Very large integers (> 2^53, JavaScript unsafe)
# ---------------------------------------------------------------------------


class TestLargeIntegers:
    def test_above_js_max_safe_integer(self):
        big = 2**53 + 1  # 9007199254740993
        result = CanonicalSerializer.serialize({"amount": big})
        assert str(big).encode() in result

    def test_2_pow_256(self):
        big = 2**256
        result = CanonicalSerializer.serialize(big)
        assert result == str(big).encode()

    def test_negative_large_integer(self):
        big = -(2**64)
        result = CanonicalSerializer.serialize({"val": big})
        assert str(big).encode() in result

    def test_large_int_determinism(self):
        obj = {"a": 2**128, "b": 2**256}
        assert CanonicalSerializer.verify_determinism(obj)

    def test_large_int_hash_stable(self):
        obj = {"wei": 2**128}
        assert CanonicalSerializer.hash(obj) == CanonicalSerializer.hash(obj)


# ---------------------------------------------------------------------------
# Edge case 4: None / null values
# ---------------------------------------------------------------------------


class TestNoneNullValues:
    def test_top_level_none(self):
        assert CanonicalSerializer.serialize(None) == b"null"

    def test_none_in_dict(self):
        assert CanonicalSerializer.serialize({"key": None}) == b'{"key":null}'

    def test_none_in_list(self):
        assert CanonicalSerializer.serialize([1, None, 3]) == b"[1,null,3]"

    def test_nested_none(self):
        obj = {"a": {"b": None}, "c": [None]}
        assert CanonicalSerializer.serialize(obj) == b'{"a":{"b":null},"c":[null]}'

    def test_none_roundtrips(self):
        obj = {"key": None}
        restored = CanonicalSerializer.deserialize(CanonicalSerializer.serialize(obj))
        assert restored == obj
        assert restored["key"] is None


# ---------------------------------------------------------------------------
# Edge case 5: Empty objects / arrays
# ---------------------------------------------------------------------------


class TestEmptyObjectsArrays:
    def test_empty_dict(self):
        assert CanonicalSerializer.serialize({}) == b"{}"

    def test_empty_list(self):
        assert CanonicalSerializer.serialize([]) == b"[]"

    def test_nested_empty_dict(self):
        assert CanonicalSerializer.serialize({"a": {}}) == b'{"a":{}}'

    def test_nested_empty_list(self):
        assert CanonicalSerializer.serialize({"a": []}) == b'{"a":[]}'

    def test_mixed_empty(self):
        assert CanonicalSerializer.serialize({"a": {}, "b": []}) == b'{"a":{},"b":[]}'

    def test_empty_roundtrips(self):
        for obj in ({}, [], {"a": {}, "b": []}):
            assert CanonicalSerializer.deserialize(CanonicalSerializer.serialize(obj)) == obj


# ---------------------------------------------------------------------------
# Edge case 6: Floating point — must REJECT
# ---------------------------------------------------------------------------


class TestFloatRejection:
    def test_top_level_float(self):
        with pytest.raises(WalletValidationError, match="Floating point"):
            CanonicalSerializer.serialize(3.14)

    def test_float_in_dict_value(self):
        with pytest.raises(WalletValidationError, match="Floating point"):
            CanonicalSerializer.serialize({"price": 1.5})

    def test_float_in_list(self):
        with pytest.raises(WalletValidationError, match="Floating point"):
            CanonicalSerializer.serialize([1, 2.0, 3])

    def test_float_in_nested_dict(self):
        with pytest.raises(WalletValidationError, match="Floating point"):
            CanonicalSerializer.serialize({"a": {"b": 0.1}})

    def test_nan_rejected(self):
        with pytest.raises(WalletValidationError, match="Floating point"):
            CanonicalSerializer.serialize(float("nan"))

    def test_inf_rejected(self):
        with pytest.raises(WalletValidationError, match="Floating point"):
            CanonicalSerializer.serialize(float("inf"))

    def test_negative_inf_rejected(self):
        with pytest.raises(WalletValidationError, match="Floating point"):
            CanonicalSerializer.serialize(float("-inf"))

    def test_float_zero_rejected(self):
        with pytest.raises(WalletValidationError, match="Floating point"):
            CanonicalSerializer.serialize(0.0)

    def test_error_includes_path(self):
        with pytest.raises(WalletValidationError, match=r"\$\.outer\.inner"):
            CanonicalSerializer.serialize({"outer": {"inner": 9.9}})


# ---------------------------------------------------------------------------
# Additional validation: sets and non-string keys
# ---------------------------------------------------------------------------


class TestSetRejection:
    def test_top_level_set(self):
        with pytest.raises(WalletValidationError, match="Set"):
            CanonicalSerializer.serialize({1, 2, 3})

    def test_set_nested_in_dict(self):
        with pytest.raises(WalletValidationError, match="Set"):
            CanonicalSerializer.serialize({"items": {1, 2}})

    def test_error_includes_path(self):
        with pytest.raises(WalletValidationError, match=r"\$\.items"):
            CanonicalSerializer.serialize({"items": {1, 2}})


class TestNonStringKeyRejection:
    def test_int_key(self):
        with pytest.raises(WalletValidationError, match="Non-string dict key"):
            CanonicalSerializer.serialize({1: "value"})

    def test_none_key(self):
        with pytest.raises(WalletValidationError, match="Non-string dict key"):
            CanonicalSerializer.serialize({None: "value"})

    def test_bool_key(self):
        with pytest.raises(WalletValidationError, match="Non-string dict key"):
            CanonicalSerializer.serialize({True: "value"})


# ---------------------------------------------------------------------------
# hash()
# ---------------------------------------------------------------------------


class TestHash:
    def test_returns_32_bytes(self):
        h = CanonicalSerializer.hash({"a": 1})
        assert isinstance(h, bytes)
        assert len(h) == 32

    def test_same_data_different_order_same_hash(self):
        h1 = CanonicalSerializer.hash({"b": 2, "a": 1})
        h2 = CanonicalSerializer.hash({"a": 1, "b": 2})
        assert h1 == h2

    def test_different_data_different_hash(self):
        h1 = CanonicalSerializer.hash({"a": 1})
        h2 = CanonicalSerializer.hash({"a": 2})
        assert h1 != h2


# ---------------------------------------------------------------------------
# deserialize()
# ---------------------------------------------------------------------------


class TestDeserialize:
    def test_roundtrip_complex(self):
        original = {"z": [1, 2, None], "a": "hello", "m": True}
        data = CanonicalSerializer.serialize(original)
        restored = CanonicalSerializer.deserialize(data)
        assert restored == original

    def test_invalid_json_bytes(self):
        with pytest.raises(WalletValidationError, match="Failed to deserialize"):
            CanonicalSerializer.deserialize(b"not json")

    def test_non_bytes_rejected(self):
        with pytest.raises(WalletValidationError, match="Expected bytes"):
            CanonicalSerializer.deserialize("string input")

    def test_invalid_utf8(self):
        with pytest.raises(WalletValidationError, match="Failed to deserialize"):
            CanonicalSerializer.deserialize(b"\xff\xfe")


# ---------------------------------------------------------------------------
# verify_determinism()
# ---------------------------------------------------------------------------


class TestVerifyDeterminism:
    def test_simple_object(self):
        assert CanonicalSerializer.verify_determinism({"a": 1, "b": 2}) is True

    def test_complex_object(self):
        obj = {"z": [1, None, "🎉"], "a": {"nested": 2**100}}
        assert CanonicalSerializer.verify_determinism(obj, iterations=50) is True

    def test_zero_iterations_rejected(self):
        with pytest.raises(WalletValidationError, match="positive integer"):
            CanonicalSerializer.verify_determinism({}, iterations=0)

    def test_negative_iterations_rejected(self):
        with pytest.raises(WalletValidationError, match="positive integer"):
            CanonicalSerializer.verify_determinism({}, iterations=-5)


# ---------------------------------------------------------------------------
# Non-serializable types (caught by json.dumps after validation)
# ---------------------------------------------------------------------------


class TestNonSerializableTypes:
    def test_custom_object(self):
        with pytest.raises(WalletValidationError, match="Failed to serialize"):
            CanonicalSerializer.serialize({"obj": object()})

    def test_bytes_value(self):
        with pytest.raises(WalletValidationError, match="Failed to serialize"):
            CanonicalSerializer.serialize({"data": b"raw"})
