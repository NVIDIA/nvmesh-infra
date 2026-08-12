#!/usr/bin/env python

# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import print_function
from __future__ import absolute_import
from builtins import str
from builtins import range
from builtins import object
import itertools
from typing import List, Union, TYPE_CHECKING
if TYPE_CHECKING:
    from .nvmesh_network_diagnostics import NetDiagConnection
    from xlro.core.util.ssh import RemotePopen, LocalPopen

from datetime import datetime

LOG_PATH = "/tmp/nvmesh_network_diagnostics.log"
LOG_FILE = None


class NetDiagTest(object):
    def __init__(self, args, net_diag_map):
        self.net_diag_map = net_diag_map
        self.test = args.test
        self.duration = args.duration
        self.port = args.port
        self.number_of_simultaneous_clients = args.number_of_simultaneous_clients
        self.mtu = args.mtu
        self.verbose = args.verbose
        self.start_time = datetime.now()

    def start(self):
        if self.verbose:
            self.start_time = self.print_start_test_information()

        self.start_log()

        if 0 in self.test:
            self.tcp_ip_test()
        if 1 in self.test:
            self.roce_connectivity_test()
        if 2 in self.test:
            self.roce_congestion_test()
        if 3 in self.test:
            self.roce_inter_sw_congestion_test()

        self.end()

    def end(self):
        global LOG_FILE
        if self.verbose:
            self.print_end_test_information()
        if LOG_FILE:
            LOG_FILE.close()

    def print_start_test_information(self):
        '''
        print_start_test_information display the first header of information at the beginning of the test
        '''
        net_diag_map = self.net_diag_map
        start_time = datetime.now()
        information = """======================================================
    NVMesh Network Diagnostics
======================================================
Start date: {0}

Manager:    {1}
Targets:    {2}
Clients:    {3}

Tests:                          {4}
RDMA test durations:            {5}
Base port:                      {6}
Number of simultaneous clients: {7}
Total number of ports:          {8}
Timeout:                        {9} seconds
======================================================""".format(
            str(start_time),
            net_diag_map.manager.host,
            net_diag_map.tnames,
            net_diag_map.cnames,
            self.test,
            self.duration,
            self.port,
            self.number_of_simultaneous_clients,
            len(net_diag_map.get_all_ports_list()),
            self.net_diag_map.args.timeout)
        print(information)
        return start_time

    def print_end_test_information(self):
        '''
        print_end_test_information display the first header of information at the beginning of the test
        '''
        end_time = datetime.now()
        information = """
======================================================
End date:       {0}
Total time:     {1}

{2}
======================================================""".format(
            str(end_time),
            str(end_time - self.start_time),
            self.get_printable_switches_map()
            )
        print(information)

    def start_log(self):
        '''
        start_log is printing the test arguments to the logfile
        '''
        from .nvmesh_network_diagnostics import VERSION
        log("Start nvmesh_network_diagnostics version {}".format(VERSION))
        log("List of parameters to be used:")
        for arg in vars(self.net_diag_map.args):
            log("\t{}: {}".format(arg, getattr(self.net_diag_map.args, arg)))

    def get_printable_switches_map(self):
        '''
        get_printable_switches_map prepare a string to display the switches_map

        :return: representative string of switches_map
        '''
        if self.net_diag_map.args.switches_map and (1 or 2 or 3) in self.net_diag_map.args.test:
            str_to_return = 'Switches Map:\n'
            for name in self.net_diag_map.switches_map:
                str_to_return += "{} \n".format(name)
                for con in self.net_diag_map.switches_map[name]:
                    str_to_return += "\t{} \n".format(con)
            return str_to_return
        else:
            return ' '

    def tcp_ip_test(self):
        '''
        tcp_ip_test is sending a single ping with (MTU 4096 if transport is RDMA) between each pair of nics
        and in both directions
        '''
        print("\n\nStarting TCP/IP Connectivity Test:\n")
        log("Starting TCP/IP Connectivity Test")

        ports = self.net_diag_map.get_all_ports_list()

        for lport in ports:
            for rport in ports:
                lport.check_tcp_connectivity(rport)
            print('')

    def roce_connectivity_test(self):
        '''
        roce_connectivity_test is testing RDMA connection between each pair of ports and in both direction
        '''
        print("\n\nStarting RoCE Connectivity Test:\n")
        log("Starting RoCE Connectivity Test")

        self.get_switches_map(print_results=True)

    def roce_congestion_test(self):
        '''
        roce_congestion_test is testing network congestion between each pair of ports that are located
        on the same switch and in both directions
        '''
        print("\n\nStarting RoCE Congestion Test:\n")
        log("Starting RoCE Congestion Test")

        ports = self.net_diag_map.get_all_ports_list()

        if not self.net_diag_map.switches_map:
            self.get_switches_map(print_results=False)

        for server_port in ports:
            server_port.check_congestion_on_port()

    def roce_inter_sw_congestion_test(self):
        '''
        roce_inter_sw_congestion_test is testing the network congestion between each pair of switches
        '''
        if not self.net_diag_map.switches_map:
            self.get_switches_map(print_results=False)

        number_of_switches = len(self.net_diag_map.switches_map)

        if number_of_switches < 2:
            print("Not enough switches ({}) for inter-switch congestion test - skipping ... ".format(number_of_switches))
            return

        print("\n\nStarting RoCE Inter-Switches Test:\n")
        log("Starting RoCE Inter-Switches Test")

        sw_map = self.net_diag_map.switches_map

        # looping over every combinations of inter-switch links - ex: if 3 switches then (0,1), (0,2), (1,2)
        for inter_switches_connection in itertools.combinations(sw_map, 2):

            server_ports: List[NetDiagConnection] = []
            client_ports: List[NetDiagConnection] = []
            server_processes: List[Union[RemotePopen, LocalPopen]] = []
            client_processes: List[Union[RemotePopen, LocalPopen]] = []

            print("\n{} <--> {}\n".format(inter_switches_connection[0], inter_switches_connection[1]))
            log("{} <--> {}".format(inter_switches_connection[0], inter_switches_connection[1]))

            number_of_session = min(self.net_diag_map.args.number_of_simultaneous_clients,
                                    len(sw_map[inter_switches_connection[0]]),
                                    len(sw_map[inter_switches_connection[1]]))

            number_of_session = prepare_server_and_client_ports(sw_map, inter_switches_connection[0],
                                                                inter_switches_connection[1],
                                                                client_ports, server_ports, number_of_session)

            if number_of_session == 0:
                print("No valid connections found between {} and {}".format(inter_switches_connection[0],
                                                                            inter_switches_connection[1]))
                return

            log("number_of_sessions: {}".format(number_of_session))

            self.open_rdma_servers(number_of_session, server_ports, server_processes)
            self.open_rdma_clients(number_of_session, client_ports, client_processes, server_ports)
            self.communicate_and_collect_rdma_results(number_of_session, client_processes, client_ports, server_ports)

    def get_switches_map(self, print_results):
        '''
        get_switches_map will test every point to point rdma connection and print the relationships.
        it will additionally populate the switches_map

        :param print_results: True if print relationships
        '''
        ports = self.net_diag_map.get_all_ports_list()

        for server_port in ports:
            for client_port in ports:
                server_port.check_p2p_rdma_connectivity(client_port, print_results)
            if print_results:
                print('')

        for l in iter(self.get_printable_switches_map().splitlines()):
            log(l)

    def open_rdma_servers(self, number_of_session, server_ports, server_processes):
        '''
        open_rdma_servers will start number_of_session number of RDMA server on the server_ports list

        :param number_of_session: number of simultaneous RDMA session
        :param server_ports: list of server ports to use
        :param server_processes: list of server processes to use
        '''
        open_rdma_servers(self.net_diag_map, number_of_session, server_ports, server_processes)

    def open_rdma_clients(self, number_of_session, client_ports, client_processes, server_ports):
        '''
        open_rdma_clients will start number_of_session number of RDMA clients on the client_ports list
        to the server_ports

        :param number_of_session: number of simultaneous RDMA session
        :param client_ports: list of client ports to use
        :param client_processes: list of client processes
        :param server_ports: list of server ports
        '''
        open_rdma_clients(self.net_diag_map, number_of_session, client_ports, client_processes, server_ports)

    def communicate_and_collect_rdma_results(self, number_of_session, client_processes, client_ports, server_ports):
        '''
        communicate_and_collect_rdma_results will communicate the pre-started client processes and will collect
        results of the RDMA tests. It will additionaly print to STDOUT the number of sessions and
        the overall throughput.

        :param number_of_session: number of simultaneous RDMA session
        :param client_processes: list of client processes
        :param client_ports: list of client ports
        :param server_ports: list of server ports
        '''
        total_res = 0.0
        clients = 0

        for idx in range(number_of_session):
            res = client_processes[idx].communicate()[0]
            total_res += float(res)
            clients += 1
            client_ports[idx].print_result(server_ports[idx], (res[:6] + ' Gb/s'))
            log("\t\t <== {}".format(res.rstrip('\n')))

        client_ports[0].print_result_congestion_average(total_res, clients)


def prepare_server_and_client_ports(sw_map, sw_A, sw_B, client_ports, server_ports, number_of_session):
    '''
    prepare_server_and_client_ports creating the connections to be tested - server and clients on connected
    to the 2 different switches and only 1 clinet/server per port at the same time

    :param sw_map: switches_map
    :param sw_A: 1st switch to use
    :param sw_B: 2nd switch to use
    :param client_ports: list of client ports
    :param server_ports: list of server ports
    :param number_of_session: number of simultaneous RDMA session
    :return: number_of_session
    '''
    for server_port in sw_map[sw_A]:
        if len(server_ports) < number_of_session:
            if server_port not in server_ports and server_port not in client_ports:
                server_ports.append(server_port)
                for client_port in server_port.connections:
                    if client_port in sw_map[sw_B]:
                        if client_port not in server_ports and client_port not in client_ports:
                            client_ports.append(client_port)
                            break

    if len(client_ports) < len(server_ports):
        number_of_session = len(client_ports)

    return number_of_session


def open_rdma_servers(map, number_of_session, server_ports, server_processes):
    '''
    open_rdma_servers will start number_of_session number of RDMA server on the server_ports list

    :param map: the NetDiagMap object of the test
    :param number_of_session: number of simultaneous RDMA session
    :param server_ports: list of server ports to use
    :param server_processes: list of server processes to use
    '''
    from .nvmesh_network_diagnostics import RDMA, process_from_port

    for idx, logical_port in enumerate(range(map.args.port, map.args.port + number_of_session)):
        server_processes.append(process_from_port(server_ports[idx].port,
                                                  cmd=(RDMA.format(map.args.duration, server_ports[idx].port.name,
                                                                   logical_port)),
                                                  inbuf=None, get_pty=True))
        log("{} ==> {}".format(server_ports[idx].port.host.name,
                               (RDMA.format(map.args.duration, server_ports[idx].port.name, logical_port))))
        if server_processes[idx].poll() is not None:
            server_processes[idx].kill()


def open_rdma_clients(map, number_of_session, client_ports, client_processes, server_ports):
    '''
    open_rdma_clients will start number_of_session number of RDMA clients on the client_ports list
    to the server_ports

    :param map: the NetDiagMap object of the test
    :param number_of_session: number of simultaneous RDMA session
    :param client_ports: list of client ports to use
    :param client_processes: list of client processes
    :param server_ports: list of server ports
    '''
    from .nvmesh_network_diagnostics import RDMA, process_from_port

    for idx, logical_port in enumerate(range(map.args.port, map.args.port + number_of_session)):
        client_processes.append(process_from_port(client_ports[idx].port,
                                                  cmd=(RDMA.format(map.args.duration, client_ports[idx].port.name,
                                                                   logical_port)
                                                       + ' ' + server_ports[idx].port.ip), inbuf=None, get_pty=True))
        log("{} ==> {}".format(client_ports[idx].port.host.name,
                               (RDMA.format(map.args.duration, client_ports[idx].port.name, logical_port)
                                + ' ' + server_ports[idx].port.ip)))
        if client_processes[idx].poll() is not None:
            client_processes[idx].kill()


def log(str):
    '''
    log is printing the given string into the logfile

    :param str: string to print
    '''
    global LOG_FILE
    if not LOG_FILE:
        LOG_FILE = open(LOG_PATH, 'w')
    LOG_FILE.write("{} - {}\n".format(datetime.now(), str))
