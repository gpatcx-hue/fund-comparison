# 固收+基金对比报告

自动化固收+基金对比分析工具，支持风格漂移检测、下行场景归因、Beta 剥离等专业分析。

## 项目结构

```
fund-comparison/
├── .github/workflows/daily-update.yml  # 每日自动更新 workflow
├── scripts/
│   └── auto_update.py                  # 数据管道脚本
├── public/
│   └── index.html                      # 前端页面
├── funds.json                          # 基金配置清单（编辑此文件增删基金）
├── nav_cache.json                      # 净值缓存（自动生成，git 忽略）
└── README.md
```

## 使用方式

### 在线访问（部署后）

打开页面后：
1. 点击 **"加载数据源"** 一键导入每日自动更新的数据
2. 或输入基金代码逐个添加（优先从数据源读取，缺失时自动从天天基金获取）
3. 所有表格数据均可点击编辑
4. 点击 **"一键生成"** 自动生成定性评估
5. 点击 **"生成报告"** 输出 PDF

### 本地运行数据管道

```bash
pip install requests python-dateutil
python scripts/auto_update.py
```

脚本会读取 `funds.json`，从天天基金拉取数据，输出 `public/fund-data.json`。

### 增删基金

编辑 `funds.json` 的 `funds` 数组即可。每次 GitHub Actions 运行时自动生效。

## 数据管道

每日北京时间 8:00 自动运行：

1. **拉取净值** — 天天基金移动 API，5 线程并发，增量更新
2. **计算指标** — 各期收益率、波动率、最大回撤、Sharpe/Calmar/Sortino
3. **季报仓位** — 最近 8 个季度股票/债券/现金占比（风格漂移检测）
4. **场景归因** — 5 个历史极端行情下的区间收益
5. **Beta 分解** — 以沪深300ETF和十年国债ETF为基准的 OLS 回归
6. **输出 JSON** — `public/fund-data.json`，前端自动读取

## 部署

### GitHub Pages

1. 创建新仓库 `fund-comparison`
2. 推送代码：`git init && git add . && git commit -m "init" && git remote add origin <url> && git push`
3. Settings → Pages → Source: GitHub Actions

### Cloudflare Pages

1. 连接 GitHub 仓库
2. Build: 无需（纯静态文件）
3. Output: `public/`

## 技术栈

- **前端**: 单文件 HTML + Chart.js + vanilla JS
- **数据**: Python + requests
- **CI/CD**: GitHub Actions
- **托管**: GitHub Pages / Cloudflare Pages
