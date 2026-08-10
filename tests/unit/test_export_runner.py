from __future__ import annotations
import json,sys,tempfile,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT/'core'/'src'))
from PIL import Image
from anima_core.classify_overlay import serialize_annotation_json
from anima_core.contracts import JobConfig,sha256_json
from anima_core.db import StateDatabase
from anima_core.export_runner import ExportRunner,ExportRunnerError
from anima_core.export_summary import build_export_summary
from anima_core.job_preflight import JobPreparationService
from anima_core.overlay import BaselineView,OverlayLayout,WorkingAnnotationView
from anima_core.scheduler import BoundedScheduler
from anima_core.worker_protocol import ProtocolEnvelopeV1

class _Transport:
 def __init__(self,layout,bad=False,conversions=None):self.layout,self.bad,self.conversions,self.requests=layout,bad,conversions,[]
 def exchange(self,r):
  self.requests.append(r.method)
  if r.method=='hello': p={'schemaVersion':1,'payloadType':'export_hello_result','ready':True};m='hello'
  else:
   outcomes=[]
   for i in r.payload['items']:
    if self.bad: outcomes.append({'schemaVersion':1,'status':'issue','sampleId':i['sampleId'],'leaseId':i['leaseId'],'relativeImagePath':i['relativeImagePath'],'code':'final_json_invalid','fieldErrors':[{'field':'count','code':'count_invalid'},{'field':'tags','code':'cross_field_tag_collision'}]})
    else:
     path,d=self.layout.write_prepared('export',i['leaseId'],'.json',b'{}\n');outcome={'schemaVersion':1,'status':'prepared','sampleId':i['sampleId'],'leaseId':i['leaseId'],'relativeImagePath':i['relativeImagePath'],'artifacts':[{'kind':'json','relativePath':str(path.relative_to(self.layout.root)).replace('/','\\'),'sha256':d if self.conversions is not None else '0'*64}]}
     if self.conversions is not None: outcome['conversions']=self.conversions
     outcomes.append(outcome)
   p={'schemaVersion':1,'payloadType':'export_batch_result','outcomes':outcomes};m='result'
  return ProtocolEnvelopeV1('1.0','response','r-'+r.messageId,'export','export',m,p,replyTo=r.messageId,jobId=r.jobId,configHash=r.configHash)

class ExportRunnerTests(unittest.TestCase):
 def _runner(self,root,bad,conversions=None,*,schema_version=None):
  dataset=root/'d';dataset.mkdir();Image.new('RGB',(2,2)).save(dataset/'a.png');(dataset/'a.json').write_bytes(serialize_annotation_json({'quality':[],'count':'solo','character':'','series':'','artist':'','appearance':[],'tags':['ok'],'environment':[],'nl':''}))
  config_kwargs={} if schema_version is None else {'schemaVersion':schema_version}
  cfg=JobConfig(profile='e621',workMode='in_place',overwriteMode='incremental',sourceRoot=str(dataset),recursive=True,**config_kwargs);cfg.caption['enabled']=cfg.replace['enabled']=cfg.nl['enabled']=cfg.dropout['enabled']=False;cfg.classify['enabled']=True;cfg.export['format']='json'
  if schema_version==7:cfg.tokenBudget['enabled']=False
  prep=JobPreparationService(root/'s.db');job=prep.preflight(cfg.to_dict()).jobId;prep.confirm_workspace(job,confirmed=True,confirmed_rebuild=False);db=StateDatabase.open(root/'s.db')
  if schema_version==7:
   frozen=json.loads(str(db.get_job(job)['config_json']));frozen['tokenBudget'].update({'enabled':True,'resourceManifestRelativePath':r'tokenizers\tokenizer-qwen3-0.6b-anima-v1\resource.json','resourceFingerprint':'a'*64,'contextLimit':512});db.connection.execute('UPDATE jobs SET config_json=?,config_hash=? WHERE job_id=?',(json.dumps(frozen),sha256_json(frozen),job))
  sch=BoundedScheduler(db)
  for m in ('caption','classify','replace') + (('ocr',) if schema_version == 7 else ()) + ('nl','count_review','dropout') + (('token_budget',) if schema_version == 7 else ()):sch.start_module(job,m,enabled=False,profile='e621')
  sch.start_module(job,'export',enabled=True,profile='e621');layout=OverlayLayout.open_existing(str(db.get_job(job)['overlay_root']),job)
  return db,prep,job,ExportRunner(db,sch,_Transport(layout,bad,conversions),WorkingAnnotationView(BaselineView(dataset),layout),job_id=job,worker_instance_id='e')
 def test_v7_enabled_token_budget_blocks_export_before_transport_without_a_frozen_record(self):
  with tempfile.TemporaryDirectory() as t:
   db,p,j,r=self._runner(Path(t),False,schema_version=7)
   try:
    with self.assertRaisesRegex(ExportRunnerError,'Token Budget gate'):
     r.run()
    self.assertEqual([],r.transport.requests)
   finally:db.close();p.close()
 def test_issue_is_aggregated_and_repaired_from_classify(self):
  with tempfile.TemporaryDirectory() as t:
   db,p,j,r=self._runner(Path(t),True)
   try:
    self.assertEqual('completed_with_issues',r.run());issues=db.page_issues(j,limit=10);self.assertEqual(1,len(issues));self.assertEqual('classify',issues[0]['repair_start_module']);self.assertEqual(2,len(json.loads(issues[0]['field_errors_json'])))
   finally:db.close();p.close()
 def test_bad_artifact_digest_fails_module(self):
  with tempfile.TemporaryDirectory() as t:
   db,p,j,r=self._runner(Path(t),False)
   try:
    with self.assertRaises(ExportRunnerError):r.run()
    self.assertEqual('failed',db.get_job(j)['status'])
   finally:db.close();p.close()
 def test_repair_matrix_requires_a_capable_enabled_module_for_every_error(self):
  with tempfile.TemporaryDirectory() as t:
   db,p,j,r=self._runner(Path(t),True)
   try:
    self.assertEqual((True,'classify'),r._repair([{'code':'json_missing_or_blank'}],{'classify':{'enabled':True},'nl':{'enabled':False}}))
    self.assertEqual((True,'nl'),r._repair([{'code':'nl_too_large'}],{'classify':{'enabled':False},'nl':{'enabled':True}}))
    self.assertEqual((True,'classify'),r._repair([{'code':'count_invalid'},{'code':'nl_control_character'}],{'classify':{'enabled':True},'nl':{'enabled':True}}))
    self.assertEqual((False,None),r._repair([{'code':'json_syntax_invalid'}],{'classify':{'enabled':True},'nl':{'enabled':True}}))
    self.assertEqual((False,None),r._repair([{'code':'trigger_tag_collision'}],{'classify':{'enabled':True},'nl':{'enabled':True}}))
    self.assertEqual((False,None),r._repair([{'code':'count_invalid'},{'code':'tag_control_character'}],{'classify':{'enabled':True},'nl':{'enabled':True}}))
    self.assertEqual((False,None),r._repair([{'code':'json_missing_or_blank'}],{'classify':{'enabled':False},'nl':{'enabled':True}}))
    self.assertEqual((False,None),r._repair([{'code':'nl_too_large'}],{'classify':{'enabled':True},'nl':{'enabled':False}}))
    for code in ('json_read_failed','json_too_large','json_invalid_encoding','json_syntax_invalid','json_root_not_object','extra_field','field_type_invalid','array_element_type_invalid','tag_control_character','formatted_tag_collision','trigger_tag_collision'):
     self.assertEqual((False,None),r._repair([{'code':code}],{'classify':{'enabled':True},'nl':{'enabled':True}}),code)
    # F22: a multi-artist string is module-2 output, so classify can rewrite it.
    self.assertEqual((True,'classify'),r._repair([{'code':'tag_not_flat_txt_representable'}],{'classify':{'enabled':True},'nl':{'enabled':False}}))
   finally:db.close();p.close()
 def test_safe_conversion_counts_are_aggregated_into_export_diagnostics(self):
  # F32: ROADMAP.md:988 requires the summary to report converted samples per conversion type.
  with tempfile.TemporaryDirectory() as t:
   db,p,j,r=self._runner(Path(t),False,{'array_string_split':2,'array_duplicate_removed':1})
   try:
    self.assertEqual('completed',r.run())
    self.assertEqual({'samples_converted':1,'array_string_split':2,'array_duplicate_removed':1},{row['code']:row['count'] for row in db.module_diagnostics(j,'export')})
    summary=build_export_summary(job_id=j,format_value='json',job_status=db.get_job(j)['status'],module_summary=db.module_summary(j,'export'),journal=None,diagnostics=db.module_diagnostics(j,'export'))
    self.assertEqual(1,summary.convertedSamples)
    self.assertEqual({'array_string_split':2,'array_duplicate_removed':1},summary.conversions)
    with self.assertRaises(ExportRunnerError):r._collect_conversions({'conversions':{'made_up_code':1}},{})
    with self.assertRaises(ExportRunnerError):r._collect_conversions({'conversions':{'array_string_split':'2'}},{})
   finally:db.close();p.close()

 def test_export_worker_leaves_ocr_sidecars_for_the_core_directory_commit(self):
  with tempfile.TemporaryDirectory() as t:
   db,p,j,r=self._runner(Path(t),False,{'array_string_split':1})
   try:
    sidecar=b'not-a-business-export-artifact';r.view.overlay.write_ocr_sidecar('a.png',sidecar)
    self.assertEqual('completed',r.run())
    self.assertEqual(sidecar,r.view.overlay.ocr_sidecar_path('a.png').read_bytes())
    self.assertEqual([],list(r.view.overlay.root.glob('annotations/ocr_annotations/*')))
   finally:db.close();p.close()
