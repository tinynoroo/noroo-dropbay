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
META_PATH = DATA_DIR / "meta.json"
STATIC_DIR = Path(__file__).parent / "static"

BLOB_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__, static_folder=None)

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


def _human_name(name):
    return os.path.basename(name or "file")


@app.get("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.get("/api/files")
def list_files():
    with _lock:
        items = _load_meta()
    items = sorted(items, key=lambda i: i["uploadedAt"], reverse=True)
    return jsonify({"files": items})


@app.post("/api/upload")
def upload():
    uploaded = request.files.getlist("files")
    if not uploaded:
        return jsonify({"error": "업로드할 파일이 없습니다."}), 400

    with _lock:
        items = _load_meta()
        saved = []

        for f in uploaded:
            name = _human_name(f.filename)
            if not name:
                continue
            file_id = uuid.uuid4().hex
            blob_path = BLOB_DIR / file_id
            f.save(blob_path)
            size = blob_path.stat().st_size

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

    return jsonify({"saved": saved}), 201 if saved else 400


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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, threaded=True)
