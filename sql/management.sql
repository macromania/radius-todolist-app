CREATE SCHEMA management AUTHORIZATION plane_owner;
REVOKE ALL ON SCHEMA management FROM PUBLIC;
SET ROLE plane_owner;
ALTER DEFAULT PRIVILEGES IN SCHEMA management REVOKE ALL ON TABLES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES IN SCHEMA management REVOKE ALL ON SEQUENCES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES IN SCHEMA management REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;

CREATE TABLE management.pairs (
    pair_id text PRIMARY KEY CHECK (pair_id ~ '^[a-z0-9][a-z0-9-]{0,31}$'),
    isolation text NOT NULL CHECK (isolation IN ('shared', 'isolated')),
    stage text NOT NULL DEFAULT 'allocated',
    reporting_role name NOT NULL UNIQUE,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK ((isolation = 'shared') = (pair_id = 'shared'))
);
CREATE TABLE management.login_pairs (
    login_role name PRIMARY KEY,
    pair_id text NOT NULL UNIQUE REFERENCES management.pairs
);
CREATE TABLE management.tenants (
    tenant_id text PRIMARY KEY
        CHECK (tenant_id ~ '^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$'),
    onboarding_id uuid NOT NULL UNIQUE DEFAULT gen_random_uuid(),
    isolation text NOT NULL CHECK (isolation IN ('shared', 'isolated')),
    pair_id text NOT NULL REFERENCES management.pairs,
    initial_message text NOT NULL CHECK (char_length(initial_message) <= 1024),
    desired_revision bigint NOT NULL DEFAULT 1 CHECK (desired_revision = 1),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE UNIQUE INDEX isolated_pair_assignment ON management.tenants(pair_id)
    WHERE isolation = 'isolated';
CREATE TABLE management.operations (
    operation_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id text NOT NULL UNIQUE REFERENCES management.tenants,
    status text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'running', 'succeeded', 'failed', 'interrupted')),
    stage text NOT NULL DEFAULT 'accepted' CHECK (stage ~ '^[a-z][a-z0-9_-]{0,63}$'),
    error_code text CHECK (error_code ~ '^[a-z][a-z0-9_]{0,63}$'),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE UNIQUE INDEX one_active_operation ON management.operations((true))
    WHERE status IN ('pending', 'running');
CREATE TABLE management.events (
    event_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id text NOT NULL REFERENCES management.tenants,
    onboarding_id uuid NOT NULL,
    pair_id text NOT NULL REFERENCES management.pairs,
    source text NOT NULL,
    type text NOT NULL,
    version bigint NOT NULL,
    error_code text,
    stage text,
    received_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX tenant_event_page ON management.events(tenant_id, event_id);
CREATE UNIQUE INDEX unique_control_report
    ON management.events(onboarding_id, version, type) WHERE source = 'control';

CREATE FUNCTION management.preserve_tenant_identity() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
    IF NEW IS DISTINCT FROM OLD THEN
        RAISE EXCEPTION 'tenant_desired_state_immutable' USING ERRCODE = '42501';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER immutable_tenant BEFORE UPDATE ON management.tenants
    FOR EACH ROW EXECUTE FUNCTION management.preserve_tenant_identity();

ALTER TABLE management.tenants ENABLE ROW LEVEL SECURITY;
ALTER TABLE management.tenants FORCE ROW LEVEL SECURITY;
ALTER TABLE management.events ENABLE ROW LEVEL SECURITY;
ALTER TABLE management.events FORCE ROW LEVEL SECURITY;
ALTER TABLE management.pairs ENABLE ROW LEVEL SECURITY;
ALTER TABLE management.pairs FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_pair ON management.tenants
    USING (
        current_user IN ('plane_owner', 'plane_writer', 'plane_reporter')
        OR session_user IN ('mgmt_api', 'mgmt_provisioner')
        OR pair_id = (SELECT pair_id FROM management.login_pairs WHERE login_role = session_user)
    );
CREATE POLICY event_pair ON management.events
    USING (
        current_user IN ('plane_owner', 'plane_writer', 'plane_reporter')
        OR session_user IN ('mgmt_api', 'mgmt_provisioner')
        OR pair_id = (SELECT pair_id FROM management.login_pairs WHERE login_role = session_user)
    );
CREATE POLICY allocated_pair ON management.pairs
    USING (
        current_user IN ('plane_owner', 'plane_writer', 'plane_reporter')
        OR session_user IN ('mgmt_api', 'mgmt_provisioner')
        OR pair_id = (SELECT pair_id FROM management.login_pairs WHERE login_role = session_user)
    );

CREATE FUNCTION management.accept_tenant(p_tenant text, p_isolation text, p_message text)
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
    IF EXISTS (SELECT 1 FROM management.operations WHERE status IN ('pending', 'running')) THEN
        RAISE EXCEPTION 'provisioner_busy' USING ERRCODE = 'PT503';
    END IF;
    SELECT pair_id INTO v_pair FROM management.pairs p
    WHERE isolation = p_isolation AND (
        p_isolation = 'shared'
        OR NOT EXISTS (SELECT 1 FROM management.tenants t WHERE t.pair_id = p.pair_id)
    ) ORDER BY pair_id LIMIT 1 FOR UPDATE;
    IF v_pair IS NULL THEN
        RAISE EXCEPTION 'allocation_unavailable' USING ERRCODE = 'PT507';
    END IF;
    INSERT INTO management.tenants(tenant_id, isolation, pair_id, initial_message)
    VALUES (p_tenant, p_isolation, v_pair, p_message) RETURNING * INTO v_tenant;
    INSERT INTO management.operations(tenant_id) VALUES (p_tenant)
    RETURNING operation_id INTO v_operation;
    INSERT INTO management.events(tenant_id, onboarding_id, pair_id, source, type, version)
    VALUES (p_tenant, v_tenant.onboarding_id, v_pair, 'management', 'tenant_requested', 1);
    RETURN v_operation;
END $$;

CREATE FUNCTION management.report_control(
    p_tenant text, p_onboarding uuid, p_version bigint, p_type text, p_error text DEFAULT NULL
) RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
DECLARE
    v_pair text;
    v_tenant management.tenants;
    v_event management.events;
    v_id bigint;
BEGIN
    SELECT pair_id INTO v_pair FROM management.login_pairs WHERE login_role = session_user;
    IF v_pair IS NULL THEN
        RAISE EXCEPTION 'report_not_authorized' USING ERRCODE = '42501';
    END IF;
    SELECT * INTO v_tenant FROM management.tenants
    WHERE tenant_id = p_tenant AND pair_id = v_pair FOR UPDATE;
    IF NOT FOUND OR v_tenant.onboarding_id IS DISTINCT FROM p_onboarding
       OR p_version IS DISTINCT FROM v_tenant.desired_revision THEN
        RAISE EXCEPTION 'invalid_report_target' USING ERRCODE = 'PT422';
    END IF;
    IF p_type IS NULL OR p_type NOT IN ('control_record_created', 'control_record_failed')
       OR (p_type = 'control_record_created' AND p_error IS NOT NULL)
       OR (p_type = 'control_record_failed'
           AND (p_error IS NULL OR p_error !~ '^[a-z][a-z0-9_]{0,63}$')) THEN
        RAISE EXCEPTION 'invalid_report_content' USING ERRCODE = 'PT422';
    END IF;
    SELECT * INTO v_event FROM management.events
    WHERE onboarding_id = p_onboarding AND version = p_version
        AND type = p_type AND source = 'control';
    IF FOUND THEN
        IF v_event.error_code IS DISTINCT FROM p_error THEN
            RAISE EXCEPTION 'report_content_conflict' USING ERRCODE = 'PT409';
        END IF;
        RETURN v_event.event_id;
    END IF;
    IF p_type = 'control_record_failed' THEN
        SELECT event_id INTO v_id FROM management.events WHERE onboarding_id = p_onboarding
            AND version = p_version AND type = 'control_record_created' AND source = 'control';
        IF FOUND THEN RETURN v_id; END IF;
    END IF;
    INSERT INTO management.events(tenant_id, onboarding_id, pair_id, source, type, version, error_code)
    VALUES (p_tenant, p_onboarding, v_pair, 'control', p_type, p_version, p_error)
    RETURNING event_id INTO v_id;
    RETURN v_id;
END $$;
RESET ROLE;

GRANT USAGE ON SCHEMA management TO plane_writer, plane_reporter, mgmt_api, mgmt_provisioner;
GRANT CREATE ON SCHEMA management TO plane_writer, plane_reporter;
GRANT SELECT ON ALL TABLES IN SCHEMA management TO plane_writer;
GRANT SELECT ON management.tenants, management.events, management.login_pairs TO plane_reporter;
GRANT INSERT ON management.tenants, management.operations, management.events TO plane_writer;
GRANT UPDATE(stage) ON management.pairs TO plane_writer;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA management TO plane_writer, plane_reporter;
GRANT UPDATE(onboarding_id) ON management.tenants TO plane_reporter;
GRANT INSERT ON management.events TO plane_reporter;
GRANT SELECT ON management.tenants, management.pairs, management.operations,
    management.events, management.login_pairs TO mgmt_api, mgmt_provisioner;
GRANT UPDATE ON management.operations, management.pairs TO mgmt_provisioner;
GRANT UPDATE(onboarding_id) ON management.tenants TO mgmt_provisioner;
GRANT INSERT ON management.events TO mgmt_provisioner;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA management TO mgmt_provisioner;
ALTER FUNCTION management.accept_tenant(text, text, text) OWNER TO plane_writer;
ALTER FUNCTION management.report_control(text, uuid, bigint, text, text) OWNER TO plane_reporter;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA management FROM PUBLIC;
GRANT EXECUTE ON FUNCTION management.accept_tenant(text, text, text) TO mgmt_api;
REVOKE CREATE ON SCHEMA management FROM plane_writer, plane_reporter;
