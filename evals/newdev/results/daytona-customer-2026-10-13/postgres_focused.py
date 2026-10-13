import importlib,sys,unittest
sys.path.insert(0,'/app/tests')
from test_postgres import PostgresIsolation
suite=unittest.TestSuite();loader=unittest.TestLoader()
for name in ['test_workbench','test_workbench_hosts','test_inquiry','test_native_access','test_context_visibility','test_sources_jira','test_mcp_events','test_work_item_links']:
 m=importlib.import_module(name)
 for key,case in vars(m).items():
  if isinstance(case,type) and issubclass(case,unittest.TestCase) and case.__module__==name:
   wrapped=type('Postgres'+key,(PostgresIsolation,case),{'__module__':__name__})
   suite.addTests(loader.loadTestsFromTestCase(wrapped))
r=unittest.TextTestRunner(verbosity=1).run(suite)
sys.exit(not r.wasSuccessful())
