CREATE SCHEMA control AUTHORIZATION plane_owner;
REVOKE ALL ON SCHEMA control FROM PUBLIC;
SET ROLE plane_owner;
ALTER DEFAULT PRIVILEGES IN SCHEMA control REVOKE ALL ON TABLES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES IN SCHEMA control REVOKE ALL ON SEQUENCES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES IN SCHEMA control REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;

CREATE TABLE control.login_pairs (
    login_role name PRIMARY KEY,
    pair_id text NOT NULL CHECK (pair_id ~ '^[a-z0-9][a-z0-9-]{0,31}$')
);
CREATE TABLE control.tenant_config (
    tenant_id text PRIMARY KEY CHECK (tenant_id ~ '^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$'),
    onboarding_id uuid NOT NULL UNIQUE,
    pair_id text NOT NULL CHECK (pair_id ~ '^[a-z0-9][a-z0-9-]{0,31}$'),
    message text NOT NULL CHECK (char_length(message) <= 1024),
    version bigint NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE control.events (
    event_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id text NOT NULL REFERENCES control.tenant_config,
    onboarding_id uuid NOT NULL,
    pair_id text NOT NULL,
    source text NOT NULL,
    type text NOT NULL,
    version bigint NOT NULL,
    error_code text,
    received_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX tenant_event_page ON control.events(tenant_id, event_id);
CREATE UNIQUE INDEX unique_data_report
    ON control.events(onboarding_id, version, type) WHERE source = 'data';

CREATE FUNCTION control.preserve_tenant_identity() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
    IF NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
       OR NEW.onboarding_id IS DISTINCT FROM OLD.onboarding_id
       OR NEW.pair_id IS DISTINCT FROM OLD.pair_id
       OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'tenant_identity_immutable' USING ERRCODE = '42501';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER immutable_tenant BEFORE UPDATE ON control.tenant_config
    FOR EACH ROW EXECUTE FUNCTION control.preserve_tenant_identity();

ALTER TABLE control.tenant_config ENABLE ROW LEVEL SECURITY;
ALTER TABLE control.tenant_config FORCE ROW LEVEL SECURITY;
ALTER TABLE control.events ENABLE ROW LEVEL SECURITY;
ALTER TABLE control.events FORCE ROW LEVEL SECURITY;
CREATE POLICY config_pair ON control.tenant_config USING (
    current_user IN ('plane_owner', 'plane_writer', 'plane_reporter')
    OR pair_id = (SELECT pair_id FROM control.login_pairs WHERE login_role = session_user)
);
CREATE POLICY event_pair ON control.events USING (
    current_user IN ('plane_owner', 'plane_writer', 'plane_reporter')
    OR pair_id = (SELECT pair_id FROM control.login_pairs WHERE login_role = session_user)
);

CREATE FUNCTION control.ensure_tenant(
    p_tenant text, p_onboarding uuid, p_pair text, p_message text
) RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
DECLARE
    v_row control.tenant_config;
    v_inserted boolean;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM control.login_pairs
        WHERE login_role = session_user AND pair_id = p_pair) THEN
        RAISE EXCEPTION 'tenant_not_authorized' USING ERRCODE = '42501';
    END IF;
    INSERT INTO control.tenant_config(tenant_id, onboarding_id, pair_id, message)
    VALUES (p_tenant, p_onboarding, p_pair, p_message) ON CONFLICT (tenant_id) DO NOTHING;
    v_inserted := FOUND;
    SELECT * INTO v_row FROM control.tenant_config WHERE tenant_id = p_tenant FOR UPDATE;
    IF v_row.onboarding_id <> p_onboarding OR v_row.pair_id <> p_pair THEN
        RAISE EXCEPTION 'onboarding_conflict' USING ERRCODE = 'PT409';
    END IF;
    IF v_inserted THEN
        INSERT INTO control.events(tenant_id, onboarding_id, pair_id, source, type, version)
        VALUES (p_tenant, p_onboarding, p_pair, 'control', 'configuration_created', 1);
    END IF;
    RETURN v_row.version;
END $$;

CREATE FUNCTION control.update_configuration(p_tenant text, p_message text) RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
DECLARE v_row control.tenant_config;
BEGIN
    SELECT c.* INTO v_row FROM control.tenant_config c JOIN control.login_pairs b
        ON b.pair_id = c.pair_id AND b.login_role = session_user
        WHERE c.tenant_id = p_tenant FOR UPDATE OF c;
    IF NOT FOUND THEN RAISE EXCEPTION 'tenant_not_found' USING ERRCODE = 'PT404'; END IF;
    UPDATE control.tenant_config SET message = p_message, version = version + 1,
        updated_at = clock_timestamp() WHERE tenant_id = p_tenant RETURNING * INTO v_row;
    INSERT INTO control.events(tenant_id, onboarding_id, pair_id, source, type, version)
    VALUES (p_tenant, v_row.onboarding_id, v_row.pair_id,
        'control', 'configuration_updated', v_row.version);
    RETURN v_row.version;
END $$;

CREATE FUNCTION control.report_data(
    p_tenant text, p_onboarding uuid, p_version bigint, p_type text, p_error text DEFAULT NULL
) RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
DECLARE
    v_pair text;
    v_row control.tenant_config;
    v_event control.events;
    v_id bigint;
BEGIN
    SELECT pair_id INTO v_pair FROM control.login_pairs WHERE login_role = session_user;
    IF v_pair IS NULL THEN RAISE EXCEPTION 'report_not_authorized' USING ERRCODE = '42501'; END IF;
    SELECT * INTO v_row FROM control.tenant_config
        WHERE tenant_id = p_tenant AND pair_id = v_pair FOR UPDATE;
    IF NOT FOUND OR v_row.onboarding_id IS DISTINCT FROM p_onboarding
        OR p_version IS NULL OR p_version < 1 OR p_version > v_row.version THEN
        RAISE EXCEPTION 'invalid_report_target' USING ERRCODE = 'PT422';
    END IF;
    IF p_type IS NULL OR p_type NOT IN ('config_applied', 'config_apply_failed')
       OR (p_type = 'config_applied' AND p_error IS NOT NULL)
       OR (p_type = 'config_apply_failed'
           AND (p_error IS NULL OR p_error !~ '^[a-z][a-z0-9_]{0,63}$')) THEN
        RAISE EXCEPTION 'invalid_report_content' USING ERRCODE = 'PT422';
    END IF;
    SELECT * INTO v_event FROM control.events WHERE onboarding_id = p_onboarding
        AND version = p_version AND type = p_type AND source = 'data';
    IF FOUND THEN
        IF v_event.error_code IS DISTINCT FROM p_error THEN
            RAISE EXCEPTION 'report_content_conflict' USING ERRCODE = 'PT409';
        END IF;
        RETURN v_event.event_id;
    END IF;
    IF p_type = 'config_apply_failed' THEN
        SELECT event_id INTO v_id FROM control.events WHERE onboarding_id = p_onboarding
            AND version = p_version AND type = 'config_applied' AND source = 'data';
        IF FOUND THEN RETURN v_id; END IF;
    END IF;
    INSERT INTO control.events(tenant_id, onboarding_id, pair_id, source, type, version, error_code)
    VALUES (p_tenant, p_onboarding, v_pair, 'data', p_type, p_version, p_error)
    RETURNING event_id INTO v_id;
    RETURN v_id;
END $$;
RESET ROLE;

GRANT USAGE ON SCHEMA control TO plane_writer, plane_reporter, cp_api, cp_reconciler, dp_reconciler;
GRANT CREATE ON SCHEMA control TO plane_writer, plane_reporter;
GRANT SELECT ON ALL TABLES IN SCHEMA control
    TO plane_writer, plane_reporter, cp_api, cp_reconciler, dp_reconciler;
GRANT INSERT, UPDATE ON control.tenant_config TO plane_writer;
GRANT INSERT ON control.events TO plane_writer, plane_reporter;
GRANT UPDATE(onboarding_id) ON control.tenant_config TO plane_reporter;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA control TO plane_writer, plane_reporter;
ALTER FUNCTION control.ensure_tenant(text, uuid, text, text) OWNER TO plane_writer;
ALTER FUNCTION control.update_configuration(text, text) OWNER TO plane_writer;
ALTER FUNCTION control.report_data(text, uuid, bigint, text, text) OWNER TO plane_reporter;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA control FROM PUBLIC;
GRANT EXECUTE ON FUNCTION control.ensure_tenant(text, uuid, text, text) TO cp_reconciler;
GRANT EXECUTE ON FUNCTION control.update_configuration(text, text) TO cp_api;
GRANT EXECUTE ON FUNCTION control.report_data(text, uuid, bigint, text, text) TO dp_reconciler;
REVOKE CREATE ON SCHEMA control FROM plane_writer, plane_reporter;
