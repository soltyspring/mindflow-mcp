import asyncio, json, os, sys

def looks_like_json(s: str) -> bool:
    s = s.strip()
    return s.startswith("{") and s.endswith("}")

async def read_json_line(proc):
    while True:
        raw = await proc.stdout.readline()
        if not raw:
            raise RuntimeError("서버 출력이 종료되었습니다.")
        txt = raw.decode(errors="replace").strip()
        if not txt:
            continue
        if looks_like_json(txt):
            try:
                return json.loads(txt)
            except json.JSONDecodeError:
                continue

def extract_markdown(resp: dict) -> str:
    r = resp.get("result", resp) or {}
    if isinstance(r, dict) and isinstance(r.get("summary"), str):
        return r["summary"]
    c = r.get("content")
    if isinstance(c, list) and c:
        first = c[0] if isinstance(c[0], dict) else {}
        for k in ("text", "value", "content"):
            v = first.get(k)
            if isinstance(v, str) and v.strip():
                return v
    d = r.get("data")
    if isinstance(d, dict) and isinstance(d.get("summary"), str):
        return d["summary"]
    if isinstance(r, str) and r.strip():
        return r
    if "error" in resp:
        raise RuntimeError(f"MCP error: {resp['error']}")
    raise KeyError(f"요약 텍스트를 찾지 못했습니다. 전체 응답: {resp}")

async def main():
    server_path = os.path.join(os.path.dirname(__file__), "mcpserver.py")
    if not os.path.exists(server_path):
        raise FileNotFoundError(f"mcpserver.py를 찾을 수 없습니다: {server_path}")

    proc = await asyncio.create_subprocess_exec(
        sys.executable, server_path,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=os.path.dirname(server_path),
        creationflags=0
    )

    try:
        # 초기 handshake
        init_req = {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "clientInfo": {"name": "local-runner", "version": "0.1"},
                "capabilities": {},
                "rootUri": None
            }
        }
        proc.stdin.write((json.dumps(init_req) + "\n").encode()); await proc.stdin.drain()
        _ = await read_json_line(proc)

        initialized_note = {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}
        proc.stdin.write((json.dumps(initialized_note) + "\n").encode()); await proc.stdin.drain()

        # 사용자 입력
        with open("input.txt", "r", encoding="utf-8") as f:
            src = f.read().strip()

        # ① 일정 파싱
        parse_req = {
            "jsonrpc":"2.0","id":2,"method":"tools/call",
            "params":{"name":"parse_schedule_tool","arguments":{"text": src}}
        }
        proc.stdin.write((json.dumps(parse_req)+"\n").encode()); await proc.stdin.drain()
        parse_resp = await read_json_line(proc)

        parsed = {}
        try:
            parsed_raw = parse_resp.get("result", {}).get("content", [])
            if parsed_raw and isinstance(parsed_raw[0], dict):
                text_val = parsed_raw[0].get("text")
                parsed = json.loads(text_val)
        except Exception as e:
            print("❌ 일정 파싱 실패:", e)

        # ② 요약 요청
        sum_req = {
            "jsonrpc": "2.0", "id": 4, "method": "tools/call",
            "params": {
                "name": "summarize_tool",
                "arguments": {"content": src}
            }
        }
        proc.stdin.write((json.dumps(sum_req) + "\n").encode()); await proc.stdin.drain()
        sum_resp = await read_json_line(proc)
        md = extract_markdown(sum_resp)

        # ③ Notion 저장
        notion_req = {
            "jsonrpc": "2.0", "id": 5, "method": "tools/call",
            "params": {
                "name": "notion_tool",
                "arguments": {
                    "content": md,
                    "title": parsed.get("task", "학습 요약"),
                    "date": parsed.get("due"),
                    "tags": ["Summary", "과제"]
                }
            }
        }

        proc.stdin.write((json.dumps(notion_req) + "\n").encode())
        await proc.stdin.drain()
        notion_resp = await read_json_line(proc)
        print("📝 Notion 요약 응답:", notion_resp)

    finally:
        try:
            proc.terminate()
        except Exception:
            pass

if __name__ == "__main__":
    print("✅ MCP 서버 실행 시작")
    asyncio.run(main())
