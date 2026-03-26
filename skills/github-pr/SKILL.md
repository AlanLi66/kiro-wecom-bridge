---
name: "github-pr"
description: "GitHub PR 管理与代码审查。当用户要求查看 PR、review 代码、列出仓库的 PR、在 PR 上留评论时使用。"
---

# GitHub PR 管理

通过 `github_cli.py` 操作 GitHub REST API，支持 PR 查看、diff 获取、代码审查等。

## ⚠️ 重要：GitHub 链接解析

当用户发送 GitHub PR 链接时，**必须解析 URL 提取参数，然后调用 github_cli.py**，禁止直接 curl 访问 GitHub URL（私有仓库会 404）。

URL 格式：`https://github.com/{owner}/{repo}/pull/{number}` 或 `https://github.com/{owner}/{repo}/pull/{number}/files`

解析示例：
- `https://github.com/yamibuy/ec-website-customer-next/pull/26` → owner=yamibuy, repo=ec-website-customer-next, number=26
- `https://github.com/yamibuy/backend-api/pull/123/files` → owner=yamibuy, repo=backend-api, number=123

收到 PR 链接后直接调用：
```bash
/mnt/i/workspace/kiro-wecom-bridge/.venv/bin/python3 /mnt/i/workspace/kiro-wecom-bridge/github_cli.py get_pr '{"owner": "yamibuy", "repo": "ec-website-customer-next", "number": 26}'
```

## 调用方式

```bash
/mnt/i/workspace/kiro-wecom-bridge/.venv/bin/python3 /mnt/i/workspace/kiro-wecom-bridge/github_cli.py <action> '<json_args>'
```

底层使用 `gh` CLI（已通过 `gh auth login` 授权），无需额外 token。

## 操作

### list_repos — 列出仓库

```bash
github_cli.py list_repos '{"owner": "yamibuy"}'
```

### list_prs — 列出 PR

```bash
github_cli.py list_prs '{"owner": "yamibuy", "repo": "backend-api", "state": "open"}'
```

state: open / closed / all

### get_pr — 获取 PR 详情

```bash
github_cli.py get_pr '{"owner": "yamibuy", "repo": "backend-api", "number": 123}'
```

返回：标题、描述、作者、增删行数、变更文件数、是否可合并等。

### get_pr_files — 获取 PR 变更文件列表

```bash
github_cli.py get_pr_files '{"owner": "yamibuy", "repo": "backend-api", "number": 123}'
```

返回每个文件的 filename、status、additions、deletions、patch（截断到 3000 字符）。

### get_pr_diff — 获取 PR 完整 diff

```bash
github_cli.py get_pr_diff '{"owner": "yamibuy", "repo": "backend-api", "number": 123}'
```

返回完整 diff 文本（超过 30000 字符会截断）。

### get_pr_comments — 获取 PR review comments

```bash
github_cli.py get_pr_comments '{"owner": "yamibuy", "repo": "backend-api", "number": 123}'
```

### create_review_comment — 在文件指定行留评论

```bash
github_cli.py create_review_comment '{"owner": "yamibuy", "repo": "backend-api", "number": 123, "body": "这里有潜在的 NPE 风险", "path": "src/main/java/Service.java", "line": 42}'
```

可选参数：`side`（LEFT/RIGHT，默认 RIGHT）、`commit_id`（不填自动取最新）。

### submit_review — 提交整体 review

```bash
github_cli.py submit_review '{"owner": "yamibuy", "repo": "backend-api", "number": 123, "body": "整体 LGTM，有几个小建议", "event": "COMMENT"}'
```

event 可选值：
- `COMMENT` — 普通评论
- `APPROVE` — 批准
- `REQUEST_CHANGES` — 请求修改

## PR Review 工作流

当用户说"帮我 review PR #123"时，推荐流程：

1. `get_pr` 获取 PR 概要（标题、描述、规模）
2. `get_pr_files` 查看变更文件列表，评估范围
3. `get_pr_diff` 获取完整 diff，逐文件审查
4. 发现问题时用 `create_review_comment` 在具体行留评论
5. 最后用 `submit_review` 提交整体评价

审查关注点：
- 🔒 安全问题（SQL 注入、XSS、硬编码密钥）
- 🐛 逻辑错误（NPE、边界条件、并发问题）
- 📐 代码规范（命名、重复代码、过长方法）
- ⚡ 性能问题（N+1 查询、不必要的循环）
- 📝 缺少必要的注释或文档
