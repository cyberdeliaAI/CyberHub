"""Gallery module — the main image browser with SQLite indexing, metadata,
favorites, collections, search and a metadata side-panel.

This module is the CyberHub flagship: it owns the 3-column gallery
layout and most of the user-visible chrome (sidebar + grid + meta panel +
statusbar). Other modules (Viewer, Tools, Settings) are simpler pages
served inside the same topbar shell.

Settings (configured from the Settings page):
    folders                list      Image folder roots
    per_page               int       Images per page (default 200)
    background_processing bool      Process metadata/thumbs in background
    filesystem_watcher     bool      Apply targeted file changes (default true)
    processing_workers     int       Background workers (default 2)
    background_reindex     bool      Periodically rescan folders (default false)
    reindex_interval       int       Seconds between scans (default 30)
    thumb_workers          int|null  Bulk thumbnail worker count (null = auto)
    verbose                bool      Debug logging from metadata reader

All settings are also accepted via gallery.py-style CLI args on hub.py
for the current session (--port, --listen, --share-network, --models,
--verbose); the values in settings.json are used when the args are not
given.
"""

import concurrent.futures
import hashlib
import json
import os
import re
import sqlite3
import sys
import threading
import time
import traceback
from collections import deque
from io import BytesIO
from pathlib import Path

from core import Module
from core.metadata import get_image_metadata, parse_sd_parameters
from core import metadata as _metadata_mod
from core.server import build_topbar, build_module_menu, theme_body_class, _BASE_CSS, _BASE_JS, HELP_OVERLAY_HTML

# ─── Constants ────────────────────────────────────────────────────────────────

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff", ".tif"}
THUMB_SIZE = (300, 300)
# The thumbnail directory and shared database live in Hub.data_dir. Native
# installs default that directory to the portable CyberHub folder.
DEFAULT_PER_PAGE = 200       # overwritten from settings on startup
DEFAULT_REINDEX_INTERVAL = 30

try:
    from PIL import Image
    HAS_PIL = True
    LANCZOS = getattr(Image, "Resampling", Image).LANCZOS
except ImportError:
    HAS_PIL = False
    Image = None
    LANCZOS = None
    print("[WARN] Pillow not installed — pip install Pillow")

try:
    from send2trash import send2trash
    HAS_TRASH = True
except ImportError:
    HAS_TRASH = False
    print("[WARN] send2trash not installed — delete will permanently remove files. "
          "pip install send2trash")

try:
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer
    HAS_WATCHDOG = True
except ImportError:
    FileSystemEventHandler = object
    Observer = None
    HAS_WATCHDOG = False


def log_debug(msg):
    if getattr(_metadata_mod, "VERBOSE", False):
        print(f"[DEBUG] {msg}")


def safe_resolve(root, rel_path):
    """Resolve `rel_path` against `root`, returning None on traversal attempts."""
    try:
        root_abs = os.path.realpath(root)
        full = os.path.realpath(os.path.join(root, rel_path))
        if not full.startswith(root_abs + os.sep) and full != root_abs:
            log_debug(f"Path traversal blocked: {rel_path}")
            return None
        return full
    except (ValueError, OSError):
        return None


# ─── Parallel file reader ──────────────────────────────────────────────────────

def _read_file_payload(item):
    """Read metadata + image dimensions for one file. Pure / thread-safe — no DB,
    no shared mutable state — so it can run in a worker thread. `item` carries the
    bookkeeping fields the DB-write step needs; we just append (meta, w, h)."""
    full_path, full_dir, fname, fpath, fav, size, mtime, had_row = item
    meta = get_image_metadata(fpath)
    w = h = 0
    if HAS_PIL:
        try:
            with Image.open(fpath) as img:
                w, h = img.size
        except Exception:
            pass
    return (full_path, full_dir, fname, fav, size, mtime, had_row, meta, w, h)


# ─── Database ─────────────────────────────────────────────────────────────────

class GalleryDB:
    DISCOVERY_UPSERT_SQL = """
        INSERT INTO files (
            path, folder, name, ext, size, mtime, width, height,
            has_metadata, metadata_json, favorite, processing_state,
            processing_error, processed_mtime, model_name, model_family,
            model_scanned
        ) VALUES (?,?,?,?,?,?,0,0,0,NULL,0,0,NULL,0,'','unknown',0)
        ON CONFLICT(path) DO UPDATE SET
            folder=excluded.folder,
            name=excluded.name,
            ext=excluded.ext,
            size=excluded.size,
            mtime=excluded.mtime,
            processing_state=0,
            processing_error=NULL,
            model_scanned=0
    """
    MODEL_FAMILY_RULES = (
        (("z-image", "zimage", "z_image"), "Z-Image", "#38bdf8"),
        (("anima",), "Anima", "#a855f7"),
        (("krea",), "Krea", "#22c55e"),
        (("flux",), "Flux", "#f59e0b"),
        (("pony",), "Pony", "#ec4899"),
        (("illustrious",), "Illustrious", "#60a5fa"),
        (("noobai", "noob ai"), "NoobAI", "#14b8a6"),
        (("sdxl", "sd_xl", "stable diffusion xl"), "SDXL", "#94a3b8"),
        (("sd15", "sd 1.5", "sd1.5", "stable diffusion 1.5"), "SD 1.5", "#64748b"),
        (("qwen",), "Qwen", "#fb923c"),
        (("wan",), "Wan Video", "#06b6d4"),
        (("hunyuan",), "Hunyuan", "#8b5cf6"),
        (("ltxv", "ltx video"), "LTXV", "#f472b6"),
    )
    MODEL_COLORS = (
        "#38bdf8", "#22c55e", "#a855f7", "#f59e0b", "#ec4899", "#14b8a6",
        "#fb923c", "#8b5cf6", "#06b6d4", "#84cc16", "#f43f5e", "#60a5fa",
    )

    def __init__(self, db_path, thumb_dir, roots):
        """
        db_path:   absolute path to the SQLite database file
        thumb_dir: absolute path to the central thumbnail cache directory
        roots:     dict of {display_name: absolute_path} for image folders
        """
        self.db_path = db_path
        self.thumb_dir = thumb_dir
        self.roots = roots  # {"SD Output": "D:\\SD Images\\output", ...}
        self.lock = threading.Lock()
        self._local = threading.local()
        self.has_fts = False
        self.search_index_ready = False
        self._search_index_notice_shown = False
        self._processing_stop = threading.Event()
        self._processing_paused = threading.Event()
        self._processing_wakeup = threading.Event()
        self._processing_lock = threading.Lock()
        self._processing_threads = []
        self._processing_claimed = set()
        self._processing_priority = deque()
        self._processing_priority_set = set()
        self._processing_active = 0
        self._processing_session_done = 0
        self._processing_failures = {}
        self._model_backfill_lock = threading.Lock()
        self._tag_counts_dirty = threading.Event()
        self._processing_rebuild_fts = False
        self._scan_status_lock = threading.Lock()
        self._scan_status = {
            "active": False, "root": "", "scanned": 0,
            "total": 0, "queued": 0, "error": "",
        }
        self._create_tables()
        self._ensure_search_index()

    def _get_conn(self):
        if not hasattr(self._local, "conn"):
            self._local.conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self._local.conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn.execute("PRAGMA busy_timeout=30000")
        return self._local.conn

    def optimize(self, progress=None):
        """Compact + refresh the database. Returns a dict describing what ran.

        VACUUM rewrites the entire file, so on a large library it can take tens of
        seconds while reclaiming little — it's only worth it when meaningful free
        space exists. So we VACUUM only when >10% of pages are free AND at least
        ~5 MB would be reclaimed. SQLite's bounded PRAGMA optimize refreshes query
        planner statistics; a full FTS segment merge is intentionally not part of
        routine maintenance because it can monopolize a large database for minutes.
        """
        def report(stage, message, current=0, total=0):
            if progress:
                progress(stage, message, current, total)

        report("inspect", "Inspecting database", 0, 3)
        conn = self._get_conn()
        did_vacuum = False
        reclaimable = 0
        with self.lock:
            conn.commit()  # close any implicit transaction
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.OperationalError:
                pass
            try:
                before = os.path.getsize(self.db_path)
            except OSError:
                before = 0
            try:
                page_count = conn.execute("PRAGMA page_count").fetchone()[0]
                free = conn.execute("PRAGMA freelist_count").fetchone()[0]
                page_size = conn.execute("PRAGMA page_size").fetchone()[0]
            except sqlite3.OperationalError:
                page_count = free = page_size = 0
            reclaimable = free * page_size
            want_vacuum = bool(page_count) and (free / page_count) > 0.10 and reclaimable > 5 * 1024 * 1024
            old_iso = conn.isolation_level
            conn.isolation_level = None  # autocommit - required for VACUUM
            try:
                if want_vacuum:
                    report("vacuum", "Reclaiming unused database space", 1, 3)
                    conn.execute("VACUUM")
                    did_vacuum = True
                else:
                    report("vacuum", "No database rewrite needed", 1, 3)
                report("statistics", "Refreshing query statistics", 2, 3)
                conn.execute("PRAGMA optimize")
            finally:
                conn.isolation_level = old_iso
        try:
            after = os.path.getsize(self.db_path)
        except OSError:
            after = before
        report("complete", "Database optimization complete", 3, 3)
        return {
            "before": before,
            "after": after,
            "vacuumed": did_vacuum,
            "fts": False,
            "reclaimable": reclaimable,
        }

    def _create_tables(self):
        conn = self._get_conn()
        with self.lock:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS folders (
                    path TEXT PRIMARY KEY, name TEXT, parent TEXT,
                    file_count INTEGER DEFAULT 0, has_subfolders INTEGER DEFAULT 0,
                    mtime REAL, cover_image TEXT,
                    subtree_file_count INTEGER DEFAULT 0,
                    latest_mtime REAL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS files (
                    path TEXT PRIMARY KEY, folder TEXT, name TEXT, ext TEXT,
                    size INTEGER, mtime REAL, width INTEGER, height INTEGER,
                    has_metadata INTEGER DEFAULT 0, metadata_json TEXT,
                    favorite INTEGER DEFAULT 0,
                    processing_state INTEGER DEFAULT 1,
                    processing_error TEXT,
                    processed_mtime REAL DEFAULT 0,
                    model_name TEXT DEFAULT '',
                    model_family TEXT DEFAULT 'unknown',
                    model_scanned INTEGER DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS tags (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT UNIQUE,
                    count INTEGER DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS file_tags (
                    file_path TEXT,
                    tag_id INTEGER,
                    tag_type TEXT DEFAULT 'prompt',
                    PRIMARY KEY (file_path, tag_id)
                );
                CREATE INDEX IF NOT EXISTS idx_files_folder ON files(folder);
                CREATE INDEX IF NOT EXISTS idx_folders_parent ON folders(parent);
                CREATE INDEX IF NOT EXISTS idx_tags_name ON tags(name);
                CREATE INDEX IF NOT EXISTS idx_file_tags_tag ON file_tags(tag_id);
                CREATE INDEX IF NOT EXISTS idx_file_tags_file ON file_tags(file_path);
                CREATE TABLE IF NOT EXISTS collections (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT UNIQUE NOT NULL,
                    color TEXT DEFAULT '#4a9eff',
                    created REAL
                );
                CREATE TABLE IF NOT EXISTS file_collections (
                    file_path TEXT,
                    collection_id INTEGER,
                    added REAL,
                    PRIMARY KEY (file_path, collection_id)
                );
                CREATE INDEX IF NOT EXISTS idx_fc_collection ON file_collections(collection_id);
                CREATE INDEX IF NOT EXISTS idx_fc_file ON file_collections(file_path);
            """)
            # Migration: add favorite column to existing DBs (must run before index creation)
            try:
                conn.execute("ALTER TABLE files ADD COLUMN favorite INTEGER DEFAULT 0")
            except sqlite3.OperationalError:
                pass
            file_columns = {r[1] for r in conn.execute("PRAGMA table_info(files)").fetchall()}
            file_migrations = (
                ("processing_state", "INTEGER DEFAULT 1"),
                ("processing_error", "TEXT"),
                ("processed_mtime", "REAL DEFAULT 0"),
                ("model_name", "TEXT DEFAULT ''"),
                ("model_family", "TEXT DEFAULT 'unknown'"),
                ("model_scanned", "INTEGER DEFAULT 0"),
            )
            for column, declaration in file_migrations:
                if column not in file_columns:
                    conn.execute(f"ALTER TABLE files ADD COLUMN {column} {declaration}")
            folder_columns = {r[1] for r in conn.execute("PRAGMA table_info(folders)").fetchall()}
            for column, declaration in (
                ("subtree_file_count", "INTEGER DEFAULT 0"),
                ("latest_mtime", "REAL DEFAULT 0"),
            ):
                if column not in folder_columns:
                    conn.execute(f"ALTER TABLE folders ADD COLUMN {column} {declaration}")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_files_favorite ON files(favorite)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_files_metadata ON files(has_metadata)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_files_mtime ON files(mtime)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_files_folder_name ON files(folder, name)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_files_folder_mtime ON files(folder, mtime)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_files_folder_size ON files(folder, size)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_files_processing ON files(processing_state, mtime)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_files_model_scan ON files(model_scanned, path)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_files_model_status ON files(model_scanned, processing_state, path)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_folders_parent_visible ON folders(parent, subtree_file_count)")
            try:
                conn.execute("""
                    CREATE VIRTUAL TABLE IF NOT EXISTS gallery_search USING fts5(
                        path UNINDEXED,
                        folder UNINDEXED,
                        name,
                        tags,
                        metadata,
                        tokenize = 'unicode61 tokenchars ''_:-.'''
                    )
                """)
                self.has_fts = True
            except sqlite3.OperationalError as e:
                self.has_fts = False
                print(f"[SEARCH] FTS5 unavailable; using legacy LIKE search ({e})")
            aggregate_count = conn.execute(
                "SELECT COUNT(*) FROM folders WHERE subtree_file_count > 0"
            ).fetchone()[0]
            if not aggregate_count and conn.execute("SELECT 1 FROM files LIMIT 1").fetchone():
                self._refresh_folder_aggregates(conn)
            conn.commit()

    def index_tree(self, force=False, prune_deleted=None, workers=None):
        """Discover filesystem changes without opening image contents.

        New and changed files are inserted immediately with processing_state=0.
        The background processor later derives metadata, dimensions, model fields,
        search data and thumbnails. This keeps startup and rescans responsive even
        for very large or network-backed libraries.
        """
        conn = self._get_conn()
        t0 = time.time(); queued_files = 0; scanned_files = 0; skipped_dirs = 0
        pruned_files = 0; pruned_folders = 0
        last_progress = t0
        discovery_batch = []
        if prune_deleted is None:
            prune_deleted = force
        all_folders = set() if prune_deleted else None
        all_files = set() if prune_deleted else None
        scanned_roots = set()
        with self.lock:
            estimated_total = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        with self._scan_status_lock:
            self._scan_status = {
                "active": True, "root": "", "scanned": 0,
                "total": int(estimated_total or 0), "queued": 0, "error": "",
            }
        def flush_discovery():
            if not discovery_batch:
                return
            batch = discovery_batch[:]
            discovery_batch.clear()
            with self.lock:
                conn.executemany(self.DISCOVERY_UPSERT_SQL, batch)
                conn.commit()

        if force and self.has_fts:
            with self.lock:
                conn.execute("DELETE FROM gallery_search")
                conn.commit()
            self.search_index_ready = False
            self._processing_rebuild_fts = True

        for root_name, root_abs in self.roots.items():
            if not os.path.isdir(root_abs):
                print(f"[INDEX] Root unavailable, skipping: {root_name} -> {root_abs}")
                continue
            print(f"[INDEX] Scanning root: {root_name}")
            with self._scan_status_lock:
                self._scan_status["root"] = root_name
            scanned_roots.add(root_name)
            for dirpath, dirnames, filenames in os.walk(root_abs):
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]
                rel_dir = os.path.relpath(dirpath, root_abs).replace("\\", "/")
                if rel_dir == ".":
                    full_dir = root_name
                else:
                    full_dir = root_name + "/" + rel_dir
                parent = os.path.dirname(full_dir).replace("\\", "/") if "/" in full_dir else ""
                if prune_deleted:
                    all_folders.add(full_dir)
                images = [f for f in filenames if Path(f).suffix.lower() in IMAGE_EXTENSIONS]
                has_subs = 1 if dirnames else 0
                cover = (full_dir + "/" + images[0]) if images else None
                try:
                    dir_mtime = os.path.getmtime(dirpath)
                except OSError:
                    dir_mtime = 0
                if not force and not prune_deleted:
                    with self.lock:
                        folder_row = conn.execute("SELECT file_count, mtime FROM folders WHERE path=?", (full_dir,)).fetchone()
                    if folder_row and folder_row[0] == len(images) and abs((folder_row[1] or 0) - dir_mtime) < 0.01:
                        skipped_dirs += 1
                        scanned_files += len(images)
                        with self._scan_status_lock:
                            self._scan_status["scanned"] = scanned_files
                        now = time.time()
                        if (scanned_files and scanned_files % 2500 == 0) or now - last_progress >= 30:
                            print(
                                f"[INDEX] Scanned {scanned_files} files "
                                f"({queued_files} new/updated so far, {skipped_dirs} unchanged folders skipped)..."
                            )
                            last_progress = now
                        continue
                with self.lock:
                    conn.execute("""
                        INSERT INTO folders (
                            path, name, parent, file_count, has_subfolders,
                            mtime, cover_image
                        ) VALUES (?,?,?,?,?,?,?)
                        ON CONFLICT(path) DO UPDATE SET
                            name=excluded.name,
                            parent=excluded.parent,
                            file_count=excluded.file_count,
                            has_subfolders=excluded.has_subfolders,
                            mtime=excluded.mtime,
                            cover_image=excluded.cover_image
                    """, (full_dir, os.path.basename(dirpath) if rel_dir != "." else root_name,
                           parent, len(images), has_subs, dir_mtime, cover))
                    existing_mtimes = {
                        row[0]: float(row[1] or 0)
                        for row in conn.execute(
                            "SELECT path, mtime FROM files WHERE folder=?", (full_dir,)
                        ).fetchall()
                    } if not force else {}
                    if not force and not prune_deleted:
                        current_paths = {
                            root_name + "/" + os.path.relpath(os.path.join(dirpath, image_name), root_abs).replace("\\", "/")
                            for image_name in images
                        }
                        stale_paths = [path for path in existing_mtimes if path not in current_paths]
                        if stale_paths:
                            pruned_files += self._prune_file_records(conn, stale_paths)
                for fname in images:
                    fpath = os.path.join(dirpath, fname)
                    rel_from_root = os.path.relpath(fpath, root_abs).replace("\\", "/")
                    full_path = root_name + "/" + rel_from_root
                    scanned_files += 1
                    if scanned_files % 250 == 0:
                        with self._scan_status_lock:
                            self._scan_status["scanned"] = scanned_files
                    if prune_deleted:
                        all_files.add(full_path)
                    try: stat = os.stat(fpath)
                    except OSError: continue
                    if not force:
                        existing_mtime = existing_mtimes.get(full_path)
                        if existing_mtime is not None and abs(existing_mtime - stat.st_mtime) < 0.01:
                            now = time.time()
                            if scanned_files % 2500 == 0 or now - last_progress >= 30:
                                print(
                                    f"[INDEX] Scanned {scanned_files} files "
                                    f"({queued_files} new/updated so far)..."
                                )
                                last_progress = now
                            continue
                    discovery_batch.append((
                        full_path, full_dir, fname, Path(fname).suffix.lower(),
                        stat.st_size, stat.st_mtime,
                    ))
                    queued_files += 1
                    with self._scan_status_lock:
                        self._scan_status["scanned"] = scanned_files
                        self._scan_status["queued"] = queued_files
                    if len(discovery_batch) >= 500:
                        flush_discovery()
                    now = time.time()
                    if now - last_progress >= 5:
                        print(f"[INDEX] {queued_files} new/updated files queued "
                              f"({scanned_files} scanned)...")
                        last_progress = now
        flush_discovery()
        print(
            f"[INDEX] Scan complete: {scanned_files} image files seen, "
            f"{queued_files} new/updated queued, {skipped_dirs} unchanged folders skipped. "
            "Finalizing database..."
        )
        with self.lock:
            stale_roots = self._prune_unconfigured_roots(conn)
            pruned_files += stale_roots[0]
            pruned_folders += stale_roots[1]
            if prune_deleted:
                print("[INDEX] Checking for deleted files...")
                # Only prune files/folders belonging to roots that were actually scanned
                ef = {r[0] for r in conn.execute("SELECT path FROM files").fetchall()}
                td = {p for p in (ef - all_files) if p.split("/", 1)[0] in scanned_roots}
                if td:
                    td_list = list(td)
                    for i in range(0, len(td_list), 1000):
                        pruned_files += self._prune_file_records(conn, td_list[i:i + 1000])
                        print(f"[INDEX] Pruned {min(i + 1000, len(td_list))}/{len(td_list)} deleted files...")
                ef2 = {r[0] for r in conn.execute("SELECT path FROM folders").fetchall()}
                td2 = {p for p in (ef2 - all_folders) if p.split("/", 1)[0] in scanned_roots}
                if td2:
                    conn.executemany("DELETE FROM folders WHERE path=?", [(p,) for p in td2])
                    pruned_folders += len(td2)
                    print(f"[INDEX] Pruned {len(td2)} deleted folders")
            else:
                missing = self._prune_missing_folders(conn, scanned_roots)
                pruned_files += missing[0]
                pruned_folders += missing[1]
                print("[INDEX] Full deleted-file prune skipped for quick startup scan")
            if force or queued_files or pruned_files or pruned_folders:
                self._refresh_folder_aggregates(conn)
            conn.commit()
        elapsed = time.time() - t0
        print(f"[INDEX] Discovery finished in {elapsed:.2f}s; {queued_files} files await background processing")
        if pruned_files or pruned_folders:
            print(f"[INDEX] Cleanup: {pruned_folders} folders, {pruned_files} files removed from DB")
        if prune_deleted or pruned_files:
            self._refresh_tag_counts()
        self._search_cache = {}
        self._search_cache_key = None
        print("[INDEX] Checking search index...")
        self._ensure_search_index()
        self._processing_wakeup.set()
        with self._scan_status_lock:
            self._scan_status.update({
                "active": False, "scanned": scanned_files,
                "total": max(int(self._scan_status.get("total") or 0), scanned_files),
                "queued": queued_files,
            })
        return {"queued": queued_files, "files": scanned_files, "seconds": elapsed}

    def scan_status(self):
        with self._scan_status_lock:
            return dict(self._scan_status)

    def finish_scan_with_error(self, error):
        with self._scan_status_lock:
            self._scan_status["active"] = False
            self._scan_status["error"] = str(error)[:300]

    def _map_absolute_path(self, absolute_path):
        candidate = os.path.abspath(absolute_path)
        for root_name, root_abs in self.roots.items():
            try:
                if os.path.commonpath((candidate, root_abs)) != os.path.commonpath((root_abs, root_abs)):
                    continue
            except (ValueError, OSError):
                continue
            relative = os.path.relpath(candidate, root_abs).replace("\\", "/")
            db_path = root_name if relative == "." else root_name + "/" + relative
            return root_name, root_abs, db_path
        return None

    def _ensure_folder_chain(self, conn, root_name, root_abs, absolute_dir):
        relative = os.path.relpath(absolute_dir, root_abs)
        parts = [] if relative == "." else relative.replace("\\", "/").split("/")
        for depth in range(len(parts) + 1):
            current_parts = parts[:depth]
            current_abs = os.path.join(root_abs, *current_parts) if current_parts else root_abs
            if not os.path.isdir(current_abs):
                continue
            current_path = root_name + ("/" + "/".join(current_parts) if current_parts else "")
            parent = current_path.rsplit("/", 1)[0] if "/" in current_path else ""
            try:
                with os.scandir(current_abs) as entries:
                    visible = [entry for entry in entries if not entry.name.startswith(".")]
                images = [entry.name for entry in visible if entry.is_file() and Path(entry.name).suffix.lower() in IMAGE_EXTENSIONS]
                has_subs = int(any(entry.is_dir() for entry in visible))
                folder_mtime = os.path.getmtime(current_abs)
            except OSError:
                images = []
                has_subs = 0
                folder_mtime = 0
            cover = current_path + "/" + images[0] if images else None
            conn.execute("""
                INSERT INTO folders (
                    path, name, parent, file_count, has_subfolders,
                    mtime, cover_image
                ) VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(path) DO UPDATE SET
                    name=excluded.name,
                    parent=excluded.parent,
                    file_count=excluded.file_count,
                    has_subfolders=excluded.has_subfolders,
                    mtime=excluded.mtime,
                    cover_image=excluded.cover_image
            """, (
                current_path,
                root_name if not current_parts else current_parts[-1],
                parent,
                len(images),
                has_subs,
                folder_mtime,
                cover,
            ))

    def sync_changed_paths(self, paths):
        """Apply debounced filesystem events without walking every gallery root."""
        conn = self._get_conn()
        expanded = set(os.path.abspath(path) for path in paths if path)
        for path in list(expanded):
            lower = path.lower()
            if lower.endswith("_meta.txt"):
                base = path[:-9]
            elif lower.endswith(".txt"):
                base = path[:-4]
            else:
                continue
            for extension in IMAGE_EXTENSIONS:
                candidate = base + extension
                if os.path.isfile(candidate):
                    expanded.add(candidate)

        queued = removed = folder_changes = 0
        affected_folders = set()
        with self.lock:
            for absolute_path in expanded:
                mapped = self._map_absolute_path(absolute_path)
                if not mapped:
                    continue
                root_name, root_abs, db_path = mapped
                extension = Path(absolute_path).suffix.lower()
                if extension in IMAGE_EXTENSIONS:
                    if os.path.isfile(absolute_path):
                        try:
                            stat = os.stat(absolute_path)
                        except OSError:
                            continue
                        row = conn.execute("SELECT mtime FROM files WHERE path=?", (db_path,)).fetchone()
                        if row and abs((row[0] or 0) - stat.st_mtime) < 0.01:
                            continue
                        self._ensure_folder_chain(conn, root_name, root_abs, os.path.dirname(absolute_path))
                        folder = db_path.rsplit("/", 1)[0] if "/" in db_path else root_name
                        affected_folders.add(folder)
                        conn.execute(self.DISCOVERY_UPSERT_SQL, (
                            db_path, folder, os.path.basename(absolute_path), extension,
                            stat.st_size, stat.st_mtime,
                        ))
                        queued += 1
                    else:
                        if conn.execute("SELECT 1 FROM files WHERE path=?", (db_path,)).fetchone():
                            affected_folders.add(db_path.rsplit("/", 1)[0] if "/" in db_path else root_name)
                            removed += self._prune_file_records(conn, [db_path])
                elif not os.path.exists(absolute_path):
                    parent = db_path.rsplit("/", 1)[0] if "/" in db_path else ""
                    if parent:
                        affected_folders.add(parent)
                    file_rows = conn.execute(
                        "SELECT path FROM files WHERE folder=? OR folder LIKE ?",
                        (db_path, db_path + "/%"),
                    ).fetchall()
                    if file_rows:
                        removed += self._prune_file_records(conn, [row[0] for row in file_rows])
                    cursor = conn.execute(
                        "DELETE FROM folders WHERE path=? OR path LIKE ?",
                        (db_path, db_path + "/%"),
                    )
                    if cursor.rowcount and cursor.rowcount > 0:
                        folder_changes += cursor.rowcount
            if queued or removed or folder_changes:
                self._refresh_folder_branches(conn, affected_folders)
                conn.commit()
        if queued or removed or folder_changes:
            self._search_cache = {}
            self._search_cache_key = None
            if removed:
                self._tag_counts_dirty.set()
            self._processing_wakeup.set()
            print(f"[GALLERY] Filesystem sync: {queued} queued, {removed} removed")
        return {"queued": queued, "removed": removed, "folders": folder_changes}

    def _refresh_folder_aggregates(self, conn=None):
        """Cache descendant counts and newest dates for fast folder-tree queries."""
        conn = conn or self._get_conn()
        folder_rows = conn.execute("SELECT path, parent FROM folders").fetchall()
        stats = {row[0]: [0, 0.0] for row in folder_rows}
        parents = {row[0]: row[1] or "" for row in folder_rows}
        for folder, count, latest in conn.execute(
            "SELECT folder, COUNT(*), COALESCE(MAX(mtime), 0) FROM files GROUP BY folder"
        ).fetchall():
            if folder in stats:
                stats[folder] = [int(count or 0), float(latest or 0)]
        for path in sorted(stats, key=lambda value: value.count("/"), reverse=True):
            parent = parents.get(path, "")
            if parent in stats:
                stats[parent][0] += stats[path][0]
                stats[parent][1] = max(stats[parent][1], stats[path][1])
        conn.executemany(
            "UPDATE folders SET subtree_file_count=?, latest_mtime=? WHERE path=?",
            [(values[0], values[1], path) for path, values in stats.items()],
        )

    def _refresh_folder_branches(self, conn, folders):
        """Refresh counts only for changed folders and their ancestors."""
        pending = set()
        for folder in folders or ():
            current = (folder or "").strip("/")
            while current:
                pending.add(current)
                row = conn.execute(
                    "SELECT parent FROM folders WHERE path=?", (current,)
                ).fetchone()
                parent = row[0] if row else (current.rsplit("/", 1)[0] if "/" in current else "")
                if not parent or parent == current:
                    break
                current = parent

        for folder in sorted(pending, key=lambda value: value.count("/"), reverse=True):
            if not conn.execute("SELECT 1 FROM folders WHERE path=?", (folder,)).fetchone():
                continue
            direct = conn.execute(
                "SELECT COUNT(*), COALESCE(MAX(mtime), 0), MIN(path) FROM files WHERE folder=?",
                (folder,),
            ).fetchone()
            children = conn.execute(
                """
                SELECT COUNT(*), COALESCE(SUM(subtree_file_count), 0),
                       COALESCE(MAX(latest_mtime), 0)
                FROM folders WHERE parent=? AND path!=parent
                """,
                (folder,),
            ).fetchone()
            direct_count = int(direct[0] or 0)
            subtree_count = direct_count + int(children[1] or 0)
            latest = max(float(direct[1] or 0), float(children[2] or 0))
            conn.execute(
                """
                UPDATE folders
                SET file_count=?, has_subfolders=?, cover_image=?,
                    subtree_file_count=?, latest_mtime=?
                WHERE path=?
                """,
                (
                    direct_count, int(children[0] or 0) > 0, direct[2],
                    subtree_count, latest, folder,
                ),
            )

    def generate_missing_thumbnails(self, workers=None, progress=None):
        """Generate stale/missing thumbnails from indexed files in bounded batches.

        The old maintenance path walked every gallery root twice and retained all
        source paths in memory. SQLite already owns that inventory, so this pass
        streams rows by rowid and only opens images whose thumbnail is missing or
        older than the source file.
        """
        if not HAS_PIL:
            return {"ok": False, "error": "Pillow is required for thumbnail generation"}
        workers = workers or min(8, (os.cpu_count() or 4))
        workers = max(1, min(32, int(workers)))
        conn = self._get_conn()
        with self.lock:
            total = int(conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] or 0)
        checked = generated = skipped = errors = 0
        last_rowid = 0
        started = time.time()

        def report(stage, message):
            if progress:
                progress(stage, message, checked, total, {
                    "generated": generated,
                    "skipped": skipped,
                    "errors": errors,
                })

        def process(file_path):
            abs_path = self.resolve_path(file_path)
            if not abs_path or not os.path.isfile(abs_path):
                return "error"
            thumb_path = get_thumb_path(self.thumb_dir, file_path)
            try:
                if (
                    os.path.isfile(thumb_path)
                    and os.path.getmtime(thumb_path) >= os.path.getmtime(abs_path)
                ):
                    return "skipped"
            except OSError:
                pass
            result = ensure_thumbnail(self.thumb_dir, file_path, abs_path)
            return "generated" if result == thumb_path and os.path.isfile(thumb_path) else "error"

        report("prepare", "Checking indexed thumbnails")
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            while True:
                with self.lock:
                    rows = conn.execute(
                        "SELECT rowid, path FROM files WHERE rowid>? ORDER BY rowid LIMIT 500",
                        (last_rowid,),
                    ).fetchall()
                if not rows:
                    break
                last_rowid = rows[-1][0]
                for outcome in executor.map(process, [row[1] for row in rows]):
                    checked += 1
                    if outcome == "generated":
                        generated += 1
                    elif outcome == "skipped":
                        skipped += 1
                    else:
                        errors += 1
                report("generate", "Checking and generating thumbnails")

        elapsed = time.time() - started
        report("complete", "Thumbnail generation complete")
        print(
            f"[THUMBS] Database pass complete in {elapsed:.1f}s: "
            f"{generated} generated, {skipped} current, {errors} errors"
        )
        return {
            "ok": True,
            "total": total,
            "generated": generated,
            "skipped": skipped,
            "errors": errors,
            "seconds": elapsed,
            "workers": workers,
        }

    def _refresh_tag_counts(self):
        conn = self._get_conn()
        with self.lock:
            conn.execute("CREATE TEMP TABLE IF NOT EXISTS tag_counts (tag_id INTEGER PRIMARY KEY, count INTEGER)")
            conn.execute("DELETE FROM tag_counts")
            conn.execute("INSERT INTO tag_counts SELECT tag_id, COUNT(*) FROM file_tags GROUP BY tag_id")
            conn.execute("UPDATE tags SET count = COALESCE((SELECT count FROM tag_counts WHERE tag_counts.tag_id = tags.id), 0)")
            conn.execute("DELETE FROM tags WHERE count = 0")
            conn.execute("DROP TABLE IF EXISTS tag_counts")
            conn.commit()
        self._tag_counts_dirty.clear()

    def processing_status(self):
        conn = self._get_conn()
        with self.lock:
            total = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
            pending = conn.execute(
                "SELECT COUNT(*) FROM files WHERE processing_state=0"
            ).fetchone()[0]
            failed = conn.execute(
                "SELECT COUNT(*) FROM files WHERE processing_state=2"
            ).fetchone()[0]
            model_pending = conn.execute(
                "SELECT COUNT(*) FROM files WHERE model_scanned=0 AND processing_state!=0"
            ).fetchone()[0]
        with self._processing_lock:
            threads = sum(1 for thread in self._processing_threads if thread.is_alive())
            active = self._processing_active
            done = self._processing_session_done
        return {
            "total": int(total or 0),
            "pending": int(pending or 0),
            "failed": int(failed or 0),
            "model_pending": int(model_pending or 0),
            "active": active,
            "workers": threads,
            "done": done,
            "paused": self._processing_paused.is_set(),
            "running": threads > 0 and not self._processing_stop.is_set(),
        }

    def prioritize_processing(self, file_path):
        if not file_path:
            return
        with self._processing_lock:
            if file_path not in self._processing_priority_set:
                self._processing_priority.appendleft(file_path)
                self._processing_priority_set.add(file_path)
        self._processing_wakeup.set()

    def pause_processing(self):
        self._processing_paused.set()

    def resume_processing(self):
        self._processing_paused.clear()
        self._processing_wakeup.set()

    def start_processing(self, workers=2):
        workers = max(1, min(8, int(workers or 2)))
        with self._processing_lock:
            alive = [thread for thread in self._processing_threads if thread.is_alive()]
            self._processing_threads = alive
            self._processing_stop.clear()
            for number in range(len(alive), workers):
                thread = threading.Thread(
                    target=self._processing_worker,
                    name=f"gallery-processor-{number + 1}",
                    daemon=True,
                )
                thread.start()
                self._processing_threads.append(thread)
        self._processing_wakeup.set()

    def stop_processing(self):
        self._processing_stop.set()
        self._processing_wakeup.set()

    def _claim_processing_file(self, conn):
        with self._processing_lock:
            while self._processing_priority:
                path = self._processing_priority.popleft()
                self._processing_priority_set.discard(path)
                if path in self._processing_claimed:
                    continue
                row = conn.execute(
                    "SELECT processing_state FROM files WHERE path=?", (path,)
                ).fetchone()
                if row and row[0] == 0:
                    self._processing_claimed.add(path)
                    return path
            rows = conn.execute(
                "SELECT path FROM files WHERE processing_state=0 ORDER BY mtime DESC LIMIT 128"
            ).fetchall()
            for row in rows:
                path = row[0]
                if path not in self._processing_claimed:
                    self._processing_claimed.add(path)
                    return path
        return None

    def _write_thumbnail_bytes(self, file_path, raw_data):
        if not HAS_PIL:
            return 0, 0
        abs_path = self.resolve_path(file_path)
        if not abs_path:
            return 0, 0
        thumb_path = get_thumb_path(self.thumb_dir, file_path)
        tmp_path = f"{thumb_path}.{threading.get_ident()}.tmp"
        os.makedirs(os.path.dirname(thumb_path), exist_ok=True)
        with Image.open(BytesIO(raw_data)) as image:
            width, height = image.size
            image.thumbnail(THUMB_SIZE, LANCZOS)
            if image.mode not in ("RGB", "L"):
                image = image.convert("RGB")
            image.save(tmp_path, "WEBP", quality=80, method=4)
        os.replace(tmp_path, thumb_path)
        return width, height

    def _process_file(self, conn, file_path):
        abs_path = self.resolve_path(file_path)
        if not abs_path or not os.path.isfile(abs_path):
            raise FileNotFoundError(abs_path or file_path)
        with open(abs_path, "rb") as handle:
            raw_data = handle.read()
        metadata = get_image_metadata(abs_path, raw_data=raw_data)
        width = height = 0
        if HAS_PIL:
            width, height = self._write_thumbnail_bytes(file_path, raw_data)
        model_name = self._model_from_meta(metadata)
        family_key = self._model_family(model_name)[0]
        metadata_json = json.dumps(metadata) if metadata else None

        with self.lock:
            current = conn.execute(
                "SELECT folder, name, metadata_json, width, height, mtime FROM files WHERE path=?",
                (file_path,),
            ).fetchone()
            if not current:
                return
            replace_search = bool(current[2] or current[3] or current[4]) and not self._processing_rebuild_fts
            conn.execute("""
                UPDATE files SET
                    width=?, height=?, has_metadata=?, metadata_json=?,
                    processing_state=1, processing_error=NULL,
                    processed_mtime=mtime, model_name=?, model_family=?,
                    model_scanned=1
                WHERE path=?
            """, (
                width, height, 1 if metadata else 0, metadata_json,
                model_name, family_key, file_path,
            ))
            self._index_tags(conn, file_path, metadata)
            self._index_search(
                conn, file_path, current[0], current[1], metadata,
                replace=replace_search,
            )
            conn.commit()
        self._tag_counts_dirty.set()

    def _process_model_backfill_batch(self, conn, limit=100):
        if not self._model_backfill_lock.acquire(blocking=False):
            return 0
        try:
            with self.lock:
                rows = conn.execute("""
                    SELECT path, metadata_json
                    FROM files
                    WHERE processing_state!=0 AND model_scanned=0
                    ORDER BY path
                    LIMIT ?
                """, (limit,)).fetchall()
            if not rows:
                return 0
            updates = []
            for path, metadata_json in rows:
                metadata = self._safe_meta_load(metadata_json)
                model_name = self._model_from_meta(metadata)
                family_key = self._model_family(model_name)[0]
                updates.append((model_name, family_key, path))
            with self.lock:
                conn.executemany(
                    "UPDATE files SET model_name=?, model_family=?, model_scanned=1 WHERE path=?",
                    updates,
                )
                conn.commit()
            return len(updates)
        finally:
            self._model_backfill_lock.release()

    def _processing_worker(self):
        conn = self._get_conn()
        while not self._processing_stop.is_set():
            if self._processing_paused.is_set():
                self._processing_wakeup.wait(0.5)
                self._processing_wakeup.clear()
                continue
            file_path = self._claim_processing_file(conn)
            if file_path:
                with self._processing_lock:
                    self._processing_active += 1
                ok = False
                try:
                    self._process_file(conn, file_path)
                    ok = True
                except Exception as exc:
                    attempts = self._processing_failures.get(file_path, 0) + 1
                    self._processing_failures[file_path] = attempts
                    with self.lock:
                        conn.execute(
                            "UPDATE files SET processing_state=?, processing_error=? WHERE path=?",
                            (2 if attempts >= 3 else 0, str(exc)[:500], file_path),
                        )
                        conn.commit()
                    if attempts >= 3:
                        print(f"[GALLERY] Background processing failed for {file_path}: {exc}")
                finally:
                    with self._processing_lock:
                        self._processing_active = max(0, self._processing_active - 1)
                        self._processing_claimed.discard(file_path)
                        if ok:
                            self._processing_session_done += 1
                            self._processing_failures.pop(file_path, None)
                continue

            backfilled = self._process_model_backfill_batch(conn)
            if backfilled:
                with self._processing_lock:
                    self._processing_session_done += backfilled
                time.sleep(0.01)
                continue

            with self._processing_lock:
                idle = self._processing_active == 0
            if idle and self._tag_counts_dirty.is_set():
                self._refresh_tag_counts()
                self._ensure_search_index()
            if idle and self._processing_rebuild_fts:
                pending = conn.execute(
                    "SELECT 1 FROM files WHERE processing_state=0 LIMIT 1"
                ).fetchone()
                if not pending:
                    self._processing_rebuild_fts = False
                    self._ensure_search_index()
            self._processing_wakeup.wait(0.5)
            self._processing_wakeup.clear()

    def _ensure_search_index(self):
        if not self.has_fts:
            return
        conn = self._get_conn()
        with self.lock:
            try:
                file_count = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
                search_count = conn.execute("SELECT COUNT(*) FROM gallery_search").fetchone()[0]
            except sqlite3.OperationalError:
                return
        self.search_index_ready = (search_count == file_count)
        if file_count and not self.search_index_ready and not self._search_index_notice_shown:
            print(
                f"[SEARCH] Full-text search index is incomplete "
                f"({search_count}/{file_count}). Using legacy search until Settings > Maintenance > Rebuild search is run."
            )
            self._search_index_notice_shown = True

    def rebuild_search_index(self, progress=None):
        """Rebuild the FTS5 full-text search index from scratch.

        Optimised for large libraries:
        - Processes rows in chunks of 2 000 to avoid loading the entire
          metadata_json column into RAM at once.
        - Computes the heavy regex / JSON work *outside* the lock so other
          gallery requests can proceed between chunks.
        - Uses executemany() for batch INSERTs instead of one INSERT per row.

        Expected throughput: ~10 000–40 000 rows/s on a typical machine,
        so 37 000 files should finish in well under 10 seconds.
        """
        if not self.has_fts:
            return {"ok": False, "error": "FTS5 search is not available"}
        conn = self._get_conn()
        t0 = time.time()

        # Clear the table once, then rebuild in chunks.
        with self.lock:
            total = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
            conn.execute("DELETE FROM gallery_search")
            conn.commit()

        CHUNK = 2000
        last_rowid = 0
        processed = 0

        if progress:
            progress("prepare", "Preparing search index", 0, total)

        while True:
            # Fetch one chunk — release lock between chunks so gallery stays responsive.
            with self.lock:
                chunk_rows = conn.execute(
                    """
                    SELECT rowid, path, folder, name, metadata_json
                    FROM files
                    WHERE rowid > ?
                    ORDER BY rowid
                    LIMIT ?
                    """,
                    (last_rowid, CHUNK),
                ).fetchall()
            if not chunk_rows:
                break

            # Compute FTS text outside the lock (regex + JSON parsing is expensive).
            batch = []
            for _rowid, path, folder, name, metadata_json in chunk_rows:
                meta = {}
                if metadata_json:
                    try:
                        meta = json.loads(metadata_json)
                    except (TypeError, json.JSONDecodeError):
                        meta = {}
                tags = " ".join(tag for tag, _ in self._extract_tags(meta))
                metadata_text = self._search_text(name, meta)
                batch.append((path, folder, name or "", tags, metadata_text))

            # Batch-insert this chunk.
            with self.lock:
                conn.executemany(
                    "INSERT INTO gallery_search(path, folder, name, tags, metadata) VALUES (?,?,?,?,?)",
                    batch,
                )
                conn.commit()

            processed += len(chunk_rows)
            last_rowid = chunk_rows[-1][0]
            elapsed = time.time() - t0
            print(f"[SEARCH] Rebuilt {processed}/{total} files ({elapsed:.1f}s elapsed)...")
            if progress:
                progress("rebuild", "Rebuilding search data", processed, total)

        self._search_cache = {}
        self._search_cache_key = None
        self.search_index_ready = True
        self._search_index_notice_shown = False
        elapsed = time.time() - t0
        print(f"[SEARCH] Full-text index rebuilt: {processed} files in {elapsed:.2f}s")
        if progress:
            progress("complete", "Search index complete", total, total)
        return {"ok": True, "files": processed, "seconds": elapsed}

    def resolve_path(self, db_path):
        """Resolve a DB path (root_name/rel/path) to an absolute filesystem path."""
        if not db_path:
            return None
        parts = db_path.split("/", 1)
        root_name = parts[0]
        remainder = parts[1] if len(parts) > 1 else ""
        root_abs = self.roots.get(root_name)
        if not root_abs:
            return None
        if not remainder:
            return root_abs
        return safe_resolve(root_abs, remainder)

    @staticmethod
    def _extract_tags(meta):
        """Extract searchable tags from metadata. Returns list of (name, type) tuples."""
        tags = []
        raw = meta.get("parameters", "")
        if not raw:
            raw = meta.get("prompt", "")
        if not raw:
            return tags
        parsed = parse_sd_parameters(raw)
        # Prompt tags (split on comma)
        prompt = parsed.get("prompt", "")
        if prompt:
            for tag in re.split(r",\s*", prompt):
                tag = re.sub(r"[\\/()\[\]{}]+", "", tag).strip().lower()
                tag = re.sub(r"\s+", " ", tag)
                if len(tag) >= 2 and len(tag) <= 80 and not tag.startswith("<"):
                    tags.append((tag, "prompt"))
        # Settings as tags (model, sampler, etc.)
        settings = parsed.get("settings", {})
        for key in ("Model", "Sampler", "Source"):
            val = settings.get(key, "").strip()
            if val and len(val) >= 2:
                tags.append((f"{key.lower()}:{val.lower()}", "setting"))
        # LoRA extraction from prompt
        for match in re.finditer(r"<lora:([\w\s._-]+?)(?::[\d.]+)?>", prompt, re.IGNORECASE):
            tags.append((f"lora:{match.group(1).strip().lower()}", "lora"))
        return tags

    def _index_tags(self, conn, file_path, meta):
        """Extract tags from metadata and store in tags/file_tags tables."""
        # Remove old tags for this file
        conn.execute("DELETE FROM file_tags WHERE file_path=?", (file_path,))
        if not meta:
            return
        tags = self._extract_tags(meta)
        for tag_name, tag_type in tags:
            # Insert or get tag
            conn.execute("INSERT OR IGNORE INTO tags (name, count) VALUES (?, 0)", (tag_name,))
            row = conn.execute("SELECT id FROM tags WHERE name=?", (tag_name,)).fetchone()
            if row:
                conn.execute("INSERT OR IGNORE INTO file_tags (file_path, tag_id, tag_type) VALUES (?,?,?)",
                             (file_path, row[0], tag_type))

    @staticmethod
    def _flatten_metadata_values(value, out, limit=1200):
        if len(out) >= limit:
            return
        if isinstance(value, dict):
            for k, v in value.items():
                if len(out) >= limit:
                    break
                out.append(str(k))
                GalleryDB._flatten_metadata_values(v, out, limit)
        elif isinstance(value, (list, tuple)):
            for item in value:
                if len(out) >= limit:
                    break
                GalleryDB._flatten_metadata_values(item, out, limit)
        elif value is not None:
            text = str(value).strip()
            if text:
                out.append(text[:4000])

    @staticmethod
    def _fts_query(query):
        raw_terms = re.findall(r"[A-Za-z0-9_.-]+", (query or "").lower())
        terms = []
        for term in raw_terms:
            term = term.strip("_:.-")
            if term and re.search(r"[a-z0-9]", term):
                terms.append(term)
        if not terms:
            return ""
        # Prefix terms make partial searches like "illustr" match "Illustrious".
        return " ".join(f"{term}*" for term in terms[:12])

    def _search_text(self, name, meta):
        parts = [name or ""]
        tag_names = [tag for tag, _tag_type in self._extract_tags(meta or {})]
        parts.extend(tag_names)
        flat = []
        self._flatten_metadata_values(meta or {}, flat)
        parts.extend(flat)
        text = " ".join(p for p in parts if p)
        # Also index a split variant so sub-words inside names joined by _ . : - /
        # (e.g. "CyberRealistic_zit_v5.0") become individually searchable — this
        # lets you find a checkpoint by a fragment like "zit". The original joined
        # tokens stay indexed too, so exact tag search (model:..., lora:...,
        # score_7) keeps working.
        split = re.sub(r"[._:/\\-]+", " ", text)
        if split != text:
            text = text + " " + split
        return text

    def _index_search(self, conn, file_path, folder, name, meta, replace=True):
        if not self.has_fts:
            return
        tags = " ".join(tag for tag, _tag_type in self._extract_tags(meta or {}))
        metadata = self._search_text(name, meta or {})
        try:
            # `path` is UNINDEXED in FTS5, so DELETE WHERE path=? is a full-table
            # scan — O(n) per call, O(n^2) over a full rebuild. Callers that just
            # cleared the table (rebuild) pass replace=False to skip it.
            if replace:
                conn.execute("DELETE FROM gallery_search WHERE path=?", (file_path,))
            conn.execute(
                "INSERT INTO gallery_search(path, folder, name, tags, metadata) VALUES (?,?,?,?,?)",
                (file_path, folder, name or "", tags, metadata)
            )
        except sqlite3.OperationalError as e:
            self.has_fts = False
            print(f"[SEARCH] FTS index disabled after error: {e}")

    @staticmethod
    def _safe_meta_load(metadata_json):
        if not metadata_json:
            return {}
        try:
            meta = json.loads(metadata_json)
            return meta if isinstance(meta, dict) else {}
        except (TypeError, json.JSONDecodeError):
            return {}

    @staticmethod
    def _clean_model_name(value):
        if value is None:
            return ""
        if isinstance(value, (list, tuple, dict)):
            return ""
        text = str(value).strip().strip('"').strip("'")
        if not text:
            return ""
        text = os.path.basename(text.replace("\\", "/"))
        text = re.sub(r"\.(safetensors|ckpt|pt|pth|bin|onnx)$", "", text, flags=re.IGNORECASE)
        text = re.sub(r"[_]+", " ", text).strip()
        return text[:120]

    @staticmethod
    def _find_model_value(value, depth=0):
        if depth > 6:
            return ""
        keys = {
            "model", "model_name", "base_model", "basemodel", "base_model_name",
            "checkpoint", "checkpoint_name", "ckpt_name", "unet_name",
            "modelname", "modelnamefull",
        }
        if isinstance(value, dict):
            for k, v in value.items():
                key = str(k).replace(" ", "").replace("-", "_").lower()
                if key in keys:
                    cleaned = GalleryDB._clean_model_name(v)
                    if cleaned and re.search(r"[A-Za-z]", cleaned):
                        return cleaned
            for v in value.values():
                found = GalleryDB._find_model_value(v, depth + 1)
                if found:
                    return found
        elif isinstance(value, list):
            for item in value[:80]:
                found = GalleryDB._find_model_value(item, depth + 1)
                if found:
                    return found
        return ""

    @staticmethod
    def _model_from_meta(meta):
        if not isinstance(meta, dict) or not meta:
            return ""
        raw = meta.get("parameters") or ""
        if raw:
            parsed = parse_sd_parameters(str(raw))
            settings = parsed.get("settings") or {}
            for key in ("Model", "baseModel", "Base model", "Checkpoint"):
                model = GalleryDB._clean_model_name(settings.get(key))
                if model:
                    return model
        if isinstance(meta.get("prompt"), str) and meta.get("prompt", "").lstrip().startswith("{"):
            comfy_meta = dict(meta)
            comfy_meta = _metadata_mod.extract_comfyui_prompt(comfy_meta)
            raw = comfy_meta.get("parameters") or ""
            if raw:
                parsed = parse_sd_parameters(str(raw))
                settings = parsed.get("settings") or {}
                model = GalleryDB._clean_model_name(settings.get("Model"))
                if model:
                    return model
        return GalleryDB._find_model_value(meta)

    @classmethod
    def _model_family(cls, model_name):
        text = (model_name or "").lower()
        for patterns, label, color in cls.MODEL_FAMILY_RULES:
            if any(p in text for p in patterns):
                key = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")
                return key, label, color
        if model_name:
            idx = int(hashlib.md5(model_name.lower().encode("utf-8")).hexdigest()[:2], 16) % len(cls.MODEL_COLORS)
            return "other", "Other models", cls.MODEL_COLORS[idx]
        return "unknown", "Unknown model", "#64748b"

    @classmethod
    def _file_model_info(cls, model, stored_family="unknown"):
        family_key, family_label, family_color = cls._model_family(model)
        if not model and stored_family:
            family_key = stored_family
        model_label = model or family_label
        exact_key = re.sub(r"[^a-z0-9]+", "-", (model_label or "unknown").lower()).strip("-") or "unknown"
        return {
            "model_name": model,
            "model_family_key": family_key,
            "model_family_label": family_label,
            "model_group_color": family_color,
            "model_exact_key": exact_key,
            "model_exact_label": model_label,
        }

    @classmethod
    def _file_dict(cls, r, include_model_info=False):
        data = {
            "path": r[0], "name": r[1], "folder": r[2], "ext": r[3], "size": r[4],
            "mtime": r[5], "width": r[6], "height": r[7],
            "has_metadata": bool(r[8]), "favorite": bool(r[9]),
            "processing": r[10] == 0,
        }
        if include_model_info:
            data.update(cls._file_model_info(
                r[11] if len(r) > 11 else "",
                r[12] if len(r) > 12 else "unknown",
            ))
        return data

    @staticmethod
    def _metadata_filter_sql(metadata_filter, alias=""):
        """Return a safe SQL predicate for the tri-state metadata filter.

        Only successfully processed files may appear in the ``without`` view.
        Pending or failed files must never look like metadata-free cleanup
        candidates merely because their metadata has not been read.
        """
        prefix = f"{alias}." if alias else ""
        if metadata_filter == "with":
            return f"{prefix}processing_state=1 AND {prefix}has_metadata=1"
        if metadata_filter == "without":
            return f"{prefix}processing_state=1 AND {prefix}has_metadata=0"
        return ""

    def _delete_search(self, conn, file_path):
        if self.has_fts:
            try:
                conn.execute("DELETE FROM gallery_search WHERE path=?", (file_path,))
            except sqlite3.OperationalError:
                pass

    def _delete_search_many(self, conn, paths):
        """Delete many FTS rows with one scan per chunk.

        `path` is UNINDEXED in the FTS5 table, so `DELETE ... WHERE path=?`
        scans the whole FTS table. Doing that once per pruned file made the
        final deleted-file pass crawl on large libraries. A single `IN (...)`
        statement still scans, but only once for hundreds of paths.
        """
        if not self.has_fts:
            return
        paths = list(paths)
        CHUNK = 500  # stay comfortably below SQLite's common 999 variable limit
        try:
            for i in range(0, len(paths), CHUNK):
                sub = paths[i:i + CHUNK]
                if not sub:
                    continue
                placeholders = ",".join("?" for _ in sub)
                conn.execute(f"DELETE FROM gallery_search WHERE path IN ({placeholders})", sub)
        except sqlite3.OperationalError:
            pass

    def _prune_file_records(self, conn, paths):
        """Remove DB/search/thumb records for files that no longer exist on disk."""
        paths = list(paths)
        if not paths:
            return 0
        conn.executemany("DELETE FROM file_tags WHERE file_path=?", [(p,) for p in paths])
        conn.executemany("DELETE FROM file_collections WHERE file_path=?", [(p,) for p in paths])
        self._delete_search_many(conn, paths)
        conn.executemany("DELETE FROM files WHERE path=?", [(p,) for p in paths])
        pruned = 0
        for p in paths:
            thumb = get_thumb_path(self.thumb_dir, p)
            if os.path.exists(thumb):
                try:
                    os.remove(thumb)
                except Exception:
                    pass
            pruned += 1
        return pruned

    def _prune_missing_folders(self, conn, scanned_roots):
        """Remove folders that are in SQLite but no longer exist on disk.

        Quick startup scans intentionally avoid comparing every file in the DB.
        Folder existence checks are much cheaper, and they keep the sidebar from
        showing deleted directories with broken thumbnails.
        """
        if not scanned_roots:
            return 0, 0
        rows = []
        for root in scanned_roots:
            rows.extend(conn.execute(
                "SELECT path FROM folders WHERE path=? OR path LIKE ? ORDER BY LENGTH(path)",
                (root, root + "/%")
            ).fetchall())

        missing_roots = []
        pruned_files = 0
        pruned_folders = 0
        for (folder_path,) in rows:
            if any(folder_path == m or folder_path.startswith(m + "/") for m in missing_roots):
                continue
            abs_path = self.resolve_path(folder_path)
            if abs_path and os.path.isdir(abs_path):
                continue
            missing_roots.append(folder_path)

            file_rows = conn.execute(
                "SELECT path FROM files WHERE folder=? OR folder LIKE ?",
                (folder_path, folder_path + "/%")
            ).fetchall()
            paths = [r[0] for r in file_rows]
            if paths:
                pruned_files += self._prune_file_records(conn, paths)
            cur = conn.execute(
                "DELETE FROM folders WHERE path=? OR path LIKE ?",
                (folder_path, folder_path + "/%")
            )
            pruned_folders += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0

        if pruned_files or pruned_folders:
            print(f"[INDEX] Pruned {pruned_folders} missing folders and {pruned_files} files")
        return pruned_files, pruned_folders

    def _prune_unconfigured_roots(self, conn):
        """Remove DB rows for gallery roots that are no longer configured.

        Settings stores the selected root folders, while SQLite stores the indexed
        tree. If a root is removed from Settings, scans no longer visit it, so its
        old rows need an explicit cleanup pass.
        """
        configured = set(self.roots.keys())
        if not configured:
            return 0, 0
        roots = {
            (r[0] or "").split("/", 1)[0]
            for r in conn.execute("SELECT path FROM folders UNION SELECT path FROM files").fetchall()
            if r[0]
        }
        stale_roots = sorted(r for r in roots if r and r not in configured)
        if not stale_roots:
            return 0, 0
        pruned_files = 0
        pruned_folders = 0
        for root in stale_roots:
            paths = [
                r[0] for r in conn.execute(
                    "SELECT path FROM files WHERE path=? OR path LIKE ?",
                    (root, root + "/%")
                ).fetchall()
            ]
            if paths:
                pruned_files += self._prune_file_records(conn, paths)
            cur = conn.execute(
                "DELETE FROM folders WHERE path=? OR path LIKE ?",
                (root, root + "/%")
            )
            pruned_folders += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        if pruned_files or pruned_folders:
            print(f"[INDEX] Pruned {pruned_folders} unconfigured-root folders and {pruned_files} files")
        return pruned_files, pruned_folders

    def _prune_empty_folder_records(self, conn, candidate_folders):
        """Remove indexed folder branches that no longer contain indexed images."""
        candidates = set()
        for folder in candidate_folders or []:
            folder = (folder or "").strip("/")
            while folder:
                candidates.add(folder)
                if "/" not in folder:
                    break
                folder = folder.rsplit("/", 1)[0]
        pruned = 0
        for folder in sorted(candidates, key=len, reverse=True):
            has_files = conn.execute(
                "SELECT 1 FROM files WHERE folder=? OR folder LIKE ? LIMIT 1",
                (folder, folder + "/%")
            ).fetchone()
            if has_files:
                continue
            cur = conn.execute(
                "DELETE FROM folders WHERE path=? OR path LIKE ?",
                (folder, folder + "/%")
            )
            pruned += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        return pruned

    def get_subfolders(self, parent="", sort="name", active_path=""):
        conn = self._get_conn()
        sort = (sort or "name").lower()
        with self.lock:
            rows = conn.execute("""
                SELECT
                    f.path,
                    f.name,
                    f.file_count,
                    EXISTS (
                        SELECT 1
                        FROM folders c
                        WHERE c.parent = f.path
                          AND c.path != c.parent
                          AND c.subtree_file_count > 0
                        LIMIT 1
                    ) AS has_visible_children,
                    f.cover_image,
                    f.latest_mtime
                FROM folders f
                WHERE f.parent = ?
                  AND f.path != ?
                  AND f.subtree_file_count > 0
            """, (parent, parent)).fetchall()
        items = [{"path": r[0], "name": r[1], "count": r[2], "has_children": bool(r[3]), "cover": r[4], "latest": r[5] or 0} for r in rows]
        active_path = (active_path or "").strip("/")
        if sort == "newest":
            items.sort(key=lambda item: (-(item.get("latest") or 0), item["name"].lower()))
        elif sort == "active":
            def active_rank(item):
                path = item.get("path") or ""
                active = bool(active_path and (active_path == path or active_path.startswith(path + "/") or path.startswith(active_path + "/")))
                return (0 if active else 1, item["name"].lower())
            items.sort(key=active_rank)
        else:
            items.sort(key=lambda item: item["name"].lower())
        return items

    def get_files(self, folder="", sort="name", order="asc", page=1, per_page=DEFAULT_PER_PAGE, favorite_only=False, time_filter=None, include_model_info=False, metadata_filter="all"):
        conn = self._get_conn()
        sort_col = {"name": "name", "date": "mtime", "size": "size", "favorite": "favorite"}.get(sort, "name")
        # Favorite sort: favorites first, then by date
        if sort == "favorite":
            order_clause = "favorite DESC, mtime DESC"
        else:
            order_dir = "DESC" if order == "desc" else "ASC"
            order_clause = f"{sort_col} {order_dir}"
        offset = (page - 1) * per_page
        # An empty folder is the Gallery's top-level "All images" view. Folder
        # paths stored in the index always start with a configured root name, so
        # no real folder is lost by reserving the empty value for this purpose.
        where = []
        params = []
        if folder:
            where.append("folder=?")
            params.append(folder)
        if favorite_only:
            where.append("favorite=1")
        metadata_where = self._metadata_filter_sql(metadata_filter)
        if metadata_where:
            where.append(metadata_where)
        if time_filter:
            now = time.time()
            if time_filter == "today":
                cutoff = now - 86400
            elif time_filter == "7days":
                cutoff = now - 7 * 86400
            elif time_filter == "30days":
                cutoff = now - 30 * 86400
            else:
                cutoff = 0
            if cutoff:
                where.append("mtime>=?")
                params.append(cutoff)
        where_sql = " AND ".join(where) if where else "1=1"
        metadata_col = ", model_name, model_family" if include_model_info else ""
        with self.lock:
            total = conn.execute(f"SELECT COUNT(*) FROM files WHERE {where_sql}", params).fetchone()[0]
            rows = conn.execute(
                f"SELECT path, name, folder, ext, size, mtime, width, height, has_metadata, favorite, processing_state{metadata_col} FROM files WHERE {where_sql} ORDER BY {order_clause} LIMIT ? OFFSET ?",
                params + [per_page, offset]).fetchall()
        pages = max(1, (total + per_page - 1) // per_page)
        return {
            "files": [self._file_dict(r, include_model_info) for r in rows],
            "total": total, "page": page, "pages": pages, "per_page": per_page
        }

    def get_timeline_files(self, time_filter="today", sort="date", order="desc", page=1, per_page=DEFAULT_PER_PAGE, include_model_info=False, metadata_filter="all"):
        """Get files across all folders filtered by time."""
        conn = self._get_conn()
        now = time.time()
        cutoffs = {"today": 86400, "7days": 7*86400, "30days": 30*86400}
        cutoff = now - cutoffs.get(time_filter, 86400)
        sort_col = {"name": "name", "date": "mtime", "size": "size"}.get(sort, "mtime")
        order_dir = "DESC" if order == "desc" else "ASC"
        offset = (page - 1) * per_page
        metadata_col = ", model_name, model_family" if include_model_info else ""
        where = ["mtime>=?"]
        params = [cutoff]
        metadata_where = self._metadata_filter_sql(metadata_filter)
        if metadata_where:
            where.append(metadata_where)
        where_sql = " AND ".join(where)
        with self.lock:
            total = conn.execute(f"SELECT COUNT(*) FROM files WHERE {where_sql}", params).fetchone()[0]
            rows = conn.execute(
                f"SELECT path, name, folder, ext, size, mtime, width, height, has_metadata, favorite, processing_state{metadata_col} FROM files WHERE {where_sql} ORDER BY {sort_col} {order_dir} LIMIT ? OFFSET ?",
                params + [per_page, offset]).fetchall()
        pages = max(1, (total + per_page - 1) // per_page)
        return {
            "files": [self._file_dict(r, include_model_info) for r in rows],
            "total": total, "page": page, "pages": pages, "per_page": per_page
        }

    def get_all_favorites(self, sort="date", order="desc", page=1, per_page=DEFAULT_PER_PAGE, include_model_info=False, metadata_filter="all"):
        """Get all favorites across all folders."""
        conn = self._get_conn()
        sort_col = {"name": "name", "date": "mtime", "size": "size"}.get(sort, "mtime")
        order_dir = "DESC" if order == "desc" else "ASC"
        offset = (page - 1) * per_page
        metadata_col = ", model_name, model_family" if include_model_info else ""
        where = ["favorite=1"]
        metadata_where = self._metadata_filter_sql(metadata_filter)
        if metadata_where:
            where.append(metadata_where)
        where_sql = " AND ".join(where)
        with self.lock:
            total = conn.execute(f"SELECT COUNT(*) FROM files WHERE {where_sql}").fetchone()[0]
            rows = conn.execute(
                f"SELECT path, name, folder, ext, size, mtime, width, height, has_metadata, favorite, processing_state{metadata_col} FROM files WHERE {where_sql} ORDER BY {sort_col} {order_dir} LIMIT ? OFFSET ?",
                (per_page, offset)).fetchall()
        pages = max(1, (total + per_page - 1) // per_page)
        return {
            "files": [self._file_dict(r, include_model_info) for r in rows],
            "total": total, "page": page, "pages": pages, "per_page": per_page
        }

    def toggle_favorite(self, rel_path):
        """Toggle favorite status. Returns new state."""
        conn = self._get_conn()
        with self.lock:
            row = conn.execute("SELECT favorite FROM files WHERE path=?", (rel_path,)).fetchone()
            if not row: return None
            new_val = 0 if row[0] else 1
            conn.execute("UPDATE files SET favorite=? WHERE path=?", (new_val, rel_path))
            conn.commit()
        return {"path": rel_path, "favorite": bool(new_val)}

    def get_file_metadata(self, rel_path):
        conn = self._get_conn()
        with self.lock:
            row = conn.execute("""
                SELECT metadata_json, width, height, size, name, ext,
                       processing_state, processing_error, folder
                FROM files WHERE path=?
            """, (rel_path,)).fetchone()
        if not row: return None
        meta_raw = json.loads(row[0]) if row[0] else {}
        absolute_path = self.resolve_path(rel_path) or ""
        info = {
            "width": row[1], "height": row[2], "size": row[3],
            "name": row[4], "ext": row[5], "gallery_path": rel_path,
            "folder": row[8] or "", "folder_path": os.path.dirname(absolute_path) if absolute_path else "",
            "absolute_path": absolute_path,
        }
        parsed = {}
        if "parameters" in meta_raw and "prompt" in meta_raw:
            current = parse_sd_parameters(meta_raw.get("parameters") or "")
            if not _metadata_mod._looks_like_prompt_text(current.get("prompt", "")):
                refreshed = _metadata_mod.extract_comfyui_prompt(dict(meta_raw))
                if refreshed.get("parameters"):
                    meta_raw = refreshed
        if "parameters" in meta_raw:
            parsed = parse_sd_parameters(meta_raw["parameters"])
        elif "prompt" in meta_raw:
            prompt = meta_raw.get("prompt", "")
            if isinstance(prompt, str) and prompt.lstrip().startswith(("{", "[")):
                parsed["workflow"] = prompt[:2000] + "..." if len(prompt) > 2000 else prompt
            else:
                parsed["prompt"] = prompt
            if "Negative prompt" in meta_raw: parsed["negative_prompt"] = meta_raw["Negative prompt"]
            if "workflow" in meta_raw:
                wf = meta_raw["workflow"]
                parsed["workflow"] = wf[:2000] + "..." if len(wf) > 2000 else wf
        # civitai enrichment is performed by the module handler (uses hub.civitai)
        return {
            "info": info,
            "raw_meta": meta_raw,
            "parsed": parsed,
            "processing": row[6] == 0,
            "processing_error": row[7] if row[6] == 2 else None,
        }

    def get_stats(self):
        conn = self._get_conn()
        with self.lock:
            files = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
            with_metadata = conn.execute(
                "SELECT COUNT(*) FROM files WHERE has_metadata=1"
            ).fetchone()[0]
            favorites = conn.execute(
                "SELECT COUNT(*) FROM files WHERE favorite=1"
            ).fetchone()[0]
            pending = conn.execute(
                "SELECT COUNT(*) FROM files WHERE processing_state=0"
            ).fetchone()[0]
            failed = conn.execute(
                "SELECT COUNT(*) FROM files WHERE processing_state=2"
            ).fetchone()[0]
            model_pending = conn.execute(
                "SELECT COUNT(*) FROM files WHERE model_scanned=0 AND processing_state!=0"
            ).fetchone()[0]
            folders = conn.execute("SELECT COUNT(*) FROM folders").fetchone()[0]
        with self._processing_lock:
            active = self._processing_active
            paused = self._processing_paused.is_set()
        return {
            "files": int(files or 0),
            "folders": int(folders or 0),
            "with_metadata": int(with_metadata or 0),
            "favorites": int(favorites or 0),
            "processing_pending": int(pending or 0),
            "processing_failed": int(failed or 0),
            "model_pending": int(model_pending or 0),
            "processing_active": active,
            "processing_paused": paused,
            "scan": self.scan_status(),
        }

    # Search result cache: query_key -> list of file paths
    _search_cache = {}
    _search_cache_key = None

    def search(self, query, page=1, per_page=DEFAULT_PER_PAGE, sort="date", order="desc", include_model_info=False, metadata_filter="all"):
        """Global search using the full-text index, with legacy LIKE fallback."""
        conn = self._get_conn()
        sort_col = {"name": "name", "date": "mtime", "size": "size"}.get(sort, "mtime")
        order_dir = "DESC" if order == "desc" else "ASC"
        fts_query = self._fts_query(query)
        if not self.has_fts or not self.search_index_ready or not fts_query:
            return self._search_like(query, page, per_page, sort, order, include_model_info, metadata_filter)
        metadata_col = ", f.model_name, f.model_family" if include_model_info else ""
        metadata_where = self._metadata_filter_sql(metadata_filter, "f")
        metadata_clause = f" AND {metadata_where}" if metadata_where else ""

        with self.lock:
            fts_error = None
            try:
                total = conn.execute(
                    f"""SELECT COUNT(*)
                        FROM gallery_search s
                        JOIN files f ON f.path = s.path
                        WHERE gallery_search MATCH ?{metadata_clause}""",
                    (fts_query,)
                ).fetchone()[0]
                offset = (page - 1) * per_page
                rows = conn.execute(f"""
                    SELECT f.path, f.name, f.folder, f.ext, f.size, f.mtime,
                           f.width, f.height, f.has_metadata, f.favorite, f.processing_state{metadata_col}
                    FROM gallery_search s
                    JOIN files f ON f.path = s.path
                    WHERE gallery_search MATCH ?{metadata_clause}
                    ORDER BY f.{sort_col} {order_dir}
                    LIMIT ? OFFSET ?
                """, (fts_query, per_page, offset)).fetchall()
                folder_rows = conn.execute(f"""
                    SELECT f.folder, COUNT(*) as cnt
                    FROM gallery_search s
                    JOIN files f ON f.path = s.path
                    WHERE gallery_search MATCH ?{metadata_clause}
                    GROUP BY f.folder
                    ORDER BY cnt DESC
                """, (fts_query,)).fetchall()
                folder_info = {}
                if folder_rows:
                    fps = [r[0] for r in folder_rows]
                    ph = ",".join(["?"] * len(fps))
                    name_rows = conn.execute(f"SELECT path, name FROM folders WHERE path IN ({ph})", fps).fetchall()
                    folder_info = {r[0]: r[1] for r in name_rows}
            except sqlite3.OperationalError as e:
                fts_error = e

        if fts_error:
            print(f"[SEARCH] FTS query failed; using legacy search ({fts_error})")
            return self._search_like(query, page, per_page, sort, order, include_model_info, metadata_filter)

        pages = max(1, (total + per_page - 1) // per_page)
        files = [self._file_dict(r, include_model_info) for r in rows]
        folders = [{"path": r[0], "name": folder_info.get(r[0], r[0] or "Root"), "count": r[1]} for r in folder_rows]
        return {"files": files, "total": total, "page": page, "pages": pages,
                "per_page": per_page, "folders": folders, "query": query}

    def _search_like(self, query, page=1, per_page=DEFAULT_PER_PAGE, sort="date", order="desc", include_model_info=False, metadata_filter="all"):
        """Legacy global search: tags + filename first, metadata_json LIKE only if needed."""
        conn = self._get_conn()
        sort_col = {"name": "name", "date": "mtime", "size": "size"}.get(sort, "mtime")
        order_dir = "DESC" if order == "desc" else "ASC"
        like = f"%{query.lower()}%"
        cache_key = f"legacy|{query}|{sort}|{order}|{metadata_filter}"
        metadata_col = ", model_name, model_family" if include_model_info else ""
        metadata_where = self._metadata_filter_sql(metadata_filter)
        metadata_clause = f" AND {metadata_where}" if metadata_where else ""

        with self.lock:
            # Check if we have cached results for this exact query+sort
            if self._search_cache_key == cache_key and self._search_cache.get("paths"):
                cached = self._search_cache
            else:
                # Tier 1: tags + filename (fast, indexed)
                fast_where = """(files.path IN (
                    SELECT ft.file_path FROM file_tags ft
                    JOIN tags t ON ft.tag_id = t.id
                    WHERE t.name LIKE ?
                ) OR files.name LIKE ?)""" + metadata_clause
                fast_params = (like, like)
                fast_count = conn.execute(f"SELECT COUNT(*) FROM files WHERE {fast_where}", fast_params).fetchone()[0]

                # Tier 2: only if tier 1 found nothing, fall back to metadata_json LIKE
                if fast_count == 0:
                    full_where = """(files.path IN (
                        SELECT ft.file_path FROM file_tags ft
                        JOIN tags t ON ft.tag_id = t.id
                        WHERE t.name LIKE ?
                    ) OR files.name LIKE ? OR files.metadata_json LIKE ?)""" + metadata_clause
                    full_params = (like, like, f"%{query}%")
                    use_where = full_where
                    use_params = full_params
                else:
                    use_where = fast_where
                    use_params = fast_params

                # Cache all matching paths for fast pagination
                all_paths = conn.execute(
                    f"SELECT path FROM files WHERE {use_where} ORDER BY {sort_col} {order_dir}",
                    use_params).fetchall()
                cached = {"paths": [r[0] for r in all_paths], "where": use_where, "params": use_params}
                self._search_cache = cached
                self._search_cache_key = cache_key

            # Paginate from cached paths
            total = len(cached["paths"])
            offset = (page - 1) * per_page
            page_paths = cached["paths"][offset:offset + per_page]

            if page_paths:
                ph = ",".join(["?"] * len(page_paths))
                rows = conn.execute(
                    f"SELECT path, name, folder, ext, size, mtime, width, height, has_metadata, favorite, processing_state{metadata_col} FROM files WHERE path IN ({ph}) ORDER BY {sort_col} {order_dir}",
                    page_paths).fetchall()
            else:
                rows = []

            # Folder breakdown (from cached paths, not re-queried)
            folder_rows = conn.execute(
                f"SELECT folder, COUNT(*) as cnt FROM files WHERE {cached['where']} GROUP BY folder ORDER BY cnt DESC",
                cached["params"]).fetchall()
            folder_info = {}
            if folder_rows:
                fps = [r[0] for r in folder_rows]
                ph = ",".join(["?"] * len(fps))
                name_rows = conn.execute(f"SELECT path, name FROM folders WHERE path IN ({ph})", fps).fetchall()
                folder_info = {r[0]: r[1] for r in name_rows}

        pages = max(1, (total + per_page - 1) // per_page)
        files = [self._file_dict(r, include_model_info) for r in rows]
        folders = [{"path": r[0], "name": folder_info.get(r[0], r[0] or "Root"), "count": r[1]} for r in folder_rows]
        return {"files": files, "total": total, "page": page, "pages": pages,
                "per_page": per_page, "folders": folders, "query": query}

    def search_in_folder(self, query, folder, page=1, per_page=DEFAULT_PER_PAGE, sort="date", order="desc", include_model_info=False, metadata_filter="all"):
        """Search within a folder and its subfolders using FTS when available."""
        conn = self._get_conn()
        fts_query = self._fts_query(query)
        if not self.has_fts or not self.search_index_ready or not fts_query:
            return self._search_in_folder_like(query, folder, page, per_page, sort, order, include_model_info, metadata_filter)
        folder_like = folder + "/%" if folder else "%"
        sort_col = {"name": "name", "date": "mtime", "size": "size"}.get(sort, "mtime")
        order_dir = "DESC" if order == "desc" else "ASC"
        metadata_col = ", f.model_name, f.model_family" if include_model_info else ""
        metadata_where = self._metadata_filter_sql(metadata_filter, "f")
        metadata_clause = f" AND {metadata_where}" if metadata_where else ""

        with self.lock:
            fts_error = None
            try:
                where_folder = "(s.folder=? OR s.folder LIKE ?)"
                total = conn.execute(f"""
                    SELECT COUNT(*)
                    FROM gallery_search s
                    JOIN files f ON f.path = s.path
                    WHERE gallery_search MATCH ? AND {where_folder}{metadata_clause}
                """, (fts_query, folder, folder_like)).fetchone()[0]
                offset = (page - 1) * per_page
                rows = conn.execute(f"""
                    SELECT f.path, f.name, f.folder, f.ext, f.size, f.mtime,
                           f.width, f.height, f.has_metadata, f.favorite, f.processing_state{metadata_col}
                    FROM gallery_search s
                    JOIN files f ON f.path = s.path
                    WHERE gallery_search MATCH ? AND {where_folder}{metadata_clause}
                    ORDER BY f.{sort_col} {order_dir}
                    LIMIT ? OFFSET ?
                """, (fts_query, folder, folder_like, per_page, offset)).fetchall()
            except sqlite3.OperationalError as e:
                fts_error = e

        if fts_error:
            print(f"[SEARCH] Folder FTS query failed; using legacy search ({fts_error})")
            return self._search_in_folder_like(query, folder, page, per_page, sort, order, include_model_info, metadata_filter)

        pages = max(1, (total + per_page - 1) // per_page)
        files = [self._file_dict(r, include_model_info) for r in rows]
        return {"files": files, "total": total, "page": page, "pages": pages, "per_page": per_page}

    def _search_in_folder_like(self, query, folder, page=1, per_page=DEFAULT_PER_PAGE, sort="date", order="desc", include_model_info=False, metadata_filter="all"):
        """Legacy folder search. Tags first, LIKE fallback."""
        conn = self._get_conn()
        like = f"%{query.lower()}%"
        folder_like = folder + "/%" if folder else "%"
        sort_col = {"name": "name", "date": "mtime", "size": "size"}.get(sort, "mtime")
        order_dir = "DESC" if order == "desc" else "ASC"
        metadata_col = ", model_name, model_family" if include_model_info else ""
        metadata_where = self._metadata_filter_sql(metadata_filter)
        metadata_clause = f" AND {metadata_where}" if metadata_where else ""

        with self.lock:
            # Tier 1: tags + filename
            fast_where = """(folder=? OR folder LIKE ?) AND (
                files.path IN (
                    SELECT ft.file_path FROM file_tags ft
                    JOIN tags t ON ft.tag_id = t.id
                    WHERE t.name LIKE ?
                ) OR files.name LIKE ?)""" + metadata_clause
            fast_params = (folder, folder_like, like, like)
            fast_count = conn.execute(f"SELECT COUNT(*) FROM files WHERE {fast_where}", fast_params).fetchone()[0]

            if fast_count == 0:
                full_where = """(folder=? OR folder LIKE ?) AND (
                    files.path IN (
                        SELECT ft.file_path FROM file_tags ft
                        JOIN tags t ON ft.tag_id = t.id
                        WHERE t.name LIKE ?
                    ) OR files.name LIKE ? OR files.metadata_json LIKE ?)""" + metadata_clause
                use_where = full_where
                use_params = (folder, folder_like, like, like, f"%{query}%")
            else:
                use_where = fast_where
                use_params = fast_params

            total = conn.execute(f"SELECT COUNT(*) FROM files WHERE {use_where}", use_params).fetchone()[0]
            offset = (page - 1) * per_page
            rows = conn.execute(
                f"SELECT path, name, folder, ext, size, mtime, width, height, has_metadata, favorite, processing_state{metadata_col} FROM files WHERE {use_where} ORDER BY {sort_col} {order_dir} LIMIT ? OFFSET ?",
                use_params + (per_page, offset)).fetchall()

        pages = max(1, (total + per_page - 1) // per_page)
        files = [self._file_dict(r, include_model_info) for r in rows]
        return {"files": files, "total": total, "page": page, "pages": pages, "per_page": per_page}

    def get_tags(self, prefix="", limit=50):
        """Autocomplete: get tags matching prefix, sorted by frequency."""
        conn = self._get_conn()
        with self.lock:
            if prefix:
                rows = conn.execute("SELECT name, count FROM tags WHERE name LIKE ? ORDER BY count DESC LIMIT ?",
                                    (f"%{prefix.lower()}%", limit)).fetchall()
            else:
                rows = conn.execute("SELECT name, count FROM tags ORDER BY count DESC LIMIT ?", (limit,)).fetchall()
        return [{"name": r[0], "count": r[1]} for r in rows]

    def get_without_metadata_paths(self):
        """Return completed images that contain no generation metadata."""
        conn = self._get_conn()
        with self.lock:
            rows = conn.execute(
                """SELECT path FROM files
                   WHERE processing_state=1 AND has_metadata=0
                   ORDER BY path"""
            ).fetchall()
        return [row[0] for row in rows]

    def count_without_metadata(self):
        conn = self._get_conn()
        with self.lock:
            return conn.execute(
                "SELECT COUNT(*) FROM files WHERE processing_state=1 AND has_metadata=0"
            ).fetchone()[0]

    def delete_files(self, rel_paths, progress=None):
        """Move files to trash and remove their Gallery records in one batch."""
        conn = self._get_conn()
        results = []
        affected_folders = set()
        successful_paths = []
        paths = list(dict.fromkeys(p for p in rel_paths if isinstance(p, str) and p))
        total = len(paths)
        ok_count = 0
        failed_count = 0
        for index, rel_path in enumerate(paths, 1):
            full_path = self.resolve_path(rel_path)
            if not full_path or not os.path.isfile(full_path):
                results.append({"path": rel_path, "ok": False, "error": "File not found"})
                failed_count += 1
                if progress:
                    progress(index, total, ok_count, failed_count, "trash", rel_path)
                continue
            try:
                if HAS_TRASH:
                    send2trash(full_path)
                else:
                    os.remove(full_path)
                successful_paths.append(rel_path)
                affected_folders.add(os.path.dirname(rel_path))
                results.append({"path": rel_path, "ok": True})
                ok_count += 1
                log_debug(f"Deleted: {rel_path}")
            except Exception as e:
                results.append({"path": rel_path, "ok": False, "error": str(e)})
                failed_count += 1
                log_debug(f"Delete failed for {rel_path}: {e}")
            if progress:
                progress(index, total, ok_count, failed_count, "trash", rel_path)

        if successful_paths:
            if progress:
                progress(total, total, ok_count, failed_count, "database", "")
            with self.lock:
                self._prune_file_records(conn, successful_paths)
                for folder in affected_folders:
                    conn.execute(
                        "UPDATE folders SET file_count = (SELECT COUNT(*) FROM files WHERE folder=?) WHERE path=?",
                        (folder, folder),
                    )
                pruned_folders = self._prune_empty_folder_records(conn, affected_folders)
                if pruned_folders:
                    print(f"[DELETE] Pruned {pruned_folders} empty folder records")
                self._refresh_folder_branches(conn, affected_folders)
                conn.execute("UPDATE tags SET count = (SELECT COUNT(*) FROM file_tags WHERE file_tags.tag_id = tags.id)")
                conn.execute("DELETE FROM tags WHERE count = 0")
                conn.commit()
        self._search_cache = {}
        self._search_cache_key = None
        return results

    # ─── Collections ──────────────────────────────────────────────────────────

    def create_collection(self, name, color="#4a9eff"):
        conn = self._get_conn()
        with self.lock:
            try:
                conn.execute("INSERT INTO collections (name, color, created) VALUES (?,?,?)",
                             (name.strip(), color, time.time()))
                conn.commit()
                row = conn.execute("SELECT id, name, color, created FROM collections WHERE name=?", (name.strip(),)).fetchone()
                return {"id": row[0], "name": row[1], "color": row[2], "count": 0}
            except sqlite3.IntegrityError:
                return {"error": "Collection already exists"}

    def rename_collection(self, collection_id, new_name):
        conn = self._get_conn()
        with self.lock:
            try:
                conn.execute("UPDATE collections SET name=? WHERE id=?", (new_name.strip(), collection_id))
                conn.commit()
                return {"ok": True}
            except sqlite3.IntegrityError:
                return {"error": "Name already taken"}

    def delete_collection(self, collection_id):
        conn = self._get_conn()
        with self.lock:
            conn.execute("DELETE FROM file_collections WHERE collection_id=?", (collection_id,))
            conn.execute("DELETE FROM collections WHERE id=?", (collection_id,))
            conn.commit()
        return {"ok": True}

    def get_collections(self):
        conn = self._get_conn()
        with self.lock:
            rows = conn.execute("""
                SELECT c.id, c.name, c.color, c.created, COUNT(fc.file_path) as cnt
                FROM collections c
                LEFT JOIN file_collections fc ON c.id = fc.collection_id
                GROUP BY c.id ORDER BY c.name COLLATE NOCASE
            """).fetchall()
        return [{"id": r[0], "name": r[1], "color": r[2], "created": r[3], "count": r[4]} for r in rows]

    def add_to_collection(self, collection_id, paths):
        conn = self._get_conn()
        now = time.time()
        added = 0
        with self.lock:
            for p in paths:
                try:
                    conn.execute("INSERT OR IGNORE INTO file_collections (file_path, collection_id, added) VALUES (?,?,?)",
                                 (p, collection_id, now))
                    added += 1
                except Exception: pass
            conn.commit()
        return {"added": added}

    def remove_from_collection(self, collection_id, paths):
        conn = self._get_conn()
        with self.lock:
            for p in paths:
                conn.execute("DELETE FROM file_collections WHERE file_path=? AND collection_id=?", (p, collection_id))
            conn.commit()
        return {"removed": len(paths)}

    def get_collection_files(self, collection_id, sort="date", order="desc", page=1, per_page=DEFAULT_PER_PAGE, include_model_info=False, metadata_filter="all"):
        conn = self._get_conn()
        sort_col = {"name": "f.name", "date": "fc.added", "size": "f.size"}.get(sort, "fc.added")
        order_dir = "DESC" if order == "desc" else "ASC"
        offset = (page - 1) * per_page
        metadata_col = ", f.model_name, f.model_family" if include_model_info else ""
        metadata_where = self._metadata_filter_sql(metadata_filter, "f")
        metadata_clause = f" AND {metadata_where}" if metadata_where else ""
        with self.lock:
            total = conn.execute(
                f"SELECT COUNT(*) FROM file_collections fc JOIN files f ON fc.file_path = f.path WHERE fc.collection_id=?{metadata_clause}",
                (collection_id,)).fetchone()[0]
            rows = conn.execute(
                f"""SELECT f.path, f.name, f.folder, f.ext, f.size, f.mtime, f.width, f.height, f.has_metadata, f.favorite, f.processing_state{metadata_col}
                    FROM file_collections fc JOIN files f ON fc.file_path = f.path
                    WHERE fc.collection_id=?{metadata_clause} ORDER BY {sort_col} {order_dir} LIMIT ? OFFSET ?""",
                (collection_id, per_page, offset)).fetchall()
        pages = max(1, (total + per_page - 1) // per_page)
        return {
            "files": [self._file_dict(r, include_model_info) for r in rows],
            "total": total, "page": page, "pages": pages, "per_page": per_page
        }

    def get_file_collections(self, file_path):
        """Get which collections a file belongs to."""
        conn = self._get_conn()
        with self.lock:
            rows = conn.execute("""
                SELECT c.id, c.name, c.color FROM collections c
                JOIN file_collections fc ON c.id = fc.collection_id
                WHERE fc.file_path=?
            """, (file_path,)).fetchall()
        return [{"id": r[0], "name": r[1], "color": r[2]} for r in rows]

# ─── Thumbnail Generator ─────────────────────────────────────────────────────

def get_thumb_path(thumb_dir, rel_path):
    h = hashlib.md5(rel_path.encode()).hexdigest()
    return os.path.join(thumb_dir, h[:2], h + ".webp")

def migrate_thumb_layout(thumb_dir):
    """One-time migration: move flat thumbs into hash-prefix subfolders."""
    if not os.path.isdir(thumb_dir):
        return
    flat_files = [f for f in os.listdir(thumb_dir) if f.endswith(".webp") and os.path.isfile(os.path.join(thumb_dir, f))]
    if not flat_files:
        return
    print(f"[MIGRATE] Moving {len(flat_files)} thumbnails to subfolder layout...")
    moved = 0
    for fname in flat_files:
        prefix = fname[:2]
        dest_dir = os.path.join(thumb_dir, prefix)
        os.makedirs(dest_dir, exist_ok=True)
        src = os.path.join(thumb_dir, fname)
        dst = os.path.join(dest_dir, fname)
        try:
            os.rename(src, dst)
            moved += 1
        except OSError as e:
            log_debug(f"Thumb migration failed for {fname}: {e}")
    old_jpgs = [f for f in os.listdir(thumb_dir) if f.endswith((".jpg", ".jpg.old")) and os.path.isfile(os.path.join(thumb_dir, f))]
    for fname in old_jpgs:
        try:
            os.remove(os.path.join(thumb_dir, fname))
        except OSError:
            pass
    print(f"[MIGRATE] Done. {moved} thumbnails moved to {len(set(f[:2] for f in flat_files))} subfolders.")

def ensure_thumbnail(thumb_dir, db_path, abs_path):
    """Generate thumbnail if needed. Returns thumb path or fallback to original."""
    if not abs_path or not os.path.isfile(abs_path):
        return None
    thumb_path = get_thumb_path(thumb_dir, db_path)
    if os.path.exists(thumb_path):
        src_mtime = os.path.getmtime(abs_path)
        th_mtime = os.path.getmtime(thumb_path)
        if th_mtime >= src_mtime:
            return thumb_path
    if not HAS_PIL:
        return abs_path
    try:
        os.makedirs(os.path.dirname(thumb_path), exist_ok=True)
        with Image.open(abs_path) as img:
            img.thumbnail(THUMB_SIZE, LANCZOS)
            img.save(thumb_path, "WEBP", quality=80)
        return thumb_path
    except Exception as e:
        log_debug(f"Thumbnail generation failed: {e}")
        return abs_path

# (old ensure_thumbnail removed — using new version above)


def generate_all_thumbs(thumb_dir, roots, workers=None):
    """Pre-generate all thumbnails using a thread pool."""
    import concurrent.futures
    if not HAS_PIL:
        print("[ERROR] Pillow is required for thumbnail generation"); return

    migrate_thumb_layout(thumb_dir)

    print(f"[THUMBS] Scanning {len(roots)} folder(s)...")
    all_images = []  # list of (db_path, abs_path)
    for root_name, root_abs in roots.items():
        if not os.path.isdir(root_abs): continue
        for dirpath, dirnames, filenames in os.walk(root_abs):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for fname in filenames:
                if Path(fname).suffix.lower() in IMAGE_EXTENSIONS:
                    fpath = os.path.join(dirpath, fname)
                    db_path = root_name + "/" + os.path.relpath(fpath, root_abs).replace(os.sep, "/")
                    all_images.append((db_path, fpath))

    total = len(all_images)
    if total == 0:
        print("[THUMBS] No images found."); return

    existing = sum(1 for db_path, _ in all_images if os.path.exists(get_thumb_path(thumb_dir, db_path)))
    to_generate = total - existing
    print(f"[THUMBS] {total} images, {existing} thumbnails exist, {to_generate} to generate")
    if to_generate == 0:
        print("[THUMBS] All thumbnails up to date."); return

    if workers is None:
        workers = min(8, (os.cpu_count() or 4))
    print(f"[THUMBS] Generating with {workers} workers...")
    t0 = time.time()
    done = 0; skipped = 0; errors = 0
    lock = threading.Lock()

    def process(item):
        nonlocal done, skipped, errors
        db_path, full_path = item
        thumb_path = get_thumb_path(thumb_dir, db_path)
        if os.path.exists(thumb_path):
            try:
                if os.path.getmtime(full_path) <= os.path.getmtime(thumb_path):
                    with lock: skipped += 1; done += 1
                    return
            except OSError: pass
        os.makedirs(os.path.dirname(thumb_path), exist_ok=True)
        try:
            with Image.open(full_path) as img:
                img.thumbnail(THUMB_SIZE, LANCZOS)
                if img.mode in ("RGBA", "P"): img = img.convert("RGB")
                img.save(thumb_path, "WEBP", quality=80, method=4)
        except Exception:
            with lock: errors += 1; done += 1
            return
        with lock:
            done += 1
            if done % 500 == 0 or done == total:
                elapsed = time.time() - t0
                rate = done / elapsed if elapsed > 0 else 0
                pct = done * 100 / total
                eta = (total - done) / rate if rate > 0 else 0
                print(f"[THUMBS] {done}/{total} ({pct:.1f}%) — {rate:.0f}/s — ETA {eta:.0f}s")

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(process, item) for item in all_images]
        try:
            concurrent.futures.wait(futures)
        except KeyboardInterrupt:
            print(f"\n[THUMBS] Interrupted at {done}/{total}")
            executor.shutdown(wait=False, cancel_futures=True)
            return

    elapsed = time.time() - t0
    generated = done - skipped - errors
    print(f"[THUMBS] Done in {elapsed:.1f}s. {generated} generated, {skipped} skipped, {errors} errors.")


GALLERY_BODY = r'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Gallery — {HUB_TITLE}</title>
{FONT_LINKS}
<style>
:root {
    --bg-darkest: #0a0c10; --bg-dark: #0f1218; --bg-panel: #141820;
    --bg-card: #1a1f2a; --bg-hover: #222838; --bg-active: #1c2436;
    --bg-input: #1a1f2a; --bg-main: #0a0c10; --bg: #0a0c10;
    --border: #1e2433; --border-light: #2a3040;
    --text: #c8cdd8; --text-dim: #6b7280; --text-bright: #e8ecf4;
    --accent: #4a9eff; --accent-dim: #2d6ab3; --accent-glow: rgba(74,158,255,0.08);
    --green: #3ddc84; --orange: #f59e0b; --red:#ef4444;
    --prompt-text: #a9b1d6; --neg-prompt: #f7768e;
    --setting-key: #9ece6a; --setting-val: #c0caf5;
    --radius: 6px;
    --font: 'IBM Plex Sans', -apple-system, sans-serif;
    --mono: 'JetBrains Mono', 'Consolas', monospace;
    --thumb-size: 180px;
}
body.theme-light {
    --bg-darkest:#f4f6fb; --bg-dark:#eef2f8; --bg-panel:#ffffff;
    --bg-card:#f7f9fd; --bg-hover:#edf3fb; --bg-active:#e5f0ff;
    --bg-input:#ffffff; --bg-main:#f4f6fb; --bg:#f4f6fb;
    --border:#d9e1ee; --border-light:#c7d3e4;
    --text:#334155; --text-dim:#718096; --text-bright:#111827;
    --accent:#2563eb; --accent-dim:#1d4ed8; --accent-glow:rgba(37,99,235,0.10);
    --green:#16a34a; --orange:#d97706; --red:#dc2626; --purple:#7c3aed;
    --prompt-text:#334155; --neg-prompt:#be123c;
    --setting-key:#3f7d20; --setting-val:#243b65;
}
@media (prefers-color-scheme: light) {
    body.theme-system {
        --bg-darkest:#f4f6fb; --bg-dark:#eef2f8; --bg-panel:#ffffff;
        --bg-card:#f7f9fd; --bg-hover:#edf3fb; --bg-active:#e5f0ff;
        --bg-input:#ffffff; --bg-main:#f4f6fb; --bg:#f4f6fb;
        --border:#d9e1ee; --border-light:#c7d3e4;
        --text:#334155; --text-dim:#718096; --text-bright:#111827;
        --accent:#2563eb; --accent-dim:#1d4ed8; --accent-glow:rgba(37,99,235,0.10);
        --green:#16a34a; --orange:#d97706; --red:#dc2626; --purple:#7c3aed;
        --prompt-text:#334155; --neg-prompt:#be123c;
        --setting-key:#3f7d20; --setting-val:#243b65;
    }
}
* { margin:0; padding:0; box-sizing:border-box; }
body { background:var(--bg-darkest); color:var(--text); font-family:var(--font); font-size:13px; line-height:1.5; overflow:hidden; height:100vh; }

.app { display:grid; grid-template-columns:260px 1fr 370px; grid-template-rows:48px 1fr 28px; height:100vh; transition:grid-template-columns .18s ease; }
.app.meta-collapsed { grid-template-columns:260px 1fr 0; }
.topbar { grid-column:1/-1; background:var(--bg-dark); border-bottom:1px solid var(--border); display:flex; align-items:center; padding:0 16px; gap:16px; z-index:10; }
.sidebar { grid-row:2; background:var(--bg-panel); border-right:1px solid var(--border); overflow-y:auto; overflow-x:hidden; }
.gallery-area { grid-row:2; overflow-y:auto; background:var(--bg-dark); padding:0px 12px 12px 12px; position:relative; display:flex; flex-direction:column; }
.meta-panel { grid-row:2; background:var(--bg-panel); border-left:1px solid var(--border); overflow-y:auto; display:flex; flex-direction:column; min-width:0; transition:opacity .15s ease; }
.app.meta-collapsed .meta-panel { opacity:0; pointer-events:none; border-left:0; overflow:hidden; }
.statusbar { grid-column:1/-1; background:var(--bg-dark); border-top:1px solid var(--border); display:flex; align-items:center; padding:0 12px; font-size:11px; color:var(--text-dim); gap:16px; }
.processing-status { margin-left:auto; display:none; align-items:center; gap:8px; min-width:300px; justify-content:flex-end; }
.processing-status.visible { display:flex; }
.processing-track { width:120px; height:4px; overflow:hidden; border-radius:2px; background:var(--border); }
.processing-fill { width:0; height:100%; background:var(--accent); transition:width .25s ease; }
.processing-fill.indeterminate { width:32% !important; animation:processing-slide 1.1s ease-in-out infinite; }
@keyframes processing-slide { from { transform:translateX(-110%); } to { transform:translateX(340%); } }
.processing-toggle { border:0; background:none; color:var(--text-dim); font:inherit; cursor:pointer; padding:2px 4px; }
.processing-toggle:hover { color:var(--text-bright); }

.topbar-title { font-family:var(--mono); font-weight:600; font-size:14px; color:var(--accent); letter-spacing:-0.3px; white-space:nowrap; text-decoration:none; cursor:pointer; }
.topbar-title:hover { color:var(--text-bright); }

/* Hub menu (hamburger + dropdown panel) */
.hub-menu { position:relative; }
.hub-menu-btn { background:none; border:1px solid transparent; color:var(--text-dim); width:32px; height:32px; display:flex; align-items:center; justify-content:center; border-radius:var(--radius); cursor:pointer; transition:all .15s; padding:0; }
.hub-menu-btn:hover { background:var(--bg-hover); color:var(--text); }
.hub-menu.open .hub-menu-btn { background:var(--bg-active); color:var(--accent); border-color:var(--accent-dim); }
.hub-menu-active { font-size:12px; color:var(--text); margin-left:2px; white-space:nowrap; display:flex; align-items:center; gap:5px; pointer-events:none; }
.hub-menu-active .icon { width:14px; height:14px; display:inline-flex; align-items:center; justify-content:center; color:currentColor; }
.hub-menu-panel { position:absolute; top:calc(100% + 4px); left:0; background:var(--bg-panel); border:1px solid var(--border-light); border-radius:var(--radius); min-width:520px; padding:4px; box-shadow:0 8px 24px rgba(0,0,0,.5); z-index:1000; opacity:0; visibility:hidden; transform:translateY(-4px); transition:opacity .12s, transform .12s, visibility .12s; display:grid; grid-template-columns:repeat(2, minmax(240px, 1fr)); gap:1px 4px; }
.hub-menu.open .hub-menu-panel { opacity:1; visibility:visible; transform:translateY(0); }
.hub-menu-item { display:flex; align-items:center; gap:10px; padding:8px 10px; border-radius:4px; text-decoration:none; color:var(--text); font-size:12px; transition:background .1s; cursor:pointer; min-width:0; }
.hub-menu-item:hover { background:var(--bg-hover); }
.hub-menu-item.active { background:var(--bg-active); color:var(--accent); }
.hub-menu-item .icon { width:20px; height:20px; display:inline-flex; align-items:center; justify-content:center; flex-shrink:0; color:currentColor; }
.hub-menu-item .icon svg, .hub-menu-active .icon svg { width:16px; height:16px; display:block; }
.hub-menu-item > span:not(.icon) { min-width:0; overflow:hidden; }
.hub-menu-item .label { display:block; font-weight:500; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.hub-menu-item .desc { display:block; font-size:10px; color:var(--text-dim); font-weight:400; margin-top:1px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.hub-menu-sep { grid-column:1 / -1; height:1px; background:var(--border); margin:4px 6px; }
.hub-menu-panel > a:last-child { grid-column:1 / -1; }
@media (max-width:560px) { .hub-menu-panel { grid-template-columns:1fr; min-width:240px; } .hub-menu-panel > a:last-child { grid-column:auto; } }
.breadcrumb { display:flex; align-items:center; gap:4px; font-size:12px; color:var(--text-dim); flex:1; min-width:0; overflow:hidden; }
.breadcrumb span { cursor:pointer; padding:2px 6px; border-radius:3px; white-space:nowrap; transition:background .15s; }
.breadcrumb span:hover { background:var(--bg-hover); color:var(--text-bright); }
.breadcrumb .sep { color:var(--border-light); cursor:default; padding:0 2px; }
.breadcrumb .sep:hover { background:none; color:var(--border-light); }

.search-wrap { display:flex; align-items:center; gap:4px; }
.search-box { display:flex; align-items:center; background:var(--bg-card); border:1px solid var(--border); border-radius:var(--radius); padding:0 10px; gap:6px; width:220px; transition:border-color .2s; }
.search-box:focus-within { border-color:var(--accent); }
.search-box svg { color:var(--text-dim); flex-shrink:0; }
.search-box input { background:none; border:none; color:var(--text); font:inherit; font-size:12px; outline:none; width:100%; padding:5px 0; }
.search-clear { background:none; border:1px solid var(--border); color:var(--text-dim); font-size:11px; padding:4px 8px; border-radius:var(--radius); cursor:pointer; display:none; transition:all .15s; white-space:nowrap; }
.search-clear:hover { background:var(--bg-hover); color:var(--text); }
.search-clear.visible { display:block; }

.topbar-controls { display:flex; gap:4px; align-items:center; }
.btn-icon { background:none; border:1px solid transparent; color:var(--text-dim); width:32px; height:32px; display:flex; align-items:center; justify-content:center; border-radius:var(--radius); cursor:pointer; transition:all .15s; }
.btn-icon:hover { background:var(--bg-hover); color:var(--text); }
.btn-icon.active { background:var(--bg-active); color:var(--accent); border-color:var(--accent-dim); }
.sort-select { background:var(--bg-card); border:1px solid var(--border); color:var(--text); font:inherit; font-size:11px; padding:4px 8px; border-radius:var(--radius); cursor:pointer; outline:none; }
.metadata-filter-select.active { border-color:var(--accent); color:var(--accent); background:var(--bg-active); }

/* Sidebar */
.sidebar-header { padding:12px 14px 8px; font-size:10px; font-weight:600; text-transform:uppercase; letter-spacing:.8px; color:var(--text-dim); display:flex; justify-content:space-between; align-items:center; }
.sidebar-header .search-badge { background:var(--accent); color:#fff; font-size:9px; padding:1px 6px; border-radius:8px; font-weight:500; }

.folder-item { display:flex; align-items:center; padding:5px 10px; cursor:pointer; border-left:3px solid transparent; transition:all .12s; gap:6px; font-size:12px; color:var(--text); user-select:none; }
.folder-item:hover { background:var(--bg-hover); }
.folder-item.active { background:var(--bg-active); border-left-color:var(--accent); color:var(--text-bright); }

.folder-toggle { width:16px; height:16px; display:flex; align-items:center; justify-content:center; font-size:10px; color:var(--text-dim); transition:transform .15s; flex-shrink:0; }
.folder-toggle.open { transform:rotate(90deg); }
.folder-toggle.empty { visibility:hidden; }

.folder-icon { width:28px; height:28px; border-radius:4px; overflow:hidden; flex-shrink:0; background:var(--bg-card); display:flex; align-items:center; justify-content:center; font-size:14px; color:var(--text-dim); }
.folder-icon svg { width:15px; height:15px; display:block; }
.folder-icon img { width:100%; height:100%; object-fit:cover; }
.folder-name { flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.folder-count { font-size:10px; color:var(--text-dim); background:var(--bg-card); padding:1px 6px; border-radius:10px; font-family:var(--mono); }
.folder-count.search-count { background:var(--accent-dim); color:#fff; }
.folder-children { display:none; }
.folder-children.open { display:block; }

/* Gallery Grid */
.gallery-grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(var(--thumb-size),1fr)); gap:8px; flex:1; align-content:start; }
.gallery-group-header { grid-column:1/-1; display:flex; align-items:center; gap:8px; min-height:28px; margin:8px 0 0; padding:4px 2px; color:var(--text-bright); font-size:11px; font-weight:700; letter-spacing:.04em; text-transform:uppercase; border-bottom:1px solid var(--border); }
.gallery-group-dot { width:9px; height:9px; border-radius:50%; background:var(--model-color,#64748b); box-shadow:0 0 12px var(--model-color,#64748b); flex-shrink:0; }
.gallery-group-count { color:var(--text-dim); font-family:var(--mono); font-size:10px; font-weight:500; margin-left:2px; }
.thumb-card { position:relative; border-radius:var(--radius); overflow:hidden; background:var(--bg-card); cursor:pointer; border:2px solid transparent; transition:all .15s; aspect-ratio:1; }
.thumb-card:hover { border-color:var(--border-light); transform:translateY(-1px); }
.thumb-card.selected { border-color:var(--accent); box-shadow:0 0 0 1px var(--accent-dim); }
.thumb-card img { width:100%; height:100%; object-fit:cover; display:block; background:var(--bg-darkest); }
.thumb-overlay { position:absolute; bottom:0; left:0; right:0; background:linear-gradient(transparent,rgba(0,0,0,.85)); padding:24px 8px 6px; pointer-events:none; }
.thumb-name { font-size:10px; color:#eee; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.thumb-meta-badge { position:absolute; top:6px; right:6px; width:8px; height:8px; border-radius:50%; background:var(--green); box-shadow:0 0 6px var(--green); }
.thumb-fav { position:absolute; top:5px; right:18px; font-size:14px; cursor:pointer; z-index:3; opacity:0; transition:opacity .15s; line-height:1; filter:drop-shadow(0 1px 2px rgba(0,0,0,.8)); }
.thumb-card:hover .thumb-fav { opacity:1; }
.thumb-fav.active { opacity:1; }
.thumb-dims { position:absolute; top:6px; left:6px; font-size:9px; font-family:var(--mono); color:rgba(255,255,255,.7); background:rgba(0,0,0,.5); padding:1px 4px; border-radius:3px; pointer-events:none; opacity:0; transition:opacity .15s; }
.thumb-card:hover .thumb-dims { opacity:1; }
.thumb-model-badge { position:absolute; left:6px; bottom:24px; max-width:calc(100% - 12px); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font-size:9px; font-weight:700; color:#fff; background:rgba(0,0,0,.58); border:1px solid var(--model-color,#64748b); border-left:3px solid var(--model-color,#64748b); border-radius:999px; padding:2px 6px; pointer-events:none; opacity:.9; text-shadow:0 1px 2px rgba(0,0,0,.8); }
.thumb-processing { position:absolute; top:6px; left:6px; padding:2px 5px; border-radius:3px; background:rgba(0,0,0,.72); color:#dbeafe; font-size:9px; font-family:var(--mono); pointer-events:none; }

/* Sidebar special items */
.sidebar-divider { height:1px; background:var(--border); margin:8px 14px; }
.sidebar-special { padding:5px 14px; cursor:pointer; display:flex; align-items:center; gap:8px; font-size:12px; color:var(--text); transition:all .12s; border-left:3px solid transparent; }
.sidebar-special:hover { background:var(--bg-hover); }
.sidebar-special.active { background:var(--bg-active); border-left-color:var(--accent); color:var(--text-bright); }
.sidebar-special .ss-icon { width:16px; height:16px; display:inline-flex; align-items:center; justify-content:center; color:currentColor; }
.sidebar-special .ss-icon svg { width:15px; height:15px; display:block; }
.sidebar-special .ss-count { font-size:10px; color:var(--text-dim); background:var(--bg-card); padding:1px 6px; border-radius:10px; font-family:var(--mono); margin-left:auto; }

/* Meta panel favorite */
.meta-fav-btn { cursor:pointer; font-size:16px; margin:0 10px 0 8px; width:28px; display:flex; align-items:center; justify-content:center; transition:transform .15s, color .15s; flex-shrink:0; }
.meta-fav-btn:hover { transform:scale(1.15); color:var(--orange); }
.meta-delete-btn { color:var(--text-dim); border-color:var(--border); }
.meta-delete-btn:hover { color:var(--red); border-color:var(--red); background:rgba(239,68,68,.10); }

/* Civitai model info */
.meta-civitai { background:var(--bg-card); border:1px solid var(--accent-dim); border-radius:var(--radius); padding:8px 10px; font-size:11px; }
.meta-civitai-name { font-weight:600; color:var(--accent); }
.meta-civitai-name a { color:var(--accent); text-decoration:none; }
.meta-civitai-name a:hover { text-decoration:underline; }
.meta-civitai-detail { color:var(--text-dim); font-family:var(--mono); font-size:10px; margin-top:2px; }
.meta-civitai-words { margin-top:4px; display:flex; flex-wrap:wrap; gap:3px; }
.meta-civitai-words span { background:var(--bg-hover); border:1px solid var(--border); padding:1px 6px; border-radius:8px; font-size:9px; color:var(--text); font-family:var(--mono); }

/* Collections */
.coll-list { padding:0 0 4px 0; }
.coll-item { display:flex; align-items:center; padding:4px 14px; cursor:pointer; gap:6px; font-size:12px; color:var(--text); transition:all .12s; border-left:3px solid transparent; }
.coll-item:hover { background:var(--bg-hover); }
.coll-item.active { background:var(--bg-active); border-left-color:var(--accent); color:var(--text-bright); }
.coll-dot { width:8px; height:8px; border-radius:50%; flex-shrink:0; }
.coll-name { flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.coll-cnt { font-size:10px; color:var(--text-dim); background:var(--bg-card); padding:1px 6px; border-radius:10px; font-family:var(--mono); }
.coll-del { font-size:11px; color:var(--text-dim); cursor:pointer; opacity:0; transition:all .15s; padding:0 2px; flex-shrink:0; }
.coll-item:hover .coll-del { opacity:1; }
.coll-del:hover { color:var(--red); }
.coll-add-btn { padding:4px 14px; cursor:pointer; font-size:11px; color:var(--accent); transition:all .12s; display:flex; align-items:center; gap:4px; }
.coll-add-btn:hover { background:var(--bg-hover); }

/* Collection picker dropdown in selection bar */
.coll-picker { position:relative; }
.coll-picker-menu { position:absolute; bottom:100%; left:0; background:var(--bg-panel); border:1px solid var(--border); border-radius:var(--radius); min-width:180px; max-height:240px; overflow-y:auto; box-shadow:0 8px 24px rgba(0,0,0,.5); display:none; margin-bottom:4px; z-index:910; }
.coll-picker-menu.open { display:block; }
.coll-picker-item { padding:6px 12px; cursor:pointer; font-size:12px; display:flex; align-items:center; gap:6px; transition:background .1s; }
.coll-picker-item:hover { background:var(--bg-hover); }
.coll-picker-input { width:100%; background:var(--bg-card); border:none; border-bottom:1px solid var(--border); color:var(--text); font:inherit; font-size:12px; padding:8px 12px; outline:none; }
.coll-picker-input::placeholder { color:var(--text-dim); }

/* Meta panel collection badges */
.meta-collections { display:flex; flex-wrap:wrap; gap:4px; margin-top:6px; }
.meta-coll-badge { display:flex; align-items:center; gap:4px; font-size:10px; padding:2px 8px; border-radius:10px; background:var(--bg-card); border:1px solid var(--border); color:var(--text); }
.meta-coll-badge .coll-dot { width:6px; height:6px; }
.meta-coll-badge .coll-remove { cursor:pointer; color:var(--text-dim); margin-left:2px; font-size:12px; line-height:1; transition:color .15s; }
.meta-coll-badge .coll-remove:hover { color:var(--red); }

.gallery-empty { display:flex; align-items:center; justify-content:center; flex:1; color:var(--text-dim); font-size:14px; flex-direction:column; gap:8px; text-align:center; min-height:320px; }
.gallery-empty strong { color:var(--text); font-size:15px; font-weight:600; }
.gallery-empty span { max-width:360px; }

/* Sticky gallery toolbar */
.gallery-toolbar {
    position:sticky; top:0; z-index:5;
    background:var(--bg-dark); border-bottom:1px solid var(--border);
    display:flex; align-items:center; justify-content:space-between;
    padding:15px 4px; margin:0 -12px 8px -12px;
    font-size:11px; font-family:var(--mono); color:var(--text-dim);
    min-height:32px;
}
.gallery-toolbar .gt-info { padding-left:8px; }
.gallery-toolbar .gt-nav { display:flex; align-items:center; gap:4px; padding-right:4px; }
.gallery-toolbar .gt-nav button {
    background:var(--bg-card); border:1px solid var(--border); color:var(--text-dim);
    font:inherit; font-size:12px; padding:3px 10px; border-radius:var(--radius);
    cursor:pointer; transition:all .15s; font-family:var(--mono);
}
.gallery-toolbar .gt-nav button:hover:not(:disabled) { background:var(--bg-hover); color:var(--text); border-color:var(--border-light); }
.gallery-toolbar .gt-nav button:disabled { opacity:.3; cursor:default; }
.gallery-toolbar .gt-nav .gt-page { color:var(--text); padding:0 6px; font-size:11px; cursor:pointer; border-radius:3px; transition:background .15s; }
.gallery-toolbar .gt-nav .gt-page:hover { background:var(--bg-hover); }
.gallery-toolbar .gt-nav .gt-page-input {
    width:48px; background:var(--bg-card); border:1px solid var(--accent); color:var(--text);
    font:inherit; font-size:11px; font-family:var(--mono); padding:2px 4px; border-radius:var(--radius);
    text-align:center; outline:none; -moz-appearance:textfield;
}
.gallery-toolbar .gt-nav .gt-page-input::-webkit-outer-spin-button,
.gallery-toolbar .gt-nav .gt-page-input::-webkit-inner-spin-button { -webkit-appearance:none; margin:0; }

/* Pagination */
.pagination { display:flex; align-items:center; justify-content:center; gap:4px; padding:10px 0 4px; flex-shrink:0; }
.pagination button {
    background:var(--bg-card); border:1px solid var(--border); color:var(--text-dim);
    font:inherit; font-size:11px; padding:4px 10px; border-radius:var(--radius);
    cursor:pointer; transition:all .15s; font-family:var(--mono);
}
.pagination button:hover:not(:disabled) { background:var(--bg-hover); color:var(--text); border-color:var(--border-light); }
.pagination button:disabled { opacity:.3; cursor:default; }
.pagination button.active { background:var(--accent-glow); color:var(--accent); border-color:var(--accent-dim); }
.pagination .page-info { font-size:11px; color:var(--text-dim); padding:0 8px; font-family:var(--mono); }

/* Meta Panel */
.meta-empty { display:flex; align-items:center; justify-content:center; height:100%; color:var(--text-dim); font-size:13px; padding:20px; text-align:center; }
.meta-panel-head { flex-shrink:0; display:flex; align-items:center; justify-content:space-between; gap:8px; padding:8px 10px; background:var(--bg-dark); border-bottom:1px solid var(--border); }
.meta-panel-title { font-size:10px; font-weight:600; text-transform:uppercase; letter-spacing:.7px; color:var(--text-dim); overflow:hidden; white-space:nowrap; text-overflow:ellipsis; }
.meta-panel-controls { display:flex; align-items:center; gap:4px; }
.meta-mini-btn { background:none; border:1px solid transparent; color:var(--text-dim); height:24px; min-width:24px; padding:0 7px; border-radius:var(--radius); font:11px var(--font); cursor:pointer; display:inline-flex; align-items:center; justify-content:center; gap:4px; transition:all .15s; }
.meta-mini-btn:hover { background:var(--bg-hover); color:var(--text); }
.meta-mini-btn.active { background:var(--bg-active); color:var(--accent); border-color:var(--accent-dim); }
.meta-preview { width:100%; max-height:340px; overflow:hidden; background:var(--bg-darkest); flex-shrink:0; display:flex; align-items:center; justify-content:center; cursor:pointer; position:relative; }
.meta-preview img { max-width:100%; max-height:340px; object-fit:contain; }
.meta-tabs { display:flex; align-items:center; background:var(--bg-dark); border-bottom:1px solid var(--border); flex-shrink:0; padding-right:4px; }
.meta-tab { flex:1; padding:8px 12px; text-align:center; font-size:11px; font-weight:500; color:var(--text-dim); cursor:pointer; border-bottom:2px solid transparent; transition:all .15s; background:none; border-top:none; border-left:none; border-right:none; }
.meta-tab:hover { color:var(--text); }
.meta-tab.active { color:var(--accent); border-bottom-color:var(--accent); }
.meta-content { flex:1; overflow-y:auto; padding:12px; }
.meta-section { margin-bottom:14px; }
.meta-section-title { display:flex; align-items:center; gap:6px; font-size:10px; font-weight:600; text-transform:uppercase; letter-spacing:.6px; color:var(--text-dim); margin-bottom:6px; padding-bottom:4px; border-bottom:1px solid var(--border); }
.meta-section-title .copy-btn { margin-left:auto; cursor:pointer; color:var(--text-dim); font-size:11px; font-weight:400; text-transform:none; letter-spacing:0; transition:color .15s; }
.meta-section-title .copy-btn:hover { color:var(--accent); }
.meta-prompt { font-family:var(--mono); font-size:11px; line-height:1.6; color:var(--prompt-text); word-break:break-word; white-space:pre-wrap; background:var(--bg-card); padding:8px 10px; border-radius:var(--radius); border:1px solid var(--border); }
.meta-prompt.negative { color:var(--neg-prompt); }
.meta-settings-grid { display:grid; grid-template-columns:auto 1fr; gap:2px 12px; font-family:var(--mono); font-size:11px; }
.meta-key { color:var(--setting-key); white-space:nowrap; }
.meta-val { color:var(--setting-val); word-break:break-all; }
.meta-raw { font-family:var(--mono); font-size:10px; line-height:1.6; color:var(--text); white-space:pre-wrap; word-break:break-all; background:var(--bg-card); padding:10px; border-radius:var(--radius); border:1px solid var(--border); }
.meta-file-info { display:grid; grid-template-columns:auto 1fr; gap:2px 12px; font-size:11px; }
.meta-file-info .label { color:var(--text-dim); }
.meta-file-info .value { color:var(--text); font-family:var(--mono); }
.meta-file-info .path-value { min-width:0; overflow-wrap:anywhere; word-break:break-word; }
.meta-file-info .path-copy { cursor:pointer; color:var(--accent); font-family:var(--font); font-size:10px; margin-left:5px; white-space:nowrap; }
.meta-file-info .path-copy:hover { color:var(--text-bright); }

/* Lightbox */
.lightbox { position:fixed; inset:0; background:rgba(0,0,0,.92); z-index:1000; display:none; align-items:center; justify-content:center; cursor:zoom-out; }
.lightbox.open { display:flex; }
.lightbox img { max-width:95vw; max-height:95vh; object-fit:contain; }
.lightbox-nav { position:absolute; top:50%; transform:translateY(-50%); background:rgba(255,255,255,.1); border:none; color:white; font-size:28px; width:48px; height:64px; cursor:pointer; display:flex; align-items:center; justify-content:center; border-radius:6px; transition:background .15s; }
.lightbox-nav:hover { background:rgba(255,255,255,.2); }
.lightbox-nav.prev { left:16px; }
.lightbox-nav.next { right:16px; }
.lightbox-close, .lightbox-delete { position:absolute; top:16px; background:rgba(255,255,255,.1); border:none; color:white; font-size:20px; width:40px; height:40px; cursor:pointer; border-radius:6px; display:flex; align-items:center; justify-content:center; transition:background .15s, color .15s; }
.lightbox-close { right:16px; }
.lightbox-delete { right:64px; font-size:18px; }
.lightbox-close:hover { background:rgba(255,255,255,.2); }
.lightbox-delete:hover { background:rgba(239,68,68,.25); color:#fff; }
.lightbox-counter { position:absolute; bottom:20px; left:50%; transform:translateX(-50%); color:rgba(255,255,255,.6); font-family:var(--mono); font-size:12px; }

::-webkit-scrollbar { width:6px; height:6px; }
::-webkit-scrollbar-track { background:transparent; }
::-webkit-scrollbar-thumb { background:var(--border-light); border-radius:3px; }
::-webkit-scrollbar-thumb:hover { background:var(--text-dim); }

.loading-bar { position:absolute; top:0; left:0; right:0; height:2px; background:var(--accent); transform:scaleX(0); transform-origin:left; z-index:100; pointer-events:none; }
.loading-bar.active { animation:loading-pulse 1.5s ease-in-out infinite; }
@keyframes loading-pulse { 0%{transform:scaleX(0);opacity:1} 50%{transform:scaleX(.7);opacity:1} 100%{transform:scaleX(1);opacity:0} }

.thumb-slider { width:80px; -webkit-appearance:none; height:3px; background:var(--border-light); border-radius:2px; outline:none; }
.thumb-slider::-webkit-slider-thumb { -webkit-appearance:none; width:12px; height:12px; border-radius:50%; background:var(--accent); cursor:pointer; }

/* Optional Auto Tagger integration */
.ai-tags-toolbar { display:none; }
.ai-tags-toolbar.available { display:inline-flex; }
.ai-filter-popover { position:fixed; top:48px; right:174px; z-index:950; width:300px; display:none; background:var(--bg-panel); border:1px solid var(--border-light); border-radius:8px; padding:10px; box-shadow:0 12px 32px rgba(0,0,0,.48); }
.ai-filter-popover.open { display:block; }
.ai-filter-title { display:flex; align-items:center; justify-content:space-between; color:var(--text-bright); font-size:11px; font-weight:600; margin-bottom:7px; }
.ai-filter-title span { color:var(--text-dim); font-size:9px; font-weight:400; }
.ai-filter-input { width:100%; box-sizing:border-box; border:1px solid var(--border); border-radius:6px; background:var(--bg-card); color:var(--text); font:11px var(--mono); padding:8px 9px; outline:none; }
.ai-filter-input:focus { border-color:var(--accent); }
.ai-filter-actions { display:flex; gap:6px; margin-top:8px; }
.ai-filter-actions button { flex:1; border:1px solid var(--border); border-radius:6px; background:var(--bg-card); color:var(--text); font:11px var(--font); padding:6px; cursor:pointer; }
.ai-filter-actions button.primary { background:var(--accent); border-color:var(--accent); color:#fff; }
.ai-suggestions { display:flex; flex-wrap:wrap; gap:4px; max-height:140px; overflow:auto; margin-top:8px; }
.ai-suggestion { border:1px solid var(--border); border-radius:5px; background:var(--bg-card); color:var(--text-dim); font:10px var(--mono); padding:3px 5px; cursor:pointer; }
.ai-suggestion:hover { color:var(--text); border-color:var(--accent-dim); }
.ai-tag-list { display:flex; flex-wrap:wrap; gap:5px; }
.ai-tag-chip { border:1px solid var(--border); border-radius:5px; background:var(--bg-card); color:#cbd5e1; font:10px var(--mono); padding:3px 5px; cursor:pointer; }
.ai-tag-chip:hover { border-color:var(--accent-dim); background:var(--bg-hover); }
.ai-tag-chip.selected { border-color:var(--accent); background:var(--accent-glow); box-shadow:0 0 0 1px var(--accent-dim); }
.ai-tag-chip:focus-visible { outline:2px solid var(--accent); outline-offset:1px; }
.ai-tag-chip.character { color:#fbbf24; border-color:rgba(251,191,36,.3); }
.ai-tag-chip.rating { color:#34d399; border-color:rgba(52,211,153,.3); }
.ai-tag-chip .score { opacity:.58; margin-left:3px; }
.ai-tag-actions { display:flex; gap:6px; margin-top:7px; }
.ai-tag-actions button { border:1px solid var(--border); border-radius:5px; background:var(--bg-card); color:var(--text); font:10px var(--font); padding:4px 7px; cursor:pointer; }
.ai-tag-actions button:disabled { opacity:.45; cursor:not-allowed; }
.ai-tags-toolbar.active { color:var(--accent); border-color:var(--accent-dim); background:var(--bg-active); }

.toast { position:fixed; bottom:40px; right:16px; background:var(--bg-card); border:1px solid var(--border); color:var(--text); padding:8px 14px; border-radius:var(--radius); font-size:12px; z-index:2000; animation:toast-in .2s ease-out; box-shadow:0 4px 16px rgba(0,0,0,.4); }
@keyframes toast-in { from{opacity:0;transform:translateY(8px)} }

/* Multi-select */
.thumb-card.multi-selected { border-color:var(--orange); box-shadow:0 0 0 1px var(--orange); }
.thumb-card.multi-selected::after {
    content:'\2713'; position:absolute; top:4px; left:4px; width:20px; height:20px;
    background:var(--orange); color:#fff; border-radius:50%; font-size:12px;
    display:flex; align-items:center; justify-content:center; z-index:5;
    pointer-events:none; font-weight:bold;
}

.selection-bar {
    position:fixed; bottom:40px; left:50%; transform:translateX(-50%);
    background:var(--bg-card); border:1px solid var(--orange); border-radius:8px;
    padding:8px 16px; display:none; align-items:center; gap:12px; z-index:900;
    box-shadow:0 8px 32px rgba(0,0,0,.5); font-size:12px;
    animation:toast-in .2s ease-out;
}
.selection-bar.visible { display:flex; }
.selection-bar .sel-count { color:var(--orange); font-family:var(--mono); font-weight:600; }
.selection-bar .sel-btn {
    background:none; border:1px solid var(--border); color:var(--text); font:inherit;
    font-size:11px; padding:4px 12px; border-radius:var(--radius); cursor:pointer; transition:all .15s;
}
.selection-bar .sel-btn:hover { background:var(--bg-hover); }
.selection-bar .sel-btn.danger { border-color:var(--red); color:var(--red); }
.selection-bar .sel-btn.danger:hover { background:rgba(239,68,68,.15); }

.delete-progress {
    position:fixed; right:16px; bottom:40px; z-index:1100; width:min(380px,calc(100vw - 32px));
    display:none; padding:11px 12px; border:1px solid var(--border-light); border-radius:8px;
    background:var(--bg-panel); box-shadow:0 10px 32px rgba(0,0,0,.5);
}
.delete-progress.visible { display:block; }
.delete-progress-head { display:flex; justify-content:space-between; gap:12px; margin-bottom:7px; color:var(--text-bright); font-size:11px; }
.delete-progress-count { color:var(--text-dim); font-family:var(--mono); white-space:nowrap; }
.delete-progress-track { height:4px; overflow:hidden; border-radius:3px; background:var(--border); }
.delete-progress-fill { height:100%; width:0; border-radius:3px; background:var(--red); transition:width .2s ease; }
.delete-progress-message { margin-top:6px; overflow:hidden; color:var(--text-dim); font-size:10px; text-overflow:ellipsis; white-space:nowrap; }

/* Confirm dialog */
.confirm-overlay {
    position:fixed; inset:0; background:rgba(0,0,0,.7); z-index:2000;
    display:none; align-items:center; justify-content:center;
}
.confirm-overlay.open { display:flex; }
.confirm-dialog {
    background:var(--bg-panel); border:1px solid var(--border); border-radius:10px;
    padding:24px; max-width:400px; width:90%;
    box-shadow:0 16px 48px rgba(0,0,0,.6);
}
.confirm-dialog h3 { font-size:15px; color:var(--text-bright); margin-bottom:8px; font-weight:600; }
.confirm-dialog p { font-size:12px; color:var(--text-dim); margin-bottom:16px; line-height:1.6; }
.confirm-dialog .btn-row { display:flex; gap:8px; justify-content:flex-end; }
.confirm-dialog button {
    padding:6px 16px; border-radius:var(--radius); font:inherit; font-size:12px; cursor:pointer; border:1px solid var(--border); transition:all .15s;
}
.confirm-dialog .btn-cancel { background:var(--bg-card); color:var(--text); }
.confirm-dialog .btn-cancel:hover { background:var(--bg-hover); }
.confirm-dialog .btn-delete { background:var(--red); color:#fff; border-color:var(--red); }
.confirm-dialog .btn-delete:hover { background:#dc2626; }
.confirm-dialog button:focus-visible { outline:2px solid var(--accent); outline-offset:2px; }
</style>
</head>
<body class="{BODY_CLASS}">
<div class="app" id="app">
    <div class="topbar">
        {MODULE_NAV}
        <a href="/" class="topbar-title">&#x2B21; {HUB_TITLE}</a>
        <div class="breadcrumb" id="breadcrumb"></div>
        <div class="search-wrap">
            <div class="search-box">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/></svg>
                <input type="text" id="searchInput" placeholder="Search files & prompts...">
            </div>
            <button class="search-clear" id="searchClear">&#x2715; Clear</button>
        </div>
        <div class="topbar-controls">
            <select class="sort-select" id="sortSelect">
                <option value="name-asc">Name A-Z</option>
                <option value="name-desc">Name Z-A</option>
                <option value="date-desc" selected>Newest</option>
                <option value="date-asc">Oldest</option>
                <option value="size-desc">Largest</option>
                <option value="size-asc">Smallest</option>
                <option value="favorite-desc">&#x2B50; Favorites first</option>
            </select>
            <select class="sort-select" id="groupSelect" title="Group current page">
                <option value="none">No grouping</option>
                <option value="family">Group: model family</option>
                <option value="model">Group: exact model</option>
            </select>
            <select class="sort-select metadata-filter-select" id="metadataFilterSelect" title="Filter images by generation metadata">
                <option value="all">All images</option>
                <option value="with">With metadata</option>
                <option value="without">Without metadata</option>
            </select>
            <input type="range" class="thumb-slider" id="thumbSlider" min="100" max="400" value="180" title="Thumbnail size">
            <button class="btn-icon ai-tags-toolbar" id="aiTagsBtn" title="Filter or tag images using visual AI tags" aria-label="AI tags">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M20 13 13 20l-9-9V4h7z"/><circle cx="8.5" cy="8.5" r="1.5"/></svg>
            </button>
            <button class="btn-icon" id="refreshBtn" title="Re-index">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 2v6h-6"/><path d="M3 12a9 9 0 0 1 15-6.7L21 8"/><path d="M3 22v-6h6"/><path d="M21 12a9 9 0 0 1-15 6.7L3 16"/></svg>
            </button>
            <button class="btn-icon" id="metaToggleBtn" title="Toggle metadata panel">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M15 4v16"/><path d="M7 8h4"/><path d="M7 12h4"/></svg>
            </button>
            <button class="btn-icon theme-toggle-btn" type="button" title="Toggle dark/light mode" aria-label="Toggle dark/light mode" onclick="hubToggleTheme()">
                <svg class="theme-icon theme-icon-moon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.8A8.5 8.5 0 1 1 11.2 3 6.6 6.6 0 0 0 21 12.8z"/></svg>
                <svg class="theme-icon theme-icon-sun" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41"/></svg>
            </button>
            <button class="btn-icon help-btn" type="button" id="helpBtn" title="Help for this page" aria-label="Help" data-help-key="gallery" data-help-title="Gallery" onclick="hubOpenHelp()">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M9.1 9a3 3 0 0 1 5.8 1c0 2-3 3-3 3"/><path d="M12 17h.01"/></svg>
            </button>
            <a class="btn-icon" href="/settings" id="settingsLink" title="Settings">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>
            </a>
        </div>
    </div>
    {HELP_OVERLAY}

    <div class="sidebar" id="sidebar">
        <div id="sidebarSpecial">
            <div class="sidebar-special" id="navFavorites">
                <span class="ss-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l2.7 5.5 6.1.9-4.4 4.3 1 6.1L12 17l-5.4 2.8 1-6.1-4.4-4.3 6.1-.9L12 3z"/></svg></span>
                <span>Favorites</span>
                <span class="ss-count" id="favCount">0</span>
            </div>
            <div class="sidebar-special" id="navToday">
                <span class="ss-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="5" width="16" height="15" rx="2"/><path d="M8 3v4M16 3v4M4 10h16"/><path d="M9 14h1M14 14h1M9 17h1"/></svg></span>
                <span>Today</span>
            </div>
            <div class="sidebar-special" id="navWeek">
                <span class="ss-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="5" width="16" height="15" rx="2"/><path d="M8 3v4M16 3v4M4 10h16"/><path d="M8 14h8M8 17h5"/></svg></span>
                <span>Last 7 days</span>
            </div>
            <div class="sidebar-special" id="navMonth">
                <span class="ss-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="5" width="16" height="15" rx="2"/><path d="M8 3v4M16 3v4M4 10h16"/><path d="M8 14h8M8 17h8"/></svg></span>
                <span>Last 30 days</span>
            </div>
            <div class="sidebar-divider"></div>
        </div>
        <div class="sidebar-header">Collections <span class="coll-add-btn" id="collAddBtn">+ new</span></div>
        <div class="coll-list" id="collList"></div>
        <div class="sidebar-divider"></div>
        <div class="sidebar-header" id="sidebarHeader">Folders</div>
        <div id="folderTree"></div>
    </div>

    <div class="gallery-area" id="galleryArea">
        <div class="loading-bar" id="loadingBar"></div>
        <div class="gallery-toolbar" id="galleryToolbar" style="display:none">
            <div class="gt-info" id="galleryCount"></div>
            <div class="gt-nav">
                <button id="gtPrev" title="Previous page">&#x2039;</button>
                <span class="gt-page" id="gtPage" title="Click to jump to page"></span>
                <input class="gt-page-input" id="gtPageInput" type="number" min="1" style="display:none">
                <button id="gtNext" title="Next page">&#x203A;</button>
            </div>
        </div>
        <div class="gallery-grid" id="galleryGrid"></div>
        <div class="gallery-empty" id="galleryEmpty">
            <strong id="galleryEmptyTitle">Loading gallery...</strong>
            <span id="galleryEmptyDetail" style="font-size:11px">If the index is running, new images will appear here as they are processed.</span>
        </div>
        <div class="pagination" id="pagination" style="display:none"></div>
    </div>

    <div class="meta-panel" id="metaPanel">
        <div class="meta-empty">Click an image to view its generation metadata</div>
    </div>

    <div class="lightbox" id="lightbox">
        <img id="lightboxImg" src="">
        <button class="lightbox-nav prev" id="lbPrev">&#x276E;</button>
        <button class="lightbox-nav next" id="lbNext">&#x276F;</button>
        <button class="lightbox-delete" id="lbDelete" title="Move current image to trash">&#x1F5D1;</button>
        <button class="lightbox-close" id="lbClose">&#x2715;</button>
        <div class="lightbox-counter" id="lbCounter"></div>
    </div>

    <div class="statusbar">
        <span id="statusFiles">-</span>
        <span id="statusFolders">-</span>
        <span id="statusMeta">-</span>
        <span id="statusFav">-</span>
        <div class="processing-status" id="processingStatus">
            <span id="processingText"></span>
            <div class="processing-track"><div class="processing-fill" id="processingFill"></div></div>
            <button class="processing-toggle" id="processingToggle" type="button" onclick="toggleBackgroundProcessing()">Pause</button>
        </div>
    </div>
</div>

<div class="ai-filter-popover" id="aiFilterPopover">
    <div class="ai-filter-title">Search AI tags <span>all tags must match</span></div>
    <input class="ai-filter-input" id="aiFilterInput" placeholder="portrait, cafe, sitting" autocomplete="off">
    <div class="ai-suggestions" id="aiSuggestions"></div>
    <div class="ai-filter-actions">
        <button class="primary" id="aiFilterApply">Apply filter</button>
        <button id="aiFilterClear">Clear</button>
    </div>
    <div class="ai-filter-actions">
        <button id="aiTagFolderBtn">Tag current folder</button>
        <button id="aiOpenModuleBtn">Open Auto Tagger</button>
    </div>
</div>

<div class="selection-bar" id="selectionBar">
    <span class="sel-count" id="selCount">0 selected</span>
    <div class="coll-picker">
        <button class="sel-btn" id="selCollBtn">&#x1F4C2; Add to...</button>
        <div class="coll-picker-menu" id="collPickerMenu">
            <input class="coll-picker-input" id="collPickerInput" placeholder="New collection name..." autocomplete="off">
            <div id="collPickerList"></div>
        </div>
    </div>
    <button class="sel-btn" id="selRemColl" style="display:none">&#x2716; Remove from collection</button>
    <button class="sel-btn" id="selAutoTag" style="display:none">AI Tag</button>
    <button class="sel-btn" id="selCompare" onclick="openCompare()">&#x1F50D; Compare</button>
    <button class="sel-btn" id="selClear">Clear</button>
    <button class="sel-btn danger" id="selDelete">&#x1F5D1; Delete</button>
</div>

<div class="delete-progress" id="deleteProgress" role="status" aria-live="polite">
    <div class="delete-progress-head">
        <span id="deleteProgressTitle">Moving images to system trash</span>
        <span class="delete-progress-count" id="deleteProgressCount"></span>
    </div>
    <div class="delete-progress-track"><div class="delete-progress-fill" id="deleteProgressFill"></div></div>
    <div class="delete-progress-message" id="deleteProgressMessage"></div>
</div>

<div class="confirm-overlay" id="confirmOverlay">
    <div class="confirm-dialog">
        <h3 id="confirmTitle">Delete images?</h3>
        <p id="confirmMsg"></p>
        <div class="btn-row">
            <button class="btn-cancel" id="confirmCancel">Cancel</button>
            <button class="btn-delete" id="confirmOk">Delete</button>
        </div>
    </div>
</div>

<script>
/* Hub hamburger menu: toggle on click, close on outside-click or Esc */
(function() {
    function init() {
        document.addEventListener('click', function(ev) {
            var btn = ev.target.closest('.hub-menu-btn');
            var menu = document.querySelector('.hub-menu');
            if (!menu) return;
            if (btn && menu.contains(btn)) {
                ev.preventDefault();
                menu.classList.toggle('open');
            } else if (!ev.target.closest('.hub-menu-panel')) {
                menu.classList.remove('open');
            }
        });
        document.addEventListener('keydown', function(ev) {
            if (ev.key === 'Escape') {
                var menu = document.querySelector('.hub-menu.open');
                if (menu) menu.classList.remove('open');
            }
        });
    }
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
    else init();
})();

var API = {
    get: function(url) {
        return fetch(url).then(function(r){
            return r.json().catch(function(){ return {}; }).then(function(body){
                if (!r.ok) throw new Error(body.error || ('HTTP ' + r.status));
                return body;
            });
        }).catch(function(e){ showToast('Error: '+e.message); return null; });
    },
    post: function(url, data) {
        return fetch(url, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(data)}).then(function(r){
            return r.json().catch(function(){ return {}; }).then(function(body){
                if (!r.ok) throw new Error(body.error || ('HTTP ' + r.status));
                return body;
            });
        }).catch(function(e){ showToast('Error: '+e.message); return null; });
    }
};

var currentFolder = '';
var selectedFile = null;
var selectionAnchorFile = null;
var currentFiles = [];
var currentMetaTab = 'metadata';
var accordionFolders = {ACCORDION_FOLDERS};
var skipDeleteConfirmation = {SKIP_DELETE_CONFIRMATION};
var folderSortMode = '{FOLDER_SORT}';
var galleryGroupMode = localStorage.getItem('galleryGroupMode') || 'none';
var metadataFilter = localStorage.getItem('galleryMetadataFilter') || 'all';
if (['all', 'with', 'without'].indexOf(metadataFilter) < 0) metadataFilter = 'all';
var autoTaggerAvailable = {AUTO_TAGGER_AVAILABLE};
var aiTagFilter = [];
var aiTagFolder = '';
// Folder-tree expansion is remembered across navigation/sessions.
var expandedFolders = new Set();
try { expandedFolders = new Set(JSON.parse(localStorage.getItem('galleryExpanded') || '[]')); } catch (e) {}
if (accordionFolders && expandedFolders.size > 1) expandedFolders = new Set();
function saveExpandedFolders() { try { localStorage.setItem('galleryExpanded', JSON.stringify(Array.from(expandedFolders))); } catch (e) {} }
var lightboxIndex = 0;
var currentPage = 1;
var totalPages = 1;
var searchMode = false;
var searchQuery = '';
var searchFolderFilter = null;
var multiSelected = new Set(); // Set of file paths
var hasTrash = true; // Updated at init from server
var metaCache = {}; // path -> metadata cache
var lastGalleryTotal = 0;
var specialView = null; // null, 'favorites', timeline, collection, or 'ai-tags'
var metaPanelPinned = localStorage.getItem('galleryMetaPinned') === '1';
var metaPanelCollapsed = !metaPanelPinned;
var GALLERY_STATE_KEY = 'cyberdelia.gallery.state';
var pendingRestoreScroll = null;
var galleryStateTimer = null;
var ICON_FOLDER = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7.5A2.5 2.5 0 0 1 5.5 5H10l2 2h6.5A2.5 2.5 0 0 1 21 9.5v7A2.5 2.5 0 0 1 18.5 19h-13A2.5 2.5 0 0 1 3 16.5v-9z"/></svg>';
var ICON_ALL_IMAGES = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></svg>';
var ICON_SEARCH = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="7"/><path d="M20 20l-4-4"/></svg>';
var ICON_PIN = '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 17v5"/><path d="M9 10.8a2 2 0 0 1-1.1 1.8l-1.8.9A2 2 0 0 0 5 15.2V16a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-.8a2 2 0 0 0-1.1-1.8l-1.8-.9A2 2 0 0 1 15 10.8V7a1 1 0 0 1 1-1 2 2 0 0 0 0-4H8a2 2 0 0 0 0 4 1 1 0 0 1 1 1z"/></svg>';

function loadGalleryState() {
    try { return JSON.parse(localStorage.getItem(GALLERY_STATE_KEY) || '{}') || {}; }
    catch (e) { return {}; }
}

function saveGalleryStateNow() {
    try {
        var area = document.getElementById('galleryArea');
        localStorage.setItem(GALLERY_STATE_KEY, JSON.stringify({
            folder: currentFolder || '',
            page: currentPage || 1,
            sort: document.getElementById('sortSelect') ? document.getElementById('sortSelect').value : 'date-desc',
            group: document.getElementById('groupSelect') ? document.getElementById('groupSelect').value : galleryGroupMode,
            metadataFilter: metadataFilter,
            searchMode: !!searchMode,
            searchQuery: searchQuery || '',
            searchFolderFilter: searchFolderFilter,
            specialView: specialView || '',
            aiTags: aiTagFilter.slice(),
            aiFolder: aiTagFolder || '',
            selectedImage: selectedFile || '',
            scrollTop: area ? area.scrollTop : 0
        }));
    } catch (e) {}
}

function modelInfoParam() {
    return galleryGroupMode === 'none' ? '' : '&models=1';
}

function metadataFilterParam() {
    return '&metadata=' + encodeURIComponent(metadataFilter || 'all');
}

function scheduleGalleryStateSave() {
    clearTimeout(galleryStateTimer);
    galleryStateTimer = setTimeout(saveGalleryStateNow, 150);
}

function restoreGalleryScrollIfNeeded() {
    if (pendingRestoreScroll === null || pendingRestoreScroll === undefined) return;
    var y = pendingRestoreScroll;
    pendingRestoreScroll = null;
    setTimeout(function() {
        var area = document.getElementById('galleryArea');
        if (area) area.scrollTop = y || 0;
    }, 80);
}

window.addEventListener('pagehide', saveGalleryStateNow);
document.addEventListener('visibilitychange', function() {
    if (document.visibilityState === 'hidden') saveGalleryStateNow();
});

function applyMetaPanelState() {
    var app = document.getElementById('app');
    var toggleBtn = document.getElementById('metaToggleBtn');
    if (!app) return;
    app.classList.toggle('meta-collapsed', metaPanelCollapsed);
    if (toggleBtn) {
        toggleBtn.classList.toggle('active', !metaPanelCollapsed);
        toggleBtn.title = metaPanelCollapsed ? 'Show metadata panel' : 'Hide metadata panel';
    }
}

function setMetaPanelCollapsed(collapsed) {
    metaPanelCollapsed = collapsed;
    applyMetaPanelState();
}

function setMetaPanelPinned(pinned) {
    metaPanelPinned = pinned;
    localStorage.setItem('galleryMetaPinned', pinned ? '1' : '0');
    applyMetaPanelState();
}

// ═══════════════════════════════════════════════════════
// ── Folder Tree (lazy-load)
// ═══════════════════════════════════════════════════════

async function loadFolderTree() {
    var tree = document.getElementById('folderTree');
    tree.innerHTML = '';
    document.getElementById('sidebarHeader').innerHTML = 'Folders';

    var rootWrap = document.createElement('div');
    rootWrap.className = 'folder-wrapper';
    var rootEl = document.createElement('div');
    rootEl.className = 'folder-item active';
    rootEl.dataset.path = '';
    rootEl.innerHTML = '<span class="folder-toggle open">&#x25B6;</span><div class="folder-icon">' + ICON_ALL_IMAGES + '</div><span class="folder-name">All images</span>';
    rootEl.addEventListener('click', function() {
        exitSearchMode();
        document.querySelectorAll('.folder-item').forEach(function(f){f.classList.remove('active')});
        rootEl.classList.add('active');
        navigateTo('');
    });
    rootWrap.appendChild(rootEl);
    var rootChildren = document.createElement('div');
    rootChildren.className = 'folder-children open';
    rootWrap.appendChild(rootChildren);
    tree.appendChild(rootWrap);

    var data = await API.get('/api/folders?parent=&active=' + encodeURIComponent(currentFolder || ''));
    if (!data) return;
    for (var i = 0; i < data.length; i++) {
        rootChildren.appendChild(createFolderItem(data[i], 1));
    }
}

async function loadChildrenFor(wrapper, parentPath) {
    var childDiv = wrapper.querySelector(':scope > .folder-children');
    if (!childDiv) { childDiv = document.createElement('div'); childDiv.className = 'folder-children'; wrapper.appendChild(childDiv); }
    if (childDiv.dataset.loaded) return;
    childDiv.dataset.loaded = '1';
    var data = await API.get('/api/folders?parent=' + encodeURIComponent(parentPath) + '&active=' + encodeURIComponent(currentFolder || ''));
    if (!data) return;
    for (var i = 0; i < data.length; i++) {
        childDiv.appendChild(createFolderItem(data[i], getDepth(data[i].path)));
    }
}

function getDepth(path) { return path ? path.split(/[\\/]/).length : 0; }

function closeSiblingFolders(wrapper, folderPath) {
    if (!accordionFolders || !wrapper || !wrapper.parentElement) return;
    var siblings = wrapper.parentElement.children;
    for (var i = 0; i < siblings.length; i++) {
        var sib = siblings[i];
        if (sib === wrapper || !sib.classList || !sib.classList.contains('folder-wrapper')) continue;
        var sibItem = sib.querySelector(':scope > .folder-item');
        var sibChildren = sib.querySelector(':scope > .folder-children');
        var sibToggle = sibItem ? sibItem.querySelector('.folder-toggle') : null;
        if (sibChildren) sibChildren.classList.remove('open');
        if (sibToggle) sibToggle.classList.remove('open');
        if (sibItem && sibItem.dataset && sibItem.dataset.path) {
            expandedFolders.delete(sibItem.dataset.path);
        }
    }
    if (folderPath) {
        expandedFolders.forEach(function(p) {
            if (p !== folderPath && folderPath.indexOf(p + '/') !== 0 && p.indexOf(folderPath + '/') !== 0) {
                expandedFolders.delete(p);
            }
        });
    }
}

function shouldRestoreFolderOpen(path) {
    if (!path) return false;
    if (!accordionFolders) return expandedFolders.has(path);
    return !!currentFolder && (currentFolder === path || currentFolder.indexOf(path + '/') === 0);
}

function createFolderItem(folder, depth) {
    var wrapper = document.createElement('div');
    wrapper.className = 'folder-wrapper';
    var el = document.createElement('div');
    el.className = 'folder-item';
    el.style.paddingLeft = (8 + depth * 18) + 'px';
    el.dataset.path = folder.path;
    var toggleClass = folder.has_children ? 'folder-toggle' : 'folder-toggle empty';
    var iconHtml = folder.cover
        ? '<div class="folder-icon"><img src="/thumb/' + encodeURIComponent(folder.cover) + '" loading="lazy"></div>'
        : '<div class="folder-icon">' + ICON_FOLDER + '</div>';
    el.innerHTML = '<span class="' + toggleClass + '">&#x25B6;</span>' + iconHtml +
        '<span class="folder-name">' + escHtml(folder.name) + '</span>' +
        (folder.count > 0 ? '<span class="folder-count">' + folder.count + '</span>' : '');
    wrapper.appendChild(el);
    if (folder.has_children) {
        var childDiv = document.createElement('div');
        childDiv.className = 'folder-children';
        wrapper.appendChild(childDiv);
    }
    el.addEventListener('click', async function() {
        document.querySelectorAll('.folder-item').forEach(function(f){f.classList.remove('active')});
        el.classList.add('active');
        if (accordionFolders) closeSiblingFolders(wrapper, folder.path);
        if (searchMode) {
            searchFolderFilter = folder.path;
            currentPage = 1;
            doSearch();
        } else {
            navigateTo(folder.path);
        }
        if (folder.has_children) {
            var toggle = el.querySelector('.folder-toggle');
            var children = wrapper.querySelector(':scope > .folder-children');
            if (children) {
                if (children.classList.contains('open')) {
                    children.classList.remove('open'); toggle.classList.remove('open');
                    expandedFolders.delete(folder.path); saveExpandedFolders();
                } else {
                    await loadChildrenFor(wrapper, folder.path);
                    children.classList.add('open'); toggle.classList.add('open');
                    expandedFolders.add(folder.path); saveExpandedFolders();
                }
            }
        } else if (accordionFolders) {
            saveExpandedFolders();
        }
    });
    // Highlight the folder you're currently browsing.
    if (folder.path === currentFolder) el.classList.add('active');
    // Restore saved expansion: open this folder and load its children (which will
    // themselves auto-expand if they were saved as open).
    if (folder.has_children && shouldRestoreFolderOpen(folder.path)) {
        var savedToggle = el.querySelector('.folder-toggle');
        var savedChildren = wrapper.querySelector(':scope > .folder-children');
        loadChildrenFor(wrapper, folder.path).then(function() {
            if (savedChildren) savedChildren.classList.add('open');
            if (savedToggle) savedToggle.classList.add('open');
        });
    }
    return wrapper;
}

// ═══════════════════════════════════════════════════════
// ── Search with folder filtering
// ═══════════════════════════════════════════════════════

function renderSearchFolders(folders, activeFolder) {
    var tree = document.getElementById('folderTree');
    tree.innerHTML = '';
    document.getElementById('sidebarHeader').innerHTML =
        'Results <span class="search-badge">' + folders.length + ' folders</span>';

    // "All folders" item
    var allEl = document.createElement('div');
    allEl.className = 'folder-item' + (activeFolder === null ? ' active' : '');
    allEl.style.paddingLeft = '8px';
    allEl.innerHTML = '<span class="folder-toggle empty">&#x25B6;</span><div class="folder-icon">' + ICON_SEARCH + '</div>' +
        '<span class="folder-name">All folders</span>';
    allEl.addEventListener('click', function() {
        document.querySelectorAll('.folder-item').forEach(function(f){f.classList.remove('active')});
        allEl.classList.add('active');
        searchFolderFilter = null;
        currentPage = 1;
        doSearch();
    });
    tree.appendChild(allEl);

    for (var i = 0; i < folders.length; i++) {
        var f = folders[i];
        var el = document.createElement('div');
        el.className = 'folder-item' + (activeFolder === f.path ? ' active' : '');
        el.style.paddingLeft = '26px';
        el.dataset.path = f.path;
        el.innerHTML = '<div class="folder-icon">' + ICON_FOLDER + '</div>' +
            '<span class="folder-name">' + escHtml(f.name || f.path || 'Root') + '</span>' +
            '<span class="folder-count search-count">' + f.count + '</span>';
        el.addEventListener('click', (function(fp) {
            return function() {
                document.querySelectorAll('.folder-item').forEach(function(x){x.classList.remove('active')});
                this.classList.add('active');
                searchFolderFilter = fp;
                currentPage = 1;
                doSearch();
            };
        })(f.path));
        tree.appendChild(el);
    }
}

var searchTimeout;
document.getElementById('searchInput').addEventListener('input', function(e) {
    clearTimeout(searchTimeout);
    var q = e.target.value.trim();
    searchTimeout = setTimeout(function() {
        if (q.length < 2) { exitSearchMode(); loadGallery(currentFolder); return; }
        clearSpecialView();
        searchQuery = q;
        searchFolderFilter = null;
        currentPage = 1;
        searchMode = true;
        document.getElementById('searchClear').classList.add('visible');
        doSearch();
        scheduleGalleryStateSave();
    }, 300);
});

document.getElementById('searchClear').addEventListener('click', function() {
    document.getElementById('searchInput').value = '';
    exitSearchMode();
    loadFolderTree();
    loadGallery(currentFolder);
});

function exitSearchMode() {
    searchMode = false;
    searchQuery = '';
    searchFolderFilter = null;
    currentPage = 1;
    document.getElementById('searchClear').classList.remove('visible');
    scheduleGalleryStateSave();
}

async function doSearch() {
    var loading = document.getElementById('loadingBar');
    loading.classList.add('active');
    showGalleryPlaceholder('Searching...', 'Large metadata searches can take a moment while the gallery index is being updated.');

    var sort = document.getElementById('sortSelect').value;
    var parts = sort.split('-');
    var sortParam = '&sort=' + parts[0] + '&order=' + parts[1];

    var url;
    if (searchFolderFilter !== null) {
        url = '/api/search_folder?q=' + encodeURIComponent(searchQuery) +
              '&folder=' + encodeURIComponent(searchFolderFilter) +
              '&page=' + currentPage + sortParam + modelInfoParam() + metadataFilterParam();
    } else {
        url = '/api/search?q=' + encodeURIComponent(searchQuery) + '&page=' + currentPage + sortParam + modelInfoParam() + metadataFilterParam();
    }

    var data = await API.get(url);
    loading.classList.remove('active');
    if (!data) return;

    // Update folder sidebar (only on global search, not folder-filtered)
    if (searchFolderFilter === null && data.folders) {
        renderSearchFolders(data.folders, null);
    } else if (searchFolderFilter !== null && !data.folders) {
        // Keep current sidebar but update active state
    }

    currentFiles = data.files || [];
    totalPages = data.pages || 1;
    currentPage = data.page || 1;

    renderGalleryFiles(currentFiles, data.total || 0, true);
    renderPagination();
    updateBreadcrumb(searchFolderFilter !== null ? searchFolderFilter : '');
    scheduleGalleryStateSave();
}

// ═══════════════════════════════════════════════════════
// ── Optional Auto Tagger filters
// ═══════════════════════════════════════════════════════

function parseAiTags(value) {
    var out = [];
    String(value || '').split(',').forEach(function(tag) {
        tag = tag.trim().toLowerCase().replace(/\s+/g, '_');
        if (tag && out.indexOf(tag) < 0) out.push(tag);
    });
    return out;
}

function updateAiFilterButton() {
    var btn = document.getElementById('aiTagsBtn');
    if (!btn) return;
    btn.classList.toggle('active', specialView === 'ai-tags' && aiTagFilter.length > 0);
    btn.title = aiTagFilter.length ? ('AI tags: ' + aiTagFilter.join(', ')) : 'Filter or tag images using visual AI tags';
}

async function loadAiTagView(resetPage) {
    if (!autoTaggerAvailable || !aiTagFilter.length) return;
    if (resetPage === undefined) resetPage = true;
    activateSpecialView('ai-tags', null, resetPage);
    updateAiFilterButton();
    var sort = document.getElementById('sortSelect').value.split('-');
    var url = '/api/auto_tagger/files?tags=' + encodeURIComponent(aiTagFilter.join(',')) +
        '&folder=' + encodeURIComponent(aiTagFolder || '') +
        '&sort=' + sort[0] + '&order=' + sort[1] + '&page=' + currentPage + modelInfoParam() + metadataFilterParam();
    var data = await API.get(url);
    if (!data) return;
    currentFiles = data.files || [];
    totalPages = data.pages || 1;
    currentPage = data.page || 1;
    renderGalleryFiles(currentFiles, data.total || 0, true);
    renderPagination();
    updateToolbarNav();
    updateBreadcrumb(aiTagFolder || '');
    var bc = document.getElementById('breadcrumb');
    if (bc) bc.insertAdjacentHTML('beforeend', '<span class="sep">&#x203A;</span><span style="color:var(--accent);cursor:default">AI: ' + escHtml(aiTagFilter.join(', ')) + '</span>');
    scheduleGalleryStateSave();
}

async function startAiTagJob(payload) {
    if (!autoTaggerAvailable) return;
    var result = await API.post('/api/auto_tagger/job', Object.assign({action:'start'}, payload || {}));
    if (result && result.ok) showToast(result.message || 'Auto tagging started');
}

var aiSuggestTimer = null;
async function updateAiSuggestions() {
    var input = document.getElementById('aiFilterInput');
    var list = document.getElementById('aiSuggestions');
    if (!input || !list) return;
    var parts = input.value.split(',');
    var query = (parts[parts.length - 1] || '').trim();
    var rows = await API.get('/api/auto_tagger/suggest?q=' + encodeURIComponent(query) + '&limit=24');
    if (!rows) return;
    list.innerHTML = rows.map(function(tag) {
        return '<button class="ai-suggestion" type="button" data-tag="' + escAttr(tag.name) + '">' +
            escHtml(String(tag.name).replace(/_/g, ' ')) + ' (' + Number(tag.count || 0).toLocaleString() + ')</button>';
    }).join('');
    list.querySelectorAll('[data-tag]').forEach(function(button) {
        button.addEventListener('click', function() {
            var values = input.value.split(',').map(function(v){return v.trim();}).filter(Boolean);
            if (values.length && values[values.length - 1].toLowerCase().indexOf(query.toLowerCase()) >= 0) values.pop();
            values.push(button.dataset.tag);
            input.value = values.join(', ') + ', ';
            input.focus();
            updateAiSuggestions();
        });
    });
}

function initAutoTaggerUi() {
    var button = document.getElementById('aiTagsBtn');
    var popover = document.getElementById('aiFilterPopover');
    var selectionButton = document.getElementById('selAutoTag');
    if (!autoTaggerAvailable) {
        if (popover) popover.remove();
        return;
    }
    button.classList.add('available');
    selectionButton.style.display = '';
    button.addEventListener('click', function(e) {
        e.stopPropagation();
        popover.classList.toggle('open');
        if (popover.classList.contains('open')) {
            document.getElementById('aiTagFolderBtn').textContent = currentFolder ? 'Tag current folder' : 'Tag all untagged';
            document.getElementById('aiFilterInput').focus();
            updateAiSuggestions();
        }
    });
    popover.addEventListener('click', function(e){ e.stopPropagation(); });
    document.addEventListener('click', function(){ popover.classList.remove('open'); });
    document.getElementById('aiFilterInput').addEventListener('input', function() {
        clearTimeout(aiSuggestTimer);
        aiSuggestTimer = setTimeout(updateAiSuggestions, 220);
    });
    document.getElementById('aiFilterInput').addEventListener('keydown', function(e) {
        if (e.key === 'Enter') document.getElementById('aiFilterApply').click();
    });
    document.getElementById('aiFilterApply').addEventListener('click', function() {
        aiTagFilter = parseAiTags(document.getElementById('aiFilterInput').value);
        aiTagFolder = currentFolder || '';
        if (!aiTagFilter.length) return;
        popover.classList.remove('open');
        loadAiTagView(true);
    });
    document.getElementById('aiFilterClear').addEventListener('click', function() {
        aiTagFilter = [];
        aiTagFolder = '';
        document.getElementById('aiFilterInput').value = '';
        popover.classList.remove('open');
        clearSpecialView();
        updateAiFilterButton();
        loadGallery(currentFolder);
    });
    document.getElementById('aiTagFolderBtn').addEventListener('click', function() {
        popover.classList.remove('open');
        startAiTagJob(currentFolder ? {scope:'folder', folder:currentFolder} : {scope:'all'});
    });
    document.getElementById('aiOpenModuleBtn').addEventListener('click', function() {
        window.location.href = '/auto-tagger';
    });
    selectionButton.addEventListener('click', function() {
        startAiTagJob({scope:'paths', paths:Array.from(multiSelected)});
    });
    updateAiFilterButton();
}

// ═══════════════════════════════════════════════════════
// ── Gallery
// ═══════════════════════════════════════════════════════

async function navigateTo(folder) {
    currentFolder = folder;
    currentPage = 1;
    metaCache = {};
    clearSpecialView();
    updateBreadcrumb(folder);
    try { localStorage.setItem('galleryFolder', folder || ''); } catch (e) {}
    await loadGallery(folder);
    scheduleGalleryStateSave();
}

function updateBreadcrumb(folder) {
    var bc = document.getElementById('breadcrumb');
    var parts = folder ? folder.split(/[\\/]/) : [];
    var html = '<span data-path="">All images</span>';
    var path = '';
    for (var i = 0; i < parts.length; i++) {
        path += (path ? '/' : '') + parts[i];
        html += '<span class="sep">&#x203A;</span><span data-path="' + escAttr(path) + '">' + escHtml(parts[i]) + '</span>';
    }
    if (searchMode) {
        html += '<span class="sep">&#x203A;</span><span style="color:var(--accent);cursor:default">&#x1F50D; "' + escHtml(searchQuery) + '"</span>';
    }
    bc.innerHTML = html;
    bc.querySelectorAll('span[data-path]').forEach(function(s) {
        s.addEventListener('click', function() {
            if (searchMode) return; // don't navigate away during search
            navigateTo(s.dataset.path);
        });
    });
}

async function loadGallery(folder) {
    var sort = document.getElementById('sortSelect').value;
    var parts = sort.split('-');
    var loading = document.getElementById('loadingBar');
    loading.classList.add('active');
    showGalleryPlaceholder('Loading gallery...', 'If the index is running, new images will appear here as they are processed.');

    var data = await API.get('/api/files?folder=' + encodeURIComponent(folder) +
        '&sort=' + parts[0] + '&order=' + parts[1] + '&page=' + currentPage + modelInfoParam() + metadataFilterParam());
    loading.classList.remove('active');
    if (!data) return;

    currentFiles = data.files || [];
    totalPages = data.pages || 1;
    currentPage = data.page || 1;

    renderGalleryFiles(currentFiles, data.total || 0, false);
    renderPagination();
    scheduleGalleryStateSave();
}

function showGalleryPlaceholder(title, detail) {
    var grid = document.getElementById('galleryGrid');
    var empty = document.getElementById('galleryEmpty');
    var titleEl = document.getElementById('galleryEmptyTitle');
    var detailEl = document.getElementById('galleryEmptyDetail');
    if (grid) grid.style.display = 'none';
    if (titleEl) titleEl.textContent = title || 'Loading gallery...';
    if (detailEl) detailEl.textContent = detail || '';
    if (empty) empty.style.display = 'flex';
}

function safeModelColor(color) {
    return /^#[0-9a-fA-F]{6}$/.test(color || '') ? color : '#64748b';
}

function getFileGroupInfo(file) {
    if (galleryGroupMode === 'model') {
        return {
            key: file.model_exact_key || 'unknown',
            label: file.model_exact_label || 'Unknown model',
            color: safeModelColor(file.model_group_color)
        };
    }
    return {
        key: file.model_family_key || 'unknown',
        label: file.model_family_label || 'Unknown model',
        color: safeModelColor(file.model_group_color)
    };
}

function createGalleryGroupHeader(info, count) {
    var header = document.createElement('div');
    header.className = 'gallery-group-header';
    header.style.setProperty('--model-color', info.color);
    header.innerHTML =
        '<span class="gallery-group-dot"></span>' +
        '<span>' + escHtml(info.label) + '</span>' +
        '<span class="gallery-group-count">' + count + '</span>';
    return header;
}

function createThumbCard(file, index) {
    var card = document.createElement('div');
    card.className = 'thumb-card';
    card.dataset.path = file.path;
    card.dataset.index = index;
    var dimsText = (file.width && file.height) ? file.width + '\u00D7' + file.height : '';
    var favClass = file.favorite ? 'thumb-fav active' : 'thumb-fav';
    var favStar = file.favorite ? '\u2605' : '\u2606';
    var groupInfo = getFileGroupInfo(file);
    var showModelBadge = galleryGroupMode === 'none' && groupInfo.key !== 'unknown';
    card.style.setProperty('--model-color', groupInfo.color);
    card.innerHTML =
        '<img data-src="/thumb/' + encodeURIComponent(file.path) + '" alt="' + escAttr(file.name) + '" loading="lazy">' +
        '<span class="' + favClass + '" data-path="' + escAttr(file.path) + '">' + favStar + '</span>' +
        (file.has_metadata ? '<div class="thumb-meta-badge" title="Metadata found"></div>' : '') +
        (file.processing ? '<div class="thumb-processing" title="Metadata and thumbnail are being processed">Processing</div>' : '') +
        (dimsText ? '<div class="thumb-dims">' + dimsText + '</div>' : '') +
        (showModelBadge ? '<div class="thumb-model-badge">' + escHtml(groupInfo.label) + '</div>' : '') +
        '<div class="thumb-overlay"><div class="thumb-name">' + escHtml(file.name) + '</div></div>';
    // Star click handler (stop propagation so card click doesn't fire)
    card.querySelector('.thumb-fav').addEventListener('click', (function(fp) {
        return function(e) {
            e.stopPropagation();
            e.preventDefault();
            toggleFavorite(fp);
        };
    })(file.path));
    card.addEventListener('click', (function(fp,c,idx){return function(e){
        if (e.ctrlKey || e.metaKey) {
            // Toggle multi-select
            e.preventDefault();
            toggleMultiSelect(fp, c, true);
        } else if (e.shiftKey && selectedFile) {
            // Range select
            e.preventDefault();
            rangeSelect(idx);
        } else {
            // Normal click: clear multi-select if any, then select for metadata
            if (multiSelected.size > 0) { clearMultiSelect(); }
            selectImage(fp, c);
        }
    }})(file.path, card, index));
    card.addEventListener('dblclick', (function(idx,fp){return function(){lightboxIndex=idx; openLightbox('/image/'+encodeURIComponent(fp))}})(index, file.path));
    // Restore multi-select state if re-rendering same page
    if (multiSelected.has(file.path)) { card.classList.add('multi-selected'); }
    return card;
}

function renderGalleryFiles(files, total, isSearch) {
    var grid = document.getElementById('galleryGrid');
    var empty = document.getElementById('galleryEmpty');
    var toolbar = document.getElementById('galleryToolbar');
    var countEl = document.getElementById('galleryCount');
    lastGalleryTotal = total || files.length || 0;

    if (files.length === 0) {
        var metadataFiltered = metadataFilter !== 'all';
        showGalleryPlaceholder(
            isSearch ? 'No results found' : (metadataFiltered ? 'No images match this metadata filter' : 'No images in this folder'),
            isSearch
                ? 'Try another search term or wait until the background index has finished.'
                : (metadataFiltered
                    ? 'Choose another metadata option or folder.'
                    : 'This folder may be empty, or the background index may still be processing new images.')
        );
        toolbar.style.display = 'none';
        document.getElementById('pagination').style.display = 'none';
        return;
    }

    grid.style.display = 'grid';
    empty.style.display = 'none';
    toolbar.style.display = 'flex';
    var label = isSearch ? ' results' : ' images';
    var metaCount = files.filter(function(f){ return f.has_metadata; }).length;
    var pageDetail = files.length < total ? ' · showing ' + files.length : '';
    var metaDetail = metaCount ? ' · ' + metaCount + ' with metadata on page' : '';
    countEl.textContent = total + label + pageDetail + metaDetail;
    updateToolbarNav();
    grid.innerHTML = '';

    var observer = new IntersectionObserver(function(entries) {
        entries.forEach(function(entry) {
            if (entry.isIntersecting) {
                var img = entry.target.querySelector('img');
                if (img && img.dataset.src) { img.src = img.dataset.src; img.removeAttribute('data-src'); }
                observer.unobserve(entry.target);
            }
        });
    }, { rootMargin: '300px' });

    if (galleryGroupMode === 'none') {
        for (var i = 0; i < files.length; i++) {
            var card = createThumbCard(files[i], i);
            grid.appendChild(card);
            observer.observe(card);
        }
    } else {
        var groups = [];
        var groupMap = {};
        for (var gi = 0; gi < files.length; gi++) {
            var info = getFileGroupInfo(files[gi]);
            if (!groupMap[info.key]) {
                groupMap[info.key] = { info: info, items: [] };
                groups.push(groupMap[info.key]);
            }
            groupMap[info.key].items.push({ file: files[gi], index: gi });
        }
        groups.forEach(function(group) {
            grid.appendChild(createGalleryGroupHeader(group.info, group.items.length));
            group.items.forEach(function(item) {
                var card = createThumbCard(item.file, item.index);
                grid.appendChild(card);
                observer.observe(card);
            });
        });
    }
    // Prefetch next page thumbnails
    restoreGalleryScrollIfNeeded();
    scheduleGalleryStateSave();
    if (currentPage < totalPages) {
        prefetchNextPage();
    }
}

function prefetchNextPage() {
    // Fetch next page file list silently, then preload their thumb URLs
    var nextPage = currentPage + 1;
    var url;
    if (searchMode) {
        var sort = document.getElementById('sortSelect').value;
        var parts = sort.split('-');
        var sortParam = '&sort=' + parts[0] + '&order=' + parts[1];
        if (searchFolderFilter !== null) {
            url = '/api/search_folder?q=' + encodeURIComponent(searchQuery) +
                  '&folder=' + encodeURIComponent(searchFolderFilter) +
                  '&page=' + nextPage + sortParam + modelInfoParam() + metadataFilterParam();
        } else {
            url = '/api/search?q=' + encodeURIComponent(searchQuery) + '&page=' + nextPage + sortParam + modelInfoParam() + metadataFilterParam();
        }
    } else if (specialView === 'ai-tags' && aiTagFilter.length) {
        var sort = document.getElementById('sortSelect').value;
        var parts = sort.split('-');
        url = '/api/auto_tagger/files?tags=' + encodeURIComponent(aiTagFilter.join(',')) +
              '&folder=' + encodeURIComponent(aiTagFolder || '') +
              '&sort=' + parts[0] + '&order=' + parts[1] + '&page=' + nextPage + modelInfoParam() + metadataFilterParam();
    } else {
        var sort = document.getElementById('sortSelect').value;
        var parts = sort.split('-');
        url = '/api/files?folder=' + encodeURIComponent(currentFolder) +
              '&sort=' + parts[0] + '&order=' + parts[1] + '&page=' + nextPage + modelInfoParam() + metadataFilterParam();
    }
    // Keep navigation warm without competing with visible thumbnails or indexing.
    var runPrefetch = function() {
        fetch(url).then(function(r) { return r.json(); }).then(function(data) {
            if (!data || !data.files) return;
            data.files.slice(0, 12).forEach(function(f) {
                var link = document.createElement('link');
                link.rel = 'prefetch';
                link.href = '/thumb/' + encodeURIComponent(f.path);
                link.as = 'image';
                document.head.appendChild(link);
            });
        }).catch(function() {});
    };
    if (window.requestIdleCallback) requestIdleCallback(runPrefetch, {timeout: 1200});
    else setTimeout(runPrefetch, 350);
}

// ═══════════════════════════════════════════════════════
// ── Pagination
// ═══════════════════════════════════════════════════════

function updateToolbarNav() {
    var pageEl = document.getElementById('gtPage');
    var pageInput = document.getElementById('gtPageInput');
    var prevBtn = document.getElementById('gtPrev');
    var nextBtn = document.getElementById('gtNext');
    pageInput.style.display = 'none';
    pageEl.style.display = '';
    if (totalPages <= 1) {
        pageEl.textContent = '';
        prevBtn.style.display = 'none';
        nextBtn.style.display = 'none';
    } else {
        pageEl.textContent = currentPage + ' / ' + totalPages;
        prevBtn.style.display = '';
        nextBtn.style.display = '';
        prevBtn.disabled = currentPage <= 1;
        nextBtn.disabled = currentPage >= totalPages;
    }
}

document.getElementById('gtPrev').addEventListener('click', function() { if (currentPage > 1) goToPage(currentPage - 1); });
document.getElementById('gtNext').addEventListener('click', function() { if (currentPage < totalPages) goToPage(currentPage + 1); });

// Click page indicator to jump
document.getElementById('gtPage').addEventListener('click', function() {
    if (totalPages <= 1) return;
    var pageEl = document.getElementById('gtPage');
    var pageInput = document.getElementById('gtPageInput');
    pageEl.style.display = 'none';
    pageInput.style.display = '';
    pageInput.max = totalPages;
    pageInput.value = currentPage;
    pageInput.focus();
    pageInput.select();
});

document.getElementById('gtPageInput').addEventListener('keydown', function(e) {
    if (e.key === 'Enter') {
        var val = parseInt(this.value);
        if (val >= 1 && val <= totalPages && val !== currentPage) {
            goToPage(val);
        } else {
            updateToolbarNav(); // reset display
        }
    } else if (e.key === 'Escape') {
        updateToolbarNav();
    }
});

document.getElementById('gtPageInput').addEventListener('blur', function() {
    updateToolbarNav();
});
function renderPagination() {
    var container = document.getElementById('pagination');
    if (totalPages <= 1) { container.style.display = 'none'; return; }
    container.style.display = 'flex';
    container.innerHTML = '';

    function addBtn(label, page, disabled, active) {
        var b = document.createElement('button');
        b.textContent = label;
        b.disabled = disabled;
        if (active) b.className = 'active';
        if (!disabled) b.addEventListener('click', function() { goToPage(page); });
        container.appendChild(b);
    }

    addBtn('\u00AB', 1, currentPage === 1, false);
    addBtn('\u2039', currentPage - 1, currentPage === 1, false);

    // Page numbers: show window around current page
    var start = Math.max(1, currentPage - 3);
    var end = Math.min(totalPages, currentPage + 3);
    if (start > 1) {
        addBtn('1', 1, false, currentPage === 1);
        if (start > 2) { var s = document.createElement('span'); s.className = 'page-info'; s.textContent = '...'; container.appendChild(s); }
    }
    for (var p = start; p <= end; p++) {
        addBtn('' + p, p, false, p === currentPage);
    }
    if (end < totalPages) {
        if (end < totalPages - 1) { var s = document.createElement('span'); s.className = 'page-info'; s.textContent = '...'; container.appendChild(s); }
        addBtn('' + totalPages, totalPages, false, currentPage === totalPages);
    }

    addBtn('\u203A', currentPage + 1, currentPage === totalPages, false);
    addBtn('\u00BB', totalPages, currentPage === totalPages, false);
}

function goToPage(page) {
    currentPage = page;
    document.getElementById('galleryArea').scrollTop = 0;
    if (searchMode) {
        doSearch();
    } else if (specialView === 'ai-tags') {
        loadAiTagView(false);
    } else if (specialView === 'favorites') {
        loadFavorites(document.querySelector('#navFavorites'), false);
    } else if (specialView === 'today' || specialView === '7days' || specialView === '30days') {
        loadTimeline(specialView, document.querySelector('.sidebar-special.active'), false);
    } else if (specialView && specialView.startsWith('collection:')) {
        var colId = parseInt(specialView.split(':')[1]);
        var col = collectionsCache.find(function(c) { return c.id === colId; });
        if (col) viewCollection(col, false);
    } else {
        loadGallery(currentFolder);
    }
    scheduleGalleryStateSave();
}

// ═══════════════════════════════════════════════════════
// ── Image Selection & Metadata
// ═══════════════════════════════════════════════════════

async function selectImage(path, cardEl, keepSelectionAnchor) {
    selectedFile = path;
    if (!keepSelectionAnchor) selectionAnchorFile = path || null;
    try { localStorage.setItem('gallerySelectedImage', path || ''); } catch (e) {}
    scheduleGalleryStateSave();
    setMetaPanelCollapsed(false);
    document.querySelectorAll('.thumb-card').forEach(function(c){c.classList.remove('selected')});
    if (cardEl) cardEl.classList.add('selected');
    var panel = document.getElementById('metaPanel');
    // Check cache first
    var meta = metaCache[path];
    if (!meta) {
        meta = await API.get('/api/metadata?path=' + encodeURIComponent(path));
        if (meta && (meta.info || meta.parsed)) { metaCache[path] = meta; }
    }
    if (meta && meta.processing && (!meta.parsed || Object.keys(meta.parsed).length === 0)) {
        panel.innerHTML = '<div class="meta-empty">Processing metadata...</div>';
        setTimeout(function() {
            if (selectedFile === path) {
                delete metaCache[path];
                selectImage(path, cardEl, true);
            }
        }, 1500);
        return;
    }
    if (!meta || (!meta.info && !meta.parsed)) { panel.innerHTML = '<div class="meta-empty">No metadata found</div>'; return; }
    renderMetaPanel(meta, path);
}

function renderMetaPanel(meta, path) {
    window._libMeta = meta; window._libPath = path;
    var panel = document.getElementById('metaPanel');
    var parsed = meta.parsed || {}, rawMeta = meta.raw_meta || {}, info = meta.info || {};
    // Find favorite state from currentFiles
    var isFav = false;
    for (var fi = 0; fi < currentFiles.length; fi++) {
        if (currentFiles[fi].path === path) { isFav = currentFiles[fi].favorite; break; }
    }
    var favStar = isFav ? '\u2605' : '\u2606';
    var fileName = info.name || path.split('/').pop().split('\\').pop() || 'Selected image';
    var pinLabel = ICON_PIN;
    var html =
        '<div class="meta-panel-head">' +
            '<div class="meta-panel-title" title="' + escAttr(fileName) + '">' + escHtml(fileName) + '</div>' +
            '<div class="meta-panel-controls">' +
                '<button class="meta-mini-btn meta-delete-btn" id="metaDeleteBtn" title="Move this image to trash (Delete)">&#x1F5D1;</button>' +
                '<button class="meta-mini-btn ' + (metaPanelPinned ? 'active' : '') + '" id="metaPinBtn" title="' + (metaPanelPinned ? 'Unpin panel' : 'Pin panel') + '">' + pinLabel + '</button>' +
                '<button class="meta-mini-btn" id="metaCollapseBtn" title="Hide metadata panel">&#x203A;</button>' +
            '</div>' +
        '</div>' +
        '<div class="meta-preview" id="metaPreview"><img src="/image/' + encodeURIComponent(path) + '" alt=""></div>' +
        '<div class="meta-tabs">' +
        '<button class="meta-tab ' + (currentMetaTab==='metadata'?'active':'') + '" data-tab="metadata">Metadata</button>' +
        '<button class="meta-tab ' + (currentMetaTab==='raw'?'active':'') + '" data-tab="raw">Raw Metadata</button>' +
        (autoTaggerAvailable ? '<button class="meta-tab ' + (currentMetaTab==='ai-tags'?'active':'') + '" data-tab="ai-tags">AI Tags</button>' : '') +
        '<span class="meta-fav-btn" id="metaFavBtn" title="Toggle favorite (F)">' + favStar + '</span>' +
        '</div>' +
        '<div class="meta-content" id="metaContent">';
    if (currentMetaTab === 'metadata') html += renderParsedMeta(parsed, info, rawMeta, meta.civitai);
    else if (currentMetaTab === 'raw') html += renderRawMeta(rawMeta, info);
    else html += '<div class="meta-empty">Loading AI tags...</div>';
    html += '</div>';
    panel.innerHTML = html;
    document.getElementById('metaPinBtn').addEventListener('click', function() {
        setMetaPanelPinned(!metaPanelPinned);
        renderMetaPanel(meta, path);
    });
    document.getElementById('metaDeleteBtn').addEventListener('click', function(e) {
        e.stopPropagation();
        confirmDelete([path]);
    });
    document.getElementById('metaCollapseBtn').addEventListener('click', function() {
        setMetaPanelCollapsed(true);
    });
    panel.querySelectorAll('.meta-tab').forEach(function(tab) {
        tab.addEventListener('click', function() { currentMetaTab = tab.dataset.tab; renderMetaPanel(meta, path); });
    });
    document.getElementById('metaFavBtn').addEventListener('click', function() { toggleFavorite(path); });
    document.getElementById('metaPreview').addEventListener('click', function() {
        var idx = currentFiles.findIndex(function(f){return f.path===path});
        if (idx >= 0) lightboxIndex = idx;
        openLightbox('/image/' + encodeURIComponent(path));
    });
    panel.querySelectorAll('.copy-btn').forEach(function(btn) {
        btn.addEventListener('click', function(e) {
            e.stopPropagation();
            var text = btn.dataset.copy;
            var orig = btn.textContent;
            function onSuccess() { btn.textContent = '\u2713 Copied'; setTimeout(function(){btn.textContent=orig}, 1500); }
            function onFail() { btn.textContent = '\u2717 Failed'; setTimeout(function(){btn.textContent=orig}, 1500); }
            if (navigator.clipboard && navigator.clipboard.writeText) {
                navigator.clipboard.writeText(text).then(onSuccess).catch(function() {
                    // Fallback for non-secure contexts
                    copyFallback(text) ? onSuccess() : onFail();
                });
            } else {
                copyFallback(text) ? onSuccess() : onFail();
            }
        });
    });
    // Collections belong to regular metadata; guard against late async results
    // being inserted after the user has switched tabs or images.
    if (currentMetaTab === 'metadata') API.get('/api/file/collections?path=' + encodeURIComponent(path)).then(function(colls) {
        if (currentMetaTab !== 'metadata' || selectedFile !== path) return;
        if (!colls || !colls.length) return;
        var content = document.getElementById('metaContent');
        if (!content) return;
        var bhtml = '<div class="meta-section" id="metaCollSection"><div class="meta-section-title">Collections</div><div class="meta-collections">';
        for (var ci = 0; ci < colls.length; ci++) {
            bhtml += '<span class="meta-coll-badge" data-coll-id="' + colls[ci].id + '">' +
                '<span class="coll-dot" style="background:' + escAttr(colls[ci].color) + '"></span>' +
                escHtml(colls[ci].name) +
                '<span class="coll-remove" title="Remove from ' + escAttr(colls[ci].name) + '">\u00D7</span></span>';
        }
        bhtml += '</div></div>';
        content.insertAdjacentHTML('afterbegin', bhtml);
        // Bind remove buttons
        content.querySelectorAll('.coll-remove').forEach(function(btn) {
            btn.addEventListener('click', function(e) {
                e.stopPropagation();
                var badge = btn.closest('.meta-coll-badge');
                var collId = parseInt(badge.dataset.collId);
                var collName = badge.textContent.replace('\u00D7','').trim();
                API.post('/api/collection/remove', { id: collId, paths: [path] }).then(function(r) {
                    if (!r) return;
                    badge.remove();
                    // Remove section if no badges left
                    var section = document.getElementById('metaCollSection');
                    if (section && section.querySelectorAll('.meta-coll-badge').length === 0) {
                        section.remove();
                    }
                    showToast('Removed from "' + collName + '"');
                    loadCollections();
                });
            });
        });
    });
    if (autoTaggerAvailable && currentMetaTab === 'ai-tags') loadAutoTagsForMeta(path);
}

async function loadAutoTagsForMeta(path) {
    var data = await API.get('/api/auto_tagger/tags?path=' + encodeURIComponent(path));
    if (!data || selectedFile !== path || currentMetaTab !== 'ai-tags') return;
    var content = document.getElementById('metaContent');
    if (!content) return;
    var previous = document.getElementById('metaAiTagsSection');
    if (previous) previous.remove();
    var tags = data.tags || [];
    var text = tags.filter(function(tag){ return tag.category !== 'rating'; }).map(function(tag){ return tag.name; }).join(', ');
    var html = '<div class="meta-section" id="metaAiTagsSection"><div class="meta-section-title">AI Tags';
    if (text) html += '<span class="copy-btn" data-ai-copy="' + escAttr(text) + '">Copy</span>';
    html += '</div>';
    if (tags.length) {
        html += '<div class="ai-tag-list">';
        tags.forEach(function(tag) {
            html += '<button type="button" class="ai-tag-chip ' + escAttr(tag.category) + '" data-ai-tag="' + escAttr(tag.name) + '" aria-pressed="false" title="Select tag · Confidence ' + Number(tag.score || 0).toFixed(4) + '">' +
                escHtml(String(tag.name).replace(/_/g, ' ')) + '<span class="score">' + Math.round(Number(tag.score || 0) * 100) + '%</span></button>';
        });
        html += '</div><div class="ai-tag-actions"><button data-ai-filter="1" disabled>Select tags to filter</button><button data-ai-retag="1">Re-tag image</button></div>';
        if (data.state && data.state.stale) html += '<div style="font-size:10px;color:var(--orange);margin-top:5px">Image changed; tags need refresh</div>';
        if (data.state && data.state.used_thumbnail) html += '<div style="font-size:10px;color:var(--text-dim);margin-top:5px">Large-image thumbnail used</div>';
    } else if (data.state && data.state.status === 2) {
        html += '<div style="font-size:11px;color:var(--red)">' + escHtml(data.state.error || 'Tagging failed') + '</div><div class="ai-tag-actions"><button data-ai-retag="1">Retry</button></div>';
    } else {
        html += '<div style="font-size:11px;color:var(--text-dim)">Not tagged yet</div><div class="ai-tag-actions"><button data-ai-retag="1">Tag image</button></div>';
    }
    html += '</div>';
    content.innerHTML = html;
    var section = document.getElementById('metaAiTagsSection');
    var copy = section.querySelector('[data-ai-copy]');
    if (copy) copy.addEventListener('click', function(){ copyPlainText(copy.dataset.aiCopy, copy); });
    var filter = section.querySelector('[data-ai-filter]');
    var selectedTags = [];
    section.querySelectorAll('[data-ai-tag]').forEach(function(chip) {
        chip.addEventListener('click', function() {
            var tag = chip.dataset.aiTag;
            var index = selectedTags.indexOf(tag);
            if (index >= 0) selectedTags.splice(index, 1);
            else selectedTags.push(tag);
            var selected = selectedTags.indexOf(tag) >= 0;
            chip.classList.toggle('selected', selected);
            chip.setAttribute('aria-pressed', selected ? 'true' : 'false');
            if (filter) {
                filter.disabled = selectedTags.length === 0;
                filter.textContent = selectedTags.length ? ('Filter selected (' + selectedTags.length + ')') : 'Select tags to filter';
            }
        });
    });
    if (filter) filter.addEventListener('click', function() {
        if (!selectedTags.length) return;
        aiTagFilter = selectedTags.slice();
        aiTagFolder = currentFolder || '';
        var input = document.getElementById('aiFilterInput');
        if (input) input.value = selectedTags.map(function(tag){ return tag.replace(/_/g, ' '); }).join(', ');
        loadAiTagView(true);
    });
    var retag = section.querySelector('[data-ai-retag]');
    if (retag) retag.addEventListener('click', function() {
        startAiTagJob({scope:'paths', paths:[path], retag:true});
    });
}

function copyPlainText(text, btn) {
    var orig = btn ? btn.textContent : '';
    function ok() {
        if (!btn) return;
        btn.textContent = '\u2713 Copied';
        setTimeout(function(){ btn.textContent = orig; }, 1400);
    }
    function fail() {
        if (!btn) return;
        btn.textContent = '\u2717 Failed';
        setTimeout(function(){ btn.textContent = orig; }, 1400);
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(ok).catch(function(){ copyFallback(text) ? ok() : fail(); });
    } else {
        copyFallback(text) ? ok() : fail();
    }
}

function renderFilePathRows(info) {
    var h = '';
    if (info.folder_path) {
        h += '<span class="label">Folder</span><span class="value path-value" title="' + escAttr(info.folder_path) + '">' +
            escHtml(info.folder_path) + '<span class="path-copy copy-btn" data-copy="' + escAttr(info.folder_path) + '">Copy</span></span>';
    }
    if (info.absolute_path) {
        h += '<span class="label">Path</span><span class="value path-value" title="' + escAttr(info.absolute_path) + '">' +
            escHtml(info.absolute_path) + '<span class="path-copy copy-btn" data-copy="' + escAttr(info.absolute_path) + '">Copy</span></span>';
    }
    return h;
}

function renderParsedMeta(parsed, info, rawMeta, civitai) {
    var h = '';
    // Prompt first
    if (parsed.prompt) h += '<div class="meta-section"><div class="meta-section-title">Prompt <span class="copy-btn" data-copy="'+escAttr(parsed.prompt)+'">Copy</span></div><div class="meta-prompt">'+escHtml(parsed.prompt)+'</div></div>';
    if (parsed.negative_prompt) h += '<div class="meta-section"><div class="meta-section-title">Negative Prompt <span class="copy-btn" data-copy="'+escAttr(parsed.negative_prompt)+'">Copy</span></div><div class="meta-prompt negative">'+escHtml(parsed.negative_prompt)+'</div></div>';
    if (parsed.prompt) {
        h += '<div class="meta-section" style="padding:6px 0">' +
             '<div style="display:flex; align-items:center; gap:6px; margin-bottom:6px; font-size:11px; color:var(--text-dim,#888)">' +
                 '<span>Target:</span>' +
                 '<select id="saveTargetSelect" style="flex:1; background:var(--bg-card,#1a1a1a); border:1px solid var(--border,#333); border-radius:4px; color:var(--text,#eee); font-size:11px; padding:3px 4px;">' +
                     '<option value="general">General</option>' +
                     '<option value="sdxl">SDXL</option>' +
                     '<option value="zit">Z-Image</option>' +
                     '<option value="flux">Flux</option>' +
                     '<option value="illustrious">Illustrious</option>' +
                     '<option value="pony">Pony</option>' +
                     '<option value="llm">LLM</option>' +
                     '<option value="suno">Suno</option>' +
                 '</select>' +
             '</div>' +
             '<button onclick="saveToLibrary()" style="width:100%;background:var(--accent,#4a9eff);color:#fff;border:none;border-radius:6px;padding:7px 0;cursor:pointer;font-size:13px">&#x1F4DA; Save to Prompt Library</button></div>';
    }
    // File info
    h += '<div class="meta-section"><div class="meta-section-title">File Info</div><div class="meta-file-info">' +
        '<span class="label">Name</span><span class="value">' + escHtml(info.name||'') + '</span>' +
        '<span class="label">Size</span><span class="value">' + formatSize(info.size) + '</span>' +
        '<span class="label">Dimensions</span><span class="value">' + (info.width||'?') + ' \u00D7 ' + (info.height||'?') + '</span>' +
        '<span class="label">Format</span><span class="value">' + escHtml((info.ext||'').toUpperCase().replace('.','')) + '</span>' +
        renderFilePathRows(info) +
        '</div></div>';
    // Civitai model info
    if (civitai) {
        h += '<div class="meta-section"><div class="meta-section-title">Model (Civitai)</div><div class="meta-civitai">';
        h += '<div class="meta-civitai-name"><a href="https://civitai.com/models/' + civitai.id + '" target="_blank" rel="noopener">' + escHtml(civitai.model) + '</a></div>';
        var detail = escHtml(civitai.version);
        if (civitai.type) detail += ' \u00B7 ' + escHtml(civitai.type);
        if (civitai.base) detail += ' \u00B7 ' + escHtml(civitai.base);
        if (civitai.creator) detail += ' \u00B7 by ' + escHtml(civitai.creator);
        h += '<div class="meta-civitai-detail">' + detail + '</div>';
        if (civitai.tags && civitai.tags.length > 0) {
            h += '<div class="meta-civitai-words">';
            for (var tg = 0; tg < civitai.tags.length; tg++) {
                h += '<span>' + escHtml(civitai.tags[tg]) + '</span>';
            }
            h += '</div>';
        }
        if (civitai.trained_words && civitai.trained_words.length > 0) {
            h += '<div class="meta-civitai-detail" style="margin-top:4px">Trigger words:</div>';
            h += '<div class="meta-civitai-words">';
            for (var tw = 0; tw < civitai.trained_words.length; tw++) {
                h += '<span>' + escHtml(civitai.trained_words[tw]) + '</span>';
            }
            h += '</div>';
        }
        h += '</div></div>';
    }
    // Settings
    if (parsed.settings && Object.keys(parsed.settings).length > 0) {
        h += '<div class="meta-section"><div class="meta-section-title">Settings</div><div class="meta-settings-grid">';
        var settingsKeys = Object.keys(parsed.settings);
        [
            ['Model', 'Model hash'],
            ['VAE', 'VAE hash']
        ].forEach(function(pair) {
            var first = settingsKeys.indexOf(pair[0]);
            var second = settingsKeys.indexOf(pair[1]);
            if (first >= 0 && second >= 0 && second < first) {
                settingsKeys.splice(second, 1);
                first = settingsKeys.indexOf(pair[0]);
                settingsKeys.splice(first + 1, 0, pair[1]);
            }
        });
        settingsKeys.forEach(function(k) {
            h += '<span class="meta-key">'+escHtml(k)+'</span><span class="meta-val">'+escHtml(parsed.settings[k])+'</span>';
        });
        h += '</div></div>';
    }
    var skip = {parameters:1,prompt:1,'Negative prompt':1};
    for (var rk in rawMeta) { if (skip[rk]) continue; var rv = rawMeta[rk];
        if (typeof rv === 'string' && rv.length > 0) { var d = rv.length > 500 ? rv.slice(0,500)+'\u2026' : rv;
            h += '<div class="meta-section"><div class="meta-section-title">'+escHtml(rk)+' <span class="copy-btn" data-copy="'+escAttr(rv)+'">Copy</span></div><div class="meta-prompt">'+escHtml(d)+'</div></div>'; }}
    if (!parsed.prompt && !parsed.settings && Object.keys(rawMeta).length===0)
        h += '<div style="color:var(--text-dim);text-align:center;padding:20px">No generation metadata found</div>';
    return h;
}

function renderRawMeta(rawMeta, info) {
    var h = '<div class="meta-section"><div class="meta-section-title">File Info</div><div class="meta-file-info">' +
        '<span class="label">Name</span><span class="value">'+escHtml(info.name||'')+'</span>' +
        '<span class="label">Size</span><span class="value">'+formatSize(info.size)+'</span>' +
        '<span class="label">Dimensions</span><span class="value">'+(info.width||'?')+' \u00D7 '+(info.height||'?')+'</span>' +
        renderFilePathRows(info) + '</div></div>';
    if (Object.keys(rawMeta).length === 0) { h += '<div style="color:var(--text-dim);text-align:center;padding:20px">No raw metadata</div>'; }
    else {
        var rt = rawMeta.parameters || '';
        if (!rt) { for (var k in rawMeta) { var v = rawMeta[k]; rt += k+': '+(typeof v==='string'&&v.length>1000?v.slice(0,1000)+'\u2026':v)+'\n\n'; }}
        h += '<div class="meta-section"><div class="meta-section-title">Raw Parameters <span class="copy-btn" data-copy="'+escAttr(rt)+'">Copy</span></div><div class="meta-raw">'+escHtml(rt)+'</div></div>';
    }
    return h;
}

// ═══════════════════════════════════════════════════════
// ── Lightbox
// ═══════════════════════════════════════════════════════

function openLightbox(src) { document.getElementById('lightboxImg').src=src; document.getElementById('lightbox').classList.add('open'); updateLightboxCounter(); }
function closeLightbox() { document.getElementById('lightbox').classList.remove('open'); document.getElementById('lightboxImg').src=''; }
function lightboxNav(delta) {
    if (!currentFiles.length) return;
    lightboxIndex = (lightboxIndex + delta + currentFiles.length) % currentFiles.length;
    var f = currentFiles[lightboxIndex];
    document.getElementById('lightboxImg').src = '/image/' + encodeURIComponent(f.path);
    updateLightboxCounter();
    selectImage(f.path, document.querySelector('.thumb-card[data-index="'+lightboxIndex+'"]'));
}
function updateLightboxCounter() { document.getElementById('lbCounter').textContent = currentFiles.length > 0 ? (lightboxIndex+1)+' / '+currentFiles.length : ''; }
document.getElementById('lbClose').addEventListener('click', function(e){e.stopPropagation();closeLightbox()});
document.getElementById('lbPrev').addEventListener('click', function(e){e.stopPropagation();lightboxNav(-1)});
document.getElementById('lbNext').addEventListener('click', function(e){e.stopPropagation();lightboxNav(1)});
document.getElementById('lbDelete').addEventListener('click', function(e){
    e.stopPropagation();
    if (!currentFiles.length) return;
    var f = currentFiles[lightboxIndex];
    if (f && f.path) confirmDelete([f.path]);
});
document.getElementById('lightbox').addEventListener('click', function(e){ if(e.target===e.currentTarget||e.target.id==='lightboxImg') closeLightbox(); });

// ── Sort ──
document.getElementById('sortSelect').addEventListener('change', function() {
    currentPage = 1;
    if (searchMode) { doSearch(); }
    else if (specialView === 'ai-tags') { loadAiTagView(true); }
    else if (specialView === 'favorites') { loadFavorites(document.querySelector('#navFavorites'), true); }
    else if (specialView && specialView.startsWith('collection:')) {
        var colId = parseInt(specialView.split(':')[1]);
        var col = collectionsCache.find(function(c) { return c.id === colId; });
        if (col) viewCollection(col, true);
    }
    else if (specialView) { loadTimeline(specialView, document.querySelector('.sidebar-special.active'), true); }
    else { loadGallery(currentFolder); }
    scheduleGalleryStateSave();
});
document.getElementById('groupSelect').addEventListener('change', function() {
    galleryGroupMode = this.value || 'none';
    try { localStorage.setItem('galleryGroupMode', galleryGroupMode); } catch (e) {}
    currentPage = 1;
    if (searchMode) { doSearch(); }
    else if (specialView === 'ai-tags') { loadAiTagView(true); }
    else if (specialView === 'favorites') { loadFavorites(document.querySelector('#navFavorites'), true); }
    else if (specialView && specialView.startsWith('collection:')) {
        var colId = parseInt(specialView.split(':')[1]);
        var col = collectionsCache.find(function(c) { return c.id === colId; });
        if (col) viewCollection(col, true);
    }
    else if (specialView) { loadTimeline(specialView, document.querySelector('.sidebar-special.active'), true); }
    else { loadGallery(currentFolder); }
    scheduleGalleryStateSave();
});
document.getElementById('metadataFilterSelect').addEventListener('change', function() {
    metadataFilter = ['with', 'without'].indexOf(this.value) >= 0 ? this.value : 'all';
    this.classList.toggle('active', metadataFilter !== 'all');
    try { localStorage.setItem('galleryMetadataFilter', metadataFilter); } catch (e) {}
    currentPage = 1;
    metaCache = {};
    clearMultiSelect();
    if (searchMode) { doSearch(); }
    else if (specialView === 'ai-tags') { loadAiTagView(true); }
    else if (specialView === 'favorites') { loadFavorites(document.querySelector('#navFavorites'), true); }
    else if (specialView && specialView.startsWith('collection:')) {
        var colId = parseInt(specialView.split(':')[1]);
        var col = collectionsCache.find(function(c) { return c.id === colId; });
        if (col) viewCollection(col, true);
    }
    else if (specialView) { loadTimeline(specialView, document.querySelector('.sidebar-special.active'), true); }
    else { loadGallery(currentFolder); }
    scheduleGalleryStateSave();
});
document.getElementById('thumbSlider').addEventListener('input', function(e) { document.documentElement.style.setProperty('--thumb-size', e.target.value+'px'); });
document.getElementById('galleryArea').addEventListener('scroll', scheduleGalleryStateSave);
document.getElementById('metaToggleBtn').addEventListener('click', function() {
    setMetaPanelCollapsed(!metaPanelCollapsed);
});

// ── Refresh ──
document.getElementById('refreshBtn').addEventListener('click', async function() {
    var btn = document.getElementById('refreshBtn');
    btn.style.opacity = '0.5'; btn.style.pointerEvents = 'none';
    showToast('Starting re-index...');
    var result = await API.post('/api/reindex', {});
    if (searchMode) { doSearch(); }
    else if (specialView) { goToPage(currentPage); }
    else { await loadFolderTree(); await loadGallery(currentFolder); }
    await updateStatus(); await updateFavCount();
    btn.style.opacity = '1'; btn.style.pointerEvents = '';
    showToast(result && result.started ? 'Indexing in background' : 'Index already running');
});

// ── Keyboard ──
document.addEventListener('keydown', function(e) {
    if (e.target.tagName === 'INPUT') {
        if (e.key === 'Escape') { document.getElementById('searchInput').blur(); }
        return;
    }
    // Confirm dialog open? handle Escape/Enter and left/right button choice.
    if (document.getElementById('confirmOverlay').classList.contains('open')) {
        var cancelBtn = document.getElementById('confirmCancel');
        var okBtn = document.getElementById('confirmOk');
        if (e.key === 'Escape') { e.preventDefault(); closeConfirm(); }
        else if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
            e.preventDefault();
            if (okBtn.style.display === 'none') cancelBtn.focus();
            else if (document.activeElement === okBtn) cancelBtn.focus();
            else okBtn.focus();
        } else if (e.key === 'Enter') {
            e.preventDefault();
            if (document.activeElement === okBtn && okBtn.style.display !== 'none') okBtn.click();
            else cancelBtn.click();
        }
        return;
    }
    var lbOpen = document.getElementById('lightbox').classList.contains('open');
    if (e.key === 'Escape') {
        if (lbOpen) { closeLightbox(); return; }
        if (multiSelected.size > 0) { clearMultiSelect(); return; }
        return;
    }
    if (lbOpen) {
        if (e.key === 'ArrowLeft') { e.preventDefault(); lightboxNav(-1); }
        if (e.key === 'ArrowRight') { e.preventDefault(); lightboxNav(1); }
        if (e.key === 'Delete') {
            e.preventDefault();
            if (currentFiles.length) {
                var f = currentFiles[lightboxIndex];
                if (f && f.path) confirmDelete([f.path]);
            }
        }
        return;
    }
    // Delete key
    if (e.key === 'Delete') {
        e.preventDefault();
        if (multiSelected.size > 0) { confirmDelete(Array.from(multiSelected)); }
        else if (selectedFile) { confirmDelete([selectedFile]); }
        return;
    }
    // F key: toggle favorite on selected image
    if (e.key === 'f' && !e.ctrlKey && !e.metaKey && !e.altKey) {
        if (selectedFile) { toggleFavorite(selectedFile); }
        return;
    }
    // Ctrl+A: select all on current page
    if (e.key === 'a' && (e.ctrlKey || e.metaKey)) {
        e.preventDefault();
        selectAllOnPage();
        return;
    }
    // Page nav shortcuts
    if (e.altKey && e.key === 'PageDown') { e.preventDefault(); if (currentPage < totalPages) goToPage(currentPage+1); return; }
    if (e.altKey && e.key === 'PageUp') { e.preventDefault(); if (currentPage > 1) goToPage(currentPage-1); return; }
    if (e.altKey && e.key === 'Home') { e.preventDefault(); goToPage(1); return; }
    if (e.altKey && e.key === 'End') { e.preventDefault(); goToPage(totalPages); return; }
    if (e.key === 'F6' || (e.key === 'f' && e.ctrlKey)) { e.preventDefault(); document.getElementById('searchInput').focus(); return; }

    var cards = Array.from(document.querySelectorAll('.thumb-card'));
    if (!cards.length) return;
    var idx = cards.findIndex(function(c){return c.classList.contains('selected')});
    if (idx < 0) idx = 0;
    var cols = getGalleryColumnCount(cards);
    var nextIdx = getArrowTargetIndex(cards, idx, cols, e.key);
    if (nextIdx !== idx || ['ArrowRight','ArrowLeft','ArrowDown','ArrowUp'].indexOf(e.key) >= 0) {
        e.preventDefault();
        if (e.shiftKey) extendSelectionWithKeyboard(cards, idx, nextIdx);
        else cards[nextIdx].click();
        cards[nextIdx].scrollIntoView({block:'nearest'});
    } else if (e.key === 'Enter' && selectedFile) {
        var si = currentFiles.findIndex(function(f){return f.path===selectedFile});
        if (si >= 0) lightboxIndex = si;
        openLightbox('/image/' + encodeURIComponent(selectedFile));
    }
});

function getGalleryColumnCount(cards) {
    if (!cards || cards.length < 2) return 1;
    var firstTop = cards[0].offsetTop;
    var cols = 1;
    for (var i = 1; i < cards.length; i++) {
        if (Math.abs(cards[i].offsetTop - firstTop) > 2) break;
        cols++;
    }
    return Math.max(1, cols);
}

function findThumbCard(path) {
    var cards = Array.from(document.querySelectorAll('.thumb-card'));
    return cards.find(function(c){ return c.dataset.path === path; }) || null;
}

function getCardIndexByPath(cards, path) {
    if (!path) return -1;
    return cards.findIndex(function(c){ return c.dataset.path === path; });
}

function getArrowTargetIndex(cards, idx, cols, key) {
    if (key === 'ArrowRight') return Math.min(idx + 1, cards.length - 1);
    if (key === 'ArrowLeft') return Math.max(idx - 1, 0);
    if (key === 'ArrowDown') return Math.min(idx + cols, cards.length - 1);
    if (key === 'ArrowUp') return Math.max(idx - cols, 0);
    return idx;
}

function extendSelectionWithKeyboard(cards, fromIdx, toIdx) {
    if (!cards.length) return;
    if (!selectionAnchorFile) {
        selectionAnchorFile = selectedFile || (cards[fromIdx] ? cards[fromIdx].dataset.path : '');
    }
    var anchorIdx = getCardIndexByPath(cards, selectionAnchorFile);
    var from = anchorIdx >= 0 ? anchorIdx : Math.max(0, Math.min(fromIdx, cards.length - 1));
    var to = Math.max(0, Math.min(toIdx, cards.length - 1));
    setRangeSelection(cards, from, to);
    selectImage(cards[to].dataset.path, cards[to], true);
}

// ── Multi-select & Delete ──

function ensureSelectedFileInMultiSelect(cards) {
    if (multiSelected.size > 0 || !selectedFile) return;
    var selectedCard = findThumbCard(selectedFile);
    if (selectedCard) {
        multiSelected.add(selectedFile);
        selectedCard.classList.add('multi-selected');
    }
    selectionAnchorFile = selectedFile;
}

function toggleMultiSelect(path, card, keepFocus) {
    var cards = Array.from(document.querySelectorAll('.thumb-card'));
    ensureSelectedFileInMultiSelect(cards);
    if (multiSelected.has(path)) {
        multiSelected.delete(path);
        card.classList.remove('multi-selected');
    } else {
        multiSelected.add(path);
        card.classList.add('multi-selected');
    }
    if (keepFocus) {
        selectImage(path, card, true);
        if (!selectionAnchorFile) selectionAnchorFile = path;
    }
    updateSelectionBar();
}

function rangeSelect(toIdx) {
    var cards = Array.from(document.querySelectorAll('.thumb-card'));
    if (!selectionAnchorFile) selectionAnchorFile = selectedFile;
    var fromIdx = getCardIndexByPath(cards, selectionAnchorFile);
    if (fromIdx < 0) fromIdx = 0;
    setRangeSelection(cards, fromIdx, toIdx);
    if (cards[toIdx]) selectImage(cards[toIdx].dataset.path, cards[toIdx], true);
}

function setRangeSelection(cards, fromIdx, toIdx) {
    var start = Math.min(fromIdx, toIdx);
    var end = Math.max(fromIdx, toIdx);
    multiSelected.clear();
    cards.forEach(function(c){ c.classList.remove('multi-selected'); });
    for (var i = start; i <= end; i++) {
        var p = cards[i].dataset.path;
        multiSelected.add(p);
        cards[i].classList.add('multi-selected');
    }
    updateSelectionBar();
}

function selectAllOnPage() {
    var cards = Array.from(document.querySelectorAll('.thumb-card'));
    cards.forEach(function(c) {
        multiSelected.add(c.dataset.path);
        c.classList.add('multi-selected');
    });
    updateSelectionBar();
}

function clearMultiSelect() {
    multiSelected.clear();
    selectionAnchorFile = selectedFile || null;
    document.querySelectorAll('.thumb-card.multi-selected').forEach(function(c){
        c.classList.remove('multi-selected');
    });
    updateSelectionBar();
}

function updateSelectionBar() {
    var bar = document.getElementById('selectionBar');
    var count = multiSelected.size;
    if (count === 0) { bar.classList.remove('visible'); return; }
    bar.classList.add('visible');
    document.getElementById('selCount').textContent = count + ' selected';
    // Show "Remove from collection" only when viewing a collection
    var remBtn = document.getElementById('selRemColl');
    remBtn.style.display = (specialView && specialView.startsWith('collection:')) ? '' : 'none';
    // Show "Compare" only for 2-4 images
    var cmpBtn = document.getElementById('selCompare');
    cmpBtn.style.display = (count >= 2 && count <= 4) ? '' : 'none';
}

var confirmCallback = null;
function confirmDelete(paths) {
    var overlay = document.getElementById('confirmOverlay');
    var n = paths.length;
    if (!hasTrash) {
        /* Backend will reject the delete — don't lie about what will happen. */
        document.getElementById('confirmTitle').textContent = 'Safe delete unavailable';
        document.getElementById('confirmMsg').textContent = 'Install send2trash first ( pip install send2trash ) and restart the hub. Permanent file deletion is intentionally not supported here.';
        var okBtn = document.getElementById('confirmOk');
        okBtn.style.display = 'none';
        overlay.classList.add('open');
        confirmCallback = null;
        setTimeout(function(){ document.getElementById('confirmCancel').focus(); }, 0);
        return;
    }
    if (skipDeleteConfirmation) {
        executeDelete(paths);
        return;
    }
    document.getElementById('confirmOk').style.display = '';
    document.getElementById('confirmTitle').textContent = 'Delete ' + n + ' image' + (n > 1 ? 's' : '') + '?';
    document.getElementById('confirmMsg').textContent = n === 1
        ? paths[0].split('/').pop() + ' will be moved to your system trash.'
        : n + ' files will be moved to your system trash.';
    overlay.classList.add('open');
    confirmCallback = function() { executeDelete(paths); };
    setTimeout(function(){ document.getElementById('confirmCancel').focus(); }, 0);
}

function closeConfirm() {
    document.getElementById('confirmOverlay').classList.remove('open');
    confirmCallback = null;
}

document.getElementById('confirmCancel').addEventListener('click', closeConfirm);
document.getElementById('confirmOk').addEventListener('click', function() {
    if (confirmCallback) confirmCallback();
    closeConfirm();
});
document.getElementById('confirmOverlay').addEventListener('click', function(e) {
    if (e.target === e.currentTarget) closeConfirm();
});

var deletePollTimer = null;
var deleteObservedActive = false;
var deletePreferredPath = null;

function setDeleteControls(disabled) {
    document.querySelectorAll('#selectionBar button').forEach(function(button) {
        button.disabled = !!disabled;
    });
    var metaButton = document.getElementById('metaDeleteBtn');
    if (metaButton) metaButton.disabled = !!disabled;
    document.getElementById('lbDelete').disabled = !!disabled;
}

function chooseDeleteFallback(paths) {
    var deleted = new Set(paths);
    var index = selectedFile ? currentFiles.findIndex(function(file){ return file.path === selectedFile; }) : -1;
    if (index < 0) index = currentFiles.findIndex(function(file){ return deleted.has(file.path); });
    if (index < 0) index = 0;
    for (var i = index; i < currentFiles.length; i++) {
        if (!deleted.has(currentFiles[i].path)) return currentFiles[i].path;
    }
    for (var j = Math.min(index - 1, currentFiles.length - 1); j >= 0; j--) {
        if (!deleted.has(currentFiles[j].path)) return currentFiles[j].path;
    }
    return null;
}

function renderDeleteProgress(job) {
    var box = document.getElementById('deleteProgress');
    var current = Number(job.current || 0);
    var total = Number(job.total || 0);
    var percent = total ? Math.max(0, Math.min(100, current / total * 100)) : 0;
    box.classList.add('visible');
    document.getElementById('deleteProgressTitle').textContent = job.active ? 'Moving images to system trash' : (job.error ? 'Delete encountered an error' : 'Delete complete');
    document.getElementById('deleteProgressCount').textContent = total ? current + ' / ' + total : '';
    document.getElementById('deleteProgressFill').style.width = percent + '%';
    document.getElementById('deleteProgressMessage').textContent = job.error || job.message || 'Preparing deletion';
}

async function loadCurrentGalleryView() {
    if (searchMode) return doSearch();
    if (specialView === 'ai-tags') return loadAiTagView(false);
    if (specialView === 'favorites') return loadFavorites(document.querySelector('#navFavorites'), false);
    if (specialView === 'today' || specialView === '7days' || specialView === '30days') {
        return loadTimeline(specialView, document.querySelector('.sidebar-special.active'), false);
    }
    if (specialView && specialView.startsWith('collection:')) {
        var colId = parseInt(specialView.split(':')[1]);
        var col = collectionsCache.find(function(item) { return item.id === colId; });
        if (col) return viewCollection(col, false);
    }
    return loadGallery(currentFolder);
}

async function refreshGalleryAfterDelete() {
    closeLightbox();
    selectedFile = null;
    selectionAnchorFile = null;
    clearMultiSelect();
    document.getElementById('metaPanel').innerHTML = '<div class="meta-empty">Click an image to view its generation metadata</div>';
    await loadCurrentGalleryView();
    if (currentPage > totalPages) {
        currentPage = Math.max(1, totalPages);
        await loadCurrentGalleryView();
    }
    var target = deletePreferredPath && currentFiles.find(function(file){ return file.path === deletePreferredPath; });
    if (!target && currentFiles.length) target = currentFiles[0];
    if (target) {
        var card = findThumbCard(target.path);
        selectImage(target.path, card);
        if (card) card.scrollIntoView({block:'nearest'});
    }
    deletePreferredPath = null;
    setDeleteControls(false);
    await Promise.all([loadFolderTree(), loadCollections(), updateStatus()]);
}

function pollDeleteProgress(delay) {
    if (deletePollTimer) clearTimeout(deletePollTimer);
    deletePollTimer = setTimeout(async function() {
        try {
            var data = await fetch('/api/maintenance_status').then(function(response){ return response.json(); });
            var job = (data && data.job) || {};
            if (job.kind !== 'delete') {
                if (!deleteObservedActive) document.getElementById('deleteProgress').classList.remove('visible');
                return;
            }
            if (!job.active && !deleteObservedActive) {
                document.getElementById('deleteProgress').classList.remove('visible');
                return;
            }
            renderDeleteProgress(job);
            if (job.active) {
                deleteObservedActive = true;
                setDeleteControls(true);
                pollDeleteProgress(400);
                return;
            }
            if (deleteObservedActive) {
                deleteObservedActive = false;
                await refreshGalleryAfterDelete();
                var details = job.details || {};
                var summary = details.deleted != null ? details.deleted + ' moved to trash' : (job.message || 'Delete complete');
                if (details.failed) summary += ', ' + details.failed + ' failed';
                showToast(summary);
            }
            setTimeout(function() {
                if (!deleteObservedActive) document.getElementById('deleteProgress').classList.remove('visible');
            }, 3500);
        } catch (e) {
            document.getElementById('deleteProgressMessage').textContent = 'Waiting for delete status...';
            if (deleteObservedActive) pollDeleteProgress(1000);
        }
    }, delay == null ? 0 : delay);
}

async function executeDelete(paths) {
    if (deleteObservedActive) {
        showToast('A delete job is already running');
        return;
    }
    deletePreferredPath = chooseDeleteFallback(paths);
    setDeleteControls(true);
    renderDeleteProgress({active:true, current:0, total:paths.length, message:'Starting background delete'});
    var result = await API.post('/api/delete/start', { scope:'paths', paths:paths });
    if (!result || !result.started) {
        setDeleteControls(false);
        document.getElementById('deleteProgress').classList.remove('visible');
        if (result && result.busy) showToast('Another Gallery maintenance task is already running');
        return;
    }
    deleteObservedActive = true;
    pollDeleteProgress(100);
}

// Selection bar buttons
document.getElementById('selClear').addEventListener('click', clearMultiSelect);
document.getElementById('selDelete').addEventListener('click', function() {
    if (multiSelected.size > 0) confirmDelete(Array.from(multiSelected));
});
document.getElementById('selRemColl').addEventListener('click', async function() {
    if (multiSelected.size === 0 || !specialView || !specialView.startsWith('collection:')) return;
    var colId = parseInt(specialView.split(':')[1]);
    var col = collectionsCache.find(function(c) { return c.id === colId; });
    var paths = Array.from(multiSelected);
    var r = await API.post('/api/collection/remove', { id: colId, paths: paths });
    if (!r) return;
    showToast(paths.length + ' removed from "' + (col ? col.name : 'collection') + '"');
    clearMultiSelect();
    loadCollections();
    // Refresh collection view
    if (col) viewCollection(col);
});

// ═══════════════════════════════════════════════════════
// ── Favorites
// ═══════════════════════════════════════════════════════

async function toggleFavorite(path) {
    var result = await API.post('/api/favorite', { path: path });
    if (!result || result.error) return;
    // Update in currentFiles
    for (var i = 0; i < currentFiles.length; i++) {
        if (currentFiles[i].path === path) {
            currentFiles[i].favorite = result.favorite;
            break;
        }
    }
    // Update star on card
    var star = document.querySelector('.thumb-fav[data-path="' + CSS.escape(path) + '"]');
    if (star) {
        star.textContent = result.favorite ? '\u2605' : '\u2606';
        if (result.favorite) star.classList.add('active'); else star.classList.remove('active');
    }
    // Update meta panel star if this image is selected
    var metaStar = document.getElementById('metaFavBtn');
    if (metaStar && selectedFile === path) {
        metaStar.textContent = result.favorite ? '\u2605' : '\u2606';
    }
    // Update metadata cache
    if (metaCache[path]) { delete metaCache[path]; }
    // Update favorite count in sidebar
    updateFavCount();
    showToast(result.favorite ? 'Added to favorites' : 'Removed from favorites');
}

async function updateFavCount() {
    var s = await API.get('/api/stats');
    if (!s) return;
    document.getElementById('favCount').textContent = s.favorites || 0;
}

// ═══════════════════════════════════════════════════════
// ── Sidebar special views (Favorites, Timeline)
// ═══════════════════════════════════════════════════════

function clearSpecialView() {
    var wasAiTags = specialView === 'ai-tags';
    specialView = null;
    document.querySelectorAll('.sidebar-special').forEach(function(el) { el.classList.remove('active'); });
    if (wasAiTags) {
        aiTagFilter = [];
        aiTagFolder = '';
        var input = document.getElementById('aiFilterInput');
        if (input) input.value = '';
        updateAiFilterButton();
    }
}

function activateSpecialView(view, el, resetPage) {
    if (resetPage === undefined) resetPage = true;
    // Clear search mode if active
    if (searchMode) {
        document.getElementById('searchInput').value = '';
        exitSearchMode();
        loadFolderTree();
    }
    specialView = view;
    if (resetPage) currentPage = 1;
    metaCache = {};
    document.querySelectorAll('.sidebar-special').forEach(function(s) { s.classList.remove('active'); });
    document.querySelectorAll('.folder-item').forEach(function(f) { f.classList.remove('active'); });
    if (el) el.classList.add('active');
}

document.getElementById('navFavorites').addEventListener('click', function() {
    loadFavorites(this, true);
});
document.getElementById('navToday').addEventListener('click', function() { loadTimeline('today', this); });
document.getElementById('navWeek').addEventListener('click', function() { loadTimeline('7days', this); });
document.getElementById('navMonth').addEventListener('click', function() { loadTimeline('30days', this); });

async function loadFavorites(el, resetPage) {
    if (resetPage === undefined) resetPage = true;
    activateSpecialView('favorites', el, resetPage);
    updateBreadcrumb('');
    var sort = document.getElementById('sortSelect').value;
    var parts = sort.split('-');
    var sortField = parts[0] === 'favorite' ? 'date' : parts[0];
    var sortOrder = parts[0] === 'favorite' ? 'desc' : parts[1];
    var data = await API.get('/api/favorites?sort=' + sortField + '&order=' + sortOrder + '&page=' + currentPage + modelInfoParam() + metadataFilterParam());
    if (!data) return;
    currentFiles = data.files || [];
    totalPages = data.pages || 1;
    currentPage = data.page || 1;
    document.getElementById('galleryCount').textContent = (data.total || 0) + ' favorites';
    renderGalleryFiles(currentFiles, data.total || 0, false);
    renderPagination();
    updateToolbarNav();
    scheduleGalleryStateSave();
}

async function loadTimeline(period, el, resetPage) {
    if (resetPage === undefined) resetPage = true;
    activateSpecialView(period, el, resetPage);
    var labels = { today: 'Today', '7days': 'Last 7 days', '30days': 'Last 30 days' };
    updateBreadcrumb('');
    var sort = document.getElementById('sortSelect').value;
    var parts = sort.split('-');
    var data = await API.get('/api/timeline?period=' + period + '&sort=' + parts[0] + '&order=' + parts[1] + '&page=' + currentPage + modelInfoParam() + metadataFilterParam());
    if (!data) return;
    currentFiles = data.files || [];
    totalPages = data.pages || 1;
    currentPage = data.page || 1;
    document.getElementById('galleryCount').textContent = (data.total || 0) + ' images \u2014 ' + labels[period];
    renderGalleryFiles(currentFiles, data.total || 0, false);
    renderPagination();
    updateToolbarNav();
    scheduleGalleryStateSave();
}

// ═══════════════════════════════════════════════════════
// ── Collections
// ═══════════════════════════════════════════════════════

var collectionsCache = [];

async function loadCollections() {
    var data = await API.get('/api/collections');
    if (!data) return;
    collectionsCache = data;
    renderCollectionsSidebar(data);
}

function renderCollectionsSidebar(collections) {
    var list = document.getElementById('collList');
    list.innerHTML = '';
    for (var i = 0; i < collections.length; i++) {
        var c = collections[i];
        var el = document.createElement('div');
        el.className = 'coll-item';
        el.dataset.id = c.id;
        el.innerHTML = '<span class="coll-dot" style="background:' + escAttr(c.color) + '"></span>' +
            '<span class="coll-name">' + escHtml(c.name) + '</span>' +
            '<span class="coll-cnt">' + c.count + '</span>' +
            '<span class="coll-del" title="Delete collection">\u00D7</span>';
        el.querySelector('.coll-name').addEventListener('click', (function(col) { return function() { viewCollection(col); }; })(c));
        el.querySelector('.coll-cnt').addEventListener('click', (function(col) { return function() { viewCollection(col); }; })(c));
        el.querySelector('.coll-dot').addEventListener('click', (function(col) { return function() { viewCollection(col); }; })(c));
        el.querySelector('.coll-del').addEventListener('click', (function(col) { return function(e) {
            e.stopPropagation();
            if (!confirm('Delete collection "' + col.name + '"?\n\nImages will not be deleted, only the collection label.')) return;
            API.post('/api/collection/delete', { id: col.id }).then(function(r) {
                if (!r) return;
                showToast('Collection "' + col.name + '" deleted');
                // If viewing this collection, go back to root
                if (specialView === 'collection:' + col.id) {
                    clearSpecialView();
                    navigateTo('');
                }
                loadCollections();
            });
        }; })(c));
        list.appendChild(el);
    }
}

async function viewCollection(col, resetPage) {
    if (resetPage === undefined) resetPage = true;
    activateSpecialView('collection:' + col.id, null, resetPage);
    document.querySelectorAll('.coll-item').forEach(function(e) { e.classList.remove('active'); });
    var el = document.querySelector('.coll-item[data-id="' + col.id + '"]');
    if (el) el.classList.add('active');
    var sort = document.getElementById('sortSelect').value;
    var parts = sort.split('-');
    var data = await API.get('/api/collection/files?id=' + col.id + '&sort=' + parts[0] + '&order=' + parts[1] + '&page=' + currentPage + modelInfoParam() + metadataFilterParam());
    if (!data) return;
    currentFiles = data.files || [];
    totalPages = data.pages || 1;
    currentPage = data.page || 1;
    document.getElementById('galleryCount').textContent = (data.total || 0) + ' in "' + col.name + '"';
    renderGalleryFiles(currentFiles, data.total || 0, false);
    renderPagination();
    updateToolbarNav();
    scheduleGalleryStateSave();
}

// Create new collection
document.getElementById('collAddBtn').addEventListener('click', function() {
    var name = prompt('New collection name:');
    if (!name || !name.trim()) return;
    API.post('/api/collection/create', { name: name.trim() }).then(function(r) {
        if (!r) return;
        if (r.error) { showToast(r.error); return; }
        showToast('Collection "' + r.name + '" created');
        loadCollections();
    });
});

// Collection picker in selection bar
document.getElementById('selCollBtn').addEventListener('click', function() {
    var menu = document.getElementById('collPickerMenu');
    if (menu.classList.contains('open')) { menu.classList.remove('open'); return; }
    // Populate picker with current collections
    var list = document.getElementById('collPickerList');
    list.innerHTML = '';
    for (var i = 0; i < collectionsCache.length; i++) {
        var c = collectionsCache[i];
        var item = document.createElement('div');
        item.className = 'coll-picker-item';
        item.innerHTML = '<span class="coll-dot" style="background:' + escAttr(c.color) + '"></span>' + escHtml(c.name);
        item.addEventListener('click', (function(col) { return function() {
            addSelectionToCollection(col.id, col.name);
            document.getElementById('collPickerMenu').classList.remove('open');
        }; })(c));
        list.appendChild(item);
    }
    document.getElementById('collPickerInput').value = '';
    menu.classList.add('open');
    document.getElementById('collPickerInput').focus();
});

// Create collection from picker input
document.getElementById('collPickerInput').addEventListener('keydown', function(e) {
    if (e.key === 'Enter') {
        var name = this.value.trim();
        if (!name) return;
        API.post('/api/collection/create', { name: name }).then(function(r) {
            if (!r || r.error) { showToast(r ? r.error : 'Error'); return; }
            showToast('Created "' + r.name + '"');
            loadCollections().then(function() {
                addSelectionToCollection(r.id, r.name);
            });
            document.getElementById('collPickerMenu').classList.remove('open');
        });
    } else if (e.key === 'Escape') {
        document.getElementById('collPickerMenu').classList.remove('open');
    }
});

// Close picker when clicking outside
document.addEventListener('click', function(e) {
    var picker = document.getElementById('collPickerMenu');
    if (picker.classList.contains('open') && !e.target.closest('.coll-picker')) {
        picker.classList.remove('open');
    }
});

async function addSelectionToCollection(collId, collName) {
    var paths = Array.from(multiSelected);
    if (paths.length === 0 && selectedFile) paths = [selectedFile];
    if (paths.length === 0) return;
    var r = await API.post('/api/collection/add', { id: collId, paths: paths });
    if (!r) return;
    showToast(paths.length + ' added to "' + collName + '"');
    loadCollections();
}

// ── Utils ──
function escHtml(s) { var d=document.createElement('div'); d.textContent=s||''; return d.innerHTML; }
function escAttr(s) { return (s||'').replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/'/g,'&#39;').replace(/</g,'&lt;'); }
function formatSize(b) { if(!b) return '?'; if(b<1024) return b+' B'; if(b<1024*1024) return (b/1024).toFixed(1)+' KB'; return (b/1024/1024).toFixed(1)+' MB'; }
function copyFallback(text) {
    try {
        var ta = document.createElement('textarea');
        ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
        document.body.appendChild(ta); ta.select();
        var ok = document.execCommand('copy');
        document.body.removeChild(ta);
        return ok;
    } catch(e) { return false; }
}
function showToast(msg) { var e=document.querySelector('.toast'); if(e)e.remove(); var t=document.createElement('div'); t.className='toast'; t.textContent=msg; document.body.appendChild(t); setTimeout(function(){t.remove()},2500); }
async function updateStatus() {
    var s = await API.get('/api/stats');
    if (!s) return;
    document.getElementById('statusFiles').textContent = s.files + ' images';
    document.getElementById('statusFolders').textContent = s.folders + ' folders';
    document.getElementById('statusMeta').textContent = s.with_metadata + ' with metadata';
    document.getElementById('statusFav').textContent = '\u2B50 ' + (s.favorites || 0) + ' favorites';
    document.getElementById('favCount').textContent = s.favorites || 0;
    var pending = (s.processing_pending || 0) + (s.model_pending || 0);
    var failed = s.processing_failed || 0;
    var box = document.getElementById('processingStatus');
    var text = document.getElementById('processingText');
    var fill = document.getElementById('processingFill');
    var button = document.getElementById('processingToggle');
    window.galleryProcessingPaused = !!s.processing_paused;
    var scan = s.scan || {};
    fill.classList.remove('indeterminate');
    fill.style.transform = '';
    if (scan.active) {
        box.classList.add('visible');
        window.galleryProcessingBatchTotal = 0;
        window.galleryProcessingLastPending = undefined;
        var scanLabel = 'Scanning' + (scan.root ? ' "' + scan.root + '"' : '');
        if (scan.total && (scan.scanned || 0) <= scan.total) {
            text.textContent = scanLabel + ' · ' + (scan.scanned || 0) + ' / ' + scan.total;
            fill.style.width = Math.max(0, Math.min(100, ((scan.scanned || 0) / scan.total) * 100)) + '%';
        } else {
            text.textContent = scanLabel + ' · ' + (scan.scanned || 0) + ' files';
            fill.classList.add('indeterminate');
        }
        button.style.display = 'none';
    } else if (pending || failed || s.processing_active) {
        box.classList.add('visible');
        if (s.processing_paused) text.textContent = 'Background processing paused';
        else if (s.processing_pending) text.textContent = s.processing_pending + ' images remaining';
        else if (s.model_pending) text.textContent = s.model_pending + ' model records remaining';
        else if (failed) text.textContent = failed + ' processing errors';
        else text.textContent = 'Finishing background processing';
        if (pending) {
            if (typeof window.galleryProcessingLastPending !== 'number') {
                window.galleryProcessingBatchTotal = pending;
            } else if (pending > window.galleryProcessingLastPending) {
                window.galleryProcessingBatchTotal += pending - window.galleryProcessingLastPending;
            }
            window.galleryProcessingBatchTotal = Math.max(
                pending, window.galleryProcessingBatchTotal || pending
            );
            var batchTotal = Math.max(1, window.galleryProcessingBatchTotal);
            fill.style.width = Math.max(0, Math.min(100, ((batchTotal - pending) / batchTotal) * 100)) + '%';
            window.galleryProcessingLastPending = pending;
        } else {
            fill.style.width = '100%';
        }
        button.textContent = s.processing_paused ? 'Resume' : 'Pause';
        button.style.display = pending ? '' : 'none';
    } else {
        box.classList.remove('visible');
        window.galleryProcessingBatchTotal = 0;
        window.galleryProcessingLastPending = undefined;
    }
}

async function toggleBackgroundProcessing() {
    var action = window.galleryProcessingPaused ? 'resume' : 'pause';
    await API.post('/api/processing', {action: action});
    await updateStatus();
}

// Mark the current folder as active in the tree. During restore the deep folder
// item may still be loading (lazy expansion), so retry briefly until it exists.
function setActiveFolder(path) {
    var items = document.querySelectorAll('.folder-item');
    var target = null;
    items.forEach(function(f){ if (f.dataset.path === path) target = f; });
    if (!target) return false;
    items.forEach(function(f){ f.classList.remove('active'); });
    target.classList.add('active');
    return true;
}
function highlightFolderRetry(path) {
    if (setActiveFolder(path)) return;
    var tries = 0;
    var iv = setInterval(function(){ if (setActiveFolder(path) || ++tries > 25) clearInterval(iv); }, 80);
}

// ── Init ──
(async function() {
    applyMetaPanelState();
    initAutoTaggerUi();
    var info = await API.get('/api/info');
    if (info) hasTrash = !!info.trash;
    var savedState = loadGalleryState();
    if (['all', 'with', 'without'].indexOf(savedState.metadataFilter) >= 0) {
        metadataFilter = savedState.metadataFilter;
    }
    var metadataSelect = document.getElementById('metadataFilterSelect');
    if (metadataSelect) {
        metadataSelect.value = metadataFilter;
        metadataSelect.classList.toggle('active', metadataFilter !== 'all');
    }
    var savedFolder = savedState.folder;
    if (savedFolder === undefined || savedFolder === null) {
        try { savedFolder = localStorage.getItem('galleryFolder') || ''; } catch (e) { savedFolder = ''; }
    }
    var restoringFolderView = !(savedState.searchMode && savedState.searchQuery) && !(savedState.specialView || '');
    if (restoringFolderView) currentFolder = savedFolder || '';
    await loadFolderTree();
    await loadCollections();
    if (savedState.sort && document.getElementById('sortSelect')) {
        document.getElementById('sortSelect').value = savedState.sort;
    }
    if (savedState.group) {
        galleryGroupMode = savedState.group;
        try { localStorage.setItem('galleryGroupMode', galleryGroupMode); } catch (e) {}
    }
    if (document.getElementById('groupSelect')) {
        document.getElementById('groupSelect').value = galleryGroupMode;
    }
    currentPage = Math.max(1, parseInt(savedState.page || '1', 10) || 1);
    pendingRestoreScroll = savedState.scrollTop || 0;
    var queryAiTags = [];
    if (autoTaggerAvailable) {
        try { queryAiTags = parseAiTags(new URLSearchParams(window.location.search).get('ai_tags') || ''); } catch (e) {}
    }
    if (autoTaggerAvailable && (queryAiTags.length || (savedState.specialView === 'ai-tags' && Array.isArray(savedState.aiTags) && savedState.aiTags.length))) {
        aiTagFilter = queryAiTags.length ? queryAiTags : savedState.aiTags;
        aiTagFolder = queryAiTags.length ? '' : (savedState.aiFolder || '');
        if (queryAiTags.length) currentPage = 1;
        var aiInput = document.getElementById('aiFilterInput');
        if (aiInput) aiInput.value = aiTagFilter.join(', ');
        await loadAiTagView(false);
    } else if (savedState.searchMode && savedState.searchQuery) {
        searchMode = true;
        searchQuery = savedState.searchQuery || '';
        searchFolderFilter = savedState.searchFolderFilter || null;
        document.getElementById('searchInput').value = searchQuery;
        document.getElementById('searchClear').classList.add('visible');
        await doSearch();
    } else if (savedState.specialView === 'favorites') {
        await loadFavorites(document.querySelector('#navFavorites'), false);
    } else if (savedState.specialView === 'today' || savedState.specialView === '7days' || savedState.specialView === '30days') {
        await loadTimeline(savedState.specialView, document.querySelector('#nav' + (savedState.specialView === 'today' ? 'Today' : savedState.specialView === '7days' ? 'Week' : 'Month')), false);
    } else if (savedState.specialView && savedState.specialView.startsWith('collection:')) {
        var savedColId = parseInt(savedState.specialView.split(':')[1], 10);
        var savedCol = collectionsCache.find(function(c) { return c.id === savedColId; });
        if (savedCol) await viewCollection(savedCol, false);
        else await loadGallery('');
    } else {
        currentFolder = savedFolder || '';
        updateBreadcrumb(currentFolder);
        await loadGallery(currentFolder);
        if (currentFolder) highlightFolderRetry(currentFolder);
    }
    // Re-select the image you last clicked, if it's on the current page.
    try {
        var savedImg = savedState.selectedImage || localStorage.getItem('gallerySelectedImage') || '';
        if (savedImg) {
            var sidx = currentFiles.findIndex(function(f){ return f.path === savedImg; });
            if (sidx >= 0) {
                var scard = document.querySelector('.thumb-card[data-index="' + sidx + '"]');
                if (scard) selectImage(savedImg, scard);
            }
        }
    } catch (e) {}
    await updateStatus();
    pollDeleteProgress(0);
    setInterval(updateStatus, 5000);
})();

function openCompare() {
    var selected = Array.from(multiSelected);
    if (selected.length < 2) { alert('Select at least 2 images to compare (Ctrl+Click)'); return; }
    if (selected.length > 4) { alert('Compare supports up to 4 images. You have ' + selected.length + ' selected.'); return; }
    /* Use repeated path= params instead of CSV — a filename can legitimately contain a comma,
       which would otherwise be %2C-decoded and then re-split on the comma boundary. */
    var query = selected.map(function(p){ return 'path=' + encodeURIComponent(p); }).join('&');
    window.location.href = '/compare?' + query;
}

async function saveToLibrary() {
    var m = window._libMeta, p = window._libPath;
    if (!m || !m.parsed || !m.parsed.prompt) return;
    var parsed = m.parsed, info = m.info || {};
    /* The hub's metadata parser nests A1111/Forge settings under parsed.settings with the
       wire-format capitalized keys (Steps, Sampler, "CFG scale", Seed, Model, Size).
       Reading parsed.steps etc. always returned undefined, so source_meta ended up as {}.
       Read the right place; keep parsed.* as fallback for non-A1111 parser output. */
    var st = parsed.settings || {};
    var smeta = {};
    var _model   = st.Model        || parsed.model;     if (_model)   smeta.model = _model;
    var _sampler = st.Sampler      || parsed.sampler;   if (_sampler) smeta.sampler = _sampler;
    var _steps   = st.Steps        || parsed.steps;     if (_steps)   smeta.steps = _steps;
    var _cfg     = st['CFG scale'] || parsed.cfg_scale; if (_cfg)     smeta.cfg = _cfg;
    var _seed    = st.Seed         || parsed.seed;      if (_seed)    smeta.seed = _seed;
    var _size    = st.Size || (info.width && info.height ? info.width + 'x' + info.height : '');
    if (_size) smeta.size = _size;
    var title = prompt('Save to Prompt Library\n\nTitle:', (parsed.prompt || '').substring(0, 60).replace(/,.*/, '').trim() || 'Untitled');
    if (title === null) return;
    var targetSel = document.getElementById('saveTargetSelect');
    var target = (targetSel && targetSel.value) || 'general';
    var data = {
        title: title || 'Untitled',
        type: 'generation',
        target: target,
        content: parsed.prompt || '',
        negative: parsed.negative_prompt || '',
        notes: '',
        source_image: p || '',
        source_meta: JSON.stringify(smeta),
        tags: ''
    };
    // Try to also upload the image as an attachment so the library card is self-contained
    if (p) {
        try {
            var blob = await fetch('/image/' + encodeURIComponent(p)).then(function(r){return r.ok ? r.blob() : null;});
            if (blob) {
                var basename = (p.split('/').pop().split('\\').pop()) || 'image';
                var fd = new FormData();
                fd.append('file', blob, basename);
                var up = await fetch('/api/library/attachment', {method:'POST', body: fd}).then(function(r){return r.json();});
                if (up && up.ok) {
                    data.attachment = up.attachment;
                    data.attachment_type = up.attachment_type;
                    data.attachment_name = up.attachment_name;
                }
            }
        } catch (e) { /* fall through: save card without attachment */ }
    }
    fetch('/api/library/card', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data)})
        .then(function(r) { return r.json(); })
        .then(function(r) {
            if (r.ok) { alert('Saved to Prompt Library: ' + title + (data.attachment ? '\n(image attached)' : '')); }
            else { alert('Error: ' + (r.error || 'Unknown')); }
        })
        .catch(function(e) { alert('Error: ' + e); });
}
</script>
</body>
</html>
'''


# ─── GalleryModule (hub integration) ──────────────────────────────────────────

class GalleryModule(Module):
    """Module wrapper around GalleryDB + the HTML gallery UI."""

    name = "Gallery"
    version = "1.2.13"
    icon = "\U0001F5BC"   # 🖼
    description = "Browse and manage your AI-generated image collection."
    order = 10

    settings_schema = {
        "folders": {
            "type": "folder_list",
            "label": "Image folders",
            "default": [],
            "desc": "Folders to scan for AI-generated images. The first folder is used as the gallery root. Requires restart.",
            "requires_restart": True,
        },
        "per_page": {
            "type": "number", "label": "Images per page", "default": 200,
            "min": 50, "max": 1000,
            "desc": "Pagination size in the grid. Applies on next load.",
        },
        "background_reindex": {
            "type": "bool", "label": "Background re-indexing", "default": False,
            "desc": "Periodically rescan the folder for new files. Off by default; use the Re-index button for a manual rescan. Requires restart to change.",
            "requires_restart": True,
        },
        "reindex_interval": {
            "type": "number", "label": "Re-index interval (seconds)", "default": 30,
            "min": 5, "max": 3600,
            "desc": "How often to rescan. Requires restart.",
            "requires_restart": True,
        },
        "thumb_workers": {
            "type": "number", "label": "Thumbnail workers", "default": 0,
            "min": 0, "max": 32,
            "desc": "Threads used by 'Generate all thumbnails'. 0 = auto (min(8, CPU count)).",
        },
        "accordion_folders": {
            "type": "bool", "label": "Close other folders when opening one", "default": False,
            "desc": "Keeps the folder tree compact by closing sibling branches when you open a folder. Off by default.",
        },
        "folder_sort": {
            "type": "select", "label": "Folder sort", "default": "name",
            "options": ["name", "newest", "active"],
            "desc": "Sort folder siblings in the Gallery sidebar by name, newest image, or the currently active folder branch.",
        },
        "skip_delete_confirmation": {
            "type": "bool", "label": "Skip delete confirmation", "default": False,
            "desc": "Delete immediately after pressing Delete or the trash button. Files still go to the system trash.",
        },
        "background_processing": {
            "type": "bool", "label": "Background image processing", "default": True,
            "desc": "Process metadata, dimensions, model information and thumbnails after the fast folder scan.",
        },
        "filesystem_watcher": {
            "type": "bool", "label": "Watch folders for changes", "default": True,
            "desc": "Update individual files as they are created, changed or removed instead of rescanning the full library. Requires restart.",
            "requires_restart": True,
        },
        "processing_workers": {
            "type": "number", "label": "Background processing workers", "default": 2,
            "min": 1, "max": 8,
            "desc": "Parallel image readers. 2 is a safe default for SSD, HDD and network folders. Requires restart.",
            "requires_restart": True,
        },
        "verbose": {
            "type": "bool", "label": "Verbose metadata debug log", "default": False,
            "desc": "Print parser debug to the server console.",
        },
    }

    def __init__(self, hub):
        super().__init__(hub)
        self.db = None
        self.root_path = ""
        self._bg_thread = None
        self._thumb_thread = None
        self._index_thread = None
        self._search_thread = None
        self._optimize_thread = None
        self._delete_thread = None
        self._watch_observer = None
        self._watch_paths = set()
        self._watch_lock = threading.Lock()
        self._watch_event = threading.Event()
        self._watch_thread = None
        self._maintenance_lock = threading.Lock()
        self._maintenance_job = {
            "kind": "", "label": "", "active": False, "stage": "",
            "message": "", "current": 0, "total": 0, "percent": 0,
            "started": 0, "finished": 0, "error": "", "details": {},
        }

    # ─── Lifecycle ───────────────────────────────────────────────────────────
    def _maintenance_begin(self, kind, label):
        with self._maintenance_lock:
            if self._maintenance_job.get("active"):
                return False
            self._maintenance_job = {
                "kind": kind,
                "label": label,
                "active": True,
                "stage": "starting",
                "message": f"Starting {label.lower()}",
                "current": 0,
                "total": 0,
                "percent": 0,
                "started": time.time(),
                "finished": 0,
                "error": "",
                "details": {},
            }
        return True

    def _maintenance_update(self, kind, stage, message, current=0, total=0, details=None):
        with self._maintenance_lock:
            if self._maintenance_job.get("kind") != kind:
                return
            self._maintenance_job.update({
                "stage": stage,
                "message": message,
                "current": int(current or 0),
                "total": int(total or 0),
                "percent": round((current / total) * 100, 1) if total else 0,
            })
            if details is not None:
                self._maintenance_job["details"] = dict(details)

    def _maintenance_finish(self, kind, message, result=None):
        with self._maintenance_lock:
            if self._maintenance_job.get("kind") != kind:
                return
            total = int(self._maintenance_job.get("total") or 0)
            self._maintenance_job.update({
                "active": False,
                "stage": "complete",
                "message": message,
                "current": total,
                "percent": 100,
                "finished": time.time(),
                "error": "",
            })
            if result is not None:
                self._maintenance_job["details"] = dict(result)

    def _maintenance_fail(self, kind, error):
        with self._maintenance_lock:
            if self._maintenance_job.get("kind") != kind:
                return
            self._maintenance_job.update({
                "active": False,
                "stage": "error",
                "message": "Maintenance task failed",
                "finished": time.time(),
                "error": str(error)[:500],
            })

    def _maintenance_snapshot(self):
        with self._maintenance_lock:
            job = dict(self._maintenance_job)
            job["details"] = dict(job.get("details") or {})
        if job.get("active") and job.get("kind") == "index" and self.db:
            scan = self.db.scan_status()
            job.update({
                "stage": "scan",
                "message": "Scanning " + (scan.get("root") or "gallery folders"),
                "current": int(scan.get("scanned") or 0),
                "total": int(scan.get("total") or 0),
            })
            total = job["total"]
            current = job["current"]
            job["percent"] = round((current / total) * 100, 1) if total and current <= total else 0
        started = float(job.get("started") or 0)
        ended = float(job.get("finished") or 0) if not job.get("active") else time.time()
        job["elapsed"] = max(0, round(ended - started, 1)) if started else 0
        return {
            "job": job,
            "scan": self.db.scan_status() if self.db else {},
            "background": self.db.processing_status() if self.db else {},
        }

    def _queue_watch_path(self, path):
        if not path:
            return
        with self._watch_lock:
            self._watch_paths.add(os.path.abspath(path))
            self._watch_event.set()

    def _start_filesystem_watcher(self):
        if not self.db or self._watch_observer or not self.setting("filesystem_watcher", True):
            return
        if not HAS_WATCHDOG:
            print("[GALLERY] Folder watcher unavailable; install watchdog or use manual re-index")
            return

        module = self

        class GalleryWatchHandler(FileSystemEventHandler):
            def on_any_event(self, event):
                if event.event_type in ("opened", "closed", "closed_no_write"):
                    return
                module._queue_watch_path(getattr(event, "src_path", ""))
                module._queue_watch_path(getattr(event, "dest_path", ""))

        observer = Observer()
        handler = GalleryWatchHandler()
        scheduled = 0
        for root in self.db.roots.values():
            try:
                observer.schedule(handler, root, recursive=True)
                scheduled += 1
            except Exception as exc:
                print(f"[GALLERY] Could not watch {root}: {exc}")
        if not scheduled:
            return
        try:
            observer.start()
        except Exception as exc:
            print(f"[GALLERY] Folder watcher failed to start: {exc}")
            return
        self._watch_observer = observer

        def consume_events():
            while True:
                self._watch_event.wait()
                time.sleep(0.75)
                with self._watch_lock:
                    paths = list(self._watch_paths)
                    self._watch_paths.clear()
                    self._watch_event.clear()
                try:
                    result = self.db.sync_changed_paths(paths)
                    if result.get("queued") and self.setting("background_processing", True):
                        self.db.start_processing(self.setting("processing_workers", 2))
                except Exception as exc:
                    print(f"[GALLERY] Filesystem watcher sync failed: {exc}")
                    traceback.print_exc()

        self._watch_thread = threading.Thread(
            target=consume_events,
            name="gallery-filesystem-watcher",
            daemon=True,
        )
        self._watch_thread.start()
        print(f"[GALLERY] Watching {scheduled} image folder(s) for targeted updates")

    def _start_index(self, force=False, label="Index", maintenance_kind=None):
        if not self.db:
            return False
        if self._index_thread and self._index_thread.is_alive():
            print(f"[GALLERY] {label} skipped; index already running")
            return False

        def run():
            try:
                print(f"[GALLERY] {label} started in background...")
                result = self.db.index_tree(force=force)
                if self.setting("background_processing", True):
                    self.db.start_processing(self.setting("processing_workers", 2))
                stats = self.db.get_stats()
                print(f"[GALLERY] {stats['files']} images, {stats['folders']} folders, "
                      f"{stats['with_metadata']} with metadata")
                if maintenance_kind:
                    queued = int(result.get("queued") or 0)
                    message = "Folder scan complete"
                    if queued:
                        message += f"; {queued} images queued for background processing"
                    self._maintenance_finish(maintenance_kind, message, result)
            except Exception as e:
                print(f"[GALLERY] {label} failed: {e}")
                self.db.finish_scan_with_error(e)
                if maintenance_kind:
                    self._maintenance_fail(maintenance_kind, e)
                traceback.print_exc()

        self._index_thread = threading.Thread(target=run, daemon=True)
        self._index_thread.start()
        return True

    def on_startup(self):
        global DEFAULT_PER_PAGE
        folders = self.setting("folders", [])
        # Backwards compat: if old "folder" setting exists, migrate it
        if not folders:
            old_folder = self.setting("folder", "")
            if old_folder:
                folders = [old_folder]
                self.hub.settings.set_module_setting("gallery", "folders", folders)
        DEFAULT_PER_PAGE = int(self.setting("per_page", 200) or 200)
        _metadata_mod.VERBOSE = bool(self.setting("verbose", False)) or _metadata_mod.VERBOSE

        # Build roots dict from valid folders
        roots = {}
        for f in (folders or []):
            f = f.strip()
            if f and os.path.isdir(f):
                name = os.path.basename(f.rstrip("/\\"))
                # Disambiguate duplicate basenames
                base_name = name
                counter = 2
                while name in roots:
                    name = f"{base_name}_{counter}"
                    counter += 1
                roots[name] = os.path.abspath(f)

        if not roots:
            print("[GALLERY] No image folder configured. Open Settings to add one.")
            return

        # Native installs keep these next to hub.py. Docker points data_path()
        # at its persistent /app/data volume through CYBERHUB_DATA_DIR.
        db_path = self.hub.data_path("cyberdelia.db")
        self.thumb_dir = self.hub.data_path(".thumbs")
        os.makedirs(self.thumb_dir, exist_ok=True)

        for rn, rp in roots.items():
            print(f"[GALLERY] Root: {rn} -> {rp}")
        migrate_thumb_layout(self.thumb_dir)
        self.db = GalleryDB(db_path, self.thumb_dir, roots)
        self._start_filesystem_watcher()
        self._start_index(force=False, label="Startup index")

        if self.setting("background_reindex", False):
            interval = int(self.setting("reindex_interval", DEFAULT_REINDEX_INTERVAL) or DEFAULT_REINDEX_INTERVAL)

            def bg():
                while True:
                    time.sleep(interval)
                    try:
                        self._start_index(force=False, label="Background index")
                    except Exception as e:
                        print(f"[BG-INDEX] {e}")
            self._bg_thread = threading.Thread(target=bg, daemon=True)
            self._bg_thread.start()
            print(f"[GALLERY] Background re-indexing every {interval}s")
        else:
            print("[GALLERY] Background re-indexing disabled")

    def on_settings_changed(self, key, value):
        # Live-apply where safe; others need a restart (flagged in schema).
        if key == "per_page":
            global DEFAULT_PER_PAGE
            try:
                DEFAULT_PER_PAGE = max(50, min(int(value), 1000))
            except (TypeError, ValueError):
                pass
        elif key == "verbose":
            _metadata_mod.VERBOSE = bool(value)
        elif key == "background_processing" and self.db:
            if value:
                self.db.resume_processing()
                self.db.start_processing(self.setting("processing_workers", 2))
            else:
                self.db.pause_processing()

    # ─── Routing ─────────────────────────────────────────────────────────────
    def routes_get(self):
        return {
            "/gallery": self._page,
            "/api/folders": self._api_folders,
            "/api/files": self._api_files,
            "/api/favorites": self._api_favorites,
            "/api/timeline": self._api_timeline,
            "/api/metadata": self._api_metadata,
            "/api/stats": self._api_stats,
            "/api/maintenance_status": self._api_maintenance_status,
            "/api/info": self._api_info,
            "/api/search": self._api_search,
            "/api/search_folder": self._api_search_folder,
            "/api/tags": self._api_tags,
            "/api/collections": self._api_collections,
            "/api/collection/files": self._api_collection_files,
            "/api/file/collections": self._api_file_collections,
            "/api/processing": self._api_processing,
            "/api/delete/preview": self._api_delete_preview,
        }

    def routes_post(self):
        return {
            "/api/reindex": self._api_reindex,
            "/api/rebuild_search": self._api_rebuild_search,
            "/api/optimize_db": self._api_optimize_db,
            "/api/delete": self._api_delete,
            "/api/delete/start": self._api_delete_start,
            "/api/favorite": self._api_favorite,
            "/api/collection/create": self._api_collection_create,
            "/api/collection/rename": self._api_collection_rename,
            "/api/collection/delete": self._api_collection_delete,
            "/api/collection/add": self._api_collection_add,
            "/api/collection/remove": self._api_collection_remove,
            "/api/generate_thumbs": self._api_generate_thumbs,
            "/api/processing": self._api_processing_control,
        }

    def prefix_routes(self):
        return {
            "/thumb/": self._handle_thumb,
            "/image/": self._handle_image,
        }

    # ─── Page ────────────────────────────────────────────────────────────────
    def _page(self, handler, qs):
        from html import escape
        from core.server import _font_links
        title = escape(self.hub.settings.get("title", "CyberHub"))
        module_nav = build_module_menu(self.hub.registry, "gallery")
        page = (
            GALLERY_BODY
            .replace("{HUB_TITLE}", title)
            .replace("{MODULE_NAV}", module_nav)
            .replace("{FONT_LINKS}", _font_links())
            .replace("{BODY_CLASS}", theme_body_class(self.hub.settings))
            .replace("{ACCORDION_FOLDERS}", "true" if self.setting("accordion_folders", False) else "false")
            .replace("{SKIP_DELETE_CONFIRMATION}", "true" if self.setting("skip_delete_confirmation", False) else "false")
            .replace("{FOLDER_SORT}", str(self.setting("folder_sort", "name") or "name"))
            .replace("{AUTO_TAGGER_AVAILABLE}", "true" if self.hub.registry.get("auto_tagger") else "false")
            .replace("{HELP_OVERLAY}", HELP_OVERLAY_HTML)
        )
        handler.respond_html(page)

    # ─── Image / thumb prefix routes ─────────────────────────────────────────
    def _handle_thumb(self, handler, rel_path):
        if not self._require_db(handler): return
        self.db.prioritize_processing(rel_path)
        abs_path = self.db.resolve_path(rel_path)
        thumb = ensure_thumbnail(self.thumb_dir, rel_path, abs_path)
        if thumb:
            handler.serve_file(thumb, immutable=".thumbs" in thumb)
        else:
            handler.send_error(403)

    def _handle_image(self, handler, rel_path):
        if not self.db:
            handler.send_error(503); return
        full = self.db.resolve_path(rel_path)
        if full and os.path.isfile(full):
            handler.serve_file(full)
        else:
            handler.send_error(403)

    # ─── Shared guard ────────────────────────────────────────────────────────
    def _require_db(self, handler):
        if self.db is None:
            handler.respond_json({"error":
                "Gallery not configured. Open Settings and set an image folder."},
                status=503)
            return False
        return True

    # ─── GET API ─────────────────────────────────────────────────────────────
    def _api_folders(self, handler, qs):
        if not self._require_db(handler): return
        handler.respond_json(self.db.get_subfolders(
            qs.get("parent", [""])[0],
            sort=str(self.setting("folder_sort", "name") or "name"),
            active_path=qs.get("active", [""])[0],
        ))

    @staticmethod
    def _api_wants_model_info(qs):
        return qs.get("models", [""])[0] == "1"

    @staticmethod
    def _api_metadata_filter(qs):
        value = qs.get("metadata", ["all"])[0]
        return value if value in ("all", "with", "without") else "all"

    def _api_files(self, handler, qs):
        if not self._require_db(handler): return
        handler.respond_json(self.db.get_files(
            folder=qs.get("folder", [""])[0],
            sort=qs.get("sort", ["name"])[0],
            order=qs.get("order", ["asc"])[0],
            page=handler._int(qs, "page"),
            per_page=handler._int(qs, "per_page", DEFAULT_PER_PAGE),
            favorite_only=qs.get("favorites", [""])[0] == "1",
            time_filter=qs.get("time", [None])[0],
            include_model_info=self._api_wants_model_info(qs),
            metadata_filter=self._api_metadata_filter(qs),
        ))

    def _api_favorites(self, handler, qs):
        if not self._require_db(handler): return
        handler.respond_json(self.db.get_all_favorites(
            sort=qs.get("sort", ["date"])[0],
            order=qs.get("order", ["desc"])[0],
            page=handler._int(qs, "page"),
            per_page=handler._int(qs, "per_page", DEFAULT_PER_PAGE),
            include_model_info=self._api_wants_model_info(qs),
            metadata_filter=self._api_metadata_filter(qs),
        ))

    def _api_timeline(self, handler, qs):
        if not self._require_db(handler): return
        handler.respond_json(self.db.get_timeline_files(
            time_filter=qs.get("period", ["today"])[0],
            sort=qs.get("sort", ["date"])[0],
            order=qs.get("order", ["desc"])[0],
            page=handler._int(qs, "page"),
            per_page=handler._int(qs, "per_page", DEFAULT_PER_PAGE),
            include_model_info=self._api_wants_model_info(qs),
            metadata_filter=self._api_metadata_filter(qs),
        ))

    def _api_metadata(self, handler, qs):
        if not self._require_db(handler): return
        file_path = qs.get("path", [""])[0]
        self.db.prioritize_processing(file_path)
        if self.setting("background_processing", True):
            self.db.start_processing(self.setting("processing_workers", 2))
        data = self.db.get_file_metadata(file_path) or {}
        # Enrich with Civitai info (this is what gallery.py did in the DB; we do
        # it here so the DB layer doesn't depend on the hub object).
        if data:
            data["civitai"] = self.hub.civitai.lookup(data.get("parsed", {}))
        handler.respond_json(data)

    def _api_stats(self, handler, qs):
        if not self._require_db(handler): return
        handler.respond_json(self.db.get_stats())

    def _api_processing(self, handler, qs):
        if not self._require_db(handler): return
        handler.respond_json(self.db.processing_status())

    def _api_maintenance_status(self, handler, qs):
        if not self._require_db(handler): return
        handler.respond_json(self._maintenance_snapshot())

    def _api_info(self, handler, qs):
        handler.respond_json({"trash": HAS_TRASH})

    def _api_delete_preview(self, handler, qs):
        if not self._require_db(handler): return
        handler.respond_json({
            "count": self.db.count_without_metadata(),
            "trash": HAS_TRASH,
            "filter": "completed_without_metadata",
        })

    def _api_search(self, handler, qs):
        if not self._require_db(handler): return
        handler.respond_json(self.db.search(
            query=qs.get("q", [""])[0],
            page=handler._int(qs, "page"),
            per_page=handler._int(qs, "per_page", DEFAULT_PER_PAGE),
            sort=qs.get("sort", ["date"])[0],
            order=qs.get("order", ["desc"])[0],
            include_model_info=self._api_wants_model_info(qs),
            metadata_filter=self._api_metadata_filter(qs),
        ))

    def _api_search_folder(self, handler, qs):
        if not self._require_db(handler): return
        handler.respond_json(self.db.search_in_folder(
            query=qs.get("q", [""])[0],
            folder=qs.get("folder", [""])[0],
            page=handler._int(qs, "page"),
            per_page=handler._int(qs, "per_page", DEFAULT_PER_PAGE),
            sort=qs.get("sort", ["date"])[0],
            order=qs.get("order", ["desc"])[0],
            include_model_info=self._api_wants_model_info(qs),
            metadata_filter=self._api_metadata_filter(qs),
        ))

    def _api_reindex(self, handler, content_len, content_type):
        if not self._require_db(handler): return
        data = handler.read_body_json(content_len)
        if content_len > 0 and data is None:
            handler.respond_json({"error": "Invalid JSON"}, status=400)
            return
        data = data or {}
        force = bool(data.get("force", False))
        if not self._maintenance_begin("index", "Gallery folder scan"):
            handler.respond_json({"ok": True, "force": force, "started": False, "busy": True})
            return
        started = self._start_index(
            force=force,
            label="Manual index",
            maintenance_kind="index",
        )
        if not started:
            self._maintenance_fail("index", "An index scan is already running")
        handler.respond_json({"ok": True, "force": force, "started": started})

    def _api_processing_control(self, handler, content_len, content_type):
        if not self._require_db(handler): return
        data = handler.read_body_json(content_len) or {}
        action = str(data.get("action", "")).lower()
        if action == "pause":
            self.db.pause_processing()
        elif action == "resume":
            self.db.resume_processing()
            self.db.start_processing(self.setting("processing_workers", 2))
        else:
            handler.respond_json({"error": "Action must be pause or resume"}, status=400)
            return
        handler.respond_json({"ok": True, **self.db.processing_status()})

    def _api_rebuild_search(self, handler, content_len, content_type):
        if not self._require_db(handler): return
        if self._search_thread and self._search_thread.is_alive():
            handler.respond_json({"ok": True, "started": False})
            return
        if not self._maintenance_begin("search", "Search index rebuild"):
            handler.respond_json({"ok": True, "started": False, "busy": True})
            return

        def run():
            try:
                result = self.db.rebuild_search_index(
                    progress=lambda stage, message, current, total: self._maintenance_update(
                        "search", stage, message, current, total
                    )
                )
                if not result.get("ok"):
                    raise RuntimeError(result.get("error") or "Search index rebuild failed")
                self._maintenance_finish(
                    "search",
                    f"Search index rebuilt for {result.get('files', 0)} images",
                    result,
                )
            except Exception as e:
                print(f"[GALLERY] Search rebuild failed: {e}")
                self._maintenance_fail("search", e)
                traceback.print_exc()

        self._search_thread = threading.Thread(target=run, daemon=True)
        self._search_thread.start()
        handler.respond_json({"ok": True, "started": True})

    def _api_optimize_db(self, handler, content_len, content_type):
        if not self._require_db(handler): return
        if self._optimize_thread and self._optimize_thread.is_alive():
            handler.respond_json({"ok": True, "started": False})
            return
        if not self._maintenance_begin("optimize", "Database optimization"):
            handler.respond_json({"ok": True, "started": False, "busy": True})
            return

        def run():
            was_paused = self.db.processing_status().get("paused", False)
            try:
                self.db.pause_processing()
                self._maintenance_update(
                    "optimize", "waiting", "Waiting for active image processing", 0, 3
                )
                deadline = time.time() + 30
                while self.db.processing_status().get("active") and time.time() < deadline:
                    time.sleep(0.1)
                result = self.db.optimize(
                    progress=lambda stage, message, current, total: self._maintenance_update(
                        "optimize", stage, message, current, total
                    )
                )
                before, after = result["before"], result["after"]
                result.update({
                    "saved": max(0, before - after),
                    "before_mb": round(before / (1024 * 1024), 2),
                    "after_mb": round(after / (1024 * 1024), 2),
                    "saved_kb": round(max(0, before - after) / 1024, 1),
                })
                message = f"Database optimized; {result['after_mb']} MB on disk"
                if result.get("vacuumed"):
                    message += f", {result['saved_kb']} KB reclaimed"
                else:
                    message += ", no rewrite needed"
                self._maintenance_finish("optimize", message, result)
            except Exception as exc:
                print(f"[GALLERY] Database optimization failed: {exc}")
                self._maintenance_fail("optimize", exc)
                traceback.print_exc()
            finally:
                if not was_paused:
                    self.db.resume_processing()
                    if self.setting("background_processing", True):
                        self.db.start_processing(self.setting("processing_workers", 2))

        self._optimize_thread = threading.Thread(target=run, daemon=True)
        self._optimize_thread.start()
        handler.respond_json({"ok": True, "started": True})

    def _api_tags(self, handler, qs):
        if not self._require_db(handler): return
        handler.respond_json(self.db.get_tags(
            prefix=qs.get("q", [""])[0],
            limit=handler._int(qs, "limit", 50),
        ))

    def _api_collections(self, handler, qs):
        if not self._require_db(handler): return
        handler.respond_json(self.db.get_collections())

    def _api_collection_files(self, handler, qs):
        if not self._require_db(handler): return
        handler.respond_json(self.db.get_collection_files(
            collection_id=handler._int(qs, "id", 0, min_val=0),
            sort=qs.get("sort", ["date"])[0],
            order=qs.get("order", ["desc"])[0],
            page=handler._int(qs, "page"),
            per_page=handler._int(qs, "per_page", DEFAULT_PER_PAGE),
            include_model_info=self._api_wants_model_info(qs),
            metadata_filter=self._api_metadata_filter(qs),
        ))

    def _api_file_collections(self, handler, qs):
        if not self._require_db(handler): return
        handler.respond_json(self.db.get_file_collections(qs.get("path", [""])[0]))

    # ─── POST API ────────────────────────────────────────────────────────────
    def _start_delete_job(self, paths, label):
        paths = list(dict.fromkeys(paths))
        if not paths:
            return False
        if self._delete_thread and self._delete_thread.is_alive():
            return False
        if not self._maintenance_begin("delete", label):
            return False

        total = len(paths)
        self._maintenance_update("delete", "trash", "Preparing files for system trash", 0, total)

        def run():
            try:
                def report(current, progress_total, deleted, failed, stage, path):
                    if stage == "database":
                        message = "Updating Gallery after deletion"
                    else:
                        name = os.path.basename(path) if path else "files"
                        message = f"Moving {name} to system trash"
                    self._maintenance_update(
                        "delete", stage, message, current, progress_total,
                        {"deleted": deleted, "failed": failed},
                    )

                results = self.db.delete_files(paths, progress=report)
                deleted = sum(1 for result in results if result.get("ok"))
                failed = len(results) - deleted
                message = f"Moved {deleted} image{'s' if deleted != 1 else ''} to system trash"
                if failed:
                    message += f"; {failed} failed"
                self._maintenance_finish(
                    "delete", message,
                    {"deleted": deleted, "failed": failed, "requested": total, "trash": True},
                )
            except Exception as exc:
                print(f"[GALLERY] Background delete failed: {exc}")
                self._maintenance_fail("delete", exc)
                traceback.print_exc()

        self._delete_thread = threading.Thread(target=run, daemon=True)
        self._delete_thread.start()
        return True

    def _api_delete_start(self, handler, content_len, content_type):
        if not self._require_db(handler): return
        if not HAS_TRASH:
            handler.respond_json({"error": "Safe delete requires send2trash. Install it: pip install send2trash"}, status=400)
            return
        data = handler.read_body_json(content_len)
        if not data:
            handler.respond_json({"error": "Invalid JSON"}, status=400)
            return

        scope = str(data.get("scope", "paths"))
        if scope == "without_metadata":
            paths = self.db.get_without_metadata_paths()
            expected = data.get("expected_count")
            if expected is not None:
                try:
                    expected = int(expected)
                except (TypeError, ValueError):
                    handler.respond_json({"error": "Invalid expected count"}, status=400)
                    return
                if expected != len(paths):
                    handler.respond_json({
                        "error": "The number of images changed. Check the count and confirm again.",
                        "count": len(paths),
                    }, status=409)
                    return
            label = "Deleting images without metadata"
        elif scope == "paths":
            paths = data.get("paths", [])
            if not isinstance(paths, list):
                handler.respond_json({"error": "Paths must be a list"}, status=400)
                return
            paths = list(dict.fromkeys(p for p in paths if isinstance(p, str) and p))
            if len(paths) > 500:
                handler.respond_json({"error": "Too many files (max 500)"}, status=400)
                return
            label = "Deleting Gallery images"
        else:
            handler.respond_json({"error": "Unknown delete scope"}, status=400)
            return

        if not paths:
            handler.respond_json({"ok": True, "started": False, "empty": True, "total": 0})
            return
        started = self._start_delete_job(paths, label)
        handler.respond_json({
            "ok": True,
            "started": started,
            "busy": not started,
            "total": len(paths),
            "trash": True,
        })

    def _api_delete(self, handler, content_len, content_type):
        if not self._require_db(handler): return
        if not HAS_TRASH:
            handler.respond_json({"error": "Safe delete requires send2trash. Install it: pip install send2trash"}, status=400)
            return
        data = handler.read_body_json(content_len)
        if not data:
            handler.respond_json({"error": "Invalid JSON"}, status=400); return
        paths = data.get("paths", [])
        if not paths or not isinstance(paths, list):
            handler.respond_json({"error": "No paths provided"}, status=400); return
        if len(paths) > 500:
            handler.respond_json({"error": "Too many files (max 500)"}, status=400); return
        results = self.db.delete_files(paths)
        ok_count = sum(1 for r in results if r["ok"])
        handler.respond_json({
            "results": results, "deleted": ok_count,
            "failed": len(results) - ok_count, "trash": HAS_TRASH,
        })

    def _api_favorite(self, handler, content_len, content_type):
        if not self._require_db(handler): return
        data = handler.read_body_json(content_len)
        if not data:
            handler.respond_json({"error": "Invalid JSON"}, status=400); return
        fpath = data.get("path", "")
        if not fpath:
            handler.respond_json({"error": "No path provided"}, status=400); return
        result = self.db.toggle_favorite(fpath)
        if result is None:
            handler.respond_json({"error": "File not found"}, status=404); return
        handler.respond_json(result)

    def _api_collection_create(self, handler, content_len, content_type):
        if not self._require_db(handler): return
        data = handler.read_body_json(content_len)
        if not data:
            handler.respond_json({"error": "Invalid JSON"}, status=400); return
        name = data.get("name", "").strip()
        if not name:
            handler.respond_json({"error": "No name provided"}, status=400); return
        handler.respond_json(self.db.create_collection(name, data.get("color", "#4a9eff")))

    def _api_collection_rename(self, handler, content_len, content_type):
        if not self._require_db(handler): return
        data = handler.read_body_json(content_len)
        if not data:
            handler.respond_json({"error": "Invalid JSON"}, status=400); return
        handler.respond_json(self.db.rename_collection(int(data.get("id", 0)), data.get("name", "")))

    def _api_collection_delete(self, handler, content_len, content_type):
        if not self._require_db(handler): return
        data = handler.read_body_json(content_len)
        if not data:
            handler.respond_json({"error": "Invalid JSON"}, status=400); return
        handler.respond_json(self.db.delete_collection(int(data.get("id", 0))))

    def _api_collection_add(self, handler, content_len, content_type):
        if not self._require_db(handler): return
        data = handler.read_body_json(content_len)
        if not data:
            handler.respond_json({"error": "Invalid JSON"}, status=400); return
        handler.respond_json(self.db.add_to_collection(int(data.get("id", 0)), data.get("paths", [])))

    def _api_collection_remove(self, handler, content_len, content_type):
        if not self._require_db(handler): return
        data = handler.read_body_json(content_len)
        if not data:
            handler.respond_json({"error": "Invalid JSON"}, status=400); return
        handler.respond_json(self.db.remove_from_collection(int(data.get("id", 0)), data.get("paths", [])))

    def _api_generate_thumbs(self, handler, content_len, content_type):
        """Kick off thumbnail generation in a background thread; reply immediately."""
        if not self._require_db(handler): return
        if self._thumb_thread and self._thumb_thread.is_alive():
            handler.respond_json({"ok": True, "already_running": True}); return
        if not self._maintenance_begin("thumbnails", "Thumbnail generation"):
            handler.respond_json({"ok": True, "started": False, "busy": True})
            return
        workers = self.setting("thumb_workers", 0) or 0
        try:
            workers = int(workers)
        except (TypeError, ValueError):
            workers = 0
        if workers <= 0:
            workers = None  # auto

        def _worker():
            try:
                result = self.db.generate_missing_thumbnails(
                    workers=workers,
                    progress=lambda stage, message, current, total, details: self._maintenance_update(
                        "thumbnails", stage, message, current, total, details
                    ),
                )
                if not result.get("ok"):
                    raise RuntimeError(result.get("error") or "Thumbnail generation failed")
                self._maintenance_finish(
                    "thumbnails",
                    f"Thumbnail check complete; {result.get('generated', 0)} generated, "
                    f"{result.get('skipped', 0)} already current",
                    result,
                )
            except Exception as e:
                print(f"[THUMBS] Background job failed: {e}")
                self._maintenance_fail("thumbnails", e)
                traceback.print_exc()

        self._thumb_thread = threading.Thread(target=_worker, daemon=True)
        self._thumb_thread.start()
        handler.respond_json({"ok": True, "started": True, "workers": workers or "auto"})
