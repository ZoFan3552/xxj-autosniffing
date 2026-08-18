# XXJ Auto Sniffing

基于 Tauri + React + TypeScript 开发的 HTTP/HTTPS 流量抓包与拦截工具，支持 Android 设备通过 ADB 自动配置代理。

## 功能特性

- 📡 **流量捕获**：实时捕获和展示 HTTP/HTTPS 请求响应
- 🛡 **请求拦截**：支持类似 Charles 的断点拦截功能，可修改请求/响应内容
- 🎭 **Mock 规则**：按 URL 正则预先声明「直接应答」或「局部改写」，命中后无需人工介入
- 🔐 **加解密支持**：本地密钥在进程内加解密，或退回外部加解密接口
- 🔌 **WebSocket**：记录每一帧，并可把录好的帧序列按原时刻回放给客户端
- 📤 **代发请求**：从已抓到的流量里取出身份，以学习机的身份主动调接口
- 📱 **ADB 集成**：自动检测 Android 设备并配置代理
- 🎯 **智能过滤**：支持按 URL、Host、Method 过滤流量
- ⚙️ **灵活配置**：可配置代理端口、捕获规则、断点规则、Mock 规则等

## 技术栈

- **前端**：React 19 + TypeScript + Vite
- **后端**：Rust + Tauri 2.0
- **代理核心**：Python + mitmproxy
- **UI 组件**：react-json-view-lite、@tanstack/react-virtual

## 环境要求

- Node.js 16+
- Rust 1.70+
- Python 3.8+
- ADB (Android Debug Bridge) - 可选，用于 Android 设备代理配置

## 安装依赖

```bash
# 安装前端依赖
npm install

# 构建 Python addon（首次运行或 Python 代码变更后需要）
python scripts/build_addon.py
```

## 开发

```bash
# 启动开发服务器
npm run dev

# 或直接启动 Tauri 开发模式
npm run tauri dev
```

## 构建

### Windows

```bash
# 构建安装包（NSIS + MSI）
npm run build:win

# 构建可执行文件（不打包）
npm run build:win:noinstaller
```

### macOS

```bash
# 构建 .app 和 .dmg
npm run build:mac
```

## 使用说明

### 基础使用

1. 启动应用后，点击右上角"启动"按钮开启代理服务
2. 配置设备的 HTTP 代理指向应用显示的代理地址（默认 8080 端口）
3. 在"流量"标签页查看捕获的请求
4. 在"拦截"标签页处理断点拦截的请求

### Android 设备配置

1. 通过 USB 连接 Android 设备并启用 USB 调试
2. 应用会自动检测设备并配置代理
3. ADB 状态栏会显示当前连接的设备

### 配置说明

在"设置"标签页可配置：

- **代理端口**：本地代理服务监听端口
- **本地密钥**：填了就在本机做加解密，不再调用加解密接口。算法与学习机的 CBB 网络 SDK 一致：
  明文 → gzip → AES-128-ECB/PKCS7 → base64，AES 密钥取 `SHA1(密钥)[:16]`。
  密钥若以 Base64 形式保存（与 App 源码里的存法一致），勾上下方的复选框
- **加解密接口**：拿不到本地密钥时的后备，POST 纯文本；解密接口直接返回明文，
  加密接口返回 `{"data": "<密文>"}`
- **捕获规则**：指定需要捕获的 Host 列表（支持后缀匹配）
- **断点规则**：配置拦截规则（URL 正则、拦截阶段等）
- **Mock 规则**：URL 正则 + 应答方式。「直接应答」不向服务端发请求，直接返回规则里写明的
  响应体；「局部改写」照常发请求，再把 JSON 片段按 RFC 7386 合并进真实响应。
  可限制命中次数与延迟毫秒数。密文体在网络上有裸 base64 与外面套一层引号两种形态，
  「局部改写」跟随被替换的原响应，「直接应答」因为是凭空构造、无从判断，固定发裸 base64

一条流量的处理顺序是：先看 Mock 规则，命中就不再触发断点；都没命中才正常转发。

### WebSocket

"WebSocket"标签页列出本次代理运行中的连接与每一帧（方向、相对握手时刻的偏移、内容）。
选中一条连接点"装填回放"，下一条握手在同一 path 上的连接就会被这批帧顶替：真实帧上下行
都被吞掉（回放就是要代替服务端应答，放真实下行帧混进来只会污染），录好的下行帧按录制时的
相对时刻注回客户端。回放只生效一次，之后的连接回到真实服务端。

### 代发请求

"代发"标签页以学习机的身份主动调接口，用于探测接口是否部署、返回是否正确，不必在设备上
把界面点一遍。身份（凭证请求头与请求体里的 `base` 信封）从已抓到的设备流量里取得，
所以要先让设备发过一次该 host 的请求。代发的请求绕回代理自身，因此和设备流量一样会被记录，
也一样会被 Mock 规则命中；流量列表里用"代发"标签区分。

## 项目结构

```
.
├── src/                    # React 前端源码
│   ├── components/         # UI 组件
│   ├── utils/              # 工具函数
│   └── types.ts            # TypeScript 类型定义
├── src-tauri/              # Tauri Rust 后端
│   └── src/
│       ├── python/         # Python 代理脚本
│       ├── adb_manager.rs  # ADB 管理模块
│       ├── proxy_runner.rs # 代理进程管理
│       └── commands.rs     # Tauri 命令接口
├── scripts/                # 构建脚本与端到端自检
└── public/                 # 静态资源
```

## 开发建议

### IDE 配置

- [VS Code](https://code.visualstudio.com/)
- 推荐插件：
  - Tauri
  - rust-analyzer
  - ESLint
  - Prettier

### 调试

- 前端：使用浏览器开发者工具（Tauri 窗口右键 -> Inspect Element）
- Rust 后端：查看终端日志输出
- Python 代理：日志输出到 Tauri 控制台

## 常见问题

**Q: 代理启动失败？**  
A: 检查端口是否被占用，或 Python 环境是否正确配置

**Q: ADB 无法连接设备？**  
A: 确保已安装 ADB 并添加到系统 PATH，设备已启用 USB 调试

**Q: 无法解密请求内容？**  
A: 检查设置里的本地密钥是否正确；未填本地密钥时，检查加解密接口地址是否配置正确

**Q: 代发请求提示"还没抓到该 host 的身份"？**  
A: 身份只能从流经代理的设备流量里取得。先在设备上触发一次该 host 的、带 Authorization 的请求

## 自检

```bash
# addon 内部逻辑自检（加解密、JSON 合并、规则匹配）
python src-tauri/src/python/addon_bridge.py --selftest

# 端到端：拉起真实 mitmproxy 与本地上游，验证记录、Mock、断点、代发、WS 回放
python scripts/e2e_addon.py

# 同一套用例改用 PyInstaller 打好的 sidecar 跑，用来查打包时漏掉的 import
XXJ_ADDON_CMD=src-tauri/binaries/addon_bridge-<target-triple> python scripts/e2e_addon.py
```

## License

内部项目，仅供公司内部使用
