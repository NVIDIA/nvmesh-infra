# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import sys
import random
import time
from collections import defaultdict
from datetime import datetime
from functools import partial

from typing import Any, Optional, Type, Union


from xlro.core.util.operations import Operation, SimpleMultiOperation
from xlro.core.util.entities_utils import get_toma_leader_service
from xlro.core.entities import Drive, DriveStatus, Target, Service, Manager, Client, SourceTypes, Volume, Host, Node, \
    Attachment, ClientNode, ExternalClient
from xlro.core.util.general_utils import wait_for_property_values, wait_for_it, get_attached_volumes_specified, \
    WaitResult
from xlro.core.entities import Service
from xlro.core.util import thread_manager
from xlro.core.util.ssh import Connection
from xlro.core.util.service_utils import get_service_dependencies
from xlro.qa.utils.host_utils import HostUtils


def get_operation(operation_name: str) -> Type[Operation]:
    operation = getattr(sys.modules[__name__], operation_name)
    if not issubclass(operation, Operation):
        raise ValueError("searched - {}, found - {} is not of type - {}".format(operation_name, operation, Operation))
    return operation


class NullOperation(Operation):
    def __init__(self, *args, **kwargs):
        super(NullOperation, self).__init__()

    def _do(self):
        assert True

    def _verify_do(self):
        assert True

    def _undo(self):
        assert True

    def _verify_undo(self):
        assert True


class DetachAttachAll(Operation):

    def __init__(self, ent, is_snapshot, io_enabled_timeout, healthy_timeout, attachments_dict, mounts_dict, clients, stop_io_op, **kwargs):
        super(DetachAttachAll, self).__init__()
        self.is_snapshot = is_snapshot
        self.io_enabled_timeout = io_enabled_timeout
        self.healthy_timeout = healthy_timeout
        self.attachments_dict = attachments_dict
        self.mounts_dict = mounts_dict
        self.clients = clients
        self.stop_client_io = stop_io_op
        self.client_for_disaster = ent

    def _do(self):
        from xlro.qa.utils.host_utils import HostUtils as host_utils

        if self.stop_client_io:
            self.stop_client_io.do()
        for client, vols in self.attachments_dict.items():
            if vols:
                assert self.mounts_dict.get(client, None) is not None, f'DetachAttachAll try to umount from unknown client={client}'
                host_utils.unmount(client.name, self.mounts_dict[client])
                # client.detach(vols, force=self.force_detach, wait_till_completed=True)
                client.detach(vols, wait_till_completed=True)

    def _verify_do(self):
        start_t = datetime.now()
        wait_for_property_values(self.clients, 'health', [Client.HEALTH.HEALTHY],
                                 SourceTypes.MANAGEMENT, timeout=self.healthy_timeout).assert_result(
            f"some clients: did not reach status {Client.HEALTH.HEALTHY} after detach (management)")
        self.logger.debug(f'Total time for clients to reach HEALTHY after detach: {(datetime.now() - start_t).total_seconds()} seconds')

        if not Client.bulk_wait_for_attachments_status(self.attachments_dict, True, ['Detached'],
                                                       poll=2,
                                                       timeout=180):
            raise Exception('verify_do: volumes not detached within timeout.')

    def _undo(self):
        from xlro.qa.utils.host_utils import HostUtils as host_utils
        for client, vols in self.attachments_dict.items():
            client.is_setup = False
            client.need_setup = True
            client.bindings = {}
            if vols:
                client.attach(vols, wait_till_completed=True)
                assert self.mounts_dict.get(client, None) is not None, f'DetachAttachAll try to remount from unknown client={client.name}'
                host_utils.remount(self.mounts_dict[client], client)

    def _verify_undo(self):
        start_t = datetime.now()
        assert Client.bulk_wait_for_attachments_io_enabled(clients_volumes=self.attachments_dict,
                                                           timeout=self.io_enabled_timeout), \
                f'Timed out attaching {self.attachments_dict}.'
        self.logger.debug(f'Total time to attach: {(datetime.now() - start_t).total_seconds()} seconds')
        start_t = datetime.now()
        wait_for_property_values(self.clients, 'health', [Client.HEALTH.HEALTHY],
                                     SourceTypes.MANAGEMENT, timeout=self.healthy_timeout).assert_result(
                                    f"some clients: did not reach status {Client.HEALTH.HEALTHY} after attach (management)")
        self.logger.debug(f'Total time for clients to reach HEALTHY after attach: {(datetime.now() - start_t).total_seconds()} seconds')

        if self.stop_client_io:
            self.stop_client_io.undo()


class EvictDrives(Operation):

    def __init__(self, drives, evicting_timeout=10, initializing_timeout=60, formatting_timeout=10):
        super(EvictDrives, self).__init__()
        self.drives = drives
        self.evicting_timeout = evicting_timeout
        self.initializing_timeout = initializing_timeout
        self.formatting_timeout = formatting_timeout

    def _do(self):
        evict_results = Drive.evict_drives(self.drives)
        self.logger.debug('evict drives - {} results - {}'.format(self.drives, evict_results))
        failed_evictions = [res for res in evict_results if not res['success']]
        assert not failed_evictions, 'The following drives failed to evict: {}'.format(failed_evictions)

    def _verify_do(self):
        wait_for_property_values(self.drives, 'evicted', [True], timeout=self.evicting_timeout).\
            assert_result('failed to wait for evicted')

    def _undo(self):
        format_results = Drive.format_drives(self.drives)
        self.logger.debug('format drives - {} results - {}'.format(self.drives, format_results))
        failed_formats = [res for res in format_results if not res['success']]
        assert not failed_formats, 'The following drives failed to format: {}'.format(failed_formats)

    def _verify_undo(self):
        wait_for_property_values(self.drives, 'evicted', [False], timeout=self.formatting_timeout).\
                   assert_result() and\
               wait_for_property_values(self.drives, 'status', [DriveStatus.OK], timeout=self.initializing_timeout).\
                   assert_result()


class EvictDrive(EvictDrives):

    def __init__(self, drive, *args, **kwargs):
        super(EvictDrive, self).__init__([drive], *args, **kwargs)


class BounceHostPort(Operation):
    def __init__(self, port, *cmds):
        super(BounceHostPort, self).__init__()
        self.port = port
        self.cmds = cmds  # command names to be used for dis/connect operations. example: 'ip', ifconfig'

    def _do(self):
        assert self.port.disconnect(*self.cmds) is not None, "disconnection {} failed".format(self.port)

    def _verify_do(self):
        self.port.wait_for_disconnect().assert_result("failed wait for disconnect")

    def _undo(self):
        assert self.port.connect(*self.cmds) is not None, "connection {} failed".format(self.port)

    def _verify_undo(self):
        self.port.wait_for_connect().assert_result("failed wait for connect")


class BounceSwitchPortByHostPort(BounceHostPort):
    def _do(self):
        assert self.port.switchport.disconnect() is not None, "disconnection {} failed".format(self.port.switchport)

    def _verify_do(self):
        self.port.switchport.wait_for_disconnect().assert_result("failed wait for disconnect")

    def _undo(self):
        assert self.port.switchport.connect() is not None, "connection {} failed".format(self.port.switchport)

    def _verify_undo(self):
        self.port.switchport.wait_for_connect().assert_result("failed wait for connect")


class BounceHostRandomSwitchPort(BounceSwitchPortByHostPort):
    def __init__(self, target: Target, *args: Any, **kwargs: Any) -> None:
        rnd_port = random.choice([port for nic in target.node.nics for port in nic.ports])
        super(BounceHostRandomSwitchPort, self).__init__(port=rnd_port)


class BounceService(Operation):
    def __init__(self, service_for_start, stop_client_io= None, skip_stop_io=True, down_timeout=30, up_timeout=5, io_enabled_timeout=60, service_for_stop=None):
        super(BounceService, self).__init__()
        self.service_for_start = service_for_start
        self.service_for_stop = service_for_stop if service_for_stop else service_for_start
        self.down_timeout = down_timeout
        self.up_timeout = up_timeout
        self.skip_stop_io = skip_stop_io

        # new state variables
        self.stop_client_io = stop_client_io
        self.io_enabled_timeout = io_enabled_timeout
        client = Client.instance(name=service_for_start.host.name)

        #self.client_volumes_dic = get_attached_volumes_specified([client], [Volume.ONLINE, Volume.DEGRADED])

    # do we need this function ?
    # is it needed for kill ???
    def pre_um_do(self):
        if self.stop_client_io and not self.skip_stop_io:
            self.stop_client_io.do()

    def post_um_verify_undo(self):
        # this function is called unconditionally from different disasters.
        # here is the single place for skipping when needed.
        if self.stop_client_io and not self.skip_stop_io:
            self.stop_client_io.undo()

    def _do(self):
        self.pre_um_do()
        self.logger.info(f"stopping service - {self.service_for_stop}, 'hosts' = [{self.service_for_stop.host}]")
        code = self.service_for_stop.stop()
        assert code == 0, f"Failed to stop {self.service_for_stop}, code: {code}"

    def _verify_do(self):
        self.logger.debug("waiting service - {} get down in {} sec".format(self.service_for_start, self.down_timeout))
        wait_for_it(lambda: self.service_for_stop.status() == Service.STATUS.DOWN, timeout=self.down_timeout).assert_result()
        if self.service_for_stop.name == "nvmeshclient":
            client_host = Client.instance(name=self.service_for_stop.host.name).host
            wait_for_it(lambda: not HostUtils.lsmod(client_host, 'nvmeibc', return_code=1)).assert_result(
                f"nvmeshclient service is stopped but nvmeibc is still loaded")
            wait_for_it(lambda: not HostUtils.lsmod(client_host, 'nvmeibs', return_code=1)).assert_result(
                f"nvmeshtarget service is stopped but nvmeibs is still loaded")
            wait_for_it(lambda: not HostUtils.lsmod(client_host, 'nvmeiba', return_code=1)).assert_result(
                f"nvmeshatom service is stopped but nvmeiba is still loaded")

    def _undo(self):
        self.logger.debug("starting service - {}".format(self.service_for_start))
        code = self.service_for_start.start()
        assert code == 0, "Failed to start {}, code: {}".format(self.service_for_start, code)

    def _verify_undo(self):
        self.logger.debug("waiting service - {} to get up in {} sec".format(self.service_for_start, self.up_timeout))
        wait_for_it(lambda: self.service_for_start.status() == Service.STATUS.UP, timeout=self.up_timeout).assert_result()
        self.post_um_verify_undo()


class KillService(BounceService):
    def __init__(self, *args, kill_signal=9, down_timeout=120, **kwargs):
        super(KillService, self).__init__(*args, **kwargs)
        self.kill_signal = kill_signal
        self.original_pid = self.service_for_stop.get_pid()
        self.down_timeout = down_timeout
        assert self.service_for_start == self.service_for_stop, "kill service must use the same service for stop and start"

    def _do(self):
        self.logger.debug("killing service - {}, signal - {}".format(self.service_for_stop, self.kill_signal))
        res = self.service_for_stop.kill(self.kill_signal)
        assert res == 0, 'killing service - {} returned exit code {}, expected 0'.format(self.service_for_stop, res)

    def _verify_do(self):
        self.logger.debug("waiting service process - {} to die in {} sec".format(self.service_for_stop, self.down_timeout))
        wait_for_it(lambda: self.service_for_stop.host.connection.execute("ps {}".format(self.original_pid))[2] == 1,
                    timeout=self.down_timeout).assert_result()
        super(KillService, self)._verify_do()  # Verify service is down, takes a while to stop after killing main PID

    def _verify_undo(self):
        super(KillService, self)._verify_undo()
        new_pid = self.service_for_start.get_pid()
        assert new_pid != self.original_pid, 'Service {} has the same pid {} as before killing it'.format(self.service_for_start,
                                                                                                          new_pid)


class KillUtils:

    def special_init(self, client, stop_io_op, attach_disconnect, is_snapshot):
        self.client = client
        self.skip_stop_io = False
        # do not change self.kill_signal it affect the um service, not StopIo
        # stopIO is using signal 15 and retry with signal 9 if needed
        self.stop_client_io = stop_io_op
        self.attach_disconnect = attach_disconnect
        self.is_snapshot = is_snapshot
        self.attached_vols = {a.volume for a in self.client.get_property('attachments', source=SourceTypes.MANAGEMENT,
                                                                         no_cache=True).values() if not a.is_hidden}
        self.attached_vol_names = {v.name for v in self.attached_vols}

        if self.is_snapshot:  # snapshot on dpu
            self.stop_client_io = None  # do not stop traffic
            self.attach_disconnect = None

    def before_do_disaster(self):
        if self.stop_client_io:
            self.stop_client_io.do()


class BounceUMClientCMService(BounceService, KillUtils):
    def __init__(self, client, *args, stop_io_op=None, is_snapshot=False, attach_disconnect=None, **kwargs):
        service_name = 'nvmeshcm'
        self.dependencies = [client.host.services[name] for name in get_service_dependencies(client.name, service_name)]
        service = client.client_node.services[service_name]
        super(BounceUMClientCMService, self).__init__(service, *args, **kwargs)
        self.is_snapshot = is_snapshot
        self.logger.info(f'dependencies list for restore={self.dependencies}')
        self.special_init(client, stop_io_op, attach_disconnect, is_snapshot)

    def _do(self):
        self.before_do_disaster()
        super(BounceUMClientCMService, self)._do()

    def _verify_do(self):
        super(BounceUMClientCMService, self)._verify_do()
        for service in self.dependencies:
            self.logger.debug(f"waiting for depended service - {service.name} go down down in {self.down_timeout} sec")
            wait_for_it(lambda: service.status() == Service.STATUS.DOWN, timeout=self.down_timeout).assert_result()

    def _undo(self):
        # start CM than start each depended service and wait for UP before starting next service
        super(BounceUMClientCMService, self)._undo()
        for service in self.dependencies:
            self.logger.info(f'starting depended service {service.name}')
            code = service.start()
            assert code == 0, "Failed to start {service}, code: {code}"
            wait_for_it(lambda: service.status() == Service.STATUS.UP,
                        timeout=self.up_timeout).assert_result(f'service {service} did not reach STATUS.UP')

class BounceUMClientService(BounceService, KillUtils):
    def __init__(self, client, *args, stop_io_op=None, is_snapshot=False, attach_disconnect=None, up_timeout=60, **kwargs):
        service = client.client_node.services['nvmeshum']
        client.need_setup = True
        super(BounceUMClientService, self).__init__(service, *args, up_timeout=up_timeout, **kwargs)
        self.special_init(client, stop_io_op, attach_disconnect, is_snapshot)

class BounceUMClientKCService(BounceService, KillUtils):
    def __init__(self, client_ref: Union[str, Client], *args, stop_io_op=None, is_snapshot=False, attach_disconnect=None, **kwargs):
        self.client = Client.instance(name=client_ref) if isinstance(client_ref, str) else client_ref
        service_for_stop = self.client.client_node.services['client']
        service_for_start = self.client.host.services.get('target', service_for_stop)
        self.umService = self.client.host.services['nvmeshum']
        self.cm = self.client.host.services['nvmeshcm']
        super(BounceUMClientKCService, self).__init__(service_for_start, *args, service_for_stop=service_for_stop, **kwargs)
        self.special_init(client_ref, stop_io_op, attach_disconnect, is_snapshot)

    def _undo(self):
        super(BounceUMClientKCService, self)._undo()
        code = 0 if self.service_for_start is None else self.service_for_start.start()
        assert code == 0, "Failed to start {}, code: {}".format(self.service_for_start, code)
        code = self.umService.start()
        assert code == 0, "Failed to start {}, code: {}".format(self.umService, code)

    def _verify_undo(self):
        attached_snapshots = []
        super(BounceUMClientKCService, self)._verify_undo()
        if self.service_for_start:
            wait_for_it(lambda: self.service_for_start.status() == Service.STATUS.UP, timeout=self.up_timeout).assert_result()
        wait_for_it(lambda: self.cm.status() == Service.STATUS.UP, timeout=self.up_timeout).assert_result()
        wait_for_it(lambda: self.umService.status() == Service.STATUS.UP, timeout=self.up_timeout).assert_result()


class BounceClientService(BounceService):
    def __init__(self, client_ref: Union[str, Client], *args,  target_undo: bool = False, stop_io_op=None, is_snapshot=False, attach_disconnect=None, snapshot_ready_timeout=300, **kwargs):
        self.client = Client.instance(name=client_ref) if isinstance(client_ref, str) else client_ref

        target_service = self.client.host.services.get('target', None)
        client_service = self.client.host.services['nvmeshum'] if self.client.isUmClient else self.client.host.services['client']

        service_for_stop = client_service
        service_for_start = target_service if (target_service is not None
                                               and self.client.host.services['target'].status() != Service.STATUS.DOWN)\
            else client_service

        assert not is_snapshot, "No support for snapshot in BounceClientService"
        self.client_volumes_dic = get_attached_volumes_specified([client_ref], [Volume.ONLINE, Volume.DEGRADED])
        super(BounceClientService, self).__init__(service_for_start, stop_io_op, *args, skip_stop_io=False, service_for_stop=service_for_stop, **kwargs)

    def _verify_undo(self):
        self.logger.debug("waiting service - {} to get up in {} sec".format(self.service_for_start, self.up_timeout))
        wait_for_it(lambda: self.service_for_start.status() == Service.STATUS.UP, timeout=self.up_timeout).assert_result()
        assert self.client.wait_for_attach(self.client_volumes_dic[self.client], poll=3, timeout=30), 'Timeout reached while waiting for attachments'
        assert Client.bulk_wait_for_attachments_io_enabled(self.client_volumes_dic, timeout=60),\
            'Timeout reached while waiting for to be io enabled'
        self.post_um_verify_undo()

class BounceTargetService(BounceService):
    def __init__(self, target, *args, **kwargs):
        self.target = target
        super(BounceTargetService, self).__init__(target.services['target'], *args, **kwargs)


class BounceToma(BounceService):
    def __init__(self, target, *args, **kwargs):
        self.target = target
        super(BounceToma, self).__init__(target.services['toma'], *args, **kwargs)


class KillToma(KillService):
    def __init__(self, target, *args, **kwargs):
        super(KillToma, self).__init__(target.services['toma'], *args, **kwargs)


class KillTomaLeader(KillToma):
    """similar to KillService but self.service is initialized to None,
     and only determined once _do is called"""

    def __init__(self, target: Target, *args: Any, **kwargs: Any) -> None:
        # TODO: This Operation should really not inherit KillToma
        super(KillTomaLeader, self).__init__(target, *args, **kwargs)
        self.target = target

    def _do(self):
        self.service_for_stop = get_toma_leader_service(self.target)
        # Once leader is determined, set service_for_start to be same - so we start what was stopped, not new leader
        self.service_for_start = self.service_for_stop
        assert self.service_for_stop, "Could not find toma leader"
        self.original_pid = self.service_for_stop.get_pid()
        super(KillTomaLeader, self)._do()


class BounceDrives(Operation):
    MIN_DELAY = 0.1
    NO_DELAY = 0

    def __init__(self, drives, min_rand_time=0, max_rand_time=0, down_timeout=60, up_timeout=60):
        super(BounceDrives, self).__init__()
        self.drives = drives
        self.min_delay = min_rand_time
        self.max_delay = max_rand_time or min_rand_time
        self.down_timeout = down_timeout
        self.up_timeout = up_timeout

    def get_targets_drives(self):
        targetDrives = defaultdict(list)
        for drv in self.drives:
            targetDrives[drv.target].append(drv)
        return targetDrives

    def _do(self):
        for drive in self.drives:
            self.logger.debug("remove drive {0} from {1}".format(drive.name, drive.target.name))
            drive.remove_drives([drive])
            time.sleep(random.uniform(self.min_delay, self.max_delay))
        assert True

    def _verify_do(self):
        def no_pci_slot(d):
            try:
                d.get_property('pci_slot', SourceTypes.PROC, no_cache=True)
            except:
                return True
            return False

        for drive in self.drives:
            wait_for_it(lambda: no_pci_slot(drive)).assert_result()

        drives = []
        for target, _drives in self.get_targets_drives().items():
            if target.get_property('health', no_cache=True) == Target.HEALTH.HEALTHY:
                drives += _drives

        if drives:
            wait_for_property_values(objects=self.drives, prop_name='status', values=[DriveStatus.MISSING],
                                     timeout=self.down_timeout).assert_result()

    def _undo(self):
        for drive in self.drives:
            self.logger.debug("return drive {} to {}".format(drive.name, drive.target.name))
            drive.return_drives([drive])
        assert True

    def _verify_undo(self):
        for drive in self.drives:
            # Get PCI address before trying to get slot, since sometimes the address is empty after bounce
            wait_for_it(lambda: drive.get_property('pci_address', SourceTypes.PROC, no_cache=True), poll=5, timeout=120)
            self.logger.debug(f"pci_slot is: {drive.pci_slot}, pci_address is: {drive.pci_address}")
            # Now try to get the slot and assert if the drive is still disabled
            wait_for_it(lambda: drive.get_property('pci_slot', SourceTypes.PROC, no_cache=True), poll=5, timeout=120) \
                .assert_result()

        drives = []
        for target, _drives in self.get_targets_drives().items():
            if target.get_property('health', no_cache=True) == Target.HEALTH.HEALTHY:
                drives += _drives

        if drives:
            wait_for_property_values(objects=drives, prop_name='status', values=[DriveStatus.OK],
                                     timeout=self.up_timeout).assert_result()


class RemoveDrive(BounceDrives):
    def __init__(self, drive, *args, **kwargs):
        super(RemoveDrive, self).__init__([drive], *args, **kwargs)

class BounceWCVDrive(BounceDrives):
    def __init__(self, manager, *args, **kwargs):
        # Randomly select a drive from the drives used by the WCV/MDV volumes
        wcv_volumes = [v for v in manager.volumes if v.vtype== 'WCV']
        assert len(wcv_volumes) == 1, f"Invalid number of WCV/MDV volumes ({len(wcv_volumes)}) found while trying to bounce a WCV drive"
        from xlro.core.util.volume_utils import get_volume_data_drives
        drive_to_bounce = random.choice(get_volume_data_drives(wcv_volumes[0]))
        super(BounceWCVDrive, self).__init__([drive_to_bounce], *args, **kwargs)


class ResetNVMe(Operation):
    def __init__(self, drive):
        super(ResetNVMe, self).__init__()
        self.drive = drive

    def _do(self):
        assert Drive.reset_nvme([self.drive]), 'Reset nvme for drive {} failed'.format(self.drive)

    def _verify_do(self):
        wait_for_it(lambda: self.drive.status == DriveStatus.OK).assert_result()

    def _undo(self):
        pass

    def _verify_undo(self):
        pass


class RemoveEvictDrives(Operation):
    def __init__(self, drives, min_rand_time=0, max_rand_time=0, down_timeout=10, up_timeout=30, evicting_timeout=10, initializing_timeout=60):
        # TODO - find more elegant way.. (inspect ? )
        super(RemoveEvictDrives, self).__init__()
        self.drives = drives
        self.remove_oper = BounceDrives(drives, min_rand_time, max_rand_time, down_timeout, up_timeout)
        self.evict_oper = EvictDrives(drives, evicting_timeout, initializing_timeout)

    def _do(self):
        self.remove_oper._do()
        self.remove_oper._verify_do()
        self.evict_oper._do()

    def _verify_do(self):
        self.evict_oper._verify_do()

    def _undo(self):
        self.remove_oper._undo()
        self.remove_oper._verify_undo()
        # Wait for drive status to return to 'Ok' after remove_oper._undo(), otherwise evict_oper._undo() fails
        wait_for_property_values(self.drives, 'status', [DriveStatus.OK], timeout=30)\
            .assert_result(prefix="Some drives didn't return to OK status after 30 seconds")
        self.evict_oper._undo()

    def _verify_undo(self):
        self.evict_oper._verify_undo()


class RemoveEvictDrive(RemoveEvictDrives):
    def __init__(self, drive, *args, **kwargs):
        super(RemoveEvictDrive, self).__init__([drive], *args, **kwargs)


class BounceTargetDrives(BounceDrives):
    def __init__(self, target_node, *args, **kwargs):
        super(BounceTargetDrives, self).__init__(target_node.drives, *args, **kwargs)


class RebootHost(Operation):
    BOUNCE_DELAY = 30

    def __init__(self, host, force_level=0, disconnect_timeout=60, connect_timeout=600, stop_io_op=None,
                 start_target_after_reboot=False, verify_attachments_after_reboot=True, service_start_timeout=60,
                 io_enabled_timeout=120, attached_timeout=60, verify_client_status=True, snapshot_ready_timeout=300,
                 do_initiator_disasters=False, is_snapshot=False, **kwargs):
        from xlro.qa.utils.host_utils import HostUtils as host_utils
        from xlro.qa.utils.host_utils import DpuUtils as dpu_utils
        self.host_utils = host_utils
        self.dpu_utils = dpu_utils

        super(RebootHost, self).__init__()
        self.host = host
        self.client = Client.instance(name=host.name)
        self.attached_vol_names = set(v for v, a in self.client.get_property('attachments', source=SourceTypes.MANAGEMENT, no_cache=True).items() if not a.is_hidden)
        self.force_level = force_level
        self.disconnect_timeout = disconnect_timeout
        self.connect_timeout = connect_timeout
        self.stop_io_op = stop_io_op
        self.start_target_after_reboot = start_target_after_reboot
        self.verify_attachments_after_reboot = verify_attachments_after_reboot
        self.service_start_timeout = service_start_timeout
        self.io_enabled_timeout = io_enabled_timeout
        self.attached_timeout = 120 if is_snapshot else attached_timeout
        self.verify_client_status = verify_client_status
        self.snapshot_ready_timeout = snapshot_ready_timeout
        self.do_initiator_disasters = do_initiator_disasters
        self.is_snapshot = is_snapshot
        self.client_node = ClientNode.instance(name=self.client.name)
        self.client_status_timeout = 600 if is_snapshot else 60  # TBD read from scenario yaml
        self.is_dpu = self.client_node.is_dpu
        self.ipmi_host_name = kwargs.get('ipmi_host_name', None)
        self.last_boot = self.host_utils.get_last_boot(self.host)

    def _do(self):
        if self.stop_io_op:
            self.stop_io_op.do()
        host = self.host
        if self.do_initiator_disasters and self.is_snapshot and self.client_node.is_dpu:
            dpu_host_name = self.dpu_utils.get_host_from_dpu(self.client)
            host = Host.instance(name=dpu_host_name)
        host.reboot(force_level=self.force_level, wait=False)

    def _verify_do(self):
        host = self.host
        if self.do_initiator_disasters and self.is_snapshot and self.client_node.is_dpu:
            dpu_host_name = self.dpu_utils.get_host_from_dpu(self.client)
            host = Host.instance(name=dpu_host_name)
        if not wait_for_it(host._disconnected, timeout=self.disconnect_timeout, quiet=True):
            self.logger.debug('Failed to verify that {} is disconnected in verify_do stage.'
                              'will double check in verify_undo stage'.format(self.host.name))

    def _undo(self):
        pass

    def _verify_undo(self):
        if self.ipmi_host_name:
            assert wait_for_it(lambda: Connection(self.ipmi_host_name, timeout=20, fail_is_error=False), poll=10,
                               timeout=self.connect_timeout), \
                f'Reboot undo failed. Cannot establish connection to host {self.ipmi_host_name} for {self.client.name}'
        attached_snapshots = []
        host_name = self.dpu_utils.get_host_from_dpu(self.client) if self.do_initiator_disasters and self.is_snapshot \
                                                             and self.client_node.is_dpu else self.host.name
        assert wait_for_it(lambda: Connection(host_name, timeout=20, fail_is_error=False), poll=10,
                           timeout=self.connect_timeout), \
                                   'Reboot undo failed. Cannot establish connection to: {}'.format(self.host.name)
        current_boot = self.host_utils.get_last_boot(self.host)
        assert self.last_boot != current_boot, \
            f"Reboot host on {self.host.name} Failed: last boot: {self.last_boot}; current_boot: {current_boot}"
        self.logger.debug(f"last_boot: {self.last_boot}  current_boot: {current_boot}")
        if self.verify_client_status:
            if self.host.services['client'] == Service.STATUS.DOWN:  # skip for for pure UM (no client)
                assert wait_for_it(lambda: self.host.services['client'].status() == Service.STATUS.UP, timeout=self.client_status_timeout), \
                                           'Reboot undo failed. Client not restarted on {}'.format(self.host.name)

            # JW: Shouldn't this test if self.client is umclient?
            if self.host.services['nvmeshum'] == Service.STATUS.DOWN:
                self.host.services['nvmeshum'].start()
                assert wait_for_it(lambda: self.host.services["nvmeshum"].status() == Service.STATUS.UP,
                                   timeout=30), f"nvmeshum service on {self.host.name} is not up after 30 sec"

        if self.start_target_after_reboot:
            self.host.services['target'].start()
            assert wait_for_it(lambda: self.host.services['target'].status() == Service.STATUS.UP, timeout=self.service_start_timeout), \
                   'Reboot undo failed. Target did not restart on {}'.format(self.host.name)

        if self.verify_attachments_after_reboot:
            def _all_attached():
                new_attachments = self.client.get_property('attachments', source=SourceTypes.MANAGEMENT, no_cache=True)
                return set(v for v, a in new_attachments.items() if not a.is_hidden) == self.attached_vol_names
            assert wait_for_it(_all_attached, poll=3, timeout=self.attached_timeout), f'Timeout reached while waiting for attachments for {self.attached_timeout}'
            client_volumes_dic = get_attached_volumes_specified([self.client],[Volume.ONLINE, Volume.DEGRADED])
            wait_vols = Client.bulk_wait_for_attachments_io_enabled(client_volumes_dic, timeout=self.io_enabled_timeout)
            if not wait_vols:
                if self.stop_io_op:
                    # IO will be resumed - MUST ensure volumes are io-enabled first
                    raise AssertionError(
                        f"Cannot resume IO: Volumes {wait_vols} stayed io-disabled after "
                        f"{self.io_enabled_timeout} sec from reboot on {self.host.name}. Aborting to prevent IO panic."
                    )
                else:
                    # No IO to resume - safe to continue with warning
                    self.logger.warning(
                        f"Volumes {wait_vols} stayed io-disabled after {self.io_enabled_timeout} sec from reboot. "
                        f"If it stays that way after all disasters are undone, test will fail."
                    )
                    return
            for vname, attachment in self.client.attachments.items():
                if Volume.instance(name=vname).is_snapshot:
                    attached_snapshots.append(attachment)
            if attached_snapshots:
                wait_for_property_values(attached_snapshots, 'is_snapshot_ready', [True], SourceTypes.MANAGEMENT,
                                         timeout=self.snapshot_ready_timeout) \
                    .assert_result(f"Some Snapshots still not ready after {self.snapshot_ready_timeout} sec")
                wait_for_property_values([self.client], 'health', [self.client.HEALTH.HEALTHY], SourceTypes.MANAGEMENT, timeout=120).assert_result(
                    f"client: {self.client.name} status is not {self.client.HEALTH.HEALTHY} but {self.client.health}")

        if self.stop_io_op:
            self.stop_io_op.undo()


class RebootHostIpmi(RebootHost):
    def __init__(self, host, disconnect_timeout=220, connect_timeout=220, **kwargs):
        super(RebootHostIpmi, self).__init__(host, disconnect_timeout=disconnect_timeout,
                                             connect_timeout=connect_timeout, **kwargs)
        self.ipmi_cmd = kwargs.get('ipmi_cmd', 'reset')

    def _do(self):
        if self.stop_io_op:
            self.stop_io_op.do()
        self.host.ipmi(cmd=self.ipmi_cmd, wait=False)


class HardRebootHostIpmi(RebootHostIpmi):
    def __init__(self, host, disconnect_timeout=300, connect_timeout=600, stop_io_op=None, **kwargs):
        super().__init__(host, disconnect_timeout=disconnect_timeout, connect_timeout=connect_timeout, stop_io_op=stop_io_op, **kwargs)

    def _do(self):
        self.logger.info(f"Powering off {self.host.name}")
        if self.stop_io_op:
            self.stop_io_op.do()
        self.host.ipmi(cmd='off', wait=False)

    def _undo(self):
        self.logger.info(f"Powering on {self.host.name}")
        self.host.ipmi(cmd='on', wait=False)

class RebootFSHost(RebootHost):

    def __init__(self, host, disconnect_timeout=60, connect_timeout=900, start_target_after_reboot=True,
                 verify_attachments_after_reboot=True, vols_per_client=None, io_enabled_timeout=300,
                 attached_timeout=300, service_start_timeout=60, stop_io_op=None):
        self.vols_per_client = vols_per_client
        assert self.vols_per_client, "please specify vols_per_client parameter"
        super(RebootFSHost, self).__init__(host=host, disconnect_timeout=disconnect_timeout,
                                           connect_timeout=connect_timeout,
                                           start_target_after_reboot=start_target_after_reboot,
                                           verify_attachments_after_reboot=verify_attachments_after_reboot,
                                           stop_io_op=None, service_start_timeout=service_start_timeout,
                                           io_enabled_timeout=io_enabled_timeout,
                                           attached_timeout=attached_timeout)
        self.logger.info(f"RebootFSHost: connect_timeout={connect_timeout}")
        self.fs_stop_io_op = stop_io_op

    def _do(self):
        if self.fs_stop_io_op:
            self.logger.notice(f"Stopping IO on {self.host.name}")
            self.fs_stop_io_op.do()
        self.logger.notice(f"Unmounting mountpoints on {self.host.name}")
        self.host_utils.unmount(self.host.name, self.vols_per_client[self.host.name])
        self.logger.notice(f"Rebooting {self.host.name}")
        self.host.reboot(force_level=self.force_level, wait=False)

    def _verify_undo(self):
        super(RebootFSHost, self)._verify_undo()
        client = Client.instance(name=self.host.name)
        is_attached = self.client.wait_for_attach(self.vols_per_client[client.name])
        if is_attached:
            is_io_enabled = Client.bulk_wait_for_attachments_io_enabled(
                {client: self.vols_per_client[client.name]},
                timeout=self.io_enabled_timeout)
            assert is_io_enabled, f"some md volumes on {self.client.name} are io disabled"
        self.logger.notice(f"trying to remount fs on {self.host.name} after reboot")
        self.host_utils.remount(self.vols_per_client[client.name], client)
        if self.fs_stop_io_op:
            self.fs_stop_io_op.undo()


class RebootTomaTarget(RebootHost):
    def __init__(self, toma_target, **kwargs):
        super(RebootTomaTarget, self).__init__(toma_target.host, **kwargs)
        self.target = toma_target


class RebootTomaTargetIpmi(RebootHostIpmi):
        def __init__(self, toma_target, **kwargs):
            super(RebootTomaTargetIpmi, self).__init__(toma_target.host, **kwargs)
            self.target = toma_target


class RebootClient(RebootHost):
    def __init__(self, client, start_target_after_reboot=False, snapshot_ready_timeout=300,
                 do_initiator_disasters=False, is_snapshot=False, **kwargs):
        self.start_target_after_reboot = start_target_after_reboot if 'target' in client.host.services else False
        # for dpu mode this disaster will be used both for dpu reboot and host reboot
        # if running client disaster   --> dpu
        # host if initiator disaster --> host
        host = client.host if not is_snapshot else Host.instance(name=client.name)
        super(RebootClient, self).__init__(host=host, start_target_after_reboot=start_target_after_reboot,
                                           snapshot_ready_timeout=snapshot_ready_timeout,
                                           do_initiator_disasters=do_initiator_disasters, is_snapshot=is_snapshot, **kwargs)
        self.client = client


class RebootClientIpmi(RebootHostIpmi):
    def __init__(self, client, ipmi_cmd='reset', **kwargs):
        super(RebootClientIpmi, self).__init__(client.host, connect_timeout=600, ipmi_cmd=ipmi_cmd, **kwargs)
        self.client = client


class KillClient(KillService):
    def __init__(self, client, target_undo=False, *args, **kwargs):
        super(KillClient, self).__init__(service_for_start=client.client_node.services['client'], *args, **kwargs)

    def _undo(self):
        self.logger.debug("starting service - {}".format(self.service_for_start))
        assert self.service_for_start.start() == 0, 'failed to start client service for {}'.format(self.service_for_start)


class KillServiceRefactored(Operation):
    def __init__(self, host, service_name, *args, kill_signal=9, down_timeout=120, up_timeout=60, **kwargs):
        # fist parameter is host (not client) so target/toma can be used too in the future
        # do not change self.kill_signal it affect the um service, not StopIo
        # stopIO is using signal 15 and retry with signal 9 if needed
        super(KillServiceRefactored, self).__init__()
        self.dependencies = [host.services[name] for name in get_service_dependencies(host.name, service_name)]
        self.service_name = service_name
        self.service_to_kill = host.services[service_name]
        self.host = host
        self.kill_signal = kill_signal
        self.original_pid = self.service_to_kill.get_pid()
        self.down_timeout = down_timeout
        self.up_timeout = up_timeout

    def _do(self):
        self.logger.debug(f"killing service: {self.service_to_kill}, by signal: {self.kill_signal}")
        res = self.service_to_kill.kill(self.kill_signal)
        assert res == 0, f'killing service: <{self.service_to_kill}> returned exit code {res}, expected 0'

    def _verify_do(self):
        self.logger.debug(f"waiting service process - {self.service_to_kill} to die in {self.down_timeout} sec")
        wait_for_it(lambda: self.service_to_kill.host.connection.execute("ps {}".format(self.original_pid))[2] == 1,
                    timeout=self.down_timeout).assert_result()
        for service in self.dependencies:
            self.logger.debug(f"waiting for depended service - {service.name} go down down in {self.down_timeout} sec")
            wait_for_it(lambda: service.status() == Service.STATUS.DOWN, timeout=self.down_timeout).assert_result()
        self.logger.debug("all depended services went down")

    def _undo(self):
        self.logger.debug(f"start is harmless even if {self.service_name} is autostart")
        code = self.service_to_kill.start()
        assert code == 0, f"Failed to start {self.service_to_kill}, code: {code}"

    def _verify_undo(self):
        self.logger.debug(f"going to wait for {self.service_name} to go up (either by autostart or start())")
        wait_for_it(lambda: self.service_to_kill.status() == Service.STATUS.UP,
                    timeout=self.up_timeout).assert_result(f'service {self.service_to_kill} did not reach STATUS.UP')

        new_pid = self.service_to_kill.get_pid()
        assert new_pid != self.original_pid, (f'Service {self.service_to_kill} '
                                              f'has the same pid {new_pid} as before killing it')
        for service in self.dependencies:
            self.logger.info(f'starting depended service {service.name}')
            code = service.start()
            assert code == 0, f"Failed to start {service}, code: {code}"
            wait_for_it(lambda: service.status() == Service.STATUS.UP,
                        timeout=self.up_timeout).assert_result(f'service {service} did not reach STATUS.UP')


class UMUnbind(Operation):
    def __init__(self, client, stop_client_io, is_snapshot):
        super(UMUnbind, self).__init__()
        self.stop_client_io = stop_client_io
        self.is_snapshot = is_snapshot
        self.attached_vol_names = None
        self.client = client

    def _do(self):
        self.attached_vols = {a.volume for a in
                              self.client.get_property('attachments', source=SourceTypes.MANAGEMENT,
                                                       no_cache=True).values() if not a.is_hidden}
        self.attached_vol_names = {v.name for v in self.attached_vols}

    def _verify_do(self):
        return

    def _undo(self):
        return

    def _verify_undo(self):
        return


class StopIO(Operation):
    def __init__(self, stop_client_io, is_snapshot=False):
        super(StopIO, self).__init__()
        if is_snapshot:  # snapshot on dpu
            self.stop_client_io = None  # do not stop traffic
        else:
            self.stop_client_io = stop_client_io

    def _do(self):
        if self.stop_client_io:
            self.stop_client_io.do()

    def _verify_do(self):
        return

    def _undo(self):
        return

    def _verify_undo(self):
        if self.stop_client_io:
            self.stop_client_io.undo()


class KillCM(SimpleMultiOperation):
    def __init__(self, client, *args, stop_io_op=None, up_timeout=60, is_snapshot=False, **kwargs):

        io_operation = StopIO(stop_io_op, is_snapshot=is_snapshot)
        um_operation = UMUnbind(client, stop_io_op, is_snapshot)
        kill_operation = KillServiceRefactored(Host.instance(name=client.name), "nvmeshcm", kill_signal=9, down_timeout=120, up_timeout=up_timeout)
        super(KillCM, self).__init__([io_operation, um_operation, kill_operation], is_undo_reverse=True)


class KillUM(KillService, KillUtils):
    def __init__(self, client, *args, stop_io_op=None, is_snapshot=False, attach_disconnect=None, **kwargs):
        self.is_snapshot = is_snapshot
        super(KillUM, self).__init__(service_for_start=client.client_node.services['nvmeshum'], *args, **kwargs)
        self.special_init(client, stop_io_op, attach_disconnect, is_snapshot)

    def _do(self):
        super(KillUM, self).pre_um_do()
        super(KillUM, self)._do()

    def _verify_undo(self):
        super(KillUM, self)._verify_undo()


class Detach(Operation):
    def __init__(self, client, volume, force=False):
        super(Detach, self).__init__()
        self.client = client
        self.volume = volume
        self.force = force

    def _do(self):
        self.client.detach([self.volume], force=self.force)

    def _verify_do(self):
        self.client.wait_for_detach([self.volume], poll=1, timeout=20).assert_result()

    def _undo(self):
        self.client.attach([self.volume])

    def _verify_undo(self):
        self.client.wait_for_attach([self.volume], poll=1, timeout=20).assert_result()


class ForceDetach(Detach):
    def __init__(self, client, volume):
        super(ForceDetach, self).__init__(client, volume, force=True)


class BounceAllNodePorts(SimpleMultiOperation):
    def __init__(self, node, *cmds, **kwargs):
        self.node = node
        ports = [port for nic in node.nics for port in nic.ports]
        discarded_nics = []
        key_string = 'discarded_nics'
        if key_string in kwargs:
            discarded_nics = kwargs[key_string]
        disruptors = [BounceHostPort(port, *cmds) for port in ports if port.if_name not in discarded_nics]
        assert disruptors, 'There are no valid ports that can be bounced'
        super(BounceAllNodePorts, self).__init__(disruptors)


class BounceAllNodeSwitchPorts(SimpleMultiOperation):
    def __init__(self, node: Union[Node,Target,str], *cmds):

        if isinstance(node,Node):
            self.node = node
        elif isinstance(node, Target):
            self.node = node.node
        elif isinstance(node, str):
            self.node = Node.instance(name=node)
        else:
            raise Exception(f"Expected Node, Host, str types got {type(node)}")

        disruptors = [BounceSwitchPortByHostPort(port, *cmds) for nic in self.node.get_property('nics', no_cache=True)
                      for port in nic.ports]

        super(BounceAllNodeSwitchPorts, self).__init__(disruptors)


class BounceAllClientSwitchPorts(SimpleMultiOperation):
    def __init__(self, client, *cmds):
        self.node = client.node
        disruptors = [BounceSwitchPortByHostPort(port, *cmds) for nic in client.node.get_property('nics', no_cache=True)
                      for port in nic.ports]
        super(BounceAllClientSwitchPorts, self).__init__(disruptors)


class BounceAllNVNodePorts(BounceAllNodePorts):
    def __init__(self, nvnode, *cmds, stop_io_op=None, **kwargs):
        self.nvnode = nvnode
        super(BounceAllNVNodePorts, self).__init__(nvnode.node, *cmds, **kwargs)
        self.stop_io_op = stop_io_op

    def _do(self):
        if self.stop_io_op:
            self.stop_io_op.do()
        super(BounceAllNVNodePorts, self)._do()

    def _undo(self):
        super(BounceAllNVNodePorts, self)._undo()
        if self.stop_io_op:
            self.stop_io_op.undo()


class BounceTargetsService(Operation):
    def __init__(self, mgmt, down_timeout=60, up_timeout=60):
        super(BounceTargetsService, self).__init__()
        self.mgmt = mgmt
        self.down_timeout = down_timeout
        self.up_timeout = up_timeout

    def _do(self):
        self.logger.debug("Shutdown target node gracefully through SDK")
        tmanager = thread_manager.ThreadPoolManager()
        tmanager.map(lambda s: s.stop(),
                     [trg.services['target'] for trg in self.mgmt.targets])
        tmanager.shutdown()

    def _verify_do(self):
        self.logger.debug("check all targets service are DOWN")
        wait_for_it(lambda: all(
            [target.services['target'].status() == Service.STATUS.DOWN for target in self.mgmt.targets]),
                    timeout=self.down_timeout).assert_result()

    def _undo(self):
        self.logger.debug("start targets services")
        tmanager = thread_manager.ThreadPoolManager()
        tmanager.map(lambda s: s.start(), [trg.services['target'] for trg in self.mgmt.targets])
        tmanager.shutdown()

    def _verify_undo(self):
        self.logger.debug("check all targets service are UP")
        wait_for_it(lambda: all(
            [target.services['target'].status() == Service.STATUS.UP for target in self.mgmt.targets]),
                    timeout=self.up_timeout).assert_result()

class TomaErrorInjection(Operation):
    """Execute Toma RPC injection with an option to undo"""
    INJECTOR_PREFIX = "simulate"

    def __init__(self, target: Target, cmd: str, apply_param: str, stop_param: Optional[str] = None, injector_pref=INJECTOR_PREFIX) -> None:
        super(TomaErrorInjection, self).__init__()
        self.target = target
        self.cmd = cmd
        self.apply_param = apply_param
        self.stop_param = stop_param
        self.injector_pref = injector_pref

    def _do(self):
        self.target.toma_rpc_cmd("{} {} {}".format(self.injector_pref, self.cmd, self.apply_param))

    def _verify_do(self):
        pass

    def _undo(self):
        if self.stop_param:
            self.target.toma_rpc_cmd("{} {} {}".format(self.injector_pref, self.cmd, self.stop_param))

    def _verify_undo(self):
        pass


# like a partial but also let us subclass
def partial_inj(*args, **kwargs):
    class PartialMethod(partial):
        """taken from https://gist.github.com/carymrobbins/8940382"""
        def __get__(self, instance, owner):
            if instance is None:
                return self
            return partial(self.func, instance, # type: ignore
                           *(self.args or ()), **(self.keywords or {}))

    class TomaErrorInjectionSub(TomaErrorInjection):
        __init__ = PartialMethod(TomaErrorInjection.__init__, *args, **kwargs)
    return TomaErrorInjectionSub


TomaInjFollowerSer = partial_inj(cmd="follower-ser", apply_param="pause", stop_param="resume")
TomaInjRaftPauseIn = partial_inj(cmd="raft", apply_param="pause-in", stop_param="resume")
TomaInjRaftPauseOut = partial_inj(cmd="raft", apply_param="pause-out", stop_param="resume")
TomaInjRaftPauseInOut = partial_inj(cmd="raft", apply_param="pause-in-out", stop_param="resume")
TomaInjTopoDiscard = partial_inj(cmd="topo-discard", stop_param="0 permanent")
TomaInjEndlessRebuild = partial_inj(cmd="rebuild", apply_param="endless", stop_param="normal")
TomaInjFailSmartCnt = partial_inj(cmd="smart-cnt", apply_param="fail", stop_param="normal")
TomaInjFailZeroingSegment = partial_inj(cmd="zeroing", apply_param="fail", stop_param="normal")
TomaInjClientDisconnectTopoReg = partial_inj(cmd="client-disconnect", apply_param="brute", stop_param="normal")
TomaInjStaleRebuild = partial_inj(cmd="stale-rebuild", apply_param="disable", stop_param="enable")
TomaInjMDWrite = partial_inj(cmd="md-write", apply_param="pause", stop_param="resume")
TomaInjRaftLongMsg = partial_inj(cmd="raft-long-msg", stop_param="0")
TomaInjDirtyRebuild = partial_inj(cmd="max_n_simultaneous_dirty_rebuild", apply_param="0", stop_param="2", injector_pref="config")
