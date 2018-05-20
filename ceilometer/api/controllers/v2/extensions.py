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
from pecan import rest
from wsme import types as wtypes
import wsmeext.pecan as wsme_pecan

from ceilometer.api.controllers.v2 import base


class Extension(base.Base):
    """A representation of the extension."""

    namespace = wtypes.text
    "namespace of extension"

    name = wtypes.text
    "name of extension"

    description = wtypes.text
    "description of extension"

    updated = wtypes.text
    "updated time of extension"

    alias = wtypes.text
    "alias of extension"

    links = [wtypes.text]
    "links of extension"

    @classmethod
    def from_db_model(cls, m):
        return cls(namespace=m['namespace'],
                   name=m['name'],
                   description=m['description'],
                   updated=m['updated'],
                   alias=m['alias'],
                   links=m['links'])


class ExtensionList(base.Base):
    """A representation of extensions."""

    extensions = [Extension]
    "list of extensions"

    @classmethod
    def from_db_model(cls, m):
        return cls(extensions=m)


class ExtensionsController(rest.RestController):
    """Manages extensions queries."""

    @wsme_pecan.wsexpose(ExtensionList)
    def get(self):
        ext_data = {}
        ext_data['namespace'] = ("http://docs.windriver.com/tis/ext/"
                                 "pipelines/v1")
        ext_data['name'] = "pipelines"
        ext_data['description'] = ("Windriver Telemetry Pipelines"
                                   " for managing the writing of"
                                   " Ceilometer PMs to a"
                                   " Comma-Separated-Value file.")
        ext_data['updated'] = "2014-10-01T12:00:00-00:00"
        ext_data['alias'] = "wrs-pipelines"
        ext_data['links'] = []
        ext = Extension.from_db_model(ext_data)
        ext_list = []
        ext_list.append(ext)
        return ExtensionList.from_db_model(ext_list)
