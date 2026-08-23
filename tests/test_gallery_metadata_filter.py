import os
import tempfile
import time
import unittest
from unittest.mock import patch

from modules.gallery import GalleryDB


class GalleryMetadataFilterTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = os.path.join(self.temp_dir.name, "images")
        os.makedirs(root)
        self.root = root
        self.db = GalleryDB(
            os.path.join(self.temp_dir.name, "gallery.db"),
            os.path.join(self.temp_dir.name, "thumbs"),
            {"Root": root},
        )
        now = time.time()
        rows = [
            ("Root/with.png", "with.png", 1, 1),
            ("Root/without.png", "without.png", 0, 1),
            ("Root/pending.png", "pending.png", 0, 0),
            ("Root/failed.png", "failed.png", 0, 2),
        ]
        conn = self.db._get_conn()
        for index, (path, name, has_metadata, state) in enumerate(rows):
            conn.execute(
                """INSERT INTO files (
                       path,folder,name,ext,size,mtime,width,height,has_metadata,
                       metadata_json,favorite,processing_state,model_scanned
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,1,?,1)""",
                (path, "Root", name, ".png", 100 + index, now, 64, 64,
                 has_metadata, "{}" if has_metadata else None, state),
            )
            conn.execute(
                "INSERT INTO gallery_search(path,folder,name,tags,metadata) VALUES (?,?,?,?,?)",
                (path, "Root", name, "cat", "cat portrait"),
            )
        conn.execute("INSERT INTO collections(name,color,created) VALUES ('Test','#4a9eff',?)", (now,))
        collection_id = conn.execute("SELECT id FROM collections WHERE name='Test'").fetchone()[0]
        conn.executemany(
            "INSERT INTO file_collections(file_path,collection_id,added) VALUES (?,?,?)",
            [(path, collection_id, now) for path, _name, _meta, _state in rows],
        )
        conn.commit()
        self.collection_id = collection_id
        self.db.search_index_ready = True

    def tearDown(self):
        conn = getattr(self.db._local, "conn", None)
        if conn is not None:
            conn.close()
        self.temp_dir.cleanup()

    def assert_paths(self, result, expected):
        self.assertEqual([item["path"] for item in result["files"]], expected)

    def test_without_metadata_excludes_pending_and_failed_files(self):
        result = self.db.get_files(folder="Root", metadata_filter="without")
        self.assert_paths(result, ["Root/without.png"])

    def test_filter_applies_to_search_favorites_timeline_and_collections(self):
        self.assert_paths(
            self.db.search("cat", metadata_filter="with"),
            ["Root/with.png"],
        )
        self.assert_paths(
            self.db.search_in_folder("cat", "Root", metadata_filter="without"),
            ["Root/without.png"],
        )
        self.assert_paths(
            self.db.get_all_favorites(metadata_filter="without"),
            ["Root/without.png"],
        )
        self.assert_paths(
            self.db.get_timeline_files("today", metadata_filter="with"),
            ["Root/with.png"],
        )
        self.assert_paths(
            self.db.get_collection_files(self.collection_id, metadata_filter="without"),
            ["Root/without.png"],
        )

    def test_metadata_filter_also_applies_to_legacy_search(self):
        self.db.has_fts = False
        self.assert_paths(
            self.db.search("with", metadata_filter="with"),
            ["Root/with.png"],
        )
        self.assert_paths(
            self.db.search_in_folder("without", "Root", metadata_filter="without"),
            ["Root/without.png"],
        )

    def test_file_info_contains_server_folder_and_file_path(self):
        info = self.db.get_file_metadata("Root/with.png")["info"]
        self.assertEqual(info["folder"], "Root")
        root = os.path.realpath(os.path.join(self.temp_dir.name, "images"))
        self.assertEqual(info["folder_path"], root)
        self.assertEqual(info["absolute_path"], os.path.join(root, "with.png"))

    def test_without_metadata_cleanup_candidates_are_completed_only(self):
        self.assertEqual(self.db.count_without_metadata(), 1)
        self.assertEqual(self.db.get_without_metadata_paths(), ["Root/without.png"])

    def test_batch_delete_reports_progress_and_refreshes_database(self):
        file_path = os.path.join(self.root, "without.png")
        with open(file_path, "wb") as image_file:
            image_file.write(b"image")
        progress = []

        with patch("modules.gallery.HAS_TRASH", True), patch(
            "modules.gallery.send2trash", side_effect=os.remove
        ):
            results = self.db.delete_files(
                ["Root/without.png"],
                progress=lambda *args: progress.append(args),
            )

        self.assertEqual(results, [{"path": "Root/without.png", "ok": True}])
        self.assertFalse(os.path.exists(file_path))
        self.assertEqual(self.db.count_without_metadata(), 0)
        self.assertEqual(self.db.get_files(folder="Root", metadata_filter="without")["files"], [])
        self.assertTrue(any(event[4] == "trash" for event in progress))
        self.assertTrue(any(event[4] == "database" for event in progress))


if __name__ == "__main__":
    unittest.main()
