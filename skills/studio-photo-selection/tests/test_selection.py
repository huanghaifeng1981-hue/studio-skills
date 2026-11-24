import unittest
import tempfile
import subprocess
import json
from unittest.mock import patch
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from selection_tool import Assessment, SelectionTool, digest

class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        for i in range(5): (self.root / f'{i}.jpg').write_bytes(bytes([i]) * 50)
        self.tool = SelectionTool(source=self.root, organization_id='org-a')
        self.rows = [Assessment(f'{i}.jpg', i, 'reviewed', 'portrait' if i%2 else 'landscape') for i in range(5)]
    def tearDown(self): self.tmp.cleanup()
    def plan(self, **kw): return self.tool.execute(assessments=self.rows, count=2, **kw)
    def test_best_and_worst(self):
        self.assertEqual([r['filename'] for r in self.plan()['selected']], ['4.jpg','3.jpg'])
        self.assertEqual([r['filename'] for r in self.plan(mode='worst')['selected']], ['0.jpg','1.jpg'])
    def test_quotas(self):
        p=self.plan(minimum_portrait=2)
        self.assertEqual({r['filename'] for r in p['selected']},{'1.jpg','3.jpg'})
    def test_groups(self):
        rows=[Assessment('3.jpg',3,'good',group='same'),Assessment('4.jpg',4,'good',group='same')]
        with self.assertRaises(ValueError): self.tool.execute(assessments=rows,count=2)
    def test_exact_duplicate_with_different_groups(self):
        (self.root/'1.jpg').write_bytes((self.root/'0.jpg').read_bytes())
        rows=[Assessment('0.jpg',1,'good',group='a'),Assessment('1.jpg',2,'good',group='b')]
        with self.assertRaises(ValueError): self.tool.execute(assessments=rows,count=2)
    def test_insufficient(self):
        with self.assertRaises(ValueError): self.tool.execute(assessments=self.rows,count=6)
    def test_missing_asset(self):
        with self.assertRaises(ValueError): self.tool.execute(assessments=[Assessment('missing.jpg',1,'review')],count=1)
    def test_organization(self):
        with self.assertRaises(ValueError): self.plan(organization_id='org-b')
        with self.assertRaises(ValueError): self.plan(profile={'organization_id':'org-b'})
    def test_read_only_plan(self):
        before={p.name:digest(p) for p in self.root.iterdir()}
        self.plan()
        self.assertEqual(before,{p.name:digest(p) for p in self.root.iterdir()})
    def test_copy_empty_existing(self):
        p=self.plan(); (self.root/'123').mkdir(); result=self.tool.copy(p,folder='123')
        self.assertEqual(result['count'],2)
        for r in p['selected']:
            self.assertEqual(digest(self.root/r['filename']),r['sha256'])
            self.assertEqual(digest(self.root/'123'/r['filename']),r['sha256'])
    def test_existing_target_preserved(self):
        p=self.plan(); out=self.root/'123';out.mkdir(); (out/'x').write_text('keep')
        with self.assertRaises(ValueError): self.tool.copy(p,folder='123')
        self.assertEqual((out/'x').read_text(),'keep')
    def test_source_changed(self):
        p=self.plan();(self.root/'4.jpg').write_bytes(b'new')
        with self.assertRaises(ValueError): self.tool.copy(p)
        self.assertFalse((self.root/'已选').exists())
    def test_paths(self):
        with self.assertRaises(ValueError): self.tool.copy(self.plan(),folder='../escape')
        with self.assertRaises(ValueError): self.tool.execute(assessments=[Assessment('../escape.jpg',1,'review')],count=1)
    def test_symlinks(self):
        (self.root/'link.jpg').symlink_to(self.root/'0.jpg')
        with self.assertRaises(ValueError): self.tool.execute(assessments=[Assessment('link.jpg',1,'review')],count=1)
        (self.root/'dest').symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValueError): self.tool.copy(self.plan(),folder='dest')

    def test_interrupted_copy_preserved(self):
        plan = self.plan()
        with patch('selection_tool.shutil.copyfileobj', side_effect=OSError('disk full')):
            with self.assertRaises(RuntimeError): self.tool.copy(plan)
        self.assertTrue((self.root/'已选').is_dir())
        for row in plan['selected']:
            self.assertEqual(digest(self.root/row['filename']), row['sha256'])
        with self.assertRaises(ValueError): self.tool.copy(plan)

    def test_standalone_cli(self):
        with tempfile.TemporaryDirectory() as task:
            task = Path(task)
            assessments = task/'assessments.json'
            assessments.write_text(json.dumps([dict(filename=r.filename, score=r.score,
                reason='人工复核', orientation=r.orientation) for r in self.rows]), encoding='utf-8')
            script = Path(__file__).resolve().parents[1]/'scripts/run_selection.py'
            args = [sys.executable, str(script), '--source', str(self.root),
                    '--organization', 'org-a', '--count', '2', '--assessments', str(assessments)]
            output = task/'计划.json'
            result = subprocess.run(args+['--output', str(output), '--copy'], cwd=task, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(output.read_text(encoding='utf-8'))['count'], 2)
            self.assertEqual(len(list((self.root/'已选').iterdir())), 2)
            result = subprocess.run(args+['--output', str(output)], cwd=task, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            result = subprocess.run(args+['--output', str(self.root/'plan.json')], cwd=task, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((self.root/'plan.json').exists())

if __name__=='__main__': unittest.main()
