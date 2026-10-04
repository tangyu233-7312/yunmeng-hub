"""极简的「假 OpenAI 兼容服务」，用于本地端到端演示与浏览器探针。

==================== 用途 ====================
毕设演示时不想依赖真实 API Key、也不想花掉额度，但又需要看到
**真实的流式打字机效果**（文字逐字出现）与真实的 HTTP/SSE 协议往返。
这个小服务就是为此写的：它实现了 OpenAI 兼容协议的两个端点：

    POST /v1/chat/completions   对话（stream=false 返回 JSON；stream=true 返回 SSE）
    GET  /v1/models             模型列表

它会把回复内容**逐字**吐出来，中间带一点延迟，所以前端看起来
就是模型在"一个字一个字地说话"。

==================== 怎么用？====================
    # 终端 1：启动假服务（端口 8123）
    .\\.venv\\Scripts\\python.exe scripts/fake_openai_server.py

    # 终端 2：启动本项目
    .\\.venv\\Scripts\\python.exe -m uvicorn app.main:app --port 8000

然后在控制台「模型配置」里新建一个：
    Base URL   http://127.0.0.1:8123/v1
    API Key    随便填（留空也行）
    模型名     fake-model
    上下文窗口  8192

★ 它不是测试的一部分：`pytest` 永远不依赖它（测试一律用 httpx.MockTransport）。
"""

from __future__ import annotations

import argparse
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

#: 默认回复（会逐字吐出来）
DEFAULT_REPLY = (
    "【本地假模型】我听见你说话了。\n\n"
    "这段文字是**一个字一个字**推送给前端的，"
    "所以你在对话界面里能看到打字机效果 —— 说明 SSE 流式链路是通的："
    "适配器 → 叙事引擎 → SSE 端点 → 浏览器逐段渲染。"
)

#: 状态块（只有当用户这轮明确提到"状态"时才追加）。
#  ★ 为什么要有它：状态栏这条链路（模型输出块 → 后端剥离+校验+落库 → 前端渲染）
#  以前**一条断言都没有** —— 假模型从来不说状态块，于是"真模型不吐状态块"
#  这种最要命的情况在探针里是隐形的（用户验收时就是这么踩到的）。
#  约定：消息里带"状态"两个字，假模型就扮演一个"守规矩的模型"。
STATE_BLOCK = (
    '\n\n<state>{"hp": {"current": 88, "max": 100}, "inventory": ["提灯", "铜钥匙"], '
    '"location": "灯塔三层 · 灯室", "quests": [{"title": "点亮灯塔", "status": "active"}], '
    '"flags": {"灯油": "半桶"}}</state>'
)

#: 翻译中间件的系统提示词里一定有这个词（见 app/narrative/translate.py）
TRANSLATE_MARKER = "翻译中间件"
#: 假"译文"：与英文原文明显不同，便于断言"界面显示的是译文"
FAKE_TRANSLATION = "【本地假译文】你好，旅行者。前面的路还很长，夜也很冷。"
#: 用户这轮提到它 → 假模型**用英文回复**（用来驱动翻译中间件的输出侧）
ENGLISH_MARKER = "英文回复"
FAKE_ENGLISH_REPLY = (
    "I hear you, traveler. The road ahead is long and the night is cold. "
    "Keep your lantern lit."
)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    delay: float = 0.12

    # ---------------- 路由 ----------------
    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/").endswith("/models"):
            self._json(
                200,
                {
                    "object": "list",
                    "data": [{"id": "fake-model", "object": "model"}, {"id": "fake-reasoner"}],
                },
            )
            return
        self._json(404, {"error": {"message": f"未知路径 {self.path}"}})

    def do_POST(self) -> None:  # noqa: N802
        if not self.path.rstrip("/").endswith("/chat/completions"):
            self._json(404, {"error": {"message": f"未知路径 {self.path}"}})
            return

        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            self._json(400, {"error": {"message": "请求体不是合法 JSON"}})
            return

        messages = body.get("messages") or []

        # ★ 翻译中间件：它的系统提示词里带着"翻译中间件"四个字 → 回一段"译文"。
        #   这样冒烟测试与浏览器探针都能验证"中间件真的调了模型、且结果被存下来"，
        #   而假模型依然是**通用**的（不认请求方是谁，只认提示词里写了什么）。
        system_text = " ".join(
            str(m.get("content") or "") for m in messages if m.get("role") == "system"
        )
        if TRANSLATE_MARKER in system_text:
            self._respond(body, FAKE_TRANSLATION)
            return

        # 从最后一条**真正的**用户消息里取一点线索，让回复看起来"听懂了"
        # （尾注以 user 角色发送，但开头带 [系统指令…]，要跳过它继续往前找）
        reply = DEFAULT_REPLY
        for message in reversed(messages):
            if message.get("role") != "user":
                continue
            text = str(message.get("content") or "")[:40]
            if not text or text.startswith("[系统指令"):
                continue
            if ENGLISH_MARKER in text:
                # ★ 用户要求"英文回复" → 假模型说英文。
                #   这是驱动翻译中间件**输出侧**的唯一办法（回复必须是外语才需要译）。
                reply = FAKE_ENGLISH_REPLY
            else:
                reply = f"【本地假模型】你说的是「{text}」。\n\n" + DEFAULT_REPLY
                # ★ 用户这轮提到"状态"→ 扮演守规矩的模型，末尾追加状态块。
                #   （探针靠这一句验证"状态栏真的会亮起来 + 后端真的剥掉了 JSON"）
                if "状态" in text:
                    reply += STATE_BLOCK
            break

        self._respond(body, reply)

    def _respond(self, body: dict, reply: str) -> None:
        if body.get("stream"):
            self._stream(reply, body.get("model") or "fake-model")
        else:
            self._json(
                200,
                {
                    "id": "chatcmpl-fake",
                    "object": "chat.completion",
                    "model": body.get("model") or "fake-model",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": reply},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 20, "completion_tokens": len(reply), "total_tokens": 20 + len(reply)},
                },
            )

    # ---------------- SSE ----------------
    def _stream(self, reply: str, model: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        def send(payload: dict) -> None:
            self.wfile.write(f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8"))
            self.wfile.flush()

        # 逐字推送。每个字一个包，这正是真实厂商流式返回的形态。
        for char in reply:
            send(
                {
                    "id": "chatcmpl-fake",
                    "object": "chat.completion.chunk",
                    "model": model,
                    "choices": [{"index": 0, "delta": {"content": char}, "finish_reason": None}],
                }
            )
            time.sleep(self.delay)

        send(
            {
                "id": "chatcmpl-fake",
                "object": "chat.completion.chunk",
                "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": 20,
                    "completion_tokens": len(reply),
                    "total_tokens": 20 + len(reply),
                },
            }
        )
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    # ---------------- 工具 ----------------
    def _json(self, status: int, payload: dict) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, fmt: str, *args) -> None:  # 静默默认日志，避免刷屏
        return


def main() -> None:
    parser = argparse.ArgumentParser(description="本地假 OpenAI 兼容服务（仅用于演示流式效果）")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8123)
    parser.add_argument(
        "--delay",
        type=float,
        default=0.12,
        help="每个字符之间的间隔秒数（调大更容易肉眼看出打字机效果）",
    )
    args = parser.parse_args()

    Handler.delay = max(0.0, args.delay)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"假 OpenAI 服务已启动: http://{args.host}:{args.port}/v1  (delay={Handler.delay}s)")
    print("在控制台里把 Base URL 填成上面这个地址、模型名填 fake-model 即可。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
