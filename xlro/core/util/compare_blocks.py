# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import division
from builtins import str
from builtins import range
from builtins import object

from xlro.core.util.general_utils import old_div
import os
import random
import tempfile
from typing import Optional, Tuple, Union, List, Dict
import subprocess
import logging

from xlro.core.entities import Volume, Client, PRaid, Host
from xlro.core.util.ssh import Connection, temp_dir
from xlro.core.util.block_objects import BlockSet, StaticSliceRange
from xlro.core.util import thread_manager
from xlro.core import infra_conf

CMP_SCRIPT_PATH = infra_conf.root.tools.cmp_blocks
logger = logging.getLogger('cmp_blocks')

def cmp_blocks(host_name: str, 
               data_disks: int, 
               parity_disks: int, 
               data_dir: str, 
               metadata_dir: str, 
               plba: Optional[Union[PRaid.LBA,int]] = None, 
               debug_di: Union[bool, str] = False, 
               cmp_path: str = CMP_SCRIPT_PATH,
               verbose: Union[bool, str] = False, 
               reconst_bmp: Optional[str] = None) -> Tuple[str, str, int]:
    if not cmp_path:
        cmp_path = CMP_SCRIPT_PATH
    cmp_cmd = f"{cmp_path} --d_path {data_dir}/data_%d --md_path {metadata_dir}/metadata_%d " \
              f" --verbose {str(verbose).lower()} --dbg_di {str(debug_di).lower()} -d {data_disks} -p {parity_disks}"
    if reconst_bmp is not None:
        cmp_cmd += f' --reconst_bmp {reconst_bmp}'
    if plba is not None:
        cmp_cmd += f" --rlba {getattr(plba, 'addr', plba)}"
    out, err, code = Connection.execute_on_host(host_name, cmp_cmd)
    logger.debug(f'cmp_blocks response - output: {out}. err: {err}. code: {code}')
    return out, err, code


def reconstruct_blocks(reconst_bmp: str, 
                       ldir: str, 
                       host_name: str, 
                       data_disks: int, 
                       parity_disks: int, 
                       plba: Optional[Union[PRaid.LBA, int]] = None, 
                       debug_di: Union[bool, str] = False, 
                       cmp_path: Optional[str] = CMP_SCRIPT_PATH,
                       verbose: Union[bool, str] = False) -> str:
    host = Host.instance(name=host_name)
    with temp_dir(host_name, '/tmp/tmpXXXXXX-reconst-blocks') as rtmpdir:
        host.connection.put_dir(ldir, rtmpdir)
        cmp_dir = os.path.join(rtmpdir, os.path.basename(ldir))
        _bmp = int(reconst_bmp, 16)
        roles = []
        role = 0
        while _bmp:
            if not _bmp % 2:
                roles.append(role)
            _bmp = old_div(_bmp, 2)
            role += 1

        # Below is not needed since cmp_blocks v1.8
        #for role in roles:
        #    Connection.execute_on_host(host_name, '> {0}/data_{1} > {0}/metadata_{1}'.format(cmp_dir, role))
        cmp_blocks(host_name, data_disks, parity_disks, cmp_dir, cmp_dir, plba, debug_di, cmp_path, verbose, reconst_bmp)
        lpath = tempfile.mkdtemp('-reconst-blocks')
        host.connection.get_dir(lpath, cmp_dir)
        return os.path.join(lpath, os.path.basename(cmp_dir))


def rcmp_blocks(ldir: str,
                host_name: str,
                data_disks: int,
                parity_disks: int,
                plba: Optional[Union[PRaid.LBA, int]] = None,
                debug_di: Union[bool, str] = False,
                cmp_path: Optional[str] = CMP_SCRIPT_PATH,
                verbose: Union[bool, str] = False) -> str:
    host = Host.instance(name=host_name)
    with temp_dir(host_name, '/tmp/tmpXXXXXX-cmp-blocks') as rtmpdir:
        host.connection.put_dir(ldir, rtmpdir)
        cmp_dir = os.path.join(rtmpdir, os.path.basename(ldir))
        return cmp_blocks(host_name, data_disks, parity_disks, cmp_dir, cmp_dir, plba, debug_di, cmp_path, verbose)[0]

class VolumeCompare(object):
    """control class - supplies interface for different 'cmp' operations on given volume (and from given client)"""
    logger = logger.getChild('VolumeCompare')

    def __init__(self, volume: Volume, client: Client, local_dir: str, debug_di: bool = False, verbose: bool = False, cmp_path: Optional[str] = None) -> None:
        self.volume = volume
        self.client = client
        self.bs_cmp_results: Dict[str, bool] = {}
        self.local_dir = local_dir
        self._debug_di = debug_di
        self.verbose = verbose
        self.cmp_path = cmp_path

    @property
    def praids(self) -> List[PRaid]:
        """convenient property for addressing all the volume's praids"""
        return [p for c in self.volume.chunks for p in c.pRaids]

    @property
    def debug_di(self) -> bool:
        """tmp prop, suppose to determine and return the dbg_di status of the given attachment"""
        # TODO - recheck option to get the dbg_di from Attachment
        return self._debug_di

    def cmp(self, block_set_count: int = 10) -> bool:
        """preforms compare operation on all volume randomly (cmp is operation on praid, so pick 'n' random praids)"""
        praids = self.praids
        bs_per_praid = block_set_count // len(praids) + 1  # to "spread" the requested bs count over all praids
        for praid in praids:
            if not self.praid_cmp(praid, bs_per_praid):
                return False
        return True

    def praid_cmp(self, praid: PRaid, bs_count: int) -> bool:
        """preform compare blocks on <bs_count> random block_sets within given <praid>"""
        if praid.RAIDlevel in (PRaid.RaidLevels.RAID0, PRaid.RaidLevels.JBOD):
            return True

        # picking 'bs_count' random separate block-sets within given  praid
        paddr_list = random.sample(range(0, praid.pages, praid.dataDisks * BlockSet.SLICE_COUNT), bs_count)
        for paddr in paddr_list:
            plba = PRaid.LBA(paddr)
            block_set = BlockSet.plba2block_set(praid, plba)
            bs_result = self._block_set_cmp(block_set, plba)  # convenient method for 'block_set_compare'
            self.bs_cmp_results[str(block_set)] = bs_result  # storing results in instance prop after each iteration
            if not bs_result:
                return False
        return True

    def _block_set_cmp(self, block_set: StaticSliceRange, plba: PRaid.LBA) -> bool:
        """convenient method for 'block_set_compare'"""
        return block_set_compare(block_set, self.client, self.local_dir, plba, self.debug_di, verbose=self.verbose,
                                 cmp_path=self.cmp_path)


BASE_TMP_DIR = "/tmp"


def block_set_compare(block_set: StaticSliceRange, attached_client: Client, base_local_dir: str = BASE_TMP_DIR, plba: Optional[PRaid.LBA] = None, debug_di: bool = False, cmp_path: Optional[str] = None,
                      verbose: bool = False, data_folder: Optional[str] = None) -> bool:
    # TODO - when attachment.debug_di is in master - change debug_di flag to attachment.debug_di bool value
    # attachment = Attachment.instance(volume=volume, client=attached_client)

    # TODO - add mechanism to determine if to keep log or not
    # content_path = "{}_{}_slice_idx_{}_count_{}_vlba_{}_".format(attached_client.name, block_set.praid.chunk.name, block_set.slice_idx,
    #                                              block_set.slice_count, block_set.vlba.addr)

    content_path ="CB_{}_{}_{}".format(attached_client.name,
                                    block_set.praid.chunk.name,
                                    block_set.slice_idx if isinstance(block_set, BlockSet) else block_set.vlbs.addr)

    trgs = set([blk.drive.target for blk in block_set.content])
    with thread_manager.ThreadPoolManager() as executor:
        list(executor.map(lambda trg: trg.modprobe_target_kmods(), trgs))

    if not data_folder:
        l_tmpdir = tempfile.mkdtemp(prefix=content_path + '_', dir=base_local_dir)
        os.chmod(l_tmpdir, 0o755)
        block_set.copy(l_tmpdir)
    else:
        l_tmpdir = data_folder

    if block_set.praid.RAIDlevel not in (PRaid.RaidLevels.JBOD, PRaid.RaidLevels.RAID0):
        return _block_set_cmp_ec(block_set, attached_client, l_tmpdir, plba, debug_di, cmp_path, verbose)

    with open(os.path.join(l_tmpdir, 'cmp_results'), 'w') as f:
        f.write("Compare blocks isn't running on Jbod and raid0")
    logger.info("Result, Data and Metadata files can be found in path :{0}".format(os.path.join(l_tmpdir,
                                                                                                'cmp_results')))
    return True  # compare of JBOD / RAID-0 praids is always True. notice that in those cases, well return True earlier


def _block_set_cmp_ec(block_set: StaticSliceRange, attached_client: Client, l_tmpdir: str, plba: Optional[PRaid.LBA] = None, debug_di: bool = False, cmp_path: Optional[str] = None, verbose: bool = False) -> bool:
    cmp_plba = plba if block_set.praid.RAIDlevel not in (PRaid.RaidLevels.JBOD, PRaid.RaidLevels.RAID0) else None
    with temp_dir(attached_client.client_node.name) as r_tmpdir:
        attached_client.client_node.connection.put_dir(lpath=l_tmpdir, rpath=r_tmpdir)
        files_dir = os.path.join(r_tmpdir, os.path.basename(l_tmpdir))
        results, err, di = cmp_blocks(host_name=attached_client.client_node.name, data_dir=files_dir, metadata_dir=files_dir,
                                plba=cmp_plba, data_disks=block_set.praid.dataDisks,
                                parity_disks=block_set.praid.parityDisks,
                                debug_di=debug_di, cmp_path=cmp_path, verbose=verbose)

        logger.info("data {0} dump files can be found in path :{1}".format(block_set.block_set_idx if isinstance(block_set, BlockSet) else block_set.slice_idx, l_tmpdir))
    if results:
        logger.info("Compare Blocks Result:\n{}".format(results))
        with open(os.path.join(l_tmpdir, 'cmp_results'), 'w') as f:
            f.write(results)
    else:
        raise Exception("Got Errors in cmp_blocks:{0}".format(err))

    logger.info("compare_block result idx = {0} in path {1}".format(block_set.block_set_idx if isinstance(block_set, BlockSet) else block_set.slice_idx, "%s/cmp_results" % l_tmpdir ))
    return di == 0


def local_md5_compare(path0: str, path1: str) -> bool:
    """preforms naive di check for raid1 by comparing hash on 2 data files"""
    md50 = subprocess.check_output("cat {} | md5sum".format(path0), shell=True)
    md51 = subprocess.check_output("cat {} | md5sum".format(path1), shell=True)
    return md50 == md51
