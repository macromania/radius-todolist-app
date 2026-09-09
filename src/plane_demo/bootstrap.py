"""Fresh-database initialization; never imported or invoked by runtime workloads."""

import json
import logging
import os
import re
from pathlib import Path

import psycopg
from psycopg import sql

from plane_demo.settings import required

logger = logging.getLogger(__name__)
OWNERS = {"plane_owner", "plane_writer", "plane_reporter"}


def initialize(
    dsn: str,
    kind: str,
    passwords: dict[str, str],
    slots: list[dict[str, str]],
    schema_directory: Path = Path("sql"),
    pair_id: str = "",
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
    with psycopg.connect(dsn, connect_timeout=10) as connection:
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
        connection.execute((schema_directory / f"{kind}.sql").read_text())
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
        )
    except (psycopg.Error, ValueError, KeyError, OSError) as error:
        logger.error("bootstrap_failed category=%s", type(error).__name__)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
