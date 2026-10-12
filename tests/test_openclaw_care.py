import json
import tempfile
import unittest
from pathlib import Path
from collector.openclaw_care import CareBudget, registry, support_dir

SID='openclaw:main:11111111-1111-4111-8111-111111111111'
class CareTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.home=Path(self.tmp.name);self.root=support_dir(self.home);self.root.mkdir(parents=True)
 def test_authorization_must_be_explicit_selected_subset(self):
  p=self.root/'selected-openclaw-sessions.json';p.write_text(json.dumps({'version':1,'ids':[SID],'care_ids':[SID,'openclaw:main:22222222-2222-4222-8222-222222222222']}))
  selected,care=registry(self.home);self.assertEqual(selected,{SID});self.assertEqual(care,{SID})
  p.write_text(json.dumps({'version':1,'ids':[SID]}));self.assertFalse(registry(self.home)[1])
 def test_three_automatic_requests_survive_restart_then_cool_down(self):
  b=CareBudget(self.home)
  for t in [100,200,300]:b.reserve(SID,t)
  reloaded=CareBudget(self.home);self.assertEqual(reloaded.retry_at(SID,301),3700)
  with self.assertRaises(ValueError):reloaded.reserve(SID,301)
  self.assertEqual(reloaded.retry_at(SID,3700),0);reloaded.reserve(SID,3700)
  raw=b.path.read_text();self.assertNotIn(SID,raw);self.assertNotIn('11111111',raw)
 def test_corrupt_budget_fails_closed_without_overwrite(self):
  b=CareBudget(self.home);b.path.write_text('{bad');original=b.path.read_bytes()
  with self.assertRaises(ValueError):b.retry_at(SID,100)
  with self.assertRaises(ValueError):b.reserve(SID,100)
  self.assertEqual(b.path.read_bytes(),original)
 def test_redirect_never_reads_or_replaces_external_file(self):
  outside=self.home/'outside.json';outside.write_text(json.dumps({'version':1,'ids':[SID],'care_ids':[SID]}))
  (self.root/'selected-openclaw-sessions.json').symlink_to(outside);self.assertEqual(registry(self.home),(set(),set()))
  b=CareBudget(self.home);b.path.symlink_to(outside)
  with self.assertRaises(ValueError):b.reserve(SID,100)
  self.assertTrue(b.path.is_symlink());self.assertIn('care_ids',outside.read_text())
 def test_invalid_or_oversize_registry_never_authorizes(self):
  p=self.root/'selected-openclaw-sessions.json';p.write_text('x'*65537);self.assertFalse(registry(self.home)[1])
  p.write_text(json.dumps({'version':2,'ids':[SID],'care_ids':[SID]}));self.assertFalse(registry(self.home)[1])
 def test_budget_keeps_one_session_separate_from_another(self):
  b=CareBudget(self.home)
  for t in [100,200,300]:b.reserve(SID,t)
  self.assertEqual(b.retry_at('openclaw:other:22222222-2222-4222-8222-222222222222',301),0)
 def test_invalid_timestamp_values_are_not_treated_as_zero_usage(self):
  b=CareBudget(self.home)
  for value in [True,-1,float('nan'),float('inf'),'100']:
   b.path.write_text(json.dumps({b.key(SID):[value]}))
   with self.assertRaises(ValueError):b.retry_at(SID,200)
