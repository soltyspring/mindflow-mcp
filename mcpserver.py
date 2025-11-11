#!/usr/bin/env python
from mcp.server.fastmcp import FastMCP
from anthropic import Anthropic
import os, re, json, time, requests
from dotenv import load_dotenv
import warnings, asyncio
from concurrent.futures import ThreadPoolExecutor
from openai import OpenAI
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy import text

from typing import List, Dict, Any

warnings.filterwarnings("ignore", category=ResourceWarning)

DATABASE_URL = os.getenv("DATABASE_URL", "mysql+pymysql://root:a3497@localhost:3306/mindflow")
engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(bind=engine)

# 윈도우 비동기 루프
if hasattr(asyncio, "WindowsProactorEventLoopPolicy"):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# 환경 변수 로드
load_dotenv()

# Claude 클라이언트
client = Anthropic(api_key=os.getenv("CLAUDE_API_KEY"))
openai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
mcp = FastMCP("mindflow-mcp")

# Notion 설정
NOTION_TOKEN = os.getenv("NOTION_TOKEN")
NOTION_DATABASE_ID = os.getenv("NOTION_DATABASE_ID")
NOTION_VERSION = "2022-06-28"
NOTION_API_BASE = "https://api.notion.com/v1"

# ------------------------------------------------
# Markdown → Notion Blocks
# ------------------------------------------------
def parse_rich_text(line: str):
    """**bold**, `code` 지원"""
    segments = []
    for match in re.finditer(r"(\*\*.+?\*\*|`.+?`)", line):
        token = match.group(0)
        if token.startswith("**"):
            segments.append({"type": "text", "text": {"content": token[2:-2]}, "annotations": {"bold": True}})
        elif token.startswith("`"):
            segments.append({"type": "text", "text": {"content": token[1:-1]}, "annotations": {"code": True}})
    if not segments:
        return [{"type": "text", "text": {"content": line}}]
    return segments

def _md_line_to_block(line: str):
    line = line.rstrip("\n")
    if line.startswith("### "):
        return {"type": "heading_3", "heading_3": {"rich_text": parse_rich_text(line[4:])}}
    if line.startswith("## "):
        return {"type": "heading_2", "heading_2": {"rich_text": parse_rich_text(line[3:])}}
    if line.startswith("# "):
        return {"type": "heading_1", "heading_1": {"rich_text": parse_rich_text(line[2:])}}
    if re.match(r"^\s*[-*]\s+", line):
        return {"type": "bulleted_list_item", "bulleted_list_item": {"rich_text": parse_rich_text(re.sub(r'^\s*[-*]\s+', '', line))}}
    return {"type": "paragraph", "paragraph": {"rich_text": parse_rich_text(line if line else " ")}}

def markdown_to_blocks(md: str):
    return [_md_line_to_block(ln) for ln in md.splitlines()]

# ------------------------------------------------
# 일정 추출
# ------------------------------------------------
@mcp.tool()
def parse_schedule_tool(text: str) -> dict:
    """
    Claude가 본문에서 'task'와 'due'를 추출 (동기 호출; 이벤트 루프 건드리지 않음)
    """
    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            temperature=0,
            messages=[{
                "role": "user",
                "content": (
                    "너는 학생의 학습노트를 분석해서 **의미 있는 제목과 마감일**을 찾아내는 도우미야.\n"
                    "아래 내용을 모두 읽고, 문맥을 이해해서 다음 JSON 형식으로만 출력해.\n\n"
                    "{\n"
                    "  \"task\": \"내용의 핵심을 반영한 구체적 제목 (예: '자바 GUI 이벤트 처리와 리스너 구조 정리')\",\n"
                    "  \"due\": \"과제 마감일 (YYYY-MM-DD 형식, 없으면 null)\"\n"
                    "}\n\n"
                    "⚠️ 반드시 JSON만 출력하고, 코드블록(```)이나 설명을 포함하지 말아라.\n"
                    "⚠️ 제목은 반드시 본문 내용을 요약한 형태로 작성하라. 단순히 '자바 과제' 같은 표현은 금지.\n\n"
                    f"{text}"
                )
            }]
        )

        raw = response.content[0].text.strip()
        raw = re.sub(r"^```[a-zA-Z]*", "", raw).strip("` \n")

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            m = re.search(r"\{.*\}", raw, re.DOTALL)
            data = json.loads(m.group(0)) if m else {"task": "학습 요약", "due": None}

        if not data.get("task"):
            data["task"] = "학습 요약"
        if "due" not in data:
            data["due"] = None
        return data

    except Exception as e:
        return {"error": str(e)}


# ------------------------------------------------
# 요약 도구
# ------------------------------------------------
@mcp.tool()
def summarize_tool(content: str, max_tokens: int = 1200) -> str:
    async def _run():
        def _sync_call():
            return client.messages.create(
                model="claude-haiku-4-5-20251001",   # ⚡ 빠른 모델
                max_tokens=max_tokens,
                temperature=0.7,
                messages=[
                    {
                        "role": "user",
                        "content": (
                            "너는 전문가 교수님이야 이 텍스트를 키워드가 들어있다 생각해서 잘 풀어서 설명해 글자수가 길어도돼.\n"
                            "이 내용을 바탕으로 **복습용으로 충분히 이해할 수 있도록** 자세하게 적고 확장한 개념도 작성해여 정리해줘.\n"
                            "단순 요약이 아니라 누락된 개념은 채워넣고, 관련된 정의·예시·배경지식을 덧붙여.\n"
                            "Markdown 형식을 많이 활용해서 구성해줘.\n"
                            "```java``` 위 형식의 코드를 사용할땐 항상 ``` 3개씩 사용해야해 명심해.\n"
                            f"{content}"
                        )
                    }
                ]
            )
        # Claude API를 별도 스레드에서 실행 → 병렬 안전
        return await asyncio.to_thread(_sync_call)

    try:
        loop = asyncio.get_running_loop()
        result = asyncio.run_coroutine_threadsafe(_run(), loop).result()

        text = result.content[0].text.strip()
        text = text.replace("``` ```", "```").strip()
        return text or "⚠️ Claude 응답이 비어 있습니다."

    except Exception as e:
        return f"Error: {str(e)}"

# ------------------------------------------------
def _chunk_blocks(blocks: List[Dict[str, Any]], size: int = 90):
    # 여유를 두고 90개씩 잘라 보냅니다. (최대 100 제한 회피)
    for i in range(0, len(blocks), size):
        yield blocks[i:i+size]

def _append_children(page_id: str, children_chunk: List[Dict[str, Any]], token: str):
    headers = {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }
    # Notion: Append block children → PATCH /blocks/{block_id}/children
    url = f"{NOTION_API_BASE}/blocks/{page_id}/children"
    resp = requests.patch(url, headers=headers, json={"children": children_chunk}, timeout=30)
    return resp

# ------------------------------------------------
# Notion 저장
# ------------------------------------------------

def notion_create_page_with_db_id(
    title: str,
    blocks: list,
    extra_props: dict | None,
    token: str,
    database_id: str,
):
    if not token:
        return {"status": "error", "message": "NOTION_TOKEN missing"}
    if not database_id:
        return {"status": "error", "message": "NOTION_DATABASE_ID missing"}

    headers = {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }

    props = {"Name": {"title": [{"text": {"content": title}}]}}
    if extra_props:
        # 한글 키 → 영문 스키마 매핑(기존 로직 유지)
        if "과제 마감일" in extra_props and "Date" not in props:
            props["Date"] = extra_props["과제 마감일"]
        if "다중 선택" in extra_props and "Tags" not in props:
            props["Tags"] = extra_props["다중 선택"]
        for k in ("Date", "Tags", "Status", "Source"):
            if k in extra_props:
                props[k] = extra_props[k]

    # 1) 첫 청크(<=100개 이하)만 포함하여 페이지 생성
    first_children = list(_chunk_blocks(blocks, 90))[0] if blocks else []
    payload = {
        "parent": {"database_id": database_id},
        "properties": props,
        "children": first_children,
    }

    create_resp = requests.post(f"{NOTION_API_BASE}/pages", headers=headers, json=payload, timeout=30)
    if not (200 <= create_resp.status_code < 300):
        return {"status": "error", "message": create_resp.text}

    data = create_resp.json()
    page_id = data.get("id")
    page_url = data.get("url")

    # 2) 남은 블록들 나눠서 append
    if blocks and len(blocks) > len(first_children):
        # 이미 보낸 첫 덩어리를 제외한 나머지
        remaining = blocks[len(first_children):]

        for chunk in _chunk_blocks(remaining, 90):
            # Rate limit 완화(권장): 3 rps 이하 → 약간의 sleep
            resp = _append_children(page_id, chunk, token)
            if not (200 <= resp.status_code < 300):
                # 부분 실패 시 어디까지 들어갔는지 알려주기
                return {
                    "status": "partial_error",
                    "message": resp.text,
                    "url": page_url,
                    "page_id": page_id,
                }
            time.sleep(0.35)  # 안전한 간격

    return {"status": "ok", "url": page_url, "id": page_id}
# DB에서 사용자 토큰 조회
def get_user_token(user_email: str):
    session = SessionLocal()
    try:
        row = session.execute(
            text(
                "SELECT notion_token FROM notion_tokens "
                "JOIN google_users ON notion_tokens.google_user_id = google_users.id "
                "WHERE google_users.email = :email LIMIT 1"
            ),
            {"email": user_email}
        ).fetchone()
        if row:
            return row[0]
    finally:
        session.close()
    return None

# notion_tool 시그니처 확장
@mcp.tool()
def notion_tool(
    content: str,
    title: str = "학습 요약",
    date: str | None = None,
    tags: list[str] | None = None,
    user_email: str | None = None,
    notion_database_id: str | None = None,   # 이미 있으면 유지
    user_token_plain: str | None = None,     # ✅ 추가
) -> dict:
    # 1) 서버에서 넘겨준 평문 토큰이 최우선
    user_token = user_token_plain
    # 2) 없으면 기존 로직
    if not user_token:
        user_token = get_user_token(user_email) if user_email else NOTION_TOKEN
    if not user_token:
        return {"status": "error", "message": "NOTION_TOKEN missing"}

    blocks = markdown_to_blocks(content)
    extra = {}
    if date:
        extra["과제 마감일"] = {"date": {"start": date}}
    if tags:
        extra["다중 선택"] = {"multi_select": [{"name": t} for t in tags]}

    # DB ID 결정
    db_id = notion_database_id or NOTION_DATABASE_ID
    if not db_id:
        return {"status": "error", "message": "NOTION_DATABASE_ID missing"}

    # (헬퍼가 있다면 그걸 사용)
    return notion_create_page_with_db_id(title, blocks, extra, token=user_token, database_id=db_id)


# helper로 분리(가독성)
"""
def notion_create_page_with_db_id(title: str, blocks: list, extra_props: dict | None, token: str, database_id: str):
    headers = {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }
    props = {"Name": {"title": [{"text": {"content": title}}]}}
    if extra_props:
        props.update(extra_props)

    payload = {
        "parent": {"database_id": database_id},
        "properties": props,
        "children": blocks
    }

    print(f"[Notion] token_head={token[:6]} len={len(token)} db={database_id[:8]}...", flush=True)

    resp = requests.post(f"{NOTION_API_BASE}/pages", headers=headers, json=payload, timeout=30)
    if 200 <= resp.status_code < 300:
        data = resp.json()
        return {"status": "ok", "url": data.get("url"), "id": data.get("id")}
    else:
        return {"status": "error", "message": resp.text}
"""

@mcp.tool()
def compose_dual_tool(text: str) -> dict:
    """
    GPT → task/due 추출 (JSON)
    Claude → 복습용 Markdown 요약
    내부에서 병렬 실행하여 지연시간 단축. 최종 {task, due, content_md} 반환.
    """
    import re, json as _json

    def call_gpt_parse(t: str) -> dict:
    # 본문 그대로 넘겨서 title/task/due 추출 (제목: 내용 관련, 한글 10자 이내)
        sysmsg = (
            "You are a precise extractor. Output STRICT JSON ONLY with keys:\n"
            "title (Korean, <=10 chars, must include a salient term from the text),\n"
            "task  (specific, meaningful task description in Korean),\n"
            "due   (YYYY-MM-DD or null). No code fences or extra text."
        )
        usermsg = (
            "Read the following text and output JSON ONLY in this exact shape:\n"
            "{\n"
            '  "title": "본문의 핵심 용어를 포함한 한국어 제목 (<= 10자)",\n'
            '  "task": "구체적 작업 설명 (한국어)",\n'
            '  "due": "YYYY-MM-DD" or null\n'
            "}\n\n"
            "Rules for title:\n"
            "- pick ONE salient domain term (e.g., key concept/class/API/proper noun) FROM THE TEXT and include it\n"
            "- max length 10 chars in Korean\n"
            "- avoid generic words like '과제', '요약' alone\n\n"
            f"{t}"
        )
        r = openai_client.responses.create(
            model="gpt-4o-mini",
            input=[{"role":"system","content":sysmsg},{"role":"user","content":usermsg}],
            temperature=0, max_output_tokens=200,
        )

        import re, json as _json
        raw = getattr(r, "output_text", "") or ""
        raw = re.sub(r"^```[a-zA-Z]*", "", raw).strip("` \n")

        try:
            data = _json.loads(raw)
        except _json.JSONDecodeError:
            m = re.search(r"\{.*\}", raw, re.DOTALL)
            data = _json.loads(m.group(0)) if m else {}

        # --- 10자 강제 ---
        title = (data.get("title") or data.get("task") or "학습요약").strip().strip('"').strip("'")
        title = re.sub(r"\s+", " ", title)[:10] or "학습요약"

        task  = (data.get("task") or title or "학습요약").strip()
        due   = data.get("due") or None
        if isinstance(due, str) and not due.strip():
            due = None

        return {"title": title, "task": task, "due": due}

    def call_claude_summarize(t: str) -> str:
        prompt = (
            "너는 교수다. 아래 텍스트를 복습용 **Markdown**으로 자세히 정리해라.\n"
            "- 헤더(##, ###), 목록, 표, **강조**, 코드블록 적극 사용\n"
            "- 단순 요약이 아니라: 개념 설명 + 예시 + 오해 포인트 + Best Practices 추가\n"
            "- 마지막에 '🧠 핵심 요약' 섹션 포함\n\n"
            f"{t}"
        )
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1600,
            temperature=0.4,
            messages=[{"role": "user", "content": prompt}],
        )
        text = resp.content[0].text.strip()
        # 코드블록 꼬임 방지
        text = text.replace("``` ```", "```").strip()
        return text or "# 요약\n(내용이 비어 있습니다)"

    # ── 병렬 실행 (툴 하나 내부에서 스레드로 병렬) ──
    with ThreadPoolExecutor(max_workers=2) as ex:
        fut_parse = ex.submit(call_gpt_parse, text)
        fut_sum   = ex.submit(call_claude_summarize, text)

        try:
            parsed = fut_parse.result(timeout=90)
        except Exception:
            parsed = {"title": "학습요약", "task": "학습 요약", "due": None}  # ← title 포함

        try:
            content_md = fut_sum.result(timeout=180)
        except Exception:
            content_md = "# 요약\n(요약 생성에 실패했습니다)"

    return {
        "title": parsed.get("title") or "학습요약",
        "task": parsed.get("task", "학습 요약"),
        "due": parsed.get("due"),
        "content_md": content_md,
    }


# ------------------------------------------------
# 서버 실행
# ------------------------------------------------
if __name__ == "__main__":
    import sys, traceback
    print("🚀 MCP 서버 시작", file=sys.stderr, flush=True)
    try:
        mcp.run()  # 절대 stdout에 다른 print 하지 말 것
    except KeyboardInterrupt:
        print("🟥 MCP 서버 수동 종료", file=sys.stderr, flush=True)
    except Exception as e:
        print(f"❌ MCP 서버 예외: {e}", file=sys.stderr, flush=True)
        traceback.print_exc()
