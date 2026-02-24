#!/usr/bin/env python

# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import time
import unittest
import os
import sys
from xlro.core.util.ssh import Connection, local_execute, CmdException, execute_cmd_locally, LocalPopen
from xlro.core.entities import Host
from xlro.core.test.utils import TEST_REMOTE
from getpass import getuser
import subprocess
import socket

PY3 = sys.version_info[0] == 3


class TestSSH(unittest.TestCase):
    def test_ssh(self):
        result = Connection.execute_on_host(TEST_REMOTE, 'whoami; echo error >&2; exit 3')
        assert result[0].strip() == Connection.ssh_options(TEST_REMOTE).get('username', getuser())
        assert result[1].strip() == 'error'
        assert result[2] == 3

    def _test_terminate(self, cmd):
        begin = time.time()
        proc = Connection.get_connection('localhost').popen(cmd)
        time.sleep(0.5)
        proc.terminate()
        proc.wait()
        assert int(time.time()-begin) < 2, '"{}" not killed'.format(cmd)

    def test_terminate(self):
        self._test_terminate('sleep 5')
        self._test_terminate('sudo tail -f /etc/passwd')
        self._test_terminate('sudo journalctl -f')
        self._test_terminate('bash -c "sleep 5 && sleep 5 && sleep 5"')
        self._test_terminate('tail -f /etc/passwd')
        self._test_terminate('bash -c "sudo tail -f /etc/passwd | grep joe"')
        self._test_terminate('tail -f /etc/passwd | sudo grep joe"')

    def test_multi(self):
        begin = time.time()
        hosts = ['localhost', TEST_REMOTE]
        results = Connection.execute_on_all(hosts, 'sleep 2; echo Hello; echo World >&2')
        # Ensure we executed in parallel, not sequential
        assert int(time.time()-begin) < 4

        # Tuples returned are (host, stdout, stderr, code)
        assert [r[0] for r in results] == hosts

    def test_local(self):
        # This also covers the new execute() exceptions
        assert local_execute('exit 7')[2] == 7, 'local command did not fail as expected'
        assert local_execute('exit 0')[2] == 0, 'local command did not succeed as expected'
        self.assertRaises(CmdException, local_execute, 'exit 0', fail=0)
        self.assertRaises(CmdException, local_execute, 'exit 0', fail=(2,1,0))
        self.assertRaises(CmdException, local_execute, 'exit 0', success=3)
        self.assertRaises(CmdException, local_execute, 'exit 0', success=(1,2,3))
        self.assertRaises(CmdException, local_execute, 'echo "Hello World" >&2', err_is_fail=True)

        # Confirm change in stream behavior
        errcmd = 'echo "Hello World" >&2'
        lcl_result = execute_cmd_locally(errcmd)
        if not PY3:
            assert lcl_result[0].strip() == 'Hello World', 'Legacy local not merging stdout/stderr'
        if not PY3:
            assert lcl_result[0] == local_execute(errcmd)[1], 'Stderr redirect error'
        # Confirm ability to maintain
        if not PY3:
            assert lcl_result == local_execute(errcmd, stderr=subprocess.STDOUT), 'Stderr compatibility error'

    def test_hosts(self):
        # Confirm expected localhost behavior
        is_ssh_cmd = f'test "$SSH_CLIENT" != "{os.environ.get("SSH_CLIENT", "")}"'
        lhost = Host.instance(name='localhost')
        rhost = Host.instance(name=TEST_REMOTE)
        assert lhost != rhost, 'Host({}) should != Host(localhost)'.format(TEST_REMOTE)
        assert lhost.execute(is_ssh_cmd)[2] == 1, 'localhost should not use SSH'
        assert rhost.execute(is_ssh_cmd)[2] == 0, 'remote should use SSH'

        # Confirm execute_on_all behavior
        hosts = [lhost, rhost]
        assert len(Host.execute_on_all(hosts, is_ssh_cmd)) == len(hosts), 'Missing results'
        self.assertRaises(CmdException, Host.execute_on_all, hosts, is_ssh_cmd, success=0)
        self.assertRaises(CmdException, Host.execute_on_all, hosts, is_ssh_cmd, success=1)

        # confirm stderr consistency for Remote and Local Popen
        cmd = 'echo Hello; echo World >&2'
        assert lhost.execute(cmd) == rhost.execute(cmd), 'Local/Remote stream behavior inconsistent.'

    def Xtest_lc(self):
        import platform
        if 'fedora' in platform.platform().lower():
            self.skipTest('Fedora overrides LC_NAME')
        proc = Connection.get_connection(TEST_REMOTE).popen("echo $$; exec sleep 100")
        self.addCleanup(proc.terminate)
        pid = int(proc.stdout.readline())
        out, err, code = Connection.get_connection(TEST_REMOTE).execute('cat /proc/{}/environ'.format(pid))
        assert 'LC_NAME={}'.format(os.environ['LC_NAME']) in out, "LC_NAME not in {}".format(out)


if __name__ == '__main__':
    unittest.main()
