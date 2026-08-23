"""Civitai models.json loader and hash lookup.

Loads checkpoint hashes from a (full or slim) Civitai models.json export
into an in-memory dict so that gallery metadata showing only a model hash
can be enriched with the model name, version, base, creator and tags.

Also supports updating the models.json from the Civitai API (requires `requests`).
"""

import datetime
import json
import os
import threading
import time

API_URL = "https://civitai.com/api/v1/models"


class CivitaiLookup:
    """In-memory AutoV2 hash → model-info lookup.

    A single instance lives on the Hub. Reloading is cheap; the file is
    a JSON dump.
    """

    def __init__(self):
        self.table = {}            # autov2_hash_lower -> info dict
        self.source_path = None
        self.loaded_at = None
        self.source_mode = ""       # default resource, Settings override, or CLI override
        self.debug = False
        self._update_status = {"running": False, "message": "", "progress": ""}

    def __bool__(self):
        return bool(self.table)

    def load(self, path):
        """Load models.json. Returns True on success."""
        if not path or not os.path.isfile(path):
            return False
        try:
            t0 = time.time()
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            print(f"[CIVITAI] Failed to read {path}: {e}")
            return False

        models = data.get("models", [])
        table = {}
        skipped = 0
        for m in models:
            model_type = m.get("type", "")
            # Only Checkpoints — LoRA/TI/VAE hashes don't appear in image metadata
            if model_type and model_type.lower() != "checkpoint":
                skipped += 1
                continue
            creator = m.get("creator", "")
            if isinstance(creator, dict):
                creator = creator.get("username", "")
            tags = m.get("tags", [])
            versions = m.get("modelVersions") or m.get("v") or []
            for v in versions:
                tw = v.get("trainedWords") or v.get("tw") or []
                base = v.get("baseModel") or v.get("base") or ""
                for fi in v.get("files", []):
                    av2 = fi.get("hash") or fi.get("hashes", {}).get("AutoV2")
                    if not av2:
                        continue
                    table[av2.lower()] = {
                        "model": m.get("name", ""),
                        "version": v.get("name", ""),
                        "file": fi.get("name", ""),
                        "id": m.get("id"),
                        "creator": creator,
                        "type": model_type,
                        "base": base,
                        "tags": tags[:8] if tags else [],
                        "trained_words": tw[:10] if tw else [],
                    }

        self.table = table
        self.source_path = path
        self.loaded_at = time.time()

        elapsed = time.time() - t0
        print(f"[CIVITAI] Loaded {len(table)} hashes from "
              f"{len(models) - skipped} checkpoints in {elapsed:.2f}s")
        if skipped:
            print(f"[CIVITAI] Skipped {skipped} non-checkpoint models")

        data_date = data.get("date")
        if data_date:
            age_days = (time.time() - data_date) / 86400
            date_str = datetime.datetime.fromtimestamp(data_date).strftime("%Y-%m-%d")
            if age_days < 1:
                print(f"[CIVITAI] Data from {date_str} (today)")
            elif age_days < 7:
                print(f"[CIVITAI] Data from {date_str} ({int(age_days)} days ago)")
            else:
                print(f"[CIVITAI] Data from {date_str} ({int(age_days)} days ago — consider updating)")
        return True

    def lookup(self, parsed):
        """Resolve a Civitai model entry from parsed SD parameters."""
        if not self.table:
            return None
        settings = parsed.get("settings", {}) if isinstance(parsed, dict) else {}
        model_hash = settings.get("Model hash", "").strip().lower()
        if not model_hash:
            return None
        hit = self.table.get(model_hash)
        if hit:
            return hit
        if len(model_hash) >= 8:
            for h, info in self.table.items():
                if h.startswith(model_hash) or model_hash.startswith(h):
                    return info
        return None

    def get_info(self):
        """Return status info for the Settings page."""
        info = {
            "loaded": bool(self.table),
            "hashes": len(self.table),
            "path": self.source_path or "",
            "source_mode": self.source_mode or "",
            "update_status": self._update_status,
        }
        if self.source_path and os.path.isfile(self.source_path):
            info["file_size_mb"] = round(os.path.getsize(self.source_path) / 1024 / 1024, 1)
            try:
                with open(self.source_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                info["model_count"] = len(data.get("models", []))
                date = data.get("date")
                if date:
                    info["data_date"] = datetime.datetime.fromtimestamp(date).strftime("%Y-%m-%d %H:%M")
                    info["age_days"] = int((time.time() - date) / 86400)
            except Exception:
                pass
        return info

    # ─── Update from Civitai API ──────────────────────────────────────────

    def log_debug(self, msg):
        if self.debug:
            print(f"[CIVITAI DEBUG] {msg}")

    def start_update(self, api_key="", types=None):
        """Start a background update. Returns immediately."""
        if self._update_status["running"]:
            return {"error": "Update already running"}
        if not self.source_path:
            return {"error": "No models.json path configured"}
        try:
            import requests as _req  # noqa: F401
        except ImportError:
            return {"error": "Python 'requests' not installed. Run: pip install requests"}

        self.log_debug(f"start_update source_path={self.source_path!r} source_mode={self.source_mode!r} api_key_set={bool(api_key)}")
        self._update_status = {"running": True, "message": "Starting...", "progress": ""}
        t = threading.Thread(target=self._run_update, args=(api_key, types or ["Checkpoint"]), daemon=True)
        t.start()
        return {"ok": True, "message": "Update started in background"}

    def _run_update(self, api_key, types):
        """Background thread: fetch new models from Civitai API and merge."""
        import requests

        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        output = self.source_path
        try:
            self.log_debug(f"update output={output!r} types={types}")
            # Load existing
            existing_by_id = {}
            if os.path.isfile(output):
                self._update_status["message"] = "Loading existing models.json..."
                with open(output, "r", encoding="utf-8") as f:
                    data = json.load(f)
                existing_by_id = {m["id"]: m for m in data.get("models", [])}
                self._update_status["message"] = f"Loaded {len(existing_by_id)} existing models"
                self.log_debug(f"loaded existing models={len(existing_by_id)}")

            stop_ids = set(existing_by_id.keys()) if existing_by_id else set()
            total_new = 0
            total_refreshed = 0
            scan_existing = bool(stop_ids)
            # Civitai can surface existing/early-access/updated models among the
            # newest results. Do not stop at the first known ID; scan a small
            # window and stop only after several pages without any new IDs.
            # Existing records may still be refreshed, but refresh noise should
            # not keep the incremental scan alive forever.
            min_scan_pages = 5 if scan_existing else None
            no_change_limit = 5 if scan_existing else None
            max_scan_pages = 50 if scan_existing else None

            for model_type in types:
                self._update_status["message"] = f"Fetching new {model_type} models..."
                params = {"limit": 100, "types": model_type, "sort": "Newest"}
                if api_key:
                    params["nsfw"] = "true"

                page = 0
                pages_without_new = 0
                while True:
                    try:
                        self.log_debug(f"GET {API_URL} params={params}")
                        r = requests.get(API_URL, params=params, headers=headers, timeout=30)
                        self.log_debug(f"response status={r.status_code} url={r.url}")
                        if r.status_code == 401:
                            self.log_debug("API key invalid or expired")
                            self._update_status = {"running": False, "message": "Error: API key invalid or expired", "progress": ""}
                            return
                        if r.status_code == 429:
                            self._update_status["message"] = "Rate limited, waiting 10s..."
                            self.log_debug("rate limited; sleeping 10s")
                            time.sleep(10)
                            continue
                        if r.status_code != 200:
                            detail = ""
                            try:
                                payload = r.json()
                                detail = payload.get("error") or payload.get("message") or ""
                            except Exception:
                                detail = (r.text or "")[:160].strip()
                            msg = f"Error: HTTP {r.status_code}" + (f" — {detail}" if detail else "")
                            self.log_debug(msg)
                            self._update_status = {"running": False, "message": msg, "progress": ""}
                            return
                        rdata = r.json()
                    except requests.exceptions.Timeout:
                        self.log_debug("request timed out; retrying after 5s")
                        time.sleep(5)
                        continue
                    except Exception as e:
                        self.log_debug(f"request error: {e}")
                        self._update_status = {"running": False, "message": f"Error: {e}", "progress": ""}
                        return

                    items = rdata.get("items", [])
                    self.log_debug(f"items={len(items)} nextCursor={bool(rdata.get('metadata', {}).get('nextCursor'))}")
                    if not items:
                        break

                    page_new = 0
                    page_refreshed = 0
                    page_existing = 0
                    for item in items:
                        model = self._strip_model(item)
                        model_id = model["id"]
                        if stop_ids and model_id in stop_ids:
                            page_existing += 1
                            if existing_by_id.get(model_id) != model:
                                existing_by_id[model_id] = model
                                page_refreshed += 1
                                total_refreshed += 1
                            continue
                        existing_by_id[model_id] = model
                        stop_ids.add(model_id)
                        page_new += 1
                        total_new += 1

                    page += 1
                    if page_new:
                        pages_without_new = 0
                    else:
                        pages_without_new += 1
                    self._update_status["progress"] = (
                        f"Page {page}: {total_new} new, {total_refreshed} refreshed "
                        f"({page_new} new, {page_refreshed} refreshed, {page_existing} existing on this page)"
                    )
                    self.log_debug(self._update_status["progress"] + f"; pages_without_new={pages_without_new}")

                    if scan_existing and page >= min_scan_pages and pages_without_new >= no_change_limit:
                        self.log_debug(f"stopping after {pages_without_new} pages without new models")
                        break
                    if scan_existing and page >= max_scan_pages:
                        self.log_debug(f"stopping after max_scan_pages={max_scan_pages}")
                        break

                    cursor = rdata.get("metadata", {}).get("nextCursor")
                    if not cursor:
                        break
                    params["cursor"] = cursor
                    time.sleep(0.5)

            # Save
            if total_new > 0 or total_refreshed > 0:
                self._update_status["message"] = f"Saving {len(existing_by_id)} models..."
                output_data = {
                    "date": time.time(),
                    "source": "civitai.com/api/v1",
                    "types": types,
                    "nsfw_included": bool(api_key),
                    "models": list(existing_by_id.values())
                }
                with open(output, "w", encoding="utf-8") as f:
                    json.dump(output_data, f, separators=(",", ":"))
                self.log_debug(f"saved models={len(existing_by_id)} to {output!r}")

                # Reload into memory
                self.load(output)
                self._update_status = {
                    "running": False,
                    "message": f"Done! Added {total_new} new models, refreshed {total_refreshed}. Total: {len(existing_by_id)} models, {len(self.table)} hashes.",
                    "progress": ""
                }
            else:
                self.log_debug("already up to date; no new models found")
                self._update_status = {
                    "running": False,
                    "message": "Already up to date — no new models found.",
                    "progress": ""
                }

        except Exception as e:
            self._update_status = {"running": False, "message": f"Error: {e}", "progress": ""}
            print(f"[CIVITAI] Update error: {e}")

    @staticmethod
    def _strip_model(item):
        """Strip a Civitai API model to essential fields."""
        model = {
            "id": item["id"],
            "name": item["name"],
            "type": item.get("type", ""),
            "creator": item.get("creator"),
            "tags": item.get("tags", []),
            "modelVersions": []
        }
        for v in item.get("modelVersions", []):
            version = {
                "id": v["id"],
                "name": v.get("name", ""),
                "baseModel": v.get("baseModel", ""),
                "trainedWords": v.get("trainedWords", []),
                "files": []
            }
            for f in v.get("files", []):
                h = f.get("hashes", {})
                version["files"].append({
                    "name": f.get("name", ""),
                    "sizeKB": f.get("sizeKB", 0),
                    "type": f.get("type", ""),
                    "metadata": f.get("metadata", {}),
                    "hashes": h
                })
            model["modelVersions"].append(version)
        return model
