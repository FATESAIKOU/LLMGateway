#!/usr/bin/env python3
"""Offline vectors; all SSH private keys live in a deleted temporary directory."""
import argparse, base64, copy, hashlib, importlib.util, json, pathlib, subprocess, tempfile, sys
sys.dont_write_bytecode = True
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from schemes import signing_data, verify_a, verify_b, sign_a, P, L, BASE

ROOT = pathlib.Path(__file__).resolve().parent
V = json.loads((ROOT/'vectors.json').read_text())
HAS_A = importlib.util.find_spec('cryptography') is not None

def ssh_verify(message, signature, allowed, namespace='llmgateway'):
    result = subprocess.run(['ssh-keygen','-Y','verify','-f',str(allowed),'-I','prototype','-n',namespace,'-s',str(signature)], input=message, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=10)
    return result.returncode == 0, result.stderr.decode(errors='replace')

def run():
    parser = argparse.ArgumentParser()
    parser.add_argument('--require-a', action='store_true')
    args = parser.parse_args()
    print(f'A SKIP count: {0 if HAS_A else 1}')
    if args.require_a and not HAS_A:
        print('FAIL: --require-a requires cryptography')
        raise SystemExit(1)
    message = signing_data(V['request'])
    assert message.decode() == V['signing_data']
    public = bytes.fromhex(V['public_hex']); sig = bytes.fromhex(V['signature_hex'])
    tests = [('valid', V['request'], sig, True)]
    s = bytearray(sig); s[0] ^= 1
    tests.append(('signature bit', V['request'], bytes(s), False))
    for label, field, value in [('audience','audience','mlp-sidecar'),('method','method','POST'),('query order','target','/v1/resolve?b=2&a=1'),('body','body_hex','01')]:
        v = copy.deepcopy(V['request']); v[field] = value
        tests.append((label, v, sig, False))
    for name, fn in [('A', verify_a), ('B', verify_b)]:
        if name == 'A' and not HAS_A:
            print('A SKIP: cryptography unavailable'); continue
        for label, req, signature, expected in tests:
            result = fn(public, signature, signing_data(req))
            print(f'{name} {label}: {"PASS" if result == expected else "FAIL"}')
            if result != expected: raise SystemExit(1)
    # RFC 8032 section 7.1, first three vectors (message, public key, signature).
    rfc = [
        ('', 'd75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a', 'e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e06522490155' '5fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b'),
        ('72', '3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c', '92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da' '085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00'),
        ('af82', 'fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025', '6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3a' 'c18ff9b538d16f290ae67f760984dc6594a7c15e9716ed28dc027beceea1ec40a')]
    ident = (1).to_bytes(32, 'little')
    base = (BASE[1] | ((BASE[0] & 1) << 255)).to_bytes(32, 'little')
    unit_sig = ident + bytes(32)
    order2 = (P-1).to_bytes(32, 'little')
    # [1]B = R + [h]A for order-2 A when h is even: catches removal of subgroup check.
    msg = b''
    while int.from_bytes(hashlib.sha512(base + order2 + msg).digest(), 'little') % L % 2:
        msg += b'x'
    negative = [
        ('s+L', public, sig[:32] + (int.from_bytes(sig[32:], 'little') + L).to_bytes(32, 'little'), message),
        ('noncanonical y', (P+1).to_bytes(32, 'little'), base + (1).to_bytes(32, 'little'), b''),
        ('small-order identity and unit signature', ident, unit_sig, b''),
        ('small-order order-2 forgery', order2, base + (1).to_bytes(32, 'little'), msg),
        ('x=0 sign=1', (1 + 2**255).to_bytes(32, 'little'), unit_sig, b''),
        ('noncanonical identity', (P+1).to_bytes(32, 'little'), unit_sig, b'')]
    for name, fn in [('A', verify_a), ('B', verify_b)]:
        if name == 'A' and not HAS_A: continue
        for i, (m, pk, signature) in enumerate(rfc, 1):
            ok = fn(bytes.fromhex(pk), bytes.fromhex(signature), bytes.fromhex(m))
            print(f'{name} RFC 8032 {i}: {"PASS" if ok else "FAIL"}')
            if not ok: raise SystemExit(1)
        for label, pk, signature, m in negative:
            result = fn(pk, signature, m)
            # A's documented OpenSSL behavior is observational, not a security expectation.
            ok = not result if name == 'B' else True
            verdict = ('PASS' if ok else 'FAIL') if name == 'B' else 'OBSERVED'
            print(f'{name} {label}: result={result} {verdict}')
            if not ok: raise SystemExit(1)
    if HAS_A:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
        key = Ed25519PrivateKey.generate()
        signed = sign_a(key, message)
        raw = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        assert verify_a(raw, signed, message) and verify_b(raw, signed, message)
        print('A generated signature verified by A and B: PASS')
    with tempfile.TemporaryDirectory(prefix='lgw-236-') as tmp:
        p = pathlib.Path(tmp); key = p/'key'; data = p/'data'; allowed = p/'allowed'
        subprocess.run(['ssh-keygen','-q','-t','ed25519','-N','','-f',str(key)],check=True)
        allowed.write_text('prototype '+key.with_suffix('.pub').read_text())
        data.write_bytes(message)
        subprocess.run(['ssh-keygen','-Y','sign','-n','llmgateway','-f',str(key),str(data)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        sshsig = p/'data.sig'
        for label, req, _, expected in tests:
            if label == 'signature bit':
                lines = sshsig.read_bytes().splitlines()
                blob = bytearray(base64.b64decode(b''.join(lines[1:-1]), validate=True))
                # SSHSIG ends with a length-prefixed signature blob: algorithm + signature bytes.
                pos = 6 + 4
                def field():
                    nonlocal_pos[0] += 4 + int.from_bytes(blob[nonlocal_pos[0]:nonlocal_pos[0]+4], 'big')
                nonlocal_pos = [pos]
                for _ in range(4): field()  # public key, namespace, reserved, hash algorithm
                outer = nonlocal_pos[0]; size = int.from_bytes(blob[outer:outer+4], 'big')
                inner = outer + 4
                alg_len = int.from_bytes(blob[inner:inner+4], 'big'); inner += 4 + alg_len
                sig_len = int.from_bytes(blob[inner:inner+4], 'big'); inner += 4
                assert inner + sig_len == outer + 4 + size == len(blob) and sig_len > 0
                blob[inner + sig_len - 1] ^= 1
                encoded = base64.b64encode(blob)
                bad = p/'bad.sig'; bad.write_bytes(lines[0]+b'\n'+b'\n'.join(encoded[i:i+70] for i in range(0,len(encoded),70))+b'\n'+lines[-1]+b'\n')
                result, err = ssh_verify(message,bad,allowed)
            else: result, err = ssh_verify(signing_data(req),sshsig,allowed)
            ok = result == expected and (expected or 'incorrect signature' in err)
            print(f'C {label}: {"PASS" if ok else "FAIL"}')
            if not ok: raise SystemExit(1)
        result, err = ssh_verify(message,sshsig,allowed,'mlp-sidecar')
        ok = not result and 'namespace does not match' in err
        print(f'C namespace: {"PASS" if ok else "FAIL"}')
        if not ok: raise SystemExit(1)
        # Wording is OpenSSH-version specific (9.6p1: "Couldn't parse signature"); the check is "rejected as malformed".
        bad.write_bytes(b'not an SSH signature\n')
        result, err = ssh_verify(message,bad,allowed)
        ok = not result and ('parse signature' in err or 'invalid format' in err)
        print(f'C malformed format: {"PASS" if ok else "FAIL"}')
        if not ok: raise SystemExit(1)
    try:
        signing_data({**V['request'], 'audience': 'llmgateway-ü'})
        print('non-ASCII signing field: FAIL (no error)'); raise SystemExit(1)
    except ValueError as e:
        print(f'non-ASCII signing field: PASS ({e})')
    print('All available vectors passed; duplicate headers require HTTP-layer rejection (not implemented).')

if __name__ == '__main__': run()
