# WVD Inspector

这个文件夹专门放 **`.wvd` 本地检测脚本、使用步骤、概念说明和当前样本报告**。

> 目标：
> - 只做本地读取、静态分析、兼容性判断
> - 不提取、不生成、不修改 `.wvd`
> - 帮你判断 **“这个 `.wvd` 看起来像什么设备、风险在哪里、在 votify 里大概会走哪条链路”**

---

## 目录结构

- `<repo_root>/votify/wvd_inspector/inspect_wvd.py`
  - 本地 `.wvd` 检测脚本
- `<repo_root>/votify/wvd_inspector/README.md`
  - 说明文档、WVD 介绍、类型与作用、操作步骤
- `<repo_root>/votify/wvd_inspector/CURRENT_DEVICE_REPORT.md`
  - 对当前 `<repo_root>/device.wvd` 的详细分析报告

---

## 一、什么是 `.wvd`

`.wvd` 一般可以理解成 **Widevine Device** 的设备凭据封装文件。

它通常包含：

- 设备类型（例如 Android / Chrome）
- 安全级别（例如 L1 / L2 / L3）
- 设备私钥
- Client ID（客户端身份信息）
- 可能存在的 VMP（Verified Media Path）相关数据
- 设备画像信息（如包名、机型、构建信息、公司名等）

对本项目 `votify` 来说，`.wvd` 的主要作用不是“登录账号”，而是：

- 在 **web/AAC/部分视频/部分 Widevine 流程** 下，参与本地 CDM（Content Decryption Module）工作
- 用来生成或配合 Widevine license 流程
- 最终帮助拿到媒体解密所需的关键数据

也就是说：

- `cookies.txt` 更偏向 **账号身份 / 会话**
- `.wvd` 更偏向 **设备身份 / DRM 能力**

两者不是一回事，缺一也可能失败。

---

## 二、`.wvd` 在 `votify` 里的作用

### 1. 哪些链路会直接用到 `.wvd`

在这个项目里，大体可以这么理解：

#### A. `web + AAC / 某些视频`
这条链路会明显依赖 `.wvd`。

原因：
- 需要本地 CDM
- 需要生成 Widevine 相关请求
- 服务端是否下发 license，和 `.wvd` 画像强相关

#### B. `librespot + vorbis`
这条链路 **不是主要依赖 `.wvd`**。

更依赖：
- 账号类型
- librespot 会话是否被接受
- metadata/token/audio key 是否能拿到

#### C. `desktop + Spotify.dll`
这条链路主要依赖：
- 桌面 session
- `Spotify.dll`

`.wvd` 不是主角。

---

## 三、`.wvd` 常见类型与作用

这个“类型”可以从多个维度看。

### 1. 按设备类型分

当前 `pywidevine` 这里常见能识别到的有：

- `ANDROID`
- `CHROME`

#### `ANDROID`
更像 Android 设备/Android App 体系下的设备画像。

常见用途：
- Android 播放链路
- 某些依赖 Android CDM 的场景

#### `CHROME`
更像浏览器类设备画像。

常见用途：
- 浏览器相关链路
- 和 Web 环境更接近的身份

> 注意：**设备类型不是“越像浏览器越好”或“越像 Android 越好”**。
> 是否可用，取决于目标服务端怎么识别、怎么放行。

---

### 2. 按安全级别分

常见是：

- `L1`
- `L2`
- `L3`

#### `L1`
通常表示更高等级的安全环境。

一般特征：
- 更容易用于高质量、要求更严格的 DRM 场景
- 更接近服务端偏好的高信任设备

#### `L2`
介于中间，实际场景相对少见。

#### `L3`
通常是最常见、也最容易见到的本地研究样本级别。

一般特征：
- 适合调试、分析、本地验证
- 但在严格服务端风控或 license 校验时更容易被拒

> 重要：
> **L3 不等于坏文件，也不等于一定失败。**
> 它只是通常意味着“服务端不一定信任你”。

---

### 3. 按设备来源画像分

#### 真机画像
更接近正常消费设备。

特征可能包括：
- 真实机型名
- 正常生产构建信息
- 更像真实 App 的包名
- 较少命中 emulator / userdebug / dev-keys / x86 等特征

#### 模拟器画像
更偏开发/测试环境。

常见信号：
- `sdk_gphone`
- `emu`
- `x86_64`
- `userdebug`
- `dev-keys`

这类并不是文件损坏，但 **往往更容易被服务端风控盯上**。

#### 浏览器画像
例如：
- `com.android.chrome`
- Firefox/Edge/Brave 等包名

这种画像不是绝对不能用，但在媒体服务场景中，**不一定比原生媒体 App 画像更稳**。

---

### 4. 按 VMP 状态分

#### 带 VMP
VMP（Verified Media Path）可以理解为一组“更完整的媒体路径验证材料”。

通常作用：
- 提高某些场景下的可信度
- 在更严格的服务端校验里可能更有利

#### 不带 VMP
不代表文件坏。

但通常意味着：
- 画像可信度不一定高
- 某些服务端链路更容易拒绝

---

## 四、为什么一个 `.wvd` 本地能加载，线上却仍然 403

这是最容易误会的一点。

### 本地能加载，说明什么？
只说明：

- 文件格式没坏
- 私钥/Client ID 结构可被 `pywidevine` 解析
- 本地 CDM 初始化可能成功

### 本地能加载，不说明什么？
**不说明服务端一定会接受它。**

服务端仍然可能因为这些原因拒绝：

- 账号权限不足
- 设备画像不被接受
- `.wvd` 对应环境太像模拟器/测试机
- 缺 VMP
- 当前音质/流格式要求更高
- 当前服务端策略已收紧

所以：

> **“能 load” ≠ “能拿到 Widevine license”**

---

## 五、这个检测脚本能做什么

`inspect_wvd.py` 会做这些事：

1. 读取 `.wvd` 文件
2. 输出文件摘要
   - 路径
   - 大小
   - SHA-256
   - 头版本
3. 输出设备摘要
   - type
   - security level
   - system id
   - private key 长度/位数
   - client id token type
4. 输出 Client Info
   - application_name
   - model_name
   - build_info
   - company_name
   - 等等
5. 输出 Client Capabilities
6. 检测 VMP 状态
7. 做一次 **本地 CDM open/close 探测**
   - 只验证本地是否能正常创建 session
   - 不发网络请求
8. 做静态风险判断
   - 是否像模拟器
   - 是否像浏览器
   - 是否 L3
   - 是否无 VMP
9. 给出 `votify` 场景下的兼容性解读

---

## 六、脚本操作步骤

### 方式 A：直接查看文本输出

```bash
cd <repo_root>
./.venv/bin/python <repo_root>/votify/wvd_inspector/inspect_wvd.py <repo_root>/device.wvd
```

### 方式 B：输出 Markdown 报告

```bash
cd <repo_root>
./.venv/bin/python <repo_root>/votify/wvd_inspector/inspect_wvd.py <repo_root>/device.wvd --format markdown
```

### 方式 C：保存成 Markdown 文件

```bash
cd <repo_root>
./.venv/bin/python <repo_root>/votify/wvd_inspector/inspect_wvd.py \
  <repo_root>/device.wvd \
  --format markdown \
  --output <repo_root>/votify/wvd_inspector/my_report.md
```

### 方式 D：输出 JSON

```bash
cd <repo_root>
./.venv/bin/python <repo_root>/votify/wvd_inspector/inspect_wvd.py \
  <repo_root>/device.wvd \
  --format json
```

### 方式 E：跳过本地 CDM 探测

```bash
cd <repo_root>
./.venv/bin/python <repo_root>/votify/wvd_inspector/inspect_wvd.py \
  <repo_root>/device.wvd \
  --no-cdm-probe
```

---

## 七、怎么理解输出结果

### 1. `security_level`
- `1`：通常更强
- `3`：通常更容易被服务端拒绝，但不代表文件坏

### 2. `application_name`
- 如果像 `com.spotify.music`：通常更接近原生媒体 App 画像
- 如果像 `com.android.chrome`：更偏浏览器环境

### 3. `build_info`
如果出现这类关键词，通常是高风险信号：
- `sdk`
- `emu`
- `x86`
- `userdebug`
- `dev-keys`

### 4. `vmp.present`
- `true`：有 VMP
- `false`：无 VMP，不等于坏，但可能更弱

### 5. `local_probe.ok`
- `true`：本地 CDM 结构上能用
- `false`：文件可能损坏、依赖有问题、或本地解析失败

> 再强调一次：
> `local_probe.ok=true` 也 **不代表服务端一定会发 license**。

---

## 八、当前样本的经验性判断标准

如果你看到下面这种组合：

- `security_level = 3`
- `application_name = com.android.chrome`
- `model/build_info` 明显像模拟器
- `vmp.present = false`

那通常可以把它理解为：

> **本地可读，但线上严格链路风险很高。**

这种 `.wvd` 更像：
- 研究样本
- 调试样本
- 本地结构验证样本

而不是“优先推荐拿去跑严格服务端 license 的候选样本”。

---

## 九、对 `votify` 当前场景的实战理解

在这个仓库里，你要区分三个问题：

### 问题 1：账号有没有权限
例如：
- FREE / PREMIUM
- 是否 on-demand

### 问题 2：`.wvd` 本地结构是否正常
这正是本脚本负责检查的重点。

### 问题 3：服务端是否接受这份 `.wvd`
这是最难的一层。

脚本只能帮你判断：
- 这份 `.wvd` 静态画像是否高风险
- 是否明显像模拟器/浏览器/L3/无 VMP

但最终是否通过，仍然要看：
- 当前 cookies
- 当前账号类型
- 当前媒体类型与质量
- 当时的服务端策略

---

## 十、建议的使用顺序

建议你以后按这个顺序判断：

1. **先跑 `inspect_wvd.py`**
   - 看它是不是明显高风险画像
2. **再看账号状态**
   - FREE / PREMIUM
3. **再看下载链路**
   - librespot / web / desktop
4. **最后再看服务端错误**
   - metadata 403
   - token 403
   - widevine license 403
   - audio key 403

这样你不会把所有问题都误归因给 `.wvd`。

---

## 十一、当前样本报告

当前样本的详细报告见：

- `<repo_root>/votify/wvd_inspector/CURRENT_DEVICE_REPORT.md`
