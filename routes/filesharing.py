"""
routes/filesharing.py, File Sharing module for Daily Tasks.
Provides a simple LAN file-sharing system with folder hierarchy,
upload/download, rename, move, recursive delete, and Hosted Folder support
(serving local host directories live and read-only without file copying).
"""

import io
import json
import os
import shutil
import tempfile
import threading
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from flask import Blueprint, Response, jsonify, render_template, request, send_file
from werkzeug.utils import secure_filename

import config

filesharing_bp = Blueprint("filesharing", __name__)

# ── Storage ──────────────────────────────────────────────────────────────
FILESHARE_DIR = config.BASE_DIR / "fileshare_uploads"
FILESHARE_DIR.mkdir(parents=True, exist_ok=True)

_MANIFEST_PATH = FILESHARE_DIR / "_manifest.json"
_manifest_lock = threading.RLock()  # reentrant: handlers hold it around load+save
_corrupt_copied = False
_corrupt_backup_name = ""  # set once corruption is seen; shown to users until restart


# ── Manifest helpers ─────────────────────────────────────────────────────

def _load_manifest() -> Dict[str, Any]:
    """Load the manifest JSON from disk (or return an empty one)."""
    with _manifest_lock:
        return _load_manifest_unlocked()


def _load_manifest_unlocked() -> Dict[str, Any]:
    global _corrupt_copied, _corrupt_backup_name
    if _MANIFEST_PATH.exists():
        try:
            manifest = json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))
            if isinstance(manifest, dict):
                manifest.setdefault("folders", [])
                manifest.setdefault("files", [])
                manifest.setdefault("hosted_folders", [])
                return manifest
            raise ValueError("manifest root is not an object")
        except ValueError as exc:  # JSONDecodeError is a ValueError
            if _corrupt_copied:
                return {"folders": [], "files": [], "hosted_folders": []}
            _corrupt_copied = True
            backup = _MANIFEST_PATH.with_name(
                f"_manifest.corrupt-{time.strftime('%Y%m%d-%H%M%S')}.json"
            )
            try:
                shutil.copy2(_MANIFEST_PATH, backup)
                _corrupt_backup_name = backup.name
            except OSError:
                _corrupt_backup_name = "(backup copy failed)"
            print(f"[filesharing] WARNING: corrupt manifest ({exc}); copied to {backup.name}")
        except OSError:
            pass
    return {"folders": [], "files": [], "hosted_folders": []}


def _save_manifest(manifest: Dict[str, Any]) -> None:
    """Persist the manifest JSON to disk atomically."""
    global _corrupt_copied
    with _manifest_lock:
        _save_manifest_unlocked(manifest)
        _corrupt_copied = False


def _save_manifest_unlocked(manifest: Dict[str, Any]) -> None:
    fd, tmp_path = tempfile.mkstemp(dir=str(FILESHARE_DIR), prefix="_manifest.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(manifest, indent=2, ensure_ascii=False))
        os.replace(tmp_path, _MANIFEST_PATH)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _find_item(manifest: Dict, item_id: str):
    """Find an item (folder or file) by ID. Returns (collection, index) or (None, -1)."""
    for collection_key in ("folders", "files"):
        for idx, item in enumerate(manifest[collection_key]):
            if item["id"] == item_id:
                return manifest[collection_key], idx
    return None, -1


def _get_descendant_ids(manifest: Dict, folder_id: str) -> List[str]:
    """Recursively collect all descendant folder and file IDs under a folder."""
    ids = []
    # Direct child folders
    child_folder_ids = [f["id"] for f in manifest["folders"] if f.get("parent_id") == folder_id]
    for cid in child_folder_ids:
        ids.append(cid)
        ids.extend(_get_descendant_ids(manifest, cid))
    # Direct child files
    for f in manifest["files"]:
        if f.get("parent_id") == folder_id:
            ids.append(f["id"])
    return ids


def _is_ancestor(manifest: Dict, potential_ancestor_id: str, target_id: str) -> bool:
    """Check if potential_ancestor_id is an ancestor of target_id (prevents cyclic moves)."""
    current = target_id
    visited = set()
    while current:
        if current in visited:
            return False  # safety: broken chain
        visited.add(current)
        if current == potential_ancestor_id:
            return True
        # Find parent of current
        parent_id = None
        for f in manifest["folders"]:
            if f["id"] == current:
                parent_id = f.get("parent_id")
                break
        current = parent_id
    return False


# ── Hosted Folder Tree & Streaming Helpers ───────────────────────────────

def _build_hosted_tree(root_path: Path, current_path: Path, rel_prefix: str = "") -> Dict[str, Any]:
    """Walk a hosted directory live on disk, skipping symlinks and returning a nested tree."""
    children = []
    try:
        with os.scandir(current_path) as it:
            entries = []
            for entry in it:
                try:
                    if entry.is_symlink():
                        continue
                    entries.append(entry)
                except OSError:
                    continue

        dirs = sorted([e for e in entries if e.is_dir(follow_symlinks=False)], key=lambda e: e.name.lower())
        files = sorted([e for e in entries if e.is_file(follow_symlinks=False)], key=lambda e: e.name.lower())

        for d in dirs:
            d_rel = f"{rel_prefix}/{d.name}" if rel_prefix else d.name
            child_tree = _build_hosted_tree(root_path, Path(d.path), d_rel)
            children.append(child_tree)

        for f in files:
            f_rel = f"{rel_prefix}/{f.name}" if rel_prefix else f.name
            try:
                st = f.stat(follow_symlinks=False)
                size = st.st_size
                modified = st.st_mtime
            except OSError:
                size = 0
                modified = 0.0
            children.append({
                "type": "file",
                "name": f.name,
                "path": f_rel.replace("\\", "/"),
                "size": size,
                "modified": modified,
            })
    except OSError:
        pass

    return {
        "type": "dir",
        "name": current_path.name if rel_prefix else root_path.name,
        "path": rel_prefix.replace("\\", "/"),
        "children": children,
    }


class ChunkedZipStream(io.RawIOBase):
    """Memory-efficient stream buffer for real-time ZipFile streaming without buffering entire archives."""

    def __init__(self):
        self._buf = bytearray()
        self._pos = 0

    def write(self, b):
        self._buf.extend(b)
        self._pos += len(b)
        return len(b)

    def tell(self):
        return self._pos

    def flush(self):
        pass

    def pop_bytes(self) -> bytes:
        if self._buf:
            data = bytes(self._buf)
            self._buf.clear()
            return data
        return b""


# ── Routes ────────────────────────────────────────────────────────────────

@filesharing_bp.route("/filesharing", methods=["GET"])
@filesharing_bp.route("/filesharing/", methods=["GET"])
def index():
    """Render the File Share UI page."""
    return render_template("filesharing.html", active_page="filesharing")


@filesharing_bp.route("/filesharing/files", methods=["GET"])
def list_files():
    """Return the full manifest (folders + files + live-walked hosted_folders)."""
    manifest = _load_manifest()

    hosted_list = []
    for hf in manifest.get("hosted_folders", []):
        hf_entry = {
            "id": hf["id"],
            "name": hf["name"],
            "path": hf["path"],
            "registered_at": hf.get("registered_at", 0.0),
        }
        try:
            hf_path = Path(hf["path"]).resolve(strict=True)
            if hf_path.is_dir():
                hf_entry["unavailable"] = False
                root_tree = _build_hosted_tree(hf_path, hf_path, "")
                root_tree["name"] = hf["name"]
                hf_entry["tree"] = root_tree
            else:
                hf_entry["unavailable"] = True
                hf_entry["tree"] = None
        except (OSError, ValueError, RuntimeError):
            hf_entry["unavailable"] = True
            hf_entry["tree"] = None
        hosted_list.append(hf_entry)

    response_data = {
        "folders": manifest.get("folders", []),
        "files": manifest.get("files", []),
        "hosted_folders": hosted_list,
    }
    if _corrupt_backup_name:
        response_data["warning"] = (
            "The file-share manifest was corrupt and has been reset. "
            f"Previous records were saved to fileshare_uploads/{_corrupt_backup_name}."
        )
    return jsonify(response_data)


@filesharing_bp.route("/filesharing/folder/create", methods=["POST"])
def create_folder():
    """Create a new virtual folder."""
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Folder name is required"}), 400

    parent_id = data.get("parent_id") or None

    with _manifest_lock:
        manifest = _load_manifest()

        # Validate parent exists if specified
        if parent_id:
            parent_found = any(f["id"] == parent_id for f in manifest["folders"])
            if not parent_found:
                return jsonify({"error": f"Parent folder '{parent_id}' not found"}), 404

        folder = {
            "id": f"fld_{uuid.uuid4().hex[:12]}",
            "name": name,
            "parent_id": parent_id,
            "created_at": time.time(),
        }
        manifest["folders"].append(folder)
        _save_manifest(manifest)

    return jsonify(folder), 201


@filesharing_bp.route("/filesharing/upload", methods=["POST"])
def upload_file():
    """Upload a file to the file share."""
    uploaded = request.files.get("file")
    if not uploaded or not uploaded.filename:
        return jsonify({"error": "No file provided"}), 400

    original_name = uploaded.filename
    safe_name = secure_filename(original_name) or f"upload_{uuid.uuid4().hex[:8]}"

    # Ensure unique disk filename
    disk_filename = f"{uuid.uuid4().hex[:12]}_{safe_name}"
    disk_path = FILESHARE_DIR / disk_filename
    uploaded.save(str(disk_path))

    uploader = request.form.get("uploader", "").strip() or "Anonymous"
    folder_id = request.form.get("folder_id", "").strip() or None

    file_size = disk_path.stat().st_size

    file_record = {
        "id": f"fil_{uuid.uuid4().hex[:12]}",
        "name": original_name,
        "disk_filename": disk_filename,
        "size": file_size,
        "uploader": uploader,
        "parent_id": folder_id if folder_id else None,
        "uploaded_at": time.time(),
    }
    with _manifest_lock:
        manifest = _load_manifest()
        manifest["files"].append(file_record)
        _save_manifest(manifest)

    return jsonify(file_record), 201


@filesharing_bp.route("/filesharing/download/<item_id>", methods=["GET"])
def download_file(item_id: str):
    """Download a file by its ID."""
    manifest = _load_manifest()
    file_rec = next((f for f in manifest["files"] if f["id"] == item_id), None)
    if not file_rec:
        return jsonify({"error": "File not found"}), 404

    disk_path = FILESHARE_DIR / file_rec["disk_filename"]
    if not disk_path.exists():
        return jsonify({"error": "File missing from disk"}), 404

    return send_file(
        str(disk_path),
        as_attachment=True,
        download_name=file_rec["name"],
    )


@filesharing_bp.route("/filesharing/rename/<item_id>", methods=["PATCH"])
def rename_item(item_id: str):
    """Rename a file or folder."""
    data = request.get_json(silent=True) or {}
    new_name = (data.get("name") or "").strip()
    if not new_name:
        return jsonify({"error": "New name is required"}), 400

    with _manifest_lock:
        manifest = _load_manifest()
        collection, idx = _find_item(manifest, item_id)
        if collection is None:
            return jsonify({"error": "Item not found"}), 404

        collection[idx]["name"] = new_name
        _save_manifest(manifest)
        result = dict(collection[idx])

    return jsonify(result)


@filesharing_bp.route("/filesharing/move", methods=["POST"])
def move_item():
    """Move a file or folder to a new parent folder."""
    data = request.get_json(silent=True) or {}
    item_id = data.get("item_id", "").strip()
    new_parent_id = data.get("new_parent_id") or None

    if not item_id:
        return jsonify({"error": "item_id is required"}), 400

    with _manifest_lock:
        manifest = _load_manifest()
        collection, idx = _find_item(manifest, item_id)
        if collection is None:
            return jsonify({"error": "Item not found"}), 404

        # Validate new parent exists
        if new_parent_id:
            parent_found = any(f["id"] == new_parent_id for f in manifest["folders"])
            if not parent_found:
                return jsonify({"error": "Target parent folder not found"}), 404

        # Prevent cyclic move: can't move a folder into one of its own descendants
        is_folder = item_id.startswith("fld_")
        if is_folder and new_parent_id:
            if _is_ancestor(manifest, item_id, new_parent_id):
                return jsonify({"error": "Cannot move folder into its own descendant (cyclic)"}), 400

        collection[idx]["parent_id"] = new_parent_id
        _save_manifest(manifest)
        result = dict(collection[idx])

    return jsonify(result)


@filesharing_bp.route("/filesharing/delete/<item_id>", methods=["DELETE"])
def delete_item(item_id: str):
    """Delete a file or folder (recursive for folders)."""
    with _manifest_lock:
        manifest = _load_manifest()
        collection, idx = _find_item(manifest, item_id)
        if collection is None:
            return jsonify({"error": "Item not found"}), 404

        is_folder = item_id.startswith("fld_")

        if is_folder:
            # Collect all descendant IDs
            descendant_ids = _get_descendant_ids(manifest, item_id)
            all_ids_to_remove = {item_id} | set(descendant_ids)

            # Delete disk files for all files being removed
            for f in manifest["files"]:
                if f["id"] in all_ids_to_remove:
                    disk_path = FILESHARE_DIR / f["disk_filename"]
                    try:
                        disk_path.unlink(missing_ok=True)
                    except Exception:
                        pass

            # Remove from manifest
            manifest["folders"] = [f for f in manifest["folders"] if f["id"] not in all_ids_to_remove]
            manifest["files"] = [f for f in manifest["files"] if f["id"] not in all_ids_to_remove]
        else:
            # Single file deletion
            file_rec = collection[idx]
            disk_path = FILESHARE_DIR / file_rec["disk_filename"]
            try:
                disk_path.unlink(missing_ok=True)
            except Exception:
                pass
            manifest["files"] = [f for f in manifest["files"] if f["id"] != item_id]

        _save_manifest(manifest)
    return jsonify({"deleted": item_id, "ok": True})


# ── Hosted Folder Endpoints ───────────────────────────────────────────────

@filesharing_bp.route("/filesharing/host-folder", methods=["POST"])
def host_folder():
    """Register a real local folder on the host machine for live read-only sharing."""
    data = request.get_json(silent=True) or {}
    raw_path = (data.get("path") or "").strip()
    if not raw_path:
        return jsonify({"error": "Path is required"}), 400

    custom_name = (data.get("name") or "").strip()

    try:
        p = Path(raw_path)
        if not p.exists():
            return jsonify({"error": f"Path does not exist: {raw_path}"}), 404
        if not p.is_dir():
            return jsonify({"error": f"Path is not a directory: {raw_path}"}), 400

        resolved_path = p.resolve(strict=True)
    except (OSError, PermissionError) as exc:
        return jsonify({"error": f"Cannot access path: {exc}"}), 400

    norm_target = os.path.normcase(str(resolved_path))

    with _manifest_lock:
        manifest = _load_manifest()

        for existing in manifest.get("hosted_folders", []):
            try:
                existing_resolved = Path(existing["path"]).resolve()
                if os.path.normcase(str(existing_resolved)) == norm_target:
                    return jsonify({
                        "error": "This folder is already hosted",
                        "existing_id": existing["id"],
                    }), 409
            except Exception:
                if os.path.normcase(str(existing.get("path", ""))) == norm_target:
                    return jsonify({
                        "error": "This folder is already hosted",
                        "existing_id": existing["id"],
                    }), 409

        entry = {
            "id": f"hf_{uuid.uuid4().hex[:12]}",
            "name": custom_name or resolved_path.name or str(resolved_path),
            "path": str(resolved_path),
            "registered_at": time.time(),
        }
        manifest["hosted_folders"].append(entry)
        _save_manifest(manifest)

    return jsonify(entry), 201


@filesharing_bp.route("/filesharing/host-download/<hosted_folder_id>", methods=["GET"])
def host_download_file(hosted_folder_id: str):
    """Download a single file from a hosted folder."""
    manifest = _load_manifest()
    entry = next((hf for hf in manifest.get("hosted_folders", []) if hf["id"] == hosted_folder_id), None)
    if not entry:
        return jsonify({"error": "Hosted folder not found"}), 404

    try:
        root = Path(entry["path"]).resolve(strict=True)
        if not root.is_dir():
            return jsonify({"error": "Hosted folder is no longer available on the host"}), 404
    except (OSError, ValueError, RuntimeError):
        return jsonify({"error": "Hosted folder is no longer available on the host"}), 404

    relative_path = request.args.get("path", "").strip()
    if not relative_path:
        return jsonify({"error": "path is required"}), 400

    try:
        candidate = (root / relative_path).resolve(strict=True)
    except (OSError, ValueError, RuntimeError):
        return jsonify({"error": "File not found"}), 404

    if not (candidate == root or root in candidate.parents):
        return jsonify({"error": "Path escapes hosted folder"}), 403

    if candidate.is_symlink() or not candidate.is_file():
        return jsonify({"error": "File not found"}), 404

    return send_file(
        candidate,
        as_attachment=True,
        download_name=candidate.name,
        conditional=True,
    )


@filesharing_bp.route("/filesharing/host-download-zip/<hosted_folder_id>", methods=["GET"])
def host_download_zip(hosted_folder_id: str):
    """Download a hosted folder (or subfolder) as a dynamically streamed ZIP archive."""
    manifest = _load_manifest()
    entry = next((hf for hf in manifest.get("hosted_folders", []) if hf["id"] == hosted_folder_id), None)
    if not entry:
        return jsonify({"error": "Hosted folder not found"}), 404

    try:
        root = Path(entry["path"]).resolve(strict=True)
        if not root.is_dir():
            return jsonify({"error": "Hosted folder is no longer available on the host"}), 404
    except (OSError, ValueError, RuntimeError):
        return jsonify({"error": "Hosted folder is no longer available on the host"}), 404

    relative_path = request.args.get("path", "").strip()
    try:
        candidate = (root / relative_path).resolve(strict=True) if relative_path else root
    except (OSError, ValueError, RuntimeError):
        return jsonify({"error": "Path not found"}), 404

    if not (candidate == root or root in candidate.parents):
        return jsonify({"error": "Path escapes hosted folder"}), 403

    if not candidate.is_dir():
        return jsonify({"error": "path is not a directory"}), 400

    zip_filename = f"{candidate.name or entry['name']}.zip"

    def generate_zip_chunks():
        buf = ChunkedZipStream()
        with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
            for dirpath_str, dirnames, filenames in os.walk(str(candidate), followlinks=False):
                dirpath = Path(dirpath_str)
                try:
                    resolved_dir = dirpath.resolve(strict=True)
                    if not (resolved_dir == root or root in resolved_dir.parents):
                        continue
                    if resolved_dir.is_symlink():
                        continue
                except (OSError, ValueError, RuntimeError):
                    continue

                dirnames[:] = [d for d in dirnames if not (dirpath / d).is_symlink()]

                for fn in sorted(filenames):
                    file_path = dirpath / fn
                    try:
                        if file_path.is_symlink():
                            continue
                        resolved_file = file_path.resolve(strict=True)
                        if not (resolved_file == root or root in resolved_file.parents):
                            continue
                        if resolved_file.is_symlink() or not resolved_file.is_file():
                            continue

                        arcname = file_path.relative_to(candidate).as_posix()

                        try:
                            st = resolved_file.stat()
                            mtime = time.localtime(st.st_mtime)[:6]
                        except OSError:
                            mtime = (2026, 1, 1, 0, 0, 0)

                        zinfo = zipfile.ZipInfo(filename=arcname, date_time=mtime)
                        zinfo.compress_type = zipfile.ZIP_DEFLATED

                        with zf.open(zinfo, mode="w") as dest:
                            with open(resolved_file, "rb") as src:
                                while True:
                                    chunk = src.read(65536)
                                    if not chunk:
                                        break
                                    dest.write(chunk)
                                    out = buf.pop_bytes()
                                    if out:
                                        yield out
                        out = buf.pop_bytes()
                        if out:
                            yield out
                    except (OSError, PermissionError, ValueError, RuntimeError):
                        continue

        out = buf.pop_bytes()
        if out:
            yield out

    response = Response(generate_zip_chunks(), mimetype="application/zip")
    response.headers["Content-Disposition"] = f'attachment; filename="{zip_filename}"'
    return response


@filesharing_bp.route("/filesharing/host-folder/<hosted_folder_id>", methods=["DELETE"])
def unregister_hosted_folder(hosted_folder_id: str):
    """Unregister a hosted folder. Removes only the manifest entry without touching filesystem."""
    with _manifest_lock:
        manifest = _load_manifest()
        hosted_folders = manifest.get("hosted_folders", [])
        entry_idx = next((i for i, hf in enumerate(hosted_folders) if hf["id"] == hosted_folder_id), -1)
        if entry_idx == -1:
            return jsonify({"error": "Hosted folder not found"}), 404

        manifest["hosted_folders"].pop(entry_idx)
        _save_manifest(manifest)
    return jsonify({"deleted": True, "id": hosted_folder_id}), 200
