#!/usr/bin/env python3
"""Capture a complete Part 4 ROI; append unmeasured ChampSim lookahead records.

This is trace plumbing, not a metric-calculation or performance-scoring script.
Requires the course Pin tracer with -start_symbol / -stop_symbol support.
"""
import argparse
import hashlib
import json
import lzma
import os
from pathlib import Path
import re
import signal
import struct
import subprocess
import sys
import tempfile
import threading

PADDING = 8192


def sha256(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def stop(process):
    if process is None or process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()
    except ProcessLookupError:
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pin-root', type=Path, required=True)
    parser.add_argument('--tracer', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--force', action='store_true', help='Replace a previous capture only after success')
    parser.add_argument('--max-instructions', type=int, default=2_000_000_000,
                        help='Safety limit; reaching it rejects the capture')
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    if not command or args.max_instructions <= 0:
        parser.error('Supply a program after -- and a positive instruction limit')
    output = args.output.resolve()
    metadata = Path(str(output) + '.json')
    log_path = Path(str(output) + '.capture.log')
    pin, tracer = args.pin_root.resolve() / 'pin', args.tracer.resolve()
    binary = Path(command[0]).resolve()
    if not str(output).endswith('.xz'):
        parser.error('--output must end in .xz')
    for path in (pin, tracer, binary):
        if not path.is_file():
            parser.error('Missing file: ' + str(path))
    if not os.access(pin, os.X_OK) or not os.access(binary, os.X_OK):
        parser.error('Pin and the program must be executable')
    if any(path.exists() for path in (output, metadata, log_path)) and not args.force:
        parser.error('Capture files exist; use another name or --force')
    command[0] = str(binary)
    output.parent.mkdir(parents=True, exist_ok=True)
    binary_hash = sha256(binary)
    process = None
    try:
        with tempfile.TemporaryDirectory(prefix='.part4-capture-', dir=output.parent) as temp:
            folder = Path(temp)
            fifo, compressed = folder / 'input.fifo', folder / 'trace.xz'
            os.mkfifo(fifo)
            state = {'bytes': 0, 'error': None}

            def compress():
                try:
                    with fifo.open('rb') as source, lzma.open(compressed, 'wb', preset=0) as target:
                        while True:
                            block = source.read(1024 * 1024)
                            if not block:
                                break
                            target.write(block)
                            state['bytes'] += len(block)
                        # These records provide CPU lookahead after the complete ROI.
                        # They must not be included in --simulation-instructions.
                        target.write(struct.pack('<Q56x', 0x70000000) * PADDING)
                except BaseException as error:
                    state['error'] = error

            worker = threading.Thread(target=compress, daemon=True)
            worker.start()
            pin_command = [str(pin), '-t', str(tracer), '-o', str(fifo),
                           '-start_symbol', 'roi_begin', '-stop_symbol', 'roi_end',
                           '-t', str(args.max_instructions), '--'] + command
            print('Capturing the complete compute function...', file=sys.stderr, flush=True)
            try:
                with log_path.open('w') as log:
                    process = subprocess.Popen(pin_command, stdout=log, stderr=subprocess.STDOUT,
                                               start_new_session=True)
                    status = process.wait()
            finally:
                stop(process)
                # Release a FIFO reader if Pin failed before opening the output.
                try:
                    fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
                    os.close(fd)
                except OSError:
                    pass
                worker.join(timeout=10)
            if worker.is_alive() or state['error'] is not None:
                raise RuntimeError('Trace compression failed: ' + str(state['error']))
            if status:
                raise RuntimeError('Program/Pin failed; see ' + str(log_path))
            log = log_path.read_text(errors='replace')
            stop_marker = re.search(r'Stopped tracing .* after (\d+) instructions', log)
            if ('Started tracing' not in log or stop_marker is None
                    or 'Reached instruction limit' in log):
                raise RuntimeError('Missing or truncated complete ROI; see ' + str(log_path))
            count, tail = divmod(state['bytes'], 64)
            if tail or count <= 0 or count != int(stop_marker[1]):
                raise RuntimeError('Trace length does not match the stopped ROI')
            records = []
            for line in log.splitlines():
                try:
                    value = json.loads(line)
                except ValueError:
                    continue
                if isinstance(value, dict) and value.get('verified') is True:
                    records.append(value)
            if len(records) != 1:
                raise RuntimeError('The fixed driver did not report successful full-output verification')
            if sha256(binary) != binary_hash:
                raise RuntimeError('The program changed during capture; retry after building finishes')
            details = {'format': 'course-champsim-input_instr-64', 'version': 1,
                       'roi_instructions': count, 'padding_instructions': PADDING,
                       'warmup_instructions': 0, 'binary_sha256': binary_hash,
                       'tracer_sha256': sha256(tracer),
                       'trace_sha256': sha256(compressed), 'command': command,
                       'driver_result': records[0]}
            meta_temp = folder / 'metadata.json'
            meta_temp.write_text(json.dumps(details, indent=2) + '\n')
            compressed.replace(output)
            meta_temp.replace(metadata)
            print(json.dumps({'trace': str(output), 'roi_instructions': count,
                              'padding_instructions': PADDING, 'metadata': str(metadata)}))
            return 0
    except KeyboardInterrupt:
        stop(process)
        print('Capture interrupted; no successful new trace was published', file=sys.stderr)
        return 130
    except (OSError, ValueError, RuntimeError) as error:
        stop(process)
        print('ERROR: ' + str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
