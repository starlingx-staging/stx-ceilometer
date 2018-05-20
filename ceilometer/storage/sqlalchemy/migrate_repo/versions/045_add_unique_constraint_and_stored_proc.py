# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.
#
# Copyright (c) 2016 Wind River Systems, Inc.
#

from migrate.changeset.constraint import UniqueConstraint
import sqlalchemy as sa
from sqlalchemy import text


# Add unique constraint for user_id, project_id, resource_id, source_id and
# resource_metadata
# Add stored procedure to perform sample insertion with resource and meter
# existence check and creation of meter and/or resource
def upgrade(migrate_engine):
    # when upgrade, the sample meter and resource tables are not transformed
    #  to new system, so it is safe to create unique constraint
    meta = sa.MetaData(bind=migrate_engine)
    resource = sa.Table("resource", meta, autoload=True)
    uni_constraint = UniqueConstraint(
        "user_id", "project_id", "resource_id", "source_id",
        "resource_metadata", name="uix_resource_uni", table=resource
    )
    uni_constraint.create()

    index = sa.Index("idx_resource_user_id", resource.c.user_id)
    index.create(bind=migrate_engine)
    index = sa.Index("idx_resource_project_id", resource.c.project_id)
    index.create(bind=migrate_engine)

    # in some tox environment, sqlite3 failed the unique index with
    # where clause
    if migrate_engine.name == 'postgresql':
        t = text(
            "CREATE UNIQUE INDEX uix_project_user_id_null_idx ON resource "
            "(resource_id, source_id, resource_metadata) WHERE project_id "
            "IS NULL and user_id is NULL;"
        )
        migrate_engine.execute(t)

        t = text(
            "CREATE UNIQUE INDEX uix_user_id_null_idx ON resource "
            "(project_id, resource_id, source_id, resource_metadata) "
            "WHERE user_id IS NULL;"
        )
        migrate_engine.execute(t)

        t = text(
            "CREATE UNIQUE INDEX uix_project_id_null_idx ON resource "
            "(user_id, resource_id, source_id, resource_metadata) "
            "WHERE project_id IS NULL;"
        )
        migrate_engine.execute(t)

    if migrate_engine.name == 'postgresql':
        t = text(
            "CREATE INDEX idx_resource_metadata "
            "ON resource (resource_metadata text_pattern_ops);"
        )
    else:
        t = text(
            "CREATE INDEX idx_resource_metadata "
            "ON resource (resource_metadata);"
        )
    migrate_engine.execute(t)

    # only support postgresql until further requirement
    if migrate_engine.name != 'postgresql':
        return

    t = text(
        "create or replace function create_sample"
        "(counter_volume float, counter_name varchar(255), "
        "counter_type varchar(255), counter_unit varchar(255),"
        "r_id varchar(255), u_id varchar(255), p_id varchar(255), "
        "s_id varchar(255), m_hash varchar(32), r_meta text, ts varchar(255),"
        " msg_signature varchar(1000), msg_id varchar(1000)) "
        "RETURNS table(new_sample_id bigint, resource_id integer, "
        "meter_id integer, new_resource boolean, "
        "new_meter boolean) as\n"
        "$$\n"
        "DECLARE\n"
        "   res_id integer;\n"
        "   met_id integer;\n"
        "   new_resource boolean = false;\n"
        "   new_meter boolean = false;\n"
        "BEGIN\n"
        "   select internal_id into res_id from resource"
        " where ((user_id is Null and u_id is Null) or"
        " (user_id = u_id)) and\n"
        "       ((project_id is Null and p_id is Null) or"
        " (project_id = p_id)) and\n"
        "       resource.resource_id = r_id and\n"
        "       resource.source_id = s_id and\n"
        "       resource.metadata_hash = m_hash;\n\n"
        "   IF res_id is NULL THEN\n"
        "   BEGIN\n"
        "       insert into resource (resource_id, user_id, project_id, "
        "source_id, resource_metadata, metadata_hash)\n"
        "       values(r_id, u_id, p_id, s_id, r_meta, m_hash);\n"
        "       select lastval() into res_id;\n"
        "       select true into new_resource;\n"
        "       EXCEPTION WHEN unique_violation THEN\n"
        "           select internal_id into res_id from resource \n"
        "           where resource.resource_id = r_id and\n"
        "               resource.user_id = u_id and\n"
        "               resource.project_id = p_id and\n"
        "               resource.metadata_hash = m_hash;\n"
        "       END;\n"
        "   END IF;\n\n"
        "   select meter.id into met_id from meter\n"
        "       where meter.name = counter_name and \n"
        "       meter.type = counter_type and\n"
        "       meter.unit = counter_unit;\n"
        "   if met_id is NULL THEN\n"
        "   BEGIN\n"
        "       insert into meter (name, type, unit) \n"
        "       values(counter_name, counter_type, counter_unit);\n"
        "       select lastval() into met_id;\n"
        "       select true into new_meter;\n"
        "       EXCEPTION WHEN unique_violation THEN\n"
        "           select meter.id into met_id from meter "
        "where meter.name = counter_name and "
        "meter.type = counter_type and meter.unit = counter_unit;\n"
        "       END;\n"
        "   end if;\n\n"
        "   insert into sample (volume, timestamp, message_signature, "
        "message_id, recorded_at, meter_id, resource_id)\n"
        "	values(counter_volume, "
        "to_timestamp(ts, 'YYYY-MM-DD HH24:MI:SS'), msg_signature, "
        "msg_id, to_timestamp(ts, 'YYYY-MM-DDTHH:MI:SS'), met_id, res_id);\n"
        "   return query select lastval() as new_sample_id, \n"
        "                       res_id as resource_id, \n"
        "                       met_id as meter_id, \n"
        "                       new_resource as new_resource, \n"
        "                       new_meter as new_meter;\n"
        "END;\n"
        "$$\n"
        "LANGUAGE plpgsql;"
    )

    migrate_engine.execute(t)

    # Since there's no CREATE OR REPLACE TYPE and sqlalchemy engine
    # does not run the Postgres script to check if resourcedata
    # type exists in the pg_type table before creating one properly,
    # the safest option is to drop the stored proc which depends
    # on the type and the type if they exist before creating them.

    t = text(
        "DROP FUNCTION IF EXISTS resource_getbatch(text);"
    )

    migrate_engine.execute(t)

    t = text(
        "DROP TYPE IF EXISTS resourcedata;"
    )

    migrate_engine.execute(t)

    t = text(
        "CREATE TYPE resourcedata AS ("
        "resource_id character varying(255), user_id character varying(255), "
        "project_id character varying(255), source_id character varying(255), "
        "resource_metadata text);"
    )

    migrate_engine.execute(t)

    # Use multiple query method in the following stored proc as opposed to
    # IN/= ANY clause as we want to limit the number of items for
    # each matching resource_id to 1. The query is precompiled and query
    # plan is cached so it will be just as fast.

    t = text(
        "CREATE FUNCTION resource_getbatch(resource_ids text) RETURNS "
        "SETOF resourcedata AS\n"
        "$BODY$\n"
        "   DECLARE\n"
        "       r       text;\n"
        "       resids  text[] := string_to_array($1, ',');\n"
        "   BEGIN\n"
        "       FOREACH r IN ARRAY resids LOOP\n"
        "           $1 = r;\n"
        "           RETURN QUERY EXECUTE 'SELECT resource_id, user_id, "
        "project_id, source_id, resource_metadata FROM resource WHERE "
        "resource_id = $1 LIMIT 1'\n"
        "USING r;\n"
        "       END LOOP;\n"
        "   END;\n"
        "$BODY$\n"
        "LANGUAGE plpgsql;"
    )

    migrate_engine.execute(t)
