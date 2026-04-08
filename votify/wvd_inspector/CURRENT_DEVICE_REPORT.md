# WVD 检测报告

## 1. 文件摘要

- **path**: `<repo_root>/device.wvd`
- **size**: `3504`
- **sha256**: `07ea04dff4cb82a1cd1040cfe6d4aad52e48cec3834d6cc46b6f12c45ad3728a`
- **wvd_header_version**: `2`

## 2. 设备摘要

- **type**: `ANDROID`
- **security_level**: `3`
- **system_id**: `28926`
- **flags**: `{}`
- **private_key_bits**: `2048`
- **private_key_der_length**: `1191`
- **client_id_length**: `2302`
- **client_id_token_type**: `DRM_DEVICE_CERTIFICATE`

## 3. 本地探测

- **ok**: `True`
- **detail**: 可以正常创建并关闭本地 CDM session

## 4. 风险评估

- **risk_score**: `90`
- **risk_band**: `very-high`

- **[high] 安全级别为 L3**：L3 通常更适合调试和本地研究，但在严格的服务端校验场景中更容易被拒绝。
- **[critical] 命中模拟器/开发机构建特征**：检测到了 emu/sdk/x86/userdebug/dev-keys 等关键词；这类设备画像在服务端风控里通常风险更高。
- **[medium] application_name 指向浏览器包名**：当前 application_name=com.android.chrome，更像浏览器环境而不是原生媒体 App。
- **[high] 未检测到 VMP 数据**：Verified Media Path (VMP) 为空；这不等于文件坏了，但会降低某些场景下的可信度。

## 5. 建议

- 如果目标链路依赖严苛的服务端 license 校验，L3 设备通常不是最稳妥的选择。
- 如果你追求更高成功率，优先考虑更接近真实消费设备画像的 WVD。
- 浏览器来源的 WVD 在某些媒体服务上可能比原生 App 画像更容易触发限制。
- 无 VMP 不代表必定失败，但它通常不是加分项。

## 6. VMP 摘要

- **present**: `False`
- **raw_length**: `0`
- **signer**: `None`
- **signature_count**: `0`
- **files**: `0` entries

## 7. Client Capabilities

- **client_token**: `True`
- **session_token**: `True`
- **max_hdcp_version**: `0`
- **oem_crypto_api_version**: `17`
- **anti_rollback_usage_table**: `False`
- **can_update_srm**: `False`
- **supported_certificate_key_type**: `[0]`
- **analog_output_capabilities**: `1`
- **can_disable_analog_output**: `False`
- **resource_rating_tier**: `1`

## 8. Client Info

- **application_name**: `com.android.chrome`
- **origin**: `AF967AE6CC671E6368B56CCF588172C7`
- **package_certificate_hash_bytes**: `8P1sW0EPJcslw7UzRsiXL64w+O50Ed+RBICtay1g24M=`
- **company_name**: `Google`
- **model_name**: `sdk_gphone64_x86_64`
- **architecture_name**: `x86_64`
- **device_name**: `emu64x`
- **product_name**: `sdk_gphone64_x86_64`
- **build_info**: `google/sdk_gphone64_x86_64/emu64x:13/TE1A.240213.009/12342917:userdebug/dev-keys`
- **widevine_cdm_version**: `17.0.0`
- **oem_crypto_security_patch_level**: `0`
- **oem_crypto_build_information**: `OEMCrypto Level3 Code Feb  2 2023 05:37:47 28926 X86 64bit APIv17.1`

## 9. Votify 兼容性解读

- **librespot + vorbis**（uses_wvd=`no`）：WVD 不是这条链路的核心依赖。该链路更依赖账号权限、librespot 会话和服务端对 Ogg/metadata/token 的放行。
- **web + AAC / 部分视频**（uses_wvd=`yes`）：这是 WVD 直接参与的主要链路之一。即使文件能本地加载，也仍然可能在向服务端请求 Widevine license 时被拒绝。
- **desktop + Spotify.dll**（uses_wvd=`no`）：这条链路主要依赖桌面会话与 DLL，不以 WVD 为主。
- **当前 WVD 静态画像结论**（uses_wvd=`n/a`）：从静态特征看，这份 WVD 在严格校验场景中的风险偏高；更适合作为研究/调试样本，而不是优先候选。
- **高质量/更严格 DRM 场景**（uses_wvd=`yes`）：L3 设备在某些更严格的质量层级或授权接口上可能天然更吃亏。
## 10. 结合当前仓库上下文的补充判断

基于我对这个仓库当前环境的实际测试，还可以补充两点：

- 当前 `cookies.txt` 对应账号状态是 `FREE`
- 在当前仓库环境下，这份 `.wvd` 虽然能本地加载并通过 CDM open/close 探测，但在真实下载链路里请求 Widevine license 时仍然收到 `403`

因此，这份样本更准确的结论是：

> **文件本身没坏，但在当前环境里不适合作为严格 Spotify Widevine 链路的优先候选。**

换句话说，它更像：

- 本地研究样本
- 结构验证样本
- 风险画像示例样本

而不是当前这个仓库里最稳的生产候选样本。
