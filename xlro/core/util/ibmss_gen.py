#!/usr/bin/env python

# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

'''
    ibmss_gen.py - Generate scripts for NVmesh volume creation and IBMSS configuration for an IBMSS FileSystem

    The script will accept a VPG and desired FS size, and calculate the needed NVMesh volumes needed
    to support balanced spread and failover across a set of NSD servers.
    It generates two outputs:
        - An nvmesh cli script for actually creating the volumes using the selected VPG
        - A set of NSD-Drive stanzas configuring the devices with balanced primary, secondary, etc. servers

    For example:
        python ibmss_gen.py --cli nvmesh.cli --ibm nsd.conf myVPG 5T NSD-{1..10}
    This will create 90 volumes (so primary - 10 servers, and failover - 9 servers) can stay evenly balanced,
    where total size will be >= 5T
    To create the volumes the user will need to run the created nvmesh.cli file via nvmesh CLI
        /usr/bin/nvmesh < nvmesh.cli

    See --help for more options.
'''
try:
    from typing import *
except:
    pass
import random
import re
import sys
import argparse
import math

# An Allocation is a tuple of ordered list of servers and # of resources for that server list
class Allocation(object):
    def __init__(self, servers, r_count):
        self.servers = servers
        self.r_count = r_count

    def __repr__(self):
        return '{} @ {}'.format(self.r_count, self.servers)


def allocate(n_servers: int, n_resources: int, res_per_server: int) -> List[Allocation]:
    allocations = [Allocation([], n_resources)]
    server_set = set(range(n_servers))
    for nth_server in range(res_per_server):
        # Redistribute Allocations amongst Nth server
        new_allocations = []
        for alloc in allocations:
            r_count = alloc.r_count
            available = server_set - set(alloc.servers)
            r_per = r_count // len(available)
            if r_per:
                n_allocs = [Allocation(alloc.servers + [s], r_per) for s in available]
                for a_extra in random.sample(n_allocs, r_count % len(n_allocs)):
                    a_extra.r_count += 1
            else:
                n_allocs = [Allocation(alloc.servers + [s], 1) for s in random.sample(available, r_count)]
            new_allocations.extend(n_allocs)
        allocations = new_allocations
    return allocations

def toBytes(s):
    units = 'bKMGTP'
    try:
        match = re.match('(?P<n>\d+)(?P<u>[{}]?)(?P<i>I?)B?$'.format(units[1:]), s.upper())
        assert match, 'Units could not be found for {}'.format(s)
        mdict = match.groupdict()
        multi = (1024 if mdict['i'] else 1000) ** (units.index(mdict.get('u', 'b')))
        return int(mdict['n']) * multi
    except Exception as e:
        raise argparse.ArgumentTypeError('Invalid size spec: {}'.format(s))

def main():

    parser = argparse.ArgumentParser()
    parser.add_argument('-s', '--servers-per-volume', type=int, default=3,
            help="How many servers to assign to each volume (default 3)")
    parser.add_argument('-p', '--prefix', default='nsd-',
            help="Volume name prefix. (default is 'nds-')")
    parser.add_argument('-n', '--nvolumes', type=int, default=-1,
            help="How many volumes to create (default = nservers * nservers-1)")
    parser.add_argument('--stanza', type=argparse.FileType('r'),
            help="Custom stanza template file. (Replaces {device}, {name} and {servers})")
    parser.add_argument('--cli', type=argparse.FileType('w'),
            default=sys.stdout,
            help="Output file for NVMesh CLI")
    parser.add_argument('--ibm', type=argparse.FileType('w'),
            default=sys.stdout,
            help="Output file for IBMSS config")
    parser.add_argument('vpg', #type=EntityArg(VPG),
            help='VPG name')
    parser.add_argument('FS_size', type=toBytes, help='Total FS size')
    parser.add_argument('servers', nargs='*', default=['S{}'.format(i) for i in range(10)],
            help='NSD Server list')

    args = parser.parse_args()

    if args.nvolumes <= 0:
        args.nvolumes = len(args.servers) * (len(args.servers) - 1)
    
    if args.stanza:
        stanza = args.stanza.read().decode()
    else:
        stanza = '%nsd:\n\tdevice={device}\n\tnds={name}\n\tservers={servers}\n\tusage=dataAndMetadata\n'


    vcapacity = int(math.ceil(args.FS_size * 1.0 / args.nvolumes))

    allocations = allocate(len(args.servers), args.nvolumes, args.servers_per_volume)
    seq = 0
    for a in allocations:
        pservers = '+'.join([str(n) for n in a.servers])
        cservers = ','.join([args.servers[n] for n in a.servers])
        for i in range(a.r_count):
            vname = '{}{}-{}'.format(args.prefix, pservers, i)
            args.cli.write('volume create --vpg {} -c {} -n {}\n'.format(args.vpg, vcapacity, vname))
            args.ibm.write(stanza.format(device='/dev/nvmesh/' + vname, name=vname, servers=cservers, seq=seq))
            seq += 1


if __name__ == '__main__':
    main()
