#!/usr/bin/env python

# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import print_function
from builtins import object
from abc import ABCMeta, abstractmethod
from typing import Optional, Callable, Mapping, Any
import logging
import re
from os import path
from threading import Thread
from concurrent.futures import ThreadPoolExecutor
from xlro.core.util.general_utils import IDAdapter
from xlro.core import infra_conf

class EventKeyFilter(object):
    ''' Check event for required and/or regex matching keys '''
    logger = logging.getLogger('KeyFilter')
    def __init__(self, required, **matches):
        self.required = required or []
        self.matches = matches
        self.regexps = {key: re.compile(matches[key]) for key in matches}

    def __call__(self, event):
        if not all(key in event for key in self.required):
            self.logger.debug('Filtering failed. required: {} <> actual: {}'.format(self.required, list(event.keys())))
            return False
        for key, regex in self.regexps.items():
            if key not in event or not regex.search(event[key]):
                self.logger.debug('Filtering failed. [{}] {} <> {}'.format(key, event.get(key, ''), self.matches[key]))
                return False
        self.logger.debug('Filtering passed. Keys: {}, Matches: {}'.format(self.required, self.matches))
        return True

    def __str__(self):
        return '{}: required: {}, matches: {}'.format(self.__class__.__name__, self.required, self.matches) 

class Action(object): # Should be ABCMeta?
    def __init__(self, condition: Optional[Callable] = None, is_async: bool = False, **defaults: str) -> None:
        self.condition = condition
        self.defaults = defaults
        self.is_async = is_async
        self.logger = IDAdapter(logging.getLogger(self.__class__.__name__))
        self.name = self.logger.extra['logger_name']

    @abstractmethod
    def doit(self, event: dict) -> None:
        ''' This is the method to override to perform the action '''
        # YAGNI? Consider returning a bool for success/failure, a new event to allow chaining?
        self.logger.debug('NOOP action on Event: {}'.format(event))
        # For now, no return convention

    def handle_event(self, raw_event: Mapping) -> Any:
        ''' Enrich event with defaults. Then, if condition(), call doit().
            (NOTE: Override doit(), not this.)
        '''
        event = self.defaults.copy()
        event.update(raw_event)
        if self.condition is None or self.condition(event):
            self.logger.debug(f'Condition matched for Action: {self} on Event: {event}')
            if self.is_async:
                t = Thread(target=self.doit, args=(event,), name=self.name)
                t.start()
                return t
            else:
                return self.doit(event)


class RemoteCmdAction(Action):
    # Don't conflict with Monitor/Message keys
    HOST_KEY = 'act_host' # Default to {host}

    def __init__(self, cmd, host='{host}', logfile=None, **kwargs):
        self.cmd = cmd
        self.host = host
        self.logfile = logfile
        super(RemoteCmdAction, self).__init__(**kwargs)

    def doit(self, event):
        from xlro.core.util.ssh import Connection, execute_cmd_locally
        # Note: Don't use cmd which is used by the monitors, I think
        try:
            host = event.get(self.HOST_KEY, self.host).format(**event)
            cmd = self.cmd.format(**event)
            if host == 'localhost':
                out, error, code = execute_cmd_locally(cmd, timeout=event.get('timeout', None))
            else:
                out, error, code = Connection.execute_on_host(host, cmd, **event.get('exec_args', {}))
            if self.logfile:
                try:
                    if self.logfile == '-':
                        print((out or error).strip())
                    else:
                        logfile = self.logfile.format(**dict(event, host=host))
                        if not path.isabs(logfile):
                            logfile = path.join(infra_conf.root.logging.logdir or '.', logfile)
                        with open(logfile, 'w') as logfp:
                            logfp.write(out or error)
                except Exception as log_e:
                    self.logger.info('Log failed. ({}) {}'.format(type(log_e), log_e))
        except Exception as e:
            self.logger.info('Action failed. ({}) {}'.format(type(e), e))

class MultiRemoteCmdAction(RemoteCmdAction):
    def __init__(self, cmd, hosts=None, **kwargs):
        self.hosts = hosts
        super(MultiRemoteCmdAction, self).__init__(cmd, **kwargs)

    def doit(self, event):
        hosts = event.pop(self.HOST_KEY, self.hosts).format(**event).split()
        with ThreadPoolExecutor(len(hosts)) as executor:
            m = executor.map(lambda h: super(MultiRemoteCmdAction, self).doit(dict({self.HOST_KEY: h}, **event)), hosts)

class ClusterCmdAction(MultiRemoteCmdAction):
    ''' MultiRemoteCmd with special keys for pre-defined cluster hosts.
    HOST_KEY can use: {manager} {clients} {targets} or {nodes} as well as any event key
    '''
    def doit(self, event):
        from xlro.core.entities import Manager
        manager = Manager.get_manager()
        event[self.HOST_KEY] = event.get(self.HOST_KEY, self.hosts).format(
                manager=manager.host,
                clients=' '.join([h.name for h in manager.clients]),
                targets=' '.join([h.name for h in manager.targets]),
                nodes=' '.join({h.name for h in manager.targets}),
                **event
            )
        return super(ClusterCmdAction, self).doit(event)

__all__ = [ 'EventKeyFilter', 'Action', 'RemoteCmdAction', 'MultiRemoteCmdAction', 'ClusterCmdAction' ]

