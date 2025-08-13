import uvicorn
import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# Import all the routers
from routers.pv_socket import router as pv_socket_router
from routers.camera_socket import router as camera_router  
from routers.qs_console_socket import router as qs_console_router
from routers.core_api import router as core_api_router

# Create the main FastAPI application
app = FastAPI(
    title="Ophyd as a Service",
    description="Unified FastAPI server for EPICS PV control, Ophyd device control, area detector array streaming, and more.",
    version="1.0.0"
)

# Get configuration from environment variables once
OAS_PORT = os.getenv("OAS_PORT", "8001")
OAS_HOST = os.getenv("OAS_HOST", "localhost")
BASE_WS_URL = f"ws://{OAS_HOST}:{OAS_PORT}"
BASE_HTTP_URL = f"http://{OAS_HOST}:{OAS_PORT}"

# Configure CORS
origins = [
    "http://localhost.tiangolo.com",
    "https://localhost.tiangolo.com", 
    "http://localhost",
    "http://localhost:8080",
    "http://localhost:3000",
    "http://localhost:5173",
    "*"
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include all routers with appropriate prefixes and tags
app.include_router(pv_socket_router, prefix="/api/v1", tags=["PV WebSocket"])
app.include_router(camera_router, prefix="/api/v1", tags=["Camera Streaming"])
app.include_router(qs_console_router, prefix="/api/v1", tags=["Queue Server"])
app.include_router(core_api_router, tags=["Device Management"])

# WebSocket info endpoint
@app.get("/websockets", tags=["WebSocket Info"])
def list_websockets():
    """List all available WebSocket endpoints with connection details"""
    return {
        "websockets": {
            "pv_monitor": {
                "endpoint": "/api/v1/ophydSocket",
                "description": "Real-time EPICS PV monitoring and control",
                "example_url": f"{BASE_WS_URL}/api/v1/ophydSocket",
                "actions": ["subscribe", "unsubscribe", "refresh", "subscribeSafely", "subscribeReadOnly", "set"]
            },
            "camera_stream": {
                "endpoint": "/api/v1/pvcamera",
                "description": "Live camera image streaming from area detectors",
                "example_url": f"{BASE_WS_URL}/api/v1/pvcamera",
                "format": "Base64 encoded JPEG images"
            },
            "queue_server": {
                "endpoint": "/api/v1/queue_server",
                "description": "Queue server console communication via ZMQ",
                "example_url": f"{BASE_WS_URL}/api/v1/queue_server",
                "protocol": "ZMQ bridge to WebSocket"
            },
            "device_websocket": {
                "endpoint": "/ws",
                "description": "General device WebSocket for testing",
                "example_url": f"{BASE_WS_URL}/ws",
                "type": "Echo WebSocket"
            }
        },
        "connection_info": {
            "protocol": "WebSocket",
            "base_url": BASE_WS_URL,
            "test_tools": [
                f"Browser console (new WebSocket('{BASE_WS_URL}/api/v1/ophydSocket'))",
                "wscat (npm install -g wscat)",
                "WebSocket test clients",
                "curl --include --no-buffer --header 'Connection: Upgrade' --header 'Upgrade: websocket'"
            ]
        },
        "usage_examples": {
            "pv_subscribe": {
                "action": "subscribe",
                "pv": "IOC:m1",
                "description": "Subscribe to PV updates"
            },
            "pv_set": {
                "action": "set",
                "pv": "IOC:m1",
                "value": 10,
                "timeout": 1,
                "description": "Set PV value"
            }
        }
    }

# Override the root endpoint to provide API information
@app.get("/", tags=["Root"])
def read_root():
    return {
        "message": "Ophyd WebSocket Server",
        "version": "1.0.0",
        "api_docs": f"{BASE_HTTP_URL}/docs",
        "websocket_info": f"{BASE_HTTP_URL}/websockets",
        "endpoints": {
            "websockets": {
                "pv_monitor": f"{BASE_WS_URL}/api/v1/ophydSocket",
                "camera_stream": f"{BASE_WS_URL}/api/v1/pvcamera", 
                "queue_server": f"{BASE_WS_URL}/api/v1/queue_server",
                "device_websocket": f"{BASE_WS_URL}/ws"
            },
            "rest_api": {
                "devices": f"{BASE_HTTP_URL}/devices",
                "websocket_info": f"{BASE_HTTP_URL}/websockets",
                "docs": f"{BASE_HTTP_URL}/docs",
                "openapi": f"{BASE_HTTP_URL}/openapi.json"
            }
        },
        "description": "This server provides WebSocket endpoints for EPICS PV monitoring, camera streaming, queue server communication, and REST API endpoints for device management."
    }

if __name__ == "__main__":
    # Get port from environment variable with OAS prefix, default to 8001
    port = int(OAS_PORT)
    host = os.getenv("OAS_HOST", "0.0.0.0")  # Use different default for server binding
    
    print(f"Starting Ophyd as a Service (OAS) on {host}:{port}")
    print(f"API Documentation: http://{host}:{port}/docs")
    print(f"WebSocket Info: http://{host}:{port}/websockets")
    print(f"Environment variables: OAS_PORT={port}, OAS_HOST={OAS_HOST}")
    
    uvicorn.run("server:app", host=host, port=port, reload=True)