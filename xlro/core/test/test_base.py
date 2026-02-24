#!/usr/bin/env python

# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from builtins import next
from builtins import map
from builtins import zip
from builtins import str
from builtins import range
import unittest
import sys
import collections
from xlro.core.entities.base import *
import time
from xlro.core.util.thread_manager import ThreadPoolManager
from typing import Dict
PY3 = sys.version_info[0] == 3


class TestEntities(unittest.TestCase):
    @entity([SourceTypes.LOCAL, SourceTypes.PROC])
    class Entity(NamedEntity):
        bar = PropertySpec()
        x = PropertySpec()
        m = PropertySpec()
        y = PropertySpec(str)
        kid = PropertySpec('Child')
        kidslist = PropertySpec(['Child'])
        emptyListDefault = PropertySpec([str], default=[])
        emptyList = PropertySpec([str])
        kidsmap = PropertySpec({'x': 'Child'})
        foo = PropertySpec()
        z = PropertySpec(str, default='default-z')
        dict_seq = PropertySpec([dict])

        @prop_loader(None, ['x'])
        def load_get_x(self, source):
            return {'x': 'x-' + source}

        @prop_loader(SourceTypes.PROC, ['y'])
        def load_get_PROC_y(self):
            return {'y': 'y-prop'}

        @prop_loader(SourceTypes.PROC, ['foo', 'bar', 'xyyzy'])
        def some_proc_func(self):
            return {'foo': 'proc-foo', 'bar': 'proc-bar', 'xyzzy': 'xyzzy'}

        @prop_loader(SourceTypes.LOCAL, ['foo', 'bar'])
        def some_local_func(self):
            return {'foo': 'local-foo', 'bar': 'local-bar'}

    @entity([SourceTypes.LOCAL])
    class Sub1(Entity):
        # Extends properties and key
        extra = PropertySpec(str, key=True)

    @entity([SourceTypes.LOCAL])
    class Sub2(Entity):
        # Overrides properties and key
        _xlro_keyprops = ['extra']
        extra = PropertySpec(str, key=True)
        _xlro_props: Dict[str, PropertySpecT] = {}

    @entity([SourceTypes.LOCAL])
    class Sub3(Entity):
        # Extends properties and replaces key
        _xlro_keyprops = ['extra']
        extra = PropertySpec(str, key=True)

    @entity([SourceTypes.LOCAL])
    class Sub4(Entity):
        # extend properties for mutable once
        mutable_prop = PropertySpec(list)
        set_prop = PropertySpec(set)

    @entity([SourceTypes.LOCAL])
    class LoadCacheEntity(Entity):
        cached_0 = PropertySpec(list)
        extra_cached_0 = PropertySpec(list)
        prop_with_no_loader = PropertySpec(list)

        star_counter = 0

        @prop_loader(SourceTypes.LOCAL, ['*'])
        def some_local_star_loader(self):
            """counting LOCAL star loader - throws AssertionError for every odd call"""
            self.star_counter += 1
            assert (self.star_counter + 1) % 2, "loader fails when loader_counter is odd. count={}".format(self.star_counter)
            return {'y': "i am y", 'extra_cached_0': [1, 2, 3]}

        cached_counter = 0

        @prop_loader(SourceTypes.LOCAL, ['cached_0'])
        def some_local_cached_loader(self):
            """counting LOCAL loader for 'cached_0' prop"""
            self.cached_counter += 1
            return {'cached_0': [1, 2, 3]}

    @entity([SourceTypes.LOCAL, SourceTypes.OS])
    class MultiLoadCacheEntity(LoadCacheEntity):
        sourced_prop = PropertySpec(str)

        multi_counter = {SourceTypes.LOCAL: 0, SourceTypes.OS: 0}

        @prop_loader([SourceTypes.LOCAL, SourceTypes.OS], ['*'])
        def some_multi_star_loader(self, source):
            self.multi_counter[source] += 1
            return {"sourced_prop": "{}_val".format(source)}

    @entity([SourceTypes.LOCAL, SourceTypes.PROC])
    class SubEntity(Entity):
        foo2 = PropertySpec(str, key=True)

        @prop_loader(SourceTypes.PROC, ['y'])
        def load_get_PROC_y(self):
            return {'y': 'y-subprop'}

    @entity([SourceTypes.LOCAL])
    class Child(BaseEntity):
        p1 = PropertySpec(str, key=True)
        p2 = PropertySpec()
        another = PropertySpec('Entity') # a loop!

    @entity([SourceTypes.LOCAL])
    class TransEntity(NamedEntity):
        x = PropertySpec(int)
        y = PropertySpec(int, transient=True)
        load_counter = 0

        @prop_loader(SourceTypes.LOCAL, ['x'])
        def load_x(self):
            self.load_counter += 1
            return {'x': 1}

        @prop_loader(SourceTypes.LOCAL, ['*'])
        def load_star_without_y(self):
            return {'someThingElse': 'doNotCare'}

        @prop_loader(SourceTypes.LOCAL, ['*'])
        def load_star_with_y(self):
            return {'y': self.load_counter}

    @entity([SourceTypes.LOCAL, SourceTypes.PROC])
    class LoadNoneEntity(Entity):
        none_src_prop_2 = PropertySpec(int)
        none_src_prop_3 = PropertySpec(int)
        none_src_prop_4 = PropertySpec(int)
        none_src_prop_5 = PropertySpec(int)

        @prop_loader(None, ['none_src_prop_2'])
        def load_no_src_2_good(self, source=None):
            return {'none_src_prop_2': 2}

        @prop_loader(None, ['none_src_prop_3'])
        def load_no_src_3_good(self, source):
            return {'none_src_prop_3': 3}

        @prop_loader([], ['none_src_prop_4'])
        def load_no_src_4_good(self, source):
            return {'none_src_prop_4': 4}

        @prop_loader('', ['none_src_prop_5'])
        def load_no_src_5_good(self, source):
            return {'none_src_prop_5': 5}

    @entity([SourceTypes.LOCAL])
    class DefaultsEntity(Entity):
        empt_mute_prop = PropertySpec(list, default=[])
        full_mute_prop = PropertySpec(list, default=[1,2,3])
        immute_prop = PropertySpec(str, default='123')

    @entity([SourceTypes.LOCAL])
    class DefaultKeyEntity(DefaultsEntity):
        def_key_prop = PropertySpec(list, key=True, default=[1, 2, 3])

    @entity([SourceTypes.LOCAL])
    class RecursiveLoaderEntity(NamedEntity):
        no_loader = PropertySpec()

        @prop_loader(SourceTypes.LOCAL, ['no_loader'])
        def infinte_loader(self):
            return {'no_loader': self.no_loader}

    @entity([SourceTypes.PROC])
    class ProcEntity(Entity):
        pass

    def test_create_instance_parallel(self):

        def get_entity(entity_name):
            time.sleep(1)
            return self.Entity.instance(name=entity_name)
        try:
            results = list(ThreadPoolManager(max_workers=50).map(get_entity, ["multithread_test_entity"] * 50))
            self.assertEqual(len(set(results)), 1, "Duplicate entities")
        except Exception as e:
            self.assertTrue(False, "got Exception during get or create instance, error: {}".format(e))

    def test_transient_load_at_end(self):
        te = self.TransEntity(name='test_1')
        te._xlro_loaders[SourceTypes.LOCAL]['*'].sort(key=lambda f: f.__name__.count('with_y')) # sorting loaders list so the first '*' loader won't return y - transient property
        te.x # x = 1, x_counter = 1
        self.assertEqual(te.y, 1) # x = 1, x_counter = 1, y = 1
        te.get_property('x', no_cache=True) # x = 1, x_counter = 2, y = 2
        self.assertEqual(te.y, 2) # x = 1, x_counter = 2, y = 2

    def test_transient_load_at_start(self):
        te = self.TransEntity(name='test_2')
        te._xlro_loaders[SourceTypes.LOCAL]['*'].sort(key=lambda f: f.__name__.count('without_y')) # sorting loaders list so the first '*' loader will return y - transient property
        te.x # x = 1, x_counter = 1
        self.assertEqual(te.y, 1) # x = 1, x_counter = 1, y = 1
        te.get_property('x', no_cache=True) # x = 1, x_counter = 2, y = 2
        self.assertEqual(te.y, 2) # x = 1, x_counter = 2, y = 2

    def test_imports(self):
        from xlro.core.entities.imports_check import check_entities
        missing = check_entities()
        for test in [mod for mod in missing if mod.startswith('test_')]:
            del missing[test]
        missing.pop(__name__, None)
        missing.pop(__name__.rpartition('.')[2], None)
        errmsg = 'Missing imports in entities/__init__.py:\n'
        for mod in missing:
            errmsg += 'from {} import {}\n'.format(mod, ', '.join(missing[mod]))
        self.assertFalse(missing, errmsg)

    def test_inheritance(self):
        # Sub1 extends props and key
        self.assertIn('name', self.Sub1._xlro_props)
        self.assertIn('extra', self.Sub1._xlro_props)
        self.assertIn('name', self.Sub1._xlro_keyprops)
        self.assertIn('extra', self.Sub1._xlro_keyprops)

        # Sub2 overrides both
        self.assertNotIn('name', self.Sub2._xlro_props)
        self.assertIn('extra', self.Sub2._xlro_props)
        self.assertNotIn('name', self.Sub2._xlro_keyprops)
        self.assertIn('extra', self.Sub2._xlro_keyprops)

        # Sub3 extends properties and overrides key
        self.assertIn('name', self.Sub3._xlro_props)
        self.assertIn('extra', self.Sub3._xlro_props)
        self.assertNotIn('name', self.Sub3._xlro_keyprops)
        self.assertIn('extra', self.Sub3._xlro_keyprops)

    def test_constructors(self):
        # One should use the instance() or from_dict()factory methods for an Entity, not the constructor
        e1 = self.Entity.from_dict({'name': 'local-name'}, SourceTypes.LOCAL)
        e2 = self.Entity.instance(SourceTypes.LOCAL, name='local-name')
        e3 = self.Entity.instance(SourceTypes.LOCAL, name='other-name')
        self.assertEqual(id(e1), id(e2))
        self.assertNotEqual(id(e1), id(e3))
        # constructor won't catch you if you happen to be first with unique key
        self.Entity(SourceTypes.LOCAL, name='new-name')
        self.assertRaises(Exception, lambda: self.Entity(SourceTypes.LOCAL, name='new-name'))
        self.assertEqual(e1.key(), 'Entity:local-name')

        # key fields required
        # e = self.Entity(SourceTypes.LOCAL)
        # print '** ENTITY:', e
        # print '** ENTITY.NAME:', e.name
        self.assertRaises(Exception, lambda: self.Entity(SourceTypes.LOCAL))
        self.assertRaises(Exception, lambda: self.Entity.instance(SourceTypes.LOCAL))

        # Sub-entity with extended key
        self.assertRaises(Exception, lambda: self.SubEntity.instance(SourceTypes.LOCAL))
        self.assertRaises(Exception, lambda: self.SubEntity.instance(SourceTypes.LOCAL, name='fred'))
        self.assertRaises(Exception, lambda: self.SubEntity.instance(SourceTypes.LOCAL, foo2='barney'))
        sub = self.SubEntity.instance(SourceTypes.LOCAL, name='Joe', foo2='W')
        sub2 = self.SubEntity.instance(SourceTypes.LOCAL, foo2='W', name='Joe')
        self.assertEqual(sub.key(), 'SubEntity:Joe:W')
        self.assertEqual(id(sub), id(sub2))

        # Enforcement can be disabled. (This is needed, for example, for fire-cli which uses empty constructor()
        BaseEntity.ENFORCE_CACHE = False
        e1 = self.Entity(SourceTypes.LOCAL, name='no-enforcement')
        e2 = self.Entity(SourceTypes.LOCAL, name='no-enforcement')
        self.assertNotEqual(id(e1), id(e2))
        # enforce-cache False still requires keys
        self.assertRaises(Exception, lambda: self.Entity(SourceTypes.LOCAL))
        BaseEntity.ENFORCE_CACHE = True

    def test_types(self):
        e = self.Entity.from_dict({'name': 'local-name'})
        e2 = self.Entity.from_dict({'name': 'other-name'}, SourceTypes.LOCAL)
        self.assertTrue('foo' in e._xlro_props)
        self.assertTrue(isinstance(e.__class__.__dict__['foo'], Property))
        self.assertEqual(e.z, 'default-z', 'new-style default error.')

        self.assertEqual(e.foo, 'local-foo', 'default-loader not called.')
        e.set_property('foo', 3, SourceTypes.PROC)
        self.assertEqual(e.foo, 'local-foo', 'default should have precedence over Proc')
        e.set_property('foo', 4, SourceTypes.LOCAL)
        self.assertEqual(e.foo, 4)
        e.foo = None
        self.assertEqual(e.foo, None, 'None is still a value, and should override.')

        # Ensure that despite defaulting, all values are stored
        edict = e.to_dict()
        self.assertEqual(edict['foo'], None)
        self.assertEqual(edict['foo:LOCAL'], None)
        self.assertEqual(edict['foo:PROC'], 3)

        e.set_property('foo', 3, SourceTypes.PROC)
        self.assertEqual(e.foo, None)
        e.foo = 5
        self.assertEqual(e.foo, 5)

        e.foo = '1'
        e2.foo = '2'
        self.assertEqual(e.foo, '1')
        self.assertEqual(e2.foo, '2')

    def test_props(self):
        # Confirm values set by default-dict (is this good with instance?)
        e = self.Entity.instance(SourceTypes.LOCAL, name='local-name')
        self.assertEqual(e.name, 'local-name')

        e2 = self.Entity.from_dict({'name': 'joe'}, SourceTypes.LOCAL)
        self.assertEqual(e2.name, 'joe')
        e3 = self.Entity.instance(name='smith')
        self.assertEqual(e3.name, 'smith')

        with self.assertRaises(Exception): # Unknown property should raise exception
            e.xyzzy # type: ignore ## Intentional failure
        with self.assertRaises(Exception): # Known property with no loader should raise exception
            e.kid # type: ignore ## Intentional failure
        e.y # Known property with no loader for default source-type should *not* raise exception

        # Try a prop and source-specific loader
        self.assertEqual(e.get_property('x', SourceTypes.PROC), 'x-PROC')

        # Try default and group loader
        self.assertEqual(e.foo, 'local-foo')

        # Explicitly retrieve second source-type
        self.assertEqual(e.get_property('foo', SourceTypes.PROC), 'proc-foo')

        # proc-foo also loads other properties as part of group
        self.assertEqual(e.xyzzy, 'xyzzy')  # type: ignore ## Intentional test of dynamic property

        e2 = self.Entity(SourceTypes.LOCAL, {'name': 'local-name2'})
        se = self.SubEntity(SourceTypes.LOCAL, {'name': 'local-sub', 'foo2': 'needed-for-key'})
        e4 = self.Sub4.instance(name='quigon')

        # Make sure no cross-impact between instances, types and sub-types
        self.assertEqual(e.name, 'local-name')
        self.assertEqual(e2.name, 'local-name2')
        self.assertEqual(se.name, 'local-sub')
        self.assertEqual(se.get_property('y', SourceTypes.PROC), 'y-subprop')
        self.assertEqual(e2.get_property('y', SourceTypes.PROC), 'y-prop')

        # assure BaseEntity behave as object get & set (regarding mutable types changes)
        e4.mutable_prop = [1, 2, 3]
        tmp_mutable_prop = e4.mutable_prop
        e4.mutable_prop.append(4)
        self.assertEqual(tmp_mutable_prop, [1, 2, 3, 4])

    def test_get_attr(self):
        e = self.Entity.instance(name="matan")
        assert e.z == 'default-z'
        with self.assertRaises(Exception):
            temp = e.m
        templist = e.emptyListDefault
        templist2 = e.emptyListDefault
        assert id(templist) == id(templist2)

        templist.append('xyzzy')
        assert e.emptyListDefault == templist

    def test_dict_and_refs(self):
        e = self.Entity.instance(name='joe')
        k = self.Child.instance(p1='p1-k1')
        # k.another = e
        e.kidslist = [k, {'p1': 'p1-k2'}]
        edict = e.to_dict()
        kids = edict['kidslist']
        assert kids[0]['_entity_type'] == 'Child'
        assert kids[0]['p1'] == 'p1-k1'
        assert kids[1]['_entity_type'] == 'Child'
        assert kids[1]['p1'] == 'p1-k2'
        e = self.Entity.from_dict(edict)
        assert e.to_dict() == edict

        # Now test references.
        e.kidslist = [k, {'p1': 'p1-k2'}, k]
        edict = e.to_dict()
        kids = edict['kidslist']
        assert kids[2] == kids[0][BaseEntity.REF_PROP], 'Child 3 should be reference to child 1'

        # Restore from dict with references and overrides
        e.set_property('bar', 'baz')
        e.set_property('bar', 'baz-proc', SourceTypes.PROC)
        edict = e.to_dict()
        e2 = self.Entity.from_dict(edict)
        self.assertEqual(e.bar, e2.bar, 'default value not restored.')
        self.assertEqual(e.get_property('bar', SourceTypes.PROC), 'baz-proc', 'override name not restored.')
        k0 = e.kidslist[0]
        k2 = e.kidslist[2]
        self.assertTrue(isinstance(k0, self.Child), 'kid must be instance of Child')
        self.assertEqual(k0, k2, 'k0 and k2 should point to same object')
        self.assertEqual(id(k0), id(k2), 'k0 and k2 should point to same object')
        self.assertEqual(edict, e2.to_dict(), 'dicts should match.')

        #TODO: Still can't handle recursive (e.another = e)

    def test_nesting(self):
        e = self.Entity.instance(name='nesting')
        k1 = self.Child.from_dict({'p1': 'p1-k1'})
        k2 = self.Child.from_dict({'p1': 'p1-k2'})
        e.kid = k1
        assert e.kid.p1 == 'p1-k1'
        e.kidslist = [ k1, k2, {'p1': 'p1-k3'} ]
        assert e.kidslist[0].p1 == 'p1-k1'
        assert isinstance(e.kidslist[2], self.Child)
        assert e.kidslist[2].p1 == 'p1-k3'
        e.kidsmap = {'a': k1, 'b': {'p1': 'new-p1', 'p2': 'p2-b'}, 'c': k2}
        assert e.kidsmap['b'].p2 == 'p2-b'
        assert e.kidsmap['a'].p1 == 'p1-k1'
        assert e.kidsmap['c'].p1 == 'p1-k2'
        str(e)

    def test_keys(self):
        self.assertRaises(Exception, lambda: NamedEntity(), 'Entity should require key')
        ne1 = NamedEntity(name='joe')
        ne2 = NamedEntity(name='deb')
        # Reset is allowed
        ne1.set_property('name', 'joe')
        self.assertRaises(Exception, lambda: ne1.set_property('name', 'fred'), 'Entity key should be immutable')

    def test_typed_creation(self):
        ne = NamedEntity(name='freddy')
        d = ne.to_dict()
        e = BaseEntity.from_dict(d)
        assert type(e) == type(ne), 'From_dict should create requested subclass.'
        e = BaseEntity.instance(**d)
        assert type(e) == type(ne), 'instance should create requested subclass.'

    def test_get(self):
        e = NamedEntity.instance(name='joe-w')

        # This should raise an exception for non-key fields. Should use from_dict() or set_properties()?
        e = NamedEntity.instance(name='joe-w', x=2)
        # This should == None, NOT throw exception
        assert getattr(e, 'y', None) == None, 'getattr() should return the default value.'

    @entity([SourceTypes.LOCAL, SourceTypes.PROC])
    class LoadEntity0(Entity):

        @prop_loader(SourceTypes.PROC, ['foo', '*'])
        def load_foo_from_proc_at_father(self):
            return {}

    @entity([SourceTypes.LOCAL, SourceTypes.PROC])
    class LoadEntity1(LoadEntity0):

        @prop_loader(SourceTypes.PROC, ['foo', '*'])
        def load_foo_from_proc_at_son(self):
            return {}

    @entity([SourceTypes.LOCAL])
    class SequencedEntity(Entity):
        seq_str = PropertySpec((str, ))

    def test_loaders_order_foo(self):
        father = self.LoadEntity0.instance(name='loading_0')
        child = self.LoadEntity1.instance(name='loading_1')

        father_all_loaders = father._xlro_loaders[SourceTypes.PROC]['foo']
        child_all_loaders = child._xlro_loaders[SourceTypes.PROC]['foo']
        child_only_loaders = [loader for loader in list(child.__class__.__dict__.values())
                              if callable(loader) and hasattr(loader, '_loads')]

        for stype in child._xlro_loaders:
            for p, p_loaders in child._xlro_loaders[stype].items():
                self.assertListEqual(child_all_loaders, child_only_loaders + father_all_loaders)

    def test_cache_not_calling_loaders_twice(self):
        cached_loaded_ent = self.LoadCacheEntity(name='ent_0')

        cached_0 = cached_loaded_ent.cached_0
        self.assertEqual(cached_loaded_ent.cached_counter, 1)  # assure chcd loader was called once

        # an odd call to 'some_local_star_loader raises exception
        self.assertRaises(AttributeError, cached_loaded_ent.get_property, 'prop_with_no_loader')
        self.assertEqual(cached_loaded_ent.star_counter, 1)  # assure star loader was called once

        extra_cached_0 = cached_loaded_ent.get_property('extra_cached_0')
        self.assertEqual(cached_loaded_ent.star_counter, 2)  # assure star loader wasn't called again due to caching

        cached_1 = cached_loaded_ent.get_property('cached_0', no_cache=True)
        self.assertEqual(cached_loaded_ent.cached_counter, 2)  # assure chcd loader was called again
        self.assertIsNot(cached_0, cached_1)  # assure chcd object has changed

        # an odd call to 'some_local_star_loader raises exception
        self.assertRaises(AttributeError, cached_loaded_ent.get_property, 'extra_cached_0', no_cache=True)
        self.assertEqual(cached_loaded_ent.star_counter, 3)  # assure star loader was called again

        extra_cached_1 = cached_loaded_ent.get_property('extra_cached_0', no_cache=True)
        self.assertEqual(cached_loaded_ent.star_counter, 4)  # assure star loader was called again

        self.assertIsNot(extra_cached_0, extra_cached_1)  # assure extra_chcd object has changed

    def test_cache_multi_loaders(self):
        cached_loaded_ent = self.MultiLoadCacheEntity(name='ent_1')

        # checks basic caching mechanism and counting
        local_p = cached_loaded_ent.get_property('sourced_prop', SourceTypes.LOCAL)
        self.assertEqual(cached_loaded_ent.multi_counter[SourceTypes.LOCAL], 1)
        self.assertEqual(cached_loaded_ent.multi_counter[SourceTypes.OS], 0)
        self.assertTrue([cached_loaded_ent._loaders_called[SourceTypes.LOCAL][loader]
                         for loader in cached_loaded_ent._loaders_called[SourceTypes.LOCAL] if
                         cached_loaded_ent.some_multi_star_loader.__func__ is getattr(loader, 'func', loader)][0]) # type: ignore[attr-defined] ## mypy doesn't support __func__?

        # checks caching mechanism for multi source loaders
        # calls a prop with no loaders - to assure the 'LOCAL' star loaders is not called twice
        self.assertRaises(AttributeError, getattr, cached_loaded_ent, 'prop_with_no_loader')

        self.assertEqual(cached_loaded_ent.multi_counter[SourceTypes.LOCAL], 1)
        self.assertEqual(cached_loaded_ent.multi_counter[SourceTypes.OS], 1)
        self.assertTrue([cached_loaded_ent._loaders_called[SourceTypes.OS][loader]
                         for loader in cached_loaded_ent._loaders_called[SourceTypes.OS] if
                         cached_loaded_ent.some_multi_star_loader.__func__ is getattr(loader, 'func', loader)][0]) # type: ignore[attr-defined] ## mypy doesn't support __func__?

        # checks the cache reset mechanism
        cached_loaded_ent.reset_loaders_called_cache([SourceTypes.LOCAL])
        self.assertTrue(SourceTypes.LOCAL not in cached_loaded_ent._loaders_called)
        self.assertTrue([cached_loaded_ent._loaders_called[SourceTypes.OS][loader] for
                         loader in cached_loaded_ent._loaders_called[SourceTypes.OS] if
                         cached_loaded_ent.some_multi_star_loader.__func__ is getattr(loader, 'func', loader)][0]) # type: ignore[attr-defined] ## mypy doesn't support __func__?

        cached_loaded_ent.reset_loaders_called_cache()
        self.assertTrue(SourceTypes.LOCAL not in cached_loaded_ent._loaders_called)
        self.assertTrue(SourceTypes.OS not in cached_loaded_ent._loaders_called)

    def test_cache_two_different_instances(self):
        cached_loaded_ent_0 = self.LoadCacheEntity(name='ent_2_0')
        cached_loaded_ent_1 = self.LoadCacheEntity(name='ent_2_1')

        cached_0_0 = cached_loaded_ent_0.cached_0
        if not PY3:
            self.assertTrue(cached_loaded_ent_0._loaders_called[SourceTypes.LOCAL]
                            [self.LoadCacheEntity.some_local_cached_loader.__func__]) # type: ignore[attr-defined] ## mypy doesn't support __func__?
        self.assertTrue(SourceTypes.LOCAL not in cached_loaded_ent_1._loaders_called)

        cached_0_1 = cached_loaded_ent_1.cached_0

        if not PY3:
            self.assertTrue(cached_loaded_ent_0._loaders_called[SourceTypes.LOCAL]
                            [self.LoadCacheEntity.some_local_cached_loader.__func__]) # type: ignore[attr-defined] ## mypy doesn't support __func__?
            self.assertTrue(cached_loaded_ent_1._loaders_called[SourceTypes.LOCAL]
                            [self.LoadCacheEntity.some_local_cached_loader.__func__]) # type: ignore[attr-defined] ## mypy doesn't support __func__?

        cached_loaded_ent_0.reset_loaders_called_cache()
        self.assertTrue(SourceTypes.LOCAL not in cached_loaded_ent_0._loaders_called)
        if not PY3:
            self.assertTrue(cached_loaded_ent_1._loaders_called[SourceTypes.LOCAL]
                            [self.LoadCacheEntity.some_local_cached_loader.__func__]) # type: ignore[attr-defined] ## mypy doesn't support __func__?

    def test_cache_behavior_after_reset(self):
        cached_loaded_ent = self.LoadCacheEntity(name='ent_3')

        cached_0_0 = cached_loaded_ent.cached_0
        # uses get_property instead .x because __getattr__ is called twice for AttributeErrors
        self.assertRaises(AttributeError, cached_loaded_ent.get_property, 'extra_cached_0')

        if not PY3:
            self.assertTrue(cached_loaded_ent._loaders_called[SourceTypes.LOCAL]
                            [self.LoadCacheEntity.some_local_cached_loader.__func__], LoaderStatus.PASSED) # type: ignore[attr-defined] ## mypy doesn't support __func__?
            self.assertIs(cached_loaded_ent._loaders_called[SourceTypes.LOCAL]
                          [self.LoadCacheEntity.some_local_star_loader.__func__], LoaderStatus.FAILED) # type: ignore[attr-defined] ## mypy doesn't support __func__?

        cached_loaded_ent.reset_loaders_called_cache()
        self.assertRaises(AttributeError, getattr, cached_loaded_ent, 'prop_with_no_loader')

        self.assertTrue(self.LoadCacheEntity.some_local_cached_loader
                        not in cached_loaded_ent._loaders_called[SourceTypes.LOCAL])

        if not PY3:
            self.assertTrue(cached_loaded_ent._loaders_called[SourceTypes.LOCAL]
                        [self.LoadCacheEntity.some_local_star_loader.__func__]) # type: ignore[attr-defined] ## mypy doesn't support __func__?

    def test_no_src_loader(self):
        n_s_e = self.LoadNoneEntity(name='lne')

        def create_bad_none_loader_entity():
            @entity([SourceTypes.LOCAL, SourceTypes.PROC])
            class BadLoadEntity(TestEntities.Entity):
                none_src_prop_0 = PropertySpec(int)
                none_src_prop_1 = PropertySpec(int)

                @prop_loader(None, ['none_src_prop_0'])
                def load_no_src_0_bad(self):
                    return {'none_src_prop_0': 0}

                @prop_loader(None, ['none_src_prop_1'])
                def load_no_src_1_bad(self, source_type=None):
                    return {'none_src_prop_1': 1}
            return BadLoadEntity

        self.assertRaises(AssertionError, create_bad_none_loader_entity)

        self.assertEqual(n_s_e.none_src_prop_2, 2)
        self.assertEqual(n_s_e.none_src_prop_3, 3)
        self.assertEqual(n_s_e.none_src_prop_4, 4)
        self.assertEqual(n_s_e.none_src_prop_5, 5)
        for src in n_s_e._xlro_sourcetypes:
            self.assertEqual(n_s_e.get_property('none_src_prop_2', src, no_cache=True), 2)

        with self.assertRaises(AttributeError):
            self.assertEqual(n_s_e.get_property('none_src_prop_2', SourceTypes.MANAGEMENT), 2)

    def test_empty_set_prop(self):
        ent0 = self.Sub4(name='ent0')
        ent0.mutable_prop = []
        ent0.set_prop = set()

    def test_props_defaults(self):
        ent0 = self.DefaultsEntity(name='ent0')
        ent1 = self.DefaultsEntity(name='ent1')
        ent2 = self.DefaultsEntity(name='ent2')

        empt_mute_prop = ent0.empt_mute_prop
        self.assertIs(empt_mute_prop, ent0.empt_mute_prop)

        ent0.empt_mute_prop.append(0)
        self.assertEqual(ent1.empt_mute_prop, [])
        ent1.empt_mute_prop.append(1)
        self.assertEqual(ent0.empt_mute_prop, [0])
        self.assertEqual(ent2.empt_mute_prop, [])

        ent0.full_mute_prop.append(0)
        self.assertEqual(ent1.full_mute_prop, [1, 2, 3])
        ent1.full_mute_prop.append(1)
        self.assertEqual(ent0.full_mute_prop, [1, 2, 3, 0])
        self.assertEqual(ent2.full_mute_prop, [1, 2, 3])

        immute_prop = ent0.immute_prop
        self.assertIs(immute_prop, ent0.immute_prop)
        self.assertEqual(ent1.immute_prop, '123')
        ent0.immute_prop = '456'
        self.assertEqual(ent0.immute_prop, '456')

    def test_key_props_defaults(self):
        ent0 = self.DefaultKeyEntity(name='ent0')
        self.assertEqual(ent0.def_key_prop, [1, 2, 3])
        ent1 = self.DefaultKeyEntity(name="ent1", def_key_prop=[4, 5, 6])
        self.assertEqual(ent1.def_key_prop, [4, 5, 6])
        with self.assertRaises(Exception):
            ent0.def_key_prop = [4, 5, 6]

        dkp0 = ent0.def_key_prop
        dkp1 = ent1.def_key_prop
        self.assertNotEqual(id(dkp1), id(dkp0))
        self.assertEqual(ent1.def_key_prop, [4, 5, 6])
        ent2 = self.DefaultKeyEntity(name='ent2')
        self.assertEqual(ent2.def_key_prop, [1, 2, 3])

    def test_set_entity_prop_from_dict(self):
        ent0 = self.Entity(name='ent0')
        child_dict = {'p1': 'p1_val', 'p2': 'p2_val'}
        setattr(ent0, 'kid', child_dict)
        self.assertEqual(type(ent0.kid), self.Child)

    def validate_seq_prop_type(self, ent, prop, seq_val):
        """compare val type against ent's prop's 'ptype' from PropertySpec"""
        spec = ent._property_spec(prop)
        prop_type = type(spec.ptype)
        membertype = next(iter(spec.ptype))
        self.assertIsInstance(seq_val, prop_type, 'val is not instance of ptype')
        self.assertTrue(all(isinstance(val, membertype) for val in seq_val), 'not all members suites ptype member type')

    def test_set_sequence_prop(self):
        seq_ent = self.SequencedEntity(name='seq_ent_0')
        base_seq_value = ('1', '2', '3')

        seq_ent.seq_str = base_seq_value
        self.validate_seq_prop_type(seq_ent, 'seq_str', seq_ent.seq_str)

        seq_ent.seq_str = list(base_seq_value)
        self.validate_seq_prop_type(seq_ent, 'seq_str', seq_ent.seq_str)

        seq_ent.seq_str = set(base_seq_value)
        self.validate_seq_prop_type(seq_ent, 'seq_str', seq_ent.seq_str)

        seq_ent.seq_str = iter(base_seq_value)
        self.validate_seq_prop_type(seq_ent, 'seq_str', seq_ent.seq_str)

        seq_ent.seq_str = list(map(str, base_seq_value))
        self.validate_seq_prop_type(seq_ent, 'seq_str', seq_ent.seq_str)

        seq_ent.seq_str = list(map(int, base_seq_value))
        self.validate_seq_prop_type(seq_ent, 'seq_str', seq_ent.seq_str)

    def test_set_dict_seq_prop(self):
        ent0 = self.Entity(name='ent1')
        dict_seq = [{v: i} for i, v in enumerate('abcd')]
        ent0.dict_seq = dict_seq
        self.assertEqual(ent0.dict_seq, dict_seq)

    def test_infinte_loading(self):
        ent = self.RecursiveLoaderEntity(name='ent')
        with self.assertRaises(AttributeError):
            ent.no_loader

    def test_proc_entity(self):
        # pent.loos_prop = loos_prop_val

        pent = self.ProcEntity(name='pent')
        get_prop_pmaps = lambda prop: [pmap_key for pmap_key, pmap in pent._property_maps.items() if prop in pmap]
        # self.assertEqual(pent._xlro_sourcetypes, [SourceTypes.LOCAL, SourceTypes.PROC])
        loos_prop_val = 'tmp_loos_prop'
        loos_prop_name = 'loos_prop'
        setattr(pent, loos_prop_name, loos_prop_val)
        self.assertIn(loos_prop_name, pent.__dict__)
        # print get_prop_pmaps('loos_prop')
        self.assertFalse(get_prop_pmaps('loos_prop'))
        # self.assertFalse(all(map(lambda pmap: loos_prop_name not in pmap, pent._property_maps.values())))
        pent.y = 'y_val'
        self.assertNotIn('y', pent.__dict__)
        self.assertTrue(get_prop_pmaps('y'))
        self.assertTrue(get_prop_pmaps('y') == [SourceTypes.LOCAL])

    def test_sourcetypes_local_insertion(self):
        @entity(sourcetypes=[SourceTypes.LOCAL, SourceTypes.PROC])
        class LpEntity(NamedEntity):
            pass
        self.assertIn(SourceTypes.LOCAL, LpEntity._xlro_sourcetypes)
        self.assertIn(SourceTypes.PROC, LpEntity._xlro_sourcetypes)
        self.assertNotIn(SourceTypes.MANAGEMENT, LpEntity._xlro_sourcetypes)

    def test_immutable_list(self):
        @entity(sourcetypes=[SourceTypes.LOCAL, SourceTypes.PROC, SourceTypes.MANAGEMENT])
        class TestList(NamedEntity):
            lst = PropertySpec([str])

            @prop_loader(SourceTypes.PROC, ['lst'])
            def _loader(self):
                return {'lst': ['a', 'b', 'c']}

        # User flow - should only set local fields
        e = TestList.instance(name="toTest")
        # setting local
        e.lst = []
        x = e.lst
        y = e.lst
        x.append('FFF')
        self.assertEqual(x, y)
        self.assertEqual(id(x), id(y))

        # Block users to change not local source types lists
        y = e.get_property('lst', SourceTypes.PROC, True)
        self.assertNotEqual(id(x), id(y))
        with self.assertRaises(Exception):
            y.append("check")
        with self.assertRaises(Exception):
            del y[0]
        with self.assertRaises(Exception):
            y[1] = "wow"

        # how should we change lists internally
        to_update = list(y)
        to_update.append('d')
        e.set_property('lst', to_update, SourceTypes.PROC)
        self.assertEqual(e.get_property('lst', SourceTypes.PROC, False), to_update)
        self.assertNotEqual(id(e.get_property('lst', SourceTypes.PROC, False)), id(to_update))

    def test_immutable_dict(self):
        @entity(sourcetypes=[SourceTypes.LOCAL, SourceTypes.PROC, SourceTypes.MANAGEMENT])
        class TestDict(NamedEntity):
            city_to_num = PropertySpec({str: int})

            @prop_loader(SourceTypes.PROC, ['city_to_num'])
            def _loader(self):
                return {'city_to_num': {"tel aviv": 1, "rishon": 2, "jerusalem": 3}}

        # User flow - should only set local fields
        e = TestDict.instance(name="toTest")
        # setting local
        e.city_to_num = {}
        x = e.city_to_num
        y = e.city_to_num
        x["haifa"] = 4
        self.assertEqual(x, y)
        self.assertEqual(id(x), id(y))

        # Block users to change not local source types lists
        y = e.get_property('city_to_num', SourceTypes.PROC, True)
        self.assertNotEqual(id(x), id(y))
        with self.assertRaises(Exception):
            y["tel aviv"] = 2
        with self.assertRaises(Exception):
            y.setdefault("beit shemesh", 999)
        with self.assertRaises(Exception):
            del y[0]
        with self.assertRaises(Exception):
            y.pop("jerusalem")

        # how should we change dict internally
        to_update = dict(y)
        to_update["rishon"] = 8
        e.set_property('city_to_num', to_update, SourceTypes.PROC)
        self.assertEqual(e.get_property('city_to_num', SourceTypes.PROC, False), to_update)
        self.assertNotEqual(id(e.get_property('city_to_num', SourceTypes.PROC, False)), id(to_update))

    @entity(sourcetypes=[SourceTypes.LOCAL, SourceTypes.PROC, SourceTypes.MANAGEMENT])
    class Human(NamedEntity):
        age = PropertySpec(int)
        best_friend = PropertySpec('Human')
        friends = PropertySpec(['Human'])

    def test_entity_cache_dict(self):
        Human = self.Human

        ent_cache = BaseEntity.ENTITY_CACHE

        ent_cache.clear()
        max_size = ent_cache.lru_max = 10
        ent_cache.clear()

        created_humans = []
        # fill up the dict
        for i in range(max_size):
            created_humans.append(Human.instance(name=str(i)))

        self.assertEqual(created_humans, list(ent_cache.lru_dict.values()))

        # add one more
        Human.instance(name=str(max_size))
        self.assertNotIn(created_humans[0], list(ent_cache.lru_dict.values()))

        # assert still in weakref:
        self.assertIn(created_humans[0], list(ent_cache.weak_dict.values()))

        citizen0 = created_humans[0]
        citizen0_genkey = citizen0.key()
        del created_humans

        # assert still in weakref:
        self.assertIn(citizen0, list(ent_cache.weak_dict.values()))

        citizen0.set_property('age', 99, SourceTypes.PROC)
        self.assertEqual(citizen0.age, 99)

        citizen0.set_property('friend', Human(name="yosi"))
        citizen0.friend.set_property('age', 400) # type: ignore

        # remove all references to citizen0
        del citizen0
        # should be gone from weakref dict now
        self.assertNotIn(citizen0_genkey, ent_cache.weak_dict)

        # entity should recreate
        citizen0 = Human.instance(name='0')
        self.assertIn(citizen0.key(), ent_cache.weak_dict)

    def test_load_to_disk(self):
        Human = self.Human
        ent_cache = BaseEntity.ENTITY_CACHE
        ent_cache.clear()
        ent_cache.lru_max = 10

        yoav = Human.instance(name="yoav")
        yoav_genkey = yoav.key()
        yoav_friends = []
        for f in range(5):
            yoav_friends.append(Human.instance(name=str(f)))
            yoav_friends[f].age = f

        top_secret = Human.instance(name="secret")
        top_secret_genkey = top_secret.key()
        top_secret.age = 666

        yoav.friends = yoav_friends
        yoav.best_friend = top_secret
        del yoav
        del top_secret
        ent_cache.pop(yoav_genkey)
        ent_cache.pop(top_secret_genkey)

        self.assertNotIn(yoav_genkey, ent_cache.weak_dict)
        self.assertNotIn(top_secret_genkey, ent_cache.weak_dict)

        # see that we only load "yoav" from disk and not all his friends
        for f in yoav_friends: # type: ignore
            f.age *= -1 # type: ignore

        # reload yoav
        yoav = Human.instance(name="yoav")
        self.assertIn(yoav_genkey, ent_cache.weak_dict)
        self.assertIn(top_secret_genkey, list(ent_cache.weak_dict.keys()))

        self.assertEqual(yoav.friends, yoav_friends)

        # validate that we only load the keys.
        for idx, friend in enumerate(yoav.friends):
            self.assertEqual(friend.age, idx*-1)

        # validate that top secret was loaded with all props
        top_secret = yoav.best_friend
        self.assertEqual(top_secret.age, 666)

    @entity(sourcetypes=[SourceTypes.LOCAL, SourceTypes.PROC])
    class CacheAwareEntity(NamedEntity):
        int_prop = PropertySpec(int, default=1)
        ent_prop = PropertySpec('CacheAwareEntity')
        list_prop = PropertySpec(['CacheAwareEntity'])
        dict_prop = PropertySpec({int: 'CacheAwareEntity'})
        # TODO: find out why {'CacheAwareEntity': int} and other complex types doesn't work

        def assert_cache_ref(self, assert_func, prop_name, src):
            def get_ref_for_comapre(elem):
                if isinstance(elem, collections.abc.Mapping):
                    return {k: get_ref_for_comapre(v) for k, v in elem.items()}
                elif isinstance(elem, collections.abc.Iterable) and not isinstance(elem, str):
                    return [get_ref_for_comapre(e) for e in elem]
                else:
                    ref = self._gen_ref(elem, src)
                    if src not in [SourceTypes.LOCAL, SourceTypes.DEFAULT]:
                        ref = str(ref)
                    return ref

            cached_item = get_ref_for_comapre(self._property_maps[src][prop_name])
            prop_item = get_ref_for_comapre(getattr(self, prop_name))
            assert_func(cached_item, prop_item)

    def test_data_structures_refs(self):
        master_ent = self.CacheAwareEntity(name='master_ent')
        list_prop = [self.CacheAwareEntity.instance(name='ent{}'.format(i)) for i in range(10)]
        ent_prop = list_prop[0]
        dict_prop = {n: ent for n, ent in zip(list(range(len(list_prop))), list_prop)}
        propmap = {'ent_prop': ent_prop, 'list_prop': list_prop, 'dict_prop': dict_prop}
        for source in [SourceTypes.LOCAL, SourceTypes.PROC]:
            master_ent.set_properties(propmap, source)
            master_ent.assert_cache_ref(self.assertEqual, 'ent_prop', source)
            master_ent.assert_cache_ref(self.assertSequenceEqual, 'list_prop', source)
            master_ent.assert_cache_ref(self.assertDictEqual, 'dict_prop', source)

    """
    Disable this test until fixing the issue
    """
    def _test_circular_refs(self):
        # this test would NOT work for LOCAL and DEFAULT sourcetypes as they return the original class and will cause
        # an infinite pointing loop
        ref_src = SourceTypes.PROC
        assert_funcs = [self.assertEqual, self.assertSequenceEqual, self.assertDictEqual]
        assert_props = ['ent_prop', 'list_prop', 'dict_prop']
        ent1 = self.CacheAwareEntity(name='ent1')
        ent2 = self.CacheAwareEntity(name='ent2')

        ent1.set_properties({'ent_prop': ent2, 'list_prop': [ent2], 'dict_prop': {1: ent2}}, ref_src)
        ent2.set_properties({'ent_prop': ent1, 'list_prop': [ent1], 'dict_prop': {1: ent1}}, ref_src)

        for assert_func, prop_name in zip(assert_funcs, assert_props):
            ent1.assert_cache_ref(assert_func, prop_name, ref_src)
            ent2.assert_cache_ref(assert_func, prop_name, ref_src)

        del ent1
        del ent2
        self.Entity.ENTITY_CACHE.clear()
        import gc
        gc.collect()
        pass


if __name__ == '__main__':
    unittest.main()
