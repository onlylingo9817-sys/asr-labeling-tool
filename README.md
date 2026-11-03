# ASR 标注工具（可克隆版）

本地中文语音校对/说话人标注工具：支持多录音、毫秒级起止时间、嵌套插话、人工确认与 Label Studio 写回。

## 5 分钟上手（只需 Python 3）

默认只启动编辑器，自带 12 秒合成示例音频，不包含任何客户录音。

```bash
git clone https://github.com/onlylingo9817-sys/asr-labeling-tool.git
cd asr-labeling-tool
./scripts/start_all.sh
```

打开 http://127.0.0.1:8765/

停止：`./scripts/stop_all.sh`，状态：`./scripts/status.sh`

> 启动脚本使用 macOS `screen`。Linux 可自行把它替换为 systemd/tmux；不能依赖后台 PID 在终端退出后继续存活。
>
> Windows 目前没有一键启动脚本：安装 Python 3 后，可运行 `python app\editor\serve_editor.py --port 8765`，再打开 http://127.0.0.1:8765/ 。

## 换成自己的录音

1. 把音频放在 `app/editor/audio/`（建议 16k/单声道 WAV）。
2. 更新 `app/editor/data.json`：每个 recording 的 `taskId`、`audio`、`waveform`、`duration`、`utterances`。
3. 为每条录音生成波形：

```bash
ffmpeg -i app/editor/audio/my.wav -filter_complex showwavespic=s=1400x160:colors=#1d4ed8 -frames:v 1 app/editor/waveforms/my.png
```

4. 刷新页面。音频、句表、波形会按 taskId 切换。

**不要把客户录音、Label Studio 数据库、声纹库提交到 Git。** 见 `.gitignore`。

## 用 FunASR 生成底稿

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app/interjection_nested.py --audio my.wav --out my_nested.json
```

首次运行会下载 FunASR / 说话人模型，体积较大且耗时。生成 JSON 后按 `app/editor/data.json` 格式合并为 recordings。

## 写回 Label Studio（可选）

1. 启动本地 Label Studio：`label-studio start --port 8080`
2. 复制配置：`cp .env.example .env`
3. 在 `.env` 设置 `ENABLE_SYNC=1`，必要时设置 `LABEL_STUDIO_URL`、`LS_BIN`、`LS_READ_ONLY_TASKS`
4. `./scripts/start_all.sh`

写回规则：

- 人工确认（`manual/reference` 或 `reviewed=true`）→ Annotation
- 机器待定 → Prediction
- 已有 Annotation 不静默覆盖，页面会提示再次确认
- 每次写回前自动备份数据库
- 嵌套插话按自己的起止时间和说话人写成独立 region

## 声纹分组（可选，实验性）

需要额外安装：

```bash
pip install sherpa-onnx soundfile funasr
```

并准备两个模型：

- `sherpa-onnx-pyannote-segmentation-3-0`
- `3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx`

在 `.env` 设置 `ENABLE_VOICEPRINT=1` 及模型/音频映射：

```ini
VOICE_AUDIO_JSON={"5":"app/editor/audio/my.wav"}
SEG_MODEL=/path/to/segmentation/model.onnx
EMB_MODEL=/path/to/campplus.onnx
```

声纹结果只是辅助分组，短插话和重叠话仍会出错，不能替代人工试听。

## 目录

```text
app/editor/      静态校对页 + Range 音频服务 + 示例数据
app/sync/        Label Studio 写回服务
app/voiceprint/  可选声纹服务
app/asr_pipeline.py / interjection_nested.py  可选 ASR 处理
scripts/         启停与状态
```

## 已知边界

- FunASR 车型名/数字/英文缩写会有识别错误
- 短插话说话人不稳，必须听音确认
- 数字中文“四五十万/两万九千八/百分之”不能安全全自动替换；`app/number_diff.py` 只生成复核清单
- 本仓默认只带合成示例，不提供客户数据


## 多项目与从 Label Studio 拉取

编辑器顶部现在有「项目」下拉框，录音列表和浏览器本地草稿都按 project 隔离。不同 project 下即使 task id 相同，也不会互相串数据。

1. 在 `.env` 设置 `ENABLE_SYNC=1` 并启动同步服务。
2. 打开 http://127.0.0.1:8765/ ，在顶部选择项目。
3. 点「⇣ 从 Label Studio 拉取」。
4. 拉取规则：
   - 只新增当前编辑器里没有的 task；
   - 已存在的 task 会跳过，不覆盖本地修改；
   - 没有 annotation 的 task 也会拉入，显示为“未标注”，方便先听音频；
   - 音频复制为 `app/editor/audio/project<项目>_task<task>.wav`，不同项目不会互相覆盖；
   - 同时生成该录音的独立波形。
5. 同步写回时会校验 `projectId` 与 task 的真实项目；不一致会拒绝写入，避免把 A 项目的标注写到 B 项目。

原有 `data.json` 中没有 `projectId` 的旧录音，建议补上 `projectId` 后再做多项目操作。
