# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from builtins import str
from builtins import object
from xlro.core.entities import Manager, MgmtHost
from collections import namedtuple, Iterable
from collections.abc import Mapping
from multiprocessing import dummy as multithread


class Doctor(object):
    Health_issue = namedtuple('H_issue', ['check', 'fix'])

    health_issues = {'services': Health_issue(check=['check_services'], fix=['restart_services']),
                     'ports': Health_issue(check=['check_ports'], fix=[])}

    def __init__(self, manager):
        self.manager = manager
        self.subsystems = self.manager.get_all_subsystems()
        self.network_nodes = self.manager.get_all_network_nodes()

    @staticmethod
    def check_service(service):
        valid_statuses = (0, 4) # running or not exists
        return service.key() if service.status() not in valid_statuses else None

    def check_services(self):
        pool = multithread.Pool(20)
        results = pool.map(self.check_service, [service for host in self.subsystems for service in list(host.services.values())])
        pool.close()
        return {'services': set([service for service in results if service is not None])}

    def check_ports(self):
        all_ports = [port for node in self.network_nodes for nic in node.nics for port in nic.ports]
        bad_ports_keys = [port.key() for port in all_ports if port.net_state.lower() in port.STATUS_DOWN]
        return {'ports': set(bad_ports_keys)}

    def restart_services(self, diagnose=None):
        services = [service for host in self.subsystems for service in list(host.services.values()) if
                    not isinstance(host, MgmtHost)]
        if diagnose is not None:
            services = [service for service in services if service.key() in diagnose.get('services', {})]
        for service in services:
            service.restart()


class Diagnose(dict):

    def __bool__(self):
        return bool(len(list(self.values())) and max(self.values()))

    def __str__(self):
        s = ""
        for key, val in [(key, val) for key, val in self.items() if val]:
            s += "\n{key}:{val}".format(key=key, val=str(val))
        return s

    def update(self, diagnose=None, health_issue='other'):
        if not diagnose:
            return self

        elif not isinstance(diagnose, Mapping):
            diagnose = {health_issue: diagnose}

        for key, value in diagnose.items():
            value = value if isinstance(value, Iterable) and not isinstance(value, str) else [value]
            self.setdefault(key, set()).update(value)

        return self
