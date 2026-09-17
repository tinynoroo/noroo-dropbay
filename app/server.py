import base64
import binascii
import hmac
import json
import mimetypes
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory, abort

DATA_DIR = Path(os.environ.get("DATA_DIR", "/app/data"))
BLOB_DIR = DATA_DIR / "blobs"
UPLOAD_DIR = DATA_DIR / "uploads"
META_PATH = DATA_DIR / "meta.json"
STATIC_DIR = Path(__file__).parent / "static"

BLOB_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__, static_folder=None)

# ── 접근 제한 ─────────────────────────────────────────────────────────
#
# 토큰이 맞아야만 들어올 수 있다. 토큰은 두 가지로 받는다.
#   ① Authorization: Bearer <토큰>      헤더 입력란이 있는 클라이언트
#   ② /mcp/<토큰>                        주소만 넣을 수 있는 클라이언트
# 연결하는 쪽 UI가 무엇을 지원하든 하나는 통하게 하려는 것이다.
#
# /healthz 만 열어 둔다. 배포 도구가 사이트 생존을 확인하는 주소라
# 여기까지 잠그면 배포가 스스로 실패했다고 판단해 되돌려 버린다.
TOKEN = os.environ.get("UPLOAD_TOKEN", "")
OPEN_PATHS = {"/healthz"}

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


def _is_open_mcp_call():
    """접속 단계(handshake)면 True. tools/call 은 여기서 False 가 된다."""
    if not request.path.startswith("/mcp"):
        return False
    body = request.get_json(silent=True)
    if isinstance(body, dict):
        return body.get("method") in MCP_OPEN_METHODS
    if isinstance(body, list):
        return all(b.get("method") in MCP_OPEN_METHODS for b in body if isinstance(b, dict))
    return False


@app.before_request
def _gate():
    if request.path in OPEN_PATHS:
        return None
    # OAuth 탐색 주소는 401 이 아니라 404 로 답해야 한다. 401 을 주면
    # 클라이언트가 OAuth 서버로 오해하고 등록을 시도하다 실패한다.
    if request.path.startswith("/.well-known/"):
        return jsonify({"error": "not found"}), 404
    if not TOKEN:
        return jsonify({"error": "서버에 토큰이 설정되지 않았습니다."}), 503
    if _is_open_mcp_call():
        return None
    if not _token_ok(_presented_token()):
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

        final_name = _unique_blob_name(session["name"])
        item = {
            "id": final_name,
            "name": final_name,
            "size": session["size"],
            "type": session["type"],
            "uploadedAt": datetime.now(timezone.utc).isoformat(),
        }
        _part_path(upload_id).replace(BLOB_DIR / final_name)
        with _meta_lock:
            items = _load_meta()
            items.append(item)
            _save_meta(items)
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

    final_name = _unique_blob_name(name)
    (BLOB_DIR / final_name).write_bytes(blob)
    item = {
        "id": final_name,
        "name": final_name,
        "size": len(blob),
        "type": mimetypes.guess_type(final_name)[0] or "application/octet-stream",
        "uploadedAt": datetime.now(timezone.utc).isoformat(),
    }
    with _meta_lock:
        items = _load_meta()
        items.append(item)
        _save_meta(items)
    return item


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
        if params.get("name") != "save_image":
            return {"jsonrpc": "2.0", "id": rpc_id,
                    "error": {"code": -32601, "message": "없는 도구입니다."}}
        try:
            item = _save_image(params.get("arguments") or {})
        except ValueError as exc:
            return {"jsonrpc": "2.0", "id": rpc_id,
                    "result": {"isError": True,
                               "content": [{"type": "text", "text": str(exc)}]}}
        result = {"content": [{"type": "text",
                               "text": "저장했습니다: %s (%d bytes)" % (item["name"], item["size"])}]}
    else:
        return {"jsonrpc": "2.0", "id": rpc_id,
                "error": {"code": -32601, "message": "지원하지 않는 method 입니다."}}

    return {"jsonrpc": "2.0", "id": rpc_id, "result": result}


@app.post("/mcp")
@app.post("/mcp/<token>")
def mcp(token=None):
    body = request.get_json(silent=True) or {}
    if isinstance(body, list):                        # 배치 요청
        out = [r for r in (_mcp_dispatch(b) for b in body) if r is not None]
        return jsonify(out) if out else ("", 204)
    reply = _mcp_dispatch(body)
    return ("", 204) if reply is None else jsonify(reply)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, threaded=True)
