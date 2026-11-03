# 快速开始（转发版）

## 1. 克隆并打开

```bash
git clone https://github.com/onlylingo9817-sys/asr-labeling-tool.git
cd asr-labeling-tool
./scripts/doctor.sh
./scripts/start_all.sh
```

浏览器打开：

```text
http://127.0.0.1:8765/
```

停止：

```bash
./scripts/stop_all.sh
```

## 2. 安装完整处理能力

基础编辑器只要有 Python 3 就能打开；要处理 Label Studio 音频、生成波形和 ASR 底稿，需要 ffmpeg 和 FunASR：

```bash
# macOS
brew install ffmpeg
# Ubuntu
# sudo apt install ffmpeg

./scripts/setup.sh asr
cp .env.example .env
./scripts/doctor.sh
```

`doctor.sh` 输出 `RESULT: READY` 即基础环境可用。

## 3. 连接 Label Studio

确保 Label Studio 在本机运行于 `http://localhost:8080`，然后编辑 `.env`：

```ini
ENABLE_SYNC=1
LABEL_STUDIO_URL=http://localhost:8080
LS_BIN=/绝对路径/label-studio
AUTO_ASR=1
```

重启：

```bash
./scripts/stop_all.sh
./scripts/start_all.sh
```

## 4. 使用流程

1. 顶部选择项目。
2. 点「⇣ 从 Label Studio 拉取」。
3. 已有 Annotation 的 task：保留原句子，补音频和波形。
4. 没有 Annotation 的 task：自动跑 ASR，生成文本、毫秒时间、嵌套插话和 3 段窗口。
5. 听音校对文本和说话人。
6. 点「↥ 同步到 Label Studio」；当前页面全部句子会覆盖写入 Annotation。
7. 标签使用英文值：`overlap / accent / unclear / cut`。

## 5. 注意事项

- 当前一键启停脚本按 macOS 验证；Linux 需要 `screen`；Windows 需手动运行 `python app\editor\serve_editor.py --port 8765`。
- ASR 较慢，首次会下载模型；30 分钟录音通常需要数分钟。
- 声纹分组是实验功能，短插话和重叠话仍会出错，不能替代试听。
- 音频必须能被本机 Label Studio media 目录访问；多人共享时建议部署在同一台内部服务器。
- 不要提交客户录音、数据库或声纹文件到 Git。

需要给 AI 自动安装时，让它先读 `AI_SETUP.md` 并运行 `./scripts/doctor.sh`。
