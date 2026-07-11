# CryptoRank 中文 Demo

把 `CryptoRank` 从“一个加密数据网站”升级成一个适合中文用户演示、内容生产和自动化承接的双形态工具：

- `Python CLI 版`
- `中文网页 Demo 版`

## 项目目标

- 还原 `CryptoRank` 免费层最值钱的入口
- 把市场、融资、Upcoming、空投活动压缩成中文雷达
- 做一个适合 YouTube 演示的工作台
- 给后续的 bot、skill、会员内容和自动化流程提供前端入口

## 当前能力

### Python CLI

入口文件：

- `scripts/cryptorank_demo.py`

支持模式：

- `radar`
- `funding`
- `upcoming`
- `airdrops`
- `brief`
- `raw`

示例：

```bash
python3 scripts/cryptorank_demo.py radar
python3 scripts/cryptorank_demo.py funding --limit 8
python3 scripts/cryptorank_demo.py brief --limit 5
```

### ClawHub 适配版 Skill

目录：

- `clawhub-skills/cryptorank-radar/`

特点：

- 自包含 skill 包结构
- 默认中文输出
- 支持 `radar / funding / upcoming / airdrops / brief / raw`
- 支持 `text / json / markdown` 三种输出
- 已包含 `SKILL.md`、`_meta.json`、`LICENSE.txt`、执行脚本、参考文档、打包脚本

示例：

```bash
python3 clawhub-skills/cryptorank-radar/scripts/run_skill.py --mode radar --lang zh --limit 5 --output json
python3 clawhub-skills/cryptorank-radar/scripts/package_skill.py
```

### 中文网页 Demo

技术栈：

- React 18
- TypeScript
- TailwindCSS
- Express

页面：

- `/`：首页工作台
- `/funding`：中文融资雷达
- `/opportunities`：中文机会页

接口：

- `/api/radar`
- `/api/funding`
- `/api/upcoming`
- `/api/airdrops`
- `/api/opportunities`
- `/api/brief`

## 启动方式

### 1. 安装依赖

```bash
npm install
```

### 2. 启动网页 Demo

```bash
npm run dev
```

启动后访问：

- `http://localhost:5173/`

### 3. 检查类型

```bash
npm run check
```

### 4. 运行测试

```bash
npm test
```

## 网页 Demo 特点

- 默认中文界面
- 桌面优先布局，适合录屏
- 深色终端雷达风格
- 后端支持双通道：优先尝试官方 `v2` API，失败时自动回退到免费层抓取
- 前端统一展示市场总览、榜单、融资、Upcoming、空投和今日摘要
- 支持刷新数据与复制摘要

## 为什么网页版走“Python + Express”

已经验证过的稳定链路在 `scripts/cryptorank_demo.py` 里。

由于当前环境下：

- Node 直接请求 `cryptorank.io` 可能出现不稳定连接
- 官方 `v2` API 在当前运行环境里还可能被 Cloudflare 拦截

所以网页 Demo 的后端会：

1. 先尝试官方 `v2` API
2. 如果失败，再复用 Python 抓取脚本
3. 最后把结果统一转换成前端可用的 JSON

这样做的好处：

- 抓取更稳
- CLI 和网页共用同一条数据链
- 更适合继续扩展成 bot / MCP / workflow

## 目录结构

```text
.
├── api/
├── public/
├── scripts/
│   └── cryptorank_demo.py
├── clawhub-skills/
│   └── cryptorank-radar/
├── shared/
├── src/
│   ├── components/
│   ├── hooks/
│   └── pages/
├── .trae/
│   ├── documents/
│   └── skills/
├── package.json
└── README.md
```

## 文档位置

产品与技术文档位于：

- `.trae/documents/cryptorank-web-demo-prd.md`
- `.trae/documents/cryptorank-web-demo-technical-architecture.md`

## 下一步适合继续做什么

- 增加 Markdown 导出
- 增加日报保存
- 做 Telegram / Discord 推送
- 封装为 MCP server
- 增加更完整的中文分类和标签映射
