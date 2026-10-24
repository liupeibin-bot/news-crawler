# news-crawler

每小时抓取 [路德社 ludepress.com](https://ludepress.com) 的新文章，生成**新闻摘要 + 投资参考**，发送到邮箱。

## 工作方式

1. `crawler.py` 通过网站的 WordPress REST API 增量抓取新文章（API 不可用时改用 RSS），只用 Python 标准库。
2. Claude 定时任务每小时运行一次：执行爬虫，阅读新文章，写摘要和投资参考，用 Gmail 发送，再把数据提交回本仓库。详细步骤见 [HOURLY_TASK.md](HOURLY_TASK.md)。
3. 没有新文章时不发邮件。

## 目录

| 路径 | 内容 |
|---|---|
| `crawler.py` | 爬虫 |
| `HOURLY_TASK.md` | 每小时任务的执行说明，改这里就能调整邮件内容和格式 |
| `data/state.json` | 上次抓取位置和已抓取文章 id（用于去重） |
| `data/articles/YYYY-MM-DD.jsonl` | 按发布日期（UTC）存档的全文 |
| `data/categories.json` | 分类 id 与名称对照 |
| `out/` | 每次运行的临时输出，不提交 |

## 手动运行

```bash
python3 crawler.py --init 20   # 第一次：从最新 20 篇开始
python3 crawler.py             # 之后：只抓新文章
```

## 说明

- 遵守网站 robots.txt，请求之间间隔 1 秒。
- 抓取内容仅供个人阅读和分析，不对外再发布。
- 投资参考由 AI 生成，不构成投资建议。
