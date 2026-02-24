#!/usr/bin/env python3

# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

__version__ = '1.02'

import socket
import ctypes
import os
import json
import sys
import logging
import subprocess
from collections import defaultdict
from datetime import datetime
from itertools import count
op_counter = count()

from xlro.core import infra_conf
# sys.path.append("/opt/NVMesh/common-repo/tools")
from .read_dwarf import resolve_dwarf_only

NEEDED_C_STRUCTS = ["nvmeib_get_disk_names_reply", "nlmsghdr", "nvmeib_nl_uk_comm_msg",
                    "nvmeib_nl_uk_comm_msg", "nvmeib_nl_uk_comm_msg", "nvmeib_nl_uk_comm_rep", "nvmeib_io_to_disk",
                    "nvmeib_io_to_disk_reply"]
NEEDED_C_ENUMS = ["uk_comm_opcode"]
NEEDED_C_STRUCTURES = NEEDED_C_STRUCTS + NEEDED_C_ENUMS

# TODO: should first try to read from cache, fallback to o, fallback to ko
INFRA_SO_FILE = os.path.abspath(infra_conf.root.tools.infra_shared_so)
if os.path.isfile(INFRA_SO_FILE):
    types, enums, parsed, resolved = resolve_dwarf_only(INFRA_SO_FILE, "|".join(NEEDED_C_STRUCTURES))
else:
    ret = subprocess.Popen(['modinfo', '-F', 'filename', 'nvmeibs'], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    stdout, stderr = ret.communicate()
    types, enums, parsed, resolved = resolve_dwarf_only(stdout.strip(), "|".join(NEEDED_C_STRUCTURES))
# TODO: should cache here

try:
    for c_obj in NEEDED_C_STRUCTS:
        assert "struct__" + c_obj in dir(types)
    for c_obj in NEEDED_C_ENUMS:
        assert c_obj in dir(enums)
except AssertionError:
    raise Exception(f"Netlink can't find '{c_obj}' in {INFRA_SO_FILE}.")

logger = logging.getLogger("nvmesh_netlink")


def ctype_to_ptype(obj):
    if isinstance(obj, (ctypes.Array, list)):
        ret = [ctype_to_ptype(e) for e in obj]
        # pretty print byte arrays
        if hasattr(obj, "_type_") and obj._type_ == ctypes.c_byte:
            ret = str(bytearray(ret))
        return ret

    if isinstance(obj, ctypes._Pointer):
        return ctype_to_ptype(obj.contents) if obj else None

    if isinstance(obj, ctypes._SimpleCData):
        return ctype_to_ptype(obj.value)

    try:
        if isinstance(obj, long):
            ret = int(obj)
            if isinstance(ret, long):
                # as json don't support long just dump it as a string
                return str(ret)
            return ret
    except:
        pass

    if isinstance(obj, bytes):
        return obj.decode('utf-8')

    if isinstance(obj, (bool, int, float, str)):
        return obj

    if obj is None:
        return obj

    if isinstance(obj, (ctypes.Structure, ctypes.Union)):
        result = {}
        anonymous = getattr(obj, '_anonymous_', [])

        for spec in getattr(obj, '_fields_', []):
            key = spec[0]
            value = getattr(obj, key)

            # private fields don't encode
            if key.startswith('_'):
                continue

            if key in anonymous:
                result.update(ctype_to_ptype(value))
            else:
                result[key] = ctype_to_ptype(value)

        return result

    logger.warning(f'unmapped obj: {type(obj)} {obj}')


sock = None


def get_socket():
    global sock
    if not sock:
        sock = socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, 31)
        sock.bind((0, 0))
        sock.settimeout(5.0)
    return sock


class Request(object):
    reply_ctype = None
    request_ctype = None
    opcode = None

    # TODO: check with Ofer what exactly should be the alignment of data - looks like it works but probably on mistake
    class NetlinkMsg(ctypes.Structure):
        _fields_ = [
            ("header", types.struct__nlmsghdr),
            ("data", types.struct__nvmeib_nl_uk_comm_msg)
        ]

    @classmethod
    def create_reply_ctype(cls):
        assert cls.reply_ctype, "reply_ctype is not configured for this class"
        fields = [("header", types.struct__nlmsghdr),
                  ("data", types.struct__nvmeib_nl_uk_comm_msg),
                  ("reply", cls.reply_ctype)]
        return type(cls.__name__ + "Type", (ctypes.Structure,), {"_fields_": fields})

    @staticmethod
    def convert_struct_to_bytes(s):
        buffer = ctypes.create_string_buffer(ctypes.sizeof(s))
        ctypes.memmove(buffer, ctypes.addressof(s), ctypes.sizeof(s))
        return buffer.raw

    def __init__(self, **req_kwargs):
        # self.req_buf = req_buf
        self._response = None
        self._rest = None
        self.sock = get_socket()
        self.req_kwargs = req_kwargs

    def send(self, **req_kwargs):
        netlink_msg_header = types.struct__nlmsghdr()
        netlink_msg_header.nlmsg_type = 17
        netlink_msg_header.nlmsg_pid = os.getpid()
        netlink_msg_header.nlmsg_len = ctypes.sizeof(types.struct__nlmsghdr) + \
                                       ctypes.sizeof(types.struct__nvmeib_nl_uk_comm_msg)

        if self.request_ctype:
            # strings and ints
            def typesafe(v):
                if isinstance(v, str):
                    return bytes(v, 'utf-8')
                elif isinstance(v, float):
                    return int(v)
                else:
                    return v
            kwargs = {k: typesafe(v) for k, v in req_kwargs.items()}
            # for k, v in kwargs.items():
                # print(f'KEY: {k}: ({type(v)}) {v}')
            request = self.request_ctype(**kwargs)
        else:
            request = ""

        nvmesh_header = types.struct__nvmeib_nl_uk_comm_msg()
        assert self.opcode is not None, "opcode is not set"
        nvmesh_header.opcode = self.opcode
        nvmesh_header.process = 'I'  # Infra symbol
        nvmesh_header.len = ctypes.sizeof(types.struct__nvmeib_nl_uk_comm_msg)

        if request:
            nvmesh_header.len += ctypes.sizeof(request)
            msg = self.NetlinkMsg(header=netlink_msg_header, data=nvmesh_header)
            logger.debug("sending msg: " + str(ctype_to_ptype(msg)))
            logger.debug("sending request: " + str(ctype_to_ptype(request)))

            # request is located in a zero length array at the end of NetlinkMsg - easiest way to do it
            msg = self.convert_struct_to_bytes(msg) + self.convert_struct_to_bytes(request)
        else:
            msg = self.NetlinkMsg(header=netlink_msg_header, data=nvmesh_header)
            # logger.debug("sending msg: " + str(ctype_to_ptype(msg)))
        self.sock.send(msg)

    @property
    def response(self):
        if not self._response:
            res = self.sock.recv(4096)
            response = self.create_reply_ctype()()
            ctypes.memmove(ctypes.addressof(response), res, ctypes.sizeof(response))
            self._response = response
            # support _rest for zero length array
            self._rest = res[ctypes.sizeof(response):]
            logger.debug("got response:" + str(ctype_to_ptype(self._response)))
            assert not self._response.reply.base.error, "response code was {}".format(self._response.reply.base.error)
        return self._response, self._rest


class DrivesRequest(Request):
    reply_ctype = types.struct__nvmeib_get_disk_names_reply
    opcode = enums.uk_comm_opcode.csc_get_disk_names

    def perform(self):
        ret = self.get_drives_info()
        print(json.dumps(ret, sys.stdout, indent=2))

    def get_drives_info(self):
        self.send()
        reply, rest = self.response
        ret = []
        arr = ctypes.cast(rest, ctypes.POINTER(types.struct__nvmeib_short_disk_info))
        for i in range(reply.reply.n_disks):
            ret.append(arr[i])
        return ctype_to_ptype(ret)


class IORequest(Request):
    request_ctype = types.struct__nvmeib_io_to_disk
    reply_ctype = types.struct__nvmeib_io_to_disk_reply
    opcode = enums.uk_comm_opcode.csc_io_to_disk

    @staticmethod
    def get_aligned_buffer(length, alignment=4096):
        data_buf = ctypes.create_string_buffer(length + alignment)
        # data buf has to be 4k aligned
        data_buf_addr = ctypes.addressof(data_buf)
        aligned_addr = data_buf_addr if not data_buf_addr % alignment else \
            (data_buf_addr + alignment) - (data_buf_addr % alignment)
        # we return the original data buffer and aligned address inside it
        return data_buf, aligned_addr

    @staticmethod
    def _open_file(path, to_read=True):
        if not path or path == '-':
            return sys.stdin.buffer if to_read else sys.stdout.buffer
        return open(path, 'rb' if to_read else 'wb')

    def open_files(self, req_args, to_read=True):
        dfile = self._open_file(req_args["data"], to_read)
        mfile = dfile if req_args["data"] == req_args["md"] else self._open_file(req_args["md"], to_read)
        return dfile, mfile

class ReadRequest(IORequest):
    DriveInfo = None
    Stats = defaultdict(lambda: defaultdict(int))

    def __init__(self, return_data=False, **req_kwargs):
        # self.req_buf = req_buf
        self.return_data = return_data
        super().__init__(**req_kwargs)

    def perform(self):
        left_len_to_read = self.req_kwargs.pop('data_len')
        left_md_to_read = self.req_kwargs.pop('md_len')
        start_sector = self.req_kwargs.pop('start_sector')
        # as we treat data and md as path to files and not as pointers - pop them from req_kwargs
        files_dict = {"data": self.req_kwargs.pop("data", ""), "md": self.req_kwargs.pop("md", "")}
        dfile, mfile = self.open_files(files_dict, False)

        disk_id = self.req_kwargs['disk_id']

        all_data = bytes()
        all_md = bytes()

        if not ReadRequest.DriveInfo:
            self.Stats['DriveInfo']['op-count'] += 1
            ReadRequest.DriveInfo = DrivesRequest().get_drives_info()
        # print(json.dumps(drives, indent=2))
        block_size, md_size, max_request_size = None, None, None
        for drive in ReadRequest.DriveInfo:
            if drive['disk_id'] == disk_id:
                block_size = drive['hw_block_size'] if self.req_kwargs['is_hw'] else drive['sw_block_size']
                md_size = drive['hw_md_size'] if self.req_kwargs['is_hw'] else drive['sw_md_size']
                max_request_size = drive['max_request_size']
                break
        assert block_size is not None, "could not find drive info"
        self.Stats[disk_id]['block_size'] = block_size
        self.Stats[disk_id]['max_request_size'] = max_request_size

        data_buf, aligned_addr = self.get_aligned_buffer(left_len_to_read)
        if left_md_to_read:
            meta_buf = ctypes.create_string_buffer(left_md_to_read)
        CHAR_P = ctypes.POINTER(ctypes.c_char)

        data_cursor = aligned_addr
        md_cursor = ctypes.addressof(meta_buf) if left_md_to_read else 0
        while left_len_to_read:
            data_read_len = min(left_len_to_read, block_size * max_request_size)
            md_read_len = min(left_md_to_read, md_size * max_request_size)

            op_id = f'{os.getpid()}-{next(op_counter)}'
            logger.debug(f'read #{op_id}: {data_read_len}')
            time_started = datetime.now()
            self.send(data=ctypes.cast(data_cursor, CHAR_P), is_read=1,
                      md=ctypes.cast(md_cursor, CHAR_P),
                      data_len=data_read_len, md_len=md_read_len, start_sector=start_sector,
                      **self.req_kwargs)
            reply, rest = self.response
            data_cursor += data_read_len
            if md_cursor:
                md_cursor += md_read_len
            self._response = None
            self.Stats[disk_id]['bytes_read'] += data_read_len
            self.Stats[disk_id]['op-count'] += 1
            elapsed = (datetime.now() - time_started).total_seconds()
            logger.debug(f'read #{op_id}: {data_read_len}, elapsed: {elapsed}')
            self.Stats[disk_id]['elapsed'] += elapsed

            left_len_to_read -= data_read_len
            left_md_to_read -= md_read_len
            start_sector = start_sector + data_read_len / block_size

        buf_offset = aligned_addr - ctypes.addressof(data_buf)
        all_data = data_buf[buf_offset:buf_offset + (data_cursor-aligned_addr)]
        all_md = meta_buf.raw if left_md_to_read else None
        if self.return_data:
            return all_data, all_md
        if mfile and all_md:
            mfile.write(all_md)
        dfile.write(all_data)


class WriteRequest(IORequest):
    def __init__(self, inbuf=None, *args, **kwargs):
        self.inbuf = inbuf
        super().__init__(*args, **kwargs)

    def perform(self):
        # as we treat data and md as path to files and not as pointers - pop them from req_kwargs
        data_len = self.req_kwargs["data_len"] or -1
        meta_len = self.req_kwargs["md_len"] or -1

        if self.inbuf:
            assert data_len >= 0 and meta_len >= 0, "Must provide data_len and md_len"
            data_buf_to_write = self.inbuf[:data_len]
            meta_buf = self.inbuf[data_len:]
        else:
            files_dict = {"data": self.req_kwargs.pop("data", ""), "md": self.req_kwargs.pop("md", "")}
            dfile, mfile = self.open_files(files_dict)
            data_buf_to_write = dfile.read(data_len)
            meta_buf = mfile.read(meta_len)

        data_len = self.req_kwargs.pop("data_len", 0) or len(data_buf_to_write)
        data_buf, aligned_addr = self.get_aligned_buffer(data_len)
        # writing data to the appropriate place in buffer
        aligned_offset = aligned_addr - ctypes.addressof(data_buf)
        data_buf[aligned_offset:aligned_offset + len(data_buf_to_write)] = data_buf_to_write

        meta_len = self.req_kwargs["md_len"] or len(meta_buf)
        meta_buf = ctypes.create_string_buffer(meta_buf, meta_len)

        CHAR_P = ctypes.POINTER(ctypes.c_char)
        self.send(data=ctypes.cast(aligned_addr, CHAR_P), is_read=0,
                  md=ctypes.cast(ctypes.addressof(meta_buf), CHAR_P),
                  data_len=data_len,
                  **self.req_kwargs)
        return self.response


def try_int(o):
    # as we work with ctypes I don't see a reason will need more, guess there are some really edge cases
    try:
        return int(o)
    except Exception:
        return o

OPS = {
    'drives': (DrivesRequest, {}),
    'read': (ReadRequest, {'start_sector': 'start_lba', 'md_len': 'meta_len'}),
    'write': (WriteRequest, {'start_sector': 'start_lba', 'md_len': 'meta_len'})
}

def main():
    import argparse

    class StdoutVersionAction(argparse._VersionAction):
        """
        Default version action of python2 prints version to stderr, changed in later versions.
        """
        def __call__(self, parser, namespace, values, option_string=None):
            version = self.version
            if version is None:
                version = parser.version
            formatter = parser._get_formatter()
            formatter.add_text(version)
            parser._print_message(formatter.format_help(), sys.stdout)
            parser.exit()


    parser = argparse.ArgumentParser()
    parser.add_argument('-l', '--loglevel', default='ERROR')
    parser.add_argument('--version', action=StdoutVersionAction, version=__version__)
    subparsers = parser.add_subparsers(help='sub-command help')

    for name, (op, backward_comp_dict) in OPS.items():
        subparser = subparsers.add_parser(name)
        subparser.set_defaults(op_obj=op, pid=os.getpid())
        if not op.request_ctype:
            continue
        # TODO: support recursive fields as well - for now not needed
        for name, field in op.request_ctype.__dict__.items():
            # filter anonymous fields
            if 'unnamed' not in name and field.__class__.__name__ == 'CField':
                args = ('--{}'.format(name),)
                if name in backward_comp_dict:
                    args += ('--{}'.format(backward_comp_dict[name]),)
                subparser.add_argument(*args, help=str(field), type=try_int)

    args = vars(parser.parse_args())
    op = args.pop("op_obj")

    handler = logging.StreamHandler(sys.stderr)
    logger.addHandler(handler)
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)
    logger.setLevel(args.pop("loglevel"))

    req_kwargs = {k: v for k, v in args.items() if v is not None}
    op(**req_kwargs).perform()

if __name__ == '__main__':
    main()
