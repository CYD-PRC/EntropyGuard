#!/usr/bin/env python3
"""
Entropy Runtime · MessageBoard 集成脚本
在服务器端原地修改 main.py，接入 MessageBoard 模块
"""
import os
import sys
import re

ROOT = "/root/EntropyGuard"
MAIN = os.path.join(ROOT, "main.py")

print("=" * 60)
print("Entropy Runtime · MessageBoard 集成")
print("=" * 60)

# ========== Step 1: 读 main.py ==========
with open(MAIN, "r", encoding="utf-8") as f:
    content = f.read()

original = content

# ========== Step 2: 在 import 区添加 MessageBoard 入口 ==========
# 在 "from config import Config" 之后插入
import_insert = """
from messageboard import MessageBoard, get_messageboard  # Multi-Agent MessageBoard
from routes.messageboard_api import router as mb_router, set_board, setup_messageboard_ws
"""
content = content.replace(
    "from config import Config",
    "from config import Config" + import_insert,
    1,
)

# ========== Step 3: 在路由注册区添加 MessageBoard 路由 ==========
# 在 "app.include_router(api_router)" 之后插入
content = content.replace(
    "app.include_router(api_router)\nsetup_websocket(app)",
    "app.include_router(api_router)\napp.include_router(mb_router)\nsetup_websocket(app)\nsetup_messageboard_ws(app)",
    1,
)

# ========== Step 4: 添加 startup 事件 ==========
# 在 "from fastapi.staticfiles import StaticFiles" 之后、app.mount 之前插入 startup handler
startup_handler = """
import logging as _logging
_logger = _logging.getLogger("entropyruntime")

@app.on_event("startup")
async def init_messageboard():
    \"\"\"初始化 MessageBoard，自动从 storage_path 加载持久化数据\"\"\"
    try:
        data_file = os.path.join(os.path.dirname(__file__), "messageboard_data.json")
        board = get_messageboard(storage_path=data_file)
        set_board(board)
        _logger.info("[MessageBoard] Initialized (storage: %s, messages: %d)",
                     data_file, len(board.messages))
    except Exception as e:
        _logger.error("[MessageBoard] Init error: %s", e)

"""
content = content.replace(
    "from fastapi.staticfiles import StaticFiles\napp.mount",
    "from fastapi.staticfiles import StaticFiles\n" + startup_handler + "\napp.mount",
    1,
)

# ========== Step 5: 验证并保存 ==========
if content == original:
    print("❌ 未检测到任何变更，main.py 可能已更新过或格式不匹配")
    sys.exit(1)

# 备份
backup_path = MAIN + ".bak." + __import__("time").strftime("%Y%m%d_%H%M%S")
with open(backup_path, "w", encoding="utf-8") as f:
    f.write(original)
print(f"✅ 备份已保存: {backup_path}")

with open(MAIN, "w", encoding="utf-8") as f:
    f.write(content)
print(f"✅ main.py 已更新")

# ========== Step 6: 更新 messageboard.py 持久化路径 ==========
MB_FILE = os.path.join(ROOT, "messageboard.py")
with open(MB_FILE, "r", encoding="utf-8") as f:
    mb_content = f.read()

# 确保 persist 和 load 方法接受文件路径参数（已有，不需要改）
# 但需要确保 data_dir 默认值指向 Entropy Runtime 目录
if 'data_dir="/tmp"' in mb_content or 'data_dir = "/tmp"' in mb_content:
    mb_content = mb_content.replace(
        'data_dir="/tmp"',
        f'data_dir="{ROOT}"',
    )
    mb_content = mb_content.replace(
        "data_dir = '/tmp'",
        f"data_dir = '{ROOT}'",
    )
    with open(MB_FILE, "w", encoding="utf-8") as f:
        f.write(mb_content)
    print("✅ messageboard.py 持久化路径已修正")

print("\n" + "=" * 60)
print("✅ 集成完成！")
print("=" * 60)

# ========== Step 7: 自检 ==========
print("\n🔍 自检...")
# 检查 messageboard.py 能否导入
sys.path.insert(0, ROOT)
try:
    from messageboard import MessageBoard, get_messageboard
    m = get_messageboard()
    print(f"  ✅ messageboard 导入 OK (MessageBoard 实例: {type(m).__name__})")
except Exception as e:
    print(f"  ❌ messageboard 导入失败: {e}")

# 检查 routes/messageboard_api.py 能否导入
try:
    from routes.messageboard_api import router
    print(f"  ✅ routes/messageboard_api 导入 OK (router prefix: {router.prefix})")
except Exception as e:
    print(f"  ❌ routes/messageboard_api 导入失败: {e}")

# 语法检查 main.py
try:
    import py_compile
    py_compile.compile(MAIN, doraise=True)
    print(f"  ✅ main.py 语法检查通过")
except py_compile.PyCompileError as e:
    print(f"  ❌ main.py 语法错误: {e}")
