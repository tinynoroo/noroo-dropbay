import json
import mimetypes
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory, send_file, abort

DATA_DIR = Path(os.environ.get("DATA_DIR", "/app/data"))
BLOB_DIR = DATA_DIR / "blobs"
META_PATH = DATA_DIR / "meta.json"
STATIC_DIR = Path(__file__).parent / "static"

MAX_FILE_MB = int(os.environ.get("MAX_FILE_MB", "200"))
MAX_TOTAL_MB = int(os.environ.get("MAX_TOTAL_MB", "2048"))
MAX_FILE_BYTES = MAX_FILE_MB * 1024 * 1024
MAX_TOTAL_BYTES = MAX_TOTAL_MB * 1024 * 1024

BLOB_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__, static_folder=None)
# 여러 파일을 한 요청으로 올릴 수 있어 개별 파일 상한보다 여유를 둔다.
app.config["MAX_CONTENT_LENGTH"] = MAX_FILE_BYTES * 4

_lock = threading.Lock()


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


def _total_bytes(items):
    return sum(item["size"] for item in items)


def _human_name(name):
    return os.path.basename(name or "file")


@app.get("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.get("/api/config")
def config():
    return jsonify({"maxFileMB": MAX_FILE_MB, "maxTotalMB": MAX_TOTAL_MB})


@app.get("/api/files")
def list_files():
    with _lock:
        items = _load_meta()
    items = sorted(items, key=lambda i: i["uploadedAt"], reverse=True)
    return jsonify({
        "files": items,
        "totalBytes": _total_bytes(items),
        "maxTotalBytes": MAX_TOTAL_BYTES,
    })


@app.post("/api/upload")
def upload():
    uploaded = request.files.getlist("files")
    if not uploaded:
        return jsonify({"error": "업로드할 파일이 없습니다."}), 400

    with _lock:
        items = _load_meta()
        used = _total_bytes(items)
        saved, errors = [], []

        for f in uploaded:
            name = _human_name(f.filename)
            if not name:
                continue
            file_id = uuid.uuid4().hex
            blob_path = BLOB_DIR / file_id
            f.save(blob_path)
            size = blob_path.stat().st_size

            if size > MAX_FILE_BYTES:
                blob_path.unlink(missing_ok=True)
                errors.append("%s: 파일당 최대 %dMB까지 가능합니다." % (name, MAX_FILE_MB))
                continue
            if used + size > MAX_TOTAL_BYTES:
                blob_path.unlink(missing_ok=True)
                errors.append("%s: 저장 공간이 부족합니다." % name)
                continue

            used += size
            item = {
                "id": file_id,
                "name": name,
                "size": size,
                "type": f.mimetype or mimetypes.guess_type(name)[0] or "application/octet-stream",
                "uploadedAt": datetime.now(timezone.utc).isoformat(),
            }
            items.append(item)
            saved.append(item)

        _save_meta(items)

    status = 201 if saved else 400
    return jsonify({"saved": saved, "errors": errors}), status


@app.get("/api/files/<file_id>/download")
def download(file_id):
    with _lock:
        items = _load_meta()
    item = next((i for i in items if i["id"] == file_id), None)
    blob_path = BLOB_DIR / file_id
    if not item or not blob_path.exists():
        abort(404)
    return send_file(blob_path, mimetype=item["type"],
                      as_attachment=True, download_name=item["name"])


@app.delete("/api/files/<file_id>")
def delete(file_id):
    with _lock:
        items = _load_meta()
        remaining = [i for i in items if i["id"] != file_id]
        if len(remaining) == len(items):
            abort(404)
        _save_meta(remaining)
    (BLOB_DIR / file_id).unlink(missing_ok=True)
    return jsonify({"ok": True})


@app.errorhandler(413)
def too_large(_e):
    return jsonify({"error": "요청이 너무 큽니다. 파일당 최대 %dMB까지 가능합니다." % MAX_FILE_MB}), 413


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, threaded=True)
