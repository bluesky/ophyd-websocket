# React Hooks
To use ophyd websocket with a React app, the custom hooks ```useOphydPVSocket.ts``` and ```useOphydDeviceSocket.ts``` are provided from Finch along with supporting types.

## Connecting to a single EPICS PV

```javascript
import useOphyPVdSocket from './useOphydPVSocket';

const deviceNameList = ['bl531_xps2:sample_x_mm'];

const { devices, handleSetValueRequest, toggleDeviceLock, toggleExpand } = useOphydPVSocket(deviceNameList)
```

## Connecting to multiple EPICS PVs

```javascript
const deviceNameList =['bl531_xps2:sample_x_mm', 'bl531_xps:sample_y_mm'];

const { devices, handleSetValueRequest, toggleDeviceLock, toggleExpand } = useOphydPVSocket(deviceNameList)
```
Note that even though we sent in multiple pvs, the hook only creates a single websocket, and a single ```devices``` state variable that will contain the live information for all pvs.

## Reading the current value of a connected device

```javascript
let myValue = devices['bl531_xps2:sample_x_mm'].value;
```

## Reading current value using the name property
```javascript
const sampleHolderX = devices['bl531_xps2:sample_x_mm'];
let myValue = devices[sampleHolderX.name];
```

## Checking if device is actively connected
```javascript
let isSampleHolderConnected = sampleHolderX.isConnected;
```

## Updating the value through the set value handler
```javascript
let newValue = 7;
handleSetValueRequest(sampleHolderX.name, newValue);
```
This function sends a message to the websocket to set the new value. The websocket currently does not support writing strings, but this will be added soon.

## Connecting to a single Ophyd Device
Note you will need to have started up the server with a directory that contains your ophyd device python files. 

```javascript
import useOphyDevicedSocket from './useOphydDeviceSocket';

const deviceNameList = ['sim_motor'];

const { devices, handleSetValueRequest, toggleDeviceLock, toggleExpand } = useOphydDeviceSocket(deviceNameList)
```

Reading and setting values are the same as the functions from `useOphydPVSocket`