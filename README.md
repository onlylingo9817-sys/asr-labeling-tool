# ASR 标注工具（可克隆版）

本地中文语音校对/说话人标注工具：支持多录音、毫秒级起止时间、嵌套插话、人工确认与 Label Studio 写回。

## 5 分钟上手（只需 Python 3）

默认只启动编辑器，自带 12 秒合成示例音频，不包含任何客户录音。

```bash
git clone https://github.com/onlylingo9817-sys/asr-labeling-tool.git
cd asr-labeling-tool
./scripts/doctor.sh
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

同步规则（当前版本）：

- 点一次同步，就把编辑器当前全部句子覆盖写入 Annotation
- 已选标签会一起写回，标签值是英文 `overlap / accent / unclear / cut`
- 没有 Annotation 时创建，已有 Annotation 时覆盖
- 写回前自动备份数据库
- 嵌套插话按自己的起止时间和说话人写成独立 region
- 项目 ID 与 task 实际所属项目不一致时拒绝写回

## 环境与服务面板

页面右上角「⚙ 环境与服务」可检测 ffmpeg / ASR / 声纹 / Label Studio 连接，并一键安装、启用或重启各项服务（由 `app/setup_server.py` 提供，端口 8766，随 `scripts/start_all.sh` 启动）。

## 声纹分组（可选，实验性）

需要额外安装：

```bash
pip install -r requirements.txt
```

声纹模型（Cam++）首次使用时会自动下载，无需手动准备。

在 `.env` 设置 `ENABLE_VOICEPRINT=1`。默认会按 `app/editor/data.json` 中每个录音的 `audio` 字段自动解析音频，支持多项目、任意 task id：

```ini
ENABLE_VOICEPRINT=1
```

如果需要覆盖某条录音的音频路径，可额外设置：

```ini
VOICE_AUDIO_JSON={"5":"app/editor/audio/my.wav"}
```

`/group`、`/enroll`、`/candidates`、`/apply` 建议同时传 `taskId` 和 `projectId`；旧客户端只传 `taskId` 仍然兼容。

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
6. 没有 annotation 的录音会以“未标注”拉入；可以点「＋ 新增一句」手动开始录入。

原有 `data.json` 中没有 `projectId` 的旧录音，建议补上 `projectId` 后再做多项目操作。


## 预处理能力（音频 / 波形 / ASR）

从 Label Studio 拉取 task 时，工具会自动：

1. 将音频转为 16k 单声道 16-bit WAV；
2. 生成独立波形图；
3. 如果该 task 没有 Annotation，自动运行 FunASR，生成带毫秒时间、嵌套插话的底稿；
4. 已存在 Annotation 的 task 不会被 ASR 覆盖，保留原有句子和时间。

编辑器顶部还有「✦ 生成 ASR 底稿」，可对当前录音重新生成；这会替换该录音在编辑器里的句子，请先确认。

运行 ASR 需要额外环境：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

首次运行 FunASR 会下载模型，30 分钟录音通常需要数分钟。若不想自动跑 ASR，可在 `.env` 设置：

```ini
AUTO_ASR=0
```

音频、波形始终会生成，不依赖 ASR。


## 机器可执行的安装指引

如果你要让 AI/同事把仓库装到可用状态，直接读 `AI_SETUP.md`，并先运行：

```bash
./scripts/doctor.sh
```

`doctor.sh` 会明确列出缺少的 ffmpeg、ASR 环境或 Label Studio 连接，不会用模糊的“失败”代替。
