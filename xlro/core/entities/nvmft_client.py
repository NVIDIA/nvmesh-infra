# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import json
from typing import Iterable, Dict, Any

from xlro.core.entities import ExternalClient, Volume
from xlro.core.entities.base import entity, SourceTypes
from xlro.core.util.ssh import Connection


@entity(sourcetypes=[SourceTypes.PROC])
class NVMftClient(ExternalClient):
    def get_subsystems(self):
        RPC_PATH = '/opt/nvmesh/nvmft/spdk/scripts/rpc.py'
        return json.loads(Connection.err2exc(self.host.execute(f"sudo {RPC_PATH} nvmf_get_subsystems")))

    def do_setup(self):
        pass

    def do_teardown(self):
        pass

    def do_bind(self, volumes: Iterable[Volume], *args, **kwargs) -> Dict[Volume, Any]:
        #expected_nqns = []
        #for vol in volumes:
        #    vol.nvmf_enabled = True
        #    vol.enabled_nvmf_clients += self.nvmesh_client.name  # Can cause race conditions w/ multiple nvmft clients?
        #    expected_nqns.append(f'nqn.2016-06.io.spdk:{vol.name}{self.nvmesh_client.name}')
        #Volume.update_many(volumes)

        #self.nvmesh_client.attach(volumes, wait_till_completed=True, *args, **kwargs)

        #def all_subsystems_exist():
        #    existing_nqns = [s['nqn'] for s in self.get_subsystems()]
        #    return set(existing_nqns) == set(expected_nqns)
        #wait_for_it(all_subsystems_exist, poll=2, timeout=120).assert_result()

        #Connection.err2exc(self.initiator.host.execute(' && '.join(
        #    [f'sudo nvme connect -t {s["transport"]} -a {s["address"]} -s {s["port"]} -q {s["nqn"]} -n {s["nqn"]}' for s in self.get_subsystems()]
        #)))
        #nvmft_devs = Connection.err2exc(self.initiator.host.execute("sudo nvme list | grep 'NVIDIA NVMesh' | awk '{print $1}' | xargs")).strip().split(' ')
        # TODO: continue...
        pass

    def do_unbind(self, *args, **kwargs):
        pass
