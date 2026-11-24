import unittest
import tempfile
import json
import sys
from pathlib import Path
from argparse import Namespace
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import ollama_assess as oa

class OllamaTests(unittest.TestCase):
    def result(self):
        return dict(quality=80,aesthetic=85,subject_match=1,people_count=1,eligible=True,reason='Visible expression')
    def args(self,root,**kw):
        return Namespace(endpoint='http://127.0.0.1:11434',model='vision:test',model_digest='digest-a',source=str(root/'source'),output=str(root/'out'),organization='org-a',profile=None,max_images=5,preview_size=1024,focus='',constraints='',timeout=2,resume=False,**kw)
    def inputs(self,root):
        try:
            from PIL import Image
        except ImportError:
            self.skipTest('Pillow not installed in current Python environment')
        source=root/'source';source.mkdir()
        for i in range(2): Image.new('RGB',(30,20),(i*100,0,0)).save(source/f'{i}.jpg')
    def info(self):return {'name':'vision:test','digest':'digest-a','details':{},'capabilities':['vision']}
    def test_endpoints(self):
        self.assertEqual(oa.validate_endpoint('http://127.0.0.1:11434/'),'http://127.0.0.1:11434')
        for bad in ['file:///tmp','http://x@localhost','http://localhost/api','http://localhost/?x=1']:
            with self.assertRaises(ValueError):oa.validate_endpoint(bad)
    def test_vision_and_digest(self):
        tags={'models':[{'name':'vision:test','digest':'a'}]}
        with patch.object(oa,'request_json',side_effect=[tags,{'capabilities':['completion']}]):
            with self.assertRaises(ValueError):oa.preflight('http://localhost','vision:test')
        with patch.object(oa,'request_json',return_value=tags):
            with self.assertRaises(ValueError):oa.preflight('http://localhost','vision:test','b')
            with self.assertRaises(ValueError):oa.preflight('http://localhost','missing:test')
    def test_result_rejection(self):
        for key,value in [('quality',True),('quality',101),('aesthetic',float('nan')),('subject_match',-1),('people_count',1.2),('eligible','true'),('reason','')]:
            r=self.result();r[key]=value
            with self.assertRaises(ValueError):oa.validate_result(r)
        r=self.result();r['filename']='../bad'
        with self.assertRaises(ValueError):oa.validate_result(r)
    def test_profile_isolation_and_minimization(self):
        p={'organization_id':'org-a','version':'1','status':'candidate','secret':'do not transmit','candidate_preferences':[{'rule':'theme','limit':'uncertain','evidence':['customer.jpg']} ]}
        r=oa.rules_from_profile(p,'org-a');self.assertNotIn('customer.jpg',json.dumps(r));self.assertNotIn('secret',json.dumps(r))
        with self.assertRaises(ValueError):oa.rules_from_profile(p,'org-b')
    def test_inference_protocol(self):
        response={'done':True,'message':{'content':json.dumps(self.result())}}
        with patch.object(oa,'request_json',return_value=response) as req:
            result,_=oa.assess_one('http://localhost','vision:test','encoded')
            self.assertTrue(result['eligible']);body=req.call_args.args[2]
            self.assertFalse(body['stream']);self.assertFalse(body['think']);self.assertEqual(body['messages'][1]['images'],['encoded'])
        response['done']=False
        with patch.object(oa,'request_json',return_value=response):
            with self.assertRaises(ValueError):oa.assess_one('http://localhost','v','e')
    def test_empty_and_truncated_inference(self):
        for response in [{'done':True,'message':{'content':''}}, {'done':True,'done_reason':'length','message':{'content':json.dumps(self.result())}}]:
            with patch.object(oa,'request_json',return_value=response):
                with self.assertRaises(ValueError):oa.assess_one('http://localhost','v','e')
    def test_no_redirect(self):
        self.assertIsNone(oa.NoRedirect().redirect_request(None,None,302,'',{},'http://elsewhere'))
    def test_subject_priority(self):
        r=self.result();r['subject_match']=0
        self.assertLess(oa.assessment_row('x','portrait',r,'child')['score'],30)
    def test_read_only_run_and_replay(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);self.inputs(root);a=self.args(root)
            before={p.name:oa.digest(p) for p in (root/'source').iterdir()}
            with patch.object(oa,'preflight',return_value=self.info()),patch.object(oa,'assess_one',return_value=(self.result(),{})) as call:
                report=oa.run(a);self.assertEqual(len(report['completed']),2)
                a.resume=True;oa.run(a);self.assertEqual(call.call_count,2)
            self.assertEqual(before,{p.name:oa.digest(p) for p in (root/'source').iterdir()})
            a.resume=False
            with patch.object(oa,'preflight',return_value=self.info()):
                with self.assertRaises(FileExistsError):oa.run(a)
    def test_interruption_resume_and_changed_context(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);self.inputs(root);a=self.args(root)
            with patch.object(oa,'preflight',return_value=self.info()),patch.object(oa,'assess_one',side_effect=[(self.result(),{}),TimeoutError('timeout')]):
                with self.assertRaises(TimeoutError):oa.run(a)
            report=json.loads((root/'out/model-review.json').read_text());self.assertEqual(report['status'],'blocked');self.assertEqual(len(report['completed']),1)
            self.assertFalse((root/'out/assessments.json').exists())
            a.resume=True;a.focus='changed'
            with patch.object(oa,'preflight',return_value=self.info()):
                with self.assertRaises(ValueError):oa.run(a)
            a.focus=''
            with patch.object(oa,'preflight',return_value=self.info()),patch.object(oa,'assess_one',return_value=(self.result(),{})) as call:
                oa.run(a);self.assertEqual(call.call_count,1)
    def test_changed_source_during_inference(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);self.inputs(root);a=self.args(root)
            def inference(*args):
                (root/'source/0.jpg').write_bytes(b'changed');return self.result(),{}
            with patch.object(oa,'preflight',return_value=self.info()),patch.object(oa,'assess_one',side_effect=inference):
                with self.assertRaises(ValueError):oa.run(a)
            self.assertFalse((root/'out/assessments.json').exists())
    def test_budget_output_and_decode(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);self.inputs(root);a=self.args(root);a.max_images=1
            with self.assertRaises(ValueError):oa.run(a)
            a.max_images=5;a.output=str(root/'source/out')
            with self.assertRaises(ValueError):oa.run(a)
    def test_eligibility_filter(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);self.inputs(root);a=self.args(root);r=self.result();r['eligible']=False
            with patch.object(oa,'preflight',return_value=self.info()),patch.object(oa,'assess_one',return_value=(r,{})):
                oa.run(a)
            self.assertEqual(json.loads((root/'out/assessments.json').read_text()),[])

if __name__=='__main__':unittest.main()
