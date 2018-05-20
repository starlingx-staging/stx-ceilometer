#
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
# Copyright (c) 2013-2016 Wind River Systems, Inc.
#

"""SQLAlchemy storage backend."""

from __future__ import absolute_import
import datetime
import hashlib
import os

from collections import namedtuple
from oslo_db import api
from oslo_db import exception as dbexc
from oslo_db.sqlalchemy import session as db_session
from oslo_log import log
from oslo_serialization import jsonutils
from oslo_utils import timeutils
import six
import sqlalchemy as sa
from sqlalchemy import and_
from sqlalchemy import distinct
from sqlalchemy import func
from sqlalchemy.orm import aliased
from sqlalchemy.sql import text

import ceilometer
from ceilometer.i18n import _
from ceilometer import storage
from ceilometer.storage import base
from ceilometer.storage import models as api_models
from ceilometer.storage.sqlalchemy import models
from ceilometer.storage.sqlalchemy import utils as sql_utils
from ceilometer import utils

LOG = log.getLogger(__name__)


STANDARD_AGGREGATES = dict(
    avg=func.avg(models.Sample.volume).label('avg'),
    sum=func.sum(models.Sample.volume).label('sum'),
    min=func.min(models.Sample.volume).label('min'),
    max=func.max(models.Sample.volume).label('max'),
    count=func.count(models.Sample.volume).label('count')
)

UNPARAMETERIZED_AGGREGATES = dict(
    stddev=func.stddev_pop(models.Sample.volume).label('stddev')
)

PARAMETERIZED_AGGREGATES = dict(
    validate=dict(
        cardinality=lambda p: p in ['resource_id', 'user_id', 'project_id']
    ),
    compute=dict(
        cardinality=lambda p: func.count(
            distinct(getattr(models.Resource, p))
        ).label('cardinality/%s' % p)
    )
)

AVAILABLE_CAPABILITIES = {
    'meters': {'query': {'simple': True,
                         'metadata': True}},
    'resources': {'query': {'simple': True,
                            'metadata': True}},
    'samples': {'query': {'simple': True,
                          'metadata': True,
                          'complex': True}},
    'statistics': {'groupby': True,
                   'query': {'simple': True,
                             'metadata': True},
                   'aggregation': {'standard': True,
                                   'selectable': {
                                       'max': True,
                                       'min': True,
                                       'sum': True,
                                       'avg': True,
                                       'count': True,
                                       'stddev': True,
                                       'cardinality': True}}
                   },
}


AVAILABLE_STORAGE_CAPABILITIES = {
    'storage': {'production_ready': True},
}


def apply_metaquery_filter(session, query, metaquery):
    """Apply provided metaquery filter to existing query.

    :param session: session used for original query
    :param query: Query instance
    :param metaquery: dict with metadata to match on.
    """
    for k, value in six.iteritems(metaquery):
        key = k[9:]  # strip out 'metadata.' prefix
        try:
            _model = sql_utils.META_TYPE_MAP[type(value)]
        except KeyError:
            raise ceilometer.NotImplementedError(
                'Query on %(key)s is of %(value)s '
                'type and is not supported' %
                {"key": k, "value": type(value)})
        else:
            meta_alias = aliased(_model)
            on_clause = and_(models.Resource.internal_id == meta_alias.id,
                             meta_alias.meta_key == key)
            # outer join is needed to support metaquery
            # with or operator on non existent metadata field
            # see: test_query_non_existing_metadata_with_result
            # test case.
            query = query.outerjoin(meta_alias, on_clause)
            query = query.filter(meta_alias.value == value)

    return query


def make_query_from_filter(session, query, sample_filter, require_meter=True):
    """Return a query dictionary based on the settings in the filter.

    :param session: session used for original query
    :param query: Query instance
    :param sample_filter: SampleFilter instance
    :param require_meter: If true and the filter does not have a meter,
                          raise an error.
    """

    if sample_filter.meter:
        # meter filter is on caller level with a in clause
        pass
    elif require_meter:
        raise RuntimeError('Missing required meter specifier')
    if sample_filter.source:
        query = query.filter(
            models.Resource.source_id == sample_filter.source)
    if sample_filter.start_timestamp:
        ts_start = sample_filter.start_timestamp
        if sample_filter.start_timestamp_op == 'gt':
            query = query.filter(models.Sample.timestamp > ts_start)
        else:
            query = query.filter(models.Sample.timestamp >= ts_start)
    if sample_filter.end_timestamp:
        ts_end = sample_filter.end_timestamp
        if sample_filter.end_timestamp_op == 'le':
            query = query.filter(models.Sample.timestamp <= ts_end)
        else:
            query = query.filter(models.Sample.timestamp < ts_end)
    if sample_filter.user:
        if sample_filter.user == 'None':
            sample_filter.user = None
        query = query.filter(models.Resource.user_id == sample_filter.user)
    if sample_filter.project:
        if sample_filter.project == 'None':
            sample_filter.project = None
        query = query.filter(
            models.Resource.project_id == sample_filter.project)
    if sample_filter.resource:
        query = query.filter(
            models.Resource.resource_id == sample_filter.resource)
    if sample_filter.message_id:
        query = query.filter(
            models.Sample.message_id == sample_filter.message_id)

    if sample_filter.metaquery:
        query = apply_metaquery_filter(session, query,
                                       sample_filter.metaquery)

    return query


def apply_multiple_metaquery_filter(query, metaquery):
    """This is to support multiple metadata filter

    The result of this is to add a sub clause in where
    ... and sample.resource_id in (
        select id from metadata_xxx where <meta filter 1>
         union select id from metadata_xxx where <meta filter 2>
         union ...)
    """

    if len(metaquery) <= 1:
        return apply_metaquery_filter_to_query(query, metaquery)

    quires = []
    i = 0
    for k, value in six.iteritems(metaquery):
        key = k[9:]  # strip out 'metadata.' prefix
        try:
            _model = sql_utils.META_TYPE_MAP[type(value)]
        except KeyError:
            raise ceilometer.NotImplementedError(
                'Query on %(key)s is of %(value)s '
                'type and is not supported' %
                {"key": k, "value": type(value)})
        else:
            table_name = _model.__tablename__

            i += 1
            key_parm = 'key_%s' % i
            value_parm = 'value_%s' % i
            sql = "select id from %s where meta_key = :%s and value = :%s" % (
                table_name, key_parm, value_parm
            )
            quires.append(sql)
            query.args[key_parm] = key
            query.args[value_parm] = value

    union_clause = ' union '.join(quires)
    query.wheres.append('sample.resource_id in (%s)' % union_clause)

    return query


def apply_metaquery_filter_to_query(query, metaquery):
    """Apply provided metaquery filter to existing query.

    :param query: SimpleQueryGenerator instance
    :param metaquery: dict with metadata to match on.
    """

    if len(metaquery) > 1:
        return apply_multiple_metaquery_filter(query, metaquery)

    join_list = []
    i = 0
    for k, value in six.iteritems(metaquery):
        key = k[9:]  # strip out 'metadata.' prefix
        try:
            _model = sql_utils.META_TYPE_MAP[type(value)]
        except KeyError:
            raise ceilometer.NotImplementedError(
                'Query on %(key)s is of %(value)s '
                'type and is not supported' %
                {"key": k, "value": type(value)})
        else:
            table_name = _model.__tablename__
            if table_name not in join_list:
                join_list.append(table_name)

            i += 1
            meta_key_param = 'meta_key_{0}'.format(i)
            meta_value_param = 'meta_value_{0}'.format(i)
            join_clause = \
                'join {0} on {0}.id = sample.resource_id ' \
                'and {0}.meta_key = :{1} ' \
                'and {0}.value = :{2}'.format(
                    table_name, meta_key_param, meta_value_param
                )
            query.joins.append(join_clause)
            query.args[meta_key_param] = key
            query.args[meta_value_param] = value
    return query


def add_stats_filter_to_query(query, sample_filter):
    """Update the query with addition filters

    :param query: Query object
    :param sample_filter: SampleFilter instance
    Note that meter_name, start_time and end_time has already applied
    """

    if sample_filter.source:
        query.wheres.append('resource.source_id = :source_id')
        query.args['source_id'] = sample_filter.source
    if sample_filter.user:
        if sample_filter.user == 'None':
            query.wheres.append('resource.user_id is null')
        else:
            query.wheres.append('resource.user_id = :user_id')
            query.args['user_id'] = sample_filter.user
    if sample_filter.project:
        if sample_filter.project == 'None':
            query.wheres.append('resource.project_id is null')
        else:
            query.wheres.append('resource.project_id = :project_id')
            query.args['project_id'] = sample_filter.project
    if sample_filter.resource:
        query.wheres.append('resource.resource_id = :resource_id')
        query.args['resource_id'] = sample_filter.resource
    if sample_filter.message_id:
        query.wheres.append('sample.message_id = :message_id')
        query.args['message_id'] = sample_filter.message_id
    if sample_filter.metaquery:
        query = apply_metaquery_filter_to_query(
            query,
            sample_filter.metaquery
        )

    return query


def apply_filter_to_meter_query(query, sample_filter):
    """Update the query with additional filters

    :param query: Query object
    :param sample_filter: SampleFilter instance
    Metadata is not applicable to meter queries
    """

    if sample_filter.user:
        query = query.filter(
            models.Resource.user_id == sample_filter.user)
    if sample_filter.project:
        query = query.filter(
            models.Resource.project_id == sample_filter.project)
    if sample_filter.source:
        query = query.filter(
            models.Resource.source_id == sample_filter.source)
    if sample_filter.resource:
        query = query.filter(
            models.Resource.resource_id == sample_filter.resource)

    return query


# sqlalchemy isn't flexible enough to handle handwrite complex column
# in query. This query generator class help to solve the problem.
# limitation: where clause is 'and' relation
class SimpleQueryGenerator(object):
    def __init__(self):
        self.columns = []
        self.froms = []
        self.joins = []
        self.wheres = []
        self.groups = []
        self.orders = []
        self.args = {}

    def __str__(self):
        res = 'select '
        res = res + ', '.join(self.columns) + '\n'
        res = res + 'FROM ' + ', '.join(self.froms) + '\n'

        if len(self.joins) > 0:
            res = res + '\n '.join(self.joins) + '\n'

        if len(self.wheres) > 0:
            res = res + 'where ' + ' and '.join(self.wheres) + '\n'

        if len(self.groups) > 0:
            res = res + 'group by ' + ', '.join(self.groups) + '\n'

        if len(self.orders) > 0:
            res = res + 'order by ' + ', '.join(self.orders) + '\n'

        return res

    def get_sql(self):
        return self.__str__()


class Connection(base.Connection):
    """Put the data into a SQLAlchemy database.

    Tables::

        - meter
          - meter definition
          - { id: meter id
              name: meter name
              type: meter type
              unit: meter unit
              }
        - resource
          - resource definition
          - { internal_id: resource id
              resource_id: resource uuid
              user_id: user uuid
              project_id: project uuid
              source_id: source id
              resource_metadata: metadata dictionary
              metadata_hash: metadata dictionary hash
              }
        - sample
          - the raw incoming data
          - { id: sample id
              meter_id: meter id            (->meter.id)
              resource_id: resource id      (->resource.internal_id)
              volume: sample volume
              timestamp: datetime
              recorded_at: datetime
              message_signature: message signature
              message_id: message uuid
              }
    """
    CAPABILITIES = utils.update_nested(base.Connection.CAPABILITIES,
                                       AVAILABLE_CAPABILITIES)
    STORAGE_CAPABILITIES = utils.update_nested(
        base.Connection.STORAGE_CAPABILITIES,
        AVAILABLE_STORAGE_CAPABILITIES,
    )

    def __init__(self, conf, url):
        super(Connection, self).__init__(conf, url)
        # Set max_retries to 0, since oslo.db in certain cases may attempt
        # to retry making the db connection retried max_retries ^ 2 times
        # in failure case and db reconnection has already been implemented
        # in storage.__init__.get_connection_from_config function
        options = dict(self.conf.database.items())
        options['max_retries'] = 0
        # oslo.db doesn't support options defined by Ceilometer
        for opt in storage.OPTS:
            options.pop(opt.name, None)
        self._engine_facade = db_session.EngineFacade(url, **options)

    def upgrade(self):
        # NOTE(gordc): to minimise memory, only import migration when needed
        from oslo_db.sqlalchemy import migration
        path = os.path.join(os.path.abspath(os.path.dirname(__file__)),
                            'sqlalchemy', 'migrate_repo')
        engine = self._engine_facade.get_engine()

        # from migrate import exceptions as migrate_exc
        # from migrate.versioning import api
        # from migrate.versioning import repository

        # repo = repository.Repository(path)
        # try:
        #     api.db_version(engine, repo)
        # except migrate_exc.DatabaseNotControlledError:
        #     models.Base.metadata.create_all(engine)
        #     api.version_control(engine, repo, repo.latest)
        # else:
        #     migration.db_sync(engine, path)
        migration.db_sync(engine, path)

    def clear(self):
        engine = self._engine_facade.get_engine()
        for table in reversed(models.Base.metadata.sorted_tables):
            engine.execute(table.delete())
        engine.dispose()

    @staticmethod
    def _create_meter(conn, name, type, unit):
        # TODO(gordc): implement lru_cache to improve performance
        try:
            meter = models.Meter.__table__
            trans = conn.begin_nested()
            if conn.dialect.name == 'sqlite':
                trans = conn.begin()
            with trans:
                meter_row = conn.execute(
                    sa.select([meter.c.id])
                    .where(sa.and_(meter.c.name == name,
                                   meter.c.type == type,
                                   meter.c.unit == unit))).first()
                meter_id = meter_row[0] if meter_row else None
                if meter_id is None:
                    result = conn.execute(meter.insert(), name=name,
                                          type=type, unit=unit)
                    meter_id = result.inserted_primary_key[0]
        except dbexc.DBDuplicateEntry:
            # retry function to pick up duplicate committed object
            meter_id = Connection._create_meter(conn, name, type, unit)

        return meter_id

    @staticmethod
    def _create_resource(conn, res_id, user_id, project_id, source_id,
                         rmeta):
        # TODO(gordc): implement lru_cache to improve performance
        try:
            res = models.Resource.__table__
            m_hash = jsonutils.dumps(rmeta, sort_keys=True)
            if six.PY3:
                m_hash = m_hash.encode('utf-8')
            m_hash = hashlib.md5(m_hash).hexdigest()
            trans = conn.begin_nested()
            if conn.dialect.name == 'sqlite':
                trans = conn.begin()
            with trans:
                res_row = conn.execute(
                    sa.select([res.c.internal_id])
                    .where(sa.and_(res.c.resource_id == res_id,
                                   res.c.user_id == user_id,
                                   res.c.project_id == project_id,
                                   res.c.source_id == source_id,
                                   res.c.metadata_hash == m_hash))).first()
                internal_id = res_row[0] if res_row else None
                if internal_id is None:
                    result = conn.execute(res.insert(), resource_id=res_id,
                                          user_id=user_id,
                                          project_id=project_id,
                                          source_id=source_id,
                                          resource_metadata=rmeta,
                                          metadata_hash=m_hash)
                    internal_id = result.inserted_primary_key[0]
                    if rmeta and isinstance(rmeta, dict):
                        meta_map = {}
                        for key, v in utils.dict_to_keyval(rmeta):
                            try:
                                _model = sql_utils.META_TYPE_MAP[type(v)]
                                if meta_map.get(_model) is None:
                                    meta_map[_model] = []
                                meta_map[_model].append(
                                    {'id': internal_id, 'meta_key': key,
                                     'value': v})
                            except KeyError:
                                LOG.warning(_("Unknown metadata type. Key "
                                              "(%s) will not be queryable."),
                                            key)
                        for _model in meta_map.keys():
                            conn.execute(_model.__table__.insert(),
                                         meta_map[_model])

        except dbexc.DBDuplicateEntry:
            # retry function to pick up duplicate committed object
            internal_id = Connection._create_resource(
                conn, res_id, user_id, project_id, source_id, rmeta)

        return internal_id

    @staticmethod
    def _create_resource_meta(conn, res_id, rmeta):
        if rmeta and isinstance(rmeta, dict):
            meta_map = {}
            for key, v in utils.dict_to_keyval(rmeta):
                try:
                    _model = sql_utils.META_TYPE_MAP[type(v)]
                    if meta_map.get(_model) is None:
                        meta_map[_model] = []
                    meta_map[_model].append(
                        {'id': res_id, 'meta_key': key,
                         'value': v})
                except KeyError:
                    LOG.warning(_("Unknown metadata type. Key "
                                  "(%s) will not be queryable."),
                                key)
            for _model in meta_map.keys():
                conn.execute(_model.__table__.insert(),
                             meta_map[_model])

    # FIXME(sileht): use set_defaults to pass cfg.CONF.database.retry_interval
    # and cfg.CONF.database.max_retries to this method when global config
    # have been removed (puting directly cfg.CONF don't work because and copy
    # the default instead of the configured value)
    @api.wrap_db_retry(retry_interval=10, max_retries=10,
                       retry_on_deadlock=True)
    def record_metering_data(self, data):
        """Write the data to the backend storage system.

        :param data: a dictionary such as returned by
                     ceilometer.publisher.utils.meter_message_from_counter
        """
        engine = self._engine_facade.get_engine()
        metadata = jsonutils.dumps(
            data['resource_metadata'],
            sort_keys=True
        )

        m_hash = hashlib.md5(metadata).hexdigest()

        conn = engine.raw_connection()
        try:
            cursor = conn.cursor()
            params = [
                float(data['counter_volume']),
                data['counter_name'],
                data['counter_type'],
                data['counter_unit'],
                data['resource_id'],
                data['user_id'],
                data['project_id'],
                data['source'],
                m_hash,
                metadata,
                str(data['timestamp']),
                data['message_signature'],
                data['message_id']
            ]
            cursor.callproc("create_sample", params)
            result = cursor.fetchone()
            # result is tuple of
            # (sample_id, resource_id, meter_id, is_new_resource, is_new_meter)
            # limited by sqlalchemy, the column names are not accessible.
            cursor.close()
            new_resource_id = result[1]
            is_new_resource = result[3]
            conn.commit()

            if is_new_resource and data['resource_metadata']:
                with engine.begin() as conn2:
                    self._create_resource_meta(
                        conn2,
                        new_resource_id,
                        data['resource_metadata']
                    )
        finally:
            conn.close()

    def clear_expired_metering_data(self, ttl):
        """Clear expired data from the backend storage system.

        Clearing occurs according to the time-to-live.
        :param ttl: Number of seconds to keep records for.
        """
        # Prevent database deadlocks from occurring by
        # using separate transaction for each delete
        session = self._engine_facade.get_session()
        with session.begin():
            end = timeutils.utcnow() - datetime.timedelta(seconds=ttl)
            sample_q = (session.query(models.Sample)
                        .filter(models.Sample.timestamp < end))
            rows = sample_q.delete()
            LOG.info("%d samples removed from database", rows)

        engine = self._engine_facade.get_engine()
        with engine.connect() as conn:
            result = conn.execute(text('SELECT last_value FROM meter_id_seq;'))
            first_row = result.first()
            if first_row is not None and \
               first_row['last_value'] > 7378697629483820646:
                stmt = text('ALTER SEQUENCE meter_id_seq RESTART WITH 1;')
                conn.execute(stmt)
                LOG.info("restart sample id sequence, last value was %d",
                         first_row['last_value'])

        if not self.conf.database.sql_expire_samples_only:
            with session.begin():
                # remove Meter definitions with no matching samples
                (session.query(models.Meter)
                 .filter(~models.Meter.samples.any())
                 .delete(synchronize_session=False))

            # remove metadata of resources with no matching samples
            for table in [models.MetaText, models.MetaBigInt,
                          models.MetaFloat, models.MetaBool]:
                with session.begin():
                    resource_q = (session.query(models.Resource.internal_id)
                                  .filter(~models.Resource.samples.any()))
                    resource_subq = resource_q.subquery()
                    (session.query(table)
                     .filter(table.id.in_(resource_subq))
                     .delete(synchronize_session=False))

            # remove resource with no matching samples
            with session.begin():
                resource_q = (session.query(models.Resource.internal_id)
                              .filter(~models.Resource.samples.any()))
                resource_q.delete(synchronize_session=False)
            LOG.info("Expired residual resource and"
                     " meter definition data")

    def get_resources(self, user=None, project=None, source=None,
                      start_timestamp=None, start_timestamp_op=None,
                      end_timestamp=None, end_timestamp_op=None,
                      metaquery=None, resource=None, limit=None):
        """Return an iterable of api_models.Resource instances

        :param user: Optional ID for user that owns the resource.
        :param project: Optional ID for project that owns the resource.
        :param source: Optional source filter.
        :param start_timestamp: Optional modified timestamp start range.
        :param start_timestamp_op: Optional start time operator, like gt, ge.
        :param end_timestamp: Optional modified timestamp end range.
        :param end_timestamp_op: Optional end time operator, like lt, le.
        :param metaquery: Optional dict with metadata to match on.
        :param resource: Optional resource filter.
        :param limit: Maximum number of results to return.
        """
        if limit == 0:
            return
        s_filter = storage.SampleFilter(user=user,
                                        project=project,
                                        source=source,
                                        start_timestamp=start_timestamp,
                                        start_timestamp_op=start_timestamp_op,
                                        end_timestamp=end_timestamp,
                                        end_timestamp_op=end_timestamp_op,
                                        metaquery=metaquery,
                                        resource=resource)

        session = self._engine_facade.get_session()
        # get list of resource_ids
        has_timestamp = start_timestamp or end_timestamp
        # NOTE: When sql_expire_samples_only is enabled, there will be some
        #       resources without any sample, in such case we should use inner
        #       join on sample table to avoid wrong result.
        if self.conf.database.sql_expire_samples_only or has_timestamp:
            res_q = session.query(distinct(models.Resource.resource_id)).join(
                models.Sample,
                models.Sample.resource_id == models.Resource.internal_id)
        else:
            res_q = session.query(distinct(models.Resource.resource_id))
        res_q = make_query_from_filter(session, res_q, s_filter,
                                       require_meter=False)
        res_q = res_q.limit(limit) if limit else res_q
        for res_id in res_q.all():
            if has_timestamp:
                # get max and min sample timestamp value
                min_max_q = (session.query(func.max(models.Sample.timestamp)
                                           .label('max_timestamp'),
                                           func.min(models.Sample.timestamp)
                                           .label('min_timestamp'))
                             .join(models.Resource,
                                   models.Resource.internal_id ==
                                   models.Sample.resource_id)
                             .filter(models.Resource.resource_id ==
                                     res_id[0]))

                min_max_q = make_query_from_filter(session, min_max_q,
                                                   s_filter,
                                                   require_meter=False)
                min_max = min_max_q.first()

                # get resource details for latest sample
                res_q = (session.query(models.Resource.resource_id,
                                       models.Resource.user_id,
                                       models.Resource.project_id,
                                       models.Resource.source_id,
                                       models.Resource.resource_metadata)
                         .join(models.Sample,
                               models.Sample.resource_id ==
                               models.Resource.internal_id)
                         .filter(models.Sample.timestamp ==
                                 min_max.max_timestamp)
                         .filter(models.Resource.resource_id ==
                                 res_id[0])
                         .order_by(models.Sample.id.desc()).limit(1))
            else:
                # If the client does not specify the timestamps, use
                # a much leaner query to retrieve resource info
                min_max_ts = namedtuple('min_max_ts', ['min_timestamp',
                                                       'max_timestamp'])
                min_max = min_max_ts(None, None)
                res_q = (session.query(models.Resource.resource_id,
                                       models.Resource.user_id,
                                       models.Resource.project_id,
                                       models.Resource.source_id,
                                       models.Resource.resource_metadata)
                         .filter(models.Resource.resource_id == res_id[0])
                         .limit(1))

            res = res_q.first()

            yield api_models.Resource(
                resource_id=res.resource_id,
                project_id=res.project_id,
                first_sample_timestamp=min_max.min_timestamp,
                last_sample_timestamp=min_max.max_timestamp,
                source=res.source_id,
                user_id=res.user_id,
                metadata=res.resource_metadata
            )

    def get_meters(self, user=None, project=None, resource=None, source=None,
                   meter=None, metaquery=None, limit=None, unique=False):
        """Return an iterable of api_models.Meter instances

        :param user: Optional ID for user that owns the resource.
        :param project: Optional ID for project that owns the resource.
        :param resource: Optional ID of the resource.
        :param source: Optional source filter.
        :param metaquery: Optional dict with metadata to match on.
        :param limit: Maximum number of results to return.
        :param unique: If set to true, return only unique meter information.
        """
        if limit == 0:
            return
        s_filter = storage.SampleFilter(user=user,
                                        project=project,
                                        meter=meter,
                                        source=source,
                                        metaquery=metaquery,
                                        resource=resource)

        session = self._engine_facade.get_session()

        if s_filter.meter:
            # If a specific meter is provided which is the case for
            # meter based stats report, use a faster query
            subq = session.query(models.Sample.meter_id.label('m_id'),
                                 models.Sample.resource_id.label('res_id'),
                                 models.Meter.name.label('m_name'),
                                 models.Meter.type.label('m_type'),
                                 models.Meter.unit.label('m_unit')).join(
                models.Meter, models.Meter.id == models.Sample.meter_id)
            subq = subq.filter(models.Meter.name == meter)
            subq = subq.subquery()
            query_sample = (session.query(
                func.max(subq.c.m_id).label('id'),
                func.max(subq.c.m_name).label('name'),
                func.max(subq.c.m_type).label('type'),
                func.max(subq.c.m_unit).label('unit'),
                models.Resource.resource_id.label('resource_id'),
                func.max(models.Resource.project_id).label('project_id'),
                func.max(models.Resource.source_id).label('source_id'),
                func.max(models.Resource.user_id).label('user_id'))
                .join(models.Resource, models.Resource.internal_id ==
                      subq.c.res_id))

            # Apply additional filters as required. This specific function
            # skips meter filter, which has been applied in the sub-query,
            # as well as metadata filter. Metadata does not apply to meter
            # related queries.
            query_sample = apply_filter_to_meter_query(query_sample, s_filter)

            query_sample = query_sample.group_by(models.Resource.resource_id)
        else:
            # NOTE(gordc): get latest sample of each meter/resource. we do not
            #          filter here as we want to filter only on latest record.

            subq = session.query(func.max(models.Sample.id).label('id')).join(
                models.Resource,
                models.Resource.internal_id == models.Sample.resource_id)

            if unique:
                subq = subq.group_by(models.Sample.meter_id)
            else:
                subq = subq.group_by(models.Sample.meter_id,
                                     models.Resource.resource_id)

            if resource:
                subq = subq.filter(models.Resource.resource_id == resource)
            subq = subq.subquery()

            # get meter details for samples.
            query_sample = (session.query(models.Sample.meter_id,
                                          models.Meter.name, models.Meter.type,
                                          models.Meter.unit,
                                          models.Resource.resource_id,
                                          models.Resource.project_id,
                                          models.Resource.source_id,
                                          models.Resource.user_id)
                            .join(subq, subq.c.id == models.Sample.id)
                            .join(models.Meter, models.Meter.id ==
                                  models.Sample.meter_id)
                            .join(models.Resource,
                                  models.Resource.internal_id ==
                                  models.Sample.resource_id))

            query_sample = make_query_from_filter(
                session,
                query_sample,
                s_filter,
                require_meter=False
            )

            if s_filter.meter:
                query_sample = query_sample.filter(
                    models.Meter.name == s_filter.meter
                )

        query_sample = query_sample.limit(limit) if limit else query_sample

        if unique:
            for row in query_sample.all():
                yield api_models.Meter(
                    name=row.name,
                    type=row.type,
                    unit=row.unit,
                    resource_id=None,
                    project_id=None,
                    source=None,
                    user_id=None)
        else:
            for row in query_sample.all():
                yield api_models.Meter(
                    name=row.name,
                    type=row.type,
                    unit=row.unit,
                    resource_id=row.resource_id,
                    project_id=row.project_id,
                    source=row.source_id,
                    user_id=row.user_id)

    def get_meter_types(self, user=None, project=None, resource=None,
                        source=None, metaquery=None, limit=None):
        """Return an iterable of api_models.Meter instances. Params are unused

        :param user: Optional ID for user that owns the resource.
        :param project: Optional ID for project that owns the resource.
        :param resource: Optional ID of the resource.
        :param source: Optional source filter.
        :param metaquery: Optional dict with metadata to match on.
        :param limit: Maximum number of results to return.
        """
        if limit == 0:
            return
        session = self._engine_facade.get_session()
        query_meters = (session.query(models.Meter.name,
                                      models.Meter.type,
                                      models.Meter.unit))
        query_meters = query_meters.limit(limit) if limit else query_meters
        for row in query_meters.all():
            yield api_models.Meter(
                name=row.name,
                type=row.type,
                unit=row.unit,
                resource_id=None,
                project_id=None,
                source=None,
                user_id=None)

    @staticmethod
    def _retrieve_samples(query):
        samples = query.all()

        for s in samples:
            # Remove the id generated by the database when
            # the sample was inserted. It is an implementation
            # detail that should not leak outside of the driver.
            yield api_models.Sample(
                source=s.source_id,
                counter_name=s.counter_name,
                counter_type=s.counter_type,
                counter_unit=s.counter_unit,
                counter_volume=s.counter_volume,
                user_id=s.user_id,
                project_id=s.project_id,
                resource_id=s.resource_id,
                timestamp=s.timestamp,
                recorded_at=s.recorded_at,
                resource_metadata=s.resource_metadata,
                message_id=s.message_id,
                message_signature=s.message_signature,
            )

    def get_samples(self, sample_filter, limit=None):
        """Return an iterable of api_models.Samples.

        :param sample_filter: Filter.
        :param limit: Maximum number of results to return.
        """
        if limit == 0:
            return []

        session = self._engine_facade.get_session()
        query = session.query(models.Sample.timestamp,
                              models.Sample.recorded_at,
                              models.Sample.message_id,
                              models.Sample.message_signature,
                              models.Sample.volume.label('counter_volume'),
                              models.Meter.name.label('counter_name'),
                              models.Meter.type.label('counter_type'),
                              models.Meter.unit.label('counter_unit'),
                              models.Resource.source_id,
                              models.Resource.user_id,
                              models.Resource.project_id,
                              models.Resource.resource_metadata,
                              models.Resource.resource_id).join(
            models.Meter, models.Meter.id == models.Sample.meter_id).join(
            models.Resource,
            models.Resource.internal_id == models.Sample.resource_id).order_by(
            models.Sample.timestamp.desc())

        query = make_query_from_filter(
            session,
            query,
            sample_filter,
            require_meter=False
        )
        if sample_filter.meter:
            query = query.filter(models.Meter.name == sample_filter.meter)

        if limit:
            query = query.limit(limit)
        return self._retrieve_samples(query)

    def query_samples(self, filter_expr=None, orderby=None, limit=None):
        if limit == 0:
            return []

        session = self._engine_facade.get_session()
        engine = self._engine_facade.get_engine()
        query = session.query(models.Sample.timestamp,
                              models.Sample.recorded_at,
                              models.Sample.message_id,
                              models.Sample.message_signature,
                              models.Sample.volume.label('counter_volume'),
                              models.Meter.name.label('counter_name'),
                              models.Meter.type.label('counter_type'),
                              models.Meter.unit.label('counter_unit'),
                              models.Resource.source_id,
                              models.Resource.user_id,
                              models.Resource.project_id,
                              models.Resource.resource_metadata,
                              models.Resource.resource_id).join(
            models.Meter, models.Meter.id == models.Sample.meter_id).join(
            models.Resource,
            models.Resource.internal_id == models.Sample.resource_id)
        transformer = sql_utils.QueryTransformer(models.FullSample, query,
                                                 dialect=engine.dialect.name)
        if filter_expr is not None:
            transformer.apply_filter(filter_expr)

        transformer.apply_options(orderby, limit)
        return self._retrieve_samples(transformer.get_query())

    @staticmethod
    def _get_aggregate_functions(aggregate):
        if not aggregate:
            return [f for f in STANDARD_AGGREGATES.values()]

        functions = []

        for a in aggregate:
            if a.func in STANDARD_AGGREGATES:
                functions.append(STANDARD_AGGREGATES[a.func])
            elif a.func in UNPARAMETERIZED_AGGREGATES:
                functions.append(UNPARAMETERIZED_AGGREGATES[a.func])
            elif a.func in PARAMETERIZED_AGGREGATES['compute']:
                validate = PARAMETERIZED_AGGREGATES['validate'].get(a.func)
                if not (validate and validate(a.param)):
                    raise storage.StorageBadAggregate('Bad aggregate: %s.%s'
                                                      % (a.func, a.param))
                compute = PARAMETERIZED_AGGREGATES['compute'][a.func]
                functions.append(compute(a.param))
            else:
                # NOTE(zqfan): We already have checked at API level, but
                # still leave it here in case of directly storage calls.
                msg = _('Invalid aggregation function: %s') % a.func
                raise storage.StorageBadAggregate(msg)

        return functions

    def _make_stats_query(self, sample_filter, groupby, aggregate):
        # don't add meter filter, let the caller handles it

        select = [
            func.min(models.Sample.timestamp).label('tsmin'),
            func.max(models.Sample.timestamp).label('tsmax'),
            models.Meter.unit
        ]
        select.extend(self._get_aggregate_functions(aggregate))

        session = self._engine_facade.get_session()

        if groupby:
            group_attributes = []
            for g in groupby:
                if g != 'resource_metadata.instance_type':
                    group_attributes.append(getattr(models.Resource, g))
                else:
                    group_attributes.append(
                        getattr(models.MetaText, 'value')
                        .label('resource_metadata.instance_type'))

            select.extend(group_attributes)

        query = (
            session.query(*select)
            .join(models.Meter,
                  models.Sample.meter_id == models.Meter.id)
            .join(models.Resource,
                  models.Resource.internal_id == models.Sample.resource_id)
            .group_by(models.Meter.unit))

        if groupby:
            for g in groupby:
                if g == 'resource_metadata.instance_type':
                    query = query.join(
                        models.MetaText,
                        models.Resource.internal_id == models.MetaText.id)
                    query = query.filter(
                        models.MetaText.meta_key == 'instance_type')
            query = query.group_by(*group_attributes)

        return make_query_from_filter(session, query, sample_filter)

    @staticmethod
    def _stats_result_aggregates(result, aggregate):
        stats_args = {}
        if isinstance(result.count, six.integer_types):
            stats_args['count'] = result.count
        for attr in ['min', 'max', 'sum', 'avg']:
            if hasattr(result, attr):
                stats_args[attr] = getattr(result, attr)
        if aggregate:
            stats_args['aggregate'] = {}
            for a in aggregate:
                key = '%s%s' % (a.func, '/%s' % a.param if a.param else '')
                stats_args['aggregate'][key] = getattr(result, key)
        return stats_args

    @staticmethod
    def _stats_result_to_model(result, period, period_start,
                               period_end, groupby, aggregate):
        stats_args = Connection._stats_result_aggregates(result, aggregate)
        stats_args['unit'] = result.unit
        duration = (timeutils.delta_seconds(result.tsmin, result.tsmax)
                    if result.tsmin is not None and result.tsmax is not None
                    else None)
        stats_args['duration'] = duration
        stats_args['duration_start'] = result.tsmin
        stats_args['duration_end'] = result.tsmax
        stats_args['period'] = period
        stats_args['period_start'] = period_start
        stats_args['period_end'] = period_end
        stats_args['groupby'] = (dict(
            (g, getattr(result, g)) for g in groupby) if groupby else None)
        return api_models.Statistics(**stats_args)

    @staticmethod
    def _stats_result_to_model2(
            meter_unit, meter_name, duration_start, duration_end,
            period, period_start, period_end, count, max, sum,
            avg, min
    ):
        stats_args = {
            'count': count, 'max': max, 'sum': sum, 'avg': avg, 'min': min
        }
        stats_args['unit'] = meter_unit
        stats_args['meter_name'] = meter_name
        duration = (timeutils.delta_seconds(duration_start, duration_end)
                    if duration_start is not None and duration_end is not None
                    else None)
        stats_args['duration'] = duration
        stats_args['duration_start'] = duration_start
        stats_args['duration_end'] = duration_end
        stats_args['period'] = period
        stats_args['period_start'] = period_start
        stats_args['period_end'] = period_end
        stats_args['groupby'] = dict()
        return api_models.Statistics(**stats_args)

    def _get_meter(self, meter_name_list):
        session = self._engine_facade.get_session()
        result = {}
        for meter_id, meter_name, meter_unit in session.query(
                models.Meter.id, models.Meter.name, models.Meter.unit
        ).filter(
            models.Meter.name.in_(meter_name_list)
        ):
            # make it a lookup table for performance
            result[str(meter_id)] = {
                'meter_id': meter_id,
                'meter_name': meter_name,
                'meter_unit': meter_unit
                }
        return result

    @staticmethod
    def _generate_meter_statistics_query(sample_filter, meter_ids, period):
        # use custom build query to aggregate by time period.
        # note that result set may have some gaps (i.e, no result for certain
        # period(s))
        my_query = SimpleQueryGenerator()
        start_time = sample_filter.start_timestamp
        end_time = sample_filter.end_timestamp
        grp_col = 'floor((extract(epoch from sample.timestamp) - ' \
                  'extract(epoch from timestamp :start_time)) / ' \
                  ':period) as grp'

        my_query.columns.extend([
            'sample.meter_id as meter_id',
            'min(sample.timestamp) AS tsmin',
            'max(sample.timestamp) AS tsmax',
            'count(sample.volume) AS cnt',
            'max(sample.volume) AS max',
            'sum(sample.volume) AS sum',
            'avg(sample.volume) AS avg',
            'min(sample.volume) AS min',
            grp_col
        ])

        my_query.froms.append('sample')
        my_query.joins.append(
            'join resource on resource.internal_id = sample.resource_id'
        )
        my_query.wheres.extend([
            'sample.timestamp >= :start_time',
            'sample.timestamp <= :end_time'
        ])

        idx = 0
        in_list = []
        for meter_id in meter_ids:
            idx += 1
            m_id_param = 'meter_id_%s' % idx
            in_list.append(':' + m_id_param)
            my_query.args[m_id_param] = meter_id

        in_clause = 'sample.meter_id in (%s)' % ','.join(in_list)
        my_query.wheres.append(in_clause)

        my_query.groups.append('sample.meter_id')
        my_query.groups.append('grp')
        my_query.orders.append('grp')
        my_query.args['period'] = period
        my_query.args['start_time'] = start_time
        my_query.args['end_time'] = end_time

        add_stats_filter_to_query(my_query, sample_filter)
        return my_query

    def get_meter_statistics(self, sample_filter, period=None, groupby=None,
                             aggregate=None):
        """Return an iterable of api_models.Statistics instances.

        Items are containing meter statistics described by the query
        parameters. The filter must have a meter value set.
        """

        # by definition, a meter filter is required
        # it was originally from _make_stats_query, move it to the top of
        # function to be explicit
        if not sample_filter:
            return
        if not sample_filter.meter:
            return

        meter_list = sample_filter.meter \
            if isinstance(sample_filter.meter, list) \
            else [sample_filter.meter]

        if groupby:
            for group in groupby:
                if group not in ['user_id', 'project_id', 'resource_id',
                                 'resource_metadata.instance_type']:
                    raise ceilometer.NotImplementedError('Unable to group by '
                                                         'these fields')

        meter_list_dict = self._get_meter(meter_list)
        meter_id_list = [m['meter_id'] for m in meter_list_dict.itervalues()]
        if len(meter_id_list) == 0:
            return

        if not period:
            q = self._make_stats_query(sample_filter, groupby, aggregate)
            q = q.filter(
                models.Sample.meter_id.in_(meter_id_list)
            )
            for res in q:
                if res.count:
                    yield self._stats_result_to_model(res, 0,
                                                      res.tsmin, res.tsmax,
                                                      groupby,
                                                      aggregate)
            return

        if not (sample_filter.start_timestamp and sample_filter.end_timestamp):
            q = self._make_stats_query(sample_filter, None, aggregate)
            q = q.filter(
                models.Sample.meter_id.in_(meter_id_list)
            )
            res = q.first()
            if not res:
                # NOTE(liusheng):The 'res' may be NoneType, because no
                # sample has found with sample filter(s).
                return

        query = self._make_stats_query(sample_filter, groupby, aggregate)
        if not query:
            return

        if not sample_filter.start_timestamp:
            sample_filter.start_timestamp = res.tsmin
        if not sample_filter.end_timestamp:
            sample_filter.end_timestamp = res.tsmax

        my_query = self._generate_meter_statistics_query(
            sample_filter, meter_list_dict.keys(), period
        )

        sql = my_query.get_sql()
        select_stm = text(sql)

        start_time = sample_filter.start_timestamp
        engine = self._engine_facade.get_engine()
        with engine.begin() as conn:
            result = conn.execute(select_stm, **my_query.args)
            for row in result.fetchall():
                grp_idx = row.grp
                p_start = start_time + datetime.timedelta(0, period * grp_idx)
                p_end = p_start + datetime.timedelta(0, period)
                meter = meter_list_dict[str(row.meter_id)]
                r = self._stats_result_to_model2(
                    meter_unit=meter['meter_unit'],
                    meter_name=meter['meter_name'],
                    duration_start=row.tsmin,
                    duration_end=row.tsmax,
                    period=period,
                    period_start=p_start,
                    period_end=p_end,
                    count=row.cnt,
                    max=row.max,
                    sum=row.sum,
                    avg=row.avg,
                    min=row.min
                )
                yield r
            result.close()

    def get_resources_batch(self, resource_ids):
        """Return an iterable of api_models.Resource instances.

        :param resource_ids: Mandatory list of resource IDs.
        """
        # WRS extension to the OpenStack Ceilometer SQL storage
        # connection class. It provides an effective method to retrieve
        # resource info for a batch of resource ids. This method should be
        # used when the client does not specify user, project, source,
        # metaquery, and start/end timestamp range in the request.

        if resource_ids:
            resids = ','.join(resource_ids)
            engine = self._engine_facade.get_engine()
            with engine.connect() as con:
                stmt = text('SELECT * from resource_getbatch(:input_ids)')
                results = con.execute(stmt, input_ids=resids)
            for r in results.fetchall():
                yield api_models.Resource(
                    resource_id=r.resource_id,
                    project_id=r.project_id,
                    first_sample_timestamp=None,
                    last_sample_timestamp=None,
                    source=r.source_id,
                    user_id=r.user_id,
                    metadata=jsonutils.loads(r.resource_metadata)
                )
        else:
            raise RuntimeError('Missing required list of resource ids')
