# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from builtins import map
from builtins import zip
from builtins import str
from builtins import range
from builtins import object
import collections
import logging
from typing import Any,Dict,Hashable,Optional,Sequence,Union
from functools import reduce

_logger = logging.getLogger('dict_utils')

def _assert_reps(xxx_todo_changeme, rdct):
    (k, v) = xxx_todo_changeme
    assert v not in rdct, "value {} already exist as key in dict".format(v)
    return k, v


def reverse_updated_dict(dct, multi_value=False):

    if multi_value:
        rdct: Dict[Any, Any] = {}
        [rdct.setdefault(k, []).append(v) for item in dct.items() for k, v in (item, reversed(item))]

    else:
        rdct = dct.copy()
        list(map(lambda k_v: rdct.__setitem__(k_v[1], k_v[0]), (_assert_reps(item, rdct) for item in dct.items()))) # type: ignore ## Can't type lambda

    return rdct


class DictUtils(object):
    IGN = '_IGNORE'

    @classmethod
    def _update_ignore_key(cls, ignore: dict, key: Any, is_add: bool) -> bool:

        """
        setting the leafs in the ignore dict (add or remove the cls.IGN flag from key)
        return bool - whether the key is empty - in order to pop it from ignore dict
        """
        key_ignore_count = ignore.setdefault(key, {}).setdefault(cls.IGN, 0)

        if is_add:
            ignore[key][cls.IGN] = key_ignore_count + 1
        else:
             ignore[key][cls.IGN] = key_ignore_count - 1
             if ignore[key][cls.IGN] <= 0:
                ignore[key].pop(cls.IGN)
                if not ignore.get(key, False):
                    return True
        return False

    @classmethod
    def update_ignore_dict(cls, ignore: dict, ignore_spec: Union[Hashable, dict, Sequence], is_add: bool) -> bool:

        """
        recursive method to update the ignore dict
        tries to call itself for every key in the new_ignore dict
        for every iteration holds a list that represent for key in new whether or not to pop that key out
        when the new_ignore argument is Hashable - calls the _update_ignore_key
        returns whether or not all the all the old_ignore keys where poped

        the ignore dict saves ignored keys that are not relevant for a given moment.
        therefor every added ignore spec needs to be removed individually

        example: (starting from an empty ignore dict)
            1. add {'nvme182.acme.com'}  # ignore_dict={'nvme182.acme.com':{DictUtils.IGN: 1}}
            2. add {'nvme182.acme.com':'nvmeshclient'}  # ignore_dict={'nvme182.acme.com':{DictUtils.IGN: 1,
                                                                                        'nvmeshclient': {DictUtils.IGN: 1}}}
            3. remove {'nvmes182.acme.com'}  # ignore_dict={'nvme182.acme.com':{'nvmeshclient': {DictUtils.IGN: 1}}}
            4. result {'nvme182.acme.com':{'nvmeshclient': {DictUtils.IGN: 1}}} (step 2 remains in ignore dict)
        """

        if isinstance(ignore_spec, collections.Hashable):
            return cls._update_ignore_key(ignore, ignore_spec, is_add)

        try:
            empty_keys_flags = [cls.update_ignore_dict(ignore.setdefault(k, {}), v, is_add)
                                for k, v in ignore_spec.items()]

        except AttributeError as e:
            empty_keys_flags = [cls.update_ignore_dict(ignore, k, is_add) for k in ignore_spec]

        [ignore.pop(k) for k, flag in zip(ignore_spec, empty_keys_flags) if flag]
        return not ignore

    @classmethod
    def _compare_dicts(cls, expected: Union[dict, Sequence, Hashable], actual: Union[dict, Sequence, Hashable], ignore: dict) -> bool:

        """
        recursive method for comparing to dictionaries with considering the ignore_dict
        returns bool - the result of the check for the given 'actual' dicts -> True means equal
        returns 'and' chain of for every key in the dict - > if that key in ignore_dict - return True
        when the 'reduce' function throws an AttributeError (for .iteritems) -> checks equality of expected and actual
        NOTICE -> that might loose a case of non-dict containing shallow copied only objects

        TODO - add support in multiple valid values in actual(given) dict & similar in ignore
        """

        try:
            return reduce(lambda x, k: x and (cls.IGN in ignore.get(k, {})
                            or cls._compare_dicts(expected[k], actual[k], ignore.get(k, {}))), list(expected.keys()), True) # type: ignore ## Exceptions handled
        except KeyError as e:
            return False
        except AttributeError as e:
            return expected == actual

    @classmethod
    def cmp_dicts(cls, expected: dict, actual: dict, ignore: Optional[dict] = None) -> bool:
        return cls._compare_dicts(expected, actual, ignore or {})

    @classmethod
    def spec2ignore(cls, ignore_spec: dict) -> dict:
        ignore: Dict = {}
        cls.update_ignore_dict(ignore=ignore, ignore_spec=ignore_spec, is_add=True)
        return ignore


NOWARN_KEY = '_nowarn_'
def merge_dicts(a, b, path=None, warnfile=None):
    """ recursively merge dict b into dict a
        (as opposed to dict.update() which will overwrite whole sub-trees)
    """
    if path is None:
        path = []
    for key in b:
        if key in a:
            # Note: we don't currently support "merge" of lists. Just replace
            if isinstance(a[key], dict) and isinstance(b[key], dict):
                warnfile = warnfile if not NOWARN_KEY in a else None
                merge_dicts(a[key], b[key], path + [str(key)], warnfile=warnfile)
            elif isinstance(a[key], list) and isinstance(b[key], list):
                if a[key] and isinstance(a[key][0], dict) or b[key] and isinstance(b[key][0], dict):
                    while len(b[key]) > len(a[key]):
                        a[key].append(dict())
                    for idx in range(len(a[key])):
                        warnfile = warnfile if not NOWARN_KEY in a else None
                        merge_dicts(a[key][idx], b[key][idx], path + [str(key)], warnfile=warnfile)
                else:
                    a[key] = b[key]
            elif isinstance(a[key], list) and isinstance(b[key], str):
                a[key] = [b[key]]
            else:
                # We should be more type-safe...
                a[key] = b[key]
        else:
            # New keys generate a warning
            if warnfile and not a.get(NOWARN_KEY, False):
                _logger.warn(
                    'Unrecognized override config "{}" in file: {}'.format('.'.join(path + [str(key)]), warnfile))
            a[key] = b[key]
    return a

def del_path(d: Dict, path: str):
    ''' Remove nested items from dict, based on '.' separated path '''
    key, _, rest = path.partition('.')
    try:
        if not rest:
            del d[key]
        else:
            del_path(d[key], rest)
    except:
        # Nothing to delete - key missing or d[key] not dict
        pass

def expand_dots(d: Dict) -> Dict:
    ''' Expand . keynames to sub-dicts.  So {'a.b': 4} will become {'a': {'b': 4}} '''

    # Handle nested values
    for key in [k for k in d if '.' in k]:
        path, _, subkey = key.rpartition('.')
        subdict = d
        for elem in path.split('.'):
            subdict = subdict.setdefault(elem, dict())
        subdict[subkey] = d.pop(key)

    return d
