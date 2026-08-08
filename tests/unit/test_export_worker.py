from __future__ import annotations

import json, sys, tempfile, unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]; sys.path.insert(0, str(ROOT / "workers" / "export" / "src")); sys.path.insert(0, str(ROOT / "shared" / "anima_caption_format"))
from anima_export_worker.protocol import parse_process
from anima_export_worker.worker import ExportWorker

class ExportWorkerTests(unittest.TestCase):
    def test_worker_uses_the_shared_caption_format_package(self) -> None:
        import anima_export_worker.worker as worker_module

        self.assertEqual("anima_caption_format", worker_module.normalize_annotation.__module__.split(".")[0])

    def test_prepares_only_overlay_artifacts_and_aggregates_invalid_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary); dataset=root/"dataset"; dataset.mkdir(); overlay=root/".dataset.anima-overlay-job"; (overlay/"annotations").mkdir(parents=True)
            (overlay/"overlay-manifest.json").write_text(json.dumps({"schemaVersion":1,"jobId":"job-export","datasetRoot":str(dataset)}),encoding="utf-8")
            source={"quality":[],"count":"solo","character":"","series":"","artist":"","appearance":[],"tags":["smile"],"environment":[],"nl":""}
            (overlay/"annotations"/"sample.json").write_text(json.dumps(source),encoding="utf-8")
            worker=ExportWorker(); worker.initialize({"schemaVersion":1,"payloadType":"export_hello_request","jobId":"job-export","configHash":"a"*64,"datasetRoot":str(dataset),"overlayRoot":str(overlay),"format":"both","captionFormat":{"replaceUnderscoresWithSpaces":True,"preserveEscapes":True,"triggersEnabled":False,"triggerTerms":[]}})
            items=parse_process({"schemaVersion":1,"payloadType":"export_process_request","items":[{"schemaVersion":1,"sampleId":1,"leaseId":"lease-export-1","relativeImagePath":"sample.png","annotationKey":"sample"}]})
            outcome=worker.process(items)["outcomes"][0]
            self.assertEqual("prepared",outcome["status"]); self.assertEqual({"json","txt"},{artifact["kind"] for artifact in outcome["artifacts"]})
            self.assertTrue((overlay/"prepared"/"export"/"lease-export-1.json").is_file()); self.assertTrue((overlay/"prepared"/"export"/"lease-export-1.txt").is_file()); self.assertFalse((dataset/"sample.json").exists())
            repeated=parse_process({"schemaVersion":1,"payloadType":"export_process_request","items":[{"schemaVersion":1,"sampleId":2,"leaseId":"lease-export-2","relativeImagePath":"sample.png","annotationKey":"sample"}]})
            repeated_outcome=worker.process(repeated)["outcomes"][0]
            self.assertEqual("prepared",repeated_outcome["status"])
            self.assertEqual((overlay/"prepared"/"export"/"lease-export-1.json").read_bytes(),(overlay/"prepared"/"export"/"lease-export-2.json").read_bytes())
            self.assertEqual((overlay/"prepared"/"export"/"lease-export-1.txt").read_bytes(),(overlay/"prepared"/"export"/"lease-export-2.txt").read_bytes())
            (overlay/"annotations"/"sample.json").write_text('{"tags":NaN,"tags":[]}',encoding="utf-8")
            issue=worker.process(items)["outcomes"][0]
            self.assertEqual("issue",issue["status"]); self.assertEqual("final_json_invalid",issue["code"]); self.assertEqual(1,len(issue["fieldErrors"]))

    def _worker(self,root,payload,fmt,caption):
        dataset=root/"dataset"; dataset.mkdir(); overlay=root/".dataset.anima-overlay-job"; (overlay/"annotations").mkdir(parents=True)
        (overlay/"overlay-manifest.json").write_text(json.dumps({"schemaVersion":1,"jobId":"job-export","datasetRoot":str(dataset)}),encoding="utf-8")
        (overlay/"annotations"/"sample.json").write_text(json.dumps(payload),encoding="utf-8")
        worker=ExportWorker(); worker.initialize({"schemaVersion":1,"payloadType":"export_hello_request","jobId":"job-export","configHash":"a"*64,"datasetRoot":str(dataset),"overlayRoot":str(overlay),"format":fmt,"captionFormat":caption})
        items=parse_process({"schemaVersion":1,"payloadType":"export_process_request","items":[{"schemaVersion":1,"sampleId":1,"leaseId":"lease-export-1","relativeImagePath":"sample.png","annotationKey":"sample"}]})
        return worker.process(items)["outcomes"][0]

    def test_unserializable_trigger_becomes_a_per_item_issue_instead_of_escaping(self) -> None:
        # F22: FlatTextSerializationError used to leave main() and fail the whole job.
        payload={"quality":[],"count":"solo","character":"","series":"","artist":"","appearance":[],"tags":["smile"],"environment":[],"nl":""}
        with tempfile.TemporaryDirectory() as temporary:
            outcome=self._worker(Path(temporary),payload,"both",{"replaceUnderscoresWithSpaces":True,"preserveEscapes":True,"triggersEnabled":True,"triggerTerms":["bad, term"]})
        self.assertEqual("issue",outcome["status"]); self.assertEqual("final_json_invalid",outcome["code"])
        self.assertEqual([{"code":"tag_not_flat_txt_representable"}],outcome["fieldErrors"])

    def test_json_only_export_is_not_blocked_by_display_layer_collisions(self) -> None:
        # F08: `count`/`tags` sharing a bucket label and a trigger collision only matter for TXT.
        payload={"quality":[],"count":"solo","character":"","series":"blue eyes","artist":"","appearance":["blue_eyes"],"tags":["solo"],"environment":[],"nl":""}
        caption={"replaceUnderscoresWithSpaces":True,"preserveEscapes":True,"triggersEnabled":True,"triggerTerms":["solo"]}
        with tempfile.TemporaryDirectory() as temporary:
            outcome=self._worker(Path(temporary),payload,"json",caption)
        self.assertEqual("prepared",outcome["status"]); self.assertEqual([{"kind","relativePath","sha256"}],[set(a) for a in outcome["artifacts"]])
        self.assertEqual(["json"],[a["kind"] for a in outcome["artifacts"]])
        with tempfile.TemporaryDirectory() as temporary:
            blocked=self._worker(Path(temporary),payload,"both",caption)
        self.assertEqual("issue",blocked["status"])
        self.assertEqual({"formatted_tag_collision","trigger_tag_collision"},{e["code"] for e in blocked["fieldErrors"]})

if __name__ == "__main__": unittest.main()
