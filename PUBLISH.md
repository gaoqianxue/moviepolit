# 推送到仓库 / 发布为插件仓库

本目录**已经是一个初始化好并完成首次提交的 git 仓库**（分支 `main`）。你只需要补一个远端地址就能推。

## 一、推送（二选一）

### 方式 A：用仓库里的助手脚本

Windows / PowerShell：

```powershell
cd C:\Users\十一\Documents\deepseek-harness\default-workspace\moviepolit-plugin

# 公开仓库或已有凭据
.\push.ps1 -RepoUrl https://github.com/<用户名>/<仓库名>.git

# 私有仓库：用 Personal Access Token（GitHub 需勾选 repo 权限）
$env:GIT_TOKEN = "ghp_xxxxxxxxxxxxxxxx"
.\push.ps1 -RepoUrl https://github.com/<用户名>/<仓库名>.git -Branch main
```

Linux / macOS / 容器内：

```bash
./push.sh https://github.com/<用户名>/<仓库名>.git main
GIT_TOKEN=ghp_xxx ./push.sh https://github.com/<用户名>/<仓库名>.git main
```

脚本会自动：补提交 → 设置 remote → push，失败时 `pull --rebase` 后重试。

**关于 token 安全**：脚本里的 `origin` 永远只写「不带 token」的地址，token 仅在推送那一刻作为
git 参数临时传入，因此**任何失败路径都不会把 token 写进 `.git/config`**（已实测：失败后全量
grep `.git/` 目录搜不到 token 字样）。推送失败时脚本会以非零退出码结束，不会出现「假成功」。

如果本机直连 GitHub 不通，脚本会自动读取系统代理（WinINET / `HTTPS_PROXY` 环境变量）；
也可以手动指定（`127.0.0.1:7897` 是本机常见端口，按你的实际软件填）：

```powershell
.\push.ps1 -RepoUrl https://github.com/<用户名>/<仓库名>.git -Proxy http://127.0.0.1:7897
```

```bash
GIT_PROXY=http://127.0.0.1:7897 ./push.sh https://github.com/<用户名>/<仓库名>.git main
```

> **Windows 若报「未对文件进行数字签名」**：这是 PowerShell 执行策略拦的，在**同一个窗口**先执行
> `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force` 再跑脚本即可（只影响当前窗口，
> 不会改系统设置）。也可以用 `powershell.exe -ExecutionPolicy Bypass -File .\push.ps1 ...`。

### 方式 B：手动三条命令

```bash
cd <本目录>
git remote add origin https://github.com/<用户名>/<仓库名>.git
git push -u origin main
```

> 本机 `~/.gitconfig` 没有配置 user.name / user.email，仓库内已用 local 配置
> `MoviePolit <moviepolit@local>` 兜底；想换成你自己的身份：
> `git config user.name "你的名字"; git config user.email "你的邮箱"`，然后 `git commit --amend --reset-author`。

## 二、登记到 MoviePilot 插件市场

推送后，仓库结构应为：

```
<仓库根>/
├─ package.v2.json          # 市场清单，key 必须与插件类名一致：MoviePolit
├─ plugins.v2/
│  └─ moviepolit/           # 目录名 = 类名全小写
│     ├─ __init__.py
│     └─ README.md
├─ push.ps1 / push.sh       # 可选，给本地推送用，不影响市场
├─ .gitignore
└─ .gitattributes
```

在 MoviePilot「设置 → 插件 → 插件市场」里添加插件仓库地址
`https://github.com/<用户名>/<仓库名>`，然后在市场中搜索「流媒体上线监控」安装。
（插件图标当前指向 MoviePilot 官方仓库的 `icons/movie.jpg`；若想用自己的图标，把 png 放到
仓库的 `icons/` 目录，并同时改 `__init__.py` 的 `plugin_icon` 与 `package.v2.json` 的 `icon`。）

## 三、手动安装（不走市场）

把 `plugins.v2/moviepolit` 复制成 MoviePilot 的 `app/plugins/moviepolit/`（目录名全小写），
重载插件或重启容器后在「本地插件」启用。Docker 通常是：

```bash
docker cp plugins.v2/moviepolit <容器名>:/app/app/plugins/moviepolit
```

## 四、关于 `https://github.com/users/gaoqianxue/projects/1`

这是 GitHub 的 **Projects 看板**（项目面板）地址，不是 git 仓库地址，**不能用来 push**，
`git clone` / `git remote add` 都会失败。它只能确定账号名是 `gaoqianxue`。

要推送必须先在 GitHub 上**新建一个空仓库**（例如 `moviepolit`，建议公开，插件市场才能直接拉取），
然后使用它的 HTTPS 地址：

```
https://github.com/gaoqianxue/moviepolit.git
```

> 实测 `gaoqianxue` 名下**目前没有任何公开仓库**，`https://github.com/gaoqianxue/moviepolit.git`
> 通过代理访问返回 `Repository not found`，所以这个仓库确实还需要你先在网页上创建。

## 五、关于 `https://discovery-api.intern-ai.org.cn`

该地址**不是 git 服务**：`/`、`/api/v1/version`、`/api/v4/version`、`/<repo>.git/info/refs?service=git-upload-pack`
实测全部返回 Go 风格的 `404 page not found`，也没有 OpenAPI 文档（`/openapi.json` 同 404），
因此无法作为 `git remote` 使用。如果它其实是别的用途（例如某个内部制品/模型 API），
请告诉我它的接口路径与鉴权方式，我再按它的协议上传。
