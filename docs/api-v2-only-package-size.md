# API v2-only 打包体积记录

本文记录移除 GPT-SoVITS Gradio Web API、pydub 和 FFmpeg 后，Windows one-folder 构建的体积变化，供上游 PR 复核。

## 变更范围

- TTS 后端固定为 GPT-SoVITS `api_v2`，请求非流式 PCM WAV。
- 使用 Python 标准库 `wave` 和 `audioop` 调整 WAV 音量。
- Windows 播放固定使用 `winsound`。
- 删除 Gradio 客户端、后端选择 UI 和相关 PyInstaller 收集规则。
- 删除 `pydub` 依赖以及 `ffmpeg.exe`、`ffprobe.exe` 收集规则。
- 旧配置仅在 `tts_backend` 为 `api_v2` 时将 `gradio_server_url` 迁移到 `api_v2_url`。

这意味着构建不再支持 Gradio Web API，也不再提供非 Windows 的 `ffplay` 播放回退。项目自身的 Windows 桌面使用场景不受后者影响。

## 测量环境

- Windows 10 22H2 x64
- Python 3.12.13
- PyInstaller 6.22.0
- Node.js 24.19.0
- Next.js 14.2.7
- PyInstaller one-folder 构建，`upx=False`

基线为改动前实际安装的 one-folder 包；候选包由同一项目源码 clean build 得到。所有数字均为目录内普通文件的 `Length` 之和，`MiB = bytes / 1024 / 1024`。由于基线不是在当前 venv 中重新构建，结果代表实际发布包对比，而不是完全隔离的编译器基准。

## 结果

| 指标 | 改动前 | API v2-only | 变化 |
| --- | ---: | ---: | ---: |
| 文件数 | 905 | 939 | +34 |
| 总字节数 | 231,116,221 | 51,860,740 | -179,255,481 |
| 总体积 | 220.41 MiB | 49.46 MiB | -170.95 MiB |
| 主 EXE | 10,456,314 bytes | 9,903,883 bytes | -552,431 bytes |

总体积减少 **77.56%**。

基线中的 FFmpeg 文件为：

| 文件 | 字节数 | MiB |
| --- | ---: | ---: |
| `ffmpeg.exe` | 52,925,440 | 50.47 |
| `ffprobe.exe` | 122,135,040 | 116.48 |
| 合计 | 175,060,480 | 166.95 |

FFmpeg 占总减少量的 97.66%；其余 Gradio/pydub 代码和构建差异净减少 4,195,001 bytes（4.00 MiB）。候选包同时补齐了 `bilibili-api-python 17.2.0` 在运行时直接导入但未声明的依赖，因此文件数增加并不代表功能回退。

## 复现命令

```powershell
cd frontend
npm run build
cd ..

.venv\Scripts\python.exe -m unittest discover -s tests -v
.venv\Scripts\python.exe -m PyInstaller --clean --noconfirm bili_voice.spec
```

目录体积测量：

```powershell
$root = Resolve-Path .\dist\bili_voice
$files = Get-ChildItem -LiteralPath $root -Recurse -File
$bytes = ($files | Measure-Object Length -Sum).Sum

[pscustomobject]@{
    Files = $files.Count
    TotalBytes = $bytes
    TotalMiB = [math]::Round($bytes / 1MB, 2)
    ExeBytes = (Get-Item -LiteralPath (Join-Path $root 'bili_voice.exe')).Length
}
```

确认候选包不包含已移除组件：

```powershell
Get-ChildItem -LiteralPath .\dist\bili_voice -Recurse -File |
    Where-Object { $_.FullName -match '(?i)ffmpeg|ffprobe|ffplay|pydub|gradio' }
```

预期无输出。

## 验证结果

- 后端单元测试：6 项通过。
- Next.js production build：通过。
- `import backend.main`：通过。
- PyInstaller clean build：通过。
- 候选目录禁用组件扫描：无匹配。
- 打包 EXE 启动：通过；首页返回 HTTP 200，TTS health 返回 `backend=api_v2`，`/api/tts/enqueue` 路由存在。
