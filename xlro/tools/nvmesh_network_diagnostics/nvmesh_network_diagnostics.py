#!/usr/bin/env python

# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import print_function
from __future__ import absolute_import
from builtins import str
from builtins import range
from builtins import object
import sys
import argparse
import time
from typing import List, Union
from . import nvmesh_network_diagnostics_tests as net_diag_tests

from xlro.core.entities import Node
from xlro.core.util.ssh import RemotePopen, LocalPopen
from xlro.core.util.cli_util import add_common_args, handle_common_args
from argparse import RawTextHelpFormatter
from .nvmesh_network_diagnostics_tests import NetDiagTest


PING = "ping -s {} -c 1 -D -I {} {}"
RDMA = 'ib_send_bw -R --report_gbits --output=bandwidth -F -D {} -d {} -p {}'
EDGE_ERR = '--XX--'
EDGE_OK = '----->'
VERSION='v1.0'
DOC='https://github.com/NVIDIA/nvmesh-documentation'


def process_from_port(port, *args, **kwargs):
    return port.host.connection.popen(*args, **kwargs)


class NetDiagMap(object):
    def __init__(self, args):
        self.manager = args.manager
        self.cnames = args.clients or [c.name for c in self.manager.clients]
        self.tnames = args.targets or [t.name for t in self.manager.targets]
        self.cnodes = [Node.instance(name=c) for c in self.cnames]
        self.tnodes = [Node.instance(name=t) for t in self.tnames]
        self.switches_map = {}
        self.args = args

    def get_nodes_set(self):
        return set(self.cnodes + self.tnodes)

    def get_all_ports_list(self):
        nodes = self.get_nodes_set()
        return [NetDiagConnection(self, p) for n in nodes for nic in n.nics for p in nic.ports]


class NetDiagConnection(object):
    def __init__(self, net_diag_map, port):
        self.map = net_diag_map
        self.port = port
        self.switch = port.switchport.switch.name
        self.connections = []

    def __str__(self):
        return "{}".format(self.port)

    def check_tcp_connectivity(self, rport):
        '''
        checks connectivity to remote port

        :param rport: Remote port of type NetDiagConnection
        '''
        process = process_from_port(self.port,
                                         cmd=(PING.format(self.map.args.mtu, self.port.ip, rport.port.ip)),
                                         inbuf=None, get_pty=True)
        net_diag_tests.log("{} ==> {}".format(self.port.host.name,
                                              (PING.format(self.map.args.mtu, self.port.ip, rport.port.ip))))
        res = process.communicate()[0]
        for r in iter(res.splitlines()):
            net_diag_tests.log("\t\t <== {}".format(r))
        self.print_result(rport, EDGE_OK if "1 received" in res else EDGE_ERR)

    def check_p2p_rdma_connectivity(self, client_port, print_results):
        '''
        check point to point RDMA connectivity

        :param client_port: Client port of type NetDiagConnection
        :param print_results: True if print results
        :return: switches_map
        '''
        res = None
        server_process = process_from_port(self.port,
                                           cmd=(RDMA.format(self.map.args.duration, self.port.name, self.map.args.port)),
                                           inbuf=None, get_pty=True)
        net_diag_tests.log("{} ==> {}".format(self.port.host.name,
                                              (RDMA.format(self.map.args.duration, self.port.name, self.map.args.port))))

        if server_process.poll() is None:
            client_process = process_from_port(client_port.port,
                                               cmd=(RDMA.format(self.map.args.duration, client_port.port.name,
                                                                self.map.args.port) + ' ' + self.port.ip),
                                               inbuf=None, get_pty=True)
            net_diag_tests.log("{} ==> {}".format(client_port.port.host.name,
                                                  (RDMA.format(self.map.args.duration, client_port.port.name,
                                                               self.map.args.port) + ' ' + self.port.ip)))

            net_diag_tests.log("Sleeping {} seconds".format(self.map.args.duration + self.map.args.timeout))
            time.sleep(self.map.args.duration + self.map.args.timeout)

            if client_process.poll() == 0 and server_process.poll() == 0:
                res = client_process.communicate()[0]

        if res:
            net_diag_tests.log("\t\t <== {}".format(res.rstrip('\n')))
            if self.switch not in self.map.switches_map:
                self.map.switches_map[self.switch] = []
            if self not in self.map.switches_map[self.switch]:
                self.map.switches_map[self.switch].append(self)
            if client_port.port.host.name != self.port.host.name:
                self.connections.append(client_port)
            if print_results:
                client_port.print_result(self, (res[:6] + ' Gb/s'))

        else:
            if print_results:
                net_diag_tests.log("\t\t <== ERROR")
                client_port.print_result(self, EDGE_ERR)

    def check_congestion_on_port(self):
        '''
        check_congestion_on_port will test multiple RDMA session at the same time on given port
        '''
        server_processes: List[Union[LocalPopen, RemotePopen]] = []
        client_processes: List[Union[LocalPopen, RemotePopen]] = []
        max_num_of_clients = 0
        current_net_diag_con = None

        for net_diag_con in self.map.switches_map[self.switch]:
            if net_diag_con.port == self.port:
                current_net_diag_con = net_diag_con
                max_num_of_clients = len(net_diag_con.connections)
        assert current_net_diag_con, 'No connection.'
        num_of_session = min(self.map.args.number_of_simultaneous_clients, max_num_of_clients)
        net_diag_tests.log("number_of_sessions: {}".format(num_of_session))

        self.open_rdma_servers(num_of_session, server_processes)
        self.open_rdma_clients(num_of_session, current_net_diag_con.connections, client_processes)
        self.communicate_and_collect_rdma_results(num_of_session, current_net_diag_con, client_processes)

    def print_result(self, rport, res):
        '''
        print_results will be used to display the results of the test

        :param rport: Remote port of type NetDiagConnection
        :param res: relationship / result
        '''
        _rport = rport.port
        print("{0:50}\t{1:10}\t{2:10}\t{3:8}\t\t{4:10}\t{5:10}\t{6:50}".format(
            self.port.host.name, self.port.name, (self.port.ip or 'NULL '),
            res, (_rport.ip or 'NULL '), _rport.name, _rport.host.name))

    def print_result_partial(self, res):
        '''
        print_results_partial will be used to display the results of the test with client side only

        :param res: relationship / result
        '''
        print("{0:50}\t{1:10}\t{2:10}\t{3:8}\t\t{4:10}\t{5:10}\t{6:50}".format(
            self.port.host.name, self.port.name, (self.port.ip or 'NULL '), res, ' ', ' ', ' '))

    def print_result_congestion_average(self, total_res, clients):
        '''
        print_results_partial will be used to display the results of the test with client side only

        :param res: relationship / result
        '''
        print("{0:50}\t{1:10}\t{2:10}\t{3:8}\n".format("TOTAL:", (str(clients) + ' connections'), ' ',
                                                       (str(round(total_res, 2)) + ' Gb/s')))

    def open_rdma_servers(self, number_of_session, server_processes):
        '''
        open_rdma_servers will start number_of_session number of RDMA server on
        the same server NetDiagConnections (self) list

        :param number_of_session: number of simultaneous RDMA session
        :param server_processes: list of server processes to use
        '''
        net_diag_tests.open_rdma_servers(self.map, number_of_session, ([self] * number_of_session), server_processes)

    def open_rdma_clients(self, number_of_session, connections, client_processes):
        '''
        open_rdma_clients will start number_of_session number of RDMA clients on the connections list
        to the server_ports

        :param number_of_session: number of simultaneous RDMA session
        :param connections : list of client ports to use
        :param client_processes: list of client processes
        '''
        net_diag_tests.open_rdma_clients(self.map, number_of_session, connections, client_processes,
                                                           ([self] * number_of_session))

    def communicate_and_collect_rdma_results(self, number_of_session, current_net_diag_con, client_processes):
        '''
        communicate_and_collect_rdma_results will communicate the pre-started client processes and will collect
        results of the RDMA tests. It will additionaly print to STDOUT the number of sessions and
        the overall throughput.

        :param number_of_session: number of simultaneous RDMA session
        :param current_net_diag_con: current NetDiagConnection object
        :param client_processes: list of client processes
        '''
        total_res = 0.0
        clients = 0

        for idx in range(number_of_session):
            res = client_processes[idx].communicate()[0]
            total_res += float(res)
            clients += 1
            if idx == 0:
                current_net_diag_con.connections[idx].print_result(self, (res[:6] + ' Gb/s'))
            else:
                current_net_diag_con.connections[idx].print_result_partial(res[:6] + ' Gb/s')
            net_diag_tests.log("\t\t <== {}".format(res.rstrip('\n')))
        self.print_result_congestion_average(total_res, clients)


def main():
    parser = argparse.ArgumentParser(formatter_class=RawTextHelpFormatter, description="""
    NVMesh Network Diagnostics version:  {}
    Logs: {}
    Documentation: {}""".format(VERSION, net_diag_tests.LOG_PATH, DOC))
    add_common_args(parser)

    parser.add_argument('-t', '--targets', nargs='+', help='Target - overriding/ignoring Management')
    parser.add_argument('-c', '--clients', nargs='+', help='Client - overriding/ignoring Management')
    parser.add_argument('-T', '--test', type=int, nargs='+', default=[0, 1, 2, 3],
                        help='''List of tests to perform:
            0 - TCP/IP Connectivity Test
            1 - RoCE Connectivity Test
            2 - RoCE Congestion Test
            3 - RoCE Inter-Switch Congestion Test
    Default: 0,1,2,3''')
    parser.add_argument('-m', '--mtu', action='store', default=4096, type=int,
                        help="MTU to be used in the tcp_ip test - Default: 4096 seconds")
    parser.add_argument('-d', '--duration', action='store', default=10, type=int,
                        help="Duration in seconds of the RDMA tests - Default: 5 seconds")
    parser.add_argument('-nc', '--number_of_simultaneous_clients', action='store', default=2, type=int,
                        help="Desired number of Simultaneous RDMA clients during Congestion tests - Default: 2")
    parser.add_argument('-p', '--port', action='store', default=18515, type=int,
                        help="Base logical port to be used for Congestion tests - Default: 18515")
    parser.add_argument('-o', '--output', help="Redirect STDOUT to desired file")
    parser.add_argument('-v', '--verbose', action='store_true',
                        help="Display more information before & after the tests")
    parser.add_argument('-sw', '--switches_map', action='store_true', help="Print the switches_map | require --verbose")
    parser.add_argument('-tout', '--timeout', action='store', type=int, default=1,
                        help="time in second to wait for additional time after RDMA test - can be helpful on loaded "
                             "setups")

    args = parser.parse_args()
    handle_common_args(args)

    if args.switches_map and (args.verbose is None or args.verbose is False):
        parser.error("-sw/--switches_map require -v/--verbose")

    def invalid(msg):
        print('ERROR:', msg, file=sys.stderr)
        parser.print_help(sys.stderr)
        sys.exit(2)

    if not args.manager and (not args.targets or not args.clients):
        invalid('Either manager (-M) or Targets and Clients (-t and -c) are required.')

    if args.output and args.output != '-':
        sys.stdout = open(args.output, 'w')

    net_diag_map = NetDiagMap(args)
    net_diag_test = NetDiagTest(args, net_diag_map)

    net_diag_test.start()


if __name__ == '__main__':
    main()
