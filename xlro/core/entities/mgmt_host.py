# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from os import path
from types import ModuleType
from typing import Dict, Any

import paramiko.ssh_exception

from xlro.core.entities.base import *
from xlro.core.entities import Host, Service
from xlro.core.util.general_utils import parse_js_conf


@entity(sourcetypes=[SourceTypes.MANAGEMENT, SourceTypes.OS])
class MgmtHost(Host):
    version : str = PropertySpec(str)
    mgmt_git_info : dict = PropertySpec(dict)
    _mgmt_js_config : dict = {}
    _mongo_shell : str = None
    PKG_NAME = 'nvmesh-management'
    MGMT_DIR = '/opt/nvmesh/management/'
    pkg_info = path.join(MGMT_DIR, 'version')
    MGMT_JS_CONF = '/etc/nvmesh/management.js.conf'
    DEFAULT_NVMESH_CERTS_DIR = '/etc/nvmesh/tls'

    def __init__(self, *args, **kwargs):
        super(MgmtHost, self).__init__(*args, **kwargs)
        self.services = {
                'mgr': Service.instance(name='nvmeshmgr', host=self, pid_path='nvmeshmgr/management'),
                'mongod': Service.instance(name='mongod', host=self)
            }

    @prop_loader(SourceTypes.OS, ['version'])
    def load_version_from_proc(self):
        return { 'version': self.package_version(self.PKG_NAME) }

    @prop_loader(SourceTypes.OS, ['mgmt_git_info'])
    def load_mgmt_git_info(self):
        info: Dict[str, Any] = {}
        exec(self.proc_content(self.pkg_info, no_cache=True), {}, info)
        return {'mgmt_git_info': info}

    def drop_database(self, full_drop=True):
        try:
            mongod_service = self.services['mongod']
            if mongod_service.status() == Service.STATUS.DOWN:
                mongod_service.start()

            drop_args = '--eval "db.dropDatabase()"' if full_drop else path.join(self.MGMT_DIR, 'clearDB.js')
            drop_cmd = f"{self.mongo_shell} management {drop_args}"
            _, err, code = self.connection.execute(drop_cmd)
            assert not code, "Unable to drop Mongo database due to: {}".format(err)
        except KeyError:
            self.logger.info('Unable to drop Mongo database - No mongod service available')

    def delete_kafka_topics(self):
        kafka_brokers = self.mgmt_js_config['kafkaConnection']['hosts']
        expected_return_codes = (0,1) # 0 - success, 1 - no topics found
        self.execute("/opt/kafka/bin/kafka-topics.sh --bootstrap-server localhost:9092 --delete --topic '.*'",
                timeout=10, success=expected_return_codes, desc='delete-kafka-topics')

    @property
    def mgmt_js_config(self):
        if not self._mgmt_js_config:
            try:
                self.logger.debug('fetching mgmt js conf')
                self._mgmt_js_config = parse_js_conf(self.execute(f'cat {self.MGMT_JS_CONF}')[0])
            except Exception as e:
                self.logger.debug(f'Failed to fetch mgmt js conf on {self.name}')
                raise e
        return self._mgmt_js_config

    @property
    def mongo_shell(self):
        if not self._mongo_shell:
            try:
                self._mongo_shell = self.connection.execute('command  -v mongosh &> /dev/null && echo mongosh || echo mongo')[0].strip()
            except:
                self._mongo_shell = 'mongo'
        return self._mongo_shell
