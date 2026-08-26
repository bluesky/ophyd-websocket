import numpy as np

from ophyd_websocket.routers.pv_socket import _serialize_pv_value


def test_serialize_numeric_array_to_list():
    value = np.array([1, 2, 3], dtype=np.int32)
    assert _serialize_pv_value(value) == [1, 2, 3]


def test_serialize_float_array_to_list():
    value = np.array([1.5, 2.5, 3.5], dtype=np.float64)
    assert _serialize_pv_value(value) == [1.5, 2.5, 3.5]


def test_serialize_ascii_byte_array_to_string():
    value = np.array([72, 101, 108, 108, 111, 0], dtype=np.uint8)
    assert _serialize_pv_value(value) == "Hello"


def test_serialize_small_integer_spectrum_stays_array():
    value = np.array([1, 1, 2, 3, 5, 8], dtype=np.int32)
    assert _serialize_pv_value(value) == [1, 1, 2, 3, 5, 8]


def test_serialize_numpy_scalar_to_python_scalar():
    value = np.int32(42)
    assert _serialize_pv_value(value) == 42