# EP19 研究状态与逐证券报告交接

更新时间：2026-11-08；执行者 `/root/parse_quality`。产品所有者明确要求停止新增实施，成果代码固化并详细交接。已经停止新增代码、扩展测试和截图；本文记录实际边界，不能作为 EP19 全部验收通过或生产发布证明。

## 固化位置

- 工作副本：`/Users/mac/Documents/Codex/2026-11-04/ni/work/tiger-repair-implementation-20261108/parse-quality`
- 最新本地提交：`919c459b9e1d93260b738d9d557169f80b1949aa`
- 提交标题：`feat(report): separate research evidence and account delivery states [T-2026-1106-0001]`
- 父提交：`1c2d255d9ea2b952c67b5ac674685883230363d7`（先前的专题语义与 max_conclusions 交付）
- 仅包含 5 个本子任务文件，工作树已回读干净。未推送 GitHub、未合并主干、未发布。
- 机器回执：`outputs/tiger-repair-implementation-20261108/research-status-receipt.json`
- 根代理集成副本：`work/tiger-intel-capacity-implementation-20261106/source`；根代理需自行选择是否在停止工作范围内 cherry-pick 固化，本文不暗示已经集成。

## 文件与当前行为

1. `trading_system/research_status.py`：纯投影，无网络、文件读取、计价、调度、重试、账户发布。输出 `tiger.research-status/v1`，四轴 `process / evidence / decision / account`，工作单元、原始文档、逐证券的独立稳定计数，阻塞原因与恢复条件、全证券卡、来源目录。
2. `trading_system/report.py`：新增同合同的 Markdown 状态块、全证券卡与完整来源目录。报告渲染固定为 `report_ready / pending_publication`；不接受 `raw.research_status` 中预计算的 committed 作为权威。新卡片保留原始执行价格精度，数值来自原有 `execution_decisions`，不补算价格/股数/2R。
3. `trading_system/report_html.py`：同样的四轴总览、全证券卡、真实搜索/状态筛选、空结果、禁用清除、证据折叠、同文档来源锚点；320/390/768/1440 宽度完成实际 Chrome 验证。新研究报告不再把所有未放行标的混入“淘汰率”，也不使用旧单票卡的衍生价格算式。旧无 intelligence 报告继续原有读路径。
4. `tests/test_repair_research_status.py`：29 个当前用例，包括原始 completed/account updated 不证明提交、verified_run 形状/身份/摘要绑定、partial 与 sufficient、合格 HOLD 与 WAIT_EVIDENCE、池外待补查、原始数值精度、重复身份与计数矛盾拒绝、观察取消与调度取消分离、来源转义与安全 URL、空报告。
5. `scripts/acceptance/research-report.mjs`：真实 Chrome 静态报告控件检查；与现有 `scripts/acceptance/report-layout.mjs` 共用 Playwright 加载器。输出目录必须新建，防止覆盖红绿证据。

## 调用合同（给 runner/runtime 维护者）

```python
project_research_status(
    raw=None,
    *,
    work_status=None,
    document_dispositions=None,
    verification=None,
    observed_state=None,
    report_ready=False,
    observation_state="connected",
    run_id=None,
)
```

- `raw`：当轮完整 `result.raw`。执行决定、审计漏斗、公司证据、intelligence gaps/frontier/bundle/source_index 均从这里取得。显示 action 时 report helper 加入 `PipelineResult.action`；runner 可提供原始 summary/action。
- `work_status`：原 `WorkStore.status()`。缺失时 unit 总数为 `None`，不冒充 0。仅 graph completed 映射 `work_completed`，不升级为业务回执。
- `document_dispositions`：原 `WorkStore.document_dispositions()`。按原始 document IDs 计算，校验 admitted = terminal + pending、状态分区与唯一身份。模型次数、分块数不能作为文档完成数。
- `verification`：仅允许调用方已经成功执行 `agent_runtime.verify_run` 后传入其完整返回值。纯投影不是独立密码学核验器；它只拒绝误传、混 run、receipt hash 不一致、缺 checks 与 raw/summary 冲突。`published-account-ancestry` 才能显示账户 committed；`prepared-unpublished-account` 仍 pending_publication。
- 完成态不要仅传 `summary.disclosures.research_audit`：现有 summary 只有 gates 汇总，没有全证券 rows。应在成功 `verify_run` 后读取该次已经核验的完整 result.raw。否则 buy 缺逐股 gate 会失败关闭。
- 进行态可传 `raw={}`、已观测 `work_status`、document dispositions、observed_state 与 run_id，缺证据和账户保持 unknown/unverified。
- 当前 observation 允许 `connected / disconnected / cancelled / loading`。取消观察不会停止调度；`cancel_requested` 单独表示调度取消请求，在途 unknown 需要先核对原请求。
- 根代理新增的 source_index builder 消费格式：`id,title,url,published_at,source_id,content_sha256,completeness`。同 ID 的不同 URL/title/日期/内容 hash 会拒绝冲突；不同 ID 即使相同 URL/正文仍单独保留。
- 根代理被告知应在新研究 raw 中加入 `run_id=execution.run_id`，报告才能显示原任务身份；此 integration 修改不在本提交。

## 证据与覆盖

范围内列出 26 个代码/最近测试文件：A 级完整读取 15，B 级执行/机检但未完整读取 6，C 级集成点选择性读取 5，未读 0。逐文件、checkout、SHA256 与等级在 `research-status-receipt.json`。B 级 6 文件属于被中止的扩大测试范围，不能声称每个文件全部测试已经跑完；C 级不声称理解了完整运行时实现。

已完成的验证：

- 初始 RED：`research-status-before.xml`，20/20 失败，包含未存在 pure module 与实际报告缺状态块/控件的断言。
- 补强 RED：`research-status-hardening-before.xml`，5 失败、24 通过，捕获无关池内行情缺失污染充分状态、模型动作冲突、危险 buy gate、缺计数假 0 与价格显示舍入。
- 最终聚焦：`research-status-focused.xml`，29 通过，0 失败/跳过。
- 报告最近回归：`research-status-report-regression.xml`，99 通过，4.64 秒。
- 扩大回归：`research-status-full-regression.xml`。收到停止指令后 Ctrl-C，185 已通过，104.34 秒，退出码 2；这是未完成测试，不得写作全量通过。
- 静态布局：`research-status-layout-first/layout.json`，2 份报告 × 4 宽度 × 5 页签，共 40/40 通过；无页面错误、无横向溢出。
- 真实控件：`research-status-controls-first/receipt.json`，4 宽度 × 9 检查，共 36 断言通过；空过滤、清除恢复、禁用态、keyboard focus、证据 details、同文档来源跳转、离线静态筛选均实际操作；0 page error、0 外部请求。
- 目视仅检查了 `research-status-controls-first/status-390.png` 与 `cards-1440.png` 两张；其余截图自动生成并通过布局测量，不冒充逐张目视。
- `git diff --check` 在提交前通过；显式停止后没有追加凭据扫描/广泛审计。

浏览器边界：原生 Chrome CUA 连接连续两次 30 秒超时，已发工具 completion_summary。后备使用本机安装的 Google Chrome 独立 headless QA context，未使用用户登录态，未访问外部账户。已启动的浏览器脚本全部返回并在 finally 关闭，临时 `127.0.0.1:8876` HTTP 服务也已停止。

## 夹具与可复跑入口

已生成夹具：`outputs/tiger-repair-implementation-20261108/research-status-preview/report.html`、`report.md`、`empty.html`。这是合成验证夹具，不是生产研究报告。内容含 7 个不同证券状态和一个未进入交易引用的 partial 原文。浏览器回执记录输入文件 SHA256。

Python：`/Users/mac/Documents/Codex/2026-11-04/ni/work/tiger-cloud-run-20261104/python-venv/bin/python`。
Node：`/Users/mac/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node`。
Playwright 解析环境：`NODE_PATH=/Users/mac/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules`。
Chrome：`/Applications/Google Chrome.app/Contents/MacOS/Google Chrome`。

如后续获准继续，原检查用例可原样重跑，无需重复设计：

```text
python -m pytest tests/test_repair_research_status.py tests/test_report_truthfulness.py tests/test_report_html.py tests/test_report_chain_count.py tests/test_report_whitepaper_assets.py tests/test_research_audit.py tests/test_intelligence_workflow_v2.py tests/test_intelligence_proof.py tests/test_intelligence_scoring_proof.py tests/test_intelligence_json_observation_proof.py -q --tb=short --junitxml=NEW_XML

node scripts/acceptance/report-layout.mjs --html-dir PREVIEW_DIR --evidence-dir NEW_DIR --executable CHROME
node scripts/acceptance/research-report.mjs --html PREVIEW_DIR/report.html --empty-html PREVIEW_DIR/empty.html --evidence-dir NEW_DIR --executable CHROME
```

浏览器 API 依据本次打开的 Playwright 官方 `https://playwright.dev/docs/screenshots` 与 `https://playwright.dev/docs/emulation`。没有升级或安装依赖。

## 尚未完成与后续顺序

1. **根代理独立 A 审阅尚未完成。** 需要重点看资格投影：`_verified` 不是替代 verify_run；`_security_rows` 的 scope、company gate 与原始 instruction 绑定；是否所有真实运行状态都能正确投影。
2. **runner/runtime/Node/MCP 尚未接入本提交。** `/root/impl_snapshots` 已收到函数签名与完成态使用完整 raw 的要求；其最新实现不在本工作树，不能宣称四端已经一致。
3. **根代理 source_index/run_id/proof 修改尚未与本提交联合回归。** 根代理消息说 source builder + 26 项自身测试已通过，但本子代理未重读最终差异、未独立核验。
4. **扩大回归被用户要求中止。** 使用新 XML 续跑，保留当前中止回执；不得覆盖或把 185 通过解释为全套完成。
5. **静态报告不能承担 live scheduler 控件。** 当前仅真实静态交互；异步加载、调度取消、外部模型 unknown 请求核对、断线后运行时恢复没有通过浏览器验证。文案明确不提供会触发付费或账户动作的假控件。
6. **真实生产来源、付费模型、main 集成和发布未执行。** 本提交只证明本地合同和显示机制，不能声明业务语义准确率或生产可用。

先前 topic 结果独立保留：`1c2d255` 的完整最终回归 387 passed / 2 skipped / 262.54 秒，`topic-semantics-receipt.json` 已更新，无需当作 EP19 回归重复运行。

## 收尾报告

- 已完成：用户停止指令要求的成果固化、既有短测试收尾、长测试中止、浏览器/HTTP 收尾和详细交接。
- 未完成/未覆盖：上面 6 项，均显式保留；不再继续实施。
- 自主追加事项及理由：发现稳定计数、buy gate 冲突和显示精度问题后已在停止指令之前补强；停止之后没有追加实施。
- 采用的假设：报告是发布前生成的封存快照；完整 verify_run 返回只由可信调用方传入；证券范围以原审计漏斗为准。
- 置信度：本地聚焦合同与静态展示为高；跨入口和生产整合为未验证。
- 建议复核：verify_run 调用边界；充分 HOLD 与池外待补查区分；原始价格/股数与完整来源目录。

## 自检表

- [是] 覆盖了停止实施、固化成果及详细交接的全部当前范围；未把 EP19 未完成部分冒充已交付。
- [是] 没有把已授权可自主完成的固化/工具收尾事项推给用户，没有不必要的问句。
- [是] 结论对应代码或回执，A/B/C/未验证边界显式，长测试中止没有伪装成功。
- [是] 交接文件、已提交代码和已保存证据可逐项审查；仍明确等待独立审阅。
