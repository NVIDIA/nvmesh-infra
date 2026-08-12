# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from builtins import range
from typing import List
import random
import unittest
from unittest import mock

from xlro.core.util import general_utils
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

    def test_get_hostnames_ssh_fallback_is_non_interactive(self):
        old_host_cache = general_utils._host_cache.copy()
        old_alias_cache = {k: v[:] for k, v in general_utils._alias_cache.items()}
        general_utils._host_cache.clear()
        general_utils._alias_cache.clear()
        commands = []

        def fake_run_local(cmd):
            commands.append(cmd)
            if cmd.startswith('host '):
                return '', '', 1
            return 'resolved-host\n', '', 0

        try:
            with mock.patch.object(general_utils, 'gethostbyaddr', side_effect=Exception('no ptr')), \
                    mock.patch.object(general_utils, 'gethostbyname', return_value='192.0.2.1'), \
                    mock.patch.object(general_utils, 'run_local', side_effect=fake_run_local):
                hostname, aliases = general_utils.get_hostnames('10.1.2.3', skip_cache=True)
        finally:
            general_utils._host_cache.clear()
            general_utils._host_cache.update(old_host_cache)
            general_utils._alias_cache.clear()
            general_utils._alias_cache.update(old_alias_cache)

        self.assertEqual(hostname, 'resolved-host')
        self.assertIn('10.1.2.3', aliases)
        self.assertIn('-o BatchMode=yes', commands[1])
        self.assertIn('-o PasswordAuthentication=no', commands[1])
        self.assertIn('-o StrictHostKeyChecking=no', commands[1])
        self.assertIn('-o UserKnownHostsFile=/dev/null', commands[1])
        self.assertIn('-o LogLevel=ERROR', commands[1])
        self.assertIn('2>/dev/null', commands[1])


if __name__ == '__main__':
    unittest.main()
