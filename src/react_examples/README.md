# React Hooks
To use ophyd websocket with a React app, a custom hook ```useOphydSocket.ts``` is provided along with supporting types.

## Connecting to a single epics pv

```javascript
import { useMemo } from 'react';
import useOphydSocket from './useOphydSocket';

const deviceNameList = useMemo(()=>['bl531_xps2:sample_x_mm'], []);

const { devices, handleSetValueRequest, toggleDeviceLock, toggleExpand } = useOphydSocket(deviceNameList)
```
Why ```useMemo```?

The point of useMemo is to prevent the custom hook from re-creating the websocket. We memoize the input arg (a list of pvs) so that when eventual updates to ```devices``` tells React to re-render the component, the original input arg is not re-created. 



## Connecting to multiple epics pvs

```javascript
const deviceNameList = useMemo(()=>['bl531_xps2:sample_x_mm', 'bl531_xps:sample_y_mm'], []);

const { devices, handleSetValueRequest, toggleDeviceLock, toggleExpand } = useOphydSocket(deviceNameList)
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

