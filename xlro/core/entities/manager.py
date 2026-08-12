#!/usr/bin/env python

# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from builtins import str
from builtins import map

import json
import logging
import os
import re
import sys

from typing import List, Dict, Optional, Any, Union, Tuple

from deprecated import deprecated
from threading import Lock, RLock

from xlro.core import infra_conf
from xlro.core.entities.etypes import ChoiceStr, HTTPSServerAuthMethod
from xlro.core.entities.rest_info import RestVersionManager, MgmtVersion
from xlro.core.util.general_utils import host_name, wait_for_it, wait_for_first, WaitResult, wait_for_property_values, \
    mask_hidden_fields, parse_js_conf, add_names
from xlro.core.util.creds import get_creds, local_settings_dir
from xlro.core.util.thread_manager import ThreadPoolManager
from xlro.core.util.consts import Deprecate
from xlro.core.entities import Host, Client, Target, Volume, Drive, ClientNode, \
    MgmtHost  # pylint: disable=unused-import
from xlro.core.entities.base import prop_loader, PropertySpec, SourceTypes, entity, BaseEntity
from xlro.core.entities.network import Node

from xlro.core.sdk.Utils import Utils, MongoObj
from xlro.core.sdk.ConnectionManager import ConnectionManager, DEFAULT_PORT

if not hasattr(Utils, "convertUnitCapacityToBytes"):
    Utils.convertUnitCapacityToBytes = staticmethod(Utils.convertUnitToBytes)  # type: ignore


_rest_server_names: Dict[str, str] = {}
def parse_rest_servers(servers: str) -> List[str]:
    """ Attempt to preserve the configured names of the management servers vs. result of host_name() """
    global _rest_server_names
    ep_list = [s.strip() for s in re.sub(r'(:\d+)', '', servers).split(',') if s != '']
    Manager.logger.debug(f'parse_rest_servers() servers: {servers} -> ep_list: {ep_list}')
    # To preserve configured names as is.
    _rest_server_names.update({host_name(ep): ep for ep in ep_list})
    return ep_list

def get_rest_server_name(name: str) -> str:
    """ Get the configured name of a management server, or the original name if not found """
    global _rest_server_names
    return _rest_server_names.get(host_name(name), name)

def non_local_hostname(name: str) -> str:
    # Maybe this should be part of host_name(), but I'm afraid of breaking something
    hname = host_name(name)
    return name if hname.startswith('localhost') else hname

@entity(sourcetypes=[SourceTypes.MANAGEMENT, SourceTypes.PROC, SourceTypes.LOCAL])
class Manager(BaseEntity):
    # We default to https, but in ConnectionManager, we'll back off to http if we get an SSLError
    DEFAULT_PROTO = 'https'
    WEB_SOCKET_PORT = 4001
    logger = logging.getLogger(__name__)

    rest_lock = Lock()
    conn_lock = Lock()
    config_lock = Lock()
    consts_lock = RLock()
    _use_rest_attach: Optional[bool] = None
    _latest_instance: Optional['Manager'] = None
    uuid_to_manager: Dict[str, 'Manager'] = {}
    _api_consts: Optional[Dict[str, Dict]] = None
    ENV = 'NVMESH_MGMT'
    endpoints : List[str] = PropertySpec([str], key=True)
    mgmt_cluster : List[str] = PropertySpec([str], default=[])
    mongo_cluster : List[str] = PropertySpec([str])
    https_server_auth_method : HTTPSServerAuthMethod = PropertySpec(HTTPSServerAuthMethod, default=HTTPSServerAuthMethod("credentials"))
    # user : str = PropertySpec(str, default='admin')
    # passwd : str = PropertySpec(str, default='admin')
    volumes : List[Volume] = PropertySpec([Volume])
    clients : List[Client] = PropertySpec([Client])
    targets : List[Target] = PropertySpec([Target])
    all_drives : List[Drive] = PropertySpec([Drive])
    drives : List[Drive] = PropertySpec([Drive])
    version : str = PropertySpec(str)
    node_version : str = PropertySpec(str)
    mongo_version : str = PropertySpec(str)
    cluster : 'Cluster' = PropertySpec('Cluster')
    api_version : str = PropertySpec(str)
    web_socket_endpoints : int = PropertySpec(int)
    _creds: Optional[Tuple[str, str]] = None
    _global_nvmeshconf: Dict[str, str] = {}

    def __init__(self, *args, **kwargs):
        super(Manager, self).__init__(*args, **kwargs)
        self._default_settings_set = False
        self._connection = None
        self.uuid: Optional[str] = None
        self._user: Optional[str] = None
        self.creds_path: Optional[str] = None
        self.protocol = self.DEFAULT_PROTO
        self._rest_endpoints = None

        # Almost a singleton, though at some point we might switch managers (HA, multi-cluster testing, etc.)
        self.__class__._latest_instance = self

    @property
    def creds(self):
        if not self._creds:
            host = self.host
            self.logger.debug(f'Getting creds for {host}')
            self._creds = get_creds(host, defaults=('admin', 'admin'), creds_path=self.creds_path, user=self._user)
        return self._creds

    @property
    def user(self):
        return self.creds[0]

    @property
    def passwd(self):
        return self.creds[1]

    def connect(self, user: str = None, **auth):
        ''' Get connection WITHOUT swallowing exceptions. '''
        # TODO: Don't want to change interface now, but it was a mistake for .connection to swallow exceptions
        self.logger.debug(f'Connect to {self.endpoints} as user: {user}, auth params: {mask_hidden_fields(auth)}')
        if self._connection:
            self.logger.debug(f'Existing connection: {self._connection}, auth: {mask_hidden_fields(self._connection.auth)} - {"match" if auth == self._connection.auth else "mismatch"}')

        if self._connection and user in (None, self._connection.user) and all(v in (None, self._connection.auth.get(a)) for a, v in auth.items()):
            return self._connection

        with self.conn_lock:
            if self._connection and user in (None, self._connection.user) and all(v in (None, self._connection.auth.get(a)) for a, v in auth.items()):
                return self._connection

        self.reset(user, **auth)
        self.logger.debug(f'Connection: {self._connection.user}@{self._connection}. uuid={self.uuid}, id={id(self)}')
        return self._connection

    def get_certfile_paths(self, auth: Dict) -> Dict[str, str]:
        ''' Find cert, key and ca files to authorize to management on given mgmt-host '''

        locald = local_settings_dir()
        local = lambda f: os.path.join(local_settings_dir(), f)
        clusterd = os.path.join(locald, self.host)
        cluster = lambda f: os.path.join(clusterd, f)
        shared = lambda f: os.path.join(MgmtHost.DEFAULT_NVMESH_CERTS_DIR, f)
        cert_user = self._user.capitalize()
        for cert_file, default_paths in {
                    'cert': (cluster(f'{cert_user}.crt'), local('nvmesh.crt'), shared(f'{cert_user}.crt')),
                    'key': (cluster(f'{cert_user}.key'), local('nvmesh.key'), shared(f'{cert_user}.key')),
                    'ca': (self.global_nvmeshconf().get('_REST_CA', ''), cluster('ca_chain.crt'), local('ca_chain.crt'), shared('ca_chain.crt'))
                }.items():

            # Priority is: passed in value (auth[]) or explicitly configured path
            fpath = auth.get(cert_file, getattr(infra_conf.root.cluster.tls, cert_file))
            # Otherwise, check for file in default locations
            if not fpath:
                unreadable = []
                for fpath in default_paths:
                    if os.path.exists(fpath):
                        if os.access(fpath, os.R_OK):
                            break
                        unreadable.append(fpath)
                else:
                    if unreadable:
                        raise PermissionError(
                            f'TLS {cert_file.upper()} file(s) found but not readable: {", ".join(unreadable)}. '
                            f'Run with sudo or fix file permissions.')
                    raise Exception(f'Unable to connect via TLS. {cert_file.upper()} not configured or found at {" or ".join(default_paths)}.')
                    # No longer trying to copy from the management.  That was probably over-engineered.
                    # Also, sub-directories per management will confuse normal users. Working with multiple managements
                    # needs to be handled by config/command-line
            auth[cert_file] = fpath

        return auth

    def get_mgmt_connection(self, user, **auth):
        ''' Don't use cache.  Get new connection '''
        # JW: The auth dict will not be persistent, and retried each time.  Is this intentional?
        use_tls = auth.get('use_tls') if isinstance(auth.get('use_tls'), bool) else self.use_tls
        self._user = user or self.user
        self.logger.debug(f'get-conn: {self}. user: {self._user}, auth: {mask_hidden_fields(auth)}, tls: {use_tls}')
        mgmt_hosts = self.mgmt_hosts
        self.logger.debug(f'get-conn: mhosts: {mgmt_hosts}')
        self.creds_path = auth.pop('creds_path', None)
        if use_tls:
            self.logger.info(f'get-conn: Attempt login with MTLS')
            auth['use_tls'] = True

            if not all(auth.get(ftype) for ftype in ['ca', 'cert', 'key']):
                # New scheme is: user files are in ~/.nvmesh/nvmesh.{crt,key}. CA is in config as _REST_CA
                self.get_certfile_paths(auth)

        elif not auth.get('password'):
            auth['password'] = self.passwd

        try:
            endpoints = self.rest_endpoints
            assert endpoints, 'No endpoints found in conf files.'
            self.logger.debug(f'get-conn: Got proc endpoints: {endpoints}')
        except Exception as e:
            self.logger.info(f'get-conn: endpoints from conf failed: {repr(e)}.  Existing conn? {bool(self._connection)}')
            # Only try from existing mgmt connection.  If not, either we can reach the current endpoint or not.
            if self._connection: # Should prevent infinite loop with cluster loader
                try:
                    endpoints = self.get_property('mgmt_cluster', source=SourceTypes.MANAGEMENT, no_cache=True)
                    assert endpoints, 'Manager did not return any endpoints'
                except Exception as e2:
                    self.logger.info(f'get-conn: endpoints from MGMT failed: {repr(e2)}.')
                    endpoints = self.endpoints
            else:
                endpoints = self.endpoints
        ep_hosts = [non_local_hostname(self.endpoint_to_host(ep)) for ep in endpoints]
        self.logger.debug(f'get-conn: My endpoints: {self.endpoints}, discovered endpoints: {endpoints}, ep_hosts: {ep_hosts}, self.host: {host_name(self.host)} ({self.host})')
        # I can't remember why, and it can be annoying or wrong.  Leaving as comment in case some issue recurs
        # assert host_name(self.host) in ep_hosts, f'{host_name(self.host)} is not a management host ({ep_hosts})'
        servers = Utils.transformManagementClusterToUrls(",".join(endpoints), self.protocol, DEFAULT_PORT)
        conn = ConnectionManager.getInstance(dbUUID=None, managementServers=servers, user=self._user, logger=self.logger, **auth)
        # Reset protocol based on what actually worked
        self.protocol = conn.managementServers[0].partition(':')[0]
        self.logger.debug(f'get-conn: Connected to {conn.managementServers} #{conn.dbUUID}')
        return conn

    @property
    def connection(self):
        """ The management connection.  A lazy property, because Mgmt may not be up at first.  So wait until someone DEFINITELY needs it. """
        if not self._connection:
            try:
                self.connect()
            except Exception as e:
                #JW This shows up too often.  Let the caller fail if the None connection is a problem
                self.logger.info(f'Failed on connect {self.host}. {repr(e)}.')
        return self._connection

    def reset(self, user: str = None, **auth):
        # TODO: Even this doesn't seem enough.  This is the great problem with "install" being a test.
        # For example, cluster monitors might be running, but cluster may have changed.
        # self.logger.info(f'Prior MGMT API Version: {self.api_version}, MGMT Version: {self.version}')
        # Need to clear mgmt_js which might have changed between connections
        for mh in self.mgmt_hosts:
            mh._mgmt_js_config = {}
            mh._nvmeshconf = None
        self._global_nvmeshconf = {}
        self._rest_endpoints = None
        self.protocol = self.DEFAULT_PROTO
        self._api_consts = None
        self._mgmt_info = None
        # Reset any cached props for manager
        # JW: Should we reset everything from MANAGEMENT and PROC?
        for prop in ['volumes', 'clients', 'targets', 'drives', 'all_drives', 'version', 'api_version', 'mgmt_cluster', 'https_server_auth_method']:
            self.reset_property(prop)

        if self.uuid and self.uuid_to_manager.pop(self.uuid, None):
            self.logger.info('RESET Removed old UUID: {}'.format(self.uuid))

        self.protocol = self.DEFAULT_PROTO
        try:
            self._connection = self.get_mgmt_connection(user, **auth)
        except PermissionError:
            raise
        except Exception as e:
            # We do this as last resort, because it requires SSH access to the nodes
            self.logger.info(f'RESET Failed to get management connection for {self.host}.  Trying to find more HA endpoints via nvmesh.conf')
            # TODO: I need to figure out the "_endpoints" mess...
            conf_endpoints = [self._to_endpoint(ep) for ep in parse_rest_servers(self.global_nvmeshconf().get("_REST_SERVERS", ""))]
            expanded = set(conf_endpoints) - set(self._rest_endpoints)
            if not expanded:
                self.logger.info(f'RESET Failed to find more HA endpoints...')
                raise
            self.logger.debug(f'RESET expanded from conf: {expanded} and re-trying...')
            self._rest_endpoints = list(expanded | set(self._rest_endpoints))
            try:
                self._connection = self.get_mgmt_connection(user, **auth)
            except Exception as e2:
                self.logger.info(f'RESET Failed to get management connection on retry.  {repr(e2)}')
                raise
        # Try to expand endpoints for HA
        mservers = self._connection.managementServers[:]
        try:
            _ = self.get_property('mgmt_cluster', source=SourceTypes.MANAGEMENT, no_cache=True)
        except Exception as e:
            pass
        self.logger.debug(f'RESET Trying to expand endpoints from management.  WAS: {mservers}, NOW: {self._connection.managementServers}')

        for m in list(sys.modules['xlro.core.entities.etypes'].__dict__.values()):
            if isinstance(m, type) and issubclass(m, ChoiceStr) and getattr(m, 'consts_key'):
                try:
                    m.choices = list(self.api_consts[m.consts_key].values())
                except KeyError:
                    pass

        self.logger.info(f'Newer MGMT API Version: {self.api_version}, MGMT Version: {self.version}')
        self.uuid = self._connection.dbUUID
        if self.uuid:
            self.logger.info('RESET Management UUID: {}'.format(self.uuid))
            self.uuid_to_manager[self.uuid] = self

    @classmethod
    def _to_endpoint(cls, host):
        h, _, port = host.partition(":")
        return "{}:{}".format(non_local_hostname(h), port or DEFAULT_PORT)

    @classmethod
    def map_props(cls, propmap, source_type=None):
        propmap = super(Manager, cls).map_props(propmap, source_type)
        if 'host' in propmap:
            # backward compatibility for 'host' property
            host = propmap.pop('host')
            if not propmap.get('endpoints', []):
                propmap['endpoints'] = [str(host)] if isinstance(host, str) else host
        if 'endpoints' in propmap:
            try:
                propmap['endpoints'] = list(map(cls._to_endpoint, propmap['endpoints']))
            except Exception:
                cls.logger.info("couldn't detect host name for {}".format(propmap['endpoints']))
        return propmap

    @classmethod
    def _genkey(cls, source: Optional[str] = None, kwargs: Optional[dict] = None) -> str:
        # A kludge to check ENV at object creation time.
        # Using PropertySpec default didn't work, because that happened at import time.

        # Set host from endpoints or endpoints from host
        kwargs = kwargs or {}
        if 'endpoints' in kwargs:
            # Force end-points to full format
            kwargs['endpoints'] = [cls._to_endpoint(ep) for ep in kwargs['endpoints']]
            kwargs.setdefault('host', kwargs['endpoints'][0].partition(':')[0])
        else:
            kwargs.setdefault('host', os.environ.get(cls.ENV))
            kwargs['endpoints'] = [cls._to_endpoint(h.strip()) for h in kwargs['host'].split(',')]
        # Sort endpoints
        kwargs['endpoints'].sort()
        return super(Manager, cls)._genkey(source, kwargs)

    _default_endpoints: Optional[List[str]] = None
    @classmethod
    def default_endpoints(cls) -> List[str]:
        if cls._default_endpoints:
            return cls._default_endpoints
        env = os.environ.get(cls.ENV, None)
        if env:
            cls.logger.debug(f'Default mgmt endpoints from {cls.ENV}: {env}')
            endpoints = parse_rest_servers(env)
        else:
            endpoints = infra_conf.root.cluster.management
            if endpoints:
                cls.logger.debug(f'Default mgmt endpoints from cluster config: {",".join(endpoints)}')
        if not endpoints:
            try:
                endpoints = parse_rest_servers(Host.instance(name='localhost').nvmeshconf()['_REST_SERVERS'])
                assert endpoints, 'No endpoints found in local conf file.'
                cls.logger.debug(f'Default mgmt endpoints from nvmesh-conf {",".join(endpoints)}')
            except Exception as e:
                raise Exception(f'No default mgmt endpoints found {repr(e)}')
        cls._default_endpoints = endpoints
        return endpoints

    @classmethod
    def get_manager(cls, mgmt_uuid: Optional[str] = None) -> 'Manager':
        if mgmt_uuid:
            return cls.uuid_to_manager[mgmt_uuid]
        if not cls._latest_instance:
            endpoints = cls.default_endpoints()
            if endpoints:
                try:
                    cls.instance(endpoints=endpoints)
                except Exception as e:
                    cls.logger.info('No instance, and failed to create from default "{}".'.format(endpoints))
        assert cls._latest_instance is not None, 'No manager connected yet'
        return cls._latest_instance

    @staticmethod
    def _err2exc(response):
        err, out = response
        if err:
            raise Exception(err)
        return out

    @prop_loader(SourceTypes.MANAGEMENT, ['mgmt_cluster'])
    def _load_mgmt_cluster_from_mgmt(self):
        err, mgmt_cluster = self.connection.get('/managementCluster/all/0/0')
        assert not err, f"Failed to retrieve management cluster nodes - {err}"
        # Learn hostname/ip mappings
        cluster = []
        for m in mgmt_cluster:
            # Learn the hostname<->ip mapping from the manager, even if we couldn't figure it out ourselves
            add_names(m['hostname'], [m['ip']])
            cluster.append(m['hostname'])

        # Update managementServers which might not have the HA set, but only append vs mess with order or current index
        for ms in Utils.transformManagementClusterToUrls(",".join(cluster), self.protocol, DEFAULT_PORT):
            if not ms in self.connection.managementServers:
                self.connection.managementServers.append(ms)
        return {'mgmt_cluster': cluster}

    @prop_loader(SourceTypes.PROC, ['mgmt_cluster'])
    def _load_mgmt_cluster_from_proc(self):
        return {'mgmt_cluster': self.rest_endpoints}

    @prop_loader(SourceTypes.MANAGEMENT, ['mongo_cluster'])
    def _load_mongo_cluster_from_mgmt(self):
        err, mongo_cluster = self.connection.get('/mongoDB/all')
        assert not err, f"Failed to retrieve mongo cluster nodes - {err}"
        return {'mongo_cluster': [host_name(m['host']) for m in mongo_cluster[0]['members']]}

    @prop_loader(SourceTypes.PROC, ['mongo_cluster'])
    def _load_mongo_cluster_from_proc(self):
        return {'mongo_cluster': [host_name(m.split(':')[0]) for m in self.mgmt_host.mgmt_js_config['mongoConnection']['hosts'].split(',')]}

    @property
    def url(self):
        return [f'{self.protocol}://{host}' for host in self.endpoints]

    @staticmethod
    def endpoint_to_host(endpoint):
        return non_local_hostname(endpoint.partition(':')[0])

    @property
    def host(self):
        """
        Backward compatibility for 'host' property
        """
        return self.endpoint_to_host(self.endpoints[0])

    def global_nvmeshconf(self) -> Dict[str, str]:
        '''
            Try to get nvmesh.conf, either locally (best on nodes) or from a management host (tools from outside)
            NOTE: Because the source may not be local, this is only valid for "global" settings.
            You must use Host.nvmeshconf() for anything host-specific.
        '''
        if not self._global_nvmeshconf:
            with self.config_lock:
                if not self._global_nvmeshconf:
                    try:
                        # TODO: if on a node of cluster 1, trying to reach cluster 2, this will be a problem.
                        # But we need 'localhost' to avoid ssh dependency
                        mgmt_hosts = self.mgmt_hosts + [MgmtHost.instance(name='localhost')]
                        self.logger.debug(f'Loading nvmesh.conf from {mgmt_hosts}')
                        h, self._global_nvmeshconf = wait_for_first(lambda h: h.nvmeshconf(), mgmt_hosts)
                        self.logger.debug(f'Got nvmesh.conf from {h.name}')
                    except Exception as e:
                        self.logger.info(f'Failed to get nvmesh.conf! {repr(e)}')
                        # Maybe we should swallow the exception and return {}?  Let the caller deal.
                        raise
        return self._global_nvmeshconf

    @property
    def rest_endpoints(self) -> List[str]:
        ''' Get configured REST hostnames '''
        if self._rest_endpoints:
            return self._rest_endpoints
        self._rest_endpoints = self.endpoints or self.default_endpoints()
        if not self._rest_endpoints:
            servers = self.global_nvmeshconf().get('_REST_SERVERS', '')
            self.logger.debug(f'REST_SERVERS={servers}')
            self._rest_endpoints = parse_rest_servers(servers)
        return self._rest_endpoints or []

    @property
    def live_host(self):
        cm = self._connection
        self.logger.debug(f'Get live-host: {self}, id={id(self)}, cm={cm}')
        assert cm and cm.isAlive(), 'Cannot connect to Management at {}'.format(self.endpoints)
        live_url = cm.managementServers[cm.currentMgmtIndex]
        return self.endpoint_to_host(live_url.rpartition('/')[2])

    @property
    def current_host(self):
        ''' like live_host but without checking is_alive '''
        cm = self.connection
        live_url = cm.managementServers[cm.currentMgmtIndex]
        return self.endpoint_to_host(live_url.rpartition('/')[2])

    @property
    def mgmt_host(self):
        return MgmtHost.instance(name=self.host)

    @property
    def live_mgmt_host(self):
        return MgmtHost.instance(name=self.live_host)

    @property
    def mgmt_hosts(self):
        return [MgmtHost.instance(name=non_local_hostname(self.endpoint_to_host(e))) for e in self.endpoints]

    @prop_loader(SourceTypes.MANAGEMENT, ['clients'])
    def load_clients_from_sdk(self):
        # All targets are clients, but some clients may not be registered in multi-instance...
        if infra_conf.root.cluster.explicit_clients:
            return {'clients': [Client.instance(name=c) for c in infra_conf.root.cluster.clients]}
        clients = Client.sdk_get(mgmt=self) # type: ignore[arg-type] ### MUST figure this out one day
        targets = self.get_property('targets', SourceTypes.MANAGEMENT)
        self.logger.info('Natural Clients: {}, Targets: {}'.format(
            ','.join([c.name for c in clients]), ','.join([t.name for t in targets])))
        return {'clients': list(set(clients + [Client.instance(name=t.name) for t in targets]))}

    @property
    def client_nodes(self) -> List[ClientNode]:
        return [ClientNode.instance(name=c.name) for c in self.clients if not c.sub_name]

    @prop_loader(SourceTypes.MANAGEMENT, ['targets'])
    def load_targets_from_sdk(self):
        return {'targets': Target.sdk_get(mgmt=self)} # type: ignore[arg-type] ### MUST figure this out one day

    @prop_loader(SourceTypes.MANAGEMENT, ['volumes'])
    def load_volumes_from_sdk(self):
        return {'volumes': Volume.sdk_get(mgmt=self)} # type: ignore[arg-type] ### MUST figure this out one day

    @prop_loader(SourceTypes.MANAGEMENT, ['all_drives', 'drives'])
    def load_drives_from_sdk(self):
        all_drives = Drive.sdk_get(mgmt=self) # type: ignore[arg-type] ### MUST figure this out one day
        return {'all_drives': all_drives, 'drives': [d for d in all_drives if not d.excluded] }

    @deprecated(Deprecate.ToBeReplaced(Volume.get_headlines))
    def list_volumes(self) -> List[dict]:
        return [{'name': n} for n in Volume.get_headlines()]

    @deprecated(Deprecate.ToBeReplaced(Drive.evict_drives))
    def evict_drives(self, disk_ids: List[str]) -> List[dict]:
        drives = [Drive.instance(name=did) for did in disk_ids]
        return Drive.evict_drives(drives)

    @deprecated(Deprecate.ToBeReplaced(Client.attach, Client.bulk_wait_for_attachments_status))
    def attach_volumes(self, volumes: List[str], clients: List[str], wait_till_completed: Optional[bool] = True) -> bool:

        # TODO - change either volumes or clients or both to objects (vs str)
        clients_objs = [Client.instance(name=client) for client in clients]
        volumes_objs = [Volume.instance(name=vname) for vname in volumes]

        [client_obj.attach(volumes_objs) for client_obj in clients_objs]

        if wait_till_completed and not self.wait_for_attach(volumes, clients):
            self.logger.warn('timed out waiting for {} to attach to volumes {}'.format(clients, volumes))
            return False

        return True

    @deprecated(Deprecate.ToBeReplaced(Client.detach, Client.bulk_wait_for_attachments_status))
    def detach_volumes(self, volumes: List[str], clients: List[str], wait_till_completed: Optional[bool] = True) -> bool:
        # TODO - change arguments - volumes or clients or both to objects (vs str)
        clients_objs = [Client.instance(name=client) for client in clients]
        volumes_objs = [Volume.instance(name=vname) for vname in volumes]

        [client_obj.detach(volumes_objs) for client_obj in clients_objs]

        if wait_till_completed and not self.wait_for_detach(volumes, clients):
            self.logger.warn('timed out waiting for {} to detach to volumes {}'.format(clients, volumes))
            return False

        return True

    @deprecated(Deprecate.ToBeReplaced(Client.detach, Client.bulk_wait_for_attachments_status))
    def detach_all_volumes(self, clients: List[str], wait_till_completed: Optional[bool] = True) -> bool:

        # TODO - change either volumes or clients or both to objects (vs str)
        clients_objs = [Client.instance(name=client) for client in clients]
        for client_obj in clients_objs:
            attached_vols = [att.volume
                             for att in list(client_obj.attachments.values())
                             if att.status == 'Attached']
            if attached_vols:
                client_obj.detach(attached_vols)

        def all_detached():
            for client in clients:
                if self.show_attached(client):
                    return False

            return True

        if wait_till_completed and not wait_for_it(all_detached):
            self.logger.warn('timed out waiting for detach-all-volumes.')
            return False
        return True

    @deprecated(Deprecate.ToBeReplaced(Volume.wait_for_volumes_statuses))
    def wait_for_volume_status(self, volume: str, status_list: List[str], *args: List[Any], **kwargs: Dict[str, Any]) -> WaitResult:
        # TODO - think of changing loading mechanism to allow narrow prop loading
        return wait_for_it(lambda: Volume.instance(name=volume).get_property
                                   ('status', SourceTypes.MANAGEMENT, no_cache=True) in status_list, *args,  # type: ignore[arg-type]
                           **kwargs)  # type: ignore

    @deprecated(Deprecate.ToBeReplaced(Volume.wait_for_volumes_statuses))
    def wait_for_volumes_status(self, volumes_names: List[str], status: str, *args: List[Any], **kwargs: Dict[str, Any]) -> WaitResult:
        volumes_obj = [Volume.instance(name=vname) for vname in volumes_names]

        def check_volumes_status():
            for vol in volumes_obj:
                if vol.get_property('status', SourceTypes.MANAGEMENT, no_cache=True) != status:
                    return False
            return True

        return wait_for_it(check_volumes_status, *args, **kwargs)  # type: ignore

    @deprecated(Deprecate.ToBeReplaced(Client.bulk_wait_for_attachments_status))
    def wait_for_detach(self, volumes: List[str], clients: List[str], *args: Any, **kwargs: Any) -> WaitResult:
        return self._wait_for_attach_detach_sdk(volumes, clients, False, *args, **kwargs)

    @deprecated(Deprecate.ToBeReplaced(Client.bulk_wait_for_attachments_status))
    def wait_for_attach(self, volumes: List[str], clients: List[str], *args: Any, **kwargs: Any) -> WaitResult:
        return self._wait_for_attach_detach_sdk(volumes, clients, True, *args, **kwargs)

    @deprecated
    def show_attached(self, client_name: str) -> List[str]:
        # TODO - pass those methods for manager.py to the objects themselves
        client = Client.instance(name=client_name)
        attachments = list(client.get_property('attachments', SourceTypes.MANAGEMENT, no_cache=True).values())
        return [att.volume.name for att in attachments if att.status == 'Attached']

    def get_all_subsystems(self) -> List[Host]:
        hosts = [t.host for t in self.targets] + [c.host for c in self.client_nodes] + self.mgmt_hosts
        return hosts

    def get_all_network_nodes(self) -> List[Node]:
        all_subsystems = self.get_all_subsystems()
        return [Node.instance(name=node_name) for node_name in
                set([nvmesh_node.name for nvmesh_node in all_subsystems])]

    @deprecated(Deprecate.ToBeReplaced(Drive.format_drives))
    def format_disks(self, format_type: str, disk_ids: List[str]) -> List[dict]:
        drives = [Drive.instance(name=did) for did in disk_ids]
        return Drive.format_drives(drives, format_type)

    @deprecated(Deprecate.ToBeReplaced(Volume.rebuild_volumes))
    def rebuild_volumes(self, volumes_id: List[str]) -> List[dict]:
        volumes = [Volume.instance(name=vid) for vid in volumes_id]
        return Volume.rebuild_volumes(volumes)

    def client_uuid_to_client(self, uuid: str) -> Client:
        for c in self.clients:
            if c.uuid == uuid:
                return c
        raise Exception("Could not match {} to any client".format(uuid))

    # SDK methods

    @deprecated
    def _wait_for_attach_detach_sdk(self, volumes: List[str], clients: List[str], is_check_attach: bool, *args: Any, **kwargs: Any) -> WaitResult:

        def all_to_all():
            for client in clients:
                for volume in volumes:
                    if is_check_attach ^ (volume in self.show_attached(client)):
                        return False
            return True

        return wait_for_it(all_to_all, *args, **kwargs)

    def validate_services(self, nvmesh_subsystems=None, timeout=None):
        # type: (Optional[List[Union[Client,Target]]], int) -> bool
        if not timeout:
            timeout = 60 + len(self.clients) * 15
        if not nvmesh_subsystems:
            nvmesh_subsystems = [nd for nd in self.clients + self.targets if not nd.name.startswith('scale')] # type: ignore # Should probably have a base-class?
        self.logger.info('Waiting for services {} to be up'.format(nvmesh_subsystems))
        with ThreadPoolManager() as executor:
            def assert_healthy_service(s):
                wait_for_property_values([s], "health", [s.HEALTH.HEALTHY], source=SourceTypes.MANAGEMENT, timeout=timeout).\
                    assert_result('Service status did not reach {} on {}'.format(s.HEALTH.HEALTHY, s.name))
                return True

            assert all(executor.map(lambda s: assert_healthy_service(s), nvmesh_subsystems)), \
                'Not all services are up after {} seconds'.format(timeout)
        return True

    def get_latest_iops(self, client, volume):
        from xlro.core.sdk.ConnectionManager import ConnectionManager
        from xlro.core.sdk.Utils import Utils

        cm = ConnectionManager.getInstance(self.uuid, self.url, self.connection.user, **self.connection.auth)

        error, ret = cm.doRequest('get', 'statistics/data/' + Utils.buildQueryStr(
            {'filter': [MongoObj('registrantID', client.name),
                        MongoObj('id', volume.name)],
             'sort': [MongoObj('time', -1)]}))

        assert not error, "{} returned when trying to fetch statistics".format(error)

        # A really dirty 2 lines to get latest report for total ops, feels pointless to wrap it here
        try:
            self.logger.debug("options for statistics {}".format([x['_id'] for x in ret]))
            ordered = sorted(((int(k), v) for k, v in ret[0]['samples'].items()), key=lambda t: t[0])
            io_ops = ordered[-1][1]['4K']['ops']['total']
            # some debug needed to understand if we are fetching info right
            self.logger.debug("statistic info for {}.{}: {} iops ()".format(ret[0]['_id'], ordered[-1][0], io_ops))
        except Exception as e:
            self.logger.exception("failed getting statistics for {} {} :{}".format(client.name, volume.name, repr(e)))
            raise

        return io_ops

    _mgmt_info = None
    @property
    def mgmt_info(self) -> Dict:
        if self._mgmt_info:
            return self._mgmt_info
        err = None
        info = None
        try:
            err, info = self.connection.get('/systemInfo')
            assert info and not err
        except:
            # Because we can be called recursively...
            if self._mgmt_info:
                return self._mgmt_info
            try:
                err, info = self.connection.get('/aboutInfo')
            except:
                pass
        assert info and not err, 'Failed to get management info. Error: {err}'
        self._mgmt_info = info
        return info


    @prop_loader(SourceTypes.MANAGEMENT, ['version', 'node_version', 'mongo_version', 'cluster'])
    def _load_about_info(self):
        from xlro.core.entities import Cluster
        try:
            about_info = self.mgmt_info
        except Exception as e:
            self.logger.debug(f'Unable to fetch manager about info - {repr(e)}')
            about_info = {}
        return {'version': about_info.get('managementVersion') or 'NOT_AVAILABLE',
                'node_version': about_info.get('nodeVersion'),
                'mongo_version': about_info.get('mongoVersion'),
                'cluster': Cluster.instance(dbUUID=self.connection.dbUUID)}

    @prop_loader(SourceTypes.MANAGEMENT, ['api_version'])
    def _load_api_version(self):
        try:
            api_version = self.mgmt_info['APIVersion']
        except:
            try:
                err, api_version = self.connection.get('/APIVersion')
                assert not err, "Unable to get APIVersion from management"
            except (AssertionError, AttributeError):
                # Either management does not respond or bad route/endpoint
                api_version = RestVersionManager.default_version()
        return {'api_version': MgmtVersion(api_version)}

    @property
    def api_consts(self):
        if self._api_consts is None:
            with self.consts_lock:
                if self._api_consts is None:
                    try:
                        if self._api_consts is None:
                            if self.api_version >= 16:
                                err, api_consts_js = self.connection.get('/consts')
                                self._api_consts = api_consts_js
                            else:
                                api_consts_js, err = self.connection.get('/javascripts/consts.js')
                                self._api_consts = parse_js_conf(api_consts_js['content'].decode())
                            assert not err, f"Unable to get API consts from management"
                    except Exception as e:
                        self.logger.warning(f"Unable to get management consts - {repr(e)}")
                        self._api_consts = {}
        return self._api_consts

    @property
    def reservation_modes(self):
        return self.api_consts['reservationModes']

    @property
    def use_rest_attach(self):
        if self._use_rest_attach is None:
            with self.rest_lock:
                if self._use_rest_attach is None:
                    try:
                        err = self.connection.post('/clients/attach')[0]
                        self._use_rest_attach = not err or err.get('code') != 404
                    except Exception as e:
                        self.logger.info(f'Exception determining /clients/attach support: {type(e)}')
                        self._use_rest_attach = False
            self.logger.info(f'use-rest-attach: {self._use_rest_attach}')

        return self._use_rest_attach

    @prop_loader(SourceTypes.PROC, ['https_server_auth_method'])
    def load_https_server_auth_method(self):
        rest_auth_method = self.global_nvmeshconf().get('_REST_AUTH_METHOD', '')
        if rest_auth_method:
            return {'https_server_auth_method': rest_auth_method}

        for mh in self.mgmt_hosts:
            try:
                try:
                    https_server_auth_method = mh.mgmt_js_config['server']['auth']['authenticationMethod']
                except KeyError:
                    https_server_auth_method = mh.mgmt_js_config.get('httpsServerAuthenticationMethod', 'credentials')
                return {'https_server_auth_method': https_server_auth_method}
            except Exception as e:
                self.logger.debug(f'Unable to fetch httpsServerAuthenticationMethod of {mh.name} - {repr(e)}')

        self.logger.info(f"Unable to fetch httpsServerAuthenticationMethod of {self.endpoints}. Default to 'credentials'")
        return {'https_server_auth_method': 'credentials'}

    @property
    def use_tls(self):
        return self.get_property('https_server_auth_method', source=SourceTypes.PROC) == 'MTLS'
