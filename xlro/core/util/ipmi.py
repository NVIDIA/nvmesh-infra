#!/usr/bin/env python

# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

##
# NOTE: Ported from infraClient/common/IpmiUtils.py
##
import re
import time
from abc import ABC, abstractmethod
import socket
import logging

from xlro.core.util.general_utils import wait_for_it

logger = logging.getLogger('ipmi')
from xlro.core import infra_conf
from xlro.core.util.ssh import local_execute, Connection

IP_PATTERN = re.compile(r'(\d{1,3}\.){3}\d{1,3}$')

# TODO: Holdover from SystemUtils.  Should be refactored
class SubProcessResponse(object):
    def __init__(self, stdout, stderr, returnCode):
        self.stdout = stdout
        self.stderr = stderr
        self.returnCode = returnCode

    def __str__(self):
        return 'return code: {} err: {} out: {}'.format(self.returnCode, self.stderr, self.stdout)

def getIpmiByNode(nodename):
    match = re.match(r'^bf\d+n(\d+)', nodename)
    if match:
        logger.debug(f"Got bluefield {nodename} as argument, will powercycle the host instead")
        nodename = "nvme" + match.group(1)
    node = nodename if IP_PATTERN.match(nodename) else socket.gethostbyname(nodename)
    logger.debug(f'getIpmiByNode({nodename} -> {node})')

    # Handle clouds
    cloud = None
    try:
        cloud = infra_conf.root.cluster.cloud.platform
    except:
        pass
    if cloud:
        if cloud == 'oci':
            logger.debug(f'{node} > OCI({nodename})')
            return OCICloudIpmiManager(nodename, nodename)
        else:
            raise Exception(f'Power control not implemented for cloud platform: {cloud}')

    # all 10.0.6.x, 10.0.7.x and so on, except 10.0.11.x are VMs
    vm = r'^10\.0\.([6-9]|[1-9]0|[1-9][2-9]|1[0-9][0-9]|2[0-4][0-9]|25[0-5])\.([0-9]|[1-9][0-9]|1[0-9]{2}|2[0-4][0-9]|25[0-5])$'
    nvme = r'^10\.0\.11?\.([1-9]|[1-9][0-9]|1[0-9][0-9]|2[0-4][0-9]|25[0-5])$'
    if re.search(vm, node):
        logger.debug(f'{node} matches VM')
        ipmiAddress = '10.0.1.{}'.format(node.split('.')[-2])
        internalName = socket.gethostbyaddr(node)
        if internalName[1] and internalName[1][0]:
            name = internalName[1][0]
        elif internalName[0] and '.' in internalName[0]:
            name = internalName[0].split('.')[0]
        else:
            raise Exception('not able to get internal VM name, so will not be able to send commands to it using the hypervisor.')

        logger.debug(f'{node} > VM({ipmiAddress}, {internalName})')
        return VirtualMachineIpmiManager(ipmiAddress, internalName=name)
    elif re.search(nvme, node):
        logger.debug(f'{node} matches NODE')
        if node.split('.')[2] == '11':
            # if ip is 10.0.11.x then the IPMI is on 10.0.12.x
            ipmiAddress = '10.0.12.{}'.format(node.split('.')[-1])
        else:
            # for any other machine the IPMI is on 10.0.2.x
            ipmiAddress = '10.0.2.{}'.format(node.split('.')[-1])
        logger.debug(f'{node} > Physical({ipmiAddress})')
        return PhysicalMachineIpmiManager(ipmiAddress)

    else:
        # TRY new naming convention
        try:
            logger.debug(f'{node} no match.  Try {nodename}-ilo')
            # ipmiName = socket.gethostbyaddr(node)[0].partition('.')[0] + '-ilo'
            ipmiName = nodename.partition('.')[0] + '-ilo'
            ipmiAddress = socket.gethostbyname(ipmiName)
            logger.debug(f'{node} > Physical({ipmiAddress})')
            return PhysicalMachineIpmiManager(ipmiAddress)
        except Exception as e:
            ipFormat = 'nvmeX - 10.0.1.X ; vmX-Y - 10.0.X.Y where the hypervisor is on nvmeX'
            raise Exception(f'Node {node} does not match conventions: "-ilo" or {ipFormat} ({repr(e)})')


class IpmiManager(ABC):
    def __init__(self, address, internalName=None):
        self.address = address
        self.internalName = internalName

    def executeLocal(self, cmdLst):
        # cmd = SystemUtils.runLocalCmd(' '.join(cmdLst))
        cmd = SubProcessResponse(*local_execute(' '.join(cmdLst)))
        if cmd.returnCode != 0:
            cmd.stderr = 'error running cmd \"{}\". {}'.format(' '.join(cmdLst), cmd.stderr)
        return cmd

    def executeRemote(self, cmdLst):
        # cmd = XSSH.execute(host=self.address, cmd=' '.join(cmdLst), outAsList=False)
        cmd = SubProcessResponse(*Connection.execute_on_host(self.address, cmd=' '.join(cmdLst)))
        if cmd.returnCode != 0:
            cmd.stderr = 'error running cmd \"{}\". {}'.format(' '.join(cmdLst), cmd.stderr)
        return cmd

    @abstractmethod
    def getPowerStatus(self):
        raise NotImplementedError

    @abstractmethod
    def powerOn(self):
        raise NotImplementedError

    @abstractmethod
    def powerOff(self):
        raise NotImplementedError

    @abstractmethod
    def powerCycle(self):
        raise NotImplementedError

    @abstractmethod
    def reset(self):
        raise NotImplementedError

    @abstractmethod
    def soft(self):
        raise NotImplementedError

    @abstractmethod
    def is_power_on(self):
        raise NotImplementedError

    @abstractmethod
    def is_power_off(self):
        raise NotImplementedError


    def __repr__(self):
        return f'{self.__class__.__name__}: {self.address} ({self.internalName})'


class PhysicalMachineIpmiManager(IpmiManager):
    def __init__(self, address, *args, **kwargs):
        from xlro.core.util.ssh import Connection
        super().__init__(address, *args, **kwargs)
        sshconf = Connection.ssh_config(self.address)
        self.user = sshconf.get('user', 'ADMIN')
        self.password = sshconf.get('password', 'ADMIN')

    def executePowerCmd(self, address, cmd):
        basePowerCmd = ['ipmitool', '-U', self.user, '-P', self.password, '-H', address, '-I', 'lanplus', 'chassis', 'power', cmd]
        return self.executeLocal(basePowerCmd)

    def getPowerStatus(self):
        return self.executePowerCmd(self.address, 'status')

    def powerOn(self):
        return self.executePowerCmd(self.address, 'on')

    def powerOff(self):
        return self.executePowerCmd(self.address, 'off')

    def powerCycle(self):
        return self.executePowerCmd(self.address, 'cycle')

    def _is_power(self, state):
        return f'Chassis Power is {state.lower()}' in self.getPowerStatus().stdout.strip()

    def is_power_on(self):
        return self._is_power('on')

    def is_power_off(self):
        return self._is_power('off')

    def reset(self):
        return self.executePowerCmd(self.address, 'reset')

    def soft(self):
        return self.executePowerCmd(self.address, 'soft')


class VirtualMachineIpmiManager(IpmiManager):

    def executePowerCmd(self, cmd):
        basePowerCmd = ['sudo', 'virsh', cmd]
        return self.executeRemote(basePowerCmd)

    def getPowerStatus(self):
        return self.executePowerCmd('list --all | grep {} | awk {}'.format(self.internalName, '\'{print $3}\''))

    def powerOn(self):
        return self.executePowerCmd('start {}'.format(self.internalName))

    def powerOff(self):
        return self.executePowerCmd('destroy {}'.format(self.internalName))

    def _is_power(self, state):
        return self.getPowerStatus().stdout.strip().lower() == state.lower()

    def is_power_on(self):
        return self._is_power('running')

    def is_power_off(self):
        return self._is_power('shut off')

    def powerCycle(self):

        def getCurrentTimeSeconds():
            return int(round(time.time()))

        destroy = self.executePowerCmd('destroy {}'.format(self.internalName))

        timeout = getCurrentTimeSeconds() + 60
        currentTime = getCurrentTimeSeconds()
        while self.getPowerStatus().stdout.strip() == 'running' and currentTime <= timeout:
            currentTime = getCurrentTimeSeconds()
            time.sleep(1)

        if  currentTime > timeout:
            SubProcessResponse('', 'Timeout while waiting for VM to shutdown', 1)

        start = self.executePowerCmd('start {}'.format(self.internalName))

        returnCode = start.returnCode
        error = ' + '.join([destroy.stderr, start.stderr])
        out = ' + '.join([destroy.stdout, start.stdout])

        return SubProcessResponse(out, error, returnCode)

    def reset(self):
        return self.executePowerCmd('reboot {}'.format(self.internalName))

    def soft(self):
        return self.executePowerCmd('shutdown {}'.format(self.internalName))

class OCICloudIpmiManager(IpmiManager):
    def __init__(self, *args, **kwargs):
        super(OCICloudIpmiManager, self).__init__(*args, **kwargs)
        try:
            cloud = infra_conf.root.cluster.cloud
            self.args = f'--compartment-id {cloud.compartment} --region {cloud.region}'
        except Exception as e:
            raise Exception(f'OCI not properly configured. {repr(e)}')

    def executePowerCmd(self, address, cmd):
        name = self.internalName or self.address
        result = self.executeLocal([f'oci compute instance list {self.args} --display-name {name} --query "data[0].id" | tr -d \\"'])
        if result.returnCode != 0 or not result.stdout:
            result.returnCode = 1
            result.stderr = f'Node {name} not found.'
            return result
        ocid = result.stdout.strip()
        return self.executeLocal([f'oci compute instance action --action {cmd} --instance-id {ocid}'])

    def getPowerStatus(self):
        name = self.internalName or self.address
        result = self.executeLocal([f'oci compute instance list {self.args} --display-name {name} --query "data[0].\\"lifecycle-state\\"" | tr -d \\"'])
        if result.returnCode == 0 and not result.stdout:
            result.returnCode = 1
            result.stderr = f'Node {name} not found.'
        return result

    def powerOn(self):
        return self.executePowerCmd(self.address, 'START')

    def powerOff(self):
        return self.executePowerCmd(self.address, 'STOP')

    def powerCycle(self):
        return self.executePowerCmd(self.address, 'RESET')

    def reset(self):
        return self.executePowerCmd(self.address, 'RESET')

    def soft(self):
        return self.executePowerCmd(self.address, 'SOFTRESET')

    def _is_power(self, state):
        #TODO: need to iplement once oci setup is available
        pass


if __name__ == '__main__':
    from xlro.core.util.cli_util import CLIArgumentParser
    parser = CLIArgumentParser()
    parser.add_argument('-r', '--reset', default=False, action="store_true", help='restart using ipmi power cycle')
    parser.add_argument('nodes', nargs='+')
    args = parser.parse_args()
    for node in args.nodes:
        try:
            ipmi = getIpmiByNode(node)
            print(f'{node} -> {ipmi}')
            print(f'status: {ipmi.getPowerStatus()}')
            if args.reset:
                print(f'reset: {ipmi.reset()}')
        except Exception as e:
            print(f'{node} -> {repr(e)}')
