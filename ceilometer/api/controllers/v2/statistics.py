#
# Copyright 2012 New Dream Network, LLC (DreamHost)
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
# Copyright (c) 2013-2015 Wind River Systems, Inc.
#
import pecan
from pecan import rest
from wsme import types as wtypes
import wsmeext.pecan as wsme_pecan

from ceilometer.api.controllers.v2 import base
from ceilometer.api.controllers.v2 import meters
from ceilometer.api.controllers.v2.meters import Aggregate
from ceilometer.api.controllers.v2 import utils as v2_utils
from ceilometer.api import rbac
from ceilometer.i18n import _
from ceilometer import storage
from ceilometer import utils

from oslo_log import log
from oslo_utils import timeutils

LOG = log.getLogger(__name__)


class ScopedStatistics(meters.Statistics):

    meter_name = wtypes.text
    "The unique name for the meter"

    def __init__(self, meter_name,
                 start_timestamp=None, end_timestamp=None, **kwds):
        kwds['meter_name'] = meter_name
        kwds['start_timestamp'] = start_timestamp
        kwds['end_timestamp'] = end_timestamp
        super(ScopedStatistics, self).__init__(**kwds)

    def __repr__(self):
        # for logging calls
        return "ScopedStats(%s) " % self.meter_name


class StatisticsController(rest.RestController):

    @wsme_pecan.wsexpose([ScopedStatistics],
                         [base.Query], [unicode], [unicode], int, [Aggregate])
    def get_all(self,
                q=None, meter=None, groupby=None, period=None, aggregate=None):
        """Retrieve all statistics for all meters

        :param q: Filter rules for the statistics to be returned.
        """
        rbac.enforce('compute_statistics', pecan.request)

        q = q or []
        meter = meter or []
        groupby = groupby or []
        aggregate = aggregate or []

        if period and period < 0:
            raise base.ClientSideError(_("Period must be positive."))

        g = meters._validate_groupby_fields(groupby)

        # TO DO:  break out the meter names and invoke multiple calls
        kwargs = v2_utils.query_to_kwargs(q, storage.SampleFilter.__init__)

        aggregate = utils.uniq(aggregate, ['func', 'param'])
        # Find the original timestamp in the query to use for clamping
        # the duration returned in the statistics.
        start = end = None
        for i in q:
            if i.field == 'timestamp' and i.op in ('lt', 'le'):
                end = timeutils.parse_isotime(i.value).replace(
                    tzinfo=None)
            elif i.field == 'timestamp' and i.op in ('gt', 'ge'):
                start = timeutils.parse_isotime(i.value).replace(
                    tzinfo=None)
        ret = []

        kwargs['meter'] = meter
        f = storage.SampleFilter(**kwargs)
        try:
            computed = pecan.request.storage_conn.get_meter_statistics(
                f, period, g, aggregate)
            dbStats = [ScopedStatistics(start_timestamp=start,
                                        end_timestamp=end,
                                        **c.as_dict())
                       for c in computed]
            ret += dbStats
        except OverflowError:
            LOG.exception("Problem processing meters %s" % meter)

        return ret
