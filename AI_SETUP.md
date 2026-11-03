# AI 安装与验收指引

这份文件是给自动化助手/同事看的。目标：把仓库安装到“编辑器可打开、ASR 可运行、可选连接浏览器里的 Label Studio”的状态。不要提交客户音频、数据库或声纹文件。

## 先做只读检查

```bash
cd asr-labeling-tool
./scripts/doctor.sh
```

期望：
- `RESULT: READY` 表示基础编辑器可用。
- 如果 `ffmpeg/ffprobe` 缺失，先安装系统依赖。
- `funasr` 是可选能力；缺失时编辑器仍可打开，只是不能自动生成 ASR 底稿。

## 基础安装

```bash
# macOS
brew install ffmpeg
# Ubuntu
# sudo apt install ffmpeg
```

```bash
./scripts/setup.sh base
cp -n .env.example .env
./scripts/start_all.sh
./scripts/status.sh
curl -fsS http://127.0.0.1:8765/health
```

浏览器打开 http://127.0.0.1:8765/ ，应看到示例录音和 3 句示例。

## 安装 ASR（可选但推荐）

```bash
./scripts/setup.sh asr
```

然后在 `.env` 里确认：

```ini
AUTO_ASR=1
ASR_PYTHON=/绝对路径/asr-labeling-tool/.venv/bin/python
```

验证：

```bash
./scripts/doctor.sh
# 应出现 [ok] funasr available
```

第一次跑 ASR 会下载模型，30 分钟录音通常需要数分钟。

## 接 Label Studio（可选）

前提：Label Studio 已经在 `http://localhost:8080` 运行，并且本机有 `label-studio` CLI。`sync_server.py` 只在需要刷新 API token 时调用 CLI。

```bash
# .env
ENABLE_SYNC=1
LABEL_STUDIO_URL=http://localhost:8080
LS_BIN=/绝对路径/label-studio
```

重开：

```bash
./scripts/stop_all.sh
./scripts/start_all.sh
./scripts/doctor.sh
```

编辑器顶部应出现项目下拉，并能点「⇣ 从 Label Studio 拉取」。

## ASR / 音频 / 波形链路

拉取 task 时：

1. 复制 Label Studio 的 `data.audio` 对应文件；
2. 用 ffmpeg 规范化为 16k 单声道 WAV；
3. 生成独立波形；
4. 若 task 没有 Annotation，并且 `AUTO_ASR=1`，调用 `app/interjection_nested.py` 生成底稿；
5. 已有 Annotation 的 task 不跑 ASR，保留原句子。

编辑器顶部「✦ 生成 ASR 底稿」可以重跑当前录音。

## 验收清单

- [ ] `./scripts/doctor.sh` 返回 READY
- [ ] `curl http://127.0.0.1:8765/health` 返回 200
- [ ] 页面能切换示例录音
- [ ] 选择“未标注”task 时有音频和波形
- [ ] 点「生成 ASR 底稿」后出现句子和 3 个窗口
- [ ] 拉取已标注 task 时句子不变
- [ ] 同步写回后 Label Studio Annotation 里有 tags（overlap/accent/unclear/cut）

## 不要做

- 不要把 `*.wav`、`*.sqlite3`、`voiceprints.json`、`work/logs/` 提交到 Git。
- 不要把客户音频放到公开仓库。
- 不要把 ASR 生成结果当成最终人工标注；必须试听校对。
