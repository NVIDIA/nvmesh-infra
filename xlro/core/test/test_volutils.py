#!/usr/bin/env python

# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import sys
import os
import unittest
from xlro.core.entities import Manager, Volume, PRaid
from xlro.core.util.volume_utils import volume_from_spec, VOL_PREFIX
from xlro.core import infra_conf
from xlro.core.sdk.Utils import Utils as SDKUtils

class ClusterTest(unittest.TestCase):
    manager: Manager
    @classmethod
    def setUpClass(cls):
        try:
            cls.manager = Manager.get_manager()
        except:
            raise unittest.SkipTest('No management specified')

class TestVolUtils(ClusterTest):
    def test_spec(self):
        def_bytes = SDKUtils.convertUnitCapacityToBytes(infra_conf.root.volume_defs._defaults_.capacity)
        v = volume_from_spec('raid-1')
        assert v.RAIDlevel == PRaid.RaidLevels.RAID1, 'Incorrect raid-level {} for Raid-1'.format(v.RAIDlevel)
        assert v.capacity == def_bytes, 'Volume did not inherit capacity from defaults'
        assert v.name.startswith(VOL_PREFIX), 'Misnamed volume: {}'.format(v.name)
        v = volume_from_spec('ec-12D-3P', name='joe', capacity='17G')
        assert v.name == 'joe', 'Name override failed.'
        assert v.capacity == SDKUtils.convertUnitCapacityToBytes('17G'), 'Capacity override failed.'
        assert v.dataBlocks == 12 and v.parityBlocks == 3, 'EC parsed blocks failed.'

    def test_crud(self):
        v: Volume = volume_from_spec('raid-0')
        assert v not in self.manager.volumes, 'Random volume {} already exists'.format(v.name)
        v.create()
        try:
            assert Volume.wait_for_creation([v]), 'Volume was not created'
            assert v in self.manager.volumes, 'Volume not in manager list.'
        finally:
            v.delete()
        assert Volume.wait_for_deletion([v]), 'Volume was not deleted'
        assert v not in self.manager.volumes, 'Volume still in manager list.'

if __name__ == '__main__':
    from xlro.core.util.cli_util import CLIArgumentParser
    args, rest = CLIArgumentParser().parse_known_args()
    rest.insert(0, sys.argv[0])
    unittest.main(argv=rest)
