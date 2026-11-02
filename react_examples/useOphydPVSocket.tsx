import { useState, useRef, useEffect, useCallback, useMemo } from 'react';
import { Devices } from './deviceControllerTypes';
import { useOphydApiUrls } from './apiUtils';
import {
    MessageResponse,
    ErrorResponse,
    ValueUpdateResponse,
    MetaUpdateResponse,
} from './ophydPVSocketTypes';

type SubscribeAction = 'subscribe' | 'subscribeSafely' | 'subscribeReadOnly';

/** What the server says this socket's API key may do (see README "Authentication"). */
export interface OphydSocketPermissions {
    read: boolean;
    write: boolean;
    console: boolean;
}

interface UseOphydPVSocketOptions {
    wsUrl?: string;
    /**
     * Queue Server API key -- the same `qServerApiKey` Finch already sends to the
     * Queue Server. Sent as the socket's first message; subscriptions go out only
     * after the server accepts it. Re-sent on every (re)connect.
     */
    apiKey?: string;
    subscribeAction?: SubscribeAction;
}

export default function useOphydPVSocket(
    deviceNameList: string[],
    { wsUrl, apiKey, subscribeAction = 'subscribeSafely' }: UseOphydPVSocketOptions = {},
) {
    // eslint-disable-next-line react-hooks/exhaustive-deps
    const memoizedDeviceNames = useMemo(() => deviceNameList, [JSON.stringify(deviceNameList)]); //device updates can retrigger the hook if inputs aren't memoized
    const configWsUrl = useOphydApiUrls().getWsUrl('pv-socket');
    const apiUrl: string = wsUrl ?? configWsUrl;
    const [devices, setDevices] = useState<Devices>(() => {
        const initialDevices: Devices = {};
        memoizedDeviceNames.forEach((deviceName) => {
            initialDevices[deviceName] = {
                name: deviceName,
                value: '',
                connected: false,
                locked: false,
                timestamp: 0,
                expanded: false,
                pv: deviceName,
                read_access: false,
                write_access: false,
            };
        });
        return initialDevices;
    });
    const wsRef = useRef<WebSocket | null>(null);
    const [permissions, setPermissions] = useState<OphydSocketPermissions | null>(null);
    const [authError, setAuthError] = useState<string | null>(null);
    const hasRenderedOnlyOnce = useRef(false);

    /**
     * Toggles the lock state of a device between locked and unlocked.
     * When locked, the device UI will typically be disabled or read-only.
     * @param deviceName - The name/identifier of the device to toggle
     */
    const toggleDeviceLock = useCallback((deviceName: string) => {
        setDevices((prevDevices) => ({
            ...prevDevices,
            [deviceName]: {
                ...prevDevices[deviceName],
                locked: !prevDevices[deviceName].locked,
            },
        }));
    }, []);

    /**
     * Sends a device value update request through the WebSocket connection.
     * @param deviceName - The name/identifier of the device to update
     * @param value - The new value to set for the device (string, number, or boolean)
     */
    const handleSetValueRequest = useCallback(
        (deviceName: string, value: string | number | boolean) => {
            if (wsRef.current) {
                const setValueMessage = {
                    action: 'set',
                    pv: deviceName,
                    value: value,
                };
                wsRef.current.send(JSON.stringify(setValueMessage));
            }
        },
        [],
    );

    /**
     * Toggles the expanded state of a device in the UI.
     * Used to show/hide additional device details or controls.
     * @param deviceName - The name/identifier of the device to expand/collapse
     */
    const toggleExpand = useCallback((deviceName: string) => {
        setDevices((prevDevices) => ({
            ...prevDevices,
            [deviceName]: {
                ...prevDevices[deviceName],
                expanded: !prevDevices[deviceName].expanded,
            },
        }));
    }, []);

    useEffect(() => {
        if (hasRenderedOnlyOnce.current) {
            //after the initial render, if the deviceNameList changes, reset the entire devices state
            const initialDevices: Devices = {};
            memoizedDeviceNames.forEach((deviceName) => {
                initialDevices[deviceName] = {
                    name: deviceName,
                    value: '',
                    connected: false,
                    locked: false,
                    timestamp: 0,
                    expanded: false,
                    pv: deviceName,
                    read_access: false,
                    write_access: false,
                };
            });
            setDevices(initialDevices);
        } else {
            hasRenderedOnlyOnce.current = true;
        }
    }, [memoizedDeviceNames]);

    // Initialize WebSocket connection
    useEffect(() => {
        // Don't connect if no devices to subscribe to
        if (memoizedDeviceNames.length === 0) {
            // Close existing connection if devices list becomes empty
            if (wsRef.current) {
                wsRef.current.close();
                wsRef.current = null;
            }
            return;
        }

        //console.log('initializing WebSocket connection to', apiUrl);
        const ws = new WebSocket(apiUrl);
        wsRef.current = ws;

        // Per connection, so a reconnect re-authenticates and resubscribes.
        let subscribed = false;
        const subscribe = () => {
            if (subscribed) return;
            subscribed = true;
            ws.send(JSON.stringify({ action: subscribeAction, pvs: memoizedDeviceNames }));
        };

        // Open WebSocket connection. With an API key, authenticate first and
        // subscribe only once the server accepts it (see onmessage).
        ws.onopen = () => {
            if (apiKey) {
                ws.send(JSON.stringify({ action: 'auth', api_key: apiKey }));
            } else {
                subscribe();
            }
        };

        // Handle incoming WebSocket messages
        ws.onmessage = (event) => {
            try {
                const parsed = JSON.parse(event.data);
                if (parsed.action === 'auth') {
                    if (parsed.ok) {
                        setPermissions(parsed.permissions);
                        setAuthError(null);
                        subscribe(); // no-op on a later re-auth ack
                    } else {
                        // The server closes the socket after a failed or revoked auth.
                        setPermissions(null);
                        setAuthError(parsed.error);
                    }
                    return;
                }
                const message:
                    | MessageResponse
                    | ErrorResponse
                    | ValueUpdateResponse
                    | MetaUpdateResponse = parsed;
                if ('sub_type' in message && message.sub_type === 'meta') {
                    //meta updates occur when we first subscribe to a device, or if the connection changes (lost or regained EPICS connection)
                    setDevices((prevDevices) => ({
                        ...prevDevices,
                        [message.pv]: {
                            ...prevDevices[message.pv],
                            ...message,
                            //connected: message.connected,
                            //units: message.units,
                            min: message.lower_ctrl_limit,
                            max: message.upper_ctrl_limit,
                        },
                    }));
                } else if ('pv' in message) {
                    //pv is in the message on regular updates to the device value
                    const deviceName = message.pv;
                    setDevices((prevDevices) => ({
                        ...prevDevices,
                        [deviceName]: {
                            ...prevDevices[deviceName],
                            ...message,
                        },
                    }));
                }
                if ('error' in message) {
                    console.error('WebSocket error message:', message.error);
                }
            } catch (error) {
                console.error('Error parsing WebSocket message:', error);
            }
        };

        // Handle WebSocket errors
        ws.onerror = (error) => {
            console.error('WebSocket error:', error);
        };

        // Handle WebSocket closure
        ws.onclose = () => {
            //console.log('WebSocket connection closed');
        };

        // Cleanup WebSocket connection on unmount
        return () => {
            if (wsRef.current) {
                wsRef.current.close();
                wsRef.current = null;
            }
        };
    }, [wsUrl, memoizedDeviceNames, apiUrl, subscribeAction, apiKey]);

    return {
        devices,
        toggleDeviceLock,
        handleSetValueRequest,
        toggleExpand,
        /** `null` until authenticated (or when no apiKey is given). Hide write controls when `!permissions?.write`; the server enforces it either way. */
        permissions,
        authError,
    };
}
