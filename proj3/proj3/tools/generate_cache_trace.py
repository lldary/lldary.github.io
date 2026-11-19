#!/usr/bin/env python3
"""Generate deterministic instruction traces for the course's 64-byte ChampSim format.

These are synthetic microbenchmark instruction streams, not traces collected from gemm.
Each lane follows independent pointer rings; each load is followed by checksum and
arithmetic work. A direct jump connects each unrolled body to the next body.
"""
import argparse
import hashlib
import json
import math
import random
import struct
import subprocess
from pathlib import Path

RECORD = struct.Struct('<QBB2B4B2Q4Q')
assert RECORD.size == 64
DEFAULT_LANES = 2


def record(ip, sources=(), destination=0, address=0, branch=False):
    return RECORD.pack(ip, int(branch), int(branch), destination, 0,
                       *(tuple(sources) + (0,) * (4 - len(sources))),
                       0, 0, address, 0, 0, 0)


def rings(base, count, stride, rng, lanes):
    assert count % lanes == 0
    nodes = list(range(count))
    rng.shuffle(nodes)
    length = count // lanes
    return [[base + stride * n for n in nodes[lane * length:(lane + 1) * length]]
            for lane in range(lanes)]


def body(output, pc, addresses, pointer_register):
    for lane, address in enumerate(addresses):
        ptr, checksum, work1, work2 = pointer_register + lane, 48 + lane, 64 + lane, 80 + lane
        ip = pc + lane * 16
        output.extend(record(ip, (ptr,), ptr, address))
        output.extend(record(ip + 4, (ptr, checksum), checksum))
        output.extend(record(ip + 8, (work1,), work1))
        output.extend(record(ip + 12, (work2,), work2))
    output.extend(record(pc + len(addresses) * 16, destination=26, branch=True))


def make_pattern(kind, seed, hot_kib, warm_kib, conflict_nodes, lanes=DEFAULT_LANES):
    rng = random.Random(seed)
    output = bytearray()
    if kind == 'capacity':
        hot = rings(0x10000000, hot_kib * 16, 64, rng, lanes)
        warm = rings(0x20000000, warm_kib * 16, 64, rng, lanes)
        hot_length, warm_length = len(hot[0]), len(warm[0])
        period = math.lcm(hot_length // math.gcd(3, hot_length), warm_length)
        hot_index = warm_index = 0
        for _ in range(period):
            for group in range(4):
                cold = group == 3
                pool, index, register = (warm, warm_index, 40) if cold else (hot, hot_index, 32)
                body(output, 0x400000 + group * 0x100, [r[index % len(r)] for r in pool], register)
                if cold:
                    warm_index += 1
                else:
                    hot_index += 1
        details = dict(hot_kib=hot_kib, warm_kib=warm_kib, hot_load_fraction=0.75,
                       distinct_data_lines=(hot_kib + warm_kib) * 16)
    else:
        pool = rings(0x30000000, conflict_nodes, 4096, rng, lanes)
        for index in range(len(pool[0])):
            body(output, 0x400000, [r[index] for r in pool], 32)
        details = dict(conflict_nodes=conflict_nodes, address_stride=4096,
                       distinct_data_lines=conflict_nodes)
    return bytes(output), details


def generate(args):
    if args.instructions < 1:
        raise ValueError('instruction count must be positive')
    if args.hot_kib <= 0 or args.warm_kib <= 0:
        raise ValueError('working-set sizes must be positive')
    if args.lanes < 1 or args.lanes > 8:
        raise ValueError('lanes must be between one and eight')
    if (args.hot_kib * 16) % args.lanes or (args.warm_kib * 16) % args.lanes:
        raise ValueError('working-set line counts must be divisible by lanes')
    if args.conflict_nodes < args.lanes or args.conflict_nodes % args.lanes:
        raise ValueError('conflict nodes must be a positive multiple of lanes')
    pattern, details = make_pattern(args.kind, args.seed, args.hot_kib, args.warm_kib, args.conflict_nodes, args.lanes)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    remaining = args.instructions * RECORD.size
    with args.output.open('wb') as compressed:
        process = subprocess.Popen(['xz', '-T2', '-1', '-c'], stdin=subprocess.PIPE, stdout=compressed)
        try:
            while remaining:
                block = pattern[:min(remaining, len(pattern))]
                process.stdin.write(block)
                remaining -= len(block)
            process.stdin.close()
            if process.wait() != 0:
                raise RuntimeError('xz failed')
        except BaseException:
            process.kill()
            process.wait()
            raise
    metadata = dict(kind=args.kind, seed=args.seed, instructions=args.instructions,
                    record_bytes=RECORD.size, format='little-endian course input_instr',
                    lanes=args.lanes, instructions_per_body=4 * args.lanes + 1, loads_per_body=args.lanes,
                    pattern_instructions=len(pattern) // RECORD.size,
                    compressed_sha256=hashlib.sha256(args.output.read_bytes()).hexdigest(), **details)
    args.output.with_suffix(args.output.suffix + '.json').write_text(json.dumps(metadata, indent=2) + '\n')
    print(json.dumps(metadata), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('kind', choices=['capacity', 'conflict'])
    parser.add_argument('output', type=Path)
    parser.add_argument('--instructions', type=int, default=124000000)
    parser.add_argument('--lanes', type=int, default=DEFAULT_LANES)
    parser.add_argument('--seed', type=int, default=20261025)
    parser.add_argument('--hot-kib', type=int, default=96)
    parser.add_argument('--warm-kib', type=int, default=1024)
    parser.add_argument('--conflict-nodes', type=int, default=16)
    generate(parser.parse_args())


if __name__ == '__main__':
    main()
