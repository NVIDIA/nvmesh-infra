# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from builtins import str
from builtins import object
from typing import List, Dict, Optional, Any, Iterator, Tuple, Union, Generator, NamedTuple, TypeVar


class BaseLBA(object): # PY3 - should be ABCMeta?
    """
        - contains addr (lba), blockSize, delta - to support range of addresses
        - different methods to construct new LBAs suitable for device(volume,chunk ...)
        - every 'storage like entity' have an inner class 'LBA' - subclass of BaseLBA
        - allow specific typing between plba(BaseLBA) to vlba(BaseLBA)
    """

    def __init__(self, addr: Union[int, float], blockSize: int) -> None:
        assert isinstance(addr, int) or addr.is_integer(), 'address {} is not an integer'.format(addr)
        self.addr = int(addr)
        self.blockSize = blockSize

    def __radd__(self, other: int) -> int:
        return self.addr + other

    T = TypeVar('T', bound='BaseLBA')
    def __add__(self: T, other: Union[int, 'BaseLBA']) -> T:
        return self.__class__(self.addr + getattr(other, 'addr', other), self.blockSize)

    def __sub__(self: T, other: Union[int, T]) -> Union[T, 'LBARange']:
        assert getattr(other, 'addr', other) <= self.addr, 'cannot sub LBA with bigger integer'
        if isinstance(other, HwLBA):
            assert other.blockSize == self.blockSize, 'for operation between 2 lba, blockSize must be equal'
            return LBARange(other, n_blocks=(self.addr-other.addr))

        return self.__class__(self.addr - other, self.blockSize)

    def __rsub__(self, other: int) -> int:
        return other - self.addr

    def __str__(self):
        return str((self.addr, self.blockSize))

    @property
    def offset(self):
        return self.addr * self.blockSize


class SwLBA(BaseLBA):
    def __init__(self, addr):
        from xlro.core.entities.volume import Volume
        super(SwLBA, self).__init__(addr, Volume.BLOCK_SIZE)

    def __add__(self, other):
        return self.__class__(self.addr + other)

    def __sub__(self, other: Union[int, 'SwLBA']) -> 'SwLBA':
        assert getattr(other, 'addr', other) <= self.addr, 'cannot sub LBA with bigger integer'
        return self.__class__(self.addr - other)


class HwLBA(BaseLBA):
    @classmethod
    def convert(cls, lbs: 'HwLBA', block_size: int) -> 'HwLBA':
        assert isinstance(lbs, HwLBA), 'expected lbs of type HwLBA , got {}'.format(type(lbs))
        bs_ratio = lbs.blockSize / float(block_size)
        assert bs_ratio.is_integer(), 'HW Blocksize {} is not a multiple of {}'.format(lbs.blockSize, block_size)
        n_addr = lbs.addr * int(bs_ratio)
        return cls(n_addr, block_size)


class LBARange(object):
    def __init__(self, lbs: BaseLBA, n_blocks: int) -> None:
        self.lbs = lbs
        self.n_blocks = n_blocks
        self.blockSize = lbs.blockSize

    @classmethod
    def get_by_bytes(cls, lbs: BaseLBA, n_bytes: int) -> 'LBARange':
        n_blocks = n_bytes / float(lbs.blockSize)
        assert n_blocks.is_integer(), 'n_bytes divide to a natural number blocks'
        return cls(lbs, int(n_blocks))

    @classmethod
    def get_by_union(cls, lba_obj: Union['LBARange', BaseLBA], count: int) -> 'LBARange':
        if isinstance(lba_obj, BaseLBA):
            return LBARange(lba_obj, count)

        elif isinstance(lba_obj, LBARange):
            if count > 1:
                # TODO - add LBARange.__mul__ method
                return LBARange(lba_obj.lbs, lba_obj.n_blocks * count)
            else:
                # consider dealing with edge case of "illegal" count value (count <= 1 or not isinstance(count, int))
                return lba_obj
        else:
            # TODO - here you can add support in type lba_obj:int
            raise TypeError('lba_obj - {}:{} must be in types [LBARange, HwLBA]'.format(type(lba_obj), lba_obj))

    def __str__(self):
        return "{}_{}".format(self.lbs, self.n_blocks)
