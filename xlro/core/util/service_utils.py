# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from xlro.core.entities import Service, Host
from xlro.core.util.ssh import Connection
from collections import OrderedDict


def get_services_list(host_name):
    out, error, cmd = Connection.execute_on_host(host=host_name, cmd="systemctl | grep 'NVMesh(r)' | grep -o '\<nvmesh[^[:blank:]]*' | cut -f 1 -d '.'")
    return [Service.instance(host=Host.instance(name=host_name), name=service_name) for service_name in out.split()]


def get_services_and_pids_dict(host_name):
    services_list = get_services_list(host_name)
    services_and_pids_dict = {}
    for service in services_list:
        cmd = "sudo service {} status | grep 'Main PID' | awk '{{ print $3 }}'".format(service.name)
        out, error, cmd = Connection.execute_on_host(host=host_name, cmd=cmd)
        services_and_pids_dict[service.name.replace('nvmesh', '')] = out
    return services_and_pids_dict


def get_service_dependencies(host_name, service_name, top=True, results=None):
    results = OrderedDict() if results is None else results
    cmd = f"systemctl list-dependencies --reverse --plain --no-pager {service_name} | grep -E '^\s+[^ ]+\.service$'"
    out1, _, _ = Connection.execute_on_host(host=host_name, cmd=cmd)
    local_dependencies = out1.split()
    for dep in local_dependencies:
        cmd2 = f'systemctl show {dep} -p Requires\,PartOf | grep -q "{service_name}"'
        out2, error2, status2 = Connection.execute_on_host(host=host_name, cmd=cmd2)
        if status2 == 0:
            dep_original = dep
            dep = "client.service" if dep == "nvmeshclient.service" else dep  # T.B.D is there better way to align with INFRA
            dep = "target.service" if dep == "nvmeshtarget.service" else dep  # T.B.D is there better way to align with INFRA
            dep = "toma.service" if dep == "nvmeshtoma.service" else dep  # T.B.D is there better way to align with INFRA
            results[dep] = None
            results = get_service_dependencies(host_name, dep_original, False, results)
    return [service_name.replace(".service", "") for service_name in results] if top else results


'''
main to verify get_service_dependencies function
'''
if __name__ == "__main__":
    import sys

    if len(sys.argv) == 1:
        print(f'Usage: {sys.argv[0]} host_name [root_service]')
        exit(0)
    host = sys.argv[1]
    if len(sys.argv) == 2:
        print([x.name for x in get_services_list(host)])
        exit(0)
    service = sys.argv[2]
    dependency_as_list = get_service_dependencies(host, service)
    print(f'host={host} root={service} dep={dependency_as_list}')
