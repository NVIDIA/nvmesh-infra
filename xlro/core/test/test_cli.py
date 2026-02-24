#!/usr/bin/env python

# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import unittest
import confetti
from xlro.core.entities import BaseEntity, NamedEntity, SourceTypes
from xlro.core.entities.base import entity, PropertySpec
from xlro.core.util.cli_util import CLIArgumentParser, str2entity, EntitiesArg, config_override


class TestCLI(unittest.TestCase):
    @entity([SourceTypes.LOCAL])
    class E1(NamedEntity):
        foo = PropertySpec(str)
        bar = PropertySpec(str, key=True)

    @entity([SourceTypes.LOCAL])
    class E2(BaseEntity):
        foo = PropertySpec(int, key=True)
        baz = PropertySpec(str)
        name = PropertySpec(str, key=True)
        blah = PropertySpec(str)
        bar = PropertySpec(int, key=True)

    @entity([SourceTypes.LOCAL])
    class E2B(E2):
        pass

    def assertRaisesRegex(self, *args, **kwargs):
        try:
            # PY3
            super(TestCLI, self).assertRaisesRegex(*args, **kwargs) # type: ignore
        except:
            super(TestCLI, self).assertRaisesRegexp(*args, **kwargs)

    def test_str2entity(self):
        assert self.E1._xlro_keyprops == ['name', 'bar'], 'E1 keys should be: name, bar'
        assert self.E2._xlro_keyprops == ['foo', 'name', 'bar'], 'E2 keys should be: name, bar'

        e1 = self.E1(name='joe', bar='saloon')
        assert str2entity('name=joe:bar=saloon', self.E1) == e1
        assert str2entity('joe:saloon', self.E1) == e1
        assert str2entity('E1:joe:saloon') == e1
        assert str2entity('E1:joe2:saloon') != e1
        self.assertRaisesRegex(Exception, 'Value required for key', str2entity, 'E1:joe')
        self.assertRaisesRegex(Exception, 'Value required for key', str2entity, 'joe', self.E1)

        e2 = self.E2(name='joe', bar=100, foo=3)
        assert str2entity('E2:3:joe:100') == e2
        assert str2entity('E2:100:joe:3') != e2
        # Order not needed if explicit
        assert str2entity('E2:bar=100:name=joe:foo=3') == e2
        # Partial explicit, remaining follow order
        assert str2entity('E2:name=joe:3:100') == e2
        self.assertRaisesRegex(Exception, 'not subclass', str2entity, 'E1:joe:saloon', self.E2)

    def test_conf_overrides(self):
        conf = confetti.Config({
                'top': {
                    'whatever': {
                        'boolean': True,
                        'number': 7,
                        'float': 7.2,
                        'string': 'hello',
                        'numlist': [1, 2, 3],
                        'strlist': ['a', 'b', 'c'],
                    }
                }
            })

        # Test typing
        config_override(conf, 'top.whatever.number', '99')
        assert conf.root.top.whatever.number == 99, f'Conversion to int failed.'
        config_override(conf, 'top.whatever.float', '99.2')
        assert conf.root.top.whatever.float == 99.2, f'Conversion to float failed.'

        # Test tri-state
        for f in ['0', '', 'FaLsE']:
            config_override(conf, 'top.whatever.boolean', f)
            assert conf.root.top.whatever.boolean == False, f'Setting boolean to "{f}" failed. Expected False.'
        for f in ['1', 'hello world', 'tRuE', '-3']:
            config_override(conf, 'top.whatever.boolean', f)
            assert conf.root.top.whatever.boolean == True, f'Setting boolean to "{f}" failed. Expected True.'
        config_override(conf, 'top.whatever.boolean', 'NONE')
        assert conf.root.top.whatever.boolean == None, f'Setting boolean to None failed.'

        # Test new leaves
        self.assertRaisesRegex(Exception, 'Cannot add key',
                config_override, conf, 'top.whatever.new.path', 'hello world')
        config_override(conf, 'top.whatever.new', 'hello world')
        assert conf.root.top.whatever.new == 'hello world', f'Adding leaf failed.'

        # Test typed list operations
        config_override(conf, 'top.whatever.numlist+', '4,5')
        assert conf.root.top.whatever.numlist == [1,2,3,4,5], f'List addition failed. Got {conf.root.top.whatever.numlist}'
        config_override(conf, 'top.whatever.numlist-', '2,4,9')
        assert conf.root.top.whatever.numlist == [1,3,5], 'List subtraction failed.'

if __name__ == '__main__':
    from xlro.core.util.cli_util import init_logging
    unittest.main()

