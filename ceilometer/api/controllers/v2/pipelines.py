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
from ceilometer.api.controllers.v2 import base
from ceilometer.i18n import _
from ceilometer import pipeline as pipeline_t
from ceilometer.publisher import csvfile
import os
from oslo_log import log
import pecan
from pecan import rest
from stevedore import extension
import subprocess
import wsme
from wsme import types as wtypes
import wsmeext.pecan as wsme_pecan
import yaml

LOG = log.getLogger(__name__)

P_TYPE = {
    'name': 'sample',
    'pipeline': pipeline_t.SamplePipeline,
    'source': pipeline_t.SampleSource,
    'sink': pipeline_t.SampleSink
}


class Pipeline(base.Base):
    """An externally defined object for pipeline. Corresponds to a SINK

    """

    name = wtypes.text
    enabled = bool
    location = wtypes.text
    max_bytes = int
    backup_count = int
    compress = bool

    "The unique name for the pipeline"

    def __init__(self, rule=None, **kwargs):
        super(Pipeline, self).__init__(**kwargs)

    def __repr__(self):
        # for logging calls
        return "Pipeline Sink(%s)" % self.name

    @staticmethod
    def validate(pipeline):
        valid_fields = ['name',
                        'enabled',
                        'location',
                        'max_bytes',
                        'backup_count',
                        'compress']
        for field in valid_fields:
            if getattr(pipeline, field) is None:
                error = _("%s is mandatory") % field
                pecan.response.translatable_error = error
                raise wsme.exc.ClientSideError(unicode(error))

        if pecan.request.cfg.publisher_csvfile.csv_location_strict:
            # Use abspath to expand sneaky things like ../ in the path
            loc_val = os.path.abspath(getattr(pipeline, 'location'))
            if not loc_val.startswith(pecan.request.cfg.
                                      publisher_csvfile.csv_location):
                fmt = _("%s violates the location constraint in config")
                error = fmt % loc_val
                pecan.response.translatable_error = error
                raise wsme.exc.ClientSideError(unicode(error))

        # we cannot have negative values for max_bytes
        if getattr(pipeline, 'max_bytes') < 0:
            error = _("max_bytes must be >= 0.")
            pecan.response.translatable_error = error
            raise wsme.exc.ClientSideError(unicode(error))

        return pipeline

    @classmethod
    def from_dict(cls, some_dict):
        return cls(**(some_dict))

    @classmethod
    def sample(cls):
        return cls(name='Sample')


class PipelinesController(rest.RestController):
    """Works on pipelines.

         Caveat: we do not support MULTIPLE CSV per sink
    """

    def format_as_dict(self, sink, csvpublisher):
        some_dict = {}
        some_dict['name'] = sink.name
        some_dict['enabled'] = csvpublisher.is_enabled
        some_dict['location'] = csvpublisher.location
        some_dict['max_bytes'] = csvpublisher.max_bytes
        some_dict['backup_count'] = csvpublisher.backup_count
        some_dict['compress'] = csvpublisher.compress
        return some_dict

    def get_pipelines_from_manager(self):
        pipeline_cfg = {}
        pipelines = pecan.request.pipeline_manager.pipelines
        for p in pipelines:
            for pb in p.sink.publishers:
                if isinstance(pb, csvfile.CSVFilePublisher):
                    # If it is the same sink, we can just overwrite it
                    pipeline_cfg[p.sink.name] = self.format_as_dict(p.sink, pb)
        return pipeline_cfg.values()

    def get_pipelines_from_cfg(self, conf, cfg):
        # This code is based on the constructor in pipeline.py
        transformer_manager = extension.ExtensionManager(
            'ceilometer.transformer')
        publisher_manager = pipeline_t.PublisherManager(
            conf, P_TYPE['name'])
        new_pipelines = []
        if 'sources' in cfg or 'sinks' in cfg:
            if not ('sources' in cfg and 'sinks' in cfg):
                raise pipeline_t.PipelineException(
                    "Both sources & sinks are required", cfg)
            sources = [P_TYPE['source'](s) for s in cfg.get('sources', [])]
            sinks = dict((s['name'], P_TYPE['sink'](conf, s,
                         transformer_manager, publisher_manager))
                         for s in cfg.get('sinks', []))
            for source in sources:
                source.check_sinks(sinks)
                for target in source.sinks:
                    new_pipelines.append(
                        P_TYPE['pipeline'](conf, source, sinks[target]))
        else:
            for pipedef in cfg:
                source = P_TYPE['source'](pipedef)
                sink = P_TYPE['sink'](conf, pipedef,
                                      transformer_manager, publisher_manager)
                new_pipelines.append(P_TYPE['pipeline'](conf, source, sink))
        return new_pipelines

    def get_pipelines_from_yaml(self):
        pipeline_cfg = {}

        yaml_cfg = []
        conf = pecan.request.cfg
        cfg_file = conf.pipeline_cfg_file
        if not os.path.exists(cfg_file):
            cfg_file = conf.find_file(cfg_file)
        with open(cfg_file) as fap:
            filedata = fap.read()
            yaml_cfg = yaml.safe_load(filedata)

        # Parse and convert to Pipeline objects
        pipelines = self.get_pipelines_from_cfg(conf, yaml_cfg)
        for p in pipelines:
            for pb in p.sink.publishers:
                if isinstance(pb, csvfile.CSVFilePublisher):
                    # If it is the same sink, we can just overwrite it
                    pipeline_cfg[p.sink.name] = self.format_as_dict(p.sink, pb)
        return pipeline_cfg.values()

    def get_pipelines(self):
        return self.get_pipelines_from_yaml()

    @wsme_pecan.wsexpose(Pipeline, unicode)
    def get_one(self, pipeline_name):
        """Retrieve details about one pipeline.

        :param pipeline_name: The name of the pipeline.
        """
        # authorized_project = acl.get_limited_to_project(
        #                           pecan.request.headers)
        pipelines = self.get_pipelines()
        for p in pipelines:
            # The pipeline name HERE is the same as the sink name
            if p.get('name', None) == pipeline_name:
                return Pipeline.from_dict(p)
        raise base.EntityNotFound(_('Pipeline'), pipeline_name)

    @wsme_pecan.wsexpose([Pipeline], [base.Query])
    def get_all(self, q=[]):
        """Retrieve definitions of all of the pipelines.

        :param q: Filter rules for the pipelines to be returned.   unused
        """
        # authorized_project = acl.get_limited_to_project(
        #                           pecan.request.headers)
        # kwargs =
        # _query_to_kwargs(q, pecan.request.storage_conn.get_resources)
        pipelines = self.get_pipelines()
        pipeline_list = [Pipeline.from_dict(p) for p in pipelines]
        return pipeline_list

    @wsme.validate(Pipeline)
    @wsme_pecan.wsexpose(Pipeline, wtypes.text, body=Pipeline)
    def put(self, data):
        """Modify this pipeline."""
        # authorized_project = acl.get_limited_to_project(
        #                           pecan.request.headers)
        # note(sileht): workaround for
        # https://bugs.launchpad.net/wsme/+bug/1220678
        Pipeline.validate(data)

        # Get the matching csv publisher for the pipeline.
        yaml_cfg = []
        conf = pecan.request.cfg
        cfg_file = conf.pipeline_cfg_file
        if not os.path.exists(cfg_file):
            cfg_file = conf.find_file(cfg_file)
        with open(cfg_file) as fap:
            filedata = fap.read()
            yaml_cfg = yaml.safe_load(filedata)

        # Parse and convert to Pipeline objects
        pipelines = self.get_pipelines_from_cfg(conf, yaml_cfg)

        csv = None
        for p in pipelines:
            if p.sink.name == data.name:
                for pb in p.sink.publishers:
                    if isinstance(pb, csvfile.CSVFilePublisher):
                        csv = pb
                        break

        if not csv:
            raise base.EntityNotFound(_('Pipeline'), data.name)

        # Alter the settings.
        csv.change_settings(enabled=data.enabled,
                            location=data.location,
                            max_bytes=data.max_bytes,
                            backup_count=data.backup_count,
                            compress=data.compress)
        new_csv = csv.as_yaml()

        # Set new_csv as the matching csvfile publisher for the sink
        sinks = yaml_cfg.get('sinks', [])
        for sink in sinks:
            if sink.get('name') == data.name:
                publishers = sink.get('publishers', [])
                for index, item in enumerate(publishers):
                    if item.strip().lower().startswith('csvfile:'):
                        publishers[index] = new_csv

        # Re-process in-memory version of yaml to prove it is still good
        # Should show throw an exception if syntax became invalid

        pipelines = self.get_pipelines_from_cfg(conf, yaml_cfg)

        # Must be outputted as a single yaml (ie: use dump ,not dump_all)
        stream = file(cfg_file, 'w')
        # Write a YAML representation of data to 'document.yaml'.
        yaml.safe_dump(yaml_cfg,
                       stream,
                       default_flow_style=False)
        stream.close()

        # Emit SIGHUP to all ceilometer processes to re-read config
        hup_list = ['ceilometer-collector',
                    'ceilometer-agent-notification']
        for h in hup_list:
            p1 = subprocess.Popen(['pgrep', '-f', h], stdout=subprocess.PIPE)
            subprocess.Popen(['xargs', '--no-run-if-empty', 'kill', '-HUP'],
                             stdin=p1.stdout, stdout=subprocess.PIPE)
        pecan.request.pipeline_manager = \
            pipeline_t.setup_pipeline(pecan.request.cfg)
        # Re-parse and return value from file
        # This protects us if the file was not stored properly
        pipelines = self.get_pipelines()
        for p in pipelines:
            if p.get('name', None) == data.name:
                return Pipeline.from_dict(p)
        # If we are here, the yaml got corrupted somehow
        raise wsme.exc.ClientSideError(unicode('pipeline yaml is corrupted'))
