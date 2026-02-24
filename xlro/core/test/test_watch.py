#!/usr/bin/env python

# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import unittest
import time
import tempfile
import shutil
import typing
import stat
import os
import json

from os import path, mkdir
from glob import glob

from xlro.core.util.watchers import PyWatchMonitor
from xlro.core.test.utils import DEFAULT_FILE_PERMISSION, TEST_REMOTE


class TestWatch(unittest.TestCase):
    tmpdir = None       # str
    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix='test-watch-')
        os.chmod(cls.tmpdir, DEFAULT_FILE_PERMISSION | stat.S_IXOTH)

    @classmethod
    def tearDownClass(cls):
        if cls.tmpdir:
            shutil.rmtree(cls.tmpdir)

    def test_file_watch(self):
        # Use glob pattern and both positional and named pattern/format
        assert self.tmpdir
        mon = PyWatchMonitor(TEST_REMOTE, sep=':', file=self.tmpdir + '/*watch*', format='{name}:{2}', delay=0.3,
                pattern='name=(?P<name>\w+).*value:([^,]+)', logpath='./watch.log')

        # Create empty file with rwrwrw
        tmpfile = path.join(self.tmpdir, 'a-watched-file')
        with open(tmpfile, 'w') as tmpfp:
            pass
        os.chmod(tmpfile, DEFAULT_FILE_PERMISSION)

        mon.start()
        # Every second, copy in new content
        chapters = 0
        for datapath in sorted(glob(path.join(path.dirname(__file__), 'data/test-watch.v*'))):
            chapters += 1
            with open(datapath, 'r') as datafp, open(tmpfile, 'w') as tmpfp:
                tmpfp.write(datafp.read())
            time.sleep(1)
        mon.stop()

        # print 'FILE-WATCH EVENTS:', json.dumps(mon.events, indent=2)
        assert len(mon.events) == 3, 'There should be 3 events'
        data: typing.Dict[str, typing.List[str]] = mon.events[0]['data']
        assert set(data.keys()) == set(['Joe', 'Deb']), 'Chapter 1 should just be Joe and Deb.'
        assert data['Joe'][1] == ['husband'], 'Chapter 1 should just be Joe and Deb.'
        assert data['Deb'][1] == ['wife'], 'Chapter 1 should just be Joe and Deb.'
        data = mon.events[1]['data']
        assert 'Joe' not in data, 'No change to Joe in Chapter 2'
        assert data['Deb2'] == ([],['2nd-wife']), 'Deb2 introduced in Chapter 2'
        data = mon.events[2]['data']
        assert not data['Joe'][1] and not data['Deb2'][1], 'Only Deb should survive chapter 3'
        assert mon.results == {'Deb':['avenged']}, 'Only Deb should survive chapter 3'

    def test_cmd_watch(self):
        mon = PyWatchMonitor(TEST_REMOTE, cmd='date "+date:%D,time:%T" | tr "," "\n" | sort')
        mon.start()
        time.sleep(4)
        mon.stop()
        assert len(mon.events) >= 2, 'Should be at least 2 events. Events={}'.format(mon.events)
        # print 'CMD-WATCH EVENTS:', json.dumps(mon.events, indent=2)
        # print 'CMD-WATCH RESULTS:', json.dumps(mon.results, indent=2)
        data0 = mon.events[0]['data']['data']
        # Initialization event is now swallowed, and only used to initialize results.
        # assert not data0[0]
        # assert len(data0[1]) == 2
        # So, even first event should have old-time and new-time, but no change in day
        assert len(data0[0]) == 1
        assert len(data0[1]) == 1
        dataN = mon.events[-1]['data']['data']
        assert len(dataN[0]) == 1
        assert len(dataN[1]) == 1
        assert mon.results[1] == dataN[1][0]

    def test_ls_watcher(self):
        from xlro.core.util.watchers import LsWatcher
        lswatcher = None
        try:
            tmpd = tempfile.mkdtemp(dir=path.abspath('.'))
            os.chmod(tmpd, DEFAULT_FILE_PERMISSION | stat.S_IXOTH)
            initfiles = ['pre-existing-1', 'pre-existing-2']
            for f in initfiles:
                open(path.join(tmpd, f), 'w').close() 
            initdirs = ['x-d1', 'x-d2']
            for d in initdirs:
                mkdir(path.join(tmpd, d))
            lswatcher = LsWatcher(TEST_REMOTE, '{0}/p* {0}/x*'.format(tmpd))
            lswatcher.start()
            start_set = set(lswatcher.results)
            assert lswatcher.is_initialized(), 'Watcher did not initialize.'
            assert len(start_set) == len(initdirs) + len(initfiles), 'Did not find all initial entries.'
            expected = set()
            newfiles = ['post-init-f1', 'xpost-init-f1']
            for f in newfiles:
                newf = path.join(tmpd, f)
                open(newf, 'w').close() 
                expected.add(newf)
            newdirs = ['post-init-d1', 'xpost-init-d1']
            for d in newdirs:
                newd = path.join(tmpd, d)
                mkdir(newd)
                expected.add(newd)
            time.sleep(1)
            new_set = set(lswatcher.results)-start_set
            assert new_set == expected, 'Did not find all new entries. Expected {}, but found: {}'.format(expected, new_set)
        finally:
            if lswatcher:
                lswatcher.stop()
            shutil.rmtree(tmpd)

if __name__ == '__main__':
    unittest.main()
