# AStock Quant Console

本地运行的量化研究工作台框架：React / TypeScript 前端、FastAPI API、SQLite 控制与审计状态、Parquet 数据层，以及因子研究、策略回测、PAPER 模拟、事件与数据质量模块。

这是从本地项目提取的**源码发行版**。不包含行情数据库、个人持仓、账户记录、研究结果、OOS 接触历史、API 密钥、内部验收材料或旧 Git 历史。空数据启动后的待配置、缺数据和未资格化状态是正常状态；不等于完整市场覆盖或生产验收通过。

## 快速启动

需要 Python 3.11+、Node.js 20+ 和 npm。在仓库根目录运行：

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
python scripts/init_public.py
cd dashboard
npm ci
npm run build
cd ..
python scripts/v2/start_local.py
```

macOS / Linux 将虚拟环境激活命令换为 `source .venv/bin/activate`，其余命令相同。打开 [本地工作台](http://127.0.0.1:8888)。Windows 配置完成后也可以运行 `start_v2.bat`。

初始化只建立空表，重复执行会校验已应用的迁移校验和，不生成模拟收益或导入历史账户。数据不足时业务功能会阻断，页面不会自动拥有原安装中的数据。

## 开发与检查

```powershell
python -m pytest
python scripts/check_public_release.py
cd dashboard
npm run dev
```

开发前端默认使用 5173 端口，`/api` 代理到 8888。默认 pytest 收集独立单元、架构和逻辑测试。`tests/contract`、`tests/integration` 保留原数据集验收代码，其中部分需要自行准备发布数据、OOS 历史及运行证据，不属于空库快速启动检查。

## 配置与数据

- `config/v2/settings.json`：项目内相对路径、端口、资源限制与安全开关。
- `config/settings.json`：兼容引擎配置；相对路径按仓库根目录解析。
- `config/v2/ai.example.json`：可复制为同目录 `ai.local.json`，自行配置模型和密钥；默认关闭 AI。也可通过进程环境变量配置，见 `.env.example`。程序不自动加载 `.env`。
- `config/secrets.example.json`：兼容数据适配器的空配置模板，可复制为 `secrets.local.json`。
- 数据中转服务地址已换为 `.invalid` 示例域名，兼容适配器默认关闭。接入自己的服务时需核对协议、授权和 HTTPS 地址；V2 relay 的固定地址常量位于 `src/quantcore/activation/vendor.py`。
- `config/v2/operational_profile.json` 是待配置模板，私有 manifest 校验和已移除。使用前须按自己的已发布数据填写并验证。
- OOS 历史默认为空，不代表过去未使用过数据。迁移实际研究时应先导入自己的接触历史；未知窗口继续受访问策略限制。

运行数据写入 `data/`、`state/`、`artifacts/`、`logs/`，这些目录全部被 Git 忽略。数据提供方的行情和商业数据不随代码授权或分发。

## 目录

| 目录 | 用途 |
| --- | --- |
| `dashboard/` | React 看板源码与 npm 锁文件 |
| `quantweb/` | FastAPI 及只读展示适配 |
| `src/quantcore/` | 数据、研究、执行、会计、运行与安全核心 |
| `qforge/` | 兼容研究引擎 |
| `reference_engine/` | 独立参考执行引擎 |
| `scripts/` | 空库初始化、日常命令与发布检查 |
| `tests/` | 独立测试及需自备数据的验收测试 |

默认仅绑定本机回环地址，实盘交易、真实下单、券商访问及账户访问保持禁用。框架用于研究与模拟，回测与模拟表现不构成收益承诺。

## 上传 GitHub

**只在本发行目录创建新仓库并上传，不要上传原工作目录。** 先运行发布检查，再执行：

```powershell
git init -b main
git add .
git diff --cached --stat
git commit -m "Initial public framework release"
git remote add origin https://github.com/YOUR_NAME/YOUR_REPOSITORY.git
git push -u origin main
```

本发行版不含旧 Git 历史。后续提交前再次执行发布检查，并审阅暂存区。原创项目代码采用 [MIT License](LICENSE)；第三方资产保留各自许可，见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
