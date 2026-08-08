from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
INSTALL_ROOT = ROOT / ".runtime-build"
RESOURCE_ROOT = ROOT / "resource-library"
sys.path.insert(0, str(ROOT / "core" / "src"))

from PIL import Image

from anima_core.classify_overlay import serialize_annotation_json
from anima_core.contracts import JobConfig
from anima_core.db import StateDatabase
from anima_core.job_preflight import JobPreparationService
from anima_core.launcher import WorkerLauncher
from anima_core.overlay import BaselineView, OverlayLayout, WorkingAnnotationView
from anima_core.policy_runner import PolicyRunner
from anima_core.scheduler import BoundedScheduler
from anima_core.stdio_transport import StdioJsonlTransport


def _business() -> dict[str, object]:
    return {
        "quality": [], "count": "solo", "character": "amy_rose", "series": "", "artist": "",
        "appearance": ["white hair"], "tags": ["smile"], "environment": ["outdoors"], "nl": "A person smiles.",
    }


class PolicyCoreRunnerIntegrationTests(unittest.TestCase):
    def test_embedded_policy_worker_writes_prepared_artifact_only_core_commits_overlay(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dataset = root / "dataset"
            artist = dataset / "1_Artist"
            artist.mkdir(parents=True)
            Image.new("RGB", (4, 4), "white").save(artist / "image.png")
            (artist / "image.json").write_bytes(serialize_annotation_json(_business()))
            config = JobConfig(profile="e621", workMode="in_place", overwriteMode="incremental", sourceRoot=str(dataset), recursive=True)
            config.caption["enabled"] = config.classify["enabled"] = config.replace["enabled"] = config.nl["enabled"] = False
            assert config.countReview is not None
            config.countReview["enabled"] = False
            config.dropout["enabled"] = True
            config.dropout["quality"]["enabled"] = False
            preparation = JobPreparationService(root / "state.db")
            job_id = preparation.preflight(config.to_dict()).jobId
            preparation.confirm_workspace(job_id, confirmed=True, confirmed_rebuild=False)
            database = StateDatabase.open(root / "state.db")
            try:
                scheduler = BoundedScheduler(database)
                for module in ("caption", "classify", "replace", "nl", "count_review"):
                    scheduler.start_module(job_id, module, enabled=False, profile="e621")
                scheduler.start_module(job_id, "dropout", enabled=True, profile="e621")
                job = database.get_job(job_id)
                layout = OverlayLayout.open_existing(str(job["overlay_root"]), job_id)
                process = WorkerLauncher.from_install_root(INSTALL_ROOT, resource_root=RESOURCE_ROOT).spawn(
                    "policy", expected_owner="policy"
                )
                with StdioJsonlTransport(process) as transport:
                    status = PolicyRunner(database, scheduler, transport, WorkingAnnotationView(BaselineView(dataset), layout),
                        job_id=job_id, worker_instance_id="policy-integration", install_root=RESOURCE_ROOT).run()
                self.assertEqual("completed", status)
                result = json.loads(layout.annotation_path(r"1_Artist\image", ".json").read_text(encoding="utf-8"))
                self.assertEqual("@Artist", result["artist"])
                self.assertEqual("solo", result["count"])
                self.assertEqual(_business()["tags"], result["tags"])
            finally:
                database.close()
                preparation.close()


if __name__ == "__main__":
    unittest.main()
