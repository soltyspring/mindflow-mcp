#!/usr/bin/env python
# server.py
import os, re, json, requests, asyncio, time, urllib.parse
from typing import Optional, List, Dict, Any

from fastapi import FastAPI, File, UploadFile, Form, Request, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, JSONResponse
from pydantic import BaseModel
from dotenv import load_dotenv

from sqlalchemy import create_engine, Column, BigInteger, String, Text, DateTime, ForeignKey, func
from sqlalchemy.orm import declarative_base, sessionmaker, Session, relationship
from sqlalchemy import or_

from cryptography.fernet import Fernet


# DB 엔진 및 세션 설정
# ─────────────────────────────────────────────────────────────
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session

DATABASE_URL = os.getenv("DATABASE_URL", "mysql+pymysql://root:a3497@localhost:3306/mindflow")


# FastAPI 종속성 주입용
def get_db():
    """
    요청마다 새로운 DB 세션을 생성하고, 요청이 끝나면 자동으로 닫아주는 함수
    """
    db: Session = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# ──────────────────────────────────────────────────────────────────────────────
# 0) .env 먼저 로드 (기존 코드보다 위로 당김: 환경변수 선반영)
# ──────────────────────────────────────────────────────────────────────────────
load_dotenv()
# ──────────────────────────────────────────────────────────────────────────────
# 1) 환경변수GoogleUser
# ──────────────────────────────────────────────────────────────────────────────
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:5173/")
PRESENTON_URL = os.getenv("PRESENTON_URL", "http://localhost:5000")
PRESENTON_TIMEOUT = int(os.getenv("PRESENTON_TIMEOUT", "120"))
PPT_SERVICE_URL = os.getenv("PPT_SERVICE_URL")  # 예: http://localhost:5000/generate
DATABASE_URL = os.getenv("DATABASE_URL")
ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY")

# Google OAuth
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI", "http://localhost:8000/auth/google/callback")
SESSION_SECRET = os.getenv("SESSION_SECRET", "change-me")

#DB 세션 암호화 키
fernet = Fernet(ENCRYPTION_KEY.encode() if isinstance(ENCRYPTION_KEY, str) else ENCRYPTION_KEY)

Base = declarative_base()
engine = create_engine(DATABASE_URL, pool_pre_ping=True, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)




# ─────────── GoogleUser ───────────
class GoogleUser(Base):
    __tablename__ = "google_users"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    google_id = Column(String(255), unique=True, nullable=False)
    email = Column(String(255), unique=True, nullable=False)
    name = Column(String(120))
    picture = Column(String(512))
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    notion_token = relationship("NotionToken", back_populates="user", uselist=False)

# ─────────── NotionSetting ───────────
class NotionSetting(Base):
    __tablename__ = "notion_settings"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    google_user_id = Column(BigInteger, ForeignKey("google_users.id"), unique=True, index=True, nullable=False)
    parent_page_id = Column(String(64), nullable=True)
    database_id = Column(String(64), nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

# ─────────── NotionToken ───────────
class NotionToken(Base):
    __tablename__ = "notion_tokens"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    google_user_id = Column(BigInteger, ForeignKey("google_users.id"), nullable=False)
    notion_token = Column(Text, nullable=False)  # 암호화 저장
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    user = relationship("GoogleUser", back_populates="notion_token")



def enc(s: str) -> str:
    return fernet.encrypt(s.encode()).decode()

def dec(s: str) -> str:
    return fernet.decrypt(s.encode()).decode()

# ── MCP 툴이 들어있는 모듈 (compose_dual_tool, notion_tool 사용)
import mcpserver  # 같은 디렉터리에 있어야 import 성공

# ──────────────────────────────────────────────────────────────────────────────
# 2) FastAPI 앱 & 미들웨어
# ──────────────────────────────────────────────────────────────────────────────
app = FastAPI(title="MindFlow Web API", version="0.1.0")

# CORS 설정
from fastapi.middleware.cors import CORSMiddleware

FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:5173/")
FRONTEND_ORIGIN = "http://localhost:5173"

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],  # ✅ 끝에 슬래시 ❌
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
    https_only=False,
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


# === 추가: 통합 세팅 입력 ===
class NotionSetupIn(BaseModel):
    token: Optional[str] = None          # secret_... (옵션)
    parent_url: Optional[str] = None     # 부모 페이지 URL 또는 ID (옵션)
    name: str = "MindFlow Notes"

# === 추가: Notion 페이지 ID 추출 ===
def _extract_page_id(url_or_id: str) -> str:
    s = (url_or_id or "").strip()
    if not s:
        raise HTTPException(status_code=400, detail="parent_url 비어 있음")
    import re
    # 36자(대시 포함) → 32자로
    m = re.search(r"([0-9a-fA-F-]{36})", s)
    if m:
        return m.group(1).replace("-", "")
    # 32자 그대로
    if re.search(r"[0-9a-fA-F]{32}", s):
        return re.search(r"[0-9a-fA-F]{32}", s).group(0)
    raise HTTPException(status_code=400, detail="유효한 Notion 페이지 URL/ID가 아닙니다.")

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
# 4.5) Notion helpers (토큰/헤더/홈페이지+DB 보장)
# ──────────────────────────────────────────────────────────────────────────────
NOTION_VERSION = "2022-06-28"

def _get_user_notion_token(db: Session, user_id: int) -> str:
    u = db.get(GoogleUser, user_id)
    if not u or not u.notion_token:
        raise HTTPException(status_code=404, detail="Notion 토큰이 저장되어 있지 않습니다.")
    return dec(u.notion_token.notion_token)

def _notion_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }

def _ensure_home_page(db: Session, user_id: int, token: str) -> str:
    """
    내부 통합(internal integration)은 workspace 루트에 페이지를 새로 못 만듭니다.
    따라서 '부모 페이지'를 미리 받아두고 거기에만 생성해야 합니다.
    부모가 없으면 명확한 409 에러를 던집니다.
    """
    setting = db.query(NotionSetting).filter_by(google_user_id=user_id).one_or_none()
    if setting and setting.parent_page_id:
        return setting.parent_page_id

    # 기존: workspace=True 로 페이지 생성하던 부분 제거
    raise HTTPException(status_code=409, detail="need_parent_page")

def _ensure_user_database(db: Session, user_id: int, token: str, db_name: str = "MindFlow Notes") -> str:
    setting = db.query(NotionSetting).filter_by(google_user_id=user_id).one_or_none()
    if setting and setting.database_id:
        return setting.database_id

    parent_page_id = _ensure_home_page(db, user_id, token)
    body = {
        "parent": {"type": "page_id", "page_id": parent_page_id},
        "title": [{"type": "text", "text": {"content": db_name}}],
        "properties": {
            "Name": {"title": {}},
            "Tags": {"multi_select": {}},
            "Date": {"date": {}},
            "Source": {"rich_text": {}},
            "Status": {"status": {}},
        },
    }
    r = requests.post("https://api.notion.com/v1/databases", headers=_notion_headers(token), json=body, timeout=15)
    if r.status_code != 200:
        raise HTTPException(status_code=r.status_code, detail=f"데이터베이스 생성 실패: {r.text}")
    database_id = r.json()["id"]

    if not setting:
        setting = NotionSetting(google_user_id=user_id, parent_page_id=parent_page_id, database_id=database_id)
        db.add(setting)
    else:
        setting.database_id = database_id
    db.commit()
    return database_id

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

@app.post("/logout")
def logout(request: Request):
    # 세션 내용 비우기
    request.session.clear()

    # 응답 생성 + 세션 쿠키 강제 삭제(이름 기본값: "session")
    resp = JSONResponse({"ok": True})
    # SessionMiddleware 기본 쿠키 이름은 "session" 입니다. 바꾸지 않았다면 아래 그대로 사용.
    resp.delete_cookie(key="session", path="/")
    return resp

@app.get("/auth/google/callback")
def auth_callback(request: Request, code: Optional[str] = None, state: Optional[str] = None, db: Session = Depends(get_db)):
    if not code or not state:
        return _err("missing code/state")

    # state 검증
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

    # ID 토큰 검증
    try:
        claims = id_token.verify_oauth2_token(id_tok, google_requests.Request(), GOOGLE_CLIENT_ID)
    except Exception as e:
        return _err(f"id_token invalid: {e}", 401)

    sub = claims.get("sub")
    email = claims.get("email")
    name = claims.get("name")
    picture = claims.get("picture")

    # 🔹 DB 업서트
    u = db.query(GoogleUser).filter(or_(GoogleUser.google_id == sub, GoogleUser.email == email)).one_or_none()
    if u:
        u.google_id = sub
        u.email = email or u.email
        u.name = name
        u.picture = picture
    else:
        u = GoogleUser(google_id=sub, email=email, name=name, picture=picture)
        db.add(u)
    db.commit()
    db.refresh(u)

    request.session["user"] = {
        "id": u.id, "sub": sub, "email": email, "name": name, "picture": picture,
    }

    return RedirectResponse(FRONTEND_URL)


@app.get("/me")
def me(request: Request, db: Session = Depends(get_db)):
    sess = get_current_user(request)
    if not sess:
        return _err("unauthenticated", 401)
    u = db.get(GoogleUser, sess.get("id")) if sess.get("id") else None
    has_notion = bool(u and u.notion_token)
    return {"ok": True, "user": {
        "id": u.id if u else None,
        "email": sess.get("email"),
        "name": sess.get("name"),
        "picture": sess.get("picture"),
        "has_notion": has_notion,
    }}




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
async def process_json(payload: ProcessIn, user=Depends(require_user), db: Session = Depends(get_db)): # ← 로그인 필요하게 예시 적용
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

    # ✅ 노션 DB 보장 (없으면 자동 생성)
    token = _get_user_notion_token(db, user["id"])
    db_id = _ensure_user_database(db, user["id"], token)  # ← 반환값 받기!!

    # ✅ 노션 기록
    notion_res = await asyncio.to_thread(
        mcpserver.notion_tool,
        content=md,
        title=title,
            date=due,
            tags=tags,
            user_email=user["email"],
            notion_database_id=db_id,
            user_token_plain=token,      # ✅ 이 줄 추가!
            
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
    db: Session = Depends(get_db),
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

    # ✅ 노션 DB 보장 (없으면 자동 생성)
    token = _get_user_notion_token(db, user["id"])
    db_id = _ensure_user_database(db, user["id"], token)

    # ✅ 노션 기록
    notion_res = await asyncio.to_thread(
        mcpserver.notion_tool,
        content=md,
        title=title,
        date=due,
        tags=tags_final if tags_final else None,
        user_email=user["email"],
        notion_database_id=db_id,  # ← 추가
    )

    ppt_res = _make_ppt_with_presenton(title, md) if make_ppt else None
    return ProcessOut(title=title, task=task, due=due, content_md=md, notion=notion_res, ppt=ppt_res)

class NotionTokenIn(BaseModel):
    token: str

@app.post("/me/notion-token")
def save_notion_token(payload: NotionTokenIn, request: Request, db: Session = Depends(get_db)):
    sess = get_current_user(request)
    if not sess:
        return _err("unauthenticated", 401)
    u = db.get(GoogleUser, sess["id"])
    if not u:
        return _err("user not found", 404)

    enc_token = enc(payload.token)
    if u.notion_token:
        u.notion_token.notion_token = enc_token
    else:
        db.add(NotionToken(google_user_id=u.id, notion_token=enc_token))
    db.commit()
    return {"ok": True}

@app.delete("/me/notion-token")
def delete_notion_token(request: Request, db: Session = Depends(get_db)):
    sess = get_current_user(request)
    if not sess:
        return _err("unauthenticated", 401)
    u = db.get(GoogleUser, sess["id"])
    if not u:
        return _err("user not found", 404)
    if u.notion_token:
        db.delete(u.notion_token)
        db.commit()
    return {"ok": True}

# (상태 조회) 저장 여부 + 마스킹
@app.get("/me/notion-token")
def get_notion_token_status(request: Request, db: Session = Depends(get_db)):
    sess = get_current_user(request)
    if not sess:
        return _err("unauthenticated", 401)
    u = db.get(GoogleUser, sess["id"])
    if not u or not u.notion_token:
        return {"ok": True, "has_token": False, "masked": None}
    try:
        plain = dec(u.notion_token.notion_token)
        masked = (plain[:6] + "****" + plain[-2:]) if len(plain) > 8 else (plain[:4] + "****")
    except Exception:
        masked = "(masked)"
    return {"ok": True, "has_token": True, "masked": masked}

# (유효성 검사)
@app.post("/me/notion-token/verify")
def verify_notion_token(request: Request, db: Session = Depends(get_db)):
    sess = get_current_user(request)
    if not sess:
        return _err("unauthenticated", 401)
    token = _get_user_notion_token(db, sess["id"])
    r = requests.get("https://api.notion.com/v1/users/me", headers=_notion_headers(token), timeout=10)
    return {"ok": r.status_code == 200, "status": r.status_code, "body": r.text if r.status_code != 200 else None}

# (DB 상태)
@app.get("/me/notion-db")
def get_notion_db_status(request: Request, db: Session = Depends(get_db)):
    sess = get_current_user(request)
    if not sess:
        return _err("unauthenticated", 401)
    setting = db.query(NotionSetting).filter_by(google_user_id=sess["id"]).one_or_none()
    return {
        "ok": True,
        "has_database": bool(setting and setting.database_id),
        "database_id": getattr(setting, "database_id", None),
        "parent_page_id": getattr(setting, "parent_page_id", None),
    }

# (원클릭) 홈 페이지 + DB 자동 생성
class InitDbIn(BaseModel):
    name: str = "MindFlow Notes"

@app.post("/me/notion-db/auto")
def init_notion_db_auto(payload: InitDbIn, request: Request, db: Session = Depends(get_db)):
    sess = get_current_user(request)
    if not sess:
        return _err("unauthenticated", 401)
    token = _get_user_notion_token(db, sess["id"])
    db_id = _ensure_user_database(db, sess["id"], token, db_name=payload.name.strip() or "MindFlow Notes")
    setting = db.query(NotionSetting).filter_by(google_user_id=sess["id"]).one()
    return {"ok": True, "database_id": db_id, "parent_page_id": setting.parent_page_id}

@app.post("/me/notion-setup")
def notion_setup(payload: NotionSetupIn, request: Request, db: Session = Depends(get_db)):
    sess = get_current_user(request)
    if not sess:
        return _err("unauthenticated", 401)

    # ✅ 둘 다 필수
    if not (payload.token and payload.token.strip() and payload.parent_url and payload.parent_url.strip()):
        raise HTTPException(status_code=400, detail="token과 parent_url이 모두 필요합니다.")

    # 1) 토큰 저장
    u = db.get(GoogleUser, sess["id"])
    if not u:
        return _err("user not found", 404)
    enc_token = enc(payload.token.strip())
    if u.notion_token:
        u.notion_token.notion_token = enc_token
    else:
        db.add(NotionToken(google_user_id=u.id, notion_token=enc_token))
    db.commit()

    # 2) 부모 페이지 검증 + 저장
    token = _get_user_notion_token(db, sess["id"])
    page_id = _extract_page_id(payload.parent_url)
    r = requests.get(f"https://api.notion.com/v1/pages/{page_id}", headers=_notion_headers(token), timeout=10)
    if r.status_code == 404:
        raise HTTPException(status_code=404, detail="해당 페이지를 찾을 수 없습니다.")
    if r.status_code == 403:
        raise HTTPException(status_code=403, detail="통합에 페이지 접근권한이 없습니다. 노션에서 ‘공유 → 통합에 연결’을 해주세요.")
    if r.status_code != 200:
        raise HTTPException(status_code=r.status_code, detail=f"페이지 확인 실패: {r.text}")

    setting = db.query(NotionSetting).filter_by(google_user_id=sess["id"]).one_or_none()
    if not setting:
        setting = NotionSetting(google_user_id=sess["id"], parent_page_id=page_id)
        db.add(setting)
    else:
        setting.parent_page_id = page_id
    db.commit()

    # 3) DB 자동 생성
    db_id = _ensure_user_database(db, sess["id"], token, db_name=(payload.name or "MindFlow Notes").strip())
    setting = db.query(NotionSetting).filter_by(google_user_id=sess["id"]).one()
    return {"ok": True, "database_id": db_id, "parent_page_id": setting.parent_page_id}

