#!/usr/bin/env python
# server.py
import os, re, json, requests, asyncio
from typing import Optional, List
from fastapi import FastAPI, File, UploadFile, Form
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv

PRESENTON_URL = os.getenv("PRESENTON_URL", "http://localhost:5000")
PRESENTON_TIMEOUT = 120

# ── MCP 툴이 들어있는 모듈을 그대로 import ──
# mcpserver.py와 같은 디렉터리에 두면 import가 바로 됩니다.
import mcpserver  # <- compose_dual_tool, notion_tool 사용

load_dotenv()

PPT_SERVICE_URL = os.getenv("PPT_SERVICE_URL")  # 예: http://localhost:5000/generate

app = FastAPI(title="MindFlow Web API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

# ---------- Schemas ----------
class ProcessIn(BaseModel):
    text: str
    make_ppt: bool = False
    tags: Optional[List[str]] = None
    date: Optional[str] = None  # YYYY-MM-DD (없으면 compose_dual_tool 결과의 due 사용)

class ProcessOut(BaseModel):
    title: str
    task: str
    due: Optional[str]
    content_md: str
    notion: dict
    ppt: Optional[dict] = None

# ---------- Helpers ----------
def _read_upload(uf: UploadFile) -> str:
    b = uf.file.read()
    for enc in ("utf-8", "cp949", "euc-kr"):
        try:
            return b.decode(enc)
        except Exception:
            continue
    return b.decode("utf-8", errors="ignore")

def _clamp10(s: str) -> str:
    s = (s or "").strip()
    s = re.sub(r"\s+", " ", s)
    return (s[:10] or "학습요약")

def _make_ppt_with_presenton(title: str, md: str) -> dict:
    """
    Presenton 로컬 API 호출 → PPTX 생성
    """
    url = f"{PRESENTON_URL}/api/v1/ppt/presentation/generate"
    payload = {
        "content": md,           # Markdown 원문
        "export_as": "pptx",     # 반드시 pptx
        "language": "Korean",
        "n_slides": 5,           # 불안정하면 5~6으로 낮춰 테스트
        "template": "general",
        # "title": title,        # 엔진이 지원하면 주석 해제
    }
    try:
        r = requests.post(url, json=payload, timeout=PRESENTON_TIMEOUT)
        r.raise_for_status()
        data = r.json()

        # 엔진별 키 다름: path/ppt_url, edit_path/edit_url 등 매핑
        ppt_url  = data.get("path") or data.get("ppt_url")
        edit_url = data.get("edit_path") or data.get("edit_url")

        return {
            "status": "ok",
            "ppt_url": ppt_url,
            "edit_url": edit_url,
            "presentation_id": data.get("presentation_id"),
            "engine": "presenton",
            "raw": data,  # 디버깅용(원하면 제거)
        }

    except requests.HTTPError as e:
        # 에러 본문도 같이 반환
        msg = getattr(e.response, "text", str(e))
        return {"status": "error", "engine": "presenton", "message": f"HTTP {e.response.status_code}: {msg}"}
    except Exception as e:
        return {"status": "error", "engine": "presenton", "message": str(e)}   
    


def _try_make_ppt(title: str, md: str) -> Optional[dict]:
    if not PPT_SERVICE_URL:
        return None
    try:
        r = requests.post(PPT_SERVICE_URL, json={"title": title, "content": md}, timeout=60)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        return {"error": str(e)}

# ---------- Endpoints ----------
@app.get("/health")
def health():
    return {
        "ok": True,
        "has_openai": bool(os.getenv("OPENAI_API_KEY")),
        "has_claude": bool(os.getenv("CLAUDE_API_KEY")),
        "has_notion": bool(os.getenv("NOTION_TOKEN") and os.getenv("NOTION_DATABASE_ID")),
        "ppt_service": PPT_SERVICE_URL or None,
    }

@app.post("/process", response_model=ProcessOut)
async def process_json(payload: ProcessIn):
    comp = await asyncio.to_thread(mcpserver.compose_dual_tool, payload.text)

    title = _clamp10(comp.get("title") or comp.get("task") or "학습요약")
    task  = comp.get("task") or title

    comp_due = comp.get("due")          # ← GPT가 판단한 due
    due      = payload.date or comp_due # 저장용 날짜(사용자 입력이 있으면 그걸 저장)

    # ✅ 태그 정책: GPT가 due를 뽑아낸 경우에만 '과제' 추가
    base_tags = (payload.tags or []).copy()
    if comp_due:
        base_tags.append("과제")
    # 중복 제거 + 공백 정리
    tags = []
    for t in base_tags:
        t = (t or "").strip()
        if t and t not in tags:
            tags.append(t)

    md = comp.get("content_md") or "# 요약\n(내용이 비어 있습니다)"

    notion_res = await asyncio.to_thread(
        mcpserver.notion_tool,
        content=md, title=title, date=due, tags=tags if tags else None
    )

    ppt_res = _make_ppt_with_presenton(title, md) if payload.make_ppt else None
    
    return ProcessOut(title=title, task=task, due=due, content_md=md, notion=notion_res, ppt=ppt_res)

@app.post("/process-file", response_model=ProcessOut)
async def process_file(
    file: UploadFile = File(...),
    make_ppt: bool = Form(False),
    tags: Optional[str] = Form(None),
    date: Optional[str] = Form(None),
):
    text = (file.file.read()).decode("utf-8", errors="ignore")
    user_tags = [t.strip() for t in (tags or "").split(",") if t and t.strip()]

    comp = await asyncio.to_thread(mcpserver.compose_dual_tool, text)

    title = _clamp10(comp.get("title") or comp.get("task") or "학습요약")
    task  = comp.get("task") or title

    comp_due = comp.get("due")         # ← GPT 판단
    due      = date or comp_due        # 저장용 날짜

    # ✅ GPT가 due를 뽑아냈을 때만 '과제' 추가
    base_tags = user_tags.copy()
    if comp_due:
        base_tags.append("과제")
    # 중복 제거
    tags_final = []
    for t in base_tags:
        t = (t or "").strip()
        if t and t not in tags_final:
            tags_final.append(t)

    md = comp.get("content_md") or "# 요약\n(내용이 비어 있습니다)"

    notion_res = await asyncio.to_thread(
        mcpserver.notion_tool,
        content=md, title=title, date=due, tags=tags_final if tags_final else None
    )
    ppt_res = _make_ppt_with_presenton(title, md) if make_ppt else None

    return ProcessOut(title=title, task=task, due=due, content_md=md, notion=notion_res, ppt=ppt_res)
