"""
Test startup file for test environment
Contains devices that match the test IOC PVs
"""
from ophyd import EpicsSignal, EpicsMotor, Device, Component

# Simple signals matching test IOC
m1 = EpicsSignal("IOC:m1", name="m1")
m2 = EpicsSignal("IOC:m2", name="m2")
detector_counts = EpicsSignal("IOC:detector:counts", name="detector_counts")

# EpicsMotor matching test IOC motor PVs
m2_motor = EpicsMotor("IOC:m2:", name="m2_motor")

# Custom device for testing
class SimpleDetector(Device):
    counts = Component(EpicsSignal, "counts")

detector = SimpleDetector("IOC:detector:", name="detector")

# Camera/Area detector components for testing
class SimpleCamera(Device):
    min_x = Component(EpicsSignal, "MinX")
    min_y = Component(EpicsSignal, "MinY") 
    size_x = Component(EpicsSignal, "SizeX")
    size_y = Component(EpicsSignal, "SizeY")
    color_mode = Component(EpicsSignal, "ColorMode")
    data_type = Component(EpicsSignal, "DataType")
    bin_x = Component(EpicsSignal, "BinX")
    bin_y = Component(EpicsSignal, "BinY")

cam1 = SimpleCamera("IOC:cam1:", name="cam1")

# Image data signal for camera testing
image1_array = EpicsSignal("IOC:image1:ArrayData", name="image1_array")

# Private signal (should be ignored by device registry)
_private_signal = EpicsSignal("IOC:private", name="private_signal")