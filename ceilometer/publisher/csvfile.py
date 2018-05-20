# -*- encoding: utf-8 -*-
###############################################################################
# CSVFilePublisher is a derivative work of FilePublisher.  Below is its license
###############################################################################
#
# Copyright 2013 IBM Corp
#
# Auth: Tong Li <litong01@us.ibm.com>
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
##############################################################################
# CompressingRotatingFileHandler
# derived from logging.handlers.RotatingFileHandler
# Below is its license
##############################################################################

# Copyright 2001-2010 by Vinay Sajip. All Rights Reserved.
#
# Permission to use, copy, modify, and distribute this software and its
# documentation for any purpose and without fee is hereby granted,
# provided that the above copyright notice appear in all copies and that
# both that copyright notice and this permission notice appear in
# supporting documentation, and that the name of Vinay Sajip
# not be used in advertising or publicity pertaining to distribution
# of the software without specific, written prior permission.
# VINAY SAJIP DISCLAIMS ALL WARRANTIES WITH REGARD TO THIS SOFTWARE, INCLUDING
# ALL IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL
# VINAY SAJIP BE LIABLE FOR ANY SPECIAL, INDIRECT OR CONSEQUENTIAL DAMAGES OR
# ANY DAMAGES WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER
# IN AN ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT
# OF OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.

"""
Additional handlers for the logging package for Python. The core package is
based on PEP 282 and comments thereto in comp.lang.python, and influenced by
Apache's log4j system.

Copyright (C) 2001-2010 Vinay Sajip. All Rights Reserved.

To use, simply 'import logging.handlers' and log away!
"""


import csv
import gzip
import io
import logging
import logging.handlers
import os
import six
import urlparse

import ceilometer
from ceilometer import publisher

from oslo_concurrency import lockutils
from oslo_config import cfg
from oslo_log import log

LOG = log.getLogger(__name__)


CSV_METER_PUBLISH_OPTS = [
    cfg.StrOpt('csv_location',
               default='',
               help='Starting path location for csv files.',
               ),
    cfg.BoolOpt('csv_location_strict',
                default=False,
                help='Flag to enforce a starting path location for csv files.'
                ),
]


def str2bool(v):
    return v.lower() in ("yes", "true", "t", "1")


class CompressingRotatingFileHandler(logging.handlers.RotatingFileHandler):

    """Derived from RotatingFileHandler

    does a compress phase when rolling files
    Never leave the stream open.  Ceilometer forks child processes
    and this handler does not deal well with a child process rotating files
    """
    def __init__(self, filename, mode='a', maxBytes=0, backupCount=0,
                 encoding=None, delay=1, compression='gzip', compress=True):
        # Set the delay flag. We do not want the parent to open the stream
        super(CompressingRotatingFileHandler, self).__init__(filename,
                                                             mode,
                                                             maxBytes,
                                                             backupCount,
                                                             encoding,
                                                             delay)
        self.compress = compress
        if self.compress:
            self.compressExtension = '.gz'
        else:
            self.compressExtension = ''

    # WRS: Do not let stream remain open. Forked child processes may
    # rotate the file and make the parent handle invalid
    def shouldRollover(self, record):
        # WRS. handler superclass opens the stream. It needs to be closed.
        ret = super(CompressingRotatingFileHandler,
                    self).shouldRollover(record)
        if self.stream:
            self.stream.close()
            self.stream = None
        return ret

    # WRS: Do not let stream remain open. Forked child processes may
    # rotate the file and make the parent handle invalid
    def emit(self, record):
        super(CompressingRotatingFileHandler, self).emit(record)
        if self.stream:
            self.stream.flush()
            self.stream.close()
            self.stream = None

    def doRollover(self):
        """Does the same as RotatingFileHandler except

        it compresses the file and adds compression extension
        """
        if self.stream:
            self.stream.flush()
            self.stream.close()
            self.stream = None
        if self.backupCount > 0:
            for i in range(self.backupCount - 1, 0, -1):
                sfn = "%s.%d%s" % (self.baseFilename,
                                   i,
                                   self.compressExtension)
                dfn = "%s.%d%s" % (self.baseFilename,
                                   i + 1,
                                   self.compressExtension)
                if os.path.exists(sfn):
                    if os.path.exists(dfn):
                        os.remove(dfn)
                    os.rename(sfn, dfn)

            # Do compression here.
            sfn = self.baseFilename
            dfn = self.baseFilename + ".1" + self.compressExtension
            if os.path.exists(dfn):
                os.remove(dfn)
            if self.compress:
                with open(sfn, 'rb') as orig_file:
                    with gzip.open(dfn, 'wb') as zipped_file:
                        zipped_file.writelines(orig_file)
            else:
                os.rename(sfn, dfn)
        # Re-create
        # WRS: Do not let stream remain open. Forked child processes may
        # rotate the file and make the parent handle invalid
        os.remove(self.baseFilename)
        self.mode = 'a'
        self.stream = self._open()
        self.stream.close()
        self.stream = None


class CSVFilePublisher(publisher.ConfigPublisherBase):
    """Publisher metering data to file.

    Based on FilePublisher (copyright license included at top of this file)

    The publisher which records metering data into a csv formatted file.
    The file name and location are configured in ceilometer pipeline file.
    If a file name and location is not specified, this publisher will not
    log any meters other than log a warning in Ceilometer log file.

    If csv_location_strict is True in config, then only locations that start
    with the path set in config for csv_location will be considered valid

    To enable this publisher, add the following section to file
    /etc/ceilometer/publisher.yaml or simply add it to an existing pipeline.

        -
            name: csv_file
            interval: 600
            counters:
                - "*"
            transformers:
            publishers:
                - csvfile:///var/test.csv?max_bytes=10000000&backup_count=5

    File path is required for this publisher to work properly.
    max_bytes AND  backup_count required to rotate files
    compress will indicate if the rotated logs are compressed
    enabled can be used to turn off this publisher

    """

    KEY_DELIM = '::'
    NESTED_DELIM = '__'
    ORDERED_KEYS = ['project_id',
                    'user_id',
                    'name',
                    'resource_id',
                    'timestamp',
                    'volume',
                    'unit',
                    'type',
                    'source',
                    'id']
    SUB_DICT_KEYS = ['resource_metadata']

    def __init__(self, conf, parsed_url):
        super(CSVFilePublisher, self).__init__(conf, parsed_url)

        self.is_enabled = True
        self.compress = True
        self.location = ''
        self.max_bytes = 0
        self.backup_count = 0
        self.rfh = None
        self.publisher_logger = None
        self.location = parsed_url.path

        if not self.location or self.location.lower() == 'csvfile':
            LOG.error('The path for the csvfile publisher is required')
            return
        if self.conf.publisher_csvfile.csv_location_strict:
            # Eliminate "../" in the location
            self.location = os.path.abspath(self.location)
            if (not self.location.startswith(
               self.conf.publisher_csvfile.csv_location)):
                LOG.error(
                    'The location %s for the csvfile must start with %s'
                    % (self.location, self.conf.publisher_csvfile.csv_location)
                )
                return

        # Handling other configuration options in the query string
        if parsed_url.query:
            params = urlparse.parse_qs(parsed_url.query)
            if params.get('backup_count'):
                try:
                    self.backup_count = int(params.get('backup_count')[0])
                except ValueError:
                    LOG.error('max_bytes should be a number.')
                    return
            if params.get('max_bytes'):
                try:
                    self.max_bytes = int(params.get('max_bytes')[0])
                    if self.max_bytes < 0:
                        LOG.error('max_bytes must be >= 0.')
                        return
                except ValueError:
                    LOG.error('max_bytes should be a number.')
                    return
            if params.get('compress'):
                try:
                    self.compress = str2bool(params.get('compress')[0])
                except ValueError:
                    LOG.error('compress should be a bool.')
                    return
            if params.get('enabled'):
                try:
                    self.is_enabled = str2bool(params.get('enabled')[0])
                except ValueError:
                    LOG.error('enabled should be a bool.')
                    return

        self.setup_logger()

    def setup_logger(self):
        # create compressable rotating file handler
        self.rfh = CompressingRotatingFileHandler(
            self.location,
            maxBytes=self.max_bytes,
            backupCount=self.backup_count,
            compression='gzip',
            compress=self.compress)
        self.publisher_logger = logging.Logger('publisher.csvfile')
        self.publisher_logger.propagate = False
        self.publisher_logger.setLevel(logging.INFO)
        self.rfh.setLevel(logging.INFO)
        self.publisher_logger.addHandler(self.rfh)

    def as_yaml(self):
        return 'csvfile://' + (self.location
                               + '?max_bytes=' + str(self.max_bytes)
                               + '&backup_count=' + str(self.backup_count)
                               + '&compress=' + str(self.compress)
                               + '&enabled=' + str(self.is_enabled))

    def change_settings(self,
                        enabled=None,
                        location=None,
                        max_bytes=None,
                        backup_count=None,
                        compress=None):
        if enabled is not None:
            if enabled != self.is_enabled:
                self.is_enabled = enabled

        if compress is not None:
            if compress != self.compress:
                self.compress = compress

        if location is not None:
            if location != self.location:
                self.location = location

        if max_bytes is not None:
            if max_bytes != self.max_bytes:
                self.max_bytes = max_bytes

        if backup_count is not None:
            if backup_count != self.backup_count:
                self.backup_count = backup_count
        self.setup_logger()

    def aggregate(self, k, v):
        # Stick the key and value together and add to the list.
        # Make sure we return empty string instead of 'None'
        return k + self.KEY_DELIM + (six.text_type(v) if v is not None else '')

    def convert_to_list(self, some_dict, prefix=None):
        # Stick the key and value together and add to the list.
        formatted_list = []
        if some_dict is not None:
            for k, v in some_dict.iteritems():
                new_k = (prefix + self.NESTED_DELIM + k if prefix else k)
                if type(v) is dict:
                    formatted_list.extend(self.convert_to_list(v, new_k))
                else:
                    formatted_list.append(self.aggregate(new_k, v))
        return formatted_list

    def convert_to_ordered_list(self, some_dict):
        """Convert a sample to a list in a specific order."""
        formatted_list = []
        for key in self.ORDERED_KEYS:
            formatted_list.append(self.aggregate(key, some_dict.get(key)))
        for key in self.SUB_DICT_KEYS:
            formatted_list.extend(self.convert_to_list(some_dict.get(key),
                                                       key))
        return formatted_list

    def format_sample(self, sample):
        """Convert a sample to a CSV formatted string

        :param sample: Sample from pipeline after transformation
        """
        csv_handle = io.BytesIO()
        w = csv.writer(csv_handle)
        formatted_list = self.convert_to_ordered_list(sample.as_dict())
        w.writerow([u.encode('utf-8') for u in formatted_list])
        return csv_handle.getvalue().strip()

    def publish_samples(self, samples):
        """Publish the samples to csv formatted output

        :param samples: Samples from pipeline after transformation
        """
        with lockutils.lock(self.conf.host, 'csv-publish-samples-',
                            external=True, lock_path='/tmp/'):
            if self.is_enabled:
                if self.publisher_logger:
                    for sample in samples:
                        self.publisher_logger.info(self.format_sample(sample))

    def publish_events(self, events):
        """Send an event message for publishing

        :param events: events from pipeline after transformation
        """
        raise ceilometer.NotImplementedError
