from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "core" / "src"))
sys.path.insert(0, str(ROOT / "workers" / "export" / "src"))

from PIL import Image

from anima_core.contracts import JobConfig
from anima_core.db import StateDatabase
from anima_core.job_preflight import JobPreparationService
from anima_core.pipeline import PipelineService
from anima_export_worker.normalizer import CaptionDisplayPolicy, normalize_json_bytes


RAW_GROUPS = (
    "artist", "character", "contributor", "copyright", "general",
    "invalid", "lore", "meta", "species",
)
STANDARD_FIELDS = {
    "quality", "count", "character", "series", "artist",
    "appearance", "tags", "environment", "nl",
}


class RawE621PipelineIntegrationTests(unittest.TestCase):
    def test_raw_e621_annotation_reaches_committed_json_and_txt_without_remote_work(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dataset = root / "dataset"
            dataset.mkdir()
            Image.new("RGB", (4, 4), "white").save(dataset / "sample.png")
            raw = {group: [] for group in RAW_GROUPS}
            raw.update({
                "artist": ["kannos"],
                "character": ["raw_character"],
                "contributor": ["ignored_contributor"],
                "copyright": ["example_series"],
                "general": ["solo", "blue_fur", "bed"],
                "invalid": ["ignored_invalid"],
                "lore": ["ignored_lore"],
                "meta": ["cool_colors"],
                "species": ["wolf"],
            })
            (dataset / "sample.json").write_text(
                json.dumps(raw, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

            config = JobConfig(
                profile="e621",
                workMode="in_place",
                overwriteMode="incremental",
                sourceRoot=str(dataset),
            )
            config.nl["enabled"] = False
            assert config.countReview is not None
            config.countReview["enabled"] = False
            config.dropout["enabled"] = True
            config.dropout["artist"]["enabled"] = False
            config.dropout["quality"]["enabled"] = False
            config.dropout["appearanceNl"]["enabled"] = True
            config.export["format"] = "both"

            database_path = root / "state.db"
            preparation = JobPreparationService(database_path)
            pipeline = PipelineService(database_path, install_root=ROOT / ".runtime-build")
            try:
                summary = preparation.preflight(config.to_dict())
                preparation.confirm_workspace(summary.jobId, confirmed=True, confirmed_rebuild=False)
                pipeline.start(summary.jobId)
                deadline = time.monotonic() + 60.0
                while pipeline.is_running(summary.jobId) and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertFalse(pipeline.is_running(summary.jobId), "raw E621 pipeline did not settle within 60 seconds")

                database = StateDatabase.open(database_path)
                try:
                    job = database.get_job(summary.jobId)
                    module_status = {
                        str(row["module_id"]): str(row["status"])
                        for row in database.module_summaries(summary.jobId)
                    }
                    self.assertEqual("succeeded", job["status"])
                    self.assertEqual(
                        {
                            "caption": "completed",
                            "classify": "completed",
                            "replace": "completed",
                            "nl": "skipped",
                            "count_review": "skipped",
                            "dropout": "completed",
                            "export": "completed",
                        },
                        module_status,
                    )
                    self.assertEqual(
                        1,
                        database.module_diagnostic_count(
                            summary.jobId, "caption", "e621_raw_json_converted",
                        ),
                    )
                    overlay_root = Path(str(job["overlay_root"]))
                    self.assertFalse(overlay_root.exists())
                    self.assertIsNone(job["commit_journal_path"])
                finally:
                    database.close()

                final_json = dataset / "sample.json"
                final_txt = dataset / "sample.txt"
                self.assertTrue(final_json.is_file())
                self.assertTrue(final_txt.is_file())
                payload = json.loads(final_json.read_text(encoding="utf-8"))
                self.assertEqual(STANDARD_FIELDS, set(payload))
                self.assertEqual("kannos", payload["artist"])
                self.assertEqual("solo", payload["count"])
                self.assertIn("blue_fur", payload["appearance"])
                serialized = json.dumps(payload, ensure_ascii=False)
                for ignored in ("ignored_contributor", "ignored_invalid", "ignored_lore"):
                    self.assertNotIn(ignored, serialized)
                normalized = normalize_json_bytes(
                    final_json.read_bytes(),
                    CaptionDisplayPolicy(True, True, False, ()),
                    export_format="both",
                )
                self.assertTrue(normalized.valid, normalized.field_errors)
                self.assertTrue(final_txt.read_text(encoding="utf-8").strip())
            finally:
                pipeline.close()
                preparation.close()


if __name__ == "__main__":
    unittest.main()
