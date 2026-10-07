"""Reject contaminated fixtures, rewritten observations and post-hoc scoring."""
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
import unittest

from evals.audit.fixture import attest_patch, freeze, git, validate_records, verify_fixture
from evals.audit.ledger import Ledger, TaskObserver, digest, redact, verify
from evals.audit.scoring import pin_rubric, score, score_run, validate_rubric
from evals.audit.viewer import render


CUTOFF = '2025-10-06T00:00:00Z'
BRIEF = 'Handle fractional delays without changing which operations may retry.'


def provenance():
    return {'kind': 'blinded_reconstruction', 'reviewer': 'evaluation-reviewer',
            'reviewed_brief_sha256': hashlib.sha256(BRIEF.encode()).hexdigest(),
            'no_solution': True, 'no_expected_people': True, 'no_future_references': True}


def record():
    return {'id': 'POL-1', 'provider': 'jira', 'namespace': 'test', 'body': 'Ask the policy team.',
            'version': '1', 'created_at': '2024-01-01T00:00:00Z', 'updated_at': '2024-02-01T00:00:00Z',
            'snapshot_at': '2024-02-01T01:00:00Z', 'provenance': 'historical-fixture', 'acl': ['test-team']}


def rubric():
    return {'reviewer': 'blinded-reviewer', 'decisions': [
        {'id': 'format', 'intent': 'Which delay formats are supported?', 'acceptable_contacts': [
            {'person_id': 'person-a', 'role': 'knowledgeable', 'evidence': 'prior review policy', 'evidence_at': '2024-01-01T00:00:00Z'},
            {'person_id': 'person-b', 'role': 'referrer', 'evidence': 'listed team member', 'evidence_at': '2024-01-01T00:00:00Z'}]},
        {'id': 'scope', 'intent': 'Does this apply outside staging?', 'acceptable_contacts': []}]}


class FrozenReplayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.repo = self.root / 'source'
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        git(self.repo, 'config', 'user.name', 'Fixture Engineer')
        git(self.repo, 'config', 'user.email', 'fixture@example.invalid')
        (self.repo / 'policy.txt').write_text('old policy\n')
        self.base = self.commit('baseline', '2024-01-01T00:00:00Z')
        (self.repo / 'solution.txt').write_text('future answer and person')
        self.future = self.commit('future', '2026-01-01T00:00:00Z')
        git(self.repo, 'tag', 'future-tag')
        self.out = self.root / 'sealed'

    def commit(self, message, date):
        git(self.repo, 'add', '.')
        git(self.repo, '-c', 'core.hooksPath=/dev/null', 'commit', '-qm', message,
            env={**os.environ, 'GIT_AUTHOR_DATE': date, 'GIT_COMMITTER_DATE': date})
        return git(self.repo, 'rev-parse', 'HEAD')

    def freeze(self, **kwargs):
        return freeze(self.repo, self.base, CUTOFF, self.out, BRIEF, records=[record()],
                      brief_provenance=kwargs.get('provenance', provenance()))

    def test_future_refs_objects_files_and_original_fetch_location_are_absent(self):
        manifest = self.freeze()
        self.assertEqual(verify_fixture(self.out), manifest)
        checkout = self.out / 'checkout'
        self.assertFalse((checkout / 'solution.txt').exists())
        self.assertEqual(git(checkout, 'tag'), '')
        self.assertEqual(git(checkout, 'remote'), '')
        self.assertFalse((checkout / '.git/FETCH_HEAD').exists())
        for oid in (self.future, git(self.repo, 'rev-parse', self.future + ':solution.txt')):
            self.assertNotEqual(subprocess.run(['git','-C',str(checkout),'cat-file','-e',oid], capture_output=True).returncode, 0)
        self.assertEqual(manifest['history_count'], 1)

    def test_future_commit_with_old_tip_timestamp_is_rejected(self):
        (self.repo / 'tip.txt').write_text('clock skew')
        tip = self.commit('old-looking tip', '2024-03-01T00:00:00Z')
        with self.assertRaisesRegex(ValueError, 'reachable commit'):
            freeze(self.repo, tip, CUTOFF, self.out, BRIEF, brief_provenance=provenance())

    def test_brief_review_is_bound_to_exact_text(self):
        p = provenance(); p['reviewed_brief_sha256'] = '0' * 64
        with self.assertRaisesRegex(ValueError, 'exact brief'):
            self.freeze(provenance=p)

    def test_unreviewed_solution_leakage_is_not_claimed_safe(self):
        p = provenance();p['no_solution'] = False
        with self.assertRaisesRegex(ValueError, 'explicitly assess'):
            self.freeze(provenance=p)

    def test_snapshot_tampering_is_rejected(self):
        self.freeze()
        (self.out / 'brief.txt').write_text(BRIEF + ' Use the held-out answer.')
        with self.assertRaisesRegex(ValueError, 'brief changed'):
            verify_fixture(self.out)

    def test_checkout_tampering_is_rejected(self):
        self.freeze()
        (self.out / 'checkout/policy.txt').write_text('future policy')
        with self.assertRaisesRegex(ValueError, 'dirty'):
            verify_fixture(self.out)

    def test_future_object_injection_is_rejected(self):
        self.freeze()
        git(self.out / 'checkout', 'fetch', '--no-tags', str(self.repo), self.future)
        with self.assertRaisesRegex(ValueError, 'outside its baseline'):
            verify_fixture(self.out)

    def test_today_edited_issue_cannot_be_backdated_by_creation_time(self):
        r = record();r['updated_at'] = r['snapshot_at'] = '2026-01-01T00:00:00Z'
        with self.assertRaisesRegex(ValueError, 'pre-cutoff'):
            validate_records([r], CUTOFF)

    def test_naive_timestamp_and_unknown_acl_are_rejected(self):
        for key, value in [('snapshot_at', '2024-01-01'), ('acl', [])]:
            r = record();r[key] = value
            with self.assertRaises(ValueError): validate_records([r], CUTOFF)

    def test_patch_check_does_not_repair_or_accept_untracked_work(self):
        self.freeze(); repo = self.out / 'checkout'
        (repo / 'policy.txt').write_text('revised policy\n')
        patch = subprocess.check_output(['git','-C',str(repo),'diff',self.base])
        self.assertTrue(attest_patch(repo, self.base, patch)['pass'])
        self.assertFalse(attest_patch(repo, self.base, patch.replace(b'-old policy',b'-wrong policy'))['pass'])
        (repo / 'new.py').write_text('unsubmitted test')
        self.assertFalse(attest_patch(repo, self.base, patch)['pass'])
        self.assertEqual(git(repo, 'diff', '--cached'), '')

    def test_overwrite_and_nested_destination_refused(self):
        self.freeze()
        with self.assertRaises(ValueError):self.freeze()
        with self.assertRaises(ValueError):freeze(self.repo,self.base,CUTOFF,self.repo/'nested',BRIEF,brief_provenance=provenance())


class AuditLedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'events.jsonl';self.ledger = Ledger(self.path)

    def test_redacts_nested_json_and_tokens_without_losing_question(self):
        secret = 'test-private-credential'
        ledger = Ledger(self.path, secrets=[secret])
        ledger.append('mcp_response', {'content': [{'text': json.dumps({'question':'Ask which format?', 'token':secret})}], 'header': 'Bearer '+secret})
        text = self.path.read_text()
        self.assertNotIn(secret,text);self.assertIn('Ask which format?',text)
        self.assertEqual(self.path.stat().st_mode & 0o777,0o600)

    def test_common_token_forms_are_redacted(self):
        for prefix in ('sk-ant-svc-','ghp_','rvn_','xoxb-'):
            token=prefix+'a'*32
            self.assertNotIn(token,str(redact({'message':token})))

    def test_concurrent_writers_keep_sequence_and_chain(self):
        with ThreadPoolExecutor(max_workers=5) as pool:
            list(pool.map(lambda n:self.ledger.append('step',{'n':n}),range(30)))
        rows=self.ledger.read();self.assertEqual(len(rows),30)
        self.assertEqual({r['data']['n'] for r in rows},set(range(30)))

    def test_edits_and_tail_removal_detected_against_retained_head(self):
        self.ledger.append('start',{});head=self.ledger.append('end',{})['hash']
        rows=self.ledger.read(head)
        with self.assertRaises(ValueError):verify(rows[:1],head)
        rows[0]['data']={'edited':True}
        with self.assertRaises(ValueError):verify(rows)

    def test_does_not_append_after_corruption(self):
        self.ledger.append('start',{})
        self.path.write_text(self.path.read_text()+'{partial')
        before=self.path.read_bytes()
        with self.assertRaises(ValueError):self.ledger.append('end',{})
        self.assertEqual(before,self.path.read_bytes())

    def test_viewer_treats_record_content_as_data(self):
        self.ledger.append('source',{'body':'</script><script>alert(1)</script>'})
        out=Path(self.tmp.name)/'view.html';render(self.path,out)
        self.assertNotIn('</script><script>alert',out.read_text())
        self.assertIn('\\u003c/script>',out.read_text())

    def test_rubric_frozen_before_host_and_cannot_change(self):
        r=rubric();pin_rubric(self.ledger,r,CUTOFF)
        event=self.ledger.append('mcp_response',{'question':'formats?'})
        observations=[{'decision_id':'format','discovered':True,'first_contact':'person-b',
                       'judge':'reviewer-b','evidence_events':[event['sequence']],
                       'question_quality':{'readable':'pass'}}]
        result=score_run(self.ledger,r,observations)
        self.assertEqual(result['first_contact_pass'],1)
        self.assertEqual(result['rows'][0]['authorization'],'unknown')
        self.assertEqual(result['unobserved_decisions'],['scope'])
        self.assertEqual(result['rows'][0]['question_quality']['neutral'],'unknown')
        r['decisions'][0]['intent']='post-hoc edit'
        with self.assertRaisesRegex(ValueError,'differs'):score_run(self.ledger,r,observations)
        with self.assertRaises(ValueError):pin_rubric(self.ledger,r,CUTOFF)

    def test_future_contact_evidence_cannot_label_a_past_route(self):
        r=rubric();r['decisions'][0]['acceptable_contacts'][0]['evidence_at']='2026-01-01T00:00:00Z'
        with self.assertRaises(ValueError):validate_rubric(r,CUTOFF)

    def test_explicit_no_decision_rubric_is_distinct_from_missing_labels(self):
        r={'reviewer':'reviewer-a','decisions':[]}
        pin_rubric(self.ledger,r,CUTOFF)
        result=score_run(self.ledger,r,[])
        self.assertEqual(result['expected'],0)
        self.assertEqual(result['first_contact_scored'],0)
        self.assertEqual(result['unobserved_decisions'],[])
        with self.assertRaises(ValueError):
            validate_rubric({'reviewer':'reviewer-a'},CUTOFF)

    def test_quality_without_judge_or_trace_and_invalid_authority_rejected(self):
        for addition in ({'question_quality':{'neutral':'pass'}},{'authorization':'probably'}):
            with self.assertRaises(ValueError):
                score(rubric(),[{'decision_id':'format','discovered':True,**addition}])

    def test_rubric_cannot_be_pinned_after_a_host_call(self):
        self.ledger.append('mcp_request',{})
        with self.assertRaises(ValueError):pin_rubric(self.ledger,rubric(),CUTOFF)

    def test_observer_records_changed_state_and_internal_events_once(self):
        observer=TaskObserver(self.ledger)
        tree={'task_id':'t','nodes':[{'node_id':'d','question':'What format?','authorized':False}], 'observed_at':'first'}
        trace={'task_id':'t','events':[{'id':1,'kind':'ask_drafted','at':'original-time'}]}
        observer.observe(tree,trace)
        tree['observed_at']='later'
        observer.observe(tree,trace)
        self.assertEqual(len(self.ledger.read()),2)
        tree['nodes'][0]['authorized']=True
        trace['events'].append({'id':2,'kind':'signature'})
        observer.observe(tree,trace)
        rows=self.ledger.read();self.assertEqual(len(rows),4)
        self.assertFalse(rows[1]['data']['nodes'][0]['authorized'])
        self.assertTrue(rows[3]['data']['nodes'][0]['authorized'])


if __name__ == '__main__':unittest.main()
