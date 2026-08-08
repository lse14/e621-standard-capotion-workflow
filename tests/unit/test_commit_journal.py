from __future__ import annotations
import sys,tempfile,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT/'core'/'src'))
from anima_core.commit_journal import CommitJournal,CommitJournalError,recovery_action

class CommitJournalTests(unittest.TestCase):
 def test_schema_and_recovery_actions_are_strict(self):
  with tempfile.TemporaryDirectory() as t:
   root=Path(t);dataset=root/'dataset';dataset.mkdir();value={'schemaVersion':1,'jobId':'job','state':'rollback_created','datasetRoot':str(dataset),'stagingRoot':str(root/'.dataset.staging'),'rollbackRoot':str(root/'.dataset.rollback'),'backupZip':str(root/'.dataset.anima-backups'/'job.zip')}
   journal=CommitJournal.from_value(value,job_id='job');self.assertEqual('finish_commit',recovery_action(journal))
   value['state']='invalid'
   with self.assertRaises(CommitJournalError):CommitJournal.from_value(value,job_id='job')
