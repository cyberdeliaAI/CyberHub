"""Auto Tagger: optional WD-EVA02 visual tagging for the CyberHub Gallery."""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import urllib.request
import warnings
from pathlib import Path

from core import Module
from core.server import build_shell


MODEL_REPO = "SmilingWolf/wd-eva02-large-tagger-v3"
MODEL_REVISION = "b25b82a03f7282e41aa2f257a52c7583b710bd1c"
MODEL_DIR_NAME = "wd-eva02-large-tagger-v3"
MODEL_FILES = ("model.onnx", "selected_tags.csv")
MODEL_MIN_SIZES = {"model.onnx": 50 * 1024 * 1024, "selected_tags.csv": 1024}
MODEL_URLS = {
    name: f"https://huggingface.co/{MODEL_REPO}/resolve/{MODEL_REVISION}/{name}"
    for name in MODEL_FILES
}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}
PRESETS = {
    "More tags": (0.25, 0.65),
    "Balanced": (0.35, 0.85),
    "Strict": (0.50, 0.90),
}


def _safe_float(value, default):
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _format_bytes(value):
    value = int(value or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{value} B"
        value /= 1024.0
    return "0 B"


class AutoTaggerModule(Module):
    name = "Auto Tagger"
    version = "1.0.5"
    icon = "\U0001F3F7"
    description = "Analyze Gallery images locally and make visual AI tags searchable."
    order = 34

    settings_schema = {
        "preset": {
            "type": "select", "label": "Tagging preset", "default": "Balanced",
            "options": ["More tags", "Balanced", "Strict", "Custom"],
            "desc": "Balanced uses the official WD thresholds. More tags is broader; Strict reduces false positives.",
        },
        "general_threshold": {
            "type": "number", "label": "Custom general threshold", "default": 0.35,
            "min": 0.05, "max": 0.95, "step": 0.01,
            "desc": "Used only with the Custom preset.",
        },
        "character_threshold": {
            "type": "number", "label": "Custom character threshold", "default": 0.85,
            "min": 0.05, "max": 0.99, "step": 0.01,
            "desc": "Used only with the Custom preset. Character detection should remain relatively strict.",
        },
        "adaptive_threshold": {
            "type": "bool", "label": "Adaptive MCut thresholds", "default": False,
            "desc": "Choose thresholds per image using the official MCut method.",
        },
        "provider": {
            "type": "select", "label": "Execution provider", "default": "Auto",
            "options": ["Auto", "CPU", "CoreML", "CUDA", "DirectML"],
            "desc": "Auto prefers available hardware acceleration and falls back to CPU. CUDA requires onnxruntime-gpu.",
        },
        "batch_size": {
            "type": "number", "label": "Inference batch size (0 = Auto)", "default": 0,
            "min": 0, "max": 8,
            "desc": "Auto uses 4 on CUDA, 2 on CoreML/DirectML and 1 on CPU. Lower this if GPU memory is limited.",
        },
        "image_source": {
            "type": "select", "label": "Tagging image source",
            "default": "Original (best quality)",
            "options": ["Original (best quality)", "Gallery thumbnail (fast)"],
            "desc": "Cached Gallery thumbnails preserve the full composition and are much faster when originals are on a network drive, with slightly less fine-detail accuracy.",
        },
        "auto_tag_new": {
            "type": "bool", "label": "Automatically tag newly indexed images", "default": False,
            "desc": "Only images added after enabling this option are queued automatically.",
        },
        "large_image_thumbnail": {
            "type": "bool", "label": "Use thumbnail for extremely large images", "default": True,
            "desc": "Avoid excessive memory use above 100 megapixels. Normal images always use the original.",
        },
    }

    def __init__(self, hub):
        super().__init__(hub)
        self.gallery = None
        self.model_dir = Path(hub.resource_path("auto_tagger", MODEL_DIR_NAME))
        self._tagger = None
        self._tagger_lock = threading.Lock()
        self._inference_lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._download_thread = None
        self._download_cancel = threading.Event()
        self._download = {
            "active": False, "file": "", "done": 0, "total": 0,
            "file_done": 0, "file_total": 0, "error": "", "cancelled": False,
        }
        self._job_thread = None
        self._job_pause = threading.Event()
        self._job_cancel = threading.Event()
        self._job_paths = []
        self._job = self._empty_job()
        self._auto_monitor_thread = None
        self._stop = threading.Event()

    @staticmethod
    def _empty_job():
        return {
            "id": "", "active": False, "state": "idle", "scope": "",
            "label": "", "folder": "", "done": 0, "total": 0,
            "failed": 0, "started": 0.0, "finished": 0.0,
            "current": "", "error": "", "device": "", "used_thumbnail": 0,
            "batch_size": 0, "rate": 0.0, "rate_batches": 0,
            "source": "original",
            "cursor_rowid": 0, "ceiling_rowid": 0,
        }

    # ------------------------------------------------------------------ lifecycle
    def on_startup(self):
        self.gallery = self.hub.registry.get("gallery")
        self.model_dir.mkdir(parents=True, exist_ok=True)
        if not self.gallery or not getattr(self.gallery, "db", None):
            print("[AUTO TAGGER] Gallery is unavailable; module remains idle")
            return
        self._create_tables()
        self.gallery.db.auto_tagger_enabled = True
        self._cleanup_stale_rows()
        self._ensure_auto_new_baseline()
        self._auto_monitor_thread = threading.Thread(
            target=self._auto_monitor, daemon=True, name="auto-tagger-monitor"
        )
        self._auto_monitor_thread.start()

    def on_settings_changed(self, key, value):
        if key in {"provider"}:
            self._unload_tagger()
        if key == "auto_tag_new" and value:
            self._set_config("auto_new_after_rowid", str(self._max_gallery_rowid()))

    # ------------------------------------------------------------------ routes
    def routes_get(self):
        return {
            "/auto_tagger": self._page,
            "/auto-tagger": self._page,
            "/api/auto_tagger/status": self._api_status,
            "/api/auto_tagger/folders": self._api_folders,
            "/api/auto_tagger/tags": self._api_tags,
            "/api/auto_tagger/suggest": self._api_suggest,
            "/api/auto_tagger/files": self._api_files,
        }

    def routes_post(self):
        return {
            "/api/auto_tagger/install": self._api_install,
            "/api/auto_tagger/cancel_download": self._api_cancel_download,
            "/api/auto_tagger/remove_model": self._api_remove_model,
            "/api/auto_tagger/job": self._api_job,
            "/api/auto_tagger/tag-image": self._api_tag_image,
        }

    def _page(self, handler, qs):
        handler.respond_html(build_shell(
            self.hub.registry, self.hub.settings,
            active_key=self.key(), page_title="Auto Tagger", body_html=PAGE_BODY,
        ))

    # ------------------------------------------------------------------ database
    def _db(self):
        if not self.gallery or not getattr(self.gallery, "db", None):
            return None
        return self.gallery.db._get_conn()

    def _create_tables(self):
        conn = self._db()
        if conn is None:
            return
        with self.gallery.db.lock:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS auto_tagger_files (
                    file_path TEXT PRIMARY KEY,
                    source_mtime REAL DEFAULT 0,
                    model_revision TEXT DEFAULT '',
                    status INTEGER DEFAULT 0,
                    tagged_at REAL DEFAULT 0,
                    error TEXT DEFAULT '',
                    used_thumbnail INTEGER DEFAULT 0,
                    last_job TEXT DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS auto_tagger_scores (
                    file_path TEXT,
                    tag_name TEXT,
                    category TEXT,
                    score REAL,
                    PRIMARY KEY (file_path, tag_name, category)
                );
                CREATE TABLE IF NOT EXISTS auto_tagger_config (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );
                CREATE TABLE IF NOT EXISTS auto_tagger_catalog (
                    tag_name TEXT,
                    category TEXT,
                    use_count INTEGER DEFAULT 0,
                    score_sum REAL DEFAULT 0,
                    PRIMARY KEY (tag_name, category)
                );
                CREATE INDEX IF NOT EXISTS idx_auto_tagger_status
                    ON auto_tagger_files(status, model_revision);
                CREATE INDEX IF NOT EXISTS idx_auto_tagger_scores_tag
                    ON auto_tagger_scores(tag_name, category, score);
                CREATE INDEX IF NOT EXISTS idx_auto_tagger_scores_match
                    ON auto_tagger_scores(tag_name, file_path);
                CREATE INDEX IF NOT EXISTS idx_auto_tagger_scores_file
                    ON auto_tagger_scores(file_path, category, score);
                CREATE INDEX IF NOT EXISTS idx_auto_tagger_catalog_popular
                    ON auto_tagger_catalog(use_count DESC, tag_name);
                CREATE TRIGGER IF NOT EXISTS auto_tagger_catalog_insert
                AFTER INSERT ON auto_tagger_scores BEGIN
                    INSERT INTO auto_tagger_catalog(
                        tag_name,category,use_count,score_sum
                    ) VALUES (NEW.tag_name,NEW.category,1,NEW.score)
                    ON CONFLICT(tag_name,category) DO UPDATE SET
                        use_count=use_count+1,
                        score_sum=score_sum+NEW.score;
                END;
                CREATE TRIGGER IF NOT EXISTS auto_tagger_catalog_delete
                AFTER DELETE ON auto_tagger_scores BEGIN
                    UPDATE auto_tagger_catalog SET
                        use_count=use_count-1,
                        score_sum=score_sum-OLD.score
                    WHERE tag_name=OLD.tag_name AND category=OLD.category;
                    DELETE FROM auto_tagger_catalog
                    WHERE tag_name=OLD.tag_name AND category=OLD.category
                      AND use_count<=0;
                END;
                CREATE TRIGGER IF NOT EXISTS auto_tagger_cleanup_file
                AFTER DELETE ON files BEGIN
                    DELETE FROM auto_tagger_scores WHERE file_path=OLD.path;
                    DELETE FROM auto_tagger_files WHERE file_path=OLD.path;
                END;
            """)
            catalog_count = conn.execute(
                "SELECT COUNT(*) FROM auto_tagger_catalog"
            ).fetchone()[0]
            score_count = conn.execute(
                "SELECT COUNT(*) FROM auto_tagger_scores"
            ).fetchone()[0]
            if score_count and not catalog_count:
                conn.execute("""
                    INSERT INTO auto_tagger_catalog(
                        tag_name,category,use_count,score_sum
                    )
                    SELECT tag_name,category,COUNT(*),SUM(score)
                    FROM auto_tagger_scores GROUP BY tag_name,category
                """)
            conn.commit()

    def _cleanup_stale_rows(self):
        conn = self._db()
        if conn is None:
            return
        with self.gallery.db.lock:
            conn.execute("DELETE FROM auto_tagger_scores WHERE file_path NOT IN (SELECT path FROM files)")
            conn.execute("DELETE FROM auto_tagger_files WHERE file_path NOT IN (SELECT path FROM files)")
            conn.commit()

    def _set_config(self, key, value):
        conn = self._db()
        if conn is None:
            return
        with self.gallery.db.lock:
            conn.execute(
                "INSERT OR REPLACE INTO auto_tagger_config(key,value) VALUES (?,?)",
                (str(key), str(value)),
            )
            conn.commit()

    def _get_config(self, key, default=""):
        conn = self._db()
        if conn is None:
            return default
        with self.gallery.db.lock:
            row = conn.execute(
                "SELECT value FROM auto_tagger_config WHERE key=?", (str(key),)
            ).fetchone()
        return row[0] if row else default

    def _max_gallery_rowid(self):
        conn = self._db()
        if conn is None:
            return 0
        with self.gallery.db.lock:
            row = conn.execute("SELECT COALESCE(MAX(rowid),0) FROM files").fetchone()
        return int(row[0] or 0)

    def _ensure_auto_new_baseline(self):
        if not self._get_config("auto_new_after_rowid", ""):
            self._set_config("auto_new_after_rowid", str(self._max_gallery_rowid()))

    # ------------------------------------------------------------------ model
    def _model_file_ready(self, name):
        path = self.model_dir / name
        try:
            return path.is_file() and path.stat().st_size >= MODEL_MIN_SIZES[name]
        except OSError:
            return False

    def _model_ready(self):
        return all(self._model_file_ready(name) for name in MODEL_FILES)

    def _runtime_status(self):
        missing = []
        providers = []
        try:
            import numpy  # noqa: F401
        except Exception:
            missing.append("numpy")
        try:
            from PIL import Image  # noqa: F401
        except Exception:
            missing.append("Pillow")
        try:
            from .tagger import available_providers
            providers = available_providers()
            if not providers:
                missing.append("onnxruntime")
        except Exception:
            missing.append("onnxruntime")
        relevant = [
            provider for provider in providers
            if provider in {
                "CUDAExecutionProvider", "CoreMLExecutionProvider",
                "DmlExecutionProvider", "CPUExecutionProvider",
            }
        ]
        requested = str(self.setting("provider", "Auto") or "Auto")
        provider_names = {
            "CUDA": "CUDAExecutionProvider",
            "CoreML": "CoreMLExecutionProvider",
            "DirectML": "DmlExecutionProvider",
            "CPU": "CPUExecutionProvider",
        }
        labels = {
            "CUDAExecutionProvider": "CUDA",
            "CoreMLExecutionProvider": "CoreML",
            "DmlExecutionProvider": "DirectML",
            "CPUExecutionProvider": "CPU",
        }
        preferred = provider_names.get(requested)
        if requested == "Auto":
            preferred = next((name for name in (
                "CUDAExecutionProvider", "CoreMLExecutionProvider",
                "DmlExecutionProvider", "CPUExecutionProvider",
            ) if name in relevant), "")
        fallback = bool(preferred and preferred not in relevant)
        active = preferred if not fallback else (
            "CPUExecutionProvider" if "CPUExecutionProvider" in relevant else ""
        )
        loaded = getattr(self._tagger, "device", "") if self._tagger is not None else ""
        if loaded in relevant:
            active = loaded
            fallback = bool(requested != "Auto" and preferred and loaded != preferred)
        if missing:
            message = "Missing " + ", ".join(sorted(set(missing)))
        elif fallback:
            message = f"{requested} unavailable; using {labels.get(active, 'CPU')}"
        else:
            message = labels.get(active, requested or "CPU")
        return {
            "ready": not missing, "missing": sorted(set(missing)),
            "providers": relevant, "requested": requested,
            "active": active, "fallback": fallback, "message": message,
        }

    def _thresholds(self):
        preset = str(self.setting("preset", "Balanced") or "Balanced")
        if preset in PRESETS:
            general, character = PRESETS[preset]
        else:
            general = _safe_float(self.setting("general_threshold", 0.35), 0.35)
            character = _safe_float(self.setting("character_threshold", 0.85), 0.85)
        return {
            "preset": preset,
            "general": max(0.01, min(general, 0.99)),
            "character": max(0.01, min(character, 0.99)),
            "adaptive": bool(self.setting("adaptive_threshold", False)),
        }

    def _tag_revision(self):
        thresholds = self._thresholds()
        signature = json.dumps({
            "general": thresholds["general"],
            "character": thresholds["character"],
            "adaptive": thresholds["adaptive"],
        }, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(signature.encode("utf-8")).hexdigest()[:12]
        return f"{MODEL_REVISION}:{digest}"

    def _get_tagger(self):
        if self._tagger is not None:
            return self._tagger
        with self._tagger_lock:
            if self._tagger is None:
                from .tagger import WDTagger
                tagger = WDTagger(
                    self.model_dir,
                    provider=str(self.setting("provider", "Auto") or "Auto"),
                ).load()
                self._tagger = tagger
        return self._tagger

    def _unload_tagger(self):
        with self._tagger_lock:
            if self._tagger is not None:
                try:
                    self._tagger.unload()
                except Exception:
                    pass
            self._tagger = None

    # ------------------------------------------------------------------ download
    def _start_download(self):
        with self._state_lock:
            if self._download["active"]:
                return False
            self._download.update({
                "active": True, "file": "", "done": 0, "total": 0,
                "file_done": 0, "file_total": 0, "error": "", "cancelled": False,
            })
            self._download_cancel.clear()
        self._download_thread = threading.Thread(
            target=self._download_worker, daemon=True, name="auto-tagger-download"
        )
        self._download_thread.start()
        return True

    def _probe_size(self, url):
        try:
            request = urllib.request.Request(
                url, method="HEAD", headers={"User-Agent": "CyberHub-AutoTagger/1.0"}
            )
            with urllib.request.urlopen(request, timeout=30) as response:
                return int(response.headers.get("Content-Length") or 0)
        except Exception:
            return 0

    def _download_one(self, name, url):
        destination = self.model_dir / name
        partial = self.model_dir / (name + ".part")
        expected = self._probe_size(url)
        existing = partial.stat().st_size if partial.exists() else 0
        if expected and existing == expected:
            if existing < MODEL_MIN_SIZES[name]:
                raise IOError(f"Partial {name} is unexpectedly small")
            os.replace(partial, destination)
            return
        headers = {"User-Agent": "CyberHub-AutoTagger/1.0"}
        if existing:
            headers["Range"] = f"bytes={existing}-"
        request = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(request, timeout=60) as response:
            partial_response = getattr(response, "status", 200) == 206
            if existing and not partial_response:
                existing = 0
            length = int(response.headers.get("Content-Length") or 0)
            total = expected or (existing + length if partial_response else length)
            mode = "ab" if partial_response and existing else "wb"
            with self._state_lock:
                self._download.update({
                    "file": name, "file_done": existing, "file_total": total,
                })
            with partial.open(mode) as output:
                while True:
                    if self._download_cancel.is_set():
                        raise InterruptedError("Model download cancelled")
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    output.write(chunk)
                    with self._state_lock:
                        self._download["file_done"] += len(chunk)
                        self._download["done"] += len(chunk)
        if total and partial.stat().st_size != total:
            raise IOError(
                f"Incomplete {name}: {_format_bytes(partial.stat().st_size)} of {_format_bytes(total)}"
            )
        if partial.stat().st_size < MODEL_MIN_SIZES[name]:
            raise IOError(f"Downloaded {name} is unexpectedly small")
        os.replace(partial, destination)

    def _download_worker(self):
        try:
            self.model_dir.mkdir(parents=True, exist_ok=True)
            missing = [name for name in MODEL_FILES if not self._model_file_ready(name)]
            sizes = {name: self._probe_size(MODEL_URLS[name]) for name in missing}
            with self._state_lock:
                self._download["total"] = sum(sizes.values())
            for name in missing:
                self._download_one(name, MODEL_URLS[name])
            with self._state_lock:
                self._download.update({"active": False, "file": "", "error": ""})
        except InterruptedError:
            with self._state_lock:
                self._download.update({
                    "active": False, "file": "", "error": "", "cancelled": True,
                })
        except Exception as exc:
            with self._state_lock:
                self._download.update({"active": False, "error": str(exc)[:500]})

    # ------------------------------------------------------------------ jobs
    @staticmethod
    def _folder_descendant_pattern(folder):
        escaped = str(folder or "").replace("\\", "\\\\")
        escaped = escaped.replace("%", "\\%").replace("_", "\\_")
        return escaped + "/%"

    def _scope_condition(self, scope, folder="", new_after=0, ceiling_rowid=0):
        clauses = []
        params = []
        if scope == "folder" and folder:
            clauses.append("(f.folder=? OR f.folder LIKE ? ESCAPE '\\')")
            params.extend([folder, self._folder_descendant_pattern(folder)])
        if scope == "new":
            clauses.append("f.rowid>?")
            params.append(int(new_after or 0))
        if ceiling_rowid:
            clauses.append("f.rowid<=?")
            params.append(int(ceiling_rowid))
        return clauses, params

    def _count_scope(
        self, scope, folder, retag, job_id, paths=None, new_after=0,
        ceiling_rowid=0,
    ):
        conn = self._db()
        if conn is None:
            return 0
        if scope == "paths":
            return len(paths or [])
        clauses, params = self._scope_condition(
            scope, folder, new_after, ceiling_rowid
        )
        if not retag:
            clauses.append(
                "(a.file_path IS NULL OR a.status!=1 OR a.source_mtime!=f.mtime OR a.model_revision!=?)"
            )
            params.append(self._tag_revision())
        clauses.append("COALESCE(a.last_job,'')!=?")
        params.append(job_id)
        where = " AND ".join(clauses) if clauses else "1=1"
        with self.gallery.db.lock:
            row = conn.execute(f"""
                SELECT COUNT(*) FROM files f
                LEFT JOIN auto_tagger_files a ON a.file_path=f.path
                WHERE {where}
            """, params).fetchone()
        return int(row[0] or 0)

    def _next_scope_rows(self, limit=8):
        conn = self._db()
        if conn is None:
            return []
        with self._state_lock:
            job = dict(self._job)
        clauses, params = self._scope_condition(
            job["scope"], job.get("folder", ""), job.get("new_after", 0),
            job.get("ceiling_rowid", 0),
        )
        if job.get("cursor_rowid"):
            clauses.append("f.rowid<?")
            params.append(int(job["cursor_rowid"]))
        if not job.get("retag"):
            clauses.append(
                "(a.file_path IS NULL OR a.status!=1 OR a.source_mtime!=f.mtime OR a.model_revision!=?)"
            )
            params.append(self._tag_revision())
        clauses.append("COALESCE(a.last_job,'')!=?")
        params.append(job["id"])
        where = " AND ".join(clauses) if clauses else "1=1"
        with self.gallery.db.lock:
            rows = conn.execute(f"""
                SELECT f.rowid, f.path, f.mtime FROM files f
                LEFT JOIN auto_tagger_files a ON a.file_path=f.path
                WHERE {where}
                ORDER BY f.rowid DESC LIMIT ?
            """, params + [int(limit)]).fetchall()
        if rows:
            with self._state_lock:
                if self._job.get("id") == job.get("id"):
                    self._job["cursor_rowid"] = int(rows[-1][0])
        return [(row[1], float(row[2] or 0)) for row in rows]

    def _mark_failure(self, file_path, mtime, job_id, error):
        conn = self._db()
        with self.gallery.db.lock:
            conn.execute("""
                INSERT INTO auto_tagger_files(
                    file_path,source_mtime,model_revision,status,tagged_at,error,last_job
                ) VALUES (?,?,?,2,?,?,?)
                ON CONFLICT(file_path) DO UPDATE SET
                    source_mtime=excluded.source_mtime,
                    model_revision=excluded.model_revision,
                    status=2,tagged_at=excluded.tagged_at,
                    error=excluded.error,last_job=excluded.last_job
            """, (
                file_path, mtime, self._tag_revision(), time.time(),
                str(error)[:500], job_id,
            ))
            conn.commit()

    def _store_result(self, file_path, mtime, job_id, result, used_thumbnail=False):
        self._store_results([(file_path, mtime, result, used_thumbnail)], job_id)

    def _store_results(self, items, job_id):
        if not items:
            return
        conn = self._db()
        revision = self._tag_revision()
        tagged_at = time.time()
        with self.gallery.db.lock:
            for file_path, mtime, result, used_thumbnail in items:
                conn.execute("DELETE FROM auto_tagger_scores WHERE file_path=?", (file_path,))
                rows = [
                    (file_path, tag.name, tag.category, float(tag.score))
                    for tag in result.all
                ]
                conn.executemany("""
                    INSERT OR REPLACE INTO auto_tagger_scores(
                        file_path,tag_name,category,score
                    ) VALUES (?,?,?,?)
                """, rows)
                conn.execute("""
                    INSERT INTO auto_tagger_files(
                        file_path,source_mtime,model_revision,status,tagged_at,
                        error,used_thumbnail,last_job
                    ) VALUES (?,?,?,1,?,'',?,?)
                    ON CONFLICT(file_path) DO UPDATE SET
                        source_mtime=excluded.source_mtime,
                        model_revision=excluded.model_revision,status=1,
                        tagged_at=excluded.tagged_at,error='',
                        used_thumbnail=excluded.used_thumbnail,last_job=excluded.last_job
                """, (
                    file_path, mtime, revision, tagged_at,
                    1 if used_thumbnail else 0, job_id,
                ))
            conn.commit()

    @staticmethod
    def _downsample_source(image, target_size):
        from PIL import Image, ImageOps

        target_size = max(32, int(target_size or 448))
        work = ImageOps.exif_transpose(image)
        try:
            if max(work.size) > target_size:
                resampling = getattr(Image, "Resampling", Image).LANCZOS
                try:
                    work.thumbnail(
                        (target_size, target_size), resampling, reducing_gap=3.0
                    )
                except TypeError:
                    work.thumbnail((target_size, target_size), resampling)
            return work.convert("RGBA").copy()
        finally:
            if work is not image:
                try:
                    work.close()
                except Exception:
                    pass

    @staticmethod
    def _thumbnail_is_current(thumb, absolute):
        try:
            return (
                os.path.isfile(thumb)
                and os.path.getmtime(thumb) >= os.path.getmtime(absolute)
            )
        except OSError:
            return False

    def _load_image(self, file_path, target_size=448):
        from PIL import Image

        absolute = self.gallery.db.resolve_path(file_path)
        if not absolute or not os.path.isfile(absolute):
            raise FileNotFoundError(file_path)
        extension = os.path.splitext(absolute)[1].lower()
        if extension not in IMAGE_EXTS:
            raise ValueError(f"Unsupported image type: {extension or 'unknown'}")

        digest = hashlib.md5(file_path.encode("utf-8")).hexdigest()
        thumb = os.path.join(self.gallery.thumb_dir, digest[:2], digest + ".webp")
        use_gallery_thumb = str(
            self.setting("image_source", "Original (best quality)") or ""
        ).startswith("Gallery thumbnail")
        if use_gallery_thumb and self._thumbnail_is_current(thumb, absolute):
            with Image.open(thumb) as cached:
                return self._downsample_source(cached, target_size), True

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", getattr(Image, "DecompressionBombWarning", Warning))
            with Image.open(absolute) as probe:
                width, height = probe.size
                huge = width * height > 100_000_000
                if huge and self.setting("large_image_thumbnail", True):
                    if self._thumbnail_is_current(thumb, absolute):
                        with Image.open(thumb) as cached:
                            return self._downsample_source(cached, target_size), True
                try:
                    if (probe.format or "").upper() in {"JPEG", "MPO"}:
                        probe.draft("RGB", (target_size, target_size))
                except Exception:
                    pass
                try:
                    probe.seek(0)
                except Exception:
                    pass
                image = self._downsample_source(probe, target_size)
        return image, False

    def _process_rows(self, rows):
        if not rows:
            return True
        batch_started = time.monotonic()
        with self._state_lock:
            job_id = self._job["id"]
            start_done = int(self._job.get("done", 0))
        tagger = self._get_tagger()
        thresholds = self._thresholds()
        images = []
        good_rows = []
        try:
            for file_path, mtime in rows:
                if self._job_cancel.is_set():
                    break
                with self._state_lock:
                    self._job["current"] = file_path
                try:
                    image, used_thumbnail = self._load_image(
                        file_path, tagger.target_size
                    )
                    images.append(image)
                    good_rows.append((file_path, mtime, used_thumbnail))
                except Exception as exc:
                    self._mark_failure(file_path, mtime, job_id, exc)
                    self._advance_job(failed=True)
            if not images:
                return True

            batch_supported = True
            stored = []
            with self._inference_lock:
                try:
                    results = tagger.tag_images(
                        images,
                        general_threshold=thresholds["general"],
                        character_threshold=thresholds["character"],
                        adaptive_threshold=thresholds["adaptive"],
                    )
                    stored = [
                        (row[0], row[1], result, row[2])
                        for row, result in zip(good_rows, results)
                    ]
                except Exception as batch_error:
                    # Fall back once when the provider/model exposes a fixed batch size.
                    # The worker then switches subsequent work to batches of one.
                    batch_supported = len(images) <= 1
                    for image, row in zip(images, good_rows):
                        file_path, mtime, used_thumbnail = row
                        try:
                            result = tagger.tag_image(
                                image,
                                general_threshold=thresholds["general"],
                                character_threshold=thresholds["character"],
                                adaptive_threshold=thresholds["adaptive"],
                            )
                            stored.append((file_path, mtime, result, used_thumbnail))
                        except Exception as exc:
                            self._mark_failure(file_path, mtime, job_id, exc or batch_error)
                            self._advance_job(failed=True)
            self._store_results(stored, job_id)
            for _file_path, _mtime, _result, used_thumbnail in stored:
                self._advance_job(used_thumbnail=used_thumbnail)
            return batch_supported
        finally:
            for image in images:
                try:
                    image.close()
                except Exception:
                    pass
            self._record_batch_rate(start_done, batch_started)

    def _advance_job(self, failed=False, used_thumbnail=False):
        with self._state_lock:
            self._job["done"] += 1
            if failed:
                self._job["failed"] += 1
            if used_thumbnail:
                self._job["used_thumbnail"] += 1

    def _record_batch_rate(self, start_done, batch_started):
        elapsed = max(0.001, time.monotonic() - batch_started)
        with self._state_lock:
            completed = int(self._job.get("done", 0)) - int(start_done)
            if completed <= 0:
                return
            self._job["rate_batches"] = int(self._job.get("rate_batches", 0)) + 1
            # CUDA/CoreML performs one-time graph and kernel work on its first run.
            if self._job["rate_batches"] == 1:
                return
            sample = completed / elapsed
            previous = float(self._job.get("rate", 0.0) or 0.0)
            self._job["rate"] = sample if previous <= 0 else previous * 0.65 + sample * 0.35

    def _effective_batch_size(self, device):
        try:
            configured = int(self.setting("batch_size", 0) or 0)
        except (TypeError, ValueError):
            configured = 0
        if configured > 0:
            return max(1, min(8, configured))
        if device == "CUDAExecutionProvider":
            return 4
        if device in {"CoreMLExecutionProvider", "DmlExecutionProvider"}:
            return 2
        return 1

    def _start_job(self, scope, folder="", paths=None, retag=False, label="", new_after=0):
        runtime = self._runtime_status()
        if not self.gallery or not getattr(self.gallery, "db", None):
            return False, "Gallery is not available"
        if not self._model_ready():
            return False, "Install the Auto Tagger model first"
        if not runtime["ready"]:
            return False, "Missing dependencies: " + ", ".join(runtime["missing"])
        with self._state_lock:
            if self._job["active"]:
                return False, "A tagging job is already running"
            job_id = f"{int(time.time() * 1000)}-{threading.get_ident()}"
            ceiling_rowid = self._max_gallery_rowid()
            clean_paths = []
            if scope == "paths":
                seen = set()
                for path in paths or []:
                    path = str(path or "").strip()
                    if path and path not in seen and self.gallery.db.resolve_path(path):
                        clean_paths.append(path)
                        seen.add(path)
            total = self._count_scope(
                scope, folder, bool(retag), job_id,
                paths=clean_paths, new_after=new_after,
                ceiling_rowid=ceiling_rowid,
            )
            if total <= 0:
                return False, "No images need tagging in this scope"
            self._job_paths = clean_paths
            self._job = {
                **self._empty_job(),
                "id": job_id, "active": True, "state": "running",
                "scope": scope, "label": label or "Tagging images",
                "folder": folder, "total": total, "started": time.time(),
                "retag": bool(retag), "new_after": int(new_after or 0),
                "cursor_rowid": ceiling_rowid + 1,
                "ceiling_rowid": ceiling_rowid,
            }
            self._job_pause.clear()
            self._job_cancel.clear()
        self._job_thread = threading.Thread(
            target=self._job_worker, daemon=True, name="auto-tagger-job"
        )
        self._job_thread.start()
        return True, "Tagging started"

    def _job_worker(self):
        try:
            with self._state_lock:
                self._job["current"] = "Loading model and execution provider..."
            tagger = self._get_tagger()
            expected_device = tagger.device
            source = (
                "thumbnail"
                if str(self.setting("image_source", "") or "").startswith("Gallery thumbnail")
                else "original"
            )
            with self._state_lock:
                self._job["device"] = expected_device
                self._job["source"] = source
                label = expected_device.replace("ExecutionProvider", "") or "inference"
                self._job["current"] = f"Warming up {label}..."
                scope = self._job["scope"]
            try:
                with self._inference_lock:
                    tagger.warmup()
            except Exception as exc:
                if expected_device == "CUDAExecutionProvider":
                    raise RuntimeError(
                        "CUDA warm-up failed. The CUDA/cuDNN runtime is incomplete or its "
                        "DLLs are not visible. Reinstall nvidia-cudnn-cu12 and restart "
                        f"CyberHub. Details: {str(exc)[:240]}"
                    ) from exc
                raise
            if (
                expected_device == "CUDAExecutionProvider"
                and tagger.device != "CUDAExecutionProvider"
            ):
                raise RuntimeError(
                    "CUDA warm-up fell back to CPU. Reinstall the CUDA/cuDNN runtime and "
                    "restart CyberHub before starting this job."
                )
            batch_size = self._effective_batch_size(tagger.device)
            with self._state_lock:
                self._job["device"] = tagger.device
                self._job["batch_size"] = batch_size
            if scope == "paths":
                paths = list(self._job_paths)
                conn = self._db()
                rows = []
                with self.gallery.db.lock:
                    for path in paths:
                        row = conn.execute("SELECT mtime FROM files WHERE path=?", (path,)).fetchone()
                        if row:
                            rows.append((path, float(row[0] or 0)))
                start = 0
                while start < len(rows):
                    if self._job_cancel.is_set():
                        break
                    while self._job_pause.is_set() and not self._job_cancel.is_set():
                        time.sleep(0.25)
                    chunk = rows[start:start + batch_size]
                    batch_supported = self._process_rows(chunk)
                    start += len(chunk)
                    if not batch_supported and batch_size > 1:
                        batch_size = 1
                        with self._state_lock:
                            self._job["batch_size"] = 1
            else:
                while not self._job_cancel.is_set():
                    while self._job_pause.is_set() and not self._job_cancel.is_set():
                        time.sleep(0.25)
                    rows = self._next_scope_rows(batch_size)
                    if not rows:
                        break
                    batch_supported = self._process_rows(rows)
                    if not batch_supported and batch_size > 1:
                        batch_size = 1
                        with self._state_lock:
                            self._job["batch_size"] = 1
            cancelled = self._job_cancel.is_set()
            with self._state_lock:
                completed_new_rowid = (
                    int(self._job.get("ceiling_rowid", 0))
                    if scope == "new" and not cancelled else 0
                )
                self._job["active"] = False
                self._job["finished"] = time.time()
                self._job["current"] = ""
                self._job["state"] = "cancelled" if cancelled else "complete"
            if completed_new_rowid:
                self._set_config("auto_new_after_rowid", str(completed_new_rowid))
        except Exception as exc:
            with self._state_lock:
                self._job.update({
                    "active": False, "state": "error", "finished": time.time(),
                    "current": "", "error": str(exc)[:500],
                })

    def _pause_job(self):
        with self._state_lock:
            if not self._job["active"]:
                return False
            self._job_pause.set()
            self._job["state"] = "paused"
        return True

    def _resume_job(self):
        with self._state_lock:
            if not self._job["active"]:
                return False
            self._job_pause.clear()
            self._job["state"] = "running"
        return True

    def _cancel_job(self):
        with self._state_lock:
            if not self._job["active"]:
                return False
            self._job_cancel.set()
            self._job_pause.clear()
            self._job["state"] = "cancelling"
        return True

    def _auto_monitor(self):
        while not self._stop.wait(8.0):
            try:
                if not self.setting("auto_tag_new", False) or not self._model_ready():
                    continue
                with self._state_lock:
                    if self._job["active"]:
                        continue
                baseline = int(self._get_config("auto_new_after_rowid", "0") or 0)
                self._start_job(
                    "new", retag=False, label="Tagging new images", new_after=baseline
                )
            except Exception:
                pass

    # ------------------------------------------------------------------ status / API
    def _status(self):
        runtime = self._runtime_status()
        conn = self._db()
        total = tagged = failed = 0
        if conn is not None:
            with self.gallery.db.lock:
                total = int(conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] or 0)
                tagged = int(conn.execute(
                    """SELECT COUNT(*) FROM auto_tagger_files a
                       JOIN files f ON f.path=a.file_path
                       WHERE a.status=1 AND a.model_revision=?
                         AND a.source_mtime=f.mtime""",
                    (self._tag_revision(),),
                ).fetchone()[0] or 0)
                failed = int(conn.execute(
                    "SELECT COUNT(*) FROM auto_tagger_files WHERE status=2"
                ).fetchone()[0] or 0)
        with self._state_lock:
            job = dict(self._job)
            download = dict(self._download)
        rate = float(job.get("rate", 0.0) or 0.0)
        remaining = max(0, int(job.get("total", 0)) - int(job.get("done", 0)))
        job["rate"] = round(rate, 2)
        job["eta"] = round(remaining / rate) if job.get("active") and rate else 0
        files = {}
        for name in MODEL_FILES:
            path = self.model_dir / name
            try:
                files[name] = path.stat().st_size if path.is_file() else 0
            except OSError:
                files[name] = 0
        return {
            "version": self.version,
            "gallery_ready": bool(self.gallery and getattr(self.gallery, "db", None)),
            "runtime": runtime,
            "model": {
                "ready": self._model_ready(), "repo": MODEL_REPO,
                "revision": MODEL_REVISION, "path": str(self.model_dir), "files": files,
            },
            "download": download,
            "job": job,
            "counts": {"total": total, "tagged": tagged, "failed": failed},
            "thresholds": self._thresholds(),
            "tag_revision": self._tag_revision(),
        }

    def _api_status(self, handler, qs):
        handler.respond_json(self._status())

    def _api_folders(self, handler, qs):
        conn = self._db()
        if conn is None:
            handler.respond_json({"folders": [], "total": 0})
            return
        with self.gallery.db.lock:
            rows = conn.execute("""
                SELECT path,name,file_count,subtree_file_count
                FROM folders
                WHERE subtree_file_count>0
                ORDER BY path COLLATE NOCASE
            """).fetchall()
        folders = [
            {
                "path": str(row[0] or ""),
                "name": str(row[1] or ""),
                "direct": int(row[2] or 0),
                "count": int(row[3] or 0),
            }
            for row in rows if row[0]
        ]
        handler.respond_json({"folders": folders, "total": len(folders)})

    def _api_install(self, handler, content_len, content_type):
        if self._model_ready():
            handler.respond_json({"ok": True, "note": "Model is already installed"})
            return
        started = self._start_download()
        handler.respond_json({"ok": True, "started": started})

    def _api_cancel_download(self, handler, content_len, content_type):
        with self._state_lock:
            active = bool(self._download["active"])
        if active:
            self._download_cancel.set()
        handler.respond_json({"ok": active})

    def _api_remove_model(self, handler, content_len, content_type):
        with self._state_lock:
            if self._job["active"] or self._download["active"]:
                handler.respond_json({"error": "Stop the active job or download first"}, status=409)
                return
        self._unload_tagger()
        removed = []
        for name in MODEL_FILES:
            for path in (self.model_dir / name, self.model_dir / (name + ".part")):
                try:
                    if path.exists():
                        path.unlink()
                        removed.append(path.name)
                except OSError as exc:
                    handler.respond_json({"error": str(exc)}, status=500)
                    return
        handler.respond_json({"ok": True, "removed": removed})

    def _api_job(self, handler, content_len, content_type):
        data = handler.read_body_json(content_len) or {}
        action = str(data.get("action") or "start").lower()
        if action == "pause":
            handler.respond_json({"ok": self._pause_job()}); return
        if action == "resume":
            handler.respond_json({"ok": self._resume_job()}); return
        if action == "cancel":
            handler.respond_json({"ok": self._cancel_job()}); return
        if action == "unload":
            self._unload_tagger(); handler.respond_json({"ok": True}); return
        scope = str(data.get("scope") or "all").lower()
        if scope not in {"all", "folder", "paths"}:
            handler.respond_json({"error": "Invalid tagging scope"}, status=400); return
        paths = data.get("paths") if isinstance(data.get("paths"), list) else []
        folder = str(data.get("folder") or "").strip("/")
        if scope == "folder":
            if not folder:
                handler.respond_json({"error": "Choose a Gallery folder first"}, status=400)
                return
            conn = self._db()
            with self.gallery.db.lock:
                exists = conn.execute(
                    "SELECT 1 FROM folders WHERE path=? AND subtree_file_count>0",
                    (folder,),
                ).fetchone() if conn is not None else None
            if not exists:
                handler.respond_json({"error": "Gallery folder was not found in the index"}, status=404)
                return
        retag = bool(data.get("retag", False))
        labels = {
            "all": "Re-tagging all images" if retag else "Tagging all untagged images",
            "folder": (
                f"Re-tagging folder: {folder}" if retag
                else f"Tagging folder: {folder}"
            ),
            "paths": "Tagging selected images",
        }
        ok, message = self._start_job(
            scope, folder=folder, paths=paths, retag=retag, label=labels[scope]
        )
        handler.respond_json({"ok": ok, "message": message}, status=200 if ok else 409)

    def _api_tag_image(self, handler, content_len, content_type):
        """Tag one browser-supplied image without adding it to the Gallery."""
        import base64
        import binascii
        import io

        if content_len > 140 * 1024 * 1024:
            handler.respond_json({"error": "Image upload is too large"}, status=413)
            return
        if not self._model_ready():
            handler.respond_json({"error": "Install the Auto Tagger model first"}, status=409)
            return
        runtime = self._runtime_status()
        if not runtime["ready"]:
            handler.respond_json({
                "error": "Missing dependencies: " + ", ".join(runtime["missing"])
            }, status=503)
            return

        data = handler.read_body_json(content_len)
        encoded = data.get("image_b64") if isinstance(data, dict) else None
        if not encoded:
            handler.respond_json({"error": "No image provided"}, status=400)
            return
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            handler.respond_json({"error": "Invalid base64 image"}, status=400)
            return
        if len(raw) > 100 * 1024 * 1024:
            handler.respond_json({"error": "Decoded image is too large"}, status=413)
            return

        thresholds = self._thresholds()
        try:
            general_threshold = float(data.get("general_threshold", thresholds["general"]))
        except (TypeError, ValueError):
            general_threshold = thresholds["general"]
        general_threshold = max(0.05, min(general_threshold, 0.95))

        image = None
        try:
            from PIL import Image

            tagger = self._get_tagger()
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", getattr(Image, "DecompressionBombWarning", Warning))
                with Image.open(io.BytesIO(raw)) as source:
                    if source.width * source.height > 200_000_000:
                        handler.respond_json({
                            "error": "Image dimensions are too large for direct tagging"
                        }, status=413)
                        return
                    image = self._downsample_source(source, tagger.target_size)
            with self._inference_lock:
                result = tagger.tag_image(
                    image,
                    general_threshold=general_threshold,
                    character_threshold=thresholds["character"],
                    adaptive_threshold=False,
                )
            handler.respond_json({
                "ok": True,
                "model": MODEL_REPO,
                "device": tagger.device,
                "general_threshold": general_threshold,
                "tags": [
                    {"name": tag.name, "category": tag.category, "score": tag.score}
                    for tag in result.all
                ],
            })
        except Exception as exc:
            handler.respond_json({"error": str(exc)[:500]}, status=500)
        finally:
            if image is not None:
                try:
                    image.close()
                except Exception:
                    pass

    def _api_tags(self, handler, qs):
        path = str(qs.get("path", [""])[0] or "")
        if not path or self._db() is None:
            handler.respond_json({"path": path, "tagged": False, "tags": []}); return
        conn = self._db()
        with self.gallery.db.lock:
            state = conn.execute("""
                SELECT a.status,a.tagged_at,a.error,a.used_thumbnail,
                       a.source_mtime,a.model_revision,f.mtime
                FROM auto_tagger_files a LEFT JOIN files f ON f.path=a.file_path
                WHERE a.file_path=?
            """, (path,)).fetchone()
            rows = conn.execute("""
                SELECT tag_name,category,score FROM auto_tagger_scores
                WHERE file_path=? ORDER BY
                    CASE category WHEN 'rating' THEN 0 WHEN 'character' THEN 1 ELSE 2 END,
                    score DESC
            """, (path,)).fetchall()
        handler.respond_json({
            "path": path,
            "tagged": bool(
                state and state[0] == 1 and state[5] == self._tag_revision()
                and state[6] is not None and state[4] == state[6]
            ),
            "state": {
                "status": state[0], "tagged_at": state[1], "error": state[2],
                "used_thumbnail": bool(state[3]), "source_mtime": state[4],
                "model_revision": state[5],
                "stale": bool(
                    state[5] != self._tag_revision() or state[6] is None
                    or state[4] != state[6]
                ),
            } if state else None,
            "tags": [
                {"name": row[0], "category": row[1], "score": row[2]} for row in rows
            ],
        })

    def _api_suggest(self, handler, qs):
        query = str(qs.get("q", [""])[0] or "").strip().lower()
        limit = handler._int(qs, "limit", 30, 1, 100)
        conn = self._db()
        if conn is None:
            handler.respond_json([]); return
        where = "WHERE tag_name LIKE ?" if query else ""
        params = [f"%{query.replace(' ', '_')}%"] if query else []
        with self.gallery.db.lock:
            rows = conn.execute(f"""
                SELECT tag_name,category,use_count,
                       CASE WHEN use_count>0 THEN score_sum/use_count ELSE 0 END
                FROM auto_tagger_catalog {where}
                ORDER BY use_count DESC,score_sum/use_count DESC LIMIT ?
            """, params + [limit]).fetchall()
        handler.respond_json([
            {"name": row[0], "category": row[1], "count": row[2], "score": row[3]}
            for row in rows
        ])

    def _api_files(self, handler, qs):
        raw_tags = str(qs.get("tags", [""])[0] or "")
        tags = []
        for value in raw_tags.split(","):
            value = value.strip().lower().replace(" ", "_")
            if value and value not in tags:
                tags.append(value)
        if not tags or self._db() is None:
            handler.respond_json({"files": [], "total": 0, "page": 1, "pages": 1}); return
        page = handler._int(qs, "page", 1)
        per_page = handler._int(qs, "per_page", 200, 50, 1000)
        folder = str(qs.get("folder", [""])[0] or "").strip("/")
        sort = str(qs.get("sort", ["date"])[0] or "date")
        order = str(qs.get("order", ["desc"])[0] or "desc")
        sort_col = {
            "name": "f.name", "date": "f.mtime", "size": "f.size",
            "favorite": "f.favorite",
        }.get(sort, "f.mtime")
        order_sql = "DESC" if order.lower() == "desc" else "ASC"
        model_info = qs.get("models", [""])[0] == "1"
        metadata_col = ", f.model_name, f.model_family" if model_info else ""
        metadata_filter = str(qs.get("metadata", ["all"])[0] or "all")
        metadata_sql = self.gallery.db._metadata_filter_sql(metadata_filter, "f")
        metadata_clause = f" AND {metadata_sql}" if metadata_sql else ""
        placeholders = ",".join("?" for _ in tags)
        folder_sql = ""
        params = list(tags)
        if folder:
            folder_sql = "AND (f.folder=? OR f.folder LIKE ?)"
            params.extend([folder, folder + "/%"])
        conn = self._db()
        with self.gallery.db.lock:
            total = conn.execute(f"""
                SELECT COUNT(*) FROM files f JOIN (
                    SELECT file_path FROM auto_tagger_scores
                    WHERE tag_name IN ({placeholders})
                    GROUP BY file_path HAVING COUNT(DISTINCT tag_name)=?
                ) matched ON matched.file_path=f.path WHERE 1=1 {folder_sql}{metadata_clause}
            """, list(tags) + [len(tags)] + params[len(tags):]).fetchone()[0]
            offset = (page - 1) * per_page
            rows = conn.execute(f"""
                SELECT f.path,f.name,f.folder,f.ext,f.size,f.mtime,f.width,f.height,
                       f.has_metadata,f.favorite,f.processing_state{metadata_col}
                FROM files f JOIN (
                    SELECT file_path FROM auto_tagger_scores
                    WHERE tag_name IN ({placeholders})
                    GROUP BY file_path HAVING COUNT(DISTINCT tag_name)=?
                ) matched ON matched.file_path=f.path
                WHERE 1=1 {folder_sql}{metadata_clause}
                ORDER BY {sort_col} {order_sql}, f.mtime DESC LIMIT ? OFFSET ?
            """, list(tags) + [len(tags)] + params[len(tags):] + [per_page, offset]).fetchall()
        pages = max(1, (int(total or 0) + per_page - 1) // per_page)
        handler.respond_json({
            "files": [self.gallery.db._file_dict(row, model_info) for row in rows],
            "total": int(total or 0), "page": page, "pages": pages,
            "per_page": per_page, "tags": tags,
        })


PAGE_BODY = r"""
<style>
.at-page{height:100%;overflow:auto;background:var(--bg-darkest);padding:22px;color:var(--text)}
.at-wrap{max-width:1120px;margin:0 auto}.at-head{display:flex;align-items:flex-start;justify-content:space-between;gap:16px;margin-bottom:18px}.at-head h1{font-size:20px;margin:0 0 5px;color:var(--text-bright)}.at-sub{font-size:12px;color:var(--text-dim)}
.at-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px;margin-bottom:14px}.at-stat{border:1px solid var(--border);background:var(--bg-panel);border-radius:8px;padding:12px}.at-stat-label{font-size:10px;text-transform:uppercase;letter-spacing:.6px;color:var(--text-dim);margin-bottom:5px}.at-stat-value{font-size:14px;color:var(--text-bright);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.at-card{border:1px solid var(--border);background:var(--bg-panel);border-radius:8px;padding:15px;margin-bottom:12px}.at-card-head{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:12px}.at-card h2{font-size:13px;margin:0;color:var(--text-bright)}
.at-actions{display:flex;flex-wrap:wrap;gap:8px}.at-btn{border:1px solid var(--border);background:var(--bg-card);color:var(--text);border-radius:7px;padding:8px 12px;font:inherit;font-size:12px;cursor:pointer}.at-btn:hover:not(:disabled){border-color:var(--accent);color:var(--text-bright)}.at-btn.primary{background:var(--accent);border-color:var(--accent);color:#fff}.at-btn.danger{color:var(--red)}.at-btn:disabled{opacity:.45;cursor:not-allowed}
.at-folder-tools{display:grid;grid-template-columns:minmax(260px,1fr) auto auto;gap:8px;align-items:end;margin:12px 0 4px}.at-folder-field{display:flex;flex-direction:column;gap:5px}.at-folder-field label{font-size:10px;text-transform:uppercase;letter-spacing:.6px;color:var(--text-dim)}.at-input{width:100%;box-sizing:border-box;border:1px solid var(--border);background:var(--bg-card);color:var(--text-bright);border-radius:7px;padding:8px 10px;font:inherit;font-size:12px;outline:none}.at-input:focus{border-color:var(--accent)}.at-folder-note{min-height:16px;margin-bottom:8px}
.at-progress{height:8px;background:var(--bg-card);border-radius:999px;overflow:hidden;margin:10px 0}.at-progress>span{display:block;height:100%;width:0;background:var(--accent);transition:width .25s}.at-job-row{display:flex;align-items:center;justify-content:space-between;gap:14px;font-size:11px;color:var(--text-dim)}.at-job-current{font-family:var(--mono);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:70%}.at-error{display:none;color:var(--red);font-size:12px;margin-top:10px;white-space:pre-wrap}.at-error.on{display:block}.at-tags{display:flex;flex-wrap:wrap;gap:6px}.at-tag{font-family:var(--mono);font-size:11px;border:1px solid var(--border);background:var(--bg-card);color:var(--text);padding:4px 7px;border-radius:6px;cursor:pointer}.at-tag.character{color:#fbbf24}.at-tag.rating{color:#34d399}.at-tag .count{color:var(--text-dim);margin-left:5px}
@media(max-width:760px){.at-page{padding:14px}.at-grid{grid-template-columns:1fr 1fr}.at-head{display:block}.at-head .at-actions{margin-top:10px}.at-folder-tools{grid-template-columns:1fr}.at-folder-tools .at-btn{width:100%}}
</style>
<div class="at-page"><div class="at-wrap">
  <div class="at-head"><div><h1>Auto Tagger</h1><div class="at-sub">Local visual tagging for Gallery images</div></div><div class="at-actions"><a class="at-btn" href="/gallery">Open Gallery</a><a class="at-btn" href="/settings">Settings</a></div></div>
  <div class="at-grid">
    <div class="at-stat"><div class="at-stat-label">Model</div><div class="at-stat-value" id="modelState">Checking</div></div>
    <div class="at-stat"><div class="at-stat-label">Runtime</div><div class="at-stat-value" id="runtimeState">Checking</div></div>
    <div class="at-stat"><div class="at-stat-label">Tagged</div><div class="at-stat-value" id="taggedState">0</div></div>
    <div class="at-stat"><div class="at-stat-label">Preset</div><div class="at-stat-value" id="presetState">Balanced</div></div>
  </div>
  <section class="at-card"><div class="at-card-head"><h2>Model</h2><span class="at-sub" id="modelPath"></span></div><div class="at-actions"><button class="at-btn primary" id="installBtn">Install model</button><button class="at-btn" id="cancelDownloadBtn">Cancel download</button><button class="at-btn danger" id="removeBtn">Remove model</button></div><div class="at-progress" id="downloadProgress"><span></span></div><div class="at-job-row"><span id="downloadText">The model is downloaded only after you click Install.</span><span id="downloadNumbers"></span></div><div class="at-error" id="downloadError"></div></section>
  <section class="at-card"><div class="at-card-head"><h2>Background tagging</h2><span class="at-sub" id="deviceState"></span></div><div class="at-actions"><button class="at-btn primary" id="tagAllBtn">Tag all untagged</button><button class="at-btn" id="retagAllBtn">Re-tag all</button><button class="at-btn" id="pauseBtn">Pause</button><button class="at-btn danger" id="cancelBtn">Cancel</button></div><div class="at-folder-tools"><div class="at-folder-field"><label for="folderInput">Gallery folder</label><input class="at-input" id="folderInput" list="galleryFolderList" autocomplete="off" placeholder="Choose or type a Gallery folder"><datalist id="galleryFolderList"></datalist></div><button class="at-btn primary" id="tagFolderBtn">Tag folder</button><button class="at-btn" id="retagFolderBtn">Re-tag folder</button></div><div class="at-sub at-folder-note" id="folderNote">Loading Gallery folders...</div><div class="at-progress" id="jobProgress"><span></span></div><div class="at-job-row"><span id="jobText">Idle</span><span id="jobNumbers"></span></div><div class="at-job-current" id="jobCurrent"></div><div class="at-error" id="jobError"></div></section>
  <section class="at-card"><div class="at-card-head"><h2>Most common AI tags</h2><span class="at-sub">Stored locally in the Gallery database</span></div><div class="at-tags" id="topTags"><span class="at-sub">No tags yet</span></div></section>
</div></div>
<script>
(function(){
'use strict';
var lastState=null, timer=null, folderCounts=new Map();
function $(id){return document.getElementById(id)}
function fmtBytes(v){v=Number(v||0);var u=['B','KB','MB','GB'],i=0;while(v>=1024&&i<u.length-1){v/=1024;i++}return (i?v.toFixed(1):Math.round(v))+' '+u[i]}
function fmtTime(s){s=Math.max(0,Math.round(Number(s||0)));if(!s)return '';var h=Math.floor(s/3600),m=Math.floor((s%3600)/60),x=s%60;return (h?h+'h ':'')+(m?m+'m ':'')+x+'s'}
async function api(url,options){var r=await fetch(url,options);var d=await r.json().catch(function(){return {}});if(!r.ok)throw new Error(d.error||d.message||('HTTP '+r.status));return d}
function post(url,data){return api(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data||{})})}
function setError(id,text){var el=$(id);el.textContent=text||'';el.classList.toggle('on',!!text)}
function selectedFolder(){return String($('folderInput').value||'').replace(/^\/+|\/+$/g,'')}
function updateFolderControls(){var folder=selectedFolder(),valid=folderCounts.has(folder),s=lastState||{},model=s.model||{},runtime=s.runtime||{},job=s.job||{};$('tagFolderBtn').disabled=!valid||!model.ready||!runtime.ready||!!job.active;$('retagFolderBtn').disabled=!valid||!model.ready||!runtime.ready||!!job.active;var count=valid?Number(folderCounts.get(folder)||0):0;$('folderNote').textContent=valid?(count.toLocaleString()+' indexed images, including subfolders'):(folder?'Choose a folder from the Gallery index':'Folder jobs include all subfolders')}
function render(s){lastState=s;var model=s.model||{},runtime=s.runtime||{},job=s.job||{},dl=s.download||{},counts=s.counts||{};
  $('modelState').textContent=model.ready?'Ready':'Not installed';$('runtimeState').textContent=runtime.message||(runtime.ready?'CPU':'Missing '+(runtime.missing||[]).join(', '));$('runtimeState').title=(runtime.providers||[]).join(', ');$('taggedState').textContent=Number(counts.tagged||0).toLocaleString()+' / '+Number(counts.total||0).toLocaleString();$('presetState').textContent=(s.thresholds||{}).preset||'Balanced';$('modelPath').textContent=model.path||'';
  $('installBtn').disabled=!!model.ready||!!dl.active;$('cancelDownloadBtn').disabled=!dl.active;$('removeBtn').disabled=!model.ready||!!job.active||!!dl.active;
  var dtotal=Number(dl.file_total||dl.total||0),ddone=Number(dl.file_done||dl.done||0),dp=dtotal?Math.min(100,ddone/dtotal*100):0;$('downloadProgress').querySelector('span').style.width=dp+'%';$('downloadText').textContent=dl.active?('Downloading '+(dl.file||'model')):(model.ready?'Model files are ready.':dl.cancelled?'Download cancelled. Install resumes the partial file.':'The model is downloaded only after you click Install.');$('downloadNumbers').textContent=dl.active?(fmtBytes(ddone)+' / '+fmtBytes(dtotal)):'';setError('downloadError',dl.error);
  var total=Number(job.total||0),done=Number(job.done||0),rate=Number(job.rate||0),pct=total?Math.min(100,done/total*100):(job.state==='complete'?100:0);$('jobProgress').querySelector('span').style.width=pct+'%';$('jobText').textContent=job.active?(job.label||'Tagging images'):(job.state==='complete'?'Complete':job.state==='cancelled'?'Cancelled':job.state==='error'?'Failed':'Idle');var speed=rate?(' · '+rate.toFixed(rate<10?2:1)+' img/s'):(job.active&&done?' · measuring speed':'');$('jobNumbers').textContent=total?(done.toLocaleString()+' / '+total.toLocaleString()+(job.failed?' · '+job.failed+' failed':'')+speed+(job.eta?' · '+fmtTime(job.eta)+' left':'')):'';$('jobCurrent').textContent=job.current||'';var device=job.device?job.device.replace('ExecutionProvider',''):'';$('deviceState').textContent=device+(job.batch_size?' · batch '+job.batch_size:'')+(job.source==='thumbnail'?' · fast thumbs':'');setError('jobError',job.error);
  $('tagAllBtn').disabled=!model.ready||!runtime.ready||!!job.active;$('retagAllBtn').disabled=!model.ready||!runtime.ready||!!job.active;$('pauseBtn').disabled=!job.active;$('pauseBtn').textContent=job.state==='paused'?'Resume':'Pause';$('cancelBtn').disabled=!job.active;updateFolderControls();
}
async function refresh(){try{render(await api('/api/auto_tagger/status'));}catch(e){setError('jobError',e.message)}}
async function loadFolders(){try{var data=await api('/api/auto_tagger/folders'),folders=data.folders||[],list=$('galleryFolderList'),frag=document.createDocumentFragment();folderCounts.clear();folders.forEach(function(folder){folderCounts.set(folder.path,Number(folder.count||0));var option=document.createElement('option');option.value=folder.path;option.label=Number(folder.count||0).toLocaleString()+' images';frag.appendChild(option)});list.replaceChildren(frag);var saved='';try{saved=localStorage.getItem('galleryFolder')||localStorage.getItem('autoTaggerFolder')||''}catch(e){}if(saved&&folderCounts.has(saved))$('folderInput').value=saved;updateFolderControls()}catch(e){$('folderNote').textContent='Could not load Gallery folders';setError('jobError',e.message)}}
async function loadTags(){try{var rows=await api('/api/auto_tagger/suggest?limit=40');$('topTags').innerHTML=rows.length?rows.map(function(t){return '<button class="at-tag '+t.category+'" type="button" data-tag="'+String(t.name).replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;')+'">'+String(t.name).replace(/&/g,'&amp;').replace(/</g,'&lt;')+'<span class="count">'+Number(t.count).toLocaleString()+'</span></button>'}).join(''):'<span class="at-sub">No tags yet</span>';$('topTags').querySelectorAll('[data-tag]').forEach(function(el){el.onclick=function(){window.location.href='/gallery?ai_tags='+encodeURIComponent(el.dataset.tag)}})}catch(e){}}
$('installBtn').onclick=async function(){try{await post('/api/auto_tagger/install',{});refresh()}catch(e){setError('downloadError',e.message)}};
$('cancelDownloadBtn').onclick=async function(){try{await post('/api/auto_tagger/cancel_download',{});refresh()}catch(e){setError('downloadError',e.message)}};
$('removeBtn').onclick=async function(){if(!confirm('Remove the downloaded Auto Tagger model? Stored image tags are kept.'))return;try{await post('/api/auto_tagger/remove_model',{});refresh()}catch(e){setError('downloadError',e.message)}};
$('tagAllBtn').onclick=async function(){try{await post('/api/auto_tagger/job',{action:'start',scope:'all'});refresh()}catch(e){setError('jobError',e.message)}};
$('retagAllBtn').onclick=async function(){if(!confirm('Re-tag every Gallery image? This can take a long time.'))return;try{await post('/api/auto_tagger/job',{action:'start',scope:'all',retag:true});refresh()}catch(e){setError('jobError',e.message)}};
$('folderInput').oninput=function(){try{localStorage.setItem('autoTaggerFolder',selectedFolder())}catch(e){}updateFolderControls()};
$('tagFolderBtn').onclick=async function(){var folder=selectedFolder();try{await post('/api/auto_tagger/job',{action:'start',scope:'folder',folder:folder});refresh()}catch(e){setError('jobError',e.message)}};
$('retagFolderBtn').onclick=async function(){var folder=selectedFolder();if(!confirm('Re-tag every image in '+folder+' and its subfolders?'))return;try{await post('/api/auto_tagger/job',{action:'start',scope:'folder',folder:folder,retag:true});refresh()}catch(e){setError('jobError',e.message)}};
$('pauseBtn').onclick=async function(){try{await post('/api/auto_tagger/job',{action:(lastState&&lastState.job&&lastState.job.state==='paused')?'resume':'pause'});refresh()}catch(e){setError('jobError',e.message)}};
$('cancelBtn').onclick=async function(){try{await post('/api/auto_tagger/job',{action:'cancel'});refresh()}catch(e){setError('jobError',e.message)}};
refresh();loadFolders();loadTags();timer=setInterval(function(){refresh();if(lastState&&lastState.job&&!lastState.job.active)loadTags()},1800);
})();
</script>
"""
