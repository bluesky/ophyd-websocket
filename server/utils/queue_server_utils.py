"""
Queue Server utilities for safety checks and status monitoring
"""
import os
import urllib.request
import urllib.error
import json
from fastapi import HTTPException


# Queue Server configuration from environment variables
QSERVER_HOST = os.getenv("QSERVER_HTTP_SERVER_HOST", "localhost")
QSERVER_PORT = os.getenv("QSERVER_HTTP_SERVER_PORT", "60610")
QSERVER_API_KEY = os.getenv("QSERVER_HTTP_SERVER_SINGLE_USER_API_KEY", "test")
QSERVER_BASE_URL = f"http://{QSERVER_HOST}:{QSERVER_PORT}"

# Safety configuration - default to requiring queue server for safety
OAS_REQUIRE_QSERVER = os.getenv("OAS_REQUIRE_QSERVER", "true").lower() in ("true", "1", "yes", "on")


async def get_queue_server_status():
    """
    Get the current status from the queue server
    
    Returns:
        dict: Queue server status response
        
    Raises:
        HTTPException: If unable to connect or authenticate with queue server
    """
    try:
        url = f"{QSERVER_BASE_URL}/api/status"
        
        # Create request with API key authentication
        request = urllib.request.Request(url)
        request.add_header("Authorization", f"Apikey {QSERVER_API_KEY}")
        
        with urllib.request.urlopen(request, timeout=10) as response:
            if response.status == 200:
                data = json.loads(response.read().decode('utf-8'))
                return data
            else:
                raise HTTPException(
                    status_code=response.status,
                    detail=f"Queue server returned status {response.status}"
                )
    except urllib.error.URLError as e:
        raise HTTPException(
            status_code=503, 
            detail=f"Could not connect to queue server at {QSERVER_BASE_URL}: {str(e)}"
        )
    except urllib.error.HTTPError as e:
        # Handle authentication errors specifically
        if e.code == 401:
            raise HTTPException(
                status_code=401,
                detail=f"Authentication failed - check QSERVER_HTTP_SERVER_SINGLE_USER_API_KEY"
            )
        elif e.code == 403:
            raise HTTPException(
                status_code=403,
                detail=f"Access forbidden - API key may not have sufficient permissions"
            )
        else:
            raise HTTPException(
                status_code=e.code,
                detail=f"Queue server returned HTTP {e.code}: {e.reason}"
            )
    except json.JSONDecodeError as e:
        raise HTTPException(
            status_code=502,
            detail=f"Queue server returned invalid JSON: {str(e)}"
        )
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Unexpected error connecting to queue server: {str(e)}"
        )


async def check_queue_server_safety():
    """
    Check if it's safe to perform device operations (queue is not running)
    
    Environment variable OAS_REQUIRE_QSERVER controls behavior:
    - true (default): Strict mode - requires queue server to be reachable and idle
    - false: Permissive mode - allows operations if queue server is unreachable due to connection issues
    
    Returns:
        bool: True if safe to operate devices
        
    Raises:
        HTTPException: If unable to check queue status or queue is running
    """
    try:
        status = await get_queue_server_status()
        
        # Check if queue is currently running an item
        running_item_uid = status.get("running_item_uid")
        manager_state = status.get("manager_state", "unknown")
        
        if running_item_uid is not None:
            raise HTTPException(
                status_code=423,  # 423 Locked - resource is locked
                detail={
                    "error": "Device operation blocked - Queue server is currently running an experiment",
                    "running_item_uid": running_item_uid,
                    "manager_state": manager_state,
                    "message": "Cannot modify device values while an experiment is running. Wait for the current item to complete."
                }
            )
        
        # Additional safety checks
        if manager_state in ["running", "paused"]:
            raise HTTPException(
                status_code=423,
                detail={
                    "error": f"Device operation blocked - Queue server state is '{manager_state}'",
                    "manager_state": manager_state,
                    "message": "Cannot modify device values while queue server is in a running or paused state."
                }
            )
        
        return True
        
    except HTTPException as http_exc:
        # Check if this is a connection error and if we're in permissive mode
        if not OAS_REQUIRE_QSERVER and http_exc.status_code == 503:
            # In permissive mode, allow operations if queue server is unreachable
            # (connection refused, timeout, etc.)
            print(f"Warning: Queue server unreachable at {QSERVER_BASE_URL}, but OAS_REQUIRE_QSERVER=false - allowing device operation")
            return True
        
        # Re-raise HTTPExceptions (our custom errors or authentication issues)
        raise
    except Exception as e:
        if not OAS_REQUIRE_QSERVER:
            # In permissive mode, allow operations on unexpected errors (likely connection issues)
            print(f"Warning: Unexpected error checking queue server safety, but OAS_REQUIRE_QSERVER=false - allowing device operation: {str(e)}")
            return True
        
        # In strict mode, block on any unexpected errors
        raise HTTPException(
            status_code=500,
            detail=f"Unable to verify queue server safety: {str(e)}"
        )


def queue_safety_required(func):
    """
    Decorator to add queue server safety check to endpoint functions
    
    Usage:
        @queue_safety_required
        async def my_device_endpoint():
            # This will only execute if queue is not running
            pass
    """
    async def wrapper(*args, **kwargs):
        await check_queue_server_safety()
        return await func(*args, **kwargs)
    return wrapper
