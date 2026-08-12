# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import csv
from io import StringIO
import struct
from typing import Any, Dict, List, Tuple
import uuid
from xlro.core.entities.volume import Volume
from xlro.tools.nvmesh_diag.util import DiagModule, MsgLvl
from xlro.core.util.lba import SwLBA
from xlro.core.entities import Target
from xlro.core.entities.drive import DriveStatus

class BlockDevice(DiagModule):
    description = "Block Device"
    _MAX_CACHE_BLOCKS = 10
    _MAX_PARSED_GPT_PARTITIONS = 3

    def discover(self):
        self.diag_info.devices = []

        out, _, _ = self.run_cmd("systemctl is-active nvmeshtarget.service", is_sudo=False, print_err=False)
        if out.strip() != "active":
            self.skip("NVMesh target service is not running. Skipping block device check.")

        target = Target.instance(name=self.nodename)

        self.diag_info.devices = [d for d in target.drives if getattr(d, 'status', '') == DriveStatus.OK] # We check only OK devices

        self.add_message(f'Found {len(self.diag_info.devices)} Ok devices')

        non_ok_devices = [d for d in target.drives if getattr(d, 'status', '') != DriveStatus.OK]

        if non_ok_devices:
            self.add_message(f'Found {len(non_ok_devices)} non-Ok devices:', MsgLvl.WARNING)
            for device in non_ok_devices:
                self.add_message(f'{device.name}: {getattr(device, "status", "NO STATUS")}', MsgLvl.WARNING)

        # Parse MBR, GPT header, and GPT partitions for each device
        self.add_message(f'Parsing MBR, GPT header, and GPT partitions for each device')
        self.diag_info.mbr_info = {} # {device: {mbr_info}}
        self.diag_info.gpt_info = {} # {device: {gpt_info}}
        self.diag_info.gpt_partitions = {} # {device: [{gpt_partition_info}]}
        for device in self.diag_info.devices:
            # Initialize for each device
            self.diag_info.current_device = device
            self.diag_info.cache_data = bytes() # Per device

            self.diag_info.mbr_info[device.name] = self.parse_mbr(self.read_disk_block(0))
            self.diag_info.gpt_info[device.name] = self.parse_gpt_header(self.read_disk_block(1))
            self.diag_info.gpt_partitions[device.name] = self.parse_gpt_partitions(self.diag_info.gpt_info[device.name])

            self.add_message(f'Successfully parsed MBR, GPT header, and GPT partitions for device: {device.name}')

    def validate(self):
        # Validate that the MBR and GPT header meet expected values
        passed_device_count = 0
        for device in self.diag_info.devices:
            self.add_message(f'Validating MBR and GPT header for device: {device.name}')
            self.diag_info.current_device = device

            mbr_passed = self.validate_mbr()
            self.add_message(f'MBR validated for device: {device}', MsgLvl.SUCCESS if mbr_passed else MsgLvl.ERROR)

            gpt_header_passed = self.validate_gpt_header()
            self.add_message(f'GPT header validated for device: {device}', MsgLvl.SUCCESS if gpt_header_passed else MsgLvl.ERROR)

            gpt_partitions_passed = self.validate_gpt_partitions()
            self.add_message(f'GPT partitions validated for device: {device}', MsgLvl.SUCCESS if gpt_partitions_passed else MsgLvl.ERROR)

            if mbr_passed and gpt_header_passed and gpt_partitions_passed:
                passed_device_count += 1

        total_device_count = len(self.diag_info.devices)
        self.add_message(f'RESULT | TOTAL={total_device_count} | PASS={passed_device_count} | FAIL={total_device_count - passed_device_count}', MsgLvl.SUCCESS if passed_device_count == total_device_count else MsgLvl.ERROR)

    def details(self):
        for device in self.diag_info.devices:
            self.add_message(f'Detailed MBR and GPT information for device: {device.name}')
            self.diag_info.current_device = device
            self.mbr_details(self.diag_info.mbr_info[device.name], self.expectations['mbr'])
            self.gpt_header_details(self.diag_info.gpt_info[device.name], self.expectations['gpt'])
            self.gpt_partitions_details(self.diag_info.gpt_partitions[device.name], self.expectations['gpt_partitions'])
            self.secondary_gpt_details() # We don't validate secondary GPT, so we parse and print it in this function in details phase only.

    def read_disk_block(self, block_idx: int) -> bytes:
        """Read a block from disk device using direct I/O to bypass potentially stale OS buffer"""
        """If read fails, return a block of zeros"""
        """As optimization, we cache the last read blocks to avoid reading the same blocks multiple times."""
        device = self.diag_info.current_device
        block_size = Volume.BLOCK_SIZE # Page size
        # Check if the requested block is already in the cache
        if (block_idx+1)*block_size <= len(self.diag_info.cache_data):
            start_pos = block_idx * block_size
            return self.diag_info.cache_data[start_pos:start_pos + block_size]

        try:
            # If the block is beyond our current cache, we need to read all blocks up to this one to maintain contiguity
            total_blocks_needed = block_idx+1

            if total_blocks_needed > self._MAX_CACHE_BLOCKS:
                # If reading all blocks would exceed our limit, just read this block
                # f"sudo dd if={device} bs={block_size} count=1 skip={block_idx} iflag=direct status=none 2>/dev/null | xxd -p | tr -d '\n'"
                return b"".join([x.data for x in list(device.read_pages(SwLBA(block_idx),count=1))])
            else:
                # Start reading from where our cache ends
                bytes_to_start = len(self.diag_info.cache_data)

                # Calculate which blocks to read
                start_block = bytes_to_start // block_size
                blocks_to_read = total_blocks_needed - start_block

                # Read all needed blocks with a single dd command
                # f"sudo dd if={device} bs={block_size} count={blocks_to_read} skip={start_block} iflag=direct status=none 2>/dev/null | xxd -p | tr -d '\n'"
                block_data = b"".join([x.data for x in list(device.read_pages(SwLBA(start_block),count=blocks_to_read))])
                self.diag_info.cache_data += block_data

                # Extract and return the requested block
                start_pos = block_idx * block_size
                return self.diag_info.cache_data[start_pos:start_pos + block_size]

        except Exception as e:
            self.add_message(f"Failed to read block {block_idx} from {self.diag_info.current_device.name}: {str(e)}", MsgLvl.ERROR)
            return bytes(block_size)

    @staticmethod
    def _find_non_zero_sections(data: bytes) -> List[Tuple[int, int]]:
        """Find contiguous non-zero sections in data"""
        non_zero_sections = []
        start_idx = None

        for i, byte in enumerate(data):
            if byte != 0 and start_idx is None:
                start_idx = i
            elif byte == 0 and start_idx is not None:
                non_zero_sections.append((start_idx, i - 1))
                start_idx = None

        # Handle case where non-zero section extends to the end
        if start_idx is not None:
            non_zero_sections.append((start_idx, len(data) - 1))

        return non_zero_sections

    """
    struct nvmeibt_mbr_partition_record {
        char boot_indicator;
            // Offset: 0     | Size: 1 byte  | Bootable flag (0x80 = bootable)
        char starting_chs[3];
            // Offset: 1-3   | Size: 3 bytes | CHS address of first block
        char os_type;
            // Offset: 4     | Size: 1 byte  | Partition type identifier
        char ending_chs[3];
            // Offset: 5-7   | Size: 3 bytes | CHS address of last block
        int  pba_s;
            // Offset: 8-11  | Size: 4 bytes | PBA of first sector
        int  n_pblk;
            // Offset: 12-15 | Size: 4 bytes | Number of blocks in partition
    };
    // Total size: 16 bytes per partition record
    """
    def _parse_mbr_partition(self, data: bytes, index: int, offset: int) -> Dict[str, Any]:
        """Parse a single MBR partition entry"""
        partition_data = data[offset:offset+16]

        boot_indicator = partition_data[0]
        os_type = partition_data[4]
        pba_s = struct.unpack("<I", partition_data[8:12])[0]
        n_pblk = struct.unpack("<I", partition_data[12:16])[0]
        is_empty = pba_s == 0 and n_pblk == 0

        partition_info = {
            "index": index + 1,
            "offset": offset,
            "boot_indicator": boot_indicator,
            "is_bootable": boot_indicator == 0x80,
            "os_type": os_type,
            "starting_chs": partition_data[1:4],
            "ending_chs": partition_data[5:8],
            "pba_s": pba_s,
            "n_pblk": n_pblk,
            "is_empty": is_empty
        }

        if not is_empty:
            block_size = self.diag_info.current_device.blockSize
            partition_info["size_gb"] = n_pblk * block_size / (1024*1024*1024)

        return partition_info

    """
    struct nvmeibt_disk_mbr {
        char boot_code[440];
            // Offset: 0-439   | Size: 440 bytes | Boot code/loader
        int disk_signature;
            // Offset: 440-443 | Size: 4 bytes   | Unique disk ID
        short unknown;
            // Offset: 444-445 | Size: 2 bytes   | Usually zeros
        struct nvmeibt_mbr_partition_record partitions[4];
            // Offset: 446-510 | Size: 64 bytes  | 4 partition entries (16 bytes each)
        short mbr_signature;
            // Offset: 510-511 | Size: 2 bytes   | Boot signature (0xAA55)
    };
    // Total size: 512 bytes (standard MBR size)
    """
    def parse_mbr(self, data: bytes) -> Dict[str, Any]:
        """Parse MBR data and return structured information"""
        try:
            boot_code = data[0:440]
            is_boot_code_zeros = all(b == 0 for b in boot_code)

            # Parse non-zero sections for detailed reporting
            non_zero_sections = [] if is_boot_code_zeros else BlockDevice._find_non_zero_sections(boot_code)

            # Parse partition entries
            partitions = [self._parse_mbr_partition(data, i, 446 + (i * 16)) for i in range(4)]

            return {
                "boot_code": boot_code,
                "is_boot_code_zeros": is_boot_code_zeros,
                "non_zero_sections": non_zero_sections,
                "disk_signature": struct.unpack("<I", data[440:444])[0],
                "unknown": struct.unpack("<H", data[444:446])[0],
                "mbr_signature": struct.unpack("<H", data[510:512])[0],
                "partitions": partitions,
                "non_empty_partitions": sum(1 for p in partitions if not p["is_empty"]),
                "has_gpt_part": any(p["os_type"] == 0xEE for p in partitions),
                "raw_data": data
            }
        except Exception as e:
            self.add_message(f"Failed to parse MBR: {str(e)}", MsgLvl.ERROR)
            return {}

    """
    struct nvmeibt_disk_gpt_header {
        uint64_t gpt_signature;
            // Offset: 0-7   | Size: 8 bytes  | Signature ("EFI PART")
        int revision;
            // Offset: 8-11  | Size: 4 bytes  | Revision number
        int header_size;
            // Offset: 12-15 | Size: 4 bytes  | Header size in bytes
        int header_crc32;
            // Offset: 16-19 | Size: 4 bytes  | CRC32 of header
        int reserved;
            // Offset: 20-23 | Size: 4 bytes  | Reserved
        uint64_t my_pba;
            // Offset: 24-31 | Size: 8 bytes  | PBA of this header
        uint64_t alternate_pba;
            // Offset: 32-39 | Size: 8 bytes  | PBA of backup header
        uint64_t first_usable_pba;
            // Offset: 40-47 | Size: 8 bytes  | First usable PBA for partitions
        uint64_t last_usable_pba;
            // Offset: 48-55 | Size: 8 bytes  | Last usable PBA for partitions
        union nvmeib_uuid disk_obj_uuid;
            // Offset: 56-71 | Size: 16 bytes | Disk GUID
        uint64_t partition_entry_pba;
            // Offset: 72-79 | Size: 8 bytes  | Starting PBA of partition entries
        int n_partition_entries;
            // Offset: 80-83 | Size: 4 bytes  | Number of partition entries
        int size_of_partition_entry;
            // Offset: 84-87 | Size: 4 bytes  | Size of each partition entry
        uint32_t partition_entry_array_crc32;
            // Offset: 88-91 | Size: 4 bytes  | CRC32 of partition array
    };
    // Total size: 92 bytes (standard GPT header size)
    """
    def parse_gpt_header(self, data: bytes) -> Dict[str, Any]:
        """Parse GPT header data and return structured information"""
        try:
            return {
                # Header identification
                "gpt_signature": struct.unpack("<Q", data[0:8])[0],
                "sig_ascii": data[0:8].decode('ascii', errors='replace'),
                "revision": struct.unpack("<I", data[8:12])[0],
                "header_size": struct.unpack("<i", data[12:16])[0],
                "header_crc32": struct.unpack("<I", data[16:20])[0],
                "reserved": struct.unpack("<I", data[20:24])[0],

                # Disk layout information
                "my_pba": struct.unpack("<Q", data[24:32])[0],
                "alternate_pba": struct.unpack("<Q", data[32:40])[0],
                "first_usable_pba": struct.unpack("<Q", data[40:48])[0],
                "last_usable_pba": struct.unpack("<Q", data[48:56])[0],
                "disk_uuid": uuid.UUID(bytes_le=data[56:72]),

                # Partition table information
                "partition_entry_pba": struct.unpack("<Q", data[72:80])[0],
                "n_partition_entries": struct.unpack("<i", data[80:84])[0],
                "size_of_partition_entry": struct.unpack("<i", data[84:88])[0],
                "partition_entry_array_crc32": struct.unpack("<I", data[88:92])[0],

                # Raw data for later access
                "raw_data": data
            }
        except Exception as e:
            self.add_message(f"Failed to parse GPT header: {str(e)}", MsgLvl.ERROR)
            return {}

    def _parse_utf16_name(self, name_bytes: bytes) -> str:
        """Parse UTF-16LE name from bytes with null termination"""
        try:
            # Find null terminator
            null_pos = 0
            while null_pos < len(name_bytes) and (name_bytes[null_pos] != 0 or name_bytes[null_pos + 1] != 0):
                null_pos += 2

            name = name_bytes[0:null_pos].decode('utf-16le')
            return name
        except Exception:
            self.add_message(f"Invalid UTF-16 name", MsgLvl.ERROR)
            return "(Invalid UTF-16)"

    """
    struct nvmeibt_disk_gpt_partition_entry
    {
      union nvmeib_uuid   type_guid;
            // Offset: 0-15   | Size: 16 bytes | Type GUID
      union nvmeib_uuid   unique_guid;
            // Offset: 16-31  | Size: 16 bytes | Partition GUID
      uint64_t            pba_s;
            // Offset: 32-39  | Size: 8 bytes  | Starting PBA
      uint64_t            pba_e;
            // Offset: 40-47  | Size: 8 bytes  | Ending PBA
      uint64_t            attributes;
            // Offset: 48-55  | Size: 8 bytes  | Attributes
      char16_t            partition_name[36];
            // Offset: 56-127 | Size: 72 bytes | Partition name
    };
    // Total size: 128 bytes
    """
    def parse_gpt_partition(self, gpt_info: Dict[str, Any], partition_idx: int) -> Dict[str, Any]:
        """Parse a specific GPT partition and return structured information"""
        if not gpt_info:
            return {}

        if partition_idx < 0 or partition_idx >= gpt_info["n_partition_entries"]:
            self.add_message(f"Partition index {partition_idx} out of range", MsgLvl.ERROR)
            return {}

        # Calculate which block contains this partition entry
        block_size = self.diag_info.current_device.blockSize
        entry_size = gpt_info["size_of_partition_entry"]
        entries_per_block = block_size // entry_size

        block_idx = gpt_info["partition_entry_pba"] + (partition_idx // entries_per_block)
        idx_in_block = partition_idx % entries_per_block

        try:
            # Read the block containing this partition entry
            block_data = self.read_disk_block(block_idx)

            # Extract entry data
            entry_offset = (idx_in_block * entry_size) % block_size
            entry_data = block_data[entry_offset:entry_offset + entry_size]

            # Parse entry from the raw data
            type_guid = uuid.UUID(bytes_le=entry_data[0:16])
            pba_s = struct.unpack("<Q", entry_data[32:40])[0]
            pba_e = struct.unpack("<Q", entry_data[40:48])[0]

            # Check if this is an empty partition
            is_empty = (type_guid == uuid.UUID('00000000-0000-0000-0000-000000000000') and
                      pba_s == 0 and pba_e == 0)

            return {
                "partition_num": partition_idx+1,
                "type_guid": str(type_guid),
                "unique_guid": str(uuid.UUID(bytes_le=entry_data[16:32])),
                "name": self._parse_utf16_name(entry_data[56:128]),
                "pba_s": pba_s,
                "pba_e": pba_e,
                "attributes": struct.unpack("<Q", entry_data[48:56])[0],
                "is_empty": is_empty,
                "size_gb": (pba_e - pba_s + 1) * block_size / (1024*1024*1024) if not is_empty else 0,
                "raw_data": entry_data
            }
        except Exception as e:
            self.add_message(f"Error reading partition entry: {str(e)}", MsgLvl.ERROR)
            return {}

    def parse_gpt_partitions(self, gpt_info: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Parse GPT partition entries and return structured information"""
        try:
            # We only care about the first _MAX_PARSED_GPT_PARTITIONS partitions
            max_entries = gpt_info["n_partition_entries"]
            partition_ranges = range(0,min(self._MAX_PARSED_GPT_PARTITIONS,max_entries))

            partition_results = []

            # Check each partition
            for partition_idx in partition_ranges:
                partition_result = self.parse_gpt_partition(gpt_info, partition_idx)
                partition_results.append(partition_result)

            return partition_results
        except Exception as e:
            self.add_message(f"Failed to parse GPT partitions: {str(e)}", MsgLvl.ERROR)
            return []

    def validate_mbr(self):
        expected = self.expectations['mbr'] # Refer to expectations.yaml for the expected values
        name = self.diag_info.current_device.name
        mbr_info = self.diag_info.mbr_info[name]
        is_passed = True

        for field, expected_value in expected.items():
            if mbr_info[field] != expected_value:
                self.add_message(f"{field} for device {name} is not {expected_value}", MsgLvl.ERROR)
                is_passed = False

        return is_passed

    def validate_gpt_header(self):
        expected = self.expectations['gpt'] # Refer to expectations.yaml for the expected values
        name = self.diag_info.current_device.name
        gpt_info = self.diag_info.gpt_info[name]
        is_passed = True

        for field, expected_value in expected.items():
            if gpt_info[field] != expected_value:
                self.add_message(f"{field} for device {name} is not {expected_value}", MsgLvl.ERROR)
                is_passed = False

        return is_passed

    def validate_gpt_partitions(self):
        expected = self.expectations['gpt_partitions'] # Refer to expectations.yaml for the expected values
        name = self.diag_info.current_device.name
        gpt_partitions = self.diag_info.gpt_partitions[name]

        # For simplicity we only validate the first metadata partition; in cases when the drive doesn't support EC, there will be just metadata partition and no journal and serjio partitions.
        if len(gpt_partitions) == 0:
            self.add_message(f"No GPT partitions found for device {name}", MsgLvl.ERROR)
            return False

        is_passed = True
        for partition_idx in range(0, self._MAX_PARSED_GPT_PARTITIONS):
            partition = gpt_partitions[partition_idx]
            expected_partition = expected[partition_idx+1]
            if expected_partition["optional"]:
                continue # We only validate the first metadata partition
            if partition["is_empty"]:
                self.add_message(f"Partition {partition_idx+1} for device {name} is empty", MsgLvl.ERROR)
                is_passed = False

            for field, expected_value in expected_partition.items():
                if field == "optional":
                    continue # "optional" isn't a real field of GPT partition

                if partition[field] != expected_value:
                    self.add_message(f"{field} for device {name} Partition {partition_idx+1} is not {expected_value}", MsgLvl.ERROR)
                    is_passed = False

        return is_passed

    def print_check_result(self, actual_value: Any, expected_value: Any, field_name: str, indent: str="   "):
        """Print a check result with a status symbol."""
        status = "  " if expected_value is None else ("✅" if actual_value == expected_value else "❌")
        self.add_message(f"{indent}{status} {field_name}: {actual_value}")

        if expected_value is not None and actual_value != expected_value:
            self.add_message(f"{indent}     Expected: {expected_value}")

    def format_bytes_as_hex(self, data: bytes, start: int, length: int) -> str:
        """Format bytes as hex string with spaces"""
        bytes_slice = data[start:start+length]
        return ' '.join(f'{b:02x}' for b in bytes_slice)

    def mbr_details(self, mbr_info: Dict[str, Any], expected_value: Dict[str, Any]):
        self.add_message(f"=== MBR Header of {self.diag_info.current_device.name} ===")
        if mbr_info["is_boot_code_zeros"]:
            self.print_check_result("All zeros", "All zeros", "Boot code")
        else:
            self.print_check_result("Contains non-zero boot code. ", "All zeros", "Boot code")
            for start, end in mbr_info["non_zero_sections"]:
                length = end - start + 1
                if length <= 32:  # Short section, print it all
                    self.add_message(f"     Offset 0x{start:04x}-0x{end:04x} ({length} bytes): {self.format_bytes_as_hex(mbr_info['boot_code'], start, length)}")
                else:  # Longer section, print beginning and end
                    self.add_message(f"     Offset 0x{start:04x}-0x{end:04x} ({length} bytes):")
                    self.add_message(f"     Start: {self.format_bytes_as_hex(mbr_info['boot_code'], start, 16)}")
                    self.add_message(f"     End:   {self.format_bytes_as_hex(mbr_info['boot_code'], end-15, 16)}")
        self.print_check_result(f"0x{mbr_info['disk_signature']:08x}", f"0x{expected_value['disk_signature']:08x}", "Disk signature")
        self.print_check_result(f"0x{mbr_info['unknown']:04x}", f"0x{expected_value['unknown']:04x}", "Unknown")
        self.print_check_result(f"0x{mbr_info['mbr_signature']:04x}", f"0x{expected_value['mbr_signature']:04x}", "MBR signature")
        self.add_message(f"MBR partitions:")
        for partition in mbr_info["partitions"]:
            self.add_message(f"   Partition {partition['index']}:")
            self.add_message(f"     Offset: 0x{partition['offset']:04x}")
            self.add_message(f"     Boot indicator: 0x{partition['boot_indicator']:02x}")
            self.add_message(f"     OS type: 0x{partition['os_type']:02x}")
            self.add_message(f"     Starting CHS: {self.format_bytes_as_hex(mbr_info['raw_data'], partition['offset'], 3)}")
            self.add_message(f"     Ending CHS: {self.format_bytes_as_hex(mbr_info['raw_data'], partition['offset']+5, 3)}")
            self.add_message(f"     Starting PBA: {partition['pba_s']}")
            self.add_message(f"     Number of blocks: {partition['n_pblk']}")
            if partition["is_empty"]:
                self.add_message(f"     Status: Empty partition entry")
            else:
                self.add_message(f"     Size: {partition['size_gb']:.2f} GB")


    def gpt_header_details(self, gpt_info: Dict[str, Any], expected_value: Dict[str, Any]):
        self.add_message(f"=== GPT Header of {self.diag_info.current_device.name} ===")
        self.print_check_result(gpt_info['sig_ascii'], expected_value['sig_ascii'], "GPT signature")
        self.print_check_result(f"0x{gpt_info['revision']:08x}", f"0x{expected_value['revision']:08x}", "GPT revision")
        self.print_check_result(gpt_info['header_size'], expected_value['header_size'], "GPT header size")
        self.print_check_result(f"0x{gpt_info['header_crc32']:08x}", None, "GPT header CRC32")
        self.print_check_result(f"0x{gpt_info['reserved']:08x}", f"0x{expected_value['reserved']:08x}", "Reserved")
        self.print_check_result(gpt_info['my_pba'], None, "Current PBA (this header)")
        self.print_check_result(gpt_info['alternate_pba'], None, "Alternate PBA (backup header)")
        self.print_check_result(gpt_info['first_usable_pba'], None, "First usable PBA for partitions")
        self.print_check_result(gpt_info['last_usable_pba'], None, "Last usable PBA for partitions")
        self.print_check_result(gpt_info['disk_uuid'], None, "Disk UUID")
        self.print_check_result(gpt_info['partition_entry_pba'], None, "Starting PBA of partition entries")
        self.print_check_result(gpt_info['n_partition_entries'], expected_value['n_partition_entries'], "Number of partition entries")
        self.print_check_result(gpt_info['size_of_partition_entry'], expected_value['size_of_partition_entry'], "Size of partition entry")
        self.print_check_result(f"0x{gpt_info['partition_entry_array_crc32']:08x}", None, "Partition entry array CRC32")

    def gpt_partitions_details(self, gpt_partitions: List[Dict[str, Any]], expected_partitions: Dict[str, Any]):
        self.add_message("=== Parsed GPT Partitions ===")
        for partition in gpt_partitions:
            partition_num = partition['partition_num']
            expected_value = expected_partitions[partition_num]
            if partition["is_empty"]:
                self.print_check_result("Empty (unused entry)", expected_value["name"], f"Partition {partition_num}") # No matter if the partition is optional or not, we print the expected partition name for empty ones
            else:
                self.print_check_result("", None, f"Partition {partition_num}") # Partition number, not for checking. Using print_check_result just to ensure indentation.
                self.print_check_result(partition['type_guid'], expected_value["type_guid"], "Type GUID","     ")
                self.print_check_result(partition['unique_guid'], None, "Unique GUID","     ")
                self.print_check_result(partition['name'], expected_value["name"], "Name","     ")
                self.print_check_result(partition['pba_s'], None, "PBA start","     ")
                self.print_check_result(partition['pba_e'], None, "PBA end","     ")
                self.print_check_result(partition['attributes'], None, "Attributes","     ")
                self.print_check_result(f"{partition['size_gb']:.2f} GB", None, "Size","     ")

    def secondary_gpt_details(self):
        self.add_message("======== Secondary GPT ========")
        backup_block_idx = self.diag_info.gpt_info[self.diag_info.current_device.name]['alternate_pba']
        secondary_gpt_info = self.parse_gpt_header(self.read_disk_block(backup_block_idx))
        self.gpt_header_details(secondary_gpt_info, self.expectations['gpt'])

        secondary_gpt_partitions = self.parse_gpt_partitions(secondary_gpt_info)
        self.gpt_partitions_details(secondary_gpt_partitions, self.expectations['gpt_partitions'])
