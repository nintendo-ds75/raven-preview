"""A model pass reads a node on a scratch twin and removes the twin after.

Dry run 3 on the Parcelwise demo: a task_context_reads row (a foreign key to
decisions) pointed at the twin, the DELETE tripped it, the whole cleanup
rolled back, and the live node stayed marked as being read while the twin
sat on the task page as a duplicate question. The cleanup now moves what
points at the twin onto the live node, removes the rest, and never leaves the
live node stuck; open repairs a database already in that state.
"""
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from fixtures import OfflineCase
from bridge import canvas, graph as graph_mod, ladder, visibility
from bridge.config import Config
from bridge.graph import ScratchCleanupFailed, decision_references, repair_model_pass
from bridge.store import Store


REPO = 'scratch/policy'
QUESTION = 'How long should policy audit records remain?'
ANSWER = 'Keep policy audit records for thirty days.'


def ago(seconds):
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


class ScratchCleanupTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.path = None
        self.store = self.fresh_store('scratch-cleanup.db')
        self.path = self.store.path
        self.graph = self.store.graph
        self.addCleanup(self.graph.close)
        self.store.add_owner({'name': 'Policy Owner', 'team': 'Policy', 'patterns': 'policy/*'})
        for method in ('complete', 'complete_json'):
            guard = patch('bridge.llm.Client.' + method, side_effect=AssertionError('Provider calls forbidden'))
            guard.start()
            self.addCleanup(guard.stop)

    def node(self, question=QUESTION):
        task = self.store.add_run({'title': 'Review audit retention', 'repo': REPO})['id']
        did = self.graph.add_decision(task, question, 'policy', 'pending', repo=REPO,
                                      owner='Policy Owner', path='policy/retention.py')
        return task, did

    def source(self):
        _, did = self.node()
        self.store.answer(did, {'answer': ANSWER, 'signed_by': 'Policy Owner'})
        return did

    def events(self, kind):
        return [json.loads(r['detail']) for r in self.graph.db.execute(
            'SELECT detail FROM events WHERE kind=? ORDER BY id', (kind,))]

    def model_pass(self, did, during=None):
        """Run the pass on `did`; `during(scratch_id)` runs after the
        ladder has made the twin and before the cleanup."""
        self.graph.db.execute('UPDATE decisions SET model_pending=1 WHERE id=?', (did,))
        current = self.store.get_decision(did)
        original = ladder.ask
        seen = {}

        def read(*args, **kwargs):
            result = original(*args, **kwargs)
            seen['scratch'] = result['drafts'][0]
            row = self.graph.db.execute('SELECT draft, client_ref, scratch_of FROM decisions WHERE id=?',
                                        (seen['scratch'],)).fetchone()
            self.assertEqual((row['draft'], row['client_ref'], row['scratch_of']), (1, '__model_pass__', did))
            if during:
                during(seen['scratch'])
            return result

        with patch('bridge.ladder.ask', side_effect=read):
            canvas._model_node_pass(self.store, Config(model_api='none'), did, '', ['policy/retention.py'],
                                    [], '', current['owner_id'], '', current['updated_at'])
        return seen['scratch']

    def reference(self, scratch, task, did):
        """What the dry run left pointing at the twin: a task that read it
        (the foreign key that tripped), a link that put it on the tree as a
        related question, a feedback row of the routing memory."""
        db = self.graph.db
        db.execute('INSERT INTO task_context_reads VALUES(?,?,?,?)', (task, scratch, 'rev-scratch', ago(1)))
        _, other = self.node('Should the retention window be configurable?')
        db.execute("INSERT INTO decision_links(decision_id, related_id, kind, note, created_at) VALUES(?,?,?,?,?)",
                   (other, scratch, 'related', 'the same question in another scope', ago(1)))
        db.execute("INSERT INTO routing_feedback(decision_id, person_id, repo, question, outcome, updated_at) "
                   "VALUES(?,?,?,?,?,?)", (scratch, 'person-1', REPO, QUESTION, 'answered', ago(1)))
        return other

    def test_references_to_the_twin_move_to_the_live_node_and_the_twin_goes(self):
        self.source()
        task, did = self.node()
        holder = {}
        scratch = self.model_pass(did, during=lambda sid: holder.update(other=self.reference(sid, task, did)))
        db = self.graph.db
        self.assertIsNone(db.execute('SELECT 1 FROM decisions WHERE id=?', (scratch,)).fetchone())
        self.assertEqual(db.execute('SELECT count(*) FROM decisions WHERE draft=1').fetchone()[0], 0)
        live = self.store.get_decision(did)
        self.assertFalse(live['model_pending'])
        self.assertEqual(live['source'], 'memory')
        self.assertTrue(any(e['kind'] == 'model_read' for e in live['events']))
        self.assertEqual(self.events('model_pass_cleanup_failed'), [])
        reads = [dict(r) for r in db.execute('SELECT * FROM task_context_reads WHERE task_id=?', (task,))]
        self.assertEqual([r['decision_id'] for r in reads if r['revision'] == 'rev-scratch'], [did])
        self.assertEqual(db.execute('SELECT related_id FROM decision_links WHERE decision_id=? AND kind=?',
                                    (holder['other'], 'related')).fetchone()['related_id'], did)
        self.assertEqual(db.execute('SELECT decision_id FROM routing_feedback WHERE person_id=?',
                                    ('person-1',)).fetchone()['decision_id'], did)
        for table, column, _ in decision_references(db):
            self.assertIsNone(db.execute(f'SELECT 1 FROM {table} WHERE {column}=?', (scratch,)).fetchone(), table)
        self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])
        self.assertNotIn(scratch, json.dumps(canvas.get_tree(self.store, task), default=str))

    def test_a_row_the_live_node_already_has_is_dropped_not_duplicated(self):
        self.source()
        task, did = self.node()

        def collide(sid):
            self.graph.db.execute('INSERT INTO task_context_reads VALUES(?,?,?,?)', (task, sid, 'same-rev', ago(1)))
            self.graph.db.execute('INSERT INTO task_context_reads VALUES(?,?,?,?)', (task, did, 'same-rev', ago(1)))

        scratch = self.model_pass(did, during=collide)
        rows = self.graph.db.execute("SELECT decision_id FROM task_context_reads WHERE revision='same-rev'").fetchall()
        self.assertEqual([r['decision_id'] for r in rows], [did])
        self.assertIsNone(self.graph.db.execute('SELECT 1 FROM decisions WHERE id=?', (scratch,)).fetchone())
        self.assertFalse(self.store.get_decision(did)['model_pending'])

    def test_a_failed_cleanup_hides_the_twin_and_still_clears_the_live_node(self):
        self.source()
        task, did = self.node()
        failure = ScratchCleanupFailed('task_context_reads', sqlite3.IntegrityError('FOREIGN KEY constraint failed'))
        with patch('bridge.canvas.discard_decision', side_effect=failure):
            scratch = self.model_pass(did)
        live = self.store.get_decision(did)
        self.assertFalse(live['model_pending'])
        self.assertEqual(live['source'], 'memory')
        failed = self.events('model_pass_cleanup_failed')
        self.assertEqual(len(failed), 1)
        self.assertEqual((failed[0]['decision_id'], failed[0]['scratch_id'], failed[0]['table'], failed[0]['error']),
                         (did, scratch, 'task_context_reads', 'IntegrityError'))
        twin = self.graph.db.execute('SELECT status, draft, model_pending FROM decisions WHERE id=?', (scratch,)).fetchone()
        self.assertEqual((twin['status'], twin['draft'], twin['model_pending']), ('withdrawn', 1, 0))
        # No page shows it, and no page read trips over it.
        self.assertNotIn(scratch, json.dumps(canvas.get_tree(self.store, task), default=str))
        self.assertNotIn(scratch, json.dumps(self.store.inbox(), default=str))
        self.assertEqual(self.graph.expire_drafts(seconds=0), [])
        self.assertIsNotNone(self.graph.db.execute('SELECT 1 FROM decisions WHERE id=?', (scratch,)).fetchone())
        # The next open removes it for real, and says so once.
        with self.graph.transaction():
            done = repair_model_pass(self.graph.db, stale=0)
        self.assertEqual(done['scratch'], [scratch])
        self.assertIsNone(self.graph.db.execute('SELECT 1 FROM decisions WHERE id=?', (scratch,)).fetchone())
        self.assertEqual(len(self.events('model_pass_cleanup_failed')), 1)
        self.assertEqual(self.events('scratch_removed')[0]['decision_id'], did)

    def test_a_read_of_the_twin_is_recorded_against_the_live_node(self):
        self.source()
        task, did = self.node()

        def read_the_twin(sid):
            visibility.remember_reads(self.store, task, {'nodes': [{'id': sid, 'question': QUESTION}]})
            rows = self.graph.db.execute('SELECT decision_id FROM task_context_reads WHERE task_id=?', (task,)).fetchall()
            self.assertEqual({r['decision_id'] for r in rows}, {did})

        self.model_pass(did, during=read_the_twin)
        self.assertFalse(self.store.get_decision(did)['model_pending'])

    def orphan(self, task, did, age=7200, adopted=True):
        """A twin the pass left behind, with a read recorded against it."""
        sid = self.graph.add_decision(task, QUESTION, 'policy', 'pending', repo=REPO, owner='Policy Owner',
                                      path='policy/retention.py', draft=True)
        self.graph.db.execute("UPDATE decisions SET client_ref='__model_pass__', scratch_of=?, created_at=? WHERE id=?",
                              (did, ago(age), sid))
        self.graph.db.execute('INSERT INTO task_context_reads VALUES(?,?,?,?)', (task, sid, 'rev-orphan', ago(age)))
        if adopted:
            self.graph.append_event('model_read', {'task_id': task, 'decision_id': did})
        return sid

    def test_the_draft_sweep_removes_a_referenced_twin_without_raising(self):
        task, did = self.node()
        sid = self.orphan(task, did)
        self.assertEqual(self.graph.expire_drafts(), [])
        self.assertIsNone(self.graph.db.execute('SELECT 1 FROM decisions WHERE id=?', (sid,)).fetchone())
        self.assertEqual(self.graph.db.execute("SELECT decision_id FROM task_context_reads WHERE revision='rev-orphan'")
                         .fetchone()['decision_id'], did)
        self.assertEqual(self.graph.db.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_open_repairs_a_stuck_node_and_an_orphan_twin(self):
        task, did = self.node()
        sid = self.orphan(task, did)
        unadopted_task, unadopted = self.node()
        lost = self.orphan(unadopted_task, unadopted, adopted=False)
        self.graph.db.execute('UPDATE decisions SET model_pending=1, updated_at=? WHERE id=?', (ago(3600), did))
        _, fresh = self.node()
        self.graph.db.execute('UPDATE decisions SET model_pending=1 WHERE id=?', (fresh,))
        self.graph.close()
        reopened = Store(self.path)
        self.addCleanup(reopened.graph.close)
        db = reopened.graph.db
        self.assertEqual(db.execute('SELECT model_pending FROM decisions WHERE id=?', (did,)).fetchone()[0], 0)
        # A read in flight is not stuck: its mark stays.
        self.assertEqual(db.execute('SELECT model_pending FROM decisions WHERE id=?', (fresh,)).fetchone()[0], 1)
        for twin in (sid, lost):
            self.assertIsNone(db.execute('SELECT 1 FROM decisions WHERE id=?', (twin,)).fetchone())
        reads = {r['task_id']: r['decision_id'] for r in db.execute(
            "SELECT task_id, decision_id FROM task_context_reads WHERE revision='rev-orphan'")}
        # Moved to the live node where the pass published there; gone where it did not.
        self.assertEqual(reads, {task: did})
        events = [json.loads(r['detail']) | {'kind': r['kind']} for r in db.execute(
            "SELECT kind, detail FROM events WHERE kind IN ('scratch_removed', 'model_pending_repaired') ORDER BY id")]
        self.assertEqual({(e['kind'], e.get('scratch_id', e['decision_id'])) for e in events},
                         {('scratch_removed', sid), ('scratch_removed', lost), ('model_pending_repaired', did)})
        self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_every_foreign_key_to_decisions_is_found_from_the_schema(self):
        refs = {(table, column): rule for table, column, rule in decision_references(self.graph.db)}
        self.assertEqual(refs[('task_context_reads', 'decision_id')], 'NO ACTION')
        self.assertEqual(refs[('decision_versions', 'decision_id')], 'CASCADE')
        self.assertEqual(refs[('provider_calls', 'decision_id')], 'NO ACTION')
        for soft in (('events', 'decision_id'), ('decision_links', 'decision_id'), ('decision_links', 'related_id'),
                     ('contact_observations', 'decision_id'), ('routing_feedback', 'decision_id')):
            self.assertIn(soft, refs)
        self.assertNotIn(('decisions', 'source_id'), refs)
        # A table added later is swept without anyone listing it.
        self.graph.db.execute('CREATE TABLE later_notes (decision_id TEXT NOT NULL REFERENCES decisions(id), body TEXT)')
        self.assertIn(('later_notes', 'decision_id'), {(t, c) for t, c, _ in decision_references(self.graph.db)})
        self.assertIs(graph_mod.MODEL_PENDING_STALE, canvas.MODEL_PENDING_STALE)
