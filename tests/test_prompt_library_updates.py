import os
import tempfile
import unittest

from modules.prompt_library import PromptLibraryDB


class PromptLibraryUpdateTests(unittest.TestCase):
    def test_partial_update_preserves_card_type_and_target(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            database = PromptLibraryDB(os.path.join(tmpdir, "library.db"))
            try:
                card_id = database.create_card({
                    "title": "Krea preset",
                    "type": "captioner",
                    "target": "llm",
                    "content": "before",
                })

                database.update_card(card_id, {"content": "after"})

                row = database.conn.execute(
                    "SELECT type, target, content FROM prompt_cards WHERE id=?",
                    (card_id,),
                ).fetchone()
                self.assertEqual(row, ("captioner", "llm", "after"))
            finally:
                database.conn.close()


if __name__ == "__main__":
    unittest.main()
