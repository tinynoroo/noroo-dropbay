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

_meta_lock = threading.Lock()
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

    upload_id = uuid.uuid4().hex
    _part_path(upload_id).write_bytes(b"")
    _session_path(upload_id).write_text(json.dumps({
        "name": name,
        "size": size,
        "type": mimetype,
        "createdAt": datetime.now(timezone.utc).isoformat(),
    }, ensure_ascii=False))
    return jsonify({"uploadId": upload_id, "receivedBytes": 0}), 201


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

        item = {
            "id": upload_id,
            "name": session["name"],
            "size": session["size"],
            "type": session["type"],
            "uploadedAt": datetime.now(timezone.utc).isoformat(),
        }
        _part_path(upload_id).replace(BLOB_DIR / upload_id)
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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, threaded=True)
