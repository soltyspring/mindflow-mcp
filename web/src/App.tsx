import React, { useMemo, useRef, useState, useEffect } from "react";

type MeResp =
  | { ok: true; user: { email: string; name?: string; picture?: string } }
  | { ok: false; error: string };

async function fetchMe(): Promise<MeResp> {
  const r = await fetch(`${API_BASE}/me`, { credentials: "include" });
  return r.json();
}

// API 베이스
const BACKEND = "http://localhost:8000";
const API_BASE = BACKEND;

// 유틸
function cx(...xs: Array<string | false | null | undefined>) {
  return xs.filter(Boolean).join(" ");
}

async function postJSON<T>(path: string, payload: any): Promise<T> {
  const r = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
    credentials: "include",           // 세션 쿠키 추가
  });
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

async function postFormData<T>(path: string, fd: FormData): Promise<T> {
  const r = await fetch(`${API_BASE}${path}`, { method: "POST", body: fd, credentials: "include" }); // ★ 추가
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

// 타입
interface ProcessOut {
  title: string;
  task: string;
  due?: string | null;
  content_md: string;
  notion: { status: string; url?: string; id?: string; message?: string };
  ppt?: { url?: string; ppt_url?: string; [k: string]: any } | null;
}

// 미니 토스트
function useToast() {
  const [msg, setMsg] = useState<string | null>(null);
  useEffect(() => {
    if (!msg) return;
    const t = setTimeout(() => setMsg(null), 2500);
    return () => clearTimeout(t);
  }, [msg]);
  return { msg, show: (m: string) => setMsg(m) } as const;
}

// 스켈레톤 바
function ProgressBar({ show }: { show: boolean }) {
  return (
    <div className={cx("fixed left-0 right-0 top-0 z-50 h-1", show ? "opacity-100" : "opacity-0")}
      style={{ transition: "opacity .25s" }}>
      <div className="h-1 w-1/3 animate-[progress_1.2s_ease-in-out_infinite] bg-black/80 rounded-r-full" />
      <style>{`@keyframes progress{0%{margin-left:-33%}50%{margin-left:50%}100%{margin-left:100%}}`}</style>
    </div>
  );
}

// 스위치
function Switch({ checked, onChange, label }: { checked: boolean; onChange: (v: boolean) => void; label: string }) {
  return (
    <label className="flex items-center gap-3 select-none cursor-pointer">
      <span className="text-sm text-gray-600">{label}</span>
      <button
        type="button"
        onClick={() => onChange(!checked)}
        className={cx(
          "relative inline-flex h-6 w-11 items-center rounded-full transition-colors",
          checked ? "bg-black" : "bg-gray-300"
        )}
        aria-pressed={checked}
      >
        <span
          className={cx(
            "inline-block h-5 w-5 transform rounded-full bg-white shadow transition-transform",
            checked ? "translate-x-5" : "translate-x-1"
          )}
        />
      </button>
    </label>
  );
}

// 인라인 아이콘 (SVG)
const IconUpload = () => (
  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>
);
const IconLink = () => (
  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <path d="M10 13a5 5 0 0 1 7 0l1 1a5 5 0 0 1-7 7l-1-1"/><path d="M14 11a5 5 0 0 1-7 0l-1-1a5 5 0 0 1 7-7l1 1"/></svg>
);
const IconCheck = () => (
  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M20 6L9 17l-5-5"/></svg>
);
const IconX = () => (
  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
);

export default function App() {
  const [text, setText] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [makePpt, setMakePpt] = useState(false);
  const [tags, setTags] = useState<string>("");
  const [date, setDate] = useState<string>("");

  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [res, setRes] = useState<ProcessOut | null>(null);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const toast = useToast();

  const canSubmit = (text.trim().length > 0 || file !== null) && !loading;
  const [me, setMe] = useState<MeResp | null>(null);
  useEffect(() => { fetchMe().then(setMe).catch(() => setMe(null)); }, []);

  function goGoogleLogin() {
    window.location.href = `${BACKEND}/auth/google/login`;
  }

  async function doLogout() {
    await fetch(`${API_BASE}/logout`, { method: "POST", credentials: "include" });
    setMe(null);
    toast.show("로그아웃 했어요");
  }

  function handleDrop(e: React.DragEvent<HTMLDivElement>) {
    e.preventDefault();
    const f = e.dataTransfer.files?.[0];
    if (f) { setFile(f); toast.show("파일을 추가했어요"); }
  }

  async function doSubmit() {
    setLoading(true); setError(null); setRes(null);
    try {
      if (file) {
        const fd = new FormData();
        fd.append("file", file);
        fd.append("make_ppt", String(makePpt));
        if (tags.trim()) fd.append("tags", tags);
        if (date.trim()) fd.append("date", date);
        const data = await postFormData<ProcessOut>("/process-file", fd);
        setRes(data);
      } else {
        const payload = { text, make_ppt: makePpt, tags: tags.split(",").map(s=>s.trim()).filter(Boolean), date: date || null };
        const data = await postJSON<ProcessOut>("/process", payload);
        setRes(data);
      }
      toast.show("정리 완료!");
    } catch (e: any) {
      setError(e?.message || "요청 실패");
    } finally {
      setLoading(false);
      setConfirmOpen(false);
    }
  }

  return (
    <div className="min-h-screen flex flex-col bg-gradient-to-b from-white via-white to-gray-100">
      <ProgressBar show={loading} />

      {/* 헤더 */}
      <header className="px-6 py-4 border-b bg-white/80 backdrop-blur supports-[backdrop-filter]:bg-white/60 sticky top-0 z-40">
      <div className="max-w-6xl mx-auto flex items-center justify-between gap-4">
        <div className="flex items-center gap-3">
          <div>
            <h1 className="text-lg md:text-xl font-semibold leading-tight">MindFlow</h1>
          </div>
        </div>

        <div className="flex items-center gap-4">
          <Switch checked={makePpt} onChange={setMakePpt} label="PPT 생성" />

          {/* 로그인 상태에 따라 분기 */}
          {me?.ok ? (
            <div className="flex items-center gap-3">
              <img
                src={me.user.picture || "https://placehold.co/32x32?text=U"}
                alt="avatar"
                className="h-8 w-8 rounded-full border"
              />
              <span className="text-sm">{me.user.name || me.user.email}</span>
              <button onClick={doLogout} className="px-3 py-2 rounded-xl border hover:bg-gray-100 text-sm">
                로그아웃
              </button>
              <button
                onClick={() => (canSubmit ? setConfirmOpen(true) : toast.show("텍스트 또는 파일을 넣어주세요"))}
                disabled={!canSubmit}
                className={cx(
                  "px-4 py-2 rounded-xl text-white shadow-sm transition-all",
                  canSubmit ? "bg-black hover:bg-gray-800 active:scale-[.98]" : "bg-gray-400 cursor-not-allowed"
                )}
              >
                Notion에 정리
              </button>
            </div>
          ) : (
            <div className="flex items-center gap-3">
              <button
                onClick={goGoogleLogin}
                className="px-4 py-2 rounded-xl border hover:bg-gray-100"
                title="회원가입/로그인"
              >
                Login
              </button>
              <button
                onClick={() => (canSubmit ? setConfirmOpen(true) : toast.show("텍스트 또는 파일을 넣어주세요"))}
                disabled={!canSubmit}
                className={cx(
                  "px-4 py-2 rounded-xl text-white shadow-sm transition-all",
                  canSubmit ? "bg-black hover:bg-gray-800 active:scale-[.98]" : "bg-gray-400 cursor-not-allowed"
                )}
              >
                Notion에 정리
              </button>
            </div>
          )}
        </div>
      </div> {/* ← 이 닫는 div가 반드시 필요! */}
    </header>

      {/* 메인 */}
      <main className="flex-1 max-w-6xl mx-auto p-6 grid lg:grid-cols-2 gap-6">
        {/* 입력 카드 */}
        <section className="bg-white rounded-2xl shadow-sm border p-5 space-y-4">
          <div className="flex items-center justify-between">
            <h2 className="font-semibold">입력</h2>
            {file && (
              <button onClick={() => setFile(null)} className="text-sm text-gray-600 hover:text-black">파일 제거</button>
            )}
          </div>

          <textarea
            value={text}
            onChange={(e)=>setText(e.target.value)}
            placeholder="여기에 텍스트를 붙여넣으세요 (.txt 파일 드래그&드롭/업로드도 가능)"
            className="w-full h-56 border rounded-xl p-3 focus:outline-none focus:ring-2 focus:ring-black/50"
          />

          <div
            onDrop={handleDrop}
            onDragOver={(e)=>e.preventDefault()}
            className={cx(
              "rounded-xl border-2 border-dashed p-6 text-center text-sm text-gray-600",
              "hover:bg-gray-50"
            )}
          >
            {file ? (
              <div className="flex items-center justify-between">
                <div>
                  <div className="font-medium">선택된 파일</div>
                  <div className="text-gray-500 text-sm">{file.name}</div>
                </div>
                <button onClick={()=>setFile(null)} className="px-3 py-1 rounded-lg border hover:bg-gray-100">제거</button>
              </div>
            ) : (
              <>
                <div className="mb-2 font-medium flex items-center justify-center gap-2"><IconUpload/> 파일 드래그&드롭</div>
                <div className="mb-3">또는</div>
                <button onClick={()=>fileInputRef.current?.click()} className="px-3 py-1 rounded-lg border hover:bg-gray-100">파일 선택</button>
                <input ref={fileInputRef} type="file" accept=".txt" className="hidden"
                  onChange={(e)=>setFile(e.target.files?.[0] ?? null)} />
              </>
            )}
          </div>

          <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
            <div className="md:col-span-2">
              <label htmlFor="tags" className="text-sm text-gray-600">태그(쉼표 구분)</label>
              <input
                id="tags"
                type="text"
                value={tags}                               // 기본값은 useState("") 로 비우기
                onChange={(e) => setTags(e.target.value)}
                placeholder="예) SQL 과제, 자료구조, 발표"   // ← 회색 안내 문구
                className="mt-1 w-full border rounded-lg px-3 py-2
                          focus:outline-none focus:ring-2 focus:ring-black/50
                          placeholder:text-gray-400 placeholder:italic"
              />
            </div>
          </div>

          {error && (
            <div className="p-3 rounded-xl bg-red-50 text-red-700 text-sm flex items-center gap-2">
              <IconX/> {error}
            </div>
          )}
        </section>

        {/* 결과 카드 */}
        <section className="bg-white rounded-2xl shadow-sm border p-5 space-y-4">
          <div className="flex items-center justify-between">
            <h2 className="font-semibold">결과</h2>
            {res && (
              <span className="text-xs px-2 py-1 rounded-full bg-gray-100 border">완료</span>
            )}
          </div>

          {!res && !error && (
            <div className="text-gray-500 text-sm">왼쪽에서 텍스트를 입력하거나 파일을 업로드하고 상단 버튼을 눌러주세요.</div>
          )}

          {res && (
            <div className="space-y-4">
              <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
                <div className="p-3 rounded-xl bg-gray-50">
                  <div className="text-xs text-gray-500">제목(10자)</div>
                  <div className="text-lg font-bold leading-tight">{res.title}</div>
                </div>
                <div className="p-3 rounded-xl bg-gray-50">
                  <div className="text-xs text-gray-500">마감</div>
                  <div className="font-medium">{res.due || "-"}</div>
                </div>
                <div className="p-3 rounded-xl bg-gray-50">
                  <div className="text-xs text-gray-500">상태</div>
                  <div className="flex items-center gap-2">{res.notion?.status === "ok" ? (<><IconCheck/> Notion 저장</>) : (<><IconX/> 저장 실패</>)}</div>
                </div>
              </div>

              {/* <div className="p-3 rounded-xl bg-gray-50">
                <div className="text-xs text-gray-500 mb-1">Task</div>
                <div className="font-medium break-words">{res.task}</div>
              </div> */}

              <div className="p-3 rounded-xl bg-gray-50">
                <div className="text-xs text-gray-500 mb-1">Notion</div>
                {res.notion?.status === "ok" ? (
                  <a className="text-blue-600 underline inline-flex items-center gap-1" href={res.notion.url} target="_blank" rel="noreferrer"><IconLink/> 페이지 열기</a>
                ) : (
                  <div className="text-red-700 text-sm">저장 실패: {res.notion?.message || "알 수 없음"}</div>
                )}
              </div>

              {res.ppt && (
                <div className="p-3 rounded-xl bg-gray-50">
                  <div className="text-xs text-gray-500 mb-1">PPT</div>
                  {res.ppt.url || res.ppt.ppt_url ? (
                    <a className="text-blue-600 underline" href={(res.ppt.url || res.ppt.ppt_url)!} target="_blank" rel="noreferrer">다운로드</a>
                  ) : (
                    <div className="text-gray-600 text-sm">생성됨 (직접 경로 확인 필요)</div>
                  )}
                </div>
              )}

              <details className="rounded-xl border p-3">
                <summary className="cursor-pointer font-semibold">생성된 본문(Markdown 원문 보기)</summary>
                <pre className="mt-2 whitespace-pre-wrap break-words text-sm">{res.content_md}</pre>
              </details>
            </div>
          )}
        </section>
      </main>

      {/* 확인 모달 */}
      {confirmOpen && (
        <div className="fixed inset-0 z-50 grid place-items-center bg-black/40 p-4" onClick={()=>setConfirmOpen(false)}>
          <div className="bg-white rounded-2xl shadow-xl w-full max-w-md p-6" onClick={(e)=>e.stopPropagation()}>
            <div className="text-lg font-semibold mb-1">이대로 진행할까요?</div>
            <p className="text-sm text-gray-600 mb-5">Notion 저장은 항상 수행됩니다. {makePpt ? "PPT도 함께 생성합니다." : "PPT는 생성하지 않습니다."}</p>
            <div className="flex items-center justify-between mb-4">
              <Switch checked={makePpt} onChange={setMakePpt} label="PPT 생성" />
            </div>
            <div className="flex justify-end gap-2">
              <button className="px-4 py-2 rounded-xl border" onClick={()=>setConfirmOpen(false)}>취소</button>
              <button className="px-4 py-2 rounded-xl bg-black text-white" onClick={doSubmit}>확인</button>
            </div>
          </div>
        </div>
      )}

      {/* 토스트 */}
      {toast.msg && (
        <div className="fixed bottom-4 right-4 z-50">
          <div className="bg-black text-white text-sm px-4 py-2 rounded-xl shadow">
            {toast.msg}
          </div>
        </div>
      )}

      <footer className="max-w-6xl mx-auto px-6 pb-10 pt-2 text-xs text-gray-500">
        <div className="mt-2">API:.</div>
      </footer>
    </div>
  );
}
