# Free Video Gen

开箱即用的 GeminiGen 视频生成脚本。

这个脚本会使用你本机 Chrome 里的 GeminiGen 登录态，请求 `https://geminigen.ai` 生成视频，自动轮询结果，并可把视频下载到本地。

## 环境要求

- Windows
- 已安装 Google Chrome
- Python 3.10+
- 一个可正常登录的 GeminiGen 账号

## 安装

```powershell
git clone https://github.com/ciouskeila-hue/freevideogen.git
cd freevideogen
python -m venv .venv
.\.venv\Scripts\activate
python -m pip install -r requirements.txt
```

## 第一次登录

运行：

```powershell
python geminigen_video_client.py login --extract
```

流程：

1. 脚本会自动打开 Chrome。
2. 你在 Chrome 里登录 GeminiGen。
3. 登录成功后关闭 Chrome。
4. 回到终端按 Enter。
5. 脚本会把本机登录态保存到 `geminigen_session.json`。

`geminigen_session.json` 只保存在本地，已经被 `.gitignore` 忽略，不会上传到 GitHub。

如果你本机 Chrome 已经登录过 GeminiGen，也可以直接测试：

```powershell
python geminigen_video_client.py auth-info --refresh
```

## 生成视频

最简单示例：

```powershell
python geminigen_video_client.py generate --prompt "一只电影感的猫宇航员飞过霓虹太空" --download output.mp4
```

常用参数示例：

```powershell
python geminigen_video_client.py generate `
  --prompt "夜晚赛博朋克城市，电影镜头，雨天霓虹灯" `
  --aspect-ratio landscape `
  --resolution 480p `
  --duration 6 `
  --download output.mp4
```

使用首帧图片：

```powershell
python geminigen_video_client.py generate --prompt "让这张图动起来" --first-frame image.jpg --download output.mp4
```

## 其他命令

查看当前登录状态：

```powershell
python geminigen_video_client.py auth-info
```

刷新并查看登录状态：

```powershell
python geminigen_video_client.py auth-info --refresh
```

查询历史任务：

```powershell
python geminigen_video_client.py history --uuid YOUR_HISTORY_UUID
```

查看帮助：

```powershell
python geminigen_video_client.py --help
python geminigen_video_client.py generate --help
```

## 不会上传到 GitHub 的文件

仓库已忽略这些本地文件：

- `geminigen_session.json`
- `geminigen_last_video.json`
- `geminigen_*.json`
- `.mp4` / `.mov` / `.avi` / `.mkv`
- `.jpg` / `.jpeg` / `.png` / `.webp`
- `.env`
- `.venv/`
- `__pycache__/`

## 常见问题

### 提示找不到 Chrome Local Storage

先运行：

```powershell
python geminigen_video_client.py login --extract
```

### 提示未安装 undetected-chromedriver

运行：

```powershell
python -m pip install -r requirements.txt
```

### 遇到 Turnstile / 人机验证

正常运行生成命令即可。接口要求 Turnstile 时，脚本会尝试自动打开浏览器获取 token。你需要保持 Chrome 可用，并完成浏览器里出现的验证。

### 没有下载到视频

脚本会把接口最终结果保存到：

```text
geminigen_last_video.json
```

如果返回里没有直接视频 URL，会提示 `No video URL found in result`，可以先查看这个 JSON 文件确认任务状态。
