#!/usr/bin/env python
# server.py
import os, re, json, requests, asyncio, time, urllib.parse
from typing import Optional, List, Dict, Any

from fastapi import FastAPI, File, UploadFile, Form, Request, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, JSONResponse
from pydantic import BaseModel
from dotenv import load_dotenv

# ──────────────────────────────────────────────────────────────────────────────
# 0) .env 먼저 로드 (기존 코드보다 위로 당김: 환경변수 선반영)
# ──────────────────────────────────────────────────────────────────────────────
load_dotenv()
# ──────────────────────────────────────────────────────────────────────────────
# 1) 환경변수
# ──────────────────────────────────────────────────────────────────────────────
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:5173/")
PRESENTON_URL = os.getenv("PRESENTON_URL", "http://localhost:5000")
PRESENTON_TIMEOUT = int(os.getenv("PRESENTON_TIMEOUT", "120"))
PPT_SERVICE_URL = os.getenv("PPT_SERVICE_URL")  # 예: http://localhost:5000/generate

# Google OAuth
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI", "http://localhost:8000/auth/google/callback")
SESSION_SECRET = os.getenv("SESSION_SECRET", "change-me")  # 반드시 강한 랜덤값

# ── MCP 툴이 들어있는 모듈 (compose_dual_tool, notion_tool 사용)
import mcpserver  # 같은 디렉터리에 있어야 import 성공

# ──────────────────────────────────────────────────────────────────────────────
# 2) FastAPI 앱 & 미들웨어
# ──────────────────────────────────────────────────────────────────────────────
app = FastAPI(title="MindFlow Web API", version="0.1.0")

# CORS 설정
from fastapi.middleware.cors import CORSMiddleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],  # 프론트 실행 주소
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 서버 사이드 세션(서명 쿠키) — DB 불필요
from starlette.middleware.sessions import SessionMiddleware
app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    same_site="lax",
    https_only=False,  # 운영 도메인에서는 True + HTTPS 권장
)

# ──────────────────────────────────────────────────────────────────────────────
# 3) 스키마
# ──────────────────────────────────────────────────────────────────────────────
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
    notion: Dict[str, Any]
    ppt: Optional[Dict[str, Any]] = None

# ──────────────────────────────────────────────────────────────────────────────
# 4) 헬퍼
# ──────────────────────────────────────────────────────────────────────────────
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
        "content": md,
        "export_as": "pptx",
        "language": "Korean",
        "n_slides": 5,
        "template": "general",
        # "title": title,
    }
    try:
        r = requests.post(url, json=payload, timeout=PRESENTON_TIMEOUT)
        r.raise_for_status()
        data = r.json()

        ppt_url  = data.get("path") or data.get("ppt_url")
        edit_url = data.get("edit_path") or data.get("edit_url")

        return {
            "status": "ok",
            "ppt_url": ppt_url,
            "edit_url": edit_url,
            "presentation_id": data.get("presentation_id"),
            "engine": "presenton",
            "raw": data,
        }

    except requests.HTTPError as e:
        msg = getattr(e.response, "text", str(e))
        code = getattr(e.response, "status_code", 500)
        return {"status": "error", "engine": "presenton", "message": f"HTTP {code}: {msg}"}
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

# ──────────────────────────────────────────────────────────────────────────────
# 5) 구글 OAuth (웹 서버 코드 플로우 + ID 토큰 검증)
# ──────────────────────────────────────────────────────────────────────────────
from itsdangerous import TimestampSigner, BadSignature
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests

_signer = TimestampSigner(SESSION_SECRET)

def _err(msg: str, status: int = 400):
    return JSONResponse({"ok": False, "error": msg}, status_code=status)

def get_current_user(request: Request) -> Optional[dict]:
    sess = request.session or {}
    return sess.get("user")

def require_user(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="login required")
    return user

@app.get("/auth/google/login")
def auth_login(request: Request):
    if not GOOGLE_CLIENT_ID or not GOOGLE_REDIRECT_URI:
        return _err("Google OAuth env not set (GOOGLE_CLIENT_ID/GOOGLE_REDIRECT_URI)", 500)

    # CSRF 방지용 state 서명
    state_payload = json.dumps({"t": int(time.time())})
    state = _signer.sign(state_payload.encode()).decode()

    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        # "code_challenge": "...", "code_challenge_method": "S256"  # PKCE를 쓰려면 추가
        "access_type": "offline",  # 선택: refresh_token 원할 때
        # "prompt": "consent",     # 선택: 매번 동의 유도
    }
    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(params)
    return RedirectResponse(url)

@app.get("/auth/google/callback")
def auth_callback(request: Request, code: Optional[str] = None, state: Optional[str] = None):
    if not code or not state:
        return _err("missing code/state")

    # state 검증 (10분 유효)
    try:
        raw = _signer.unsign(state.encode(), max_age=600)
        _ = json.loads(raw.decode())
    except BadSignature:
        return _err("invalid state", 400)

    # 토큰 교환
    try:
        token_res = requests.post(
            "https://oauth2.googleapis.com/token",
            data={
                "code": code,
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "redirect_uri": GOOGLE_REDIRECT_URI,
                "grant_type": "authorization_code",
            },
            timeout=20,
        )
        token_res.raise_for_status()
    except requests.HTTPError as e:
        msg = getattr(e.response, "text", str(e))
        code_ = getattr(e.response, "status_code", 502)
        return _err(f"token exchange failed: HTTP {code_}: {msg}", 502)
    except Exception as e:
        return _err(f"token exchange failed: {e}", 502)

    tok = token_res.json()
    id_tok = tok.get("id_token")
    if not id_tok:
        return _err("no id_token", 502)

    # ID 토큰 검증 (iss, aud, exp)
    try:
        claims = id_token.verify_oauth2_token(id_tok, google_requests.Request(), GOOGLE_CLIENT_ID)
        # claims 예) { "sub": "...", "email": "...", "name": "...", "picture": "..." }
    except Exception as e:
        return _err(f"id_token invalid: {e}", 401)

    # 세션 저장 (DB 불필요)
    request.session["user"] = {
        "sub": claims.get("sub"),
        "email": claims.get("email"),
        "name": claims.get("name"),
        "picture": claims.get("picture"),
    }
    return RedirectResponse(FRONTEND_URL)  # 필요시 원하는 경로로 변경

@app.get("/me")
def me(request: Request):
    user = get_current_user(request)
    if not user:
        return _err("unauthenticated", 401)
    return {"ok": True, "user": user}

@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return {"ok": True}

# ──────────────────────────────────────────────────────────────────────────────
# 6) 기존 엔드포인트
# ──────────────────────────────────────────────────────────────────────────────
@app.get("/health")
def health():
    return {
        "ok": True,
        "has_openai": bool(os.getenv("OPENAI_API_KEY")),
        "has_claude": bool(os.getenv("CLAUDE_API_KEY")),
        "has_notion": bool(os.getenv("NOTION_TOKEN") and os.getenv("NOTION_DATABASE_ID")),
        "ppt_service": PPT_SERVICE_URL or None,
        "google_oauth_ready": bool(GOOGLE_CLIENT_ID and GOOGLE_REDIRECT_URI),
    }

@app.post("/process", response_model=ProcessOut)
async def process_json(payload: ProcessIn, user=Depends(require_user)):  # ← 로그인 필요하게 예시 적용
    comp = await asyncio.to_thread(mcpserver.compose_dual_tool, payload.text)

    title = _clamp10(comp.get("title") or comp.get("task") or "학습요약")
    task  = comp.get("task") or title

    comp_due = comp.get("due")
    due      = payload.date or comp_due

    base_tags = (payload.tags or []).copy()
    if comp_due:
        base_tags.append("과제")
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
    user=Depends(require_user),  # ← 로그인 필요
):
    text = (file.file.read()).decode("utf-8", errors="ignore")
    user_tags = [t.strip() for t in (tags or "").split(",") if t and t.strip()]

    comp = await asyncio.to_thread(mcpserver.compose_dual_tool, text)

    title = _clamp10(comp.get("title") or comp.get("task") or "학습요약")
    task  = comp.get("task") or title

    comp_due = comp.get("due")
    due      = date or comp_due

    base_tags = user_tags.copy()
    if comp_due:
        base_tags.append("과제")
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
