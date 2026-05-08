"""
Queue Server safety checks and utilities for preventing device conflicts during experiments

This module provides safety mechanisms to prevent device operations while the Bluesky
Queue Server is running experiments, avoiding potential conflicts and data corruption.
"""
import os
import logging
import functools
import httpx
from fastapi import HTTPException

logger = logging.getLogger(__name__)

# Queue Server configuration from environment variables
QSERVER_HOST = os.getenv("QSERVER_HTTP_SERVER_HOST", "localhost")
QSERVER_PORT = os.getenv("QSERVER_HTTP_SERVER_PORT", "60610")
QSERVER_API_KEY = os.getenv("QSERVER_HTTP_SERVER_SINGLE_USER_API_KEY", "test")
QSERVER_BASE_URL = f"http://{QSERVER_HOST}:{QSERVER_PORT}"

# Safety configuration - default to not requiring queue server for safety since socket may be ran without a qserver
OAS_REQUIRE_QSERVER = os.getenv("OAS_REQUIRE_QSERVER", "false").lower() in ("true", "1", "yes", "on")

async def get_queue_server_status() -> dict:
    """
    Get the current status from the queue server using async HTTP client
    
    Returns:
        dict: Queue server status response
        
    Raises:
        HTTPException: If unable to connect or authenticate with queue server
    """
    url = f"{QSERVER_BASE_URL}/api/status"
    
    # Set up headers with API key authentication
    headers = {
        "Authorization": f"Apikey {QSERVER_API_KEY}",
        "Accept": "application/json"
    }
    
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(url, headers=headers)
            
            if response.status_code == 200:
                return response.json()
            elif response.status_code == 401:
                raise HTTPException(
                    status_code=401,
                    detail="Authentication failed - check QSERVER_HTTP_SERVER_SINGLE_USER_API_KEY"
                )
            elif response.status_code == 403:
                raise HTTPException(
                    status_code=403,
                    detail="Access forbidden - API key may not have sufficient permissions"
                )
            else:
                raise HTTPException(
                    status_code=response.status_code,
                    detail=f"Queue server returned status {response.status_code}: {response.text}"
                )
                
    except httpx.ConnectError as e:
        raise HTTPException(
            status_code=503,
            detail=f"Could not connect to queue server at {QSERVER_BASE_URL}: Connection refused"
        )
    except httpx.TimeoutException as e:
        raise HTTPException(
            status_code=504,
            detail=f"Timeout connecting to queue server at {QSERVER_BASE_URL}"
        )
    except httpx.RequestError as e:
        raise HTTPException(
            status_code=503,
            detail=f"Network error connecting to queue server: {str(e)}"
        )
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Unexpected error connecting to queue server: {str(e)}"
        )


async def check_queue_server_safety() -> bool:
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
        
        logger.info(f"Queue server safety check passed: manager_state='{manager_state}', running_item_uid={running_item_uid}")
        return True
        
    except HTTPException as http_exc:
        # Check if this is a connection error and if we're in permissive mode
        if not OAS_REQUIRE_QSERVER and http_exc.status_code in (503, 504):
            # In permissive mode, allow operations if queue server is unreachable
            logger.warning(f"Queue server unreachable at {QSERVER_BASE_URL}, but OAS_REQUIRE_QSERVER=false - allowing device operation")
            return True
        
        # Re-raise HTTPExceptions (our custom errors or authentication issues)
        raise
    except Exception as e:
        if not OAS_REQUIRE_QSERVER:
            # In permissive mode, allow operations on unexpected errors (likely connection issues)
            logger.warning(f"Unexpected error checking queue server safety, but OAS_REQUIRE_QSERVER=false - allowing device operation: {str(e)}")
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
    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        await check_queue_server_safety()
        return await func(*args, **kwargs)
    return wrapper
