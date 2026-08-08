from __future__ import annotations
import json,sys,tempfile,unittest,zipfile
from unittest.mock import patch
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT/'core'/'src'))
from anima_core.annotation_backup import AnnotationBackupError,sha256_file,write_backup
from anima_core.annotation_restore import AnnotationRestoreCoordinator
from anima_core.overlay import OverlayLayout
class BackupTests(unittest.TestCase):
 def test_zip64_backup_is_paged_and_records_missing(self):
  with tempfile.TemporaryDirectory() as t:
   root=Path(t);(root/'a.json').write_text('{"x":1}',encoding='utf-8');calls=[]
   def page(cursor):
    calls.append(cursor);return [{'sample_id':1,'annotation_key':'a','in_processing_scope':True}] if cursor is None else []
   target=write_backup(root,root/'backup.zip',page)
   with zipfile.ZipFile(target) as archive:
    self.assertEqual('{"x":1}',archive.read('a.json').decode());manifest=[json.loads(x) for x in archive.read('manifest.jsonl').splitlines()];self.assertEqual(2,len(manifest));self.assertFalse(manifest[0]['exists']);self.assertTrue(manifest[1]['exists']);self.assertEqual(sha256_file(root/'a.json'),manifest[1]['sha256'])
   self.assertEqual([None,1],calls)

 def test_restore_replaces_only_business_annotations_via_directory_commit(self):
  with tempfile.TemporaryDirectory() as t:
   root=Path(t);dataset=root/'dataset';dataset.mkdir();image=dataset/'a.png';image.write_bytes(b'image')
   original_json=b'{"tags":["original"]}\n';original_txt=b'original\n';(dataset/'a.json').write_bytes(original_json);(dataset/'a.txt').write_bytes(original_txt)
   old_time=1_700_000_000_123_456_700;__import__('os').utime(dataset/'a.json',ns=(old_time,old_time))
   backup_dir=root/'.dataset.anima-backups';backup_dir.mkdir();backup=write_backup(dataset,backup_dir/'job.zip',lambda cursor:[{'sample_id':1,'annotation_key':'a','in_processing_scope':True}] if cursor is None else [])
   source_image_id=image.stat().st_ino;(dataset/'a.json').write_bytes(b'{"tags":["new"]}\n');(dataset/'a.txt').write_bytes(b'new\n')
   layout=OverlayLayout.create(dataset,'job');result=AnnotationRestoreCoordinator(layout).restore(backup)
   self.assertEqual(2,result.restored);self.assertEqual(original_json,(dataset/'a.json').read_bytes());self.assertEqual(original_txt,(dataset/'a.txt').read_bytes())
   self.assertEqual(old_time,(dataset/'a.json').stat().st_mtime_ns);self.assertEqual(source_image_id,image.stat().st_ino)
   self.assertEqual('committed',json.loads(layout.commit_journal_path().read_text(encoding='utf-8'))['state'])

 def test_backup_round_trip_restores_annotations_in_subdirectories(self):
  with tempfile.TemporaryDirectory() as t:
   root=Path(t);dataset=root/'dataset';nested=dataset/'sub';nested.mkdir(parents=True);(nested/'a.png').write_bytes(b'image');original=b'{"tags":["nested"]}\n';(nested/'a.json').write_bytes(original)
   backup_dir=root/'.dataset.anima-backups';backup_dir.mkdir();backup=write_backup(dataset,backup_dir/'job.zip',lambda cursor:[{'sample_id':1,'annotation_key':'sub\\a','in_processing_scope':True}] if cursor is None else [])
   with zipfile.ZipFile(backup) as archive:self.assertIn('sub/a.json',archive.namelist())
   (nested/'a.json').write_bytes(b'{"tags":["new"]}\n');(nested/'a.txt').write_bytes(b'new\n')
   result=AnnotationRestoreCoordinator(OverlayLayout.create(dataset,'job')).restore(backup)
   self.assertEqual(2,result.restored);self.assertEqual(original,(nested/'a.json').read_bytes());self.assertFalse((nested/'a.txt').exists())

 def test_backup_is_rejected_when_an_archived_entry_does_not_match_the_manifest(self):
  with tempfile.TemporaryDirectory() as t:
   root=Path(t);(root/'a.json').write_text('{"x":1}',encoding='utf-8')
   page=lambda cursor:[{'sample_id':1,'annotation_key':'a','in_processing_scope':True}] if cursor is None else []
   with patch('anima_core.annotation_backup.sha256_file',return_value='0'*64):
    with self.assertRaises(AnnotationBackupError):write_backup(root,root/'backup.zip',page)
   self.assertFalse((root/'backup.zip').exists());self.assertFalse((root/'backup.zip.partial').exists())

 def test_restore_deletes_annotations_absent_from_the_backup(self):
  with tempfile.TemporaryDirectory() as t:
   root=Path(t);dataset=root/'dataset';dataset.mkdir();(dataset/'a.png').write_bytes(b'image')
   backup_dir=root/'.dataset.anima-backups';backup_dir.mkdir();backup=write_backup(dataset,backup_dir/'job.zip',lambda cursor:[{'sample_id':1,'annotation_key':'a','in_processing_scope':True}] if cursor is None else [])
   (dataset/'a.json').write_text('{"tags":["new"]}\n',encoding='utf-8');(dataset/'a.txt').write_text('new\n',encoding='utf-8')
   result=AnnotationRestoreCoordinator(OverlayLayout.create(dataset,'job')).restore(backup)
   self.assertEqual(2,result.restored);self.assertFalse((dataset/'a.json').exists());self.assertFalse((dataset/'a.txt').exists())

 def test_backup_and_restore_leave_ocr_sidecars_outside_the_business_inventory(self):
  with tempfile.TemporaryDirectory() as t:
   root=Path(t);dataset=root/'dataset';dataset.mkdir();(dataset/'a.png').write_bytes(b'image');(dataset/'a.json').write_text('{"tags":["old"]}\n',encoding='utf-8');(dataset/'a.txt').write_text('old\n',encoding='utf-8')
   sidecar=dataset/'ocr_annotations'/'a.png.ocr.json';sidecar.parent.mkdir();sidecar.write_bytes(b'ocr-sidecar-must-not-enter-business-backup')
   backup_dir=root/'.dataset.anima-backups';backup_dir.mkdir();backup=write_backup(dataset,backup_dir/'job.zip',lambda cursor:[{'sample_id':1,'annotation_key':'a','in_processing_scope':True}] if cursor is None else [])
   with zipfile.ZipFile(backup) as archive:self.assertFalse(any(name.startswith('ocr_annotations/') for name in archive.namelist()))
   (dataset/'a.json').write_text('{"tags":["new"]}\n',encoding='utf-8');(dataset/'a.txt').write_text('new\n',encoding='utf-8')
   AnnotationRestoreCoordinator(OverlayLayout.create(dataset,'job')).restore(backup)
   self.assertEqual(b'ocr-sidecar-must-not-enter-business-backup',sidecar.read_bytes())
