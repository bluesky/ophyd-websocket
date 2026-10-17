from fastapi import APIRouter, WebSocket, WebSocketDisconnect
import zmq
import asyncio
import logging
import json
import os

from ..auth import authenticate_websocket, is_auth_message

logging.basicConfig(level=logging.INFO)

router = APIRouter()

@router.websocket("/qs-console-socket")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    logging.info("New WebSocket connection")

    # Before connecting to ZMQ: console output needs the console scope.
    session = await authenticate_websocket(websocket, "console")
    if session is None:
        return

    context = zmq.Context()
    socket = context.socket(zmq.SUB)

    # Determine the connection address
    zmq_host = os.getenv("ZMQ_HOST", "localhost")
    zmq_port = os.getenv("ZMQ_PORT", "60625")
    zmq_address = f"tcp://{zmq_host}:{zmq_port}"

    socket.connect(zmq_address)
    socket.setsockopt_string(zmq.SUBSCRIBE, "")
    logging.info(f"Connected to ZMQ service at {zmq_address}...")

    async def zmq_listener():
        while True:
            try:
                message = socket.recv_string(flags=zmq.NOBLOCK) # the no block option will raise an error if there's no message
                if (message != "QS_Console"):
                    logging.info(f"Received message from ZMQ: {message}")
                    await websocket.send_text(message)
            except zmq.Again as e:
                await asyncio.sleep(0.1)

    zmq_task = asyncio.create_task(zmq_listener())
    try:
        async with session.guard(websocket):
            while True:
                data = await websocket.receive_text()
                # Not logged: client messages may carry an API key.
                try:
                    parsed = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if is_auth_message(parsed):
                    await session.handle_auth_message(websocket, parsed)
    except WebSocketDisconnect:
        logging.info("WebSocket connection closed")
    finally:
        zmq_task.cancel()
        socket.close()
        context.term()


