-- Postgres init script for the medplum DB stack.
--
-- The official postgres image runs every .sql in /docker-entrypoint-initdb.d/
-- exactly once, on the first container start when /var/lib/postgresql/data
-- is empty. We use that to provision both the main `medplum` DB (already
-- created by POSTGRES_DB env) and the `medplum_test` DB that medplum's
-- test framework (loadTestConfig in packages/server/src/config/loader.ts)
-- expects.
--
-- The `medplum_test_readonly` role is a separate read-only user that
-- loadTestConfig references for the readonlyDatabase pool.

CREATE DATABASE medplum_test;

CREATE ROLE medplum_test_readonly WITH LOGIN PASSWORD 'medplum_test_readonly';
GRANT CONNECT ON DATABASE medplum_test TO medplum_test_readonly;
