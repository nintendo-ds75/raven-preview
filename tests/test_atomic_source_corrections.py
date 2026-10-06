"""Human source revisions and their consumers share one writer boundary."""
import json
import threading
from pathlib import Path
from contextlib import contextmanager
from unittest.mock import patch

from fixtures import OfflineCase
from bridge import canvas, proof
from bridge.graph import ts_to_iso
from bridge.store import Invalid, Store


class AtomicSourceCorrectionTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.store = Store(Path(self.temp.name) / 'sources.db')
        self.graph = self.store.graph
        self.addCleanup(self.graph.close)
        self.store.add_owner({'name': 'Policy Owner', 'team': 'Policy', 'patterns': 'policy/*'})
        self.other = Store(self.store.path)
        self.addCleanup(self.other.graph.close)
        for method in ('complete', 'complete_json'):
            guard = patch('bridge.llm.Client.' + method, side_effect=AssertionError('Provider calls forbidden'))
            guard.start()
            self.addCleanup(guard.stop)

    def node(self, question='How long do we keep policy records?'):
        task = self.store.add_run({'title': question, 'repo': 'atomic/policy'})['id']
        node = self.graph.add_decision(task, question, 'policy', 'pending', repo='atomic/policy',
                                       owner='Policy Owner', path='policy/retention.py')
        return task, node

    def source(self):
        task, node = self.node()
        self.store.answer(node, {'answer': 'Keep records thirty days.'})
        return task, node

    def derived(self, source):
        task, node = self.node('What retention period should the archive use?')
        revision = self.store.get_decision(source)['updated_at']
        self.graph.update_decision(node, status='resolved', source='memory', source_id=source,
                                   source_revision=revision, answer='Keep records thirty days.', signoff='required')
        return task, node

    def sign(self, node, store=None, by='Policy Owner'):
        store = store or self.store
        current = store.get_decision(node)
        return canvas.sign_off(store, node, {'by': by, 'expected_updated_at': current['updated_at']})

    def correct(self, node, **extra):
        current = self.store.get_decision(node)
        return self.store.answer(node, {'answer': 'Keep records seven days.',
            'expected_updated_at': current['updated_at'], **extra})

    def test_correction_cannot_publish_before_dependent_invalidation(self):
        _, source = self.source()
        task, dependent = self.derived(source)
        old = self.other.get_decision(dependent)
        entered, release, attempted, finished = [threading.Event() for _ in range(4)]
        errors, accepted = [], []
        original = self.graph.transaction

        @contextmanager
        def pause():
            if threading.current_thread().name == 'source-writer' and not entered.is_set():
                entered.set()
                if not release.wait(10):
                    raise AssertionError('Invalidation barrier timed out')
            with original() as db:
                yield db

        def writer():
            try:
                self.correct(source)
            except BaseException as error:
                errors.append(error)
            finally:
                self.graph.close_thread()

        def signer():
            attempted.set()
            try:
                row = canvas.sign_off(self.other, dependent,
                    {'by': 'Policy Owner', 'expected_updated_at': old['updated_at']})
                accepted.append(row['authorized'])
                self.other.update_run(task, {'status': 'completed'})
                accepted.append('finished')
            except Invalid:
                pass
            except BaseException as error:
                errors.append(error)
            finally:
                finished.set()
                self.other.graph.close_thread()

        with patch.object(self.graph, 'transaction', pause):
            a = threading.Thread(target=writer, name='source-writer')
            a.start()
            self.assertTrue(entered.wait(10))
            b = threading.Thread(target=signer)
            b.start()
            self.assertTrue(attempted.wait(10))
            # On the old code the correction is committed, and signing and
            # finishing both succeed while dependent invalidation is paused.
            self.assertTrue(finished.wait(5))
            release.set()
            a.join(10)
            b.join(10)
        self.assertFalse(a.is_alive() or b.is_alive())
        self.assertEqual(errors, [])
        print('CURRENT_MAIN_HUMAN_SOURCE_RACE', json.dumps({'accepted': accepted}))
        self.assertEqual(accepted, [], 'Stale derived answer acquired authority after its source correction')

    def test_source_pin_is_checked_even_without_invalidation_flag(self):
        _, source = self.source()
        task, dependent = self.derived(source)
        # Model old databases/imports that missed invalidation, independently
        # of flags. No current source value is invented for their old pin.
        with self.other.connect() as db:
            db.execute("UPDATE decisions SET answer='Seven days', updated_at=? WHERE id=?",
                       ('2030-01-01T00:00:00+00:00', source))
        self.assertFalse(self.store.get_decision(dependent)['needs_review'])
        with self.assertRaisesRegex(Invalid, 'recorded revision'):
            self.sign(dependent)
        self.graph.update_decision(dependent, signoff='signed')
        for finish in (lambda: self.store.update_run(task, {'status': 'completed'}),
                       lambda: canvas.finish_task(self.store, {'task_id': task})):
            with self.assertRaisesRegex(Invalid, 'recorded revision'):
                finish()

    def test_missing_or_malformed_legacy_pin_is_not_backfilled_from_current_source(self):
        _, source = self.source()
        for revision in ('', 'unknown', '1700000000', '2026-01-01T00:00:00'):
            with self.subTest(revision=revision):
                task, node = self.derived(source)
                self.graph.db.execute('UPDATE decisions SET source_revision=? WHERE id=?', (revision, node))
                with self.assertRaisesRegex(Invalid, 'recorded revision'):
                    self.sign(node)
                self.graph.update_decision(node, signoff='signed')
                with self.assertRaisesRegex(Invalid, 'recorded revision'):
                    self.store.update_run(task, {'status': 'completed'})
                self.assertEqual(self.store.get_decision(node)['source_revision'], revision)

    def test_equivalent_timestamp_encodings_accept_the_same_legacy_revision(self):
        from datetime import datetime, timedelta, timezone
        _, source = self.source()
        task, dependent = self.derived(source)
        value = datetime.fromisoformat(self.store.get_decision(source)['updated_at'])
        for equivalent in (value.isoformat().replace('+00:00', 'Z'),
                           value.astimezone(timezone(timedelta(hours=5))).isoformat(),
                           ts_to_iso(value.timestamp())):
            self.graph.db.execute('UPDATE decisions SET source_revision=? WHERE id=?', (equivalent, dependent))
            self.assertTrue(self.sign(dependent)['authorized'])
        self.store.update_run(task, {'status': 'completed'})

    def test_mixed_dependencies_preserve_completed_signatures_and_reach_active_consumers(self):
        _, source = self.source()
        history_task, historical = self.derived(source)
        self.sign(historical)
        self.store.update_run(history_task, {'status': 'completed'})
        bundle = proof.create(self.store, history_task, 'diff --git a/a b/a\n+kept\n')
        before = self.store.get_decision(historical)
        _, child = self.node('What should the child archive show?')
        self.graph.update_decision(child, parent_id=historical)
        self.store.answer(child, {'answer': 'Show the period.'})
        task, explicit = self.node('What should the receipt say?')
        self.graph.add_link(explicit, child, 'depends')
        self.store.answer(explicit, {'answer': 'Say the recorded period.'})
        _, final = self.derived(explicit)
        self.sign(final)
        self.correct(source)
        past = self.store.get_decision(historical)
        for field in ('status', 'signoff', 'signatures', 'signed_by', 'signed_revision', 'signed_hash', 'answer'):
            self.assertEqual(past[field], before[field], field)
        self.assertFalse(past['authorized'])
        self.assertFalse(self.graph.get_decision(historical).authorized)
        exported = proof.export(self.store, {'task_id': history_task})
        self.assertEqual(exported['bundle'], bundle)
        self.assertTrue(exported['stale'])
        for node in (child, explicit, final):
            current = self.store.get_decision(node)
            self.assertTrue(current['needs_review'])
            self.assertFalse(current['authorized'])
            with self.assertRaisesRegex(Invalid, 'review|corrected'):
                self.sign(node)
        with self.assertRaises(Invalid):
            self.store.update_run(task, {'status': 'completed'})

    def test_parent_and_explicit_edges_cannot_hide_a_stale_pinned_ancestor(self):
        _, source = self.source()
        _, middle = self.derived(source)
        self.sign(middle)
        for kind in ('parent', 'depends'):
            task, leaf = self.node('What should the ' + kind + ' consumer do?')
            if kind == 'parent':
                self.graph.update_decision(leaf, parent_id=middle)
            else:
                self.graph.add_link(leaf, middle, 'depends')
            self.store.answer(leaf, {'answer': 'Use the prior period.'})
            with self.other.connect() as db:
                db.execute("UPDATE decisions SET updated_at='2031-01-01T00:00:00+00:00' WHERE id=?", (source,))
            with self.assertRaisesRegex(Invalid, 'recorded revision'):
                self.sign(leaf)
            with self.assertRaisesRegex(Invalid, 'recorded revision'):
                self.store.update_run(task, {'status': 'completed'})

    def test_correction_and_supersession_roll_back_with_invalidation_and_events(self):
        for mode in ('correction', 'supersession'):
            with self.subTest(mode=mode):
                _, source = self.source()
                _, dependent = self.derived(source)
                self.sign(dependent)
                _, replacement = self.node('What is the replacement retention policy?')
                originals = {node: self.store.get_decision(node) for node in (source, dependent, replacement)}
                original = self.graph.flag_dependents
                def fail(*args, **kwargs):
                    result = original(*args, **kwargs)
                    self.assertIn(dependent, result)
                    raise RuntimeError('Fail after dependent writes')
                with patch.object(self.graph, 'flag_dependents', fail), patch.object(self.store, 'notify') as notify:
                    with self.assertRaisesRegex(RuntimeError, 'dependent writes'):
                        if mode == 'correction':
                            self.correct(source)
                        else:
                            self.store.answer(replacement, {'answer': 'Seven days', 'supersedes': source})
                    notify.assert_not_called()
                for node, before in originals.items():
                    self.assertEqual(self.store.get_decision(node), before)

    def test_bound_answer_commit_defers_side_effects_and_rollback_discards_them(self):
        _, source = self.source()
        _, dependent = self.derived(source)
        before = self.store.get_decision(source)
        for rollback in (True, False):
            with self.subTest(rollback=rollback), patch.object(self.store, 'notify') as notify:
                try:
                    with self.graph.transaction() as db:
                        self.store.answer(source, {'answer': 'Seven days', 'expected_updated_at': before['updated_at']},
                                          transaction_db=db)
                        self.assertTrue(db.execute('SELECT needs_review FROM decisions WHERE id=?', (dependent,)).fetchone()[0])
                        notify.assert_not_called()
                        if rollback:
                            raise RuntimeError('Abort outer confirmation')
                except RuntimeError:
                    pass
                if rollback:
                    notify.assert_not_called()
                    self.assertEqual(self.store.get_decision(source), before)
                else:
                    self.assertTrue(notify.called)
                    self.assertEqual(self.store.get_decision(source)['answer'], 'Seven days')

    def test_bound_signoff_and_managed_revision_roll_back_together(self):
        _, source = self.source()
        _, dependent = self.derived(source)
        before = self.store.get_decision(dependent)
        with patch.object(self.graph, 'learn_from_answer') as learn:
            with self.assertRaisesRegex(RuntimeError, 'Abort'):
                with self.graph.transaction() as db:
                    self.sign(dependent)  # Nested caller need not repeat transaction_db.
                    learn.assert_not_called()
                    self.assertIsNotNone(db.execute('SELECT 1 FROM decision_revisions WHERE decision_id=?', (dependent,)).fetchone())
                    raise RuntimeError('Abort confirmed signature')
            learn.assert_not_called()
        self.assertEqual(self.store.get_decision(dependent), before)

    def test_managed_terminal_publication_checks_legacy_sources_on_its_writer(self):
        from bridge.execution import ExecutionService
        _, source = self.source()
        task, dependent = self.derived(source)
        self.sign(dependent)
        with self.store.connect() as db:
            db.execute("INSERT INTO executions(run_id,submission_key,task,config,launch_state,status,created_at,updated_at) "
                       "VALUES(?,?,?,'{}','linked','working','now','now')", (task, task, 'Managed archive'))
        service = ExecutionService(self.store, object())
        with self.other.connect() as db:
            db.execute("UPDATE decisions SET updated_at='2032-01-01T00:00:00+00:00' WHERE id=?", (source,))
        for status in ('result_ready', 'completed'):
            service.update(task, status=status)
            self.assertEqual(service.get(task)['status'], 'review_required')
            self.assertEqual(service.get(task)['review_required'], 1)
            self.assertEqual(self.graph.db.execute('SELECT status FROM runs WHERE id=?', (task,)).fetchone()[0], 'review_required')

    def test_correction_writer_holds_finish_and_cosign_until_invalidation_commits(self):
        for operation in ('finish', 'cosign'):
            with self.subTest(operation=operation):
                _, source = self.source()
                task, node = self.derived(source)
                if operation == 'finish':
                    self.sign(node)
                else:
                    self.graph.db.execute('UPDATE decisions SET required_signers=? WHERE id=?',
                                          (json.dumps(['Policy Owner', 'Co Owner']), node))
                    self.sign(node)
                before = self.other.get_decision(node)
                entered, release, started, done = [threading.Event() for _ in range(4)]
                errors, accepted = [], []
                original = self.graph.flag_dependents
                def hold(*args, **kwargs):
                    result = original(*args, **kwargs)
                    if args[0] == source:
                        entered.set()
                        if not release.wait(10):
                            raise AssertionError('Writer barrier timed out')
                    return result
                def writer():
                    try:
                        self.correct(source)
                    except BaseException as error:
                        errors.append(error)
                    finally:
                        self.graph.close_thread()
                def consumer():
                    started.set()
                    try:
                        if operation == 'finish':
                            self.other.update_run(task, {'status': 'completed'})
                        else:
                            canvas.sign_off(self.other, node, {'by': 'Co Owner',
                                'expected_updated_at': before['updated_at']})
                        accepted.append(operation)
                    except Invalid:
                        pass
                    except BaseException as error:
                        errors.append(error)
                    finally:
                        done.set()
                        self.other.graph.close_thread()
                with patch.object(self.graph, 'flag_dependents', hold):
                    writer_thread = threading.Thread(target=writer)
                    writer_thread.start()
                    self.assertTrue(entered.wait(10))
                    # The source and invalidation are both still uncommitted.
                    self.assertEqual(self.other.get_decision(source)['answer'], 'Keep records thirty days.')
                    consumer_thread = threading.Thread(target=consumer)
                    consumer_thread.start()
                    self.assertTrue(started.wait(10))
                    try:
                        self.assertFalse(done.wait(.1))
                    finally:
                        release.set()
                        writer_thread.join(10)
                        consumer_thread.join(10)
                self.assertFalse(writer_thread.is_alive() or consumer_thread.is_alive())
                self.assertEqual(errors, [])
                self.assertEqual(accepted, [])
                self.assertFalse(self.store.get_decision(node)['authorized'])
                self.assertEqual(json.loads(self.store.get_decision(node)['signatures']), [])

    def test_source_change_during_finish_is_rechecked_on_finish_writer(self):
        _, source = self.source()
        task, node = self.derived(source)
        self.sign(node)
        original = self.graph.expire_drafts
        def correction_before_writer():
            original()
            with self.other.connect() as db:
                db.execute("UPDATE decisions SET updated_at='2033-01-01T00:00:00+00:00' WHERE id=?", (source,))
        with patch.object(self.graph, 'expire_drafts', correction_before_writer):
            with self.assertRaisesRegex(Invalid, 'recorded revision'):
                self.store.update_run(task, {'status': 'completed'})
        self.assertEqual(self.graph.db.execute('SELECT status FROM runs WHERE id=?', (task,)).fetchone()[0], 'working')

    def test_signoff_correction_rolls_back_owner_change_and_notifications(self):
        _, source = self.source()
        _, dependent = self.derived(source)
        before = self.store.get_decision(source)
        original = self.graph.flag_dependents
        def abort(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError('Abort signoff correction')
        with patch.object(self.graph, 'flag_dependents', abort), patch.object(self.store, 'notify') as notify:
            with self.assertRaisesRegex(RuntimeError, 'Abort'):
                canvas.sign_off(self.store, source, {'by': 'A Different Owner', 'answer': 'Seven days',
                    'expected_updated_at': before['updated_at']})
            notify.assert_not_called()
        self.assertEqual(self.store.get_decision(source), before)
        self.assertFalse(self.store.get_decision(dependent)['needs_review'])

    def test_delayed_twin_settlement_cannot_pin_old_answer_to_new_revision(self):
        from bridge.ladder import close_open_twins
        _, source = self.source()
        old_revision = self.store.get_decision(source)['updated_at']
        self.correct(source)
        _, twin = self.node()
        self.assertEqual(close_open_twins(self.graph, source, 'How long do we keep policy records?',
            'atomic/policy', 'Keep records seven days.', 'Policy Owner', expected_revision=old_revision), 0)
        self.assertEqual(close_open_twins(self.graph, source, 'How long do we keep policy records?',
            'atomic/policy', 'Keep records thirty days.', 'Policy Owner'), 0)
        self.assertEqual(self.store.get_decision(twin)['status'], 'pending')
        self.assertEqual(close_open_twins(self.graph, source, 'How long do we keep policy records?',
            'atomic/policy', 'Keep records seven days.', 'Policy Owner'), 1)
        current = self.store.get_decision(twin)
        self.assertEqual(current['answer'], 'Keep records seven days.')
        self.assertEqual(current['source_revision'], self.store.get_decision(source)['updated_at'])
