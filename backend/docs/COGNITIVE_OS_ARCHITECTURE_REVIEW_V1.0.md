# Cognitive OS Backend — 架构评审报告 v1.0

> 评审对象：`D:\AI\code\cognitive-os\backend`
> 方法：本轮实测读取源码/构建文件/目录清单（命令与文件读取结果均来自本轮工具输出）
> 状态：事实与推断分离；文末集中列出**未验证假设**

---

## 0. 需求与约束确认

**需求**：确认技术栈与依赖清单、目录与模块边界、分层与调用关系、配置与外部依赖、数据流与关键抽象；产出优缺点清单，每条附具体文件/目录证据；标注未验证假设。

**约束（本轮可观察到的事实）**：
- 后端为 **C11 单体 + 静态库分层**：`CMakeLists.txt` 设置 `CMAKE_C_STANDARD 11`，构建出 `libcagent`（STATIC）并由 `cognitive-os-agent`、测试、bench、mock 等 9 个可执行目标链接。
- 存在**三套并行构建入口**：`CMakeLists.txt`、`Makefile`、`build.sh`/`build.ps1`/`build.bat`（Windows 走 Zig 0.14.1）。三者维护的源文件集合需同时成立（`Makefile` 用 `find src third_party/cJSON -name '*.c'` + 平台目录拼接，`CMakeLists` 用 `GLOB_RECURSE` + `list(FILTER EXCLUDE ...os/(windows|linux|macos|posix))`）。
- 本目录**不在 Git 仓库内**：`git` 工具在 `D:/AI/code/cognitive-os/backend` 报 “not a git repository (or any of the parent directories): .git”，回退到上级 `D:/AI/code` 也失败。无法给出提交级证据。

---

## 1. 技术栈与依赖清单（实测）

### 1.1 语言与运行时
| 项 | 证据 |
|---|---|
| C11 主线 | `CMakeLists.txt`（`set(CMAKE_C_STANDARD 11)`）；`include/cognitive-os-agent.h` 中 `#define CAGENT_VERSION "2.0.0"` |
| 平台后端二选一 | `CMakeLists.txt`：WIN32 → `src/os/windows/*.c` + `ws2_32 winhttp bcrypt crypt32 shell32 m`；否则 `src/os/posix/*.c` + `src/os/<linux|macos>/http.c` |
| 协程 | `README.md`（“ucontext on Linux, Fiber on Windows, behind a single coa_coro primitive”）|
| 规模 | 本轮统计：`src/**/*.c` 共 126 个 C/H 文件，约 55.5k 行；按模块：api 7、runtime 13、action 10、infra 9、memory 8、llm 7、os 16 |

### 1.2 第三方依赖（vendored，无包管理器）
| 依赖 | 位置 | 证据 |
|---|---|---|
| cJSON | `third_party/cJSON/cJSON.c`（2 文件 ~97 KB） | `CMakeLists.txt` 追加 `third_party/cJSON/cJSON.c` |
| wasm3 | `third_party/wasm3/wasm3_all.c`（33 文件 ~493 KB） | `CMakeLists.txt`；`include/plugin_runtime/wasm_runner.h` |
| tiny-regex | `third_party/tiny-regex/`（2 文件） | `src/infra/ssh_regex.c` 直接 `#include "../../third_party/tiny-regex/re.c"` |
| webview | `third_party/webview/`（5 文件 ~3.1 MB） | `tools/desktop.cc` 系桌面壳（推断，见 §8） |

### 1.3 非 C 依赖（脚本/测试/工具链）
- `tools/requirements-attachments.txt`：`Pillow>=10.4`、`PyMuPDF>=1.24`、`pypdf>=5`、`opencv-python-headless>=4.10`、`imageio-ffmpeg>=0.5`、`pillow-heif>=0.18`、`pywin32>=306 (win32)` —— 附件/文档/图像抽取链（对应 `src/api/attachment.c`、`tools/attachment_extract.py`）。
- `webarena/package.json`：唯一 npm 清单，依赖 `@playwright/mcp ^0.0.80` —— 仅评测用，**不进运行时**。
- 构建期 Python：`CMakeLists.txt` `find_package(Python3 REQUIRED)`，用 `tools/gen_web_ui.py` 生成 `include/api/web_ui.h`（≈5740 行，把 `apps/web/index.html` 323 KB 内联进二进制），`tools/gen_attachment_extractor.py` 生成 `include/api/attachment_extractor.h`。
- **无** `pyproject.toml`、无根级 `requirements.txt`：本轮多次 `glob`/`shell` 扫描仅命中 `tools/requirements-attachments.txt` 与 `webarena/package.json`（另有 `build/report-venv` 下的 site-packages 噪声，属构建产物）。

---

## 2. 目录与模块边界

```
backend/
├── include/<module>/*.h   ← 公共契约（与 src 同名 18 个模块目录）
├── src/
│   ├── action/(10)        tool_*.c 工具实现 + tools.c 注册/派发 + skill/mcp_conn/ssh_vault
│   ├── api/(7)            api_rest.c(5138) http_server websocket/ws_server auth attachment market_remote
│   ├── cognition/(5)      reasoning.c(4638) planner.c(1082) evaluator blackboard attention
│   ├── runtime/(13)       scheduler task state_machine policy_engine flow flow_store tasklog cron hook event_bus orchestrator state_store agent
│   ├── memory/(8)         memory service record episode graph kv promotion vector
│   ├── context/(1)        mmu.c(673)
│   ├── execution/(1)      executor.c(284)
│   ├── llm/(7)            llm openai anthropic mock router sse usage
│   ├── os/(16)            windows/ posix/ linux/ macos/ 后端 + os_* 抽象
│   ├── infra/(9)          config logging metrics audit trace catalog ringbuf util
│   ├── plugin_runtime/(6) manager sandbox capability registry filetracker wasm_runner
│   ├── plugin_intelligence/(6) analyzer architect codegen generator security testing
│   ├── retrieval/(3)      engine embedding context_builder
│   ├── security/(3)       secret_boundary secret_detector ssh_passwords
│   ├── snapshot/(2) tx/(1) im/(2) cluster/(1)
├── cli/main.c, cli/client.h       终端客户端（复用服务端会话/工具）
├── apps/web/index.html            Web UI（构建期内联）
├── tools/                         代码生成、mock LLM/MCP、评测、打包
├── tests/                         46 个 Python 测试 + test_all.c/test_scenario.c/bench_*
└── state*/                        多个运行态目录（state、state-test-* 共 20+）
```

**边界判断（基于实际 include 边）**：本轮实测跨模块 include 计数（`(root)->os 6`、`action->infra 12`、`action->os 20`、`runtime->os 23`、`llm->infra 12`、`api->os 16`）。可读出的分层是：
`cli/api → runtime/cognition → action/execution/tx/snapshot → infra/security/os → third_party`。

---

## 3. 分层与调用关系（关键路径）

1. **入口**：`cli/main.c` 直接调用或 `serve` 起 HTTP；`src/cognitive_os_agent.c`（1413 行）是**装配根**：`version()` + `runtime_ctx`（`include/cognitive-os-agent.h` 列出 30+ 字段：bus/scheduler/policy/memory/snapshot/tools/llm/txm/reasoning/blackboard/agents/auth/trace/router/usage/registry/skills/mcp/cluster/hooks/state/memsvc/attention/index/im/channels/cron/tasklog/flowstore）。
2. **任务生命周期**：`runtime/task.c` + `scheduler.c` + `state_machine.c`（RECEIVE→UNDERSTAND→REASON→PLAN→ACT→VERIFY→LEARN，见 README 与 `include/runtime/state_machine.h`）；终态由 `cognitive_os_agent.c` 的 `task_done()` 回写 `state_store_task_set` + `tasklog_record` + `flow_store_mark`——**持久化三个写点集中在同一个 hook**，是核心一致性锚点。
3. **推理主循环**：`cognition/reasoning.c` → `planner_plan_ex()`（`src/cognition/planner.c:1105`）→ LLM（`src/llm/llm.c` + `openai.c`/`anthropic.c`）→ 工具计划 → `action/tools.c` 的 `tool_execute()`（`:332` 调 `policy_check`）→ observation 回流。
4. **工具面**：`action/tools.c:103-114` 注册 12 个内置工具：`file_read/list_dir/file_write/file_edit/shell/ssh/git/mcp/skill/skill_manage/glob/grep`。与计划器提示中的工具集一致。
5. **事务/回滚**：`tx/tx.c`(191) + `snapshot/snapshot.c`(373) + `snapshot/cow.c`(48)，被 `execution/executor.c` 与 action 写工具包裹。
6. **多智能体**：`runtime/orchestrator.c`（decompose → `flow_run` → merge，失败回落单 agent），`runtime/flow.c`(756) 按拓扑层并行、`{{nodeid}}` 占位引用建边、Kahn 判环（`include/runtime/flow.h`）。
7. **外部协议**：`api/websocket.c`（RFC6455 帧 + SHA1/base64 握手）+ `api/ws_server.c` 分片处理；`llm/sse.c` 增量流。

---

## 4. 配置与外部依赖

- **配置加载链**：`infra/config.c` 明确 `defaults → JSON 文件 → 环境变量(COA_*)`，点号 key、`config_save_file` 原子写。实测 `state/cognitive-os-agent.json` 形如 `{"llm.provider":"openai","llm.model":"deepseek-v4.1-flash","reasoning.max_rounds":-1,"scheduler.workers":8,"tx.use_transaction":true}`。
- **环境变量面**（实测 grep）：`COA_LLM_TIMEOUT_MS`、`COA_TOOL_PROTOCOL`、`COA_LLM_VISION_TIMEOUT_MS`、`COA_HTTP_STREAM_TRANSPORT`、`COA_SHELL`、`COA_SNAPSHOT_MAX_FILE`、`COA_CLIENT_TOKEN`、`COA_DOCUMENT_PYTHON`、`COA_BENCH_BINARY`、`TERM`（`src/terminal_screen.h`）。**共 10 个，分散在 8 个模块**，无集中登记表。
- **外部依赖面**：LLM HTTP（OpenAI-compatible `/v1/chat/completions`、Anthropic `/v1/messages`，`WinHTTP`/libcurl）、可选 MCP 子进程（`tools/mock_mcp_server.js`）、SSH（`os` 进程 + `ssh_vault` 凭据库：macOS Keychain `SecKeychain*`、Windows `CryptProtectData`）、`market.url` 远程市场。
- **凭据**：`state/cognitive-os-agent.json` 中 `llm.api_key` 以明文落盘（本轮 shell 输出 `cognitive-os-agent.json len=46 state=plaintext`），而 SSH 口令走系统密钥库（`src/action/ssh_vault.c`）——**两条凭据路径安全等级不一致**。

---

## 5. 数据流与关键抽象

**主数据流**：`HTTP/WS/CLI → api_rest → task → scheduler(coroutine) → reasoning → context/mmu + retrieval/engine → planner → llm(SSE) → planned_action[] → tools(tx+snapshot+policy) → observation → reasoning 循环 → tasklog/state/memory → 回答返回`。

**关键抽象（接口契约）**：
| 抽象 | 头文件 | 契约要点 |
|---|---|---|
| `tool` / `tool_exec_fn` | `include/action/tools.h` | `{name,description,json_schema,is_write,execute,ud}`；`token_json_schema` 子集校验；`is_write` 决定是否进事务 |
| `tool_ctx` | 同上 | workspace/state_root/session_id/should_abort 等**显式注入**，无全局单例 |
| `llm` + `llm_vtable` | `include/llm/llm.h` | `chat/stream/destroy` 三方法；`cancel` 协程检查点；响应含 `http_status/stop_reason/tool_call_count/truncated` 结构化诊断 |
| `planner_model_diagnostic` | `include/cognition/planner.h` | 明确只含元数据、**不含正文与密钥** |
| `reasoning_config` | `include/cognition/reasoning.h` | 预算 `budget_hot/warm/cold` + `exec_backend(local|wsl|remote)` + `max_rounds<0=unlimited` |
| `executor_ops` | `include/execution/executor.h` | `start/execute/stop/destroy/snapshot/restore`，Local/WSL/Remote 同签名替换 |
| `flow` DAG | `include/runtime/flow.h` | JSON `{nodes,edges}`，`{{id}}` 隐含依赖，Kahn 判环 |

**上下文治理**：`src/context/mmu.c` + `reasoning_config.budget_*`（默认 hot 8192 / warm 3072 / cold 4096 字符），超预算自动降级（hot 折叠最老 turn、warm 收缩 worklog、cold 收缩检索）——`include/cognition/reasoning.h` 注释即契约。

---

## 6. 优点清单（附证据）

| # | 优点 | 证据 |
|---|---|---|
| P1 | **控制权在 runtime 而非 prompt**：策略检查在工具派发层强制执行，模型无法绕过 | `src/action/tools.c:332` `policy_check(...)`；`planner_plan_ex` 的 `policy` 参数“deny 规则既阻断调用又从目录移除” |
| P2 | **递归架构换执行环境不改上层**：Executor vtable 已抽象 | `include/execution/executor.h` 注释 + `executor_new_wsl/remote` |
| P3 | **每层都有稳定头文件契约**：`include/` 与 `src/` 18 个同名模块一一对应，`cognitive-os-agent.h` 为伞头 | 目录实测；`include/cognitive-os-agent.h` 逐层 include 清单 |
| P4 | **写操作可回滚**：`is_write` 标记 + cow snapshot + tx，三者解耦 | `include/action/tools.h`（`is_write`）、`src/snapshot/cow.c`、`src/tx/tx.c` |
| P5 | **上下文预算显式化、可降级**，而非依赖模型窗口 | `include/cognition/reasoning.h` `budget_hot/warm/cold`；`src/context/mmu.c` |
| P6 | **可观测性一等公民**：trace span + 结构化进度 + 逐模型用量账本 | `include/infra/trace.h`、`include/llm/usage.h`、`reasoning_progress()`；`orchestrator.c` 的 `llm_simple_booked()` 避免 decompose/merge 漏记 token |
| P7 | **无限轮次不被默认预算截断，但有防循环**：`max_rounds<0` 为默认，循环检测按“不同成功观测”判定 | `include/cognition/reasoning.h`；README 并发模型小节 |
| P8 | **诊断信息有安全边界**：planner 诊断只含 `http_status/stop_reason/stage` 等元数据 | `include/cognition/planner.h` `planner_model_diagnostic` 注释 |
| P9 | **依赖零外部包管理器**：vendored cJSON/wasm3/tiny-regex，离线可构建 | `third_party/` 实测 42 文件；`CMakeLists.txt` 追加清单 |
| P10 | **测试面广**：46 个 Python e2e/UI 测试覆盖 CLI、workspace、流式预览、IM、崩溃恢复、可选缺失读取等真实路径 | `tests/` 清单（本轮 list_dir）+ `CMakeLists.txt` `add_test` |
| P11 | **配置分层明确**：defaults→file→env，点号 key，原子落盘 | `include/infra/config.h` |

## 7. 缺点清单（附证据）

| # | 缺点 | 证据 | 影响 |
|---|---|---|---|
| C1 | **巨型文件**：`api_rest.c` 5138 行、`reasoning.c` 4638 行、`cognitive_os_agent.c` 1413 行 | 本轮行数统计 | 变更冲突与评审成本高；`reasoning.c` 已同时承担状态机编排、工具循环、循环检测、记忆写入 |
| C2 | **三套构建系统并行且逻辑重复**（CMake `GLOB_RECURSE`+FILTER / Make `find`+wildcard / build.sh 三份平台选择） | `CMakeLists.txt`、`Makefile`、`build.sh` | 新增平台或源目录需同步改三处；`GLOB_RECURSE CONFIGURE_DEPENDS` 对老 CMake 有陷阱 |
| C3 | **环境变量无集中登记**，10 个变量散落 8 个模块 | grep 结果（`llm.c:40`、`planner.c:649/802`、`proc.c:61`、`snapshot.c:57`、`api_rest.c:3999`…） | 文档化/校验缺失，拼写错误静默失效 |
| C4 | **LLM API key 明文落盘**，与 SSH 口令走系统密钥库的做法不一致 | `state/cognitive-os-agent.json` 实测 plaintext；`src/infra/../action/ssh_vault.c` 用 Keychain/CryptProtectData | 备份/共享 state 目录即泄密 |
| C5 | **`runtime_ctx` 上帝对象**：30+ 字段，几乎所有模块经它耦合 | `include/cognitive-os-agent.h` 结构体定义 | 单测需构造全量上下文；跨模块循环依赖风险（`cognition->api`、`tx->action` 的 include 边已出现） |
| C6 | **`include/api/web_ui.h` 5740 行生成文件入源码树** | `CMakeLists.txt` 生成规则 + 行数统计 | diff 噪声巨大；`apps/web/index.html` 任一改动都会污染源码历史 |
| C7 | **状态目录污染源仓库**：`state`、`state-bench`、`state-test-*` 等 20+ 目录与 7 个 `build*.log` 平铺在 `backend/` 根 | 本轮 list_dir 输出 | 无 `.gitignore` 可核（该目录非 Git 仓库），归档/打包易夹带运行态与密钥 |
| C8 | **平台层用宏切换而非运行期**：`include/cognitive-os-agent.h` 用 `_WIN32` 定义 `WINDOWS/POSIX` 宏 | 头文件实测 | 无法在单进程内跑跨平台后端；交叉编译需精确过滤源（`CMakeLists` 的 EXCLUDE 正则即是补丁） |
| C9 | **`third_party/tiny-regex` 以 `.c` 直接 include 方式使用** | `src/infra/ssh_regex.c:2` `#include "../../third_party/tiny-regex/re.c"` | 违反分离编译，`re.c` 的符号无法被多处共享；改动影响面不透明 |
| C10 | **`webview` 体积 3.1 MB 占 third_party 主体，但与运行时无构建关系** | `third_party/webview/` 5 文件 3.14 MB vs `CMakeLists.txt` 只链 cJSON/wasm3 | 仓库膨胀；桌面壳与 runtime 的边界未在构建中表达 |
| C11 | **`max_rounds=-1` 默认无限 + 循环检测靠启发式**（“不同成功观测”集合） | `include/cognition/reasoning.h`；README 并发模型小节 | 长任务失败模式难在编译期约束，只能靠运行时兜底 |
| C12 | **`build/report-venv` 等构建产物混入扫描范围** | 多次 glob/shell 命中 `build/report-venv/Lib/site-packages/...` | 污染依赖清单扫描（本轮已排除），也提示构建产物未与源码隔离 |

---

## 8. 未验证假设（明确标注）

1. **`third_party/webview` 仅用于 `tools/desktop*.cc` 桌面壳**——由文件名与目录推断，**未读 `tools/desktop.cc` 内容**，也未在 `CMakeLists.txt` 中找到引用（CMake 未编译它）。
2. **`Makefile` 中 `LDLIBS` 的 `uname` 分支在 Windows MSYS 下是否与 CMake 的 `bcrypt/crypt32/shell32` 完全等价**——只读了 `Makefile` 前 ~45 行，未验证链接成功（本轮未执行构建）。
3. **`include/api/web_ui.h` 的确由 `apps/web/index.html` 单文件生成**（无 JS/CSS 拆分）——由 `CMakeLists` 的 `DEPENDS apps/web/index.html` 推断，未读 `tools/gen_web_ui.py`。
4. **`cognition -> api` 的 include 边是否为真实编译依赖还是注释/前向声明**——来自目录级 grep 计数，未逐条读该 include 语句。
5. **`Claude/Anthropic` 流式路径“回落到完整答案”的现状**——来自 README 描述，未读 `src/llm/anthropic.c` 验证。
6. **`state*` 目录是否全部为测试残留、有无被运行时默认加载**——只看了 `state/` 下 14 个文件，未逐个目录检查。
7. **`CAGENT_VERSION "2.0.0"` 与文档 `v3.0/v3.1` 的版本对应关系**——两者并存，未找到映射说明。
8. **性能/容量指标（`docs/COGNITIVE_OS_ARCHITECTURE_BASELINE_V3.0.md` §3 的 10k events/s、p95<5ms 等）均为“工程目标非实测”**——文档自身声明，未复测。

---

## 9. 结论与建议优先级

**架构定性**：这是一个**分层清晰、契约先行、以 runtime 为中心**的 C11 单体。`include/` 与 `src/` 的 18 个同名模块构成了可辩护的模块边界，Executor/Tool/LLM/Flow 四个 vtable 或 JSON 契约是真正的可替换点。主要风险不在架构设计，而在**工程卫生**：巨型源文件、三套构建、散落环境变量、明文密钥、生成物入源码树。

**建议（按 ROI 排序）**：
1. 拆 `api_rest.c`（按 `/v1/tasks`、`/v1/memory`、`/v1/flows` 路由族）与 `reasoning.c`（拆 loop / completion-check / cycle-detection / memory-write）——先解锁可测试性。
2. 把环境变量收敛为 `infra/config.c` 中的一张注册表（名称、默认值、说明），`getenv` 只在该处出现。
3. `llm.api_key` 复用 `security/secret_boundary.h` 的既有边界，与 SSH 口令统一走系统密钥库。
4. 选定 CMake 为唯一构建真相源，`Makefile`/`build.sh` 改为薄包装。
5. `web_ui.h` 移出 `include/`（生成到 `build/generated/`），并把 `state*`、`build*.log` 纳入忽略规则。
6. tiny-regex 改为正常编译单元，不再 `#include *.c`。
