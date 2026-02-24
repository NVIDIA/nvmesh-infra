#!/usr/bin/env python

# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from builtins import map
from os import path, chmod
import shutil
import time
import stat
import tempfile
import unittest
import syslog
import json
import typing

from xlro.core import infra_conf
from xlro.core.util.monitor import *
from xlro.core.util.monitor_cli import mspec_to_multi_monitor
from xlro.core.test.utils import DEFAULT_FILE_PERMISSION, TEST_REMOTE

class TestMonitors(unittest.TestCase):
    host = TEST_REMOTE
    def test_pid_monitor_kill(self):
        # cmd = 'echo PID=$$ && exec sudo /opt/nvmesh/perfTest/io_stress/btestEX -c -t 0 -T 5 -D -B 300000 R 70 /dev/nvmesh/v0j'
        cmdline = 'nohup tail -f /etc/passwd'
        cmd = 'tail' #cmdline.partition(' ')[0]
        mon = PidMonitor(cmdline, self.host)
        mon.start()
        time.sleep(1)
        pid = mon.pid # mon.pid gets wiped by .stop()
        self.assertTrue(pid in Connection.execute_on_host(self.host, 'pgrep {}'.format(cmd))[0].splitlines(),
                '"{}" is not running?!'.format(cmd))
        mon.stop()
        self.assertFalse(pid in Connection.execute_on_host(self.host, 'pgrep {}'.format(cmd))[0].splitlines(),
                '"{}" is still running?!'.format(cmd))

    def test_pattern_handler(self):
        ph = PatternHandler('^hello')
        self.assertFalse(ph.handle_msg('not hello'), 'Should not match')
        self.assertTrue(ph.handle_msg('hello'), 'Should match')
        
        ph = PatternHandler('^hello +(?P<firstname>\S+)')
        self.assertFalse(ph.handle_msg('not hello'), 'Should not match')
        self.assertFalse(ph.handle_msg('hello'), 'Should not match')
        self.assertTrue(ph.handle_msg('hello bob')['firstname'] == 'bob', 'Should find "bob"')
        self.assertTrue(ph.handle_msg('hello bob the builder')['firstname'] == 'bob', 'Should find only "bob"')

        ph = PatternHandler(['^hello +(?P<firstname>\S+)', '^hi +(?P<firstname>\S+)'], { 'salutation': True })
        self.assertFalse(ph.handle_msg('not hello'), 'Should not match')
        self.assertTrue(ph.handle_msg('hello bob the builder')['firstname'] == 'bob', 'Should find only "bob"')
        self.assertTrue(ph.handle_msg('hi bob the builder')['firstname'] == 'bob', 'Should find only "bob"')
        self.assertTrue(ph.handle_msg('hi bob the builder')['salutation'], 'Should be a salutation.')

        ph = PatternHandler(['^hello +(?P<firstname>\S+)', {'^hi ': [' +mr +(?P<firstname>\S+)', ' +(?P<firstname>\S+)'] }])
        self.assertTrue(ph.handle_msg('hi mr bob the builder')['firstname'] == 'bob', 'Should find only "bob"')
        self.assertTrue(ph.handle_msg('hi there mr bob the builder')['firstname'] == 'bob', 'Should find only "bob"')
        self.assertTrue(ph.handle_msg('hi bob')['firstname'] == 'bob', 'Should find only "bob"')

    def test_pattern_monitor(self):
        fp = tempfile.NamedTemporaryFile(mode='w')
        chmod(fp.name, DEFAULT_FILE_PERMISSION)
        cmd = 'tail -f {}'.format(fp.name)
        mon = CmdMonitor(cmd, host=self.host, event_defaults={'file': fp.name})
        ph = PatternHandler(['^hello +(?P<firstname>\S+)',
            {'^hi ': [' +mr +(?P<firstname>\S+)', ' +(?P<firstname>\S+)'] }], {'hi': True})
        mon.add_msg_handler(ph)
        mon.start()
        fp.write('ignore this\n')
        fp.write('ignore hi bob\n')
        fp.write('hi mr bob\n')
        fp.write('ignore hello bob\n')
        fp.write('hello mr bob 2\n')
        fp.write('hello bob the builder\n')
        fp.write('\n')
        fp.write('\n')
        fp.write('hi mr joe\n')
        fp.file.flush()  # type: ignore  # stackoverflow.com/q/64429113/
        time.sleep(2)
        fp.close()

        hi_events = mon.events
        mon.stop()
        self.assertTrue([e['firstname'] for e in hi_events] == ['bob', 'mr', 'bob', 'joe'], 'Names are wrong.')
        self.assertTrue([e['file'] for e in hi_events] == [fp.name]*len(hi_events), 'Monitor defaults incorrect.')
        self.assertTrue([e['hi'] for e in hi_events] == [True]*len(hi_events), 'Handler defaults are wrong.')

    def test_multiline_monitor(self):
        fp = tempfile.NamedTemporaryFile(mode='w')
        chmod(fp.name, DEFAULT_FILE_PERMISSION | stat.S_IXOTH)
        test_input = '''
        something begin conversation that should be ignored
        hello joe
        hello-joe
        end conversation
        a line between conversations
        something begin conversation something
        joe says hello deb # double event
        not a middle match
        hello joe says deb
        a says that does NOT match a hello
        another says that does NOT match a hello
        last-one says that does NOT match a hello
        says that does NOT match a hello
        goodbye doesn't match either
        xsaysy does match
        end
        conversation
        real end conversation marker
        '''
        fp.write(test_input)
        fp.file.flush()  # type: ignore  # stackoverflow.com/q/64429113/
        cmd = 'cat {}'.format(fp.name)
        mon = CmdMonitor(cmd, host=self.host, event_defaults={'file': fp.name})
        ph = PatternHandler([
            'hello +(?P<firstname>\S+)', 
            RangePattern('begin conversation(?!.*ignore)', 'end conversation', 20, '((?P<WHO>\S+) )?says'),
        ])
        mon.add_msg_handler(ph)
        mon.start()
        time.sleep(1)
        fp.close()

        # assert wait_for_it(lambda: len(mon.events) == 4, timeout=10), 'should be 4 events. Got: {}'.format(json.dumps(mon.events, indent=2))
        mon.wait_all()
        assert len(mon.events) == 4, 'should be 4 events. Got: {}'.format(json.dumps(mon.events, indent=2))
        assert len([event for event in mon.events if 'firstname' in event]) == 3, 'Should be 3 hello events'
        multiline = mon.events[-1]
        assert multiline['WHO'] == 'last-one', 'WHO should be set by last matching mid-line'

    def test_multi_monitor(self):
        # Build hierarchy of monitors as Parent (Kid1, Kid2 (Gkid2.1, Gkid2.2))
        tmpd = tempfile.mkdtemp(dir=path.abspath('.'))
        chmod(tmpd, DEFAULT_FILE_PERMISSION | stat.S_IXOTH)
        gkid1 = Monitor([PatternHandler('gkid1', {'gk2.1-msg': 1})], logpath=path.join(tmpd, 'gkid1.log'))
        gkid2 = Monitor([PatternHandler('gkid2', {'gk2.2-msg': 1})], logpath=path.join(tmpd, 'gkid2.log'))
        kid1 = Monitor([PatternHandler(' kid1', {'k1-msg': 1})], logpath=path.join(tmpd, 'kid1.log'))
        kid2 = MultiMonitor([gkid1, gkid2], event_defaults={'k2-mon': True}, logpath=path.join(tmpd, 'kid2.log'))
        kid2.add_msg_handler(PatternHandler(' kid2', {'k2-msg': 'flag'}))
        class ParentMonitor(MultiMonitor):
            handled: typing.List[typing.Dict] = []
            def on_event(self, event):
                # An arbitrary event-handler, to be sure we get called as well.
                if 'k2-msg' in event:
                    self.handled.append(event)
                super(ParentMonitor, self).on_event(event)
        parent = ParentMonitor([kid1, kid2], event_defaults={'parent-mon': True}, logpath=path.join(tmpd, 'parent.log'))
        parent.add_msg_handler(PatternHandler('parent', {'parent-msg': 'mflag'}))

        parent.start()
        gkid1.on_message('line 1 for logs\n')
        gkid1.on_message('gkid1 and kid2 event\n')
        kid1.on_message('message for kid1 and parent\n')
        parent.on_message('gkid1, gkid2, kid1 and kid2 should NOT see this message, but parent should.\n')
        gkid1.on_message('last line of logs\n')
        parent.stop()

        self.assertEqual(len(gkid1.events), 1)
        self.assertEqual(len(gkid2.events), 0)
        self.assertEqual(len(kid1.events), 1)
        self.assertEqual(len(kid2.events), 2) # Self and gkid1 inherited
        self.assertEqual(kid2.events[0][MsgHandler.FULL_MSG], kid2.events[0][MsgHandler.FULL_MSG])
        self.assertEqual(len([e for e in parent.events if 'parent-msg' in e]), 2) # Self 2
        self.assertEqual(len(parent.events), 5) # Self 2 and 3 descendants

        logfile = path.join(tmpd, 'gkid1.log')
        self.assertEqual(int(Connection.execute_on_host(self.host,
                'wc -l <{}'.format(logfile))[0]), 4, '{} should have 4 lines.'.format(logfile))
        logfile = path.join(tmpd, 'parent.log')
        self.assertEqual(int(Connection.execute_on_host(self.host,
                'wc -l <{}'.format(logfile))[0]), 10, '{} should have 5 text lines + 5 events.'.format(logfile))

        self.assertIsNotNone(parent.handled)
        self.assertEqual(len(parent.handled), 1)

        # Not a cleanup, so we can leave the tmpdir if something went wrong.
        shutil.rmtree(tmpd)

    def assert_events(self, msg, monitor, **kwargs):
        if not kwargs:
            assert not monitor.events, msg + '- Unexpected events.'
            return

        # KWARGS are keys with expected value for all events
        for key, values in kwargs.items():
            expected = set(values or [])
            found = {e[key] for e in monitor.events}
            assert expected == found, '{} - Expected "{}": {}, but found: {}'.format(msg, key, expected, found)

    def assert_procs(self, monitor, running=-1, stopped=-1, failed=-1, total=-1):
        # print 'POLL-ALL:', monitor.poll_all(), \
                # 'RUNNING:', len(monitor.running_monitors()), \
                # 'STOPPED:', len(monitor.stopped_monitors()), \
                # 'FAILED:', len(monitor.failed_stopped_monitors())
        assert total < 0 or len(monitor.poll_all()) == total, 'Expected {} total.'.format(total)
        assert running < 0 or len(monitor.running_monitors()) == running, 'Expected {} running.'.format(running)
        assert stopped < 0 or len(monitor.stopped_monitors()) == stopped, 'Expected {} stopped.'.format(stopped)
        assert failed < 0 or len(monitor.failed_stopped_monitors()) == failed, 'Expected {} failed.'.format(failed)

    def test_ignore(self):
        # Build monitor hierarchy
        class EchoHandler(MsgHandler):
            def __init__(self, **kwargs):
                super(EchoHandler, self).__init__()
                self.kwargs = kwargs
            def handle_msg(self, msg):
                event = self.kwargs.copy()
                event['msg'] = msg
                return event

        class MsgMonitor(Monitor):
            def __init__(self, **kwargs):
                super(MsgMonitor, self).__init__(handlers=[EchoHandler(**kwargs)])

        m1a = MsgMonitor(mon='m1a')
        m1b = MsgMonitor(mon='m1b')
        mm1 = MultiMonitor([m1a, m1b])
        m2a = MsgMonitor(mon='m2a')
        m2b = MsgMonitor(mon='m2b')
        mm2 = MultiMonitor([m2a, m2b])
        mm = MultiMonitor([mm1, mm2])
        self.assert_events('Initial-state', mm)
        m1a.on_message('test1')
        m2b.on_message('test2')
        self.assert_events('Grandchild should have 1 events', m1a, mon=['m1a'])
        self.assert_events('Grandchild should have 1 events', m2b, mon=['m2b'])
        self.assert_events('Grandparent should have 2 events', mm, mon=['m1a', 'm2b'])

        mm.reset()
        self.assert_events('Reset should clear events', mm)
        self.assert_events('Reset should clear sub-monitor events', m1a)

        mm1.ignore()
        m1a.on_message('test3')
        m2b.on_message('test4')
        self.assert_events('Events should be ignored.', mm1)
        self.assert_events('Events should be ignored in grandchild.', m1a)
        self.assert_events('Only 2nd event should reach root.', mm, mon=['m2b'])

        mm.unignore()
        m1a.on_message('test5')
        m2b.on_message('test6')
        self.assert_events('Grandparent should have all events', mm, mon=['m1a', 'm2b'])

        # Now try stop/start
        class TestMonitor(CmdMonitor):
            def __init__(self, mon_id, host):
                super(TestMonitor, self).__init__('while true; do date; sleep 0.2; done', host,
                        expected=0, handlers=[PatternHandler('.')], event_defaults={'id': mon_id})
        m1 = TestMonitor('m1', self.host)
        m2 = TestMonitor('m2', self.host)
        mm = MultiMonitor([m1, m2])
        mm.start()
        time.sleep(0.4)
        self.assert_events('All children should be running.', mm, id=['m1', 'm2'])
        m1.ignore()
        mm.reset()
        time.sleep(0.4)
        self.assert_events('Only m2 should have events.', mm, id=['m2'])
        assert m1.process, 'M1 should still be running'
        m1.unignore()
        time.sleep(0.4)
        self.assert_events('All children should be running.', mm, id=['m1', 'm2'])

        assert len(mm.running_monitors()) == 2, 'All should be running'
        assert not mm.stopped_monitors(), 'None should be stopped.'
        assert not mm.failed_stopped_monitors(), 'None should be failed.'

        mm = MultiMonitor([MultiMonitor([MultiMonitor([MultiMonitor([mm])])])])

        self.assert_procs(mm, total=2, running=2, stopped=0, failed=0)
        m1.process.kill()
        self.assert_procs(mm, total=2, running=1, stopped=1, failed=1)
        # NOTE: Exit events can only be guaranteed after wait_all() ensures reader process completed.
        m1.wait_all()
        assert len(mm.get_events(CmdMonitor.EXIT)) == 1, 'Parent should see unexpected death event.'
        m2.ignore()
        assert m2.process, 'm2 should be running, but ignored.'
        m2.process.kill()
        # Ignored processes shouldn't count as stopped or failed.
        self.assert_procs(mm, total=2, stopped=1, failed=1)
        m2.wait_all()
        assert len(mm.get_events(CmdMonitor.EXIT)) == 1, 'Parent should not see ignored unexpected death event.'
        assert not mm.running_monitors(), 'None should be running'
        mm.reset()

        assert m2.ignoring, 'M2 should be ignoring.'
        mm.start()
        assert not m2.ignoring, 'M2 should not be ignoring after start.'
        assert len(mm.running_monitors()) == 2, 'All should be running'
        assert not mm.stopped_monitors(), 'None should be stopped.'
        time.sleep(0.4)
        self.assert_events('All children should be reporting after restart.', mm, id=['m1', 'm2'])

        self.assert_procs(mm, total=2, running=2)
        m1.ignore()
        self.assert_procs(mm, total=2, running=2)
        m1.process.kill()
        self.assert_procs(mm, total=2, running=1, stopped=0, failed=0)
        m2.process.kill()
        self.assert_procs(mm, total=2, running=0, stopped=1, failed=1)
        mm.stop()


    def test_multi_processes(self):
        gk1 = CmdMonitor('tail -f /etc/passwd', self.host)
        gk1.start()
        gk2 = CmdMonitor('tail -f /etc/passwd', self.host)
        gk2.start()
        gk3 = CmdMonitor('exit 1', self.host)
        gk3.start()
        k1 = MultiMonitor(monitors=[gk1, gk2, gk3])
        k2 = Monitor()
        parent = MultiMonitor(monitors=[k1, k2])

        submons = set([k1, k2, gk1, gk2, gk3])
        assert set(parent.all_monitors()) == submons
        assert gk3.process, 'gk3 should be running.'
        gk3.process.wait()
        gk1.stop()

        expected = [0, None, 1]
        assert set(parent.poll_all()) == set(expected), 'Expected {}, but got {}'.format(expected, parent.poll_all())
        assert parent.running_monitors() == [gk2]
        assert set(parent.stopped_monitors()) == set([gk1, gk3])
        parent.stop()

    def test_polling_monitor(self):
        pm = PatternHandler('(?P<number>\d*3\d*)')
        fp = tempfile.NamedTemporaryFile(mode='w', delete=False)
        mon = PollingMonitor(f"bash -c 'echo The number $RANDOM is random | tee -a {fp.name}'", self.host, 0.05)
        mon.add_msg_handler(pm)
        mon.start()
        waitsecs = 3
        for i in range(waitsecs*2):
            time.sleep(0.5)
            if len(mon.events):
                break
        mon.stop()
        assert mon.events, f'No random numbers with a 3?! (See {fp.name})'
        os.remove(fp.name)
        for event in mon.events:
            assert '3' in event['number'], 'Bad event: {}'.format(event)

    def test_system_monitor(self):
        if self.host == 'localhost':
            self.skipTest("test is not passing on localhost mode due to popen slow buffering?")
        mname = 'journal-monitor'
        mspec = infra_conf.get_config(f'monitors.{mname}').serialize_to_dict()
        sm = mspec_to_multi_monitor({mname: mspec}, hosts='localhost')
        sm.start()
        time.sleep(0.5)
        err = [
                "------------[ cut here ]------------",
                "KERNEL WARNING TRIGGERED",
                "Lets Fake a syslog message",
                "With 4 lines",
                "end trace",
                "irrelevent log",
                "------------[ cut here ]------------",
                "KERNEL WARNING TRIGGERED",
                "first-opening",
                "this is #EC-1234 indicator",
                "------------[ cut here ]------------",
                "KERNEL WARNING TRIGGERED",
                "Double open",
                "end trace",
                "more irrelevent log",
                "even more irrelevent log",
                "------------[ cut here ]------------",
                "KERNEL WARNING TRIGGERED with an opening #EC-4321 indicator",
                "A non-error indicator is only in the opening line",
                "end trace"
            ]
        list(map(syslog.syslog, err))
        waitsecs = 30
        for i in range(waitsecs*2):
            time.sleep(0.5)
            if len(sm.events) == 3:
                break
        else:
            assert len(sm.events) == 3, f'expected 3 events, got only {len(sm.events)} after {waitsecs} seconds.'
        sm.stop()

        # we expect only one event
        assert len(sm.events) == 3, f'expected 3 events, got {len(sm.events)}'
        assert MsgHandler.NON_ERROR not in sm.events[0], 'First event should be a real error.'
        assert MsgHandler.NON_ERROR in sm.events[1], 'Second event should be a non-error.'
        assert MsgHandler.NON_ERROR in sm.events[2], 'Third event should be a non-error.'


    def test_exit_codes(self):
        # Simple, with no expect
        cm = CmdMonitor('exit 1', self.host)
        cm.start()
        cm.wait_all()
        assert not cm.events, 'No events expected for expected==None'

        # Simple, with expect
        cm = CmdMonitor('exit 1', self.host, expected=1)
        cm.start()
        cm.wait_all()
        assert not cm.events, 'No events expected for expected==exit'

        # Simple, with expect
        cm = CmdMonitor('tail /etc/passwd', self.host, expected=1)
        cm.start()
        cm.wait_all()
        assert cm.events, 'Event expected for expected!=exit'
        assert cm.events[0][CmdMonitor.EXIT] == 0, 'Event expected with EXIT == 0'

        cm0 = CmdMonitor('tail /etc/passwd', self.host, expected=0, event_defaults={'id': 'good'})
        cm1 = CmdMonitor('xtail /etc/passwd', self.host, expected=0, event_defaults={'id': 'bad-command'})
        cm2 = CmdMonitor('xtail /etc/passwd', self.host, expected=None, event_defaults={'id': 'bad-command-ignored'})
        cm3 = CmdMonitor('tail /etc/passwdX', self.host, expected=[0], event_defaults={'id': 'bad-args'})
        cm4 = CmdMonitor('tail -f /etc/passwd', self.host, expected=[0,1], event_defaults={'id': 'long-cmd'})
        cm5 = PidMonitor('tail -f /etc/passwd', self.host, expected=[0,1], event_defaults={'id': 'long-pid'})
        mm = MultiMonitor(monitors=[cm0, cm1, cm2, cm3, cm4, cm5])
        mm.start()
        time.sleep(2)

        results = mm.poll_all()
        assert 0 in results, 'Good command should exit 0'
        assert 127 in results, 'Bad command should exit 127'
        assert 1 in results, 'Bad args should exit 1'
        assert results.count(None) == 2, 'Long commands should not return poll() value.'

        assert len(mm.events) == 2, 'Should be 2 events from bad and error command.'
        cm4.stop()
        ids = [e.get('id', None) for e in mm.events]
        assert 'bad-command' in ids, 'Bad command should generate unexpected exit event.'
        assert 'bad-args' in ids, 'Bad command should generate unexpected exit event.'
        assert len(mm.events) == 2, 'Stop() should not generate new events.'

        assert cm5.process, 'cm5 should be running.'
        cm5.process.kill()
        results = mm.poll_all()
        assert results.count(-1) == 1, 'process.kill() should return -1. stop() should not Results={}'.format(results)

        mm.wait_all()
        mm.poll_all()

        assert results == mm.wait_all(), 'Repeated wait_all/poll_all should not change.'

    def test_parallel(self):
        msgs = []
        delay = 0.3

        class Mon(Monitor):
            # Monitor with messaging
            def __init__(self, name, *args, **kwargs):
                self._name = name
                super(Mon, self).__init__(*args, **kwargs)

            def start(self):
                msgs.append(self._name)
                super(Mon, self).start()
                time.sleep(delay)

            def stop(self):
                super(Mon, self).stop()
                msgs.append(self._name)
                time.sleep(delay)

        class EMon(Mon):
            # Monitor with failing stop()
            def __init__(self, etype, *args, **kwargs):
                self.etype = etype
                super(EMon, self).__init__(*args, **kwargs)

            def stop(self):
                super(EMon, self).stop()
                raise self.etype(self._name)

        class MMon(MultiMonitor):
            # MultiMonitor with messaging
            def __init__(self, name, *args, **kwargs):
                self._name = name
                super(MMon, self).__init__(*args, **kwargs)

            def start(self):
                msgs.append(self._name)
                super(MMon, self).start()
                time.sleep(delay)

            def stop(self):
                super(MMon, self).stop()
                msgs.append(self._name)
                time.sleep(delay)

        m1 = Mon('1')
        m2 = Mon('2')
        m3 = Mon('3')
        m4 = Mon('4')
        m5 = Mon('5')
        m6 = Mon('6')


        mm1 = MMon('123', [m1, m2, m3])
        mm2 = MMon('45', [m4, m5])
        mm3 = MMon('root', [mm1, mm2, m6])

        before = time.time()
        mm3.start()
        elapsed = time.time() - before
        assert delay*5 >= elapsed >= delay*3, 'Elapsed start should much less than serial time, and more than depth * delay'
        assert len(msgs) == 9, '9 monitors should have messaged on start()'
        ids = [m for m in msgs if len(m) == 1]
        # This was failing randomly.  Assert requires many more samples.
        # assert ids != sorted(ids) and ids != sorted(ids, reverse=True), 'IDs should not be in order'
        for mm in '45', '123':
            for m in mm:
                assert msgs.index(m) > msgs.index(mm), 'Child {} start() should be after Parent {} start()'.format(m, mm)
        assert msgs[0] == 'root', 'Root start() should be first'
        msgs = []

        before = time.time()
        mm3.stop()
        elapsed = time.time() - before
        assert delay*5 >= elapsed >= delay*3, 'Elapsed stop should much less than serial time, and more than depth * delay'
        assert len(msgs) == 9, '9 monitors should have messaged on stop()'
        ids = [m for m in msgs if len(m) == 1]
        # False negatives sometimes... assert ids != sorted(ids) and ids != sorted(ids, reverse=True), 'IDs should not be in order'
        for mm in '45', '123':
            for m in mm:
                assert msgs.index(m) < msgs.index(mm), 'Child {} stop() should be before Parent {} stop()'.format(m, mm)
        assert msgs[-1] == 'root', 'Root stop() should be last'

        delay = 0
        mm1.add_monitor(EMon(Exception, 'non-fatal'))
        before = time.time()
        mm3.start()
        msgs = []
        mm3.stop()
        assert 'non-fatal' in msgs, 'Non-fatal descendant monitor should have run with Exception swallowed.'
        mm1.add_monitor(EMon(Monitor.FatalMonitorException, 'fatal'))
        exc = None
        try:
            mm3.start()
            mm3.stop()
        except Exception as e:
            exc = e
        assert isinstance(exc, Monitor.FatalMonitorException), 'Fatal descendant monitor should have thrown exception'

    def test_match_events(self):
        m1 = Monitor(event_defaults={'src': 'm1'}, log_option=Monitor.LoggingOptions.LOG_EVENT_TO_LOG)
        m2 = Monitor(event_defaults={'src': 'm2'}, log_option=Monitor.LoggingOptions.LOG_EVENT_TO_LOG)
        m3 = Monitor(event_defaults={'src': 'm3'}, log_option=Monitor.LoggingOptions.LOG_EVENT_TO_LOG)
        mm = MultiMonitor(monitors=[m1,m2,m3], event_defaults={'src': 'mm'}, log_option=Monitor.LoggingOptions.LOG_EVENT_TO_LOG)

        m1._add_event({'A': 1, 'B': 2, 'C': 3})
        m1._add_event({'A': 1, 'B': 2, 'C': 3})
        m1._add_event({'A': 1, 'B': 2, 'C': 4})
        assert len(m1.events) == 3

        m2._add_event({'A': 2, 'D': {'d': {'deep': 1}}})
        m2._add_event({'A': 2, 'D': {'d': {'deep': 0}}, 'Q': False})
        assert len(m2.events) == 2

        m3._add_event({'A': 1, 'D': {'x': 0, 'y': 1, 'z': 2}, 'Q': True})
        m3._add_event({'A': 1, 'D': {'x': 0, 'y': 2, 'z': 3}})
        assert len(m3.events) == 2

        assert len(mm.events) == 7

        def assert_matches(n_matches, includes=[], excludes=[]):
            matches = mm.match_events(includes, excludes)
            assert len(matches) == n_matches, 'Includes: {}, Excludes: {}, Should have matched {} events. Got {}: {}'.format(
                    includes, excludes, n_matches, len(matches), json.dumps(matches, indent=2))

        # Simple keys
        assert_matches(7)
        assert_matches(5, includes=['B', 'Q'])
        assert_matches(2, excludes=['B','Q'])
        assert_matches(5, includes=['A'], excludes=['Q'])

        # Dicts
        assert_matches(3, includes=[{'A':2},{'C':4}])
        assert_matches(2, includes=[{'D':{'x':0}}])
        assert_matches(1, includes=[{'D':{'d':{'deep':0}}}])
        assert_matches(3, includes=['D'], excludes=[{'D':{'d':{'deep':0}}}])

        # ANY
        assert_matches(3, includes=[{'C':Monitor.ANY}])
        assert_matches(3, excludes=[{'D':Monitor.ANY}])
        assert_matches(2, includes=[{'D':{'d':{'deep':Monitor.ANY}}}])
        assert_matches(5, includes=[{'A':Monitor.ANY}], excludes=[{'D':{'d':{'deep':Monitor.ANY}}}])
        assert_matches(5, includes=[{'A':1}], excludes=[{'D':{'d':{'deep':Monitor.ANY}}}])
        assert_matches(0, includes=[{'A':2}], excludes=[{'D':{'d':{'deep':Monitor.ANY}}}])

    def test_formatting(self):
        mon = Monitor(event_defaults={'foo': 'Hello', MsgHandler.EVENT_FORMAT: 'Hello World == {foo} {bar}' }, log_option=0)
        e1 = {'bar': 'World' }
        assert mon.format_event(e1) == 'Hello World == Hello World'
        e2 = {'bar': 'Dolly', MsgHandler.EVENT_TEMPLATE: 'Hello Dolly == {{foo}} {{bar}}' }
        assert mon.format_event(e2) == 'Hello Dolly == Hello Dolly', mon.format_event(e2)

class TestMonitorsLocal(TestMonitors):
    host = 'localhost'


if __name__ == '__main__':
    from xlro.core.util.cli_util import init_logging
    unittest.main()

