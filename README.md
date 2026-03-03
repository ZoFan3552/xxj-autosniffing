# XXJ Auto Sniffing

基于 Tauri + React + TypeScript 开发的 HTTP/HTTPS 流量抓包与拦截工具，支持 Android 设备通过 ADB 自动配置代理。

## 功能特性

- 📡 **流量捕获**：实时捕获和展示 HTTP/HTTPS 请求响应
- 🛡 **请求拦截**：支持类似 Charles 的断点拦截功能，可修改请求/响应内容
- 🔐 **加解密支持**：集成自定义加解密接口，自动解密请求响应体
- 📱 **ADB 集成**：自动检测 Android 设备并配置代理
- 🎯 **智能过滤**：支持按 URL、Host、Method 过滤流量
- ⚙️ **灵活配置**：可配置代理端口、捕获规则、断点规则等

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
- **加解密接口**：用于解密请求响应的 HTTP 接口地址
- **捕获规则**：指定需要捕获的 Host 列表（支持后缀匹配）
- **断点规则**：配置拦截规则（URL 正则、拦截阶段等）

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
├── scripts/                # 构建脚本
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
A: 检查设置中的加解密接口地址是否正确配置

## License

内部项目，仅供公司内部使用
