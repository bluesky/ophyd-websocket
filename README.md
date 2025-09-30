# ophyd-websocket
Experimental python based websocket server used to live-monitor and set EPICS/ophyd device values through a web browser.

## Use Case 
If you are building a web browser application and need to:
* Monitor the current value of an EPICS PV or Ophyd device
* Set the value of a device
* Know immediately when device disconnects/reconnects 

then ophyd-websocket can provide these features.

## How it works

Clients send a message to ophyd-websocket with the name of a pv. The websocket then calls EpicsSignal(your_pv) to establish a connection and provide functionality including live updates when a value changes, the device connection status changes, or to set the value. The get and set methods are utilized with ophyd.

Ophyd async is not currently supported.

A single websocket instance can hold any number of pv subscriptions.


# Installation
```bash 
git clone https://github.com/bluesky/ophyd-websocket.git 
```
Optionally set up a conda environment
```bash
conda create -n ophyd_websocket python=3.12
conda activate ophyd_websocket
```
Install requirements

```bash
#/ophyd-websocket
pip install -r requirements.txt
```

# Starting the Websocket

Start the websocket server
```bash
#/ophyd-websocket
python server/server.py
```

Start the websocket server with host and port set in command line
```bash
#/ophyd-websocket
OAS_PORT=8001 OAS_HOST=0.0.0.0 python server/server.py
```

# Example - Subscribing to a pv
Example JSON message to ws://localhost:8000/api/v1/pv-socket
```bash
#JSON Message from client to /api/v1/pv-socket
{
    "action": "subscribe",
    "pv": "IOC:m1"
}
```

Responses from server over websocket:
```bash
#JSON Messages from /api/v1/pv-socket to client

#first message indicates status of subscription attempt
{
    "message": "Subscribed to IOC:m1"
}
#second message is the current value (sent every time value changes)
{
    "pv": "IOC:m1",
    "value": 0.0,
    "timestamp": 1759256744.247916,
    "connected": true,
    "read_access": true,
    "write_access": true
}
#third message is the connection information (only sent on connect/disconnect)
{
    "connected": true,
    "read_access": true,
    "write_access": true,
    "timestamp": 1759256744.247916,
    "status": 0,
    "severity": 0,
    "precision": 5,
    "setpoint_timestamp": null,
    "setpoint_status": null,
    "setpoint_severity": null,
    "lower_ctrl_limit": -100.0,
    "upper_ctrl_limit": 100.0,
    "units": "degrees",
    "enum_strs": null,
    "setpoint_precision": null,
    "sub_type": "meta",
    "obj": "IOC:m1",
    "pv": "IOC:m1"
}
```

JSON message client to /api/v1/pv-socket
```bash
#JSON Message from client to /api/v1/pv-socket
{
    "action": "set",
    "pv": "IOC:m1",
    "value": 10
}
```
Responses from server over websocket:

```bash
#JSON Message from /api/v1/pv-socket to client
{
    "pv": "IOC:m1",
    "value": 10,
    "timestamp": 1759259117.565635,
    "connected": true,
    "read_access": true,
    "write_access": true
}
```

# Messages and Responses
```mermaid
sequenceDiagram
        Browser<<-->>Ophyd-Websocket: Connect
        Browser->>Ophyd-Websocket: Subscribe DMC01
        activate Ophyd-Websocket
        Ophyd-Websocket->>EPICS: Subscribe DMC01
        EPICS<<-->>Ophyd-Websocket: DMC01 Updates
        Ophyd-Websocket->>Browser: DMC01 metadata
        Ophyd-Websocket->>Browser: DMC01 current value
        deactivate Ophyd-Websocket
        External->>EPICS: caput DMC01 newValue
        EPICS->>Ophyd-Websocket: DMC01 update
        activate Ophyd-Websocket
        Ophyd-Websocket->>Browser: DMC01 current value
        deactivate Ophyd-Websocket
```

# Experimental Features
In addition to subscribing to PVs, where you must provide the exact PV name, you can also subscribe to devices, stream area detector images, and stream queue server console output from this server. These features are experimental and not intended for to be stable.

## "Ophyd as a Service" REST API
After starting the server navigate to [http://localhost:8001/docs](http://localhost:8001/docs) to see endpoints and try out functionality. 

You can load up predefined Ophyd devices with a POST request to `http://localhost:8001/api/v1/load-devices`.

These predefined Ophyd devices should live in any python file that can be accessed during server startup. Pass a `--startup-dir` arg to the server with your file or folder.

```bash
python server/server.py --startup-dir /path/to/devices.py
```
Then in your python file instantiate your Ophyd devices, which will then be available when making API calls or websocket subscriptions to devices.

```python
#/path/to/devices.py
from ophyd import EpicsSignal, EpicsMotor, Device, Component

# Simple EPICS signals - these will be detected and added to registry
sim_motor1 = EpicsSignal("IOC:m1", name="motor1")
sim_motor2 = EpicsMotor("IOC:m2", name="motor2")
# Custom Ophyd Device class
class SimpleMotor(Device):
    """A simple motor device with position and velocity"""
    m1 = Component(EpicsSignal, "m1")
    m2 = Component(EpicsSignal, "m2")

sim_motor_device = SimpleMotor("IOC:", name="sim_motor_device")
```


## Stream Queue Server Console Output
Socket Endpoint: /api/v1/qs-console-socket

## Stream Area Detector Images
Socket Endpoint: /api/v1/camera-socket

# Docker setup
```bash
docker build -t ophyd-websocket . 
docker run -p 8001:8001 ophyd-websocket
```