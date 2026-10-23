# 把本包传到你的 GitHub（GitHub Desktop 图形界面，和排课系统同一套路）

目标：在你的 GitHub 账号 `jason8668` 下新建一个仓库 `bike-callcenter`，把本包完整推上去。
之后云端部署、更新都只走这个仓库（服务器 `git pull`）。

## 第 1 步：在 GitHub 网站建空仓库

1. 浏览器打开 github.com，登录你的账号；
2. 右上角 **+** → **New repository**；
3. Repository name 填 `bike-callcenter`；选 **Private**（含客户业务设计，先私有）；
4. **不要**勾选 Add a README / .gitignore（本包自带，避免冲突）；
5. 点 **Create repository**，停留在建好的页面备用。

## 第 2 步：用 GitHub Desktop 关联本地文件夹

1. 把我给你的 zip 解压到本地一个固定位置，例如 `D:\projects\bike-callcenter`（解压后文件夹里直接能看到 README.md、docs、middleware 等，不要多套一层文件夹）；
2. 打开 GitHub Desktop → **File → Add local repository** → 选择上面那个文件夹；
3. 它会提示这还不是 Git 仓库，点 **create a repository**（Create a Git repository），确认后点 **Publish repository**；
4. 弹窗里确认账号是你的、仓库名 `bike-callcenter`、保持私有勾选，点 **Publish**。
   - 如果第 1 步已经在网站建了同名空仓库，Publish 时若提示已存在，改走菜单 **Repository → Push** 之前先在 Desktop 里 **File → Add local repository** 后选择 **Add remote** 填网站仓库地址 `https://github.com/jason8668/bike-callcenter.git`，再 Push。

## 第 3 步：检查推送结果

回到 GitHub 网站刷新 `bike-callcenter` 仓库，应能看到：README.md、docs/（5 个文档）、middleware/、freeswitch/、zammad-setup/、deploy/、VERIFY.md。
特别确认：**middleware/.env 没有被传上去**（本包只有 .env.example；.gitignore 已排除 .env，密钥永不进 Git）。

## 以后的修改流程（和排课系统一样）

我在本地改好 → 给你新的完整 zip → 你解压覆盖本地文件夹 → GitHub Desktop 里写一句提交说明 → **Commit to main** → **Push origin** → 服务器上跑更新脚本拉取。不要只传改动过的几个文件（排课系统第 4 批吃过亏：增量包覆盖导致仓库少文件，必须整包覆盖）。
