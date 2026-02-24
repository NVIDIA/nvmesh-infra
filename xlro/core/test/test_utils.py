
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from builtins import range
from typing import List
import random
import unittest

from xlro.core.util.general_utils import list_index_insert


class TestUtils(unittest.TestCase):
    def test_list_index_insert(self):
        sorted_l = list(range(100, 999))
        random.shuffle(sorted_l)
        i_l = list(enumerate(sorted_l))
        for round in range(10):
            shuffled_i_l = i_l[:]
            random.shuffle(shuffled_i_l)
            new_s_l: List = []
            for i, v in shuffled_i_l:
                list_index_insert(new_s_l, i, v)

            self.assertEqual(new_s_l, sorted_l)


if __name__ == '__main__':
    unittest.main()
