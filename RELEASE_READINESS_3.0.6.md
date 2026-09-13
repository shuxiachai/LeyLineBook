# LeyLineBook 3.0.6 本地候选验收

> 本文保留本地候选阶段的授权范围和结果。后续验证分支、远端 CI 及发布进度见 [发布验证进度](RELEASE_PROGRESS_3.0.6.md)，不回写或抹去本阶段未执行的项目。

## 范围

- 目标：补齐首次从已发布 v3.0.5 升级到本地 3.0.6 候选的恢复保护与验收。
- 用户已明确要求仅本地验收；不提交、不推送、不打标签、不创建 Release、不部署线上 PWA，也不触发远端 CI。
- 用户进一步确认仅无界面验收。不得运行原生窗口、系统浏览器回退或会打开真实新程序界面的旧升级协调器；这些环节保留为未验证。
- 既有正式 EXE 和真实用户数据库不作为写入或运行目标；所有进程使用隔离目录、测试数据、独立会话和端口。
- 前一轮时间契约与 PWA 修复见 PROJECT_REVIEW_2026-09-13.md；其中的测试数量是该轮结果，不替代本轮验收。

## 旧版基线

| 项目 | 已核验值 |
| --- | --- |
| 发布标签 | v3.0.5 |
| 标签提交 | 505ff2d1c1d0dd2eb5af843b217f2df3dc8d7a34 |
| 文件名 | LeyLineBook-v3.0.5-Windows-x64.exe |
| 大小 | 16,154,324 字节 |
| SHA-256 | 590e1f54a898341c6ab1a1716505d51552aef6ec582afcace8e172b9aa4f8b95 |
| 来源核对 | 本地文件哈希与 GitHub 官方 release asset digest 一致 |

只读取上述原始文件，测试时复制到新的隔离目录再执行。旧更新器确认新界面就绪后会删除同目录旧 EXE，因此新版本必须在就绪前完成旧程序恢复材料准备，不能只保留原文件路径。

## 进度

| 项目 | 状态 | 验收要求 |
| --- | --- | --- |
| 官方旧包身份 | 已完成 | 文件大小与官方 SHA-256 一致 |
| 首次启动保护 | 已实现并通过定点独立复审 | 在旧库初始化前完成可验证快照；异常日志保守拒绝，不自动修复 |
| 候选版本同步 | 已完成 | 桌面/PWA 3.0.6、SW 缓存 v7；schema 4/timeContract 2 不变 |
| 新增保护回归 | 32 项定向及 124 项 Python 全量通过 | 成功、失败、重复启动、换入旧库、全新库、并发与异常日志边界 |
| 真实双 EXE 无界面首启与恢复 | 已通过 1 个完整成功场景 | 官方旧 EXE 造测试库、候选自动首启保护、完整恢复材料与隔离旧版读取；不替代完整升级 |
| 真实旧协调器与界面就绪 | 本轮不执行 | 用户仅允许无界面验收；旧协调器会打开新程序界面，不能运行或模拟就绪冒充通过 |
| Windows 合成场景 | 本轮完整 6 场景通过 | 正常及预期失败均符合断言；保留前轮一次 late 中断记录，不放宽断言 |
| 候选冻结 EXE | 已构建并通过 5 项冻结 API 验收 | 与双 EXE 验收使用同一最终产物，SHA-256 一致 |
| 本地 CI 配置 | 已增加门禁并通过定向审查 | 既有打包后增加冻结 API 验收与失败退出；远端未执行 |
| 独立审查 | 此次限定范围终审完成 | 独立只读核对最终报告、产物指纹、脚本范围及文案，未发现有证据的不一致；未重新执行测试 |
| 中断恢复后的收尾核验 | 已完成 | 13 项构建输入及新旧包哈希一致；4 个隔离目录没有匹配的残留进程 |
| 远端 CI / 正式发布 | 本轮不执行 | 用户明确暂不推送，不能标记为通过 |

## 证据边界

- 首次保护检查点与数据库初始化在同一事务提交；恢复材料失败不提交检查点。检查点不表示界面已经就绪，也不要求今后每次正常启动都保留历史恢复文件。
- 冻结 EXE 的自动首升需要保存并核验官方旧 EXE；手动替换且旧文件已不存在时，必须明确记录需重新下载旧版，而不能声称已保存旧程序。

- 未发布的本地候选不能走官方 releases/latest 的完整发现和下载路径。真实双 EXE 与旧协调器协议的验证，不等于 GitHub 更新发现/下载已通过。
- 本轮双 EXE 使用 `--no-browser`，新候选附加有效升级 health 参数以验证冻结程序的首次升级保护；没有实际界面就绪信号是预期结果。该测试不执行旧协调器，不证明程序替换及界面就绪闭环。
- 合成进程、源码 WebView2、冻结 EXE API、真实双 EXE 升级分别记录，不能互相代替。
- 不主动调用 /api/ready 冒充界面就绪，不修改官方旧 EXE，不放宽下载白名单或系统安全设置。
- 恢复只在另一全新数据目录演练；不自动覆盖当前数据库，也不让旧程序打开可能已有新写入的当前库。
- 普通 JSON 备份不包含账号凭据；恢复材料需要保留完整 SQLite 数据及其加密凭据。

实际 EXE 与合成进程结果见下文，分别说明验证层级，不用源码测试替代。

## 本轮运行环境与初步结果

- Windows 本地 Python 3.12.9、Node.js 24.14.0、PyInstaller 6.21.0、pywebview 6.2.1、tzdata 2026.2；没有安装或升级依赖。
- CI 配置的 Python 3.11、Node.js 22 与本地运行时不同，本地结果不能替代远端环境结果。
- `npm test`：55 项通过，0 失败、0 跳过。该轮在候选冻结前执行，最终产物验证另列。
- 新增冻结门禁的 2 项定向测试由实施方报告通过，独立只读审查未发现新增问题；不据此声称远端工作流已执行。
- `test_first_upgrade.py`（30 项）及 `test_update_recovery.py`（2 项）：32/32 通过，0 失败、0 跳过，1.506 秒。这是实施方定向执行结果，另由未参与实现的测试角色重跑全量。
- 定点审查关闭：非空 rollback journal 在 SQLite 连接前停止并保留原文件；手动同名覆盖不把候选误当旧 EXE；内部检查点完整结构校验；顶层可操作错误及失败说明；无界面异常即使日志写入失败仍退出 1；测试阶段日志隔离与 API 禁止重定向。
- 上述 journal 策略是保守拒绝异常残留，不是自动恢复，也不保证抵御任意外部并发文件改写。

## 独立全量与浏览器结果

| 命令 / 环境 | 结果 | 证据 |
| --- | --- | --- |
| `python -B -X utf8 -m unittest discover -s tests -p 'test_*.py' -v` | 124 通过、0 失败、0 跳过，15.240 秒 | `output/playwright/regression-20260913T000000Z/run-20260913T183745/logs/python-unittest.log` |
| `npm test` | 55 通过、0 失败、0 跳过 | 同目录 `npm-test.log` |
| 默认 `npm run test:browser` | 0 通过、8 失败；缺少 Chromium headless shell，浏览器没有启动 | 同目录 `browser-default.log`、`playwright-environment.log` |
| `PLAYWRIGHT_CHANNEL=msedge` 的常规 browser 矩阵 | 15 通过、0 失败、0 跳过 | `output/playwright/edge-headless-20260913T184453-fae7ccd2/browser-msedge-default.log` |
| 同上，另设 `LEYLINEBOOK_PWA_ONLY=1` | 11 通过、1 个预期桌面凭据子测试跳过、0 失败 | 同目录 `browser-msedge-pwa-only.log` |

Python/PWA 全量由未参与实现的测试角色执行；遇到缺失浏览器后停止，转交专家核验本机 Edge 153.0.4234.32 及项目原有 channel 支持，再使用默认 headless 模式运行浏览器矩阵。未安装 Chromium，未修改断言、超时、浏览器安全参数或持久化配置。默认 Chromium 的环境失败计数 1 保留，不能改写成该环境已修复。

源码与静态资源前后哈希一致，AST、Node 语法及 `git diff --check` 通过。独立审查者只读核对了 Edge 日志、headless 启动、退出码及 9 项哈希清单；PWA 日志有一条未归因的 Edge WidgetHost/Message6 诊断，相关测试及进程退出均通过，仍保留原始记录，不宣称日志无任何异常。

## 本地候选产物

- 构建命令：`python -m PyInstaller --clean --noconfirm --distpath <候选目录>/dist --workpath <候选目录>/build LeyLineBook.spec`。
- 候选目录：`output/playwright/candidate-3.0.6-ef35de70b4e04a9c812600e0bc9aa459/`。
- 程序：`dist/LeyLineBook-v3.0.6-Windows-x64.exe`，20,555,016 字节。
- SHA-256：`b5a7a9aa744a6f7e6a6a0d3272ff27ed45c6af35c2dd8c4d06adc5eaaec16aed`；同目录提供 `.exe.sha256` 文件。
- 构建只执行一次，退出码 0。`candidate.json`、`source-inputs.json` 与 `build.log` 保存在候选目录；构建前后输入哈希一致。
- 基线提交仍为 `c619e2f84f31e50b0bb3db6dd7886d2ae7130888`，候选包含未提交的工作区修改，并非该提交本身的可复现发行包。
- 构建日志保留 `pycparser.lextab`、`pycparser.yacctab`、`importlib_resources.trees` 缺失的 hidden-import 警告；构建成功不代表所有原生界面路径已验证。

## 实际产物验收

| 验收 | 命令 / 入口 | 结果与证据 |
| --- | --- | --- |
| 真实双 EXE 无界面首启与恢复 | `python -B -X utf8 scripts/real_upgrade_smoke.py --new-exe <本候选 EXE> --new-sha256 <上述 SHA-256>` | 成功场景通过；`output/playwright/real-headless-upgrade-540hbkw1/report.json` |
| 冻结 EXE 时区与认证 API | `python -B -X utf8 scripts/frozen_time_smoke.py --exe <同一候选 EXE>` | 5/5 通过；`output/playwright/frozen-time-5o17k19j/report.json` |
| Windows 合成升级协调 | `npm run test:upgrade` | 6/6 场景符合预期；`output/playwright/upgrade-fixture-zvw63lrt/report.json` |
| 双 EXE 脚本的纯 helper 回归 | `python -B -X utf8 -m unittest discover -s scripts/fixtures -p test_real_upgrade_helpers.py -v` | 4/4 通过；与完整 Python 全量分开计数，不当作真实 EXE 场景 |

真实双 EXE 场景先通过官方 v3.0.5 副本的认证 API 创建虚构账号、任务、记录及加密凭据；随后以 `--no-browser` 和有效升级 health 参数启动候选副本，验证首次保护已经完成，但 health 文件仍不存在。报告确认：升级前 SQLite 完整快照一致，旧程序存档匹配官方哈希；候选产生新写入后，另用全新数据目录及旧 EXE 副本读取快照，测试凭据可以解密，当前库新写入没有被覆盖。整个流程没有执行旧协调器、调用 `/api/ready`、打开原生窗口或访问 GitHub 更新发现/下载。

真实双 EXE 报告中的 `completeAutomaticUpgrade`、`oldCoordinatorExecuted`、`nativeReadyVerified`、`githubDiscoveryDownload`、`frozenFailureCasesExecuted` 均为 `false`，与本轮授权范围一致。缺旧 EXE、损坏旧 EXE、初始化失败等故障边界由隔离源码单元测试覆盖，不能据成功场景宣称它们已在冻结 EXE 上完整验收。

冻结 API 的 5 项分别为：拒绝 Sydney 夏令时空缺、拒绝未知 IANA 时区、接受重复小时的两个合法偏移、拒绝偏移不匹配。拒绝场景验证数据不变；合法场景验证导入、导出、确认档案及状态语义。报告同时记录实际打包时区资源。两个真实 EXE 报告的候选哈希均与本页产物一致。

合成流程中的 `healthy` 返回 0；`move_failure`、`start_failure`、`startup_failure`、`timeout`、`late` 按预期返回 1 并保留恢复证据。因此“6 场景通过”指测试断言通过，不是 6 次升级都成功。本轮 timeout 为 32.984 秒，late 为 32.891 秒，均正确判定 `health_timeout`。前轮一次 `0xC000013A` 中断及其未知来源仍保留在历史复核文档；本轮完整通过不构成对前次中断原因的解释。

## 收尾与结论

中断后未重新打包或修改程序代码。重新校验候选、官方旧 EXE、13 项构建输入及三份最终报告，并检查四个隔离输出目录对应的可执行文件路径和进程命令行，未发现匹配的残留进程。机器可读记录位于候选目录的 `final-verification.json`。测试日志和数据均留在 Git 忽略的 `output/playwright/` 内；遗留的旧路径索引也已归档，未删除验收证据。

收尾阶段的独立审查者只读核对了最终文案、脚本范围、候选指纹及报告哈希和结果，限定范围内未发现不一致或漏项；没有重新运行测试、启动 EXE 或操作远端。此前实现定点审查与本次证据核对分别记录，不将不同审查者的工作混为同一次审查。

当前版本可作为 **3.0.6 本地候选** 保存，已完成本轮允许的无界面验收。这里的通过不等同于完整发布验收：真实 Windows 界面就绪、官方旧协调器切换、完整更新发现/下载及远端 CI 仍未验证，也没有执行提交、推送、打标签、Release 或 PWA 部署。

本轮未增加 Windows 代码签名，不能保证系统不会拦截未签名程序；没有修改系统安全设置。schema 4 备份仍不能交给已发布的 v3.0.5 及更早版本导入。正式发布前仍须在另行允许的范围内完成真实界面/升级和目标 CI 环境验收，而不是将这些项目视为已通过。
