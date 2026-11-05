# ASR 标注工具使用说明

## 一、本机怎么打开

在终端执行：

```bash
cd /Users/mi/Documents/Codex/2026-11-03/lu
./scripts/start_all.sh
./scripts/status.sh
```

浏览器打开：

```text
http://127.0.0.1:8765/
```

停止本机服务：

```bash
cd /Users/mi/Documents/Codex/2026-11-03/lu
./scripts/stop_all.sh
```

三个服务说明：

| 服务 | 端口 | 用途 |
|---|---:|---|
| 校对页 | 8765 | 浏览器标注界面 |
| 同步服务 | 8768 | 从 Label Studio 拉取任务、写回标注 |
| 声纹服务 | 8769 | 可选，声纹分组 |

---

## 二、同事第一次安装

同事在 Mac 上执行：

```bash
git clone https://github.com/onlylingo9817-sys/asr-labeling-tool.git
cd asr-labeling-tool
./scripts/start_all.sh
```

打开：

```text
http://127.0.0.1:8765/
```

**然后点页面右上角「⚙ 环境与服务」**：它会检测 ffmpeg、ASR/声纹环境、模型、Label Studio 连接和各服务状态，缺什么给一个按钮——一键安装 ASR/声纹环境、一键安装/启动 Label Studio、预下载模型、一键启用/重启/关闭同步与声纹服务，还能切换声纹模型。需要管理员权限的步骤（如 Linux 装 ffmpeg）只会给出命令，不会擅自提权。

仓库自带一段 12 秒合成示例，不需要客户数据就能打开界面。

如需启用 Label Studio 拉取/写回：

```bash
cp .env.example .env
```

编辑 `.env`，至少设置：

```ini
ENABLE_SYNC=1
```

然后重新执行：

```bash
./scripts/start_all.sh
```

Windows 没有一键脚本；安装 Python 3 后运行：

```text
python app\editor\serve_editor.py --port 8765
```

---

## 三、选择项目

页面顶部有三组控件：

```text
项目  [下拉框]   ⇣ 从 Label Studio 拉取   语音 [下拉框]
```

### 项目下拉框

列出 Label Studio 中的全部项目，例如：

```text
语料标注 (#1)
New Project #2 (#2)
语料标记-new (#4)
ASR底稿试标（勿动正式） (#5)
```

选择哪个项目，下面的语音列表只显示那个项目的 task。

### 从 Label Studio 拉取

选择项目后，点击：

```text
⇣ 从 Label Studio 拉取
```

它的行为：

- 只拉取当前项目里**本地还没有**的 task。
- 已存在的 task 会跳过，不覆盖本地修改。
- 没有标注的 task 也会拉进来，显示为“未标注”。
- 音频保存为：

```text
app/editor/audio/project<项目ID>_task<taskID>.wav
```

- 同时生成独立波形。
- 拉取结束会提示新增、跳过、缺音频的数量。

如果项目刚新建，还没有 task，会显示：

```text
该项目暂无录音，请点“从 Label Studio 拉取”
```

---

## 四、标注流程

### 1. 选语音

在「语音」下拉框选择 task，例如：

```text
N70-5 · 191 句
```

如果是未标注录音：

```text
project5 / task 9 · 未标注
```

### 2. 听音频

- 点「▶」播放当前句。
- 空格键播放/暂停。
- J / K 上一句 / 下一句。
- 可以拖动进度条，也可以点击波形定位。
- 播放速度可选 0.5×–2×。

### 3. 修改文本

每一行右侧是转写文本，直接点击修改。

包含嵌套插话的句子，正文和插话分开显示：

```text
啊现在就是一个纯电优先
↳ 嗯  02:04.090–02:04.325
```

插话有自己的时间、文本和说话人标签。

### 4. 标注说话人

每行左侧有说话人下拉框，可选项包括：

```text
SALES_1 ... SALES_4
CUSTOMER_1 ... CUSTOMER_5
CAR_VOICE
OTHER
UNKNOWN
BACKGROUND
```

选择后该句立即变成“人工确认”。

### 5. 修改嵌套插话说话人

句中的“嗯/啊”下方有插话标签，标签旁有独立下拉框。

例如：

```text
↳ 嗯  02:04.090–02:04.325  [CUSTOMER_1 ▼]  [拆出]
```

把这里改成顾客，父句仍可以是销售。

### 6. 合并与拆分

- 点「⤵ 合并(M)」把当前句和下一句合并。
- 合并后说话人取字数更多的一方。
- 拆分有两种：「✂ 按光标拆(S)」按字数比例估算切点，可能不准；「⏱ 按播放位置拆(T)」按当前播放位置拆，最准。详见文末「拆分与调整分界」。
- 插话旁的「拆出」会按插话自己的毫秒区间生成独立句。
- 支持撤销/重做。

### 7. 打标签

每行下方单独一行“标签”显示的是 Label Studio 的实际英文标签值：

> 注意：同一区域里的「→ 顾客1 + 抢话(C)」「→ 说话人未知(U)」是**快捷操作**，不是标签。C 会把说话人设为 CUSTOMER_1 **并**加上 overlap；U 只把说话人设为 UNKNOWN，**不会**加 unclear。

```text
overlap / accent / unclear / cut
```

中文含义可以悬停查看：抢话 / 口音 / 听不清 / 截断。

同步时写回的也是这些英文值，界面不会再做翻译。

### 8. 新增一句

如果录音是“未标注”，点列表上方的：

```text
＋ 新增一句
```

可以先手工建一句，再填时间和文本。

---

## 五、同步回 Label Studio

确认完若干句后，点右上角：

```text
↥ 同步到 Label Studio
```

默认规则是**以页面当前内容为最终结果、覆盖写入**：

- 默认只同步前/中/尾三段窗口内的句子（页面顶部「仅同步三段内」可取消，取消后同步全部）；
- 点同步后会先检查，并弹窗列出：写入句数、未逐句确认的句数、仍为 UNKNOWN 的句数、标签数，以及将被替换的现有 Annotation；
- 没改过的机器建议不需要逐句点确认，同步时一并写入；
- 当前窗口内的每一句都会写回；
- 已选标签会一起写回；
- 已有的 Annotation 会被当前状态覆盖；
- 写回前自动备份数据库；
- 项目 ID 与 task 实际所属项目不一致时拒绝写回。

也就是说，不需要先“人工确认”才能同步。页面上看到什么，同步后 Label Studio 就应当是什么。

如果任务还没有 Annotation，第一次同步会创建；之后再次同步会覆盖更新。

## 六、浏览器本地修改

所有修改先保存在浏览器本地。

切换项目、切换录音、刷新页面都不会丢。

但它只保存在**当前浏览器**里。换电脑或清空浏览器数据后，未同步的内容会丢失。

因此标注一段后应及时点「同步到 Label Studio」。

---

## 七、常见问题

### 打开页面没有项目

确认同步服务已启动：

```bash
./scripts/status.sh
```

应看到：

```text
[running] editor port=8765
[running] sync port=8768
```

### 点“从 Label Studio 拉取”失败

检查：

1. Label Studio 是否运行在 `http://localhost:8080`。
2. `.env` 中是否设置 `ENABLE_SYNC=1`。
3. 当前 Label Studio 账号是否有对应项目权限。

### 某条 task 没拉进来

拉取结果会提示“缺音频/无标注”。

常见原因：

- task 的 `data.audio` 指向的文件不在本机 Label Studio media 目录。
- 该 task 没有 annotation。

### 标注写回后项目里看不到

确认：

- 当前项目是否选对；
- 是否写成了 Prediction 而不是 Annotation；
- Label Studio 页面是否需要刷新。

---

## 八、数据安全

不要提交到 Git：

- 客户原始录音
- Label Studio 数据库
- 声纹文件
- 其他个人信息

公开仓库只包含代码和 12 秒合成示例。

本机同步备份位于：

```text
work/sync_backups/
```

---

## 九、最短操作卡

```text
1. ./scripts/start_all.sh
2. 打开 http://127.0.0.1:8765/
3. 顶部选项目
4. 点“从 Label Studio 拉取”
5. 选一条语音
6. 听音、改文本、定说话人
7. 点“同步到 Label Studio”
```


## 预处理与 ASR

「从 Label Studio 拉取」会自动完成音频标准化、波形生成。没有 Annotation 的 task 会自动生成 ASR 底稿；已有 Annotation 的 task 只补音频和波形，不改原有句子。

编辑器顶部「✦ 生成 ASR 底稿」可重跑当前录音。拉取时的 ASR 在后台运行并显示进度（每条约 1–2 分钟，请不要重复点击）。ASR 较慢，建议先在「⚙ 环境与服务」里一键安装独立的 Python 环境（或在 `.env` 配置）：

```ini
ASR_PYTHON=/path/to/venv/bin/python
AUTO_ASR=1
```

不想自动识别时设 `AUTO_ASR=0`，仍会生成音频和波形。


## 拆分与调整分界

- `S`：按光标处拆分，时间点按字数比例估算，**可能不准**。
- `T` 或「⏱ 按播放位置拆」：先播放/暂停到想拆的时间点，再拆，切点就是播放位置，最准。
- 拆完后，句首时间下的 `◀ ▶` 可微调与上一句的分界（±0.1 秒，按住 Shift ±0.02 秒）。
- 拆错了：撤销（⌘Z），或对两句点「合并」后重新拆。
