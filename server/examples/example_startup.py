"""
Example startup file for Ophyd device registry

This file demonstrates how to define Ophyd devices that will be automatically
loaded into the device registry when the server starts with --startup-dir flag.

Any Ophyd Device or EpicsSignal instances defined at module level will be
automatically detected and added to the registry.
"""

from ophyd import EpicsSignal, EpicsMotor, Device, Component

# Simple EPICS signals - these will be detected and added to registry
motor1 = EpicsSignal("IOC:m1", name="motor1")
motor2 = EpicsMotor("IOC:m2", name="motor2")
detector_counts = EpicsSignal("IOC:detector:counts", name="detector_counts")

# Custom Ophyd Device class
class SimpleMotor(Device):
    """A simple motor device with position and velocity"""
    m1 = Component(EpicsSignal, "m1")
    m2 = Component(EpicsSignal, "m2")

sim_motor_device = SimpleMotor("IOC:", name="sim_motor_device")

# This will NOT be detected (it's a class, not an instance)
class AnotherDevice(Device):
    pass

# This will NOT be detected (starts with underscore)
_private_signal = EpicsSignal("IOC:private", name="private")

print("Example startup file loaded - devices defined:")
print("- motor1, motor2, detector_counts (EpicsSignals)")
print("- sample_motor, scan_motor (SimpleMotor devices)")

from ophyd.sim import hw

# Import ALL simulated Ophyd objects in global namespace (borrowed from ophyd.sim)
globals().update(hw().__dict__)
del hw