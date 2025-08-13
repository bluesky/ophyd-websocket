import uvicorn
import os
import argparse
import logging
from pathlib import Path
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# Import all the routers
from routers.pv_socket import router as pv_socket_router
from routers.camera_socket import router as camera_router  
from routers.qs_console_socket import router as qs_console_router
from routers.core_api import router as core_api_router

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

def parse_arguments():
    """Parse command line arguments"""
    parser = argparse.ArgumentParser(description="Ophyd as a Service (OAS) - FastAPI Server")
    parser.add_argument(
        "--startup-dir",
        type=str,
        help="Startup directory path for initialization files",
        default=None
    )
    return parser.parse_args()

def log_environment_and_startup_info(startup_dir=None):
    """Log all relevant environment variables and startup information"""
    
    # Collect all OAS and related environment variables with proper defaults
    env_vars = {}
    
    # OAS specific variables with actual defaults
    env_vars["OAS_PORT"] = os.getenv("OAS_PORT", "8001")
    env_vars["OAS_HOST"] = os.getenv("OAS_HOST", "localhost")
    env_vars["OAS_REQUIRE_QSERVER"] = os.getenv("OAS_REQUIRE_QSERVER", "true")
    
    # Queue Server variables with defaults
    env_vars["QSERVER_HTTP_SERVER_HOST"] = os.getenv("QSERVER_HTTP_SERVER_HOST", "localhost")
    env_vars["QSERVER_HTTP_SERVER_PORT"] = os.getenv("QSERVER_HTTP_SERVER_PORT", "60610")
    env_vars["QSERVER_HTTP_SERVER_SINGLE_USER_API_KEY"] = os.getenv("QSERVER_HTTP_SERVER_SINGLE_USER_API_KEY", "test")
    
    # EPICS related variables (these typically don't have defaults)
    epics_vars = ["EPICS_CA_ADDR_LIST", "EPICS_CA_AUTO_ADDR_LIST", "EPICS_CA_MAX_ARRAY_BYTES"]
    for var in epics_vars:
        env_vars[var] = os.getenv(var, "Not set")
    
    # Define variable groups for organized display
    oas_vars = ["OAS_PORT", "OAS_HOST", "OAS_REQUIRE_QSERVER"]
    qserver_vars = ["QSERVER_HTTP_SERVER_HOST", "QSERVER_HTTP_SERVER_PORT", "QSERVER_HTTP_SERVER_SINGLE_USER_API_KEY"]
    
    # Build startup information message
    startup_info = [
        "="*80,
        "OPHYD AS A SERVICE (OAS) - STARTUP INFORMATION",
        "="*80,
        f"Server Host: {env_vars['OAS_HOST']}",
        f"Server Port: {env_vars['OAS_PORT']}",
        "",
        "ENVIRONMENT VARIABLES:",
        "-" * 40,
    ]
    
    # Add OAS variables
    startup_info.append("OAS Configuration:")
    for var in oas_vars:
        startup_info.append(f"  {var}: {env_vars[var]}")
    
    startup_info.append("")
    startup_info.append("Queue Server Configuration:")
    for var in qserver_vars:
        startup_info.append(f"  {var}: {env_vars[var]}")
    
    startup_info.append("")
    startup_info.append("EPICS Configuration:")
    for var in epics_vars:
        startup_info.append(f"  {var}: {env_vars[var]}")
    
    # Add startup directory information
    if startup_dir:
        startup_info.extend([
            "",
            "STARTUP DIRECTORY:",
            "-" * 40,
            f"Startup Directory: {startup_dir}",
        ])
        
        # Check if directory exists and list contents
        startup_path = Path(startup_dir)
        if startup_path.exists() and startup_path.is_dir():
            startup_info.append(f"Directory Status: EXISTS")
            try:
                files = list(startup_path.glob("*"))
                if files:
                    startup_info.append("Directory Contents:")
                    for file in sorted(files):
                        startup_info.append(f"  - {file.name}")
                else:
                    startup_info.append("Directory is empty")
            except PermissionError:
                startup_info.append("Directory Status: Permission denied")
        else:
            startup_info.append(f"Directory Status: NOT FOUND")
    else:
        startup_info.extend([
            "",
            "STARTUP DIRECTORY:",
            "-" * 40,
            "Startup Directory: Not specified (use --startup-dir flag)",
        ])
    
    startup_info.extend([
        "",
        "SERVER ENDPOINTS:",
        "-" * 40,
        f"API Documentation: http://{env_vars['OAS_HOST']}:{env_vars['OAS_PORT']}/docs",
        f"WebSocket Info: http://{env_vars['OAS_HOST']}:{env_vars['OAS_PORT']}/websockets",
        f"Root Endpoint: http://{env_vars['OAS_HOST']}:{env_vars['OAS_PORT']}/",
        "="*80
    ])
    
    # Log everything as one big statement
    logger.info("\n".join(startup_info))

# Create the main FastAPI application
app = FastAPI(
    title="Ophyd as a Service",
    description="Unified FastAPI server for Ophyd device control, area detector array streaming, and more.",
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
app.include_router(core_api_router, tags=["Ophyd Device Management"])

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
    # Parse command line arguments
    args = parse_arguments()
    
    # Get configuration from environment variables
    port = int(OAS_PORT)
    host = os.getenv("OAS_HOST", "0.0.0.0")  # Use different default for server binding
    
    # Log comprehensive startup information
    log_environment_and_startup_info(args.startup_dir)
    
    uvicorn.run("server:app", host=host, port=port, reload=True)