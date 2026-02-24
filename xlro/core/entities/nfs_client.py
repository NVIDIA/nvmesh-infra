# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import random
import threading
from collections import defaultdict
from typing import Iterable, Dict, Any, List

from xlro.core.entities import ExternalClient, Volume, Client, Service
from xlro.core.entities.base import entity, SourceTypes, PropertySpec
from xlro.core.util.ssh import Connection

@entity(sourcetypes=[SourceTypes.PROC])
class NfsClient(ExternalClient):
    fs_type: str = PropertySpec(str, default='ext4')
    is_using_rdma: bool = False
    NFS_LOCK: threading.Lock = threading.Lock()
    CLIENT2BINDING: Dict[Client, Any] = defaultdict()  # class level dictionary so that each instance is aware not to repeat fs create on nfs server

    RDMA = 'rdma'
    TCP = 'tcp'
    NFS_PROTOCOLS = [RDMA, TCP]
    NFS_CLIENT_SERVICE = "nfs-utils"
    NFS_SERVER_SERVICES = ["rpcbind", "nfs-server", "nfs-idmapd"]

    @property
    def nfs_server_services(self) -> List[Service]:
        return [self.client_node.services[service] for service in self.NFS_SERVER_SERVICES]

    @property
    def nfs_client_services(self) -> List[Service]:
        return [Service.instance(name=self.NFS_CLIENT_SERVICE, host=self.initiator)]

    @staticmethod
    def mount_point(v: Volume):
        return f'/mnt/{v.name}'

    def do_setup(self):
        # TODO: I don't like the install packages. need to reconsider this.
        from xlro.qa.utils.host_utils import HostUtils

        HostUtils([self.initiator.name, self.host.name]).install_packages_if_needed(['nfs-kernel-server' if "ubuntu" in self.initiator.platform else 'nfs-utils'], install=True)
        for service in self.nfs_server_services + self.nfs_client_services:
            assert not service.restart(), f"Failed to restart NFS service {service} on {service.host}"

    def do_teardown(self):
        for service in self.nfs_server_services + self.nfs_client_services:
            assert not service.stop(), f"Failed to stop NFS service {service} on {service.host}"

        if self.is_using_rdma:
            for host in [self.initiator, self.host]:
                Connection.err2exc(host.execute('sudo modprobe -r rpcrdma'))
            self.is_using_rdma = False

    def exportfs(self, initiator, fs, undo=False):
        # TODO: this solution is not persistent but IMO, better than tampering w/ exports file. If we ever need to support reboots we need to change this
        export_data = ' '.join([f"{p.ip}:{fs.mount_point}" for n in initiator.nics for p in n.ports])
        # chmod 777 is really necessary??
        export_op = '-u' if undo else f'-o rw,no_root_squash && sudo chmod 777 {fs.mount_point}'
        return Connection.err2exc(self.host.execute(f"sudo exportfs {export_data} {export_op}"))

    def do_bind(self, volumes: Iterable[Volume], protocol: str = TCP, *args, **kwargs) -> Dict[Volume, Any]:
        # TODO: refactor FS and Filesystem, move to core entities
        from xlro.infra.test.utils import FS, Filesystem

        if protocol == self.RDMA and not self.is_using_rdma:
            Connection.err2exc(self.initiator.host.execute('sudo modprobe rpcrdma'))
            Connection.err2exc(self.host.execute('sudo modprobe rpcrdma ; sudo bash -c "echo rdma 20049 > /proc/fs/nfsd/portlist"'))
            self.is_using_rdma = True

        super(NfsClient, self).attach(volumes, wait_till_completed=True, *args, **kwargs)
        for v in volumes:
            if not NfsClient.CLIENT2BINDING.get(self, {}).get(v):
                with NfsClient.NFS_LOCK:
                    if not NfsClient.CLIENT2BINDING.get(self, {}).get(v):
                        NfsClient.CLIENT2BINDING.setdefault(self, {}).update({v: FS([self], v.name, self.fs_type, self.mount_point(v))})

            self.exportfs(self.initiator, NfsClient.CLIENT2BINDING[self][v])

            # Why is FS using a Client and not a host???
            Filesystem.mkdr([self.initiator.name], self.mount_point(v))
            Filesystem.mount([self.initiator.name],
                             f'{random.choice([p.ip for n in self.node.nics for p in n.ports])}:{self.mount_point(v)}',
                             self.mount_point(v), 0, ['rdma,port=20049'] if protocol == self.RDMA else None)

        return NfsClient.CLIENT2BINDING[self]

    def do_unbind(self, volumes: Iterable[Volume], *args, **kwargs) -> Iterable[Volume]:
        to_detach = []
        for v in volumes:
            fs = NfsClient.CLIENT2BINDING.get(self, {}).pop(v, None)
            if fs:
                to_detach.append(v)
                self.exportfs(self.initiator, fs, undo=True)
                fs.umount()

        super(NfsClient, self).detach(to_detach, wait_till_completed=True, *args, **kwargs)
        return to_detach
