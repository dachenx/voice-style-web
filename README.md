# voice-style-web

本地配音 Web：输入普通话文案和参考人物音频，确认武汉话表达稿后生成、试听、返修并导出音频。预留其他语言与模型接口。

当前交付至 M01：项目列表、创建、详情、重命名与本地持久化。参考音频、文案转换及语音生成尚未实现，声音效果尚未验收。

- [方案、形成过程和决策记录](docs/DECISIONS.md)
- [优化与修复记录](docs/OPTIMIZATIONS.md)
- [分模块开发及验收](docs/DEVELOPMENT.md)
- [团队与方案监督员](docs/TEAM.md)
- [代理必须遵守的规则](AGENTS.md)

## 开发检查

需要 Python 3.9+、Git。治理检查只使用 Python 标准库。

```sh
sh scripts/install-hooks.sh
python3 scripts/quality_gate.py quick
python3 scripts/quality_gate.py full --no-review
python3 scripts/quality_gate.py digest
python3 scripts/quality_gate.py full
```

每次完成一个有意义的代码片段执行 quick；模块结束运行 full，并取得独立方案监督员对当前内容摘要的审查。Git 提交与推送 Hook 会复查。

Codex 项目 Hook 定义在 `.codex/hooks.json`。首次或定义变化后，需在 Codex CLI 的 `/hooks` 中审阅并信任；未信任时不会自动执行。Git Hook 安装与 Codex 信任是两件事。详见 [OpenAI 官方 Hooks 文档](https://learn.chatgpt.com/docs/hooks)。

公开仓库仅存源码、合成测试和脱敏文档；真实声音样本、输出音频、模型、数据库与密钥放在忽略目录。未选择开源许可证前，不宣称额外授予开源使用权。

## 安装与启动 M01

需要 Python 3.11+、Node.js 22.12+ 和 Git。先确认 `python3 --version`、`node --version`；macOS 自带 Python 3.9 不适用。下面在仓库根目录运行，使用满足版本要求的 Python 创建环境：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r backend/requirements.lock
npm ci --prefix frontend
sh scripts/install-hooks.sh
python3 scripts/dev.py
```

打开终端输出的 [本地开发页面](http://127.0.0.1:5173)。脚本同时启动 Vite 和 FastAPI；按 Ctrl+C 关闭两个服务。开发页 `/api` 请求经 Vite 代理到 127.0.0.1:8000。端口被占用时先关闭占用程序，避免同时启动两份服务。

构建后可只运行一个服务：

```sh
npm --prefix frontend run build
python3 scripts/dev.py --built
```

页面为 [本地构建页面](http://127.0.0.1:8000)，接口文档在 [/docs](http://127.0.0.1:8000/docs)。以上命令面向 macOS/Linux；Windows 的虚拟环境可执行文件位于 `.venv/Scripts/`，Git Hook 需要 Git Bash。

项目名称为 1–80 个 Unicode 字符，去首尾空白，不能含控制字符。同名项目允许存在，ID 独立；每次提交成功后才显示保存提示。选中的项目 ID 保存在页面 hash 中，刷新可恢复详情；页面列表始终从服务端读取。

## 本地数据、迁移与恢复

数据库默认在仓库 `data/projects.sqlite3`，不随启动时所在目录改变，不提交到 Git。可在启动前设置 `VOICE_DATA_DIR` 为专用绝对目录；更换目录会打开另一份数据，不会自动搬迁原数据。

服务启动自动执行 `backend/app/migrations/` 中的编号 SQL，将已执行版本及 SHA-256 写入 `schema_migrations`。重复启动不重复迁移，不清空项目。迁移在事务中完成，失败回滚；未知未来版本、迁移记录断档或已执行 SQL 被改动时拒绝启动。请保留已执行的迁移文件，仅追加新编号。

备份时先关闭所有服务，再复制整个 `data/`（或自定义目录）到自己的备份位置。恢复时先关闭服务并保留当前目录的副本，再用备份目录替换，并运行与备份 schema 匹配的应用版本。不要删除数据库来修复迁移错误，也不要用旧应用强行降级新数据库。M01 没有自动备份或数据删除功能。

## 功能验收

首次安装浏览器测试运行时（与锁定的 Playwright 版本匹配）：

```sh
cd frontend
npx playwright install chromium
cd ..
python3 scripts/quality_gate.py full --no-review
```

Linux 无浏览器系统依赖时使用 `npx playwright install --with-deps chromium`。完整检查需要允许本地进程绑定回环端口；隔离环境禁用端口时应申请运行权限，不跳过测试。浏览器验收独占 18765 端口；冲突时设置 `VOICE_E2E_PORT`，不复用其他运行中的服务。

M01 检查包含后端 ruff/mypy/pytest、前端 ESLint/TypeScript/Vite 构建、桌面及 390px 窄屏 Chromium 浏览器行为。API 测试会用两个独立服务进程验证重启；浏览器测试使用真实 HTTP API，故障路径明确注入网络或 503 响应。所有测试数据库位于临时目录，不写入用户的 `data/`。

手动验收：创建项目 → 重命名 → 刷新页面 → 停止再启动服务 → 确认名称与项目仍在。当前只验证项目管理，不能用这些测试证明武汉话、音色或模型可用。

依赖使用 `frontend/package-lock.json` 与 `backend/requirements.lock` 固定。更新时应重新解析并提交锁文件，重新完成模块验收。环境与接口依据：[Vite 启动要求](https://vite.dev/guide/)、[FastAPI 本地服务](https://fastapi.tiangolo.com/deployment/manually/)、[Playwright 独立测试服务](https://playwright.dev/docs/test-webserver)。
