import base64
import binascii
import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory, abort, redirect
from werkzeug.middleware.proxy_fix import ProxyFix

DATA_DIR = Path(os.environ.get("DATA_DIR", "/app/data"))
BLOB_DIR = DATA_DIR / "blobs"
UPLOAD_DIR = DATA_DIR / "uploads"
TICKET_DIR = DATA_DIR / "tickets"
META_PATH = DATA_DIR / "meta.json"
STATIC_DIR = Path(__file__).parent / "static"

BLOB_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
TICKET_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__, static_folder=None)
# Caddy 뒤에 있으므로 X-Forwarded-Proto/Host 를 믿어야 발급하는 업로드
# 주소가 https://upload.example.com/... 로 제대로 나온다.
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

# ── 접근 제한 ─────────────────────────────────────────────────────────
#
# 토큰이 맞아야만 들어올 수 있다. 토큰은 두 가지로 받는다.
#   ① Authorization: Bearer <토큰>      헤더 입력란이 있는 클라이언트
#   ② /mcp/<토큰>                        주소만 넣을 수 있는 클라이언트
# 연결하는 쪽 UI가 무엇을 지원하든 하나는 통하게 하려는 것이다.
#
# /healthz 는 열어 둔다. 배포 도구가 사이트 생존을 확인하는 주소라
# 여기까지 잠그면 배포가 스스로 실패했다고 판단해 되돌려 버린다.
# 로그인 화면과 1회용 업로드 주소(/api/submit/<티켓>)도 자체 인증을 한다.
#
# 웹 화면은 토큰 대신 비밀번호(UPLOAD_PASSWORD)로 로그인하고, 로그인하면
# 서명된 쿠키를 받는다. 비밀번호가 설정돼 있지 않으면 웹 로그인은 막힌다.
TOKEN = os.environ.get("UPLOAD_TOKEN", "")
PASSWORD = os.environ.get("UPLOAD_PASSWORD", "")
OPEN_PATHS = {"/healthz", "/login", "/logout"}
OPEN_PREFIXES = ("/api/submit/",)
SESSION_COOKIE = "upload_session"
SESSION_TTL = 30 * 24 * 3600

# MCP 클라이언트는 인증 없는 401 을 "이 서버는 OAuth 를 쓴다"는 신호로 읽는다.
# 그래서 /mcp 의 접속 단계(initialize, tools/list)는 토큰 없이 통과시키고,
# 실제로 파일을 쓰는 tools/call 에서만 토큰을 본다. 드러나는 것은 도구 이름과
# 설명뿐이고, 저장된 파일과 업로드 기능은 그대로 잠겨 있다.
MCP_OPEN_METHODS = {"initialize", "notifications/initialized", "initialized", "tools/list"}


def _token_ok(candidate):
    return bool(TOKEN) and bool(candidate) and hmac.compare_digest(candidate, TOKEN)


# 커넥터 UI 마다 넣을 수 있는 헤더 이름이 다르다. Authorization 은 예약돼
# 있어 막히고, 임의 이름은 승인 목록에 없다고 막히는 곳도 있다. 어느 이름이
# 허용되는지 미리 알 수 없으므로 흔히 쓰이는 후보를 모두 받는다.
TOKEN_HEADERS = (
    "X-API-Key",
    "X-Api-Key",
    "Api-Key",
    "X-Auth-Token",
    "X-Access-Token",
    "X-Token",
    "X-Upload-Token",
)


def _presented_token():
    # ① 허용 후보 헤더들
    for name in TOKEN_HEADERS:
        value = request.headers.get(name, "").strip()
        if value:
            return value
    # ② 표준 Bearer (넣을 수 있는 UI 라면 이게 가장 깔끔하다)
    auth = request.headers.get("Authorization", "").strip()
    if auth:
        return auth[7:].strip() if auth.startswith("Bearer ") else auth
    # ③ /mcp/<토큰> — 헤더를 전혀 못 넣는 UI 용
    parts = request.path.strip("/").split("/")
    if len(parts) == 2 and parts[0] == "mcp":
        return parts[1]
    return ""


def _looks_like_mcp(path):
    """ChatGPT 는 /mcp 가 아니라 주소 맨 앞(/)으로 보내기도 한다."""
    return path.startswith("/mcp") or path == "/"


def _is_open_mcp_call():
    """접속 단계(handshake)면 True. tools/call 은 여기서 False 가 된다."""
    # 본문 없는 첫 접속은 /mcp 주소에서만 허용한다.
    # 루트(/)까지 풀어주면 웹 화면(GET /)이 같이 열려 버린다.
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return request.path.startswith("/mcp")
    if not _looks_like_mcp(request.path):
        return False
    body = request.get_json(silent=True)
    if isinstance(body, dict):
        return body.get("method") in MCP_OPEN_METHODS
    if isinstance(body, list):
        return all(b.get("method") in MCP_OPEN_METHODS for b in body if isinstance(b, dict))
    return False


# ── 웹 로그인 세션 ─────────────────────────────────────────────────────
#
# 쿠키 값은 "만료시각.서명". 서명 키에 비밀번호가 섞여 있어서 비밀번호를
# 바꾸면 기존 로그인이 전부 풀린다.

def _session_sig(exp):
    key = hashlib.sha256(("session:%s:%s" % (PASSWORD, TOKEN)).encode()).digest()
    return hmac.new(key, exp.encode(), hashlib.sha256).hexdigest()


def _session_ok():
    if not PASSWORD:
        return False
    exp, _, sig = request.cookies.get(SESSION_COOKIE, "").partition(".")
    if not exp.isdigit() or int(exp) < time.time():
        return False
    return hmac.compare_digest(sig, _session_sig(exp))


@app.get("/login")
def login_page():
    if _session_ok():
        return redirect("/")
    return send_from_directory(STATIC_DIR, "login.html")


@app.post("/login")
def login():
    if not PASSWORD:
        return jsonify({"error": "서버에 비밀번호가 설정되지 않았습니다."}), 503
    given = request.form.get("password", "")
    if not hmac.compare_digest(given.encode(), PASSWORD.encode()):
        time.sleep(1)                     # 무작위 대입을 느리게 만든다
        return redirect("/login?e=1", code=303)
    exp = str(int(time.time()) + SESSION_TTL)
    resp = redirect("/", code=303)
    resp.set_cookie(SESSION_COOKIE, "%s.%s" % (exp, _session_sig(exp)),
                    max_age=SESSION_TTL, httponly=True, samesite="Lax",
                    secure=request.is_secure)
    return resp


@app.get("/logout")
def logout():
    resp = redirect("/login", code=303)
    resp.delete_cookie(SESSION_COOKIE)
    return resp


@app.before_request
def _gate():
    if request.path in OPEN_PATHS or request.path.startswith(OPEN_PREFIXES):
        return None
    # OAuth 탐색 주소는 401 이 아니라 404 로 답해야 한다. 401 을 주면
    # 클라이언트가 OAuth 서버로 오해하고 등록을 시도하다 실패한다.
    if request.path.startswith("/.well-known/"):
        return jsonify({"error": "not found"}), 404
    if _session_ok():
        return None
    # 브라우저로 첫 화면에 들어오면 로그인 화면으로 보낸다.
    if request.method == "GET" and request.path == "/":
        return redirect("/login")
    if not TOKEN:
        return jsonify({"error": "서버에 토큰이 설정되지 않았습니다."}), 503
    if _is_open_mcp_call():
        return None
    if not _token_ok(_presented_token()):
        # 왜 막혔는지 알아야 고칠 수 있다. 본문 전체가 아니라
        # method 이름만 남긴다 (토큰·이미지가 로그에 남지 않도록).
        method = "?"
        body = request.get_json(silent=True)
        if isinstance(body, dict):
            method = body.get("method") or "?"
        elif isinstance(body, list):
            method = ",".join(str(b.get("method")) for b in body if isinstance(b, dict)) or "?"
        app.logger.warning(
            "거절: %s %s method=%s 토큰=%s",
            request.method, request.path, method,
            "있음" if _presented_token() else "없음",
        )
        return jsonify({"error": "인증이 필요합니다."}), 401
    return None


@app.get("/healthz")
def healthz():
    return jsonify({"ok": True})

_meta_lock = threading.Lock()
_init_lock = threading.Lock()
_upload_locks = {}
_upload_locks_guard = threading.Lock()


def _upload_lock(upload_id):
    with _upload_locks_guard:
        lock = _upload_locks.get(upload_id)
        if lock is None:
            lock = threading.Lock()
            _upload_locks[upload_id] = lock
        return lock


def _drop_upload_lock(upload_id):
    with _upload_locks_guard:
        _upload_locks.pop(upload_id, None)


def _load_meta():
    if not META_PATH.exists():
        return []
    try:
        return json.loads(META_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return []


def _save_meta(items):
    tmp = META_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(items, ensure_ascii=False, indent=2))
    tmp.replace(META_PATH)


def _human_name(name):
    return os.path.basename(name or "file")


def _session_path(upload_id):
    return UPLOAD_DIR / ("%s.json" % upload_id)


def _part_path(upload_id):
    return UPLOAD_DIR / ("%s.part" % upload_id)


def _load_session(upload_id):
    path = _session_path(upload_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _part_size(upload_id):
    path = _part_path(upload_id)
    return path.stat().st_size if path.exists() else 0


def _unique_blob_name(name):
    """FTP 등으로 직접 내려받을 걸 고려해 원본 파일명 그대로 저장한다.

    같은 이름이 이미 있으면 'name (2).ext' 식으로 겹치지 않게 붙인다.
    """
    if not (BLOB_DIR / name).exists():
        return name
    stem, suffix = os.path.splitext(name)
    n = 2
    while (BLOB_DIR / ("%s (%d)%s" % (stem, n, suffix))).exists():
        n += 1
    return "%s (%d)%s" % (stem, n, suffix)


def _publish_blob(src, name, mimetype=None):
    """다 받은 파일(src)을 blobs 로 옮기고 목록(meta.json)에 올린다."""
    final_name = _unique_blob_name(name)
    src.replace(BLOB_DIR / final_name)
    item = {
        "id": final_name,
        "name": final_name,
        "size": (BLOB_DIR / final_name).stat().st_size,
        "type": mimetype or mimetypes.guess_type(final_name)[0] or "application/octet-stream",
        "uploadedAt": datetime.now(timezone.utc).isoformat(),
    }
    with _meta_lock:
        items = _load_meta()
        items.append(item)
        _save_meta(items)
    return item


def _find_matching_session(fingerprint):
    """이름+크기+수정시각+앞부분 해시가 모두 같은, 아직 끝나지 않은 업로드를 찾는다.

    새로고침으로 브라우저가 이전 File 객체를 잃어도 사용자가 같은 파일을
    다시 고르면 여기서 알아보고 이어받을 수 있게 한다.
    """
    for path in UPLOAD_DIR.glob("*.json"):
        try:
            session = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if session.get("fingerprint") == fingerprint:
            return path.stem, session
    return None


@app.get("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.post("/")
def index_mcp():
    """루트로 들어온 JSON-RPC 를 MCP 로 처리한다."""
    return mcp()


@app.get("/api/files")
def list_files():
    with _meta_lock:
        items = _load_meta()
    items = sorted(items, key=lambda i: i["uploadedAt"], reverse=True)
    return jsonify({"files": items})


@app.delete("/api/files/<file_id>")
def delete(file_id):
    with _meta_lock:
        items = _load_meta()
        remaining = [i for i in items if i["id"] != file_id]
        if len(remaining) == len(items):
            abort(404)
        _save_meta(remaining)
    (BLOB_DIR / file_id).unlink(missing_ok=True)
    return jsonify({"ok": True})


# ── 재개 가능한(청크) 업로드 ───────────────────────────────────────────
#
# 1) /api/upload/init      로 업로드 하나를 등록하고 uploadId를 받는다.
# 2) /api/upload/<id>/chunk?offset=N 으로 청크를 순서대로 보낸다.
#    네트워크가 끊기거나 오프셋이 어긋나면(409) 서버가 실제 수신 바이트를
#    알려주므로, 클라이언트는 그 지점부터 이어서 보내면 된다.
# 3) 전부 보낸 뒤 /api/upload/<id>/complete 로 마무리한다.
#
# 진행 상태는 디스크(파트 파일 크기 + 세션 JSON)에 있어 서버가 재시작돼도
# 이어받을 수 있다.

@app.post("/api/upload/init")
def upload_init():
    body = request.get_json(silent=True) or {}
    name = _human_name(body.get("name"))
    try:
        size = int(body.get("size"))
    except (TypeError, ValueError):
        size = -1
    if not name or size < 0:
        return jsonify({"error": "잘못된 요청입니다."}), 400
    mimetype = body.get("type") or mimetypes.guess_type(name)[0] or "application/octet-stream"
    fingerprint = {
        "name": name,
        "size": size,
        "lastModified": body.get("lastModified"),
        "prefixHash": body.get("prefixHash"),
    }

    with _init_lock:
        if fingerprint["lastModified"] is not None and fingerprint["prefixHash"]:
            match = _find_matching_session(fingerprint)
            if match:
                upload_id, _session = match
                return jsonify({
                    "uploadId": upload_id,
                    "receivedBytes": _part_size(upload_id),
                    "resumed": True,
                }), 200

        upload_id = uuid.uuid4().hex
        _part_path(upload_id).write_bytes(b"")
        _session_path(upload_id).write_text(json.dumps({
            "name": name,
            "size": size,
            "type": mimetype,
            "fingerprint": fingerprint,
            "createdAt": datetime.now(timezone.utc).isoformat(),
        }, ensure_ascii=False))
    return jsonify({"uploadId": upload_id, "receivedBytes": 0, "resumed": False}), 201


@app.get("/api/upload/<upload_id>")
def upload_status(upload_id):
    session = _load_session(upload_id)
    if not session:
        abort(404)
    return jsonify({"receivedBytes": _part_size(upload_id), "totalBytes": session["size"]})


@app.put("/api/upload/<upload_id>/chunk")
def upload_chunk(upload_id):
    session = _load_session(upload_id)
    if not session:
        abort(404)
    try:
        offset = int(request.args.get("offset", "-1"))
    except ValueError:
        offset = -1

    with _upload_lock(upload_id):
        current = _part_size(upload_id)
        if offset != current:
            return jsonify({"error": "offset 불일치", "receivedBytes": current}), 409

        data = request.get_data()
        if current + len(data) > session["size"]:
            return jsonify({"error": "예상 크기를 초과했습니다.", "receivedBytes": current}), 400

        with open(_part_path(upload_id), "ab") as fh:
            fh.write(data)
        new_size = current + len(data)

    return jsonify({"receivedBytes": new_size})


@app.post("/api/upload/<upload_id>/complete")
def upload_complete(upload_id):
    session = _load_session(upload_id)
    if not session:
        abort(404)

    with _upload_lock(upload_id):
        received = _part_size(upload_id)
        if received != session["size"]:
            return jsonify({"error": "전송이 끝나지 않았습니다.", "receivedBytes": received}), 409

        item = _publish_blob(_part_path(upload_id), session["name"], session["type"])
        _session_path(upload_id).unlink(missing_ok=True)

    _drop_upload_lock(upload_id)
    return jsonify({"saved": item}), 201


@app.delete("/api/upload/<upload_id>")
def upload_cancel(upload_id):
    with _upload_lock(upload_id):
        _part_path(upload_id).unlink(missing_ok=True)
        _session_path(upload_id).unlink(missing_ok=True)
    _drop_upload_lock(upload_id)
    return jsonify({"ok": True})


# ── MCP ───────────────────────────────────────────────────────────────
#
# 도구 하나만 노출한다: save_image(filename, data) — data 는 base64.
# 받은 파일은 기존 업로드와 같은 곳(blobs)에 같은 방식으로 기록한다.

MCP_TOOLS = [{
    "name": "save_image",
    "description": "이미지를 서버에 저장한다. data 는 base64로 인코딩한 파일 내용.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "filename": {"type": "string", "description": "저장할 파일 이름 (예: clinic.png)"},
            "data": {"type": "string", "description": "base64로 인코딩한 이미지"},
        },
        "required": ["filename", "data"],
    },
}, {
    "name": "request_upload",
    "description": (
        "큰 파일(이미지 외 모든 형식)을 올릴 때 쓴다. 파일 내용 대신 1회용 업로드 "
        "주소(submitUrl)를 발급한다. 주소는 10분 뒤 만료되고 한 번만 쓸 수 있다. "
        "발급받은 주소로 파일 원본을 HTTP PUT 으로 보내면 저장된다 "
        "(예: curl -T ./photo.png \"<submitUrl>\"). 직접 HTTP 요청을 보낼 수 없으면 "
        "주소를 사용자에게 알려주고 브라우저로 열어 올리게 한다."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "filename": {"type": "string", "description": "저장할 파일 이름 (예: clinic.png)"},
        },
        "required": ["filename"],
    },
}]


def _save_image(args):
    name = _human_name(args.get("filename"))
    raw = args.get("data") or ""
    if "," in raw[:64] and raw.lstrip().startswith("data:"):
        raw = raw.split(",", 1)[1]          # data:image/png;base64,... 형태도 받는다
    try:
        blob = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("base64 를 해독하지 못했습니다.")
    if not blob:
        raise ValueError("빈 파일입니다.")

    tmp = UPLOAD_DIR / ("%s.part" % uuid.uuid4().hex)
    tmp.write_bytes(blob)
    return _publish_blob(tmp, name)


# ── 1회용 업로드 주소 (Figma MCP 의 upload_assets 와 같은 방식) ─────────
#
# base64 를 도구 인자에 통째로 넣으면 모델 출력 길이에 막혀 큰 파일을
# 못 보낸다. 그래서 MCP 는 주소만 발급하고, 파일은 그 주소로 일반 HTTP
# 요청(curl -T 등)이나 브라우저로 직접 보낸다.
#   - 주소 자체가 인증이다 (추측 불가능한 티켓). 10분 뒤 만료, 한 번만 쓴다.
#   - 티켓은 디스크에 있어 서버가 재시작돼도 유효하다.

TICKET_TTL = 600
_TICKET_RE = re.compile(r"[A-Za-z0-9_-]{32,64}")


def _ticket_path(ticket):
    return TICKET_DIR / ("%s.json" % ticket)


def _load_ticket(ticket):
    if not _TICKET_RE.fullmatch(ticket):
        return None
    try:
        data = json.loads(_ticket_path(ticket).read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if data.get("expiresAt", 0) < time.time():
        _ticket_path(ticket).unlink(missing_ok=True)
        return None
    return data


def _request_upload(args):
    name = _human_name(args.get("filename"))
    now = time.time()
    for path in TICKET_DIR.glob("*.json"):         # 만료된 티켓 정리
        try:
            if json.loads(path.read_text()).get("expiresAt", 0) < now:
                path.unlink(missing_ok=True)
        except (OSError, json.JSONDecodeError):
            path.unlink(missing_ok=True)
    ticket = secrets.token_urlsafe(32)
    _ticket_path(ticket).write_text(json.dumps(
        {"name": name, "expiresAt": now + TICKET_TTL}, ensure_ascii=False))
    url = "%s/api/submit/%s" % (request.host_url.rstrip("/"), ticket)
    return url, name


@app.route("/api/submit/<ticket>", methods=["GET"])
def submit_form(ticket):
    """사람이 주소를 브라우저로 열었을 때 쓰는 최소한의 업로드 화면."""
    info = _load_ticket(ticket)
    if not info:
        return "업로드 주소가 만료됐거나 이미 사용됐습니다.", 404
    return (
        "<!doctype html><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        "<title>업로드</title><body style='font-family:sans-serif;padding:24px'>"
        "<p>저장될 이름: <b>%s</b> (10분 안에 한 번만 쓸 수 있습니다)</p>"
        "<form method=post enctype=multipart/form-data>"
        "<input type=file name=file required> <button>업로드</button></form>"
    ) % _escape(info["name"])


@app.route("/api/submit/<ticket>", methods=["PUT", "POST"])
def submit(ticket):
    if not _load_ticket(ticket):
        return jsonify({"error": "업로드 주소가 만료됐거나 이미 사용됐습니다."}), 404
    # 한 번만 쓰도록 먼저 티켓을 가져간다. rename 은 원자적이라 동시에
    # 두 요청이 와도 하나만 성공한다.
    claimed = TICKET_DIR / ("%s.claimed" % ticket)
    try:
        _ticket_path(ticket).rename(claimed)
    except FileNotFoundError:
        return jsonify({"error": "업로드 주소가 이미 사용됐습니다."}), 404
    info = json.loads(claimed.read_text())

    tmp = UPLOAD_DIR / ("%s.part" % uuid.uuid4().hex)
    form_upload = "file" in request.files
    try:
        if form_upload:
            request.files["file"].save(tmp)
        else:
            with open(tmp, "wb") as fh:
                while True:
                    chunk = request.stream.read(1024 * 1024)
                    if not chunk:
                        break
                    fh.write(chunk)
        if tmp.stat().st_size == 0:
            raise ValueError("빈 파일입니다.")
        item = _publish_blob(tmp, info["name"])
    except Exception as exc:
        # 전송이 끊기는 등 실패하면 아직 만료 전인 티켓은 다시 쓸 수 있게 돌려놓는다.
        tmp.unlink(missing_ok=True)
        claimed.rename(_ticket_path(ticket))
        if isinstance(exc, ValueError):
            return jsonify({"error": str(exc)}), 400
        raise
    claimed.unlink(missing_ok=True)

    if form_upload:
        return "저장했습니다: %s (%d bytes)" % (_escape(item["name"]), item["size"]), 201
    return jsonify({"saved": item}), 201


def _escape(s):
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
             .replace('"', "&quot;").replace("'", "&#39;"))


def _mcp_dispatch(body):
    rpc_id = body.get("id")
    method = body.get("method")

    if method == "initialize":
        result = {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "upload-prj", "version": "1.0.0"},
        }
    elif method in ("notifications/initialized", "initialized"):
        return None                                   # 알림에는 응답하지 않는다
    elif method == "tools/list":
        result = {"tools": MCP_TOOLS}
    elif method == "tools/call":
        if not _token_ok(_presented_token()):
            return {"jsonrpc": "2.0", "id": rpc_id,
                    "error": {"code": -32001, "message": "인증이 필요합니다."}}
        params = body.get("params") or {}
        tool = params.get("name")
        args = params.get("arguments") or {}
        if tool == "save_image":
            try:
                item = _save_image(args)
            except ValueError as exc:
                return {"jsonrpc": "2.0", "id": rpc_id,
                        "result": {"isError": True,
                                   "content": [{"type": "text", "text": str(exc)}]}}
            text = "저장했습니다: %s (%d bytes)" % (item["name"], item["size"])
            result = {"content": [{"type": "text", "text": text}]}
        elif tool == "request_upload":
            url, name = _request_upload(args)
            text = (
                "submitUrl: %s\n"
                "10분 안에 한 번만 쓸 수 있습니다. 파일 원본을 PUT 으로 보내세요:\n"
                "  curl -T <로컬 파일 경로> \"%s\"\n"
                "HTTP 요청을 보낼 수 없다면 이 주소를 사용자에게 주고 브라우저로 열어 "
                "올리게 하세요. 저장될 이름: %s"
            ) % (url, url, name)
            result = {"content": [{"type": "text", "text": text}],
                      "structuredContent": {"submitUrl": url, "filename": name,
                                            "expiresInSeconds": TICKET_TTL}}
        else:
            return {"jsonrpc": "2.0", "id": rpc_id,
                    "error": {"code": -32601, "message": "없는 도구입니다."}}
    else:
        return {"jsonrpc": "2.0", "id": rpc_id,
                "error": {"code": -32601, "message": "지원하지 않는 method 입니다."}}

    return {"jsonrpc": "2.0", "id": rpc_id, "result": result}


@app.route("/mcp", methods=["GET", "HEAD", "POST", "OPTIONS"])
@app.route("/mcp/<token>", methods=["GET", "HEAD", "POST", "OPTIONS"])
def mcp(token=None):
    # 본문 없는 접속 시도에는 살아 있다는 것만 알려준다
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return jsonify({"ok": True, "transport": "http", "server": "upload-prj"})
    body = request.get_json(silent=True) or {}
    if isinstance(body, list):                        # 배치 요청
        out = [r for r in (_mcp_dispatch(b) for b in body) if r is not None]
        return jsonify(out) if out else ("", 204)
    reply = _mcp_dispatch(body)
    return ("", 204) if reply is None else jsonify(reply)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, threaded=True)
