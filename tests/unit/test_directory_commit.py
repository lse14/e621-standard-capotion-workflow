from __future__ import annotations
import sys,tempfile,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT/'core'/'src'))
from anima_core.directory_commit import switch,restore_rollback
class Tests(unittest.TestCase):
 def test_switch(self):
  with tempfile.TemporaryDirectory() as t:
   r=Path(t);d=r/'d';s=r/'.d.s';b=r/'.d.r';d.mkdir();s.mkdir();(d/'x').write_text('old');(s/'x').write_text('new');switch(d,s,b);self.assertEqual('new',(d/'x').read_text());self.assertEqual('old',(b/'x').read_text())
 def test_restore_only_when_dataset_missing(self):
  with tempfile.TemporaryDirectory() as t:
   r=Path(t);d=r/'d';b=r/'.d.r';b.mkdir();(b/'x').write_text('old');self.assertTrue(restore_rollback(d,b));self.assertEqual('old',(d/'x').read_text());self.assertFalse(restore_rollback(d,b))
