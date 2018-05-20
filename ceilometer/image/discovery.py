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

import glanceclient
from oslo_config import cfg

from ceilometer.agent import plugin_base
from ceilometer import keystone_client

SERVICE_OPTS = [
    cfg.StrOpt('glance',
               default='image',
               help='Glance service type.'),
]


def _get_region_name(conf, service_type):
    reg = conf.region_name_for_services
    # If Shared Services configured, override region for image/volumes
    shared_services_region_name = conf.region_name_for_shared_services
    shared_services_types = conf.shared_services_types
    if shared_services_region_name:
        if service_type in shared_services_types:
            reg = shared_services_region_name
    return reg


class ImagesDiscovery(plugin_base.DiscoveryBase):
    def __init__(self, conf):
        super(ImagesDiscovery, self).__init__(conf)
        creds = conf.service_credentials
        self.glance_client = glanceclient.Client(
            version='2',
            session=keystone_client.get_session(conf),
            region_name=_get_region_name(conf, conf.service_types.glance),
            interface=creds.interface,
            service_type=conf.service_types.glance)

    def discover(self, manager, param=None):
        """Discover resources to monitor."""
        return self.glance_client.images.list()
