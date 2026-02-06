"""
Test IOC using caproto for simulating EPICS devices
"""
import asyncio
from caproto.server import pvproperty, PVGroup, ioc_arg_parser, run

class TestIOC(PVGroup):
    """
    Test IOC that simulates devices used by ophyd-websocket
    Matches the PV structure expected by the example startup devices
    """
    
    # Simple motor signals (for EpicsSignal devices)
    m1 = pvproperty(value=0.0, dtype=float, doc="Motor 1 position")
    m2 = pvproperty(value=5.0, dtype=float, doc="Motor 2 position") 
    detector_counts = pvproperty(value=1000, dtype=int, doc="Detector counts", name="detector:counts")
    
    # EpicsMotor components (for EpicsMotor devices)
    m2_user_readback = pvproperty(value=5.0, dtype=float, doc="Motor 2 readback", name="m2:user_readback")
    m2_user_setpoint = pvproperty(value=5.0, dtype=float, doc="Motor 2 setpoint", name="m2:user_setpoint")
    m2_motor_done_move = pvproperty(value=1, dtype=int, doc="Motor 2 done moving", name="m2:motor_done_move")
    m2_motor_moving = pvproperty(value=0, dtype=int, doc="Motor 2 moving", name="m2:motor_moving")
    m2_high_limit = pvproperty(value=100.0, dtype=float, doc="Motor 2 high limit", name="m2:high_limit")
    m2_low_limit = pvproperty(value=-100.0, dtype=float, doc="Motor 2 low limit", name="m2:low_limit")
    
    # Camera/Area Detector simulation for camera-socket tests
    cam1_MinX = pvproperty(value=0, dtype=int, doc="Camera min X", name="cam1:MinX")
    cam1_MinY = pvproperty(value=0, dtype=int, doc="Camera min Y", name="cam1:MinY")
    cam1_SizeX = pvproperty(value=512, dtype=int, doc="Camera size X", name="cam1:SizeX")
    cam1_SizeY = pvproperty(value=512, dtype=int, doc="Camera size Y", name="cam1:SizeY")
    cam1_ColorMode = pvproperty(value=0, dtype=int, doc="Camera color mode", name="cam1:ColorMode")
    cam1_DataType = pvproperty(value=1, dtype=int, doc="Camera data type", name="cam1:DataType")
    cam1_BinX = pvproperty(value=1, dtype=int, doc="Camera bin X", name="cam1:BinX")
    cam1_BinY = pvproperty(value=1, dtype=int, doc="Camera bin Y", name="cam1:BinY")
    
    # Image array data (simulated camera frames)
    image1_ArrayData = pvproperty(
        value=list(range(100)),  # Small test array
        max_length=1000,  # Reasonable size for testing
        name="image1:ArrayData",
        doc="Image array data"
    )
    
    # Private signal that should NOT be detected
    private = pvproperty(value=42.0, dtype=float, doc="Private signal")
    
    @m1.putter
    async def m1(self, instance, value):
        """Simulate motor 1 movement"""
        print(f"Setting m1 to {value}")
        return value
    
    @m2.putter  
    async def m2(self, instance, value):
        """Simulate motor 2 movement and update related PVs"""
        print(f"Setting m2 to {value}")
        # Update readback and setpoint
        await self.m2_user_readback.write(value)
        await self.m2_user_setpoint.write(value)
        return value
    
    @m2_user_setpoint.putter
    async def m2_user_setpoint(self, instance, value):
        """Handle EpicsMotor setpoint changes"""
        print(f"EpicsMotor m2 setpoint: {value}")
        # Simulate movement
        await self.m2_motor_moving.write(1)
        await asyncio.sleep(0.1)  # Simulate movement time
        await self.m2_user_readback.write(value)
        await self.m2_motor_done_move.write(1)
        await self.m2_motor_moving.write(0)
        return value
        
    @detector_counts.putter
    async def detector_counts(self, instance, value):
        """Simulate detector counts changing"""
        print(f"Setting detector counts to {value}")
        return value

if __name__ == "__main__":
    ioc_options, run_options = ioc_arg_parser(
        default_prefix='IOC:',
        desc="Test IOC for ophyd-websocket testing"
    )
    
    ioc = TestIOC(**ioc_options)
    run(ioc.pvdb, **run_options)