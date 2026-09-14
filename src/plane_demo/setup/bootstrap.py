"""Fresh-database initialization; never imported or invoked by runtime workloads."""

import hashlib
import json
import logging
import os
import re
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
from psycopg import sql

from plane_demo.shared.settings import required

logger = logging.getLogger(__name__)
OWNERS = {"plane_owner", "plane_writer", "plane_reporter"}
SCHEMA_VERSION = 2
CATALOG_QUERY = """
SELECT jsonb_build_object(
 'schemas',(SELECT jsonb_agg(jsonb_build_object('name',nspname,'owner',nspowner::regrole::text,
   'acl',nspacl::text) ORDER BY nspname) FROM pg_namespace WHERE nspname=ANY(%s)),
 'relations',(SELECT jsonb_agg(jsonb_build_object('schema',n.nspname,'name',c.relname,
   'kind',c.relkind,'owner',c.relowner::regrole::text,'acl',c.relacl::text,
   'rls',c.relrowsecurity,'forced',c.relforcerowsecurity) ORDER BY n.nspname,c.relname)
   FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=ANY(%s)),
 'policies',(SELECT jsonb_agg(to_jsonb(p) ORDER BY schemaname,tablename,policyname)
   FROM pg_policies p WHERE schemaname=ANY(%s)),
 'columns',(SELECT jsonb_agg(jsonb_build_object('schema',n.nspname,'relation',c.relname,
   'name',a.attname,'type',format_type(a.atttypid,a.atttypmod),'nullable',NOT a.attnotnull,
   'acl',a.attacl::text,'default',pg_get_expr(d.adbin,d.adrelid),
   'identity',a.attidentity,'generated',a.attgenerated) ORDER BY n.nspname,c.relname,a.attnum)
   FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid
   JOIN pg_namespace n ON n.oid=c.relnamespace
   LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum
   WHERE n.nspname=ANY(%s) AND a.attnum>0 AND NOT a.attisdropped),
 'constraints',(SELECT jsonb_agg(jsonb_build_object('schema',n.nspname,'name',c.conname,
   'relation',c.conrelid::regclass::text,'definition',pg_get_constraintdef(c.oid))
   ORDER BY n.nspname,c.conrelid::regclass::text,c.conname)
   FROM pg_constraint c JOIN pg_namespace n ON n.oid=c.connamespace WHERE n.nspname=ANY(%s)),
 'indexes',(SELECT jsonb_agg(jsonb_build_object('schema',n.nspname,'name',c.relname,
   'definition',pg_get_indexdef(i.indexrelid),'valid',i.indisvalid,
   'ready',i.indisready,'live',i.indislive) ORDER BY n.nspname,c.relname)
   FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid
   JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=ANY(%s)),
 'triggers',(SELECT jsonb_agg(jsonb_build_object('schema',n.nspname,'relation',c.relname,
   'name',t.tgname,'enabled',t.tgenabled,'definition',pg_get_triggerdef(t.oid))
   ORDER BY n.nspname,c.relname,t.tgname)
   FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
   JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=ANY(%s)),
 'functions',(SELECT jsonb_agg(jsonb_build_object('name',p.oid::regprocedure::text,
   'owner',p.proowner::regrole::text,'acl',p.proacl::text,'definition',pg_get_functiondef(p.oid))
   ORDER BY p.oid::regprocedure::text) FROM pg_proc p
   JOIN pg_namespace n ON n.oid=p.pronamespace
   WHERE n.nspname=ANY(%s) AND p.prokind IN ('f','p')),
 'roles',(SELECT jsonb_agg(jsonb_build_object('name',rolname,'login',rolcanlogin,
   'super',rolsuper,'create_role',rolcreaterole,'create_db',rolcreatedb,'bypass_rls',rolbypassrls,
   'inherit',rolinherit,'replication',rolreplication) ORDER BY rolname)
   FROM pg_roles WHERE rolname=ANY(%s)),
 'memberships',(SELECT jsonb_agg(jsonb_build_object('role',r.rolname,'member',m.rolname,
   'admin',a.admin_option,'inherit',a.inherit_option,'set',a.set_option)
   ORDER BY r.rolname,m.rolname) FROM pg_auth_members a JOIN pg_roles r ON r.oid=a.roleid
   JOIN pg_roles m ON m.oid=a.member WHERE m.rolname=ANY(%s)))
"""


def catalog_digest(connection, kind: str, roles: set[str]) -> str:
    schemas = [kind, "demo_metadata"]
    names = sorted(OWNERS | roles)
    value = connection.execute(CATALOG_QUERY, (schemas,) * 8 + (names, names)).fetchone()[0]
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def initialized(connection, kind: str, roles: set[str], expected: dict) -> bool:
    marker = connection.execute("SELECT to_regclass('demo_metadata.schema_version')").fetchone()[0]
    if marker is None:
        if connection.execute("SELECT to_regnamespace(%s)", (kind,)).fetchone()[0] is not None:
            raise ValueError("database_schema_version_missing")
        return False
    rows = connection.execute(
        "SELECT schema_kind,version,configuration,schema_sha256,catalog_sha256 "
        "FROM demo_metadata.schema_version"
    ).fetchall()
    if len(rows) != 1:
        raise ValueError("database_schema_version_ambiguous")
    row = rows[0]
    if row[:4] != (kind, SCHEMA_VERSION, expected["configuration"], expected["schema_sha256"]):
        raise ValueError("database_schema_version_mismatch")
    if row[4] != catalog_digest(connection, kind, roles):
        raise ValueError("database_schema_contract_changed")
    return True


def initialize(
    dsn: str,
    kind: str,
    passwords: dict[str, str],
    slots: list[dict[str, str]],
    schema_directory: Path = Path("sql"),
    pair_id: str = "",
    initialization_id: str | None = None,
) -> None:
    if kind not in {"management", "control"}:
        raise ValueError("BOOTSTRAP_KIND must be management or control")
    roles = (
        {"mgmt_api", "mgmt_provisioner"}
        if kind == "management"
        else {"cp_api", "cp_reconciler", "dp_reconciler"}
    )
    if kind == "management":
        reporting_roles = {slot["reporting_role"] for slot in slots}
        if roles & reporting_roles or len(reporting_roles) != len(slots):
            raise ValueError("each pair requires a distinct child-only reporting role")
        roles |= reporting_roles
        if not slots or not any(slot["pair_id"] == "shared" for slot in slots):
            raise ValueError("management allocation requires a shared slot")
    elif not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,31}", pair_id):
        raise ValueError("control initialization requires PAIR_ID")
    if set(passwords) != roles:
        raise ValueError("ROLE_PASSWORDS_JSON must match precisely the runtime role names")
    if any(
        not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", role)
        or role in OWNERS
        or len(passwords[role]) < 32
        for role in roles
    ):
        raise ValueError("invalid runtime role name or password shorter than 32 characters")
    schema = (schema_directory / f"{kind}.sql").read_text()
    expected = {
        "configuration": {
            "pair_id": pair_id,
            "slots": sorted(slots, key=lambda item: item["pair_id"]),
        },
        "schema_sha256": hashlib.sha256(schema.encode()).hexdigest(),
    }
    operation = UUID(initialization_id) if initialization_id else uuid4()
    with psycopg.connect(dsn, connect_timeout=10) as connection:
        connection.execute("SELECT pg_advisory_xact_lock(35510,3)")
        if initialized(connection, kind, roles, expected):
            logger.info("database_already_initialized kind=%s version=%s", kind, SCHEMA_VERSION)
            return
        for role in sorted(OWNERS | roles):
            found = connection.execute(
                "SELECT rolsuper, rolbypassrls, rolcanlogin, rolcreatedb, rolcreaterole, "
                "rolreplication FROM pg_roles WHERE rolname = %s",
                (role,),
            ).fetchone()
            if found:
                if found != (False, False, role in roles, False, False, False):
                    raise ValueError("existing role has incompatible privileges")
                memberships = connection.execute(
                    "SELECT 1 FROM pg_auth_members m JOIN pg_roles r ON r.oid=m.member "
                    "WHERE r.rolname=%s LIMIT 1",
                    (role,),
                ).fetchone()
                if memberships:
                    raise ValueError("existing runtime/owner role has unexpected memberships")
            else:
                connection.execute(
                    sql.SQL(
                        "CREATE ROLE {} {} NOSUPERUSER NOCREATEDB NOCREATEROLE "
                        "NOINHERIT NOBYPASSRLS"
                    ).format(sql.Identifier(role), sql.SQL("LOGIN" if role in roles else "NOLOGIN"))
                )
            if role in roles:
                connection.execute(
                    sql.SQL("ALTER ROLE {} PASSWORD {}").format(
                        sql.Identifier(role), sql.Literal(passwords[role])
                    )
                )
        # The trusted initialization login needs temporary owner membership on managed PG.
        setup_role = connection.execute("SELECT session_user").fetchone()[0]
        restored_memberships = []
        for role in sorted(OWNERS):
            membership = connection.execute(
                "SELECT m.inherit_option,m.set_option FROM pg_auth_members m "
                "JOIN pg_roles r ON r.oid=m.roleid JOIN pg_roles u ON u.oid=m.member "
                "WHERE r.rolname=%s AND u.rolname=session_user",
                (role,),
            ).fetchone()
            is_superuser = connection.execute(
                "SELECT rolsuper FROM pg_roles WHERE rolname=session_user"
            ).fetchone()[0]
            if not is_superuser and membership != (True, True):
                connection.execute(
                    sql.SQL("GRANT {} TO {} WITH INHERIT TRUE, SET TRUE").format(
                        sql.Identifier(role), sql.Identifier(setup_role)
                    )
                )
                restored_memberships.append((role, membership))
        connection.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
        connection.execute(schema)
        connection.execute("CREATE SCHEMA demo_metadata AUTHORIZATION plane_owner")
        connection.execute("REVOKE ALL ON SCHEMA demo_metadata FROM PUBLIC")
        connection.execute("SET ROLE plane_owner")
        if kind == "management":
            for slot in slots:
                pair = slot["pair_id"]
                role = slot["reporting_role"]
                connection.execute(
                    "INSERT INTO management.pairs(pair_id,isolation,reporting_role) "
                    "VALUES (%s,%s,%s)",
                    (pair, "shared" if pair == "shared" else "isolated", role),
                )
                connection.execute(
                    "INSERT INTO management.login_pairs(login_role,pair_id) VALUES (%s,%s)",
                    (role, pair),
                )
                connection.execute(
                    sql.SQL("GRANT USAGE ON SCHEMA management TO {}").format(sql.Identifier(role))
                )
                connection.execute(
                    sql.SQL(
                        "GRANT SELECT ON management.tenants, management.pairs, "
                        "management.login_pairs TO {}"
                    ).format(sql.Identifier(role))
                )
                connection.execute("SET ROLE plane_reporter")
                connection.execute(
                    sql.SQL(
                        "GRANT EXECUTE ON FUNCTION "
                        "management.report_control(text,uuid,bigint,text,text) TO {}"
                    ).format(sql.Identifier(role))
                )
                connection.execute("SET ROLE plane_owner")
        else:
            for role in roles:
                connection.execute(
                    "INSERT INTO control.login_pairs(login_role,pair_id) VALUES (%s,%s)",
                    (role, pair_id),
                )
        connection.execute(
            "CREATE TABLE demo_metadata.schema_version ("
            "singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton),"
            "schema_kind text NOT NULL,version integer NOT NULL,configuration jsonb NOT NULL,"
            "schema_sha256 text NOT NULL,catalog_sha256 text NOT NULL,"
            "initialization_id uuid NOT NULL,"
            "initialized_at timestamptz NOT NULL DEFAULT clock_timestamp())"
        )
        for role in sorted(roles | {setup_role}):
            connection.execute(
                sql.SQL("GRANT USAGE ON SCHEMA demo_metadata TO {}").format(sql.Identifier(role))
            )
            connection.execute(
                sql.SQL("GRANT SELECT ON demo_metadata.schema_version TO {}").format(
                    sql.Identifier(role)
                )
            )
        digest = catalog_digest(connection, kind, roles)
        connection.execute(
            "INSERT INTO demo_metadata.schema_version"
            "(schema_kind,version,configuration,schema_sha256,catalog_sha256,initialization_id)"
            "VALUES (%s,%s,%s::jsonb,%s,%s,%s)",
            (
                kind,
                SCHEMA_VERSION,
                json.dumps(expected["configuration"]),
                expected["schema_sha256"],
                digest,
                operation,
            ),
        )
        connection.execute("RESET ROLE")
        for role, membership in restored_memberships:
            if membership is None:
                connection.execute(
                    sql.SQL("REVOKE {} FROM {}").format(
                        sql.Identifier(role), sql.Identifier(setup_role)
                    )
                )
            else:
                connection.execute(
                    sql.SQL("GRANT {} TO {} WITH INHERIT {}, SET {}").format(
                        sql.Identifier(role),
                        sql.Identifier(setup_role),
                        sql.SQL("TRUE" if membership[0] else "FALSE"),
                        sql.SQL("TRUE" if membership[1] else "FALSE"),
                    )
                )


def main() -> None:
    try:
        initialize(
            required("BOOTSTRAP_DSN"),
            required("BOOTSTRAP_KIND"),
            json.loads(required("ROLE_PASSWORDS_JSON")),
            json.loads(os.environ.get("PAIR_SLOTS_JSON", "[]")),
            Path(os.environ.get("SQL_DIRECTORY", "/app/sql")),
            os.environ.get("PAIR_ID", ""),
            os.environ.get("INITIALIZATION_ID"),
        )
    except (psycopg.Error, ValueError, KeyError, OSError) as error:
        logger.error("bootstrap_failed category=%s", type(error).__name__)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
