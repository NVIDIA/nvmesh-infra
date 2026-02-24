#!/usr/bin/env python

# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import os
import unittest
from xlro.core.entities.mrsp import Controller

class TestEntities(unittest.TestCase):
    def _test_tpv_parse(self):
        os.path.join(os.path.dirname(__file__), 'data/mrsp/test-tpv.v1')
        with open('data/mrsp/test-tpv.v1', 'r') as f:
            data = f.read()

        tpvs = Controller._create_tpv_from_text(data)
        for i, tpv in enumerate(tpvs):
            assert tpv.id == 'TP{0}'.format(i)
            assert tpv.size == i+1024

if __name__ == '__main__':
    unittest.main()

