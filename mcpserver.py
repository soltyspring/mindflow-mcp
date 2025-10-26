#!/usr/bin/env python
from mcp.server.fastmcp import FastMCP
from anthropic import Anthropic
import os, re, json, time, requests
from dotenv import load_dotenv
import warnings, asyncio
warnings.filterwarnings("ignore", category=ResourceWarning)

# 윈도우 비동기 루프
if hasattr(asyncio, "WindowsProactorEventLoopPolicy"):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# 환경 변수 로드
load_dotenv()

# Claude 클라이언트
client = Anthropic(api_key=os.getenv("CLAUDE_API_KEY"))
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
    Claude가 본문 내용을 분석해 의미 있는 제목(task)과 마감일(due)을 추출
    """
    try:
        response = client.messages.create(
            model="claude-3-5-haiku-20241022",
            max_tokens=200,
            messages=[
                {
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
                }
            ],
        )

        parsed = response.content[0].text.strip()
        if parsed.startswith("```"):
            parsed = re.sub(r"^```[a-zA-Z]*", "", parsed).strip("` \n")

        # Claude 응답 검증
        data = json.loads(parsed)
        if not data.get("task"):
            data["task"] = "학습 요약"
        return data

    except Exception as e:
        return {"error": str(e)}

# ------------------------------------------------
# 요약 도구
# ------------------------------------------------
@mcp.tool()
def summarize_tool(content: str, max_tokens: int = 1200) -> str:
    try:
        response = client.messages.create(
            model="claude-3-5-haiku-20241022",
            max_tokens=max_tokens,
            messages=[
                {
                    "role": "user",
                    "content": (
                        "너는 전문가 교수님이야 이 텍스트를 키워드가 들어있다 생각해서 잘 풀어서 설명해 글자수가 길어도돼.\n"
                        "이 내용을 바탕으로 **복습용으로 충분히 이해할 수 있도록** 자세하게 적고 확장한 개념도 작성해여 정리해줘.\n"
                        "단순 요약이 아니라 누락된 개념은 채워넣고, 관련된 정의·예시·배경지식을 덧붙여.\n"
                        "Markdown 형식을 많이 활용해서 구성해줘.\n\n"
                        f"{content}"
                    )
                }
            ]
        )
        return response.content[0].text.strip()
    except Exception as e:
        return f"Error: {str(e)}"

# ------------------------------------------------
# Notion 저장
# ------------------------------------------------
def notion_create_page(title: str, blocks: list, extra_props: dict | None = None):
    if not NOTION_TOKEN:
        return {"status": "error", "message": "NOTION_TOKEN missing"}

    headers = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }

    props = {"이름": {"title": [{"text": {"content": title}}]}}
    if extra_props:
        props.update(extra_props)

    payload = {
        "parent": {"database_id": NOTION_DATABASE_ID},
        "properties": props,
        "children": blocks
    }

    resp = requests.post(f"{NOTION_API_BASE}/pages", headers=headers, json=payload, timeout=30)
    if 200 <= resp.status_code < 300:
        data = resp.json()
        return {"status": "ok", "url": data.get("url"), "id": data.get("id")}
    else:
        return {"status": "error", "message": resp.text}

@mcp.tool()
def notion_tool(content: str, title: str = "학습 요약", date: str | None = None, tags: list[str] | None = None) -> dict:
    blocks = markdown_to_blocks(content)
    extra = {}
    if date:
        extra["과제 마감일"] = {"date": {"start": date}}
    if tags:
        extra["다중 선택"] = {"multi_select": [{"name": t} for t in tags]}
    return notion_create_page(title, blocks, extra)

# ------------------------------------------------
# 서버 실행
# ------------------------------------------------
if __name__ == "__main__":
    mcp.run()
