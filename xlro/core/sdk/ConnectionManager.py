# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0


from http.cookiejar import MozillaCookieJar

from future import standard_library

from xlro.core import infra_conf
from xlro.core.util.creds import local_settings_dir
from xlro.core.util.general_utils import mask_hidden_fields, host_aliases

standard_library.install_aliases()
from builtins import object
from typing import Dict, Optional, IO, Any
import json
from threading import Lock
import requests
import urllib3
import urllib.parse as urlparse
import random
from hashlib import md5
import time
import http
import os
import re
from itertools import count
from logging import getLogger
from xlro.core.sdk.Utils import Utils

DEFAULT_PORT = 4000

class TunnelSession(requests.Session):
    '''
        For testing on OCI (or any non-directly accessible cluster) you can use an SSH tunnel for REST.
        For example, for OCI cluster in-c99021 you could run:
            ssh -N -f -L 4000:in-c99021-n1:4000 bastion-iad
        This will establish a tunnel via bastion-iad and forward localhost:4000 to in-c99021:4000 (management)
        Then, set REST_TUNNEL=localhost:4000
        REST_TUNNEL can be any non-blank.  If it's not of the format [host:]port, we default to localhost:4000.
        REST_TUNNEL should be UNSET, or set to "", to disable.
    '''
    ssl_lock = Lock()
    URL_PATT = r'^(?P<scheme>https*)://(?P<host>[^:/]+)(:(?P<port>\d+))?(?P<rest>.*)'
    COOKIE_JAR = os.path.join(local_settings_dir(), 'cookies.txt')
    host = None
    def __init__(self, user: str):
        super().__init__()
        self.logger = getLogger('TunnelSession')
        self.tunnel = os.environ.get('REST_TUNNEL')
        self.cookiejar = os.path.join(local_settings_dir(), 'cookies.txt' if not user else f'{user}-cookies.txt')
        self.cookies = MozillaCookieJar(filename=self.cookiejar)
        try:
            self.cookies.load(ignore_discard=True, ignore_expires=True)
            self.logger.debug(f'Loaded existing cookiejar: {self.cookiejar}')
        except Exception as e:
            # Start fresh if file is corrupt
            self.logger.debug(f'Failed to load cookiejar: {repr(e)}.  Resetting')
            self.cookies.clear()
            self.cookies.save(ignore_discard=True, ignore_expires=True)

        if not self.tunnel:
            return
        host, sep, port = self.tunnel.rpartition(':')
        self.host = host or 'localhost'
        self.port = int(port) if port.isdigit() else DEFAULT_PORT

    def request(self, method, url, *args, **kwargs):
        orig_url = url
        match = re.match(self.URL_PATT, url)
        if match:
            host = match.group('host')
        else:
            self.logger.warning(f'URL Parse failed on {method.upper()} - {url}')

        if self.tunnel is not None:
            if match:
                try:
                    port = self.port + int(host.rpartition('n')[1]) - 1
                except:
                    port = self.port
                url = match.group('scheme') + '://' + self.host + ':' + str(port) + match.group('rest')
                self.logger.debug(f'Tunnel URL: {method.upper()} - {orig_url} via {url}')
        elif match and match.group('scheme') == 'https':
            from requests.exceptions import SSLError
            # For SSL, try all aliases until we know which will match the CN/SAN of the certificate
            if not self.host:
                err = None
                hostnames = host_aliases(host)
                self.logger.debug(f'Attempt to validate SSL to {host} via all hostnames: {hostnames}')
                for h in host_aliases(host):
                    with self.ssl_lock:
                        urllib3_log_disabled = urllib3.connection.log.disabled
                        if not urllib3_log_disabled:
                            urllib3.connection.log.disabled = True
                        url = orig_url.replace(f'://{host}', f'://{h}')
                        try:
                            result = super().request(method, url, *args, **kwargs)
                            self.host = h
                            self.logger.debug(f'SSL OK for {self.host}')
                            return result
                        except Exception as e:
                            if not err and isinstance(e, SSLError):
                                err = e
                            self.logger.debug(f'{h}: {repr(e)}')
                        finally:
                            if not urllib3_log_disabled:
                                urllib3.connection.log.disabled = False

                raise err or Exception(f'Failed to connect to any of {hostnames}')
            if host != self.host:
                url = url.replace(f'://{host}', f'://{self.host}')
                self.logger.debug(f'{orig_url} -> {url}')

        return super().request(method, url, *args, **kwargs)


class BytesEncoder(json.JSONEncoder):
    def default(self, obj):
        try:
            return super(BytesEncoder, self).default(obj)
        except:
            if isinstance(obj, (bytes, bytearray)):
                return str(obj, 'utf-8', 'backslashreplace')
            raise

class RandomSleepTime(object):
    def __init__(self, start, stop, precision=2):
        self.start = start
        self.stop = stop
        self.precision = precision

    def getValue(self):
        return round(random.uniform(self.start, self.stop), self.precision)

class ConnectionManagerError(Exception):
    pass

class ManagementTimeout(ConnectionManagerError):
    def __init__(self, iport, msg=''):
        ConnectionManagerError.__init__(self, 'Could not connect to Management at {0}'.format(iport), msg)

class ManagementLoginError(ConnectionManagerError):
    pass

class ManagementConnectError(ConnectionManagerError):
    pass

class ChangePasswordRequiredError(ManagementLoginError):
    pass

class ConnectionManager(object):
    logger = getLogger('ConnectionManager')
    DEFAULT_NVMESH_CONFIG_FILE = '/etc/nvmesh/nvmesh.conf'
    __instances: Dict[str, 'Connection'] = {}

    @staticmethod
    def debug_getInstances():
        return ConnectionManager.__instances

    @staticmethod
    def getInstance(dbUUID, managementServers, user, configFile=DEFAULT_NVMESH_CONFIG_FILE, logger=None, **auth):
        # if not dbUUID:
            # return Connection(managementServers=managementServers, user=user, password=password, configFile=configFile, logger=logger)
        # Only reuse connection if matching UUID and credentials
        connection = ConnectionManager.__instances.get(dbUUID)
        if connection and user == connection.user and auth == connection.auth:
            return connection
        # Create new connection
        ConnectionManager.logger.info(f'New connection for {dbUUID}, servers={managementServers}, user={user}, auth={mask_hidden_fields(auth)}')
        connection = Connection(managementServers=managementServers, configFile=configFile, user=user, logger=logger, **auth)
        if not dbUUID:
            dbUUID = connection.dbUUID
        if dbUUID:
            ConnectionManager.__instances[dbUUID] = connection
        return connection

    @staticmethod
    def getCredentials(dbUUID):
        return ConnectionManager.__instances[dbUUID].getCredentials()

    @staticmethod
    def removeInstance(id):
        if id in ConnectionManager.__instances:
            del ConnectionManager.__instances[id]

    @classmethod
    def addInstance(cls, dbUUID, connection):
        if not dbUUID:
            raise ValueError('empty dbUUID')

        ConnectionManager.__instances[dbUUID] = connection


cm_conf = infra_conf.root.connection_manager

class Connection(object):
    rest_debug_init = False
    rest_debug_lock = Lock()
    rest_debug_log: Optional[IO] = None
    rest_count = 0

    def __init__(self, managementServers, user, configFile, logger, **auth):
        self.is_debug = cm_conf.debug
        self.managementServer = None
        self.managementServers = []
        self.httpRequestTimeout = cm_conf.timeout
        self.randomSleepBetweenRequests = RandomSleepTime(start=0.5, stop=1)
        self.randomSleepBeforeChangingMgmt = RandomSleepTime(start=0, stop=0.2)
        self.maxHttpRequestRetries = cm_conf.max_http_retries
        self.maxManagementsRotations = cm_conf.max_mgmt_rotations
        self.warn_on_res_size_above_mb = cm_conf.warn_on_res_size_above_mb
        self.configFile = configFile
        self.logger = logger or getLogger('ManagementConnection')
        self.setManagementServers(managementServers)
        self.currentMgmtIndex = random.randint(0, len(self.managementServers) - 1)
        self.user = user
        self.auth = auth
        self.password = auth.get('password')
        # Session ID can't JUST be user, because if password changes, we should NOT re-use the session
        userhash = md5((self.user+(self.password or 'MTLS-AUTH')).encode()).hexdigest()
        self.session = TunnelSession(userhash)
        self.session.verify = False
        if auth.get('use_tls'):
            self.session.cert = (auth.get('cert'), auth.get('key'))
            self.session.verify = auth.get('ca', True)
            self.logger.debug(f'Created secured session with cert: {self.session.cert}, verify: {self.session.verify}')

        # This finds a "live" server.  Required before calls to get() or post(), etc.
        self.isAlive()
        try:
            err, out = self.get('/systemInfo')
            assert isinstance(out, dict) and 'dbUUID' in out, f'/systemInfo failed. {err}'
            self.dbUUID = out['dbUUID']
        except Exception as e:
            self.logger.debug(f'{repr(e)}')
            try:
                err, out = self.get('/dbUUID') # Obsolete from 3.3
                assert isinstance(out, dict) and 'dbUUID' in out, f'/dbUUID failed. {err}'
                self.dbUUID = out['dbUUID']
            except Exception as e:
                self.dbUUID = None
                self.logger.warning(f'Failed to get dbUUID for {managementServers} when creating connection: {repr(e)}')

    def getCredentials(self):
        return self.user, self.password

    def setManagementServers(self, managementServers=None):
        if managementServers:
            self.managementServers = managementServers if isinstance(managementServers, list) else [managementServers]

    def isAlive(self):
        index = 0
        currentRotation = 0

        while currentRotation < self.maxManagementsRotations:
            try:
                err, out = self.get('/isAlive')
                self.logger.debug(f'IS-ALIVE: {self.managementServer} #{index} R-{currentRotation + 1} of {self.maxManagementsRotations} OK? {(not bool(err))}')
                return not err
            except (ManagementTimeout, ManagementConnectError) as ex:
                if 'CERTIFICATE_VERIFY_FAILED' in repr(ex):
                    self.logger.info(f'SSL certificate verification failed for {self.managementServer}. Trying next management.')
                elif 'SSLError' in repr(ex):
                    if self.auth.get('use_tls'):
                        self.logger.info(f'SSLError for {self.managementServer} but TLS is required. Trying next management.')
                    else:
                        self.logger.info(f'SSLError! Switching from https to http')
                        self.managementServers = [s.replace('https', 'http') for s in self.managementServers]
                        self.managementServer = self.managementServer.replace('https', 'http')
                        continue
                self.getNextMgmtIndex()
                index += 1
                currentRotation += 1 if index % len(self.managementServers) == 0 else 0
                sleepBeforeChangingMgmt = self.randomSleepBeforeChangingMgmt.getValue()
                self.logger.debug(
                    "failed isAlive to: {0}, exception: {1}, waiting for: {2}s before next request".format(self.managementServer, ex, sleepBeforeChangingMgmt))
                time.sleep(sleepBeforeChangingMgmt)

        raise ManagementTimeout(msg="Tried isAlive on all Management Servers in rotation for {} rotations and all failed".format(
                                        self.maxManagementsRotations), iport=', '.join(self.managementServers))

    def getNextMgmtIndex(self):
        if len(self.managementServers) != 1:
            self.currentMgmtIndex = (self.currentMgmtIndex + 1) % len(self.managementServers)
            self.session.host = None  # Reset cached hostname when switching servers

    def post(self, route, payload=None, **kwargs):
        return self.request('post', route, payload, **kwargs)

    def get(self, route, payload=None, **kwargs):
        return self.request('get', route, payload, **kwargs)

    def request(self, method, route, payload=None, **kwargs):
        if not kwargs.get('numberOfRetries'):
            route = Utils.encodePlusInRoute(route)

        self.managementServer = self.managementServers[self.currentMgmtIndex]
        return self.doRequest(method, route, payload, **kwargs)

    @property
    def debug_log(self):
        ''' Return rest-debug-logfile, if configured '''
        from xlro.core import infra_conf
        from os import environ
        if not Connection.rest_debug_init:
            with Connection.rest_debug_lock:
                if not Connection.rest_debug_init:
                    Connection.rest_debug_init = True
                    debug_path = environ.get('REST_DEBUG', infra_conf.root.logging.rest_debug)
                    if debug_path:
                        try:
                            Connection.rest_debug_log = open(debug_path, 'w')
                        except Exception as e:
                            self.logger.info(f'Failed to open REST_DEBUG: {debug_path}. {repr(e)}')
        return Connection.rest_debug_log

    def write_log(self, method, route, payload, start, err=None, out=None, exception=None, rest_log=None):
        log = rest_log or self.debug_log
        if log:
            try:
                with Connection.rest_debug_lock:
                    masked_payload = mask_hidden_fields(payload)
                    json.dump(dict(METHOD=method, ROUTE=route, PAYLOAD=masked_payload, OUT=out, ERR=err,
                                EXCEPTION=repr(exception) if exception else None, SERVER=self.managementServer,
                                REQTIME=time.asctime(time.localtime(start)), ELAPSED=f'{time.time()-start:.3f}s',
                                ID=self.session.cert if self.session.verify else self.user),
                            cls=BytesEncoder, fp=log, indent=2)
                    log.write('\n')
                    log.flush()
            except:
                pass

    def doRequest(self, method, route, payload=None, rest_log=None, **kwargs):
        ''' Wrapper to handle logging '''
        try:
            self.__class__.rest_count += 1
            start = time.time()
            err, out = self._doRequest(method, route, payload=payload, **kwargs)
            self.write_log(method, route, payload, start, err=err, out=out, rest_log=rest_log)
            return err, out
        except Exception as e:
            self.write_log(method, route, payload, start, exception=e, rest_log=rest_log)
            raise

    def _doRequest(self, method, route, payload=None, **kwargs):
        CHANGE_PASSWD_MSG = 'Changing password is required'
        SERVICE_UPGRADE_MODE = 'upgrade mode'
        LOGIN_MSGS = [ # What a HACK :-(
                'Please login via the /login route.',
                '<form action="/login"'
        ]
        IS_ALIVE_TIMEOUT = 2
        isAliveRoute = route == '/isAlive'
        volumeSaveRoute = 'volumes/save' in route
        timeout = kwargs.get('timeout')
        numberOfRetries = kwargs.get('numberOfRetries', 0)

        if volumeSaveRoute:
            volName = payload[0]['name']
        startTime = None

        if not isAliveRoute:
            self.logger.debug(f'In doRequest method={method} route={route} payload={mask_hidden_fields(payload)} timeout={timeout}, numberOfRetries={numberOfRetries}')
        url = ''
        try:
            url = urlparse.urljoin(str(self.managementServer), str(route))
            self.logger.debug('Doing request to: {}'.format(url))

            if method == 'post':
                if self.is_debug and volumeSaveRoute:
                    startTime = time.time()

                with self.session as s:
                    res = s.post(url, json=payload, timeout=self.httpRequestTimeout if not timeout else timeout)

                if startTime:
                    execTime = (time.time() - startTime) * 1000
                    err, jsonObject = self.handleResponse(res)
                    self.logger.debug("id: {0}, err: {1}, res: {2}, it took me: {3}ms to save".format(volName, err, json.dumps(jsonObject), execTime))
            elif method == 'get':
                with self.session as s:
                    res = s.get(url, params=payload, timeout=IS_ALIVE_TIMEOUT if isAliveRoute else self.httpRequestTimeout)
            else:
                raise Exception(f'Unimplemented request method: {method}')

            if CHANGE_PASSWD_MSG in res.text:
                raise ChangePasswordRequiredError(f'Login to {self.managementServer} failed - {CHANGE_PASSWD_MSG} via GUI or using curl command', url)

            if SERVICE_UPGRADE_MODE in res.text:
                raise ManagementConnectError(f'Management {self.managementServer} is in upgrade mode and cannot process new requests')

            if len(res.text) / ( 1024 * 1024) > self.warn_on_res_size_above_mb:
                self.logger.warning(f'Response size for {url} is {len(res.text) / ( 1024 * 1024)} MB')

            if any([m in res.text for m in LOGIN_MSGS]) or (res.status_code == http.HTTPStatus.UNAUTHORIZED and 'Operation not permitted' not in res.text):
                res = self.login()
                if not res:
                    raise ManagementLoginError(f'Login to {self.managementServer} failed.', url)

                if res and volumeSaveRoute:
                    self.logger.debug("after login, id: {0}, res: {1}".format(volName, str(res.content)))

                success = json.loads(res.content)['success']
                if success:
                    return self._doRequest(method, route, payload, **kwargs)

            if not isAliveRoute:
                self.logger.debug(f'method={method} route={route} got response: {str(res.content)}')

            err, jsonObj = self.handleResponse(res)
            return err, jsonObj

        except Exception as ex:
            self.logger.debug(f'Request fail.  Method={method}, Route={route}, Error: {repr(ex)}')
            if self.is_debug and volumeSaveRoute:
                execTime = -1
                if startTime:
                    execTime = (time.time() - startTime) * 1000

                self.logger.debug("ex:id {0}, Request to {1} failed, ex: {2}, it took me: {3}ms, ex type: {4}, ex name: {5}".format(
                    volName, route, ex, execTime, type(ex), type(ex).__name__))

            if isAliveRoute:
                if isinstance(ex, ConnectionManagerError):
                    raise
                raise ManagementTimeout(url, str(ex))
            elif numberOfRetries < self.maxHttpRequestRetries:
                kwargs['numberOfRetries'] = numberOfRetries + 1
                sleepTimeBetweenRequestRetry = self.randomSleepBetweenRequests.getValue()
                self.logger.debug(f"Got exception: sleeping {sleepTimeBetweenRequestRetry}s then retrying.")
                time.sleep(sleepTimeBetweenRequestRetry)
                return self.request(method, route, payload, **kwargs)
            else:
                self.logger.debug("Request to {0}, failed {1} times. Trying to change management server.".format(route, self.maxHttpRequestRetries))
                # JW: Why isAlive()?  Why not just getNextMgmtIndex? Maybe only isAlive tries login?
                failedServer = self.managementServer
                isAlive = self.isAlive()
                if not isAlive:
                    if isinstance(ex, ConnectionManagerError):
                        raise
                    raise ManagementTimeout(url, str(ex))

                if failedServer == self.managementServer:
                    # If isAlive() thinks current server is OK, but we got repeated exception, just reraise vs retry
                    raise

                # isAlive() has rotated currentMgmtIndex to a live server, so we try again
                kwargs['numberOfRetries'] = 0
                try:
                    return self.request(method, route, payload, **kwargs)
                except Exception as e:
                    # Not sure why this is here.  Maybe needed before isAlive to not waste time on dead mgr?
                    if self.currentMgmtIndex + 1 == len(self.managementServers):
                        raise e

    @staticmethod
    def handleResponse(res):
        jsonObj = None
        err = None

        if res.status_code in [http.HTTPStatus.OK, http.HTTPStatus.NOT_MODIFIED]:
            try:
                if res.content:
                    jsonObj = json.loads(res.content)
            except Exception as ex:
                err = {
                    "code": res.status_code,
                    "message": str(ex),
                    "content": res.content
                }
        else:
            err = {
                "code": res.status_code,
                "message": res.reason,
                "content": res.content
            }

        return err, jsonObj

    def login(self):
        try:
            start = time.time()
            with self.session as s:
                payload = {"username": self.user, "password": self.password}
                out = s.post(f'{self.managementServer}/login', data=payload,
                             verify=False, timeout=self.httpRequestTimeout)
                s.cookies.save(ignore_discard=True)
            self.write_log('POST', '/login', payload, start, err=None, out=str(out))
            self.logger.info(f'LOGIN to {self.managementServer} SUCCESS. OUT={out}')
            return out
        except requests.ConnectionError as ex:
            self.write_log('POST', '/login', payload, start, err=None, out=None, exception=ex)
            self.session = TunnelSession()
            self.logger.info(f'LOGIN to {self.managementServer} FAILURE.  {repr(ex)}')
            raise ManagementTimeout(self.managementServer, str(ex))
