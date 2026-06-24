-- 004_rls.sql — Row-Level Security: tenant isolation by client_id. Idempotent.
-- Every query (outside a superuser/admin connection) is auto-filtered by the per-connection GUC
-- `app.current_client_id`, bound in di/db.py:acquire(client_id=...). FORCE so even the table
-- owner is filtered.
--
-- NOTE: superusers and BYPASSRLS roles bypass RLS even with FORCE. In production the app connects
-- as a non-superuser role; local dev may run as a superuser (isolation then relies on the
-- application always passing client_id). DROP+CREATE makes policy creation idempotent.

DO $$
DECLARE
    t text;
    tables text[] := ARRAY[
        'di_documents', 'doc_version', 'di_entity', 'client_merged_fact',
        'di_decision_trace', 'knode', 'arep'
    ];
BEGIN
    FOREACH t IN ARRAY tables LOOP
        EXECUTE format('ALTER TABLE __SCHEMA__.%I ENABLE ROW LEVEL SECURITY;', t);
        EXECUTE format('ALTER TABLE __SCHEMA__.%I FORCE ROW LEVEL SECURITY;', t);
        EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON __SCHEMA__.%I;', t);
        EXECUTE format(
            'CREATE POLICY tenant_isolation ON __SCHEMA__.%I '
            'USING (client_id = current_setting(''app.current_client_id'', true)) '
            'WITH CHECK (client_id = current_setting(''app.current_client_id'', true));',
            t
        );
    END LOOP;
END$$;
