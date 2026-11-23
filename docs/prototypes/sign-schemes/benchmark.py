#!/usr/bin/env python3
"""Short verification-only benchmark, not the 10-minute service acceptance test."""
import concurrent.futures, importlib.util, json, os, pathlib, statistics, subprocess, sys, tempfile, time
sys.dont_write_bytecode = True
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from schemes import signing_data, verify_a, verify_b
from run_vectors import ssh_verify
v=json.loads((pathlib.Path(__file__).resolve().parent/'vectors.json').read_text())
m=signing_data(v['request']); pub=bytes.fromhex(v['public_hex']); sig=bytes.fromhex(v['signature_hex'])

def bench(name, fn, count):
    start=time.perf_counter(); assert fn(); cold=(time.perf_counter()-start)*1000
    assert fn()  # warm-up before latency and throughput samples
    times=[]
    for _ in range(20):
        start=time.perf_counter(); assert fn(); times.append((time.perf_counter()-start)*1000)
    times.sort()
    rates=[]
    for _ in range(5):
        start=time.perf_counter()
        with concurrent.futures.ThreadPoolExecutor(max_workers=32) as pool:
            results=list(pool.map(lambda _: fn(), range(count)))
        elapsed=time.perf_counter()-start
        assert all(results)
        rates.append(count/elapsed)
    print(f'{name}: first call={cold:.3f} ms; warmed latency p50={statistics.median(times):.3f} ms p99={times[-1]:.3f} ms (20 samples); concurrency=32 n={count} per run; throughput median={statistics.median(rates):.2f} req/s range={min(rates):.2f}–{max(rates):.2f} (5 runs)', flush=True)

print(f'cpu_count={os.cpu_count()} (shared with other sessions; throughput varies run to run)', flush=True)
if importlib.util.find_spec('cryptography'): bench('A',lambda: verify_a(pub,sig,m),320)
else: print('A SKIP: cryptography unavailable')
bench('B',lambda: verify_b(pub,sig,m),32)
with tempfile.TemporaryDirectory(prefix='lgw-236-bench-') as tmp:
    p=pathlib.Path(tmp); key=p/'key'; data=p/'data'; allowed=p/'allowed'
    subprocess.run(['ssh-keygen','-q','-t','ed25519','-N','','-f',str(key)],check=True)
    allowed.write_text('prototype '+key.with_suffix('.pub').read_text()); data.write_bytes(m)
    subprocess.run(['ssh-keygen','-Y','sign','-n','llmgateway','-f',str(key),str(data)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    bench('C',lambda: ssh_verify(m,p/'data.sig',allowed)[0],320)
