"""#275：`llmgateway` 與 MLP sidecar（`mlp-sidecar`）namespace 的互拒（跨 repo：MLP#1529）。

這一支只驗「送進本 repo 的那一半」：MLP 用 **自己的** SSHSIG namespace 簽出來的簽章，
不得被 `lgwd` 的 enforce gate 當成合法的 llmgateway 請求。反向（llmgateway 簽章送
MLP）不在本 repo 驗；固定的跨 repo 向量見 `MLP_CROSS_VECTOR`（私鑰已丟棄）。

作法與界線：

- 只用真的 `ssh-keygen` 與 `TemporaryDirectory` 裡的臨時 ed25519 鑰（測試結束即刪）；
  不讀任何既有金鑰、不碰 `~/.ssh`、不連網、不連真機、不寫任何東西進 repo。
- 八行簽章資料與 `tests.auth_signer.signing_message` 完全相同（第 2 行仍是協議的
  audience `llmgateway`），**唯一差異是 SSHSIG 信封裡的 namespace**；這樣「被拒」就
  只可能來自 namespace／長度那一層，而不是「訊息長得剛好不一樣」。
- 端到端沿用 `tests.test_http_auth.HttpAuthCase` 的夾具（真的 loopback、真的名冊、真的
  兩庫、真的 `ssh-keygen -Y verify`，service 是 `tests.test_http.FakeService`）。

長度背景（實測，2026-11-23）：SSHSIG blob 每多 1 byte 的 namespace 就多 1 byte，
base64url 長度不一定同步增加。`llmgateway`（10 字元）→ 180 bytes／240 字元；
`mlp-sidecar`（11 字元）→ 181 bytes／**242** 字元；13 字元 namespace → 183 bytes／
244 字元。
"""

from __future__ import annotations

import base64
import subprocess
import tempfile
import unittest

from lgwd import sshsig
from lgwd.auth_registry import KeyEntry
from lgwd.sshsig import Reject
from tests.auth_signer import (
    NAMESPACE,
    SSH_KEYGEN,
    Signer,
    b64url,
    dearmor,
    new_nonce,
    signing_message,
)
from tests.test_http_auth import COOLDOWN, JSON_BODY, UNAUTHORIZED, HttpAuthCase

#: MLP sidecar 用的 namespace（與 `lgwd.sshsig.NAMESPACE` 必須不同）。
MLP_NAMESPACE = "mlp-sidecar"
#: 10 字元的外來 namespace：與 `llmgateway` **等長**，用來證明 namespace 那一層獨立於
#: 長度（等長 → 仍是 240 字元、180 bytes，預解析只可能以 namespace 拒）。
SAME_LENGTH_FOREIGN_NAMESPACE = "mlpsidecar"
#: 13 字元的 namespace：blob 多 3 bytes，簽章剛好 **244** 字元（#275 要盯的那個長度）。
LEN_244_NAMESPACE = "mlp-sidecar13"

#: 實測長度（上面的說明）；測試會斷言，長度表若變了就會紅。
SIG_LEN_LLMGATEWAY = 240
SIG_LEN_MLP = 242
SIG_LEN_244 = 244

#: 跨 repo 的固定向量（MLP 那一半用）。
#: 由一把臨時 ed25519 鑰對「與 llmgateway 請求相同的八行」以 `-n mlp-sidecar` 簽出；
#: 私鑰用完即丟（暫存目錄已刪），這裡只有公鑰與簽章，不含任何真憑證。
MLP_CROSS_VECTOR = {
    "schema": 1,
    "key_id": "k-275-vector",
    "public_key": (
        "ssh-ed25519 "
        "AAAAC3NzaC1lZDI1NTE5AAAAILnC84RgoAc4hKwhieHsSWG1qD9rAcgVJSN1I40Fshue"
    ),
    "namespace": MLP_NAMESPACE,
    "signing_data": (
        "LGW-HTTP-SIG-V1\n"
        "llmgateway\n"
        "k-275-vector\n"
        "1795392000\n"
        "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8\n"
        "GET\n"
        "/v1/subscriptions\n"
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855\n"
    ),
    "signature": (
        "U1NIU0lHAAAAAQAAADMAAAALc3NoLWVkMjU1MTkAAAAgucLzhGCgBziErCGJ4exJ"
        "YbWoP2sByBUlI3UjjQWyG54AAAALbWxwLXNpZGVjYXIAAAAAAAAABnNoYTUxMgAA"
        "AFMAAAALc3NoLWVkMjU1MTkAAABAvL-LJOcXPsSc4Ahg46TWCuee-DHx-QOKdVvD"
        "Bs9sJbWt2tZAOvgzcHVPtxErOSLd9198g2eINVOp_DaYFuP2AA"
    ),
}

#: `ssh-keygen` 子行程只給這兩個（與 `tests.auth_signer` 同形；HOME 指向暫存目錄）。
_BASE_ENV = {"LC_ALL": "C", "PATH": "/usr/bin:/bin"}


def _env(home: str) -> dict:
    env = dict(_BASE_ENV)
    env["HOME"] = home
    return env


def sign_in_namespace(signer: Signer, message: bytes, namespace: str) -> str:
    """用真的 `ssh-keygen -Y sign -n <namespace>` 簽，回無 padding base64url 簽章。"""
    result = subprocess.run(
        [SSH_KEYGEN, "-Y", "sign", "-n", namespace, "-f", signer.private_path],
        input=message, capture_output=True, env=_env(signer.directory), timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError("ssh-keygen -Y sign failed with exit {0}".format(
            result.returncode))
    return b64url(dearmor(result.stdout.decode("ascii")))


def namespaced_headers(signer: Signer, *, key_id: str, method: str, target: str,
                       body: bytes, timestamp: int, nonce: str,
                       namespace: str) -> tuple[bytes, list]:
    """八行訊息（`llmgateway` 的格式）＋ 指定 namespace 的 SSHSIG 信封，組成五個標頭。"""
    message = signing_message(key_id=key_id, timestamp=timestamp, nonce=nonce,
                              method=method, target=target, body=body)
    return message, [
        ("X-LGW-Auth-Version", "1"),
        ("X-LGW-Key-Id", key_id),
        ("X-LGW-Timestamp", str(timestamp)),
        ("X-LGW-Nonce", nonce),
        ("X-LGW-Signature", sign_in_namespace(signer, message, namespace)),
    ]


def signature_of(headers) -> str:
    for name, value in headers:
        if name.lower() == "x-lgw-signature":
            return value
    raise AssertionError("no X-LGW-Signature in headers")


def entry_for(signer: Signer, key_id: str = "k-275") -> KeyEntry:
    """把臨時鑰的公鑰 32 bytes 包成 `KeyEntry`（名冊的形狀；不寫任何檔）。"""
    return KeyEntry(key_id=key_id, principal="w1-worker",
                    public_key=signer.public_key32, status="active")


def auth_lines(logs) -> list:
    return [line for line in logs.output if "auth mode=" in line]


class CountingVerifier:
    """把 `verify` 計數後交給真的 verifier：用來證明拒絕沒有走到驗章那一層。"""

    def __init__(self, real):
        self.real = real
        self.calls = 0

    def verify(self, *args, **kwargs):
        self.calls += 1
        return self.real.verify(*args, **kwargs)


class CrossNamespaceHttpTests(HttpAuthCase):
    """#275：MLP namespace 的簽章在 enforce gate 是 401，且沒有業務／防重放副作用。"""

    def with_counting_verifier(self):
        runtime, service, port = self.enforce_server()
        verifier = CountingVerifier(runtime.gate.verifier)
        runtime.gate.verifier = verifier
        return runtime, service, port, verifier

    def test_275_mlp_namespace_is_rejected_without_side_effects(self):
        runtime, service, port, verifier = self.with_counting_verifier()
        timestamp = self.now()
        nonce = new_nonce()
        message, headers = namespaced_headers(
            self.signer, key_id="k-a", method="GET", target="/v1/subscriptions",
            body=b"", timestamp=timestamp, nonce=nonce, namespace=MLP_NAMESPACE)
        signature = signature_of(headers)
        self.assertEqual(len(signature), SIG_LEN_MLP,
                         msg="mlp-sidecar 的簽章長度（11 字元 namespace）")
        self.assertNotEqual(len(signature), sshsig.SIG_B64URL_LEN)

        with self.assertLogs("lgwd.http", level="INFO") as logs:
            status, body, response_headers = self.call(
                port, "GET", "/v1/subscriptions", headers)

        # 1) 401：與一般未授權逐 byte 相同、不加 WWW-Authenticate。
        self.assertEqual((status, body), (401, UNAUTHORIZED))
        self.assertNotIn("www-authenticate", response_headers)

        # 2) 業務無副作用：service（含冷卻登記）一次都沒被呼叫。
        self.assertEqual(service.calls, [])

        # 3) 稽核只有那一筆 deny（reason=malformed，沒有 principal），沒有 pass。
        lines = auth_lines(logs)
        self.assertEqual(len(lines), 1, msg="外來 namespace 只記一筆認證事件")
        self.assertIn("mode=enforce", lines[0])
        self.assertIn("outcome=deny", lines[0])
        self.assertIn("reason=malformed", lines[0])
        self.assertIn("principal=-", lines[0])

        # 4) 防重放：被拒的請求不寫 nonce（現行語意：accept 只在 PASS 之後呼叫）。
        self.assertEqual(runtime.replay.row_count(), 0)

        # 5) 拒絕發生在預解析（格式／namespace），不是「剛好驗章失敗」：verifier 沒被叫。
        self.assertEqual(verifier.calls, 0)

        # 6) nonce 沒有被消耗：同一個 timestamp／nonce 換成 llmgateway namespace 就通過。
        control_message, control_headers = namespaced_headers(
            self.signer, key_id="k-a", method="GET", target="/v1/subscriptions",
            body=b"", timestamp=timestamp, nonce=nonce, namespace=NAMESPACE)
        self.assertEqual(control_message, message,
                         msg="兩邊的八行資料必須相同，只有信封 namespace 不同")
        self.assertEqual(len(signature_of(control_headers)), SIG_LEN_LLMGATEWAY)
        status, _, _ = self.call(port, "GET", "/v1/subscriptions", control_headers)
        self.assertEqual(status, 200)
        self.assertEqual(service.calls, [("list_subscriptions",)])
        self.assertEqual(runtime.replay.row_count(), 1)
        self.assertEqual(verifier.calls, 1)

    def test_275_mlp_namespace_post_cooldown_has_no_business_effect(self):
        """POST 那條也驗：冷卻登記（唯一有業務副作用的路由）沒有發生。"""
        runtime, service, port, verifier = self.with_counting_verifier()
        _, headers = namespaced_headers(
            self.signer, key_id="k-a", method="POST", target=COOLDOWN,
            body=JSON_BODY, timestamp=self.now(), nonce=new_nonce(),
            namespace=MLP_NAMESPACE)
        status, body, _ = self.call(port, "POST", COOLDOWN, headers, JSON_BODY)
        self.assertEqual((status, body), (401, UNAUTHORIZED))
        self.assertEqual(service.calls, [])
        self.assertEqual(runtime.replay.row_count(), 0)
        self.assertEqual(verifier.calls, 0)

    def test_275_244_character_signature_is_rejected_at_the_gate(self):
        """#275 要求 3 的 HTTP 面：244 字元的簽章（13 字元 namespace）必須 401、沒有副作用。"""
        runtime, service, port, verifier = self.with_counting_verifier()
        _, headers = namespaced_headers(
            self.signer, key_id="k-a", method="GET", target="/v1/subscriptions",
            body=b"", timestamp=self.now(), nonce=new_nonce(),
            namespace=LEN_244_NAMESPACE)
        self.assertEqual(len(signature_of(headers)), SIG_LEN_244)
        status, body, _ = self.call(port, "GET", "/v1/subscriptions", headers)
        self.assertEqual((status, body), (401, UNAUTHORIZED))
        self.assertEqual(service.calls, [])
        self.assertEqual(runtime.replay.row_count(), 0)
        self.assertEqual(verifier.calls, 0, msg="長度閘門不得把 244 字元帶到驗章")

    def test_275_same_key_signed_in_llmgateway_namespace_passes(self):
        """對照組：#275 的拒絕不是鑰、名冊或夾具壞掉（同一把鑰、同一段程式）。"""
        runtime, service, port, verifier = self.with_counting_verifier()
        with self.assertLogs("lgwd.http", level="INFO") as logs:
            status, body, _ = self.call(
                port, "GET", "/v1/subscriptions", self.sign(target="/v1/subscriptions"))
        self.assertEqual(status, 200)
        self.assertEqual(service.calls, [("list_subscriptions",)])
        self.assertEqual(runtime.replay.row_count(), 1)
        self.assertEqual(verifier.calls, 1)
        lines = auth_lines(logs)
        self.assertEqual(len(lines), 1)
        self.assertIn("outcome=pass", lines[0])
        self.assertIn("principal=w1-worker", lines[0])


class CrossNamespacePreparseTests(unittest.TestCase):
    """#275／要求 3：長度與 namespace 兩層各自把關，且拒因是格式而不是巧合。"""

    @classmethod
    def setUpClass(cls):
        cls._keys = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls._keys.cleanup)
        cls.signer = Signer(cls._keys.name, "k-275-prep")
        cls.entry = entry_for(cls.signer)

    def signature(self, namespace: str, key_id: str = "k-275-prep") -> str:
        message = signing_message(
            key_id=key_id, timestamp=1795392000,
            nonce="AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8",
            method="GET", target="/v1/subscriptions")
        return sign_in_namespace(self.signer, message, namespace)

    def test_275_llmgateway_signature_is_240_and_accepted(self):
        signature = self.signature(NAMESPACE)
        self.assertEqual(len(signature), SIG_LEN_LLMGATEWAY)
        blob = sshsig.preparse(signature, self.entry)
        self.assertEqual(len(blob), sshsig.BLOB_LEN)

    def test_275_mlp_namespace_fails_the_length_gate_first(self):
        """現行語意：`mlp-sidecar` 的 242 字元先被長度閘門擋（`bad_length`），沒到 namespace。"""
        signature = self.signature(MLP_NAMESPACE)
        self.assertEqual(len(signature), SIG_LEN_MLP)
        with self.assertRaises(Reject) as caught:
            sshsig.preparse(signature, self.entry)
        self.assertEqual(caught.exception.kind, sshsig.KIND_BAD_LENGTH)
        self.assertNotEqual(caught.exception.kind, sshsig.KIND_BAD_NAMESPACE)

    def test_275_244_character_signature_is_never_a_legal_llmgateway_signature(self):
        """13 字元 namespace → 剛好 244 字元；必須在長度閘門就被擋（不是碰巧驗不過）。"""
        signature = self.signature(LEN_244_NAMESPACE)
        self.assertEqual(len(signature), SIG_LEN_244)
        self.assertNotEqual(len(signature), sshsig.SIG_B64URL_LEN)
        with self.assertRaises(Reject) as caught:
            sshsig.preparse(signature, self.entry)
        self.assertEqual(caught.exception.kind, sshsig.KIND_BAD_LENGTH)

    def test_275_same_length_foreign_namespace_fails_the_namespace_gate(self):
        """等長（240 字元／180 bytes）的外來 namespace：只可能以 namespace 被拒。"""
        signature = self.signature(SAME_LENGTH_FOREIGN_NAMESPACE)
        self.assertEqual(len(signature), SIG_LEN_LLMGATEWAY,
                         msg="等長對照：長度相同，所以這一條測的是 namespace")
        with self.assertRaises(Reject) as caught:
            sshsig.preparse(signature, self.entry)
        self.assertEqual(caught.exception.kind, sshsig.KIND_BAD_NAMESPACE)


class CrossNamespaceVectorTests(unittest.TestCase):
    """跨 repo 固定向量（給 MLP#1529）：結構獨立重解一次，不呼叫 `lgwd.sshsig` 的預解析。"""

    @staticmethod
    def read_string(buf: bytes, pos: int) -> tuple[bytes, int]:
        length = int.from_bytes(buf[pos:pos + 4], "big")
        return buf[pos + 4:pos + 4 + length], pos + 4 + length

    def test_275_vector_is_a_242_character_mlp_namespace_signature(self):
        signature = MLP_CROSS_VECTOR["signature"]
        self.assertEqual(len(signature), SIG_LEN_MLP)
        blob = base64.urlsafe_b64decode(signature + "==")
        self.assertEqual(len(blob), 181)
        self.assertEqual(blob[:6], b"SSHSIG")
        self.assertEqual(int.from_bytes(blob[6:10], "big"), 1)
        public_key, pos = self.read_string(blob, 10)
        self.assertEqual(
            base64.b64encode(public_key).decode("ascii"),
            MLP_CROSS_VECTOR["public_key"].split()[1])
        namespace, pos = self.read_string(blob, pos)
        self.assertEqual(namespace, MLP_CROSS_VECTOR["namespace"].encode("ascii"))
        self.assertEqual(namespace, b"mlp-sidecar")
        reserved, pos = self.read_string(blob, pos)
        self.assertEqual(reserved, b"")
        hash_algorithm, pos = self.read_string(blob, pos)
        self.assertEqual(hash_algorithm, b"sha512")
        _, pos = self.read_string(blob, pos)
        self.assertEqual(pos, len(blob), msg="向量不得有多餘 bytes")

    def test_275_vector_signing_data_is_the_eight_llmgateway_lines(self):
        data = MLP_CROSS_VECTOR["signing_data"]
        lines = data.split("\n")
        self.assertEqual(len(lines), 9, msg="八行＋結尾 LF")
        self.assertEqual(lines[0], "LGW-HTTP-SIG-V1")
        self.assertEqual(lines[1], "llmgateway")
        self.assertEqual(lines[2], MLP_CROSS_VECTOR["key_id"])
        self.assertEqual(lines[8], "")
        self.assertEqual(
            lines,
            signing_message(
                key_id=MLP_CROSS_VECTOR["key_id"], timestamp=1795392000,
                nonce="AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8",
                method="GET", target="/v1/subscriptions").decode("ascii").split("\n"))


if __name__ == "__main__":
    unittest.main()
