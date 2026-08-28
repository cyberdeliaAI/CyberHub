"""Prompt Library — Save, organize and reuse prompts, presets, snippets and notes."""

import hashlib, io, json, os, re, sqlite3, threading, time, zipfile
from core import Module
from core.server import build_shell

CARD_TYPES = ["generation", "captioner", "snippet", "rewrite", "negative", "system", "notes"]
CARD_TARGETS = ["general", "sdxl", "zit", "flux", "illustrious", "pony", "llm", "suno"]
ALLOWED_IMAGE_EXT = {"jpg", "jpeg", "png", "webp", "gif", "bmp"}
ALLOWED_DOC_EXT = {"pdf", "md", "markdown"}
ALLOWED_ATTACHMENT_EXT = ALLOWED_IMAGE_EXT | ALLOWED_DOC_EXT
MAX_UPLOAD_BYTES = 15 * 1024 * 1024   # 15 MB per attachment
MAX_ZIP_BYTES = 200 * 1024 * 1024     # 200 MB compressed ZIP upload
MAX_ZIP_UNCOMPRESSED = 500 * 1024 * 1024  # 500 MB total uncompressed (zip-bomb guard)
THUMB_MAX_EDGE = 400

# Attachment filenames are content-addressed sha256[:16] + safe extension.
# Reject anything else so imported card data cannot smuggle paths into href/src.
_ATTACH_RE = re.compile(r"^[a-f0-9]{16}\.(jpg|jpeg|png|webp|gif|bmp|pdf|md|markdown)$")
_THUMB_RE = re.compile(r"^[a-f0-9]{16}_thumb\.jpg$")


def _attachment_type_for_ext(ext):
    if ext in ALLOWED_IMAGE_EXT:
        return "image"
    if ext == "pdf":
        return "pdf"
    if ext in {"md", "markdown"}:
        return "markdown"
    return "file"


def _sanitize_card_fields(data, partial=False):
    """In-place: clamp `type` and `target` to known whitelist; strip unsafe `attachment` value.
    When ``partial`` is true, omitted fields are left untouched. Returns the same dict."""
    if "type" in data and data.get("type") not in CARD_TYPES:
        data["type"] = "generation"
    elif not partial and "type" not in data:
        data["type"] = "generation"
    if "target" in data and data.get("target") not in CARD_TARGETS:
        data["target"] = "general"
    elif not partial and "target" not in data:
        data["target"] = "general"
    att = data.get("attachment", "")
    if att and not _ATTACH_RE.match(att):
        # Hostile or malformed → drop attachment refs entirely
        data["attachment"] = ""
        data["attachment_type"] = ""
        data["attachment_name"] = ""
    elif att:
        ext = os.path.splitext(att)[1].lstrip(".").lower()
        data["attachment_type"] = _attachment_type_for_ext(ext)
    return data


class PromptLibraryDB:
    def __init__(self, db_path):
        self.lock = threading.Lock()
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        with self.lock:
            self.conn.executescript("""
                CREATE TABLE IF NOT EXISTS prompt_cards (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL, type TEXT NOT NULL DEFAULT 'generation',
                    target TEXT NOT NULL DEFAULT 'general', content TEXT NOT NULL DEFAULT '',
                    negative TEXT DEFAULT '', notes TEXT DEFAULT '',
                    favorite INTEGER DEFAULT 0, source_image TEXT DEFAULT '',
                    source_meta TEXT DEFAULT '', created_at REAL NOT NULL, updated_at REAL NOT NULL,
                    attachment TEXT DEFAULT '', attachment_type TEXT DEFAULT '',
                    attachment_name TEXT DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS prompt_tags (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL);
                CREATE TABLE IF NOT EXISTS prompt_card_tags (card_id INTEGER NOT NULL, tag_id INTEGER NOT NULL, PRIMARY KEY (card_id, tag_id));
            """)
            # Migration: add columns to existing DBs that lack them
            existing_cols = {r[1] for r in self.conn.execute("PRAGMA table_info(prompt_cards)").fetchall()}
            for col, ddl in [
                ("attachment",      "ALTER TABLE prompt_cards ADD COLUMN attachment TEXT DEFAULT ''"),
                ("attachment_type", "ALTER TABLE prompt_cards ADD COLUMN attachment_type TEXT DEFAULT ''"),
                ("attachment_name", "ALTER TABLE prompt_cards ADD COLUMN attachment_name TEXT DEFAULT ''"),
            ]:
                if col not in existing_cols:
                    self.conn.execute(ddl)
            self.conn.commit()

    def _tag_id(self, name):
        name = name.strip().lower()
        if not name: return None
        row = self.conn.execute("SELECT id FROM prompt_tags WHERE name=?", (name,)).fetchone()
        if row: return row[0]
        return self.conn.execute("INSERT INTO prompt_tags (name) VALUES (?)", (name,)).lastrowid

    def _set_tags(self, card_id, tag_names):
        self.conn.execute("DELETE FROM prompt_card_tags WHERE card_id=?", (card_id,))
        for t in tag_names:
            tid = self._tag_id(t)
            if tid: self.conn.execute("INSERT OR IGNORE INTO prompt_card_tags VALUES (?,?)", (card_id, tid))
        self.conn.execute("DELETE FROM prompt_tags WHERE id NOT IN (SELECT DISTINCT tag_id FROM prompt_card_tags)")

    def _card_tags(self, card_id):
        return [r[0] for r in self.conn.execute(
            "SELECT t.name FROM prompt_tags t JOIN prompt_card_tags ct ON t.id=ct.tag_id WHERE ct.card_id=? ORDER BY t.name",
            (card_id,)).fetchall()]

    def _row_to_dict(self, row):
        return {"id": row[0], "title": row[1], "type": row[2], "target": row[3],
                "content": row[4], "negative": row[5] or "", "notes": row[6] or "",
                "favorite": row[7], "source_image": row[8] or "", "source_meta": row[9] or "",
                "created_at": row[10], "updated_at": row[11],
                "attachment": (row[12] if len(row) > 12 else "") or "",
                "attachment_type": (row[13] if len(row) > 13 else "") or "",
                "attachment_name": (row[14] if len(row) > 14 else "") or "",
                "tags": self._card_tags(row[0])}

    def create_card(self, data):
        _sanitize_card_fields(data)
        now = time.time()
        sm = data.get("source_meta", "")
        if not isinstance(sm, str): sm = json.dumps(sm)
        with self.lock:
            cur = self.conn.execute(
                "INSERT INTO prompt_cards (title,type,target,content,negative,notes,favorite,source_image,source_meta,created_at,updated_at,attachment,attachment_type,attachment_name) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (data.get("title","Untitled"), data["type"], data["target"],
                 data.get("content",""), data.get("negative",""), data.get("notes",""),
                 int(data.get("favorite",0)), data.get("source_image",""), sm, now, now,
                 data.get("attachment",""), data.get("attachment_type",""), data.get("attachment_name","")))
            card_id = cur.lastrowid
            tags = data.get("tags", [])
            if isinstance(tags, str): tags = [t.strip() for t in tags.split(",") if t.strip()]
            self._set_tags(card_id, tags)
            self.conn.commit()
        return card_id

    def update_card(self, card_id, data):
        _sanitize_card_fields(data, partial=True)
        # If the attachment is being replaced or removed, remember the old one so we can clean up
        old_attachment = None
        if "attachment" in data:
            with self.lock:
                row = self.conn.execute("SELECT attachment FROM prompt_cards WHERE id=?", (card_id,)).fetchone()
            if row and row[0] and row[0] != data["attachment"]:
                old_attachment = row[0]

        now = time.time(); fields, vals = [], []
        for col in ("title","type","target","content","negative","notes","favorite","source_image","source_meta",
                    "attachment","attachment_type","attachment_name"):
            if col in data:
                v = data[col]
                if col == "source_meta" and not isinstance(v, str): v = json.dumps(v)
                fields.append(f"{col}=?"); vals.append(v)
        if not fields: return False
        fields.append("updated_at=?"); vals.append(now); vals.append(card_id)
        with self.lock:
            self.conn.execute(f"UPDATE prompt_cards SET {','.join(fields)} WHERE id=?", vals)
            if "tags" in data:
                tags = data["tags"]
                if isinstance(tags, str): tags = [t.strip() for t in tags.split(",") if t.strip()]
                self._set_tags(card_id, tags)
            self.conn.commit()
        return {"ok": True, "old_attachment": old_attachment}

    def delete_card(self, card_id):
        """Returns the deleted card's attachment filename (or None) for orphan cleanup by caller."""
        with self.lock:
            row = self.conn.execute("SELECT attachment FROM prompt_cards WHERE id=?", (card_id,)).fetchone()
            old_attachment = row[0] if row and row[0] else None
            self.conn.execute("DELETE FROM prompt_card_tags WHERE card_id=?", (card_id,))
            self.conn.execute("DELETE FROM prompt_cards WHERE id=?", (card_id,))
            self.conn.execute("DELETE FROM prompt_tags WHERE id NOT IN (SELECT DISTINCT tag_id FROM prompt_card_tags)")
            self.conn.commit()
        return old_attachment

    def attachment_reference_count(self, filename):
        """How many cards reference this attachment filename."""
        with self.lock:
            return self.conn.execute("SELECT COUNT(*) FROM prompt_cards WHERE attachment=?", (filename,)).fetchone()[0]

    def referenced_attachments(self):
        """Set of all attachment filenames still referenced by any card."""
        with self.lock:
            return {r[0] for r in self.conn.execute("SELECT DISTINCT attachment FROM prompt_cards WHERE attachment<>''").fetchall()}

    def get_card(self, card_id):
        with self.lock:
            row = self.conn.execute("SELECT * FROM prompt_cards WHERE id=?", (card_id,)).fetchone()
        return self._row_to_dict(row) if row else None

    def toggle_favorite(self, card_id):
        with self.lock:
            row = self.conn.execute("SELECT favorite FROM prompt_cards WHERE id=?", (card_id,)).fetchone()
            if not row: return None
            nv = 0 if row[0] else 1
            self.conn.execute("UPDATE prompt_cards SET favorite=?,updated_at=? WHERE id=?", (nv, time.time(), card_id))
            self.conn.commit()
        return nv

    def list_cards(self, type_filter=None, target_filter=None, tag_filter=None,
                   favorites_only=False, search=None, sort="updated", limit=200, offset=0):
        clauses, params = [], []
        if type_filter and type_filter != "all": clauses.append("c.type=?"); params.append(type_filter)
        if target_filter and target_filter != "all": clauses.append("c.target=?"); params.append(target_filter)
        if favorites_only: clauses.append("c.favorite=1")
        if tag_filter:
            clauses.append("c.id IN (SELECT card_id FROM prompt_card_tags ct2 JOIN prompt_tags pt2 ON ct2.tag_id=pt2.id WHERE pt2.name=?)")
            params.append(tag_filter.lower())
        if search:
            clauses.append("(c.title LIKE ? OR c.content LIKE ? OR c.notes LIKE ? OR c.negative LIKE ?)")
            s = f"%{search}%"; params.extend([s, s, s, s])
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        order = "c.updated_at DESC" if sort == "updated" else "c.created_at DESC" if sort == "created" else "c.title ASC"
        with self.lock:
            total = self.conn.execute(f"SELECT COUNT(*) FROM prompt_cards c {where}", params).fetchone()[0]
            paged_params = list(params) + [limit, offset]
            rows = self.conn.execute(f"SELECT c.* FROM prompt_cards c {where} ORDER BY c.favorite DESC, {order} LIMIT ? OFFSET ?", paged_params).fetchall()
        return {"total": total, "offset": offset, "limit": limit, "cards": [self._row_to_dict(r) for r in rows]}

    def get_all_tags(self):
        with self.lock:
            return [{"name": r[0], "count": r[1]} for r in self.conn.execute(
                "SELECT t.name, COUNT(ct.card_id) FROM prompt_tags t JOIN prompt_card_tags ct ON t.id=ct.tag_id GROUP BY t.name ORDER BY COUNT(ct.card_id) DESC").fetchall()]

    def get_stats(self):
        with self.lock:
            total = self.conn.execute("SELECT COUNT(*) FROM prompt_cards").fetchone()[0]
            by_type = dict(self.conn.execute("SELECT type, COUNT(*) FROM prompt_cards GROUP BY type").fetchall())
            by_target = dict(self.conn.execute("SELECT target, COUNT(*) FROM prompt_cards GROUP BY target").fetchall())
        return {"total": total, "by_type": by_type, "by_target": by_target}

    def export_all(self):
        """Plain JSON export — attachments are referenced by filename but not bundled."""
        return {"version": 1, "cards": self.list_cards(limit=99999)["cards"]}

    def import_cards(self, data):
        n = 0
        for c in data.get("cards", []):
            c.pop("id", None)
            # _sanitize_card_fields runs inside create_card too, but be explicit at the boundary
            _sanitize_card_fields(c)
            self.create_card(c); n += 1
        return n


class PromptLibraryModule(Module):
    name = "Library"
    icon = "\U0001F4DA"
    description = "Save, organize and reuse prompts, presets, snippets and notes."
    order = 25
    settings_schema = {}

    def key(self):
        return "library"

    def __init__(self, hub):
        super().__init__(hub)
        self.db = None
        self.attachments_dir = None

    def on_startup(self):
        hub_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        self.db = PromptLibraryDB(self.hub.data_path("cyberdelia.db"))
        if os.path.realpath(self.hub.data_dir) == os.path.realpath(hub_dir):
            # Preserve the established portable layout for native installs.
            self.attachments_dir = os.path.join(
                hub_dir, "resources", "library", "attachments"
            )
        else:
            self.attachments_dir = self.hub.data_path("library", "attachments")
        os.makedirs(self.attachments_dir, exist_ok=True)
        print(f"[LIBRARY] {self.db.get_stats()['total']} prompt cards")

    def routes_get(self):
        return {"/library": self._page, "/api/library/cards": self._api_list,
                "/api/library/tags": self._api_tags, "/api/library/stats": self._api_stats,
                "/api/library/export": self._api_export,
                "/api/library/export-zip": self._api_export_zip}

    def routes_post(self):
        return {"/api/library/card": self._api_create, "/api/library/card/update": self._api_update,
                "/api/library/card/delete": self._api_delete, "/api/library/card/favorite": self._api_favorite,
                "/api/library/import": self._api_import,
                "/api/library/import-zip": self._api_import_zip,
                "/api/library/attachment": self._api_upload_attachment,
                "/api/library/cleanup-attachments": self._api_cleanup_attachments}

    def prefix_routes(self):
        return {"/library/attachments/": self._serve_attachment}

    def _page(self, handler, qs):
        handler.respond_html(build_shell(self.hub.registry, self.hub.settings,
            active_key="library", page_title="Library", body_html=PAGE_BODY))

    def _api_list(self, handler, qs):
        if not self.db: handler.respond_json({"error": "Not ready"}, status=503); return
        try: limit = max(1, min(1000, int(qs.get("limit", ["200"])[0])))
        except ValueError: limit = 200
        try: offset = max(0, int(qs.get("offset", ["0"])[0]))
        except ValueError: offset = 0
        handler.respond_json(self.db.list_cards(
            type_filter=qs.get("type",[None])[0], target_filter=qs.get("target",[None])[0],
            tag_filter=qs.get("tag",[None])[0], favorites_only=qs.get("fav",["0"])[0]=="1",
            search=qs.get("q",[None])[0], sort=qs.get("sort",["updated"])[0],
            limit=limit, offset=offset))

    def _api_tags(self, handler, qs): handler.respond_json(self.db.get_all_tags() if self.db else [])
    def _api_stats(self, handler, qs): handler.respond_json(self.db.get_stats() if self.db else {})
    def _api_export(self, handler, qs):
        if not self.db: handler.respond_json({"error": "Not ready"}, status=503); return
        handler.respond_json(self.db.export_all())

    def _api_export_zip(self, handler, qs):
        """ZIP bundle: library.json + attachments/*. Migrating to another machine preserves thumbnails."""
        if not self.db: handler.respond_json({"error": "Not ready"}, status=503); return
        export = self.db.export_all()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("library.json", json.dumps(export, indent=2))
            if self.attachments_dir and os.path.isdir(self.attachments_dir):
                # Only bundle attachments actually referenced by exported cards (no orphans)
                referenced = set()
                for c in export["cards"]:
                    att = c.get("attachment")
                    if att and _ATTACH_RE.match(att):
                        referenced.add(att)
                for fn in referenced:
                    orig = os.path.join(self.attachments_dir, fn)
                    if os.path.isfile(orig):
                        zf.write(orig, f"attachments/{fn}")
                    if _attachment_type_for_ext(os.path.splitext(fn)[1].lstrip(".").lower()) == "image":
                        base = os.path.splitext(fn)[0]
                        thumb = os.path.join(self.attachments_dir, f"{base}_thumb.jpg")
                        if os.path.isfile(thumb):
                            zf.write(thumb, f"attachments/{base}_thumb.jpg")
        buf.seek(0)
        handler.respond_binary(buf.read(), "application/zip", download_name="prompt-library.zip")

    def _api_create(self, handler, content_len, content_type):
        data = handler.read_body_json(content_len)
        if data is None: handler.respond_json({"error": "Invalid JSON"}, status=400); return
        handler.respond_json({"ok": True, "id": self.db.create_card(data)})

    def _api_update(self, handler, content_len, content_type):
        data = handler.read_body_json(content_len)
        if not data or "id" not in data: handler.respond_json({"error": "Missing id"}, status=400); return
        result = self.db.update_card(data["id"], data)
        # If the attachment changed, the old file may now be unreferenced — clean it up
        if isinstance(result, dict) and result.get("old_attachment"):
            self._cleanup_attachment_if_unused(result["old_attachment"])
        handler.respond_json({"ok": True})

    def _api_delete(self, handler, content_len, content_type):
        data = handler.read_body_json(content_len)
        if not data or "id" not in data: handler.respond_json({"error": "Missing id"}, status=400); return
        old = self.db.delete_card(data["id"])
        if old: self._cleanup_attachment_if_unused(old)
        handler.respond_json({"ok": True})

    def _api_favorite(self, handler, content_len, content_type):
        data = handler.read_body_json(content_len)
        if not data or "id" not in data: handler.respond_json({"error": "Missing id"}, status=400); return
        handler.respond_json({"ok": True, "favorite": self.db.toggle_favorite(data["id"])})

    def _api_import(self, handler, content_len, content_type):
        data = handler.read_body_json(content_len)
        if data is None: handler.respond_json({"error": "Invalid JSON"}, status=400); return
        handler.respond_json({"ok": True, "imported": self.db.import_cards(data)})

    def _api_import_zip(self, handler, content_len, content_type):
        """Accepts a ZIP from /api/library/export-zip. Two-phase import:
          1. Validate: parse library.json, check ZIP contents, decode each attachment with PIL.
          2. Commit: copy validated attachments into resources/library/attachments/, then import cards.
        If validation fails, nothing is written — no half-extracted attachments left behind.
        Also guards against zip bombs by checking total uncompressed size."""
        if not self.db or not self.attachments_dir:
            handler.respond_json({"error": "Not ready"}, status=503); return
        if content_len > MAX_ZIP_BYTES:
            handler.respond_json({"error": f"ZIP too large (max {MAX_ZIP_BYTES // 1024 // 1024} MB compressed)"}, status=413); return
        if not content_type or "multipart/form-data" not in content_type:
            handler.respond_json({"error": "Expected multipart/form-data"}, status=400); return
        try: files = handler.parse_multipart(content_len, content_type)
        except ValueError as e: handler.respond_json({"error": str(e)}, status=413); return
        f = files.get("file")
        if not f or not f.get("data"):
            handler.respond_json({"error": "No file part"}, status=400); return
        try:
            zf = zipfile.ZipFile(io.BytesIO(f["data"]))
        except zipfile.BadZipFile:
            handler.respond_json({"error": "Not a valid ZIP file"}, status=400); return

        # ── Phase 1: Validate everything before touching disk ──────────────
        total_uncompressed = sum(info.file_size for info in zf.infolist())
        if total_uncompressed > MAX_ZIP_UNCOMPRESSED:
            handler.respond_json({"error": f"ZIP contents would exceed {MAX_ZIP_UNCOMPRESSED // 1024 // 1024} MB uncompressed"}, status=413); return

        try:
            payload = json.loads(zf.read("library.json").decode("utf-8"))
        except KeyError:
            handler.respond_json({"error": "library.json missing from ZIP"}, status=400); return
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            handler.respond_json({"error": f"library.json invalid: {e}"}, status=400); return
        if not isinstance(payload, dict) or not isinstance(payload.get("cards"), list):
            handler.respond_json({"error": "library.json has wrong shape (expected {cards: [...]})"}, status=400); return

        referenced = set()
        for c in payload["cards"]:
            att = c.get("attachment", "")
            if att and _ATTACH_RE.match(att): referenced.add(att)
        referenced_thumbs = {os.path.splitext(fn)[0] + "_thumb.jpg" for fn in referenced}

        to_extract = []   # (target_basename, bytes)
        for zname in zf.namelist():
            if not zname.startswith("attachments/") or zname.endswith("/"): continue
            base = zname[len("attachments/"):]
            is_attachment = bool(_ATTACH_RE.match(base))
            is_thumb = bool(_THUMB_RE.match(base))
            if not (is_attachment or is_thumb): continue
            if base not in referenced and base not in referenced_thumbs: continue
            try: data = zf.read(zname)
            except Exception: continue
            if len(data) > MAX_UPLOAD_BYTES:
                handler.respond_json({"error": f"Attachment {base} exceeds {MAX_UPLOAD_BYTES // 1024 // 1024} MB"}, status=413); return
            ext = "jpg" if is_thumb else os.path.splitext(base)[1].lstrip(".").lower()
            kind = "image" if is_thumb else _attachment_type_for_ext(ext)
            if kind == "image":
                try:
                    from PIL import Image
                    img = Image.open(io.BytesIO(data)); img.verify()
                except ImportError:
                    handler.respond_json({"error": "Pillow not installed on the server"}, status=503); return
                except Exception:
                    handler.respond_json({"error": f"Attachment {base} is not a readable image"}, status=400); return
            elif kind == "pdf":
                if not data.startswith(b"%PDF-"):
                    handler.respond_json({"error": f"Attachment {base} is not a readable PDF"}, status=400); return
            elif kind == "markdown":
                try:
                    data.decode("utf-8")
                except UnicodeDecodeError:
                    handler.respond_json({"error": f"Attachment {base} is not valid UTF-8 markdown"}, status=400); return
            to_extract.append((base, data))

        # ── Phase 2: Commit — write attachments first, then import cards ──
        extracted = 0
        for base, data in to_extract:
            target = os.path.join(self.attachments_dir, base)
            if os.path.isfile(target): continue  # respect dedup, never overwrite
            with open(target, "wb") as dst: dst.write(data)
            extracted += 1
        n = self.db.import_cards(payload)
        handler.respond_json({"ok": True, "imported": n, "attachments_extracted": extracted})

    def _api_cleanup_attachments(self, handler, content_len, content_type):
        """Sweep the attachments folder, delete files no card references. Returns count + freed bytes."""
        if not self.db or not self.attachments_dir:
            handler.respond_json({"error": "Not ready"}, status=503); return
        referenced = self.db.referenced_attachments()
        # Also keep referenced files' matching _thumb.jpg
        referenced_thumbs = {os.path.splitext(fn)[0] + "_thumb.jpg" for fn in referenced}
        removed = 0; freed = 0
        for fn in os.listdir(self.attachments_dir):
            if fn in referenced or fn in referenced_thumbs:
                continue
            # Only touch files matching our naming scheme — never random user files
            if not (_ATTACH_RE.match(fn) or _THUMB_RE.match(fn)):
                continue
            path = os.path.join(self.attachments_dir, fn)
            try:
                freed += os.path.getsize(path)
                os.remove(path); removed += 1
            except OSError:
                pass
        handler.respond_json({"ok": True, "removed": removed, "freed_bytes": freed})

    def _cleanup_attachment_if_unused(self, filename):
        """Remove original + thumbnail iff no remaining card references this filename."""
        if not filename or not self.attachments_dir: return
        if not _ATTACH_RE.match(filename): return  # only operate on our own naming scheme
        if self.db.attachment_reference_count(filename) > 0: return
        orig = os.path.join(self.attachments_dir, filename)
        base = os.path.splitext(filename)[0]
        thumb = os.path.join(self.attachments_dir, f"{base}_thumb.jpg")
        paths = [orig]
        if _attachment_type_for_ext(os.path.splitext(filename)[1].lstrip(".").lower()) == "image":
            paths.append(thumb)
        for p in paths:
            try:
                if os.path.isfile(p): os.remove(p)
            except OSError: pass

    # ─── Attachment upload & serve ─────────────────────────────────────────

    def _api_upload_attachment(self, handler, content_len, content_type):
        """Accepts multipart 'file' field, content-addresses, returns {hash, type, name, ext}.
        Same file uploaded twice → same hash (automatic deduplication)."""
        if not self.attachments_dir:
            handler.respond_json({"error": "Not ready"}, status=503); return
        if content_len > MAX_UPLOAD_BYTES:
            handler.respond_json({"error": f"File too large (max {MAX_UPLOAD_BYTES // 1024 // 1024} MB)"}, status=413); return
        if not content_type or "multipart/form-data" not in content_type:
            handler.respond_json({"error": "Expected multipart/form-data"}, status=400); return
        try:
            files = handler.parse_multipart(content_len, content_type)
        except ValueError as e:
            handler.respond_json({"error": str(e)}, status=413); return
        f = files.get("file")
        if not f or not f.get("data"):
            handler.respond_json({"error": "No file part"}, status=400); return

        data = f["data"]
        filename = f.get("filename") or "image"
        ext = os.path.splitext(filename)[1].lstrip(".").lower() or "jpg"
        if ext not in ALLOWED_ATTACHMENT_EXT:
            handler.respond_json({"error": f"Only image, PDF and Markdown files allowed ({', '.join(sorted(ALLOWED_ATTACHMENT_EXT))})"}, status=400); return
        # Normalize jpeg
        if ext == "jpeg": ext = "jpg"
        kind = _attachment_type_for_ext(ext)

        img = None
        if kind == "image":
            try:
                from PIL import Image, ImageOps
            except ImportError:
                handler.respond_json({"error": "Pillow not installed on the server"}, status=503); return
            try:
                img = Image.open(io.BytesIO(data))
                img.load()
            except Exception as e:
                handler.respond_json({"error": f"Cannot read image: {e}"}, status=400); return
        elif kind == "pdf":
            if not data.startswith(b"%PDF-"):
                handler.respond_json({"error": "Cannot read PDF"}, status=400); return
        elif kind == "markdown":
            try:
                data.decode("utf-8")
            except UnicodeDecodeError:
                handler.respond_json({"error": "Markdown file must be UTF-8 text"}, status=400); return

        # Content-address using SHA-256 of original bytes
        h = hashlib.sha256(data).hexdigest()[:16]
        base = h
        orig_path  = os.path.join(self.attachments_dir, f"{base}.{ext}")
        thumb_path = os.path.join(self.attachments_dir, f"{base}_thumb.jpg")

        # Save original if new
        if not os.path.isfile(orig_path):
            with open(orig_path, "wb") as fh:
                fh.write(data)

        # Build a thumbnail if missing
        if kind == "image" and not os.path.isfile(thumb_path):
            try:
                tn = ImageOps.exif_transpose(img).convert("RGB")
                tn.thumbnail((THUMB_MAX_EDGE, THUMB_MAX_EDGE), Image.LANCZOS)
                tn.save(thumb_path, "JPEG", quality=82, optimize=True)
            except Exception as e:
                # Best-effort: thumbnail failed but we still have the original
                print(f"[LIBRARY] thumbnail failed for {base}.{ext}: {e}")

        handler.respond_json({
            "ok": True,
            "attachment": f"{base}.{ext}",
            "attachment_type": kind,
            "attachment_name": filename,
            "width": img.width if img else 0, "height": img.height if img else 0,
            "size": len(data),
        })

    def _serve_attachment(self, handler, tail):
        """GET /library/attachments/<file>  — serves original or _thumb.jpg from resources/library/attachments/.
        `tail` is the URL-decoded suffix after the prefix. Path traversal protection: only basenames, no ../
        Serves inline (Content-Disposition: inline) so <img src> displays in browser tabs, not forced download."""
        if not self.attachments_dir:
            handler.respond_json({"error": "Not ready"}, status=503); return
        if not tail or "/" in tail or "\\" in tail or ".." in tail:
            handler.respond_json({"error": "Bad path"}, status=400); return
        target = os.path.join(self.attachments_dir, tail)
        if not os.path.isfile(target):
            handler.respond_json({"error": "Not found"}, status=404); return
        ext = os.path.splitext(tail)[1].lstrip(".").lower()
        mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
                "webp": "image/webp", "gif": "image/gif", "bmp": "image/bmp",
                "pdf": "application/pdf", "md": "text/markdown; charset=utf-8",
                "markdown": "text/markdown; charset=utf-8"}.get(ext, "application/octet-stream")
        with open(target, "rb") as fh:
            data = fh.read()
        handler.send_response(200)
        handler.send_header("Content-Type", mime)
        handler.send_header("Content-Length", len(data))
        handler.send_header("Content-Disposition", "inline")
        handler.send_header("Cache-Control", "public, max-age=31536000, immutable")
        handler.end_headers()
        handler.wfile.write(data)



PAGE_BODY = r"""
<style>
/* Prompt Library — module-scoped styles, pl- namespaced */
.pl-app { display:flex; height:calc(100vh - 48px); overflow:hidden; font-size:14px; background:var(--bg-darkest); color:var(--text); }
.pl-side { width:220px; flex-shrink:0; border-right:1px solid var(--border); padding:14px 14px; overflow-y:auto; background:var(--bg-panel); }
@media (max-width:820px){ .pl-side { width:170px; padding:12px 10px; } }
@media (max-width:640px){ .pl-side { width:140px; } }
.pl-main { flex:1; display:flex; flex-direction:column; min-width:0; }
.pl-toolbar { display:flex; align-items:center; gap:8px; padding:11px 16px; border-bottom:1px solid var(--border); background:var(--bg-panel); flex-shrink:0; }
.pl-toolbar input[type=text] { flex:1; background:var(--bg-card); border:1px solid var(--border); border-radius:6px; padding:7px 11px; color:var(--text); font-size:13px; font-family:inherit; outline:none; }
.pl-toolbar input[type=text]:focus { border-color:var(--accent); }
.pl-toolbar .pl-btn-primary { background:var(--accent); color:#fff; border:none; border-radius:6px; padding:7px 14px; cursor:pointer; font-size:13px; white-space:nowrap; font-weight:400; }
.pl-toolbar .pl-btn-primary:hover { background:var(--accent-dim); }
.pl-toolbar .pl-tb-ico { background:none; color:var(--text-dim); padding:6px 8px; font-size:16px; border:none; cursor:pointer; border-radius:6px; }
.pl-toolbar .pl-tb-ico:hover { color:var(--text); background:var(--bg-hover); }
.pl-content { flex:1; overflow-y:auto; padding:16px; }

.pl-group { margin-bottom:14px; }
.pl-group-title { font-size:10px; font-weight:700; text-transform:uppercase; letter-spacing:.08em; color:var(--text-dim); margin-bottom:5px; padding:0 6px; font-family:var(--mono); }
.pl-f { display:flex; align-items:center; gap:6px; padding:5px 8px; border-radius:5px; cursor:pointer; font-size:13px; color:var(--text-dim); transition:background .12s, color .12s; }
.pl-f:hover { background:var(--bg-hover); color:var(--text); }
.pl-f.pl-act { background:var(--bg-active); color:var(--accent); }
.pl-f .pl-cnt { margin-left:auto; font-size:11px; opacity:.6; font-family:var(--mono); }
.pl-fav-toggle { display:flex; align-items:center; gap:6px; padding:6px 8px; border-radius:5px; cursor:pointer; font-size:13px; color:var(--text-dim); margin-top:6px; border-top:1px solid var(--border); padding-top:12px; }
.pl-fav-toggle:hover { background:var(--bg-hover); color:var(--text); }
.pl-fav-toggle.pl-act { color:var(--orange); }

.pl-cards { display:flex; flex-direction:column; gap:8px; }
.pl-card { background:var(--bg-panel); border:1px solid var(--border); border-radius:8px; padding:12px 14px; cursor:pointer; transition:border-color .12s; border-left-width:3px; }
.pl-card:hover { border-color:var(--border-light); }
.pl-card.pl-sel { border-color:var(--accent); border-width:2px; border-left-width:3px; padding:11px 13px; }
/* Color-coded left edge by card type — visual identity */
.pl-card.pl-t-generation { border-left-color:#7c5cf0; }
.pl-card.pl-t-captioner  { border-left-color:#26a87d; }
.pl-card.pl-t-snippet    { border-left-color:#d4a020; }
.pl-card.pl-t-rewrite    { border-left-color:#d05088; }
.pl-card.pl-t-negative   { border-left-color:#d04040; }
.pl-card.pl-t-system     { border-left-color:#4080d0; }
.pl-card.pl-t-notes      { border-left-color:#808080; }
.pl-card-top { display:flex; align-items:center; gap:6px; margin-bottom:6px; flex-wrap:wrap; }
.pl-badge { display:inline-block; font-size:10px; font-weight:700; padding:2px 7px; border-radius:4px; text-transform:uppercase; letter-spacing:.04em; font-family:var(--mono); }
/* Translucent tint of the category hue + saturated text — adapts to both themes
   (subtle tint on dark cards, light tint on white cards). */
.pl-b-generation { background:rgba(124,92,240,.16); color:#7c5cf0; }
.pl-b-captioner  { background:rgba(38,168,125,.18); color:#1f9d74; }
.pl-b-snippet    { background:rgba(212,160,32,.20); color:#a9800f; }
.pl-b-rewrite    { background:rgba(208,80,136,.16); color:#cf5288; }
.pl-b-negative   { background:rgba(208,64,64,.16); color:#d24a4a; }
.pl-b-system     { background:rgba(64,128,208,.18); color:#4282d2; }
.pl-b-notes      { background:rgba(128,128,128,.20); color:#7a7a7a; }
.pl-target-pill { font-size:11px; color:var(--text-dim); background:var(--bg-card); padding:2px 7px; border-radius:4px; font-family:var(--mono); }
.pl-card-title { font-size:14px; font-weight:500; color:var(--text); flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.pl-card-preview { font-size:12px; color:var(--text-dim); line-height:1.5; display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden; margin-bottom:6px; font-family:var(--mono); }
.pl-card-foot { display:flex; align-items:center; gap:4px; flex-wrap:wrap; }
.pl-tag-pill { font-size:10px; padding:2px 7px; border-radius:4px; background:var(--bg-card); color:var(--text-dim); font-family:var(--mono); }
.pl-card-actions { display:flex; gap:2px; margin-left:auto; }
.pl-card-actions button { background:none; border:none; cursor:pointer; padding:3px 6px; color:var(--text-dim); font-size:15px; border-radius:4px; }
.pl-card-actions button:hover { color:var(--text); background:var(--bg-hover); }
.pl-star-on { color:var(--orange) !important; }

/* Card thumbnail (48x48 in list) */
.pl-card-with-thumb { display:flex; gap:12px; align-items:flex-start; }
.pl-thumb-sm { width:48px; height:48px; border-radius:4px; background:var(--bg-card); object-fit:cover; flex-shrink:0; border:1px solid var(--border); cursor:pointer; }
.pl-thumb-sm:hover { border-color:var(--accent); }
.pl-card-body { flex:1; min-width:0; }
.pl-doc-pill { display:inline-flex; align-items:center; gap:5px; max-width:100%; margin:0 0 6px; padding:4px 7px; border-radius:4px; background:var(--bg-card); border:1px solid var(--border); color:var(--text-dim); font:10px var(--mono); }
.pl-doc-pill span { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }

/* Card detail thumbnail (larger) */
.pl-detail-thumb-wrap { margin-bottom:12px; }
.pl-detail-thumb { max-width:400px; max-height:300px; width:auto; height:auto; border-radius:6px; border:1px solid var(--border); display:block; cursor:zoom-in; background:var(--bg-card); }
.pl-detail-thumb-meta { font-size:10px; color:var(--text-dim); margin-top:6px; font-family:var(--mono); display:flex; gap:10px; align-items:center; flex-wrap:wrap; }
.pl-detail-thumb-meta a { color:var(--accent); text-decoration:none; }
.pl-detail-thumb-meta a:hover { text-decoration:underline; }
.pl-doc-detail { display:flex; align-items:center; gap:12px; margin-bottom:12px; padding:10px 12px; border:1px solid var(--border); border-radius:6px; background:var(--bg-card); }
.pl-doc-icon { width:38px; height:38px; border-radius:5px; display:flex; align-items:center; justify-content:center; background:var(--bg-hover); color:var(--accent); font:700 12px var(--mono); flex-shrink:0; text-transform:uppercase; }
.pl-doc-info { min-width:0; flex:1; }
.pl-doc-name { font:12px var(--mono); color:var(--text); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.pl-doc-actions { display:flex; gap:10px; margin-top:4px; font:11px var(--mono); }
.pl-doc-actions a { color:var(--accent); text-decoration:none; }
.pl-doc-actions a:hover { text-decoration:underline; }

/* Editor: drop area + attached file display */
.pl-drop { border:2px dashed var(--border-light); border-radius:6px; padding:24px 16px; text-align:center; color:var(--text-dim); font-size:12px; line-height:1.6; cursor:pointer; transition:border-color .12s, color .12s, background .12s; }
.pl-drop:hover { border-color:var(--accent); color:var(--text); background:var(--accent-glow); }
.pl-drop.pl-drag-over { border-color:var(--accent); color:var(--text); background:var(--accent-glow); }
.pl-attached { display:flex; align-items:center; gap:10px; background:var(--bg-card); border:1px solid var(--border); border-radius:6px; padding:8px 10px; }
.pl-attached img { width:42px; height:42px; border-radius:3px; object-fit:cover; flex-shrink:0; background:var(--bg-darkest); }
.pl-attached .pl-attached-doc { width:42px; height:42px; border-radius:3px; display:flex; align-items:center; justify-content:center; flex-shrink:0; background:var(--bg-hover); color:var(--accent); font:700 11px var(--mono); text-transform:uppercase; }
.pl-attached .pl-attached-info { flex:1; min-width:0; }
.pl-attached .pl-attached-name { font:12px var(--mono); color:var(--text); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.pl-attached .pl-attached-meta { font:10px var(--mono); color:var(--text-dim); margin-top:2px; }
.pl-attached .pl-attached-rm { background:none; border:none; color:var(--red); cursor:pointer; padding:4px 8px; font-size:14px; border-radius:4px; }
.pl-attached .pl-attached-rm:hover { background:rgba(239,68,68,.12); }
.pl-upload-progress { font:11px var(--mono); color:var(--text-dim); padding:8px 12px; }

/* Load more / total counter */
.pl-loadmore { display:flex; align-items:center; justify-content:center; gap:12px; padding:14px 0 6px; }
.pl-loadmore .pl-count { font:11px var(--mono); color:var(--text-dim); }
.pl-loadmore button { background:var(--bg-card); border:1px solid var(--border-light); border-radius:6px; padding:6px 16px; color:var(--text); font:12px var(--font); cursor:pointer; }
.pl-loadmore button:hover { border-color:var(--accent); background:var(--bg-hover); }

.pl-detail { background:var(--bg-panel); border:1px solid var(--border); border-radius:8px; padding:18px 20px; margin-bottom:12px; }
.pl-detail .pl-d-top { display:flex; align-items:center; gap:8px; margin-bottom:14px; flex-wrap:wrap; }
.pl-detail .pl-card-title { font-size:16px; }
.pl-fl { font-size:10px; font-weight:700; text-transform:uppercase; letter-spacing:.08em; color:var(--text-dim); margin:11px 0 4px; font-family:var(--mono); }
.pl-fv { background:var(--bg-card); border-radius:6px; padding:10px 12px; font-size:13px; font-family:var(--mono); line-height:1.6; color:var(--text); white-space:pre-wrap; min-height:40px; word-break:break-word; border:1px solid var(--border); }
.pl-d-actions { display:flex; gap:6px; margin-top:14px; flex-wrap:wrap; }
.pl-d-actions button { background:var(--bg-card); border:1px solid var(--border); border-radius:6px; padding:6px 12px; color:var(--text-dim); cursor:pointer; font-size:12px; display:flex; align-items:center; gap:5px; font-family:inherit; }
.pl-d-actions button:hover { color:var(--text); border-color:var(--border-light); }
.pl-d-actions .pl-del { color:var(--red); }
.pl-d-actions .pl-del:hover { background:var(--red); color:#fff; border-color:transparent; }

.pl-editor { background:var(--bg-panel); border:1px solid var(--border); border-radius:8px; padding:18px 20px; }
.pl-editor-title { font-size:15px; font-weight:600; color:var(--text-bright); margin-bottom:14px; }
.pl-editor .pl-row { display:flex; gap:10px; }
.pl-editor .pl-row > * { flex:1; }
.pl-editor input, .pl-editor select, .pl-editor textarea {
    width:100%; background:var(--bg-card); border:1px solid var(--border); border-radius:6px;
    padding:8px 11px; color:var(--text); font-size:13px; font-family:inherit; outline:none;
    transition:border-color .12s;
}
.pl-editor input:focus, .pl-editor select:focus, .pl-editor textarea:focus { border-color:var(--accent); }
.pl-editor textarea { font-family:var(--mono); min-height:80px; resize:vertical; line-height:1.55; }
.pl-editor select { appearance:none; -webkit-appearance:none; background-image:url("data:image/svg+xml;charset=utf-8,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='6' viewBox='0 0 10 6'%3E%3Cpath fill='%236b7280' d='M1 1l4 4 4-4'/%3E%3C/svg%3E"); background-repeat:no-repeat; background-position:right 12px center; padding-right:30px; cursor:pointer; }
.pl-editor select option { background:var(--bg-card); color:var(--text); padding:8px; }
.pl-editor .pl-ed-actions { display:flex; gap:8px; margin-top:16px; justify-content:flex-end; }
.pl-editor .pl-ed-actions button { padding:7px 18px; border-radius:6px; cursor:pointer; font-size:13px; border:1px solid var(--border); background:var(--bg-card); color:var(--text); font-family:inherit; }
.pl-editor .pl-ed-actions button:hover { border-color:var(--border-light); }
.pl-editor .pl-ed-actions .pl-save { background:var(--accent); color:#fff; border-color:transparent; }
.pl-editor .pl-ed-actions .pl-save:hover { background:var(--accent-dim); }

.pl-empty { text-align:center; padding:60px 20px; color:var(--text-dim); }
.pl-empty .pl-empty-icon { margin-bottom:12px; opacity:.45; color:var(--text-dim); display:flex; justify-content:center; }
.pl-empty .pl-empty-icon svg { width:42px; height:42px; display:block; }

/* Markdown rendering inside notes view */
.pl-md-body { padding:8px 12px; }
.pl-md-body > *:first-child { margin-top:0; }
.pl-md-body > *:last-child { margin-bottom:0; }
.pl-md-p { margin:0 0 8px; line-height:1.55; }
.pl-md-h { font-size:14px; font-weight:600; color:var(--text-bright); margin:12px 0 6px; line-height:1.3; }
.pl-md-h:first-child { margin-top:0; }
h1.pl-md-h { font-size:16px; } h2.pl-md-h { font-size:14px; } h3.pl-md-h { font-size:13px; color:var(--text); }
.pl-md-ul, .pl-md-ol { margin:4px 0 8px; padding-left:22px; }
.pl-md-ul li, .pl-md-ol li { margin:2px 0; line-height:1.55; }
.pl-md-bq { border-left:3px solid var(--border-light); padding:2px 12px; margin:6px 0; color:var(--text-dim); font-style:italic; }
.pl-md-hr { border:none; border-top:1px solid var(--border); margin:12px 0; }
.pl-md-pre { background:var(--bg-darkest); border:1px solid var(--border); border-radius:4px; padding:8px 10px; margin:6px 0; font-family:var(--mono); font-size:11px; line-height:1.5; overflow-x:auto; white-space:pre; color:var(--text); }
.pl-md-code { background:var(--bg-darkest); border:1px solid var(--border); border-radius:3px; padding:1px 5px; font-family:var(--mono); font-size:11px; color:var(--orange); }
.pl-md-a { color:var(--accent); text-decoration:none; border-bottom:1px dotted var(--accent); }
.pl-md-a:hover { border-bottom-style:solid; }

/* Keyboard shortcut hint in toolbar */
.pl-kbd-hint { font-size:10px; color:var(--text-dim); margin-left:8px; opacity:.55; font-family:var(--mono); }
.pl-kbd-hint kbd { background:var(--bg-card); border:1px solid var(--border); border-radius:3px; padding:1px 5px; font-family:var(--mono); font-size:10px; }
</style>

<div class="pl-app">
  <div class="pl-side">
    <div class="pl-group"><div class="pl-group-title">Type</div><div id="typeF"></div></div>
    <div class="pl-group"><div class="pl-group-title">Target</div><div id="targetF"></div></div>
    <div class="pl-group"><div class="pl-group-title">Tags</div><div id="tagF"></div></div>
    <div class="pl-fav-toggle" id="favT" onclick="toggleFavF()">&#x2605; Favorites only</div>
  </div>
  <div class="pl-main">
    <div class="pl-toolbar">
      <input type="text" id="plQ" placeholder="Search prompts, content, notes..." oninput="debSearch()">
      <button class="pl-tb-ico" onclick="doExport()" title="Export JSON (cards only)">&#x2B07; JSON</button>
      <button class="pl-tb-ico" onclick="doExportZip()" title="Export ZIP (cards + images)">&#x2B07; ZIP</button>
      <button class="pl-tb-ico" onclick="document.getElementById('impF').click()" title="Import JSON or ZIP">&#x2B06;</button>
      <input type="file" id="impF" accept=".json,.zip,application/json,application/zip" style="display:none" onchange="doImport(event)">
      <button class="pl-tb-ico" onclick="doCleanup()" title="Clean unused attachments from disk">&#x2715;</button>
      <button class="pl-btn-primary" onclick="showEd(null)">&#xFF0B; New card</button>
      <span class="pl-kbd-hint"><kbd>N</kbd> new · <kbd>/</kbd> search · <kbd>Esc</kbd> close</span>
    </div>
    <div class="pl-content" id="plC"></div>
  </div>
</div>

<script>
var _cards=[],_sel=null,_flt={type:'all',target:'all',tag:null,fav:false,q:''},_deb=null,_edMode=false;
var _pageSize=200,_total=0;

function E(s){var d=document.createElement('div');d.textContent=s||'';return d.innerHTML;}
function EA(s){return(s||'').replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');}
function bc(t){return'pl-b-'+(t||'generation');}
function api(u,o){return fetch(u,o).then(function(r){return r.json()});}

function buildListUrl(offset){
  var p='?sort=updated&limit='+_pageSize+'&offset='+offset;
  if(_flt.type!=='all')p+='&type='+_flt.type;
  if(_flt.target!=='all')p+='&target='+_flt.target;
  if(_flt.tag)p+='&tag='+encodeURIComponent(_flt.tag);
  if(_flt.fav)p+='&fav=1';
  if(_flt.q)p+='&q='+encodeURIComponent(_flt.q);
  return '/api/library/cards'+p;
}

function loadCards(){
  api(buildListUrl(0)).then(function(r){
    _cards=r.cards||[];_total=r.total||0;
    if(_sel){var f=_cards.find(function(c){return c.id===_sel.id});_sel=f||null;}
    if(!_edMode)renderCards();
  });
}

function loadMore(){
  api(buildListUrl(_cards.length)).then(function(r){
    if(r.cards&&r.cards.length){_cards=_cards.concat(r.cards);_total=r.total||_total;renderCards();}
  });
}

function loadSide(){
  api('/api/library/stats').then(function(s){
    var types=[{k:'all',l:'All',n:s.total||0}];
    ['generation','captioner','snippet','rewrite','negative','system','notes'].forEach(function(t){
      types.push({k:t,l:t.charAt(0).toUpperCase()+t.slice(1),n:(s.by_type||{})[t]||0});
    });
    var h='';types.forEach(function(t){
      h+='<div class="pl-f'+(_flt.type===t.k?' pl-act':'')+'" onclick="setF(\'type\',\''+t.k+'\')">'+E(t.l)+'<span class="pl-cnt">'+t.n+'</span></div>';
    });
    document.getElementById('typeF').innerHTML=h;
    var tgts=[{k:'all',l:'All',n:s.total||0}];
    ['general','sdxl','zit','flux','illustrious','pony','llm','suno'].forEach(function(t){
      var n=(s.by_target||{})[t]||0;
      if(n>0||t==='all')tgts.push({k:t,l:t==='zit'?'Z-Image Turbo':t.charAt(0).toUpperCase()+t.slice(1),n:n});
    });
    var h2='';tgts.forEach(function(t){
      h2+='<div class="pl-f'+(_flt.target===t.k?' pl-act':'')+'" onclick="setF(\'target\',\''+t.k+'\')">'+E(t.l)+'<span class="pl-cnt">'+t.n+'</span></div>';
    });
    document.getElementById('targetF').innerHTML=h2;
  });
  api('/api/library/tags').then(function(tags){
    var h='';tags.slice(0,20).forEach(function(t){
      h+='<div class="pl-f'+(_flt.tag===t.name?' pl-act':'')+'" onclick="setF(\'tag\',\''+EA(t.name)+'\')">'+E(t.name)+'<span class="pl-cnt">'+t.count+'</span></div>';
    });
    if(!tags.length)h='<div style="font-size:12px;color:var(--text-dim);padding:4px 8px">No tags yet</div>';
    document.getElementById('tagF').innerHTML=h;
  });
  document.getElementById('favT').className='pl-fav-toggle'+(_flt.fav?' pl-act':'');
}

function setF(k,v){if(k==='tag'){_flt.tag=(_flt.tag===v)?null:v;}else{_flt[k]=v;}_sel=null;_edMode=false;loadSide();loadCards();}
function toggleFavF(){_flt.fav=!_flt.fav;_sel=null;_edMode=false;loadSide();loadCards();}
function debSearch(){clearTimeout(_deb);_deb=setTimeout(function(){_flt.q=document.getElementById('plQ').value.trim();loadCards();},250);}

function renderCards(){
  var el=document.getElementById('plC');
  if(!_cards.length){el.innerHTML='<div class="pl-empty"><div class="pl-empty-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M4 5.5A2.5 2.5 0 0 1 6.5 3H20v17H6.5A2.5 2.5 0 0 0 4 22V5.5z"/><path d="M8 7h8"/><path d="M8 11h6"/></svg></div>No prompt cards yet<br><span style="font-size:13px">Click <b>+ New card</b> to create one</span></div>';return;}
  var validTypes={generation:1,captioner:1,snippet:1,rewrite:1,negative:1,system:1,notes:1};
  var h='<div class="pl-cards">';
  _cards.forEach(function(c){
    var s=_sel&&_sel.id===c.id;
    /* Belt-and-braces: even though backend whitelists, fall back here if untrusted JSON ever slips through */
    var safeType=validTypes[c.type]?c.type:'generation';
    h+='<div class="pl-card pl-t-'+safeType+(s?' pl-sel':'')+'" onclick="selCard('+c.id+')">';
    var hasThumb=c.attachment&&c.attachment_type==='image';
    if(hasThumb)h+='<div class="pl-card-with-thumb"><img class="pl-thumb-sm" src="/library/attachments/'+encodeURIComponent(thumbName(c.attachment))+'" alt="" onerror="this.style.display=\'none\'"><div class="pl-card-body">';
    h+='<div class="pl-card-top"><span class="pl-badge '+bc(safeType)+'">'+E(safeType)+'</span>';
    h+='<span class="pl-target-pill">'+E(c.target==='zit'?'ZIT':c.target)+'</span>';
    h+='<span class="pl-card-title">'+E(c.title)+'</span>';
    h+='<div class="pl-card-actions"><button class="'+(c.favorite?'pl-star-on':'')+'" onclick="event.stopPropagation();togFav('+c.id+')" title="Favorite">&#x2605;</button>';
    h+='<button onclick="event.stopPropagation();copyC('+c.id+')" title="Copy">&#x2398;</button></div></div>';
    if(c.attachment&&c.attachment_type!=='image')h+='<div class="pl-doc-pill">'+E(docLabel(c))+' <span>'+E(c.attachment_name||c.attachment)+'</span></div>';
    if(c.content)h+='<div class="pl-card-preview">'+E(c.content)+'</div>';
    if(c.tags&&c.tags.length){h+='<div class="pl-card-foot">';c.tags.forEach(function(t){h+='<span class="pl-tag-pill">'+E(t)+'</span>';});
      if(c.source_image)h+='<span class="pl-tag-pill" style="margin-left:auto">gallery</span>';h+='</div>';}
    if(hasThumb)h+='</div></div>'; // close pl-card-body + pl-card-with-thumb
    h+='</div>';
    if(s)h+=detailHTML(c);
  });
  h+='</div>';
  /* Load more / total counter */
  if(_total>_cards.length){
    h+='<div class="pl-loadmore"><span class="pl-count">Showing '+_cards.length+' of '+_total+'</span>'
      +'<button onclick="loadMore()">Load more</button></div>';
  } else if(_total>=20){
    h+='<div class="pl-loadmore"><span class="pl-count">'+_cards.length+' cards</span></div>';
  }
  el.innerHTML=h;
}

/* Given "abc123.png" → "abc123_thumb.jpg" */
function thumbName(fn){
  var dot=fn.lastIndexOf('.');
  var base=dot>=0?fn.substring(0,dot):fn;
  return base+'_thumb.jpg';
}

function docLabel(c){
  var t=(c.attachment_type||'file').toUpperCase();
  if(t==='MARKDOWN')return'MD';
  return t;
}

function attachUrl(c){return'/library/attachments/'+encodeURIComponent(c.attachment);}

function selCard(id){_sel=(_sel&&_sel.id===id)?null:_cards.find(function(c){return c.id===id})||null;_edMode=false;renderCards();}

function detailHTML(c){
  var h='<div class="pl-detail"><div class="pl-d-top"><span class="pl-badge '+bc(c.type)+'">'+E(c.type)+'</span>';
  h+='<span class="pl-target-pill">'+E(c.target)+'</span><span class="pl-card-title">'+E(c.title)+'</span>';
  h+='<button style="background:none;border:none;cursor:pointer;font-size:18px;color:'+(c.favorite?'var(--orange)':'var(--text-dim)')+'" onclick="togFav('+c.id+')">&#x2605;</button></div>';
  if(c.attachment&&c.attachment_type==='image'){
    /* encodeURIComponent prevents any rogue character in attachment from breaking out of the href context */
    var origUrl=attachUrl(c);
    h+='<div class="pl-detail-thumb-wrap"><img class="pl-detail-thumb" src="'+origUrl+'" onclick="window.open(this.src,\'_blank\')" alt="">'
       +'<div class="pl-detail-thumb-meta">'+E(c.attachment_name||c.attachment)+' &middot; <a href="'+origUrl+'" target="_blank" rel="noopener">view original</a> &middot; <a href="'+origUrl+'" download="'+EA(c.attachment_name||c.attachment)+'">download</a></div></div>';
  } else if(c.attachment){
    var docUrl=attachUrl(c);
    h+='<div class="pl-doc-detail"><div class="pl-doc-icon">'+E(docLabel(c))+'</div><div class="pl-doc-info">'
      +'<div class="pl-doc-name">'+E(c.attachment_name||c.attachment)+'</div>'
      +'<div class="pl-doc-actions"><a href="'+docUrl+'" target="_blank" rel="noopener">open</a><a href="'+docUrl+'" download="'+EA(c.attachment_name||c.attachment)+'">download</a></div>'
      +'</div></div>';
  }
  h+='<div class="pl-fl">Content</div><div class="pl-fv">'+E(c.content)+'</div>';
  if(c.negative)h+='<div class="pl-fl">Negative</div><div class="pl-fv">'+E(c.negative)+'</div>';
  if(c.notes)h+='<div class="pl-fl">Notes</div><div class="pl-fv pl-md-body" style="font-family:inherit">'+mdRender(c.notes)+'</div>';
  if(c.tags&&c.tags.length){h+='<div class="pl-fl">Tags</div><div style="display:flex;gap:4px;flex-wrap:wrap;margin-top:4px">';c.tags.forEach(function(t){h+='<span class="pl-tag-pill">'+E(t)+'</span>';});h+='</div>';}
  if(c.source_image)h+='<div class="pl-fl">Source</div><div style="font-size:12px;color:var(--text-dim);font-family:var(--mono)">'+E(c.source_image)+'</div>';
  if(c.source_meta){try{var sm=typeof c.source_meta==='string'?JSON.parse(c.source_meta):c.source_meta;
    if(sm&&typeof sm==='object'){var p=[];for(var k in sm)p.push(k+': '+sm[k]);
      if(p.length)h+='<div style="font-size:11px;color:var(--text-dim);margin-top:4px">'+E(p.join(' \u00B7 '))+'</div>';}}catch(e){}}
  h+='<div class="pl-d-actions"><button onclick="copyC('+c.id+')">&#x2398; Copy prompt</button>';
  h+='<button onclick="showEd('+c.id+')">&#x270E; Edit</button>';
  h+='<button onclick="dupCard('+c.id+')">&#x2750; Duplicate</button>';
  h+='<button class="pl-del" onclick="delCard('+c.id+')">&#x2717; Delete</button></div></div>';
  return h;
}

function togFav(id){api('/api/library/card/favorite',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:id})}).then(function(){loadCards();loadSide();});}

function copyC(id){var c=_cards.find(function(x){return x.id===id});if(!c)return;
  var t=c.content||'';if(c.negative)t+='\nNegative prompt: '+c.negative;
  (function(v){function fb(){try{var e=document.createElement('textarea');e.value=v;e.style.position='fixed';e.style.opacity='0';document.body.appendChild(e);e.select();document.execCommand('copy');document.body.removeChild(e);toast('Copied to clipboard');}catch(err){toast('Could not copy');}}if(navigator.clipboard&&window.isSecureContext){navigator.clipboard.writeText(v).then(function(){toast('Copied to clipboard');}).catch(fb);}else{fb();}})(t);}

function delCard(id){if(!confirm('Delete this prompt card?'))return;
  api('/api/library/card/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:id})}).then(function(){_sel=null;_edMode=false;loadCards();loadSide();});}

function dupCard(id){var c=_cards.find(function(x){return x.id===id});if(!c)return;
  var d=JSON.parse(JSON.stringify(c));delete d.id;d.title+=' (copy)';d.favorite=0;
  api('/api/library/card',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(d)}).then(function(){loadCards();loadSide();toast('Duplicated');});}

/* Per-editor attachment state — gets stamped onto the card on save. */
var _edAttach=null; /* {attachment, attachment_type, attachment_name, width, height, size} */

function showEd(id){
  _edMode=true;var c=id?_cards.find(function(x){return x.id===id}):null;
  /* Initialise local attachment state from the card (if editing) */
  _edAttach = (c && c.attachment) ? {attachment:c.attachment, attachment_type:c.attachment_type, attachment_name:c.attachment_name||c.attachment} : null;
  var el=document.getElementById('plC');
  var h='<div class="pl-editor"><div class="pl-editor-title">'+(c?'Edit card':'New prompt card')+'</div>';
  h+='<div class="pl-fl">Title</div><input id="eT" value="'+EA(c?c.title:'')+'" placeholder="Card title...">';
  h+='<div class="pl-row"><div><div class="pl-fl">Type</div><select id="eTy">';
  ['generation','captioner','snippet','rewrite','negative','system','notes'].forEach(function(t){
    h+='<option value="'+t+'"'+(c&&c.type===t?' selected':(!c&&t==='generation'?' selected':''))+'>'+t.charAt(0).toUpperCase()+t.slice(1)+'</option>';});
  h+='</select></div><div><div class="pl-fl">Target</div><select id="eTg">';
  ['general','sdxl','zit','flux','illustrious','pony','llm','suno'].forEach(function(t){
    h+='<option value="'+t+'"'+(c&&c.target===t?' selected':'')+'>'+(t==='zit'?'Z-Image Turbo':t.charAt(0).toUpperCase()+t.slice(1))+'</option>';});
  h+='</select></div></div>';
  h+='<div class="pl-fl">Attachment (optional)</div>';
  h+='<div id="eAttachWrap"></div>';
  h+='<input type="file" id="eFile" accept="image/*,.pdf,.md,.markdown,application/pdf,text/markdown,text/plain" style="display:none">';
  h+='<div class="pl-fl">Tags</div><input id="eTa" value="'+EA(c?(c.tags||[]).join(', '):'')+'" placeholder="portrait, lighting, cinematic...">';
  h+='<div class="pl-fl">Content</div><textarea id="eC" rows="5" placeholder="Prompt, preset, snippet or notes...">'+(c?E(c.content):'')+'</textarea>';
  h+='<div class="pl-fl">Negative prompt (optional)</div><textarea id="eN" rows="2" placeholder="Negative prompt...">'+(c?E(c.negative):'')+'</textarea>';
  h+='<div class="pl-fl">Notes (optional) <span style="font-weight:400;text-transform:none;font-family:inherit;color:var(--text-dim);margin-left:6px">— supports markdown</span></div><textarea id="eNo" rows="3" style="font-family:var(--mono)" placeholder="Notes, references, settings... **bold**, *italic*, `code`, # heading, - list">'+(c?E(c.notes):'')+'</textarea>';
  h+='<div class="pl-ed-actions"><button onclick="_edMode=false;_edAttach=null;renderCards()">Cancel</button>';
  h+='<button class="pl-save" onclick="saveEd('+(c?c.id:'null')+')">'+(c?'Update':'Create')+'</button></div></div>';
  el.innerHTML=h;
  renderAttachEditor();
  wireAttachEditor();
}

function renderAttachEditor(){
  var wrap=document.getElementById('eAttachWrap'); if(!wrap)return;
  if(_edAttach&&_edAttach.attachment){
    var isImage=_edAttach.attachment_type==='image';
    var thumb='/library/attachments/'+encodeURIComponent(thumbName(_edAttach.attachment));
    wrap.innerHTML='<div class="pl-attached">'
      +(isImage?'<img src="'+thumb+'" alt="">':'<div class="pl-attached-doc">'+E(docLabel(_edAttach))+'</div>')
      +'<div class="pl-attached-info">'
      +'<div class="pl-attached-name">'+E(_edAttach.attachment_name||_edAttach.attachment)+'</div>'
      +'<div class="pl-attached-meta">'+(isImage&&_edAttach.width?(_edAttach.width+' &times; '+_edAttach.height):docLabel(_edAttach))+(_edAttach.size?(' &middot; '+formatBytes(_edAttach.size)):'')+'</div>'
      +'</div>'
      +'<button class="pl-attached-rm" onclick="removeAttach()" title="Remove">&#x2717;</button>'
      +'</div>';
  } else {
    wrap.innerHTML='<div class="pl-drop" id="eDrop" onclick="document.getElementById(\'eFile\').click()">Drop file here<br>or click to browse<div style="font-size:10px;margin-top:4px;opacity:.6">JPG · PNG · WEBP · GIF · PDF · MD &middot; max 15 MB</div></div>';
  }
}

function wireAttachEditor(){
  var fi=document.getElementById('eFile');
  if(fi)fi.onchange=function(e){var f=e.target.files[0]; if(f)uploadAttach(f); e.target.value='';};
  /* Drag-drop on the area */
  var d=document.getElementById('eDrop');
  if(d){
    ['dragenter','dragover'].forEach(function(ev){d.addEventListener(ev,function(e){e.preventDefault();e.stopPropagation();d.classList.add('pl-drag-over');});});
    ['dragleave','drop'].forEach(function(ev){d.addEventListener(ev,function(e){e.preventDefault();e.stopPropagation();d.classList.remove('pl-drag-over');});});
    d.addEventListener('drop',function(e){var f=e.dataTransfer.files[0]; if(f)uploadAttach(f);});
  }
}

function uploadAttach(file){
  if(!file)return;
  if(file.size>15*1024*1024){toast('File too large (max 15 MB)','error');return;}
  var wrap=document.getElementById('eAttachWrap');
  if(wrap)wrap.innerHTML='<div class="pl-upload-progress">Uploading '+E(file.name)+'…</div>';
  var fd=new FormData();fd.append('file',file,file.name);
  fetch('/api/library/attachment',{method:'POST',body:fd})
    .then(function(r){return r.json();})
    .then(function(d){
      if(d.error){toast('Upload failed: '+d.error,'error');renderAttachEditor();wireAttachEditor();return;}
      _edAttach={attachment:d.attachment,attachment_type:d.attachment_type,attachment_name:d.attachment_name,width:d.width,height:d.height,size:d.size};
      renderAttachEditor();wireAttachEditor();
    })
    .catch(function(e){toast('Upload error','error');renderAttachEditor();wireAttachEditor();});
}

function removeAttach(){_edAttach=null;renderAttachEditor();wireAttachEditor();}

function formatBytes(n){
  if(!n)return'';
  if(n<1024)return n+' B';
  if(n<1024*1024)return Math.round(n/1024)+' KB';
  return (n/(1024*1024)).toFixed(1)+' MB';
}

function saveEd(id){
  var d={title:document.getElementById('eT').value.trim()||'Untitled',type:document.getElementById('eTy').value,
    target:document.getElementById('eTg').value,tags:document.getElementById('eTa').value,
    content:document.getElementById('eC').value,negative:document.getElementById('eN').value,
    notes:document.getElementById('eNo').value,
    attachment:_edAttach?_edAttach.attachment:'',
    attachment_type:_edAttach?_edAttach.attachment_type:'',
    attachment_name:_edAttach?_edAttach.attachment_name:''};
  if(id){d.id=id;api('/api/library/card/update',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(d)}).then(function(){_edMode=false;_edAttach=null;loadCards();loadSide();toast('Updated');});}
  else{api('/api/library/card',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(d)}).then(function(){_edMode=false;_edAttach=null;loadCards();loadSide();toast('Created');});}
}

function doExport(){fetch('/api/library/export').then(function(r){return r.json()}).then(function(d){
  var b=new Blob([JSON.stringify(d,null,2)],{type:'application/json'});var a=document.createElement('a');
  a.href=URL.createObjectURL(b);a.download='prompt-library.json';a.click();toast('Exported '+d.cards.length+' cards');});}

function doExportZip(){
  /* Server bundles cards + only referenced attachment files into a single ZIP */
  toast('Building ZIP...');
  fetch('/api/library/export-zip').then(function(r){
    if(!r.ok)throw new Error('export failed');
    return r.blob();
  }).then(function(b){
    var a=document.createElement('a');a.href=URL.createObjectURL(b);
    a.download='prompt-library.zip';a.click();
    toast('Exported ZIP ('+Math.round(b.size/1024)+' KB)');
  }).catch(function(){toast('Export failed','error');});
}

function doImport(ev){
  var f=ev.target.files[0];if(!f)return;
  /* Detect by extension: .zip → bundle, .json → cards only */
  var isZip=f.name.toLowerCase().endsWith('.zip')||f.type==='application/zip';
  if(isZip){
    var fd=new FormData();fd.append('file',f,f.name);
    fetch('/api/library/import-zip',{method:'POST',body:fd}).then(function(r){return r.json();}).then(function(r){
      if(r.error){toast('Import failed: '+r.error,'error');}
      else{loadCards();loadSide();toast('Imported '+(r.imported||0)+' cards, '+(r.attachments_extracted||0)+' attachments');}
    }).catch(function(){toast('Import failed','error');});
  } else {
    var r=new FileReader();
    r.onload=function(e){try{var d=JSON.parse(e.target.result);
      api('/api/library/import',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(d)}).then(function(r){loadCards();loadSide();toast('Imported '+(r.imported||0)+' cards');});}
      catch(err){toast('Invalid JSON','error');}};r.readAsText(f);
  }
  ev.target.value='';
}

function doCleanup(){
  if(!confirm('Remove unused attachment files from disk?\n\nFiles not referenced by any card will be deleted. This cannot be undone.'))return;
  fetch('/api/library/cleanup-attachments',{method:'POST'}).then(function(r){return r.json();}).then(function(r){
    if(r.error){toast('Cleanup failed: '+r.error,'error');return;}
    if(r.removed===0)toast('No unused attachments found');
    else toast('Removed '+r.removed+' files ('+Math.round(r.freed_bytes/1024)+' KB freed)');
  });
}

function toast(m){var t=document.createElement('div');t.textContent=m;
  t.style.cssText='position:fixed;bottom:20px;left:50%;transform:translateX(-50%);background:var(--bg-panel);color:var(--text);border:1px solid var(--border-light);padding:9px 18px;border-radius:8px;font-size:13px;z-index:9999;opacity:0;transition:opacity .2s;font-family:var(--font);box-shadow:0 8px 24px rgba(0,0,0,.45)';
  document.body.appendChild(t);requestAnimationFrame(function(){t.style.opacity='1';});
  setTimeout(function(){t.style.opacity='0';setTimeout(function(){t.remove();},300);},2000);}

/* Minimal markdown renderer for notes — safe (HTML-escapes input first, then applies known patterns).
   Handles: # h1..### h3, **bold**, *italic*, `code`, ```code blocks```, [text](url),
   - and 1. lists, > blockquotes, --- horizontal rule, blank-line paragraphs. */
function mdRender(src){
  if(!src)return'';
  var s=String(src).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  // Fenced code first (preserve contents from inline transforms)
  var codeBlocks=[];
  s=s.replace(/```([\s\S]*?)```/g,function(m,c){codeBlocks.push(c.replace(/^\n/,''));return'\x00CB'+(codeBlocks.length-1)+'\x00';});
  var lines=s.split(/\n/),out=[],inUl=false,inOl=false,para=[];
  function flushP(){if(para.length){out.push('<p class="pl-md-p">'+para.join(' ')+'</p>');para=[];}}
  function closeLists(){if(inUl){out.push('</ul>');inUl=false;}if(inOl){out.push('</ol>');inOl=false;}}
  for(var i=0;i<lines.length;i++){
    var ln=lines[i];
    if(/^\x00CB\d+\x00$/.test(ln)){flushP();closeLists();var idx=+ln.replace(/\x00CB|\x00/g,'');out.push('<pre class="pl-md-pre">'+codeBlocks[idx]+'</pre>');continue;}
    if(/^---+\s*$/.test(ln)){flushP();closeLists();out.push('<hr class="pl-md-hr">');continue;}
    var h=ln.match(/^(#{1,3})\s+(.+)$/);
    if(h){flushP();closeLists();out.push('<h'+h[1].length+' class="pl-md-h">'+h[2]+'</h'+h[1].length+'>');continue;}
    var ul=ln.match(/^[-*]\s+(.+)$/);
    if(ul){flushP();if(!inUl){closeLists();out.push('<ul class="pl-md-ul">');inUl=true;}out.push('<li>'+ul[1]+'</li>');continue;}
    var ol=ln.match(/^\d+\.\s+(.+)$/);
    if(ol){flushP();if(!inOl){closeLists();out.push('<ol class="pl-md-ol">');inOl=true;}out.push('<li>'+ol[1]+'</li>');continue;}
    var bq=ln.match(/^&gt;\s?(.*)$/);
    if(bq){flushP();closeLists();out.push('<blockquote class="pl-md-bq">'+bq[1]+'</blockquote>');continue;}
    if(ln.trim()===''){flushP();closeLists();continue;}
    closeLists();para.push(ln);
  }
  flushP();closeLists();
  var html=out.join('\n');
  html=html.replace(/`([^`\n]+)`/g,'<code class="pl-md-code">$1</code>');
  html=html.replace(/\*\*([^*\n]+)\*\*/g,'<strong>$1</strong>');
  html=html.replace(/\*([^*\n]+)\*/g,'<em>$1</em>');
  /* Markdown link: only allow URL chars that can't escape href; escape any " or ' just in case */
  html=html.replace(/\[([^\]]+)\]\((https?:\/\/[^)\s"'<>]+)\)/g,function(_m,text,url){
    var safeUrl=url.replace(/&/g,'&amp;').replace(/"/g,'%22').replace(/'/g,'%27');
    return '<a class="pl-md-a" href="'+safeUrl+'" target="_blank" rel="noopener">'+text+'</a>';
  });
  return html;
}

/* Keyboard shortcuts */
document.addEventListener('keydown',function(e){
  var t=e.target,tag=(t.tagName||'').toLowerCase();
  var inField=tag==='input'||tag==='textarea'||tag==='select';
  // Ctrl/Cmd+K → focus search
  if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='k'){e.preventDefault();var q=document.getElementById('plQ');if(q){q.focus();q.select();}return;}
  if(inField){
    // Esc in editor cancels edit
    if(e.key==='Escape'&&_edMode){_edMode=false;renderCards();t.blur&&t.blur();}
    return;
  }
  if(e.key==='n'||e.key==='N'){e.preventDefault();showEd(null);return;}
  if(e.key==='/'){e.preventDefault();var q=document.getElementById('plQ');if(q){q.focus();q.select();}return;}
  if(e.key==='Escape'){if(_edMode){_edMode=false;renderCards();}else if(_sel){_sel=null;renderCards();}}
});

loadSide();loadCards();
</script>
"""
