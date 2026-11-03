# ASR 标注工具交接文档

> 生成时间：2026-11-03 19:50 CST
> 项目目录：`/Users/mi/Documents/Codex/2026-11-03/lu`
> 发布仓库：https://github.com/onlylingo9817-sys/asr-labeling-tool
> 最新提交：`29b1026 Add session handoff notes`

新会话请先读完本文件，再按“当前服务状态 → 待人工确认事项 → 已验证功能 → 操作约束”执行。

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
| 编辑器 | 8765 | 运行中，screen `asr-lab-editor` |
| 同步服务 | 8768 | 运行中，screen `asr-lab-sync` |
| 声纹服务 | 8769 | 运行中，screen `asr-lab-voiceprint` |

健康检查：

```bash
cd /Users/mi/Documents/Codex/2026-11-03/lu
./scripts/status.sh

curl -sS http://127.0.0.1:8765/health   # 200
curl -sS http://127.0.0.1:8768/health   # 200
curl -sS http://127.0.0.1:8769/health   # {"ok":true}
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

## 三、声纹分组 Failed to fetch（已修复）

### 原现象

页面点击「🎙 声纹配置 → 开始分组」提示：

```text
Failed to fetch
```

### 原根因

1. `8769` 声纹服务没有启动。
2. 项目根目录没有 `.env`。
3. `scripts/start_all.sh` 默认 `ENABLE_VOICEPRINT=0`，因此启动时会打印：

```text
[skip] voiceprint (set ENABLE_VOICEPRINT=1 in .env to enable)
```

浏览器会直接 `fetch('http://127.0.0.1:8769/group')`，端口没有服务，所以报 `Failed to fetch`，不是算法返回的错误。

### 当前启动方式

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

### 声纹服务现状

- `work/voiceprints.json` 当前是空库：

```json
{"settings":{"threshold":0.06,"maxProfiles":6},"profiles":{}}
```

- 音频映射已改为按 `work/editor/data.json` 的 `taskId + projectId + audio` 动态解析，不再写死 task 5–8。显式 `VOICE_AUDIO_JSON` 仍可覆盖。

```python
DATA_FILE = Path(os.environ.get('EDITOR_DATA_FILE') or str(ROOT/'work/editor/data.json'))
AUDIO_OVERRIDES = {int(k): ROOT/v for k, v in _env_json('VOICE_AUDIO_JSON').items()}
```

因此：

- task 6（N90）音频存在，服务启动后可以分组。
- task 9（project 5）已能按 `audio/project5_task9.wav` 分组；前端请求同时携带 `projectId`，支持不同项目下同号 task。
- `profiles` 为空时，`/apply`、`/candidates` 会报“还没有注册声纹样本”，但 `/group` 使用无监督聚类，不依赖 profiles。

已实测：

- task 9：97 句 → 3 组，约 9 秒。
- task 6：941 句 → 3 组，约 146 秒（602 / 313 / 26 句），无前端错误。
- 长音频耗时主要来自逐句 Cam++ 特征提取；短音频表现正常。

### 为什么 N90 现在全是 UNKNOWN

N90 最近重新生成了 ASR：

- 当前本地数据：941 句，全部 `speaker=UNKNOWN`
- ASR 输出只负责文本、时间和插话，不负责说话人；`preprocess.asr_utterances()` 会明确把所有 speaker 初始化为 `UNKNOWN`
- 所以这不是“声纹把标签清了”，而是重新生成底稿后的正常初始状态

要恢复说话人：

1. 打开「🎙 声纹配置」
2. 点「开始分组」，逐组试听并确认角色
3. 或手工逐句选择说话人

注意：当前只生成分组建议，不会自动改写句子说话人；必须点「整组设为该角色」才会写入浏览器草稿。

---

## 四、当前编辑数据（以 `work/editor/data.json` 为准）

| task | project | 来源 | 句数 | 说话人概况 |
|---|---:|---|---:|---|
| 5 | 4 | N70-5 | 191 | SALES_1 132 / CUSTOMER_1 51 / CUSTOMER_2 4 / UNKNOWN 4 |
| 6 | 4 | N90-7 | 941 | 本地全部 UNKNOWN；Label Studio 旧 Annotation 另有 194 句自动标签 |
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

### N90 旧标签可复用情况（2026-11-03 核对）

- `work/editor/data.before-rebuild.json` 的旧 N90 有 194 句，与新 941 句中的 194 句起止时间**完全一致**。
- 这 194 句的说话人分布：SALES_1 97 / CUSTOMER_1 69 / CUSTOMER_2 22 / CUSTOMER_3 4 / CUSTOMER_4 1 / UNKNOWN 1。
- 它们全部是 `speakerSource=auto`、`speakerConfidence=low`、`reviewed=false`，来自 Label Studio task 6 旧 Annotation，不是人工确认。
- 因此可以恢复为“机器建议/待核”，但不应标记为人工确认；剩余 747 句没有旧标签。
- 本次未自动迁移，避免把低置信度自动结果误当人工真值。

### Label Studio 数据库状态（真实库）

数据库：`/Users/mi/Library/Application Support/label-studio/label_studio.sqlite3`

当前 Annotation：

- task 5：Annotation id=1，2 个 region（1 句），**人工原始标注，禁止删除/覆盖**
- task 6：Annotation id=5，448 个 region / 219 句（最近一次同步的旧版；本地已重新 ASR 为 941 句，尚未同步）
- task 7：Annotation id=4，354 个 region / 177 句
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

### 7. 声纹动态音频映射

- `work/voiceprint_server.py` 和发布版的 `app/voiceprint/voiceprint_server.py` 都已改为动态读取 `data.json`。
- `/group`、`/enroll`、`/candidates`、`/apply` 都支持可选 `projectId`。
- 旧调用只传 `taskId` 仍兼容；同号 task 跨项目时必须传 `projectId`。

### 8. 2026-11-03 回归结果

已通过（未触碰真实 Annotation）：

- 8765 / 8768 / 8769 三个 `/health` 均 200。
- task 9 `/group`：97 句、3 组、约 9 秒。
- task 6 `/group`：941 句、3 组、约 146 秒。
- 浏览器「声纹配置 → 开始分组」正常显示 3 组；请求体含 `projectId`。
- 说话人 SALES_1 → 撤销恢复 UNKNOWN → 重做恢复 SALES_1。
- 项目 4/5 切换，录音列表与草稿不串数据。
- 项目 4「从 Label Studio 拉取」：新增 0、跳过 4、缺音频 0。
- `work/editor/test_sync_payload.py` 6 项全部通过。
- task 6 `/sync` dry-run：941 句、2082 个 region（1041 句 + 100 个插话），全部待核。

尚未执行（需要真实数据写回或人工确认）：

- N90 的 941 句实际同步到 Label Studio task 6。
- ASR 重生成（会替换 `work/editor/data.json`，虽有备份）。
- 重置为底稿（会下载草稿并清除当前浏览器草稿）。
- 真实 Annotation 写回后的 tags 可见性检查。

### 9. 2026-11-03 优化轮（分组准确率 / 应用卡住 / 编辑性能）

**应用一直“应用中”的原因**：旧版把每个样本逐个 `/enroll`（每次 ffmpeg 切片+模型），一组 12 次约 14 秒，且登记会污染声纹库。现在“整组设为该角色”只改本地，瞬间完成；声纹样本登记改为可选勾选，并合并为一次批量请求。

**分组算法**（`work/voiceprint_server.py`）：
- 评测集：N70 前 2 分钟人工参考 78 句（`work/eval/`，仅本地）。旧算法 k=3 整体 80.8%，新算法（长句 Ward 聚类定说话人 + 全部句子按质心归属）k=3 91.0%、k=4 92.3%；“高置信”子集（≥1 秒且间距≥0.12）约 93–97%。
- 每个成员返回 `sure/margin`。应用时：高置信句直接确认；其余句可勾选“仅作待核建议”，不会标成人工确认。
- 音频整体读入内存、按样本数组直接推理；句子向量按录音落盘缓存（`work/outputs/vp_emb_*.pkl`），同一录音再次分组几乎瞬间。N90 首次仍需数分钟（逐句提特征），后续秒级。
- 局限：3 秒内短插话（嗯/好）声纹本身不可靠；已试过“轮次交替”规则，反而更差，未采用。

**声纹面板**：试听用单一播放器 + ▶/⏸；“换一批样本”；“在列表中查看本组”；范围可选“仅三段窗口内”；进度/耗时实时显示；应用可撤销（⌘Z）。

**编辑器**：
- 改说话人/标签/快捷键只刷新单行，不再整页重绘（N90 941 句：约 600ms → 约 5ms）。
- 新增“窗口内/窗口外”筛选；“仅同步三段内”开关（默认开，只写回前/中/尾窗口内句子；N90 即 194 句，而非 941 句）。
- 播放时自动高亮并滚动到当前句。
- 手动新增句子自动归入所在窗口。

回归：浏览器全流程通过（分组→应用→撤销、筛选、快捷键、跟随播放），无控制台错误；`test_sync_payload.py` 通过。真实 Annotation 未写入。测试产生的声纹样本已清空（备份在 `work/sync_backups/voiceprints-test-pollution-*.json`），`work/voiceprints.json` 为空库。

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
- 最新发布提交：`29b1026`（本次声纹修复尚未提交）

已比对：

- `work/editor/index.html` 与发布版一致
- `work/preprocess.py` 与发布版一致
- `work/make_segments.py` 与发布版一致
- `work/voiceprint_server.py` 与发布版已同步为动态音频解析，仅根目录/默认路径不同

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

`/group`、`/enroll`、`/candidates`、`/apply` 的请求体建议都带 `{taskId, projectId, ...}`。

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

1. 读本文件，运行 `./scripts/status.sh`，确认 8769 为 running。
2. N90 的 747 句窗口外句子：默认已只同步三段内（可在页面取消勾选）；是否从本地数据删除由人工决定。
3. 人工试听 N90 声纹分组，逐组确认角色；确认后再同步 task 6。
4. 如需复用旧 194 句标签，先按“自动建议/待核”迁移，不要标为人工确认。
5. 在明确批准后执行 task 6 真实同步；同步前自动备份，同步后检查 tags/region 可见性。
6. 按需执行 ASR 重生成、重置为底稿回归（都会改当前本地数据，执行前确认备份）。
7. 验证发布版动态音频映射；本次修复尚未提交，当前 GitHub `main` 仍是 `29b1026`。
