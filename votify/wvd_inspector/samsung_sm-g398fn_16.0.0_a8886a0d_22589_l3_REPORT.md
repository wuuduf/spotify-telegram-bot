# WVD 检测报告

## 1. 文件摘要

- **path**: `<repo_root>/samsung_sm-g398fn_16.0.0_a8886a0d_22589_l3.wvd`
- **size**: `3357`
- **sha256**: `237d50f9cc7f9d3a64fc03d89accc0da2b414764a2d798b2658e0b3a67d197bb`
- **wvd_header_version**: `2`

## 2. 设备摘要

- **type**: `ANDROID`
- **security_level**: `3`
- **system_id**: `22589`
- **flags**: `{}`
- **private_key_bits**: `2048`
- **private_key_der_length**: `1190`
- **client_id_length**: `2156`
- **client_id_token_type**: `DRM_DEVICE_CERTIFICATE`

## 3. 本地探测

- **ok**: `True`
- **detail**: 可以正常创建并关闭本地 CDM session

## 4. 风险评估

- **risk_score**: `40`
- **risk_band**: `high`

- **[high] 安全级别为 L3**：L3 通常更适合调试和本地研究，但在严格的服务端校验场景中更容易被拒绝。
- **[high] 未检测到 VMP 数据**：Verified Media Path (VMP) 为空；这不等于文件坏了，但会降低某些场景下的可信度。

## 5. 建议

- 如果目标链路依赖严苛的服务端 license 校验，L3 设备通常不是最稳妥的选择。
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
- **oem_crypto_api_version**: `16`
- **anti_rollback_usage_table**: `False`
- **can_update_srm**: `False`
- **supported_certificate_key_type**: `[0]`
- **analog_output_capabilities**: `1`
- **can_disable_analog_output**: `False`
- **resource_rating_tier**: `1`

## 8. Client Info

- **application_name**: `com.kaltura.kalturadeviceinfo`
- **company_name**: `samsung`
- **model_name**: `SM-G398FN`
- **architecture_name**: `arm64-v8a`
- **device_name**: `xcover4s`
- **product_name**: `xcover4seea`
- **build_info**: `samsung/xcover4seea/xcover4s:11/RP1A.200720.012/G398FNXXUGCVE6:user/release-keys`
- **widevine_cdm_version**: `16.0.0`
- **oem_crypto_security_patch_level**: `0`
- **oem_crypto_build_information**: `OEMCrypto Level3 Code 22589 May 28 2021 19:37:19`

## 9. Votify 兼容性解读

- **librespot + vorbis**（uses_wvd=`no`）：WVD 不是这条链路的核心依赖。该链路更依赖账号权限、librespot 会话和服务端对 Ogg/metadata/token 的放行。
- **web + AAC / 部分视频**（uses_wvd=`yes`）：这是 WVD 直接参与的主要链路之一。即使文件能本地加载，也仍然可能在向服务端请求 Widevine license 时被拒绝。
- **desktop + Spotify.dll**（uses_wvd=`no`）：这条链路主要依赖桌面会话与 DLL，不以 WVD 为主。
- **当前 WVD 静态画像结论**（uses_wvd=`n/a`）：从静态特征看，这份 WVD 在严格校验场景中的风险偏高；更适合作为研究/调试样本，而不是优先候选。
- **高质量/更严格 DRM 场景**（uses_wvd=`yes`）：L3 设备在某些更严格的质量层级或授权接口上可能天然更吃亏。
