SET ROLE plane_owner;
CREATE TABLE management.admission_settings (
    singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton),
    mode text NOT NULL CHECK (mode = 'prepared')
);
INSERT INTO management.admission_settings(mode) VALUES ('prepared');
GRANT SELECT ON management.admission_settings TO plane_writer;
GRANT CREATE ON SCHEMA management TO plane_writer;
SET ROLE plane_writer;

CREATE OR REPLACE FUNCTION management.accept_tenant(
    p_tenant text, p_isolation text, p_message text
)
RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
DECLARE
    v_pair text;
    v_tenant management.tenants;
    v_operation uuid;
BEGIN
    PERFORM pg_advisory_xact_lock(35510, 1);
    IF EXISTS (SELECT 1 FROM management.tenants WHERE tenant_id = p_tenant) THEN
        RAISE EXCEPTION 'duplicate_tenant' USING ERRCODE = 'PT409';
    END IF;
    SELECT pair_id INTO v_pair FROM management.pairs p
    WHERE isolation = p_isolation AND stage = 'available' AND (
        p_isolation = 'shared'
        OR NOT EXISTS (SELECT 1 FROM management.tenants t WHERE t.pair_id = p.pair_id)
    ) ORDER BY pair_id LIMIT 1 FOR UPDATE;
    IF v_pair IS NULL THEN
        RAISE EXCEPTION 'allocation_unavailable' USING ERRCODE = 'PT507';
    END IF;
    INSERT INTO management.tenants(tenant_id, isolation, pair_id, initial_message)
    VALUES (p_tenant, p_isolation, v_pair, p_message) RETURNING * INTO v_tenant;
    INSERT INTO management.operations(tenant_id,status,stage)
    VALUES (p_tenant,'succeeded','available') RETURNING operation_id INTO v_operation;
    INSERT INTO management.events(tenant_id,onboarding_id,pair_id,source,type,version)
    VALUES (p_tenant,v_tenant.onboarding_id,v_pair,'management','tenant_requested',1);
    INSERT INTO management.events(tenant_id,onboarding_id,pair_id,source,type,version,stage)
    VALUES (
        p_tenant,v_tenant.onboarding_id,v_pair,'management','provisioning_succeeded',1,'available'
    );
    RETURN v_operation;
END $$;

SET ROLE plane_owner;
REVOKE CREATE ON SCHEMA management FROM plane_writer;
RESET ROLE;
