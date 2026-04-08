#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from pywidevine import Cdm, Device
from pywidevine.license_protocol_pb2 import ClientIdentification

EMULATOR_KEYWORDS = (
    "emulator",
    "sdk_",
    "sdk gphone",
    "sdk_gphone",
    "generic",
    "goldfish",
    "ranchu",
    "x86",
    "userdebug",
    "dev-keys",
    "avd",
    "genymotion",
)

BROWSER_PACKAGES = {
    "com.android.chrome",
    "org.mozilla.firefox",
    "com.microsoft.emmx",
    "com.brave.browser",
}


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def parse_header_version(raw: bytes) -> int | None:
    try:
        header = Device.Structures.header.parse(raw)
        return int(header.version)
    except Exception:
        return None


def get_client_info_map(device: Device) -> dict[str, str | list[str]]:
    result: dict[str, str | list[str]] = {}
    for item in device.client_id.client_info:
        if item.name not in result:
            result[item.name] = item.value
        else:
            current = result[item.name]
            if isinstance(current, list):
                current.append(item.value)
            else:
                result[item.name] = [current, item.value]
    return result


def get_client_capabilities(device: Device) -> dict[str, Any]:
    capabilities: dict[str, Any] = {}
    for field, value in device.client_id.client_capabilities.ListFields():
        capabilities[field.name] = list(value) if hasattr(value, "__iter__") and not isinstance(value, (str, bytes, bytearray, dict)) and field.label == field.LABEL_REPEATED else value
    return capabilities


def get_token_type_name(token_type_value: int) -> str:
    try:
        return ClientIdentification.TokenType.Name(token_type_value)
    except Exception:
        return f"UNKNOWN({token_type_value})"


def get_vmp_summary(device: Device) -> dict[str, Any]:
    signer = getattr(device.vmp, "signer", b"") or b""
    signatures = list(getattr(device.vmp, "signatures", []))
    return {
        "present": bool(getattr(device.client_id, "vmp_data", b"")),
        "raw_length": len(getattr(device.client_id, "vmp_data", b"")),
        "signer": signer.decode("utf-8", errors="replace") if signer else None,
        "signature_count": len(signatures),
        "files": [
            {
                "filename": getattr(sig, "filename", None),
                "test_signing": getattr(sig, "test_signing", None),
                "sha512_length": len(getattr(sig, "sha512", b"")),
            }
            for sig in signatures
        ],
    }


def probe_cdm(device: Device) -> dict[str, Any]:
    try:
        cdm = Cdm.from_device(device)
        session_id = cdm.open()
        cdm.close(session_id)
        return {"ok": True, "detail": "可以正常创建并关闭本地 CDM session"}
    except Exception as exc:
        return {"ok": False, "detail": f"CDM 本地探测失败: {type(exc).__name__}: {exc}"}


def classify_device(device: Device, client_info: dict[str, Any], vmp_summary: dict[str, Any]) -> dict[str, Any]:
    findings: list[dict[str, str]] = []
    suggestions: list[str] = []
    risk_score = 0

    application_name = str(client_info.get("application_name", ""))
    build_info = str(client_info.get("build_info", ""))
    model_name = str(client_info.get("model_name", ""))
    product_name = str(client_info.get("product_name", ""))
    device_name = str(client_info.get("device_name", ""))

    combined = " ".join(
        [
            application_name,
            build_info,
            model_name,
            product_name,
            device_name,
        ]
    ).lower()

    if device.security_level == 3:
        risk_score += 25
        findings.append(
            {
                "severity": "high",
                "title": "安全级别为 L3",
                "detail": "L3 通常更适合调试和本地研究，但在严格的服务端校验场景中更容易被拒绝。",
            }
        )
        suggestions.append("如果目标链路依赖严苛的服务端 license 校验，L3 设备通常不是最稳妥的选择。")
    elif device.security_level == 1:
        findings.append(
            {
                "severity": "info",
                "title": "安全级别为 L1",
                "detail": "L1 一般是更高信任级别，通常更适合高质量或更严格的 DRM 场景。",
            }
        )
    else:
        risk_score += 10
        findings.append(
            {
                "severity": "medium",
                "title": f"安全级别为 L{device.security_level}",
                "detail": "不是最差也不是最佳，是否可用仍取决于服务端策略。",
            }
        )

    if any(keyword in combined for keyword in EMULATOR_KEYWORDS):
        risk_score += 35
        findings.append(
            {
                "severity": "critical",
                "title": "命中模拟器/开发机构建特征",
                "detail": "检测到了 emu/sdk/x86/userdebug/dev-keys 等关键词；这类设备画像在服务端风控里通常风险更高。",
            }
        )
        suggestions.append("如果你追求更高成功率，优先考虑更接近真实消费设备画像的 WVD。")

    if application_name in BROWSER_PACKAGES:
        risk_score += 15
        findings.append(
            {
                "severity": "medium",
                "title": "application_name 指向浏览器包名",
                "detail": f"当前 application_name={application_name}，更像浏览器环境而不是原生媒体 App。",
            }
        )
        suggestions.append("浏览器来源的 WVD 在某些媒体服务上可能比原生 App 画像更容易触发限制。")

    if not vmp_summary["present"]:
        risk_score += 15
        findings.append(
            {
                "severity": "high",
                "title": "未检测到 VMP 数据",
                "detail": "Verified Media Path (VMP) 为空；这不等于文件坏了，但会降低某些场景下的可信度。",
            }
        )
        suggestions.append("无 VMP 不代表必定失败，但它通常不是加分项。")

    if application_name == "com.spotify.music":
        findings.append(
            {
                "severity": "info",
                "title": "application_name 命中 Spotify 包名",
                "detail": "从设备画像角度看，这通常比普通浏览器环境更接近目标媒体应用。",
            }
        )

    if device.type.name == "CHROME":
        risk_score += 10
        findings.append(
            {
                "severity": "medium",
                "title": "WVD 设备类型为 CHROME",
                "detail": "CHROME 类型通常与浏览器播放链路强相关，不同服务的接受度差异较大。",
            }
        )

    risk_score = max(0, min(risk_score, 100))
    if risk_score >= 70:
        risk_band = "very-high"
    elif risk_score >= 40:
        risk_band = "high"
    elif risk_score >= 20:
        risk_band = "medium"
    else:
        risk_band = "low"

    if not suggestions:
        suggestions.append("从静态画像上看没有明显高风险特征，但最终是否可用仍以服务端响应为准。")

    return {
        "risk_score": risk_score,
        "risk_band": risk_band,
        "findings": findings,
        "suggestions": suggestions,
    }


def build_votify_compatibility(device: Device, classification: dict[str, Any]) -> list[dict[str, str]]:
    notes: list[dict[str, str]] = []

    notes.append(
        {
            "path": "librespot + vorbis",
            "uses_wvd": "no",
            "assessment": "WVD 不是这条链路的核心依赖。该链路更依赖账号权限、librespot 会话和服务端对 Ogg/metadata/token 的放行。",
        }
    )
    notes.append(
        {
            "path": "web + AAC / 部分视频",
            "uses_wvd": "yes",
            "assessment": "这是 WVD 直接参与的主要链路之一。即使文件能本地加载，也仍然可能在向服务端请求 Widevine license 时被拒绝。",
        }
    )
    notes.append(
        {
            "path": "desktop + Spotify.dll",
            "uses_wvd": "no",
            "assessment": "这条链路主要依赖桌面会话与 DLL，不以 WVD 为主。",
        }
    )

    if classification["risk_band"] in {"high", "very-high"}:
        notes.append(
            {
                "path": "当前 WVD 静态画像结论",
                "uses_wvd": "n/a",
                "assessment": "从静态特征看，这份 WVD 在严格校验场景中的风险偏高；更适合作为研究/调试样本，而不是优先候选。",
            }
        )
    else:
        notes.append(
            {
                "path": "当前 WVD 静态画像结论",
                "uses_wvd": "n/a",
                "assessment": "从静态特征看没有特别突出的高风险项，但是否可用仍要看账号与服务端策略。",
            }
        )

    if device.security_level == 3:
        notes.append(
            {
                "path": "高质量/更严格 DRM 场景",
                "uses_wvd": "yes",
                "assessment": "L3 设备在某些更严格的质量层级或授权接口上可能天然更吃亏。",
            }
        )

    return notes


def build_report(path: Path, include_cdm_probe: bool) -> dict[str, Any]:
    raw = path.read_bytes()
    device = Device.load(path)
    client_info = get_client_info_map(device)
    capabilities = get_client_capabilities(device)
    vmp_summary = get_vmp_summary(device)
    cdm_probe = probe_cdm(device) if include_cdm_probe else {"ok": None, "detail": "未执行本地 CDM 探测"}
    classification = classify_device(device, client_info, vmp_summary)

    report = {
        "file": {
            "path": str(path.resolve()),
            "size": path.stat().st_size,
            "sha256": sha256_file(path),
            "wvd_header_version": parse_header_version(raw),
        },
        "device": {
            "type": device.type.name,
            "security_level": device.security_level,
            "system_id": device.system_id,
            "flags": device.flags,
            "private_key_bits": int(device.private_key.size_in_bits()),
            "private_key_der_length": len(device.private_key.export_key("DER")),
            "client_id_length": len(device.client_id.SerializeToString()),
            "client_id_token_type": get_token_type_name(device.client_id.type),
        },
        "client_info": client_info,
        "client_capabilities": capabilities,
        "vmp": vmp_summary,
        "local_probe": cdm_probe,
        "classification": classification,
        "votify_compatibility": build_votify_compatibility(device, classification),
    }
    return report


def render_text(report: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("WVD 检测报告")
    lines.append("=" * 60)
    lines.append("")

    lines.append("[文件]")
    for key, value in report["file"].items():
        lines.append(f"- {key}: {value}")
    lines.append("")

    lines.append("[设备摘要]")
    for key, value in report["device"].items():
        lines.append(f"- {key}: {value}")
    lines.append("")

    lines.append("[本地探测]")
    lines.append(f"- ok: {report['local_probe']['ok']}")
    lines.append(f"- detail: {report['local_probe']['detail']}")
    lines.append("")

    lines.append("[风险评估]")
    lines.append(f"- score: {report['classification']['risk_score']}")
    lines.append(f"- band: {report['classification']['risk_band']}")
    for item in report["classification"]["findings"]:
        lines.append(f"  - ({item['severity']}) {item['title']}: {item['detail']}")
    lines.append("")

    lines.append("[建议]")
    for item in report["classification"]["suggestions"]:
        lines.append(f"- {item}")
    lines.append("")

    lines.append("[VMP]")
    for key, value in report["vmp"].items():
        if key == "files":
            lines.append(f"- {key}: {len(value)} entries")
        else:
            lines.append(f"- {key}: {value}")
    lines.append("")

    lines.append("[Client Capabilities]")
    for key, value in report["client_capabilities"].items():
        lines.append(f"- {key}: {value}")
    lines.append("")

    lines.append("[Client Info]")
    for key, value in report["client_info"].items():
        lines.append(f"- {key}: {value}")
    lines.append("")

    lines.append("[Votify 兼容性解读]")
    for item in report["votify_compatibility"]:
        lines.append(f"- {item['path']} | uses_wvd={item['uses_wvd']} | {item['assessment']}")

    return "\n".join(lines)


def render_markdown(report: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("# WVD 检测报告")
    lines.append("")

    lines.append("## 1. 文件摘要")
    lines.append("")
    for key, value in report["file"].items():
        lines.append(f"- **{key}**: `{value}`")
    lines.append("")

    lines.append("## 2. 设备摘要")
    lines.append("")
    for key, value in report["device"].items():
        lines.append(f"- **{key}**: `{value}`")
    lines.append("")

    lines.append("## 3. 本地探测")
    lines.append("")
    lines.append(f"- **ok**: `{report['local_probe']['ok']}`")
    lines.append(f"- **detail**: {report['local_probe']['detail']}")
    lines.append("")

    lines.append("## 4. 风险评估")
    lines.append("")
    lines.append(f"- **risk_score**: `{report['classification']['risk_score']}`")
    lines.append(f"- **risk_band**: `{report['classification']['risk_band']}`")
    lines.append("")
    if report["classification"]["findings"]:
        for item in report["classification"]["findings"]:
            lines.append(f"- **[{item['severity']}] {item['title']}**：{item['detail']}")
    else:
        lines.append("- 未发现显著风险特征。")
    lines.append("")

    lines.append("## 5. 建议")
    lines.append("")
    for item in report["classification"]["suggestions"]:
        lines.append(f"- {item}")
    lines.append("")

    lines.append("## 6. VMP 摘要")
    lines.append("")
    for key, value in report["vmp"].items():
        if key == "files":
            lines.append(f"- **{key}**: `{len(value)}` entries")
        else:
            lines.append(f"- **{key}**: `{value}`")
    lines.append("")

    lines.append("## 7. Client Capabilities")
    lines.append("")
    for key, value in report["client_capabilities"].items():
        lines.append(f"- **{key}**: `{value}`")
    lines.append("")

    lines.append("## 8. Client Info")
    lines.append("")
    for key, value in report["client_info"].items():
        lines.append(f"- **{key}**: `{value}`")
    lines.append("")

    lines.append("## 9. Votify 兼容性解读")
    lines.append("")
    for item in report["votify_compatibility"]:
        lines.append(f"- **{item['path']}**（uses_wvd=`{item['uses_wvd']}`）：{item['assessment']}")
    lines.append("")

    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="本地 WVD 检测脚本：只做读取、摘要与静态风险分析。")
    parser.add_argument("path", nargs="?", default="device.wvd", help="待检测的 .wvd 文件路径，默认是当前目录下的 device.wvd")
    parser.add_argument("--format", choices=("text", "markdown", "json"), default="text", help="输出格式")
    parser.add_argument("--output", help="将结果写入文件")
    parser.add_argument("--no-cdm-probe", action="store_true", help="跳过本地 CDM open/close 探测")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    path = Path(args.path).expanduser()
    if not path.exists():
        raise SystemExit(f"文件不存在: {path}")

    report = build_report(path, include_cdm_probe=not args.no_cdm_probe)

    if args.format == "json":
        content = json.dumps(report, indent=2, ensure_ascii=False)
    elif args.format == "markdown":
        content = render_markdown(report)
    else:
        content = render_text(report)

    if args.output:
        output_path = Path(args.output).expanduser()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(content, encoding="utf-8")
    else:
        print(content)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
