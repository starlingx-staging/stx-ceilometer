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
from ceilometer.api.controllers.v2.meters import MeterController
from ceilometer.api.controllers.v2 import utils as v2_utils
from ceilometer.api import rbac
from ceilometer import sample

from oslo_log import log


LOG = log.getLogger(__name__)


class MeterType(base.Base):
    """The minimal representation for a meter"""

    name = wtypes.text
    "The unique name for the meter"

    type = wtypes.Enum(str, *sample.TYPES)
    "The meter type (see :ref:`measurements`)"

    unit = wtypes.text
    "The unit of measure"

    def __init__(self, **kwargs):
        super(MeterType, self).__init__(**kwargs)

    def __repr__(self):
        # for logging calls
        return "MeterType (%s)" % self.name

    # Next 3 functions are required for uniqueness
    def __eq__(self, other):
        if isinstance(other, MeterType):
            return self.name == other.name
        else:
            return False

    def __ne__(self, other):
        return (not self.__eq__(other))

    def __hash__(self):
        return hash(self.__repr__())

    @classmethod
    def from_dict(cls, some_dict):
        return cls(**(some_dict))

    @classmethod
    def sample(cls):
        return cls(name='Sample')


class MeterTypesController(rest.RestController):
    """Works on metertypes."""

    def get_metertypes(self):
        return []

    # Allow querying meter details about a specific meter
    @pecan.expose()
    def _lookup(self, meter_name, *remainder):
        return MeterController(meter_name), remainder

    @wsme_pecan.wsexpose([MeterType], [base.Query], int)
    def get_all(self, q=[], limit=None):
        """Return all known meters, based on the data recorded so far.

        :param q: Filter rules for the meters to be returned.
        :param limit: limit of the number of meter types to return.
        """

        rbac.enforce('get_meters', pecan.request)

        q = q or []
        # Timestamp field is not supported for Meter queries
        lim = v2_utils.enforce_limit(limit)
        kwargs = v2_utils.query_to_kwargs(
            q, pecan.request.storage_conn.get_meter_types,
            ['limit'], allow_timestamps=False)
        return [MeterType.from_db_model(m)
                for m in pecan.request.storage_conn.get_meter_types(limit=lim,
                                                                    **kwargs)]
