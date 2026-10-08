"""最小化的 Ollama 协议兼容服务（仅用于测试）。按 Ollama 的线格式实现
`/api/tags`、`/api/version`、`/api/chat`，让 ChatOllama 真实发起请求并解析流式
响应，验证集成代码而不是用 mock 换掉整层。

    .venv\\Scripts\\python.exe tests\\fake_ollama.py   # 独立运行
    from tests.fake_ollama import start_fake_ollama   # 测试中以线程启动
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---------------------------------------------------------------------------
# 推理链标签同样用 chr() 拼接：直接写字面量会在部分工具链中被改写
# ---------------------------------------------------------------------------
_LT, _GT, _SLASH = chr(60), chr(62), chr(47)
THINK_OPEN = _LT + "think" + _GT
THINK_CLOSE = _LT + _SLASH + "think" + _GT

MODEL_NAME = "deepseek-r1:1.5b"

# 默认回答：先输出一段推理链，再输出带引用的正文。
# 推理链刻意在标签中间切开，用来验证流式解析器的缓冲逻辑。
DEFAULT_PIECES = [
    THINK_OPEN[:3],
    THINK_OPEN[3:] + "用户问的是住宿报销标准，",
    "我应该先定位差旅费用条款。",
    THINK_CLOSE[:4],
    THINK_CLOSE[4:],
    "根据文档，住宿费一线城市每晚 600 元，其他城市每晚 400 元 [1]。",
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # 让测试可以按需替换回答内容
    pieces = DEFAULT_PIECES
    model = MODEL_NAME
    fail_with = None  # 设为整数则返回该状态码，用于测试错误分支
    # rag.py 先查 /api/show 的 capabilities，确认支持 thinking 才开原生思维链通道
    capabilities = ["completion", "tools", "thinking"]
    # 记录最近一次 /api/generate 的请求体：用于断言「预热」与「退出卸载」
    last_generate_payload = None

    def log_message(self, *args):  # noqa: ANN002 - 屏蔽默认 stderr 噪音
        pass

    # ------------------------------------------------------------------
    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/api/tags"):
            self._json({
                "models": [
                    {
                        "name": self.model,
                        "model": self.model,
                        "modified_at": _now(),
                        "size": 1_100_000_000,
                        "digest": "0" * 64,
                        "details": {
                            "parent_model": "",
                            "format": "gguf",
                            "family": "qwen2",
                            "families": ["qwen2"],
                            "parameter_size": "1.5B",
                            "quantization_level": "Q4_K_M",
                        },
                    }
                ]
            })
        elif self.path.startswith("/api/version"):
            self._json({"version": "0.0.0-fake"})
        else:
            self._json({"error": "not found"}, status=404)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            payload = {}

        if self.fail_with:
            self._json({"error": "injected failure"}, status=self.fail_with)
            return

        if self.path.startswith("/api/chat"):
            if payload.get("stream", True):
                self._stream_chat()
            else:
                self._once_chat()
        elif self.path.startswith("/api/show"):
            # 只实现 capabilities —— 真实 Ollama 还返回 modelfile / details 等
            self._json({
                "model": self.model,
                "capabilities": list(self.capabilities),
                "details": {"family": "qwen2", "parameter_size": "1.5B"},
            })
        elif self.path.startswith("/api/generate"):
            # 不带 prompt 的预载请求：真实 Ollama 返回 done_reason="load"
            _Handler.last_generate_payload = payload
            self._json({
                "model": self.model,
                "created_at": _now(),
                "response": "",
                "done": True,
                "done_reason": "load" if payload.get("keep_alive") != 0 else "unload",
            })
        else:
            self._json({"error": "not found"}, status=404)

    # ------------------------------------------------------------------
    def _message(self, content: str) -> dict:
        return {"role": "assistant", "content": content}

    def _once_chat(self) -> None:
        content = "".join(self.pieces)
        self._json({
            "model": self.model,
            "created_at": _now(),
            "message": self._message(content),
            "done": True,
            "done_reason": "stop",
            "total_duration": 1_000_000,
            "eval_count": len(content),
            "prompt_eval_count": 64,
        })

    def _stream_chat(self) -> None:
        """按 Ollama 的 NDJSON 流格式逐块输出。"""
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        def write_chunk(payload: bytes) -> None:
            """写一个 HTTP chunk；NDJSON 的换行必须属于载荷本身。"""
            data = payload + b"\n"
            self.wfile.write(f"{len(data):X}\r\n".encode())
            self.wfile.write(data)
            self.wfile.write(b"\r\n")
            self.wfile.flush()

        for piece in self.pieces:
            frame = {
                "model": self.model,
                "created_at": _now(),
                "message": self._message(piece),
                "done": False,
            }
            write_chunk(json.dumps(frame, ensure_ascii=False).encode("utf-8"))
            time.sleep(0.005)  # 稍微拉开，模拟真实流式

        final = {
            "model": self.model,
            "created_at": _now(),
            "message": self._message(""),
            "done": True,
            "done_reason": "stop",
            "total_duration": 2_000_000,
            "load_duration": 100_000,
            "prompt_eval_count": 64,
            "prompt_eval_duration": 500_000,
            "eval_count": 42,
            "eval_duration": 1_400_000,
        }
        write_chunk(json.dumps(final, ensure_ascii=False).encode("utf-8"))
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()


def start_fake_ollama(host: str = "127.0.0.1", port: int = 0):
    """在后台线程启动假 Ollama，返回 ``(server, base_url)``。"""
    server = ThreadingHTTPServer((host, port), _Handler)
    server.daemon_threads = True
    actual_port = server.server_address[1]

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://{host}:{actual_port}"


def main() -> int:
    parser = argparse.ArgumentParser(description="最小 Ollama 协议兼容服务（测试用）")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=11434)
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), _Handler)
    server.daemon_threads = True
    print(f"假 Ollama 已启动：http://{args.host}:{args.port}")
    print(f"  模型：{_Handler.model}")
    print("  Ctrl+C 停止")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
