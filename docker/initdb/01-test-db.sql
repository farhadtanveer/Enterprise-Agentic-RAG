-- Local dev convenience: the test suite uses its own database so test
-- fixtures can never touch development data. Matches TEST_DATABASE_URL in
-- docs/dev-testing.md.
CREATE DATABASE rag_test;
