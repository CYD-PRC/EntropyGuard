"""
EntropyGuard · WebSocket
实时状态同步
"""
from fastapi import WebSocket, WebSocketDisconnect

# 全局连接列表
active_connections: list = []


def setup_websocket(app):
    @app.websocket("/ws/twin")
    async def websocket_twin(websocket: WebSocket):
        from audit import state
        await websocket.accept()
        active_connections.append(websocket)
        await websocket.send_json({
            "type": "init",
            "data": {
                "current_gear": state.current_gear,
                "gear_name": {1: "EMBRACE", 2: "EXPLORE", 3: "ADAPT", 4: "LET_GO"}[state.current_gear],
                "control_entropy": state.control_entropy,
            },
        })
        try:
            while True:
                msg = await websocket.receive_json()
                if msg.get("type") == "heartbeat":
                    await websocket.send_json({"type": "pong"})
        except WebSocketDisconnect:
            if websocket in active_connections:
                active_connections.remove(websocket)
