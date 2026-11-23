"""Offline prototype only. Not a production Ed25519 implementation."""
import hashlib

P = 2**255 - 19
L = 2**252 + 27742317777372353535851937790883648493
D = -121665 * pow(121666, P-2, P) % P
I = pow(2, (P-1)//4, P)
IDENTITY = (0, 1)

def recover(y, sign):
    if y >= P: raise ValueError('noncanonical y')
    x2 = (y*y-1) * pow(D*y*y+1, P-2, P) % P
    x = pow(x2, (P+3)//8, P)
    if (x*x-x2) % P: x = x*I % P
    if (x*x-x2) % P or (x == 0 and sign): raise ValueError('invalid point')
    if x & 1 != sign: x = P-x
    return x, y

def decode(s):
    if len(s) != 32: raise ValueError('point length')
    n = int.from_bytes(s, 'little')
    return recover(n & ((1<<255)-1), n>>255)

def add(a, b):
    x,y = a; u,v = b
    t = D*x*u*y*v % P
    return ((x*v+u*y)*pow(1+t, P-2, P)%P, (y*v+x*u)*pow(1-t, P-2, P)%P)

def mul(p, n):
    q = IDENTITY
    while n:
        if n & 1: q = add(q, p)
        p = add(p, p); n >>= 1
    return q

BASE = recover(4*pow(5, P-2, P)%P, 0)

def verify_b(public, signature, message):
    try:
        if len(signature) != 64 or len(public) != 32: return False
        a, r = decode(public), decode(signature[:32])
        s = int.from_bytes(signature[32:], 'little')
        if s >= L or a == IDENTITY or r == IDENTITY: return False
        if mul(a, L) != IDENTITY or mul(r, L) != IDENTITY: return False
        h = int.from_bytes(hashlib.sha512(signature[:32]+public+message).digest(), 'little') % L
        return mul(BASE, s) == add(r, mul(a, h))
    except (ValueError, OverflowError): return False

def signing_data(v):
    fields = ['LGW-HTTP-SIG-V1', v['audience'], v['key_id'], v['timestamp'], v['nonce'], v['method'], v['target'], hashlib.sha256(bytes.fromhex(v['body_hex'])).hexdigest()]
    if any('\n' in x or '\r' in x for x in fields): raise ValueError('newline')
    try: return ('\n'.join(fields)+'\n').encode('ascii')
    except UnicodeEncodeError: raise ValueError('signing fields must be ASCII') from None

def verify_a(public, signature, message):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from cryptography.exceptions import InvalidSignature
    try:
        Ed25519PublicKey.from_public_bytes(public).verify(signature, message)
        return True
    except (InvalidSignature, ValueError): return False

def sign_a(private, message):
    """Sign with a caller-supplied cryptography Ed25519 private key."""
    return private.sign(message)
