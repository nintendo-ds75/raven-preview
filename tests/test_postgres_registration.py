"""PostgreSQL suite registration can be checked without a server or credentials."""
from collections import Counter
import importlib
import unittest
from unittest.mock import patch

import test_postgres


PERSISTENCE_CASES = {
    'test_contact_reasoning_contract': 'ContactReasoningContractTests',
    'test_contact_evidence_context': 'ContactEvidenceContextTests',
    'test_inquiry_citations': 'InquiryCitationRaceTests',
    'test_canvas_memory_provenance': 'BackgroundMemoryProvenanceTests',
    'test_context_guidance': 'ConnectedContextReadTests',
}
GUIDANCE_METHODS = {
    'test_engaged_kickoff_and_resume_guide_evidence_reading_without_forcing_calls',
    'test_no_judgment_task_can_finish_without_a_source_reading_gate',
}


def cases(suite):
    for case in suite:
        if isinstance(case, unittest.TestSuite):
            yield from cases(case)
        else:
            yield case


class PostgresRegistrationTests(unittest.TestCase):
    def discover(self, enabled):
        with patch.dict('os.environ', {'BRIDGE_TEST_POSTGRES': '1' if enabled else '0'}), \
                patch.object(test_postgres.PostgresIsolation, 'setUp', side_effect=AssertionError('Discovery only')), \
                patch.object(test_postgres.database, 'connect', side_effect=AssertionError('No database')), \
                patch('socket.create_connection', side_effect=AssertionError('No network')), \
                patch('socket.socket.connect', side_effect=AssertionError('No network')):
            return list(cases(test_postgres.load_tests(unittest.TestLoader(), unittest.TestSuite(), None)))

    def test_postgres_remains_explicit_opt_in(self):
        self.assertEqual(self.discover(False), [])

    def test_new_persistence_contracts_wrap_every_original_method_once(self):
        discovered = self.discover(True)
        for module_name, class_name in PERSISTENCE_CASES.items():
            with self.subTest(case=class_name):
                original = getattr(importlib.import_module(module_name), class_name)
                selected = [case for case in discovered if type(case).__name__ == 'Postgres' + class_name]
                expected = unittest.TestLoader().getTestCaseNames(original)
                self.assertEqual(Counter(case._testMethodName for case in selected), Counter(expected))
                self.assertTrue(selected)
                for case in selected:
                    self.assertEqual(type(case).__bases__, (test_postgres.PostgresIsolation, original))

    def test_only_persisted_guidance_and_citation_races_enter_postgres_lane(self):
        discovered = self.discover(True)
        names = {type(case).__name__ for case in discovered}
        guidance = [case for case in discovered if type(case).__name__ == 'PostgresContextGuidanceTests']
        self.assertEqual(Counter(case._testMethodName for case in guidance), Counter(GUIDANCE_METHODS))
        self.assertNotIn('PostgresInquiryCitationTests', names)
        self.assertNotIn('PostgresAcceptanceTests', names)
        self.assertNotIn('PostgresPostgresRegistrationTests', names)


if __name__ == '__main__':
    unittest.main()
