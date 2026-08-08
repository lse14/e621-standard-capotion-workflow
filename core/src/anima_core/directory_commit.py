from __future__ import annotations
import os
from pathlib import Path
class DirectoryCommitError(RuntimeError): pass
def switch(dataset:Path,staging:Path,rollback:Path)->None:
 if not dataset.is_dir() or not staging.is_dir() or rollback.exists():raise DirectoryCommitError('directory switch precondition failed')
 try: os.replace(dataset,rollback);os.replace(staging,dataset)
 except Exception:
  if not dataset.exists() and rollback.exists():os.replace(rollback,dataset)
  raise

def restore_rollback(dataset:Path,rollback:Path)->bool:
 """Restore the old directory only when the current dataset path is absent."""
 if dataset.exists(): return False
 if not rollback.is_dir(): raise DirectoryCommitError('rollback is unavailable')
 os.replace(rollback,dataset);return True
