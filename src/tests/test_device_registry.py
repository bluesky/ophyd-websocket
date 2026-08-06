"""Test the guarneri-backed device registry."""
import tempfile
from pathlib import Path

import pytest

pytest.importorskip("guarneri")

from device_registry import DeviceRegistry, device_registry

M1 = 'ophyd.EpicsSignal:\n  - {name: m1, read_pv: "IOC:m1"}\n'
DET = 'ophyd.EpicsSignal:\n  - {name: detector, read_pv: "IOC:detector:counts"}\n'


def _write_yaml(text: str) -> str:
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False)
    f.write(text)
    f.close()
    return f.name


def test_device_registry_import():
    assert DeviceRegistry is not None


def test_device_registry_singleton():
    assert DeviceRegistry() is not None
    assert device_registry is not None


def test_device_loading_from_file():
    cfg = _write_yaml(
        'ophyd.EpicsSignal:\n'
        '  - {name: m1, read_pv: "IOC:m1"}\n'
        '  - {name: detector, read_pv: "IOC:detector:counts"}\n'
    )
    try:
        registry = DeviceRegistry()
        registry.clear()
        registry.load_config(cfg, fake=True)
        devices = registry.list_devices()
        assert set(devices) == {"m1", "detector"}, devices
    finally:
        Path(cfg).unlink()


def test_device_loading_from_directory():
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "devices_a.yaml").write_text(
            'ophyd.EpicsSignal:\n  - {name: motor1, read_pv: "IOC:m1"}\n'
        )
        (Path(d) / "devices_b.yaml").write_text(
            'ophyd.EpicsSignal:\n  - {name: motor2, read_pv: "IOC:m2"}\n'
        )
        (Path(d) / "readme.txt").write_text("ignored")  # non-config, skipped
        registry = DeviceRegistry()
        registry.clear()
        registry.load_config(d, fake=True)
        devices = registry.list_devices()
        assert set(devices) == {"motor1", "motor2"}, devices


def test_non_device_configs_skipped_in_directory():
    # A BITS-style configs/ dir mixes device YAMLs with non-device configs
    # (iconfig.yml, extra_logging.yml, and tiled_*.yml tiled server configs).
    # A directory load only picks up "devices*" files, so the non-device configs
    # are ignored -- parsing them as device configs would fail (their top-level
    # values are settings, not lists of device kwargs).
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "devices_motors.yml").write_text(
            'ophyd.EpicsMotor:\n  - {name: m1, prefix: "IOC:m1:"}\n'
        )
        (Path(d) / "iconfig.yml").write_text(
            "DATABROKER_CATALOG: temp\nBEC:\n  PLOTS: false\n"
        )
        (Path(d) / "extra_logging.yml").write_text("version: 1\n")
        (Path(d) / "tiled_config_bl531.yml").write_text(
            "authentication:\n  allow_anonymous_access: true\n"
            "uvicorn:\n  port: 8000\n"
        )
        registry = DeviceRegistry()
        registry.clear()
        registry.load_config(d, fake=True)
        assert set(registry.list_devices()) == {"m1"}, registry.list_devices()


def test_get_device_by_name():
    cfg = _write_yaml('ophyd.EpicsSignal:\n  - {name: test_device, read_pv: "IOC:test"}\n')
    try:
        registry = DeviceRegistry()
        registry.clear()
        registry.load_config(cfg, fake=True)
        device = registry.get_device("test_device")
        assert device is not None
        assert device.name == "test_device"
        assert registry.get_device("nonexistent") is None
    finally:
        Path(cfg).unlink()


def test_device_registry_clear():
    cfg = _write_yaml(M1)
    try:
        registry = DeviceRegistry()
        registry.load_config(cfg, fake=True)
        assert len(registry.list_devices()) > 0
        registry.clear()
        assert registry.list_devices() == []
    finally:
        Path(cfg).unlink()


def test_missing_config_raises():
    registry = DeviceRegistry()
    registry.clear()
    with pytest.raises(FileNotFoundError):
        registry.load_config("/nonexistent/devices.yaml", fake=True)
    assert registry.list_devices() == []


def test_unknown_device_class_is_skipped():
    # guarneri warns and skips a device_class it cannot import; nothing registered.
    cfg = _write_yaml('not_a_real.module.Klass:\n  - {name: x, prefix: "IOC:"}\n')
    try:
        registry = DeviceRegistry()
        registry.clear()
        registry.load_config(cfg, fake=True)
        assert registry.list_devices() == []
    finally:
        Path(cfg).unlink()
