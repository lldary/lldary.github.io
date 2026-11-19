#!/usr/bin/env python3
"""Evaluate an L1D prefetcher against next_line in the course ChampSim environment.

Default: my_prefetcher on shell_sort, gemm and 14 cache traces; 20M warmup + 100M measurement.
Cache next_line baselines; stdout prints only {"score": 100 * GM(IPC/IPC_next_line)}.
Uses Python 3.8+ standard-library modules on Linux; see auto-run.README.md.
"""
from __future__ import annotations

import argparse
import copy
from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

WARMUP_INSTRUCTIONS = 20_000_000
SIMULATION_INSTRUCTIONS = 100_000_000
PREFETCH_TRACES = ('shell_sort.champsimtrace.xz', 'gemm.champsimtrace.xz')
CACHE_TRACES = [
    "cache-10.champsimtrace.xz",
    "cache-11-1.champsimtrace.xz",
    "cache-11-2.champsimtrace.xz",
    "cache-1.champsimtrace.xz",
    "cache-2.champsimtrace.xz",
    "cache-3.champsimtrace.xz",
    "cache-4.champsimtrace.xz",
    "cache-5.champsimtrace.xz",
    "cache-6.champsimtrace.xz",
    "cache-7.champsimtrace.xz",
    "cache-8.champsimtrace.xz",
    "cache-9.champsimtrace.xz",
    "603.bwaves_s-1080B.champsimtrace.xz",
    "654.roms_s-1021B.champsimtrace.xz",
]
DEFAULT_TRACES = (*PREFETCH_TRACES, *CACHE_TRACES)
BASELINE = 'next_line'
DEFAULT_CONFIG = {
    'num_cores': 1, 'block_size': 64, 'page_size': 4096,
    'ooo_cpu': [{'frequency': 4000, 'rob_size': 352, 'branch_predictor': 'bimodal',
                 'btb': 'basic_btb', 'mispredict_penalty': 15}],
    'L1I': {'sets': 64, 'ways': 8, 'mshr_size': 16, 'max_tag_check': 1,
            'max_fill': 1, 'prefetcher': 'no', 'replacement': 'lru'},
    'L1D': {'sets': 64, 'ways': 12, 'mshr_size': 16, 'max_tag_check': 1,
            'max_fill': 1, 'prefetcher': BASELINE, 'replacement': 'lru'},
    'L2C': {'sets': 2048, 'ways': 16, 'mshr_size': 20, 'max_tag_check': 4,
            'max_fill': 4, 'prefetcher': 'no', 'replacement': 'lru', 'lower_level': 'DRAM'},
    'physical_memory': {'name': 'DRAM'},
}


def progress(message):
    print(message, file=sys.stderr, flush=True)


@contextmanager
def file_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def digest_file(path):
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as handle:
            json.dump(data, handle, indent=2, allow_nan=False)
            handle.write('\n')
        Path(name).replace(path)
    finally:
        Path(name).unlink(missing_ok=True)


def merge(defaults, supplied):
    result = copy.deepcopy(defaults)
    for key, value in supplied.items():
        if isinstance(result.get(key), dict) and isinstance(value, dict):
            result[key] = merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def configuration(args, prefetcher):
    """Fill omitted baseline fields, and reject changes to the shared platform."""
    path = args.config
    if path is None and (args.root / 'lab3_config.json').is_file():
        path = args.root / 'lab3_config.json'
    supplied = json.loads(path.read_text()) if path else {}
    if not isinstance(supplied, dict):
        raise ValueError('Configuration must contain one JSON object')
    supplied = copy.deepcopy(supplied)
    supplied.pop('executable_name', None)
    cores = supplied.pop('ooo_cpu', [{}])
    if (supplied.get('num_cores', 1) != 1 or not isinstance(cores, list)
            or len(cores) != 1 or not isinstance(cores[0], dict)):
        raise ValueError('Evaluation requires a single-core configuration')
    for level in ('L1I', 'L1D', 'L2C', 'physical_memory'):
        if level in supplied and not isinstance(supplied[level], dict):
            raise ValueError(f'{level} must use the documented baseline object format')
    config = merge(DEFAULT_CONFIG, supplied)
    config['ooo_cpu'] = [merge(DEFAULT_CONFIG['ooo_cpu'][0], cores[0])]
    config['L1D']['prefetcher'] = BASELINE
    canonical = lambda value: json.dumps(value, sort_keys=True, allow_nan=False)
    if canonical(config) != canonical(DEFAULT_CONFIG):
        different = sorted(k for k in set(config) | set(DEFAULT_CONFIG)
                           if canonical(config.get(k)) != canonical(DEFAULT_CONFIG.get(k)))
        raise ValueError('Configuration differs from the Part 2 shared platform: '
                         + ', '.join(different) + '. Restore the documented baseline first.')
    config['L1D']['prefetcher'] = prefetcher
    return config


def cpu_stats(data, expected, source):
    if not isinstance(data, dict):
        raise ValueError(f'Invalid CPU statistics: {source}')
    instructions, cycles = data.get('instructions'), data.get('cycles')
    if (isinstance(instructions, bool) or not isinstance(instructions, int)
            or isinstance(cycles, bool) or not isinstance(cycles, int)
            or instructions < expected or instructions <= 0 or cycles <= 0):
        raise ValueError(f'Incomplete or invalid measurement counters: {source}')
    ipc = instructions / cycles
    if not math.isfinite(ipc) or ipc <= 0:
        raise ValueError(f'IPC must be finite and positive: {source}')
    return {'instructions': instructions, 'cycles': cycles, 'ipc': ipc}


def parse_cpu_stats(json_path, log_path, expected):
    """Use native ROI instruction/cycle counts, not rounded or custom stdout."""
    output = log_path.read_text(errors='replace')
    if ('ChampSim completed all CPUs' not in output
            or 'Region of Interest Statistics' not in output):
        raise ValueError(f'ChampSim did not report a complete ROI: {log_path}')
    phases = json.loads(json_path.read_text())
    if not isinstance(phases, list):
        raise ValueError(f'Invalid native statistics file: {json_path}')
    simulation = [p for p in phases if isinstance(p, dict) and p.get('name') == 'Simulation']
    if len(simulation) != 1:
        raise ValueError(f'Expected one Simulation phase: {json_path}')
    roi = simulation[0].get('roi')
    if not isinstance(roi, dict):
        raise ValueError(f'Missing native ROI: {json_path}')
    cores = roi.get('cores', [])
    if not isinstance(cores, list) or len(cores) != 1:
        raise ValueError(f'Expected one ROI CPU: {json_path}')
    return cpu_stats(cores[0], expected, json_path)


def baseline_source_digest(root):
    # Compile only active modules. Candidate edits under prefetcher/<candidate>/
    # do not affect the next_line binary or invalidate its cache.
    digest = hashlib.sha256()
    files = []
    for name in ('inc', 'src', 'config', 'branch', 'btb', 'replacement',
                 'prefetcher/next_line', 'prefetcher/no'):
        folder = root / name
        if folder.is_dir():
            files.extend(p for p in folder.rglob('*')
                         if p.is_file() and p.suffix in ('.h', '.hh', '.hpp', '.hxx', '.cc', '.cpp', '.cxx', '.py', '.options'))
    for name in ('Makefile', 'global.options', 'module.options', 'vcpkg.json',
                 'config.sh', 'vcpkg_installed/vcpkg/status'):
        if (root / name).is_file():
            files.append(root / name)
    for path in sorted(files):
        digest.update(str(path.relative_to(root)).encode())
        digest.update(b'\0')
        digest.update(bytes.fromhex(digest_file(path)))
    compiler = shlex.split(os.environ.get('CXX', 'g++'))
    if not compiler:
        raise ValueError('CXX must name a compiler')
    version = subprocess.run(compiler + ['--version'], capture_output=True, timeout=15)
    if version.returncode:
        raise ValueError('Cannot determine the C++ compiler version')
    digest.update(json.dumps(compiler).encode())
    digest.update(version.stdout)
    for name in ('CXXFLAGS', 'CPPFLAGS', 'LDFLAGS', 'LDLIBS', 'MAKEFLAGS'):
        digest.update(json.dumps([name, os.environ.get(name, '')]).encode())
    return digest.hexdigest()


def baseline_metadata(args, config):
    common = {'version': 1, 'project': 'proj3-prefetch', 'metric': 'cpu_ipc',
              'prefetcher': BASELINE, 'configuration': config,
              'warmup_instructions': args.warmup, 'simulation_instructions': args.simulation,
              'source_sha256': baseline_source_digest(args.root)}
    return {trace: dict(common, trace_sha256=digest_file(args.trace_dir / trace))
            for trace in args.traces}


def cached_stats(path, metadata):
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, dict) or data.get('metadata') != metadata:
            return None
        stats = cpu_stats(data, metadata['simulation_instructions'], path)
        value = data.get('ipc')
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or not math.isclose(value, stats['ipc'], rel_tol=1e-12)):
            return None
        return stats
    except (OSError, ValueError, TypeError, OverflowError):
        return None


def calculate_score(results):
    """Baseline-equivalent score = 100, using an equal-weight geometric mean."""
    if not results:
        raise ValueError('No benchmark results')
    logs = []
    for result in results:
        baseline, candidate = result['baseline_ipc'], result['candidate_ipc']
        for value in (baseline, candidate):
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or value <= 0):
                raise ValueError('Scoring requires finite, positive IPC values')
        logs.append(math.log(candidate) - math.log(baseline))
    score = 100 * math.exp(math.fsum(logs) / len(logs))
    if not math.isfinite(score) or score <= 0:
        raise ValueError('Score is not finite and positive')
    return score

class Processes:
    """Own only this run's build/simulation process groups, including children."""
    def __init__(self):
        self.lock = threading.Lock()
        self.active = set()
        self.stopped = False

    def run(self, command, cwd, log_path):
        with log_path.open("w") as log:
            with self.lock:
                if self.stopped:
                    raise RuntimeError("Evaluation cancelled")
                process = subprocess.Popen(command, cwd=cwd, stdout=log,
                                           stderr=subprocess.STDOUT, start_new_session=True)
                self.active.add(process)
            try:
                status = process.wait()
            except BaseException:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                except ProcessLookupError:
                    pass
                raise
            finally:
                with self.lock:
                    self.active.discard(process)
        if status:
            raise RuntimeError(f"Command failed (exit {status}); see {log_path}")

    def stop(self):
        with self.lock:
            self.stopped = True
            active = list(self.active)
        for process in active:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        for process in active:
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()



def parallel_traces(args, traces, worker, processes):
    executor = ThreadPoolExecutor(max_workers=args.workers)
    futures, results = {}, {}
    try:
        futures = {executor.submit(worker, trace): trace for trace in traces}
        for future in as_completed(futures):
            results[futures[future]] = future.result()
    except BaseException:
        processes.stop()
        for future in futures:
            future.cancel()
        raise
    finally:
        executor.shutdown(wait=True)
    return results



def build_prefetcher(args, run_dir, processes, prefetcher, role):
    config = configuration(args, prefetcher)
    name = f'auto-run-p3-{role}-{prefetcher}-{run_dir.name}'
    config['executable_name'] = name
    config_file = run_dir / f'{role}-config.json'
    config_file.write_text(json.dumps(config, indent=2) + '\n')
    progress(f'[BUILD] Configuring and compiling {role} {prefetcher} once')
    # Share the Lab 2 lock: ChampSim's generated files are common to all builds.
    with file_lock(args.root / '.auto-run/build.lock'):
        processes.run([sys.executable, str(args.root / 'config.sh'),
                       '--no-compile-all-modules', str(config_file)],
                      args.root, run_dir / f'{role}-configure.log')
        processes.run(['make', '-j', str(args.build_jobs), f'bin/{name}'],
                      args.root, run_dir / f'{role}-build.log')
    binary = args.root / 'bin' / name
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise RuntimeError(f'Build did not produce executable {binary}')
    return binary


def simulate(args, binary, run_dir, processes, trace, role):
    log = run_dir / f'{role}-{trace}.log'
    native_json = run_dir / f'{role}-{trace}.stats.json'
    progress(f'[{role.upper()} START] {trace}')
    processes.run([str(binary), '--json', str(native_json),
                   '--warmup-instructions', str(args.warmup),
                   '--simulation-instructions', str(args.simulation),
                   str(args.trace_dir / trace)], args.root, log)
    stats = parse_cpu_stats(native_json, log, args.simulation)
    progress(f'[{role.upper()} DONE] {trace}: IPC={stats["ipc"]:.9g}')
    return stats


def prepare_baselines(args, run_dir, processes):
    config = configuration(args, BASELINE)
    metadata = baseline_metadata(args, config)
    with file_lock(args.baseline_dir / '.baseline.lock'):
        values, missing = {}, []
        for trace in args.traces:
            stats = cached_stats(args.baseline_dir / f'{trace}.json', metadata[trace])
            if stats is None:
                missing.append(trace)
            else:
                values[trace] = stats
        if not missing:
            progress(f'[BASELINE] Reusing all {len(values)} next_line results; no baseline build or simulation')
            return values
        progress(f'[BASELINE] Generating {len(missing)} missing/stale next_line results; reusing {len(values)}')
        binary = build_prefetcher(args, run_dir, processes, BASELINE, 'baseline')
        # Refuse to cache a build if shared/baseline sources changed while compiling.
        expected_source = next(iter(metadata.values()))['source_sha256']
        if baseline_source_digest(args.root) != expected_source:
            raise ValueError('Baseline sources changed during compilation; rerun after editing is finished')

        def one(trace):
            stats = simulate(args, binary, run_dir, processes, trace, 'baseline')
            atomic_json(args.baseline_dir / f'{trace}.json', dict(stats, metadata=metadata[trace]))
            return stats

        values.update(parallel_traces(args, missing, one, processes))
        return values


def evaluate(args, binary, run_dir, baselines, processes):
    def one(trace):
        candidate = simulate(args, binary, run_dir, processes, trace, 'candidate')
        baseline = baselines[trace]
        return {'trace': trace, 'baseline_ipc': baseline['ipc'], 'candidate_ipc': candidate['ipc'],
                'speedup': candidate['ipc'] / baseline['ipc'],
                'baseline_instructions': baseline['instructions'], 'baseline_cycles': baseline['cycles'],
                'candidate_instructions': candidate['instructions'], 'candidate_cycles': candidate['cycles']}

    results = parallel_traces(args, args.traces, one, processes)
    return [results[trace] for trace in args.traces]


def positive(value):
    value = int(value)
    if value <= 0:
        raise argparse.ArgumentTypeError('must be greater than zero')
    return value


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--champsim-root', dest='root', type=Path, default=Path.cwd(),
                        help='ChampSim checkout; default: current working directory')
    parser.add_argument('--config', type=Path,
                        help='Shared-platform JSON; default: <root>/lab3_config.json, then built-in course baseline')
    parser.add_argument('--candidate', default='my_prefetcher', help='L1D module directory name; default: my_prefetcher')
    parser.add_argument('--baseline-dir', type=Path, help='Default: <root>/baseline/next_line')
    parser.add_argument('--trace-dir', type=Path, help='Default: <root>/traces')
    parser.add_argument('--trace', action='append', dest='traces',
                        help='Trace filename, repeatable; default: shell_sort, gemm and 14 cache traces')
    parser.add_argument('--max-worker', '--max-workers', dest='workers', type=positive, default=4)
    parser.add_argument('--build-jobs', type=positive, default=min(8, os.cpu_count() or 1))
    parser.add_argument('--warmup-instructions', dest='warmup', type=int, default=WARMUP_INSTRUCTIONS)
    parser.add_argument('--simulation-instructions', dest='simulation', type=positive, default=SIMULATION_INSTRUCTIONS)
    parser.add_argument('--output-file', type=Path, help='Write detailed results in addition to the one-line stdout score')
    args = parser.parse_args(argv)
    args.root = args.root.resolve()
    args.config = args.config.resolve() if args.config else None
    args.baseline_dir = (args.baseline_dir or args.root / 'baseline/next_line').resolve()
    args.trace_dir = (args.trace_dir or args.root / 'traces').resolve()
    args.traces = args.traces or list(DEFAULT_TRACES)
    if args.warmup < 0:
        parser.error('--warmup-instructions must be nonnegative')
    if len(set(args.traces)) != len(args.traces):
        parser.error('Duplicate trace filenames')
    if any(Path(t).name != t or t in ('.', '..') for t in args.traces):
        parser.error('--trace requires a filename, not a path')
    if not args.candidate.isidentifier() or not args.candidate.isascii():
        parser.error('--candidate must be a C++ module identifier')
    return args


def main(argv=None):
    args = arguments(argv)
    processes, previous = Processes(), {}

    def interrupted(signum, _frame):
        signal.signal(signum, signal.SIG_IGN)
        raise KeyboardInterrupt(f'signal {signum}')

    try:
        for name in ('config.sh', 'Makefile'):
            if not (args.root / name).is_file():
                raise ValueError(f'Missing {args.root / name}; run inside the prepared ChampSim environment')
        for name in (BASELINE, args.candidate):
            if not (args.root / 'prefetcher' / name).is_dir():
                raise ValueError(f'Missing prefetcher module: {name}')
        for trace in args.traces:
            if not (args.trace_dir / trace).is_file():
                raise ValueError(f'Missing trace: {args.trace_dir / trace}')
        configuration(args, args.candidate)
        config_path = args.config or args.root / 'lab3_config.json'
        if args.output_file and args.output_file.resolve() == config_path.resolve():
            raise ValueError('--output-file must not overwrite the input configuration')
        workspace = args.root / '.auto-run-p3'
        workspace.mkdir(exist_ok=True)
        run_dir = Path(tempfile.mkdtemp(prefix='run-', dir=workspace))
        progress(f'[LOGS] {run_dir}')
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous[sig] = signal.signal(sig, interrupted)
        baselines = prepare_baselines(args, run_dir, processes)
        binary = build_prefetcher(args, run_dir, processes, args.candidate, 'candidate')
        results = evaluate(args, binary, run_dir, baselines, processes)
        raw_score = calculate_score(results)
        score = round(raw_score, 6)
        detail = {'score': score, 'geomean_speedup': raw_score / 100,
                  'score_formula': '100 * geometric_mean(candidate_ipc / next_line_ipc)',
                  'baseline': BASELINE, 'candidate': args.candidate,
                  'warmup_instructions': args.warmup, 'simulation_instructions': args.simulation,
                  'configuration': configuration(args, args.candidate),
                  'binary': str(binary), 'results': results}
        atomic_json(run_dir / 'results.json', detail)
        if args.output_file:
            atomic_json(args.output_file.resolve(), detail)
        print(json.dumps({'score': score}, allow_nan=False), flush=True)
        return 0
    except KeyboardInterrupt:
        progress('[ERROR] Evaluation interrupted')
        return 130
    except (OSError, ValueError, RuntimeError, OverflowError, subprocess.TimeoutExpired) as exc:
        progress(f'[ERROR] {exc}')
        return 1
    finally:
        for sig in previous:
            signal.signal(sig, signal.SIG_IGN)
        processes.stop()
        for sig, handler in previous.items():
            signal.signal(sig, handler)


if __name__ == '__main__':
    sys.exit(main())
