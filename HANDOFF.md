# ASR 标注工具交接文档

> 生成时间：2026-11-03 19:36 CST  
> 项目目录：`/Users/mi/Documents/Codex/2026-11-03/lu`  
> 发布仓库：https://github.com/onlylingo9817-sys/asr-labeling-tool  
> 最新提交：`6647df9 Add reset to base draft with double confirmation`

新会话请先读完本文件，再按“当前服务状态 → 立即待修问题 → 已验证功能 → 操作约束”执行。

---

## 一、项目是什么

本地中文试驾录音标注工具，能力包括：

- 多项目 / 多 task 隔离
- Label Studio 项目选择与任务拉取
- 音频标准化、波形生成
- FunASR 自动底稿（文本、毫秒时间、嵌套“嗯/啊”插话）
- 说话人标注、插话独立说话人
- 合并、拆分、撤销/重做、重置为底稿
- 当前完整状态同步回 Label Studio Annotation
- 可选声纹分组（实验性）

---

## 二、当前正在运行的服务

| 服务 | 端口 | 当前状态 |
|---|---:|---|
| 编辑器 | 8765 | 运行中，pid 1691 |
| 同步服务 | 8768 | 运行中，pid 1718 |
| 声纹服务 | 8769 | **未运行** |

健康检查：

```bash
cd /Users/mi/Documents/Codex/2026-11-03/lu
./scripts/status.sh

curl -sS http://127.0.0.1:8765/health   # 200
curl -sS http://127.0.0.1:8768/health   # 200
curl -sS http://127.0.0.1:8769/health   # 当前 connection refused
```

浏览器地址：http://127.0.0.1:8765/

启停：

```bash
./scripts/stop_all.sh
./scripts/start_all.sh
./scripts/status.sh
```

服务由 macOS `screen` 托管，日志在 `work/logs/`。

---

## 三、当前阻断问题：声纹分组 Failed to fetch

### 现象

页面点击「🎙 声纹配置 → 开始分组」提示：

```text
Failed to fetch
```

### 根因

1. `8769` 声纹服务没有启动。
2. 项目根目录没有 `.env`。
3. `scripts/start_all.sh` 默认 `ENABLE_VOICEPRINT=0`，因此启动时会打印：

```text
[skip] voiceprint (set ENABLE_VOICEPRINT=1 in .env to enable)
```

浏览器会直接 `fetch('http://127.0.0.1:8769/group')`，端口没有服务，所以报 `Failed to fetch`，不是算法返回的错误。

### 启动声纹服务

```bash
cd /Users/mi/Documents/Codex/2026-11-03/lu
cat > .env <<'EOF'
ENABLE_VOICEPRINT=1
VOICE_PY=/Users/mi/Documents/Codex/2026-11-03/lu/work/asr-venv/bin/python
EOF
./scripts/stop_all.sh
./scripts/start_all.sh
./scripts/status.sh
curl -sS http://127.0.0.1:8769/health
```

### 声纹服务额外限制

- `work/voiceprints.json` 当前是空库：

```json
{"settings":{"threshold":0.06,"maxProfiles":6},"profiles":{}}
```

- 声纹服务代码 `work/voiceprint_server.py` 的音频映射写死为 task 5–8：

```python
AUDIO = {tid: ROOT/f'work/editor/audio/task{tid}.wav' for tid in (5,6,7,8)}
```

因此：

- task 6（N90）音频存在，服务启动后可以分组。
- task 9（project 5）不在映射里，声纹/登记会失败；需要改成按 `data.json` 动态读取音频，或显式补映射。
- `profiles` 为空时，`/apply`、`/candidates` 会报“还没有注册声纹样本”，但 `/group` 使用无监督聚类，不依赖 profiles。

### 为什么 N90 现在全是 UNKNOWN

N90 最近重新生成了 ASR：

- 当前本地数据：941 句，全部 `speaker=UNKNOWN`
- ASR 输出只负责文本、时间和插话，不负责说话人；`preprocess.asr_utterances()` 会明确把所有 speaker 初始化为 `UNKNOWN`
- 所以这不是“声纹把标签清了”，而是重新生成底稿后的正常初始状态

要恢复说话人：

1. 启动声纹服务
2. 打开「🎙 声纹配置」
3. 点「开始分组」，逐组试听并确认角色
4. 或手工逐句选择说话人

---

## 四、当前编辑数据（以 `work/editor/data.json` 为准）

| task | project | 来源 | 句数 | 说话人概况 |
|---|---:|---|---:|---|
| 5 | 4 | N70-5 | 191 | SALES_1 132 / CUSTOMER_1 51 / CUSTOMER_2 4 / UNKNOWN 4 |
| 6 | 4 | N90-7 | 941 | 全部 UNKNOWN |
| 7 | 4 | SU7-6 | 208 | SALES_1 120 / CUSTOMER_1 70 / CUSTOMER_2 18 |
| 8 | 4 | YU7-6 | 170 | 已有标签 |
| 9 | 5 | project5 / task 9 | 97 | 全部 UNKNOWN |

### N90 窗口现状

- 窗口定义：前段 `120–300`、中段 `1020.62–1200.62`、尾段 `1981.24–2161.24`
- 句子计数：front 114、middle 80、tail 0、full 747

说明：

- ASR 原始识别最后一句到约 `1897.88s`，尾段 `1981.24s` 之后没有语音，所以 tail 没有句子。
- 当前 `window=full` 的 747 句，是落在三个窗口之外的句子；页面选择某个窗口时只过滤显示该窗口句子，“全部”仍会显示全部。
- 新会话如果要求“只保留三段内句子”，需要明确改变过滤/裁剪策略；当前实现不是只保留 3 段。

### Label Studio 数据库状态（真实库）

数据库：`/Users/mi/Library/Application Support/label-studio/label_studio.sqlite3`

当前 Annotation：

- task 5：Annotation id=1，1 个结果，**人工原始标注，禁止删除/覆盖**
- task 6：Annotation id=5，219 个结果（最近一次同步的旧版；本地已重新 ASR 为 941 句，尚未同步）
- task 7：Annotation id=4，177 个结果
- task 8：无 Annotation

备份：`work/sync_backups/`

---

## 五、最近已修复并可验证

### 1. 撤销 / 重做

- 改说话人、改标签、候选说话人、文本编辑、快捷说话人现在都会写入撤销历史
- 撤销后可重做
- 已浏览器验证：改 `SALES_1 → SALES_2 → 撤销 → 重做`，值正确恢复

### 2. 重置为底稿

新增按钮：`⟲ 重置为底稿`

- 两次确认
- 清除当前录音的全部本地标记、标签、合并/拆分
- 重新载入服务端 ASR 底稿
- 重置前会下载当前草稿 JSON 备份
- 重置后停留在同一条录音
- 已浏览器验证：乱改文本后重置，恢复原始底稿

### 3. 生成 ASR 底稿

- 无业务限制，任何时候可重新生成
- 两次确认
- 服务端先把旧 `data.json` 备份到 `work/sync_backups/editor-task<id>-...-before-asr.json`
- 成功后清除浏览器旧草稿并立即加载新结果
- 返回窗口计数

### 4. 同步到 Label Studio

当前页面规则：

- 点同步 = 当前页面全部句子覆盖写入 Annotation
- 标签使用英文 `overlap / accent / unclear / cut`
- 嵌套插话按自己的起止和说话人写为独立 region
- 同步前自动备份数据库
- 同步后清理该 task 的旧 `tasks_annotationdraft`，避免 Label Studio 继续打开旧草稿
- 项目 ID 不匹配会拒绝写回

### 5. 拉取 / 预处理

- 顶部有项目下拉
- 「⇣ 从 Label Studio 拉取」只新增本地没有的 task
- 已有 task 跳过，不覆盖本地修改
- 拉取时转 16k 单声道 WAV、生成波形
- 没有 Annotation 的 task 自动跑 ASR（`AUTO_ASR=1`）
- 已有 Annotation 的 task 不跑 ASR，保留原句子

### 6. 三段窗口规则

`work/make_segments.py` 现在保证：

- `duration > 0` 时始终返回 3 段
- `>= 9 分钟` 使用标准三段布局
- `< 9 分钟` 按实际时长切 3 段

已验证：0、150、540、900、1680.84、2221.24 秒。

---

## 六、代码与发布版位置

### 本地开发/运行版

- 编辑器：`work/editor/index.html`
- 编辑器静态服务：`work/editor/serve_editor.py`
- 同步/拉取/ASR 接口：`work/sync_server.py`
- 预处理与 ASR 转换：`work/preprocess.py`
- 三段窗口：`work/make_segments.py`
- 声纹服务：`work/voiceprint_server.py`
- 当前数据：`work/editor/data.json`

### 发布版

- 目录：`dist/asr-labeling-tool`
- GitHub 已推送
- 最新发布提交：`6647df9`

已比对：

- `work/editor/index.html` 与发布版一致
- `work/preprocess.py` 与发布版一致
- `work/make_segments.py` 与发布版一致
- `work/voiceprint_server.py` 与发布版**不完全一致**（发布版做了路径适配）；声纹修复后需要再次同步并回归

---

## 七、重要接口

同步服务 `http://127.0.0.1:8768`：

- `GET /health`
- `GET /projects`
- `POST /pull`：`{projectId, force?, includeEmpty?}`
- `POST /asr`：`{projectId, taskId}`
- `POST /sync`：`{projectId, taskId, utterances, mode, replaceAnnotation?, dryRun?}`

声纹服务 `http://127.0.0.1:8769`：

- `GET /health`
- `GET /state`
- `POST /enroll`
- `POST /group`
- `POST /candidates`
- `POST /apply`

---

## 八、安全约束（必须遵守）

1. **不要删除或覆盖 task 5 的 Annotation id=1**。
2. 任何真实 Label Studio 数据库写操作前都必须备份。
3. 测试写回/ASR 替换优先使用数据库或编辑器数据副本，不要直接对真实库做试验。
4. 不要提交客户录音、Label Studio 数据库、`voiceprints.json`、模型文件到 Git。
5. ASR 结果不是最终标注，必须人工试听确认。
6. 声纹分组只是辅助，短插话、重叠话、车机声仍会错。

---

## 九、新会话建议执行顺序

1. 读本文件，运行 `./scripts/status.sh`。
2. 修复声纹服务启动：
   - 创建 `.env`，设置 `ENABLE_VOICEPRINT=1` 与 `VOICE_PY`
   - `./scripts/stop_all.sh && ./scripts/start_all.sh`
   - `curl http://127.0.0.1:8769/health`
3. 用浏览器验证「开始分组」不再报 `Failed to fetch`。
4. 修复 `voiceprint_server.py` 对 task 9 / 动态任务音频的支持。
5. 决定 N90 的 747 句 `full` 是否要裁掉，只保留三段。
6. 让 N90 的说话人从 UNKNOWN 恢复：声纹分组 + 人工确认，或重新标注。
7. 把 N90 的新 941 句与 Label Studio task 6 的旧 Annotation 做一次明确同步。
8. 修改后同步发布版：
   - `dist/asr-labeling-tool/app/editor/index.html`
   - `dist/asr-labeling-tool/app/sync/sync_server.py`
   - `dist/asr-labeling-tool/app/voiceprint/voiceprint_server.py`
   - 提交并推送到 GitHub
9. 跑回归：
   - 撤销/重做
   - 重置为底稿
   - ASR 重生成
   - 项目切换、拉取已有 task 跳过
   - 同步后 Label Studio Annotation 中 tags 可见
   - 声纹分组
