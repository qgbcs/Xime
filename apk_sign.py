#!/usr/bin/env python3
"""使用 NIST256p 私钥对 APK 进行确定性签名（整包字节可复现）。

修改说明：
  1. 移除 cryptography.x509.CertificateBuilder 生成证书的逻辑，
     因为它底层走 OpenSSL，ECDSA 使用随机 k（随机 nonce），每次输出都不同。
  2. 证书用 asn1crypto 手工拼装 TbsCertificate，然后用 RFC6979 确定性
     ECDSA（ecdsa 库的 sign_digest_deterministic）对其签名。
  3. 移除对 apksigner 的签名依赖：apksigner 的 v2/v3 签名同样走 JDK 的
     随机 k ECDSA，每次输出不同。本文件改为按 AOSP apksig 的字节格式
     （V2SchemeSigner / V3SchemeSigner，均已用参考实现逐字节比对验证）
     自行生成 APK Signature Scheme v2 + v3 签名块，签名全部使用 RFC6979
     确定性 ECDSA —— 相同私钥 + 相同 APK 字节 => 输出 APK 逐字节一致。
  4. v1（JAR 签名）被刻意省略：v2 自 API 24 起即为有效安装签名，
     本签名面向 minSdk >= 24 的应用。apksigner verify 可正常校验。
  5. apksigner 若可用（--sdk 或 ANDROID_HOME），签名后自动做一次
     verify 自检，保证自造签名块与参考验证器兼容。

结构地面真值（解析 apksigner 产物 + 比对 AOSP apksig 源码确认）：
  * 签名块对 = u64(pair_size) + u32(id) + value
  * v2/v3 value = LP(LP(signer体))
  * v2 signer体 = [LP sd][LP sigs][LP pk]
  * v3 signer体 = [LP sd][u32 minSdk][u32 maxSdk][LP sigs][LP pk]
  * sd(v2)  = LP( digests_sec + certs_sec + attrs_sec )
  * sd(v3)  = LP( digests_sec + certs_sec + u32 minSdk + u32 maxSdk + attrs_sec )
  * digests_sec = LP( LP( u32 algid + LP(digest32) ) )
  * certs_sec   = LP( LP( cert_der ) )
  * attrs_sec   = LP( 空 )
  * sigs_sec    = LP( LP( u32 algid + LP(sig_der) ) )
  * pk          = LP( spki_der )
  * 签名对象 = sd 的 content（即去掉最外层 LP 前缀后的字节）
  * 内容摘要：三段 [0,块起点) / 中央目录 / EOCD；EOCD 的 CD 偏移字段按
    "指向签名块起点"参与摘要（即未修改输入的原值）；每段独立按 1MB 分块，
    块摘要 = SHA256(0xa5 + u32块长 + 块)，根 = SHA256(0x5a + u32块数 + 块摘要拼接)
"""

import argparse
import datetime
import hashlib
import importlib.util
import os
import re
import struct
import subprocess
import sys
from pathlib import Path


def _pip_install(packages, index_url):
    command = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "-i",
        index_url,
    ]
    if "tuna" in index_url:
        command += ["--trusted-host", "pypi.tuna.tsinghua.edu.cn"]
    try:
        subprocess.check_call(command + list(packages))
        return True
    except (OSError, subprocess.CalledProcessError) as error:
        print(f"[!] 使用 {index_url} 安装依赖失败: {error}", file=sys.stderr)
        return False


def ensure_dependencies():
    """缺依赖时自动 pip 安装（同 multi_mqtt.py 的机制）。"""
    packages = {
        "cryptography": "cryptography",
        "ecdsa": "ecdsa",
        "asn1crypto": "asn1crypto",
    }
    missing = [package for module, package in packages.items()
               if importlib.util.find_spec(module) is None]
    if not missing:
        return

    print(f"[+] 缺少依赖: {', '.join(missing)}，开始自动安装 ...")
    # 可用 APK_SIGN_PIP_INDEX 指定源；默认按环境选源，失败自动换下一个源。
    override = os.environ.get("APK_SIGN_PIP_INDEX", "").strip()
    if override:
        index_urls = [override]
    elif "PYTHONANYWHERE_DOMAIN" in os.environ or "SPACE_ID" in os.environ:
        index_urls = ["https://pypi.org/simple",
                      "https://pypi.tuna.tsinghua.edu.cn/simple"]
    else:
        index_urls = ["https://pypi.tuna.tsinghua.edu.cn/simple",
                      "https://pypi.org/simple"]

    for index_url in index_urls:
        if _pip_install(missing, index_url):
            break
    else:
        sys.exit(f"[!] 依赖安装失败，请手动执行: {sys.executable} -m pip install "
                 f"{' '.join(missing)}")

    still_missing = [module for module in packages
                     if importlib.util.find_spec(module) is None]
    if still_missing:
        sys.exit(f"[!] 依赖安装后仍无法导入: {', '.join(still_missing)}")

ensure_dependencies()

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

import ecdsa

from asn1crypto import x509 as asn1x509
from asn1crypto import keys as asn1keys
from asn1crypto import algos as asn1algos

KEY_ALIAS = "apk_signer"
KEY_PASSWORD = "123456"
DEFAULT_PEM_PATH = Path.home() / ".ssh" / "NIST256p.pem"

# ---- APK Signing Block 常量（与 AOSP apksig 一致） ----
APK_SIG_BLOCK_MAGIC = b"APK Sig Block 42"
V2_BLOCK_ID = 0x7109871A
V3_BLOCK_ID = 0xF05368C0
ECDSA_SHA256_ALGID = 0x0201          # ECDSA with SHA-256, CHUNKED_SHA256 内容摘要
CHUNK_MAX_SIZE_BYTES = 1024 * 1024   # 1MB 分块
V3_MAX_SDK = 0x7FFFFFFF


def private_key_from_pem(pem_path: Path) -> ec.EllipticCurvePrivateKey:
    if not pem_path.is_file():
        raise FileNotFoundError(f"找不到 APK 签名私钥: {pem_path}")
    try:
        private_key = serialization.load_ssh_private_key(
            pem_path.read_bytes(), password=None
        )
    except ValueError:
        private_key = serialization.load_pem_private_key(
            pem_path.read_bytes(), password=None
        )
    if not isinstance(private_key, ec.EllipticCurvePrivateKey):
        raise ValueError("APK 签名私钥必须是 ECDSA 私钥")
    if not isinstance(private_key.curve, ec.SECP256R1):
        raise ValueError("APK 签名仅支持 NIST256p (secp256r1)")
    return private_key


def private_key_from_secexp(secexp: int) -> ec.EllipticCurvePrivateKey:
    curve = ec.SECP256R1()
    if not 1 <= secexp < 2**curve.key_size:
        raise ValueError(f"secexp 必须是 1 到 {2**curve.key_size - 1} 之间的整数")
    return ec.derive_private_key(secexp, curve)


def deterministic_ecdsa_sign_digest(
    private_key: ec.EllipticCurvePrivateKey, digest: bytes
) -> bytes:
    """使用 RFC6979 确定性 ECDSA 签名，确保相同 key+digest 必定得到相同签名。"""
    if not isinstance(private_key.curve, ec.SECP256R1):
        raise ValueError("仅支持 secp256r1 / NIST256p 的确定性签名")

    private_value = private_key.private_numbers().private_value
    signing_key = ecdsa.SigningKey.from_secret_exponent(
        private_value, curve=ecdsa.NIST256p
    )
    return signing_key.sign_digest_deterministic(
        digest,
        hashfunc=hashlib.sha256,
        sigencode=ecdsa.util.sigencode_der,
    )


def deterministic_ecdsa_sign(
    private_key: ec.EllipticCurvePrivateKey, message: bytes
) -> bytes:
    return deterministic_ecdsa_sign_digest(
        private_key, hashlib.sha256(message).digest()
    )


def build_certificate_deterministic(
    private_key: ec.EllipticCurvePrivateKey,
) -> bytes:
    """
    手工构造一个完全确定性的 X.509 v3 自签名证书（DER 字节）。
    相同私钥 => 相同证书 DER 字节 => 相同签名指纹，任何机器可复现。
    """
    spki_der = private_key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    spki = asn1keys.PublicKeyInfo.load(spki_der)

    serial_number = int.from_bytes(
        hashlib.sha256(spki_der).digest(), "big"
    ) % (2**63 - 1)
    if serial_number == 0:
        serial_number = 1

    subject = asn1x509.Name.build({
        "country_name": "CN",
        "organization_name": "SELF",
        "common_name": "APK_SIGNER",
    })

    not_before = asn1x509.Time({
        "utc_time": datetime.datetime(
            2024, 1, 1, 0, 0, 0, tzinfo=datetime.timezone.utc
        ),
    })

    not_after = asn1x509.Time({
        "general_time": datetime.datetime(
            2099, 12, 31, 23, 59, 59, tzinfo=datetime.timezone.utc
        ),
    })

    signature_algorithm = asn1algos.SignedDigestAlgorithm({
        "algorithm": "sha256_ecdsa"
    })

    tbs = asn1x509.TbsCertificate({
        "version": "v3",
        "serial_number": serial_number,
        "signature": signature_algorithm,
        "issuer": subject,
        "validity": {
            "not_before": not_before,
            "not_after": not_after,
        },
        "subject": subject,
        "subject_public_key_info": spki,
    })

    tbs_der = tbs.dump()
    signature = deterministic_ecdsa_sign(private_key, tbs_der)

    cert = asn1x509.Certificate({
        "tbs_certificate": tbs,
        "signature_algorithm": signature_algorithm,
        "signature_value": signature,
    })

    return cert.dump()


def make_keystore(
    private_key: ec.EllipticCurvePrivateKey, output_dir: Path
) -> dict:
    cert_der = build_certificate_deterministic(private_key)

    output_dir.mkdir(parents=True, exist_ok=True)

    key_path = output_dir / "apk_sign_key_NIST256p.pk8"
    cert_path = output_dir / "apk_sign_cert_NIST256p.der"

    key_path.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    cert_path.write_bytes(cert_der)

    return {
        "key_path": str(key_path),
        "cert_path": str(cert_path),
    }


def find_apksigner(sdk_path: str | None = None) -> Path:
    sdk = sdk_path or os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT")
    if not sdk:
        raise FileNotFoundError(
            "未找到 Android SDK 路径，请先通过 --sdk 或 ANDROID_HOME 设置。"
        )
    build_tools_dir = Path(sdk) / "build-tools"
    candidates = []
    if build_tools_dir.is_dir():
        candidates = [
            path / "apksigner"
            for path in build_tools_dir.iterdir()
            if path.is_dir() and (path / "apksigner").is_file()
        ]
    candidates.sort(
        key=lambda path: tuple(int(part) for part in re.findall(r"\d+", path.parent.name)),
        reverse=True,
    )
    if not candidates:
        raise FileNotFoundError(f"SDK 中未找到 apksigner: {build_tools_dir}")
    return candidates[0]


def signed_path(apk_path: Path) -> Path:
    if apk_path.name.endswith("-unsigned.apk"):
        return apk_path.with_name(
            apk_path.name[:-len("-unsigned.apk")] + "-signed.apk"
        )
    return apk_path.with_name(f"{apk_path.stem}-signed{apk_path.suffix}")


# ============================================================
# 确定性 APK Signing Block（v2 + v3）构造
# ============================================================

def _lp(payload: bytes) -> bytes:
    """uint32 小端长度前缀。"""
    return struct.pack("<I", len(payload)) + payload


def _content_digest(sections) -> bytes:
    """APK Signature Scheme v2/v3 的 CHUNKED_SHA256 内容摘要。

    每段独立按 1MB 分块；块摘要 = SHA256(0xa5 + u32块长 + 块内容)；
    根摘要 = SHA256(0x5a + u32块数 + 所有块摘要按序拼接)。
    空段不产生块。
    """
    chunk_digests: list[bytes] = []
    for section in sections:
        for start in range(0, len(section), CHUNK_MAX_SIZE_BYTES):
            chunk = section[start : start + CHUNK_MAX_SIZE_BYTES]
            chunk_digests.append(
                hashlib.sha256(
                    b"\xa5" + struct.pack("<I", len(chunk)) + chunk
                ).digest()
            )
    return hashlib.sha256(
        b"\x5a"
        + struct.pack("<I", len(chunk_digests))
        + b"".join(chunk_digests)
    ).digest()


def _locate_eocd(data: bytes) -> int:
    """定位 End of Central Directory，兼容带注释的 ZIP。"""
    scan_floor = max(0, len(data) - 22 - 0xFFFF)
    pos = data.rfind(b"PK\x05\x06")
    while pos != -1:
        if pos >= scan_floor and pos + 22 <= len(data):
            comment_len = struct.unpack_from("<H", data, pos + 20)[0]
            if pos + 22 + comment_len == len(data):
                return pos
        pos = data.rfind(b"PK\x05\x06", 0, pos)
    raise ValueError("未找到 ZIP End of Central Directory（EOCD）")


def _build_signer_values(
    private_key: ec.EllipticCurvePrivateKey,
    cert_der: bytes,
    digest: bytes,
    min_sdk: int,
) -> tuple[bytes, bytes]:
    """构造 v2 / v3 的 block value（各含一个签名者）。"""
    spki_der = private_key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )

    digests_sec = _lp(
        _lp(struct.pack("<I", ECDSA_SHA256_ALGID) + _lp(digest))
    )
    certs_sec = _lp(_lp(cert_der))
    attrs_sec = _lp(b"")
    sdk_fields = struct.pack("<II", min_sdk, V3_MAX_SDK)

    def one_signer(is_v3: bool) -> bytes:
        sd_content = (
            digests_sec
            + certs_sec
            + (sdk_fields if is_v3 else b"")
            + attrs_sec
        )
        signature = deterministic_ecdsa_sign(private_key, sd_content)
        sigs_sec = _lp(
            _lp(struct.pack("<I", ECDSA_SHA256_ALGID) + _lp(signature))
        )
        body = (
            _lp(sd_content)
            + (sdk_fields if is_v3 else b"")
            + sigs_sec
            + _lp(spki_der)
        )
        return _lp(_lp(body))  # value = LP(LP(signer体))

    return one_signer(False), one_signer(True)


def _build_signing_block(v2_value: bytes, v3_value: bytes) -> bytes:
    pairs = b""
    for block_id, value in ((V2_BLOCK_ID, v2_value), (V3_BLOCK_ID, v3_value)):
        pairs += struct.pack("<Q", 4 + len(value))
        pairs += struct.pack("<I", block_id)
        pairs += value
    # size 字段统计的是其后所有字节：pairs + 尾部 size(8) + magic(16)
    size = len(pairs) + 8 + 16
    return (
        struct.pack("<Q", size)
        + pairs
        + struct.pack("<Q", size)
        + APK_SIG_BLOCK_MAGIC
    )


def sign_apk_deterministic(
    apk_path: Path,
    private_key: ec.EllipticCurvePrivateKey,
    cert_der: bytes,
    output_path: Path,
    min_sdk: int = 24,
) -> Path:
    """对未签名 APK 写入确定性 v2+v3 签名块，返回输出路径。"""
    if not apk_path.is_file():
        raise FileNotFoundError(f"找不到需要签名的 APK: {apk_path}")

    data = apk_path.read_bytes()
    eocd_off = _locate_eocd(data)
    cd_off = struct.unpack_from("<I", data, eocd_off + 16)[0]
    if not 0 < cd_off <= eocd_off:
        raise ValueError(f"非法的中央目录偏移: {cd_off}")
    if cd_off >= len(APK_SIG_BLOCK_MAGIC) and data[cd_off - len(APK_SIG_BLOCK_MAGIC):cd_off] == APK_SIG_BLOCK_MAGIC:
        raise ValueError("输入 APK 已包含签名块，请提供未签名的 ZIP/APK")

    entries = data[:cd_off]
    central_dir = data[cd_off:eocd_off]
    eocd = bytearray(data[eocd_off:])

    # 内容摘要：EOCD 的 CD 偏移字段按“指向签名块起点”参与摘要。
    # 我们的块恰好插在原 cd_off 处，因此输入 EOCD 原值即为所需值。
    digest = _content_digest([entries, bytes(central_dir), bytes(eocd)])

    v2_value, v3_value = _build_signer_values(
        private_key, cert_der, digest, min_sdk
    )
    block = _build_signing_block(v2_value, v3_value)

    # 写回：条目区 + 签名块 + 中央目录 + EOCD（CD 偏移加上块长度）
    struct.pack_into("<I", eocd, 16, cd_off + len(block))
    output_path.write_bytes(entries + block + central_dir + eocd)
    print(f"签名成功: {output_path.resolve()}")
    return output_path


def verify_with_apksigner(apk_path: Path, sdk_path: str | None) -> bool:
    """用参考实现 apksigner 校验自造签名块（可用时）。"""
    try:
        apksigner = find_apksigner(sdk_path)
    except FileNotFoundError as error:
        print(f"[!] 跳过 apksigner 自检: {error}")
        return True
    result = subprocess.run(
        [str(apksigner), "verify", "--print-certs", str(apk_path)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(result.stdout)
        print(result.stderr, file=sys.stderr)
        print("错误: apksigner verify 未通过，签名块与参考实现不兼容。", file=sys.stderr)
        return False
    print("[+] 自检: apksigner verify 通过")
    for line in result.stdout.splitlines():
        if "SHA-256" in line:
            print(f"    {line.strip()}")
    return True


def print_cert_fingerprints(cert_path: Path) -> None:
    """打印证书 DER 的 SHA-256 / SHA-1，用于核对每次构建是否稳定。"""
    der = cert_path.read_bytes()
    print(f"证书 SHA-256: {hashlib.sha256(der).hexdigest()}")
    print(f"证书 SHA-1:   {hashlib.sha1(der).hexdigest()}")


def parse_secexp(value: str) -> int:
    if not value or not isinstance(value, str):
        raise ValueError("secexp 不能为空")
    allowed = {
        "__builtins__": {},
        "abs": abs,
        "bin": bin,
        "hex": hex,
        "int": int,
        "oct": oct,
        "pow": pow,
    }
    try:
        result = eval(value, allowed, {})
    except Exception as exc:  # pragma: no cover
        raise ValueError(f"secexp 表达式无效: {value!r} ({exc})") from exc
    if isinstance(result, bool) or not isinstance(result, int):
        raise ValueError(f"secexp 必须求值为整数，当前值为 {result!r}")
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="使用 NIST256p 私钥确定性签名 APK")
    parser.add_argument("apk", type=Path, nargs="?", help="待签名 APK 路径")
    parser.add_argument("--mode", choices=("pem", "secexp"), default=None)
    parser.add_argument("--pem", type=Path, default=DEFAULT_PEM_PATH)
    parser.add_argument("--secexp", help="Python 整数表达式，例如 2**64")
    parser.add_argument("--sdk", help="Android SDK 路径（用于签名后 apksigner 自检），默认读取 ANDROID_HOME")
    parser.add_argument("--min-sdk", type=int, default=24, help="v3 签名块声明的 minSdk（默认 24）")
    parser.add_argument(
        "--test-deterministic",
        action="store_true",
        help="验证同一 key + message 的确定性 ECDSA 输出是否完全一致",
    )
    parser.add_argument(
        "--message",
        default="deterministic-ecdsa-test-message",
        help="用于确定性签名测试的消息内容",
    )
    args = parser.parse_args()
    if args.secexp is not None and args.mode is None:
        args.mode = "secexp"
    if args.mode is None:
        args.mode = "pem"
    return args


def main() -> int:
    args = parse_args()
    if args.mode == "pem":
        private_key = private_key_from_pem(args.pem)
    else:
        if args.secexp is None:
            raise SystemExit("--mode secexp 必须同时提供 --secexp")
        private_key = private_key_from_secexp(parse_secexp(args.secexp))

    if args.test_deterministic:
        # 1) 消息签名确定性
        payload = args.message.encode("utf-8")
        sig1 = deterministic_ecdsa_sign(private_key, payload)
        sig2 = deterministic_ecdsa_sign(private_key, payload)
        print(f"message={args.message!r}")
        print(f"sig1={sig1.hex()}")
        print(f"sig2={sig2.hex()}")
        print(f"same={sig1 == sig2}")

        # 2) 证书确定性
        cert1 = build_certificate_deterministic(private_key)
        cert2 = build_certificate_deterministic(private_key)
        print(f"证书 SHA-256 #1: {hashlib.sha256(cert1).hexdigest()}")
        print(f"证书 SHA-256 #2: {hashlib.sha256(cert2).hexdigest()}")
        print(f"证书完全一致: {cert1 == cert2}")
        return 0

    if args.apk is None:
        raise SystemExit("必须提供 APK 路径，或使用 --test-deterministic 仅做确定性签名测试")

    keystore = make_keystore(private_key, Path.home() / ".ssh")
    print_cert_fingerprints(Path(keystore["cert_path"]))
    cert_der = Path(keystore["cert_path"]).read_bytes()

    output_path = signed_path(args.apk)
    sign_apk_deterministic(
        args.apk, private_key, cert_der, output_path, min_sdk=args.min_sdk
    )
    if not verify_with_apksigner(output_path, args.sdk):
        return 1
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (FileNotFoundError, ValueError) as error:
        print(f"错误: {error}", file=sys.stderr)
        raise SystemExit(1)
