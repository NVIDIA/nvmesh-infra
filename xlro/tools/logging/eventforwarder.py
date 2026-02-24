#!/usr/bin/env python3

# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

'''
Client for Management Events
'''
import json
import logging
import re
import sys
import os
import time
from collections import defaultdict

import yaml
from typing import Optional, Dict, Sequence, Callable, Tuple, Any

import socket
import websocket
import ssl

from websocket import WebSocketConnectionClosedException

from xlro.core.util.cli_util import CLIArgumentParser
from xlro.core.util.common import get_path

sslopt = {"cert_reqs": ssl.CERT_NONE}
logger = logging.getLogger('EventForwarder')
BACKOFF_MAX: int = 60
AFFECTED_ENTITIES_CONF = get_path(f'{os.path.dirname(__file__)}/ename2affected.yaml', 'ename2affected.yaml')
E2A_CACHE = defaultdict(dict)
DEFAULT_EVENTS = [
    # 'backupChangeEvent',
    # 'newBackupEvent',
    # 'backupRemovedEvent',
    # 'serversCountChangeEvent',
    # 'clientsCountChangeEvent',
    # 'volumesCountChangeEvent',
    'nicsCountChangeEvent',
    'disksCountChangeEvent',
    # 'allocatedSpaceChangeEvent',
    # 'allocatedSpaceDirtyEvent',
    # 'largestVolumesChangeEvent',
    'dirtyBitsChangeEvent',
    'volumeDeletedEvent',
    'volumesVersionsChangeEvent',
    'zonesRanksChangeEvent',
    'targetZoneChange',
    'canExportVolumeViaNvmfChangedEvent',
    'targetRemovedEvent',
    'newTargetEvent',
    'targetFailureEvent',
    'targetWentOnlineEvent',
    'targetWebSocketStatusChangedEvent',
    'newDiskEvent',
    'diskRemovedEvent',
    'diskReappearEvent',
    'diskStatusChangeEvent',
    # 'segmentsChangedOnDiskEvent',
    'driveZeroingProgressChangeEvent',
    'drivePoolChangeEvent',
    'volumeDeletionZeroingProgressChangeEvent',
    'diskFailureEvent',
    'diskWentOnlineEvent',
    'diskEvictedEvent',
    'newNicEvent',
    'nicRemovedEvent',
    'nicReappearEvent',
    'nicChangeEvent',
    'nicFailureEvent',
    'nicWentOnlineEvent',
    'clientRemovedEvent',
    'newClientEvent',
    # 'clientFailureEvent',
    'clientWentOnlineEvent',
    'volumeRemovedEvent',
    'newVolumeEvent',
    'targetControlJobs',
    'attachVolumesEvent',
    'detachVolumesEvent',
    'restartClientEvent',
    'shutdownClientEvent',
    'formatDiskEvent',
    'DiskFinishedFormatEvent',
    # 'resendReportEvent',
    'volumeRemapEvent',
    'volumeExtendedEvent',
    'volumeStatusChangeEvent',
    'volumeActionChangeEvent',
    'volumeFailureEvent',
    'volumeWentOnlineEvent',
    'newManagementInClusterEvent',
    'remainingServersForBatchCompletionEvent',
    'remainingClientsForBatchCompletionEvent',
    'configurationChangeEvent',
    'rollupPolicyChangeEvent',
    'sendStatisticsEvent',
    'collectStatisticsChangedEvent',
    # 'newLogEvent',
    # 'logChangedEvent',
    'volumeVersionChangeEvent',
    # 'sendClientReportEvent',
    'updateConfigProfileEvent',
    'clientConfigProfileUpdated',
    'targetConfigProfileUpdated',
    'restartRequiredChanged',
    'configProfileUserOverrideChanged',
    'newNodeEvent',
    'nodeRemovedEvent',
    'reconnectOnNewStatsAddressEvent',
    'hostname',
    'generalSettingsChangeEvent',
]


class ManagementLoginFailure(Exception):
    def __init__(self, err):
        super().__init__(err)


class ManagementWebSocketClient(object):
    ''' Connection to login and listen for events, based on Python example from Gil '''
    def __init__(self, address: str, username: str, password: str, secure: bool = True, forward: str = None, client_id: str = 'python-ws-client', backoff_timer_max=BACKOFF_MAX):
        self.address = address
        self.secure = secure
        self.url = f'ws{"s" if self.secure else ""}://{self.address}'
        self.fsock_args = self.get_fsock_args(forward)
        self.fsock = None
        self.client_id = client_id
        self.accessToken = None
        self.shouldContinue = True
        self.username = username or 'admin@nvidia.com'
        self.password = password or 'admin'
        self.ws = None
        self.timer_max = backoff_timer_max
        self.reconnect()

    def get_fsock_args(self, forward: Optional[str]) -> Optional[Tuple[str, int]]:
        if not forward:
            return None

        fhost, _, fport = forward.rpartition(':')
        return fhost or "127.0.0.1", int(fport)

    def login(self):
        login_msg = {
            'route': '/login',
            'email': self.username,
            'password': self.password,
        }

        self._send(login_msg)
        res = self._receive()
        if res is None or not res.get('success'):
            raise ManagementLoginFailure(f'Login failure: {"No Response" if res is None else res.get("err", "unknown error")}')
        logger.debug('login successful')
        self.accessToken = res.get('accessToken')
        # TODO: get management ID, version
        return

    def reconnect(self):
        interval = 1
        logger.debug(f'Connecting to: {self.url}')
        while True:
            try:
                if self.fsock_args and not self.fsock:
                    self.fsock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    self.fsock.connect(self.fsock_args)

                self.ws = websocket.create_connection(self.url, sslopt=sslopt, timeout=BACKOFF_MAX)
                self.login()
                break
            except (ConnectionRefusedError, OSError) as e:
                # Connection refused or host unreachable
                logger.debug(f'Failed connecting to {self.url}: {repr(e)}. Retry in {interval} seconds...')
                time.sleep(interval)
                interval = interval * 2 if interval * 2 < self.timer_max else self.timer_max

    def register_to_events(self, event_names: Sequence, do_on_event: Callable, *args, **kwargs):
        ''' Register for set of named events, and for each received, call do_on_event(event) '''
        register_to_events_msg = {
            'route': '/registerToEvents',
            'payload': {
                'events': event_names
            }
        }

        self._send(register_to_events_msg)

        while self.shouldContinue:
            res = self._receive()
            if res:
                do_on_event(res.get('payload'), *args, **kwargs)

    def _send(self, data: Dict):
        ''' Send formatted message to Mgmt on WS '''
        msg = self._build_message(data)
        logger.debug(f'Send: {json.dumps(msg, indent=2)}')
        self.ws.send(json.dumps(msg))

    def _receive(self) -> Optional[Dict]:
        ''' Receive formatted response from Mgmt on WS '''
        logger.debug("Receiving...")
        result = self.ws.recv()
        if not result:
            # Most likely a timeout - otherwise an exception would be raised
            return None

        logger.debug(f'RECIEVED:{os.linesep}{json.dumps(result, indent=2)}{os.linesep}')
        return json.loads(result)

    def close(self):
        self.ws.close()
        self.fsock.close()
        self.fsock = None

    def _build_message(self, data):
        ''' Wrap the data with the required envelope '''
        msg = {
            'registrant': {
                'id': self.client_id,
                'type': 'python-ws-client'
            },
            'accessToken': self.accessToken
        }

        msg.update(data)
        return msg

def _get_path_values(payload, path):
    if not path:
        return [str(payload)]

    key, _, rest = path.partition('.')
    try:
        payload = payload[key]
        if isinstance(payload, list):
            return [i for l in [_get_path_values(p, rest) for p in payload] for i in l]
        else:
            return _get_path_values(payload, rest)
    except Exception:
        return []

def _get_affected_conf(event_name, affected_conf):
    if event_name not in E2A_CACHE:
        for lookup, affected_entity2field in affected_conf.items():
            if lookup == event_name or (lookup.startswith('r/') and re.search(lookup[2:], event_name, re.IGNORECASE)):
                E2A_CACHE[event_name].update(affected_entity2field)

        E2A_CACHE.setdefault(event_name, {})

    return E2A_CACHE.get(event_name)

def get_affected_entities(event: Dict['str', Any], affected_conf: Dict[str, dict]) -> Optional[str]:
    event_name = event.get('eventName')
    if not event_name:
        return

    affected_entities_conf = _get_affected_conf(event_name, affected_conf)
    if not affected_entities_conf:
        return

    return ' '.join([f'{entity_name}:{v}' for entity_name, path in affected_entities_conf.items() for v in _get_path_values(event, path)])


def main():
    parser = CLIArgumentParser(require_manager=False)
    # NOTE: alternative to -M so as not to create an unnecessary HTTP connection and other overhead
    parser.add_argument('-m', '--mgr', default='', required=True, help='mhost[:mport] (Use this, NOT -M)')
    parser.add_argument('-i', '--id', action='store_true', default='event-subscriber', help='Client ID')
    parser.add_argument('-p', '--print', action='store_true', help='Print events. Default is false if forwarding, otherwise true.')
    parser.add_argument('-j', '--json', action='store_true', help='Print events as 1-line JSON.')
    parser.add_argument('-f', '--forward', help='Forwarding address as [host:]port')
    parser.add_argument('-b', '--backoff-timer-max', type=int, default=BACKOFF_MAX, help='Max interval for reconnection to MGMT')
    parser.add_argument('event', nargs='*', default=DEFAULT_EVENTS, help='NVMesh Manager as host[:port]')
    parser.add_argument('-a', '--affected-entities', default=AFFECTED_ENTITIES_CONF, help='YAML conf file for affected entities enrichment')
    args = parser.parse_args()
    if not args.forward:
        args.print = True

    mhost, _, mport = args.mgr.partition(':')
    user, _, passwd = args.user.partition(':')
    c = ManagementWebSocketClient(client_id=args.id, address=f'{mhost}:{mport or 4001}', username=user, password=passwd,
                                  forward=args.forward, backoff_timer_max=args.backoff_timer_max)

    # TODO: this is very inefficient for forwarder - parses and reformats the json
    # OTOH: this allows enrichment, but maybe that should be done downstream

    def handle_event(event, fsock, affected_conf):
        if 'payload' in event:
            event = event['payload']
        else:
            event['_no_payload'] = True

        if not isinstance(event, dict):
            logger.warning(f'Ignoring event: {event}, type: {type(event)}')
            return

        ns = time.time_ns()
        # JSON float will lose precision in most tools, and Fluent accepts the parts individually
        event['ts_sec'] = ns // 1000000000
        event['ts_nano'] = ns % 1000000000
        event.setdefault('host', mhost)
        event.setdefault('affectedEntities', get_affected_entities(event, affected_conf))
        event.setdefault('message', f'event: {event.get("eventName", "UNKNOWN")}. entities: {event.get("affectedEntities", "UNKNOWN")}')
        event.pop('_id', None)  # _id is being used by OpenSearch
        if args.print or args.json:
            json.dump(event, sys.stdout, indent=None if args.json else 2)
            sys.stdout.write(os.linesep)
            sys.stdout.flush()
        if fsock:
            fsock.sendall(json.dumps(event).encode())

    if args.event[0] == '-':
        event_names = set(DEFAULT_EVENTS) - set(args.event[1:])
    elif args.event[0] == '+':
        event_names = set(DEFAULT_EVENTS)
        event_names.update(args.event[1:])
    else:
        event_names = args.event

    with open(args.affected_entities, 'r') as fp:
        affected_entities_conf = yaml.safe_load(fp.read())  # type: Dict[str, list]

    while True:
        try:
            c.register_to_events(list(event_names), handle_event, c.fsock, affected_entities_conf)
        except (WebSocketConnectionClosedException, BrokenPipeError, TimeoutError, socket.timeout) as e:
            logger.warning(f'About to reconnect due to: {repr(e)}')
            if not isinstance(e, WebSocketConnectionClosedException):
                c.close()
            c.reconnect()
        except Exception as e:
            logger.error(f'Unhandled exception caught: {repr(e)}')
            raise e


if __name__ == '__main__':
    main()
