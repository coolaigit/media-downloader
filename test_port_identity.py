# -*- coding: utf-8 -*-
"""离线验证：实例认领必须校验身份，不能只看 app 标记。

背景（2026-10-22 真实事故）：
开发机在 8850 跑源码版（数据目录是隔离的 _v3_data_auth），用户双击已安装的 V2.5，
V2.5 的 resolve_port 扫 8848-8870，看到 /api/health 有 app=MediaDownloader 就认领成
「我已经在运行」，窗口直接连到源码版 —— 用户看到开发版界面和开发版下载目录，
以为安装好的客户端被污染了。

修复：健康检查带 instance（数据目录+资源目录指纹），认领时必须比对。
这个测试用本地假服务模拟「另一个实例」，验证四种情况的行为。
"""
import http.server
import json
import socketserver
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import ports  # noqa: E402

FAILS = []


def check(cond, msg):
    print(("  [OK ] " if cond else "  [FAIL] ") + msg)
    if not cond:
        FAILS.append(msg)


class FakeHealth(http.server.BaseHTTPRequestHandler):
    payload = {}

    def do_GET(self):
        body = json.dumps(self.payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), FakeHealth)
srv.daemon_threads = True
PORT = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()

me = ports.instance_id()
print("本实例指纹:", me)
print("假服务端口:", PORT)

print("=== 1. 指纹稳定性 ===")
check(bool(me) and len(me) == 12, f"instance_id 是 12 位 hex（{me}）")
check(ports.instance_id() == me, "同一进程内多次调用稳定")

print("=== 2. 别的实例（不同数据目录）不应被认领 ===")
FakeHealth.payload = {"ok": True, "app": "MediaDownloader", "instance": "ffffffffffff"}
check(ports.probe_running(PORT, expect_instance=me) is False,
      "instance 不匹配 → 不认领（这正是事故场景）")
check(ports.probe_running(PORT) is True,
      "不给 expect_instance 时仍按 app 标记判断（兼容旧调用）")

print("=== 3. 老版本没有 instance 字段也不认领 ===")
FakeHealth.payload = {"ok": True, "app": "MediaDownloader"}
check(ports.probe_running(PORT, expect_instance=me) is False,
      "缺 instance 字段 → 不认领（宁可多起一个服务，也不挂错版本）")

print("=== 4. 自己的实例应该被认领 ===")
FakeHealth.payload = {"ok": True, "app": "MediaDownloader", "instance": me}
check(ports.probe_running(PORT, expect_instance=me) is True,
      "instance 匹配 → 认领（同一个安装的第二次启动）")

print("=== 5. 非本工具不认领 ===")
FakeHealth.payload = {"ok": True, "app": "SomeOtherApp", "instance": me}
check(ports.probe_running(PORT, expect_instance=me) is False,
      "app 标记不对 → 不认领")
FakeHealth.payload = {"ok": False, "app": "MediaDownloader", "instance": me}
check(ports.probe_running(PORT, expect_instance=me) is False,
      "ok=false → 不认领")

print("=== 6. 健康检查字段（源码层面）===")
main_src = Path(__file__).resolve().parent / "app" / "main.py"
txt = main_src.read_text(encoding="utf-8")
check('"instance": instance_id()' in txt, "/api/health 返回 instance")
check('"data_root": str(DATA_ROOT)' in txt, "/api/health 返回 data_root（排查时能一眼看出是谁）")

srv.shutdown()

if FAILS:
    print(f"\n[FAIL] {len(FAILS)} 项未通过")
    sys.exit(1)
print("\n实例认领身份校验通过")
